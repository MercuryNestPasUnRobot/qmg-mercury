"""Value the nearest scoring sites using current, visible board routes."""
from collections import Counter, defaultdict, deque

from .content_effects import action_for, condition_met, target_regions
from .definitions import BATTLES, CONSTRUCTION
from .model import Token, canonical
from .operations import ScoreResolver
from .positional_scoring import ActionDistanceScoring
from .rules import LegalActionGenerator, MapGraph, unit_rule_matches
from .score_units import ScoreUnits


class ScoringRoutes:
    def __init__(self, rules, state, pieces, weights, *, include_connections=True):
        self.rules = rules
        self.state = state
        self.weights = weights
        self.config = weights["route_forecast"]
        self.frontier = weights.get("frontier_scoring")
        self.units = ScoreUnits.from_weights(weights)
        self.pieces = pieces
        self.seat_rates = None
        self.direct_income = {}
        self.safe_sites = {}
        self.locations = {key: {site["region"]} for key, site in rules.sites.items() if not site.get("dynamic")}
        for token in state.tokens.values():
            if token.token_type == "SCORE_SOURCE" and token.position_id in rules.regions:
                if rules.sites[token.definition_id].get("dynamic"):
                    self.locations.setdefault(token.definition_id, set()).add(token.position_id)
        self.occupancy = defaultdict(Counter)
        self.surface = defaultdict(set)
        self.units_by_region = defaultdict(list)
        for piece in pieces:
            team = rules.team(piece.owner_country)
            self.occupancy[piece.position_id][team] += 1
            self.units_by_region[piece.position_id].append(piece)
            if rules.units[piece.definition_id]["kind"] != "AIR":
                self.surface[piece.position_id].add(team)
        self.adjacency = {}
        self.region_weights = {}
        self.cache = {}
        self.target_scores = {}
        self.action_scores = {}
        self.near_site_distances = {}
        self.attack_victims = {}
        self.removal_routes = {}
        self.position = (ActionDistanceScoring(rules, state, pieces, self.locations, self.units,
                                              self.config["nearest_sites"], include_connections=include_connections)
                         if self.weights.get("action_distance_scoring") else None)

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
        team = self.rules.team(force)
        if not any(site in rates for seat, rates in self.scoring_rates().items() if self.rules.team(seat) == team):
            return False
        if (force, site) not in self.safe_sites:
            enemy = "allies" if team == "axis" else "axis"
            locations = self.locations[site]
            approach = locations.union(*(self.adjacency[force][region] for region in locations))
            self.safe_sites[force, site] = all(enemy not in self.surface[p] for p in approach)
        return self.safe_sites[force, site]

    def scoring_rates(self):
        if self.seat_rates is None:
            scorer = ScoreResolver(self.rules)
            supply_cache = {}
            self.seat_rates = {seat: scorer.sites(self.state, seat, pieces=self.pieces, supply_cache=supply_cache)
                               for seat in self.rules.countries}
        return self.seat_rates

    def direct_site_gain(self, force, region):
        key = force, region
        if key not in self.direct_income:
            sites = {site for site, locations in self.locations.items() if region in locations}
            if not sites:
                return 0
            seat = self.rules.force_controllers[force]
            team = self.rules.team(force)
            kind = "ARMY" if self.rules.regions[region]["type"] == "LAND" else "NAVY"
            unit = next((key for key, definition in self.rules.units.items() if definition["kind"] == kind), None)
            if unit is None:
                return 0
            candidate = Token("projected-construction", unit, "UNIT", force, region)
            pieces = [*self.pieces, candidate]
            scorer = ScoreResolver(self.rules)
            before = self.scoring_rates()
            supply_cache = {}
            change = 0
            for ally in self.rules.countries:
                if self.rules.team(ally) == team and (ally == seat or sites.intersection(before[ally])):
                    after = scorer.sites(self.state, ally, pieces=pieces, supply_cache=supply_cache)
                    change += sum(after.get(site, 0) - before[ally].get(site, 0) for site in sites)
            self.direct_income[key] = change
        return self.direct_income[key]

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

    def _removable_targets(self, force, region):
        key = force, region
        if key not in self.attack_victims:
            kind = "ARMY" if self.rules.regions[region]["type"] == "LAND" else "NAVY"
            team = self.rules.team(force)
            self.attack_victims[key] = [t for t in self.units_by_region[region] if
                self.rules.team(t.owner_country) != team and self.rules.units[t.definition_id]["kind"] == kind
                and not unit_rule_matches(self.rules, self.state, t, "protection")]
        return self.attack_victims[key]

    def removal_value(self, token_id, *, local_only=False):
        victim = self.state.tokens[token_id]
        if unit_rule_matches(self.rules, self.state, victim, "protection"):
            return 0.0
        if self.position:
            # Price the denied position from its owner's side, plus one lost action.
            return self.units.action + self.position.value(
                victim.owner_country, victim.position_id, excluded=token_id)
        return self._without_unit(token_id).target_value(
            victim.owner_country, victim.position_id, construction=True, local_only=local_only)

    def _without_unit(self, token_id):
        if token_id not in self.removal_routes:
            remaining = [t for t in self.pieces if t.token_id != token_id]
            self.removal_routes[token_id] = ScoringRoutes(self.rules, self.state, remaining, self.weights)
        return self.removal_routes[token_id]

    def capture_value(self, force, region):
        if self.position:
            return sum(self.position.capture(force, region, self.rules.sites[site]["vp"])
                       for site, locations in self.locations.items() if region in locations)
        remaining = max(0, self.rules.settings["max_rounds"] - self.state.turn.round_id + 1)
        return self.units.vp_score(min(remaining, self.units.capture_rounds) * self.direct_site_gain(force, region))

    def target_value(self, force, region, *, battle=False, construction=False, local_only=False, include_capture=True):
        cache_key = (force, region, battle, construction, local_only, include_capture)
        if cache_key in self.target_scores:
            return self.target_scores[cache_key]
        if self.position:
            value = (max((self.removal_value(t.token_id) for t in self._removable_targets(force, region)), default=0)
                     if battle else self.position.value(force, region))
            self.target_scores[cache_key] = value
            return value
        if battle and self.weights.get("mirrored_attacks"):
            victims = self._removable_targets(force, region)
            value = max((self.removal_value(t.token_id, local_only=local_only) for t in victims), default=0.0)
            if victims and not local_only:
                value += self.weights["military_weights"][self.rules.units[victims[0].definition_id]["kind"]]
            self.target_scores[cache_key] = value
            return value
        team = self.rules.team(force)
        enemy = "allies" if team == "axis" else "axis"
        victim = enemy in self.surface[region]
        total = self.frontier["battle_target"] if self.frontier and battle and victim else 0.0
        construction_factors = self.construction_factors(force, region) if construction else {}
        income_weight = (self.frontier or {}).get("direct_site_income", 0) if construction else 0
        if construction and self.units.enabled and include_capture:
            total += self.capture_value(force, region)
        elif income_weight:
            total += income_weight * self.direct_site_gain(force, region)
        for key, (distance, factor) in self.nearest(force, region).items():
            if distance > 2 and local_only:
                continue
            if self.frontier and distance <= 2:
                already_allied = (construction and (self.units.enabled or income_weight)
                                  and distance == 0 and team in self.surface[region])
                if already_allied or self.safely_held(force, key):
                    factor *= self.frontier["friendly_target"]
                factor *= construction_factors.get(key, 1)
                if battle and not victim:
                    if local_only:
                        continue
                    factor *= self.frontier["friendly_target"]
            total += self.units.vp_score(self.rules.sites[key]["vp"]) * self.distance_weight(distance) * factor
        self.target_scores[cache_key] = total
        return total

    def paper_bonus(self, force, region, *, battle=False):
        if self.position:
            return self.target_value(force, region, battle=battle)
        if self.units.enabled and battle and self.weights.get("mirrored_attacks"):
            return max((self._without_unit(t.token_id).paper_bonus(t.owner_country, region)
                        for t in self._removable_targets(force, region)), default=0.0)
        value = self.target_value(force, region, battle=battle, construction=not battle, local_only=True,
                                  include_capture=not self.units.enabled)
        future = min(self.units.vp_score(self.frontier["paper_cap"]), self.frontier["paper_fraction"] * value)
        fixed = self.capture_value(force, region) if not battle and self.units.enabled else 0
        return fixed + future

    def best_action_bonus(self, seat, op, *, attack_premium=0):
        """Price a concrete, immediately available placement or battle from the observed board."""
        cache_key = seat, canonical(op), attack_premium
        if cache_key in self.action_scores:
            return self.action_scores[cache_key]
        if op["type"] == "CARD_EFFECT":
            effect = op["effect"]
            if effect["type"] == "battle" and effect["kind"] == "either":
                return max(self.best_action_bonus(seat, {**op, "effect": {**effect, "kind": kind}},
                                                  attack_premium=attack_premium)
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
        force, battle = action.get("force", seat), action["type"] in BATTLES
        if battle and self.weights.get("mirrored_attacks"):
            targets = [p for p in targets if self._removable_targets(force, p)]
        bonus = max((self.paper_bonus(force, p, battle=battle) + attack_premium for p in targets), default=0.0)
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
