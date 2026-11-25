import pytest

from conftest import card, make, move_initial
from qmg_core import CoreAPI, ScoreEvaluator
from qmg_core.ai_match_policy import CandidateView, RulePolicy
from qmg_core.content_effects import instruction
from qmg_core.evaluation import OneStepPlanner
from qmg_core.model import TURN_ORDER, zone
from qmg_core.rules import LegalActionGenerator, MapGraph, SupplyResolver
from qmg_core.status_scoring import StatusForecast
from test_action_distance_scoring import map_with_sites
from test_ai_match import profile
from test_hand_retention import hand_only


def deploy(data, effects):
    card(data, "g-noop")["effects"] = [instruction(effect) for effect in effects]
    hand_only(data, "g-deck-2")
    processor = make(data)
    client = CoreAPI(processor, ("germany",))
    command = next(c for c in client.legal_commands() if c.get("card") == "g-deck-2")
    return processor, client, command


def rank(processor, client, command):
    offered = CandidateView(client.observe(), [command])
    row, = OneStepPlanner(processor.rules, profile()).rank(offered, "germany")
    assert row["complete"]
    return row


def test_b_values_settlement_without_the_a_status_or_board_forecast(data, monkeypatch):
    map_with_sites(data, [("A", "B"), ("B", "C")], ())
    game, client, play = deploy(data, [{"type": "points", "amount": 1, "side": "self"}])

    def no_forecast(*_args, **_kwargs):
        raise AssertionError("B should score the settled position using its own evaluator")

    monkeypatch.setattr(StatusForecast, "portfolio", no_forecast)
    monkeypatch.setattr(ScoreEvaluator, "_value", no_forecast)
    row = rank(game, client, play)
    assert row["actual_vp_delta"] == row["q"] == 1
    assert row["card_delta"] == row["resource_delta"] == 0
    assert game.state.scores["axis"] == 0


def test_unsupplied_recruitment_gives_no_a_or_b_placement_credit(data):
    map_with_sites(data, [("B", "C"), ("C", "E")], ("C",))
    data["settings"].update(free_recruit=True, supply_revision=2, mandatory_turn_play=True)
    game, client, play = deploy(data, [{"type": "recruit", "kind": "army", "space": "B"}])
    a, = RulePolicy(game.rules, ScoreEvaluator(game.rules, profile())).rank(client.observe(), [play])
    assert a["rule_score"] == 0
    row = rank(game, client, play)
    assert row["map_delta"] == row["q"] == 0
    client.apply(play)
    assert game.state.tokens["germany-army-1"].position_id == "B"
    client.apply(next(c for c in client.legal_commands() if c["type"] == "END_PHASE"))
    assert game.state.tokens["germany-army-1"].position_id == zone("germany", "RESERVE_ARMY")


@pytest.mark.parametrize("mode,expected", [("AUTO", 2), ("OPTIONAL", 0)])
def test_b_needs_proof_that_an_unsupplied_recruit_survives(data, mode, expected):
    map_with_sites(data, [], ())
    data["settings"].update(free_recruit=True, mandatory_turn_play=True)
    data["abilities"].append({"id": "guard-at-supply", "activation_mode": mode, "after": "origin", "priority": 10,
                              "effects": [instruction({"type": "prevent-removal"})],
                              "trigger": {"operation_type": "CARD_EVENT", "card_trigger": {
                                  "type": "before-any-removal", "victims": ["germany"], "kind": "army"}}})
    card(data, "g-status")["abilities"] = ["guard-at-supply"]
    move_initial(data, "g-status", zone("germany", "STATUS"))
    game, client, play = deploy(data, [{"type": "recruit", "kind": "army", "space": "B"}])
    assert rank(game, client, play)["map_delta"] == expected
    client.apply(play)
    assert "germany-army-1" not in SupplyResolver(game.rules).supplied(game.state, "germany")
    while game.state.turn.country_index == 0:
        commands = client.legal_commands()
        client.apply(next(c for c in commands if c.get("option") == "use" or c["type"] == "END_PHASE"))
    assert game.state.tokens["germany-army-1"].position_id == "B"


@pytest.mark.parametrize("scenario,expected", [("independent-port", 2), ("open-at-supply", 2),
                                                ("closed-before-supply", 0)])
def test_strait_bonus_requires_a_completed_surviving_operation(data, scenario, expected):
    sites = ("C",) if scenario == "independent-port" else ()
    map_with_sites(data, [("A", "S"), ("C", "T")], sites)
    data["settings"].update(free_recruit=True, supply_revision=2, allied_supply=True, naval_army_base=True,
                            mandatory_turn_play=True, straits=[{"land": "B", "seas": ["S", "T"]}])
    move_initial(data, "italy-army-0", "C")
    army = {"type": "recruit", "kind": "army", "space": "B"}
    navy = {"type": "build", "kind": "navy", "space": "T"}
    game, client, play = deploy(data, [army])
    assert rank(game, client, play)["map_delta"] == 0
    effects = [army, navy]
    if scenario == "closed-before-supply":
        effects.append({"type": "choose-unit", "filter": {"force": "self", "kind": "army", "spaces": ["B"]}})
    game, client, play = deploy(data, effects)
    assert "T" not in LegalActionGenerator(game.rules).targets(game.state, "germany", {"type": "BUILD_NAVY"})
    assert rank(game, client, play)["map_delta"] == expected
    client.apply(play)
    assert game.state.tokens["germany-navy-1"].position_id == "T"
    assert "T" in MapGraph(game.rules).neighbors("S", game.state, "germany")
    while game.state.turn.country_index == 0:
        active = game.state.decision
        commands = game.legal_commands(TURN_ORDER)
        command = commands[0] if active else next(c for c in commands if c["type"] == "END_PHASE")
        game.submit(command, TURN_ORDER)
    assert game.state.tokens["germany-army-1"].position_id == zone("germany", "RESERVE_ARMY")
    assert game.state.tokens["germany-navy-1"].position_id == (
        "T" if expected else zone("germany", "RESERVE_NAVY"))
    assert "T" not in MapGraph(game.rules).neighbors("S", game.state, "germany")
    if scenario == "open-at-supply":
        assert "germany-navy-1" not in SupplyResolver(game.rules).supplied(game.state, "germany")
