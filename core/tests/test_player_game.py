import json
from time import monotonic, sleep

import pytest

from conftest import card, move_initial
from qmg_core.ai_debug import DebugPanel
from qmg_core.model import TURN_ORDER
from qmg_core.player_game import PlayerError
from qmg_core.replay import DATA_TAG, refresh_player_recordings, write_json
from test_ai_debug import SyntheticVersion, eventually
from test_response_windows import add_listener
from test_reallocation import add_card


def test_single_player_match_archives_only_the_human_perspective(tmp_path, data, monkeypatch):
    data["settings"].update(max_rounds=1, victory_margin=0, deck_exhaustion_vp=0)
    card(data, "uk-secret")["title"] = "HIDDEN_BRITISH_RESPONSE"
    add_listener(data, "uk-secret", "hidden-response", [],
                 {"operation_type": "BUILD_ARMY", "actor": "self"}, kind="response")
    move_initial(data, "uk-deck", "united-kingdom:HAND")
    card(data, "uk-other")["title"] = "HIDDEN_BRITISH_HAND"
    public = add_card(data, "united-kingdom", "public-play", "HAND", effects=[{"type": "GAIN_VP", "amount": 1}])
    card(data, public)["title"] = "PUBLIC_BRITISH_PLAY"
    add_listener(data, "g-noop", "human-interrupt", [{"type": "GAIN_VP", "amount": 1}],
                 {"operation_type": "CARD_EVENT", "card_trigger": {"type": "any-time"}}, kind="response")
    next(a for a in data["abilities"] if a["id"] == "human-interrupt")["after"] = "delete"
    algorithms = set()

    class PassPhases:
        def __init__(self, core, rules, config):
            self.policy = config["policy"]

        def decide(self, client, seat):
            assert client.controlled_countries == (seat,)
            assert all("tokens" not in client.observe(include_history=False)["positions"][zone(other, "DISCARD")]
                       for other in TURN_ORDER if other != seat)
            algorithms.add((seat, self.policy))
            moves = client.legal_commands()
            if seat == "united-kingdom":
                for cmd in moves:
                    if cmd.get("card") in (public, "uk-deck") and cmd["type"] in ("PLAY_CARD", "DISCARD_CARD"):
                        if cmd["type"] == "PLAY_CARD" and cmd["card"] != public:
                            continue
                        return cmd, {}
            return next(c for c in moves if c["type"] == "END_PHASE"), {}

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
        assert britain["counts"]["RESPONSE"] == 1 and britain["counts"]["HAND"] == 2
        assert not {"RESPONSE", "HAND", "DISCARD"} & britain["cards"].keys()
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
        latest = game.status()
        assert any(p["name"] == "PUBLIC_BRITISH_PLAY" for p in latest["recent_plays"])
        assert "HIDDEN_BRITISH_HAND" not in json.dumps(latest)
        assert latest["board"]["seats"]["united-kingdom"]["counts"]["DISCARD"] == 2

        recording = panel.players.library / game.status()["replay"].removeprefix("/replays/")
        text = recording.read_text(encoding="utf-8")
        assert "HIDDEN_BRITISH_HAND" not in text and "HIDDEN_BRITISH_RESPONSE" not in text
        payload = json.loads(text.split(DATA_TAG, 1)[1].split("</script>", 1)[0])
        assert payload["tokens"][public]["name"] == "PUBLIC_BRITISH_PLAY"
        assert any(p["key"] == public and p["kind"] == "play" for f in payload["frames"] for p in f["plays"])
        assert all(not f["seats"]["united-kingdom"]["piles"]["DISCARD"] for f in payload["frames"])
        assert any(f["decision"] and f["decision"]["country"] == "united-kingdom" and
                   f["decision"]["command"] == {"type": "DISCARD_CARD"} for f in payload["frames"])
        assert payload["game"]["human"] == "germany" and "seed" not in payload["game"]
        assert "玩家对局 · " in payload["batch"]["name"] and "半分点验收" in payload["batch"]["name"]
        assert "A×4" in payload["batch"]["name"] and "B×1" in payload["batch"]["name"]
        assert payload["frames"][0]["seats"]["united-kingdom"]["response_count"] == 1
        assert payload["frames"][-1]["winner"] == game.status()["winner"]
        assert any(frame["decision"] and frame["decision"]["policy"] == "human" for frame in payload["frames"])
        assert (game.folder / "commands.jsonl").is_file()
        manifest = recording.with_name("manifest.json")
        older = json.loads(manifest.read_text(encoding="utf-8"))
        del older["games"][recording.stem]["discard_visibility"]
        write_json(manifest, older)
        recording.write_text("old player recording with unrelated original discard identities", encoding="utf-8")
        assert refresh_player_recordings(panel.players.library) == 1
        assert "HIDDEN_BRITISH_HAND" not in recording.read_text(encoding="utf-8")
        assert "PUBLIC_BRITISH_PLAY" in recording.read_text(encoding="utf-8")

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
