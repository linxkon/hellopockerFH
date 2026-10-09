"""Read-only chronological B/B/C replay; outcomes never score changed actions."""
import argparse
from collections import Counter
from copy import deepcopy
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import backtest
from competition_client.adapter import convert_decision


def replay(snapshot, evaluation, output):
    config = json.loads((evaluation / "evaluation.json").read_text())
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    allowed = {}
    for line in (snapshot / "hands.jsonl").open():
        hand = json.loads(line)
        for snap in hand.get("snapshots", []):
            obs = snap.get("observation", {})
            request = obs.get("actionRequest")
            if request:
                allowed[(obs["competitionId"], obs["table"]["id"], request["id"])] = request["allowedActions"]
    counts, runs = Counter(), {}
    labels = ["B", "B_repeat", "C1", "C2", "C3", "C"]
    with (output / "differences.jsonl").open("x") as report:
        for line in (snapshot / "decisions.jsonl").open():
            row = json.loads(line)
            state = row.get("state")
            if not state:
                counts["missing_state"] += 1
                continue
            counts["decisions"] += 1
            key = (row["run_id"], row["competition_id"])
            if key not in runs:
                modules = {}
                for label in labels:
                    decide = backtest.load_decide(config["sources"]["B" if label == "B_repeat" else label])
                    mod = sys.modules[decide.__module__]
                    decide({"type": "warmup"})
                    modules[label] = mod
                runs[key] = (modules, Counter())
            modules, past = runs[key]
            if row.get("reused"):
                counts["reused_skipped"] += 1
                continue
            old, new = modules["B"]._base, modules["C"]._base
            semantic = {}
            triggers = []
            for label, function in (("S1", "_preflop_history"), ("S2", "_effective_stack_bb"), ("S3", "_is_facing_jam")):
                a, b = getattr(old, function)(state), getattr(new, function)(state)
                semantic[label] = {"B": a, "C": b}
                changed = a != b
                if label == "S1":
                    changed |= any(old._villain_action_summary(state,p["seat"]) != new._villain_action_summary(state,p["seat"]) for p in state["players"])
                if changed:
                    triggers.append(label)
                    past[label] += 1
                    counts[label] += 1
            actions = {label: mod.decide(deepcopy(state)) for label,mod in modules.items()}
            spec = allowed.get((row["competition_id"], row["table_id"], row["request_id"]))
            converted = {label: convert_decision(a, state, spec) for label,a in actions.items()} if spec else None
            counts["allowed_actions_covered"] += int(spec is not None)
            counts["B_repeat_raw_differences"] += int(actions["B"] != actions["B_repeat"])
            counts["B_recorded_raw_differences"] += int(actions["B"] != row.get("raw_decision"))
            changed_labels = [label for label in labels[2:] if actions[label] != actions["B"]]
            for label in changed_labels:
                counts[label + "_raw_differences"] += 1
                if not past:
                    counts["unexplained_raw_differences"] += 1
            if converted:
                for label in labels[2:]:
                    counts[label+"_submitted_differences"] += int(converted[label] != converted["B"])
            if triggers or changed_labels or actions["B"] != actions["B_repeat"]:
                report.write(json.dumps({"run_id": row["run_id"], "hand_id": row["hand_id"], "request_id": row["request_id"],
                                        "state": state, "triggers": triggers, "past_triggers": dict(past), "semantic": semantic,
                                        "actions": actions, "converted": converted})+"\n")
    result = {"counts": dict(counts), "run_competition_groups": len(runs), "snapshot": str(snapshot),
              "settlement_scope": json.loads((snapshot/"summary.json").read_text()),
              "note": "Only decision-time inputs used. Original outcomes are not counterfactual candidate returns. Past trigger presence alone is not proof of causal attribution."}
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps(dict(counts), ensure_ascii=False))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.expanduser().resolve().is_relative_to(ROOT):
        parser.error("Output must be outside repository")
    replay(args.snapshot.expanduser(), args.evaluation.expanduser(), args.output.expanduser())


if __name__ == "__main__":
    main()
