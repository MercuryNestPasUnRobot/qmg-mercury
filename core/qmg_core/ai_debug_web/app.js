"use strict";

const $ = id => document.getElementById(id);
const countries = {germany: "德国", "united-kingdom": "英国", japan: "日本", "soviet-union": "苏联",
  italy: "意大利", "united-states": "美国"};
const phases = {Setup: "开局", Play: "出牌", Air: "空军", Supply: "补给", Victory: "计分", Discard: "弃牌", Draw: "摸牌"};
const teams = {axis: "轴心", allies: "同盟", draw: "平局"};
const sides = ["axis", "allies"];
const opposite = {axis: "allies", allies: "axis"};
const configFormat = "qmg-ai-config/v1";
const states = {idle: "待开始", starting: "准备中", running: "运行中", pause: "暂停中", paused: "已暂停",
  step: "单步执行中", stepping: "单步执行中",
  stop: "停止中", run: "继续中", completed: "已完成", stopped: "已停止", failed: "出错", interrupted: "已中断"};
const modes = {single: "单局", paired: "换边两局", continuous: "连续换边"};
const actions = {END_PHASE: "结束阶段", PLAY_CARD: "出牌", DISCARD_CARD: "弃牌", FORFEIT_PLAY: "无牌可弃",
  CHOOSE: "选择", USE_STATUS: "发动状态牌", USE_AIR: "空军行动", REALLOCATE: "调配资源", ACTIVATE_CARD: "主动响应"};
const basic = {"build-army": "建设陆军", "build-navy": "建设海军", "land-battle": "发起陆战", "sea-battle": "发起海战"};
const choices = {use: "发动", decline: "放弃", pass: "跳过", confirm: "确认", cancel: "取消", done: "完成"};
const units = {ARMY: "陆军", NAVY: "海军", AIR: "空军"};
const zones = {HAND: "手牌", STATUS: "状态区", DISABLED_STATUS: "停用状态", RESPONSE: "响应区", DECK: "牌库",
  DISCARD: "弃牌堆", RESOLUTION: "结算区", RESERVE_ARMY: "陆军预备区", RESERVE_NAVY: "海军预备区",
  RESERVE_AIR: "空军预备区", OBJECT_POOL: "对象池", SET_ASIDE: "待命区"};
const events = {...actions, TRANSFER: "移动对象", DRAW: "摸牌", DISCARD: "弃牌", SET_RESPONSE: "盖放响应",
  DECISION_REQUESTED: "等待选择", DECISION_RESOLVED: "确认选择",
  ABILITY_TRIGGERED: "触发响应", BEGIN_PHASE: "进入阶段", TURN_END: "回合结束", GAIN_VP: "获得 VP",
  SCORE_COUNTRY: "据点计分", ELIMINATE: "移除单位", OBJECT_SETTLED: "对象结算完成", ACTIVATE_ABILITY: "发动能力",
  CARD_EFFECT: "牌效果", BATTLE_DECLARED: "宣告战斗", COUNTRY_LOSS: "国家损失", RECRUIT: "征召",
  LAND_BATTLE: "陆战", SEA_BATTLE: "海战", BUILD_ARMY: "建设陆军", BUILD_NAVY: "建设海军"};
const reasons = {"hand-retention": "手牌保留", "deadline": "达到时限", "matched-exchange": "沿用调配方案",
  "matched-exchange-play": "沿用调配后的出牌", "matched-continuation": "沿用续步", "rules-only": "纸面评分",
  "response-timing": "响应时机", "only-legal-command": "唯一合法指令", "finished": "结算完成",
  "pending-choice": "待决选择", "no-comparable-commands": "没有可比较的方案"};
const errors = {"Basic reserve must exceed the hand slot cost": "基础牌储备下限须高于手牌槽位成本。",
  "Scoring units must be finite and positive": "VP、牌和行动的基础值须大于零。",
  "Invalid route influence weights": "请检查路线系数。远距离衰减和友方系数须在 0 与 1 之间，敌方系数须大于 1。",
  "Invalid planning horizon or saturation": "展望回合数须为 1 至 20，收益饱和阈值须大于零。"};

