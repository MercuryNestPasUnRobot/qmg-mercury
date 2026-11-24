"""Value the nearest scoring sites using current, visible board routes."""
from collections import Counter, defaultdict, deque

from .content_effects import action_for, condition_met, target_regions
from .definitions import BATTLES, CONSTRUCTION
from .model import canonical
from .operations import ScoreResolver
from .rules import LegalActionGenerator, MapGraph


class ScoringRoutes:
    def __init__(self, rules, state, pieces, weights):
        self.rules = rules
        self.state = state
        self.weights = weights
        self.config = weights["route_forecast"]
        self.frontier = weights.get("frontier_scoring")
        self.pieces = pieces
        self.held_by = None
        self.safe_sites = {}
        self.locations = {key: {site["region"]} for key, site in rules.sites.items() if not site.get("dynamic")}
        for token in state.tokens.values():
            if token.token_type == "SCORE_SOURCE" and token.position_id in rules.regions:
                if rules.sites[token.definition_id].get("dynamic"):
                    self.locations.setdefault(token.definition_id, set()).add(token.position_id)
        self.occupancy = defaultdict(Counter)
        self.surface = defaultdict(set)
        for piece in pieces:
            team = rules.team(piece.owner_country)
            self.occupancy[piece.position_id][team] += 1
            if rules.units[piece.definition_id]["kind"] != "AIR":
                self.surface[piece.position_id].add(team)
        self.adjacency = {}
        self.region_weights = {}
        self.cache = {}
        self.target_scores = {}
        self.action_scores = {}
        self.near_site_distances = {}

    def _site_route(self, paths, force, start, end):
        distance, factor = paths[end]
        if self.frontier and distance <= 2:
            team = self.rules.team(force)
            enemy = "allies" if team == "axis" else "axis"
            if distance == 2:
                adjacent = self.adjacency[force]
                factor = max(self.config["friendly_unit"] ** self.occupancy[middle][team] *
                             self.frontier["near_enemy"] ** self.occupancy[middle][enemy]
                             for middle in adjacent[start] & adjacent[end])
            if enemy in self.surface[end]:
                factor *= self.frontier["near_enemy"]
        return distance, -factor

    def nearest(self, force, start):
        key = force, start
        if key not in self.cache:
            team = self.rules.team(force)
            if force not in self.adjacency:
                self.adjacency[force] = MapGraph(self.rules).adjacency(self.state, force)
            if team not in self.region_weights:
                self.region_weights[team] = {
                    region: self.config["friendly_unit"] ** counts[team] *
                            self.config["enemy_unit"] ** sum(n for side, n in counts.items() if side != team)
                    for region, counts in self.occupancy.items()
                }
            paths = shortest_route_weights(self.adjacency[force], start, self.region_weights[team])
            reachable = []
            for site, regions in self.locations.items():
                options = [self._site_route(paths, force, start, region) for region in regions if region in paths]
                if options:
                    distance, negative_weight = min(options)
                    reachable.append((distance, site, -negative_weight))
            self.cache[key] = {site: (distance, factor) for distance, site, factor in
                               sorted(reachable)[:self.config["nearest_sites"]]}
        return self.cache[key]

    def distance_weight(self, distance):
        nearby = self.weights["distance_weights"]
        if distance < len(nearby):
            return nearby[distance]
        return nearby[-1] * self.config["distance_tail_decay"] ** (distance - len(nearby) + 1)

    def safely_held(self, force, site):
        if self.held_by is None:
            scorer = ScoreResolver(self.rules)
            self.held_by = {(self.rules.team(seat), key) for seat in self.rules.countries
                            for key in scorer.sites(self.state, seat, pieces=self.pieces)}
        team = self.rules.team(force)
        if (team, site) not in self.held_by:
            return False
        if (force, site) not in self.safe_sites:
            enemy = "allies" if team == "axis" else "axis"
            locations = self.locations[site]
            approach = locations.union(*(self.adjacency[force][region] for region in locations))
            self.safe_sites[force, site] = all(enemy not in self.surface[p] for p in approach)
        return self.safe_sites[force, site]

    def _site_distances_within_two(self, force, site):
        key = force, site
        if key not in self.near_site_distances:
            adjacency = self.adjacency[force]
            reached = dict.fromkeys(self.locations[site], 0)
            edge = set(reached)
            for distance in (1, 2):
                edge = {n for p in edge for n in adjacency[p] if n not in reached}
                reached.update(dict.fromkeys(edge, distance))
            self.near_site_distances[key] = reached
        return self.near_site_distances[key]

    def construction_factors(self, force, region, *, already_present=False, ignored_sites=()):
        decay = (self.frontier or {}).get("build_friendly_neighbor", 1)
        if decay == 1:
            return {}
        sites = self.nearest(force, region)
        team = self.rules.team(force)
        nearby = {p: self.occupancy[p][team] for p in self.adjacency[force][region] | {region}}
        nearby[region] -= int(already_present)
        nearby = {p: count for p, count in nearby.items() if count > 0}
        factors = {}
        # Nearest sites are ordered by distance, then ID. Charge each friendly unit
        # once, to the first site where it is at least as close as the target.
        for site, (distance, _) in sites.items():
            if distance > 2 or not nearby:
                break
            if site in ignored_sites:
                continue
            distances = self._site_distances_within_two(force, site)
            qualifying = [p for p in nearby if distances.get(p, 3) <= distance]
            if qualifying:
                factors[site] = decay ** sum(nearby.pop(p) for p in qualifying)
        return factors

    def target_value(self, force, region, *, battle=False, construction=False, local_only=False):
        cache_key = (force, region, battle, construction, local_only)
        if cache_key in self.target_scores:
            return self.target_scores[cache_key]
        team = self.rules.team(force)
        enemy = "allies" if team == "axis" else "axis"
        victim = enemy in self.surface[region]
        total = self.frontier["battle_target"] if self.frontier and battle and victim else 0.0
        construction_factors = self.construction_factors(force, region) if construction else {}
        for key, (distance, factor) in self.nearest(force, region).items():
            if distance > 2 and local_only:
                continue
            if self.frontier and distance <= 2:
                if self.safely_held(force, key):
                    factor *= self.frontier["friendly_target"]
                factor *= construction_factors.get(key, 1)
                if battle and not victim:
                    if local_only:
                        continue
                    factor *= self.frontier["friendly_target"]
            total += self.rules.sites[key]["vp"] * self.distance_weight(distance) * factor
        self.target_scores[cache_key] = total
        return total

    def paper_bonus(self, force, region, *, battle=False):
        value = self.target_value(force, region, battle=battle, construction=not battle, local_only=True)
        return min(self.frontier["paper_cap"], self.frontier["paper_fraction"] * value)

    def best_action_bonus(self, seat, op):
        """Price a concrete, immediately available placement or battle from the observed board."""
        cache_key = seat, canonical(op)
        if cache_key in self.action_scores:
            return self.action_scores[cache_key]
        if op["type"] == "CARD_EFFECT":
            effect = op["effect"]
            if effect["type"] == "battle" and effect["kind"] == "either":
                return max(self.best_action_bonus(seat, {**op, "effect": {**effect, "kind": kind}})
                           for kind in ("army", "navy"))
            if (effect["type"] not in ("choose-space", "build", "recruit", "battle") or
                    effect.get("kind", "army") not in ("army", "navy") or
                    any(k in effect for k in ("nearBuilt", "nearAirAction", "atAirAction", "battleRange", "battleDistance"))):
                return 0
            chooser = effect.get("chooser", seat)
            action = action_for(effect, chooser)
            if (action["force"] not in self.rules.force_controllers or self.rules.team(chooser) != self.rules.team(seat)
                    or self.rules.team(action["force"]) != self.rules.team(seat)
                    or not condition_met(self.rules, self.state, chooser, effect.get("when"))):
                return 0
            targets = target_regions(self.rules, self.state, chooser, effect)
        elif op["type"] in BATTLES | CONSTRUCTION:
            action = op
            targets = LegalActionGenerator(self.rules).targets(self.state, seat, op)
            if op.get("target", "$choice") != "$choice":
                targets = [p for p in targets if p == op["target"]]
        else:
            return 0
        bonus = max((self.paper_bonus(action.get("force", seat), p, battle=action["type"] in BATTLES)
                     for p in targets), default=0.0)
        self.action_scores[cache_key] = bonus
        return bonus


def shortest_route_weights(adjacency, start, region_weights):
    # Endpoints are excluded. Among equally short routes keep the largest importance.
    paths, queue = {start: (0, 1.0)}, deque([start])
    while queue:
        region = queue.popleft()
        distance, importance = paths[region]
        if region != start:
            importance *= region_weights.get(region, 1.0)
        for neighbor in adjacency[region]:
            previous = paths.get(neighbor)
            if previous is None:
                paths[neighbor] = distance + 1, importance
                queue.append(neighbor)
            elif previous[0] == distance + 1 and importance > previous[1]:
                paths[neighbor] = distance + 1, importance
    return paths
