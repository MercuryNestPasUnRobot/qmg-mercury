from conftest import choose, move_initial, play
from test_ai_debug import SyntheticVersion
from test_reallocation import add_card
from qmg_core.ai_debug import DebugPanel
from qmg_core.board_connections import PRINTED_REGIONS, corrected_edges, corrected_straits, edge_pairs
from qmg_core.engine import CommandProcessor
from qmg_core.map_variants import match_rules
from qmg_core.replay import recording_topology, write_json
from qmg_core.rules import MapGraph


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
    assert neighbors("germany") == {"baltic-sea", "balkans"}
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


def test_kazakhstan_and_russia_no_longer_have_a_direct_edge_on_new_board():
    edges = [("kazakhstan", "russia"), ("kazakhstan", "moscow"), ("moscow", "russia")]
    checked = set(corrected_edges(edges, regional_review=True))
    assert ("kazakhstan", "russia") not in checked
    assert {("kazakhstan", "moscow"), ("moscow", "russia")} <= checked


def test_existing_maps_gain_balkans_border_and_conscription_uses_selected_map(tmp_path, data):
    data["regions"].extend({"id": key, "type": "LAND"} for key in ("germany", "balkans"))
    data["settings"]["free_recruit"] = True
    move_initial(data, "germany-army-0", "germany")
    effect = {"type": "choose-space", "action": "recruit", "kind": "army", "spaces": ["germany", "B"]}
    source = add_card(data, "germany", "conscription", "HAND", effects=[
        {"type": "CARD_EFFECT", "effect": effect}, {"type": "CARD_EFFECT", "effect": {**effect, "optional": True}}])
    panel = DebugPanel(tmp_path / "runs", SyntheticVersion(tmp_path, data))
    standard = panel.map_details("standard")["layout"]
    border = {"a": "balkans", "b": "germany", "uses": ["battle", "build", "supply"]}
    assert border in standard["connections"]
    standard["connections"].remove(border)
    standard["connections"].append({"a": "D", "b": "germany", "uses": ["build"]})
    key = "a" * 32
    write_json(panel.maps_directory / (key + ".json"), {"id": key, "name": "旧地图", "layout": standard})
    assert border in panel.map_details(key)["layout"]["connections"]
    current = match_rules(panel.base_rules, panel.map_record(key)["layout"])
    for purpose in ("supply", "battle", "build"):
        assert "balkans" in MapGraph(current).neighbors("germany", purpose=purpose)
    p = CommandProcessor.create(current)
    play(p, source)
    assert p.state.decision.legal_options == ["D", "balkans"]
    choose(p, "balkans")
    assert p.state.decision.legal_options == ["D", "done"]
    choose(p, "D")
    assert not p.state.decision
    assert len(p.state.positions["balkans"]) == len(p.state.positions["D"]) == 1

    default = CommandProcessor.create(match_rules(panel.base_rules))
    play(default, source)
    assert default.state.decision.legal_options == ["balkans"]
    choose(default, "balkans")
    assert default.state.decision.legal_options == ["done"]
    choose(default, "done")
    assert not default.state.decision