const app = {
  versions: new Map(), live: null, viewed: null, runId: "", game: 0, followGame: true,
  cursor: null, next: null, drafts: null, editTeam: "axis", fields: new Map(),
  pinnedRevision: "", pageKey: "", busy: false, refreshing: false,
  board: null, boardKey: "", region: "", artReady: false, awaitingStep: null,
};

const number = n => typeof n === "number" ? n.toLocaleString("zh-CN", {maximumFractionDigits: 2}) : "—";
const date = value => value ? new Date(value).toLocaleString("zh-CN", {hour12: false}) : "";
const node = (tag, text, className) => {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
};

async function api(path, payload) {
  const response = await fetch(path, payload === undefined ? {} : {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload),
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || "本机调试服务没有响应。");
  return value;
}

function message(text, isError = false) {
  $("message").hidden = !text;
  $("message").dataset.error = String(isError);
  const prefix = /^(轴心|同盟) · /.exec(text || "")?.[0] || "";
  $("message").textContent = prefix + (errors[(text || "").slice(prefix.length)] || (text || "").slice(prefix.length));
}

const slot = team => app.live?.status.match?.swapped ? opposite[team] : team;
const draftFor = team => app.drafts[slot(team)];
const caption = draft => draft ? `${draft.version === "current" ? "当前工作区" : draft.version} · ${draft.policy}` : "";

function preset(version, policy, weights) {
  const fields = app.versions.get(version).fields;
  return {version, policy, values: Object.fromEntries(fields.map(field => [field.key,
    weights ? field.path.reduce((part, key) => part[key], weights) : field.value]))};
}

function renderTeamLabels() {
  for (const team of sides) {
    $("edit-" + team).setAttribute("aria-pressed", String(app.editTeam === team));
    $(team + "-profile").textContent = caption(draftFor(team));
  }
}

function renderEditor() {
  if (!app.drafts) return;
  const draft = draftFor(app.editTeam);
  const profile = app.versions.get(draft.version);
  renderTeamLabels();
  $("version").value = draft.version;
  $("policy").value = draft.policy;
  if (app.fields.size && [...app.fields.keys()].join() === profile.fields.map(f => f.key).join()) {
    for (const [key, input] of app.fields) input.value = Number.isFinite(draft.values[key]) ? draft.values[key] : "";
    return;
  }
  const groups = new Map();
  for (const field of profile.fields) {
    if (!groups.has(field.group)) groups.set(field.group, []);
    groups.get(field.group).push(field);
  }
  app.fields.clear();
  const elements = [];
  const advanced = node("details", undefined, "advanced");
  advanced.append(node("summary", "更多评分参数"));
  for (const [title, fields] of groups) {
    const detail = node("details", undefined, "group");
    detail.open = title === "基础得分";
    const controls = node("div", undefined, "two");
    for (const field of fields) {
      const label = node("label", field.label, "field number-label");
      const input = node("input");
      input.type = "number";
      input.step = field.integer ? "1" : "any";
      input.min = "0";
      input.max = "1000000";
      input.required = true;
      input.value = Number.isFinite(draft.values[field.key]) ? draft.values[field.key] : "";
      input.title = field.key;
      input.addEventListener("input", () => { draftFor(app.editTeam).values[field.key] = input.valueAsNumber; });
      app.fields.set(field.key, input);
      label.append(input);
      controls.append(label);
    }
    detail.append(node("summary", title), controls);
    if (detail.open) elements.push(detail);
    else advanced.append(detail);
  }
  if (advanced.childElementCount > 1) elements.push(advanced);
  $("fields").replaceChildren(...elements);
}

