"""Iteration 01: state semantics, with independently switchable corrections."""
import importlib.util
from pathlib import Path
import sys

STREETS = {"preflop": 0, "flop": 1, "turn": 2, "river": 3}


def nominal_bb(state):
    """Authoritative nominal BB; legacy logs then the legacy engine's 100."""
    value = state.get("big_blind")
    if type(value) is int and value > 0:
        return value
    for entry in state.get("action_log") or []:
        value = entry.get("amount") if isinstance(entry, dict) and entry.get("action") == "big_blind" else None
        if type(value) is int and value > 0:
            return value
    return 100


def load(name, fixes):
    spec = importlib.util.spec_from_file_location(name + "_baseline", Path(__file__).with_name("baseline.py"))
    base = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = base
    spec.loader.exec_module(base)
    legacy_reconstruct = base._reconstruct_hand

    def reconstruct(log, big_blind=None):
        bb = nominal_bb({"big_blind": big_blind, "action_log": log})
        explicit = bool(log) and all("street" in a for a in log)
        if not explicit and "S1" not in fixes:
            return legacy_reconstruct(log)
        # Old engine states omit street names: preserve the existing boundary
        # inference for those states. Authoritative protocol names take priority.
        inferred = iter(legacy_reconstruct(log)) if not explicit else None
        committed, totals, records = {}, {}, []
        previous, current, min_raise = None, 0, bb
        for entry in log:
            seat, raw, amount = entry["seat"], entry["action"], entry.get("amount") or 0
            blind = raw in {"small_blind", "big_blind"}
            old = None if explicit or blind else next(inferred)
            street = STREETS[entry["street"]] if explicit else 0 if blind else old["street_idx"]
            if street != previous:
                committed, current, previous = {}, 0, street
                min_raise = bb
            own = committed.get(seat, 0)
            paid = max(0, amount - own) if raw in {"raise", "all_in"} else amount if raw in {"small_blind", "big_blind", "call"} else 0
            after = own + paid
            increase = max(0, after - current) if raw in {"raise", "all_in"} else 0
            act = raw
            if "S1" in fixes and raw == "all_in":
                act = "raise" if increase > 0 else "call"
            if not blind:
                records.append({"seat": seat, "street_idx": street, "action": act,
                                "raw_action": raw, "is_all_in": raw == "all_in",
                                "amount": amount, "owed": max(0, current-own),
                                "pot_before": sum(totals.values()), "paid": paid,
                                "bet_before": own, "bet_after": after,
                                "current_before": current, "raise_increment": increase,
                                "is_full_raise": increase > 0 and increase >= min_raise,
                                "is_short_raise": 0 < increase < min_raise})
            committed[seat] = after
            totals[seat] = totals.get(seat, 0) + paid
            current = max(current, after)
            if blind and raw == "big_blind":
                min_raise = bb
                if "S1" in fixes:
                    current = max(current, bb)
            elif increase > 0 and increase >= min_raise:
                min_raise = increase
        return records

    base._reconstruct_hand = reconstruct
    if "S2" in fixes:
        effective = base._effective_stack_bb
        to_chips = base._bb_to_chips

        def bb_log(state):
            # Existing stack and clamping definitions stay in the frozen source.
            return dict(state, action_log=[{"action": "big_blind", "amount": nominal_bb(state)}])

        base._effective_stack_bb = lambda state: effective(bb_log(state))
        base._bb_to_chips = lambda state, count: to_chips(bb_log(state), count)
        base._v16mc_bb_amount = nominal_bb
    if "S3" in fixes:
        def facing_jam(state):
            owed, stack = state.get("amount_owed", 0), state.get("your_stack", 0)
            if owed <= 0:
                return False
            if stack > 0 and owed >= stack:
                return True
            current, cap = state.get("current_bet", 0), state.get("max_raise_to")
            if cap is not None and cap > 0 and current >= cap:
                return True
            own, me = state.get("your_bet_this_street", 0), state.get("seat_to_act")
            return any(p.get("seat") != me and not p.get("is_folded") and p.get("state") != "busted"
                       and (p.get("is_all_in") or p.get("state") == "all_in" or p.get("stack", 1) == 0)
                       and p.get("bet_this_street", 0) > own
                       for p in state.get("players") or [] if isinstance(p, dict))
        base._is_facing_jam = facing_jam
    return base
