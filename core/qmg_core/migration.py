"""Compile a local user transcription export into a Core catalog and coverage report."""
from argparse import ArgumentParser
from copy import deepcopy
from pathlib import Path
import json

from .catalog import ContentCatalog
from .content_effects import instruction, supported_effect
from .content_triggers import supported_trigger
from .definitions import RuleSet
from .model import CoreError, TURN_ORDER


def passive_modifier(passive):
    kind = passive["type"]
    modifier = {"scope": "all", **({"force": passive["force"]} if "force" in passive else {})}
    if kind == "always-supplied":
        return {**modifier, "kind": "always_supplied", **({"kinds": passive["kinds"]} if "kinds" in passive else {})}
    if kind in ("add-supply-source", "deny-supply-source", "set-home"):
        native = {"add-supply-source": "supply_source", "deny-supply-source": "deny_supply_source", "set-home": "home"}[kind]
        return {**modifier, "kind": native, "region": passive["space"],
                **({"team": passive["side"]} if "side" in passive else {})}
    if kind == "adjacency":
        return {**modifier, "kind": "connection", "a": passive["from"], "b": passive["to"],
                "enabled": passive["connected"], "team": passive["side"]}
    if kind == "economic-discard-adjust":
        return {**modifier, "kind": "economic_adjustment", "role": passive["role"],
                "country": passive["country"], "amount": passive["amount"]}
    if kind == "set-home-if-enemy-army":
        return {**modifier, "kind": "home", "region": passive["space"], "enemy_occupied_region": passive["triggerSpace"]}
    if kind == "share-supply-lines":
        return {**modifier, "kind": "supply_sharing", "forces": passive["forces"]}
    if kind == "replace-army-build-with-recruit":
        return {**modifier, "kind": "recruit_region", "force": passive["country"], "region": passive["space"]}
    if kind == "ignore-enemy-statuses-during-own-turn":
        return {"kind": "ignore_enemy_statuses"}
    if kind == "reallocate-cost":
        return {**modifier, "kind": "reallocate_cost", "country": passive["country"], "count": passive["count"]}
    if kind == "reallocate-discard-source":
        return {**modifier, "kind": "reallocate_discard", "country": passive["country"]}
    return None


def _effect_gaps(effects):
    return sorted({effect["type"] for effect in effects if not supported_effect(effect)})


