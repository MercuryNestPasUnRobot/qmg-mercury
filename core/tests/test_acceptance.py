from contextlib import contextmanager
from copy import deepcopy
from html.parser import HTMLParser
from http.client import HTTPConnection
import json
import subprocess
import sys
from threading import Thread
from urllib.parse import urlencode

import pytest

from conftest import card, choose, finish_choices, make, move_initial, play
from qmg_core import CoreError, MapGraph, OperationLogger, RuleSet, SaveRepository, SupplyResolver, observation
from qmg_core.catalog import ContentCatalog
from qmg_core.model import TURN_ORDER, zone
from qmg_core.workbench import Workbench, make_server


def add_response(data, operation, activation="OPTIONAL"):
    definition = card(data, "g-draw")
    definition.update(type="RESPONSE", effects=[], abilities=["response-trigger"])
    data["abilities"].append({
        "id": "response-trigger", "trigger": operation, "activation_mode": activation,
        "reset_policy": "NEVER", "priority": 99, "effects": [{"type": "GAIN_VP", "amount": 4}],
    })


def test_setting_declining_and_revealing_response_keep_information_boundaries(data):
    add_response(data, {"operation_type": "END_PHASE", "actor": "self"})
    p = make(data)
    play(p, "g-draw")
    assert p.state.tokens["g-draw"].position_id == "germany:RESPONSE"
    assert p.state.scores["axis"] == 0
    enemy = observation(p, ("united-kingdom",))
    assert enemy["positions"]["germany:RESPONSE"] == {"count": 1}
    assert "g-draw" not in json.dumps(enemy)
    for option in ("decline", "use"):
        p.submit(p.legal_commands(("germany",))[0], ("germany",))
        enemy = observation(p, ("united-kingdom",))
        assert enemy["decision"] == {"required_country": "germany"}
        assert "g-draw" not in json.dumps(enemy)
        choose(p, option)
    enemy = observation(p, ("united-kingdom",))
    assert enemy["tokens"]["g-draw"]["position_id"] == "germany:DISCARD"
    assert enemy["history"][0]["source"] is None  # A later reveal never rewrites private history.
    assert sum(r["source"] == "g-draw" and r["operation_type"] == "PAY_COST" for r in enemy["history"]) == 1
    assert p.state.scores["axis"] == 5  # Four from the response and one from the following Victory phase.
    assert p.state.tokens["ability:g-draw:response-trigger"].position_id == "germany:OBJECT_POOL"


def test_announcement_uses_actual_battle_victim_for_defensive_trigger(data):
    move_initial(data, "united-kingdom-army-0", "B")
    next(ability for ability in data["abilities"] if ability["id"] == "uk-prevent")["trigger"]["announced_type"] = "LAND_BATTLE"
    p = make(data)
    play(p, "g-battle")
    choose(p, "B")
    assert p.state.tokens["united-kingdom-army-0"].position_id == "B"
    assert next(r for r in p.records if r["operation_type"] == "LAND_BATTLE")["outcome"]["success"] is False


def test_winning_response_is_public_and_closes_unfinished_frames(data):
    add_response(data, {"operation_type": "END_PHASE", "actor": "self"}, "AUTO")
    data["settings"]["victory_margin"] = 4
    p = make(data)
    play(p, "g-draw")
    p.submit(p.legal_commands(("germany",))[0], ("germany",))
    assert p.state.winner == "axis" and p.state.scores == {"axis": 4, "allies": 0}
    assert not p.state.stack and not p.state.announcements and not p.state.decision
    assert p.state.tokens["g-draw"].position_id == "germany:DISCARD"
    assert p.state.tokens["ability:g-draw:response-trigger"].position_id == "germany:OBJECT_POOL"


def end_phase(p):
    p.submit(next(c for c in p.legal_commands(TURN_ORDER) if c["type"] == "END_PHASE"), TURN_ORDER)


def start_turn_end(p):
    while p.state.turn.phase != "Draw":
        end_phase(p)
        finish_choices(p)
    end_phase(p)


