"""Project engine state into Agent-specific Sohu protocol observations."""
from datetime import datetime, timezone


def timestamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def legal_actions(engine, seat):
    p = engine.players[seat]
    owed = max(0, engine.current_bet - p.bet_this_street)
    actions = [{"type": "fold"}]
    if owed:
        actions.append({"type": "call", "amount": min(owed, p.stack)})
    else:
        actions.append({"type": "check"})
    opponents = [other for other in engine.players if other.seat != seat and other.is_active]
    may_raise = seat not in engine._acted_since_full_raise and bool(opponents)
    minimum = engine.current_bet + engine.min_raise - p.bet_this_street
    if may_raise and p.stack >= minimum:
        actions.append({"type": "raise" if engine.current_bet else "bet",
                        "minAmount": minimum, "maxAmount": p.stack})
    if p.stack > 0 and (p.stack <= owed or may_raise):
        actions.append({"type": "allIn", "amount": p.stack})
    return actions


def legacy_decision(decision, player):
    kind = decision["type"]
    if kind in {"bet", "raise"}:
        return {"action": "raise", "amount": player.bet_this_street + decision["amount"]}
    return {"action": "all_in" if kind == "allIn" else kind}


def hand_snapshot(engine, state, history, seat_map):
    complete = state["type"] == "hand_complete"
    pots = engine._compute_side_pots()
    return {
        "id": engine.hand_id,
        "phase": "showdown" if complete else engine.street,
        "communityCards": [str(c) for c in engine.community_cards],
        "pot": engine.pot,
        "sidePots": [{"amount": p["amount"], "eligibleAgentIds": [x.bot_id for x in p["eligible"]]}
                     for p in pots[1:]],
        "dealerSeatIndex": seat_map[engine.players[engine.dealer_seat].bot_id],
        "actingSeatIndex": None if complete else seat_map[engine.players[state["seat_to_act"]].bot_id],
        "actions": list(history),
        "result": engine.settlement if complete else None,
    }


def table_snapshot(arena, agent_id):
    engine, state = arena.engine, arena.state
    revealed = state.get("revealed_cards", {})
    players = []
    for p in engine.players:
        cards = [str(c) for c in p.hole_cards] if p.bot_id == agent_id else revealed.get(p.bot_id)
        players.append({
            "agentId": p.bot_id, "name": arena.members[p.bot_id]["name"], "avatarUrl": None,
            "seatIndex": arena.seat_map[p.bot_id], "stack": p.stack,
            "presence": "away" if arena.members[p.bot_id]["leaving"] or arena.members[p.bot_id]["timeouts_in_row"] else "present",
            "handState": {"status": "folded" if p.is_folded else "allIn" if p.is_all_in else "active",
                          "holeCards": cards, "currentBet": p.bet_this_street},
        })
    return {"id": arena.table_id, "phase": "active", "blinds": arena.blinds,
            "players": players, "hand": hand_snapshot(engine, state, arena.history, arena.seat_map),
            "recovery": None}
