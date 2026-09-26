/* 本地化资源契约审查台 —— 原生 JS 前端 */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const CODE_LABELS = {
  MISSING_KEY: "缺失键",
  EXTRA_KEY: "多余键",
  PLACEHOLDER_MISMATCH: "占位符不一致",
  BRANCH_MISMATCH: "分支不一致",
  INVALID_SYNTAX: "ICU 语法错误",
  BROKEN_REFERENCE: "引用无法解析",
  CYCLE_FALLBACK: "循环回退",
  UNCOVERED_LANGUAGE: "未覆盖语言",
  INVALID_RESOURCE: "无效资源值",
};

let state = { rev: null, issues: [], selected: new Map() };

// --------------------------------------------------------------- 通用

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(
      typeof data.detail === "string" ? data.detail
      : (data.detail && data.detail.errors
         ? `请求被拒绝（${data.detail.errors.length} 个错误）`
         : `HTTP ${res.status}`));
    err.status = res.status;
    err.data = (data.detail && typeof data.detail === "object") ? data.detail : null;
    throw err;
  }
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// --------------------------------------------------------------- Tab 切换

$$(".tab").forEach(btn => btn.addEventListener("click", () => {
  $$(".tab").forEach(b => b.classList.remove("active"));
  $$(".panel").forEach(p => p.classList.remove("active"));
  btn.classList.add("active");
  $(`#tab-${btn.dataset.tab}`).classList.add("active");
  if (btn.dataset.tab === "issues") loadIssues();
  if (btn.dataset.tab === "graph") loadGraph();
  if (btn.dataset.tab === "diff" || btn.dataset.tab === "issues" ||
      btn.dataset.tab === "graph") loadRevisions();
  if (btn.dataset.tab === "audit") loadAudit();
  if (btn.dataset.tab === "impact") impactOnTab();
  if (btn.dataset.tab === "preview") previewOnTab();
  if (btn.dataset.tab === "pack") packOnTab();
  if (btn.dataset.tab === "rename") renameOnTab();
}));

// --------------------------------------------------------------- 导入

const fileInput = $("#files");
fileInput.addEventListener("change", () => {
  const box = $("#file-meta");
  box.innerHTML = "";
  [...fileInput.files].forEach(f => {
    const div = document.createElement("div");
    div.className = "fm";
    const guess = f.name.replace(/\.(json|arb)$/i, "").split(/[/\\]/).pop();
    div.innerHTML = `<code>${esc(f.name)}</code>
      语言：<input type="text" class="fm-locale" data-file="${esc(f.name)}"
        value="${esc(guess)}" size="10">`;
    box.appendChild(div);
  });
});

$("#import-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#import-msg");
  msg.textContent = "导入中…"; msg.className = "msg";
  const fd = new FormData();
  fd.append("base_locale", $("#base-locale").value.trim());
  fd.append("label", $("#import-label").value.trim());
  const parents = $("#parents-json").value.trim();
  const locales = {};
  $$(".fm-locale").forEach(inp => {
    if (inp.value.trim()) locales[inp.dataset.file] = inp.value.trim();
  });
  fd.append("parents_json", parents || "{}");
  fd.append("locales_json", JSON.stringify(locales));
  [...fileInput.files].forEach(f => fd.append("files", f));
  try {
    const r = await fetch("/api/import", { method: "POST", body: fd });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail);
    msg.textContent = data.deduplicated
      ? `相同输入已存在，切换到版本 ${data.rev}` : `已创建版本 ${data.rev}`;
    msg.className = "msg ok";
    await refreshAll(data.rev);
  } catch (err) {
    msg.textContent = "导入失败：" + err.message;
    msg.className = "msg err";
  }
});

// --------------------------------------------------------------- 版本

async function loadRevisions() {
  const data = await api("/api/revisions");
  const revs = data.revisions;
  const cur = revs.find(r => r.rev === state.rev) ? state.rev
    : (revs[0] && revs[0].rev);
  state.rev = cur || null;
  for (const id of ["#issue-rev", "#graph-rev", "#diff-from", "#diff-to",
                    "#impact-rev", "#mp-rev", "#pk-rev", "#rn-rev"]) {
    const sel = $(id);
    const prev = sel.value || (id === "#diff-from" && revs[1] && revs[1].rev) || cur;
    sel.innerHTML = "";
    revs.forEach(r => {
      const o = document.createElement("option");
      o.value = r.rev;
      o.textContent = `${r.rev} (${r.base_locale}, ${r.created_at}${r.label ? ", " + r.label : ""})`;
      sel.appendChild(o);
    });
    if (prev && revs.some(r => r.rev === prev)) sel.value = prev;
  }
  $("#rev-val").textContent = state.rev || "—";
}

async function refreshAll(rev) {
  state.rev = rev || state.rev;
  await loadRevisions();
  await loadIssues();
  await loadGraph();
  await loadAudit();
}

// --------------------------------------------------------------- 问题

$("#issue-rev").addEventListener("change", e => { state.rev = e.target.value; loadIssues(); });
$("#filter-severity").addEventListener("change", loadIssues);
$("#filter-code").addEventListener("change", loadIssues);

async function loadIssues() {
  if (!state.rev) return;
  const params = new URLSearchParams();
  if ($("#filter-severity").value) params.set("severity", $("#filter-severity").value);
  if ($("#filter-code").value) params.set("code", $("#filter-code").value);
  const data = await api(`/api/revisions/${state.rev}/issues?${params}`);
  state.issues = data.issues;

  const codeSel = $("#filter-code");
  const curCode = codeSel.value;
  codeSel.innerHTML = '<option value="">全部类型</option>';
  Object.keys(data.by_code).sort().forEach(c => {
    const o = document.createElement("option");
    o.value = c; o.textContent = `${CODE_LABELS[c] || c} (${data.by_code[c]})`;
    codeSel.appendChild(o);
  });
  codeSel.value = curCode;

  const sum = $("#issue-summary");
  sum.innerHTML = `<span class="chip error">错误 ${data.counts.error || 0}</span>
    <span class="chip warning">警告 ${data.counts.warning || 0}</span>` +
    Object.keys(data.by_code).sort().map(c =>
      `<span class="chip code">${CODE_LABELS[c] || c}: ${data.by_code[c]}</span>`).join("");

  const tbody = $("#issue-table tbody");
  tbody.innerHTML = "";
  const tpl = $("#issue-row-tpl");
  state.issues.forEach((it, idx) => {
    const row = tpl.content.cloneNode(true);
    const tr = $("tr", row);
    $(".idx", row).textContent = idx + 1;
    $(".sev", row).innerHTML =
      `<span class="sev-${it.severity}">${it.severity === "error" ? "错误" : "警告"}</span>`;
    $(".code", row).textContent = CODE_LABELS[it.code] || it.code;
    $(".loc", row).textContent = it.locale;
    $(".key", row).textContent = it.key;
    $(".pos", row).textContent = it.pos;
    const det = $(".detail", row);
    let html = `<div>${esc(it.detail)}</div>`;
    if (it.expected || it.actual) {
      html += `<div>期望：<span class="expected">${esc(it.expected)}</span>
        ｜实际：<span class="actual">${esc(it.actual)}</span></div>`;
    }
    if (it.fix) {
      const tag = it.fix.auto
        ? '<span class="auto">可自动应用</span>'
        : '<span class="review">建议人工复核</span>';
      html += `<div class="fix-box">${esc(it.fix.reason || it.fix.op)} ${tag}</div>`;
    }
    det.innerHTML = html;
    const cb = $(".pick", row);
    if (!it.fix) { cb.disabled = true; tr.classList.add("row-disabled"); }
    cb.checked = state.selected.has(idx);
    if (cb.checked) tr.classList.add("selected");
    cb.addEventListener("change", () => {
      if (cb.checked) state.selected.set(idx, it); else state.selected.delete(idx);
      tr.classList.toggle("selected", cb.checked);
      updateMergeBar();
    });
    tbody.appendChild(row);
  });
  $("#select-all").checked = false;
  updateMergeBar();
}

$("#select-all").addEventListener("change", e => {
  $$("#issue-table tbody .pick").forEach((cb, i) => {
    if (!cb.disabled) {
      cb.checked = e.target.checked;
      cb.dispatchEvent(new Event("change"));
    }
  });
});

function updateMergeBar() {
  const n = state.selected.size;
  $("#selected-count").textContent = `已选 ${n} 项`;
  $("#btn-merge").disabled = n === 0;
}

$("#btn-merge").addEventListener("click", async () => {
  const btn = $("#btn-merge");
  const msg = $("#merge-msg");
  const reviewItems = [...state.selected.values()].filter(i => i.fix && i.fix.review);
  if (reviewItems.length &&
      !confirm(`所选修复中有 ${reviewItems.length} 项建议人工复核（值可能为基准语言草稿）。仍要合并吗？`)) {
    return;
  }
  btn.disabled = true;
  msg.textContent = "合并中…"; msg.className = "msg";
  const fixes = [...state.selected.values()].map(i => ({
    op: i.fix.op, locale: i.fix.locale, key: i.fix.key,
    value: i.fix.value, parent: i.fix.parent, patches: i.fix.patches,
    reason: i.fix.reason,
  }));
  try {
    const r = await api("/api/merge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_rev: state.rev, fixes }),
    });
    state.selected.clear();
    msg.textContent = `合并成功：${r.deduplicated ? "内容等同于既有版本" : "新版本"} ${r.rev}，应用 ${r.applied.length} 项`;
    msg.className = "msg ok";
    await refreshAll(r.rev);
  } catch (err) {
    msg.textContent = "合并失败（未做任何更改）：" + err.message;
    msg.className = "msg err";
  } finally {
    btn.disabled = state.selected.size === 0;
  }
});

