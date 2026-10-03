/* 事故复盘时间线 —— 前端（无框架，原生 JS）
 *
 * 数据流：左侧表单只编辑「原始输入 payload」；每次变更防抖调用
 * /api/compute/preview 得到「推算结果 result」并渲染右侧。保存时把
 * payload 连同所基于的 base_version 一起提交；409 表示本地版本过期。
 */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  retros: [],
  retro: null,          // 复盘元信息
  version: null,        // 服务器上的最新版本 {version, payload, result,...}
  payload: { events: [], relations: [] }, // 正在编辑的原始输入
  dirty: false,
  preview: null,        // 当前编辑内容对应的推算结果
  previewTimer: null,
  readChains: new Set(),
};

// --------------------------------------------------------------------------- //
// 小工具
// --------------------------------------------------------------------------- //

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtTime(s) { return s ? String(s).replace("T", " ") : "—"; }
function uid() {
  return "e" + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
}
function toast(msg, ms = 2600) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.hidden = true; }, ms);
}
async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(url, opt);
  let data = null;
  try { data = await res.json(); } catch (_) { /* 非 JSON */ }
  if (!res.ok) {
    const err = new Error((data && data.error) || `${method} ${url} → ${res.status}`);
    err.status = res.status;
    err.code = data && data.code;
    throw err;
  }
  return data;
}
function eventTitle(id) {
  const ev = state.payload.events.find((e) => e.id === id);
  return ev && ev.title ? ev.title : id;
}
function normEvent(ev) {
  return {
    id: ev.id,
    title: ev.title || "",
    start_min: ev.start_min || null,
    start_max: ev.start_max || null,
    duration_minutes: ev.duration_minutes === "" || ev.duration_minutes == null
      ? null : Number(ev.duration_minutes),
    note: ev.note || "",
  };
}
function cleanPayload() {
  return {
    events: state.payload.events.map(normEvent),
    relations: state.payload.relations
      .filter((r) => r.before && r.after && r.before !== r.after)
      .map((r) => ({ before: r.before, after: r.after, note: r.note || "" })),
  };
}

// --------------------------------------------------------------------------- //
// 复盘列表
// --------------------------------------------------------------------------- //

async function loadRetros(selectId) {
  state.retros = (await api("GET", "/api/retros")).retros;
  renderRetroList();
  if (selectId) selectRetro(selectId);
  else if (state.retros.length && !state.retro) selectRetro(state.retros[0].id);
}

function statusDot(s) {
  return s === "conflict" ? "dot-conflict" : s === "draft" ? "dot-draft" : "dot-ok";
}

function renderRetroList() {
  const ul = $("#retro-list");
  ul.innerHTML = "";
  for (const r of state.retros) {
    const li = document.createElement("li");
    if (state.retro && r.id === state.retro.id) li.classList.add("active");
    li.innerHTML = `
      <div class="retro-item-name">
        <span class="dot ${statusDot(r.statusHint || "ok")}"></span>
        <span>${esc(r.name)}</span>
        ${r.is_final ? '<span class="badge badge-final" style="font-size:10px">定稿</span>' : ""}
      </div>
      <div class="retro-item-meta">v${r.current_version} · 更新 ${fmtTime(r.updated_at)}</div>`;
    li.onclick = () => selectRetro(r.id);
    ul.appendChild(li);
  }
}

async function selectRetro(id) {
  const data = await api("GET", `/api/retros/${id}`);
  state.retro = data.retro;
  state.version = data.version;
  state.payload = data.version
    ? JSON.parse(JSON.stringify(data.version.payload))
    : { events: [], relations: [] };
  state.dirty = false;
  state.preview = data.version ? data.version.result : null;
  $("#editor").hidden = false;
  $("#results").hidden = false;
  renderAll();
  renderRetroList();
}

// --------------------------------------------------------------------------- //
// 编辑器
// --------------------------------------------------------------------------- //

