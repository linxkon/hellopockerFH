from copy import deepcopy
from datetime import datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from competition_client.config import Config, DriverLock, read_key
from competition_client.http_client import APIError, HTTPClient, NetworkError, redact
from competition_client.protocol import ProtocolError
from competition_client.strategy_worker import StrategyWorker
from tests.sohu_helpers import COMPETITION_ID, DISCOVERY, observation


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.reply()

            def do_POST(self):
                self.reply()

            def reply(self):
                cls.captured.append((self.path, self.headers.get("Authorization"), self.rfile.read(int(self.headers.get("Content-Length", 0)))))
                if cls.delay:
                    time.sleep(cls.delay)
                self.send_response_only(cls.code)
                self.send_header("Date", cls.server_date)
                self.send_header("Retry-After", cls.retry_after)
                if cls.code == 302:
                    self.send_header("Location", "/redirect-target")
                self.end_headers()
                try:
                    self.wfile.write(cls.body)
                except BrokenPipeError:
                    pass
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        type(self).code, type(self).body = 200, json.dumps(DISCOVERY).encode()
        type(self).server_date = format_datetime(datetime.now(timezone.utc), usegmt=True)
        type(self).retry_after, type(self).delay, type(self).captured = "1", 0, []
        self.client = HTTPClient(self.url, "sk_dummy_unit_test", timeout=3.0)

    def test_discovery_is_public_action_header_only(self):
        self.assertEqual(self.client.request("discovery"), DISCOVERY)
        self.assertIsNone(self.captured[0][1])
        type(self).body = json.dumps(observation()).encode()
        body = {"competitionId": COMPETITION_ID, "tableId": "t", "actionRequestId": "opaque", "decision": {"type": "call"}}
        self.client.request("action", body)
        path, header, raw = self.captured[1]
        self.assertEqual(header, "Bearer sk_dummy_unit_test")
        self.assertNotIn(b"sk_", raw)
        self.assertEqual(json.loads(raw), body)
        self.assertEqual(path, "/api/competitions/action")

    def test_known_error_and_retry_after(self):
        type(self).code, type(self).retry_after = 503, "3"
        type(self).body = b'{"error":{"code":"temporarily_unavailable","message":"sk_do_not_print"}}'
        with self.assertRaises(APIError) as raised:
            self.client.request("discovery")
        self.assertEqual(raised.exception.retry_after, 3)
        self.assertNotIn("sk_", str(raised.exception))

    def test_fail_closed_nonjson_unknown_code_wrong_status_redirect(self):
        cases = [(200, b"<html>oops</html>"), (500, b'{"error":{"code":"new_error"}}'),
                 (200, b'{"error":{"code":"internal_error"}}'), (401, b'{"error":{"code":"stale_table"}}'),
                 (302, b'{}')]
        for code, body in cases:
            type(self).code, type(self).body = code, body
            with self.subTest(code=code), self.assertRaises(ProtocolError):
                self.client.request("discovery")
        self.assertEqual(len(self.captured), len(cases))  # no redirect followed

    def test_clock_skew_stops_discovery(self):
        type(self).server_date = "Sat, 01 Aug 2026 00:00:00 GMT"
        with self.assertRaises(ProtocolError):
            self.client.request("discovery")

    def test_socket_timeout_is_ambiguous(self):
        type(self).delay = 0.1
        with self.assertRaises(NetworkError):
            self.client.request("join", {"competitionId": COMPETITION_ID}, timeout=0.01)
        time.sleep(0.15)  # allow the intentionally timed-out handler to finish

    def test_credentials_and_redaction(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"key"
            path.write_text("sk_dummy_unit_test")
            path.chmod(0o600)
            self.assertEqual(read_key(path), "sk_dummy_unit_test")
            path.chmod(0o644)
            with self.assertRaises(ProtocolError):
                read_key(path)
        self.assertNotIn("sk_", redact("oops sk_dummy_unit_test; Authorization: Bearer secret"))
        self.assertNotIn("secret", redact("Authorization: Bearer secret"))

    def test_single_driver_lock_across_state_dirs(self):
        first = DriverLock("sk_dummy_unit_test_lock")
        self.addCleanup(first.close)
        with self.assertRaises(RuntimeError):
            DriverLock("sk_dummy_unit_test_lock")


class WorkerTests(unittest.TestCase):
    def test_actual_baseline_worker_and_interruptible_timeout(self):
        from competition_client.adapter import ObservationAdapter
        worker = StrategyWorker(str(Path("hero_bots/bot_sohu_v16/bot.py").resolve()))
        self.addCleanup(worker.close)
        state = ObservationAdapter().convert(observation())
        result = worker.decide(state, 1)
        self.assertIn(result["action"], {"fold", "check", "call", "raise", "all_in"})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"slow.py"
            path.write_text("import time\ndef decide(state):\n    if state.get('type') != 'warmup': time.sleep(5)\n    return {'action':'fold'}\n")
            slow = StrategyWorker(str(path))
            self.addCleanup(slow.close)
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                slow.decide(state, 0.05)
            self.assertLess(time.monotonic()-started, 0.5)
            self.assertIsNone(slow.process)
