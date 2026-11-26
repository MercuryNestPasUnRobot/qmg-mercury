"""Deterministic VP-equivalent diagnostics, with bounded within-card choice search."""
from copy import deepcopy
from functools import cache
import json
from math import isfinite
from pathlib import Path
from time import perf_counter

from .api import CoreAPI, observed_position, simulation_from_observation
from .definitions import CONSTRUCTION, RuleSet
from .economic_scoring import EconomicForecast
from .engine import CommandProcessor, ResolutionBudgetExceeded
from .hand_retention import cycling_ev, score_retention
from .map_influence import ScoringRoutes
from .model import CoreError, TURN_ORDER, PHASES, zone, digest
from .recruitment import recruitment_play
from .operations import ScoreResolver, outcome
from .reallocation_scoring import extra_refill, payment_plans, price
from .response_scoring import is_delayed
from .status_scoring import STATUS_FORECAST_DEFAULTS, ability_airdrop, is_airdrop, is_status, verify_status
from .rules import LegalActionGenerator, MapGraph, SupplyResolver, board_units
from .score_units import ACTION_SUPPORT_EFFECTS, PLAY_OPPORTUNITY_COMMANDS, ScoreUnits, VALUED_ACTIONS, equivalent_action
from .settlement_scoring import PositionValue, SettlementScorer