// --------------------------------------------------------------- 回退图

$("#graph-rev").addEventListener("change", loadGraph);

async function loadGraph() {
  const rev = $("#graph-rev").value || state.rev;
  if (!rev) return;
  const data = await api(`/api/revisions/${rev}/graph`);
  const canvas = $("#graph-canvas");
  canvas.innerHTML = "";
  // 按链分组展示：以每条链从根开始绘制；简化为每个节点+指向父的箭头序列
  const drawn = new Set();
  data.nodes.forEach(n => {
    if (drawn.has(n.locale)) return;
    const chain = n.status === "cycle" ? n.chain : [...n.chain].reverse();
    chain.forEach((loc, i) => {
      if (drawn.has(loc)) return;
      drawn.add(loc);
      const node = data.nodes.find(x => x.locale === loc);
      const d = document.createElement("div");
      d.className = `gnode ${node.status}`;
      d.innerHTML = `<div class="gloc">${esc(loc)}${node.is_base ? " ★" : ""}</div>
        <div class="gparent">→ ${esc(node.parent || "∅")}</div>`;
      canvas.appendChild(d);
      if (i < chain.length - 1) {
        const a = document.createElement("div");
        a.className = "garrow"; a.textContent = "←";
        canvas.appendChild(a);
      }
    });
  });

  const tbody = $("#graph-table tbody");
  tbody.innerHTML = "";
  const statusText = { covered: "✓ 到达基准", cycle: "✗ 循环",
    broken: "✗ 链中断（父缺失）", unrooted: "✗ 未到达基准" };
  data.nodes.forEach(n => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(n.locale)}${n.is_base ? " ★基准" : ""}</td>
      <td>${esc(n.parent || "—")}</td>
      <td>${esc(n.chain.join(" → "))}</td>
      <td>${esc(statusText[n.status] || n.status)}</td>`;
    tbody.appendChild(tr);
  });
}

// --------------------------------------------------------------- 差异

$("#diff-form").addEventListener("submit", async e => {
  e.preventDefault();
  const out = $("#diff-output");
  out.innerHTML = "生成中…";
  try {
    const a = $("#diff-from").value, b = $("#diff-to").value;
    const d = await api(`/api/diff?from_rev=${encodeURIComponent(a)}&to_rev=${encodeURIComponent(b)}`);
    $("#diff-summary").innerHTML =
      `<span class="chip added">新增 ${d.summary.added}</span>
       <span class="chip removed">删除 ${d.summary.removed}</span>
       <span class="chip code">修改 ${d.summary.changed}</span>`;
    const groups = [
      ["新增键", d.added, "added", i => `<b>${esc(i.locale)}:${esc(i.key)}</b>
        <div class="diff-val">${esc(i.value)}</div>`],
      ["删除键", d.removed, "removed", i => `<b>${esc(i.locale)}:${esc(i.key)}</b>
        <div class="diff-val">${esc(i.value)}</div>`],
      ["修改键", d.changed, "", i => `<b>${esc(i.locale)}:${esc(i.key)}</b>
        <div class="diff-val removed">- ${esc(i.from)}</div>
        <div class="diff-val added">+ ${esc(i.to)}</div>`],
    ];
    out.innerHTML = groups.map(([title, items, cls, render]) =>
      `<div class="diff-group"><h3>${title}（${items.length}）</h3>` +
      items.map(i => `<div class="${cls}" style="padding:4px 8px;margin:2px 0">${render(i)}</div>`).join("") +
      "</div>").join("");
  } catch (err) {
    out.innerHTML = `<span class="msg err">${esc(err.message)}</span>`;
  }
});

// --------------------------------------------------------------- 审计

$("#audit-refresh").addEventListener("click", loadAudit);

async function loadAudit() {
  const data = await api("/api/audit?limit=200");
  const tbody = $("#audit-table tbody");
  tbody.innerHTML = "";
  data.records.forEach(r => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(r.ts)}</td><td>${esc(r.actor)}</td>
      <td>${esc(r.action)}</td><td>${esc(r.from_rev || "—")}</td>
      <td>${esc(r.to_rev)}</td><td>${esc(r.base_rev)}</td>
      <td class="audit-detail">${esc(JSON.stringify(r.detail, null, 1))}</td>`;
    tbody.appendChild(tr);
  });
}

// --------------------------------------------------------------- 引用影响分析

const impact = {
  rev: null,
  detail: null,       // 版本语言键清单（impact-locale 用）
  result: null,       // 最近一次分析结果
  selectedNode: null, // SVG 中选中的节点 id
  staleTimer: null,
};

const SVG_NS = "http://www.w3.org/2000/svg";

async function impactOnTab() {
  await loadRevisions();
  impact.rev = $("#impact-rev").value || state.rev;
  await impactLoadLocales();
  impactRenderStale();
  if (!impact.staleTimer) {
    impact.staleTimer = setInterval(impactCheckStale, 15000);
  }
}

$("#impact-rev").addEventListener("change", async () => {
  impact.rev = $("#impact-rev").value;
  impact.result = null;
  impact.selectedNode = null;
  await impactLoadLocales();
  impactRenderStale();
});

async function impactLoadLocales() {
  if (!impact.rev) return;
  const data = await api(`/api/revisions/${impact.rev}`);
  impact.detail = data;
  const locSel = $("#impact-locale");
  const prev = locSel.value;
  locSel.innerHTML = "";
  data.locales.forEach(l => {
    const o = document.createElement("option");
    o.value = l.locale;
    o.textContent = `${l.locale}${l.locale === data.base_locale ? " ★基准" : ""}（${l.key_count} 键）`;
    locSel.appendChild(o);
  });
  if (prev && data.locales.some(l => l.locale === prev)) locSel.value = prev;
  await impactLoadKeys();
}

async function impactLoadKeys() {
  if (!impact.rev) return;
  const loc = $("#impact-locale").value;
  if (!loc) return;
  const data = await api(`/api/revisions/${impact.rev}/entries/${loc}`);
  const keySel = $("#impact-key");
  const prev = keySel.value;
  keySel.innerHTML = "";
  Object.keys(data.entries).sort().forEach(k => {
    const o = document.createElement("option");
    o.value = k; o.textContent = k;
    keySel.appendChild(o);
  });
  if (prev && data.entries[prev] !== undefined) keySel.value = prev;
}

$("#impact-locale").addEventListener("change", impactLoadKeys);

$("#impact-form").addEventListener("submit", async e => {
  e.preventDefault();
  await impactRun(false);
});

async function impactRun() {
  const msg = $("#impact-msg");
  const loc = $("#impact-locale").value;
  const key = $("#impact-key").value;
  if (!impact.rev || !loc || !key) return;
  msg.textContent = "分析中…"; msg.className = "msg";
  impact.result = null;
  impact.selectedNode = null;
  try {
    impact.result = await api(
      `/api/revisions/${impact.rev}/impact?locale=${encodeURIComponent(loc)}` +
      `&key=${encodeURIComponent(key)}`);
    msg.textContent = "";
    impactRender();
    impactRenderStale();
  } catch (err) {
    msg.textContent = "分析失败：" + err.message;
    msg.className = "msg err";
  }
}

function impactRenderStale() {
  const banner = $("#impact-stale");
  if (!impact.result) { banner.hidden = true; return; }
  const rev = impact.result.revision;
  const stale = !rev.is_current;
  banner.hidden = !stale;
  if (stale) {
    banner.innerHTML = `⚠ 当前分析结果绑定版本 <b>${esc(rev.rev)}</b>，` +
      `但该版本已不是当前版本（当前：<b>${esc(rev.current_rev)}</b>）。` +
      `快照内容不可变、结果仍可查阅；如需基于最新版本分析请切换。`;
  }
}

async function impactCheckStale() {
  const panel = $("#tab-impact");
  if (!panel.classList.contains("active") || !impact.result) return;
  try {
    const s = await api("/api/state");
    if (impact.result.revision.current_rev !== s.current_rev) {
      impact.result.revision.current_rev = s.current_rev;
      impact.result.revision.is_current = s.current_rev === impact.result.rev;
      impactRenderStale();
    }
  } catch (e) { /* 轮询失败静默 */ }
}

function impactRender() {
  const r = impact.result;
  if (!r) return;
  const s = r.summary;
  $("#impact-meta").innerHTML =
    `<span class="chip code">版本 ${esc(r.rev)}</span>
     <span class="chip" style="background:#dbeafe">直接依赖 ${s.direct}</span>
     <span class="chip code">间接依赖 ${s.indirect}</span>
     <span class="chip code">受影响语言 ${s.affected_locale_count}</span>
     <span class="chip ${s.in_cycle ? "error" : ""}"
       style="${s.in_cycle ? "" : "background:#f0fdf4;color:var(--ok)"}">
       循环 SCC ${s.scc_count}${s.in_cycle ? "（目标在环内）" : ""}</span>
     <span class="chip error">原有断裂引用 ${s.broken_refs}</span>
     <span class="chip code">图节点 ${s.total_nodes} / 边 ${s.total_edges}</span>`;
  $("#impact-target").innerHTML =
    `<span class="tkey">${esc(r.target.locale)} : ${esc(r.target.key)}</span>
     <div class="tval">${esc(r.target.value)}</div>`;

  // 语言筛选器
  const fLoc = $("#imp-filter-locale");
  const prevLoc = fLoc.value;
  fLoc.innerHTML = '<option value="">全部语言</option>';
  [...new Set(r.graph.nodes.map(n => n.locale))].sort().forEach(loc => {
    const o = document.createElement("option");
    o.value = loc; o.textContent = loc;
    fLoc.appendChild(o);
  });
  fLoc.value = prevLoc;

  impactRenderGraph();
  impactRenderTable();
  impactRenderSccs();
  impactRenderBroken();
  $("#impact-export").disabled = false;
}

