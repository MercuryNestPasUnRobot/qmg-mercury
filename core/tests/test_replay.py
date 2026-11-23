import json
from pathlib import Path

import pytest

from conftest import card, make, move_initial
from qmg_core import CoreAPI, RuleSet
from qmg_core.ai_match import run_matches
from qmg_core.model import CoreError, digest
from qmg_core.replay import ReplayBatch, Tape, page, refresh_recordings, replay_game


def test_tape_restores_board_status_hidden_response_and_score(data):
    move_initial(data, "g-status", "germany:STATUS")
    card(data, "g-build")["effects"].append({"type": "GAIN_VP", "amount": 2})
    data["settings"]["plays_per_turn"] = 2
    engine = make(data)
    client = CoreAPI(engine, ("germany",))
    tape = Tape(engine)
    for source in ("g-restore", "g-build"):
        client.submit(next(c for c in client.legal_commands() if c.get("card") == source))
        if engine.state.decision:
            target = next(c for c in client.legal_commands() if c["option"] == "B")
            client.submit(target)
        tape.capture()
    start, hidden, after = tape.data()["frames"]
    assert start["seats"]["germany"]["STATUS"] == ["g-status"]
    assert "g-restore" in hidden["seats"]["germany"]["RESPONSE"]
    assert start["seats"]["germany"]["DECK"] == 2
    assert isinstance(start["seats"]["germany"]["HAND"], int)
    unit = next(key for key in after["board"]["B"] if tape.tokens[key]["unit"] == "ARMY")
    assert any(unit == key and to == "B" for event in after["events"] for key, _, to in event["moves"])
    assert after["scores"] == {"axis": 2, "allies": 0}
    assert tape.tokens["g-restore"]["kind"] == "CARD"
    assert "g-deck-2" not in tape.tokens


def test_match_replays_reconstruct_from_logs_and_publish_a_private_library(data, tmp_path):
    rules = RuleSet({**data, "settings": {**data["settings"], "max_rounds": 1, "victory_margin": 0}})
    weights = json.loads(Path("data/ai_test_weights.json").read_text(encoding="utf8"))
    source, library = tmp_path / "source", tmp_path / "library"
    settings = {"budget_ms": 1000, "shortlist_width": 2, "nodes_per_candidate": weights["max_simulation_nodes"]}
    batch = ReplayBatch(library, source, rules, name="首批", version="B v1", note="规则 A 作为回退", settings=settings)
    report = run_matches(rules, weights, seeds=[31], output=source, width=2, budget_ms=1000, replay=batch)
    assert "首批" in (library / "打开录像.html").read_text(encoding="utf8")
    assert RuleSet.load(library / "rules" / (rules.fingerprint + ".json")).fingerprint == rules.fingerprint
    game = report["games"][0]
    recording = batch.directory / (digest(game["game_id"])[:16] + ".html")
    html = recording.read_text(encoding="utf8")
    marker = '<script type="application/json" id="replay-data">'
    tape = json.loads(html.split(marker)[1].split("</script>", 1)[0])
    assert tape["batch"]["ai_version"] == "B v1"
    assert tape["frames"][-1]["scores"] == game["scores"] and tape["frames"][-1]["winner"] == game["winner"]
    assert len(tape["frames"]) == 1 + sum(game["decisions_by_seat"].values())
    assert refresh_recordings(library) == 2
    refreshed = recording.read_text(encoding="utf8")
    assert json.loads(refreshed.split(marker)[1].split("</script>", 1)[0]) == tape
    assert 'id="board-art"' in refreshed
    with pytest.raises(CoreError, match="reported result"):
        replay_game(rules, {**game, "record_head": "corrupt"}, source / game["decision_log"])
    lines = (source / game["decision_log"]).read_text(encoding="utf8").splitlines()
    tampered = json.loads(lines[0])
    tampered["version"] += 1
    incorrect = source / "incorrect.jsonl"
    incorrect.write_text("\n".join([json.dumps(tampered), *lines[1:]]), encoding="utf8")
    with pytest.raises(CoreError, match="state sequence mismatch"):
        replay_game(rules, game, incorrect)


def test_card_markup_cannot_break_out_of_recording_data():
    html = page("测试", "<main></main>", {"text": "</script><script>alert(1)</script>"})
    assert "<script>alert(1)</script>" not in html
    assert json.loads(html.split('id="replay-data">')[1].split("</script>")[0])["text"].startswith("</script>")
