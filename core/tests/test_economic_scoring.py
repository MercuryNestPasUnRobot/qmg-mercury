from copy import deepcopy

import pytest

from conftest import card, make
from qmg_core import CommandProcessor, CoreAPI
from qmg_core.ai_match_policy import RulePolicy, SeatPolicy
from qmg_core.content_effects import instruction
from qmg_core.evaluation import OneStepPlanner, ScoreEvaluator
from qmg_core.model import zone
from test_ai_match import profile
from test_hand_retention import hand_only
from test_reallocation import add_card


@pytest.fixture
def economy(data):
    data["settings"].update(mandatory_turn_play=True, max_rounds=20, victory_margin=30,
                            hand_limit=7, deck_exhaustion_vp=-1)
    return data


def test_guaranteed_vp_is_one_to_one_and_end_pressure_still_limits_the_horizon(economy):
    engine = make(economy)
    scorer = ScoreEvaluator(engine.rules, profile())
    initial = CoreAPI(engine, ("germany",)).observe(include_history=False)
    start = scorer.economic_from_view(initial)
    assert start.gaps("axis") == (30, 30)
    assert start.points("germany", 1, 0.75) == 1

    late = deepcopy(initial)
    late["turn"]["round_id"] = 20
    assert scorer.economic_from_view(late).points("germany", 1, 0.75) == 1
    for team in ("axis", "allies"):
        close = deepcopy(initial)
        close["scores"][team] = 29
        forecast = scorer.economic_from_view(close)
        assert forecast.gaps("axis") == ((1, 59) if team == "axis" else (59, 1))
        assert forecast.horizon == 2
        assert forecast.closes_game("germany", 1) is (team == "axis")
        assert forecast.points("germany", 1, 0.75, closeout=True) == 1


def test_milling_counts_only_added_shortfalls_and_checks_if_draws_will_happen(economy):
    engine = make(economy)
    scorer = ScoreEvaluator(engine.rules, profile())
    view = CoreAPI(engine, ("germany",)).observe(include_history=False)
    view["positions"][zone("united-kingdom", "HAND")]["count"] = 7
    deck = view["positions"][zone("united-kingdom", "DECK")]
    deck["count"] = 20
    forecast = scorer.economic_from_view(view)
    assert forecast.draws("united-kingdom") == 8
    assert forecast.mill("germany", "united-kingdom", 2, 0.75) == 0
    deck["count"] = 9
    assert forecast.mill("germany", "united-kingdom", 2, 0.75) == pytest.approx(0.60)
    deck["count"] = 0
    # Both penalties are immediate, with no future draw removed from an already empty deck.
    assert forecast.mill("germany", "united-kingdom", 2, 0.75) == 2
    assert forecast.mill("soviet-union", "united-kingdom", 2, 0.75) == -2

    deck["count"] = 9
    view["turn"]["round_id"] = 20
    late = scorer.economic_from_view(view)
    assert late.draws("united-kingdom") == 1
    assert late.mill("germany", "united-kingdom", 2, 0.75) == 0
    view["turn"].update(country_index=1, phase="Draw")
    assert scorer.economic_from_view(view).draws("united-kingdom") == 0


def test_normal_play_switches_from_development_to_a_closing_point(economy):
    card(economy, "g-vp")["effects"][0]["amount"] = 1
    hand_only(economy, "g-vp", "g-build")
    engine = make(economy)
    policy = SeatPolicy(engine.rules, evaluator=ScoreEvaluator(engine.rules, profile()))
    assert policy.decide(CoreAPI(engine, ("germany",)), "germany")[0]["card"] == "g-build"
    state = engine.state
    state.scores["axis"] = 29
    near_win = CommandProcessor(engine.rules, state)
    client = CoreAPI(near_win, ("germany",))
    chosen, _ = policy.decide(client, "germany")
    assert chosen["card"] == "g-vp"
    client.submit(chosen)
    assert near_win.state.winner == "axis"


def test_simulation_accounts_for_immediate_and_future_missing_cards_separately(economy):
    economy["settings"]["victory_margin"] = 0  # Keep the remaining turns fixed across the VP change.
    card(economy, "g-noop")["effects"] = [
        instruction({"type": "discard-top", "target": "united-kingdom", "count": 2})]
    hand_only(economy, "g-deck-2")
    engine = make(economy)
    client = CoreAPI(engine, ("germany",))
    planner = OneStepPlanner(engine.rules, profile())
    row = next(row for row in planner.rank(client, "germany") if row["command"]["type"] == "PLAY_CARD")
    assert row["complete"]
    assert row["actual_vp_delta"] == 1
    assert row["resource_delta"] == pytest.approx(0.60)
    client.submit(row["command"])
    assert engine.state.scores["allies"] == -1


def test_mill_target_ranking_and_privacy_use_only_public_counts(economy):
    card(economy, "g-noop")["effects"] = [instruction({
        "type": "choose-country", "side": "enemy", "action": "discard-top", "count": 2,
        "countries": ["united-kingdom", "soviet-union"]})]
    add_card(economy, "united-kingdom", "extra-private", "DECK")
    for n in range(20):
        add_card(economy, "soviet-union", f"private-{n}", "DECK")
    hand_only(economy, "g-deck-2")
    first = make(economy)
    alternate_state = first.state
    alternate_state.positions[zone("united-kingdom", "DECK")].reverse()
    second = CommandProcessor(first.rules, alternate_state)
    assert first.state.positions[zone("united-kingdom", "DECK")] != second.state.positions[zone("united-kingdom", "DECK")]
    policy = RulePolicy(first.rules, ScoreEvaluator(first.rules, profile()))
    rankings = []
    for engine in (first, second):
        client = CoreAPI(engine, ("germany",))
        client.submit(client.legal_commands()[0])
        view = client.observe(include_history=False)
        assert view["decision"]["deck_discard_count"] == 2
        assert view["positions"][zone("united-kingdom", "DECK")] == {"count": 2}
        assert CoreAPI(engine, ("japan",)).observe(include_history=False)["decision"] == {"required_country": "germany"}
        rankings.append(policy.rank(view, client.legal_commands()))
    assert rankings[0] == rankings[1]
    assert rankings[0][0]["command"]["option"] == "united-kingdom"
    assert rankings[0][0]["rule_score"] > rankings[0][1]["rule_score"]
