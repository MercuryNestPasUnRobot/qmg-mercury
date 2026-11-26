"""Conversion factors for AI evaluation; the Core still settles raw VP."""
from dataclasses import dataclass, field
from math import isfinite

from .model import CoreError


STANDARD_SITE_VP = 2

# Base unit, multiplier, and optional coefficient used by older configurations.
LINKED_SCORES = {
    "full_site_round": ("vp", STANDARD_SITE_VP, None),
    "distance_step": ("action", 1, None),
    "unit_removal": ("action", 1, None),
    "unknown_protection": ("action", 1, None),
    "play_opportunity": ("action", 1, None),
    "expansion": ("vp", 1, "expansion_preference"),
    "basic_discard": ("card", 1, "basic_usage_cost"),
    "extra_draw": ("card", 1, None),
    "card_return": ("card", 1, None),
    "future_shortfall": ("vp", 1, "economic_forecast.future_shortfall_value"),
    "requested_discard": ("card", 1, "status_forecast.request_discard_card_value"),
    "forced_discard": ("card", 1, "status_forecast.forced_discard_card_value"),
    "exchange_saved_payment": ("card", 1, "status_forecast.exchange_saved_payment_value"),
    "alternative_status_play": ("action", 1, "status_forecast.alternative_play_action_value"),
    "play_from_zone_rule": ("action", 0.6 / 2, None),
    "play_from_zone_hand": ("action", 0.5, "effect_value_weights.play-from-zone"),
    "search_deck_rule": ("card", 0.35, None),
    "search_deck_hand": ("card", 1, "effect_value_weights.search-deck"),
}

VALUED_ACTIONS = {"BUILD_ARMY", "BUILD_NAVY", "RECRUIT", "LAND_BATTLE", "SEA_BATTLE", "REDEPLOY", "USE_AIR"}
PLAY_OPPORTUNITY_COMMANDS = {"PLAY_CARD", "USE_STATUS", "DISCARD_CARD", "FORFEIT_PLAY"}
ACTION_SUPPORT_EFFECTS = {
    "prevent-removal", "protect-unit-this-turn", "protect-units-this-turn",
    "supply-this-turn", "supply-units-this-turn", "forbid-air-defense-this-turn",
    "disable-triggered-status", "cancel-card-effect", "cancel-economic", "prevent-economic-discard",
}


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
    supply_stability: float | None = None
    linked_values: dict = field(default_factory=dict, hash=False)

    @classmethod
    def from_weights(cls, weights):
        if "scoring_units" not in weights:
            return cls()
        units = weights["scoring_units"]
        required = {"vp", "card", "action", "capture_rounds"}
        if not isinstance(units, dict) or not required <= units.keys() <= required | {"supply_stability"}:
            raise CoreError("Invalid scoring units")
        if any(type(units[key]) not in (float, int) or not isfinite(units[key]) or units[key] <= 0
               for key in ("vp", "card", "action")):
            raise CoreError("Scoring units must be finite and positive")
        if type(units["capture_rounds"]) is not int or units["capture_rounds"] < 0:
            raise CoreError("Capture rounds must be a nonnegative integer")
        stability = units.get("supply_stability", 0)
        if type(stability) not in (float, int) or not isfinite(stability) or stability < 0:
            raise CoreError("Supply stability must be finite and nonnegative")
        overrides = weights.get("scoring_overrides", {})
        if not isinstance(overrides, dict) or any(
            key not in LINKED_SCORES or type(value) not in (int, float) or not isfinite(value) or value < 0
            for key, value in overrides.items()
        ):
            raise CoreError("Invalid scoring overrides")
        linked = {}
        if "scoring_overrides" in weights:
            for key, (base, multiplier, coefficient) in LINKED_SCORES.items():
                if key in overrides:
                    linked[key] = overrides[key]
                    continue
                if coefficient:
                    value = weights
                    for part in coefficient.split("."):
                        value = value.get(part, {}) if isinstance(value, dict) else {}
                    if type(value) not in (float, int):
                        continue
                    multiplier *= value
                linked[key] = multiplier * units[base]
        return cls(**units, enabled=True, linked_values=linked)

    def vp_score(self, amount):
        return amount * self.vp

    def card_score(self, count):
        return count * self.card

    def action_score(self, count):
        return count * self.action

    def linked(self, key, fallback):
        return self.linked_values.get(key, fallback)

    def site_score(self, vp):
        return vp * self.linked("full_site_round", self.vp_score(STANDARD_SITE_VP)) / STANDARD_SITE_VP

    def distance_score(self, steps):
        return steps * self.linked("distance_step", self.action)
