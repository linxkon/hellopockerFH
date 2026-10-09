"""Read-only replay and observed-player statistics; never contacts a server."""
import argparse
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import sqlite3

from .config import ROOT
from .protocol import require
from .replay_store import private_directory, private_fd, source_for


def _money_by_agent(items):
    totals = Counter()
    for item in items:
        totals[item["agentId"]] += item["amount"]
    return dict(totals)


def _request_key(body):
    return (body.get("competition_id"), body.get("table_id"), body.get("request_id"))


def _finish_hand(record):
    hand = record["hand"]
    actions = hand.get("actions", [])
    contributions = Counter()
    for action in actions:
        contributions[action["agentId"]] += action.get("amount", 0)
    result = hand.get("result")
    record["settled"] = result is not None
    record["coverage"] = "settlement_observed" if result is not None else "settlement_not_observed"
    record["contributions"] = dict(contributions)
    for agent in contributions:
        record["players"].setdefault(agent, {"agentId": agent, "handState": None})
    record["net_result"] = None
    if result is not None:
        awards = _money_by_agent(result.get("payouts", []))
        returned = _money_by_agent(result.get("returnedBets", []))
        record["net_result"] = {
            agent: awards.get(agent, 0) + returned.get(agent, 0) - contributions.get(agent, 0)
            for agent in record["players"]}
    # Unknown settlement is never silently counted as a loss or zero profit.
    return record


def player_statistics(hands):
    stats = {}
    for record in hands:
        actions = record["hand"].get("actions", [])
        grouped = defaultdict(list)
        for action in actions:
            grouped[action["agentId"]].append(action)
        for agent, player in record["players"].items():
            st = stats.setdefault(agent, {"agent_id": agent, "name": player.get("name"),
                "observed_hands": 0, "settled_hands": 0, "net_chips_on_settled_hands": 0,
                "actions": Counter(), "actions_by_street": defaultdict(Counter),
                "timeout_actions": 0, "leave_actions": 0, "preflop_samples": 0,
                "vpip_hands": 0, "pfr_hands": 0, "public_showdowns": 0,
                "amount_contributed": 0, "net_bb_on_settled_hands": 0,
                "faced_bet": 0, "fold_vs_bet": 0, "call_vs_bet": 0, "raise_vs_bet": 0})
            st["name"] = player.get("name", st["name"])
            st["observed_hands"] += 1
            own_actions = grouped[agent]
            for action in own_actions:
                st["actions"][action["type"]] += 1
                st["actions_by_street"][action["street"]][action["type"]] += 1
                st["timeout_actions"] += action.get("cause") == "timeout"
                st["leave_actions"] += action.get("cause") == "leave"
            committed, current, street = Counter(), 0, None
            for action in actions:
                if action["street"] != street:
                    committed, current, street = Counter(), 0, action["street"]
                who = action["agentId"]
                owed = max(0, current - committed[who])
                target = committed[who] + action.get("amount", 0)
                if who == agent and owed > 0 and action["type"] not in {"smallBlind", "bigBlind"} and not action.get("cause"):
                    st["faced_bet"] += 1
                    st["fold_vs_bet"] += action["type"] == "fold"
                    st["call_vs_bet"] += action["type"] == "call" or (action["type"] == "allIn" and target <= current)
                    st["raise_vs_bet"] += action["type"] == "raise" or (action["type"] == "allIn" and target > current)
                committed[who] = target
                current = max(current, target)
            st["amount_contributed"] += record["contributions"].get(agent, 0)
            pf = [a for a in own_actions if a["street"] == "preflop"]
            hs = player.get("handState") or {}
            # A partial preflop observation cannot establish that a player never
            # entered/raised. Only use completed preflop samples as denominators.
            pf_complete = record["settled"] or record["hand"].get("phase") != "preflop" or hs.get("status") in {"folded", "allIn"}
            if pf_complete:
                st["preflop_samples"] += 1
                st["vpip_hands"] += any(a["type"] in {"call", "bet", "raise", "allIn"} and a.get("amount", 0) > 0 for a in pf)
                # A short all-in call is VPIP, but is not a preflop raise.
                committed, current = Counter(), 0
                raised = False
                for action in actions:
                    if action["street"] != "preflop":
                        continue
                    target = committed[action["agentId"]] + action.get("amount", 0)
                    if action["agentId"] == agent:
                        raised |= action["type"] in {"bet", "raise"} or (action["type"] == "allIn" and target > current)
                    committed[action["agentId"]] = target
                    current = max(current, target)
                st["pfr_hands"] += raised
            if record["settled"]:
                st["settled_hands"] += 1
                st["net_chips_on_settled_hands"] += record["net_result"].get(agent, 0)
                st["net_bb_on_settled_hands"] += record["net_result"].get(agent, 0) / record["blinds"]["big"]
                if agent != record["hero_agent_id"] and hs.get("holeCards"):
                    st["public_showdowns"] += 1
    for st in stats.values():
        st["actions"] = dict(st["actions"])
        st["actions_by_street"] = {street: dict(counts) for street, counts in st["actions_by_street"].items()}
        n = st["preflop_samples"]
        st["vpip"] = st["vpip_hands"] / n if n else None
        st["pfr"] = st["pfr_hands"] / n if n else None
        st["fold_to_bet"] = st["fold_vs_bet"] / st["faced_bet"] if st["faced_bet"] else None
        st["bb_per_100_on_observed_settled_hands"] = st["net_bb_on_settled_hands"] / st["settled_hands"] * 100 if st["settled_hands"] else None
    return stats


