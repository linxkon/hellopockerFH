"""Fail-closed validation of the fields used for routing and decisions."""
from datetime import datetime
import re


class ProtocolError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ProtocolError(message)


def integer(value, name, minimum=0):
    require(type(value) is int and minimum <= value <= 2**53 - 1,
            f"Invalid integer: {name}")
    return value


def identifier(value, name):
    require(isinstance(value, str) and bool(value.strip()), f"Invalid ID: {name}")
    return value


def deadline(value):
    require(isinstance(value, str), "Missing deadlineAt")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None, "deadlineAt needs timezone")
        return parsed.timestamp()
    except (ValueError, OverflowError) as exc:
        raise ProtocolError("Invalid deadlineAt") from exc


def allowed_actions(actions):
    require(isinstance(actions, list) and bool(actions), "Missing allowedActions")
    found = {}
    for action in actions:
        require(isinstance(action, dict), "Invalid allowed action")
        kind = action.get("type")
        require(kind in {"fold", "check", "call", "allIn", "bet", "raise"}
                and kind not in found, "Unknown or duplicate allowed action")
        keys = {"type"}
        if kind in {"bet", "raise"}:
            keys |= {"minAmount", "maxAmount"}
            lo = integer(action.get("minAmount"), "minAmount", 1)
            hi = integer(action.get("maxAmount"), "maxAmount", 1)
            require(lo <= hi, "Inverted action range")
        elif kind in {"call", "allIn"}:
            keys.add("amount")
            integer(action.get("amount"), "allowed amount", 1)
        require(set(action) == keys, "Invalid allowed action shape")
        found[kind] = action
    return found


def validate_decision(decision, actions):
    allowed = allowed_actions(actions)
    require(isinstance(decision, dict), "Invalid decision")
    kind = decision.get("type")
    require(kind in allowed, "Decision is not allowed")
    keys = {"type"}
    if kind in {"bet", "raise"}:
        keys.add("amount")
        amount = integer(decision.get("amount"), "decision amount", 1)
        require(allowed[kind]["minAmount"] <= amount <= allowed[kind]["maxAmount"],
                "Decision amount outside range")
    require(set(decision) == keys, "Unexpected decision fields")
    return decision


def validate_body(command, body):
    require(isinstance(body, dict), "Request must be an object")
    if command == "leave":
        require(set(body) in ({"tableId"}, {"competitionId"}), "Leave needs exactly one ID")
    else:
        required = {"competitionId"}
        optional = set()
        if command == "observe":
            optional = {"tableId"}
        elif command == "action":
            required |= {"tableId", "actionRequestId", "decision"}
        else:
            require(command == "join", "Unknown command")
        require(required <= set(body) <= required | optional, "Invalid request fields")
    for key in set(body) - {"decision"}:
        identifier(body[key], key)
    if command == "action":
        decision = body["decision"]
        require(isinstance(decision, dict), "Invalid decision")
        kind = decision.get("type")
        require(kind in {"fold", "check", "call", "allIn", "bet", "raise"}, "Unknown decision")
        require(set(decision) == ({"type", "amount"} if kind in {"bet", "raise"} else {"type"}),
                "Invalid decision shape")
        if "amount" in decision:
            integer(decision["amount"], "amount", 1)


def validate_discovery(data, competition_id, expected=None):
    require(isinstance(data, dict) and isinstance(data.get("competitions"), list),
            "Invalid Discovery response")
    matches = [c for c in data["competitions"] if isinstance(c, dict) and c.get("id") == competition_id]
    require(len(matches) == 1, "Configured Competition missing or duplicated")
    item = matches[0]
    require(item.get("status") in {"scheduled", "active", "ended", "cancelled"}, "Invalid Competition status")
    require(item.get("type") in {"playground", "league"}, "Invalid Competition type")
    summary = item.get("summary")
    require(isinstance(summary, dict), "Missing Competition summary")
    for key in ("initialBankroll", "buyIn", "minPlayers", "maxPlayers", "actionTimeoutSeconds"):
        integer(summary.get(key), key, 0 if key == "initialBankroll" else 1)
    require(2 <= summary["minPlayers"] <= summary["maxPlayers"], "Invalid player limits")
    require(30 <= summary["actionTimeoutSeconds"] <= 300, "Invalid action timeout")
    require(type(summary.get("automaticRebuy")) is bool, "Invalid automaticRebuy")
    validate_blinds(summary.get("blinds"))
    standing = summary.get("standing")
    require(isinstance(standing, dict) and standing.get("metric") == "bbPer100", "Invalid standing config")
    integer(standing.get("minimumHands"), "minimumHands")
    if item["type"] == "league":
        for key in ("totalRounds", "handsPerMatch"):
            integer(summary.get(key), key, 1)
    for key, value in (expected or {}).items():
        require(summary.get(key) == value, f"Competition config mismatch: {key}")
    return item