def test_turn_end_response_restores_original_unit_and_listener_finishes_before_next_country(tmp_path, data):
    add_response(data, {"operation_type": "TURN_END", "actor": "self"})
    data["abilities"][-1]["effects"] = [{
        "type": "TRANSFER", "token": "germany-army-2",
        "expected_from": "germany:RESERVE_ARMY", "destination": "B",
    }]
    move_initial(data, "g-status", "germany:STATUS")
    data["abilities"][0].update(trigger={"operation_type": "TRANSFER", "actor": "self"},
                                activation_mode="AUTO", discard_cost=0)
    checkpoint = tmp_path / "state.json"
    journal = tmp_path / "operations.jsonl"
    p = make(data, checkpoint_path=checkpoint, logger=OperationLogger(journal))
    play(p, "g-draw")
    start_turn_end(p)
    assert p.state.turn.turn_id == 1 and p.state.turn.phase == "Draw"
    assert p.state.decision.decision_type == "ABILITY"
    assert p.state.tokens["germany-army-2"].position_id == "germany:RESERVE_ARMY"
    assert "g-draw" not in json.dumps(observation(p, ("united-kingdom",)))
    assert sum(r["operation_type"] == "TURN_END" for r in p.records) == 1
    resumed = SaveRepository.resume(checkpoint, p.rules, journal)
    assert resumed.state.decision.decision_id == p.state.decision.decision_id
    choose(resumed, "use")
    assert resumed.state.tokens["germany-army-2"].position_id == "B"
    assert resumed.state.tokens["g-draw"].position_id == "germany:DISCARD"
    assert resumed.state.turn.turn_id == 2 and resumed.state.turn.phase == "Play"
    records = resumed.records
    restoration = next(r for r in records if r["operation_type"] == "TRANSFER")
    bonus = next(r for r in records if r["outcome"]["score_changes"] == [{"team": "axis", "amount": 2}])
    transition = next(r for r in records if r["operation_type"] == "BEGIN_PHASE" and r["actor"] == "united-kingdom")
    assert restoration["turn_id"] == bonus["turn_id"] == 1 and transition["turn_id"] == 2
    assert restoration["sequence_id"] < bonus["sequence_id"] < transition["sequence_id"]
    assert restoration["outcome"]["changes"] == [
        {"token_id": "germany-army-2", "from": "germany:RESERVE_ARMY", "to": "B"},
        {"token_id": "ability:g-status:g-extra", "from": "germany:ABILITY_READY", "to": "germany:ABILITY_PENDING"},
    ]
    assert SaveRepository.recover(checkpoint, p.rules, journal).state.to_dict() == resumed.state.to_dict()


def test_invalid_restore_is_logged_without_moving_existing_unit(data):
    add_response(data, {"operation_type": "TURN_END", "actor": "self"}, "AUTO")
    data["abilities"][-1]["effects"] = [{
        "type": "TRANSFER", "token": "germany-army-2",
        "expected_from": "germany:RESERVE_ARMY", "destination": "E",
    }]
    p = make(data)
    play(p, "g-draw")
    start_turn_end(p)
    result = next(r["outcome"] for r in p.records if r["operation_type"] == "TRANSFER")
    assert result["success"] is False and result["changes"] == []
    assert p.state.tokens["germany-army-2"].position_id == "germany:RESERVE_ARMY"
    assert p.state.turn.country_index == 1


def test_turn_end_restores_the_unit_removed_during_supply(data):
    add_response(data, {"operation_type": "TURN_END", "actor": "self"}, "AUTO")
    unit = "germany-army-2"
    move_initial(data, unit, "D")
    data["abilities"][-1]["effects"] = [{
        "type": "TRANSFER", "token": unit,
        "expected_from": "germany:RESERVE_ARMY", "destination": "D",
    }]
    p = make(data)
    play(p, "g-draw")
    end_phase(p)
    assert p.state.turn.phase == "Supply"
    assert p.state.tokens[unit].position_id == "germany:RESERVE_ARMY"
    start_turn_end(p)
    moves = [change for record in p.records for change in record["outcome"]["changes"]
             if change["token_id"] == unit]
    assert [(move["from"], move["to"]) for move in moves] == [
        ("D", "germany:RESERVE_ARMY"), ("germany:RESERVE_ARMY", "D"),
    ]
    assert p.state.tokens[unit].position_id == "D" and p.state.turn.turn_id == 2


