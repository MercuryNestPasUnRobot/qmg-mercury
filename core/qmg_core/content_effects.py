"""Declarative card instructions executed through the ordinary resolution stack."""
from copy import deepcopy

from .model import CoreError, TURN_ORDER, shuffle, zone
from .operations import outcome, outcome_failure
from .rules import LegalActionGenerator, MapGraph, SupplyResolver, board_units, home_regions


SUPPORTED_EFFECTS = frozenset({
    "points", "points-per-unit", "points-per-occupied-space", "discard-top", "discard-top-per-unit",
    "build", "recruit", "choose-space", "choose-unit", "battle", "choose-one", "discard-hand",
    "shuffle-deck", "recover-discard", "discard-response", "discard-status", "choose-country",
    "put-hand-on-deck-bottom", "search-deck", "random-discard-to-deck-top",
    "prevent-removal", "eliminate-built-unit", "discard-triggered-status",
    "request-hand-discard",
    "build-at-battle-space", "recruit-at-battle-space", "recruit-at-removed-space",
    "redeploy-armies", "order-deck-top",
    "protect-unit-this-turn", "supply-this-turn",
    "cancel-economic", "economic-target-adjust", "prevent-economic-discard", "economic-discard-bonus",
    "play-from-zone",
    "shuffle-used-card", "return-declared-army-card", "return-played-card",
    "protect-units-this-turn", "supply-units-this-turn",
    "cancel-card-effect",
    "battle-series",
    "stage-bolster", "reveal-discard-cards", "return-revealed-discard", "country-loss-choice",
    "air-action", "eliminate-air-superiority-attacker", "forbid-air-defense-this-turn", "disable-triggered-status",
})

# Reject unrecognized qualifiers instead of accepting a card with a partial effect.
EFFECT_FIELDS = {
    "air-action": "force actions baseKind near",
    "battle": "kind force spaces near nearBuilt nearAirAction atAirAction battleRange adjacentUnit optional",
    "battle-series": "kind min max filter atTurnStartAdjacent",
    "build": "force kind space", "recruit": "force kind space",
    "build-at-battle-space": "force kind", "recruit-at-battle-space": "force kind", "recruit-at-removed-space": "force kind",
    "choose-space": "action kind force chooser spaces homeRange near nearBuilt nearAirAction battleDistance optional",
    "choose-unit": "filter nearAirAction optional", "choose-one": "chooser options",
    "choose-country": "side action count countries homeProximity skipIfNoTarget",
    "country-loss-choice": "count target removeKinds removeSpaces",
    "discard-hand": "count kinds strict", "discard-response": "target", "discard-status": "activeOnly",
    "discard-top": "target count when", "discard-top-per-unit": "target countPer filter",
    "economic-discard-bonus": "amount perAirNearTargetHome bonusIf", "economic-target-adjust": "amount",
    "order-deck-top": "count", "play-from-zone": "zone kinds optional target textIncludes fromLastSearchOnly revealedOnly drawAfter shuffleAfter",
    "points": "amount side when", "points-per-unit": "amountPer filter side",
    "points-per-occupied-space": "amountPer side spaces occupiedBy",
    "protect-units-this-turn": "filter", "supply-units-this-turn": "filter", "supply-this-turn": "force",
    "put-hand-on-deck-bottom": "min max", "recover-discard": "kind kinds target destination count",
    "redeploy-armies": "count", "request-hand-discard": "target min max onDecline onPaidRecovery",
    "return-played-card": "discardHandCount", "reveal-discard-cards": "countries", "search-deck": "min max kinds",
    "random-discard-to-deck-top": "target count mode",
}


def force_id(value, country):
    return country if value in (None, "self") else value


def unit_matches(rules, state, country, token, definition):
    force = definition.get("force", "any")
    same_team = rules.team(token.owner_country) == rules.team(country)
    if ((force == "self" and token.owner_country != country) or
            (force == "team" and not same_team) or (force == "enemy" and same_team) or
            (force not in ("self", "team", "enemy", "any") and token.owner_country != force)):
        return False
    kind = rules.units[token.definition_id]["kind"].lower()
    if definition.get("forces") and token.owner_country not in definition["forces"]:
        return False
    if definition.get("kind", kind) != kind or ("kinds" in definition and kind not in definition["kinds"]):
        return False
    if "spaces" in definition and token.position_id not in definition["spaces"]:
        return False
    if token.position_id in definition.get("excludeSpaces", ()):
        return False
    graph = MapGraph(rules)
    if distance := definition.get("distance"):
        actual = graph.distances(distance["from"], state, country).get(token.position_id)
        if actual is None or not distance.get("min", 0) <= actual <= distance["max"]:
            return False
    if definition.get("suppliedOnly") and token.token_id not in SupplyResolver(rules).supplied(state, token.owner_country):
        return False
    if adjacent := definition.get("adjacentUnit"):
        neighbors = graph.neighbors(token.position_id, state, country)
        adjacent_filter = {"force": adjacent["force"], "kind": adjacent["kind"]}
        if not any(t.position_id in neighbors and unit_matches(rules, state, country, t, adjacent_filter)
                   for t in board_units(rules, state)):
            return False
    return True


def filtered_units(rules, state, country, definition):
    return [token for token in board_units(rules, state) if unit_matches(rules, state, country, token, definition)]


def condition_met(rules, state, country, condition):
    if condition is None:
        return True
    if condition["type"] == "all":
        return all(condition_met(rules, state, country, item) for item in condition["conditions"])
    count = len(filtered_units(rules, state, country, condition["filter"]))
    return condition["min"] <= count <= condition.get("max", count)


