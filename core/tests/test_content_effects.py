import json
from pathlib import Path

import pytest

from conftest import card, choose, make, move_initial, play
from qmg_core import CommandProcessor, CoreAPI, CoreError, OperationLogger, RuleSet, SaveRepository
from qmg_core.catalog import ContentCatalog
from qmg_core.content_effects import instruction
from qmg_core.migration import compile_transcriptions
from qmg_core.rules import active_unit_rule
from qmg_core.model import TURN_ORDER


def test_branch_effect_targets_foreign_allied_units_then_resumes(data):
    move_initial(data, "italy-army-0", "italy:RESERVE_ARMY")
    card(data, "g-chain")["effects"] = [
        instruction({"type": "choose-one", "options": [
            {"label": "gain", "effects": [{"type": "points", "side": "self", "amount": 2}]},
            {"label": "deploy", "effects": [{"type": "choose-space", "force": "italy", "kind": "army",
                                            "action": "recruit", "homeRange": 0}]},
        ]}),
        instruction({"type": "points", "side": "enemy", "amount": 1}),
    ]
    p = make(data)
    play(p, "g-chain")
    assert p.state.decision.legal_options == ["0", "1"]
    choose(p, "1")
    assert p.state.decision.legal_options == ["H"]
    choose(p, "H")
    assert p.state.scores == {"axis": 0, "allies": 1}
    assert any(t.owner_country == "italy" and t.position_id == "H" for t in p.state.tokens.values())
    assert p.state.tokens["g-chain"].position_id == "germany:DISCARD"


def test_strict_card_cost_waits_for_all_choices_and_survives_restart(tmp_path, data):
    card(data, "g-chain")["effects"] = [
        instruction({"type": "discard-hand", "count": 2, "strict": True}),
        instruction({"type": "points", "side": "self", "amount": 3}),
    ]
    checkpoint, journal = tmp_path / "state.json", tmp_path / "ops.jsonl"
    p = make(data, checkpoint_path=checkpoint, logger=OperationLogger(journal))
    play(p, "g-chain")
    choose(p, "g-copy")
    assert p.state.tokens["g-copy"].position_id == "germany:HAND"
    p = SaveRepository.resume(checkpoint, p.rules, journal)
    assert "g-copy" not in p.state.decision.legal_options
    choose(p, "g-draw")
    assert p.state.tokens["g-copy"].position_id == p.state.tokens["g-draw"].position_id == "germany:DISCARD"
    paid = next(r for r in p.records if r["operation_type"] == "DISCARD_PAYMENT")
    assert len(paid["outcome"]["changes"]) == 2 and p.state.scores["axis"] == 3
    assert SaveRepository.recover(checkpoint, p.rules, journal).state == p.state


def test_hand_listener_requires_activation_then_resolves_physical_card(data):
    response = card(data, "g-draw")
    response.update(legacy_kind="bolster", abilities=["hand-listener"], effects=[])
    data["abilities"].append({
        "id": "hand-listener", "after": "delete", "activation_mode": "OPTIONAL", "priority": 88,
        "listener_scope": ["HAND"], "effects": [instruction({"type": "points", "side": "self", "amount": 2})],
        "trigger": {"operation_type": "CARD_EVENT", "card_trigger": {"type": "victory-start"}},
    })
    p = make(data)
    assert "g-draw" not in {c.get("card") for c in p.legal_commands(("germany",))}
    for _ in range(2):
        p.submit(next(c for c in p.legal_commands(("germany",)) if c["type"] == "END_PHASE"), ("germany",))
    assert p.state.decision.decision_type == "ABILITY"
    choose(p, "use")
    assert p.state.tokens["g-draw"].position_id == "germany:DISCARD"
    assert p.state.scores["axis"] == 3
    assert any(c["from"] == "germany:HAND" and c["to"] == "RESOLUTION"
               for r in p.records for c in r["outcome"]["changes"] if c["token_id"] == "g-draw")


