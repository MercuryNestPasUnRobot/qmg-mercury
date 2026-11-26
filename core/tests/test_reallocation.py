from copy import deepcopy

import pytest

from conftest import card, choose, finish_choices, make, move_initial
from test_air_and_content_actions import end_phase, marker
from qmg_core import CoreAPI
from qmg_core.ai_match_policy import CandidateView, RulePolicy, SeatPolicy
from qmg_core.evaluation import OneStepPlanner
from qmg_core.model import TURN_ORDER, zone


def add_card(data, seat, suffix, position, *, kind="other", effects=()):
    key = f"{seat}-{suffix}"
    data["cards"].append({"id": key, "country": seat, "type": "EVENT", "legacy_kind": kind,
                          "failure_policy": "stop", "effects": list(effects), "abilities": []})
    data["initial_tokens"].append({"token_id": key, "definition_id": key, "token_type": "CARD",
                                   "owner_country": seat, "position_id": zone(seat, position)})
    return key


def enable(data, seats):
    data["settings"].update(reallocation=True, victory_margin=0, max_rounds=2)
    for seat in seats:
        marker(data, "reallocation-opportunity-" + seat, seat)


def execute(client, kind):
    client.submit(next(c for c in client.legal_commands() if c["type"] == kind), include_history=False)


def test_all_six_seats_can_pay_three_and_take_one_non_air_basic_at_opening(data):
    enable(data, TURN_ORDER)
    for seat in TURN_ORDER:
        add_card(data, seat, "basic", "DECK", kind="build-army")
        add_card(data, seat, "basic-2", "DECK", kind="sea-battle")
        add_card(data, seat, "air-power-test", "DECK", kind="air-power")
        for n in range(3):
            add_card(data, seat, f"pay-{n}", "HAND")
    p = make(data)
    initial_inventory = set(p.state.tokens)
    for index, seat in enumerate(TURN_ORDER):
        assert p.state.turn.country_index == index and p.state.turn.phase == "Play"
        client = CoreAPI(p, (seat,))
        hand = zone(seat, "HAND")
        before_hand = len(p.state.positions[hand])
        execute(client, "REALLOCATE")
        assert p.state.decision.required_country == seat
        for n in range(3):
            choose(p, f"{seat}-pay-{n}")
        assert p.state.decision.legal_options == ["confirm", "cancel"]
        choose(p, "confirm")
        choose(p, "0")
        assert p.state.decision.legal_options == [seat + "-basic", seat + "-basic-2"]
        choose(p, seat + "-basic")
        assert p.state.tokens[seat + "-basic"].position_id == hand
        assert len(p.state.positions[hand]) == before_hand - 2
        assert p.state.turn.plays_remaining == p.rules.settings["plays_per_turn"]
        assert not any(c["type"] == "REALLOCATE" for c in client.legal_commands())
        assert set(p.state.tokens) == initial_inventory
        while p.state.turn.country_index == index:
            end_phase(p)
            finish_choices(p)
        assert p.state.tokens["opportunity:reallocation-opportunity-" + seat].position_id == zone(seat, "ABILITY_READY")


def test_cancelling_is_free_and_normal_play_closes_exchange_window(data):
    enable(data, ("germany",))
    move_initial(data, "g-build", "germany:DECK")
    card(data, "g-build")["legacy_kind"] = "build-army"
    p = make(data)
    client = CoreAPI(p, ("germany",))
    before = p.state.positions
    execute(client, "REALLOCATE")
    choose(p, "g-draw")
    choose(p, "cancel")
    assert p.state.positions == before
    assert any(c["type"] == "REALLOCATE" for c in client.legal_commands())
    client.submit(next(c for c in client.legal_commands() if c.get("card") == "g-vp"))
    assert p.state.turn.phase == "Play"
    assert not any(c["type"] == "REALLOCATE" for c in client.legal_commands())