class ScoreEvaluator:
    def __init__(self, rules, weights):
        self.rules = RuleSet({**rules.data, "initial_tokens": []}, projected=True)
        self.weights = deepcopy(weights)
        self.units = ScoreUnits.from_weights(weights)
        self.action_distance = weights.get("action_distance_scoring", False)
        opportunity = weights.get("action_opportunity_scoring", False)
        if type(opportunity) is not bool:
            raise CoreError("Invalid action opportunity scoring setting")
        self.action_opportunity = opportunity and self.action_distance
        marginal = weights.get("marginal_opportunity_scoring", False)
        if type(marginal) is not bool:
            raise CoreError("Invalid marginal opportunity scoring setting")
        self.marginal_opportunity = marginal and self.action_opportunity
        if type(weights.get("response_timing_scoring", False)) is not bool:
            raise CoreError("Invalid response timing scoring setting")
        if type(weights.get("stacked_site_scoring", False)) is not bool:
            raise CoreError("Invalid stacked site scoring setting")
        if type(self.action_distance) is not bool or (self.action_distance and
                (not self.units.enabled or "route_forecast" not in weights or "economic_forecast" not in weights)):
            raise CoreError("Action distance scoring requires standard units, routes and economic forecasting")
        settlement_scoring = weights.get("settlement_scoring", False)
        if type(settlement_scoring) is not bool or (settlement_scoring and not self.units.enabled):
            raise CoreError("Settlement scoring requires standard units")
        expansion = weights.get("expansion_preference", 0)
        if type(expansion) not in (int, float) or not isfinite(expansion) or expansion < 0:
            raise CoreError("Invalid expansion preference")
        self.settlement = (SettlementScorer(self.units, expansion, location_weights=self.weights if self.action_opportunity else None,
                                            economy_factory=self.economic_from_state if self.marginal_opportunity else None)
                           if settlement_scoring else None)
        self.seat_forecast = self.units.enabled or weights.get("seat_forecast", False)
        numbers = [*weights["distance_weights"], *weights["supply_weights"].values(),
                   *weights["military_weights"].values(), *weights["card_value_weights"].values(),
                   *weights["resource_penalties"].values(), weights["time_discount"], weights["saturation"],
                   weights["expansion_weight"], weights["threat_weight"], weights["unusable_card_weight"]]
        numbers.append(weights.get("basic_usage_cost", 0))
        if type(weights.get("seat_forecast", False)) is not bool:
            raise CoreError("Invalid seat forecast setting")
        if type(weights.get("construction_search_width", 0)) is not int or weights.get("construction_search_width", 0) < 0:
            raise CoreError("Invalid construction search width")
        if type(weights.get("mirrored_attacks", False)) is not bool or (
                weights.get("mirrored_attacks") and "frontier_scoring" not in weights):
            raise CoreError("Mirrored attacks require frontier scoring")
        if "effect_value_weights" in weights:
            effect_weights = weights["effect_value_weights"]
            if not isinstance(effect_weights, dict) or not {"default", "points"} <= set(effect_weights):
                raise CoreError("Effect values require default and points weights")
            numbers.extend(effect_weights.values())
        if "hand_retention" in weights:
            retention = weights["hand_retention"]
            if not isinstance(retention, dict) or set(retention) != {
                    "slot_cost", "basic_floor", "duplicate_penalty", "basic_congestion"}:
                raise CoreError("Invalid hand retention settings")
            numbers.extend(retention.values())
        if "route_forecast" in weights:
            route = weights["route_forecast"]
            if (not isinstance(route, dict) or set(route) != {
                    "nearest_sites", "distance_tail_decay", "friendly_unit", "enemy_unit"}
                    or type(route["nearest_sites"]) is not int or route["nearest_sites"] < 1
                    or not weights["distance_weights"]):
                raise CoreError("Invalid route forecast settings")
            numbers.extend(route.values())
        self.map_weights = None
        if "frontier_scoring" in weights:
            frontier = weights["frontier_scoring"]
            required = {"near_enemy", "held_forecast", "friendly_target", "battle_target", "paper_fraction", "paper_cap"}
            if (not isinstance(frontier, dict) or not required <= frontier.keys() or
                    frontier.keys() - required - {"build_friendly_neighbor", "direct_site_income"} or "route_forecast" not in weights
                    or "economic_forecast" not in weights):
                raise CoreError("Invalid frontier scoring settings")
            numbers.extend(frontier.values())
            self.map_weights = self.weights
        if "economic_forecast" in weights:
            economy = weights["economic_forecast"]
            required_economy = {"vp_pressure_bonus", "closeout_bonus", "lead_vp_per_round", "future_shortfall_value"}
            if (not isinstance(economy, dict) or not required_economy <= economy.keys()
                    or economy.keys() - required_economy - {"deck_top_look"}):
                raise CoreError("Invalid economic forecast settings")
            numbers.extend(economy.values())
        if any(type(n) not in (int, float) or not isfinite(n) or n < 0 for n in numbers) or not 0 <= weights["time_discount"] <= 1:
            raise CoreError("Evaluation weights must be finite nonnegative values")
        if weights["saturation"] <= 0 or type(weights["horizon"]) is not int or not 1 <= weights["horizon"] <= 20:
            raise CoreError("Invalid planning horizon or saturation")
        if self.map_weights and (frontier["near_enemy"] < route["enemy_unit"] or
                                 frontier["friendly_target"] > 1 or frontier["held_forecast"] < 1 or
                                 not 0 < frontier.get("build_friendly_neighbor", 1) <= 1):
            raise CoreError("Invalid frontier influence weights")
        if "hand_retention" in weights and not (0 <= retention["slot_cost"] < retention["basic_floor"]):
            raise CoreError("Basic reserve must exceed the hand slot cost")
        if "route_forecast" in weights and not (
                0 < route["distance_tail_decay"] < 1 and 0 < route["friendly_unit"] < 1 < route["enemy_unit"]):
            raise CoreError("Invalid route influence weights")
        if "economic_forecast" in weights and not (
                economy["lead_vp_per_round"] > 0 and 0 <= economy["future_shortfall_value"] <= 1):
            raise CoreError("Invalid economic forecast weights")
        if type(weights["max_simulation_nodes"]) is not int or not 1 <= weights["max_simulation_nodes"] <= 10000:
            raise CoreError("Invalid simulation budget")
        self.status_weights = None
        if "status_held_factor" in weights:
            factor = weights["status_held_factor"]
            if "economic_forecast" not in weights or type(factor) not in (int, float) or not 0 <= factor <= 1:
                raise CoreError("Invalid status forecast settings")
            coefficients = weights.get("status_forecast", {})
            if (not isinstance(coefficients, dict) or coefficients.keys() - STATUS_FORECAST_DEFAULTS.keys() or any(
                    type(value) not in (int, float) or not isfinite(value) or value < 0 or
                    (type(STATUS_FORECAST_DEFAULTS[key]) is int and type(value) is not int)
                    for key, value in coefficients.items()) or coefficients.get("unknown_hand_scale", 3) <= 0):
                raise CoreError("Invalid status forecast coefficients")
            self.status_weights = {"discount": weights["time_discount"], "held_factor": factor,
                                   **({"coefficients": coefficients} if coefficients else {})}
            for card in rules.cards.values():
                if is_status(card):
                    verify_status(rules, card)

    @classmethod
    def load(cls, rules, path):
        return cls(rules, json.loads(Path(path).read_text(encoding="utf-8")))

    def evaluate(self, client, country):
        view = client.observe()
        if view["resolution_depth"] and not view["winner"]:
            raise CoreError("Incomplete resolution cannot receive a completed-action score")
        belief = simulation_from_observation(self.rules, view)
        return self._value(belief, country)

    def economic_from_view(self, view):
        settings = self.weights.get("economic_forecast")
        if settings is None:
            return None

        @cache
        def position():
            return observed_position(self.rules, view)

        return EconomicForecast(self.rules, settings, view["turn"], view["scores"],
                                lambda seat, kind: view["positions"][zone(seat, kind)]["count"],
                                lambda: position()[1], view["winner"], status_weights=self.status_weights, map_weights=self.map_weights,
                                units=self.units, hand_average=(lambda economy, seat: self.hand_average(*position(), seat, economy))
                                if self.action_opportunity else None)

    def economic_from_state(self, rules, state):
        settings = self.weights.get("economic_forecast")
        if settings is None:
            return None
        return EconomicForecast(rules, settings, vars(state.turn), state.scores,
                                lambda seat, kind: len(state.positions[zone(seat, kind)]),
                                lambda: state, state.winner, status_weights=self.status_weights, map_weights=self.map_weights,
                                units=self.units, hand_average=(lambda economy, seat: self.hand_average(rules, state, seat, economy))
                                if self.action_opportunity else None)

    def hand_value(self, rules, state, country, card, legal, economy=None, *, include_card=True, estimate_draw=True):
        w = self.weights
        weights = w["card_value_weights"]
        card_stock = self.units.card if self.units.enabled and include_card else 0
        if card.get("simulation_unknown"):
            return card_stock + weights["unknown"]
        if economy and is_delayed(card) and economy.responses:
            estimate = economy.responses.staging(country, card["id"], estimate_draw=estimate_draw)
            return card_stock + (max(0, estimate["net"]) if estimate["window_remaining"] else 0)
        if economy and economy.status_weights and is_status(card):
            return (card_stock + economy.statuses.report(country, card["id"], held=True)["future"] +
                    self.program_value(rules, state, country, card["effects"], economy, estimate_draw=estimate_draw))
        deployable = card["type"] == "RESPONSE" or (card["type"] == "STATUS" and not card.get("after"))
        abilities = [rules.abilities[a] for a in card.get("abilities", ())] if deployable else []
        airdrop = [a for a in abilities if self.action_distance and economy and economy.status_weights and is_airdrop(a)]
        copies = sum(a.get("copies", 1) for a in abilities if a not in airdrop)
        air_value = sum(max(0, economy.statuses.airdrop_value(country, a["id"])) for a in airdrop)
        if self.action_opportunity and economy and card.get("play_as"):
            program = max((economy.routes.tactical_effect(country, {"type": mode}) for mode in card["play_as"]), default=0)
        else:
            program = self.program_value(rules, state, country, card["effects"], economy, estimate_draw=estimate_draw,
                                         economic=bool(economy and economy.continuous_rules and card.get("legacy_kind") == "economic"))
        value = air_value + self.units.action_score(copies * weights["ability_ready"] / 2) + program
        return card_stock + max(0.0, value) * (
            1 if legal.can_start_card(state, country, card) else w["unusable_card_weight"])

    def program_value(self, rules, state, country, steps, economy=None, *, estimate_draw=True, economic=False):
        w, value = self.weights, 0.0
        card_weights = w["card_value_weights"]
        drawn = 0
        mills = economy.economic_mill_values(country, steps) if economic else {}
        for index, instruction in enumerate(steps):
            if index in mills:
                value += mills[index]
                continue
            operation = instruction.get("operation", instruction)
            kind = operation["type"]
            multiplier = operation.get("amount", 1) if kind == "GAIN_VP" else operation.get("count", 1)
            if self.action_opportunity and economy:
                location = economy.routes.tactical_effect(country, operation)
                if location is not None:
                    value += location
                    continue
            if kind == "GAIN_VP" and (economy or self.units.enabled):
                recipient = operation.get("score_country") or country
                delta = multiplier if rules.team(recipient) == rules.team(country) else -multiplier
                value += (economy.points(country, delta, card_weights["GAIN_VP"]) if economy else self.units.vp_score(delta))
            elif kind == "CARD_EFFECT" and "effect_value_weights" in w:
                value += effect_potential(rules, state, country, operation["effect"], w["effect_value_weights"], economy,
                                          units=self.units, native_weights=card_weights,
                                          tactical=self.action_opportunity)
            elif kind == "DRAW" and self.units.enabled:
                if not self.action_opportunity or not economy:
                    value += self.units.card_score(multiplier)
                elif estimate_draw:
                    value += economy.draw_value(country, multiplier, drawn)
                    drawn += multiplier
            else:
                estimate = card_weights.get(kind, card_weights.get(instruction["type"], 0)) * multiplier
                value += self.units.action_score(estimate / 2) if kind in VALUED_ACTIONS else estimate
        return value

    @property
    def hand_slot_cost(self):
        return self.units.card_score(self.weights.get("hand_retention", {}).get("slot_cost", 0.0))

    @property
    def uses_hand_retention(self):
        return self.marginal_opportunity or bool(self.hand_slot_cost)

    @property
    def card_play_cost(self):
        if self.action_distance:
            return 0
        return self.units.card if self.units.enabled else self.hand_slot_cost

    def action_cost(self, kind, phase):
        if self.action_opportunity and (kind == "USE_AIR" or (phase == "Play" and kind in PLAY_OPPORTUNITY_COMMANDS)):
            return self.units.linked("play_opportunity", self.units.action)
        return 0

    def basic_discard_cost(self, view, seat, card):
        kind = card.get("legacy_kind")
        use = view.get("basic_card_use", {}).get(seat, {}).get(kind)
        weight = self.units.linked("basic_discard", self.units.card_score(self.weights.get("basic_usage_cost", 0)))
        return weight * use["played"] / use["total"] if use else 0.0

    def short_term(self, rules, state, country, card, legal, economy, *, estimate_draw=True):
        from .air import air_options
        from .content_effects import condition_met

        if card.get("implementation", {}).get("status") == "blocked":
            return 0.0
        kind = card.get("legacy_kind")
        if kind == "air-power":
            if not rules.settings.get("air_phase"):
                return 0.0
            actions = ("deploy", "marshal", "superiority")
            effects = [{"type": "air-action", "force": force, "actions": actions} for force in rules.controlled_forces(country)]
            if self.action_opportunity and economy:
                return max((economy.routes.tactical_effect(country, {"type": "CARD_EFFECT", "effect": effect})
                            for effect in effects), default=0)
            useful = any(air_options(rules, state, country, effect) for effect in effects)
            return self.units.action_score(0.325 if useful else 0.10)
        raw = self.hand_value(rules, state, country, card, legal, economy, include_card=False, estimate_draw=estimate_draw)
        if self.status_weights and is_status(card):
            return raw
        if economy and is_delayed(card) and economy.responses:
            return raw
        if (kind == "bolster" or card["type"] in ("RESPONSE", "STATUS")) and condition_met(
                rules, state, country, card.get("content_condition")):
            attached = max((self.program_value(rules, state, country, rules.abilities[key]["effects"], economy,
                                               estimate_draw=estimate_draw) for key in card.get("abilities", ())), default=0)
            raw += 0.5 * max(0, attached) + (0.35 if card.get("modifiers") else 0)
            if kind == "bolster":
                raw += 0.20
        return raw

    def hand_average(self, rules, state, country, economy):
        hand = state.positions[zone(country, "HAND")]
        legal = LegalActionGenerator(rules)
        # Draw effects cannot estimate the value of a draw by recursively valuing themselves.
        return sum(self.short_term(rules, state, country, rules.cards[state.tokens[key].definition_id], legal,
                                   economy, estimate_draw=False) for key in hand) / len(hand) if hand else 0

    def cycle_value(self, economy, seat, count, retention):
        if economy and self.action_opportunity:
            return economy.cycle_value(seat, count)
        return cycling_ev(retention, self.units.card, count)

    def retention(self, view, country, *, position=None):
        rules, state = position if position is not None else observed_position(self.rules, view)
        legal = LegalActionGenerator(rules)
        economy = self.economic_from_state(rules, state)

        def short_term(card):
            return self.short_term(rules, state, country, card, legal, economy)

        retention = {key: self.units.card_score(v) for key, v in self.weights["hand_retention"].items()}
        return score_retention(rules, state, country, retention, short_term,
                               lambda card: self.basic_discard_cost(view, country, card),
                               expansion_cost=economy.routes.expansion_discard_cost if self.marginal_opportunity else None)

    def target_proximity(self, view, country, regions):
        if "route_forecast" not in self.weights:
            return {}
        rules, state = observed_position(self.rules, view)
        routes = ScoringRoutes(rules, state, board_units(rules, state), self.weights)
        return {region: routes.target_value(country, region) for region in regions}

    def _value(self, processor, country, *, excluded_unit=None, initial_units=None, played_card=None, card_event_value=0):
        s, rules, w = processor.state, processor.rules, self.weights
        team = rules.team(country)
        sign = lambda c: 1 if rules.team(c) == team else -1
        enemy = "allies" if team == "axis" else "axis"
        actual = s.scores[team] - s.scores[enemy]
        pieces = board_units(rules, s)
        removal = 0.0
        if excluded_unit is not None:
            pieces = [piece for piece in pieces if piece.token_id != excluded_unit.token_id]
        economy = self.economic_from_state(rules, s)
        stability = 0
        if self.action_distance:
            routes = ScoringRoutes(rules, s, pieces, w, include_connections=bool(economy and economy.continuous_rules))
            map_value, point_values, influences = routes.position.board_values(country, routes.scoring_rates())
            if not s.winner:
                stability = routes.supply_stability.value(country)
                map_value += stability
            if initial_units is not None:
                present = {piece.token_id for piece in pieces}
                removal = self.units.action_score(-sum(sign(owner) for key, owner in initial_units.items()
                                                        if key not in present))
                map_value += removal
        else:
            map_value, point_values, influences = self._legacy_map(rules, s, pieces, sign, excluded_unit)
        cards, resources = 0.0, 0.0
        legal = LegalActionGenerator(rules)
        card_weights = w["card_value_weights"]
        for c in TURN_ORDER:
            hand = s.positions[zone(c, "HAND")]
            for token_id in hand:
                card = rules.cards[s.tokens[token_id].definition_id]
                cards += sign(c) * self.hand_value(rules, s, c, card, legal, economy)
            # Status source and its ability replicas represent the same resource: count replicas only.
            for t in s.tokens.values():
                if t.owner_country == c and t.token_type == "ABILITY" and t.position_id == zone(c, "ABILITY_READY"):
                    source = s.tokens.get(t.source_token_id)
                    if self.status_weights and source and is_status(rules.cards.get(source.definition_id, {})):
                        continue
                    if self.action_distance and self.status_weights and is_airdrop(rules.abilities[t.definition_id]):
                        cards += sign(c) * max(0, economy.statuses.airdrop_value(c, t.definition_id))
                        continue
                    cards += sign(c) * self.units.action_score(card_weights["ability_ready"] / 2)
            if self.status_weights and (s.positions[zone(c, "STATUS")] or s.positions.get(zone(c, "DISABLED_STATUS"))):
                cards += sign(c) * sum(economy.statuses.portfolio(c, True).values())
            if economy:
                resources -= sign(c) * economy.future_loss(c)
            elif not s.positions[zone(c, "DECK")]:
                resources -= sign(c) * w["resource_penalties"]["empty_deck"]
        if played_card and s.tokens[played_card].position_id != zone(country, "HAND"):
            cards += self.units.card
        cards += card_event_value
        terminal = 2 if s.winner == team else -2 if s.winner == enemy else 0
        return PositionValue(float(actual), map_value, cards, resources,
                             self.units.vp_score(actual) + map_value + cards + resources,
                             terminal, tuple(point_values), tuple(influences), removal, card_event_value,
                             supply_stability=stability)

    def _legacy_map(self, rules, s, pieces, sign, excluded_unit):
        w = self.weights
        supply = SupplyResolver(rules)
        supplied = {force: supply.supplied(s, force, units=pieces) for force in rules.force_controllers}
        routes = ScoringRoutes(rules, s, pieces, w) if "route_forecast" in w else None
        if routes:
            nearby_sites = {t.token_id: routes.nearest(t.owner_country, t.position_id) for t in pieces}
        else:
            distances_by_unit = {t.token_id: MapGraph(rules).distances(t.position_id, s, t.owner_country) for t in pieces}
        opportunities = ()
        seat_rates = {}
        if self.seat_forecast:
            opportunities = scoring_opportunities(rules, s.turn)
            supply_cache = {seat: set().union(*(supplied[force] for force in rules.controlled_forces(seat)))
                            for seat in TURN_ORDER}
            scorer = ScoreResolver(rules)
            for seat in TURN_ORDER:
                seat_rates[seat] = scorer.sites(s, seat, pieces=pieces, supply_cache=supply_cache)
        locations = {}
        controlling_units = {}
        for site_id, site in rules.sites.items():
            if site.get("dynamic"):
                regions = {t.position_id for t in s.tokens.values() if t.token_type == "SCORE_SOURCE"
                           and t.definition_id == site_id and t.position_id in rules.regions}
            else:
                regions = {site["region"]}
            if not regions:
                continue
            locations[site_id] = regions
            controlling_units[site_id] = [t for t in pieces if t.position_id in regions
                and rules.force_controllers[t.owner_country] in site["countries"] and
                rules.units[t.definition_id]["kind"] != "AIR" and
                (not site["requires_supply"] or t.token_id in supplied[t.owner_country])]
        held_sites = ({site for rates in seat_rates.values() for site in rates} if self.seat_forecast else
                      {site for site, units in controlling_units.items() if units})
        crowding = {t.token_id: routes.construction_factors(
            t.owner_country, t.position_id, already_present=True, ignored_sites=held_sites) for t in pieces} if routes else {}
        point_values, influences, map_value = [], [], 0.0
        for site_id, regions in locations.items():
            site = rules.sites[site_id]
            controllers = controlling_units[site_id]
            friendly, hostile = 0.0, 0.0
            for t in pieces:
                route_factor = 1.0
                if routes:
                    site_route = nearby_sites[t.token_id].get(site_id)
                    if site_route is None:
                        continue
                    distance, route_factor = site_route
                    distance_weight = routes.distance_weight(distance)
                else:
                    distances = distances_by_unit[t.token_id]
                    distance = min(distances.get(p, 100000) for p in regions)
                    distance_weight = w["distance_weights"][distance] if distance < len(w["distance_weights"]) else 0.0
                supply_weight = w["supply_weights"]["supplied" if t.token_id in supplied[t.owner_country] else "unsupplied"]
                kind = rules.units[t.definition_id]["kind"]
                reserve = len(s.positions[zone(t.owner_country, "RESERVE_" + kind)])
                if (excluded_unit and t.owner_country == excluded_unit.owner_country
                        and kind == rules.units[excluded_unit.definition_id]["kind"]):
                    reserve += 1
                resource = 1.0 if distance == 0 else (1.0 if reserve else w["supply_weights"]["no_reserve_expansion"])
                strength = w["military_weights"][kind] * distance_weight * route_factor * supply_weight * resource
                if routes:
                    strength *= crowding[t.token_id].get(site_id, 1)
                influences.append({"token": t.token_id, "site": site_id, "distance": distance,
                                   "score_influence": strength, "military_influence": strength,
                                   "supply_weight": supply_weight, "route_factor": route_factor})
                if sign(t.owner_country) > 0:
                    friendly += strength
                else:
                    hostile += strength
            advantage = max(-1.0, min(1.0, (friendly - hostile) / w["saturation"]))
            control = sign(controllers[0].owner_country) if controllers else 0
            relevant = {rules.force_controllers[t.owner_country] for t in controllers} if controllers else set(site["countries"])
            remaining = 0
            for round_id in range(s.turn.round_id, rules.settings["max_rounds"] + 1):
                for index, eligible_country in enumerate(TURN_ORDER):
                    already_scored = (round_id == s.turn.round_id and (index < s.turn.country_index or
                                      (index == s.turn.country_index and s.turn.phase in PHASES and PHASES.index(s.turn.phase) >= PHASES.index("Victory"))))
                    if eligible_country in relevant and not already_scored:
                        remaining += 1
            remaining = min(w["horizon"], remaining)
            baseline = site["vp"] * sum(w["time_discount"] ** i for i in range(remaining))
            expansion = advantage if not control else 0
            threat = max(0.0, -advantage) if control > 0 else -max(0.0, advantage) if control < 0 else 0.0
            value = self.units.vp_score(baseline) * (
                control + w["expansion_weight"] * expansion - w["threat_weight"] * threat)
            if control:
                value *= w.get("frontier_scoring", {}).get("held_forecast", 1)
            diagnostic = {"site": site_id, "baseline_vp": baseline, "control": control,
                          "friendly": friendly, "hostile": hostile, "advantage": advantage, "value": value}
            if self.seat_forecast:
                diagnostic.update(forecast_site(site, site_id, seat_rates, opportunities, sign, advantage, w, self.units))
                value = diagnostic["value"]
            point_values.append(diagnostic)
            map_value += value
        return map_value, point_values, influences

    def supply_marginal(self, client, country, token_id):
        belief = simulation_from_observation(self.rules, client.observe())
        before = self._value(belief, country).map_value
        token = belief.state.tokens.get(token_id)
        if not token or token.token_type != "UNIT" or token.position_id not in belief.rules.regions:
            raise CoreError("Marginal analysis requires a visible board unit")
        after = self._value(belief, country, excluded_unit=token).map_value
        return before - after  # Diagnostic only; never summed into total again.


