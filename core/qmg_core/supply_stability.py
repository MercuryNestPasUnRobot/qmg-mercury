"""Score a force's direct, visible supply options independently of end-of-turn supply."""
from collections import defaultdict
from functools import cached_property

from .rules import RuleModifierResolver, SupplyResolver, board_units


class SupplyStability:
    def __init__(self, rules, state, weight, *, pieces=None):
        self.rules = rules
        self.state = state
        self.weight = weight
        self.pieces = board_units(rules, state) if pieces is None else pieces
        self.supply = SupplyResolver(rules)

    @cached_property
    def by_force(self):
        if not self.weight:
            return {}
        units = defaultdict(list)
        occupied = defaultdict(set)
        ports = defaultdict(set)
        revised = self.rules.settings.get("supply_revision", 1) == 2
        for token in self.pieces:
            kind = self.rules.units[token.definition_id]["kind"]
            team = self.rules.team(token.owner_country)
            units[token.owner_country].append((token, kind))
            occupied[team].add(token.position_id)
            if revised or kind == "ARMY":
                ports[team].add(token.position_id)
        resolver = RuleModifierResolver(self.rules)
        modifiers = {force: resolver.resolve(self.state, force) for force in self.rules.force_controllers}
        scoring_regions = self.supply.scoring_sources(self.state)
        sources = {force: self.supply.army_sources(self.state, force, mods, scoring_regions)
                   for force, mods in modifiers.items()}
        result = {}
        for force, owned in units.items():
            team = self.rules.team(force)
            enemy = "allies" if team == "axis" else "axis"
            network = self.supply.network(force, modifiers[force])
            available = set().union(*(sources[member] for member in network)) - occupied[enemy]
            graph = self.supply.graph.adjacency(self.state, force, "supply")
            exempt = {kind for m in modifiers[force] if m["kind"] == "always_supplied"
                      for kind in m.get("kinds", ("army", "navy", "air"))}
            numerator, denominator = 0, len(owned)
            for token, kind in owned:
                region = token.position_id
                neighbors = graph[region]
                numerator += len(neighbors & available) + int(region in available) + int(kind.lower() in exempt)
                if kind == "NAVY":
                    numerator += len(neighbors & ports[team])
                    denominator += 1
            result[force] = self.weight * numerator / denominator
        return result

    def value(self, perspective):
        team = self.rules.team(perspective)
        return sum(score if self.rules.team(force) == team else -score for force, score in self.by_force.items())
