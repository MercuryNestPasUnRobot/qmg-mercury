"""Estimate the value of immediate VP and deck attrition from public information."""
from functools import cached_property
from math import ceil, copysign

from .content_effects import available_options, condition_met, filtered_units, force_id, point_award
from .model import TURN_ORDER, zone


POINT_EFFECTS = {"points", "points-per-unit", "points-per-occupied-space"}
MILL_EFFECTS = {"discard-top", "discard-top-per-unit", "choose-country"}


class EconomicForecast:
    def __init__(self, rules, settings, turn, scores, count, visible_state, winner, *, status_weights=None):
        self.rules, self.settings = rules, settings
        self.scores, self.count, self.visible_state = scores, count, visible_state
        self.margin = rules.settings["victory_margin"]
        self.current = turn["country_index"]
        seats = len(TURN_ORDER)
        total = rules.settings["max_rounds"] * seats
        remaining = max(0, total - (turn["round_id"] - 1) * seats - self.current)
        gap = min(self.gaps("axis"))
        score_pressure = 1 - gap / self.margin if self.margin else 0
        self.pressure = max(1 - remaining / total, score_pressure)
        margin_turns = max(1, ceil(gap * seats / settings["lead_vp_per_round"])) if self.margin else remaining
        self.horizon = min(remaining, margin_turns) if not winner else 0
        self.turn = turn
        self.penalty = max(0, -rules.settings.get("deck_exhaustion_vp", 0))
        self.status_weights = status_weights

    @cached_property
    def statuses(self):
        from .status_scoring import StatusForecast
        return StatusForecast(self, **self.status_weights) if self.status_weights else None

    def gaps(self, team):
        lead = self.scores[team] - self.scores["allies" if team == "axis" else "axis"]
        return max(0, self.margin - lead), max(0, self.margin + lead)

    def points(self, seat, delta, base, *, closeout=False):
        value = delta * base * (1 + self.settings["vp_pressure_bonus"] * self.pressure)
        if closeout and self.closes_game(seat, delta):
            value += copysign(self.settings["closeout_bonus"], delta)
        return value

    def closes_game(self, seat, delta):
        own, enemy = self.gaps(self.rules.team(seat))
        gap = own if delta > 0 else enemy
        return bool(self.margin and 0 < gap <= abs(delta))

    def draws(self, seat):
        seats = len(TURN_ORDER)
        delay = (TURN_ORDER.index(seat) - self.current) % seats
        if delay == 0 and self.turn["phase"] == "Draw":
            delay = seats  # The current Draw has already refilled the hand.
        if delay >= self.horizon:
            return 0
        future = (self.horizon - delay - 1) // seats
        limit = self.rules.settings["hand_limit"]
        normal = min(limit, self.rules.settings["plays_per_turn"])
        spend = normal
        if delay == 0:
            spend = min(normal, self.turn["plays_remaining"]) if self.turn["phase"] in ("Setup", "Play") else 0
        first_refill = min(limit, max(0, limit - self.count(seat, "HAND") + spend))
        return first_refill + future * normal

    def shortfall(self, seat, deck=None):
        return max(0, self.draws(seat) - (self.count(seat, "DECK") if deck is None else deck))

    def future_loss(self, seat):
        return self.settings["future_shortfall_value"] * self.penalty * self.shortfall(seat)

    def mill(self, seat, target, count, base, *, closeout=False):
        deck = self.count(target, "DECK")
        sign = -1 if self.rules.team(seat) == self.rules.team(target) else 1
        immediate = sign * self.penalty * max(0, count - deck)
        future = 0 if self.closes_game(seat, immediate) else (
            self.shortfall(target, max(0, deck - count)) - self.shortfall(target))
        return (self.points(seat, immediate, base, closeout=closeout)
                + sign * self.settings["future_shortfall_value"] * self.penalty * future)

    def effect(self, seat, effect, base, *, closeout=False):
        kind = effect["type"]
        if kind not in POINT_EFFECTS and kind not in MILL_EFFECTS:
            return None
        if effect.get("when") and not condition_met(self.rules, self.visible_state(), seat, effect["when"]):
            return 0
        if kind in POINT_EFFECTS:
            state = None if kind == "points" else self.visible_state()
            recipient, amount = point_award(self.rules, state, seat, effect)
            delta = amount if self.rules.team(seat) == self.rules.team(recipient) else -amount
            return self.points(seat, delta, base, closeout=closeout)
        if kind == "choose-country":
            return max((self.mill(seat, target, effect["count"], base, closeout=closeout)
                        for target in available_options(self.rules, self.visible_state(), seat, effect)), default=0)
        target = force_id(effect["target"], seat)
        if target not in TURN_ORDER:
            return 0  # A trigger-dependent target is only known during resolution.
        count = (effect.get("count", 0) if kind == "discard-top" else
                 effect["countPer"] * len(filtered_units(self.rules, self.visible_state(), seat, effect["filter"])))
        return self.mill(seat, target, count, base, closeout=closeout)
