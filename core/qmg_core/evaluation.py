"""Deterministic VP-equivalent diagnostics, with bounded within-card choice search."""
from copy import deepcopy
from dataclasses import dataclass
import json
from math import isfinite
from pathlib import Path

from .api import CoreAPI, observed_position, simulation_from_observation
from .definitions import RuleSet
from .engine import CommandProcessor, ResolutionBudgetExceeded
from .hand_retention import score_retention
from .model import CoreError, TURN_ORDER, PHASES, zone, digest
from .operations import ScoreResolver, outcome
from .reallocation_scoring import payment_plans, price
from .rules import LegalActionGenerator, MapGraph, SupplyResolver, board_units


@dataclass(frozen=True)
class PositionValue:
    actual_vp: float
    map_value: float
    card_value: float
    resource_adjustment: float
    total: float
    terminal_rank: int
    score_points: tuple
    token_influence: tuple


class ScoreEvaluator:
    def __init__(self, rules, weights):
        self.rules = RuleSet({**rules.data, "initial_tokens": []}, projected=True)
        self.weights = deepcopy(weights)
        numbers = [*weights["distance_weights"], *weights["supply_weights"].values(),
                   *weights["military_weights"].values(), *weights["card_value_weights"].values(),
                   *weights["resource_penalties"].values(), weights["time_discount"], weights["saturation"],
                   weights["expansion_weight"], weights["threat_weight"], weights["unusable_card_weight"]]
        if type(weights.get("seat_forecast", False)) is not bool:
            raise CoreError("Invalid seat forecast setting")
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
        if any(type(n) not in (int, float) or not isfinite(n) or n < 0 for n in numbers) or not 0 <= weights["time_discount"] <= 1:
            raise CoreError("Evaluation weights must be finite nonnegative values")
        if weights["saturation"] <= 0 or type(weights["horizon"]) is not int or not 1 <= weights["horizon"] <= 20:
            raise CoreError("Invalid planning horizon or saturation")
        if "hand_retention" in weights and not 0 < retention["slot_cost"] < retention["basic_floor"]:
            raise CoreError("Basic reserve must exceed the hand slot cost")
        if type(weights["max_simulation_nodes"]) is not int or not 1 <= weights["max_simulation_nodes"] <= 10000:
            raise CoreError("Invalid simulation budget")

    @classmethod
    def load(cls, rules, path):
        return cls(rules, json.loads(Path(path).read_text(encoding="utf-8")))

    def evaluate(self, client, country):
        view = client.observe()
        if view["resolution_depth"] and not view["winner"]:
            raise CoreError("Incomplete resolution cannot receive a completed-action score")
        belief = simulation_from_observation(self.rules, view)
        return self._value(belief, country)

    def hand_value(self, rules, state, country, card, legal):
        w = self.weights
        weights = w["card_value_weights"]
        if card.get("simulation_unknown"):
            return weights["unknown"]
        deployable = card["type"] == "RESPONSE" or (card["type"] == "STATUS" and not card.get("after"))
        copies = sum(rules.abilities[a].get("copies", 1) for a in card.get("abilities", [])) if deployable else 0
        value = copies * weights["ability_ready"] + self.program_value(rules, state, country, card["effects"])
        return max(0.0, value) * (1 if legal.can_start_card(state, country, card) else w["unusable_card_weight"])

    def program_value(self, rules, state, country, steps):
        w, value = self.weights, 0.0
        for instruction in steps:
            operation = instruction.get("operation", instruction)
            multiplier = operation.get("amount", 1) if operation["type"] == "GAIN_VP" else operation.get("count", 1)
            if operation["type"] == "CARD_EFFECT" and "effect_value_weights" in w:
                value += effect_potential(rules, state, country, operation["effect"], w["effect_value_weights"])
            else:
                weights = w["card_value_weights"]
                value += weights.get(operation["type"], weights.get(instruction["type"], 0)) * multiplier
        return value

    @property
    def hand_slot_cost(self):
        return self.weights.get("hand_retention", {}).get("slot_cost", 0.0)

    def retention(self, view, country):
        from .air import air_options
        from .content_effects import condition_met

        rules, state = observed_position(self.rules, view)
        legal = LegalActionGenerator(rules)

        def short_term(card):
            if card.get("implementation", {}).get("status") == "blocked":
                return 0.0
            kind = card.get("legacy_kind")
            if kind == "air-power":
                if not rules.settings.get("air_phase"):
                    return 0.0
                actions = ("deploy", "marshal", "superiority")
                useful = any(air_options(rules, state, country, {"force": force, "actions": actions})
                             for force in rules.controlled_forces(country))
                return 0.65 if useful else 0.20
            raw = self.hand_value(rules, state, country, card, legal)
            if kind == "bolster" or card["type"] in ("RESPONSE", "STATUS"):
                ready = condition_met(rules, state, country, card.get("content_condition"))
                if ready:
                    attached = max((self.program_value(rules, state, country, rules.abilities[k]["effects"])
                                    for k in card.get("abilities", ())), default=0)
                    raw += 0.5 * max(0, attached) + (0.35 if card.get("modifiers") else 0)
                    if kind == "bolster":
                        raw += 0.20
            return raw

        return score_retention(rules, state, country, self.weights["hand_retention"], short_term)

    def _value(self, processor, country, *, excluded_unit=None):
        s, rules, w = processor.state, processor.rules, self.weights
        team = rules.team(country)
        sign = lambda c: 1 if rules.team(c) == team else -1
        enemy = "allies" if team == "axis" else "axis"
        actual = s.scores[team] - s.scores[enemy]
        pieces = board_units(rules, s)
        if excluded_unit is not None:
            pieces = [piece for piece in pieces if piece.token_id != excluded_unit.token_id]
        supply = SupplyResolver(rules)
        supplied = {force: supply.supplied(s, force, units=pieces) for force in rules.force_controllers}
        distances_by_unit = {t.token_id: MapGraph(rules).distances(t.position_id, s, t.owner_country) for t in pieces}
        opportunities = ()
        seat_rates = {}
        if w.get("seat_forecast"):
            opportunities = scoring_opportunities(rules, s.turn)
            for seat in TURN_ORDER:
                supplied_for_seat = set().union(*(supplied[force] for force in rules.controlled_forces(seat)))
                seat_rates[seat] = ScoreResolver(rules).sites(s, seat, pieces=pieces, supplied=supplied_for_seat)
        point_values, influences, map_value = [], [], 0.0
        for site_id, site in rules.sites.items():
            if site.get("dynamic"):
                sources = [t for t in s.tokens.values() if t.token_type == "SCORE_SOURCE" and t.definition_id == site_id and t.position_id in rules.regions]
                regions = sorted({t.position_id for t in sources})
            else:
                regions = [site["region"]]
            if not regions:
                continue
            friendly, hostile = 0.0, 0.0
            for t in pieces:
                distances = distances_by_unit[t.token_id]
                distance = min((distances.get(p, 100000) for p in regions), default=100000)
                dw = w["distance_weights"][distance] if distance < len(w["distance_weights"]) else 0.0
                supply_weight = w["supply_weights"]["supplied" if t.token_id in supplied[t.owner_country] else "unsupplied"]
                kind = rules.units[t.definition_id]["kind"]
                reserve = len(s.positions[zone(t.owner_country, "RESERVE_" + kind)])
                if (excluded_unit and t.owner_country == excluded_unit.owner_country
                        and kind == rules.units[excluded_unit.definition_id]["kind"]):
                    reserve += 1
                resource = 1.0 if distance == 0 else (1.0 if reserve else w["supply_weights"]["no_reserve_expansion"])
                strength = w["military_weights"][kind] * dw * supply_weight * resource
                influences.append({"token": t.token_id, "site": site_id, "distance": distance,
                                   "score_influence": strength, "military_influence": strength, "supply_weight": supply_weight})
                if sign(t.owner_country) > 0:
                    friendly += strength
                else:
                    hostile += strength
            advantage = max(-1.0, min(1.0, (friendly - hostile) / w["saturation"]))
            controllers = [t for t in pieces if t.position_id in regions
                           and rules.force_controllers[t.owner_country] in site["countries"] and
                           rules.units[t.definition_id]["kind"] != "AIR" and
                           (not site["requires_supply"] or t.token_id in supplied[t.owner_country])]
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
            value = baseline * (control + w["expansion_weight"] * expansion - w["threat_weight"] * threat)
            diagnostic = {"site": site_id, "baseline_vp": baseline, "control": control,
                          "friendly": friendly, "hostile": hostile, "advantage": advantage, "value": value}
            if w.get("seat_forecast"):
                diagnostic.update(forecast_site(site, site_id, seat_rates, opportunities, sign, advantage, w))
                value = diagnostic["value"]
            point_values.append(diagnostic)
            map_value += value
        cards, resources = 0.0, 0.0
        legal = LegalActionGenerator(rules)
        card_weights = w["card_value_weights"]
        for c in TURN_ORDER:
            hand = s.positions[zone(c, "HAND")]
            for token_id in hand:
                card = rules.cards[s.tokens[token_id].definition_id]
                cards += sign(c) * self.hand_value(rules, s, c, card, legal)
            # Status source and its ability replicas represent the same resource: count replicas only.
            for t in s.tokens.values():
                if t.owner_country == c and t.token_type == "ABILITY" and t.position_id == zone(c, "ABILITY_READY"):
                    cards += sign(c) * card_weights["ability_ready"]
            if not s.positions[zone(c, "DECK")]:
                resources -= sign(c) * w["resource_penalties"]["empty_deck"]
        terminal = 2 if s.winner == team else -2 if s.winner == enemy else 0
        return PositionValue(float(actual), map_value, cards, resources, actual + map_value + cards + resources,
                             terminal, tuple(point_values), tuple(influences))

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


