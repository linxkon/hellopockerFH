import unittest
import eval7

from local_arena.engine import ArenaEngine
from local_arena.protocol import legal_actions


class ArenaRulesTests(unittest.TestCase):
    def engine(self, stacks, **kwargs):
        return ArenaEngine("hand", list(stacks), starting_stacks=stacks,
                           small_blind=50, big_blind=100, seed=8, **kwargs)

    def test_heads_up_and_multiway_order(self):
        for n in range(2, 7):
            with self.subTest(n=n):
                e = self.engine({str(i): 1000 for i in range(n)})
                s = e.start_hand()
                self.assertEqual(s["seat_to_act"], 0 if n == 2 else 3 % n)
                while s["street"] == "preflop":
                    s = e.apply_action(s["seat_to_act"], {"action": "call"})
                self.assertEqual(s["seat_to_act"], 1)

    def test_small_blind_all_in_has_no_inactive_request(self):
        e = self.engine({"a": 25, "b": 1000})
        s = e.start_hand()
        self.assertEqual(s["type"], "hand_complete")
        self.assertEqual(e.settlement["returnedBets"], [{"agentId": "b", "amount": 75}])
        self.assertEqual(sum(s["final_stacks"].values()), 1025)

    def test_no_betting_into_dry_side_pot(self):
        e = self.engine({"a": 150, "b": 1000})
        s = e.start_hand()
        s = e.apply_action(s["seat_to_act"], {"action": "all_in"})
        actions = legal_actions(e, s["seat_to_act"])
        self.assertEqual({a["type"] for a in actions}, {"fold", "call"})
        s = e.apply_action(s["seat_to_act"], {"action": "call"})
        self.assertEqual(s["type"], "hand_complete")
        self.assertEqual(len(s["community_cards"]), 5)

    def test_short_big_blind_preserves_full_opening_bet(self):
        e = self.engine({"a": 1000, "b": 1000, "c": 25})
        s = e.start_hand()
        self.assertEqual(s["seat_to_act"], 0)
        self.assertEqual(s["current_bet"], 100)
        self.assertEqual(legal_actions(e, 0)[1], {"type": "call", "amount": 100})

    def test_short_all_in_does_not_reopen_caller(self):
        e = self.engine({"a": 1000, "b": 150, "c": 1000})
        s = e.start_hand()  # a calls, b jams to 150, c calls
        s = e.apply_action(0, {"action": "call"})
        s = e.apply_action(1, {"action": "all_in"})
        s = e.apply_action(2, {"action": "call"})
        self.assertEqual(s["seat_to_act"], 0)
        self.assertEqual({a["type"] for a in legal_actions(e, 0)}, {"fold", "call"})

    def test_cumulative_short_all_ins_reopen(self):
        e = self.engine({"a": 1000, "b": 150, "c": 200, "d": 1000})
        e.start_hand()
        e.apply_action(3, {"action": "call"})
        e.apply_action(0, {"action": "call"})
        e.apply_action(1, {"action": "all_in"})
        s = e.apply_action(2, {"action": "all_in"})
        self.assertEqual(s["seat_to_act"], 3)
        self.assertIn("raise", {a["type"] for a in legal_actions(e, 3)})

    def test_side_pots_and_returned_bet(self):
        e = self.engine({"a": 100, "b": 200, "c": 300})
        s = e.start_hand()
        while s["type"] != "hand_complete":
            s = e.apply_action(s["seat_to_act"], {"action": "all_in"})
        result = e.settlement
        self.assertEqual(result["returnedBets"], [{"agentId": "c", "amount": 100}])
        self.assertEqual(sum(p["amount"] for p in result["payouts"]), 500)
        self.assertEqual(sum(s["final_stacks"].values()), 600)
        for p in result["payouts"]:
            self.assertEqual(p["amount"], sum(a["amount"] for a in p["potAwards"]))
        self.assertEqual({a["potIndex"] for p in result["payouts"] for a in p["potAwards"]}, {0, 1})

    def test_fold_returns_unmatched_raise(self):
        e = self.engine({"a": 1000, "b": 1000})
        e.start_hand()
        e.apply_action(0, {"action": "raise", "amount": 500})
        s = e.apply_action(1, {"action": "fold"})
        self.assertEqual(e.settlement["returnedBets"], [{"agentId": "a", "amount": 400}])
        self.assertEqual(e.settlement["payouts"][0]["amount"], 200)
        self.assertFalse(s["revealed_cards"])

    def test_odd_chips_clockwise_from_dealer(self):
        e = self.engine({"a": 1000, "b": 1000, "c": 1000})
        e.start_hand()
        e.community_cards = [eval7.Card(c) for c in ["As", "Ks", "Qs", "Js", "Ts"]]
        for p in e.players:
            p.stack, p.total_invested = 999, 1
        e.players[2].is_folded = True
        e.pot = 3
        result = e._showdown()
        self.assertEqual(result["final_stacks"], {"a": 1000, "b": 1001, "c": 999})


if __name__ == "__main__":
    unittest.main()
