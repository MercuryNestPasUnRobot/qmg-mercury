"""Strategic site value in VP and action opportunities, on public map data."""
from collections import defaultdict, deque
from math import inf

from .definitions import PER_UNIT_SITE
from .expansion_scoring import ConstructionNetwork
from .model import TURN_ORDER, zone
from .regional_sites import active_hidden_sites
from .rules import MapGraph, RuleModifierResolver, home_regions, occupied_home_forces
from .score_units import STANDARD_SITE_VP


class ActionDistanceScoring:
    def __init__(self, rules, state, pieces, locations, units, nearest_sites, *,
                 include_connections=True, construction_rules=False, stacked_sites=False):
        self.rules = rules
        self.state = state
        self.units = units
        self.stacked_sites = stacked_sites
        self.layers = {layer.key: layer for layer in active_hidden_sites(rules, state)
                       if layer.award.mode != "absence" and layer.active(rules, state)}
        self.locations = {**locations, **{key: set(layer.spaces) for key, layer in self.layers.items()}}
        self.unit_definitions = {unit["kind"]: key for key, unit in rules.units.items()}
        self.nearest_sites = nearest_sites
        self.pieces = tuple(t for t in pieces if rules.units[t.definition_id]["kind"] != "AIR")
        self.blocked = occupied_home_forces(rules, state, self.pieces)
        self.homes = {}
        if rules.settings.get("occupied_home_blocks_scoring") and (units.home_importance or units.auxiliary_home_importance):
            self.homes = {owner: home_regions(rules, state, owner) for owner in rules.force_controllers}
        self.remaining = 0 if state.winner else max(0, rules.settings["max_rounds"] - state.turn.round_id + 1)
        self.include_connections = include_connections
        self.construction_rules = construction_rules
        self.construction_networks, self.construction_paths = {}, {}
        self.graphs, self.paths, self.bases, self.sites_by_position = {}, {}, {}, {}
        self.income_changes = {}
        self.source_owners = defaultdict(set)
        for token in state.tokens.values():
            if token.token_type == "SCORE_SOURCE" and token.position_id in rules.regions:
                self.source_owners[token.definition_id, token.position_id].add(token.owner_country)
        self.has_income_changes = include_connections and any(
            m["kind"] == "score_bonus"
            for seat in TURN_ORDER for token in state.positions.get(zone(seat, "STATUS"), ())
            for m in rules.cards[state.tokens[token].definition_id].get("modifiers", ()))

    def distances(self, force, start):
        key = force, start
        if key not in self.paths:
            if force not in self.graphs:
                self.graphs[force] = MapGraph(self.rules).adjacency(
                    self.state, force, include_connection_modifiers=self.include_connections)
            adjacent = self.graphs[force]
            distance, queue = {start: 0}, deque([start])
            while queue:
                region = queue.popleft()
                for neighbor in adjacent[region]:
                    if neighbor not in distance:
                        distance[neighbor] = distance[region] + 1
                        queue.append(neighbor)
            self.paths[key] = distance
        return self.paths[key]

    def scoring_rounds(self, seat):
        turn = self.state.turn
        order = TURN_ORDER.index(seat)
        scored = order < turn.country_index or (
            order == turn.country_index and turn.phase in ("Victory", "Discard", "Draw"))
        return max(0, self.remaining - int(scored))

    def capture(self, force, region, vp, *, distance_offset=0):
        key = force, region, vp, distance_offset
        if key not in self.bases:
            seat = self.rules.force_controllers[force]
            team = self.rules.team(force)
            cap = self.units.site_score(vp * self.scoring_rounds(seat))
            # The occupying move has already cleared hostile units on the site itself.
            nearest = min((self.distances(t.owner_country, t.position_id).get(region, inf) + distance_offset
                           for t in self.pieces if t.position_id != region and self.rules.team(t.owner_country) != team),
                          default=inf)
            initial = self.units.site_score(vp * self.units.capture_rounds)
            self.bases[key] = cap if nearest == inf else max(0, min(cap, initial + self.units.distance_score(nearest - 1)))
        return self.bases[key]

    def distance_value(self, base, distance):
        return 0 if distance == inf else max(0, base - self.units.distance_score(distance))

    def proportional_base(self, force, region, vp, *, distance_offset=0):
        return self.capture(force, region, STANDARD_SITE_VP, distance_offset=distance_offset) * vp / STANDARD_SITE_VP

    def site_base(self, force, region, site, *, distance_offset=0):
        if force in self.blocked:
            return 0
        if layer := self.layers.get(site):
            if layer.owner in self.blocked or not layer.build_eligible(self.rules, self.state, force, region, self.unit_definitions):
                return 0
            return self.proportional_base(layer.owner, region, layer.award.amount, distance_offset=distance_offset)
        definition = self.rules.sites[site]
        seat = self.rules.force_controllers[force]
        owners = self.source_owners[site, region] if definition.get("dynamic") else {None}
        if seat not in definition["countries"] or (None not in owners and seat not in owners):
            return 0
        printed = definition["vp"]
        if self.has_income_changes:
            if seat not in self.income_changes:
                changes = defaultdict(int)
                for modifier in RuleModifierResolver(self.rules).resolve(self.state, seat):
                    if modifier["kind"] == "score_bonus":
                        changes[modifier["site"]] += modifier["amount"]
                self.income_changes[seat] = changes
            change = self.income_changes[seat].get(site, 0)
            if change:
                return self.proportional_base(force, region, max(0, printed + change), distance_offset=distance_offset)
        if definition.get("dynamic") or definition.get("scoring") == PER_UNIT_SITE:
            return self.proportional_base(force, region, printed, distance_offset=distance_offset)
        return self.capture(force, region, printed, distance_offset=distance_offset)

    def construction_distances(self, force, region):
        key = force, region
        if key not in self.construction_paths:
            if force not in self.construction_networks:
                self.construction_networks[force] = ConstructionNetwork(self.rules, self.state, force)
            self.construction_paths[key] = self.construction_networks[force].distance(region)
        return self.construction_paths[key]

    def site_values(self, force, region, *, excluded=None, construction=False, distance_offset=0):
        """Value each chosen score layer after distance and unit adjustments."""
        construction = construction and self.construction_rules
        key = force, region, excluded, construction, distance_offset
        if key in self.sites_by_position:
            return self.sites_by_position[key]
        from_start = self.distances(force, region)
        practical = self.construction_distances(force, region) if construction else from_start
        penalty = 2 if construction and force in self.rules.auxiliary_forces else 1
        team = self.rules.team(force)
        routes = []
        for site, locations in self.locations.items():
            for end in sorted(locations):
                distance = practical.get(end)
                if distance is None:
                    continue
                distance += distance_offset
                base = self.site_base(force, end, site, distance_offset=distance_offset)
                if base <= 0:
                    continue
                value = base - self.units.distance_score(penalty * distance)
                if value >= 0:
                    routes.append((distance, site, end, value))
        # Multiple physical copies of the same score source still represent one site.
        nearest = {}
        for row in sorted(routes):
            nearest.setdefault(row[1], row)
        if self.stacked_sites:
            locations = dict.fromkeys(row[2] for row in nearest.values())
            places = set(list(locations)[:self.nearest_sites])
            routes = [row for row in nearest.values() if row[2] in places]
        else:
            routes = list(nearest.values())[:self.nearest_sites]
        scores = {site: (distance, raw) for distance, site, _, raw in routes}
        changes = defaultdict(float)
        for token in self.pieces:
            if token.token_id == excluded:
                continue
            position = token.position_id
            friendly = self.rules.team(token.owner_country) == team
            if self.stacked_sites and not friendly:
                continue
            best, best_site = 0, None
            for distance, site, end, _ in routes:
                to_end = self.distances(force, end).get(position, inf)
                geographic_distance = from_start.get(end, inf)
                same_distance = to_end == geographic_distance and geographic_distance != inf
                if not (to_end < geographic_distance or (same_distance and (self.stacked_sites or distance == 0))):
                    continue
                if not (self.stacked_sites and same_distance) and from_start.get(position, inf) + to_end != geographic_distance:
                    continue
                layer = self.layers.get(site)
                per_unit = (layer.award.mode == "each-unit" if layer else
                            self.rules.sites[site].get("scoring") == PER_UNIT_SITE)
                if friendly and distance == distance_offset and per_unit:
                    continue  # An allied unit here does not share the new unit's VP.
                base = self.site_base(token.owner_country, end, site, distance_offset=distance_offset)
                change = self.distance_value(base, to_end + distance_offset) / 2
                if self.stacked_sites:
                    changes[site] -= change
                elif change > best:
                    best, best_site = change, site
            if best_site is not None:
                changes[best_site] += -best if friendly else best
        scores = {site: (distance, max(0, value + changes[site])) for site, (distance, value) in scores.items()}
        self.sites_by_position[key] = scores
        return scores

    def home_values(self, force, region, *, construction=False, distance_offset=0, local_only=False):
        if not self.homes:
            return {}
        practical = construction and self.construction_rules
        distances = self.construction_distances(force, region) if practical else self.distances(force, region)
        step = 2 if practical and force in self.rules.auxiliary_forces else 1
        values = {}
        for owner, homes in self.homes.items():
            distance = min((distances.get(home, inf) for home in homes), default=inf)
            if distance == inf or (local_only and distance):
                continue
            base = self.units.auxiliary_home_importance if owner in self.rules.auxiliary_forces else self.units.home_importance
            if score := self.distance_value(base, step * (distance + distance_offset)):
                values[owner] = score
        return values

    def home_board_values(self, country):
        best = defaultdict(float)
        if self.homes:
            for token in self.pieces:
                force = token.owner_country
                for owner, value in self.home_values(force, token.position_id).items():
                    key = owner, self.rules.team(force)
                    best[key] = max(best[key], value)
        team = self.rules.team(country)
        enemy = "allies" if team == "axis" else "axis"
        return {"home:" + owner: best[owner, team] - best[owner, enemy]
                for owner in dict.fromkeys(owner for owner, _ in best)}

    def value(self, force, region, *, excluded=None, construction=False, distance_offset=0):
        sites = self.site_values(force, region, excluded=excluded, construction=construction, distance_offset=distance_offset)
        homes = self.home_values(force, region, construction=construction, distance_offset=distance_offset)
        return sum(value for _, value in sites.values()) + sum(homes.values())

    def best_adjacent_site(self, force, regions):
        regions = set(regions)
        return max((self.distance_value(self.site_base(force, end, site), 1)
                    for site, locations in self.locations.items() for end in locations
                    if any(self.distances(force, end).get(region) == 1 for region in regions)), default=0)

    def board_values(self, country, rates):
        """A site contributes at most once per side, even with several allied units."""
        best, held, influences = defaultdict(float), defaultdict(float), []
        for token in self.pieces:
            force, region = token.owner_country, token.position_id
            team = self.rules.team(force)
            seat = self.rules.force_controllers[force]
            for site, (distance, score) in self.site_values(force, region, excluded=token.token_id).items():
                key = site, team
                if distance == 0:
                    beneficiary = self.layers[site].owner if site in self.layers else seat
                    if site not in rates[beneficiary]:
                        continue
                    score = self.site_base(force, region, site)
                    held[key] = max(held[key], score)
                best[key] = max(best[key], score)
                influences.append({"token": token.token_id, "site": site, "distance": distance,
                                   "score_influence": score, "military_influence": score,
                                   "supply_weight": 1, "route_factor": 1})
        own = self.rules.team(country)
        enemy = "allies" if own == "axis" else "axis"
        points = []
        for site, regions in self.locations.items():
            layer = self.layers.get(site)
            definition = self.rules.sites.get(site, {})
            value = best[site, own] - best[site, enemy]
            if definition.get("dynamic"):
                value = self.dynamic_site_value(country, site, regions)
            controlled = held[site, own] - held[site, enemy]
            points.append({"site": site, "value": value, "fixed_score": controlled,
                           "long_term_score": value - controlled, "control": int(bool(held[site, own])) -
                           int(bool(held[site, enemy])), "baseline_vp": layer.award.amount if layer else definition["vp"],
                           "friendly": best[site, own], "hostile": best[site, enemy], "advantage": value})
        points.extend({"site": site, "value": value, "fixed_score": 0, "long_term_score": value}
                      for site, value in self.home_board_values(country).items())
        return sum(row["value"] for row in points), points, influences

    def closest_site_interest(self, owner, region, vp, countries=None):
        """One nearest unit per side, keeping the highest impact among ties."""
        teams = {}
        for token in self.pieces:
            force = token.owner_country
            if countries is not None and self.rules.force_controllers[force] not in countries:
                continue
            distance = self.distances(force, token.position_id).get(region, inf)
            score = self.distance_value(self.proportional_base(force, region, vp), distance)
            team = self.rules.team(force)
            teams[team] = min(teams.get(team, (inf, 0)), (distance, -score))
        team = self.rules.team(owner)
        return -teams.get(team, (inf, 0))[1] + sum(row[1] for side, row in teams.items() if side != team)

    def force_site_interest(self, force, region, vp, *, contested=False):
        distance = min((self.distances(force, token.position_id).get(region, inf) for token in self.pieces
                        if token.owner_country == force), default=inf)
        value = self.distance_value(self.proportional_base(force, region, vp), distance)
        if not contested:
            return value
        opponents = []
        for token in self.pieces:
            enemy = token.owner_country
            if self.rules.team(enemy) != self.rules.team(force):
                distance = self.distances(enemy, token.position_id).get(region, inf)
                opponents.append((distance, -self.distance_value(self.proportional_base(enemy, region, vp), distance)))
        return value + min(opponents, default=(inf, 0))[1]

    def dynamic_site_value(self, owner, site, regions):
        definition = self.rules.sites[site]
        return max((self.closest_site_interest(owner, p, definition["vp"], definition["countries"])
                    for p in sorted(regions)), key=abs, default=0)

    def source_move(self, owner, token, destination):
        site = token.definition_id
        before = self.locations.get(site, set())
        after = {t.position_id for t in self.state.tokens.values() if t.token_type == "SCORE_SOURCE"
                 and t.definition_id == site and t.token_id != token.token_id and t.position_id in self.rules.regions}
        if destination in self.rules.regions:
            after.add(destination)
        return self.dynamic_site_value(owner, site, after) - self.dynamic_site_value(owner, site, before)

    def connection_change(self, after, owner, endpoints, *, distance_offset=0, affected_only=False):
        closest, candidates = {}, defaultdict(list)
        for token in self.pieces:
            force, region = token.owner_country, token.position_id
            before_reach = self.distances(force, region)
            after_reach = after.distances(force, region)
            distance = min(min(before_reach.get(end, inf), after_reach.get(end, inf)) for end in endpoints)
            if distance == inf:
                continue
            team = self.rules.team(force)
            if affected_only:
                delta = (after.value(force, region, excluded=token.token_id, distance_offset=distance_offset) -
                         self.value(force, region, excluded=token.token_id, distance_offset=distance_offset))
                if not delta:
                    continue
            else:
                delta = None
            if distance < closest.get(team, inf):
                closest[team] = distance
                candidates[team].clear()
            if distance == closest[team]:
                candidates[team].append((token, delta))
        team = self.rules.team(owner)
        result = 0
        for side, tokens in candidates.items():
            changes = (delta if delta is not None else
                       after.value(t.owner_country, t.position_id, excluded=t.token_id, distance_offset=distance_offset) -
                       self.value(t.owner_country, t.position_id, excluded=t.token_id, distance_offset=distance_offset)
                       for t, delta in tokens)
            result += max(changes, key=abs) * (1 if side == team else -1)
        return result
