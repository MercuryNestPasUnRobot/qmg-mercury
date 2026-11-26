import pytest

from conftest import card, make, move_initial
from qmg_core import CoreAPI, ScoreEvaluator
from qmg_core.ai_match_policy import CandidateView, RulePolicy
from qmg_core.api import simulation_from_observation
from qmg_core.content_effects import instruction
from qmg_core.evaluation import OneStepPlanner
from qmg_core.model import TURN_ORDER, zone
from qmg_core.operations import ScoreResolver
from test_ai_match import forecast_search_profile, profile
from test_hand_retention import hand_only, reach_discard
from test_status_scoring import scoring_status


def map_with_sites(data, edges, sites=("C",), *, rounds=10):
    data["settings"].update(max_rounds=rounds, victory_margin=0)
    data["connections"] = [{"id": f"edge-{i}", "a": a, "b": b} for i, (a, b) in enumerate(edges)]
    data["score_sites"] = [{"id": p, "region": p, "vp": 2, "countries": list(TURN_ORDER),
                            "requires_supply": False} for p in sites]
    data["initial_tokens"] = [t for t in data["initial_tokens"] if t["token_type"] != "SCORE_SOURCE"]


def forecast(data, seat="germany", weights=None):
    game = make(data)
    scorer = ScoreEvaluator(game.rules, weights or profile())
    view = CoreAPI(game, (seat,)).observe(include_history=False)
    return scorer.economic_from_view(view)


def test_capture_income_distance_and_remaining_round_cap(data):
    map_with_sites(data, [("A", "B"), ("B", "C"), ("C", "D"), ("C", "E")])
    position = forecast(data).routes.position
    assert position.capture("germany", "C", 2) == 4
    assert [position.value("germany", p) for p in ("C", "B", "A")] == [4, 2, 0]
    move_initial(data, "united-kingdom-army-1", "C")
    assert forecast(data).routes.position.capture("germany", "C", 2) == 4
    move_initial(data, "united-kingdom-army-1", zone("united-kingdom", "RESERVE_ARMY"))
    data["connections"][-1].update(a="D")
    position = forecast(data).routes.position
    assert position.capture("germany", "C", 2) == 6
    assert position.value("germany", "A", excluded="germany-army-0") == 2
    data["settings"]["max_rounds"] = 2
    assert forecast(data).routes.position.capture("germany", "C", 2) == 4


def test_configurable_action_value_changes_the_reach(data):
    map_with_sites(data, [("A", "B"), ("B", "C"), ("A", "S"), ("S", "T"), ("C", "E")])
    weights = profile()
    assert "C" not in forecast(data, weights=weights).routes.position.site_values("germany", "T")
    weights["scoring_units"]["action"] = 1
    position = forecast(data, weights=weights).routes.position
    assert position.site_values("germany", "T")["C"] == (4, 0)


def test_full_site_override_drives_half_sites_a_b_and_response_wait_without_changing_vp(data):
    map_with_sites(data, [("A", "E")], ("A",))
    data["score_sites"][0]["countries"] = ["germany"]
    data["score_sites"].append({**data["score_sites"][0], "id": "half", "vp": 1, "scoring": "per-unit"})
    data["settings"]["supply_revision"] = 2
    move_initial(data, "united-kingdom-army-0", "E")
    game = make(data)
    weights = profile()
    weights["scoring_units"].update(vp=3, action=7)
    weights["scoring_overrides"] = {"full_site_round": 5, "distance_step": 0}
    scorer = ScoreEvaluator(game.rules, weights)
    view = CoreAPI(game, ("germany",)).observe()
    economy = scorer.economic_from_view(view)
    position = economy.routes.position
    assert position.site_base("germany", "A", "A") == 10
    assert position.site_base("germany", "A", "half") == 5
    assert position.distance_value(10, 2) == 10
    assert economy.responses.time_cost(2) == 10
    projected = simulation_from_observation(game.rules, view, search_projection=True)
    income = {row["site"]: row["value"] for row in scorer.settlement.value(projected, "germany").score_points}
    assert income == {"A": 10, "half": 5}
    assert ScoreResolver(game.rules).sites(game.state, "germany") == {"A": 2, "half": 1}


