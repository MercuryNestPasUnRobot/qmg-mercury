from http.server import ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
from socket import create_connection
from threading import Thread
from time import monotonic, sleep
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zipfile import ZipFile

import pytest

from qmg_core import ai_debug
from qmg_core.ai_debug import DebugPanel, RequestError, VersionRegistry, edited_weights, handler_for, log_window
from qmg_core.ai_debug_worker import PolicyClient
from qmg_core.api import CoreAPI
from qmg_core.definitions import RuleSet
from qmg_core.engine import CommandProcessor
from qmg_core.model import zone


def eventually(predicate):
    end = monotonic() + 12
    while monotonic() < end:
        value = predicate()
        if value:
            return value
        sleep(0.025)
    pytest.fail("debug worker did not acknowledge the requested change")


class SyntheticVersion:
    def __init__(self, tmp_path, data):
        rules = RuleSet(data)
        catalog = tmp_path / "fixture.json"
        catalog.write_text(json.dumps(rules.data), encoding="utf-8")
        self.defaults = json.loads(Path("data/ai_test_weights.json").read_text(encoding="utf-8"))
        self.defaults["max_simulation_nodes"] = 16
        self.options = {version: {"label": version, "defaults": self.defaults, "catalog": str(catalog),
                                  "commit": version, "rules_digest": rules.fingerprint}
                        for version in ("current", "v1-test")}

    def public(self):
        return [{"id": "v1-test", "label": "验证版本"}]

    def core(self, version):
        assert version in self.options
        return Path.cwd()


def configs(axis=None, allies=None):
    return {"axis": {"version": "current", "policy": "A", "values": axis or {}},
            "allies": {"version": "v1-test", "policy": "B", "values": allies or {}}}


def test_policy_input_contains_only_the_acting_seats_observation(data):
    processor = CommandProcessor.create(RuleSet(data), seed=31, game_id="seed-31")
    client = CoreAPI(processor, ("germany",))
    sent = {}
    policy = PolicyClient.__new__(PolicyClient)

    def exchange(payload):
        sent.update(payload)
        return {"command": payload["commands"][0], "row": {}}

    policy.exchange = exchange
    policy.decide(client, "germany")
    assert sent["view"]["game_id"] == "debug-projection"
    assert sent["view"]["history"] == []
    assert sent["commands"] == client.legal_commands()
    positions = sent["view"]["positions"]
    assert positions[zone("germany", "HAND")]["tokens"]
    assert "tokens" not in positions[zone("united-kingdom", "HAND")]
    assert positions[zone("united-kingdom", "HAND")]["count"] > 0
    assert "tokens" not in positions[zone("germany", "DECK")]
    assert set(processor.state.positions[zone("united-kingdom", "HAND")]).isdisjoint(sent["view"]["tokens"])