function renderAll() {
  $("#retro-name").value = state.retro.name;
  const badge = $("#version-badge");
  badge.textContent = state.dirty
    ? `基于 v${state.version.version} · 有未保存修改`
    : `v${state.version.version}${state.version.author ? " · " + esc(state.version.author) : ""}`;
  badge.className = "badge" + (state.dirty ? " badge-dirty" : "");
  $("#final-badge").hidden = !state.retro.is_final;
  $("#saved-at").textContent = "保存于 " + fmtTime(state.version.created_at);
  renderEventsTable();
  renderRelationsTable();
  renderResults();
}

function renderEventsTable() {
  const tbody = $("#events-tbody");
  tbody.innerHTML = "";
  const resultById = (state.preview && state.preview.events) || {};

  state.payload.events.forEach((ev, idx) => {
    const r = resultById[ev.id] || {};
    const tr = document.createElement("tr");
    if (r.input_conflict || r.conflict_indexes?.length) tr.classList.add("row-conflict");
    else if (r.draft) tr.classList.add("row-draft");

    tr.innerHTML = `
      <td><input class="input" data-k="title" value="${esc(ev.title)}" placeholder="事件名称"></td>
      <td><input type="datetime-local" step="60" data-k="start_min" value="${esc(ev.start_min || "")}"></td>
      <td><input type="datetime-local" step="60" data-k="start_max" value="${esc(ev.start_max || "")}"></td>
      <td><input type="number" min="0" step="1" data-k="duration_minutes"
                 value="${ev.duration_minutes ?? ""}" placeholder="分钟"></td>
      <td>${renderEventState(r)}</td>
      <td><button class="icon-btn" title="删除事件">✕</button></td>`;

    tr.querySelectorAll("input").forEach((inp) => {
      inp.addEventListener("change", () => {
        ev[inp.dataset.k] = inp.value;
        markDirty();
        if (inp.dataset.k === "title") renderRelationsTable();
      });
    });
    tr.querySelector(".icon-btn").onclick = () => {
      state.payload.events.splice(idx, 1);
      state.payload.relations = state.payload.relations.filter(
        (x) => x.before !== ev.id && x.after !== ev.id);
      markDirty();
      renderEventsTable();
      renderRelationsTable();
    };
    tr.dataset.evId = ev.id;
    tbody.appendChild(tr);
  });
}

function renderEventState(r) {
  if (!r || !("draft" in r)) return '<span class="muted">—</span>';
  if (r.input_conflict)
    return '<span class="state-pill state-conflict" title="最早开始晚于最晚开始">输入冲突</span>';
  if (r.conflict_indexes && r.conflict_indexes.length)
    return '<span class="state-pill state-conflict">冲突</span>';
  if (r.draft)
    return `<span class="state-pill state-skipped" title="${esc(r.reason)}">暂存草稿</span>`;
  return '<span class="state-pill state-ok">可推算</span>';
}

function renderRelationsTable() {
  const tbody = $("#relations-tbody");
  tbody.innerHTML = "";
  const relResult = new Map();
  (state.preview?.relations || []).forEach((r) =>
    relResult.set(`${r.before}|${r.after}`, r));
  const opts = (sel) => state.payload.events
    .map((e) => `<option value="${esc(e.id)}" ${e.id === sel ? "selected" : ""}>${esc(eventTitle(e.id))}</option>`)
    .join("");

  state.payload.relations.forEach((rel, idx) => {
    const r = relResult.get(`${rel.before}|${rel.after}`);
    const tr = document.createElement("tr");
    if (r && (r.status === "conflict" || r.status === "cycle")) tr.classList.add("row-conflict");
    tr.innerHTML = `
      <td><select class="input" data-k="before"><option value="">请选择…</option>${opts(rel.before)}</select></td>
      <td class="muted" style="text-align:center">先于 →</td>
      <td><select class="input" data-k="after"><option value="">请选择…</option>${opts(rel.after)}</select></td>
      <td>${renderRelState(r)}</td>
      <td><button class="icon-btn">✕</button></td>`;

    tr.querySelectorAll("select").forEach((sel) => {
      sel.addEventListener("change", () => {
        rel[sel.dataset.k] = sel.value;
        markDirty();
      });
    });
    tr.querySelector(".icon-btn").onclick = () => {
      state.payload.relations.splice(idx, 1);
      markDirty();
      renderRelationsTable();
    };
    tbody.appendChild(tr);
  });
}

