"""Run a debug match with an authoritative Core and isolated versioned policies."""

from dataclasses import fields
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from time import perf_counter, sleep


OPPONENT = {"axis": "allies", "allies": "axis"}

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


def token_label(rules, token):
    definition = token["definition_id"]
    if token["token_type"] == "UNIT":
        return {"force": token["owner_country"], "unit": rules.units[definition]["kind"]}
    return {"name": rules.cards[definition].get("title", definition)}


def board_snapshot(rules, view, number, step):
    from qmg_core.model import TURN_ORDER, zone

    points = {}
    for site in rules.sites.values():
        if not site.get("dynamic"):
            region = site["region"]
            points[region] = points.get(region, 0) + site["vp"]

    def tokens_at(position, kind):
        for token_id in view["positions"].get(position, {}).get("tokens", ()):
            token = view["tokens"][token_id]
            if token["token_type"] == kind:
                yield token_label(rules, token)

    return {"game": number, "step": step, "scores": view["scores"],
            "forces": {seat: force.get("name", seat) for seat, force in rules.auxiliary_forces.items()},
            "regions": {key: {"name": region.get("name", key), "type": region["type"],
                              "vp": points.get(key, 0), "units": list(tokens_at(key, "UNIT"))}
                        for key, region in rules.regions.items()},
            "seats": {seat: {kind: list(tokens_at(zone(seat, kind), "CARD"))
                             for kind in ("HAND", "STATUS", "DISABLED_STATUS", "RESPONSE")}
                      for seat in TURN_ORDER}}


def settlement(rules, records, before, after):
    events = []
    for record in records:
        outcome = record["outcome"]
        moves = []
        for change in outcome["changes"]:
            token_id = change["token_id"]
            token = after["tokens"].get(token_id) or before["tokens"].get(token_id)
            if token and token["token_type"] in ("CARD", "UNIT"):
                moves.append({"from": change["from"], "to": change["to"], **token_label(rules, token)})
        events.append({"type": record["operation_type"], "actor": record["actor"], "moves": moves,
                       "scores": outcome["score_changes"], "ok": outcome["success"],
                       "reason": outcome["context"].get("reason")})
    decision = after["decision"]
    return {"events": events, "scores_before": before["scores"], "scores_after": after["scores"],
            "awaiting": {key: decision[key] for key in ("required_country", "decision_type")} if decision else None,
            "winner": after["winner"]}


class PolicyClient:
    def __init__(self, core, rules, config):
        from qmg_core.ai_match_policy import DEFAULT_BUDGET_MS

        self.process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--policy", core],
            cwd=core, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8")
        try:
            ready = self.exchange({"rules": {**rules.data, "initial_tokens": []}, "weights": config["weights"],
                                   "policy": config["policy"], "budget_ms": DEFAULT_BUDGET_MS})
            self.supports_move_air = ready["supports_move_air"]
        except (OSError, ValueError, RuntimeError):
            self.close()
            raise

    def exchange(self, payload):
        log_row(self.process.stdin, payload)
        self.process.stdin.flush()
        response = self.process.stdout.readline()
        if not response:
            raise RuntimeError("AI 决策进程意外退出。")
        result = json.loads(response)
        if "error" in result:
            raise ValueError(result["error"])
        return result

    def decide(self, client, country):
        view = client.observe(include_history=False)
        commands = client.legal_commands()
        if not self.supports_move_air:
            commands = [command for command in commands if command["type"] != "MOVE_AIR"]
        result = self.exchange({"view": {**view, "game_id": "debug-projection"},
                                "commands": commands, "country": country})
        return result["command"], result["row"]

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdout.close()


class SeatObservation:
    def __init__(self, view, commands):
        self.view = view
        self.commands = commands

    def observe(self, *, include_history=False):
        return self.view

    def legal_commands(self):
        return self.commands


def serve_policy(core):
    sys.path.insert(0, core)
    from qmg_core import air
    from qmg_core.ai_match_policy import DeadlineSearch, SeatPolicy
    from qmg_core.definitions import RuleSet
    from qmg_core.evaluation import ScoreEvaluator
    from qmg_core.model import CoreError, TurnClock

    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    init = json.loads(sys.stdin.readline())
    try:
        rules = RuleSet(init["rules"], projected=True)
        evaluator = ScoreEvaluator(rules, init["weights"])
    except CoreError as error:
        log_row(sys.stdout, {"error": str(error)})
        return
    search = DeadlineSearch(rules, init["weights"]) if init["policy"] == "B" else None
    policies = {}
    turn_fields = {field.name for field in fields(TurnClock)}
    log_row(sys.stdout, {"ready": True, "supports_move_air": hasattr(air, "phase_air_effect")})
    try:
        for line in sys.stdin:
            request = json.loads(line)
            country, view = request["country"], request["view"]
            # Older simulators use the subset of the public clock they understand.
            view["turn"] = {key: value for key, value in view["turn"].items() if key in turn_fields}
            if country not in policies:
                policies[country] = SeatPolicy(rules, evaluator=evaluator, search=search, budget_ms=init["budget_ms"])
            client = SeatObservation(view, request["commands"])
            command, row = policies[country].decide(client, country)
            log_row(sys.stdout, {"command": command, "row": row})
    finally:
        if search is not None:
            search.close()


