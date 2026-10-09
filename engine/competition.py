"""Local Competition parameters and exact, unrounded standing calculations."""
from dataclasses import asdict, dataclass
from fractions import Fraction


@dataclass(frozen=True)
class CompetitionConfig:
    small_blind: int
    big_blind: int
    buy_in: int
    initial_bankroll: int
    automatic_rebuy: bool
    max_players: int = 6
    total_rounds: int = 1
    hands_per_match: int = 20
    minimum_hands: int = 20

    def __post_init__(self):
        for key in ("small_blind", "big_blind", "buy_in", "max_players", "total_rounds", "hands_per_match"):
            value = getattr(self, key)
            if type(value) is not int or value <= 0:
                raise ValueError(f"Invalid Competition parameter: {key}")
        for key in ("initial_bankroll", "minimum_hands"):
            if type(getattr(self, key)) is not int or getattr(self, key) < 0:
                raise ValueError(f"Invalid Competition parameter: {key}")
        if not 2 <= self.max_players <= 9 or self.small_blind > self.big_blind:
            raise ValueError("Invalid Competition blinds/player cap")
        if type(self.automatic_rebuy) is not bool:
            raise ValueError("automatic_rebuy must be boolean")

    @classmethod
    def from_summary(cls, summary):
        """Explicitly use a discovered official summary, never example defaults."""
        return cls(small_blind=summary["blinds"]["small"], big_blind=summary["blinds"]["big"],
                   buy_in=summary["buyIn"], initial_bankroll=summary["initialBankroll"],
                   automatic_rebuy=summary["automaticRebuy"], max_players=summary["maxPlayers"],
                   total_rounds=summary.get("totalRounds", 1), hands_per_match=summary.get("handsPerMatch", 20),
                   minimum_hands=summary["standing"]["minimumHands"])

    def to_dict(self):
        return asdict(self)

    @property
    def required_hands(self):
        return (self.total_rounds * self.hands_per_match * 95 + 99) // 100


def exact_bb_per_100(net_result, hands, big_blind):
    return Fraction(net_result * 100, big_blind * hands) if hands else None


def standing(net_result, hands, config):
    rate = exact_bb_per_100(net_result, hands, config.big_blind)
    return {"net_result": net_result, "hands_played": hands,
            "bb_per_100": float(rate) if rate is not None else None,
            "qualification_eligible": hands >= max(config.required_hands, config.minimum_hands)}
