"""Configuration for one finite, fixed-roster evaluation match."""
from dataclasses import asdict, dataclass
from pathlib import Path
import uuid

from engine.competition import CompetitionConfig

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class ArenaConfig:
    competition_id: str
    participants: list
    name: str = "Internal evaluation"
    small_blind: int = 100
    big_blind: int = 200
    buy_in: int = 20000
    initial_bankroll: int = 400000
    automatic_rebuy: bool = True
    hands: int = 100
    minimum_hands: int = 20
    action_timeout: int = 30
    max_consecutive_timeouts: int = 3
    duration_seconds: int = 86400
    seed: int = 0
    dealer_seat: int = 0
    host: str = "127.0.0.1"
    port: int = 8765

    def __post_init__(self):
        uuid.UUID(self.competition_id)
        if not isinstance(self.participants, list) or not 2 <= len(self.participants) <= 6:
            raise ValueError("A match needs 2–6 participants")
        ids, hashes = set(), set()
        for p in self.participants:
            if (not isinstance(p, dict) or set(p) != {"agent_id", "name", "key_hash"}
                    or not all(isinstance(v, str) and v for v in p.values())):
                raise ValueError("Invalid participant")
            uuid.UUID(p["agent_id"])
            if len(p["key_hash"]) != 64 or any(c not in "0123456789abcdef" for c in p["key_hash"]):
                raise ValueError("Invalid credential hash")
            ids.add(p["agent_id"])
            hashes.add(p["key_hash"])
        if len(ids) != len(self.participants) or len(hashes) != len(self.participants):
            raise ValueError("Participant identities and credentials must be distinct")
        self.competition_config()
        if self.initial_bankroll < self.buy_in:
            raise ValueError("Initial bankroll must cover buy-in")
        for key in ("hands", "action_timeout", "max_consecutive_timeouts", "duration_seconds", "port"):
            if type(getattr(self, key)) is not int or getattr(self, key) < 1:
                raise ValueError(f"Invalid {key}")
        if not 30 <= self.action_timeout <= 300 or self.port > 65535:
            raise ValueError("Action timeout must be 30–300 seconds; invalid port")
        if type(self.seed) is not int or type(self.dealer_seat) is not int or not 0 <= self.dealer_seat < len(ids):
            raise ValueError("Invalid seed/dealer seat")
        if not isinstance(self.name, str) or not self.name or not isinstance(self.host, str) or not self.host:
            raise ValueError("Invalid name/host")
        # All protocol amounts must fit JavaScript safe integers.
        if len(ids) * self.initial_bankroll > 2**53 - 1:
            raise ValueError("Bankroll exceeds protocol amount range")

    def competition_config(self):
        return CompetitionConfig(self.small_blind, self.big_blind, self.buy_in,
                                 self.initial_bankroll, self.automatic_rebuy,
                                 len(self.participants), 1, self.hands, self.minimum_hands)

    def to_dict(self):
        return asdict(self)


def runtime_path(path):
    path = Path(path).expanduser().resolve()
    if path == ROOT or path.is_relative_to(ROOT):
        raise ValueError("Arena credentials and runtime data must be outside the repository")
    return path