function renderRelState(r) {
  if (!r) return '<span class="muted">—</span>';
  const map = {
    ok: ['state-ok', '参与推算'],
    skipped: ['state-skipped', '暂不参与'],
    conflict: ['state-conflict', '冲突'],
    cycle: ['state-cycle', '成环'],
  };
  const [cls, label] = map[r.status] || map.ok;
  return `<span class="state-pill ${cls}" title="${esc(r.reason || "前一事件结束 ≤ 后一事件开始")}">${label}</span>`;
}

function markDirty() {
  state.dirty = true;
  renderResults();          // 先清掉旧结果标记（基于未保存输入）
  const badge = $("#version-badge");
  badge.textContent = `基于 v${state.version.version} · 有未保存修改`;
  badge.className = "badge badge-dirty";
  renderEventsStateBadges();
  schedulePreview();
}

function renderEventsStateBadges() {
  // 输入变更后只更新状态列，避免重建表格导致输入焦点丢失
  $$("#events-tbody tr").forEach((tr) => {
    const r = state.preview?.events?.[tr.dataset.evId];
    if (r) {
      tr.classList.toggle("row-conflict",
        r.input_conflict || (r.conflict_indexes && r.conflict_indexes.length > 0));
      tr.classList.toggle("row-draft", r.draft);
      tr.querySelector("td:nth-child(5)").innerHTML = renderEventState(r);
    }
  });
  $$("#relations-tbody tr").forEach((tr) => {
    // 关系状态在预览返回后整体刷新（轻量）
  });
}

// --------------------------------------------------------------------------- //
// 推算结果：横幅 / 甘特图 / 事件卡片
// --------------------------------------------------------------------------- //

function renderResults() {
  const r = state.preview;
  renderBanners(r);
  renderGantt(r);
  renderEventCards(r);
  renderRelationsStatusOnly();
}

function renderBanners(r) {
  const cb = $("#conflict-banner");
  const db = $("#draft-banner");
  if (!r) { cb.hidden = db.hidden = true; return; }

  if (r.conflicts.length) {
    const items = r.conflicts.map((c, i) => {
              const anchor = c.events[0] ? `data-jump="${esc(c.events[0])}"` : "";
              return `<li><a ${anchor}>冲突 ${i + 1}</a>：${esc(c.message)}</li>`;
            }).join("");
    cb.innerHTML = `<div class="banner-title">⛔ 存在 ${r.conflicts.length} 处约束冲突——此版本<strong>不能定稿</strong>，输入已全部保留：</div><ul>${items}</ul>`;
    cb.hidden = false;
    cb.querySelectorAll("a[data-jump]").forEach((a) => {
      a.onclick = () => {
        const card = document.querySelector(`.ecard[data-ev-id="${a.dataset.jump}"]`);
        if (card) card.scrollIntoView({ behavior: "smooth", block: "center" });
      };
    });
  } else {
    cb.hidden = true;
  }

  const drafts = Object.values(r.events).filter((e) => e.draft);
  if (drafts.length) {
    const names = drafts.map((d) => `「${esc(eventTitle(d.id))}」`).join("、");
    db.hidden = false;
    db.innerHTML = `<div class="banner-title">⏳ ${drafts.length} 个事件暂存（${names}）</div>
      缺少完整的时间范围或持续时长；其关联的「先于」关系暂不参与推算，原因显示在关系行与事件卡片中。`;
  } else {
    db.hidden = true;
  }
}

