from copy import deepcopy

import pytest

from conftest import card, make, move_initial
from qmg_core import CoreAPI, ScoreEvaluator
from qmg_core.ai_match_policy import CandidateView, RulePolicy, SeatPolicy
from qmg_core.content_effects import instruction
from qmg_core.evaluation import OneStepPlanner
from qmg_core.model import zone
from test_action_distance_scoring import map_with_sites
from test_ai_match import profile
from test_hand_retention import hand_only


def response(data, trigger, effect, *, bolster=False, in_hand=False):
    source = card(data, "g-status")
    source.update(type="EVENT" if bolster else "RESPONSE", legacy_kind="bolster" if bolster else "response")
    ability = next(a for a in data["abilities"] if a["id"] == "g-extra")
    ability.clear()
    ability.update(id="g-extra", priority=10, activation_mode="OPTIONAL", after="delete",
                   trigger={"operation_type": "CARD_EVENT", "card_trigger": {"type": trigger}},
                   effects=[instruction(effect)])
    if bolster:
        ability["listener_scope"] = ["HAND", "RESPONSE"]
    move_initial(data, "g-status", zone("germany", "HAND" if in_hand else "RESPONSE"))
    return source


def evaluator(game):
    return ScoreEvaluator(game.rules, profile())


def choice(client, kind):
    command = next(c for c in client.legal_commands() if c["type"] == kind)
    client.apply(command)


def test_staging_scores_full_base_minus_standard_site_vp_per_turn(data):
    map_with_sites(data, [], ())
    hand_only(data)
    source = response(data, "play-phase-start", {"type": "points", "amount": 5, "side": "self"}, in_hand=True)
    source["content_condition"] = {"type": "unit-count", "min": 1,
                                   "filter": {"kind": "navy", "force": "self", "spaces": ["T"]}}
    rejected = make(data)
    assert evaluator(rejected).economic_from_view(CoreAPI(rejected, ("germany",)).observe()).responses.staging(
        "germany", "g-status")["activations"] == 0
    source.pop("content_condition")
    game = make(data)
    client = CoreAPI(game, ("germany",))
    scorer = evaluator(game)
    policy = RulePolicy(game.rules, scorer)
    forecast = scorer.economic_from_view(client.observe()).responses
    assert forecast.staging("germany", "g-status") == {
        "base": 5, "wait_turns": 1, "time_cost": 2, "net": 3, "window_remaining": True, "activations": 1}
    assert forecast.time_cost(2) == 4
    assert policy.card_value(game.rules.cards["g-status"], "germany", forecast.economy) == 3


def test_two_consecutive_battles_cannot_score_the_same_victim_twice(data):
    map_with_sites(data, [("A", "B")], ())
    move_initial(data, "united-kingdom-army-0", "B")
    effect = {"type": "battle", "kind": "army", "spaces": ["B"]}
    response(data, "victory-start", effect)
    next(a for a in data["abilities"] if a["id"] == "g-extra")["effects"].append(instruction(effect))
    game = make(data)
    forecast = evaluator(game).economic_from_view(CoreAPI(game, ("germany",)).observe()).responses
    assert forecast.staging("germany", "g-status")["base"] == 2


def test_single_use_basic_response_gets_one_legal_opening_without_the_trigger_card(data):
    map_with_sites(data, [("A", "B")], ())
    hand_only(data)
    response(data, "after-build", {"type": "points", "amount": 4, "side": "self"}, in_hand=True)
    ability = next(a for a in data["abilities"] if a["id"] == "g-extra")
    trigger = ability["trigger"]["card_trigger"]
    trigger.update(kind="army", builder="self", spaces=["B"])
    game = make(data)
    forecast = evaluator(game).economic_from_view(CoreAPI(game, ("germany",)).observe()).responses
    assert forecast.staging("germany", "g-status")["activations"] == 1
    assert forecast.staging("germany", "g-status")["base"] == 4
    trigger["spaces"] = ["E"]
    game = make(data)
    forecast = evaluator(game).economic_from_view(CoreAPI(game, ("germany",)).observe()).responses
    assert forecast.staging("germany", "g-status")["activations"] == 0


