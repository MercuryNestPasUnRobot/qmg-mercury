"use strict";

const $ = id => document.getElementById(id);
const names = {germany: "德国", "united-kingdom": "英国", japan: "日本", "soviet-union": "苏联",
  italy: "意大利", "united-states": "美国", france: "法国", china: "中国"};
const factions = {germany: "axis", japan: "axis", italy: "axis", "united-kingdom": "allies", "soviet-union": "allies", "united-states": "allies"};
const phaseNames = {Setup: "开局弃牌", Play: "出牌", Air: "空军", Supply: "补给", Victory: "计分", Discard: "弃牌", Draw: "摸牌"};
const unitNames = {ARMY: "陆军", NAVY: "海军", AIR: "空军"};
const zoneLabels = {HAND: "手牌", RESPONSE: "响应牌", STATUS: "状态牌", DISABLED_STATUS: "停用状态牌", DISCARD: "弃牌堆"};
const playLabels = {play: "出牌", response: "响应", air: "空军"};
const seats = Object.keys(factions);
const terminal = new Set(["completed", "failed", "stopped"]);
const setup = {versions: new Map(), profiles: {}, editing: "", occupied: ""};
let match = null, ignored = "", busy = false, polling = false, matchKey = "", regionKey = "", cardSeat = "";
let requestGeneration = 0;

function node(tag, text, cls) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}

function button(text, action, css = "button secondary") {
  const el = node("button", text, css);
  el.type = "button";
  el.addEventListener("click", action);
  return el;
}

