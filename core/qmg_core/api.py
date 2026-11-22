"""Application boundary: projected observations and the same commands for every client."""
from copy import deepcopy
from collections import Counter

from .model import CoreError, TURN_ORDER, Token, TurnClock, GameState, zone
from .engine import CommandProcessor
from .definitions import RuleSet
from .rules import MapGraph, initial_abilities

PUBLIC_ZONES = {"DISCARD", "STATUS", "DISABLED_STATUS", "RESERVE_ARMY", "RESERVE_NAVY", "RESERVE_AIR"}


def _visible_position(position, rules, controlled):
    if position is None:
        return False
    country, _, kind = position.partition(":")
    return (position in rules.regions or position == "RESOLUTION" or kind in PUBLIC_ZONES
            or (country in controlled and kind != "DECK"))


def _reallocation_view(rules, s, controlled, visible):
    from .content_actions import BASIC_KINDS, reallocation_terms
    from .air import opportunity

    seat = TURN_ORDER[s.turn.country_index]
    if seat not in controlled or s.turn.phase != "Play" or not rules.settings.get("reallocation"):
        return None
    decision = s.decision
    own_exchange = decision and decision.required_country == seat and s.stack[-1].context.get("reallocation")
    if not own_exchange and (s.stack or s.turn.plays_remaining != rules.settings["plays_per_turn"] or
                             not opportunity(s, "reallocation-opportunity-" + seat)):
        return None

    def basic_kind(token):
        if token["owner_country"] == seat and token["token_type"] == "CARD":
            return rules.cards[token["definition_id"]].get("legacy_kind")
        return None

    # Infer only the active seat's aggregate composition from the fixed catalog
    # and visible cards outside the deck. The real deck contents aren't traversed.
    copies = Counter(map(basic_kind, rules.data["initial_tokens"]))
    seen = Counter(map(basic_kind, visible.values()))
    cost, discard = reallocation_terms(rules, s, seat)
    result = {"cost": cost, "discard_allowed": discard,
              "deck": {kind: max(0, copies[kind] - seen[kind]) for kind in BASIC_KINDS}}
    if own_exchange:
        continuation = decision.continuation
        effect = continuation.get("effect", {})
        if continuation["type"] == "REALLOCATION_SELECTED":
            result["payment"] = continuation["selected"]
        elif effect.get("type") == "choose-one":
            result["sources"] = {option: effect["options"][int(option)]["label"] for option in decision.legal_options}
        elif effect.get("type") in ("search-deck", "recover-discard"):
            result["source"] = "deck" if effect["type"] == "search-deck" else "discard"
            result["targets"] = {key: s.tokens[key].definition_id for key in sorted(decision.legal_options)}
    return result


def observation(processor, controlled_countries, *, include_history=True):
    rules, s = processor.rules, processor.state
    controlled = set(controlled_countries)
    if not controlled <= set(TURN_ORDER):
        raise CoreError("Unknown seat")
    visible, positions = {}, {}
    for position, ids in s.positions.items():
        country, _, kind = position.partition(":")
        permitted = _visible_position(position, rules, controlled)
        positions[position] = {"count": len(ids)}
        if position == "SCORE_DORMANT" or (country not in controlled and (kind.startswith("ABILITY_") or kind in ("SCORE_DORMANT", "SET_ASIDE", "OBJECT_POOL"))):
            positions[position]["count"] = 0
        if permitted:
            known = [key for key in ids if s.tokens[key].token_type not in ("ABILITY", "RULE") or
                     _visible_position(s.tokens[s.tokens[key].source_token_id].position_id, rules, controlled)]
            positions[position].update(tokens=known, count=len(known))
            for token_id in known:
                visible[token_id] = deepcopy(s.tokens[token_id].__dict__)
    history = []
    revealed_sources = set()
    public_objects = set()
    for record in processor.records if include_history else ():
        source = record["source"]
        outcome = record["outcome"]
        if record["operation_type"] == "SET_RESPONSE":
            revealed_sources.discard(source)
        if revealed_source := outcome["context"].get("revealed_source"):
            revealed_sources.add(revealed_source)
        moves = outcome["changes"]
        public_objects.update(c["token_id"] for c in moves if c["to"] == "RESOLUTION")
        obj = record["operation"].get("object_token_id")
        hidden_response = (record["operation"].get("object_origin") == zone(record["actor"], "RESPONSE")
                           and record["actor"] not in controlled and obj not in public_objects)
        source_position = next((change["from"] for change in outcome["changes"] if change["token_id"] == source), None)
        source_definition = rules.cards.get(s.tokens[source].definition_id) if source in s.tokens else None
        response_private = source_definition and source_definition["type"] == "RESPONSE" and source not in revealed_sources and record["actor"] not in controlled
        hidden_source = hidden_response or (source is not None and (response_private or (
            source not in visible and source not in public_objects and not _visible_position(source_position, rules, controlled))))
        history.append({"sequence_id": record["sequence_id"],
                        "operation_type": "PRIVATE_OPERATION" if hidden_source else record["operation_type"],
                        "actor": record["actor"], "source": None if hidden_source else source,
                        "changes": [deepcopy(c) for c in moves if _visible_position(c["to"], rules, controlled)
                                    and (c["token_id"] in visible or c["to"] == "RESOLUTION"
                                         or _visible_position(c["from"], rules, controlled))],
                        "score_changes": deepcopy(outcome["score_changes"]),
                        "success": outcome["success"]})
        public_objects.difference_update(c["token_id"] for c in moves if c["from"] == "RESOLUTION")
        revealed_sources.difference_update(c["token_id"] for c in moves if c["to"] == zone(record["actor"], "RESPONSE"))
    decision = None
    if s.decision:
        decision = {"required_country": s.decision.required_country}
        if s.decision.required_country in controlled:
            decision.update(decision_id=s.decision.decision_id, decision_type=s.decision.decision_type,
                            legal_options=list(s.decision.legal_options), optional_flag=s.decision.optional_flag,
                            operation_type=s.decision.continuation["type"])
            if s.decision.continuation["type"] == "RESPONSE_SELECTED":
                candidates = s.decision.continuation["candidates"]
                decision["response_sources"] = {
                    "use" if len(candidates) == 1 else key: s.tokens[key].source_token_id for key in candidates
                }
    resolution = []
    for frame in s.stack:
        ability_id = frame.context.get("ability_token")
        if frame.context.get("origin") == zone(frame.actor_country, "RESPONSE") and ability_id not in visible:
            continue
        source = ability_id if ability_id in s.positions["RESOLUTION"] else frame.source_card_id
        if source in visible:
            resolution.append({"source": source, "country": frame.actor_country})
    reallocation = _reallocation_view(rules, s, controlled, visible)
    return {"game_id": s.game_id, "ruleset_version": rules.version, "version": s.version,
            "tokens": visible, "positions": positions, "turn": deepcopy(s.turn.__dict__),
            "scores": dict(s.scores), "winner": s.winner, "decision": decision,
            "resolution_depth": len(s.stack), "resolution": resolution, "history": history,
            **({"reallocation": reallocation} if reallocation is not None else {}),
            **({"turn_start": deepcopy(s.turn_start)} if s.turn_start else {})}


