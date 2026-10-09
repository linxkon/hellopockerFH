from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest

from competition_client.adapter import ObservationAdapter
from tests.sohu_helpers import observation

ROOT = Path(__file__).resolve().parents[1]


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def state(log=()):
    return dict(type="action_request", hand_id="h", street="preflop", seat_to_act=0,
                your_cards=["As", "Kd"], community_cards=[], pot=1500,
                your_stack=50000, your_bet_this_street=0, big_blind=1000,
                current_bet=1000, amount_owed=1000, can_check=False,
                min_raise_to=2000, max_raise_to=50000,
                players=[dict(seat=i, bot_id=f"p{i}", stack=50000,
                              bet_this_street=0, is_folded=False, state="active") for i in range(6)],
                action_log=list(log))


def log(allin=3000, explicit=True):
    entries = [dict(seat=1, action="small_blind", amount=500),
               dict(seat=2, action="big_blind", amount=1000),
               dict(seat=3, action="raise", amount=3000),
               dict(seat=4, action="all_in", amount=allin)]
    return [dict(e, street="preflop") for e in entries] if explicit else entries


class SemanticsTests(unittest.TestCase):
    def setUp(self):
        self.old = module("hero_bots/bot_sohu_v16/bot.py", "old")._base
        self.new = module("hero_bots/bot_sohu_iter01/bot.py", "new")._base

    def test_reproduce_allin_call_and_raise_classification(self):
        for explicit in (False, True):
            for amount, raises, full, short in ((1500, 1, False, False), (3000, 1, False, False),
                                                (3500, 2, False, True), (5000, 2, True, False)):
                s = state(log(amount, explicit))
                with self.subTest(explicit=explicit, amount=amount):
                    self.assertEqual(self.old._preflop_history(s)["raises"], 2)
                    self.assertEqual(self.new._preflop_history(s)["raises"], raises)
                    rec = self.new._reconstruct_hand(s["action_log"], s["big_blind"])[-1]
                    self.assertEqual((rec["raw_action"], rec["is_all_in"]), ("all_in", True))
                    self.assertEqual((rec["is_full_raise"], rec["is_short_raise"]), (full, short))
                    summary = self.new._villain_action_summary(s, 4)
                    self.assertEqual(summary["threebet_pre"], raises == 2)

    def test_stats_call_allin_and_idempotence(self):
        s = state(log())
        self.new.update_stats(s)
        stats = self.new.PLAYER_STATS["p4"]
        self.assertEqual([stats[k] for k in ("allins", "calls", "raises", "pfr_yes", "true_pfr_yes", "call_vs_bet", "raise_vs_bet")],
                         [1, 1, 0, 0, 0, 1, 0])
        saved = deepcopy(stats)
        self.assertFalse(self.new.update_stats(s))
        self.assertEqual(stats, saved)
        s["action_log"] += [dict(seat=3, action="check", amount=0, street="flop"),
                            dict(seat=5, action="all_in", amount=500, street="flop")]
        self.new.update_stats(s)
        self.assertEqual(self.new.PLAYER_STATS["p5"]["flop_aggr"], 1)
        self.assertEqual(self.new._reconstruct_hand(s["action_log"], 1000)[-1]["current_before"], 0)

    def test_nominal_blind_and_legacy_fallback(self):
        s = state(log())
        s["action_log"][1]["amount"] = 100
        self.assertEqual(self.old._effective_stack_bb(s), 500)
        self.assertEqual(self.new._effective_stack_bb(s), 50)
        self.assertEqual(self.new._bb_to_chips(s, 3), 3000)
        self.assertEqual(self.new._v16mc_bb_amount(s), 1000)
        s["action_log"] = []
        self.assertEqual(self.new._effective_stack_bb(s), 50)
        s.pop("big_blind")
        self.assertEqual(self.new._v16mc_bb_amount(s), 100)
        s["action_log"] = [dict(action="big_blind", amount=200)]
        self.assertEqual(self.new._v16mc_bb_amount(s), 200)
        for invalid in (0, -1, True, None, "1000"):
            s["big_blind"] = invalid
            self.assertEqual(self.new._v16mc_bb_amount(s), 200)

    def test_short_blind_does_not_turn_nominal_call_into_raise(self):
        s = state(log(1000))
        s["action_log"][1]["amount"] = 100
        s["action_log"].pop(2)
        rec = self.new._reconstruct_hand(s["action_log"], 1000)[-1]
        self.assertEqual((rec["action"], rec["owed"]), ("call", 1000))

    def test_legacy_full_raise_threshold_resets_each_street(self):
        entries = log(3000)
        entries += [dict(seat=5, action="all_in", amount=1000, street="flop")]
        rec = self.new._reconstruct_hand(entries)[-1]
        self.assertTrue(rec["is_full_raise"])
        self.assertFalse(rec["is_short_raise"])

    def test_no_raise_sentinel_and_live_jams(self):
        s = state(log())
        s.update(max_raise_to=0, current_bet=3000, amount_owed=3000)
        self.assertTrue(self.old._is_facing_jam(s))
        self.assertFalse(self.new._is_facing_jam(s))
        s["players"][4].update(state="all_in", stack=0, bet_this_street=3500)
        self.assertTrue(self.new._is_facing_jam(s))
        s["players"][4]["bet_this_street"] = 0  # all-in on a previous street
        self.assertFalse(self.new._is_facing_jam(s))
        s["players"][4].update(state="active", stack=1000, is_all_in=True, bet_this_street=3500)
        self.assertTrue(self.new._is_facing_jam(s))
        s["players"][4]["is_folded"] = True
        self.assertFalse(self.new._is_facing_jam(s))
        s["amount_owed"] = s["your_stack"]
        self.assertTrue(self.new._is_facing_jam(s))
        s["amount_owed"] = 0
        self.assertFalse(self.new._is_facing_jam(s))

    def test_real_adapter_no_raise_regular_bet(self):
        obs = observation()
        obs["actionRequest"]["allowedActions"] = [{"type": "fold"}, {"type": "call", "amount": 100}]
        s = ObservationAdapter().convert(obs)
        self.assertTrue(self.old._is_facing_jam(s))
        self.assertFalse(self.new._is_facing_jam(s))

    def test_real_adapter_allin_call_retains_live_jam_and_correct_profile(self):
        obs = observation()
        players = obs["table"]["players"]
        players[1]["handState"].update(status="allIn", currentBet=250)
        players[1]["stack"] = 0
        players.append({"agentId": "agent-three", "seatIndex": 7, "stack": 19750,
                        "handState": {"status": "folded", "holeCards": None, "currentBet": 250}})
        obs["table"]["hand"]["actions"] += [
            {"street": "preflop", "agentId": "agent-three", "type": "raise", "amount": 250},
            {"street": "preflop", "agentId": "agent-two", "type": "allIn", "amount": 50},
            {"street": "preflop", "agentId": "agent-three", "type": "fold", "amount": 0}]
        obs["table"]["hand"]["pot"] = 600
        obs["actionRequest"]["allowedActions"] = [{"type": "fold"}, {"type": "call", "amount": 150}]
        s = ObservationAdapter().convert(obs)
        self.assertEqual(self.old._preflop_history(s)["raises"], 2)
        self.assertEqual(self.new._preflop_history(s)["raises"], 1)
        self.assertTrue(self.new._is_facing_jam(s))
        self.new.update_stats(s)
        st = self.new.PLAYER_STATS["agent-two"]
        self.assertEqual((st["allins"], st["calls"], st["pfr_yes"]), (1, 1, 0))

    def test_normal_semantics_and_independent_instances(self):
        s = state(log())
        s["action_log"][-1]["action"] = "call"
        s["action_log"][-1]["amount"] = 3000
        for helper in ("_preflop_history", "_effective_stack_bb", "_v16mc_bb_amount", "_is_facing_jam"):
            self.assertEqual(getattr(self.old, helper)(s), getattr(self.new, helper)(s))
        self.assertEqual(self.old._bb_to_chips(s, 3), self.new._bb_to_chips(s, 3))
        other = module("hero_bots/bot_sohu_iter01/bot.py", "other")._base
        self.new.update_stats(s)
        self.assertEqual(other.PLAYER_STATS, {})
        self.assertIsNot(self.new.PLAYER_STATS, other.PLAYER_STATS)
        self.assertEqual(self.new.decide({"type": "warmup"}), self.old.decide({"type": "warmup"}))


if __name__ == "__main__":
    unittest.main()
