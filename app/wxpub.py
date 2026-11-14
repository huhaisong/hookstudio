"""视频号发布页：怎么开、怎么读、怎么填。

分工（改版时只动这里和 wxsel.py）：
  `wxbot.py`  驱动浏览器、账号 profile、扫码登录
  `wxpub.py`  发布页的动作（本文件）—— 上传、填文案、设定时、回读核对
  `wxsel.py`  所有选择器

### 为什么要先有 `dump()`
视频号助手是异步组件（合集下拉、位置搜索、定时开关都是后加载的）。选择器靠猜
能跑一次，改版后静默填错、把「含 AI 声明」填成「自制」这种东西没人会发现。
所以先有只读探针：把真实页面上的输入框、按钮、单选、关键区块连同**位置**倒出来，
按真实 DOM 写选择器，再逐项回读核对（填了 ≠ 生效）。

`dump()` 只读不写：不点任何按钮、不改任何输入。
"""
from __future__ import annotations

import datetime
import json
import os
import re

from . import core

CREATE_URL = "https://channels.weixin.qq.com/platform/post/create"
LIST_URL = "https://channels.weixin.qq.com/platform/post/list"
PLATFORM_URL = "https://channels.weixin.qq.com/platform"       # 首页：账号信息 + 昨日数据 + 最近视频


class WxPubError(Exception):
    """发布页操作失败（文案已经是人话，路由层直接给用户看）。"""


# 表单里能认出「这就是发表表单」的关键词（与 wxpost.FORM_KEYS 保持一致）。
FORM_KEYS = ("视频描述", "短标题", "位置", "合集", "链接", "活动",
             "定时发表", "封面")


def form_frame(page, timeout: float = 90.0):
    """找出**真正渲染出表单**的那一帧。

    ### 为什么不能按 frame 的 URL 认
    `micro/content/post/create` 那一帧常年停在「We're sorry but
    finder-helper-web doesn't work properly without JavaScript enabled」的
    引导页上（2026-11-13 连续两次实测），真正能操作的表单在点过
    「发表视频」之后才出现在**同一帧**里。按 URL 找帧 = 永远对着引导页
    操作 = 每个选择器都数到 0，现象是「页面打开了但什么都点不动」。

    ### 判据为什么这么严
    只看「正文含关键词」会误判：`post-card.html`（最近视频弹层）里也有
    「封面」「链接」这些字，命中 1~2 个词。实测真表单是
    关键词命中 **7** 个、且 `input[type=file]` 有 1 个。所以判据是：
    - 关键词命中 ≥ 3 个，**且**
    - 帧里有 `input[type=file]`（上传入口，一定在表单里）
    两条都满足才是表单。凑不齐就继续等，不将就。
    """
    import time
    end = time.time() + timeout
    best = None
    best_score = 0
    while time.time() < end:
        for fr in page.frames:
            try:
                info = fr.evaluate("""(keys) => {
                  const body = ((document.body && document.body.innerText) || '')
                               .replace(/\\s+/g, ' ');
                  if (!body) return {score: 0, nfile: 0};
                  if (body.includes('does not work properly without JavaScript')) {
                    return {score: 0, nfile: 0};
                  }
                  let score = 0;
                  for (const k of keys) if (body.includes(k)) score++;
                  return {score: score,
                          nfile: document.querySelectorAll('input[type=file]').length};
                }""", list(FORM_KEYS))
            except Exception:  # noqa: BLE001
                continue
            score = int(info.get("score") or 0)
            nfile = int(info.get("nfile") or 0)
            if score > best_score:
                best_score = score
                best = fr
            # 关键词够多、且上传口确实在，才算表单
            if score >= 3 and nfile >= 1:
                return fr
        page.wait_for_timeout(1000)
    return None

# 导出可见元素的 DOM 摘要。放在模块里而不是脚本里：服务端那个 Chrome 正占着
# profile（Windows 上 Cookies 文件被独占，拷出来是残缺的、一开就是登录页），
# 所以探针必须跑在**正在运行的那个浏览器**里，不能另起一个进程。
DUMP_JS = r"""
() => {
  const vis = (e) => {
    if (!e) return false;
    const r = e.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const st = getComputedStyle(e);
    return st.visibility !== 'hidden' && st.display !== 'none' && Number(st.opacity) > 0.05;
  };
  const path = (e) => {
    const parts = [];
    let n = e, d = 0;
    while (n && n.nodeType === 1 && d < 6) {
      let s = n.tagName.toLowerCase();
      if (n.id) { s += '#' + n.id; parts.unshift(s); break; }
      const cls = (n.className && typeof n.className === 'string')
        ? n.className.trim().split(/\s+/).slice(0, 3).join('.') : '';
      if (cls) s += '.' + cls;
      const p = n.parentElement;
      if (p) {
        const sibs = Array.from(p.children).filter((x) => x.tagName === n.tagName);
        if (sibs.length > 1) s += `:nth-of-type(${sibs.indexOf(n) + 1})`;
      }
      parts.unshift(s);
      n = n.parentElement; d++;
    }
    return parts.join(' > ');
  };
  const txt = (e) => ((e.innerText || e.textContent || '') + '').replace(/\s+/g, ' ').trim().slice(0, 60);
  const box = (e) => {
    const r = e.getBoundingClientRect();
    return [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)];
  };

  const inputs = [], buttons = [], radios = [], areas = [], links = [], blocks = [];
  document.querySelectorAll('input, textarea, [contenteditable="true"], [contenteditable=""]').forEach((e) => {
    if (!vis(e)) return;
    areas.push({ tag: e.tagName.toLowerCase(), type: e.type || '',
      ph: e.getAttribute('placeholder') || '', maxlen: e.maxLength || 0,
      accept: e.getAttribute('accept') || '', cls: (e.className || '').toString().slice(0, 90),
      text: txt(e), box: box(e), path: path(e) });
  });
  document.querySelectorAll('button, [role="button"], [class*="btn"]').forEach((e) => {
    if (!vis(e)) return;
    const t = txt(e);
    if (!t || t.length > 20) return;
    buttons.push({ text: t, cls: (e.className || '').toString().slice(0, 80), box: box(e), path: path(e) });
  });
  document.querySelectorAll('[role="radio"], [role="checkbox"], input[type="radio"], input[type="checkbox"], .weui-desktop-form__radio, .weui-desktop-form__checkbox').forEach((e) => {
    if (!vis(e)) return;
    radios.push({ tag: e.tagName.toLowerCase(), type: e.type || '',
      checked: e.checked === true || e.getAttribute('aria-checked') === 'true',
      text: txt(e.parentElement || e), box: box(e), path: path(e) });
  });
  document.querySelectorAll('a[href]').forEach((e) => {
    if (!vis(e)) return;
    const t = txt(e);
    if (!t) return;
    links.push({ text: t, href: (e.getAttribute('href') || '').slice(0, 120), path: path(e) });
  });

  // 关键区块：按关键词找「这一段是什么」（合集/位置/封面/声明/扩展链接/活动）
  const KW = ['合集', '位置', '定位', '封面', '原创', '声明', '链接', '活动', '话题',
              '标题', '定时', '发布', '简介', '描述', 'AI', '转载', '短标题'];
  document.querySelectorAll('div, section, li, span, label').forEach((e) => {
    const t = txt(e);
    if (!t || t.length > 14) return;
    if (!KW.some((k) => t.includes(k))) return;
    if (!vis(e)) return;
    const r = e.getBoundingClientRect();
    if (r.width > 900) return;             // 太宽的容器没信息量
    blocks.push({ text: t, tag: e.tagName.toLowerCase(),
      cls: (e.className || '').toString().slice(0, 90), box: box(e), path: path(e) });
  });

  return {
    url: location.href,
    title: document.title,
    fields: areas, buttons, radios,
    links: links.slice(0, 40),
    blocks: blocks.slice(0, 90),
    bodyText: (document.body.innerText || '').replace(/\n{2,}/g, '\n').slice(0, 2500),
  };
}
"""


