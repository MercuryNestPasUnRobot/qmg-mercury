import pytest

from conftest import card, choose, make, move_initial
from qmg_core import CoreAPI, ScoreResolver
from qmg_core.api import simulation_from_observation
from qmg_core.board_connections import apply_half_sites
from qmg_core.content_effects import instruction, target_regions
from qmg_core.definitions import PER_UNIT_SITE
from qmg_core.model import TURN_ORDER, zone
from qmg_core.rules import LegalActionGenerator, SupplyResolver, board_units
from qmg_core.settlement_scoring import SettlementScorer
from test_action_distance_scoring import forecast, map_with_sites
from test_status_scoring import scoring_status


def test_reviewed_map_defines_three_half_sites_and_preserves_the_us_inventory():
    regions = [{"id": p} for p in ("scandinavia", "north-africa", "hawaii")]
    initial = [{"token_id": "united-states-army-1", "position_id": "united-states"},
               {"token_id": "united-states-army-2", "position_id": "united-states:RESERVE_ARMY"},
               {"token_id": "united-states-army-3", "position_id": "united-states:RESERVE_ARMY"}]
    sites = [{"id": "score-hawaii", "region": "hawaii", "countries": list(TURN_ORDER),
              "requires_supply": True, "vp": 2}]
    apply_half_sites(regions, sites, initial)
    assert {site["region"] for site in sites} == {p["id"] for p in regions}
    assert all(site["vp"] == 1 and site["scoring"] == PER_UNIT_SITE and site["requires_supply"] for site in sites)
    assert [t["position_id"] for t in initial] == ["united-states", "hawaii", "united-states:RESERVE_ARMY"]
    assert regions[-1]["construction_requires_adjacent_for"] == ["united-states"]


def test_half_sites_supply_and_pay_each_surface_unit_including_same_seat_auxiliaries(data):
    map_with_sites(data, [], rounds=1)
    data["settings"].update(supply_revision=2, shared_site_scoring=True)
    data["score_sites"][0].update(scoring=PER_UNIT_SITE, vp=1, requires_supply=True)
    data["auxiliary_forces"] = [{"id": "aux", "controller": "germany"}]
    data["initial_tokens"].append({"token_id": "aux-army", "position_id": "C", "owner_country": "aux",
                                  "token_type": "UNIT", "definition_id": "army"})
    move_initial(data, "germany-army-0", "C")
    move_initial(data, "italy-army-0", "C")
    game = make(data)
    pieces = board_units(game.rules, game.state)
    assert SupplyResolver(game.rules).supplied_for_seat(game.state, "germany", pieces) >= {"aux-army", "germany-army-0"}
    scorer = ScoreResolver(game.rules)
    assert scorer.sites(game.state, "germany") == {"C": 2}
    assert scorer.sites(game.state, "italy") == {"C": 1}

    for _ in range(100):
        if game.state.winner:
            break
        commands = game.legal_commands(TURN_ORDER)
        command = next((c for c in commands if c["type"] == "END_PHASE"), commands[0])
        game.submit(command, TURN_ORDER)
    gains = [(row["actor"], row["operation"]["amount"]) for row in game.records
             if row["operation"].get("score_site") == "C"]
    assert gains == [("germany", 2), ("italy", 1)]


def test_construction_requires_a_supplied_adjacent_unit_even_for_fixed_card_targets_or_free_recruit(data):
    map_with_sites(data, [("E", "B"), ("C", "B")], ())
    data["settings"]["free_recruit"] = True
    target = next(r for r in data["regions"] if r["id"] == "B")
    target.update(home_country="germany", construction_requires_adjacent_for=["germany"])
    card(data, "g-status").setdefault("modifiers", []).append(
        {"kind": "build_region", "unit_kind": "ARMY", "region": "B"})
    move_initial(data, "g-status", zone("germany", "STATUS"))
    game = make(data)
    targets = LegalActionGenerator(game.rules)
    for operation, effect_type in (("BUILD_ARMY", "build"), ("RECRUIT", "recruit")):
        assert "B" not in targets.targets(game.state, "germany", {"type": operation})
        assert target_regions(game.rules, game.state, "germany", {"type": effect_type, "space": "B"}) == []
    assert "B" in targets.targets(game.state, "united-kingdom", {"type": "BUILD_ARMY"})
    assert "B" in targets.targets(game.state, "united-kingdom", {"type": "RECRUIT"})
    move_initial(data, "germany-army-0", "C")
    unsupplied = make(data)
    assert "B" not in LegalActionGenerator(unsupplied.rules).targets(unsupplied.state, "germany", {"type": "BUILD_ARMY"})
    del target["home_country"]
    next(r for r in data["regions"] if r["id"] == "C")["supply_source_for"] = "germany"
    data["settings"]["free_recruit"] = False
    supplied = make(data)
    for effect_type in ("build", "recruit"):
        assert target_regions(supplied.rules, supplied.state, "germany", {"type": effect_type, "space": "B"}) == ["B"]