function renderControls() {
  const status = app.live?.status;
  const locked = Boolean(status);
  const paused = status?.state === "paused" && !status?.requested;
  const pending = Boolean(status?.requested) || ["starting", "stepping"].includes(status?.state);
  $("primary").textContent = locked ? (paused ? "继续测试" : "暂停测试") : "开始测试";
  $("primary").disabled = app.busy || pending || !app.drafts;
  $("stop").hidden = !locked;
  $("stop").disabled = app.busy || status?.requested === "stop";
  $("step").hidden = !paused && ![status?.state, status?.requested].some(value => ["step", "stepping"].includes(value));
  $("step").disabled = app.busy || !paused;
  for (const id of ["seed", "mode"]) $(id).disabled = locked || app.busy || !app.drafts;
  const editable = (!locked || paused) && !app.busy && Boolean(app.drafts);
  for (const id of ["version", "policy", "reset", "paste-profile"]) $(id).disabled = !editable;
  for (const id of ["edit-axis", "edit-allies", "copy-side", "copy-both"]) $(id).disabled = !app.drafts;
  for (const input of app.fields.values()) input.disabled = !editable;
  $("apply-area").hidden = !paused;
  $("apply").disabled = !paused || app.busy;
  $("parameter-note").textContent = paused ? "两方改动会在应用、单步或续跑时一并生效。" :
    locked ? "先暂停测试，再调整任一方的配置。" : "每方独立设置版本、策略和评分参数。";
  $("mode-note").textContent = {single: "本局结束后自动停止。", paired: "同一种子交换双方整套配置，两局后停止。",
    continuous: "同一种子换边两局，再用下一个种子继续，直到手动停止。"}[$("mode").value];
}

function matchSide(game) {
  return game.swapped === undefined ? `B 执${teams[game.search_team]}` : game.swapped ? "双方换边" : "首局配置";
}

function renderOverview() {
  const detail = app.live || app.viewed;
  const status = detail?.status;
  const run = detail?.run;
  const state = status?.requested || status?.state || "idle";
  $("state").dataset.state = state;
  $("state").textContent = states[state] || state;
  const lineup = status?.configs ? sides.map(team => `${teams[team]} ${caption(status.configs[status.match?.swapped ? opposite[team] : team])}`).join(" / ") : run?.version_label;
  $("run-context").textContent = run ? [lineup, modes[run.mode], run.catalog].join(" · ") :
    "选择版本与参数后，即可开始测试。";
  $("completed").textContent = status?.completed || 0;
  $("round").textContent = status?.round ?? "—";
  $("axis").textContent = status?.scores?.axis ?? "—";
  $("allies").textContent = status?.scores?.allies ?? "—";
  const match = status?.match;
  $("position").textContent = match ? [
    `第 ${match.number} 局`, `种子 ${match.seed}`, matchSide(match),
    countries[status.country], phases[status.phase], `第 ${status.step} 步`,
  ].filter(Boolean).join(" · ") : "等待启动";
  $("revision").textContent = status?.weights_revision ? `参数 ${status.weights_revision} · B 预算 3 秒` : "B 决策预算 3 秒";
}

function setOptions(select, entries, value, empty) {
  const other = entries.length ? entries : [["", empty]];
  const signature = JSON.stringify(other);
  if (select.dataset.signature !== signature) {
    select.replaceChildren(...other.map(([id, title]) => {
      const option = node("option", title);
      option.value = id;
      return option;
    }));
    select.dataset.signature = signature;
  }
  if (select.value !== String(value)) select.value = String(value);
  select.disabled = !entries.length;
}

const forceName = key => countries[key] || app.board?.forces[key] || key;
const tokenName = token => token.name || `${forceName(token.force)}${units[token.unit] || token.unit}`;

function showCardZones() {
  const cards = app.board?.seats[$("board-seat").value];
  if (!cards) return;
  const sections = [];
  for (const [zone, heading] of [["HAND", "手牌"], ["STATUS", "状态牌"], ["RESPONSE", "响应牌"]]) {
    const shown = zone === "STATUS" ? [...cards.STATUS, ...cards.DISABLED_STATUS.map(card => ({...card, disabled: true}))] : cards[zone];
    const section = node("section", undefined, "card-zone");
    section.append(node("h3", `${heading} · ${shown.length}`));
    if (shown.length) {
      const list = node("ul");
      for (const card of shown) list.append(node("li", card.name + (card.disabled ? "（停用）" : "")));
      section.append(list);
    } else section.append(node("p", "空", "muted"));
    sections.push(section);
  }
  $("card-zones").replaceChildren(...sections);
}

