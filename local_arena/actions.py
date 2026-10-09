"""Causal action tokens and accepted-body idempotency."""
import json
import uuid


class ActionLedger:
    def __init__(self):
        self.accepted = {}

    @staticmethod
    def token():
        return "action:" + str(uuid.uuid4())

    @staticmethod
    def canonical(body):
        return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def duplicate(self, agent_id, body):
        key = (agent_id, body["tableId"], body["actionRequestId"])
        old = self.accepted.get(key)
        if old is None:
            return None
        return old == self.canonical(body)

    def commit(self, agent_id, body):
        self.accepted[(agent_id, body["tableId"], body["actionRequestId"])] = self.canonical(body)
