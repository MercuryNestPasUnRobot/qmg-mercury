"""Local AI test controls and scoring logs, separate from the replay library."""

from argparse import ArgumentParser
from copy import deepcopy
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from math import isfinite
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from threading import Lock
from time import monotonic, sleep
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from zipfile import ZipFile

from .ai_debug_worker import read_json, timestamp, write_json
from .score_units import LINKED_SCORES


CORE = Path(__file__).resolve().parents[1]
ROOT = CORE.parent
ASSETS = Path(__file__).with_name("ai_debug_web")
BOARD_ASSETS = Path(__file__).with_name("replay_web")
BOARD_ART = ROOT / "local-content" / "replays" / "board" / "map.png"
OUTPUT = ROOT / "local-content" / "ai-debug"
TEAMS = ("axis", "allies")
DISCARD_CATALOG = "7a65cda6bc6de0beac8b13e53934b247f5c2ed6449201ec0527bb32fb92604df"
VERSIONS = {
    "current": ("当前工作区", None, DISCARD_CATALOG),
    "v1.25": ("v1.25 · 持续规则与补给", "9aeaa3f3de6b50576415eee7513fecbcf3541b3f", DISCARD_CATALOG),
    "v1.24": ("v1.24 · 就绪能力时机", "872d7720e5dd3391a63ae5061baef690465051e3", DISCARD_CATALOG),
    "v1.23": ("v1.23 · 弃牌与过牌", "173dd0ebe51561fed8a4b5dd70a1fb3e581025e8", DISCARD_CATALOG),
    "v1.22": ("v1.22 · 行动机会与落点", "7f748bb278e3eb0bc292576f98cd317a001c9a07", DISCARD_CATALOG),
    "v1.21": ("v1.21 · 逐种结算调配", "90a084ed419528357d650f32ac80e38243c7d3b7", DISCARD_CATALOG),
    "v1.20": ("v1.20 · 征召与留存", "0fa645af31cd879ef6773334ef86f394f59c233d", DISCARD_CATALOG),
    "v1.19": ("v1.19 · 弃牌与过牌", "57843b3bcf97cb37e6e1b8bb5a3e4b0e2586d731", DISCARD_CATALOG),
    "v1.18": ("v1.18 · 叠加与隐分点", "1f72825e769f19d1948ae8b983dd8786321ca83c",
              "d8dbf328fce1b2888546f48e1179fc658c0510600c95e9a61e03734215a3ab39"),
}
GROUPS = {
    "scoring_units": "基础得分", "hand_retention": "手牌保留", "economic_forecast": "VP 与牌库",
    "route_forecast": "距离与路线", "frontier_scoring": "地图局势", "distance_weights": "距离权重",
    "supply_weights": "补给权重", "military_weights": "部队权重", "card_value_weights": "牌效估值",
    "effect_value_weights": "效果估值", "resource_penalties": "资源惩罚",
    "status_forecast": "状态牌预测",
}
LABELS = {
    "vp": "每 1 VP", "card": "每张牌", "action": "每次行动", "capture_rounds": "占点评估回合数",
    "supply_stability": "补给稳定基础分",
    "expansion_preference": "每个可达分点的扩张偏好", "basic_usage_cost": "基础牌已用比例成本",
    "status_held_factor": "手中状态牌折算比例", "time_discount": "时间折扣",
    "horizon": "展望回合数", "saturation": "收益饱和阈值", "expansion_weight": "扩张权重",
    "threat_weight": "威胁权重", "construction_search_width": "建设候选宽度",
    "max_simulation_nodes": "每个候选的结算节点上限", "unusable_card_weight": "暂不可用牌比例",
    "slot_cost": "手牌槽位成本", "basic_floor": "基本牌储备下限", "duplicate_penalty": "重复牌扣分",
    "basic_congestion": "基础牌拥挤扣分", "vp_pressure_bonus": "终局 VP 压力加权",
    "closeout_bonus": "收官加权", "lead_vp_per_round": "每轮领先 VP 参考值",
    "future_shortfall_value": "未来牌库短缺比例", "deck_top_look": "查看一张牌库顶",
    "nearest_sites": "考虑的最近分点数", "distance_tail_decay": "远距离衰减",
    "friendly_unit": "友方单位系数", "enemy_unit": "敌方单位系数", "near_enemy": "邻近敌军加权",
    "held_forecast": "占区预估", "friendly_target": "盟友占区价值", "battle_target": "战斗目标",
    "paper_fraction": "纸面地图折算", "paper_cap": "纸面地图上限", "build_friendly_neighbor": "友军建设系数",
    "supplied": "有补给", "unsupplied": "无补给", "no_reserve_expansion": "无预备队的扩张",
    "ARMY": "陆军", "NAVY": "海军", "AIR": "空军", "empty_deck": "牌库用尽",
    "unknown": "未知牌", "ability_ready": "可发动能力的兜底权重", "CHOOSE": "选择", "REDEPLOY": "重新部署",
    "DRAW": "摸牌", "GAIN_VP": "增加 VP", "TRANSFER": "移动对象", "BUILD_ARMY": "建设陆军",
    "BUILD_NAVY": "建设海军", "RECRUIT": "征召", "LAND_BATTLE": "陆战", "SEA_BATTLE": "海战",
    "default": "默认效果", "points": "计分", "build": "建设", "recruit": "征召", "choose-space": "选择地区",
    "battle": "战斗", "choose-unit": "选择单位", "air-action": "空军行动", "play-from-zone": "从指定区域出牌",
    "search-deck": "搜索牌库", "discard-hand": "弃手牌", "discard-status": "弃状态牌",
    "extra_trigger_opportunities": "额外预计触发次数", "projected_distance_offset": "空降与路径的距离修正",
    "condition_safe": "安全时条件维持比例", "condition_threatened": "受威胁时条件维持比例",
    "condition_horizon": "条件维持折扣回合数", "arrival_accessible": "可直达地区的进驻比例",
    "arrival_nearby": "邻近地区的进驻比例", "request_discard_card_value": "要求每弃一张的估值",
    "filter_card_value": "每张滤牌估值", "rule_option_multiplier": "规则增加落点的折算",
    "rule_option_decay": "次优落点衰减", "rule_option_limit": "规则落点上限",
    "exchange_discard_base": "调配从弃牌区取牌比例", "exchange_discard_deck_bonus": "调配从弃牌区取牌的牌库修正",
}
INTEGER = {"scoring_units.capture_rounds", "route_forecast.nearest_sites", "horizon", "construction_search_width",
           "max_simulation_nodes", "status_forecast.extra_trigger_opportunities", "status_forecast.projected_distance_offset",
           "status_forecast.condition_horizon", "status_forecast.unknown_hand_scale", "status_forecast.rule_option_limit"}