def scoring_opportunities(rules, turn):
    end = (rules.settings["max_rounds"] - turn.round_id + 1) * len(TURN_ORDER)
    current = turn.country_index
    first = current + (turn.phase in ("Victory", "Discard", "Draw"))
    return tuple((index - current, TURN_ORDER[index % len(TURN_ORDER)]) for index in range(first, end))


def forecast_site(site, site_id, rates, opportunities, sign, advantage, weights, units=ScoreUnits()):
    """Separate the initial scoring window from discounted later opportunities."""
    horizon = weights["horizon"]
    discount = weights["time_discount"]
    held = any(site_id in values for values in rates.values())
    fixed_schedule = []
    fixed = 0.0
    if held:
        candidates = [(delay, seat, rates[seat][site_id]) for delay, seat in opportunities if site_id in rates[seat]]
        fixed_end = units.capture_rounds * len(TURN_ORDER)
        fixed_schedule = [entry for entry in candidates if entry[0] < fixed_end]
        schedule = [entry for entry in candidates if entry[0] >= fixed_end][:horizon]
        baseline = sum(vp for _, _, vp in fixed_schedule) + sum(vp * discount ** delay for delay, _, vp in schedule)
        fixed = units.vp_score(sum(vp * sign(seat) for _, seat, vp in fixed_schedule))
        value = sum(vp * discount ** delay * sign(seat) *
                    (1 - weights["threat_weight"] * max(0, -sign(seat) * advantage)) for delay, seat, vp in schedule)
        value *= weights.get("frontier_scoring", {}).get("held_forecast", 1)
    else:
        # A neutral site is a hypothetical opportunity for one seat. Multiplying
        # it by every eligible seat would value the same future capture repeatedly.
        eligible = next((seat for _, seat in opportunities if seat in site["countries"] and sign(seat) * advantage > 0), None)
        schedule = [(delay, seat, site["vp"]) for delay, seat in opportunities if seat == eligible][:horizon]
        baseline = sum(vp * discount ** delay for delay, _, vp in schedule)
        value = baseline * weights["expansion_weight"] * advantage
    long_term = units.vp_score(value)
    return {"baseline_vp": baseline, "value": fixed + long_term, "fixed_score": fixed, "long_term_score": long_term,
            "opportunities": [
        {"seat": seat, "delay": delay, "vp": vp, "projected_capture": not held,
         "fixed": delay < units.capture_rounds * len(TURN_ORDER) and held}
        for delay, seat, vp in (*fixed_schedule, *schedule)
    ]}


