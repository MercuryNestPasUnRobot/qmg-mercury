import pytest

from conftest import card, make, move_initial
from qmg_core import CoreAPI, ScoreEvaluator
from qmg_core.ai_match_policy import RulePolicy
from qmg_core.content_effects import instruction
from qmg_core.evaluation import OneStepPlanner
from test_ai_match import profile
from test_hand_retention import hand_only


def test_a_and_b_share_the_units_for_vp_cards_and_actions(data):
    engine = make(data)
    client = CoreAPI(engine, ("germany",))
    weights = profile()
    weights["scoring_units"].update(vp=2, card=3, action=6)
    evaluator = ScoreEvaluator(engine.rules, weights)
    economy = evaluator.economic_from_view(client.observe())
    indicators = (
        ([{"type": "GAIN_VP", "amount": 2, "score_country": "united-kingdom"}], -4, -4),
        ([instruction({"type": "points", "side": "self", "amount": 2})], 4, 4),
        ([{"type": "DRAW", "count": 2}], 6, 6),
        ([{"type": "BUILD_ARMY"}], 4.8, 3),
        ([instruction({"type": "build", "kind": "army"})], 4.8, 3),
    )
    policy = RulePolicy(engine.rules, evaluator)
    for steps, paper, held in indicators:
        assert policy.instructions(steps, "germany", economy) == pytest.approx(paper)
        assert evaluator.program_value(engine.rules, engine.state, "germany", steps, economy) == pytest.approx(held)


def test_paid_card_stock_and_settled_vp_are_counted_once_in_b(data):
    card(data, "g-noop")["effects"] = []
    hand_only(data, "g-deck-2", "g-vp")
    engine = make(data)
    client = CoreAPI(engine, ("germany",))
    weights = profile()
    weights["scoring_units"].update(vp=2, card=3)
    planner = OneStepPlanner(engine.rules, weights)
    ranked = {r["command"].get("card"): r for r in planner.rank(client, "germany") if r["complete"]}
    blank = ranked["g-deck-2"]
    assert blank["card_delta"] == pytest.approx(-3)
    assert "initial_card_score" not in blank
    points = ranked["g-vp"]
    assert points["actual_vp_delta"] > 0
    assert points["vp_score_delta"] == 2 * points["actual_vp_delta"]
    assert points["q"] == pytest.approx(sum(points[key] for key in
                                          ("vp_score_delta", "map_delta", "card_delta", "resource_delta")))
    client.submit(points["command"])
    assert engine.state.scores["axis"] == points["actual_vp_delta"]


def test_two_scoring_rounds_use_the_vp_unit_and_leave_later_vp_separate(data):
    data["settings"].update(max_rounds=5, victory_margin=0)
    data["score_sites"] = [site for site in data["score_sites"] if site["id"] == "factory"]
    data["initial_tokens"] = [t for t in data["initial_tokens"] if t["token_type"] != "SCORE_SOURCE"]
    data["connections"].append({"id": "to-factory", "a": "A", "b": "C"})
    empty = make(data)
    view = CoreAPI(empty, ("germany",)).observe()
    move_initial(data, "germany-army-0", "C")
    occupied = make(data)
    client = CoreAPI(occupied, ("germany",))
    normal = profile()
    double = profile()
    double["scoring_units"]["vp"] = 2
    scores = []
    for weights, fixed in ((normal, 4), (double, 8)):
        evaluator = ScoreEvaluator(empty.rules, weights)
        routes = evaluator.economic_from_view(view).routes
        assert routes.capture_value("germany", "C") == fixed
        site, = evaluator.evaluate(client, "germany").score_points
        assert site["fixed_score"] == fixed
        assert site["value"] == site["fixed_score"] + site["long_term_score"]
        assert [r["delay"] for r in site["opportunities"] if r["fixed"]] == [0, 6]
        assert all(r["delay"] >= 12 for r in site["opportunities"] if not r["fixed"])
        scores.append(site["long_term_score"])
    assert scores[1] == pytest.approx(2 * scores[0]) and scores[0] > 0