def test_turn_end_response_can_wait_for_a_legal_restoration_destination(data):
    add_response(data, {"operation_type": "TURN_END", "actor": "self"}, "AUTO")
    data["abilities"][-1]["effects"] = [{"type": "CHOOSE", "operation": {
        "type": "TRANSFER", "token": "germany-army-2",
        "expected_from": "germany:RESERVE_ARMY", "destination": "$choice",
        "destinations": ["B", "C", "E", "S"],
    }}]
    p = make(data)
    play(p, "g-draw")
    start_turn_end(p)
    assert p.state.decision.legal_options == ["B", "C"]
    assert p.state.turn.turn_id == 1
    assert observation(p, ("united-kingdom",))["resolution"][-1]["source"] == "g-draw"
    choose(p, "C")
    assert p.state.tokens["germany-army-2"].position_id == "C"
    assert p.state.turn.turn_id == 2
    decision = next(record for record in p.records if record["operation_type"] == "DECISION_RESOLVED"
                    and record["source"] == "g-draw")
    assert decision["resolution_id"] is not None
    assert decision["outcome"]["context"]["choice"] == "C"


def test_turn_end_checks_victory_after_all_responses(data):
    add_response(data, {"operation_type": "TURN_END", "actor": "self"}, "AUTO")
    data["abilities"][-1]["effects"].append({"type": "GAIN_VP", "amount": -3})
    data["settings"]["victory_margin"] = 4
    p = make(data)
    play(p, "g-draw")
    start_turn_end(p)
    assert not p.state.winner
    assert p.state.scores["axis"] == 2 and p.state.turn.country_index == 1


def test_multiple_turn_end_responses_and_restart_preserve_order(tmp_path, data):
    add_response(data, {"operation_type": "TURN_END", "actor": "self"}, "AUTO")
    card(data, "g-status").update(type="RESPONSE", effects=[])
    move_initial(data, "g-status", "germany:RESPONSE")
    data["abilities"][0].update(trigger={"operation_type": "TURN_END", "actor": "self"},
                                activation_mode="AUTO", discard_cost=0)
    data["abilities"][0]["effects"] = [{
        "type": "TRANSFER", "token": "germany-army-2",
        "expected_from": "germany:RESERVE_ARMY", "destination": "B",
    }]
    data["abilities"][-1]["effects"] = [{
        "type": "TRANSFER", "token": "germany-army-2",
        "expected_from": "B", "destination": "C",
    }]
    p = make(data)
    play(p, "g-draw")
    while p.state.turn.phase != "Draw":
        end_phase(p)
        finish_choices(p)
    checkpoint = tmp_path / "same-state.json"
    SaveRepository.save(checkpoint, p)
    reloaded = SaveRepository.recover(checkpoint, p.rules)
    end_phase(p)
    end_phase(reloaded)
    assert p.state.tokens["germany-army-2"].position_id == "C"
    assert p.state.to_dict() == reloaded.state.to_dict()
    assert p.record_head == reloaded.record_head
    events = [r for r in p.records if r["operation_type"] in ("TURN_END", "TRANSFER")]
    assert [r["operation_type"] for r in events] == ["TURN_END", "TRANSFER", "TRANSFER"]
    assert [r["targets"]["destination"] for r in events[1:]] == ["B", "C"]
    assert all(r["turn_id"] == 1 for r in events)


def test_tagged_connections_and_status_change_share_effective_graph(data):
    data["connections"][0]["movement_or_supply_tags"] = ["build"]
    move_initial(data, "germany-army-1", "B")
    p = make(data)
    graph = MapGraph(p.rules)
    assert "B" in graph.neighbors("A", p.state, "germany", "build")
    assert "B" not in graph.neighbors("A", p.state, "germany", "battle")
    assert "germany-army-1" not in SupplyResolver(p.rules).supplied(p.state, "germany")
    card(data, "g-status")["modifiers"] = [{
        "kind": "connection", "a": "A", "b": "B", "enabled": True,
        "movement_or_supply_tags": ["supply"],
    }]
    p = make(data)
    play(p, "g-status")
    assert "germany-army-1" in SupplyResolver(p.rules).supplied(p.state, "germany")
    assert "B" not in MapGraph(p.rules).neighbors("A", p.state, "germany", "battle")