LEGACY_CARD_VALUES = {"REDEPLOY", "DRAW", "GAIN_VP", "BUILD_ARMY", "BUILD_NAVY", "RECRUIT", "LAND_BATTLE", "SEA_BATTLE"}
LEGACY_MAP_GROUPS = {"distance_weights", "frontier_scoring"}
RETIRED_GROUPS = {"supply_weights", "military_weights", "resource_penalties", "hand_retention"}
RETIRED_FIELDS = {
    "expansion_weight", "threat_weight", "saturation", "horizon",
    *(f"route_forecast.{key}" for key in ("distance_tail_decay", "friendly_unit", "enemy_unit")),
    *(f"economic_forecast.{key}" for key in ("vp_pressure_bonus", "closeout_bonus", "deck_top_look")),
    *(f"status_forecast.{key}" for key in (
        "condition_safe", "condition_threatened", "condition_horizon", "arrival_accessible", "arrival_nearby",
        "known_action_likelihood", "keyword_draw_likelihood", "action_draw_likelihood", "unknown_hand_likelihood_cap",
        "unknown_hand_scale", "own_activity_likelihood", "enemy_activity_likelihood", "discard_activity_likelihood",
        "rule_option_multiplier", "rule_option_decay", "rule_option_limit", "economic_target_share", "filter_card_value")),
    *(f"card_value_weights.{key}" for key in LEGACY_CARD_VALUES | {"ability_ready", "CHOOSE", "TRANSFER"}),
    *(f"effect_value_weights.{key}" for key in (
        "points", "build", "recruit", "choose-space", "battle", "choose-unit", "air-action", "discard-status")),
}
LINK_LABELS = {
    "full_site_round": "全分点每轮收益／响应每轮等待成本",
    "distance_step": "地图每格距离的估值",
    "unit_removal": "移除单位的基础收益",
    "unknown_protection": "防移除估值（地点未知）",
    "play_opportunity": "出牌和空军的行动成本",
    "expansion": "每个可达分点的扩张偏好",
    "basic_discard": "基础牌已用比例的弃置成本",
    "extra_draw": "每张额外过牌的起值",
    "card_return": "每张牌的回收估值",
    "future_shortfall": "每 1 VP 未来缺牌损失的估值",
    "requested_discard": "每张被要求弃牌的预估收益",
    "forced_discard": "无可出牌时强制弃牌的预估成本",
    "exchange_saved_payment": "调配少弃一张的预估收益",
    "alternative_status_play": "状态牌替代常规出牌的估值",
    "play_from_zone_rule": "从指定区域出牌（纸面兜底）",
    "play_from_zone_hand": "从指定区域出牌（手牌兜底）",
    "search_deck_rule": "搜索牌库（纸面兜底）",
    "search_deck_hand": "搜索牌库（手牌兜底）",
}
TERMINAL = {"completed", "stopped", "failed", "interrupted"}
RUN_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{8}")


