from collections import deque
from copy import deepcopy

from .definitions import BATTLES, CONNECTION_USES, CONSTRUCTION, FORCE_OPERATIONS, TARGETED_OPERATIONS
from .model import CoreError, ZONES, TURN_ORDER, GameState, Token, TurnClock, zone, shuffle


def auxiliary_reserves(rules):
    positions = {zone(force, f"RESERVE_{kind}") for force in rules.auxiliary_forces for kind in ("ARMY", "NAVY")}
    if any(u["kind"] == "AIR" for u in rules.units.values()):
        positions.update(zone(force, "RESERVE_AIR") for force in rules.force_controllers)
    if rules.settings.get("temporary_status_disable"):
        positions.update(zone(force, "DISABLED_STATUS") for force in rules.countries)
    return positions


def deployed_zones(rules, token):
    kinds = ("STATUS", "RESPONSE", "HAND") if rules.cards[token.definition_id].get("legacy_kind") == "bolster" else (
        "STATUS", "RESPONSE")
    return {zone(token.owner_country, kind) for kind in kinds}


def status_enabled(rules, state, source):
    if source.position_id != zone(source.owner_country, "STATUS"):
        return False
    current = TURN_ORDER[state.turn.country_index]
    if rules.team(source.owner_country) == rules.team(current):
        return True
    return not any(t.token_type == "CARD" and t.owner_country == current
                   and t.position_id == zone(current, "STATUS") and any(
                       m["kind"] == "ignore_enemy_statuses" for m in rules.cards[t.definition_id].get("modifiers", []))
                   for t in state.tokens.values())


class RuleModifierResolver:
    def __init__(self, rules):
        self.rules = rules

    def resolve(self, state, country):
        force = country
        country = self.rules.force_controllers[country]
        result = []
        for token in sorted(state.tokens.values(), key=lambda item: item.token_id):
            if token.token_type == "CARD" and status_enabled(self.rules, state, token):
                for modifier in self.rules.cards[token.definition_id].get("modifiers", []):
                    if self.rules.cards[token.definition_id].get("content_condition"):
                        from .content_effects import condition_met
                        if not condition_met(self.rules, state, token.owner_country,
                                             self.rules.cards[token.definition_id]["content_condition"]):
                            continue
                    if modifier["kind"] == "supply_sharing" and force not in modifier["forces"]:
                        continue
                    if occupied := modifier.get("enemy_occupied_region"):
                        if not any(t.position_id == occupied and self.rules.units[t.definition_id]["kind"] == "ARMY"
                                   and self.rules.team(t.owner_country) != self.rules.team(force)
                                   for t in board_units(self.rules, state)):
                            continue
                    if modifier.get("force", force) != force or modifier.get("team", self.rules.team(force)) != self.rules.team(force):
                        continue
                    if modifier.get("scope", "owner") == "all" or token.owner_country == country:
                        result.append(deepcopy(modifier))
        return result


