"""Conversion factors for AI evaluation; the Core still settles raw VP."""
from dataclasses import dataclass
from math import isfinite

from .model import CoreError


VALUED_ACTIONS = {"BUILD_ARMY", "BUILD_NAVY", "RECRUIT", "LAND_BATTLE", "SEA_BATTLE", "REDEPLOY", "USE_AIR"}


def equivalent_action(effect, seat):
    if effect["type"] in ("build", "recruit", "choose-space", "battle") and effect.get("kind", "army") in ("army", "navy"):
        from .content_effects import action_for
        return action_for(effect, seat)["type"]
    return None


@dataclass(frozen=True)
class ScoreUnits:
    vp: float = 1
    card: float = 1
    action: float = 2
    capture_rounds: int = 0
    enabled: bool = False

    @classmethod
    def from_weights(cls, weights):
        if "scoring_units" not in weights:
            return cls()
        units = weights["scoring_units"]
        if not isinstance(units, dict) or units.keys() != {"vp", "card", "action", "capture_rounds"}:
            raise CoreError("Invalid scoring units")
        if any(type(units[key]) not in (float, int) or not isfinite(units[key]) or units[key] <= 0
               for key in ("vp", "card", "action")):
            raise CoreError("Scoring units must be finite and positive")
        if type(units["capture_rounds"]) is not int or units["capture_rounds"] < 0:
            raise CoreError("Capture rounds must be a nonnegative integer")
        return cls(**units, enabled=True)

    def vp_score(self, amount):
        return amount * self.vp

    def card_score(self, count):
        return count * self.card

    def action_score(self, count):
        return count * self.action