# 判断「这一帧是不是装着发表表单」。发表页在 iframe 里，只认主文档会得到 0 个元素。
FRAME_JS = r"""
() => {
  const vis = (e) => {
    const r = e.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const st = getComputedStyle(e);
    return st.visibility !== 'hidden' && st.display !== 'none';
  };
  const inputs = Array.from(document.querySelectorAll('input')).filter(vis).length;
  const editables = Array.from(document.querySelectorAll('textarea, [contenteditable="true"]')).filter(vis).length;
  const body = (document.body && document.body.innerText || '').replace(/\s+/g, ' ');
  const KW = ['视频描述', '短标题', '位置', '合集', '链接', '活动', '定时发表', '封面'];
  return {
    inputs: inputs, editable: editables,
    hits: KW.filter((k) => body.includes(k)),
    head: body.slice(0, 120),
  };
}
"""


def _diag_dir() -> str:
    d = os.path.join(core.DATA_DIR, "diag")
    os.makedirs(d, exist_ok=True)
    return d


# 重放接口时要跟着带过去的头。微信这些 CGI 会看 referer / 自定义头，
# 自己 new 一个请求只带 cookie 是会被拒的（返回空列表，不报错，最难查）。
_HDR_KEEP = ("content-type", "referer", "origin", "x-requested-with")


def _pick_headers(headers: dict) -> dict:
    """只留重放需要的几个头，别把 cookie 抄进诊断文件。"""
    out = {}
    for k, v in (headers or {}).items():
        lk = k.lower()
        if lk in _HDR_KEEP or lk.startswith("x-wechat") or lk.startswith("x-wx"):
            out[lk] = str(v)[:160]
    return out


def dump(session, url: str = CREATE_URL, wait_ms: int = 6000,
         click: str = "", net: bool = False, visible: bool = True) -> dict:
    """只读导出某个页面：DOM 摘要 + 整页截图（可选：抓 XHR 接口）。

    `click` 给文字时，先按文字点一下再 dump —— 用来打开「选择视频号剧集」
    这类弹窗，看它里面的真实结构。
    `net=True` 时把页面发起的 JSON 接口记下来（合集/位置/剧集都是接口拉的，
    光看 DOM 只能看到中文名，拿不到 id）。
    """
    stamp = datetime.datetime.now().strftime("%H%M%S")
    hits: list[dict] = []

    def _go(page):
        if net:
            def _on_resp(resp):
                try:
                    u = resp.url or ""
                    if "cgi-bin" not in u and "/api/" not in u:
                        return
                    ct = (resp.headers or {}).get("content-type", "")
                    if "json" not in ct:
                        return
                    body = resp.json()
                    # method + 请求体也要留：这些接口的参数在 POST body 里，
                    # 只记 URL 等于只看到 _aid/_rid 两个流水号，猜不出怎么传关键词。
                    try:
                        rq = resp.request
                        hits.append({"url": u[:200], "m": rq.method,
                                     "post": (rq.post_data or "")[:600],
                                     "hdrs": _pick_headers(rq.headers),
                                     "data": body})
                    except Exception:  # noqa: BLE001
                        hits.append({"url": u[:200], "data": body})
                except Exception:  # noqa: BLE001  响应读失败不影响主流程
                    pass
            page.on("response", _on_resp)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(wait_ms)
        if click:
            try:
                page.get_by_text(click, exact=False).first.click(timeout=8000)
                page.wait_for_timeout(2500)
            except Exception as exc:  # noqa: BLE001
                hits.append({"click_err": f"{click}: {type(exc).__name__}"})
        out: dict = {}
        try:
            out["dom"] = page.evaluate(DUMP_JS)
        except Exception as exc:  # noqa: BLE001
            out["err"] = f"{type(exc).__name__}: {exc}"
        # 发表页其实在 iframe 里（`micro/content/...`），主文档是空壳。
        # 只 dump 主文档会得到 0 个元素，所以逐帧扫一遍谁装着表单。
        frames = []
        try:
            for fr in page.frames:
                try:
                    info = fr.evaluate(FRAME_JS)
                except Exception as exc:  # noqa: BLE001
                    frames.append({"url": (fr.url or "")[:160],
                                   "err": type(exc).__name__})
                    continue
                info["url"] = (fr.url or "")[:160]
                frames.append(info)
        except Exception:  # noqa: BLE001
            pass
        out["frames"] = frames
        # 真正装着表单的那一帧，把完整 DOM 也导出来
        best = max(frames, key=lambda f: f.get("inputs", 0) + f.get("editable", 0),
                   default=None)
        if best and (best.get("inputs", 0) + best.get("editable", 0)) > 0:
            try:
                for fr in page.frames:
                    if (fr.url or "")[:160] == best.get("url"):
                        out["form"] = fr.evaluate(DUMP_JS)
                        break
            except Exception as exc:  # noqa: BLE001
                out["form_err"] = type(exc).__name__
        if hits:
            out["net"] = hits[-40:]
        try:
            tag = "create" if "create" in url else "page"
            png = os.path.join(_diag_dir(), f"{stamp}-{tag}.png")
            page.screenshot(path=png, full_page=True)
            out["shot"] = png
        except Exception:  # noqa: BLE001
            pass
        return out

    return session.run(_go, timeout=200, visible=visible)