function parseT(s) { return new Date(s.replace(" ", "T")); }

function renderGantt(r) {
  const wrap = $("#gantt-wrap");
  if (!r || !state.payload.events.length) {
    wrap.innerHTML = '<div class="muted small">添加事件后，这里显示可行时间窗概览。</div>';
    return;
  }
  const active = state.payload.events
    .map((e) => ({ e, res: r.events[e.id] }))
    .filter((x) => x.res && x.res.start_window);

  if (!active.length) {
    wrap.innerHTML = '<div class="muted small">还没有信息完整、可推算的事件。</div>';
    return;
  }

  let tMin = Infinity, tMax = -Infinity;
  active.forEach(({ res }) => {
    tMin = Math.min(tMin, parseT(res.start_window.min).getTime());
    tMax = Math.max(tMax, parseT(res.end_window.max).getTime());
  });
  if (tMax <= tMin) tMax = tMin + 60000;
  const span = tMax - tMin;
  const pct = (t) => ((t - tMin) / span) * 100;
  const tickLabel = (t, i, n) => {
    const d = new Date(t);
    const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
    return (i === 0 || i === n - 1)
      ? `${d.getMonth() + 1}/${d.getDate()} ${hm}` : hm;
  };
  const N = 7;
  const ticks = Array.from({ length: N }, (_, i) =>
    `<span>${tickLabel(tMin + (span * i) / (N - 1), i, N)}</span>`).join("");

  const rows = state.payload.events.map((e) => {
    const res = r.events[e.id];
    if (!res) return "";
    if (res.draft)
      return `<div class="gantt-row"><div class="gantt-label" title="${esc(e.title)}">${esc(eventTitle(e.id))}</div>
        <div class="gantt-track"><div class="gantt-bar bar-draft" style="left:2px;width:10px" title="暂存草稿"></div></div></div>`;
    if (!res.start_window)
      return `<div class="gantt-row"><div class="gantt-label">${esc(eventTitle(e.id))}</div>
        <div class="gantt-track"><div class="gantt-bar bar-conflict" style="left:2px;width:10px" title="输入冲突"></div></div></div>`;
    const x1 = pct(parseT(res.start_window.min).getTime());
    const x2 = pct(parseT(res.end_window.max).getTime());
    const fixed = res.start_window.min === res.start_window.max;
    const conflict = res.conflict_indexes && res.conflict_indexes.length;
    const w = Math.max(x2 - x1, 0.8);
    return `<div class="gantt-row"><div class="gantt-label" title="${esc(eventTitle(e.id))}">${esc(eventTitle(e.id))}</div>
      <div class="gantt-track"><div class="gantt-bar ${fixed ? "bar-fixed" : ""} ${conflict ? "bar-conflict" : ""}"
        style="left:${x1}%;width:${w}%"
        title="${esc(eventTitle(e.id))} 可行区间 ${fmtTime(res.start_window.min)} ~ ${fmtTime(res.end_window.max)}"></div></div></div>`;
  }).join("");

  wrap.innerHTML = `<div class="gantt">
    <div class="gantt-axis">${ticks}</div>${rows}
  </div>
  <div class="muted small" style="margin-top:4px">条纹=时间窗范围；实色=开始时刻已被约束完全确定；红色=存在冲突。</div>`;
}

function chainHtml(steps, conflict) {
  return `<ol class="chain ${conflict ? "chain-conflict" : ""}">
    ${steps.map((s) => `<li>${esc(s.text)}</li>`).join("")}</ol>`;
}

