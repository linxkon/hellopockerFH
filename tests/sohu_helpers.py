from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures/sohu"
OBSERVATION = json.loads((FIXTURES / "observation.json").read_text())
DISCOVERY = json.loads((FIXTURES / "discovery.json").read_text())
COMPETITION_ID = OBSERVATION["competitionId"]
TABLE_ID = OBSERVATION["table"]["id"]


class Clock:
    def __init__(self):
        self.now = datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp()
        self.elapsed = 0.0

    def time(self):
        return self.now + self.elapsed

    def monotonic(self):
        return self.elapsed

    def sleep(self, duration):
        self.elapsed += duration


def observation(status="seated", lifecycle="active", request=True):
    data = deepcopy(OBSERVATION)
    data["status"], data["competitionStatus"] = status, lifecycle
    if status != "seated":
        data["table"], data["actionRequest"] = None, None
    elif not request:
        data["actionRequest"] = None
    return data


class Worker:
    def __init__(self, action=None):
        self.calls = 0
        self.action = action or {"action": "raise", "amount": 500}

    def decide(self, state, timeout):
        self.calls += 1
        if isinstance(self.action, Exception):
            raise self.action
        return deepcopy(self.action)


class ScriptedHTTP:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def request(self, command, body=None, timeout=None):
        self.requests.append((command, deepcopy(body), timeout))
        if not self.script:
            raise AssertionError(f"Unexpected {command}")
        expected, response = self.script.pop(0)
        assert expected == command, (expected, command)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(body)
        return deepcopy(response)
