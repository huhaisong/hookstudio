/* 版本闸：服务器换了新版，旧页面必须刷新后才能继续用。
 *
 * 为什么必须有这个东西（2026-11-04 用户要求「需要更新时必须更新之后才可继续使用」）：
 *   后端改了接口，浏览器里还跑着上一次的 app.js —— 按钮点了没反应、参数按旧格式
 *   提交、AI 授权票据不认，用户只会觉得「坏了」。这类问题排查成本极高，
 *   而根因只有一句话：页面和服务器不是同一版。靠人记着按 Ctrl+F5 不可靠。
 *
 * 三处指纹，任意两处不一致就锁屏：
 *   ① 页面里的 <meta name="app-build">（服务端渲染 HTML 时注入，代表「这个页面是哪一版」）
 *   ② /api/status 响应的 build（代表「服务器现在是哪一版」）
 *   ③ 每个响应头 X-App-Build（请求途中后端被更新也能立刻发现）
 *
 * 锁屏是硬的：全屏遮罩 + 只有一个「立即刷新」。不做「稍后再说」——那就是把旧页面
 * 又放回去用了，等于没做。
 */
(function (global) {
  const meta = document.querySelector('meta[name="app-build"]');
  const BUILD = (meta && meta.content) || "";
  const K_TRY = "hs_upd_try";

  let locked = false;
  let mask = null;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  function tries() {
    try { return Number(sessionStorage.getItem(K_TRY) || 0); } catch (e) { return 0; }
  }

  function bumpTries() {
    try { sessionStorage.setItem(K_TRY, String(tries() + 1)); } catch (e) { /* 无痕模式 */ }
  }

  /** 锁屏。serverB 只用于显示，不参与判断。 */
  function lock(serverB) {
    if (locked) return;
    locked = true;
    document.documentElement.classList.add("upd-lock");

    const n = tries();
    bumpTries();
    // 刷了一次还对不上，说明浏览器或中间层还留着旧东西（代理/CDN/强缓存），
    // 这时只给「刷新」按钮就是死循环，得把话说清楚。
    const stuck = n >= 2;

    mask = document.createElement("div");
    mask.className = "upd-mask";
    mask.innerHTML = `
      <div class="upd-card">
        <div class="upd-ico">⟳</div>
        <h1>系统已更新，需要刷新后继续</h1>
        <p class="upd-lead">这个页面是旧版本，程序已经更新。
          继续操作可能保存出错，所以已暂停使用，刷新一下就好。</p>
        <div class="upd-code">
          <span>页面 <b>${esc(BUILD || "未知")}</b></span>
          <span>服务器 <b>${esc(serverB || "未知")}</b></span>
        </div>
        <button class="upd-btn" id="upd-reload">立即刷新</button>
        <p class="upd-tip">${stuck
          ? "刷新后仍提示？按 <b>Ctrl + F5</b>（Mac：⌘ + Shift + R）强制重新加载。"
          : "刷新后会自动回到当前页面，不用重新登录。"}</p>
      </div>`;
    document.body.appendChild(mask);
    const btn = mask.querySelector("#upd-reload");
    btn.addEventListener("click", () => {
      btn.disabled = true;
      btn.textContent = "正在刷新…";
      // 清掉「试过几次」，否则下次正常更新也会被当成刷不干净
      setTimeout(() => { try { sessionStorage.removeItem(K_TRY); } catch (e) {} }, 4000);
      location.reload();
    });
    // 焦点拉过来，避免用户还在键盘上操作后面的表单
    setTimeout(() => btn.focus(), 30);
  }

  /** 比指纹锁更硬的一种锁：这版程序本身不能用了，刷新解决不了，必须换安装包。
   *
   *  什么时候会走到这里：服务器把「最低可用版本」调到比本机高（见 app/update.py）。
   *  exe 分发给别人之后，本地指纹闸永远不会触发——用户电脑上的页面和后端是同一份
   *  安装包，两者天然一致。只有服务器说了算才能实现「必须更新后才能继续使用」。
   */
  function lockUpgrade(info) {
    if (locked) return;
    locked = true;
    document.documentElement.classList.add("upd-lock");
    info = info || {};
    const local = info.local || "";
    const need = info.latest || info.min || "";
    const url = info.url || "";
    mask = document.createElement("div");
    mask.className = "upd-mask";
    mask.innerHTML = `
      <div class="upd-card upd-hard">
        <div class="upd-ico">⤓</div>
        <h1>这个版本已停止使用</h1>
        <p class="upd-lead">这个版本已经不能用了，请安装新版后继续，
          <b>刷新页面不起作用</b>。原来的项目和成片都在，不会丢。</p>
        <div class="upd-code">
          <span>当前版本 <b>${esc(local || "未知")}</b></span>
          <span>需要版本 <b>${esc(need || "更新")}</b></span>
        </div>
        ${info.note ? `<p class="upd-note">${esc(info.note)}</p>` : ""}
        ${url
          ? `<a class="upd-btn" href="${esc(url)}" target="_blank" rel="noopener">下载新版</a>`
          : `<button class="upd-btn" disabled>请联系管理员获取新版安装包</button>`}
        <p class="upd-tip">装好之后关掉黑窗口再双击新的 钩子工坊.exe，浏览器会自动打开；
          原来的项目和成片都在，不会丢。</p>
      </div>`;
    document.body.appendChild(mask);
  }

  /** 比指纹。serverB 只用于显示，不参与判断。 */
  function check(serverB) {
    if (!BUILD || !serverB) return false;
    if (serverB === BUILD) {
      // 恰好一致：说明这次刷新是干净的，把计数清掉
      try { sessionStorage.removeItem(K_TRY); } catch (e) { /* ignore */ }
      return false;
    }
    lock(serverB);
    return true;
  }

  // ── 包装 fetch：任何响应都能发现「后端换版了」／「这版被封了」──────────
  // 只加响应头判断，不改变请求行为；锁屏后照常放行，只是界面已经拦住用户了。
  const rawFetch = global.fetch.bind(global);
  global.fetch = function (input, init) {
    return rawFetch(input, init).then((res) => {
      // 426 = 服务器要求升级。详情在 body 里，读一份克隆，别把下游要读的流吃掉。
      if (res.status === 426) {
        res.clone().json().then((d) => lockUpgrade(d)).catch(() => lockUpgrade({}));
        return res;
      }
      const b = res.headers && res.headers.get("X-App-Build");
      if (b) check(b);
      return res;
    });
  };

  /** 主动向服务端问一次当前版本（含「服务器要求的最低版本」）。 */
  function poll() {
    if (locked || document.hidden) return Promise.resolve(false);
    return rawFetch("/api/status", { cache: "no-store" })
      .then((r) => r.json())
      .then((d) => {
        // 远程闸优先：本地指纹都懒比了，这版已经不该用
        if (d && d.update && d.update.need) {
          lockUpgrade(Object.assign({ local: d.version }, d.update.need));
          return true;
        }
        return check(d && d.build);
      })
      .catch(() => false);
  }

  // 页面放着不动也要能发现（很多人是开着窗口去泡咖啡，回来接着点）
  setInterval(poll, 20000);
  setTimeout(poll, 2500);

  global.AppVersion = { build: BUILD, check, lock, lockUpgrade, poll, locked: () => locked };
})(window);