@pytest.mark.parametrize("owner", ["germany", "united-kingdom"])
def test_a_route_influence_prices_each_army_using_its_own_score_rate(data, owner):
    map_with_sites(data, [("A", "B"), ("B", "C"), ("C", "E")])
    data["score_sites"][0].update(scoring=PER_UNIT_SITE, vp=1)
    token = "germany-army-1" if owner == "germany" else "united-kingdom-army-1"
    move_initial(data, token, "C")
    baseline = forecast(data).routes.position
    assert baseline.site_base("germany", "C", "C") == 2
    # The route uses the acting seat's reach, but the army contributes according to its own payout.
    card(data, "g-status").setdefault("modifiers", []).append(
        {"kind": "score_bonus", "site": "C", "amount": 1, "scope": "all", "force": owner})
    move_initial(data, "g-status", zone("germany", "STATUS"))
    position = forecast(data).routes.position
    assert position.site_base(owner, "C", "C") == 2 * baseline.site_base(owner, "C", "C")
    other = "united-kingdom" if owner == "germany" else "germany"
    before = baseline.value(other, "C", excluded=token)
    after = position.value(other, "C", excluded=token)
    assert after == before
    if owner == "germany":
        # Full points share their payout, so an ally's own bonus increases the deduction.
        data["score_sites"][0].update(scoring="regular", vp=2)
        position = forecast(data).routes.position
        assert position.value("italy", "C") == pytest.approx(1)
    else:
        assert position.value(other, "C") == baseline.value(other, "C")


def test_a_attack_denies_the_enemy_status_and_b_counts_it_once_per_window(data):
    map_with_sites(data, [("A", "B"), ("B", "C"), ("C", "E")])
    data["score_sites"][0].update(scoring=PER_UNIT_SITE, vp=1)
    scoring_status(data)["effects"] = [instruction(
        {"type": "points-per-unit", "side": "self", "amountPer": 1,
         "filter": {"force": "self", "kind": "army", "spaces": ["C"]}})]
    move_initial(data, "germany-army-1", "C")
    plain = make(data)
    without = forecast(data, seat="united-kingdom").routes.removal_value("germany-army-1")
    move_initial(data, "g-status", zone("germany", "STATUS"))
    game = make(data)
    with_status = forecast(data, seat="united-kingdom").routes.removal_value("germany-army-1")
    assert with_status - without == 2
    def settled(processor, seat):
        client = CoreAPI(processor, (seat,))
        projected = simulation_from_observation(processor.rules, client.observe(), search_projection=True)
        return SettlementScorer(forecast(data).units).value(projected, seat)

    assert settled(plain, "germany").score_points == ({"site": "C", "value": 1},)
    boosted = {row["site"]: row["value"] for row in settled(game, "germany").score_points}
    assert boosted == {"C": 1, "status:g-status": 1}
    assert {row["site"]: row["value"] for row in settled(game, "united-kingdom").score_points} == {
        "C": -1, "status:g-status": -1}

    data["settings"]["max_rounds"] = 1
    game = make(data)
    client = CoreAPI(game, ("germany",))
    while not game.state.decision:
        client.apply(next(c for c in client.legal_commands() if c["type"] == "END_PHASE"))
    choose(game, "use")
    end = settled(game, "germany")
    assert end.actual_vp == 2 and not end.score_points