def test_discard_recovery_status_enforces_a_funded_target_even_with_no_basic_in_deck(data):
    enable(data, ("germany",))
    card(data, "g-build")["legacy_kind"] = "build-army"
    card(data, "g-status")["modifiers"] = [{"kind": "reallocate_discard", "scope": "all", "country": "germany"}]
    move_initial(data, "g-status", "germany:STATUS")
    p = make(data)
    client = CoreAPI(p, ("germany",))
    execute(client, "REALLOCATE")
    choose(p, "g-draw")
    choose(p, "g-vp")
    assert p.state.decision.legal_options == ["g-build", "g-copy", "cancel"]
    choose(p, "g-build")
    choose(p, "confirm")
    assert p.state.decision.legal_options == ["1"]
    choose(p, "1")
    deck, rng = p.state.positions["germany:DECK"], p.state.rng
    choose(p, "g-build")
    assert p.state.tokens["g-build"].position_id == "germany:HAND"
    assert (p.state.positions["germany:DECK"], p.state.rng) == (deck, rng)


def test_exchange_scores_same_observation_and_only_the_payer_sees_search_targets(data, weights):
    enable(data, ("germany",))
    add_card(data, "germany", "basic", "DECK", kind="build-army",
             effects=[{"type": "CHOOSE", "operation": {"type": "BUILD_ARMY", "target": "$choice"}}])
    add_card(data, "germany", "basic2", "DECK", kind="build-navy")
    other = deepcopy(data)
    next(t for t in other["initial_tokens"] if t["token_id"] == "uk-hidden")["definition_id"] = "uk-other"
    p, q = make(data), make(other, seed=1000)
    assert p.state.positions["germany:DECK"] != q.state.positions["germany:DECK"]
    client, alternate = (CoreAPI(processor, ("germany",)) for processor in (p, q))
    view = client.observe()
    assert view == alternate.observe()
    assert view["reallocation"]["deck"] == {"build-army": 1, "build-navy": 1, "land-battle": 0, "sea-battle": 0}
    assert view["positions"]["germany:DECK"] == {"count": 4}
    initial = p.state
    policy = RulePolicy(p.rules)
    a = next(r for r in policy.rank(view, client.legal_commands()) if r["command"]["type"] == "REALLOCATE")
    scores = []
    for seat in (client, alternate):
        b = next(r for r in OneStepPlanner(p.rules, weights).rank(seat, "germany")
                 if r["command"]["type"] == "REALLOCATE")
        assert b["complete"] and not b["continuations"]
        assert b["q"] == pytest.approx(sum(b[key] for key in ("actual_vp_delta", "map_delta", "card_delta", "resource_delta")))
        scores.append(b)
    assert scores[0] == scores[1]
    for row in (a, scores[0]):
        plan = row["reallocation"]
        assert len(plan["payment"]) == 3 and plan["discard_score"] <= 0
        assert "deck_score" not in plan
        assert plan["total"] == pytest.approx(plan["play_score"] + plan["discard_score"])
    assert p.state == initial
    execute(client, "REALLOCATE")
    for _ in range(3):
        step = policy.rank(client.observe(), client.legal_commands())[0]["command"]
        client.submit(step)
    choose(p, "confirm")
    choose(p, "0")
    assert client.observe()["reallocation"]["targets"] == {"germany-basic": "germany-basic", "germany-basic2": "germany-basic2"}
    opposing = CoreAPI(p, ("united-kingdom",)).observe()
    assert "reallocation" not in opposing and opposing["decision"] == {"required_country": "germany"}
    assert client.observe()["positions"]["germany:DECK"] == {"count": 4}


def test_projected_payment_retires_a_listener_bound_to_a_paid_hand_card(data, weights):
    enable(data, ("germany",))
    payment = {"g-draw", "g-vp", "g-restore"}
    for token in data["initial_tokens"]:
        if token["position_id"] == "germany:HAND" and token["token_id"] not in payment:
            token["position_id"] = "germany:DECK"
    card(data, "g-restore")["legacy_kind"] = "bolster"
    card(data, "g-build")["legacy_kind"] = "build-army"
    p = make(data)
    client = CoreAPI(p, ("germany",))
    listener = "ability:g-restore:g-turn-restore"
    assert p.state.tokens[listener].position_id == "germany:ABILITY_READY"
    scored = next(row for row in OneStepPlanner(p.rules, weights).rank(client, "germany")
                  if row["command"]["type"] == "REALLOCATE")
    assert scored["complete"] and set(scored["reallocation"]["payment"]) == payment
    assert p.state.tokens[listener].position_id == "germany:ABILITY_READY"

    class ExchangeSearch:
        def run(self, view, commands, country, deadline):
            exchange = [c for c in commands if c["type"] == "REALLOCATE"]
            assert len(exchange) == 1
            return OneStepPlanner(p.rules, weights).rank(CandidateView(view, exchange), country), "finished"

    rule = RulePolicy(p.rules)
    exchange = next(row for row in rule.rank(client.observe(), client.legal_commands())
                    if row["command"]["type"] == "REALLOCATE")
    assert exchange["rule_score"] < -1
    policy = SeatPolicy(p.rules, search=ExchangeSearch(), width=2)
    chosen, report = policy.decide(client, "germany")
    assert chosen["type"] == "REALLOCATE" and report["mode"] == "search"
    client.submit(chosen)
    while p.state.decision:
        step, report = policy.decide(client, "germany")
        assert report["search_status"] == "matched-exchange" and step in client.legal_commands()
        client.submit(step)
    assert p.state.tokens["g-build"].position_id == "germany:HAND"
    assert all(p.state.tokens[key].position_id == "germany:DISCARD" for key in payment)