class MapGraph:
    def __init__(self, rules):
        self.rules = rules

    def adjacency(self, state=None, country=None, purpose=None, *, include_connection_modifiers=True):
        if purpose is not None and purpose not in CONNECTION_USES:
            raise CoreError("Unknown connection use")
        edges = {}
        for connection in self.rules.connections.values():
            edge = tuple(sorted((connection["a"], connection["b"])))
            edges.setdefault(edge, set()).update(connection.get("movement_or_supply_tags", CONNECTION_USES))
        if state is not None and country is not None:
            for strait in self.rules.settings.get("straits", []):
                axis = any(t.position_id == strait["land"] and self.rules.units[t.definition_id]["kind"] == "ARMY"
                           and self.rules.team(t.owner_country) == "axis" for t in board_units(self.rules, state))
                if (self.rules.team(country) == "axis") == axis:
                    edges.setdefault(tuple(sorted(strait["seas"])), set()).update(CONNECTION_USES)
            if include_connection_modifiers:
                for m in RuleModifierResolver(self.rules).resolve(state, country):
                    if m["kind"] != "connection":
                        continue
                    edge = tuple(sorted((m["a"], m["b"])))
                    uses = edges.setdefault(edge, set())
                    tags = m.get("movement_or_supply_tags", CONNECTION_USES)
                    if m["enabled"]:
                        uses.update(tags)
                    else:
                        uses.difference_update(tags)
        neighbors = {region: set() for region in self.rules.regions}
        for (a, b), uses in edges.items():
            if uses and (purpose is None or purpose in uses):
                neighbors[a].add(b)
                neighbors[b].add(a)
        return neighbors

    def neighbors(self, region, state=None, country=None, purpose=None):
        if region not in self.rules.regions:
            raise CoreError("Unknown region")
        return sorted(self.adjacency(state, country, purpose)[region])

    def distances(self, start, state=None, country=None, purpose=None):
        if start not in self.rules.regions:
            raise CoreError("Unknown region")
        adjacency = self.adjacency(state, country, purpose)
        distances, queue = {start: 0}, deque([start])
        while queue:
            p = queue.popleft()
            for n in sorted(adjacency[p]):
                if n not in distances:
                    distances[n] = distances[p] + 1
                    queue.append(n)
        return distances


def board_units(rules, state, country=None):
    return sorted((t for t in state.tokens.values() if t.token_type == "UNIT" and t.position_id in rules.regions
                   and (country is None or t.owner_country == country)), key=lambda token: token.token_id)


def capture_turn_start(rules, state):
    if rules.settings.get("record_turn_start"):
        state.turn_start = {
            "units": {token.token_id: token.position_id for token in board_units(rules, state)},
            "adjacency": {country: {region: sorted(neighbors) for region, neighbors in
                                    MapGraph(rules).adjacency(state, country).items()}
                          for country in rules.force_controllers},
        }


def home_regions(rules, state, force):
    homes = {r["id"] for r in rules.regions.values() if r.get("home_country") == force}
    for modifier in RuleModifierResolver(rules).resolve(state, force):
        if modifier["kind"] == "home":
            homes = {modifier["region"]}
    return homes


def active_unit_rule(state, token_id, definition):
    return any(t.token_type == "RULE" and t.source_token_id == token_id and t.definition_id == definition
               and t.position_id in (zone(t.owner_country, "RESPONSE"), "RESOLUTION")
               for t in state.tokens.values())


def unit_rule_matches(rules, state, token, mode):
    if active_unit_rule(state, token.token_id, "unit-" + mode):
        return True
    from .content_effects import unit_matches
    for response in state.tokens.values():
        if response.token_type != "RULE" or response.position_id not in (zone(response.owner_country, "RESPONSE"), "RESOLUTION"):
            continue
        area = rules.abilities[response.definition_id].get("area_rule")
        if area and area["mode"] == mode and unit_matches(rules, state, response.owner_country, token, area["filter"]):
            return True
    return False


