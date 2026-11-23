"""Check each source declaration against the installed catalog and exercise six seats."""
from argparse import ArgumentParser
from collections import Counter
import json
from copy import deepcopy
from pathlib import Path

from .api import CoreAPI
from .content_verify import check_catalog, settle_choices
from .definitions import RuleSet
from .engine import CommandProcessor
from .migration import passive_modifier
from .model import CoreError, TURN_ORDER, zone
from .operations import OperationExecutor, outcome
from .rules import initialize, validate_state


NATIVE_ACTIONS = {"build-army": "BUILD_ARMY", "build-navy": "BUILD_NAVY",
                  "land-battle": "LAND_BATTLE", "sea-battle": "SEA_BATTLE"}


def effect_paths(effects, prefix):
    for index, effect in enumerate(effects):
        path = f"{prefix}[{index}]"
        yield {"path": path, "type": effect["type"], "conditional": "when" in effect}
        for option_index, option in enumerate(effect.get("options", [])):
            yield from effect_paths(option["effects"], f"{path}.options[{option_index}].effects")


def compare_card(source, rules):
    key = source["id"]
    card = rules.cards[key]
    kind = source["kind"]
    errors = []

    def check(expected, actual, path):
        if expected != actual:
            errors.append(path)

    # Check the actual execution fields. The retained source metadata can agree
    # perfectly with the export even when the executor gets a different program.
    check(source, card.get("source_card"), "source_card")
    check(source["country"], card["country"], "country")
    check(kind, card.get("legacy_kind"), "legacy_kind")
    check("STATUS" if kind == "status" else "RESPONSE" if kind == "response" else "EVENT", card["type"], "type")
    check(source["name"], card.get("title"), "title")
    check(source["text"], card.get("text"), "text")
    check(source.get("condition"), card.get("content_condition"), "content_condition")
    check([NATIVE_ACTIONS[mode] for mode in source.get("playAs", [])], card.get("play_as", []), "play_as")
    check("executable", card.get("implementation", {}).get("status"), "implementation")

    effects = source.get("effects", [])
    if kind in NATIVE_ACTIONS:
        program = [{"type": "CHOOSE", "operation": {"type": NATIVE_ACTIONS[kind], "target": "$choice"}}]
        check(program, card["effects"], "native action")
    elif trigger := source.get("trigger"):
        ability_id = "use-" + key
        ability = rules.abilities.get(ability_id, {})
        check(True, ability_id in card["abilities"], "listener binding")
        check(trigger, ability.get("trigger", {}).get("card_trigger"), "listener trigger")
        check_effects(effects, ability.get("effects", []), "listener effects", check)
        if kind == "bolster":
            check({"HAND", "RESPONSE"}, set(ability.get("listener_scope", [])), "bolster zones")
        if trigger.get("oncePerTurn"):
            check("TURN_END", ability.get("reset_policy"), "listener renewal")
        check([], card["effects"], "on placement effects")
    else:
        check_effects(effects, card["effects"], "effects", check)

    active = source.get("activeEffects", [])
    if active:
        ability_id = card.get("active_ability")
        ability = rules.abilities.get(ability_id, {})
        check(True, ability_id in card["abilities"], "active binding")
        check({"operation_type": "USE_STATUS"}, ability.get("trigger"), "active trigger")
        check_effects(active, ability.get("effects", []), "activeEffects", check)
    else:
        check(None, card.get("active_ability"), "active binding")

    expected_modifiers = []
    for index, passive in enumerate(source.get("passives", [])):
        converted = passive_modifier(passive)
        if converted:
            expected_modifiers.append(converted)
            continue
        ability_id = f"passive-{key}-{index}"
        ability = rules.abilities.get(ability_id, {})
        check(True, ability_id in card["abilities"], f"passives[{index}] listener binding")
        if passive["type"] == "discard-stage-score":
            target = {"type": "discard-stage-card", "kind": passive["kind"], "country": passive["country"]}
            program = [{"type": "points", "side": "self", "amount": passive["points"]}]
        elif passive["type"] == "battle-attacker-discard":
            target = {"type": "before-battle-removal", "kind": passive["kind"], "victims": passive["victims"]}
            if "spaces" in passive:
                target["spaces"] = passive["spaces"]
            program = [{"type": "discard-top", "target": "battle-attacker", "count": passive["count"]}]
            if passive.get("oncePerTurn"):
                check("TURN_END", ability.get("reset_policy"), f"passives[{index}] renewal")
        else:
            errors.append(f"passives[{index}] translation")
            continue
        check(target, ability.get("trigger", {}).get("card_trigger"), f"passives[{index}] trigger")
        check_effects(program, ability.get("effects", []), f"passives[{index}] effects", check)
    check(expected_modifiers, card.get("modifiers", []), "passive modifiers")

    if reaction := source.get("economicReaction"):
        ability_id = "economic-" + key
        ability = rules.abilities.get(ability_id, {})
        check(True, ability_id in card["abilities"], "economic reaction binding")
        check({"type": "economic-play", "actor": "any", "keyword": reaction["keyword"]},
              ability.get("trigger", {}).get("card_trigger"), "economic reaction trigger")
        program = {"type": "economic-discard-bonus", "amount": reaction["amount"]}
        if "bonusIf" in reaction:
            program["bonusIf"] = reaction["bonusIf"]
        check_effects([program], ability.get("effects", []), "economic reaction effects", check)

    return {"id": key, "seat": card["country"], "kind": kind, "condition": bool(source.get("condition")),
            "listener": bool(source.get("trigger")), "passives": len(source.get("passives", [])),
            "alternate_modes": card.get("play_as", []), "errors": errors,
            "steps": [*effect_paths(effects, "effects"), *effect_paths(active, "activeEffects")]}


