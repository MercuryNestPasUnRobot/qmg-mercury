"""Estimate the value of immediate VP and deck attrition from public information."""
from functools import cached_property
from math import ceil, copysign

from .content_effects import available_options, condition_met, filtered_units, force_id, point_award
from .hand_retention import cycling_ev
from .model import TURN_ORDER, zone
from .score_units import ScoreUnits


POINT_EFFECTS = {"points", "points-per-unit", "points-per-occupied-space"}
MILL_EFFECTS = {"discard-top", "discard-top-per-unit", "choose-country"}
CARD_RETURN_EFFECTS = {"put-hand-on-deck-bottom", "recover-discard", "return-declared-army-card",
                       "return-played-card", "return-revealed-discard", "shuffle-used-card"}


def self_deck_operations(rules, state, seat, steps, *, choose_branch=None):
    """Describe known consumption of the seat's deck without inspecting its cards."""
    for step in steps:
        op = step.get("operation", step)
        if op["type"] == "DRAW":
            yield "draw", op.get("count", 1)
        elif op["type"] == "CARD_EFFECT":
            effect = op["effect"]
            kind = effect["type"]
            if not condition_met(rules, state, seat, effect.get("when")):
                continue
            if kind == "choose-one" and choose_branch and effect.get("chooser", seat) in (seat, "self"):
                branch = choose_branch(effect["options"])
                yield from self_deck_operations(rules, state, seat, branch["effects"], choose_branch=choose_branch)
            elif kind == "discard-top" and effect["target"] in (seat, "self"):
                yield "discard", effect["count"]
            elif kind == "discard-top-per-unit" and effect["target"] in (seat, "self"):
                yield "discard", effect["countPer"] * len(filtered_units(rules, state, seat, effect["filter"]))


