"use strict";

const $ = id => document.getElementById(id);
function node(tag, value, cls) {
  const element = document.createElement(tag);
  if (value !== undefined) element.textContent = value;
  if (cls) element.className = cls;
  return element;
}

const world = {
  canada: [0, 1], "united-states": [0, 3], "latin-america": [1, 5],
  "north-atlantic": [1, 1], "central-atlantic": [2, 3], "south-atlantic": [3, 6],
  "united-kingdom": [2, 1], "western-europe": [3, 3], "north-sea": [3, 1],
  scandinavia: [4, 0], germany: [4, 2], italy: [4, 4], "baltic-sea": [5, 1],
  "eastern-europe": [5, 2], balkans: [5, 4], ukraine: [6, 3], moscow: [6, 1],
  russia: [7, 0], "black-sea": [6, 4], "north-africa": [4, 6], mediterranean: [4, 5],
  "south-africa": [4, 8], madagascar: [5, 8], "middle-east": [6, 5], "caspian-sea": [7, 3],
  "arabian-sea": [6, 7], "indian-ocean": [7, 8], kazakhstan: [8, 2], india: [8, 5],
  siberia: [9, 0], mongolia: [9, 2], "china-west": [9, 4], "china-east": [10, 3],
  vladivostok: [11, 1], japan: [12, 2], "east-china-sea": [11, 3], "iwo-jima": [12, 3],
  "southeast-asia": [9, 5], "south-china-sea": [10, 5], philippines: [11, 5],
  indonesia: [9, 6], "new-guinea": [11, 6], australia: [10, 8], "new-zealand": [12, 8],
  "north-pacific": [13, 1], hawaii: [13, 3], "central-pacific": [12, 5],
  "east-pacific": [14, 3], "southeast-pacific": [14, 6], "south-pacific": [13, 7],
};
// Percentage anchors from the frozen project's src/engine/map.ts.
const boardAnchors = {
  canada: [11, 18], "united-states": [11, 28], "latin-america": [19, 62],
  "united-kingdom": [30, 25], scandinavia: [36, 13], "western-europe": [31, 33],
  germany: [37, 29], italy: [36, 38], "eastern-europe": [39, 25], balkans: [40, 36],
  ukraine: [45, 29], moscow: [52, 20], russia: [47, 14], siberia: [63, 12],
  vladivostok: [79, 11], kazakhstan: [56, 31], mongolia: [65, 29],
  "china-west": [62, 40], "china-east": [71, 40], japan: [80, 32], "iwo-jima": [81, 46],
  "southeast-asia": [68, 53], philippines: [74, 56], indonesia: [71, 66],
  "new-guinea": [79, 66], india: [60, 49], "middle-east": [48, 45],
  "north-africa": [39, 52], "south-africa": [39, 62], madagascar: [46, 76],
  australia: [76, 78], "new-zealand": [84, 89], hawaii: [89, 43],
  "north-atlantic": [17, 40], "southeast-pacific": [10, 67], "central-atlantic": [29, 72],
  "south-atlantic": [30, 89], "north-sea": [26, 30], "baltic-sea": [38, 18],
  "black-sea": [44, 38], "caspian-sea": [51, 37], mediterranean: [42, 45],
  "arabian-sea": [54, 64], "indian-ocean": [59, 76], "south-china-sea": [72, 58],
  "east-china-sea": [79, 44], "north-pacific": [86, 27], "central-pacific": [85, 56],
  "east-pacific": [91, 59], "south-pacific": [84, 71],
};
const forceColors = {
  "soviet-union": "#c52f3d", china: "#934637", france: "#3265b8", "united-kingdom": "#777242",
  "united-states": "#23824b", italy: "#7f49a5", germany: "#222226", japan: "#f3e6c8",
};
const teamNames = {axis: "轴心", allies: "同盟", draw: "平局"};
const phaseNames = {Setup: "设置", Play: "出牌", Air: "空军", Supply: "补给", Victory: "计分", Discard: "弃牌", Draw: "摸牌"};
const unitNames = {ARMY: "陆军", NAVY: "海军", AIR: "空军"};
const zoneNames = {HAND: "手牌", DECK: "牌库", DISCARD: "弃牌", STATUS: "状态区", DISABLED_STATUS: "停用区",
  RESPONSE: "响应区", RESERVE_ARMY: "陆军预备", RESERVE_NAVY: "海军预备", RESERVE_AIR: "空军预备",
  ABILITY_READY: "可用区", ABILITY_PENDING: "待结算", OBJECT_POOL: "对象池",
  RESOLUTION: "结算区", SCORE_DORMANT: "计分休眠区", SET_ASIDE: "待命区"};
