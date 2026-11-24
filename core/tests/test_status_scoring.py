"""Window accounting is exercised on shareable mechanisms and the optional local card set."""
from dataclasses import replace
from math import isfinite
from pathlib import Path

import pytest

from conftest import card, choose, make, move_initial
from qmg_core import CommandProcessor, CoreAPI, RuleSet
from qmg_core.ai_match_policy import RulePolicy, paper_play_choices
from qmg_core.api import simulation_from_observation
from qmg_core.content_effects import instruction
from qmg_core.evaluation import ScoreEvaluator
from qmg_core.model import TURN_ORDER, zone
from qmg_core.operations import outcome
from qmg_core.status_scoring import is_status
from test_ai_match import profile


def scoring_status(data, *, phase="during-victory", points=1):
    data["settings"].update(victory_margin=0, max_rounds=2)
    card(data, "g-status").update(legacy_kind="status")
    ability = next(a for a in data["abilities"] if a["id"] == "g-extra")
    ability.clear()
    ability.update(id="g-extra", priority=10, activation_mode="OPTIONAL", after="origin",
                   trigger={"operation_type": "CARD_EVENT", "card_trigger": {"type": phase}},
                   effects=[instruction({"type": "points", "amount": points, "side": "self"})])
    return ability


def view_forecast(processor, country="germany"):
    client = CoreAPI(processor, (country,))
    evaluator = ScoreEvaluator(processor.rules, profile())
    return evaluator.economic_from_view(client.observe(include_history=False)).statuses


def test_deployment_counts_only_windows_after_placement(data):
    scoring_status(data)
    engine = make(data)
    forecast = view_forecast(engine)
    initial = forecast.report("germany", "g-status")
    assert initial["windows"] == 2 and initial["future"] == pytest.approx(1.8)
    assert forecast.report("germany", "g-status", held=True)["future"] == pytest.approx(.8 * .55)
    client = CoreAPI(engine, ("germany",))
    policy = RulePolicy(engine.rules, ScoreEvaluator(engine.rules, profile()))
    row = next(r for r in policy.rank(client.observe(), client.legal_commands())
               if r["command"].get("card") == "g-status")
    assert row["status_forecast"] == initial
    assert row["rule_score"] == pytest.approx(1.8)

    scoring_status(data, phase="turn-start")
    forecast = view_forecast(make(data))
    assert forecast.report("germany", "g-status")["future"] == pytest.approx(.8)


def test_b_moves_settled_vp_out_of_future_value(data):
    scoring_status(data)
    data["settings"]["max_rounds"] = 1
    move_initial(data, "g-status", zone("germany", "STATUS"))
    engine = make(data)
    client = CoreAPI(engine, ("germany",))
    scorer = ScoreEvaluator(engine.rules, profile())
    before = sum(view_forecast(engine).portfolio("germany", True).values())
    assert before == 1
    for _ in range(8):
        if engine.state.decision:
            choose(engine, "use")
            break
        end = next(c for c in client.legal_commands() if c["type"] == "END_PHASE")
        client.submit(end)
    assert engine.state.turn.phase == "Victory"
    assert any(r["outcome"]["score_changes"] for r in engine.records)
    assert sum(view_forecast(engine).portfolio("germany", True).values()) == 0
    scored = scorer.evaluate(client, "germany")
    assert scored.actual_vp == engine.state.scores["axis"] - engine.state.scores["allies"]
    # Removing the already-settled status alone has zero residual card value.
    state = engine.state
    engine.executor.transfer(state, "g-status", zone("germany", "DISCARD"), outcome("TRANSFER"))
    comparison = CommandProcessor(engine.rules, state)
    assert scored.card_value == pytest.approx(scorer.evaluate(CoreAPI(comparison, ("germany",)), "germany").card_value)


def test_competing_primary_responses_count_only_the_better_choice(data):
    ability = scoring_status(data, points=1)
    move_initial(data, "g-status", zone("germany", "STATUS"))
    second = dict(ability, id="second-trigger",
                  effects=[instruction({"type": "points", "amount": 2, "side": "self"})])
    data["abilities"].append(second)
    data["cards"].append(dict(card(data, "g-status"), id="second", abilities=["second-trigger"]))
    forecast = view_forecast(make(data))
    report = forecast.report("germany", "second")
    assert report["gross"] == pytest.approx(3.6)
    assert report["competition"] == report["future"] == pytest.approx(1.8)


def test_hidden_order_and_opposing_hand_identities_do_not_change_paper_score(data):
    scoring_status(data)
    engine = make(data)
    state = engine.state
    state.positions[zone("germany", "DECK")].reverse()
    foreign_hand = state.positions[zone("united-kingdom", "HAND")]
    foreign_deck = state.positions[zone("united-kingdom", "DECK")]
    a, b = foreign_hand[0], foreign_deck[0]
    foreign_hand[0], foreign_deck[0] = b, a
    state.tokens[a] = replace(state.tokens[a], position_id=zone("united-kingdom", "DECK"))
    state.tokens[b] = replace(state.tokens[b], position_id=zone("united-kingdom", "HAND"))
    alternative = CommandProcessor(engine.rules, state)
    assert view_forecast(engine).report("germany", "g-status") == view_forecast(alternative).report("germany", "g-status")


