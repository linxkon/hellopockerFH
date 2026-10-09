"""Protocol adaptation of the frozen v16 baseline; legacy source is unchanged."""
import importlib.util
from pathlib import Path
import sys

_path = Path(__file__).resolve().parents[1] / "bot_v16_squeeze_C_metacap" / "bot.py"
_spec = importlib.util.spec_from_file_location(__name__ + "_baseline", _path)
_base = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _base
_spec.loader.exec_module(_base)
BOT_NAME = "sohu_v16_squeeze_C_metacap"
_HAVE_EVAL7 = _base._HAVE_EVAL7
_legacy_reconstruct = _base._reconstruct_hand


def _reconstruct_hand(log):
    if not log or not all("street" in a for a in log):
        return _legacy_reconstruct(log)
    committed, totals, records = {}, {}, []
    previous, current = None, 0
    for entry in log:
        street = entry["street"]
        if street != previous:
            committed, current, previous = {}, 0, street
        seat, act, amount = entry["seat"], entry["action"], entry["amount"]
        own = committed.get(seat, 0)
        if act not in {"small_blind", "big_blind"}:
            records.append({"seat": seat, "street_idx": {"preflop": 0, "flop": 1, "turn": 2, "river": 3}[street],
                            "action": act, "amount": amount, "owed": max(0, current-own),
                            "pot_before": sum(totals.values())})
        paid = amount-own if act in {"raise", "all_in"} else amount if act in {"small_blind", "big_blind", "call"} else 0
        committed[seat] = own + paid
        totals[seat] = totals.get(seat, 0) + paid
        current = max(current, committed[seat])
    return records


_base._reconstruct_hand = _reconstruct_hand


def decide(game_state):
    return _base.decide(game_state)