const commandNames = {END_PHASE: "结束阶段", PLAY_CARD: "出牌", DISCARD_CARD: "弃牌", FORFEIT_PLAY: "无牌可弃，扣 1 VP", CHOOSE: "选择", USE_STATUS: "发动状态牌",
  USE_AIR: "空军行动", REALLOCATE: "调配资源", ACTIVATE_CARD: "主动响应",
  BUILD_ARMY: "建立陆军", BUILD_NAVY: "建立海军", LAND_BATTLE: "陆战", SEA_BATTLE: "海战", RECRUIT: "征召"};
const eventNames = {...commandNames, TRANSFER: "对象转移", DRAW: "抓牌", DISCARD: "弃牌", SET_RESPONSE: "盖放响应",
  ABILITY_TRIGGERED: "响应触发", BEGIN_PHASE: "进入阶段", TURN_END: "回合结束", GAIN_VP: "获得 VP",
  SCORE_COUNTRY: "据点计分", ELIMINATE: "移除单位", OBJECT_SETTLED: "对象结算完成", ACTIVATE_ABILITY: "发动能力",
  CARD_EFFECT: "牌效果", BATTLE_DECLARED: "宣告战斗", COUNTRY_LOSS: "国家损失"};
const optionNames = {use: "使用能力", decline: "放弃", confirm: "确认", cancel: "取消", done: "完成",
  skip: "跳过", discard: "弃牌", air: "空军", unit: "地面／海军单位"};
const modeNames = {rules: "规则评分 A", search: "完整结算 B", plan: "沿用已评分的续步", forced: "唯一合法指令"};
const statusNames = {"deadline":"达到时限", "pending-choice":"待决选择", "response-timing":"按响应时机选择", "no-comparable-commands":"无可比较的行动",
  "matched-continuation":"续步吻合", "matched-exchange":"调配方案与合法选项吻合",
  "rules-only":"规则策略", "hand-retention":"手牌保留评分", "only-legal-command":"唯一合法指令", "finished":"搜索完成"};
const shortReason = reason => ({
  "Action cannot be represented from this observation": "席位观察无法表示",
  "Within-card choice budget exceeded; no partial score assigned": "超过结算分支上限",
})[reason] || statusNames[reason] || reason || "未完成";
const signed = n => (n > 0 ? "+" : "") + Number(n).toFixed(Number.isInteger(n) ? 0 : 2);
const exchangeDetails = plan => [
  "出牌 " + signed(plan.play_score), "弃牌 " + signed(plan.discard_score),
  ...(plan.deck_score === undefined ? [] : ["牌库 " + signed(plan.deck_score),
    "余陆 " + plan.land_remaining + "／海 " + plan.naval_remaining]),
].join(" · ");
const retentionDetails = value => [
  "保留分 " + signed(value.net), "短期 " + signed(value.short_term), "初始 " + signed(value.slot_cost),
  "基础储备 " + signed(value.basic_reserve), "重复 " + signed(value.redundancy),
  value.used_basic_cost ? "弃置成本 " + signed(-value.used_basic_cost) : "",
  value.forced ? "无牌可出，必须弃牌" : value.replacement_available ? "" : "无额外补牌",
  value.extra_draws ? "过牌 " + signed(value.draw_cycle) + "（手牌平均 " + signed(value.hand_average) + "）" : "",
  value.penalty_saved ? "免扣分 " + signed(value.penalty_saved) : "",
  value.discard_trigger ? "弃牌触发 " + signed(value.discard_trigger) : "",
  value.exhaustion ? "牌库耗尽 " + signed(value.exhaustion) : "",
].filter(Boolean).join(" · ");

function pieceIcon(kind, color, label, count = 1) {
  const piece = node("span", undefined, "piece");
  piece.title = label;
  piece.setAttribute("aria-label", label);
  piece.setAttribute("role", "img");
  const mold = node("span", undefined, "piece-mold");
  const shape = node("span", undefined, "piece-shape");
  shape.dataset.kind = kind.toLowerCase();
  shape.style.setProperty("--piece-color", color);
  mold.append(shape);
  piece.append(mold);
  if (count > 1) piece.append(node("small", "×" + count, "piece-count"));
  return piece;
}

async function loadBoardArt(image) {
  image.src = "../../board/map.png";
  try {
    await image.decode();
  } catch {
    return false;
  }
  image.hidden = false;
  return true;
}

