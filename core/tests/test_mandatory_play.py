import pytest

from conftest import card, finish_choices, make
from qmg_core import CoreAPI, CoreError
from qmg_core.ai_match_policy import SeatPolicy
from qmg_core.evaluation import ScoreEvaluator
from qmg_core.model import zone
from test_ai_match import profile
from test_hand_retention import hand_only


def start(data, *hand):
    data["settings"].update(mandatory_turn_play=True, victory_margin=0)
    hand_only(data, *hand)
    processor = make(data)
    return processor, CoreAPI(processor, ("germany",))


def test_mandatory_play_forbids_skip_and_discard_until_card_is_played(data):
    p, client = start(data, "g-vp")
    assert [c["type"] for c in client.legal_commands()] == ["PLAY_CARD"]
    with pytest.raises(CoreError, match="illegal command"):
        client.submit({"type": "END_PHASE", "actor": "germany", "expected_version": p.state.version})
    client.submit(client.legal_commands()[0])
    finish_choices(p)
    assert p.state.turn.plays_remaining == 0
    assert [c["type"] for c in client.legal_commands()] == ["END_PHASE"]


def test_unplayable_hand_requires_discard_even_without_replacement(data):
    data["settings"]["hand_limit"] = 100
    card(data, "g-noop").update(legacy_kind="build-army", requirements=[{"kind": "phase", "phase": "Victory"}])
    p, client = start(data, "g-deck-2")
    before = p.state.scores["axis"]
    action, row = SeatPolicy(p.rules, evaluator=ScoreEvaluator(p.rules, profile())).decide(client, "germany")
    assert action["type"] == "DISCARD_CARD" and row["rule_scores"][0]["retention"]["forced"]
    retention = row["rule_scores"][0]["retention"]
    assert retention["slot_cost"] == 0 and retention["discard_cost"] == 1
    assert not row["rule_scores"][0]["retention"]["replacement_available"]
    client.submit(action)
    assert p.state.tokens["g-deck-2"].position_id == zone("germany", "DISCARD")
    assert p.state.scores["axis"] == before and p.state.turn.plays_remaining == 0
    with pytest.raises(CoreError, match="illegal command"):
        client.submit(action)
    assert [c["type"] for c in client.legal_commands()] == ["END_PHASE"]


def test_empty_hand_costs_exactly_one_vp_and_consumes_the_play(data):
    p, client = start(data)
    assert [c["type"] for c in client.legal_commands()] == ["FORFEIT_PLAY"]
    weights = profile()
    weights["scoring_units"]["vp"] = 2
    policy = SeatPolicy(p.rules, evaluator=ScoreEvaluator(p.rules, weights))
    action, row = policy.decide(client, "germany")
    assert row["rule_scores"][0]["rule_score"] == -4
    assert row["rule_scores"][0]["action_cost"] == -2
    client.submit(action)
    assert p.state.scores == {"axis": -1, "allies": 0}
    assert p.state.turn.plays_remaining == 0
    assert any(r["operation_type"] == "GAIN_VP" and r["outcome"]["score_changes"] == [{"team": "axis", "amount": -1}]
               for r in p.records)
    assert [c["type"] for c in client.legal_commands()] == ["END_PHASE"]


def test_ai_plays_the_highest_paper_card_despite_negative_simulation(data):
    card(data, "g-vp")["effects"][0]["amount"] = 1
    p, client = start(data, "g-vp", "g-build")
    evaluator = ScoreEvaluator(p.rules, profile())

    class Search:
        def run(self, view, commands, country, deadline):
            assert [(c["type"], c["card"]) for c in commands] == [("PLAY_CARD", "g-build")]
            return [{"command": commands[0], "complete": True, "q": -0.1,
                     "terminal_rank": 0, "initial_card_score": -0.55,
                     "continuations": [], "choice_signatures": []}], "finished"

    a, _ = SeatPolicy(p.rules, evaluator=evaluator).decide(client, "germany")
    b, decision = SeatPolicy(p.rules, evaluator=evaluator, search=Search()).decide(client, "germany")
    assert a == b and b["card"] == "g-build"
    assert decision["selected_q"] == -0.1