def load_replay(path, hand_id=None, include_snapshots=False):
    path = Path(path).expanduser().resolve()
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")  # consistent export while the live driver appends
        sessions = [json.loads(row[0]) for row in db.execute("SELECT manifest FROM sessions ORDER BY rowid")]
        sources = {s["source"] for s in sessions}
        require(len(sources) == 1, "Replay must contain exactly one log source")
        source = next(iter(sources))
        all_events = [dict(row) for row in db.execute("SELECT * FROM events ORDER BY id")]
        for event in all_events:
            event["source"] = source
            event["body"] = json.loads(event["body"])
        rows = [dict(row, visibility="agent_observation") for row in db.execute("SELECT * FROM observations ORDER BY id")]
        for event in all_events:
            if event["kind"] == "public_table_snapshot":
                body = event["body"]
                rows.append({"received_at": event["recorded_at"], "run_id": event["run_id"],
                    "visibility": "public_table_stream", "body": json.dumps({
                        "competitionId": body["competition_id"], "agentId": body["hero_agent_id"], "table": body["table"]})})
        rows.sort(key=lambda row: row["received_at"])
        hands, latest, observation_count, public_snapshot_count = {}, None, 0, 0
        for row in rows:
            obs = json.loads(row["body"])
            if row["visibility"] == "agent_observation":
                latest, observation_count = obs, observation_count + 1
            else:
                public_snapshot_count += 1
            table = obs.get("table") or {}
            hand = table.get("hand") or {}
            if not hand.get("id") or (hand_id and hand["id"] != hand_id):
                continue
            key = (obs["competitionId"], table["id"], hand["id"])
            if key not in hands:
                hands[key] = {"source": source, "competition_id": key[0], "table_id": key[1],
                    "hand_id": key[2], "hero_agent_id": obs["agentId"], "run_ids": [],
                    "first_seen_at": row["received_at"], "players": {}, "snapshot_count": 0,
                    "agent_snapshot_count": 0, "public_snapshot_count": 0,
                    "initial_observation_phase": hand.get("phase"), "blinds": table.get("blinds")}
                paid = _money_by_agent([dict(a, amount=a.get("amount", 0)) for a in hand.get("actions", [])])
                hands[key]["starting_stacks_inferred"] = {
                    p["agentId"]: p["stack"] + paid.get(p["agentId"], 0)
                    for p in table.get("players", []) if p.get("handState") is not None
                } if hand.get("result") is None else None
                if include_snapshots:
                    hands[key]["snapshots"] = []
            record = hands[key]
            record["last_seen_at"] = row["received_at"]
            record["snapshot_count"] += 1
            counter = "agent_snapshot_count" if row["visibility"] == "agent_observation" else "public_snapshot_count"
            record[counter] += 1
            if row["run_id"] not in record["run_ids"]:
                record["run_ids"].append(row["run_id"])
            # Preserve revealed cards if a later snapshot clears handState.
            for player in table.get("players", []):
                if player.get("handState") is None:
                    continue
                previous = record["players"].get(player["agentId"], {})
                cards = (previous.get("handState") or {}).get("holeCards")
                player = dict(player)
                player["handState"] = dict(player["handState"])
                if not player["handState"].get("holeCards") and cards:
                    player["handState"]["holeCards"] = cards
                record["players"][player["agentId"]] = player
            # Every server observation carries this hand's complete public line.
            # Prefer the most complete line/result, including across restarts.
            previous = record.get("hand", {})
            if len(hand.get("actions", [])) >= len(previous.get("actions", [])):
                if previous.get("result") is None or hand.get("result") is not None:
                    record["hand"] = hand
            if include_snapshots:
                snapshot = {"received_at": row["received_at"], "run_id": row["run_id"], "visibility": row["visibility"]}
                if row["visibility"] == "agent_observation":
                    snapshot["observation"] = obs
                else:
                    snapshot["table"] = table
                record["snapshots"].append(snapshot)
        records = [_finish_hand(h) for h in hands.values()]
        selected_requests = set()
        events = []
        for event in all_events:
            if event["kind"] == "decision" and (hand_id is None or event["body"].get("hand_id") == hand_id):
                selected_requests.add(_request_key(event["body"]))
        for event in all_events:
            if hand_id is None or event["body"].get("hand_id") == hand_id or _request_key(event["body"]) in selected_requests:
                events.append(event)
        decisions, pending = [], {}
        for event in events:
            if event["kind"] == "decision":
                decision = dict(event["body"], source=source, run_id=event["run_id"], recorded_at=event["recorded_at"], outcome=None)
                decisions.append(decision)
                pending[_request_key(event["body"])] = decision
            elif event["kind"] == "action_outcome":
                decision = pending.pop(_request_key(event["body"]), None)
                if decision is not None:
                    decision["outcome"] = event["body"]
        hero_stack = None
        if latest and latest.get("table"):
            hero_stack = next((p["stack"] for p in latest["table"]["players"] if p["agentId"] == latest["agentId"]), None)
        summary = {"schema_version": 1, "source": source, "runs": len(sessions),
            "observation_count": observation_count, "public_snapshot_count": public_snapshot_count, "hands_observed": len(records),
            "hands_with_settlement": sum(h["settled"] for h in records),
            "hands_without_settlement": sum(not h["settled"] for h in records),
            "decision_records": len(decisions), "unique_decision_requests": len(selected_requests),
            "fallback_counts": dict(Counter(d["fallback"] for d in decisions if d.get("fallback"))),
            "player_statistics": player_statistics(records),
            "latest_bankroll_off_table": latest.get("bankroll") if latest else None,
            "latest_hero_stack_on_table": hero_stack,
            "latest_server_standing": latest.get("standing") if latest else None,
            "coverage_note": "Only received agent observations and public table snapshots are available. Stream reconnect gaps and missing settlements remain unknown; rates use observed samples. No chat is recorded."}
        return {"summary": summary, "sessions": sessions, "hands": records, "decisions": decisions, "events": events}
    finally:
        db.close()