def compile_transcriptions(export):
    if export.get("format") != "qmg-user-transcriptions-v1":
        raise CoreError("Unsupported transcription export")
    pack = export["pack"]
    countries = [{"id": key, "name": export["country_names"][key], "team": export["country_teams"][key]}
                 for key in TURN_ORDER]
    regions = [{"id": p["id"], "name": p["name"], "type": p["kind"].upper(),
                **({"home_country": p["home"], "supply_source_for": p["home"]} if p.get("home") else {})}
               for p in pack["spaces"]]
    edges = sorted({tuple(sorted((p["id"], neighbor))) for p in pack["spaces"] for neighbor in p["neighbors"]})
    cards, abilities, coverage, area_responses = [], [], [], {}
    for index, original in enumerate(pack["cards"]):
        original = deepcopy(original)
        key, kind, country = original["id"], original["kind"], original["country"]
        effects = original.get("effects", [])
        gaps = _effect_gaps(effects)
        trigger = original.get("trigger")
        if trigger and not supported_trigger(trigger):
            gaps.append("trigger:" + trigger["type"])
        if original.get("activeEffects"):
            gaps.extend("active:" + gap for gap in _effect_gaps(original["activeEffects"]))
        modifiers = [passive_modifier(modifier) for modifier in original.get("passives", [])]
        gaps.extend("passive:" + modifier["type"] for modifier in original.get("passives", [])
                    if passive_modifier(modifier) is None and modifier["type"] not in ("battle-attacker-discard", "discard-stage-score"))
        native = {"build-army": "BUILD_ARMY", "build-navy": "BUILD_NAVY",
                  "land-battle": "LAND_BATTLE", "sea-battle": "SEA_BATTLE"}.get(kind)
        definition = {"id": key, "country": country, "title": original["name"], "text": original["text"],
                      "type": "STATUS" if kind == "status" else "RESPONSE" if kind == "response" else "EVENT",
                      "legacy_kind": kind, "failure_policy": "stop", "effects": [], "abilities": [],
                      "source_card": original}
        if modifiers and not gaps:
            definition["modifiers"] = [m for m in modifiers if m is not None]
            if definition["modifiers"]:
                passive_id = "rules-" + key
                definition["abilities"].append(passive_id)
                abilities.append({"id": passive_id, "activation_mode": "AUTO", "after": "origin", "priority": 60000 + index,
                    "trigger": {"operation_type": "CARD_EVENT", "card_trigger": {"type": "before-rule-use"}}, "effects": []})
        if "condition" in original:
            definition["content_condition"] = original["condition"]
        if original.get("playAs"):
            modes = {"build-army": "BUILD_ARMY", "build-navy": "BUILD_NAVY",
                     "land-battle": "LAND_BATTLE", "sea-battle": "SEA_BATTLE"}
            definition["play_as"] = [modes[mode] for mode in original["playAs"]]
        status = "blocked" if gaps else "executable"
        definition["implementation"] = {"status": status, "gaps": sorted(set(gaps))}
        if native:
            definition["effects"] = [{"type": "CHOOSE", "operation": {"type": native, "target": "$choice"}}]
        elif trigger:
            ability_id = "use-" + key
            definition["abilities"].append(ability_id)
            ability = {"id": ability_id, "activation_mode": "OPTIONAL",
                       "after": "origin" if kind == "status" else "delete", "priority": 100 + index,
                       "trigger": {"operation_type": "CARD_EVENT", "card_trigger": trigger},
                       "effects": [instruction(effect) for effect in effects] if not gaps else []}
            if kind == "bolster":
                ability["listener_scope"] = ["HAND", "RESPONSE"]
            if trigger.get("oncePerTurn"):
                ability.pop("after")
                ability["reset_policy"] = "TURN_END"
            abilities.append(ability)
        elif not gaps:
            definition["effects"] = [instruction(effect) for effect in effects]
        for passive_index, passive in enumerate(original.get("passives", [])):
            if passive["type"] == "discard-stage-score" and not gaps:
                ability_id = f"passive-{key}-{passive_index}"
                definition["abilities"].append(ability_id)
                abilities.append({"id": ability_id, "activation_mode": "OPTIONAL", "priority": 31000 + index,
                    "after": "origin", "trigger": {"operation_type": "CARD_EVENT", "card_trigger": {
                        "type": "discard-stage-card", "kind": passive["kind"], "country": passive["country"]}},
                    "effects": [instruction({"type": "points", "side": "self", "amount": passive["points"]})]})
            if passive["type"] != "battle-attacker-discard" or gaps:
                continue
            ability_id = f"passive-{key}-{passive_index}"
            definition["abilities"].append(ability_id)
            ability = {"id": ability_id, "activation_mode": "OPTIONAL", "priority": 30000 + index,
                "trigger": {"operation_type": "CARD_EVENT", "card_trigger": {
                    "type": "before-battle-removal", "victims": passive["victims"], "kind": passive["kind"],
                    **({"spaces": passive["spaces"]} if "spaces" in passive else {})}},
                "effects": [instruction({"type": "discard-top", "target": "battle-attacker", "count": passive["count"]})]}
            if passive.get("oncePerTurn"):
                ability["reset_policy"] = "TURN_END"
            else:
                ability["after"] = "origin"
            abilities.append(ability)
        if reaction := original.get("economicReaction"):
            ability_id = "economic-" + key
            definition["abilities"].append(ability_id)
            abilities.append({"id": ability_id, "activation_mode": "OPTIONAL", "after": "origin",
                "priority": 32000 + index, "trigger": {"operation_type": "CARD_EVENT", "card_trigger": {
                    "type": "economic-play", "actor": "any", "keyword": reaction["keyword"]}},
                "effects": [instruction({"type": "economic-discard-bonus", "amount": reaction["amount"],
                                         **({"bonusIf": reaction["bonusIf"]} if "bonusIf" in reaction else {})})]})
        if original.get("activeEffects") and not gaps:
            active_id = "active-" + key
            definition["active_ability"] = active_id
            definition["abilities"].append(active_id)
            abilities.append({"id": active_id, "activation_mode": "AUTO", "after": "origin",
                              "priority": 1000 + index, "trigger": {"operation_type": "USE_STATUS"},
                              "effects": [instruction(effect) for effect in original["activeEffects"]]})
        pending = [*effects, *original.get("activeEffects", [])]
        area = []
        while pending:
            effect = pending.pop()
            pending.extend(child for branch in effect.get("options", []) for child in branch["effects"])
            if effect["type"] == "forbid-air-defense-this-turn":
                area_id = f"area-{key}-{len(area)}"
                area.append(area_id)
                abilities.append({"id": area_id, "activation_mode": "AUTO", "after": "delete", "priority": 21000 + index,
                                  "trigger": {"operation_type": "TURN_END"}, "effects": [], "global_rule": "forbid-air-defense"})
                continue
            if effect["type"] not in ("protect-units-this-turn", "supply-units-this-turn"):
                continue
            area_id = f"area-{key}-{len(area)}"
            area.append(area_id)
            mode = "protection" if effect["type"] == "protect-units-this-turn" else "supply"
            abilities.append({"id": area_id, "activation_mode": "AUTO", "after": "delete",
                              "priority": 20000 + index, "trigger": {"operation_type": "TURN_END"}, "effects": [],
                              "area_rule": {"mode": mode, "filter": effect["filter"]}})
        if area:
            area_responses[key] = area
        if kind == "status":
            restore = "restore-" + key
            area_responses.setdefault(key, []).append(restore)
            abilities.append({"id": restore, "activation_mode": "AUTO", "after": "delete", "priority": 40000 + index,
                "trigger": {"operation_type": "TURN_END"}, "effects": [], "on_settle": [
                    {"type": "TRANSFER", "token": "$bound_source", "destination": "$owner_status"}]})
        cards.append(definition)
        coverage.append({"id": key, "country": country, "title": original["name"], "kind": kind,
                         "status": status, "gaps": sorted(set(gaps))})
    for index, definition in enumerate(("unit-protection", "unit-supply")):
        abilities.append({"id": definition, "activation_mode": "AUTO", "after": "delete",
                          "priority": 10000 + index, "trigger": {"operation_type": "TURN_END"}, "effects": []})
    initial = []
    for country in TURN_ORDER:
        initial.extend({"token_id": f"card-{country}-{i + 1}", "definition_id": key,
                        "token_type": "CARD", "owner_country": country, "position_id": f"{country}:DECK"}
                       for i, key in enumerate(pack["decks"][country]))
    for country, limits in pack["limits"].items():
        home = next((r["id"] for r in regions if r.get("home_country") == country), None)
        for unit_kind in ("army", "navy", "air"):
            for index in range(limits[unit_kind]):
                initial.append({"token_id": f"{country}-{unit_kind}-{index + 1}", "definition_id": unit_kind,
                                "token_type": "UNIT", "owner_country": country,
                                "position_id": home if unit_kind == "army" and index == 0 and home else
                                               f"{country}:RESERVE_{unit_kind.upper()}"})
    sites = [{"id": "score-" + p["id"], "region": p["id"], "vp": 2, "countries": list(TURN_ORDER),
              "requires_supply": True} for p in pack["spaces"] if p.get("supply")]
    opportunities = {"air-opportunity-" + force: force for force in pack["limits"]}
    opportunities.update({"reallocation-opportunity-" + country: country for country in TURN_ORDER})
    for index, key in enumerate(opportunities):
        abilities.append({"id": key, "activation_mode": "AUTO", "after": "delete", "priority": 50000 + index,
                          "trigger": {"operation_type": "TURN_END"}, "effects": [], "on_settle": [
                              {"type": "TRANSFER", "token": "$object", "destination": "$owner_ready"}]})
    rules = RuleSet({
        "ruleset_version": f"transcribed-{pack['id']}-v{pack['version']}",
        "countries": countries, "turn_order": list(TURN_ORDER),
        "auxiliary_forces": [{"id": "france", "name": export["country_names"]["france"], "controller": "united-kingdom"},
                             {"id": "china", "name": export["country_names"]["china"], "controller": "united-states"}],
        "regions": regions,
        "connections": [{"id": f"edge-{i}", "a": a, "b": b} for i, (a, b) in enumerate(edges)],
        "units": [{"id": "army", "kind": "ARMY"}, {"id": "navy", "kind": "NAVY"}, {"id": "air", "kind": "AIR"}],
        "cards": cards, "abilities": abilities, "score_sites": sites, "initial_tokens": initial,
        "settings": {"initial_hand": 12, "setup_discard": 5, "hand_limit": 7, "plays_per_turn": 1,
                     "max_rounds": 20, "victory_margin": 30, "deck_exhaustion_vp": -1,
                     "max_atomic_steps": 10000, "allied_supply": False,
                     "unit_responses": ["unit-protection", "unit-supply"], "area_responses": area_responses,
                     "record_turn_start": True, "air_phase": True, "temporary_status_disable": True,
                     "reallocation": True, "turn_opportunities": opportunities, "free_recruit": True, "naval_army_base": True,
                     "surface_supply": True, "straits": pack.get("straits", []), "declare_passives": True},
        "source_pack": {"id": pack["id"], "version": pack["version"], "verified": pack["verified"],
                        "straits": pack.get("straits", []), "limits": pack["limits"],
                        "reviewed": export.get("reviewed", [])},
    })
    report = {"total_definitions": len(cards), "total_copies": sum(len(deck) for deck in pack["decks"].values()),
              "executable": sum(item["status"] == "executable" for item in coverage),
              "blocked": sum(item["status"] == "blocked" for item in coverage), "cards": coverage}
    return rules, report


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        source = json.loads(args.source.read_text(encoding="utf-8"))
        rules, report = compile_transcriptions(source)
        root = args.destination
        root.mkdir(parents=True, exist_ok=True)
        (root / "rules.json").write_text(json.dumps(rules.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        catalog = root / f"{rules.fingerprint}.sqlite3"
        if not catalog.exists():
            ContentCatalog.build(rules, catalog)
        report.update(rules_digest=rules.fingerprint, catalog=str(catalog.resolve()))
        (root / "coverage.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({key: value for key, value in report.items() if key != "cards"}))
        print(json.dumps({"catalog": str(catalog), "rules_digest": rules.fingerprint}))
    except (CoreError, OSError, ValueError) as error:
        parser.exit(1, f"Migration error: {error}\n")


if __name__ == "__main__":
    main()