class SupplyResolver:
    def __init__(self, rules):
        self.rules, self.graph = rules, MapGraph(rules)

    def supplied(self, state, country, *, units=None):
        if units is None:
            units = board_units(self.rules, state)
        modifiers = RuleModifierResolver(self.rules).resolve(state, country)
        always = {t.token_id for t in units if t.owner_country == country and any(
            m["kind"] == "always_supplied" and self.rules.units[t.definition_id]["kind"].lower()
            in m.get("kinds", ("army", "navy", "air")) for m in modifiers)}
        always.update(t.token_id for t in units if t.owner_country == country
                      and unit_rule_matches(self.rules, state, t, "supply"))
        share = self.rules.settings["allied_supply"]
        groups = [m["forces"] for m in modifiers if m["kind"] == "supply_sharing"]
        revised = self.rules.settings.get("supply_revision", 1) == 2
        if revised or self.rules.settings.get("surface_supply"):
            network = {country}
            while True:
                expanded = network | {force for group in groups if network.intersection(group) for force in group}
                if expanded == network:
                    break
                network = expanded
            if share:
                network.update(force for force in self.rules.force_controllers if self.rules.team(force) == self.rules.team(country))
            pieces = [t for t in units if t.owner_country in network and self.rules.units[t.definition_id]["kind"] != "AIR"]
            seeded, occupied = set(), set()
            adjacency = self.graph.adjacency(state, country, "supply")
            team = self.rules.team(country)
            ports = {t.position_id for t in units if self.rules.team(t.owner_country) == team and
                     (revised or self.rules.units[t.definition_id]["kind"] == "ARMY")}
            unported = {t.token_id for t in pieces if self.rules.units[t.definition_id]["kind"] == "NAVY" and
                        adjacency[t.position_id].isdisjoint(ports)}
            scoring_regions = set()
            if revised:
                scoring_regions.update(site["region"] for site in self.rules.sites.values() if not site.get("dynamic"))
                scoring_regions.update(t.position_id for t in state.tokens.values() if t.token_type == "SCORE_SOURCE"
                                       and t.position_id in self.rules.regions and self.rules.sites[t.definition_id].get("dynamic"))
            army_sources = {}
            for token in pieces:
                has_port = token.token_id not in unported
                if revised and not has_port:
                    continue
                force = token.owner_country
                token_modifiers = modifiers if force == country else RuleModifierResolver(self.rules).resolve(state, force)
                automatic = unit_rule_matches(self.rules, state, token, "supply") or any(
                    m["kind"] == "always_supplied" and self.rules.units[token.definition_id]["kind"].lower()
                    in m.get("kinds", ("army", "navy", "air")) for m in token_modifiers)
                if automatic or has_port:
                    occupied.add(token.position_id)
                if automatic:
                    seeded.add(token.position_id)
                elif self.rules.units[token.definition_id]["kind"] == "ARMY":
                    if force not in army_sources:
                        sources = home_regions(self.rules, state, force) | scoring_regions
                        if revised:
                            sources.update(r["id"] for r in self.rules.regions.values()
                                           if r.get("supply_source_for") == force and r.get("home_country") != force)
                        sources.update(m["region"] for m in token_modifiers if m["kind"] == "supply_source")
                        sources.difference_update(m["region"] for m in token_modifiers if m["kind"] == "deny_supply_source")
                        army_sources[force] = sources
                    if token.position_id in army_sources[force]:
                        seeded.add(token.position_id)
            reached, queue = seeded.copy(), deque(sorted(seeded))
            while queue:
                for region in adjacency[queue.popleft()]:
                    if region in occupied and region not in reached:
                        reached.add(region)
                        queue.append(region)
            supplied = always | {t.token_id for t in units if t.owner_country == country and t.position_id in reached}
            return supplied - unported if revised else supplied
        pieces = [t for t in units if t.owner_country == country or
                  ((share or any(t.owner_country in group for group in groups))
                   and self.rules.team(t.owner_country) == self.rules.team(country))]
        occupied = {t.position_id for t in pieces if self.rules.units[t.definition_id]["kind"] != "AIR"}
        sources = {r["id"] for r in self.rules.regions.values() if r.get("supply_source_for") == country}
        if any(m["kind"] == "home" for m in modifiers):
            sources.difference_update(r["id"] for r in self.rules.regions.values() if r.get("home_country") == country)
            sources.update(home_regions(self.rules, state, country))
        sources.update(m["region"] for m in modifiers if m["kind"] == "supply_source")
        sources.difference_update(m["region"] for m in modifiers if m["kind"] == "deny_supply_source")
        reached = sources & occupied
        queue = deque(sorted(reached))
        adjacency = self.graph.adjacency(state, country, "supply")
        while queue:
            for n in adjacency[queue.popleft()]:
                if n in occupied and n not in reached:
                    reached.add(n)
                    queue.append(n)
        return always | {t.token_id for t in pieces if t.owner_country == country and t.position_id in reached}

    def supplied_for_seat(self, state, country, units):
        return {token_id for force in self.rules.controlled_forces(country)
                for token_id in self.supplied(state, force, units=units)}