async function api(path, payload) {
  const response = await fetch(path, payload === undefined ? {} : {method: "POST",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "无法读取本机对局。");
  return data;
}

function message(text, error = false) {
  const banner = $("player-message");
  banner.textContent = text;
  banner.hidden = !text;
  banner.dataset.error = String(error);
}

function options(select, rows, choice) {
  select.replaceChildren(...rows.map(([value, text]) => {
    const el = node("option", text);
    el.value = value;
    return el;
  }));
  if (choice) select.value = choice;
}

function selectHuman() {
  const team = $("human-team").value;
  const list = seats.filter(seat => factions[seat] === team);
  const seat = setup.occupied;
  options($("human-seat"), list.map(seat => [seat, names[seat]]), list.includes(seat) ? seat : list[0]);
  setup.occupied = $("human-seat").value;
  paintSetup();
}

function profileLabel(config) {
  return (config.version === "current" ? setup.versions.get(config.version).label : config.version) + " · " + config.policy;
}

function paintSetup() {
  const available = seats.filter(seat => seat !== setup.occupied);
  if (!available.includes(setup.editing)) setup.editing = available[0];
  $("setup-seats").replaceChildren(...available.map(seat => {
    const select = button(undefined, () => { setup.editing = seat; paintSetup(); }, "team-choice");
    select.setAttribute("aria-pressed", String(seat === setup.editing));
    select.append(node("strong", names[seat]), node("small", profileLabel(setup.profiles[seat])));
    return select;
  }));
  const profile = setup.profiles[setup.editing];
  $("player-version").value = profile.version;
  $("player-policy").value = profile.policy;
  paintWeights(profile);
}

function paintWeights(profile) {
  const schema = setup.versions.get(profile.version);
  const fields = new Map(schema.fields.map(field => [field.key, field]));
  const links = schema.linked_fields || [];
  const coefficients = new Set(links.map(link => link.coefficient));
  const inputs = [];
  function inputField(field, value, changed, clear) {
    const wrapper = node("label", field.label, "field");
    const el = node("input");
    el.type = "number";
    el.min = String(field.min ?? 0);
    el.max = String(field.max ?? 1000000);
    el.step = field.integer ? "1" : "any";
    el.value = value ?? "";
    el.addEventListener("input", () => el.setCustomValidity(field.min_exclusive && el.valueAsNumber === Number(el.min) ?
      `${field.label}须大于 ${el.min}。` : ""));
    el.addEventListener("change", () => changed(el));
    wrapper.append(el);
    if (clear) wrapper.append(clear);
    inputs.push(wrapper);
    return el;
  }
  for (const field of fields.values()) {
    if (coefficients.has(field.key)) continue;
    const input = inputField(field, profile.values[field.key], el => {
      if (!el.reportValidity()) return;
      if (el.value) profile.values[field.key] = el.valueAsNumber;
      else delete profile.values[field.key];
      if (field.key.startsWith("scoring_units.")) paintWeights(profile);
    });
    input.placeholder = String(field.value);
  }
  for (const link of links) {
    const configured = key => profile.values[key] ?? fields.get(key)?.value;
    const linked = configured(link.base) * link.multiplier * (link.coefficient ? configured(link.coefficient) : 1);
    const restore = button("恢复联动", () => { delete profile.overrides[link.key]; paintWeights(profile); }, "text-button");
    const input = inputField(link, profile.overrides[link.key], el => {
      if (!el.reportValidity()) return;
      if (el.value) profile.overrides[link.key] = el.valueAsNumber;
      else delete profile.overrides[link.key];
      restore.disabled = !Object.hasOwn(profile.overrides, link.key);
    }, restore);
    input.placeholder = `联动 ${Number(linked.toFixed(6))}`;
    restore.disabled = !Object.hasOwn(profile.overrides, link.key);
  }
  $("player-weights").replaceChildren(...inputs);
}

function applyGroup(team) {
  const profile = setup.profiles[setup.editing];
  for (const seat of seats) {
    if (seat !== setup.occupied && (!team || factions[seat] === team)) setup.profiles[seat] = structuredClone(profile);
  }
  paintSetup();
}

function inspect(title, text) {
  $("player-card-title").textContent = title;
  $("player-card-text").textContent = text;
  $("player-card-dialog").showModal();
}

async function act(action, payload) {
  if (busy || !match) return;
  busy = true;
  requestGeneration++;
  message("");
  try {
    paintMatch(await api(`/api/player/${match.id}/${action}`, payload));
  } catch (error) {
    message(error.message, true);
  } finally {
    busy = false;
    poll();
  }
}

const choose = index => act("command", {choice: index, version: match.version});

function paintRegion() {
  const place = match?.board?.regions[regionKey];
  const area = $("player-region");
  if (!place) { area.textContent = "选择地区可查看驻军和相邻地区。"; return; }
  const title = `${place.name}${place.vp ? ` · ${place.vp} VP` : ""}`;
  const held = place.units.map(unit => (names[unit.force] || match.board.forces[unit.force] || unit.force) + unitNames[unit.unit]);
  const neighbors = place.neighbors.map(key => match.board.regions[key].name);
  area.replaceChildren(node("strong", title), node("p", held.join("、") || "无驻军"),
    node("p", neighbors.length ? "相邻 · " + neighbors.join("、") : "无相邻地区", "muted"));
  for (const [i, choice] of match.choices.entries()) {
    if (choice.region === regionKey) area.append(button(choice.label, () => choose(i)));
  }
}

function paintBoard() {
  if (!match?.board) return;
  const regions = Object.entries(match.board.regions);
  const art = $("player-board-art");
  const printed = art.naturalWidth > 0 && regions.every(([id]) => Object.hasOwn(boardAnchors, id));
  art.hidden = !printed;
  $("player-board-stage").classList.toggle("printed", printed);
  const columns = Math.max(1, Math.ceil(Math.sqrt(regions.length * 2)));
  const rows = Math.ceil(regions.length / columns);
  const targets = new Set(match.choices.map(choice => choice.region));
  $("board-regions").replaceChildren(...regions.map(([id, place], i) => {
    const [x, y] = printed ? boardAnchors[id] : [(i % columns + .5) * 100 / columns, (Math.floor(i / columns) + .5) * 100 / rows];
    const marker = button(undefined, () => { regionKey = id; paintRegion(); highlightRegion(); },
      `board-place ${place.type.toLowerCase()}${targets.has(id) ? " target" : ""}`);
    marker.dataset.region = id;
    marker.style.left = `${x}%`;
    marker.style.top = `${y}%`;
    marker.title = [place.name, ...place.units.map(u => (names[u.force] || u.force) + unitNames[u.unit])].join(" · ");
    marker.setAttribute("aria-label", marker.title);
    if (!printed) marker.append(node("span", place.name, "board-place-name"));
    if (place.units.length) {
      const pieces = node("span", undefined, "board-pieces");
      for (const unit of place.units) pieces.append(pieceIcon(unit.unit, forceColors[unit.force] || "#647769",
        (names[unit.force] || unit.force) + unitNames[unit.unit]));
      marker.append(pieces);
    } else if (printed) marker.append(node("span", undefined, "board-dot"));
    return marker;
  }));
  highlightRegion();
  paintRegion();
}

function highlightRegion() {
  for (const marker of $("board-regions").children) {
    marker.setAttribute("aria-pressed", String(marker.dataset.region === regionKey));
  }
}

function paintCards() {
  if (!match?.board) return;
  const positions = match.board.seats;
  $("player-seats").replaceChildren(...seats.map(seat => {
    const entry = positions[seat];
    const chip = button(undefined, () => { cardSeat = seat; paintCards(); }, "team-choice");
    chip.setAttribute("aria-pressed", String(cardSeat === seat));
    const controller = seat === match.human ? "你" : "AI " + profileLabel(match.seats[seat]);
    chip.append(node("strong", `${names[seat]} · ${controller}`), node("small",
      `手牌 ${entry.counts.HAND} · 响应 ${entry.counts.RESPONSE} · 弃牌 ${entry.counts.DISCARD} · 牌库 ${entry.counts.DECK}`));
    return chip;
  }));
  const entry = positions[cardSeat];
  const visible = Object.entries(entry.cards).filter(([kind]) => cardSeat === match.human ||
    kind === "STATUS" || kind === "DISABLED_STATUS");
  $("player-card-zones").replaceChildren(...visible.map(([kind, cards]) => {
    const group = node("div", undefined, "player-card-group");
    group.append(node("h3", `${names[cardSeat]} · ${zoneLabels[kind]} · ${cards.length} 张`));
    for (const card of cards) group.append(button(card.name, () => inspect(card.name, card.text)));
    return group;
  }));
}

function paintMatch(next) {
  if (!next || next.id === ignored) return;
  if (match?.id !== next.id) { cardSeat = next.human; regionKey = ""; }
  match = next;
  $("player-setup").hidden = true;
  $("player-match").hidden = false;
  const key = [match.id, match.version, match.state, Boolean(match.replay)].join("/");
  if (key === matchKey) return;
  matchKey = key;
  $("match-caption").textContent = `${match.map} · 你执${names[match.human]}`;
  $("match-stop").hidden = terminal.has(match.state) || ["archiving", "stopping"].includes(match.state);
  $("match-new").hidden = !terminal.has(match.state);
  $("match-replay").hidden = !match.replay;
  if (match.replay) $("match-replay").href = match.replay;
  const turn = match.turn;
  const seat = turn?.phase === "Setup" ? match.actor : match.active;
  $("player-turn").textContent = turn ? `第 ${turn.round_id} 轮 · ${names[seat]} · ${phaseNames[turn.phase]}` : "准备开局";
  const winner = match.winner === "draw" ? "平局" : match.winner === "axis" ? "轴心获胜" : "同盟获胜";
  const status = {starting: "正在准备对局", human: match.may_pass ? "你可以在当前时机响应" : "轮到你操作",
    ai: `等待${names[match.actor] || "其他"} AI`, resolving: "正在结算", archiving: `${match.winner ? winner : "对局已结束"} · 正在核对录像`,
    completed: winner, stopped: "这局已结束", stopping: "正在结束这局", failed: match.error};
  $("player-timing").textContent = status[match.state];
  for (const team of ["axis", "allies"]) $(team + "-vp").textContent = match.board?.scores[team] ?? 0;
  const regular = $("player-actions");
  const opening = $("player-opening-action");
  regular.replaceChildren();
  opening.replaceChildren();
  $("player-opening").hidden = match.state !== "human" || !match.reallocation_open;
  for (const [i, choice] of match.choices.entries()) {
    const row = node("div", undefined, "player-move");
    row.append(button(choice.label, () => choose(i)));
    if (choice.text) row.append(button("牌文", () => inspect(choice.label, choice.text), "text-button"));
    (choice.opening ? opening : regular).append(row);
  }
  $("player-opening-note").textContent = opening.childElementCount
    ? "在本回合出牌前，可调配一次。所找基本牌不含空军行动。"
    : "当前没有足够的可弃手牌，或没有可找的基本牌。";
  if (!match.choices.length) regular.append(node("p", status[match.state], "muted"));
  const plays = $("player-plays");
  plays.replaceChildren(...(match.recent_plays || []).map(play => {
    const title = [names[play.country] || names[play.owner], playLabels[play.kind], play.name].join(" · ");
    return button(title, () => inspect(title, play.text));
  }));
  if (!plays.childElementCount) plays.append(node("small", "尚未公开出牌。", "muted"));
  $("player-pass").hidden = !match.may_pass;
  paintBoard();
  paintCards();
}

async function poll() {
  if (polling || busy) return;
  polling = true;
  const generation = requestGeneration;
  try {
    const next = await api("/api/player");
    if (!busy && generation === requestGeneration) paintMatch(next);
  } catch (error) {
    message(error.message, true);
  } finally {
    polling = false;
  }
}

$("human-team").addEventListener("change", selectHuman);
$("human-seat").addEventListener("change", () => { setup.occupied = $("human-seat").value; paintSetup(); });
$("player-version").addEventListener("change", event => {
  const previous = setup.profiles[setup.editing];
  setup.profiles[setup.editing] = {version: event.target.value, policy: previous.policy, values: {}, overrides: {}};
  paintSetup();
});
$("player-policy").addEventListener("change", event => { setup.profiles[setup.editing].policy = event.target.value; paintSetup(); });
$("copy-all-ai").addEventListener("click", () => applyGroup());
$("copy-axis-ai").addEventListener("click", () => applyGroup("axis"));
$("copy-allies-ai").addEventListener("click", () => applyGroup("allies"));
$("player-pass").addEventListener("click", () => choose(-1));
$("match-stop").addEventListener("click", () => act("stop", {}));
$("match-new").addEventListener("click", () => {
  ignored = match.id;
  $("player-setup").hidden = false;
  $("player-match").hidden = $("match-new").hidden = $("match-replay").hidden = true;
  $("match-caption").textContent = "选择一个真人席位，其余五席由 AI 控制。";
});
$("player-board-art").addEventListener("load", paintBoard);
$("player-fit").addEventListener("click", event => {
  const fit = $("player-board-scroll").classList.toggle("fit");
  event.target.setAttribute("aria-pressed", String(fit));
  event.target.textContent = fit ? "恢复原尺寸" : "适应宽度";
});
$("player-start-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (busy) return;
  const invalid = [...$("player-weights").querySelectorAll("input")].find(input => !input.validity.valid);
  if (invalid) { $("player-weights").parentElement.open = true; invalid.reportValidity(); return; }
  busy = $("player-start").disabled = true;
  requestGeneration++;
  message("");
  try {
    const human = setup.occupied;
    const configs = Object.fromEntries(seats.filter(seat => seat !== human).map(seat => [seat, setup.profiles[seat]]));
    paintMatch(await api("/api/player", {human, map: $("player-map").value, seats: configs,
      seed: $("player-seed").value ? $("player-seed").valueAsNumber : null}));
  } catch (error) {
    message(error.message, true);
  } finally {
    busy = $("player-start").disabled = false;
    poll();
  }
});

(async () => {
  try {
    const [versions, maps] = await Promise.all([api("/api/versions"), api("/api/maps")]);
    setup.versions = new Map(versions.map(version => [version.id, version]));
    options($("player-version"), versions.map(version => [version.id, version.label]), "current");
    options($("player-map"), maps.map(map => [map.id, map.name]), "standard");
    setup.profiles = Object.fromEntries(seats.map(seat => [seat, {version: "current", policy: "A", values: {}, overrides: {}}]));
    selectHuman();
    $("player-start").disabled = false;
    await poll();
    setInterval(poll, 650);
  } catch (error) {
    message(error.message, true);
  }
})();