class RequestError(ValueError):
    def __init__(self, message, status=HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


def numeric_fields(weights, *, inactive=False):
    fields = []
    modern = "scoring_overrides" in weights

    def visit(value, path):
        if isinstance(value, (dict, list)):
            for key, child in value.items() if isinstance(value, dict) else enumerate(value):
                visit(child, (*path, key))
        elif type(value) in (int, float):
            key = ".".join(map(str, path))
            last = path[-1]
            legacy_map = weights.get("action_distance_scoring") is True and path[0] in LEGACY_MAP_GROUPS
            retired = legacy_map or (modern and (path[0] in RETIRED_GROUPS or key in RETIRED_FIELDS))
            if retired != inactive:
                return
            group = GROUPS.get(path[0], "搜索设置" if "width" in key or key == "max_simulation_nodes" else "通用估值")
            if weights.get("action_opportunity_scoring") and path[0] == "card_value_weights" and last in LEGACY_CARD_VALUES:
                group = "旧版牌效估值（当前主线已替代）"
            if weights.get("marginal_opportunity_scoring") and (path[0] == "hand_retention" or key == "economic_forecast.deck_top_look"):
                group = "旧版估值（当前主线已替代）"
            if modern and path[0] in ("card_value_weights", "effect_value_weights"):
                group = "独立牌效兜底"
            label = f"距离 {last + 1}" if type(last) is int else LABELS.get(last, last)
            if weights.get("known_ability_scoring") and key == "status_forecast.extra_trigger_opportunities":
                label = "基础牌相关能力的最低预计次数"
            fields.append({"key": key, "path": path, "value": value, "integer": key in INTEGER,
                "label": label, "group": group})

    for group, value in weights.items():
        if group != "scoring_overrides":
            visit(value, (group,))
    return fields


def linked_fields(weights):
    if "scoring_overrides" not in weights:
        return []
    return [{"key": key, "label": LINK_LABELS[key], "base": f"scoring_units.{base}", "unit_label": LABELS[base],
             "multiplier": multiplier, "coefficient": coefficient}
            for key, (base, multiplier, coefficient) in LINKED_SCORES.items()]


def edited_weights(weights, updates, *, overrides=None):
    if not isinstance(updates, dict):
        raise RequestError("评分参数必须是一组数值。")
    permitted = {field["key"]: field for field in numeric_fields(weights)}
    inactive = {field["key"] for field in numeric_fields(weights, inactive=True)}
    edited = deepcopy(weights)
    for key, value in updates.items():
        if key in inactive:
            continue  # Older presets included parameters that this scoring model has replaced.
        field = permitted.get(key)
        if field is None:
            raise RequestError("此版本没有该评分参数。")
        if (type(value) not in (int, float) or not 0 <= value <= 1e6 or not isfinite(value) or
                (field["integer"] and (type(value) is not int))):
            raise RequestError(f'{field["label"]}的数值无效。')
        node = edited
        *parents, leaf = field["path"]
        for part in parents:
            node = node[part]
        node[leaf] = value
    if overrides is not None:
        if not isinstance(overrides, dict):
            raise RequestError("覆盖值必须是一组数值。")
        permitted_overrides = {field["key"]: field["label"] for field in linked_fields(weights)}
        for key, value in overrides.items():
            if key not in permitted_overrides:
                raise RequestError("此版本没有该评分覆盖项。")
            if type(value) not in (int, float) or not isfinite(value) or not 0 <= value <= 1e6:
                raise RequestError(f"{permitted_overrides[key]}的数值无效。")
        if "scoring_overrides" in edited:
            edited["scoring_overrides"] = dict(overrides)
    return edited


class VersionRegistry:
    def __init__(self, workspace, cache):
        self.workspace = workspace
        self.cache = cache
        self.options = {}
        for key, (label, commit, identity) in VERSIONS.items():
            core = self.workspace / "core"
            try:
                source = json.loads(self.git("show", f"{commit}:core/data/ai_test_weights.json")) if commit else read_json(
                    core / "data" / "ai_test_weights.json")
            except (OSError, subprocess.SubprocessError, ValueError):
                continue
            source["max_simulation_nodes"] = 16  # Same default as the six-seat batch runner.
            try:
                revision = commit or self.git("rev-parse", "HEAD").decode().strip()
            except (OSError, subprocess.SubprocessError):
                revision = None
            catalog = self.workspace / "local-content" / "replays" / "rules" / (identity + ".json")
            if not catalog.is_file():
                catalog = self.workspace / "local-content" / "baselines" / (
                    "board-reviewed-regional-sites/content.sqlite3" if key == "v1.18"
                    else f"board-reviewed-discard-phase/{identity}.sqlite3")
            self.options[key] = {"id": key, "label": label, "commit": revision, "pinned": bool(commit),
                "defaults": source, "catalog": str(catalog) if catalog.is_file() else None,
                "rules_digest": identity if catalog.is_file() else None}

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.workspace), *args], check=True, capture_output=True, timeout=15).stdout

    def core(self, version):
        option = self.options[version]
        if not option["pinned"]:
            return self.workspace / "core"
        destination = self.cache / option["commit"]
        ready = destination / ".ready"
        if ready.is_file():
            return destination / "core"
        blob = self.git("archive", "--format=zip", option["commit"], "core/qmg_core", "core/data/architecture_fixture.json")
        # Install individual files atomically. Moving a whole populated directory
        # is unreliable on Windows checkout and antivirus configurations.
        with ZipFile(BytesIO(blob)) as archive:
            for name in archive.namelist():
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or name.endswith("/"):
                    continue
                if (not name.startswith("core/qmg_core/") or path.suffix != ".py") and name != "core/data/architecture_fixture.json":
                    continue
                target = destination.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_name(f".{target.name}.{uuid4().hex[:8]}.tmp")
                temp.write_bytes(archive.read(name))
                temp.replace(target)
        ready.touch()
        return destination / "core"

    def public(self):
        return [{k: row[k] for k in ("id", "label", "commit", "pinned")} | {
            "catalog": "本地六席卡库" if row["catalog"] else "合成测试场景",
            "inactive_fields": [field["key"] for field in numeric_fields(row["defaults"], inactive=True)],
            "fields": numeric_fields(row["defaults"]), "linked_fields": linked_fields(row["defaults"])}
            for row in self.options.values()]