class LegalActionGenerator:
    def __init__(self, rules):
        self.rules, self.graph, self.supply = rules, MapGraph(rules), SupplyResolver(rules)

    def requirements_met(self, state, country, requirements):
        for requirement in requirements:
            if requirement["kind"] == "min_hand" and len(state.positions[zone(country, "HAND")]) < requirement["count"]:
                return False
            if requirement["kind"] == "occupied" and not any(t.position_id == requirement["region"] for t in board_units(self.rules, state, country)):
                return False
            if requirement["kind"] == "phase" and state.turn.phase != requirement["phase"]:
                return False
        return True

    def can_start_card(self, state, country, card, *, activating=False):
        if card.get("legacy_kind") in ("bolster", "air-power"):
            return False
        if card.get("implementation", {}).get("status") == "blocked":
            return False
        # A deployed listener's condition belongs to its activation, not to placing the card.
        placement = card.get("legacy_kind") in ("status", "response") and not activating
        if "content_condition" in card and not placement:
            from .content_effects import condition_met
            if not condition_met(self.rules, state, country, card["content_condition"]):
                return False
        if card.get("simulation_unknown") or not self.requirements_met(state, country, card.get("requirements", [])):
            return False
        if card.get("play_as"):
            return any(self.targets(state, country, {"type": mode}) for mode in card["play_as"])
        if not card["effects"]:
            return True
        first = card["effects"][0]
        effect = first["operation"] if first["type"] == "CHOOSE" else first
        if effect["type"] == "CARD_EFFECT":
            from .content_effects import active_program_can_begin, can_begin
            if activating:
                return active_program_can_begin(self.rules, state, country, card["effects"])
            effect = effect["effect"]
            return can_begin(self.rules, state, effect.get("chooser", country), effect)
        if effect["type"] == "REDEPLOY":
            return self.rules.force_controllers.get(effect.get("force", country)) == country
        if effect["type"] not in TARGETED_OPERATIONS:
            return True
        selected = effect.get("destination", effect.get("target", effect.get("target_token", "$choice")))
        targets = self.targets(state, country, effect)
        return bool(targets) and (selected == "$choice" or selected in targets)

    def targets(self, state, country, op):
        kind = op["type"]
        if op.get("expected_victim"):
            victim = state.tokens.get(op["expected_victim"])
            if victim is None or victim.position_id != op.get("target"):
                return []
        if "effect_constraints" in op:
            from .content_effects import target_regions
            return target_regions(self.rules, state, country, op["effect_constraints"])
        if kind in FORCE_OPERATIONS:
            force = op.get("force", country)
            controller = self.rules.force_controllers.get(force)
            if controller != country and not (op.get("allied_effect") is True and controller in self.rules.countries
                                               and self.rules.team(force) == self.rules.team(country)):
                return []
        if kind in CONSTRUCTION:
            unit_kind = op.get("unit_kind", "NAVY" if kind == "BUILD_NAVY" else "ARMY")
            reserve = state.positions[zone(force, f"RESERVE_{unit_kind}")]
            if not reserve:
                return []
            expected_terrain = "LAND" if unit_kind == "ARMY" else "SEA"
            units = board_units(self.rules, state)
            supplied = self.supply.supplied(state, force)
            origins = {t.position_id for t in units if t.token_id in supplied and self.rules.units[t.definition_id]["kind"] != "AIR"}
            homes = home_regions(self.rules, state, force)
            modifiers = RuleModifierResolver(self.rules).resolve(state, force)
            extra = {m["region"] for m in modifiers if m["kind"] == "build_region" and m["unit_kind"] == unit_kind}
            adjacency = self.graph.adjacency(state, country, "build")
            result = []
            for region, definition in self.rules.regions.items():
                if definition["type"] != expected_terrain:
                    continue
                present = [t for t in units if t.position_id == region]
                if any(self.rules.team(t.owner_country) != self.rules.team(country) or
                       (t.owner_country == force and self.rules.units[t.definition_id]["kind"] == unit_kind) for t in present):
                    continue
                neighbors = adjacency[region]
                has_adjacent_origin = bool(neighbors & origins)
                needs_adjacent_origin = force in definition.get("construction_requires_adjacent_for", ())
                if needs_adjacent_origin and not has_adjacent_origin:
                    continue
                reachable = region in homes or has_adjacent_origin or region in extra
                if unit_kind == "ARMY" and any(m["kind"] == "recruit_region" and m["region"] == region for m in modifiers):
                    reachable = True
                if kind == "RECRUIT":
                    reachable = (needs_adjacent_origin or self.rules.settings.get("free_recruit", False) or
                                 region in homes or bool(neighbors & homes))
                elif self.rules.settings.get("naval_army_base") and unit_kind == "NAVY":
                    reachable = reachable and any(t.position_id in neighbors and
                        self.rules.units[t.definition_id]["kind"] == "ARMY" and
                        self.rules.team(t.owner_country) == self.rules.team(country) for t in units)
                if reachable:
                    result.append(region)
            return sorted(result)
        if kind in BATTLES:
            terrain = "LAND" if kind == "LAND_BATTLE" else "SEA"
            supplied = self.supply.supplied(state, force)
            origins = {t.position_id for t in board_units(self.rules, state, force) if t.token_id in supplied
                       and self.rules.units[t.definition_id]["kind"] != "AIR"}
            adjacency = self.graph.adjacency(state, country, "battle")
            result = []
            for region, d in self.rules.regions.items():
                if d["type"] != terrain or not (adjacency[region] & origins):
                    continue
                occupants = [t for t in board_units(self.rules, state) if t.position_id == region]
                if not any(self.rules.team(t.owner_country) == self.rules.team(country)
                           and self.rules.units[t.definition_id]["kind"] != "AIR" for t in occupants):
                    result.append(region)
            return sorted(result)
        if kind == "ELIMINATE":
            return sorted(t.token_id for t in board_units(self.rules, state) if
                          (op.get("target_owner") in (None, t.owner_country)) and
                          (op.get("allow_friendly", False) or self.rules.team(t.owner_country) != self.rules.team(country)))
        if kind == "DISCARD":
            return list(state.positions[zone(country, "HAND")])
        if kind == "TRANSFER":
            token = state.tokens.get(op["token"])
            if not token or token.position_id != op.get("expected_from", token.position_id):
                return []
            positions = op.get("destinations", tuple(state.positions))
            if op["destination"] != "$choice":
                positions = [op["destination"]] if op["destination"] in positions else []
            result = set()
            for position in positions:
                # A card enters resolution only as part of a play or ability activation.
                if position == token.position_id or position == "RESOLUTION":
                    continue
                try:
                    validate_token_position(self.rules, state, token, position)
                except CoreError:
                    continue
                if op.get("index") is None or op["index"] <= len(state.positions[position]):
                    result.add(position)
            return sorted(result)
        return []


