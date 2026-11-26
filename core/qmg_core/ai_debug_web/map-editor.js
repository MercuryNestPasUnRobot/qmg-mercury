"use strict";

(() => {
  const editor = $("map-editor");
  const selector = $("match-map");
  const linkForm = $("map-link-form");
  const straitForm = $("map-strait-form");
  const image = $("map-editor-art");
  const canvas = $("map-editor-lines");
  const usesNames = {build: "建设", battle: "战斗", supply: "补给"};
  let draft, region, editingStrait;
  let loading = false;
  let saving = false;

  const sameLink = (a, b, c, d) => (a === c && b === d) || (a === d && b === c);

  function notice(text, error = false) {
    const box = $("map-editor-message");
    box.textContent = text;
    box.dataset.error = String(error);
    box.hidden = !text;
  }

  async function loadMaps(selected) {
    const rows = await api("/api/maps");
    const choice = app.live?.run.map?.id || selected || selector.value;
    setOptions(selector, rows.map(row => [row.id, row.name]), choice, "");
    app.mapsReady = true;
    renderControls();
  }

  function draw() {
    if (!draft) return;
    const regions = Object.entries(draft.regions);
    const printed = image.naturalWidth > 0 && regions.every(([id]) => Object.hasOwn(boardAnchors, id));
    const columns = Math.max(1, Math.ceil(Math.sqrt(regions.length * 1.6)));
    const rows = Math.ceil(regions.length / columns);
    image.hidden = !printed;
    const coords = new Map(regions.map(([id], index) => [id, printed ? boardAnchors[id] :
      [(index % columns + .5) * 100 / columns, (Math.floor(index / columns) + .5) * 100 / rows]]));
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    function line(a, b, color, width, dash = []) {
      const [x1, y1] = coords.get(a), [x2, y2] = coords.get(b);
      ctx.beginPath();
      ctx.moveTo(x1 * canvas.width / 100, y1 * canvas.height / 100);
      ctx.lineTo(x2 * canvas.width / 100, y2 * canvas.height / 100);
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      ctx.setLineDash(dash);
      ctx.stroke();
    }

    const incident = link => link.a === region || link.b === region;
    for (const {a, b} of draft.layout.connections.filter(link => !incident(link))) line(a, b, "#263a5266", 1.6);
    for (const {a, b} of draft.layout.connections.filter(incident)) line(a, b, "#174a70", 4);
    for (const {land, seas: [a, b]} of draft.layout.straits) {
      line(a, b, "#a65d12", a === region || b === region || land === region ? 4 : 2.4, [9, 6]);
    }

    $("map-editor-regions").replaceChildren(...regions.map(([id, place]) => {
      const [x, y] = coords.get(id);
      const vp = draft.layout.points[id] || 0;
      const marker = node("button", vp === 2 ? "●" : vp === 1 ? "◐" : "○", "map-marker");
      marker.type = "button";
      marker.style.left = `${x}%`;
      marker.style.top = `${y}%`;
      marker.title = `${place.name} · ${vp === 2 ? "全分点" : vp === 1 ? "半分点" : "普通地区"}`;
      marker.setAttribute("aria-label", marker.title);
      marker.setAttribute("aria-pressed", String(id === region));
      marker.addEventListener("click", () => selectRegion(id));
      return marker;
    }));
  }

  function rowAction(text, label, action) {
    const button = node("button", text, "text-button");
    button.type = "button";
    button.setAttribute("aria-label", label);
    button.addEventListener("click", action);
    return button;
  }

  function optionsFor(select, value, filter = () => true) {
    const choices = Object.entries(draft.regions).filter(filter).map(([id, place]) => [id, place.name]);
    if (select !== "map-region") choices.unshift(["", "请选择"]);
    setOptions($(select), choices, value, "请选择");
  }

  function fillLink() {
    const target = $("map-link-destination").value;
    const existing = draft.layout.connections.find(({a, b}) => sameLink(a, b, region, target));
    for (const check of linkForm.querySelectorAll("input")) check.checked = !existing || existing.uses.includes(check.value);
  }

  function selectRegion(id) {
    region = id;
    $("map-region").value = id;
    $("map-points").value = draft.layout.points[id] || 0;
    optionsFor("map-link-destination", $("map-link-destination").value, ([key]) => key !== id);
    renderLinks();
    draw();
  }

  function renderLinks() {
    const links = draft.layout.connections.filter(({a, b}) => a === region || b === region);
    $("map-neighbors").replaceChildren(...links.map(link => {
      const target = link.a === region ? link.b : link.a;
      const place = draft.regions[target].name;
      const row = node("li");
      row.append(node("span", `${place} · ${link.uses.map(use => usesNames[use]).join("／")}`),
        rowAction("编辑", `编辑连接到${place}`, () => { $("map-link-destination").value = target; fillLink(); }),
        rowAction("移除", `移除连接到${place}`, () => {
          draft.layout.connections = draft.layout.connections.filter(item => item !== link);
          renderLinks();
          draw();
        }));
      return row;
    }));
    if (!links.length) $("map-neighbors").append(node("li", "这个地区没有普通连接。", "muted"));
    fillLink();
  }

  function fillStrait(strait) {
    editingStrait = strait;
    if (strait) {
      $("map-strait-land").value = strait.land;
      [$("map-strait-first").value, $("map-strait-second").value] = strait.seas;
    } else straitForm.reset();
    $("map-strait-apply").textContent = strait ? "更新海峡" : "添加海峡";
  }

  function renderStraits() {
    const name = id => draft.regions[id].name;
    $("map-straits").replaceChildren(...draft.layout.straits.map(strait => {
      const row = node("li");
      const label = strait.seas.map(name).join(" ↔ ");
      row.append(node("span", `${label} · ${name(strait.land)}`),
        rowAction("编辑", `编辑海峡 ${label}`, () => fillStrait(strait)),
        rowAction("移除", `移除海峡 ${label}`, () => {
          draft.layout.straits = draft.layout.straits.filter(item => item !== strait);
          if (editingStrait === strait) fillStrait(null);
          renderStraits();
          draw();
        }));
      return row;
    }));
  }

  async function openEditor() {
    if (loading) return;
    loading = true;
    try {
      draft = await api(`/api/maps/${encodeURIComponent(selector.value)}`);
      const first = Object.keys(draft.regions)[0];
      optionsFor("map-region", first);
      optionsFor("map-strait-land", "", ([, place]) => place.type === "LAND");
      for (const id of ["map-strait-first", "map-strait-second"]) optionsFor(id, "", ([, place]) => place.type === "SEA");
      $("map-source").textContent = `基于 ${draft.name} · 保存为新地图`;
      $("map-name").value = "";
      notice("");
      selectRegion(first);
      fillStrait(null);
      renderStraits();
      editor.showModal();
    } catch (error) {
      message(error.message, true);
    } finally {
      loading = false;
    }
  }

  $("edit-map").addEventListener("click", openEditor);
  image.addEventListener("load", draw);
  $("map-region").addEventListener("change", event => selectRegion(event.target.value));
  $("map-link-destination").addEventListener("change", fillLink);
  $("map-points").addEventListener("change", event => { draft.layout.points[region] = Number(event.target.value); draw(); });
  linkForm.addEventListener("submit", event => {
    event.preventDefault();
    const destination = $("map-link-destination").value;
    const uses = new FormData(linkForm).getAll("uses");
    if (!uses.length) return notice("请至少选择一种用途，或在连接列表中移除此连接。", true);
    if (draft.layout.straits.some(({seas: [a, b]}) => sameLink(a, b, region, destination))) {
      return notice("这两个海域已有海峡。若要改成普通连接，请先移除海峡。", true);
    }
    const existing = draft.layout.connections.find(({a, b}) => sameLink(a, b, region, destination));
    if (existing) existing.uses = uses;
    else draft.layout.connections.push({a: region, b: destination, uses});
    notice("连接已更新。");
    renderLinks();
    draw();
  });

  straitForm.addEventListener("submit", event => {
    event.preventDefault();
    const first = $("map-strait-first").value, second = $("map-strait-second").value;
    if (first === second) return notice("请选择两个不同的海域。", true);
    if (draft.layout.straits.some(item => item !== editingStrait && sameLink(...item.seas, first, second))) {
      return notice("这两个海域已有海峡。", true);
    }
    const row = {land: $("map-strait-land").value, seas: [first, second]};
    if (editingStrait) Object.assign(editingStrait, row);
    else draft.layout.straits.push(row);
    draft.layout.connections = draft.layout.connections.filter(({a, b}) => !sameLink(a, b, first, second));
    fillStrait(null);
    renderStraits();
    renderLinks();
    draw();
    notice("海峡已更新。");
  });

  $("map-save-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (saving) return;
    saving = $("map-save").disabled = true;
    try {
      const map = await api("/api/maps", {name: $("map-name").value, layout: draft.layout});
      await loadMaps(map.id);
      editor.close();
      message(`已保存地图「${map.name}」。`);
    } catch (error) {
      notice(error.message, true);
    } finally {
      saving = $("map-save").disabled = false;
    }
  });

  loadMaps().catch(error => message(error.message, true));
})();
