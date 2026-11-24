"""Strategic site value in VP and action opportunities, on public map data."""
from collections import defaultdict, deque
from math import inf

from .model import TURN_ORDER
from .rules import MapGraph


class ActionDistanceScoring:
    def __init__(self, rules, state, pieces, locations, units, nearest_sites, *, include_connections=True):
        self.rules = rules
        self.state = state
        self.units = units
        self.locations = locations
        self.nearest_sites = nearest_sites
        self.pieces = tuple(t for t in pieces if rules.units[t.definition_id]["kind"] != "AIR")
        self.remaining = 0 if state.winner else max(0, rules.settings["max_rounds"] - state.turn.round_id + 1)
        self.include_connections = include_connections
        self.graphs, self.paths, self.bases, self.sites_by_position = {}, {}, {}, {}

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

    def capture(self, force, region, vp):
        key = force, region, vp
        if key not in self.bases:
            seat = self.rules.force_controllers[force]
            team = self.rules.team(force)
            cap = self.units.vp_score(vp * self.scoring_rounds(seat))
            # The occupying move has already cleared hostile units on the site itself.
            nearest = min((self.distances(t.owner_country, t.position_id).get(region, inf)
                           for t in self.pieces if t.position_id != region and self.rules.team(t.owner_country) != team),
                          default=inf)
            initial = self.units.vp_score(vp * self.units.capture_rounds)
            self.bases[key] = max(0, min(cap, initial + self.units.action_score(nearest - 1)))
        return self.bases[key]

    def distance_value(self, base, distance):
        return max(0, base - self.units.action_score(distance))

    def site_values(self, force, region, *, excluded=None):
        """Deduct or add each other unit at most once across the chosen routes."""
        key = force, region, excluded
        if key in self.sites_by_position:
            return self.sites_by_position[key]
        from_start = self.distances(force, region)
        team = self.rules.team(force)
        routes = []
        for site, locations in self.locations.items():
            definition = self.rules.sites[site]
            if not any(self.rules.team(seat) == team for seat in definition["countries"]):
                continue
            for end in sorted(locations):
                distance = from_start.get(end)
                if distance is None:
                    continue
                base = self.capture(force, end, definition["vp"])
                value = base - self.units.action_score(distance)
                if value >= 0:
                    routes.append((distance, site, end, base, value))
        # Multiple physical copies of the same score source still represent one site.
        nearest = {}
        for row in sorted(routes):
            nearest.setdefault(row[1], row)
        routes = list(nearest.values())[:self.nearest_sites]
        scores = {site: (distance, raw) for distance, site, _, _, raw in routes}
        changes = defaultdict(float)
        for token in self.pieces:
            if token.token_id == excluded:
                continue
            position = token.position_id
            friendly = self.rules.team(token.owner_country) == team
            best, best_site = 0, None
            for distance, site, end, base, _ in routes:
                to_end = self.distances(force, end).get(position, inf)
                if not (to_end < distance or to_end == distance == 0):
                    continue
                if from_start.get(position, inf) + to_end != distance:
                    continue
                change = self.distance_value(base, to_end) / 2
                if change > best:
                    best, best_site = change, site
            if best_site is not None:
                changes[best_site] += -best if friendly else best
        scores = {site: (distance, max(0, value + changes[site])) for site, (distance, value) in scores.items()}
        self.sites_by_position[key] = scores
        return scores

    def value(self, force, region, *, excluded=None):
        return sum(value for _, value in self.site_values(force, region, excluded=excluded).values())

    def best_adjacent_site(self, force, regions):
        regions = set(regions)
        team = self.rules.team(force)
        return max((self.distance_value(self.capture(force, end, self.rules.sites[site]["vp"]), 1)
                    for site, locations in self.locations.items() for end in locations
                    if any(self.rules.team(seat) == team for seat in self.rules.sites[site]["countries"])
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
                    if site not in rates[seat]:
                        continue
                    score = self.capture(force, region, self.rules.sites[site]["vp"])
                    held[key] = max(held[key], score)
                best[key] = max(best[key], score)
                influences.append({"token": token.token_id, "site": site, "distance": distance,
                                   "score_influence": score, "military_influence": score,
                                   "supply_weight": 1, "route_factor": 1})
        own = self.rules.team(country)
        enemy = "allies" if own == "axis" else "axis"
        points = []
        for site, regions in self.locations.items():
            definition = self.rules.sites[site]
            value = best[site, own] - best[site, enemy]
            if definition.get("dynamic"):
                value = self.dynamic_site_value(country, site, regions)
            controlled = held[site, own] - held[site, enemy]
            points.append({"site": site, "value": value, "fixed_score": controlled,
                           "long_term_score": value - controlled, "control": int(bool(held[site, own])) -
                           int(bool(held[site, enemy])), "baseline_vp": definition["vp"],
                           "friendly": best[site, own], "hostile": best[site, enemy], "advantage": value})
        return sum(row["value"] for row in points), points, influences

    def closest_site_interest(self, owner, region, vp, countries=None):
        """One nearest unit per side, keeping the highest impact among ties."""
        teams = {}
        for token in self.pieces:
            force = token.owner_country
            if countries is not None and self.rules.force_controllers[force] not in countries:
                continue
            distance = self.distances(force, token.position_id).get(region, inf)
            score = self.distance_value(self.capture(force, region, vp), distance)
            team = self.rules.team(force)
            teams[team] = min(teams.get(team, (inf, 0)), (distance, -score))
        team = self.rules.team(owner)
        return -teams.get(team, (inf, 0))[1] + sum(row[1] for side, row in teams.items() if side != team)

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

    def connection_change(self, after, owner, endpoints):
        closest, candidates = {}, defaultdict(list)
        for token in self.pieces:
            force, region = token.owner_country, token.position_id
            before_reach = self.distances(force, region)
            after_reach = after.distances(force, region)
            distance = min(min(before_reach.get(end, inf), after_reach.get(end, inf)) for end in endpoints)
            if distance == inf:
                continue
            team = self.rules.team(force)
            if distance < closest.get(team, inf):
                closest[team] = distance
                candidates[team].clear()
            if distance == closest[team]:
                candidates[team].append(token)
        team = self.rules.team(owner)
        result = 0
        for side, tokens in candidates.items():
            changes = (after.value(t.owner_country, t.position_id, excluded=t.token_id) -
                       self.value(t.owner_country, t.position_id, excluded=t.token_id) for t in tokens)
            result += max(changes, key=abs) * (1 if side == team else -1)
        return result