def validate_token_position(rules, state, token, position):
    if position not in state.positions:
        raise CoreError("Unknown destination")
    country = token.owner_country
    if token.token_type == "UNIT":
        kind = rules.units[token.definition_id]["kind"]
        if position in rules.regions:
            terrain = "LAND" if kind == "ARMY" else "SEA"
            if kind != "AIR" and rules.regions[position]["type"] != terrain:
                raise CoreError("Unit terrain mismatch")
            for occupant_id in state.positions[position]:
                occupant = state.tokens[occupant_id]
                if occupant.token_type == "UNIT" and occupant_id != token.token_id and (
                    (occupant.owner_country == country and rules.units[occupant.definition_id]["kind"] == kind)
                    or rules.team(occupant.owner_country) != rules.team(country)
                ):
                    raise CoreError("Illegal hostile coexistence or duplicate national unit")
        elif position != zone(country, f"RESERVE_{kind}"):
            raise CoreError("Invalid reserve")
    elif token.token_type == "CARD":
        if position not in {"RESOLUTION", *(zone(country, kind) for kind in ("DECK", "HAND", "DISCARD", "STATUS", "DISABLED_STATUS", "RESPONSE", "SET_ASIDE", "OBJECT_POOL"))}:
            raise CoreError("Invalid card zone")
    elif token.token_type == "SCORE_SOURCE":
        dormant = zone(country, "SCORE_DORMANT") if country else "SCORE_DORMANT"
        if position != dormant and position not in rules.regions:
            raise CoreError("Invalid dynamic score source")
    elif token.token_type == "ABILITY":
        raise CoreError("Ability positions follow their source card and activation lifecycle")
    elif token.token_type == "RULE":
        if position not in (zone(country, "OBJECT_POOL"), zone(country, "RESPONSE"), zone(country, "ABILITY_READY")):
            raise CoreError("Rule object position must follow its response lifecycle")