def complete_lines(path):
    if path.is_file():
        with path.open(encoding="utf-8") as stream:
            return [json.loads(line) for line in stream if line.endswith("\n")]
    return []


def log_window(path, before=None, limit=36):
    if not path.is_file():
        return {"rows": [], "before": None}
    with path.open("rb") as stream:
        end = stream.seek(0, 2)
        if before is not None:
            end = min(end, before)
        start = end
        chunks = []
        lines = 0
        while start and lines <= limit + 1:
            boundary = max(0, start - 65536)
            stream.seek(boundary)
            chunk = stream.read(start - boundary)
            chunks.append(chunk)
            lines += chunk.count(b"\n")
            start = boundary
        data = b"".join(reversed(chunks))
    if start:
        skip = data.find(b"\n") + 1
        start += skip
        data = data[skip:]
    parts = data.split(b"\n")[:-1]
    trim = max(0, len(parts) - limit)
    next_before = start + sum(len(line) + 1 for line in parts[:trim])
    return {"rows": [json.loads(line) for line in parts[trim:][::-1]],
            "before": next_before if next_before else None}


class DebugPanel:
    def __init__(self, output=OUTPUT, versions=None):
        self.output = Path(output)
        self.versions = versions or VersionRegistry(ROOT, ROOT / ".cache" / "ai-debug-versions")
        self.active = None

    def directory(self, run_id):
        directory = self.output / run_id
        if not RUN_ID.fullmatch(run_id) or not (directory / "run.json").is_file():
            raise RequestError("找不到这次测试。", HTTPStatus.NOT_FOUND)
        return directory

    def status(self, directory):
        state = read_json(directory / "status.json")
        live = self.active and self.active[0] == directory
        if state["state"] not in TERMINAL:
            if not live or self.active[1].poll() is not None:
                state.update(state="interrupted", error="调试进程已关闭。已写入的日志可继续查看。")
            else:
                control = read_json(directory / "control.json")
                if control["revision"] > state["control_revision"]:
                    state["requested"] = control["action"]
        return state

    def list_runs(self):
        folders = sorted(self.output.glob("*"), reverse=True)
        rows = []
        active = None
        for folder in folders:
            if RUN_ID.fullmatch(folder.name) and (folder / "run.json").is_file():
                meta = read_json(folder / "run.json")
                status = self.status(folder)
                rows.append({**meta, "state": status["state"], "completed": status["completed"]})
                if self.active and self.active[0] == folder and status["state"] not in TERMINAL:
                    active = folder.name
        return {"runs": rows, "active": active}

    def detail(self, run_id):
        directory = self.directory(run_id)
        return {"run": read_json(directory / "run.json"), "status": self.status(directory),
                "games": complete_lines(directory / "games.jsonl")}

    def board(self, run_id, game):
        try:
            return read_json(self.directory(run_id) / f"board-{game}.json")
        except FileNotFoundError:
            return None

    def configurations(self, supplied, previous=None):
        if not isinstance(supplied, dict) or set(supplied) != set(TEAMS):
            raise RequestError("请提供轴心和同盟各自的 AI 配置。")
        configs = {}
        for team in TEAMS:
            config = supplied[team]
            if not isinstance(config, dict):
                raise RequestError("阵营配置格式有误。")
            version, policy = config.get("version"), config.get("policy")
            if not isinstance(version, str) or version not in self.versions.options or policy not in ("A", "B"):
                raise RequestError("请选择有效的 AI 版本和 A／B 策略。")
            option = self.versions.options[version]
            old = previous[team] if previous else None
            base = old["weights"] if old and old["version"] == version else option["defaults"]
            configs[team] = {"version": version, "label": option["label"], "commit": option["commit"],
                             "policy": policy, "weights": edited_weights(base, config.get("values", {}),
                                                                          overrides=config.get("overrides"))}
        return configs

    def policy_paths(self, configs):
        return {version: str(self.versions.core(version))
                for version in dict.fromkeys(config["version"] for config in configs.values())}

    def start(self, data):
        if self.active and self.active[1].poll() is None:
            raise RequestError("已有测试正在运行。请先结束它。", HTTPStatus.CONFLICT)
        seed, mode = data.get("seed"), data.get("mode")
        if type(seed) is not int or not 0 <= seed < 2 ** 32 or mode not in ("single", "paired", "continuous"):
            raise RequestError("请选择有效的种子和运行方式。")
        configs = self.configurations(data.get("configs"))
        paths = self.policy_paths(configs)
        option = self.versions.options["current"]
        core = self.versions.core("current")
        catalog = option["catalog"] or str(core / "data" / "architecture_fixture.json")
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:8]
        directory = self.output / run_id
        directory.mkdir(parents=True)
        write_json(directory / "status.json", {"state": "starting", "control_revision": 0, "weights_revision": 0,
            "step": 0, "completed": 0, "match": None, "configs": configs})
        write_json(directory / "control.json", {"action": "run", "revision": 1, "configs": configs, "paths": paths})
        write_json(directory / "run.json", {"id": run_id, "seed": seed, "mode": mode, "created_at": timestamp(),
            "version_label": "轴心 " + configs["axis"]["version"] + " " + configs["axis"]["policy"] +
                " / 同盟 " + configs["allies"]["version"] + " " + configs["allies"]["policy"],
            "referee_commit": option["commit"], "catalog": "本地六席卡库" if option["catalog"] else "合成测试场景",
            "rules_digest": option["rules_digest"]})
        try:
            with (directory / "worker.log").open("ab") as output:
                worker = subprocess.Popen([sys.executable, "-B", str(Path(__file__).with_name("ai_debug_worker.py")),
                    str(directory), str(core), catalog], stdin=subprocess.DEVNULL, stdout=output, stderr=output, cwd=core)
        except OSError:
            status = read_json(directory / "status.json")
            write_json(directory / "status.json", {**status, "state": "failed", "error": "无法启动本地调试进程。"})
            raise
        self.active = (directory, worker)
        return self.detail(run_id)

    def control(self, run_id, action, data):
        directory = self.directory(run_id)
        if not self.active or self.active[0] != directory or self.active[1].poll() is not None:
            raise RequestError("这次测试已经结束。", HTTPStatus.CONFLICT)
        status = self.status(directory)
        if status["state"] in TERMINAL:
            raise RequestError("这次测试已经结束。", HTTPStatus.CONFLICT)
        request = read_json(directory / "control.json")
        if request["action"] == "stop":
            if action == "stop":
                return self.detail(run_id)
            raise RequestError("请等当前测试停止。", HTTPStatus.CONFLICT)
        if request["action"] == "pause" and action == "pause":
            return self.detail(run_id)
        if action in ("resume", "weights", "step"):
            if status["state"] != "paused" or "requested" in status:
                raise RequestError("请等测试暂停后再修改参数或单步执行。", HTTPStatus.CONFLICT)
            configs = status["configs"]
            if "configs" in data:
                configs = self.configurations(data["configs"], configs)
            request.update(configs=configs, paths=self.policy_paths(configs))
        request.update(action={"pause": "pause", "resume": "run", "weights": "pause", "stop": "stop", "step": "step"}[action],
                       revision=request["revision"] + 1)
        write_json(directory / "control.json", request)
        return self.detail(run_id)

    def close(self):
        if self.active and self.active[1].poll() is None:
            directory, process = self.active
            request = read_json(directory / "control.json")
            write_json(directory / "control.json", {**request, "action": "stop", "revision": request["revision"] + 1})
            end = monotonic() + 7
            while process.poll() is None and monotonic() < end:
                sleep(0.1)
            if process.poll() is None:
                process.terminate()


