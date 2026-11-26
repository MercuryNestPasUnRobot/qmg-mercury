import json
from time import monotonic, sleep

import pytest

from conftest import card, move_initial
from qmg_core.ai_debug import DebugPanel
from qmg_core.model import TURN_ORDER
from qmg_core.player_game import PlayerError
from qmg_core.replay import DATA_TAG
from test_ai_debug import SyntheticVersion, eventually
from test_response_windows import add_listener


def test_single_player_match_archives_only_the_human_perspective(tmp_path, data, monkeypatch):
    data["settings"].update(max_rounds=1, victory_margin=0, deck_exhaustion_vp=0)
    card(data, "uk-secret")["title"] = "HIDDEN_BRITISH_RESPONSE"
    add_listener(data, "uk-secret", "hidden-response", [],
                 {"operation_type": "BUILD_ARMY", "actor": "self"}, kind="response")
    move_initial(data, "uk-deck", "united-kingdom:HAND")
    card(data, "uk-other")["title"] = "HIDDEN_BRITISH_HAND"
    add_listener(data, "g-noop", "human-interrupt", [{"type": "GAIN_VP", "amount": 1}],
                 {"operation_type": "CARD_EVENT", "card_trigger": {"type": "any-time"}}, kind="response")
    next(a for a in data["abilities"] if a["id"] == "human-interrupt")["after"] = "delete"
    algorithms = set()

    class PassPhases:
        def __init__(self, core, rules, config):
            self.policy = config["policy"]

        def decide(self, client, seat):
            assert client.controlled_countries == (seat,)
            algorithms.add((seat, self.policy))
            return next(c for c in client.legal_commands() if c["type"] == "END_PHASE"), {}

        def close(self):
            pass

    monkeypatch.setattr("qmg_core.player_game.PolicyClient", PassPhases)
    panel = DebugPanel(tmp_path / "debug", SyntheticVersion(tmp_path, data))
    try:
        layout = panel.map_details("standard")["layout"]
        layout["points"]["A"] = 1
        selected = panel.save_map({"name": "半分点验收", "layout": layout})
        configs = {seat: {"version": "current", "policy": "B" if seat == "japan" else "A"}
                   for seat in TURN_ORDER if seat != "germany"}
        request = {"human": "germany", "map": selected["id"], "seed": 31, "seats": configs}
        with pytest.raises(PlayerError, match="其余五个"):
            panel.players.start({**request, "seats": {**configs, "germany": configs["japan"]}})
        started = panel.players.start(request)
        key = started["id"]
        game = panel.players.current
        first = eventually(lambda: game.status() if game.status()["state"] == "human" else None)
        assert first["board"]["regions"]["A"]["vp"] == 1
        assert first["board"]["seats"]["germany"]["cards"]["HAND"]
        britain = first["board"]["seats"]["united-kingdom"]
        assert britain["counts"]["RESPONSE"] == britain["counts"]["HAND"] == 1
        assert "RESPONSE" not in britain["cards"] and "HAND" not in britain["cards"]
        assert "seed" not in first and "uk-hidden" not in json.dumps(first)
        with pytest.raises(PlayerError, match="局面已更新"):
            panel.players.act(key, "command", {"version": first["version"] + 1, "choice": 0})
        with pytest.raises(PlayerError, match="没有该操作"):
            panel.players.act(key, "command", {"version": first["version"], "choice": -1})

        passed = responded = False
        end = monotonic() + 12
        while monotonic() < end:
            status = game.status()
            if status["state"] in ("completed", "failed"):
                break
            if status["state"] != "human":
                sleep(.02)
                continue
            if status["may_pass"]:
                if not passed:
                    choice, passed = -1, True
                else:
                    choice = next(i for i, row in enumerate(status["choices"]) if row["label"].startswith("主动响应"))
                    responded = True
            else:
                choice = next((i for i, row in enumerate(status["choices"]) if row["label"] == "结束阶段"), 0)
            panel.players.act(key, "command", {"version": status["version"], "choice": choice})
        error = game.folder / "error.txt"
        assert game.status()["state"] == "completed", error.read_text(encoding="utf-8") if error.exists() else game.status()
        assert passed and responded and ("japan", "B") in algorithms and ("united-kingdom", "A") in algorithms
        assert panel.list_runs()["runs"] == []

        recording = panel.players.library / game.status()["replay"].removeprefix("/replays/")
        text = recording.read_text(encoding="utf-8")
        assert "HIDDEN_BRITISH_HAND" not in text and "HIDDEN_BRITISH_RESPONSE" not in text
        payload = json.loads(text.split(DATA_TAG, 1)[1].split("</script>", 1)[0])
        assert payload["game"]["human"] == "germany" and "seed" not in payload["game"]
        assert "玩家对局 · " in payload["batch"]["name"] and "半分点验收" in payload["batch"]["name"]
        assert "A×4" in payload["batch"]["name"] and "B×1" in payload["batch"]["name"]
        assert payload["frames"][0]["seats"]["united-kingdom"]["response_count"] == 1
        assert payload["frames"][-1]["winner"] == game.status()["winner"]
        assert any(frame["decision"] and frame["decision"]["policy"] == "human" for frame in payload["frames"])
        assert (game.folder / "commands.jsonl").is_file()

        game.worker.join(2)
        second = panel.players.start(request)
        other = panel.players.current
        opening = eventually(lambda: other.status() if other.status()["state"] == "human" else None)
        choice = next(i for i, row in enumerate(opening["choices"]) if row["label"] == "结束阶段")
        panel.players.act(second["id"], "command", {"version": opening["version"], "choice": choice})
        eventually(lambda: other.status()["step"] > 0)
        panel.players.act(second["id"], "stop", {})
        stopped = eventually(lambda: other.status() if other.status()["state"] == "stopped" else None)
        assert stopped["replay"] and stopped["winner"] is None
    finally:
        panel.close()
