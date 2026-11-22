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
        ("germany", 0, 2), ("italy", 4, 2)]
    assert held["baseline_vp"] == pytest.approx(2 + 2 * profile["time_discount"] ** 4)
    for _ in range(2):
        client.submit(next(c for c in client.legal_commands() if c["type"] == "END_PHASE"))
    value = scorer.evaluate(client, "germany")
    assert value.actual_vp == 2
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
    assert after.card_value - before.card_value == pytest.approx(4 * profile["effect_value_weights"]["points"])
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
