"""Run complete, headless six-seat pairs: bounded search vs the same pure rule policy."""
from argparse import ArgumentParser
from collections import Counter
from datetime import datetime, timezone
import json
from math import ceil, isfinite
from pathlib import Path
from statistics import mean, median
from time import perf_counter

from .ai_match_policy import DEFAULT_BUDGET_MS, DeadlineSearch, SeatPolicy
from .api import CoreAPI
from .definitions import RuleSet
from .engine import CommandProcessor
from .evaluation import ScoreEvaluator
from .model import CoreError, TURN_ORDER, digest
from .replay import LIBRARY, ReplayBatch, replay_game


DATA = Path(__file__).resolve().parent.parent / "data"


def play_game(rules, weights, *, seed, search_team, width, budget_ms, output, command_limit=10000):
    started = perf_counter()
    game_id = f"s{seed}-b-{search_team}"
    processor = CommandProcessor.create(rules, seed=seed, game_id=game_id)
    clients = {seat: CoreAPI(processor, (seat,)) for seat in TURN_ORDER}
    sides = {seat: "B" if rules.team(seat) == search_team else "A" for seat in TURN_ORDER}
    search = DeadlineSearch(rules, weights)
    evaluator = ScoreEvaluator(rules, weights)
    policies = {seat: SeatPolicy(rules, search=search if sides[seat] == "B" else None,
                                 evaluator=evaluator, width=width, budget_ms=budget_ms) for seat in TURN_ORDER}
    times = {"A": [], "B": []}
    modes = {"A": Counter(), "B": Counter()}
    search_statuses = Counter()
    budget_misses = Counter()
    decisions = Counter()
    log_path = output / (game_id + ".jsonl")
    try:
        # The host uses the same interface, with a public projection for the initial dispatch.
        view = CoreAPI(processor, ()).observe(include_history=False)
        with log_path.open("x", encoding="utf-8", buffering=1) as log:
            while not view["winner"]:
                if sum(decisions.values()) >= command_limit:
                    raise CoreError(f"{game_id} reached the command limit before a rules-defined result")
                turn = view["turn"]
                actor = view["decision"]["required_country"] if view["decision"] else TURN_ORDER[turn["country_index"]]
                client, side = clients[actor], sides[actor]
                command, row = policies[actor].decide(client, actor)
                row.update(policy=side, seed=seed, game_id=game_id, decision_index=sum(decisions.values()) + 1,
                           active_country=TURN_ORDER[turn["country_index"]])
                log.write(json.dumps(row, ensure_ascii=False) + "\n")
                times[side].append(row["elapsed_ms"])
                modes[side][row["mode"]] += 1
                budget_misses[side] += row["elapsed_ms"] > budget_ms
                if side == "B":
                    search_statuses[row["search_status"]] += 1
                decisions[actor] += 1
                view = client.submit(command, include_history=False)
    finally:
        search.close()
    scores = view["scores"]
    enemy = "axis" if search_team == "allies" else "allies"
    result = "draw" if view["winner"] == "draw" else "win" if view["winner"] == search_team else "loss"
    return {"seed": seed, "game_id": game_id, "search_team": search_team, "winner": view["winner"],
            "result_b": result, "scores": scores, "vp_diff_b": scores[search_team] - scores[enemy],
            "ended_in_round": view["turn"]["round_id"], "ended_in_phase": view["turn"]["phase"],
            "seats": sides, "decisions_by_seat": {seat: decisions[seat] for seat in TURN_ORDER},
            "modes": modes, "search_statuses": search_statuses, "over_budget": budget_misses,
            "latency_ms": {side: latency(values) for side, values in times.items()},
            "seconds": round(perf_counter() - started, 3), "decision_log": log_path.name,
            "record_head": processor.record_head}, times


def latency(samples):
    ordered = sorted(samples)
    if not ordered:
        return {"count": 0, "p50": None, "p95": None, "max": None}
    return {"count": len(ordered), "p50": round(median(ordered), 3),
            "p95": round(ordered[ceil(len(ordered) * 0.95) - 1], 3),
            "max": round(ordered[-1], 3)}