def check_effects(expected, program, path, check):
    check(len(expected), len(program), f"{path} size")
    for index, (effect, instruction) in enumerate(zip(expected, program)):
        check({"type": "CARD_EFFECT", "effect": effect}, instruction, f"{path}[{index}]")


def compare_catalog(export, rules):
    pack = export["pack"]
    if {source["id"] for source in pack["cards"]} != set(rules.cards):
        raise CoreError("The set of compiled definitions differs from the source")
    rows = [compare_card(source, rules) for source in pack["cards"]]
    decks = {}
    for seat in TURN_ORDER:
        actual = [t.definition_id for t in rules.inventory.values()
                  if t.token_type == "CARD" and t.position_id == zone(seat, "DECK")]
        decks[seat] = {"source": len(pack["decks"][seat]), "catalog": len(actual),
                       "exact_sequence": actual == pack["decks"][seat]}
    return rows, decks


def check_seats(rules, seed):
    p = CommandProcessor.create(rules, seed=seed, game_id="content-seat-audit")
    rows = {}
    setup_choices = settle_choices(p)
    for seat in TURN_ORDER:
        if TURN_ORDER[p.state.turn.country_index] != seat or p.state.turn.phase != "Play":
            raise CoreError(f"Turn order did not enter the play phase for {seat}")
        client = CoreAPI(p, (seat,))
        commands = [c for c in client.legal_commands() if c["type"] == "PLAY_CARD"]
        if not commands:
            raise CoreError(f"No normal play from the audited opening hand for {seat}")
        command = commands[0]
        other = next(c for c in TURN_ORDER if c != seat)
        start = p.state
        if command in CoreAPI(p, (other,)).legal_commands() or command["card"] in CoreAPI(p, (other,)).observe()["tokens"]:
            raise CoreError(f"Foreign seat can access the play for {seat}")
        token = start.tokens[command["card"]]
        number = len(p.records)
        client.submit(command)
        settle_choices(p, take_optional=True)
        if p.state.turn.plays_remaining != start.turn.plays_remaining - 1:
            raise CoreError(f"Normal play did not use one action for {seat}")
        records = p.records[number:]
        if not any(r["source"] == token.token_id and r["actor"] == seat
                   and r["operation_type"] in ("PLAY_CARD", "SET_RESPONSE") for r in records):
            raise CoreError(f"Normal play was not recorded for {seat}")
        rows[seat] = {
            "legal_starting_cards": len({c["card"] for c in commands}),
            "played": token.definition_id,
            "destination": p.state.tokens[token.token_id].position_id,
            "root_play_recorded": any(r["source"] == token.token_id and r["actor"] == seat
                                      and r["operation_type"] in ("PLAY_CARD", "SET_RESPONSE") for r in records),
            "unsuccessful_steps": [r["operation_type"] for r in records if not r["outcome"]["success"]],
            "normal_action_used": True,
        }
        for _ in rules.phases:
            command = next(c for c in client.legal_commands() if c["type"] == "END_PHASE")
            client.submit(command)
            settle_choices(p)
        if p.state.stack or p.state.announcements or p.state.positions["RESOLUTION"]:
            raise CoreError(f"Seat transition left unfinished resolution for {seat}")
    return {"seed": seed, "setup_choices": setup_choices, "completed_turns": p.state.turn.turn_id - 1, "seats": rows}


