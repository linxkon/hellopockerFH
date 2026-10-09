"""Standard-library HTTP service and independent action deadline timer."""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from competition_client.http_client import ERROR_STATUSES
from .arena import ArenaError
from .credentials import authenticate

LOG = logging.getLogger(__name__)


def strict_json(raw):
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:
                raise ValueError("Duplicate JSON field")
            result[k] = v
        return result

    def invalid_constant(value):
        raise ValueError("Non-finite JSON number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)


class ArenaHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True

    def __init__(self, address, arena):
        self.arena = arena
        self.stopping = threading.Event()
        super().__init__(address, Handler)
        self.timer = threading.Thread(target=self._tick, name="arena-deadlines", daemon=True)

    def start_timer(self):
        self.timer.start()

    def _tick(self):
        while not self.stopping.wait(0.02):
            try:
                with self.arena.lock:
                    self.arena.tick()
            except Exception:
                # Failed engine/report operations must halt play, not silently
                # serve inconsistent observations or continue with lost logs.
                LOG.error("Arena timer failed; run marked interrupted")
                self.arena.stop("internal_error")
                self.stopping.set()
                self.shutdown()

    def server_close(self):
        self.stopping.set()
        if self.timer.is_alive():
            self.timer.join(timeout=2)
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalPokerArena/1"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass  # Never log request paths, bodies, or Authorization.

    def reply(self, status, data):
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def error(self, code):
        self.reply(ERROR_STATUSES[code], {"error": {"code": code}})

    def do_GET(self):
        try:
            try:
                path = urlsplit(self.path)
            except ValueError:
                raise ArenaError("invalid_request") from None
            query = parse_qs(path.query, keep_blank_values=True)
            if (path.path != "/api/competitions" or set(query) - {"status"}
                    or "status" in query and (len(query["status"]) != 1 or query["status"][0] not in {"scheduled", "active", "ended", "cancelled"})):
                raise ArenaError("invalid_request")
            with self.server.arena.lock:
                self.server.arena.tick()
                data = self.server.arena.discovery()
                if "status" in query:
                    data["competitions"] = [c for c in data["competitions"] if c["status"] == query["status"][0]]
            self.reply(200, data)
        except ArenaError as exc:
            self.error(exc.code)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self._internal_error()

    def do_POST(self):
        try:
            try:
                path = urlsplit(self.path)
            except ValueError:
                raise ArenaError("invalid_request") from None
            command = path.path.removeprefix("/api/competitions/")
            if path.query or path.path != "/api/competitions/" + command or command not in {"join", "observe", "action", "leave"}:
                raise ArenaError("invalid_request")
            auth = self.headers.get_all("Authorization", [])
            agent_id = authenticate(auth[0], self.server.arena.config.participants) if len(auth) == 1 else None
            if agent_id is None:
                raise ArenaError("authentication_required")
            if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
                raise ArenaError("invalid_request")
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1:
                raise ArenaError("invalid_request")
            try:
                length = int(lengths[0])
                if not 0 < length <= 65536:
                    raise ValueError()
                body = strict_json(self.rfile.read(length))
            except (ValueError, UnicodeError, TimeoutError):
                raise ArenaError("invalid_request") from None
            self.reply(200, self.server.arena.handle(command, agent_id, body))
        except ArenaError as exc:
            self.error(exc.code)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self._internal_error()

    def _internal_error(self):
        LOG.error("Arena request failed; run marked interrupted")
        try:
            self.server.arena.stop("internal_error")
            self.error("internal_error")
        finally:
            self.server.stopping.set()
            threading.Thread(target=self.server.shutdown, daemon=True).start()
