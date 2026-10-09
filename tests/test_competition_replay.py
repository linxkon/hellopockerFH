from copy import deepcopy
import json
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest

from competition_client.config import Config
from competition_client.decision_store import DecisionStore
from competition_client.driver import Driver
from competition_client.protocol import ProtocolError
from competition_client.replay import export_replay, load_replay
from competition_client.replay_store import ReplayJournal, source_for
from competition_client.public_stream import PublicTableStream, table_snapshots
from tests.sohu_helpers import Clock, COMPETITION_ID, DISCOVERY, ScriptedHTTP, Worker, observation


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Config(COMPETITION_ID, "never-read-this-key", state_dir=self.temp.name)

    def journal(self):
        journal = ReplayJournal(self.config)
        self.addCleanup(journal.close)
        return journal

    def settled(self):
        obs = observation(request=False)
        obs["table"]["hand"].update(phase="complete", pot=300, result={
            "payouts": [{"agentId": "agent-two", "amount": 200,
                         "potAwards": [{"potKind": "main", "potIndex": 0, "amount": 200}]}],
            "returnedBets": [{"agentId": "agent-two", "amount": 100}]})
        obs["table"]["hand"]["actions"].append({"street": "preflop", "agentId": "agent-one", "type": "fold", "chat": "discard chat"})
        obs["table"]["players"][0]["handState"]["status"] = "folded"
        obs["table"]["players"][1]["stack"] = 20100
        return obs

    def test_revealed_cards_results_sidepots_and_no_chat_or_credentials(self):
        journal = self.journal()
        obs = observation()
        obs["table"]["hand"]["actions"][0]["chat"] = "do not record"
        obs["table"]["players"][1]["name"] = "sk_private_secret"
        obs["table"]["Authorization"] = "Bearer hidden"
        journal.observation(obs)
        final = self.settled()
        final["table"]["players"][1]["handState"]["holeCards"] = ["Kh", "Kd"]
        final["table"]["hand"]["sidePots"] = [{"amount": 100, "eligibleAgentIds": ["agent-two"]}]
        journal.observation(final)
        report = load_replay(journal.path, include_snapshots=True)
        hand = report["hands"][0]
        self.assertEqual(hand["net_result"], {"agent-one": -100, "agent-two": 100})
        self.assertEqual(hand["hand"]["sidePots"], final["table"]["hand"]["sidePots"])
        self.assertEqual(hand["players"]["agent-two"]["handState"]["holeCards"], ["Kh", "Kd"])
        serialized = json.dumps(report)
        for secret in ("discard chat", "do not record", "sk_private_secret", "Bearer hidden", "never-read-this-key"):
            self.assertNotIn(secret, serialized)
        self.assertNotIn('"chat":', serialized)
        self.assertEqual(journal.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(journal.directory.stat().st_mode & 0o777, 0o700)

    def test_nonacting_and_settlement_observations_are_recorded_by_driver(self):
        journal = self.journal()
        with DecisionStoreContext(self.temp.name) as store:
            final = self.settled()
            final["table"]["hand"]["actions"].append({"street": "preflop", "agentId": "agent-two", "type": "check"})
            script = [("discovery", DISCOVERY), ("join", {"competitionId": COMPETITION_ID, "status": "queued"}),
                      ("observe", observation(request=False)), ("observe", final),
                      ("observe", observation("idle", "ended"))]
            worker = Worker()
            Driver(self.config, ScriptedHTTP(script), store, worker, Clock(), journal=journal).run()
            self.assertEqual(worker.calls, 0)
        report = load_replay(journal.path)
        self.assertEqual(report["summary"]["hands_with_settlement"], 1)
        self.assertEqual(report["summary"]["player_statistics"]["agent-two"]["actions"]["check"], 1)

    def test_restart_and_duplicate_observations_do_not_double_count_hands_actions(self):
        first = self.journal()
        first.observation(observation())
        first.observation(observation())
        second = self.journal()
        second.observation(observation())
        second.observation(self.settled())
        report = load_replay(first.path)
        self.assertEqual(len(report["sessions"]), 2)
        self.assertEqual(report["summary"]["observation_count"], 3)
        self.assertEqual(report["summary"]["hands_observed"], 1)
        self.assertEqual(report["summary"]["player_statistics"]["agent-one"]["actions"]["smallBlind"], 1)
        self.assertEqual(len(report["hands"][0]["run_ids"]), 2)

    def test_partial_hand_never_synthesizes_zero_profit_or_negative_vpip_sample(self):
        journal = self.journal()
        journal.observation(observation())
        report = load_replay(journal.path)
        self.assertIsNone(report["hands"][0]["net_result"])
        self.assertEqual(report["summary"]["hands_without_settlement"], 1)
        for st in report["summary"]["player_statistics"].values():
            self.assertIsNone(st["vpip"])
            self.assertIsNone(st["bb_per_100_on_observed_settled_hands"])
        self.assertEqual(report["summary"]["latest_bankroll_off_table"], 20600)
        self.assertEqual(report["summary"]["latest_hero_stack_on_table"], 19900)

    def test_multiplayer_short_allin_and_forced_fold_statistics(self):
        journal = self.journal()
        obs = observation(request=False)
        obs["table"]["players"].append({"agentId": "agent-three", "seatIndex": 2, "stack": 0,
            "handState": {"status": "allIn", "currentBet": 150, "holeCards": ["Qc", "Qd"]}})
        hand = obs["table"]["hand"]
        hand["actions"] += [
            {"street": "preflop", "agentId": "agent-three", "type": "allIn", "amount": 150},
            {"street": "preflop", "agentId": "agent-one", "type": "fold", "cause": "timeout"}]
        hand.update(phase="complete", pot=450, sidePots=[{"amount": 100}], result={
            "payouts": [{"agentId": "agent-two", "amount": 100, "potAwards": [{"potKind": "side", "potIndex": 1, "amount": 100}]},
                        {"agentId": "agent-three", "amount": 350, "potAwards": [{"potKind": "main", "potIndex": 0, "amount": 350}]}],
            "returnedBets": []})
        journal.observation(obs)
        report = load_replay(journal.path)
        stats = report["summary"]["player_statistics"]
        self.assertEqual(stats["agent-three"]["vpip"], 1)
        self.assertEqual(stats["agent-three"]["pfr"], 0)
        self.assertEqual(stats["agent-three"]["call_vs_bet"], 1)
        self.assertEqual(stats["agent-one"]["timeout_actions"], 1)
        self.assertEqual(stats["agent-one"]["faced_bet"], 0)
        self.assertEqual(sum(report["hands"][0]["net_result"].values()), 0)

    def test_pending_action_is_reused_with_journal_and_full_decision_context(self):
        journal = self.journal()
        with DecisionStoreContext(self.temp.name) as store:
            obs = observation()
            body = {"competitionId": COMPETITION_ID, "tableId": obs["table"]["id"],
                    "actionRequestId": obs["actionRequest"]["id"], "decision": {"type": "call"}}
            store.save(body)
            store.meta("engaged", True)
            worker = Worker()
            http = ScriptedHTTP([("discovery", DISCOVERY), ("observe", obs), ("action", observation("idle", "ended"))])
            Driver(self.config, http, store, worker, Clock(), journal=journal).run()
            self.assertEqual(worker.calls, 0)
            self.assertEqual(http.requests[-1][1], body)
        report = load_replay(journal.path)
        decision = report["decisions"][0]
        self.assertTrue(decision["reused"])
        self.assertEqual(decision["state"]["your_cards"], ["As", "Td"])
        self.assertEqual(decision["outcome"]["status"], "done")

    def test_journal_disk_failure_does_not_prevent_submission(self):
        journal = self.journal()
        journal.db.execute("PRAGMA query_only=ON")
        with DecisionStoreContext(self.temp.name) as store:
            script = [("discovery", DISCOVERY), ("join", {"competitionId": COMPETITION_ID, "status": "queued"}),
                      ("observe", observation()), ("action", observation("idle", "ended"))]
            http = ScriptedHTTP(script)
            with self.assertLogs("competition_client.replay_store", level="ERROR"):
                Driver(self.config, http, store, Worker(), Clock(), journal=journal).run()
            self.assertTrue(journal.failed)
            self.assertEqual(http.requests[-1][0], "action")

    def test_sources_separate_local_arena_even_when_served_over_https(self):
        self.assertEqual(source_for(self.config), "sohu_online")
        self.config.base_url = "http://127.0.0.1:8765"
        self.assertEqual(source_for(self.config), "local_arena")
        self.config.base_url = "https://arena.example.com"
        self.assertEqual(source_for(self.config), "competition_custom")
        self.config.log_source = "local_arena"
        self.assertEqual(source_for(self.config), "local_arena")
        self.config.log_source = "sohu_online"
        with self.assertRaises(ProtocolError):
            source_for(self.config)

    def test_export_is_read_only_private_and_filters_by_hand(self):
        journal = self.journal()
        obs = observation()
        journal.observation(obs)
        other = deepcopy(obs)
        other["table"]["hand"]["id"] = "second-hand"
        journal.observation(other)
        report = load_replay(journal.path, hand_id=obs["table"]["hand"]["id"], include_snapshots=True)
        self.assertEqual(len(report["hands"]), 1)
        directory = Path(self.temp.name) / "export"
        export_replay(report, directory)
        self.assertEqual(len(list(directory.iterdir())), 5)
        self.assertEqual((directory / "hands.jsonl").stat().st_mode & 0o777, 0o600)
        self.assertNotIn('"chat":', (directory / "hands.jsonl").read_text())
        with self.assertRaises(ProtocolError):
            export_replay(report, directory)
        self.assertEqual(journal.db.execute("SELECT count(*) FROM observations").fetchone()[0], 2)

    def test_raw_invalid_values_are_safe_to_record(self):
        journal = self.journal()
        journal.event("invalid_raw", {"amount": float("nan"), "unserializable": object(), "chat": "ignore"})
        result = json.loads(journal.db.execute("SELECT body FROM events").fetchone()[0])
        self.assertIsNone(result["amount"])
        self.assertNotIn("chat", result)

    def test_cyclic_strategy_output_cannot_stop_submission_recording_path(self):
        journal = self.journal()
        cyclic = {}
        cyclic["self"] = cyclic
        with self.assertLogs("competition_client.replay_store", level="ERROR"):
            journal.event("raw_output", cyclic)
        self.assertTrue(journal.failed)
        journal.observation(observation())

    def test_each_correction_has_its_own_outcome_and_table_request_identity(self):
        journal = self.journal()
        first = {"competition_id": COMPETITION_ID, "table_id": "table-one", "hand_id": "hand-one", "request_id": "opaque"}
        second = dict(first, table_id="table-two", hand_id="hand-two")
        journal.event("decision", first)
        journal.event("action_outcome", dict(first, status="rejected"))
        journal.event("decision", first)
        journal.event("action_outcome", dict(first, status="done"))
        journal.event("decision", second)
        journal.event("request_error", dict(second, code="network_result_unknown"))
        journal.event("action_outcome", dict(second, status="expired"))
        report = load_replay(journal.path, hand_id="hand-one")
        self.assertEqual([d["outcome"]["status"] for d in report["decisions"]], ["rejected", "done"])
        self.assertEqual(report["summary"]["unique_decision_requests"], 1)
        self.assertFalse(any(e["kind"] == "request_error" for e in report["events"]))

    def test_public_stream_settlement_merges_without_overwriting_private_cards_or_bankroll(self):
        journal = self.journal()
        obs = observation()
        journal.observation(obs)
        final = self.settled()
        final["table"]["players"][0]["handState"]["holeCards"] = None
        final["table"]["players"][1]["handState"]["holeCards"] = ["Kh", "Kd"]
        payload = {"table": final["table"], "messages": [{"text": "do not record this conversation"}], "turnTimer": None}
        wire = (": heartbeat\n\nevent: snapshot\ndata: " + json.dumps(payload) + "\n\n").encode()
        tables = list(table_snapshots(io.BytesIO(wire)))
        self.assertEqual(len(tables), 1)
        journal.event("public_table_snapshot", {"competition_id": COMPETITION_ID,
            "table_id": final["table"]["id"], "hand_id": final["table"]["hand"]["id"],
            "hero_agent_id": obs["agentId"], "table": tables[0]})
        next_obs = observation()
        next_obs["table"]["hand"]["id"] = "next-hand"
        journal.observation(next_obs)
        report = load_replay(journal.path, include_snapshots=True)
        hand = report["hands"][0]
        self.assertTrue(hand["settled"])
        self.assertEqual(hand["players"]["agent-one"]["handState"]["holeCards"], ["As", "Td"])
        self.assertEqual(hand["players"]["agent-two"]["handState"]["holeCards"], ["Kh", "Kd"])
        self.assertEqual(hand["net_result"], {"agent-one": -100, "agent-two": 100})
        self.assertEqual(hand["public_snapshot_count"], 1)
        self.assertEqual(report["summary"]["observation_count"], 2)
        self.assertEqual(report["summary"]["latest_bankroll_off_table"], 20600)
        self.assertNotIn("do not record this conversation", json.dumps(report))
        self.assertEqual(hand["snapshots"][-1]["visibility"], "public_table_stream")

    def test_public_stream_parser_rejects_invalid_json_and_ignores_other_events(self):
        self.assertEqual(list(table_snapshots(io.BytesIO(b'event: chat\ndata: {"text":"ignored"}\n\n'))), [])
        with self.assertRaises(ValueError):
            list(table_snapshots(io.BytesIO(b'event: snapshot\ndata: invalid\n\n')))

    def test_public_stream_records_from_separate_thread_without_credentials(self):
        journal = self.journal()
        final = self.settled()
        wire = ('event: snapshot\ndata: ' + json.dumps({'table': final['table'], 'messages': [{'text': 'ignore this chat'}]}) + '\n\n').encode()
        headers = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                headers.append(dict(self.headers))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(wire)))
                self.end_headers()
                self.wfile.write(wire)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        collector = PublicTableStream(journal, f'http://127.0.0.1:{server.server_port}', COMPETITION_ID)
        try:
            collector.follow(final['table']['id'], final['agentId'])
            end = time.monotonic() + 3
            rows = []
            while time.monotonic() < end:
                rows = journal.db.execute("SELECT body FROM events WHERE kind='public_table_snapshot'").fetchall()
                if rows:
                    break
                time.sleep(0.01)
            self.assertEqual(len(rows), 1)
            self.assertTrue(headers)
            self.assertFalse(any('Authorization' in h for h in headers))
            self.assertNotIn('ignore this chat', rows[0][0])
            self.assertNotIn('discard chat', rows[0][0])
            self.assertEqual(json.loads(rows[0][0])['table']['hand']['result'], final['table']['hand']['result'])
        finally:
            collector.close()
            collector.thread.join(timeout=2)
            server.shutdown()
            server.server_close()
            serving.join(timeout=2)


class DecisionStoreContext:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.store = DecisionStore(self.path)
        return self.store

    def __exit__(self, *_):
        self.store.close()