def check_phase_listeners(rules, seed):
    phases = {"turn-start": "Play", "play-phase-start": "Play", "air-phase-start": "Air",
              "victory-start": "Victory", "during-victory": "Victory", "discard-start": "Discard"}
    data = deepcopy(rules.data)
    data["settings"].update(initial_hand=0, setup_discard=0)
    scenario_rules = RuleSet(data)
    executor = OperationExecutor(scenario_rules)
    results = {}
    for card in scenario_rules.cards.values():
        listener = scenario_rules.abilities.get("use-" + card["id"], {})
        trigger = listener.get("trigger", {}).get("card_trigger", {}).get("type")
        if trigger not in phases:
            continue
        state = initialize(scenario_rules, seed)
        owner = card["country"]
        source = next(t for t in state.tokens.values() if t.token_type == "CARD" and t.definition_id == card["id"])
        fixture = outcome("TEST_SETUP")
        for key in state.positions[zone(owner, "DECK")][:8]:
            executor.transfer(state, key, zone(owner, "HAND"), fixture)
        executor.transfer(state, source.token_id, zone(owner, {"status": "STATUS", "response": "RESPONSE"}.get(
            card["legacy_kind"], "HAND")), fixture)
        slot = next(t for t in state.tokens.values() if t.source_token_id == source.token_id
                    and t.definition_id == listener["id"])
        phase = phases[trigger]
        index = scenario_rules.phases.index(phase)
        state.turn.phase = scenario_rules.phases[index - 1]
        if state.turn.phase == "Play":
            state.turn.plays_remaining = 0
        state.turn.country_index = (TURN_ORDER.index(owner) - int(index == 0)) % len(TURN_ORDER)
        validate_state(scenario_rules, state)
        p = CommandProcessor(scenario_rules, state)
        actor = TURN_ORDER[state.turn.country_index]
        client = CoreAPI(p, (actor,))
        client.submit(next(c for c in client.legal_commands() if c["type"] == "END_PHASE"))
        settle_choices(p, take_optional=True, preferred_listener=slot.token_id)
        target_records = [r for r in p.records if r["operation"].get("object_token_id") == slot.token_id]
        results[card["id"]] = {
            "phase": phase, "trigger": trigger, "activated": any(
                r["operation_type"] == "ABILITY_ACTIVATED" for r in target_records),
            "settled": any(r["operation_type"] == "OBJECT_SETTLED" for r in target_records),
            "effect_trace": [
                {"instruction": r["operation"]["effect"]["type"], "outcome": r["operation_type"]}
                for r in target_records if r["operation"]["type"] == "CARD_EFFECT"
            ],
        }
    return results