function showRegion() {
  const region = app.board?.regions[app.region];
  $("board-focus").textContent = region ? [region.name, region.vp ? `${region.vp} VP` : "",
    region.units.length ? region.units.map(tokenName).join("、") : "空区"].filter(Boolean).join(" · ") :
    "点击位置查看部队。";
  for (const button of $("board-regions").querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button.dataset.region === app.region));
  }
}

function paintBoard() {
  if (!app.board) return;
  const regions = Object.entries(app.board.regions);
  const anchored = regions.every(([key]) => Object.hasOwn(boardAnchors, key));
  const printed = app.artReady && anchored;
  const columns = Math.max(1, Math.ceil(Math.sqrt(regions.length * 2)));
  const rows = Math.ceil(regions.length / columns);
  $("board-art").hidden = !printed;
  $("board-stage").classList.toggle("printed", printed);
  $("board-regions").replaceChildren(...regions.map(([key, region], index) => {
    const [x, y] = printed ? boardAnchors[key] : anchored ? [4 + world[key][0] * 6.55, 6 + world[key][1] * 11] :
      [(index % columns + .5) * 100 / columns, (Math.floor(index / columns) + .5) * 100 / rows];
    const button = node("button", undefined, `board-place ${region.type.toLowerCase()}`);
    button.type = "button";
    button.dataset.region = key;
    button.style.left = `${x}%`;
    button.style.top = `${y}%`;
    button.title = [region.name, ...region.units.map(tokenName)].join(" · ");
    button.setAttribute("aria-label", button.title);
    button.addEventListener("click", () => { app.region = key; showRegion(); });
    if (!printed) button.append(node("span", region.name, "board-place-name"));
    if (region.units.length) {
      const groups = new Map();
      for (const token of region.units) {
        const group = token.force + "/" + token.unit;
        if (!groups.has(group)) groups.set(group, {token, count: 0});
        groups.get(group).count++;
      }
      const pieces = node("span", undefined, "board-pieces");
      for (const {token, count} of groups.values()) {
        pieces.append(pieceIcon(token.unit, forceColors[token.force] || "#647769", tokenName(token) + " ×" + count, count));
      }
      button.append(pieces);
    } else if (printed) button.append(node("span", undefined, "board-dot"));
    return button;
  }));
  showRegion();
}

async function loadBoard() {
  const detail = app.live || app.viewed;
  const game = app.live?.status.match?.number || app.game;
  const key = detail && game ? [detail.run.id, game, detail.status.step, Boolean(app.live)].join("/") : "";
  if (key && key === app.boardKey) return;
  const board = key ? await api(`/api/runs/${detail.run.id}/board?game=${game}`) : null;
  if (app.busy || detail !== (app.live || app.viewed)) return;
  app.board = board;
  app.boardKey = board ? key : "";
  $("board-detail").hidden = $("board-fit").hidden = !board;
  $("board-wait").hidden = Boolean(board);
  $("board-position").textContent = board ? `${app.live ? "实时局面" : "保留局面"} · 第 ${board.game} 局 · 第 ${board.step} 步` : "";
  if (!board) {
    $("board-wait").textContent = game ? "这一局暂无棋盘快照。" : "开始测试后显示地图和各国牌区。";
    return;
  }
  const seats = Object.keys(board.seats);
  const picker = $("board-seat");
  setOptions(picker, seats.map(seat => [seat, forceName(seat)]), seats.includes(picker.value) ? picker.value : seats[0], "");
  $("board-legend").replaceChildren(...Object.entries(forceColors).filter(([force]) => countries[force] || board.forces[force])
    .map(([force, color]) => {
      const label = node("span", forceName(force));
      const swatch = node("i");
      swatch.style.backgroundColor = color;
      label.prepend(swatch);
      return label;
    }));
  paintBoard();
  showCardZones();
}

function commandName(command, row, plan) {
  const main = actions[command.type] || command.type;
  const key = command.card || command.option;
  const target = plan ? basic[plan.kind] || plan.kind : (row.names?.[key] || choices[key] || key);
  return target ? main + " · " + target : main;
}