function impactVisibleNodes() {
  const r = impact.result;
  const onlySub = $("#imp-filter-subgraph").checked;
  const locFilter = $("#imp-filter-locale").value;
  const showBroken = $("#imp-filter-broken").checked;
  const sccNodes = new Set();
  r.sccs.forEach(comp => comp.forEach(n => sccNodes.add(`${n.locale} ${n.key}`)));
  const visible = new Set();
  const brokenSrcs = new Set();
  r.graph.edges.forEach(e => {
    if (e.broken) brokenSrcs.add(e.from);
  });
  r.graph.nodes.forEach(n => {
    if (onlySub && n.kind === "other") {
      if (!(showBroken && brokenSrcs.has(n.id))) return;
    }
    if (locFilter && n.locale !== locFilter) return;
    visible.add(n.id);
  });
  return { visible, sccNodes, brokenSrcs, showBroken, locFilter };
}

function impactRenderGraph() {
  const r = impact.result;
  const svg = $("#impact-svg");
  svg.innerHTML = "";
  const { visible, sccNodes, showBroken } = impactVisibleNodes();

  // 分栏：上游按 distance（BFS 层级），目标 0，下游按 dep_distances，其他聚于左栏
  const NODE_W = 170, NODE_H = 42, GAP_X = 70, GAP_Y = 18;
  const MARGIN_X = 30, MARGIN_Y = 20, LABEL_H = 18;
  const maxDep = Math.max(0, ...Object.values(r.graph.dep_distances));
  const buckets = new Map(); // col -> nodes
  r.graph.nodes.forEach(n => {
    if (!visible.has(n.id)) return;
    let col;
    if (n.kind === "target") col = 0;
    else if (n.distance !== null && n.distance !== undefined) col = -n.distance;
    else if (r.graph.dep_distances[String(n.id)] !== undefined)
      col = r.graph.dep_distances[String(n.id)];
    else col = -(maxDep + 10); // 仅在显示断裂边时出现的无关节点：最左聚栏
    if (!buckets.has(col)) buckets.set(col, []);
    buckets.get(col).push(n);
  });
  const cols = [...buckets.keys()].sort((a, b) => a - b);
  const pos = new Map();
  let maxRows = 0;
  cols.forEach(col => {
    const arr = buckets.get(col).sort((a, b) =>
      a.locale < b.locale ? -1 : a.locale > b.locale ? 1
      : a.key < b.key ? -1 : a.key > b.key ? 1 : 0);
    maxRows = Math.max(maxRows, arr.length);
    arr.forEach((n, i) => {
      const x = MARGIN_X + cols.indexOf(col) * (NODE_W + GAP_X);
      const y = MARGIN_Y + i * (NODE_H + GAP_Y);
      pos.set(n.id, { x, y });
    });
  });
  const width = MARGIN_X * 2 + cols.length * NODE_W + (cols.length - 1) * GAP_X;
  const height = MARGIN_Y * 2 + maxRows * NODE_H + (maxRows - 1) * GAP_Y + LABEL_H;
  svg.setAttribute("width", Math.max(width, 600));
  svg.setAttribute("height", height);
  svg.setAttribute("viewBox", `0 0 ${Math.max(width, 600)} ${height}`);

  // 列标题（距离标注）
  cols.forEach(col => {
    const x = MARGIN_X + cols.indexOf(col) * (NODE_W + GAP_X);
    const t = document.createElementNS(SVG_NS, "text");
    t.setAttribute("x", x + NODE_W / 2);
    t.setAttribute("y", MARGIN_Y - 6);
    t.setAttribute("text-anchor", "middle");
    t.setAttribute("class", "edge-label");
    t.textContent = col === 0 ? "目标"
      : col < 0 && col > -maxDep - 5 ? `上游 · ${-col} 跳`
      : col > 0 ? `下游 · ${col} 跳` : "其他（断裂引用源）";
    svg.appendChild(t);
  });

  const nodeById = new Map(r.graph.nodes.map(n => [n.id, n]));

  // 边（先绘制，节点压在上方）
  r.graph.edges.forEach(e => {
    const fromOk = visible.has(e.from);
    const toOk = e.to !== null && visible.has(e.to);
    if (e.broken) {
      if (!showBroken || !fromOk) return;
    } else if (!fromOk || !toOk) {
      return;
    }
    const a = pos.get(e.from);
    let x2, y2, gx;
    if (e.broken) {
      // 断裂边：节点右侧伸出红色虚线断头
      x2 = a.x + NODE_W + GAP_X * 0.55; y2 = a.y + NODE_H / 2;
      gx = x2;
    } else {
      const b = pos.get(e.to);
      x2 = b.x; y2 = b.y + NODE_H / 2;
      gx = a.x + NODE_W + Math.max(14, (x2 - a.x - NODE_W) / 2);
    }
    const d = `M ${a.x + NODE_W} ${a.y + NODE_H / 2} C ${gx} ${a.y + NODE_H / 2},` +
              ` ${e.broken ? gx : x2 - 30} ${y2}, ${e.broken ? x2 : x2} ${y2}`;
    const hit = document.createElementNS(SVG_NS, "path");
    hit.setAttribute("d", d);
    hit.setAttribute("class", "edge-hit");
    const line = document.createElementNS(SVG_NS, "path");
    line.setAttribute("d", d);
    line.setAttribute("class", "edge" + (e.broken ? " broken-edge" : ""));
    line.dataset.eid = e.eid;
    const tip = `${nodeById.get(e.from).locale}:${nodeById.get(e.from).key} ` +
      `—@${e.ref_key}→ ${e.broken ? "✗ 沿父链无法解析 (" + e.chain.join(" → ") + ")"
        : nodeById.get(e.to).locale + ":" + nodeById.get(e.to).key}`;
    hit.appendChild(document.createElementNS(SVG_NS, "title")).textContent = tip;
    const midX = (a.x + NODE_W + x2) / 2;
    const midY = (a.y + NODE_H / 2 + y2) / 2 - 4;
    const lab = document.createElementNS(SVG_NS, "text");
    lab.setAttribute("x", midX); lab.setAttribute("y", midY);
    lab.setAttribute("text-anchor", "middle");
    lab.setAttribute("class", "edge-label");
    lab.textContent = "@" + (e.ref_key.length > 16 ? e.ref_key.slice(0, 15) + "…" : e.ref_key);
    if (e.broken) lab.style.fill = "var(--error)";
    hit.addEventListener("click", () => {
      impact.selectedNode = e.from;
      const n = nodeById.get(e.from);
      impactRenderGraph();
      impactShowNode(n);
    });
    svg.appendChild(hit); svg.appendChild(line); svg.appendChild(lab);
  });

  // 节点
  r.graph.nodes.forEach(n => {
    if (!pos.has(n.id)) return;
    const { x, y } = pos.get(n.id);
    const g = document.createElementNS(SVG_NS, "g");
    const rect = document.createElementNS(SVG_NS, "rect");
    rect.setAttribute("x", x); rect.setAttribute("y", y);
    rect.setAttribute("width", NODE_W); rect.setAttribute("height", NODE_H);
    rect.setAttribute("rx", 7);
    let cls = `gnode-rect k-${n.kind}`;
    if (sccNodes.has(`${n.locale} ${n.key}`)) cls += " k-scc";
    if (n.id === impact.selectedNode) cls += " k-selected";
    rect.setAttribute("class", cls);
    rect.addEventListener("click", () => {
      impact.selectedNode = n.id;
      impactRenderGraph();
      impactShowNode(n);
    });
    const t = document.createElementNS(SVG_NS, "text");
    t.setAttribute("x", x + 8); t.setAttribute("y", y + 16);
    t.setAttribute("class", "gnode-text");
    t.appendChild(impactTspan(n.locale + "  ", "t-loc"));
    t.appendChild(impactTspan(
      n.kind === "target" ? "◉ "
      : n.kind === "direct" ? "● "
      : n.kind === "indirect" ? "○ " : "· "));
    const t2 = document.createElementNS(SVG_NS, "text");
    t2.setAttribute("x", x + 8); t2.setAttribute("y", y + 33);
    t2.setAttribute("class", "gnode-text");
    const keyText = n.key.length > 24 ? n.key.slice(0, 23) + "…" : n.key;
    t2.textContent = keyText;
    t2.appendChild(document.createElementNS(SVG_NS, "title")).textContent = n.key;
    g.appendChild(rect); g.appendChild(t); g.appendChild(t2);
    g.appendChild(document.createElementNS(SVG_NS, "title")).textContent =
      `${n.locale}:${n.key}\n类型：${({target: "目标", direct: "直接依赖", indirect: "间接依赖", other: "其他"})[n.kind]}` +
      (n.distance !== null && n.distance !== undefined ? `\n距离：${n.distance}` : "");
    svg.appendChild(g);
  });
}

function impactTspan(text, cls) {
  const ts = document.createElementNS(SVG_NS, "tspan");
  ts.textContent = text;
  if (cls) ts.setAttribute("class", cls);
  return ts;
}

