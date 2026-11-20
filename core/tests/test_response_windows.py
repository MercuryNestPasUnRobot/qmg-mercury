import json

import pytest

from conftest import card, choose, make, move_initial, play
from qmg_core import CoreAPI, CoreError, OperationLogger, SaveRepository
from qmg_core.content_effects import instruction
from qmg_core.model import TURN_ORDER
from qmg_core.rules import validate_state


def battle(target):
    return {"type": "LAND_BATTLE", "target": target}


def build(target):
    return {"type": "BUILD_ARMY", "target": target}


def add_listener(data, source_id, name, effects, trigger, *, kind="bolster", mode="OPTIONAL", priority=10):
    definition = card(data, source_id)
    owner = definition["country"]
    source = next(row for row in data["initial_tokens"] if row["definition_id"] == source_id)
    scope = {"bolster": "HAND", "response": "RESPONSE", "status": "STATUS"}[kind]
    definition.update(type={"bolster": "EVENT", "response": "RESPONSE", "status": "STATUS"}[kind],
                      legacy_kind=kind, abilities=[name], effects=[])
    move_initial(data, source["token_id"], f"{owner}:{scope}")
    data["abilities"].append({"id": name, "activation_mode": mode, "after": "origin", "priority": priority,
        "listener_scope": [scope], "trigger": trigger, "effects": effects})
    return f"ability:{source['token_id']}:{name}"


AFTER_BATTLE = {"operation_type": "LAND_BATTLE"}
BEFORE_BATTLE = {"operation_type": "ANNOUNCE", "announced_type": "LAND_BATTLE", "actor": "enemy"}


def moves(p):
    return [(r["operation_type"], r["operation"]["target"], r["source"]) for r in p.records
            if r["operation_type"] in ("LAND_BATTLE", "BUILD_ARMY") and r["outcome"]["success"]]


def assert_closed(p):
    s = p.state
    validate_state(p.rules, s)
    assert {key: (t.definition_id, t.token_type, t.owner_country, t.source_token_id) for key, t in s.tokens.items()} == {
        key: (t.definition_id, t.token_type, t.owner_country, t.source_token_id) for key, t in p.rules.inventory.items()}
    assert not s.stack and not s.decision and not s.announcements and not s.positions["RESOLUTION"]
    assert all(not s.positions[f"{seat}:ABILITY_PENDING"] for seat in TURN_ORDER)
    for record in p.records:
        for change in record["outcome"]["changes"]:
            assert change["token_id"] in s.tokens and change["from"] in s.positions and change["to"] in s.positions


def test_battle_response_builds_before_original_card_continues(data):
    card(data, "g-chain")["effects"] = [battle("B"), build("C")]
    trigger = {"operation_type": "CARD_EVENT", "card_trigger": {"type": "battle-space", "actor": "self", "kind": "army"}}
    add_listener(data, "g-draw", "extra-build", [build("B")], trigger)
    p = make(data)
    play(p, "g-chain")
    assert p.state.tokens["g-draw"].position_id == "germany:HAND"
    assert p.state.tokens["g-chain"].position_id == "RESOLUTION"
    assert moves(p) == [("LAND_BATTLE", "B", "g-chain")]
    choose(p, "use")
    assert moves(p) == [("LAND_BATTLE", "B", "g-chain"), ("BUILD_ARMY", "B", "g-draw"), ("BUILD_ARMY", "C", "g-chain")]
    assert p.state.tokens["g-draw"].position_id == "germany:DISCARD"
    assert_closed(p)


@pytest.mark.parametrize("repeat_battle", [False, True])
def test_active_player_picks_one_response_and_new_battle_opens_new_timing(tmp_path, data, repeat_battle):
    card(data, "g-chain")["effects"] = [battle("B")]
    # B and D are both eligible battle targets. Building on B is also legal after the first battle.
    data["connections"].append({"id": "test-edge-ad", "a": "A", "b": "D"})
    repeat_id = add_listener(data, "g-draw", "extra-battle", [battle("D")], AFTER_BATTLE)
    build_id = add_listener(data, "g-noop", "extra-build", [build("B")], AFTER_BATTLE)
    checkpoint, journal = tmp_path / "state.json", tmp_path / "ops.jsonl"
    p = make(data, checkpoint_path=checkpoint, logger=OperationLogger(journal))
    play(p, "g-chain")
    decision = p.state.decision
    assert decision.decision_type == "RESPONSE_ORDER"
    assert set(decision.legal_options) == {repeat_id, build_id, "decline"}
    enemy = CoreAPI(p, ("united-kingdom",))
    assert enemy.observe()["decision"] == {"required_country": "germany"}
    assert repeat_id not in json.dumps(enemy.observe()) and build_id not in json.dumps(enemy.observe())
    assert not enemy.legal_commands()
    working = CoreAPI(p, ("germany",))
    old = next(cmd for cmd in working.legal_commands() if cmd["option"] == build_id)
    p = SaveRepository.resume(checkpoint, p.rules, journal)
    choose(p, repeat_id if repeat_battle else build_id)
    if repeat_battle:
        assert [row[:2] for row in moves(p)] == [("LAND_BATTLE", "B"), ("LAND_BATTLE", "D")]
        assert p.state.decision.legal_options == ["decline", "use"]
        assert p.state.decision.decision_id != decision.decision_id
        with pytest.raises(CoreError, match="stale"):
            CoreAPI(p, ("germany",)).submit(old)
        choose(p, "use")
    expected = [("LAND_BATTLE", "B", "g-chain")]
    if repeat_battle:
        expected.append(("LAND_BATTLE", "D", "g-draw"))
    expected.append(("BUILD_ARMY", "B", "g-deck-2"))
    assert moves(p) == expected
    assert SaveRepository.recover(checkpoint, p.rules, journal).state == p.state
    assert_closed(p)


