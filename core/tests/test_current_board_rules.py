import pytest

from conftest import card, finish_choices, make, move_initial, play
from qmg_core import CommandProcessor, CoreAPI, RuleSet, ScoreEvaluator
from qmg_core.api import simulation_from_observation
from qmg_core.map_variants import match_rules
from qmg_core.model import TURN_ORDER, ZoneRegistry, zone
from qmg_core.operations import ScoreResolver
from test_action_distance_scoring import map_with_sites
from test_ai_match import profile
from test_hand_retention import hand_only, reach_discard
from test_reallocation import execute


@pytest.mark.parametrize("lead, winner", [(30, None), (31, "axis")])
def test_victory_threshold_waits_for_the_last_turn_of_the_round(data, lead, winner):
    data["settings"].update(max_rounds=3, victory_margin=30, deck_exhaustion_vp=0)
    for site in data["score_sites"]:
        site["vp"] = 0
    card(data, "g-vp")["effects"][0]["amount"] = lead
    hand_only(data, "g-vp")
    game = CommandProcessor.create(match_rules(RuleSet(data)))
    play(game, "g-vp")
    assert game.state.scores["axis"] == lead and game.state.winner is None

    for seat in TURN_ORDER:
        client = CoreAPI(game, (seat,))
        while game.state.turn.phase != "Draw":
            finish_choices(game)
            execute(client, "END_PHASE")
        assert game.state.winner is None
        execute(client, "END_PHASE")
        if seat != TURN_ORDER[-1]:
            assert game.state.winner is None and game.state.turn.round_id == 1
    assert game.state.scores["axis"] - game.state.scores["allies"] == lead
    assert game.state.winner == winner
    outcome = game.records[-1]["outcome"]
    if winner:
        assert outcome["event"] == "GAME_END" and outcome["context"]["reason"] == "victory_margin"
    else:
        assert game.state.turn.round_id == 2


def test_soviet_homeland_moves_the_scoring_block_and_a_b_position_bonus(data):
    map_with_sites(data, [("G", "B"), ("D", "B"), ("A", "G"), ("A", "D")], ("B",))
    data["settings"].update(occupied_home_blocks_scoring=True, supply_revision=2)
    data["score_sites"].append({**data["score_sites"][0], "id": "half-B", "vp": 1, "scoring": "per-unit"})
    data["auxiliary_forces"] = [{"id": "fixture-auxiliary", "controller": "germany"}]
    next(place for place in data["regions"] if place["id"] == "C")["home_country"] = "fixture-auxiliary"
    next(place for place in data["regions"] if place["id"] == "D")["supply_source_for"] = "germany"
    move_initial(data, "soviet-union-army-0", "B")
    move_initial(data, "germany-army-0", "G")
    data["cards"].append({**card(data, "g-vp"), "id": "new-capital", "country": "soviet-union", "type": "STATUS",
                          "effects": [], "modifiers": [{"kind": "home", "scope": "all", "force": "soviet-union", "region": "D"}]})
    data["initial_tokens"].append({"token_id": "new-capital", "token_type": "CARD", "definition_id": "new-capital",
                                   "owner_country": "soviet-union", "position_id": zone("soviet-union", "HAND")})
    game = make(data)
    rules = game.rules
    scorer = ScoreResolver(rules)
    evaluator = ScoreEvaluator(rules, profile())
    assert scorer.sites(game.state, "soviet-union") == {}

    moved = game.state
    ZoneRegistry.transfer(moved, "new-capital", zone("soviet-union", "STATUS"))
    assert scorer.sites(moved, "soviet-union") == {"B": 2, "half-B": 1}
    before = evaluator.economic_from_state(rules, game.state).routes.position
    after = evaluator.economic_from_state(rules, moved).routes.position
    for side in ("soviet-union", "germany"):
        assert before.home_values(side, "G") == {"soviet-union": 2}
        assert after.home_values(side, "G") == {}
        assert after.home_values(side, "D") == {"soviet-union": 2}
        assert after.home_values(side, "C") == {"fixture-auxiliary": 1}

    ZoneRegistry.transfer(moved, "germany-army-0", "D")
    assert scorer.sites(moved, "soviet-union") == {}
    enemy_location = evaluator.economic_from_state(rules, moved).routes
    assert enemy_location.position.home_values("germany", "D") == {"soviet-union": 2}
    assert enemy_location.removal_value("germany-army-0") >= 4
    visible = CoreAPI(CommandProcessor(rules, moved), ("soviet-union",)).observe()
    projected = simulation_from_observation(rules, visible, search_projection=True)
    result = evaluator.settlement.value(projected, "soviet-union")
    assert {row["site"]: row["value"] for row in result.score_points}["home:soviet-union"] == -2


@pytest.mark.parametrize("discards", [False, True])
def test_discard_penalty_is_a_core_vp_transaction(data, discards):
    hand_only(data, "g-build")
    data["settings"].update(hand_limit=1, deck_exhaustion_vp=0)
    game = CommandProcessor.create(match_rules(RuleSet(data)))
    client = CoreAPI(game, ("germany",))
    reach_discard(client)
    before = game.state.scores["axis"]
    if discards:
        execute(client, "DISCARD_CARD")
    first = len(game.records)
    execute(client, "END_PHASE")
    penalty = [record["outcome"]["score_changes"] for record in game.records[first:]
               if record["operation"].get("reason") == "no-discard"]
    assert game.state.scores["axis"] == before - int(not discards)
    assert penalty == ([] if discards else [[{"team": "axis", "amount": -1}]])
