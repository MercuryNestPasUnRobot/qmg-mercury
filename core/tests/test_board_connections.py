from qmg_core.board_connections import PRINTED_REGIONS, corrected_edges, corrected_straits, edge_pairs
from qmg_core.replay import recording_topology


def test_printed_board_shared_borders_wrap_links_and_gibraltar():
    imported = [
        ("united-kingdom", "north-atlantic"), ("united-kingdom", "north-sea"), ("north-sea", "germany"),
        ("germany", "baltic-sea"), ("western-europe", "north-sea"), ("western-europe", "north-atlantic"),
        ("north-africa", "north-atlantic"), ("canada", "north-pacific"), ("canada", "north-atlantic"),
    ]
    checked = corrected_edges(imported)

    def neighbors(region):
        return {b if a == region else a for a, b in checked if region in (a, b)}

    assert neighbors("united-kingdom") == {"north-sea"}
    assert neighbors("germany") == {"baltic-sea"}
    assert neighbors("western-europe") == {"north-sea"}
    assert neighbors("north-sea") == {"united-kingdom", "western-europe", "north-africa", "central-atlantic"}
    assert neighbors("canada") == {"north-atlantic", "north-pacific", "east-pacific"}
    assert corrected_edges(checked) == checked

    north_africa = {"land": "north-africa", "seas": ["north-atlantic", "mediterranean"]}
    denmark = {"land": "scandinavia", "seas": ["north-sea", "baltic-sea"]}
    assert corrected_straits([north_africa, denmark]) == [
        {"land": "north-africa", "seas": ["north-sea", "mediterranean"]}, denmark,
    ]
    assert "mediterranean" not in neighbors("north-sea")


def test_saved_replay_can_show_both_checked_board_and_original_rules(tmp_path):
    path = tmp_path / "replays" / "batches" / "batch" / "game.html"
    original = {
        "batch": {},
        "regions": {key: {} for key in PRINTED_REGIONS},
        "edges": [["united-kingdom", "north-atlantic"], ["united-kingdom", "north-sea"], ["north-sea", "germany"]],
        "straits": [{"land": "north-africa", "seas": ["north-atlantic", "mediterranean"]}],
        "frames": [{"scores": {"axis": 7, "allies": 4}}],
    }
    payload = recording_topology(path, original)
    assert payload["frames"] == original["frames"]
    assert payload["edges"] == original["edges"]
    assert payload["straits"] == original["straits"]
    checked = payload["board_reference"]
    assert {pair for pair in edge_pairs(checked["edges"]) if "united-kingdom" in pair} == {
        ("north-sea", "united-kingdom"),
    }
    assert checked["straits"] == [{"land": "north-africa", "seas": ["north-sea", "mediterranean"]}]
    assert "board_reference" not in recording_topology(path, {**payload, **checked})
