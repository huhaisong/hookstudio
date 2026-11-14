/* 会话工具：登录页 / 管理后台 / 主界面共用。
 *
 * 只做四件事：存取令牌、带凭证请求、401 自动刷新、跳登录。
 * 不引框架——这个项目的前端就是原生 JS，多一个依赖就多一份打包风险。
 *
 * 令牌存在 localStorage（不是 cookie）：本机工具没有 CSRF 面，
 * localStorage 更好调试；如果要部署到公网做多租户，建议改成
 * HttpOnly + SameSite=Lax 的 cookie（那时刷新逻辑要跟着改）。
 */
(function (global) {
  const K_ACCESS = "hs_access";
  const K_REFRESH = "hs_refresh";
  const K_USER = "hs_user";

  const S = {
    get access() { return localStorage.getItem(K_ACCESS) || ""; },
    get refresh() { return localStorage.getItem(K_REFRESH) || ""; },
    get user() {
      try { return JSON.parse(localStorage.getItem(K_USER) || "null"); } catch (e) { return null; }
    },
    save(data) {
      if (data.access_token) localStorage.setItem(K_ACCESS, data.access_token);
      if (data.refresh_token) localStorage.setItem(K_REFRESH, data.refresh_token);
      if (data.user) localStorage.setItem(K_USER, JSON.stringify(data.user));
    },
    setUser(u) { localStorage.setItem(K_USER, JSON.stringify(u || null)); },
    clear() {
      [K_ACCESS, K_REFRESH, K_USER].forEach((k) => localStorage.removeItem(k));
    },
    /** 跳登录页，带回跳地址和「为什么被踢出来」（登录页要按这个给用户一句话） */
    toLogin(next, reason) {
      // 过期锁屏在场时不再跳转：遮罩本身就是「必须重新登录」的硬闸，
      // 轮询的 401 由遮罩兜住，用户点按钮才去登录页
      if (document.querySelector("#sess-mask")) return;
      const to = next || (location.pathname + location.search);
      if (location.pathname === "/login") return;
      let url = "/login?next=" + encodeURIComponent(to);
      if (reason) url += "&reason=" + encodeURIComponent(reason);
      location.href = url;
    },
  };

  // 2026-11-14 硬过期（用户定稿）：登录有效期 24 小时，过期即锁屏重登，
  // 不再做无感续期——旧的「401 就拿 refresh 换新续命」逻辑连同单飞一起移除。
  // refresh_token 仍由登录下发、退出（全设备）时用于吊销，前端不再主动用它。

  /** 带凭证请求：自动加 Bearer。401 = 过期/无效，清凭证送登录页（硬过期）。
   *
   * 401 / 403 的分工（2026-11-04 补）：
   *   401 = 没凭证或凭证过期 → 清凭证送登录页（reason=expired）
   *   403 + code=banned = 账号被封 → 清凭证、送登录页（reason=banned）
   *   403 其它 = 权限不够（普通用户点管理员接口）→ **留在原地**，只报错。
   * 把 403 一律当掉线处理会让普通用户莫名其妙被踢到登录页。
   */
  async function ureq(url, opts = {}) {
    const headers = Object.assign(
      { "Content-Type": "application/json" },
      opts.headers || {},
      S.access ? { Authorization: "Bearer " + S.access } : {}
    );
    const r = await fetch(url, Object.assign({}, opts, { headers }));
    if (r.status === 401) {
      S.clear();
    }
    const txt = await r.text();
    let data;
    try { data = JSON.parse(txt); } catch (e) { data = { detail: txt.slice(0, 300) }; }
    if (!r.ok) {
      if (data && data.code === "banned") {
        S.clear();
        S.toLogin(null, "banned");
      } else if (data && data.code === "must-change-pwd") {
        // 不是掉线、也不是没权限，是「还没更新密码」——把人按在改密弹窗里
        openPwdGate(data.detail);
      } else if (r.status === 401) {
        S.clear();
        // revoked = 被踢下线 / 改过密码；expired = 单纯过期
        S.toLogin(null, data && data.code === "revoked" ? "kicked" : "expired");
      }
      const err = new Error(data.detail || ("HTTP " + r.status));
      err.status = r.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  /* ── 强制改密闸（2026-11-04）─────────────────────────────────────────
   * 后端在中间件里硬拦：`must_change_pwd=1` 的用户除了「读自己是谁 / 改密 / 退出 /
   * 续期」之外，所有接口都是 403 + code=must-change-pwd。
   * 这里负责把人接住：弹一个**关不掉**的卡片，改完才继续。
   *
   * 为什么不只是 toast 提示：提示可以被无视（关标签页、直接 curl），
   * 用户要的是「更新之后才可继续使用」，那就得让界面也走不过去。
   */
  let gateOpen = false;

  function openPwdGate(msg) {
    if (gateOpen) return;
    if (location.pathname === "/login") return;   // 登录页自己引导
    gateOpen = true;
    const mask = document.createElement("div");
    mask.className = "gp-mask";
    mask.innerHTML = `
      <div class="gp-card">
        <h1>请先设置新密码</h1>
        <p class="gp-lead">${esc(msg || "你还在使用初始密码（或管理员刚重置的密码）。改完之后才能继续使用。")}</p>
        <label class="gp-fld">原密码
          <input id="gp-old" type="password" autocomplete="current-password" placeholder="当前正在用的密码">
        </label>
        <label class="gp-fld">新密码
          <input id="gp-new" type="password" autocomplete="new-password" placeholder="至少 8 位，不能是纯数字">
        </label>
        <label class="gp-fld">确认新密码
          <input id="gp-new2" type="password" autocomplete="new-password" placeholder="再输一次">
        </label>
        <p class="gp-err" id="gp-err"></p>
        <button class="gp-btn" id="gp-save">保存并继续</button>
        <p class="gp-tip">改完后其他设备会退出登录，这台电脑继续用。</p>
      </div>`;
    document.body.appendChild(mask);
    const err = mask.querySelector("#gp-err");
    const btn = mask.querySelector("#gp-save");
    const iOld = mask.querySelector("#gp-old");
    const iNew = mask.querySelector("#gp-new");
    const iNew2 = mask.querySelector("#gp-new2");
    setTimeout(() => iOld.focus(), 50);

    async function submit() {
      err.textContent = "";
      const oldPwd = iOld.value;
      const newPwd = iNew.value;
      if (!oldPwd) { err.textContent = "请输入原密码"; iOld.focus(); return; }
      if (newPwd.length < 8) { err.textContent = "新密码至少 8 位"; iNew.focus(); return; }
      if (newPwd !== iNew2.value) { err.textContent = "两次输入的新密码不一样"; iNew2.focus(); return; }
      btn.disabled = true;
      btn.textContent = "保存中…";
      try {
        const r = await fetch("/api/auth/password", {
          method: "PUT",
          headers: Object.assign({ "Content-Type": "application/json" },
            S.access ? { Authorization: "Bearer " + S.access } : {}),
          body: JSON.stringify({ old_password: oldPwd, new_password: newPwd }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) {
          err.textContent = d.detail || ("HTTP " + r.status);
          btn.disabled = false;
          btn.textContent = "保存并继续";
          return;
        }
        // 改密会让所有旧 token 作废，后端给当前设备发了一对新凭证
        if (d && d.access_token) S.save(d);
        btn.textContent = "已保存，正在重新加载…";
        // 整页重载最稳：所有模块都会按「已改密」的新身份重新拉一遍状态
        setTimeout(() => location.reload(), 300);
      } catch (e) {
        err.textContent = "连不上服务：" + e.message;
        btn.disabled = false;
        btn.textContent = "保存并继续";
      }
    }

    btn.addEventListener("click", submit);
    mask.querySelectorAll("input").forEach((el) => {
      el.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
    });
  }

  /** ISO(UTC) → 本地 `11-04 16:20:31`。库里全存 UTC，显示时才转。 */
  function ftime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso).slice(0, 19);
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  const ROLE_CN = { admin: "管理员", user: "普通用户" };
  const STATUS_CN = { active: "正常", banned: "已封禁" };

  /* ── access 有效期本地判定（2026-11-14 登录过期锁屏用）──────────────
     解 JWT 的 exp（base64url 载荷，客户端只读不改——改了也没用，服务端
     会验签）。解不出 = 0 = 视作已过期。valid() 留 30 秒余量防边界抖动。 */
  function accessExp() {
    try {
      const parts = (localStorage.getItem(K_ACCESS) || "").split(".");
      if (parts.length < 2) return 0;
      const b64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
      const pad = b64 + "=".repeat((4 - b64.length % 4) % 4);
      const payload = JSON.parse(decodeURIComponent(escape(atob(pad))));
      return Number(payload.exp || 0);
    } catch (e) { return 0; }
  }
  function accessValid() {
    return accessExp() > Math.floor(Date.now() / 1000) + 30;
  }

  global.Session = {
    state: S,
    ureq,
    esc,
    ftime,
    accessExp,
    accessValid,
    /** 前端主动弹改密闸（登录返回 must_change_pwd=1 时用） */
    requirePasswordChange: openPwdGate,
    passwordGateOpen: () => gateOpen,
    ROLE_CN,
    STATUS_CN,
    logout(allDevices) {
      const rt = S.refresh;
      return fetch("/api/auth/logout", {
        method: "POST",
        headers: Object.assign({ "Content-Type": "application/json" },
          S.access ? { Authorization: "Bearer " + S.access } : {}),
        body: JSON.stringify({ refresh_token: rt, all_devices: !!allDevices }),
      }).catch(() => {}).then(() => S.clear());
    },
  };
})(window);
