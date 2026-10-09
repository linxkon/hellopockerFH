"""Replay smoke decision streams into independent B/B/C histories."""
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


def audit(evaluation, traces, output):
    config = json.loads((evaluation/"evaluation.json").read_text())
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    counts = Counter()
    previous, modules, loaded, past = None, {}, set(), Counter()
    with (output/"differences.jsonl").open("x") as differences:
        for line in traces.open():
            record = json.loads(line)
            context = record["context"]
            key = json.dumps(context, sort_keys=True)
            state = record["state"]
            if key != previous:
                for name in loaded:
                    sys.modules.pop(name, None)
                before = set(sys.modules)
                modules = {}
                for label in ("B", "B_repeat", "C1", "C2", "C3", "C"):
                    decide = backtest.load_decide(config["sources"]["B" if label == "B_repeat" else label])
                    modules[label] = sys.modules[decide.__module__]
                    decide({"type": "warmup"})
                loaded = {name for name in set(sys.modules)-before if name.startswith("_bt_bot_")}
                previous, past = key, Counter()
                counts["streams"] += 1
            old, new = modules["B"]._base, modules["C"]._base
            triggers, semantic = [], {}
            for label,helper in (("S1","_preflop_history"),("S2","_effective_stack_bb"),("S3","_is_facing_jam")):
                a, b = getattr(old,helper)(state), getattr(new,helper)(state)
                semantic[label] = {"B":a,"C":b}
                changed = a != b
                counts[label+"_helper_differences"] += int(changed)
                if label == "S1":
                    # Even raising jams change class counters (raises + allins).
                    changed |= any(e["action"] == "all_in" for e in state["action_log"])
                if changed:
                    counts[label] += 1
                    past[label] += 1
                    triggers.append(label)
            actions = {label: mod.decide(deepcopy(state)) for label,mod in modules.items()}
            if "S2" in triggers:
                semantic["blind_and_size"] = {
                    label:{"nominal_bb":base._v16mc_bb_amount(state),"three_bb_raise_to":base._bb_to_chips(state,3)}
                    for label,base in (("B",old),("C",new))}
            profile_changes = {}
            for player in old.PLAYER_STATS.keys() | new.PLAYER_STATS.keys():
                fields = {}
                for field in ("raises","calls","allins","pfr_yes","true_pfr_yes","raise_vs_bet","call_vs_bet",
                              "flop_aggr","turn_aggr","river_aggr"):
                    a,b = old.PLAYER_STATS.get(player,{}).get(field,0),new.PLAYER_STATS.get(player,{}).get(field,0)
                    if a != b:
                        fields[field]={"B":a,"C":b}
                if fields:
                    profile_changes[player]=fields
            counts["decisions"] += 1
            counts["B_repeat_differences"] += int(actions["B"] != actions["B_repeat"])
            actual = "B" if context["side"] == "B" else context["variant"]
            counts["actual_reproduction_differences"] += int(actions[actual] != record["action"])
            changed = []
            for label in ("C1","C2","C3","C"):
                if actions[label] != actions["B"]:
                    changed.append(label)
                    counts[label+"_differences"] += 1
                    permitted = {"C1":{"S1"},"C2":{"S2"},"C3":{"S3"},"C":{"S1","S2","S3"}}[label]
                    if not permitted & past.keys():
                        counts["unexplained_differences"] += 1
            if triggers or changed or actions["B"] != actions["B_repeat"]:
                differences.write(json.dumps({"context":context,"state":state,"triggers":triggers,
                                              "semantic":semantic,"profile_changes":profile_changes,
                                              "past_triggers":dict(past),"actions":actions})+"\n")
    for name in loaded:
        sys.modules.pop(name,None)
    (output/"summary.json").write_text(json.dumps({"counts":dict(counts),"traces":str(traces),
                 "note":"Changed behavior needs inspection of the saved state and original trigger; no counterfactual returns are inferred."},indent=2)+"\n")
    print(json.dumps(dict(counts)))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation",type=Path,required=True)
    parser.add_argument("--traces",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    if args.output.expanduser().resolve().is_relative_to(ROOT):
        parser.error("Output must be outside repository")
    audit(args.evaluation.expanduser(),args.traces.expanduser(),args.output.expanduser())


if __name__ == "__main__":
    main()
