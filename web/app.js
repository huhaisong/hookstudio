const $ = (s) => document.querySelector(s);
const ST = { path: "", proj: null, timer: null };


async function api(url, opts = {}) {
  // 带上登录凭证（2026-11-04 用户体系）。**登录模式是固定的**：后端中间件除登录/
  // 探活外一律要身份，所以没有凭证时下面会拿到 401 → 自动去登录页，不存在匿名可用。
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  const tk = window.Session && Session.state.access;
  if (tk) headers.Authorization = "Bearer " + tk;
  let r;
  try {
    r = await fetch(url, { ...opts, headers });
  } catch (err) {
    // fetch 在网络层就失败（浏览器只说 TypeError: Failed to fetch）= 本地服务没在跑、
    // 端口被占、或进程刚退出。这句英文用户看不懂，翻成「程序退了，重开一次」。
    throw new Error("连不上本地服务（127.0.0.1:8100）。多半是程序窗口被关掉或进程退出了，重新打开钩子工坊再试。");
  }
  const txt = await r.text();
  let data;
  try { data = JSON.parse(txt); } catch { data = { detail: txt.slice(0, 200) }; }
  if (r.status === 401 && window.Session) {
    // 2026-11-14 硬过期（用户定稿）：过期 = 锁屏重登，不再无感续期——
    // revoked = 被踢下线/改过密码，expired = 单纯过期，登录页按这个给不同提示。
    Session.state.toLogin(null, data.code === "revoked" ? "kicked" : "expired");
    throw new Error(data.detail || "登录已过期，请重新登录");
  }
  // 被管理员封禁：403 + code=banned。必须清掉本地凭证再送去登录页，
  // 否则界面上还留着一个「看似登录着」的账号，用户只会以为是网络问题（2026-11-04 修）。
  if (r.status === 403 && data && data.code === "banned" && window.Session) {
    Session.state.clear();
    Session.state.toLogin(null, "banned");
    throw new Error(data.detail || "账号已被封禁");
  }
  // 还在用初始密码（或管理员重置过的密码）：后端只放行「改密」，这里把弹窗拉起来。
  // 不跳登录页——人没掉线，是「有一件事必须先做完」。
  if (r.status === 403 && data && data.code === "must-change-pwd" && window.Session) {
    Session.requirePasswordChange(data.detail);
    throw new Error(data.detail || "请先修改密码");
  }
  // 426：服务器判定这版程序已停用（exe 分发才有的一种情况，见 app/update.py）。
  // 全屏升级卡由 version.js 的 fetch 包装负责弹，这里只把错误往上抛，
  // 别再叠一个 toast——卡片已经盖住整个界面了，后面弹什么都看不见。
  if (r.status === 426) throw new Error(data.detail || "当前版本已停止使用，请升级");
  if (!r.ok) throw new Error(data.detail || "HTTP " + r.status);
  return data;
}

/* AI 授权票据（2026-11-04）：生成前先向后端申领，Key 永远不下发到浏览器。
   票据 15 分钟、绑定「当前账号 + 这个项目 + 这个用途」，换项目或换用途都要重领。
   服务端凭证没配好时这里会直接报错（detail 里写清原因），比跑到一半失败好。 */
async function aiGrant(pid, purpose) {
  const r = await api("/api/ai/grant", {
    method: "POST",
    body: JSON.stringify({ project_id: pid, purpose }),
  });
  return r.grant;
}

