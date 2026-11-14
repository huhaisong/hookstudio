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
    key: "", scan: null, edit: null, picker: null, opts: {}, optBusy: {},
    // 选好号后的静默预取：optFor 记住已经备好的是哪个号，optSeq 用来丢弃
    // 切号之前发出的旧响应，optState 只是 ② 里一行小字，不弹窗不打断。
    optFor: "", optSeq: 0, optState: "", optFailAt: 0,
    // 当前选中的号只由 /api/publish/queue 下发（current_account_id），前端不存：
    // 刷新、多开页面都只会看到服务端那一份，避免界面显示 A、实际发到 B。
    tab: "pub", aid: "", stat: null, diag: null, mon: null,
    statBusy: false, diagBusy: false, abn: null, delBusy: false, openBusy: false,
    fix: null,   // 「登录失效就地出码」的扫码卡；null＝没在修
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
  };

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pad = (n) => String(n).padStart(2, "0");
  /** 拉候选要开一次浏览器。后端最坏 150 秒，前端不能跟着无限转：
   *  超时就恢复按钮并把话说明白，不然用户只看到「正在读剧集」永远不动。
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
      stopScanPoll();
      S.scan = null;
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
    } else {
      stopPoll();
      stopScanPoll();
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
    const posts = d.posts || [];
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
              ? `${esc(String(acc.fans || 0))} 粉丝 · ${esc(String(acc.feeds || 0))} 个作品`
              : "点右边「采一次」，程序去号里读一遍"}</span>
          </div>
        </div>
        <div class="stat-ops">
          <span class="hint">${d.at ? `上次 ${esc(showTime(d.at))}` : ""}</span>
          <button class="primary sm" data-act="statSample"${S.statBusy ? " disabled" : ""}>
            ${S.statBusy ? "正在读…" : d.at ? "再采一次" : "采一次"}</button>
        </div>
      </div>
      ${posts.length ? `
        <div class="stat-card">
          <div class="stat-card-head">
            <h3>作品数据</h3>
            <span class="hint">播放量是平台给的总数；「这段时间涨了」要至少采样两次才看得出来。</span>
          </div>
          <div class="stat-tbl-wrap"><table class="stat-tbl">
            <thead><tr>
              <th>作品</th><th>发布时间</th><th class="num">播放数</th><th class="num">这段时间涨了</th>
              <th class="num">点赞</th><th class="num">评论</th><th class="num">转发</th><th class="num">收藏</th>
            </tr></thead>
            <tbody>${posts.map(statRow).join("")}</tbody>
          </table></div>
        </div>`
        : `<div class="pub-empty">${esc(d.note
          || "还没有数据。点右上「采一次」，程序会去号里读一遍（大约 10 秒）。")}</div>`}`;
  }

  function statRow(p) {
    const bars = p.series || [];
    // 柱子 = 相邻两点的差。第一次采样时只有一个点，画不出柱，这是对的，不是 bug
    const diffs = bars.slice(1).map((v, i) => Math.max(0, v - bars[i]));
    const max = Math.max(1, ...diffs);
    const cols = diffs.map((dv) => `<i style="height:${Math.max(1, Math.round(dv / max * 14))}px"
      title="+${dv}"></i>`).join("");
    const inc = p.inc == null ? "—" : (p.inc > 0 ? "+" + p.inc : String(p.inc));
    // 合集名只在和标题不一样时才显示，否则同一行字会出现两遍
    const col = p.collection && p.collection !== p.title ? `<em>${esc(p.collection)}</em>` : "";
    return `<tr>
      <td><div class="stat-w">
        <span class="stat-cov">${p.cover
          ? `<img src="${esc(p.cover)}" alt="" loading="lazy">` : ""}</span>
        <div class="stat-wt"><b>${esc(p.title || "（无标题）")}</b>${col}</div>
      </div></td>
      <td class="muted">${esc(tsText(p.ts))} · ${durText(p.dur)}</td>
      <td class="num"><b>${esc(String(p.play))}</b></td>
      <td class="num"><b class="${p.inc > 0 ? "up" : "muted"}">${esc(inc)}</b>${
        cols ? `<span class="stat-bars">${cols}</span>`
             : `<span class="mini muted">再采一次可见</span>`}</td>
      <td class="num">${esc(String(p.like))}</td>
      <td class="num">${esc(String(p.comment))}</td>
      <td class="num">${esc(String(p.forward))}</td>
      <td class="num">${esc(String(p.fav))}</td>
    </tr>`;
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
      S.opts = {}; S.optFor = ""; S.optSeq++;
      if (S.tab === "stat") renderStat(true);
      if (S.tab === "diag") renderDiag(true);
    }
    const key = JSON.stringify((d.jobs || []).map((j) => [j.id, j.status, j.plan_at, j.title]))
      + (d.gate ? d.gate.ok + d.gate.reason : "");
    if (quiet && key === S.key) { paintBadge(d); paintGlobalAcc(); ensureOpts(); reconcileAccounts(); return; }
    S.key = key;
    render();
    maybePlan();
    paintBadge(d);
    ensureOpts();
    reconcileAccounts();
    maybeAutoFix(d);
  }

  /** 当前号的登录态是「过期」→ 自动摆出就地出码的扫码卡
   *  （2026-11-14 用户要求：「选择了登录过期的账号，不应该是到扫码的时候了吗」）。
   *  一次页面会话每个号只自动摆一次；用户点「取消」后不再纠缠。
   *  摆卡本身会自查登录态：其实已经续上了就直接收摊，不会让人白扫。 */
  function maybeAutoFix(d) {
    const cur = d && d.current_account;
    if (!cur || cur.state !== "expired" || S.scan || S.fix) return;
    S.fixOffered = S.fixOffered || {};
    if (S.fixOffered[cur.id]) return;
    S.fixOffered[cur.id] = true;
    openFix(cur.id, true);
  }

  /** 对一遍账：把每个号的**真实**登录态读回来，更新列表。
   *
   *  为什么要有：账号状态是存在本地的。登录那一刻如果前端没赶上（页面刷新、
   *  轮询被浏览器打断、窗口被关），库里就永远停在「待扫码」，而实际早就登录
   *  好了 —— 用户看到的就是「明明已经登录了，还一直说要扫码」
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

  /** 选好号就把这个号能自动读的候选一次拉全。
   *
   *  「无感」= 不弹提示、不整页重画、不挡住任何输入：只在 ② 里放一行小字，
   *  回来后把它换成下拉。走的是**一个**批量接口，不是连发三个单项 ——
   *  session 的 page 是复用的，同一号的操作排在一个线程里，连发会各开一次
   *  浏览器、串行排队二十多秒，还会互相踩「弹层正在点」的状态。
   *
   *  位置不预取：平台要地名才给候选，没关键词时读不到任何东西。
   */
  function ensureOpts(force) {
    const aid = currentAid();
    if (!aid) { S.optFor = ""; return; }
    const acc = ((S.d && S.d.accounts) || []).find((x) => x.id === aid) || {};
    if (acc.state !== "online") return;
    if (!force && S.optFor === aid) return;
    // load() 是轮询，失败后别每隔几秒就重开一次浏览器；单项手动重试不受限
    if (!force && Date.now() - (S.optFailAt || 0) < 5 * 60 * 1000) return;
    S.optFor = aid;
    const seq = ++S.optSeq;
    S.optState = "loading";
    refreshFields();
    apiT(`/api/publish/options/all?aid=${encodeURIComponent(aid)}`)
      .then((r) => {
        // 中途换了号：旧响应直接丢，别把上一个号的剧集塞给这个号
        if (seq !== S.optSeq || currentAid() !== aid) return;
        if (!r || r.error) {
          S.optFor = ""; S.optFailAt = Date.now(); S.optState = "fail";
          refreshFields();
          probeLoginOnFail(aid);   // 失败最常见的原因就是登录掉了，问一句
          return;
        }
        ["declare", "link", "location"].forEach((k) => { if (r[k]) S.opts[k] = r[k]; });
        S.optState = "";
        refreshFields();
      })
      .catch(() => {
        if (seq !== S.optSeq) return;
        S.optFor = ""; S.optFailAt = Date.now(); S.optState = "fail";
        refreshFields();
        probeLoginOnFail(aid);     // 95 秒超时这种，多半也是卡在登录页上
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

  // ── 渲染 ──────────────────────────────────────────────────────────────
  function render() {
    const d = S.d;
    if (!d) return;
    paintGlobalAcc();
    const g = d.gate || { ok: true };
    if (!g.ok) {
      const tips = {
        offline: ["网络异常，暂时用不了",
          "现在连不上服务器，确认不了你的版本权限。等网络恢复再打开这一页就行，已经排好的不会丢。"],
        stale: ["登录状态已失效",
          "服务器那边这张登录凭证已经作废了（换过机器登录或改过密码都会这样）。退出账号重新登录一次再进来。"],
        need_pro: ["此功能需要专业版",
          "批量定时发布是专业版功能。开通后一次排一批，到点提醒你发。"],
      };
      const [head, body] = tips[g.reason] || ["暂时用不了", "稍后再试一次。"];
      BODY().innerHTML = `<div class="pub-gate"><b>${esc(head)}</b><p>${esc(body)}</p></div>`;
      return;
    }

    const accounts = d.accounts || [];
    const jobs = d.jobs || [];
    const drafts = jobs.filter((j) => j.status === "draft").length;
    const queuedN = jobs.filter((j) => j.status === "queued").length;
    // 队列 = 设置工作台（还没安排的草稿）；已提交 = 点完「开始上传」的
    // 后台任务（等着传 / 正在传 / 已定时 / 已提交 / 已发布 / 失败 / 已取消）。
    // 开始上传成功后队列清空、通用设置恢复默认，下一批从头填
    // （2026-11-14 用户定稿：后台任务不占设置工作台）。
    const draftJobs = jobs.filter((j) => j.status === "draft");
    const submittedJobs = jobs.filter((j) => j.status !== "draft");
    const failedN = submittedJobs.filter((j) => j.status === "failed").length;
    const noAcc = !currentAid();
    const startTxt = noAcc ? "先在上面账号条选号"
      : `开始上传（${drafts} 条）`;
    BODY().innerHTML = `
      <div class="pub-cols">
        <div class="pub-left">
          <section class="pub-sec">
            <div class="pub-sec-h"><span class="pub-n">1</span>
              <div class="pub-h-txt"><b>选视频</b><em>整个文件夹、几个文件，或者从某个项目里挑成片</em></div></div>
            ${candBox()}
            <div class="pub-acts">
              <button class="ghost sm" data-act="pickdir">＋ 选文件夹</button>
              <button class="ghost sm" data-act="pickfiles">＋ 选视频文件</button>
              <button class="ghost sm" data-act="pickproj">＋ 从项目挑</button>
              <span class="hint">已选 ${S.sel.size} 条</span>
              <button class="primary sm" data-act="add" ${S.sel.size && !noAcc ? "" : "disabled"}>加入队列</button>
            </div>
          </section>

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
              退回队列；失败的行内有「重试」「扫码重登」。整批都不要了，
              点「已提交」下面的「批量取消」。
              <b>已经传到平台上的，程序删不掉</b>——要撤就去视频号助手
              （右上角「进入视频号助手」直达）的「待发布」里自己删。</p>
              <p><b>间隔别太小</b>：同一个号 5 分钟连发几条容易被当成同质化搬运，
              同批素材建议 30 分钟以上。</p>
            </div>
          </details>
        </div>

        <aside class="pub-right">
          <div class="pub-qhead">
            <div class="seg pub-qtabs">
              <button class="pub-qtab${S.hist ? "" : " on"}" data-act="qtab" data-hist="0">队列${draftJobs.length ? " " + draftJobs.length : ""}</button>
              <button class="pub-qtab${S.hist ? " on" : ""}" data-act="qtab" data-hist="1">已提交${submittedJobs.length ? " " + submittedJobs.length : ""}</button>
            </div>
          </div>
          ${!S.hist ? `
          <div id="pub-plan" class="pub-plan"></div>
          ${draftJobs.length ? `<div class="pub-rows">${draftJobs.map((j, i) => row(j, i)).join("")}</div>`
            : `<div class="pub-empty">队列是空的。左边选好视频、设好时间和内容，点「开始上传」。</div>`}
          <div class="pub-foot">
            <button class="ghost sm" data-act="clear" ${noAcc || !drafts ? "disabled" : ""}
              title="把「还没安排」的条目从队列里删掉">删掉没安排的${drafts ? `（${drafts} 条）` : ""}</button>
            ${drafts || noAcc ? `<button class="primary" data-act="start" ${noAcc ? "disabled" : ""}>${startTxt}</button>` : ""}
          </div>`
          : (submittedJobs.length
            ? `${failedN ? `<div class="pub-acts"><button class="ghost sm" data-act="retryAll">重试失败 ${failedN} 条</button></div>` : ""}
              <div class="pub-rows">${submittedJobs.map((j, i) => row(j, i)).join("")}</div>
              <div class="pub-foot">
            <button class="ghost sm" data-act="stop" ${noAcc || !queuedN ? "disabled" : ""}
              title="取消还在排队、没上传的条目，退回队列重新设置；已经传到平台的取消不了，得到视频号助手手动删">批量取消${queuedN ? `（${queuedN} 条）` : ""}</button>
              </div>`
            : `<div class="pub-empty">还没有提交过的任务。点「开始上传」之后，交出去的都在这里跟踪。</div>`)}
        </aside>
      </div>`;
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
    // 「登录失效就地出码」：二维码卡挂在全局账号条下面，三个 Tab 都看得见。
    const fix = S.fix ? fixBox() : "";
    if (!aid || !a) {
      return `<div class="pub-cur none global"><span>当前账号</span>
        <b>还没选号</b><em>在下面点一张账号卡片就行。</em></div>${fix}`;
    }
    return `<div class="pub-cur global">
      <div class="pub-cur-l">
        <span>当前账号</span>
        <b>${esc(a.label || a.name || aid)}</b>
        <em>点别的账号卡片可切换；提交过的任务在右边「已提交」里</em>
      </div>
    </div>${fix}`;
  }

  // ── 登录失效就地出码（2026-11-14）────────────────────────────────────
  // 以前候选/上传失败只会说「点一下重试」，用户不知道其实是登录掉了。
  // 现在失败时先探一次登录态，掉线就直接在这儿摆二维码，扫码成功后
  // 自动把候选重新读一遍。复用扫码框的样式，但状态独立（S.fix ≠ S.scan）。
  function fixBox() {
    const f = S.fix || {};
    return `<div class="pub-scan fix">
      <div class="pub-scan-qr">
        ${f.qr ? `<img src="${esc(f.qr)}" alt="微信扫码重新登录视频号助手">`
               : `<div class="pub-scan-wait">${f.busy ? "正在取二维码…" : "…"}</div>`}
      </div>
      <div class="pub-scan-side">
        <b>「${esc(f.label || "这个号")}」的登录失效了</b>
        <p>用微信扫一扫上面的码，登录视频号助手${f.fallback ? "（截图取不到，弹了一个窗口，在那个窗口里扫）" : ""}。
           扫完之后候选会自动重新读，不用刷新页面。</p>
        ${f.err ? `<p class="bad">${esc(f.err)}</p>` : ""}
        <div class="pub-scan-btns">
          <button class="ghost sm" data-act="fixRescan">换一张二维码</button>
          <button class="ghost sm" data-act="fixCancel">取消</button>
        </div>
      </div>
    </div>`;
  }

  function paintFix() { paintGlobalAcc(); }

  /** 顶部全局账号条的全部内容（骨架 #pub-global-acc 里，三个 Tab 共用）。
   *  2026-11-14 用户定：确认账号是发布/数据/诊断一切操作的前提，
   *  不是排片流程的一个步骤——从发布页步骤里挪出来，步骤只剩
   *  选视频 → 通用设置 → 什么时候发。登录失效的就地出码卡（fix）也挂这儿。 */
  function accountBar(d) {
    const accs = (d && d.accounts) || [];
    const head = globalAccBox(d);
    if (S.scan) return `${head}${scanBox()}`;
    return `${head}
      <div class="pub-acc-wrap">
        ${accs.length ? `<div class="pub-accs">${accs.map(accCard).join("")}</div>`
          : `<div class="pub-empty">还没登录过账号。点「添加账号」，用微信扫一下就行。</div>`}
        <div class="pub-acts">
          <button class="ghost sm" data-act="addacc">＋ 添加账号（微信扫码）</button>
          <span class="hint">还能加 ${Math.max(0, (d && d.max_accounts || 10) - accs.length)} 个号</span>
        </div>
      </div>`;
  }

  function stopFixPoll() {
    if (S.fix && S.fix.timer) clearInterval(S.fix.timer);
    if (S.fix) S.fix.timer = null;
  }

  async function openFix(aid, auto) {
    if (!aid) aid = currentAid();
    if (!aid) { toast("先在上面账号条选号"); return; }
    stopFixPoll();
    S.fix = { aid: aid, label: accLabel(aid), qr: "", err: "", busy: true,
              timer: null, since: Date.now(), busyPoll: false, fails: 0,
              fallback: false, auto: !!auto };
    paintFix();
    try {
      // visible=0：先无头截图嵌进界面；后端截不到会自己弹窗兜底并回传 fallback。
      const r = await apiT(`/api/publish/accounts/${aid}/qr?visible=0`,
        { method: "POST" }, 170000);
      if (!S.fix || S.fix.aid !== aid) return;
      if (r.logged_in && !r.qr) {
        // 探测时又变回登录态了（可能刚好被别的流程续上）——直接收摊重读。
        await fixDone(aid);
        return;
      }
      S.fix.qr = r.qr || "";
      S.fix.fallback = !!r.fallback;
      S.fix.busy = false;
      paintFix();
      startFixPoll();
    } catch (e) {
      if (S.fix) { S.fix.busy = false; S.fix.err = e.message || "取二维码失败"; paintFix(); }
    }
  }

  async function rescanFix(hard) {
    if (!S.fix) return;
    S.fix.err = ""; S.fix.busy = true; S.fix.qr = "";
    paintFix();
    try {
      const r = await apiT(`/api/publish/accounts/${S.fix.aid}/qr?refresh=${hard ? 1 : 0}`
        + `&force=${hard ? 1 : 0}&visible=0`, { method: "POST" }, 170000);
      if (!S.fix) return;
      if (r.logged_in && !r.qr) { await fixDone(S.fix.aid); return; }
      S.fix.qr = r.qr || "";
      S.fix.fallback = !!r.fallback;
      S.fix.since = Date.now();
      S.fix.busy = false;
      paintFix();
      startFixPoll();
    } catch (e) {
      if (S.fix) { S.fix.busy = false; S.fix.err = e.message || "取不到码，点「换一张」再试"; paintFix(); }
    }
  }

  function startFixPoll() {
    stopFixPoll();
    if (!S.fix) return;
    S.fix.busyPoll = false;
    S.fix.fails = 0;
    S.fix.timer = setInterval(async () => {
      if (!S.fix) return;
      if (S.fix.busyPoll) return;
      if (Date.now() - (S.fix.since || 0) > QR_TTL) { await rescanFix(true); return; }
      S.fix.busyPoll = true;
      try {
        const r = await api(`/api/publish/accounts/${S.fix.aid}/login-state`);
        if (!S.fix) return;
        if (r.error) { S.fix.err = r.error; paintFix(); return; }
        if (!r.logged_in) return;
        await fixDone(S.fix.aid);
      } catch (e) {
        if (!S.fix) return;
        S.fix.fails = (S.fix.fails || 0) + 1;
        if (S.fix.fails <= 3) { S.fix.err = e.message || "连不上本地服务"; paintFix(); }
      } finally {
        if (S.fix) S.fix.busyPoll = false;
      }
    }, 2500);
  }

  async function fixDone(aid) {
    stopFixPoll();
    const label = S.fix ? S.fix.label : "";
    S.fix = null;
    paintFix();
    // 候选是按号缓存的，登录态变了就得重读；失败计数也一并清掉。
    S.opts = {}; S.optFor = ""; S.optState = ""; S.optFailAt = 0; S.optSeq++;
    await selectAccount(aid, true);
    toast(label ? `已登录：${label}` : "登录成功，正在重新读这个号的选项");
    await load();
    ensureOpts(true);
  }

  function closeFix() {
    stopFixPoll();
    S.fix = null;
    paintFix();
  }

  /** 候选拉取失败后问一句「是不是登录掉了」：掉了就自动摆码，别让人猜。 */
  async function probeLoginOnFail(aid) {
    if (S.fix) return;                       // 已经在扫码流程里就别叠一层
    if (!aid || aid !== currentAid()) return;
    try {
      const r = await apiT(`/api/publish/accounts/${aid}/login-state`,
        {}, 50000);
      if (!r || r.logged_in !== false) return;   // 登录好好的，失败另有原因
      await openFix(aid, true);
    } catch (e) { /* 探测失败就保持原样，不打扰 */ }
  }


  function paintGlobalAcc() {
    const box = document.querySelector("#pub-global-acc");
    if (box && S.d) box.innerHTML = accountBar(S.d);
    syncOpenBtn();
  }

  /** 「进入视频号助手」用的是当前号的登录态，没选号就不给点。
   *  按钮在骨架里，状态跟着队列接口走，不在前端自己记一个 aid。 */
  function syncOpenBtn() {
    const b = document.querySelector("#btn-open-platform");
    if (!b) return;
    const has = !!((S.d && S.d.current_account_id) && S.d.current_account);
    b.disabled = !has || S.openBusy;
    b.textContent = S.openBusy ? "正在打开…" : "进入视频号助手 ↗";
    b.title = has ? "用当前号打开视频号助手，不用扫码" : "先在「发布」页选一个号";
  }

  /** 没确认账号时三页统一的占位；账号管理（① 区）不受这个门槛。 */
  function needAccBox() {
    return `<div class="pub-need-acc"><b>先确认是哪个号</b>
      <p>发布、数据、诊断都是按号分开的。在上面账号条点一张账号卡片，
      选好之后这里就能用了。</p></div>`;
  }

  const ACC_STATE = {
    online:  ["已登录", "on"],
    pending: ["待扫码", "wait"],
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
    // 三样东西都要露：备注名（自己起的，用来区分号）、视频号名称、视频号 ID。
    // 只显示备注名时，客户加了三四个号就分不清哪个是哪个（2026-11-13 用户要求）。
    const nick = a.wx_name || "";
    const same = !nick || nick === a.name;
    const sub = [same ? "" : nick, a.finder_id ? `ID ${a.finder_id}` : ""]
      .filter(Boolean).join(" · ");
    return `<div class="pub-acc${on ? " on" : ""}" data-acc="${a.id}">
      <span class="av">${av}</span>
      <span class="who">
        <b>${esc(a.name)}</b>
        <span class="sub" title="${esc(sub || "还没读到视频号 ID，登录后会自动补上")}">${
          sub ? esc(sub) : `<em>ID 未读到</em>`}</span>
      </span>
      <span class="st ${st[1]}">${st[0]}</span>
      <span class="ops">
        <button class="mini" data-acc-relogin="${a.id}" title="换一个微信重新登录">重登</button>
        <button class="mini" data-acc-rename="${a.id}" title="改个好认的名字">改名</button>
        <button class="mini del" data-acc-del="${a.id}" title="删掉这个号（登录状态一起删）">删</button>
      </span>
    </div>`;
  }

  function scanBox() {
    const s = S.scan || {};
    return `<div class="pub-scan">
      <div class="pub-scan-qr">
        ${s.qr ? `<img src="${esc(s.qr)}" alt="微信扫码登录视频号助手">`
               : `<div class="pub-scan-wait">正在打开视频号助手…</div>`}
      </div>
      <div class="pub-scan-side">
        <b>用微信扫一下</b>
        <p>微信 →「扫一扫」→ <b>手机上点一下确认</b>，这边就自动进去了。</p>
        <p class="mini-note">扫完只是「扫到了」，手机上没有「确认登录」这一下是不会登录的。
          二维码 5 分钟就过期，这边会自己换新的，不用管。</p>
        <p class="mini-note">扫码时程序会打开一个浏览器窗口，<b>别关掉</b>——
          之后上传视频还要用它。</p>
        ${s.err ? `<p class="bad">${esc(s.err)}</p>` : ""}
        <div class="pub-acts">
          <button class="ghost sm" data-act="rescan">换一张二维码</button>
          <button class="ghost sm" data-act="scancancel">取消</button>
        </div>
      </div>
    </div>`;
  }

  const QR_TTL = 4 * 60 * 1000;

  function stopScanPoll() {
    if (S.scan && S.scan.timer) clearInterval(S.scan.timer);
    if (S.scan) S.scan.timer = null;
  }

  function startScanPoll() {
    stopScanPoll();
    if (!S.scan) return;
    if (!S.scan.since) S.scan.since = Date.now();
    S.scan.busy = false;
    S.scan.fails = 0;
    S.scan.timer = setInterval(async () => {
      if (!S.scan) return;
      // 上一轮还没回来就别再发：接口慢的时候请求会越堆越多，
      // 最后变成「界面一直转圈」，看起来跟没反应一模一样。
      if (S.scan.busy) return;
      if (Date.now() - (S.scan.since || 0) > QR_TTL) { await rescan(true); return; }
      S.scan.busy = true;
      try {
        const r = await api(`/api/publish/accounts/${S.scan.aid}/login-state`);
        if (r.error) { S.scan.err = r.error; render(); return; }
        if (!r.logged_in) return;
        stopScanPoll();
        const nm = (r.account && (r.account.name || r.account.wx_name)) || "";
        const loggedAid = S.scan.aid;
        S.scan = null;
        S.opts = {}; S.optFor = ""; S.optState = ""; S.optFailAt = 0; S.optSeq++;
        await selectAccount(loggedAid, true);
        toast(nm ? `已登录：${nm}` : "登录成功");
        await load();
      } catch (e) {
        // 不再静默吞掉：扫完码「没反应」最常见的原因就是这里连着报错、
        // 但界面上一个字都不显示。告诉用户连不上，比让他干瞪眼强。
        if (!S.scan) return;
        S.scan.fails = (S.scan.fails || 0) + 1;
        S.scan.err = e.message || "连不上本地服务";
        render();
      } finally {
        if (S.scan) S.scan.busy = false;
      }
    }, 2000);
  }

  async function beginScan(aid, qr) {
    S.scan = { aid: aid, qr: qr || "", err: "", timer: null, since: Date.now(),
               busy: false, fails: 0 };
    render();
    if (!qr) await rescan();
    else startScanPoll();
  }

  async function rescan(force, hard) {
    if (!S.scan) return;
    S.scan.err = "";
    render();
    try {
      // hard=1 是「重登」：后端会先把这个号的登录态清掉，保证给的是一张新码。
      // 不带这个参数时，浏览器里还留着自己就回「已登录 + 空码」，界面看着就是没反应。
      const r = await api(`/api/publish/accounts/${S.scan.aid}/qr?refresh=${force ? 1 : 0}`
        + `&force=${hard ? 1 : 0}`, { method: "POST" });
      if (!S.scan) return;
      S.scan.qr = r.qr || "";
      S.scan.since = Date.now();
      if (r.logged_in && !r.qr) {
        // 已经登录着、又没拿到新码：说明这次没真重登，如实说，别装作在扫码。
        stopScanPoll();
        S.scan.err = "这个号现在是登录状态，没能拿到新的二维码。再点一次「重登」试试。";
        render();
        return;
      }
      if (r.logged_in) {
        stopScanPoll();
        const loggedAid = S.scan.aid;
        S.scan = null;
        await selectAccount(loggedAid, true);
        await load();
        return;
      }
      render();
      startScanPoll();
    } catch (e) {
      if (S.scan) { S.scan.err = e.message; S.scan.qr = ""; render(); }
    }
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
  /** ② 里的一行小字：候选是从哪个号读回来的，别让人猜。 */
  function optsOwnerLine() {
    if (!S.optFor) return "";
    return `<div class="pub-opts-from">这些选项来自 <b>${esc(accLabel(S.optFor))}</b>${
      S.optState === "loading" ? " · 正在读…" : ""}</div>`;
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
    // 拉一次要开浏览器 8-15 秒。中间必须有个「在读」的样子，否则看着像按钮没反应。
    if (S.optBusy[key]) {
      return `<label data-pk="${key}"><span>${label}</span>${face("正在读…")}
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
    const busy = S.optBusy.link;
    let body;
    if (busy) {
      body = `<button class="pub-sel" disabled>正在读剧集…</button>`;
    } else if (!o) {
      body = `<button class="pub-sel" data-act="loadOpt" data-kind="link">选条目</button>`;
    } else if (o.err) {
      body = `<button class="pub-sel bad" data-act="loadOpt" data-kind="link">${esc(o.err)}</button>`;
    } else if ((o.items || []).length) {
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
    } else {
      body = `<button class="pub-sel" data-act="loadOpt" data-kind="link">重试一次</button>`;
    }
    const note = (!busy && o && !o.err && !(o.items || []).length) ? (o.note || "") : "";
    // 类型固定成「视频号剧集」：其它三种（文章/红包封面/小程序短剧）这轮不用，
    // 留一个类型下拉反而让人以为切过去就能用。字段值由 collectBatchFields 写死 series。
    const owner = S.optFor ? `<i class="pub-from">来自 ${esc(accLabel(S.optFor))}</i>` : "";
    return `<label class="span2"><span>扩展链接 · 视频号剧集${owner}</span>
      <div class="pub-link">${body}</div>
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
    const hint = S.optState === "loading"
      ? `<div class="pub-opt-state">正在准备这个号的选项…不用等，继续填就行</div>`
      : S.optState === "fail"
        ? `<div class="pub-opt-state bad">没读到选项，点字段里的按钮重试</div>` : "";
    return `${hint}${optsOwnerLine()}
      ${coverBox(t)}
      ${picker("location", "位置", t.location, "输入地名后按回车，从结果里挑")}
      ${picker("declare", "视频标注", t.declare, "短剧一般选「内容为虚构剧情」")}
      ${linkField(t)}
      <label><span>话题</span><input data-bf="topics"
             value="${esc(topicsText(t.topics))}" placeholder="空格隔开，不用打 #"></label>
      <label class="chk"><input type="checkbox" data-bf="original"${t.original ? " checked" : ""}>
        <span>声明原创<em>号没开通原创权限时勾了会被驳回</em></span></label>`;
  }

  function refreshFields() {
    const box = document.querySelector("#pub-fields");
    if (!box || !S.d) return;
    box.innerHTML = fieldsHTML(S.d);
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
    // 跟后端 LOCKED_STATUS 对齐（scheduled / published / publishing）：
    // publishing 也算，否则界面上给了「文案」按钮，点进去后端一律拒。
    const locked = ["scheduled", "published", "publishing"].includes(j.status);
    const open = S.edit === j.id;
    const tags = topicsText(j.topics);
    const from = j.project ? ` · 来自「${esc(j.project)}」` : "";
    const ep = (j.src || "").startsWith("set:") ? j.src.slice(4) : `#${i + 1}`;
    return `<div class="pub-row st-${st[1]}${due ? " due" : ""}" data-job="${esc(j.id)}">
      <div class="pub-rhead">
        <span class="pub-ep">${esc(ep)}</span>
        <div class="pub-st ${st[1]}">${due ? "该发了"
          : past ? "已提交"
          : (j.status === "publishing" && j.step) ? `正在上传 · ${esc(j.step)}` : st[0]}</div>
      </div>
      <div class="pub-main">
        <input class="pub-title" data-title="${esc(j.id)}" value="${esc(j.title)}"
               maxlength="100" ${locked ? "disabled" : ""} title="点一下就能改标题">
        <div class="pub-meta">
          ${esc(showTime(j.plan_at))} · 发到 ${esc(j.account_name || (j.status === "draft" ? "开始上传时确定" : "未选号"))} · ${esc(mb(j.size))}
          ${tags ? ` · ${esc(tags)}` : ""}${from}
          ${j.cover_url ? `<img class="pub-cov-dot" src="${esc(j.cover_url)}"
             alt="" title="这条已指定封面图片">` : ""}
          ${j.file_exists === false ? `<span class="bad">文件找不到了</span>` : ""}
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
             <button class="ghost sm" data-act="fixLogin">扫码重登</button>` : ""}
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

  async function doPlan() {
    const box = document.querySelector("#pub-plan");
    const tbox = document.querySelector("#pub-times");
    if (!box || !S.d) return;
    const drafts = ((S.d.jobs) || []).filter((j) => j.status === "draft").length;
    if (!drafts) {
      // 空队列时下面已经有一句「队列是空的」，这里别再重复一遍
      box.innerHTML = (S.d.jobs || []).length
        ? `<div class="hint">都已经安排好了，没有还没安排的。</div>` : "";
      if (tbox) tbox.innerHTML = "";
      return;
    }
    try {
      const r = await api("/api/publish/plan", {
        method: "POST",
        body: JSON.stringify({ first_at: S.first, interval_min: Number(S.gap) || 30 }),
      });
      S.plan = r;
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
      box.innerHTML = `<div class="pub-alarm bad">${esc(e.message)}</div>`;
      if (tbox) tbox.innerHTML = "";
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
        render();
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
        render();
        return;
      }
      if (name === "pickproj") {
        const r = await api("/api/publish/projects");
        S.opts.projs = r.projects || [];
        S.picker = "proj";
        S.cand = [];
        S.sel.clear();
        S.pid = "";
        render();
        return;
      }
      if (name === "backCand") { resetCand(); render(); return; }
      if (name === "clearCand") { resetCand(); render(); return; }
      if (name === "add") {
        const body = { files: [], sets: [], pid: S.pid, dir_path: "" };
        if (S.picker === "proj") {
          body.sets = S.cand.filter((_c, i) => S.sel.has(i))
            .map((c) => Number(c.index)).filter((n) => !Number.isNaN(n));
        } else {
          body.files = S.cand.filter((_c, i) => S.sel.has(i)).map((c) => c.path);
        }
        if (!(body.sets.length || body.files.length)) { toast("先勾几条"); return; }
        const r = await api("/api/publish/add", { method: "POST", body: JSON.stringify(body) });
        const dup = (r.dup || []).length;
        toast(`加了 ${(r.added || []).length} 条` + (dup ? `，${dup} 条已经在队列里` : ""));
        resetCand();
        await load();
        return;
      }
      if (name === "addacc") {
        toast("正在打开视频号助手，稍等一下…");
        const r = await api("/api/publish/accounts", { method: "POST", body: JSON.stringify({}) });
        await beginScan(r.account.id, r.qr);
        return;
      }
      if (name === "rescan") { await rescan(true); return; }
      if (name === "scancancel") {
        const aid = S.scan && S.scan.aid;
        stopScanPoll();
        S.scan = null;
        const a = aid && ((S.d && S.d.accounts) || []).find((x) => x.id === aid);
        if (a && a.state === "pending") {
          try { await api(`/api/publish/accounts/${aid}`, { method: "DELETE" }); } catch (e) { /* 忽略 */ }
        }
        await load();
        return;
      }
      if (name === "loadOpt") {
        const kind = el.dataset.kind;
        const aid = currentAid();
        if (!aid) { toast("先在①里选一个号，候选要从这个号里拉"); return; }
        const acc = ((S.d && S.d.accounts) || []).find((x) => x.id === aid) || {};
        if (acc.state !== "online") { toast("这个号的登录态不在了，先点「重登」扫一下"); return; }
        const box = document.querySelector(`[data-optq="${kind}"]`);
        const q = ((box && box.value) || (S.optQ && S.optQ[kind]) || "").trim();
        if (kind === "location" && !q) { toast("先输入地点名，比如「石牌」"); return; }
        S.optQ = S.optQ || {};
        S.optQ[kind] = q;
        const seq = S.optSeq;        // 中途切号，上一个号的旧候选不许写回来
        S.optBusy[kind] = true;      // 这一步要 8-15 秒，必须让界面看得出在读
        render();
        try {
          // 类型固定成视频号剧集（界面上已经没有类型下拉了）
          const r = await apiT(`/api/publish/options?aid=${encodeURIComponent(aid)}`
            + `&kind=${encodeURIComponent(kind)}&q=${encodeURIComponent(q)}`
            + (kind === "link" ? "&link_kind=series" : ""));
          if (seq === S.optSeq) S.opts[kind] = r;
        } catch (e) {
          if (seq === S.optSeq) S.opts[kind] = { err: e.message };
        }
        delete S.optBusy[kind];
        render();
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
        S.edit = S.edit === jid ? null : jid;
        render();
        return;
      }
      if (name === "closef") { S.edit = null; render(); return; }
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
        const who = a.label || a.name || acc;
        // 第一条贴得太近而条数又多时，逐条上传多半赶不上排的时间——平台会拒
        // 或直接失败。提前一句话说清，比事后一条条标失败强（2026-11-14）。
        const minsAhead = (new Date(S.first).getTime() - Date.now()) / 60000;
        const warn = (n >= 2 && minsAhead < 15)
          ? `\n⚠ 第一条离现在只有 ${Math.max(0, Math.round(minsAhead))} 分钟，`
            + `${n} 条逐条上传多半来不及，建议把第一条往后调。\n` : "";
        // 不可逆之前把「发到哪个号」写全：备注名 + 视频号名 + ID，
        // 不是一句「确定吗」能替代的（2026-11-13 用户要求）。
        if (!confirm(`这 ${n} 条会发给：\n【${who}】\n`
          + `首条 ${showTime(S.first)} · 间隔 ${Number(S.gap) || 30} 分钟\n`
          + `${warn}\n`
          + "确认后立刻开始逐条上传，并按上面的时间在平台定时；发出去就收不回来了。\n"
          + "提交后队列清空、设置恢复默认，这批到「已提交」里跟踪；切号随时可以。\n"
          + "还在排队的条目可以在「已提交」里批量取消。确定吗？")) return;
        const r = await api("/api/publish/start", {
          method: "POST",
          body: JSON.stringify({ first_at: S.first, interval_min: Number(S.gap) || 30, account_id: acc }),
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
          ? `已重新排队：原定时间已经过了，改到 ${showTime(r.moved_to)} 发`
          : "已重新排队，接着上传");
        await load();
        return;
      }
      if (name === "retryAll") {
        // 逐条调 retry 接口；start() 会自动把上传线程喊起来，不用单独管
        const ids = ((S.d && S.d.jobs) || [])
          .filter((j) => j.status === "failed").map((j) => j.id);
        let moved = 0;
        for (const id of ids) {
          const r = await api(`/api/publish/job/${id}/retry`, { method: "POST" })
            .catch(() => null);
          if (r && r.moved_to) moved++;
        }
        toast(ids.length
          ? `已重新排队 ${ids.length} 条，接着上传`
            + (moved ? `（其中 ${moved} 条原定时间已过，已往后顺延）` : "")
          : "没有可重试的");
        await load();
        return;
      }
      if (name === "stop") {
        if (!confirm("批量取消还在排队、没上传的条目？\n"
          + "它们会退回队列重新设置；已经传到平台并定了时间的，程序取消不了"
          + "（要去视频号助手「待发布」里手动删），已经发出去的也不受影响。继续吗？")) return;
        const r = await api("/api/publish/stop", { method: "POST" });
        toast(r.on_platform
          ? `取消 ${r.reverted} 条；另有 ${r.on_platform} 条已交平台，取消不了`
          : `取消 ${r.reverted} 条`);
        await load();
        return;
      }
      if (name === "clear") {
        if (!confirm("把「还没安排」的条目从队列里删掉？\n"
          + "已经排好、已经发出去的都不动。删了以后想发要重新勾一次。继续吗？")) return;
        const r = await api("/api/publish/clear-draft", { method: "POST" });
        toast(`删掉了 ${r.removed} 条`);
        await load();
        return;
      }
      if (name === "qtab") {
        S.hist = el.dataset.hist === "1";
        render();
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
      if (name === "fixLogin") {
        await openFix();
        return;
      }
      if (name === "fixRescan") { await rescanFix(false); return; }
      if (name === "fixCancel") { closeFix(); toast("收起了；需要时再点「扫码重登」"); return; }
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
            toast(r.already ? "窗口已经开着了，切过去看就行"
                            : "已打开，用的是当前这个号，不用扫码");
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
        if (b) actStat(b.dataset.act, b);
      });
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
        // 重登 = 一定要重新扫码（换一个微信登录），所以走 force。
        S.scan = { aid: aid, qr: "", err: "", timer: null, since: Date.now(),
                   busy: false, fails: 0 };
        render();
        rescan(true, true);
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
        if (!confirm(`删掉「${cur.name || "这个号"}」？\n登录状态会一起清掉，之后要重新扫码。已经排好的发布计划不受影响。`)) return;
        api(`/api/publish/accounts/${aid}`, { method: "DELETE" })
          .then(() => { toast("已删除"); return load(); })
          .catch((err) => toast(err.message));
        return;
      }
      const acc = e.target.closest("[data-acc]");
      if (acc) {
        const aid = acc.dataset.acc;
        if (currentAid() === aid) return;
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
          ensureOpts(true);
          toast(`已切到【${accLabel(aid)}】，正在重读候选`);
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
      if (bf) { applyBusy = doApply(); return; }
      const opt = e.target.closest("[data-opt]");
      if (opt) { applyBusy = doApply(); return; }
      const cb = e.target.closest("[data-cand]");
      if (cb) {
        const n = Number(cb.dataset.cand);
        cb.checked ? S.sel.add(n) : S.sel.delete(n);
        cb.closest(".pub-pick").classList.toggle("on", cb.checked);
        const hint = document.querySelector(".pub-acts .hint");
        render();
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