def test_initial_checkpoint_can_resume_before_first_setup_record(tmp_path, data):
    data["settings"].update(initial_hand=0, setup_discard=0)
    for country in TURN_ORDER:
        card_id = country + "-opening"
        data["cards"].append({"id": card_id, "country": country, "type": "EVENT",
                              "effects": [], "failure_policy": "stop"})
        data["initial_tokens"].append({"token_id": card_id, "definition_id": card_id, "owner_country": country,
                                       "token_type": "CARD", "position_id": zone(country, "DECK")})
    data["settings"].update(initial_hand=1, setup_discard=1)

    class FailedWriter:
        def append(self, record):
            raise OSError("Interrupted before first record")

    checkpoint, journal = tmp_path / "initial.json", tmp_path / "operations.jsonl"
    with pytest.raises(OSError):
        make(data, logger=FailedWriter(), checkpoint_path=checkpoint)
    resumed = SaveRepository.resume(checkpoint, RuleSet(data), journal)
    assert resumed.state.turn.phase == "Setup"
    assert resumed.state.decision.required_country == "germany"
    choose(resumed, resumed.state.decision.legal_options[0])
    assert resumed.state.decision.required_country == "united-kingdom"
    assert SaveRepository.recover(checkpoint, resumed.rules, journal).record_head == resumed.record_head


def test_process_kill_and_resume_continue_same_journal(tmp_path, data):
    rules = RuleSet(data)
    pack = tmp_path / "pack.json"
    pack.write_text(json.dumps(data), encoding="utf-8")
    code = """
from pathlib import Path
import sys
from qmg_core import CommandProcessor, OperationLogger, RuleSet
root = Path(sys.argv[1])
rules = RuleSet.load(root / 'pack.json')
p = CommandProcessor.create(rules, logger=OperationLogger(root / 'operations.jsonl'),
                            checkpoint_path=root / 'state.json')
commands = p.legal_commands(('germany',))
p.submit(next(c for c in commands if c.get('card') == 'g-chain'), ('germany',))
print(p.state.decision.decision_id, flush=True)
sys.stdin.read()
"""
    with subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, text=True) as process:
        try:
            pending_id = process.stdout.readline().strip()
            assert pending_id.startswith("decision:")
        finally:
            process.kill()
            process.wait(timeout=10)
    journal, checkpoint = tmp_path / "operations.jsonl", tmp_path / "state.json"
    before = journal.read_bytes()
    resumed = SaveRepository.resume(checkpoint, rules, journal)
    assert resumed.state.decision.decision_id == pending_id
    choose(resumed, "B")
    assert journal.read_bytes().startswith(before)
    assert resumed.state.scores["axis"] == 3
    recovered = SaveRepository.recover(checkpoint, rules, journal)
    assert recovered.state.to_dict() == resumed.state.to_dict()
    assert recovered.record_head == resumed.record_head


