/* 管理后台：用户管理 / 操作日志 / 系统设置。
 *
 * 全部走 Session.ureq（自动带 token、401 自动刷新）。
 * 渲染一律经过 esc()——用户名、备注、日志里的 path 都可能是用户可控的内容，
 * 直接拼进 innerHTML 等于把后台拱手送人。
 */
const $ = (s) => document.querySelector(s);
const esc = Session.esc;
const ftime = Session.ftime;
const ROLE_CN = Session.ROLE_CN;
const STATUS_CN = Session.STATUS_CN;

const ST = {
  page: { users: 1, logs: 1 },
  pages: { users: 1, logs: 1 },
  me: null,
  action: null,   // 弹窗回调
};

/* ── 通用：toast / 弹窗 / 分页 ─────────────────────────────────────── */
let toastTimer = null;
function toast(text) {
  let el = $("#ad-toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "ad-toast";
    el.className = "ad-toast";
    document.body.appendChild(el);
  }
  el.textContent = text;
  el.classList.add("on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("on"), 2800);
}

/** 统一的输入弹窗（封禁要填理由，原生 prompt 在某些浏览器被拦） */
function ask(title, desc, { value = "", placeholder = "", withInput = true } = {}) {
  return new Promise((resolve) => {
    $("#mo-title").textContent = title;
    $("#mo-desc").textContent = desc || "";
    const inp = $("#mo-input");
    inp.style.display = withInput ? "" : "none";
    inp.value = value;
    inp.placeholder = placeholder;
    $("#ad-modal").classList.remove("hidden");
    if (withInput) setTimeout(() => inp.focus(), 30);
    ST.action = () => {
      $("#ad-modal").classList.add("hidden");
      ST.action = null;
      resolve(withInput ? inp.value.trim() : true);
    };
    $("#mo-ok").onclick = () => ST.action && ST.action();
    $("#mo-cancel").onclick = () => {
      $("#ad-modal").classList.add("hidden");
      ST.action = null;
      resolve(null);
    };
  });
}

function pageBar(el, page, pages, onGo) {
  el.innerHTML = `<button class="ghost sm" ${page <= 1 ? "disabled" : ""} data-go="${page - 1}">上一页</button>
    <span class="hint">第 ${page} / ${Math.max(1, pages)} 页</span>
    <button class="ghost sm" ${page >= pages ? "disabled" : ""} data-go="${page + 1}">下一页</button>`;
  el.querySelectorAll("button[data-go]").forEach((b) => {
    b.addEventListener("click", () => onGo(Number(b.dataset.go)));
  });
}

/* ── 用户管理 ───────────────────────────────────────────────────────── */
async function loadUsers(page = 1) {
  ST.page.users = page;
  const q = $("#u-q").value.trim();
  const role = $("#u-role").value;
  const status = $("#u-status").value;
  const url = `/api/admin/users?page=${page}&size=20&q=${encodeURIComponent(q)}`
    + `&role=${role}&status=${status}`;
  let data;
  try {
    data = await Session.ureq(url);
  } catch (e) {
    toast("加载用户失败：" + e.message);
    return;
  }
  ST.pages.users = data.pages;
  const c = data.counts || {};
  $("#u-stat").textContent = `共 ${c.total || 0} 人 · 管理员 ${c.admins || 0} · 已封禁 ${c.banned || 0}`;

  $("#u-rows").innerHTML = (data.items || []).map((u) => {
    const banned = u.status === "banned";
    const self = ST.me && u.id === ST.me.id;
    const isAdmin = u.role === "admin";
    return `<tr class="${banned ? "ad-banned" : ""}">
      <td class="ad-mono">${u.id}</td>
      <td>
        <div class="ad-user">
          <span class="ad-avatar">${esc((u.display_name || u.username).slice(0, 1))}</span>
          <div>
            <b>${esc(u.display_name || u.username)}</b>
            <span class="ad-handle">@${esc(u.username)}</span>
            ${self ? '<span class="ad-tag">我</span>' : ""}
            ${u.must_change_pwd ? '<span class="ad-tag warn">初始密码</span>' : ""}
            ${banned && u.ban_reason ? `<div class="ad-ban-reason">封禁理由：${esc(u.ban_reason)}</div>` : ""}
          </div>
        </div>
      </td>
      <td class="ad-dim">${esc(u.email || "—")}</td>
      <td><span class="ad-tag ${isAdmin ? "admin" : ""}">${ROLE_CN[u.role] || u.role}</span></td>
      <td><span class="ad-tag ${banned ? "bad" : "ok"}">${STATUS_CN[u.status] || u.status}</span></td>
      <td class="ad-dim">${ftime(u.last_login_at)}</td>
      <td class="ad-dim">${ftime(u.last_active)}</td>
      <td class="ad-ops">
        ${banned
          ? `<button class="ghost sm" data-act="unban" data-id="${u.id}">解封</button>`
          : `<button class="ghost sm danger" data-act="ban" data-id="${u.id}" ${self ? "disabled title='不能封禁自己'" : ""}>封禁</button>`}
        <button class="ghost sm" data-act="kick" data-id="${u.id}" title="让这个用户所有设备立刻退出登录">踢下线</button>
        <button class="ghost sm" data-act="role" data-id="${u.id}" data-role="${isAdmin ? "user" : "admin"}"
                ${self ? "disabled title='不能改自己的角色'" : ""}>${isAdmin ? "降为普通" : "设为管理员"}</button>
        <button class="ghost sm" data-act="pwd" data-id="${u.id}">重置密码</button>
      </td>
    </tr>`;
  }).join("") || `<tr><td colspan="8" class="ad-empty">没有匹配的用户</td></tr>`;

  pageBar($("#u-page"), data.page, data.pages, (p) => loadUsers(p));

  $("#u-rows").querySelectorAll("button[data-act]").forEach((b) => {
    b.addEventListener("click", () => onUserAction(b.dataset.act, Number(b.dataset.id), b.dataset.role));
  });
}

async function onUserAction(act, id, role) {
  try {
    if (act === "ban") {
      const reason = await ask("封禁用户",
        "封禁后该用户所有设备立即掉线，且无法再登录。理由会写进日志。\n"
        + "如果当前是「免登录模式」，会同时开启强制登录——否则被封的人清掉本地凭证"
        + "就能以匿名身份继续用，封禁等于没做。",
        { placeholder: "例如：批量导出他人素材 / 违反使用规则" });
      if (reason === null) return;
      const r = await Session.ureq(`/api/admin/users/${id}/ban`, {
        method: "POST", body: JSON.stringify({ reason: reason || "违反使用规则" }),
      });
      toast(r && r.auth_required_enabled
        ? "已封禁，该用户全部令牌已失效；并已开启强制登录（可在系统设置关掉）"
        : "已封禁，该用户全部令牌已失效");
    } else if (act === "unban") {
      if (!confirm("解除封禁？该用户需要重新登录。")) return;
      await Session.ureq(`/api/admin/users/${id}/unban`, { method: "POST" });
      toast("已解封");
    } else if (act === "kick") {
      if (!confirm("让这个用户所有设备立刻退出登录？（不封号）\n"
        + "免登录模式下会同时开启强制登录，否则他回到首页还能以匿名身份继续用。")) return;
      const r = await Session.ureq(`/api/admin/users/${id}/kick`, { method: "POST" });
      toast(r && r.auth_required_enabled
        ? "已踢下线，并已开启强制登录（可在系统设置关掉）"
        : "已踢下线");
    } else if (act === "role") {
      if (!confirm(`确定把该用户设为「${ROLE_CN[role]}」？`)) return;
      await Session.ureq(`/api/admin/users/${id}/role`, {
        method: "PUT", body: JSON.stringify({ role }),
      });
      toast("角色已更新（对方需要重新登录才生效）");
    } else if (act === "pwd") {
      const pwd = await ask("重置密码", "用户下次登录用新密码，旧密码立即失效，所有设备会被踢下线。",
        { placeholder: "至少 8 位，不能是纯数字" });
      if (pwd === null) return;
      await Session.ureq(`/api/admin/users/${id}/password`, {
        method: "PUT", body: JSON.stringify({ new_password: pwd, force_change: true }),
      });
      toast("密码已重置");
    }
    loadUsers(ST.page.users);
  } catch (e) {
    toast("操作失败：" + e.message);
  }
}

/* ── 操作日志 ───────────────────────────────────────────────────────── */
async function loadLogFilters() {
  try {
    const d = await Session.ureq("/api/admin/logs/filters");
    $("#lg-action").innerHTML = `<option value="">全部动作</option>`
      + (d.actions || []).map((a) => `<option value="${esc(a)}">${esc(a)}</option>`).join("");
    $("#lg-user").innerHTML = `<option value="">全部用户</option>`
      + (d.users || []).map((u) => `<option value="${u.user_id}">${esc(u.username)}（${u.n}）</option>`).join("");
  } catch (e) { /* 首次没有日志时为空，不打扰 */ }
}

async function loadLogStats() {
  try {
    const d = await Session.ureq("/api/admin/logs/stats?days=7");
    const rate = d.total ? Math.round((d.failed / d.total) * 100) : 0;
    $("#lg-cards").innerHTML = `
      <div class="ad-card"><span>近 7 天请求</span><b>${d.total}</b></div>
      <div class="ad-card"><span>失败请求</span><b class="${d.failed ? "bad" : ""}">${d.failed}</b><em>${rate}%</em></div>
      <div class="ad-card"><span>活跃用户</span><b>${d.active_users}</b></div>
      <div class="ad-card"><span>最热动作</span><b class="sm">${esc((d.top_actions[0] || {}).action || "—")}</b><em>${(d.top_actions[0] || {}).n || 0} 次</em></div>`;
  } catch (e) { /* 忽略 */ }
}

function logQuery(page) {
  const p = new URLSearchParams();
  p.set("page", page);
  p.set("size", 30);
  const put = (k, v) => { if (v !== "" && v != null) p.set(k, v); };
  put("q", $("#lg-q").value.trim());
  put("action", $("#lg-action").value);
  put("method", $("#lg-method").value);
  put("user_id", $("#lg-user").value);
  put("status", $("#lg-status").value);
  put("fail_only", $("#lg-fail").checked ? "1" : "");
  // date input 是本地日期；库里存 UTC。差 8 小时以内可以接受（筛选粒度是天），
  // 这里直接拼成当天的 ISO 前缀做字符串比较即可。
  put("date_from", $("#lg-from").value ? $("#lg-from").value + "T00:00:00+00:00" : "");
  put("date_to", $("#lg-to").value ? $("#lg-to").value + "T23:59:59+00:00" : "");
  return p.toString();
}

async function loadLogs(page = 1) {
  ST.page.logs = page;
  let data;
  try {
    data = await Session.ureq("/api/admin/logs?" + logQuery(page));
  } catch (e) {
    toast("加载日志失败：" + e.message);
    return;
  }
  ST.pages.logs = data.pages;
  $("#lg-rows").innerHTML = (data.items || []).map((r, i) => {
    const bad = r.status_code >= 400;
    const detail = r.payload || (r.target ? `target: ${r.target}` : "");
    return `<tr class="ad-log ${bad ? "bad" : ""}" data-i="${i}">
        <td class="ad-mono">${ftime(r.created_at)}</td>
        <td>${r.username ? esc(r.username) : '<span class="ad-dim">匿名</span>'}</td>
        <td class="ad-mono ad-act">${esc(r.action || "—")}</td>
        <td class="ad-mono">${esc(r.method)}</td>
        <td class="ad-path" title="${esc(r.path)}">${esc(r.route || r.path)}</td>
        <td class="ad-mono ${bad ? "bad" : "ok"}">${r.status_code}</td>
        <td class="ad-mono">${r.duration_ms}ms</td>
        <td class="ad-mono ad-dim">${esc(r.ip || "—")}</td>
        <td class="ad-dim">${esc(r.message || "")}</td>
      </tr>
      ${detail ? `<tr class="ad-detail hidden" data-d="${i}"><td colspan="9">
        <div class="ad-detail-box"><span class="ad-dim">参数（已脱敏）</span><pre>${esc(detail)}</pre>
        ${r.target ? `<span class="ad-dim">对象：${esc(r.target)}</span>` : ""}
        ${r.ua ? `<span class="ad-dim">UA：${esc(r.ua)}</span>` : ""}</div></td></tr>` : ""}`;
  }).join("") || `<tr><td colspan="9" class="ad-empty">没有匹配的日志</td></tr>`;

  $("#lg-rows").querySelectorAll("tr.ad-log").forEach((tr) => {
    tr.addEventListener("click", () => {
      const d = $("#lg-rows").querySelector(`tr[data-d="${tr.dataset.i}"]`);
      if (d) d.classList.toggle("hidden");
    });
  });
  pageBar($("#lg-page"), data.page, data.pages, (p) => loadLogs(p));
  loadLogStats();
}

/* ── 设置 ───────────────────────────────────────────────────────────── */
async function loadSettings() {
  try {
    const d = await Session.ureq("/api/admin/settings");
    // st-auth 复选框已随「免登录模式」一起删除（2026-11-04）：现在是固定登录模式
    $("#st-access").value = d.access_minutes;
    $("#st-refresh").value = d.refresh_days;
    $("#st-note").textContent = `数据库：${d.db_file}`;
  } catch (e) {
    toast("读取设置失败：" + e.message);
  }
}

$("#st-save").addEventListener("click", async () => {
  const body = {
    access_minutes: Number($("#st-access").value) || 120,
    refresh_days: Number($("#st-refresh").value) || 14,
  };
  const pwd = $("#st-initpwd").value.trim();
  if (pwd) body.admin_init_password = pwd;
  try {
    await Session.ureq("/api/admin/settings", { method: "PUT", body: JSON.stringify(body) });
    $("#st-initpwd").value = "";
    toast("已保存");
    loadSettings();
  } catch (e) {
    toast("保存失败：" + e.message);
  }
});

/* ── 启动 ───────────────────────────────────────────────────────────── */
/** 切 Tab 的唯一入口：按钮点击和 #logs 深链都走这里，保证状态一致。 */
function showTab(p) {
  document.querySelectorAll(".ad-tab").forEach((b) => b.classList.toggle("on", b.dataset.p === p));
  ["users", "logs", "settings"].forEach((x) => $("#p-" + x).classList.toggle("hidden", x !== p));
  if (p === "logs") { loadLogFilters(); loadLogs(1); }
  if (p === "settings") loadSettings();
}

document.querySelectorAll(".ad-tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    location.hash = btn.dataset.p;   // 写进地址栏，刷新 / 分享链接都能停在这个页
    showTab(btn.dataset.p);
  });
});

