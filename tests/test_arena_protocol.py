import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from competition_client.http_client import HTTPClient
from local_arena.arena import Arena
from local_arena.server import ArenaHTTPServer
from tests.arena_helpers import configuration, FakeClock, actor, action_body


class ArenaProtocolTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.arena = Arena(configuration(3), clock=self.clock)
        self.server = ArenaHTTPServer(("127.0.0.1", 0), self.arena)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.clients = [HTTPClient(self.url, f"sk_local_test_{i}") for i in range(3)]
        self.ids = list(self.arena.members)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def raw(self, path, body=None, key="sk_local_test_0"):
        req = Request(self.url + path, data=body,
                      headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        try:
            response = urlopen(req, timeout=2)
        except HTTPError as exc:
            response = exc
        with response:
            return response.code, json.loads(response.read())

    def test_public_discovery_query_strictness(self):
        status, data = self.raw("/api/competitions?status=active")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["competitions"]), 1)
        self.assertEqual(self.raw("/api/competitions?status=ended")[1]["competitions"], [])
        for path in ("?status=active&status=active", "?unknown=x", "?status=", "?status=bad"):
            self.assertEqual(self.raw("/api/competitions" + path)[0], 400)

    def test_auth_and_strict_json(self):
        body = json.dumps({"competitionId": self.arena.config.competition_id}).encode()
        self.assertEqual(self.raw("/api/competitions/join", body, key="sk_wrong")[0], 401)
        for body in (b'[]', b'{"competitionId":"a","competitionId":"b"}', b'{"competitionId":NaN}', b'{}'):
            self.assertEqual(self.raw("/api/competitions/join", body)[0], 400)
        bad = json.dumps({"competitionId": self.arena.config.competition_id, "extra": 1}).encode()
        self.assertEqual(self.raw("/api/competitions/join", bad)[0], 400)

    def test_agent_specific_request_and_chat_idempotency(self):
        competition = {"competitionId": self.arena.config.competition_id}
        for c in self.clients:
            c.request("join", competition)
        current = actor(self.arena)
        index = self.ids.index(current)
        body = dict(action_body(self.arena, current, {"type": "call"}), chat=" hello ")
        status, obs = self.raw("/api/competitions/action", json.dumps(body).encode(), key=f"sk_local_test_{index}")
        self.assertEqual(status, 200)
        self.assertEqual(obs["table"]["hand"]["actions"][-1]["chat"], "hello")
        self.assertEqual(self.raw("/api/competitions/action", json.dumps(body).encode(), key=f"sk_local_test_{index}")[0], 200)
        changed = dict(body, chat="different")
        self.assertEqual(self.raw("/api/competitions/action", json.dumps(changed).encode(), key=f"sk_local_test_{index}")[1]["error"]["code"], "idempotency_conflict")
        other = (index + 1) % 3
        self.assertEqual(self.raw("/api/competitions/action", json.dumps(body).encode(), key=f"sk_local_test_{other}")[1]["error"]["code"], "stale_action_request")

    def test_actual_timer_expires_without_http_traffic(self):
        competition = {"competitionId": self.arena.config.competition_id}
        for c in self.clients:
            c.request("join", competition)
        self.clock.elapsed = 31
        self.server.start_timer()
        import time
        until = time.monotonic() + 2
        while time.monotonic() < until:
            with self.arena.lock:
                if sum(m["timeouts"] for m in self.arena.members.values()):
                    break
            time.sleep(0.01)
        self.assertEqual(sum(m["timeouts"] for m in self.arena.members.values()), 1)

    def test_concurrent_duplicate_only_executes_once(self):
        from concurrent.futures import ThreadPoolExecutor
        competition = {"competitionId": self.arena.config.competition_id}
        for c in self.clients:
            c.request("join", competition)
        agent = actor(self.arena)
        index = self.ids.index(agent)
        body = action_body(self.arena, agent, {"type": "call"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.raw, "/api/competitions/action", json.dumps(body).encode(), f"sk_local_test_{index}") for _ in range(2)]
            self.assertEqual([f.result()[0] for f in futures], [200, 200])
        self.assertEqual(self.arena.members[agent]["actions"], 1)

    def test_malformed_url_does_not_stop_match(self):
        from http.client import HTTPConnection
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            connection.request("GET", "http://[", headers={"Host": "127.0.0.1"})
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
        finally:
            connection.close()
        self.assertEqual(self.raw("/api/competitions")[0], 200)
        self.assertEqual(self.arena.status, "active")


if __name__ == "__main__":
    unittest.main()