def instruction(effect):
    return {"type": "CARD_EFFECT", "effect": deepcopy(effect)}


def supported_effect(effect):
    if effect.get("type") not in SUPPORTED_EFFECTS:
        return False
    if set(effect) - {"type", *EFFECT_FIELDS.get(effect["type"], "").split()}:
        return False
    if effect["type"] == "choose-one":
        return all(supported_effect(child) for option in effect["options"] for child in option["effects"])
    return True


def validate_condition(rules, condition):
    if not isinstance(condition, dict):
        raise CoreError("Condition must be an object")
    if condition.get("type") == "all":
        if not isinstance(condition.get("conditions"), list):
            raise CoreError("Combined condition requires a list")
        for child in condition["conditions"]:
            validate_condition(rules, child)
        return
    if condition.get("type") != "unit-count":
        raise CoreError("Unknown content condition")
    validate_filter(rules, condition["filter"])
    for key in ("min", "max"):
        if key in condition and (type(condition[key]) is not int or condition[key] < 0):
            raise CoreError("Invalid unit-count bound")
    if "min" not in condition or condition.get("max", condition["min"]) < condition["min"]:
        raise CoreError("Invalid unit-count range")


def validate_filter(rules, definition):
    if not isinstance(definition, dict) or definition.get("force") not in (
            "self", "team", "enemy", "any", *rules.force_controllers):
        raise CoreError("Invalid unit filter force")
    for key in ("spaces", "excludeSpaces"):
        if key in definition and (not isinstance(definition[key], list)
                                 or any(p not in rules.regions for p in definition[key])):
            raise CoreError("Invalid unit filter regions")
    if "kind" in definition and definition["kind"] not in ("army", "navy", "air"):
        raise CoreError("Invalid unit filter kind")
    if "kinds" in definition and (not isinstance(definition["kinds"], list) or
                                 any(k not in ("army", "navy", "air") for k in definition["kinds"])):
        raise CoreError("Invalid unit filter kinds")
    if distance := definition.get("distance"):
        if (distance.get("from") not in rules.regions or type(distance.get("max")) is not int
                or type(distance.get("min", 0)) is not int or not 0 <= distance.get("min", 0) <= distance["max"]):
            raise CoreError("Invalid unit filter distance")


def validate_effect(rules, effect):
    if not supported_effect(effect):
        raise CoreError("Unimplemented transcribed effect")
    for key in ("count", "countPer", "min", "max", "homeRange", "discardHandCount"):
        if key in effect and (type(effect[key]) is not int or effect[key] < 0):
            raise CoreError("Effect quantity must be a nonnegative integer")
    if "min" in effect and effect.get("max", effect["min"]) < effect["min"]:
        raise CoreError("Invalid effect choice range")
    for key in ("amount", "amountPer"):
        if key in effect and type(effect[key]) is not int:
            raise CoreError("Effect amount must be an integer")
    if "when" in effect:
        validate_condition(rules, effect["when"])
    if "filter" in effect:
        validate_filter(rules, effect["filter"])
    for key in ("spaces", "removeSpaces"):
        if key in effect and (not isinstance(effect[key], list) or any(p not in rules.regions for p in effect[key])):
            raise CoreError("Unknown effect regions")
    if "space" in effect and effect["space"] not in rules.regions:
        raise CoreError("Unknown effect region")
    if "force" in effect and effect["force"] not in ("self", *rules.force_controllers):
        raise CoreError("Unknown effect force")
    if "chooser" in effect and effect["chooser"] not in rules.countries:
        raise CoreError("Unknown choosing seat")
    if effect["type"] == "air-action" and (not isinstance(effect.get("actions"), list) or not effect["actions"] or
                                           not set(effect["actions"]) <= {"deploy", "marshal", "superiority"}):
        raise CoreError("Invalid air action choices")
    if effect["type"] == "choose-one":
        if not isinstance(effect.get("options"), list) or not effect["options"]:
            raise CoreError("Branch effect requires options")
        for branch in effect["options"]:
            if not isinstance(branch.get("label"), str) or not isinstance(branch.get("effects"), list):
                raise CoreError("Branch effect requires a label and effects")
            for child in branch["effects"]:
                validate_effect(rules, child)


def event_effect(effect, frame):
    effect = deepcopy(effect)
    event = frame.context.get("event", {})
    outcome = frame.context.get("event_outcome", {})
    battle_region = event.get("target")
    built_region = frame.context.get("last_built_region", event.get("target"))
    if effect.pop("nearAirAction", False):
        if event.get("type") != "AIR_ACTION_FINISHED":
            raise CoreError("Effect needs a recorded air action")
        if effect["type"] == "choose-unit":
            effect["filter"] = {**effect["filter"], "distance": {"from": event["target"], "min": 1, "max": 1}}
        else:
            effect["near"] = {"space": event["target"], "minDistance": 1, "maxDistance": 1}
    if effect.pop("atAirAction", False):
        if event.get("type") != "AIR_ACTION_FINISHED" or event.get("air_action") != "superiority":
            raise CoreError("Effect needs recorded air superiority")
        effect["space"] = event["target"]
    if effect.get("nearBuilt"):
        if built_region is None:
            raise CoreError("Effect needs a recorded construction region")
        effect["near"] = {"space": built_region, "minDistance": 1, "maxDistance": 1}
        del effect["nearBuilt"]
    for key in ("battleDistance", "battleRange"):
        if key in effect:
            if battle_region is None:
                raise CoreError("Effect needs a recorded battle region")
            effect["near"] = {"space": battle_region, "maxDistance": effect.pop(key)}
    if effect["type"] in ("build-at-battle-space", "recruit-at-battle-space", "recruit-at-removed-space"):
        kind = effect["type"]
        if kind == "recruit-at-removed-space":
            battle_region = next((c["from"] for c in outcome.get("changes", []) if c["from"] is not None), None)
        if battle_region is None:
            raise CoreError("Effect needs a recorded removal or battle region")
        effect.update(type="build" if kind == "build-at-battle-space" else "recruit", space=battle_region)
    if effect["type"] == "battle" and effect["kind"] == "same":
        operation = event.get("announced_type", event.get("type"))
        effect["kind"] = "navy" if operation in ("SEA_BATTLE", "BUILD_NAVY") else "army"
    return effect