// 支持 /admin#logs、/admin#settings 直达（找不到或没写就落在用户管理）
const hashTab = (location.hash || "").replace("#", "");
showTab(["logs", "settings"].includes(hashTab) ? hashTab : "users");

$("#u-search").addEventListener("click", () => loadUsers(1));
$("#u-q").addEventListener("keydown", (e) => { if (e.key === "Enter") loadUsers(1); });
$("#lg-search").addEventListener("click", () => loadLogs(1));
$("#lg-q").addEventListener("keydown", (e) => { if (e.key === "Enter") loadLogs(1); });
$("#lg-export").addEventListener("click", () => {
  const p = new URLSearchParams(logQuery(1));
  p.delete("page"); p.delete("size");
  // 导出走原生导航：浏览器会弹出下载，不用自己处理 blob
  const url = "/api/admin/logs/export?" + p.toString();
  fetch(url, { headers: Session.state.access ? { Authorization: "Bearer " + Session.state.access } : {} })
    .then(async (r) => {
      if (!r.ok) throw new Error((await r.json()).detail || "导出失败");
      const blob = await r.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `audit_${new Date().toISOString().slice(0, 10)}.csv`;
      a.click();
      URL.revokeObjectURL(a.href);
    })
    .catch((e) => toast(e.message));
});