def test_pause_applies_weights_at_a_decision_boundary_and_keeps_the_log(tmp_path, data):
    data["settings"].update(max_rounds=20, victory_margin=100000)
    panel = DebugPanel(tmp_path / "runs", SyntheticVersion(tmp_path, data))
    try:
        result = panel.start({"seed": 31, "mode": "continuous", "configs": configs()})
        run_id = result["run"]["id"]
        directory = panel.directory(run_id)

        def status():
            state = panel.detail(run_id)["status"]
            assert state["state"] not in ("interrupted", "failed"), (directory / "worker.log").read_text()
            return state

        eventually(lambda: status()["step"] >= 1)
        with pytest.raises(RequestError, match="已有测试"):
            panel.start({})
        panel.control(run_id, "pause", {})
        eventually(lambda: status()["state"] == "paused")
        paused_at = status()["step"]
        assert paused_at >= 1
        panel.control(run_id, "weights", {"configs": configs({"scoring_units.vp": 3}, {"scoring_units.vp": 0})})
        eventually(lambda: status().get("error"))
        assert status()["step"] == paused_at and status()["weights_revision"] == 1
        assert status()["configs"]["axis"]["weights"]["scoring_units"]["vp"] == 1
        panel.control(run_id, "weights", {"configs": configs({"scoring_units.vp": 2, "distance_weights.1": 0.9})})
        eventually(lambda: status()["weights_revision"] == 2)
        assert status()["step"] == paused_at and status()["state"] == "paused"
        initial = panel.board(run_id, 1)
        assert initial["step"] == paused_at and initial["regions"]
        assert len(initial["seats"]) == 6
        assert all(set(zones) == {"HAND", "STATUS", "DISABLED_STATUS", "RESPONSE"} for zones in initial["seats"].values())
        edited = configs({"scoring_units.action": 2.4})
        edited["axis"].update(version="v1-test", policy="B")
        panel.control(run_id, "step", {"configs": edited})
        stepped = eventually(lambda: status().get("last_step"))
        assert stepped["step"] == paused_at + 1
        sleep(0.25)
        assert status()["step"] == paused_at + 1 and status()["state"] == "paused"
        board = panel.board(run_id, stepped["game"])
        assert board["step"] == stepped["step"]
        row = log_window(directory / "game-1.jsonl")["rows"][0]
        assert row["settlement"]["scores_before"] == initial["scores"]
        assert row["settlement"]["scores_after"] == board["scores"]
        assert row["settlement"]["events"]
        assert row["weights_revision"] == 3
        assert row["ai_version"] == "v1-test" and row["policy"] == "B"
        panel.control(run_id, "resume", {})
        eventually(lambda: status()["step"] > stepped["step"])
        panel.control(run_id, "stop", {})
        eventually(lambda: status()["state"] == "stopped")
        rows = [json.loads(line) for line in (directory / "game-1.jsonl").read_text(encoding="utf-8").splitlines()]
        assert rows[paused_at - 1]["weights_revision"] == 1
        assert rows[paused_at]["weights_revision"] == 3
        assert rows[paused_at]["decision_index"] == paused_at + 1
        assert rows[paused_at]["version"] > rows[paused_at - 1]["version"]
        params = [json.loads(line) for line in (directory / "parameters.jsonl").read_text(encoding="utf-8").splitlines()]
        assert params[-1]["after_decision"] == paused_at
        assert params[-2]["configs"]["axis"]["weights"]["scoring_units"]["vp"] == 2
        assert params[-2]["configs"]["axis"]["weights"]["distance_weights"][1] == 0.9
        assert params[-1]["configs"]["axis"]["weights"]["scoring_units"]["action"] == 2.4
        assert params[-1]["configs"]["allies"] == params[0]["configs"]["allies"]
        assert rows[paused_at]["weights_digest"] == params[-1]["configs"][row["config_slot"]]["weights_digest"]
        assert DebugPanel(panel.output, panel.versions).list_runs()["runs"][0]["state"] == "stopped"
    finally:
        panel.close()


def test_log_pages_are_separate_and_preserve_utf8_across_cursors(tmp_path):
    path = tmp_path / "game-1.jsonl"
    with path.open("w", encoding="utf-8") as stream:
        for step in range(140):
            stream.write(json.dumps({"step": step, "name": "调试日志" * 300}, ensure_ascii=False) + "\n")
        stream.write('{"step":')
    cursor = None
    actual = []
    while True:
        page = log_window(path, cursor, limit=22)
        actual.extend(row["step"] for row in page["rows"])
        cursor = page["before"]
        if cursor is None:
            break
    assert actual == list(reversed(range(140)))
    assert not log_window(tmp_path / "game-2.jsonl")["rows"]


