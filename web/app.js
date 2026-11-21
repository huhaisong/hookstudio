const $ = (s) => document.querySelector(s);
const ST = { path: "", proj: null, timer: null, pendingRender: null };


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
  if (!r.ok) {
    const e = new Error(data.detail || "HTTP " + r.status);
    e.status = r.status;   // 让「409 再确认一次」这类分支能按状态码判断
    throw e;
  }
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

/* 应用内确认框：原生 confirm 在用户多次取消后会被浏览器静默拦截（点了
   没反应），且同步阻塞轮询、进度条僵死。高风险操作一律走这里。
   askConfirm(opts) 返回 Promise<boolean>：true=确认，false=取消。 */
const cfmModal = $("#modal-confirm");
const cfmTitle = $("#cfm-title");
const cfmBody = $("#cfm-body");
const cfmOk = $("#cfm-ok");
const cfmCancel = $("#cfm-cancel");
let cfmDone = null;                       // 非 null 表示有一个确认框还开着
let cfmBack = null;                       // 打开时的焦点，关闭后还回去
let cfmKeydown = null;
function cfmClose(ok) {
  if (!cfmDone) return;
  const done = cfmDone;
  cfmDone = null;
  document.removeEventListener("keydown", cfmKeydown);
  cfmModal.classList.add("hidden");
  // 还焦点：原焦点可能已被轮询置灰或随界面刷新移除，还不了就算了
  try { if (cfmBack && document.contains(cfmBack)) cfmBack.focus(); } catch (e) { /* 忽略 */ }
  // 复位到默认态：下次打开不残留红色主按钮和上一次的文案
  cfmBody.textContent = "";
  cfmTitle.textContent = "确认";
  cfmOk.textContent = "确定";
  cfmOk.classList.remove("danger-solid");
  cfmCancel.textContent = "取消";
  done(ok);
}
window.askConfirm = function (opts) {
  return new Promise((resolve) => {
    // 同一时间只允许一个确认框：叠着弹会把用户绕晕，后来的直接算取消
    if (cfmDone) { resolve(false); return; }
    cfmDone = resolve;
    cfmTitle.textContent = opts.title || "确认";
    const segs = Array.isArray(opts.body) ? opts.body : String(opts.body || "").split("\n");
    segs.forEach((s) => {
      if (!String(s).trim()) return;      // 空段不排，别留半截空白
      const p = document.createElement("p");
      p.textContent = String(s);          // 纯文本，不拼 HTML
      cfmBody.appendChild(p);
    });
    cfmOk.textContent = opts.okText || "确定";
    cfmCancel.textContent = opts.cancelText || "取消";
    cfmOk.classList.toggle("danger-solid", !!opts.danger);
    cfmBack = document.activeElement;
    cfmModal.classList.remove("hidden");
    (opts.danger ? cfmCancel : cfmOk).focus();   // 危险动作焦点落在「取消」
    cfmKeydown = (e) => {
      if (e.key === "Escape") { e.preventDefault(); cfmClose(false); }
      else if (e.key === "Tab") {         // 只有取消/确定两个焦点，手动互换，别走出弹窗
        e.preventDefault();
        (document.activeElement === cfmOk ? cfmCancel : cfmOk).focus();
      }
    };
    document.addEventListener("keydown", cfmKeydown);
  });
};
cfmOk.addEventListener("click", () => cfmClose(true));
cfmCancel.addEventListener("click", () => cfmClose(false));
cfmModal.addEventListener("click", (e) => { if (e.target === cfmModal) cfmClose(false); });

function fmt(sec) {
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

/* 2026-11-19 用户定：全界面统一显示真实模型名，和下拉 PM_MODELS 一个口径。
   这张表给已选集/套的标签和检测提示用；只有「跟随设置」(空值) 和未知型号仍说人话。 */
const MODEL_LABEL = {
  "qwen3.5-plus": "qwen3.5-plus", "ZHIPU/GLM-5.2": "ZHIPU/GLM-5.2",
  "ZHIPU/GLM-5.3": "ZHIPU/GLM-5.3", "deepseek-v4-pro": "deepseek-v4-pro",
  "deepseek-v4.1-flash": "deepseek-v4.1-flash",
  "deepseek-flash": "deepseek-flash", "qwen-max": "qwen-max",
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
  paintPills(st);

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
    ST.autoCfg = cfg.autopipe_cfg || {};   // 一键全自动上次的参数（记住上次）
    if (!$("#save-dir").value && ST.cfgOutput) {
      $("#save-dir").value = ST.cfgOutput.replace(/\\/g, "/");
    }
  } catch (_) { /* 配置读不到不影响主流程 */ }
  await loadModelsAlive();
  updateModeHint();
  await loadList();
  loadSaveDirHist();
  // 上次异常中断的任务：开机不再自动跑，这里逐条问用户要不要续（C 档 2026-11-19）
  askResumePending();
}

/* 待确认续跑：后端 startup 只登记不自动跑，这里问过用户才发 decide。
   一次问一条、递归处理下一条；用户点「先不做」=忽略，不再反复打扰。 */
async function askResumePending(list) {
  try {
    if (list === undefined) {
      const r = await api("/api/resume/pending");
      list = r.items || [];
    }
    if (!list.length) return;
    const it = list[0];
    const ok = await askConfirm({
      title: "上次有个任务没做完",
      body: [
        `「${it.name || "一个项目"}」的「${it.label}」上次没跑完就停了。`,
        "程序重开后不会自己接着跑——要继续吗？已经做好的部分都在，继续只补没做完的。",
      ],
      okText: "接着做", cancelText: "先不做",
    });
    try {
      await api("/api/resume/decide", {
        method: "POST",
        body: JSON.stringify({ pid: it.pid, action: ok ? "run" : "dismiss" }),
      });
    } catch (_) { /* 忽略：项目可能已删，decide 接口幂等 */ }
    // 用户确认继续的，若正在看这个项目就刷新状态条；再问下一条
    if (ok && ST.proj && ST.proj.id === it.pid) {
      try { ST.proj = await api("/api/projects/" + it.pid); render(); startPoll(); } catch (_) {}
    }
    await askResumePending(list.slice(1));
  } catch (_) { /* 询问失败不阻塞正常使用 */ }
}

async function loadList() {
  const rows = await api("/api/projects");
  const modeName = (m) => m === "batch" ? "批量混剪"
    : m === "shots" ? "只做引流分镜" : "单片导流（历史）";
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

/* 顶栏状态（2026-11-15 用户定）：只在**出问题**时出现。
   两个都正常时是纯噪音——信息在设置里也有，还占掉顶栏最显眼的位置、跟
   「联系作者」抢注意力。所以正常就一个胶囊都不留，异常才变红并可点去设置。
   留着而不是删掉，是因为 AI 不可用时这是全站唯一随时可见的健康信号，
   删干净以后用户只会看到一串失败提示，不知道是配置问题。 */
function paintPills(st) {
  const bad = [];
  if (!st || !st.llm_key) bad.push("AI 生成不可用");
  if (st && !st.whisper_model) bad.push("台词识别未就绪");
  $("#pills").innerHTML = bad.length
    ? bad.map((t) => `<button class="pill bad" data-pill-fix
        title="${t}：点一下打开设置检查">${t}</button>`).join("")
    : "";
  document.querySelectorAll("[data-pill-fix]").forEach((el) =>
    el.addEventListener("click", () => $("#btn-settings").click()));
}

/* 「最近用过」：位置由后端记着（app/pickmem.py，选完就记），这里只负责摆出来。
   点一下等于「选中那个位置」，不再弹框——同一批素材天天用，省一次翻目录。 */
function tailDir(p, n = 2) {
  const segs = String(p || "").replace(/\\/g, "/").split("/").filter(Boolean);
  if (!segs.length) return String(p || "");
  const tail = segs.slice(-n).join("/");
  return segs.length > n ? "…/" + tail : tail;
}

async function loadHistChips(boxId, key, onPick) {
  const box = document.getElementById(boxId);
  if (!box) return;
  let items = [];
  try {
    const r = await api(`/api/pick-history?key=${encodeURIComponent(key)}&limit=3`);
    items = (r && r.items) || [];
  } catch (e) {
    box.innerHTML = "";                       // 读不到历史不影响主流程
    return;
  }
  if (!items.length) {
    box.innerHTML = "";
    return;
  }
  box.innerHTML = `<span class="hist-l">最近用过</span>` + items.map((it) =>
    `<button class="hist-chip" data-path="${esc(it.path)}"
      title="${esc(it.path)}">${esc(tailDir(it.path))}</button>`).join("");
  box.querySelectorAll(".hist-chip").forEach((el) =>
    el.addEventListener("click", () => onPick(el.dataset.path)));
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
  // 一键全自动只对「批量混剪」有意义；分镜模式没有台词/底片/出片这条链，藏掉
  const autoCard = $("#auto-new-card");
  if (autoCard) autoCard.classList.toggle("hidden", shotsOnly);
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

/* 新建页：勾「全自动一条龙」→ 展开参数面板（记住上次）。 */
$("#in-autopipe").addEventListener("change", (e) => {
  const box = $("#auto-new-params");
  if (!box) return;
  const on = e.target.checked;
  box.classList.toggle("hidden", !on);
  if (on && !box.dataset.ready) {
    box.innerHTML = autoParamsHTML("an_");
    autoBind("an_");                 // 先绑事件，回填 BGM 勾选时 change 才会显示文件夹框
    autoFillCfg("an_", ST.autoCfg);
    box.dataset.ready = "1";
  }
});

/* ===================== 一键全自动（2026-11-21）=====================
   同一套参数面板在「新建项目页」(前缀 an_) 和「项目页」(前缀 ap_) 共用。
   cfg 结构：{jobs:[{model,count,min_minutes,max_minutes}], order, eps, save_dir,
              fx:{auto_fx,src_ver,mirror,bgm,bgm_mode,bgm_dir,...}}
   记住上次：存进 config.autopipe_cfg（后端 ConfigBody/AutoPipeBody 都收）。 */
const AUTO_DEF_JOBS = [
  { model: "deepseek-v4.1-flash", count: 5 },
  { model: "ZHIPU/GLM-5.2", count: 5 },
  { model: "deepseek-v4-pro", count: 5 },
];
const AUTO_DEF_MIN = 3, AUTO_DEF_MAX = 7;
// PM_MODELS 在本文件后面才 const 声明；若在顶部 .filter 会触发 TDZ，整份脚本停摆。
// 改成函数调用时再求值（面板只在用户交互时渲染，那时 PM_MODELS 已初始化）。
function autoRealModels() { return PM_MODELS.filter(([v]) => v); }

function autoModelOpts(sel) {
  const models = autoRealModels();
  const known = models.some(([v]) => v === sel);
  let h = models.map(([v, t]) =>
    `<option value="${esc(v)}"${v === sel ? " selected" : ""}>${esc(t)}</option>`).join("");
  // 老配置里存了不在候选里的自定义型号：补一个选中项，别把它悄悄改掉
  if (sel && !known) h += `<option value="${esc(sel)}" selected>${esc(sel)}（自定义）</option>`;
  return h;
}

/* 生成一套参数面板 HTML。p = id 前缀，避免两处面板 id 撞车。 */
function autoParamsHTML(p) {
  const rows = AUTO_DEF_JOBS.map((j, i) => `
    <div class="row auto-job-row" style="gap:8px;margin:6px 0">
      <label style="flex:2;min-width:180px">模型 ${i + 1}
        <select id="${p}model${i}" class="auto-model">${autoModelOpts(j.model)}</select></label>
      <label style="flex:1;min-width:90px">出几套
        <input id="${p}count${i}" class="auto-count" type="number" min="1" max="20" value="${j.count}"></label>
    </div>`).join("");
  return `
    <div class="fx-group" style="margin-top:12px">
      <div class="fx-group-h"><i class="fx-gno">1</i>用哪些模型、各出几套<span class="hint">AI 会读全剧台词，每个模型各排一套剪法</span></div>
      ${rows}
      <p class="hint">每套时长 <input id="${p}minmin" class="auto-min" type="number" min="1" max="15" value="${AUTO_DEF_MIN}" style="width:56px"> ~ <input id="${p}maxmin" class="auto-max" type="number" min="1" max="20" value="${AUTO_DEF_MAX}" style="width:56px"> 分钟</p>
    </div>
    <div class="fx-group" style="margin-top:12px">
      <div class="fx-group-h"><i class="fx-gno">2</i>随机与画面<span class="hint">默认就是你要的「全自动随机 + 按集镜像」</span></div>
      <div class="fx-checks">
        <label class="chk"><input type="checkbox" id="${p}rand" checked> 全自动随机（选集随机 + 干净底片按集随机左右镜像）</label>
      </div>
      <p class="hint">关掉就按集顺序用原始视频、不镜像。一般保持开就行。</p>
    </div>
    <div class="fx-group" style="margin-top:12px">
      <div class="fx-group-h"><i class="fx-gno">3</i>成片存哪<span class="hint">片子按剧名归到这个文件夹下的子目录</span></div>
      <div class="row" style="gap:8px">
        <label style="flex:1;min-width:180px">保存到
          <input id="${p}savedir" class="auto-savedir" type="text" spellcheck="false" placeholder="例如 F:/钩子工坊_成片"></label>
        <button class="ghost" type="button" data-autosavedirpick="${p}">选择文件夹…</button>
      </div>
    </div>
    <div class="fx-group" style="margin-top:12px">
      <div class="fx-group-h"><i class="fx-gno">4</i>配乐与声音<span class="hint">配乐可选；去掉原片音乐默认开</span></div>
      <div class="fx-checks">
        <label class="chk"><input type="checkbox" id="${p}bgm" checked> 加配乐（从我的文件夹随机挑）</label>
        <label class="chk" title="出片时把原片自带的背景音乐压掉，只留人声。适合你要换成自己的配乐、或者想去掉原片音乐降重的时候。代价：关门、碗筷、雨声这些环境音也会跟着变轻（程序会留一点点现场底），而且第一次出片要多等几分钟——同样素材再出就直接用上次的结果。默认开。"><input type="checkbox" id="${p}voiceiso" checked> 去掉原片自带音乐</label>
      </div>
      <div class="auto-bgmbox hidden" data-autobgmwrap="${p}" style="margin-top:8px">
        <div class="row" style="gap:8px">
          <label style="flex:1;min-width:180px">音乐文件夹（mp3 / m4a / wav / flac）
            <input id="${p}bgmdir" class="auto-bgmdir" type="text" spellcheck="false" placeholder="选一个放着多首歌的文件夹"></label>
          <button class="ghost" type="button" data-autobgmdirpick="${p}">选择…</button>
        </div>
        <p class="hint">每份成片从里面挑一首、尽量不重复；默认会填上上次选的文件夹。</p>
      </div>
    </div>
    <p class="hint" id="${p}err" style="color:var(--danger,#c0392b);margin:10px 0 0"></p>`;
}

function autoE(p, id) { return document.getElementById(p + id); }
function autoOn(p, id) { const e = autoE(p, id); return !!(e && e.checked); }
function autoVal(p, id) { const e = autoE(p, id); return e ? String(e.value).trim() : ""; }
function autoNum(p, id) { return Number(autoVal(p, id)) || 0; }

/* 读面板 → cfg。返回 null 表示校验没过（原因写在面板的错误行）。 */
function autoReadCfg(p, needSaveDir) {
  const setErr = (m) => { const e = autoE(p, "err"); if (e) e.textContent = m; return null; };
  const jobs = [];
  for (let i = 0; i < AUTO_DEF_JOBS.length; i++) {
    const model = autoVal(p, "model" + i);
    const count = Math.max(1, Math.min(20, autoNum(p, "count" + i) || AUTO_DEF_JOBS[i].count));
    if (model) jobs.push({ model, count,
      min_minutes: Math.max(0.5, autoNum(p, "minmin") || AUTO_DEF_MIN),
      max_minutes: Math.max(0.5, autoNum(p, "maxmin") || AUTO_DEF_MAX) });
  }
  if (!jobs.length) return setErr("至少留一个模型出片");
  const save_dir = autoVal(p, "savedir");
  if (needSaveDir && !save_dir) return setErr("先填或选一个成片保存文件夹");
  const bgm = autoOn(p, "bgm");
  const bgm_dir = autoVal(p, "bgmdir");
  if (bgm && !bgm_dir) return setErr("勾了配乐，就先选一个放着多首歌的音乐文件夹");
  const rand = autoOn(p, "rand");
  return {
    jobs, order: "chrono", eps: [], save_dir,
    fx: {
      auto_fx: rand,
      src_ver: rand ? "nosub" : "",
      mirror: rand, first_mirror: true, first_mirror_dir: "v",
      grade: "rand", zoom: true, buffer_speed: true,
      voice_iso: autoOn(p, "voiceiso"),
      bgm, bgm_mode: "folder", bgm_dir,
    },
  };
}

/* 用 cfg 回填面板（记住上次）。cfg 为空时用默认值。 */
function autoFillCfg(p, cfg) {
  cfg = cfg || {};
  const jobs = cfg.jobs || [];
  for (let i = 0; i < AUTO_DEF_JOBS.length; i++) {
    const j = jobs[i] || AUTO_DEF_JOBS[i];
    const sel = autoE(p, "model" + i);
    if (sel) sel.value = j.model || AUTO_DEF_JOBS[i].model;
    const c = autoE(p, "count" + i);
    if (c) c.value = j.count || AUTO_DEF_JOBS[i].count;
  }
  const mn = autoE(p, "minmin"), mx = autoE(p, "maxmin");
  if (mn) mn.value = jobs[0] && jobs[0].min_minutes ? jobs[0].min_minutes : AUTO_DEF_MIN;
  if (mx) mx.value = jobs[0] && jobs[0].max_minutes ? jobs[0].max_minutes : AUTO_DEF_MAX;
  const fx = cfg.fx || {};
  const rand = autoE(p, "rand");
  if (rand) rand.checked = fx.auto_fx !== false;   // 默认开
  const sd = autoE(p, "savedir");
  if (sd) sd.value = (cfg.save_dir || "").replace(/\\/g, "/");
  let lastBgm = "";
  try { lastBgm = localStorage.getItem("hs_bgm_dir") || ""; } catch (e) {}
  const bgm = autoE(p, "bgm"), bd = autoE(p, "bgmdir");
  if (bgm) { bgm.checked = fx.bgm !== false; bgm.dispatchEvent(new Event("change")); }  // 默认开，显式关才尊重
  if (bd) bd.value = fx.bgm_dir || lastBgm;
  // 去掉原片自带音乐（2026-11-21 加进面板）：老配置没这个字段时默认开，跟出片面板一致
  const vi = autoE(p, "voiceiso");
  if (vi) vi.checked = fx.voice_iso !== false;
  const e = autoE(p, "err"); if (e) e.textContent = "";
}

/* 绑定面板交互：配乐勾选显示/隐藏文件夹、两个选文件夹按钮。 */
function autoBind(p) {
  const bgm = autoE(p, "bgm");
  if (bgm) bgm.addEventListener("change", () => {
    const wrap = document.querySelector(`[data-autobgmwrap="${p}"]`);
    if (wrap) wrap.classList.toggle("hidden", !bgm.checked);
  });
  const pickSave = document.querySelector(`[data-autosavedirpick="${p}"]`);
  if (pickSave) pickSave.addEventListener("click", async () => {
    try {
      const init = autoVal(p, "savedir");
      const r = await api(`/api/pick-folder?initial=${encodeURIComponent(init)}&key=addvideos_dir`
        + `&title=${encodeURIComponent("选择成片保存文件夹")}`, { method: "POST" });
      if (r && r.path) { const e = autoE(p, "savedir"); if (e) e.value = r.path; toast("成片保存文件夹选好了"); }
    } catch (e) { toast("也可以直接把文件夹路径填进输入框"); }
  });
  const pickBgm = document.querySelector(`[data-autobgmdirpick="${p}"]`);
  if (pickBgm) pickBgm.addEventListener("click", async () => {
    try {
      const init = autoVal(p, "bgmdir");
      const r = await api(`/api/pick-folder?initial=${encodeURIComponent(init)}&key=bgm_dir`
        + `&title=${encodeURIComponent("选择音乐文件夹")}`, { method: "POST" });
      if (r && r.path) {
        const e = autoE(p, "bgmdir"); if (e) e.value = r.path;
        try { localStorage.setItem("hs_bgm_dir", r.path); } catch (e2) {}
        toast("音乐文件夹选好了：每份成片从里面挑一首");
      }
    } catch (e) { toast("也可以直接把文件夹路径填进输入框"); }
  });
  const bd = autoE(p, "bgmdir");
  if (bd) bd.addEventListener("change", () => {
    try { localStorage.setItem("hs_bgm_dir", bd.value.trim()); } catch (e) {}
  });
}

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
  // 一键全自动：勾了就先读参数（含 BGM 必填校验），随建项目一起交给后端
  const wantAuto = !shotsOnly && !!( $("#in-autopipe") && $("#in-autopipe").checked );
  let autopipeCfg = null;
  if (wantAuto) {
    autopipeCfg = autoReadCfg("an_", true);
    if (!autopipeCfg) return toast($("#auto-new-params #an_err")?.textContent || "全自动参数没填好");
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
        ...(wantAuto ? { autopipe_cfg: autopipeCfg } : {}),
      }),
    });
    const reused = (p.episodes || []).filter((e) => (e.line_count === undefined
      ? (e.lines || []).length : e.line_count) > 0).length;
    if (wantAuto) {
      ST.autoCfg = autopipeCfg;   // 记住这次参数，下次新建预填
      toast(`已创建：${p.episodes.length} 集 — 全自动已启动，台词、底片、剪辑、出片会自动一路做完`);
    } else {
      toast(shotsOnly ? "已创建：在打开的页面里粘贴剧情素材，就能生成分镜提示词"
        : reused ? `已创建：${p.episodes.length} 集，其中 ${reused} 集台词直接复用——不用再整理了`
          : `已创建（批量混剪）：${p.episodes.length} 集 — 正在自动整理台词，完成后到第 3 步生成剪辑结构`);
    }
    await loadList();
    await openProject(p.id);
  } catch (e) {
    toast("创建失败：" + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = shotsOnly ? "创建" : "扫描并创建";
  }
});