$("#btn-logout").addEventListener("click", async () => {
  await Session.logout(false);
  location.href = "/login";
});

(async () => {
  if (!Session.state.access) return Session.state.toLogin("/admin");
  try {
    const me = await Session.ureq("/api/auth/me");
    if (!me.logged_in) return Session.state.toLogin("/admin");
    if (!me.permissions || !me.permissions.admin) {
      document.body.innerHTML = `<div class="au-wrap"><div class="au-card">
        <h1>需要管理员权限</h1>
        <p class="hint">当前账号是普通用户，打不开管理后台。</p>
        <a class="primary wide" href="/" style="display:block;text-align:center;line-height:34px">回到工作台</a>
      </div></div>`;
      return;
    }
    ST.me = me.user;
    $("#ad-who").textContent = `${me.user.display_name || me.user.username}（${ROLE_CN[me.user.role]}）`;
    // 初始密码：直接弹「必须先改」的卡片，改完才继续（后端也拦着所有业务接口）
    if (me.user.must_change_pwd) Session.requirePasswordChange();
    loadUsers(1);
  } catch (e) {
    // 「还没改初始密码」不是掉线：改密卡片已经弹出来了，把人留在原地
    // （否则一边弹改密框、一边把人踹到登录页，改完回来又是同一个框）
    if (e && e.data && e.data.code === "must-change-pwd") return;
    Session.state.toLogin("/admin");
  }
})();