def save_dump(data: dict, name: str) -> str:
    p = os.path.join(_diag_dir(), f"{name}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return p


# 读「刚弹出来的那个列表」里的候选项。按类的后缀匹配，因为微信的类名前面
# 常带作用域前缀（`post-album-item` / `location-item-info` / `activity-item-info`）。
LIST_JS = r"""
(sel) => {
  const vis = (e) => {
    const r = e.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return false;
    const st = getComputedStyle(e);
    return st.visibility !== 'hidden' && st.display !== 'none' && Number(st.opacity) > 0.1;
  };
  const out = [];
  document.querySelectorAll(sel).forEach((e) => {
    if (!vis(e)) return;
    const t = ((e.innerText || '') + '').replace(/\s+/g, ' ').trim();
    if (!t) return;
    out.push({
      text: t.slice(0, 120),
      id: e.getAttribute('data-id') || e.getAttribute('data-value') || '',
      cls: (e.className || '').toString().slice(0, 60),
    });
  });
  return out.slice(0, 80);
}
"""

# 链接类型下拉里的四项（2026-11-13 实测）。**只留这一份**：拉候选的兜底 DOM 路径
# 和真正发表时的点击都要用，写两套早晚分叉（2026-11-14 剧集选不中就是分叉的后果）。
LINK_KIND_CN = {"series": "视频号剧集", "article": "公众号文章",
                "redpack": "红包封面", "miniapp": "小程序短剧"}

# 位置/活动/合集/链接 各自「怎么打开」+「列表长什么样」
# 2026-11-13 用真号实测：position-display / post-album-wrap / post-activity-wrap
PICKERS = {
    "collection": {"open": ".post-album-wrap",
                   "sel": '[class*="album-item"], [class*="collection-item"]',
                   "empty": "这个号还没建过合集。去视频号助手「合集」里建一个，再回来选。"},
    "location":   {"open": ".post-position-wrap",
                   "sel": ".location-item-info",
                   "input": 'input[placeholder="搜索附近位置"]',
                   "empty": "没搜到这个地点。换个更短的词试试（比如只写街道名）。"},
    "activity":   {"open": ".post-activity-wrap",
                   "sel": ".activity-item-info",
                   "empty": "这个号现在没有可参与的活动。"},
    "link":       {"open": ".post-link-wrap",
                   "sel": '[class*="series-item"], [class*="export-item"], [class*="album-item"]',
                   # 平台自己的原话是「仅能选择已经上架的剧集」，所以别写「去建一个」
                   # —— 建了没上架一样选不到，那句话会把人带偏。
                   "empty": "这个号没有可选的条目。剧集只列平台已「上架」的；"
                            "公众号文章 / 红包封面也要先在对应后台做好。"},
}

# ── 候选优先走接口，点 DOM 只是兜底 ────────────────────────────────────────
# 2026-11-13 实测：发表页一打开自己就会调这三个接口
#   helper/helper_search_location     位置候选（body 里 query 一换就是搜索结果）
#   collection/get_collection_list    合集
#   post/finder_get_object_tag_list   视频标注（无需标注 / 含AI / 虚构剧情 …）
# 为什么不再点 DOM 取候选：这些行是后加载的，点早了直接超时；类名一改版就静默
# 空手而归（还以为是「这个号没有」）。借页面自己发的请求当模板重放，body 里的
# 鉴权字段照抄，改一个业务字段即可，比手搓请求稳得多。
API_REPLAY = {
    "location":   ("helper/helper_search_location", {"query": "{q}"}),
    "collection": ("collection/get_collection_list", {"pageNum": 1, "pageSize": 50}),
    "declare":    ("post/finder_get_object_tag_list", {"source": 1}),
    # 剧集不是「合集」，接口也不是同一个 —— 2026-11-13 踩过：拿 collection 当剧集
    # 去查，返回空数组，界面就报「这个号还没有剧集」，而号里其实有 39 部。
    # 真正的接口是 search_drama_component，页面只在**点开那个弹层时**才发，
    # 所以 options() 里要先真实点开一次拿模板，再重放翻页（见 _prime_series）。
    "series":     ("post/search_drama_component",
                   {"currentPage": 1, "pageSize": 50}),
}
_HDR_REPLAY = ("content-type", "referer", "origin", "x-wechat-uin")
# 拉候选失败时最难受的是「不知道为什么」，把中间步骤记到 data/diag/ 里。
# 只在 HOOK_OPT_TRACE=1 时写，客户机器上不会留这些文件。
_TRACE = os.environ.get("HOOK_OPT_TRACE") == "1"


def _trace(name: str, obj) -> None:
    if not _TRACE:
        return
    try:
        d = os.path.join(core.DATA_DIR, "diag")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"opt-{name}.json"), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
    except Exception:  # noqa: BLE001
        pass


def collect_tmpl(resp, tmpl: dict) -> None:
    """把页面自己发过的候选 CGI 记成模板（拉候选、发表两条路共用一份）。

    为什么挂 `response` 而不是 `request`：`request.post_data` 碰到二进制上报
    会抛异常，监听器一抛就把整个 sync 通道卡住（表现是接口一直不返回）。
    从 response 反查 request 是 dump() 用了很久的写法，稳。

    2026-11-14：发表流程也要用这套（剧集/标注都得拿 id 才知道该点哪条），
    所以从 options() 里抽出来 —— 两处各写一份的话，迟早只改一处。
    """
    try:
        u = resp.url or ""
        for k, (path, _p) in API_REPLAY.items():
            if path in u and k not in tmpl:
                rq = resp.request
                try:
                    post = rq.post_data or ""
                except Exception:  # noqa: BLE001
                    post = ""
                tmpl[k] = {"url": u, "hdrs": dict(rq.headers or {}), "post": post}
                return
    except Exception:  # noqa: BLE001  记不到就算了，会退回点 DOM
        pass


def _replay(page, tmpl: dict, kind: str, patch: dict, q: str = "") -> dict | None:
    """用页面自己发过的请求当模板，改掉业务字段重发一次。拿不到返回 None。

    `tmpl` 按 kind 存（location / collection / declare），别按接口路径存 ——
    两处键必须一致，否则「拦到了却查不到」，表现和「没拦到」一模一样。
    """
    t = tmpl.get(kind)
    if not t:
        _trace(f"replay-{kind}", {"why": "没拦到模板", "have": list(tmpl)})
        return None
    try:
        body = json.loads(t.get("post") or "{}")
    except Exception:  # noqa: BLE001
        body = {}
    if not body:
        _trace(f"replay-{kind}", {"why": "模板没有请求体", "url": t.get("url")})
        return None
    for k, v in patch.items():
        body[k] = q if v == "{q}" else v
    hdrs = {k: v for k, v in (t.get("hdrs") or {}).items()
            if k.lower() in _HDR_REPLAY}
    try:
        resp = page.request.post(t["url"], headers=hdrs, data=json.dumps(body))
        j = resp.json()
    except Exception as exc:  # noqa: BLE001
        _trace(f"replay-{kind}", {"why": "重放失败",
                                  "err": f"{type(exc).__name__}: {exc}"[:300],
                                  "url": t.get("url"), "body": body, "hdrs": hdrs})
        return None
    if j.get("errCode") not in (0, None):
        _trace(f"replay-{kind}", {"why": "接口拒绝", "resp": j})
        return None
    return j.get("data") or {}


# ── 页面上的三个小动作，一律走 evaluate ───────────────────────────────────
# 不用 Playwright 的 locator：发表页在 iframe 里，locator 会数到 0 而
# document.querySelector 能拿到（2026-11-13 反复实测）。evaluate 这条路
# 和探针完全一致，探针能读到的东西，这里就能点到。
_CLICK_JS = """([sel, ms]) => new Promise((res) => {
  const t0 = Date.now();
  const tick = () => {
    const e = document.querySelector(sel);
    if (e && (e.offsetWidth || e.offsetHeight)) { e.click(); res(true); return; }
    if (Date.now() - t0 > ms) { res(false); return; }
    setTimeout(tick, 200);
  };
  tick();
})"""

_CLICK_TEXT_JS = """([txt, ms]) => new Promise((res) => {
  const t0 = Date.now();
  const tick = () => {
    const all = [...document.querySelectorAll('div,span,button,li,label')];
    const hit = all.find((x) => (x.innerText || '').trim() === txt)
             || all.find((x) => (x.innerText || '').trim().includes(txt)
                          && x.children.length <= 2);
    if (hit && (hit.offsetWidth || hit.offsetHeight)) { hit.click(); res(true); return; }
    if (Date.now() - t0 > ms) { res(false); return; }
    setTimeout(tick, 200);
  };
  tick();
})"""

