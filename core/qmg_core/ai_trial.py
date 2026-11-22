"""Small reproducible feasibility comparisons for the AI test profile."""
from argparse import ArgumentParser
from copy import deepcopy
import json
from pathlib import Path
from statistics import median
from time import perf_counter

from .api import CoreAPI
from .ai_match_policy import CandidateView
from .content_verify import settle_choices
from .definitions import RuleSet
from .engine import CommandProcessor
from .evaluation import OneStepPlanner
from .model import TURN_ORDER, digest


DATA = Path(__file__).resolve().parent.parent / "data"
ACTION_PRIORITY = {"GAIN_VP": 2.0, "BUILD_ARMY": 1.6, "RECRUIT": 1.6, "BUILD_NAVY": 1.4,
                   "LAND_BATTLE": 1.3, "SEA_BATTLE": 1.3, "DRAW": 0.45}
EFFECT_PRIORITY = {"points": 1.5, "choose-space": 1.55, "recruit": 1.55, "build": 1.55,
                   "battle": 1.3, "battle-series": 1.3, "choose-unit": 1.2, "air-action": 1.0,
                   "discard-top": 0.65, "play-from-zone": 0.6, "search-deck": 0.35}


def priority(rules, visible, command):
    if command["type"] == "END_PHASE":
        return -1
    if command["type"] == "REALLOCATE":
        return 0.1
    if command["type"] == "USE_AIR":
        return 1.0
    token = visible.get(command.get("card"))
    if token is None:
        return 0.0
    if mode := command.get("play_as"):
        return ACTION_PRIORITY.get(mode, 0.4)
    card = rules.cards[token["definition_id"]]
    effects = card["effects"]
    if command["type"] == "USE_STATUS":
        effects = rules.abilities[card["active_ability"]]["effects"]
    total = 0.25 if card["type"] in ("RESPONSE", "STATUS") else 0.0
    for step in effects:
        op = step.get("operation", step)
        if op["type"] == "CARD_EFFECT":
            effect = op["effect"]
            total += EFFECT_PRIORITY.get(effect["type"], 0.2)
        elif op["type"] == "GAIN_VP":
            total += ACTION_PRIORITY["GAIN_VP"] * max(0, op["amount"])
        else:
            total += ACTION_PRIORITY.get(op["type"], 0.25)
    return total


def compare_candidates(planner, client, seat, width):
    view = client.observe()
    ordered = sorted(client.legal_commands(), key=lambda c: priority(planner.evaluator.rules, view["tokens"], c),
                     reverse=True)
    short = ordered[:width]
    fallback = next((c for c in ordered if c["type"] == "END_PHASE"), None)
    if fallback is not None and fallback not in short:
        short.append(fallback)
    metrics = {}
    for label, commands in (("all", ordered), ("shortlist", short)):
        start = perf_counter()
        ranked = planner.rank(CandidateView(view, commands), seat)
        elapsed = perf_counter() - start
        completed = [row for row in ranked if row["complete"]]
        best = completed[0] if completed else None
        metrics[label] = {"commands": len(commands), "completed": len(completed),
                          "nodes": sum(r["nodes"] for r in ranked), "seconds": round(elapsed, 4),
                          "best": ({key: best[key] for key in ("command", "q", "terminal_rank")} if best else None),
                          "incomplete_reasons": sorted({r["reason"] for r in ranked if not r["complete"]})}
    reference, accelerated = (metrics[label]["best"] for label in ("all", "shortlist"))
    metrics["matched"] = bool(reference and accelerated and
                              (reference["q"], reference["terminal_rank"]) == (accelerated["q"], accelerated["terminal_rank"]))
    metrics["heuristic_first"] = ordered[0]
    metrics["heuristic_matches_reference"] = bool(reference and reference["command"] == ordered[0])
    return metrics


def compare_openings(rules, weights, seed, width, cases):
    processor = CommandProcessor.create(rules, seed=seed)
    settle_choices(processor)
    planner = OneStepPlanner(rules, weights)
    rows = []
    for seat in TURN_ORDER[:cases]:
        client = CoreAPI(processor, (seat,))
        before = processor.state
        result = compare_candidates(planner, client, seat, width)
        if processor.state != before:
            raise ValueError("AI comparison modified the match it was evaluating")
        rows.append({"seat": seat, **result})
        # Play through the same reference trajectory to obtain the next seat's decision.
        commands = client.legal_commands()
        opening = next((c for c in commands if c["type"] == "PLAY_CARD"), None)
        if opening:
            client.submit(opening)
            settle_choices(processor)
        for _ in rules.phases:
            client.submit(next(c for c in client.legal_commands() if c["type"] == "END_PHASE"))
            settle_choices(processor)
            if processor.state.winner:
                return rows
    return rows


