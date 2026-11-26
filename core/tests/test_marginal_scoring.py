from types import SimpleNamespace

import pytest

from conftest import card, make, move_initial, play
from qmg_core import CoreAPI, ScoreEvaluator
from qmg_core.ai_match_policy import RulePolicy
from qmg_core.content_effects import instruction
from qmg_core.model import zone
from qmg_core.reallocation_scoring import extra_refill
from test_action_distance_scoring import forecast, map_with_sites
from test_ai_match import profile
from test_hand_retention import hand_only, reach_discard
from test_status_scoring import scoring_status


def test_discard_loses_all_reachable_sites_even_if_the_route_display_is_capped(data):
    map_with_sites(data, [("A", "B"), ("A", "C")], ("B", "C"))
    card(data, "g-build")["legacy_kind"] = "build-army"
    hand_only(data, "g-build", "g-deck-2")
    data["settings"].update(hand_limit=2, discard_phase_penalty=1)
    game = make(data)
    client = CoreAPI(game, ("germany",))
    reach_discard(client)
    weights = profile()
    weights["route_forecast"]["nearest_sites"] = 1
    evaluator = ScoreEvaluator(game.rules, weights)
    view = client.observe(include_history=False)
    rows = RulePolicy(game.rules, evaluator).rank(view, client.legal_commands())
    terms = next(row["retention"] for row in rows if row["command"].get("card") == "g-build")
    assert terms["slot_cost"] == 0
    assert terms["expansion_cost"] == 2 * weights["expansion_preference"]
    assert terms["discard_cost"] == pytest.approx(terms["short_term"] + terms["expansion_cost"])
    discards = [row for row in rows if row["command"]["type"] == "DISCARD_CARD"]
    assert discards[0]["command"].get("card") == "g-deck-2"


def test_exchange_counts_only_real_additional_refill_slots_and_available_cards():
    counts = {"HAND": 5, "DECK": 8}
    economy = SimpleNamespace(count=lambda _seat, area: counts[area], rules=SimpleNamespace(settings={"hand_limit": 5}))
    plan = {"source": "deck", "payment": ["a", "b", "c"]}
    assert extra_refill(economy, "germany", plan) == 2
    counts["DECK"] = 2
    assert extra_refill(economy, "germany", plan) == 0
    assert extra_refill(economy, "germany", {**plan, "source": "discard"}) == 1


def test_forecast_deck_exhaustion_uses_full_rules_penalty(data):
    data["settings"].update(deck_exhaustion_vp=-2, max_rounds=3, victory_margin=0)
    economy = forecast(data)
    deck = economy.count("germany", "DECK")
    assert economy.shortfall("germany", deck - 1) >= economy.shortfall("germany", deck)
    economy.count = lambda seat, area: 0 if area == "DECK" else 1
    missing = economy.shortfall("germany")
    assert missing > 0 and economy.future_loss("germany") == missing * 2


def test_reorder_exposes_only_the_seats_revealed_set_and_sacrifices_the_weaker_card(data):
    card(data, "g-draw")["effects"] = [instruction({"type": "order-deck-top", "count": 2}),
                                      instruction({"type": "discard-top", "target": "self", "count": 1})]
    card(data, "g-vp")["effects"] = [{"type": "GAIN_VP", "amount": 3}]
    hand_only(data, "g-draw")
    revealed = {"g-deck-1", "g-deck-2"}
    data["initial_tokens"] = [token for token in data["initial_tokens"] if token["position_id"] != "germany:DECK" or
                              token["token_id"] in revealed]
    results, actual_orders = [], set()
    for seed in (1, 1000, 2000, 3000, 4000):
        game = make(data, seed=seed)
        actual_orders.add(tuple(game.state.positions[zone("germany", "DECK")]))
        play(game, "g-draw")
        client = CoreAPI(game, ("germany",))
        view = client.observe(include_history=False)
        order = view["decision"]["deck_order"]
        assert set(order["options"]) == revealed and order["known_operations"] == [("discard", 1)]
        assert "tokens" not in view["positions"][zone("germany", "DECK")]
        assert CoreAPI(game, ("united-kingdom",)).observe()["decision"] == {"required_country": "germany"}
        policy = RulePolicy(game.rules, ScoreEvaluator(game.rules, profile()))
        options = policy.rank(view, client.legal_commands())
        results.append((order, [row["command"]["option"] for row in options]))
    assert len(actual_orders) == 2
    assert all(result == results[0] for result in results)
    assert results[0][1][0] == "g-deck-2"


def test_ordering_follows_the_predicted_play_instead_of_always_putting_strength_first(data):
    card(data, "g-draw")["effects"] = [{"type": "GAIN_VP", "amount": 5},
                                      instruction({"type": "discard-top", "target": "self", "count": 1})]
    card(data, "g-vp")["effects"] = [{"type": "GAIN_VP", "amount": 4}]
    hand_only(data, "g-draw")
    game = make(data)
    client = CoreAPI(game, ("germany",))
    view = client.observe(include_history=False)
    evaluator = ScoreEvaluator(game.rules, profile())
    economy = evaluator.economic_from_view(view)
    policy = RulePolicy(game.rules, evaluator)
    order = {"selected": 0, "known_operations": [], "options": {"good": "g-vp", "weak": "g-noop"}}
    scores = policy.deck_order_scores(order, "germany", economy, view)
    assert scores["weak"] > scores["good"]
    scores = policy.deck_order_scores({**order, "selected": 1}, "germany", economy, view)
    assert scores["good"] > scores["weak"]


def test_conditional_points_use_matching_cards_plus_one_without_probability(data):
    ability = scoring_status(data, phase="after-build")
    ability["trigger"]["card_trigger"].update(kind="army", builder="self")
    map_with_sites(data, [("A", "B")], ())
    card(data, "g-build")["legacy_kind"] = "build-army"
    hand_only(data, "g-build")
    move_initial(data, "g-status", zone("germany", "STATUS"))
    values = forecast(data).statuses.portfolio("germany", True)
    assert len(values) == 2 and list(values.values()) == [1, 1]
    observer = forecast(data, "united-kingdom").statuses
    assert list(observer.portfolio("germany", True).values()) == [1]


def test_projected_site_adds_a_distance_and_protection_uses_build_value(data):
    ability = scoring_status(data, phase="before-battle-removal")
    ability["trigger"]["card_trigger"]["kind"] = "army"
    ability["effects"] = [instruction({"type": "prevent-removal"})]
    map_with_sites(data, [("B", "C")], ("C",), rounds=4)
    move_initial(data, "germany-army-1", "B")
    economy = forecast(data)
    position = economy.routes.position
    assert position.site_values("germany", "C", distance_offset=1)["C"][0] == 1
    assert position.value("germany", "C", distance_offset=1) == position.value("germany", "C") - 2
    status = economy.statuses
    assert status.program("germany", "united-kingdom", "g-extra", "B", committed=True) == position.value(
        "germany", "B", construction=True, excluded="germany-army-1")
    assert status.program("germany", "united-kingdom", "g-extra", None) == 2


def test_temporary_supply_only_values_the_before_after_difference(data):
    map_with_sites(data, [("B", "C")], ("C",), rounds=4)
    move_initial(data, "germany-army-1", "B")
    economy = forecast(data)
    routes = economy.routes
    effect = {"type": "supply-units-this-turn", "filter": {"kind": "army", "force": "self", "spaces": ["B"]}}
    assert routes.temporary_supply_value("germany", effect) == routes.protection_value("germany", "B", token="germany-army-1") > 0
    effect["filter"]["spaces"] = ["A"]
    assert routes.temporary_supply_value("germany", effect) == 0
