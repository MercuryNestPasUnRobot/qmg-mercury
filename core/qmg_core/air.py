"""Air actions and battle continuations, expressed as ordinary stack instructions."""
import json

from .model import zone
from .operations import outcome, outcome_failure
from .rules import MapGraph, SupplyResolver, board_units, unit_rule_matches


def air_units(rules, state, force=None):
    return [t for t in board_units(rules, state, force) if rules.units[t.definition_id]["kind"] == "AIR"]


def air_options(rules, state, actor, effect):
    force = actor if effect.get("force", "self") == "self" else effect["force"]
    if rules.team(force) != rules.team(actor):
        return []
    supplied = SupplyResolver(rules).supplied(state, force)
    units = board_units(rules, state, force)
    air = air_units(rules, state, force)
    occupied = {t.position_id for t in air}
    bases = {t.position_id for t in units if t.token_id in supplied and
             rules.units[t.definition_id]["kind"] != "AIR" and
             (effect.get("baseKind") != "navy" or rules.units[t.definition_id]["kind"] == "NAVY")}
    if near := effect.get("near"):
        distances = MapGraph(rules).distances(near["space"], state, force)
        bases = {p for p in bases if distances.get(p, 1000) <= near["maxDistance"]}
    options = []
    if "deploy" in effect["actions"] and state.positions.get(zone(force, "RESERVE_AIR")):
        options.extend(["deploy", None, p, None] for p in sorted(bases - occupied))
    for token in air:
        if token.token_id not in supplied:
            continue
        if "marshal" in effect["actions"]:
            options.extend(["marshal", token.token_id, p, None] for p in sorted(bases - (occupied - {token.position_id})))
        if "superiority" in effect["actions"]:
            for p in MapGraph(rules).neighbors(token.position_id, state, force):
                victims = [t.token_id for t in air_units(rules, state) if t.position_id == p
                           and rules.team(t.owner_country) != rules.team(force)]
                options.extend(["superiority", token.token_id, p, victim] for victim in victims or [None])
    return [json.dumps(option, separators=(",", ":")) for option in options]


def opportunity(state, definition):
    return next((t for t in state.tokens.values() if t.token_type == "RULE" and t.definition_id == definition
                 and t.position_id == zone(t.owner_country, "ABILITY_READY")), None)


def global_rule(rules, state, kind):
    return any(t.token_type == "RULE" and t.position_id in ("RESOLUTION", zone(t.owner_country, "RESPONSE"))
               and rules.abilities[t.definition_id].get("global_rule") == kind for t in state.tokens.values())


def victim_options(rules, state, op):
    kind = "ARMY" if op["type"] == "LAND_BATTLE" else "NAVY"
    return [t.token_id for t in board_units(rules, state) if t.position_id == op["target"]
            and rules.units[t.definition_id]["kind"] == kind
            and rules.team(t.owner_country) != rules.team(op["actor_country"])]


def rebase_options(rules, state, token):
    supplied = SupplyResolver(rules).supplied(state, token.owner_country)
    neighbors = MapGraph(rules).neighbors(token.position_id, state, token.owner_country)
    occupied = {t.position_id for t in air_units(rules, state, token.owner_country) if t.token_id != token.token_id}
    return sorted({t.position_id for t in board_units(rules, state, token.owner_country)
                   if rules.units[t.definition_id]["kind"] != "AIR" and t.token_id in supplied
                   and t.position_id in neighbors and t.position_id not in occupied})


