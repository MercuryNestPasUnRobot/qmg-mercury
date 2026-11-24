from copy import deepcopy

import pytest

from conftest import card, choose, make, move_initial, play
from qmg_core import CoreAPI
from qmg_core.ai_match_policy import CandidateView, RulePolicy
from qmg_core.evaluation import OneStepPlanner, ScoreEvaluator
from test_ai_match import profile


def test_only_actual_play_increases_discard_cost_and_neither_policy_charges_play(data):
    data["settings"]["plays_per_turn"] = 2
    card(data, "g-build")["legacy_kind"] = "build-army"
    card(data, "g-battle")["legacy_kind"] = "land-battle"
    engine = make(data)
    client = CoreAPI(engine, ("germany",))
    weights = profile()
    original = {**weights, "basic_usage_cost": 0}
    evaluator = ScoreEvaluator(engine.rules, weights)
    basic = engine.rules.cards["g-build"]
    assert client.observe()["basic_card_use"]["germany"]["build-army"] == {"played": 0, "total": 2}
    play(engine, "g-build")
    choose(engine, "B")
    view = client.observe(include_history=False)
    assert view["basic_card_use"]["germany"] == {"build-army": {"played": 1, "total": 2},
                                                "land-battle": {"played": 0, "total": 1}}
    assert "germany" not in CoreAPI(engine, ("italy",)).observe()["basic_card_use"]
    assert evaluator.basic_discard_cost(view, "germany", basic) == pytest.approx(0.6)

    command = next(row for row in client.legal_commands() if row.get("card") == "g-copy")
    policies = [RulePolicy(engine.rules, ScoreEvaluator(engine.rules, w)) for w in (original, weights)]
    assert policies[0].rank(view, [command]) == policies[1].rank(view, [command])
    offered = CandidateView(view, [command])
    old, new = [OneStepPlanner(engine.rules, w).rank(offered, "germany")[0] for w in (original, weights)]
    assert old["complete"] and new["complete"] and new["q"] == old["q"]
    assert policies[1].discard_value(basic, "germany", None, view) == pytest.approx(
        policies[0].discard_value(basic, "germany", None, view) - 0.6)
    assert evaluator.retention(view, "germany")["g-copy"]["used_basic_cost"] == pytest.approx(0.6)

    for _ in range(3):
        client.submit(next(c for c in client.legal_commands() if c["type"] == "END_PHASE"))
    before_discard = client.observe(include_history=False)
    assert before_discard["decision"]["operation_type"] == "DISCARD"
    discard = next(c for c in client.legal_commands() if c.get("option") == "g-copy")
    discard_scores = [policy.rank(before_discard, [discard])[0]["rule_score"] for policy in policies]
    assert discard_scores[1] == pytest.approx(discard_scores[0] - 0.6)
    client.submit(discard)
    updated = client.observe(include_history=False)
    assert "g-copy" in updated["positions"]["germany:DISCARD"]["tokens"]
    assert updated["basic_card_use"] == view["basic_card_use"]


def test_fixed_inventory_denominator_and_one_count_per_physical_basic(data):
    card(data, "g-build")["legacy_kind"] = "build-army"
    card(data, "g-build")["after"] = "origin"
    data["settings"]["plays_per_turn"] = 2
    expanded = deepcopy(data)
    extra = deepcopy(next(t for t in expanded["initial_tokens"] if t["token_id"] == "g-copy"))
    expanded["initial_tokens"].append({**extra, "token_id": "g-third-copy", "position_id": "germany:DECK"})
    for config, expected in ((data, 0.6), (expanded, 0.4)):
        engine = make(config)
        client = CoreAPI(engine, ("germany",))
        for target in ("B", "C"):
            play(engine, "g-build")
            choose(engine, target)
        view = client.observe(include_history=False)
        evaluator = ScoreEvaluator(engine.rules, profile())
        assert evaluator.basic_discard_cost(view, "germany", engine.rules.cards["g-build"]) == pytest.approx(expected)


def test_local_friendly_crowding_discounts_building_and_leaves_battle_scores(data):
    move_initial(data, "italy-army-0", "B")
    engine = make(data)
    view = CoreAPI(engine, ("germany",)).observe()
    weights = profile()
    old = deepcopy(weights)
    old["frontier_scoring"].pop("build_friendly_neighbor")
    base, new = [ScoreEvaluator(engine.rules, w).economic_from_view(view).routes for w in (old, weights)]
    region = "B"  # The ally in B is charged to the factory. The army in A counts only for g-home.
    assert new.construction_factors("germany", region) == {"factory": 0.75, "g-home": 0.75}
    assert new.target_value("germany", region, battle=True) == base.target_value("germany", region, battle=True)
    client = CoreAPI(engine, ("germany",))
    map_values = [ScoreEvaluator(engine.rules, w).evaluate(client, "germany").score_points for w in (old, weights)]
    before, after = [next(site["value"] for site in points if site["site"] == "factory") for points in map_values]
    assert before > after