def action_for(effect, country):
    kind = effect["type"]
    unit_kind = effect.get("kind", "army").upper()
    if kind == "battle":
        operation = "LAND_BATTLE" if unit_kind == "ARMY" else "SEA_BATTLE"
    elif kind == "recruit" or effect.get("action") == "recruit":
        operation = "RECRUIT"
    else:
        operation = "BUILD_" + unit_kind
    return {"type": operation, "force": force_id(effect.get("force"), country),
            "unit_kind": unit_kind, "target": effect.get("space", "$choice"), "allied_effect": True}


def target_regions(rules, state, country, effect):
    action = action_for(effect, country)
    targets = LegalActionGenerator(rules).targets(state, country, action)
    graph = MapGraph(rules)
    if "space" in effect:
        targets = [p for p in targets if p == effect["space"]]
    if "spaces" in effect:
        targets = [p for p in targets if p in effect["spaces"]]
    if near := effect.get("near"):
        distances = graph.distances(near["space"], state, country)
        targets = [p for p in targets if near.get("minDistance", 0) <= distances.get(p, near["maxDistance"] + 1) <= near["maxDistance"]]
    if "homeRange" in effect:
        homes = home_regions(rules, state, action["force"])
        within = {p for home in homes for p, d in graph.distances(home, state, country).items()
                  if d <= effect["homeRange"]}
        targets = [p for p in targets if p in within]
    if adjacent := effect.get("adjacentUnit"):
        origins = filtered_units(rules, state, country, {"force": adjacent["force"], "kind": adjacent["kind"]})
        within = {p for t in origins for p in graph.neighbors(t.position_id, state, country)}
        targets = [p for p in targets if p in within]
    return targets


def available_options(rules, state, country, effect, context=None):
    kind = effect["type"]
    if kind == "air-action":
        from .air import air_options
        return air_options(rules, state, country, effect)
    if kind == "battle-series":
        units = filtered_units(rules, state, country, effect["filter"])
        if adjacent := effect.get("atTurnStartAdjacent"):
            original = state.turn_start.get("units", {})
            graph = state.turn_start.get("adjacency", {}).get(country, {})
            origins = [original[t.token_id] for t in state.tokens.values() if t.token_type == "UNIT"
                       and t.token_id in original and t.owner_country == force_id(adjacent["force"], country)
                       and rules.units[t.definition_id]["kind"].lower() == adjacent["kind"]]
            targets = {region for origin in origins for region in graph.get(origin, [])}
            units = [t for t in units if t.token_id in original and original[t.token_id] in targets
                     and t.position_id == original[t.token_id]]
        return [t.token_id for t in units]
    if kind == "battle" and effect["kind"] == "either":
        return [unit_kind + "|" + target for unit_kind in ("army", "navy")
                for target in target_regions(rules, state, country, {**effect, "kind": unit_kind})]
    if kind in ("build", "recruit", "choose-space", "battle"):
        return target_regions(rules, state, country, effect)
    if kind == "choose-unit":
        return [t.token_id for t in filtered_units(rules, state, country, effect["filter"])]
    if kind == "choose-one":
        return [str(i) for i, option in enumerate(effect["options"])
                if branch_can_begin(rules, state, country, option["effects"], context)]
    if kind == "play-from-zone":
        owner = force_id(effect.get("target"), country)
        legal = LegalActionGenerator(rules)
        result = []
        if sum(f.context.get("extra_play", False) for f in state.stack) >= 4:
            return result
        for key in state.positions[zone(owner, effect["zone"].upper())]:
            card = rules.cards[state.tokens[key].definition_id]
            if effect.get("fromLastSearchOnly") and key not in (context or {}).get("search_tokens", []):
                continue
            if effect.get("revealedOnly") and key != (context or {}).get("revealed_discard", {}).get(owner):
                continue
            if effect.get("kinds") and card.get("legacy_kind") not in effect["kinds"]:
                continue
            if effect.get("textIncludes") and effect["textIncludes"] not in card.get("title", "") + card.get("text", ""):
                continue
            first = card["effects"][0] if card["effects"] else None
            recursive = first and first["type"] == "CARD_EFFECT" and first["effect"]["type"] == "play-from-zone"
            if not recursive and not legal.can_start_card(state, owner, card):
                continue
            if card.get("implementation", {}).get("status") != "blocked":
                result.append(key)
        return result
    if kind == "stage-bolster":
        return [key for key in state.positions[zone(country, "HAND")]
                if rules.cards[state.tokens[key].definition_id].get("legacy_kind") == "bolster"]
    if kind == "choose-country":
        return [c for c in effect.get("countries", TURN_ORDER)
                if (effect["side"] == "any" or (rules.team(c) == rules.team(country)) == (effect["side"] == "team"))
                and (not effect.get("homeProximity") or any(
                    MapGraph(rules).distances(t.position_id, state, country).get(home["id"], 1000)
                    <= effect["homeProximity"]["maxDistance"]
                    for t in filtered_units(rules, state, country, effect["homeProximity"]["filter"])
                    for home in rules.regions.values() if home.get("home_country") == c))]
    target = force_id(effect.get("target"), country)
    source = {"discard-hand": "HAND", "put-hand-on-deck-bottom": "HAND", "search-deck": "DECK",
              "recover-discard": "DISCARD", "discard-response": "RESPONSE", "discard-status": "STATUS"}.get(kind)
    if source:
        kinds = effect.get("kinds", [effect["kind"]] if kind == "recover-discard" and "kind" in effect else None)
        zones = [source, "DISABLED_STATUS"] if kind == "discard-status" and not effect.get("activeOnly") else [source]
        return [key for source_zone in zones for key in state.positions.get(zone(target, source_zone), [])
                if state.tokens[key].token_type == "CARD" and key != (context or {}).get("excluded_card") and
                (kinds is None or rules.cards[state.tokens[key].definition_id].get("legacy_kind") in kinds)]
    return []


