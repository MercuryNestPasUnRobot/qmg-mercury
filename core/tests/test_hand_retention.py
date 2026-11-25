import json
from pathlib import Path

import pytest

from conftest import card, finish_choices, make, move_initial, play
from qmg_core import CoreAPI, CoreError, SaveRepository
from qmg_core.ai_match_policy import RulePolicy, SeatPolicy
from qmg_core.evaluation import OneStepPlanner, ScoreEvaluator, acceptable_play
from qmg_core.hand_retention import is_hand_choice, score_retention
from qmg_core.model import TURN_ORDER, zone
from qmg_core.replay import Tape
from test_air_and_content_actions import add_air
from test_reallocation import add_card, execute


@pytest.fixture
def profile():
    return json.loads(Path("data/ai_test_weights.json").read_text(encoding="utf-8"))


def hand_only(data, *keys):
    for token in data["initial_tokens"]:
        if token["position_id"] == "germany:HAND" and token["token_id"] not in keys:
            token["position_id"] = "germany:DECK"
    for key in keys:
        move_initial(data, key, "germany:HAND")


def reach_discard(client):
    while client.observe()["turn"]["phase"] != "Discard":
        execute(client, "END_PHASE")


def test_six_phase_turn_penalizes_skipping_discard_before_drawing(data):
    add_air(data)
    hand_only(data, "g-deck-2")
    data["settings"].update(hand_limit=1, discard_phase_penalty=1)
    p = make(data)
    client = CoreAPI(p, ("germany",))
    play(p, "g-deck-2")
    for phase in ("Play", "Air", "Supply", "Victory"):
        assert p.state.turn.phase == phase
        execute(client, "END_PHASE")
    assert p.state.turn.phase == "Discard"
    assert not client.observe()["turn"]["discarded_in_phase"]
    points = p.state.scores["axis"]
    tape = Tape(p)
    execute(client, "END_PHASE")
    assert p.state.turn.phase == "Draw"
    assert p.state.scores["axis"] == points - 1
    assert len(p.state.positions["germany:HAND"]) == 1
    operations = [r["operation"] for r in p.records]
    penalty = next(i for i, op in enumerate(operations) if op.get("reason") == "no-discard")
    draw = next(i for i, op in enumerate(operations) if op["type"] == "DRAW")
    assert penalty < draw
    tape.capture()
    assert any(e.get("reason") == "no-discard" for e in tape.frames[-1]["events"])


def test_discard_waives_only_the_active_turn_penalty_and_survives_recovery(data, tmp_path):
    hand_only(data, "g-build", "g-deck-2")
    data["settings"].update(hand_limit=1, discard_phase_penalty=1)
    p = make(data)
    client = CoreAPI(p, ("germany",))
    reach_discard(client)
    assert p.state.decision and not client.observe()["turn"]["discarded_in_phase"]
    finish_choices(p)
    assert client.observe()["turn"]["discarded_in_phase"]
    save = tmp_path / "after-discard.json"
    SaveRepository.save(save, p)
    p = SaveRepository.recover(save, p.rules)
    client = CoreAPI(p, ("germany",))
    assert client.observe()["turn"]["discarded_in_phase"]
    points = p.state.scores["axis"]
    execute(client, "END_PHASE")
    assert p.state.scores["axis"] == points
    assert not client.observe()["turn"]["discarded_in_phase"]
    execute(client, "END_PHASE")
    britain = CoreAPI(p, ("united-kingdom",))
    while p.state.turn.phase != "Discard":
        execute(britain, "END_PHASE")
        finish_choices(p)
    assert not britain.observe()["turn"]["discarded_in_phase"]
    execute(britain, "END_PHASE")
    assert any(r["actor"] == "united-kingdom" and r["operation"].get("reason") == "no-discard" for r in p.records)


def test_discard_ranking_counts_one_penalty_waiver_and_one_score_per_extra_draw(data, profile):
    hand_only(data, "g-deck-2", "g-build")
    data["settings"].update(hand_limit=2, discard_phase_penalty=1)
    card(data, "g-build")["legacy_kind"] = "build-army"
    p = make(data)
    client = CoreAPI(p, ("germany",))
    reach_discard(client)
    policy = RulePolicy(p.rules, ScoreEvaluator(p.rules, profile))
    before = policy.rank(client.observe(), client.legal_commands())
    assert before[0]["command"].get("card") == "g-deck-2"
    weak, basic = (next(row["retention"] for row in before if row["command"].get("card") == key)
                   for key in ("g-deck-2", "g-build"))
    assert weak["penalty_saved"] == 1
    assert weak["draw_cycle"] == pytest.approx(1 - (weak["short_term"] + basic["short_term"]) / 2)
    assert before[0]["rule_score"] == pytest.approx(1 + weak["draw_cycle"] - weak["net"])
    client.submit(before[0]["command"])
    after = policy.rank(client.observe(), client.legal_commands())
    remaining = next(row["retention"] for row in after if row["command"].get("card") == "g-build")
    assert remaining["penalty_saved"] == 0
    assert remaining["draw_cycle"] == pytest.approx(1 - basic["short_term"])