def effect_potential(rules, state, seat, effect, weights, economy=None, *, units=ScoreUnits(), native_weights=None,
                     tactical=False):
    from .content_effects import condition_met, point_award
    from .economic_scoring import POINT_EFFECTS
    if economy:
        dynamic = economy.effect(seat, effect, weights["points"])
        if dynamic is not None:
            return dynamic
        if tactical:
            location = economy.routes.tactical_effect(seat, {"type": "CARD_EFFECT", "effect": effect})
            if location is not None:
                return location
    if not condition_met(rules, state, seat, effect.get("when")):
        return 0.0
    kind = effect["type"]
    if kind == "choose-one":
        return max((sum(effect_potential(rules, state, seat, child, weights, economy,
                                        units=units, native_weights=native_weights, tactical=tactical) for child in option["effects"])
                    for option in effect["options"]), default=0.0)
    if units.enabled and kind in POINT_EFFECTS:
        recipient, amount = point_award(rules, state, seat, effect)
        return units.vp_score(amount if rules.team(recipient) == rules.team(seat) else -amount)
    if kind == "points":
        award = effect["side"]
        friendly = award in ("self", rules.team(seat))
        return weights["points"] * effect["amount"] * (1 if friendly else -1)
    estimate = weights.get(kind, weights["default"])
    if units.enabled:
        action = equivalent_action(effect, seat)
        if action and native_weights and action in native_weights:
            return units.action_score(native_weights[action] / 2)
        if kind == "play-from-zone":
            return units.linked("play_from_zone_hand", units.action_score(estimate / 2))
        if kind == "search-deck":
            return units.linked("search_deck_hand", units.card_score(estimate))
        if kind in ACTION_SUPPORT_EFFECTS or kind in ("battle-series", "battle", "air-action"):
            return units.action_score(estimate / 2)
        if kind == "recover-discard":
            return units.card_score(estimate)
    return estimate