def execute_air(engine, state, frame, op):
    from .engine import CardInterpreter
    rules, country, kind = engine.rules, frame.actor_country, op["type"]
    if kind == "AIR_ACTION_SELECTED":
        if op["choice"] not in air_options(rules, state, country, op["effect"]):
            return outcome_failure(kind, "Air action is no longer legal"), False
        action, attacker, target, victim = json.loads(op["choice"])
        force = country if op["effect"].get("force", "self") == "self" else op["effect"]["force"]
        result = outcome("AIR_MOVEMENT")
        if action == "deploy":
            engine.executor.transfer(state, state.positions[zone(force, "RESERVE_AIR")][0], target, result)
        elif action == "marshal":
            engine.executor.transfer(state, attacker, target, result)
        event = {"type": "AIR_ACTION_FINISHED", "air_action": action, "force": force,
                 "target": target, "attacker_token": attacker, "victim": victim,
                 "air_origin": state.tokens[attacker].position_id if attacker else None}
        frame.remaining_operations.append(event)
        if action == "superiority" and victim:
            frame.remaining_operations.append({"type": "ELIMINATE", "target_token": victim,
                                               "reason": "air-superiority", "expected_from": target})
        return result, False
    if kind == "AIR_ACTION_FINISHED":
        victim = state.tokens.get(op.get("victim"))
        removed = victim and victim.position_id == zone(victim.owner_country, "RESERVE_AIR")
        op["removed_force"] = victim.owner_country if removed else None
        return outcome(kind, removed_force=op["removed_force"]), True
    if kind == "BATTLE_VICTIM_SELECTED":
        battle = op["battle"]
        if op["choice"] not in victim_options(rules, state, battle):
            return outcome_failure(kind, "Battle victim has changed"), False
        frame.remaining_operations.append({**battle, "target_token": op["choice"]})
        return outcome(kind), False
    if kind == "BATTLE_AIR_WINDOW":
        battle = op["battle"]
        victim = state.tokens.get(battle.get("target_token"))
        defending = next((t for t in air_units(rules, state, victim.owner_country)
                          if t.position_id == battle["target"]), None) if victim else None
        if defending and not global_rule(rules, state, "forbid-air-defense"):
            child = CardInterpreter.frame(state, rules.force_controllers[victim.owner_country], None,
                [{"type": "AIR_DEFENSE_REQUEST", "battle": battle, "air": defending.token_id}], frame.resolution_id)
            state.stack.append(child)
        else:
            frame.remaining_operations.append(battle)
        return outcome(kind), False
    if kind == "AIR_DEFENSE_REQUEST":
        return engine._request(state, frame, "AIR_DEFENSE", ["unit", "air"],
            {**op, "type": "AIR_DEFENSE_SELECTED"}, op["operation_id"]), False
    if kind == "AIR_DEFENSE_SELECTED":
        if op["choice"] == "unit":
            frame.remaining_operations.append({**op["battle"], "actor_override": op["battle"]["actor_country"]})
        else:
            frame.remaining_operations.extend([
                {**op, "type": "AIR_DEFENSE_PAID"},
                {"type": "ELIMINATE", "target_token": op["air"], "allow_friendly": True, "reason": "air-defense-cost"},
            ])
        return outcome(kind), False
    if kind == "AIR_DEFENSE_PAID":
        token = state.tokens[op["air"]]
        battle = op["battle"]
        if token.position_id in rules.regions:
            frame.remaining_operations.append({**battle, "actor_override": battle["actor_country"]})
        else:
            child = CardInterpreter.frame(state, battle["actor_country"], None,
                [{"type": "AIR_COUNTER_REQUEST", "battle": battle}], frame.resolution_id)
            state.stack.append(child)
        return outcome(kind), False
    if kind == "AIR_COUNTER_REQUEST":
        battle = op["battle"]
        force = battle.get("force", country)
        supplied = SupplyResolver(rules).supplied(state, force)
        neighbors = MapGraph(rules).neighbors(battle["target"], state, force)
        options = ["pass"] + [t.token_id for t in air_units(rules, state, force)
                               if t.token_id in supplied and t.position_id in neighbors]
        return engine._request(state, frame, "AIR_COUNTER", options,
            {**op, "type": "AIR_COUNTER_SELECTED"}, op["operation_id"], True), False
    if kind == "AIR_COUNTER_SELECTED":
        if op["choice"] != "pass":
            frame.remaining_operations.extend([
                {**op, "type": "AIR_COUNTER_PAID", "air": op["choice"]},
                {"type": "ELIMINATE", "target_token": op["choice"], "allow_friendly": True, "reason": "air-attack-cost"},
            ])
        return outcome(kind), False
    if kind == "AIR_COUNTER_PAID":
        if state.tokens[op["air"]].position_id not in rules.regions:
            frame.remaining_operations.append(op["battle"])
        return outcome(kind), False
    if kind == "CHECK_AIR_BASE":
        token = state.tokens[op["air"]]
        if token.position_id not in rules.regions or any(t.position_id == token.position_id and
                rules.units[t.definition_id]["kind"] != "AIR" for t in board_units(rules, state, token.owner_country)):
            return outcome(kind), False
        options = rebase_options(rules, state, token)
        if options:
            return engine._request(state, frame, "AIR_REBASE", options,
                {"type": "AIR_REBASE_SELECTED", "air": token.token_id}, op["operation_id"]), False
        if not unit_rule_matches(rules, state, token, "protection"):
            frame.remaining_operations.append({"type": "ELIMINATE", "target_token": token.token_id,
                                               "allow_friendly": True, "reason": "base-loss"})
        return outcome(kind), False
    if kind == "AIR_REBASE_SELECTED":
        token = state.tokens[op["air"]]
        if op["choice"] not in rebase_options(rules, state, token):
            return outcome_failure(kind, "Air base is no longer legal"), False
        result = outcome("AIR_REBASE")
        engine.executor.transfer(state, token.token_id, op["choice"], result)
        return result, True
    raise ValueError("Unknown air continuation")