function showSettlement(result) {
  $("settlement").hidden = !result;
  if (!result) return;
  const position = key => {
    if (!key) return "—";
    const [seat, kind] = key.split(":");
    return app.board?.regions[key]?.name || (kind ? `${forceName(seat)}${zones[kind] || kind}` : zones[key] || key);
  };
  const score = Object.entries(teams).filter(([team]) => team !== "draw")
    .map(([team, name]) => `${name} ${result.scores_before[team]} → ${result.scores_after[team]} VP`);
  if (result.awaiting) score.push(`等待${forceName(result.awaiting.required_country)}选择`);
  if (result.winner) score.push(result.winner === "draw" ? "平局" : `${teams[result.winner]}获胜`);
  $("settlement-meta").textContent = score.join(" · ");
  $("settlement-events").replaceChildren(...result.events.map(event => {
    const item = node("li");
    const name = [forceName(event.actor), events[event.type] || event.type, event.ok ? "" : "未结算", event.reason].filter(Boolean);
    item.append(node("strong", name.join(" · ")));
    for (const move of event.moves) item.append(node("small", `${tokenName(move)} · ${position(move.from)} → ${position(move.to)}`));
    for (const {team, amount} of event.scores) item.append(node("small", `${teams[team]} ${amount > 0 ? "+" : ""}${amount} VP`));
    return item;
  }));
}

function showDecision(row, button) {
  for (const item of $("steps").querySelectorAll("[aria-selected]")) item.removeAttribute("aria-selected");
  button?.closest("tr").setAttribute("aria-selected", "true");
  $("decision").hidden = false;
  $("decision-title").textContent = `第 ${row.decision_index} 步 · ${commandName(row.command, row, row.selected_reallocation)}`;
  $("decision-meta").textContent = [countries[row.country], row.policy + " 策略",
    row.ai_version === "current" ? "当前工作区" : row.ai_version,
    "参数 " + row.weights_revision, reasons[row.search_status] || row.search_status,
    `${number(row.elapsed_ms)} ms`, row.selected_q == null ? "" : "采用评分 " + number(row.selected_q)]
    .filter(Boolean).join(" · ");
  const list = [];
  for (const [title, candidates] of [["纸面评分", row.rule_scores], ["结算评分", row.candidate_scores]]) {
    if (!candidates?.length) continue;
    list.push(node("li", title, "score-heading"));
    for (const candidate of candidates) {
      const item = node("li");
      const rank = candidate.complete === false ? (reasons[candidate.reason] || "未完成") : number(candidate.q ?? candidate.rule_score);
      const action = node("div", commandName(candidate.command, row, candidate.reallocation));
      const terms = [["行动机会", candidate.action_cost], ["VP", candidate.actual_vp_delta], ["地图", candidate.map_delta],
        ["其中补给稳定", candidate.supply_stability_delta],
        ["牌差", candidate.card_delta], ["过牌", candidate.cycle_score ?? candidate.retention?.draw_cycle],
        ["损失可达分点", -(candidate.retention?.expansion_cost ?? candidate.reallocation?.discard_expansion_cost ?? 0)]]
        .filter(([, value]) => value);
      if (terms.length) action.append(node("small", terms.map(([label, value]) => label + " " + number(value)).join(" · ")));
      item.append(action, node("span", rank));
      list.push(item);
    }
  }
  $("decision-scores").replaceChildren(...(list.length ? list : [node("li", "本步没有可比较的候选评分。", "muted")]));
  showSettlement(row.settlement);
  $("decision-raw").textContent = JSON.stringify(row, null, 2);
}

function clearLog() {
  app.pageKey = "";
  app.cursor = null;
  app.next = null;
  $("decision").hidden = true;
  $("steps").replaceChildren();
  $("step-table").hidden = true;
  $("pager").hidden = true;
  $("empty").hidden = false;
}