class DebugRun:
    def __init__(self, directory, rules):
        self.directory = directory
        self.rules = rules
        self.meta = read_json(directory / "run.json")
        self.progress = read_json(directory / "status.json")
        self.configs = None
        self.policies = {}
        self.error = None

    def publish(self, **changes):
        self.progress.update(changes)
        write_json(self.directory / "status.json", self.progress)

    def install_configs(self, request):
        from qmg_core.model import digest

        configs = request["configs"]
        staged = {}
        for team, config in configs.items():
            if team in self.policies and self.configs[team] == config:
                continue
            try:
                staged[team] = PolicyClient(request["paths"][config["version"]], self.rules, config)
            except (OSError, ValueError, RuntimeError) as error:
                for policy in staged.values():
                    policy.close()
                actual_team = OPPONENT[team] if self.progress["match"]["swapped"] else team
                raise ValueError(("轴心" if actual_team == "axis" else "同盟") + " · " + str(error)) from error
        for team, policy in staged.items():
            if team in self.policies:
                self.policies[team].close()
            self.policies[team] = policy
        if configs != self.configs:
            self.configs = configs
            self.progress["weights_revision"] += 1
            self.progress["configs"] = configs
            with (self.directory / "parameters.jsonl").open("a", encoding="utf-8") as stream:
                log_row(stream, {"at": timestamp(), "revision": self.progress["weights_revision"],
                    "game": self.progress["match"]["number"], "after_decision": self.progress["step"],
                    "configs": {team: {**config, "weights_digest": digest(config["weights"])}
                                for team, config in configs.items()}})

    def close_policies(self):
        for policy in self.policies.values():
            policy.close()
        self.policies.clear()

    def before_decision(self):
        while True:
            request = read_json(self.directory / "control.json")
            if request["action"] == "stop":
                self.publish(state="stopped", control_revision=request["revision"], ended_at=timestamp())
                return False
            if request["revision"] != self.progress["control_revision"]:
                if request["configs"] != self.configs or not self.policies:
                    try:
                        self.install_configs(request)
                    except ValueError as error:
                        self.error = str(error)
                    else:
                        self.error = None
                else:
                    self.error = None
                self.publish(control_revision=request["revision"], error=self.error,
                             state="paused" if request["action"] == "pause" or self.error else
                             "stepping" if request["action"] == "step" else "running")
            if self.progress["state"] != "paused":
                if not self.policies:
                    self.install_configs(request)
                return True
            sleep(0.1)

    def match(self, number, seed):
        from qmg_core.api import CoreAPI
        from qmg_core.engine import CommandProcessor
        from qmg_core.model import CoreError, TURN_ORDER, digest

        swapped = number % 2 == 0
        self.publish(match={"number": number, "seed": seed, "swapped": swapped}, step=0, round=1,
                     phase="Setup", country=None, scores={"axis": 0, "allies": 0})
        started = perf_counter()
        game_id = f"{self.meta['id']}-{number}"
        processor = CommandProcessor.create(self.rules, seed=seed, game_id=game_id)
        clients = {seat: CoreAPI(processor, (seat,)) for seat in TURN_ORDER}
        # The local judge publishes a separate display. Policies only receive their own seats.
        judge = CoreAPI(processor, TURN_ORDER)
        view = judge.observe(include_history=False)
        board_file = self.directory / f"board-{number}.json"
        write_json(board_file, board_snapshot(self.rules, view, number, 0))
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
                team = self.rules.team(actor)
                slot = OPPONENT[team] if swapped else team
                config = self.configs[slot]
                command, row = self.policies[slot].decide(client, actor)
                before = view
                cursor = len(processor.records)
                client.apply(command)
                view = judge.observe(include_history=False)
                step = self.progress["step"] + 1
                row.update(decision_index=step, team=team, config_slot=slot, policy=config["policy"],
                           ai_version=config["version"], ai_commit=config["commit"],
                           active_country=active, seed=seed, game_id=game_id, names=decision_names(self.rules, row),
                           weights_revision=self.progress["weights_revision"], weights_digest=digest(config["weights"]),
                           scores=view["scores"], settlement=settlement(self.rules, processor.records[cursor:], before, view))
                log_row(stream, row)
                write_json(board_file, board_snapshot(self.rules, view, number, step))
                result = {"step": step, "scores": view["scores"], "round": view["turn"]["round_id"],
                          "phase": view["turn"]["phase"], "country": view["decision"]["required_country"] if
                          view["decision"] else TURN_ORDER[view["turn"]["country_index"]]}
                if self.progress["state"] == "stepping":
                    result.update(state="paused", last_step={"game": number, "step": step,
                                                               "revision": self.progress["control_revision"]})
                self.publish(**result)
        with (self.directory / "games.jsonl").open("a", encoding="utf-8") as stream:
            log_row(stream, {**self.progress["match"], "winner": view["winner"], "scores": view["scores"],
                "steps": self.progress["step"], "record_head": processor.record_head,
                "seconds": round(perf_counter() - started, 3)})
        self.publish(completed=number, round=view["turn"]["round_id"], phase=view["turn"]["phase"])
        return True

    def run(self):
        seed = self.meta["seed"]
        try:
            number = 1
            while self.match(number, seed):
                if self.meta["mode"] == "single" or (self.meta["mode"] == "paired" and number == 2):
                    self.publish(state="completed", ended_at=timestamp())
                    break
                if not self.before_decision():
                    break
                # New games get fresh plans, even when the scoring weights are unchanged.
                self.close_policies()
                number += 1
                if number % 2 == 1:
                    seed = (seed + 1) % (2 ** 32)
        finally:
            self.close_policies()


def main():
    directory, core = map(Path, sys.argv[1:])
    sys.path.insert(0, str(core))
    from qmg_core.definitions import RuleSet
    from qmg_core.model import CoreError

    try:
        sys.stdin.reconfigure(encoding="utf-8")
        rules = RuleSet(json.load(sys.stdin))
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
    if sys.argv[1] == "--policy":
        serve_policy(sys.argv[2])
    else:
        main()