def validate_blinds(blinds):
    require(isinstance(blinds, dict), "Missing blinds")
    small = integer(blinds.get("small"), "small blind", 1)
    big = integer(blinds.get("big"), "big blind", 1)
    require(small <= big, "Invalid blinds")


def cards(value, count=None):
    require(isinstance(value, list) and all(isinstance(c, str) and re.fullmatch(r"[2-9TJQKA][cdhs]", c) for c in value), "Invalid cards")
    require(len(set(value)) == len(value), "Duplicate cards")
    if count is not None:
        require(len(value) == count, "Wrong card count")


def validate_observation(data, competition_id, agent_id=None):
    require(isinstance(data, dict), "Invalid Observation")
    require(data.get("competitionId") == competition_id, "Competition identity changed")
    identifier(data.get("agentId"), "agentId")
    if agent_id is not None:
        require(data["agentId"] == agent_id, "Agent identity changed")
    require(data.get("competitionType") in {"playground", "league"}, "Invalid Competition type")
    require(data.get("competitionStatus") in {"scheduled", "active", "ended", "cancelled"}, "Invalid lifecycle")
    require(data.get("status") in {"idle", "queued", "seated"}, "Invalid participation status")
    integer(data.get("bankroll"), "bankroll")
    standing = data.get("standing")
    require("standing" in data and (standing is None or isinstance(standing, dict)), "Missing/invalid standing")
    # Live Playground returns null before the first hand has settled. Preserve
    # that unknown value; do not synthesize zeros for authoritative scores.
    if standing is not None:
        integer(standing.get("handsPlayed"), "handsPlayed")
        require(type(standing.get("netResult")) is int and abs(standing["netResult"]) <= 2**53-1, "Invalid netResult")
        bb = standing.get("bbPer100")
        require(bb is None if standing["handsPlayed"] == 0 else type(bb) in (int, float) and abs(bb) < float("inf"), "Invalid bbPer100")
        require("rank" in standing, "Missing rank")
        if standing["rank"] is not None:
            integer(standing["rank"], "rank", 1)
    require("table" in data and "actionRequest" in data, "Incomplete Observation")
    table = data["table"]
    require((table is not None) == (data["status"] == "seated"), "Table/status mismatch")
    if table is not None:
        require(isinstance(table, dict), "Invalid Table")
        identifier(table.get("id"), "table.id")
        require(table.get("phase") in {"active", "closed"}, "Invalid table phase")
        validate_blinds(table.get("blinds"))
        players = table.get("players")
        require(isinstance(players, list) and bool(players), "Missing players")
        seats, agents = set(), set()
        for p in players:
            require(isinstance(p, dict), "Invalid player")
            seat = integer(p.get("seatIndex"), "seatIndex")
            agent = identifier(p.get("agentId"), "player agentId")
            require(seat not in seats and agent not in agents, "Duplicate player identity")
            seats.add(seat)
            agents.add(agent)
            integer(p.get("stack"), "stack")
            require("handState" in p, "Missing handState")
            hs = p["handState"]
            if hs is not None:
                require(isinstance(hs, dict) and hs.get("status") in {"active", "folded", "allIn"}, "Invalid handState")
                integer(hs.get("currentBet"), "currentBet")
        require(data["agentId"] in agents, "Agent not at Table")
    request = data["actionRequest"]
    if request is not None:
        require(table is not None and table["phase"] == "active", "Request outside active Table")
        require(isinstance(request, dict), "Invalid actionRequest")
        identifier(request.get("id"), "actionRequest.id")
        deadline(request.get("deadlineAt"))
        allowed_actions(request.get("allowedActions"))
    return data