_FILL_JS = """([sel, text]) => {
  const e = document.querySelector(sel);
  if (!e) return false;
  e.focus();
  const proto = e instanceof HTMLTextAreaElement
    ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value');
  if (setter && setter.set) setter.set.call(e, text); else e.value = text;
  e.dispatchEvent(new Event('input', { bubbles: true }));
  e.dispatchEvent(new Event('change', { bubbles: true }));
  e.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'a' }));
  return true;
}"""


# 真实鼠标点击用：先滚到可视区，再量矩形（都按「选择器优先、文字兜底」找元素）
_SCROLL_JS = """([sel, txt]) => {
  let e = sel ? document.querySelector(sel) : null;
  if (!e && txt) {
    const all = document.querySelectorAll('div,span,button,a,li,p');
    for (const n of all) {
      if ((n.textContent || '').trim() === txt) { e = n; break; }
    }
  }
  if (e) e.scrollIntoView({ block: 'center' });
  return !!e;
}"""

_RECT_JS = """([sel, txt]) => {
  let e = sel ? document.querySelector(sel) : null;
  if (!e && txt) {
    const all = document.querySelectorAll('div,span,button,a,li,p');
    for (const n of all) {
      if ((n.textContent || '').trim() === txt) { e = n; break; }
    }
  }
  if (!e) return null;
  const r = e.getBoundingClientRect();
  if (r.width < 1 || r.height < 1) return null;
  return { x: r.x, y: r.y, w: r.width, h: r.height };
}"""


def _js_click(form, sel: str, ms: int = 15000) -> bool:
    try:
        return bool(form.evaluate(_CLICK_JS, [sel, ms]))
    except Exception:  # noqa: BLE001
        return False


def _js_click_text(form, txt: str, ms: int = 8000) -> bool:
    try:
        return bool(form.evaluate(_CLICK_TEXT_JS, [txt, ms]))
    except Exception:  # noqa: BLE001
        return False


def _js_fill(form, sel: str, text: str) -> bool:
    try:
        return bool(form.evaluate(_FILL_JS, [sel, text]))
    except Exception:  # noqa: BLE001
        return False


def _mouse_click(page, form, sel: str = "", txt: str = "") -> bool:
    """在表单 iframe 里做一次**真实**鼠标点击。

    为什么必须真实点：链接那一行是 Vue + weui 组件，`el.click()` 派发的合成
    事件它不认 —— 点了不报错、不展开、也不发请求，看着像「这个号没有数据」。
    做法是先在 iframe 里滚到可视区并量出矩形，加上 iframe 在主文档里的偏移，
    再用 page.mouse 点。2026-11-13 用这个办法才把剧集接口逼出来。
    """
    try:
        form.evaluate(_SCROLL_JS, [sel, txt])
        page.wait_for_timeout(350)
        rect = form.evaluate(_RECT_JS, [sel, txt])
        if not rect:
            return False
        try:
            box = form.frame_element().bounding_box() or {"x": 0, "y": 0}
        except Exception:  # noqa: BLE001
            box = {"x": 0, "y": 0}
        page.mouse.click(box["x"] + rect["x"] + rect["w"] / 2,
                         box["y"] + rect["y"] + rect["h"] / 2)
        return True
    except Exception:  # noqa: BLE001
        return False


def open_post_form(page, timeout: float = 120.0):
    """把发表页推进到「能填表单」的状态，返回装着表单的那一帧。

    ### 为什么必须点一下「发表视频」
    2026-11-13 实测：直接 goto 发表页，页面会停在**两帧都不是表单**的状态：
    - `micro/content/post/create` 帧：停在「We're sorry but finder-helper-web
      doesn't work properly without JavaScript enabled」的引导页上
    - `micro/content/iframe/post-card.html` 帧：是「最近视频 / 最近图文 /
      发表视频」这个**素材选择器**

    只有点了那个「发表视频」按钮，`post/create` 帧才会换成真表单
    （实测关键词命中 7 个、input[type=file] 出现 1 个）。
    不点就去找帧 = 永远在引导页里挑选择器 = 每个都数到 0，
    现象是「页面打开了但什么都点不动」。

    ### 返回
    找到表单帧就返回它；超时返回 None。
    """
    import time
    page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=60000)

    end = time.time() + timeout
    clicked = False
    while time.time() < end:
        # 已经渲染出表单就直接用
        fr = form_frame(page, timeout=1)
        if fr is not None:
            return fr
        # 没点过就去点「发表视频」（点一次就够）
        if not clicked:
            clicked = _click_publish_entry(page)
            if clicked:
                page.wait_for_timeout(2500)
                continue
        page.wait_for_timeout(1000)
    return None


def _wait_tmpl(page, tmpl: dict, key: str, seconds: float = 8.0) -> bool:
    """等页面自己把某支 CGI 的请求模板发出来。

    这里必须用 `page.wait_for_timeout` 让出事件循环，不能 `time.sleep`：
    sync 模式下 response 回调和当前代码在同一个线程，sleep 会把事件分发
    堵死，模板永远到不了（现象就是「等了半天还是没有」）。
    """
    import time
    end = time.time() + seconds
    while time.time() < end:
        if key in tmpl:
            return True
        page.wait_for_timeout(400)
    return key in tmpl


def _click_publish_entry(page) -> bool:
    """在「最近视频」那个弹层里真点「发表视频」。

    用真实鼠标点（Vue + weui 不认合成 click）。找的是文字恰好等于
    「发表视频」的最深那个元素（按钮本体，不是包着它的容器）。
    """
    for fr in page.frames:
        try:
            hit = fr.evaluate("""() => {
              const all = Array.from(document.querySelectorAll('div,span,button,a'));
              const cands = all.filter(e => {
                const t = ((e.textContent||'')+'').replace(/\\s+/g,'').trim();
                if (t !== '发表视频') return false;
                const r = e.getBoundingClientRect();
                return r.width > 1 && r.height > 1;
              });
              if (!cands.length) return null;
              const e = cands[cands.length - 1];
              const r = e.getBoundingClientRect();
              return {x: r.x, y: r.y, w: r.width, h: r.height};
            }""")
        except Exception:  # noqa: BLE001
            continue
        if not hit:
            continue
        try:
            box = fr.frame_element().bounding_box() or {"x": 0, "y": 0}
            page.mouse.click(box["x"] + hit["x"] + hit["w"] / 2,
                             box["y"] + hit["y"] + hit["h"] / 2)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _api_items(kind: str, data: dict) -> list[dict]:
    """把接口返回摊平成 [{v, cn, ...}]。字段名按 2026-11-13 实测的真实返回写。"""
    out: list[dict] = []
    if kind == "location":
        for x in data.get("list") or []:
            out.append({"v": x.get("uid") or x.get("poiCheckSum") or x.get("name") or "",
                        "cn": x.get("name") or "",
                        "addr": x.get("fullAddress") or x.get("address") or "",
                        "city": x.get("city") or "",
                        "poi": x.get("poiCheckSum") or ""})
    elif kind == "collection":
        for x in data.get("collectionList") or []:
            out.append({"v": str(x.get("collectionId") or x.get("id")
                                 or x.get("feedId") or ""),
                        "cn": x.get("name") or x.get("collectionName")
                        or x.get("title") or ""})
    elif kind == "series":
        # search_drama_component 的返回：data.list[] 每项
        # id / name / coverUrl / mediaCount（多少集）/ usable / unusableReason
        for x in data.get("list") or []:
            if not x.get("name"):
                continue
            out.append({"v": x.get("id") or "",
                        "cn": x.get("name") or "",
                        "count": int(x.get("mediaCount") or 0),
                        "cover": x.get("coverUrl") or "",
                        "usable": x.get("usable") is not False,
                        "why": x.get("unusableReason") or ""})
    elif kind == "declare":
        for x in data.get("tags") or []:
            tt = str(x.get("tagType") if x.get("tagType") is not None else "")
            # tagType 0 就是「无需标注」，和库里「空 = 不声明」对齐
            out.append({"v": "" if tt in ("0", "None") else tt,
                        "cn": x.get("tagName") or "",
                        "preview": x.get("previewTagName") or ""})
    return [x for x in out if x["cn"]]