let toastTimer = null;function toast(msg) {
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

/* 界面上不出现模型代号：多组对比时只显示人能懂的档位名。
   值仍用真实代号提交，改的只是给用户看的文字。 */
const MODEL_LABEL = {
  "qwen3.5-plus": "默认", "ZHIPU/GLM-5.2": "快速", "ZHIPU/GLM-5.3": "快速·长剧本",
  "deepseek-v4-pro": "精细", "deepseek-flash": "轻快", "qwen-max": "备用",
};
function modelLabel(v) {
  if (!v) return "默认";
  return MODEL_LABEL[v] || "自定义";
}

/* 局部刷新：轮询每 1.5 秒回来一次，只重画「数据真的变了」的那一块。
   整块 innerHTML 重建会把用户展开的表合上、把正在读的位置滚走——
   刷新不能挡住用户看别的东西（2026-11-03 用户要求）。 */
function patchHtml(el, sig, build) {
  if (!el || el._sig === sig) return false;
  el._sig = sig;
  el.innerHTML = build();
  return true;
}

/* 文本没变就不写；轮询常驻，避免每 1.5 秒重排一次 */
function setText(sel, txt) {
  const el = typeof sel === "string" ? $(sel) : sel;
  if (el && el.textContent !== txt) el.textContent = txt;
}

async function boot() {
  // 登录模式是固定行为（2026-11-04 用户定性）：本地没有 access 就直接去登录页，
  // 别先发一串注定 401 的请求再弹「初始化失败」。
  // 2026-11-14 硬过期：access 不在或已过期（本地就能判 exp）都直接去登录页，
  // 不再拿 refresh 无感续期——登录有效期 24 小时，到点重新登录。
  if (window.Session && !Session.accessValid()) {
    Session.state.toLogin("/");
    return;
  }
  const st = await api("/api/status");
  ST.status = st;
  loadWho();
  // 顶栏只报「能不能用」，不报服务商、型号、档位这些内部信息
  $("#pills").innerHTML = [
    `<span class="pill ${st.llm_key ? "ok" : "bad"}">AI 生成 ${st.llm_key ? "已就绪" : "暂不可用"}</span>`,
    `<span class="pill ${st.whisper_model ? "ok" : "bad"}">台词识别 ${st.whisper_model ? "已就绪" : "需检查"}</span>`,
  ].join("");

  const roots = await api("/api/roots");
  renderDrives(roots);
  // 上次素材放在哪个目录就回到哪儿；目录被删了就退回第一个盘（P2）
  let startDir = "";
  try { startDir = localStorage.getItem("hs_lastdir") || ""; } catch (e) {}
  if (startDir) {
    try { await browse(startDir); }
    catch (e) { if (roots.length) await browse(roots[0].path); }
  } else if (roots.length) await browse(roots[0].path);
  if (!roots.length) $("#crumb").textContent = "没有找到任何磁盘";
  try {
    const cfg = await api("/api/config");
    ST.cfgOutput = (cfg.paths && cfg.paths.output) || "";
    ST.llmModel = cfg.llm_model || "";
    if (!$("#save-dir").value && ST.cfgOutput) {
      $("#save-dir").value = ST.cfgOutput.replace(/\\/g, "/");
    }
  } catch (_) { /* 配置读不到不影响主流程 */ }
  await loadModelsAlive();
  updateModeHint();
  await loadList();
}

async function loadList() {
  const rows = await api("/api/projects");
  const modeName = (m) => m === "batch" ? "批量混剪"
    : m === "shots" ? "只做分镜" : "单片导流";
  // 项目列表没变化就不重建：轮询收尾会调这里，重建会把侧栏滚动位置弹回顶部
  const sig = JSON.stringify(rows.map((r) => [r.id, r.name, r.mode, r.episodes,
    r.output ? 1 : 0, r.path, !!(ST.proj && ST.proj.id === r.id)]));
  const built = patchHtml($("#plist"), sig, () => rows.length
    ? rows.map((r) => `
      <div class="pitem ${ST.proj && ST.proj.id === r.id ? "on" : ""}" data-id="${r.id}" title="${esc(r.path || "")}">
        <b>${esc(r.name)}</b>
        <span>${modeName(r.mode)}${r.episodes ? ` · ${r.episodes} 集` : ""}${r.output ? " · 已出片" : ""}</span>
        <span class="ppath">${esc(r.path || "")}</span>
      </div>`).join("")
    : `<div class="hint">还没有项目</div>`);
  if (!built) return;
  document.querySelectorAll(".pitem").forEach((el) =>
    el.addEventListener("click", () => openProject(el.dataset.id)));
  applyProjFilter();
}

/* 项目搜索（2026-11-14 P2）：项目一多就得有。按名字/路径实时过滤，空了全显 */
function applyProjFilter() {
  const box = $("#proj-search");
  if (!box) return;
  const q = (box.value || "").trim().toLowerCase();
  document.querySelectorAll("#plist .pitem").forEach((el) => {
    el.style.display = !q || (el.textContent || "").toLowerCase().includes(q) ? "" : "none";
  });
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
  // 记住浏览到哪儿了：下次打开新建页直接回到这个目录（2026-11-14 P2）
  try { localStorage.setItem("hs_lastdir", d.path); } catch (e) {}
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
  updatePickHint();
  if (d.videos) $("#in-name").placeholder = d.path.split("/").pop();
}

/* 「选用」的真实语义（2026-11-14 P0-1）：原来按钮只改提示文字，真正建项目的
   是最后浏览的目录——点了「选用 A」再逛进 B，建出来的就是 B 且提示还在说 A；
   而且 browse() 每次都会把提示整个覆盖掉，连「已选用」三个字都不剩。
   现在选用 = 把当前目录存进 ST.pickedPath，创建时用 pickedPath；浏览到别处时
   提示如实区分「已选用哪个」和「正在浏览哪个」，不再让提示骗人。 */
function updatePickHint() {
  const hint = $("#pick-hint");
  const picked = ST.pickedPath || "";
  if (!hint || !picked) return;                 // 没选用过：保持原有引导文案
  const cur = ST.path || "";
  hint.textContent = picked === cur
    ? "已选用：" + picked + " —— 选好模式后点「扫描并创建」"
    : "已选用：" + picked + "（当前浏览的是 " + cur + "，要改成就再点一次「选用」）";
}

/* ---------- 新建 ---------- */
$("#proj-search").addEventListener("input", applyProjFilter);

$("#btn-new").addEventListener("click", () => {
  const was = ST.proj;
  const busy = was && (taskBusy(was.task) || taskBusy(was.task_nosub) || taskBusy(was.task_shots));
  ST.proj = null;
  $("#gbar").classList.add("hidden");
  $("#view-new").classList.remove("hidden");
  $("#view-proj").classList.add("hidden");
  loadList();
  updateModeHint();   // 回到新建面板时，按上次选的模式恢复界面（比如「只做分镜」不显示选目录）
  if (busy) toast(`「${was.name}」还在处理中，回到那个项目能看到进度`);
});

const MODE_HINTS = {
  // funnel 仅用于历史项目（单片导流已下线，新建不再提供）
  funnel: "单片导流（历史项目）：按集序排结构表，确认后出一条片。",
  batch: "批量混剪：AI 通读全剧台词，一次排出 N 套不同主线的剪法，你勾选后只给选中的套出片。",
  shots: "只做引流分镜：不用导入视频，创建后直接粘一段剧情素材，生成 15/30/60 秒的分镜提示词。",
};

/* 新建面板：随模式切换提示、要不要选文件夹、台词来源 */
function updateModeHint() {
  const m = $("#in-mode").value;
  const shotsOnly = m === "shots";
  const reuse = $("#in-lines").value === "reuse";
  $("#mode-hint").textContent = MODE_HINTS[m] || MODE_HINTS.batch;
  $("#pick-card").classList.toggle("hidden", shotsOnly);
  $("#lines-wrap").classList.toggle("hidden", shotsOnly);
  $("#reuse-wrap").classList.toggle("hidden", shotsOnly || !reuse);
  $("#btn-create").textContent = shotsOnly ? "创建" : "扫描并创建";
  if (shotsOnly) $("#in-name").placeholder = "例：替嫁新娘·钩子素材";
  if (!shotsOnly && reuse) loadReuseOptions("#in-reuse");
}

/* 可复用的项目：有台词的才列出来（没转的列了也没用） */
async function loadReuseOptions(sel, excludeId) {
  try {
    const rows = await api("/api/projects");
    const usable = rows.filter((r) => (r.lines || 0) > 0 && r.id !== excludeId);
    const el = $(sel);
    if (!el) return;
    el.innerHTML = usable.length
      ? usable.map((r) => `<option value="${r.id}">${esc(r.name)}（${r.episodes} 集 · 已整理 ${r.lines} 句）</option>`).join("")
      : `<option value="">还没有整理好台词的项目</option>`;
  } catch (_) { /* 读不到就留空，用户手动转写 */ }
}

$("#in-mode").addEventListener("change", updateModeHint);
$("#in-lines").addEventListener("change", updateModeHint);

$("#btn-up").addEventListener("click", () => {
  const p = $("#btn-up").dataset.parent;
  if (p) browse(p);
});

$("#btn-use").addEventListener("click", () => {
  ST.pickedPath = ST.path;
  updatePickHint();
  $("#btn-use").classList.add("primary");
  toast("已选中目录，选好模式后点「扫描并创建」");
});

$("#btn-create").addEventListener("click", async () => {
  const mode = $("#in-mode").value;
  const shotsOnly = mode === "shots";
  const name = $("#in-name").value.trim();
  // 用「选用」过的目录；没点过「选用」就按老习惯用最后浏览的目录（P0-1）
  const usePath = ST.pickedPath || ST.path || "";
  if (!shotsOnly && !usePath) return toast("先选一个目录");
  if (shotsOnly && !name) return toast("给这个分镜项目起个名字（会显示在左侧项目列表）");
  // 复用台词只对「导入了视频」的项目有意义；只做分镜没有集可对
  const reuseFrom = !shotsOnly && $("#in-lines").value === "reuse"
    ? $("#in-reuse").value : "";
  if (!shotsOnly && $("#in-lines").value === "reuse" && !reuseFrom) {
    return toast("先选一个整理好台词的项目，或改回「自动识别台词」");
  }
  const btn = $("#btn-create");
  btn.disabled = true;
  btn.textContent = shotsOnly ? "创建中…" : "扫描中…";
  try {
    const p = await api("/api/projects", {
      method: "POST",
      body: JSON.stringify({
        path: shotsOnly ? "" : usePath,
        name,
        target_minutes: 10,          // 单片导流下线后仅作占位（批量按面板里的每套时长走）
        mode,
        reuse_from: reuseFrom,
      }),
    });
    const reused = (p.episodes || []).filter((e) => (e.line_count === undefined
      ? (e.lines || []).length : e.line_count) > 0).length;
    toast(shotsOnly ? "已创建：在打开的页面里粘贴剧情素材，就能生成分镜提示词"
      : reused ? `已创建：${p.episodes.length} 集，其中 ${reused} 集台词直接复用——不用再整理了`
        : `已创建（批量混剪）：${p.episodes.length} 集 — 正在自动整理台词，完成后到第 3 步生成剪辑结构`);
    await loadList();
    await openProject(p.id);
  } catch (e) {
    toast("创建失败：" + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = shotsOnly ? "创建" : "扫描并创建";
  }
});

/* ---------- 项目 ---------- */
async function openProject(id) {
  ST.proj = await api("/api/projects/" + id);
  ST.step = null;                       // 新项目重新挑「下一步该做哪一步」
  const main = $("#main"); if (main) main.scrollTop = 0;
  $("#view-new").classList.add("hidden");
  $("#view-proj").classList.remove("hidden");
  // 保存位置：优先这个项目上次用的，其次全局「成片保存位置」
  const lastDir = (ST.proj.batch && ST.proj.batch.save_dir) || ST.cfgOutput || "";
  if (lastDir) $("#save-dir").value = lastDir.replace(/\\/g, "/");
  updateSaveDirNote();
  // 上次调的画面设置（全局默认 + 每套覆盖都存在项目里），打开就还原
  writeFx("fx-", (ST.proj.batch || {}).fx_default || null);
  render();
  updatePlanFxChips();
  loadList();
  // 进了项目就自动转写：只补从没转过的集；已失败的集不自动重试，免得每次打开都空跑一遍
  const cur = ST.proj;
  if (!cur.task && cur.episodes.some((e) => e.status === "pending")) {
    await autoTranscribe(cur.id);
  }
  const p = ST.proj;
  // 任一产线在跑就要轮询：只判断 cuts 会漏掉「只开了分镜」的项目的进度刷新
  if (p.task || p.task_nosub || p.task_shots) startPoll(); else stopPoll();
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
  transcribe: "正在整理台词", plan: "AI 正在编排结构",
  analyze: "正在让 AI 出结构表", render: "正在出片", batch: "正在批量出片",
  shots: "AI 正在写镜号提示词", nosub: "正在生成处理好的视频",
};

/* 任务是不是「还在跑」。
   不能只看 task 存不存在：任务跑完有的模块会把 task 置空，有的会把 pct 留在 100，
   后者会让所有按钮永久禁用（2026-11-03 用户反馈：引流分镜生成按钮点不动）。
   所以「没有 error 且 pct < 100」才算忙；到 100 一律当已结束。 */
function taskBusy(t) {
  return !!t && !t.error && Number(t.pct || 0) < 100;
}

function fmtElapsed(sec) {
  const s = Math.max(0, Math.round(sec || 0));
  if (s < 60) return `${s} 秒`;
  return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
}

/* 三条产线各自的进度：第 1/3 步 cuts（p.task）、第 2 步 nosub（p.task_nosub）、
   第 4 步 shots（p.task_shots）。顶部状态条一次只显示一条：先按当前步骤挑，
   再按「谁在跑」挑；返回 group 是给「中止」和防连点标记用的。 */
function activeBarTask(p) {
  const cands = [[p.task, "cuts"], [p.task_nosub, "nosub"], [p.task_shots, "shots"]];
  const want = { "0": "nosub", "3": "shots" }[ST.step] || "cuts";
  const pick = (fn) => {
    const hit = cands.find(fn);
    return hit ? { task: hit[0], group: hit[1] } : null;
  };
  return pick(([t, g]) => g === want && taskBusy(t))
      || pick(([t]) => taskBusy(t))
      || { task: p.task || p.task_nosub || p.task_shots || null, group: "cuts" };
}

/* 三条产线各自记一个「已发中止」：任务真正停下（状态清空）后才解锁按钮，
   避免连点；也别因为另一条还在跑就把这一条的按钮一直锁着。 */
const _cancelSent = { cuts: false, nosub: false, shots: false };
/* 中止后的反馈条：状态条不能「啪一下就没了」，只剩一个 toast 抓不住
   （2026-11-14 用户反馈）。留 60 秒的结果反馈，期间有新任务就自然覆盖。 */
const _abortNote = { group: "", at: 0 };

/* 顶部状态条：有任务时显示「在做什么 + 进度 + 已用时间 + 预计还需」，没有就收起 */
function updateBar(t, group) {
  const bar = $("#gbar");
  const stop = $("#btn-task-cancel");
  if (!taskBusy(t)) {
    const fresh = _abortNote.group && Date.now() - _abortNote.at < 60000;
    if (fresh) {
      bar.classList.remove("hidden");
      $("#gbar-fill").style.width = "100%";
      $("#gbar-text").textContent = "已中止 · 已经做好的部分都保留，想接着做重新点一次就行";
      if (stop) { stop.disabled = true; stop.textContent = "中止"; }
      return;
    }
    _abortNote.group = "";
    bar.classList.add("hidden");
    return;
  }
  _abortNote.group = "";
  const pct = Math.max(2, Math.min(100, Math.round(t.pct || 0)));
  bar.classList.remove("hidden");
  $("#gbar-fill").style.width = pct + "%";
  const parts = [t.msg || TASK_FALLBACK[t.kind] || "处理中"];
  if ((t.elapsed || 0) >= 1) parts.push(`已用 ${fmtElapsed(t.elapsed)}`);
  // 预估还要多久：按「已用时间 ÷ 已完成百分比」线性外推。百分比不稳的任务
  // 估出来也会飘，所以带 ≈ 号，且只在跑了一阵子之后才显示。
  if (pct >= 5 && (t.elapsed || 0) >= 5) {
    parts.push(`预计还需 ≈ ${fmtElapsed(Math.round((t.elapsed || 0) / pct * (100 - pct)))}`);
  }
  parts.push(`${pct}%`);
  $("#gbar-text").textContent = parts.join(" · ");
  if (stop) {
    stop.dataset.group = group || "";
    const sent = !!_cancelSent[group || "cuts"];
    stop.disabled = sent;
    stop.textContent = sent ? "中止中…" : "中止";
  }
}

/* 点「中止」：只停当前显示的那一条。几条同时跑时，停完这条别的会接着
   显示，可以再点一次；已经做好的成片、台词、结构都不受影响。 */
$("#btn-task-cancel").addEventListener("click", async () => {
  const p = ST.proj;
  const btn = $("#btn-task-cancel");
  if (!p || btn.disabled) return;
  const group = btn.dataset.group || "";
  const key = group || "cuts";
  if (!confirm("确定中止当前这一步吗？\n\n已经做好的部分会保留；没跑完的不会继续，想接着做重新点一次就行。正在处理的一段会立刻停下。")) return;
  _cancelSent[key] = true;
  btn.disabled = true;
  btn.textContent = "中止中…";
  try {
    const r = await api(`/api/projects/${p.id}/task/cancel`, {
      method: "POST", body: JSON.stringify({ group }),
    });
    if (!(r.cancelled || []).length) {
      _cancelSent[key] = false;
      toast("这一步刚好已经结束了");
    } else {
      _abortNote.group = key;
      _abortNote.at = Date.now();
      toast("已中止，正在收尾；已经做好的部分都还在");
    }
  } catch (e) {
    _cancelSent[key] = false;
    toast(e.message || "没能中止，请再试一次");
  }
});

/* ---------- 步骤导航（2026-11-07）----------
   原来 5 个步骤全平铺成一张长卷，用户要一直滚、也分不清自己做到哪。
   导航一次只亮一步：状态由「有没有产物」算，默认停在下一步该做的位置。
   ST.step 只由点击或切换项目改变，1.5 秒一次的轮询不会把它冲掉。 */
const STEP_SLOTS = {
  "0": ["#step-nosub"],       // key 仍是 "0"（复用老槽位），界面上是第 2 步：台词整理之后的可选一步
  "1": ["#step-transcribe"],
  // 2026-11-07：原第 2 步「剪辑结构」和原第 3 步「出片」合成一步，
  // 出片进度就地点完就能看，不用再切一次导航。
  "2": ["#funnel-step2", "#batch-step2"],
  "3": ["#step-shots"],
  "4": ["#step-vmerge"],
};
// 合并后的第 2 步里既有排结构也有出片，两种任务都算「这步在跑」。
const TASK_OF_STEP = {
  "0": ["nosub"],
  "1": ["transcribe"],
  "2": ["plan", "batch", "analyze", "render"],
  "3": ["shots"],
  "4": [],
};

function stepNavItems(p) {
  const shotsOnly = p.mode === "shots";
  const eps = p.episodes || [];
  const done = eps.filter((e) => e.status === "done").length;
  const plans = p.plans || [];
  const items = [];
  // no = 导航上固定显示的序号。完整流程是「0 准备素材 + 1~4 四步」，
  // 插入第 0 步后不能再用数组下标 i+1，否则后面每一步都会错号。
  // 只做分镜的项目只有两步，不给 no，让下标算出 1/2。
  if (!shotsOnly) {
    items.push({ key: "1", no: "1", label: "台词整理", sub: "整理台词",
      state: eps.length && done === eps.length ? "done" : done ? "run" : "todo" });
    // 台词整理之后的一步：抹掉原字幕、在原位置换成自己的台词（可选，可跳过）
    const ns = p.nosub || {};
    const nsDone = Object.keys(ns.files || {}).length;
    const nsAll = eps.length > 0 && nsDone >= eps.length;
    items.push({ key: "0", no: "2", label: "重做字幕", sub: "换成自己的台词",
      state: nsAll ? "done" : "todo",
      txt: nsAll ? "已备好" : nsDone ? `已备 ${nsDone}/${eps.length} 集` : "可跳过" });
    const hasPlan = p.mode === "batch" ? plans.length : (p.edl || []).length;
    const hasOut = p.mode === "batch" ? (p.sets || []).length : p.output;
    items.push({ key: "2", no: "3", label: "剪辑与出片", sub: "排结构 · 出片",
      state: hasOut ? "done" : "todo",
      txt: hasOut ? "已完成" : hasPlan ? "待出片" : "未开始" });
  }
  items.push({ key: "3", no: shotsOnly ? undefined : "4", label: "引流分镜", sub: "可独立用",
    state: p.shots ? "done" : "todo", alone: true });
  items.push({ key: "4", no: shotsOnly ? undefined : "5", label: "两两合并", sub: "可独立用",
    state: "todo", alone: true });
  return items;
}

function defaultStep(p) {
  if (p.mode === "shots") return "3";
  const eps = p.episodes || [];
  const done = eps.filter((e) => e.status === "done").length;
  if (!eps.length || done < eps.length) return "1";
  // 台词整理完 → 落在核心的「剪辑与出片」；重做字幕是可选项，不抢落地位
  // （2026-11-14 用户定性，修正与注释相反的旧行为）。唯一例外：重做字幕的
  // 产线正在跑时落回它，不然用户看不到自己的任务进度。
  if (taskBusy(p.task_nosub)) return "0";
  return "2";
}

function renderStepNav(p) {
  const nav = $("#step-nav");
  if (!nav) return;
  const items = stepNavItems(p);
  const keys = items.map((x) => x.key);
  if (!ST.step || !keys.includes(ST.step)) {
    // 上次自己停在哪一步就回哪儿；没有记忆才按「下一步该做哪一步」推断。
    // （可跳过的步骤不该一直抢占落地位：做不做重做字幕是用户自己的选择。）
    let remembered = "";
    try { remembered = localStorage.getItem("hs_step_" + p.id) || ""; } catch (e) {}
    ST.step = (remembered && keys.includes(remembered)) ? remembered : defaultStep(p);
  }
  // 每步看自己那条产线：第 2 步看 task_nosub、第 4 步看 task_shots，其余看 task(cuts)
  const taskOf = (key) => (key === "3" ? p.task_shots : key === "0" ? p.task_nosub : p.task);
  const sig = JSON.stringify([items, ST.step,
    taskBusy(p.task), taskBusy(p.task_nosub), taskBusy(p.task_shots)]);
  if (nav._sig !== sig) {
    nav._sig = sig;
    nav.innerHTML = items.map((it, i) => {
      const on = it.key === ST.step;
      const tk = taskOf(it.key);
      const running = taskBusy(tk) && (TASK_OF_STEP[it.key] || []).includes((tk || {}).kind);
      const st = running ? "run" : it.state;
      const txt = (it.txt && !running) ? it.txt
        : st === "done" ? "已完成" : st === "run" ? "进行中"
          : (it.alone ? "独立" : "未开始");
      return `<button class="sn-item ${on ? "on" : ""} ${st}" data-step="${it.key}" title="${it.label} · ${it.sub}">
        <span class="sn-dot">${st === "done" ? "✓" : (it.no === undefined ? i + 1 : it.no)}</span>
        <span class="sn-txt"><b>${it.label}</b><span class="sn-sub"><i>${it.sub}</i><span class="sn-state">${txt}</span></span></span>
      </button>`;
    }).join("");
    nav.querySelectorAll(".sn-item").forEach((btn) => {
      btn.addEventListener("click", () => {
        ST.step = btn.dataset.step;
        // 记住用户自己停在哪一步：下次打开项目直接回到这儿，
        // 不用每次都从「下一步」的推断位置重新找（2026-11-14 用户要求更好用）。
        try { localStorage.setItem("hs_step_" + p.id, ST.step); } catch (e) {}
        const m = $("#main"); if (m) m.scrollTop = 0;
        render();
      });
    });
  }
  // 父组（#funnel-steps / #batch-steps）按模式由 render() 控制，这里只管单步显隐。
  Object.entries(STEP_SLOTS).forEach(([k, sels]) => {
    const show = keys.includes(k) && k === ST.step;
    sels.forEach((s) => { const el = $(s); if (el) el.classList.toggle("hidden", !show); });
  });
  // 只做分镜的项目只显示「引流分镜 + 两两合并」两步，序号按可见步数算。
  // 完整流程现在是 5 步（台词整理 / 去字幕+加字幕 / 剪辑出片 / 引流分镜 / 两两合并）。
  setText("#vm-idx", String(p.mode === "shots" ? 2 : 5));
}

function render() {
  const p = ST.proj;
  if (!p) {
    $("#gbar").classList.add("hidden");
    return;
  }
  if (!taskBusy(p.task)) _cancelSent.cuts = false;
  if (!taskBusy(p.task_nosub)) _cancelSent.nosub = false;
  if (!taskBusy(p.task_shots)) _cancelSent.shots = false;
  const bar = activeBarTask(p);
  updateBar(bar.task, bar.group);
  const isBatch = p.mode === "batch";
  const shotsOnly = p.mode === "shots";   // 只做分镜：不导入视频，前面三步都不显示
  $("#step-transcribe").classList.toggle("hidden", shotsOnly);
  $("#shots-idx").classList.toggle("hidden", shotsOnly);   // 独立模式下没有第 1~2 步，序号会看着怪
  $("#funnel-steps").classList.toggle("hidden", isBatch || shotsOnly);
  $("#batch-steps").classList.toggle("hidden", !isBatch);

  const dur = p.episodes.reduce((a, e) => a + (e.duration || 0), 0);
  const bad = p.episodes.filter((e) => e.status === "error").length;
  setText("#p-name", p.name);
  setText("#p-path", shotsOnly
    ? "模式：只做引流分镜（未导入视频，剧情素材在下方粘贴）"
    : "素材：" + (p.win_dir || p.src_dir || "—")
      + "　·　模式：" + (isBatch ? "批量混剪" : "单片导流"));
  const b = p.batch || {};
  const durText = isBatch
    ? (b.cut_mode === "hook"
        // 钩子前置每集时长不一样（整集长度各不同），说「每套 X 分钟」会误导
        ? `每集：钩子 ${b.hook_sec || 15} 秒 + 整集正片`
        : mixDurText(b))
    : `目标 ${p.target_minutes} 分钟`;
  setText("#p-meta", shotsOnly
    ? `不导入视频 · 剧情素材：你自己提供 · 只产出分镜提示词`
    : `${p.episodes.length} 集 · 素材总时长 ${fmt(dur)} · ${durText} · ${p.line_total} 句台词` +
      (bad ? ` · ${bad} 集整理失败` : ""));

  const t = p.task;                 // 第 1 步 + 第 3 步（cuts）
  const busy = taskBusy(t);
  const tNs = p.task_nosub;         // 第 2 步（独立并行）
  const nsBusy = taskBusy(tNs);
  const tShot = p.task_shots;       // 第 4 步（独立并行）
  const shotBusy = taskBusy(tShot);
  const setState = (sel, txt) => {
    const el = $(sel);
    if (el && el.textContent !== txt) el.textContent = txt;
  };

  const done = p.episodes.filter((e) => e.status === "done").length;
  setState("#s1-state", t && t.kind === "transcribe" ? `${t.pct}% · ${t.msg}`
    : done === p.episodes.length ? `已完成 ${done}/${p.episodes.length} 集`
    : done ? `已完成 ${done}/${p.episodes.length} 集` : "未开始");

  if (isBatch) {
    syncHookState();
    renderCutMode();
    renderHookPanel();
    const plans = p.plans || [];
    // 单位跟着「当前结构是哪种产线」走：钩子前置是一集一条，说「套」会让人以为
    // 还能再多出几套；混剪反之。plans 为空时按当前 tab 猜。
    const hookMode = plans.length ? plans[0].mode === "hook" : HK.mode === "hook";
    const unitName = hookMode ? "集" : "套";
    setState("#b2-state", t && t.kind === "plan" ? `${t.pct}% · ${t.msg}`
      : plans.length
        ? (hookMode ? `已出 ${plans.length} 集钩子` : `已出 ${plans.length} 套结构`)
        : "未开始");
    const curGen = Number((p.batch && p.batch.gen) || p.plan_gen || 0);
    const sets = (p.sets || []).filter((s) => Number(s.gen || 0) === curGen);
    const fails = (p.batch || {}).failures || [];
    const failTxt = fails.length && !(t && t.kind === "batch")
      ? ` · ${fails.length} ${hookMode ? "条" : "套"}失败` : "";
    setState("#b3-state", t && t.kind === "batch" ? `${t.pct}% · ${t.msg}`
      : sets.length || fails.length
        ? `已出片 ${sets.filter((s) => s.output).length}/${(p.plans || []).length || sets.length} ${unitName}${failTxt}`
        : "未开始");
  } else {
    setState("#s2-state", t && t.kind === "analyze" ? `${t.pct}% · ${t.msg}`
      : p.edl.length ? `${p.edl.length} 段 · ${fmt(p.edl_total)}` : "未开始");
    setState("#s3-state", t && t.kind === "render" ? `${t.pct}% · ${t.msg}`
      : p.output ? `${fmt(p.output_duration || 0)} · ${((p.output_size || 0) / 1048576).toFixed(0)}MB` : "未开始");
  }

  // 引流分镜与模式无关：三种模式下都在，自己粘素材时完全不依赖前面的转写
  const shotsP = p.shots;
  setState("#b4-state", tShot && shotBusy ? `${tShot.pct}% · ${tShot.msg}`
    : shotsP ? `已出 ${shotsP.count} 条 ${shotsP.duration}s / ${(shotsP.seg_total
      || (shotsP.episodes_md || []).reduce((a, e) => a + (e.segments || []).length, 0))} 段` : "未开始");
  renderShots();
  syncShotSrc();

  $("#btn-transcribe").disabled = busy || done === p.episodes.length;
  $("#transcript-links").classList.toggle("hidden", !p.line_total);
  $("#btn-transcript-dl").href = `/api/projects/${p.id}/transcript?download=1`;
  // 删项目要等三条产线都停：有任务在跑时删掉，后台任务会往不存在的目录里写。
  $("#btn-del").disabled = busy || nsBusy || shotBusy;

  if (isBatch) {
    // 参数表单只在「进入这个项目」时按 b.jobs 重建一次（initPlanModels，由
    // planBuiltFor 守卫）；生成 / 轮询期间绝不回填或重建，否则会把用户刚改的
    // 参数覆盖回上一轮的值，或把多组参数错乱成一堆重复行。
    if (planBuiltFor !== p.id) initPlanModels(p);
    $("#btn-plan").disabled = busy || !done;
    renderPlans();
    renderSets();
  } else {
    $("#btn-analyze").disabled = busy || done === 0;
    $("#btn-render").disabled = busy || !p.edl.length;
    $("#btn-save-edl").disabled = busy;
    if (document.activeElement !== $("#edl") && $("#edl").value !== (p.edl_text || "")) {
      $("#edl").value = p.edl_text || "";
    }
    if ($("#p-note").textContent !== (p.notes || "")) $("#p-note").textContent = p.notes || "";
    updateStat();
  }

  if (t && t.error) {
    const m = { transcribe: "#s1-state", analyze: "#s2-state", render: "#s3-state",
                plan: "#b2-state", batch: "#b3-state" };
    setState(m[t.kind] || "#s1-state", "失败：" + t.error.slice(0, 120));
  }
  // 分镜的失败单独显示在自己的状态行，不再混进前面几步的槽位
  if (tShot && tShot.error) setState("#b4-state", "失败：" + tShot.error.slice(0, 120));

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
  renderNoSub(p);
  renderStepNav(p);
}

/* ---------- 台词整理之后的一步：重做字幕（2026-11-11，2026-11-12 收敛）-------
   逐集把原字幕抹掉，再按这一集整理好的台词把新字幕烧在原来那行字幕的位置上，
   产出每集一条独立视频；后面剪辑出片直接从这些视频切段。整步可选，跳过也行。

   界面上有四调项：出场动效、动效幅度、抹除柔和程度、抹掉的地方怎么补。
   其余（做法、字幕位置、要不要加字幕）都已固定（2026-11-12 用户要求），
   所以 mode/auto/newsub 不再从界面读，直接写死跟后端一致。 */
const NS_DEF = { mode: "soft", auto: true, pos: 0.75, size: 0.10, soft: 0.5,
                 fill: "above", fill_mode: "mirror",
                 newsub: true, newsub_pos: 0.80, newsub_motion: 0.5,
                 newsub_style: "rise" };
/* 新字幕出场动效。跟后端 render.NEWSUB_STYLES 一一对应，
   首字母参与设置指纹（改风格就得重做副本）。 */
const NS_STYLES = ["fade", "rise", "pop", "float", "type"];
/* 抹除引擎版本，必须与后端 nosub.sig_of 里的前缀同步（后端现在写的是
   `E{subdetect.VERSION}`，所以这里要跟着 subdetect.VERSION 改）。
   忘了同步的话 cfg_sig 永远对不上，界面会一直说「改过设置、要重做」。
   E4 = 每集按自己的字幕带中心烧新字幕（多来源项目不再跳位）。
   E5 = 短片段项目不再被判成「没字幕」。
   E6 = 画面运动的碎帧不再留洞（原字幕闪一下才消失）。 */
const NS_ENGINE = "E6";
let nsCfgBuiltFor = null;
let _nsSaveT = null;

function readNoSubCfg() {
  const v = (id, d) => {
    const e = $("#" + id);
    return e ? Number(e.value) : d;
  };
  // 位置、带高、从哪边补都不再由界面提供：用上次识别出来的那几个值
  // （还没有识别结果就取项目里存的那份，再没有用默认）。后端 clean_cfg
  // 收下之后还会被识别结果覆盖一次，所以这里只要「传得回去、对得上指纹」。
  const ns = (ST.proj && ST.proj.nosub) || {};
  const det = ns.detect || {};
  const saved = Object.assign({}, NS_DEF,
    (ST.proj && ST.proj.nosub_cfg) || ns.cfg || {});
  const pick = (k) => (det[k] == null ? saved[k] : det[k]);
  const style = $("#ns-nstyle") ? String($("#ns-nstyle").value) : "";
  const motion = Math.round(saved.newsub_motion * 100);
  const fm = $("#ns-fillmode") ? String($("#ns-fillmode").value) : "";
  return {
    mode: "soft",
    auto: true,
    pos: Math.min(0.92, Math.max(0.50, pick("pos"))),
    size: Math.min(0.20, Math.max(0.03, pick("size"))),
    soft: Math.min(1, Math.max(0, v("ns-soft", Math.round(saved.soft * 100)) / 100)),
    fill: pick("fill") === "below" ? "below" : "above",
    fill_mode: fm === "borrow" ? "borrow" : "mirror",
    newsub: true,
    newsub_pos: Math.min(0.92, Math.max(0.50, pick("newsub_pos"))),
    newsub_motion: Math.min(1, Math.max(0,
      v("ns-nmotion", Number.isFinite(motion) ? motion : 50) / 100)),
    newsub_style: NS_STYLES.indexOf(style) >= 0 ? style : "rise",
  };
}

/* 跟后端 nosub.sig_of(cfg)（只算设置那一半）同一套算法：用来判断
   「现在面板上的设置，和已经生成的那份副本是不是同一版」。不一致就提示重做。
   c 必须是**完整**的 cfg（pos/size/fill/newsub_pos 都在），因为识别出来的
   新字幕位置也算在指纹里；只拿面板上的三个值算会永远对不上。 */
function nsSig(c) {
  return `${NS_ENGINE}F${Math.round(c.pos * 1000)}${Math.round(c.size * 1000)}`
    + `${Math.round(c.soft * 100)}${c.fill === "below" ? "d" : "u"}`
    + `${c.fill_mode === "borrow" ? "B" : "M"}`
    + `|N${Math.round(c.newsub_pos * 1000)}${Math.round(c.newsub_motion * 100)}`
    + `${(c.newsub_style || "rise").charAt(0)}`;
}

/* 面板上只剩三个可调项：出场动效、动效幅度、抹除柔和程度。
   位置、带高、从哪边补、识别到的范围都不再显示——识别是机器的事，
   结果只落在成品里；原理放在「这一步会做什么」里讲。 */
function nsSync() {
  const c = readNoSubCfg();
  const n = Math.round(c.soft * 100);
  setText("#ns-softv", n < 25 ? "偏清晰" : n < 75 ? "中等" : "更柔");
  const m = Math.round(c.newsub_motion * 100);
  setText("#ns-nmotionv", m <= 0 ? "几乎不动" : m >= 90 ? "明显"
    : m < 30 ? "很轻" : m < 70 ? "推荐" : "偏明显");
  return c;
}

function writeNoSubCfg(p) {
  // 只在「刚进入这个项目」时回填一次。轮询每 1.5 秒来一次，反复回填会把
  // 用户正在拖的滑杆弹回上次保存的值（生成设置那边吃过这个亏）。
  if (nsCfgBuiltFor === p.id) return;
  nsCfgBuiltFor = p.id;
  const c = Object.assign({}, NS_DEF, p.nosub_cfg || (p.nosub || {}).cfg || {});
  const put = (id, v) => { const e = $("#" + id); if (e) e.value = v; };
  put("ns-soft", Math.round((c.soft == null ? 0.5 : c.soft) * 100));
  put("ns-nmotion", Math.round((c.newsub_motion == null ? 0.5 : c.newsub_motion) * 100));
  put("ns-nstyle", NS_STYLES.indexOf(c.newsub_style) >= 0 ? c.newsub_style : "rise");
  put("ns-fillmode", c.fill_mode === "borrow" ? "borrow" : "mirror");
  nsSync();
}

function saveNoSubCfg() {
  clearTimeout(_nsSaveT);
  _nsSaveT = setTimeout(async () => {
    if (!ST.proj) return;
    try {
      await api(`/api/projects/${ST.proj.id}/nosub-config`, {
        method: "PUT", body: JSON.stringify(readNoSubCfg()),
      });
    } catch (e) { /* 存不上不打断调参，点生成时会再带上当前值 */ }
  }, 700);
}

function renderNoSub(p) {
  writeNoSubCfg(p);
  const ns = p.nosub || {};
  const files = ns.files || {}, errs = ns.errors || {};
  const total = (p.episodes || []).length;
  const done = Object.keys(files).length;
  const failed = Object.keys(errs).length;
  const t = p.task_nosub;
  const busy = taskBusy(t) && t.kind === "nosub";
  const cur = readNoSubCfg();
  // 能动的只有「柔和程度 / 动效幅度 / 出场动效」三项。位置、带高、填充方向
  // 都是识别出来的，所以拿上一次那份完整 cfg 当底、只把这三项盖上去再算
  // 指纹——直接用手拼的话 newsub_pos 会差一位，界面会一直喊「要重做」。
  const base = ns.cfg && ns.cfg.pos != null
    ? Object.assign({}, ns.cfg, {
      soft: cur.soft,
      fill_mode: cur.fill_mode,
      newsub_motion: cur.newsub_motion,
      newsub_style: cur.newsub_style,
    })
    : null;
  const changed = !!(ns.cfg_sig && base && ns.cfg_sig !== nsSig(base));

  const state = busy ? `${Math.round(t.pct || 0)}% · ${t.msg}`
    : t && t.error && t.kind === "nosub" ? "失败：" + String(t.error).slice(0, 120)
      : done >= total && total ? `已备好 ${done} 集`
        : done ? `已备 ${done}/${total} 集` : "可选，可跳过";
  setText("#s0-state", state);

  let note = "";
  if (busy) note = "正在生成；可以切到第 3 步同时出片（那边「用哪份视频」要选原始视频）。";
  else if (done && changed) note = `现有这 ${done} 集是按旧设置做的，改过设置了——点生成会按新设置重做一遍。`;
  else if (done && failed) note = `已备好 ${done} 集，还有 ${failed} 集没做成，点生成会补做。`;
  else if (done) note = `已备好 ${done} 集。设置没变时再点生成只会补没做过的集；改过台词的话，回来点一次让字幕跟上。`;
  else note = "这一步可以跳过；想用「原字幕换成自己台词」的视频，随时回来生成。";
  setText("#ns-state", note);

  const btn = $("#btn-nosub-run");
  if (btn) {
    btn.disabled = busy || taskBusy(t) || total === 0;
    btn.textContent = done && changed ? "按新设置重做"
      : done && failed ? "补做没成功的集" : "生成处理好的视频";
  }
  const has = done > 0;
  $("#btn-nosub-preview").classList.toggle("hidden", !has);
  $("#btn-nosub-open").classList.toggle("hidden", !has);
}

["ns-soft", "ns-nmotion", "ns-nstyle", "ns-fillmode"].forEach((id) => {
  const e = $("#" + id);
  if (!e) return;
  const onChange = () => {
    nsSync();
    if (ST.proj) renderNoSub(ST.proj);
    saveNoSubCfg();
  };
  e.addEventListener("input", onChange);
  e.addEventListener("change", onChange);
});

$("#btn-nosub-run").addEventListener("click", async () => {
  const p = ST.proj;
  if (!p) return;
  if (taskBusy(p.task_nosub)) return toast("这一步已经在生成了，等它跑完再试");
  const cfg = readNoSubCfg();
  const ns = p.nosub || {};
  const done = Object.keys(ns.files || {}).length;
  if (done && (!ns.cfg_sig || ns.cfg_sig === nsSig(cfg))
      && !confirm("设置没变，已有的集会跳过，只补没生成成功的那几集。\n\n想换一种效果，先改上面的方式或滑杆再点生成。继续？")) return;
  try {
    await api(`/api/projects/${p.id}/nosub`, {
      method: "POST", body: JSON.stringify(cfg),
    });
    p.task_nosub = { kind: "nosub", pct: 0, msg: "启动中", error: "" };
    render();
    startPoll();
    toast("开始生成处理好的视频，进度在页面顶部");
  } catch (e) {
    toast(e.message);
  }
});

$("#btn-nosub-preview").addEventListener("click", async () => {
  const p = ST.proj;
  if (!p) return;
  try {
    await api(`/api/projects/${p.id}/nosub/preview`, { method: "POST" });
    toast("已用系统播放器打开第一集处理好的视频");
  } catch (e) { toast(e.message); }
});

$("#btn-nosub-open").addEventListener("click", async () => {
  const p = ST.proj;
  if (!p) return;
  try {
    await api(`/api/projects/${p.id}/nosub/open`, { method: "POST" });
  } catch (e) { toast(e.message); }
});

/* ---------- 编排方式：多套混剪 / 单集钩子前置（2026-11-06）-------------
   两条产线共用同一份转写结果和出片管线，区别只在这一步排出来的结构形态：
   混剪 = N 套 4-6 分钟的片；钩子前置 = 每集一条「15 秒爆点 + 整集正片」。
   HK 是这块的界面状态（不落盘）；落盘的只有生成后写进 batch 的
   cut_mode / hook_sec / hook_eps——下次打开项目按上次生成的样子复原。 */
const HK = { pid: "", mode: "mix", eps: null, sec: 15, model: "" };

function hookEpDone() {
  return ((ST.proj && ST.proj.episodes) || []).filter((e) => e.status === "done");
}

function syncHookState() {
  const p = ST.proj;
  if (!p) return;
  const b = p.batch || {};
  if (HK.pid !== p.id) {
    HK.pid = p.id;
    HK.mode = b.cut_mode === "hook" ? "hook" : "mix";
    HK.sec = [10, 15, 20].includes(Number(b.hook_sec)) ? Number(b.hook_sec) : 15;
    HK.model = "";
    HK.eps = new Set(Array.isArray(b.hook_eps) && b.hook_eps.length
      ? b.hook_eps : hookEpDone().map((e) => e.index));
  }
  if (!HK.eps) HK.eps = new Set();
  // 重转写 / 重选素材之后集号会变：剔掉已不存在的集，别让幽灵集号混进请求
  const live = new Set((p.episodes || []).map((e) => e.index));
  [...HK.eps].forEach((i) => { if (!live.has(i)) HK.eps.delete(i); });
  if (!HK.eps.size) hookEpDone().forEach((e) => HK.eps.add(e.index));
}

function renderCutMode() {
  const mix = $("#mix-pane"), hook = $("#hook-pane");
  if (!mix || !hook) return;
  document.querySelectorAll("#cut-mode .cm-btn").forEach((b) => {
    b.classList.toggle("on", b.dataset.mode === HK.mode);
  });
  mix.classList.toggle("hidden", HK.mode !== "mix");
  hook.classList.toggle("hidden", HK.mode !== "hook");
}

/* 集数格子：一集一格，选中的高亮。20 集以内一屏放得下，比下拉框点得快。 */
function renderHookPanel() {
  const p = ST.proj;
  const box = $("#hook-ep-chips");
  if (!p || !box) return;
  const eps = p.episodes || [];
  const sig = p.id + "|" + eps.map((e) => `${e.index}:${Math.round(e.duration || 0)}:${e.status || ""}`).join(",");
  if (box.dataset.sig !== sig) {
    box.dataset.sig = sig;
    box.innerHTML = eps.map((e) => {
      const ok = e.status === "done";
      return `<button class="ep-chip${ok ? "" : " miss"}" data-ep="${e.index}"${ok ? "" : " disabled"}
        title="${ok ? `第 ${e.index} 集 · ${fmt(e.duration || 0)}` : `第 ${e.index} 集还没整理好，不能出`}"
        >第${String(e.index).padStart(2, "0")}集<small>${ok ? fmt(e.duration || 0) : "未整理"}</small></button>`;
    }).join("");
  }
  box.querySelectorAll(".ep-chip").forEach((el) => {
    el.classList.toggle("on", HK.eps.has(Number(el.dataset.ep)));
  });
  const sel = eps.filter((e) => HK.eps.has(e.index) && e.status === "done");
  const body = sel.reduce((a, e) => a + (e.duration || 0), 0);
  const total = body + sel.length * HK.sec;
  setText("#hook-ep-stat", eps.length ? `已选 ${sel.length}/${eps.length} 集` : "");
  setText("#hook-calc", sel.length
    ? `共 ${sel.length} 条成片：正片 ${fmt(body)} + 钩子 ${sel.length}×${HK.sec}s ≈ 合计 ${fmt(total)}`
    : "一集都没选——至少选一集");
  const btn = $("#btn-hookcut");
  if (btn) {
    btn.textContent = `生成 ${sel.length} 集钩子`;
    btn.disabled = !sel.length || !!(p.task && taskBusy(p.task));
  }
  document.querySelectorAll("#hook-sec button").forEach((b) => {
    b.classList.toggle("on", Number(b.dataset.sec) === HK.sec);
  });
  // 模型下拉跟混剪那边同一份候选（探不通的会被过滤），默认「跟设置里的默认模型」
  const msel = $("#hook-model");
  if (msel) {
    const key = PM_MODELS.length + "|" + (pmAlive ? [...pmAlive].sort().join(",") : "");
    if (msel.dataset.key !== key) {
      msel.dataset.key = key;
      msel.innerHTML = '<option value="">（跟随设置）</option>'
        + pmOptions().filter(([v]) => v)
          .map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join("");
      msel.value = HK.model;
    }
  }
}

/* 「3-12」「1,3,5」「5-8,10」都认：想重出中间几集时随手打一行，
   不用在 20 个格子里一个个点。 */
function parseEpRange(txt) {
  const out = new Set();
  String(txt || "").split(/[,，;；\s]+/).forEach((part) => {
    if (!part) return;
    const m = part.match(/^(\d+)\s*(?:[-~－—到至]|\.{2})\s*(\d+)$/);
    if (m) {
      let a = Number(m[1]), b = Number(m[2]);
      if (a > b) { const t = a; a = b; b = t; }
      for (let i = a; i <= b; i++) out.add(i);
    } else if (/^\d+$/.test(part)) {
      out.add(Number(part));
    }
  });
  return out;
}

/* 钩子前置的一套结构 = 一集：两段（钩子段 + 整集正片）。卡片比混剪简单得多，
   这里没有「套间重合」「名场面撞车」那些问题——每集各出一条片，互不相干。 */
function hookCard(pl, i) {
  const epDur = {};
  (ST.proj.episodes || []).forEach((e) => (epDur[e.index] = e.duration || 0));
  const span = pl.hook_span || [0, 0];
  const hs = Number(span[0]) || 0, he = Number(span[1]) || 0;
  const rows = (pl.edl || []).map((s, k) => `
    <div class="pl-row">
      <span class="pc-seq">${k + 1}</span>
      <span class="pc-num">${String(s.ep).padStart(2, "0")}</span>
      ${posBar(s, epDur)}
      <span class="pc-dur">${fmt(s.end - s.start)}</span>
      <span class="pc-rate r${Math.min(10, Math.max(1, Math.round(s.rating || 5)))}">${Math.round(s.rating || 5)}分</span>
      <span class="pc-label">${esc(s.label || "")}</span>
    </div>`).join("");
  return `
    <div class="plan ${pl.on === false ? "off" : ""}" data-i="${i}">
      <div class="plan-head">
        <input type="checkbox" ${pl.on === false ? "" : "checked"}>
        <b>第 ${pl.ep} 集</b>
        ${pl.model ? `<span class="ov info" title="这一集用的生成方式">${esc(modelLabel(pl.model))}</span>` : ""}
        <span class="hint">钩子 ${fmt(he - hs)}（原片 ${fmt(hs)}–${fmt(he)}）+ 正片 ${fmt(pl.ep_dur || 0)} · 成片 ${fmt(pl.sec || 0)}</span>
        <span class="ov plan-fx ${pl.fx ? "info" : ""}" data-i="${i}" title="这一集的出片与画面设置：标题 / 片头卡 / 去重 / 配乐 / 调色 / 用哪份视频都能改；不改就跟随这批的默认">✎ 设置 · ${esc(pl.fx ? fxShort(pl.fx) : "跟随默认")}</span>
        <span class="plan-more">展开表 ▾</span>
      </div>
      <div class="plan-body hidden">
        <div class="pl-hook">
          <span class="pl-hook-k">片头大字</span>
          <input class="pl-hook-in" data-i="${i}" value="${esc(pl.hook || "")}"
            placeholder="勾了「开场钩子卡」才用得上；留空则这一集不出卡"
            title="AI 从这一集的钩子段提炼的一句话。改完直接出片就生效，不用重新生成。" />
        </div>
        <div class="pl-order">结构：钩子段（本集 ${fmt(hs)}–${fmt(he)}，提到片头）+ 整集正片（0:00–${fmt(pl.ep_dur || 0)}）；钩子那段在正片里会再出现一次</div>
        ${rows}
      </div>
    </div>`;
}

/* 缓冲变速锁：钩子前置是「钩子 + 整集」，整集按 1.15× 放快就变成快进的剧了。
   后端在 render_plans 里对这个模式强制原速，这里把开关置灰并写清原因——
   面板留着可点、成片却不生效，是最难查的那种对不上。 */
function syncHookFxLocks() {
  const hook = !!(ST.proj && (ST.proj.plans || []).some((pl) => pl.mode === "hook"));
  ["fx-", "sx-"].forEach((p) => {
    const el = document.getElementById(p + "speed");
    if (!el) return;
    el.disabled = hook;
    const lab = el.closest(".chk");
    if (!lab) return;
    lab.style.opacity = hook ? ".5" : "";
    lab.title = hook
      ? "这条产线是「钩子 + 整集正片」：整集加速会把正片变快、钩子却是爆点，所以固定原速"
      : "评分 5-6 的缓冲段（反应、沉默、过场）放快 1.15 倍，音调不变；爆点段保持原速。关掉就全片原速。";
  });
}

/* ---------- 剪辑与出片（N 套）：勾选视图 ---------- */
function renderPlans() {
  const p = ST.proj;
  const plans = (p && p.plans) || [];
  const list = $("#plan-list");
  if (!plans.length) {
    patchHtml(list, p.id + "|empty|" + HK.mode, () => `<div class="hint" style="padding:8px 0">${
      HK.mode === "hook"
        ? "还没有钩子结构：选好集数，点上面「生成 N 集钩子」，每集挑一个爆点放到片头"
        : "还没有结构，点上面「生成 N 套结构」，一次排出多套不同主线的剪法"
    }</div>`);
    $("#plan-stat").textContent = "";
    $("#plan-note").textContent = "";
    $("#btn-render-plans").textContent = HK.mode === "hook" ? "出片（已选 0 集）" : "出片（已选 0 套）";
    $("#btn-render-plans").disabled = true;
    return;
  }
  // 结构没变就不重建 DOM：出片时前端每两三秒轮询一次进度，每次都调这里，
  // 整区 innerHTML 重建会把用户展开的「每套详细内容」自动合上
  // （2026-11-03 用户反馈）。签变了（重新编排/勾选外的东西变了）才重建。
  const sig = p.id + "|" + plans.map((pl) => [
    pl.index, pl.sec, (pl.edl || []).length, pl.blocks || 0, pl.model || "",
    // 钩子前置要靠 mode/ep 区分：两段式结构每集的片数都是 2，只比片数会把
    // 「第 5 集重挑了一次钩子」当成没变，卡片停在旧位置上
    pl.mode || "", pl.ep || 0,
  ].join(":")).join("|");
  const needBuild = sig !== renderPlans._sig || !list.querySelector(".plan");
  if (needBuild) {
    // 真要重建时，先把当前展开着的那几套记下来，建完恢复
    const openSet = new Set(
      [...list.querySelectorAll(".plan")]
        .filter((el) => !el.querySelector(".plan-body").classList.contains("hidden"))
        .map((el) => el.dataset.i));
    renderPlans._sig = sig;
    list._sig = sig;
    list.innerHTML = plans.map((pl, i) => planCard(pl, i)).join("");
    // 重建完把用户原来展开着的几套恢复，再重新挂事件
    list.querySelectorAll(".plan").forEach((el) => {
      const i = Number(el.dataset.i);
      if (openSet.has(el.dataset.i)) {
        el.querySelector(".plan-body").classList.remove("hidden");
        el.querySelector(".plan-more").textContent = "收起 ▴";
      }
      el.querySelector('input[type="checkbox"]').addEventListener("change", (e) => {
        e.stopPropagation();
        onPlanToggle(i, e.target.checked);
      });
      const hk = el.querySelector(".pl-hook-in");
      if (hk) {
        hk.addEventListener("click", (e) => e.stopPropagation());
        hk.addEventListener("change", async (e) => {
          const v = e.target.value.trim();
          const pl = ST.proj.plans[i];
          if (!pl) return;
          const old = pl.hook || "";
          pl.hook = v;
          try {
            await api(`/api/projects/${ST.proj.id}/plan-hook`, {
              method: "PUT",
              body: JSON.stringify({ index: pl.index, hook: v }),
            });
            toast(v ? "钩子已保存——出片时用这句做片头" : "已清空——这套不出片头卡");
          } catch (err) {
            pl.hook = old;
            e.target.value = old;
            toast("钩子保存失败：" + err.message);
          }
        });
      }
      el.querySelector(".plan-head").addEventListener("click", (e) => {
        if (e.target.tagName === "INPUT") return;
        if (e.target.closest(".plan-fx")) { e.stopPropagation(); openPlanFx(i); return; }
        const body = el.querySelector(".plan-body");
        body.classList.toggle("hidden");
        el.querySelector(".plan-more").textContent = body.classList.contains("hidden") ? "展开表 ▾" : "收起 ▴";
      });
      updatePlanFxChips();
    });
  }
  // 头部备注/统计每拍都对齐一次（纯文本，改值才写，不会打断阅读）
  let note = (p.batch && p.batch.plan_note) || "";
  // 结构是另一次生成的模式：卡片照实显示（出片会用它们），但要说清「这不是当前面板
  // 生成的」，否则用户切了 tab 会以为自己点错了
  const planMode = plans.length ? (plans[0].mode === "hook" ? "hook" : "mix") : "";
  if (planMode && planMode !== HK.mode) {
    note = (planMode === "hook"
      ? "下面这些是「单集钩子前置」的结果"
      : "下面这些是「多套混剪」的结果")
      + "（不是在当前面板生成的），点「出片」会用它们。要换成当前这种，点生成按钮会覆盖这些结构。"
      + (note ? "；" + note : "");
  }
  if ($("#plan-note").textContent !== note) $("#plan-note").textContent = note;
  updatePlanFeasible();
  updatePlanStat();
  updatePlanFxChips();
  syncHookFxLocks();
}

/* 一套结构 = 一张卡片：头部一行概览 + 可展开的表 */
function planCard(pl, i) {
  // 单集钩子前置是另一种产物形态（一集一条），字段和体检指标都不一样
  if (pl.mode === "hook") return hookCard(pl, i);
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
  // 平均块长：一块 10-65 秒约等于「一场戏」，低于 10 秒太碎（观众还没进戏就切走）。
  // 65 秒以上不是「错」，而是块数可能太少：一块一场戏本来就该有多长就多长，
  // 但平均超过一分钟，4-5 分钟里只剩三四块，一套片子就没什么结构可言了。
  // （2026-11-02：块长无上限，只有下限；这里是软提示，不拦不剪）
  const perMin = pl.jumps != null ? pl.jumps / Math.max(pl.sec / 60, 1) : 0;
  const ab = pl.avg_block || 0;
  // 整集块：从 0:00 圈到集尾。一两块是正常的（那一集本身就是一场戏），
  // 过半就说明这套在处理「重播剧集」而不是「挑高光」。
  const wholeShare = pl.whole ? pl.whole / Math.max(1, pl.blocks || 0) : 0;
  const tempo = !ab ? "ok"
    : (ab > 65 || ab < 10 || wholeShare >= 0.5) ? "mid" : "ok";
  const tempoTip = (pl.whole ? `${pl.whole} 块是整集（0:00 到集尾）${wholeShare >= 0.5 ? "，这套更像重播剧集" : ""}；` : "")
    + (!ab ? "" : ab > 65 ? "平均块偏长，这套块数可能太少；" : ab < 10 ? "块偏碎；" : "节奏正常；")
    + (pl.avg_gap ? `平均每次跳过 ${Math.round(pl.avg_gap)} 秒；` : "")
    + `每分钟跳切 ${perMin.toFixed(1)} 次（跳过过程，正常）`;
  return `
    <div class="plan ${pl.on === false ? "off" : ""}" data-i="${i}">
      <div class="plan-head">
        <input type="checkbox" ${pl.on === false ? "" : "checked"}>
        <b>第 ${pl.index} 套</b>
        ${pl.model ? `<span class="ov info" title="这套用的生成方式">${esc(modelLabel(pl.model))}</span>` : ""}
        <span class="hint">${(pl.edl || []).length} 片${pl.blocks ? ` / ${pl.blocks} 块` : ""} · ${fmt(pl.sec || 0)}${pl.eps && pl.eps.length ? ` · 第 ${pl.eps[0]}–${pl.eps[1]} 集` : ""}${pl.style ? ` · ${esc(pl.style)}` : ""}</span>
        ${pl.jumps != null ? `<span class="ov ${tempo}" title="${tempoTip}">跳切 ${pl.jumps} 次${ab ? ` · 均片 ${ab}s` : ""}${pl.whole ? ` · 整集 ${pl.whole}` : ""}</span>` : ""}
        ${pl.climax >= 4 ? `<span class="ov ${pl.climax >= 6 ? "mid" : "ok"}" title="连续 8 分以上的块最多连着几块。超过 4 块中间没有喘息的地方，一路紧绷，观众看着会累">连爆 ${pl.climax}</span>` : ""}
        ${pl.overlap ? `<span class="ov ${pl.overlap > MAX_OVERLAP ? "bad" : pl.overlap > MAX_OVERLAP * 0.55 ? "mid" : "ok"}" title="和前面几套用了相同片段的比例（重合秒数 ÷ 较短那套）。要求不超过 80%，超了会标红；名场面可以共用，只要这个比例没破线">与前面重复 ${Math.round(pl.overlap * 100)}%</span>` : ""}
        ${pl.anchor_hit ? `<span class="ov ${pl.anchor_hit >= 3 ? "bad" : pl.anchor_hit >= 2 ? "mid" : "ok"}" title="和前面第 ${pl.anchor_vs} 套用了同一条名场面锚点（core 那句）。名场面可以两套都用，只要「与前面重复」没超过 80% 就行">锚点重 ${pl.anchor_hit}</span>` : ""}
        <span class="ov plan-fx ${pl.fx ? "info" : ""}" data-i="${i}" title="这一套单独的出片与画面设置：标题 / 片头卡 / 去重 / 配乐 / 调色 / 变形 / 特效 / 用哪份视频都能改；不改就跟随这批套的默认">✎ 设置 · ${esc(pl.fx ? fxShort(pl.fx) : "跟随默认")}</span>
        <span class="plan-more">展开表 ▾</span>
      </div>
      <div class="plan-body hidden">
        <div class="pl-hook">
          <span class="pl-hook-k">开场钩子</span>
          <input class="pl-hook-in" data-i="${i}" value="${esc(pl.hook || "")}"
            placeholder="片头黑屏上打的一句话（12-18 字），留空则这套不出片头卡"
            title="AI 从这套实际选中的戏里提炼的悬念句，出片时做成 1.8 秒黑屏大字放在最前面。可以自己改：改完直接出片就生效，不用重新生成结构。" />
        </div>
        ${pl.bgm ? `<div class="pl-order" title="第 3 步通读全剧后给出的配乐建议：这套剪法适合什么类型的音乐。出片时勾「配乐 BGM」，程序会照这个建议自动挑一首曲子铺上。">🎵 BGM 建议：${esc(pl.bgm)}</div>` : ""}
        ${pl.order_desc ? `<div class="pl-order">顺序：${esc(pl.order_desc)}${pl.seq ? ` · 集号走位 ${esc(pl.seq)}` : ""}</div>` : ""}
        ${rows}
      </div>
    </div>`;
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
  const hook = !!(plans.length && plans[0].mode === "hook");
  // 出片耗时按实测粗估：混剪每套约 2 分钟；钩子前置要重编码整集正片，
  // 100 秒的一集实测约 100 秒（1:1），按 1.7 分钟/集估
  const txt = plans.length
    ? `已选 ${on.length}/${plans.length} ${hook ? "集" : "套"} · 合计时长 ${fmt(sec)}`
      + `（出片约 ${Math.max(1, Math.round(on.length * (hook ? 1.7 : 2)))} 分钟）`
    : "";
  // 文本没变就不写：轮询每 1.5 秒一次，无谓的赋值会让浏览器重排版
  if ($("#plan-stat").textContent !== txt) $("#plan-stat").textContent = txt;
  const btn = $("#btn-render-plans");
  btn.textContent = hook ? `出片（已选 ${on.length} 集）` : `出片（已选 ${on.length} 套）`;
  const busy = !!(ST.proj && taskBusy(ST.proj.task));
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
  const hookMode = !!((p.plans || []).length && (p.plans || [])[0].mode === "hook");
  const unitName = hookMode ? "集" : "套";
  // 单集钩子前置的一「套」就是一集：列表里写「第 3 套」会让人以为还有第 4 套没出
  const labelOf = (i, tag) => tag || `第 ${i} ${hookMode ? "集" : "套"}`;
  const actions = $("#sets-actions");
  actions.style.display = ready ? "" : "none";
  if (ready) {
    const btnTxt = `📂 打开成片文件夹（${ready} ${unitName} · ${(totalMb / 1024).toFixed(1)}GB）`;
    if ($("#btn-open-folder").textContent !== btnTxt) $("#btn-open-folder").textContent = btnTxt;
    const noteTxt = (bdir
      ? `整批都在 ${bdir.replace(/\\/g, "/")}`
      : "整批成片都在同一个文件夹里")
      + (stale.length ? `　另有 ${stale.length} 个旧结构的成片记录（文件仍在原目录，不在本列表里）` : "");
    if ($("#sets-folder-note").textContent !== noteTxt) $("#sets-folder-note").textContent = noteTxt;
  }
  const fails = ((p.batch || {}).failures || [])
    .filter((f) => !sets.some((s) => s.index === f.index && s.output));
  const failHtml = fails.length
    ? fails.map((f) => `<div class="setrow" style="opacity:.75">
          <b>${labelOf(f.index, (sets.find((s) => s.index === f.index) || {}).tag)}</b>
          <span class="hint" style="color:#b45309">失败：${esc((f.error || "").slice(0, 80))}</span>
          <span class="hint">重新点「出片」会自动补做</span>
        </div>`).join("")
    : "";
  const sig = [
    p.id, curGen, planIdx.join(","), stale.length, bdir,
    sets.map((s) => [s.index, s.output || "", s.output_duration || 0,
      s.output_size || 0, (s.edl || []).length].join(":")).join("|"),
    fails.map((f) => [f.index, (f.error || "").slice(0, 40)].join(":")).join("|"),
  ].join("//");
  // 成片列表没变化就不重建：正在出片时每 1.5 秒一轮询，重建会把用户滚到的位置弹回去
  patchHtml($("#sets-list"), sig, () => (sets.length || failHtml)
    ? sets.map((s) => {
        return `<div class="setrow">
          <b>${labelOf(s.index, s.tag)}</b>
          <span class="hint">${s.output ? `${(s.edl || []).length} 段 · ${fmt(s.output_duration || 0)} · ${((s.output_size || 0) / 1048576).toFixed(0)}MB` : "按当前结构待生成"}</span>
          ${s.output
            ? `<button class="ghost" data-play="${s.index}">用播放器打开 ↗</button>`
            : `<span class="hint">待生成</span>`}
        </div>`;
      }).join("") + failHtml
    : `<div class="hint" style="padding:6px 0">还没有批量产物</div>`);
}
/* ---------- 第 3 步：引流分镜 ---------- */
/* 界面状态跟项目走：只在切换项目时回填一次，否则每 1.5 秒的轮询会把用户
   刚勾/刚取消的选择冲掉（跟 HK 那套同一个道理）。 */
const SH = { pid: "" };

function syncShotState() {
  const p = ST.proj;
  if (!p || SH.pid === p.id) return;
  SH.pid = p.id;
  const s = p.shots || {};
  const ctl = $("#in-shot-ctl");
  if (ctl) ctl.checked = s.ctl !== false;   // 默认勾选；只有显式关过（存了 false）才不勾
}

function renderShots() {
  const p = ST.proj;
  const list = $("#shots-list");
  if (!p || !list) return;
  syncShotState();
  const shots = p.shots;
  const warn = $("#shots-warn");
  // 分镜没变化就完全不碰 DOM：轮询每 1.5 秒一次，重建会把用户展开的那条合上、
  // 把正在读的位置滚走（2026-11-03 用户要求：刷新只动该动的那块）
  const segTotal = shots ? ((shots.seg_total != null) ? shots.seg_total
    : (shots.episodes_md || []).reduce((a, e) => a + (e.segments || []).length, 0)) : 0;
  const sig = !shots ? p.id + "|none" : [p.id, shots.gen, shots.lay || 0, shots.ctl ? 1 : 0,
    shots.count, shots.duration, (shots.episodes_md || []).length, segTotal,
    (shots.warnings || []).length].join(":");
  if (list._sig === sig) return;
  list._sig = sig;
  if (!shots) {
    warn.classList.add("hidden");
    list.innerHTML = `<div class="hint" style="padding:6px 0">还没有分镜。点上面「生成引流分镜」——会根据剧情构思几条短片，逐镜写成能直接复制去用的提示词。</div>`;
    return;
  }
  if ((shots.warnings || []).length) {
    warn.classList.remove("hidden");
    warn.innerHTML = "<b>需要留意：</b><br>" +
      shots.warnings.map((w) => esc(w)).join("<br>");
  } else {
    warn.classList.add("hidden");
  }
  // 重画前记住哪些镜号、哪些段是展开着的，画完恢复
  const openSet = new Set([...list.querySelectorAll(".shot-item.open")].map((el) => el.dataset.shot));
  const segOpen = new Set([...list.querySelectorAll(".seg-item.open")].map((el) => el.dataset.seg));
  const dlName = `${p.name}_引流分镜_${shots.duration}s.md`;
  const srcTag = shots.material === "custom"
    ? `我自己提供的剧情素材${shots.script_chars ? `（${shots.script_chars} 字）` : ""}` : "本剧台词";

  list.innerHTML =
    `<div class="shots-tool">
      <span class="hint">${shots.count} 条 · 每条 ${shots.duration}s · 共 ${segTotal
        || (shots.episodes_md || []).length} 段（每段单独复制去生成，拼起来就是整条） ·
        剧情来源：${srcTag} · 生成于 ${new Date(shots.gen * 1000).toLocaleString()}</span>
      <span class="sp"></span>
      <button class="ghost sm" id="btn-shots-open">全部展开</button>
      <button class="ghost sm" id="btn-shots-close">全部收起</button>
      <button class="ghost" id="btn-dl-shots">下载整篇 .md</button>
    </div>` +
    (shots.episodes_md || []).map((ep) => {
      const segs = ep.segments || [];
      return `
      <section class="shot-item" data-shot="${ep.no}">
        <header class="shot-head">
          <span class="caret"></span>
          <span class="shot-no">镜号${ep.no}</span>
          <b class="shot-title">${esc(ep.title)}</b>
          <span class="tag">${segs.length} 段</span>
          <span class="tag">${ep.seconds || shots.duration}s</span>
          ${ep.hook ? `<span class="shot-hook">${esc(ep.hook)}</span>` : ""}
          <span class="shot-log">${esc(ep.logline || "")}</span>
          <span class="sp"></span>
          <span class="toggle-text">展开</span>
        </header>
        <div class="shot-body hidden">
          ${segs.length ? segs.map((g) => `
            <div class="seg-item" data-seg="${g.label}">
              <div class="seg-head">
                <span class="caret sm"></span>
                <span class="seg-letter">${g.letter}</span>
                <span class="seg-name">${g.letter} 段</span>
                <span class="seg-meta">${g.t_start}—${g.t_end}s · ${g.shots} 镜${g.lines ? ` · ${g.lines} 句台词` : ""}</span>
                ${(g.hooks || []).length ? `<span class="tag soft">${esc((g.hooks || []).join("／"))}</span>` : ""}
                <span class="sp"></span>
                <button class="ghost sm" data-copy-seg="${g.label}">复制这段</button>
                <span class="toggle-text">查看提示词</span>
              </div>
              <pre class="seg-body hidden">${esc(g.md)}</pre>
            </div>`).join("")
          : `<div class="hint" style="padding:10px 12px">这条没有分段数据（旧版产物），点上面重新生成一次。</div>`}
        </div>
      </section>`;
    }).join("");

  // 展开态统一用一个 .open 类驱动：箭头旋转、头部变色、正文显隐都挂在它上面。
  // 之前只有一个小 ▾/▴ 文字提示，太轻，用户看不出能点（2026-11-03 反馈）。
  const setOpen = (item, on, openText, closeText) => {
    item.classList.toggle("open", on);
    const body = item.querySelector(":scope > .shot-body, :scope > .seg-body");
    if (body) body.classList.toggle("hidden", !on);
    const t = item.querySelector(":scope > .shot-head .toggle-text, :scope > .seg-head .toggle-text");
    if (t) t.textContent = on ? closeText : openText;
  };
  const allOpen = (on) => {
    list.querySelectorAll(".shot-item").forEach((el) => setOpen(el, on, "展开", "收起"));
    list.querySelectorAll(".seg-item").forEach((el) => setOpen(el, on, "查看提示词", "收起"));
  };

  list.querySelectorAll(".shot-item").forEach((el) => {
    setOpen(el, openSet.has(el.dataset.shot), "展开", "收起");
    el.querySelector(".shot-head").addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      setOpen(el, !el.classList.contains("open"), "展开", "收起");
    });
  });
  list.querySelectorAll(".seg-item").forEach((el) => {
    setOpen(el, segOpen.has(el.dataset.seg), "查看提示词", "收起");
    el.querySelector(".seg-head").addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      setOpen(el, !el.classList.contains("open"), "查看提示词", "收起");
    });
  });

  const btnOpen = $("#btn-shots-open");
  if (btnOpen) btnOpen.addEventListener("click", () => allOpen(true));
  const btnClose = $("#btn-shots-close");
  if (btnClose) btnClose.addEventListener("click", () => allOpen(false));

  const dl = $("#btn-dl-shots");
  if (dl) dl.addEventListener("click", () => {
    const blob = new Blob([shots.doc || ""], { type: "text/markdown;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = dlName;
    a.click();
    URL.revokeObjectURL(a.href);
  });
  const copy = (btn, text, label) => {
    navigator.clipboard.writeText(text)
      .then(() => { btn.textContent = "已复制 ✓"; setTimeout(() => { btn.textContent = label; }, 1500); })
      .catch(() => toast("复制失败，请展开后手动选择复制"));
  };
  list.querySelectorAll("[data-copy-seg]").forEach((btn) => {
    const label = btn.textContent;
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const id = btn.dataset.copySeg;
      let md = "";
      (shots.episodes_md || []).forEach((ep) => (ep.segments || []).forEach((g) => {
        if (g.label === id) md = g.md;
      }));
      if (md) copy(btn, md, label);
    });
  });
}

