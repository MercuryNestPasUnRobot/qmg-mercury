"""Application boundary: projected observations and the same commands for every client."""
from copy import deepcopy

from .model import CoreError, TURN_ORDER, Token, TurnClock, GameState, zone
from .engine import CommandProcessor
from .definitions import RuleSet

PUBLIC_ZONES = {"DISCARD", "STATUS", "RESERVE_ARMY", "RESERVE_NAVY"}


def observation(processor, controlled_countries):
    rules, s = processor.rules, processor.state
    controlled = set(controlled_countries)
    if not controlled <= set(TURN_ORDER):
        raise CoreError("Unknown seat")
    visible, positions = {}, {}
    for position, ids in s.positions.items():
        country, _, kind = position.partition(":")
        public = position in rules.regions or position == "RESOLUTION" or kind in PUBLIC_ZONES
        permitted = public or (country in controlled and kind != "DECK")
        positions[position] = {"count": len(ids)}
        if position == "SCORE_DORMANT" or (country not in controlled and (kind.startswith("ABILITY_") or kind == "SCORE_DORMANT")):
            positions[position]["count"] = 0
        if permitted:
            positions[position]["tokens"] = list(ids)
            for token_id in ids:
                visible[token_id] = deepcopy(s.tokens[token_id].__dict__)
    history = []
    for record in processor.records:
        source = record["source"]
        hidden_source = source is not None and source not in visible
        history.append({"sequence_id": record["sequence_id"],
                        "operation_type": "PRIVATE_OPERATION" if hidden_source else record["operation_type"],
                        "actor": record["actor"], "source": source if source in visible else None,
                        "changes": [deepcopy(c) for c in record["outcome"]["changes"] if c["token_id"] in visible],
                        "score_changes": deepcopy(record["outcome"]["score_changes"]),
                        "success": record["outcome"]["success"]})
    decision = None
    if s.decision:
        decision = {"required_country": s.decision.required_country}
        if s.decision.required_country in controlled:
            decision.update(decision_id=s.decision.decision_id, decision_type=s.decision.decision_type,
                            legal_options=list(s.decision.legal_options), optional_flag=s.decision.optional_flag)
    return {"game_id": s.game_id, "ruleset_version": rules.version, "version": s.version,
            "tokens": visible, "positions": positions, "turn": deepcopy(s.turn.__dict__),
            "scores": dict(s.scores), "winner": s.winner, "decision": decision,
            "resolution_depth": len(s.stack), "history": history}


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