function impactShowNode(n) {
  const r = impact.result;
  const box = $("#impact-node-detail");
  box.hidden = false;
  const edgesOut = r.graph.edges.filter(e => e.from === n.id);
  const edgesIn = r.graph.edges.filter(e => e.to === n.id);
  const node = r.graph.nodes[n.id];
  const isTarget = node.kind === "target";
  let valLine = "";
  if (!isTarget) {
    const imp = r.impacted.find(i => i.locale === n.locale && i.key === n.key);
    if (imp) valLine = `<div class="ndval">${esc(imp.value)}</div>`;
  } else {
    valLine = `<div class="ndval">${esc(r.target.value)}</div>`;
  }
  box.innerHTML =
    `<b>${esc(n.locale)} : ${esc(n.key)}</b>
     <span class="hint">（${({target: "目标", direct: "直接依赖", indirect: "间接依赖", other: "其他"})[n.kind]}${
       n.distance !== null && n.distance !== undefined ? "，距离 " + n.distance : ""}）</span>
     ${valLine}
     <div class="hint" style="margin-top:6px">出边 ${edgesOut.length}：${
       edgesOut.map(e => "@" + esc(e.ref_key) + (e.broken ? " ✗断裂" :
         " → " + esc(r.graph.nodes[e.to].locale) + ":" + esc(r.graph.nodes[e.to].key))).join("；") || "—"}</div>
     <div class="hint">入边 ${edgesIn.length}：${
       edgesIn.map(e => esc(r.graph.nodes[e.from].locale) + ":" + esc(r.graph.nodes[e.from].key)
         + " @" + esc(e.ref_key)).join("；") || "—"}</div>`;
}

function impactRenderTable() {
  const r = impact.result;
  const tbody = $("#impact-table tbody");
  tbody.innerHTML = "";
  r.impacted.forEach((it, i) => {
    const tr = document.createElement("tr");
    tr.className = it.direct ? "row-direct" : "row-indirect";
    const pathId = `p${i}`;
    const pathStr = it.path.map(p => `${p.locale}:${p.key}`).join("  ▶  ");
    tr.innerHTML =
      `<td>${it.distance}</td>
       <td>${it.direct ? '<span class="sev-error">直接</span>' : '<span class="sev-warning">间接</span>'}</td>
       <td>${esc(it.locale)}</td>
       <td class="mono">${esc(it.key)}</td>
       <td><span class="path-toggle" data-id="${pathId}">▸ 展开最短证据路径（${it.path.length - 1} 跳）</span>
         <div class="path-detail" id="${pathId}" hidden>${esc(pathStr)}
引用：${esc(it.path_refs.map(x => "@" + x).join("  →  "))}

消息值：${esc(it.value)}</div></td>`;
    tbody.appendChild(tr);
  });
  $$(".path-toggle", tbody).forEach(el => el.addEventListener("click", () => {
    const d = document.getElementById(el.dataset.id);
    d.hidden = !d.hidden;
    el.textContent = d.hidden
      ? el.textContent.replace("▾", "▸")
      : el.textContent.replace("▸", "▾");
  }));
}

function impactRenderSccs() {
  const r = impact.result;
  const box = $("#impact-sccs");
  if (!r.sccs.length) {
    box.innerHTML = '<div class="hint">无循环引用。</div>';
  } else {
    box.innerHTML = r.sccs.map((comp, i) => {
      const isTarget = i === r.target_scc_index;
      const nodes = comp.map(n => `${n.locale}:${n.key}`).join("  ▶  ") +
        "  ▶  " + comp[0].locale + ":" + comp[0].key;
      return `<div class="scc ${isTarget ? "target-scc" : ""}">
        <b>SCC #${i + 1}</b>${isTarget ? "（目标在此环内）" : ""}<br>
        <span class="scc-nodes">${esc(nodes)}</span></div>`;
    }).join("");
  }
  $("#impact-locales").innerHTML = r.affected_locales.length
    ? r.affected_locales.map(l => `<span class="chip code">${esc(l)}</span>`).join("")
    : '<span class="hint">无受影响语言。</span>';
}

function impactRenderBroken() {
  const r = impact.result;
  $("#broken-count").textContent = r.broken_refs.length;
  const box = $("#impact-broken");
  box.innerHTML = r.broken_refs.length
    ? r.broken_refs.map(b => `<div class="broken-item">${esc(b.locale)}:${esc(b.key)} ` +
        `→ @${esc(b.ref_key)} ✗<br><span class="pv-cause">回退链：${
          esc(b.chain.join(" → "))}（位置 ${b.pos}）</span></div>`).join("")
    : '<div class="hint">无断裂引用。</div>';
}

["#imp-filter-subgraph", "#imp-filter-locale", "#imp-filter-broken"]
  .forEach(id => $(id).addEventListener("change", () => {
    if (impact.result) impactRenderGraph();
  }));

// --------------------------------------------------------------- 预演

function impactCurrentFixes() {
  const txt = $("#imp-fixes").value.trim();
  if (!txt) return [];
  return JSON.parse(txt);
}

$("#imp-add-delete").addEventListener("click", () => {
  if (!impact.result) return;
  const t = impact.result.target;
  const fixes = impactSafeFixes();
  fixes.push({ op: "delete", locale: t.locale, key: t.key });
  $("#imp-fixes").value = JSON.stringify(fixes, null, 2);
});

$("#imp-add-rewrite").addEventListener("click", () => {
  if (!impact.result) return;
  const t = impact.result.target;
  const fixes = impactSafeFixes();
  fixes.push({ op: "upsert", locale: t.locale, key: t.key,
               value: t.value + " " });
  $("#imp-fixes").value = JSON.stringify(fixes, null, 2);
});

$("#imp-clear-fixes").addEventListener("click", () => {
  $("#imp-fixes").value = "";
  $("#imp-preview-result").innerHTML = "";
  $("#imp-preview-msg").textContent = "";
});

function impactSafeFixes() {
  try { return impactCurrentFixes(); }
  catch (e) { return []; }
}

$("#imp-run-preview").addEventListener("click", async () => {
  const msg = $("#imp-preview-msg");
  const out = $("#imp-preview-result");
  if (!impact.result) return;
  let fixes;
  try {
    fixes = impactCurrentFixes();
  } catch (e) {
    msg.textContent = "修复集 JSON 解析失败：" + e.message;
    msg.className = "msg err";
    return;
  }
  if (!Array.isArray(fixes)) {
    msg.textContent = "修复集必须是数组";
    msg.className = "msg err";
    return;
  }
  msg.textContent = "预演中（不落库）…"; msg.className = "msg";
  out.innerHTML = "";
  const t = impact.result.target;
  try {
    const p = await api(`/api/revisions/${impact.rev}/impact/preview`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ locale: t.locale, key: t.key, fixes }),
    });
    msg.textContent = `预演完成：物化 ${p.applied.length} 项，未写入数据库。`;
    msg.className = "msg ok";
    impactLastPreview = p;
    impactRenderPreview(p);
  } catch (err) {
    msg.textContent = "预演失败（修复集无效，未做任何更改）：" + err.message;
    msg.className = "msg err";
  }
});

let impactLastPreview = null;

function pvItem(cls, html) {
  return `<div class="pv-item ${cls}">${html}</div>`;
}

function impactRenderPreview(p) {
  const c = p.broken_refs.summary;
  const refLine = b => `${esc(b.locale)}:${esc(b.key)} → @${esc(b.ref_key)}`;
  let html = `<div class="pv-chips">
      <span class="chip error">新增断裂 ${c.new_broken}</span>
      <span class="chip warning">原有断裂 ${c.preexisting}</span>
      <span class="chip" style="background:#dcfce7;color:var(--ok)">顺带修复 ${c.resolved}</span>
      <span class="chip" style="background:#dcfce7;color:var(--ok)">改写移除 ${c.removed}</span>
      <span class="chip code">落点变化 ${c.retargeted}</span>
      <span class="chip code">随源删除消失 ${c.source_removed}</span>
      <span class="chip code">断裂总数 ${c.broken_before} → ${c.broken_after}</span>
    </div>`;
  if (p.target.deleted)
    html += '<div class="pv-item pv-new">目标键被删除：影响以引用断裂形式体现，下方不再计算影响集。</div>';
  if (p.broken_refs.new_broken.length)
    html += "<h3>新增断裂引用（修复集引入）</h3>" +
      p.broken_refs.new_broken.map(b => pvItem("pv-new",
        `${refLine(b)} ✗<br><span class="pv-cause">原因：${b.cause === "target_deleted"
          ? "目标被删除" : b.cause === "ref_added" ? "改写新引入的引用" : "回退链变化"}${
          b.previous_target ? `；原先解析到 ${esc(b.previous_target.locale)}:${
            esc(b.previous_target.key)}` : ""}；链：${esc(b.chain.join(" → "))}</span>`)).join("");
  if (p.broken_refs.preexisting.length)
    html += "<h3>原有断裂引用（与本次修复无关）</h3>" +
      p.broken_refs.preexisting.map(b => pvItem("pv-old",
        `${refLine(b)} ✗<br><span class="pv-cause">链：${esc(b.chain.join(" → "))}</span>`)).join("");
  if (p.broken_refs.resolved.length)
    html += "<h3>顺带修复的断裂引用</h3>" +
      p.broken_refs.resolved.map(b => pvItem("pv-fixed",
        `${refLine(b)} ✓ → ${esc(b.resolved_to.locale)}:${esc(b.resolved_to.key)}`)).join("");
  if (p.broken_refs.removed.length)
    html += "<h3>改写移除的断裂引用（源消息仍在，不再携带该引用）</h3>" +
      p.broken_refs.removed.map(b => pvItem("pv-fixed",
        `${refLine(b)} ✂ 改写后消失<br><span class="pv-cause">原链：${
          esc(b.chain.join(" → "))}</span>`)).join("");
  if (p.broken_refs.retargeted.length)
    html += "<h3>引用落点变化</h3>" +
      p.broken_refs.retargeted.map(b => pvItem("pv-retarget",
        `${refLine(b)}：${esc(b.from.locale)}:${esc(b.from.key)} ⇒ ${
          esc(b.to.locale)}:${esc(b.to.key)}`)).join("");
  if (p.broken_refs.source_removed.length)
    html += "<h3>随源消息删除而消失的引用（非断裂）</h3>" +
      p.broken_refs.source_removed.map(b => pvItem("pv-old",
        `${esc(b.locale)}:${esc(b.key)} 携带的 @${esc(b.ref_key)}` +
        (b.was_broken ? "（删除前本就断裂）" : ""))).join("");
  if (p.impact_after) {
    const s = p.impact_after.summary;
    html += `<h3>预演后的传递影响（键仍存在：${
      p.target.rewritten ? "改写" : "结构未变"}）</h3>
      <div class="pv-chips">
        <span class="chip" style="background:#dbeafe">直接 ${s.direct}</span>
        <span class="chip code">间接 ${s.indirect}</span>
        <span class="chip code">受影响语言 ${s.affected_locale_count}</span>
        <span class="chip code">循环 SCC ${s.scc_count}</span>
      </div>`;
  }
  $("#imp-preview-result").innerHTML = html;
}

