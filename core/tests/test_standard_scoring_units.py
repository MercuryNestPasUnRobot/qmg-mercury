import pytest

from conftest import card, make, move_initial
from qmg_core import CoreAPI, ScoreEvaluator
from qmg_core.ai_match_policy import CandidateView, RulePolicy
from qmg_core.content_effects import instruction
from qmg_core.definitions import PER_UNIT_SITE
from qmg_core.evaluation import OneStepPlanner
from test_action_distance_scoring import map_with_sites
from test_ai_match import fixed_capture_profile, profile
from test_air_and_content_actions import add_air, end_phase, marker
from test_hand_retention import hand_only


def test_a_and_b_share_the_units_for_vp_cards_and_actions(data):
    engine = make(data)
    client = CoreAPI(engine, ("germany",))
    weights = profile()
    weights["action_opportunity_scoring"] = False
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
    assert blank["card_delta"] == pytest.approx(0)
    assert "initial_card_score" not in blank
    points = ranked["g-vp"]
    assert points["actual_vp_delta"] > 0
    assert points["vp_score_delta"] == 2 * points["actual_vp_delta"]
    assert points["q"] == pytest.approx(sum(points[key] for key in
                                          ("vp_score_delta", "map_delta", "card_delta", "resource_delta", "action_cost")))
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
    normal = fixed_capture_profile()
    double = fixed_capture_profile()
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


def score_play(game, client, command, weights):
    view = client.observe(include_history=False)
    evaluator = ScoreEvaluator(game.rules, weights)
    paper, = RulePolicy(game.rules, evaluator).rank(view, [command])
    settled, = OneStepPlanner(game.rules, weights).rank(CandidateView(view, [command]), "germany")
    assert settled["complete"]
    return paper, settled


def test_compound_play_pays_one_opportunity_and_its_choice_pays_none(data):
    map_with_sites(data, [], ())
    effects = [{"type": "points", "amount": amount, "side": "self"} for amount in (1, 2, 3)]
    card(data, "g-draw")["effects"] = [instruction(effects[0]), instruction({"type": "choose-one", "options": [
        {"label": "两个效果", "effects": effects[1:]}, {"label": "单个效果", "effects": effects[:1]}]})]
    hand_only(data, "g-draw")
    game = make(data)
    client = CoreAPI(game, ("germany",))
    weights = profile()
    weights["scoring_units"]["action"] = 3
    command = next(c for c in client.legal_commands() if c.get("card") == "g-draw")
    paper, settled = score_play(game, client, command, weights)
    assert paper["rule_score"] == settled["q"] == 3
    assert paper["action_cost"] == settled["action_cost"] == -3
    client.apply(command)
    for choice in RulePolicy(game.rules, ScoreEvaluator(game.rules, weights)).rank(client.observe(), client.legal_commands()):
        assert "action_cost" not in choice


def test_extra_draw_uses_the_same_nonrecursive_hand_average_for_a_and_b(data):
    map_with_sites(data, [], ())
    card(data, "g-draw")["effects"] = [{"type": "DRAW", "count": 2}]
    card(data, "g-vp")["effects"] = [{"type": "GAIN_VP", "amount": 1}]
    hand_only(data, "g-draw", "g-vp")
    game = make(data)
    client = CoreAPI(game, ("germany",))
    command = next(c for c in client.legal_commands() if c.get("card") == "g-draw")
    paper, settled = score_play(game, client, command, profile())
    assert paper["rule_score"] == settled["q"] == -1  # Two draws at 0.5 each, minus one action.
    assert settled["card_delta"] == 1
    assert settled["card_event_score"] == -1  # The two new cards already entered B's card stock.


def test_construction_ignores_legacy_action_and_hand_bonuses(data):
    map_with_sites(data, [("A", "B"), ("A", "S"), ("S", "T")], ())
    weights = profile()
    weights["card_value_weights"].update(BUILD_ARMY=14, BUILD_NAVY=27)
    for kind in ("army", "navy"):
        card(data, "g-draw")["effects"] = [instruction({"type": "build", "kind": kind, "space": "B" if kind == "army" else "T"})]
        hand_only(data, "g-draw")
        game = make(data)
        client = CoreAPI(game, ("germany",))
        command = next(c for c in client.legal_commands() if c.get("card") == "g-draw")
        evaluator = ScoreEvaluator(game.rules, weights)
        assert evaluator.retention(client.observe(), "germany")["g-draw"]["short_term"] == 0
        paper, settled = score_play(game, client, command, weights)
        assert paper["rule_score"] == settled["q"] == -2
        assert settled["map_delta"] == 0


def test_redeploy_prices_the_improvement_from_the_units_original_position(data):
    map_with_sites(data, [("A", "B"), ("B", "C"), ("C", "E")])
    next(region for region in data["regions"] if region["id"] == "B")["supply_source_for"] = "germany"
    move_initial(data, "germany-army-1", "B")
    card(data, "g-draw")["effects"] = [instruction({"type": "redeploy-armies", "count": 1})]
    hand_only(data, "g-draw")
    game = make(data)
    client = CoreAPI(game, ("germany",))
    weights = profile()
    evaluator = ScoreEvaluator(game.rules, weights)
    routes = evaluator.economic_from_view(client.observe()).routes
    improvement = routes.position.value("germany", "C", excluded="germany-army-0") - routes.position.value(
        "germany", "A", excluded="germany-army-0")
    command = next(c for c in client.legal_commands() if c.get("card") == "g-draw")
    paper, settled = score_play(game, client, command, weights)
    assert improvement > 0 and paper["rule_score"] == improvement - 2
    assert settled["action_cost"] == -2 and settled["continuations"][-1]["option"] == "C"
    client.apply(command)
    policy = RulePolicy(game.rules, evaluator)
    select_unit = policy.rank(client.observe(), client.legal_commands())[0]
    assert select_unit["command"]["option"] == "germany-army-0" and "action_cost" not in select_unit
    client.apply(select_unit["command"])
    select_target = policy.rank(client.observe(), client.legal_commands())[0]
    assert select_target["command"]["option"] == "C" and select_target["rule_score"] == improvement


def test_air_uses_position_value_and_pays_once_without_earning_site_vp(data):
    map_with_sites(data, [("A", "B"), ("B", "E")], ("A",))
    add_air(data)
    marker(data, "air-opportunity-germany", "germany")
    card(data, "g-draw").update(legacy_kind="air-power", effects=[])
    hand_only(data, "g-draw")
    game = make(data)
    client = CoreAPI(game, ("germany",))
    end_phase(game)
    command = next(c for c in client.legal_commands() if c["type"] == "USE_AIR")
    paper, settled = score_play(game, client, command, profile())
    assert paper["action_cost"] == settled["action_cost"] == -2
    assert settled["map_delta"] > 0
    assert paper["rule_score"] == settled["q"] == settled["map_delta"] - 2
    assert settled["actual_vp_delta"] == 0


def test_b_preserves_each_units_half_site_value_without_awarding_unit_stock(data):
    map_with_sites(data, [], ("A",), rounds=4)
    data["score_sites"][0].update(vp=1, scoring=PER_UNIT_SITE)
    data["settings"]["supply_revision"] = 2
    values = []
    for region in ("H", "A"):
        move_initial(data, "italy-army-0", region)
        game = make(data)
        evaluator = ScoreEvaluator(game.rules, profile())
        values.append(evaluator.settlement.value(game, "germany").map_value)
    assert values == [4, 8]
