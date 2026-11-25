"""Construction routes and reachable scoring sites on a single visible position."""
from collections import Counter, defaultdict, deque
from dataclasses import replace
from heapq import heappop, heappush

from .model import ZoneRegistry, zone
from .regional_sites import active_hidden_sites
from .rules import LegalActionGenerator, MapGraph, RuleModifierResolver, SupplyResolver, board_units


BUILD_CARDS = {"build-army": "ARMY", "build-navy": "NAVY"}


def project_placement(rules, state, force, region):
    """Use a real reserve piece in an isolated board projection."""
    kind = "ARMY" if rules.regions[region]["type"] == "LAND" else "NAVY"
    reserve = zone(force, "RESERVE_" + kind)
    stored = state.positions[reserve]
    if not stored:
        return None
    projected = replace(state, tokens=dict(state.tokens), positions={
        **state.positions, reserve: list(stored), region: list(state.positions[region])})
    token = stored[0]
    ZoneRegistry.transfer(projected, token, region)
    return projected, token


def construction_cards(rules, state, force, *, spent=None):
    # A uses its own observation and B uses its own projected hand. Opaque
    # placeholders carry no construction kind and cannot increase this budget.
    seat = rules.force_controllers[force]
    hand = Counter(rules.cards.get(state.tokens[key].definition_id, {}).get("legacy_kind")
                   for key in state.positions[zone(seat, "HAND")] if key != spent)
    return {kind: min(hand[card], len(state.positions[zone(force, "RESERVE_" + kind)]))
            for card, kind in BUILD_CARDS.items()}


