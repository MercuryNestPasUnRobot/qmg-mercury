from collections import deque
from copy import deepcopy

from .definitions import CONNECTION_USES
from .model import CoreError, ZONES, TURN_ORDER, GameState, Token, TurnClock, ZoneRegistry, zone, shuffle


class RuleModifierResolver:
    def __init__(self, rules):
        self.rules = rules

    def resolve(self, state, country):
        result = []
        for token in sorted(state.tokens.values(), key=lambda item: item.token_id):
            if token.token_type == "CARD" and token.position_id == zone(token.owner_country, "STATUS"):
                for modifier in self.rules.cards[token.definition_id].get("modifiers", []):
                    if modifier.get("scope", "owner") == "all" or token.owner_country == country:
                        result.append(deepcopy(modifier))
        return result


class MapGraph:
    def __init__(self, rules):
        self.rules = rules

    def adjacency(self, state=None, country=None, purpose=None):
        if purpose is not None and purpose not in CONNECTION_USES:
            raise CoreError("Unknown connection use")
        edges = {}
        for connection in self.rules.connections.values():
            edge = tuple(sorted((connection["a"], connection["b"])))
            edges.setdefault(edge, set()).update(connection.get("movement_or_supply_tags", CONNECTION_USES))
        if state is not None and country is not None:
            for m in RuleModifierResolver(self.rules).resolve(state, country):
                if m["kind"] == "connection":
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


class SupplyResolver:
    def __init__(self, rules):
        self.rules, self.graph = rules, MapGraph(rules)

    def supplied(self, state, country):
        modifiers = RuleModifierResolver(self.rules).resolve(state, country)
        if any(m["kind"] == "always_supplied" for m in modifiers):
            return {t.token_id for t in board_units(self.rules, state, country)}
        share = self.rules.settings["allied_supply"]
        pieces = [t for t in board_units(self.rules, state) if t.owner_country == country or
                  (share and self.rules.team(t.owner_country) == self.rules.team(country))]
        occupied = {t.position_id for t in pieces}
        sources = {r["id"] for r in self.rules.regions.values() if r.get("supply_source_for") == country}
        sources.update(m["region"] for m in modifiers if m["kind"] == "supply_source")
        reached = sources & occupied
        queue = deque(sorted(reached))
        adjacency = self.graph.adjacency(state, country, "supply")
        while queue:
            for n in adjacency[queue.popleft()]:
                if n in occupied and n not in reached:
                    reached.add(n)
                    queue.append(n)
        return {t.token_id for t in pieces if t.owner_country == country and t.position_id in reached}


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

    def targets(self, state, country, op):
        kind = op["type"]
        if kind in ("BUILD_ARMY", "BUILD_NAVY", "RECRUIT"):
            unit_kind = op.get("unit_kind", "NAVY" if kind == "BUILD_NAVY" else "ARMY")
            reserve = state.positions[zone(country, f"RESERVE_{unit_kind}")]
            if not reserve:
                return []
            expected_terrain = "LAND" if unit_kind == "ARMY" else "SEA"
            units = board_units(self.rules, state)
            supplied = self.supply.supplied(state, country)
            origins = {t.position_id for t in units if t.token_id in supplied}
            homes = {r["id"] for r in self.rules.regions.values() if r.get("home_country") == country}
            modifiers = RuleModifierResolver(self.rules).resolve(state, country)
            extra = {m["region"] for m in modifiers if m["kind"] == "build_region" and m["unit_kind"] == unit_kind}
            adjacency = self.graph.adjacency(state, country, "build")
            result = []
            for region, definition in self.rules.regions.items():
                if definition["type"] != expected_terrain:
                    continue
                present = [t for t in units if t.position_id == region]
                if any(self.rules.team(t.owner_country) != self.rules.team(country) or t.owner_country == country for t in present):
                    continue
                neighbors = adjacency[region]
                reachable = region in homes or bool(neighbors & origins) or region in extra
                if kind == "RECRUIT":
                    reachable = region in homes or bool(neighbors & homes)
                if reachable:
                    result.append(region)
            return sorted(result)
        if kind in ("LAND_BATTLE", "SEA_BATTLE"):
            unit_kind = "ARMY" if kind == "LAND_BATTLE" else "NAVY"
            terrain = "LAND" if unit_kind == "ARMY" else "SEA"
            supplied = self.supply.supplied(state, country)
            origins = {t.position_id for t in board_units(self.rules, state, country) if t.token_id in supplied}
            adjacency = self.graph.adjacency(state, country, "battle")
            result = []
            for region, d in self.rules.regions.items():
                if d["type"] != terrain or not (adjacency[region] & origins):
                    continue
                occupants = [t for t in board_units(self.rules, state) if t.position_id == region]
                if not any(self.rules.team(t.owner_country) == self.rules.team(country) for t in occupants):
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
                if position == token.position_id:
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
            if rules.regions[position]["type"] != terrain:
                raise CoreError("Unit terrain mismatch")
            for occupant_id in state.positions[position]:
                occupant = state.tokens[occupant_id]
                if occupant.token_type == "UNIT" and occupant_id != token.token_id and (
                    occupant.owner_country == country or rules.team(occupant.owner_country) != rules.team(country)
                ):
                    raise CoreError("Illegal hostile coexistence or duplicate national unit")
        elif position != zone(country, f"RESERVE_{kind}"):
            raise CoreError("Invalid reserve")
    elif token.token_type == "CARD":
        if position not in {"RESOLUTION", *(zone(country, kind) for kind in ("DECK", "HAND", "DISCARD", "STATUS", "RESPONSE", "SET_ASIDE"))}:
            raise CoreError("Invalid card zone")
    elif token.token_type == "SCORE_SOURCE":
        dormant = zone(country, "SCORE_DORMANT") if country else "SCORE_DORMANT"
        if position != dormant and position not in rules.regions:
            raise CoreError("Invalid dynamic score source")
    elif token.token_type == "ABILITY":
        raise CoreError("Ability positions follow their source card and activation lifecycle")