def forecast_site(site, site_id, rates, opportunities, sign, advantage, weights):
    """Discount when each scoring opportunity occurs, including gaps between seats."""
    horizon = weights["horizon"]
    discount = weights["time_discount"]
    held = any(site_id in values for values in rates.values())
    if held:
        schedule = [(delay, seat, rates[seat][site_id]) for delay, seat in opportunities if site_id in rates[seat]][:horizon]
        baseline = sum(vp * discount ** delay for delay, _, vp in schedule)
        value = sum(vp * discount ** delay * sign(seat) *
                    (1 - weights["threat_weight"] * max(0, -sign(seat) * advantage)) for delay, seat, vp in schedule)
    else:
        # A neutral site is a hypothetical opportunity for one seat. Multiplying
        # it by every eligible seat would value the same future capture repeatedly.
        eligible = next((seat for _, seat in opportunities if seat in site["countries"] and sign(seat) * advantage > 0), None)
        schedule = [(delay, seat, site["vp"]) for delay, seat in opportunities if seat == eligible][:horizon]
        baseline = sum(vp * discount ** delay for delay, _, vp in schedule)
        value = baseline * weights["expansion_weight"] * advantage
    return {"baseline_vp": baseline, "value": value, "opportunities": [
        {"seat": seat, "delay": delay, "vp": vp, "projected_capture": not held} for delay, seat, vp in schedule
    ]}