def test_foreign_payment_defers_moves_until_confirm_and_resumes_parent(tmp_path, data):
    card(data, "g-chain")["effects"] = [
        instruction({"type": "request-hand-discard", "target": "united-kingdom", "min": 1, "max": 1}),
        instruction({"type": "points", "side": "self", "amount": 2}),
    ]
    p = make(data)
    play(p, "g-chain")
    assert p.state.decision.required_country == "united-kingdom"
    choose(p, "uk-hidden")
    assert p.state.tokens["uk-hidden"].position_id == "united-kingdom:HAND"
    assert p.state.decision.legal_options == ["confirm"]
    checkpoint = tmp_path / "pay.json"
    SaveRepository.save(checkpoint, p)
    p = SaveRepository.recover(checkpoint, p.rules)
    choose(p, "confirm")
    assert p.state.tokens["uk-hidden"].position_id == "united-kingdom:DISCARD"
    assert p.state.scores["axis"] == 2 and not p.state.stack


def test_transcribed_defense_waits_and_cancels_announced_removal(data):
    guard = next(a for a in data["abilities"] if a["id"] == "uk-prevent")
    guard.update(effects=[instruction({"type": "prevent-removal"})],
                 trigger={"operation_type": "CARD_EVENT", "card_trigger": {
                     "type": "before-any-removal", "victims": ["united-kingdom"], "kind": "army"}})
    card(data, "g-chain")["effects"] = [{"type": "ELIMINATE", "target_token": "united-kingdom-army-0"}]
    p = make(data)
    play(p, "g-chain")
    assert p.state.tokens["united-kingdom-army-0"].position_id == "E"
    assert not p.state.announcements
    assert any(r["operation_type"] == "CANCEL" for r in p.records)


def test_temporary_protection_is_a_response_that_expires_at_turn_end(data):
    data["settings"]["unit_responses"] = ["unit-protection"]
    data["abilities"].append({"id": "unit-protection", "activation_mode": "AUTO", "after": "delete",
                              "priority": 500, "effects": [], "trigger": {"operation_type": "TURN_END"}})
    guard = next(a for a in data["abilities"] if a["id"] == "uk-prevent")
    guard["effects"] = [instruction({"type": "protect-unit-this-turn"})]
    card(data, "g-chain")["effects"] = [{"type": "ELIMINATE", "target_token": "united-kingdom-army-0"}] * 2
    p = make(data)
    initial_ids = set(p.state.tokens)
    play(p, "g-chain")
    assert p.state.tokens["united-kingdom-army-0"].position_id == "E"
    assert active_unit_rule(p.state, "united-kingdom-army-0", "unit-protection")
    for _ in range(5):
        p.submit(next(c for c in p.legal_commands(TURN_ORDER) if c["type"] == "END_PHASE"), TURN_ORDER)
        while p.state.decision:
            choose(p, p.state.decision.legal_options[0])
    assert not active_unit_rule(p.state, "united-kingdom-army-0", "unit-protection")
    assert set(p.state.tokens) == initial_ids and p.state.turn.country_index == 1


def test_economic_target_responses_adjust_discard_before_any_card_moves(data):
    card(data, "g-chain").update(legacy_kind="economic", effects=[
        instruction({"type": "discard-top", "target": "united-kingdom", "count": 3})])
    guard = next(a for a in data["abilities"] if a["id"] == "uk-prevent")
    guard.update(activation_mode="OPTIONAL", effects=[instruction({"type": "economic-target-adjust", "amount": -2})],
                 trigger={"operation_type": "CARD_EVENT", "card_trigger": {"type": "economic-target"}})
    p = make(data)
    before_deck = p.state.positions["united-kingdom:DECK"]
    play(p, "g-chain")
    assert p.state.decision.required_country == "united-kingdom"
    assert p.state.positions["united-kingdom:DECK"] == before_deck
    choose(p, "use")
    assert len(p.state.positions["united-kingdom:DECK"]) == len(before_deck) - 1
    assert p.state.scores["allies"] == 0
    events = [r["operation_type"] for r in p.records]
    assert events.index("ECONOMIC_TARGET") < events.index("ECONOMIC_RESPONSE") < events.index("TRANSFER")


