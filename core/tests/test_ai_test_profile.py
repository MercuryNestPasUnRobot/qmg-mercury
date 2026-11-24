from copy import deepcopy
import json
from pathlib import Path

import pytest

from conftest import card, make, move_initial
from qmg_core import CoreAPI
from qmg_core.api import simulation_from_observation
from qmg_core.content_effects import instruction
from qmg_core.evaluation import ScoreEvaluator


@pytest.fixture
def profile():
    return json.loads(Path("data/ai_test_weights.json").read_text(encoding="utf8"))


def test_forecast_uses_seat_opportunities_and_never_rescores_this_victory(data, profile):
    data["settings"]["max_rounds"] = 1
    data["score_sites"] = [{"id": "held", "region": "A", "vp": 2, "countries": ["germany", "italy"],
                            "requires_supply": False}]
    data["initial_tokens"] = [token for token in data["initial_tokens"] if token["token_type"] != "SCORE_SOURCE"]
    move_initial(data, "italy-army-0", "A")
    p = make(data)
    client = CoreAPI(p, ("germany",))
    scorer = ScoreEvaluator(p.rules, profile)
    held = scorer.evaluate(client, "germany").score_points[0]
    assert [(r["seat"], r["delay"], r["vp"]) for r in held["opportunities"]] == [
        ("germany", 0, 1), ("italy", 4, 1)]
    assert held["baseline_vp"] == held["fixed_score"] == 2
    assert held["long_term_score"] == 0
    for _ in range(2):
        client.submit(next(c for c in client.legal_commands() if c["type"] == "END_PHASE"))
    value = scorer.evaluate(client, "germany")
    assert value.actual_vp == 1
    assert [r["seat"] for r in value.score_points[0]["opportunities"]] == ["italy"]


def test_potential_counts_repeated_effects_but_only_one_exclusive_branch(data, profile):
    source = card(data, "g-chain")
    source["effects"] = [instruction({"type": "choose-one", "options": [
        {"label": "a", "effects": [{"type": "points", "amount": 4, "side": "self"}]},
        {"label": "b", "effects": [{"type": "points", "amount": 1, "side": "self"}]},
    ]})]
    p = make(data)
    before = ScoreEvaluator(p.rules, profile).evaluate(CoreAPI(p, ("germany",)), "germany")
    repeat = deepcopy(source["effects"][0])
    source["effects"].append(repeat)
    q = make(data)
    after = ScoreEvaluator(q.rules, profile).evaluate(CoreAPI(q, ("germany",)), "germany")
    assert after.card_value - before.card_value == pytest.approx(4 * profile["scoring_units"]["vp"])
    assert after.actual_vp == before.actual_vp == 0


def test_hidden_card_rule_replica_does_not_leak_source_or_break_ai_projection(data, profile):
    data["abilities"].append({"id": "area", "trigger": {"operation_type": "TURN_END"},
                              "activation_mode": "AUTO", "after": "delete", "priority": 900, "effects": []})
    data["settings"]["area_responses"] = {"g-noop": ["area"]}
    p = make(data)
    client = CoreAPI(p, ("germany",))
    hidden_rule = "rule:g-deck-2:area"
    view = client.observe()
    assert hidden_rule in p.state.tokens and hidden_rule not in view["tokens"]
    simulation_from_observation(p.rules, view)
    assert ScoreEvaluator(p.rules, profile).evaluate(client, "germany").actual_vp == 0


def test_routes_use_four_nearest_sites_and_adjust_for_allied_auxiliary_units(data, profile):
    data["auxiliary_forces"] = [{"id": "china", "controller": "united-states"}]
    data["regions"].extend({"id": f"lane-{i}", "type": "LAND"} for i in range(9))
    data["connections"].extend({"id": f"lane-edge-{i}", "a": f"lane-{i}", "b": f"lane-{i+1}"}
                               for i in range(8))
    data["initial_tokens"] = [t for t in data["initial_tokens"] if t["token_type"] != "SCORE_SOURCE"]
    data["score_sites"] = [{"id": f"site-{i}", "region": f"lane-{i}", "vp": 2,
                            "countries": ["soviet-union"], "requires_supply": False} for i in range(4, 9)]
    move_initial(data, "soviet-union-army-0", "lane-0")
    move_initial(data, "germany-army-0", "lane-2")
    data["initial_tokens"].append({"token_id": "china-army-0", "definition_id": "army", "token_type": "UNIT",
                                   "owner_country": "china", "position_id": "lane-1"})
    p = make(data)
    client = CoreAPI(p, ("soviet-union",))
    scorer = ScoreEvaluator(p.rules, profile)
    result = scorer.evaluate(client, "soviet-union")
    routes = [row for row in result.token_influence if row["token"] == "soviet-union-army-0"]
    assert [row["distance"] for row in routes] == [4, 5, 6, 7]
    assert all(row["route_factor"] == pytest.approx(0.8 * 1.15) and row["score_influence"] > 0 for row in routes)
    # The choice rule uses the same four routes even when no unit has yet been built there.
    expected = 2 * sum(0.1 * 0.7 ** (distance - 3) * 0.8 * 1.15 for distance in (4, 5, 6, 7))
    assert scorer.target_proximity(client.observe(), "soviet-union", ["lane-0"])["lane-0"] == pytest.approx(expected)
