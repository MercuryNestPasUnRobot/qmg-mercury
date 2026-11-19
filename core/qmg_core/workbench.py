"""Local acceptance client for the original architecture fixture."""
from argparse import ArgumentParser
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from uuid import uuid4
import json

from .api import CoreAPI
from .definitions import RuleSet
from .engine import CommandProcessor
from .evaluation import OneStepPlanner, SimulationIncomplete
from .model import CoreError, TURN_ORDER, zone
from .persistence import OperationLogger, SaveRepository


SEAT = ("germany",)
DATA = Path(__file__).resolve().parent.parent / "data"
COUNTRY_NAMES = dict(zip(TURN_ORDER, ("德国", "英国", "日本", "苏联", "意大利", "美国")))
CARD_NAMES = {
    "g-build": "部署陆军", "g-chain": "计分与部署", "g-battle": "陆战演练",
    "g-recruit": "征募陆军", "g-transport": "重新部署", "g-draw": "补充手牌",
    "g-vp": "领取测试积分", "g-status": "可选连锁", "g-noop": "观察归档",
    "g-restore": "盖放回合末恢复",
    "g-return": "接续响应",
    "uk-guard": "自动阻止", "uk-secret": "计分示例", "uk-other": "抽牌示例",
}
PHASE_NAMES = {"Setup": "开局", "Play": "出牌", "Supply": "补给", "Victory": "计分",
               "Discard": "弃牌", "Draw": "抽牌"}
ZONE_NAMES = {"DECK": "牌库", "HAND": "手牌", "DISCARD": "弃牌", "STATUS": "状态",
              "RESPONSE": "响应", "RESERVE_ARMY": "陆军预备", "RESERVE_NAVY": "海军预备"}