def test_status_replacing_the_normal_play_can_win_paper_comparison():
    choices = [{"rule_score": score, "command": {"type": kind}} for kind, score in
               [("USE_STATUS", 3), ("PLAY_CARD", 1), ("END_PHASE", 0)]]
    assert paper_play_choices(choices, True) == choices[:1]


def test_regional_condition_only_contributes_if_its_base_covers_the_approach(data):
    scoring_status(data)
    card(data, "g-build")["legacy_kind"] = "build-army"
    card(data, "g-status")["content_condition"] = {
        "type": "unit-count", "min": 1, "filter": {"force": "self", "kind": "army", "spaces": ["B"]}}
    assert view_forecast(make(data)).report("germany", "g-status")["future"] == 0
    data["settings"]["max_rounds"] = 1
    assert view_forecast(make(data)).report("germany", "g-status")["future"] == 0
    move_initial(data, "germany-army-1", "B")
    assert view_forecast(make(data)).report("germany", "g-status")["future"] == 1


def test_supply_modifier_prices_only_new_supply_and_b_leaves_it_on_the_map(data):
    ability = scoring_status(data, phase="before-rule-use")
    ability.update(effects=[], activation_mode="AUTO")
    card(data, "g-status")["modifiers"] = [{"kind": "always_supplied", "force": "germany", "scope": "all"}]
    move_initial(data, "g-status", "germany:STATUS")
    move_initial(data, "germany-army-1", "D")
    forecast = view_forecast(make(data))
    supply, options = forecast.board_changes("germany", "g-status", "g-status")
    assert supply > 0
    assert forecast.modifier("germany", "germany", "g-status", "g-status", 0, False) == pytest.approx(supply + options)
    assert forecast.modifier("germany", "germany", "g-status", "g-status", 0, True) == pytest.approx(options)


def test_a_new_fleet_opens_a_land_target_for_the_same_build_response(data):
    ability = scoring_status(data)
    ability["trigger"]["card_trigger"] = {"type": "after-build", "builder": "self", "kind": "navy"}
    ability["effects"] = [instruction({"type": "battle", "kind": "army", "nearBuilt": True})]
    forecast = view_forecast(make(data))
    assert "E" not in forecast.targets("germany", "germany", "army", "battle")
    assert "T" in forecast.targets("germany", "germany", "navy", "build")
    assert forecast.program("germany", "germany", "g-extra", "T") > 0
    assert not forecast.state.positions["T"]  # Evaluating a card does not place the fleet.


def test_an_opponent_can_infer_spent_public_status_and_its_turn_end_reset(data):
    ability = scoring_status(data)
    ability.pop("after")
    ability["reset_policy"] = "TURN_END"
    move_initial(data, "g-status", "germany:STATUS")
    engine = make(data)
    client = CoreAPI(engine, ("germany",))
    for _ in range(8):
        if engine.state.decision:
            choose(engine, "use")
            break
        client.submit(next(c for c in client.legal_commands() if c["type"] == "END_PHASE"))
    view = CoreAPI(engine, ("united-kingdom",)).observe(include_history=False)
    assert view["public_status_uses"] == {"g-status": {"g-extra": 1}}
    assert view["positions"]["germany:ABILITY_READY"] == {"count": 0}
    sim = simulation_from_observation(engine.rules, view)
    assert not any(t.definition_id == "g-extra" and t.position_id == "germany:ABILITY_READY"
                   for t in sim.state.tokens.values())
    for _ in range(12):
        if sim.state.turn.country_index != 0:
            break
        commands = sim.legal_commands(("germany",))
        action = next((c for c in commands if c["type"] == "END_PHASE"), commands[0])
        sim.submit(action, ("germany",))
    assert any(t.definition_id == "g-extra" and t.position_id == "germany:ABILITY_READY"
               for t in sim.state.tokens.values())


def test_all_local_transcribed_statuses_have_a_finite_deploy_and_hold_value():
    catalog = Path("../local-content/baselines/board-reviewed-mandatory-play/content.sqlite3")
    if not catalog.is_file():
        pytest.skip("Local transcriptions are available only in the private test workspace")
    rules = RuleSet.load(catalog)
    engine = CommandProcessor.create(rules, seed=31)
    statuses = []
    for seat in TURN_ORDER:
        forecast = view_forecast(engine, seat)
        for definition in rules.cards.values():
            if definition["country"] != seat or not is_status(definition):
                continue
            for held in (False, True):
                assert isfinite(forecast.report(seat, definition["id"], held)["future"])
            statuses.append(definition["id"])
    assert len(set(statuses)) == 64