def branch_can_begin(rules, state, country, effects, context=None):
    for effect in effects:
        chooser = effect.get("chooser", country)
        if condition_met(rules, state, chooser, effect.get("when")):
            return can_begin(rules, state, chooser, effect, context)
    return True


def can_begin(rules, state, country, effect, context=None):
    if not condition_met(rules, state, country, effect.get("when")):
        return True  # The instruction itself is legal and will be skipped in order.
    context = context or {}
    event = context.get("event", {})
    kind = effect["type"]
    if kind == "eliminate-built-unit":
        return built_target(rules, state, context) is not None
    if kind == "eliminate-air-superiority-attacker":
        return superiority_attacker(rules, state, event) is not None
    if kind in ("disable-triggered-status", "discard-triggered-status"):
        source = state.tokens.get(event.get("source_token_id"))
        return source is not None and source.position_id == zone(source.owner_country, "STATUS")
    if kind == "prevent-removal":
        announced = state.announcements.get(event.get("announcement_id"))
        return bool(announced and not announced["cancelled"])
    if effect["type"] in ("build", "recruit", "choose-space", "battle", "choose-unit", "choose-one", "battle-series",
                          "stage-bolster", "play-from-zone", "recover-discard", "discard-response", "discard-status", "air-action"):
        return bool(available_options(rules, state, country, effect, context)) or effect.get("optional", False)
    if effect["type"] == "discard-hand" and effect.get("strict"):
        return len(available_options(rules, state, country, effect, context)) >= effect["count"]
    if effect["type"] == "choose-country":
        return bool(available_options(rules, state, country, effect, context)) or effect.get("skipIfNoTarget", False)
    if effect["type"] == "search-deck":
        return len(available_options(rules, state, country, effect, context)) >= effect["min"]
    return True


def built_target(rules, state, context):
    for change in context.get("event_outcome", {}).get("changes", []):
        token = state.tokens[change["token_id"]]
        if token.token_type == "UNIT" and change["to"] in rules.regions and token.position_id == change["to"]:
            return token.token_id
    return None


def superiority_attacker(rules, state, event):
    token = state.tokens.get(event.get("attacker_token"))
    if token and token.position_id in rules.regions and token.position_id == event.get("air_origin", token.position_id):
        return token
    return None