function renderGameSelect() {
  const games = app.viewed?.games || [];
  const current = app.viewed?.status.match;
  const entries = games.map(game => [game.number, `第 ${game.number} 局 · ${game.seed} · ${matchSide(game)}`]);
  if (current && !games.some(game => game.number === current.number)) {
    entries.push([current.number, `第 ${current.number} 局 · ${current.seed} · ${matchSide(current)}`]);
  }
  const selected = app.followGame || !entries.some(([game]) => game === app.game) ? (entries.at(-1)?.[0] || 0) : app.game;
  if (selected !== app.game) clearLog();
  app.game = selected;
  setOptions($("games"), entries, app.game || "", "暂无对局");
  const finished = games.find(game => game.number === app.game);
  $("log-summary").textContent = finished ? [
    finished.winner === "draw" ? "平局" : teams[finished.winner] + "获胜",
    `轴心 ${finished.scores.axis} · 同盟 ${finished.scores.allies}`, `${finished.steps} 步`,
  ].join(" · ") : "";
}

async function loadLog() {
  const id = app.runId;
  const game = app.game;
  const cursor = app.cursor;
  if (!id || !game) { $("downloads").hidden = true; return; }
  const base = "/api/runs/" + id;
  const suffix = "?game=" + game;
  const page = await api(base + "/log" + suffix + (cursor ? "&before=" + cursor : ""));
  if (id !== app.runId || game !== app.game || cursor !== app.cursor) return;
  app.next = page.before;
  $("downloads").hidden = !page.rows.length;
  $("download-log").href = base + "/download" + suffix;
  $("download-weights").href = base + "/parameters";
  $("older").disabled = !page.before;
  $("newest").disabled = !cursor;
  $("pager").hidden = !page.rows.length;
  $("step-table").hidden = !page.rows.length;
  $("empty").hidden = Boolean(page.rows.length);
  const key = [id, game, cursor, page.rows[0]?.decision_index, page.rows.length].join("|");
  if (key === app.pageKey) return;
  app.pageKey = key;
  $("steps").replaceChildren(...page.rows.map(row => {
    const tr = node("tr");
    const cell = node("td");
    const button = node("button", String(row.decision_index), "step-link");
    button.type = "button";
    button.dataset.step = row.decision_index;
    button.addEventListener("click", () => showDecision(row, button));
    cell.append(button);
    tr.append(cell, node("td", `${countries[row.country] || row.country} · ${row.policy}`), node("td", phases[row.phase] || row.phase),
      node("td", commandName(row.command, row, row.selected_reallocation)), node("td", row.weights_revision),
      node("td", number(row.elapsed_ms) + " ms", "timing"));
    return tr;
  }));
  const step = app.awaitingStep?.target;
  const row = step?.game === game && page.rows.find(row => row.decision_index === step.step);
  if (row && app.awaitingStep.id === id) {
    showDecision(row, $("steps").querySelector(`[data-step="${step.step}"]`));
    app.awaitingStep = null;
    $("decision-title").focus({preventScroll: true});
    $("decision").scrollIntoView({block: "start"});
  }
}

function acceptLive(detail) {
  const status = detail.status;
  if (app.live?.run.id !== detail.run.id) {
    $("seed").value = detail.run.seed;
    $("mode").value = detail.run.mode;
  }
  app.live = detail;
  const key = [detail.run.id, status.weights_revision, status.match?.swapped].join(":");
  if (app.pinnedRevision !== key && !status.requested && !status.error) {
    if (status.configs) app.drafts = Object.fromEntries(sides.map(team => {
      const config = status.configs[team];
      return [team, preset(config.version, config.policy, config.weights)];
    }));
    renderEditor();
    app.pinnedRevision = key;
  }
  if (status.error) message(status.error, true);
  else if (status.state === "paused" && !status.requested) message("");
}