async function mount(data) {
  const {frames, regions, tokens, forces, game, batch} = data;
  const atPlay = {edges: data.edges, straits: data.straits || []};
  let topology = data.board_reference || atPlay;
  const country = id => forces[id]?.name || id || "";
  const end = frames.length - 1;
  const map = $("map");
  const viewport = $("viewport");
  const artwork = $("board-art");
  const canvas = $("routes");
  const ctx = canvas.getContext("2d");
  const turnStart = [];
  const points = Object.create(null);
  const buttons = Object.create(null);
  let i = 0;
  let timer = null;
  let focus = null;
  let zoomFit = false;
  let zoom = 1;
  const worldMap = Object.keys(regions).every(key => Object.hasOwn(world, key));
  const artMap = Object.keys(regions).length === Object.keys(boardAnchors).length &&
    Object.keys(regions).every(key => Object.hasOwn(boardAnchors, key)) && await loadBoardArt(artwork);
  map.classList.toggle("printed-board", artMap);

  $("match-name").textContent = batch.name;
  $("match-info").textContent = [
    batch.ai_version, "种子 " + game.seed, "B 执" + teamNames[game.search_team],
    "预算 " + batch.settings.budget_ms + " ms", batch.ai_note,
  ].filter(Boolean).join(" · ");
  $("position").max = end;
  frames.forEach((frame, index) => {
    const previous = frames[index - 1];
    if (!previous || frame.turn.turn_id !== previous.turn.turn_id ||
        (previous.turn.phase === "Setup" && frame.turn.phase !== "Setup")) {
      turnStart.push(index);
      const turn = frame.turn;
      const label = frame.winner ? "终局" : turn.phase === "Setup" ? "开局设置" :
        "第 " + turn.round_id + " 轮 · " + country(data.turn_order[turn.country_index]);
      const option = node("option", label);
      option.value = index;
      $("turns").append(option);
    }
  });
  const regionKeys = Object.keys(regions);
  const dx = 106, dy = 56, marginX = 57, marginY = 32;
  const schematicPoints = regionKeys.map((key, index) => {
    const [col, line] = worldMap ? world[key] : [index % 5, Math.floor(index / 5)];
    return [marginX + col * dx, marginY + line * dy];
  });
  const width = artMap ? 1536 : Math.max(...schematicPoints.map(([x]) => x)) + marginX;
  const height = artMap ? Math.round(width * artwork.naturalHeight / artwork.naturalWidth) :
    Math.max(...schematicPoints.map(([, y]) => y)) + marginY;
  regionKeys.forEach((key, index) => {
    const [x, y] = artMap
      ? [width * boardAnchors[key][0] / 100, height * boardAnchors[key][1] / 100]
      : schematicPoints[index];
    points[key] = [x, y];
    const button = node("button", undefined, "position" + (regions[key].type === "SEA" ? " sea" : ""));
    button.dataset.region = key;
    button.style.left = x + "px";
    button.style.top = y + "px";
    button.addEventListener("click", () => {
      focus = focus === key ? null : key;
      paintMap(frames[i]);
    });
    buttons[key] = button;
    $("regions").append(button);
  });
  const ratio = window.devicePixelRatio || 1;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  canvas.style.width = map.style.width = width + "px";
  canvas.style.height = map.style.height = height + "px";
  ctx.scale(ratio, ratio);

  function fit() {
    map.style.zoom = zoomFit ? Math.min(1, viewport.clientWidth / width) : zoom;
  }
  function pan(key) {
    zoomFit = !key;
    zoom = 1;
    fit();
    viewport.scrollLeft = key ? Math.max(0, points[key][0] - viewport.clientWidth / 2) : 0;
    viewport.scrollTop = key ? Math.max(0, points[key][1] - viewport.clientHeight / 2) : 0;
  }
  function changeZoom(step) {
    const current = Number(map.style.zoom) || 1;
    const centerX = (viewport.scrollLeft + viewport.clientWidth / 2) / current;
    const centerY = (viewport.scrollTop + viewport.clientHeight / 2) / current;
    zoom = Math.min(2, Math.max(Math.min(1, viewport.clientWidth / width), current + step));
    zoomFit = false;
    fit();
    viewport.scrollLeft = centerX * zoom - viewport.clientWidth / 2;
    viewport.scrollTop = centerY * zoom - viewport.clientHeight / 2;
  }
  const theaters = [["总览", null]];
  if (worldMap) theaters.push(...[["大西洋", "central-atlantic"], ["欧洲与非洲", "germany"],
    ["亚洲", "china-west"], ["太平洋", "central-pacific"]].filter(([, key]) => key in points));
  else theaters.push(["原始大小", regionKeys[0]]);
  for (const [name, key] of theaters) {
    const button = node("button", name);
    button.addEventListener("click", () => pan(key));
    $("theaters").append(button);
  }
  for (const [name, step] of [["缩小", -0.2], ["放大", 0.2]]) {
    const button = node("button", name);
    button.addEventListener("click", () => changeZoom(step));
    $("theaters").append(button);
  }
  if (data.board_reference) {
    const label = node("label", "邻接 ", "topology-picker");
    const select = node("select");
    select.setAttribute("aria-label", "地图邻接规则");
    const checked = node("option", "校对后棋盘");
    checked.value = "checked";
    const historical = node("option", "当局规则");
    historical.value = "played";
    select.append(checked, historical);
    select.addEventListener("change", () => {
      topology = select.value === "played" ? atPlay : data.board_reference;
      paintMap(frames[i]);
    });
    label.append(select);
    $("theaters").append(label);
  }
  window.addEventListener("resize", fit);

  for (const [kind, label] of Object.entries(unitNames)) {
    const legend = node("span", undefined, "legend-item");
    legend.append(pieceIcon(kind, "#617469", label), node("span", label));
    $("unit-key").append(legend);
  }
  for (const [force, color] of Object.entries(forceColors)) {
    if (!Object.hasOwn(forces, force)) continue;
    const legend = node("span", undefined, "legend-item");
    legend.dataset.force = force;
    const swatch = node("span", undefined, "force-swatch");
    swatch.style.setProperty("--piece-color", color);
    legend.append(swatch, node("span", country(force)));
    $("force-key").append(legend);
  }

  const judge = () => $("reveal").checked;
  let activePile = null;
  const tokenName = key => {
    const t = tokens[key];
    if (!t) return optionNames[key] || regions[key]?.name || country(key);
    if (t.unit) return country(t.force) + " " + unitNames[t.unit];
    return tokens[t.source]?.name || t.name;
  };
  const concealed = key => {
    const t = tokens[key];
    if (!t) return false;
    const seat = frames[i].seats[forces[t.force]?.seat];
    const earlier = frames[Math.max(0, i - 1)].seats[forces[t.force]?.seat];
    return seat?.RESPONSE.includes(key) || seat?.RESPONSE.includes(t.source) ||
      earlier?.RESPONSE.includes(key) || earlier?.RESPONSE.includes(t.source);
  };
  const safeName = key => !judge() && concealed(key) ? "暗置响应" : tokenName(key);
  const zoneName = key => {
    if (!key) return "—";
    if (regions[key]) return regions[key].name;
    const [owner, area] = key.split(":");
    return area ? country(owner) + " " + (zoneNames[area] || area) : zoneNames[key] || key;
  };
  const commandLabel = (command, obscure) => {
    const label = commandNames[command.type] || command.type;
    if (command.type === "CHOOSE") return obscure ? "提交选项" : "选择 · " + safeName(command.option);
    if (command.card) {
      const name = obscure ? "暗置响应" : safeName(command.card);
      return label + " · " + name + (command.play_as ? " · " + (commandNames[command.play_as] || command.play_as) : "");
    }
    return label;
  };

  function paintMap(frame) {
    const moved = new Set();
    const tracks = [];
    for (const event of frame.events) {
      for (const [key, from, to] of event.moves) {
        if (!tokens[key]?.unit) continue;
        if (from in points) moved.add(from);
        if (to in points) moved.add(to);
        if (from in points && to in points) tracks.push([from, to]);
      }
    }
    ctx.clearRect(0, 0, width, height);
    const adjacent = new Set();
    const channels = topology.straits.filter(strait => strait.seas.includes(focus));
    function line(a, b, highlight, move, channel = false) {
      ctx.strokeStyle = channel ? "#fbe3a2" : move ? "#a36529" : highlight ? (artMap ? "#fff1c1" : "#506e62") : "#8ea9a1";
      ctx.globalAlpha = (move || highlight || channel) ? 1 : 0.60;
      ctx.lineWidth = move ? 2.7 : highlight ? 1.8 : 1;
      ctx.setLineDash(move || channel ? [5, 3] : []);
      ctx.beginPath();
      ctx.moveTo(...points[a]);
      ctx.lineTo(...points[b]);
      ctx.stroke();
    }
    for (const [a, b] of topology.edges) {
      const highlight = a === focus || b === focus;
      if (highlight) adjacent.add(a === focus ? b : a);
      if (highlight || (!artMap && Math.abs(points[a][0] - points[b][0]) <= dx * 4)) line(a, b, highlight, false);
    }
    for (const strait of channels) line(...strait.seas, true, false, true);
    for (const [a, b] of tracks) line(a, b, true, true);
    const selected = focus && regions[focus];
    for (const [key, region] of Object.entries(regions)) {
      const button = buttons[key];
      const units = (frame.board[key] || []).map(id => tokens[id]).filter(t => t?.unit);
      const groups = new Map();
      for (const unit of units) {
        const k = unit.force + "/" + unit.unit;
        if (!groups.has(k)) groups.set(k, {unit, count: 0});
        groups.get(k).count++;
      }
      button.classList.toggle("changed", moved.has(key));
      button.classList.toggle("selected", focus === key);
      button.classList.toggle("neighbor", adjacent.has(key));
      button.classList.toggle("occupied", !!groups.size);
      button.setAttribute("aria-pressed", focus === key);
      button.title = region.name + (units.length ? " · " + units.map(t => country(t.force) + unitNames[t.unit]).join("、") : " · 空区");
      button.setAttribute("aria-label", button.title);
      button.replaceChildren();
      if (!artMap) {
        button.append(node("span", region.name, "place-name"));
        if (region.vp) button.append(node("span", region.half_site ? "半分点" : region.vp + " VP", "vp"));
      } else if (!groups.size) {
        button.append(node("span", undefined, "board-pin"));
      }
      if (groups.size) {
        const badges = node("span", undefined, "badges");
        for (const {unit, count} of groups.values()) {
          const label = country(unit.force) + " " + unitNames[unit.unit] + " ×" + count;
          badges.append(pieceIcon(unit.unit, forceColors[unit.force] || "#647769", label, count));
        }
        button.append(badges);
      }
    }
    if (selected) {
      const occupants = (frame.board[focus] || []).filter(key => tokens[key]?.unit).map(tokenName);
      const neighbors = [...adjacent].map(key => regions[key].name);
      const passages = channels.map(strait => {
        const other = strait.seas.find(key => key !== focus);
        const axis = (frame.board[strait.land] || []).some(key => {
          const token = tokens[key];
          return token?.unit === "ARMY" && forces[token.force]?.team === "axis";
        });
        return regions[other].name + "（" + regions[strait.land].name + "，" + (axis ? "轴心" : "同盟") + "可通行）";
      });
      $("region-detail").textContent = [selected.name, occupants.join("、") || "无驻军",
        selected.half_site ? "半分点（每个部队 1 VP）" : selected.vp ? selected.vp + " VP" : "",
        "基础相邻 " + (neighbors.join("、") || "—"), passages.length ? "海峡 " + passages.join("、") : "",
      ].filter(Boolean).join(" · ");
    } else {
      $("region-detail").textContent = "点击地区查看驻军和基础连接。";
    }
  }

  function inspectCard(key) {
    const t = tokens[key];
    const card = tokens[t?.source] || t;
    $("card-name").textContent = tokenName(key);
    $("card-text").textContent = card?.text || "此对象没有单独的牌文。";
    $("card-dialog").showModal();
  }

  function cardButton(key, css, prefix) {
    const button = node("button", prefix + tokenName(key), "card " + css);
    button.addEventListener("click", () => inspectCard(key));
    return button;
  }

  function paintSeats(frame) {
    const active = data.turn_order[frame.turn.country_index];
    $("seats").replaceChildren();
    for (const id of data.turn_order) {
      const seat = frame.seats[id];
      const team = forces[id].team;
      const pane = node("article", undefined, "seat team-" + team + (active === id ? " active" : ""));
      const head = node("div", undefined, "seat-head");
      head.append(node("strong", country(id)), node("span", teamNames[team], "seat-side"),
        node("span", team === game.search_team ? "B" : "A", "seat-side"));
      pane.append(head);
      if (seat.piles) {
        const controls = node("div", undefined, "pile-controls");
        for (const [kind, label] of [["HAND", "手牌"], ["DISCARD", "弃牌堆"]]) {
          const button = node("button", label + " " + seat[kind]);
          button.setAttribute("aria-label", country(id) + label);
          button.setAttribute("aria-controls", "pile-view");
          button.setAttribute("aria-pressed", String(activePile?.id === id && activePile.kind === kind));
          button.addEventListener("click", () => {
            activePile = {id, kind};
            for (const other of $("seats").querySelectorAll("[aria-pressed]")) {
              other.setAttribute("aria-pressed", String(other === button));
            }
            paintPile(frames[i]);
            $("pile-view").scrollIntoView({block: "nearest"});
          });
          controls.append(button);
        }
        controls.append(node("small", "牌库 " + seat.DECK));
        pane.append(controls);
      } else {
        pane.append(node("small", "手牌 " + seat.HAND + " · 牌库 " + seat.DECK + " · 弃牌 " + seat.DISCARD));
      }
      const cards = node("div", undefined, "cards");
      for (const key of seat.STATUS) cards.append(cardButton(key, "status", ""));
      for (const key of seat.DISABLED_STATUS) cards.append(cardButton(key, "disabled", "停用 · "));
      const responses = seat.RESPONSE.filter(key => tokens[key]?.kind === "CARD");
      const listeners = seat.RESPONSE.filter(key => tokens[key]?.kind !== "CARD");
      if (judge()) {
        for (const key of responses) cards.append(cardButton(key, "response", "盖牌 · "));
        for (const key of listeners) cards.append(cardButton(key, "response", "监听 · "));
      } else {
        if (responses.length) cards.append(node("span", "盖牌 ×" + responses.length, "card-back"));
        if (listeners.length) cards.append(node("span", "监听 ×" + listeners.length, "card-back"));
      }
      if (!cards.childElementCount) cards.append(node("small", "场上无状态牌或响应"));
      pane.append(cards);
      $("seats").append(pane);
    }
  }

  function paintPile(frame) {
    const pile = $("pile-view");
    pile.hidden = !activePile;
    if (!activePile) return;
    const {id, kind} = activePile;
    const keys = frame.seats[id].piles[kind];
    const label = kind === "HAND" ? "手牌" : "弃牌堆";
    $("pile-title").textContent = country(id) + " · " + label + " · " + keys.length + " 张";
    const cards = $("pile-cards");
    cards.replaceChildren();
    if (kind === "HAND" && !judge()) {
      cards.append(node("small", "打开上方的「裁判视角」查看手牌内容。"));
    } else if (!keys.length) {
      cards.append(node("small", "此时没有牌。"));
    } else {
      for (const key of keys) cards.append(cardButton(key, "", ""));
    }
  }

  function paintAction(frame) {
    const area = $("action");
    area.replaceChildren();
    if (frame.decision) {
      const row = frame.decision;
      const setResponse = frame.events.some(e => e.moves.some(([key, , to]) =>
        tokens[key]?.kind === "CARD" && to?.endsWith(":RESPONSE")));
      const obscure = !judge() && (setResponse || row.command.type === "CHOOSE");
      area.append(node("p", country(row.country) + " · " + commandLabel(row.command, obscure), "decision-label"),
        node("p", [row.policy, modeNames[row.mode] || row.mode,
          row.elapsed_ms.toFixed(1) + " / " + row.budget_ms + " ms"].join(" · "), "action-meta muted"));
    } else {
      area.append(node("p", "开局 · 初始发牌", "muted"));
    }
    if (frame.waiting) area.append(node("p", "等待 " + country(frame.waiting.required_country) + " 选择", "action-meta"));
    if (frame.resolution.length) {
      area.append(node("p", "结算中 · " + frame.resolution.map(safeName).join(" → "), "action-meta"));
    }
  }

  function paintEvents(frame) {
    const area = $("events");
    area.replaceChildren();
    if (!frame.events.length) {
      area.append(node("p", "本步没有地图、场上牌区或 VP 变化。", "muted"));
      return;
    }
    const list = node("ul", undefined, "event-list");
    for (const event of frame.events) {
      const li = node("li");
      const eventName = event.reason === "no-discard" ? "弃牌阶段未弃牌" : eventNames[event.type] || event.type;
      li.append(node("strong", [country(event.actor), eventName, event.ok ? "" : "未生效"].filter(Boolean).join(" · ")));
      if (event.site?.startsWith("hidden:")) {
        const [, source, , , region] = event.site.split(":");
        li.append(node("small", ["隐分点", safeName(source), regions[region]?.name].filter(Boolean).join(" · ")));
      }
      for (const [key, from, to] of event.moves) {
        li.append(node("small", [safeName(key), zoneName(from) + " → " + zoneName(to)].join(" · ")));
      }
      for (const score of event.scores) li.append(node("small", teamNames[score.team] + " " + signed(score.amount) + " VP"));
      list.append(li);
    }
    area.append(list);
  }

  function paintEvaluation(frame) {
    const area = $("evaluation");
    area.replaceChildren();
    const row = frame.decision;
    if (!row) {
      area.append(node("p", "走到第一条指令后显示评分。", "muted"));
      return;
    }
    if (!judge()) {
      area.append(node("p", "开启裁判视角可查看候选牌、评分和回退原因。", "muted"));
      return;
    }
    area.append(node("p", statusNames[row.search_status] || row.search_status, "muted"));
    if (row.selected_q != null) area.append(node("p", "采用评分 " + signed(row.selected_q) + " 等效 VP", "action-score"));
    const picked = c => Object.keys(row.command).every(k => c[k] === row.command[k]) &&
      Object.keys(c).length === Object.keys(row.command).length;
    const list = node("ul", undefined, "candidate-list");
    for (const c of row.candidate_scores) {
      const li = node("li", undefined, picked(c.command) ? "picked" : "");
      li.append(node("strong", commandLabel(c.command, false) + (picked(c.command) ? " ✓" : "")));
      if (c.complete) {
        const vp = "实得 " + signed(c.actual_vp_delta) + " VP" +
          (c.vp_score_delta != null && c.vp_score_delta !== c.actual_vp_delta ? "（折 " + signed(c.vp_score_delta) + " 分）" : "");
        li.append(node("small", "总分 " + signed(c.q) + (c.terminal_rank === 2 ? " · 获胜" : c.terminal_rank === -2 ? " · 落败" : "")),
          node("small", [vp, "地图 " + signed(c.map_delta),
            "牌 " + signed(c.card_delta), "资源 " + signed(c.resource_delta)].join(" · ")));
        if (c.unit_removal_score) li.append(node("small", "地图分中移除单位 " + signed(c.unit_removal_score)));
        if (c.temporary_vp) li.append(node("small", "地图分中临时占领实得 " + signed(c.temporary_vp) + " VP"));
        if (c.expansion_score) li.append(node("small", "地图分中扩张偏好 " + signed(c.expansion_score)
          + " · 可继续争取 " + c.expansion_sites.length + " 个分点"));
        if (c.card_event_score) li.append(node("small", "牌分中返牌与牌库顶操作 " + signed(c.card_event_score)));
      } else {
        li.append(node("small", shortReason(c.reason)));
      }
      if (c.reallocation) li.append(node("small", exchangeDetails(c.reallocation)));
      if (c.initial_card_score) li.append(node("small", "初始出牌分 " + signed(c.initial_card_score)));
      list.append(li);
    }
    if (list.childElementCount) area.append(list);
    const details = node("details");
    if (!list.childElementCount) details.open = true;
    const rules = node("ul", undefined, "candidate-list");
    for (const c of row.rule_scores) {
      const li = node("li", commandLabel(c.command, false) + " · " + signed(c.rule_score) + (picked(c.command) ? " ✓" : ""),
        picked(c.command) ? "picked" : "");
      if (c.reallocation) li.append(node("small", exchangeDetails(c.reallocation)));
      if (c.retention) li.append(node("small", retentionDetails(c.retention)));
      if (c.expansion) li.append(node("small", "剩余建造牌可达 " + c.expansion.sites.length
        + " 个分点 · 扩张偏好 " + signed(c.expansion.score)));
      if (c.recruitment) {
        const r = c.recruitment;
        li.append(node("small", r.complete ? "征召结算 · 地图 " + signed(r.map_delta) + " · 实得 " +
          signed(r.actual_vp_delta + (r.temporary_vp || 0)) + " VP" : "征召未确认 · " + shortReason(r.reason)));
      }
      if (c.response_forecast) {
        const r = c.response_forecast;
        if (r.immediate != null) {
          li.append(node("small", "现在发动 " + signed(r.immediate)));
          if (r.wait) li.append(node("small", "等待 " + r.wait.wait_turns + " 回合 · 联动 "
            + safeName(r.wait.card) + " → " + (regions[r.wait.target]?.name || r.wait.target)
            + " · 基础 " + signed(r.wait.base) + " · 延迟 " + signed(-r.wait.time_cost)
            + " · 净值 " + signed(r.wait.net)));
        } else {
          li.append(node("small", "盖置基础 " + signed(r.base) + " · 等待 " + r.wait_turns
            + " 回合 · 延迟 " + signed(-r.time_cost) + (r.window_remaining ? "" : " · 已无后续窗口")));
        }
      }
      if (c.status_forecast) {
        const s = c.status_forecast;
        li.append(node("small", "状态预估 · " + s.windows + " 个席位回合 · 折算 " + s.effective.toFixed(2)
          + " 次机会 · 毛收益 " + signed(s.gross) + " · 同时机重合 " + signed(s.competition) + " · 净值 " + signed(s.future)));
        const timings = {"during-victory": "计分时", "after-build": "建设后", "battle-space": "战斗后",
          "before-battle-removal": "受攻击时", "unit-activity": "部队行动时", "economic-play": "经济战时",
          "discard-stage-card": "弃指定牌时", "play-army-card": "打出建陆牌时", "turn-start": "回合开始",
          "play-phase-start": "出牌阶段开始", "discard-start": "弃牌阶段开始", USE_STATUS: "代替常规出牌",
          modifier: "持续规则"};
        const parts = Object.entries(s.by_trigger || {}).map(([kind, gain]) => (timings[kind] || kind) + " " + signed(gain));
        if (parts.length) li.append(node("small", parts.join(" · ")));
      }
      if (c.initial_card_score) li.append(node("small", "初始出牌分 " + signed(c.initial_card_score)));
      rules.append(li);
    }
    details.append(node("summary", "规则排序（" + row.rule_scores.length + " 项）"), rules);
    area.append(details);
  }

  function render() {
    const frame = frames[i];
    const turn = frame.turn;
    $("position").value = i;
    $("step").textContent = i + " / " + end;
    $("turns").value = turnStart.filter(start => start <= i).at(-1);
    $("first").disabled = $("prev").disabled = i === 0;
    $("last").disabled = $("next").disabled = i === end;
    $("axis").textContent = frame.scores.axis;
    $("allies").textContent = frame.scores.allies;
    $("clock").textContent = frame.winner ? (frame.winner === "draw" ? "终局 · 平局" : "终局 · " + teamNames[frame.winner] + "获胜") :
      ["第 " + turn.round_id + " 轮", country(data.turn_order[turn.country_index]), phaseNames[turn.phase] || turn.phase].join(" · ");
    paintMap(frame);
    paintSeats(frame);
    paintPile(frame);
    paintAction(frame);
    paintEvents(frame);
    paintEvaluation(frame);
  }

  function pause() {
    clearInterval(timer);
    timer = null;
    $("play").textContent = "▶ 播放";
  }

  function jump(target) {
    pause();
    i = Math.min(end, Math.max(0, target));
    render();
  }

  function play() {
    if (timer) {
      pause();
      return;
    }
    if (end === 0) return;
    if (i === end) i = 0;
    $("play").textContent = "Ⅱ 暂停";
    render();
    timer = setInterval(() => {
      i++;
      if (i === end) pause();
      render();
    }, 800 / Number($("speed").value));
  }

  $("first").addEventListener("click", () => jump(0));
  $("last").addEventListener("click", () => jump(end));
  $("prev").addEventListener("click", () => jump(i - 1));
  $("next").addEventListener("click", () => jump(i + 1));
  $("play").addEventListener("click", play);
  $("speed").addEventListener("change", () => {
    if (timer) {
      pause();
      play();
    }
  });
  $("turns").addEventListener("change", event => jump(Number(event.target.value)));
  $("position").addEventListener("input", event => jump(Number(event.target.value)));
  $("reveal").addEventListener("change", render);
  $("pile-close").addEventListener("click", () => {
    const selected = $("seats").querySelector('[aria-pressed="true"]');
    activePile = null;
    selected?.setAttribute("aria-pressed", "false");
    paintPile(frames[i]);
    selected?.focus();
  });
  document.addEventListener("keydown", event => {
    if (event.target.matches("input,select,button,a") || $("card-dialog").open) return;
    if (event.key === "ArrowLeft") jump(i - 1);
    else if (event.key === "ArrowRight") jump(i + 1);
    else if (event.code === "Space") play();
    else return;
    event.preventDefault();
  });
  render();
  pan(!artMap && worldMap && points.germany ? "germany" : null);
}

const picker = $("batch-select");
if (picker) {
  const selectBatch = () => document.querySelectorAll(".batch").forEach(panel => {
    panel.hidden = panel.id !== "batch-" + picker.value;
  });
  picker.addEventListener("change", selectBatch);
  selectBatch();
}
if ($("replay-data")) mount(JSON.parse($("replay-data").textContent));