/* 项目页「一键全自动」：设置展开、启动、状态回填。 */
$("#auto-proj-toggle").addEventListener("click", () => {
  const box = $("#auto-proj-params"), btn = $("#auto-proj-toggle");
  if (!box) return;
  const show = box.classList.toggle("hidden") === false;   // toggle 后是隐藏就 show=false
  btn.textContent = show ? "收起 ▴" : "设置 ▾";
  if (show && !box.dataset.ready) {
    box.innerHTML = autoParamsHTML("ap_");
    autoBind("ap_");
    box.dataset.ready = "1";
  }
  if (show) {
    const saved = (ST.proj && ST.proj.autopipe && ST.proj.autopipe.cfg) || ST.autoCfg;
    autoFillCfg("ap_", saved);
  }
});
$("#btn-autopipe").addEventListener("click", async () => {
  const p = ST.proj; if (!p) return;
  // 面板没展开就先展开并填好（用项目存的 cfg，没有就用全局上次）
  const box = $("#auto-proj-params");
  if (box && !box.dataset.ready) {
    box.innerHTML = autoParamsHTML("ap_");
    autoBind("ap_");
    box.dataset.ready = "1";
    box.classList.remove("hidden");
    const saved = (p.autopipe && p.autopipe.cfg) || ST.autoCfg;
    autoFillCfg("ap_", saved);
    const tg = $("#auto-proj-toggle"); if (tg) tg.textContent = "收起 ▴";
  }
  const cfg = autoReadCfg("ap_", false);
  if (!cfg) return toast($("#ap_err")?.textContent || "全自动参数没填好");
  // 启动前必须确认（2026-11-21 用户要求）：一键全自动会一路跑到底，中途要调 AI、占满
  // CPU（出片时电脑会卡），跑完可能几十分钟。点错一次就是一整部剧的成本，先让它看清楚
  // 要跑几套、什么模型、存到哪。取消就什么都不发生，参数原样留在面板上。
  const total = cfg.jobs.reduce((n, j) => n + j.count, 0);
  // 模型写下拉里的真实名字（面板上显示什么就写什么），别用「快速/精细」那种档位名
  const jobLine = cfg.jobs.map((j) => `${j.model} ${j.count} 套`).join("、");
  const j0 = cfg.jobs[0] || {};
  const mins = j0.min_minutes ? `${j0.min_minutes}-${j0.max_minutes} 分钟` : "";
  const go = await askConfirm({
    title: "一键全自动：跑完整个流程？",
    body: [
      "会从整理台词一路做到出片，中间不用你点：整理台词 → 抹原字幕 → 生成剪辑结构 → 出片。",
      `模型：${jobLine}，共 ${total} 套${mins ? `，每套 ${mins}` : ""}。`,
      cfg.save_dir ? `成片存到：${cfg.save_dir}` : "成片存到项目自己的默认目录。",
      "跑的过程中会调 AI、占电脑（出片时比较卡），跑完前别关这个窗口。",
    ],
    okText: "开始全自动",
  });
  if (!go) return;
  const errBox = $("#auto-proj-error");
  if (errBox) errBox.classList.add("hidden");
  const btn = $("#btn-autopipe");
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = "启动中…";
  try {
    await api(`/api/projects/${p.id}/autopipe`, {
      method: "POST", body: JSON.stringify({ cfg }),
    });
    ST.autoCfg = cfg;
    ST.proj = await api("/api/projects/" + p.id);
    render(); startPoll();
    toast("全自动已启动：台词 → 底片 → 剪辑 → 出片会自动一路做完");
  } catch (e) {
    if (errBox) { errBox.textContent = e.message || "启动失败"; errBox.classList.remove("hidden"); }
    toast("启动失败：" + (e.message || ""));
  } finally { btn.disabled = false; btn.textContent = old; }
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
  writeFx("fx-", (ST.proj.batch || {}).fx_default || null, { defaultMirror: true });
  // 全自动参数面板按项目重填：清掉 ready 标记，下次展开时读这个项目的 cfg
  const apBox = $("#auto-proj-params");
  if (apBox) { delete apBox.dataset.ready; }
  const apErr = $("#auto-proj-error"); if (apErr) apErr.classList.add("hidden");
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
  transcribe: "正在整理台词", plan: "AI 正在编排结构", planadd: "AI 正在追加新套",
  analyze: "正在让 AI 出结构表", render: "正在出片", batch: "正在批量出片",
  shots: "AI 正在写镜号提示词", nosub: "正在生成干净底片",
  autopipe: "全自动进行中",
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
  const cands = [[p.task, "cuts"], [p.task_nosub, "nosub"], [p.task_shots, "shots"],
    [p.task_planadd, "planadd"]];
  const want = { "0": "nosub", "3": "shots" }[ST.step] || "cuts";
  const pick = (fn) => {
    const hit = cands.find(fn);
    return hit ? { task: hit[0], group: hit[1] } : null;
  };
  return pick(([t, g]) => g === want && taskBusy(t))
      || pick(([t]) => taskBusy(t))
      || { task: p.task || p.task_nosub || p.task_shots || p.task_planadd || null, group: "cuts" };
}

/* 四条产线各自记一个「已发中止」：任务真正停下（状态清空）后才解锁按钮，
   避免连点；也别因为另一条还在跑就把这一条的按钮一直锁着。 */
const _cancelSent = { cuts: false, nosub: false, shots: false, planadd: false };
/* 中止后的反馈条：状态条不能「啪一下就没了」，只剩一个 toast 抓不住
   （2026-11-14 用户反馈）。留 60 秒的结果反馈，期间有新任务就自然覆盖。
   2026-11-16：原来只有一个全局槽，别的产线还在跑时会被无条件清掉（跨组并行
   是常态），用户就看不到「已中止」。改成按产线各记一份，并带上被中止那一趟
   任务的 started，用来分辨「同一趟在收尾」和「用户又点了生成」。 */
const _abortNote = { cuts: null, nosub: null, shots: null, planadd: null };

/* 顶部状态条：有任务时显示「在做什么 + 进度 + 已用时间 + 预计还需」，没有就收起 */
function updateBar(t, group) {
  const bar = $("#gbar");
  const stop = $("#btn-task-cancel");
  const key = group || "cuts";
  const note = _abortNote[key];
  const fresh = !!note && Date.now() - note.at < 60000;
  if (!fresh) _abortNote[key] = null;
  if (!taskBusy(t)) {
    if (fresh) {
      bar.classList.remove("hidden");
      $("#gbar-fill").style.width = "100%";
      $("#gbar-text").textContent = "已中止 · 已经做好的部分都保留，想接着做重新点一次就行";
      if (stop) { stop.disabled = true; stop.textContent = "中止"; }
      return;
    }
    bar.classList.add("hidden");
    return;
  }
  // 刚点过中止、后端还在收尾：别再把旧百分比和「预计还需」摆出来，那看起来
  // 跟完全没反应一样。started 变了说明用户已经重新发起了这一条产线。
  const sameTask = fresh && (!note.started
    || Number(t.started || 0) === Number(note.started));
  if (sameTask) {
    bar.classList.remove("hidden");
    $("#gbar-fill").style.width = "100%";
    $("#gbar-text").textContent = "正在收尾，马上停…";
    if (stop) { stop.disabled = true; stop.textContent = "中止中…"; }
    return;
  }
  if (fresh) _abortNote[key] = null;   // 这条产线又起了新任务，旧反馈让位
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
  const ok = await askConfirm({
    title: "中止这一步？",
    body: [
      "已经做好的部分都会保留，没跑完的不会继续。",
      "想接着做，重新点一次就行；正在处理的那一段会立刻停下。",
    ],
    okText: "中止这一步",
    cancelText: "继续跑",
    danger: true,
  });
  if (!ok) return;
  const curTask = { cuts: p.task, nosub: p.task_nosub, shots: p.task_shots }[key];
  _cancelSent[key] = true;
  btn.disabled = true;
  btn.textContent = "中止中…";
  // 点下去的这一刻就要有可见变化，不能等下一拍轮询（2026-11-16 用户反馈：
  // 「点了中止之后没有刷新状态」）。
  _abortNote[key] = { at: Date.now(), started: Number((curTask || {}).started || 0) };
  render();
  try {
    const r = await api(`/api/projects/${p.id}/task/cancel`, {
      method: "POST", body: JSON.stringify({ group }),
    });
    if (!(r.cancelled || []).length) {
      _cancelSent[key] = false;
      _abortNote[key] = null;
      toast("这一步刚好已经结束了");
    } else {
      toast("已中止，正在收尾；已经做好的部分都还在");
    }
  } catch (e) {
    _cancelSent[key] = false;
    _abortNote[key] = null;
    toast(e.message || "没能中止，请再试一次");
  } finally {
    // 三条路径都立刻拉一次真实状态：原来只靠 1.5 秒轮询，而轮询空闲 3 拍
    // 就自动停掉，界面会永远停在旧进度上。
    try {
      const freshProj = await api("/api/projects/" + p.id);
      // 请求期间用户可能已经切到别的项目，别把旧项目的数据盖回来
      if (ST.proj && ST.proj.id === p.id) ST.proj = freshProj;
    } catch (e) { /* 拉不到就交给轮询兜底 */ }
    render();
    startPoll();
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
    items.push({ key: "0", no: "2", label: "抹掉原字幕", sub: "做一次，多套共用",
      state: nsAll ? "done" : "todo",
      txt: nsAll ? "已做好" : nsDone ? `已做 ${nsDone}/${eps.length} 集` : "未做" });
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

/* 项目页「一键全自动」卡片：显示阶段、失败原因 + 重试。失败信息来自
   p.autopipe.last_error（本地完整数据，不读被裁剪的字段）。 */
function renderAutoState(p) {
  const card = $("#auto-proj-card"); if (!card) return;
  card.classList.toggle("hidden", p.mode !== "batch");
  const ap = p.autopipe || {};
  const st = $("#auto-proj-state"), err = $("#auto-proj-error");
  const btn = $("#btn-autopipe");
  const busyTask = taskBusy(p.task) && p.task.kind === "autopipe";
  const STAGE_TXT = {
    init: "准备开始", transcribe: "整理台词中", nosub: "做干净底片中",
    plan: "生成剪辑中", done: "已完成", error: "出错了",
  };
  if (busyTask) {
    if (st) st.textContent = `${p.task.pct || 0}% · ${p.task.msg || "全自动进行中"}`;
  } else if (ap.done) {
    if (st) st.textContent = "✓ 全自动已完成：台词、底片、剪辑、成片都好了";
  } else if (ap.last_error) {
    if (st) st.textContent = "上次没跑完（可重试）";
  } else if (ap.stage) {
    if (st) st.textContent = `上次停在「${STAGE_TXT[ap.stage] || ap.stage}」，可重试接着做`;
  } else {
    if (st) st.textContent = "";
  }
  if (err) {
    if (ap.last_error && !busyTask) { err.textContent = "没能跑完：" + ap.last_error + "。改好后点「重试」会接着做，已完成的不会重做。"; err.classList.remove("hidden"); }
    else err.classList.add("hidden");
  }
  if (btn) btn.textContent = (ap.last_error || ap.stage) && !ap.done ? "重试全自动" : "一键全自动";
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
  renderAutoState(p);
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
    const addT = p.task_planadd;
    setState("#b2-state", addT && taskBusy(addT) ? `追加中 ${addT.pct}% · ${addT.msg}`
      : t && t.kind === "plan" ? `${t.pct}% · ${t.msg}`
      : plans.length
        ? (hookMode ? `已出 ${plans.length} 集钩子` : `已出 ${plans.length} 套结构`)
        : "未开始");
    const curGen = Number((p.batch && p.batch.gen) || p.plan_gen || 0);
    const sets = (p.sets || []).filter((s) => Number(s.gen || 0) === curGen);
    const fails = (p.batch || {}).failures || [];
    // 「已出片几套」只在 #b3-tally（项目口径）里说一次：这里再说一遍就成了
    // 两行同样的数字。这一行只负责「在跑 / 失败 / 未开始」。
    setState("#b3-state", t && t.kind === "batch" ? `${t.pct}% · ${t.msg}`
      : fails.length
        ? `${fails.length} ${hookMode ? "条" : "套"}出片失败，下面标了原因`
        : (p.plans || []).length || sets.length ? "" : "未开始");
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

  // 2026-11-19 用户定（两条硬规矩）：
  //  ① 正在整理台词时，「重新整理台词」必须禁用、且按钮上直接写明在整理中；
  //  ② 没在整理时才可点，点了就是从第一集 whisper 听音重转（force），不是从校对开始。
  // 校对是这条链的最后一步，一样算「正在整理」，一样禁点，不能让人中途再发起一次。
  const transRunning = !!(t && t.kind === "transcribe" && taskBusy(t));
  const btnTrans = $("#btn-transcribe");
  btnTrans.disabled = busy;   // 任何 cuts 产线任务在跑都禁点，不止 transcribe
  btnTrans.textContent = transRunning ? "台词整理中…" : "重新整理台词";
  // 「添加视频」一键任务占 cuts + nosub 两组，任一组在跑都禁点，文案写明进度
  const addRunning = !!(t && t.kind === "addvideos" && taskBusy(t));
  const btnAdd = $("#btn-addvideos");
  if (btnAdd) {
    btnAdd.disabled = busy || nsBusy;
    btnAdd.textContent = addRunning ? "添加并整理中…" : "添加视频";
  }
  $("#transcript-links").classList.toggle("hidden", !p.line_total);
  // 校对清单属于上一个项目就收起：换了项目还挂着旧清单，会让人以为是这个项目的结果
  if (PROOF_S1 && PROOF_S1.st.boundPid !== p.id) {
    PROOF_S1.st.boundPid = p.id;
    PROOF_S1.show(false);
    PROOF_S1.reset();
  }
  $("#btn-transcript-dl").href = `/api/projects/${p.id}/transcript?download=1`;
  // 删项目要等三条产线都停：有任务在跑时删掉，后台任务会往不存在的目录里写。
  $("#btn-del").disabled = busy || nsBusy || shotBusy;

  if (isBatch) {
    // 参数表单只在「进入这个项目」时按 b.jobs 重建一次（initPlanModels，由
    // planBuiltFor 守卫）；生成 / 轮询期间绝不回填或重建，否则会把用户刚改的
    // 参数覆盖回上一轮的值，或把多组参数错乱成一堆重复行。
    if (planBuiltFor !== p.id) initPlanModels(p);
    // 追加式走 planadd 组、可与出片并行，出片忙（cuts busy）时仍允许点；
    // 覆盖式仍受 cuts busy 约束（会和出片抢 plans/gen）。
    $("#btn-plan").disabled = (planIsAppend() ? taskBusy(p.task_planadd) : busy) || !done;
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

/* ---------- 台词整理之后的一步：抹掉原字幕（2026-11-11 起，2026-11-16 重组）
   逐集把画面上的原字幕抹干净，产出「没有字幕的底片」；台词在出片时才烧上去，
   所以底片一份多套共用，镜像也不会把字翻反。整步不做也能出片（选「原始视频」）。

   界面上只剩两项：抹除柔和程度、抹掉的地方怎么补（都是「抹得干不干净」），
   外加字幕时间自动对齐。
   **字幕样式（动效/幅度）2026-11-16 搬到出片第 5 组**：这一步的产物不含字，
   留在面板上改了也不会有任何变化，等于骗人。
   其余（做法、字幕位置、要不要加字幕）都已固定，mode/auto/newsub 写死。 */
const NS_DEF = { mode: "soft", auto: true, pos: 0.75, size: 0.10, soft: 0.5,
                 fill: "above", fill_mode: "mirror",
                 newsub: true, newsub_pos: 0.80, newsub_motion: 0.5,
                 newsub_style: "rise", retime: true };
/* 新字幕出场动效。跟后端 render.NEWSUB_STYLES 一一对应，
   首字母参与设置指纹（改风格就得重做副本）。 */
const NS_STYLES = ["fade", "rise", "pop", "float", "type"];
/* 抹除引擎版本，必须与后端 nosub.sig_of 里的前缀同步（后端现在写的是
   `E{subdetect.VERSION}`，所以这里要跟着 subdetect.VERSION 改）。
   忘了同步的话 cfg_sig 永远对不上，界面会一直说「改过设置、要重做」。
   E4 = 每集按自己的字幕带中心烧新字幕（多来源项目不再跳位）。
   E5 = 短片段项目不再被判成「没字幕」。
   E6 = 画面运动的碎帧不再留洞（原字幕闪一下才消失）。
   E7 = 大分辨率长片不再 OOM 导致 by_ep 空。
   E8 = 双行字幕连通扩展（救第二行下半截抹不掉）。
   E9 = 扩展改成「只并强字行段」，白衬衫/下巴不再被缝进字幕带。
   E10 = 逐句框只认字幕带核心区内的强段，浅色桌面不再被当字抹掉下半屏。 */
const NS_ENGINE = "E10";
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
    // 字幕样式已搬到出片面板：这里沿用项目里存的值，控件不在了就原样传回去，
    // 免得每次保存都把它重置成出厂值（顺手改了用户上次选的动效）。
    newsub_motion: Math.min(1, Math.max(0, Number(saved.newsub_motion) || 0.5)),
    newsub_style: NS_STYLES.indexOf(saved.newsub_style) >= 0 ? saved.newsub_style : "rise",
    // 对时已挪到第 1 步转写末尾（2026-11-19），这里恒 true 只为兼容旧 cfg/指纹，
    // 第 2 步读它不再触发任何对时动作。
    retime: true,
  };
}

/* 跟后端 nosub.sig_of(cfg)（只算设置那一半）同一套算法：用来判断
   「现在面板上的设置，和已经生成的那份副本是不是同一版」。不一致就提示重做。
   c 必须是**完整**的 cfg（pos/size/fill/newsub_pos 都在），因为识别出来的
   新字幕位置也算在指纹里；只拿面板上的三个值算会永远对不上。
   |N5 的 5 是出场参数版本（先后做过：放大动效幅度、加「字幕比声音晚」、
   删掉它、烧字去标点），两处必须同步，否则面板永远认为「没改过」。 */
function nsSig(c) {
  return `${NS_ENGINE}F${Math.round(c.pos * 1000)}${Math.round(c.size * 1000)}`
    + `${Math.round(c.soft * 100)}${c.fill === "below" ? "d" : "u"}`
    + `${c.fill_mode === "borrow" ? "B" : "M"}`
    + `|N5${Math.round(c.newsub_pos * 1000)}${Math.round(c.newsub_motion * 100)}`
    + `${(c.newsub_style || "rise").charAt(0)}`;
}

/* 面板上只剩「抹得干不干净」两项 + 时间对齐开关。位置、带高、从哪边补都不显示——
   识别是机器的事，结果只落在成品里；原理放在「这一步会做什么」里讲。 */
function nsSync() {
  const c = readNoSubCfg();
  const n = Math.round(c.soft * 100);
  setText("#ns-softv", n < 25 ? "偏清晰" : n < 75 ? "中等" : "更柔");
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
  // 能动的只有「柔和程度 / 动效幅度 / 出场动效 / 补法」四项。位置、带高、填充方向
  // 都是识别出来的，所以拿上一次那份完整 cfg 当底、只把这几项盖上去再算
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

  const failed_task = !busy && t && t.error && t.kind === "nosub";
  const state = busy ? `${Math.round(t.pct || 0)}% · ${t.msg}`
    : failed_task ? "生成失败，请看下方红条"
      : done >= total && total ? `已做好 ${done} 集`
        : done ? `已做 ${done}/${total} 集` : "还没做，可跳过";
  setText("#s0-state", state);
  const stEl = $("#s0-state");
  if (stEl) stEl.classList.toggle("bad", !!failed_task);
  // 失败原因别只塞标题旁的小灰字——卡片顶部给一条醒目红条，写全错误 + 怎么办。
  const errEl = $("#s0-error");
  if (errEl) {
    if (failed_task) {
      const detail = String(t.error || "");
      errEl.innerHTML = "生成干净底片失败，可重试一次；反复失败把下面这行发给作者"
        + `<span class="se-detail">${esc(detail)}</span>`;
      errEl.classList.remove("hidden");
    } else {
      errEl.classList.add("hidden");
    }
  }

  let note = "";
  if (busy) note = "正在生成；可以切到第 3 步同时出片（那边「用哪份视频」要选原始视频）。";
  else if (done && changed) note = `现有这 ${done} 集是按旧设置做的，改过设置了——点生成会按新设置重做一遍。`;
  else if (done && failed) note = `已做好 ${done} 集，还有 ${failed} 集没做成，点生成会补做。`;
  else if (done) note = `已做好 ${done} 集。设置没变时再点生成只会补没做过的集；改过台词后不用重做底片，出片时会按新台词重烧字幕。`;
  else note = "这一步可以跳过；想用「原字幕换成自己台词」的视频，随时回来生成。";
  setText("#ns-state", note);

  const btn = $("#btn-nosub-run");
  if (btn) {
    btn.disabled = busy || taskBusy(t) || total === 0;
    btn.textContent = done && changed ? "按新设置重做"
      : done && failed ? "补做没成功的集" : "生成干净底片";
  }
  const has = done > 0;
  $("#btn-nosub-preview").classList.toggle("hidden", !has);
  $("#btn-nosub-open").classList.toggle("hidden", !has);
}

/* 字幕时间对齐已挪到第 1 步转写末尾（2026-11-19 用户定），台词一出来时间就是准的。
   这一步（抹底片）不再有对时控件、状态或撤销按钮。 */

/* 人名统一交给台词弹窗的「整剧校对」；繁转简静默自动（zh.enabled() 恒 True）。
   两者都不再有单独的设置卡片和解释文案（2026-11-19 用户定），对应 JS 一并移除。 */

["ns-soft", "ns-fillmode"].forEach((id) => {
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
    toast("开始生成干净底片，进度在页面顶部");
  } catch (e) {
    toast(e.message);
  }
});

$("#btn-nosub-preview").addEventListener("click", async () => {
  const p = ST.proj;
  if (!p) return;
  try {
    await api(`/api/projects/${p.id}/nosub/preview`, { method: "POST" });
    toast("已用系统播放器打开第一集底片");
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
      ${s.rating == null ? "" : `<span class="pc-rate r${Math.min(10, Math.max(1, Math.round(s.rating)))}">${Math.round(s.rating)}分</span>`}
      <span class="pc-label">${esc(s.label || "")}</span>
    </div>`).join("");
  return `
    <div class="plan ${pl.on === false ? "off" : ""}" data-i="${i}">
      <div class="plan-head">
        <input type="checkbox" ${pl.on === false ? "" : "checked"}>
        <b>第 ${pl.ep} 集</b>
        ${pl.model ? `<span class="ov info" title="这一集用的生成方式">${esc(modelLabel(pl.model))}</span>` : ""}
        <span class="hint">钩子 ${fmt(he - hs)}（原片 ${fmt(hs)}–${fmt(he)}）+ 正片 ${fmt(pl.ep_dur || 0)} · 成片 ${fmt(pl.sec || 0)}</span>
        <span class="ov plan-fx ${pl.fx ? "info" : ""}" data-i="${i}" title="这一集的出片与画面设置：去重 / 配乐 / 调色 / 用哪份视频都能改；不改就跟随这批的默认">✎ 设置 · ${esc(pl.fx ? fxShort(pl.fx) : "跟随默认")}</span>
        <span class="plan-more">展开表 ▾</span>
      </div>
      <div class="plan-body hidden">
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
  // 「保留已有套，新的接在后面」只在真有套时才需要露面：第一次生成时它没有意义
  const apWrap = $("#plan-append-wrap");
  if (apWrap) apWrap.classList.toggle("hidden", !plans.length);
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
    renderPlanShortfall();
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
    // 批号：补跑会往同一批里加套，批标题上的「N 套」要跟着变
    pl.batch_no || 0,
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
    list.innerHTML = planListHtml(plans);
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
      // 片头大字输入框 2026-11-21 随片头组一起删了（钩子卡没有开关可开，填了也不会出卡）
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
  // 选集格子每拍对一次选中态：台词整理完 / 换项目后集数会变，chips 靠自身 sig
  // 决定要不要重建，没变就只刷高亮，不打断用户正在点的东西。
  renderPlanEps();
  updatePlanFeasible();
  updatePlanStat();
  updatePlanFxChips();
  syncHookFxLocks();
  renderPlanShortfall();
}

/* ---------- 没生成成功的组：提示条 + 只重跑这些组（2026-11-16 A 档） ----------
   只在**有组没出来**时出现。按钮把上次失败组的原参数原样发回去，后端 append=True
   把新套接在现有结构后面；已有的套、勾选状态和出片进度都不动。 */
let shortfallPending = false;

function renderPlanShortfall() {
  const el = $("#plan-shortfall");
  if (!el) return;
  const p = ST.proj;
  const r = (p && p.batch && p.batch.plan_result) || {};
  const failed = Array.isArray(r.failed) ? r.failed : [];
  if (!failed.length) {
    el.classList.add("hidden");
    el._sig = "";
    return;
  }
  el.classList.remove("hidden");
  const busy = shortfallPending || taskBusy(p && p.task_planadd);
  const want = failed.reduce((a, f) => a + (Number(f.count) || 0), 0);
  const keep = (p && p.plans && p.plans.length) || 0;
  const asked = Number(r.requested) || want;
  const got = Number(r.got) || 0;
  const sig = JSON.stringify([asked, got, r.total, r.append, r.legacy, failed, busy]);
  const when = r.legacy ? "上次" : "这次";
  const items = failed.map((f) =>
    `<li><b>${esc(f.label || modelLabel(f.model))}</b> · 要 ${esc(f.count)} 套 — ${esc(f.reason || "没返回结果")}</li>`).join("");
  const btnTxt = busy ? "正在生成…" : `重新生成没出来的 ${want} 套`;
  patchHtml(el, sig, () => `
    <h4>${when}要 ${esc(asked)} 套，只出来 ${esc(got)} 套（现在一共 ${esc(keep)} 套）</h4>
    <p class="sf-why">有 ${esc(failed.length)} 组没生成成功。没出来的套可以单独再生成一次，已有的 ${esc(keep)} 套不会动。</p>
    <ul>${items}</ul>
    <div class="sf-act">
      <button class="primary" id="btn-plan-retry" ${busy ? "disabled" : ""}>${esc(btnTxt)}</button>
      <span class="sf-why">${busy ? "生成完会自己刷新，不用守着" : `会用到 ${esc(failed.length)} 次今日生成机会`}</span>
    </div>`);
}

$("#plan-shortfall").addEventListener("click", async (e) => {
  if (!e.target.closest("#btn-plan-retry") || shortfallPending) return;
  const p = ST.proj;
  if (!p) return;
  const failed = Array.isArray((p.batch || {}).plan_result && p.batch.plan_result.failed)
    ? p.batch.plan_result.failed : [];
  if (!failed.length || taskBusy(p.task_planadd)) return toast("正在补跑，等它结束再试");
  const jobs = failed.map((f) => ({
    model: f.model || "", count: Math.max(1, Math.min(20, Number(f.count) || 1)),
    min_minutes: Number(f.min_minutes) || 3, max_minutes: Number(f.max_minutes) || 5,
  }));
  const want = jobs.reduce((a, j) => a + j.count, 0);
  const keep = (p.plans || []).length;
  const ok = await askConfirm({
    title: "只生成没出来的套",
    body: [
      `上次有 ${failed.length} 组没生成成功，这次只重跑这 ${failed.length} 组、共 ${want} 套。`,
      `已有的 ${keep} 套结构和已经出好的片都不会动，新套接在后面。`,
      `会用到 ${failed.length} 次今日生成机会，预计约 ${planEta(failed.length)} 分钟。`,
      "没生成成功多是因为等太久，也可以先在「生成设置」里把套数或单套时长调小再试。",
    ],
    okText: `重新生成这 ${want} 套`,
  });
  if (!ok) return;
  shortfallPending = true;
  renderPlanShortfall();
  try {
    const grant = await aiGrant(p.id, "plan");
    // 补跑必须沿用那一批的选集：不带 eps 会按全集重排，出来的套跟原批不是一回事。
    await api(`/api/projects/${p.id}/plan`, {
      method: "POST",
      headers: { "X-AI-Grant": grant },
      body: JSON.stringify({
        jobs, order: FIXED_ORDER, append: true,
        eps: ((p.batch || {}).plan_eps) || [], new_batch: false,
      }),
    });
    ST.proj.task_planadd = { kind: "planadd", pct: 0,
      msg: `正在生成没出来的 ${want} 套`, error: "" };
    render();
    startPoll();
    toast(`正在生成没出来的 ${want} 套，已有的 ${keep} 套不动`);
  } catch (err) {
    toast(err.message);
  } finally {
    shortfallPending = false;
    renderPlanShortfall();
  }
});

/* 套列表：按「批」分组。同一部剧可以反复分批生成（这批勾 1-8 集、下批勾 5-11 集），
   十几套平铺在一起看不出哪套是哪次排的、用的哪些集。
   老项目没有 batch_no，照旧平铺、不插标题（不凭空造批次）。 */
function planListHtml(plans) {
  let last = null;
  let legacyTitled = false;
  const hasBatch = plans.some((x) => Number(x.batch_no || 0));
  const out = [];
  plans.forEach((pl, i) => {
    const bn = Number(pl.batch_no || 0);
    // 老套没有批号：混在新批中间会看不出哪套是哪次排的，给它们一个总标题
    if (pl.mode !== "hook" && !bn && hasBatch && !legacyTitled) {
      legacyTitled = true;
      out.push(`<div class="plan-batch"><b>之前生成的套</b>`
        + `<span>没记批次</span><i>这批之前的旧结构，出片照旧</i></div>`);
    }
    if (pl.mode !== "hook" && bn && bn !== last) {
      last = bn;
      const n = plans.filter((x) => Number(x.batch_no || 0) === bn).length;
      const brief = epsBrief(pl.batch_eps || []);
      out.push(`<div class="plan-batch"><b>第 ${bn} 批</b>`
        + `<span>${n} 套${brief ? ` · 用 ${esc(brief)}` : ""}</span>`
        + `<i>这一批只从这些集里挑片段</i></div>`);
    }
    out.push(planCard(pl, i));
  });
  return out.join("");
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
      ${s.rating == null ? "" : `<span class="pc-rate r${Math.min(10, Math.max(1, Math.round(s.rating)))}">${Math.round(s.rating)}分</span>`}
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
        <span class="ov plan-fx ${pl.fx ? "info" : ""}" data-i="${i}" title="这一套单独的出片与画面设置：去重 / 配乐 / 调色 / 变形 / 特效 / 用哪份视频都能改；不改就跟随这批套的默认">✎ 设置 · ${esc(pl.fx ? fxShort(pl.fx) : "跟随默认")}</span>
        <span class="plan-more">展开表 ▾</span>
      </div>
      <div class="plan-body hidden">
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
  const busy = !!(ST.proj && taskBusy(ST.proj.task));
  // 前面有出片在跑时不禁用：这趟会走 enqueueRender 排队，跑完自动开始
  // （和单套出片一致）。只有「没勾选」才禁点。排队中再点会替换上一趟排队请求。
  const queued = !!(ST.proj && ST.pendingRender && ST.pendingRender.pid === ST.proj.id);
  btn.textContent = queued ? "已排队·再点可改"
    : hook ? `出片（已选 ${on.length} 集）` : `出片（已选 ${on.length} 套）`;
  btn.disabled = !on.length;
}

async function planSelectAll(on) {
  const p = ST.proj;
  const plans = p.plans || [];
  if (!plans.length) return;
  plans.forEach((pl) => (pl.on = on));
  // renderPlans 的重建签名刻意不含 on（为的是 1.5s 轮询时不合上用户展开的详情），
  // 所以这里必须手动同步复选框和 off 样式——否则数据变了、统计文字也变了，
  // 但下面一排复选框看着没反应（2026-11-21 用户反馈「全选/全不选不起作用」）。
  // 不重建 DOM，用户展开的那几套保持展开。
  document.querySelectorAll("#plan-list .plan").forEach((row) => {
    const cb = row.querySelector('input[type="checkbox"]');
    if (cb) cb.checked = on;
    row.classList.toggle("off", !on);
  });
  updatePlanStat();
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
    toast("已打开成片所在的文件夹");
  } catch (e) {
    toast(`打不开文件夹：${e.message}`);
  }
});

/* 用系统默认播放器打开某一套（不在浏览器里开页面） */
$("#sets-list").addEventListener("click", async (e) => {
  const play = e.target.closest("[data-play]");
  if (play) {
    try {
      await api(`/api/projects/${ST.proj.id}/open-video?set=${play.dataset.play}`, { method: "POST" });
    } catch (err) {
      toast(err.message);
    }
    return;
  }
  // 单套出片（新追加的套 / 列表里还没出片的那一行）：用当前出片设置，只渲这一套。
  const one = e.target.closest("[data-render]");
  if (!one) return;
  const p = ST.proj;
  if (!p) return;
  const idx = Number(one.dataset.render);
  const saveDir = ($("#save-dir").value || "").trim();
  if (!saveDir) return toast("先填一个成片保存位置（点「选择文件夹…」更快）");
  const fx = readFx("fx-");
  if (!(await confirmMirrorIfOff(fx))) return;
  const pos = (p.plans || []).findIndex((x) => Number(x.index) === idx);
  if (pos < 0) return toast("结构表里已经没有这一套了，先重新生成结构");
  const pl = p.plans[pos];
  // 出片进度列表按「全部结构」画，里面可能有没勾选的套。单套出片也要先勾上：
  // 后端 only 是在勾选之后才筛的，不勾会直接被 400 挡回来（"没有勾选任何结构"）。
  const wasOff = pl.on === false;
  const setsFx = pl.fx && Object.keys(pl.fx).length ? { [String(idx)]: pl.fx } : {};
  const body = { save_dir: saveDir, fx, sets_fx: setsFx, only: [idx] };
  if (!confirm(`只渲第 ${idx} 套（${pl.ai_name || "未命名"}），用当前出片设置。` +
      (wasOff ? "\n\n这一套当前没勾选，出片前会自动勾上。" : "") +
      (taskBusy(p.task) ? "\n\n前面还有一趟在跑，这趟会排队，跑完自动开始。" : "") + "\n\n确定？")) return;
  if (wasOff) await onPlanToggle(pos, true);
  if (!enqueueRender(p.id, body, `第 ${idx} 套已排队，前面出片跑完自动开始`)) return;
  try {
    await sendRender(p.id, body);
    toast(`第 ${idx} 套已开始出片`);
  } catch (err) {
    toast(err.message);
  }
});

/* 出片进度列表（2026-11-21 用户定稿）：这是**进度视图**，不是勾选视图。
   顺序固定：① 已出片 → ② 本趟在排队/正在出（只写文案，不给按钮）
   → ③ 还没出片（排最后，带「出片」按钮）。
   勾选只决定「下一趟出哪些」，列表里既不按它过滤、也不标它——以前按 planIdx
   过滤，取消勾选的已出片套整行消失，看着像片子丢了；反过来勾了没出的套又会
   被当成「待生成」，两种都读错。 */
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
  const plans = p.plans || [];
  const hookMode = !!(plans.length && plans[0].mode === "hook");
  const unitName = hookMode ? "集" : "套";
  const t = p.task || {};
  const runOn = t.kind === "batch" && !t.error;
  // 本趟队列由后端随进度一起报（job 顺序 = 结构顺序）：不在队列里 = 这一趟根本没有它
  const queue = (runOn && t.data && Array.isArray(t.data.queue)) ? t.data.queue : [];
  // 兜底（老后端 / 服务没重启时没有 data）：至少把 msg 里那套标成「正在出」，
  // 否则正在渲的套会混进「还没出片」并挂上一个出片按钮。
  const queueUnknown = runOn && !(t.data && Array.isArray(t.data.queue));
  if (runOn && !queue.length) {
    const m = String(t.msg || "").match(/第\s*(\d+)\s*[套集]/);
    if (m) queue.push({ index: Number(m[1]), state: "run", pct: null, msg: "" });
  }
  const qMap = {}, qPos = {};
  queue.forEach((r, i) => {
    const idx = Number(r.index);
    qMap[idx] = r;
    qPos[idx] = i + 1;
  });
  const fails = {};
  ((p.batch || {}).failures || []).forEach((f) => { fails[Number(f.index)] = f; });
  // 行来源 = 结构表 + 已有成片 + 本趟队列。队列里可能有结构表里没有的套号
  // （出片中场改过结构/勾选），那种也必须出现，否则「正在出」在列表里查无此人。
  const idxs = Array.from(new Set([
    ...plans.map((pl) => Number(pl.index)),
    ...Object.keys(cur).map(Number),
    ...queue.map((r) => Number(r.index)),
  ])).sort((a, b) => a - b);
  const doneRows = [], actRows = [], todoRows = [];
  idxs.forEach((i) => {
    const s = cur[i] || {};
    const q = qMap[i];
    if (q && q.state !== "done") actRows.push({ i, s, q });
    else if (s.output) doneRows.push({ i, s });
    else todoRows.push({ i, s, fail: fails[i] });
  });
  // 单集钩子前置的一「套」就是一集：列表里写「第 3 套」会让人以为还有第 4 套没出
  const labelOf = (i) => {
    const pl = plans.find((x) => Number(x.index) === i) || {};
    const tag = (cur[i] || {}).tag || pl.tag;
    return tag || `第 ${i} ${unitName}`;
  };
  const ready = doneRows.length;
  const total = idxs.length;
  const totalMb = doneRows.reduce((a, r) => a + (r.s.output_size || 0), 0) / 1048576;
  const bdir = p.batch && p.batch.save_dir ? joinDir(p.batch.save_dir, p.name) : "";
  const nRun = actRows.filter((r) => r.q.state === "run").length;
  const nWait = actRows.length - nRun;
  // 顶部口径分开写：项目一共出了多少 / 这一趟在跑多少。只写本趟的 0/N 会被读成
  // 「项目一套没出」——明明 13 套成片在下面列着。
  const tally = $("#b3-tally");
  if (tally) {
    const txt = total
      ? `项目已出片 ${ready}/${total} ${unitName}`
        // 队列明细只在这一趟的队列拿得到时才写：拿不到就只报项目口径，
        // 拿一条兜底出来的「正在出」硬凑「本趟 N 套」会编出不存在的数字。
        + (actRows.length && !queueUnknown
          ? `　·　本趟 ${actRows.length} ${unitName}：正在出 ${nRun}、排队 ${nWait}` : "")
        + (todoRows.length ? `　·　还没出 ${todoRows.length} ${unitName}` : "")
      : "";
    if (tally.textContent !== txt) tally.textContent = txt;
  }
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
  // 排队/正在出的那一句人话：排队说位置，正在出说这一套在干什么
  const actHint = (r) => (r.q.state === "run"
    ? (r.q.msg || "正在渲染")
    : `排队中，前面还有 ${Math.max(0, qPos[r.i] - 1)} ${unitName}`)
    + (r.s.output ? `（出完替换上一版）` : "");
  // 进度未知（老后端兜底出来的那一行）只说状态，不编一个百分比出来
  const pctText = (q) => (q.pct == null ? "正在出片" : `正在出片 ${Math.round(q.pct)}%`);
  const rowDone = (r) => `<div class="setrow">
      <b>${labelOf(r.i)}</b>
      <span class="badge">✓ 已出片</span>
      <span class="hint">${(r.s.edl || []).length} 段 · ${fmt(r.s.output_duration || 0)} · ${((r.s.output_size || 0) / 1048576).toFixed(0)}MB</span>
      <button class="ghost" data-play="${r.i}">用播放器打开 ↗</button>
    </div>`;
  // 这一趟已经排上了：只写状态，不给按钮（位置已经定了，点了也没意义）
  const rowAct = (r) => `<div class="setrow">
      <b>${labelOf(r.i)}</b>
      ${r.q.state === "run"
        ? `<span class="badge run" data-setpct="${r.i}">${pctText(r.q)}</span>`
        : `<span class="badge wait">等待出片</span>`}
      <span class="hint" data-setmsg="${r.i}">${esc(actHint(r))}</span>
    </div>`;
  const rowTodo = (r) => `<div class="setrow">
      <b>${labelOf(r.i)}</b>
      ${r.fail
        ? `<span class="hint bad">失败：${esc((r.fail.error || "").slice(0, 60))}</span>`
        : `<span class="hint">${queueUnknown ? "这一趟还在跑，跑完再单套补出" : "还没出片"}</span>`}
      ${(!queueUnknown || r.fail)
        ? `<button class="ghost" data-render="${r.i}">出片 ▸</button>` : ""}
    </div>`;
  // 进度百分比/当前动作每拍都在变：放进签名会让 1.5 秒轮询把列表整块重建，
  // 滚动位置弹回顶部（31 行 + 320px 高度必然在滚）。签名只留「分组和状态」，
  // 数字和文案另外就地改两个 span。
  const sig = [
    p.id, curGen, stale.length, bdir, queueUnknown ? 1 : 0,
    doneRows.map((r) => [r.i, r.s.output || "", r.s.output_size || 0].join(":")).join(","),
    actRows.map((r) => `${r.i}:${r.q.state}`).join(","),
    todoRows.map((r) => `${r.i}:${r.fail ? "F" : ""}`).join(","),
  ].join("//");
  const list = $("#sets-list");
  const keepTop = list ? list.scrollTop : 0;
  const built = (doneRows.length || actRows.length || todoRows.length)
    ? doneRows.map(rowDone).join("") + actRows.map(rowAct).join("") + todoRows.map(rowTodo).join("")
    : "";
  patchHtml(list, sig, () => built || `<div class="hint" style="padding:6px 0">还没有出片记录</div>`);
  if (list && list.scrollTop !== keepTop) list.scrollTop = keepTop;
  actRows.forEach((r) => {
    const pctEl = list && list.querySelector(`[data-setpct="${r.i}"]`);
    if (pctEl) {
      const pc = pctText(r.q);
      if (pctEl.textContent !== pc) pctEl.textContent = pc;
    }
    const msgEl = list && list.querySelector(`[data-setmsg="${r.i}"]`);
    if (msgEl) {
      const mt = actHint(r);
      if (msgEl.textContent !== mt) msgEl.textContent = mt;
    }
  });
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
    toast(ctl ? "已记下：下次生成会带上说明句" : "已记下：下次生成不带说明句");
    return;
  }
  try {
    await api(`/api/projects/${p.id}/shots-layout`, {
      method: "POST", body: JSON.stringify({ ctl }),
    });
    ST.proj = await api("/api/projects/" + p.id);
    render();
    toast(ctl ? "已重新排版：每段开头带上说明句" : "已重新排版：说明句已去掉");
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

/* 出片排队（2026-11-21）：出片时追加了新套，新套那趟出片排在前面那趟后面。
   前端排队即可——用户就在这个页面上等，前面跑完自动发下一趟；不碰后端调度核心。
   cuts 组（p.task）忙就把请求存起来，轮询发现 cuts 空闲再 flush。 */
function enqueueRender(pid, body, msg) {
  if (taskBusy(ST.proj && ST.proj.task)) {
    ST.pendingRender = { pid, body, msg };
    toast(msg || "前面还有一趟出片在跑，这趟已排队，跑完自动开始");
    startPoll();   // 确保轮询活着，好检测空闲
    render();      // 立刻把主按钮刷成「已排队·再点可改」，不等下一拍轮询
    return false;
  }
  return true;     // 不忙，调用方直接发
}

async function sendRender(pid, body) {
  await api(`/api/projects/${pid}/render-plans`, {
    method: "POST", body: JSON.stringify(body),
  });
  ST.proj.task = { kind: "batch", pct: 0, msg: "启动中", error: "" };
  render();
  startPoll();
}

function flushPendingRender() {
  const q = ST.pendingRender;
  if (!q || taskBusy(ST.proj && ST.proj.task)) return;
  ST.pendingRender = null;
  toast("前面那趟出片完了，开始排队的那趟");
  sendRender(q.pid, q.body).catch((e) => toast(`排队出片没启动：${e.message}`));
}

function startPoll() {
  stopPoll();
  let idle = 0;
  let fails = 0;
  ST.timer = setInterval(async () => {
    try {
      ST.proj = await api("/api/projects/" + ST.proj.id);
      fails = 0;
      render();
      // 有排队出片就把 cuts 当忙：既要等前面跑完再 flush，也不能让轮询提前停。
      if (ST.pendingRender) flushPendingRender();
      // 三条产线任一在跑（或还有排队）就要继续轮询；全都停了才算空闲
      const busyNow = taskBusy(ST.proj.task) || taskBusy(ST.proj.task_nosub)
        || taskBusy(ST.proj.task_shots) || taskBusy(ST.proj.task_planadd)
        || !!ST.pendingRender;
      if (!busyNow) {
        idle += 1;
        if (idle >= 3) { stopPoll(); loadList(); }  // 连续几拍都没任务才停，避免刚启动时误判
      } else {
        idle = 0;
      }
    } catch (e) {
      // 以前这里静默吞掉：服务退出 / 接口连续失败时，状态条会一直停在旧进度，
      // 用户只会觉得「点了没反应」。连续 3 拍失败说一次，恢复后自动清零。
      fails += 1;
      if (fails === 3) toast("状态暂时刷不出来，看看程序窗口是不是还开着；恢复后会自动接上");
    }
  }, 1500);
}
function stopPoll() {
  if (ST.timer) clearInterval(ST.timer);
  ST.timer = null;
}

async function trigger(kind, body) {
  const p = ST.proj;
  try {
    await api(`/api/projects/${p.id}/${kind}`, {
      method: "POST",
      body: body ? JSON.stringify(body) : undefined,
    });
    ST.proj.task = { kind, pct: 0, msg: "启动中", error: "" };
    render();
    startPoll();
  } catch (e) {
    toast(e.message);
  }
}

/* 第 1 步：整理台词。
   2026-11-19 用户定（硬规矩）：手动点这个按钮 = 从第一集 whisper 听音重转整部剧（force），
   永远不从校对起步、也不静默只补几集。补没转的集由「打开项目自动转写」负责，不在这里。
   繁转简、对时、按屏切行、AI 校对都在重转这条链末尾自动跟着跑。 */
$("#btn-transcribe").addEventListener("click", async () => {
  const p = ST.proj;
  if (!p) return;
  const all = (p.episodes || []).length;
  const hasLines = (p.episodes || []).some((e) => (e.lines || []).length);
  const body = hasLines ? [
    `${all} 集从第一集开始逐集重新听音识别，现在的台词（含手动改过的）都会被覆盖。`,
    "转完会自动转简体、重新对时间、按原片字幕对齐断句，再跑一遍 AI 校对：人名写法不一致、同音错字、乱码漏字、断句病句都会直接改好。",
    "只有整行都是杂音的句子不会自动删，留在「校对台词」里等你勾。",
    "整部剧要跑十几到几十分钟；进度会从第 1 集开始走，中途可以中止。",
    "之前的剧情结构、分镜、成片是基于旧台词做的，整理完要重新生成。",
  ] : [
    `${all} 集从第一集开始逐集听音识别，转完自动转简体、对时间、对齐断句，再跑一遍 AI 校对。`,
    "整部剧要跑十几到几十分钟；进度会从第 1 集开始走，中途可以中止。",
  ];
  const ok = await askConfirm({
    title: hasLines ? "从头重新整理台词？" : "开始整理台词？",
    body,
    okText: hasLines ? `从第 1 集重来（${all} 集）` : `开始（${all} 集）`,
    cancelText: "先不动", danger: hasLines,
  });
  if (!ok) return;
  await trigger("transcribe", { force: true });
});

/* 第 1 步：添加视频（2026-11-20 用户定）。
   只追加新集、不碰旧集；加完一键跑「新集转写(含对时+校对) → 抹原字幕」。
   三种来源：扫描项目原素材目录 / 弹原生框选视频文件 / 选文件夹。 */
const avModal = $("#modal-addvideos");
let avDone = null;
function avClose(value) {
  if (!avDone) return;
  const done = avDone;
  avDone = null;
  avModal.classList.add("hidden");
  done(value);
}
function askVideoSource() {
  return new Promise((resolve) => {
    if (avDone) { resolve(null); return; }
    avDone = resolve;
    avModal.classList.remove("hidden");
  });
}
$("#av-cancel").addEventListener("click", () => avClose(null));
avModal.addEventListener("click", (e) => { if (e.target === avModal) avClose(null); });
document.addEventListener("keydown", (e) => {
  if (avDone && e.key === "Escape") { e.preventDefault(); avClose(null); }
});
$("#av-scan").addEventListener("click", () => avClose({ scan_src: true, paths: [] }));
$("#av-files").addEventListener("click", async () => {
  avClose("__pending__");  // 先关来源弹窗，原生框弹在后面
  try {
    const init = (ST.proj && ST.proj.win_dir) || "";
    const r = await api(`/api/pick-videos?key=addvideos_video&initial=${encodeURIComponent(init)}`, { method: "POST" });
    if (r && r.paths && r.paths.length) {
      await startAddVideos({ scan_src: false, paths: r.paths });
    } else toast("没选到视频");
  } catch (e) { toast(e.message); }
});
$("#av-folder").addEventListener("click", async () => {
  avClose("__pending__");
  try {
    const init = (ST.proj && ST.proj.win_dir) || "";
    const r = await api(`/api/pick-folder?key=addvideos_dir&initial=${encodeURIComponent(init)}`, { method: "POST" });
    if (r && r.path) {
      await startAddVideos({ scan_src: false, paths: [r.path] });
    } else toast("没选到文件夹");
  } catch (e) { toast(e.message); }
});

async function startAddVideos(body) {
  const p = ST.proj;
  if (!p) return;
  try {
    const r = await api(`/api/projects/${p.id}/add-videos`, {
      method: "POST", body: JSON.stringify(body),
    });
    // 新集马上出现在列表里（追加是先于任务写盘的），重读项目把剧集数刷新出来
    ST.proj = await api("/api/projects/" + p.id);
    ST.proj.task = { kind: "addvideos", pct: 0, msg: "启动中", error: "" };
    render();
    startPoll();
    const added = r.added || 0;
    const extra = (r.skipped ? `，跳过 ${r.skipped} 个已导入的` : "") +
                  (r.errors && r.errors.length ? `，${r.errors.length} 个读不了：${r.errors[0]}` : "");
    toast(added
      ? `加了 ${added} 集，正在整理新集台词并抹掉原字幕${extra}`
      : `没有新视频${extra}`);
  } catch (e) { toast(e.message); }
}

$("#btn-addvideos").addEventListener("click", async () => {
  const p = ST.proj;
  if (!p) return;
  if (taskBusy(p.task) || taskBusy(p.task_nosub)) {
    return toast("还有任务在跑，等它跑完再加视频");
  }
  const choice = await askVideoSource();
  if (!choice || choice === "__pending__") return;
  // 扫描原目录：先确认一下（可能一次进来很多集，要让用户知道会转录+校对，花钱花时间）
  if (choice.scan_src) {
    const ok = await askConfirm({
      title: "扫描原素材目录添加视频？",
      body: [
        "把素材目录里还没导入的视频接到现有剧集后面，已经导入的会自动跳过。",
        "加完只给新集整理台词、对时间、AI 校对（约 1~18 分钟）、抹掉原字幕；旧集和已出的成片都不动。",
        "如果新加了很多集，这一步要跑一段时间，进度在页面顶部，中途可以中止。",
      ],
      okText: "扫描并添加", cancelText: "先不加",
    });
    if (!ok) return;
  }
  await startAddVideos(choice);
});
/* 2026-11-19 用户定：耗时任务开始前先弹窗。剪辑表和出片是后台任务、可中止。 */
$("#btn-analyze").addEventListener("click", async () => {
  if (!ST.proj) return;
  const ok = await askConfirm({
    title: "生成剪辑表？",
    body: [
      "AI 通读全剧台词，按目标时长挑出片段、排成剪辑表，并写好每集要点。",
      "会覆盖现在手工改过的剪辑表和要点；台词本身不会动。",
      "整部剧通常要几分钟，跑起来后可以中止；跑完在这里看结果。",
    ],
    okText: "生成剪辑表", cancelText: "先不生成",
  });
  if (!ok) return;
  trigger("analyze");
});
$("#btn-render").addEventListener("click", async () => {
  if (!ST.proj) return;
  const ok = await askConfirm({
    title: "开始出片？",
    body: [
      "按现在的剪辑表从原片切段拼接，统一成竖屏画面和一致的音量，画面不加字。",
      "按目标时长切，整部剧出片通常要十几到几十分钟，跑起来后可以中止。",
      "成片会写到设置里的成片保存位置，出完在下面可以直接下载。",
    ],
    okText: "开始出片", cancelText: "先不出片",
  });
  if (!ok) return;
  trigger("render");
});

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
const VL = { pid: "", src: "main", eps: [], cur: 0, dirty: false };

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
  // 直出：每个视频各一份清单，切到哪集读哪集；主项目在主视区（第 1 步）校对，这里不碰。
  vlProofSync();
}

/* ── 台词校对（2026-11-19）：AI 出清单，勾选才写回 ──────────────────────
   同一套逻辑挂两处，按「这件事属于谁」分：
   · 第 1 步「校对台词」——整部剧一次，清单是全剧级的，和单集编辑无关。
   · 直出（选视频直接出片）的台词弹窗——每个视频互相独立，各调一次模型、各存
     一份清单，绝不能跨视频合并（不同剧里的同名角色会被当成同一人改坏）。
   说话时间一律不动。见后端 app/proof.py。 */
function makeProof(scopeSel, cfg) {
  const root = document.querySelector(scopeSel);
  if (!root) return null;
  const q = (k) => root.querySelector(`[data-pr="${k}"]`);
  const el = {
    title: q("title"), hint: q("hint"), run: q("run"), list: q("list"),
    acts: q("actions"), apply: q("apply"), clear: q("clear"), state: q("state"),
    auto: q("auto"),
  };
  const st = { names: [], typos: [], drops: [], busy: false, boundPid: "" };
  const isOne = () => cfg.mode() === "per_ep";
  const hasList = () => !!(st.names.length || st.typos.length || st.drops.length);
  const setState = (t) => { if (el.state) el.state.textContent = t || ""; };
  function setRun(busy) {
    if (!el.run) return;
    el.run.disabled = !!busy;
    el.run.textContent = busy ? "检查中…"
      : hasList() ? "重新检查"
        : (isOne() ? "检查这个视频" : "开始校对");
  }

  /* 收起清单（标题和提示不动） */
  function empty() {
    st.names = []; st.typos = []; st.drops = [];
    if (el.list) { el.list.innerHTML = ""; el.list.classList.add("hidden"); }
    if (el.acts) el.acts.classList.add("hidden");
    if (el.auto) { el.auto.textContent = ""; el.auto.classList.add("hidden"); }
    setState("");
    setRun(false);
  }

  /* 换项目 / 换视频时整体回到初始态 */
  function reset() {
    const one = isOne();
    if (el.title) el.title.textContent = one ? "这个视频的台词校对" : "整部剧的台词校对";
    if (el.hint) el.hint.textContent = one
      ? "只看当前这个视频（各视频互相独立）；列出人名写法、错的字、只有杂音的行，勾选后写回，说话时间不动。"
      : "通读全部集，列出人名写法、错的字、只有杂音的行；勾选后写回，说话时间不动。";
    empty();
  }

  function render(r) {
    const one = isOne();
    st.names = r.names || []; st.typos = r.typos || []; st.drops = r.drops || [];
    // 整剧量大时后端会分批通读；某一批没成时清单是残的，必须说清楚，
    // 不能让人以为「就这些」，也别把残缺清单当最终结果（2026-11-19）。
    const bad = (r.errors || []).length;
    // 「整理台词时已经自动改掉的」：人名统一 + 错字/乱码/漏字/病句全由后端直接写回，
    // 这里说清楚改了啥，并给出明细（每处整句前后），剩下的才是要人点头的（只剩删行）。
    const a = r.auto || {};
    const autoTxt = (a.names || a.typos)
      ? `整理台词时已自动统一 ${a.names || 0} 组人名、改掉 ${a.typos || 0} 处错字和病句。`
      : "";
    const items = (a.items || []).slice(0, 200);
    if (el.auto) {
      if (!autoTxt) {
        el.auto.textContent = "";
        el.auto.classList.add("hidden");
      } else {
        const rows = items.map((it) => `第${it.ep}集第${(it.i | 0) + 1}句：`
          + `${esc(it.before)} <span class="muted">→</span> ${esc(it.after)}`);
        // 明细只留最近 AUTO_LOG_MAX 条（更早那次还没开始记），措辞别夸大成「全部」。
        const label = items.length >= (a.names || 0) + (a.typos || 0)
          ? `看改了哪 ${items.length} 处` : `看最近 ${items.length} 处明细`;
        el.auto.innerHTML = esc(autoTxt)
          + (rows.length
            ? ` <details class="fold"><summary>${label}</summary>`
              + `<div class="small">${rows.join("<br>")}</div></details>`
            : "");
        el.auto.classList.remove("hidden");
      }
    }
    if (!hasList()) {
      el.list.innerHTML = bad
        ? `<div class="proof-empty">这次没拿到清单：${esc(r.errors[0])}<br>再点一次「重新检查」从头跑一遍。</div>`
        : `<div class="proof-empty">${autoTxt
          ? "剩下的没有要你确认的了。"
          : `${one ? "这个视频" : "整部剧"}没发现需要改的地方。`}</div>`;
      el.list.classList.remove("hidden");
      el.acts.classList.add("hidden");
      // 空清单可能是「真没问题」，也可能是「有一批没跑成」——两种要说清。
      if (bad) {
        setState(`有 ${bad} 批没成功（${r.errors[0]}），清单可能不全；`
          + `再点一次「重新检查」从头跑。`);
      }
      setRun(false);
      return;
    }
    const parts = [];
    if (st.names.length) {
      parts.push('<div class="proof-sec">人名写法（勾选要统一的，右边可改标准写法）</div>');
      parts.push(st.names.map((g, k) => {
        const vs = (g.variants || []).map((v) => esc(v)).join(" / ");
        const std = esc(g.standard || "");
        return `<label class="proof-row">
        <input type="checkbox" class="proof-name-pick" data-k="${k}" checked>
        <span class="pl-body"><span class="pl-old">${vs}</span>
          → 统一成 <input type="text" class="proof-std" data-k="${k}" value="${std}"
             style="width:88px;font-size:12px;padding:2px 6px"></span>
      </label>`;
      }).join(""));
    }
    if (st.typos.length) {
      parts.push('<div class="proof-sec">错的字（同音错字、识别乱码、漏字多字）</div>');
      parts.push(st.typos.map((it, k) => {
        return `<label class="proof-row">
        <input type="checkbox" class="proof-pick" data-k="${k}" checked>
        ${one ? "" : `<span class="pl-no">第 ${Number(it.ep || 0)} 集</span>`}
        <span class="pl-body"><span class="pl-old">${esc(it.old || "")}</span>
          → <span class="pl-new">${esc(it.new || "")}</span>
          ${it.why ? `<span class="pl-why"> · ${esc(it.why)}</span>` : ""}</span>
      </label>`;
      }).join(""));
    }
    if (st.drops.length) {
      parts.push('<div class="proof-sec">只有杂音的行（默认不删；要删的请自己勾，勾了整行连时间一起去掉）</div>');
      parts.push(st.drops.map((it, k) => {
        return `<label class="proof-row">
        <input type="checkbox" class="proof-drop-pick" data-k="${k}">
        ${one ? "" : `<span class="pl-no">第 ${Number(it.ep || 0)} 集</span>`}
        <span class="pl-body"><span class="pl-old">「${esc(it.text || "")}」</span>
          ${it.why ? `<span class="pl-why"> · ${esc(it.why)}</span>` : ""}</span>
      </label>`;
      }).join(""));
    }
    el.list.innerHTML = parts.join("");
    el.list.classList.remove("hidden");
    el.acts.classList.remove("hidden");
    setState(bad
      ? `有 ${bad} 批没成功（${r.errors[0]}），清单可能不全；再点一次「重新检查」从头跑。`
      : st.drops.length
        ? "改动默认勾上，取消不想改的；人名右边可改标准写法。「只有杂音的行」默认不删，要删自己勾。"
        : "默认都勾上了，取消不想改的；人名右边可改标准写法。");
    setRun(false);
  }

  /* 打开时不花钱：把上次算好的清单读出来（台词没变才有） */
  async function loadCached() {
    const pid = cfg.pid();
    if (!pid) return false;
    try {
      const pr = await api(`/api/projects/${pid}/proof${isOne() ? `?ep=${cfg.ep()}` : ""}`);
      if (pr && pr.valid) { render(pr); return true; }
    } catch (_) { /* 读不到不影响台词编辑 */ }
    return false;
  }

  async function run() {
    const pid = cfg.pid();
    if (!pid || st.busy) return;
    // 台词正在整理时后端会直接拒绝（409）。先在这里拦一句人话，
    // 别让按钮写着「检查中…」空等——用户会以为程序死了（2026-11-19 反馈）。
    const why = cfg.blocked ? cfg.blocked() : "";
    if (why) { setState(why); if (cfg.msg) cfg.msg(why, true); toast(why); return; }
    // 2026-11-19 用户定：耗时任务开始前先弹窗说清代价，别点了就闷头跑。
    // 做成配置钩子：只有整剧校对那个实例配，直出的单集校对不打扰。
    if (cfg.confirmBefore && !(await cfg.confirmBefore())) return;
    st.busy = true;
    setRun(true);
    setState("");
    if (cfg.msg) cfg.msg(isOne() ? "正在检查这个视频的台词…" : "正在通读整部剧…");
    // 整剧校对是一次模型长调用（实测 1~4 分钟），中途只能等。
    // 把「已经等了多少秒」写出来，用户才知道它还在干活、不是卡死了。
    let sec = 0;
    const tick = setInterval(() => {
      sec += 5;
      setState(`正在通读${isOne() ? "这个视频" : "整部剧"}…已等 ${sec} 秒`
        + `（通常要 1~3 分钟，别关页面）`);
    }, 5000);
    try {
      const body = isOne() ? JSON.stringify({ ep: cfg.ep() }) : "{}";
      const r = await api(`/api/projects/${pid}/proof-suggest`, { method: "POST", body });
      render(r);
      if (cfg.msg) cfg.msg(r.cached ? "清单是从上次的结果读出来的，没重复花额度。" : "检查完成，看下面的清单。");
    } catch (e) {
      // 后端「正在自动整理台词」是 409 的原文，对用户没意义，翻成人话。
      const msg = /正在自动整理台词/.test(e.message)
        ? "台词还在重新整理，等这一轮跑完再校对"
        : e.message;
      setState("检查失败：" + msg);
      if (cfg.msg) cfg.msg("检查失败：" + msg, true);
      toast("检查失败：" + msg);
    } finally {
      clearInterval(tick);
      st.busy = false;
      setRun(false);
    }
  }

  async function apply() {
    const pid = cfg.pid();
    if (!pid || st.busy) return;
    const pick = (sel) => Array.from(root.querySelectorAll(`${sel}:checked`))
      .map((n) => Number(n.dataset.k)).filter((k) => Number.isInteger(k));
    const namePicks = pick(".proof-name-pick");
    const typoPicks = pick(".proof-pick");
    const dropPicks = pick(".proof-drop-pick");
    const standards = {};
    root.querySelectorAll(".proof-std").forEach((n) => {
      const v = (n.value || "").trim();
      if (v) standards[n.dataset.k] = v;
    });
    if (!namePicks.length && !typoPicks.length && !dropPicks.length) {
      return toast("一个都没勾，不用应用");
    }
    st.busy = true;
    if (el.apply) el.apply.disabled = true;
    setState("正在写入…");
    try {
      const payload = { name_picks: namePicks, typo_picks: typoPicks,
                        drop_picks: dropPicks, standards };
      if (isOne()) payload.ep = cfg.ep();   // 直出只改当前这个视频
      const r = await api(`/api/projects/${pid}/proof-apply`, {
        method: "POST", body: JSON.stringify(payload),
      });
      const total = (r.names || 0) + (r.typos || 0);
      const dropped = r.dropped || 0;
      empty();
      if (cfg.done) await cfg.done(r);
      const what = (dropped ? `改了 ${total} 处、删了 ${dropped} 行杂音` : `共 ${total} 处改动`)
        + (isOne() ? "。要重新出片，新字幕才会生效。"
          : "。之前生成的结构和分镜要用新台词，再重新生成一次。");
      setState("已保存：" + what);
      if (cfg.msg) cfg.msg("已保存：" + what);
      toast(dropped ? `已改 ${total} 处、删 ${dropped} 行` : `已改 ${total} 处`);
    } catch (e) {
      setState(e.message);
      if (cfg.msg) cfg.msg(e.message, true);
      toast(e.message);
    } finally {
      st.busy = false;
      if (el.apply) el.apply.disabled = false;
    }
  }

  if (el.run) el.run.addEventListener("click", run);
  if (el.clear) el.clear.addEventListener("click", empty);
  if (el.apply) el.apply.addEventListener("click", apply);

  return {
    root, st, reset, empty, render, loadCached, run, apply,
    show(on) { root.classList.toggle("hidden", !on); },
  };
}

/* 第 1 步：整部剧的校对。清单是全剧级，改单集、切集都不影响它。 */
const PROOF_S1 = makeProof("#s1-proof", {
  mode: () => "whole",
  pid: () => (ST.proj && ST.proj.id) || "",
  ep: () => 0,
  // 正在整理台词时别让人点了才被拒：按钮就在旁边，直接说清为什么点不了。
  blocked: () => {
    const t = ST.proj && ST.proj.task;
    if (!t || t.kind !== "transcribe") return "";
    return `${t.msg || "台词正在重新整理"}，等它跑完再校对`;
  },
  // 2026-11-19 用户定：整剧校对是耗时任务，开始前弹窗说清代价。
  confirmBefore: async () => {
    const epN = ((ST.proj && ST.proj.episodes) || []).length;
    return askConfirm({
      title: "开始校对整部剧台词？",
      body: [
        `AI 通读全部 ${epN} 集，找出人名写法不一致、同音错字、乱码漏字、断句病句。`,
        "只列清单不改台词：勾选后才会写回，改字不动每一句的起止时间。",
        "台词没改过就直接读上次结果，不花时间也不花额度；真要重读整部剧通常 3~18 分钟。",
        "这一步是整套一起跑，中途不能停，请别关页面；跑完会在这里列出清单。",
      ],
      okText: "开始校对", cancelText: "先不校对",
    });
  },
  done: async () => { await refreshProject(); },   // 台词变了，集数/状态跟着刷新
});

/* 台词弹窗：只给直出用（每个视频各一份清单），主项目不在这里校对 */
const PROOF_VL = makeProof("#vl-proof", {
  mode: () => "per_ep",
  pid: () => VL.pid,
  ep: () => VL.cur,
  msg: (t, bad) => vlMsg(t, bad),
  done: async () => {
    // 后端只改了 lines[].text，编辑器显示的是格式化文本，重新取一次才一致。
    const fresh = await api(`/api/projects/${VL.pid}/lines`);
    VL.eps = fresh.episodes || [];
    const cur = VL.eps.find((x) => x.index === VL.cur);
    $("#vl-text").value = cur ? cur.text : $("#vl-text").value;
    VL.dirty = false;
    vlRenderChips();
    try {   // 直出面板上的台词数、状态也跟着变
      const d = await api("/api/direct/" + VL.pid);
      DP.curProj = d; dpRender(d);
    } catch (_) { /* 刷新失败不影响已保存的结果 */ }
  },
});

/* 切集时按需换清单：直出每个视频各一份，切到哪集看哪集。 */
function vlProofSync() {
  if (!VL.pid || !PROOF_VL) return;
  PROOF_VL.reset();
  if (VL.src === "direct") PROOF_VL.loadCached();
}

/* 第 1 步的入口：点一下展开并出清单（台词没改过就直接用上次的，不重复花额度） */
$("#btn-proof").addEventListener("click", async () => {
  if (!ST.proj || !ST.proj.id) return;
  PROOF_S1.show(true);
  try { PROOF_S1.root.scrollIntoView({ block: "nearest", behavior: "smooth" }); } catch (_) {}
  await PROOF_S1.run();
});

async function openViewLines(pid, src) {
  // src="main" 第 1 步（默认）；src="direct" 选视频直接出片。
  // 编辑器本身完全共用，只是保存后刷新的面板和提示不同。
  if (!pid) return;
  VL.pid = pid;
  VL.src = src || "main";
  const tip = $("#vl-tip");
  if (tip) tip.textContent = VL.src === "direct"
    ? "改完，已经做好的成片不会变，要重新出片才生效。"
    : "改完，之前生成的结构和分镜不会自动更新，需要重新生成；整部剧的人名和错字，回第 1 步点「校对台词」。";
  vlMsg("读取台词中…");
  try {
    const r = await api(`/api/projects/${VL.pid}/lines`);
    VL.eps = r.episodes || [];
    $("#vl-name").textContent = r.name || "";
    viewLinesModal.classList.remove("hidden");
    // 这个弹窗只负责「这一集」：整部剧的校对在第 1 步，
    // 只有直出（每个视频各一份清单）才把校对区带出来。
    PROOF_VL.show(VL.src === "direct");
    PROOF_VL.reset();
    const first = VL.eps.find((e) => e.count) || VL.eps[0];
    VL.cur = first ? first.index : 0;
    VL.dirty = false;
    vlSelect(VL.cur);   // 直出：内部会按当前集读这一份清单
  } catch (e) {
    toast(e.message);
  }
}

function closeViewLines() {
  if (VL.dirty) return toast("这一集改了还没保存，先点「保存这一集」或「放弃修改」");
  viewLinesModal.classList.add("hidden");
}

$("#btn-lines-view").addEventListener("click", () => openViewLines(ST.proj && ST.proj.id, "main"));
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
    if (VL.src === "direct") {
      vlMsg(`第 ${VL.cur} 集已保存（${r.line_count} 句）。已经做好的成片不会变，要重新出片才生效。`);
      // 直出项目只刷自己的面板：不要动主项目的选中态，也不要跳到第 1 步。
      try {
        const fresh = await api("/api/direct/" + VL.pid);
        DP.curProj = fresh;
        dpRender(fresh);
      } catch (_) { /* 刷新失败也别把「已保存」说成失败 */ }
    } else {
      vlMsg(`第 ${VL.cur} 集已保存（${r.line_count} 句）。之前生成的结构和分镜要用新台词，请重新生成一次。`);
      await refreshProject();
    }
    vlRenderChips();
    toast("台词已保存");
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
  // 按**这批要用的集**算：勾了 8 集却拿 40 集的时长去推荐，会推出一组
  // 这 8 集根本凑不出的套数，生成时才发现凑不够。
  const selSet = PL.eps;
  const totalMin = (p.episodes || [])
    .filter((e) => !selSet.size || selSet.has(e.index))
    .reduce((a, e) => a + (e.duration || 0), 0) / 60;
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
   - 没做过结构横评的（qwen-max、GLM-5、MiniMax-M3）也不放，
     要试就选「自定义…」手输名字。
   - deepseek-v4.1-flash 于 2026-11-19 补做结构横评后入列：保留思考时套间重合 47%，
     优于在列的 GLM-5.2（72%）；代价是出结构约 117 秒（GLM-5.2 约 59 秒）。
     注意它必须保留思考，关思考会退化成 96% 重合——见 analyze._is_thinker。
   - **跑得通 ≠ 能用**（2026-11-05 补）：横评重合 >90% 的照样不进下拉——
     qwen-plus（三套重合 96%）、MiniMax-M2.7（93%）就是这样剔掉的，
     准入标准和 kimi/step「探针通但零产出」是同一条：出不了可用结果的概不提供。
   - 每条都能在 docs/模型对比报告.html 里查到记录。
   - pmAlive：点「检测可用模型」后，现在探不通的（欠费/没开通）会从下拉里去掉，
     但不会动「自己填进来的型号」——那一行会切到「自定义…」保留原值。 */
/* 2026-11-19 用户定：下拉直接写真实模型名，不再用「快速 / 精细」档位名。
   value 仍是真实代号，提交逻辑一行没动；「跟随设置」「自定义…」保持人话。 */
const PM_MODELS = [
  ["", "跟随设置（推荐）"],
  ["qwen3.5-plus", "qwen3.5-plus"],
  ["ZHIPU/GLM-5.3", "ZHIPU/GLM-5.3"],
  ["ZHIPU/GLM-5.2", "ZHIPU/GLM-5.2"],
  ["deepseek-v4.1-flash", "deepseek-v4.1-flash"],
  ["deepseek-v4-pro", "deepseek-v4-pro"],
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

/* ---------- 这批用哪些集（2026-11-19）----------
   为什么要有：一次把 40 集 1200 多句丢给 AI，它得在上千个编号里挑，引用串号的
   概率随行数上升；套数一多，输出还会被截断。选集把编号空间压小，也让人可以
   先自己把整部剧裁一遍（这批只看 1-8 集，那批看 5-11 集）。
   空/未初始化 = 全部有台词的集。选择存在 batch.plan_eps，下次打开回填。 */
const PL = { eps: new Set(), ready: "" };

function planEpDone(p) {
  return ((p && p.episodes) || []).filter((e) => e.status === "done");
}

function planEpsSel() {
  return [...PL.eps].sort((a, b) => a - b);
}

/* 「第 1–8、12、20 集」这种短写法：批次标题上放不下 40 个集号 */
function epsBrief(list) {
  const arr = (list || []).slice().sort((a, b) => a - b);
  if (!arr.length) return "";
  const done = planEpDone(ST.proj);
  if (done.length && arr.length === done.length) return `全部 ${done.length} 集`;
  const parts = [];
  let s = arr[0], prev = arr[0];
  for (let k = 1; k <= arr.length; k++) {
    const v = arr[k];
    if (k < arr.length && v === prev + 1) { prev = v; continue; }
    parts.push(s === prev ? `${s}` : `${s}–${prev}`);
    if (k < arr.length) { s = v; prev = v; }
  }
  return `第 ${parts.join("、")} 集`;
}

/* 进项目时回填一次：优先上次存下的选集，没有就全选（老项目行为不变） */
function initPlanEps(p) {
  if (!p || PL.ready === p.id) return;
  PL.ready = p.id;
  const done = planEpDone(p).map((e) => e.index);
  const saved = ((p.batch || {}).plan_eps || []).filter((i) => done.indexOf(i) >= 0);
  PL.eps = new Set(saved.length ? saved : done);
}

function renderPlanEps() {
  const p = ST.proj;
  const box = $("#plan-ep-chips");
  if (!p || !box) return;
  const eps = p.episodes || [];
  const sig = p.id + "|" + eps.map((e) => `${e.index}:${Math.round(e.duration || 0)}:${e.status || ""}`).join(",");
  if (box.dataset.sig !== sig) {
    box.dataset.sig = sig;
    box.innerHTML = eps.map((e) => {
      const ok = e.status === "done";
      return `<button class="ep-chip${ok ? "" : " miss"}" data-ep="${e.index}"${ok ? "" : " disabled"}
        title="${ok ? `第 ${e.index} 集 · ${fmt(e.duration || 0)}` : `第 ${e.index} 集还没整理好台词，不能编排`}"
        >第${String(e.index).padStart(2, "0")}集<small>${ok ? fmt(e.duration || 0) : "未整理"}</small></button>`;
    }).join("");
  }
  box.querySelectorAll(".ep-chip").forEach((el) => {
    el.classList.toggle("on", PL.eps.has(Number(el.dataset.ep)));
  });
  const sel = planEpsSel();
  const dur = eps.filter((e) => PL.eps.has(e.index)).reduce((a, e) => a + (e.duration || 0), 0);
  setText("#plan-ep-stat", eps.length ? `已选 ${sel.length}/${eps.length} 集 · ${fmt(dur)}` : "");
  setText("#plan-ep-note", eps.length
    ? (sel.length ? `AI 只在这些集里挑片段，出的片也只从这些集切。${sel.length < eps.length ? `其余 ${eps.length - sel.length} 集这轮不用。` : ""}` : "一集都没选——至少选一集才能生成")
    : "");
  updatePlanFeasible();
}

function planEpToggle(i) {
  if (PL.eps.has(i)) PL.eps.delete(i); else PL.eps.add(i);
  renderPlanEps();
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
  initPlanEps(p);
  renderPlanEps();
  updatePlanFeasible();
}

/* 这次「生成结构」是不是追加（接在已有套后面）。
   追加走独立的 planadd 任务组，可与出片并行；覆盖式重排走 cuts，出片时不能点。 */
function planIsAppend() {
  const p = ST.proj;
  return !!(p && (p.plans || []).length && ($("#plan-append") || {}).checked);
}

function updatePlanFeasible() {
  const p = ST.proj;
  const el = $("#plan-feasible");
  const btn = $("#btn-plan");
  if (!el) return;
  const jobs = planJobs();
  const totalSets = jobs.reduce((a, j) => a + j.count, 0) || 3;
  const nSel = PL.eps.size;
  if (btn) {
    const tail = nSel && nSel < ((ST.proj && ST.proj.episodes) || []).length ? ` · 用 ${nSel} 集` : "";
    setText(btn, jobs.length > 1 ? `生成 ${totalSets} 套结构（${jobs.length} 组）${tail}`
                                 : `生成 ${totalSets} 套结构${tail}`);
    // 别只按「选没选集」开关按钮：render() 里先按忙碌态设过 disabled，
    // 这里若无条件覆盖，生成途中按钮会重新变成可点。
    // 追加式（planadd 组）可与出片并行：出片忙不禁用它，但追加自己在跑时要禁用。
    const addBusy = taskBusy(p && p.task_planadd);
    const cutsBlock = planIsAppend() ? false : !!(p && p.task && taskBusy(p.task));
    btn.disabled = !nSel || !planEpDone(p).length || addBusy || cutsBlock;
  }
  if (!p) return;
  // 素材量按**选集**算：只勾 8 集时，「相当于整部剧的百分之多少」必须用这 8 集的
  // 时长当分母，否则会算出一个不存在的余量，把套数说成还很宽松。
  const selSet = PL.eps;
  const totalMin = (p.episodes || [])
    .filter((e) => selSet.has(e.index))
    .reduce((a, e) => a + (e.duration || 0), 0) / 60;
  const allMin = (p.episodes || []).reduce((a, e) => a + (e.duration || 0), 0) / 60;
  if (!totalMin) {
    setText(el, nSel ? "" : "一集都没选——上面「这批用哪些集」至少勾一集");
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
  const scopeTxt = allMin && totalMin < allMin - 0.05
    ? `选中的 ${nSel} 集 ${totalMin.toFixed(1)} 分钟（全剧 ${allMin.toFixed(1)} 分钟）`
    : `全剧 ${totalMin.toFixed(1)} 分钟`;
  const base = `${scopeTxt}；`
    + (jobs.length > 1 ? `${jobs.length} 组共 ${totalSets} 套${detail}` : `${totalSets} 套`)
    + ` × 每套约 ${avg.toFixed(1)} 分钟 = ${need.toFixed(1)} 分钟，相当于这些集的 ${Math.round(reuse * 100)}%`;
  const warn = reuse > 1.0;
  // 套数才是「生成失败」的真正来源：每套十几块，10 套的输出就顶到模型上限，
  // 到点会被截断、整批白跑。这不是集数多的问题，所以在这里直说，而不是等失败。
  const tooMany = totalSets > 8
    ? " 单批 8 套以上容易被截断（每套十几块，模型一次写不完），建议一次 3–5 套，剩下的再点一次生成。"
    : "";
  setText(el, (reuse <= 0.7
    ? `${base}，素材够分，几套内容能错得开。`
    : reuse <= 1.0
      ? `${base}，素材要用掉大半，几套之间会有些重复（正常，能接受）。`
      : `${base}，已经超过这些集的长度——素材必然重复。按「只用八成」算，${totalSets} 套每套最多 ${(totalMin * 0.8 / totalSets).toFixed(1)} 分钟；`
        + `想保持每套 ${avg.toFixed(1)} 分钟，最多只能出 ${Math.max(1, Math.floor(totalMin * 0.8 / avg))} 套。`) + tooMany);
  el.classList.toggle("warn", warn);
}

$("#btn-plan").addEventListener("click", async () => {
  const p = ST.proj;
  if (!((p.episodes || []).length)) return toast("还没整理好台词，先完成第 1 步");
  const eps = planEpsSel();
  if (!eps.length) return toast("先在上面「这批用哪些集」里至少勾一集");
  const jobs = planJobs();
  if (!jobs.length) return toast("至少保留一组");
  const norm = jobs.map((j) => ({
    model: j.model, count: j.count,
    min_minutes: Math.min(j.lo, j.hi), max_minutes: Math.max(j.lo, j.hi),
  }));
  const totalSets = norm.reduce((a, j) => a + j.count, 0);
  const order = FIXED_ORDER;
  const has = (p.plans || []).length;
  // 已有套时默认「接在后面」：同一部剧本来就要分批生成（这批 1-8 集、下批 5-11 集），
  // 一覆盖就把前面几批和已出的片进度全清了。想重来就取消勾选。
  const keepOld = has ? !!($("#plan-append") || {}).checked : false;
  const lines = norm.map((j) =>
    `· ${modelLabel(j.model)}：${j.count} 套 × ${j.min_minutes}-${j.max_minutes} 分钟`).join("\n");
  const placement = !has
    ? ""
    : keepOld
      ? `会加在已有 ${has} 套后面（已有套和已出的片都不动）。\n`
      : `会清掉已有 ${has} 套重新生成（已出的片还在）。\n`;
  if (!confirm(`AI 只在这 ${eps.length} 集里挑片段排结构：${epsBrief(eps)}\n${lines}\n\n`
    + placement
    + `共 ${totalSets} 套，会用到 ${norm.length} 次今日生成机会，预计约 ${planEta(norm.length)} 分钟。\n`
    + "生成后每套会标出与前面套的重复程度，只提示、不拦截。\n\n继续？")) return;
  try {
    const grant = await aiGrant(p.id, "plan");
    await api(`/api/projects/${p.id}/plan`, {
      method: "POST",
      headers: { "X-AI-Grant": grant },
      body: JSON.stringify({
        jobs: norm, order, eps,
        append: keepOld, new_batch: keepOld,
      }),
    });
    const planMsg = norm.length > 1 ? `共 ${norm.length} 组，预计约 ${planEta(norm.length)} 分钟`
      : `AI 正在通读选中的这 ${eps.length} 集（${epsBrief(eps)}），预计约 3 分钟`;
    if (keepOld) {
      ST.proj.task_planadd = { kind: "planadd", pct: 0, msg: `追加：${planMsg}`, error: "" };
    } else {
      ST.proj.task = { kind: "plan", pct: 0, msg: planMsg, error: "" };
    }
    render();
    startPoll();
  } catch (e) {
    toast(e.message);
  }
});

/* 选集格子：点一下切换。一集一格比下拉框快（和单集钩子那边同一套交互）。 */
$("#plan-ep-chips").addEventListener("click", (e) => {
  const b = e.target.closest(".ep-chip");
  if (!b || b.disabled) return;
  planEpToggle(Number(b.dataset.ep));
});
$("#btn-plan-eps-all").addEventListener("click", () => {
  PL.eps = new Set(planEpDone(ST.proj).map((e) => e.index));
  renderPlanEps();
});
$("#btn-plan-eps-none").addEventListener("click", () => {
  PL.eps = new Set();
  renderPlanEps();
});
$("#btn-plan-range").addEventListener("click", () => {
  const want = parseEpRange($("#plan-range").value);
  if (!want.size) return toast("写成「3-12」或「1,3,5」这样");
  const ok = new Set(planEpDone(ST.proj).map((e) => e.index));
  const miss = [...want].filter((i) => !ok.has(i));
  PL.eps = new Set([...want].filter((i) => ok.has(i)));
  renderPlanEps();
  if (miss.length) toast(`第 ${miss.slice(0, 6).join("、")} 集还没整理好，没选上`);
  else toast(`已选 ${PL.eps.size} 集`);
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
  // 2026-11-21：原来第 1 组「片头」（看点字幕 / 开场钩子卡）整组删掉。混剪结构只交
  // from/to，AI 不再给块标题（label）和钩子文案（hook）：看点字幕勾了每块只烧同一句
  // 「精彩片段」，钩子卡勾了根本不出卡（文案取不到，render 直接跳过）。下面几组编号
  // 相应往前挪，配乐 1、开头闪镜 2、镜头转场 3。
  return `
  <div class="fx-group fx-g0 fx-auto">
    <div class="fx-group-h"><i class="fx-gno">★</i>画面全自动随机<span class="hint">点一次出片，程序自动给每一段换画面；你不用逐项设。同一批每套都不一样，只动画面，不改时长、不改台词</span></div>
    <label class="chk fx-automain" title="开着就行：放大、加速、调色、变形、氛围特效每段自动换一个无感级的小变化；字幕出场动效按「集」随机，干净底片时整幅镜像也按集随机（同一集一致，不忽翻忽不翻）。关掉后，下面展开的手动设置就是最终效果。"><input type="checkbox" id="${p}autofx" checked> 开启全自动随机（推荐，不用再管下面这些）</label>
    <div class="fx-line">
      <label class="fx-f" title="随机变化的大小。标准：同批能看出差别但不影响观感；保守：几乎看不出；强力：一眼能看出构图不同。"><select id="${p}fp">
          <option value="std">标准 · 每套都不一样（推荐）</option>
          <option value="soft">保守 · 几乎看不出来</option>
          <option value="strong">强力 · 变化更明显</option>
        </select> 随机幅度</label>
    </div>
    <p class="fx-tip" id="${p}fptip"></p>
    <p class="fx-status" id="${p}fxstatus"></p>
    <details class="fold" id="${p}manualfold"><summary>想自己细调？展开（用哪份视频 / 镜像 / 放大 / 加速 / 调色 / 变形 / 氛围 / 字幕动效）</summary>
    <div class="fold-body">
      <p class="fx-tip">不展开就是全随机。展开后改的值会成为随机的「基准」；只有把最上面的「开启全自动随机」关掉，这里才是最终效果。整幅镜像和字幕出场动效在随机开启时按集自动决定，只在关掉随机后才按你选的来。</p>
      <label class="chk" title="整幅放大一点点再居中裁回：画面指纹跟原片不一样，边缘一圈（含黑边、水印带）被切掉。开启全自动随机后，每段会在这个倍率附近做 ±1% 的微抖。"><input type="checkbox" id="${p}zoom" checked> 画面放大（去重）</label>
      <label class="chk" title="评分 5-6 的缓冲段（反应、沉默、过场）放快一点，音调不变，爆点段原速。随机开启后每段还有极轻微的变速，总时长一位不差。"><input type="checkbox" id="${p}speed" checked> 缓冲段加速</label>
      <div class="fx-group fx-g2">
        <div class="fx-group-h">调色<span class="hint">只改颜色，不改时长、不降清晰度；默认「随机」——每区自己换档</span></div>
        <div class="fx-line">
          <label class="fx-f">档位
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
        <label class="fx-f">自定义调色文件（选了就替代上面的档位）
          <span class="row"><input id="${p}lut" type="text" spellcheck="false" placeholder="例如 D:/调色/电影感.cube"><button class="ghost" data-lutpick="${p}">选择…</button></span>
        </label>
      </div>
      <div class="fx-group fx-g3">
        <div class="fx-group-h">画面变形<span class="hint">只改人脸宽窄，不改时长；拖回 100% 就是原样</span></div>
        <div class="fx-line">
          <label class="fx-slider">横向变形 <b id="${p}stv">101.5%</b>
            <input type="range" id="${p}stretch" min="94" max="106" step="0.5" value="101.5">
            <span class="fx-scale"><i>94 显瘦</i><i>100 原样</i><i>106 显宽</i></span>
          </label>
        </div>
        <p class="fx-tip">94-99% 脸略瘦，100% 原样，101-106% 脸略宽；超过 6% 背景会明显变形。出厂 101.5% 是校出来的无感档。</p>
      </div>
      <div class="fx-group fx-g4">
        <div class="fx-group-h">氛围特效<span class="hint">在原画面上叠一层，不改时长；拖到 0 = 关</span></div>
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
        <p class="fx-tip">三项出厂默认都开在最轻一档，正常看片看不出差别，只是画面更有质感。</p>
      </div>
      <div class="fx-grid2">
        <label class="fx-f">字幕出场动效
          <select id="${p}nstyle">
            <option value="rise">上浮落位 · 推荐</option>
            <option value="fade">淡入淡出 · 最稳</option>
            <option value="pop">缩放弹出 · 有劲</option>
            <option value="float">缓缓漂浮 · 有呼吸感</option>
            <option value="type">逐字打出 · 口播感</option>
          </select>
        </label>
        <label class="fx-slider"><span>动效幅度 <b id="${p}nmotionv">推荐</b></span>
          <input type="range" id="${p}nmotion" min="0" max="100" step="10" value="50">
          <span class="fx-scale"><i>0 几乎不动</i><i>50 推荐</i><i>100 明显</i></span>
        </label>
      </div>
      <p class="fx-tip">随机开启时，每集会自己挑一种出场动效（同一集一致）。</p>
      <div class="fx-group fx-g5 fx-src">
        <div class="fx-group-h">用哪份视频 · 整幅镜像<span class="hint">字幕怎么来、能不能整幅镜像，由「用哪份视频」定；翻不翻由上面的全自动随机按集决定</span></div>
        <label class="fx-f">素材版本
          <select id="${p}srcver">
            <option value="nosub">干净底片 + 出片时加字幕（可镜像）</option>
            <option value="">原始视频 · 保留原来的字幕（不能镜像）</option>
          </select>
        </label>
        <p class="fx-tip" id="${p}srcvertip"></p>
        <div class="fx-mirror-block" id="${p}mirrorblock">
          <div class="fx-line">
            <label class="chk" id="${p}mirrorbox" title="整幅画面左右翻转，去重最彻底。随机开启时按集自动决定翻不翻：属于同一集的所有段一起翻或一起不翻；只有干净底片能用（底片没字，出片时才烧字，翻不到）。原始视频带原字幕，翻了字会反，程序会强制关掉。"><input type="checkbox" id="${p}mirror"${p === "d-" ? " checked" : ""}> 整幅左右镜像（去重最强）</label>
          </div>
          <p class="fx-tip" id="${p}mirrortip"></p>
          <p class="fx-tip">${p === "d-"
            ? "「按时间窗随机」＝这条片子每 60 秒翻一次或不翻：开头 60 秒翻、接着 60 秒不翻、再 60 秒翻……各段各抛各的硬币。因为直接出片就是一个视频、没有「集」可分组。关掉最上面的全自动随机后，这个勾选才按你选的固定生效（整条翻或不翻）。"
            : "「按集随机」＝第 1 集整集翻、第 2 集整集不翻、第 3 集整集翻……同一集前后一致，不会一段翻一段不翻。关掉最上面的全自动随机后，这个勾选才按你选的固定生效。"}</p>
        </div>
        <details class="fold"><summary>两份素材分别是什么、为什么镜像跟着它走</summary>
          <div class="fold-body">
            <p><b>干净底片</b>：第 2 步做的那份——原片上的字幕已经抹干净。出片时按台词把字幕烧回去（位置和字号跟着每集原字幕走）。一份底片所有套共用。它没有字，顺序是<b>切段 → 镜像 → 烧字幕</b>，字是翻完才画的，所以才能整幅镜像。</p>
            <p><b>原始视频</b>：导入的那份，画面上还带着原来的字幕。用它出片原样搬运，字幕改不了；整幅一翻字就跟着反了，所以这一档不提供镜像，程序会强制关掉。</p>
            <p>下面单独那组「开头闪一下镜像」只翻片头 0.3 秒，是另一回事，两种素材都能用。</p>
          </div>
        </details>
      </div>
    </div></details>
  </div>

  <!-- 配乐：独立成卡，卡内再分「放什么音乐」和「多响」两层 -->
  <div class="fx-group fx-g4">
    <div class="fx-group-h"><i class="fx-gno">1</i>配乐<span class="hint">跟画面随机无关，按需开</span></div>
    <div class="fx-checks">
      <label class="chk" title="出片时自动挑一首合适的音乐循环铺满全片：音量很低、台词一响再自动压低（闪避），不盖对白；结尾淡出。默认开。"><input type="checkbox" id="${p}bgm" checked> 加配乐</label>
      <label class="chk" title="把原片自带的背景音乐压掉，只留人声。适合你要换成自己的音乐、或者想去掉原片音乐降重的时候。代价：关门、碗筷、雨声这些环境音也会跟着变轻（程序会留一点点现场底），而且第一次出片要多等几分钟——同样素材再出就直接用上次的结果。默认开。"><input type="checkbox" id="${p}voiceiso" checked> 去掉原片自带音乐</label>
    </div>
    <p class="fx-tip hidden" id="${p}voiceisotip"></p>
    <div class="fx-bgm hidden" id="${p}bgmbox">
      <div class="fx-sub-h">放什么音乐</div>
      <div class="fx-bgm-modes">
        <label class="chk"><input type="radio" name="${p}bgmmode" id="${p}bgmfolder" value="folder" checked> 从我的文件夹随机挑</label>
        <label class="chk"><input type="radio" name="${p}bgmmode" id="${p}bgmmanual" value="file"> 指定一首歌</label>
      </div>
      <div class="fx-bgm-folder hidden" id="${p}bgmfolderbox">
        <label class="fx-f">音乐文件夹（mp3 / m4a / wav / flac，子文件夹也算）
          <span class="row"><input id="${p}bgmdir" type="text" spellcheck="false" placeholder="选一个放着多首歌的文件夹"><button class="ghost" data-bgmdirpick="${p}">选择…</button></span>
        </label>
        <p class="fx-tip">每份成片从里面挑一首、尽量不重复——一个视频出 3 份就用 3 首不同的。配合上面「去掉原片自带音乐」最干净：原片的音乐压掉，换成你这些曲子。</p>
      </div>
      <div class="fx-bgm-manual hidden" id="${p}bgmmanualbox">
        <label class="fx-f">音乐文件（mp3 / m4a / wav / flac）
          <span class="row"><input id="${p}bgmpath" type="text" spellcheck="false" placeholder="选一首自己电脑上的歌"><button class="ghost" data-bgmpick="${p}">选择…</button></span>
        </label>
        <p class="fx-tip">全部成片都用这一首。</p>
      </div>
      <div class="fx-sub-h">多响</div>
      <label class="fx-slider">基础音量 <b id="${p}bvolv">35%</b>
        <input type="range" id="${p}bvol" min="5" max="60" step="1" value="35">
        <span class="fx-scale"><i>5 若有若无</i><i>35 推荐</i><i>60 偏抢</i></span>
      </label>
      <p class="fx-tip">台词一响音乐会自动压低，所以不用怕调大。35% 是「听得见、不抢戏」的推荐值；每首曲子响度不同，程序会先拉平再按这个比例混。</p>
    </div>
  </div>

  <div class="fx-group fx-g6 fx-cap">
    <div class="fx-group-h"><i class="fx-gno">2</i>开头闪一下镜像<span class="hint">只动成片最前面 0.3 秒的入场效果，跟上面整片翻的「整幅镜像」是两回事</span></div>
    <div class="fx-checks">
      <label class="chk" title="成片最前面大约 0.3 秒把画面翻一下，之后立刻恢复原样；只做一次入场效果，正片不翻。默认开、上下翻。"><input type="checkbox" id="${p}fmirror" checked> 开头 0.3 秒闪一下</label>
    </div>
    <div class="fx-line hidden" id="${p}fmirrorline">
      <label class="fx-f">往哪边翻
        <select id="${p}fmirrordir">
          <option value="v">上下翻（推荐）</option>
          <option value="h">左右翻</option>
        </select>
      </label>
    </div>
    <p class="fx-tip hidden" id="${p}fmirrortip"></p>
    <details class="fold"><summary>这和上面的「整幅镜像」有什么不同</summary>
      <div class="fold-body">
        <p>这个是入场小动作：只翻最前面那一下，后面正片一点没动；上面那个是整片每集都翻，去重更强。它跟选哪份素材无关，两种素材都能开。</p>
      </div>
    </details>
  </div>

  <!-- 段间转场空境（2026-11-18）：每个镜头之间插一段虚化推镜过渡 -->
  <div class="fx-group fx-g7 fx-trans">
    <div class="fx-group-h"><i class="fx-gno">3</i>镜头转场<span class="hint">镜头之间加一段过渡，让切换更顺；会让成片稍微变长</span></div>
    <div class="fx-checks">
      <label class="chk" title="每两个镜头之间插一段虚化推镜的过渡空境，淡入淡出。用前一段的画面做虚化底，看起来像从一个镜头自然推到下一个。AI 剪辑出片按剪辑点插入；选视频直接出片会先自动找出镜头切换的位置再插入。"><input type="checkbox" id="${p}trans" checked> 镜头之间加过渡空境</label>
    </div>
    <div class="fx-line hidden" id="${p}transline">
      <label class="fx-slider">每段过渡时长 <b id="${p}transv">0.7 秒</b>
        <input type="range" id="${p}transsec" min="0.4" max="1.5" step="0.1" value="0.7">
        <span class="fx-scale"><i>0.4 秒</i><i>0.7 推荐</i><i>1.5 秒</i></span>
      </label>
    </div>
    <p class="fx-tip hidden" id="${p}transtip"></p>
    <details class="fold"><summary>转场是怎么加的、会不会盖住台词</summary>
      <div class="fold-body">
        <p>在每个镜头切换的地方插一段短短的过渡画面：取前一段的最后一帧，虚化、压暗、缓缓推近，再淡入淡出。它占的时间是额外加进去的，<b>不会盖住或剪掉任何台词</b>——只是成片总时长会多出「过渡段数 × 每段时长」。</p>
        <p>AI 剪辑出片直接用排好的剪辑点；选视频直接出片会先自动检测画面切换的位置（镜头换的地方）再插入。检测是自动的，偶尔个别片子可能少插或多插一个。</p>
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
  // 配乐来源两档（2026-11-19 去掉自动配乐）：folder=我的文件夹随机 / file=指定一首。
  // 界面已无 auto 档；老项目存的 auto 在 writeFx 回填时就落到 folder，这里兜底也按 folder。
  const bgmMode = (() => {
    const r = document.querySelector(`input[name="${p}bgmmode"]:checked`);
    const v = r ? String(r.value) : "folder";
    return ["folder", "file"].indexOf(v) >= 0 ? v : "folder";
  })();
  return {
    // 全自动随机（2026-11-17）：默认开；老项目没这个控件时 on() 返回 false，
    // 但真正缺省由后端 RenderFx.auto_fx=True 兜底——这里只在控件存在时传值。
    ...(el("autofx") ? { auto_fx: on("autofx") } : {}),
    // 片头组（看点字幕 / 开场钩子卡）2026-11-21 已从面板删掉：混剪只交 from/to，
    // 块标题（label）和钩子文案（hook）都没有来源，勾了也只会烧一句固定文字或干脆不出卡。
    // 这里显式写死 false，别用 on() 读一个不存在的控件（老项目存过 true 的也一并关掉）。
    title: false,
    zoom: on("zoom"),
    hook_card: false,
    // 段间转场：没勾就是 0（关）；勾了才带时长。时长控件不存在时兜 0.7。
    trans_sec: on("trans")
      ? Math.min(1.5, Math.max(0.4, num("transsec") || 0.7)) : 0,
    buffer_speed: on("speed"),
    bgm: on("bgm"),
    bgm_mode: bgmMode,
    bgm_auto: false,   // 自动配乐已移除；保留字段只为老后端兼容，恒 false
    bgm_path: (val("bgmpath") || "").trim(),
    bgm_dir: (val("bgmdir") || "").trim(),
    bgm_vol: Math.min(0.6, Math.max(0.05, (num("bvol") || 35) / 100)),
    // 去掉原片自带音乐（2026-11-18）：勾了才走人声分离，没勾一律 false
    voice_iso: on("voiceiso"),
    grade: val("grade"),
    grade_int: num("gint") / 100,
    rand_min: Math.min(15, Math.max(0.5, num("randmin") || 2)) * 60,
    rand_max: Math.min(15, Math.max(0.5, num("randmax") || 5)) * 60,
    lut: (val("lut") || "").trim(),
    // 整幅镜像只在干净底片上有意义：底片没字、出片时才烧，翻不到字。
    // 原始视频带原字幕，翻了会反；禁用态下这里再兜一层，不让旧勾选值漏过去。
    mirror: srcVer ? on("mirror") : false,
    // 字幕样式（2026-11-16 从第 2 步搬过来）：底片没有字，字是出片时按这两项画的
    newsub_style: NS_STYLES.indexOf(val("nstyle")) >= 0 ? val("nstyle") : "rise",
    newsub_motion: Math.min(1, Math.max(0, (num("nmotion") || 50) / 100)),
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

/* 这次出片会不会做「随机左右翻转」？要两个条件同时成立：
   ① 素材能翻（干净底片 / 现场换字）——原始视频带原字幕，翻了字会反，程序强制关；
   ② 全自动随机开着——关掉后镜像退化成固定全翻，不算随机翻转。
   默认两项都成立（见 writeFx 的默认翻转），正常不会弹确认。 */
function mirrorWillRun(fx) {
  const canMirror = fx.src_ver === "nosub" || fx.src_ver === "live";
  return canMirror && fx.auto_fx !== false;
}

/* 出片前的随机翻转确认：没开就用应用内确认框提醒，返回 true 才继续出片。
   这是兜底——防止像之前那样闷头出完才发现没翻。用户可改设置后再点。 */
async function confirmMirrorIfOff(fx) {
  if (mirrorWillRun(fx)) return true;
  const why = (fx.src_ver === "nosub" || fx.src_ver === "live")
    ? "你关掉了「全自动随机」，这次画面不会按集随机翻转，去重会少一层。"
    : "这次用的是「原始视频」。它自带字幕，画面一旦左右翻转，字幕会跟着反过来，所以程序不会做镜像。";
  return askConfirm({
    title: "这次不做随机左右翻转",
    body: [
      why,
      "想要随机翻转：把「用哪份视频」选成可镜像的那种（干净底片，或出片时现场换字），并保持「全自动随机」开启。",
      "确定就按没有镜像的方式继续出片吗？",
    ],
    okText: "仍要出片", cancelText: "回去改设置",
  });
}

function writeFx(p, fx, opts) {
  fx = fx || {};
  opts = opts || {};
  const el = (id) => document.getElementById(p + id);
  const put = (id, v) => { const e = el(id); if (e) e.value = v; };
  const chk = (id, v) => { const e = el(id); if (e) e.checked = !!v; };
  // 默认开启随机翻转（2026-11-19 用户定「每次都默认开」）：主面板回填时，
  // 只要这个项目已经做了第 2 步干净底片，就把「用哪份视频」默认选到可镜像的
  // 「干净底片」、把「整幅左右镜像」默认勾上——全自动随机本来就默认开，这样
  // 按集随机翻转开箱即生效。没有干净底片的项目保持原始视频（翻了字会反，
  // 程序会强制关），不硬塞。只有主面板传 defaultMirror，单套/直出面板不受影响。
  const nsFiles = (((ST.proj || {}).nosub || {}).files) || {};
  const hasNosub = Object.keys(nsFiles).length > 0;
  const defaultMirror = !!opts.defaultMirror && hasNosub;
  // 全自动随机：默认开；老项目没存这个字段时也当开（写了 false 才是关）
  chk("autofx", fx.auto_fx !== false);
  // 出片增强：看点字幕 / 钩子卡 2026-11-21 已删（面板上没有这两项了），放大 / 变速默认开
  chk("zoom", fx.zoom !== false);
  // 段间转场（2026-11-18 默认开）：没存过 → 默认 0.7s 开；显式写 0 才是关
  const rawTs = fx.trans_sec;
  const ts = (rawTs === undefined || rawTs === null || rawTs === "")
    ? 0.7 : (Number(rawTs) || 0);
  chk("trans", ts > 0);
  put("transsec", ts > 0 ? Math.min(1.5, Math.max(0.4, ts)) : 0.7);
  chk("speed", fx.buffer_speed !== false);
  // 配乐默认开（2026-11-18 用户定）：只有显式写了 false 才是关，老项目没这字段也当开
  chk("bgm", fx.bgm !== false);
  // 配乐来源两档回填（2026-11-19 去掉自动配乐）：folder / file。
  // 老项目可能存 auto 或只有 bgm_auto/bgm_path：auto 一律落到 folder（自动配乐已移除），
  // 只有「显式指定一首歌」(bgm_auto=false 且给了路径、或 bgm_mode=file) 才回填到 file。
  const bgmMode = (fx.bgm_mode === "file")
    || (fx.bgm_mode !== "folder" && fx.bgm_mode !== "auto"
        && (fx.bgm_auto === false || (fx.bgm_auto == null && !!fx.bgm_path)))
    ? "file" : "folder";
  if (el("bgmfolder")) el("bgmfolder").checked = bgmMode === "folder";
  if (el("bgmmanual")) el("bgmmanual").checked = bgmMode === "file";
  put("bgmpath", fx.bgm_path || "");
  // 配乐文件夹回填：项目自己存过的优先；没存过就回填「上次选的」(hs_bgm_dir)。
  // 这样新项目打开也默认用上次的文件夹，不用每次重选。
  let lastBgmDir = "";
  try { lastBgmDir = localStorage.getItem("hs_bgm_dir") || ""; } catch (e) {}
  put("bgmdir", fx.bgm_dir || lastBgmDir);
  put("bvol", Math.round((fx.bgm_vol == null ? 0.35 : fx.bgm_vol) * 100));
  // 去掉原片自带音乐：默认开（2026-11-19 用户定），只有显式 false 才不勾（老项目没这个字段也当开）
  chk("voiceiso", fx.voice_iso !== false);
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
  // 默认翻转：有干净底片、主面板首次打开时勾上镜像；否则按项目存的值。
  // 项目里显式存过 mirror=false 的用户是主动关的，尊重其选择，不强行覆盖。
  chk("mirror", fx.mirror === false ? false : (defaultMirror || !!fx.mirror));
  // 开头镜像默认开、上下翻（2026-11-18 用户定）：只有显式写了 false 才是关，
  // 方向只有显式写了 h（左右）才用 h，其余（含老项目没字段）一律上下翻 v。
  chk("fmirror", fx.first_mirror !== false);
  put("fmirrordir", fx.first_mirror_dir === "h" ? "h" : "v");
  // 质感四项：老项目没有这些字段 → 显示出厂默认（开在无感档），
  // 与渲染端 FX_DEFAULTS 必须一致，否则面板显示和成片对不上
  const FDEF = { stretch: 1.015, glow: 0.05, grain: 0.10, vignette: 0.08 };
  put("stretch", Math.round((fx.stretch == null ? FDEF.stretch : fx.stretch) * 1000) / 10);
  put("glow", Math.round((fx.glow == null ? FDEF.glow : fx.glow) * 100));
  put("grain", Math.round((fx.grain == null ? FDEF.grain : fx.grain) * 100));
  put("vig", Math.round((fx.vignette == null ? FDEF.vignette : fx.vignette) * 100));
  // 用哪份视频（2026-11-16 收成两档）：老项目存的 "live" 就是「出片时烧自己的
  // 字幕」，和现在的「干净底片 + 现场加字幕」是同一件事（后者更快），映射过去；
  // 没有这个字段的老项目仍按原始视频显示，不悄悄改掉已经出好的片子。
  // 默认翻转：有干净底片、主面板首次打开时默认选「干净底片（可镜像）」；
  // 项目显式存过原始视频（src_ver === ""）说明用户上次主动选过——按「每次都默认开」
  // 仍回到干净底片；只有真正没底片时才停在原始视频。
  put("srcver", (defaultMirror || fx.src_ver === "nosub" || fx.src_ver === "live") ? "nosub" : "");
  // 字幕样式（从第 2 步搬过来）：老项目没有这两个字段 → 用同一套出厂默认
  put("nstyle", NS_STYLES.indexOf(fx.newsub_style) >= 0 ? fx.newsub_style : "rise");
  put("nmotion", Math.round((fx.newsub_motion == null ? 0.5 : fx.newsub_motion) * 100));
  // 指纹强度：老项目没有这个字段 → 标准档（新抽的画面参数，观感和老片一致）
  put("fp", ["soft", "std", "strong"].includes(fx.fp_level) ? fx.fp_level : "std");
  fxSync(p);
}

/* 标签、说明、子区显隐：一个函数管齐，改任何控件都调它 */
function fxSync(p) {
  const el = (id) => document.getElementById(p + id);
  // 直出面板（d-）没有「集」，整幅镜像按 60 秒时间窗随机；AI 编辑面板（fx-）
  // 仍按集随机。文案按前缀分流，逻辑本身互不干扰（2026-11-18）。
  const isDirect = p === "d-";
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
  // 配乐文件夹：记住上次选的那个（全局 localStorage，跨项目通用）。
  // 点选文件夹后、手动输入后都会走到这里；只在非空时记，清空输入框不算改选，
  // 不抹掉记忆。后端 pickmem 的 bgm_dir key 白名单缺失，这里走纯前端兜底。
  const bgmDirEl = el("bgmdir");
  if (bgmDirEl && String(bgmDirEl.value || "").trim()) {
    try { localStorage.setItem("hs_bgm_dir", String(bgmDirEl.value).trim()); } catch (e) {}
  }
  // 去掉原片音乐：勾上才解释「会发生什么」，顺带提醒要不要配套乐
  const vi = el("voiceiso"), vitip = el("voiceisotip");
  if (vitip) {
    if (vi && vi.checked) {
      const b = el("bgm");
      vitip.classList.remove("hidden");
      vitip.textContent = "出片时把原片自带的音乐压掉，只留人声和一点点现场底（关门、碗筷这些会变轻）。"
        + (b && b.checked
          ? "上面勾了配乐，会用自己的音乐铺满全片，正好接上。"
          : "没勾配乐的话成片会比较「干」，建议把上面的「加配乐」也勾上。")
        + "第一次出片要多等几分钟（跟片长成正比），同样素材再出直接用上次的结果。";
    } else if (isDirect && el("bgm") && el("bgm").checked) {
      // 直出的素材常常是已经配过乐的成片：这时候不勾「去掉原片自带音乐」，
      // 原片音乐是满电平，新配乐在它下面，听起来就像「配乐没换掉」。
      vitip.classList.remove("hidden");
      vitip.textContent = "注意：原片自带的音乐不会被去掉，会和这里选的配乐一起响（原片那条更响，"
        + "听起来像没换成）。只想留自己的音乐，把左边这个开关勾上。";
    } else {
      vitip.classList.add("hidden");
    }
  }
  // 段间转场：勾上才显示时长滑杆和说明，滑杆数字实时跟着动
  const tr = el("trans"), trline = el("transline"), trtip = el("transtip");
  const trsec = el("transsec"), trv = el("transv");
  const trOn = tr ? tr.checked : false;
  if (trv && trsec) trv.textContent = Number(trsec.value).toFixed(1) + " 秒";
  if (trline) trline.classList.toggle("hidden", !trOn);
  if (trtip) {
    trtip.classList.toggle("hidden", !trOn);
    if (trOn) trtip.textContent = isDirect
      ? "已开启：出片时先自动找出画面切换的位置，再在每个镜头之间插一段虚化过渡。成片会比原来长一些，台词一句不会少。"
      : "已开启：排好的每个剪辑点之间都会插一段虚化过渡。成片会比原来长一些，台词一句不会少。";
  }
  // 全自动随机：开着就不用管下面；关掉自动展开手动区（"点了会发生什么"说人话）
  const af = el("autofx"), fold = el("manualfold");
  const autoOn = af ? af.checked : true;
  if (fold) fold.open = !autoOn;
  // 指纹强度说明：按全自动是否开启分两套话术
  const fp = el("fp"), fptip = el("fptip");
  if (fptip) {
    const zv = el("zoom");
    if (!autoOn) {
      fptip.textContent = "现在是手动模式：出片按你展开设置的值来，不再自动换画面。想省事就把上面「开启全自动随机」勾回去。";
    } else if (zv && !zv.checked) {
      fptip.textContent = "「画面放大」没勾，每段几何微抖用不上，只剩噪点、暗角、光晕的轻微浮动。想让每段画面都不同，把展开区里的「画面放大」勾上。";
    } else if (fp && fp.value === "soft") {
      fptip.textContent = "变化很小：同一批里能看出细微差别，几乎不影响观感。想让每套差别明显一点就选「标准」。";
    } else if (fp && fp.value === "strong") {
      fptip.textContent = "变化最明显：放大、取景偏移、轻微旋转都更大，每套一眼能看出构图不同。构图很满、边缘有字的片子可能切到一点，介意就退回「标准」。";
    } else {
      fptip.textContent = "点出片时程序自动给每段换一组画面：放大/变形每段差一点点（肉眼无感）、调色每区换档、字幕动效按集随机，"
        + (isDirect ? "整幅镜像每 60 秒翻一次或不翻（各段独立）。" : "选干净底片时整幅镜像也按集随机。")
        + "同一批每套都不一样，只动画面，不改时长、不改台词。";
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
  // 配乐两档（自动配乐已移除）：只显示当前那档的输入框，避免"选了两档不知道哪个生效"
  const bgmModeEl = document.querySelector(`input[name="${p}bgmmode"]:checked`);
  const bgmMode = bgmModeEl ? String(bgmModeEl.value) : "folder";
  const manualBox = el("bgmmanualbox");
  if (manualBox) manualBox.classList.toggle("hidden", bgmMode !== "file");
  const folderBox = el("bgmfolderbox");
  if (folderBox) folderBox.classList.toggle("hidden", bgmMode !== "folder");
  // 整幅镜像勾选框在随机卡里，但「能不能翻」由素材定：原始视频带原字翻了会反 → 禁用并取消勾选；
  // 干净底片没字、出片时才烧 → 可勾。随机开着时按集自动决定，勾选框不当最终值，只作关掉后的固定值。
  const pickv = el("srcver") ? String(el("srcver").value) : "";
  const canMirror = pickv === "nosub" || pickv === "live";
  const mv = el("mirror"), mbox = el("mirrorbox"), mtip = el("mirrortip");
  if (mv && mbox) {
    mbox.classList.toggle("disabled", !canMirror);
    mv.disabled = !canMirror;
    if (!canMirror) mv.checked = false;
    if (mtip) {
      mtip.innerHTML = !canMirror
        ? "<b>用原始视频没有镜像</b>：画面上带着原来的字幕，整幅翻字会反，程序已强制关掉。想翻又不想字反，把「用哪份视频」改成干净底片。"
        : autoOn
          ? (isDirect
            ? "<b>现在按时间窗随机</b>：这条片子每 60 秒翻一次或不翻、各段独立（开头翻、接着不翻……）。这个勾选先不用管，关掉随机后才按它来。"
            : "<b>现在按集随机</b>：第 1 集整集翻、第 2 集整集不翻……同一集前后一致。这个勾选先不用管，关掉随机后才按它来。")
          : "<b>现在是手动模式</b>：勾上就整片都翻。底片没字，顺序是<b>切段 → 镜像 → 烧字幕</b>，字是翻完才画的，不会反。";
    }
  }
  // 常驻状态摘要：展开区收起时也能看到当前用的素材和镜像状态（"点了会发生什么"说人话）
  const fst = el("fxstatus");
  if (fst) {
    const srcTxt = isDirect ? "现场换字" : (canMirror ? "干净底片" : "原始视频");
    let mirrorTxt;
    if (!canMirror) mirrorTxt = "不镜像（原片带字，翻了会反）";
    else if (autoOn) mirrorTxt = isDirect ? "整幅镜像每 60 秒随机" : "整幅镜像按集随机";
    else mirrorTxt = mv && mv.checked ? (isDirect ? "整幅镜像整条都翻" : "整幅镜像每集都翻") : "不镜像";
    fst.innerHTML = "当前：<b>" + srcTxt + "</b> · <b>" + mirrorTxt + "</b> · 字幕动效按集随机"
      + (isDirect
        ? "　<span class=\"fx-status-more\">改镜像、字幕动效？点下面展开</span>"
        : "　<span class=\"fx-status-more\">改素材、镜像、字幕动效？点下面展开</span>");
  }
  // 字幕样式的幅度标签：0 = 几乎不动，100 = 明显
  const nmv = el("nmotionv"), nm = el("nmotion");
  if (nmv && nm) {
    const n = Number(nm.value) || 0;
    nmv.textContent = n <= 0 ? "几乎不动" : n >= 90 ? "明显"
      : n < 30 ? "很轻" : n < 70 ? "推荐" : "偏明显";
  }
  // 第 3 组：开头闪镜独立成组，勾上才展开方向选择和说明；不勾就是原样出片
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
    // 第 2 步已经备好了、这一套却还在用原片时，提醒一句（默认值不动，免得把老项目悄悄改掉）
    const proj = ST.proj || {};
    const nsf = Object.keys(((proj.nosub || {}).files) || {}).length;
    const epsN = (proj.episodes || []).length;
    const nsReady = epsN > 0 && nsf >= epsN;
    const burned = (proj.nosub || {}).burned !== false;
    vtip.textContent = pickv === "nosub"
      ? (burned && nsReady
        ? "第 2 步的底片还是旧版（字幕已经烧进画面），先回第 2 步重新生成一次，不然会叠出两层字。"
        : "从第 2 步做好的干净底片里切段：底片上一行字都没有，出片时按台词把字幕烧上去。"
          + (nsReady ? "" : "（这一集还没生成底片，出片前会拦下来说缺哪几集。）"))
      : nsReady
        ? "从导入的原始视频里切段，画面还是原样（带着原来的字幕）。第 2 步已经有干净底片，要用它就在上面改选。"
        : "从导入的原始视频里切段，画面保持原样（带着原来的字幕），字幕改不了、也不能镜像。";
  }
}

function fxWire(p) {
  // 勾选框：一变就同步（随机子区、BGM 子区、强度说明都在这）
  ["autofx", "title", "zoom", "hook", "speed", "bgm", "bgmfolder", "bgmmanual", "mirror", "grade", "srcver",
   "fmirror", "fmirrordir", "fp", "voiceiso", "trans"].forEach((id) => {
    const e = document.getElementById(p + id);
    e && e.addEventListener("change", () => fxSync(p));
  });
  ["bvol", "gint", "stretch", "glow", "grain", "vig", "randmin", "randmax", "bgmpath", "bgmdir", "lut", "transsec"]
    .forEach((id) => {
      const e = document.getElementById(p + id);
      e && e.addEventListener("input", () => fxSync(p));
      e && e.addEventListener("change", () => fxSync(p));
    });
  const lutBtn = document.querySelector(`[data-lutpick="${p}"]`);
  if (lutBtn) lutBtn.addEventListener("click", async () => {
    toast("已弹出选择框，选一个调色文件…");
    try {
      const cur = (document.getElementById(p + "lut") || {}).value || "";
      const r = await api(`/api/pick-file?initial=${encodeURIComponent(cur)}&key=lut`,
        { method: "POST" });
      if (r && r.path) {
        const e = document.getElementById(p + "lut");
        if (e) e.value = r.path;
        fxSync(p);
        toast("调色文件选好了，已经用上");
      }
    } catch (e) {
      toast("没选成调色文件，也可以直接把文件位置填进输入框");
    }
  });
  const bgmBtn = document.querySelector(`[data-bgmpick="${p}"]`);
  if (bgmBtn) bgmBtn.addEventListener("click", async () => {
    toast("已弹出选择框，选一首自己电脑上的音乐…");
    try {
      const cur = (document.getElementById(p + "bgmpath") || {}).value || "";
      const r = await api(`/api/pick-file?initial=${encodeURIComponent(cur)}&key=bgm`,
        { method: "POST" });
      if (r && r.path) {
        const e = document.getElementById(p + "bgmpath");
        if (e) e.value = r.path;
        fxSync(p);
        toast("配乐选好了：出片时循环铺满全片、台词响时自动压低");
      }
    } catch (e) {
      toast("没选成配乐，也可以直接把文件位置填进输入框");
    }
  });
  // 配乐文件夹：用户选一个放歌的目录，出片时每份从头里挑一首
  const bgmDirBtn = document.querySelector(`[data-bgmdirpick="${p}"]`);
  if (bgmDirBtn) bgmDirBtn.addEventListener("click", async () => {
    try {
      const cur = (document.getElementById(p + "bgmdir") || {}).value || "";
      const r = await api(`/api/pick-folder?initial=${encodeURIComponent(cur)}&key=bgm_dir`
        + `&title=${encodeURIComponent("选择音乐文件夹")}`, { method: "POST" });
      if (r && r.path) {
        const e = document.getElementById(p + "bgmdir");
        if (e) e.value = r.path;
        fxSync(p);
        toast("文件夹选好了：每份成片从里面挑一首，尽量不重复");
      }
    } catch (e) {
      toast("没选成文件夹，也可以直接把文件夹路径填进输入框");
    }
  });
}

/* 徽标上的一句话摘要：只列「和出厂默认不一样」的东西。
   fx 传的是单套覆盖（可能只有一两个字段），全空就是「跟随默认」。 */
function fxShort(fx) {
  fx = fx || {};
  const parts = [];
  if (fx.lut) parts.push("自定义调色");
  else if (fx.grade === "rand" || fx.grade_rand) {
    const lo = Math.round((fx.rand_min == null ? (fx.rand_sec == null ? 120 : fx.rand_sec) : fx.rand_min) / 60 * 2) / 2;
    const hi = Math.round((fx.rand_max == null ? (fx.rand_sec == null ? 300 : fx.rand_sec) : fx.rand_max) / 60 * 2) / 2;
    parts.push(lo >= hi ? `随机${lo}分` : `随机${lo}–${hi}分`);
  } else if (fx.grade) parts.push(`${GRADE_CN[fx.grade] || fx.grade} ${Math.round((fx.grade_int == null ? 0.65 : fx.grade_int) * 100)}%`);
  // 整幅镜像跟着素材走：原始视频不能翻（前端禁用 + 后端强制关），不写进摘要
  if (fx.mirror && (fx.src_ver === "nosub" || fx.src_ver === "live")) parts.push("镜像");
  // 开头镜像默认关，勾了才进徽标（和「用哪份素材」无关，单独一列）
  if (fx.first_mirror) parts.push(fx.first_mirror_dir === "v" ? "开头上下翻" : "开头左右翻");
  // 段间转场默认关，开了才进徽标（时长按秒说人话，不露内部字段名）
  if (Number(fx.trans_sec) > 0) parts.push("镜头过渡" + Number(fx.trans_sec).toFixed(1) + "秒");
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
  // 看点字幕 2026-11-21 随片头组一起删了，这里不再有它的偏离项
  if (fx.zoom === false) parts.push("不放大");
  // 指纹强度默认「标准」，只有改过才写进徽标
  if (fx.fp_level === "strong") parts.push("指纹强力");
  if (fx.fp_level === "soft") parts.push("指纹保守");
  // 「无钩子卡」这条 2026-11-21 删了：钩子卡恒关，留着每套徽标都会多挂一句没意义的话
  if (fx.buffer_speed === false) parts.push("缓冲原速");
  if (fx.bgm) {
    const bm = ["folder", "file"].indexOf(fx.bgm_mode) >= 0
      ? fx.bgm_mode : (fx.bgm_auto === false ? "file" : "folder");
    parts.push(bm === "file" ? "配乐·自选" : "配乐·我的文件夹");
  }
  if (fx.voice_iso) parts.push("去原片音乐");
  // 用哪份视频排在最前面；默认「原始视频」不刷屏
  if (fx.src_ver === "nosub" || fx.src_ver === "live") parts.unshift("干净底片");
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
["title", "zoom", "hook", "speed", "bgm", "bgmfolder", "bgmmanual", "bgmpath", "bgmdir", "bvol",
 "grade", "gint", "randmin", "randmax", "lut", "mirror", "stretch", "glow", "grain", "vig",
 "srcver", "nstyle", "nmotion", "fmirror", "fmirrordir", "fp"]
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
  // 配乐 BGM：文件夹由后端在出片时逐套解析；指定单曲必须当场给文件。自动配乐已移除。
  const bgmMode0 = ["folder", "file"].indexOf(fx.bgm_mode) >= 0
    ? fx.bgm_mode : (fx.bgm_auto === false ? "file" : "folder");
  if (fx.bgm && bgmMode0 === "file" && !fx.bgm_path)
    return toast("选了「指定一首歌」，先选一首自己电脑上的音乐文件");
  if (fx.bgm && bgmMode0 === "folder" && !fx.bgm_dir)
    return toast("选了「从我的文件夹随机挑」，先选一个放歌的文件夹");
  const fxNote = [
    fx.zoom ? "画面放大 103%（去重）" : "原画面",
    ...(Number(fx.trans_sec) > 0 ? [`每个镜头之间加 ${Number(fx.trans_sec).toFixed(1)} 秒过渡空境（成片会变长）`] : []),
    fx.buffer_speed ? "缓冲段加速 1.15×" : "全片原速",
    fx.bgm ? (bgmMode0 === "file"
      ? `配乐用指定的那首（音量 ${Math.round(fx.bgm_vol * 100)}%，台词响时自动压低）`
      : `配乐从我的文件夹随机挑（音量 ${Math.round(fx.bgm_vol * 100)}%，台词响时自动压低；出片前记得先选好音乐文件夹）`) : "不加配乐",
    fx.src_ver === "nosub" || fx.src_ver === "live"
      ? "素材：干净底片 + 出片时加字幕" : "素材：原始视频（带原字幕）",
    fx.first_mirror ? `开头 0.3 秒${fx.first_mirror_dir === "v" ? "上下" : "左右"}镜像` : "开头不翻",
    (() => { const f = readFx("fx-"); delete f.src_ver;
      const pic = fxShort(f);
      return pic === "默认" ? "画面：轻质感（柔光/细颗粒/轻暗角/1.5%变形，看不出）" : `画面：${pic}`; })(),
  ].join("、");
  // 每套的单独覆盖：出片增强和画面都算，只有自定义过的套才带
  const setsFx = {};
  on.forEach((pl) => { if (pl.fx && Object.keys(pl.fx).length) setsFx[String(pl.index)] = pl.fx; });
  const picNote = Object.keys(setsFx).length ? `；${Object.keys(setsFx).length} 套有单独设置` : "";
  // 随机翻转兜底确认（2026-11-19）：没开翻转先问一次，点了「回去改设置」就停。
  // 用应用内确认框，走完它才进下面的原生 confirm，两个框不会叠在一起。
  if (!(await confirmMirrorIfOff(fx))) return;
  if (!confirm(`将渲染已勾选的 ${on.length} 套（每套约 2 分钟），存到：\n${outDir}\n\n出片处理：${fxNote}${picNote}；段与段之间加 60/120 毫秒音频淡入淡出（磨掉切口的咔声）。\n\n确定？`)) return;
  const body = { save_dir: saveDir, fx, sets_fx: setsFx };
  if (!enqueueRender(p.id, body, `前面还有一趟出片在跑，这趟已排队，跑完自动开始`)) return;
  try {
    await sendRender(p.id, body);
  } catch (e) {
    toast(e.message);
  }
});

/* 常驻「打开文件夹」：出片中途/出片前都能点，打开「实际保存到」那个目录。
   把当前输入框的值传过去（可能还没落盘），没填就用项目里存的位置。 */
$("#btn-save-dir-open").addEventListener("click", async () => {
  const p = ST.proj;
  if (!p) return;
  const cur = ($("#save-dir").value || "").trim();
  try {
    const q = cur ? `?dir=${encodeURIComponent(cur)}` : "";
    await api(`/api/projects/${p.id}/open-folder${q}`, { method: "POST" });
    toast("已打开成片文件夹");
  } catch (e) {
    toast(`打不开文件夹：${e.message}`);
  }
});

$("#btn-save-dir-pick").addEventListener("click", async () => {
  const cur = ($("#save-dir").value || "").trim();
  toast("已弹出选择框，请在桌面上选一个文件夹…");
  try {
    const r = await api(`/api/pick-folder?initial=${encodeURIComponent(cur)}&key=proj_out`,
      { method: "POST" });
    if (r && r.path) {
      $("#save-dir").value = r.path;
      updateSaveDirNote();
      loadSaveDirHist();
      toast(`保存位置：${joinDir(r.path, ST.proj && ST.proj.name)}`);
    }
  } catch (e) {
    toast(`${e.message}（可以直接手动输入路径）`);
  }
});

/* 成片保存位置：同一台机器往往就那几个盘反复用，摆出最近用过的三个，
   点一下直接填进输入框（不弹框）。 */
function loadSaveDirHist() {
  return loadHistChips("hist-proj-out", "proj_out", (path) => {
    const box = $("#save-dir");
    if (!box) return;
    box.value = path;
    updateSaveDirNote();
    toast(`保存位置：${joinDir(path, ST.proj && ST.proj.name)}`);
  });
}

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
  const pid = ST.proj.id;
  // 把「会丢什么」一样样列清楚（2026-11-14 用户反馈：「全部记录」太含糊）
  const ok = await askConfirm({
    title: `删除项目「${ST.proj.name}」？`,
    body: [
      "会一起删掉：台词整理、AI 结构、分镜提示词、成片记录，以及抹字幕/词级时间/渲染分段等缓存。",
      "这个项目正在跑的任务会自动中止；发布队列里还没发的条目一并移除（已交平台的不受影响）。",
      "你硬盘里的原始视频文件不会动，已经生成的成片也不会删，但项目记录删了就找不回来。",
    ],
    okText: "删除项目",
    cancelText: "不删了",
    danger: true,
  });
  if (!ok) return;

  const doDelete = (force) => api("/api/projects/" + pid + (force ? "?force=1" : ""),
                                  { method: "DELETE" });
  let res;
  try {
    try {
      res = await doDelete(false);
    } catch (e) {
      if (e.status !== 409) throw e;   // 后端要求对「还有未发条目」再确认一次
      const again = await askConfirm({
        title: "还是有视频等着发，确认删吗？",
        body: [e.message, "确认后会把这些没发的条目一起移除；已交平台的不受影响。"],
        okText: "仍然删除", cancelText: "不删了", danger: true,
      });
      if (!again) return;
      res = await doDelete(true);
    }
  } catch (e) {
    toast("删除失败：" + e.message);
    return;                            // 失败就别清空 ST.proj，界面留在原项目上
  }

  // 回话里带清理明细，别让「删了但不知道清了什么」
  let msg = "项目已删除";
  if (res.cancelled) msg += `，中止 ${res.cancelled} 个任务`;
  if (res.jobs && res.jobs.queue) msg += `，移除 ${res.jobs.queue} 条发布条目`;
  toast(msg);
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
  // 去背景音：只有随包带了分离模型才露出这个开关（同后端的 available 判断）
  const fldDenoise = $("#fld-asr-denoise");
  if (fldDenoise) fldDenoise.classList.toggle("hidden", !c.asr_denoise_available);
  if ($("#in-asr-denoise")) $("#in-asr-denoise").checked = !!c.asr_denoise;
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
  // 去背景音：只有露出了开关才提交（模型没带时这个框是藏着的）
  if (SETTINGS && $("#in-asr-denoise") && !$("#fld-asr-denoise").classList.contains("hidden")) {
    const want = $("#in-asr-denoise").checked;
    if (want !== !!SETTINGS.asr_denoise) body.asr_denoise = want;
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
      if (body.asr_denoise !== undefined) SETTINGS.asr_denoise = body.asr_denoise;
    }
    $("#modal-settings").classList.add("hidden");
    toast("已保存");
    const st = await api("/api/status");
    ST.status = st;
    paintPills(st);
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
      const r = await api(`/api/pick-folder?initial=${encodeURIComponent(cur || "")}&key=merge`,
        { method: "POST" });
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
    try {
      await api("/api/merge/cancel", { method: "POST" });
      toast("正在取消，等当前这一对编完…");
      // 立刻把当前状态画出来，别只等下一次 1.5 秒轮询（同类问题：点了没反应）
      await refresh(false);
    } catch (e) { toast(e.message); }
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

/* ══════════════════════════════════════════════════════════════════════
   选视频直接出片（2026-11-17）
   独立面板：选视频/文件夹 → 逐条台词(转写/粘贴) → 识别字幕 → 出片。
   内部是一个打了 direct 标记的隐藏项目，复用主流程的转写/导入/检测/渲染，
   所以这里的任务进度、成片列表都从同一个项目接口读，只是自己轮询、自己画。
   ══════════════════════════════════════════════════════════════════════ */
const DP = { pid: "", timer: null, paths: [], selEp: 1, busy: false, transStopSent: false };
const dpEl = (id) => document.getElementById("dp-" + id);

function dpFmt(sec) { return fmt(sec); }

/* 第 3 步一级切换：选视频直接出片 / AI 剪辑（2026-11-17）。
   两条并列的路，进来先选走哪条。两个面板都留在 DOM 里，只切显隐——
   切来切去不丢台词、不丢设置，也不打断正在跑的任务。
   记在 localStorage：下次打开还停在上次那条路。 */
(function initRouteMode() {
  const wrap = $("#route-mode");
  if (!wrap) return;
  const KEY = "hs_step3_route";
  function apply(route, save) {
    const v = (route === "ai" || route === "direct") ? route : "ai";
    wrap.querySelectorAll(".rt-btn").forEach((b) => b.classList.toggle("on", b.dataset.route === v));
    const pd = $("#pane-direct"), pa = $("#pane-ai");
    if (pd) pd.classList.toggle("hidden", v !== "direct");
    if (pa) pa.classList.toggle("hidden", v !== "ai");
    if (save) { try { localStorage.setItem(KEY, v); } catch (e) {} }
  }
  let last = "ai";
  try { last = localStorage.getItem(KEY) || "ai"; } catch (e) {}
  apply(last, false);
  wrap.querySelectorAll(".rt-btn").forEach((b) => {
    b.addEventListener("click", () => apply(b.dataset.route, true));
  });
})();

/* 复用全局出片设置控件模板，前缀 d-；直出固定「现场换字」，素材版本不给选，
   但整幅镜像对 live 依然有效（先翻后烧字），勾选框必须露出来让人能关随机后手勾。 */
(function initDpFx() {
  const box = dpEl("fx");
  box.innerHTML = fxPanelHTML("d-");
  // 直出默认勾上「去掉原片自带音乐」（2026-11-18 用户定）：直出的素材基本都是已经
  // 配过乐的成片，不去掉就会和自己选的配乐一起响，听起来像配乐没换成。
  const viDef = document.getElementById("d-voiceiso");
  if (viDef) viDef.checked = true;
  // 素材版本固定 live：藏掉「素材版本」这一行和它的说明，但保留 select 在 DOM 里
  // （display:none 不影响 value）——readFx 靠 d-srcver=nosub 判定 canMirror、传 mirror。
  const sv = document.getElementById("d-srcver");
  if (sv) {
    sv.value = "nosub";
    const row = sv.closest("label");
    if (row) row.style.display = "none";
  }
  const svTip = document.getElementById("d-srcvertip");
  if (svTip) svTip.style.display = "none";
  // 素材版本藏了，标题别再写「由用哪份视频定」，改说镜像本身。
  const srcHdr = box.querySelector(".fx-src .fx-group-h");
  if (srcHdr) srcHdr.innerHTML = '整幅镜像（去重最强）<span class="hint">翻不翻由上面的全自动随机每 60 秒决定；关掉随机后按你勾的来</span>';
  // 镜像块所在的 fx-src 子组现在保留可见（整幅左右镜像勾选框在里面）。
  // 直出没有「集」，鼠标悬停提示改成 60 秒时间窗；只改 d- 这个实例，AI 面板不动。
  const mboxD = document.getElementById("d-mirrorbox");
  if (mboxD) mboxD.title = "整幅画面左右翻转，去重最彻底。全自动随机开着时，这条片子每 60 秒翻一次或不翻、各段独立决定（没有「集」可分组）；只有现场换字能用（字是翻完才烧的，不会反）。关掉随机后，这个勾就是你选的固定效果。";
  fxWire("d-");
  fxSync("d-");
  // 直出面板的设置必须落库（2026-11-18 修）：以前只 fxSync 不保存，刷新页面全回默认，
  // 用户勾过的「去掉原片自带音乐」下一趟就丢了——成片里原片 BGM 还在、新配乐被压在下
  // 面，用户看到的就是「怎么还是原片的音乐」。存到 batch.fx_default_direct，
  // 跟 AI 编辑出片的 fx_default 分开（两边字段语义不同，混存会串味）。
  let _dFxT = null;
  const saveDpFx = () => {
    clearTimeout(_dFxT);
    _dFxT = setTimeout(async () => {
      if (!DP.pid) return;
      try {
        await api(`/api/projects/${DP.pid}/fx-default`, {
          method: "PUT", body: JSON.stringify({ fx: readFx("d-"), scope: "direct" }),
        });
      } catch (e) { /* 存不上不打断调参，出片时会再校验 */ }
    }, 700);
  };
  box.addEventListener("input", saveDpFx);
  box.addEventListener("change", saveDpFx);
  const note = document.createElement("p");
  note.className = "fx-tip";
  note.textContent = "这个模式固定用原片现场换字幕，素材版本不用选。整幅镜像可以用：全自动随机开着时每 60 秒自动决定翻不翻，关掉随机后按你勾的来。画面、配乐、字幕样式照常在这里调。";
  box.insertBefore(note, box.firstChild);
})();

/* 成片保存位置：输入框 + 最近用过 */
function dpUpdateSaveNote() {
  // 交给统一闸门：台词没好时按钮旁要继续显示原因，不能被保存提示冲掉
  if (DP.curProj) dpRenderGate(DP.curProj);
  else dpEl("render-note").textContent = "不填则存到程序默认的成片位置";
}
dpEl("save-dir").addEventListener("input", () => {
  try { localStorage.setItem(DP_SAVEDIR_KEY, (dpEl("save-dir").value || "").trim()); } catch (e) {}
  dpUpdateSaveNote();
});

const DP_SAVEDIR_KEY = "hs_direct_savedir";
async function dpPickOutdir() {
  const cur = (dpEl("save-dir").value || "").trim();
  toast("已弹出选择框，选成片要存到哪个文件夹…");
  try {
    const r = await api(`/api/pick-folder?initial=${encodeURIComponent(cur)}&key=direct_out`, { method: "POST" });
    if (r && r.path) {
      dpEl("save-dir").value = r.path;
      try { localStorage.setItem(DP_SAVEDIR_KEY, r.path); } catch (e) {}
      dpUpdateSaveNote();
    }
  } catch (e) { toast(e.message); }
}
dpEl("pick-outdir").addEventListener("click", dpPickOutdir);

/* 选视频 / 文件夹：只收集路径，点完由 create/reselect 建项目 */
async function dpPick(kind) {
  toast(kind === "folder" ? "已弹出选择框，选放视频的文件夹…" : "已弹出选择框，选视频（可按住 Ctrl/Shift 多选）…");
  try {
    if (kind === "folder") {
      const r = await api("/api/pick-folder?key=direct_src", { method: "POST" });
      if (r && r.path) DP.paths = [r.path];
    } else {
      // 选视频走专用接口：只列 mp4/mov/mkv、可多选。旧的 /api/pick-file 是
      // 音频过滤器 + 单选，进文件夹看不到视频（2026-11-18 用户反馈）。
      const r = await api("/api/pick-videos?key=direct_src_video", { method: "POST" });
      if (r && r.paths && r.paths.length) DP.paths = r.paths;
    }
    if (!DP.paths.length) return;
    await dpCreateOrReselect();
  } catch (e) { toast(e.message); }
}
dpEl("pick-files").addEventListener("click", () => dpPick("file"));
dpEl("pick-folder").addEventListener("click", () => dpPick("folder"));

async function dpCreateOrReselect() {
  if (DP.busy) { toast("正在处理，等这一步做完再选"); return; }
  try {
    let p;
    if (DP.pid) {
      p = await api(`/api/direct/${DP.pid}/reselect`, {
        method: "POST", body: JSON.stringify({ paths: DP.paths }),
      });
    } else {
      p = await api("/api/direct/create", {
        method: "POST", body: JSON.stringify({ paths: DP.paths }),
      });
    }
    DP.pid = p.id;
    dpEl("reselect").classList.remove("hidden");
    dpEl("result").classList.add("hidden");
    dpRender(p);
    // 建项目/重选会自动转写，开轮询跟进度
    dpStartPoll();
    toast(`已加入 ${(p.episodes || []).length} 个视频，正在自动整理台词`);
  } catch (e) { toast(e.message); }
}
dpEl("reselect").addEventListener("click", async () => {
  if (!DP.pid) { toast("先选视频"); return; }
  const running = !!(((DP.curProj || {}).task || {}).kind);
  if (running) {
    toast("现在有任务在跑，先点「中止整理」或等它跑完再重选");
    return;
  }
  const ok = await askConfirm({
    title: "重新选视频？",
    body: [
      "现在这批视频的台词、字幕检测和成片记录都会清掉，换成新选的视频。",
      "原来的视频文件不会删除。",
    ],
    okText: "重新选视频", cancelText: "取消", danger: true,
  });
  if (!ok) return;
  DP.paths = [];
  DP.transStopSent = false;
  // dpPick 选完会自己走 create/reselect：DP.pid 还在，所以用重选口。
  await dpPick("file");
});

/* 把直出面板恢复成这个项目上次用的设置（没有就按出厂默认）。
   素材版本那一行是隐藏的，写回后必须再钉回 nosub：readFx 靠它判断镜像资格。 */
function dpLoadFx(p) {
  const saved = (p.batch || {}).fx_default_direct || null;
  writeFx("d-", saved || {});
  const sv = document.getElementById("d-srcver");
  if (sv) sv.value = "nosub";
  // 「去掉原片自带音乐」在直出里默认开：没存过、或存的那份里没有这个键（老记录）都算默认开；
  // 用户显式取消过（存了 false）才按 false 来。
  const vi = document.getElementById("d-voiceiso");
  if (vi && (!saved || !("voice_iso" in saved))) vi.checked = true;
  fxSync("d-");
}

/* 画视频列表 + 每集台词状态 */
function dpRender(p) {
  if (!p) return;
  DP.curProj = p;
  // 面板参数只在「换了一个项目」时回填一次：dpRender 会被轮询/刷新反复调用，
  // 每次都写一遍会把用户正在拖的滑杆按回去。
  if (DP.fxLoadedFor !== p.id) {
    DP.fxLoadedFor = p.id;
    dpLoadFx(p);
  }
  const eps = p.episodes || [];
  const files = dpEl("files");
  const listBusy = !!((p.task || {}).kind);   // 出片/整理中不动列表，免得和写盘打架
  files.innerHTML = eps.map((e) =>
    `<div class="dp-file" data-ep="${e.index}" title="${esc(e.file)}">` +
    `<span class="dp-ic">▶</span>` +
    `<span class="dp-nm">${esc(e.file)}</span>` +
    `<span class="dp-dur">${dpFmt(e.duration)}</span>` +
    `<button class="dp-file-act" data-act="open" title="用系统播放器打开这个视频"${listBusy ? " disabled" : ""}>打开</button>` +
    `<button class="dp-file-act rm" data-act="remove" title="从这次出片里移除，不删原视频文件"${listBusy ? " disabled" : ""}>移除</button>` +
    `</div>`
  ).join("") || `<span class="hint">还没选视频</span>`;
  // 事件挂在容器上（用 onclick 赋值，重画时不会累积监听器）。
  files.onclick = async (ev) => {
    const btn = ev.target.closest("button[data-act]");
    if (!btn || btn.disabled) return;
    const row = btn.closest(".dp-file");
    if (!row) return;
    const idx = Number(row.dataset.ep);
    const name = (row.querySelector(".dp-nm") || {}).textContent || "";
    if (btn.dataset.act === "open") {
      try {
        await api(`/api/direct/${DP.pid}/open-source`,
                  { method: "POST", body: JSON.stringify({ index: idx }) });
        toast("已打开，看完关掉播放器继续就行");
      } catch (err) { toast(err.message); }
      return;
    }
    if (!confirm(`把「${name}」从这次出片里移除？\n\n` +
                 "只从项目里移除；原来的视频文件和已经做好的成片都不会删除。")) return;
    btn.disabled = true;
    try {
      const fresh = await api(`/api/direct/${DP.pid}/remove-episode`,
        { method: "POST", body: JSON.stringify({ index: idx }) });
      DP.curProj = fresh;
      if (DP.selEp === idx) DP.selEp = ((fresh.episodes || [])[0] || {}).index || 1;
      dpRender(fresh);
      toast("已移除；原视频文件没有删除");
    } catch (err) { toast(err.message); btn.disabled = false; }
  };

  const epbox = dpEl("eps");
  epbox.innerHTML = eps.map((e) => {
    const n = (e.lines || []).length;
    const st = n > 0
      ? `<span class="dp-ep-state ok">${n} 句台词</span>`
      : (e.status === "error" ? `<span class="dp-ep-state err">转写失败</span>`
        : `<span class="dp-ep-state">还没台词</span>`);
    const on = DP.selEp === e.index ? " on" : "";
    return `<div class="dp-ep${on}" data-ep="${e.index}">` +
      `<span class="dp-ep-n">${e.index}</span>` +
      `<span class="dp-ep-nm">${esc(e.file)}</span>${st}` +
      `</div>`;
  }).join("") || `<span class="hint">选了视频后在这里准备台词</span>`;

  epbox.querySelectorAll(".dp-ep").forEach((row) => {
    row.addEventListener("click", () => {
      DP.selEp = Number(row.dataset.ep);
      dpRender(p);
    });
  });

  // 常驻主卡：把"已在自动整理"和当前进度摆在最显眼处，别让人第一反应去粘贴。
  const done = eps.filter((e) => (e.lines || []).length).length;
  const failed = eps.filter((e) => e.status === "error").length;
  const t = p.task || {};
  const stateBox = dpEl("auto-state");
  const bar = dpEl("auto-bar"), barFill = dpEl("auto-bar-fill");
  const aligned = !!((p.nosub || {}).spans || {}).key;
  // 进度条只在转写/自动对齐阶段出现；其它状态收起，避免摆一根空条。
  const showBar = (on, pct) => {
    bar.classList.toggle("hidden", !on);
    if (on && pct != null) barFill.style.width = Math.max(0, Math.min(100, Math.round(pct))) + "%";
  };
  // 出片进度（第 3 步自己那块）：只在 batch 任务在跑时出现，msg 已是
  // 「第 k/N 套（unit）：正在…」。中止按钮同样只在出片时出现。
  const rState = dpEl("render-state"), rBar = dpEl("render-bar"), rFill = dpEl("render-bar-fill");
  const rStop = dpEl("stop");
  if (t.kind === "batch") {
    // 失败是「已结束」的状态：不能继续亮着中止按钮、摆一根假的 0% 进度条。
    const runFailed = !!t.error;
    if (rState) {
      rState.classList.remove("hidden");
      rState.innerHTML = `<b>${runFailed ? "出片失败" : "正在出片"}</b>` +
        (t.msg && t.msg !== "失败" ? ` · ${esc(String(t.msg))}` : "") +
        (runFailed ? ` · ${esc(String(t.error))}` : "") +
        (!runFailed && t.pct != null ? ` · ${Math.round(t.pct)}%` : "");
    }
    if (rBar) {
      rBar.classList.toggle("hidden", runFailed);
      if (!runFailed && t.pct != null && rFill) rFill.style.width = Math.max(0, Math.min(100, Math.round(t.pct))) + "%";
    }
    if (rStop) rStop.classList.toggle("hidden", runFailed);
  } else {
    if (rState) rState.classList.add("hidden");
    if (rBar) rBar.classList.add("hidden");
    if (rStop) rStop.classList.add("hidden");
  }
  if (!eps.length) {
    stateBox.textContent = "";
    showBar(false);
  } else if (t.kind === "transcribe") {
    stateBox.innerHTML = `<b>正在自动整理台词</b> · ${done}/${eps.length} 个视频` +
      (t.pct != null ? ` · ${Math.round(t.pct)}%` : "") +
      (t.msg ? ` · ${esc(String(t.msg))}` : "");
    showBar(true, t.pct);
  } else if (t.kind === "analyze_direct") {
    stateBox.innerHTML = `<b>台词整理好了，正在自动识别字幕并对齐时间</b>` +
      (t.pct != null ? ` · ${Math.round(t.pct)}%` : "") +
      (t.msg ? ` · ${esc(String(t.msg))}` : "");
    showBar(true, t.pct);
  } else if (t.kind === "batch") {
    // 出片进度归位（2026-11-18）：进度条和文案属于第 3 步「出片设置」，不再挤在
    // 第 2 步「整理台词」卡里。整理台词卡这会儿只说台词已就绪，各管各的事。
    stateBox.innerHTML = `<b>${done}/${eps.length} 个视频的台词都整理好了</b>` +
      (aligned ? ` · 字幕时间也对齐好了` : ` · 正在做最后的字幕对齐…`);
    showBar(false);
  } else if (failed) {
    stateBox.innerHTML = `<b>${done}/${eps.length} 个视频整理好了，${failed} 个失败</b> · 可以重新整理或粘贴台词`;
    showBar(false);
  } else if (done === eps.length) {
    // C：逐集字幕区域识别失败的，出片会退回固定位置抹除——不能静默，告诉用户哪几集。
    const spanFail = (((p.nosub || {}).spans || {}).fail) || [];
    stateBox.innerHTML = `<b>${done}/${eps.length} 个视频的台词都整理好了</b>` +
      (aligned ? ` · 字幕时间也对齐好了，可以出片` : ` · 正在做最后的字幕对齐…`) +
      (spanFail.length ? `<br><span style="color:var(--warn,#e0a030)">${spanFail.length} 集没量到字幕区域，出片时那几集用固定位置抹除</span>` : "");
    showBar(false);
  } else {
    stateBox.innerHTML = `<b>${done}/${eps.length} 个视频已有台词</b> · 其余正在或等待自动整理`;
    showBar(false);
  }
  // 整理台词中止按钮（2026-11-19）：转写/自动对齐阶段亮出来，其它状态收起。
  // 这是直出面板专属按钮，和出片中止（dp-stop）分开，避免用户想停转写却点到出片。
  const tsStop = dpEl("transcribe-stop");
  if (tsStop) {
    const transBusy = t.kind === "transcribe" || t.kind === "analyze_direct";
    if (!transBusy) DP.transStopSent = false;   // 任务已结束，复位按钮
    tsStop.classList.toggle("hidden", !transBusy);
    tsStop.disabled = !!DP.transStopSent;
    tsStop.textContent = DP.transStopSent ? "中止中…" : "中止整理";
  }
  // 开始 / 继续整理入口（2026-11-20）：中止后没转好的集是 pending，不算 error，
  // 旧代码只在 failed>0 时才亮按钮，用户中止后就没有入口把剩下的集做完。
  // 按真实状态给入口：有没台词的集→继续转写；台词齐了但字幕区域没生成→重新对齐。
  // 任务在跑时照旧收起（点下去只会被后端 409 拒掉，没意义）。
  const startBtn = dpEl("transcribe");
  if (startBtn) {
    const left = eps.length - done;
    const running = !!t.kind;
    const needAlign = !left && eps.length > 0 && !aligned;
    startBtn.classList.toggle("hidden", running || !(left > 0 || needAlign));
    startBtn.textContent = left > 0
      ? `开始整理（还剩 ${left} 个）`
      : needAlign ? "重新对齐字幕时间" : "开始整理台词";
    startBtn.title = left > 0
      ? "只整理还没有台词的视频，已经做好的不动"
      : "台词已经好了，但字幕位置没识别好；重新扫一次并对齐时间";
  }
  // 对齐进度已在上面的主卡 stateBox 里显示；旧的 analyze-note 提示随
  // 「重新对齐字幕时间」按钮一起删了（2026-11-18），别再引用那个 null 元素，
  // 否则整段脚本从这儿抛错中断、后面的事件绑定和刷新恢复全失效。
  dpRenderGate(p);

  // 成片列表：直出项目的 sets 就是每条视频的成片
  const sets = p.sets || [];
  if (sets.length) {
    dpEl("result").classList.remove("hidden");
    dpEl("sets").innerHTML = sets.map((s) => {
      const out = s.output || "";
      const nm = out ? out.split(/[\\/]/).pop() : "";
      return `<div class="dp-set"><span class="dp-ok">✓</span><span style="flex:1">${esc(nm)}</span>` +
        (out ? `<a href="#" class="dp-open-one" data-set="${s.index}">打开文件</a>` : "") + `</div>`;
    }).join("");
    dpEl("sets").querySelectorAll(".dp-open-one").forEach((a) => {
      a.addEventListener("click", async (ev) => {
        ev.preventDefault();
        try { await api(`/api/projects/${DP.pid}/open-video?set=${a.dataset.set}`, { method: "POST" }); }
        catch (e) { toast("打不开文件，去成片文件夹里找找"); }
      });
    });
  }
}

function dpTaskText(t) {
  if (!t || !t.kind) return "";
  const name = { transcribe: "整理台词", analyze_direct: "识别字幕并对齐时间", batch: "出片" }[t.kind] || t.kind;
  if (t.error) return `${name}失败：${String(t.error).slice(0, 80)}`;
  return `${name}${t.pct != null ? " " + Math.round(t.pct) + "%" : ""}${t.msg ? " · " + t.msg : ""}`;
}

/* 出片闸门：条件跟后端 direct.ready_to_render 对齐（任务在跑 / 没台词 / 没识别字幕位置）。
   没就绪就置灰按钮并把原因写在按钮旁边，别让用户点了才收到一句报错。 */
function dpRenderGate(p) {
  const btn = dpEl("render"), note = dpEl("render-note");
  if (!btn || !note) return;
  // 「每个视频出几份」：回填项目里存的份数。用户没改过就是 1。
  const cps = dpEl("copies");
  if (cps) {
    const n = Math.min(5, Math.max(1, Number(p && p.copies) || 1));
    cps.value = String(n);
  }
  const eps = (p && p.episodes) || [];
  const t = (p && p.task) || {};
  const done = eps.filter((e) => (e.lines || []).length).length;
  const failed = eps.filter((e) => e.status === "error").length;
  const aligned = !!(((p && p.nosub) || {}).spans || {}).key;
  let why = "";
  if (!eps.length) why = "先选视频，选完会自动整理台词";
  else if (t.kind === "transcribe") why = "正在整理台词，整理好才能出片";
  else if (t.kind === "analyze_direct") why = "正在识别字幕并对齐时间，完成后才能出片";
  else if (t.kind === "batch" && !t.error) why = "正在出片，等这一批做完";
  else if (!done) why = failed ? "台词都整理失败了，先重新整理或粘贴台词" : "还没有台词，整理好才能出片";
  else if (!aligned) why = "字幕位置还没识别好，等它自动对齐完成";
  btn.disabled = !!why;   // 只有「未就绪」才禁用；出片失败后仍可点重试
  // 出片失败（批次结束、task 已清空）：把失败条数摆出来，别让提示退回
  // 「成片会存到这个文件夹」——那会让人以为出片成功但没找到文件。
  // 注意这只是提示、不禁用按钮（失败后要点它重试）。
  const fails = ((p && p.batch) || {}).failures || [];
  note.textContent = why || (fails.length
    ? `上批有 ${fails.length} 条没成功：${String(fails[0].error || "").slice(0, 60)}`
    : ((dpEl("save-dir").value || "").trim()
      ? "成片会存到这个文件夹" : "不填则存到程序默认的成片位置"));
}

/* 轮询：只看这个直出项目，进度任务字段和主流程一致 */
function dpStartPoll() { if (!DP.timer) DP.timer = setInterval(dpPoll, 1500); }
function dpStopPoll() { if (DP.timer) { clearInterval(DP.timer); DP.timer = null; } }
async function dpPoll() {
  if (!DP.pid) { dpStopPoll(); return; }
  try {
    const p = await api("/api/direct/" + DP.pid);
    dpRender(p);
    const busy = !!(p.task && p.task.kind);
    if (!busy) dpStopPoll();
  } catch (e) { dpStopPoll(); }
}

/* 开始 / 继续整理：直出项目就是普通项目，通用转写口只补没台词的集。
   中止后剩下的集是 pending（不是 error），所以这里按项目当前状态分流：
   有台词缺口 → 继续转写；台词齐了但字幕区域没识别 → 走直出的对齐口。 */
dpEl("transcribe").addEventListener("click", async () => {
  if (!DP.pid) { toast("先选视频"); return; }
  const p = DP.curProj || {};
  const eps = (p.episodes || []);
  const done = eps.filter((e) => (e.lines || []).length).length;
  const aligned = !!(((p.nosub || {}).spans || {}).key);
  try {
    if (eps.length && done < eps.length) {
      await api(`/api/projects/${DP.pid}/transcribe`, { method: "POST" });
      toast(`继续整理剩下 ${eps.length - done} 个视频`);
    } else if (!aligned) {
      await api(`/api/direct/${DP.pid}/analyze`, { method: "POST" });
      toast("开始识别字幕并对齐时间");
    } else {
      toast("台词和字幕时间都已经好了，可以直接出片");
      return;
    }
    dpStartPoll();
  } catch (e) { toast(e.message); }
});

/* 中止整理台词（2026-11-19）：直出第 2 步专属按钮。转写/自动对齐都属 cuts 组，
   复用项目通用中止口，按 DP.pid 精确定位——只停当前这个项目，不会碰其它项目
   （比如主项目正在出的片）在跑的任务。点完立刻拉状态，避免进度条停在旧值。 */
dpEl("transcribe-stop").addEventListener("click", async () => {
  if (!DP.pid) return;
  const ok = await askConfirm({
    title: "中止整理台词？",
    body: [
      "已经整理好的台词会保留，剩下没整理的会停下。",
      "想接着整理，点「重新整理没整理好的」就行。",
    ],
    okText: "中止整理", cancelText: "继续整理", danger: true,
  });
  if (!ok) return;
  DP.transStopSent = true;
  const btn = dpEl("transcribe-stop");
  btn.disabled = true; btn.textContent = "中止中…";
  try {
    const r = await api(`/api/projects/${DP.pid}/task/cancel`, {
      method: "POST", body: JSON.stringify({ group: "cuts" }),
    });
    if (!(r && (r.cancelled || []).length)) toast("这一趟刚好已经结束了");
    else { toast("已中止，正在收尾；整理好的台词都还在"); dpStartPoll(); }
  } catch (e) {
    DP.transStopSent = false;
    toast(e.message || "没能中止，请再试一次");
  }
  // 轮询空闲会自己停，不能等；三条路都立刻刷新一次真实状态。
  try { dpRender(await api("/api/direct/" + DP.pid)); } catch (e) { /* 下次轮询补上 */ }
});

/* 粘贴台词：直出专用弹窗，不碰当前主项目 */
function dpOpenImport() {
  if (!DP.pid) { toast("先选视频"); return; }
  const modal = $("#modal-dp-lines");
  if (!modal) { toast("台词弹窗没找到，刷新页面再试"); return; }
  const p = DP.curProj || {};
  $("#dp-ln-ep").innerHTML = (p.episodes || []).map((e) =>
    `<option value="${e.index}">第 ${e.index} 个 · ${esc(e.file)}</option>`).join("")
    || `<option value="${DP.selEp}">第 ${DP.selEp} 个</option>`;
  $("#dp-ln-ep").value = String(DP.selEp);
  $("#dp-ln-stat").textContent = "";
  $("#dp-ln-text").value = "";
  modal.classList.remove("hidden");
}
dpEl("import").addEventListener("click", dpOpenImport);
// 修改台词：复用第 1 步那套编辑器，但读写的是直出项目，刷新也只刷直出面板。
dpEl("edit").addEventListener("click", () => {
  if (!DP.pid) { toast("先选视频"); return; }
  openViewLines(DP.pid, "direct");
});

$("#dp-ln-close").addEventListener("click", () => $("#modal-dp-lines").classList.add("hidden"));
$("#dp-ln-import").addEventListener("click", async () => {
  if (!DP.pid) { toast("先选视频"); return; }
  const text = $("#dp-ln-text").value.trim();
  if (!text) { toast("先把台词粘进来"); return; }
  const btn = $("#dp-ln-import");
  btn.disabled = true;
  $("#dp-ln-stat").textContent = "解析中…";
  try {
    const r = await api(`/api/projects/${DP.pid}/import-lines`, {
      method: "POST",
      body: JSON.stringify({ text, ep: Number($("#dp-ln-ep").value) || 0 }),
    });
    toast(`已导入 ${r.episodes} 个视频的台词，共 ${r.line_total} 句`);
    $("#modal-dp-lines").classList.add("hidden");
    const fresh = await api("/api/direct/" + DP.pid);
    DP.curProj = fresh; dpRender(fresh);
  } catch (e) {
    $("#dp-ln-stat").textContent = "导入失败：" + e.message;
    toast(e.message);
  } finally { btn.disabled = false; }
});

/* 出片 */
dpEl("render").addEventListener("click", async () => {
  if (!DP.pid) { toast("先选视频并准备好台词"); return; }
  const saveDir = (dpEl("save-dir").value || "").trim();
  const copies = Math.min(5, Math.max(1, Number((dpEl("copies") || {}).value) || 1));
  const dFx = readFx("d-");
  // 随机翻转兜底确认（2026-11-19）：直出素材固定可镜像，只有关了全自动随机才不翻。
  if (!(await confirmMirrorIfOff(dFx))) return;
  try {
    await api(`/api/direct/${DP.pid}/render`, {
      method: "POST",
      body: JSON.stringify({ save_dir: saveDir, fx: dFx, copies }),
    });
    dpStartPoll();
    toast(copies > 1
      ? `开始出片，每个视频出 ${copies} 份（画面各自随机）`
      : "开始出片，每个视频一条成片");
  } catch (e) { toast(e.message); }
});

/* 中止出片（2026-11-18）：直出面板专属，复用项目通用的中止口（batch 属 cuts 组）。
   中止只停这一批、已做好的成片保留；点完立刻拉状态，避免进度条停在旧值。 */
dpEl("stop").addEventListener("click", async () => {
  if (!DP.pid) return;
  const ok = await askConfirm({
    title: "中止出片？",
    body: [
      "已经做好的成片会保留，剩下没做的这一批会停下。",
      "想继续，重新点一次「开始出片」就行。",
    ],
    okText: "中止出片", cancelText: "继续出片", danger: true,
  });
  if (!ok) return;
  try {
    const r = await api(`/api/projects/${DP.pid}/task/cancel`, {
      method: "POST", body: JSON.stringify({ group: "cuts" }),
    });
    if (!(r && (r.cancelled || []).length)) toast("这批刚好已经结束了");
    else { toast("已中止，正在收尾；做好的成片都还在"); dpStartPoll(); }
  } catch (e) { toast(e.message || "没能中止，请再试一次"); }
  // 三条路都立刻刷新一次：轮询空闲会自己停，不能等它。
  try { dpRender(await api("/api/direct/" + DP.pid)); } catch (e) { /* 下一次轮询会补上 */ }
});

dpEl("open-dir").addEventListener("click", async () => {
  if (!DP.pid) { toast("先选视频"); return; }
  try { await api(`/api/projects/${DP.pid}/open-folder`, { method: "POST" }); }
  catch (e) { toast(e.message); }
});

/* 打开页面时恢复最近的直出项目（只恢复面板，不切走主项目）。
   以前只设了 DP.pid 没拉详情 → 刷新后视频列表空着，像「选过的视频没了」。
   现在把详情取回来真正渲染：视频列表 / 台词进度 / 成片保存目录都恢复，
   项目还有任务在跑就续上轮询（2026-11-18）。 */
(async function dpRestore() {
  try {
    // 成片保存目录是前端本地记忆（项目里不存），刷新后读回，别恢复一半
    try {
      const saved = (localStorage.getItem(DP_SAVEDIR_KEY) || "").trim();
      if (saved) { dpEl("save-dir").value = saved; dpUpdateSaveNote(); }
    } catch (e) {}
    const rows = await api("/api/direct/list");
    if (rows && rows.length) {
      DP.pid = rows[0].id;
      dpEl("reselect").classList.remove("hidden");
      const p = await api("/api/direct/" + DP.pid);
      if (p) {
        dpRender(p);
        if (p.task && p.task.kind) dpStartPoll();
      }
    }
  } catch (e) { /* 恢复失败不影响主流程 */ }
})();