def test_allied_unit_on_site_or_closer_along_path_is_charged_once(data):
    map_with_sites(data, [("A", "B"), ("B", "C"), ("B", "D"), ("C", "E"), ("C", "F"), ("F", "D")],
                   ("C", "D"))
    move_initial(data, "japan-army-0", zone("japan", "RESERVE_ARMY"))
    weights = {**profile(), "stacked_site_scoring": False, "regional_defense_scoring": False}
    original = forecast(data, weights=weights).routes.position
    assert original.site_values("germany", "A") == {"C": (2, 0), "D": (2, 4)}
    move_initial(data, "italy-army-0", "B")
    with_ally = forecast(data, weights=weights).routes.position
    assert with_ally.site_values("germany", "A") == {"C": (2, 0), "D": (2, 1)}
    move_initial(data, "italy-army-0", "C")
    with_ally = forecast(data, weights=weights).routes.position
    assert with_ally.site_values("germany", "C")["C"] == (0, 2)
    assert with_ally.site_values("germany", "B")["C"] == (1, 0)
    move_initial(data, "italy-army-0", "H")
    move_initial(data, "united-kingdom-army-1", "C")
    foe = forecast(data, weights=weights).routes.position
    # The UK army's own capture value is 6, giving it 3 influence on this route.
    assert foe.site_values("germany", "C")["C"] == (0, 7)
    assert foe.site_values("germany", "B")["C"] == (1, 5)


def test_both_a_and_b_credit_one_action_for_a_certain_removal(data):
    map_with_sites(data, [("A", "B")], ())
    move_initial(data, "united-kingdom-army-0", "B")
    game = make(data)
    client = CoreAPI(game, ("germany",))
    scorer = ScoreEvaluator(game.rules, profile())
    view = client.observe()
    battle = next(c for c in client.legal_commands() if c.get("card") == "g-battle")
    row, = RulePolicy(game.rules, scorer).rank(view, [battle])
    assert row["rule_score"] == 0 and row["action_cost"] == -2 and "initial_card_score" not in row
    row, = OneStepPlanner(game.rules, profile()).rank(CandidateView(view, [battle]), "germany")
    assert row["complete"] and row["map_delta"] == row["unit_removal_score"] == 2
    assert row["q"] == 0 and row["action_cost"] == -2
    assert row["continuations"][0]["option"] == "B"
    weights = profile()
    weights["scoring_overrides"] = {"unit_removal": 4, "play_opportunity": 1}
    row_a, = RulePolicy(game.rules, ScoreEvaluator(game.rules, weights)).rank(view, [battle])
    row_b, = OneStepPlanner(game.rules, weights).rank(CandidateView(view, [battle]), "germany")
    assert row_a["rule_score"] == row_b["q"] == 3
    assert row_a["action_cost"] == row_b["action_cost"] == -1
    assert row_b["unit_removal_score"] == 4


def test_status_uses_visible_attacks_with_a_floor_of_one_times_gain_less_payment(data):
    ability = scoring_status(data, phase="battle-space")
    data["settings"]["max_rounds"] = 8
    ability["trigger"]["card_trigger"]["kind"] = "army"
    ability["effects"] = [instruction({"type": "discard-hand", "count": 1}),
                          instruction({"type": "build-at-battle-space", "kind": "army"})]
    for n in range(2):
        data["initial_tokens"].append({"token_id": f"extra-battle-{n}", "definition_id": "g-battle",
            "token_type": "CARD", "owner_country": "germany", "position_id": zone("germany", "HAND")})
    own = forecast(data).statuses
    assert own.extra_reaction("germany", "g-extra") == (4, 1)
    assert own.report("germany", "g-status")["future"] == 3
    assert len(own.entries("germany", "g-status", None, True)) == 3
    foreign = forecast(data, "united-kingdom").statuses
    assert foreign.extra_reaction("germany", "g-extra") == (1, 1)
    data["settings"]["max_rounds"] = 2
    assert forecast(data).statuses.report("germany", "g-status")["future"] == 1


def test_airdrop_and_deck_top_use_their_separate_bases(data):
    ability = scoring_status(data, phase="after-air-action")
    map_with_sites(data, [("A", "B"), ("B", "C"), ("C", "E")], rounds=12)
    data["settings"]["air_phase"] = True
    data["units"].append({"id": "test-air", "kind": "AIR"})
    data["initial_tokens"].append({"token_id": "g-air", "definition_id": "test-air", "token_type": "UNIT",
                                   "owner_country": "germany", "position_id": zone("germany", "RESERVE_AIR")})
    ability["effects"] = [instruction({"type": "choose-space", "kind": "army",
                                       "action": "build", "nearAirAction": True})]
    economy = forecast(data)
    expected = 2 + economy.routes.placement_stability("germany", "B")
    assert economy.statuses.program("germany", "germany", "g-extra", "A", committed=True) == pytest.approx(expected)
    assert forecast(data).statuses.report("germany", "g-status")["future"] == 0
    card(data, "g-noop")["legacy_kind"] = "air-power"
    hand_only(data, "g-deck-2")
    assert forecast(data).statuses.report("germany", "g-status")["future"] == round(expected, 4)
    ability["trigger"]["card_trigger"]["type"] = "play-phase-start"
    ability["effects"] = [instruction({"type": "order-deck-top", "count": 3})]
    card(data, "g-noop").pop("legacy_kind")
    hand_only(data, "g-deck-2")
    assert forecast(data).statuses.report("germany", "g-status")["future"] == 3
    data["settings"]["max_rounds"] = 2
    assert forecast(data).statuses.report("germany", "g-status")["future"] == 3


