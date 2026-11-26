"""A local human seat with five independently configured AI opponents and allies."""

from collections import Counter
from datetime import datetime, timezone
from http import HTTPStatus
import json
from secrets import randbits
from threading import Condition, Thread
from uuid import uuid4

from .ai_debug_worker import PolicyClient, board_snapshot, log_row, write_json
from .api import CoreAPI
from .engine import CommandProcessor
from .evaluation import ScoreEvaluator
from .map_variants import match_rules
from .model import CoreError, TURN_ORDER, digest, zone
from .replay import COUNTRIES, ReplayBatch, replay_game


TERMINAL = {"completed", "stopped", "failed"}
UNIT_NAMES = {"ARMY": "陆军", "NAVY": "海军", "AIR": "空军"}
ACTION_NAMES = {"PLAY_CARD": "打出", "USE_STATUS": "发动状态牌", "ACTIVATE_CARD": "主动响应",
                "DISCARD_CARD": "弃置", "USE_AIR": "空军行动", "MOVE_AIR": "弃牌移动空军", "END_PHASE": "结束阶段",
                "FORFEIT_PLAY": "无牌可弃，扣 1 VP", "BUILD_ARMY": "建设陆军", "BUILD_NAVY": "建设海军",
                "LAND_BATTLE": "陆战", "SEA_BATTLE": "海战"}
CHOICE_NAMES = {"use": "发动", "decline": "放弃", "pass": "跳过", "confirm": "确认", "cancel": "取消",
                "skip": "跳过", "done": "完成", "yes": "确认", "no": "不发动", "air": "空军", "unit": "陆军或海军",
                "discard": "弃牌", "discard-top": "弃置牌库顶", "return": "收回牌",
                "play": "打出", "deck": "牌库", "hand": "手牌"}