def test_continuous_run_switches_sides_before_incrementing_the_seed(tmp_path, data):
    data["settings"].update(max_rounds=1, victory_margin=100000)
    panel = DebugPanel(tmp_path / "runs", SyntheticVersion(tmp_path, data))
    try:
        created = panel.start({"seed": 31, "mode": "continuous", "configs": configs({"scoring_units.vp": 2.5})})
        key = created["run"]["id"]

        def games():
            info = panel.detail(key)
            assert info["status"]["state"] == "running", info["status"]
            return info["games"] if len(info["games"]) >= 3 else []

        eventually(lambda: panel.detail(key)["status"]["state"] != "starting")
        results = eventually(games)
        assert [(game["seed"], game["swapped"]) for game in results[:3]] == [(31, False), (31, True), (32, False)]
        folder = panel.directory(key)
        rows = [log_window(folder / f"game-{number}.jsonl", limit=1000)["rows"][-1] for number in (1, 2, 3)]
        assert [(row["team"], row["config_slot"], row["ai_version"], row["policy"]) for row in rows] == [
            ("axis", "axis", "current", "A"), ("axis", "allies", "v1-test", "B"), ("axis", "axis", "current", "A")]
        assert rows[0]["weights_digest"] == rows[2]["weights_digest"] != rows[1]["weights_digest"]
        panel.control(key, "stop", {})
        eventually(lambda: panel.detail(key)["status"]["state"] == "stopped")
    finally:
        panel.close()


def test_numeric_settings_and_loopback_panel_routes(tmp_path, data):
    registry = SyntheticVersion(tmp_path, data)
    with pytest.raises(RequestError, match="没有该评分参数"):
        edited_weights(registry.defaults, {"action_distance_scoring": 1})
    with pytest.raises(RequestError, match="数值无效"):
        edited_weights(registry.defaults, {"max_simulation_nodes": 1.5})
    panel = DebugPanel(tmp_path / "runs", registry)
    with ThreadingHTTPServer(("127.0.0.1", 0), handler_for(panel)) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            # Browsers may preconnect before requesting any resources on the socket.
            with create_connection(server.server_address, timeout=2):
                with urlopen(base, timeout=2) as response:
                    assert "AI 调试台" in response.read().decode()
                with urlopen(base + "/board.js", timeout=2) as response:
                    assert "pieceIcon" in response.read().decode()
            with urlopen(base + "/api/versions", timeout=5) as response:
                assert json.load(response)[0]["id"] == "v1-test"
            with urlopen(base + "/api/runs", timeout=5) as response:
                assert json.load(response) == {"runs": [], "active": None}
            request = Request(base + "/api/runs", data=b"{}", headers={
                "Content-Type": "application/json", "Origin": "http://other.example"})
            with pytest.raises(HTTPError) as rejected:
                urlopen(request, timeout=5)
            assert rejected.value.code == 403
        finally:
            server.shutdown()
            thread.join()


def test_pinned_runtime_installs_only_core_sources_and_marks_a_complete_cache(tmp_path, monkeypatch):
    commit = "a" * 40
    monkeypatch.setattr(ai_debug, "VERSIONS", {"pinned": ("历史版本", commit, "catalog")})
    archive = BytesIO()
    with ZipFile(archive, "w") as output:
        output.writestr("core/qmg_core/ai_match_policy.py", "PINNED = True\n")
        output.writestr("core/data/architecture_fixture.json", "{}")
        output.writestr("core/qmg_core/unrelated.txt", "excluded")
    calls = []

    def git(_registry, *args):
        calls.append(args)
        if args[0] == "show":
            return b'{"scoring_units":{"vp":1}}'
        assert args[2] == commit
        return archive.getvalue()

    monkeypatch.setattr(VersionRegistry, "git", git)
    versions = VersionRegistry(tmp_path, tmp_path / "cache")
    installed = versions.core("pinned")
    assert (installed / "qmg_core" / "ai_match_policy.py").read_text() == "PINNED = True\n"
    assert not (installed / "qmg_core" / "unrelated.txt").exists()
    assert versions.core("pinned") == installed
    assert [args[0] for args in calls] == ["show", "archive"]