function renderEventCards(r) {
  const box = $("#event-cards");
  if (!r) { box.innerHTML = ""; return; }
  box.innerHTML = "";
  state.payload.events.forEach((ev) => {
    const res = r.events[ev.id];
    if (!res) return;
    const card = document.createElement("div");
    card.className = "ecard" +
      ((res.conflict_indexes && res.conflict_indexes.length) || res.input_conflict
        ? " is-conflict" : res.draft ? " is-draft" : "");
    card.dataset.evId = ev.id;

    let body;
    if (res.draft) {
      body = `<div class="ecard-reason">⏳ ${esc(res.reason)}</div>`;
    } else if (res.input_conflict) {
      body = `<div class="ecard-reason">⛔ ${esc(res.reason)}</div>`;
    } else {
      const w = res.start_window, e = res.end_window;
      body = `
        <div class="win-line">
          <span>开始窗口</span>
          <span class="win-box">${fmtTime(w.min)} ~ ${fmtTime(w.max)}</span>
        </div>
        <div class="win-line">
          <span>结束窗口</span>
          <span class="win-box end">${fmtTime(e.min)} ~ ${fmtTime(e.max)}</span>
        </div>
        <span class="chain-toggle" data-w="s">▸ 查看开始时间窗依据链</span>
        <div data-chain="s" hidden>
          <div class="muted small">最早开始（下界）：</div>${chainHtml(w.min_chain)}
          <div class="muted small">最晚开始（上界）：</div>${chainHtml(w.max_chain)}
        </div>
        <span class="chain-toggle" data-w="e">▸ 查看结束时间窗依据链</span>
        <div data-chain="e" hidden>
          <div class="muted small">最早结束：</div>${chainHtml(e.min_chain)}
          <div class="muted small">最晚结束：</div>${chainHtml(e.max_chain)}
        </div>`;
    }

    const conflictLinks = (res.conflict_indexes || [])
      .map((i) => `<a class="mini-jump" data-i="${i}">→ 冲突 ${i + 1}</a>`).join(" ");

    card.innerHTML = `
      <div class="ecard-head">
        <span class="ecard-title">${esc(eventTitle(ev.id))}</span>
        <span class="muted small">${conflictLinks}</span>
      </div>${body}
      ${ev.note ? `<div class="rel-note">📝 ${esc(ev.note)}</div>` : ""}`;

    card.querySelectorAll(".chain-toggle").forEach((t) => {
      t.onclick = () => {
        const panel = card.querySelector(`[data-chain="${t.dataset.w}"]`);
        panel.hidden = !panel.hidden;
        t.textContent = panel.hidden
          ? (t.dataset.w === "s" ? "▸ 查看开始时间窗依据链" : "▸ 查看结束时间窗依据链")
          : (t.dataset.w === "s" ? "▾ 收起开始时间窗依据链" : "▾ 收起结束时间窗依据链");
      };
    });
    card.querySelectorAll(".mini-jump").forEach((a) => {
      a.onclick = () => {
        const c = r.conflicts[Number(a.dataset.i)];
        showConflictModal(c, Number(a.dataset.i));
      };
    });
    box.appendChild(card);
  });
}

function renderRelationsStatusOnly() {
  if (!state.preview) return;
  const map = new Map();
  state.preview.relations.forEach((r) => map.set(`${r.before}|${r.after}`, r));
  $$("#relations-tbody tr").forEach((tr) => {
    const before = tr.querySelector("[data-k=before]")?.value;
    const after = tr.querySelector("[data-k=after]")?.value;
    const cell = tr.querySelector("td:nth-child(4)");
    const found = map.get(`${before}|${after}`);
    if (cell && found) {
      cell.innerHTML = renderRelState(found);
      tr.classList.toggle("row-conflict",
        found.status === "conflict" || found.status === "cycle");
    }
  });
}

function showConflictModal(c, i) {
  openModal(`
    <h3>⛔ 冲突 ${i + 1}：${esc({ input_range: "输入范围矛盾", relation: "先于约束不可满足",
      cycle: "关系成环" }[c.kind] || "约束冲突")}</h3>
    <p>${esc(c.message)}</p>
    <div class="muted small">依据链：</div>
    ${chainHtml(c.chain, true)}
    <div class="modal-foot"><button class="btn btn-primary" id="modal-close">知道了</button></div>
  `);
  $("#modal-close").onclick = closeModal;
}

