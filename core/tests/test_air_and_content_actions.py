"""Synthetic cases for mechanics used by the locally imported content."""
import json

from conftest import card, choose, make, move_initial, play
from qmg_core import CoreAPI, SaveRepository
from qmg_core.api import simulation_from_observation
from qmg_core.content_effects import instruction
from qmg_core.model import TURN_ORDER


def add_air(data):
    data["units"].append({"id": "air", "kind": "AIR"})
    data["settings"]["air_phase"] = True
    data["initial_tokens"].extend({"token_id": c + "-air", "definition_id": "air", "token_type": "UNIT",
        "owner_country": c, "position_id": c + ":RESERVE_AIR"} for c in TURN_ORDER)


def marker(data, key, force):
    data["settings"].setdefault("turn_opportunities", {})[key] = force
    data["abilities"].append({"id": key, "activation_mode": "AUTO", "after": "delete", "priority": 990,
        "trigger": {"operation_type": "TURN_END"}, "effects": [], "on_settle": [
            {"type": "TRANSFER", "token": "$object", "destination": "$owner_ready"}]})


def end_phase(p):
    p.submit(next(c for c in p.legal_commands(TURN_ORDER) if c["type"] == "END_PHASE"), TURN_ORDER)


def test_air_power_opportunity_is_a_restoring_response_object(tmp_path, data):
    add_air(data)
    marker(data, "air-opportunity-germany", "germany")
    card(data, "g-draw").update(legacy_kind="air-power", effects=[])
    p = make(data)
    inventory = set(p.state.tokens)
    end_phase(p)
    assert p.state.turn.phase == "Air"
    command = next(c for c in p.legal_commands(TURN_ORDER) if c["type"] == "USE_AIR")
    p.submit(command, TURN_ORDER)
    choice = next(c for c in p.state.decision.legal_options if json.loads(c)[0] == "deploy")
    choose(p, choice)
    assert p.state.tokens["germany-air"].position_id == "A"
    assert p.state.tokens["g-draw"].position_id == "germany:DISCARD"
    assert p.state.tokens["opportunity:air-opportunity-germany"].position_id == "germany:RESPONSE"
    assert not any(c["type"] == "USE_AIR" for c in p.legal_commands(TURN_ORDER))
    save = tmp_path / "air.json"
    SaveRepository.save(save, p)
    p = SaveRepository.recover(save, p.rules)
    for _ in range(5):
        end_phase(p)
        while p.state.decision:
            choose(p, p.state.decision.legal_options[0])
    assert p.state.turn.country_index == 1
    assert p.state.tokens["opportunity:air-opportunity-germany"].position_id == "germany:ABILITY_READY"
    assert set(p.state.tokens) == inventory
    moves = [c for r in p.records for c in r["outcome"]["changes"]
             if c["token_id"] == "opportunity:air-opportunity-germany"]
    assert any(c["from"] == "germany:RESPONSE" and c["to"] == "RESOLUTION" for c in moves)


def battle_setup(data):
    add_air(data)
    move_initial(data, "united-kingdom-army-0", "B")
    move_initial(data, "united-kingdom-army-1", "E")
    move_initial(data, "united-kingdom-air", "B")
    move_initial(data, "uk-status", "united-kingdom:DISCARD")
    move_initial(data, "germany-air", "A")


def test_air_defense_then_counterattack_pays_real_air_costs_and_resumes(tmp_path, data):
    battle_setup(data)
    p = make(data)
    play(p, "g-battle")
    choose(p, "B")
    assert p.state.decision.decision_type == "AIR_DEFENSE"
    choose(p, "air")
    assert p.state.tokens["united-kingdom-air"].position_id == "united-kingdom:RESERVE_AIR"
    assert p.state.decision.decision_type == "AIR_COUNTER"
    save = tmp_path / "counter.json"
    SaveRepository.save(save, p)
    p = SaveRepository.recover(save, p.rules)
    choose(p, "germany-air")
    assert p.state.tokens["germany-air"].position_id == "germany:RESERVE_AIR"
    assert p.state.tokens["united-kingdom-army-0"].position_id == "united-kingdom:RESERVE_ARMY"
    assert not p.state.stack


def test_prevented_air_sacrifice_does_not_pay_defense_cost(data):
    battle_setup(data)
    guard = next(a for a in data["abilities"] if a["id"] == "uk-prevent")
    guard.update(trigger={"operation_type": "ANNOUNCE", "announced_type": "ELIMINATE", "actor": "self"},
                 effects=[instruction({"type": "prevent-removal"})])
    move_initial(data, "uk-status", "united-kingdom:STATUS")
    p = make(data)
    play(p, "g-battle")
    choose(p, "B")
    choose(p, "air")
    assert p.state.tokens["united-kingdom-army-0"].position_id == "united-kingdom:RESERVE_ARMY"
    assert p.state.decision.decision_type == "AIR_REBASE"
    choose(p, "E")
    assert p.state.tokens["united-kingdom-air"].position_id == "E"
    assert not any(r["operation_type"] == "AIR_COUNTER_SELECTED" for r in p.records)