async function refresh() {
  if (app.refreshing || app.busy) return;
  app.refreshing = true;
  try {
    const index = await api("/api/runs");
    if (!app.runId || !index.runs.some(run => run.id === app.runId)) app.runId = index.active || index.runs[0]?.id || "";
    setOptions($("runs"), index.runs.map(run => [run.id, `${date(run.created_at)} · ${run.version_label}`]), app.runId, "暂无测试");
    const browsing = app.runId;
    const [live, viewed] = await Promise.all([
      index.active ? api("/api/runs/" + index.active) : null,
      browsing && browsing !== index.active ? api("/api/runs/" + browsing) : null,
    ]);
    if (app.busy) return;
    if (live) acceptLive(live);
    else {
      if (app.live && !viewed) message("");
      const wasSwapped = app.live?.status.match?.swapped;
      app.live = null;
      app.pinnedRevision = "";
      if (wasSwapped) renderEditor();
    }
    if (browsing === app.runId) {
      app.viewed = viewed || live;
      const step = app.viewed?.status.last_step;
      if (app.awaitingStep?.id === browsing && step?.revision > app.awaitingStep.previous && !app.awaitingStep.target) {
        app.awaitingStep.target = step;
        app.followGame = false;
        app.game = step.game;
        clearLog();
      } else if (app.awaitingStep?.id === browsing && app.viewed?.status.error) {
        app.awaitingStep = null;
      }
      renderGameSelect();
      await loadBoard();
      await loadLog();
    }
    renderControls();
    renderOverview();
  } catch (error) {
    message(error.message, true);
  } finally {
    app.refreshing = false;
  }
}

const record = value => value !== null && typeof value === "object" && !Array.isArray(value);

function checkedProfile(raw) {
  const version = app.versions.get(raw?.version);
  if (!version || !["A", "B"].includes(raw?.policy) || !record(raw.values)) {
    throw new Error("配置中的 AI 版本、策略或评分参数无效。");
  }
  const result = preset(raw.version, raw.policy);
  const fields = new Map(version.fields.map(field => [field.key, field]));
  for (const [key, value] of Object.entries(raw.values)) {
    if (version.inactive_fields?.includes(key)) continue;
    const field = fields.get(key);
    if (!field) throw new Error(`此版本没有参数 ${key}。`);
    if (!Number.isFinite(value) || value < 0 || value > 1e6 || (field.integer && !Number.isInteger(value))) {
      throw new Error(`${field.label}的数值无效。`);
    }
    result.values[key] = value;
  }
  return result;
}

function profileNotice(text) {
  $("profile-notice").textContent = text;
  $("profile-notice").hidden = !text;
}

function selectTeam(team) {
  app.editTeam = team;
  renderEditor();
  renderControls();
}

function requestConfigs() {
  const configs = {};
  for (const team of sides) {
    try {
      configs[slot(team)] = checkedProfile(draftFor(team));
    } catch (error) {
      selectTeam(team);
      profileNotice(`${teams[team]} · ${error.message}`);
      return null;
    }
  }
  return configs;
}

function configDialog(text) {
  const copying = text !== undefined;
  $("config-title").textContent = copying ? "复制 AI 配置" : "粘贴 AI 配置";
  $("config-label").textContent = copying ? "复制下方文本" : `粘贴配置文本，单方配置会载入${teams[app.editTeam]}`;
  $("config-text").readOnly = copying;
  $("config-text").value = text || "";
  $("config-error").hidden = true;
  $("confirm-paste").hidden = copying;
  $("config-dialog").showModal();
  $("config-text").focus();
  if (copying) $("config-text").select();
}

async function copyConfig(both) {
  const data = {format: configFormat};
  try {
    if (both) data.sides = Object.fromEntries(sides.map(team => [team, checkedProfile(draftFor(team))]));
    else data.profile = checkedProfile(draftFor(app.editTeam));
  } catch (error) {
    profileNotice(error.message);
    return;
  }
  const text = JSON.stringify(data, null, 2);
  try {
    await navigator.clipboard.writeText(text);
    profileNotice(both ? "已复制双方 AI 配置。" : `已复制${teams[app.editTeam]} AI 配置。`);
  } catch {
    configDialog(text);
  }
}

function pasteConfig() {
  try {
    let data;
    try { data = JSON.parse($("config-text").value); }
    catch { throw new Error("配置文本无法读取，请粘贴完整内容。"); }
    if (data?.format !== configFormat || Boolean(data.sides) === Boolean(data.profile)) {
      throw new Error("请选择从调试台复制的 AI 配置文本。");
    }
    const paired = data.sides;
    const replacements = paired ? sides.map(team => [slot(team), checkedProfile(paired[team])]) :
      [[slot(app.editTeam), checkedProfile(data.profile)]];
    for (const [team, profile] of replacements) app.drafts[team] = profile;
    $("config-dialog").close();
    renderEditor();
    renderControls();
    profileNotice(paired ? "已载入双方 AI 配置。" : `已载入${teams[app.editTeam]} AI 配置。`);
  } catch (error) {
    $("config-error").textContent = error.message;
    $("config-error").hidden = false;
  }
}

