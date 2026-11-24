from copy import deepcopy

import pytest

from conftest import card, make, move_initial
from qmg_core import CoreAPI
from qmg_core.ai_match_policy import CandidateView, RulePolicy
from qmg_core.content_effects import instruction
from qmg_core.evaluation import OneStepPlanner, ScoreEvaluator
from test_ai_match import profile


def previous_weights():
    weights = profile()
    weights.pop("frontier_scoring")
    weights["distance_weights"] = [1, 0.65, 0.30, 0.10]
    return weights


def two_sites(data):
    data["initial_tokens"] = [t for t in data["initial_tokens"] if t["token_type"] != "SCORE_SOURCE"]
    data["score_sites"] = []
    for region in ("front", "rear"):
        data["regions"].append({"id": region, "type": "LAND"})
        data["connections"].append({"id": "to-" + region, "a": "A", "b": region})
        data["score_sites"].append({"id": region, "region": region, "vp": 2,
                                    "countries": data["turn_order"], "requires_supply": False})
    move_initial(data, "italy-army-0", "rear")
    move_initial(data, "united-kingdom-army-0", "front")
    for token in data["initial_tokens"]:
        if token["position_id"] == "germany:HAND" and token["token_id"] not in ("g-build", "g-battle"):
            token["position_id"] = "germany:SET_ASIDE"


def test_distance_schedule_and_enemy_route_preserve_long_approach(data):
    two_sites(data)
    engine = make(data)
    view = CoreAPI(engine, ("germany",)).observe()
    current = ScoreEvaluator(engine.rules, profile())
    routes = current.economic_from_view(view).routes
    assert [routes.distance_weight(d) for d in range(3)] == [1.6, 1, 0.5]
    assert [routes.distance_weight(d) for d in range(3, 9)] == pytest.approx([0.1 * 0.7 ** i for i in range(6)])
    # Both points are two steps from B via the same friendly army in A.
    assert routes.nearest("germany", "B") == {"front": (2, 0.8 * 1.4), "rear": (2, 0.8)}
    assert routes.target_value("germany", "front") > routes.target_value("germany", "rear")
    assert routes.paper_bonus("germany", "B", battle=True) == 0


def test_enemy_on_approach_increases_interest_in_an_allied_scoring_site(data):
    two_sites(data)
    data["connections"].append({"id": "rear-approach", "a": "B", "b": "rear"})
    def rates():
        engine = make(data)
        client = CoreAPI(engine, ("germany",))
        routes = ScoreEvaluator(engine.rules, profile()).economic_from_view(client.observe()).routes
        return routes.target_value("germany", "rear"), routes.nearest("germany", "E")["rear"]
    safe, (_, quiet_path) = rates()
    move_initial(data, "soviet-union-army-0", "B")
    threatened, (distance, enemy_path) = rates()
    assert threatened > safe
    assert distance == 2 and enemy_path == pytest.approx(1.4 * quiet_path)


@pytest.mark.parametrize("split_battle", [False, True])
def test_rule_policy_selects_attack_on_enemy_site_over_rear_build(data, split_battle):
    two_sites(data)
    if split_battle:
        card(data, "g-battle")["effects"] = [instruction({"type": "battle", "kind": "either"})]
    engine = make(data)
    client = CoreAPI(engine, ("germany",))
    previous, current = [RulePolicy(engine.rules, ScoreEvaluator(engine.rules, w)) for w in (previous_weights(), profile())]
    def rank(policy):
        return policy.rank(client.observe(), client.legal_commands())
    assert rank(previous)[0]["command"]["card"] == "g-build"
    picked = rank(current)[0]
    assert picked["command"]["card"] == "g-battle"
    assert picked["rule_score"] == pytest.approx(1.3 - 0.55 + 2)
    client.submit(picked["command"])
    view = client.observe()
    assert view["decision"]["map_force"] == "germany" and view["decision"]["map_battle"]
    assert "map_force" not in CoreAPI(engine, ("italy",)).observe()["decision"]
    target = rank(current)[0]["command"]
    assert target["option"] == ("army|front" if split_battle else "front")
    client.submit(target)
    assert engine.state.tokens["united-kingdom-army-0"].position_id != "front"


def test_held_income_weight_applies_symmetrically_only_to_forecast(data):
    two_sites(data)
    # Use disconnected sites: no approaching enemy and no change in threat between profiles.
    data["connections"] = [c for c in data["connections"] if not c["id"].startswith("to-")]
    engine = make(data)
    for seat in ("germany", "united-kingdom"):
        client = CoreAPI(engine, (seat,))
        old, new = [ScoreEvaluator(engine.rules, w).evaluate(client, seat) for w in (previous_weights(), profile())]
        assert new.actual_vp == old.actual_vp == 0
        for a, b in zip(old.score_points, new.score_points, strict=True):
            assert b["baseline_vp"] == a["baseline_vp"] > 0
            assert b["value"] == pytest.approx(1.25 * a["value"])


def test_hidden_replacements_cannot_change_frontier_scores(data):
    two_sites(data)
    other = deepcopy(data)
    next(t for t in other["initial_tokens"] if t["token_id"] == "uk-hidden")["definition_id"] = "uk-other"
    games = [make(data), make(other, seed=1000)]
    assert games[0].state.positions["germany:DECK"] != games[1].state.positions["germany:DECK"]
    scores = []
    for engine in games:
        client = CoreAPI(engine, ("germany",))
        evaluator = ScoreEvaluator(engine.rules, profile())
        scores.append((RulePolicy(engine.rules, evaluator).rank(client.observe(), client.legal_commands()),
                       evaluator.evaluate(client, "germany")))
    assert scores[0] == scores[1]


def test_search_values_breaking_enemy_income_after_settlement(data):
    two_sites(data)
    engine = make(data)
    client = CoreAPI(engine, ("germany",))
    command = next(c for c in client.legal_commands() if c.get("card") == "g-battle")
    offered = CandidateView(client.observe(), [command])
    old, new = [OneStepPlanner(engine.rules, w).rank(offered, "germany")[0] for w in (previous_weights(), profile())]
    assert new["complete"] and new["continuations"][0]["option"] == "front"
    assert new["actual_vp_delta"] == old["actual_vp_delta"] == 0
    assert new["map_delta"] > old["map_delta"] > 0


def test_exclusive_military_branch_selects_its_valued_target(data):
    two_sites(data)
    card(data, "g-battle")["effects"] = [instruction({"type": "choose-one", "options": [
        {"label": "far", "effects": [{"type": "battle", "kind": "army", "spaces": ["B"]}]},
        {"label": "front", "effects": [{"type": "battle", "kind": "army", "spaces": ["front"]}]},
    ]})]
    engine = make(data)
    client = CoreAPI(engine, ("germany",))
    policy = RulePolicy(engine.rules, ScoreEvaluator(engine.rules, profile()))
    def pick():
        return policy.rank(client.observe(), client.legal_commands())[0]["command"]
    command = pick()
    assert command["card"] == "g-battle"
    client.submit(command)
    assert "effect_branches" not in CoreAPI(engine, ("italy",)).observe()["decision"]
    branch = pick()
    assert branch["option"] == "1"
    client.submit(branch)
    assert pick()["option"] == "front"
