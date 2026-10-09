"""Frozen, serial six-player paired batches using the existing backtest referee.

Outputs contain private decision inputs; use a new directory outside the repo.
The observer adds authoritative streets and protocol-style raise availability
equally to every strategy. It never changes the engine's betting rules.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import gc
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import random
import shutil
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import backtest
from engine.competition import CompetitionConfig
from competition_client.adapter import convert_decision
from local_arena.protocol import legal_actions, legacy_decision
from local_arena.engine import ArenaEngine


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def arena_factory(**kwargs):
    kwargs.pop("competition_rules", None)
    return ArenaEngine(**kwargs)


def quantiles(values):
    values = sorted(values)
    if not values:
        return {"count": 0, "p50": None, "p95": None, "max": None}
    return {"count": len(values), "p50": values[int((len(values)-1)*.5)],
            "p95": values[int((len(values)-1)*.95)], "max": values[-1]}


def freeze(config_path, output):
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    config = json.loads(config_path.read_text())
    frozen = deepcopy(config)
    frozen["rules"] = json.loads((ROOT / config["rules"]).read_text())
    files, sources = {}, {}
    for label, source in [("B", config["baseline"]), *config["candidates"].items(),
                          *[(f"H{i+1}", p) for i, p in enumerate(config["history"])]]:
        directory = output / "sources" / label
        directory.mkdir(parents=True)
        path = ROOT / source
        if label in config["candidates"]:
            for dependency in ("baseline.py", "semantics.py", path.name):
                shutil.copy2(path.with_name(dependency), directory / dependency)
            entry = directory / path.name
        else:
            # Identical protocol history adaptation for all historical entrants.
            underlying = ROOT / config["history"][0] if label == "B" else path
            shutil.copy2(underlying, directory / "legacy.py")
            wrapper = (ROOT / config["baseline"]).read_text()
            wrapper = wrapper.replace('_path = Path(__file__).resolve().parents[1] / "bot_v16_squeeze_C_metacap" / "bot.py"',
                                      '_path = Path(__file__).with_name("legacy.py")')
            if label != "B":
                wrapper = wrapper.replace('BOT_NAME = "sohu_v16_squeeze_C_metacap"', 'BOT_NAME = "sohu_adapted_" + _base.BOT_NAME')
            (directory / "bot.py").write_text(wrapper)
            entry = directory / "bot.py"
        sources[label] = str(entry)
        files[source] = digest(path)
    # Full executable source snapshot, excluding credentials/configured state.
    for name in ("engine", "competition_client", "local_arena", "tests", "scripts"):
        shutil.copytree(ROOT / name, output / "harness" / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(ROOT / "backtest.py", output / "harness" / "backtest.py")
    shutil.copy2(config_path, output / "evaluation.original.json")
    frozen["sources"] = sources
    frozen["created_at_utc"] = time.time()
    frozen["time_limit_seconds"] = 14400
    write_json(output / "evaluation.json", frozen)
    hashes = {str(p.relative_to(output)): digest(p) for p in output.rglob("*") if p.is_file()}
    write_json(output / "manifest.json", {"source_hashes": files, "snapshot_hashes": hashes,
               "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
               "git_status": subprocess.check_output(["git", "status", "--short"], cwd=ROOT, text=True),
               "python": sys.version, "packages": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
               "command": sys.argv})
    return frozen


class Observer:
    def __init__(self, modules, trace=None, context=None):
        self.modules, self.trace, self.context = modules, trace, context
        self.log = []
        self.times = defaultdict(list)
        self.invalid = Counter()
        self.triggers = Counter()
        self.full_hands = 0
        self.pending = None

    def __call__(self, event, eng, state, **data):
        if event == "start_hand":
            self.log = [dict(e, street="preflop") for e in eng.action_log]
        elif event == "before_decision":
            state["action_log"] = deepcopy(self.log)
            allowed = {a["type"]: a for a in legal_actions(eng, state["seat_to_act"])}
            raise_spec = allowed.get("raise") or allowed.get("bet")
            own = state["your_bet_this_street"]
            state["min_raise_to"] = own + raise_spec["minAmount"] if raise_spec else 0
            state["max_raise_to"] = own + raise_spec["maxAmount"] if raise_spec else 0
        elif event == "decision":
            bid, action, elapsed = data["bid"], data["action"], data["elapsed"]
            self.times[bid].append(elapsed)
            act, amount = action.get("action"), action.get("amount")
            legal = legal_actions(eng, state["seat_to_act"])
            allowed = {a["type"]: a for a in legal}
            kind = "allIn" if act == "all_in" else "raise" if act == "raise" and "raise" in allowed else "bet" if act == "raise" else act
            illegal = kind not in allowed
            if kind in {"bet", "raise"}:
                illegal |= type(amount) is not int or not 0 < state["min_raise_to"] <= (amount or 0) <= state["max_raise_to"]
            self.invalid[bid] += int(illegal)
            # Use the actual HTTP action conversion equally for every entrant.
            # Raw invalid suggestions and legal submitted actions stay distinct.
            converted = convert_decision(action, state, legal)
            self.pending = (state["street"], state["amount_owed"], state["your_stack"])
            if bid == "hero":
                base = self.modules[bid]._base
                # Trigger counts describe input occurrences, not distinct hands.
                records = base._reconstruct_hand(state["action_log"])
                for key, trigger in {
                    "S1": any(r["action"] == "all_in" or r.get("is_all_in") for r in records),
                    "S2": any(e["action"] == "big_blind" and e["amount"] != state["big_blind"] for e in self.log),
                    "S3": state["max_raise_to"] == 0 and state["current_bet"] > 0,
                }.items():
                    self.triggers[key] += int(trigger)
                if self.trace is not None:
                    self.trace.write(json.dumps({"context": self.context, "state": state, "action": action,
                                                 "converted": converted, "illegal": bool(illegal), "elapsed": elapsed}) + "\n")
            return legacy_decision(converted, eng.players[state["seat_to_act"]])
        elif event == "after_action":
            street, owed, stack = self.pending
            entry = dict(eng.action_log[-1], street=street)
            if entry["action"] == "call":
                entry["amount"] = min(owed, stack)
            self.log.append(entry)
        elif event == "hand_complete":
            self.full_hands += int(len(eng.players) == 6)


def play(paths, seat, seed, config, trace=None, context=None):
    before = set(sys.modules)
    ids = [f"opponent-{i}" for i in range(5)]
    ids.insert(seat, "hero")
    paths = list(paths)
    modules = {}
    try:
        dm = backtest.build_decide_map(dict(zip(ids, paths)))
        for bid, decide in dm.items():
            mod = sys.modules[decide.__module__]
            modules[bid] = mod
            decide({"type": "warmup"})
        self_stats = [m._base.PLAYER_STATS for m in modules.values()]
        if len({id(s) for s in self_stats}) != 6:
            raise RuntimeError("Shared player statistics")
        observer = Observer(modules, trace, context)
        started = time.monotonic()
        report = backtest.run_match_inproc(dm, config["rules"]["hands_per_match"], seed,
                    budget=config["budget"], competition_config=CompetitionConfig(**config["rules"]),
                    compute_ev=False, observer=observer, engine_factory=arena_factory)
        report.update(elapsed=time.monotonic()-started, full_hands=observer.full_hands,
                      referee="local_arena.engine.ArenaEngine", protocol_conversion=True,
                      decision_times=dict(observer.times), invalid=dict(observer.invalid), triggers=dict(observer.triggers))
        if sum(report["net_result"].values()) != 0:
            raise RuntimeError("Score conservation failed")
        return report
    finally:
        for name in set(sys.modules) - before:
            if name.startswith("_bt_bot_"):
                del sys.modules[name]
        gc.collect()


def summary(rows, config):
    import numpy as np
    bb = config["rules"]["big_blind"]
    rng = np.random.default_rng(config["bootstrap_seed"])
    result = {}
    for variant in sorted({r["variant"] for r in rows}):
        subset = [r for r in rows if r["variant"] == variant]
        groups, draws = [], []
        for mix in range(len(config["mixes"])):
            mr = [r for r in subset if r["mix"] == mix]
            if not mr:
                continue
            stratum_draws = []
            for order in (0, 1):
                values = np.array([(r["A"]["net_result"]["hero"]-r["B"]["net_result"]["hero"])/bb for r in mr if r["order"] == order])
                if len(values):
                    stratum_draws.append(values[rng.integers(0, len(values), (config["bootstrap_replicates"], len(values)))].mean(axis=1))
            draw = np.mean(stratum_draws, axis=0)
            draws.append(draw)
            means = [(r["A"]["net_result"]["hero"]-r["B"]["net_result"]["hero"])/bb for r in mr]
            groups.append({"mix": mix, "pairs": len(mr), "difference_bb_per_match": statistics.mean(means),
                           "ci95": np.quantile(draw, [.025,.975]).tolist(), "difference_distribution": quantiles(means),
                           "by_seat": {str(s): statistics.mean([(r["A"]["net_result"]["hero"]-r["B"]["net_result"]["hero"])/bb for r in mr if r["seat"] == s]) for s in range(6) if any(r["seat"] == s for r in mr)}})
        sides = {}
        for side in ("A", "B"):
            reports = [r[side] for r in subset]
            net = sum(r["net_result"]["hero"] for r in reports)
            hands = sum(r["hands_played_by_agent"]["hero"] for r in reports)
            sides[side] = {"net_chips": net, "hero_hands": hands, "bb_per_100": net / bb / hands * 100 if hands else None,
                           "decision_seconds": quantiles([v for r in reports for v in r["decision_times"].get("hero", [])]),
                           "match_seconds": quantiles([r["elapsed"] for r in reports]),
                           "all_player_errors": sum(sum(r["errors"].values()) for r in reports),
                           "all_player_timeouts": sum(sum(t["slow"] for t in r["timing"].values()) for r in reports),
                           "hero_invalid": sum(r["invalid"].get("hero", 0) for r in reports),
                           "all_player_invalid": sum(sum(r["invalid"].values()) for r in reports),
                           "full_table_hands": sum(r["full_hands"] for r in reports),
                           "table_hands": sum(r["hands"] for r in reports),
                           "incomplete_matches": sum(r["hands"] != config["rules"]["hands_per_match"] or r["full_hands"] != r["hands"] for r in reports),
                           "triggers": dict(sum((Counter(r["triggers"]) for r in reports), Counter()))}
        overall = np.mean(draws, axis=0)
        ci = np.quantile(overall, [.025,.975]).tolist()
        reliable = all(not sides[s][k] for s in ("A", "B") for k in ("all_player_errors", "all_player_timeouts", "incomplete_matches")) and sides["A"]["hero_invalid"] <= sides["B"]["hero_invalid"]
        threshold = config["noninferiority_bb_per_match"]
        decision = "continue_validation"
        if not reliable:
            decision = "rework_reliability"
        elif ci[0] > 0 and all(g["difference_bb_per_match"] >= threshold and g["ci95"][1] >= 0 for g in groups):
            decision = "improvement_supported"
        elif ci[0] > threshold and all(g["ci95"][0] > threshold for g in groups):
            decision = "correctness_only_noninferiority"
        result[variant] = {"pairs": len(subset), "difference_bb_per_match": statistics.mean(g["difference_bb_per_match"] for g in groups),
                           "ci95": ci, "groups": groups, "sides": sides, "statistical_gate": decision,
                           "note": "Engineering correctness and explained behavior changes are separate required gates; only confirmation can support adoption."}
    return result


def run_stage(output, config, stage):
    plan = config["stages"][stage]
    count = plan["pairs_per_mix"]
    if count <= 0 or count % 12:
        raise ValueError("Pairs per mix must be a positive multiple of 12")
    directory = output / stage
    directory.mkdir(exist_ok=False)
    write_json(directory / "pre_registered.json", {"stage": stage, "plan": plan, "mixes": config["mixes"],
               "sources": {k: digest(Path(p)) for k,p in config["sources"].items()},
               "threshold": config["noninferiority_bb_per_match"], "bootstrap_replicates": config["bootstrap_replicates"],
               "bootstrap_seed": config["bootstrap_seed"], "command": sys.argv})
    rows = []
    clock_path = output / "compute_budget.json"
    spent = json.loads(clock_path.read_text())["elapsed"] if clock_path.exists() else 0
    started = time.monotonic()
    stopped = False
    try:
        with (directory / "matches.jsonl").open("x") as raw, (directory / "traces.jsonl").open("x") as trace:
            for variant in plan["variants"]:
                for mix, counts in enumerate(config["mixes"]):
                    if len(counts) != len(config["history"]) or sum(counts) != 5:
                        raise ValueError("Every mix must contain exactly five opponents")
                    field = [config["sources"][f"H{i+1}"] for i,n in enumerate(counts) for _ in range(n)]
                    for i in range(count):
                        if spent + time.monotonic()-started >= config["time_limit_seconds"]:
                            stopped = True
                            break
                        order, seat = int(i >= count//2), i % 6
                        seed = plan["seed_base"] + 1000*mix + i
                        opponents = field if order == 0 else list(reversed(field))
                        pair = {"variant": variant, "mix": mix, "order": order, "seat": seat, "seed": seed}
                        for side, key in (("A",variant), ("B","B")):
                            paths = opponents[:]
                            paths.insert(seat, config["sources"][key])
                            # Smoke/control keep input traces for offline behavior audit.
                            context = {k: pair[k] for k in ("variant", "mix", "order", "seat", "seed")}
                            context["side"] = side
                            pair[side] = play(paths, seat, seed, config, trace if stage in {"smoke", "control"} else None, context)
                        raw.write(json.dumps(pair, allow_nan=False)+"\n")
                        raw.flush()
                        rows.append(pair)
                        if (i+1) % 12 == 0:
                            print(json.dumps({"stage": stage, "variant": variant, "mix": mix, "pairs_completed": i+1,
                                              "elapsed": round(time.monotonic()-started, 2)}), flush=True)
                    if stopped:
                        break
                if stopped:
                    break
    finally:
        spent += time.monotonic()-started
        write_json(clock_path, {"elapsed": spent, "limit": config["time_limit_seconds"]})
        write_json(directory / "status.json", {"pairs": len(rows), "budget_stopped": stopped, "elapsed_total": spent})
        if rows:
            write_json(directory / "summary.json", summary(rows, config))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/iteration01.evaluation.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage", choices=["freeze", "control", "screen", "smoke", "development", "confirmation"], required=True)
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    if output.is_relative_to(ROOT):
        parser.error("Use a new output directory outside the repository")
    if args.stage == "freeze":
        freeze(args.config.resolve(), output)
    else:
        config = json.loads((output / "evaluation.json").read_text())
        run_stage(output, config, args.stage)


if __name__ == "__main__":
    main()