def test_path_and_site_modifiers_count_a_single_nearest_piece_per_side(data):
    ability = scoring_status(data, phase="before-rule-use")
    ability.update(effects=[], activation_mode="AUTO")
    map_with_sites(data, [("A", "B"), ("B", "C"), ("C", "E"), ("A", "F")])
    change = {"kind": "connection", "team": "axis", "scope": "all", "a": "A", "b": "C", "enabled": True}
    card(data, "g-status")["modifiers"] = [change]
    weights = profile()
    weights["scoring_units"]["supply_stability"] = 0
    est = forecast(data, weights=weights).statuses
    value = est.strategic_modifiers("germany", "g-status", None)
    assert value > 0
    assert est.report("germany", "g-status")["future"] == value
    move_initial(data, "italy-army-0", "F")
    # A second unit a step farther from the edited edge does not get its own path award.
    estimate = forecast(data, weights=weights).statuses
    assert estimate.strategic_modifiers("germany", "g-status", None) == value
    card(data, "g-status")["modifiers"] = [{"kind": "score_bonus", "site": "C", "amount": 2, "scope": "all"}]
    estimate = forecast(data, weights=weights).statuses
    site = estimate.economy.routes.position.closest_site_interest("germany", "C", 2, TURN_ORDER)
    assert site != 0
    assert estimate.report("germany", "g-status")["future"] == site
    card(data, "g-status")["modifiers"][0]["amount"] = -2
    assert forecast(data, weights=weights).statuses.report("germany", "g-status")["future"] == -site


def test_publicly_known_score_source_activation_and_cancellation_match_in_a_and_b(data):
    source = next(t for t in data["initial_tokens"] if t["token_id"] == "dynamic-source")
    map_with_sites(data, [("A", "B"), ("B", "C"), ("C", "D"), ("D", "E")], ())
    data["score_sites"] = [{"id": source["definition_id"], "region": "C", "dynamic": True, "vp": 2,
                           "countries": list(TURN_ORDER), "requires_supply": False}]
    data["initial_tokens"].append(source)
    move_initial(data, "germany-army-0", "B")
    card(data, "g-noop")["effects"] = [{"type": "TRANSFER", "token": source["token_id"], "destination": "C"}]
    hand_only(data, "g-deck-2")
    engine = make(data)
    client = CoreAPI(engine, ("germany",))
    view = client.observe()
    weights = forecast_search_profile()
    scorer = ScoreEvaluator(engine.rules, weights)
    command = next(c for c in client.legal_commands() if c.get("card") == "g-deck-2")
    paper, = RulePolicy(engine.rules, scorer).rank(view, [command])
    searched, = OneStepPlanner(engine.rules, weights).rank(CandidateView(view, [command]), "germany")
    assert paper["rule_score"] == pytest.approx(searched["map_delta"])
    assert searched["map_delta"] - searched["supply_stability_delta"] == pytest.approx(4)
    client.submit(command)
    economy = scorer.economic_from_view(client.observe())
    active = economy.visible_state().tokens[source["token_id"]]
    assert economy.routes.position.source_move("germany", active, zone("germany", "SCORE_DORMANT")) == -4


def test_discard_trigger_adds_to_ordinary_cycle_value(data):
    ability = scoring_status(data, phase="discard-stage-card", points=3)
    ability["trigger"]["card_trigger"].update(country="germany", kind="event")
    card(data, "g-noop")["legacy_kind"] = "event"
    data["settings"]["discard_phase_penalty"] = 1
    hand_only(data, "g-deck-2")
    move_initial(data, "g-status", zone("germany", "STATUS"))
    scores = []
    for enabled in (True, False):
        if not enabled:
            move_initial(data, "g-status", zone("germany", "SET_ASIDE"))
        engine = make(data)
        client = CoreAPI(engine, ("germany",))
        reach_discard(client)
        policy = RulePolicy(engine.rules, ScoreEvaluator(engine.rules, profile()))
        best = policy.rank(client.observe(), client.legal_commands())[0]
        assert best["command"]["type"] == "DISCARD_CARD"
        assert (best["retention"]["discard_trigger"] > 0) == enabled
        scores.append(best["rule_score"])
    assert scores[0] > scores[1] > 0