def summarize(games, times):
    def outcomes(rows):
        results = Counter(game["result_b"] for game in rows)
        return {"games": len(rows), "wins": results["win"], "draws": results["draw"],
                "losses": results["loss"], "win_rate": results["win"] / len(rows),
                "mean_vp_diff_b": mean(game["vp_diff_b"] for game in rows)}

    pairs = []
    for seed in dict.fromkeys(game["seed"] for game in games):
        pair = [game for game in games if game["seed"] == seed]
        if len(pair) == 2:
            pairs.append({"seed": seed, "mean_vp_diff_b": mean(game["vp_diff_b"] for game in pair)})
    over_budget = {side: sum(g["over_budget"][side] for g in games) for side in times}
    modes = {side: dict(sum((Counter(g["modes"][side]) for g in games), Counter())) for side in times}
    return {"overall": outcomes(games),
            "by_search_team": {team: outcomes(rows) for team in ("axis", "allies")
                               if (rows := [g for g in games if g["search_team"] == team])},
            "pairs": pairs, "latency_ms": {side: latency(values) for side, values in times.items()},
            "over_budget": over_budget, "modes": modes,
            "search_statuses": dict(sum((Counter(g["search_statuses"]) for g in games), Counter()))}


def run_matches(rules, weights, *, seeds, output, width, budget_ms, command_limit=10000, report_game=None, replay=None):
    ScoreEvaluator(rules, weights)  # Validate configuration before the first live match.
    output.mkdir(parents=True, exist_ok=True)
    games, times = [], {"A": [], "B": []}
    report = {"rules_digest": rules.fingerprint, "weights_digest": digest(weights), "seeds": list(seeds),
              "budget_ms": budget_ms, "shortlist_width": width, "nodes_per_candidate": weights["max_simulation_nodes"],
              "strategy": {"A": "rules", "B": "shortlist-projected-settlement-with-A-fallback"},
              "games": games}
    if replay is not None:
        report["replay"] = {k: replay.info[k] for k in ("name", "ai_version", "ai_note", "date")}
    with (output / "summary.json").open("x", encoding="utf-8"):
        pass
    for seed in seeds:
        for team in ("axis", "allies"):
            game, samples = play_game(rules, weights, seed=seed, search_team=team, output=output,
                                      width=width, budget_ms=budget_ms, command_limit=command_limit)
            games.append(game)
            for side in times:
                times[side].extend(samples[side])
            report["metrics"] = summarize(games, times)
            pending_report = output / "summary.partial.json"
            pending_report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            pending_report.replace(output / "summary.json")
            if replay is not None:
                replay.add(game, replay_game(rules, game, output / game["decision_log"]))
            if report_game is not None:
                report_game(game)
    return report


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--seeds", type=int, nargs="+", default=[31, 73, 127])
    parser.add_argument("--budget-ms", type=float, default=DEFAULT_BUDGET_MS)
    parser.add_argument("--width", type=int, default=3)
    parser.add_argument("--nodes", type=int, default=16)
    parser.add_argument("--command-limit", type=int, default=10000)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--replay-library", type=Path, default=LIBRARY)
    parser.add_argument("--name", help="Readable batch name in the replay library")
    parser.add_argument("--ai-version", default="AI 试验版 v1.21")
    parser.add_argument("--ai-note", default="调配逐种结算并与直接出牌比较。牌差只计一次，过牌收益单列。")
    args = parser.parse_args()
    if not isfinite(args.budget_ms) or args.budget_ms <= 0 or args.width < 1 or args.command_limit < 1:
        parser.error("Budget, shortlist width and command limit must be positive")
    if len(args.seeds) != len(set(args.seeds)):
        parser.error("Use distinct seeds for independent pairs")
    rules = RuleSet.load(args.catalog or DATA / "architecture_fixture.json")
    weights = json.loads((DATA / "ai_test_weights.json").read_text(encoding="utf-8"))
    weights["max_simulation_nodes"] = args.nodes
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    output = args.output or args.replay_library.parent / "ai-tests" / stamp
    batch = ReplayBatch(args.replay_library, output, rules,
                        name=args.name or f"六席 AI · {output.name}", version=args.ai_version, note=args.ai_note,
                        settings={"budget_ms": args.budget_ms, "shortlist_width": args.width,
                                  "nodes_per_candidate": args.nodes, "weights_digest": digest(weights)})
    report = run_matches(rules, weights, seeds=args.seeds, output=output, width=args.width, replay=batch,
                         budget_ms=args.budget_ms, command_limit=args.command_limit,
                         report_game=lambda game: print(json.dumps(game), flush=True))
    print(json.dumps(report["metrics"]))
    print(args.replay_library / "打开录像.html")


if __name__ == "__main__":
    main()
