import pytest

from conftest import card, choose, make, move_initial, play
from qmg_core import CommandProcessor, CoreAPI, CoreError, OperationLogger, OneStepPlanner, RuleSet, SaveRepository, ScoreResolver
from qmg_core.catalog import ContentCatalog
from qmg_core.model import TURN_ORDER


@pytest.fixture
def forces(data):
    data["auxiliary_forces"] = [
        {"id": "ridge-guard", "controller": "germany", "name": "山脊卫队"},
        {"id": "harbor-watch", "controller": "united-kingdom", "name": "港湾卫队"},
    ]
    site = next(region for region in data["regions"] if region["id"] == "C")
    site.update(home_country="ridge-guard", supply_source_for="ridge-guard")
    # Reuse a unit definition, but allocate independent tokens and reserve positions.
    army = next(token["definition_id"] for token in data["initial_tokens"] if token["token_id"] == "germany-army-1")
    data["initial_tokens"].extend([
        {"token_id": "ridge-1", "definition_id": army, "token_type": "UNIT",
         "owner_country": "ridge-guard", "position_id": "ridge-guard:RESERVE_ARMY"},
        {"token_id": "harbor-1", "definition_id": army, "token_type": "UNIT",
         "owner_country": "harbor-watch", "position_id": "D"},
    ])
    card(data, "g-chain").update(title="边界巡查", effects=[
        {"type": "CHOOSE", "operation": {"type": "RECRUIT", "force": "ridge-guard", "target": "$choice"}},
        {"type": "LAND_BATTLE", "force": "ridge-guard", "target": "D"},
        {"type": "GAIN_VP", "amount": 2},
    ])
    return data


def test_sqlite_card_controls_auxiliary_units_and_replays(tmp_path, forces, weights):
    catalog = ContentCatalog.build(RuleSet(forces), tmp_path / "content.sqlite3")
    rules = RuleSet.load(catalog)
    checkpoint, journal = tmp_path / "match" / "state.json", tmp_path / "match" / "operations.jsonl"
    p = CommandProcessor.create(rules, logger=OperationLogger(journal), checkpoint_path=checkpoint)
    client = CoreAPI(p, ("germany",))
    assert len(rules.force_controllers) == 8 and tuple(rules.countries) == TURN_ORDER
    card_candidate = next(row for row in OneStepPlanner(rules, weights).rank(client, "germany")
                          if row["command"].get("card") == "g-chain")
    assert card_candidate["complete"]
    client.submit(card_candidate["command"])
    assert client.observe()["decision"]["required_country"] == "germany"
    assert "D" not in p.state.decision.legal_options

    p = SaveRepository.resume(checkpoint, RuleSet.load(catalog), journal)
    client = CoreAPI(p, ("germany",))
    client.submit(next(c for c in client.legal_commands() if c.get("option") == "C"))
    assert p.state.tokens["ridge-1"].position_id == "C"
    assert p.state.tokens["germany-army-1"].position_id == "germany:RESERVE_ARMY"
    assert p.state.tokens["harbor-1"].position_id == "harbor-watch:RESERVE_ARMY"
    actions = [r for r in p.records if r["operation_type"] in ("RECRUIT", "LAND_BATTLE")]
    assert [r["operation_type"] for r in actions] == ["RECRUIT", "LAND_BATTLE"]
    assert all(r["targets"]["force"] == "ridge-guard" and r["actor"] == "germany" for r in actions)
    assert p.state.tokens["g-chain"].position_id == "germany:DISCARD" and p.state.scores["axis"] == 2
    assert ScoreResolver(rules).sites(p.state, "germany")["factory"] == 2
    assert SaveRepository.recover(checkpoint, rules, journal).state == p.state


def test_controller_supply_phase_handles_own_auxiliary_units_only(forces):
    move_initial(forces, "ridge-1", "B")
    p = make(forces)
    client = CoreAPI(p, ("germany",))
    client.submit(next(c for c in client.legal_commands() if c["type"] == "END_PHASE"))
    assert p.state.tokens["ridge-1"].position_id == "ridge-guard:RESERVE_ARMY"
    assert p.state.tokens["harbor-1"].position_id == "D"
    loss = next(r for r in p.records if r["operation_type"] == "ELIMINATE")
    assert loss["actor"] == "germany" and loss["targets"]["target_token"] == "ridge-1"


def test_auxiliary_force_has_no_seat_and_cannot_be_used_by_another_seat(forces):
    forces["auxiliary_forces"][0]["controller"] = "italy"
    p = make(forces)
    with pytest.raises(CoreError, match="Invalid seat"):
        CoreAPI(p, ("ridge-guard",))
    seat = CoreAPI(p, ("germany",))
    assert "g-chain" not in {c.get("card") for c in seat.legal_commands()}
    legal = next(c for c in seat.legal_commands() if c.get("card") == "g-build")
    with pytest.raises(CoreError, match="Unauthorized"):
        seat.submit({**legal, "force": "ridge-guard"})
    assert not p.records


def test_defensive_listener_recognizes_unit_of_an_auxiliary_force(forces):
    card(forces, "g-chain")["effects"] = [{"type": "ELIMINATE", "target_token": "harbor-1"}]
    p = make(forces)
    play(p, "g-chain")
    assert p.state.tokens["harbor-1"].position_id == "D"
    assert any(r["operation_type"] == "CANCEL" and r["actor"] == "united-kingdom" for r in p.records)


def test_redeployment_keeps_auxiliary_owner_and_seat(forces):
    move_initial(forces, "ridge-1", "B")
    card(forces, "g-chain")["effects"] = [{"type": "REDEPLOY", "force": "ridge-guard"}, {"type": "REDEPLOY"}]
    p = make(forces)
    play(p, "g-chain")
    assert p.state.decision.legal_options == ["ridge-1"]
    choose(p, "ridge-1")
    assert p.state.tokens["ridge-1"].position_id == "B"
    choose(p, "C")
    assert p.state.tokens["ridge-1"].owner_country == "ridge-guard"
    assert p.state.tokens["ridge-1"].position_id == "C"
    rebuild = next(r for r in p.records if r["outcome"]["context"].get("redeployed"))
    assert rebuild["actor"] == "germany" and rebuild["targets"]["force"] == "ridge-guard"
    assert p.state.decision.legal_options == ["germany-army-0"]
    choose(p, "germany-army-0")
    choose(p, "A")
    assert not p.state.stack