def test_extra_play_uses_core_frame_preserves_parent_and_does_not_spend_a_second_play(data):
    card(data, "g-chain")["effects"] = [
        instruction({"type": "play-from-zone", "zone": "hand", "kinds": ["event"]}),
        instruction({"type": "points", "side": "self", "amount": 2}),
    ]
    card(data, "g-draw").update(legacy_kind="event", effects=[{"type": "GAIN_VP", "amount": 1}])
    p = make(data)
    play(p, "g-chain")
    choose(p, "g-draw")
    assert p.state.scores["axis"] == 3 and not p.state.stack and p.state.turn.plays_remaining == 0
    assert p.state.tokens["g-draw"].position_id == p.state.tokens["g-chain"].position_id == "germany:DISCARD"
    extra = next(r for r in p.records if r["operation"]["type"] == "PLAY_EXTRA_CARD")
    assert extra["source"] == "g-draw"


def test_active_status_uses_ready_copy_and_spends_normal_play(data):
    move_initial(data, "g-status", "germany:STATUS")
    card(data, "g-status").update(active_ability="active-use", abilities=["active-use"])
    data["abilities"].append({"id": "active-use", "activation_mode": "AUTO", "after": "origin", "priority": 77,
                              "trigger": {"operation_type": "USE_STATUS"},
                              "effects": [instruction({"type": "points", "side": "self", "amount": 2})]})
    p = make(data)
    seat = CoreAPI(p, ("germany",))
    seat.submit(next(c for c in seat.legal_commands() if c["type"] == "USE_STATUS"))
    assert p.state.scores["axis"] == 2 and p.state.turn.plays_remaining == 0
    assert p.state.tokens["g-status"].position_id == "germany:STATUS"
    assert p.state.tokens["ability:g-status:active-use"].position_id == "germany:ABILITY_READY"


def test_any_time_response_suspends_and_refreshes_a_pending_choice(tmp_path, data):
    card(data, "g-draw").update(type="RESPONSE", effects=[], abilities=["interrupt"])
    data["abilities"].append({"id": "interrupt", "activation_mode": "OPTIONAL", "after": "delete", "priority": 85,
        "trigger": {"operation_type": "CARD_EVENT", "card_trigger": {"type": "any-time"}},
        "effects": [{"type": "TRANSFER", "token": "germany-army-0", "destination": "germany:RESERVE_ARMY"}]})
    move_initial(data, "g-draw", "germany:RESPONSE")
    p = make(data)
    play(p, "g-chain")
    client = CoreAPI(p, ("germany",))
    old_choice = next(c for c in client.legal_commands() if c.get("option") == "B")
    interrupt = next(c for c in client.legal_commands() if c["type"] == "ACTIVATE_CARD")
    client.submit(interrupt)
    assert p.state.decision.legal_options == ["A"]
    with pytest.raises(CoreError, match="stale"):
        client.submit(old_choice)
    assert p.state.tokens["g-draw"].position_id == "germany:DISCARD"
    path = tmp_path / "interrupted.json"
    SaveRepository.save(path, p)
    p = SaveRepository.recover(path, p.rules)
    choose(p, "A")
    assert not p.state.stack and p.state.scores["axis"] == 3 and p.state.turn.plays_remaining == 0


def test_area_supply_applies_to_later_deployments_and_expires(data):
    from qmg_core.rules import SupplyResolver
    effect = {"type": "supply-units-this-turn", "filter": {"force": "self", "spaces": ["D"]}}
    data["settings"]["area_responses"] = {"g-chain": ["area-supply"]}
    data["abilities"].append({"id": "area-supply", "activation_mode": "AUTO", "after": "delete",
                              "priority": 500, "effects": [], "trigger": {"operation_type": "TURN_END"},
                              "area_rule": {"mode": "supply", "filter": effect["filter"]}})
    card(data, "g-chain")["effects"] = [instruction(effect), {
        "type": "TRANSFER", "token": "germany-army-1", "destination": "D"}]
    p = make(data)
    play(p, "g-chain")
    assert "germany-army-1" in SupplyResolver(p.rules).supplied(p.state, "germany")
    for _ in range(5):
        p.submit(next(c for c in p.legal_commands(TURN_ORDER) if c["type"] == "END_PHASE"), TURN_ORDER)
        while p.state.decision:
            choose(p, p.state.decision.legal_options[0])
    assert "germany-army-1" not in SupplyResolver(p.rules).supplied(p.state, "germany")


