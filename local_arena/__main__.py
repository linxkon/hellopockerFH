"""Create private matches and run their HTTP service."""
import argparse
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import sys
import threading
import uuid
from urllib.parse import urlsplit

from competition_client.http_client import RedactingFormatter, redact

from .arena import Arena
from .config import ArenaConfig, runtime_path
from .credentials import fingerprint, new_key
from .reporting import Reporter, private_json
from .server import ArenaHTTPServer


def initialize(args):
    path = runtime_path(args.directory)
    names = args.names or [f"Agent {i + 1}" for i in range(args.players)]
    if len(names) != args.players:
        raise ValueError("--names must have one name per player")
    participants, keys = [], []
    for name in names:
        key = new_key()
        keys.append(key)
        participants.append({"agent_id": str(uuid.uuid4()), "name": name, "key_hash": fingerprint(key)})
    data = {}
    if args.template:
        data = json.loads(Path(args.template).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) - (set(ArenaConfig.__dataclass_fields__) - {"competition_id", "participants"}):
            raise ValueError("Invalid arena template fields")
    for key in ("hands", "small_blind", "big_blind", "buy_in", "initial_bankroll", "action_timeout", "seed", "dealer_seat", "host", "port"):
        if getattr(args, key) is not None:
            data[key] = getattr(args, key)
    data.setdefault("seed", secrets.randbits(64))
    cfg = ArenaConfig(competition_id=str(uuid.uuid4()), participants=participants, **data)
    base_url = (args.base_url or f"http://127.0.0.1:{cfg.port}").rstrip("/")
    url = urlsplit(base_url)
    if (url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"})
            or not url.hostname or url.path or url.query or url.fragment or url.username or url.password):
        raise ValueError("Use HTTPS for remote clients, or a loopback HTTP URL through an SSH tunnel")
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    private_json(path / "server.json", cfg.to_dict())
    for i, key in enumerate(keys):
        player_path = path / "participants" / f"agent-{i + 1}"
        player_path.mkdir(parents=True, mode=0o700)
        key_path = player_path / "agent.key"
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(key + "\n")
        private_json(player_path / "client.json", {
            "competition_id": cfg.competition_id, "key_file": str(key_path), "base_url": base_url,
            "state_dir": str(player_path / "client-state"), "strategy_timeout": 1.0,
            "log_source": "local_arena",
            "idle_interval": 0.1, "queued_interval": 0.05, "seated_interval": 0.02,
        })
    print(f"Created {args.players}-player match: {cfg.competition_id}")
    print(f"Server config: {path / 'server.json'}")
    print(f"Participant bundles: {path / 'participants'}")


def serve(args):
    source = Path(args.config).expanduser().absolute()
    if source.is_symlink():
        raise ValueError("Server config must not be a symlink")
    path = runtime_path(source)
    if path.stat().st_mode & 0o077:
        raise ValueError("Server config must have private permissions (0600)")
    cfg = ArenaConfig(**json.loads(path.read_text(encoding="utf-8")))
    directory = path.parent
    if directory.stat().st_mode & 0o077:
        raise ValueError("Runtime directory must have private permissions (0700)")
    server = arena = reporter = None
    try:
        arena = Arena(cfg)
        # Bind before creating result files, so a busy port does not consume a run.
        server = ArenaHTTPServer((cfg.host, cfg.port), arena)
        reporter = Reporter(directory)
        arena.reporter = reporter
        arena._save_report()
        def shutdown(signum, frame):
            # HTTPServer.shutdown cannot run on the serve_forever thread.
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)
        server.start_timer()
        print(f"Arena listening on {cfg.host}:{server.server_port}; competition={cfg.competition_id}", flush=True)
        server.serve_forever(poll_interval=0.1)
    finally:
        if server:
            server.server_close()
        if arena:
            arena.stop()
        if reporter:
            reporter.close()


def main(argv=None):
    os.umask(0o077)
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    parser = argparse.ArgumentParser(description="Internal 2–6 player Sohu-protocol evaluation arena")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("init", help="Create a new run and private participant bundles")
    create.add_argument("--directory", required=True)
    create.add_argument("--players", type=int, choices=range(2, 7), required=True)
    create.add_argument("--names", nargs="+")
    create.add_argument("--template")
    create.add_argument("--base-url", help="URL used by clients; defaults to loopback and configured port")
    for name in ("hands", "small-blind", "big-blind", "buy-in", "initial-bankroll", "action-timeout", "seed", "dealer-seat", "port"):
        create.add_argument("--" + name, type=int)
    create.add_argument("--host")
    run = sub.add_parser("serve", help="Run one match; completed results are never overwritten")
    run.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        initialize(args) if args.command == "init" else serve(args)
        return 0
    except Exception as exc:
        logging.error("Command failed: %s", redact(str(exc)))
        return 1


if __name__ == "__main__":
    sys.exit(main())