class PlayerError(ValueError):
    def __init__(self, message, status=HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


def force_name(rules, key):
    return COUNTRIES.get(key, rules.auxiliary_forces.get(key, {}).get("name", key))


def card_details(rules, definition):
    card = rules.cards[definition]
    return {"name": card.get("title", definition), "text": card.get("text") or ""}


def command_description(rules, view, command):
    """Use only the human's visible cards and Core-authorized choices for labels."""
    decision = view["decision"] or {}
    kind = command["type"]

    def describe(key, definition=None):
        token = view["tokens"].get(key)
        if token and token["token_type"] == "UNIT":
            return {"name": force_name(rules, token["owner_country"]) + UNIT_NAMES[rules.units[token["definition_id"]]["kind"]]}
        if token and token["token_type"] == "CARD":
            definition = token["definition_id"]
        return card_details(rules, definition) if definition in rules.cards else None

    if kind == "REALLOCATE":
        return {"label": f"调配资源 · 弃 {view.get('reallocation', {}).get('cost', 3)} 找 1", "opening": True}
    if kind == "CHOOSE":
        option = command["option"]
        source = decision.get("response_sources", {}).get(option)
        known = (decision.get("deck_order", {}).get("options", {}).get(option) or
                 view.get("reallocation", {}).get("targets", {}).get(option) or
                 decision.get("visible_choices", {}).get(option))
        if details := describe(source or option, known):
            label = details["name"]
            if source:
                label = "发动 · " + label
            elif view["turn"]["phase"] == "Setup" or decision.get("hand_discard") or decision.get("operation_type") == "DISCARD":
                label = "弃置 · " + label
            elif decision.get("operation_type") in ("COST_SELECTED", "PAYMENT_SELECTED", "REALLOCATION_SELECTED"):
                label = "支付 · " + label
            return {"label": label, "text": details.get("text", "")}
        if option == "done" and decision.get("map_recruit"):
            if decision["legal_options"] == ["done"]:
                return {"label": "结束征召 · 已无其他合法落点或预备部队"}
            return {"label": "跳过额外征召"}
        target = option.rsplit("|", 1)[-1]
        if decision.get("operation_type") == "AIR_ACTION_SELECTED":
            action, unit, target, _ = json.loads(option)
            label = {"deploy": "部署空军", "marshal": "调动空军", "superiority": "争夺制空"}.get(action, "空军行动")
            if action == "marshal" and unit in view["tokens"]:
                origin = view["tokens"][unit]["position_id"]
                return {"label": f"{label} · {rules.regions[origin].get('name', origin)} → "
                                 f"{rules.regions[target].get('name', target)}", "region": target}
        elif target in rules.regions:
            label = ("空降至" if option in decision.get("map_airdrop_options", ()) else
                     "攻击" if decision.get("map_battle") else
                     "额外征召" if decision.get("map_recruit") and decision.get("optional_flag") else
                     "征召" if decision.get("map_recruit") else "选择地区")
        else:
            return {"label": (decision.get("option_labels", {}).get(option) or
                              view.get("reallocation", {}).get("sources", {}).get(option) or CHOICE_NAMES.get(option) or
                              (force_name(rules, option) if option in rules.force_controllers else None) or
                              ACTION_NAMES.get(option) or (f"选择方案 {int(option) + 1}" if option.isdecimal() else "选择此项"))}
        return {"label": f"{label} · {rules.regions[target].get('name', target)}", "region": target}
    label = ACTION_NAMES.get(kind, kind)
    if card := describe(command.get("card")):
        source = view["tokens"].get(command["card"])
        if kind == "PLAY_CARD" and source and rules.cards[source["definition_id"]]["type"] == "RESPONSE":
            label = "盖放响应"
        label += " · " + card["name"]
        if mode := command.get("play_as"):
            label += " · " + ACTION_NAMES.get(mode, mode)
        if force := command.get("force"):
            label += " · " + force_name(rules, force)
        return {"label": label, "text": card.get("text", "")}
    return {"label": label}


def battlefield(rules, client, view, human):
    board = board_snapshot(rules, view, 1, 0)
    routes = client.regions()
    for key, region in board["regions"].items():
        region["neighbors"] = routes[key]["neighbors"]
    seats = {}
    for seat in TURN_ORDER:
        groups = {kind: view["positions"].get(zone(seat, kind), {"count": 0}) for kind in
                  ("HAND", "RESPONSE", "STATUS", "DISABLED_STATUS", "DECK", "DISCARD")}
        seats[seat] = {"team": rules.team(seat), "counts": {kind: group.get("card_count", group["count"])
                                                           for kind, group in groups.items()},
                       "cards": {kind: [card_details(rules, view["tokens"][key]["definition_id"])
                                        for key in group.get("tokens", ()) if view["tokens"][key]["token_type"] == "CARD"]
                                 for kind, group in groups.items()
                                 if kind in ("STATUS", "DISABLED_STATUS") or
                                 (seat == human and kind in ("HAND", "RESPONSE", "DISCARD"))}}
    return {"regions": board["regions"], "forces": board["forces"], "seats": seats, "scores": view["scores"]}


class PlayerGame:
    def __init__(self, folder, library, rules, record, paths):
        self.folder, self.library, self.rules = folder, library, rules
        self.record, self.paths = record, paths
        self.human = record["human"]
        self.lock = Condition()
        self.actions = []
        self.pending = None
        self.stop_requested = False
        self.state = {"id": record["id"], "map": record["map"], "human": self.human, "name": record["name"],
                      "seats": {seat: {k: config[k] for k in ("version", "policy")} for seat, config in record["configs"].items()},
                      "state": "starting", "choices": [], "may_pass": False}
        self.worker = Thread(target=self.run, name="QMG player match", daemon=True)

    def status(self):
        with self.lock:
            return dict(self.state)

    def request_stop(self):
        with self.lock:
            if self.state["state"] not in TERMINAL and self.state["state"] != "archiving":
                self.stop_requested = True
                self.state.update(state="stopping", choices=[], may_pass=False)
                self.lock.notify_all()
            return dict(self.state)

    def choose(self, data):
        with self.lock:
            choice, version = data.get("choice"), data.get("version")
            if (type(version) is not int or self.state.get("version") != version or
                    self.state["state"] != "human" or self.pending is not None):
                raise PlayerError("局面已更新，请重新选择。", HTTPStatus.CONFLICT)
            if type(choice) is not int or not (0 <= choice < len(self.actions) or
                                               choice == -1 and self.state["may_pass"]):
                raise PlayerError("这一步没有该操作。")
            self.pending = choice
            self.state.update(state="resolving", choices=[], may_pass=False)
            self.lock.notify_all()
            return dict(self.state)

    def archive(self, processor, counts, log):
        info = self.record
        view = processor.state
        game = {"game_id": info["id"], "seed": info["seed"], "winner": view.winner, "scores": view.scores,
                "human": self.human, "seats": {self.human: "human", **{seat: cfg["policy"] for seat, cfg in info["configs"].items()}},
                "decisions_by_seat": dict(counts), "record_head": processor.record_head, "decision_log": log.name}
        note = f"{COUNTRIES[self.human]} · 玩家视角 · {info['map']}"
        write_json(self.folder / "summary.json", {"rules_digest": self.rules.fingerprint, "games": [game], "budget_ms": 3000,
                  "replay": {"name": info["name"], "ai_version": info["ai_label"], "ai_note": note}})
        batch = ReplayBatch(self.library, self.folder, self.rules, name=info["name"], version=info["ai_label"],
                            note=note, settings={"budget_ms": 3000})
        tape = replay_game(self.rules, game, log, perspective=self.human)
        key = batch.add(game, tape)
        return f"/replays/batches/{batch.key}/{key}.html"

    def run(self):
        policies = {}
        counts = Counter()
        try:
            info = self.record
            processor = CommandProcessor.create(self.rules, seed=info["seed"], game_id=info["id"])
            clients = {seat: CoreAPI(processor, (seat,), private_discards=True) for seat in TURN_ORDER}
            human = clients[self.human]
            path = self.folder / "commands.jsonl"
            cursor, recent = 0, []
            with path.open("x", encoding="utf-8", buffering=1) as log:
                while True:
                    view = human.observe(include_history=False, include_card_details=True)
                    turn = view["turn"]
                    active = TURN_ORDER[turn["country_index"]]
                    actor = (view["decision"] or {}).get("required_country", active)
                    board = battlefield(self.rules, human, view, self.human)
                    cursor, played = human.public_card_plays(cursor)
                    recent = [*played[::-1], *recent][:10]
                    # Core exposes human interruptions at other seats' decision boundaries.
                    actions = [] if view["winner"] else human.legal_commands()
                    descriptions = [command_description(self.rules, view, c) for c in actions]
                    with self.lock:
                        if self.stop_requested:
                            break
                        self.actions = actions
                        self.state.update(board=board, turn=turn, actor=actor, active=active, version=view["version"],
                                          step=sum(counts.values()), winner=view["winner"],
                                          state="archiving" if view["winner"] else "human" if actions else "ai",
                                          choices=descriptions, may_pass=bool(actions and actor != self.human),
                                          recent_plays=recent,
                                          reallocation_open=bool(view.get("reallocation") and not view["decision"]
                                                                 and not view["resolution_depth"]))
                        if view["winner"]:
                            break
                        if actions:
                            self.lock.wait_for(lambda: self.stop_requested or self.pending is not None)
                            if self.stop_requested:
                                break
                        choice, self.pending = self.pending, None
                        if choice == -1:
                            self.state["state"] = "ai"
                    if sum(counts.values()) >= 10000:
                        raise CoreError("这局超过 10000 步。")
                    if choice is not None and choice >= 0:
                        command, seat, policy = actions[choice], self.human, "human"
                        label = descriptions[choice]["label"]
                    else:
                        seat = actor
                        if seat == self.human:
                            raise CoreError("真人席位没有可提交的操作。")
                        cfg = info["configs"][seat]
                        policy = cfg["policy"]
                        signature = (cfg["version"], policy, digest(cfg["weights"]))
                        if signature not in policies:
                            policies[signature] = PolicyClient(self.paths[cfg["version"]], self.rules, cfg)
                        command, _ = policies[signature].decide(clients[seat], seat)
                        label = None
                    clients[seat].apply(command)
                    counts[seat] += 1
                    log_row(log, {"decision_index": sum(counts.values()), "country": seat, "command": command,
                                  "version": view["version"], "policy": policy, "label": label,
                                  "seed": info["seed"], "game_id": info["id"]})
            winner = processor.state.winner
            final = "completed" if winner else "stopped"
            if not counts:
                with self.lock:
                    self.state.update(state=final, choices=[], may_pass=False)
                return
            with self.lock:
                self.state.update(state="archiving", choices=[], may_pass=False, winner=winner,
                                  board=board, turn=turn, version=view["version"], step=sum(counts.values()),
                                  recent_plays=recent)
            link = self.archive(processor, counts, path)
            with self.lock:
                self.state.update(state=final, replay=link)
        except Exception as error:
            try:
                (self.folder / "error.txt").write_text(str(error), encoding="utf-8")
            except OSError:
                pass
            with self.lock:
                message = "对局已结束，录像归档失败。" if self.state["state"] == "archiving" else "对局中断，已保留本机日志。"
                self.state.update(state="failed", error=message, choices=[], may_pass=False)
        finally:
            for policy in policies.values():
                policy.close()


class PlayerGames:
    def __init__(self, panel, folder, library):
        self.panel, self.folder, self.library = panel, folder, library
        self.current = None

    def start(self, data):
        if self.current and self.current.worker.is_alive():
            raise PlayerError("已有玩家对局正在进行。", HTTPStatus.CONFLICT)
        human = data.get("human")
        if human not in TURN_ORDER:
            raise PlayerError("请选择一个真人席位。")
        seat_configs = data.get("seats")
        if not isinstance(seat_configs, dict) or set(seat_configs) != set(TURN_ORDER) - {human}:
            raise PlayerError("请为其余五个席位设置 AI。")
        seed = data.get("seed")
        if seed is None:
            seed = randbits(32)
        if type(seed) is not int or not 0 <= seed < 2 ** 32:
            raise PlayerError("种子须为 0 至 4294967295 的整数。")
        configs = {seat: self.panel.configuration(seat_configs[seat]) for seat in TURN_ORDER if seat != human}
        selected = self.panel.map_record(data.get("map", "standard"))
        rules = match_rules(self.panel.base_rules, selected["layout"])
        for seat, config in configs.items():
            if config["version"] == "current":
                try:
                    ScoreEvaluator(rules, config["weights"])
                except CoreError as error:
                    raise PlayerError(f"{COUNTRIES[seat]} · {error}") from error
        ai_groups = Counter((cfg["version"], cfg["policy"]) for cfg in configs.values())
        ai_label = " / ".join(f"{'当前版' if ver == 'current' else ver} {policy}×{count}"
                              for (ver, policy), count in ai_groups.items())
        timestamp = datetime.now(timezone.utc)
        key = "player-" + uuid4().hex[:12]
        name = f"玩家对局 · {timestamp.astimezone():%Y-%m-%d %H:%M:%S %z} · {ai_label} · {selected['name']}"
        record = {"id": key, "name": name, "human": human, "seed": seed, "ai_label": ai_label,
                  "configs": configs, "map": selected["name"], "rules_digest": rules.fingerprint}
        paths = self.panel.policy_paths(configs)
        directory = self.folder / key
        directory.mkdir(parents=True)
        write_json(directory / "run.json", record)
        write_json(directory / "map.json", selected)
        self.current = PlayerGame(directory, self.library, rules, record, paths)
        self.current.worker.start()
        return self.current.status()

    def act(self, key, action, data):
        if not self.current or self.current.record["id"] != key:
            raise PlayerError("找不到这局玩家对局。", HTTPStatus.NOT_FOUND)
        return self.current.choose(data) if action == "command" else self.current.request_stop()

    def close(self):
        if self.current and self.current.worker.is_alive():
            self.current.request_stop()
            self.current.worker.join(timeout=6)
