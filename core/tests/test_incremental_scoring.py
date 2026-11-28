import pytest

from conftest import card, make, move_initial
from qmg_core import CoreAPI, ScoreEvaluator
from qmg_core.ai_match_policy import CandidateView
from qmg_core.api import simulation_from_observation
from qmg_core.evaluation import OneStepPlanner
from qmg_core.expansion_scoring import ConstructionNetwork
from qmg_core.model import zone
from qmg_core.rules import SupplyResolver
from qmg_core.search_cache import SearchCache
from test_action_distance_scoring import map_with_sites
from test_ai_match import profile
from test_hand_retention import hand_only
from test_regional_sites import count_status, effect


def visible_position(data, weights=None):
    game = make(data)
    view = CoreAPI(game, ("germany",)).observe(include_history=False)
    sim = simulation_from_observation(game.rules, view, search_projection=True)
    sim.rules.search_cache = SearchCache()
    return ScoreEvaluator(game.rules, weights or profile()).economic_from_state(sim.rules, sim.state).routes.position


def matching_removal(position, token_id):
    token = position.state.tokens[token_id]
    full = position.after_unit_removal(token_id, incremental=False)
    changed = position.after_unit_removal(token_id, incremental=True)
    assert full.state == changed.state
    assert position.state.tokens[token_id] == token
    air = position.rules.units[token.definition_id]["kind"] == "AIR"
    for offset in (0, 1):
        for tactical in (False, True):
            options = {"air": air, "distance_offset": offset, "enemy_attraction": tactical}
            assert full.position_terms(token.owner_country, token.position_id, **options) == changed.position_terms(
                token.owner_country, token.position_id, **options)
    return changed


def test_unit_delta_rechecks_strait_and_retains_unchanged_sites(data):
    map_with_sites(data, [("D", "S")], ("T",))
    data["settings"]["straits"] = [{"land": "D", "seas": ["S", "T"]}]
    move_initial(data, "germany-army-1", "D")
    position = visible_position(data)
    before = position.position_terms("germany", "D", air=False, distance_offset=0, enemy_attraction=False)
    assert before["T"] > 0
    after = matching_removal(position, "germany-army-1")
    assert "T" not in after.distances("germany", "D")
    assert "T" not in after.position_terms("germany", "D", air=False, distance_offset=0, enemy_attraction=False)
    assert after.sites_by_region is position.sites_by_region


def test_unit_delta_updates_capital_occupation_and_status_condition(data):
    map_with_sites(data, [("A", "B"), ("B", "C"), ("C", "D")])
    count_status(data, [effect(1, "C")])
    data["settings"]["occupied_home_blocks_scoring"] = True
    card(data, "g-status").update(
        modifiers=[{"kind": "home", "force": "germany", "region": "B", "scope": "all"}],
        content_condition={"type": "unit-count", "filter": {"force": "self", "kind": "army", "spaces": ["D"]},
                           "min": 1})
    move_initial(data, "germany-army-1", "D")
    move_initial(data, "united-kingdom-army-1", "B")
    position = visible_position(data)
    assert position.layers and position.home_locations["germany"] == {"B"}
    assert "germany" in position.blocked

    without_enemy = matching_removal(position, "united-kingdom-army-1")
    assert without_enemy.layers == position.layers
    assert "germany" not in without_enemy.blocked
    without_enabler = matching_removal(position, "germany-army-1")
    assert not without_enabler.layers
    assert without_enabler.home_locations["germany"] == {"A"}
    assert "germany" not in without_enabler.blocked


def test_unit_delta_keeps_a_capital_blocked_by_a_second_unit(data):
    map_with_sites(data, [("A", "B"), ("B", "C")])
    data["settings"]["occupied_home_blocks_scoring"] = True
    move_initial(data, "germany-army-0", "D")
    move_initial(data, "united-kingdom-army-1", "A")
    move_initial(data, "soviet-union-army-1", "A")
    position = visible_position(data)
    assert "germany" in position.blocked
    assert "germany" in matching_removal(position, "united-kingdom-army-1").blocked


def test_unsettled_position_uses_independent_context(data):
    map_with_sites(data, [("A", "B"), ("B", "C")])
    hand_only(data, "g-build")
    game = make(data)
    view = CoreAPI(game, ("germany",)).observe(include_history=False)
    sim = simulation_from_observation(game.rules, view, search_projection=True)
    projected_player = CoreAPI(sim, ("germany",))
    projected_player.apply(next(c for c in projected_player.legal_commands() if c.get("card") == "g-build"))
    assert sim.state.decision is not None
    position = ScoreEvaluator(game.rules, profile()).economic_from_state(sim.rules, sim.state).routes.position
    after = matching_removal(position, "germany-army-0")
    assert after.sites_by_region is not position.sites_by_region


def test_unit_delta_rechecks_the_port_detour(data):
    map_with_sites(data, [("A", "S"), ("S", "T"), ("T", "B"), ("B", "D")], ("D", "T"))
    data["settings"]["naval_army_base"] = True
    move_initial(data, "germany-army-1", "D")
    move_initial(data, "germany-navy-0", "S")
    move_initial(data, "italy-army-0", "B")
    position = visible_position(data)
    assert ConstructionNetwork(position.rules, position.state, "germany").distance("S")["T"] == 1
    after = matching_removal(position, "italy-army-0")
    assert ConstructionNetwork(after.rules, after.state, "germany").distance("S")["T"] == 2


def test_unit_delta_does_not_reuse_supply_after_a_relay_is_removed(data):
    map_with_sites(data, [("A", "B"), ("B", "C")], ())
    data["settings"].update(supply_revision=2, allied_supply=False)
    move_initial(data, "germany-army-1", "B")
    move_initial(data, "germany-army-2", "C")
    position = visible_position(data)
    after = matching_removal(position, "germany-army-1")
    resolver = SupplyResolver(position.rules)
    assert "germany-army-2" in resolver.supplied(position.state, "germany")
    supplied = resolver.supplied(after.state, "germany")
    assert "germany-army-2" not in supplied
    position.rules.search_cache = SearchCache(incremental=False)
    assert resolver.supplied(after.state, "germany") == supplied


@pytest.mark.parametrize("action_value", [1, 2, 3.25])
def test_planner_position_and_candidate_order_share_the_scoring_parameters(data, action_value):
    map_with_sites(data, [("A", "B"), ("B", "C"), ("C", "E")])
    hand_only(data, "g-build", "g-vp")
    game = make(data)
    client = CoreAPI(game, ("germany",))
    weights = profile()
    weights["scoring_units"]["action"] = action_value
    options = CandidateView(client.observe(include_history=False), client.legal_commands())
    expected = OneStepPlanner(game.rules, weights, incremental=False).rank(options, "germany")
    actual = OneStepPlanner(game.rules, weights).rank(options, "germany")
    assert len([candidate for candidate in expected if candidate["complete"]]) >= 2
    assert actual == expected
