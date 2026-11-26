from copy import deepcopy

import pytest

from conftest import card, make, move_initial
from qmg_core import CoreAPI, ScoreEvaluator
from qmg_core.ai_match_policy import CandidateView, RulePolicy
from qmg_core.content_effects import instruction
from qmg_core.evaluation import OneStepPlanner
from qmg_core.model import zone
from qmg_core.supply_stability import SupplyStability
from test_action_distance_scoring import forecast, map_with_sites
from test_ai_match import profile
from test_hand_retention import hand_only
from test_status_scoring import scoring_status


def modifier_card(data, *modifiers):
    rounds = data["settings"]["max_rounds"]
    ability = scoring_status(data, phase="before-rule-use")
    data["settings"]["max_rounds"] = rounds
    ability.update(effects=[], activation_mode="AUTO")
    card(data, "g-status")["modifiers"] = list(modifiers)


def test_supply_average_counts_distinct_ports_and_permanent_waivers(data):
    map_with_sites(data, [("A", "B"), ("S", "B"), ("S", "A"), ("S", "D")], ("B",))
    data["settings"]["supply_revision"] = 2
    move_initial(data, "germany-navy-0", "S")
    base = forecast(data).routes.supply_stability
    assert base.by_force["germany"] == pytest.approx(5 / 3)
    move_initial(data, "italy-army-0", "D")
    port = forecast(data).routes.supply_stability
    assert port.by_force["germany"] == pytest.approx(6 / 3)
    modifier_card(data, {"kind": "always_supplied", "scope": "all", "force": "germany", "kinds": ["army"]})
    move_initial(data, "g-status", zone("germany", "STATUS"))
    state = forecast(data).routes.supply_stability
    assert state.by_force["germany"] == pytest.approx(7 / 3)
    assert SupplyStability(state.rules, state.state, 2).by_force["germany"] == pytest.approx(14 / 3)


def test_sharing_counts_another_countrys_source_only_when_opened(data):
    map_with_sites(data, [("A", "E")], ())
    data["settings"].update(supply_revision=2, allied_supply=False)
    move_initial(data, "germany-navy-0", zone("germany", "RESERVE_NAVY"))
    move_initial(data, "united-kingdom-army-0", zone("united-kingdom", "RESERVE_ARMY"))
    next(region for region in data["regions"] if region["id"] == "E").update(home_country="italy", supply_source_for="italy")
    modifier_card(data, {"kind": "supply_sharing", "scope": "all", "forces": ["germany", "italy"]})
    current = forecast(data)
    assert current.routes.supply_stability.by_force["germany"] == 1
    assert current.statuses.continuous_value("germany", "g-status", None) == 1
    move_initial(data, "g-status", zone("germany", "STATUS"))
    assert forecast(data).routes.supply_stability.by_force["germany"] == 2


def test_build_and_attack_include_the_same_stability_difference_in_a_and_b(data):
    map_with_sites(data, [("A", "B"), ("B", "C")], ("C",))
    data["settings"]["supply_revision"] = 2
    move_initial(data, "germany-navy-0", zone("germany", "RESERVE_NAVY"))
    weights = profile()
    game = make(data)
    client = CoreAPI(game, ("germany",))
    view = client.observe()
    routes = ScoreEvaluator(game.rules, weights).economic_from_view(view).routes
    assert routes.placement_stability("germany", "B") == .5
    build = next(c for c in client.legal_commands() if c.get("card") == "g-build")
    result, = OneStepPlanner(game.rules, weights).rank(CandidateView(view, [build]), "germany")
    assert result["complete"] and result["supply_stability_delta"] == .5
    move_initial(data, "united-kingdom-army-0", "B")
    next(region for region in data["regions"] if region["id"] == "B")["supply_source_for"] = "united-kingdom"
    game = make(data)
    client = CoreAPI(game, ("germany",))
    view = client.observe()
    scorer = ScoreEvaluator(game.rules, weights)
    routes = scorer.economic_from_view(view).routes
    victim = "united-kingdom-army-0"
    assert routes.removal_stability(victim) == 2
    attack = next(c for c in client.legal_commands() if c.get("card") == "g-battle")
    result, = OneStepPlanner(game.rules, weights).rank(CandidateView(view, [attack]), "germany")
    assert result["complete"] and result["supply_stability_delta"] == 2


def test_a_supply_source_is_a_national_scoring_anchor_without_creating_engine_vp(data):
    map_with_sites(data, [("A", "B")], ())
    modifier_card(data, {"kind": "supply_source", "region": "B", "force": "germany", "scope": "all"})
    weights = profile()
    weights["scoring_units"]["supply_stability"] = 0
    clock = forecast(data, weights=weights)
    assert clock.statuses.continuous_value("germany", "g-status", None) == clock.routes.position.force_site_interest("germany", "B", 2) > 0
    move_initial(data, "g-status", zone("germany", "STATUS"))
    game = make(data)
    assert game.rules.sites == {} and game.state.scores == {"axis": 0, "allies": 0}