/* 提交中标志：从点下按钮到接口返回这段时间，服务端还没来得及报任务进度，
   只看 taskBusy 会有一段空窗，用户能连点两次（2026-11-03 反馈）。
   它只活到接口返回，之后交给 task 状态管。 */
let shotsSending = false;

/* 引流分镜能不能点，只由两件事决定：有没有任务在跑、剧情从哪来。
   转写进度不影响这一块——自己粘素材时它跟第 1 步完全无关
   （2026-11-03 用户要求：引流分镜可以独立，也可以和前面一起）。 */
function syncShotSrc() {
  const p = ST.proj;
  if (!p) return;
  const sel = $("#in-shot-src");
  const eps = p.episodes || [];
  const hasLines = eps.some((e) => e.status === "done") || (p.line_total || 0) > 0;
  // 只做分镜的项目里没有视频，默认就该是「自己粘」
  if (!eps.length && sel.value !== "custom") sel.value = "custom";
  const opt = sel.querySelector('option[value="script"]');
  if (opt) {
    opt.disabled = !hasLines;
    opt.textContent = hasLines ? "用本剧台词" : "用本剧台词（还没整理好）";
  }
  const custom = sel.value === "custom";
  $("#shot-script-wrap").classList.toggle("hidden", !custom);
  const n = ($("#in-shot-script").value || "").trim().length;
  $("#shot-script-note").textContent = custom
    ? (n ? `已粘 ${n} 字素材：只用这段，不读本剧台词，跟前几步完全无关。`
        : "把剧情素材粘进上面的框里，至少写清谁是谁、谁害了谁、关键转折。不用管第 1 步有没有做。")
    : (hasLines ? "用本剧整理好的全部台词当素材。"
        : "本剧还没有台词：等第 1 步做完，或把上面的「剧情来源」改成「自己粘剧情素材」。");
  // 只看第 3 步自己的任务：第 2 步在出片/排结构时，分镜照样能点（2026-11-08）
  $("#btn-shots").disabled = shotsSending || taskBusy(p.task_shots) || !(custom || hasLines);
}
$("#in-shot-src").addEventListener("change", syncShotSrc);
$("#in-shot-script").addEventListener("input", syncShotSrc);

