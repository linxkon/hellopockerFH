"""Isolated Sohu iteration 01 strategy entry."""
import importlib.util
from pathlib import Path
import sys

_spec = importlib.util.spec_from_file_location(__name__ + "_semantics", Path(__file__).with_name("semantics.py"))
_semantics = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _semantics
_spec.loader.exec_module(_semantics)
_base = _semantics.load(__name__, ('S3',))
BOT_NAME = "sohu_iter01_c3"
_HAVE_EVAL7 = _base._HAVE_EVAL7


def decide(state):
    return _base.decide(state)