// --------------------------------------------------------------------------- //
// 预览 / 保存 / 定稿
// --------------------------------------------------------------------------- //

function schedulePreview() {
  clearTimeout(state.previewTimer);
  state.previewTimer = setTimeout(doPreview, 450);
}

async function doPreview() {
  try {
    const data = await api("POST", "/api/compute/preview", { payload: cleanPayload() });
    // 切换复盘后旧请求结果作废
    state.preview = data.result;
    renderResults();
    renderEventsStateBadges();
  } catch (err) {
    // 输入尚在编辑（如数字框中间态）——静默，保存时再硬校验
    console.warn("preview failed:", err.message);
  }
}

async function onSave() {
  if (!state.retro) return;
  const btn = $("#btn-save");
  btn.disabled = true;
  try {
    const data = await api("PUT", `/api/retros/${state.retro.id}/versions`, {
      base_version: state.version.version,
      payload: cleanPayload(),
      author: $("#author").value.trim(),
    });
    applyBundle(data);
    toast(`已保存为 v${data.version.version}`);
    loadRetros(data.retro.id);
  } catch (err) {
    if (err.status === 409) return showVersionConflict(err);
    toast("保存失败：" + err.message, 4200);
  } finally {
    btn.disabled = false;
  }
}

function applyBundle(data) {
  state.retro = data.retro;
  state.version = data.version;
  state.payload = JSON.parse(JSON.stringify(data.version.payload));
  state.preview = data.version.result;
  state.dirty = false;
  renderAll();
}

function showVersionConflict(err) {
  openModal(`
    <h3>⚠️ 版本已过期，保存被拒绝</h3>
    <p>${esc(err.message)}</p>
    <p class="muted small">已有小组成员基于较新版本提交了修改。为避免覆盖他人记录，
      本次保存未写入。请刷新载入最新版本；如需保留本地改动，请先自行复制下面的内容。</p>
    <details><summary class="chain-toggle">查看我本地未保存的内容（JSON）</summary>
      <textarea class="input" rows="10" style="width:100%;margin-top:6px;font-family:monospace;font-size:12px"
        readonly>${esc(JSON.stringify(cleanPayload(), null, 2))}</textarea></details>
    <div class="modal-foot">
      <button class="btn btn-ghost" id="vc-cancel">取消</button>
      <button class="btn btn-primary" id="vc-reload">刷新并载入最新版本</button>
    </div>`);
  $("#vc-cancel").onclick = closeModal;
  $("#vc-reload").onclick = async () => {
    closeModal();
    await selectRetro(state.retro.id);
    toast("已载入最新版本");
  };
}

async function onFinalize() {
  try {
    const data = await api("POST", `/api/retros/${state.retro.id}/finalize`,
      { version: state.version.version });
    state.retro = data.retro;
    $("#final-badge").hidden = false;
    renderRetroList();
    toast("已定稿当前版本");
  } catch (err) {
    toast("不能定稿：" + err.message, 4200);
  }
}

async function onDelete() {
  openModal(`
    <h3>删除复盘</h3>
    <p>确定删除「${esc(state.retro.name)}」及其全部历史版本吗？此操作不可恢复。</p>
    <div class="modal-foot">
      <button class="btn btn-ghost" id="modal-close">取消</button>
      <button class="btn btn-danger" id="dl-ok">确认删除</button>
    </div>`);
  $("#modal-close").onclick = closeModal;
  $("#dl-ok").onclick = async () => {
    const id = state.retro.id;
    await api("DELETE", `/api/retros/${id}`);
    closeModal();
    state.retro = null;
    state.version = null;
    $("#editor").hidden = $("#results").hidden = true;
    await loadRetros();
    toast("已删除");
  };
}