class Workbench:
    def __init__(self, directory, rules, weights):
        directory = Path(directory)
        checkpoint = directory / "state.json"
        journal = directory / "operations.jsonl"
        if checkpoint.exists():
            if not journal.exists():
                saved = SaveRepository.recover(checkpoint, rules)
                if saved.state.version:
                    raise CoreError("Saved workbench has lost its journal")
            self.processor = SaveRepository.resume(checkpoint, rules, journal)
        elif journal.exists():
            raise CoreError("Existing journal has no checkpoint")
        else:
            self.processor = CommandProcessor.create(
                rules, game_id=str(uuid4()), logger=OperationLogger(journal), checkpoint_path=checkpoint
            )
        self.human = CoreAPI(self.processor, SEAT)
        self.planner = OneStepPlanner(rules, weights)
        self.ai_choices = []

    def act(self, fields):
        view = self.human.observe()
        if int(fields["version"]) != view["version"]:
            raise CoreError("页面已更新，请从当前局面重新选择。")
        if fields["action"] == "ai":
            actor = view["decision"]["required_country"] if view["decision"] else TURN_ORDER[view["turn"]["country_index"]]
            client = CoreAPI(self.processor, (actor,))
            available = client.legal_commands()
            if not available:
                raise CoreError("当前没有可执行的指令。")
            if view["decision"]:
                expected = next((c for c in self.ai_choices if c["actor"] == actor and c["expected_version"] == view["version"]), None)
                planned = next((c for c in available if expected and c["option"] == expected["option"]), None)
                self.ai_choices = [c for c in self.ai_choices if c["expected_version"] > view["version"]] if planned else []
                command = planned or self.planner.choose(self.processor, actor)
            else:
                ranked = self.planner.rank(self.processor, actor)
                best = next((candidate for candidate in ranked if candidate["complete"]), None)
                if best is None:
                    raise SimulationIncomplete("当前预算内没有完成模拟的行动。")
                command, self.ai_choices = best["command"], best["continuations"]
            client.submit(command)
        elif fields["action"] == "human":
            commands = self.human.legal_commands()
            index = int(fields["choice"])
            if not 0 <= index < len(commands):
                raise CoreError("当前视角没有此选项。")
            self.ai_choices = []
            self.human.submit(commands[index])
        else:
            raise CoreError("未知操作。")

    def page(self, error=None):
        view = self.human.observe()
        clock = view["turn"]
        seat = TURN_ORDER[clock["country_index"]]
        tokens = view["tokens"]
        commands = self.human.legal_commands()

        def token_name(token_id):
            token = tokens.get(token_id)
            if not token:
                return escape(token_id)
            definition = token["definition_id"]
            if token["token_type"] == "UNIT":
                owner = COUNTRY_NAMES[token["owner_country"]]
                return escape(f"{owner} · {'陆军' if self.processor.rules.units[definition]['kind'] == 'ARMY' else '海军'}")
            return escape(CARD_NAMES.get(definition, definition))

        def button(action, label, index=None):
            choice = "" if index is None else f'<input type="hidden" name="choice" value="{index}">'
            return (f'<form method="post" action="/action"><input type="hidden" name="version" value="{view["version"]}">'
                    f'<input type="hidden" name="action" value="{action}">{choice}<button>{label}</button></form>')

        by_target = {c["option"]: i for i, c in enumerate(commands) if c["type"] == "CHOOSE"}
        board = []
        regions = self.human.regions()
        for region, definition in regions.items():
            pieces = ", ".join(token_name(t) for t in view["positions"][region]["tokens"]) or "空地"
            neighbors = " · ".join(escape(n) for n in definition["neighbors"])
            vp = definition["base_vp"]
            if region in by_target:
                head = button("human", f"选择 {escape(region)}", by_target[region])
            else:
                head = f'<strong>{escape(region)}</strong>'
            css = f'{definition["type"].lower()}{" target" if region in by_target else ""}'
            board.append(f'<article class="{css}">{head}<span>{pieces}</span><small>相邻 {neighbors or "—"} · {vp} VP</small></article>')
        hand = view["positions"][zone(SEAT[0], "HAND")]["tokens"]
        plays = {c["card"]: i for i, c in enumerate(commands) if c["type"] == "PLAY_CARD"}
        hand_view = [button("human", token_name(t), plays[t]) if t in plays else f'<span class="card">{token_name(t)}</span>' for t in hand]
        options = []
        for i, command in enumerate(commands):
            if command["type"] == "CHOOSE" and command["option"] not in regions:
                label = {"use": "使用能力", "decline": "放弃能力"}.get(command["option"], token_name(command["option"]))
                options.append(button("human", label, i))
            elif command["type"] == "END_PHASE":
                options.append(button("human", "结束阶段", i))
        decision = view["decision"]
        waiting = f'等待 {COUNTRY_NAMES[decision["required_country"]]} 选择' if decision else "本阶段行动"
        if decision and "decision_type" in decision:
            kind = {"ABILITY": "使用或放弃能力", "COST": "选择支付牌", "TARGET": "选择合法目标",
                    "REDEPLOY_TARGET": "选择重新部署地区", "REDEPLOY_UNIT": "选择重新部署单位"}.get(decision["decision_type"], "")
            waiting += f" · {kind}" if kind else ""
        resolution = " → ".join(token_name(frame["source"]) for frame in view["resolution"])
        zones = []
        for country in TURN_ORDER:
            counts = [f'{name} {view["positions"][zone(country, key)]["count"]}' for key, name in ZONE_NAMES.items()]
            zones.append(f'<li><strong>{COUNTRY_NAMES[country]}</strong> {" · ".join(counts)}</li>')
        history = []
        for row in reversed(view["history"][-20:]):
            summary = " · ".join(f'{token_name(c["token_id"])} {escape(c["from"])} → {escape(c["to"])}' for c in row["changes"])
            history.append(f'<li><code>#{row["sequence_id"]} {escape(row["operation_type"])}</code> {summary}</li>')
        scores = view["scores"]
        winner = {"axis": "轴心获胜", "allies": "同盟获胜", "draw": "平局"}.get(view["winner"], "")
        ai = "" if winner else button("ai", "让 AI 执行一步")
        alert = "" if error is None else f'<p role="alert">{escape(error)}</p>'
        return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mercury Core 验收台</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#0f1920;color:#edf2f5;font:16px system-ui,sans-serif}}