def test_bolster_cancellation_precedes_printed_cost(data):
    card(data, "g-draw").update(legacy_kind="bolster", abilities=["bolster"], effects=[])
    data["abilities"].append({"id": "bolster", "activation_mode": "OPTIONAL", "after": "delete", "priority": 80,
        "listener_scope": ["HAND"], "trigger": {"operation_type": "CARD_EVENT", "card_trigger": {"type": "victory-start"}},
        "effects": [instruction({"type": "discard-hand", "count": 2, "strict": True}),
                    instruction({"type": "points", "side": "self", "amount": 4})]})
    defense = next(a for a in data["abilities"] if a["id"] == "uk-prevent")
    defense.update(trigger={"operation_type": "CARD_EVENT", "card_trigger": {
        "type": "before-card-effect", "kind": "bolster", "actor": "enemy"}},
        effects=[instruction({"type": "cancel-card-effect"})])
    p = make(data)
    for _ in range(2):
        p.submit(next(c for c in p.legal_commands(TURN_ORDER) if c["type"] == "END_PHASE"), TURN_ORDER)
    choose(p, "use")
    assert p.state.tokens["g-copy"].position_id == p.state.tokens["g-build"].position_id == "germany:HAND"
    assert p.state.tokens["g-draw"].position_id == "germany:DISCARD"
    assert not any(r["operation_type"] == "DISCARD_PAYMENT" for r in p.records)
    assert p.state.scores["axis"] == 1


def test_ordered_battle_preserves_original_unit_identity_through_a_response(tmp_path, data):
    data["settings"]["record_turn_start"] = True
    move_initial(data, "united-kingdom-army-0", "B")
    effect = {"type": "battle-series", "kind": "army", "min": 0, "max": 2,
              "filter": {"force": "enemy", "kind": "army"},
              "atTurnStartAdjacent": {"force": "self", "kind": "army"}}
    card(data, "g-chain")["effects"] = [instruction(effect), {"type": "GAIN_VP", "amount": 5}]
    guard = next(a for a in data["abilities"] if a["id"] == "uk-prevent")
    guard.update(trigger={"operation_type": "ANNOUNCE", "announced_type": "LAND_BATTLE", "actor": "enemy"},
                 effects=[{"type": "TRANSFER", "token": "united-kingdom-army-0", "destination": "united-kingdom:RESERVE_ARMY"},
                          {"type": "TRANSFER", "token": "united-kingdom-army-1", "destination": "B"}])
    p = make(data)
    play(p, "g-chain")
    assert p.state.decision.legal_options == ["united-kingdom-army-0", "confirm"]
    choose(p, "united-kingdom-army-0")
    file = tmp_path / "ordered.json"
    SaveRepository.save(file, p)
    p = SaveRepository.recover(file, p.rules)
    choose(p, "confirm")
    assert p.state.tokens["united-kingdom-army-1"].position_id == "B"
    battle = next(r for r in p.records if r["operation_type"] == "LAND_BATTLE")
    assert not battle["outcome"]["success"]
    assert p.state.scores["axis"] == 0


def test_local_transcription_export_preserves_all_cards_and_has_no_unimplemented_instructions(tmp_path):
    source = Path("../local-content/legacy-2026-11-19/content.json")
    if not source.is_file():
        pytest.skip("Local user transcription export is not installed")
    raw = json.loads(source.read_text(encoding="utf-8"))
    rules, report = compile_transcriptions(raw)
    assert len(rules.cards) == report["total_definitions"] == 276
    assert report["total_copies"] == 380 and report["executable"] == 276 and report["blocked"] == 0
    assert len(rules.force_controllers) == 8
    by_id = {c["id"]: c for c in raw["pack"]["cards"]}
    assert all(c["source_card"] == by_id[c["id"]] and c["text"] == by_id[c["id"]]["text"]
               for c in rules.cards.values())
    p = CommandProcessor.create(rules)
    assert len(p.state.tokens) == len(rules.inventory)
    assert p.state.decision.required_country == TURN_ORDER[0]
    opening_state = p.state
    starters = [t for t in opening_state.tokens.values() if t.token_type == "CARD"
                and t.position_id == f"{t.owner_country}:HAND"
                and rules.cards[t.definition_id].get("legacy_kind") == "bolster"]
    assert starters, "The fixed seed should deal real hand listeners"
    for source in starters:
        assert any(t.source_token_id == source.token_id and t.position_id == f"{source.owner_country}:ABILITY_READY"
                   for t in opening_state.tokens.values())
    database = tmp_path / "content.sqlite3"
    ContentCatalog.build(rules, database)
    loaded = ContentCatalog.load(database)
    assert loaded.data == rules.data and loaded.fingerprint == rules.fingerprint
    from qmg_core.workbench import Workbench
    weights = json.loads(Path("data/evaluation_weights.json").read_text(encoding="utf8"))
    page = Workbench(tmp_path / "match", loaded, weights).page().decode("utf8")
    assert "本地转录内容" in page and (tmp_path / "match" / "state.json").exists()


