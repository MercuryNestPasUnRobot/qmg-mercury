"""Map transcribed event predicates onto committed Core operations."""
from .content_effects import condition_met, unit_matches
from .model import TURN_ORDER
from .operations import BATTLES, CONSTRUCTION
from .rules import MapGraph, SupplyResolver, board_units


SUPPORTED_TRIGGERS = frozenset({
    "battle-space", "after-build", "before-battle-removal", "before-any-removal",
    "after-unit-removal", "play-phase-start", "victory-start", "discard-start",
    "during-victory", "turn-start",
    "economic-play", "economic-target",
    "after-card-use", "play-army-card",
    "any-time",
    "before-card-effect", "after-card-effect",
    "unit-activity",
    "air-phase-start", "after-air-action", "after-air-superiority", "after-air-superiority-removal", "discard-stage-card",
    "before-rule-use",
})

TRIGGER_FIELDS = {
    "after-build": "kind builder builtForce near spaces adjacentForces adjacentKind ownTurnOnly oncePerTurn",
    "battle-space": "kind actor attackers space spaces near adjacentSupplied oncePerTurn",
    "before-battle-removal": "kind victim victims spaces near suppliedOnly oncePerTurn",
    "before-any-removal": "kind victims near spaces suppliedOnly",
    "after-unit-removal": "kind victim allRemoved suppliedOnly",
    "before-card-effect": "kind actor", "after-card-effect": "kind actor",
    "any-time": "ownTurnOnly", "economic-play": "actor keyword",
    "unit-activity": "actions force kind", "after-air-action": "spaceKind",
    "after-air-superiority": "near", "after-air-superiority-removal": "victim attackerSide",
    "discard-stage-card": "country kind",
}


def supported_trigger(trigger):
    return (trigger.get("type") in SUPPORTED_TRIGGERS and
            not set(trigger) - {"type", *TRIGGER_FIELDS.get(trigger["type"], "").split()})


def validate_trigger(rules, trigger):
    from .model import CoreError
    if not supported_trigger(trigger):
        raise CoreError("Unsupported event predicate")
    for key in ("actor", "builder"):
        if key in trigger and trigger[key] not in ("self", "team", "enemy", "any", *rules.countries):
            raise CoreError("Invalid event actor predicate")
    for key in ("victim", "force", "builtForce", "country"):
        if key in trigger and trigger[key] not in ("self", *rules.force_controllers):
            raise CoreError("Invalid event force predicate")
    for key in ("victims", "attackers", "adjacentForces"):
        if key in trigger and (not isinstance(trigger[key], list) or any(c not in rules.force_controllers for c in trigger[key])):
            raise CoreError("Invalid event force list")
    if "spaces" in trigger and any(p not in rules.regions for p in trigger["spaces"]):
        raise CoreError("Unknown event regions")
    if "space" in trigger and trigger["space"] not in rules.regions:
        raise CoreError("Unknown event region")
    if near := trigger.get("near"):
        if near.get("space") not in rules.regions or type(near.get("maxDistance")) is not int or near["maxDistance"] < 0:
            raise CoreError("Invalid event distance")