def test_permanent_remote_build_site_uses_airdrop_distance_and_rejects_enemy_occupation(data):
    map_with_sites(data, [("B", "C")], ("C",))
    modifier_card(data, {"kind": "recruit_region", "force": "germany", "region": "B", "scope": "all"})
    weights = profile()
    weights["scoring_units"]["supply_stability"] = 0
    current = forecast(data, weights=weights)
    expected = current.routes.position.value("germany", "B", construction=True, distance_offset=1)
    assert current.statuses.continuous_value("germany", "g-status", None) == expected > 0
    move_initial(data, "united-kingdom-army-0", "B")
    assert forecast(data, weights=weights).statuses.continuous_value("germany", "g-status", None) == 0


def test_moving_the_home_site_changes_interest_for_both_sides(data):
    map_with_sites(data, [("A", "B"), ("B", "C"), ("C", "E")], ())
    modifier_card(data, {"kind": "home", "force": "germany", "region": "B", "scope": "all"})
    weights = profile()
    weights["scoring_units"]["supply_stability"] = 0
    move_initial(data, "united-kingdom-army-0", "B")
    clock = forecast(data, weights=weights)
    position = clock.routes.position
    friendly = position.force_site_interest("germany", "B", 2) - position.force_site_interest("germany", "A", 2)
    enemy = position.force_site_interest("united-kingdom", "A", 2) - position.force_site_interest("united-kingdom", "B", 2)
    assert enemy != 0
    assert clock.statuses.continuous_value("germany", "g-status", None) == friendly + enemy


def test_economic_modifier_counts_only_worthwhile_visible_cards_plus_one(data):
    map_with_sites(data, [], (), rounds=8)
    data["settings"]["deck_exhaustion_vp"] = -1
    modifier_card(data, {"kind": "economic_adjustment", "country": "germany", "role": "actor", "amount": 2})
    card(data, "g-noop").update(legacy_kind="economic", effects=[
        instruction({"type": "discard-top", "target": "united-kingdom", "count": 5})])
    card(data, "g-vp").update(legacy_kind="economic", effects=[
        instruction({"type": "discard-top", "target": "united-kingdom", "count": 0})])
    hand_only(data, "g-status", "g-deck-2", "g-vp")
    visible = forecast(data)
    assert visible.statuses.continuous_value("germany", "g-status", None) == 4
    move_initial(data, "g-deck-2", zone("germany", "DECK"))
    no_worthwhile = forecast(data)
    assert no_worthwhile.statuses.continuous_value("germany", "g-status", None) == 2


def test_defensive_economic_modifier_adds_one_opportunity_across_all_opponents(data):
    map_with_sites(data, [], (), rounds=8)
    data["settings"]["deck_exhaustion_vp"] = -1
    modifier_card(data, {"kind": "economic_adjustment", "country": "germany", "role": "target", "amount": -2, "scope": "all"})
    data["initial_tokens"] = [t for t in data["initial_tokens"] if t["position_id"] != zone("germany", "DECK")]
    clock = forecast(data)
    assert clock.statuses.continuous_value("germany", "g-status", None) == 2


def test_final_round_values_only_economic_effects_that_settle_now(data):
    map_with_sites(data, [], (), rounds=1)
    data["settings"]["deck_exhaustion_vp"] = -1
    card(data, "g-noop").update(legacy_kind="economic", effects=[
        instruction({"type": "discard-top", "target": "united-kingdom", "count": 1})])
    hand_only(data, "g-deck-2")
    game = make(data)
    client = CoreAPI(game, ("germany",))
    view = client.observe()
    command = next(c for c in client.legal_commands() if c["type"] == "PLAY_CARD")
    evaluator = ScoreEvaluator(game.rules, profile())
    economy = evaluator.economic_from_view(view)
    assert economy.mill("germany", "united-kingdom", 1, 1) == 0
    assert RulePolicy(game.rules, evaluator).card_value(game.rules.cards["g-noop"], "germany", economy) == 0
    outcome, = OneStepPlanner(game.rules, profile()).rank(CandidateView(view, [command]), "germany")
    assert outcome["actual_vp_delta"] == outcome["resource_delta"] == 0
    assert economy.mill("germany", "united-kingdom", 3, 1) == 2


def test_status_suppression_counts_each_enemy_source_once_on_our_turn(data):
    map_with_sites(data, [("A", "B")], ("B",))
    modifier_card(data, {"kind": "ignore_enemy_statuses"})
    move_initial(data, "united-kingdom-army-0", "B")
    for idx, points in enumerate((2, 3)):
        key = f"defense-{idx}"
        data["abilities"].append({"id": key, "trigger": {"operation_type": "CARD_EVENT", "card_trigger": {
            "type": "before-battle-removal", "kind": "army", "victim": "self"}},
            "priority": 10, "activation_mode": "OPTIONAL", "after": "origin",
            "effects": [instruction({"type": "points", "amount": points, "side": "self"})]})
        source = deepcopy(card(data, "g-status"))
        source.update(id=key, country="united-kingdom", abilities=[key], modifiers=[])
        data["cards"].append(source)
        data["initial_tokens"].append({"token_id": key, "definition_id": key, "token_type": "CARD",
                                      "owner_country": "united-kingdom", "position_id": zone("united-kingdom", "STATUS")})
    current = forecast(data)
    assert current.statuses.continuous_value("germany", "g-status", None) == 5
    game = make(data)
    assert "tokens" not in CoreAPI(game, ("germany",)).observe()["positions"][zone("united-kingdom", "HAND")]
