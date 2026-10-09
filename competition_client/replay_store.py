"""Private, source-separated observation and decision journal for replay.

This is independent of the durable submission store: telemetry failures must
never change a decision or prevent a time-sensitive submission.
"""
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
import platform
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit
import uuid

from .http_client import redact
from .protocol import require

LOG = logging.getLogger(__name__)
SOURCES = {"auto", "sohu_online", "local_arena", "competition_custom"}
_PRIVATE_KEYS = {"chat", "authorization", "key", "key_file", "keyfile", "api_key",
                 "apikey", "token", "access_token", "credential", "credential_fingerprint"}


def source_for(config):
    host = urlsplit(config.base_url).hostname
    source = getattr(config, "log_source", "auto")
    require(source in SOURCES, "Invalid log_source")
    if host == "poker.bang.sohu.com":
        require(source in {"auto", "sohu_online"}, "Sohu endpoint must use sohu_online logs")
        return "sohu_online"
    require(source != "sohu_online", "sohu_online logs require the Sohu endpoint")
    if source != "auto":
        return source
    return "local_arena" if host in {"localhost", "127.0.0.1", "::1"} else "competition_custom"


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items() if str(k).lower() not in _PRIVATE_KEYS}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "[unsupported:" + type(value).__name__ + "]"


def encoded(value):
    return json.dumps(clean(value), ensure_ascii=False, sort_keys=True, allow_nan=False)


def private_directory(path):
    path = Path(path).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(path.stat().st_mode & 0o077 == 0, "Replay directory must have mode 0700")
    return path


def private_fd(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    return fd


class ReplayJournal:
    def __init__(self, config):
        self.source = source_for(config)
        self.directory = private_directory(Path(config.state_dir).expanduser() / "telemetry" / self.source)
        self.path = self.directory / "replay.sqlite3"
        os.close(private_fd(self.path, os.O_CREAT | os.O_RDWR))
        self.db = sqlite3.connect(self.path, timeout=0.1)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (run_id TEXT PRIMARY KEY, manifest TEXT);
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY, run_id TEXT, received_at TEXT,
                competition_id TEXT, table_id TEXT, hand_id TEXT, request_id TEXT, body TEXT);
            CREATE INDEX IF NOT EXISTS observations_hand ON observations(competition_id, table_id, hand_id);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, run_id TEXT, recorded_at TEXT, kind TEXT, body TEXT);
        """)
        for row in self.db.execute("SELECT manifest FROM sessions"):
            previous = json.loads(row[0])
            require(previous["source"] == self.source and previous["competition_id"] == config.competition_id
                    and previous["base_url"].rstrip("/") == config.base_url.rstrip("/"),
                    "Replay directory belongs to a different source, endpoint or Competition")
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12]
        self.failed, self.last_observation = False, None
        root = Path(__file__).resolve().parents[1]
        paths = list((root / "competition_client").glob("*.py"))
        paths += [Path(config.strategy).expanduser().resolve()]
        # The adapted strategy delegates to this frozen baseline.
        if paths[-1] == root / "hero_bots/bot_sohu_v16/bot.py":
            paths.append(root / "hero_bots/bot_v16_squeeze_C_metacap/bot.py")
        hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        self.manifest = {"schema_version": 1, "source": self.source, "run_id": self.run_id,
                         "started_at": self.now(), "base_url": config.base_url,
                         "competition_id": config.competition_id, "strategy": config.strategy,
                         "file_sha256": hashes, "config": {
                             k: v for k, v in vars(config).items() if k != "key_file"},
                         "visibility": "agent_observations_only", "chat_recorded": False}
        self.manifest["public_table_stream"] = self.source == "sohu_online"
        self.manifest["visibility"] = "agent_observations_and_public_table_stream" if self.source == "sohu_online" else "agent_observations_only"
        from importlib.metadata import PackageNotFoundError, version
        self.manifest["environment"] = {"python": platform.python_version(), "platform": platform.platform()}
        try:
            self.manifest["environment"]["eval7"] = version("eval7")
        except PackageNotFoundError:
            self.manifest["environment"]["eval7"] = None
        with self.db:
            self.db.execute("INSERT INTO sessions VALUES (?,?)", (self.run_id, encoded(self.manifest)))

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    def _write(self, statement, values):
        if self.failed:
            return False
        try:
            with self.db:
                self.db.execute(statement, values)
            return True
        except (sqlite3.Error, OSError):
            self._fail()
            return False

    def _fail(self):
        if not self.failed:
            self.failed = True
            LOG.error("Replay recording failed; submissions continue. source=%s run=%s", self.source, self.run_id)

    def _encode(self, value):
        if self.failed:
            return None
        try:
            return encoded(value)
        except (ValueError, TypeError, RecursionError):
            self._fail()
            return None

    def observation(self, observation):
        body = self._encode({k: observation[k] for k in (
            "competitionId", "agentId", "competitionType", "competitionStatus",
            "status", "bankroll", "standing", "table", "actionRequest") if k in observation})
        if body is None or body == self.last_observation:
            return
        table = observation.get("table") or {}
        hand, request = table.get("hand") or {}, observation.get("actionRequest") or {}
        if self._write("INSERT INTO observations VALUES (NULL,?,?,?,?,?,?,?)", (
                self.run_id, self.now(), observation.get("competitionId"), table.get("id"),
                hand.get("id"), request.get("id"), body)):
            self.last_observation = body

    def event(self, kind, data):
        body = self._encode(data)
        if body is not None:
            self._write("INSERT INTO events VALUES (NULL,?,?,?,?)", (self.run_id, self.now(), kind, body))

    def close(self):
        self.db.close()


def attach_runtime_log(journal):
    path = journal.directory / (journal.run_id + ".log")
    handle = os.fdopen(private_fd(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "w", encoding="utf-8")
    handler = logging.StreamHandler(handle)
    from .http_client import RedactingFormatter
    handler.setFormatter(RedactingFormatter(
        f"%(asctime)s %(levelname)s source={journal.source} run={journal.run_id} %(message)s"))
    logging.getLogger().addHandler(handler)
    return handler
