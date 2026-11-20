"""Deterministic VP-equivalent diagnostics, with bounded within-card choice search."""
from copy import deepcopy
from dataclasses import dataclass
import json
from math import isfinite
from pathlib import Path

from .api import CoreAPI, simulation_from_observation
from .definitions import RuleSet
from .engine import CommandProcessor, ResolutionBudgetExceeded
from .model import CoreError, TURN_ORDER, PHASES, zone, digest
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
        if any(type(n) not in (int, float) or not isfinite(n) or n < 0 for n in numbers) or not 0 <= weights["time_discount"] <= 1:
            raise CoreError("Evaluation weights must be finite nonnegative values")
        if weights["saturation"] <= 0 or type(weights["horizon"]) is not int or not 1 <= weights["horizon"] <= 20:
            raise CoreError("Invalid planning horizon or saturation")
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
                distances = MapGraph(rules).distances(t.position_id, s, t.owner_country)
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
            point_values.append({"site": site_id, "baseline_vp": baseline, "control": control,
                                 "friendly": friendly, "hostile": hostile, "advantage": advantage, "value": value})
            map_value += value
        cards, resources = 0.0, 0.0
        legal = LegalActionGenerator(rules)
        card_weights = w["card_value_weights"]
        for c in TURN_ORDER:
            hand = s.positions[zone(c, "HAND")]
            for token_id in hand:
                card = rules.cards[s.tokens[token_id].definition_id]
                if card.get("simulation_unknown"):
                    intrinsic = card_weights["unknown"]
                else:
                    # A deployable ability contributes once, through its card or its active replicas.
                    deployable = card["type"] == "RESPONSE" or (card["type"] == "STATUS" and not card.get("after"))
                    copies = sum(rules.abilities[a].get("copies", 1) for a in card.get("abilities", [])) if deployable else 0
                    intrinsic = copies * card_weights["ability_ready"]
                    for instruction in card["effects"]:
                        operation = instruction.get("operation", instruction)
                        multiplier = operation.get("amount", 1) if operation["type"] == "GAIN_VP" else operation.get("count", 1)
                        intrinsic += card_weights.get(operation["type"], card_weights.get(instruction["type"], 0)) * multiplier
                    if not legal.can_start_card(s, c, card):
                        intrinsic *= w["unusable_card_weight"]
                cards += sign(c) * intrinsic
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


class SimulationIncomplete(CoreError):
    pass


class OneStepPlanner:
    def __init__(self, rules, weights):
        self.evaluator = ScoreEvaluator(rules, weights)

    def rank(self, client, country):
        root = simulation_from_observation(self.evaluator.rules, client.observe())
        before = self.evaluator._value(root, country)
        results = []
        for command in client.legal_commands():
            if command["actor"] != country:
                continue
            nodes = [0]
            candidate = CommandProcessor(root.rules, root.state)
            simulated_seat = CoreAPI(candidate, (country,))
            try:
                if command not in simulated_seat.legal_commands():
                    raise SimulationIncomplete("Action cannot be represented from this observation")
                simulated_seat.submit(command)
                value, choices = self._finish(candidate, country, nodes)
                results.append({"command": deepcopy(command), "complete": True, "nodes": nodes[0], "weights_digest": digest(self.evaluator.weights),
                                "actual_vp_delta": value.actual_vp - before.actual_vp,
                                "map_delta": value.map_value - before.map_value,
                                "card_delta": value.card_value - before.card_value,
                                "resource_delta": value.resource_adjustment - before.resource_adjustment,
                                "q": value.total - before.total, "terminal_rank": value.terminal_rank,
                                "continuations": choices})
            except (SimulationIncomplete, ResolutionBudgetExceeded) as e:
                results.append({"command": deepcopy(command), "complete": False, "reason": str(e), "q": None, "nodes": nodes[0]})
        return sorted(results, key=lambda r: (r["complete"], r.get("terminal_rank", -3), r["q"] if r["q"] is not None else float("-inf")), reverse=True)

    def _finish(self, processor, country, nodes):
        nodes[0] += 1
        if nodes[0] > self.evaluator.weights["max_simulation_nodes"]:
            raise SimulationIncomplete("Within-card choice budget exceeded; no partial score assigned")
        if processor.state.winner:
            # Terminal positions may retain irrelevant pending frame cleanup.
            return self.evaluator._value(processor, country), []
        if not processor.state.decision:
            if processor.state.stack:
                raise SimulationIncomplete("Resolution not complete")
            return self.evaluator._value(processor, country), []
        actor = processor.state.decision.required_country
        branches = []
        for command in CoreAPI(processor, (actor,)).legal_commands():
            branch = CommandProcessor(processor.rules, processor.state)
            CoreAPI(branch, (actor,)).submit(command)
            value, trail = self._finish(branch, country, nodes)
            branches.append((value, [command] + trail))
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