def test_active_limit_spans_priorities_but_automatic_listeners_still_run(data):
    card(data, "g-chain")["effects"] = [battle("B")]
    add_listener(data, "g-draw", "first", [{"type": "GAIN_VP", "amount": 2}], AFTER_BATTLE, priority=5)
    add_listener(data, "g-noop", "second", [{"type": "GAIN_VP", "amount": 7}], AFTER_BATTLE, priority=15)
    add_listener(data, "g-status", "automatic", [{"type": "GAIN_VP", "amount": 3}], AFTER_BATTLE, kind="status", mode="AUTO", priority=20)
    p = make(data)
    play(p, "g-chain")
    choose(p, "use")
    assert p.state.scores["axis"] == 5
    assert p.state.tokens["g-deck-2"].position_id == "germany:HAND"
    assert_closed(p)


def test_nonactive_player_orders_equal_responses_and_both_can_resolve(data):
    card(data, "g-chain")["effects"] = [battle("B"), build("B")]
    a = add_listener(data, "uk-guard", "first", [{"type": "GAIN_VP", "amount": 2}], AFTER_BATTLE, kind="status")
    b = add_listener(data, "uk-secret", "second", [{"type": "GAIN_VP", "amount": 7}], AFTER_BATTLE, kind="response")
    p = make(data)
    play(p, "g-chain")
    assert p.state.decision.required_country == "united-kingdom"
    assert set(p.state.decision.legal_options) == {a, b, "decline"}
    choose(p, b)
    assert p.state.scores["allies"] == 7
    assert p.state.decision.required_country == "united-kingdom"
    assert p.state.tokens["g-chain"].position_id == "RESOLUTION"
    choose(p, "use")
    assert p.state.scores["allies"] == 9
    assert moves(p)[-1] == ("BUILD_ARMY", "B", "g-chain")
    assert_closed(p)


def test_equal_priority_between_seats_follows_turn_order(data):
    card(data, "g-chain")["effects"] = [battle("B")]
    data["cards"].append({"id": "soviet-response", "country": "soviet-union", "type": "EVENT",
                          "effects": [], "abilities": [], "failure_policy": "stop"})
    data["initial_tokens"].append({"token_id": "soviet-response", "definition_id": "soviet-response",
                                   "token_type": "CARD", "owner_country": "soviet-union", "position_id": "soviet-union:HAND"})
    # Reverse registration order so that comparison depends on the seats, not input order.
    add_listener(data, "soviet-response", "later-seat", [{"type": "GAIN_VP", "amount": 7}], AFTER_BATTLE)
    add_listener(data, "uk-secret", "earlier-seat", [{"type": "GAIN_VP", "amount": 2}], AFTER_BATTLE, kind="response")
    p = make(data)
    play(p, "g-chain")
    assert p.state.decision.required_country == "united-kingdom"
    choose(p, "use")
    assert p.state.decision.required_country == "soviet-union"
    choose(p, "use")
    assert [r["actor"] for r in p.records if r["operation_type"] == "GAIN_VP"] == ["united-kingdom", "soviet-union"]
    assert_closed(p)