class Forms(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.forms = {}
        self.fields = None
        self.in_button = False
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form":
            self.fields = {}
            self.action = attributes["action"]
        elif tag == "input" and self.fields is not None:
            self.fields[attributes["name"]] = attributes["value"]
        elif tag == "button":
            self.in_button = True

    def handle_data(self, data):
        if self.in_button:
            self.forms[data] = (self.action, self.fields)

    def handle_endtag(self, tag):
        if tag == "button":
            self.in_button = False
        if tag == "form":
            self.fields = None


@contextmanager
def http(workbench):
    with make_server(workbench, 0) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        try:
            yield connection
        finally:
            connection.close()
            server.shutdown()
            thread.join(timeout=10)


def request(connection, method, route="/", fields=None, origin=None):
    headers = {}
    if fields is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if origin:
        headers["Origin"] = origin
    connection.request(method, route, body=None if fields is None else urlencode(fields), headers=headers)
    response = connection.getresponse()
    return response.status, response.read().decode()


def test_browser_forms_share_core_and_continue_after_restart(tmp_path, data, weights):
    rules = RuleSet(data)
    session = Workbench(tmp_path, rules, weights)
    with http(session) as connection:
        status, page = request(connection, "GET")
        assert status == 200 and "德国手牌" in page and "uk-secret" not in page
        path, fields = Forms(page).forms["计分与部署"]
        assert request(connection, "POST", path, fields, origin="https://untrusted.example")[0] == 403
        assert request(connection, "POST", path, fields)[0] == 303
        assert request(connection, "POST", path, fields)[0] == 409
        _, page = request(connection, "GET")
        assert "轴心 1 VP" in page and "选择合法目标" in page
        assert "选择 B" in Forms(page).forms and "选择 C" not in Forms(page).forms
        assert request(connection, "GET", "/state.json")[0] == 404
    new_session = Workbench(tmp_path, rules, weights)
    with http(new_session) as connection:
        _, page = request(connection, "GET")
        assert "轴心 1 VP" in page
        path, fields = Forms(page).forms["选择 B"]
        assert request(connection, "POST", path, fields)[0] == 303
        _, page = request(connection, "GET")
        assert "轴心 3 VP" in page and "BUILD_ARMY" in page
        assert "选择 B" not in Forms(page).forms
    assert new_session.processor.state.tokens["germany-army-1"].position_id == "B"


def test_browser_ai_executes_and_records_the_same_core_command(tmp_path, data, weights):
    session = Workbench(tmp_path, RuleSet(deepcopy(data)), weights)
    expected = session.planner.choose(session.human, "germany")
    with http(session) as connection:
        _, page = request(connection, "GET")
        path, fields = Forms(page).forms["让 AI 执行一步"]
        assert request(connection, "POST", path, fields)[0] == 303
    actual = session.processor.records[0]
    assert actual["operation_type"] == expected["type"]
    assert actual["source"] == expected.get("card")


def test_browser_default_turn_end_response_survives_restart(tmp_path, data, weights):
    card(data, "g-restore")["title"] = "盖放接续响应"
    catalog = ContentCatalog.build(RuleSet(data), tmp_path / "catalog" / "rules.sqlite3")
    save_directory = tmp_path / "match"
    session = Workbench(save_directory, RuleSet.load(catalog), weights)
    with http(session) as connection:
        def click(label):
            _, page = request(connection, "GET")
            path, fields = Forms(page).forms[label]
            assert request(connection, "POST", path, fields)[0] == 303

        click("盖放接续响应")
        while session.processor.state.turn.phase != "Draw":
            click("结束阶段")
            while session.processor.state.decision:
                # Use the server-produced discard commands while keeping the response deployed.
                _, page = request(connection, "GET")
                choices = Forms(page).forms
                click(next(label for label, (_, fields) in choices.items() if fields["action"] == "human"))
        click("结束阶段")
        click("使用能力")
        assert session.processor.state.turn.turn_id == 1
        assert session.processor.state.tokens["g-restore"].position_id == zone("germany", "DISCARD")
        assert session.processor.state.tokens["g-return"].position_id == "RESOLUTION"
        settled = next(record for record in session.processor.records if record["operation_type"] == "OBJECT_SETTLED"
                       and record["source"] == "g-restore")
        reveal = next(record for record in session.processor.records if record["operation_type"] == "PAY_COST"
                      and record["source"] == "g-return")
        assert {"token_id": "g-return", "from": zone("germany", "SET_ASIDE"), "to": zone("germany", "RESPONSE")} in settled["outcome"]["changes"]
        assert {"token_id": "g-return", "from": zone("germany", "RESPONSE"), "to": "RESOLUTION"} in reveal["outcome"]["changes"]
        assert settled["sequence_id"] < reveal["sequence_id"]
    resumed = Workbench(save_directory, RuleSet.load(catalog), weights)
    with http(resumed) as connection:
        _, page = request(connection, "GET")
        assert "选择 B" in Forms(page).forms and "选择 C" in Forms(page).forms
        path, fields = Forms(page).forms["选择 B"]
        assert request(connection, "POST", path, fields)[0] == 303
    assert resumed.processor.state.turn.country_index == 1
    assert resumed.processor.state.tokens["germany-army-2"].position_id == "B"
    assert resumed.processor.state.tokens["g-restore"].position_id == zone("germany", "DISCARD")
    assert resumed.processor.state.tokens["g-return"].position_id == zone("germany", "DISCARD")
    transfer = next(record for record in resumed.processor.records
                    if record["operation_type"] == "TRANSFER" and record["targets"].get("token") == "germany-army-2")
    assert transfer["source"] == "g-return" and transfer["turn_id"] == 1
