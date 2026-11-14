"""视频号作品删除 —— 不可逆，默认只定位不点任何删除按钮。

为什么单独一个文件：删除是发布运营里唯一会把平台上已有内容弄没的动作，
判据和别的只读接口完全不同，混进 wxpub（候选/发表）容易被人顺手复用。

硬约束（2026-11-13 实测）：
  · 作品列表其实是 `micro/content/iframe/post-card.html` 这一帧；
    `micro/content/post/list` 停在 JS 引导页，里面数不到任何条目。
  · 列表里的「删除」按钮是 Vue + weui，合成 click 不认，必须 page.mouse 真点。
  · 点完还会弹一层确认，上次四次失败都停在这一步；确认也要真点。
  · 删完必须回读列表（刷新后再读一次），标题还在就一律不算成功。

### ⚠ 2026-11-13 真号验证未通过（功能暂不可用）
定位（dry_run）是准的：能精确认出「只含这一条 + 一个删除」的容器。
但真点删除没生效，三次尝试都没打通：
  ① 真鼠标点「删除」→ 无弹层、无原生 confirm、无删除 CGI；
  ② 改成只在弹层容器里找确认按钮（避免误点别的条目）→ 仍无弹层；
  ③ 显式 hover + 坐标诊断 → 主文档命中 `WUJIE-APP.wujie_iframe`，
     frame 内同一坐标才命中 `DIV.action-content`。这是 wujie 微前端的
     iframe 坐标换算问题，真实鼠标事件进不了那一层。
已试过：真点 / hover / 合成 click / 原生 confirm accept（上一轮四种）。
**结论**：不要把「删除」当作已上线。接口默认 dry_run，前端按钮点下去会如实
报「没删掉」并保留手动入口；要打通需另立攻坚（wujie iframe 内派发完整
PointerEvent 序列，或抓平台删除 CGI 模板后重放），且必须真号复验。
"""
from __future__ import annotations

from . import wxpub

FRAME = "micro/content/iframe/post-card.html"

# 找目标条目：标题精确优先、编号兜底；只认「容器内恰好一个删除按钮」的那层，
# 串到别的条目就停手 —— 宁可删不了，绝不误删用户的其它作品。
FIND_JS = """({key, pid}) => {
  const norm = s => ((s || '') + '').replace(/\\s+/g, '').trim();
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 1 && r.height > 1; };
  const all = Array.from(document.querySelectorAll('div,span,a,button,p,li'));
  let titles = [];
  if (key) titles = all.filter(e => vis(e) && (e.innerText || '').includes(key));
  if (!titles.length && pid) {
    titles = all.filter(e => vis(e) && (
      (e.getAttribute('data-id') || '').includes(pid) ||
      (e.getAttribute('data-object-id') || '').includes(pid) ||
      (e.id || '').includes(pid)));
  }
  if (!titles.length) {
    return {found: false, reason: '列表里没有这条标题/编号',
            body: ((document.body && document.body.innerText) || '').replace(/\\s+/g,' ').slice(0, 400)};
  }
  const t = titles.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length)[0];
  let item = null;
  let cur = t;
  for (let i = 0; i < 8 && cur; i++) {
    const txt = (cur.innerText || '').replace(/\\s+/g, ' ');
    const n = (txt.match(/删除/g) || []).length;
    if (n === 1 && txt.length < 600) { item = cur; break; }
    if (n > 1) break;               // 串到多条了，不猜
    cur = cur.parentElement;
  }
  if (!item) return {found: false, reason: '找不到只含这一条的容器',
                     title: (t.innerText || '').slice(0, 100)};
  const itemText = (item.innerText || '').replace(/\\s+/g, ' ').slice(0, 300);
  if (key && !itemText.includes(key)) {
    return {found: false, reason: '容器里不是这条作品', itemText: itemText};
  }
  // Vue 按钮常是「外层 a/div + 内层 span」都写着「删除」，所以不能要求恰好
  // 一个元素；容器文本里只有一个「删除」已经保证不会串到别的条目。
  // 取可见、最深的那个（按钮本体），和按文字点选的口径一致。
  const dels = Array.from(item.querySelectorAll('a,button,span,div'))
    .filter(e => vis(e) && norm(e.textContent) === '删除');
  if (!dels.length) {
    return {found: false, reason: '找不到可见的删除按钮', itemText: itemText};
  }
  const e = dels[dels.length - 1];
  e.setAttribute('data-hook-del', '1');
  try { e.scrollIntoView({block: 'center'}); } catch (err) {}
  return {found: true, itemText: itemText, itemCls: (item.className || '').toString().slice(0, 100),
          btn: {tag: e.tagName, cls: (e.className || '').toString().slice(0, 80)}};
}"""