def execute_effect(engine, state, frame, op):
    country = frame.actor_country
    effect = event_effect(op["effect"], frame)
    rules = engine.rules
    kind = effect["type"]
    if effect.get("chooser", country) != country:
        from .engine import CardInterpreter
        child = deepcopy(effect)
        chooser = child.pop("chooser")
        state.stack.append(CardInterpreter.frame(state, chooser, None, [instruction(child)], frame.resolution_id,
                                                 context={**frame.context, "effect_source": frame.source_card_id,
                                                          "effect_actor": frame.context.get("effect_actor", country)}))
        return outcome("CHOOSER_REQUESTED", country=chooser), False
    effect.pop("chooser", None)
    if not condition_met(rules, state, country, effect.get("when")):
        return outcome("EFFECT_SKIPPED", condition=False), False
    if kind == "air-action":
        options = available_options(rules, state, country, effect)
        return engine._request(state, frame, "AIR_ACTION", options,
            {"type": "AIR_ACTION_SELECTED", "effect": effect}, op["operation_id"]), False
    if kind == "eliminate-air-superiority-attacker":
        event = frame.context.get("event", {})
        token = superiority_attacker(rules, state, event)
        if token is None:
            return outcome_failure("ELIMINATE", "Air attacker has left the recorded region"), False
        frame.remaining_operations.append({"type": "ELIMINATE", "target_token": token.token_id, "allow_friendly": True})
        return outcome("EFFECT_EXPANDED"), False
    if kind == "forbid-air-defense-this-turn":
        response = next((t for t in state.tokens.values() if t.token_type == "RULE" and
                         t.source_token_id == frame.source_card_id and
                         rules.abilities[t.definition_id].get("global_rule") == "forbid-air-defense"), None)
        if response is None:
            return outcome_failure("TEMPORARY_RULE", "Missing fixed response object"), False
        result = outcome("TEMPORARY_RULE")
        if response.position_id == zone(country, "OBJECT_POOL"):
            engine.executor.transfer(state, response.token_id, zone(country, "RESPONSE"), result)
        return result, True
    if kind == "disable-triggered-status":
        key = frame.context.get("event", {}).get("source_token_id")
        source = state.tokens.get(key)
        if source is None or source.position_id != zone(source.owner_country, "STATUS"):
            return outcome_failure("DISABLE_STATUS", "Status is no longer deployed"), False
        response = next((t for t in state.tokens.values() if t.token_type == "RULE" and
                         t.source_token_id == key and t.definition_id == "restore-" + source.definition_id), None)
        if response is None:
            return outcome_failure("DISABLE_STATUS", "Missing fixed restoration object"), False
        result = outcome("DISABLE_STATUS")
        engine.executor.transfer(state, source.token_id, zone(source.owner_country, "DISABLED_STATUS"), result)
        engine.executor.transfer(state, response.token_id, zone(source.owner_country, "RESPONSE"), result)
        return result, True
    if kind == "cancel-card-effect":
        event = frame.context.get("event", {})
        target = next((f for f in state.stack if f.resolution_id == event.get("resolution_id")), None)
        if target is None or event.get("declared_kind") != "bolster":
            return outcome_failure("CARD_CANCEL", "No declared bolster to cancel"), False
        target.context["effect_cancelled"] = True
        return outcome("CARD_CANCEL"), True
    if kind in ("protect-units-this-turn", "supply-units-this-turn"):
        mode = "protection" if kind == "protect-units-this-turn" else "supply"
        response = next((t for t in state.tokens.values() if t.token_type == "RULE"
                         and t.source_token_id == frame.source_card_id and
                         rules.abilities[t.definition_id].get("area_rule") == {"mode": mode, "filter": effect["filter"]}), None)
        if response is None:
            return outcome_failure("TEMPORARY_RULE", "Area rule is absent from fixed inventory"), False
        result = outcome("TEMPORARY_RULE")
        if response.position_id == zone(country, "OBJECT_POOL"):
            engine.executor.transfer(state, response.token_id, zone(country, "RESPONSE"), result)
        return result, True
    if kind == "reveal-discard-cards":
        revealed = {}
        for target in effect["countries"]:
            discard = state.positions[zone(target, "DISCARD")]
            if discard:
                values, state.rng = shuffle(discard, state.rng)
                revealed[target] = values[0]
        frame.context.update(revealed_discard=revealed, revealed_used=[])
        return outcome("REVEAL_DISCARD", revealed=revealed), True
    if kind == "return-revealed-discard":
        result = outcome("RETURN_REVEALED")
        for target, key in frame.context.get("revealed_discard", {}).items():
            if key not in frame.context.get("revealed_used", []) and state.tokens[key].position_id == zone(target, "DISCARD"):
                engine.executor.transfer(state, key, zone(target, "DECK"), result, index=0)
        return result, True
    if kind == "country-loss-choice":
        from .engine import CardInterpreter
        target = effect["target"]
        economic = frame.context.get("economic")
        if economic is not None and not op.get("economic_applied"):
            economic.update(target_adjustment=0, prevent_discard=False)
            frame.remaining_operations.extend([{**op, "economic_applied": True},
                                               {"type": "ECONOMIC_TARGET", "target_country": target}])
            return outcome("EFFECT_EXPANDED"), False
        count = effect["count"]
        if economic is not None:
            count = economic_count(rules, state, frame, target, count)
        steps = [{"type": "COUNTRY_LOSS_STEP", "effect": effect} for _ in range(count)]
        state.stack.append(CardInterpreter.frame(state, target, None, steps, frame.resolution_id,
            context={**frame.context, "failure_policy": "continue", "effect_actor": country, "effect_source": frame.source_card_id}))
        return outcome("LOSS_REQUESTED", country=target), False
    if kind in ("cancel-economic", "economic-target-adjust", "prevent-economic-discard", "economic-discard-bonus"):
        event = frame.context.get("event", {})
        economic_frame = next((f for f in state.stack if f.resolution_id == event.get("resolution_id")
                               and "economic" in f.context), None)
        if economic_frame is None:
            return outcome_failure("ECONOMIC_RESPONSE", "No matching economic operation"), False
        economic = economic_frame.context["economic"]
        if kind == "cancel-economic":
            economic["cancelled"] = True
        elif kind == "economic-discard-bonus":
            if "perAirNearTargetHome" in effect:
                economic.setdefault("air_bonuses", []).append({"owner": country, **effect["perAirNearTargetHome"]})
            economic["bonus"] += effect["amount"]
            if bonus := effect.get("bonusIf"):
                if filtered_units(rules, state, country, {"force": bonus["force"], "kind": bonus["kind"], "spaces": [bonus["space"]]}):
                    economic["bonus"] += bonus["amount"]
        elif kind == "economic-target-adjust":
            economic["target_adjustment"] += effect["amount"]
        else:
            economic["prevent_discard"] = True
        return outcome("ECONOMIC_RESPONSE"), True
    if kind == "request-hand-discard":
        from .engine import CardInterpreter
        target = force_id(effect["target"], country)
        if target == "battle-attacker":
            target = frame.context.get("event", {}).get("actor_country")
        if target not in TURN_ORDER:
            return outcome_failure("DISCARD_PAYMENT", "No paying country"), False
        pending = {"type": "REQUEST_PAYMENT", "effect": effect, "selected": [], "origin_country": country}
        state.stack.append(CardInterpreter.frame(state, target, None, [pending], frame.resolution_id,
                                                 context=frame.context))
        return outcome("PAYMENT_REQUESTED", paying_country=target), False
    if kind == "play-from-zone" and force_id(effect.get("target"), country) != country:
        from .engine import CardInterpreter
        target = effect["target"]
        child_effect = {**effect, "target": "self"}
        state.stack.append(CardInterpreter.frame(state, target, None, [instruction(child_effect)],
                                                 frame.resolution_id, context=frame.context))
        return outcome("EXTRA_PLAY_REQUESTED", playing_country=target), False
    if kind == "return-played-card":
        options = ["done"]
        if len(state.positions[zone(country, "HAND")]) >= effect["discardHandCount"]:
            options.append("return")
        return engine._request(state, frame, "RETURN_CARD", options,
                               {"type": "RETURN_CARD_SELECTED", "effect": effect},
                               op["operation_id"], optional=True), False
    if kind == "return-declared-army-card":
        source = frame.context.get("event", {}).get("source_token_id")
        original = next((f for f in state.stack if f.source_card_id == source and f.context.get("origin") is not None), None)
        if original is None:
            return outcome_failure("RETURN_CARD", "No pending played card"), False
        original.context["settlement_destination"] = "HAND"
        return outcome("RETURN_CARD"), True
    if kind == "shuffle-used-card":
        event = frame.context.get("event", {})
        token = state.tokens.get(event.get("source_token_id"))
        if token is None or token.position_id != zone(country, "DISCARD"):
            return outcome_failure("TRANSFER", "Used card is no longer in discard"), False
        frame.remaining_operations.extend(reversed([
            {"type": "TRANSFER", "token": token.token_id, "destination": zone(country, "DECK")},
            instruction({"type": "shuffle-deck"}),
        ]))
        return outcome("EFFECT_EXPANDED"), False
    if kind == "prevent-removal":
        event = frame.context.get("event", {})
        announcement = event.get("announcement_id")
        if announcement not in state.announcements:
            return outcome_failure("PREVENT_REMOVAL", "No live removal announcement"), False
        frame.remaining_operations.append({"type": "CANCEL", "announcement_id": announcement})
        return outcome("EFFECT_EXPANDED"), False
    if kind in ("protect-unit-this-turn", "supply-this-turn"):
        if kind == "protect-unit-this-turn":
            event = frame.context.get("event", {})
            if event.get("target_token"):
                targets = [state.tokens[event["target_token"]]]
            else:
                targets = [state.tokens[key] for key in state.positions.get(event.get("target"), [])
                           if state.tokens[key].token_type == "UNIT"]
            definition = "unit-protection"
        else:
            force = force_id(effect["force"], country)
            targets = board_units(rules, state, force)
            definition = "unit-supply"
        result = outcome("TEMPORARY_RULE")
        for token in targets:
            owner = rules.force_controllers[token.owner_country]
            response = next((t for t in state.tokens.values() if t.token_type == "RULE"
                             and t.definition_id == definition and t.source_token_id == token.token_id), None)
            if response is None:
                return outcome_failure("TEMPORARY_RULE", "Required response is absent from inventory"), False
            if response.position_id == zone(owner, "OBJECT_POOL"):
                engine.executor.transfer(state, response.token_id, zone(owner, "RESPONSE"), result)
        return result, True
    if kind == "eliminate-built-unit":
        target = built_target(rules, state, frame.context)
        if target is None:
            return outcome_failure("ELIMINATE", "No recorded built unit"), False
        frame.remaining_operations.append({"type": "ELIMINATE", "target_token": target, "allow_friendly": True})
        return outcome("EFFECT_EXPANDED"), False
    if kind == "discard-triggered-status":
        event = frame.context.get("event", {})
        target = state.tokens.get(event.get("source_token_id"))
        if target is None or target.position_id != zone(target.owner_country, "STATUS"):
            return outcome_failure("TRANSFER", "Triggered status is no longer deployed"), False
        frame.remaining_operations.append({"type": "TRANSFER", "token": target.token_id,
                                           "destination": zone(target.owner_country, "DISCARD")})
        return outcome("EFFECT_EXPANDED"), False
    if kind in ("points", "points-per-unit", "points-per-occupied-space"):
        side = effect["side"]
        recipient = country
        if side == "enemy" or (side in ("axis", "allies") and side != rules.team(country)):
            recipient = next(c for c in TURN_ORDER if rules.team(c) != rules.team(country))
        amount = effect.get("amount", 0)
        if kind == "points-per-unit":
            amount = len(filtered_units(rules, state, country, effect["filter"])) * effect["amountPer"]
        elif kind == "points-per-occupied-space":
            side = effect["occupiedBy"]
            team = rules.team(country) if side == "self" else (
                "allies" if rules.team(country) == "axis" else "axis") if side == "enemy" else side
            spaces = {t.position_id for t in board_units(rules, state)
                      if rules.team(t.owner_country) == team and t.position_id in effect["spaces"]
                      and rules.units[t.definition_id]["kind"] == "ARMY"}
            amount = len(spaces) * effect["amountPer"]
        frame.remaining_operations.append({"type": "GAIN_VP", "amount": amount, "score_country": recipient})
        return outcome("EFFECT_EXPANDED"), False
    if kind in ("discard-top", "discard-top-per-unit"):
        target = force_id(effect["target"], country)
        if target == "battle-attacker":
            target = frame.context["event"]["actor_country"]
        count = effect.get("count", 0)
        if kind == "discard-top-per-unit":
            count = effect["countPer"] * len(filtered_units(rules, state, country, effect["filter"]))
        economic = frame.context.get("economic")
        if economic is not None and rules.team(country) != rules.team(target) and not op.get("economic_applied"):
            frame.context["economic"]["target_adjustment"] = 0
            frame.context["economic"]["prevent_discard"] = False
            frame.remaining_operations.extend([
                {**op, "economic_applied": True},
                {"type": "ECONOMIC_TARGET", "target_country": target},
            ])
            return outcome("EFFECT_EXPANDED"), False
        if economic is not None and rules.team(country) != rules.team(target):
            count = economic_count(rules, state, frame, target, count)
        transfers = [{"type": "TRANSFER", "token": key, "expected_from": zone(target, "DECK"),
                      "destination": zone(target, "DISCARD")} for key in state.positions[zone(target, "DECK")][:count]]
        missing = count - len(transfers)
        if missing:
            transfers.append({"type": "GAIN_VP", "amount": rules.settings["deck_exhaustion_vp"] * missing,
                              "score_country": target})
        frame.remaining_operations.extend(reversed(transfers))
        return outcome("EFFECT_EXPANDED", count=len(transfers)), False
    if kind == "shuffle-deck":
        result = outcome("SHUFFLE_DECK")
        deck = zone(country, "DECK")
        sequence, state.rng = shuffle(state.positions[deck], state.rng)
        for key in sequence:
            engine.executor.transfer(state, key, deck, result)
        return result, True
    if kind == "redeploy-armies":
        frame.remaining_operations.append({"type": "REDEPLOY",
                                          **({"count": effect["count"]} if "count" in effect else {})})
        return outcome("EFFECT_EXPANDED"), False
    if kind == "order-deck-top":
        selected = op.get("selected", [])
        top = op.get("eligible", list(state.positions[zone(country, "DECK")][:effect["count"]]))
        options = [key for key in top if key not in selected]
        if not options:
            result = outcome("ORDER_DECK")
            for key in reversed(selected):
                engine.executor.transfer(state, key, zone(country, "DECK"), result, index=0)
            return result, True
        return engine._request(state, frame, "ORDER_DECK", options,
                               {"type": "ORDER_DECK_SELECTED", "effect": effect,
                                "selected": selected, "eligible": top}, op["operation_id"]), False
    if kind == "battle-series":
        selected = op.get("selected", [])
        options = [key for key in available_options(rules, state, country, effect) if key not in selected]
        if len(selected) >= effect["min"]:
            options.append("confirm")
        if len(selected) == effect["max"]:
            options = ["confirm"]
        return engine._request(state, frame, "BATTLE_ORDER", options,
            {"type": "BATTLE_ORDER_SELECTED", "effect": effect, "selected": selected},
            op["operation_id"], optional=effect["min"] == 0), False
    if kind == "random-discard-to-deck-top":
        target = force_id(effect["target"], country)
        discard, deck = zone(target, "DISCARD"), zone(target, "DECK")
        count = effect["count"]
        if effect["mode"] == "exact" and len(state.positions[discard]) < count:
            return outcome_failure("RECOVER_RANDOM", "Not enough discard cards"), False
        sequence, state.rng = shuffle(state.positions[discard], state.rng)
        result = outcome("RECOVER_RANDOM")
        for key in reversed(sequence[:count]):
            engine.executor.transfer(state, key, deck, result, index=0)
        return result, True
    if kind in ("build", "recruit"):
        frame.remaining_operations.append({**action_for(effect, country), "effect_constraints": effect})
        return outcome("EFFECT_EXPANDED"), False
    if kind == "search-deck" and "selected" not in op:
        frame.context["search_tokens"] = []
    options = available_options(rules, state, country, effect, frame.context)
    if kind in ("search-deck", "put-hand-on-deck-bottom") and len(options) < effect["min"] - len(op.get("selected", [])):
        return outcome_failure(kind.upper(), "Too few eligible cards for the required choice"), False
    if kind == "discard-hand" and effect.get("strict") and len(options) < effect["count"]:
        return outcome_failure("DISCARD_PAYMENT", "Not enough eligible cards for payment"), False
    remaining = op.get("remaining", effect.get("count", effect.get("max", 1)))
    selected = op.get("selected", [])
    options = [key for key in options if key not in selected]
    if remaining == 0:
        return outcome("EFFECT_COMPLETE"), False
    minimum = effect.get("min", 0 if effect.get("optional") else 1)
    if effect.get("optional") or (kind in ("search-deck", "put-hand-on-deck-bottom") and len(selected) >= minimum):
        options = [*options, "done"]
    if not options:
        if kind == "choose-country" and effect.get("skipIfNoTarget"):
            return outcome("EFFECT_SKIPPED", no_target=True), False
        if kind == "discard-hand" and not effect.get("strict"):
            frame.remaining_operations.append(instruction({"type": "discard-top", "target": "self", "count": remaining}))
            return outcome("EFFECT_EXPANDED"), False
        return outcome_failure("CARD_EFFECT", "No eligible effect targets"), False
    continuation = {"type": "CARD_EFFECT_SELECTED", "effect": effect,
                    "remaining": remaining, "selected": selected}
    return engine._request(state, frame, "CARD_EFFECT", options, continuation, op["operation_id"],
                           optional="done" in options), False