def _real_kind(kind: str, link_kind: str) -> str:
    """把前端的 kind 映射成内部接口 key。

    前端「扩展链接」只传 `kind=link`，具体是剧集还是文章看 `link_kind`。
    剧集有自己独立的分页接口，**不是合集的一种**，必须换 key 才会走对重放。
    """
    if kind == "link" and (link_kind or "series") == "series":
        return "series"
    return kind


def _prime_series(page, form, tmpl: dict, tries: int = 2) -> bool:
    """点开「链接 → 视频号剧集 → 选择需要添加的视频号剧集」，把接口模板逼出来。

    为什么要这么绕：`search_drama_component` 只在弹层被真正点开时才发，页面
    初始化不会自己发，所以「等页面加载完抓模板」这条老路在这里拿不到东西。
    必须用真实鼠标逐层点下去（合成事件 weui 不认），才会看到那个请求。

    返回是否抓到了模板。抓不到时调用方必须报「重试」，**不能**当成「这个号
    没有剧集」—— 两者长得一样，但一个是网络/时序，一个是事实。
    """
    for _ in range(max(1, tries)):
        if "series" in tmpl:
            return True
        _mouse_click(page, form, sel=".post-link-wrap")
        page.wait_for_timeout(1200)
        _mouse_click(page, form, txt="视频号剧集")
        page.wait_for_timeout(1500)
        _mouse_click(page, form, txt="选择需要添加的视频号剧集")
        page.wait_for_timeout(2800)
        if "series" in tmpl:
            return True
        # 收了再试一轮：第一次可能是下拉还开着，点击把类型关掉了
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(600)
        except Exception:  # noqa: BLE001
            pass
    return "series" in tmpl


def options(session, kind: str, q: str = "", link_kind: str = "series",
            visible: bool = False) -> dict:
    # 候选拉取是产品路径，默认无头：用户选完号不该看见一个浏览器窗口弹出来。
    # 只有开发期要盯页面时才显式传 visible=True（见下方 act 探针）。
    """从一个**已登录**的号里拉发布页的候选列表（合集 / 位置 / 活动 / 剧集 / 标注）。

    为什么一定要走真页面：视频号的位置是「搜索候选」、合集和剧集是号里自己的 id，
    手打文字既填不进去、也核对不了。所有同类工具都是这么做的。
    """
    # 前端「扩展链接」传的是 kind=link + link_kind=系列名。剧集有自己的接口，
    # 不是合集的一种，所以这里换成内部 key 去走重放；返回和 PICKERS 仍按 link 走。
    real_kind = _real_kind(kind, link_kind)
    cfg = PICKERS.get(kind)
    if not cfg and real_kind not in API_REPLAY:
        raise WxPubError("不认识这个选项类型")
    if kind == "location" and not (q or "").strip():
        return {"items": [], "need_query": True,
                "note": "输入地点名（比如「石牌」），从候选里选。"}

    def _go(page):
        tmpl: dict = {}

        def _on_resp(resp):
            collect_tmpl(resp, tmpl)

        # page 是长期复用的，上一轮挂的 response 监听器不先清掉会一层层累积
        try:
            page.remove_all_listeners("response")
        except Exception:  # noqa: BLE001
            pass
        page.on("response", _on_resp)
        # 裸 goto 会停在「最近视频」弹层 + JS 引导页（open_post_form 里有实测
        # 记录）：那两帧根本不发候选 CGI，等再久 `tmpl` 也是空的 —— 这正是
        # 「剧集/标注一直拉不到」的根因。必须先真实点一下「发表视频」。
        form = open_post_form(page, timeout=60)
        if form is None:
            return {"items": [], "kind": kind, "q": q, "via": "none",
                    "note": "这次没打开发表页，再点一次重试；连续失败就先去"
                            "视频号助手网页版看一眼能不能发表。"}
        # 视频标注那支是表单初始化自己发的，给它一点时间落到 tmpl
        if real_kind in API_REPLAY:
            _wait_tmpl(page, tmpl, "declare", 6.0)

        # ① 接口优先
        if real_kind in API_REPLAY:
            primed = True
            if real_kind == "series":
                primed = _prime_series(page, form, tmpl)
            _trace(f"tmpl-{real_kind}", {k: {"url": v.get("url"), "post": v.get("post")}
                                         for k, v in tmpl.items()})
            patch = API_REPLAY[real_kind][1]
            data = _replay(page, tmpl, real_kind, patch, q=(q or "").strip())
            if data is not None:
                items = _api_items(real_kind, data)
                if items:
                    return {"items": items, "note": "", "kind": kind, "q": q,
                            "via": "api"}
                return {"items": [], "note": (cfg or {}).get("empty")
                        or "这个号里没有可选项。", "kind": kind, "q": q, "via": "api"}
            if real_kind == "series":
                # 模板没抓到 = 那几层没点开，和设备上「没有剧集」完全是两件事。
                # 这里必须说实话，否则用户会以为号里真的空了（2026-11-13 踩过）。
                return {"items": [], "kind": kind, "q": q, "via": "none",
                        "note": "没读到剧集列表，再点一次重试。"
                                if not primed else "读到剧集列表但没拿到数据，再点一次重试。"}

        # ② 兜底：点开那一行，读弹出来的列表
        if not cfg:
            # 只有接口的项（视频标注）没有可点的行，别拿 None 去点
            return {"items": [], "note": "没读到，点一下重试。", "kind": kind}
        # form 由 open_post_form 返回，这里不再重新找一遍帧（找帧要等 30 秒，
        # 而且按 URL 认帧又会挑回引导页）。

        # 为什么不用 locator：真实页面上 `form.locator('.post-link-wrap')` 数到 0，
        # 同一帧 `document.querySelector` 却能拿到（2026-11-13 实测，反复确认过）。
        # 定位引擎在这页上不可靠，所以一律走 evaluate —— 探针也是这么读到的。
        if not _js_click(form, cfg["open"], 20000):
            _trace(f"dom-{kind}", {"err": f"找不到 {cfg['open']}"})
            return {"items": [], "note": "这一项没点开。重试一次；"
                                        "还是没有的话，先去视频号助手网页版看看这项还在不在。"}
        page.wait_for_timeout(1500)

        if kind == "link":
            # 链接先选类型（剧集/文章/红包封面/短剧），再点第二行弹出条目
            name = LINK_KIND_CN.get(link_kind, "视频号剧集")
            _js_click_text(form, name, 6000)
            page.wait_for_timeout(1200)
            for sel in (".link-display-wrap", '[class*="link-select"]', '[class*="add-link"]'):
                if _js_click(form, sel, 4000):
                    page.wait_for_timeout(2000)
                    break

        if kind == "location":
            # 位置是「先输关键词再出候选」，不输入只会看到「不显示位置」
            if not (q or "").strip():
                return {"items": [], "need_query": True,
                        "note": "输入地点名（比如「石牌」），从候选里选。"}
            if not _js_fill(form, cfg["input"], q.strip()):
                return {"items": [], "note": "位置搜索框弹不出来，重试一次。"}
            page.wait_for_timeout(2500)

        page.wait_for_timeout(600)
        raw = form.evaluate(LIST_JS, cfg["sel"])
        items = []
        for it in raw:
            t = it["text"]
            if kind == "collection" and ("创建新合集" in t or "选择合集" in t):
                continue
            if kind == "location" and t.strip() == "不显示位置":
                # 「不显示位置」是取消项，不是候选地址
                items.append({"v": "", "cn": "不显示位置", "city": ""})
                continue
            if kind == "location":
                # 「石牌坊社区 湖南省邵阳市武冈市…」→ 名字 + 地址，城市取「省XX市」那一段
                parts = t.split(" ")
                nm = parts[0]
                addr = " ".join(parts[1:])
                city = ""
                m = re.search(r"([\u4e00-\u9fa5]+市)", addr)
                if m:
                    city = m.group(1)
                items.append({"v": it["id"] or addr or nm, "cn": nm,
                              "addr": addr, "city": city})
                continue
            if kind == "collection":
                items.append({"v": it["id"] or t, "cn": t})
                continue
            if kind == "activity":
                if t.strip() == "不参与活动":
                    continue
                items.append({"v": it["id"] or t, "cn": t})
                continue
            items.append({"v": it["id"] or t, "cn": t})

        # 关掉下拉，别把页面留在半开状态（下次调用还要用同一个页面）
        try:
            page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass

        return {"items": items, "note": "" if items else cfg["empty"],
                "kind": kind, "q": q}


    return session.run(_go, timeout=150, visible=visible)


