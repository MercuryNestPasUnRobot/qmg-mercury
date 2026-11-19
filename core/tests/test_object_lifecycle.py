import json
from dataclasses import replace

import pytest

from conftest import card, choose, finish_choices, make, move_initial, play
from qmg_core import CoreError, OperationLogger, SaveRepository, observation, validate_state
from qmg_core.api import simulation_from_observation
from qmg_core.model import TURN_ORDER, ZoneRegistry
from qmg_core.engine import CommandProcessor


def configure(data, after="delete", copies=1, renewal=False, actor="self"):
    move_initial(data, "g-status", "germany:STATUS")
    primary = data["abilities"][0]
    primary.pop("reset_policy")
    primary.update(after=after, copies=copies, activation_mode="AUTO", discard_cost=0)
    if renewal:
        primary["on_settle"] = [{"type": "CREATE", "token_type": "ABILITY", "definition": "next-use", "zone": "RESPONSE", "same_copy": True}]
        data["abilities"].append({
            "id": "next-use", "after": "delete", "activation_mode": "AUTO", "priority": 12,
            "trigger": {"operation_type": "TURN_END", "actor": actor},
            "effects": [{"type": "CREATE", "token_type": "ABILITY", "definition": "g-extra", "zone": "ABILITY_READY", "same_copy": True}],
        })
    card(data, "g-battle").update(after="origin", effects=[{"type": "LAND_BATTLE", "target": "B"}] * 3)


def end_phase(p, advance=True):
    command = next(c for c in p.legal_commands(TURN_ORDER) if c["type"] == "END_PHASE")
    p.submit(command, TURN_ORDER, auto_resolve=advance)
    if advance:
        finish_choices(p)


def close_turn(p, advance=True):
    while p.state.turn.phase != "Draw":
        end_phase(p)
    end_phase(p, advance)


@pytest.mark.parametrize("after,rewards", [("origin", 3), ("delete", 1)])
def test_unlimited_and_single_use_objects_complete_by_identity(data, after, rewards):
    configure(data, after)
    p = make(data)
    key, = p.state.positions["germany:ABILITY_READY"]
    play(p, "g-battle")
    uses = [r for r in p.records if r["operation_type"] == "OBJECT_SETTLED" and r["operation"].get("object_token_id") == key]
    assert len(uses) == rewards
    assert p.state.scores["axis"] == 2 * rewards
    destination = "germany:ABILITY_READY" if after == "origin" else "germany:OBJECT_POOL"
    assert all(r["outcome"]["changes"] == [{"token_id": key, "from": "RESOLUTION", "to": destination}] for r in uses)
    assert p.state.tokens[key].position_id == destination
    assert p.state.tokens["g-battle"].position_id == "germany:HAND"
    assert p.state.positions["germany:RESPONSE"] == []


def test_two_independent_uses_wait_resolve_recreate_and_repeat(tmp_path, data):
    configure(data, copies=2, renewal=True)
    # Keep this exercise's hand between seat turns.
    data["settings"].update(hand_limit=12, victory_margin=0, deck_exhaustion_vp=0)
    p = make(data)
    originals = set(p.state.positions["germany:ABILITY_READY"])
    assert len(originals) == 2
    play(p, "g-battle")
    listeners = set(p.state.positions["germany:RESPONSE"])
    assert len(listeners) == 2 and not p.state.positions["germany:ABILITY_READY"]
    assert originals <= set(p.state.positions["germany:OBJECT_POOL"])
    assert p.state.scores["axis"] == 4
    creations = [r for r in p.records if r["operation_type"] == "OBJECT_SETTLED"
                 and r["outcome"]["context"].get("settled_definition") == "g-extra"]
    assert {r["operation"]["object_token_id"] for r in creations} == originals
    for r in creations:
        deletion, creation = r["outcome"]["changes"]
        assert deletion["from"] == "RESOLUTION" and deletion["to"] == "germany:OBJECT_POOL"
        assert creation["from"] == "germany:OBJECT_POOL" and creation["to"] == "germany:RESPONSE"
    enemy = observation(p, ("united-kingdom",))
    assert enemy["positions"]["germany:RESPONSE"] == {"count": 2}
    assert enemy["positions"]["germany:OBJECT_POOL"] == {"count": 0}
    assert all(key not in json.dumps(enemy) for key in listeners)
    belief = simulation_from_observation(p.rules, observation(p, ("germany",)))
    assert not belief.state.positions["germany:ABILITY_READY"]
    assert set(belief.state.positions["germany:RESPONSE"]) == listeners

    close_turn(p, advance=False)
    while not listeners.intersection(p.state.positions["RESOLUTION"]):
        assert p.step() is not None
    assert p.state.turn.country_index == 0 and p.state.positions["germany:ABILITY_READY"] == []
    snapshot, journal = tmp_path / "pending.json", tmp_path / "journal.jsonl"
    SaveRepository.save(snapshot, p)
    p.logger = OperationLogger(journal)
    p.advance()
    replay = SaveRepository.resume(snapshot, p.rules, journal)
    assert replay.state.to_dict() == p.state.to_dict() and replay.record_head == p.record_head
    restarted = SaveRepository.recover(snapshot, p.rules)
    restarted.advance()
    assert restarted.record_head == p.record_head
    replacements = set(p.state.positions["germany:ABILITY_READY"])
    assert replacements == originals and replacements.isdisjoint(listeners)
    assert listeners <= set(p.state.positions["germany:OBJECT_POOL"])
    entries = [r for r in p.records if r["operation_type"] == "CREATE"]
    assert {r["operation"]["object_token_id"] for r in entries} == listeners
    paired = {r["outcome"]["changes"][1]["token_id"]: r["operation"]["object_token_id"] for r in creations}
    assert all(r["outcome"]["changes"][0]["token_id"] == paired[r["operation"]["object_token_id"]] for r in entries)
    assert all(r["turn_id"] == 1 and r["outcome"]["changes"][0]["token_id"] in replacements for r in entries)
    while p.state.turn.country_index:
        close_turn(p)
    before = p.state.scores["axis"]
    play(p, "g-battle")
    assert p.state.scores["axis"] - before == 4
    assert len(p.state.positions["germany:RESPONSE"]) == 2 and replacements <= set(p.state.positions["germany:OBJECT_POOL"])