def test_voluntary_discard_settles_a_listener_and_draw_replaces_the_card(data, profile):
    profile["action_distance_scoring"] = False
    hand_only(data, "g-build", "g-deck-2")
    data["settings"]["hand_limit"] = 2
    card(data, "g-build")["legacy_kind"] = "build-army"
    card(data, "g-noop")["legacy_kind"] = "event"
    move_initial(data, "g-status", "germany:STATUS")
    listener = next(a for a in data["abilities"] if a["id"] == "g-extra")
    listener["trigger"] = {"operation_type": "CARD_EVENT",
                           "card_trigger": {"type": "discard-stage-card", "country": "germany", "kind": "event"}}
    listener.pop("discard_cost")
    listener["effects"] = [{"type": "GAIN_VP", "amount": 3}]
    p = make(data)
    client = CoreAPI(p, ("germany",))
    assert not any(c["type"] == "DISCARD_CARD" for c in client.legal_commands())
    reach_discard(client)
    scorer = ScoreEvaluator(p.rules, profile)

    class NoSearch:
        def run(self, *args):
            pytest.fail("Voluntary discards must use the calibrated hand score within the budget")

    a = SeatPolicy(p.rules, evaluator=scorer)
    b = SeatPolicy(p.rules, evaluator=scorer, search=NoSearch())
    command, log = b.decide(client, "germany")
    assert command == a.decide(client, "germany")[0]
    assert command["type"] == "DISCARD_CARD" and command["card"] == "g-deck-2"
    assert log["search_status"] == "hand-retention"
    assert log["rule_scores"][0]["retention"]["slot_cost"] == -0.55
    assert log["rule_scores"][0]["retention"]["net"] < 0
    with pytest.raises(CoreError, match="Unauthorized"):
        CoreAPI(p, ("united-kingdom",)).submit(command)
    points = p.state.scores["axis"]
    tape = Tape(p)
    client.submit(command)
    tape.capture(log)
    assert any(["g-deck-2", "germany:HAND", "germany:DISCARD"] in event["moves"]
               for event in tape.frames[-1]["events"])
    assert p.state.tokens["g-deck-2"].position_id == "germany:DISCARD"
    assert client.observe()["decision"]["decision_type"] == "ABILITY"
    client.submit(next(c for c in client.legal_commands() if c.get("option") == "use"))
    assert p.state.scores["axis"] == points + 3
    assert b.decide(client, "germany")[0]["type"] == "END_PHASE"
    execute(client, "END_PHASE")
    assert len(p.state.positions["germany:HAND"]) == 2


def test_basics_use_the_same_floor_in_all_six_seats_and_redundancy_needs_crowding(data, profile):
    data["settings"]["hand_limit"] = 7
    for seat in TURN_ORDER:
        for n in range(3):
            add_card(data, seat, f"retention-build-{n}", "HAND", kind="build-army")
        for n in range(4):
            add_card(data, seat, f"retention-event-{n}", "HAND")
    p = make(data)
    settings = profile["hand_retention"]
    by_seat = []
    for seat in TURN_ORDER:
        values = score_retention(p.rules, p.state, seat, settings, lambda _: 0.25)
        by_seat.append([values[f"{seat}-retention-build-{n}"]["net"] for n in range(3)])
    assert all(row == by_seat[0] for row in by_seat)
    assert by_seat[0][0] == pytest.approx(0.18)
    assert by_seat[0][1] < 0 and by_seat[0][2] < by_seat[0][1]

    data["settings"]["hand_limit"] = 99
    p = make(data)
    values = score_retention(p.rules, p.state, "united-states", settings, lambda _: 0.25)
    assert all(values[f"united-states-retention-build-{n}"]["net"] > 0 for n in range(3))
    useful = score_retention(p.rules, p.state, "united-states", settings, lambda _: 1.0)
    assert useful["united-states-retention-build-0"]["net"] == pytest.approx(0.45)