def test_foreign_chooser_waits_for_its_seat_and_resumes_source(data):
    move_initial(data, "italy-army-0", "italy:RESERVE_ARMY")
    card(data, "g-chain")["effects"] = [instruction({"type": "choose-one", "chooser": "italy", "options": [
        {"label": "deploy", "effects": [{"type": "choose-space", "chooser": "italy", "force": "italy",
                                          "action": "recruit", "kind": "army"}]}]}),
        instruction({"type": "points", "side": "self", "amount": 2})]
    p = make(data)
    play(p, "g-chain")
    assert p.state.decision.required_country == "italy"
    assert not any(c["type"] == "CHOOSE" for c in p.legal_commands(("germany",)))
    choose(p, "0")
    choose(p, "H")
    assert p.state.tokens["italy-army-0"].position_id == "H"
    assert p.state.scores["axis"] == 2 and p.state.tokens["g-chain"].position_id == "germany:DISCARD"


def test_revealed_discard_plays_one_card_and_returns_only_unused_originals(data):
    move_initial(data, "g-vp", "germany:DISCARD")
    move_initial(data, "uk-deck", "united-kingdom:DISCARD")
    card(data, "g-chain")["effects"] = [
        instruction({"type": "reveal-discard-cards", "countries": ["germany", "united-kingdom"]}),
        instruction({"type": "play-from-zone", "target": "united-kingdom", "zone": "discard", "revealedOnly": True}),
        instruction({"type": "return-revealed-discard"}),
    ]
    p = make(data)
    play(p, "g-chain")
    assert p.state.decision.required_country == "united-kingdom"
    choose(p, "uk-deck")
    assert p.state.tokens["uk-deck"].position_id == "united-kingdom:DISCARD"
    assert p.state.positions["germany:DECK"][0] == "g-vp"
    assert not p.state.stack


def test_country_loss_retries_when_removal_prevented_then_pays_deck_loss(data):
    card(data, "g-chain")["effects"] = [instruction({"type": "country-loss-choice", "count": 1,
        "target": "united-kingdom", "removeKinds": ["army"]})]
    p = make(data)
    play(p, "g-chain")
    choose(p, "united-kingdom-army-0")
    assert p.state.tokens["united-kingdom-army-0"].position_id == "E"
    assert p.state.decision.decision_type == "COUNTRY_LOSS"
    choose(p, "discard")
    assert p.state.tokens["uk-deck"].position_id == "united-kingdom:DISCARD"
    assert not p.state.stack
    removal = next(r for r in p.records if r["operation_type"] == "ANNOUNCE")
    assert removal["actor"] == "germany" and removal["source"] == "g-chain"


def test_economic_bonus_applies_once_per_target_in_a_multistep_card(data):
    data["settings"]["deck_exhaustion_vp"] = -1
    card(data, "g-chain").update(legacy_kind="economic", effects=[
        instruction({"type": "discard-top", "target": "united-kingdom", "count": 1}),
        instruction({"type": "discard-top", "target": "united-kingdom", "count": 1}),
    ])
    card(data, "g-draw").update(legacy_kind="bolster", effects=[], abilities=["economic"])
    data["abilities"].append({"id": "economic", "activation_mode": "OPTIONAL", "after": "delete", "priority": 92,
        "listener_scope": ["HAND"], "trigger": {"operation_type": "CARD_EVENT", "card_trigger": {"type": "economic-play", "actor": "self"}},
        "effects": [instruction({"type": "economic-discard-bonus", "amount": 2})]})
    p = make(data)
    play(p, "g-chain")
    choose(p, "use")
    # Four total losses: one real deck card and three points, rather than six losses.
    assert p.state.scores["allies"] == -3


