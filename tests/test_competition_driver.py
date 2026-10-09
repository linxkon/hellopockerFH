from copy import deepcopy
import tempfile
import unittest

from competition_client.config import Config
from competition_client.decision_store import DecisionStore
from competition_client.driver import Driver
from competition_client.http_client import APIError, NetworkError
from competition_client.protocol import ProtocolError
from tests.sohu_helpers import (Clock, COMPETITION_ID, DISCOVERY, ScriptedHTTP,
                               TABLE_ID, Worker, observation)


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DecisionStore(self.temp.name)
        self.addCleanup(self.store.close)
        self.cfg = Config(COMPETITION_ID, "unused", state_dir=self.temp.name)
        self.clock, self.worker = Clock(), Worker()

    def driver(self, script):
        self.http = ScriptedHTTP(script)
        return Driver(self.cfg, self.http, self.store, self.worker, self.clock)

    def finish(self):
        return observation("idle", "ended")

    def initial(self, obs=None):
        return [("discovery", DISCOVERY), ("join", {"competitionId": COMPETITION_ID, "status": "queued"}),
                ("observe", obs or observation())]

    def test_lost_response_and_503_identical_body_one_strategy_call(self):
        driver = self.driver(self.initial()+[("action", NetworkError()), ("action", APIError("temporarily_unavailable", 3)), ("action", self.finish())])
        driver.run()
        bodies = [r[1] for r in self.http.requests if r[0] == "action"]
        self.assertEqual(len(bodies), 3)
        self.assertTrue(all(b == bodies[0] for b in bodies))
        self.assertEqual(self.worker.calls, 1)
        self.assertEqual(bodies[0]["decision"], {"type": "raise", "amount": 400})
        self.assertEqual(self.clock.elapsed, 4)

    def test_restart_pending_action_reuses_original(self):
        body = {"competitionId": COMPETITION_ID, "tableId": TABLE_ID, "actionRequestId": "action:opaque-value", "decision": {"type": "call"}}
        self.store.save(body)
        self.store.meta("engaged", True)
        driver = self.driver([("discovery", DISCOVERY), ("observe", observation()), ("action", self.finish())])
        driver.run()
        self.assertEqual(self.worker.calls, 0)
        self.assertEqual(self.http.requests[2][1], body)

    def test_restart_after_commit_archives_old_action(self):
        self.store.save({"competitionId": COMPETITION_ID, "tableId": TABLE_ID, "actionRequestId": "old", "decision": {"type": "fold"}})
        self.store.meta("engaged", True)
        driver = self.driver([("discovery", DISCOVERY), ("observe", self.finish())])
        driver.run()
        self.assertEqual(self.store.get((COMPETITION_ID, TABLE_ID, "old"))[1], "archived")
        self.assertEqual(self.worker.calls, 0)

    def test_action_response_directly_drives_next_action(self):
        next_obs = observation()
        next_obs["actionRequest"]["id"] = "next-opaque"
        driver = self.driver(self.initial()+[("action", next_obs), ("action", self.finish())])
        driver.run()
        self.assertEqual([r[0] for r in self.http.requests], ["discovery", "join", "observe", "action", "action"])
        self.assertEqual(self.worker.calls, 2)

    def test_top_level_request_only_and_ended_drain(self):
        obs = observation(lifecycle="ended", request=False)
        driver = self.driver(self.initial(obs)+[("observe", self.finish())])
        driver.run()
        self.assertEqual(self.worker.calls, 0)
        self.assertEqual(self.http.requests[-1][1]["tableId"], TABLE_ID)
        self.assertEqual(self.clock.elapsed, 2)

    def test_idle_join_refresh_queued_heartbeat_and_direct_table(self):
        script = [("discovery", DISCOVERY), ("join", {"competitionId": COMPETITION_ID, "status": "idle"}),
                  ("discovery", DISCOVERY), ("join", {"competitionId": COMPETITION_ID, "status": "queued"}),
                  ("observe", observation("queued")), ("observe", observation(request=False)), ("observe", self.finish())]
        self.driver(script).run()
        observe_bodies = [r[1] for r in self.http.requests if r[0] == "observe"]
        self.assertEqual(observe_bodies[:2], [{"competitionId": COMPETITION_ID}]*2)
        self.assertEqual(observe_bodies[-1], {"competitionId": COMPETITION_ID, "tableId": TABLE_ID})
        self.assertEqual(self.clock.elapsed, 37)

    def test_stale_table_action_and_observe_routing(self):
        for phase in ("action", "observe"):
            with self.subTest(phase=phase):
                self.store.meta("engaged", False)
                obs = observation(request=phase == "action")
                if phase == "action":
                    obs["actionRequest"]["id"] = "stale-action-test"
                driver = self.driver(self.initial(obs)+[(phase, APIError("stale_table")), ("observe", self.finish())])
                driver.run()
                self.assertEqual(self.http.requests[-1][1], {"competitionId": COMPETITION_ID})

    def test_expired_request_no_strategy_no_submission(self):
        obs = observation()
        obs["actionRequest"]["deadlineAt"] = "2026-07-31T23:59:59Z"
        driver = self.driver(self.initial(obs)+[("observe", self.finish())])
        driver.run()
        self.assertEqual(self.worker.calls, 0)
        self.assertFalse(any(r[0] == "action" for r in self.http.requests))

    def test_near_deadline_fallback_and_timeout_fallback(self):
        for near, failure in ((True, False), (False, True)):
            with self.subTest(near=near):
                self.store.meta("engaged", False)
                obs = observation()
                obs["actionRequest"]["id"] = str(near)
                if near:
                    obs["actionRequest"]["deadlineAt"] = "2026-08-01T00:00:01Z"
                if failure:
                    self.worker.action = TimeoutError()
                self.driver(self.initial(obs)+[("action", self.finish())]).run()
                self.assertEqual(self.http.requests[-1][1]["decision"], {"type": "fold"})

    def test_retry_deadline_no_changed_body(self):
        obs = observation()
        obs["actionRequest"]["deadlineAt"] = "2026-08-01T00:00:03Z"
        driver = self.driver(self.initial(obs)+[("action", APIError("temporarily_unavailable", 4)), ("observe", self.finish())])
        driver.run()
        self.assertEqual(len([r for r in self.http.requests if r[0] == "action"]), 1)
        self.assertEqual(self.worker.calls, 1)

    def test_explicit_invalid_decision_reconfirmed_before_safe_correction(self):
        driver = self.driver(self.initial()+[("action", APIError("invalid_decision")), ("observe", observation()), ("action", self.finish())])
        driver.run()
        actions = [r[1] for r in self.http.requests if r[0] == "action"]
        self.assertEqual([b["decision"] for b in actions], [{"type": "raise", "amount": 400}, {"type": "fold"}])
        self.assertEqual(self.worker.calls, 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0], 1)

    def test_leave_timeout_stale_switch_and_no_further_commands(self):
        driver = self.driver([("leave", NetworkError()), ("leave", APIError("stale_table")),
                              ("leave", {"competitionId": COMPETITION_ID, "status": "accepted"})])
        driver.table_id = TABLE_ID
        driver.request_stop()
        driver.run()
        bodies = [r[1] for r in self.http.requests]
        self.assertEqual(bodies[:2], [{"tableId": TABLE_ID}]*2)
        self.assertEqual(bodies[2], {"competitionId": COMPETITION_ID})
        self.assertTrue(all(r[0] == "leave" for r in self.http.requests))

    def test_pending_leave_restart_before_discovery_join_or_strategy(self):
        self.store.meta("leave", {"body": {"tableId": TABLE_ID}, "status": "pending"})
        self.driver([("leave", {"tableId": TABLE_ID, "status": "accepted"})]).run()
        self.assertEqual([r[0] for r in self.http.requests], ["leave"])

    def test_stop_during_action_retry_uses_leave(self):
        driver = self.driver(self.initial()+[("action", None), ("leave", {"tableId": TABLE_ID, "status": "accepted"})])
        def stopped(body):
            driver.request_stop()
            raise NetworkError()
        self.http.script[3] = ("action", stopped)
        driver.run()
        self.assertEqual([r[0] for r in self.http.requests][-2:], ["action", "leave"])

    def test_fail_closed_errors(self):
        for code in ("authentication_required", "registration_required", "insufficient_bankroll", "internal_error", "invalid_request"):
            self.store.meta("engaged", False)
            driver = self.driver([("discovery", DISCOVERY), ("join", APIError(code))])
            with self.subTest(code=code), self.assertRaises(APIError):
                driver.run()
            self.assertEqual(len(self.http.requests), 2)

    def test_immutable_store_and_missing_competition(self):
        body = {"competitionId": COMPETITION_ID, "tableId": TABLE_ID, "actionRequestId": "test", "decision": {"type": "fold"}}
        self.store.save(body)
        with self.assertRaises(ProtocolError):
            self.store.save(dict(body, decision={"type": "call"}))
        with self.assertRaises(ProtocolError):
            self.driver([("discovery", {"competitions": []})]).run()
        self.assertEqual([r[0] for r in self.http.requests], ["discovery"])

    def test_scheduled_waits_for_exact_competition(self):
        scheduled = deepcopy(DISCOVERY)
        scheduled["competitions"][0]["status"] = "scheduled"
        script = [("discovery", scheduled)]+self.initial()+[("action", self.finish())]
        self.driver(script).run()
        self.assertEqual(self.clock.elapsed, 30)

    def test_live_initial_null_standing_is_preserved(self):
        obs = observation()
        obs["standing"] = None
        driver = self.driver(self.initial(obs)+[("action", self.finish())])
        driver.run()
        self.assertEqual(self.worker.calls, 1)
        malformed = observation()
        malformed.pop("standing")
        with self.assertRaises(ProtocolError):
            driver._accept(malformed)
