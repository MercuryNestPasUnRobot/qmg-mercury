"use strict";

const $ = id => document.getElementById(id);
const countries = {germany: "德国", "united-kingdom": "英国", japan: "日本", "soviet-union": "苏联",
  italy: "意大利", "united-states": "美国"};
const phases = {Setup: "开局", Play: "出牌", Air: "空军", Supply: "补给", Victory: "计分", Discard: "弃牌", Draw: "摸牌"};
const teams = {axis: "轴心", allies: "同盟", draw: "平局"};
const states = {idle: "待开始", starting: "准备中", running: "运行中", pause: "暂停中", paused: "已暂停",
  stop: "停止中", run: "继续中", completed: "已完成", stopped: "已停止", failed: "出错", interrupted: "已中断"};
const modes = {single: "单局", paired: "换边两局", continuous: "连续换边"};
const actions = {END_PHASE: "结束阶段", PLAY_CARD: "出牌", DISCARD_CARD: "弃牌", FORFEIT_PLAY: "无牌可弃",
  CHOOSE: "选择", USE_STATUS: "发动状态牌", USE_AIR: "空军行动", REALLOCATE: "调配资源", ACTIVATE_CARD: "主动响应"};
const basic = {"build-army": "建设陆军", "build-navy": "建设海军", "land-battle": "发起陆战", "sea-battle": "发起海战"};
const choices = {use: "发动", decline: "放弃", pass: "跳过", confirm: "确认", cancel: "取消", done: "完成"};
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
  cursor: null, next: null, draft: new Map(), fields: new Map(), profile: null,
  pinnedRevision: "", pageKey: "", busy: false, refreshing: false,
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
  $("message").textContent = errors[text] || text || "";
}

function syncFields(version, weights) {
  const profile = app.versions.get(version);
  if (!profile) return;
  app.profile = profile;
  app.draft = new Map(profile.fields.map(field => [
    field.key, weights ? field.path.reduce((part, key) => part[key], weights) : field.value,
  ]));
  $("version").value = version;
  $("version-meta").textContent = [profile.catalog, profile.commit?.slice(0, 8)].filter(Boolean).join(" · ");
  if (app.fields.size && [...app.fields.keys()].join() === profile.fields.map(f => f.key).join()) {
    for (const [key, input] of app.fields) input.value = String(app.draft.get(key));
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
      input.value = String(app.draft.get(field.key));
      input.title = field.key;
      input.addEventListener("input", () => app.draft.set(field.key, input.valueAsNumber));
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
  const pending = Boolean(status?.requested) || status?.state === "starting";
  $("primary").textContent = locked ? (paused ? "继续测试" : "暂停测试") : "开始测试";
  $("primary").disabled = app.busy || pending || !app.profile;
  $("stop").hidden = !locked;
  $("stop").disabled = app.busy || status?.requested === "stop";
  for (const id of ["version", "seed", "mode", "team"]) $(id).disabled = locked || app.busy || !app.profile;
  const editable = (!locked || paused) && !app.busy && Boolean(app.profile);
  for (const input of app.fields.values()) input.disabled = !editable;
  $("reset").disabled = !editable;
  $("apply-area").hidden = !paused;
  $("apply").disabled = !paused || app.busy;
  $("parameter-note").textContent = paused ? "应用后会保留当前局面。也可以修改后直接继续测试。" :
    "开跑前可以调整。对局中先暂停，再修改。";
  $("mode-note").textContent = {single: "本局结束后自动停止。", paired: "同一种子让 A、B 换边，两局后停止。",
    continuous: "同一种子换边两局，再用下一个种子继续，直到手动停止。"}[$("mode").value];
}

function renderOverview() {
  const detail = app.live || app.viewed;
  const status = detail?.status;
  const run = detail?.run;
  const state = status?.requested || status?.state || "idle";
  $("state").dataset.state = state;
  $("state").textContent = states[state] || state;
  $("run-context").textContent = run ? [run.version_label, modes[run.mode], run.catalog].join(" · ") :
    "选择版本与参数后，即可开始测试。";
  $("completed").textContent = status?.completed || 0;
  $("round").textContent = status?.round ?? "—";
  $("axis").textContent = status?.scores?.axis ?? "—";
  $("allies").textContent = status?.scores?.allies ?? "—";
  const match = status?.match;
  $("position").textContent = match ? [
    `第 ${match.number} 局`, `种子 ${match.seed}`, `B 执${teams[match.search_team]}`,
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

function commandName(command, row, plan) {
  const main = actions[command.type] || command.type;
  const key = command.card || command.option;
  const target = plan ? basic[plan.kind] || plan.kind : (row.names?.[key] || choices[key] || key);
  return target ? main + " · " + target : main;
}

function showDecision(row, button) {
  for (const item of $("steps").querySelectorAll("[aria-selected]")) item.removeAttribute("aria-selected");
  button?.closest("tr").setAttribute("aria-selected", "true");
  $("decision").hidden = false;
  $("decision-title").textContent = `第 ${row.decision_index} 步 · ${commandName(row.command, row, row.selected_reallocation)}`;
  $("decision-meta").textContent = [countries[row.country], row.policy + " 策略",
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
      if (candidate.complete) {
        const terms = [["VP", candidate.actual_vp_delta], ["地图", candidate.map_delta], ["牌差", candidate.card_delta],
          ["过牌", candidate.cycle_score]].filter(([, value]) => value);
        if (terms.length) action.append(node("small", terms.map(([label, value]) => label + " " + number(value)).join(" · ")));
      }
      item.append(action, node("span", rank));
      list.push(item);
    }
  }
  $("decision-scores").replaceChildren(...list);
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
  const entries = games.map(game => [game.number, `第 ${game.number} 局 · ${game.seed} · B 执${teams[game.search_team]}`]);
  if (current && !games.some(game => game.number === current.number)) {
    entries.push([current.number, `第 ${current.number} 局 · ${current.seed} · B 执${teams[current.search_team]}`]);
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
    button.addEventListener("click", () => showDecision(row, button));
    cell.append(button);
    tr.append(cell, node("td", countries[row.country] || row.country), node("td", phases[row.phase] || row.phase),
      node("td", commandName(row.command, row, row.selected_reallocation)), node("td", row.weights_revision),
      node("td", number(row.elapsed_ms) + " ms", "timing"));
    return tr;
  }));
}

