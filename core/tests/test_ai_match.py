from copy import deepcopy
import json
from multiprocessing import Event
from pathlib import Path
from time import perf_counter

from conftest import card, make
from qmg_core import CoreAPI, RuleSet
from qmg_core.api import simulation_from_observation
from qmg_core import ai_match_policy
from qmg_core.ai_match import run_matches
from qmg_core.ai_match_policy import CandidateView, DeadlineSearch, SeatPolicy
from qmg_core.evaluation import OneStepPlanner
from qmg_core.model import TURN_ORDER


def profile():
    return json.loads(Path("data/ai_test_weights.json").read_text(encoding="utf8"))


def fixed_capture_profile():
    weights = forecast_search_profile()
    weights["action_distance_scoring"] = False
    return weights


def forecast_search_profile():
    weights = profile()
    weights["settlement_scoring"] = False
    weights["action_opportunity_scoring"] = False
    return weights


def test_worker_uses_same_request_for_different_hidden_cards_and_deck_order(data):
    other = deepcopy(data)
    next(t for t in other["initial_tokens"] if t["token_id"] == "uk-hidden")["definition_id"] = "uk-other"
    p, q = make(data, game_id="s1"), make(other, seed=1000, game_id="s1000")
    assert p.state.positions["germany:DECK"] != q.state.positions["germany:DECK"]
    requests, results = [], []
    for engine in (p, q):
        client = CoreAPI(engine, ("germany",))
        view = client.observe(include_history=False)
        command = next(c for c in client.legal_commands() if c.get("card") == "g-draw")
        search = DeadlineSearch(engine.rules, profile())
        requests.append((search.data, {key: value for key, value in view.items() if key != "game_id"}, command))
        before = engine.state.to_dict()
        try:
            rows, status = search.run(view, [command], "germany", perf_counter() + 5)
        finally:
            search.close()
        assert status == "finished" and rows[0]["complete"]
        assert engine.state.to_dict() == before
        results.append(rows)
    assert requests[0] == requests[1]
    assert results[0] == results[1]
    assert requests[0][0]["initial_tokens"] == []
    assert requests[0][1]["positions"]["germany:DECK"] == {"count": len(p.state.positions["germany:DECK"])}
    assert "uk-hidden" not in requests[0][1]["tokens"]
    assert requests[0][1]["history"] == []


def _stalled_worker(connection, data, weights):
    with connection:
        connection.send(("ready", None))
        view, commands, _, _ = connection.recv()
        assert view["game_id"] == "ai-projection" and not view["history"]
        connection.send(("candidate", {"command": commands[0], "complete": True, "q": 1}))
        Event().wait()


def test_deadline_retains_complete_result_and_retires_stalled_worker(data, monkeypatch):
    monkeypatch.setattr(ai_match_policy, "_search_worker", _stalled_worker)
    engine = make(data)
    client = CoreAPI(engine, ("germany",))
    search = DeadlineSearch(engine.rules, profile())
    view = client.observe(include_history=False)
    view["history"] = [{"source": "must-not-reach-worker"}]
    command = client.legal_commands()[0]
    try:
        rows, status = search.run(view, [command], "germany", perf_counter() + 2)
        assert status == "deadline"
        assert rows == [{"command": command, "complete": True, "q": 1}]
        assert search.process is None and search.connection is None
    finally:
        search.close()


def _cooperative_worker(connection, data, weights):
    with connection:
        connection.send(("ready", None))
        while True:
            _, commands, _, _ = connection.recv()
            connection.send(("candidate", {"command": commands[0], "complete": True, "q": 1}))
            connection.send(("deadline", None))


def test_cooperative_cutoff_retains_results_and_reuses_worker(data, monkeypatch):
    monkeypatch.setattr(ai_match_policy, "_search_worker", _cooperative_worker)
    engine = make(data)
    client = CoreAPI(engine, ("germany",))
    search = DeadlineSearch(engine.rules, profile())
    try:
        command = client.legal_commands()[0]
        rows, status = search.run(client.observe(), [command], "germany", perf_counter() + 3)
        process = search.process
        assert status == "deadline" and rows[0]["complete"]
        assert process is not None and process.is_alive()
        assert search.run(client.observe(), [command], "germany", perf_counter() + 3) == (rows, status)
        assert search.process is process
    finally:
        search.close()


