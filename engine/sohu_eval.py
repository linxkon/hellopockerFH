"""Offline Sohu short-match and preliminary-league evaluation CLI."""
import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import random

from .competition import CompetitionConfig
from .tournament import bounded_tables, sohu_standings, select_sohu_finalists


def simulate_preliminary(bot_paths, config, seed, run_match=None):
    from backtest import build_decide_map, make_ids, run_match_inproc
    run_match = run_match or run_match_inproc
    ids = make_ids(bot_paths)
    paths = dict(zip(ids, bot_paths))
    persistent_strategies = build_decide_map(paths)
    balances = {bid: config.initial_bankroll for bid in ids}
    records, groups, matches = [], [], []
    for round_number in range(1, config.total_rounds+1):
        if round_number <= 3:
            order = ids[:]
            random.Random(seed*7919+round_number).shuffle(order)
        else:
            order = [r["bot_id"] for r in sohu_standings(records, config)]
        tables = bounded_tables(order, config.max_players)
        groups.append(tables)
        for index, table in enumerate(tables):
            if len(table) < 2:
                for bid in table:
                    records.append({"bot_id": bid, "net_result": 0, "hands_played": 0, "round": round_number})
                continue
            seating = table[:]
            random.Random(seed*1000003+round_number*101+index).shuffle(seating)
            dm = {bid: persistent_strategies[bid] for bid in seating}
            result = run_match(dm, config.hands_per_match, seed*1009+round_number*101+index,
                               competition_config=config, initial_balances=balances, compute_ev=False)
            matches.append(result)
            for bid in table:
                balances[bid] = result["bankroll_remaining"][bid]
                records.append({"bot_id": bid, "net_result": result["net_result"][bid],
                                "hands_played": result["hands_played_by_agent"][bid], "round": round_number})
    standings = sohu_standings(records, config)
    try:
        finalists = [r["bot_id"] for r in select_sohu_finalists(standings)]
        playoff_required = False
    except ValueError:
        finalists, playoff_required = None, True
    return {"environment_version": "sohu-v1", "competition_config": config.to_dict(),
            "seed": seed, "standings": standings, "finalists": finalists,
            "playoff_required": playoff_required, "round_groups": groups,
            "bankroll_remaining": balances, "matches": matches}


def simulate_finals(bot_paths, preliminary, config, seed, run_match=None):
    """Balanced semifinals then a fresh final Competition, official tie rules."""
    from backtest import build_decide_map, make_ids, run_match_inproc
    run_match = run_match or run_match_inproc
    selected = select_sohu_finalists(preliminary, 12)
    if len(selected) != 12:
        raise ValueError("Finals require exactly twelve qualified Agents")
    if any(row["tiebreak_required"] for row in selected):
        raise ValueError("Resolve tied preliminary ranks before simulating finals")
    ids = make_ids(bot_paths)
    paths = dict(zip(ids, bot_paths))
    ranks = {row["bot_id"]: i+1 for i, row in enumerate(selected)}
    ordered = [row["bot_id"] for row in selected]
    group_indices = ([0, 3, 4, 7, 8, 11], [1, 2, 5, 6, 9, 10])
    semifinals, finalists = [], []
    semi_cfg = replace(config, total_rounds=1, hands_per_match=20, max_players=6)
    for group, indices in enumerate(group_indices):
        table = [ordered[i] for i in indices]
        seated = table[:]
        random.Random(seed*7919+group).shuffle(seated)
        result = run_match(build_decide_map({bid: paths[bid] for bid in seated}), 20,
                           seed*1009+group, competition_config=semi_cfg, compute_ev=False)
        records = [{"bot_id": bid, "net_result": result["net_result"][bid],
                    "hands_played": result["hands_played_by_agent"][bid]} for bid in table]
        standings = sohu_standings(records, semi_cfg, preliminary_ranks=ranks)
        qualified = [row["bot_id"] for row in standings if row["qualification_eligible"]][:3]
        finalists.extend(qualified)
        semifinals.append({"group": table, "standings": standings, "match": result})
    if len(finalists) != 6:
        raise ValueError("Each semifinal needs three qualified finishers")
    random.Random(seed*7919+2).shuffle(finalists)
    final_cfg = replace(config, total_rounds=1, hands_per_match=30, max_players=6)
    result = run_match(build_decide_map({bid: paths[bid] for bid in finalists}), 30,
                       seed*1009+2, competition_config=final_cfg, compute_ev=False)
    records = [{"bot_id": bid, "net_result": result["net_result"][bid],
                "hands_played": result["hands_played_by_agent"][bid]} for bid in finalists]
    return {"semifinals": semifinals, "final": {"group": finalists,
            "standings": sohu_standings(records, final_cfg, preliminary_ranks=ranks), "match": result}}


