import pytest

from conftest import card, make, move_initial
from qmg_core import CoreAPI, ScoreEvaluator, ScoreResolver
from qmg_core.ai_match_policy import CandidateView, RulePolicy
from qmg_core.content_effects import instruction
from qmg_core.evaluation import OneStepPlanner
from qmg_core.model import zone
from test_action_distance_scoring import forecast, map_with_sites
from test_ai_match import forecast_search_profile, profile
from test_hand_retention import hand_only
from test_status_scoring import scoring_status


def line(data, sites=(), rounds=10):
    map_with_sites(data, [("A", "B"), ("B", "C"), ("C", "D"), ("D", "E")], sites, rounds=rounds)


def test_one_vp_occupation_is_half_a_regular_site_even_on_an_unmarked_region(data):
    ability = scoring_status(data)
    line(data)
    card(data, "g-status")["content_condition"] = {
        "type": "unit-count", "min": 1, "filter": {"force": "self", "kind": "army", "spaces": ["C"]}}
    move_initial(data, "germany-army-0", "B")
    move_initial(data, "germany-army-1", "A")
    move_initial(data, "g-status", zone("germany", "STATUS"))
    game = make(data)
    client = CoreAPI(game, ("germany",))
    evaluator = ScoreEvaluator(game.rules, profile())
    economy = evaluator.economic_from_view(client.observe())
    assert economy.routes.position.capture("germany", "C", 2) == 6
    assert economy.statuses.portfolio("germany", True) == {(0, "during-victory", "regional", None): 1}
    assert economy.routes.target_value("germany", "C", construction=True) == 2
    assert economy.routes.target_value("italy", "C", construction=True) == 0
    build = next(c for c in client.legal_commands() if c.get("card") == "g-build")
    result, = OneStepPlanner(game.rules, forecast_search_profile()).rank(CandidateView(client.observe(), [build]), "germany")
    assert result["complete"] and result["continuations"][0]["option"] == "C"
    assert result["actual_vp_delta"] == 0
    assert result["card_delta"] > 0

    move_initial(data, "germany-army-0", "C")
    held = forecast(data).statuses
    assert held.report("germany", "g-status")["gross"] == 3
    assert held.program("germany", "germany", ability["id"], None, committed=True) == 1


def test_alternative_regions_and_each_occupied_region_use_printed_multiplicity(data):
    ability = scoring_status(data)
    line(data)
    move_initial(data, "germany-army-0", "C")
    move_initial(data, "germany-army-1", "D")
    card(data, "g-status")["content_condition"] = {
        "type": "unit-count", "min": 1, "filter": {"force": "self", "kind": "army", "spaces": ["C", "D"]}}
    assert forecast(data).statuses.report("germany", "g-status")["future"] == 3
    card(data, "g-status").pop("content_condition")
    ability["effects"] = [instruction({"type": "points-per-occupied-space", "occupiedBy": "self",
                                       "side": "self", "amountPer": 1, "spaces": ["C", "D"]})]
    assert forecast(data).statuses.report("germany", "g-status")["future"] == 5


def test_score_bonus_and_loss_use_proportion_and_only_affected_seats(data):
    ability = scoring_status(data, phase="before-rule-use")
    ability.update(effects=[], activation_mode="AUTO")
    line(data, ("C",))
    modifier = {"kind": "score_bonus", "site": "C", "amount": -2, "team": "allies", "scope": "all"}
    card(data, "g-status")["modifiers"] = [modifier]
    move_initial(data, "united-kingdom-army-0", "C")
    assert forecast(data).statuses.report("germany", "g-status")["future"] == 6
    move_initial(data, "g-status", zone("germany", "STATUS"))
    game = make(data)
    assert ScoreResolver(game.rules).sites(game.state, "united-kingdom") == {"C": 0}
    position = forecast(data).routes.position
    assert position.site_base("united-kingdom", "C", "C") == 0
    assert position.site_base("germany", "C", "C") > 0
    modifier["amount"] = 1
    assert forecast(data).routes.position.site_base("united-kingdom", "C", "C") == 9


def test_free_look_prices_one_window_and_uses_the_standard_cycling_value(data):
    ability = scoring_status(data)
    line(data, rounds=5)
    hand_only(data, "g-deck-2")
    move_initial(data, "g-status", zone("germany", "STATUS"))
    ability["effects"] = [instruction({"type": "order-deck-top", "count": 1})]
    clock = forecast(data).statuses
    assert sum(clock.portfolio("germany", True).values()) == 1
    assert len(clock.portfolio("germany", True)) == 1
    ability["effects"][0]["effect"]["count"] = 2
    assert sum(forecast(data).statuses.portfolio("germany", True).values()) == 2


@pytest.mark.parametrize("effect,expected", [
    (instruction({"type": "order-deck-top", "count": 1}), 1),
    ({"type": "TRANSFER", "token": "g-build", "destination": "germany:DECK"}, 1),
])
def test_instant_look_and_card_return_score_in_b_simulation(data, effect, expected):
    line(data)
    card(data, "g-noop")["effects"] = [effect]
    hand_only(data, "g-deck-2")
    move_initial(data, "g-build", zone("germany", "DISCARD"))
    game = make(data)
    client = CoreAPI(game, ("germany",))
    play = next(c for c in client.legal_commands() if c.get("card") == "g-deck-2")
    weights = profile()
    view = client.observe()
    paper, = RulePolicy(game.rules, ScoreEvaluator(game.rules, weights)).rank(view, [play])
    searched, = OneStepPlanner(game.rules, weights).rank(CandidateView(view, [play]), "germany")
    assert paper["rule_score"] == pytest.approx(expected - 2)
    evaluator = ScoreEvaluator(game.rules, weights)
    assert evaluator.retention(view, "germany")["g-deck-2"]["short_term"] == pytest.approx(expected)
    assert searched["card_event_score"] == pytest.approx(expected)
    assert searched["actual_vp_delta"] == 0


def test_support_proportions_use_current_action_and_site_scores(data):
    line(data, ("C",))
    economic = forecast(data)
    assert economic.statuses.support_value("germany", "C") == pytest.approx(0.55 * 2 + 0.65 * 6)
    assert economic.statuses.support_value("germany", "A", battle=True) == pytest.approx(0.425 * 2)