def handler_for(panel):
    writes = Lock()

    class Handler(BaseHTTPRequestHandler):
        timeout = 8

        def log_message(self, _format, *_args):
            return

        def send_content(self, data, mime, status=HTTPStatus.OK, filename=None):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(data)

        def send_json(self, value, status=HTTPStatus.OK):
            self.send_content(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                              "application/json; charset=utf-8", status)

        def dispatch(self, write=False):
            try:
                host = self.headers.get("Host")
                port = self.server.server_port
                if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                    raise RequestError("请从本机打开调试台。", HTTPStatus.FORBIDDEN)
                origin = self.headers.get("Origin")
                if origin and origin != f"http://{host}":
                    raise RequestError("请从调试台页面操作。", HTTPStatus.FORBIDDEN)
                url = urlsplit(self.path)
                parts = url.path.strip("/").split("/")
                if write:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 32768 or self.headers.get_content_type() != "application/json":
                        raise RequestError("请求格式有误。")
                    data = json.loads(self.rfile.read(size))
                    if not isinstance(data, dict):
                        raise RequestError("请求格式有误。")
                    if parts == ["api", "runs"]:
                        with writes:
                            started = panel.start(data)
                        return self.send_json(started, HTTPStatus.CREATED)
                    if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] in ("pause", "resume", "weights", "step", "stop"):
                        with writes:
                            detail = panel.control(parts[2], parts[3], data)
                        return self.send_json(detail)
                elif url.path in ("/", "/app.js", "/app.css", "/board.js", "/board.css"):
                    name = "index.html" if url.path == "/" else url.path[1:]
                    mime = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}[Path(name).suffix]
                    folder = BOARD_ASSETS if name.startswith("board.") else ASSETS
                    return self.send_content((folder / name).read_bytes(), mime + "; charset=utf-8")
                elif url.path == "/board-map.png" and BOARD_ART.is_file():
                    return self.send_content(BOARD_ART.read_bytes(), "image/png")
                elif parts == ["api", "versions"]:
                    return self.send_json(panel.versions.public())
                elif parts == ["api", "runs"]:
                    return self.send_json(panel.list_runs())
                elif len(parts) == 3 and parts[:2] == ["api", "runs"]:
                    return self.send_json(panel.detail(parts[2]))
                elif len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] in ("log", "download", "parameters", "board"):
                    directory = panel.directory(parts[2])
                    if parts[3] == "parameters":
                        name = "parameters.jsonl"
                    else:
                        query = parse_qs(url.query)
                        game = int(query.get("game", ["1"])[0])
                        if game < 1:
                            raise RequestError("请选择有效的对局。")
                        name = f"game-{game}.jsonl"
                    if parts[3] == "board":
                        return self.send_json(panel.board(parts[2], game))
                    file = directory / name
                    if parts[3] == "log":
                        before = int(query["before"][0]) if "before" in query else None
                        if before is not None and before < 1:
                            raise RequestError("日志页码无效。")
                        return self.send_json(log_window(file, before))
                    if file.is_file():
                        return self.send_content(file.read_bytes(), "application/x-ndjson; charset=utf-8", filename=name)
                raise RequestError("找不到这个页面或日志。", HTTPStatus.NOT_FOUND)
            except RequestError as error:
                self.send_json({"error": str(error)}, error.status)
            except (ValueError, OverflowError):
                self.send_json({"error": "请求中的数值或格式有误。"}, HTTPStatus.BAD_REQUEST)
            except (ConnectionError, TimeoutError):
                return
            except (OSError, subprocess.SubprocessError):
                self.send_json({"error": "无法读取本机数据或启动所选版本。"}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch(write=True)

    return Handler


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5186)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    panel = DebugPanel(args.output.resolve())
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(panel)) as server:
        print(f"AI 调试台 http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            panel.close()


if __name__ == "__main__":
    main()