def test_listener_waits_for_qualifying_end_and_source_loss_cancels_it(data):
    configure(data, renewal=True, actor="enemy")
    data["settings"].update(plays_per_turn=2, victory_margin=0)
    p = make(data)
    play(p, "g-battle")
    key, = p.state.positions["germany:RESPONSE"]
    close_turn(p)
    assert p.state.tokens[key].position_id == "germany:RESPONSE" and not p.state.positions["germany:ABILITY_READY"]
    close_turn(p)
    assert p.state.tokens[key].position_id == "germany:OBJECT_POOL" and len(p.state.positions["germany:ABILITY_READY"]) == 1
    card(data, "g-draw")["effects"] = [{"type": "TRANSFER", "token": "g-status", "destination": "germany:DISCARD"}]
    p = make(data)
    play(p, "g-battle")
    play(p, "g-draw")
    close_turn(p)
    close_turn(p)
    assert all(t.position_id == "germany:OBJECT_POOL" for t in p.state.tokens.values() if t.source_token_id == "g-status")


def test_explicitly_disposable_card_disappears_after_its_last_step(data):
    card(data, "g-chain")["after"] = "delete"
    p = make(data)
    play(p, "g-chain")
    assert p.state.tokens["g-chain"].position_id == "RESOLUTION"
    choose(p, "B")
    assert p.state.tokens["g-chain"].position_id == "germany:OBJECT_POOL" and p.state.scores["axis"] == 3
    assert p.records[-1]["outcome"]["changes"] == [{"token_id": "g-chain", "from": "RESOLUTION", "to": "germany:OBJECT_POOL"}]


def test_two_hand_copies_create_independent_response_cards_and_replacements(data):
    data["settings"]["plays_per_turn"] = 3
    data["settings"]["hand_limit"] = 12
    card(data, "g-build").update(after="delete", effects=[], on_settle=[
        {"type": "CREATE", "token_type": "CARD", "definition": "turn-reply", "zone": "RESPONSE"}])
    data["cards"].append({"id": "turn-reply", "country": "germany", "type": "RESPONSE", "after": "delete",
                          "effects": [], "abilities": ["new-card"], "failure_policy": "stop"})
    data["abilities"].append({"id": "new-card", "activation_mode": "AUTO", "after": "delete", "priority": 16,
                              "trigger": {"operation_type": "TURN_END", "actor": "self"},
                              "effects": [{"type": "CREATE", "token_type": "CARD", "definition": "g-build", "zone": "HAND"}]})
    for key in ("reply-1", "reply-2"):
        data["initial_tokens"].append({"token_id": key, "definition_id": "turn-reply", "token_type": "CARD",
                                       "owner_country": "germany", "position_id": "germany:OBJECT_POOL"})
    p = make(data)
    for key in ("g-build", "g-copy"):
        play(p, key)
        assert p.state.tokens[key].position_id == "germany:OBJECT_POOL"
    assert not any(p.state.tokens[c["card"]].definition_id == "g-build" for c in p.legal_commands(TURN_ORDER) if "card" in c)
    replies = set(p.state.positions["germany:RESPONSE"])
    assert len(replies) == 2
    close_turn(p)
    assert replies <= set(p.state.positions["germany:OBJECT_POOL"])
    returned = {t.token_id for t in p.state.tokens.values() if t.definition_id == "g-build" and t.position_id == "germany:HAND"}
    created = [r for r in p.records if r["operation_type"] == "CREATE" and r["source"] in replies]
    assert {r["source"] for r in created} == replies
    assert {r["outcome"]["changes"][0]["token_id"] for r in created} <= returned
    assert len(created) == 2 and {"g-build", "g-copy"} <= returned


