import pytest

from conftest import card, make, move_initial
from qmg_core import CoreAPI, ScoreEvaluator
from qmg_core.ai_match_policy import CandidateView, RulePolicy
from qmg_core.evaluation import OneStepPlanner
from qmg_core.expansion_scoring import ConstructionNetwork
from qmg_core.model import zone
from qmg_core.rules import LegalActionGenerator
from test_action_distance_scoring import forecast, map_with_sites
from test_ai_match import profile
from test_hand_retention import hand_only


def test_auxiliary_construction_doubles_distance_cost_but_keeps_occupied_site_value(data):
    map_with_sites(data, [("A", "B"), ("B", "C"), ("C", "E")])
    data["auxiliary_forces"] = [{"id": "aux", "controller": "germany"}]
    position = forecast(data).routes.position
    assert position.value("germany", "B", construction=True) == 2
    assert position.value("aux", "B", construction=True) == 0
    assert position.value("aux", "C", construction=True) == position.value("germany", "C", construction=True) == 4


def test_new_port_costs_one_land_action_and_existing_allied_port_is_free(data):
    map_with_sites(data, [("A", "S"), ("S", "T"), ("T", "B"), ("B", "D")], ("D", "T"))
    data["settings"]["naval_army_base"] = True
    move_initial(data, "germany-army-1", "D")
    move_initial(data, "germany-navy-0", "S")
    game = make(data)
    state = game.state
    legal = LegalActionGenerator(game.rules)
    action = {"type": "BUILD_NAVY"}
    network = ConstructionNetwork(game.rules, state, "germany")
    assert "T" not in legal.targets(state, "germany", action)
    assert network.distance("S")["T"] == 2
    assert "T" not in network.reachable("S", armies=0, navies=1)
    assert "T" in network.reachable("S", armies=1, navies=1)

    move_initial(data, "italy-army-0", "B")
    game = make(data)
    legal = LegalActionGenerator(game.rules)
    network = ConstructionNetwork(game.rules, game.state, "germany")
    assert "T" in legal.targets(game.state, "germany", action)
    assert network.distance("S")["T"] == 1
    assert "T" in network.reachable("S", armies=0, navies=1)


def test_supplied_friendly_relay_does_not_consume_another_build_card(data):
    map_with_sites(data, [("A", "B"), ("B", "C"), ("C", "D")], ("C", "D"))
    move_initial(data, "germany-army-0", "B")
    move_initial(data, "germany-army-1", "C")
    game = make(data)
    network = ConstructionNetwork(game.rules, game.state, "germany")
    assert "D" in network.reachable("B", armies=1, navies=0)
    assert "C" not in network.reachable("B", armies=1, navies=0)


@pytest.mark.parametrize("retained,bonus", [(False, 0), (True, 0.25)])
def test_a_and_b_use_remaining_owned_basics_after_paying_the_play(data, retained, bonus):
    map_with_sites(data, [("A", "B"), ("B", "C"), ("C", "D")], ("C", "D"))
    hand_only(data, "g-build")
    card(data, "g-build")["legacy_kind"] = "build-army"
    data["initial_tokens"].append({"token_id": "g-build-later", "definition_id": "g-build",
                                  "token_type": "CARD", "owner_country": "germany",
                                  "position_id": zone("germany", "HAND" if retained else "DECK")})
    game = make(data)
    client = CoreAPI(game, ("germany",))
    weights = profile()
    policy = RulePolicy(game.rules, ScoreEvaluator(game.rules, weights))
    play = next(c for c in client.legal_commands() if c.get("card") == "g-build")
    view = client.observe()
    routes = policy.evaluator.economic_from_view(view).routes
    assert routes.expansion("germany", "B", spent_card="g-build") == {
        "sites": ("C",) if retained else (), "score": bonus}
    assert routes.expansion("germany", "B")["score"] == 0.25
    row, = OneStepPlanner(game.rules, weights).rank(CandidateView(view, [play]), "germany")
    assert row["complete"] and row.get("expansion_score", 0) == bonus
    assert row.get("expansion_sites", ()) == (("C",) if retained else ())
    client.apply(play)
    located = next(c for c in client.legal_commands() if c.get("option") == "B")
    choice, = policy.rank(client.observe(), [located])
    assert choice["expansion"]["score"] == bonus
