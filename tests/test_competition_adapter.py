from copy import deepcopy
import unittest

from competition_client.adapter import ObservationAdapter, convert_decision, safe_decision
from competition_client.protocol import ProtocolError, allowed_actions, validate_body, validate_decision
from tests.sohu_helpers import observation


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.obs = observation()
        self.state = ObservationAdapter().convert(self.obs)
        self.allowed = self.obs["actionRequest"]["allowedActions"]

    def test_official_example(self):
        self.assertEqual(self.state["your_cards"], ["As", "Td"])
        self.assertEqual(self.state["your_stack"], 19900)
        self.assertEqual(self.state["amount_owed"], 100)
        self.assertEqual(self.state["min_raise_to"], 400)
        self.assertEqual(convert_decision({"action": "raise", "amount": 500}, self.state, self.allowed), {"type": "raise", "amount": 400})

    def test_sparse_seats_identity_and_nonparticipant(self):
        table = self.obs["table"]
        table["players"][0]["seatIndex"] = 8
        table["players"][1]["seatIndex"] = 2
        table["hand"]["dealerSeatIndex"] = 8
        table["players"].append({"agentId": "observer", "seatIndex": 5, "stack": 90000, "handState": None})
        table["players"].reverse()
        state = ObservationAdapter().convert(self.obs)
        self.assertEqual(state["seat_to_act"], 0)
        self.assertEqual([p["bot_id"] for p in state["players"]], ["agent-one", "agent-two"])

    def test_raise_boundaries_and_nonintegers(self):
        for target, expected in [(1, 300), (400, 300), (500, 400), (999999, 19900)]:
            with self.subTest(target=target):
                result = convert_decision({"action": "raise", "amount": target}, self.state, self.allowed)
                self.assertEqual(result["amount"], expected)
                validate_decision(result, self.allowed)
        for target in (True, 500.5, "500", None, -1):
            self.assertEqual(convert_decision({"action": "raise", "amount": target}, self.state, self.allowed), {"type": "fold"})

    def test_no_expensive_fallback(self):
        allowed = [{"type": "fold"}, {"type": "call", "amount": 100}]
        for kind in ("all_in", "raise", "check", "bogus"):
            self.assertEqual(convert_decision({"action": kind, "amount": 500}, self.state, allowed), {"type": "fold"})
        self.assertEqual(safe_decision([{"type": "fold"}, {"type": "check"}]), {"type": "check"})
        with self.assertRaises(ProtocolError):
            safe_decision([{"type": "call", "amount": 100}])

    def test_call_allin_omit_amount(self):
        for old, new in (("call", "call"), ("all_in", "allIn")):
            self.assertEqual(convert_decision({"action": old, "amount": 99}, self.state, self.allowed), {"type": new})

    def test_cross_street_bet_history(self):
        hand = self.obs["table"]["hand"]
        hand["actions"] += [{"street": "preflop", "agentId": "agent-one", "type": "call", "amount": 100},
                            {"street": "preflop", "agentId": "agent-two", "type": "check", "amount": 0},
                            {"street": "flop", "agentId": "agent-two", "type": "check", "amount": 0}]
        hand.update(phase="flop", communityCards=["2c", "7d", "9s"], pot=400)
        for p in self.obs["table"]["players"]:
            p["handState"]["currentBet"] = 0
            p["stack"] = 19800
        self.obs["actionRequest"]["allowedActions"] = [{"type": "check"}, {"type": "fold"}, {"type": "bet", "minAmount": 200, "maxAmount": 19800}]
        state = ObservationAdapter().convert(self.obs)
        self.assertEqual(convert_decision({"action": "raise", "amount": 600}, state, self.obs["actionRequest"]["allowedActions"]), {"type": "bet", "amount": 600})
        self.assertEqual(state["action_log"][-1]["street"], "flop")
        import importlib.util
        spec = importlib.util.spec_from_file_location("adapted", "hero_bots/bot_sohu_v16/bot.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        replay = mod._reconstruct_hand(state["action_log"])
        self.assertEqual(replay[-1]["street_idx"], 1)
        self.assertEqual(replay[-1]["pot_before"], 400)

    def test_short_call_uses_full_owed(self):
        self.obs["table"]["players"][0]["stack"] = 50
        self.obs["actionRequest"]["allowedActions"] = [{"type": "fold"}, {"type": "call", "amount": 50}, {"type": "allIn", "amount": 50}]
        state = ObservationAdapter().convert(self.obs)
        self.assertEqual((state["amount_owed"], state["call_amount"]), (100, 50))
        self.assertEqual(state["min_raise_to"], 0)

    def test_three_way_short_allin_and_timeout_fold_ledger(self):
        players = self.obs["table"]["players"]
        players[0]["handState"]["currentBet"] = 100
        players[1]["handState"].update(status="allIn", currentBet=250)
        players[1]["stack"] = 0
        players.append({"agentId": "agent-three", "seatIndex": 7, "stack": 19750,
                        "handState": {"status": "folded", "holeCards": None, "currentBet": 250}})
        self.obs["table"]["hand"]["actions"] += [
            {"street": "preflop", "agentId": "agent-three", "type": "raise", "amount": 250},
            {"street": "preflop", "agentId": "agent-two", "type": "allIn", "amount": 50},
            {"street": "preflop", "agentId": "agent-three", "type": "fold", "amount": 0, "cause": "timeout"}]
        self.obs["table"]["hand"]["pot"] = 600
        self.obs["actionRequest"]["allowedActions"] = [{"type": "fold"}, {"type": "call", "amount": 150}]
        state = ObservationAdapter().convert(self.obs)
        self.assertEqual(state["amount_owed"], 150)
        self.assertEqual(state["action_log"][-2]["amount"], 250)
        self.assertEqual(state["action_log"][-1]["cause"], "timeout")
        self.assertEqual(state["min_raise_to"], 0)

    def test_fail_closed_on_bad_facts(self):
        mutations = [lambda o: o["table"]["hand"].pop("pot"),
                     lambda o: o["table"]["hand"].update(pot=301),
                     lambda o: o["table"]["players"][0]["handState"].update(currentBet=0),
                     lambda o: o.update(agentId="unknown"),
                     lambda o: o["table"]["hand"]["actions"][0].update(agentId="unknown"),
                     lambda o: o["table"]["hand"].update(phase="showdown"),
                     lambda o: o["actionRequest"]["allowedActions"][1].update(amount=99)]
        for mutation in mutations:
            obs = observation()
            mutation(obs)
            with self.subTest(mutation=mutation), self.assertRaises(ProtocolError):
                ObservationAdapter().convert(obs)

    def test_strict_request_shapes(self):
        for body in ({}, {"tableId": "t", "competitionId": "c"}, {"tableId": 3}):
            with self.assertRaises(ProtocolError):
                validate_body("leave", body)
        for amount in (1.1, True, "3", -1, 2**53):
            with self.assertRaises(ProtocolError):
                allowed_actions([{"type": "raise", "minAmount": amount, "maxAmount": 999}])
        with self.assertRaises(ProtocolError):
            validate_decision({"type": "call", "amount": 100}, self.allowed)

    def test_live_history_omits_zero_contribution_amount(self):
        hand = self.obs["table"]["hand"]
        hand["actions"].append({"street": "preflop", "agentId": "agent-two", "type": "check"})
        self.assertEqual(ObservationAdapter().convert(self.obs)["action_log"][-1]["amount"], 0)
        hand["actions"][-1]["type"] = "call"
        with self.assertRaises(ProtocolError):
            ObservationAdapter().convert(self.obs)
