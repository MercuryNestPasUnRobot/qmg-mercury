"""Value the nearest scoring sites using current, visible board routes."""
from collections import Counter, defaultdict, deque

from .rules import MapGraph


class ScoringRoutes:
    def __init__(self, rules, state, pieces, weights):
        self.rules = rules
        self.state = state
        self.weights = weights
        self.config = weights["route_forecast"]
        self.locations = {key: {site["region"]} for key, site in rules.sites.items() if not site.get("dynamic")}
        for token in state.tokens.values():
            if token.token_type == "SCORE_SOURCE" and token.position_id in rules.regions:
                if rules.sites[token.definition_id].get("dynamic"):
                    self.locations.setdefault(token.definition_id, set()).add(token.position_id)
        self.occupancy = defaultdict(Counter)
        for piece in pieces:
            self.occupancy[piece.position_id][rules.team(piece.owner_country)] += 1
        self.adjacency = {}
        self.region_weights = {}
        self.cache = {}

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
                options = [(paths[region][0], -paths[region][1]) for region in regions if region in paths]
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

    def target_value(self, force, region):
        return sum(self.rules.sites[key]["vp"] * self.distance_weight(distance) * factor
                   for key, (distance, factor) in self.nearest(force, region).items())


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
