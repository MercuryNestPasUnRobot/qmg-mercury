"""Local catalog integrity and opening-state operation smoke checks (not a rules oracle)."""
from argparse import ArgumentParser
from copy import deepcopy
from pathlib import Path
import json

from .definitions import RuleSet
from .engine import CommandProcessor
from .model import TURN_ORDER, zone
from .rules import initialize, validate_state


def check_catalog(rules, *, trace=False):
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
            commands = [c for c in p.legal_commands(TURN_ORDER)
                        if c.get("card") == source.token_id and c["type"] in ("PLAY_CARD", "USE_AIR")]
            if not commands:
                row["result"] = "no-opening-target"
            else:
                cases = []
                for command in commands if trace else commands[:1]:
                    p = CommandProcessor(smoke_rules, state)
                    p.submit(command, (country,))
                    choices = settle_choices(p, take_optional=trace)
                    finish = p.state
                    if finish.stack or finish.announcements or finish.positions["RESOLUTION"]:
                        raise ValueError("Opening resolution did not close: " + definition["id"])
                    if finish.tokens.keys() != smoke_rules.inventory.keys():
                        raise ValueError("Object inventory changed: " + definition["id"])
                    case = {"command": command["type"], "choices": choices, "operations": len(p.records),
                            "destination": finish.tokens[source.token_id].position_id}
                    for field in ("play_as", "force"):
                        if field in command:
                            case[field] = command[field]
                    if trace:
                        case["effect_trace"] = [
                            {"instruction": r["operation"]["effect"]["type"], "outcome": r["operation_type"]}
                            for r in p.records if r["source"] == source.token_id and r["operation"]["type"] == "CARD_EFFECT"
                        ]
                        case["unsuccessful_steps"] = [
                            {"operation": r["operation_type"], "reason": r["outcome"]["context"].get("reason"),
                             "card": smoke_rules.inventory[r["source"]].definition_id if r["source"] else None}
                            for r in p.records if not r["outcome"]["success"]
                        ]
                    cases.append(case)
                result = ("opening-stopped-at-rule" if trace and all(case["unsuccessful_steps"] for case in cases)
                          else "opening-operation-completed")
                row.update(result=result, choices=cases[0]["choices"], operations=cases[0]["operations"])
                if trace:
                    row["cases"] = cases
        rows.append(row)
    return {"rules_digest": rules.fingerprint, "definitions": len(rows), "copies": sum(
        t.token_type == "CARD" for t in rules.inventory.values()), "scope": "opening-state smoke; listeners registered, not individually triggered",
        "results": {kind: sum(r["result"] == kind for r in rows) for kind in sorted({r["result"] for r in rows})}, "cards": rows}


def settle_choices(processor, *, take_optional=False, preferred_listener=None, limit=100):
    for count in range(limit + 1):
        decision = processor.state.decision
        if decision is None:
            return count
        if count == limit:
            break
        commands = [c for c in processor.legal_commands((decision.required_country,)) if c["type"] == "CHOOSE"]
        pass_options = {"decline", "done", "pass", "cancel", "confirm"}
        if preferred_listener is not None and decision.continuation["type"] == "RESPONSE_SELECTED":
            candidates = decision.continuation["candidates"]
            option = ("use" if len(candidates) == 1 else preferred_listener) if preferred_listener in candidates else "decline"
            preferred = next(c for c in commands if c["option"] == option)
        else:
            preferred = next((c for c in commands if (c["option"] in pass_options) != take_optional), commands[0])
        processor.submit(preferred, (decision.required_country,))
    raise ValueError("Resolution exceeded the audit choice limit")


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