// --------------------------------------------------------------- 导出

$("#impact-export").addEventListener("click", () => {
  if (!impact.result) return;
  const payload = {
    generated_at: new Date().toISOString(),
    analysis: impact.result,
    last_preview: impactLastPreview,
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)],
                        { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `impact_${impact.result.rev}_${impact.result.target.locale}_` +
    `${impact.result.target.key}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
});

// --------------------------------------------------------------- 消息实例预览

const mp = {
  rev: null,
  detail: null,        // /api/revisions/{rev}
  keysByLocale: new Map(),
  result: null,
  staleTimer: null,
};

const MP_ERROR_LABELS = {
  MISSING_PARAM: "缺少变量",
  TYPE_MISMATCH: "参数类型不符",
  NO_BRANCH: "无可用分支",
  BROKEN_REFERENCE: "断裂引用",
  REFERENCE_CYCLE: "引用环",
  MESSAGE_NOT_FOUND: "消息不存在",
  INVALID_MESSAGE: "消息 ICU 语法错误",
  UNSUPPORTED_LOCALE: "不支持的语言",
  UNSUPPORTED_TYPE: "不支持的占位符类型",
  FORMAT_ERROR: "格式化失败",
};

async function previewOnTab() {
  await loadRevisions();
  mp.rev = $("#mp-rev").value || state.rev;
  await mpLoad();
  mpRenderStale();
  if (!mp.staleTimer) mp.staleTimer = setInterval(mpCheckStale, 15000);
}

$("#mp-rev").addEventListener("change", async () => {
  mp.rev = $("#mp-rev").value;
  mp.result = null;
  await mpLoad();
  mpRenderStale();
});

async function mpLoad() {
  if (!mp.rev) return;
  const data = await api(`/api/revisions/${mp.rev}`);
  mp.detail = data;
  const locSel = $("#mp-locales");
  const prev = new Set([...locSel.selectedOptions].map(o => o.value));
  locSel.innerHTML = "";
  data.locales.forEach(l => {
    const o = document.createElement("option");
    o.value = l.locale;
    o.textContent = `${l.locale}${l.locale === data.base_locale ? " ★基准" : ""}`;
    if (prev.has(l.locale)) o.selected = true;
    locSel.appendChild(o);
  });
  // 默认全选
  if (!prev.size) [...locSel.options].forEach(o => o.selected = true);
  await mpLoadKeys();
}

async function mpLoadKeys() {
  if (!mp.rev) return;
  mp.keysByLocale = new Map();
  const locs = [...$("#mp-locales").options].map(o => o.value);
  const all = new Set();
  await Promise.all(locs.map(async loc => {
    const d = await api(`/api/revisions/${mp.rev}/entries/${loc}`);
    mp.keysByLocale.set(loc, new Set(Object.keys(d.entries)));
    Object.keys(d.entries).forEach(k => all.add(k));
  }));
  const keySel = $("#mp-key");
  const prevKey = keySel.value;
  keySel.innerHTML = "";
  [...all].sort().forEach(k => {
    const o = document.createElement("option");
    o.value = k; o.textContent = k;
    keySel.appendChild(o);
  });
  if (prevKey && all.has(prevKey)) keySel.value = prevKey;
}

$("#mp-locales").addEventListener("change", mpLoadKeys);
$("#mp-select-all").addEventListener("click", () =>
  [...$("#mp-locales").options].forEach(o => o.selected = true));
$("#mp-clear-locales").addEventListener("click", () =>
  [...$("#mp-locales").options].forEach(o => o.selected = false));

$("#mp-form").addEventListener("submit", async e => {
  e.preventDefault();
  const msg = $("#mp-msg");
  const locales = [...$("#mp-locales").selectedOptions].map(o => o.value);
  const key = $("#mp-key").value;
  if (!mp.rev || !key) return;
  if (!locales.length) {
    msg.textContent = "请至少选择一个目标语言";
    msg.className = "msg err";
    return;
  }
  let variables;
  const txt = $("#mp-vars").value.trim();
  if (txt) {
    try {
      variables = JSON.parse(txt);
    } catch (err) {
      msg.textContent = "变量 JSON 解析失败：" + err.message;
      msg.className = "msg err";
      return;
    }
    if (variables === null || typeof variables !== "object" || Array.isArray(variables)) {
      msg.textContent = "变量 JSON 必须是对象 {…}";
      msg.className = "msg err";
      return;
    }
  } else {
    variables = {};
  }
  msg.textContent = "渲染中…"; msg.className = "msg";
  try {
    mp.result = await api(`/api/revisions/${mp.rev}/preview`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, locales, variables }),
    });
    msg.textContent = "";
    mpRender();
    mpRenderStale();
  } catch (err) {
    msg.textContent = "预览失败：" + err.message;
    msg.className = "msg err";
  }
});

function mpRenderStale() {
  const banner = $("#mp-stale");
  if (!mp.result) { banner.hidden = true; return; }
  const rev = mp.result.revision;
  const stale = !rev.is_current;
  banner.hidden = !stale;
  if (stale) {
    banner.innerHTML = `⚠ 预览结果绑定版本 <b>${esc(rev.rev)}</b>，` +
      `该版本已不是当前版本（当前：<b>${esc(rev.current_rev)}</b>）。` +
      `快照不可变，结果仍可查阅。`;
  }
}

async function mpCheckStale() {
  const panel = $("#tab-preview");
  if (!panel.classList.contains("active") || !mp.result) return;
  try {
    const s = await api("/api/state");
    if (mp.result.revision.current_rev !== s.current_rev) {
      mp.result.revision.current_rev = s.current_rev;
      mp.result.revision.is_current = s.current_rev === mp.result.rev;
      mpRenderStale();
    }
  } catch (e) { /* 静默 */ }
}

function mpHopsHtml(hops) {
  if (!hops || !hops.length) return "";
  const rows = hops.map((h, i) =>
    `<div class="mp-hop"><span class="mp-hop-n">${i + 1}</span>
       <code>@${esc(h.ref_key)}</code>
       ${esc(h.from.locale)}:${esc(h.from.key)}
       <span class="mp-arrow">→</span>
       <b>${esc(h.to.locale)}</b>:${esc(h.to.key)}
       <span class="hint">（链 ${esc(h.chain.join(" → "))}）</span></div>`).join("");
  return `<div class="mp-hops"><div class="mp-hops-title">引用轨迹（${hops.length} 跳）</div>${rows}</div>`;
}

function mpErrorHtml(err) {
  const label = MP_ERROR_LABELS[err.code] || err.code;
  const path = err.path && err.path.length
    ? `<div class="mp-err-path">路径：<code>${esc(err.path.join(" 〉 "))}</code></div>` : "";
  let extra = "";
  if (err.code === "NO_BRANCH" && err.available)
    extra = `<div class="hint">可用分支：${esc(err.available.join(", "))}</div>`;
  if ((err.code === "BROKEN_REFERENCE" || err.code === "MESSAGE_NOT_FOUND") && err.chain)
    extra = `<div class="hint">回退链：${esc(err.chain.join(" → "))}</div>`;
  if (err.code === "REFERENCE_CYCLE" && err.cycle)
    extra = `<div class="hint">环：${esc(err.cycle.join(" → "))}</div>`;
  return `<div class="mp-err">
      <div class="mp-err-head"><span class="sev-error">${esc(label)}</span>
        <code>${esc(err.code)}</code></div>
      <div class="mp-err-detail">${esc(err.detail)}</div>${path}${extra}
      <div class="hint">出错位置：${esc(err.locale)}:${esc(err.key)}</div>
    </div>`;
}

function mpRender() {
  const r = mp.result;
  if (!r) return;
  $("#mp-meta").innerHTML =
    `<span class="chip code">版本 ${esc(r.rev)}</span>
     <span class="chip" style="background:#dcfce7;color:var(--ok)">成功 ${r.summary.ok}</span>
     <span class="chip error">出错 ${r.summary.errors}</span>
     <span class="chip code">键 <code>${esc(r.key)}</code></span>
     <span class="chip code">目标语言 ${r.summary.locales}</span>`;
  const grid = $("#mp-results");
  grid.innerHTML = "";
  r.results.forEach(x => {
    const card = document.createElement("div");
    card.className = "mp-card " + (x.ok ? "mp-ok" : "mp-fail");
    if (x.ok) {
      card.innerHTML =
        `<div class="mp-card-head"><b>${esc(x.locale)}</b>
           <span class="mp-badge ${x.fell_back ? "mp-fb" : "mp-self"}">${
             x.fell_back ? "↩ 回退取值" : "本语言取值"}</span></div>
         <div class="mp-meta-line">
           实际取值语言：<b>${esc(x.resolved_locale)}</b>
           <span class="hint">（链 ${esc(x.fallback_chain.join(" → "))}）</span></div>
         <div class="mp-text">${esc(x.text)}</div>
         ${mpHopsHtml(x.hops)}`;
    } else {
      card.innerHTML =
        `<div class="mp-card-head"><b>${esc(x.locale)}</b>
           <span class="mp-badge mp-err-badge">✗ 未生成文本</span></div>
         ${mpErrorHtml(x.error)}`;
    }
    grid.appendChild(card);
  });
}

// --------------------------------------------------------------- 语言包编译

const pk = {
  rev: null,
  detail: null,
  allKeys: [],
  keySel: new Set(),   // 持久化的键勾选状态（筛选不影响未渲染键的选择）
  result: null,
  staleTimer: null,
};

const PK_ERROR_LABELS = {
  LOCALE_NOT_FOUND: "语言不存在",
  ENTRY_NOT_FOUND: "入口键不可解析",
  BROKEN_REFERENCE: "断裂引用",
  REFERENCE_CYCLE: "引用环",
  INVALID_MESSAGE: "ICU 语法错误",
  INVALID_LOCALE_ID: "语言标识非法",
};

async function packOnTab() {
  await loadRevisions();
  pk.rev = $("#pk-rev").value || state.rev;
  await pkLoad();
  pkRenderStale();
  if (!pk.staleTimer) pk.staleTimer = setInterval(pkCheckStale, 15000);
}

$("#pk-rev").addEventListener("change", async () => {
  pk.rev = $("#pk-rev").value;
  pk.result = null;
  pkRenderEmpty();
  await pkLoad();
  pkRenderStale();
});

async function pkLoad() {
  if (!pk.rev) return;
  const data = await api(`/api/revisions/${pk.rev}`);
  pk.detail = data;
  // 语言清单（保持勾选状态）
  const prevLoc = new Set(pkSelectedLocales());
  const locBox = $("#pk-locales");
  locBox.innerHTML = "";
  data.locales.forEach(l => {
    const id = `pk-loc-${l.locale}`;
    const div = document.createElement("label");
    div.className = "pk-item";
    div.innerHTML = `<input type="checkbox" id="${esc(id)}" value="${esc(l.locale)}"
        ${prevLoc.size === 0 || prevLoc.has(l.locale) ? "checked" : ""}>
      <span>${esc(l.locale)}${l.locale === data.base_locale ? " ★基准" : ""}
        <span class="hint">（${l.key_count} 键）</span></span>`;
    locBox.appendChild(div);
  });
  // 键全集（各语言键的并集，排序）；保留仍存在键的既有勾选
  const all = new Set();
  await Promise.all(data.locales.map(async l => {
    const d = await api(`/api/revisions/${pk.rev}/entries/${l.locale}`);
    Object.keys(d.entries).forEach(k => all.add(k));
  }));
  pk.allKeys = [...all].sort();
  // 新版本/首次进入：默认全选；否则仅保留本次键集内的旧选择
  if ([...pk.keySel].every(k => !all.has(k))) pk.keySel = new Set(pk.allKeys);
  else pk.keySel = new Set([...pk.keySel].filter(k => all.has(k)));
  pkRenderKeys();
}

function pkRenderKeys() {
  const filter = $("#pk-key-filter").value.trim().toLowerCase();
  const box = $("#pk-keys");
  box.innerHTML = "";
  pk.allKeys.filter(k => !filter || k.toLowerCase().includes(filter))
    .forEach(k => {
      const div = document.createElement("label");
      div.className = "pk-item mono";
      div.innerHTML = `<input type="checkbox" value="${esc(k)}"
          ${pk.keySel.has(k) ? "checked" : ""}>
        <span>${esc(k)}</span>`;
      $("input", div).addEventListener("change", e => {
        if (e.target.checked) pk.keySel.add(k); else pk.keySel.delete(k);
      });
      box.appendChild(div);
    });
  if (!box.children.length)
    box.innerHTML = '<div class="hint" style="padding:6px">无匹配键。</div>';
}

$("#pk-key-filter").addEventListener("input", pkRenderKeys);
$("#pk-loc-all").addEventListener("click", () =>
  $$("#pk-locales input[type=checkbox]").forEach(c => c.checked = true));
$("#pk-loc-none").addEventListener("click", () =>
  $$("#pk-locales input[type=checkbox]").forEach(c => c.checked = false));
$("#pk-key-all").addEventListener("click", () => {
  const filter = $("#pk-key-filter").value.trim().toLowerCase();
  pk.allKeys.filter(k => !filter || k.toLowerCase().includes(filter))
    .forEach(k => pk.keySel.add(k));
  pkRenderKeys();
});
$("#pk-key-none").addEventListener("click", () => {
  pk.keySel.clear();
  pkRenderKeys();
});

function pkSelectedLocales() {
  return $$("#pk-locales input[type=checkbox]:checked").map(c => c.value);
}

function pkSelectedKeys() {
  return [...pk.keySel];
}

function pkRenderEmpty() {
  $("#pk-meta").innerHTML = "";
  $("#pk-errors").hidden = true;
  $("#pk-result").hidden = true;
  $("#pk-stale").hidden = true;
}

$("#pk-compile").addEventListener("click", async () => {
  const msg = $("#pk-msg");
  const locales = pkSelectedLocales();
  const keys = pkSelectedKeys();
  if (!pk.rev) return;
  if (!locales.length) {
    msg.textContent = "请至少选择一个目标语言"; msg.className = "msg err"; return;
  }
  if (!keys.length) {
    msg.textContent = "请至少选择一个入口键"; msg.className = "msg err"; return;
  }
  msg.textContent = "编译中…"; msg.className = "msg";
  pkRenderEmpty();
  try {
    const r = await api(`/api/revisions/${pk.rev}/pack`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ locales, keys }),
    });
    pk.result = r;
    msg.textContent = `编译成功：${locales.length} 个语言 × ${keys.length} 个键，` +
      `ZIP ${r.package.bytes} 字节（SHA-256 见下表）。`;
    msg.className = "msg ok";
    pkRenderResult(r);
    pkRenderStale();
  } catch (err) {
    pk.result = null;
    if (err.data && Array.isArray(err.data.errors)) {
      msg.textContent = `整包未生成：${err.data.errors.length} 个错误阻止编译。`;
      msg.className = "msg err";
      pkRenderErrors(err.data.errors);
    } else {
      msg.textContent = "编译失败：" + err.message;
      msg.className = "msg err";
    }
  }
});

function pkRenderErrors(errors) {
  const box = $("#pk-errors");
  box.hidden = false;
  const tbody = $("#pk-error-table tbody");
  tbody.innerHTML = "";
  errors.forEach(e => {
    const tr = document.createElement("tr");
    const path = (e.path && e.path.length) ? e.path.join(" 〉 ") : "—";
    let extra = "";
    if (e.chain) extra += `<div class="hint">回退链：${esc(e.chain.join(" → "))}</div>`;
    if (e.cycle) extra += `<div class="hint">环：${esc(e.cycle.join(" → "))}</div>`;
    tr.innerHTML = `<td>${esc(e.locale)}</td><td class="mono">${esc(e.key)}</td>
      <td><span class="sev-error">${esc(PK_ERROR_LABELS[e.code] || e.code)}</span>
        <div class="hint mono">${esc(e.code)}</div></td>
      <td class="mono">${esc(path)}</td>
      <td>${esc(e.detail)}${extra}</td>`;
    tbody.appendChild(tr);
  });
}

function pkRenderResult(r) {
  $("#pk-meta").innerHTML =
    `<span class="chip code">版本 ${esc(r.rev)}</span>
     <span class="chip" style="background:#dcfce7;color:var(--ok)">语言 ${r.locales.length}</span>
     <span class="chip code">键 ${r.keys.length}</span>
     <span class="chip code">文件 ${r.files.length + 1}（含 manifest.json）</span>
     <span class="chip code">ZIP ${r.package.bytes} 字节</span>`;
  const tbody = $("#pk-files-table tbody");
  tbody.innerHTML = "";
  r.files.forEach(f => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td class="mono">${esc(f.path)}</td><td>${esc(f.locale)}</td>
      <td>${f.bytes}</td><td class="mono pk-sha">${esc(f.sha256)}</td>`;
    tbody.appendChild(tr);
  });
  const mtr = document.createElement("tr");
  mtr.innerHTML = `<td class="mono">manifest.json</td><td>—</td>
    <td>${r.manifest_bytes}</td><td class="mono pk-sha">（见 ZIP 内）</td>`;
  tbody.appendChild(mtr);
  // 来源查看下拉
  const locSel = $("#pk-src-locale");
  locSel.innerHTML = "";
  r.locales.forEach(l => {
    const o = document.createElement("option");
    o.value = l; o.textContent = l;
    locSel.appendChild(o);
  });
  pkRenderSourceKeys();
  $("#pk-result").hidden = false;
}

