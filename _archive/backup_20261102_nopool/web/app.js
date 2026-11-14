const $ = (s) => document.querySelector(s);
const ST = { path: "", proj: null, timer: null, gears: [] };

/* 片段池挖掘挡位（从后端 /api/pool-gears 拉，失败时用这份兜底） */
const GEARS_FALLBACK = [
  { key: "strict", name: "精选", desc: "只留名场面，池子最小", min_sec: 10 },
  { key: "normal", name: "标准", desc: "爆点 + 强情节，约 2 倍", min_sec: 8 },
  { key: "loose", name: "宽松", desc: "有信息量就收，约 3-4 倍", min_sec: 6 },
  { key: "max", name: "极限", desc: "近乎逐句全收", min_sec: 5 },
];

async function loadGears() {
  try {
    ST.gears = await api("/api/pool-gears");
  } catch {
    ST.gears = GEARS_FALLBACK;
  }
  if (!ST.gears.length) ST.gears = GEARS_FALLBACK;
  const optHtml = ST.gears
    .map((g) => `<option value="${g.key}">${g.name} · ${g.desc}（单块 ≥${g.min_sec} 秒）</option>`)
    .join("");
  ["#in-gear", "#in-gear-new"].forEach((sel) => {
    const el = $(sel);
    if (!el) return;
    const keep = el.value;
    el.innerHTML = optHtml;
    if (keep) el.value = keep;
    else el.value = "normal";
  });
}

function gearHint(sel = "#in-gear", hint = "#gear-hint") {
  const key = $(sel).value;
  const g = ST.gears.find((x) => x.key === key);
  const el = $(hint);
  if (!el) return;
  let txt = g
    ? `${g.name}挡：${g.desc} · 单块最短 ${g.min_sec} 秒。块数由剧情决定，不按集均分。挡位越高池子越大，出多套时越不容易雷同（都是一次 API 调用，花费相同）。`
    : "";
  const pool = (ST.proj && ST.proj.pool) || null;
  const segs = pool && pool.segments ? pool.segments : [];
  // 片 ≠ 块：一个块可能跨集拆成多片，也可能由同集几段缝成一块
  const n = new Set(segs.map((s) => s.blk || `${s.ep}_${s.start}`)).size;
  if (n) {
    if (pool.gear && pool.gear !== key) {
      txt += `　当前池子是按「${pool.gear_name || pool.gear}」挡挖的 ${n} 块，点按钮会按「${g ? g.name : key}」挡重挖并覆盖。`;
    } else {
      txt += `　当前池子就是这个挡位挖的（${n} 块），再挖一次会覆盖。`;
    }
  }
  el.textContent = txt;
}

async function api(url, opts = {}) {
  const r = await fetch(url, { headers: { "Content-Type": "application/json" }, ...opts });
  const txt = await r.text();
  let data;
  try { data = JSON.parse(txt); } catch { data = { detail: txt.slice(0, 200) }; }
  if (!r.ok) throw new Error(data.detail || "HTTP " + r.status);
  return data;
}

let toastTimer = null;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("on"), 3200);
}

