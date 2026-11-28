"""Serializable authority state; no derived flags live on tokens."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from hashlib import sha256
import json


class CoreError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode("utf-8")).hexdigest()


def zone(country, kind):
    return f"{country}:{kind}"


ZONES = ("DECK", "HAND", "DISCARD", "STATUS", "RESPONSE", "RESERVE_ARMY", "RESERVE_NAVY",
         "ABILITY_READY", "ABILITY_PENDING", "ABILITY_COOLDOWN", "ABILITY_INACTIVE", "SCORE_DORMANT", "OBJECT_POOL")
PHASES = ("Play", "Air", "Supply", "Victory", "Discard", "Draw")
TURN_ORDER = ("germany", "united-kingdom", "japan", "soviet-union", "italy", "united-states")


@dataclass(frozen=True)
class Token:
    token_id: str
    definition_id: str
    token_type: str
    owner_country: str | None
    position_id: str
    source_token_id: str | None = None


@dataclass
class TurnClock:
    round_id: int = 1
    country_index: int = 0
    phase: str = "Play"
    turn_id: int = 1
    plays_remaining: int = 1
    discarded_in_phase: bool = False


@dataclass
class ResolutionFrame:
    resolution_id: str
    source_card_id: str | None
    actor_country: str
    remaining_operations: list[dict]
    parent_resolution_id: str | None = None
    current_step: int = 0
    selected_targets: dict = field(default_factory=dict)
    eligible_token_ids: list[str] = field(default_factory=list)
    processed_token_ids: list[str] = field(default_factory=list)
    context: dict = field(default_factory=dict)


@dataclass
class DecisionRequest:
    decision_id: str
    required_country: str
    decision_type: str
    legal_options: list[str]
    originating_operation_id: str
    resolution_id: str
    optional_flag: bool = False
    continuation: dict = field(default_factory=dict)


@dataclass
class GameState:
    game_id: str
    ruleset_version: str
    tokens: dict[str, Token]
    positions: dict[str, list[str]]
    turn: TurnClock
    scores: dict[str, int]
    rng: int
    stack: list[ResolutionFrame] = field(default_factory=list)
    decision: DecisionRequest | None = None
    version: int = 0
    serial: int = 0
    winner: str | None = None
    announcements: dict = field(default_factory=dict)
    turn_start: dict = field(default_factory=dict)

    def to_dict(self):
        data = asdict(self)
        if not self.turn.discarded_in_phase:
            del data["turn"]["discarded_in_phase"]
        if not self.turn_start:
            del data["turn_start"]
        return data

    @classmethod
    def from_dict(cls, value):
        v = json.loads(canonical(value))
        v["tokens"] = {k: Token(**t) for k, t in v["tokens"].items()}
        v["turn"] = TurnClock(**v["turn"])
        v["stack"] = [ResolutionFrame(**f) for f in v["stack"]]
        v["decision"] = DecisionRequest(**v["decision"]) if v["decision"] else None
        return cls(**v)

    def new_id(self, prefix):
        self.serial += 1
        return f"{prefix}:{self.serial}"


class ZoneRegistry:
    @staticmethod
    def projected_transfer(state, token_id, destination):
        """Project one transfer for read-only evaluation, with no other state changes."""
        source = state.tokens[token_id].position_id
        projected = replace(state, tokens=dict(state.tokens), positions={
            **state.positions, source: list(state.positions[source]), destination: list(state.positions[destination])})
        ZoneRegistry.transfer(projected, token_id, destination)
        return projected

    @staticmethod
    def transfer(state, token_id, destination, index=None):
        if destination not in state.positions or token_id not in state.tokens:
            raise CoreError("Unknown token or position")
        token = state.tokens[token_id]
        source = token.position_id
        target = state.positions[destination]
        if index is not None and (type(index) is not int or not 0 <= index <= len(target)):
            raise CoreError("Invalid ordered-zone index")
        state.positions[source].remove(token_id)
        if index is None:
            target.append(token_id)
        else:
            target.insert(index, token_id)
        state.tokens[token_id] = replace(token, position_id=destination)
        return {"token_id": token_id, "from": source, "to": destination}


def shuffle(values, seed):
    # Same documented LCG/Fisher-Yates sequence as the frozen project's random.ts.
    result = list(values)
    for i in range(len(result) - 1, 0, -1):
        seed = (seed * 1664525 + 1013904223) & 0xFFFFFFFF
        j = int(seed / 0x100000000 * (i + 1))
        result[i], result[j] = result[j], result[i]
    return result, seed