class ConstructionNetwork:
    def __init__(self, rules, state, force):
        self.rules, self.state, self.force = rules, state, force
        self.seat = rules.force_controllers[force]
        team = rules.team(force)
        self.land = {key for key, region in rules.regions.items() if region["type"] == "LAND"}
        board = board_units(rules, state)
        self.ports = {t.position_id for t in board if rules.team(t.owner_country) == team and
                      rules.units[t.definition_id]["kind"] == "ARMY"}
        self.occupied = {t.position_id for t in board if rules.team(t.owner_country) != team or
                         (t.owner_country == force and rules.units[t.definition_id]["kind"] != "AIR")}
        self.surface = {t.token_id: t.position_id for t in board if t.owner_country == force
                        and rules.units[t.definition_id]["kind"] != "AIR"}
        self.graph = MapGraph(rules).adjacency(state, self.seat, "build")
        self.supply = MapGraph(rules).adjacency(state, force, "supply")
        self.coasts = {region: neighbors & self.land for region, neighbors in self.graph.items()}
        self.needs_port = rules.settings.get("naval_army_base", False)
        self.land_sites = {r["id"] for r in rules.regions.values()
                           if r.get("home_country") == force or r.get("supply_source_for") == force}
        if rules.settings.get("supply_revision") == 2:
            self.land_sites.update(site["region"] for site in rules.sites.values() if not site.get("dynamic"))
            self.land_sites.update(t.position_id for t in state.tokens.values()
                                   if t.token_type == "SCORE_SOURCE" and t.position_id in self.land)
        self.side_ports = None
        self.supplied_origins = None

    def available_side_ports(self):
        if self.side_ports is None:
            legal = LegalActionGenerator(self.rules)
            supply = SupplyResolver(self.rules)
            self.side_ports = set()
            for place in legal.targets(self.state, self.seat, {"type": "BUILD_ARMY", "force": self.force}):
                if not any(neighbor not in self.land for neighbor in self.graph[place]):
                    continue
                projection, token = project_placement(self.rules, self.state, self.force, place)
                if token in supply.supplied(projection, self.force):
                    self.side_ports.add(place)
        return self.side_ports

    def distance(self, origin):
        """Count a coastal Army as an action when a future naval hop needs one."""
        if not self.needs_port:
            result, queue = {origin: 0}, deque([origin])
            while queue:
                current = queue.popleft()
                for target in self.graph[current] - result.keys():
                    result[target] = result[current] + 1
                    queue.append(target)
            return result
        ports = self.ports | ({origin} if origin in self.land else set())
        result, labels = {}, {}
        queue = [(0, origin, "")]
        while queue:
            distance, current, shore = heappop(queue)
            key = current, shore
            if key in labels:
                continue
            labels[key] = distance
            result.setdefault(current, distance)
            for target in self.graph[current]:
                if target in self.land:
                    heappush(queue, (distance + 1, target, target))
                elif self.coasts[target] & ports or shore in self.coasts[target]:
                    heappush(queue, (distance + 1, target, shore))
                else:
                    # The normal land-to-sea path already pays for its coastal
                    # step. A port built on the existing front is a side step.
                    for coast in self.coasts[target] & self.available_side_ports():
                        heappush(queue, (distance + 2, target, coast))
        return result

    def reachable(self, origin, armies, navies):
        """Explore construction chains with separate land and sea card budgets."""
        if not (armies or navies):
            return set()
        if self.supplied_origins is None:
            supplied = SupplyResolver(self.rules).supplied(self.state, self.force)
            self.supplied_origins = {self.surface[key] for key in supplied & self.surface.keys()}
        result = set()
        labels = defaultdict(list)
        queue = deque([(origin, 0, 0, frozenset())])
        while queue:
            current, army_used, navy_used, ports = queue.popleft()
            key = current, ports
            if any(a <= army_used and n <= navy_used for a, n in labels[key]):
                continue
            labels[key].append((army_used, navy_used))
            if current != origin and current not in self.occupied:
                result.add(current)
            for target in sorted(self.graph[current] & self.supplied_origins):
                queue.appendleft((target, army_used, navy_used, ports))
            for target in self.graph[current] - self.occupied:
                if target in self.land:
                    if army_used < armies and (target in self.supply[current] or target in self.land_sites):
                        more_ports = ports | {target} if self.needs_port and navy_used < navies else ports
                        queue.append((target, army_used + 1, navy_used, more_ports))
                elif navy_used < navies and target in self.supply[current]:
                    coast = self.coasts[target]
                    if not self.needs_port or coast & (self.ports | ports):
                        queue.append((target, army_used, navy_used + 1, ports))
                    elif army_used < armies:
                        for shore in coast & self.available_side_ports():
                            queue.append((target, army_used + 1, navy_used + 1, ports | {shore}))
        return result

    def score_sites(self, origins, *, spent=None):
        if self.force in self.rules.auxiliary_forces:
            return ()
        budget = construction_cards(self.rules, self.state, self.force, spent=spent)
        reachable = set().union(*(self.reachable(p, budget["ARMY"], budget["NAVY"]) for p in origins))
        if not reachable:
            return ()
        modifiers = Counter()
        for modifier in RuleModifierResolver(self.rules).resolve(self.state, self.seat):
            if modifier["kind"] == "score_bonus":
                modifiers[modifier["site"]] += modifier["amount"]
        dynamic = {t.definition_id for t in self.state.tokens.values() if t.token_type == "SCORE_SOURCE"
                   and t.position_id in reachable and t.owner_country in (None, self.seat)}
        sites = {key for key, definition in self.rules.sites.items() if
                 self.seat in definition["countries"] and definition["vp"] + modifiers[key] > 0
                 and (key in dynamic if definition.get("dynamic") else definition["region"] in reachable)}
        if self.rules.hidden_score_rules:
            definitions = {unit["kind"]: key for key, unit in self.rules.units.items()}
            for layer in active_hidden_sites(self.rules, self.state):
                if (layer.active(self.rules, self.state) and
                        any(layer.build_eligible(self.rules, self.state, self.force, region, definitions)
                            for region in reachable.intersection(layer.spaces))):
                    sites.add(layer.key)
        return tuple(sorted(sites))
