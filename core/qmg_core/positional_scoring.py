"""Strategic site value in VP and action opportunities, on public map data."""
from collections import defaultdict
from math import inf

from .definitions import PER_UNIT_SITE
from .expansion_scoring import ConstructionNetwork
from .model import TURN_ORDER, ZoneRegistry, zone
from .regional_sites import active_hidden_sites
from .rules import MapGraph, RuleModifierResolver, home_occupation, home_occupier, home_regions, occupied_home_forces
from .score_units import STANDARD_SITE_VP


class ActionDistanceScoring:
    def __init__(self, rules, state, pieces, locations, units, nearest_sites, *,
                 include_connections=True, construction_rules=False, stacked_sites=False, regional_defense=False,
                 _removal=None):
        self.rules = rules
        self.state = state
        self.units = units
        self.stacked_sites = stacked_sites
        self.regional_defense = regional_defense
        self.printed_locations = locations
        previous, removed = _removal if _removal is not None else (None, None)
        self.layers = {layer.key: layer for layer in active_hidden_sites(rules, state)
                       if layer.award.mode != "absence" and layer.active(rules, state)}
        if previous is not None and self.layers == previous.layers:
            self.locations, self.sites_by_region = previous.locations, previous.sites_by_region
        else:
            self.locations = {**locations, **{key: set(layer.spaces) for key, layer in self.layers.items()}}
            self.sites_by_region = defaultdict(list)
            for site, places in self.locations.items():
                for place in places:
                    self.sites_by_region[place].append(site)
        self.unit_definitions = (previous.unit_definitions if previous is not None else
                                 {unit["kind"]: key for key, unit in rules.units.items()})
        self.nearest_sites = nearest_sites
        self.pieces = tuple(t for t in pieces if rules.units[t.definition_id]["kind"] != "AIR")
        self.home_locations = ({owner: home_regions(rules, state, owner) for owner in rules.force_controllers}
                               if rules.settings.get("occupied_home_blocks_scoring") else {})
        self.homes = self.home_locations if units.home_importance or units.auxiliary_home_importance else {}
        self.home_occupation = self.occupation_after_removal(previous, removed) if self.home_locations else None
        self.blocked = occupied_home_forces(rules, state, self.pieces, homes=self.home_locations,
                                            occupation=self.home_occupation)
        self.remaining = 0 if state.winner else max(0, rules.settings["max_rounds"] - state.turn.round_id + 1)
        self.include_connections = include_connections
        self.construction_rules = construction_rules
        self.construction_networks, self.construction_paths = {}, {}
        self.graphs, self.paths, self.bases, self.sites_by_position = {}, {}, {}, {}
        self.enemy_fronts = {}
        self.income_changes = {}
        if previous is not None:
            # The unit transfer only changes its map and reserve zones.
            self.source_owners = previous.source_owners
            self.has_income_changes = previous.has_income_changes
        else:
            self.source_owners = defaultdict(set)
            for token in state.tokens.values():
                if token.token_type == "SCORE_SOURCE" and token.position_id in rules.regions:
                    self.source_owners[token.definition_id, token.position_id].add(token.owner_country)
            self.has_income_changes = include_connections and any(
                m["kind"] == "score_bonus"
                for seat in TURN_ORDER for token in state.positions.get(zone(seat, "STATUS"), ())
                for m in rules.cards[state.tokens[token].definition_id].get("modifiers", ()))

    def occupation_after_removal(self, previous, removed):
        if previous is None:
            return home_occupation(self.rules, self.pieces)
        marker = home_occupier(self.rules, removed)
        if marker is None or removed not in previous.pieces:
            return previous.home_occupation
        occupation = previous.home_occupation.copy()
        occupation[marker] -= 1
        return occupation

    def after_unit_removal(self, token_id, *, incremental):
        """Map one projected transfer into a view evaluated by the regular score formulas."""
        token = self.state.tokens[token_id]
        reserve = zone(token.owner_country, "RESERVE_" + self.rules.units[token.definition_id]["kind"])
        state = ZoneRegistry.projected_transfer(self.state, token_id, reserve)
        # A quiescent single-zone transfer has a known set of changed inputs. An
        # unsettled frame gets the independent projection and context instead.
        delta = (self, token) if incremental and not self.state.stack and self.state.decision is None else None
        return ActionDistanceScoring(
            self.rules, state, (piece for piece in self.pieces if piece.token_id != token.token_id),
            self.printed_locations, self.units, self.nearest_sites, include_connections=self.include_connections,
            construction_rules=self.construction_rules, stacked_sites=self.stacked_sites,
            regional_defense=self.regional_defense, _removal=delta)

    def position_terms(self, force, region, *, air, distance_offset, enemy_attraction):
        sites = self.site_values(force, region, construction=not air, distance_offset=distance_offset, air=air,
                                 enemy_attraction=enemy_attraction)
        homes = self.home_values(force, region, construction=not air, distance_offset=distance_offset)
        return {**{site: value for site, (_, value) in sites.items()},
                **{"home:" + owner: value for owner, value in homes.items()}}

    def adjacency(self, force):
        if force not in self.graphs:
            self.graphs[force] = MapGraph(self.rules).adjacency(
                self.state, force, include_connection_modifiers=self.include_connections)
        return self.graphs[force]

    def distances(self, force, start):
        key = force, start
        if key not in self.paths:
            self.paths[key] = MapGraph(self.rules).distances_on(start, self.adjacency(force))
        return self.paths[key]

    def nearest_enemy(self, team, region):
        cache = self.rules.search_cache
        if cache is None:
            return min((self.distances(t.owner_country, t.position_id).get(region, inf)
                        for t in self.pieces if t.position_id != region and self.rules.team(t.owner_country) != team),
                       default=inf)
        if team not in self.enemy_fronts:
            fronts = {}
            for piece in self.pieces:
                if self.rules.team(piece.owner_country) != team:
                    adjacency = self.adjacency(piece.owner_country)
                    fronts.setdefault(id(adjacency), (adjacency, set()))[1].add(piece.position_id)
            self.enemy_fronts[team] = [(adjacency, frozenset(places)) for adjacency, places in fronts.values()]

        graph = MapGraph(self.rules)
        nearest = inf
        for adjacency, places in self.enemy_fronts[team]:
            # The graph is undirected. One search from the site covers every unit on this front.
            key = id(adjacency), region, places
            if key not in cache.enemy_distances:
                distances = graph.distances_on(region, adjacency)
                cache.enemy_distances[key] = min(
                    (distances.get(place, inf) for place in places if place != region), default=inf)
            nearest = min(nearest, cache.enemy_distances[key])
        return nearest

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
            nearest = self.nearest_enemy(team, region) + distance_offset
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
        owners = self.source_owners.get((site, region), ()) if definition.get("dynamic") else {None}
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

    def site_values(self, force, region, *, excluded=None, construction=False, distance_offset=0,
                    air=False, enemy_attraction=True):
        """Value each chosen score layer after distance and unit adjustments."""
        construction = construction and self.construction_rules
        key = force, region, excluded, construction, distance_offset, air, enemy_attraction
        if key in self.sites_by_position:
            return self.sites_by_position[key]
        from_start = self.distances(force, region)
        practical = self.construction_distances(force, region) if construction else from_start
        penalty = 2 if construction and force in self.rules.auxiliary_forces else 1
        if self.regional_defense:
            values = self.regional_values(force, region, excluded, distance_offset, practical, penalty, air, enemy_attraction)
            self.sites_by_position[key] = values
            return values
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

    def regional_values(self, force, region, excluded, offset, practical, rate, air, enemy_attraction):
        team = self.rules.team(force)
        friends, enemies = [], []
        for unit in self.pieces:
            if unit.token_id != excluded:
                if self.rules.team(unit.owner_country) != team:
                    enemies.append(unit)
                elif air or unit.position_id != region or unit.owner_country != force:
                    friends.append(unit)
        # A score source spanning alternatives counts at its nearest eligible location.
        distances = self.distances(force, region)
        grouped = defaultdict(dict)
        occupied = defaultdict(list)
        if enemy_attraction:
            for unit in enemies:
                occupied[unit.position_id].append(unit)
        taken = set()
        for end in sorted(self.sites_by_region.keys() & practical.keys(), key=lambda end: (practical[end], end)):
            for site in self.sites_by_region[end]:
                if site in taken:
                    continue
                base = self.site_base(force, end, site, distance_offset=offset)
                enemy_base = sum(self.site_base(unit.owner_country, end, site, distance_offset=offset)
                                 for unit in occupied[end])
                if base or enemy_base:
                    grouped[end][site] = base + enemy_base
                    taken.add(site)
        result = {}
        selected = 0
        for end, layers in grouped.items():
            distance = practical[end] + offset
            total = sum(layers.values())
            value = total - self.units.distance_score(rate * distance)
            if value < 0:
                continue
            if selected == self.nearest_sites:
                break
            selected += 1
            from_end = self.distances(force, end)
            if any(unit.position_id == end for unit in friends):
                value += self.units.action_score(sum(from_end.get(unit.position_id) == 1 for unit in enemies))
            for unit in friends:
                d = from_end.get(unit.position_id, inf)
                if d > distances.get(end, inf):
                    continue
                base = sum(self.site_base(unit.owner_country, end, site, distance_offset=offset) for site in layers)
                value -= self.distance_value(base, d + offset) / 2
                if d == 0:
                    value -= base / 2
            # Allocate the single regional estimate to its sources for the existing
            # income ledger, which keeps each source's actual VP and eligibility.
            for site, base in layers.items():
                result[site] = distance, max(0, value) * base / total
        return result

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

    def value(self, force, region, *, excluded=None, construction=False, distance_offset=0, air=False,
              enemy_attraction=True):
        sites = self.site_values(force, region, excluded=excluded, construction=construction,
                                 distance_offset=distance_offset, air=air, enemy_attraction=enemy_attraction)
        homes = self.home_values(force, region, construction=construction, distance_offset=distance_offset)
        return sum(value for _, value in sites.values()) + sum(homes.values())

    def best_adjacent_site(self, force, regions):
        regions = set(regions)
        if self.regional_defense:
            return max((self.distance_value(sum(self.site_base(force, end, site) for site in sites), 1)
                        for end, sites in self.sites_by_region.items()
                        if any(self.distances(force, end).get(region) == 1 for region in regions)), default=0)
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
            for site, (distance, score) in self.site_values(force, region, excluded=token.token_id,
                                                          enemy_attraction=False).items():
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
