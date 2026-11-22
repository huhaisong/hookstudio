/* 批量定时发布 —— 一级页面（不是抽屉、不是第 6 步）。
 *
 * 为什么独立：发布和左边的项目列表**没有必然联系**。视频可能来自任何文件夹、
 * 也可能只发一个文件，不一定是这个工具出的片。所以它和「项目」平级：
 * 切过来时左侧项目列表整块消失，页面铺满（2026-11-13 用户定）。
 *
 * 程序负责「排队、算时间、传上去、在平台定时」，到点由平台自己发。
 * （2026-11-14：这里现在会真上传 + 真点发表，文案里可以直接说「传到平台」。）
 *
 * 依赖 app.js 里的全局：$ / api / toast（同源脚本共享全局词法环境）。
 */
(() => {
  const S = {
    d: null, cand: [], sel: new Set(), pid: "", pname: "",
    // first = 第一条发布时间（默认半小时后，defFirst() 算好）；
    // gap = 每条间隔的默认分钟数：30 档与「同批素材建议 30 分钟以上」的
    // 防搬运建议一致（2026-11-14 用户定），要用别的档随时点。
    // hist = 右栏当前在「队列 / 已提交」哪个视图（2026-11-14 新增已提交任务）。
    first: "", gap: 30, plan: null, planTimer: null, poll: null, hist: false,
    // planSeq：预览请求的序号。改时间会连发好几个 plan（每次 350ms 防抖后仍然
    // 可能重叠），谁后返回谁覆盖 → 摘要闪回上一次的值。只认最后一次的响应。
    // planFor：这份预览是按哪组「首条/间隔/条数」算出来的，开始上传时直接复用，
    // 省掉一次白算（2026-11-21）。
    planSeq: 0, planFor: null,
    key: "", edit: null, picker: null, opts: {}, optBusy: {},
    verifiedCur: "",   // 本次页面会话已经真导航核对过登录态的当前号（切号即验号）
    // 选好号后的静默预取：optFor 记住已经备好的是哪个号，optSeq 用来丢弃
    // 切号之前发出的旧响应，optState 只是 ② 里一行小字，不弹窗不打断。
    optFor: "", optSeq: 0, optState: "", optFailAt: 0,
    // optRefreshAid：同一个号同时只允许一次强制重读（手动 / 静默共用），
    // 别往单线程 session 队列里排两个 18 秒。optNote 是「先用着上次读的」说明。
    optRefreshAid: "", optNote: "", optStale: false,
    // 候选读取「用户看得见」的状态：什么时候开始读、是不是第一次、上次读好
    // 是什么时候。阶段文案只改那一行字的 textContent，绝不整块重画字段区
    // （用户可能正在标题/话题里打字）。
    optLoadingAt: 0, optFirstRead: false, optReadAt: 0, optStageTimer: 0,
    // 已提交列表的筛选：全部 / 要处理的 / 已完成的
    qfilter: "all",
    // 当前选中的号只由 /api/publish/queue 下发（current_account_id），前端不存：
    // 刷新、多开页面都只会看到服务端那一份，避免界面显示 A、实际发到 B。
    tab: "pub", aid: "", stat: null, diag: null, mon: null,
    // 数据页视图状态（小v猫式）：筛选词 / 只看有增量 / 剧集 / 排序 / 双列 / 通知面板。
    // 都只影响「怎么显示本地已采到的数据」，不改后端、不重新采样。
    statFilter: "", statIncOnly: false, statColl: "", statSort: "time",
    statCols: 1, statNotice: null, statNoticeOpen: false,
    statBusy: false, diagBusy: false, abn: null, delBusy: false, openBusy: false,
    // 加入队列的进行态（2026-11-21）：以前点完到入队完成之间界面一动不动，
    // 22 条时要等半分钟到两分钟，人只会以为按钮坏了、然后连点十几次。
    // addBusy 期间按钮禁用并显示条数，连点直接吞掉（后端也加了锁）。
    addBusy: false, addN: 0,
    // 登录会话：null＝没在登录。添加账号/重登/失效兜底全走这一个状态，
    // 后端负责弹出可见浏览器窗口，前端只提示 + 轮询登录态（2026-11-14 用户定：
    // 界面里不再显示二维码）。
    login: null,
  };

  // 状态名一律说人话（2026-11-14 用户：「清空未排期」「撤回排期」这种词
  // 连他自己都看不懂，更何况用户）。每个名字都能对上「这条现在处在哪一步」，
  // 不用猜「排期」指什么。同一个状态只允许一个叫法。
  const STATUS = {
    draft:     ["还没安排", "wait"],
    queued:    ["等着上传", "wait"],
    publishing:["正在上传", "wait"],
    scheduled: ["已定时", "ok"],
    published: ["已发布", "ok"],
    manual:    ["要你手动发", "bad"],
    failed:    ["失败", "bad"],
    skipped:   ["已取消", "mute"],
    uncertain: ["结果待核实", "bad"],
  };

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pad = (n) => String(n).padStart(2, "0");
  /** 拉候选要开一次浏览器。后端最坏 150 秒，前端不能跟着无限转：
   *  超时就恢复按钮并把话说明白，不然用户只看到「正在从号里读」永远不动。
   *  底层请求不取消（同号 session 单线程，硬取消只会把队列搅乱），
   *  迟到的响应由 optSeq 丢弃。 */
  function apiT(path, opts, ms) {
    return new Promise((resolve, reject) => {
      let done = false;
      const timer = setTimeout(() => {
        if (done) return;
        done = true;
        reject(new Error("这次读得太久，先停下了。点一下重试；还不行就把这个号关掉重开。"));
      }, ms || 95000);
      api(path, opts).then((v) => {
        if (done) return;
        done = true; clearTimeout(timer); resolve(v);
      }).catch((e) => {
        if (done) return;
        done = true; clearTimeout(timer); reject(e);
      });
    });
  }
  // 视频号助手首页。申诉/违规通知都在它左侧的「通知中心」里 —— 那是个 SPA，
  // 深链接会随版本变，写死一条到期就 404，所以只打开首页。
  const WX_PLATFORM = "https://channels.weixin.qq.com/platform";

  function topicsText(list) {
    return (list || []).map((t) => "#" + String(t).replace(/^#+/, "")).join(" ");
  }
  function parseTopics(s) {
    return String(s || "").split(/[\s,，、]+/)
      .map((t) => t.replace(/^#+/, "").trim()).filter(Boolean);
  }
  function toLocal(d) {
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  function defFirst() {
    // 默认排在半小时后（2026-11-14 用户定）：客户要的是「一会儿就发」，
    // 不是下一个整点。向上取整到 5 分钟，避免 13:07 这种零头。
    const d = new Date(Date.now() + 30 * 60000);
    d.setSeconds(0, 0);
    d.setMinutes(Math.ceil(d.getMinutes() / 5) * 5);
    return toLocal(d);
  }
  function showTime(s) {
    if (!s) return "—";
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(s);
    return m ? `${m[2]}-${m[3]} ${m[4]}:${m[5]}` : s;
  }
  function isDue(j) {
    return j.status === "queued" && j.plan_at && j.plan_at <= toLocal(new Date());
  }
  function mb(n) {
    const v = Number(n || 0) / (1024 * 1024);
    return v >= 1024 ? (v / 1024).toFixed(1) + "GB" : Math.round(v) + "MB";
  }

  const VIEW = () => document.querySelector("#view-publish");
  const BODY = () => document.querySelector("#pub-body");
  const SHELL = () => document.querySelector(".shell");

  // ── 视图切换（一级导航）──────────────────────────────────────────────
  let prevView = "view-new";     // 进发布之前看的是哪一页，回去时原样恢复

  function navTo(which) {
    document.querySelectorAll(".appnav-item").forEach((n) =>
      n.classList.toggle("on", n.dataset.nav === which));
    const pub = which === "publish";
    if (pub) {
      const cur = [...document.querySelectorAll(".view")]
        .find((v) => !v.classList.contains("hidden") && v.id !== "view-publish");
      if (cur) prevView = cur.id;
    }
    // 切到发布：左侧项目列表整块收起，页面铺满。
    // 这一步是刻意的 —— 留着项目列表，用户永远会以为「发布属于当前项目」。
    SHELL().classList.toggle("pub-mode", pub);
    document.querySelectorAll(".view").forEach((v) => {
      v.classList.toggle("hidden",
        pub ? v.id !== "view-publish" : v.id !== prevView);
    });
    if (pub) {
      gotoPubTab(S.tab || "pub");
    } else {
      stopPoll();
      stopLoginPoll();
      S.login = null;
    }
  }

  // ── 三个 Tab（发布 / 数据 / 诊断）─────────────────────────────────────
  // 分开是刻意的：数据和诊断都要开一次浏览器去号里拉，一次 8-15 秒。
  // 混进发布页会让「打开这一页」直接卡住，所以切过去才拉。
  const PTAB = { pub: "#pub-body", stat: "#pub-stat", diag: "#pub-diag" };

  function gotoPubTab(which) {
    if (!PTAB[which]) which = "pub";
    S.tab = which;
    document.querySelectorAll(".pub-tab").forEach((n) =>
      n.classList.toggle("on", n.dataset.ptab === which));
    Object.keys(PTAB).forEach((k) => {
      const el = document.querySelector(PTAB[k]);
      if (el) el.classList.toggle("hidden", k !== which);
    });
    if (which === "pub") {
      load();
      startPoll();
      // 登录面板挂在全局账号条上，切 Tab 不收起它——回来时把轮询接上，
      // 否则面板会一直停在「正在登录」，其实早就登好了。
      if (S.login && !S.login.opening && !S.login.timer) startLoginPoll();
    } else {
      stopPoll();
    }
    paintGlobalAcc();
    if (which === "stat") renderStat();
    if (which === "diag") renderDiag();
  }

  // ── 数据 Tab（作品列表 + 播放流速）──────────────────────────────────────
  // 这一页刻意「先读缓存、再手动采」：采一次要开浏览器 8-15 秒，
  // 不能让客户每次点进来都干等。缓存是毫秒级，按钮上写清「上次什么时候采的」。
  function tsText(ts) {
    if (!ts) return "—";
    const d = new Date(Number(ts) * 1000);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
      + `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  function durText(s) {
    const v = Math.max(0, Math.round(Number(s) || 0));
    return `${Math.floor(v / 60)}:${pad(v % 60)}`;
  }

  // 数字显示口径：完播率/平均时长平台可能没给（老数据）→ 一律「—」，绝不补 0
  function rateText(v) { return v == null ? "—" : Number(v).toFixed(1) + "%"; }
  function avgText(v) {
    if (v == null) return "—";
    const s = Math.round(Number(v));
    return s >= 60 ? `${Math.floor(s / 60)}分${pad(s % 60)}秒` : `${s}秒`;
  }

  async function renderStat(force) {
    const box = document.querySelector("#pub-stat");
    if (!box) return;
    // 数据是号里读出来的，没确认号就不显示任何旧号的数据
    if (!currentAid()) { S.stat = null; box.innerHTML = needAccBox(); return; }
    if (!S.stat || force) {
      if (!force) box.innerHTML = `<div class="pub-loading">正在读取…</div>`;
      try {
        const aid = encodeURIComponent(currentAid());
        S.stat = await api(`/api/publish/stat?aid=${aid}`);
        S.mon = await api(`/api/publish/monitor?aid=${aid}`);
      } catch (e) {
        box.innerHTML = `<div class="pub-empty bad">${esc(e.message)}</div>`;
        return;
      }
    }
    const d = S.stat || {};
    const mon = S.mon || {};
    const acc = d.account || {};
    const allPosts = d.posts || [];
    const sum = d.summary || {};
    // 剧集下拉：从作品里出现过的合集名去重
    const colls = [...new Set(allPosts.map((p) => p.collection).filter(Boolean))];

    // ── 筛选 / 排序：只动本地显示，不重新采样 ────────────────────────────
    let posts = allPosts.filter((p) => {
      if (S.statIncOnly && !(p.inc > 0)) return false;
      if (S.statColl && p.collection !== S.statColl) return false;
      if (S.statFilter) {
        const q = S.statFilter.toLowerCase();
        if (!String(p.title || "").toLowerCase().includes(q)
          && !String(p.collection || "").toLowerCase().includes(q)) return false;
      }
      return true;
    });
    if (S.statSort === "play") posts = [...posts].sort((a, b) => b.play - a.play);
    else if (S.statSort === "inc") posts = [...posts].sort((a, b) => (b.inc || 0) - (a.inc || 0));
    else if (S.statSort === "finish") posts = [...posts].sort((a, b) => (b.finish_rate || 0) - (a.finish_rate || 0));
    // 默认 time：后端已按发布时间倒序

    box.innerHTML = `
      <div class="mon-bar${mon.on ? " on" : ""}">
        <span class="mon-dot"></span>
        <b>${mon.on ? "正在自动记录" : "没在自动记录"}</b>
        <span class="mon-sub">${mon.on
          ? `每 ${mon.every_min} 分钟自动看一次播放量${mon.next_in_min >= 0 ? `，约 ${mon.next_in_min} 分钟后再看` : ""}${mon.last_at ? ` · 上次 ${esc(showTime(mon.last_at))}` : ""}`
          : "开着才会记下每次的变化。关了就少一段记录，之前读到的不会丢。"}</span>
        <button class="ghost sm" data-act="monToggle">${mon.on ? "停止" : "开始自动记录"}</button>
      </div>

      <div class="stat-head">
        <div class="stat-acc">
          <span class="av">${acc.avatar
            ? `<img src="${esc(acc.avatar)}" alt="">`
            : `<i>${esc(String(acc.nick || "号").slice(0, 1))}</i>`}</span>
          <div>
            <b>${esc(acc.nick || "还没读过")}</b>
            <span>${acc.nick
              ? `${esc(String(acc.fans || 0))} 粉丝 · ${esc(String(sum.count ?? acc.feeds ?? 0))} 个作品`
              : "点右边「采一次」，程序去号里读一遍"}</span>
          </div>
        </div>
        <div class="stat-ops">
          <span class="hint">${d.at ? `上次 ${esc(showTime(d.at))}` : ""}</span>
          <button class="ghost sm" data-act="statNotice">通知记录${d.notify_unread ? ` ${esc(String(d.notify_unread))}` : ""}</button>
          <button class="ghost sm" data-act="statClear"${allPosts.length ? "" : " disabled"}>清空数据</button>
          <button class="primary sm" data-act="statSample"${S.statBusy ? " disabled" : ""}>
            ${S.statBusy ? "正在读…" : d.at ? "再采一次" : "采一次"}</button>
        </div>
      </div>

      ${allPosts.length ? `
        <div class="stat-summary">
          <div><span>作品数</span><b>${esc(String(sum.count ?? allPosts.length))}</b></div>
          <div><span>最高播放</span><b>${esc(String(sum.max_play ?? 0))}</b></div>
          <div><span>最低播放</span><b>${esc(String(sum.min_play ?? 0))}</b></div>
          <div><span>合计播放</span><b>${esc(String(sum.total_play ?? 0))}</b></div>
        </div>

        <div class="stat-filter">
          <input type="search" class="stat-search" data-stat-input="filter"
            placeholder="作品标题 / 剧集" value="${esc(S.statFilter)}">
          <select class="stat-sel" data-stat-input="coll">
            <option value="">全部剧集</option>
            ${colls.map((c) => `<option value="${esc(c)}"${S.statColl === c ? " selected" : ""}>${esc(c)}</option>`).join("")}
          </select>
          <select class="stat-sel" data-stat-input="sort">
            <option value="time"${S.statSort === "time" ? " selected" : ""}>按发布时间</option>
            <option value="play"${S.statSort === "play" ? " selected" : ""}>按播放数</option>
            <option value="inc"${S.statSort === "inc" ? " selected" : ""}>按这段时间涨了</option>
            <option value="finish"${S.statSort === "finish" ? " selected" : ""}>按完播率</option>
          </select>
          <label class="stat-chk"><input type="checkbox" data-stat-input="incOnly"${S.statIncOnly ? " checked" : ""}> 只看涨了的</label>
          <button class="ghost sm stat-col-btn" data-act="statCols">${S.statCols === 2 ? "单列" : "双列"}</button>
        </div>

        <div class="stat-grid${S.statCols === 2 ? " two" : ""}">
          ${posts.length ? posts.map(statCard).join("")
            : `<div class="pub-empty">没有符合筛选条件的作品。</div>`}
        </div>
        <p class="stat-tip">播放量、完播率、平均时长都是平台给的数；「这段时间涨了」要采过两次才看得出来，只采过一次的作品显示「—」。这些数读到哪算哪，不猜。</p>`
        : `<div class="pub-empty">${esc(d.note
          || "还没有数据。点右上「采一次」，程序会去号里读一遍（大约 10 秒）。")}</div>`}`;

    if (S.statNoticeOpen) paintStatNotice();
  }

  function statCard(p) {
    const inc = p.inc == null ? "—" : (p.inc > 0 ? "+" + p.inc : String(p.inc));
    const incCls = p.inc > 0 ? "up" : (p.inc < 0 ? "down" : "muted");
    const col = p.collection && p.collection !== p.title
      ? `<span class="st-collection">${esc(p.collection)}</span>` : "";
    return `<div class="st-card">
      <div class="st-cover">${p.cover
        ? `<img src="${esc(p.cover)}" alt="" loading="lazy">`
        : `<i>无封面</i>`}</div>
      <div class="st-body">
        <div class="st-title" title="${esc(p.title || "")}">${esc(p.title || "（无标题）")}</div>
        <div class="st-sub">${col}<span>${esc(tsText(p.ts))}</span></div>
        <div class="st-metrics">
          <div><span>播放</span><b>${esc(String(p.play))}</b></div>
          <div><span>完播率</span><b>${esc(rateText(p.finish_rate))}</b></div>
          <div><span>平均时长</span><b>${esc(avgText(p.avg_play_sec))}</b></div>
          <div><span>起始播放</span><b>${esc(String(p.base ?? 0))}</b></div>
          <div><span>这段时间涨了</span><b class="${incCls}">${esc(inc)}</b></div>
        </div>
        <div class="st-social">
          <span title="点赞">♡ ${esc(String(p.like))}</span>
          <span title="评论">💬 ${esc(String(p.comment))}</span>
          <span title="转发">↗ ${esc(String(p.forward))}</span>
          <span title="收藏">☆ ${esc(String(p.fav))}</span>
        </div>
      </div>
    </div>`;
  }

  // 通知记录：独立面板，覆盖在数据页上；纯读上次采样缓存
  async function openStatNotice() {
    S.statNoticeOpen = true;
    S.statNotice = null;
    const box = document.querySelector("#pub-stat");
    if (box) paintStatNoticeLoading();
    try {
      const aid = encodeURIComponent(currentAid() || "");
      S.statNotice = await api(`/api/publish/notify?aid=${aid}`);
    } catch (e) {
      S.statNotice = { items: [], note: e.message };
    }
    paintStatNotice();
  }
  function paintStatNoticeLoading() {
    const old = document.querySelector("#stat-notice-panel");
    if (old) old.remove();
    const host = document.querySelector("#pub-stat");
    if (!host) return;
    host.insertAdjacentHTML("beforeend",
      `<div class="st-notice-mask" id="stat-notice-panel" data-act="statNoticeClose">
        <div class="st-notice" data-stop="1"><div class="pub-loading">正在读取…</div></div>
      </div>`);
  }
  function paintStatNotice() {
    const old = document.querySelector("#stat-notice-panel");
    if (old) old.remove();
    const host = document.querySelector("#pub-stat");
    if (!host) return;
    const nd = S.statNotice || {};
    const items = nd.items || [];
    host.insertAdjacentHTML("beforeend", `
      <div class="st-notice-mask" id="stat-notice-panel" data-act="statNoticeClose">
        <div class="st-notice" data-stop="1">
          <div class="st-notice-head">
            <b>通知记录</b>
            <span class="hint">${nd.unread ? `未读 ${esc(String(nd.unread))} 条` : ""}</span>
            <button class="mini" data-act="statNoticeClose">关闭</button>
          </div>
          ${items.length ? `<div class="st-notice-list">${items.map((it) => `
            <div class="st-notice-item">
              <div><b>${esc(it.title || "通知")}</b><span>${esc(tsText(it.ts))}</span></div>
              ${it.text ? `<p>${esc(it.text)}</p>` : ""}
            </div>`).join("")}</div>`
            : `<div class="pub-empty">${esc(nd.note || "这个号最近没读到通知。")}</div>`}
          <p class="stat-tip">这里只列平台真发过的通知；要处理违规/申诉，还得本人到视频号助手的通知中心办。</p>
        </div>
      </div>`);
  }

  // ── 诊断 Tab（账号体检）─────────────────────────────────────────────────
  // 判不准的项一律显示「未判定」，不显示「正常」。假的正常比如实的未判定更危险：
  // 客户信了「账号一切正常」，第二天被封，会觉得是软件在骗人。
  const DIAG_COLS = [
    ["login", "登录状态"],
    ["wxstat", "视频号状态"],
    ["live", "直播状态"],
    ["heat", "加热检测"],
    ["fans_limit", "近期涨粉限制"],
    ["popup", "发布弹窗检测"],
  ];

  async function renderDiag(force) {
    const box = document.querySelector("#pub-diag");
    if (!box) return;
    if (!currentAid()) { S.diag = null; S.abn = null; box.innerHTML = needAccBox(); return; }
    if (!S.diag || force) {
      if (!force) box.innerHTML = `<div class="pub-loading">正在读取…</div>`;
      try { S.diag = await api(`/api/publish/diag?aid=${encodeURIComponent(currentAid())}`); }
      catch (e) { box.innerHTML = `<div class="pub-empty bad">${esc(e.message)}</div>`; return; }
    }
    const d = S.diag || {};
    const rows = d.rows || [];
    const cur = currentAid();
    // 默认只看当前确认的号。其他号放进折叠区（保留「一眼看全号」），
    // 但那里只显示上次结果，不提供体检按钮 —— 后端也会拒。
    const mine = rows.filter((r) => r.aid === cur);
    const others = rows.filter((r) => r.aid !== cur);
    box.innerHTML = `
      <div class="diag-head">
        <div>
          <b>这个号的体检</b>
          <span class="hint">点「体检」会登进这个号看一眼登录状态、视频号状态和违规通知。
            只读，不会改号里任何东西。</span>
        </div>
        <button class="primary sm" data-act="diagOne" data-aid="${esc(cur)}"${S.diagBusy ? " disabled" : ""}>
          ${S.diagBusy ? "正在体检…" : "体检这个号"}</button>
      </div>
      ${mine.length ? diagTable(mine)
        : `<div class="pub-empty">这个号还没体检过，点右上「体检这个号」。</div>`}
      ${others.length ? `<details class="fold"><summary>顺便看看其他 ${others.length} 个号（只看上次结果）</summary>
        ${diagTable(others, true)}</details>` : ""}
      <p class="diag-tip">有几项显示「未判定」：平台没给这几项明确信号，不代表号有问题。
        想确认就打开视频号助手看一眼。</p>
      ${S.abn ? abnModal() : ""}`;
  }

  function diagCell(c) {
    if (!c) return `<span class="dg mute">未检测</span>`;
    const cls = c.ok === true ? "ok" : c.ok === false ? "bad" : "mute";
    // detail 只在鼠标悬停时给（比如加热那一项的条件明细），正文只留一句结论
    const tip = c.detail ? `${c.text || ""}｜${c.detail}` : (c.text || "");
    return `<span class="dg ${cls}" title="${esc(tip)}">${esc(c.text || "—")}</span>`;
  }

  function diagTable(rows, readonly) {
    return `<div class="diag-wrap"><table class="diag-tb">
      <thead><tr><th>账号</th><th>分组</th>
        ${DIAG_COLS.map((c) => `<th>${c[1]}</th>`).join("")}
        <th>违规通知</th><th>近期异常视频</th><th>最后检测时间</th><th>操作</th></tr></thead>
      <tbody>${rows.map((r) => diagRow(r, readonly)).join("")}</tbody></table></div>`;
  }

  function diagRow(r, readonly) {
    const ntf = r.bad_posts || [];
    const n = (r.notify && r.notify.n) || ntf.length;
    const abn = r.abnormal || [];
    return `<tr data-diag="${esc(r.aid)}">
      <td><div class="diag-acc">
        <span class="av">${r.avatar ? `<img src="${esc(r.avatar)}" alt="">`
          : `<i>${esc(String(r.name || "号").slice(0, 1))}</i>`}</span>
        <span class="who"><b>${esc(r.name || "")}</b>
          <em>${esc((r.account && r.account.finder_id) || "")}</em></span></div></td>
      <td><span class="dg mute">默认分组</span></td>
      ${DIAG_COLS.map((c) => `<td>${diagCell(r[c[0]])}</td>`).join("")}
      <td>${!r.checked_at ? `<span class="dg mute">未检测</span>`
        : n ? `<span class="dg bad">${n} 条</span>` : `<span class="dg ok">没有</span>`}</td>
      <td>${!r.checked_at ? `<span class="dg mute">未检测</span>`
        : abn.length
          ? `<button class="abn-link" data-act="abn" data-aid="${esc(r.aid)}">${abn.length} 条</button>`
          : `<span class="dg ok">没有</span>`}</td>
      <td>${esc(r.checked_at ? showTime(r.checked_at) : "—")}</td>
      <td>${readonly
        ? `<span class="dg mute">要体检先切到这个号</span>`
        : `<button class="ghost sm" data-act="diagOne" data-aid="${esc(r.aid)}"
            ${S.diagBusy ? "disabled" : ""}>体检</button>`}</td>
    </tr>`;
  }

  // ── 近期异常视频（弹层）────────────────────────────────────────────────
  // 数据只有两类真信号：① 平台通知点名到作品的；② 作品不在公域可见。
  // **不按播放量低推测「被限流」** —— 低播放的正常作品一大片，列出来就是假警报。
  function abnRow(a) {
    const isNtf = a.kind === "notify";
    const badge = isNtf
      ? `<span class="dg bad">平台通知</span>`
      : `<span class="dg mute">${esc(a.why || "未公开")}</span>`;
    // 徽标已经写明是什么状态了，小字只补一句平台口径，别把同一句重复两遍
    const sub = isNtf ? (a.notice || "") : "没在公域推荐";
    return `<tr>
      <td><div class="abn-post">
        <span class="abn-cov">${a.cover ? `<img src="${esc(a.cover)}" alt="" loading="lazy">` : ""}</span>
        <b title="${esc(a.title)}">${esc(a.title || "（无标题）")}</b></div></td>
      <td class="abn-t">${a.ts ? esc(tsText(a.ts)) : "—"}</td>
      <td>${badge}<span class="abn-why">${esc(sub)}</span></td>
      <td><div class="abn-ops">
        <button class="mini danger" data-act="abnDel" data-pid="${esc(a.id || "")}"
          data-title="${esc(a.title || "")}" data-ts="${esc(a.ts || 0)}"
          ${S.delBusy ? "disabled" : ""}>${S.delBusy ? "正在删…" : "删除这条作品"}</button>
        <button class="mini" data-act="openWx">打开视频号助手</button>
      </div></td>
    </tr>`;
  }

  function abnModal() {
    const r = (S.abn && S.abn.row) || {};
    const list = r.abnormal || [];
    const who = [r.name, (r.account && r.account.finder_id) || ""].filter(Boolean).join(" · ");
    return `<div class="pub-mask" data-act="abnClose">
      <div class="pub-modal" data-modal>
        <div class="pm-head">
          <b>近期异常视频</b><span>${esc(who)}</span>
          <button class="mini" data-act="abnClose">关闭</button>
        </div>
        ${list.length ? `<div class="abn-wrap"><table class="abn-tb">
          <thead><tr><th>作品</th><th>发布时间</th><th>处置说明</th><th>操作</th></tr></thead>
          <tbody>${list.map(abnRow).join("")}</tbody></table></div>`
          : `<div class="pub-empty">这个号最近没读到异常作品。</div>`}
        <p class="diag-tip">这里只列平台真给过信号的作品：被通知点名的，以及不在公域可见的。
          平台没说过「推荐量减少」这句话，所以不写。「删除这条作品」会把平台上的内容
          一起删掉，<b>删了找不回来</b>；点之前会再问一次，删完还会回列表核对。
          <b>目前这一下平台还没响应成功</b>，点完会如实告诉你结果，没删掉请用旁边的
          「打开视频号助手」手动处理。申诉仍要本人到通知中心办。</p>
      </div></div>`;
  }

  // ── 数据 / 诊断的操作 ───────────────────────────────────────────────────
  async function actStat(name, el) {
    if (name === "monToggle") {
      const want = !(S.mon && S.mon.on);
      try {
        S.mon = await api(`/api/publish/monitor?on=${want ? "true" : "false"}&every_min=3`
          + `&aid=${encodeURIComponent(currentAid())}`,
          { method: "POST", body: JSON.stringify({}) });
        toast(want ? "开始自动记录了，会定时去看一次播放量" : "已停止，之前读到的还在");
        renderStat(true);
      } catch (e) { toast(e.message); }
      return;
    }
    if (name === "statCols") {
      S.statCols = S.statCols === 2 ? 1 : 2;
      renderStat(false);
      return;
    }
    if (name === "statNotice") { openStatNotice(); return; }
    if (name === "statNoticeClose") {
      S.statNoticeOpen = false;
      const p = document.querySelector("#stat-notice-panel");
      if (p) p.remove();
      return;
    }
    if (name === "statClear") {
      if (S.statBusy) return;
      if (!window.confirm("清空这个号所有已记录的数据？\n只清记录，不影响平台上的作品。删了找不回来。")) return;
      try {
        const r = await api(`/api/publish/stat/clear?aid=${encodeURIComponent(currentAid() || "")}`,
          { method: "POST", body: JSON.stringify({}) });
        S.stat = null;
        S.mon = null;
        toast(r.removed ? "已清空这个号的数据记录" : "这个号本来就没有记录");
        renderStat(true);
      } catch (e) { toast(e.message); }
      return;
    }
    if (name !== "statSample") return;
    if (S.statBusy) return;
    S.statBusy = true;
    renderStat(true);
    try {
      const d = await api(`/api/publish/stat/sample?aid=${encodeURIComponent(currentAid() || "")}`,
        { method: "POST", body: JSON.stringify({}) });
      S.stat = d;
      S.statBusy = false;
      renderStat(true);
      toast(`读到了 ${(d.posts || []).length} 条作品`);
      if (d.note) toast(d.note);
    } catch (e) {
      S.statBusy = false;
      renderStat(true);
      toast(e.message);
    }
  }

  async function actDiag(name, el, ev) {
    if (name === "abn") {
      const aid = el.dataset.aid;
      const row = ((S.diag && S.diag.rows) || []).find((x) => x.aid === aid) || {};
      S.abn = { row: row };
      renderDiag();        // 不 force：只弹层，别又开一次浏览器
      return;
    }
    if (name === "abnClose") {
      // 点弹层本体不该关（只有点和「关闭」才关）
      if (ev && ev.target.closest("[data-modal]") && ev.target !== el) return;
      S.abn = null;
      renderDiag();
      return;
    }
    if (name === "openWx") {
      window.open(WX_PLATFORM, "_blank", "noopener");
      return;
    }
    if (name === "abnDel") {
      const title = el.dataset.title || "（无标题）";
      const pid = el.dataset.pid || "";
      const when = Number(el.dataset.ts || 0) ? tsText(Number(el.dataset.ts)) : "时间没读到";
      const who = accLabel(currentAid());
      // 不可逆：作品名 + 发布时间 + 号名 + 后果，四样都写全再问
      if (!confirm(`要删掉这条作品吗？\n\n【${title}】\n发布：${when}\n账号：${who}\n\n`
        + "删除后找不回来，平台上的播放、点赞、评论也会一起没了。\n"
        + "程序会去列表里核对一遍，没删掉会如实告诉你。\n\n确认删除吗？")) return;
      if (S.delBusy) return;
      S.delBusy = true;
      renderDiag();
      try {
        const r = await api("/api/publish/abn/delete", {
          method: "POST",
          body: JSON.stringify({ aid: currentAid(), post_id: pid, title: title,
                                 dry_run: false, confirm: true }),
        });
        if (r && r.ok) {
          const row = (S.abn && S.abn.row) || {};
          row.abnormal = (row.abnormal || []).filter(
            (x) => !((x.title || "") === title && String(x.ts || 0) === String(el.dataset.ts || 0)));
          toast(`已删除：${title}`);
        } else {
          toast((r && r.note) || "没删掉，点「打开视频号助手」手动看一眼");
        }
      } catch (e) { toast(e.message); }
      S.delBusy = false;
      renderDiag();
      return;
    }
    const one = async (aid) => {
      const r = await api(`/api/publish/diag/${encodeURIComponent(aid)}`,
        { method: "POST", body: JSON.stringify({}) });
      const rows = (S.diag && S.diag.rows) || [];
      const i = rows.findIndex((x) => x.aid === aid);
      if (i >= 0) rows[i] = Object.assign({}, rows[i], r);
      return r;
    };
    if (name === "diagOne") {
      const aid = el.dataset.aid;
      if (!aid || S.diagBusy) return;
      if (aid !== currentAid()) { toast("只能体检当前确认的这个号；想看别的号先切过去"); return; }
      S.diagBusy = true;
      renderDiag(true);
      try { await one(aid); toast("体检完了"); }
      catch (e) { toast(e.message); }
      S.diagBusy = false;
      renderDiag(true);
      return;
    }
    if (name === "diagAll") {
      // 兼容旧按钮：现在只体检当前号，不再一次跑全部号
      const aid = currentAid();
      if (!aid || S.diagBusy) return;
      S.diagBusy = true;
      renderDiag(true);
      try { await one(aid); toast("体检完了"); }
      catch (e) { toast(e.message); }
      S.diagBusy = false;
      renderDiag(true);
      return;
    }
  }

  async function load(quiet) {
    if (!S.first) S.first = defFirst();
    let d;
    try {
      d = await api("/api/publish/queue");
    } catch (e) {
      BODY().innerHTML = `<div class="pub-empty bad">${esc(e.message)}</div>`;
      return;
    }
    const prevAid = S.aid;
    S.d = d;
    S.aid = d.current_account_id || "";
    if (prevAid !== S.aid) {
      // 这个页面或别的页面切了号：旧号的数据一律作废，绝不留在新号页面上
      S.stat = null; S.diag = null; S.mon = null; S.abn = null;
      S.opts = {}; S.optFor = ""; S.optState = ""; S.optSeq++;
      S.verifiedCur = "";          // 新号还没验过：下一轮 load 会补一次核对
      if (S.tab === "stat") renderStat(true);
      if (S.tab === "diag") renderDiag(true);
    }
    const key = JSON.stringify((d.jobs || []).map((j) => [j.id, j.status, j.plan_at, j.title]))
      + (d.gate ? d.gate.ok + d.gate.reason : "");
    if (quiet && key === S.key) { paintBadge(d); paintGlobalAcc(); optWaitTick(); ensureOpts(); reconcileAccounts(); return; }
    S.key = key;
    render();
    maybePlan();
    paintBadge(d);
    optWaitTick();
    ensureOpts();
    reconcileAccounts();
    // 失效兜底的登录面板跟号走：目标号恢复了 / 当前号切走了 → 收面板。
    // 不收的话，卡片会挂在一个跟当前操作无关的号上，看着像「当前号坏了」
    // （2026-11-14 用户反馈：当前号明明已登录，却挂着别的号的失效卡）。
    // 只对 auto 弹的生效：用户主动添加的号在登完之前还不是「当前号」，
    // 拿当前号去比会把刚建的空壳当场删掉。
    if (S.login && S.login.auto) {
      const tgt = (d.accounts || []).find((a) => a.id === S.login.aid);
      if (!tgt || tgt.state === "online" || currentAid() !== S.login.aid) cancelLogin(true);
    }
    maybeAutoLogin(d);
    // 页面打开后的第一次 load：当前号哪怕亮着「已登录」也真核对一次
    // （2026-11-14 用户反馈：会话被踢了卡片还亮着已登录，等到读取失败才发现）
    if (!S.verifiedCur && currentAid()) verifyOnSwitch(currentAid());
  }

  /** 当前号的登录态是「过期」→ 自动弹出登录窗口
   *  （2026-11-14 用户要求：「选择了登录过期的账号，不应该是到扫码的时候了吗」）。
   *  一次页面会话每个号只自动弹一次；用户点「取消」后不再纠缠。
   *  要不要弹本身会先自查登录态：其实已经续上了就直接收摊。 */
  function maybeAutoLogin(d) {
    const cur = d && d.current_account;
    if (!cur || cur.state !== "expired" || S.login) return;
    S.loginOffered = S.loginOffered || {};
    if (S.loginOffered[cur.id]) return;
    S.loginOffered[cur.id] = true;
    openLogin(cur.id, { auto: true, label: accLabel(cur.id) });
  }

  /** 切号即验号（2026-11-14 用户定，回应两个实拍问题）：
   *  ① 状态是本地簿记——会话被平台踢了卡片还亮着「已登录」（重启服务后
   *     尤其如此，对账逻辑只复查非 online 的号）；② 切到「登录过期」的号
   *     以前只被动等读取失败/轮询兜底，用户还得手点「重登」。
   *  现在切到哪个号就对哪个号真导航核对一次：online 的也验（防假已登录），
   *  非 online 一律直接弹出登录窗口。
   *  fromSwitch：切号触发的 = 用户明确要用这个号，清掉「只弹一次」的礼貌闸
   *  直接弹窗；页面加载触发的 = 被动兜底，尊重用户之前点过的「取消」。 */
  async function verifyOnSwitch(aid, fromSwitch) {
    if (!aid || S.login) return;
    S.verifiedCur = aid;        // 本次页面会话验过了，load() 不再重复
    const acc = ((S.d && S.d.accounts) || []).find((x) => x.id === aid) || {};
    if (acc.state === "online") {
      try {
        // hard=1 真导航：落在登录墙 = 会话死了，后端会就地标成 expired。
        // r.error 是浏览器层出错（号可能在忙上传），不武断下结论。
        const r = await apiT(`/api/publish/accounts/${aid}/login-state?hard=1`,
          {}, 50000);
        if (!r || r.error || r.logged_in !== false) return;   // 真登录着，没事
        // 状态就地刷新（r.accounts 是后端更新过的全量账号卡），别等下一轮轮询；
        // 只重画账号条，别整页 render（用户可能正打着字）
        if (r.accounts && S.d) { S.d.accounts = r.accounts; paintGlobalAcc(); }
      } catch (e) { return; }   // 核对失败不打扰：读取失败时 probeLoginOnFail 还会兜
    }
    if (S.login) return;   // 核对这几十秒里别的流程已经开了登录窗口，别抢
    const cur = ((S.d && S.d.accounts) || []).find((x) => x.id === aid) || {};
    if (cur.state && cur.state !== "online") {
      S.loginOffered = S.loginOffered || {};
      if (!fromSwitch && S.loginOffered[aid]) return;   // 用户取消过：不再纠缠
      delete S.loginOffered[aid];   // 切号触发 = 明确要用这个号，直接弹窗
      openLogin(aid, { auto: true, label: accLabel(aid) });
    }
  }

  /** 对一遍账：把每个号的**真实**登录态读回来，更新列表。
   *
   *  为什么要有：账号状态是存在本地的。登录那一刻如果前端没赶上（页面刷新、
   *  轮询被浏览器打断、窗口被关），库里就永远停在「待登录」，而实际早就登录
   *  好了 —— 用户看到的就是「明明已经登录了，还一直说要登录」
   *  （2026-11-13 用户反馈）。打开页面时对一次账，这条路就通了。
   */
  let lastReconcile = 0;
  async function reconcileAccounts() {
    const now = Date.now();
    if (now - (lastReconcile || 0) < 45000) return;
    lastReconcile = now;
    try {
      const r = await api("/api/publish/accounts/state");
      if (!r || !r.accounts || !S.d) return;
      const sig = (a) => (a || [])
        .map((x) => `${x.id}:${x.state}:${x.wx_name || ""}:${x.finder_id || ""}`).join("|");
      if (sig(r.accounts) === sig(S.d.accounts)) return;
      S.d.accounts = r.accounts;
      render();
      ensureOpts();
    } catch (e) { /* 对账失败不打扰，下一轮再来 */ }
  }

  /** 这个号的批量上传还占着浏览器吗：「排到即上传」，queued/publishing 的
   *  条目会一条条占住这个号的 session 线程（每条好几分钟）。预取排进去只会
   *  白等 95 秒然后超时，最后被当成「没读到选项」——其实号没坏，是在忙。 */
  function accBusy(aid) {
    return ((S.d && S.d.jobs) || []).some((j) =>
      j.account_id === aid && (j.status === "queued" || j.status === "publishing"));
  }

  /** 等待态/失败态跟着上传进度走（轮询每次都问一遍）：
   *  - 号忙 → 失败态改成等待态（说明白「在传上一批」，不是坏了）；
   *  - 号忙完 → 等待态自动补读一次，用户不用盯也不用挨个点重试。 */
  function optWaitTick() {
    const aid = currentAid();
    if (!aid) return;
    const busy = accBusy(aid);
    if (S.optState === "fail" && busy) {
      S.optState = "wait"; S.optFor = aid;
      refreshFields();
    }
    if (S.optState === "wait" && !busy) ensureOpts(true);
  }

  /** 候选回来了只重画 ②：用户正在这个区里打字时先别重画，数据留在
   *  S.opts，等焦点离开补一次，绝不把打到一半的字清掉。 */
  function safeRefreshFields(retry) {
    const ae = document.activeElement;
    const typing = ae && /^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName || "")
      && ae.closest && ae.closest("#pub-fields");
    if (typing) {
      if (!retry) setTimeout(() => safeRefreshFields(true), 1200);
      return;
    }
    refreshFields();
  }

  /** 把一次批量读取的结果放进 S.opts。主请求和后台静默更新共用一份。 */
  function applyOpts(r) {
    ["declare", "link"].forEach((k) => { if (r[k]) S.opts[k] = r[k]; });
    // 位置是用户输词才搜出来的，批量预取里那一支永远是「输入地名」空占位。
    // 别让后台更新一次就把刚选好的地址洗没了。
    if (r.location && !(S.opts.location && (S.opts.location.items || []).length)) {
      S.opts.location = r.location;
    }
  }

  /** 选好号就把这个号能自动读的候选一次拉全。
   *
   *  候选按号缓存在后端（data/options_cache/）：开页命中缓存是毫秒级，只有
   *  第一次 / 缓存过期 / 点「重新读一次」才真开一次浏览器（约 18 秒）。
   *  过期时后端先把旧数据给出来，再由 silentRefreshOpts 在后台悄悄更新，
   *  全程不摆加载条、不整页重画、不挡任何输入。
   *
   *  仍然只走**一个**批量接口：session 的 page 是复用的，同一号的操作排在
   *  一个线程里，连发会各开一次浏览器、串行排队，还会互相踩弹层状态。
   *  位置不预取：平台要地名才给候选，没关键词时读不到任何东西。
   */
  function ensureOpts(force, refresh) {
    const aid = currentAid();
    if (!aid) { S.optFor = ""; return; }
    const acc = ((S.d && S.d.accounts) || []).find((x) => x.id === aid) || {};
    if (acc.state !== "online") return;
    if (!force && !refresh && S.optFor === aid) return;
    // load() 是轮询，失败后别每隔几秒就重开一次浏览器；单项手动重试不受限
    if (!force && Date.now() - (S.optFailAt || 0) < 5 * 60 * 1000) return;
    // 同一个号已有一次强制重读在跑：别再排一个 18 秒进单线程队列
    if (refresh && S.optRefreshAid === aid) return;
    // 号在传上一批：预取只会排队白等，摆个等待态；传完 load() 里的
    // optWaitTick 会自动补读。急的用户仍可以点字段里的按钮单项读。
    if (accBusy(aid)) {
      S.optFor = aid;
      S.optState = "wait";
      stopOptStage();
      refreshFields();
      return;
    }
    S.optFor = aid;
    const seq = ++S.optSeq;
    S.optState = "loading";
    S.optLoadingAt = Date.now();
    S.optFirstRead = !(S.opts.declare || S.opts.link);
    if (refresh) S.optRefreshAid = aid;
    refreshFields();
    startOptStage();
    const url = `/api/publish/options/all?aid=${encodeURIComponent(aid)}`
      + (refresh ? "&refresh=1" : "");
    apiT(url)
      .then((r) => {
        // 中途换了号：旧响应直接丢，别把上一个号的剧集塞给这个号
        if (seq !== S.optSeq || currentAid() !== aid) return;
        if (!r || r.error) {
          S.optFor = ""; S.optFailAt = Date.now(); S.optState = "fail";
          S.optNote = (r && r.note) || "";
          stopOptStage();
          safeRefreshFields();
          probeLoginOnFail(aid);   // 失败最常见的原因就是登录掉了，问一句
          return;
        }
        applyOpts(r);
        S.optState = "";
        S.optNote = r.note || "";
        S.optStale = !!r.stale;
        S.optReadAt = r.cached ? Date.now() - (r.age || 0) * 1000 : Date.now();
        stopOptStage();
        safeRefreshFields();
        if (r.login) probeLoginOnFail(aid);
        if (!r.cached) toast(refresh ? "选项更新好了" : "这个号的选项读好了");
        // 后端说这份是旧的：号空闲就静默更新，界面上一直留着可用的旧选项
        if (r.stale && !r.refreshing && !refresh) silentRefreshOpts(aid);
      })
      .catch(() => {
        if (seq !== S.optSeq) return;
        S.optFor = ""; S.optFailAt = Date.now(); S.optState = "fail";
        stopOptStage();
        safeRefreshFields();
        probeLoginOnFail(aid);     // 95 秒超时这种，多半也是卡在登录页上
      })
      .finally(() => {
        if (refresh && S.optRefreshAid === aid) S.optRefreshAid = "";
      });
  }

  /** 缓存过期后的后台更新：不摆加载条、不整页重画、不弹窗。
   *  号在传上一批就跳过（别去 session 单线程队列里挤），等 optWaitTick
   *  忙完补读时自然还会再试。响应只更新下拉，绝不因 stale 再触发一次自己。 */
  function silentRefreshOpts(aid) {
    if (S.optRefreshAid === aid || accBusy(aid)) return;
    S.optRefreshAid = aid;
    const seq = S.optSeq;
    apiT(`/api/publish/options/all?aid=${encodeURIComponent(aid)}&refresh=1`)
      .then((r) => {
        if (seq !== S.optSeq || currentAid() !== aid) return;
        if (r && !r.error) {
          applyOpts(r);
          S.optNote = r.note || "";
          S.optStale = !!r.stale;
          S.optReadAt = Date.now();
          safeRefreshFields();
        }
      })
      .catch(() => {})
      .finally(() => {
        if (S.optRefreshAid === aid) S.optRefreshAid = "";
      });
  }

  function paintBadge(d) {
    const b = document.querySelector("#pub-badge");
    if (!b || !d) return;
    const n = (d.summary && d.summary.pending) || 0;
    b.textContent = n > 99 ? "99+" : String(n);
    b.classList.toggle("hidden", n === 0);
    b.classList.toggle("due", (d.summary && d.summary.due && d.summary.due.length) > 0);
  }

  function startPoll() {
    stopPoll();
    S.poll = setInterval(() => {
      // 只在「正在打字」时跳过（别把打到一半的字清掉）。以前只要焦点在发布页
      // 内就整轮跳过——用户盯着页面看上传进度时反而永远不更新，观感像卡死。
      const ae = document.activeElement;
      const typing = ae && /^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName || "")
        && ae.closest && ae.closest("#view-publish");
      if (typing) return;
      if (!VIEW() || VIEW().classList.contains("hidden")) return;
      load(true);
    }, 15000);
  }
  function stopPoll() { if (S.poll) clearInterval(S.poll); S.poll = null; }

  /** ① 那一整块（候选卡片 + 底部按钮行）。单独成函数是为了「勾一条」「选完
   *  文件夹」「清空」只重画这里 —— 以前这些都走整页 render()，②里正在打的
   *  话题、下拉展开态、输入焦点会跟着被冲掉（2026-11-21 用户反馈「点文案
   *  左边整块刷一下」时顺手一起修的）。 */
  function candSec(noAcc) {
    return `<section class="pub-sec" id="pub-sec1">
            <div class="pub-sec-h"><span class="pub-n">1</span>
              <div class="pub-h-txt"><b>选视频</b><em>整个文件夹、几个文件，或者从某个项目里挑成片</em></div></div>
            ${candBox()}
            <div class="pub-acts">
              <button class="ghost sm" data-act="pickdir">＋ 选文件夹</button>
              <button class="ghost sm" data-act="pickfiles">＋ 选视频文件</button>
              <button class="ghost sm" data-act="pickproj">＋ 从项目挑</button>
              <span class="hint">${noAcc ? "先在上面账号条选一个号"
                : S.sel.size ? `已选 ${S.sel.size} 条` : "先勾几条视频"}</span>
              <button class="primary sm" data-act="add"
                ${(S.sel.size && !noAcc && !S.addBusy) ? "" : "disabled"}
                title="${S.addBusy ? "正在加入，稍等一下"
                  : noAcc ? "先在上面账号条选一个号"
                  : !S.sel.size ? "先勾几条视频" : "把勾中的视频加进队列"}"
                >${S.addBusy ? `正在加入…（${S.addN} 条）` : "加入队列"}</button>
            </div>
            <div class="hist-chips" id="pub-hist-dir"></div>
          </section>`;
  }

  /** 右栏（队列 / 已提交）的内容。抽出来之后，点「文案」展开一条、切
   *  「队列↔已提交」、换筛选都只重画这一列，左边的 ②③ 一概不动。 */
  function rightHTML(d) {
    const jobs = d.jobs || [];
    const drafts = jobs.filter((j) => j.status === "draft").length;
    // 队列 = 设置工作台（还没安排的草稿）；已提交 = 点完「开始上传」的
    // 后台任务（等着传 / 正在传 / 已定时 / 已提交 / 已发布 / 失败 / 已取消）。
    // 开始上传成功后队列清空、通用设置恢复默认，下一批从头填
    // （2026-11-14 用户定稿：后台任务不占设置工作台）。
    const draftJobs = jobs.filter((j) => j.status === "draft");
    const submittedJobs = jobs.filter((j) => j.status !== "draft");
    // 已提交按号隔离（2026-11-14 用户反馈「切号后队列数据没变」）：任务自己
    // 带着账号走，右边就只看当前号的；没选号时不过滤（页面刚打开还没号，
    // 全部摆出来，选了号再看自己的）。草稿属于设置工作台，跟号无关，不过滤。
    const cur = currentAid();
    const mySubmitted = cur
      ? submittedJobs.filter((j) => j.account_id === cur) : submittedJobs;
    const otherSubmitted = cur
      ? submittedJobs.filter((j) => j.account_id !== cur) : [];
    const queuedN = mySubmitted.filter((j) => j.status === "queued").length;
    const failedN = mySubmitted.filter((j) => j.status === "failed").length;
    const noAcc = !cur;
    // 已提交一多（真号 20+ 条很常见）就得能一眼找到要管的那条
    const TODO_ST = ["queued", "publishing", "failed", "uncertain", "manual"];
    const todoN = mySubmitted.filter((j) => TODO_ST.includes(j.status)).length;
    const qf = S.qfilter || "all";
    const shownSubmitted = qf === "todo"
      ? mySubmitted.filter((j) => TODO_ST.includes(j.status))
      : qf === "done"
        ? mySubmitted.filter((j) => !TODO_ST.includes(j.status))
        : mySubmitted;
    const startTxt = noAcc ? "先在上面账号条选号"
      : `开始上传（${drafts} 条）`;
    // 右侧预览里的警告容易漏看，在开始按钮旁再摆一条精简版（完整版进 title）
    const warnMsg = ((S.plan && (S.plan.warns || [])[0]) || {}).msg || "";
    return `
          <div class="pub-qhead">
            <div class="seg pub-qtabs">
              <button class="pub-qtab${S.hist ? "" : " on"}" data-act="qtab" data-hist="0">队列${draftJobs.length ? " " + draftJobs.length : ""}</button>
              <button class="pub-qtab${S.hist ? " on" : ""}" data-act="qtab" data-hist="1">已提交${mySubmitted.length ? " " + mySubmitted.length : ""}</button>
            </div>
          </div>
          ${!S.hist ? `
          <div id="pub-plan" class="pub-plan"></div>
          ${draftJobs.length ? `<div class="pub-rows">${draftJobs.map((j, i) => row(j, i)).join("")}</div>`
            : `<div class="pub-empty">队列是空的。左边选好视频、设好时间和内容，点「开始上传」。</div>`}
          <div class="pub-start-warn" id="pub-start-warn"
            style="${(drafts && warnMsg) ? "" : "display:none"}"
            title="${esc(warnMsg)}">${esc(
              warnMsg.length > 88 ? warnMsg.slice(0, 88) + "…" : warnMsg)}</div>
          <div class="pub-foot">
            <button class="ghost sm" data-act="clear" ${noAcc || !drafts ? "disabled" : ""}
              title="把「还没安排」的条目从队列里删掉">删掉没安排的${drafts ? `（${drafts} 条）` : ""}</button>
            ${drafts || noAcc ? `<button class="primary" data-act="start" ${noAcc ? "disabled" : ""}>${startTxt}</button>` : ""}
          </div>`
          : (mySubmitted.length
            ? `${failedN ? `<div class="pub-acts"><button class="ghost sm" data-act="retryAll" title="只重试当前号的失败条目">重试失败 ${failedN} 条</button></div>` : ""}
              <div class="pub-qfilters">
                <button class="pub-qf${qf === "all" ? " on" : ""}" data-act="qfilter" data-f="all">全部 ${mySubmitted.length}</button>
                <button class="pub-qf${qf === "todo" ? " on" : ""}" data-act="qfilter" data-f="todo">要处理的 ${todoN}</button>
                <button class="pub-qf${qf === "done" ? " on" : ""}" data-act="qfilter" data-f="done">已完成的 ${mySubmitted.length - todoN}</button>
              </div>
              ${shownSubmitted.length
                ? `<div class="pub-rows">${shownSubmitted.map((j) => row(j, mySubmitted.indexOf(j))).join("")}</div>`
                : `<div class="pub-empty">这一类现在是空的，切到「全部」看其它条目。</div>`}
              <div class="pub-foot">
            <button class="ghost sm" data-act="stop" ${noAcc || !queuedN ? "disabled" : ""}
              title="取消当前号还在排队、没上传的条目，退回队列重新设置；已经传到平台的取消不了，得到视频号助手手动删">批量取消${queuedN ? `（${queuedN} 条）` : ""}</button>
              </div>
              ${otherSubmitted.length ? `<div class="pub-acts"><span class="hint">另外有 ${otherSubmitted.length} 条任务在别的号上，切到那个号就能看到。</span></div>` : ""}`
            : `<div class="pub-empty">这个号还没有提交过的任务。点「开始上传」之后，交出去的都在这里跟踪。</div>
              ${otherSubmitted.length ? `<div class="pub-acts"><span class="hint">另外有 ${otherSubmitted.length} 条任务在别的号上，切到那个号就能看到。</span></div>` : ""}`)}`;
  }

  /** 只重画 ①。整页 render() 会把 ② 重建：正在打的字、下拉展开态、焦点全丢。 */
  function paintCand() {
    const sec = document.getElementById("pub-sec1");
    if (!sec) { render(); return; }       // 还没画过整页（或停在门禁页）就退回整页
    sec.outerHTML = candSec(!currentAid());
    paintHist();
  }

  /** 只重画右栏。排期预览（#pub-plan）是 doPlan 异步填进去的，重建完要还回去，
   *  否则点一下「文案」侧栏那行排期摘要就白了。 */
  /** 只重建右栏。三个坑：
   *  1) 排期预览（#pub-plan）是 doPlan 异步填进去的，重建完要还回去；
   *  2) 右栏的滚动在 `.pub-rows` 内部，innerHTML 一换 scrollTop 归零 ——
   *     在第 11 条点「文案」会直接跳回第 1 条（2026-11-21 用户反馈）；
   *  3) 展开的那条如果被挤出可视区，得最小幅度滚回来，否则点了像没反应。
   */
  function paintRight(resetScroll) {
    const box = document.querySelector(".pub-right");
    const d = S.d;
    if (!box || !d) { render(); return; }
    const planEl = box.querySelector("#pub-plan");
    const planHTML = planEl ? planEl.innerHTML : "";
    const rowsEl = box.querySelector(".pub-rows");
    const keepTop = rowsEl ? rowsEl.scrollTop : 0;
    const keepY = window.scrollY;      // 窄屏下右栏不吸顶，整页也可能被带着滚
    box.innerHTML = rightHTML(d);
    const plan2 = box.querySelector("#pub-plan");
    if (plan2 && planHTML) plan2.innerHTML = planHTML;
    const rows2 = box.querySelector(".pub-rows");
    // 切「队列 / 已提交」、换筛选：那是一份新列表，从头看更合理
    if (rows2) rows2.scrollTop = resetScroll ? 0 : keepTop;
    if (keepY && !resetScroll) window.scrollTo(0, keepY);
    if (S.edit && !resetScroll) {
      const form = box.querySelector(`[data-fedit="${S.edit}"]`);
      const row = form && form.closest(".pub-row");
      if (row) row.scrollIntoView({ block: "nearest" });
    }
  }

  // ── 渲染 ──────────────────────────────────────────────────────────────
  function render() {
    const d = S.d;
    if (!d) return;
    paintGlobalAcc();
    const g = d.gate || { ok: true };
    if (!g.ok) {
      const tips = {
        offline: ["网络异常，暂时用不了",
          "现在连不上服务器，确认不了你的版本权限。等网络恢复再打开这一页就行，已定时的不会丢。"],
        stale: ["登录状态已失效",
          "服务器那边这张登录凭证已经作废了（换过机器登录或改过密码都会这样）。退出账号重新登录一次再进来。"],
        need_pro: ["此功能需要专业版",
          "批量定时发布是专业版功能。开通后一次排一批，到点提醒你发。"],
      };
      const [head, body] = tips[g.reason] || ["暂时用不了", "稍后再试一次。"];
      BODY().innerHTML = `<div class="pub-gate"><b>${esc(head)}</b><p>${esc(body)}</p></div>`;
      return;
    }

    const noAcc = !currentAid();
    BODY().innerHTML = `
      <div class="pub-cols">
        <div class="pub-left">
          ${candSec(noAcc)}

          <section class="pub-sec">
            <div class="pub-sec-h"><span class="pub-n">2</span>
              <div class="pub-h-txt"><b>这批视频的通用设置</b><em>整批一样才放这儿；封面图全批共用，个别视频可单独换</em></div></div>
            ${noAcc
              ? `<div class="pub-need-acc slim">选好号之后，这里会列出这个号的剧集、位置和标注。</div>`
              : batchBox(d)}
          </section>

          <section class="pub-sec">
            <div class="pub-sec-h"><span class="pub-n">3</span>
              <div class="pub-h-txt"><b>什么时候发</b><em>只填第一条和间隔，后面的按间隔往后推</em></div></div>
            <div class="pub-time">
              <label>第一条在<input type="datetime-local" id="pub-first" value="${esc(S.first)}"></label>
            </div>
            <div class="pub-gaps">
              <span class="pub-gaps-l">每条间隔</span>
              ${[5, 10, 30, 60, 120, 180].map((m) => `<button
                class="pub-gapb${S.gap === m ? " on" : ""}" data-gap="${m}"
                >${m >= 60 ? m / 60 + " 小时" : m + " 分钟"}</button>`).join("")}
              <label class="pub-gapx">自定义
                <input type="number" id="pub-gap" min="5" step="5" value="${S.gap}">分钟</label>
            </div>
            <div class="pub-times" id="pub-times"></div>
          </section>

          <details class="fold">
            <summary>自动发是怎么回事 / 什么情况会失败</summary>
            <div class="pub-note">
              <p><b>点「开始上传」就直接传</b>：程序立刻把每条视频传上视频号助手、填好内容、
              用<b>平台自己的定时发表</b>定在你排的时间，再回「待发布」核对一遍。核对不上会
              标成失败，不会假装发成功。全部传完后电脑关了都没关系——到点平台自己发。
              上传一条要几分钟且逐条来，所以第一条时间别贴着现在排。</p>
              <p><b>传上去之前反悔</b>：在「已提交」里，等着上传的行内点「不上传这条」
              退回队列；失败的行内有「重试」「重新登录」。整批都不要了，
              点「已提交」下面的「批量取消」。
              <b>已经传到平台上的，程序删不掉</b>——要撤就去视频号助手
              （右上角「进入视频号助手」直达）的「待发布」里自己删。</p>
              <p><b>间隔别太小</b>：同一个号 5 分钟连发几条容易被当成同质化搬运，
              同批素材建议 30 分钟以上。</p>
            </div>
          </details>
        </div>

        <aside class="pub-right">${rightHTML(d)}</aside>
      </div>`;
    paintHist();
  }

  /** ①「最近用过」的文件夹：后端记着每次确认过的位置（app/pickmem.py），
   *  点一下直接列出里面的视频，不再走一遍原生对话框。
   *  同一批素材天天发，这一步本来纯属多余；选好之后这块就撤掉，不占地方。 */
  async function paintHist() {
    const box = document.getElementById("pub-hist-dir");
    if (!box) return;
    if (S.cand.length || S.picker === "proj") { box.innerHTML = ""; return; }
    let items = [];
    try {
      const r = await api("/api/pick-history?key=publish_dir&limit=3");
      items = (r && r.items) || [];
    } catch (e) { return; }
    if (!document.body.contains(box)) return;       // 期间整页重画过了，别写进死节点
    box.innerHTML = items.length
      ? `<span class="hist-l">最近用过</span>` + items.map((it) =>
        `<button class="hist-chip" data-act="recentdir" data-path="${esc(it.path)}"
          title="${esc(it.path)}">${esc(tailDir(it.path))}</button>`).join("")
      : "";
  }


  // ── ① 选视频 ──────────────────────────────────────────────────────────
  function candBox() {
    if (S.picker === "proj") return projPicker();
    if (!S.cand.length) {
      return `<div class="pub-empty">还没选视频。<br>
        「选文件夹」会把这个文件夹里的视频全列出来（含子文件夹），勾你要发的几条；
        也可以只挑几个文件，或者从以前的项目里拿成片。</div>`;
    }
    const from = S.picker === "dir" ? `文件夹：${esc(S.pname || "")}`
      : S.picker === "proj" ? `项目：${esc(S.pname || "")}`
      : "本机选的文件";
    return `<div class="pub-src"><span class="mini-note">${from}</span>
        <button class="mini" data-act="clearCand">清空</button></div>
      <div class="pub-picks">${S.cand.map((c, i) => {
        const on = S.sel.has(i);
        return `<label class="pub-pick${on ? " on" : ""}">
          <input type="checkbox" data-cand="${i}" ${on ? "checked" : ""}>
          <b>${esc(c.tag || c.name || "")}</b>
          <span>${esc(c.name || "")}${c.size ? " · " + mb(c.size) : ""}</span>
        </label>`;
      }).join("")}</div>`;
  }

  function projPicker() {
    const list = S.opts.projs || [];
    if (!list.length) {
      return `<div class="pub-empty">还没有出过片的项目。等有项目出完片，这里会列出来。</div>
        <div class="pub-acts"><button class="ghost sm" data-act="pickdir">＋ 选文件夹</button></div>`;
    }
    return `<div class="pub-proj">
      <label>哪个项目
        <select id="pub-proj-sel">
          <option value="">— 选一个 —</option>
          ${list.map((p) => `<option value="${esc(p.id)}"${S.pid === p.id ? " selected" : ""}>
            ${esc(p.name)}（${p.n} 套可用）</option>`).join("")}
        </select>
      </label>
      <button class="ghost sm" data-act="backCand">返回</button>
    </div>
    ${S.cand.length ? `<div class="pub-picks">${S.cand.map((c, i) => {
      const on = S.sel.has(i);
      return `<label class="pub-pick${on ? " on" : ""}">
        <input type="checkbox" data-cand="${i}" ${on ? "checked" : ""}>
        <b>${esc(c.tag || "")}</b><span>${esc(c.name || "")}${c.size ? " · " + mb(c.size) : ""}</span>
      </label>`;
    }).join("")}</div>` : `<div class="pub-empty">选一个项目，就能勾它的成片。</div>`}`;
  }

  // ── 账号条（三个 Tab 共用，见骨架 #pub-global-acc）─────────────────────
  /** 三个 Tab 共用的账号条：发布 / 数据 / 诊断全建立在「先确认账号」上。
   *  光靠发布页的卡片高亮不够 —— 切到数据页就看不见了，而数据恰好是
   *  按号分开的。所以这一行放在 Tab 骨架里，切页不消失。 */
  function globalAccBox(d) {
    const aid = (d && d.current_account_id) || "";
    const a = d && d.current_account;
    if (!aid || !a) {
      return `<div class="pub-cur none global"><span>当前账号</span>
        <b>还没选号</b><em>在下面点一张账号卡片就行。</em></div>`;
    }
    return `<div class="pub-cur global">
      <div class="pub-cur-l">
        <span>当前账号</span>
        <b>${esc(a.label || a.name || aid)}</b>
        <em>点别的账号卡片可切换；提交过的任务在右边「已提交」里</em>
      </div>
    </div>`;
  }

  // ── 登录（2026-11-14 用户定：一律弹可见浏览器窗口手动登录，界面里不再显示二维码）
  // 只有一张面板、一个会话状态：添加账号 / 重登 / 切到失效号 / 读取失败兜底
  // 都走这里。以前 S.scan 与 S.fix 两套并行，同屏能摆两张卡、各挂一个定时器。
  /** 登录面板：后端把视频号助手的登录页弹成可见窗口，用户对着真窗口登录
   *  （快捷登录卡、换码、过期刷新平台自己会做），这边只提示 + 等结果。 */
  function loginBox() {
    const L = S.login || {};
    const title = L.label ? `「${esc(L.label)}」要重新登录` : "在弹出的浏览器窗口里登录";
    return `<div class="pub-scan">
      <div class="pub-scan-side">
        <b>${title}</b>
        <p>${L.opening ? "正在打开浏览器窗口…" : "浏览器窗口已经打开了。"}
          在弹出的窗口里<b>扫码</b>，或点 <b>「微信快捷登录」</b>，手机确认后这边就会自动继续。</p>
        <p class="mini-note">登录成功会自动重新加载这个号的选项，不用刷新页面。
          那个窗口用完后可以自己关掉，不用留着。</p>
        ${L.err ? `<p class="bad">${esc(L.err)}</p>` : ""}
        <div class="pub-acts">
          <button class="ghost sm" data-act="loginRetry">再开一次窗口</button>
          <button class="ghost sm" data-act="loginCancel">取消</button>
        </div>
      </div>
    </div>`;
  }

  function paintLogin() { paintGlobalAcc(); }

  /** 顶部全局账号条的全部内容（骨架 #pub-global-acc 里，三个 Tab 共用）。
   *  2026-11-14 用户定：确认账号是发布/数据/诊断一切操作的前提，
   *  不是排片流程的一个步骤——从发布页步骤里挪出来，步骤只剩
   *  选视频 → 通用设置 → 什么时候发。登录面板也挂这儿，三个 Tab 都看得见。 */
  function accountBar(d) {
    const accs = (d && d.accounts) || [];
    const head = globalAccBox(d);
    if (S.login) return `${head}${loginBox()}`;
    return `${head}
      <div class="pub-acc-wrap">
        ${accs.length ? `<div class="pub-accs">${accs.map(accCard).join("")}</div>`
          : `<div class="pub-empty">还没登录过账号。点「添加账号」，会弹出浏览器窗口让你登录。</div>`}
        <div class="pub-acts">
          <button class="ghost sm" data-act="addacc">＋ 添加账号</button>
          <span class="hint">还能加 ${Math.max(0, (d && d.max_accounts || 10) - accs.length)} 个号</span>
        </div>
      </div>`;
  }

  function stopLoginPoll() {
    if (S.login && S.login.timer) clearInterval(S.login.timer);
    if (S.login) S.login.timer = null;
  }

  /** 同时只允许一个登录会话（2026-11-14 用户定）：重登、添加账号、切号、
   *  失效兜底，谁后到谁先把上一个会话**连轮询一起停掉**——只置空会让旧定时器
   *  继续在后台跑，变成界面上看不见的轮询。 */
  function endLogin() {
    stopLoginPoll();
    S.login = null;
  }

  /** 开一个登录会话：后端在**可见浏览器窗口**里打开视频号助手的登录页，
   *  这边只提示 + 每 10 秒问一次登录态（不再截码塞进界面）。
   *  force=true（重登）＝后端先清掉旧登录态，保证换一个微信登录。
   *  auto=true＝失效号自动弹的，用户没点任何按钮，取消后本轮不再纠缠。
   *  label 只在「已知是哪个号」时给（重登/失效），新加的号不猜名字。 */
  async function openLogin(aid, opts) {
    const o = opts || {};
    if (!aid) aid = currentAid();
    if (!aid) { toast("先在上面账号条选号"); return; }
    endLogin();
    // fresh=1 只有「添加账号」这条入口会给：这次会话刚刚 add 出来的空壳号，
    // 取消登录时才可以删。重登/自动兜底都不是 fresh（哪怕号被清成 pending），
    // 取消时只收面板、绝不删号——否则老号会被当成空壳抹掉（2026-11-18 用户报）。
    S.login = { aid: aid, label: o.label || "", err: "", opening: true,
                busy: false, fails: 0, timer: null, auto: !!o.auto,
                fresh: !!o.fresh };
    paintLogin();
    try {
      const r = await apiT(`/api/publish/accounts/${aid}/login${o.force ? "?force=1" : ""}`,
        { method: "POST" }, 170000);
      if (!S.login || S.login.aid !== aid) return;
      if (r.logged_in) {
        // 开窗口的途中登录已经续上了（可能刚好被别的流程救活）——直接收摊重读。
        await loginDone();
        return;
      }
      S.login.opening = false;
      paintLogin();
      startLoginPoll();
    } catch (e) {
      if (S.login) { S.login.opening = false; S.login.err = e.message || "打不开浏览器窗口"; paintLogin(); }
    }
  }

  /** 窗口被关了/没弹出来 → 再开一次（同一个号，沿用当前会话的 label/auto）。
   *  `?reopen=1`：明确告诉后端「上一个窗口没了，起一个全新可见窗口」，
   *  别复用残留的空白标签页（否则点了没反应，2026-11-16 修复）。 */
  async function reopenLogin() {
    if (!S.login) return;
    const aid = S.login.aid;
    S.login.err = ""; S.login.opening = true;
    paintLogin();
    try {
      const r = await apiT(`/api/publish/accounts/${aid}/login?reopen=1`,
        { method: "POST" }, 170000);
      if (!S.login || S.login.aid !== aid) return;
      if (r.logged_in) { await loginDone(); return; }
      S.login.opening = false;
      paintLogin();
      startLoginPoll();
    } catch (e) {
      if (S.login) {
        S.login.opening = false;
        S.login.err = e.message || "打不开浏览器窗口，再点一次试试";
        paintLogin();
      }
    }
  }

  function startLoginPoll() {
    stopLoginPoll();
    if (!S.login) return;
    S.login.busy = false;
    S.login.fails = 0;
    S.login.timer = setInterval(async () => {
      if (!S.login) return;
      // 上一轮还没回来就别再发：接口慢时请求会越堆越多，最后看着像「没反应」。
      if (S.login.busy) return;
      S.login.busy = true;
      try {
        // 不传 hard：探测强度由后端按账号当前 state 决定——正在扫码坐实的号
        // （pending/unknown/expired）走轻量，别让真导航把刚进后台的登录态判丢；
        // 只有 online 号（切号验号）才 hard 识破假登录。传死 hard=1 会让刚扫码
        // 成功的新号一直显示「待登录」（2026-11-18 用户报）。
        const r = await api(`/api/publish/accounts/${S.login.aid}/login-state`);
        if (!S.login) return;
        if (r.error) { S.login.err = r.error; paintLogin(); return; }
        // 后端可能在「探测这一刻」没判到登录（残留标签页/SPA 冷加载），但它
        // 每次都会用写库后的全量 accounts 回传。以库里的真实 state 为准：
        // 只要这个号已经被任何一条路径坐实成 online，就收面板重画，别让界面
        // 停在「扫成功了还显示待登录」（2026-11-18 用户报）。
        const accNow = (r.accounts || []).find((x) => x.id === S.login.aid);
        if (!r.logged_in && !(accNow && accNow.state === "online")) return;
        // 先把后端刚写库的账号状态落进本地，loginDone 里 selectAccount/load
        // 之前账号条就能立刻显示成「已登录」，不用等下一轮队列轮询。
        if (r.accounts && S.d) S.d.accounts = r.accounts;
        await loginDone();
      } catch (e) {
        // 不静默吞掉：登录完「没反应」最常见的原因就是这里连着报错却一个字不说。
        if (!S.login) return;
        S.login.fails = (S.login.fails || 0) + 1;
        if (S.login.fails <= 3) { S.login.err = e.message || "连不上本地服务"; paintLogin(); }
      } finally {
        if (S.login) S.login.busy = false;
      }
    }, 10000);
  }

  /** 登录成功的统一出口：收会话 → 清这个号的候选缓存（剧集/位置/合集按号
   *  缓存，登录态变了就得重读）→ 选它为当前号 → 重新读队列和选项。
   *  登录状态本身由后端在 login-state 探测成功那一刻写库置 online。 */
  async function loginDone() {
    const aid = S.login ? S.login.aid : "";
    endLogin();
    paintLogin();
    if (!aid) return;
    S.opts = {}; S.optFor = ""; S.optState = ""; S.optFailAt = 0; S.optSeq++;
    await selectAccount(aid, true);
    const acc = ((S.d && S.d.accounts) || []).find((x) => x.id === aid);
    toast(acc ? `已登录：${acc.label || acc.name || aid}` : "登录成功，正在重新加载这个号的选项");
    await load();
    // load() 里的 ensureOpts() 已经会读：后端在登录成功那一刻清掉了该号
    // 缓存，所以这一次必然真读一遍并写回新缓存。这里再调一次只会让同一个
    // 号排两个 18 秒的浏览器任务（以前就重复，加缓存后更明显）。
  }

  /** 取消登录。
   *  只有「本次会话刚 add 出来的空壳号」（fresh=1）还停在 pending 时才删掉它，
   *  别在账号条里留一个永远登不上的号。**重登/失效兜底一律不删**：那些号本来
   *  就登录过，重登时状态被清成 pending 只是中间态，删掉等于抹掉用户的真实
   *  账号（2026-11-18 用户报「重登后没反应，原来的号全没了」）。删不删以
   *  `L.fresh` 为准，**不再只看 state===pending**——pending 有「新空壳」和
   *  「重登中间态」两个来源，光看状态必误伤。后端 del_account?fresh=1 还会
   *  按账号履历再兜一层，双重保险。
   *  quiet=true：跟号走的自动收面板（号已恢复 / 已切走），不用打扰用户。 */
  async function cancelLogin(quiet) {
    const L = S.login;
    if (!L) return;
    if (L.auto) {   // 自动弹的：用户明确不要，本轮不再自动纠缠
      S.loginOffered = S.loginOffered || {};
      S.loginOffered[L.aid] = true;
    }
    const aid = L.aid;
    const mayDrop = !!L.fresh;   // 先记下来：endLogin 会把 L 置空
    endLogin();
    paintLogin();
    const a = ((S.d && S.d.accounts) || []).find((x) => x.id === aid);
    if (mayDrop && a && a.state === "pending") {
      try { await api(`/api/publish/accounts/${aid}?fresh=1`, { method: "DELETE" }); }
      catch (e) { /* 删不掉就留着，下次还能重登 */ }
      await load();
    } else if (!quiet) {
      toast("收起了；要用这个号时点「重登」");
    }
  }

  /** 候选拉取失败后问一句「是不是登录掉了」：掉了就弹登录窗口，别让人猜。
   *  hard=1 让后端对旧标签页做真导航验证（2026-11-14）：会话死了但残留页
   *  还停在后台页上时，普通探测会误报「已登录」，只有真导航一次才作数。 */
  async function probeLoginOnFail(aid) {
    if (S.login) return;                     // 已经在登录流程里就别叠一层
    if (!aid || aid !== currentAid()) return;
    try {
      const r = await apiT(`/api/publish/accounts/${aid}/login-state?hard=1`,
        {}, 50000);
      if (!r || r.logged_in !== false) return;   // 登录好好的，失败另有原因
      await openLogin(aid, { auto: true, label: accLabel(aid) });
    } catch (e) { /* 探测失败就保持原样，不打扰 */ }
  }


  function paintGlobalAcc() {
    const box = document.querySelector("#pub-global-acc");
    if (box && S.d) box.innerHTML = accountBar(S.d);
    syncOpenBtn();
  }

  /** 「进入视频号助手」用的是当前号的登录态：**只有确认已登录才可点**
   *  （2026-11-14 用户定）。没选号 / 待登录 / 登录过期一律置灰，
   *  悬停提示说清「现在差哪一步」。
   *  按钮在骨架里，状态跟着队列接口走，不在前端自己记一个 aid。 */
  function syncOpenBtn() {
    const b = document.querySelector("#btn-open-platform");
    if (!b) return;
    const a = (S.d && S.d.current_account) || null;
    const on = !!a && a.state === "online";
    b.disabled = !on || S.openBusy;
    b.textContent = S.openBusy ? "正在打开…" : "进入视频号助手 ↗";
    b.title = !a ? "先在「发布」页选一个号"
      : on ? "用当前号的登录态打开视频号助手"
      : a.state === "expired" ? "这个号登录过期了，先重新登录再打开"
      : "这个号还没登录，先登录再打开";
  }

  /** 没确认账号时三页统一的占位；账号管理（① 区）不受这个门槛。 */
  function needAccBox() {
    return `<div class="pub-need-acc"><b>先确认是哪个号</b>
      <p>发布、数据、诊断都是按号分开的。在上面账号条点一张账号卡片，
      选好之后这里就能用了。</p></div>`;
  }

  const ACC_STATE = {
    online:  ["已登录", "on"],
    pending: ["待登录", "wait"],
    expired: ["登录过期", "bad"],
    unknown: ["未登录", "mute"],
  };

  function accCard(a) {
    // 选中态只有一处：后端下发的当前号。以前这里用 OR 把「上次排期的号」
    // 也算成选中，结果两个号同时亮、点谁都切不动。
    const on = currentAid() === a.id;
    const st = ACC_STATE[a.state] || ACC_STATE.unknown;
    const av = a.avatar
      ? `<img src="${esc(a.avatar)}" alt="">`
      : `<i>${esc((a.wx_name || a.name || "号").slice(0, 1))}</i>`;
    // 三样东西都要露，且各占一行带标签：别名（自己起的备注名，没有就整行不显示）、
    // 视频号（平台昵称）、视频号 ID（sph 开头那串）。之前备注名当大字标题、下面把
    // 「视频号名 · ID」挤成一行，客户容易看混（2026-11-16 用户要求分行加标签）。
    const alias = (a.name || "").trim();
    const wx = (a.wx_name || "").trim();
    const fid = (a.finder_id || "").trim();
    const row = (label, val) => `<span class="frow"><i>${label}</i><em>${
      val ? esc(val) : "未读到"}</em></span>`;
    const rows = [
      alias ? row("别名", alias) : "",
      row("视频号", wx),
      row("视频号ID", fid),
    ].filter(Boolean).join("");
    return `<div class="pub-acc${on ? " on" : ""}" data-acc="${a.id}">
      <span class="av">${av}</span>
      <span class="who">${rows}</span>
      <span class="st ${st[1]}">${st[0]}</span>
      <span class="ops">
        <button class="mini" data-acc-relogin="${a.id}" title="换一个微信重新登录">重登</button>
        <button class="mini" data-acc-rename="${a.id}" title="改个好认的名字">改名</button>
        <button class="mini del" data-acc-del="${a.id}" title="删掉这个号（登录状态一起删）">删</button>
      </span>
    </div>`;
  }

  // ── ② 整批通用设置（对齐真实发布页：全是「选」，不是「填」）─────────────
  /** 当前发到哪个号：唯一来源是后端。后端已经把「锁号优先」算好了，
   *  前端再自己从 batch / DOM / localStorage 推一次，早晚和真实发布对不上。 */
  function currentAid() {
    return (S.d && S.d.current_account_id) || "";
  }
  function accLabel(aid) {
    const a = ((S.d && S.d.accounts) || []).find((x) => x.id === aid);
    return a ? (a.label || a.name || aid) : "已删除的号";
  }
  /** ② 里的一行小字：候选是从哪个号读回来的，别让人猜。
   *  等待态（号在传上一批）还没读到任何东西，这行先不摆，免得像已经有选项。 */
  /** ② 顶部那一行「现在在干什么」。第一次读要十几秒，光写「正在读」
   *  用户会以为卡死；按经过时间换三句人话，全程只改这一行字。 */
  function optStageText() {
    if (S.optState === "wait")
      return "这个号正在传上一批的视频，选项等传完会自动读；急的话点字段里的按钮现在读";
    if (S.optState === "fail") return "没读到选项，点字段里的按钮重试";
    if (S.optState !== "loading") return "";
    const sec = Math.floor((Date.now() - (S.optLoadingAt || Date.now())) / 1000);
    const lead = S.optFirstRead
      ? "第一次读约十几秒，之后不用等；别的字段现在就能填。"
      : "正在更新，旧的还能接着用。";
    if (sec < 6) return `${lead} 正在打开助手…`;
    if (sec < 14) return `${lead} 正在读剧集和标注…`;
    return `${lead} 快好了…`;
  }

  function tickOptStage() {
    const el = document.querySelector("#pub-opt-state");
    if (!el) {
      if (S.optStageTimer) clearInterval(S.optStageTimer);
      S.optStageTimer = 0;
      return;
    }
    el.textContent = optStageText();
    el.classList.toggle("bad", S.optState === "fail");
  }

  function startOptStage() {
    if (S.optStageTimer) clearInterval(S.optStageTimer);
    S.optStageTimer = setInterval(tickOptStage, 1500);
  }

  function stopOptStage() {
    if (S.optStageTimer) clearInterval(S.optStageTimer);
    S.optStageTimer = 0;
    tickOptStage();
  }

  function optsOwnerLine() {
    if (!S.optFor || S.optState === "wait") return "";
    const updating = !!(S.optRefreshAid && S.optRefreshAid === currentAid());
    const tail = updating ? " · 正在更新…"
      : S.optState === "loading" ? " · 正在读…" : "";
    const m = S.optReadAt ? Math.floor((Date.now() - S.optReadAt) / 60000) : 0;
    const fresh = S.optReadAt
      ? (m < 1 ? "刚刚读过" : m < 60 ? `${m} 分钟前读过`
        : `${Math.floor(m / 60)} 小时前读过`) : "";
    const staleTip = S.optStale
      ? ` <em class="pub-opt-note">可能不是最新，点「重新读一次」</em>` : "";
    return `<div class="pub-opts-from">这些选项来自 <b>${esc(accLabel(S.optFor))}</b>${tail}
      ${fresh ? `<span class="pub-opt-fresh">${esc(fresh)}</span>` : ""}
      <button class="ghost sm pub-reread" data-act="reloadOpts"
        ${updating || S.optState === "loading" ? "disabled" : ""}>${updating ? "正在更新…" : "重新读一次"}</button>
      ${S.optNote ? `<em class="pub-opt-note">${esc(S.optNote)}</em>` : ""}${staleTip}</div>`;
  }
  /** 切号只重画顶部账号条 + ② 的字段区，绝不整页 render：
   *  用户可能正在标题/话题里打字，整页重画会把半句话清掉。
   *  账号卡已并入顶部账号条，重画条就等于重画卡。 */
  function paintAccountBox() {
    paintGlobalAcc();
  }
  async function selectAccount(aid, quiet) {
    try {
      const r = await api("/api/publish/account/select", {
        method: "POST", body: JSON.stringify({ account_id: aid }),
      });
      if (r && r.queue) {
        // select 返回的是 _view()（不含 gate），整份替换会把门禁状态弄丢。
        S.d = { ...(S.d || {}), ...r.queue,
                gate: (S.d && S.d.gate) || r.queue.gate };
      }
      return r || {};
    } catch (e) {
      if (!quiet) throw e;
      return null;   // 扫码登录时锁号拒选不算错，load() 会显示真正的锁号
    }
  }

  /** 一个选择器字段：下拉 + 「从号里拉候选」。没登录 / 没权限时给一句人话。 */
  function picker(key, label, tplVal, hint, placeholder) {
    const o = S.opts[key] || null;
    const face = (txt, cls) =>
      `<button class="pub-sel${cls ? " " + cls : ""}" data-act="loadOpt" data-kind="${key}"
        ${S.optBusy[key] ? "disabled" : ""}>${esc(txt)}</button>`;
    // 位置必须**一开始就是搜索框**。平台要输入地名才给候选（后端返回
    // need_query），之前这里先给一个按钮、按钮又要求先有关键词，两边互相等，
    // 用户点一辈子也出不来候选。这一项也不可能自动预取，只能等人输词。
    if (key === "location" && (!o || o.need_query)) {
      const q = (S.optQ && S.optQ[key]) || "";
      return `<label data-pk="${key}"><span>${label}</span>
        <div class="pub-link">
          <input data-optq="${key}" value="${esc(q)}" placeholder="输入地点名，回车搜索">
          <button class="ghost sm" data-act="loadOpt" data-kind="${key}"
            ${S.optBusy[key] ? "disabled" : ""}>${S.optBusy[key] ? "搜着…" : "搜"}</button>
        </div>
        <em>${esc((o && o.note) || hint || "")}</em></label>`;
    }
    // 读候选时不再拿禁用按钮占住整格：给一个仍可操作的空下拉 + 一行小字，
    // 用户能继续填别处，也不会以为卡死。位置是「先输词再出候选」，保留原样。
    if (S.optBusy[key]) {
      if (key === "location") {
        return `<label data-pk="${key}"><span>${label}</span>${face("正在读…")}
          <em>${esc(hint || "")}</em></label>`;
      }
      return `<label data-pk="${key}"><span>${label}</span>
        <div class="pub-link">
          <select data-opt="${key}"><option value="">— 不设置 —</option></select>
          <span class="pub-reading"><i></i>正在从号里读…</span>
        </div>
        <em>${esc(hint || "")}</em></label>`;
    }
    if (!o) {
      return `<label data-pk="${key}"><span>${label}</span>${face(placeholder || "点这里从号里选")}
        <em>${esc(hint || "")}</em></label>`;
    }
    if (o.err) {
      return `<label data-pk="${key}"><span>${label}</span>${face(o.err, "bad")}
        <em>${esc(hint || "")}</em></label>`;
    }
    // v 为空的是「无需标注」这类取消项，下拉里已经有「— 不设置 —」，
    // 再放一条同值选项会出现两个 value="" 的条目，选中结果也对不上
    const opts = (o.items || []).filter((it) => it.v !== "");
    if (!opts.length) {
      // 号里真没有：把后端那句说明原样显示出来，别只给一个空下拉让人以为坏了
      return `<label data-pk="${key}"><span>${label}</span>${face("重试一次")}
        <em class="bad">${esc(o.note || "这个号里没有可选项")}</em></label>`;
    }
    const extra = key === "location"
      // 位置选过一次只是知道了一个 POI，想换就得能重搜；不给的话只能刷新页面
      ? ` <button class="mini" data-act="reOpt" data-kind="location">重搜</button>` : "";
    return `<label data-pk="${key}"><span>${label}${extra}</span>
      <select data-opt="${key}">
        <option value="">— 不设置 —</option>
        ${opts.map((it) => `<option value="${esc(it.v)}"
          ${tplVal && (it.v === tplVal || it.cn === tplVal) ? " selected" : ""}
          >${esc(it.cn)}</option>`).join("")}
      </select>
      <em>${esc(hint || "")}</em></label>`;
  }

  /** 扩展链接：先选类型，再选条目。单独一个是因为它比别的多一层类型下拉。
   *
   * 2026-11-13 修：这一块原来是手写的按钮，`S.opts.link` 从头到尾没人读 ——
   * 后端明明回了「这个号还没有剧集」，界面上永远回到「选条目」，看着就是一直转。
   */
  function linkField(t) {
    const o = S.opts.link || null;
    // 自动预取和单项重试都算「在读」，但都不许占住选择框：以前这里整个
    // 变成禁用按钮，看着像卡死；现在下拉一直在，旁边只挂一行小字。
    const busy = !!(S.optBusy.link || S.optState === "loading");
    let body;
    if (o && (o.items || []).length) {
      body = `<select data-opt="link">
        <option value="">— 不设置 —</option>
        ${o.items.map((it) => {
          // 剧集带「多少集」；不可选的（平台标了 usable=false）单独标出来，
          // 仍列在列表里 —— 让它消失更让人以为是程序坏了。
          const badge = it.count ? ` · ${it.count} 集` : "";
          const bad = it.usable === false
            ? `（不可选${it.why ? "：" + it.why : ""}）` : "";
          return `<option value="${esc(it.v)}"
            ${it.usable === false ? "disabled" : ""}
            ${t.link_title && (it.cn === t.link_title || it.v === t.link_id) ? " selected" : ""}
            >${esc(it.cn)}${esc(badge)}${esc(bad)}</option>`;
        }).join("")}
      </select>`;
    } else if (o && o.err) {
      body = `<button class="pub-sel bad" data-act="loadOpt" data-kind="link">${esc(o.err)}</button>`;
    } else if (o) {
      body = `<button class="pub-sel" data-act="loadOpt" data-kind="link">重试一次</button>`;
    } else if (busy) {
      // 第一次读、还没有任何缓存：也给一个能选的空下拉，别整格变成禁用按钮
      body = `<select data-opt="link"><option value="">— 不设置 —</option></select>`;
    } else {
      body = `<button class="pub-sel" data-act="loadOpt" data-kind="link">选条目</button>`;
    }
    const reading = busy
      ? `<span class="pub-reading"><i></i>正在从号里读…</span>` : "";
    const note = (!busy && o && !o.err && !(o.items || []).length) ? (o.note || "") : "";
    // 类型固定成「视频号剧集」：其它三种（文章/红包封面/小程序短剧）这轮不用，
    // 留一个类型下拉反而让人以为切过去就能用。字段值由 collectBatchFields 写死 series。
    const owner = (S.optFor && S.optState !== "wait")
      ? `<i class="pub-from">来自 ${esc(accLabel(S.optFor))}</i>` : "";
    return `<label class="span2"><span>扩展链接 · 视频号剧集${owner}</span>
      <div class="pub-link">${body}${reading}</div>
      <em class="${note ? "bad" : ""}">${esc(note || "选一个这个号已上架的剧集，可以不填")}</em>
    </label>`;
  }

  /** ② 封面图片：整批共用一张。选完立刻套到全批，不用再点「套用到这批视频」。
   *  图片是后端复制到应用数据目录的那一份，界面只拿预览地址，不碰用户原路径。 */
  function coverBox(t) {
    const has = !!(t.cover_file && t.cover_url);
    return `<div class="pub-cover span2">
      <span>封面图片</span>
      <div class="pub-cov-row">
        ${has ? `<img class="pub-cov-img" src="${esc(t.cover_url)}" alt="封面预览">`
              : `<div class="pub-cov-none">还没选图</div>`}
        <div class="pub-cov-ops">
          <div class="pub-cov-btns">
            <button class="ghost sm" data-act="pickCover">${has ? "换一张" : "选择图片"}</button>
            ${has ? `<button class="ghost sm" data-act="clearCover">清除</button>` : ""}
            ${has ? `<a class="pub-cov-view" href="${esc(t.cover_url)}" target="_blank"
               rel="noopener" title="到点发布时按这张图在视频号助手里上传封面">查看原图</a>` : ""}
          </div>
          <em>${has ? "这批视频都用这张封面。"
                    : "选一张，这批都用；不选就用视频里的一帧。"}</em>
        </div>
      </div>
    </div>`;
  }

  /** ② 的字段单独抽一块：预取完成后只重画这里。
   *  整页 render 会把用户正在改的标题 / 话题输入框一起重建，打到一半的字就没了。
   *  合集、活动本轮从界面移除（后端字段留着，老队列已有的值不丢）。 */
  function fieldsHTML(d) {
    const t = d.template || {};
    // 三种状态都说人话：在读 / 号在忙（不是坏了）/ 真没读到
    const hint = `<div class="pub-opt-state${S.optState === "fail" ? " bad" : ""}"
      id="pub-opt-state">${esc(optStageText())}</div>`;
    return `${hint}${optsOwnerLine()}
      ${coverBox(t)}
      ${picker("location", "位置", t.location, "输入地名后按回车，从结果里挑")}
      ${picker("declare", "视频标注", t.declare, "短剧一般选「内容为虚构剧情」")}
      ${linkField(t)}
      <label><span>话题</span><input data-bf="topics"
             value="${esc(topicsText(t.topics))}" placeholder="空格隔开，不用打 #"></label>
      <label><span>短标题</span><input data-bf="short_title" maxlength="16"
             value="${esc(t.short_title || "")}"
             placeholder="6-16 字，整批都用这句；留空让平台自己截"></label>
      <label class="chk"><input type="checkbox" data-bf="original"${t.original ? " checked" : ""}>
        <span>声明原创<em>号没开通原创权限时勾了会被驳回</em></span></label>`;
  }

  function refreshFields() {
    const box = document.querySelector("#pub-fields");
    if (!box || !S.d) return;
    box.innerHTML = fieldsHTML(S.d);
  }

  /** 单项读取候选回来时重画 ②，但把用户正在打的字原样还回去。
   *  整个 #pub-fields 都要重画（下拉要换成新候选），直接 innerHTML 会把
   *  话题/地点里打了一半的字清掉；只延后重画又可能永远等不到焦点离开。
   *  程序赋值不触发 input/change，不会误触「改动自动套用」。 */
  function refreshFieldsPreserve() {
    const box = document.querySelector("#pub-fields");
    if (!box || !S.d) { refreshFields(); return; }
    const keyOf = (n) => n.dataset.bf || n.dataset.optq || n.dataset.opt || "";
    const saved = [];
    box.querySelectorAll("input, textarea, select").forEach((n) => {
      if (!keyOf(n)) return;
      saved.push({ k: keyOf(n), tag: n.tagName, type: n.type || "",
                   value: n.value, checked: !!n.checked,
                   focused: n === document.activeElement,
                   caret: n.selectionStart == null ? null : n.selectionStart });
    });
    refreshFields();
    const box2 = document.querySelector("#pub-fields");
    if (!box2) return;
    const nodes = Array.from(box2.querySelectorAll("input, textarea, select"));
    saved.forEach((s) => {
      const n = nodes.find((x) => keyOf(x) === s.k && x.tagName === s.tag);
      if (!n) return;
      if (s.type === "checkbox") n.checked = s.checked;
      else n.value = s.value;
      if (s.focused) {
        try {
          n.focus();
          if (s.caret != null && n.setSelectionRange) n.setSelectionRange(s.caret, s.caret);
        } catch (e) { /* 光标还原失败不影响值 */ }
      }
    });
  }

  // ── ② 的自动套用（2026-11-13 用户要求：去掉「套用到这批视频」按钮）───────
  // 改完即生效：下拉/勾选 change 直接发；话题打字 input 防抖 600ms，
  // 失焦（change）时再发一次兜底。失败只写进提示行，不弹 toast 打断填写。
  let applyTimer = 0;
  let applyBusy = null;

  /** 收集第 ② 块的值，组成 /api/publish/apply 的 payload。
   *  位置/标注/链接只有下拉真的渲染出来才提交：选项没读到时下拉不存在，
   *  这时候交空串会把模板里配好的值悄悄清掉（原按钮版就有的隐患——
   *  自动套用后每次输入都会触发，不能再带这个雷）。 */
  function collectBatchFields() {
    const fields = {};
    document.querySelectorAll("[data-bf]").forEach((n) => {
      const k = n.dataset.bf;
      if (n.type === "checkbox") fields[k] = n.checked;
      else if (k === "topics") fields.topics = parseTopics(n.value);
      else fields[k] = n.value;
    });
    // 界面已固定成「视频号剧集」，类型下拉没了，这个值由前端写死
    fields.link_kind = "series";
    // 返回值三种：undefined = 下拉没渲染/条目对不上，这项不动模板；
    // null = 明确选了「— 不设置 —」，清掉；对象 = 选中项
    const pick = (key) => {
      const sel = document.querySelector(`[data-opt="${key}"]`);
      if (!sel) return undefined;
      if (!sel.value) return null;
      const o = S.opts[key] || {};
      return (o.items || []).find((it) => it.v === sel.value) || undefined;
    };
    const map = {
      location: ["location_poi", "location_city"],
      declare: ["declare"],
      link: ["link_id", "link_title"],
    };
    Object.keys(map).forEach((k) => {
      const it = pick(k);
      if (it === undefined) return;
      if (!it) { fields[k] = ""; return; }
      if (k === "declare") { fields.declare = it.v || ""; return; }
      fields[k] = it.cn;
      if (k === "location") {
        fields.location_poi = it.v;
        fields.location_city = it.city || "";
      }
      if (k === "link") {
        fields.link_id = it.v;
        fields.link_title = it.cn;
        fields.link = it.url || "";
      }
    });
    return fields;
  }

  /** 打开着的「文案」面板同步成套用后的值：面板不刷新的话里面还是旧值，
   *  用户再点「保存」会把刚套用的整批设置又改回去（2026-11-14 用户实测踩到）。 */
  function syncFedit(j) {
    const box = document.querySelector(`[data-fedit="${j.id}"]`);
    if (!box) return;
    box.querySelectorAll("[data-jf]").forEach((n) => {
      const k = n.dataset.jf;
      if (k === "topics") n.value = topicsText(j.topics);
      else if (k === "cover_ts") n.value = Number(j.cover_ts || 0);
      else if (n.type === "checkbox") n.checked = !!j[k];
      else n.value = j[k] != null ? j[k] : "";
    });
  }

  function doApply() {
    const hint = document.querySelector("#pub-apply-hint");
    return api("/api/publish/apply",
      { method: "POST", body: JSON.stringify({ fields: collectBatchFields() }) })
      .then((r) => {
        if (hint) hint.textContent = r.n
          ? `已自动套用到 ${r.n} 条（只改还没开始发的）`
          : "这批都开始发了，没有可套用的";
        // 打开的「文案」面板跟着套用结果走，面板里的值永远 = 已保存的值
        const fresh = ((r.queue || {}).jobs || []).find((x) => x.id === S.edit);
        if (S.edit && fresh) syncFedit(fresh);
        return r;
      })
      .catch((err) => {
        if (hint) hint.textContent = "套用失败：" + err.message;
      });
  }

  function autoApply(delay) {
    clearTimeout(applyTimer);
    applyTimer = setTimeout(() => { applyBusy = doApply(); }, delay);
  }

  /** 把还没发出去的套用请求等完。「排到」前必须调：套用没落库就排期，
   *  最后一次改动会被锁在门外（排上的条目 apply 不再碰）。 */
  async function flushApply() {
    clearTimeout(applyTimer);
    if (applyBusy) { await applyBusy; applyBusy = null; }
  }

  function batchBox(d) {
    return `<div class="pub-fields" id="pub-fields">${fieldsHTML(d)}</div>
    <div class="pub-acts">
      <span class="hint" id="pub-apply-hint">改动自动套用到这批还没发的视频</span>
    </div>`;
  }

  function row(j, i) {
    const st = STATUS[j.status] || ["未知", "mute"];
    const due = isDue(j);
    // 已交平台的条目：计划时间过了 = 平台该发/已经发了（程序在点发表那一步就
    // 结束了，不会再回报）。状态如实叫「已提交」，去不去平台看由用户自己定
    // （2026-11-14 用户反馈：「时间已过·去平台看」这种提示不像给用户看的话）。
    const past = j.status === "scheduled" && j.plan_at && j.plan_at <= toLocal(new Date());
    // 跟后端 LOCKED_STATUS 对齐（scheduled / published / publishing / uncertain）：
    // publishing / uncertain 也算，否则界面上给了「文案」按钮，点进去后端一律拒。
    const locked = ["scheduled", "published", "publishing", "uncertain"].includes(j.status);
    const open = S.edit === j.id;
    const tags = topicsText(j.topics);
    const from = j.project ? ` · 来自「${esc(j.project)}」` : "";
    const ep = (j.src || "").startsWith("set:") ? j.src.slice(4) : `#${i + 1}`;
    return `<div class="pub-row st-${st[1]}${due ? " due" : ""}" data-job="${esc(j.id)}">
      <div class="pub-rhead">
        <span class="pub-ep">${esc(ep)}</span>
        <div class="pub-st ${st[1]}">${due ? "马上发"
          : past ? "到点了，正在上传"
          : (j.status === "publishing" && j.step) ? `正在上传 · ${esc(j.step)}` : st[0]}</div>
      </div>
      <div class="pub-main">
        <input class="pub-title" data-title="${esc(j.id)}" value="${esc(j.title)}"
               maxlength="100" ${locked ? "disabled" : ""} title="点一下就能改标题">
        <div class="pub-meta">
          <span>${esc(showTime(j.plan_at))} · 发到 ${esc(j.account_name || (j.status === "draft" ? "开始上传时确定" : "未选号"))} · ${esc(mb(j.size))}
          ${j.cover_url ? `<img class="pub-cov-dot" src="${esc(j.cover_url)}"
             alt="" title="这条已指定封面图片">` : ""}
          ${j.file_exists === false ? `<span class="bad">文件找不到了</span>` : ""}</span>
          ${(tags || from) ? `<span class="pub-meta2">${tags ? esc(tags) : ""}${from}</span>` : ""}
        </div>
      </div>
      <div class="pub-ops">
        ${locked ? "" : `<button class="ghost sm" data-act="editf">文案</button>`}
        ${j.status === "queued" ? `
          <button class="ghost sm" data-act="mark" data-to="draft">不上传这条</button>` : ""}
        ${j.status === "failed" ? `
          <button class="ghost sm" data-act="mark" data-to="draft"
                  title="这条没传上去，先收回「还没安排」，以后想发随时能再排">这条不发了</button>` : ""}
        ${["draft", "failed", "manual"].includes(j.status)
          ? `<button class="ghost sm" data-act="del" title="从队列里删掉这条记录">移除</button>` : ""}
        ${j.status === "failed"
          ? `<button class="primary sm" data-act="retry" data-jid="${esc(j.id)}">重试</button>
             <button class="ghost sm" data-act="relogin">重新登录</button>` : ""}
        ${j.status === "uncertain" ? `
          <button class="primary sm" data-act="resolve" data-to="published">后台已有这条</button>
          <button class="ghost sm" data-act="resolve" data-to="cancelled">核实没发出去，结束这条</button>` : ""}
      </div>
      ${open && !locked ? fedit(j) : ""}
    </div>`;
  }

  function fedit(j) {
    // 设了封面图就别再让人填取帧秒数：两个值同时摆着，改了没效果最容易被当成 bug。
    const hasCover = !!(j.cover_file_eff && j.cover_url);
    const thumb = j.cover_url
      ? `<img class="pub-cov-img sm" src="${esc(j.cover_url)}" alt="封面预览">`
      : `<div class="pub-cov-none">${j.cover_override ? "单独指定的图不在了" : "跟随全批封面"}</div>`;
    return `<div class="pub-fedit" data-fedit="${esc(j.id)}">
      <label><span>短标题</span><input data-jf="short_title" maxlength="16"
             value="${esc(j.short_title || "")}" placeholder="6-16 字，留空让平台自己截"></label>
      <label><span>封面取第几秒</span><input data-jf="cover_ts" type="number" min="0" step="0.5"
             value="${Number(j.cover_ts || 0)}" placeholder="0 = 第一帧"
             ${hasCover ? "disabled" : ""}></label>
      ${hasCover ? `<em class="pub-cov-note">已用封面图片，这个秒数不生效</em>` : ""}
      <div class="pub-cover wide">
        <span>封面图片</span>
        <div class="pub-cov-row">
          ${thumb}
          <div class="pub-cov-ops">
            <div class="pub-cov-btns">
              <button class="ghost sm" data-act="jobCover">${j.cover_override ? "换一张" : "单独换一张"}</button>
              ${j.cover_override ? `<button class="ghost sm" data-act="jobCoverClear">用回全批封面</button>` : ""}
            </div>
            <em>${j.cover_override ? "这条单独指定了封面图。"
                                  : "这条跟随 ② 的全批封面；这里换图只影响这一条。"}</em>
          </div>
        </div>
      </div>
      <label class="wide"><span>话题</span><input data-jf="topics"
             value="${esc(topicsText(j.topics))}" placeholder="用空格隔开，不用打 #"></label>
      <div class="pub-acts">
        <button class="primary sm" data-act="savef">保存</button>
        <button class="ghost sm" data-act="closef">收起</button>
      </div>
    </div>`;
  }

  // ── 排期预览 ──────────────────────────────────────────────────────────
  function maybePlan() {
    clearTimeout(S.planTimer);
    S.planTimer = setTimeout(doPlan, 350);
  }

  /** 右侧预览里的警告也摆一份到「开始上传」旁边。doPlan 是局部刷新，
   *  不整页重画（用户在打字），所以这块自己更新，不等下一次 render。 */
  function paintStartWarn(r) {
    const box = document.querySelector("#pub-start-warn");
    if (!box) return;
    const msg = (((r && r.warns) || [])[0] || {}).msg || "";
    box.style.display = msg ? "" : "none";
    box.textContent = msg.length > 88 ? msg.slice(0, 88) + "…" : msg;
    box.title = msg;
  }

  async function doPlan() {
    const box = document.querySelector("#pub-plan");
    const tbox = document.querySelector("#pub-times");
    if (!box || !S.d) return;
    const drafts = ((S.d.jobs) || []).filter((j) => j.status === "draft").length;
    if (!drafts) {
      // 空队列时下面已经有一句「队列是空的」，这里别再重复一遍
      box.innerHTML = (S.d.jobs || []).length
        ? `<div class="hint">没有还没安排的条目了。</div>` : "";
      if (tbox) tbox.innerHTML = "";
      paintStartWarn(null);
      S.plan = null;
      S.planFor = null;
      S.planSeq++;          // 作废还在路上的那次预览
      return;
    }
    const wantFirst = S.first;
    const wantGap = Number(S.gap) || 30;
    const seq = ++S.planSeq;
    try {
      const r = await api("/api/publish/plan", {
        method: "POST",
        body: JSON.stringify({ first_at: wantFirst, interval_min: wantGap }),
      });
      // 连点时间会同时挂好几个请求，谁后返回谁覆盖会让摘要闪回旧值：只认最后一次
      if (seq !== S.planSeq) return;
      S.plan = r;
      S.planFor = { first: wantFirst, gap: wantGap, n: drafts };
      paintStartWarn(r);
      if (!r.n) { box.innerHTML = `<div class="hint">队列里还没有视频，先在左边勾几条。</div>`; return; }
      const errs = (r.errors || []).map((e) => `<div class="pub-alarm bad">${esc(e.msg)}</div>`).join("");
      const wrns = (r.warns || []).map((w) => `<div class="pub-alarm warn">${esc(w.msg)}</div>`).join("");
      const eta = r.eta_min ? ` · 全部传完约 ${r.eta_min} 分钟` : "";
      box.innerHTML = `
        <div class="pub-sum">共 <b>${r.n}</b> 条：首条 ${esc(showTime(r.first))}
          → 最后一条 ${esc(showTime(r.last))}${eta}</div>
        ${errs}${wrns}`;
      // ③ 区下面把前几条的实际时间摊开，用户不用心算「5 点 + 5 分钟 × n」
      if (tbox) {
        const all = r.times || [];
        const show = all.slice(0, 6);
        tbox.innerHTML = show.map((x, i) => `<span class="pub-t${i === 0 ? " first" : ""}">
          <em>${i === 0 ? "第 1 条" : `第 ${i + 1} 条`}</em>${esc(showTime(x))}</span>`).join("")
          + (all.length > show.length
            ? `<span class="pub-t more">还有 ${all.length - show.length} 条</span>` : "");
      }
    } catch (e) {
      if (seq !== S.planSeq) return;
      box.innerHTML = `<div class="pub-alarm bad">${esc(e.message)}</div>`;
      if (tbox) tbox.innerHTML = "";
      paintStartWarn(null);
    }
  }

  // ── 操作 ──────────────────────────────────────────────────────────────
  function resetCand() {
    S.cand = [];
    S.sel.clear();
    S.picker = null;
    S.pid = "";
    S.pname = "";
  }

  async function act(name, el) {
    const jid = el && el.closest("[data-job]") && el.closest("[data-job]").dataset.job;
    try {
      if (name === "pickdir") {
        toast("正在打开文件夹选择框…");
        const r = await api("/api/publish/pick-folder", { method: "POST" });
        if (!r.path) return;
        S.picker = "dir";
        S.pname = r.path;
        S.pid = "";
        S.cand = (r.files || []).map((f) => ({ path: f.path, name: f.name, size: f.size }));
        // 整个文件夹默认全选：用户的意图就是「这些都要发」，再让他一条条勾一遍很烦
        S.sel = new Set(S.cand.map((_c, i) => i));
        if (!S.cand.length) toast("这个文件夹里没找到视频（支持 mp4/mov 等常见格式）");
        paintCand();
        return;
      }
      if (name === "recentdir") {
        // 「最近用过」点一下：直接读这个目录，等于选文件夹但不弹框
        const path = el.dataset.path || "";
        const r = await api(`/api/publish/dir-files?path=${encodeURIComponent(path)}`);
        S.picker = "dir";
        S.pname = r.path;
        S.pid = "";
        S.cand = (r.files || []).map((f) => ({ path: f.path, name: f.name, size: f.size }));
        S.sel = new Set(S.cand.map((_c, i) => i));
        if (!S.cand.length) toast("这个文件夹里没找到视频了");
        paintCand();
        return;
      }
      if (name === "pickfiles") {
        const r = await api("/api/publish/pick-files", { method: "POST" });
        if (!r.paths || !r.paths.length) return;
        S.picker = "files";
        S.pname = "";
        S.pid = "";
        S.cand = r.paths.map((p) => ({
          path: p, name: p.split(/[\\/]/).pop(), size: 0,
        }));
        S.sel = new Set(S.cand.map((_c, i) => i));
        paintCand();
        return;
      }
      if (name === "pickproj") {
        const r = await api("/api/publish/projects");
        S.opts.projs = r.projects || [];
        S.picker = "proj";
        S.cand = [];
        S.sel.clear();
        S.pid = "";
        paintCand();
        return;
      }
      if (name === "backCand") { resetCand(); paintCand(); return; }
      if (name === "clearCand") { resetCand(); paintCand(); return; }
      if (name === "add") {
        // 连点直接吞掉：上一次还没回来就再发一遍，后端会各自读旧队列再写回，
        // 轻则重复条目、重则后写覆盖先写（2026-11-21 实测被连点 12 次）。
        if (S.addBusy) return;
        const body = { files: [], sets: [], pid: S.pid, dir_path: "" };
        if (S.picker === "proj") {
          body.sets = S.cand.filter((_c, i) => S.sel.has(i))
            .map((c) => Number(c.index)).filter((n) => !Number.isNaN(n));
        } else {
          body.files = S.cand.filter((_c, i) => S.sel.has(i)).map((c) => c.path);
        }
        const n = body.sets.length || body.files.length;
        if (!n) { toast("先勾几条"); return; }
        // 入队本身已经只做 stat 级核对（毫秒级），这里主要是让「点下去有反应」：
        // 按钮立刻禁用并显示条数，回来之前不可能再发一次。
        S.addBusy = true; S.addN = n;
        paintCand();
        try {
          const r = await api("/api/publish/add", { method: "POST", body: JSON.stringify(body) });
          const dup = (r.dup || []).length;
          const repost = r.repost || [];
          const byFile = repost.filter((x) => x.kind === "file").length;
          const byContent = repost.filter((x) => x.kind === "content").length;
          toast(`加了 ${(r.added || []).length} 条` + (dup ? `，${dup} 条已经在队列里` : "")
            + (byFile ? `；${byFile} 条以前发过` : "")
            + (byContent ? `；${byContent} 条内容以前发过（换了文件名也认得出）` : ""));
          resetCand();
        } finally {
          S.addBusy = false; S.addN = 0;
        }
        await load();
        return;
      }
      if (name === "addacc") {
        // 开新会话前把旧的（连轮询）收掉，保证任何时刻只有一个登录窗口
        endLogin();
        toast("正在打开视频号助手，稍等一下…");
        const r = await api("/api/publish/accounts", { method: "POST", body: JSON.stringify({}) });
        // 新加的号还没有名字，title 就不猜；后端刚才已经把它弹出来了。
        // fresh=1：这是唯一一个「取消时可删空壳」的入口（2026-11-18）。
        await openLogin(r.account.id, { fresh: true });
        return;
      }
      if (name === "loginRetry") { await reopenLogin(); return; }
      if (name === "loginCancel") { await cancelLogin(); return; }
      if (name === "reloadOpts") {
        const aid = currentAid();
        if (!aid) { toast("先在①里选一个号"); return; }
        const acc = ((S.d && S.d.accounts) || []).find((x) => x.id === aid) || {};
        if (acc.state !== "online") { toast("这个号的登录态不在了，先点「重登」登录一下"); return; }
        if (accBusy(aid)) { toast("这个号正在传上一批，等传完再点「重新读一次」"); return; }
        // 强制真读一遍并写回新缓存；界面上旧选项一直留着，不挡填写
        ensureOpts(true, true);
        return;
      }
      if (name === "loadOpt") {
        const kind = el.dataset.kind;
        const aid = currentAid();
        if (!aid) { toast("先在①里选一个号，候选要从这个号里拉"); return; }
        const acc = ((S.d && S.d.accounts) || []).find((x) => x.id === aid) || {};
        if (acc.state !== "online") { toast("这个号的登录态不在了，先点「重登」登录一下"); return; }
        const box = document.querySelector(`[data-optq="${kind}"]`);
        const q = ((box && box.value) || (S.optQ && S.optQ[kind]) || "").trim();
        if (kind === "location" && !q) { toast("先输入地点名，比如「石牌」"); return; }
        S.optQ = S.optQ || {};
        S.optQ[kind] = q;
        const seq = S.optSeq;        // 中途切号，上一个号的旧候选不许写回来
        S.optBusy[kind] = true;      // 这一步要 8-15 秒，必须让界面看得出在读
        // 只改按钮那一行字：整页 render() 会把用户正在标题/话题里打的半句清掉
        const busyBtn = document.querySelector(`[data-act="loadOpt"][data-kind="${kind}"]`);
        if (busyBtn) busyBtn.textContent = kind === "location" ? "搜着…" : "正在读…";
        let got = 0, err = "";
        try {
          // 类型固定成视频号剧集（界面上已经没有类型下拉了）
          const r = await apiT(`/api/publish/options?aid=${encodeURIComponent(aid)}`
            + `&kind=${encodeURIComponent(kind)}&q=${encodeURIComponent(q)}`
            + (kind === "link" ? "&link_kind=series" : ""));
          if (seq === S.optSeq) {
            S.opts[kind] = r;
            got = (r && (r.items || []).length) || 0;
            err = (r && r.err) || "";
            // 后端认出登录墙会带 login=True：直接去对一次登录态，掉了就地摆码
            if (r && r.login) probeLoginOnFail(aid);
          }
        } catch (e) {
          if (seq === S.optSeq) { S.opts[kind] = { err: e.message }; err = e.message; }
        }
        delete S.optBusy[kind];
        refreshFieldsPreserve();
        if (seq === S.optSeq) {
          toast(err ? err : got ? `读到 ${got} 个选项` : "这次没读到选项，点一下重试");
        }
        return;
      }
      if (name === "reOpt") {
        // 位置选过之后想换一个：把它打回「先搜再选」状态，别逼着人刷新页面
        const kind = el.dataset.kind;
        S.opts[kind] = { items: [], need_query: true, note: "换个词再搜一次。" };
        render();
        return;
      }
      if (name === "pickCover" || name === "jobCover") {
        toast("正在打开图片选择框…");
        const r = await api("/api/publish/pick-cover", { method: "POST" });
        if (!r.ok) return;                       // 用户取消了选择框
        if (name === "jobCover") {
          await api(`/api/publish/job/${jid}`, {
            method: "PUT", body: JSON.stringify({ cover_file: r.cover_file }),
          });
          toast("这条换成自己的封面了");
        } else {
          // 整批默认：只写 template，不改任何一条的单条覆盖值
          await api("/api/publish/apply", {
            method: "POST", body: JSON.stringify({ fields: { cover_file: r.cover_file } }),
          });
          toast("这批视频都用这张封面了");
        }
        await load();
        return;
      }
      if (name === "clearCover") {
        await api("/api/publish/apply", {
          method: "POST", body: JSON.stringify({ fields: { cover_file: "" } }),
        });
        toast("封面图清掉了，回到按视频帧");
        await load();
        return;
      }
      if (name === "jobCoverClear") {
        await api(`/api/publish/job/${jid}`, {
          method: "PUT", body: JSON.stringify({ cover_file: "" }),
        });
        toast("这条用回全批封面了");
        await load();
        return;
      }
      if (name === "editf") {
        // 只动右栏：以前走 render() 会把左边 ②「通用设置」整块重建，
        // 正在打的话题/标题、下拉展开态、焦点全丢（2026-11-21 用户反馈）。
        S.edit = S.edit === jid ? null : jid;
        paintRight();
        return;
      }
      if (name === "closef") { S.edit = null; paintRight(); return; }
      if (name === "savef") {
        const box = el.closest("[data-fedit]");
        const body = {};
        box.querySelectorAll("[data-jf]").forEach((n) => {
          const k = n.dataset.jf;
          if (k === "topics") body.topics = parseTopics(n.value);
          else if (k === "cover_ts") body.cover_ts = Number(n.value) || 0;
          else body[k] = n.value;
        });
        await api(`/api/publish/job/${box.dataset.fedit}`,
          { method: "PUT", body: JSON.stringify(body) });
        toast("改好了");
        S.edit = null;
        await load();
        return;
      }
      if (name === "start") {
        const acc = currentAid();
        if (!acc) { toast("先在上面账号条点一张卡片，选一个要发到的号"); return; }
        if (S.plan && !S.plan.ok) { toast(S.plan.errors[0].msg); return; }
        const a = (S.d && S.d.current_account) || {};
        const n = ((S.d && S.d.jobs) || []).filter((j) => j.status === "draft").length;
        if (!n) { toast("队列里没有「还没安排」的视频，先在上面勾几条"); return; }
        await flushApply();   // 最后一次改动也要带上：没落库就排期，会被锁在门外
        // 确认指纹必须跟「现在这一屏」一致：改完时间立刻点提交时，防抖的
        // doPlan 可能还没跑，先强制刷一次预览再拿 intent。
        // 反过来，预览就是按眼前这组「首条/间隔/条数」算的，直接复用——
        // 以前无条件重算，等于点一下「开始上传」再白等一遍（2026-11-21）。
        const pf = S.planFor;
        const wantGap = Number(S.gap) || 30;
        if (!(pf && S.plan && pf.first === S.first && pf.gap === wantGap && pf.n === n)) {
          await doPlan();
        }
        if (S.plan && S.plan.ok === false) {
          toast(((S.plan.errors || [])[0] || {}).msg || "这批还排不了，先看上面的提示");
          return;
        }
        const who = a.label || a.name || acc;
        // 第一条贴得太近而条数又多时，逐条上传多半赶不上排的时间——平台会拒
        // 或直接失败。提前一句话说清，比事后一条条标失败强（2026-11-14）。
        const minsAhead = (new Date(S.first).getTime() - Date.now()) / 60000;
        const warn = (n >= 2 && minsAhead < 15)
          ? `\n⚠ 第一条离现在只有 ${Math.max(0, Math.round(minsAhead))} 分钟，`
            + `${n} 条逐条上传多半来不及，建议把第一条往后调。\n` : "";
        // 不可逆之前把「发到哪个号」写全：备注名 + 视频号名 + ID，
        // 不是一句「确定吗」能替代的（2026-11-13 用户要求）。
        const rep = (S.plan && S.plan.repost) || [];
        const body = [
          `发给：【${who}】 · ${n} 条`,
          `首条 ${showTime(S.first)} · 间隔 ${Number(S.gap) || 30} 分钟`,
        ];
        if (rep.length) {
          const byContent = rep.filter((x) => x.kind === "content").length;
          const sameFile = rep.length - byContent;
          const lines = [];
          if (sameFile) lines.push(`${sameFile} 条以前发过或交过平台`);
          if (byContent) lines.push(`${byContent} 条内容跟其他条或已发过的重复（换了文件名也认得出，可能是整条也可能是其中一段）`);
          body.push(`其中 ${lines.join("，")}：${rep.slice(0, 3).map((x) => x.file + (x.overlap ? `（重合 ${Math.round(x.overlap)} 秒）` : "")).join("、")}${rep.length > 3 ? " 等" : ""}。`,
            "确认是有意再发一次；不确定就点「再看看」，先去「已提交」里核对。");
        }
        if (warn) body.push(warn.replace(/^\n+/, "").trim());
        body.push(
          "确认后立刻逐条上传，并按上面的时间在平台定时；发出去就收不回来了。",
          "提交后队列清空、设置恢复默认，这批到「已提交」里跟踪；切号随时可以，还在排队的条目可以在「已提交」里批量取消。",
        );
        const ok = await askConfirm({
          title: `确认这 ${n} 条发给「${who}」？`,
          body,
          okText: "提交并开始上传",
          cancelText: "再看看",
          danger: true,
        });
        if (!ok) return;
        const r = await api("/api/publish/start", {
          method: "POST",
          body: JSON.stringify({ first_at: S.first, interval_min: Number(S.gap) || 30,
                                 account_id: acc, intent: (S.plan && S.plan.intent) || "" }),
        });
        // 提交成功：这批就成了后台任务——队列清空、设置恢复默认，下一批从头填；
        // 进度到「已提交」里跟踪（2026-11-14 用户定稿）
        S.first = defFirst();
        S.gap = 30;
        S.hist = true;
        S.cand = [];
        S.sel.clear();
        toast(r.monitor_started
          ? `已提交 ${n} 条，播放监控已自动开启——之后到「数据」页看涨幅`
          : `已提交 ${n} 条到【${a.name || who}】，正在逐条上传`);
        await load();
        return;
      }
      if (name === "retry") {
        const r = await api(`/api/publish/job/${el.dataset.jid}/retry`, { method: "POST" });
        // 原计划时间已过时后端会顺延并回传 moved_to；不说出来，用户会以为
        // 还按原来那个点发（平台也不接受过去的定时）。
        toast(r && r.moved_to
          ? `已经接着上传：原定时间已经过了，改到 ${showTime(r.moved_to)} 发`
          : "已加入等待上传，接着传");
        await load();
        return;
      }
      if (name === "retryAll") {
        // 逐条调 retry 接口；start() 会自动把上传线程喊起来，不用单独管。
        // 只重试**当前号**的失败条目（2026-11-14：已提交列表按号隔离，操作
        // 范围跟显示范围一致，别把别的号的失败条目一起带上）。
        const cur = currentAid();
        const ids = ((S.d && S.d.jobs) || [])
          .filter((j) => j.status === "failed" && (!cur || j.account_id === cur))
          .map((j) => j.id);
        let moved = 0;
        for (const id of ids) {
          const r = await api(`/api/publish/job/${id}/retry`, { method: "POST" })
            .catch(() => null);
          if (r && r.moved_to) moved++;
        }
        toast(ids.length
          ? `${ids.length} 条已加入等待上传，接着传`
            + (moved ? `（其中 ${moved} 条原定时间已过，已往后顺延）` : "")
          : "这个号没有失败的条目");
        await load();
        return;
      }
      if (name === "stop") {
        // 只取消当前号还在排队的（后端同样按号过滤，跨号误伤防在两边）
        const who = accLabel(currentAid());
        const n = ((S.d && S.d.jobs) || []).filter((j) =>
          j.status === "queued" && j.account_id === currentAid()).length;
        if (!confirm(`把【${who}】还在排队、没上传的 ${n} 条取消掉？\n`
          + "它们会退回队列重新设置；已经传到平台并定了时间的，程序取消不了"
          + "（要去视频号助手「待发布」里手动删），已经发出去的也不受影响。继续吗？")) return;
        const r = await api("/api/publish/stop", { method: "POST",
          body: JSON.stringify({ account_id: currentAid() || "" }) });
        toast(r.on_platform
          ? `取消 ${r.reverted} 条；这个号另有 ${r.on_platform} 条已交平台，取消不了`
          : `取消 ${r.reverted} 条`);
        await load();
        return;
      }
      if (name === "clear") {
        const ok = await askConfirm({
          title: "删掉还没安排的条目？",
          body: [
            "只删「还没安排」的；已定时、已发布的都不动。",
            "删了以后想发，要重新勾一次。",
          ],
          okText: "删掉这些条目",
          cancelText: "先留着",
          danger: true,
        });
        if (!ok) return;
        const r = await api("/api/publish/clear-draft", { method: "POST" });
        toast(`删掉了 ${r.removed} 条`);
        await load();
        return;
      }
      if (name === "qtab") {
        S.hist = el.dataset.hist === "1";
        paintRight(true);
        return;
      }
      if (name === "qfilter") {
        S.qfilter = el.dataset.f || "all";
        paintRight(true);
        return;
      }
      if (name === "del") {
        // 这里的「移除」只面向还没交给平台的条目（draft/failed/manual）；
        // 已交平台的行不再摆这个按钮（2026-11-14 用户反馈：那是噪音）。
        if (!confirm("从队列里删掉这条记录？平台那边不受影响。")) return;
        await api(`/api/publish/job/${jid}`, { method: "DELETE" });
        await load();
        return;
      }
      if (name === "relogin") {
        const aid = currentAid();
        await openLogin(aid, { label: accLabel(aid) });
        return;
      }
      if (name === "resolve") {
        // 点过「发表」但没核对上：只能由人去平台看过之后收尾，程序不猜
        const ok = el.dataset.to === "published";
        const word = ok
          ? "在视频号助手「待发布」里确认这条已经在了？\n点「确定」后记成「已发布」，不会再重传。"
          : "在视频号助手里确认这条确实没发出去？\n点「确定」后结束这条，不会再重传。";
        if (!confirm(word)) return;
        await api(`/api/publish/job/${jid}/mark`, {
          method: "POST",
          body: JSON.stringify({ action: ok ? "resolved_published" : "resolved_cancelled", note: "" }),
        });
        toast(ok ? "记成「已发布」" : "这条结束了");
        await load();
        return;
      }
      if (name === "mark") {
        // 界面上只剩「不上传这条」这一种用法：queued → 还没安排。
        // 以前「已定时」那行还有个「记成已取消」（状态 skipped），2026-11-14
        // 用户要求去掉，改成「移除」+ 确认框讲清平台照样会发。
        // 后端 mark(skipped) 保留，供老队列和脚本兼容，前端不再调用。
        if (!confirm("这条不上传了？会退回「还没安排」，以后想发点「开始上传」再带它一次。")) return;
        await api(`/api/publish/job/${jid}/mark`, {
          method: "POST", body: JSON.stringify({ action: "draft", note: "" }),
        });
        toast("已退回「还没安排」");
        await load();
        return;
      }
    } catch (e) {
      toast(e.message);
    }
  }

  async function refreshBadge() {
    const b = document.querySelector("#pub-badge");
    if (!b) return;
    try {
      const d = await api("/api/publish/overview");
      const n = d.pending || 0;
      b.textContent = n > 99 ? "99+" : String(n);
      b.classList.toggle("hidden", n === 0);
      b.classList.toggle("due", (d.due || []).length > 0);
    } catch (e) { /* 未登录/断网时静默 */ }
  }

  // ── 事件绑定 ──────────────────────────────────────────────────────────
  function bind() {
    const nav = document.querySelector("#appnav");
    if (nav) {
      nav.addEventListener("click", (e) => {
        const b = e.target.closest("[data-nav]");
        if (b) navTo(b.dataset.nav);
      });
    }
    const tabs = document.querySelector("#pub-tabs");
    if (tabs) {
      tabs.addEventListener("click", (e) => {
        const b = e.target.closest("[data-ptab]");
        if (b) gotoPubTab(b.dataset.ptab);
      });
    }
    // 按钮在 #pub-body 之外（页头），BODY() 的委托收不到，单独挂。
    const openBtn = document.querySelector("#btn-open-platform");
    if (openBtn) {
      openBtn.addEventListener("click", async () => {
        if (S.openBusy) return;
        const aid = currentAid();
        if (!aid) { toast("先在「发布」页选一个号"); return; }
        const a = (S.d && S.d.current_account) || {};
        if (a.state !== "online") {
          // 置灰是主闸，这里是兜底（状态刚翻过来还没重画时点了）
          toast(a.state === "expired" ? "这个号登录过期了，先重新登录再打开助手"
                                      : "这个号还没登录，先重新登录再打开助手");
          return;
        }
        S.openBusy = true; syncOpenBtn();
        try {
          const r = await api("/api/publish/open-platform", {
            method: "POST", body: JSON.stringify({ aid: aid }),
          });
          if (!r || r.ok !== true) {
            toast((r && r.note) || "窗口没打开，再点一次试试");
          } else if (r.logged_in === false) {
            // 登录态没带过去就直说，绝不假装「免扫码成功」
            toast("窗口开了，但这个号的登录没带过去；在窗口里扫一次码就能进");
          } else {
            // 后端已经区分「还开着」和「正在打开」，有 note 就用它
            toast(r.note || (r.already ? "窗口已经开着了，切过去看就行"
                            : "已打开，用的就是当前这个号"));
          }
        } catch (e) { toast(e.message); }
        S.openBusy = false; syncOpenBtn();
      });
    }
    // 数据和诊断在 #pub-body 之外（各自的容器），单独挂
    const statBox = document.querySelector("#pub-stat");
    if (statBox) {
      statBox.addEventListener("click", (e) => {
        const b = e.target.closest("[data-act]");
        // 通知面板：点遮罩关闭，点面板内部不关（data-stop）
        if (b && b.dataset.act === "statNoticeClose"
          && e.target.closest("[data-stop]") && e.target.closest("[data-stop]") !== b) return;
        if (b) actStat(b.dataset.act, b);
      });
      // 筛选控件：只改显示状态，不重新采数据、不打浏览器
      const onStatInput = (e) => {
        const t = e.target.closest("[data-stat-input]");
        if (!t) return;
        const k = t.dataset.statInput;
        if (k === "incOnly") S.statIncOnly = !!t.checked;
        else if (k === "filter") S.statFilter = t.value.trim();
        else if (k === "coll") S.statColl = t.value;
        else if (k === "sort") S.statSort = t.value;
        else return;
        // 搜索框不能整块重画（会丢焦点），只重画作品网格
        if (k === "filter") {
          const grid = document.querySelector(".stat-grid");
          if (grid && S.stat) {
            const all = S.stat.posts || [];
            let posts = all.filter((p) => {
              if (S.statIncOnly && !(p.inc > 0)) return false;
              if (S.statColl && p.collection !== S.statColl) return false;
              if (S.statFilter) {
                const q = S.statFilter.toLowerCase();
                if (!String(p.title || "").toLowerCase().includes(q)
                  && !String(p.collection || "").toLowerCase().includes(q)) return false;
              }
              return true;
            });
            if (S.statSort === "play") posts = [...posts].sort((a, b) => b.play - a.play);
            else if (S.statSort === "inc") posts = [...posts].sort((a, b) => (b.inc || 0) - (a.inc || 0));
            else if (S.statSort === "finish") posts = [...posts].sort((a, b) => (b.finish_rate || 0) - (a.finish_rate || 0));
            grid.innerHTML = posts.length ? posts.map(statCard).join("")
              : `<div class="pub-empty">没有符合筛选条件的作品。</div>`;
            return;
          }
        }
        renderStat(false);
      };
      statBox.addEventListener("input", onStatInput);
      statBox.addEventListener("change", onStatInput);
    }
    const diagBox = document.querySelector("#pub-diag");
    if (diagBox) {
      diagBox.addEventListener("click", (e) => {
        const b = e.target.closest("[data-act]");
        if (b) actDiag(b.dataset.act, b, e);
      });
    }
    if (window.Session && window.Session.state && window.Session.state.access) {
      refreshBadge();
      setInterval(refreshBadge, 5 * 60 * 1000);
    }
    if (!BODY()) return;

    VIEW().addEventListener("click", async (e) => {
      const relog = e.target.closest("[data-acc-relogin]");
      if (relog) {
        const aid = relog.dataset.accRelogin;
        // 重登 = 一定要换一个微信登录，所以 force（后端先清旧登录态）。
        // 已经在登录别的号：openLogin 会先把那个会话连轮询一起收掉。
        await openLogin(aid, { force: true, label: accLabel(aid) });
        return;
      }
      const ren = e.target.closest("[data-acc-rename]");
      if (ren) {
        const aid = ren.dataset.accRename;
        const cur = ((S.d && S.d.accounts) || []).find((x) => x.id === aid) || {};
        const nm = prompt("给这个号改个名字（只有你自己看得到，不影响视频号）：", cur.name || "");
        if (nm === null) return;
        if (!nm.trim()) { toast("名字不能是空的"); return; }
        api(`/api/publish/accounts/${aid}`, { method: "PUT", body: JSON.stringify({ name: nm.trim() }) })
          .then(() => { toast("改好了"); return load(); })
          .catch((err) => toast(err.message));
        return;
      }
      const del = e.target.closest("[data-acc-del]");
      if (del) {
        const aid = del.dataset.accDel;
        const cur = ((S.d && S.d.accounts) || []).find((x) => x.id === aid) || {};
        if (!confirm(`删掉「${cur.name || "这个号"}」？\n登录状态会一起清掉，之后要重新登录。已定时的发布不受影响。`)) return;
        api(`/api/publish/accounts/${aid}`, { method: "DELETE" })
          .then(() => { toast("已删除"); return load(); })
          .catch((err) => toast(err.message));
        return;
      }
      const acc = e.target.closest("[data-acc]");
      if (acc) {
        const aid = acc.dataset.acc;
        if (currentAid() === aid) return;
        // 切号 = 开新会话：上一个登录窗口的提示（连轮询）先收掉，别带着走
        endLogin();
        try {
          // 选号是服务端的动作：它会把「当前发到」记下来，并清掉上一个号
          // 专属的剧集 / 位置 / 合集 id。前端只负责重画，不自己拼状态。
          await selectAccount(aid);
          S.opts = {}; S.optQ = {}; S.optFor = ""; S.optState = ""; S.optFailAt = 0;
          S.optSeq++;                    // 上一个号还在跑的预取结果直接丢
          S.stat = null; S.diag = null; S.mon = null; S.abn = null;
          if (S.tab === "stat") renderStat(true);
          if (S.tab === "diag") renderDiag(true);
          paintAccountBox();
          refreshFields();
          toast(`已切到【${accLabel(aid)}】，正在核对这个号的登录态`);
          // 切号即验号（2026-11-14 用户定）：卡片状态是本地簿记，切过来就
          // 真核对一次；失效/未登录直接弹出登录窗口，不用再手点「重登」
          await verifyOnSwitch(aid, true);
          ensureOpts(true);              // 号是好的才会走到这里读候选
        } catch (err) { toast(err.message); }
        return;
      }
      const gb = e.target.closest("[data-gap]");
      if (gb) {
        // 档位按钮：点完要重画一遍（高亮 + 自定义框的值跟着变），再重算排期
        S.gap = Number(gb.dataset.gap) || 30;
        render();
        maybePlan();
        return;
      }
      const b = e.target.closest("[data-act]");
      if (b) act(b.dataset.act, b);
    });

    VIEW().addEventListener("change", (e) => {
      // ② 通用设置：改动即套用（2026-11-13 去掉「套用到这批视频」按钮）
      const bf = e.target.closest("[data-bf]");
      if (bf) {
        // 短标题不到 6 个字后端会拒（平台要求 6-16 字）。别把「还没打完」当错误
        // 弹出去，先只提示还差几个，等够了再自动套用。
        if (bf.dataset.bf === "short_title") {
          const n = (bf.value || "").trim().length;
          if (n && n < 6) {
            const hint = document.querySelector("#pub-apply-hint");
            if (hint) hint.textContent = `短标题还差 ${6 - n} 个字（要 6-16 个字）`;
            return;
          }
        }
        applyBusy = doApply();
        return;
      }
      const opt = e.target.closest("[data-opt]");
      if (opt) { applyBusy = doApply(); return; }
      const cb = e.target.closest("[data-cand]");
      if (cb) {
        const n = Number(cb.dataset.cand);
        cb.checked ? S.sel.add(n) : S.sel.delete(n);
        // 只重画 ①（已选条数、加入按钮的可用态都在这块里）。以前整页重画，
        // 勾一条就把 ② 的输入框重建一遍 —— 你刚打的话题会没。
        paintCand();
        return;
      }
      if (e.target.id === "pub-proj-sel") {
        const pid = e.target.value;
        S.pid = pid;
        S.cand = [];
        S.sel.clear();
        if (!pid) { render(); return; }
        api(`/api/projects/${pid}/publish/sets`).then((r) => {
          S.pname = r.name || "";
          S.cand = (r.sets || []).map((s) => ({
            index: s.index, tag: s.tag, name: s.name, size: s.size, path: s.file,
          }));
          S.sel = new Set(S.cand.map((_c, i) => i));
          render();
        }).catch((err) => toast(err.message));
        return;
      }
      if (e.target.id === "pub-first") { S.first = e.target.value; maybePlan(); return; }
      if (e.target.id === "pub-gap") { S.gap = Number(e.target.value) || 30; maybePlan(); return; }
      const t = e.target.closest("[data-title]");
      if (t) {
        api(`/api/publish/job/${t.dataset.title}`, {
          method: "PUT", body: JSON.stringify({ title: t.value }),
        }).then(() => toast("标题改好了")).catch((err) => toast(err.message));
      }
    });

    // 话题打字时防抖套用；失焦的 change 已兜底发过一次，这里只是「边打边存」的即时感
    VIEW().addEventListener("input", (e) => {
      if (e.target.closest('[data-bf="topics"]')) autoApply(600);
      const st = e.target.closest('[data-bf="short_title"]');
      if (st) {
        const n = (st.value || "").trim().length;
        if (!n || n >= 6) autoApply(600);   // 打到一半（1~5 字）先不发
      }
    });

    // 位置搜索框回车就搜。不监听 keydown 的话，用户敲完只能去点旁边那个「搜」，
    // 而 render 会重建 DOM —— 敲了半天字一刷新就没了。
    VIEW().addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      const q = e.target.closest("[data-optq]");
      if (!q) return;
      e.preventDefault();
      const kind = q.dataset.optq;
      S.optQ = S.optQ || {};
      S.optQ[kind] = q.value;
      // 直接触发旁边那个「搜」按钮，走同一条逻辑，不另写一份
      const btn = q.closest("label").querySelector(`[data-act="loadOpt"][data-kind="${kind}"]`);
      if (btn) btn.click();
    });
  }

  // 顶栏角标要能自己刷（用户可能一直待在项目页）
  window.PublishView = { navTo, refreshBadge };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind);
  else bind();
})();