# 点完「删除」后可能弹确认层。**只在弹层容器里找按钮**：
# 列表里别的条目也有「删除」，把「删除」当全局关键词会误点到旁的作品。
CONFIRM_JS = """() => {
  const t = e => ((e.textContent || '') + '').replace(/\\s+/g, '').trim();
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 1 && r.height > 1; };
  const dialogs = Array.from(document.querySelectorAll(
      '[class*=dialog],[class*=modal],[class*=popup],[role=dialog]'))
    .filter(e => vis(e) && /删除/.test(e.innerText || ''));
  const dlg = dialogs[dialogs.length - 1];
  if (!dlg) return {found: false,
                    dialogs: dialogs.map(e => (e.innerText||'').replace(/\\s+/g,' ').slice(0,120))};
  const cands = Array.from(dlg.querySelectorAll('button,a,span,div'))
    .filter(e => vis(e) && !e.hasAttribute('data-hook-del')
            && ['确认','确定','仍要删除','确认删除','删除'].includes(t(e)));
  if (!cands.length) return {found: false,
                             text: (dlg.innerText||'').replace(/\\s+/g,' ').slice(0,200)};
  const e = cands[cands.length - 1];
  e.setAttribute('data-hook-del-confirm', '1');
  return {found: true, text: t(e), tag: e.tagName,
          dialog: (dlg.innerText||'').replace(/\\s+/g,' ').slice(0,200)};
}"""

# 点完之后把每一帧的可见文字 / 弹层 / 按钮摊开，失败时能看出卡在哪一步
DUMP_JS = """() => {
  const t = e => ((e.textContent || '') + '').replace(/\\s+/g, '').trim();
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 1 && r.height > 1; };
  return {
    body: ((document.body && document.body.innerText) || '').replace(/\\s+/g,' ').slice(0, 420),
    dialogs: Array.from(document.querySelectorAll('[class*=dialog],[class*=modal],[class*=popup],[role=dialog]'))
      .filter(vis).map(e => (e.innerText||'').replace(/\\s+/g,' ').slice(0,180)).slice(0,5),
    buttons: Array.from(document.querySelectorAll('button,a,span,div'))
      .filter(e => vis(e) && ['删除','确认','确定','取消','再想想'].includes(t(e)))
      .map(e => ({t: t(e), cls: (e.className||'').toString().slice(0,50)})).slice(0,14),
  };
}"""


_BTN_RECT_JS = """() => {
  const e = document.querySelector('[data-hook-del="1"]');
  if (!e) return null;
  const r = e.getBoundingClientRect();
  return {x: r.x, y: r.y, w: r.width, h: r.height,
          cls: (e.className || '').toString().slice(0, 80)};
}"""

_HIT_JS = """([x, y]) => {
  const e = document.elementFromPoint(x, y);
  return e ? e.tagName + '.' + (e.className || '').toString().slice(0, 70) : null;
}"""