def test_discard_stage_listener_scores_only_actual_matching_hand_moves(data):
    card(data, "g-build")["legacy_kind"] = "build-army"
    card(data, "g-status").update(legacy_kind="status", abilities=["discard-score"])
    move_initial(data, "g-status", "germany:STATUS")
    data["settings"]["hand_limit"] = 7
    data["abilities"].append({"id": "discard-score", "activation_mode": "OPTIONAL", "after": "origin", "priority": 94,
        "trigger": {"operation_type": "CARD_EVENT", "card_trigger": {"type": "discard-stage-card", "kind": "build-army", "country": "germany"}},
        "effects": [instruction({"type": "points", "side": "self", "amount": 1})]})
    p = make(data)
    while p.state.turn.phase != "Discard":
        p.submit(next(c for c in p.legal_commands(TURN_ORDER) if c["type"] == "END_PHASE"), TURN_ORDER)
    choose(p, "g-build")
    assert p.state.decision.decision_type == "ABILITY"
    choose(p, "use")
    assert p.state.scores["axis"] == 2


def test_unknown_effect_qualifier_is_rejected_instead_of_silently_ignored(data):
    card(data, "g-chain")["effects"] = [instruction({"type": "points", "amount": 2, "side": "self", "typo": True})]
    with pytest.raises(CoreError, match="Unimplemented"):
        make(data)


def test_extra_play_alternate_mode_executes_chosen_action_instead_of_empty_effects(data):
    card(data, "g-draw").update(play_as=["BUILD_ARMY", "LAND_BATTLE"], effects=[])
    card(data, "g-chain")["effects"] = [instruction({"type": "play-from-zone", "zone": "hand"})]
    p = make(data)
    play(p, "g-chain")
    choose(p, "g-draw")
    assert p.state.decision.decision_type == "EXTRA_CARD_MODE"
    choose(p, "BUILD_ARMY")
    choose(p, "B")
    assert any(t.owner_country == "germany" and t.position_id == "B" for t in p.state.tokens.values())
    assert p.state.tokens["g-draw"].position_id == "germany:DISCARD" and p.state.turn.plays_remaining == 0


@pytest.mark.parametrize("program", ["AB", "ABC", "ACA"])
def test_combined_card_preserves_every_step_and_repeated_steps(data, program):
    effects = {
        "A": {"type": "points", "side": "self", "amount": 2},
        "B": {"type": "choose-space", "action": "recruit", "kind": "army", "spaces": ["B"]},
        "C": {"type": "discard-top", "target": "united-kingdom", "count": 1},
    }
    card(data, "g-chain")["effects"] = [instruction(effects[letter]) for letter in program]
    processor = make(data)
    play(processor, "g-chain")
    if "B" in program:
        assert processor.state.decision.legal_options == ["B"]
        choose(processor, "B")
        assert processor.state.tokens["germany-army-1"].position_id == "B"
    if "C" in program:
        assert processor.state.tokens["uk-deck"].position_id == "united-kingdom:DISCARD"
    operations = {"A": "GAIN_VP", "B": "RECRUIT", "C": "TRANSFER"}
    assert [r["operation_type"] for r in processor.records if r["operation_type"] in operations.values()] == [
        operations[letter] for letter in program]
    assert processor.state.scores["axis"] == 2 * program.count("A")


@pytest.mark.parametrize("condition_met", [False, True])
def test_step_condition_does_not_hide_card_or_skip_unconditional_suffix(data, condition_met):
    if condition_met:
        move_initial(data, "germany-army-1", "B")
    condition = {"type": "unit-count", "filter": {"force": "self", "kind": "army", "spaces": ["B"]}, "min": 1}
    card(data, "g-chain").update(effects=[
        instruction({"type": "points", "side": "self", "amount": 3, "when": condition}),
        instruction({"type": "points", "side": "self", "amount": 2})])
    processor = make(data)
    play(processor, "g-chain")
    assert processor.state.scores["axis"] == (5 if condition_met else 2)
    assert any(r["operation_type"] == "EFFECT_SKIPPED" for r in processor.records) != condition_met