def test_deck_slots_are_reserved_before_voluntary_discard(data, profile):
    hand_only(data, "g-deck-2")
    data["settings"].update(hand_limit=2, discard_phase_penalty=1, deck_exhaustion_vp=-1)
    data["initial_tokens"] = [t for t in data["initial_tokens"]
                              if t["position_id"] != "germany:DECK" or t["token_id"] == "g-deck-1"]
    p = make(data)
    client = CoreAPI(p, ("germany",))
    reach_discard(client)
    policy = RulePolicy(p.rules, ScoreEvaluator(p.rules, profile))
    ranked = policy.rank(client.observe(), client.legal_commands())
    assert ranked[0]["command"]["type"] == "END_PHASE"
    discard = next(row for row in ranked if row["command"]["type"] == "DISCARD_CARD")
    assert discard["retention"]["net"] < 0
    assert discard["retention"]["replacement_available"] is False
    assert discard["retention"]["extra_draws"] == 0
    assert discard["retention"]["draw_cycle"] == 0
    assert discard["retention"]["exhaustion"] == -1
    assert discard["retention"]["penalty_saved"] == 1


def test_opening_forced_discard_can_keep_a_better_card_over_a_basic_for_a_later_seat(data, profile):
    data["settings"].update(setup_discard=1, initial_hand=1)
    for seat in TURN_ORDER:
        add_card(data, seat, "opening-basic", "HAND", kind="build-army")
        add_card(data, seat, "opening-priority", "HAND", effects=[{"type": "GAIN_VP", "amount": 18}])
        add_card(data, seat, "opening-draw", "DECK", effects=[{"type": "GAIN_VP", "amount": 20}])
    data["initial_tokens"] = [t for t in data["initial_tokens"] if t["position_id"] != "united-kingdom:HAND"
                              or "opening" in t["token_id"]]
    data["initial_tokens"] = [t for t in data["initial_tokens"] if t["position_id"] != "united-kingdom:DECK"
                              or "opening" in t["token_id"]]
    p = make(data)
    germany = CoreAPI(p, ("germany",))
    germany.submit(germany.legal_commands()[0])
    britain = CoreAPI(p, ("united-kingdom",))
    view = britain.observe()
    assert is_hand_choice(view, "united-kingdom") and view["turn"]["country_index"] == 0
    command, log = SeatPolicy(p.rules, evaluator=ScoreEvaluator(p.rules, profile)).decide(britain, "united-kingdom")
    assert log["rule_scores"][0]["retention"]["net"] > 0
    assert command["option"] == "united-kingdom-opening-basic"
    britain.submit(command)
    finish_choices(p)
    assert p.state.tokens["united-kingdom-opening-priority"].position_id == "united-kingdom:HAND"


def test_initial_charge_applies_to_play_scores_but_preserves_immediate_victory(data, profile):
    profile["action_distance_scoring"] = False
    hand_only(data, "g-deck-2", "g-vp")
    data["settings"]["victory_margin"] = 1
    p = make(data)
    client = CoreAPI(p, ("germany",))
    rule = RulePolicy(p.rules, ScoreEvaluator(p.rules, profile))
    a = next(r for r in rule.rank(client.observe(), client.legal_commands()) if r["command"].get("card") == "g-deck-2")
    assert a["rule_score"] < 0 and a["initial_card_score"] == -1
    rows = OneStepPlanner(p.rules, profile).rank(client, "germany")
    rows = {row["command"].get("card"): row for row in rows}
    assert rows["g-deck-2"]["q"] == pytest.approx(-1) and not acceptable_play(rows["g-deck-2"])
    assert rows["g-vp"]["terminal_rank"] == 2 and acceptable_play(rows["g-vp"])


def test_discard_score_depends_on_visible_hand_and_deck_count(data, profile):
    hand_only(data, "g-deck-2", "g-build")
    data["settings"].update(hand_limit=2, discard_phase_penalty=1)
    card(data, "g-build")["legacy_kind"] = "build-army"
    p, q = make(data, seed=31), make(data, seed=73)
    assert p.state.positions["germany:DECK"] != q.state.positions["germany:DECK"]
    clients = [CoreAPI(engine, ("germany",)) for engine in (p, q)]
    for client in clients:
        reach_discard(client)
    first, second = (client.observe(include_history=False) for client in clients)
    assert first == second
    policy = RulePolicy(p.rules, ScoreEvaluator(p.rules, profile))
    assert policy.rank(first, clients[0].legal_commands()) == policy.rank(second, clients[1].legal_commands())
