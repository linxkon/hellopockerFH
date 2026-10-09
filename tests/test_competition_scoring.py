from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import analysis
import backtest
from engine.competition import CompetitionConfig, standing
from engine.game import PokerEngine
from engine.tournament import bounded_tables, sohu_standings, select_sohu_finalists, swiss_pairing

CONFIG = CompetitionConfig(small_blind=1, big_blind=2, buy_in=20, initial_bankroll=60,
                           automatic_rebuy=True, max_players=6, total_rounds=1, hands_per_match=20)


class ScoringTests(unittest.TestCase):
    def test_manual_rates_qualification_and_zero(self):
        cfg = replace(CONFIG, total_rounds=10)
        self.assertEqual(cfg.required_hands, 190)
        self.assertEqual(standing(400, 20, CONFIG)["bb_per_100"], 1000)
        self.assertFalse(standing(0, 189, cfg)["qualification_eligible"])
        self.assertTrue(standing(0, 190, cfg)["qualification_eligible"])
        self.assertIsNone(standing(0, 0, cfg)["bb_per_100"])
        self.assertEqual(backtest.summarize([20, 20], 20, [10, 20], 2)["bb_per_100"], 40/2/30*100)

    def test_rebuy_transfers_and_actual_hands(self):
        dm = {"a": lambda s: {"action": "all_in"}, "b": lambda s: {"action": "call"}}
        result = backtest.run_match_inproc(dm, 20, 42, compute_ev=False, competition_config=CONFIG)
        self.assertGreater(sum(result["rebuy_count"].values()), 0)
        self.assertEqual(sum(result["net_result"].values()), 0)
        for bid in dm:
            self.assertEqual(result["bankroll_remaining"][bid]-CONFIG.initial_bankroll, result["net_result"][bid])
            played = result["hands_played_by_agent"][bid]
            self.assertEqual(result["bb_per_100_by_agent"][bid], result["net_result"][bid]/CONFIG.big_blind/played*100)
        self.assertLess(result["hands"], 20)

    def test_different_actual_hands_and_no_rebuy(self):
        dm = {"a": lambda s: {"action": "all_in"}, "b": lambda s: {"action": "fold"},
              "c": lambda s: {"action": "call"}}
        cfg = replace(CONFIG, automatic_rebuy=False)
        result = backtest.run_match_inproc(dm, 20, 42, compute_ev=False, competition_config=cfg)
        self.assertGreater(len(set(result["hands_played_by_agent"].values())), 1)
        self.assertEqual(sum(result["rebuy_count"].values()), 0)
        for bid in dm:
            self.assertEqual(result["net_result"][bid], result["bankroll_remaining"][bid]-cfg.initial_bankroll)

    def test_zero_hands_leaving_and_inactive(self):
        dm = {"a": lambda s: {"action": "fold"}, "b": lambda s: {"action": "fold"}}
        for kwargs in ({"n_hands": 0}, {"n_hands": 20, "competition_active": False},
                       {"n_hands": 20, "leaving_agents": ("a",)},
                       {"n_hands": 20, "initial_balances": {"a": 19, "b": 19}}):
            result = backtest.run_match_inproc(dm, seed=0, compute_ev=False, competition_config=CONFIG, **kwargs)
            self.assertEqual(result["net_result"], {"a": 0, "b": 0})
            self.assertTrue(all(v is None for v in result["bb_per_100_by_agent"].values()))

    def test_new_blinds_and_short_allin_does_not_reopen(self):
        engine = PokerEngine("test", ["a", "b", "c"], starting_stacks={"a": 100, "b": 3, "c": 100}, small_blind=1, big_blind=2, competition_rules=True)
        state = engine.start_hand()
        self.assertEqual(state["seat_to_act"], 0)
        state = engine.apply_action(0, {"action": "call"})
        state = engine.apply_action(1, {"action": "all_in"})
        state = engine.apply_action(2, {"action": "call"})
        self.assertEqual(state["street"], "preflop")
        self.assertEqual(state["seat_to_act"], 0)
        self.assertEqual(state["amount_owed"], 1)
        self.assertEqual(state["min_raise_to"], 0)
        state = engine.apply_action(0, {"action": "raise", "amount": 10})
        self.assertEqual(state["street"], "flop")
        self.assertEqual(engine.players[0].total_invested, 3)

    def test_legacy_defaults_and_seed_determinism(self):
        dm = {"a": lambda s: {"action": "call"}, "b": lambda s: {"action": "call"}}
        first = backtest.run_match_inproc(dm, 10, 42, compute_ev=False)
        second = backtest.run_match_inproc(dm, 10, 42, compute_ev=False)
        self.assertEqual(first["chip_delta"], second["chip_delta"])
        self.assertEqual(first["environment_version"], "fullhouse-legacy-v2")
        self.assertEqual(sum(first["final_stacks"].values()), 20000)

    def test_bounded_grouping(self):
        for size in range(2, 65):
            tables = bounded_tables(list(range(size)), 6)
            self.assertEqual([p for t in tables for p in t], list(range(size)))
            self.assertTrue(all(2 <= len(t) <= 6 for t in tables))
        self.assertTrue(all(len(t) <= 6 for t in swiss_pairing([{"bot_id": i} for i in range(13)])))

    def test_official_ties_and_playoff(self):
        records = [{"bot_id": "a", "net_result": 20, "hands_played": 20, "round": 1},
                   {"bot_id": "b", "net_result": 10, "hands_played": 10, "round": 1},
                   {"bot_id": "c", "net_result": 20, "hands_played": 20, "round": 4}]
        table = sohu_standings(records, CONFIG)
        self.assertEqual([r["bot_id"] for r in table], ["c", "a", "b"])
        tied = sohu_standings([{"bot_id": b, "net_result": 0, "hands_played": 20} for b in ("a", "b")], CONFIG)
        self.assertTrue(all(r["tiebreak_required"] for r in tied))
        with self.assertRaises(ValueError):
            select_sohu_finalists(tied, n=1)
        finals = sohu_standings(records, CONFIG, preliminary_ranks={"a": 1, "b": 2, "c": 3})
        self.assertEqual([r["bot_id"] for r in finals], ["a", "b", "c"])

    def test_analysis_persistence_and_actual_rate_matrix(self):
        analysis.bind_harness(backtest)
        result = {"environment_version": "sohu-v1", "competition_config": CONFIG.to_dict(),
                  "per_match_deltas": {"a": [20, 20], "b": [20, 20]}, "per_match_hands": {"a": [10, 10], "b": [20, 20]}}
        job = {"job_id": "test", "label": "", "mode": "eval", "created_at": "", "started_at": "", "completed_at": "",
               "opponents": [], "preset_name": "panel", "hero": {"id": "a"}, "params": {"competition_config": CONFIG.to_dict(), "hands": 20}}
        with tempfile.TemporaryDirectory() as directory:
            path = analysis.write_result_json(job, directory, result, None)
            stored = analysis.normalize_result(json.loads(path.read_text()))
            self.assertEqual(stored["per_match_hands"], result["per_match_hands"])
            cell = analysis.matrix_cell(stored, "a", "b", "realized", use_scipy=False)
            self.assertEqual(cell["mean_bb_per_100"], 50)
            self.assertEqual(analysis.absolute_matrix(stored, ["a"], "realized")["a"]["mean_bb_per_100"], 100)
            self.assertIn("sohu", analysis.probe_label(stored))

    def test_placement_uses_actual_rate(self):
        result = {"environment_version": "sohu-v1", "competition_config": CONFIG.to_dict(),
                  "net_result": {"a": 20, "b": 30}, "chip_delta": {"a": 20, "b": 30},
                  "hands_played_by_agent": {"a": 10, "b": 20}}
        self.assertEqual(analysis.match_placement("a", result), 1)
        self.assertEqual(analysis.match_placement("b", result), 2)

    def test_serial_and_process_pool_propagate_config(self):
        analysis.bind_harness(backtest)
        hero, field = "bots/custom/nit_folder/bot.py", ["bots/custom/calling_station/bot.py"]
        serial = analysis._run_eval(hero, field, 2, 2, 0, 1, True, True, True, competition_config=CONFIG.to_dict())
        parallel = analysis._run_eval(hero, field, 2, 2, 0, 1, True, True, True, workers=2, competition_config=CONFIG.to_dict())
        self.assertEqual(serial["per_match_deltas"], parallel["per_match_deltas"])
        self.assertEqual(serial["per_match_hands"], parallel["per_match_hands"])
        self.assertEqual(serial["match_records"][0]["competition_config"], CONFIG.to_dict())

    def test_full_preliminary_rounds_and_balances(self):
        from engine.sohu_eval import simulate_preliminary
        paths = [f"/offline/agent_{i}/bot.py" for i in range(13)]
        cfg = replace(CONFIG, buy_in=200, initial_bankroll=400, total_rounds=10)
        with patch("backtest.load_decide", return_value=lambda s: {"action": "fold"}) as load:
            result = simulate_preliminary(paths, cfg, seed=7)
        self.assertEqual(load.call_count, 13)  # preserve each Agent's strategy across rounds
        self.assertEqual(len(result["round_groups"]), 10)
        self.assertTrue(all(2 <= len(t) <= 6 for round_tables in result["round_groups"] for t in round_tables))
        self.assertTrue(all(r["hands_played"] == 200 for r in result["standings"]))
        self.assertEqual(sum(result["bankroll_remaining"].values()), 13*400)

    def test_semifinal_groups_ties_and_fresh_final_score(self):
        from engine.sohu_eval import simulate_finals
        paths = [f"/offline/agent_{i}/bot.py" for i in range(12)]
        prelim = [{"bot_id": f"agent_{i}", "net_result": 120-i, "hands_played": 20} for i in range(12)]
        standings = sohu_standings(prelim, CONFIG)
        cfg = replace(CONFIG, buy_in=200, initial_bankroll=400)
        calls = []
        def match(dm, hands, seed, **kwargs):
            calls.append((list(dm), hands, kwargs))
            # Every player ties; only preliminary rank may settle it.
            return {"net_result": {bid: 0 for bid in dm}, "hands_played_by_agent": {bid: hands for bid in dm}}
        with patch("backtest.load_decide", return_value=lambda s: {"action": "fold"}):
            result = simulate_finals(paths, standings, cfg, 7, run_match=match)
        self.assertEqual(result["semifinals"][0]["group"], [f"agent_{i}" for i in (0,3,4,7,8,11)])
        self.assertEqual(result["semifinals"][1]["group"], [f"agent_{i}" for i in (1,2,5,6,9,10)])
        self.assertEqual([r["bot_id"] for r in result["final"]["standings"]], [f"agent_{i}" for i in range(6)])
        self.assertTrue(all(r["net_result"] == 0 for r in result["final"]["standings"]))
        self.assertEqual([c[1] for c in calls], [20,20,30])
        self.assertTrue(all("initial_balances" not in c[2] for c in calls))