class EconomicForecast:
    def __init__(self, rules, settings, turn, scores, count, visible_state, winner, *, status_weights=None, map_weights=None,
                 units=ScoreUnits(), hand_average=None):
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
        self.map_weights = map_weights
        self.units = units
        self.hand_average = hand_average
        self.hand_averages = {}
        self.averaging = set()

    @property
    def marginal(self):
        return bool(self.map_weights and all(self.map_weights.get(key) for key in (
            "action_distance_scoring", "action_opportunity_scoring", "marginal_opportunity_scoring")))

    @cached_property
    def routes(self):
        from .map_influence import ScoringRoutes
        from .rules import board_units
        state = self.visible_state()

        callback = (lambda: self.statuses) if self.status_weights and self.map_weights.get("action_distance_scoring") else None
        return ScoringRoutes(self.rules, state, board_units(self.rules, state), self.map_weights, status_forecast=callback)

    @cached_property
    def statuses(self):
        from .status_scoring import StatusForecast
        return StatusForecast(self, **self.status_weights) if self.status_weights else None

    @cached_property
    def responses(self):
        from .response_scoring import ResponseForecast
        if self.map_weights and self.map_weights.get("action_distance_scoring") and self.map_weights.get("response_timing_scoring"):
            return ResponseForecast(self)
        return None

    def gaps(self, team):
        lead = self.scores[team] - self.scores["allies" if team == "axis" else "axis"]
        return max(0, self.margin - lead), max(0, self.margin + lead)

    def points(self, seat, delta, base, *, closeout=False):
        value = (self.units.vp_score(delta) if self.units.enabled else
                 delta * base * (1 + self.settings["vp_pressure_bonus"] * self.pressure))
        if not self.units.enabled and closeout and self.closes_game(seat, delta):
            value += copysign(self.units.vp_score(self.settings["closeout_bonus"]), delta)
        return value

    def direct_points(self, seat, operation):
        if operation["type"] == "GAIN_VP":
            recipient, amount = operation.get("score_country") or seat, operation["amount"]
        elif operation["type"] == "CARD_EFFECT" and operation["effect"]["type"] in POINT_EFFECTS:
            effect = operation["effect"]
            state = self.visible_state() if effect["type"] != "points" or effect.get("when") else None
            if not condition_met(self.rules, state, seat, effect.get("when")):
                return 0
            recipient, amount = point_award(self.rules, state, seat, effect)
        else:
            return 0
        return amount if self.rules.team(seat) == self.rules.team(recipient) else -amount

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
        return self.units.vp_score(self.settings["future_shortfall_value"] * self.penalty * self.shortfall(seat))

    def cycle_value(self, seat, count):
        if seat in self.averaging:
            return cycling_ev(None, self.units.card, count, hand_average=self.units.card)
        if seat not in self.hand_averages:
            self.averaging.add(seat)
            try:
                self.hand_averages[seat] = self.hand_average(self, seat)
            finally:
                self.averaging.remove(seat)
        return cycling_ev(None, self.units.card, count, hand_average=self.hand_averages[seat])

    def draw_value(self, seat, count, already_drawn=0):
        draws = min(count, max(0, self.count(seat, "DECK") - already_drawn))
        return self.cycle_value(seat, draws)["cycle_score"] - self.units.vp_score(self.penalty * (count - draws))

    def mill(self, seat, target, count, base, *, closeout=False):
        deck = self.count(target, "DECK")
        sign = -1 if self.rules.team(seat) == self.rules.team(target) else 1
        immediate = sign * self.penalty * max(0, count - deck)
        future = 0 if self.closes_game(seat, immediate) else (
            self.shortfall(target, max(0, deck - count)) - self.shortfall(target))
        return (self.points(seat, immediate, base, closeout=closeout)
                + sign * self.units.vp_score(self.settings["future_shortfall_value"] * self.penalty * future))

    def top_value(self, seen, remaining_rounds, *, seat=None):
        if self.marginal and self.hand_average and seat is not None:
            return self.cycle_value(seat, seen)["cycle_score"]
        if seen == 1:
            return self.units.card_score(self.settings.get("deck_top_look", 0.3))
        return self.units.card_score(max(1, remaining_rounds * 0.25)) if seen > 1 and remaining_rounds else 0

    def return_value(self, seat, effect):
        target = force_id(effect.get("target"), seat)
        kind = effect["type"]
        if kind == "put-hand-on-deck-bottom":
            count = min(effect["max"], self.count(seat, "HAND"))
            return self.units.card_score(count) if count >= effect["min"] else 0
        if kind == "recover-discard":
            available = available_options(self.rules, self.visible_state(), seat, effect)
            count = min(effect.get("count", 1), len(available))
            return self.units.card_score(count if self.rules.team(target) == self.rules.team(seat) else -count)
        if kind == "return-played-card":
            return self.units.card_score(max(0, 1 - effect["discardHandCount"]))
        if kind == "return-revealed-discard":
            return 0  # Recipient and number are known only from the reveal's actual settlement.
        return self.units.card_score(1)

    def effect(self, seat, effect, base, *, closeout=False):
        kind = effect["type"]
        if self.map_weights and self.map_weights.get("action_distance_scoring"):
            if kind in CARD_RETURN_EFFECTS:
                return self.return_value(seat, effect)
            if kind == "order-deck-top":
                seen = min(effect["count"], self.count(seat, "DECK"))
                return self.top_value(seen, self.routes.position.scoring_rounds(seat), seat=seat)
        if kind not in POINT_EFFECTS and kind not in MILL_EFFECTS:
            return None
        if kind in POINT_EFFECTS:
            delta = self.direct_points(seat, {"type": "CARD_EFFECT", "effect": effect})
            return self.points(seat, delta, base, closeout=closeout)
        if effect.get("when") and not condition_met(self.rules, self.visible_state(), seat, effect["when"]):
            return 0
        if kind == "choose-country":
            return max((self.mill(seat, target, effect["count"], base, closeout=closeout)
                        for target in available_options(self.rules, self.visible_state(), seat, effect)), default=0)
        target = force_id(effect["target"], seat)
        if target not in TURN_ORDER:
            return 0  # A trigger-dependent target is only known during resolution.
        count = (effect.get("count", 0) if kind == "discard-top" else
                 effect["countPer"] * len(filtered_units(self.rules, self.visible_state(), seat, effect["filter"])))
        return self.mill(seat, target, count, base, closeout=closeout)
