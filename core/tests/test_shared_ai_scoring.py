from conftest import card, make, move_initial
from qmg_core import CoreAPI, ScoreEvaluator
from qmg_core.ai_match_policy import CandidateView, RulePolicy, SeatPolicy
from qmg_core.api import simulation_from_observation
from qmg_core.evaluation import OneStepPlanner
from qmg_core.model import zone
from test_action_distance_scoring import map_with_sites
from test_ai_match import profile
from test_hand_retention import hand_only
from test_regional_sites import count_status, effect


def test_a_and_b_value_the_same_visible_snapshot_and_include_hidden_site_approach(data):
    map_with_sites(data, [("A", "C")], rounds=2)
    count_status(data, [effect(2, "C")])
    data["settings"]["max_rounds"] = 2
    p = make(data)
    seat = CoreAPI(p, ("germany",))
    evaluator = ScoreEvaluator(p.rules, profile())
    planner = OneStepPlanner(p.rules, profile())
    view = seat.observe(include_history=False)
    projection = simulation_from_observation(p.rules, view, search_projection=True)
    paper = evaluator.evaluate(seat, "germany")
    search = planner.end_score(projection, "germany", None)
    assert paper == search
    points = {row["site"]: row["value"] for row in paper.score_points}
    assert points["C"] == 3
    assert sum(value for site, value in points.items() if site.startswith("hidden:")) == 3
    assert paper.map_value - paper.supply_stability == 6
    assert not any(t["position_id"] == "united-kingdom:HAND" for t in view["tokens"].values())


def test_snapshot_sums_each_unit_once_and_uses_auxiliary_construction_distance(data):
    map_with_sites(data, [("A", "B"), ("B", "C"), ("D", "C")], rounds=2)
    data["auxiliary_forces"] = [{"id": "aux", "controller": "germany"}]
    data["initial_tokens"].append({"token_id": "aux-land", "definition_id": "army", "token_type": "UNIT",
                                   "owner_country": "aux", "position_id": "B"})
    move_initial(data, "italy-army-0", "D")
    move_initial(data, "germany-army-0", "C")
    p = make(data)
    seat = CoreAPI(p, ("germany",))
    evaluator = ScoreEvaluator(p.rules, profile())
    report = evaluator.evaluate(seat, "germany")
    terms = {row["token"]: row["score_influence"] for row in report.token_influence if row["site"] == "C"}
    assert terms["aux-land"] == 0
    score = next(row["value"] for row in report.score_points if row["site"] == "C")
    assert score == sum(terms.values())
    assert seat.observe()["tokens"]["aux-land"]["position_id"] == "B"


def test_b_search_compares_cards_below_as_top_paper_score(data):
    data["settings"]["mandatory_turn_play"] = True
    map_with_sites(data, [])
    hand_only(data, "g-vp", "g-copy")
    card(data, "g-vp")["effects"] = [{"type": "GAIN_VP", "amount": 6}]
    card(data, "g-build")["effects"] = [{"type": "GAIN_VP", "amount": 3}]
    p = make(data)
    seat = CoreAPI(p, ("germany",))
    evaluator = ScoreEvaluator(p.rules, profile())
    planner = OneStepPlanner(p.rules, profile())

    class Search:
        def run(self, view, commands, country, deadline):
            return list(planner.iter_rank(CandidateView(view, commands), country, deadline=deadline)), "finished"

    a, _ = SeatPolicy(p.rules, evaluator=evaluator).decide(seat, "germany")
    b, report = SeatPolicy(p.rules, evaluator=evaluator, search=Search()).decide(seat, "germany")
    assert a == b and a["card"] == "g-vp"
    assert {row["command"]["card"] for row in report["candidate_scores"] if row["complete"]} == {"g-vp", "g-copy"}
    assert report["selected_q"] == 4
    baseline = evaluator.evaluate(seat, "germany")
    economy = evaluator.economic_from_view(seat.observe())
    payment = RulePolicy(p.rules, evaluator).discard_value(p.rules.cards["g-vp"], "germany", economy, seat.observe())
    move_initial(data, "g-vp", zone("germany", "DISCARD"))
    without = make(data)
    afterward = evaluator.evaluate(CoreAPI(without, ("germany",)), "germany")
    assert afterward.card_value - baseline.card_value == payment


def test_b_resolves_an_occupied_battle_target_with_two_defenders(data):
    map_with_sites(data, [("A", "C"), ("A", "B")], rounds=8)
    data["settings"].update(mandatory_turn_play=True, round_end_victory=True)
    move_initial(data, "united-kingdom-army-0", "C")
    move_initial(data, "soviet-union-army-0", "C")
    hand_only(data, "g-battle")
    p = make(data)
    seat = CoreAPI(p, ("germany",))
    row, = OneStepPlanner(p.rules, profile()).rank(seat, "germany")
    assert row["complete"] and row["q"] > 0
    assert row["continuations"][0]["option"] == "C"
    assert row["continuations"][1]["option"] in ("united-kingdom-army-0", "soviet-union-army-0")
    assert row["unit_removal_score"] == 2
