from copy import deepcopy
import random
import tempfile
from pathlib import Path
import json
import unittest

from competition_client.adapter import ObservationAdapter
from competition_client.protocol import validate_discovery, validate_observation
from local_arena.arena import Arena, ArenaError
from local_arena.reporting import Reporter
from tests.arena_helpers import FakeClock, configuration, join_all, action_body, actor


class ArenaLifecycleTests(unittest.TestCase):
    def test_all_roster_required_join_is_idempotent(self):
        a = Arena(configuration(3))
        agents = list(a.members)
        body = {"competitionId": a.config.competition_id}
        for _ in range(2):
            self.assertEqual(a.handle("join", agents[0], body)["status"], "queued")
        self.assertEqual(a.members[agents[0]]["bankroll"], a.config.initial_bankroll)
        join_all(a)
        self.assertTrue(a.started)
        self.assertEqual(a.members[agents[0]]["bankroll"], a.config.initial_bankroll - a.config.buy_in)
        validate_discovery(a.discovery(), a.config.competition_id)

    def test_invalid_action_retry_and_idempotency(self):
        a = Arena(configuration(hands=2))
        join_all(a)
        agent = actor(a)
        wrong = action_body(a, agent, {"type": "check"})
        old_pot = a.engine.pot
        with self.assertRaisesRegex(ArenaError, "invalid_decision"):
            a.handle("action", agent, wrong)
        self.assertEqual(a.engine.pot, old_pot)
        correct = dict(wrong, decision={"type": "call"})
        a.handle("action", agent, correct)
        accepted_pot = a.engine.pot
        a.handle("action", agent, correct)
        self.assertEqual(a.engine.pot, accepted_pot)
        with self.assertRaisesRegex(ArenaError, "idempotency_conflict"):
            a.handle("action", agent, dict(correct, decision={"type": "fold"}))
        self.assertEqual(a.members[agent]["actions"], 1)

    def test_privacy_and_incremental_amounts(self):
        a = Arena(configuration(3))
        join_all(a)
        adapters = {agent: ObservationAdapter() for agent in a.members}
        for _ in range(3):
            agent = actor(a)
            obs = a.observation(agent)
            state = adapters[agent].convert(obs)
            for p in obs["table"]["players"]:
                self.assertEqual(p["handState"]["holeCards"] is not None, p["agentId"] == agent)
            raises = [x for x in obs["actionRequest"]["allowedActions"] if x["type"] == "raise"]
            decision = {"type": "raise", "amount": raises[0]["minAmount"]} if raises else {"type": "call"}
            before = a.engine.players[a.state["seat_to_act"]].stack
            a.handle("action", agent, action_body(a, agent, decision))
            self.assertEqual(a.history[-1]["amount"], decision["amount"])
            self.assertEqual(before - a.engine.players[next(p.seat for p in a.engine.players if p.bot_id == agent)].stack, decision["amount"])
            self.assertGreater(state["pot"], 0)

    def test_adapter_handles_short_big_blind(self):
        a = Arena(configuration(3, hands=3))
        join_all(a)
        ids = list(a.members)
        a.members[ids[2]]["stack"] = 50
        a.engine = None
        a.last_dealer = -1
        a._start_hand()
        agent = actor(a)
        state = ObservationAdapter().convert(a.observation(agent))
        self.assertEqual(state["current_bet"], a.config.big_blind)
        self.assertEqual(state["call_amount"], a.config.big_blind)

    def test_background_timeout_uses_monotonic_and_old_token_is_stale(self):
        clock = FakeClock()
        a = Arena(configuration(hands=3), clock=clock)
        join_all(a)
        agent = actor(a)
        body = action_body(a, agent, {"type": "call"})
        clock.elapsed += 31
        a.tick()
        self.assertEqual(a.history[-1]["cause"], "timeout")
        self.assertEqual(a.members[agent]["timeouts"], 1)
        with self.assertRaises(ArenaError) as cm:
            a.handle("action", agent, body)
        self.assertIn(cm.exception.code, {"stale_action_request", "stale_table"})

    def test_out_of_turn_leave_preserves_actor_and_token(self):
        a = Arena(configuration(4))
        join_all(a)
        current = actor(a)
        request = deepcopy(a.request)
        other = next(x for x in a.members if x != current)
        body = {"tableId": a.table_id}
        self.assertEqual(a.handle("leave", other, body)["status"], "accepted")
        self.assertEqual(actor(a), current)
        self.assertEqual(a.request, request)
        self.assertEqual(a.history[-1]["cause"], "leave")
        ObservationAdapter().convert(a.observation(current))

    def test_leave_cashout_and_early_end(self):
        a = Arena(configuration(hands=10))
        join_all(a)
        leaving = actor(a)
        a.handle("leave", leaving, {"competitionId": a.config.competition_id})
        a.tick()
        self.assertEqual(a.outcome, "incomplete")
        self.assertEqual(a.reason, "insufficient_players")
        for agent, m in a.members.items():
            self.assertEqual(m["bankroll"], a.config.initial_bankroll + m["net"])
            self.assertEqual(a.observation(agent)["status"], "idle")
        self.assertEqual(a.handle("leave", leaving, {"tableId": a.table_id})["status"], "accepted")

    def test_rebuy_only_at_boundary(self):
        a = Arena(configuration(buy_in=200, initial_bankroll=1000, small_blind=50, big_blind=100, hands=3))
        join_all(a)
        while a.request:
            agent = actor(a)
            a.handle("action", agent, action_body(a, agent, {"type": "allIn"}))
        busted = [agent for agent, m in a.members.items() if m["stack"] == 0]
        self.assertTrue(busted)
        for agent in busted:
            self.assertEqual(a.members[agent]["rebuys"], 0)
        a.tick()
        for agent in busted:
            self.assertEqual(a.members[agent]["rebuys"], 1)
            self.assertEqual(a.members[agent]["bankroll"], 600)

    def test_finite_random_matches_2_to_6_and_reports(self):
        for n in range(2, 7):
            with self.subTest(players=n), tempfile.TemporaryDirectory() as directory:
                reporter = Reporter(directory)
                try:
                    a = Arena(configuration(n, hands=20, minimum_hands=20, initial_bankroll=400000), reporter)
                    join_all(a)
                    rng = random.Random(42)
                    steps = 0
                    while a.status == "active" or any(m["seated"] for m in a.members.values()):
                        if not a.request:
                            a.tick()
                            continue
                        agent = actor(a)
                        obs = a.observation(agent)
                        validate_observation(obs, a.config.competition_id, agent)
                        ObservationAdapter().convert(obs)
                        allowed = obs["actionRequest"]["allowedActions"]
                        choice = rng.choice(allowed)
                        decision = {"type": choice["type"]}
                        if choice["type"] in {"bet", "raise"}:
                            decision["amount"] = choice["minAmount"]
                        a.handle("action", agent, action_body(a, agent, decision))
                        steps += 1
                        self.assertLess(steps, 10000)
                    self.assertEqual(a.outcome, "completed")
                    self.assertEqual(a.completed_hands, 20)
                    self.assertEqual(sum(m["net"] for m in a.members.values()), 0)
                    for agent, m in a.members.items():
                        self.assertEqual(m["bankroll"], a.config.initial_bankroll + m["net"])
                        self.assertEqual(a.observation(agent)["standing"]["handsPlayed"], 20)
                    records = [json.loads(line) for line in (Path(directory) / "hands.jsonl").read_text().splitlines()]
                    self.assertEqual(len(records), 20)
                    for record in records:
                        self.assertEqual(sum(record["starting_stacks"].values()), sum(record["final_stacks"].values()))
                    self.assertEqual(json.loads((Path(directory) / "report.json").read_text())["outcome"], "completed")
                finally:
                    reporter.close()

    def test_stop_and_schedule_end_are_distinct(self):
        clock = FakeClock()
        a = Arena(configuration(duration_seconds=1), clock=clock)
        clock.elapsed = 2
        a.tick()
        self.assertEqual(a.outcome, "incomplete")
        self.assertEqual(a.reason, "time_limit")
        b = Arena(configuration())
        join_all(b)
        b.stop()
        self.assertEqual(b.outcome, "interrupted")
        self.assertIsNotNone(b.report()["inProgressHand"])

    def test_timeout_retirement_does_not_wait_again_on_later_street(self):
        clock = FakeClock()
        a = Arena(configuration(3, max_consecutive_timeouts=1), clock=clock)
        join_all(a)
        # Call around to the big blind, who can check when its deadline expires.
        while a.engine.players[a.state["seat_to_act"]].bet_this_street < a.engine.current_bet:
            agent = actor(a)
            a.handle("action", agent, action_body(a, agent, {"type": "call"}))
        retired = actor(a)
        clock.elapsed = 31
        a.tick()
        self.assertEqual(a.history[-1]["type"], "check")
        found = False
        for _ in range(10):
            a.tick()
            found = any(x.get("cause") == "leave" and x["agentId"] == retired for x in a.history)
            if found:
                break
            if not a.request:
                break
            agent = actor(a)
            if agent != retired:
                obs = a.observation(agent)
                types = {x["type"] for x in obs["actionRequest"]["allowedActions"]}
                a.handle("action", agent, action_body(a, agent, {"type": "check" if "check" in types else "call"}))
        self.assertTrue(found)

    def test_persistent_metadata_snapshot_and_no_repeated_writes(self):
        from competition_client.decision_store import DecisionStore
        with tempfile.TemporaryDirectory() as directory:
            store = DecisionStore(directory)
            snapshot = {"agent_id": "a", "table_id": "t", "standing": {"handsPlayed": 2}, "bankroll": 1000}
            store.meta_many(snapshot)
            changes = store.db.total_changes
            store.meta_many(snapshot)
            self.assertEqual(store.db.total_changes, changes)
            store.close()
            restored = DecisionStore(directory)
            try:
                for k, v in snapshot.items():
                    self.assertEqual(restored.meta(k), v)
            finally:
                restored.close()


if __name__ == "__main__":
    unittest.main()
