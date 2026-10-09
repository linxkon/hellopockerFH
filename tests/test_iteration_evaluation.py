import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import importlib.util
import unittest

from scripts.evaluate_iteration import Observer, arena_factory, summary
from backtest import run_match_inproc


class EvaluationTests(unittest.TestCase):
    def test_observer_preserves_short_allin_permission_and_protocol_fallback(self):
        e = arena_factory(hand_id="h", bot_ids=["a", "b", "c"], starting_stacks={"a": 1000, "b": 150, "c": 1000},
                          small_blind=50, big_blind=100, seed=8)
        s = e.start_hand()
        o = Observer({})
        o("start_hand", e, s)

        def act(raw):
            nonlocal s
            o("before_decision", e, s, bid="a")
            converted = o("decision", e, s, bid="a", action=raw, elapsed=.01)
            s = e.apply_action(s["seat_to_act"], converted)
            o("after_action", e, s)

        act({"action": "call"})
        o("before_decision", e, s, bid="b")
        self.assertEqual(s["max_raise_to"], 0)  # short jam remains allowed
        self.assertEqual(o("decision", e, s, bid="b", action={"action": "all_in"}, elapsed=.01), {"action": "all_in"})
        self.assertEqual(o.invalid["b"], 0)
        s = e.apply_action(1, {"action": "all_in"})
        o("after_action", e, s)
        act({"action": "call"})
        o("before_decision", e, s, bid="a")
        self.assertEqual(s["min_raise_to"], 0)
        self.assertEqual(o("decision", e, s, bid="a", action={"action": "all_in"}, elapsed=.01), {"action": "fold"})
        self.assertEqual(o.invalid["a"], 1)
        self.assertTrue(all("street" in row for row in s["action_log"]))

    def test_observer_factory_optional_and_scores_conserved(self):
        cfg = dict(small_blind=50, big_blind=100, buy_in=1000, initial_bankroll=10000, automatic_rebuy=True,
                   max_players=6, hands_per_match=4)
        dm = {str(i): lambda s: {"action": "check" if s["can_check"] else "call"} for i in range(6)}
        events = []
        def observer(event, eng, state, **data):
            events.append(event)
            if event == "hand_complete":
                self.assertEqual(sum(state["final_stacks"].values()), 6000)
        a = run_match_inproc(dm, 4, 7, competition_config=cfg, compute_ev=False)
        b = run_match_inproc(dm, 4, 7, competition_config=cfg, compute_ev=False, observer=observer)
        self.assertEqual(a["net_result"], b["net_result"])
        self.assertEqual(events.count("hand_complete"), 4)
        c = run_match_inproc(dm, 4, 7, competition_config=cfg, compute_ev=False, engine_factory=arena_factory)
        self.assertEqual(sum(c["net_result"].values()), 0)
        self.assertEqual(list(c["hands_played_by_agent"].values()), [4]*6)

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "Install requirements-evaluation.txt for statistics")
    def test_summary_weights_mixes_equally_and_uses_actual_hands(self):
        def result(net):
            return dict(net_result={"hero":net}, hands_played_by_agent={"hero":20},
                        decision_times={"hero":[.01]}, elapsed=.2, errors={"hero":0},
                        timing={"hero":{"slow":0}}, invalid={"hero":0}, full_hands=20, hands=20, triggers={})
        rows = [dict(variant="C",mix=m,order=i%2,seat=i%6,A=result((1+2*m)*1000),B=result(0))
                for m,n in ((0,12),(1,24)) for i in range(n)]
        config=dict(rules={"big_blind":1000,"hands_per_match":20},bootstrap_seed=1,bootstrap_replicates=100,
                    mixes=[[3,2],[2,3]],noninferiority_bb_per_match=-.2)
        report=summary(rows,config)["C"]
        self.assertEqual(report["difference_bb_per_match"],2)
        self.assertEqual(report["ci95"],[2,2])
        self.assertAlmostEqual(report["sides"]["A"]["bb_per_100"], 84000/1000/720*100)


if __name__ == "__main__":
    unittest.main()