def validate_state(rules, state, *, legacy_snapshot=False):
    if state.ruleset_version != rules.version or set(state.scores) != {"axis", "allies"}:
        raise CoreError("State/rules mismatch")
    if not isinstance(state.game_id, str) or not state.game_id or any(type(v) is not int or v < 0 for v in (state.version, state.serial, state.rng)) or state.rng > 0xFFFFFFFF:
        raise CoreError("Invalid state identity, version, counter or RNG")
    if any(type(v) is not int or v < 1 for v in (state.turn.round_id, state.turn.turn_id)) or type(state.turn.country_index) is not int or type(state.turn.plays_remaining) is not int or state.turn.plays_remaining < 0:
        raise CoreError("Invalid turn counters")
    expected_positions = set(rules.regions) | {"RESOLUTION", "SCORE_DORMANT"} | {zone(c, z) for c in TURN_ORDER for z in ZONES}
    expected_positions.update(auxiliary_reserves(rules))
    expected_positions.update(zone(c, "SET_ASIDE") for c in rules.settings.get("staging_countries", ()))
    old_positions = expected_positions - {zone(c, "OBJECT_POOL") for c in TURN_ORDER}
    has_pool = set(state.positions) == expected_positions
    if not has_pool and (not legacy_snapshot or set(state.positions) != old_positions):
        raise CoreError("Position registry mismatch")
    seen = set()
    for position, ids in state.positions.items():
        for token_id in ids:
            if token_id in seen or token_id not in state.tokens or state.tokens[token_id].position_id != position:
                raise CoreError("Every token must occupy exactly one position")
            seen.add(token_id)
    if seen != set(state.tokens):
        raise CoreError("Unpositioned token")
    if has_pool and state.tokens.keys() != rules.inventory.keys():
        raise CoreError("Closed object inventory mismatch")
    for key, t in state.tokens.items():
        owners = rules.force_controllers if t.token_type == "UNIT" else rules.countries
        if key != t.token_id or (t.owner_country not in owners and not (t.token_type == "SCORE_SOURCE" and t.owner_country is None)):
            raise CoreError("Invalid token identity/owner")
        slot = rules.inventory.get(key)
        if not slot or (slot.definition_id, slot.token_type, slot.owner_country, slot.source_token_id) != (
                t.definition_id, t.token_type, t.owner_country, t.source_token_id):
            raise CoreError("Immutable object identity mismatch")
        if t.token_type == "UNIT":
            if t.definition_id not in rules.units:
                raise CoreError("Unknown unit definition")
            validate_token_position(rules, state, t, t.position_id)
        elif t.token_type == "CARD":
            if t.definition_id not in rules.cards or rules.cards[t.definition_id]["country"] != t.owner_country:
                raise CoreError("Invalid card definition")
            validate_token_position(rules, state, t, t.position_id)
        elif t.token_type == "ABILITY":
            source = state.tokens.get(t.source_token_id)
            if not source or source.token_type != "CARD" or t.owner_country != source.owner_country or t.definition_id not in rules.bound_abilities[source.definition_id]:
                raise CoreError("Invalid ability source")
            if t.position_id not in {"RESOLUTION", zone(t.owner_country, "RESPONSE"), zone(t.owner_country, "OBJECT_POOL"), *(zone(t.owner_country, z) for z in ZONES if z.startswith("ABILITY_"))}:
                raise CoreError("Invalid ability lifecycle zone")
            if t.position_id == "RESOLUTION" and not any(f.context.get("ability_token") == key for f in state.stack):
                raise CoreError("Ability in resolution needs an active frame")
            resolving_response = (source.position_id == "RESOLUTION" and
                                  (rules.cards[source.definition_id]["type"] == "RESPONSE" or
                                   rules.cards[source.definition_id].get("legacy_kind") == "bolster")
                                  and any(frame.source_card_id == source.token_id and frame.context.get("ability_token") == t.token_id
                                          for frame in state.stack))
            if source.position_id not in deployed_zones(rules, source) and not resolving_response and t.position_id not in (zone(t.owner_country, "ABILITY_INACTIVE"), zone(t.owner_country, "OBJECT_POOL")):
                raise CoreError("Orphan active ability")
        elif t.token_type == "RULE":
            source = state.tokens.get(t.source_token_id)
            definitions = ([*rules.settings.get("unit_responses", []), *rules.settings.get("turn_opportunities", {})] if source and source.token_type == "UNIT" else
                           rules.settings.get("area_responses", {}).get(source.definition_id, []) if source else [])
            if (source is None or t.definition_id not in definitions
                    or t.owner_country != rules.force_controllers[source.owner_country]):
                raise CoreError("Invalid unit response")
            if t.position_id not in (zone(t.owner_country, "OBJECT_POOL"), zone(t.owner_country, "RESPONSE"), "RESOLUTION", zone(t.owner_country, "ABILITY_READY")):
                raise CoreError("Invalid unit response position")
            if t.position_id == "RESOLUTION" and not any(f.context.get("ability_token") == key for f in state.stack):
                raise CoreError("Unit response needs a resolution frame")
        elif t.token_type == "SCORE_SOURCE":
            if t.definition_id not in rules.sites:
                raise CoreError("Invalid dynamic score source")
            validate_token_position(rules, state, t, t.position_id)
        else:
            raise CoreError("Unknown token type")
    if state.turn.phase not in ("Setup", *rules.phases) or not 0 <= state.turn.country_index < 6:
        raise CoreError("Invalid turn")
    if any(type(v) is not int for v in state.scores.values()) or state.winner not in (None, "axis", "allies", "draw"):
        raise CoreError("Invalid score/result")
    frame_ids = [f.resolution_id for f in state.stack]
    if len(frame_ids) != len(set(frame_ids)):
        raise CoreError("Duplicate frame")
    for i, frame in enumerate(state.stack):
        if frame.actor_country not in rules.countries or type(frame.current_step) is not int or frame.current_step < 0:
            raise CoreError("Invalid resolution frame")
        if frame.parent_resolution_id is not None and frame.parent_resolution_id not in frame_ids[:i]:
            raise CoreError("Invalid resolution lineage")
        if frame.source_card_id and (frame.source_card_id not in state.tokens or state.tokens[frame.source_card_id].token_type != "CARD"):
            raise CoreError("Invalid frame source")
        if "response_candidates" in frame.context:
            candidates = frame.context["response_candidates"]
            if (not isinstance(candidates, list) or any(not isinstance(key, str) for key in candidates)
                    or len(candidates) != len(set(candidates))
                    or any(key not in state.tokens or state.tokens[key].token_type not in ("ABILITY", "RULE") for key in candidates)
                    or not isinstance(frame.context.get("event"), dict)
                    or not isinstance(frame.context.get("event_outcome"), dict)):
                raise CoreError("Invalid response window")
            if frame.source_card_id is not None or type(frame.context.get("active_response_used", False)) is not bool:
                raise CoreError("Invalid response window owner or usage")
    if state.decision and (state.decision.resolution_id not in frame_ids or not state.decision.legal_options or
                           state.decision.required_country not in rules.countries):
        raise CoreError("Unresumable decision")
    if state.decision and (state.decision.resolution_id != frame_ids[-1] or state.decision.required_country != state.stack[-1].actor_country or len(state.decision.legal_options) != len(set(state.decision.legal_options))):
        raise CoreError("Decision does not block the active resolution frame")


