from copy import deepcopy

from conftest import make, move_initial
from qmg_core import CoreAPI, ScoreEvaluator, ScoreResolver
from qmg_core.ai_match_policy import CandidateView, RulePolicy
from qmg_core.evaluation import OneStepPlanner
from qmg_core.model import TURN_ORDER
from test_ai_match import profile
from test_hand_retention import hand_only


def test_shared_scoring_settles_one_point_per_allied_seat(data):
    data["settings"].update(max_rounds=1, victory_margin=0)
    data["score_sites"] = [site for site in data["score_sites"] if site["id"] == "factory"]
    data["initial_tokens"] = [t for t in data["initial_tokens"] if t["token_type"] != "SCORE_SOURCE"]
    move_initial(data, "germany-army-0", "C")
    move_initial(data, "italy-army-0", "C")
    game = make(data)
    for seat in ("germany", "italy"):
        assert ScoreResolver(game.rules).sites(game.state, seat) == {"factory": 1}
    old_data = deepcopy(data)
    old_data["settings"].pop("shared_site_scoring")
    old = make(old_data)
    assert ScoreResolver(old.rules).sites(old.state, "germany") == {"factory": 2}
    move_initial(data, "italy-army-0", "H")
    solo = make(data)
    assert ScoreResolver(solo.rules).sites(solo.state, "germany") == {"factory": 2}

    for _ in range(100):
        if game.state.winner:
            break
        commands = game.legal_commands(TURN_ORDER)
        command = next((c for c in commands if c["type"] == "END_PHASE"), commands[0])
        game.submit(command, TURN_ORDER)
    assert game.state.winner == "axis"
    gains = [r for r in game.records if r["operation"].get("score_site") == "factory"]
    assert [(r["actor"], r["operation"]["amount"]) for r in gains] == [("germany", 1), ("italy", 1)]


def test_a_and_b_prefer_new_income_over_sharing_an_allied_site(data):
    data["score_sites"] = [site for site in data["score_sites"] if site["id"] == "factory"]
    data["score_sites"].append({**data["score_sites"][0], "id": "open", "region": "B"})
    data["initial_tokens"] = [t for t in data["initial_tokens"] if t["token_type"] != "SCORE_SOURCE"]
    data["connections"] = [{"id": "approach-open", "a": "A", "b": "B"},
                           {"id": "approach-held", "a": "A", "b": "C"}]
    hand_only(data, "g-build")
    move_initial(data, "italy-army-0", "C")
    engine = make(data)
    weights = profile()
    client = CoreAPI(engine, ("germany",))
    evaluator = ScoreEvaluator(engine.rules, weights)
    view = client.observe()
    routes = evaluator.economic_from_view(view).routes
    assert routes.direct_site_gain("germany", "C") == 0
    assert routes.direct_site_gain("germany", "B") == 2
    build = next(c for c in client.legal_commands() if c.get("card") == "g-build")
    row, = OneStepPlanner(engine.rules, weights).rank(CandidateView(view, [build]), "germany")
    assert row["complete"] and row["continuations"][0]["option"] == "B"
    client.apply(build)
    assert {"B", "C"} <= {c["option"] for c in client.legal_commands()}
    assert RulePolicy(engine.rules, evaluator).rank(client.observe(), client.legal_commands())[0]["command"]["option"] == "B"