def validate_state(rules, state):
    if state.ruleset_version != rules.version or set(state.scores) != {"axis", "allies"}:
        raise CoreError("State/rules mismatch")
    if not isinstance(state.game_id, str) or not state.game_id or any(type(v) is not int or v < 0 for v in (state.version, state.serial, state.rng)) or state.rng > 0xFFFFFFFF:
        raise CoreError("Invalid state identity, version, counter or RNG")
    if any(type(v) is not int or v < 1 for v in (state.turn.round_id, state.turn.turn_id)) or type(state.turn.country_index) is not int or type(state.turn.plays_remaining) is not int or state.turn.plays_remaining < 0:
        raise CoreError("Invalid turn counters")
    expected_positions = set(rules.regions) | {"RESOLUTION", "SCORE_DORMANT"} | {zone(c, z) for c in TURN_ORDER for z in ZONES}
    expected_positions.update(zone(c, "SET_ASIDE") for c in rules.settings.get("staging_countries", ()))
    if set(state.positions) != expected_positions:
        raise CoreError("Position registry mismatch")
    seen = set()
    for position, ids in state.positions.items():
        for token_id in ids:
            if token_id in seen or token_id not in state.tokens or state.tokens[token_id].position_id != position:
                raise CoreError("Every token must occupy exactly one position")
            seen.add(token_id)
    if seen != set(state.tokens):
        raise CoreError("Unpositioned token")
    for key, t in state.tokens.items():
        if key != t.token_id or (t.owner_country not in rules.countries and not (t.token_type == "SCORE_SOURCE" and t.owner_country is None)):
            raise CoreError("Invalid token identity/owner")
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
            if not source or source.token_type != "CARD" or t.owner_country != source.owner_country or t.definition_id not in rules.cards[source.definition_id].get("abilities", []):
                raise CoreError("Invalid ability source")
            if t.position_id not in {zone(t.owner_country, z) for z in ZONES if z.startswith("ABILITY_")}:
                raise CoreError("Invalid ability lifecycle zone")
            resolving_response = (source.position_id == "RESOLUTION" and rules.cards[source.definition_id]["type"] == "RESPONSE"
                                  and any(frame.source_card_id == source.token_id and frame.context.get("ability_token") == t.token_id
                                          for frame in state.stack))
            if source.position_id not in (zone(t.owner_country, "STATUS"), zone(t.owner_country, "RESPONSE")) and not resolving_response and t.position_id != zone(t.owner_country, "ABILITY_INACTIVE"):
                raise CoreError("Orphan active ability")
        elif t.token_type == "SCORE_SOURCE":
            if t.definition_id not in rules.sites:
                raise CoreError("Invalid dynamic score source")
            validate_token_position(rules, state, t, t.position_id)
        else:
            raise CoreError("Unknown token type")
    if state.turn.phase not in (*("Setup",), "Play", "Supply", "Victory", "Discard", "Draw") or not 0 <= state.turn.country_index < 6:
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
    if state.decision and (state.decision.resolution_id not in frame_ids or not state.decision.legal_options or
                           state.decision.required_country not in rules.countries):
        raise CoreError("Unresumable decision")
    if state.decision and (state.decision.resolution_id != frame_ids[-1] or state.decision.required_country != state.stack[-1].actor_country or len(state.decision.legal_options) != len(set(state.decision.legal_options))):
        raise CoreError("Decision does not block the active resolution frame")


def initialize(rules, seed=1, game_id="fixture-game"):
    positions = {p: [] for p in rules.regions}
    positions.update({zone(c, z): [] for c in TURN_ORDER for z in ZONES})
    positions.update({zone(c, "SET_ASIDE"): [] for c in rules.settings.get("staging_countries", ())})
    positions["RESOLUTION"] = []
    positions["SCORE_DORMANT"] = []
    tokens = {}
    for row in rules.data["initial_tokens"]:
        token = Token(**row)
        if token.token_id in tokens or token.position_id not in positions:
            raise CoreError("Duplicate token or invalid initial position")
        tokens[token.token_id] = token
        positions[token.position_id].append(token.token_id)
    for token in list(tokens.values()):
        if token.token_type == "CARD":
            for ability in rules.cards[token.definition_id].get("abilities", []):
                token_id = f"ability:{token.token_id}:{ability}"
                active = token.position_id in (zone(token.owner_country, "STATUS"), zone(token.owner_country, "RESPONSE"))
                p = zone(token.owner_country, "ABILITY_READY" if active else "ABILITY_INACTIVE")
                if token_id in tokens:
                    raise CoreError("Ability identity collision")
                tokens[token_id] = Token(token_id, ability, "ABILITY", token.owner_country, p, token.token_id)
                positions[p].append(token_id)
    for country in TURN_ORDER:
        deck = zone(country, "DECK")
        positions[deck], seed = shuffle(positions[deck], seed & 0xFFFFFFFF)
    state = GameState(game_id, rules.version, tokens, positions, TurnClock(plays_remaining=rules.settings["plays_per_turn"]), {"axis": 0, "allies": 0}, seed)
    for country in TURN_ORDER:
        deck = zone(country, "DECK")
        for token_id in positions[deck][:rules.settings["initial_hand"]]:
            ZoneRegistry.transfer(state, token_id, zone(country, "HAND"))
    validate_state(rules, state)
    return state
