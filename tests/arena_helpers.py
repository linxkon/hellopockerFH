import uuid

from local_arena.config import ArenaConfig
from local_arena.credentials import fingerprint


class FakeClock:
    def __init__(self):
        self.elapsed = 0

    def time(self):
        return 1791532800 + self.elapsed

    def monotonic(self):
        return self.elapsed


def configuration(players=2, **kwargs):
    participants = [{"agent_id": str(uuid.uuid4()), "name": f"Bot {i}",
                     "key_hash": fingerprint(f"sk_local_test_{i}")}
                    for i in range(players)]
    return ArenaConfig(str(uuid.uuid4()), participants, **kwargs)


def join_all(arena):
    for agent in arena.members:
        arena.handle("join", agent, {"competitionId": arena.config.competition_id})


def action_body(arena, agent, decision):
    observation = arena.observation(agent)
    return {"competitionId": arena.config.competition_id, "tableId": arena.table_id,
            "actionRequestId": observation["actionRequest"]["id"], "decision": decision}


def actor(arena):
    return arena.engine.players[arena.state["seat_to_act"]].bot_id
