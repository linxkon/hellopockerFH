"""Persistent spawned worker. Timed-out computations are killed, not awaited."""
import contextlib
import importlib.util
import multiprocessing
import os
import sys
import time


def _worker(connection, path):
    # Strategy stdout/stderr is untrusted and may contain private game facts.
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            import eval7  # required even if a strategy has a degraded fallback
            spec = importlib.util.spec_from_file_location("_competition_strategy", path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            if not callable(getattr(mod, "decide", None)) or getattr(mod, "_HAVE_EVAL7", True) is False:
                raise RuntimeError("Invalid strategy")
            mod.decide({"type": "warmup"})
            connection.send(("ready", None))
        except Exception:
            connection.send(("error", None))
            return
        while True:
            try:
                state = connection.recv()
            except EOFError:
                return
            if state is None:
                return
            try:
                result = mod.decide(state)
                connection.send(("result", result))
            except Exception:
                connection.send(("error", None))


class StrategyWorker:
    def __init__(self, path):
        self.path, self.process, self.connection, self.competition = path, None, None, None
        self.start()

    def start(self, timeout=10.0):
        ctx = multiprocessing.get_context("spawn")
        self.connection, child = ctx.Pipe()
        self.process = ctx.Process(target=_worker, args=(child, self.path), daemon=True)
        self.process.start()
        child.close()
        if not self.connection.poll(timeout) or self.connection.recv()[0] != "ready":
            self.close()
            raise RuntimeError("Strategy prewarm failed; install eval7 and check decide()")

    def decide(self, state, timeout):
        end = time.monotonic() + timeout
        if self.competition not in (None, state["competition_id"]):
            self.close()
        self.competition = state["competition_id"]
        try:
            if self.process is None:
                self.start(timeout=max(0.001, end-time.monotonic()))
            self.connection.send(state)
            remaining = end-time.monotonic()
            if remaining <= 0 or not self.connection.poll(remaining):
                raise TimeoutError("Strategy calculation deadline exceeded")
            kind, result = self.connection.recv()
            if kind != "result":
                raise RuntimeError("Strategy calculation failed")
            return result
        except (EOFError, BrokenPipeError, OSError, TimeoutError, RuntimeError):
            self.close()
            raise

    def close(self):
        if self.process is not None:
            self.process.kill()
            self.process.join(timeout=0.2)
            self.process = None
        if self.connection is not None:
            self.connection.close()
            self.connection = None