def test_air_superiority_event_and_after_action_effect_keep_attacker_identity(data):
    add_air(data)
    move_initial(data, "germany-air", "A")
    move_initial(data, "united-kingdom-army-0", "B")
    move_initial(data, "united-kingdom-air", "B")
    move_initial(data, "uk-status", "united-kingdom:DISCARD")
    card(data, "g-chain")["effects"] = [instruction({"type": "air-action", "force": "self", "actions": ["superiority"]})]
    card(data, "g-draw").update(legacy_kind="bolster", effects=[], abilities=["after-air"])
    data["abilities"].append({"id": "after-air", "activation_mode": "OPTIONAL", "after": "delete", "priority": 998,
        "listener_scope": ["HAND"], "trigger": {"operation_type": "CARD_EVENT", "card_trigger": {
            "type": "after-air-superiority", "near": {"space": "A", "maxDistance": 1}}},
        "effects": [instruction({"type": "points", "side": "self", "amount": 2})]})
    p = make(data)
    play(p, "g-chain")
    choice = next(c for c in p.state.decision.legal_options if json.loads(c)[3] == "united-kingdom-air")
    choose(p, choice)
    assert p.state.decision.decision_type == "ABILITY"
    choose(p, "use")
    assert p.state.tokens["germany-air"].position_id == "A"
    assert p.state.tokens["united-kingdom-air"].position_id == "united-kingdom:RESERVE_AIR"
    assert p.state.scores["axis"] == 2


def test_battle_selects_one_country_and_air_rebases_after_surface_loss(data):
    battle_setup(data)
    move_initial(data, "soviet-union-army-0", "B")
    p = make(data)
    play(p, "g-battle")
    choose(p, "B")
    assert p.state.decision.decision_type == "BATTLE_VICTIM"
    choose(p, "united-kingdom-army-0")
    choose(p, "unit")
    assert p.state.decision.decision_type == "AIR_REBASE"
    choose(p, "E")
    assert p.state.tokens["united-kingdom-air"].position_id == "E"
    assert p.state.tokens["soviet-union-army-0"].position_id == "B"


def test_reallocation_uses_payment_search_and_turn_end_response(data):
    data["settings"]["reallocation"] = True
    marker(data, "reallocation-opportunity-germany", "germany")
    card(data, "g-status")["modifiers"] = [{"kind": "reallocate_cost", "scope": "all", "country": "germany", "count": 1}]
    move_initial(data, "g-status", "germany:STATUS")
    card(data, "g-build")["legacy_kind"] = "build-army"
    move_initial(data, "g-build", "germany:DECK")
    p = make(data)
    before = p.state.turn.plays_remaining
    p.submit(next(c for c in p.legal_commands(TURN_ORDER) if c["type"] == "REALLOCATE"), TURN_ORDER)
    choose(p, "g-draw")
    assert p.state.tokens["g-draw"].position_id == "germany:HAND"
    choose(p, "confirm")
    choose(p, "0")
    choose(p, "g-build")
    assert p.state.tokens["g-draw"].position_id == "germany:DISCARD"
    assert p.state.tokens["g-build"].position_id == "germany:HAND"
    assert p.state.turn.plays_remaining == before
    assert p.state.tokens["opportunity:reallocation-opportunity-germany"].position_id == "germany:RESPONSE"
    assert not any(c["type"] == "REALLOCATE" for c in p.legal_commands(TURN_ORDER))
    clone = simulation_from_observation(p.rules, CoreAPI(p, ("germany",)).observe())
    assert clone.state.tokens["opportunity:reallocation-opportunity-germany"].position_id == "germany:RESPONSE"