main{{max-width:1180px;margin:auto;padding:24px}}header,section{{padding:18px;background:#172630;border:1px solid #304654;border-radius:12px}}
header{{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}}
h1,h2,p{{margin:0 0 10px}}h2{{font-size:18px}}p,small{{color:#b5cad5}}
.layout{{display:grid;grid-template-columns:minmax(0,2fr) minmax(260px,1fr);gap:16px;margin-top:16px;align-items:start}}
.stack{{display:grid;gap:16px}}.map{{display:grid;grid-template-columns:repeat(auto-fit,minmax(134px,1fr));gap:8px}}
article{{display:grid;gap:8px;padding:12px;background:#25352e;border:1px solid #3e5849;border-radius:8px;min-height:100px}}
article.sea{{background:#193647;border-color:#33586b}}article.target{{border:2px solid #efc46e}}
.actions{{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}}form{{margin:0}}
button,.card{{padding:8px 12px;border-radius:6px;border:1px solid #607d89;background:#273f4b;color:#fff;font:inherit}}
button{{cursor:pointer}}button:hover,button:focus-visible{{background:#38596a}}.card{{color:#b5cad5}}
ul{{margin:8px 0;padding-left:20px;line-height:1.65}}[role=alert]{{color:#ffd294}}
@media(max-width:740px){{.layout{{grid-template-columns:1fr}}main{{padding:12px}}}}
</style></head><body><main>
<header><div><h1>Mercury Core 验收台</h1><p>合成机制场景 · 德国席位 · 状态版本 {view["version"]}</p></div>
<div>第 {clock["round_id"]} 轮 · {COUNTRY_NAMES[seat]} · {PHASE_NAMES[clock["phase"]]}<br>
轴心 {scores["axis"]} VP · 同盟 {scores["allies"]} VP {winner}</div></header>
<div class="layout"><div class="stack"><section><h2>地区和合法目标</h2><div class="map">{"".join(board)}</div></section>
<section><h2>德国手牌</h2><div class="actions">{"".join(hand_view)}</div></section></div>
<div class="stack"><section><h2>{waiting}</h2>{alert}<p>{resolution}</p><div class="actions">{"".join(options)}{ai}</div></section>
<section><h2>区域</h2><ul>{"".join(zones)}</ul></section><section><h2>可见记录</h2><ul>{"".join(history)}</ul></section></div></div>
</main></body></html>""".encode("utf-8")


def make_server(workbench, port):
    class Handler(BaseHTTPRequestHandler):
        timeout = 10

        def _page(self, status=200, error=None):
            content = workbench.page(error)
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(content)

        def _local(self):
            expected = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != expected or self.headers.get("Origin") not in (None, f"http://{expected}"):
                self.send_error(403)
                return False
            return True

        def do_GET(self):
            if self._local():
                if self.path == "/":
                    self._page()
                else:
                    self.send_error(404)

        def do_POST(self):
            if not self._local():
                return
            if self.path != "/action":
                self.send_error(404)
                return
            try:
                if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                    raise ValueError("Invalid form type")
                length = int(self.headers["Content-Length"])
                if not 0 < length <= 1024:
                    raise ValueError("Invalid form size")
                values = parse_qs(self.rfile.read(length).decode("utf-8"), strict_parsing=True, max_num_fields=3)
                if any(len(value) != 1 for value in values.values()):
                    raise ValueError("Duplicate field")
                workbench.act({key: value[0] for key, value in values.items()})
            except CoreError as error:
                self._page(409, str(error))
                return
            except (ValueError, TypeError, KeyError):
                self.send_error(400)
                return
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

    return HTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5184)
    parser.add_argument("--save-dir", type=Path, default=Path("../.cache/core-workbench"))
    args = parser.parse_args()
    rules = RuleSet.load(DATA / "architecture_fixture.json")
    weights = json.loads((DATA / "evaluation_weights.json").read_text(encoding="utf-8"))
    with make_server(Workbench(args.save_dir, rules, weights), args.port) as server:
        print(f"Mercury Core: http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