def evaluate_short(bot_paths, config, matches, seed_base, hands):
    import analysis
    import backtest
    analysis.bind_harness(backtest)
    result = analysis._run_eval(bot_paths[0], bot_paths[1:], matches, hands, seed_base,
                                1.0, True, True, True, competition_config=config.to_dict())
    hero = result["hero_id"]
    ids = backtest.make_ids(bot_paths)
    result.update({"schema_version": 3, "mode": "eval", "hero": {"id": hero, "path": bot_paths[0]},
                   "opponents": [{"id": bid, "path": path} for bid, path in zip(ids[1:], bot_paths[1:])],
                   "params": {"matches": matches, "hands": hands, "seed_base": seed_base,
                              "competition_config": config.to_dict(), "big_blind": config.big_blind,
                              "environment_version": "sohu-v1"},
                   "metadata": {"label": f"Sohu baseline {hands} hands", "completed_at": datetime.now(timezone.utc).isoformat()}})
    placements = [r for r in result["per_match_placement"][hero] if r is not None]
    result.update({"seed_base": seed_base, "hands_per_match": hands,
                   "top3_probability": sum(rank <= 3 for rank in placements)/matches,
                   "champion_probability": sum(rank == 1 for rank in placements)/matches,
                   "scope": "Offline opponent panel; probabilities describe this panel only"})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Local CompetitionConfig JSON or official Discovery summary")
    parser.add_argument("--bots", nargs="+", required=True)
    parser.add_argument("--scenario", choices=["20", "30", "preliminary"], default="20")
    parser.add_argument("--matches", type=int, default=20)
    parser.add_argument("--include-finals", action="store_true", help="After preliminary, simulate balanced semifinals and a fresh final")
    parser.add_argument("--seed-base", type=int, default=0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    raw = json.loads(Path(args.config).read_text())
    config = CompetitionConfig.from_summary(raw) if "blinds" in raw else CompetitionConfig(**raw)
    if args.matches < 1 or len(args.bots) < 2:
        parser.error("Positive matches and at least two bots required")
    if args.include_finals and args.scenario != "preliminary":
        parser.error("--include-finals requires --scenario preliminary")
    if args.scenario == "preliminary":
        simulations = [simulate_preliminary(args.bots, config, args.seed_base+i) for i in range(args.matches)]
        if args.include_finals:
            for i, simulation in enumerate(simulations):
                try:
                    simulation["finals"] = simulate_finals(args.bots, simulation["standings"], config, args.seed_base+i)
                except ValueError as exc:
                    simulation["finals"] = {"unresolved": str(exc)}
        from backtest import make_ids
        hero = make_ids(args.bots)[0]
        result = {"environment_version": "sohu-v1", "competition_config": config.to_dict(),
                  "seed_base": args.seed_base, "simulations": simulations,
                  "top12_probability": sum(hero in (s["finalists"] or []) for s in simulations)/args.matches,
                  "playoff_probability": sum(s["playoff_required"] for s in simulations)/args.matches}
    else:
        hands = int(args.scenario)
        config = replace(config, total_rounds=1, hands_per_match=hands)
        result = evaluate_short(args.bots, config, args.matches, args.seed_base, hands)
    result["config_source"] = args.config
    result["config_note"] = "Verify these parameters against the selected Competition; protocol examples are not official match settings"
    result["dependencies"] = {name: version(name) for name in ("eval7", "future", "pyparsing")}
    result["strategy_sha256"] = {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in args.bots}
    baseline = Path(__file__).resolve().parents[1]/"hero_bots/bot_v16_squeeze_C_metacap/bot.py"
    result["baseline_sha256"] = hashlib.sha256(baseline.read_bytes()).hexdigest()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