def delete_post(session, post_id: str = "", title: str = "",
                dry_run: bool = True, visible: bool = False) -> dict:
    """在这个号的「最近视频」里删掉一条作品。

    `dry_run=True`（默认）只定位并校验，绝不点删除。真删必须由调用方显式关掉，
    且删完回读列表确认标题消失；对不上就如实说没删掉，不许假装成功。
    """
    key = str(title or "").strip()
    pid = str(post_id or "").strip()
    if not key and not pid:
        raise wxpub.WxPubError("没给作品标题或编号，不会猜要删哪一条")

    def _go(page):
        dialogs: list[str] = []
        reqs: list[str] = []

        def _on_dialog(dlg):
            dialogs.append((dlg.message or "")[:160])
            try:
                dlg.accept()        # 平台若用原生 confirm，默认 dismiss 等于点取消
            except Exception:  # noqa: BLE001
                pass

        def _on_resp(resp):
            u = resp.url or ""
            # 删除请求叫什么还不确定，先全记下来（只留路径尾）
            if "/cgi-bin/" in u or "/api/" in u:
                path = u.split("?")[0].rstrip("/").split("/")[-1]
                reqs.append(path + " | " + u[-60:])

        for ev, cb in (("dialog", _on_dialog), ("response", _on_resp)):
            try:
                page.remove_all_listeners(ev)
            except Exception:  # noqa: BLE001
                pass
            page.on(ev, cb)

        out: dict = {"ok": False, "found": False, "dry_run": bool(dry_run), "steps": []}
        try:
            page.goto(wxpub.LIST_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(9000)
            fr = next((f for f in page.frames if FRAME in (f.url or "")), None)
            if fr is None:
                out["note"] = "没找到作品列表，删不了。打开视频号助手手动删吧。"
                return out

            hit = fr.evaluate(FIND_JS, {"key": key, "pid": pid})
            out["match"] = {k: hit.get(k) for k in ("found", "reason", "itemText")}
            if not hit.get("found"):
                out["note"] = ("在这个号的列表里没找到这条作品（可能已经被删了）。"
                               + str(hit.get("reason") or ""))
                return out
            out["found"] = True
            out["text"] = str(hit.get("itemText") or "")[:200]
            if dry_run:
                out["ok"] = True
                out["note"] = "已定位到这条作品，没有点删除。"
                return out

            # ① 真点列表里的「删除」。这一排操作按钮要 hover 才激活，
            # 先量坐标、看清点击会落在谁身上，再显式移过去停一下。
            diag: dict = {}
            try:
                rect = fr.evaluate(_BTN_RECT_JS)
                box = fr.frame_element().bounding_box() or {"x": 0, "y": 0}
                diag = {"rect": rect, "box": box}
                if rect:
                    px = box["x"] + rect["x"] + rect["w"] / 2
                    py = box["y"] + rect["y"] + rect["h"] / 2
                    diag["hit_main"] = page.evaluate(_HIT_JS, [px, py])
                    diag["hit_frame"] = fr.evaluate(
                        _HIT_JS, [rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2])
                    page.mouse.move(px, py)
                    page.wait_for_timeout(500)
            except Exception as exc:  # noqa: BLE001
                diag["err"] = str(exc)[:200]
            out["click_diag"] = diag
            if not wxpub._mouse_click(page, fr, sel='[data-hook-del="1"]'):
                out["note"] = "删除按钮点不到，已经停手。"
                return out
            out["steps"].append("clicked_delete")
            page.wait_for_timeout(1500)

            # ② 点完先把页面摊开（诊断用），再找确认弹层
            out["after_click"] = []
            for f in page.frames:
                try:
                    info = f.evaluate(DUMP_JS)
                    info["frame"] = (f.url or "")[-70:]
                    out["after_click"].append(info)
                except Exception:  # noqa: BLE001
                    continue
            for f in page.frames:
                try:
                    c = f.evaluate(CONFIRM_JS)
                except Exception:  # noqa: BLE001
                    continue
                if c.get("found"):
                    if wxpub._mouse_click(page, f, sel='[data-hook-del-confirm="1"]'):
                        out["steps"].append("clicked_confirm:" + str(c.get("text") or "")[:20])
                        out["confirm_dialog"] = c.get("dialog") or ""
                    break
            page.wait_for_timeout(2500)

            # ③ 回读：先直接读，再刷新读一次。标题还在就一律不算成功
            def _still():
                f2 = next((f for f in page.frames if FRAME in (f.url or "")), None)
                if f2 is None:
                    return None
                try:
                    body = str(f2.evaluate("() => (document.body.innerText || '')") or "")
                    return key in body if key else None
                except Exception:  # noqa: BLE001
                    return None

            still = _still()
            if still is False:
                out["ok"] = True
                out["verified"] = "列表里已经没有这条"
            else:
                try:
                    page.reload(wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(8000)
                except Exception:  # noqa: BLE001
                    pass
                still2 = _still()
                out["ok"] = still2 is False
                out["verified"] = ("刷新后确认已不在列表" if still2 is False
                                   else "列表里还能看到这条，可能没删掉")
            out["dialogs"] = dialogs
            out["net"] = reqs[-18:]
            if not out["ok"]:
                out["note"] = "没确认删掉，别当作成功。打开视频号助手手动看一眼。"
            return out
        finally:
            for ev, cb in (("dialog", _on_dialog), ("response", _on_resp)):
                try:
                    page.remove_listener(ev, cb)
                except Exception:  # noqa: BLE001
                    pass

    return session.run(_go, timeout=180, visible=visible)