def options_batch(session, visible: bool = False) -> dict:
    """选好号之后，一次开页把「这个号能自动读到的候选」全拉回来。

    为什么不是让前端连发几个 `/api/publish/options`：session 的 page 是长期
    复用的，同一个号的所有操作都排在一个线程里。连发 3 个会各开一次页面、
    串行排队，光 goto + 等 8 秒就要 24 秒，还会互相踩「弹层正在点」的状态。
    一次开页把模板收齐、分别重放，只付一次页面钱。

    **位置不在这里**：平台要输入地名才给候选，没关键词时预取不到任何东西，
    只能等用户输词时再搜 —— 所以前端那边位置始终保持一个搜索框。
    """
    def _go(page):
        tmpl: dict = {}

        def _on_resp(resp):
            try:
                u = resp.url or ""
                for k, (path, _p) in API_REPLAY.items():
                    if path in u and k not in tmpl:
                        rq = resp.request
                        try:
                            post = rq.post_data or ""
                        except Exception:  # noqa: BLE001
                            post = ""
                        tmpl[k] = {"url": u, "hdrs": dict(rq.headers or {}),
                                   "post": post}
                        return
            except Exception:  # noqa: BLE001  记不到就算了
                pass

        page.on("response", _on_resp)
        try:
            # 同 options()：裸 goto 停在引导页，候选 CGI 一支都不会发。
            # 真实点一次「发表视频」把表单帧推出来，才谈得上拦模板。
            form = open_post_form(page, timeout=60)
            if form is None:
                note = "这次没打开发表页，点字段里的按钮重试一次。"
                return {
                    "declare": {"items": [], "kind": "declare", "via": "none", "note": note},
                    "link": {"items": [], "kind": "link", "via": "none", "note": note},
                    "location": {"items": [], "need_query": True,
                                 "note": "输入地名，从平台候选里选"},
                }
            # 表单初始化会自己发视频标注那支，等模板落到 tmpl 再重放
            _wait_tmpl(page, tmpl, "declare", 8.0)
            out: dict = {}

            # ① 视频标注：页面初始化自己就会发，等 8 秒模板必到
            d = _replay(page, tmpl, "declare", API_REPLAY["declare"][1])
            ditems = _api_items("declare", d) if d is not None else []
            out["declare"] = {
                "items": ditems, "kind": "declare",
                "via": "api" if d is not None else "none",
                "note": "" if ditems else "没读到标注，点一下重试。",
            }

            # ② 剧集：只在弹层被真实点开时才发请求（Vue + weui 不认合成 click）
            primed = _prime_series(page, form, tmpl)
            if not primed:
                # 没抓到模板 ≠ 这个号没有剧集，绝不写成「还没有剧集」
                out["link"] = {"items": [], "kind": "link", "via": "none",
                               "note": "没读到剧集，点一下重试。"}
            else:
                sdata = _replay(page, tmpl, "series", API_REPLAY["series"][1])
                sitems = _api_items("series", sdata) if sdata is not None else []
                out["link"] = {
                    "items": sitems, "kind": "link",
                    "via": "api" if sdata is not None else "none",
                    "note": "" if sitems else (
                        "平台只列已上架的剧集；这个号暂无可选剧集。"
                        if sdata is not None else "读到剧集列表但没拿到数据，点一下重试。"),
                }

            # ③ 位置：没有关键词就是没有候选，前端保持搜索框
            out["location"] = {"items": [], "need_query": True,
                               "note": "输入地名，从平台候选里选"}
            return out
        finally:
            try:
                page.remove_listener("response", _on_resp)
            except Exception:  # noqa: BLE001
                pass

    return session.run(_go, timeout=150, visible=visible)


