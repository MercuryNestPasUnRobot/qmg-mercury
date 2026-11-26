from conftest import card, make, move_initial
from qmg_core import CoreAPI, ScoreResolver
from qmg_core.content_effects import instruction
from qmg_core.api import simulation_from_observation
from qmg_core.model import TURN_ORDER, ZoneRegistry, zone
from qmg_core.rules import SupplyResolver
from qmg_core.settlement_scoring import SettlementScorer
from test_action_distance_scoring import forecast, map_with_sites
from test_ai_match import profile
from test_status_scoring import scoring_status


def effect(amount, *spaces):
    return instruction({"type": "points-per-occupied-space", "amountPer": amount, "occupiedBy": "self",
                        "side": "self", "spaces": list(spaces)})


def count_status(data, programs):
    rules = scoring_status(data)
    rules["effects"] = list(programs)
    data["settings"].update(regional_status_sites=True, supply_revision=2, max_rounds=6)
    move_initial(data, "g-status", zone("germany", "STATUS"))
    return rules


def test_same_equal_distance_ally_deducts_at_every_site_and_never_below_zero(data):
    map_with_sites(data, [("A", "C"), ("A", "D"), ("B", "C"), ("B", "D"), ("C", "E"), ("D", "E")], ("C", "D"))
    move_initial(data, "germany-army-0", "H")
    move_initial(data, "italy-army-0", "B")
    position = forecast(data).routes.position
    assert position.site_values("germany", "A") == {"C": (1, 1), "D": (1, 1)}
    move_initial(data, "united-kingdom-army-1", "C")
    position = forecast(data).routes.position
    assert position.site_values("germany", "A") == {"C": (1, 5), "D": (1, 1)}
    move_initial(data, "united-kingdom-army-1", zone("united-kingdom", "RESERVE_ARMY"))
    move_initial(data, "italy-army-0", "C")
    assert forecast(data).routes.position.site_values("germany", "A")["C"] == (1, 0)


def test_stacked_position_pays_one_distance_cost_and_off_route_friendly_penalty(data):
    map_with_sites(data, [("A", "B"), ("B", "C"), ("D", "C"), ("E", "C")])
    data["score_sites"].append({**data["score_sites"][0], "id": "second"})
    move_initial(data, "germany-army-0", "H")
    position = forecast(data).routes.position
    assert position.value("germany", "A") == 4  # Combined 8, minus two steps.
    move_initial(data, "italy-army-0", "D")
    position = forecast(data).routes.position
    assert position.distances("germany", "A")["D"] == 3
    assert position.value("germany", "A") == 1  # The closer, off-route ally deducts half of 8 - 2.


def test_occupants_use_their_own_entitlements_and_defenders_get_threat_bonus(data):
    map_with_sites(data, [("C", "E")], rounds=2)
    data["score_sites"].append({**data["score_sites"][0], "id": "half", "vp": 1,
                                "scoring": "per-unit", "countries": ["germany"]})
    move_initial(data, "germany-army-0", "H")
    move_initial(data, "united-kingdom-army-0", zone("united-kingdom", "RESERVE_ARMY"))
    move_initial(data, "italy-army-0", "C")
    position = forecast(data).routes.position
    assert position.value("germany", "C") == 2  # 6 - Italy's 2 for distance and 2 for occupation.
    move_initial(data, "united-kingdom-army-0", "E")
    assert forecast(data).routes.position.value("germany", "C") == 4
    move_initial(data, "soviet-union-army-0", "E")
    assert forecast(data).routes.position.value("germany", "C") == 6
    move_initial(data, "italy-army-0", "H")
    move_initial(data, "united-kingdom-army-0", "C")
    assert forecast(data).routes.position.value("germany", "C") == 10