/* 控制句开关只影响排版：已经生成过分镜的，勾一下就走「免 AI 重排」，
   不花额度、不用等模型。还没生成过就什么都不做，下次生成时带上这个选择。 */
$("#in-shot-ctl").addEventListener("change", async () => {
  const p = ST.proj;
  const ctl = $("#in-shot-ctl").checked;
  if (!p || !(p.shots && (p.shots.episodes || []).length)) {
    toast(ctl ? "已记下：下次生成会带上控制句" : "已记下：下次生成不带控制句");
    return;
  }
  try {
    await api(`/api/projects/${p.id}/shots-layout`, {
      method: "POST", body: JSON.stringify({ ctl }),
    });
    ST.proj = await api("/api/projects/" + p.id);
    render();
    toast(ctl ? "已重新排版：每段开头带上控制句" : "已重新排版：控制句已去掉");
  } catch (e) {
    $("#in-shot-ctl").checked = !ctl;          // 失败就把开关退回原位，别让界面说谎
    toast(e.message || "重排失败");
  }
});

/* 单条时长 15 秒时没有「30 秒一段」可切：把 30 秒选项禁掉并回落 15，
   否则用户选了组合会被后端静默改成 15，界面跟产物对不上。 */
(function () {
  const dur = $("#in-shot-dur"), seg = $("#in-shot-seg");
  if (!dur || !seg) return;
  const opt30 = seg.querySelector('option[value="30"]');
  const sync = () => {
    const d = Number(dur.value) || 60;
    const tooBig = d < 30;
    if (opt30) {
      opt30.disabled = tooBig;
      opt30.textContent = tooBig ? "30 秒 · 整条不够长，用不了" : "30 秒 · 段数减半";
    }
    if (tooBig && seg.value === "30") seg.value = "15";
  };
  dur.addEventListener("change", sync);
  sync();
})();

$("#btn-shots").addEventListener("click", async () => {
  if (shotsSending) return;   // 双击的第一层拦截（disabled 之外再兜一层）
  const p = ST.proj;
  const custom = $("#in-shot-src").value === "custom";
  const script = custom ? ($("#in-shot-script").value || "").trim() : "";
  if (custom && script.length < 30) {
    return toast("剧情素材太短，至少写 30 字（说清人物关系和一个关键转折）");
  }
  if (!custom && !((p.episodes || []).length)) {
    return toast("还没有台词：先完成第 1 步，或把「剧情来源」改成「自己粘剧情素材」");
  }
  const count = Math.max(1, Math.min(10, Number($("#in-shot-count").value) || 3));
  const rawDur = Number($("#in-shot-dur").value);
  const duration = rawDur >= 60 ? 60 : rawDur >= 30 ? 30 : 15;
  const segSel = $("#in-shot-seg");
  let segLen = Number(segSel ? segSel.value : 15) >= 30 ? 30 : 15;
  if (segLen > duration) segLen = duration;   // 15 秒整条没有 30 秒段可切
  if (p.shots && !confirm("重新生成会覆盖现有分镜（已下载的文件不受影响），继续？")) return;
  if (!confirm(`会根据${custom ? "你粘的剧情素材" : "全剧台词"}构思 ${count} 条 ${duration}s 引流短片，`
    + `再拆成 ${Math.max(1, Math.round(duration / segLen))} 段 ${segLen} 秒，每段一份能单独复制的提示词。\n\n`
    + "不是从原片里挑段、也不是改写原文——冲突怎么挑、节奏怎么排、台词怎么写都由它定；\n"
    + "同一条片里同一个人只有一份音色，A/B/C/D 各段共用（拼成一条不会换声）；不同条可以各用各的音色。\n"
    + "会用到 1 次今日生成机会，这一步大约需要几分钟。继续？")) return;
  // 确认完就立刻锁住按钮：接口往返、后端读项目拼素材这段时间，
  // 界面上还没有任何任务状态，不锁就能再点一次（重复提交 → 两次调用）。
  const btn = $("#btn-shots");
  shotsSending = true;
  btn.disabled = true;
  btn.textContent = "生成中…";
  try {
    const grant = await aiGrant(p.id, "shots");
    await api(`/api/projects/${p.id}/shots`, {
      method: "POST",
      headers: { "X-AI-Grant": grant },
      body: JSON.stringify({
        count, duration, seg_len: segLen,
        style: $("#in-shot-style").value.trim(),
        cast: $("#in-shot-cast").value.trim(),
        ctl: !!($("#in-shot-ctl") || {}).checked,
        script,
      }),
    });
    ST.proj.task_shots = { kind: "shots", pct: 0, msg: custom ? "AI 正在读素材、写分镜" : "AI 正在读剧情、写分镜", error: "" };
    render();
    startPoll();
  } catch (e) {
    toast(e.message || "发起失败");
    // 「已有任务在跑」说明上一次其实已经提交成功，把真实状态拉回来，
    // 别让界面停在“没任务、按钮能点”的假象里。
    if (/已有任务/.test(e.message || "")) {
      try { ST.proj = await api("/api/projects/" + p.id); render(); startPoll(); } catch { /* 忽略瞬时失败 */ }
    }
  } finally {
    shotsSending = false;
    btn.textContent = "生成引流分镜";
    syncShotSrc();   // 能不能重新点，仍由 taskBusy 一处统一决定
  }
});
syncShotSrc();