def initial_abilities(rules, source, include_pool=False):
    active = source.position_id in deployed_zones(rules, source)
    roots = rules.cards[source.definition_id].get("abilities", [])
    capacity = sum(rules.abilities[key].get("copies", 1) for key in roots)
    pool = zone(source.owner_country, "OBJECT_POOL")
    for definition in (sorted(rules.bound_abilities[source.definition_id]) if include_pool else roots):
        count = rules.abilities[definition].get("copies", 1) if definition in roots else 0
        total = max(count, rules.abilities[definition].get("pool_capacity", count or capacity or 1)) if include_pool else count
        for copy in range(total):
            position = pool
            if copy < count and source.position_id != pool:
                position = zone(source.owner_country, "ABILITY_READY" if active else "ABILITY_INACTIVE")
            key = f"ability:{source.token_id}:{definition}"
            yield Token(f"{key}:{copy + 1}" if copy else key, definition, "ABILITY",
                        source.owner_country, position, source.token_id)


def initial_inventory(rules, projected):
    tokens = {}
    for row in rules.data["initial_tokens"]:
        source = Token(**row)
        slots = [source]
        if not projected and source.token_type == "CARD":
            if source.definition_id not in rules.cards:
                raise CoreError("Unknown initial card definition")
            slots.extend(initial_abilities(rules, source, include_pool=True))
            slots.extend(Token(f"rule:{source.token_id}:{definition}", definition, "RULE", source.owner_country,
                               zone(source.owner_country, "OBJECT_POOL"), source.token_id)
                         for definition in rules.settings.get("area_responses", {}).get(source.definition_id, []))
        if not projected and source.token_type == "UNIT":
            owner = rules.force_controllers[source.owner_country]
            slots.extend(Token(f"rule:{source.token_id}:{definition}", definition, "RULE", owner,
                               zone(owner, "OBJECT_POOL"), source.token_id)
                         for definition in rules.settings.get("unit_responses", []))
        for slot in slots:
            if slot.token_id in tokens:
                raise CoreError("Duplicate object in initial inventory")
            tokens[slot.token_id] = slot
    for definition, force in ({} if projected else rules.settings.get("turn_opportunities", {})).items():
        source = next(t for t in tokens.values() if t.token_type == "UNIT" and t.owner_country == force)
        owner = rules.force_controllers[force]
        token = Token("opportunity:" + definition, definition, "RULE", owner, zone(owner, "ABILITY_READY"), source.token_id)
        tokens[token.token_id] = token
    return tokens