async function act(action) {
  if (app.busy) return;
  const payload = {};
  if (["start", "weights", "resume", "step"].includes(action)) {
    payload.configs = requestConfigs();
    if (!payload.configs) return;
  }
  let url = "/api/runs";
  if (action === "start") {
    if (!$("seed").reportValidity()) return;
    Object.assign(payload, {seed: $("seed").valueAsNumber, mode: $("mode").value});
  } else if (app.live) {
    url += "/" + app.live.run.id + "/" + action;
  } else return;
  app.busy = true;
  renderControls();
  message("");
  profileNotice("");
  try {
    const previous = app.live?.status.last_step?.revision || 0;
    const result = await api(url, payload);
    if (action === "start" || action === "step") {
      app.runId = result.run.id;
      app.followGame = true;
      app.game = 0;
      clearLog();
    }
    if (action === "step") app.awaitingStep = {id: result.run.id, previous};
    if (action === "resume") app.followGame = true;
    acceptLive(result);
    renderOverview();
  } catch (error) {
    message(error.message, true);
  } finally {
    app.busy = false;
    renderControls();
    await refresh();
  }
}

$("primary").addEventListener("click", () => act(app.live ? (app.live.status.state === "paused" ? "resume" : "pause") : "start"));
$("apply").addEventListener("click", () => act("weights"));
$("step").addEventListener("click", () => act("step"));
$("board-seat").addEventListener("change", showCardZones);
$("board-fit").addEventListener("click", event => {
  const fit = $("board-scroll").classList.toggle("fit");
  event.target.setAttribute("aria-pressed", String(fit));
  event.target.textContent = fit ? "恢复原尺寸" : "适应宽度";
});
$("board-art").addEventListener("load", () => { app.artReady = true; paintBoard(); });
$("board-art").src = "/board-map.png";
$("stop").addEventListener("click", () => act("stop"));
for (const team of sides) $("edit-" + team).addEventListener("click", () => selectTeam(team));
function changeVersion(version) {
  app.drafts[slot(app.editTeam)] = preset(version, $("policy").value);
  renderEditor();
  renderControls();
  profileNotice("");
}
$("reset").addEventListener("click", () => changeVersion($("version").value));
$("version").addEventListener("change", () => changeVersion($("version").value));
$("policy").addEventListener("change", () => { draftFor(app.editTeam).policy = $("policy").value; renderTeamLabels(); });
$("copy-side").addEventListener("click", () => copyConfig(false));
$("copy-both").addEventListener("click", () => copyConfig(true));
$("paste-profile").addEventListener("click", () => configDialog());
$("confirm-paste").addEventListener("click", pasteConfig);
$("mode").addEventListener("change", renderControls);
$("runs").addEventListener("change", () => {
  app.runId = $("runs").value;
  app.game = 0;
  app.followGame = true;
  clearLog();
  refresh();
});
$("games").addEventListener("change", () => {
  app.game = Number($("games").value);
  app.followGame = false;
  clearLog();
  refresh();
});
$("older").addEventListener("click", () => {
  if (app.next) {
    app.cursor = app.next;
    loadLog().catch(error => message(error.message, true));
  }
});
$("newest").addEventListener("click", () => {
  app.cursor = null;
  loadLog().catch(error => message(error.message, true));
});

(async () => {
  try {
    const data = await api("/api/versions");
    if (!data.length) throw new Error("找不到当前工作区的 AI 版本。");
    app.versions = new Map(data.map(version => [version.id, version]));
    const current = app.versions.get("current");
    $("version-meta").textContent = `对局规则 · ${current.catalog}`;
    setOptions($("version"), data.map(version => [version.id, version.label]), current.id, "");
    app.drafts = {axis: preset(current.id, "B"), allies: preset(current.id, "A")};
    renderEditor();
    renderControls();
    await refresh();
  } catch (error) {
    message(error.message, true);
  }
  window.setInterval(refresh, 1800);
})();