function _tableStat(text) {  const rows = text.split("\n").map((l) => l.trim())
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

$("#edl").addEventListener("input", updateStat);

function startPoll() {
  stopPoll();
  let idle = 0;
  ST.timer = setInterval(async () => {
    try {
      ST.proj = await api("/api/projects/" + ST.proj.id);
      render();
      // 三条产线任一在跑就要继续轮询；全都停了才算空闲
      const busyNow = taskBusy(ST.proj.task) || taskBusy(ST.proj.task_nosub)
        || taskBusy(ST.proj.task_shots);
      if (!busyNow) {
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

/* ---------- 第 1 步的两个省时入口：复用其他项目 / 粘贴台词 ----------
   场景（用户 2026-11-05 提的）：同一个剧新建项目，第一个项目已经转写好了，
   没必要再整理一遍；或者台词是从别处拿到的（本工具导出的台词文本、带时间轴的字幕）。 */
const linesModal = $("#modal-lines");

/* 只刷新当前项目，不触发自动转写（openProject 会自动开转写，这里不能用） */
async function refreshProject() {
  if (!ST.proj) return;
  ST.proj = await api("/api/projects/" + ST.proj.id);
  render();
  loadList();
}

async function openLinesModal() {
  const p = ST.proj;
  if (!p) return;
  if (!(p.episodes || []).length) return toast("这个项目没有导入视频，用不上台词");
  $("#ln-stat").textContent = "";
  $("#ln-text").value = "";
  $("#ln-ep").innerHTML = `<option value="0">自动按「【第03集】」分段</option>`
    + p.episodes.map((e) => `<option value="${e.index}">整段都算第 ${e.index} 集（${esc(e.file || "")}）</option>`).join("");
  linesModal.classList.remove("hidden");
  await loadReuseOptions("#ln-from", p.id);
}

$("#btn-reuse").addEventListener("click", openLinesModal);
$("#btn-paste").addEventListener("click", openLinesModal);
$("#btn-close-lines").addEventListener("click", () => linesModal.classList.add("hidden"));

$("#btn-do-reuse").addEventListener("click", async () => {
  const from = $("#ln-from").value;
  if (!from) return toast("先选一个要复用的项目");
  const btn = $("#btn-do-reuse");
  btn.disabled = true;
  try {
    const r = await api(`/api/projects/${ST.proj.id}/reuse-lines`,
      { method: "POST", body: JSON.stringify({ from_pid: from }) });
    toast(`已复用 ${r.copied} 集台词，共 ${r.line_total} 句`);
    linesModal.classList.add("hidden");
    await refreshProject();
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
  }
});

$("#btn-do-paste").addEventListener("click", async () => {
  const text = $("#ln-text").value.trim();
  if (!text) return toast("先把台词粘进来");
  const btn = $("#btn-do-paste");
  btn.disabled = true;
  $("#ln-stat").textContent = "解析中…";
  try {
    const r = await api(`/api/projects/${ST.proj.id}/import-lines`, {
      method: "POST",
      body: JSON.stringify({ text, ep: Number($("#ln-ep").value) || 0 }),
    });
    toast(`已导入 ${r.episodes} 集台词，项目共 ${r.line_total} 句`);
    linesModal.classList.add("hidden");
    await refreshProject();
  } catch (e) {
    $("#ln-stat").textContent = "导入失败：" + e.message;
    toast(e.message);
  } finally {
    btn.disabled = false;
  }
});

/* ---------- 第 1 步：台词逐集查看 / 修改 ----------
   转写是语音识别，错字、漏句、时间码偏一点都常见。以前只能新窗口看全文、下载，
   改不了；这里一次取回全部集，逐集编辑，保存只覆盖当前这一集，解析规则复用
   「粘贴台词」那套，所以格式、排序、时间码校验完全一致。 */
const viewLinesModal = $("#modal-view-lines");
const VL = { pid: "", eps: [], cur: 0, dirty: false };

function vlMsg(text, bad) {
  const el = $("#vl-msg");
  el.textContent = text || "";
  el.style.color = bad ? "#d05a4a" : "";
}

function vlRenderChips() {
  $("#vl-eps").innerHTML = VL.eps.map((e) =>
    `<button class="ep-chip${e.index === VL.cur ? " on" : ""}" data-ep="${e.index}"
       title="${esc(e.file || "")}">第${String(e.index).padStart(2, "0")}集<small>${e.count} 句</small></button>`
  ).join("");
  const cur = VL.eps.find((e) => e.index === VL.cur) || {};
  $("#vl-stat").textContent = VL.eps.length
    ? `共 ${VL.eps.length} 集 / ${VL.eps.reduce((a, e) => a + e.count, 0)} 句 · 当前第 ${VL.cur} 集（${cur.duration || 0} 秒）`
    : "这一集还没有台词";
  $("#vl-download").href = `/api/projects/${VL.pid}/transcript?ep=${VL.cur}&download=1`;
  $("#vl-open-all").href = `/api/projects/${VL.pid}/transcript`;
}

function vlSelect(i) {
  if (VL.dirty && i !== VL.cur) {
    return toast("这一集改了还没保存，先点「保存这一集」或「放弃修改」");
  }
  VL.cur = i;
  const e = VL.eps.find((x) => x.index === i);
  $("#vl-text").value = e ? e.text : "";
  VL.dirty = false;
  vlMsg("");
  vlRenderChips();
}

async function openViewLines() {
  if (!ST.proj) return;
  VL.pid = ST.proj.id;
  vlMsg("读取台词中…");
  try {
    const r = await api(`/api/projects/${VL.pid}/lines`);
    VL.eps = r.episodes || [];
    $("#vl-name").textContent = r.name || "";
    viewLinesModal.classList.remove("hidden");
    const first = VL.eps.find((e) => e.count) || VL.eps[0];
    VL.cur = first ? first.index : 0;
    VL.dirty = false;
    vlSelect(VL.cur);
  } catch (e) {
    toast(e.message);
  }
}

function closeViewLines() {
  if (VL.dirty) return toast("这一集改了还没保存，先保存或点「放弃修改」");
  viewLinesModal.classList.add("hidden");
}

$("#btn-lines-view").addEventListener("click", openViewLines);
$("#btn-close-vl").addEventListener("click", closeViewLines);
viewLinesModal.addEventListener("click", (e) => {
  if (e.target.id === "modal-view-lines") closeViewLines();
});
$("#vl-text").addEventListener("input", () => { VL.dirty = true; });
$("#vl-eps").addEventListener("click", (e) => {
  const chip = e.target.closest(".ep-chip");
  if (chip) vlSelect(Number(chip.dataset.ep));
});
$("#btn-vl-reset").addEventListener("click", () => vlSelect(VL.cur));

$("#btn-vl-save").addEventListener("click", async () => {
  const btn = $("#btn-vl-save");
  const text = $("#vl-text").value;
  btn.disabled = true;
  vlMsg("保存中…");
  try {
    const r = await api(`/api/projects/${VL.pid}/lines`, {
      method: "PUT", body: JSON.stringify({ ep: VL.cur, text }),
    });
    // 重新取一次：解析后句子可能重新排序，界面显示的和存进项目的要完全一致
    const fresh = await api(`/api/projects/${VL.pid}/lines`);
    VL.eps = fresh.episodes || [];
    const cur = VL.eps.find((x) => x.index === VL.cur);
    $("#vl-text").value = cur ? cur.text : text;
    VL.dirty = false;
    vlMsg(`第 ${VL.cur} 集已保存（${r.line_count} 句）。之前生成的结构和分镜要用新台词，请重新生成一次。`);
    vlRenderChips();
    toast("台词已保存");
    await refreshProject();
  } catch (e) {
    vlMsg("保存失败：" + e.message, true);
    toast(e.message);
  } finally {
    btn.disabled = false;
  }
});

/* 第 2 步：生成 N 套结构（AI 通读全剧，走 1 次调用） */

/* 套间重合度上限，必须与后端 app/batch.py 的 MAX_OVERLAP 保持一致。
   放在这里是为了让下面的文案能先引用到（const 有 TDZ，晚定义会报错）。 */
const MAX_OVERLAP = 0.8;

/* 播放顺序固定严格集序（2026-11-03 用户决定）：把高分块挪到开头当钩子会让观众
   开场就看到后面才发生的戏，因果断了。旧的下拉（自动轮换/变序开场）已从页面移除，
   这里只发固定值，老配置里存的 order 也不再读。 */
const FIXED_ORDER = "chrono";

/* 按素材推荐参数：套数 × 单套时长 ≈ 素材总长的八成。
   为什么是八成：同一部剧剪 N 套，素材共用是必然的；需求总量一旦逼近整部剧长度，
   「几套不一样」在数学上就不成立——2026-11-04 实测把 21.7 分钟素材硬塞进
   3×10.5 分钟，四个模型的重合度全在 77-100%，等于同一部片换个顺序剪三遍。
   单套压到 3 分钟以下讲不完一场戏，压到 12 分钟以上又回到「整集搬」，
   所以两头都夹住；四个档位是混剪常见的规格，不追求精确。 */

/* 各模型时长不一样时，不能把最短的下限和最长的上限拼成「每套 4~11 分钟」。 */
function mixDurText(b) {
  const jobs = Array.isArray(b.jobs) ? b.jobs : [];
  if (jobs.length > 1) {
    const a = jobs[0];
    const same = jobs.every((j) => j.min_minutes === a.min_minutes && j.max_minutes === a.max_minutes);
    if (!same) return "各模型时长见下方";
    if (a.min_minutes) return `每套 ${a.min_minutes}~${a.max_minutes} 分钟`;
  }
  return b.min_minutes ? `每套 ${b.min_minutes}~${b.max_minutes} 分钟` : "每套时长在下方批量面板设置";
}

function recommendPlanParams(totalMin) {
  if (!totalMin || totalMin < 1) return null;
  const count = totalMin >= 26 ? 4 : totalMin >= 14 ? 3 : totalMin >= 7 ? 2 : 1;
  let d = (totalMin * 0.8) / count;
  d = Math.min(12, Math.max(3, Math.round(d * 2) / 2));
  return { count, lo: Math.max(0.5, d - 0.5), hi: d + 0.5 };
}

function applyRecommend() {
  const p = ST.proj;
  if (!p) return null;
  const totalMin = (p.episodes || []).reduce((a, e) => a + (e.duration || 0), 0) / 60;
  const r = recommendPlanParams(totalMin);
  if (!r) return null;
  // 每个模型行都填同一组推荐值——多模型对比要控制变量，参数不一致比出来没意义
  document.querySelectorAll("#plan-models .pm-row").forEach((row) => {
    row.querySelector(".pm-count").value = r.count;
    row.querySelector(".pm-min").value = r.lo;
    row.querySelector(".pm-max").value = r.hi;
  });
  updatePlanFeasible();
  return r;
}

/* 读一次「上次检测结果」缓存（不现场探针，毫秒返回）；探不通的型号从下拉里去掉 */
async function loadModelsAlive() {
  try {
    const r = await api("/api/models-available");
    const items = r.items || [];
    if (!items.length) return;                     // 没检测过：下拉先按已知能用的放全
    pmAlive = new Set(items.filter((i) => i.ok).map((i) => i.model));
    const t = r.checked_at ? new Date(r.checked_at * 1000) : null;
    const dead = PM_MODELS.filter(([v]) => v && !pmAlive.has(v)).map(([v]) => modelLabel(v));
    // 设置页的候选也一起清：连不通却存成默认，下一步只会白等
    document.querySelectorAll("#model-list option").forEach((o) => {
      if (!pmAlive.has(o.value)) o.remove();
    });
    setText($("#pm-scan-note"), dead.length
      ? `上次检测（${t ? t.toLocaleString() : "—"}）：${dead.join("、")} 暂不可用，已隐藏。`
      : `上次检测（${t ? t.toLocaleString() : "—"}）：全部可用。`);
  } catch (_) { /* 检测服务读不到，就用自带清单，不影响出结构 */ }
}

$("#btn-plan-scan").addEventListener("click", async () => {
  const btn = $("#btn-plan-scan");
  const old = btn.textContent;
  btn.disabled = true;
  setText(btn, "检测中…（约 10 秒）");
  try {
    const r = await api("/api/models-available?refresh=1");
    const ok = new Set((r.items || []).filter((i) => i.ok).map((i) => i.model));
    pmAlive = ok;
    document.querySelectorAll("#plan-models .pm-row").forEach((row) => fillPmSelect(row));
    const dead = PM_MODELS.filter(([v]) => v && !ok.has(v)).map(([v]) => modelLabel(v));
    const t = r.checked_at ? new Date(r.checked_at * 1000).toLocaleString() : "";
    setText($("#pm-scan-note"), dead.length
      ? `检测完成（${t}）：${dead.join("、")} 暂不可用，已隐藏。`
      : `检测完成（${t}）：全部可用。`);
  } catch (e) {
    setText($("#pm-scan-note"), "检测失败：" + (e.message || e));
  } finally {
    btn.disabled = false;
    setText(btn, old);
  }
});

$("#btn-plan-add").addEventListener("click", () => {
  const rows = planJobs();
  const base = rows[0] || { model: ST.llmModel || "", count: 3, lo: 4, hi: 5 };
  // 新行默认复制第一行的参数——对比实验从同一组参数开始改，不容易填错
  addPlanModelRow(base.model, base.count, base.lo, base.hi);
});

$("#btn-plan-auto").addEventListener("click", () => {
  const r = applyRecommend();
  toast(r ? `按素材算：${r.count} 套 × 约 ${((r.lo + r.hi) / 2).toFixed(1)} 分钟`
          : "还没整理好台词，算不出素材总长");
});

/* 下单前的成本提示：几套 × 每套几分钟 vs 全剧多少分钟。
   同一部剧剪多套，素材必然复用——需求总量超过全剧时长时，重合度就会上去，
   与其等出完再看「与前面重合 78%」，不如点之前就说清楚。 */
/* ---- 模型行：每行一个模型，各自配 套数 / 最短 / 最长（2026-11-04）---- */
/* 第 2 步「模型」下拉的可选项 = 只放「实测真的能出结构」的型号。
   - 探针只证明「这个 Key 能连上」，不能证明「塞 30 分钟台词能跑完」：
     kimi-k3.1-pro 探针 4.7 秒通、真跑 30 分钟零产出；step-5 探针 3.2 秒通、
     34 分钟无响应 —— 这两个都不放进来。
   - 没做过结构横评的（qwen-max、GLM-5、MiniMax-M3、deepseek-v4.1-flash）也不放，
     要试就选「自定义…」手输名字。
   - **跑得通 ≠ 能用**（2026-11-05 补）：横评重合 >90% 的照样不进下拉——
     qwen-plus（三套重合 96%）、MiniMax-M2.7（93%）就是这样剔掉的，
     准入标准和 kimi/step「探针通但零产出」是同一条：出不了可用结果的概不提供。
   - 每条都能在 docs/模型对比报告.html 里查到记录。
   - pmAlive：点「检测可用模型」后，现在探不通的（欠费/没开通）会从下拉里去掉，
     但不会动「自己填进来的型号」——那一行会切到「自定义…」保留原值。 */
/* 只改显示名，value 仍是真实代号、提交逻辑一行没动：
   用户看到的是「快速 / 精细」这类档位，不是服务商和型号。 */
const PM_MODELS = [
  ["", "跟随设置（推荐）"],
  ["qwen3.5-plus", "默认 · 稳定"],
  ["ZHIPU/GLM-5.3", "快速 · 长剧本"],
  ["ZHIPU/GLM-5.2", "快速"],
  ["deepseek-v4-pro", "精细"],
];
let pmAlive = null;   // null = 没检测过，全放；Set = 只放这里面的（+默认+自定义）

function pmOptions() {
  return PM_MODELS.filter(([v]) => !v || !pmAlive || pmAlive.has(v));
}

function pmSelectHTML() {
  return pmOptions().map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join("")
    + '<option value="__custom">自定义…</option>';
}

/* 重刷一行的下拉选项，同时保留这行原来选的型号（不在候选里就落到「自定义」）。 */
function fillPmSelect(row, model) {
  const sel = row.querySelector(".pm-name");
  const keep = model === undefined ? pmModel(row) : model;
  const wasCustom = sel.value === "__custom";
  const shown = keep === undefined ? "" : keep;
  sel.innerHTML = pmSelectHTML();
  setPmModel(row, wasCustom && !shown ? row.querySelector(".pm-name-custom").value : shown);
}

/* 把一行的模型设成 model：在候选表里就用下拉，不在就切到「自定义」手输框。
   这样回填旧项目/设置页里存的自定义型号也不会丢。 */
function setPmModel(row, model) {
  const sel = row.querySelector(".pm-name");
  const cus = row.querySelector(".pm-name-custom");
  const v = (model || "").trim();
  if (!v || PM_MODELS.some(([k]) => k === v)) {
    sel.value = v;
    cus.value = "";
    cus.style.display = "none";
  } else {
    sel.value = "__custom";
    cus.value = v;
    cus.style.display = "";
  }
}

function pmModel(row) {
  const sel = row.querySelector(".pm-name");
  return sel.value === "__custom"
    ? row.querySelector(".pm-name-custom").value.trim()
    : sel.value.trim();
}

function pmRow(model, count, lo, hi) {
  const div = document.createElement("div");
  div.className = "pm-row";
  // 索引按「当前已有行数 + 1」算，新增时自然递增；删除后由 renumberPlanRows 重排
  const idx = document.querySelectorAll("#plan-models .pm-row").length + 1;
  div.innerHTML = `
    <span class="pm-grp">第 ${idx} 组</span>
    <div class="pm-name-wrap">
      <select class="pm-name">${pmSelectHTML()}</select>
      <input class="pm-name-custom" placeholder="自定义模型名" spellcheck="false"
        style="display:none">
    </div>
    <input class="pm-count" type="number" min="1" max="20" value="${count}" title="套数" placeholder="套数">
    <input class="pm-min" type="number" min="0.5" max="60" step="0.5" value="${lo}" title="最短（分钟）" placeholder="最短">
    <input class="pm-max" type="number" min="0.5" max="60" step="0.5" value="${hi}" title="最长（分钟）" placeholder="最长">
    <button class="ghost pm-del" title="移除这一组">✕</button>`;
  setPmModel(div, model);
  div.querySelector(".pm-name").addEventListener("change", (ev) => {
    const cus = div.querySelector(".pm-name-custom");
    const custom = ev.target.value === "__custom";
    cus.style.display = custom ? "" : "none";
    if (custom) cus.focus();
    else cus.value = "";
    updatePlanFeasible();
  });
  div.querySelectorAll("input").forEach((el) => el.addEventListener("input", updatePlanFeasible));
  div.querySelector(".pm-del").addEventListener("click", () => {
    div.remove();
    renumberPlanRows();
    updatePlanFeasible();
  });
  return div;
}

/* 删除某一组后，把剩下的「第 N 组」重新顺号，避免 1、3、4 这种断号看着乱。 */
function renumberPlanRows() {
  document.querySelectorAll("#plan-models .pm-row").forEach((row, i) => {
    const g = row.querySelector(".pm-grp");
    if (g) g.textContent = `第 ${i + 1} 组`;
  });
}

function addPlanModelRow(model, count, lo, hi) {
  $("#plan-models").appendChild(pmRow(model, count, lo, hi));
  updatePlanFeasible();
}

/* 与后端 core.max_concurrent() / PLAN_EST_MIN=3 同步：每组约 3 分钟，
   同时跑几组取设置里的「同时生成几条」（默认 3）。
   点生成前就把预计时间说清楚，别让用户对着进度条猜还要等多久。 */
function planEta(n) {
  const cap = (SETTINGS && SETTINGS.max_concurrent) || 3;
  return Math.ceil(Math.max(1, n) / cap) * 3;
}

function planJobs() {
  return [...document.querySelectorAll("#plan-models .pm-row")].map((row) => ({
    model: pmModel(row),
    count: Math.max(1, Math.min(20, Number(row.querySelector(".pm-count").value) || 3)),
    lo: Number(row.querySelector(".pm-min").value) || 4,
    hi: Number(row.querySelector(".pm-max").value) || 5,
  }));
}

let planBuiltFor = "";
/* 重建第 2 步的参数表单：只在「进入这个项目」时按 b.jobs 建一次。
   生成中 / 轮询里绝不重建或回填——否则会把用户刚改的参数覆盖回上一轮的值，
   或把多组参数错乱成一堆重复行（刷新后只恢复 1 行、跨项目串台都是这个坑）。 */
function initPlanModels(p) {
  const box = document.querySelector("#plan-models");
  if (!box) return;
  if (planBuiltFor === (p && p.id)) return;   // 同一项目只建一次，保留用户正在改的内容
  planBuiltFor = (p && p.id) || "";
  box.querySelectorAll(".pm-row").forEach((el) => el.remove());
  const b = (p && p.batch) || {};
  const jobs = Array.isArray(b.jobs) ? b.jobs : [];
  if (jobs.length) {
    jobs.forEach((j) =>
      box.appendChild(pmRow(j.model || "", j.count, j.min_minutes, j.max_minutes)));
  } else {
    box.appendChild(pmRow(ST.llmModel || "", 3, 4, 5));
  }
  renumberPlanRows();
  // 新项目（没出过结构、也没存过 jobs）：按素材总时长自动填一组推荐参数，
  // 省得默认 3 套 × 4-5 分钟去硬套 10 分钟的素材（2026-11-04）
  if (!b.count && !(p && (p.plans || []).length)) applyRecommend();
  updatePlanFeasible();
}

function updatePlanFeasible() {
  const p = ST.proj;
  const el = $("#plan-feasible");
  const btn = $("#btn-plan");
  if (!el) return;
  const jobs = planJobs();
  const totalSets = jobs.reduce((a, j) => a + j.count, 0) || 3;
  if (btn) setText(btn, jobs.length > 1 ? `生成 ${totalSets} 套结构（${jobs.length} 组）`
                                         : `生成 ${totalSets} 套结构`);
  if (!p) return;
  const totalMin = (p.episodes || []).reduce((a, e) => a + (e.duration || 0), 0) / 60;
  if (!totalMin) {
    setText(el, "");
    return;
  }
  const need = jobs.reduce((a, j) => {
    const lo = Math.min(j.lo, j.hi), hi = Math.max(j.lo, j.hi);
    return a + j.count * (lo + hi) / 2;
  }, 0);
  const avg = totalSets ? need / totalSets : 4;
  const reuse = need / totalMin;
  const detail = jobs.length > 1
    ? `（${jobs.map((j) => `${modelLabel(j.model)}×${j.count}`).join("，")}）` : "";
  const base = `全剧 ${totalMin.toFixed(1)} 分钟；`
    + (jobs.length > 1 ? `${jobs.length} 组共 ${totalSets} 套${detail}` : `${totalSets} 套`)
    + ` × 每套约 ${avg.toFixed(1)} 分钟 = ${need.toFixed(1)} 分钟，相当于整部剧的 ${Math.round(reuse * 100)}%`;
  const warn = reuse > 1.0;
  setText(el, reuse <= 0.7
    ? `${base}，素材够分，几套内容能错得开。`
    : reuse <= 1.0
      ? `${base}，素材要用掉大半，几套之间会有些重复（正常，能接受）。`
      : `${base}，已经超过全剧长度——素材必然重复。按「整部剧只用八成」算，${totalSets} 套每套最多 ${(totalMin * 0.8 / totalSets).toFixed(1)} 分钟；`
        + `想保持每套 ${avg.toFixed(1)} 分钟，最多只能出 ${Math.max(1, Math.floor(totalMin * 0.8 / avg))} 套。`);
  el.classList.toggle("warn", warn);
}

$("#btn-plan").addEventListener("click", async () => {
  const p = ST.proj;
  if (!((p.episodes || []).length)) return toast("还没整理好台词，先完成第 1 步");
  if ((p.plans || []).length && !confirm("重新生成结构会覆盖现有 " + p.plans.length + " 套结构（已出的片还在），继续？")) return;
  const jobs = planJobs();
  if (!jobs.length) return toast("至少保留一组");
  const norm = jobs.map((j) => ({
    model: j.model, count: j.count,
    min_minutes: Math.min(j.lo, j.hi), max_minutes: Math.max(j.lo, j.hi),
  }));
  const totalSets = norm.reduce((a, j) => a + j.count, 0);
  const order = FIXED_ORDER;
  const lines = norm.map((j) =>
    `· ${modelLabel(j.model)}：${j.count} 套 × ${j.min_minutes}-${j.max_minutes} 分钟`).join("\n");
  if (!confirm(`AI 会通读全剧台词，按下面配置出结构：\n${lines}\n\n`
    + `共 ${totalSets} 套，会用到 ${norm.length} 次今日生成机会，预计约 ${planEta(norm.length)} 分钟。\n`
    + "生成后每套会标出与前面套的重复程度，只提示、不拦截。\n\n继续？")) return;
  try {
    const grant = await aiGrant(p.id, "plan");
    await api(`/api/projects/${p.id}/plan`, {
      method: "POST",
      headers: { "X-AI-Grant": grant },
      body: JSON.stringify({ jobs: norm, order }),
    });
    ST.proj.task = { kind: "plan", pct: 0,
      msg: norm.length > 1 ? `共 ${norm.length} 组，预计约 ${planEta(norm.length)} 分钟`
                           : "AI 正在通读全剧，预计约 3 分钟", error: "" };
    render();
    startPoll();
  } catch (e) {
    toast(e.message);
  }
});

$("#btn-plan-all").addEventListener("click", () => planSelectAll(true));
$("#btn-plan-none").addEventListener("click", () => planSelectAll(false));

/* ---------- 单集钩子前置：面板交互（2026-11-06）---------- */
/* 只切面板、不动已有结构：结构是上一次生成的产物，切个 tab 就把它弄丢才是怪事。
   真覆盖要等用户点「生成 N 集钩子」，那一步会二次确认。 */
$("#cut-mode").addEventListener("click", (e) => {
  const b = e.target.closest(".cm-btn");
  if (!b) return;
  HK.mode = b.dataset.mode === "hook" ? "hook" : "mix";
  renderCutMode();
  renderHookPanel();
  renderPlans();
});

$("#hook-sec").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-sec]");
  if (!b) return;
  HK.sec = Number(b.dataset.sec) || 15;
  renderHookPanel();
});

$("#hook-ep-chips").addEventListener("click", (e) => {
  const chip = e.target.closest(".ep-chip");
  if (!chip || chip.disabled) return;
  const i = Number(chip.dataset.ep);
  if (HK.eps.has(i)) HK.eps.delete(i); else HK.eps.add(i);
  renderHookPanel();
});

$("#btn-hook-all").addEventListener("click", () => {
  HK.eps = new Set(hookEpDone().map((e) => e.index));
  renderHookPanel();
});
$("#btn-hook-none").addEventListener("click", () => {
  HK.eps = new Set();
  renderHookPanel();
});
$("#btn-hook-range").addEventListener("click", () => {
  const want = parseEpRange($("#hook-range").value);
  if (!want.size) return toast("写成「3-12」或「1,3,5」这样");
  const ok = new Set(hookEpDone().map((e) => e.index));
  const miss = [...want].filter((i) => !ok.has(i));
  HK.eps = new Set([...want].filter((i) => ok.has(i)));
  renderHookPanel();
  if (miss.length) toast(`第 ${miss.slice(0, 6).join("、")} 集还没整理好，没选上`);
  else toast(`已选 ${HK.eps.size} 集`);
});
$("#hook-model").addEventListener("change", (e) => {
  HK.model = (e.target.value || "").trim();
});

$("#btn-hookcut").addEventListener("click", async () => {
  const p = ST.proj;
  if (!p) return;
  const done = new Set(hookEpDone().map((e) => e.index));
  const eps = [...HK.eps].filter((i) => done.has(i)).sort((a, b) => a - b);
  if (!eps.length) return toast("先选至少一集");
  const old = (p.plans || []).length;
  if (old) {
    const wasHook = (p.plans || [])[0].mode === "hook";
    if (!confirm(`现在已有 ${old} ${wasHook ? "集钩子" : "套混剪"}结构，重新生成会覆盖它们（已经出好的片子还在）。\n\n`
      + `这次：${eps.length} 集 × 钩子 ${HK.sec} 秒 + 整集正片\n`
      + "会用到 1 次今日生成机会，成片在下一步出。\n\n继续？")) return;
  }
  try {
    const grant = await aiGrant(p.id, "hook");
    await api(`/api/projects/${p.id}/hookcut-plan`, {
      method: "POST",
      headers: { "X-AI-Grant": grant },
      body: JSON.stringify({ eps, hook_sec: HK.sec, model: HK.model }),
    });
    ST.proj.task = { kind: "plan", pct: 0, msg: "AI 正在给每集挑钩子", error: "" };
    render();
    startPoll();
  } catch (e) {
    toast(e.message);
  }
});

/* 修改提示词（第 1 步导流 / 第 2 步编排 + 钩子 / 第 3 步分镜 / 设置里的通用要求共用一套逻辑）。
   keys 是后端 _PROMPTS 白名单里的文件名标识；一份配置可以带多条，用顶部标签切换。
   保存后下一步生成立即生效，后端埋点记录改动全文。
   2026-11-08 用户要求「所有提示词都暴露出来、自己改」，所以输出格式也放进来了
   （保存前后端会验 JSON，改坏字段名会被拦下，不会等到跑完一次 API 才发现）。 */
/* 提示词对使用者只读（2026-11-12 用户要求）：界面上不再提供任何「修改提示词」入口。
   置 true 才恢复入口；后端另有一道 HOOK_ALLOW_PROMPT_EDIT 闸，两道都开才能改。 */
const PROMPT_EDIT_ON = false;

/* 收掉一个提示词入口：编辑卡片 + 入口按钮 + 紧挨在按钮上面的「提示词」小标题。
   小标题必须一起收，否则下面空留一行「提示词」什么都没有。 */
function dropPromptEntry(editSel, cardSel) {
  const card = cardSel ? $(cardSel) : null;
  if (card) card.remove();
  const btn = editSel ? $(editSel) : null;
  if (!btn) return;
  const prev = btn.previousElementSibling;
  if (prev && prev.classList.contains("set-title") && /提示词/.test(prev.textContent)) {
    prev.remove();
  }
  btn.remove();
}

function initPromptEditor(cfg) {
  if (!PROMPT_EDIT_ON) { dropPromptEntry(cfg.edit, cfg.card); return; }
  const editBtn = $(cfg.edit);
  if (editBtn) editBtn.hidden = false;   // HTML 里默认 hidden，放开时才显出来
  const card = $(cfg.card), box = $(cfg.box), tag = $(cfg.tag);
  if (!card || !box) return;
  const keys = cfg.keys || [{ key: cfg.key, label: "", note: cfg.note || "", after: cfg.after }];
  const titleEl = cfg.title ? $(cfg.title) : null;
  const noteEl = cfg.note ? $(cfg.note) : null;
  const tabsEl = cfg.tabs ? $(cfg.tabs) : null;
  let cur = keys[0];

  function syncHead() {
    if (titleEl) titleEl.textContent = cur.label || "提示词";
    if (noteEl) noteEl.innerHTML = cur.note || "";
    if (tabsEl) {
      tabsEl.querySelectorAll("[data-key]").forEach((b) => {
        b.classList.toggle("on", b.dataset.key === cur.key);
      });
    }
  }

  async function loadPrompt() {
    tag.textContent = "读取中…";
    try {
      const r = await api(`/api/prompts/${cur.key}`);
      box.value = r.content || "";
      tag.textContent = r.is_custom ? "（当前是自定义版）" : "（当前是内置默认）";
      box.dataset.isCustom = r.is_custom ? "1" : "";
    } catch (e) {
      tag.textContent = "";
      toast("读提示词失败：" + e.message);
    }
  }

  function pick(item) {
    cur = item;
    syncHead();
    loadPrompt();
  }

  if (tabsEl) {
    tabsEl.querySelectorAll("[data-key]").forEach((b) => {
      b.addEventListener("click", () => {
        const item = keys.find((k) => k.key === b.dataset.key);
        if (item && item.key !== cur.key) pick(item);
      });
    });
  }

  $(cfg.edit).addEventListener("click", () => {
    const show = card.classList.toggle("hidden");
    // 每次展开都重读：换过页签、手动改过文件、在别的窗口存过，看到的都是最新的
    if (!show) pick(cur);
  });
  $(cfg.cancel).addEventListener("click", () => card.classList.add("hidden"));

  $(cfg.save).addEventListener("click", async () => {
    const text = box.value.trim();
    if (!text) return toast("提示词不能是空的");
    try {
      const r = await api(`/api/prompts/${cur.key}`, {
        method: "PUT",
        body: JSON.stringify({ text }),
      });
      box.dataset.isCustom = "1";
      tag.textContent = "（当前是自定义版）";
      toast(`提示词已保存（${r.chars} 字）——${cur.after || cfg.after || "下次生成就用这份"}`);
    } catch (e) {
      toast("保存失败：" + e.message);
    }
  });

  $(cfg.reset).addEventListener("click", async () => {
    if (!confirm(`恢复「${cur.label || "这份提示词"}」的内置默认？你改的内容会被删掉。`)) return;
    try {
      await api(`/api/prompts/${cur.key}`, { method: "DELETE" });
      box.dataset.isCustom = "";
      await loadPrompt();
      toast("已恢复内置默认提示词");
    } catch (e) {
      toast("恢复失败：" + e.message);
    }
  });
}

initPromptEditor({
  card: "#prompt-edit", box: "#prompt-edit-text", tag: "#prompt-edit-tag",
  title: "#prompt-edit-title", note: "#prompt-edit-note", tabs: "#prompt-tabs",
  edit: "#btn-prompt-edit", cancel: "#btn-prompt-cancel",
  save: "#btn-prompt-save", reset: "#btn-prompt-reset",
  keys: [
    {
      key: "plans", label: "编排指令", after: "下次点「生成结构」就用这份",
      note: "编排用的指令：剧名、套数、时长等由程序自动填入，全剧台词在生成时附在后面，不显示在这里。占位符：<code>{count}</code> 套数、<code>{blocks}</code> 每套备选块数、<code>{min_sec}/{max_sec}</code> 单套时长、<code>{total_sec}/{n_ep}</code> 素材总量。",
    },
    {
      key: "plans_schema", label: "输出格式", after: "下次点「生成结构」就用这份",
      note: "程序靠 <code>plans</code> / <code>segments</code> / <code>from</code> / <code>to</code> 这些键读数。示例里的「…」「[...]」是省略写法，所以保存前只验两件事：括号有没有配平、关键键名还在不在——这两样坏了会被拦下并说明。字段说明接在示例后面照样能存。",
    },
  ],
});

/* 挑钩子提示词是独立一份（prompts/hooks.txt）：混剪那份的纪律是「必须严格集序、
   不许把后面的戏提到开头」，正好跟钩子前置相反，两份绝不能合并 */
initPromptEditor({
  card: "#hook-prompt-edit", box: "#hook-prompt-text", tag: "#hook-prompt-tag",
  title: "#hook-prompt-title", note: "#hook-prompt-note", tabs: "#hook-prompt-tabs",
  edit: "#btn-hook-prompt", cancel: "#btn-hook-prompt-cancel",
  save: "#btn-hook-prompt-save", reset: "#btn-hook-prompt-reset",
  keys: [
    {
      key: "hooks", label: "挑钩子指令", after: "下次点「生成钩子」就用这份",
      note: "台词会按你勾选的集附在后面。占位符：<code>{count}</code> 集数、<code>{hook_sec}</code> 钩子长度、<code>{min_sec}/{max_sec}</code> 时长容差、<code>{n_ep}</code> 总集数。",
    },
    {
      key: "hooks_schema", label: "输出格式", after: "下次点「生成钩子」就用这份",
      note: "程序靠 <code>hooks</code> 里每条的 <code>ep</code> / <code>from</code> / <code>to</code> / <code>hook</code> 读数。保存前验 JSON，改坏会被拦下并指出行号。",
    },
  ],
});

initPromptEditor({
  card: "#sprompt-edit", box: "#sprompt-edit-text", tag: "#sprompt-edit-tag",
  title: "#sprompt-edit-title", note: "#sprompt-edit-note", tabs: "#sprompt-tabs",
  edit: "#btn-sprompt-edit", cancel: "#btn-sprompt-cancel",
  save: "#btn-sprompt-save", reset: "#btn-sprompt-reset",
  keys: [
    {
      key: "shots", label: "分镜通则", after: "下次点「生成引流分镜」就用这份",
      note: "发给模型的系统提示词，只写全局不变的原则。条数、时长、镜数、字数由程序算好填进「分镜要求」，不用在这里写数字。",
    },
    {
      key: "shots_user", label: "分镜要求", after: "下次点「生成引流分镜」就用这份",
      note: "花括号 <code>{...}</code> 里的字由程序按素材填（时长、镜数、字数、人物卡、台词原文等），<b>别删占位符</b>。<code>## 段名</code> 是分段标记，改了名字那一段就不发了；把某段内容清空等于主动不要这条要求。",
    },
    {
      key: "shots_schema", label: "输出格式", after: "下次点「生成引流分镜」就用这份",
      note: "JSON 骨架。<code>__DUR__</code> 会被换成真实时长（15/30/60）。保存前验 JSON：字段名拼错、少括号会被拦下并指出行号，不会让整批生成白跑。",
    },
    {
      key: "shots_rules", label: "固定短句", after: "下次生成文档就用这份",
      note: "写进成片文档的三句短句：<code>## 控制句</code>（对话式生成器用，界面有开关）、<code>## 每镜要求</code>（画面不出现文字）、<code>## 合规声明</code>。整段删掉 = 用内置那句；段留着但内容清空 = 不要这句。",
    },
  ],
});

/* 第 1 步单片导流：2026-11-07 已下线，只有历史项目还会走到，所以入口放在老步骤里 */
initPromptEditor({
  card: "#fp-edit", box: "#fp-text", tag: "#fp-tag",
  title: "#fp-title", note: "#fp-note",
  edit: "#btn-fp-edit", cancel: "#btn-fp-cancel",
  save: "#btn-fp-save", reset: "#btn-fp-reset",
  keys: [{
    key: "funnel", label: "导流指令", after: "下次点「生成剪辑表」就用这份",
    note: "占位符：<code>{name}</code> 剧名、<code>{n_ep}</code> 集数、<code>{src_min}</code> 素材分钟、<code>{target}</code> 目标秒数、<code>{per_ep}</code> 每集参考均值。台词由程序附在末尾。",
  }],
});

/* 通用输出要求：第 1 步导流 / 第 2 步混剪 / 挑钩子共用那句「只输出 JSON」。
   放高级设置里——普通用户不需要碰，改坏了整批解析不出来。 */
initPromptEditor({
  card: "#sysprompt-edit", box: "#sysprompt-text", tag: "#sysprompt-tag",
  title: "#sysprompt-title", note: "#sysprompt-note",
  edit: "#btn-sysprompt-edit", cancel: "#btn-sysprompt-cancel",
  save: "#btn-sysprompt-save", reset: "#btn-sysprompt-reset",
  keys: [{
    key: "system", label: "通用输出要求", after: "下次排结构就用这份",
    note: "必须保留「只输出 JSON」这个意思：程序按 JSON 解析结果，改成自然语言回复会整批失败。",
  }],
});

/* 第 2 步出片：只渲染勾选的套（成片存到用户指定的文件夹） */
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
    : `留空会存到默认位置下的「${nm}」文件夹`;
}
$("#save-dir").addEventListener("input", updateSaveDirNote);

/* ---- 出片与画面（2026-11-05）：全局默认 + 每套单独覆盖 --------------------
   结构：#fx-pic-panel 是全局默认面板（fx- 前缀），#modal-planfx 是单套弹窗
   （sx- 前缀）——两处共用同一套控件模板，读写都走 readFx / writeFx。
   单套弹窗保存时只存「和默认不一样」的字段：默认改了，没单独设过的套跟着变；
   全部和默认一样 = 跟随默认，不会把用户「点开看了一眼」当成自定义。 */
const GRADE_CN = { cold: "冷调", warm: "暖调", vivid: "浓郁", film: "胶片" };

/* 每档到底改了什么：照 render.GRADES 的系数写，不是套话。
   强度 t 线性缩放预设幅度（R/B 中点位移 ±0.10、饱和 ±0.40 等 = 100% 的满档值）。 */
const GRADE_TIP = {
  cold: "蓝青压进暗部、饱和略降、对比微提——都市夜景、办公室、悬疑冷场。",
  warm: "橙黄提亮中间调——回忆、情感、傍晚暖光；白衣服会微微发黄。",
  vivid: "饱和和对比一起拉高——打脸、爽剧、动作戏更有冲击；红色最先溢出。",
  film: "降饱和、提对比、自带细颗粒——电影感、年代感；画面会变柔。",
};

function fxIntLevel(t) {
  if (t <= 0.03) return "0 档";
  if (t < 0.3) return "轻";
  if (t < 0.6) return "中";
  if (t < 0.85) return "明显";
  return "强";
}

/* 强度说明：既要说清「65% 是什么」，也要说清「现在算高还是低、会有什么影响」。
   grade="rand"（随机档）时强度是每区浮动的基准。 */
function fxGradeTip(grade, intv) {
  const pct = Math.round(intv * 100);
  if (grade === "rand") {
    let tail;
    if (pct <= 0) tail = "0% 等于没调，随机档也救不回来。";
    else if (pct < 30) tail = "很轻，只在暗部、灰面上看得出来，适合先试色。";
    else if (pct < 60) tail = "中等：氛围有了，肤色基本没动。";
    else if (pct < 85) tail = "偏明显，是「看得出氛围但不假」的常用区间。";
    else tail = "已到极限附近：随机档每区还会再 ±18%，个别区会顶到满档，肤色容易失真，出片后先看人脸。";
    return `强度 ${pct}%（${fxIntLevel(intv)}）＝随机档的基准：每一区在它上下 ±18% 浮动。选了「随机」就不用挑档位，程序每区自己换（相邻区不重样），四档都会轮到。${tail}`;
  }
  if (!grade) return "没选档位＝原片直出。想先看看效果，选「随机」最省事：每区自己换档，不用挑。";
  const eff = GRADE_TIP[grade] || "";
  let tail;
  if (pct <= 0) tail = "0% 等于没调，选了档也不生效。";
  else if (pct < 30) tail = "很轻，只在暗部、灰面上看得出来，适合先试色。";
  else if (pct < 60) tail = "中等：氛围有了，肤色基本没动。";
  else if (pct < 85) tail = "偏明显，是「看得出氛围但不假」的常用区间。";
  else tail = "已到这一档的极限附近：肤色最先失真（冷调发青 / 暖调发橙 / 浓郁发红），暗部也容易糊，出片后先看人脸。";
  return `强度 ${pct}%（${fxIntLevel(intv)}）＝把${GRADE_CN[grade] || grade}的预设幅度按 ${pct}% 施加（100% 是这一档的最大幅度，0% 等于原片）。${eff}${tail}`;
}

function fxPanelHTML(p) {
  return `
  <div class="fx-group fx-g1">
    <div class="fx-group-h"><i class="fx-gno">1</i>出片增强<span class="hint">决定成片怎么包装：标题、片头卡、去重、指纹、配乐。不改剧情顺序和时长</span></div>
    <div class="fx-checks">
      <label class="chk" title="每块画面顶部烧一行「看点：XXX」，写这一段在演什么（取自 AI 给这块的标签）。位置在顶部，跟素材下方的台词字幕不打架；想连素材自带的字幕一起去掉，先做第 2 步备好已处理的视频，再在第 ⑤ 组选它。嫌抢戏可以关，但观众就不知道下一块要演什么。"><input type="checkbox" id="${p}title"> 看点字幕</label>
      <label class="chk" title="整幅放大 3% 再居中裁回原尺寸：画面指纹跟原片不一样，边缘一圈（含黑边、水印带）被切掉，平台比对原片时对不上——混剪去重最省事的一招。103% 是稳妥值，再大会切到下方的台词字幕；构图很满、切到人脸时也可以关。想彻底去掉字幕，先做第 2 步备好已处理的视频，再在第 ⑤ 组选它。"><input type="checkbox" id="${p}zoom"> 画面放大 103%（去重）</label>
      <label class="chk" title="这套片子最前面加 1.8 秒纯黑底 + 一句大字（文案来自每套的「开场钩子」，可展开改）。观众判断要不要看只看开头一两秒，先用一句话把矛盾摆出来；关掉就直接从第一段开始。"><input type="checkbox" id="${p}hook"> 开场钩子卡 1.8s</label>
      <label class="chk" title="评分 5-6 的缓冲段（反应、沉默、过场）放快 1.15 倍，音调不变；爆点段保持原速。关掉就全片原速。"><input type="checkbox" id="${p}speed"> 缓冲段加速 1.15×</label>
      <label class="chk" title="出片时自动挑一首合适的音乐循环铺满全片：音量很低、台词一响再自动压低（闪避），不盖对白；结尾 2.5 秒淡出。想用自己电脑里的歌，展开后选「用我自己的音乐」。"><input type="checkbox" id="${p}bgm"> 配乐 BGM</label>
    </div>
    <div class="fx-line">
      <label class="fx-f" title="每次点出片，这一套会重新抽一组画面参数：放大多少、取景框往哪偏、轻微转多少度、噪点/暗角/光晕多强。同一批里每套都不一样；隔几天再点一次出片，同一套也会变成另一版。只动画面，不改时长、不改台词、不改剧情顺序。不想让画面有任何变化就选「保守」。"><select id="${p}fp">
          <option value="std">标准 · 每套都不一样（推荐）</option>
          <option value="soft">保守 · 几乎看不出来</option>
          <option value="strong">强力 · 变化更明显</option>
        </select> 画面指纹强度</label>
    </div>
    <p class="fx-tip" id="${p}fptip"></p>
    <div class="fx-bgm hidden" id="${p}bgmbox">
      <div class="fx-bgm-modes">
        <label class="chk" title="出片时程序按每套的配乐建议，从曲库里挑一首合适的音乐自动铺上，不用自己找歌。"><input type="radio" name="${p}bgmmode" id="${p}bgmauto" value="auto" checked> 自动配乐（推荐）</label>
        <label class="chk" title="用你自己电脑上、已经拿到商用授权的音乐。"><input type="radio" name="${p}bgmmode" id="${p}bgmmanual" value="manual"> 用我自己的音乐</label>
      </div>
      <p class="fx-tip" id="${p}bgmautotip">勾上「配乐 BGM」，出片时程序会按每套的配乐建议自动挑一首合适的音乐铺满全片，<b>不用自己找歌</b>。<span data-bgmlib>正在查看曲库…</span></p>
      <div class="fx-bgm-manual hidden" id="${p}bgmmanualbox">
        <label class="fx-f">音乐文件（mp3 / m4a / wav / flac）
          <span class="row"><input id="${p}bgmpath" type="text" spellcheck="false" placeholder="选一首自己电脑上的歌，点右边按钮更快"><button class="ghost" data-bgmpick="${p}">选择…</button></span>
        </label>
      </div>
      <label class="fx-slider">基础音量 <b id="${p}bvolv">22%</b>
        <input type="range" id="${p}bvol" min="5" max="60" step="1" value="22">
        <span class="fx-scale"><i>5 若有若无</i><i>22 推荐</i><i>60 偏抢</i></span>
      </label>
      <p class="fx-tip">音量是垫在台词底下的基础音量：台词一响音乐会自动压低，所以不用怕调大。22% 是「听得见、不抢戏」的推荐值；觉得没存在感往 30-40% 加，盖台词就往回减。</p>
    </div>
  </div>

  <div class="fx-group fx-g2">
    <div class="fx-group-h"><i class="fx-gno">2</i>调色<span class="hint">只改颜色，不改时长、不降清晰度；默认「随机」——每区自己换档，去重最稳</span></div>
    <div class="fx-line">
      <label class="fx-f">档位（随机并在这里选，不用再单独开开关）
        <select id="${p}grade">
          <option value="rand">随机 · 每区换一档（推荐）</option>
          <option value="">原片（不调色）</option>
          <option value="cold">冷调 · 蓝青，都市疏离</option>
          <option value="warm">暖调 · 橙黄，回忆 / 情感</option>
          <option value="vivid">浓郁 · 高饱和高对比，爽剧</option>
          <option value="film">胶片 · 低饱和柔对比 + 细颗粒</option>
        </select>
      </label>
      <label class="fx-slider">强度 <b id="${p}gintv">65%</b>
        <input type="range" id="${p}gint" min="0" max="100" step="5" value="65">
        <span class="fx-scale"><i>0 原片</i><i>35 轻</i><i>65 明显</i><i>100 满档</i></span>
      </label>
    </div>
    <p class="fx-tip" id="${p}gtip"></p>
    <div class="fx-line hidden" id="${p}randbox">
      <label class="fx-mini">每区最短 <input type="number" id="${p}randmin" min="0.5" max="15" step="0.5" value="2"> 分钟</label>
      <label class="fx-mini">每区最长 <input type="number" id="${p}randmax" min="0.5" max="15" step="0.5" value="5"> 分钟</label>
    </div>
    <p class="fx-tip hidden" id="${p}rtip">分区长度本身也是随机的：整片在「最短–最长」之间随机切段，每段从四档里抽一档（相邻段不重样），强度在基准上下 ±18% 浮动。同一套重渲颜色不变；想换一套配色，把结构重新生成一版（代次变了）。</p>
    <label class="fx-f">LUT 文件（.cube，选了就替代上面的档位；剪映 / 达芬奇导出的调色包）
      <span class="row"><input id="${p}lut" type="text" spellcheck="false" placeholder="例如 D:/调色/电影感.cube"><button class="ghost" data-lutpick="${p}">选择…</button></span>
    </label>
  </div>

  <div class="fx-group fx-g3">
    <div class="fx-group-h"><i class="fx-gno">3</i>画面变形<span class="hint">只改人脸宽窄，不改时长；默认已轻开，拖回 100% 就是原样</span></div>
    <div class="fx-line">
      <label class="fx-slider">横向变形 <b id="${p}stv">101.5%</b>
        <input type="range" id="${p}stretch" min="94" max="106" step="0.5" value="101.5">
        <span class="fx-scale"><i>94 显瘦</i><i>100 原样</i><i>106 显宽</i></span>
      </label>
    </div>
    <p class="fx-tip">94-99% 中间收窄、人脸略瘦，100% 原样，101-106% 中间放宽、脸略宽；超过 6% 背景会明显变形。<b>出厂 101.5%（宽 1.5%）是校出来的无感档</b>——把画面横向推开一点点，人脸宽度看不出变化。左右镜像放在第 ⑤ 组，因为它跟「用哪份视频」是同一件事：翻不翻，取决于字幕在哪一步画上去。</p>
  </div>

  <div class="fx-group fx-g4">
    <div class="fx-group-h"><i class="fx-gno">4</i>氛围特效<span class="hint">在原画面上叠一层，不改时长；默认已轻开，拖到 0 = 关</span></div>
    <div class="fx-lines3">
      <label class="fx-slider">光晕 <b id="${p}glowv">5%</b>
        <input type="range" id="${p}glow" min="0" max="100" step="5" value="5">
        <span class="fx-scale"><i>关</i><i>柔和</i><i>发雾</i></span>
      </label>
      <label class="fx-slider">颗粒 <b id="${p}grainv">10%</b>
        <input type="range" id="${p}grain" min="0" max="100" step="5" value="10">
        <span class="fx-scale"><i>关</i><i>细微</i><i>发脏</i></span>
      </label>
      <label class="fx-slider">暗角 <b id="${p}vigv">8%</b>
        <input type="range" id="${p}vig" min="0" max="100" step="1" value="8">
        <span class="fx-scale"><i>关</i><i>镜头感</i><i>发黑</i></span>
      </label>
    </div>
    <p class="fx-tip">三项出厂默认都是<b>开在最轻一档</b>，正常看片看不出差别，只是画面更有质感：<br>光晕 5%：亮部（皮肤高光、灯、窗）周围溢出一层很淡的柔光，细节不糊；30% 才开始明显柔焦。<br>颗粒 10%：极细的胶片噪点，等于给画面加了一层纹理；40% 以上暗部发脏。<br>暗角 8%：四角轻微失光，接近镜头自然暗角；20% 以上才看得出是「压暗」。</p>
  </div>

  <div class="fx-group fx-g5 fx-src">
    <div class="fx-group-h"><i class="fx-gno">5</i>用哪份视频 / 要不要镜像<span class="hint">单独一组：从哪份素材切段、要不要整幅翻转，和上面四组互不影响</span></div>
    <label class="fx-f">素材版本
      <select id="${p}srcver">
        <option value="">原始视频 · 保留原来的字幕</option>
        <option value="nosub">已处理 · 原字幕换成自己的台词</option>
        <option value="live">原片 · 出片时现场做（可镜像）</option>
      </select>
    </label>
    <div class="fx-checks">
      <label class="chk" id="${p}mirrorbox" title="整幅画面左右翻转，等于换了一个画面指纹，去重最彻底。能不能开取决于上面选的素材：已处理那份的字幕已烧进画面，翻了就是反字，所以那一档锁住。"><input type="checkbox" id="${p}mirror"> 左右镜像（整幅翻转，去重最彻底）</label>
    </div>
    <p class="fx-tip" id="${p}mirrortip"></p>
    <p class="fx-tip" id="${p}srcvertip"></p>
    <details class="fold"><summary>三份素材分别是什么</summary>
      <div class="fold-body">
        <p><b>原始视频</b>：导入的那份，字幕还是原来的。</p>
        <p><b>已处理</b>：第 2 步另外做的一份视频——原字幕抹掉、按台词换成你自己的字幕，原始视频原封不动留着。选了它但这一集还没备好，出片时会先拦下来告诉你缺哪几集，不会悄悄用回带字幕的原片。字幕已经烧在那份画面里，所以这一套不能开镜像（会翻反，出片时拦）。</p>
        <p><b>出片时现场做</b>：不占第 2 步那份副本，直接从原片切，出片这一趟顺手把原字幕抹掉、再按台词烧回自己的字幕。顺序是「抹字幕 → 镜像/调色 → 烧字幕」，字是翻转之后才画上去的，所以<b>可以同时开镜像</b>。代价是每段多跑一道抹除、第一次要先扫一遍字幕区域（会多等一会儿，之后走缓存）。</p>
      </div>
    </details>
  </div>

  <div class="fx-group fx-g6 fx-cap">
    <div class="fx-group-h"><i class="fx-gno">6</i>开头画面镜像<span class="hint">独立一组：只动成片最前面那一下，跟第 ⑤ 组的整幅翻转互不影响</span></div>
    <div class="fx-checks">
      <label class="chk" title="成片最前面大约 0.3 秒把画面翻一下，之后立刻恢复原样。默认不勾。勾上后在下面选往哪边翻。"><input type="checkbox" id="${p}fmirror"> 开头 0.3 秒镜像（默认不勾）</label>
    </div>
    <div class="fx-line hidden" id="${p}fmirrorline">
      <label class="fx-f">往哪边翻
        <select id="${p}fmirrordir">
          <option value="h">左右翻（推荐）</option>
          <option value="v">上下翻</option>
        </select>
      </label>
    </div>
    <p class="fx-tip hidden" id="${p}fmirrortip"></p>
    <details class="fold"><summary>为什么只翻开头</summary>
      <div class="fold-body">
        <p>第 ⑤ 组的「左右镜像」翻的是<b>整条片子</b>，去重最彻底，但素材自带的字幕会全程翻反。这里只翻最前面那一下：开头画面跟原片对不上，正片一点没动，也不用先做第 2 步。</p>
        <p>它跟选哪份素材无关，三份素材都能开；选「已处理」时锁住的只是第 ⑤ 组那个整幅镜像，不锁这一项。</p>
        <p>开头要是带字幕，那几个字会跟着翻一下；有开场钩子卡时，翻的是卡片后面第一段正片的开头，黑底大字那张卡不动。</p>
        <p>两处镜像同时开也能出片：开头那 0.3 秒会被再翻一次，看起来和整幅翻过去的方向相反，之后回到整幅镜像的样子。</p>
      </div>
    </details>
  </div>`;
}

function readFx(p) {
  const el = (id) => document.getElementById(p + id);
  const on = (id) => !!(el(id) && el(id).checked);
  const val = (id) => (el(id) ? el(id).value : "");
  const num = (id) => Number(val(id)) || 0;
  // 用哪份视频：独立一组，只此一个字段，不跟上面的画面参数混
  const srcVer = (() => {
    const v = val("srcver");
    return v === "nosub" || v === "live" ? v : "";
  })();
  return {
    title: on("title"),
    zoom: on("zoom"),
    hook_card: on("hook"),
    buffer_speed: on("speed"),
    bgm: on("bgm"),
    bgm_auto: !(el("bgmmanual") && el("bgmmanual").checked),
    bgm_path: (val("bgmpath") || "").trim(),
    bgm_vol: Math.min(0.6, Math.max(0.05, (num("bvol") || 22) / 100)),
    grade: val("grade"),
    grade_int: num("gint") / 100,
    rand_min: Math.min(15, Math.max(0.5, num("randmin") || 2)) * 60,
    rand_max: Math.min(15, Math.max(0.5, num("randmax") || 5)) * 60,
    lut: (val("lut") || "").trim(),
    // 镜像跟素材绑定：已处理那份的字幕已经烧进画面，翻了就是反字，所以这档恒为 false
    mirror: srcVer === "nosub" ? false : on("mirror"),
    // 开头镜像是独立一组，跟用哪份素材无关；方向下拉只在勾上时才可见
    first_mirror: on("fmirror"),
    first_mirror_dir: val("fmirrordir") === "v" ? "v" : "h",
    stretch: Math.min(1.06, Math.max(0.94, (num("stretch") || 101.5) / 100)),
    glow: num("glow") / 100,
    grain: num("grain") / 100,
    vignette: num("vig") / 100,
    // 指纹强度：每套按这个档位抽自己的画面参数（放大/取景偏移/微旋转/质感浮动）
    fp_level: val("fp") || "std",
    src_ver: srcVer,
  };
}

function writeFx(p, fx) {
  fx = fx || {};
  const el = (id) => document.getElementById(p + id);
  const put = (id, v) => { const e = el(id); if (e) e.value = v; };
  const chk = (id, v) => { const e = el(id); if (e) e.checked = !!v; };
  // 出片增强：看点字幕 / 钩子卡默认关（写了 true 才开），放大 / 变速默认开
  chk("title", !!fx.title);
  chk("zoom", fx.zoom !== false);
  chk("hook", !!fx.hook_card);
  chk("speed", fx.buffer_speed !== false);
  chk("bgm", !!fx.bgm);
  // 老项目只存了 bgm + bgm_path、没有 bgm_auto：当手动看待，别把用户选过的歌换成自动
  const manual = fx.bgm_auto === false || (fx.bgm_auto == null && !!fx.bgm_path);
  if (el("bgmmanual")) el("bgmmanual").checked = manual;
  if (el("bgmauto")) el("bgmauto").checked = !manual;
  put("bgmpath", fx.bgm_path || "");
  put("bvol", Math.round((fx.bgm_vol == null ? 0.22 : fx.bgm_vol) * 100));
  // 调色档：默认「随机」；老数据里 grade_rand=true 也合并成随机档
  let grade = fx.grade;
  if (grade == null || grade === undefined) grade = "rand";
  if (fx.grade_rand && !grade) grade = "rand";
  put("grade", grade || "");
  // 分区时长：新数据是 min/max（随机区间），老数据只有 rand_sec（固定，两端相等）
  let rmin = fx.rand_min, rmax = fx.rand_max;
  if (rmin == null && rmax == null && fx.rand_sec != null) rmin = rmax = fx.rand_sec;
  put("randmin", Math.round((rmin == null ? 120 : rmin) / 60 * 2) / 2);
  put("randmax", Math.round((rmax == null ? 300 : rmax) / 60 * 2) / 2);
  put("gint", Math.round((fx.grade_int == null ? 0.65 : fx.grade_int) * 100));
  put("lut", fx.lut || "");
  chk("mirror", !!fx.mirror);
  // 开头镜像：老项目没有这两个字段 → 不勾（默认），方向回到「左右翻」
  chk("fmirror", !!fx.first_mirror);
  put("fmirrordir", fx.first_mirror_dir === "v" ? "v" : "h");
  // 质感四项：老项目没有这些字段 → 显示出厂默认（开在无感档），
  // 与渲染端 FX_DEFAULTS 必须一致，否则面板显示和成片对不上
  const FDEF = { stretch: 1.015, glow: 0.05, grain: 0.10, vignette: 0.08 };
  put("stretch", Math.round((fx.stretch == null ? FDEF.stretch : fx.stretch) * 1000) / 10);
  put("glow", Math.round((fx.glow == null ? FDEF.glow : fx.glow) * 100));
  put("grain", Math.round((fx.grain == null ? FDEF.grain : fx.grain) * 100));
  put("vig", Math.round((fx.vignette == null ? FDEF.vignette : fx.vignette) * 100));
  // 用哪份视频：老项目没有这个字段 → 原始视频，不悄悄改掉已出好的片子
  put("srcver", fx.src_ver === "nosub" || fx.src_ver === "live" ? fx.src_ver : "");
  // 指纹强度：老项目没有这个字段 → 标准档（新抽的画面参数，观感和老片一致）
  put("fp", ["soft", "std", "strong"].includes(fx.fp_level) ? fx.fp_level : "std");
  fxSync(p);
}

/* 标签、说明、子区显隐：一个函数管齐，改任何控件都调它 */
function fxSync(p) {
  const el = (id) => document.getElementById(p + id);
  const g = el("gint"), gv = el("gintv");
  if (g && gv) gv.textContent = g.value + "%";
  const s = el("stretch"), sv = el("stv");
  if (s && sv) sv.textContent = s.value + "%";
  for (const id of ["glow", "grain", "vig"]) {
    const r = el(id), lab = el(id + "v");
    if (r && lab) lab.textContent = Number(r.value) ? r.value + "%" : "关";
  }
  const bv = el("bvol"), bvv = el("bvolv");
  if (bv && bvv) bvv.textContent = bv.value + "%";
  // 指纹强度：说明写在主视区（"点了会发生什么"），原理不进面板
  const fp = el("fp"), fptip = el("fptip");
  if (fptip) {
    const zv = el("zoom");
    if (zv && !zv.checked) {
      fptip.textContent = "「画面放大」没勾，宽度/取景/旋转都不会动，指纹随机只剩噪点、暗角、光晕的轻微浮动。想让每套画面结构都不一样，把上面的「画面放大」勾上。";
    } else if (fp && fp.value === "soft") {
      fptip.textContent = "变化很小：同一批里能看出细微差别，几乎不影响观感。想让每套差别明显一点就选「标准」。";
    } else if (fp && fp.value === "strong") {
      fptip.textContent = "变化最明显：放大、取景偏移、轻微旋转都更大，每套一眼能看出构图不同。构图很满、边缘有字的片子可能切到一点，介意就退回「标准」。";
    } else {
      fptip.textContent = "每次点出片，这一套都会重新抽一组画面参数：放大多少、取景框往哪偏、轻微转多少度、噪点暗角光晕多强。同一批里每套都不一样，隔几天再点一次出片也是另一版。只动画面，不改时长、不改台词。";
    }
  }
  const grade = el("grade") ? el("grade").value : "";
  const tip = el("gtip");
  if (tip) tip.textContent = fxGradeTip(grade, g ? Number(g.value) / 100 : 0);
  const isRand = grade === "rand";
  const box = el("randbox"), rtip = el("rtip");
  if (box) box.classList.toggle("hidden", !isRand);
  if (rtip) rtip.classList.toggle("hidden", !isRand);
  const bgmBox = el("bgmbox");
  if (bgmBox) bgmBox.classList.toggle("hidden", !(el("bgm") && el("bgm").checked));
  const bgmManual = !!(el("bgmmanual") && el("bgmmanual").checked);
  const manualBox = el("bgmmanualbox");
  if (manualBox) manualBox.classList.toggle("hidden", !bgmManual);
  const autoTip = el("bgmautotip");
  if (autoTip) autoTip.classList.toggle("hidden", bgmManual);
  // 第 ⑤ 组：素材 + 镜像绑在一起（镜像能不能开，取决于字幕在哪一步画上去）
  const pickv = el("srcver") ? String(el("srcver").value) : "";
  const mv = el("mirror"), mbox = el("mirrorbox"), mtip = el("mirrortip");
  if (mv && mbox) {
    const blocked = pickv === "nosub";
    mv.disabled = blocked;
    mbox.classList.toggle("disabled", blocked);
    if (mtip) {
      mtip.innerHTML = blocked
        ? "这一档<b>锁住镜像</b>：这份素材里的字幕已经烧进画面，整幅翻转会把字翻反。想镜像就改选「原始视频」或「出片时现场做」。"
        : pickv === "live"
          ? "可以放心开：顺序是<b>抹字幕 → 镜像 → 烧字幕</b>，字是翻转之后才画上去的，翻了还是正的。"
          : "镜像是整幅翻转，去重最彻底；代价是原片自带的字幕会跟着翻反。想翻又不想字幕翻反，把上面改成「出片时现场做」。";
    }
  }
  // 第 ⑥ 组：开头镜像独立成组，勾上才展开方向选择和说明；不勾就是原样出片
  const fm = el("fmirror"), fmline = el("fmirrorline"),
        fmdir = el("fmirrordir"), fmtip = el("fmirrortip");
  if (fm) {
    const fmOn = !!fm.checked;
    if (fmline) fmline.classList.toggle("hidden", !fmOn);
    if (fmdir) fmdir.disabled = !fmOn;
    if (fmtip) {
      fmtip.classList.toggle("hidden", !fmOn);
      fmtip.innerHTML = fmdir && fmdir.value === "v"
        ? "开头那一下画面<b>上下颠倒</b>，之后立刻恢复原样。画面里带字幕的话，开头几个字会跟着翻。"
        : "开头那一下画面<b>左右对调</b>，之后立刻恢复原样。画面里带字幕的话，开头几个字会跟着翻。";
    }
  }
  // 用哪份视频：只改一行说明，不需要展开子区
  const vtip = el("srcvertip");
  if (vtip) {
    const picking = pickv === "nosub";
    // 第 2 步已经备好了、这一套却还在用原片时，提醒一句（默认值不动，免得把老项目悄悄改掉）
    const proj = ST.proj || {};
    const nsf = Object.keys(((proj.nosub || {}).files) || {}).length;
    const epsN = (proj.episodes || []).length;
    const nsReady = epsN > 0 && nsf >= epsN;
    vtip.textContent = pickv === "live"
      ? "不用先做第 2 步：从原片切段，出片这一趟顺手抹掉原字幕、再按台词烧回自己的字幕。"
        + "因为字幕是在左右镜像之后才画上去的，这一套可以同时开镜像。"
        + (nsReady ? "（已经有了第 2 步的成品，这条路只是省掉那份副本。）" : "")
      : picking
        ? "从第 2 步「重做字幕」做好的视频里切段（原字幕已抹掉，换成你整理好的台词）。每一套都能单独改回原始视频。"
        : nsReady
        ? "从导入的原始视频里切段，画面还是原样（带着原来的字幕）。第 2 步已经备好了处理好的视频，要换成它就在上面改选。"
        : "从导入的原始视频里切段，画面保持原样。";
  }
}

function fxWire(p) {
  // 勾选框：一变就同步（随机子区、BGM 子区、强度说明都在这）
  ["title", "zoom", "hook", "speed", "bgm", "bgmauto", "bgmmanual", "mirror", "grade", "srcver",
   "fmirror", "fmirrordir", "fp"].forEach((id) => {
    const e = document.getElementById(p + id);
    e && e.addEventListener("change", () => fxSync(p));
  });
  ["bvol", "gint", "stretch", "glow", "grain", "vig", "randmin", "randmax", "bgmpath", "lut"]
    .forEach((id) => {
      const e = document.getElementById(p + id);
      e && e.addEventListener("input", () => fxSync(p));
      e && e.addEventListener("change", () => fxSync(p));
    });
  const lutBtn = document.querySelector(`[data-lutpick="${p}"]`);
  if (lutBtn) lutBtn.addEventListener("click", async () => {
    toast("已弹出选择框，选一个 .cube 调色文件…");
    try {
      const r = await api("/api/pick-file", { method: "POST" });
      if (r && r.path) {
        const e = document.getElementById(p + "lut");
        if (e) e.value = r.path;
        fxSync(p);
        toast("已选 LUT：" + r.path);
      }
    } catch (e) {
      toast(`${e.message}（也可以直接把路径粘进输入框）`);
    }
  });
  const bgmBtn = document.querySelector(`[data-bgmpick="${p}"]`);
  if (bgmBtn) bgmBtn.addEventListener("click", async () => {
    toast("已弹出选择框，选一首自己电脑上的音乐…");
    try {
      const r = await api("/api/pick-file", { method: "POST" });
      if (r && r.path) {
        const e = document.getElementById(p + "bgmpath");
        if (e) e.value = r.path;
        fxSync(p);
        toast("已选 BGM：出片时循环铺满全片、台词响时自动压低");
      }
    } catch (e) {
      toast(`${e.message}（也可以直接把文件路径粘进输入框）`);
    }
  });
  // 延后一拍：等登录票据就绪再问曲库状态；问不到也只是少一句提示，不影响出片
  setTimeout(loadBgmLibInfo, 300);
}

/* 曲库状态：只报数量，不把本机路径搬进界面 */
let _bgmLibLoaded = false;
function loadBgmLibInfo() {
  if (_bgmLibLoaded) return;
  _bgmLibLoaded = true;
  api("/api/bgm/library").then((d) => {
    const txt = d && d.ready
      ? `曲库里已有 ${d.ready} 首可用音乐。`
      : "曲库里还没有音乐：先放进几首可商用的曲子，或改选「用我自己的音乐」。";
    document.querySelectorAll("[data-bgmlib]").forEach((e) => { e.textContent = txt; });
  }).catch(() => {
    document.querySelectorAll("[data-bgmlib]").forEach((e) => { e.textContent = ""; });
  });
}

/* 徽标上的一句话摘要：只列「和出厂默认不一样」的东西。
   fx 传的是单套覆盖（可能只有一两个字段），全空就是「跟随默认」。 */
function fxShort(fx) {
  fx = fx || {};
  const parts = [];
  if (fx.lut) parts.push("LUT");
  else if (fx.grade === "rand" || fx.grade_rand) {
    const lo = Math.round((fx.rand_min == null ? (fx.rand_sec == null ? 120 : fx.rand_sec) : fx.rand_min) / 60 * 2) / 2;
    const hi = Math.round((fx.rand_max == null ? (fx.rand_sec == null ? 300 : fx.rand_sec) : fx.rand_max) / 60 * 2) / 2;
    parts.push(lo >= hi ? `随机${lo}分` : `随机${lo}–${hi}分`);
  } else if (fx.grade) parts.push(`${GRADE_CN[fx.grade] || fx.grade} ${Math.round((fx.grade_int == null ? 0.65 : fx.grade_int) * 100)}%`);
  // 镜像跟素材绑定：已处理那一档的字幕烧在画面里，镜像无效（后端也会拦），不写进摘要
  if (fx.mirror && fx.src_ver !== "nosub") parts.push("镜像");
  // 开头镜像默认关，勾了才进徽标（和「用哪份素材」无关，单独一列）
  if (fx.first_mirror) parts.push(fx.first_mirror_dir === "v" ? "开头上下翻" : "开头左右翻");
  // 质感四项：出厂默认就开着（无感档），只有**偏离默认**才写进徽标，
  // 否则每套都会挂一串「光晕5·颗粒10·暗角8·变形101.5」纯噪音。
  const FDEF = { stretch: 1.015, glow: 0.05, grain: 0.10, vignette: 0.08 };
  const st = fx.stretch == null ? FDEF.stretch : fx.stretch;
  if (Math.abs(st - FDEF.stretch) > 0.002) {
    parts.push(Math.abs(st - 1.0) < 0.002 ? "变形关" : "变形" + (st * 100).toFixed(1) + "%");
  }
  for (const [k, t] of [["glow", "光晕"], ["grain", "颗粒"], ["vignette", "暗角"]]) {
    const v = fx[k] == null ? FDEF[k] : fx[k];
    if (Math.abs(v - FDEF[k]) > 0.004) parts.push(v < 0.004 ? t + "关" : t + Math.round(v * 100));
  }
  // 出片增强的偏离：只在明确被改过时才写（true 是默认，不用刷屏）
  if (fx.title === false) parts.push("无看点字幕");
  if (fx.zoom === false) parts.push("不放大");
  // 指纹强度默认「标准」，只有改过才写进徽标
  if (fx.fp_level === "strong") parts.push("指纹强力");
  if (fx.fp_level === "soft") parts.push("指纹保守");
  if (fx.hook_card === false) parts.push("无钩子卡");
  if (fx.buffer_speed === false) parts.push("缓冲原速");
  if (fx.bgm) parts.push(fx.bgm_auto === false ? "配乐·自选" : "自动配乐");
  // 用哪份视频是独立一项，排在最前面；默认「原始视频」不刷屏
  if (fx.src_ver === "nosub") parts.unshift("已处理素材");
  if (fx.src_ver === "live") parts.unshift("现场做字幕");
  return parts.join("·") || "默认";
}

/* 全局默认面板：注入 + 折叠 + 改动自动存进项目（下次打开还在） */
$("#fx-pic-panel").innerHTML = fxPanelHTML("fx-");
fxWire("fx-");
/* 单套弹窗的面板：同一套控件模板，sx- 前缀（漏了注入的话点开弹窗会直接报错） */
$("#sx-panel").innerHTML = fxPanelHTML("sx-");
/* 注完就按当前结构锁一次：「缓冲变速」在单集钩子前置里固定原速（见 syncHookFxLocks） */
syncHookFxLocks();
fxWire("sx-");
$("#btn-fx-pic").addEventListener("click", () => {
  const panel = $("#fx-pic-panel");
  panel.classList.toggle("hidden");
  $("#btn-fx-pic").textContent = panel.classList.contains("hidden") ? "展开设置 ▾" : "收起设置 ▴";
});
let _fxSaveT = null;
function saveFxDefault() {
  clearTimeout(_fxSaveT);
  _fxSaveT = setTimeout(async () => {
    if (!ST.proj) return;
    try {
      await api(`/api/projects/${ST.proj.id}/fx-default`, {
        method: "PUT", body: JSON.stringify({ fx: readFx("fx-") }),
      });
    } catch (e) { /* 存不上不打断调参，出片时会再校验 */ }
  }, 700);
}
["title", "zoom", "hook", "speed", "bgm", "bgmauto", "bgmmanual", "bgmpath", "bvol",
 "grade", "gint", "randmin", "randmax", "lut", "mirror", "stretch", "glow", "grain", "vig",
 "srcver", "fmirror", "fmirrordir", "fp"]
  .forEach((id) => {
    const el = $("#fx-" + id);
    if (!el) return;
    el.addEventListener("input", saveFxDefault);
    el.addEventListener("change", saveFxDefault);
  });

/* 每套单独设置弹窗 */
let _pfxIdx = null;
function openPlanFx(i) {
  const pl = (ST.proj && ST.proj.plans || [])[i];
  if (!pl) return;
  _pfxIdx = i;
  $("#pfx-title").textContent = pl.tag ? `${pl.tag} · 出片与画面` : `第 ${pl.index} 套 · 出片与画面`;
  // 弹窗显示的是这套最终会用的值：默认 + 这套已存的覆盖
  writeFx("sx-", Object.assign({}, readFx("fx-"), pl.fx || {}));
  $("#pfx-mode").textContent = pl.fx ? "这套是自定义（改默认不影响它）" : "当前跟随默认";
  $("#modal-planfx").classList.remove("hidden");
}
$("#pfx-close").addEventListener("click", () => $("#modal-planfx").classList.add("hidden"));
$("#modal-planfx").addEventListener("click", (e) => {
  if (e.target.id === "modal-planfx") $("#modal-planfx").classList.add("hidden");
});
$("#pfx-reset").addEventListener("click", () => {
  writeFx("sx-", readFx("fx-"));
  toast("已恢复成默认值，点「保存」生效");
});
$("#pfx-save").addEventListener("click", async () => {
  const pl = (ST.proj && ST.proj.plans || [])[_pfxIdx];
  if (!pl) return;
  const eff = readFx("sx-"), base = readFx("fx-");
  const diff = {};
  let changed = false;
  for (const k of Object.keys(eff)) {
    if (JSON.stringify(eff[k]) !== JSON.stringify(base[k])) { diff[k] = eff[k]; changed = true; }
  }
  try {
    await api(`/api/projects/${ST.proj.id}/plan-fx`, {
      method: "PUT",
      body: JSON.stringify({ index: pl.index, fx: changed ? diff : null }),
    });
    if (changed) pl.fx = diff; else delete pl.fx;
    $("#modal-planfx").classList.add("hidden");
    updatePlanFxChips();
    toast(changed ? `已保存：这套出片用「${fxShort(pl.fx)}」` : "这套已恢复跟随默认");
  } catch (e) {
    toast("保存失败：" + e.message);
  }
});
function updatePlanFxChips() {
  document.querySelectorAll("#plan-list .plan").forEach((el) => {
    const pl = (ST.proj && ST.proj.plans || [])[Number(el.dataset.i)];
    if (!pl) return;
    const chip = el.querySelector(".plan-fx");
    if (chip) {
      chip.textContent = "✎ 设置 · " + (pl.fx ? fxShort(pl.fx) : "跟随默认");
      chip.classList.toggle("info", !!pl.fx);
    }
  });
  const sum = $("#fx-pic-sum");
  if (sum) {
    const d = fxShort(readFx("fx-"));
    sum.textContent = d === "默认"
      ? "这批套的默认设置（含轻质感：柔光 / 细颗粒 / 轻暗角 / 1.5% 变形，看不出来） · 每套标题里的「✎ 设置」可单独改"
      : `默认：${d}（每套标题里的「✎ 设置」可单独改）`;
  }
}

$("#btn-render-plans").addEventListener("click", async () => {
  const p = ST.proj;
  const on = (p.plans || []).filter((pl) => pl.on !== false);
  if (!on.length) return toast("没有勾选任何结构，先在「挑选要出片的套」里勾选");
  const saveDir = ($("#save-dir").value || "").trim();
  if (!saveDir) return toast("先填一个成片保存位置（点「选择文件夹…」更快）");
  const outDir = joinDir(saveDir, p.name);
  const fx = readFx("fx-");
  // 配乐 BGM：自动模式交给曲库在出片时按每套建议解析；手动模式必须给文件
  if (fx.bgm && fx.bgm_auto === false && !fx.bgm_path)
    return toast("选了「用我自己的音乐」，先选一首自己电脑上的音乐文件");
  const fxNote = [
    fx.title ? "每块顶部看点字幕" : "不烧看点字幕",
    fx.zoom ? "画面放大 103%（去重）" : "原画面",
    fx.hook_card ? "开场 1.8 秒黑屏钩子卡" : "无片头卡",
    fx.buffer_speed ? "缓冲段加速 1.15×" : "全片原速",
    fx.bgm ? (fx.bgm_auto === false
      ? `配乐 BGM（自己的音乐，音量 ${Math.round(fx.bgm_vol * 100)}%，台词响时自动压低）`
      : `自动配乐（音量 ${Math.round(fx.bgm_vol * 100)}%，按每套建议挑曲，台词响时自动压低）`) : "不加 BGM",
    fx.src_ver === "nosub" ? "素材：已处理（原字幕换成了自己的台词）"
      : fx.src_ver === "live" ? "素材：原片 + 现场做字幕（可镜像）" : "素材：原始视频",
    fx.first_mirror ? `开头 0.3 秒${fx.first_mirror_dir === "v" ? "上下" : "左右"}镜像` : "开头不翻",
    (() => { const f = readFx("fx-"); delete f.src_ver;
      const pic = fxShort(f);
      return pic === "默认" ? "画面：轻质感（柔光/细颗粒/轻暗角/1.5%变形，看不出）" : `画面：${pic}`; })(),
  ].join("、");
  // 每套的单独覆盖：出片增强和画面都算，只有自定义过的套才带
  const setsFx = {};
  on.forEach((pl) => { if (pl.fx && Object.keys(pl.fx).length) setsFx[String(pl.index)] = pl.fx; });
  const picNote = Object.keys(setsFx).length ? `；${Object.keys(setsFx).length} 套有单独设置` : "";
  const hookless = fx.hook_card ? on.filter((pl) => !((pl.hook || pl.ai_name || "").trim())).length : 0;
  if (!confirm(`将渲染已勾选的 ${on.length} 套（每套约 2 分钟），存到：\n${outDir}\n\n出片处理：${fxNote}${picNote}；段与段之间加 60/120 毫秒音频淡入淡出（磨掉切口的咔声）。\n\n确定？`)) return;
  if (hookless) toast(`有 ${hookless} 套没有钩子文案，片头会用套名代替——展开那一套可以自己改`);
  try {
    await api(`/api/projects/${p.id}/render-plans`, {
      method: "POST",
      body: JSON.stringify({ save_dir: saveDir, fx, sets_fx: setsFx }),
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
  // 把「会丢什么」一样样列清楚（2026-11-14 用户反馈：「全部记录」太含糊）
  if (!confirm(`删除项目「${ST.proj.name}」？\n\n`
    + "会一起删掉：台词整理、AI 结构、分镜提示词、成片记录；\n"
    + "发布队列里这个项目还没发完的条目也会一并移除（已经交到平台定时的不受影响）。\n"
    + "你硬盘里的原始视频文件不会动，但删了就找不回来。确定吗？")) return;
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
  // 常用设置人人可见；「高级设置」整块只对管理员开放——终端用户不该看到
  // 生成方式、凭证、机器信息、日志上报和内部目录，这些是排障信息不是操作项。
  const isAdmin = !!c.admin;
  const owner = isAdmin && !c.dist_mode;
  const adv = $("#adv-settings");
  if (adv) adv.classList.toggle("hidden", !owner);
  ["#fld-llmkey", "#fld-dskey"].forEach((s) => $(s).classList.toggle("hidden", !owner));
  ["#fld-model", "#fld-shotsmodel"].forEach((s) => {
    const el = $(s); if (el) el.classList.toggle("hidden", !!c.dist_mode);
  });
  $("#in-model").disabled = !owner;
  $("#in-shotsmodel").disabled = !owner;
  $("#btn-test-llm").classList.toggle("hidden", !owner);
  $("#ai-owner-hint").textContent = "这里只给管理员排障用，平时不需要改动。";
  $("#ver-hint").textContent = `v${c.version}`;
  $("#in-model").value = c.llm_model || "";
  $("#in-shotsmodel").value = c.shots_model || "";
  $("#model-hint").textContent = "改完可点「测试连通」确认能正常生成。";
  $("#llm-test-hint").className = "fld-note hidden";
  setText("#llm-test-hint", "");
  $("#in-key").value = "";
  $("#in-key").placeholder = c.llm_key_set ? `${c.llm_key_hint}（留空则不修改）` : "留空则不修改";
  $("#key-hint").textContent = c.llm_key_set ? "已配置，留空表示不改。" : "尚未配置。";
  $("#in-dskey").value = "";
  $("#in-dskey").placeholder = c.ds_key_set ? `${c.ds_key_hint}（留空则不修改）` : "留空则不修改";
  $("#dskey-hint").textContent = c.ds_key_set ? "已配置，留空表示不改。" : "尚未配置。";
  const tr = c.trial || {};
  const q = tr.quota;
  // 次数按天回满，文案必须出现「今天」，否则会被当成永久额度。
  // 等级名由服务端 /api/quota 透传到 c.trial.quota（免费/进阶/内部）；
  // 进阶、内部显式标出档位，免费档不单独标注（默认即免费，避免制造付费焦虑）。
  // 内部档额度视作不限量，total 不显示 1000000。
  if (q) {
    const lv = q.level_label || "免费";
    const unlimited = q.level === "internal" || (q.level_calls || 0) >= 1000000;
    const totalTxt = unlimited ? "不限" : q.total;
    const lvTag = lv === "免费" ? "" : `${lv} · `;
    $("#trial-hint").textContent =
      `${lvTag}今天还可生成 ${q.remaining}/${totalTxt} 次 · 每天 0 点恢复`;
  } else {
    $("#trial-hint").textContent =
      (c.llm_key_set || owner ? "AI 生成已就绪" : "AI 生成服务暂不可用，稍后再试。");
  }
  const t = (c.tiers || [])[0];
  $("#tier-hint").textContent = t && t.available
    ? "台词识别已就绪" : "未找到所需文件，安装包可能不完整";
  const s = c.system || {};
  $("#sys-hint").textContent =
    `本机：${s.cpu} 核 CPU · ${s.mem_gb ? s.mem_gb + " GB 内存" : "内存未知"}`;
  $("#in-outdir").value = c.paths.output || "";
  $("#in-hwenc").value = c.hw_encode === "off" ? "off" : "auto";
  if ($("#in-conc")) $("#in-conc").value = String(c.max_concurrent || 3);
  const fldReport = $("#fld-report");
  if (fldReport) fldReport.classList.toggle("hidden", !!c.dist_mode);
  if ($("#in-report")) $("#in-report").checked = !!c.report_audit;
  if ($("#report-hint")) {
    $("#report-hint").textContent = c.report_audit
      ? "已开启：出问题时后台能看到这台机器的操作时间线。"
      : "未开启：勾上后可在后台排查这台机器的操作记录。";
  }
  const ofree = c.paths.output_free_gb;
  const dfree = c.paths.data_free_gb;
  $("#outdir-hint").textContent =
    (ofree >= 0 ? `成片盘剩余 ${ofree} GB。` : "") +
    (dfree >= 0 && dfree < 2 ? `⚠ 可用空间不多了，建议换个盘保存。` : "") +
    " 空间不足会导致出片失败。";
  $("#path-hint").textContent = `数据目录：${c.paths.data}\n提示词目录：${c.paths.prompts}`;
  paintThemeSeg();
  $("#modal-settings").classList.remove("hidden");
}

/* ── 主题（2026-11-14）：system/dark/light。index.html 的启动脚本已在页面
   打开时按 localStorage 预设 data-theme；这里只在设置弹窗里改选择。 ── */
function paintThemeSeg() {
  let t = "dark";
  try { t = localStorage.getItem("hs_theme") || "dark"; } catch (e) {}
  document.querySelectorAll("#seg-theme button").forEach((b) => {
    b.classList.toggle("on", b.dataset.themeSet === t);
  });
}
document.querySelectorAll("#seg-theme button").forEach((b) => {
  b.addEventListener("click", () => {
    const t = b.dataset.themeSet;
    try { localStorage.setItem("hs_theme", t); } catch (e) {}
    document.documentElement.setAttribute("data-theme", t === "system"
      ? ((window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light")
      : t);
    paintThemeSeg();
  });
});

$("#btn-settings").addEventListener("click", () => openSettings().catch((e) => toast(e.message)));
$("#btn-close-settings").addEventListener("click", () => $("#modal-settings").classList.add("hidden"));
$("#modal-settings").addEventListener("click", (e) => {
  if (e.target.id === "modal-settings") $("#modal-settings").classList.add("hidden");
});

/* 联系作者：顶栏按钮 + 首屏横幅 + 右下角兜底，都开同一个二维码弹窗。
   需求都从这来——入口放用户视线动线上（顶栏/首屏），边角悬浮只当兜底。 */
const authorModal = $("#modal-author");
const openAuthor = () => authorModal.classList.remove("hidden");
document.querySelectorAll("[data-author]").forEach((b) => {
  b.addEventListener("click", openAuthor);
  if (b.tagName !== "BUTTON") {           // 首屏横幅是 div，补键盘可达
    b.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openAuthor(); }
    });
  }
});
$("#btn-close-author").addEventListener("click", () => authorModal.classList.add("hidden"));
authorModal.addEventListener("click", (e) => {
  if (e.target === authorModal) authorModal.classList.add("hidden");
});

/* 每台电脑第一次打开时自动弹一次：用户不会主动点不认识的按钮，
   所以第一次必须主动送到他眼前（只弹一次，记在 localStorage，别变成骚扰）。 */
(function autoShowAuthorOnce() {
  const KEY = "hs_author_qr_v1";
  try {
    if (localStorage.getItem(KEY)) return;
  } catch (e) { return; }                 // 无痕模式读不了就干脆不弹
  setTimeout(() => {
    // 登录页/升级闸/设置弹窗已经盖在上面时，别叠一层
    if (document.querySelector(".modal:not(.hidden), .gate")) return;
    openAuthor();
    try { localStorage.setItem(KEY, "1"); } catch (e) { /* 忽略 */ }
  }, 1600);
})();

/* 测试连通：拿输入框里**还没保存**的值试一次，试通了再点保存。
   13 个模型横评里有 4 个失败，用户每次都是等十几分钟任务跑完才知道
   「没开通 / 账号欠费 / 被限流」，这一步就是把这个时间压到几秒。 */
$("#btn-test-llm").addEventListener("click", async () => {
  const el = $("#llm-test-hint");
  const btn = $("#btn-test-llm");
  if (!el || btn.disabled) return;
  const body = { model: $("#in-model").value.trim() };
  const k = $("#in-key").value.trim();
  if (k) body.llm_key = k;
  const dk = $("#in-dskey").value.trim();
  if (dk) body.ds_key = dk;
  // 分镜模型填了且和主模型不一样就一起测，省得跑十几分钟才发现填错
  const smo = $("#in-shotsmodel").value.trim();
  const also = smo && smo !== body.model ? smo : "";
  btn.disabled = true;
  setText(btn, "测试中…");
  el.classList.remove("hidden", "ok", "bad");
  setText(el, `正在用「${body.model || "当前模型"}」发一次最小请求，最多等 2 分钟…`);
  try {
    const r = await api("/api/llm-test", { method: "POST", body: JSON.stringify(body) });
    el.classList.add(r.ok ? "ok" : "bad");
    const where = r.provider === "deepseek" ? "DeepSeek 官方" : "百炼";
    let msg = `${r.ok ? "可以用了" : "用不了"} · ${r.model}（${where}）\n${r.detail}`;
    if (also) {
      setText(el, msg + `\n正在测分镜模型「${also}」…`);
      const r2 = await api("/api/llm-test", { method: "POST", body: JSON.stringify({ ...body, model: also }) });
      const where2 = r2.provider === "deepseek" ? "DeepSeek 官方" : "百炼";
      msg += `\n分镜模型：${r2.ok ? "可以用了" : "用不了"} · ${r2.model}（${where2}）\n${r2.detail}`;
      el.classList.toggle("bad", !r.ok || !r2.ok);
    }
    setText(el, msg);
  } catch (e) {
    el.classList.add("bad");
    setText(el, "测试没跑起来：" + e.message);
  } finally {
    setText(btn, "测试连通");
    btn.disabled = false;
  }
});

$("#btn-save-settings").addEventListener("click", async () => {
  const body = {};
  // 模型 / Key 是服务端凭证，只有管理员能改（后端也会 403 兜住）。
  // 分发版一律不提交这几项：输入框都藏着，硬提交只会把已配好的模型清成空串。
  if (SETTINGS && SETTINGS.admin && !SETTINGS.dist_mode) {
    const mo = $("#in-model").value.trim();
    if (mo && mo !== SETTINGS.llm_model) body.llm_model = mo;
    // 分镜模型允许清空（存空串 = 跟主模型一致），所以只要变了就提交
    const smo = $("#in-shotsmodel").value.trim();
    if (smo !== (SETTINGS.shots_model || "")) body.shots_model = smo;
    const k = $("#in-key").value.trim();
    if (k) body.llm_key = k;
    const dk = $("#in-dskey").value.trim();
    if (dk) body.ds_key = dk;
  }
  const od = $("#in-outdir").value.trim();
  if (SETTINGS && od !== ((SETTINGS.paths || {}).output || "")) body.output_dir = od;
  // 本机渲染偏好：登录用户可改（后端同 output_dir 权限）
  const hw = $("#in-hwenc") ? $("#in-hwenc").value : "auto";
  if (SETTINGS && hw !== (SETTINGS.hw_encode || "auto")) body.hw_encode = hw;
  // 同时生成上限：与后端共用一份设置，第 2 步的预计耗时也会跟着变
  if (SETTINGS && $("#in-conc")) {
    const mc = Number($("#in-conc").value);
    if (mc !== (SETTINGS.max_concurrent || 3)) body.max_concurrent = mc;
  }
  // 日志上报：只在开发机出现这个勾选框（安装版后端会拒绝，前端也藏了）
  if (SETTINGS && !SETTINGS.dist_mode && $("#in-report")) {
    const want = $("#in-report").checked;
    if (want !== !!SETTINGS.report_audit) body.report_audit = want;
  }
  try {
    if (Object.keys(body).length) {
      await api("/api/config", { method: "PUT", body: JSON.stringify(body) });
      // 预计耗时按这个上限算，别等下次打开设置才更新
      if (body.max_concurrent) SETTINGS.max_concurrent = body.max_concurrent;
    }
    $("#modal-settings").classList.add("hidden");
    toast("已保存");
    const st = await api("/api/status");
    ST.status = st;
    $("#pills").innerHTML = [
      `<span class="pill ${st.llm_key ? "ok" : "bad"}">AI 生成 ${st.llm_key ? "已就绪" : "暂不可用"}</span>`,
      `<span class="pill ${st.whisper_model ? "ok" : "bad"}">台词识别 ${st.whisper_model ? "已就绪" : "需检查"}</span>`,
    ].join("");
  } catch (e) {
    toast("保存失败：" + e.message);
  }
});

boot().catch((e) => toast("初始化失败：" + e.message));

/* ── 登录过期全站锁屏（2026-11-14 用户定稿：硬过期）─────────────────────
   以前 access 过期后页面照样可交互：所有请求都会失败，用户在一个「僵尸页」
   里点来点去，操作一个都生效不了——直到某个请求 401 才被弹去登录页。
   现在过期即锁屏（不再无感续期，登录有效期 24 小时到点重登），
   落全站遮罩，只能重新登录。遮罩复用版本闸的 upd-mask 样式。 ── */
function showSessMask() {
  if (document.querySelector("#sess-mask")) return;
  Session.state.clear();
  const mask = document.createElement("div");
  mask.id = "sess-mask";
  mask.className = "upd-mask";
  mask.innerHTML = `<div class="upd-card"><p class="upd-lead">登录已过期</p>`
    + `<p>继续操作需要重新登录。已经完成的内容都在，不会丢。</p>`
    + `<button class="primary" id="sess-go">重新登录</button></div>`;
  document.body.appendChild(mask);
  $("#sess-go").addEventListener("click", () => {
    location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
  });
}
function sessionWatch() {
  if (!window.Session) return;
  if (location.pathname.startsWith("/login")) return;
  if (document.querySelector("#sess-mask")) return;
  // 硬过期：有效期一过直接锁屏（登录有效期 24 小时，到点重新登录），
  // 不再做无感续期——那会让「过期」形同虚设（2026-11-14 用户定稿）。
  if (!Session.accessValid()) showSessMask();
}
setInterval(sessionWatch, 10000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) sessionWatch();
});
sessionWatch();

/* ---------- 右上角账号区（2026-11-04 用户体系） ----------
   登录是**固定要求**（免登录模式已整体删除）：没凭证时 boot() 就直接去登录页了，
   所以这里的「未登录」只可能出现在「刚被踢/刚登出」的一瞬间，点它回登录页。
   登录后显示昵称 + 角色，管理员多一个「管理后台」入口。
   这里刻意不引组件/框架：一段 innerHTML + 两个全局函数，改动面最小。 */
async function loadWho() {
  const el = $("#who");
  if (!el) return;
  let me;
  try {
    me = await api("/api/auth/me");
  } catch (e) {
    el.innerHTML = "";
    return;
  }
  ST.me = me;
  if (!me.logged_in) {
    el.innerHTML = `<a class="who-btn" href="/login" title="未登录——登录后才能使用">
      <span class="who-av">?</span>未登录</a>`;
    return;
  }
  const u = me.user;
  const name = u.display_name || u.username;
  el.innerHTML = `
    <button class="who-btn" onclick="document.getElementById('who-menu').classList.toggle('on')"
            title="${esc(name)} · ${esc(u.username)}">
      <span class="who-av">${esc(name.slice(0, 1))}</span>${esc(name)}
      ${me.permissions && me.permissions.admin ? '<span class="ad-tag admin">管理员</span>' : ""}
    </button>
    <div class="who-menu" id="who-menu">
      <div class="muted">${esc(u.username)}${u.email ? " · " + esc(u.email) : ""}</div>
      <div class="sep"></div>
      ${me.permissions && me.permissions.admin ? '<a href="/admin">管理后台</a>' : ""}
      <button onclick="whoChangePwd()">修改密码</button>
      <button onclick="whoLogout()">退出登录</button>
    </div>`;
  // 还在用初始密码：不是「提示一下」，是直接把人按在改密卡片上。
  // 后端中间件已经把所有业务接口拦成 403（code=must-change-pwd），
  // 这里主动弹是为了不让用户先看到一排报错。
  if (u.must_change_pwd) Session.requirePasswordChange();
}

async function whoChangePwd() {
  $("#who-menu") && $("#who-menu").classList.remove("on");
  const oldPwd = prompt("请输入原密码");
  if (!oldPwd) return;
  const newPwd = prompt("请输入新密码（至少 8 位，不能是纯数字）");
  if (!newPwd) return;
  try {
    const d = await api("/api/auth/password", {
      method: "PUT",
      body: JSON.stringify({ old_password: oldPwd, new_password: newPwd }),
    });
    // 改密会让所有旧 token 失效，后端顺手发了一对新的，这里换掉本地的
    if (d && d.access_token) Session.state.save(d);
    toast("密码已修改，其他设备已退出登录");
    loadWho();
  } catch (e) {
    toast("改密失败：" + e.message);
  }
}

async function whoLogout() {
  try { await Session.logout(false); } catch (e) { /* 网络失败也清本地 */ }
  toast("已退出登录");
  setTimeout(() => location.reload(), 350);
}

/* ── 第 4 步：两两合并（A × B 组合出片）────────────────────────────────────
   相对独立：不依赖项目与前面步骤。选两个文件夹 → 预览组合 → 开跑；
   逐对重编码统一规格，已存在的自动跳过（断点续跑），可随时取消。 */
(function () {
  const pFill = $("#vm-prog-fill"), pText = $("#vm-prog-text"), pBar = $("#vm-prog");
  const bStart = $("#btn-vm-start"), bCancel = $("#btn-vm-cancel"), bOpen = $("#btn-vm-open");
  let timer = null;

  async function pick(cur) {
    try {
      const r = await api(`/api/pick-folder?initial=${encodeURIComponent(cur || "")}`, { method: "POST" });
      return (r && r.path) || "";
    } catch (e) {
      toast(`${e.message}（可以直接手动输入路径）`);
      return "";
    }
  }

  $("#btn-vm-a").addEventListener("click", async () => {
    const p = await pick($("#in-vm-a").value.trim());
    if (p) $("#in-vm-a").value = p;
  });
  $("#btn-vm-b").addEventListener("click", async () => {
    const p = await pick($("#in-vm-b").value.trim());
    if (p) $("#in-vm-b").value = p;
  });
  $("#btn-vm-out").addEventListener("click", async () => {
    const p = await pick($("#in-vm-out").value.trim());
    if (p) $("#in-vm-out").value = p;
  });

  $("#btn-vm-preview").addEventListener("click", async () => {
    const box = $("#vm-preview");
    const a = $("#in-vm-a").value.trim(), b = $("#in-vm-b").value.trim();
    if (!a || !b) return toast("先把两个文件夹都选好");
    box.textContent = "正在扫描…";
    try {
      const r = await api("/api/merge/preview", { method: "POST", body: JSON.stringify({ a, b }) });
      if (!r.a_files.length || !r.b_files.length) {
        box.textContent = `⚠️ ${!r.a_files.length ? "文件夹 A" : "文件夹 B"} 里没有视频文件（支持 mp4/mov/mkv/avi/flv/wmv/webm/m4v/ts/mpg）`;
        return;
      }
      const nm = (arr, k) => arr.slice(0, k).join("、") + (arr.length > k ? " 等" : "");
      box.innerHTML =
        `A 有 ${r.a_files.length} 个：${esc(nm(r.a_files, 5))}<br>` +
        `B 有 ${r.b_files.length} 个：${esc(nm(r.b_files, 5))}<br>` +
        `<b>将组合出 ${r.pairs} 条</b>（${r.a_files.length} × ${r.b_files.length}，每条 = A 一段在前 + B 一段在后）。` +
        (r.pairs > 40 ? " 数量不小，每条约需几十秒到几分钟，确认硬盘空间够再开跑。" : "");
    } catch (e) {
      box.textContent = "";
      toast(e.message);
    }
  });

  function setRunning(on, msg, pct) {
    bStart.disabled = on;
    bCancel.classList.toggle("hidden", !on);
    pBar.classList.toggle("hidden", !on);
    if (on) {
      pFill.style.width = (pct || 0) + "%";
      pText.textContent = msg || "处理中…";
    }
  }

  async function refresh(final) {
    try {
      const s = await api("/api/merge/status");
      if (s.running) {
        setRunning(true, s.msg, s.pct);
        return true; // 还在跑，继续轮询
      }
      setRunning(false);
      if (timer) { clearInterval(timer); timer = null; }
      if (final || s.total > 0) {
        const parts = [`新拼 ${s.done} 条`, `跳过已有 ${s.skipped} 条`];
        if (s.failed && s.failed.length) parts.push(`失败 ${s.failed.length} 条`);
        if (s.error) parts.push(`出错：${s.error}`);
        const el = $("#vm-preview");
        el.innerHTML = (s.error ? "❌ " : "✅ ") + parts.join("，") +
          (s.failed && s.failed.length ? `<br>${esc(s.failed.slice(0, 5).join("<br>"))}` : "") +
          `　<b>输出目录：${esc(s.out_dir || "")}</b>`;
        bOpen.classList.remove("hidden");
      }
      return false;
    } catch (e) {
      return false;
    }
  }

  bStart.addEventListener("click", async () => {
    const a = $("#in-vm-a").value.trim(), b = $("#in-vm-b").value.trim();
    if (!a || !b) return toast("先把两个文件夹都选好");
    const out = $("#in-vm-out").value.trim();
    let pairs = null;
    // 用预览数据算总数提示；没点过预览就先替用户查一次
    try {
      const pv = await api("/api/merge/preview", { method: "POST", body: JSON.stringify({ a, b }) });
      pairs = pv.pairs;
      if (!pv.a_files.length || !pv.b_files.length) {
        return toast(`${!pv.a_files.length ? "文件夹 A" : "文件夹 B"} 里没有视频文件，先检查一下`);
      }
    } catch (e) { return toast(e.message); }
    const outDir = out || "设置里「成片保存位置」下的「两两合并」文件夹";
    if (!confirm(`将组合生成 ${pairs} 条视频（A 段在前、B 段在后），统一成竖屏画面和一致的音量，存到：\n${outDir}\n\n已存在的同名文件会跳过不重做。确定开跑？`)) return;
    try {
      await api("/api/merge/start", { method: "POST", body: JSON.stringify({ a, b, out }) });
      bOpen.classList.add("hidden");
      setRunning(true, "已排队…", 0);
      if (timer) clearInterval(timer);
      timer = setInterval(refresh, 1500);
    } catch (e) {
      toast(e.message);
    }
  });

  bCancel.addEventListener("click", async () => {
    try { await api("/api/merge/cancel", { method: "POST" }); toast("正在取消，等当前这一对编完…"); }
    catch (e) { toast(e.message); }
  });

  bOpen.addEventListener("click", async () => {
    try { await api("/api/merge/open", { method: "POST" }); }
    catch (e) { toast(e.message); }
  });

  // 页面加载时查一次：上次合并还在后台跑（比如刷新过页面）就恢复进度条
  refresh(false).then((still) => {
    if (still && !timer) timer = setInterval(refresh, 1500);
  });
})();
