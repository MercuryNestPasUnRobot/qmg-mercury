"""Player operations built from existing choices, transfers and settlement."""
from .air import opportunity
from .model import zone
from .operations import outcome, outcome_failure
from .rules import RuleModifierResolver


BASIC_KINDS = ("build-army", "build-navy", "land-battle", "sea-battle")


def reallocation_terms(rules, state, country):
    modifiers = RuleModifierResolver(rules).resolve(state, country)
    costs = [m["count"] for m in modifiers if m["kind"] == "reallocate_cost" and m["country"] == country]
    return min(costs) if costs else 3, any(m["kind"] == "reallocate_discard" and m["country"] == country for m in modifiers)


def reallocation_available(rules, state, country):
    cost, discard = reallocation_terms(rules, state, country)
    return bool(state.turn.plays_remaining == rules.settings["plays_per_turn"] and
                opportunity(state, "reallocation-opportunity-" + country) and
                len(state.positions[zone(country, "HAND")]) >= cost and any(
                    is_basic(rules, state, key)
                    for kind in (("DECK", "DISCARD", "HAND") if discard else ("DECK",))
                    for key in state.positions[zone(country, kind)]))


def is_basic(rules, state, key):
    return rules.cards[state.tokens[key].definition_id].get("legacy_kind") in BASIC_KINDS


def reallocation_payment_options(rules, state, country, selected):
    cost, discard = reallocation_terms(rules, state, country)
    hand = state.positions[zone(country, "HAND")]
    remaining = cost - len(selected)
    if remaining < 0 or len(hand) < cost:
        return ["cancel"]
    funded = any(is_basic(rules, state, key) for key in state.positions[zone(country, "DECK")])
    discard_possible = discard and any(is_basic(rules, state, key)
        for key in (*state.positions[zone(country, "DISCARD")], *hand))
    if not funded and not discard_possible:
        return ["cancel"]
    funded = funded or (discard and any(is_basic(rules, state, key)
        for key in (*state.positions[zone(country, "DISCARD")], *selected)))
    if remaining == 0:
        return ["confirm", "cancel"] if funded else ["cancel"]
    return [*(key for key in hand if key not in selected and
              (funded or remaining > 1 or is_basic(rules, state, key))), "cancel"]


def begin_action(engine, state, command):
    from .engine import CardInterpreter
    country = command["actor"]
    result = outcome(command["type"])
    if command["type"] == "USE_AIR":
        force = command["force"]
        marker = opportunity(state, "air-opportunity-" + force)
        engine.executor.transfer(state, marker.token_id, zone(country, "RESPONSE"), result)
        engine.executor.transfer(state, command["card"], "RESOLUTION", result)
        state.stack.append(CardInterpreter.frame(state, country, command["card"], [
            {"type": "CARD_EFFECT", "effect": {"type": "air-action", "force": force,
                                               "actions": ["deploy", "marshal", "superiority"]}}],
            context={"origin": zone(country, "HAND"), "failure_policy": "stop"}))
    else:
        state.stack.append(CardInterpreter.frame(state, country, None, [{"type": "REALLOCATION_REQUEST", "selected": []}],
                                                 context={"reallocation": True}))
    return result


def execute_action(engine, state, frame, op):
    from .content_effects import instruction
    rules, country = engine.rules, frame.actor_country
    cost, allow_discard = reallocation_terms(rules, state, country)
    selected = op.get("selected", [])
    if op["type"] == "REALLOCATION_REQUEST":
        options = reallocation_payment_options(rules, state, country, selected)
        return engine._request(state, frame, "REALLOCATION_PAYMENT", options,
            {"type": "REALLOCATION_SELECTED", "selected": selected}, op["operation_id"], True), False
    choice = op["choice"]
    if choice == "cancel":
        return outcome("REALLOCATION_DECLINED"), False
    if choice != "confirm":
        frame.remaining_operations.append({"type": "REALLOCATION_REQUEST", "selected": [*selected, choice]})
        return outcome("REALLOCATION_PAYMENT_SELECTED"), False
    hand = state.positions[zone(country, "HAND")]
    if (len(selected) != cost or len(set(selected)) != cost or any(key not in hand for key in selected) or
            "confirm" not in reallocation_payment_options(rules, state, country, selected)):
        return outcome_failure("REALLOCATION", "Complete payment is no longer available"), False
    marker = opportunity(state, "reallocation-opportunity-" + country)
    if marker is None:
        return outcome_failure("REALLOCATION", "Opportunity was already used"), False
    result = outcome("REALLOCATION_PAID")
    for key in selected:
        engine.executor.transfer(state, key, zone(country, "DISCARD"), result)
    engine.executor.transfer(state, marker.token_id, zone(country, "RESPONSE"), result)
    branches = [{"label": "deck", "effects": [
        {"type": "search-deck", "min": 1, "max": 1, "kinds": list(BASIC_KINDS)}, {"type": "shuffle-deck"}]}]
    if allow_discard:
        branches.append({"label": "discard", "effects": [{"type": "recover-discard", "target": "self",
                                                            "kinds": list(BASIC_KINDS), "count": 1}]})
    frame.remaining_operations.append(instruction({"type": "choose-one", "options": branches}))
    return result, True
