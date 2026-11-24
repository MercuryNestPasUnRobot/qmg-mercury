from conftest import make, move_initial
from qmg_core import CoreAPI
from qmg_core.ai_match_policy import CandidateView, RulePolicy
from qmg_core.evaluation import OneStepPlanner, ScoreEvaluator
from test_ai_match import profile


def test_construction_uses_closer_or_equal_units_once_across_scoring_sites(data):
    positions = ("line-0", "line-1", "line-2", "line-3", "line-4", "equal", "other-site")
    data["regions"].extend({"id": p, "type": "LAND"} for p in positions)
    links = [(f"line-{i}", f"line-{i+1}") for i in range(4)]
    links += [("line-1", "equal"), ("line-2", "equal"), ("line-1", "other-site")]
    data["connections"].extend({"id": f"new-{i}", "a": a, "b": b} for i, (a, b) in enumerate(links))
    data["initial_tokens"] = [t for t in data["initial_tokens"] if t["token_type"] != "SCORE_SOURCE"]
    data["score_sites"] = [{"id": name, "region": region, "vp": 2, "countries": data["turn_order"],
                           "requires_supply": True} for name, region in (("first", "line-0"), ("second", "other-site"))]
    move_initial(data, "germany-army-0", "line-3")
    move_initial(data, "germany-army-1", "equal")
    move_initial(data, "italy-army-0", "line-1")
    move_initial(data, "italy-army-1", "line-4")
    engine = make(data)
    evaluator = ScoreEvaluator(engine.rules, profile())
    routes = evaluator.economic_from_view(CoreAPI(engine, ("germany",)).observe()).routes
    assert routes.construction_factors("germany", "line-2") == {"first": 0.75 ** 2}
    assert routes.construction_factors("germany", "line-2", ignored_sites={"first"}) == {"second": 0.75 ** 2}
    # The nearby army can still compete for the other point, two steps away.
    assert routes.construction_factors("germany", "line-0") == {"second": 0.75}


def test_rule_policy_and_search_build_directly_on_empty_surrounded_site(data):
    data["regions"].append({"id": "bypass", "type": "LAND"})
    data["connections"].append({"id": "bypass-connection", "a": "B", "b": "bypass"})
    data["initial_tokens"] = [t for t in data["initial_tokens"] if t["token_type"] != "SCORE_SOURCE"]
    data["score_sites"] = [site for site in data["score_sites"] if site["id"] == "factory"]
    move_initial(data, "germany-army-1", "B")
    move_initial(data, "italy-army-0", "D")
    engine = make(data)
    client = CoreAPI(engine, ("germany",))
    view = client.observe()
    weights = profile()
    evaluator = ScoreEvaluator(engine.rules, weights)
    routes = evaluator.economic_from_view(view).routes
    assert routes.construction_factors("germany", "C") == {}
    assert routes.target_value("germany", "C", construction=True) == routes.target_value("germany", "C") + 3

    card = next(c for c in client.legal_commands() if c.get("card") == "g-build")
    row, = OneStepPlanner(engine.rules, weights).rank(CandidateView(view, [card]), "germany")
    assert row["complete"] and row["continuations"][0]["option"] == "C"

    client.submit(card)
    legal = client.legal_commands()
    assert {c["option"] for c in legal} >= {"C", "bypass"}
    pick = RulePolicy(engine.rules, evaluator).rank(client.observe(), legal)[0]["command"]
    assert pick["option"] == "C"
    client.submit(pick)
    assert ("germany-army-2" in engine.state.positions["C"] and
            "germany-army-2" in engine.legal.supply.supplied(engine.state, "germany"))