# ── 可脚本化的探针（开发期校准选择器用）──────────────────────────────────
def act(session, steps: list[dict], url: str = CREATE_URL, net: bool = False,
         visible: bool = True) -> dict:
    """按步骤操作页面并导出 DOM。

    步骤写法（按顺序执行，任何一步失败只记错不中断）：
      {"wait": 800}                        等毫秒
      {"click": "css 选择器"}               点元素（优先）
      {"click_text": "选择链接"}            按文字点
      {"type": {"sel": ".xx", "text": "石牌"}}  往输入框里逐字输入（触发联想）
      {"upload": {"sel": "input[type=file]", "path": "D:/x.mp4", "frame"?}}
                                         往（隐藏的）file input 塞本地文件
      {"chooser": {"text": "支持上传", "path": "D:/x.mp4", "frame"?}}
                                         真人路径：点上传区接住系统文件框再塞文件
      {"cdp_upload": {"path": "D:/x.mp4", "frame"?}}
                                         探针路径（实测不通）：主页面 CDP 改不到这个
                                         frame 的 input.files；且该 frame 没有独立 CDP
                                         会话，「跨进程 iframe」的假设本身就是错的
      {"js_chooser": {"path": "D:/x.mp4", "frame"?}}
                                         在 frame 的 JS 里直接 input.click()，接住系统文件框
      {"fl_upload": {"iframe": "iframe[src*='post/create']", "path": "D:/x.mp4"}}
                                         frame_locator 穿透 iframe（探针路径，实测不通）
      {"js_upload": {"path": "D:/x.mp4", "frame"?}}
                                         ★ 唯一实测跑通的路径：在表单 frame 的 JS 里
                                         用 DataTransfer 造 File 塞进 input（≤8MB）
                                         前提：视频必须合规（有音轨、正常画面），
                                         黑屏无音轨会被平台前端静默拒绝，别误判成通道不通
      {"press": "Enter"}
      {"eval": {"name","js","frame"?}}      在某个 frame 里跑一段 JS 取值
      {"real_click": {"sel"?,"text"?,"frame"?}}  真实鼠标点击（换算 iframe 偏移）
      {"dump": "名字"}                      导出所有 frame 的 DOM
      {"shot": "名字"}                      截图

    为什么要有 `real_click`：发表页的下拉是 Vue + weui，`el.click()` 派发的
    合成事件它不认（点了没反应，也不报错）。只有 Chrome 真实鼠标事件能展开，
    所以要先在 frame 里量出元素矩形，加上 iframe 偏移，用 `page.mouse.click` 点。

    为什么要「所有 frame」：弹窗既可能开在表单 iframe 里，也可能挂到主文档的
    portal 上（微信的 weui 组件两种都有）。只 dump 一个 frame 会空手而归。
    `frame` 给字符串时按 url 包含匹配 —— 发表表单本身在 `/micro/content/post/create`。
    """
    out: dict = {"steps": [], "dumps": {}, "shots": {}, "errors": [], "net": []}

    def _go(page):
        if net:
            def _on_resp(resp):
                try:
                    u = resp.url or ""
                    # 上传请求可能打到独立 CDN（URL 里没有 cgi-bin），
                    # 先留一份全量 URL 便于判断「到底发没发上传」。
                    if len(out.setdefault("net_urls", [])) < 400:
                        out["net_urls"].append({"m": resp.request.method, "url": u[:220]})
                    if "cgi-bin" not in u:
                        return
                    ct = (resp.headers or {}).get("content-type", "")
                    if "json" not in ct:
                        return
                    out["net"].append({"url": u[:220], "m": resp.request.method,
                                       "post": (resp.request.post_data or "")[:600],
                                       "hdrs": _pick_headers(resp.request.headers),
                                       "data": resp.json()})
                except Exception:  # noqa: BLE001
                    pass
            page.on("response", _on_resp)

        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)

        def _frame(spec):
            if not spec:
                return page.main_frame
            for fr in page.frames:
                if spec in (fr.url or ""):
                    return fr
            return page.main_frame

        for i, st in enumerate(steps):
            try:
                if "wait" in st:
                    page.wait_for_timeout(int(st["wait"]))
                elif "open_form" in st:
                    # 裸 goto 停在「最近视频」弹层 + JS 引导页（open_post_form
                    # 里有实测），必须真点一次「发表视频」表单帧才会出来。
                    fr = open_post_form(page)
                    out.setdefault("evals", {})["open_form"] = {"found": fr is not None}
                elif "set_schedule" in st:
                    # 探针专用：直接走生产函数设定时时间（只填表单，绝不点发表），
                    # 配合后面的 vue_state eval 能看到组件内部真正存的值。
                    c = st["set_schedule"]
                    at = datetime.datetime.strptime(c["at"], "%Y-%m-%d %H:%M")
                    from . import wxpost  # 局部导入：wxpost 反过来要用本模块，顶部导入会循环
                    fr = _frame(c.get("frame"))
                    res = wxpost.set_schedule(page, fr, at)
                    out.setdefault("evals", {})["set_schedule"] = res
                elif "click" in st:
                    page.locator(st["click"]).first.click(timeout=10000)
                elif "click_text" in st:
                    page.get_by_text(st["click_text"], exact=False).first.click(timeout=10000)
                elif "type" in st:
                    c = st["type"]
                    page.locator(c["sel"]).first.click(timeout=8000)
                    page.locator(c["sel"]).first.type(c["text"], delay=90, timeout=15000)
                elif "upload" in st:
                    c = st["upload"]
                    fr = _frame(c.get("frame"))
                    sel = c.get("sel") or "input[type=file]"
                    path = c["path"]
                    if not os.path.isfile(path):
                        raise RuntimeError(f"本地文件不存在：{path}")
                    loc = fr.locator(sel)
                    if loc.count():
                        loc.first.set_input_files(path, timeout=30000)
                        how = "locator"
                    else:
                        # 表单有些版本把 input 挂在 shadow root 里，locator 数到 0；
                        # 直接在 frame 的 JS 上下文里递回找，再转成 ElementHandle。
                        h = fr.evaluate_handle("""() => {
                          const find = (root) => {
                            const hit = root.querySelector('input[type=file]');
                            if (hit) return hit;
                            for (const e of root.querySelectorAll('*')) {
                              if (e.shadowRoot) { const r = find(e.shadowRoot); if (r) return r; }
                            }
                            return null;
                          };
                          return find(document);
                        }""")
                        el = h.as_element()
                        if el is None:
                            raise RuntimeError("页面里找不到文件上传控件")
                        el.set_input_files(path, timeout=30000)
                        how = "shadow-handle"
                    out.setdefault("evals", {})[f"upload{i}"] = how
                elif "chooser" in st:
                    c = st["chooser"]
                    fr = _frame(c.get("frame"))
                    path = c["path"]
                    if not os.path.isfile(path):
                        raise RuntimeError(f"本地文件不存在：{path}")
                    with page.expect_file_chooser(
                            timeout=int(c.get("timeout") or 20000)) as fc:
                        if c.get("sel"):
                            fr.locator(c["sel"]).first.click(timeout=10000)
                        elif not _mouse_click(page, fr, "", c.get("text") or "支持上传"):
                            raise RuntimeError("没找到可以点开的上传区")
                    fc.value.set_files(path, timeout=30000)
                    out.setdefault("evals", {})[f"chooser{i}"] = "ok"
                elif "cdp_upload" in st:
                    c = st["cdp_upload"]
                    path = c["path"]
                    if not os.path.isfile(path):
                        raise RuntimeError(f"本地文件不存在：{path}")
                    # 探针路径，实测不通：这个表单 frame 并没有独立 CDP 会话
                    # （new_cdp_session(frame) 会报「属于 parent frame」），挂主 page
                    # 的会话改不到它的 input.files（回读 files=0、平台不发上传请求）。
                    # 保留这段只为回归时对照，别再把它当成「跨进程 iframe 硬阻塞」。
                    fr = _frame(c.get("frame"))
                    try:
                        cdp = page.context.new_cdp_session(fr)
                        via = "frame"
                    except Exception as exc:  # noqa: BLE001
                        cdp = page.context.new_cdp_session(page)
                        via = f"page-fallback({type(exc).__name__}: {str(exc)[:110]})"
                    try:
                        doc = cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})

                        def _find(node):
                            if (node.get("nodeName") or "").lower() == "input":
                                a = node.get("attributes") or []
                                attrs = {a[k]: a[k + 1] for k in range(0, len(a) - 1, 2)}
                                if (attrs.get("type") or "").lower() == "file":
                                    return node.get("nodeId")
                            for key in ("children", "contentDocument",
                                        "shadowRoots", "pseudoElements"):
                                v = node.get(key)
                                for ch in (v if isinstance(v, list) else [v] if v else []):
                                    if isinstance(ch, dict):
                                        found = _find(ch)
                                        if found:
                                            return found
                            return None

                        nid = _find(doc["root"])
                        if not nid:
                            raise RuntimeError("CDP 没在页面里找到文件上传控件")
                        cdp.send("DOM.setFileInputFiles", {"files": [path], "nodeId": nid})
                    finally:
                        cdp.detach()
                    # CDP 只改 files，不派发事件；antd(React) 的 onChange 其实监听 input。
                    # input / change 都派一次，并回读 files.length 确认文件真塞进去了。
                    res = _frame(c.get("frame")).evaluate("""() => {
                      const inp = document.querySelector('input[type=file]');
                      const n = inp && inp.files ? inp.files.length : -1;
                      if (inp) {
                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                      }
                      return {found: !!inp, files: n};
                    }""")
                    out.setdefault("evals", {})[f"cdp{i}"] = {
                        "via": via, **(res if isinstance(res, dict) else {"raw": res})}
                elif "js_chooser" in st:
                    # 绕开所有 locator：直接在 frame 的 JS 上下文里让 input 自己 click，
                    # 浏览器会弹系统文件框，page 级的 filechooser 事件能接住（含 iframe）。
                    c = st["js_chooser"]
                    path = c["path"]
                    if not os.path.isfile(path):
                        raise RuntimeError(f"本地文件不存在：{path}")
                    fr = _frame(c.get("frame"))
                    with page.expect_file_chooser(
                            timeout=int(c.get("timeout") or 20000)) as fc:
                        fr.evaluate("""() => {
                          const inp = document.querySelector('input[type=file]');
                          if (!inp) throw new Error('页面里没有 file input');
                          inp.click();
                          return true;
                        }""")
                    fc.value.set_files(path, timeout=30000)
                    # 关键判据：input 到底有没有真的拿到文件（js_chooser 实测拿到 0）。
                    # 塞不进文件时先怀疑视频本身不合规，别急着下「通道不通」的结论。
                    out.setdefault("evals", {})[f"jschooser{i}"] = fr.evaluate("""() => {
                      const i = document.querySelector('input[type=file]');
                      const f = i && i.files && i.files[0];
                      return {files: i && i.files ? i.files.length : -1,
                              name: f ? f.name : '', size: f ? f.size : 0};
                    }""")
                elif "fl_upload" in st:
                    # frame_locator 走的是 selector engine 穿透 iframe，和
                    # page.frames 里拿到的 Frame.locator() 不是同一套实现。
                    c = st["fl_upload"]
                    path = c["path"]
                    if not os.path.isfile(path):
                        raise RuntimeError(f"本地文件不存在：{path}")
                    fl = page.frame_locator(c.get("iframe") or "iframe")
                    n = fl.locator("input[type=file]").count()
                    fl.locator("input[type=file]").first.set_input_files(
                        path, timeout=30000)
                    out.setdefault("evals", {})[f"fl{i}"] = {
                        "count": n, "via": "frame_locator"}
                elif "js_upload" in st:
                    # ★ 唯一实测跑通的路径：绕开 Playwright 的文件注入，直接在表单所在
                    # frame 的 JS 上下文里用 DataTransfer 造 File、赋 input.files、派发
                    # input + change。平台会真发分片上传（applyuploaddfs / uploadpartdfs）。
                    # 探针 cover_steps14.json 已验证「上传视频 → 编辑封面 → 封面已更新」。
                    # 两个前提：① 只适合 ≤8MB 小文件（base64 走 evaluate 参数），大文件
                    # 要另找通道；② 视频必须合规，1 秒黑屏无音轨会被前端静默拒绝——
                    # 早先把这误判成「跨进程 iframe 塞不进文件」，实际是样本问题。
                    # 这里仍是探针 step，不是生产函数；接入队列前按铁律另出方案。
                    import base64

                    c = st["js_upload"]
                    path = c["path"]
                    if not os.path.isfile(path):
                        raise RuntimeError(f"本地文件不存在：{path}")
                    size = os.path.getsize(path)
                    if size > 8 * 1024 * 1024:
                        raise RuntimeError("js_upload 只用于 ≤8MB 的小文件可行性验证")
                    with open(path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode("ascii")
                    fr = _frame(c.get("frame"))
                    out.setdefault("evals", {})[f"jsupload{i}"] = fr.evaluate("""(o) => {
                      const bin = atob(o.b64);
                      const arr = new Uint8Array(bin.length);
                      for (let k = 0; k < bin.length; k++) arr[k] = bin.charCodeAt(k);
                      const file = new File([arr], o.name, {type: o.type});
                      const dt = new DataTransfer();
                      dt.items.add(file);
                      const inp = document.querySelector(o.sel);
                      if (!inp) return {err: 'no file input: ' + o.sel};
                      inp.files = dt.files;
                      inp.dispatchEvent(new Event('input', {bubbles: true}));
                      inp.dispatchEvent(new Event('change', {bubbles: true}));
                      return {files: inp.files.length,
                              size: inp.files[0] ? inp.files[0].size : 0,
                              name: inp.files[0] ? inp.files[0].name : ''};
                    }""", {"b64": b64, "name": os.path.basename(path),
                           "sel": c.get("sel") or "input[type=file]",
                           "type": c.get("type") or "video/mp4"})
                elif "press" in st:
                    page.keyboard.press(st["press"])
                elif "real_click" in st:
                    c = st["real_click"]
                    sel, txt = c.get("sel") or "", c.get("text") or ""
                    if not _mouse_click(page, _frame(c.get("frame")), sel, txt):
                        raise RuntimeError(f"没找到元素 sel={sel!r} text={txt!r}")
                    out.setdefault("evals", {})[f"click{i}"] = "ok"
                elif "eval" in st:
                    c = st["eval"]
                    arg = c.get("arg")
                    js = c["js"]
                    if arg is None:
                        val = _frame(c.get("frame")).evaluate(js)
                    else:
                        val = _frame(c.get("frame")).evaluate(js, arg)
                    out.setdefault("evals", {})[c.get("name") or f"e{i}"] = val
                elif "dump" in st:
                    name = st["dump"]
                    got = {}
                    for fr in page.frames:
                        try:
                            dom = fr.evaluate(DUMP_JS)
                        except Exception as exc:  # noqa: BLE001
                            got[(fr.url or "?")[-40:]] = {"err": type(exc).__name__}
                            continue
                        if (dom.get("fields") or dom.get("buttons")
                                or dom.get("blocks") or dom.get("radios")):
                            got[(fr.url or "?")[-40:]] = dom
                    out["dumps"][name] = got
                elif "shot" in st:
                    name = st["shot"]
                    p = os.path.join(_diag_dir(), f"act-{name}.png")
                    page.screenshot(path=p, full_page=True)
                    out["shots"][name] = p
            except Exception as exc:  # noqa: BLE001
                out["errors"].append(f"step{i} {list(st)[0]}: "
                                     f"{type(exc).__name__}: {str(exc)[:120]}")
        return out

    return session.run(_go, timeout=300, visible=visible)
