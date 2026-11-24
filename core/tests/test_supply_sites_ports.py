import pytest

from conftest import card, make, move_initial
from qmg_core import CoreAPI
from qmg_core.operations import ScoreResolver
from qmg_core.rules import SupplyResolver


def end_phase(client):
    client.submit(next(command for command in client.legal_commands() if command["type"] == "END_PHASE"))


@pytest.mark.parametrize("can_score", [False, True])
def test_occupied_scoring_site_supplies_land_chain_without_home(data, can_score):
    factory = next(site for site in data["score_sites"] if site["id"] == "factory")
    if not can_score:
        factory["countries"] = ["italy"]
    move_initial(data, "germany-army-0", "C")
    move_initial(data, "germany-army-1", "D")
    engine = make(data)
    client = CoreAPI(engine, ("germany",))
    keys = {"germany-army-0", "germany-army-1"}
    assert keys <= SupplyResolver(engine.rules).supplied(engine.state, "germany")
    end_phase(client)
    assert engine.state.turn.phase == "Supply"
    assert [engine.state.tokens[key].position_id for key in sorted(keys)] == ["C", "D"]
    end_phase(client)
    assert engine.state.scores["axis"] == (2 if can_score else 0)


def test_dynamic_supply_site_requires_active_token_and_pins_historical_rules(data):
    move_initial(data, "germany-army-0", "D")
    resolver = lambda engine: SupplyResolver(engine.rules).supplied(engine.state, "germany")
    assert "germany-army-0" not in resolver(make(data))
    move_initial(data, "dynamic-source", "D")
    active = make(data)
    assert "germany-army-0" in resolver(active)
    assert ScoreResolver(active.rules).sites(active.state, "germany") == {"dynamic-site": 3}
    data["settings"].pop("supply_revision")
    historical = make(data)
    assert active.rules.fingerprint != historical.rules.fingerprint
    assert "germany-army-0" not in resolver(historical)


def test_naval_port_needs_adjacent_friendly_unit_and_regular_supply(data):
    move_initial(data, "germany-navy-0", "T")
    move_initial(data, "italy-army-0", "D")
    data["connections"].append({"id": "port", "a": "D", "b": "T"})
    isolated = make(data)
    assert "germany-navy-0" not in SupplyResolver(isolated.rules).supplied(isolated.state, "germany")

    card(data, "g-status")["modifiers"] = [{"kind": "always_supplied", "kinds": ["navy"]}]
    move_initial(data, "g-status", "germany:STATUS")
    with_port = make(data)
    client = CoreAPI(with_port, ("germany",))
    assert "germany-navy-0" in SupplyResolver(with_port.rules).supplied(with_port.state, "germany")
    end_phase(client)
    assert with_port.state.tokens["germany-navy-0"].position_id == "T"

    data["connections"].pop()
    without_port = make(data)
    client = CoreAPI(without_port, ("germany",))
    assert "germany-navy-0" not in SupplyResolver(without_port.rules).supplied(without_port.state, "germany")
    end_phase(client)
    assert without_port.state.tokens["germany-navy-0"].position_id == "germany:RESERVE_NAVY"
    assert without_port.state.tokens["united-kingdom-army-0"].position_id == "E"


def test_adjacent_friendly_ship_can_provide_port_but_cannot_start_supply_chain(data):
    move_initial(data, "germany-navy-0", "S")
    move_initial(data, "germany-navy-1", "T")
    data["regions"].append({"id": "U", "type": "SEA"})
    data["connections"].append({"id": "beyond-port", "a": "T", "b": "U"})
    connected = make(data)
    keys = {"germany-navy-0", "germany-navy-1"}
    battle = {"type": "SEA_BATTLE"}
    assert keys <= SupplyResolver(connected.rules).supplied(connected.state, "germany")
    assert "U" in connected.legal.targets(connected.state, "germany", battle)

    move_initial(data, "germany-army-0", "D")
    disconnected = make(data)
    assert not SupplyResolver(disconnected.rules).supplied(disconnected.state, "germany") & keys
    assert "U" not in disconnected.legal.targets(disconnected.state, "germany", battle)