@pytest.mark.parametrize("condition_met", [False, True])
def test_conditional_status_can_deploy_then_checks_the_condition_at_its_listener(data, condition_met):
    if condition_met:
        move_initial(data, "germany-army-1", "B")
    condition = {"type": "unit-count", "filter": {"force": "self", "kind": "army", "spaces": ["B"]}, "min": 1}
    card(data, "g-status").update(legacy_kind="status", content_condition=condition, abilities=["score-if-present"])
    data["abilities"].append({"id": "score-if-present", "activation_mode": "OPTIONAL", "after": "origin", "priority": 19,
        "trigger": {"operation_type": "CARD_EVENT", "card_trigger": {"type": "victory-start"}},
        "effects": [instruction({"type": "points", "side": "self", "amount": 3})]})
    processor = make(data)
    play(processor, "g-status")
    assert processor.state.tokens["g-status"].position_id == "germany:STATUS"
    for _ in range(2):
        processor.submit(next(c for c in processor.legal_commands(("germany",)) if c["type"] == "END_PHASE"), ("germany",))
    if condition_met:
        assert processor.state.decision.decision_type == "ABILITY"
        choose(processor, "use")
    else:
        assert processor.state.decision is None
    assert processor.state.scores["axis"] == (4 if condition_met else 1)


def test_catalog_audit_detects_dropped_or_reordered_execution_even_when_source_metadata_matches(data):
    from qmg_core.content_audit import compare_card
    source = {"id": "g-chain", "kind": "event", "country": "germany", "name": "Compound test", "text": "Test",
              "effects": [{"type": "points", "side": "self", "amount": amount} for amount in (2, 3, 2)]}
    definition = card(data, "g-chain")
    definition.update(legacy_kind=source["kind"], title=source["name"], text=source["text"], source_card=source,
                      implementation={"status": "executable"}, effects=[instruction(e) for e in source["effects"]])
    assert compare_card(source, RuleSet(data))["errors"] == []
    definition["effects"][0], definition["effects"][1] = definition["effects"][1], definition["effects"][0]
    assert compare_card(source, RuleSet(data))["errors"] == ["effects[0]", "effects[1]"]
    definition["effects"].pop()
    assert "effects size" in compare_card(source, RuleSet(data))["errors"]


def test_new_search_resets_the_scope_even_when_the_player_takes_no_cards(data):
    card(data, "g-chain")["effects"] = [
        instruction({"type": "search-deck", "min": 1, "max": 1}),
        instruction({"type": "search-deck", "min": 0, "max": 1}),
        instruction({"type": "play-from-zone", "zone": "hand", "fromLastSearchOnly": True, "optional": True}),
        instruction({"type": "points", "side": "self", "amount": 2}),
    ]
    processor = make(data)
    play(processor, "g-chain")
    choose(processor, "g-deck-1")
    choose(processor, "done")
    assert processor.state.decision.legal_options == ["done"]
    choose(processor, "done")
    assert processor.state.tokens["g-deck-1"].position_id == "germany:HAND"
    assert processor.state.scores["axis"] == 2


@pytest.mark.parametrize("available_in_hand", [False, True])
def test_active_cost_needs_a_target_and_can_make_that_target_by_discarding(data, available_in_hand):
    move_initial(data, "g-status", "germany:STATUS")
    card(data, "g-status").update(legacy_kind="status", abilities=["active-cost"], active_ability="active-cost")
    card(data, "g-build")["legacy_kind"] = "build-army" if available_in_hand else "event"
    data["abilities"].append({"id": "active-cost", "activation_mode": "AUTO", "after": "origin", "priority": 19,
        "trigger": {"operation_type": "USE_STATUS"}, "effects": [
            instruction({"type": "discard-hand", "count": 1}),
            instruction({"type": "play-from-zone", "zone": "discard", "kinds": ["build-army"]}),
        ]})
    processor = make(data)
    client = CoreAPI(processor, ("germany",))
    commands = [c for c in client.legal_commands() if c["type"] == "USE_STATUS"]
    if not available_in_hand:
        assert commands == [] and processor.state.turn.plays_remaining == 1
        return
    client.submit(commands[0])
    choose(processor, "g-build")
    choose(processor, "g-build")
    choose(processor, "B")
    assert processor.state.tokens["germany-army-1"].position_id == "B"
    assert processor.state.tokens["g-build"].position_id == "germany:DISCARD"
    assert processor.state.turn.plays_remaining == 0
