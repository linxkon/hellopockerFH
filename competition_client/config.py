"""Explicit config and Unix credential/driver checks."""
import argparse
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
from .protocol import identifier, require

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Config:
    competition_id: str
    key_file: str
    base_url: str = "https://poker.bang.sohu.com"
    strategy: str = str(ROOT / "hero_bots/bot_sohu_v16/bot.py")
    state_dir: str = "~/.local/state/fullhouse-sohu"
    http_timeout: float = 10.0
    strategy_timeout: float = 1.0
    submit_reserve: float = 2.0
    max_clock_skew: float = 5.0
    idle_interval: float = 30.0
    queued_interval: float = 5.0
    seated_interval: float = 2.0
    expected_summary: dict = field(default_factory=dict)
    log_source: str = "auto"

    def validate(self):
        identifier(self.competition_id, "competition_id")
        for name in ("http_timeout", "strategy_timeout", "submit_reserve", "max_clock_skew", "idle_interval", "queued_interval", "seated_interval"):
            value = getattr(self, name)
            require(type(value) in {int, float} and math.isfinite(value) and value > 0, f"Invalid config: {name}")
        require(isinstance(self.expected_summary, dict), "expected_summary must be an object")
        from .replay_store import source_for
        source_for(self)
        strategy = Path(self.strategy).expanduser().resolve()
        require(strategy.is_file(), "Strategy entry does not exist")
        self.strategy = str(strategy)
        state = Path(self.state_dir).expanduser().resolve()
        require(not state.is_relative_to(ROOT), "Runtime state must be outside repository")
        self.state_dir = str(state)
        return self


def parse_config(argv=None):
    parser = argparse.ArgumentParser(description="Single-driver Sohu poker client")
    parser.add_argument("--config")
    for name in ("competition-id", "key-file", "strategy", "state-dir"):
        parser.add_argument("--"+name)
    parser.add_argument("--check", action="store_true", help="Read-only Discovery and strategy startup validation; never Join")
    args = parser.parse_args(argv)
    data = {}
    if args.config:
        with open(args.config, encoding="utf-8") as handle:
            data = json.load(handle)
        require(isinstance(data, dict), "Config must be an object")
    require(set(data) <= set(Config.__dataclass_fields__), "Unknown configuration field")
    for name in ("competition_id", "key_file", "strategy", "state_dir"):
        if getattr(args, name) is not None:
            data[name] = getattr(args, name)
    require(bool(data.get("competition_id")) and bool(data.get("key_file")), "--competition-id and --key-file are required")
    return Config(**data).validate(), args.check


def read_key(filename):
    path = Path(filename).expanduser().absolute()
    require(not path.is_symlink(), "Credential file must not be a symlink")
    path = path.resolve()
    require(not path.is_relative_to(ROOT), "Credential file must be outside repository")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        import stat
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_mode & 0o077 == 0,
                "Credential file must be owned by current user with mode 0600")
        with os.fdopen(fd, encoding="utf-8") as handle:
            fd = None
            key = handle.read(4097).strip()
        require(key.startswith("sk_") and 3 < len(key) <= 4096 and not any(c.isspace() for c in key), "Invalid credential format")
        return key
    finally:
        if fd is not None:
            os.close(fd)


class DriverLock:
    def __init__(self, key):
        # Global per credential, even if two invocations use different state-dir.
        directory = Path.home() / ".local/state/fullhouse-sohu-locks"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(directory.stat().st_mode & 0o077 == 0, "Lock directory must be private")
        path = directory / (hashlib.sha256(key.encode()).hexdigest()+".lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.handle = os.fdopen(fd, "w")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            raise RuntimeError("Another driver is running for this Agent") from None

    def close(self):
        self.handle.close()
