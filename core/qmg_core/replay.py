"""Reconstruct private AI match recordings and maintain a browser-ready local library."""
from argparse import ArgumentParser
from base64 import b64encode
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
import json
from pathlib import Path
from re import fullmatch
from tempfile import NamedTemporaryFile

from .api import CoreAPI
from .board_connections import PRINTED_REGIONS, corrected_edges, corrected_straits, edge_pairs
from .definitions import PER_UNIT_SITE, RuleSet
from .engine import CommandProcessor
from .model import CoreError, TURN_ORDER, digest, zone


ASSETS = Path(__file__).resolve().with_name("replay_web")
LIBRARY = Path(__file__).resolve().parents[2] / "local-content" / "replays"
COUNTRIES = dict(zip(TURN_ORDER, ("德国", "英国", "日本", "苏联", "意大利", "美国")))
TEAMS = {"axis": "轴心", "allies": "同盟", "draw": "平局"}
COUNT_ZONES = ("HAND", "DECK", "DISCARD")
CARD_ZONES = ("STATUS", "DISABLED_STATUS", "RESPONSE")
DATA_TAG = '<script type="application/json" id="replay-data">'


def write_text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        file.write(content)
        temp = Path(file.name)
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def write_json(path, value):
    write_text(path, json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class Tape:
    """Capture quiescent or blocked decision points and the Core operations between them."""
    def __init__(self, processor):
        self.client = CoreAPI(processor, TURN_ORDER)
        self.processor = processor
        self.frames = []
        self.tokens = {}
        self.cursor = 0
        self.capture()

    def capture(self, row=None):
        rules = self.processor.rules
        view = self.client.observe(include_history=False)
        for key, token in view["tokens"].items():
            if key in self.tokens:
                continue
            kind, definition_id = token["token_type"], token["definition_id"]
            source = token["source_token_id"]
            definition = rules.cards.get(definition_id, {})
            self.tokens[key] = {"kind": kind, "force": token["owner_country"], "source": source,
                                "name": definition.get("title", definition_id),
                                **({"unit": rules.units[definition_id]["kind"]} if kind == "UNIT" else {}),
                                **({"text": definition["text"]} if definition.get("text") else {})}
        changes = []
        for record in self.processor.records[self.cursor:]:
            result = record["outcome"]
            moves = []
            for change in result["changes"]:
                origin, destination = change["from"], change["to"]
                key = change["token_id"]
                if (origin in rules.regions or destination in rules.regions or
                        (row and row["command"]["type"] == "DISCARD_CARD" and
                         (origin or "").endswith(":HAND") and (destination or "").endswith(":DISCARD")) or
                        any((position or "").partition(":")[2] in ("STATUS", "DISABLED_STATUS", "RESPONSE")
                            for position in (origin, destination)) or
                        "RESOLUTION" in (origin, destination)):
                    moves.append([key, origin, destination])
            if moves or result["score_changes"] or not result["success"]:
                changes.append({"type": record["operation_type"], "actor": record["actor"],
                                "moves": moves, "scores": result["score_changes"], "ok": result["success"],
                                **({"site": record["operation"]["score_site"]}
                                   if (record["operation"].get("score_site") or "").startswith("hidden:") else {})})
        self.cursor = len(self.processor.records)
        seat_zones = {}
        for seat in TURN_ORDER:
            seat_zones[seat] = {
                **{kind: view["positions"].get(zone(seat, kind), {}).get("count", 0) for kind in COUNT_ZONES},
                **{kind: view["positions"].get(zone(seat, kind), {}).get("tokens", []) for kind in CARD_ZONES},
                "piles": {kind: view["positions"][zone(seat, kind)]["tokens"] for kind in ("HAND", "DISCARD")},
            }
        self.frames.append({
            "turn": view["turn"], "version": view["version"], "scores": view["scores"], "winner": view["winner"],
            "board": {region: ids for region in rules.regions if (ids := view["positions"][region]["tokens"])},
            "seats": seat_zones, "events": changes,
            "resolution": [frame["source"] for frame in view["resolution"]],
            "waiting": ({k: view["decision"][k] for k in ("required_country", "decision_type")}
                        if view["decision"] else None),
            "decision": row,
        })

    def data(self):
        rules = self.processor.rules
        forces = {force: {"name": COUNTRIES.get(force, rules.auxiliary_forces.get(force, {}).get("name", force)),
                          "seat": seat, "team": rules.team(force)}
                  for force, seat in rules.force_controllers.items()}
        sites, half_sites = {}, set()
        for site in rules.sites.values():
            if not site.get("dynamic"):
                sites[site["region"]] = sites.get(site["region"], 0) + site["vp"]
                if site.get("scoring") == PER_UNIT_SITE:
                    half_sites.add(site["region"])
        return {"frames": self.frames, "tokens": self.tokens, "forces": forces, "turn_order": TURN_ORDER,
                "regions": {key: {"name": region.get("name", key), "type": region["type"], "vp": sites.get(key, 0),
                                  **({"half_site": True} if key in half_sites else {})}
                            for key, region in rules.regions.items()},
                "edges": [[edge["a"], edge["b"]] for edge in rules.connections.values()],
                "straits": rules.settings.get("straits", [])}


def replay_game(rules, game, path):
    """Execute the recorded commands, accepting only the original state sequence and result."""
    processor = CommandProcessor.create(rules, seed=game["seed"], game_id=game["game_id"])
    tape = Tape(processor)
    clients = {seat: CoreAPI(processor, (seat,)) for seat in TURN_ORDER}
    expected = game["decisions_by_seat"]
    total = sum(expected.values())
    observed = Counter()
    with path.open(encoding="utf-8") as log:
        for index, line in enumerate(log, 1):
            row = json.loads(line)
            command = row["command"]
            before = tape.frames[-1]
            if (row["seed"] != game["seed"] or row["game_id"] != game["game_id"] or
                    row["decision_index"] != index or row["version"] != before["version"] or
                    row["country"] != command["actor"] or row["policy"] != game["seats"].get(row["country"]) or
                    index > total):
                raise CoreError(f"Recording metadata or state sequence mismatch at decision {index}")
            clients[row["country"]].submit(command, include_history=False)
            observed[row["country"]] += 1
            tape.capture(row)
    final = tape.frames[-1]
    if (observed != Counter(expected) or
            final["winner"] != game["winner"] or final["scores"] != game["scores"] or
            (game.get("record_head") and processor.record_head != game["record_head"])):
        raise CoreError("Recording does not reproduce the reported result")
    return tape


def page(title, body, payload=None):
    style = (ASSETS / "replay.css").read_text(encoding="utf-8")
    script = (ASSETS / "replay.js").read_text(encoding="utf-8")
    hash_source = lambda value: "'sha256-" + b64encode(sha256(value.encode("utf-8")).digest()).decode() + "'"
    data = ""
    if payload is not None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
        data = f'{DATA_TAG}{encoded}</script>'
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src {hash_source(script)}; style-src {hash_source(style)}; img-src file:">
<title>{escape(title)} · Mercury</title><style>{style}</style>
</head><body>{body}{data}<script>{script}</script></body></html>'''


def render_recording(path, payload):
    payload = recording_topology(path, payload)
    title = f'{payload["batch"]["name"]} · {payload["game"]["game_id"]}'
    body = (ASSETS / "player.html").read_text(encoding="utf-8")
    write_text(path, page(title, body, payload))


def recording_topology(path, payload):
    if set(payload["regions"]) != PRINTED_REGIONS:
        return payload
    result = {**payload}
    result.pop("board_reference", None)
    if "straits" not in result:
        fingerprint = payload["batch"].get("rules_digest", "")
        if fullmatch(r"[0-9a-f]{64}", fingerprint):
            archive = path.parents[2] / "rules" / (fingerprint + ".json")
            if archive.is_file():
                result["straits"] = read_json(archive)["settings"].get("straits", [])
    straits = result.get("straits", [])
    audited = corrected_edges(payload["edges"])
    channels = corrected_straits(straits)
    if set(audited) != edge_pairs(payload["edges"]) or channels != straits:
        result["board_reference"] = {"edges": audited, "straits": channels}
    return result


def refresh_recordings(root):
    """Re-render the viewer from already verified recordings without executing games."""
    refreshed = 0
    for manifest in sorted(root.glob("batches/*/manifest.json")):
        for key in read_json(manifest)["games"]:
            if not fullmatch(r"[0-9a-f]{16}", key):
                raise CoreError("Invalid recording key")
            target = manifest.parent / (key + ".html")
            _, found, data = target.read_text(encoding="utf-8").partition(DATA_TAG)
            if not found or "</script>" not in data:
                raise CoreError(f"Recording data missing in {target.name}")
            payload = json.loads(data.split("</script>", 1)[0])
            render_recording(target, payload)
            refreshed += 1
    refresh_library(root)
    return refreshed


class ReplayBatch:
    def __init__(self, root, source, rules, *, name, version, note, settings):
        self.root = root
        self.key = digest(str(source.resolve()))[:16]
        self.directory = root / "batches" / self.key
        self.manifest = self.directory / "manifest.json"
        saved = read_json(self.manifest) if self.manifest.exists() else {}
        if saved and saved["rules_digest"] != rules.fingerprint:
            raise CoreError("A recording batch cannot mix rulesets")
        summary = source / "summary.json"
        timestamp = datetime.fromtimestamp(summary.stat().st_mtime, timezone.utc) if summary.exists() else datetime.now(timezone.utc)
        self.info = {"name": name, "ai_version": version, "ai_note": note, "settings": settings,
                     "date": saved.get("date", timestamp.isoformat(timespec="seconds")),
                     "rules_digest": rules.fingerprint, "games": saved.get("games", {})}
        self.root.mkdir(parents=True, exist_ok=True)
        pinned = root / "rules" / (rules.fingerprint + ".json")
        if not pinned.exists():
            write_json(pinned, rules.data)

    def add(self, game, tape):
        key = digest(game["game_id"])[:16]
        self.info["games"][key] = {k: game[k] for k in ("game_id", "seed", "search_team", "winner", "scores")}
        payload = {**tape.data(), "batch": {k: v for k, v in self.info.items() if k != "games"},
                   "game": self.info["games"][key]}
        render_recording(self.directory / (key + ".html"), payload)
        write_json(self.manifest, self.info)
        refresh_library(self.root)


def refresh_library(root):
    sections = []
    for manifest in root.glob("batches/*/manifest.json"):
        info = read_json(manifest)
        sections.append((info["date"], manifest.parent.name, info))
    batch_items, panels = [], []
    for index, (date, key, info) in enumerate(sorted(sections, reverse=True)):
        selected = " selected" if index == 0 else ""
        name = escape(info["name"])
        batch_items.append(f'<option value="{key}"{selected}>{name} · {len(info["games"])} 局</option>')
        games = []
        for game_key, game in info["games"].items():
            scores = game["scores"]
            team = TEAMS[game["search_team"]]
            result = TEAMS[game["winner"]] + ("获胜" if game["winner"] != "draw" else "")
            games.append(f'''<a class="game-link" href="batches/{key}/{game_key}.html">
<span><strong>种子 {game["seed"]}</strong><small>B 执{team}</small></span>
<span><strong>{scores["axis"]} <i>:</i> {scores["allies"]}</strong><small>{result}</small></span>
<span class="launch">播放 <b aria-hidden="true">→</b></span></a>''')
        panels.append(f'''<section class="batch" id="batch-{key}"{' hidden' if index else ''}>
<header class="batch-heading"><div><h2>{name}</h2><p>{escape(info["ai_note"])}</p></div>
<div class="edition">{escape(info["ai_version"])}<small>{escape(date[:10])} · {len(games)} 局</small></div>
</header><div class="game-list">{"".join(games)}</div></section>''')
    choose = (f'<label class="batch-picker">测试批次<select id="batch-select">{"".join(batch_items)}</select></label>'
              if batch_items else '<p>完成一次 AI 对局后，录像会出现在这里。</p>')
    body = f'''<main class="library"><header class="masthead"><div class="brand">M</div>
<div><h1>Mercury 录像台</h1><p>选择一局，回看出兵、响应和 AI 决策。</p></div></header>
{choose}{"".join(panels)}</main>'''
    write_text(root / "打开录像.html", page("测试录像", body))


def import_batch(source, root, rules, *, name, version, note):
    report = read_json(source / "summary.json")
    if rules.fingerprint != report["rules_digest"]:
        raise CoreError("Use the original catalog for this batch")
    settings = {key: report[key] for key in ("budget_ms", "shortlist_width", "nodes_per_candidate", "weights_digest")}
    batch = ReplayBatch(root, source, rules, name=name, version=version, note=note, settings=settings)
    for game in report["games"]:
        log = source / game["decision_log"]
        if log.resolve().parent != source.resolve():
            raise CoreError("Decision log must be inside the batch")
        tape = replay_game(rules, game, log)
        batch.add(game, tape)
        print(f'重放已核对 {game["game_id"]} · {len(tape.frames) - 1} 步', flush=True)


def main():
    parser = ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--batch", type=Path)
    action.add_argument("--refresh", action="store_true", help="Update the appearance of saved recordings")
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--library", type=Path, default=LIBRARY)
    parser.add_argument("--name")
    parser.add_argument("--ai-version")
    parser.add_argument("--ai-note")
    args = parser.parse_args()
    if args.refresh:
        print(f'已更新 {refresh_recordings(args.library)} 局录像的界面')
        print(args.library / "打开录像.html")
        return
    report = read_json(args.batch / "summary.json")
    fingerprint = report["rules_digest"]
    if not fullmatch(r"[0-9a-f]{64}", fingerprint):
        parser.error("Invalid rules digest in the batch summary")
    catalog = args.catalog or args.library / "rules" / (fingerprint + ".json")
    info = report.get("replay", {})
    import_batch(args.batch, args.library, RuleSet.load(catalog), name=args.name or info.get("name", args.batch.name),
                 version=args.ai_version or info.get("ai_version", "存档 AI"),
                 note=args.ai_note or info.get("ai_note", "从已完成的测试日志复现。"))
    print(args.library / "打开录像.html")


if __name__ == "__main__":
    main()