def test_exchange_compares_all_basics_and_rebinds_the_actual_play_after_payment(data):
    from test_ai_match import profile

    enable(data, ("germany",))
    data["settings"]["mandatory_turn_play"] = True
    for token in data["initial_tokens"]:
        if token["position_id"] == "germany:HAND":
            token["position_id"] = "germany:DECK"
    for n in range(4):
        add_card(data, "germany", f"exchange-junk-{n}", "HAND")
    card(data, "g-battle")["legacy_kind"] = "land-battle"
    card(data, "g-build")["legacy_kind"] = "build-army"
    add_card(data, "germany", "exchange-navy", "DECK", kind="build-navy")
    add_card(data, "germany", "exchange-sea", "DECK", kind="sea-battle")
    move_initial(data, "g-status", "germany:STATUS")
    move_initial(data, "uk-status", "united-kingdom:DISCARD")
    move_initial(data, "united-kingdom-army-0", "B")
    ability = next(a for a in data["abilities"] if a["id"] == "g-extra")
    ability.pop("discard_cost", None)
    ability["effects"] = [{"type": "GAIN_VP", "amount": 8}]

    p = make(data)
    client = CoreAPI(p, ("germany",))
    weights = profile()
    planner = OneStepPlanner(p.rules, weights)
    alternatives = {}

    class InProcessSearch:
        def run(self, view, commands, country, deadline):
            rows = list(planner.iter_rank(CandidateView(view, commands), country))
            alternatives.update((r["reallocation"]["kind"], r) for r in rows if r.get("reallocation"))
            return rows, "finished"

    b = SeatPolicy(p.rules, evaluator=planner.evaluator, search=InProcessSearch())
    command, log = b.decide(client, "germany")
    assert command["type"] == "REALLOCATE"
    assert set(alternatives) == {"build-army", "build-navy", "land-battle", "sea-battle"}
    assert all(r["complete"] for r in alternatives.values())
    assert log["selected_reallocation"]["kind"] == "land-battle"
    battle = alternatives["land-battle"]
    assert battle["actual_vp_delta"] == 8
    assert battle["card_delta"] == -2
    assert battle["cycle_score"] == 2
    assert battle["action_cost"] == -2
    assert battle["q"] == pytest.approx(sum(battle[k] for k in
        ("actual_vp_delta", "map_delta", "card_delta", "resource_delta", "cycle_score", "action_cost")))
    assert any(r["command"]["type"] == "PLAY_CARD" and r["complete"] for r in log["candidate_scores"])

    class MissingDirectScore:
        def run(self, view, commands, country, deadline):
            return [battle], "deadline"

    fallback = SeatPolicy(p.rules, evaluator=planner.evaluator, search=MissingDirectScore())
    assert fallback.decide(client, "germany")[0]["type"] == "PLAY_CARD"

    client.apply(command)
    while p.state.decision:
        step, log = b.decide(client, "germany")
        assert log["search_status"] == "matched-exchange"
        client.apply(step)
    play, log = b.decide(client, "germany")
    assert play["type"] == "PLAY_CARD" and play["card"] == "g-battle"
    assert log["search_status"] == "matched-exchange-play"
    client.apply(play)
    step, log = b.decide(client, "germany")
    assert step["option"] == "B" and log["search_status"] == "matched-continuation"
    client.apply(step)
    response, _ = b.decide(client, "germany")
    assert response["option"] == "use"
    client.apply(response)
    assert p.state.scores["axis"] == 8
