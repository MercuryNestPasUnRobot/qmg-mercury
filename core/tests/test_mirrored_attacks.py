from copy import deepcopy

import pytest

from conftest import card, make, move_initial
from qmg_core import CoreAPI
from qmg_core.ai_match_policy import RulePolicy
from qmg_core.evaluation import ScoreEvaluator
from test_ai_match import profile


def routes(engine, weights):
    evaluator = ScoreEvaluator(engine.rules, weights)
    client = CoreAPI(engine, ("germany",))
    return evaluator.economic_from_view(client.observe()).routes


def test_removal_uses_defenders_build_routes_and_does_not_count_the_target_itself(data):
    move_initial(data, "united-kingdom-army-0", "B")
    move_initial(data, "g-status", "germany:STATUS")
    card(data, "g-status")["modifiers"] = [{"kind": "connection", "a": "B", "b": "C", "enabled": False}]
    data["initial_tokens"] = [t for t in data["initial_tokens"] if t["token_type"] != "SCORE_SOURCE"]
    data["score_sites"] = [site for site in data["score_sites"] if site["id"] == "factory"]
    weights = profile()
    live = routes(make(data), weights)
    no_victim = deepcopy(data)
    move_initial(no_victim, "united-kingdom-army-0", "united-kingdom:RESERVE_ARMY")
    vacant = routes(make(no_victim), weights)
    defender_value = vacant.target_value("united-kingdom", "B", construction=True, local_only=True)
    assert defender_value > vacant.target_value("germany", "B", construction=True, local_only=True)
    assert live.removal_value("united-kingdom-army-0", local_only=True) == pytest.approx(defender_value)
    assert live.paper_bonus("germany", "B", battle=True) == pytest.approx(
        vacant.paper_bonus("united-kingdom", "B"))


@pytest.mark.parametrize("victim", [False, True])
def test_land_battle_matches_army_base_only_when_it_can_remove_enemy(data, victim):
    if victim:
        move_initial(data, "united-kingdom-army-0", "B")
    p = make(data)
    weights = profile()
    evaluator = ScoreEvaluator(p.rules, weights)
    view = CoreAPI(p, ("germany",)).observe()
    policy = RulePolicy(p.rules, evaluator)
    economy = evaluator.economic_from_view(view)
    bonus = economy.routes.paper_bonus("germany", "B", battle=True)
    value = policy.play_card_value(p.rules.cards["g-battle"], "germany", economy)
    assert value == pytest.approx((1.6 if victim else 1.3) + bonus - evaluator.hand_slot_cost)
    assert bonus > 0 if victim else bonus == 0