def test_optional_hidden_response_stays_in_place_on_decline(data):
    configure(data, renewal=True)
    data["abilities"][-1]["activation_mode"] = "OPTIONAL"
    p = make(data)
    play(p, "g-battle")
    reply, = p.state.positions["germany:RESPONSE"]
    close_turn(p, advance=False)
    p.advance()
    assert p.state.tokens[reply].position_id == "germany:RESPONSE"
    enemy = observation(p, ("united-kingdom",))
    assert enemy["decision"] == {"required_country": "germany"}
    assert reply not in json.dumps(enemy) and not enemy["resolution"]
    choose(p, "decline")
    assert p.state.tokens[reply].position_id == "germany:RESPONSE" and not p.state.positions["germany:ABILITY_READY"]


def test_owner_turn_start_compatibility_listens_only_to_owner_play_entry(data):
    move_initial(data, "g-status", "germany:STATUS")
    data["abilities"][0].update(reset_policy="OWNER_TURN_START", activation_mode="AUTO", discard_cost=0)
    data["settings"].update(victory_margin=0, deck_exhaustion_vp=0)
    p = make(data)
    play(p, "g-battle")
    choose(p, "B")
    reply, = p.state.positions["germany:RESPONSE"]
    for _ in range(5):
        close_turn(p)
        assert p.state.tokens[reply].position_id == "germany:RESPONSE"
    close_turn(p)
    assert p.state.turn.country_index == 0 and p.state.tokens[reply].position_id == "germany:OBJECT_POOL"
    assert len(p.state.positions["germany:ABILITY_READY"]) == 1


def test_old_cooldown_checkpoint_continues_via_an_actual_listener(tmp_path, data):
    move_initial(data, "g-status", "germany:STATUS")
    p = make(data)
    state = p.state
    for country in TURN_ORDER:
        for key in state.positions.pop(country + ":OBJECT_POOL"):
            del state.tokens[key]
    ZoneRegistry.transfer(state, "ability:g-status:g-extra", "germany:ABILITY_COOLDOWN")
    file = tmp_path / "old.json"
    SaveRepository.save(file, CommandProcessor(p.rules, state, legacy_snapshot=True))
    p = SaveRepository.recover(file, p.rules)
    end_phase(p)
    key, = p.state.positions["germany:RESPONSE"]
    close_turn(p)
    assert p.state.tokens[key].position_id == "germany:OBJECT_POOL" and len(p.state.positions["germany:ABILITY_READY"]) == 1
    assert any(r["operation_type"] == "CREATE" and r["operation"]["object_token_id"] == key for r in p.records)


@pytest.mark.parametrize("op", [{"type": "CREATE", "token_type": "ABILITY", "definition": "bad", "zone": "RESPONSE"},
                                {"type": "CREATE", "token_type": "ABILITY", "definition": "g-extra", "zone": "HAND"}])
def test_invalid_object_creation_is_rejected_at_content_load(data, op):
    data["abilities"][0]["effects"] = [op]
    with pytest.raises(CoreError, match="Ability creation"):
        make(data)


def test_empty_pool_cannot_mint_or_reuse_an_active_card(data):
    card(data, "g-draw")["effects"] = [
        {"type": "CREATE", "token_type": "CARD", "definition": "g-noop", "zone": "HAND", "token": "only-slot"}
    ] * 2
    data["initial_tokens"].append({"token_id": "only-slot", "definition_id": "g-noop", "owner_country": "germany",
                                   "token_type": "CARD", "position_id": "germany:OBJECT_POOL"})
    p = make(data)
    keys = set(p.state.tokens)
    play(p, "g-draw")
    first, second = [r["outcome"] for r in p.records if r["operation_type"] == "CREATE"]
    assert first["changes"] == [{"token_id": "only-slot", "from": "germany:OBJECT_POOL", "to": "germany:HAND"}]
    assert not second["success"] and not second["changes"]
    assert p.state.positions["germany:HAND"].count("only-slot") == 1 and set(p.state.tokens) == keys


@pytest.mark.parametrize("damage", ["missing", "extra", "redefined", "pool_removed"])
def test_closed_inventory_rejects_changes_even_if_positions_agree(data, damage):
    p = make(data)
    s = p.state
    pool = s.positions["germany:OBJECT_POOL"]
    key = pool[0]
    if damage == "missing":
        pool.remove(key)
        del s.tokens[key]
    elif damage == "extra":
        pool.append("undeclared")
        s.tokens["undeclared"] = replace(s.tokens[key], token_id="undeclared")
    elif damage == "redefined":
        s.tokens[key] = replace(s.tokens[key], definition_id="g-extra")
    else:
        for country in TURN_ORDER:
            for key in s.positions.pop(country + ":OBJECT_POOL"):
                del s.tokens[key]
    with pytest.raises(CoreError):
        validate_state(p.rules, s)