function pkRenderSourceKeys() {
  const r = pk.result;
  if (!r) return;
  const loc = $("#pk-src-locale").value;
  const keySel = $("#pk-src-key");
  keySel.innerHTML = "";
  r.keys.forEach(k => {
    const o = document.createElement("option");
    o.value = k; o.textContent = k;
    keySel.appendChild(o);
  });
  pkRenderSource();
}

$("#pk-src-locale").addEventListener("change", pkRenderSourceKeys);
$("#pk-src-key").addEventListener("change", pkRenderSource);

function pkRenderSource() {
  const r = pk.result;
  if (!r) return;
  const loc = $("#pk-src-locale").value;
  const key = $("#pk-src-key").value;
  const box = $("#pk-source");
  const prov = r.provenance[loc] && r.provenance[loc][key];
  if (!prov) { box.innerHTML = '<div class="hint">无来源信息。</div>'; return; }
  const src = prov.source;
  const hops = prov.hops.length
    ? prov.hops.map((h, i) =>
        `<div class="mp-hop"><span class="mp-hop-n">${i + 1}</span>
           <code>@${esc(h.ref_key)}</code> ${esc(h.from.locale)}:${esc(h.from.key)}
           <span class="mp-arrow">→</span> <b>${esc(h.to.locale)}</b>:${esc(h.to.key)}
           <span class="hint">（链 ${esc(h.chain.join(" → "))}）</span></div>`).join("")
    : '<div class="hint">无引用（消息未携带 {@key}）。</div>';
  const sources = prov.sources.map(s =>
    `<span class="chip code">${esc(s.locale)}:${esc(s.key)}</span>`).join(" ");
  box.innerHTML =
    `<div class="pk-src-line">实际来源：<b>${esc(src.locale)}</b>
       <span class="mp-badge ${src.fell_back ? "mp-fb" : "mp-self"}">${
         src.fell_back ? "↩ 回退取值" : "本语言取值"}</span>
       <span class="hint">（链 ${esc(src.fallback_chain.join(" → "))}）</span></div>
     <div class="pk-src-line">导出模板（已展开引用，自包含）：</div>
     <div class="mp-text mono">${esc(r.templates[loc][key])}</div>
     <div class="pk-src-line">展开涉及的消息来源（${prov.sources.length}）：${sources}</div>
     <div class="mp-hops"><div class="mp-hops-title">引用展开轨迹（${prov.hops.length} 跳）</div>${hops}</div>`;
}