def economic_count(rules, state, frame, target, base):
    """Reaction bonuses apply once to each target of this resolving card."""
    from .rules import RuleModifierResolver
    economic, country = frame.context["economic"], frame.actor_country
    applied = economic.setdefault("applied_targets", [])
    if target in applied:
        return base
    applied.append(target)
    count = base + economic["bonus"] + economic["target_adjustment"]
    for bonus in economic.get("air_bonuses", []):
        force = force_id(bonus["force"], bonus["owner"])
        homes = home_regions(rules, state, target)
        count += bonus["amountPer"] * sum(any(MapGraph(rules).distances(home, state, force).get(t.position_id, 1000)
            <= bonus["maxDistance"] for home in homes) for t in filtered_units(rules, state, country, {"force": force, "kind": "air"}))
    for modifier in RuleModifierResolver(rules).resolve(state, country):
        if modifier["kind"] == "economic_adjustment" and modifier["country"] == (
                country if modifier["role"] == "actor" else target):
            count += modifier["amount"]
    return 0 if economic["prevent_discard"] else max(0, count)


def request_payment(engine, state, frame, op):
    effect, selected = op["effect"], op["selected"]
    hand = state.positions[zone(frame.actor_country, "HAND")]
    options = [key for key in hand if key not in selected] if len(selected) < effect["max"] else []
    if len(selected) >= effect["min"]:
        options.append("confirm")
    if effect.get("onDecline") or len(hand) < effect["min"]:
        options.append("decline")
    if not options:
        return outcome_failure("DISCARD_PAYMENT", "No payment available"), False
    return engine._request(state, frame, "PAYMENT", options, {**op, "type": "PAYMENT_SELECTED"},
                           op["operation_id"], optional="decline" in options), False