def test_nonbasic_economic_trigger_requires_a_visible_playable_card(data):
    hand_only(data)
    response(data, "economic-play", {"type": "points", "amount": 4, "side": "self"}, bolster=True, in_hand=True)
    card(data, "g-noop")["legacy_kind"] = "economic"
    for present in (False, True):
        move_initial(data, "g-deck-2", zone("germany", "HAND" if present else "DECK"))
        game = make(data)
        forecast = evaluator(game).economic_from_view(CoreAPI(game, ("germany",)).observe()).responses
        assert forecast.staging("germany", "g-status")["activations"] == int(present)


def test_remote_response_recruit_uses_shifted_position_without_supply_proof(data):
    map_with_sites(data, [("B", "C")], ("C",))
    data["settings"]["free_recruit"] = True
    hand_only(data)
    response(data, "any-time", {"type": "recruit", "kind": "army", "space": "B"})
    game = make(data)
    weights = profile()
    weights["scoring_units"]["supply_stability"] = 0
    economy = ScoreEvaluator(game.rules, weights).economic_from_view(CoreAPI(game, ("germany",)).observe())
    routes = economy.routes
    assert not routes.placement_supplied("germany", "B")
    shifted = routes.position.value("germany", "B", construction=True, distance_offset=1) + routes.placement_stability("germany", "B")
    assert shifted > 0
    assert economy.responses.staging("germany", "g-status")["base"] == shifted
    client = CoreAPI(game, ("germany",))
    activate = next(c for c in client.legal_commands() if c["type"] == "ACTIVATE_CARD")
    planned, = OneStepPlanner(game.rules, weights).rank(CandidateView(client.observe(), [activate]), "germany")
    assert planned["complete"] and planned["map_delta"] == pytest.approx(shifted)
    move_initial(data, "united-kingdom-army-1", "B")
    game = make(data)
    forecast = evaluator(game).economic_from_view(CoreAPI(game, ("germany",)).observe()).responses
    assert forecast.staging("germany", "g-status")["base"] == 0


@pytest.mark.parametrize("bolster", [False, True])
def test_zero_vp_response_window_is_declined_and_card_is_preserved(data, bolster):
    map_with_sites(data, [], ())
    hand_only(data)
    response(data, "during-victory", {"type": "points-per-unit", "side": "self", "amountPer": 1,
              "filter": {"force": "self", "kind": "army", "spaces": ["B"]}}, bolster=bolster, in_hand=bolster)
    game = make(data)
    client = CoreAPI(game, ("germany",))
    while not client.observe()["decision"]:
        choice(client, "END_PHASE")
    command, row = SeatPolicy(game.rules, evaluator=evaluator(game)).decide(client, "germany")
    assert command["option"] == "decline"
    use = next(r for r in row["rule_scores"] if r["command"].get("option") == "use")
    assert use["response_forecast"] == {"immediate": 0, "wait": None}
    client.apply(command)
    assert game.state.tokens["g-status"].position_id == zone("germany", "HAND" if bolster else "RESPONSE")


@pytest.mark.parametrize("visible_build,amount,option", [(True, 3, "decline"), (True, 2, "use"), (False, 3, "use")])
def test_wait_only_for_a_better_net_gain_from_a_card_already_in_own_hand(data, visible_build, amount, option):
    map_with_sites(data, [("A", "B")], ())
    hand_only(data, *("g-build",) if visible_build else ())
    response(data, "during-victory", {"type": "points-per-unit", "side": "self", "amountPer": amount,
              "filter": {"force": "self", "kind": "army", "spaces": ["A", "B"]}})
    other = deepcopy(data)
    other["initial_tokens"].reverse()
    answers = []
    for setup in (data, other):
        game = make(setup)
        client = CoreAPI(game, ("germany",))
        while not client.observe()["decision"]:
            choice(client, "END_PHASE")
        command, row = SeatPolicy(game.rules, evaluator=evaluator(game)).decide(client, "germany")
        assert command["option"] == option
        estimate = next(r["response_forecast"] for r in row["rule_scores"] if r["command"]["option"] == "use")
        assert estimate["immediate"] == amount
        assert estimate["wait"] == ({"card": "g-build", "target": "B", "base": 6, "wait_turns": 1,
                                     "time_cost": 2, "net": 4} if option == "decline" else None)
        answers.append(estimate)
    assert answers[0] == answers[1]


