from contextlib import closing
import sqlite3

import pytest

from conftest import card
from qmg_core import CommandProcessor, CoreAPI, CoreError, OperationLogger, RuleSet, SaveRepository
from qmg_core.catalog import ContentCatalog
from qmg_core.model import zone


def test_catalog_roundtrip_keeps_identity_order_and_static_tables(tmp_path, data):
    json_rules = RuleSet(data)
    target = ContentCatalog.build(json_rules, tmp_path / "content #1" / "rules.sqlite3")
    catalog_rules = RuleSet.load(target)
    assert catalog_rules.data == data
    assert catalog_rules.fingerprint == json_rules.fingerprint
    assert CommandProcessor.create(catalog_rules, seed=7).state == CommandProcessor.create(json_rules, seed=7).state
    with closing(sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT operation FROM card_effects WHERE owner_id=? AND stage=? ORDER BY ordinal",
                          ("g-chain", "effects")).fetchall()
        assert rows == [("GAIN_VP",), ("CHOOSE",), ("GAIN_VP",)]
        assert db.execute("SELECT ability_id FROM card_abilities WHERE card_id=?", ("g-restore",)).fetchall() == [
            ("g-turn-restore",)
        ]
        assert db.execute("SELECT COUNT(*) FROM initial_tokens").fetchone()[0] == len(data["initial_tokens"])
        assert {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {
            "catalog", "definitions", "cards", "abilities", "card_abilities",
            "card_effects", "ability_effects", "initial_tokens",
        }


def test_catalog_reopens_json_match_and_runs_effects_without_writing_content(tmp_path, data):
    path = ContentCatalog.build(RuleSet(data), tmp_path / "content" / "rules.sqlite3")
    content_before = path.read_bytes()
    journal = tmp_path / "matches" / "game" / "operations.jsonl"
    checkpoint = journal.with_name("state.json")
    first = CommandProcessor.create(RuleSet(data), logger=OperationLogger(journal), checkpoint_path=checkpoint)
    human = CoreAPI(first, ("germany",))
    human.submit(next(c for c in human.legal_commands() if c.get("card") == "g-chain"))
    assert first.state.tokens["g-chain"].position_id == "RESOLUTION"
    assert first.state.scores["axis"] == 1

    resumed = SaveRepository.resume(checkpoint, ContentCatalog.load(path), journal)
    seat = CoreAPI(resumed, ("germany",))
    with pytest.raises(CoreError, match="Unauthorized"):
        seat.submit({"type": "TRANSFER", "actor": "germany", "expected_version": resumed.state.version,
                     "token": "germany-army-1", "destination": "B"})
    seat.submit(next(c for c in seat.legal_commands() if c.get("option") == "B"))
    assert resumed.state.tokens["germany-army-1"].position_id == "B"
    assert resumed.state.tokens["g-chain"].position_id == zone("germany", "DISCARD")
    assert resumed.state.scores["axis"] == 3

    effects = [r for r in resumed.records if r["operation_type"] in ("GAIN_VP", "BUILD_ARMY", "OBJECT_SETTLED")]
    assert [r["operation_type"] for r in effects] == ["GAIN_VP", "BUILD_ARMY", "GAIN_VP", "OBJECT_SETTLED"]
    assert effects[1]["outcome"]["changes"] == [
        {"token_id": "germany-army-1", "from": zone("germany", "RESERVE_ARMY"), "to": "B"}
    ]
    assert SaveRepository.recover(checkpoint, RuleSet(data), journal).state == resumed.state
    assert path.read_bytes() == content_before
    assert list(path.parent.iterdir()) == [path]
    assert set(p.name for p in journal.parent.iterdir()) == {"state.json", "operations.jsonl"}

    # Sharing a version label cannot make an edited card compatible with the old match.
    card(data, "g-chain")["effects"][0]["amount"] = 2
    revised = ContentCatalog.build(RuleSet(data), path.with_name("revised.sqlite3"))
    with pytest.raises(CoreError, match="rules identity mismatch"):
        SaveRepository.recover(checkpoint, ContentCatalog.load(revised), journal)


def test_catalog_publish_refuses_existing_files_and_invalid_positions(tmp_path, data):
    rules = RuleSet(data)
    existing = tmp_path / "save.json"
    existing.write_text("existing match", encoding="utf-8")
    with pytest.raises(FileExistsError):
        ContentCatalog.build(rules, existing)
    assert existing.read_text(encoding="utf-8") == "existing match"

    data["initial_tokens"][0]["position_id"] = "missing-position"
    unpublished = tmp_path / "bad.sqlite3"
    with pytest.raises(CoreError, match="initial position"):
        ContentCatalog.build(RuleSet(data), unpublished)
    assert not unpublished.exists()
    assert list(tmp_path.iterdir()) == [existing]


def test_catalog_rejects_insufficient_opening_hand(tmp_path, data):
    data["settings"].update(initial_hand=1, setup_discard=1)
    target = tmp_path / "setup.sqlite3"
    with pytest.raises(CoreError, match="Not enough cards for setup"):
        ContentCatalog.build(RuleSet(data), target)
    assert not target.exists()


def test_catalog_rejects_edited_effect_and_keeps_missing_catalog_absent(tmp_path, data):
    path = ContentCatalog.build(RuleSet(data), tmp_path / "rules.sqlite3")
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE card_effects SET parameters=? WHERE owner_id=? AND ordinal=0",
                   ('{"amount":99}', "g-chain"))
    with pytest.raises(CoreError, match="fingerprint mismatch"):
        ContentCatalog.load(path)
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(CoreError, match="Could not load"):
        ContentCatalog.load(missing)
    assert not missing.exists()


def test_invalid_authored_effect_reports_card_stage_and_position(data):
    card(data, "g-chain")["effects"][1] = {"type": "TRANSFER", "token": "g-chain"}
    with pytest.raises(CoreError, match=r"g-chain\.effects\[1\]: Transfer requires"):
        RuleSet(data)