function fmt(sec) {
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

async function boot() {
  const st = await api("/api/status");
  ST.status = st;
  $("#pills").innerHTML = [
    `<span class="pill ${st.llm_key ? "ok" : "bad"}">模型 ${st.model}</span>`,
    `<span class="pill ${st.whisper_model ? "ok" : "bad"}">转写 ${st.whisper_tier}·${st.whisper_model ? "就绪" : "缺模型"}</span>`,
    `<span class="pill ${st.llm_key ? "ok" : "bad"}">Key ${st.llm_key ? "已配置" : "未配置"}</span>`,
  ].join("");

  const roots = await api("/api/roots");
  renderDrives(roots);
  if (roots.length) await browse(roots[0].path);
  else $("#crumb").textContent = "没有找到任何磁盘";
  await loadGears();
  try {
    const cfg = await api("/api/config");
    ST.cfgOutput = (cfg.paths && cfg.paths.output) || "";
    if (!$("#save-dir").value && ST.cfgOutput) {
      $("#save-dir").value = ST.cfgOutput.replace(/\\/g, "/");
    }
  } catch (_) { /* 配置读不到不影响主流程 */ }
  updateModeHint();
  await loadList();
}

async function loadList() {
  const rows = await api("/api/projects");
  const modeName = (m) => m === "batch" ? "批量混剪" : "单片导流";
  $("#plist").innerHTML = rows.length
    ? rows.map((r) => `
      <div class="pitem ${ST.proj && ST.proj.id === r.id ? "on" : ""}" data-id="${r.id}" title="${esc(r.path || "")}">
        <b>${esc(r.name)}</b>
        <span>${modeName(r.mode)} · ${r.episodes} 集${r.output ? " · 已出片" : ""}</span>
        <span class="ppath">${esc(r.path || "")}</span>
      </div>`).join("")
    : `<div class="hint">还没有项目</div>`;
  document.querySelectorAll(".pitem").forEach((el) =>
    el.addEventListener("click", () => openProject(el.dataset.id)));
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* 盘符切换：本机可能有多个盘，素材常常不在系统盘上 */
function renderDrives(roots) {
  $("#drives").innerHTML = roots.length
    ? roots.map((r) => `<button class="drive" data-path="${esc(r.path)}">${esc(r.label)}</button>`).join("")
    : "";
  document.querySelectorAll(".drive").forEach((el) =>
    el.addEventListener("click", () => browse(el.dataset.path)));
}

function markDrive(path) {
  const cur = String(path || "").slice(0, 2).toLowerCase();
  document.querySelectorAll(".drive").forEach((el) =>
    el.classList.toggle("on", String(el.dataset.path || "").slice(0, 2).toLowerCase() === cur));
}

async function browse(path) {
  const d = await api("/api/browse?path=" + encodeURIComponent(path));
  ST.path = d.path;
  $("#crumb").textContent = d.win;
  markDrive(d.path);
  $("#dirs").innerHTML = d.dirs.length
    ? d.dirs.map((x) => `
      <div class="drow" data-path="${esc(x.path)}">
        <span class="n">${esc(x.name)}</span>
        <span class="badge ${x.videos ? "" : "zero"}">${x.videos ? x.videos + " 个视频" : "空"}</span>
      </div>`).join("")
    : `<div class="hint" style="padding:12px 6px">这个目录下没有子目录</div>`;
  document.querySelectorAll(".drow").forEach((el) =>
    el.addEventListener("click", () => browse(el.dataset.path)));
  $("#btn-up").disabled = !d.parent;
  $("#btn-up").dataset.parent = d.parent || "";
  $("#btn-use").disabled = d.videos === 0;
  $("#pick-hint").textContent = d.videos
    ? `当前目录有 ${d.videos} 个视频`
    : "当前目录没有视频，进入有视频的子目录";
  if (d.videos) $("#in-name").placeholder = d.path.split("/").pop();
}

/* ---------- 新建 ---------- */
$("#btn-new").addEventListener("click", () => {
  const was = ST.proj;
  const busy = was && was.task && !was.task.error;
  ST.proj = null;
  $("#gbar").classList.add("hidden");
  $("#view-new").classList.remove("hidden");
  $("#view-proj").classList.add("hidden");
  loadList();
  if (busy) toast(`「${was.name}」还在后台跑，回到那个项目能看到进度`);
});

const MODE_HINTS = {
  funnel: "单片导流：AI 排「钩子→按剧情推进→悬念收尾」的结构表，你确认后出一条片。",
  batch: "批量混剪：AI 挖一次全剧精彩片段池，你勾选后再由本机批量组装出 N 套，每套时长可不同。",
};

/* 新建面板：模式 + 挖片挡位 的说明合在一行提示里 */
function updateModeHint() {
  const m = $("#in-mode").value;
  const el = $("#mode-hint");
  if (m !== "batch") {
    el.textContent = MODE_HINTS.funnel;
    return;
  }
  const g = ST.gears.find((x) => x.key === $("#in-gear-new").value);
  el.textContent = g
    ? `批量混剪：AI 挖一次片段池（挡位「${g.name}」——${g.desc}，单段最短 ${g.min_sec} 秒），你勾选后本机组装 N 套。挡位越高池子越大，出多套越不容易雷同（API 都是一次调用，花费不变）。`
    : MODE_HINTS.batch;
}

$("#in-mode").addEventListener("change", () => {
  const m = $("#in-mode").value;
  $("#target-wrap").style.display = m === "funnel" ? "" : "none";
  $("#gear-wrap-new").classList.toggle("hidden", m !== "batch");
  updateModeHint();
});

$("#in-gear-new").addEventListener("change", updateModeHint);

$("#btn-up").addEventListener("click", () => {
  const p = $("#btn-up").dataset.parent;
  if (p) browse(p);
});

$("#btn-use").addEventListener("click", () => {
  $("#pick-hint").textContent = "已选用：" + $("#crumb").textContent;
  $("#btn-use").classList.add("primary");
  toast("已选中目录，选好模式后点「扫描并创建」");
});

$("#btn-create").addEventListener("click", async () => {
  if (!ST.path) return toast("先选一个目录");
  const btn = $("#btn-create");
  btn.disabled = true;
  btn.textContent = "扫描中…";
  try {
    const p = await api("/api/projects", {
      method: "POST",
      body: JSON.stringify({
        path: ST.path,
        name: $("#in-name").value.trim(),
        target_minutes: Number($("#in-target").value) || 10,
        mode: $("#in-mode").value,
        gear: $("#in-gear-new").value || "normal",
      }),
    });
    const gname = (ST.gears.find((x) => x.key === $("#in-gear-new").value) || {}).name || "";
    toast(p.mode === "batch"
      ? `已创建（批量混剪）：${p.episodes.length} 集 — 正在自动转写，转完按「${gname}」挡挖片段池`
      : `已创建（单片导流）：${p.episodes.length} 集 — 正在自动转写，转完可以「让 AI 出表」`);
    await loadList();
    await openProject(p.id);
  } catch (e) {
    toast("创建失败：" + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "扫描并创建";
  }
});

/* ---------- 项目 ---------- */
async function openProject(id) {
  ST.proj = await api("/api/projects/" + id);
  $("#view-new").classList.add("hidden");
  $("#view-proj").classList.remove("hidden");
  // 保存位置：优先这个项目上次用的，其次全局「成片保存位置」
  const lastDir = (ST.proj.batch && ST.proj.batch.save_dir) || ST.cfgOutput || "";
  if (lastDir) $("#save-dir").value = lastDir.replace(/\\/g, "/");
  updateSaveDirNote();
  render();
  loadList();
  // 进了项目就自动转写：只补从没转过的集；已失败的集不自动重试，免得每次打开都空跑一遍
  const cur = ST.proj;
  if (!cur.task && cur.episodes.some((e) => e.status === "pending")) {
    await autoTranscribe(cur.id);
  }
  const p = ST.proj;
  const poolDone = !!((p.pool || {}).segments || []).length;
  if (p.task || (p.mode === "batch" && !poolDone)) startPoll(); else stopPoll();
}

/* 静默开跑：不弹提示；409（已有任务在跑）或项目刚被删都直接忽略 */
async function autoTranscribe(id) {
  try {
    await api(`/api/projects/${id}/transcribe`, { method: "POST" });
    ST.proj = await api("/api/projects/" + id);
    render();
  } catch (e) { /* 忽略 */ }
}

const TASK_FALLBACK = {
  transcribe: "正在转写台词", pool: "正在挖片段池", plan: "正在组装结构",
  analyze: "正在让 AI 出结构表", render: "正在出片", batch: "正在批量出片",
};

function fmtElapsed(sec) {
  const s = Math.max(0, Math.round(sec || 0));
  if (s < 60) return `${s} 秒`;
  return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
}

/* 顶部状态条：有任务时显示「在做什么 + 进度 + 已用时间」，没有就收起 */
function updateBar(t) {
  const bar = $("#gbar");
  if (!t || t.error) {
    bar.classList.add("hidden");
    return;
  }
  const pct = Math.max(2, Math.min(100, Math.round(t.pct || 0)));
  bar.classList.remove("hidden");
  $("#gbar-fill").style.width = pct + "%";
  const parts = [t.msg || TASK_FALLBACK[t.kind] || "处理中"];
  if ((t.elapsed || 0) >= 1) parts.push(`已用 ${fmtElapsed(t.elapsed)}`);
  parts.push(`${pct}%`);
  $("#gbar-text").textContent = parts.join(" · ");
}

function render() {
  const p = ST.proj;
  if (!p) {
    $("#gbar").classList.add("hidden");
    return;
  }
  updateBar(p.task);
  const isBatch = p.mode === "batch";
  $("#funnel-steps").classList.toggle("hidden", isBatch);
  $("#batch-steps").classList.toggle("hidden", !isBatch);

  const dur = p.episodes.reduce((a, e) => a + (e.duration || 0), 0);
  const bad = p.episodes.filter((e) => e.status === "error").length;
  $("#p-name").textContent = p.name;
  $("#p-path").textContent = "素材：" + (p.win_dir || p.src_dir || "—")
    + "　·　模式：" + (isBatch ? "批量混剪" : "单片导流");
  const b = p.batch || {};
  const durText = isBatch
    ? (b.min_minutes ? `每套 ${b.min_minutes}~${b.max_minutes} 分钟` : "每套时长在下方批量面板设置")
    : `目标 ${p.target_minutes} 分钟`;
  $("#p-meta").textContent =
    `${p.episodes.length} 集 · 素材总时长 ${fmt(dur)} · ${durText} · ${p.line_total} 句台词` +
    (bad ? ` · ${bad} 集转写失败` : "");

  const t = p.task;
  const busy = !!t && !t.error;
  const setState = (sel, txt) => ($(sel).textContent = txt);

  const done = p.episodes.filter((e) => e.status === "done").length;
  const autoBusy = t && (t.kind === "transcribe" || t.kind === "pool") && !t.error;
  setState("#s1-state", t && t.kind === "transcribe" ? `${t.pct}% · ${t.msg}`
    : done === p.episodes.length ? (isBatch && autoBusy ? "自动完成" : `已完成 ${done}/${p.episodes.length} 集`)
    : done ? `已完成 ${done}/${p.episodes.length} 集` : "未开始");

  if (isBatch) {
    const segs = p.pool ? (p.pool.segments || []) : [];
    const poolTotal = segs.reduce((a, s) => a + (s.end - s.start), 0);
    const gearName = (p.pool && p.pool.gear_name) || "";
    setState("#b2-state", t && t.kind === "pool" ? `${t.pct}% · ${t.msg}`
      : segs.length ? `${gearName ? gearName + "挡 · " : ""}片段池 ${segs.length} 段 · 合计 ${fmt(poolTotal)}` : "未开始");
    const plans = p.plans || [];
    setState("#b3-state", t && t.kind === "plan" ? `${t.pct}% · ${t.msg}`
      : plans.length ? `已出 ${plans.length} 套结构` : "未开始");
    const curGen = Number((p.batch && p.batch.gen) || p.plan_gen || 0);
    const sets = (p.sets || []).filter((s) => Number(s.gen || 0) === curGen);
    const fails = (p.batch || {}).failures || [];
    const failTxt = fails.length && !(t && t.kind === "batch")
      ? ` · ${fails.length} 套失败` : "";
    setState("#b4-state", t && t.kind === "batch" ? `${t.pct}% · ${t.msg}`
      : sets.length || fails.length
        ? `已出片 ${sets.filter((s) => s.output).length}/${(p.plans || []).length || sets.length} 套${failTxt}`
        : "未开始");
  } else {
    setState("#s2-state", t && t.kind === "analyze" ? `${t.pct}% · ${t.msg}`
      : p.edl.length ? `${p.edl.length} 段 · ${fmt(p.edl_total)}` : "未开始");
    setState("#s3-state", t && t.kind === "render" ? `${t.pct}% · ${t.msg}`
      : p.output ? `${fmt(p.output_duration || 0)} · ${((p.output_size || 0) / 1048576).toFixed(0)}MB` : "未开始");
  }

  $("#btn-transcribe").disabled = busy || done === p.episodes.length;
  $("#transcript-links").classList.toggle("hidden", !p.line_total);
  $("#btn-transcript").href = `/api/projects/${p.id}/transcript`;
  $("#btn-transcript-dl").href = `/api/projects/${p.id}/transcript?download=1`;
  $("#btn-del").disabled = busy;

  if (isBatch) {
    $("#btn-pool").disabled = busy;
    $("#in-gear").disabled = busy;
    $("#btn-pool").textContent = (p.pool && (p.pool.segments || []).length)
      ? "按此挡位重挖" : "AI 挖片段池";
    // 回填这个池子是用哪个挡位挖的（每个项目只回填一次，避免盖掉用户刚选的）
    const curGear = (p.pool && p.pool.gear) || p.pool_gear;
    if (ST.gearFor !== p.id) {
      ST.gearFor = p.id;
      if (curGear && ST.gears.some((g) => g.key === curGear)) $("#in-gear").value = curGear;
    }
    gearHint();
    $("#btn-save-pool").disabled = busy;
    if (document.activeElement !== $("#pool")) $("#pool").value = p.pool_text || "";
    // 回填上次批量设置的套数和时长范围
    if (document.activeElement !== $("#in-count") && b.count) $("#in-count").value = b.count;
    if (document.activeElement !== $("#in-min") && b.min_minutes) $("#in-min").value = b.min_minutes;
    if (document.activeElement !== $("#in-max") && b.max_minutes) $("#in-max").value = b.max_minutes;
    if (document.activeElement !== $("#in-order") && b.order) $("#in-order").value = b.order;
    renderPoolList();
    renderPlans();
    renderSets();
  } else {
    $("#btn-analyze").disabled = busy || done === 0;
    $("#btn-render").disabled = busy || !p.edl.length;
    $("#btn-save-edl").disabled = busy;
    if (document.activeElement !== $("#edl")) $("#edl").value = p.edl_text || "";
    $("#p-note").textContent = p.notes || "";
    updateStat();
  }

  if (t && t.error) {
    const m = { transcribe: "#s1-state", analyze: "#s2-state", render: "#s3-state",
                pool: "#b2-state", plan: "#b3-state", batch: "#b4-state" };
    setState(m[t.kind] || "#s1-state", "失败：" + t.error.slice(0, 120));
  }

  if (!isBatch) {
    const wrap = $("#out-wrap");
    if (p.output) {
      wrap.classList.remove("hidden");
      const url = `/api/projects/${p.id}/video?v=${p.output}`;
      if ($("#out-video").getAttribute("src") !== url) $("#out-video").setAttribute("src", url);
      $("#btn-download").setAttribute("href", url);
      $("#btn-download").setAttribute("download", p.output);
    } else {
      wrap.classList.add("hidden");
    }
  }
}

/* ---------- 剪辑结构（N 套）：勾选视图 ---------- */
function renderPlans() {
  const p = ST.proj;
  const plans = (p && p.plans) || [];
  const list = $("#plan-list");
  if (!plans.length) {
    list.innerHTML = `<div class="hint" style="padding:8px 0">还没有结构，点上面「生成 N 套结构」（本机组装，不花钱，几秒钟）</div>`;
    $("#plan-stat").textContent = "";
    $("#plan-note").textContent = "";
    $("#btn-render-plans").textContent = "出片（已选 0 套）";
    $("#btn-render-plans").disabled = true;
    return;
  }
  list.innerHTML = plans.map((pl, i) => {
    const epDur = {};
    (ST.proj.episodes || []).forEach((e) => (epDur[e.index] = e.duration || 0));
    const rows = (pl.edl || []).map((s, k) => `
      <div class="pl-row">
        <span class="pc-seq">${k + 1}</span>
        <span class="pc-num">${String(s.ep).padStart(2, "0")}</span>
        ${posBar(s, epDur)}
        <span class="pc-dur">${fmt(s.end - s.start)}</span>
        <span class="pc-rate r${Math.min(10, Math.max(1, Math.round(s.rating || 5)))}">${Math.round(s.rating || 5)}分</span>
        <span class="pc-label">${esc(s.label || "")}</span>
      </div>`).join("");
    // 节奏体检：跳切多不等于差——跳过过程本来就是好剪辑。真正该看的是
    // 平均块长：一块 10-65 秒约等于「一场戏」，超过 65 秒像在搬整集，低于 10 秒又太碎。
    // （2026-11-02 调整：一场交锋常常就占一整集 40-60 秒，60 秒左右是「完整」不是「搬整集」）
    const perMin = pl.jumps != null ? pl.jumps / Math.max(pl.sec / 60, 1) : 0;
    const ab = pl.avg_block || 0;
    const tempo = !ab ? "ok" : (ab > 65 || ab < 10) ? "mid" : "ok";
    const tempoTip = (!ab ? "" : ab > 65 ? "块偏长，接近搬整集；" : ab < 10 ? "块偏碎；" : "节奏正常；")
      + (pl.avg_gap ? `平均每次跳过 ${Math.round(pl.avg_gap)} 秒；` : "")
      + `每分钟跳切 ${perMin.toFixed(1)} 次（跳过过程，正常）`;
    return `
      <div class="plan ${pl.on === false ? "off" : ""}" data-i="${i}">
        <label class="plan-head">
          <input type="checkbox" ${pl.on === false ? "" : "checked"}>
          <b>第 ${pl.index} 套</b>
          <span class="hint">${(pl.edl || []).length} 片${pl.blocks ? ` / ${pl.blocks} 块` : ""} · ${fmt(pl.sec || 0)}${pl.eps && pl.eps.length ? ` · 第 ${pl.eps[0]}–${pl.eps[1]} 集` : ""}${pl.style ? ` · ${esc(pl.style)}` : ""}</span>
          ${pl.jumps != null ? `<span class="ov ${tempo}" title="${tempoTip}">跳切 ${pl.jumps} 次${ab ? ` · 均片 ${ab}s` : ""}</span>` : ""}
          ${pl.overlap ? `<span class="ov ${pl.overlap > MAX_OVERLAP ? "bad" : pl.overlap > MAX_OVERLAP * 0.55 ? "mid" : "ok"}">与前面重合 ${Math.round(pl.overlap * 100)}%</span>` : ""}
          <span class="plan-more">展开表 ▾</span>
        </label>
        <div class="plan-body hidden">
          ${pl.order_desc ? `<div class="pl-order">顺序：${esc(pl.order_desc)}${pl.seq ? ` · 集号走位 ${esc(pl.seq)}` : ""}</div>` : ""}
          ${rows}
        </div>
      </div>`;
  }).join("");

  list.querySelectorAll(".plan").forEach((el) => {
    const i = Number(el.dataset.i);
    el.querySelector('input[type="checkbox"]').addEventListener("change", (e) => {
      e.stopPropagation();
      onPlanToggle(i, e.target.checked);
    });
    el.querySelector(".plan-head").addEventListener("click", (e) => {
      if (e.target.tagName === "INPUT") return;
      const body = el.querySelector(".plan-body");
      body.classList.toggle("hidden");
      el.querySelector(".plan-more").textContent = body.classList.contains("hidden") ? "展开表 ▾" : "收起 ▴";
    });
  });
  $("#plan-note").textContent = (p.batch && p.batch.plan_note) || "";
  updatePlanFeasible();
  updatePlanStat();
}

async function onPlanToggle(i, on) {
  const p = ST.proj;
  p.plans[i].on = on;
  const row = document.querySelector(`.plan[data-i="${i}"]`);
  if (row) row.classList.toggle("off", !on);
  updatePlanStat();
  try {
    await api(`/api/projects/${p.id}/plans-select`, {
      method: "PUT",
      body: JSON.stringify({ on: p.plans.map((pl) => pl.on !== false) }),
    });
  } catch (e) {
    toast("勾选保存失败：" + e.message);
  }
}

function updatePlanStat() {
  const plans = (ST.proj && ST.proj.plans) || [];
  const on = plans.filter((pl) => pl.on !== false);
  const sec = on.reduce((a, pl) => a + (pl.sec || 0), 0);
  $("#plan-stat").textContent = plans.length
    ? `已选 ${on.length}/${plans.length} 套 · 合计时长 ${fmt(sec)}（出片约 ${Math.max(1, Math.round(on.length * 2))} 分钟）`
    : "";
  const btn = $("#btn-render-plans");
  btn.textContent = `出片（已选 ${on.length} 套）`;
  const busy = !!(ST.proj && ST.proj.task && !ST.proj.task.error);
  btn.disabled = busy || !on.length;
}

async function planSelectAll(on) {
  const p = ST.proj;
  const plans = p.plans || [];
  if (!plans.length) return;
  plans.forEach((pl) => (pl.on = on));
  renderPlans();
  try {
    await api(`/api/projects/${p.id}/plans-select`, {
      method: "PUT",
      body: JSON.stringify({ on: plans.map(() => on) }),
    });
  } catch (e) {
    toast("保存失败：" + e.message);
  }
}

/* ---------- 片段池：只读列表（不勾选，AI 圈出什么就全用） ---------- */

/* 片段在整集里的位置条：一条代表整集长度的小横轨，块画在对应位置上。
   不写「36.2-49.3s」这种数字——块在集内哪里一眼可见；精确秒数放 title 悬停。 */
function posBar(s, epDur) {
  const total = epDur[s.ep] || s.end * 1.25 || 100; // 缺时长时兜底，别让条画满
  const left = Math.min(100, (s.start / total) * 100);
  const width = Math.max(2, Math.min(100 - left, ((s.end - s.start) / total) * 100));
  return `<span class="pc-bar" title="第${s.ep}集 ${s.start.toFixed(1)}s–${s.end.toFixed(1)}s（全集 ${fmt(total)}）">
    <span class="pc-bar-hit" style="left:${left.toFixed(1)}%;width:${width.toFixed(1)}%"></span>
  </span>`;
}

function renderPoolList() {
  const p = ST.proj;
  const segs = (p && p.pool && p.pool.segments) || [];
  const list = $("#pool-list");
  if (!segs.length) {
    list.innerHTML = `<div class="hint" style="padding:6px 0">还没有片段池，点上面「AI 挖片段池」</div>`;
    return;
  }
  // 跨集块会被拆成多片（同一 blk），第二片起标「↳续」，让人知道这是同一段剧情的延续。
  // 池子只读不勾选：AI 圈出什么就全部进池，用户不再左右挑选。
  // 位置用一条代表整集长度的横条表示，精确秒数放悬停提示。
  const epDur = {};
  (p.episodes || []).forEach((e) => (epDur[e.index] = e.duration || 0));
  list.innerHTML = segs.map((s, i) => {
    const cont = i > 0 && s.blk && s.blk === segs[i - 1].blk;
    return `
    <div class="pool-row" data-i="${i}">
      <span class="pc-num">${cont ? "↳" : String(s.ep).padStart(2, "0")}</span>
      ${posBar(s, epDur)}
      <span class="pc-dur">${fmt(s.end - s.start)}</span>
      <span class="pc-rate r${Math.min(10, Math.max(1, Math.round(s.rating || 5)))}">${Math.round(s.rating || 5)}分</span>
      <span class="pc-label">${esc(s.label || "")}${cont ? "（跨集续）" : ""}</span>
    </div>`;
  }).join("");
  updatePoolStat();
}

function updatePoolStat() {
  const p = ST.proj;
  if (!p) return;
  const textMode = !$("#pool").classList.contains("hidden");
  if (textMode) {
    const { n, total } = _tableStat($("#pool").value);
    $("#pool-stat").textContent = n
      ? `文本模式：${n} 段 · 合计 ${fmt(total)}（保存后回到列表）`
      : "还没有片段";
    return;
  }
  const segs = (p.pool && p.pool.segments) || [];
  if (!segs.length) { $("#pool-stat").textContent = "还没有片段"; return; }
  const total = segs.reduce((a, s) => a + (s.end - s.start), 0);
  const blocks = new Set(segs.map((s, i) => s.blk || "b" + i)).size;
  $("#pool-stat").textContent =
    `共 ${segs.length} 片 / ${blocks} 块 · 合计 ${fmt(total)}（全部用于组装）`;
}

/* 列表 / 文本编辑 视图切换 */
$("#btn-pool-toggle").addEventListener("click", () => {
  const toText = $("#pool").classList.contains("hidden");
  $("#pool").classList.toggle("hidden", !toText);
  $("#pool-list").classList.toggle("hidden", toText);
  $("#btn-save-pool").classList.toggle("hidden", !toText);
  $("#btn-pool-toggle").textContent = toText ? "返回列表" : "文本编辑";
  updatePoolStat();
});

$("#btn-open-folder").addEventListener("click", async () => {
  const p = ST.proj;
  if (!p) return;
  try {
    const r = await api(`/api/projects/${p.id}/open-folder`, { method: "POST" });
    toast(`已打开：${r.path}`);
  } catch (e) {
    toast(`打不开文件夹：${e.message}`);
  }
});

/* 用系统默认播放器打开某一套（不在浏览器里开页面） */
$("#sets-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-play]");
  if (!btn) return;
  try {
    await api(`/api/projects/${ST.proj.id}/open-video?set=${btn.dataset.play}`, { method: "POST" });
  } catch (err) {
    toast(err.message);
  }
});

function renderSets() {
  const p = ST.proj;
  if (!p) return;
  // 只认当前结构版本的成片记录：结构重生成后，旧 gen 的记录虽然文件还在，
  // 但拍的是上一版结构，混进列表里会让人以为「这套已经出好了」。
  const curGen = Number((p.batch && p.batch.gen) || p.plan_gen || 0);
  const cur = {};
  const stale = [];
  (p.sets || []).forEach((s) => {
    if (Number(s.gen || 0) === curGen) cur[s.index] = s;
    else if (s.output) stale.push(s);
  });
  const planIdx = (p.plans || []).filter((pl) => pl.on !== false).map((pl) => pl.index);
  const rows = (planIdx.length ? planIdx : Object.keys(cur).map(Number))
    .map((i) => ({ index: i, ...(cur[i] || {}) }));
  const sets = rows;
  const ready = sets.filter((s) => s.output).length;
  const totalMb = sets.reduce((a, s) => a + (s.output_size || 0), 0) / 1048576;
  const bdir = p.batch && p.batch.save_dir ? joinDir(p.batch.save_dir, p.name) : "";
  const actions = $("#sets-actions");
  actions.style.display = ready ? "" : "none";
  if (ready) {
    $("#btn-open-folder").textContent = `📂 打开成片文件夹（${ready} 套 · ${(totalMb / 1024).toFixed(1)}GB）`;
    $("#sets-folder-note").textContent = (bdir
      ? `整批都在 ${bdir.replace(/\\/g, "/")}`
      : "整批成片都在同一个文件夹里")
      + (stale.length ? `　另有 ${stale.length} 个旧结构的成片记录（文件仍在原目录，不在本列表里）` : "");
  }
  const fails = ((p.batch || {}).failures || [])
    .filter((f) => !sets.some((s) => s.index === f.index && s.output));
  const failHtml = fails.length
    ? fails.map((f) => `<div class="setrow" style="opacity:.75">
          <b>第 ${f.index} 套</b>
          <span class="hint" style="color:#b45309">失败：${esc((f.error || "").slice(0, 80))}</span>
          <span class="hint">重新点「出片」会自动补做</span>
        </div>`).join("")
    : "";
  $("#sets-list").innerHTML = (sets.length || failHtml)
    ? sets.map((s) => {
        return `<div class="setrow">
          <b>第 ${s.index} 套</b>
          <span class="hint">${s.output ? `${(s.edl || []).length} 段 · ${fmt(s.output_duration || 0)} · ${((s.output_size || 0) / 1048576).toFixed(0)}MB` : "按当前结构待生成"}</span>
          ${s.output
            ? `<button class="ghost" data-play="${s.index}">用播放器打开 ↗</button>`
            : `<span class="hint">待生成</span>`}
        </div>`;
      }).join("") + failHtml
    : `<div class="hint" style="padding:6px 0">还没有批量产物</div>`;
}
function _tableStat(text) {
  const rows = text.split("\n").map((l) => l.trim())
    .filter((l) => l && !l.startsWith("#") && !l.startsWith("集"));
  let n = 0, total = 0;
  for (const r of rows) {
    const parts = r.split(/[|｜,，\t]/);
    if (parts.length < 3) continue;
    const a = parseFloat(parts[1]), b = parseFloat(parts[2]);
    if (!isFinite(a) || !isFinite(b) || b <= a) continue;
    n++; total += b - a;
  }
  return { n, total };
}

function updateStat() {
  const { n, total } = _tableStat($("#edl").value);
  const target = (ST.proj?.target_minutes || 10) * 60;
  const diff = total - target;
  $("#edl-stat").textContent = `${n} 段 · 合计 ${fmt(total)} · 目标 ${fmt(target)}` +
    (n ? `（${diff >= 0 ? "+" : ""}${diff.toFixed(0)} 秒）` : "");
}

function updatePoolStat() {
  const { n, total } = _tableStat($("#pool").value);
  $("#pool-stat").textContent = n
    ? `${n} 段 · 合计 ${fmt(total)}（删除不要的行，改完点「保存片段池」）`
    : "还没有片段";
}

$("#edl").addEventListener("input", updateStat);
$("#pool").addEventListener("input", updatePoolStat);

function startPoll() {
  stopPoll();
  let idle = 0;
  ST.timer = setInterval(async () => {
    try {
      ST.proj = await api("/api/projects/" + ST.proj.id);
      render();
      if (!ST.proj.task || ST.proj.task.error) {
        idle += 1;
        if (idle >= 3) { stopPoll(); loadList(); }  // 连续几拍都没任务才停，避免刚启动时误判
      } else {
        idle = 0;
      }
    } catch (e) { /* 忽略瞬时失败 */ }
  }, 1500);
}
function stopPoll() {
  if (ST.timer) clearInterval(ST.timer);
  ST.timer = null;
}

async function trigger(kind) {
  const p = ST.proj;
  try {
    await api(`/api/projects/${p.id}/${kind}`, { method: "POST" });
    ST.proj.task = { kind, pct: 0, msg: "启动中", error: "" };
    render();
    startPoll();
  } catch (e) {
    toast(e.message);
  }
}

$("#btn-transcribe").addEventListener("click", () => trigger("transcribe"));
$("#btn-analyze").addEventListener("click", () => trigger("analyze"));
$("#btn-render").addEventListener("click", () => trigger("render"));

$("#in-gear").addEventListener("change", () => gearHint());

/* 挖 / 重挖片段池（按所选挡位） */
$("#btn-pool").addEventListener("click", async () => {
  const p = ST.proj;
  const gear = $("#in-gear").value || "normal";
  const g = ST.gears.find((x) => x.key === gear) || {};
  const has = ((p.pool || {}).segments || []).length;
  const cur = (p.pool && p.pool.gear) || "";
  if (has && cur !== gear &&
      !confirm(`按「${g.name}」挡重挖会覆盖现有 ${has} 段片段池（勾选状态会重置，已出的片不受影响）。继续？（约几分钱，一次调用）`)) return;
  if (has && cur === gear &&
      !confirm(`再按「${g.name}」挡挖一次会覆盖现有 ${has} 段片段池（勾选状态会重置，已出的片不受影响）。继续？（约几分钱，一次调用）`)) return;
  try {
    await api(`/api/projects/${p.id}/pool`, {
      method: "POST",
      body: JSON.stringify({ gear }),
    });
    ST.proj.task = { kind: "pool", pct: 0, msg: `启动中（${g.name}挡）`, error: "" };
    render();
    startPoll();
  } catch (e) {
    toast(e.message);
  }
});

$("#btn-save-pool").addEventListener("click", async () => {
  try {
    const r = await api(`/api/projects/${ST.proj.id}/pool`, {
      method: "PUT",
      body: JSON.stringify({ text: $("#pool").value }),
    });
    toast(`片段池已保存：${r.segments} 段 / ${fmt(r.total)}`);
    ST.proj = await api("/api/projects/" + ST.proj.id);
    render();
  } catch (e) {
    toast("保存失败：" + e.message);
  }
});

/* 第 3 步：生成 N 套结构（本机，不花钱） */

/* 套间重合度上限，必须与后端 app/batch.py 的 MAX_OVERLAP 保持一致。
   放在这里是为了让下面的文案和算法都能先引用到（const 有 TDZ，晚定义会报错）。 */
const MAX_OVERLAP = 0.7;

const ORDER_HINTS = {
  auto: "自动轮换：每套用不同的叙事结构（纯集序 / 最炸开场 / 最炸收尾 / 首尾双爆点 / 后段冷开场再回溯 / 两幕爆发），可以改时序但都有逻辑，不会乱跳。",
  chrono: "全部严格集序：所有套都按集号从小到大播，最稳，适合怕观众看乱的场合。",
  varied: "全部变序开场：每套开头都换一种抓人方式（最炸段 / 后段冲突冷开场 / 两幕爆发），但正文仍按集序推进，不会乱跳。",
};
function refreshOrderHint() {
  const base = ORDER_HINTS[$("#in-order").value] || ORDER_HINTS.auto;
  $("#order-hint").textContent =
    `${base}套间重合度控制在 ${Math.round(MAX_OVERLAP * 100)}% 以内，素材池撑不起会提示。`;
}
$("#in-order").addEventListener("change", refreshOrderHint);
refreshOrderHint();

/* 实时可行性提示：按当前片段池和时长设置，重合度 ≤MAX_OVERLAP 时大约能出几套。
   跟后端 _max_sets_heuristic 同一套算法，让用户点按钮之前就知道上限在哪。 */
function updatePlanFeasible() {
  const p = ST.proj;
  const el = $("#plan-feasible");
  const btn = $("#btn-plan");
  const count = Math.max(1, Number($("#in-count").value) || 3);
  btn.textContent = `生成 ${count} 套结构`;
  if (!p || !el) return;
  const segs = (p.pool && p.pool.segments) || [];
  if (!segs.length) {
    el.textContent = "片段池还是空的——先在第 2 步挖池，这里会告诉你大概能出几套。";
    el.classList.remove("warn");
    return;
  }
  const poolSec = segs.reduce((a, s) => a + (s.end - s.start), 0);
  const poolMin = poolSec / 60;
  const mn = Number($("#in-min").value) || 3;
  const mx = Number($("#in-max").value) || 7;
  const lo = Math.min(mn, mx), hi = Math.max(mn, mx);
  const divRatio = count <= 2 ? 0.92 : count <= 5 ? 0.62 : 0.55;
  const capMax = Math.max(lo, Math.min(hi, poolMin * divRatio));
  const tSec = (lo + capMax) / 2 * 60; // 平均每套秒数
  const r = poolSec / tSec, c = MAX_OVERLAP / 2;
  const disc = (1 + c) ** 2 - 4 * c * r;
  const est = disc < 0 ? 0 : Math.max(0, Math.floor(((1 + c) - Math.sqrt(disc)) / (2 * c)));
  const base = `片段池 ${segs.length} 段 · 共 ${poolMin.toFixed(1)} 分钟；按每套 ${lo}~${capMax.toFixed(1)} 分钟、重合度 ≤${Math.round(MAX_OVERLAP * 100)}%`;
  if (est && count > est) {
    el.textContent = `${base}，估计最多能出约 ${est} 套 —— 你设的 ${count} 套大概率组不满，建议调小套数或压低时长。`;
    el.classList.add("warn");
  } else if (est) {
    el.textContent = `${base}，估计最多能出约 ${est} 套，设 ${count} 套没问题。`;
    el.classList.remove("warn");
  } else {
    el.textContent = `${base}，池子相对充裕；若实际组不满会自动提示并保留能出的套。`;
    el.classList.remove("warn");
  }
}
["in-count", "in-min", "in-max"].forEach((id) => {
  $("#" + id).addEventListener("input", updatePlanFeasible);
});

$("#btn-plan").addEventListener("click", async () => {
  const p = ST.proj;
  const segs = (p.pool && p.pool.segments) || [];
  if (!segs.length) return toast("片段池还是空的，先点「AI 挖片段池」");
  if ((p.plans || []).length && !confirm("重新生成结构会覆盖现有 " + p.plans.length + " 套结构（已出的片还在），继续？")) return;
  const count = Math.max(1, Number($("#in-count").value) || 3);
  const mn = Number($("#in-min").value) || 3;
  const mx = Number($("#in-max").value) || 7;
  const order = $("#in-order").value || "auto";
  try {
    await api(`/api/projects/${p.id}/plan`, {
      method: "POST",
      body: JSON.stringify({
        count, min_minutes: Math.min(mn, mx), max_minutes: Math.max(mn, mx), order,
      }),
    });
    ST.proj.task = { kind: "plan", pct: 0, msg: "启动中", error: "" };
    render();
    startPoll();
  } catch (e) {
    toast(e.message);
  }
});

/* AI 编排：把全剧台词交给 AI 一次排出多套结构。
   只走 1 次调用，所以先把「消耗 1 次、可能拿到几套」说清楚再发。 */
$("#btn-ai-plan").addEventListener("click", async () => {
  const p = ST.proj;
  if (!((p.episodes || []).length)) return toast("还没有转写结果，先完成第 1 步转写");
  if ((p.plans || []).length && !confirm("重新生成结构会覆盖现有 " + p.plans.length + " 套结构（已出的片还在），继续？")) return;
  const count = Math.max(1, Math.min(20, Number($("#in-count").value) || 3));
  const mn = Number($("#in-min").value) || 3;
  const mx = Number($("#in-max").value) || 7;
  const order = $("#in-order").value || "auto";
  const lo = Math.min(mn, mx), hi = Math.max(mn, mx);
  if (!confirm(`AI 会通读全剧台词，一次排出 ${count} 套（每套 ${lo}-${hi} 分钟）。\n\n`
    + "会消耗 1 次 AI 调用（约几分钱）。它看得见完整剧情，能把各套排成不同主线，\n"
    + "但套间重合度只能算出来给你看，不能像本机组装那样精确压到 70% 以内。\n\n继续？")) return;
  try {
    await api(`/api/projects/${p.id}/ai-plan`, {
      method: "POST",
      body: JSON.stringify({ count, min_minutes: lo, max_minutes: hi, order }),
    });
    ST.proj.task = { kind: "plan", pct: 0, msg: "AI 正在通读全剧", error: "" };
    render();
    startPoll();
  } catch (e) {
    toast(e.message);
  }
});

$("#btn-plan-all").addEventListener("click", () => planSelectAll(true));
$("#btn-plan-none").addEventListener("click", () => planSelectAll(false));

/* 第 4 步：只渲染勾选的套（成片存到用户指定的文件夹） */
/* 保存位置 = 用户选的根目录 + 项目名子目录。规则与后端 core.batch_out_dir 保持一致：
   用户只选「放到哪个大文件夹」，片子按项目归到子目录，多个项目不会混在一层。 */
function safeName(name) {
  return String(name || "").replace(/[\\/:*?"<>|\r\n\t]/g, "_").trim()
    .replace(/[ .]+$/, "").slice(0, 60) || "未命名项目";
}
function joinDir(root, name) {
  return `${String(root || "").replace(/[\/\\]+$/, "")}/${safeName(name)}`;
}
function updateSaveDirNote() {
  const el = $("#save-dir-note");
  if (!el) return;
  const root = ($("#save-dir").value || "").trim();
  const nm = safeName(ST.proj && ST.proj.name);
  el.textContent = root
    ? `实际存到：${joinDir(root, nm)}/　（按项目名自动建子目录）`
    : `留空会存到程序默认目录下的 ${nm}/`;
}
$("#save-dir").addEventListener("input", updateSaveDirNote);

$("#btn-render-plans").addEventListener("click", async () => {
  const p = ST.proj;
  const on = (p.plans || []).filter((pl) => pl.on !== false);
  if (!on.length) return toast("没有勾选任何结构，先在第 3 步勾选要出片的套");
  const saveDir = ($("#save-dir").value || "").trim();
  if (!saveDir) return toast("先填一个成片保存位置（点「选择文件夹…」更快）");
  const outDir = joinDir(saveDir, p.name);
  if (!confirm(`将渲染已勾选的 ${on.length} 套（每套约 2 分钟），存到：\n${outDir}\n\n确定？`)) return;
  try {
    await api(`/api/projects/${p.id}/render-plans`, {
      method: "POST",
      body: JSON.stringify({ save_dir: saveDir }),
    });
    ST.proj.task = { kind: "batch", pct: 0, msg: "启动中", error: "" };
    render();
    startPoll();
  } catch (e) {
    toast(e.message);
  }
});

$("#btn-save-dir-pick").addEventListener("click", async () => {
  const cur = ($("#save-dir").value || "").trim();
  toast("已弹出选择框，请在桌面上选一个文件夹…");
  try {
    const r = await api(`/api/pick-folder?initial=${encodeURIComponent(cur)}`, { method: "POST" });
    if (r && r.path) {
      $("#save-dir").value = r.path;
      updateSaveDirNote();
      toast(`保存位置：${joinDir(r.path, ST.proj && ST.proj.name)}`);
    }
  } catch (e) {
    toast(`${e.message}（可以直接手动输入路径）`);
  }
});

$("#btn-save-edl").addEventListener("click", async () => {
  try {
    const r = await api(`/api/projects/${ST.proj.id}/edl`, {
      method: "PUT",
      body: JSON.stringify({ text: $("#edl").value }),
    });
    toast(`已保存：${r.segments} 段 / ${fmt(r.total)}`);
    ST.proj = await api("/api/projects/" + ST.proj.id);
    render();
  } catch (e) {
    toast("保存失败：" + e.message);
  }
});

$("#btn-del").addEventListener("click", async () => {
  if (!confirm("删除这个项目的全部记录？（不会动你的原始视频）")) return;
  await api("/api/projects/" + ST.proj.id, { method: "DELETE" });
  ST.proj = null;
  $("#view-proj").classList.add("hidden");
  $("#view-new").classList.remove("hidden");
  loadList();
});

/* ---------- 设置 ---------- */
let SETTINGS = null;

async function openSettings() {
  const c = await api("/api/config");
  SETTINGS = c;
  $("#ver-hint").textContent = `v${c.version}`;
  $("#in-key").value = "";
  $("#in-key").placeholder = c.llm_key_set ? `${c.llm_key_hint}（留空则不修改）` : "sk-…";
  $("#key-hint").textContent = c.llm_key_set
    ? "已配置。留空表示不改；填新值会覆盖。"
    : "还没有配置 Key，AI 出表和挖片段池都需要它。";
  const tr = c.trial || {};
  const q = tr.quota;
  $("#trial-hint").textContent = c.llm_key_set
    ? (q ? `官方体验额度还剩 ${q.remaining}/${q.total} 次（当前用自己的 Key，不消耗体验次数）` : "")
    : (q ? `未填 Key 时使用官方体验额度：还剩 ${q.remaining}/${q.total} 次，用完需要填自己的 Key`
         : "体验服务暂时连不上；AI 功能需要在设置里填自己的百炼 Key。");
  const t = (c.tiers || [])[0];
  $("#tier-hint").textContent = t
    ? `${t.name}${t.available ? "" : "（未找到模型文件，安装包可能不完整）"}\n${t.note}`
    : `small`;
  const s = c.system || {};
  $("#sys-hint").textContent =
    `本机：${s.cpu} 核 CPU · ${s.mem_gb ? s.mem_gb + " GB 内存" : "内存未知"}`;
  $("#in-outdir").value = c.paths.output || "";
  const ofree = c.paths.output_free_gb;
  const dfree = c.paths.data_free_gb;
  $("#outdir-hint").textContent =
    (ofree >= 0 ? `成片盘剩余 ${ofree} GB。` : "") +
    (dfree >= 0 && dfree < 2 ? `⚠ 程序数据盘只剩 ${dfree} GB，建议改成其它盘。` : "") +
    " 出片要写约 1GB 临时文件，空间不足会渲染失败。";
  $("#path-hint").textContent = `数据目录：${c.paths.data}\n提示词目录：${c.paths.prompts}`;
  $("#modal-settings").classList.remove("hidden");
}

$("#btn-settings").addEventListener("click", () => openSettings().catch((e) => toast(e.message)));
$("#btn-close-settings").addEventListener("click", () => $("#modal-settings").classList.add("hidden"));
$("#modal-settings").addEventListener("click", (e) => {
  if (e.target.id === "modal-settings") $("#modal-settings").classList.add("hidden");
});

$("#btn-save-settings").addEventListener("click", async () => {
  const body = {};
  const k = $("#in-key").value.trim();
  if (k) body.llm_key = k;
  const od = $("#in-outdir").value.trim();
  if (SETTINGS && od !== ((SETTINGS.paths || {}).output || "")) body.output_dir = od;
  try {
    if (Object.keys(body).length) {
      await api("/api/config", { method: "PUT", body: JSON.stringify(body) });
    }
    $("#modal-settings").classList.add("hidden");
    toast("已保存");
    const st = await api("/api/status");
    ST.status = st;
    $("#pills").innerHTML = [
      `<span class="pill ${st.llm_key ? "ok" : "bad"}">模型 ${st.model}</span>`,
      `<span class="pill ${st.whisper_model ? "ok" : "bad"}">转写 ${st.whisper_tier}·${st.whisper_model ? "就绪" : "缺模型"}</span>`,
      `<span class="pill ${st.llm_key ? "ok" : "bad"}">Key ${st.llm_key ? "已配置" : "未配置"}</span>`,
    ].join("");
  } catch (e) {
    toast("保存失败：" + e.message);
  }
});

boot().catch((e) => toast("初始化失败：" + e.message));