class SimulationIncomplete(CoreError):
    pass


def candidate_key(row):
    return (row["complete"], row.get("terminal_rank", -3),
            row["q"] if row["q"] is not None else float("-inf"))


def acceptable_play(row, *, mandatory=False):
    paid = row.get("initial_card_score") or row.get("consumes_card") or row.get("action_cost")
    kind = row.get("exchange_followup", {}).get("play", row["command"])["type"]
    return row["complete"] and (not paid or row["q"] > 0 or row["terminal_rank"] == 2
                                or (mandatory and kind in PLAY_OPPORTUNITY_COMMANDS))


class OneStepPlanner:
    def __init__(self, rules, weights):
        self.evaluator = ScoreEvaluator(rules, weights)

    def rank(self, client, country):
        return sorted(self.iter_rank(client, country), key=candidate_key, reverse=True)

    def end_score(self, processor, country, deadline, **effects):
        if self.evaluator.settlement:
            return self.evaluator.settlement.value(processor, country, deadline=deadline, **effects)
        return self.evaluator._value(processor, country, **effects)

    def iter_rank(self, client, country, *, deadline=None):
        view = client.observe()
        root = simulation_from_observation(self.evaluator.rules, view, search_projection=True)
        root_state = root.state
        before = self.end_score(root, country, deadline)
        economy = (self.evaluator.economic_from_state(root.rules, root_state) if self.evaluator.action_opportunity else None)
        if self.evaluator.settlement:
            initial_units = dict(before.surviving_units)
            original_board = frozenset(t.token_id for t in board_units(root.rules, root_state))
        else:
            initial_units = ({piece.token_id: piece.owner_country for piece in board_units(root.rules, root_state)}
                             if self.evaluator.action_distance else None)
            original_board = frozenset()
        for command in client.legal_commands():
            self._check_deadline(deadline)
            if command["actor"] != country:
                continue
            nodes = [0]
            try:
                if command["type"] == "REALLOCATE":
                    yield from self._rank_exchange(root, root_state, view, country, command, before, deadline)
                    continue
                candidate = CommandProcessor(root.rules, root_state, search_projection=True)
                simulated_seat = CoreAPI(candidate, (country,))
                if command not in simulated_seat.legal_commands():
                    raise SimulationIncomplete("Action cannot be represented from this observation")
                simulated_seat.apply(command)
                consumes_card = command["type"] in ("PLAY_CARD", "USE_AIR")
                played = command["card"] if consumes_card and self.evaluator.action_distance else None
                source = root_state.tokens.get(command.get("card"))
                recruitment_path = source and recruitment_play(root.rules, root.rules.cards[source.definition_id], command)
                cost = self.evaluator.action_cost(command["type"], root_state.turn.phase)
                value, choices = self._finish(candidate, country, nodes, deadline,
                                              initial_units=initial_units, played_card=played,
                                              original_board=original_board, recruitment_path=recruitment_path,
                                              draw_economy=economy if cost or command["type"] == "ACTIVATE_CARD" else None)
                initial = -self.evaluator.hand_slot_cost if consumes_card and not self.evaluator.units.enabled else 0.0
                vp_delta = value.actual_vp - before.actual_vp
                yield {"command": deepcopy(command), "complete": True, "nodes": nodes[0],
                       "weights_digest": digest(self.evaluator.weights),
                       "actual_vp_delta": vp_delta, "vp_score_delta": self.evaluator.units.vp_score(vp_delta),
                       "map_delta": value.map_value - before.map_value,
                       **({"supply_stability_delta": value.supply_stability - before.supply_stability}
                          if self.evaluator.units.supply_stability is not None else {}),
                       "card_delta": value.card_value - before.card_value + initial,
                       "resource_delta": value.resource_adjustment - before.resource_adjustment,
                       "q": value.total - before.total + initial - cost, "terminal_rank": value.terminal_rank,
                       **({"action_cost": -cost} if cost else {}),
                       **({"unit_removal_score": value.unit_removal_score} if value.unit_removal_score else {}),
                       **({"temporary_vp": value.temporary_vp} if value.temporary_vp else {}),
                       **({"card_event_score": value.card_event_value} if value.card_event_value else {}),
                       **({"expansion_score": value.expansion_score, "expansion_sites": value.expansion_sites}
                          if value.expansion_sites else {}),
                       **({"initial_card_score": initial} if initial else {}),
                       **({"consumes_card": True} if consumes_card and self.evaluator.units.enabled
                          and not self.evaluator.action_distance else {}),
                       "continuations": [choice for choice, _ in choices],
                       "choice_signatures": [signature for _, signature in choices]}
            except (SimulationIncomplete, ResolutionBudgetExceeded) as e:
                yield {"command": deepcopy(command), "complete": False,
                       "reason": str(e), "q": None, "nodes": nodes[0]}

    def _rank_exchange(self, root, state, view, country, command, before, deadline):
        """Yield completed alternatives as they finish, using one public starting position."""
        from .ai_match_policy import RulePolicy

        rules = root.rules
        legal = LegalActionGenerator(rules)
        economy = self.evaluator.economic_from_state(rules, state)
        retention = (self.evaluator.retention(view, country, position=(rules, state))
                     if self.evaluator.hand_slot_cost else {})
        marginal = self.evaluator.marginal_opportunity

        def payment_cost(card):
            return (-self.evaluator.hand_value(rules, state, country, card, legal, economy) -
                    self.evaluator.basic_discard_cost(view, country, card))

        by_definition = {}
        for plan in payment_plans(rules, view, country, payment_cost,
                                  expansion_cost=economy.routes.expansion_discard_cost if marginal else None):
            if marginal:
                plan["discard_usage_cost"] = sum(self.evaluator.basic_discard_cost(view, country,
                    rules.cards[state.tokens[key].definition_id]) for key in plan["payment"])
            key = (plan["source"], plan["definition"])
            if key not in by_definition or plan["discard_score"] > by_definition[key]["discard_score"]:
                by_definition[key] = plan
        if not by_definition:
            raise SimulationIncomplete("No exchange plan from the seat's visible inventory")
        paper = RulePolicy(rules, self.evaluator)
        plans = sorted(by_definition.values(),
                       key=lambda p: -paper.play_card_value(rules.cards[p["definition"]], country, economy))
        pool = zone(country, "OBJECT_POOL")
        deck_samples = {}
        for plan in plans:
            if plan["source"] == "deck":
                definition = plan["definition"]
                deck_samples[definition] = {
                    "token_id": f"estimate:reallocation:{country}:{plan['kind']}",
                    "definition_id": definition, "token_type": "CARD", "owner_country": country,
                    "source_token_id": None, "position_id": pool}
        stock = root
        if deck_samples:
            # Park representatives outside the hand and deck. Only the selected one replaces
            # an opaque deck slot; follow-up effects still see an otherwise opaque deck.
            projected = deepcopy(view)
            for sample in deck_samples.values():
                key = sample["token_id"]
                projected["tokens"][key] = sample
                projected["positions"][pool]["tokens"].append(key)
                projected["positions"][pool]["count"] += 1
            stock = simulation_from_observation(self.evaluator.rules, projected, search_projection=True)
        stock_state = stock.state
        original_board = frozenset(t.token_id for t in board_units(rules, state)) if self.evaluator.settlement else frozenset()
        cycling = self.evaluator.cycle_value(economy, country, view["reallocation"]["cost"], retention) if not marginal else {}
        for index, plan in enumerate(plans):
            nodes = [0]
            try:
                self._check_deadline(deadline)
                # Leave time to try the remaining kinds even when the first has a long response chain.
                plan_deadline = deadline
                if deadline is not None:
                    reserve = min(0.18 * (len(plans) - index - 1), max(0, deadline - perf_counter()) / 2)
                    plan_deadline -= reserve
                yield self._exchange_play(stock, stock_state, deck_samples, plan, cycling, country, command,
                                          before, original_board, nodes, plan_deadline, economy if marginal else None)
            except (SimulationIncomplete, ResolutionBudgetExceeded, TimeoutError) as error:
                yield {"command": deepcopy(command), "complete": False,
                       "reason": "deadline" if isinstance(error, TimeoutError) else str(error),
                       "q": None, "nodes": nodes[0], "reallocation": plan}

    def _exchange_play(self, stock, stock_state, deck_samples, plan, cycling, country, command, before, original_board,
                       nodes, deadline, original_economy=None):
        from .air import opportunity
        from .ai_match_policy import RulePolicy

        hand, discard = zone(country, "HAND"), zone(country, "DISCARD")
        post_payment = deepcopy(stock_state)
        executor = stock.executor
        changes = outcome("ESTIMATED_REALLOCATION")
        for key in plan["payment"]:
            executor.transfer(post_payment, key, discard, changes)
        marker = opportunity(post_payment, "reallocation-opportunity-" + country)
        executor.transfer(post_payment, marker.token_id, zone(country, "RESPONSE"), changes)
        target = plan["target"]
        if target is None:
            target = deck_samples[plan["definition"]]["token_id"]
            deck = post_payment.positions[zone(country, "DECK")]
            executor.transfer(post_payment, deck[-1], zone(country, "OBJECT_POOL"), changes)
        executor.transfer(post_payment, target, hand, changes)
        ordinary = CommandProcessor(stock.rules, post_payment, search_projection=True)
        client = CoreAPI(ordinary, (country,))
        legal_play = [c for c in client.legal_commands()
                      if c["type"] in ("PLAY_CARD", "USE_STATUS", "FORFEIT_PLAY", "DISCARD_CARD", "END_PHASE")]
        plays = [c for c in legal_play if c["type"] == "PLAY_CARD" and c.get("card") == target]
        if self.evaluator.settlement:
            initial_units = dict(before.surviving_units)
            if not plays and legal_play:
                ranked = RulePolicy(ordinary.rules, self.evaluator).rank(
                    client.observe(include_history=False), legal_play, verify_recruitment=False)
                plays = [ranked[0]["command"]]
        else:
            before = self.end_score(ordinary, country, deadline)
            initial_units = ({piece.token_id: piece.owner_country for piece in board_units(ordinary.rules, post_payment)}
                             if self.evaluator.action_distance else None)
        outcomes = []
        economy = (self.evaluator.economic_from_state(ordinary.rules, post_payment) if self.evaluator.action_opportunity else None)
        for play in plays:
            self._check_deadline(deadline)
            branch = ordinary if len(plays) == 1 else CommandProcessor(ordinary.rules, post_payment, search_projection=True)
            CoreAPI(branch, (country,)).apply(play)
            played = play["card"] if self.evaluator.action_distance and play["type"] == "PLAY_CARD" else None
            source = post_payment.tokens.get(play.get("card"))
            recruitment_path = source and recruitment_play(ordinary.rules, ordinary.rules.cards[source.definition_id], play)
            value, trail = self._finish(branch, country, nodes, deadline, initial_units=initial_units,
                                        played_card=played, original_board=original_board, recruitment_path=recruitment_path,
                                        draw_economy=economy if play["type"] != "END_PHASE" else None)
            self._check_deadline(deadline)
            outcomes.append((value, play, trail))
        after, play, choices = max(outcomes, key=lambda row: (row[0].terminal_rank, row[0].total),
                                   default=(before, None, []))
        cost = self.evaluator.action_cost(play["type"], post_payment.turn.phase) if play else 0
        initial = -self.evaluator.hand_slot_cost if outcomes and not self.evaluator.units.enabled else 0.0
        if original_economy:
            spend = int(bool(play and play["type"] in ("PLAY_CARD", "DISCARD_CARD")))
            draws = 0 if after.terminal_rank else extra_refill(original_economy, country, plan, spend=spend)
            cycling = original_economy.cycle_value(country, draws)
        plan = {**plan, **cycling}
        if self.evaluator.settlement:
            # The final stock already contains the acquisition and has paid the real expense.
            fees = -self.evaluator.units.card_score(len(plan["payment"]))
            card_adjustment = -plan.get("discard_expansion_cost", 0) - plan.get("discard_usage_cost", 0)
            plan = price({**plan, "discard_score": fees + card_adjustment}, after.total - before.total - fees - cost)
        else:
            plan = price(plan, after.total - before.total + initial - cost)
            card_adjustment = plan["discard_score"]
        return {"command": deepcopy(command), "complete": True, "nodes": nodes[0],
                "weights_digest": digest(self.evaluator.weights),
                "actual_vp_delta": after.actual_vp - before.actual_vp,
                "vp_score_delta": self.evaluator.units.vp_score(after.actual_vp - before.actual_vp),
                "map_delta": after.map_value - before.map_value,
                **({"supply_stability_delta": after.supply_stability - before.supply_stability}
                   if self.evaluator.units.supply_stability is not None else {}),
                "card_delta": after.card_value - before.card_value + card_adjustment + initial,
                "resource_delta": after.resource_adjustment - before.resource_adjustment,
                **({"cycle_score": cycling["cycle_score"]} if cycling else {}),
                "q": plan["total"], "terminal_rank": after.terminal_rank,
                **({"action_cost": -cost} if cost else {}),
                **({"unit_removal_score": after.unit_removal_score} if after.unit_removal_score else {}),
                **({"card_event_score": after.card_event_value} if after.card_event_value else {}),
                **({"expansion_score": after.expansion_score, "expansion_sites": after.expansion_sites}
                   if after.expansion_sites else {}),
                "continuations": [], "choice_signatures": [], "reallocation": plan,
                **({"exchange_followup": {"play": play, "target": target,
                                          "continuations": [choice for choice, _ in choices],
                                          "choice_signatures": [signature for _, signature in choices]}}
                   if self.evaluator.settlement and play else {})}

    @staticmethod
    def _check_deadline(deadline):
        if deadline is not None and perf_counter() >= deadline:
            raise TimeoutError("Projected search exceeded its deadline")

    def _card_events(self, processor, state, country, played_card, draw_economy=None):
        if not self.evaluator.action_distance:
            return 0
        rules = processor.rules
        economy = None
        team = rules.team(country)
        result = 0
        for record in processor.records:
            outcome = record["outcome"]
            if not outcome["success"]:
                continue
            if draw_economy and outcome["event"] == "DRAW":
                actor = record["actor"]
                count = sum(move["from"] == zone(actor, "DECK") and move["to"] == zone(actor, "HAND")
                            for move in outcome["changes"])
                if count:
                    cycle = draw_economy.cycle_value(actor, count)
                    adjustment = count * self.evaluator.units.card - cycle["cycle_score"]
                    # The settled card stock already includes the full value of every drawn card.
                    result -= (1 if rules.team(actor) == team else -1) * adjustment
            if outcome["event"] == "ORDER_DECK":
                actor = record["actor"]
                seen = len(outcome["changes"])
                if self.evaluator.marginal_opportunity:
                    forecast = draw_economy or economy
                    if forecast is None:
                        forecast = economy = self.evaluator.economic_from_state(rules, state)
                    value = forecast.cycle_value(actor, seen)["cycle_score"]
                elif self.evaluator.settlement:
                    look = self.evaluator.weights.get("economic_forecast", {}).get("deck_top_look", 0.3)
                    value = self.evaluator.units.card_score(1 if seen > 1 else look if seen else 0)
                else:
                    if economy is None:
                        economy = self.evaluator.economic_from_state(rules, state)
                    value = economy.top_value(seen, economy.routes.position.scoring_rounds(actor))
                result += (1 if rules.team(actor) == team else -1) * value
            for move in outcome["changes"]:
                origin, destination = move["from"], move["to"]
                token = state.tokens[move["token_id"]]
                if token.token_type != "CARD" or not origin or origin == destination:
                    continue
                owner = token.owner_country
                returns_to_deck = destination == zone(owner, "DECK")
                rearms = origin == zone(owner, "DISCARD") and destination == zone(owner, "RESPONSE")
                goes_back_to_hand = (token.token_id == played_card and origin == "RESOLUTION" and
                                     destination == zone(owner, "HAND"))
                recovered = origin == zone(owner, "DISCARD") and destination == zone(owner, "HAND")
                card_unit = self.evaluator.units.card
                return_value = self.evaluator.units.linked("card_return", card_unit)
                sign = 1 if rules.team(owner) == team else -1
                if returns_to_deck or rearms or goes_back_to_hand:
                    result += sign * return_value
                elif recovered:
                    result += sign * (return_value - card_unit)  # Recovered cards are already included in stock.
        return result

    @staticmethod
    def _settled_airdrops(processor, state):
        placements = {}
        for record in processor.records:
            if record["operation_type"] not in CONSTRUCTION:
                continue
            action = record["operation"]
            ability = state.tokens.get(action.get("object_token_id"))
            if not ability:
                continue
            for move in record["outcome"]["changes"]:
                if (move["to"] in processor.rules.regions and state.tokens[move["token_id"]].token_type == "UNIT" and
                        ability_airdrop(processor.rules, state, ability, action, move["to"])):
                    placements[move["token_id"]] = move["to"]
        return placements

    def _finish(self, processor, country, nodes, deadline=None, *, initial_units=None, played_card=None,
                card_events=0, original_board=frozenset(), direct_removals=frozenset(), constructed=frozenset(),
                recruited=frozenset(), recruitment_path=False, draw_economy=None, airdrops=None):
        self._check_deadline(deadline)
        nodes[0] += 1
        if nodes[0] > self.evaluator.weights["max_simulation_nodes"]:
            raise SimulationIncomplete("Within-card choice budget exceeded; no partial score assigned")
        state = processor.state
        card_events += self._card_events(processor, state, country, played_card, draw_economy)
        if original_board:
            direct_removals = direct_removals.union(
                move["token_id"] for record in processor.records
                if record["operation_type"] in ("LAND_BATTLE", "SEA_BATTLE", "ELIMINATE")
                and record["operation"].get("reason") != "unsupplied"
                for move in record["outcome"]["changes"]
                if move["token_id"] in original_board and move["from"] in processor.rules.regions
                and ":RESERVE_" in move["to"])
        effects = {"initial_units": initial_units, "played_card": played_card, "card_event_value": card_events}
        if self.evaluator.settlement:
            effects["direct_removals"] = direct_removals
            new_units = [(record["operation_type"], move["token_id"]) for record in processor.records
                         if record["operation_type"] in CONSTRUCTION
                         and not (self.evaluator.action_opportunity and record["outcome"]["context"].get("redeployed"))
                         for move in record["outcome"]["changes"] if move["to"] in processor.rules.regions
                         and state.tokens[move["token_id"]].token_type == "UNIT"]
            recruited = recruited.union(key for action, key in new_units if action == "RECRUIT")
            effects["recruited"] = recruited
            if self.evaluator.weights.get("known_ability_scoring"):
                airdrops = {**(airdrops or {}), **self._settled_airdrops(processor, state)}
                effects["airdrops"] = airdrops
            if self.evaluator.settlement.expansion_value or self.evaluator.units.supply_stability is not None:
                constructed = constructed.union(key for _, key in new_units)
                effects["constructed"] = constructed
        if state.winner:
            # Terminal positions may retain irrelevant pending frame cleanup.
            return self.end_score(processor, country, deadline, **effects), []
        if not state.decision:
            if state.stack:
                raise SimulationIncomplete("Resolution not complete")
            return self.end_score(processor, country, deadline, **effects), []
        decision = state.decision
        actor = decision.required_country
        signature = (decision.decision_type, decision.continuation["type"], sorted(decision.legal_options))
        client = CoreAPI(processor, (actor,))
        commands = client.legal_commands()
        width = self.evaluator.weights.get("construction_search_width", 0)
        if self.evaluator.marginal_opportunity and decision.decision_type == "ORDER_DECK":
            from .ai_match_policy import RulePolicy
            ranked = RulePolicy(processor.rules, self.evaluator).rank(client.observe(include_history=False), commands)
            commands = [row["command"] for row in ranked if row["command"]["type"] == "CHOOSE"][:1]
        if (actor == country and self.evaluator.weights.get("response_timing_scoring") and
                decision.continuation["type"] == "RESPONSE_SELECTED"):
            from .ai_match_policy import RulePolicy
            rows = RulePolicy(processor.rules, self.evaluator).rank(client.observe(include_history=False), commands)
            if any(row.get("response_forecast") and row["command"]["type"] == "CHOOSE" for row in rows):
                commands = [rows[0]["command"]]
        # A port or route may first look unsupplied and then make a later action legal.
        if width and len(commands) > width and not (recruitment_path or recruited):
            view = client.observe(include_history=False)
            if ((view["decision"].get("map_force") and not view["decision"]["map_battle"]) or
                    (self.evaluator.action_opportunity and decision.continuation["type"] in (
                        "REDEPLOY_SELECTED", "REBUILD_UNIT", "AIR_ACTION_SELECTED"))):
                from .ai_match_policy import RulePolicy
                ranked = RulePolicy(processor.rules, self.evaluator).rank(view, commands)
                commands = [row["command"] for row in ranked[:width]]
        branches = []
        for command in commands:
            self._check_deadline(deadline)
            branch = CommandProcessor(processor.rules, state, search_projection=True)
            CoreAPI(branch, (actor,)).apply(command)
            value, trail = self._finish(branch, country, nodes, deadline, initial_units=initial_units,
                                        played_card=played_card, card_events=card_events, original_board=original_board,
                                        direct_removals=direct_removals, constructed=constructed, recruited=recruited,
                                        recruitment_path=recruitment_path, draw_economy=draw_economy, airdrops=airdrops)
            branches.append((value, [(command, signature), *trail]))
        if not branches:
            raise SimulationIncomplete("No legal continuation")
        key = lambda item: (item[0].terminal_rank, item[0].total)
        return (max if processor.rules.team(actor) == processor.rules.team(country) else min)(branches, key=key)

    def choose(self, client, country):
        if client.observe()["decision"]:
            options = [command for command in client.legal_commands() if command["actor"] == country]
            if not options:
                raise CoreError("AI does not control this decision")
            return options[0]
        candidates = [c for c in self.rank(client, country) if c["complete"]]
        if not candidates:
            raise SimulationIncomplete("No complete candidate within configured budget")
        return deepcopy(candidates[0]["command"])