def test_search_projection_settles_through_core_without_journal_work(data):
    engine = make(data)
    client = CoreAPI(engine, ("germany",))
    view = client.observe()
    full = simulation_from_observation(engine.rules, view)
    short = simulation_from_observation(engine.rules, view, search_projection=True)
    command = next(c for c in client.legal_commands() if c.get("card") == "g-draw")
    for p in (full, short):
        CoreAPI(p, ("germany",)).apply(command)
    assert short.state == full.state
    assert [r["operation_type"] for r in short.records] == [r["operation_type"] for r in full.records]
    assert all("record_digest" not in r for r in short.records)
    assert all("record_digest" in r for r in full.records)


def test_search_rebinds_same_choice_window_and_uses_a_when_it_changes(data):
    # Only this chain can win, after the pending choice that the test exercises.
    data["settings"]["victory_margin"] = 101
    card(data, "g-chain")["effects"].append({"type": "GAIN_VP", "amount": 100})
    engine = make(data)
    client = CoreAPI(engine, ("germany",))
    planner = OneStepPlanner(engine.rules, profile())

    class FixedSearch:
        def run(self, view, commands, country, deadline):
            assert view["decision"] is None
            return planner.rank(CandidateView(view, commands), country), "finished"

    ai = SeatPolicy(engine.rules, search=FixedSearch(), width=20)
    command, row = ai.decide(client, "germany")
    assert row["mode"] == "search" and command["card"] == "g-chain"
    client.submit(command, include_history=False)
    # Live counters can have advanced through operations absent from the projection.
    planned, signature, score = ai.plan[0]
    ai.plan[0] = {**planned, "decision_id": "decision:987"}, signature, score
    saved = deepcopy(ai.plan)
    live = client.observe(include_history=False)
    assert live["decision"]["decision_id"] != ai.plan[0][0]["decision_id"]
    command, row = ai.decide(client, "germany")
    assert row["mode"] == "plan" and command in client.legal_commands()
    ai.plan = saved
    planned, signature, score = ai.plan[0]
    ai.plan[0] = planned, ("other-kind", *signature[1:]), score
    fallback, row = ai.decide(client, "germany")
    rule_choice, _ = SeatPolicy(engine.rules).decide(client, "germany")
    assert fallback == rule_choice and row["search_status"] == "pending-choice"
    assert not ai.plan


def test_deadline_without_complete_candidate_uses_identical_rule_choice(data):
    class NoResult:
        def run(self, view, commands, country, deadline):
            return [], "deadline"

    engine = make(data)
    client = CoreAPI(engine, ("germany",))
    a, _ = SeatPolicy(engine.rules).decide(client, "germany")
    b, row = SeatPolicy(engine.rules, search=NoResult()).decide(client, "germany")
    assert a == b
    assert row["mode"] == "rules" and row["search_status"] == "deadline"
    assert row["rule_scores"][0]["command"] == b
    assert row["candidate_scores"] and all(r["q"] is None for r in row["candidate_scores"])


def test_paired_match_reaches_rules_defined_end_with_all_six_seats(data, tmp_path):
    data["settings"].update(max_rounds=1, victory_margin=0)
    report = run_matches(RuleSet(data), profile(), seeds=[73], output=tmp_path, width=2, budget_ms=1000)
    results = report["games"]
    assert [game["search_team"] for game in results] == ["axis", "allies"]
    assert all(game["winner"] in ("draw", "axis", "allies") for game in results)
    assert all(results[0]["seats"][seat] != results[1]["seats"][seat] for seat in TURN_ORDER)
    assert report["metrics"]["overall"]["games"] == 2
    assert report["metrics"]["pairs"][0]["mean_vp_diff_b"] == sum(g["vp_diff_b"] for g in results) / 2
    for game in results:
        assert set(game["decisions_by_seat"]) == set(TURN_ORDER)
        assert min(game["decisions_by_seat"].values()) > 0
        vp = game["scores"][game["search_team"]]
        assert game["vp_diff_b"] == 2 * vp - sum(game["scores"].values())
        entries = [json.loads(line) for line in
                   (tmp_path / game["decision_log"]).read_text(encoding="utf8").splitlines()]
        assert len(entries) == sum(game["decisions_by_seat"].values())
        for index, entry in enumerate(entries, 1):
            assert entry["decision_index"] == index
            assert entry["country"] == entry["command"]["actor"]
            assert entry["elapsed_ms"] >= 0 and entry["budget_ms"] == 1000
            assert any(row["command"] == entry["command"] for row in entry["rule_scores"])
            if entry["policy"] == "A":
                assert not entry["candidate_scores"]
