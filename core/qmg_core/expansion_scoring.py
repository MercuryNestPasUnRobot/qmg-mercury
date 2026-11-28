"""Construction routes and reachable scoring sites on a single visible position."""
from collections import Counter, defaultdict, deque
from functools import cached_property
from heapq import heappop, heappush

from .model import ZoneRegistry, zone
from .regional_sites import active_hidden_sites
from .rules import LegalActionGenerator, MapGraph, RuleModifierResolver, SupplyResolver, board_units
from .search_cache import board_dependencies


BUILD_CARDS = {"build-army": "ARMY", "build-navy": "NAVY"}


def project_placement(rules, state, force, region):
    """Use a real reserve piece in an isolated board projection."""
    kind = "ARMY" if rules.regions[region]["type"] == "LAND" else "NAVY"
    reserve = zone(force, "RESERVE_" + kind)
    stored = state.positions[reserve]
    if not stored:
        return None
    token = stored[0]
    return ZoneRegistry.projected_transfer(state, token, region), token


def construction_cards(rules, state, force, *, spent=None, discarded=()):
    # A uses its own observation and B uses its own projected hand. Opaque
    # placeholders carry no construction kind and cannot increase this budget.
    seat = rules.force_controllers[force]
    hand = Counter(rules.cards.get(state.tokens[key].definition_id, {}).get("legacy_kind")
                   for key in state.positions[zone(seat, "HAND")] if key != spent and key not in discarded)
    return {kind: min(hand[card], len(state.positions[zone(force, "RESERVE_" + kind)]))
            for card, kind in BUILD_CARDS.items()}


class ConstructionNetwork:
    def __init__(self, rules, state, force):
        self.rules, self.state, self.force = rules, state, force
        self.seat = rules.force_controllers[force]
        team = rules.team(force)
        self.land = {key for key, region in rules.regions.items() if region["type"] == "LAND"}
        self.graph = MapGraph(rules).adjacency(state, self.seat, "build")
        self.board = board = board_units(rules, state)
        self.ports = {t.position_id for t in board if rules.team(t.owner_country) == team and
                      rules.units[t.definition_id]["kind"] == "ARMY" and t.position_id in self.land and
                      self.graph[t.position_id] - self.land}
        self.occupied = {t.position_id for t in board if rules.team(t.owner_country) != team or
                         (t.owner_country == force and rules.units[t.definition_id]["kind"] != "AIR")}
        self.surface = {t.token_id: t.position_id for t in board if t.owner_country == force
                        and rules.units[t.definition_id]["kind"] != "AIR"}
        self.coasts = {region: neighbors & self.land for region, neighbors in self.graph.items()}
        self.needs_port = rules.settings.get("naval_army_base", False)
        self.side_ports = None
        self.supplied_origins = None
        self.hand_sites = {}
        self.starting_placements = {}

    @cached_property
    def supply(self):
        return MapGraph(self.rules).adjacency(self.state, self.force, "supply")

    @cached_property
    def land_sites(self):
        sites = {r["id"] for r in self.rules.regions.values()
                 if r.get("home_country") == self.force or r.get("supply_source_for") == self.force}
        if self.rules.settings.get("supply_revision") == 2:
            sites.update(site["region"] for site in self.rules.sites.values() if not site.get("dynamic"))
            sites.update(t.position_id for t in self.state.tokens.values()
                         if t.token_type == "SCORE_SOURCE" and t.position_id in self.land)
        return sites

    def available_side_ports(self):
        if self.side_ports is None:
            cache = self.rules.search_cache
            if cache is None:
                self.side_ports = self.find_side_ports()
            else:
                reserve = self.state.positions[zone(self.force, "RESERVE_ARMY")]
                key = self.force, next(iter(reserve), None), board_dependencies(self.rules, self.state, self.board)
                if key not in cache.side_ports:
                    cache.side_ports[key] = self.find_side_ports()
                self.side_ports = cache.side_ports[key]
        return self.side_ports

    def find_side_ports(self):
        legal = LegalActionGenerator(self.rules)
        supply = SupplyResolver(self.rules)
        ports = set()
        for place in legal.targets(self.state, self.seat, {"type": "BUILD_ARMY", "force": self.force}):
            if not any(neighbor not in self.land for neighbor in self.graph[place]):
                continue
            projection, token = project_placement(self.rules, self.state, self.force, place)
            if token in supply.supplied(projection, self.force):
                ports.add(place)
        return frozenset(ports)

    def distance(self, origin):
        """Count a coastal Army as an action when a future naval hop needs one."""
        if not self.needs_port:
            return MapGraph(self.rules).distances_on(origin, self.graph)
        ports = self.ports | ({origin} if origin in self.land else set())
        cache = self.rules.search_cache
        if cache is None:
            return self.port_distances(origin, ports)[0]
        routes = cache.construction_distances.setdefault((id(self.graph), origin, frozenset(ports)), {})
        # Port availability only matters if the route actually examines a missing coastal link.
        if None in routes:
            return routes[None]
        if routes and (side_ports := self.available_side_ports()) in routes:
            return routes[side_ports]
        distances, side_ports = self.port_distances(origin, ports)
        routes[side_ports] = distances
        return distances

    def port_distances(self, origin, ports):
        result, labels = {}, {}
        side_ports = None
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
                    if side_ports is None:
                        side_ports = self.available_side_ports()
                    for coast in self.coasts[target] & side_ports:
                        heappush(queue, (distance + 2, target, coast))
        return result, side_ports

    def reachable(self, origin, armies, navies, *, initial_ports=frozenset()):
        """Explore construction chains with separate land and sea card budgets."""
        if not (armies or navies):
            return set()
        if self.supplied_origins is None:
            supplied = SupplyResolver(self.rules).supplied(self.state, self.force)
            self.supplied_origins = {self.surface[key] for key in supplied & self.surface.keys()}
        result = set()
        labels = defaultdict(list)
        queue = deque([(origin, 0, 0, initial_ports)])
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
        return self.sites_in(reachable)

    def hand_score_sites(self, discarded=()):
        budget = construction_cards(self.rules, self.state, self.force, discarded=discarded)
        armies, navies = budget["ARMY"], budget["NAVY"]
        key = armies, navies
        if key not in self.hand_sites:
            if not (armies or navies):
                self.hand_sites[key] = frozenset()
            else:
                supply = SupplyResolver(self.rules)
                supplied = supply.supplied(self.state, self.force)
                reachable = set().union(*(self.reachable(self.surface[token], armies, navies)
                                          for token in supplied & self.surface.keys()))
                legal = LegalActionGenerator(self.rules)
                # A legal first build can also start from a home or a rule-granted landing site.
                for kind, available in budget.items():
                    if not available:
                        continue
                    if kind not in self.starting_placements:
                        positions = legal.targets(self.state, self.seat, {"type": "BUILD_" + kind, "force": self.force})
                        placements = []
                        for region in positions:
                            projected, token = project_placement(self.rules, self.state, self.force, region)
                            if token in supply.supplied(projected, self.force):
                                placements.append(region)
                        self.starting_placements[kind] = placements
                    for region in self.starting_placements[kind]:
                        reachable.add(region)
                        reachable.update(self.reachable(region, armies - int(kind == "ARMY"),
                                         navies - int(kind == "NAVY"),
                                         initial_ports=frozenset({region}) if kind == "ARMY" and self.needs_port else frozenset()))
                self.hand_sites[key] = frozenset(self.sites_in(reachable))
        return self.hand_sites[key]

    def sites_in(self, reachable):
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
