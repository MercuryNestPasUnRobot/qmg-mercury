from copy import deepcopy

from conftest import card, make, move_initial
from qmg_core import CoreAPI, ScoreEvaluator
from qmg_core.api import simulation_from_observation
from qmg_core.expansion_scoring import ConstructionNetwork
from qmg_core.model import ZoneRegistry, zone
from qmg_core.rules import MapGraph
from qmg_core.search_cache import SearchCache
from test_action_distance_scoring import map_with_sites
from test_ai_match import profile
from test_hand_retention import hand_only


def test_routes_reuse_topology_until_strait_or_connection_rules_change(data):
    map_with_sites(data, [("A", "B"), ("B", "C")])
    data["settings"]["straits"] = [{"land": "D", "seas": ["S", "T"]}]
    card(data, "g-status")["modifiers"] = [
        {"kind": "connection", "team": "axis", "scope": "all", "a": "A", "b": "C",
         "enabled": True, "movement_or_supply_tags": ["build"]},
    ]
    game = make(data)
    game.rules.search_cache = SearchCache()
    graph, state = MapGraph(game.rules), game.state
    build = graph.adjacency(state, "germany", "build")
    across = graph.distances("S", state, "germany", "build")
    assert "T" not in across
    assert graph.distances("S", state, "united-kingdom", "build")["T"] == 1
    assert graph.adjacency(state, "italy", "build") is build

    ZoneRegistry.transfer(state, "germany-army-1", "B")
    assert graph.adjacency(state, "germany", "build") is build
    assert graph.distances("S", state, "germany", "build") is across

    ZoneRegistry.transfer(state, "germany-army-1", "D")
    assert graph.distances("S", state, "germany", "build")["T"] == 1
    assert "T" not in graph.distances("S", state, "united-kingdom", "build")
    supply = graph.adjacency(state, "germany", "supply")
    ZoneRegistry.transfer(state, "g-status", zone("germany", "STATUS"))
    assert graph.distances("A", state, "germany", "build")["C"] == 1
    assert graph.distances("A", state, "germany", "build") is graph.distances("A", state, "italy", "build")
    assert graph.adjacency(state, "germany", "supply") is supply
    assert graph.distances("A", state, "united-kingdom", "build")["C"] == 2
    ZoneRegistry.transfer(state, "g-status", zone("germany", "DISCARD"))
    assert graph.distances("A", state, "germany", "build")["C"] == 2


def test_weighted_route_invalidates_only_when_port_access_changes(data):
    map_with_sites(data, [("A", "S"), ("S", "T"), ("T", "B"), ("B", "D")], ("D", "T"))
    data["settings"]["naval_army_base"] = True
    move_initial(data, "germany-army-1", "D")
    move_initial(data, "germany-navy-0", "S")
    move_initial(data, "italy-army-0", "F")
    game = make(data)
    rules, state = game.rules, game.state
    rules.search_cache = SearchCache()

    def distances():
        return ConstructionNetwork(rules, state, "germany").distance("S")

    original = distances()
    assert original["T"] == 2
    # A move inland on the same board changes neither the topology nor usable ports.
    ZoneRegistry.transfer(state, "italy-army-0", "E")
    assert distances() is original
    ZoneRegistry.transfer(state, "italy-army-0", "B")
    with_port = distances()
    assert with_port["T"] == 1
    rules.search_cache = None
    assert distances() == with_port


def test_site_capture_reuses_the_enemy_front_when_only_allied_units_move(data):
    map_with_sites(data, [("A", "B"), ("B", "C"), ("C", "D"), ("D", "E")])
    move_initial(data, "united-kingdom-army-1", "C")
    game = make(data)
    rules, state = game.rules, game.state
    rules.search_cache = cache = SearchCache()
    evaluator = ScoreEvaluator(rules, profile())

    def capture():
        return evaluator.economic_from_state(rules, state).routes.position.capture("germany", "C", 2)

    assert capture() == 6  # The enemy already on the site would have been removed.
    ZoneRegistry.transfer(state, "germany-army-0", "B")
    assert capture() == 6
    assert len(cache.enemy_distances) == 1
    ZoneRegistry.transfer(state, "united-kingdom-army-0", "D")
    assert capture() == 4
    assert len(cache.enemy_distances) == 2


def test_score_components_invalidate_by_inputs_on_the_visible_projection(data):
    map_with_sites(data, [("A", "B"), ("B", "C"), ("C", "E")])
    hand_only(data, "g-build")
    game = make(data)
    view = CoreAPI(game, ("germany",)).observe(include_history=False)
    sim = simulation_from_observation(game.rules, view, search_projection=True)
    rules = sim.rules
    evaluator = ScoreEvaluator(game.rules, profile())

    original = sim.state
    card_change = deepcopy(original)
    ZoneRegistry.transfer(card_change, "g-build", zone("germany", "DISCARD"))
    board_change = deepcopy(card_change)
    ZoneRegistry.transfer(board_change, "germany-army-1", "B")
    states = (original, card_change, board_change)
    expected = [evaluator.snapshot_value(rules, state, "germany") for state in states]

    rules.search_cache = cache = SearchCache()
    assert evaluator.snapshot_value(rules, original, "germany") == expected[0]
    assert evaluator.snapshot_value(rules, card_change, "germany") == expected[1]
    assert len(cache.map_scores) == len(cache.supply_scores) == 1
    assert len(cache.card_scores) == len(cache.resource_scores) == 2
    assert evaluator.snapshot_value(rules, board_change, "germany") == expected[2]
    assert len(cache.map_scores) == len(cache.supply_scores) == 2
    assert len(cache.card_scores) == 3
    assert len(cache.resource_scores) == 2
    assert evaluator.snapshot_value(rules, original, "germany") == expected[0]
