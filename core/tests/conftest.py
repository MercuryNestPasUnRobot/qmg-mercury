from copy import deepcopy
import json
from pathlib import Path
import pytest

from qmg_core import CommandProcessor, RuleSet


@pytest.fixture
def data():
    return json.loads(Path("data/architecture_fixture.json").read_text(encoding="utf-8"))


@pytest.fixture
def weights():
    return json.loads(Path("data/evaluation_weights.json").read_text(encoding="utf-8"))


def make(data, **kwargs):
    rules = RuleSet(deepcopy(data))
    return CommandProcessor.create(rules, **kwargs)


def move_initial(data, token_id, position):
    next(t for t in data["initial_tokens"] if t["token_id"] == token_id)["position_id"] = position


def card(data, card_id):
    return next(c for c in data["cards"] if c["id"] == card_id)


def play(processor, token_id, auto_resolve=True):
    command = next(c for c in processor.legal_commands(("germany",)) if c.get("card") == token_id)
    processor.submit(command, ("germany",), auto_resolve=auto_resolve)


def choose(processor, option):
    country = processor.state.decision.required_country
    command = next(c for c in processor.legal_commands((country,)) if c["option"] == option)
    processor.submit(command, (country,))


def finish_choices(processor):
    for _ in range(100):
        if not processor.state.decision:
            return
        choose(processor, processor.state.decision.legal_options[0])
    raise AssertionError("Choice loop did not terminate")
