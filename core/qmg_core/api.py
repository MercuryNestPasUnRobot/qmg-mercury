"""Application boundary: projected observations and the same commands for every client."""
from copy import deepcopy

from .model import CoreError, TURN_ORDER, Token, TurnClock, GameState, zone
from .engine import CommandProcessor
from .definitions import RuleSet
from .rules import MapGraph

PUBLIC_ZONES = {"DISCARD", "STATUS", "RESERVE_ARMY", "RESERVE_NAVY"}


def _visible_position(position, rules, controlled):
    if position is None:
        return False
    country, _, kind = position.partition(":")
    return (position in rules.regions or position == "RESOLUTION" or kind in PUBLIC_ZONES
            or (country in controlled and kind != "DECK"))


def observation(processor, controlled_countries):
    rules, s = processor.rules, processor.state
    controlled = set(controlled_countries)
    if not controlled <= set(TURN_ORDER):
        raise CoreError("Unknown seat")
    visible, positions = {}, {}
    for position, ids in s.positions.items():
        country, _, kind = position.partition(":")
        permitted = _visible_position(position, rules, controlled)
        positions[position] = {"count": len(ids)}
        if position == "SCORE_DORMANT" or (country not in controlled and (kind.startswith("ABILITY_") or kind in ("SCORE_DORMANT", "SET_ASIDE"))):
            positions[position]["count"] = 0
        if permitted:
            positions[position]["tokens"] = list(ids)
            for token_id in ids:
                visible[token_id] = deepcopy(s.tokens[token_id].__dict__)
    history = []
    revealed_sources = set()
    for record in processor.records:
        source = record["source"]
        outcome = record["outcome"]
        if record["operation_type"] == "SET_RESPONSE":
            revealed_sources.discard(source)
        if revealed_source := outcome["context"].get("revealed_source"):
            revealed_sources.add(revealed_source)
        source_position = next((change["from"] for change in outcome["changes"] if change["token_id"] == source), None)
        source_definition = rules.cards.get(s.tokens[source].definition_id) if source in s.tokens else None
        response_private = source_definition and source_definition["type"] == "RESPONSE" and source not in revealed_sources and record["actor"] not in controlled
        hidden_source = source is not None and (response_private or (source not in visible and not _visible_position(source_position, rules, controlled)))
        history.append({"sequence_id": record["sequence_id"],
                        "operation_type": "PRIVATE_OPERATION" if hidden_source else record["operation_type"],
                        "actor": record["actor"], "source": None if hidden_source else source,
                        "changes": [deepcopy(c) for c in outcome["changes"]
                                    if _visible_position(c["to"], rules, controlled)
                                    and (c["token_id"] in visible or _visible_position(c["from"], rules, controlled))],
                        "score_changes": deepcopy(outcome["score_changes"]),
                        "success": outcome["success"]})
    decision = None
    if s.decision:
        decision = {"required_country": s.decision.required_country}
        if s.decision.required_country in controlled:
            decision.update(decision_id=s.decision.decision_id, decision_type=s.decision.decision_type,
                            legal_options=list(s.decision.legal_options), optional_flag=s.decision.optional_flag,
                            operation_type=s.decision.continuation["type"])
    resolution = []
    for frame in s.stack:
        source = frame.source_card_id
        if source in visible:
            resolution.append({"source": source, "country": frame.actor_country})
    return {"game_id": s.game_id, "ruleset_version": rules.version, "version": s.version,
            "tokens": visible, "positions": positions, "turn": deepcopy(s.turn.__dict__),
            "scores": dict(s.scores), "winner": s.winner, "decision": decision,
            "resolution_depth": len(s.stack), "resolution": resolution, "history": history}


class CoreAPI:
    """The host assigns seats; caller-provided network identity is not authentication."""
    def __init__(self, processor, controlled_countries):
        if not set(controlled_countries) <= set(TURN_ORDER):
            raise CoreError("Invalid seat assignment")
        self._processor = processor
        self.controlled_countries = tuple(controlled_countries)

    def observe(self):
        return observation(self._processor, self.controlled_countries)

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

    def submit(self, command):
        self._processor.submit(deepcopy(command), self.controlled_countries)
        return self.observe()


def simulation_from_observation(rules, view):
    """Never clone the authoritative hidden state, RNG, journal or deck order."""
    if (view["resolution_depth"] or view["decision"]) and not view["winner"]:
        raise CoreError("Start a candidate simulation at a quiescent action boundary")
    data = deepcopy(rules.data)
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
        if token.token_type == "CARD" and token.position_id == zone(token.owner_country, "STATUS"):
            for ability_id in rules.cards[token.definition_id].get("abilities", []):
                if not any(t.token_type == "ABILITY" and t.source_token_id == token.token_id and t.definition_id == ability_id for t in tokens.values()):
                    token_id = f"known-ability:{token.token_id}:{ability_id}"
                    p = zone(token.owner_country, "ABILITY_READY")
                    tokens[token_id] = Token(token_id, ability_id, "ABILITY", token.owner_country, p, token.token_id)
                    positions[p].append(token_id)
    data["initial_tokens"] = [deepcopy(t.__dict__) for t in tokens.values() if t.token_type != "ABILITY"]
    simulation_rules = RuleSet(data)
    state = GameState(view["game_id"], rules.version, tokens, positions, TurnClock(**view["turn"]),
                      deepcopy(view["scores"]), 1, version=view["version"], winner=view["winner"])
    return CommandProcessor(simulation_rules, state)
