"""Twelve serial HTTP matches, each with six independent real client processes."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from competition_client.replay import load_replay
from scripts.evaluate_iteration import quantiles, write_json


def reserve_port():
    with socket.socket() as sock:
        for port in range(20000, 21000):
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No available local arena port")


def match(directory, paths, seed, rules):
    port = reserve_port()
    cmd = [sys.executable, "-m", "local_arena", "init", "--directory", str(directory), "--players", "6",
           "--hands", str(rules["hands_per_match"]), "--port", str(port), "--seed", str(seed)]
    for field in ("small_blind", "big_blind", "buy_in", "initial_bankroll"):
        cmd += ["--"+field.replace("_", "-"), str(rules[field])]
    subprocess.run(cmd, cwd=ROOT, check=True, capture_output=True, text=True, timeout=10)
    processes, logs = [], []
    started = time.monotonic()
    try:
        log = (directory / "server.log").open("x")
        logs.append(log)
        server = subprocess.Popen([sys.executable, "-m", "local_arena", "serve", "--config", str(directory/"server.json")],
                                  cwd=ROOT, stdout=log, stderr=log)
        processes.append(server)
        until = time.monotonic()+10
        while True:
            if time.monotonic() > until or server.poll() is not None:
                raise RuntimeError("Arena startup failed; inspect server.log")
            try:
                with urlopen(f"http://127.0.0.1:{port}/api/competitions", timeout=.2) as response:
                    if response.status == 200:
                        break
            except (URLError, TimeoutError):
                time.sleep(.02)
        for i,path in enumerate(paths):
            client_config = directory / "participants" / f"agent-{i+1}" / "client.json"
            log = (directory / f"client-{i+1}.log").open("x")
            logs.append(log)
            p = subprocess.Popen([sys.executable, "-m", "competition_client", "--config", str(client_config), "--strategy", path],
                                 cwd=ROOT, stdout=log, stderr=log)
            processes.append(p)
        until = time.monotonic()+240
        while any(p.poll() is None for p in processes[1:]):
            if time.monotonic() > until:
                raise RuntimeError("HTTP match exceeded 240 seconds")
            if server.poll() is not None:
                raise RuntimeError("Arena stopped unexpectedly")
            time.sleep(.1)
        report = json.loads((directory/"report.json").read_text())
        failures = []
        if report["outcome"] != "completed" or report["completedHands"] != rules["hands_per_match"]:
            failures.append("incomplete")
        if any(p.returncode != 0 for p in processes[1:]):
            failures.append("client_exit")
        if any(p["handsPlayed"] != rules["hands_per_match"] or p["timeouts"] or p["invalidDecisions"] or p["settledStack"] != 0 for p in report["players"]):
            failures.append("client_reliability")
        hands = [json.loads(line) for line in (directory/"hands.jsonl").open()]
        if any(len(h["starting_stacks"]) != 6 for h in hands):
            failures.append("not_six_players")
        for p in report["players"]:
            agent = p["agentId"]
            net = sum(h["final_stacks"].get(agent,0)-h["starting_stacks"].get(agent,0) for h in hands)
            if net != p["netResult"]:
                failures.append("score_reconstruction")
        if sum(p["netResult"] for p in report["players"]) != 0:
            failures.append("score_conservation")
        replay_summaries, calculation = [], []
        for i in range(6):
            journal = directory / "participants" / f"agent-{i+1}" / "client-state" / "telemetry" / "local_arena" / "replay.sqlite3"
            replay = load_replay(journal)
            replay_summaries.append(replay["summary"])
            if replay["summary"]["source"] != "local_arena" or replay["summary"]["fallback_counts"]:
                failures.append("telemetry_or_fallback")
            calculation.extend(d["calculation_s"] for d in replay["decisions"] if d.get("calculation_s") is not None)
        server.send_signal(signal.SIGTERM)
        server.wait(timeout=10)
        if server.returncode != 0:
            failures.append("server_exit")
        return {"elapsed": time.monotonic()-started, "report": report, "failures": failures,
                "client_exits": [p.returncode for p in processes[1:]], "client_calculation_seconds": quantiles(calculation),
                "replay_summaries": replay_summaries}
    finally:
        for p in reversed(processes):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
        for log in logs:
            log.close()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluation, output = args.evaluation.expanduser().resolve(), args.output.expanduser().resolve()
    if output.is_relative_to(ROOT):
        parser.error("Output must be outside the repository")
    config = json.loads((evaluation/"evaluation.json").read_text())
    if not config["rules"]["automatic_rebuy"]:
        parser.error("This validation configuration requires automatic rebuy")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    write_json(output / "plan.json", {"sources": config["sources"], "rules": config["rules"], "mixes": config["mixes"],
                                    "seats": [0,3], "seed_base": 40000, "command": sys.argv})
    results = []
    for mix, counts in enumerate(config["mixes"]):
        field = [config["sources"][f"H{i+1}"] for i,n in enumerate(counts) for _ in range(n)]
        for order,seat in enumerate((0,3)):
            seed = 40000+1000*mix+order
            opponents = field if order == 0 else list(reversed(field))
            for side in ("C", "B"):
                paths = opponents[:]
                paths.insert(seat, config["sources"][side])
                row = {"mix": mix, "side": side, "seat": seat, "order": order, "seed": seed}
                directory = output / f"P{mix+1}-seat{seat}-{side}"
                try:
                    row.update(match(directory, paths, seed, config["rules"]))
                except Exception as exc:
                    row.update(failures=[type(exc).__name__], error=str(exc))
                results.append(row)
                write_json(output/"results.json", results)
                print(json.dumps({k:row[k] for k in ("mix","side","seat","failures")}), flush=True)
    write_json(output/"summary.json", {"matches": len(results), "successful": sum(not r["failures"] for r in results),
              "failure_counts": dict(sum((Counter(r["failures"]) for r in results),Counter())),
              "elapsed_seconds": quantiles([r["elapsed"] for r in results if "elapsed" in r]),
              "note": "Integration validation only; random Competition IDs affect spot RNG, so this is not paired strategy-return evidence."})
    return int(any(r["failures"] for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
