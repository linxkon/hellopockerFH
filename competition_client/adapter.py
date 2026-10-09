"""Convert authoritative observations; never infer missing game facts."""
from .protocol import (ProtocolError, allowed_actions, cards, identifier, integer,
                       require, validate_decision, validate_observation)

STREETS = {"preflop": 0, "flop": 1, "turn": 2, "river": 3}
ACTION_NAMES = {"smallBlind": "small_blind", "bigBlind": "big_blind",
                "fold": "fold", "check": "check", "call": "call",
                "bet": "raise", "raise": "raise", "allIn": "all_in"}


def safe_decision(actions):
    allowed = allowed_actions(actions)
    for kind in ("check", "fold"):
        if kind in allowed:
            return {"type": kind}
    raise ProtocolError("No safe check/fold fallback")


def convert_decision(raw, state, actions):
    allowed = allowed_actions(actions)
    if not isinstance(raw, dict):
        return safe_decision(actions)
    kind = {"all_in": "allIn", "raise": "raise", "fold": "fold",
            "check": "check", "call": "call"}.get(raw.get("action"))
    decision = None
    if kind == "raise":
        kind = "raise" if "raise" in allowed else "bet"
        target = raw.get("amount")
        if kind in allowed and type(target) is int and 0 < target <= 2**53 - 1:
            spec = allowed[kind]
            amount = max(spec["minAmount"], min(spec["maxAmount"], target - state["your_bet_this_street"]))
            decision = {"type": kind, "amount": amount}
    elif kind in allowed:
        decision = {"type": kind}
    return validate_decision(decision or safe_decision(actions), actions)


class ObservationAdapter:
    def __init__(self):
        self.hand_key = None
        self.mapping = None

    def convert(self, observation):
        validate_observation(observation, observation.get("competitionId"))
        require(observation["actionRequest"] is not None, "No Action Request")
        table = observation["table"]
        hand = table.get("hand")
        require(isinstance(hand, dict), "Missing Hand")
        identifier(hand.get("id"), "hand.id")
        street = hand.get("phase")
        require(street in STREETS, "Hand is not actionable")
        require(hand.get("result") is None, "Settled Hand has Action Request")
        cards(hand.get("communityCards"), [0, 3, 4, 5][STREETS[street]])
        integer(hand.get("pot"), "pot")
        participants = [p for p in table["players"] if p["handState"] is not None]
        require(len(participants) >= 2, "Not enough Hand participants")
        dealer = integer(hand.get("dealerSeatIndex"), "dealerSeatIndex")
        require(dealer in {p["seatIndex"] for p in participants}, "Dealer not in Hand")
        # Dealer maps to zero. The rest retain physical clockwise seat order.
        ordered = sorted(participants, key=lambda p: (p["seatIndex"] < dealer, p["seatIndex"]))
        key = (observation["competitionId"], table["id"], hand["id"])
        mapping = {p["agentId"]: i for i, p in enumerate(ordered)}
        if key == self.hand_key:
            require(mapping == self.mapping, "Seat mapping changed within Hand")
        else:
            self.hand_key, self.mapping = key, mapping
        by_id = {p["agentId"]: p for p in participants}
        me = by_id.get(observation["agentId"])
        require(me is not None and me["handState"]["status"] == "active", "Agent is not active in Hand")
        cards(me["handState"].get("holeCards"), 2)
        require(not set(me["handState"]["holeCards"]) & set(hand["communityCards"]), "Duplicate visible cards")
        history = hand.get("actions")
        require(isinstance(history, list), "Missing Hand actions")
        committed = {a: 0 for a in mapping}
        totals = {a: 0 for a in mapping}
        log, last_street, blind_seen = [], "preflop", set()
        for action in history:
            require(isinstance(action, dict), "Invalid history entry")
            phase = action.get("street")
            require(phase in STREETS and STREETS[last_street] <= STREETS[phase] <= STREETS[street], "History streets out of order")
            if phase != last_street:
                committed = {a: 0 for a in mapping}
                last_street = phase
            agent, kind = action.get("agentId"), action.get("type")
            require(agent in mapping and kind in ACTION_NAMES, "Unknown history identity/action")
            # Live history omits amount on zero-contribution fold/check.
            # Monetary actions still require an explicit safe integer.
            amount = integer(action.get("amount", 0 if kind in {"fold", "check"} else None), "history amount")
            if kind in {"fold", "check"}:
                require(amount == 0, "Nonzero fold/check contribution")
            if kind in {"smallBlind", "bigBlind"}:
                require(phase == "preflop" and kind not in blind_seen, "Invalid blind history")
                blind_seen.add(kind)
            committed[agent] += amount
            totals[agent] += amount
            log.append({"seat": mapping[agent], "bot_id": agent, "action": ACTION_NAMES[kind],
                        "amount": committed[agent] if kind in {"bet", "raise", "allIn"} else amount,
                        "street": phase, **({"cause": action["cause"]} if "cause" in action else {})})
        require(blind_seen == {"smallBlind", "bigBlind"}, "Incomplete blind history")
        if last_street != street:
            committed = {a: 0 for a in mapping}
        for agent, player in by_id.items():
            require(committed[agent] == player["handState"]["currentBet"], "History/currentBet mismatch")
        require(sum(totals.values()) == hand["pot"], "History/pot mismatch")
        allowed = allowed_actions(observation["actionRequest"]["allowedActions"])
        own_bet = me["handState"]["currentBet"]
        current = max(committed.values())
        if street == "preflop" and sum(p["handState"]["status"] == "active" and p["stack"] > 0
                                       for p in participants) >= 2:
            # A short all-in big blind still establishes the full opening bet
            # for two or more players who can continue betting.
            current = max(current, table["blinds"]["big"])
        owed = current - own_bet
        require(("check" in allowed) == (owed == 0), "check/currentBet mismatch")
        if "call" in allowed:
            require(allowed["call"]["amount"] == min(owed, me["stack"]), "call/currentBet mismatch")
        if "allIn" in allowed:
            require(allowed["allIn"]["amount"] == me["stack"], "allIn/stack mismatch")
        raise_spec = allowed.get("raise") or allowed.get("bet")
        if raise_spec:
            require(raise_spec["maxAmount"] <= me["stack"], "Raise range exceeds stack")
        players = []
        for p in ordered:
            hs = p["handState"]
            players.append({"seat": mapping[p["agentId"]], "bot_id": p["agentId"],
                            "stack": p["stack"], "state": "all_in" if hs["status"] == "allIn" else hs["status"],
                            "is_folded": hs["status"] == "folded", "is_all_in": hs["status"] == "allIn",
                            "bet_this_street": hs["currentBet"], "hole_cards": None})
        return {"type": "action_request", "competition_id": observation["competitionId"],
                "hand_id": hand["id"], "street": street, "seat_to_act": mapping[me["agentId"]],
                "dealer_seat": 0, "pot": hand["pot"], "community_cards": hand["communityCards"],
                "your_cards": me["handState"]["holeCards"], "your_stack": me["stack"],
                "your_bet_this_street": own_bet, "current_bet": current, "amount_owed": owed,
                "call_amount": allowed.get("call", {}).get("amount", 0), "can_check": "check" in allowed,
                "min_raise_to": own_bet + raise_spec["minAmount"] if raise_spec else 0,
                "max_raise_to": own_bet + raise_spec["maxAmount"] if raise_spec else 0,
                "big_blind": table["blinds"]["big"], "small_blind": table["blinds"]["small"],
                "players": players, "action_log": log}
