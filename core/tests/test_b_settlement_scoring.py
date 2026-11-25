import pytest

from conftest import card, make, move_initial
from qmg_core import CoreAPI, ScoreEvaluator
from qmg_core.ai_match_policy import CandidateView, RulePolicy, SeatPolicy
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


def paper_rank(processor, client, command):
    row, = RulePolicy(processor.rules, ScoreEvaluator(processor.rules, profile())).rank(client.observe(), [command])
    assert row["recruitment"]["complete"]
    return row["rule_score"]


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
    assert paper_rank(game, client, play) == 0
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
    assert paper_rank(game, client, play) == expected
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
    assert paper_rank(game, client, play) == 0
    assert rank(game, client, play)["map_delta"] == 0
    effects = [army, navy]
    if scenario == "closed-before-supply":
        effects.append({"type": "choose-unit", "filter": {"force": "self", "kind": "army", "spaces": ["B"]}})
    game, client, play = deploy(data, effects)
    assert "T" not in LegalActionGenerator(game.rules).targets(game.state, "germany", {"type": "BUILD_NAVY"})
    assert paper_rank(game, client, play) == expected
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


def test_a_keeps_the_checked_recruitment_location_for_a_later_build(data):
    map_with_sites(data, [("A", "S"), ("C", "T")], ("C",))
    data["settings"].update(free_recruit=True, supply_revision=2, allied_supply=True, naval_army_base=True,
                            mandatory_turn_play=True, straits=[{"land": "D", "seas": ["S", "T"]}])
    move_initial(data, "italy-army-0", "C")
    game, client, play = deploy(data, [
        {"type": "choose-space", "action": "recruit", "kind": "army", "spaces": ["B", "D"]},
        {"type": "build", "kind": "navy", "space": "T"},
    ])
    assert paper_rank(game, client, play) == 2
    evaluator = ScoreEvaluator(game.rules, profile())

    class NoDuplicateSearch:
        def run(self, *_args):
            pytest.fail("B should reuse the completed recruitment branch")

    command, comparison = SeatPolicy(game.rules, evaluator=evaluator, search=NoDuplicateSearch()).decide(client, "germany")
    assert command == play and comparison["selected_q"] == 2
    policy = SeatPolicy(game.rules, evaluator=evaluator)
    assert policy.decide(client, "germany")[0] == play
    client.apply(play)
    selected, log = policy.decide(client, "germany")
    assert selected["option"] == "D" and log["search_status"] == "matched-continuation"
    client.apply(selected)
    while game.state.decision:
        client.apply(policy.decide(client, "germany")[0])
    assert game.state.tokens["germany-navy-1"].position_id == "T"


def withdraw_at_turn_end(data, region):
    data["abilities"].append({"id": "withdraw-recruit", "activation_mode": "AUTO", "after": "origin", "priority": 10,
                              "effects": [instruction({"type": "choose-unit",
                                  "filter": {"force": "self", "kind": "army", "spaces": [region]}})],
                              "trigger": {"operation_type": "TURN_END", "actor": "self"}})
    card(data, "g-status")["abilities"] = ["withdraw-recruit"]
    move_initial(data, "g-status", "germany:STATUS")


def test_recruitment_counts_only_site_vp_actually_earned_before_turn_end_removal(data):
    map_with_sites(data, [("A", "C")], ("C",))
    data["settings"].update(free_recruit=True, supply_revision=2, mandatory_turn_play=True)
    withdraw_at_turn_end(data, "C")
    game, client, play = deploy(data, [{"type": "recruit", "kind": "army", "space": "C"}])
    assert paper_rank(game, client, play) == 2
    result = rank(game, client, play)
    assert result["q"] == result["temporary_vp"] == 2
    client.apply(play)
    while game.state.turn.country_index == 0:
        commands = client.legal_commands()
        client.apply(commands[0])
    assert game.state.scores["axis"] == 2
    assert game.state.tokens["germany-army-1"].position_id == zone("germany", "RESERVE_ARMY")


def test_second_recruitment_can_supply_the_first_before_cleanup(data):
    map_with_sites(data, [("A", "C"), ("C", "B")], ())
    data["settings"].update(free_recruit=True, supply_revision=2, mandatory_turn_play=True)
    game, client, play = deploy(data, [{"type": "recruit", "kind": "army", "space": region} for region in ("B", "C")])
    assert paper_rank(game, client, play) == 4
    assert rank(game, client, play)["map_delta"] == 4


def test_temporary_port_scores_its_surviving_navy(data):
    map_with_sites(data, [("A", "S"), ("S", "T"), ("B", "T")], ())
    data["settings"].update(free_recruit=True, supply_revision=2, naval_army_base=True, mandatory_turn_play=True)
    withdraw_at_turn_end(data, "B")
    game, client, play = deploy(data, [{"type": "recruit", "kind": "army", "space": "B"},
                                        {"type": "build", "kind": "navy", "space": "T"}])
    assert "T" not in LegalActionGenerator(game.rules).targets(game.state, "germany", {"type": "BUILD_NAVY"})
    assert paper_rank(game, client, play) == rank(game, client, play)["map_delta"] == 2
    client.apply(play)
    assert game.state.tokens["germany-navy-1"].position_id == "T"
    while game.state.turn.country_index == 0:
        client.apply(client.legal_commands()[0])
    assert game.state.tokens["germany-army-1"].position_id == zone("germany", "RESERVE_ARMY")
    assert game.state.tokens["germany-navy-1"].position_id == "T"