def matches_trigger(rules, state, listener, trigger, op, result):
    source = rules.cards[state.tokens[listener.source_token_id].definition_id]
    owner, kind, event = listener.owner_country, trigger["type"], result["event"]
    if kind == "any-time":
        return False
    if kind == "discard-stage-card":
        return (state.turn.phase == "Discard" and op.get("actor_country") == trigger["country"] and any(
            change["from"] == f"{trigger['country']}:HAND" and change["to"] == f"{trigger['country']}:DISCARD"
            and rules.cards[state.tokens[change["token_id"]].definition_id].get("legacy_kind") == trigger["kind"]
            for change in result["changes"] if state.tokens[change["token_id"]].token_type == "CARD"))
    if kind in ("after-air-action", "after-air-superiority", "after-air-superiority-removal"):
        if event != "AIR_ACTION_FINISHED":
            return False
        if kind == "after-air-superiority-removal":
            return (op.get("air_action") == "superiority" and op.get("removed_force") == trigger["victim"]
                    and rules.team(op["force"]) == trigger["attackerSide"])
        if op["force"] != owner or (op["air_action"] == "superiority") != (kind == "after-air-superiority"):
            return False
        return (not trigger.get("spaceKind") or rules.regions[op["target"]]["type"].lower() == trigger["spaceKind"]) and _region_matches(rules, state, owner, trigger, op["target"])
    if source.get("implementation", {}).get("status") == "blocked":
        return False
    if not condition_met(rules, state, owner, source.get("content_condition")):
        return False
    if kind == "before-rule-use":
        if event != "RULE_USE":
            return False
        from .rules import RuleModifierResolver
        modifiers = RuleModifierResolver(rules).resolve(state, op["rule_force"])
        return any(m["kind"] in op["rule_kinds"] and m in modifiers for m in source.get("modifiers", []))
    phases = {"play-phase-start": "Play", "turn-start": "Play", "air-phase-start": "Air",
              "victory-start": "Victory", "discard-start": "Discard"}
    if kind in phases:
        phase = state.turn.phase if event == "SETUP_DONE" else op.get("phase")
        return event in ("BEGIN_PHASE", "SETUP_DONE") and phase == phases[kind] and op.get("actor_country") == owner
    if kind in ("before-card-effect", "after-card-effect"):
        if event != ("CARD_DECLARED" if kind == "before-card-effect" else "CARD_EFFECT_FINISHED"):
            return False
        if op.get("declared_kind") != trigger["kind"]:
            return False
        actor = op["actor_country"]
        relation = trigger["actor"]
        return (relation == "any" or relation == "self" and actor == owner
                or relation == "team" and rules.team(actor) == rules.team(owner)
                or relation == "enemy" and rules.team(actor) != rules.team(owner) or actor == relation)
    if kind == "during-victory":
        return event == "CHECK_SCORE" and op.get("actor_country") == owner
    if kind in ("after-card-use", "play-army-card"):
        required_event = "OBJECT_SETTLED" if kind == "after-card-use" else "PLAY_CARD"
        if event != required_event or op.get("actor_country") != owner or state.turn.phase != "Play":
            return False
        source_token = state.tokens.get(op.get("source_token_id"))
        if source_token is None:
            return False
        source_definition = rules.cards.get(source_token.definition_id, {})
        if kind == "after-card-use":
            return source_definition.get("type") != "STATUS" and op.get("settled_definition") == source_token.definition_id
        return source_definition.get("legacy_kind") == "build-army" or op.get("play_as") == "BUILD_ARMY"
    if kind in ("economic-play", "economic-target"):
        if event != ("ECONOMIC_PLAY" if kind == "economic-play" else "ECONOMIC_TARGET"):
            return False
        if kind == "economic-target" and op.get("target_country") != owner:
            return False
        actor = op["actor_country"]
        relation = trigger.get("actor", "any")
        if (relation == "self" and actor != owner or relation == "enemy" and rules.team(actor) == rules.team(owner)
                or relation in rules.countries and actor != relation):
            return False
        event_source = state.tokens.get(op.get("source_token_id"))
        if "keyword" in trigger and (event_source is None or
                trigger["keyword"] not in (rules.cards[event_source.definition_id].get("text", "") + rules.cards[event_source.definition_id].get("title", ""))):
            return False
        return True
    if trigger.get("ownTurnOnly") and TURN_ORDER[state.turn.country_index] != owner:
        return False
    actor = op.get("actor_country")
    relation = trigger.get("actor", trigger.get("builder", "any"))
    if ((relation == "self" and actor != owner) or
            (relation == "team" and (actor is None or rules.team(actor) != rules.team(owner))) or
            (relation == "enemy" and (actor is None or rules.team(actor) == rules.team(owner))) or
            (relation in rules.countries and actor != relation)):
        return False
    physical = None
    if kind == "battle-space":
        if event not in BATTLES:
            return False
        unit_kind = "army" if event == "LAND_BATTLE" else "navy"
    elif kind == "after-build":
        if event not in CONSTRUCTION:
            return False
        unit_kind = op.get("unit_kind", "NAVY" if event == "BUILD_NAVY" else "ARMY").lower()
        built = next((change["token_id"] for change in result["changes"]
                      if change["to"] in rules.regions and state.tokens[change["token_id"]].token_type == "UNIT"), None)
        physical = state.tokens.get(built)
        if not physical:
            return False
        if trigger.get("builtForce", physical.owner_country) != physical.owner_country:
            return False
    elif kind in ("before-battle-removal", "before-any-removal", "after-unit-removal", "unit-activity"):
        announced = op.get("announced_type")
        before = kind.startswith("before-")
        if before != (event == "ANNOUNCE"):
            return False
        if kind == "before-battle-removal" and announced not in BATTLES:
            return False
        victims = []
        if event == "ANNOUNCE" and announced in BATTLES:
            victims = [state.tokens[key] for key in state.positions[op["target"]]
                       if state.tokens[key].token_type == "UNIT" and
                       rules.units[state.tokens[key].definition_id]["kind"] == ("ARMY" if announced == "LAND_BATTLE" else "NAVY") and
                       (not op.get("target_token") or key == op["target_token"])]
        elif event == "ANNOUNCE" and announced == "ELIMINATE":
            victims = [state.tokens[op["target_token"]]]
        elif event in BATTLES | {"ELIMINATE"}:
            victims = [state.tokens[c["token_id"]] for c in result["changes"]
                       if c["from"] in rules.regions and state.tokens[c["token_id"]].token_type == "UNIT"]
        if kind == "unit-activity":
            if event in BATTLES:
                victims = [state.tokens[key] for key in result["context"].get("attacked_units", [])]
            activity = "build" if event in ("BUILD_ARMY", "BUILD_NAVY") else "recruit" if event == "RECRUIT" else (
                "attacked" if event in BATTLES else None)
            if activity not in trigger["actions"]:
                return False
            if activity in ("build", "recruit"):
                return any(c["to"] in rules.regions and
                           state.tokens[c["token_id"]].owner_country == trigger["force"] and
                           rules.units[state.tokens[c["token_id"]].definition_id]["kind"].lower() == trigger["kind"]
                           for c in result["changes"])
        for token in victims:
            victim = trigger.get("victim", trigger.get("force"))
            if victim == "self" and token.owner_country != owner:
                continue
            if victim not in (None, "self") and token.owner_country != victim:
                continue
            if trigger.get("victims") and token.owner_country not in trigger["victims"]:
                continue
            if trigger.get("kind") and rules.units[token.definition_id]["kind"].lower() != trigger["kind"]:
                continue
            position = next((c["from"] for c in result["changes"] if c["token_id"] == token.token_id),
                            token.position_id)
            if not _region_matches(rules, state, owner, trigger, position):
                continue
            if trigger.get("suppliedOnly"):
                supplied = (token.token_id in SupplyResolver(rules).supplied(state, token.owner_country) if before else
                            result["context"].get("removed_supplied", {}).get(token.token_id, False))
                if not supplied:
                    continue
            if trigger.get("allRemoved") and any(t.owner_country == token.owner_country and
                    rules.units[t.definition_id]["kind"] == rules.units[token.definition_id]["kind"]
                    for t in board_units(rules, state)):
                continue
            return True
        return False
    else:
        return False
    if trigger.get("kind", unit_kind) != unit_kind:
        return False
    force = op.get("force", actor)
    if trigger.get("attackers") and force not in trigger["attackers"]:
        return False
    position = physical.position_id if physical else op["target"]
    if not _region_matches(rules, state, owner, trigger, position):
        return False
    if adjacent := trigger.get("adjacentSupplied"):
        definition = {**adjacent, "suppliedOnly": True}
        if not any(t.position_id in MapGraph(rules).neighbors(position, state, owner)
                   and unit_matches(rules, state, owner, t, definition) for t in board_units(rules, state)):
            return False
    if forces := trigger.get("adjacentForces"):
        neighbors = MapGraph(rules).neighbors(position, state, owner)
        if not any(t.position_id in neighbors and t.owner_country in forces and
                   (not trigger.get("adjacentKind") or
                    rules.units[t.definition_id]["kind"].lower() == trigger["adjacentKind"])
                   for t in board_units(rules, state)):
            return False
    return True


def _region_matches(rules, state, owner, trigger, position):
    if trigger.get("space", position) != position or ("spaces" in trigger and position not in trigger["spaces"]):
        return False
    if near := trigger.get("near"):
        if MapGraph(rules).distances(near["space"], state, owner).get(position, 1000) > near["maxDistance"]:
            return False
    return True