def test_next_build_response_waits_one_turn_not_two(data):
    map_with_sites(data, [("A", "B"), ("B", "C")], ())
    hand_only(data, "g-build")
    data["initial_tokens"].append({"token_id": "g-build-later", "definition_id": "g-build", "token_type": "CARD",
                                  "owner_country": "germany", "position_id": zone("germany", "HAND")})
    response(data, "after-build", {"type": "points-per-unit", "side": "self", "amountPer": 3,
              "filter": {"force": "self", "kind": "army", "spaces": ["B", "C"]}})
    next(a for a in data["abilities"] if a["id"] == "g-extra")["trigger"]["card_trigger"].update(
        kind="army", builder="self")
    game = make(data)
    client = CoreAPI(game, ("germany",))
    play = next(c for c in client.legal_commands() if c.get("card") == "g-build")
    client.apply(play)
    choice(client, "CHOOSE")
    command, row = SeatPolicy(game.rules, evaluator=evaluator(game)).decide(client, "germany")
    assert command["option"] == "decline"
    estimate = next(r["response_forecast"] for r in row["rule_scores"] if r["command"]["option"] == "use")
    assert estimate["immediate"] == 3
    assert estimate["wait"] == {"card": "g-build-later", "target": "C", "base": 6, "wait_turns": 1,
                                 "time_cost": 2, "net": 4}


def test_forecast_skips_unrelated_card_that_requires_an_opponents_hidden_hand(data):
    map_with_sites(data, [("A", "B")], ())
    hand_only(data, "g-build", "g-deck-2")
    move_initial(data, "uk-hidden", zone("united-kingdom", "HAND"))
    card(data, "g-noop")["effects"] = [instruction({
        "type": "choose-one", "options": [{"label": "uncertain",
            "effects": [{"type": "play-from-zone", "zone": "hand", "target": "united-kingdom"}]}]})]
    response(data, "during-victory", {"type": "points-per-unit", "side": "self", "amountPer": 3,
              "filter": {"force": "self", "kind": "army", "spaces": ["A", "B"]}})
    game = make(data)
    client = CoreAPI(game, ("germany",))
    while not client.observe()["decision"]:
        choice(client, "END_PHASE")
    command, row = SeatPolicy(game.rules, evaluator=evaluator(game)).decide(client, "germany")
    assert command["option"] == "decline"
    assert any(r.get("response_forecast", {}).get("wait", {}).get("card") == "g-build" for r in row["rule_scores"])


def test_after_card_use_forecast_cannot_play_an_opponents_hidden_hand(data):
    hand_only(data, "g-deck-2")
    move_initial(data, "uk-hidden", zone("united-kingdom", "HAND"))
    card(data, "g-noop")["effects"] = [instruction({
        "type": "choose-one", "options": [{"label": "借用手牌",
            "effects": [{"type": "play-from-zone", "zone": "hand", "target": "united-kingdom"}]}]})]
    response(data, "after-card-use", {"type": "points", "amount": 3, "side": "self"}, in_hand=True)
    game = make(data)
    scorer = evaluator(game)
    hidden_view = CoreAPI(game, ("germany",)).observe()
    visible_view = CoreAPI(game, ("germany", "united-kingdom")).observe()
    hidden = scorer.economic_from_view(hidden_view)
    visible = scorer.economic_from_view(visible_view)
    assert hidden.responses.staging("germany", "g-status")["activations"] == 0
    assert visible.responses.staging("germany", "g-status")["base"] == 3
    policy = RulePolicy(game.rules, scorer)
    candidate = game.rules.cards["g-noop"]
    assert policy.discard_value(candidate, "germany", hidden, hidden_view) > policy.discard_value(
        candidate, "germany", visible, visible_view)