def test_status_disable_restores_card_through_its_own_response(data):
    data["settings"]["temporary_status_disable"] = True
    data["settings"]["area_responses"] = {"g-status": ["restore-g-status"]}
    card(data, "g-status")["legacy_kind"] = "status"
    next(a for a in data["abilities"] if a["id"] == "g-extra")["trigger"] = {
        "operation_type": "CARD_EVENT", "card_trigger": {"type": "after-build", "builder": "self", "kind": "army"}}
    move_initial(data, "g-status", "germany:STATUS")
    data["abilities"].append({"id": "restore-g-status", "activation_mode": "AUTO", "after": "delete", "priority": 991,
        "trigger": {"operation_type": "TURN_END"}, "effects": [], "on_settle": [
            {"type": "TRANSFER", "token": "$bound_source", "destination": "$owner_status"}]})
    guard = next(a for a in data["abilities"] if a["id"] == "uk-prevent")
    guard.update(trigger={"operation_type": "CARD_EVENT", "card_trigger": {
        "type": "before-card-effect", "kind": "status", "actor": "enemy"}},
        effects=[instruction({"type": "disable-triggered-status"})])
    p = make(data)
    inventory = set(p.state.tokens)
    play(p, "g-build")
    choose(p, "B")
    choose(p, "use")
    assert p.state.tokens["g-status"].position_id == "germany:DISABLED_STATUS"
    assert p.state.tokens["rule:g-status:restore-g-status"].position_id == "germany:RESPONSE"
    for _ in range(5):
        end_phase(p)
        while p.state.decision:
            choose(p, p.state.decision.legal_options[0])
    assert p.state.tokens["g-status"].position_id == "germany:STATUS"
    assert p.state.tokens["rule:g-status:restore-g-status"].position_id == "germany:OBJECT_POOL"
    assert set(p.state.tokens) == inventory


def test_passive_rule_declaration_can_be_removed_before_target_revalidation(data):
    data["settings"]["declare_passives"] = True
    move_initial(data, "g-status", "germany:STATUS")
    card(data, "g-status").update(legacy_kind="status", abilities=["passive"], modifiers=[
        {"kind": "build_region", "unit_kind": "ARMY", "region": "C"}])
    data["abilities"].append({"id": "passive", "activation_mode": "AUTO", "after": "origin", "priority": 900,
        "trigger": {"operation_type": "CARD_EVENT", "card_trigger": {"type": "before-rule-use"}}, "effects": []})
    guard = next(a for a in data["abilities"] if a["id"] == "uk-prevent")
    guard.update(trigger={"operation_type": "CARD_EVENT", "card_trigger": {
        "type": "before-card-effect", "kind": "status", "actor": "enemy"}},
        effects=[instruction({"type": "discard-triggered-status"})])
    p = make(data)
    play(p, "g-build")
    assert "C" in p.state.decision.legal_options
    choose(p, "C")
    assert p.state.tokens["g-status"].position_id == "germany:DISCARD"
    assert not p.state.positions["C"]
    assert any(r["operation_type"] == "BUILD_ARMY" and not r["outcome"]["success"] for r in p.records)


def test_air_bonus_counts_only_air_in_range_of_target_home(data, weights):
    from qmg_core.evaluation import ScoreEvaluator
    add_air(data)
    move_initial(data, "germany-air", "A")
    data["settings"]["deck_exhaustion_vp"] = -1
    card(data, "g-chain").update(legacy_kind="economic", effects=[
        instruction({"type": "discard-top", "target": "united-kingdom", "count": 1})])
    card(data, "g-draw").update(legacy_kind="bolster", effects=[], abilities=["air-economic"])
    data["abilities"].append({"id": "air-economic", "activation_mode": "OPTIONAL", "after": "delete", "priority": 901,
        "listener_scope": ["HAND"], "trigger": {"operation_type": "CARD_EVENT", "card_trigger": {"type": "economic-play", "actor": "self"}},
        "effects": [instruction({"type": "economic-discard-bonus", "amount": 0,
            "perAirNearTargetHome": {"force": "self", "maxDistance": 2, "amountPer": 1}})]})
    p = make(data)
    evaluator = ScoreEvaluator(p.rules, weights)
    before = p.state.to_dict()
    assert evaluator.evaluate(CoreAPI(p, ("germany",)), "germany").total is not None
    assert p.state.to_dict() == before
    play(p, "g-chain")
    choose(p, "use")
    assert p.state.scores["allies"] == -1


def test_surface_supply_requires_naval_army_base_and_strait_control(data):
    from qmg_core.rules import MapGraph, SupplyResolver
    data["settings"]["surface_supply"] = True
    move_initial(data, "germany-navy-1", "T")
    p = make(data)
    assert "germany-navy-1" not in SupplyResolver(p.rules).supplied(p.state, "germany")
    data["connections"].append({"id": "base", "a": "D", "b": "T"})
    move_initial(data, "italy-army-0", "D")
    p = make(data)
    assert "germany-navy-1" in SupplyResolver(p.rules).supplied(p.state, "germany")
    data["connections"] = [edge for edge in data["connections"] if {edge["a"], edge["b"]} != {"S", "T"}]
    data["settings"]["straits"] = [{"land": "B", "seas": ["S", "T"]}]
    p = make(data)
    assert "T" in MapGraph(p.rules).neighbors("S", p.state, "united-kingdom")
    assert "T" not in MapGraph(p.rules).neighbors("S", p.state, "germany")
    move_initial(data, "germany-army-1", "B")
    p = make(data)
    assert "T" in MapGraph(p.rules).neighbors("S", p.state, "germany")
    assert "T" not in MapGraph(p.rules).neighbors("S", p.state, "united-kingdom")
