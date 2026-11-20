"""Local catalog integrity and opening-state operation smoke checks (not a rules oracle)."""
from argparse import ArgumentParser
from copy import deepcopy
from pathlib import Path
import json

from .definitions import RuleSet
from .engine import CommandProcessor
from .model import TURN_ORDER, zone
from .rules import initialize, validate_state


def check_catalog(rules):
    data = deepcopy(rules.data)
    data["settings"].update(initial_hand=0, setup_discard=0)
    smoke_rules = RuleSet(data)
    rows = []
    for definition in smoke_rules.cards.values():
        state = initialize(smoke_rules, seed=31, game_id="local-content-smoke")
        country = definition["country"]
        state.turn.country_index = TURN_ORDER.index(country)
        state.turn.phase = "Air" if definition.get("legacy_kind") == "air-power" else "Play"
        source = next(t for t in state.tokens.values() if t.token_type == "CARD" and t.definition_id == definition["id"])
        # Test fixture preparation only. Players still submit the actual CoreAPI commands below.
        from .operations import OperationExecutor, outcome
        executor = OperationExecutor(smoke_rules)
        for key in list(state.positions[zone(country, "DECK")])[:8]:
            executor.transfer(state, key, zone(country, "HAND"), outcome("FIXTURE"))
        executor.transfer(state, source.token_id, zone(country, "HAND"), outcome("FIXTURE"))
        validate_state(smoke_rules, state)
        p = CommandProcessor(smoke_rules, state)
        row = {"id": definition["id"], "kind": definition.get("legacy_kind", definition["type"])}
        if definition.get("legacy_kind") == "bolster":
            ready = [t for t in p.state.tokens.values() if t.source_token_id == source.token_id
                     and t.token_type == "ABILITY" and t.position_id == zone(country, "ABILITY_READY")]
            if not ready:
                raise ValueError("Hand listener was not installed: " + definition["id"])
            row["result"] = "hand-listener-registered"
        else:
            commands = [c for c in p.legal_commands(TURN_ORDER) if c.get("card") == source.token_id]
            if not commands:
                row["result"] = "no-opening-target"
            else:
                p.submit(commands[0], TURN_ORDER)
                choices = 0
                while p.state.decision and choices < 100:
                    commands = [c for c in p.legal_commands(TURN_ORDER) if c["type"] == "CHOOSE"]
                    preferred = next((c for c in commands if c["option"] in ("decline", "done", "pass", "confirm")), commands[0])
                    p.submit(preferred, TURN_ORDER)
                    choices += 1
                if p.state.decision or p.state.stack:
                    raise ValueError("Opening resolution failed to terminate: " + definition["id"])
                if set(p.state.tokens) != set(smoke_rules.inventory):
                    raise ValueError("Object inventory changed: " + definition["id"])
                row.update(result="opening-operation-completed", choices=choices, operations=len(p.records))
        rows.append(row)
    return {"rules_digest": rules.fingerprint, "definitions": len(rows), "copies": sum(
        t.token_type == "CARD" for t in rules.inventory.values()), "scope": "opening-state smoke; listeners registered, not individually triggered",
        "results": {kind: sum(r["result"] == kind for r in rows) for kind in sorted({r["result"] for r in rows})}, "cards": rows}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = check_catalog(RuleSet.load(args.catalog))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    print(json.dumps({k: v for k, v in report.items() if k != "cards"}), flush=True)


if __name__ == "__main__":
    main()