$("#pk-download").addEventListener("click", () => {
  if (!pk.result) return;
  // 下载严格使用已编译的语言/键集合，保证与页面显示的清单同字节
  const params = new URLSearchParams();
  pk.result.locales.forEach(l => params.append("locales", l));
  pk.result.keys.forEach(k => params.append("keys", k));
  const a = document.createElement("a");
  a.href = `/api/revisions/${pk.rev}/pack/download?${params}`;
  a.download = pk.result.package.filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
});

function pkRenderStale() {
  const banner = $("#pk-stale");
  if (!pk.result) { banner.hidden = true; return; }
  const rev = pk.result.revision;
  const stale = !rev.is_current;
  banner.hidden = !stale;
  if (stale) {
    banner.innerHTML = `⚠ 编译结果绑定版本 <b>${esc(rev.rev)}</b>，` +
      `但该版本已不是当前版本（当前：<b>${esc(rev.current_rev)}</b>）。` +
      `快照不可变、结果仍可查阅与下载。`;
  }
}

async function pkCheckStale() {
  const panel = $("#tab-pack");
  if (!panel.classList.contains("active") || !pk.result) return;
  try {
    const s = await api("/api/state");
    if (pk.result.revision.current_rev !== s.current_rev) {
      pk.result.revision.current_rev = s.current_rev;
      pk.result.revision.is_current = s.current_rev === pk.result.rev;
      pkRenderStale();
    }
  } catch (e) { /* 轮询失败静默 */ }
}

// --------------------------------------------------------------- 资源键重命名迁移

const rn = {
  rev: null,
  rows: [{ old: "", neu: "" }],
  result: null,        // 最近一次试运行/确认结果
  committed: false,    // 当前结果是否为确认产物
  staleTimer: null,
};

const RN_ERROR_LABELS = {
  EMPTY_PLAN: "空计划",
  BAD_MAPPING: "映射格式错误",
  SELF_RENAME: "新旧键相同",
  SOURCE_NOT_FOUND: "源键不存在",
  DUPLICATE_SOURCE: "源键重复",
  DUPLICATE_TARGET: "目标键重复",
  TARGET_CONFLICT: "目标键与未迁移键冲突",
  CHAIN_AMBIGUOUS: "链式歧义",
  INVALID_KEY_CHAR: "目标键含非法字符",
};

const RN_RISK_LABELS = {
  BROKEN_REFERENCE: "原有断裂引用",
  UNPARSEABLE_REFERENCE: "语法错误消息中的引用（未自动改写）",
  NEW_BROKEN_REFERENCE: "迁移后新增断裂",
};

async function renameOnTab() {
  await loadRevisions();
  rn.rev = $("#rn-rev").value || state.rev;
  rnRenderStale();
  if (!rn.staleTimer) rn.staleTimer = setInterval(rnCheckStale, 15000);
}

$("#rn-rev").addEventListener("change", () => {
  rn.rev = $("#rn-rev").value;
  rn.result = null;
  rnClearResult();
  rnRenderStale();
});

function rnMappingsFromRows() {
  return rn.rows
    .filter(r => r.old.trim() || r.neu.trim())
    .map(r => ({ old_key: r.old.trim(), new_key: r.neu.trim() }));
}

function rnRenderRows() {
  const box = $("#rn-rows");
  box.innerHTML = "";
  rn.rows.forEach((row, i) => {
    const div = document.createElement("div");
    div.className = "rn-row";
    div.innerHTML =
      `<span class="rn-idx">${i + 1}</span>
       <input type="text" class="rn-old mono" placeholder="old_key" value="${esc(row.old)}">
       <span class="rn-arrow">→</span>
       <input type="text" class="rn-new mono" placeholder="new_key" value="${esc(row.neu)}">
       <button type="button" class="mini rn-del">✕</button>`;
    const oldI = $(".rn-old", div), newI = $(".rn-new", div);
    oldI.addEventListener("input", () => { row.old = oldI.value; rnSyncJson(); });
    newI.addEventListener("input", () => { row.neu = newI.value; rnSyncJson(); });
    $(".rn-del", div).addEventListener("click", () => {
      rn.rows.splice(i, 1);
      if (!rn.rows.length) rn.rows.push({ old: "", neu: "" });
      rnRenderRows();
    });
    box.appendChild(div);
  });
  rnSyncJson();
}

function rnSyncJson() {
  const m = rnMappingsFromRows();
  $("#rn-json").value = m.length ? JSON.stringify(m, null, 2) : "";
}

$("#rn-add-row").addEventListener("click", () => {
  rn.rows.push({ old: "", neu: "" });
  rnRenderRows();
});

$("#rn-clear").addEventListener("click", () => {
  rn.rows = [{ old: "", neu: "" }];
  rn.result = null;
  rnClearResult();
  rnRenderRows();
});

$("#rn-load-json").addEventListener("click", () => {
  let parsed;
  try {
    parsed = JSON.parse($("#rn-json").value.trim() || "[]");
  } catch (e) {
    rnSetMsg("JSON 解析失败：" + e.message, "err");
    return;
  }
  if (!Array.isArray(parsed)) { rnSetMsg("JSON 必须是映射数组", "err"); return; }
  rn.rows = parsed.map(m => ({
    old: String(m && m.old_key != null ? m.old_key : ""),
    neu: String(m && m.new_key != null ? m.new_key : ""),
  }));
  if (!rn.rows.length) rn.rows.push({ old: "", neu: "" });
  rn.result = null;
  rnClearResult();
  rnRenderRows();
  rnSetMsg(`已载入 ${rn.rows.length} 行映射。`, "");
});

$("#rn-copy-json").addEventListener("click", () => {
  rnSyncJson();
  const ta = $("#rn-json");
  ta.focus(); ta.select();
  navigator.clipboard?.writeText(ta.value).catch(() => {});
});

function rnSetMsg(text, cls) {
  const m = $("#rn-msg");
  m.textContent = text;
  m.className = "msg" + (cls ? " " + cls : "");
}

function rnClearResult() {
  $("#rn-result").hidden = true;
  $("#rn-errors").hidden = true;
  $("#rn-commit").disabled = true;
  $("#rn-download").disabled = true;
}

$("#rn-plan").addEventListener("click", () => rnRunPlan(false));