def effect_potential(rules, state, seat, effect, weights):
    from .content_effects import condition_met
    if not condition_met(rules, state, seat, effect.get("when")):
        return 0.0
    kind = effect["type"]
    if kind == "choose-one":
        return max((sum(effect_potential(rules, state, seat, child, weights) for child in option["effects"])
                    for option in effect["options"]), default=0.0)
    if kind == "points":
        award = effect["side"]
        friendly = award in ("self", rules.team(seat))
        return weights["points"] * effect["amount"] * (1 if friendly else -1)
    return weights.get(kind, weights["default"])


class SimulationIncomplete(CoreError):
    pass


def candidate_key(row):
    return (row["complete"], row.get("terminal_rank", -3),
            row["q"] if row["q"] is not None else float("-inf"))


def acceptable_play(row):
    return row["complete"] and (not row.get("initial_card_score") or row["q"] > 0 or row["terminal_rank"] == 2)


class OneStepPlanner:
    def __init__(self, rules, weights):
        self.evaluator = ScoreEvaluator(rules, weights)

    def rank(self, client, country):
        return sorted(self.iter_rank(client, country), key=candidate_key, reverse=True)

    def iter_rank(self, client, country):
        view = client.observe()
        root = simulation_from_observation(self.evaluator.rules, view)
        before = self.evaluator._value(root, country)
        for command in client.legal_commands():
            if command["actor"] != country:
                continue
            nodes = [0]
            try:
                if command["type"] == "REALLOCATE":
                    yield self._rank_exchange(root, view, country, command, nodes)
                    continue
                candidate = CommandProcessor(root.rules, root.state)
                simulated_seat = CoreAPI(candidate, (country,))
                if command not in simulated_seat.legal_commands():
                    raise SimulationIncomplete("Action cannot be represented from this observation")
                simulated_seat.submit(command, include_history=False)
                value, choices = self._finish(candidate, country, nodes)
                initial = -self.evaluator.hand_slot_cost if command["type"] in ("PLAY_CARD", "USE_AIR") else 0.0
                yield {"command": deepcopy(command), "complete": True, "nodes": nodes[0],
                       "weights_digest": digest(self.evaluator.weights),
                       "actual_vp_delta": value.actual_vp - before.actual_vp,
                       "map_delta": value.map_value - before.map_value,
                       "card_delta": value.card_value - before.card_value + initial,
                       "resource_delta": value.resource_adjustment - before.resource_adjustment,
                       "q": value.total - before.total + initial, "terminal_rank": value.terminal_rank,
                       **({"initial_card_score": initial} if initial else {}),
                       "continuations": [choice for choice, _ in choices],
                       "choice_signatures": [signature for _, signature in choices]}
            except (SimulationIncomplete, ResolutionBudgetExceeded) as e:
                yield {"command": deepcopy(command), "complete": False,
                       "reason": str(e), "q": None, "nodes": nodes[0]}

    def _rank_exchange(self, root, view, country, command, nodes):
        """Evaluate the ordinary play of one representative of the estimated acquisition."""
        state, rules = root.state, root.rules
        legal = LegalActionGenerator(rules)

        def hand_value(card):
            return self.evaluator.hand_value(rules, state, country, card, legal)

        plans = payment_plans(rules, view, country, lambda card: -hand_value(card))
        plan = max(plans, key=lambda p: price(p, hand_value(rules.cards[p["definition"]]))["total"], default=None)
        if plan is None:
            raise SimulationIncomplete("No exchange plan from the seat's visible inventory")
        hand, discard = zone(country, "HAND"), zone(country, "DISCARD")
        target = plan["target"]
        stock = root
        if target is None:
            # A catalog representative replaces one unknown deck slot. No real deck token is used.
            projected = deepcopy(view)
            positions = projected["positions"]
            positions[zone(country, "DECK")]["count"] -= 1
            target = "estimate:reallocation:" + country
            projected["tokens"][target] = {"token_id": target, "definition_id": plan["definition"], "token_type": "CARD",
                "owner_country": country, "source_token_id": None, "position_id": hand}
            positions[hand]["tokens"].append(target)
            positions[hand]["count"] += 1
            stock = simulation_from_observation(self.evaluator.rules, projected)

        post_payment = stock.state
        changes = outcome("ESTIMATED_REALLOCATION")
        for key in plan["payment"]:
            stock.executor.transfer(post_payment, key, discard, changes)
        if plan["source"] == "discard":
            stock.executor.transfer(post_payment, target, hand, changes)
        ordinary = CommandProcessor(stock.rules, post_payment)
        before = self.evaluator._value(ordinary, country)
        outcomes = []
        for play in CoreAPI(ordinary, (country,)).legal_commands():
            if play["type"] == "PLAY_CARD" and play.get("card") == target:
                branch = CommandProcessor(ordinary.rules, ordinary.state)
                CoreAPI(branch, (country,)).submit(play, include_history=False)
                value, _ = self._finish(branch, country, nodes)
                outcomes.append(value)
        after = max(outcomes, key=lambda value: (value.terminal_rank, value.total), default=before)
        initial = -self.evaluator.hand_slot_cost if outcomes else 0.0
        plan = price(plan, after.total - before.total + initial)
        return {"command": deepcopy(command), "complete": True, "nodes": nodes[0],
                "weights_digest": digest(self.evaluator.weights),
                "actual_vp_delta": after.actual_vp - before.actual_vp,
                "map_delta": after.map_value - before.map_value,
                "card_delta": after.card_value - before.card_value + plan["discard_score"] + initial,
                "resource_delta": after.resource_adjustment - before.resource_adjustment + plan["deck_score"],
                "q": plan["total"], "terminal_rank": 0,
                "continuations": [], "choice_signatures": [], "reallocation": plan}

    def _finish(self, processor, country, nodes):
        nodes[0] += 1
        if nodes[0] > self.evaluator.weights["max_simulation_nodes"]:
            raise SimulationIncomplete("Within-card choice budget exceeded; no partial score assigned")
        state = processor.state
        if state.winner:
            # Terminal positions may retain irrelevant pending frame cleanup.
            return self.evaluator._value(processor, country), []
        if not state.decision:
            if state.stack:
                raise SimulationIncomplete("Resolution not complete")
            return self.evaluator._value(processor, country), []
        decision = state.decision
        actor = decision.required_country
        signature = (decision.decision_type, decision.continuation["type"], sorted(decision.legal_options))
        branches = []
        for command in CoreAPI(processor, (actor,)).legal_commands():
            branch = CommandProcessor(processor.rules, state)
            CoreAPI(branch, (actor,)).submit(command, include_history=False)
            value, trail = self._finish(branch, country, nodes)
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