function acceptLive(detail) {
  const status = detail.status;
  if (app.live?.run.id !== detail.run.id) {
    $("seed").value = detail.run.seed;
    $("mode").value = detail.run.mode;
    $("team").value = detail.run.first_team;
  }
  const key = detail.run.id + ":" + status.weights_revision;
  if (app.pinnedRevision !== key && !status.requested && !status.error) {
    syncFields(detail.run.version, status.weights);
    app.pinnedRevision = key;
  }
  if (status.error) message(status.error, true);
  else if (status.state === "paused" && !status.requested) message("");
  app.live = detail;
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
      app.live = null;
      app.pinnedRevision = "";
    }
    if (browsing === app.runId) {
      app.viewed = viewed || live;
      renderGameSelect();
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

function values() {
  for (const input of app.fields.values()) {
    if (!input.checkValidity()) {
      for (let detail = input.closest("details"); detail; detail = detail.parentElement.closest("details")) {
        detail.open = true;
      }
      input.reportValidity();
      return null;
    }
  }
  return Object.fromEntries(app.draft);
}

async function act(action) {
  if (app.busy) return;
  const payload = {};
  if (["start", "weights", "resume"].includes(action)) {
    payload.values = values();
    if (!payload.values) return;
  }
  let url = "/api/runs";
  if (action === "start") {
    if (!$("seed").reportValidity()) return;
    Object.assign(payload, {version: $("version").value, seed: $("seed").valueAsNumber, mode: $("mode").value,
      first_team: $("team").value});
  } else if (app.live) {
    url += "/" + app.live.run.id + "/" + action;
  } else return;
  app.busy = true;
  renderControls();
  message("");
  try {
    const result = await api(url, payload);
    if (action === "start") {
      app.runId = result.run.id;
      app.followGame = true;
      app.game = 0;
      clearLog();
    }
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
$("stop").addEventListener("click", () => act("stop"));
$("reset").addEventListener("click", () => { syncFields($("version").value); renderControls(); });
$("version").addEventListener("change", () => { syncFields($("version").value); renderControls(); });
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
    setOptions($("version"), data.map(version => [version.id, version.label]), data[0].id, "");
    syncFields(data[0].id);
    renderControls();
    await refresh();
  } catch (error) {
    message(error.message, true);
  }
  window.setInterval(refresh, 1800);
})();
