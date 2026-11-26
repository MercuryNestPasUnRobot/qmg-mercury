"""Run a debug session in its selected Core version, isolated from the control panel."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from time import perf_counter, sleep


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path):
    for attempt in range(8):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == 7:
                raise
            sleep(0.015)


def write_json(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    # A short-lived reader may still have the destination open on Windows.
    for attempt in range(8):
        try:
            temp.replace(path)
            return
        except PermissionError:
            if attempt == 7:
                raise
            sleep(0.015)


def log_row(stream, value):
    stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")


def decision_names(rules, row):
    names = {}
    for item in (row, *row["rule_scores"], *row["candidate_scores"]):
        command = item["command"]
        for field in ("card", "option"):
            key = command.get(field)
            if key is None or key in names:
                continue
            token = rules.inventory.get(key)
            if token and token.token_type == "CARD":
                names[key] = rules.cards[token.definition_id].get("title", token.definition_id)
            elif key in rules.regions:
                names[key] = rules.regions[key].get("name", key)
    return names


class DebugRun:
    def __init__(self, directory, rules):
        self.directory = directory
        self.rules = rules
        self.meta = read_json(directory / "run.json")
        self.progress = read_json(directory / "status.json")
        self.weights = None
        self.search = None
        self.policies = {}
        self.error = None

    def publish(self, **changes):
        self.progress.update(changes)
        write_json(self.directory / "status.json", self.progress)

    def install_weights(self, weights):
        from qmg_core.ai_match_policy import DeadlineSearch, SeatPolicy
        from qmg_core.evaluation import ScoreEvaluator
        from qmg_core.model import TURN_ORDER, digest

        # Validate the whole profile in the exact version that will execute it.
        evaluator = ScoreEvaluator(self.rules, weights)
        if self.search is not None:
            self.search.close()
        self.search = DeadlineSearch(self.rules, weights)
        self.policies = {seat: SeatPolicy(self.rules, evaluator=evaluator,
            search=self.search if self.rules.team(seat) == self.progress["match"]["search_team"] else None,
            width=3, budget_ms=3000) for seat in TURN_ORDER}
        if self.weights != weights:
            self.weights = weights
            self.progress["weights_revision"] += 1
            self.progress["weights"] = weights
            with (self.directory / "parameters.jsonl").open("a", encoding="utf-8") as stream:
                log_row(stream, {"at": timestamp(), "revision": self.progress["weights_revision"],
                    "game": self.progress["match"]["number"], "after_decision": self.progress["step"],
                    "weights_digest": digest(weights), "weights": weights})

    def before_decision(self):
        from qmg_core.model import CoreError

        while True:
            request = read_json(self.directory / "control.json")
            if request["action"] == "stop":
                self.publish(state="stopped", control_revision=request["revision"], ended_at=timestamp())
                return False
            if request["revision"] != self.progress["control_revision"] or (self.search is None and not self.error):
                if request["weights"] != self.weights or self.search is None:
                    try:
                        self.install_weights(request["weights"])
                    except CoreError as error:
                        self.error = str(error)
                    else:
                        self.error = None
                else:
                    self.error = None
                self.publish(control_revision=request["revision"], error=self.error,
                             state="paused" if request["action"] == "pause" or self.error else "running")
            if self.progress["state"] != "paused":
                return True
            sleep(0.1)

    def match(self, number, seed, side):
        from qmg_core.api import CoreAPI
        from qmg_core.engine import CommandProcessor
        from qmg_core.model import CoreError, TURN_ORDER, digest

        self.publish(match={"number": number, "seed": seed, "search_team": side}, step=0, round=1,
                     phase="Setup", country=None, scores={"axis": 0, "allies": 0})
        if not self.before_decision():
            return False
        started = perf_counter()
        game_id = f"{self.meta['id']}-{number}"
        processor = CommandProcessor.create(self.rules, seed=seed, game_id=game_id)
        clients = {seat: CoreAPI(processor, (seat,)) for seat in TURN_ORDER}
        view = CoreAPI(processor, ()).observe(include_history=False)
        with (self.directory / f"game-{number}.jsonl").open("x", encoding="utf-8", buffering=1) as stream:
            while not view["winner"]:
                turn = view["turn"]
                active = TURN_ORDER[turn["country_index"]]
                actor = view["decision"]["required_country"] if view["decision"] else active
                self.progress.update(round=turn["round_id"], phase=turn["phase"], country=actor, scores=view["scores"])
                if not self.before_decision():
                    return False
                if self.progress["step"] >= 10000:
                    raise CoreError("本局超过 10000 步，已保留调试日志。")
                client = clients[actor]
                command, row = self.policies[actor].decide(client, actor)
                view = client.submit(command, include_history=False)
                step = self.progress["step"] + 1
                row.update(decision_index=step, policy="B" if self.rules.team(actor) == side else "A",
                           active_country=active, seed=seed, game_id=game_id, names=decision_names(self.rules, row),
                           weights_revision=self.progress["weights_revision"], weights_digest=digest(self.weights),
                           scores=view["scores"])
                log_row(stream, row)
                self.publish(step=step, scores=view["scores"])
        with (self.directory / "games.jsonl").open("a", encoding="utf-8") as stream:
            log_row(stream, {**self.progress["match"], "winner": view["winner"], "scores": view["scores"],
                "steps": self.progress["step"], "record_head": processor.record_head,
                "seconds": round(perf_counter() - started, 3)})
        self.publish(completed=number, round=view["turn"]["round_id"], phase=view["turn"]["phase"])
        return True

    def run(self):
        seed = self.meta["seed"]
        side = self.meta["first_team"]
        try:
            number = 1
            while self.match(number, seed, side):
                if self.meta["mode"] == "single" or (self.meta["mode"] == "paired" and number == 2):
                    self.publish(state="completed", ended_at=timestamp())
                    break
                # New games get fresh plans, even when the scoring weights are unchanged.
                self.search.close()
                self.search = None
                number += 1
                side = "allies" if side == "axis" else "axis"
                if number % 2 == 1:
                    seed = (seed + 1) % (2 ** 32)
        finally:
            if self.search is not None:
                self.search.close()


def main():
    directory, core, catalog = map(Path, sys.argv[1:])
    sys.path.insert(0, str(core))
    from qmg_core.definitions import RuleSet
    from qmg_core.model import CoreError

    try:
        rules = RuleSet.load(catalog)
        run = DebugRun(directory, rules)
        if run.meta["rules_digest"] and run.meta["rules_digest"] != rules.fingerprint:
            raise CoreError("所选卡库与该版本的校验值不符。")
        run.publish(rules_digest=rules.fingerprint)
        run.run()
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        progress = read_json(directory / "status.json")
        write_json(directory / "status.json", {**progress, "state": "failed", "ended_at": timestamp(),
                                              "error": str(error)})
        raise


if __name__ == "__main__":
    main()