class CoreAPI:
    """The host assigns seats; caller-provided network identity is not authentication."""
    def __init__(self, processor, controlled_countries):
        if not set(controlled_countries) <= set(TURN_ORDER):
            raise CoreError("Invalid seat assignment")
        self._processor = processor
        self.controlled_countries = tuple(controlled_countries)

    def observe(self, *, include_history=True):
        return observation(self._processor, self.controlled_countries, include_history=include_history)

    def legal_commands(self):
        return deepcopy(self._processor.legal_commands(self.controlled_countries))

    def regions(self):
        rules, state = self._processor.rules, self._processor.state
        graph = MapGraph(rules)
        country = self.controlled_countries[0] if self.controlled_countries else None
        neighbors = graph.adjacency(state, country)
        return {region: {"type": definition["type"],
                         "neighbors": sorted(neighbors[region]),
                         "base_vp": sum(site["vp"] for site in rules.sites.values()
                                        if site["region"] == region and not site.get("dynamic"))}
                for region, definition in rules.regions.items()}

    def submit(self, command, *, include_history=True):
        self._processor.submit(deepcopy(command), self.controlled_countries)
        return self.observe(include_history=include_history)


def simulation_from_observation(rules, view):
    """Never clone the authoritative hidden state, RNG, journal or deck order."""
    if (view["resolution_depth"] or view["decision"]) and not view["winner"]:
        raise CoreError("Start a candidate simulation at a quiescent action boundary")
    data = deepcopy({**rules.data, "initial_tokens": []})
    tokens = {k: Token(**v) for k, v in view["tokens"].items()}
    positions = {}
    for country in TURN_ORDER:
        data["cards"].append({"id": f"unknown-{country}", "country": country, "type": "EVENT",
                              "effects": [], "abilities": [], "failure_policy": "stop", "simulation_unknown": True})
    for p, descriptor in view["positions"].items():
        if "tokens" in descriptor:
            positions[p] = list(descriptor["tokens"])
        else:
            country, _, kind = p.partition(":")
            positions[p] = []
            # Hidden ability replicas are not observable resources and are not invented.
            if kind.startswith("ABILITY_"):
                continue
            for i in range(descriptor["count"]):
                token_id = f"unknown:{country}:{kind}:{i}"
                tokens[token_id] = Token(token_id, f"unknown-{country}", "CARD", country, p)
                positions[p].append(token_id)
    # Public deployed statuses are known. Conservatively model their abilities as ready;
    # never read their actual hidden replica positions or private usage history.
    for token in list(tokens.values()):
        if (token.token_type == "CARD" and token.position_id == zone(token.owner_country, "STATUS") and
                "tokens" not in view["positions"][zone(token.owner_country, "ABILITY_READY")]):
            for replica in initial_abilities(rules, token, include_pool=zone(token.owner_country, "OBJECT_POOL") in positions):
                tokens[replica.token_id] = replica
                positions[replica.position_id].append(replica.token_id)
    state = GameState(view["game_id"], rules.version, tokens, positions, TurnClock(**view["turn"]),
                      deepcopy(view["scores"]), 1, version=view["version"], winner=view["winner"],
                      turn_start=deepcopy(view.get("turn_start", {})))
    if zone(TURN_ORDER[0], "OBJECT_POOL") not in positions:
        from .operations import OperationExecutor
        from .persistence import upgrade_object_pools
        upgrade_object_pools(state, RuleSet(data), OperationExecutor(rules))
    data["initial_tokens"] = [deepcopy(t.__dict__) for t in tokens.values()]
    simulation_rules = RuleSet(data, projected=True)
    return CommandProcessor(simulation_rules, state)