def export_replay(report, directory):
    directory = Path(directory).expanduser().resolve()
    require(not directory.is_relative_to(ROOT), "Replay exports must be outside repository")
    directory = private_directory(directory)
    # A separate export directory prevents accidental overwrite of an older run.
    require(not any(directory.iterdir()), "Export directory must be empty")
    for name in ("summary", "sessions", "hands", "decisions", "events"):
        jsonl = name in {"hands", "decisions", "events"}
        path = directory / (name + (".jsonl" if jsonl else ".json"))
        with os.fdopen(private_fd(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "w", encoding="utf-8") as handle:
            if jsonl:
                for record in report[name]:
                    handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
            else:
                json.dump(report[name], handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--config", help="Locate the journal without reading credentials or joining")
    group.add_argument("--database")
    parser.add_argument("--hand-id")
    parser.add_argument("--include-snapshots", action="store_true")
    parser.add_argument("--output", help="New private export directory outside the repository")
    args = parser.parse_args(argv)
    try:
        if args.config:
            from types import SimpleNamespace
            config = json.loads(Path(args.config).expanduser().read_text())
            config.setdefault("base_url", "https://poker.bang.sohu.com")
            config.setdefault("state_dir", "~/.local/state/fullhouse-sohu")
            config = SimpleNamespace(**config)
            path = Path(config.state_dir).expanduser() / "telemetry" / source_for(config) / "replay.sqlite3"
        else:
            path = Path(args.database).expanduser()
        require(path.is_file(), "Replay journal not found; recording starts after restarting the updated client")
        report = load_replay(path, args.hand_id, args.include_snapshots)
        if args.output:
            export_replay(report, args.output)
        print(json.dumps(report["hands"] if args.hand_id else report["summary"], ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        from .http_client import redact
        parser.exit(1, redact(str(exc)) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