async function rnRunPlan(isCommit, body) {
  if (!rn.rev) { rnSetMsg("请先选择版本", "err"); return; }
  const mappings = rnMappingsFromRows();
  rnSetMsg(isCommit ? "迁移中（单事务改写）…" : "试运行中（不写库）…", "");
  rnClearResult();
  try {
    let url, payload;
    if (isCommit) {
      url = `/api/revisions/${rn.rev}/rename/commit`;
      payload = body;
    } else {
      url = `/api/revisions/${rn.rev}/rename/plan`;
      payload = { mappings };
    }
    const r = await api(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    rn.committed = isCommit;
    rnRenderResult(r);
    if (isCommit) {
      const kind = r.replayed
        ? "该迁移此前已完成，回放既有结果（幂等，无新增写入），版本 "
        : (r.deduplicated ? "相同计划已存在（幂等），版本 " : "新版本 ");
      rnSetMsg(
        `迁移成功：${kind}${r.new_rev}` +
        `，键改名 ${r.key_renames.length} 项、引用改写 ${r.summary.reference_count} 处。`,
        "ok");
      await refreshAll(r.new_rev);
      rn.rev = r.new_rev;
      $("#rn-rev").value = r.new_rev;
    } else {
      rnSetMsg(`试运行完成：命中 ${r.summary.entry_count} 个条目、` +
        `${r.summary.reference_count} 处引用、风险 ${r.summary.risk_count} 项（未写库）。`, "ok");
    }
    rnRenderStale();
  } catch (err) {
    if (err.status === 400 && err.data && Array.isArray(err.data.errors)) {
      rnRenderErrors(err.data.errors);
      rnSetMsg(`映射校验失败：${err.data.errors.length} 个错误（未写库）。`, "err");
    } else {
      rnSetMsg((isCommit ? "迁移失败（已整体回滚，未做任何更改）：" : "试运行失败：") +
        err.message, "err");
    }
  }
}

function rnRenderErrors(errors) {
  const box = $("#rn-errors");
  box.hidden = false;
  const tbody = $("#rn-error-table tbody");
  tbody.innerHTML = "";
  errors.forEach(e => {
    const tr = document.createElement("tr");
    let extra = "";
    if (e.chain) extra = `<div class="hint mono">${esc(e.chain.join(" → "))}</div>`;
    if (e.locales) extra += `<div class="hint">语言：${esc(e.locales.join(", "))}</div>`;
    tr.innerHTML =
      `<td><span class="sev-error">${esc(RN_ERROR_LABELS[e.code] || e.code)}</span>
         <div class="hint mono">${esc(e.code)}</div></td>
       <td class="mono">${esc(e.old_key || "")}</td>
       <td class="mono">${esc(e.new_key || "")}</td>
       <td>${esc(e.detail || "")}${extra}</td>`;
    tbody.appendChild(tr);
  });
}

function rnRenderResult(r) {
  rn.result = r;
  $("#rn-errors").hidden = true;
  $("#rn-result").hidden = false;
  $("#rn-commit").disabled = rn.committed;
  $("#rn-download").disabled = false;

  const s = r.summary;
  const tag = rn.committed
    ? (r.deduplicated
       ? `<span class="chip warning">幂等提交（产物版本已存在）</span>`
       : `<span class="chip" style="background:#dcfce7;color:var(--ok)">已确认迁移</span>`)
    : `<span class="chip code">试运行（未写库）</span>`;
  $("#rn-meta").innerHTML =
    `${tag}
     <span class="chip code">版本 ${esc(r.rev)}</span>
     <span class="chip code">计划摘要 <code>${esc(r.plan_hash)}</code></span>
     <span class="chip code">映射 ${s.mapping_count}</span>
     <span class="chip code">键改名 ${s.key_count}（条目 ${s.entry_count}）</span>
     <span class="chip code">引用改写 ${s.reference_count}（消息 ${s.message_count}）</span>
     <span class="chip ${s.risk_count ? "error" : ""}"
       style="${s.risk_count ? "" : "background:#dcfce7;color:var(--ok)"}">
       断裂风险 ${s.risk_count}</span>
     <span class="chip code">断裂引用 ${s.broken_before} → ${s.broken_after}</span>` +
    (rn.committed
      ? `<span class="chip" style="background:#dbeafe">${esc(r.from_rev)} → ${esc(r.new_rev)}</span>`
      : `<span class="hint">确认时 expected_rev = <code>${esc(r.rev)}</code>${
          r.revision.is_current ? "" : "（该版本已过期，确认将被拒绝）"}</span>`);

  const ktb = $("#rn-keys-table tbody");
  ktb.innerHTML = "";
  r.key_renames.forEach((k, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${i + 1}</td>
       <td class="mono">${esc(k.old_key)}</td>
       <td class="mono">${esc(k.new_key)}</td>
       <td>${k.locales.map(l => `<span class="chip code">${esc(l)}</span>`).join(" ")}</td>
       <td class="hint">${k.absent_locales.length ? esc(k.absent_locales.join(", ")) : "—"}</td>`;
    ktb.appendChild(tr);
  });

  const rtb = $("#rn-refs-table tbody");
  rtb.innerHTML = "";
  r.reference_rewrites.forEach((x, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${i + 1}</td>
       <td>${esc(x.locale)}</td>
       <td class="mono">${esc(x.key)}</td>
       <td>${x.pos}–${x.end}</td>
       <td class="mono">{@${esc(x.old_key)}} → {@${esc(x.new_key)}}</td>
       <td>${esc(x.resolved_locale)}:${esc(x.resolved_key)}
         <div class="hint">链：${esc(x.chain.join(" → "))}</div></td>`;
    rtb.appendChild(tr);
  });
  if (!r.reference_rewrites.length)
    rtb.innerHTML = '<tr><td colspan="6" class="hint">无需要改写的引用。</td></tr>';

  $("#rn-risk-count").textContent = r.risks.items.length;
  const riskBox = $("#rn-risks");
  riskBox.innerHTML = r.risks.items.length
    ? r.risks.items.map(x => {
        const loc = x.locale, key = x.key;
        let main;
        if (x.kind === "BROKEN_REFERENCE") {
          main = `${esc(loc)}:${esc(key)} → @${esc(x.ref_key)} ✗ 沿链无法解析` +
            (x.renamed_source ? ` <span class="sev-warning">（其引用名在迁移集合中，但引用本就断裂，未改写）</span>` : "") +
            `<br><span class="pv-cause">链：${esc(x.chain.join(" → "))}（位置 ${x.pos}）</span>`;
        } else if (x.kind === "UNPARSEABLE_REFERENCE") {
          main = `${esc(loc)}:${esc(key)} <span class="sev-error">ICU 语法错误</span>，` +
            `疑似引用 @${esc(x.ref_keys.join(", @"))} 未自动改写<br>` +
            `<span class="pv-cause">${esc(x.detail)}（位置 ${x.positions.join(", ")}）</span>`;
        } else {
          main = `${esc(loc)}:${esc(key)} → @${esc(x.ref_key)} ✗ ${esc(x.detail || "")}` +
            `<br><span class="pv-cause">链：${esc((x.chain || []).join(" → "))}</span>`;
        }
        return `<div class="broken-item"><span class="sev-error">${
          esc(RN_RISK_LABELS[x.kind] || x.kind)}</span><br>${main}</div>`;
      }).join("")
    : '<div class="hint">无断裂风险：重命名不会引入新的断裂引用。</div>';
}

$("#rn-commit").addEventListener("click", () => {
  if (!rn.result || rn.committed) return;
  const mappings = rn.result.mappings;
  // expected_rev 必须等于所选版本（后端复核 rev == expected_rev == current_rev）
  const expectedRev = rn.rev;
  if (rn.result.revision && !rn.result.revision.is_current) {
    rnSetMsg("该版本已不是当前版本，请重新试运行后再确认（expected_rev 已过期）。", "err");
    return;
  }
  if (!confirm(
      `确认对版本 ${rn.rev} 执行 ${mappings.length} 条键重命名？\n` +
      `将在单事务中改写 ${rn.result.summary.entry_count} 个条目与 ` +
      `${rn.result.summary.reference_count} 处引用，并生成新不可变版本。\n` +
      (rn.result.risks.summary.total
        ? `注意：存在 ${rn.result.risks.summary.total} 项断裂风险（见下），它们不会被自动改写。\n`
        : "") +
      "失败将整体回滚。")) {
    return;
  }
  rnRunPlan(true, { expected_rev: expectedRev, mappings, actor: "user" });
});

$("#rn-download").addEventListener("click", () => {
  if (!rn.result) return;
  const payload = {
    generated_at: new Date().toISOString(),
    kind: rn.committed ? "rename_commit" : "rename_plan",
    result: rn.result,
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)],
                        { type: "application/json" });
  const a = document.createElement("a");
  const revPart = rn.committed ? r.new_rev : r.rev;
  a.href = URL.createObjectURL(blob);
  a.download = `rename_${revPart}_${r.plan_hash}` +
    (rn.committed ? `_from_${r.from_rev}.json` : "_plan.json");
  a.click();
  URL.revokeObjectURL(a.href);
});

function rnRenderStale() {
  const banner = $("#rn-stale");
  if (!rn.result || !rn.result.revision) { banner.hidden = true; return; }
  const rev = rn.result.revision;
  const stale = !rev.is_current;
  banner.hidden = !stale;
  if (stale) {
    banner.innerHTML = `⚠ 当前试运行结果绑定版本 <b>${esc(rev.rev)}</b>，` +
      `但该版本已不是当前版本（当前：<b>${esc(rev.current_rev)}</b>）。` +
      `快照不可变、计划仍可查阅；<b>确认会因 expected_rev 过期被拒绝（409）</b>，请重新试运行。`;
  }
}

async function rnCheckStale() {
  const panel = $("#tab-rename");
  if (!panel.classList.contains("active") || !rn.result) return;
  try {
    const s = await api("/api/state");
    if (rn.result.revision && rn.result.revision.current_rev !== s.current_rev) {
      rn.result.revision.current_rev = s.current_rev;
      rn.result.revision.is_current = s.current_rev === rn.result.rev;
      rnRenderStale();
    }
  } catch (e) { /* 静默 */ }
}

// 初始化行编辑器
rnRenderRows();

// --------------------------------------------------------------- 启动

(async function init() {
  try {
    const s = await api("/api/state");
    state.rev = s.current_rev;
    await loadRevisions();
    await loadIssues();
    await loadAudit();
  } catch (err) {
    console.error(err);
  }
})();