def test_printed_half_and_multiple_statuses_stack_in_core_and_b_and_one_route_slot_in_a(data):
    map_with_sites(data, [("C", "E")], ("C",))
    data["score_sites"].append({**data["score_sites"][0], "id": "half-C", "vp": 1, "scoring": "per-unit"})
    first = count_status(data, [effect(1, "C")])
    data["abilities"].append({**first, "id": "other-regional", "effects": [effect(2, "C")]})
    card(data, "g-noop").update(type="STATUS", legacy_kind="status", effects=[], abilities=["other-regional"])
    move_initial(data, "g-deck-2", zone("germany", "STATUS"))
    move_initial(data, "germany-army-0", "C")
    game = make(data)
    client = CoreAPI(game, ("germany",))
    raw = ScoreResolver(game.rules).sites(game.state, "germany")
    assert sorted(raw.values()) == [1, 1, 2, 2] and len(raw) == 4
    projected = simulation_from_observation(game.rules, client.observe(), search_projection=True)
    b = SettlementScorer(forecast(data).units).value(projected, "germany")
    assert {row["site"]: row["value"] for row in b.score_points} == raw

    weights = profile()
    weights["route_forecast"]["nearest_sites"] = 1
    route = forecast(data, weights=weights).routes.position.site_values("germany", "C", excluded="germany-army-0")
    assert sorted(score for _, score in route.values()) == [2, 2, 4, 4]
    before = len(game.records)
    while game.state.turn.phase != "Victory":
        client.apply(next(c for c in client.legal_commands() if c["type"] == "END_PHASE"))
    assert not game.state.decision and game.state.scores["axis"] == 6
    assert {r["operation"]["score_site"]: r["operation"]["amount"] for r in game.records[before:]
            if r["operation_type"] == "GAIN_VP"} == raw


def test_hidden_only_site_has_no_supply_and_disappears_with_the_source(data):
    map_with_sites(data, [], ())
    count_status(data, [effect(1, "B")])
    data["settings"]["temporary_status_disable"] = True
    move_initial(data, "germany-army-0", "B")
    game = make(data)
    state = game.state
    resolver = ScoreResolver(game.rules)
    points = resolver.sites(state, "germany")
    assert points == {"hidden:g-status:g-extra:0:B": 1}
    assert "germany-army-0" not in SupplyResolver(game.rules).supplied(state, "germany")
    for seat in TURN_ORDER[1:]:
        assert not resolver.sites(state, seat)
    ZoneRegistry.transfer(state, "g-status", zone("germany", "DISABLED_STATUS"))
    assert not resolver.sites(state, "germany")
    ZoneRegistry.transfer(state, "g-status", zone("germany", "DISCARD"))
    assert not resolver.sites(state, "germany")
    client = CoreAPI(game, ("germany",))
    while game.state.turn.phase != "Victory":
        client.apply(next(c for c in client.legal_commands() if c["type"] == "END_PHASE"))
    assert game.state.scores["axis"] == 0
    assert game.state.tokens["germany-army-0"].position_id == zone("germany", "RESERVE_ARMY")


def test_alternative_sites_pay_once_and_regional_absence_is_respected(data):
    map_with_sites(data, [], ())
    condition = {"type": "unit-count", "min": 1, "filter": {"force": "self", "kind": "army", "spaces": ["C", "D"]}}
    empty = {"type": "unit-count", "min": 0, "max": 0, "filter": {"force": "enemy", "kind": "army", "spaces": ["B"]}}
    count_status(data, [instruction({"type": "points", "side": "self", "amount": 2, "when": condition}),
                        instruction({"type": "points", "side": "self", "amount": 1, "when": empty})])
    move_initial(data, "germany-army-0", "C")
    move_initial(data, "germany-army-1", "D")
    game = make(data)
    state = game.state
    resolver = ScoreResolver(game.rules)
    assert resolver.sites(state, "germany") == {"hidden:g-status:g-extra:0": 2, "hidden:g-status:g-extra:1": 1}
    ZoneRegistry.transfer(state, "united-kingdom-army-0", "B")
    assert resolver.sites(state, "germany") == {"hidden:g-status:g-extra:0": 2}