def select_payment(engine, state, frame, op):
    effect, selected, choice = op["effect"], op["selected"], op["choice"]
    hand = state.positions[zone(frame.actor_country, "HAND")]
    if choice not in ("confirm", "decline"):
        if choice not in hand or choice in selected or len(selected) >= effect["max"]:
            return outcome_failure("DISCARD_PAYMENT", "Payment card is unavailable"), False
        frame.remaining_operations.append({**op, "type": "REQUEST_PAYMENT", "selected": [*selected, choice]})
        return outcome("PAYMENT_SELECTED"), False
    if choice == "decline":
        if effect.get("onDecline") == "prevent-battle-removal":
            frame.remaining_operations.append(instruction({"type": "prevent-removal"}))
        return outcome("PAYMENT_DECLINED"), False
    if not effect["min"] <= len(selected) <= effect["max"] or any(key not in hand for key in selected):
        return outcome_failure("DISCARD_PAYMENT", "Full payment is unavailable"), False
    result = outcome("DISCARD_PAYMENT", count=len(selected))
    for key in selected:
        engine.executor.transfer(state, key, zone(frame.actor_country, "DISCARD"), result)
    if recovery := effect.get("onPaidRecovery"):
        frame.remaining_operations.append(instruction({"type": "random-discard-to-deck-top",
            "target": force_id(recovery["target"], op["origin_country"]), "count": len(selected), "mode": recovery["mode"]}))
    return result, True