def initialize(rules, seed=1, game_id="fixture-game"):
    from .operations import OperationExecutor, outcome

    positions = {p: [] for p in rules.regions}
    positions.update({zone(c, z): [] for c in TURN_ORDER for z in ZONES})
    positions.update({position: [] for position in sorted(auxiliary_reserves(rules))})
    positions.update({zone(c, "SET_ASIDE"): [] for c in rules.settings.get("staging_countries", ())})
    positions["RESOLUTION"] = []
    positions["SCORE_DORMANT"] = []
    tokens = rules.inventory.copy()
    for token in tokens.values():
        if token.position_id not in positions:
            raise CoreError("Invalid initial position")
        positions[token.position_id].append(token.token_id)
    for country in TURN_ORDER:
        deck = zone(country, "DECK")
        positions[deck], seed = shuffle(positions[deck], seed & 0xFFFFFFFF)
    state = GameState(game_id, rules.version, tokens, positions, TurnClock(plays_remaining=rules.settings["plays_per_turn"]), {"axis": 0, "allies": 0}, seed)
    executor = OperationExecutor(rules)
    deal = outcome("INITIAL_DEAL")
    for country in TURN_ORDER:
        deck = zone(country, "DECK")
        hand = zone(country, "HAND")
        for token_id in positions[deck][:rules.settings["initial_hand"]]:
            executor.transfer(state, token_id, hand, deal)
        if len(positions[hand]) < rules.settings["setup_discard"]:
            raise CoreError(f"Not enough cards for setup: {country}")
    validate_state(rules, state)
    capture_turn_start(rules, state)
    return state