def check_active_statuses(rules, seed):
    data = deepcopy(rules.data)
    data["settings"].update(initial_hand=0, setup_discard=0)
    scenario_rules = RuleSet(data)
    executor = OperationExecutor(scenario_rules)
    results = {}
    for card in scenario_rules.cards.values():
        if not card.get("active_ability"):
            continue
        owner = card["country"]
        state = initialize(scenario_rules, seed)
        state.turn.country_index = TURN_ORDER.index(owner)
        source = next(t for t in state.tokens.values() if t.token_type == "CARD" and t.definition_id == card["id"])
        executor.transfer(state, source.token_id, zone(owner, "STATUS"), outcome("TEST_SETUP"))
        p = CommandProcessor(scenario_rules, state)
        command = next((c for c in p.legal_commands((owner,)) if c["type"] == "USE_STATUS" and c["card"] == source.token_id), None)
        if command is None:
            results[card["id"]] = {"available": False}
            continue
        p.submit(command, (owner,))
        settle_choices(p, take_optional=True)
        results[card["id"]] = {"available": True, "effects": [
            {"instruction": r["operation"]["effect"]["type"], "outcome": r["operation_type"]}
            for r in p.records if r["source"] == source.token_id and r["operation"]["type"] == "CARD_EFFECT"
        ], "unsuccessful_steps": [r["operation_type"] for r in p.records if not r["outcome"]["success"]]}
    return results


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=31)
    parser.add_argument("--runtime", action="store_true", help="Trace legal opening commands for every definition")
    args = parser.parse_args()
    export = json.loads(args.source.read_text(encoding="utf8"))
    rules = RuleSet.load(args.catalog)
    rows, decks = compare_catalog(export, rules)
    divergent = sum(bool(row["errors"]) for row in rows)
    report = {"rules_digest": rules.fingerprint, "definitions": len(rows), "mismatched_definitions": divergent,
              "effect_nodes": sum(len(row["steps"]) for row in rows), "card_conditions": sum(row["condition"] for row in rows),
              "conditional_effect_nodes": sum(step["conditional"] for row in rows for step in row["steps"]),
              "listener_definitions": sum(row["listener"] for row in rows), "decks": decks, "cards": rows}
    if args.runtime and divergent == 0 and all(row["exact_sequence"] for row in decks.values()):
        report["seats"] = check_seats(rules, args.seed)
        opening = check_catalog(rules, trace=True)
        report["opening_coverage"] = opening["results"]
        runs = {entry["id"]: entry for entry in opening["cards"]}
        for row in rows:
            row["opening"] = runs[row["id"]]
        report["opening_commands"] = sum(len(run.get("cases", [])) for run in runs.values())
        report["opening_commands_with_unsuccessful_steps"] = sum(
            bool(case["unsuccessful_steps"]) for run in runs.values() for case in run.get("cases", []))
        report["opening_by_seat"] = {seat: dict(Counter(row["opening"]["result"] for row in rows if row["seat"] == seat))
                                     for seat in TURN_ORDER}
        phase_cases = check_phase_listeners(rules, args.seed)
        for row in rows:
            if row["id"] in phase_cases:
                row["phase_listener_case"] = phase_cases[row["id"]]
        report["phase_listeners"] = {"attempted": len(phase_cases), "activated": sum(r["activated"] for r in phase_cases.values()),
                                     "settled": sum(r["settled"] for r in phase_cases.values())}
        active_cases = check_active_statuses(rules, args.seed)
        for row in rows:
            if row["id"] in active_cases:
                row["active_status_case"] = active_cases[row["id"]]
        report["active_statuses"] = {"attempted": len(active_cases),
                                     "available": sum(r["available"] for r in active_cases.values())}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    print(json.dumps({key: value for key, value in report.items() if key != "cards"}))
    if divergent or any(not row["exact_sequence"] for row in decks.values()):
        parser.exit(1, "Catalog differs from the source. See the local report.\n")


if __name__ == "__main__":
    main()