def hidden_response_probe(fixture):
    """Two legal, authored worlds for a synthetic response. No real deck is sampled."""
    data = deepcopy(fixture.data)
    data["settings"]["victory_margin"] = 0
    cards = {card["id"]: card for card in data["cards"]}
    cards["g-chain"]["effects"] = [{"type": "BUILD_ARMY", "target": "B"}, {"type": "GAIN_VP", "amount": 3}]
    cards["g-build"]["effects"] = [{"type": "GAIN_VP", "amount": 2}]
    for card, ability, trigger, effect in (
        ("uk-secret", "trial-cancel", {"operation_type": "ANNOUNCE", "announced_type": "BUILD_ARMY", "actor": "enemy"},
         [{"type": "CANCEL", "announcement_id": "$event.announcement_id"}]),
        ("uk-other", "trial-idle", {"operation_type": "TURN_END"}, []),
    ):
        cards[card].update(type="RESPONSE", abilities=[ability], effects=[])
        data["abilities"].append({"id": ability, "trigger": trigger, "activation_mode": "AUTO",
                                  "after": "delete", "priority": 997, "effects": effect})
    perspectives, outcomes = [], {}
    for label, definition in (("response_can_cancel", "uk-secret"), ("response_cannot_cancel", "uk-other")):
        world = deepcopy(data)
        token = next(t for t in world["initial_tokens"] if t["token_id"] == "uk-hidden")
        token.update(definition_id=definition, position_id="united-kingdom:RESPONSE")
        rules = RuleSet(world)
        p = CommandProcessor.create(rules, game_id="ai-hidden-probe")
        perspectives.append(CoreAPI(p, ("germany",)).observe())
        values = {}
        for name, source in (("risky", "g-chain"), ("safe", "g-build")):
            branch = CommandProcessor.create(rules, game_id="ai-hidden-probe")
            client = CoreAPI(branch, ("germany",))
            client.submit(next(c for c in client.legal_commands() if c.get("card") == source))
            settle_choices(branch)
            values[name] = branch.state.scores["axis"]
        outcomes[label] = values
    if perspectives[0] != perspectives[1]:
        raise ValueError("The synthetic worlds do not have the same public observation")
    return {"same_observation": True, "outcomes_vp": outcomes,
            "equal_weight_mean": {k: sum(r[k] for r in outcomes.values()) / len(outcomes) for k in ("risky", "safe")}}


def summary(rows):
    return {
        "decisions": len(rows), "shortlist_matches": sum(row["matched"] for row in rows),
        "heuristic_matches": sum(row["heuristic_matches_reference"] for row in rows),
        **{label: {"completed": sum(row[label]["completed"] for row in rows),
                   "commands": sum(row[label]["commands"] for row in rows),
                   "nodes": sum(row[label]["nodes"] for row in rows),
                   "median_seconds": median(row[label]["seconds"] for row in rows)} for label in ("all", "shortlist")},
    }


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--seed", type=int, default=31)
    parser.add_argument("--cases", type=int, choices=range(1, 7), default=6)
    parser.add_argument("--nodes", type=int, default=16)
    parser.add_argument("--width", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.width < 1:
        parser.error("Shortlist width must be positive")
    fixture = RuleSet.load(DATA / "architecture_fixture.json")
    rules = RuleSet.load(args.catalog) if args.catalog else fixture
    weights = json.loads((DATA / "ai_test_weights.json").read_text(encoding="utf8"))
    weights["max_simulation_nodes"] = args.nodes
    probe = hidden_response_probe(fixture)
    rows = compare_openings(rules, weights, args.seed, args.width, args.cases)
    report = {"rules_digest": rules.fingerprint, "weights_digest": digest(weights), "seed": args.seed,
              "node_limit_per_candidate": args.nodes, "shortlist_width": args.width,
              "metrics": summary(rows), "hidden_response_probe": probe, "decisions": rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    print(json.dumps({key: value for key, value in report.items() if key != "decisions"}))


if __name__ == "__main__":
    main()