def execute_selection(engine, state, frame, op):
    effect, choice = op["effect"], op["choice"]
    country, kind = frame.actor_country, effect["type"]
    if choice == "done":
        return outcome("EFFECT_COMPLETE"), False
    if choice not in available_options(engine.rules, state, country, effect, frame.context) or choice in op["selected"]:
        return outcome_failure("CARD_EFFECT", "Effect target no longer eligible"), False
    operations = []
    if kind == "choose-one":
        operations = [instruction(child) for child in effect["options"][int(choice)]["effects"]]
    elif kind == "choose-country":
        operations = [instruction({"type": "discard-top", "target": choice, "count": effect["count"]})]
    elif kind == "stage-bolster":
        operations = [{"type": "TRANSFER", "token": choice, "destination": zone(country, "RESPONSE")}]
    elif kind == "play-from-zone":
        operations = [{"type": "PLAY_EXTRA_CARD", "token": choice, "expected_from": state.tokens[choice].position_id}]
        if effect.get("drawAfter"):
            operations.append({"type": "DRAW", "count": effect["drawAfter"]})
        if effect.get("shuffleAfter"):
            operations.append(instruction({"type": "shuffle-deck"}))
    elif kind in ("choose-space", "battle"):
        if kind == "battle" and effect["kind"] == "either":
            unit_kind, choice = choice.split("|", 1)
            effect = {**effect, "kind": unit_kind}
        operations = [{**action_for(effect, country), "target": choice, "effect_constraints": effect}]
    elif kind == "choose-unit":
        operations = [{"type": "ELIMINATE", "target_token": choice, "allow_friendly": True}]
    else:
        target = force_id(effect.get("target"), country)
        destination = {
            "discard-hand": "DISCARD", "discard-status": "DISCARD", "discard-response": "DISCARD",
            "put-hand-on-deck-bottom": "DECK", "search-deck": "HAND",
            "recover-discard": effect.get("destination", "hand").upper(),
        }[kind]
        strict_cost = kind == "discard-hand" and effect.get("strict")
        selected = [*op["selected"], choice]
        if kind == "search-deck":
            frame.context["search_tokens"] = selected
        if not strict_cost or op["remaining"] == 1:
            pay = selected if strict_cost else [choice]
            operations = [{"type": "TRANSFER", "token": token, "expected_from": state.tokens[token].position_id,
                           "destination": zone(target, destination)} for token in pay]
        if op["remaining"] > 1 and kind in ("discard-hand", "put-hand-on-deck-bottom", "search-deck"):
            operations.append({**instruction(effect), "remaining": op["remaining"] - 1, "selected": [*op["selected"], choice]})
        if strict_cost and op["remaining"] == 1:
            result = outcome("DISCARD_PAYMENT")
            for token in selected:
                engine.executor.transfer(state, token, zone(target, "DISCARD"), result)
            return result, True
    frame.remaining_operations.extend(reversed(operations))
    return outcome("EFFECT_SELECTED"), False