async function onRename() {
  const name = $("#retro-name").value.trim();
  if (!state.retro || name === state.retro.name) return;
  const data = await api("PATCH", `/api/retros/${state.retro.id}`, { name });
  state.retro = data.retro;
  renderRetroList();
}

async function onHistory() {
  const data = await api("GET", `/api/retros/${state.retro.id}/versions`);
  const rows = data.versions.map((v) => `
    <div class="vrow" data-v="${v.version}">
      <strong>v${v.version}</strong>
      <span class="state-pill ${v.status === "conflict" ? "state-conflict"
        : v.status === "draft" ? "state-skipped" : "state-ok"}">${
          { ok: "无冲突", draft: "含暂存", conflict: "冲突(未定稿)" }[v.status]}</span>
      <span class="muted small">${esc(v.author || "匿名")} · ${fmtTime(v.created_at)}</span>
      <div class="muted small">${esc(v.note || "")}</div>
    </div>`).join("");
  openModal(`<h3>历史版本</h3>
    <p class="muted small">新版本只能基于最新版保存；点击某行可用该版本的原始内容覆盖当前编辑区（保存后成为新版本）。</p>
    ${rows}
    <div class="modal-foot"><button class="btn btn-primary" id="modal-close">关闭</button></div>`);
  $("#modal-close").onclick = closeModal;
  $$("#modal-card .vrow").forEach((row) => {
    row.onclick = async () => {
      const v = await api("GET",
        `/api/retros/${state.retro.id}/versions/${row.dataset.v}`);
      state.payload = JSON.parse(JSON.stringify(v.payload));
      state.dirty = true;
      state.preview = v.result;
      closeModal();
      renderAll();
      toast(`已载入 v${v.version} 的内容到编辑区（保存即成为新版本）`);
    };
  });
}

async function onNewRetro() {
  const name = prompt("新复盘名称：", "新事故复盘 " + new Date().toLocaleDateString());
  if (name === null) return;
  const data = await api("POST", "/api/retros", {
    name, author: $("#author").value.trim() });
  await loadRetros(data.retro.id);
  toast("已创建新复盘");
}

// --------------------------------------------------------------------------- //
// 弹窗
// --------------------------------------------------------------------------- //

function openModal(html) {
  $("#modal-card").innerHTML = html;
  $("#modal").hidden = false;
}
function closeModal() { $("#modal").hidden = true; }

// --------------------------------------------------------------------------- //
// 初始化
// --------------------------------------------------------------------------- //

function bind() {
  $("#btn-new-retro").onclick = onNewRetro;
  $("#btn-save").onclick = onSave;
  $("#btn-finalize").onclick = onFinalize;
  $("#btn-delete").onclick = onDelete;
  $("#btn-history").onclick = onHistory;
  $("#retro-name").addEventListener("change", onRename);
  $("#modal").addEventListener("click", (e) => {
    if (e.target.id === "modal") closeModal();
  });

  $("#btn-add-event").onclick = () => {
    state.payload.events.push({
      id: uid(), title: "", start_min: "", start_max: "", duration_minutes: "",
    });
    markDirty();
    renderEventsTable();
  };
  $("#btn-add-relation").onclick = () => {
    if (state.payload.events.length < 2) {
      toast("请先添加至少两个事件");
      return;
    }
    state.payload.relations.push({
      before: state.payload.events[0].id,
      after: state.payload.events[1].id,
    });
    markDirty();
    renderRelationsTable();
  };

  const savedAuthor = localStorage.getItem("timeline.author");
  if (savedAuthor) $("#author").value = savedAuthor;
  $("#author").addEventListener("change", () =>
    localStorage.setItem("timeline.author", $("#author").value.trim()));

  window.addEventListener("beforeunload", (e) => {
    if (state.dirty) { e.preventDefault(); e.returnValue = ""; }
  });
}

bind();
loadRetros().catch((err) => toast("加载失败：" + err.message, 5000));