def test_any_time_response_cannot_reuse_the_same_suspended_timing(data):
    card(data, "g-chain")["effects"] = [
        {"type": "CHOOSE", "operation": build("$choice")},
        {"type": "CHOOSE", "operation": build("$choice")},
    ]
    trigger = {"operation_type": "CARD_EVENT", "card_trigger": {"type": "any-time"}}
    add_listener(data, "g-draw", "first", [{"type": "GAIN_VP", "amount": 2}], trigger, kind="response")
    add_listener(data, "g-noop", "second", [{"type": "GAIN_VP", "amount": 3}], trigger, kind="response")
    p = make(data)
    play(p, "g-chain")
    client = CoreAPI(p, ("germany",))
    original_timing = p.state.decision.originating_operation_id
    client.submit(next(c for c in client.legal_commands() if c["type"] == "ACTIVATE_CARD" and c["card"] == "g-draw"))
    assert p.state.decision.originating_operation_id == original_timing
    assert all(c["type"] != "ACTIVATE_CARD" for c in client.legal_commands())
    forged = {"type": "TRANSFER", "actor": "germany", "expected_version": p.state.version,
              "token": "germany-army-1", "destination": "C"}
    with pytest.raises(CoreError, match="illegal"):
        client.submit(forged)
    choose(p, "B")
    assert p.state.decision.originating_operation_id != original_timing
    client.submit(next(c for c in client.legal_commands() if c["type"] == "ACTIVATE_CARD" and c["card"] == "g-deck-2"))
    choose(p, "C")
    assert p.state.scores["axis"] == 5
    assert_closed(p)


def test_later_response_revalidates_target_after_nested_response(data):
    card(data, "g-chain")["effects"] = [battle("B")]
    first = add_listener(data, "uk-guard", "first", [build("B")], AFTER_BATTLE, kind="status")
    # Another legal build before the first one resolves would try to use the same site.
    second = add_listener(data, "uk-secret", "second", [build("B")], AFTER_BATTLE, kind="response")
    add_listener(data, "uk-other", "nested", [{"type": "GAIN_VP", "amount": 4}],
                 {"operation_type": "BUILD_ARMY", "actor": "self"}, kind="response", priority=500)
    p = make(data)
    play(p, "g-chain")
    assert set(p.state.decision.legal_options) == {first, second, "decline"}
    choose(p, first)
    assert moves(p)[-1] == ("BUILD_ARMY", "B", "uk-status")
    assert p.state.decision.required_country == "united-kingdom"
    assert p.state.stack[-1].context["event_outcome"]["event"] == "BUILD_ARMY"
    choose(p, "use")
    assert p.state.scores["allies"] == 4
    assert p.state.tokens["uk-hidden"].position_id == "united-kingdom:RESPONSE"
    assert_closed(p)


def test_cancelled_announcement_drops_stale_responses_and_continues_card(data):
    card(data, "g-chain").update(failure_policy="continue", effects=[battle("B"), build("B")])
    add_listener(data, "uk-guard", "cancel", [instruction({"type": "prevent-removal"})], BEFORE_BATTLE, kind="status")
    add_listener(data, "uk-secret", "late", [{"type": "GAIN_VP", "amount": 5}], BEFORE_BATTLE, kind="response", priority=20)
    p = make(data)
    play(p, "g-chain")
    choose(p, "use")
    assert p.state.scores["allies"] == 0
    assert moves(p) == [("BUILD_ARMY", "B", "g-chain")]
    assert not any(r["operation_type"] == "LAND_BATTLE" and r["outcome"]["success"] for r in p.records)
    assert_closed(p)


def test_initial_deal_and_draw_both_arm_hand_listeners(data):
    card(data, "g-chain")["effects"] = [battle("B")]
    card(data, "g-build")["effects"] = [{"type": "DRAW", "count": 10}, battle("B")]
    ability = add_listener(data, "g-draw", "opening-response", [{"type": "GAIN_VP", "amount": 3}], AFTER_BATTLE)
    move_initial(data, "g-draw", "germany:DECK")
    data["settings"]["initial_hand"] = 12
    p = make(data)
    assert p.state.tokens[ability].position_id == "germany:ABILITY_READY"
    play(p, "g-chain")
    choose(p, "use")
    assert p.state.scores["axis"] == 3
    assert_closed(p)

    data["settings"]["initial_hand"] = 0
    p = make(data)
    assert p.state.tokens[ability].position_id == "germany:ABILITY_INACTIVE"
    play(p, "g-build")
    assert p.state.tokens[ability].position_id == "germany:ABILITY_READY"
    choose(p, "use")
    assert p.state.scores["axis"] == 3
    assert_closed(p)


def test_unlimited_hand_response_returns_to_the_hand_for_the_next_event(data):
    card(data, "g-chain")["effects"] = [battle("B"), battle("B")]
    ability = add_listener(data, "g-draw", "return-to-hand", [{"type": "GAIN_VP", "amount": 3}], AFTER_BATTLE)
    card(data, "g-draw")["after"] = "origin"
    p = make(data)
    play(p, "g-chain")
    choose(p, "use")
    assert p.state.decision.legal_options == ["decline", "use"]
    assert p.state.tokens["g-draw"].position_id == "germany:HAND"
    assert p.state.tokens[ability].position_id == "germany:ABILITY_READY"
    choose(p, "use")
    assert p.state.scores["axis"] == 6
    assert p.state.tokens["g-draw"].position_id == "germany:HAND"
    assert_closed(p)
