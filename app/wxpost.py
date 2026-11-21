"""把一条作业真正发到视频号上（定时发表）。

与 `wxpub.py` 的分工：
  `wxpub.py`   **读**号里的候选（合集 / 位置 / 剧集 / 视频标注）＋开发期探针
  `wxpost.py`  **写**：上传视频 → 设封面 → 填文案 → 选位置/剧集/标注 → 设定时
                → 发表 → 回读「待发布」核对

### 为什么单独一个文件
发表是**不可逆**的：点下去内容就进平台了。这里每一步都要能单独报错、单独
重试，所以不复用探针那套「尽量返回结果、不抛异常」的写法——这一步失败就
必须抛出来，让这一条标失败，别让它半吊子发出去。

### 三个反复踩过的坑（代码里都标了出处）
1. **表单在 iframe 里**：`frame.locator()` 在这页数到 0，`document.querySelector`
   才拿得到 → 全部走 `evaluate`。
2. **Vue + weui 的弹层不认合成 click**：`el.click()` 点了没反应也不报错 →
   要 `page.mouse` 真点（`_mouse_click`）。
3. **大文件塞不进 input**：base64 走 `evaluate` 有 8MB 上限，真实成片几十上百兆 →
   让页面自己去本机取：`fetch(本机地址)` → `blob` → `File` → `DataTransfer`。
   本机地址带签名，外人猜不出来（见 publishapi.publish_media）。
"""
from __future__ import annotations

import datetime
import difflib
import json
import os
import re
import time

from . import wxpub

FORM_FRAME = "micro/content/post/create"
CREATE_URL = "https://channels.weixin.qq.com/platform/post/create"

# 表单里能认出「这就是发表表单」的关键词（判据统一放在 wxpub.form_frame，
# 这里只留一份名字，**别再写第二套判断**——两套迟早分叉）。
FORM_KEYS = ("视频描述", "短标题", "位置", "合集", "链接", "活动",
             "定时发表", "封面")

# 发表页上各控件的真实选择器（2026-11-13 真实号实测导出，见
# data/diag/probe-create.json）。改版时只动这里。
SEL = {
    "desc":        ".input-editor",                      # 视频描述（contenteditable）
    "short":       'input[placeholder*="短标题"]',
    "schedule_at": 'input[placeholder="请选择发表时间"]',   # readonly，日历点出来的
    "schedule_time": 'input[placeholder="请选择时间"]',        # 展开时/分下拉
    "cal_right": "button.weui-desktop-btn__icon__right",      # 日历翻下个月
    "cal_label": ".weui-desktop-picker__panel__label",       # 「2026年」「11月」
    "cal_table": "table.weui-desktop-picker__table",        # 日期格 td > a
    "hour_list": "ol.weui-desktop-picker__time__hour",      # 00..23
    "min_list": "ol.weui-desktop-picker__time__minute",     # 00..59
    "cover_edit":  ".edit-btn",                          # 封面预览旁边的「编辑」
    "cover_img":   '.single-cover-uploader-wrap input[type="file"]',
    "declare":     ".select-placeholder",                # 「选择视频标注」那个占位
    "link":        ".post-link-wrap",                    # 扩展链接（剧集）
    "location":    ".post-position-wrap",
    "location_in": 'input[placeholder="搜索附近位置"]',
    "loc_item":    ".location-item-info",
    "series_item": '[class*="series-item"], [class*="export-item"]',
    "publish":     ".form-btns .weui-desktop-btn_wrp",   # 保存草稿/手机预览/发表 那一排
}


class PostError(Exception):
    """发表流程失败。文案已经是人话，worker 直接写进队列的失败原因里。"""


class IdentityMismatchError(PostError):
    """浏览器里登的号跟这条排期时锁的号不是同一个。

    单独一类：worker 见到它不自动重试（重试一百次也还是那个号），直接
    标失败并清掉这个号的候选缓存，让人去重新登录。
    """


# ── 小工具 ────────────────────────────────────────────────────────────────
def _form_frame(page, timeout: float = 90.0):
    """等发表表单**真正渲染出来**的那一帧。

    判据统一放在 wxpub.form_frame（读候选、选帧就一份实现）。
    这里不按 frame 的 URL 找：`micro/content/post/create` 常年停在
    「需要 JavaScript」的引导页上，对着它操作每个选择器都数到 0。
    """
    from . import wxpub
    fr = wxpub.form_frame(page, timeout=timeout)
    if fr is None:
        raise PostError("发表页没打开（表单一直没渲染出来）。稍后重试一次。")
    return fr


def _ev(form, js: str, arg=None):
    try:
        return form.evaluate(js, arg) if arg is not None else form.evaluate(js)
    except Exception as exc:  # noqa: BLE001
        raise PostError("页面操作失败：" + str(exc)[:140]) from exc


def _mouse_click(page, form, sel: str = "", txt: str = "") -> bool:
    """真实鼠标点击（iframe 里的坐标要加上 iframe 自身偏移）。

    为什么不用 `el.click()`：weui 的下拉/弹层是 Vue 组件，合成事件它不认 ——
    点了不报错、不展开、不发请求，看着像「这个号没有数据」（2026-11-13 踩过）。
    """
    try:
        form.evaluate(_SCROLL_JS, [sel, txt])
        page.wait_for_timeout(300)
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


def _mouse_click_main(page, sel: str = "", txt: str = "") -> bool:
    """主文档层面的真实鼠标点击。

    弹层不一定挂在表单 frame 里：wujie 微前端会把组件渲染到主文档
    （wxdel.py 顶部有同款坑的记录）。主文档坐标不用加 iframe 偏移。
    """
    try:
        page.evaluate(_SCROLL_JS, [sel, txt])
        page.wait_for_timeout(300)
        rect = page.evaluate(_RECT_JS, [sel, txt])
        if not rect:
            return False
        page.mouse.click(rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2)
        return True
    except Exception:  # noqa: BLE001
        return False


_PICK_JS = """(txt) => {
  const all = Array.from(document.querySelectorAll('div,span,button,a,li,p,label'));
  const norm = (n) => ((n.textContent || '') + '').replace(/\\s+/g, '').trim();
  const cands = all.filter((n) => norm(n) === txt);
  // 页面上常挂着好几个同名按钮（别的弹层的模板 / 隐藏的旧实例），
  // 取第一个几乎必然点到 width=0 的那个 —— 点了没反应也不报错。
  // 只在**可见**的里面挑，取最深（DOM 顺序最靠后）的那个。
  const vis = cands.filter((n) => { const r = n.getBoundingClientRect();
    return r.width > 1 && r.height > 1; });
  return vis.length ? vis[vis.length - 1]
                     : (cands.length ? cands[cands.length - 1] : null);
}"""

_SCROLL_JS = """([sel, txt]) => {
  const PICK = """ + _PICK_JS + """;
  let e = sel ? document.querySelector(sel) : null;
  if (!e && txt) e = PICK(txt);
  if (!e) return false;
  try { e.scrollIntoView({block: 'center'}); } catch (err) {}
  return true;
}"""

_RECT_JS = """([sel, txt]) => {
  const PICK = """ + _PICK_JS + """;
  let e = sel ? document.querySelector(sel) : null;
  if (!e && txt) e = PICK(txt);
  if (!e) return null;
  const r = e.getBoundingClientRect();
  if (r.width < 1 || r.height < 1) return null;
  return { x: r.x, y: r.y, w: r.width, h: r.height };
}"""


def _pick_cell(page, form, container_sel: str, text: str,
               item_sel: str = "li,a,td,span,div,button",
               contains: bool = False) -> bool:
    """在指定容器里按文字挑一个选项，滚到可见后**真实鼠标点**。

    为什么限定容器：日历表格里有 20 号、时列表里也有 20 时；按文字全局找
    必然点错（2026-11-13 实测，取「最后一个 20」命中了分钟列表深处、
    坐标 y=1453 已在视口外，点了完全没反应）。

    为什么要 scrollIntoView：时/分列表是 `ol` 内部滚动容器，24/60 个 li
    里大多数不在可视区，不滚就点不到。

    为什么真实鼠标：weui 的这些 li 是 Vue 组件，合成 click 不认。

    `contains=True` 用于位置候选（「石牌」要匹配「石牌坊社区 湖南省…」），
    并排除「不显示位置」这个取消项。
    """
    pt = _ev(form, """(o) => {
      const c = document.querySelector(o.cs);
      if (!c) return null;
      const norm = (x) => ((x.textContent || '') + '').replace(/\\s+/g, '').trim();
      const cand = Array.from(c.querySelectorAll(o.itemSel))
        .filter(x => {
          const t = norm(x);
          const hit = o.contains ? (t.includes(o.t) && t !== '不显示位置')
                                 : (t === o.t);
          return hit
            && !(x.className||'').toString().includes('disabled')
            && !(x.className||'').toString().includes('faded')
            && x.getBoundingClientRect().width > 1;
        });
      // 同一个可点条目常是「外层 div > 内层 div > name div」层层嵌套、
      // 文字完全相同；取 DOM 顺序最靠后（最深）的那个最精确。
      const e = cand[cand.length - 1];
      if (!e) return null;
      try { e.scrollIntoView({block: 'center'}); } catch (err) {}
      const r = e.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) return null;
      return {x: r.x + r.width/2, y: r.y + r.height/2};
    }""", {"cs": container_sel, "t": text, "itemSel": item_sel,
           "contains": bool(contains)})
    if not pt:
        return False
    try:
        box = form.frame_element().bounding_box() or {"x": 0, "y": 0}
    except Exception:  # noqa: BLE001
        box = {"x": 0, "y": 0}
    page.mouse.click(box["x"] + pt["x"], box["y"] + pt["y"])
    return True


def _set_input(form, sel: str, value: str) -> bool:
    """给 input 赋值并派发事件。

    Vue 2 的 v-model 监听 input 事件；用原生 setter 赋值再派发 input/change
    才认。直接 `el.value = x` 的话界面变了、Vue 内部的值还是旧的，提交上去
    就是空字段（这类静默失败最难查）。
    """
    return bool(_ev(form, """([sel, v]) => {
      const el = document.querySelector(sel);
      if (!el) return false;
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value').set;
      setter.call(el, v);
      el.dispatchEvent(new Event('input', {bubbles: true}));
      el.dispatchEvent(new Event('change', {bubbles: true}));
      el.dispatchEvent(new Event('blur', {bubbles: true}));
      return true;
    }""", [sel, value]))


def _set_editor(form, sel: str, text: str) -> bool:
    """给 contenteditable 的编辑器写内容。

    用 execCommand('insertText') 而不是 innerText：前者会走浏览器真正的
    输入通道，Vue 的编辑器组件能收到；后者只是改 DOM，组件里的值不会更新。
    """
    return bool(_ev(form, """([sel, v]) => {
      const el = document.querySelector(sel);
      if (!el) return false;
      el.focus();
      try {
        document.execCommand('selectAll', false, null);
        document.execCommand('insertText', false, v);
      } catch (err) {
        el.textContent = v;
        el.dispatchEvent(new Event('input', {bubbles: true}));
      }
      return true;
    }""", [sel, text]))


def _read_text(form, sel: str) -> str:
    try:
        return str(_ev(form, """(s) => {
          const el = document.querySelector(s);
          if (!el) return '';
          return (el.value !== undefined && el.value !== '')
            ? el.value : ((el.innerText || el.textContent || '') + '');
        }""", sel) or "")
    except Exception:  # noqa: BLE001
        return ""


# ── 上传 ───────────────────────────────────────────────────────────────────
def upload_file(form, url: str, name: str, mime: str, sel: str) -> dict:
    """把一个本地文件塞进页面里的 file input。

    走「页面自己去本机取」的路线：
      页面 fetch(本机地址) → blob → new File → DataTransfer → input.files
      → 派发 input + change
    为什么不用 base64：base64 要把整个文件塞进 evaluate 的参数，8MB 就到顶了，
    真实成片几十上百兆。也不走 CDP / set_input_files：这页的 iframe 里那两条路
    实测都不通（详见 MEMORY）。
    """
    res = _ev(form, """async (o) => {
      const resp = await fetch(o.url, {cache: 'no-store'});
      if (!resp.ok) return {err: 'http ' + resp.status};
      const blob = await resp.blob();
      const file = new File([blob], o.name, {type: o.type});
      const dt = new DataTransfer();
      dt.items.add(file);
      const inp = document.querySelector(o.sel);
      if (!inp) return {err: 'no file input: ' + o.sel};
      inp.files = dt.files;
      inp.dispatchEvent(new Event('input', {bubbles: true}));
      inp.dispatchEvent(new Event('change', {bubbles: true}));
      return {files: inp.files.length, size: inp.files[0] ? inp.files[0].size : 0};
    }""", {"url": url, "name": name, "type": mime, "sel": sel})
    if isinstance(res, dict) and res.get("err"):
        raise PostError("文件没塞进去：" + str(res["err"])[:120])
    return res if isinstance(res, dict) else {}


def wait_upload_done(page, form, seconds: float = 1200.0) -> None:
    """等视频上传完（封面区出现 **且** 上传提示消失才算完）。

    为什么不能只看「封面预览出现」：大海2 上实测（2026-11-14），视频还没
    传完时封面区就已经渲染出来了，只是带着「文件上传中，请等待完成后再
    编辑」的警告——提前去设封面，弹层里的确认按钮迟迟不出来（当晚连挂
    三条都死在封面这一步的真正根源）。所以两关一起看：封面区出现，
    且「文件上传中 / 取消上传 / 百分比」这些上传中的痕迹都消失。
    """
    end = time.time() + seconds
    while time.time() < end:
        ok = _ev(form, """() => {
          const t = (document.body.innerText || '');
          const area = t.includes('封面预览') || t.includes('编辑封面');
          const busy = t.includes('文件上传中') || t.includes('取消上传')
            || /\\d+\\s*%/.test(t);
          return area && !busy;
        }""")
        if ok:
            return
        page.wait_for_timeout(2000)
    raise PostError("视频上传超时（20 分钟）。多半是网速慢或视频太大，重试一次。")


# ── 封面 ───────────────────────────────────────────────────────────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DBG_COVER_FLAG = os.path.join(_ROOT, "data", ".dbg_cover")
_DBG_COVER_OUT = os.path.join(_ROOT, "logs", "hooktest", "cover_state.json")


def _dbg_cover(form, n: int, uploading: bool, extra: dict | None = None) -> None:
    """临时取证（2026-11-14）：封面弹层里「确认/确定」到底存不存在、可见不可见。

    只在 `data/.dbg_cover` 存在时写，所以**不用重启服务**就能开关；查完删掉文件
    即恢复零开销。写的是候选元素的矩形和可见性，判「是没点还是点空了」用。
    """
    if not os.path.exists(_DBG_COVER_FLAG):
        return
    try:
        info = _ev(form, """() => {
          const norm = (n) => ((n.textContent || '') + '').replace(/\\s+/g, '').trim();
          const all = Array.from(document.querySelectorAll('div,span,button,a,li,p,label'));
          const pick = (t) => all.filter((n) => norm(n) === t).map((n) => {
            const r = n.getBoundingClientRect();
            const cs = getComputedStyle(n);
            return {tag: n.tagName, cls: (n.className || '').toString().slice(0, 60),
                    x: Math.round(r.x), y: Math.round(r.y),
                    w: Math.round(r.width), h: Math.round(r.height),
                    vis: cs.visibility, disp: cs.display, op: cs.opacity,
                    disabled: !!n.disabled};
          });
          const body = document.body.innerText || '';
          return {confirm: pick('确定'), ok: pick('确认'), cancel: pick('取消'),
                  busy: body.includes('文件上传中'), cancelUp: body.includes('取消上传'),
                  dialogs: Array.from(document.querySelectorAll('.weui-desktop-dialog, .weui-desktop-modal'))
                    .map((d) => ({cls: (d.className || '').toString().slice(0, 80),
                                  txt: (d.innerText || '').replace(/\\s+/g, ' ').slice(0, 120),
                                  w: Math.round(d.getBoundingClientRect().width),
                                  h: Math.round(d.getBoundingClientRect().height)}))};
        }""") or {}
    except Exception as exc:  # noqa: BLE001
        info = {"err": str(exc)[:120]}
    info["iter"] = n
    info["uploading_flag"] = bool(uploading)
    info["at"] = time.strftime("%H:%M:%S")
    if extra:
        info.update(extra)
    try:
        os.makedirs(os.path.dirname(_DBG_COVER_OUT), exist_ok=True)
        with open(_DBG_COVER_OUT, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=1)
    except Exception:  # noqa: BLE001  取证失败绝不影响发表流程
        pass


def set_cover(page, form, img_url: str, name: str) -> dict:
    """把指定图片设成封面。

    流程（2026-11-13 真实号实测，见 data/diag/cover_click_out.txt）：
      真实点「编辑」→ `.single-cover-uploader-wrap input[type=file]` 塞图
      → **真实鼠标点可见的「确认/确定」** → 弹层收起来 = 平台接受。
      按钮文案号间有差异（seatonull=确认，大海2=确定），两个都试。

    两个坑：
    1. 页面同时挂着 6 个「确认」（可见 2 / 隐藏 4），合成 click 或按「第一个」
       匹配都会点到 width=0 的那个 → 静默无反应。必须真实鼠标点可见的。
    2. 成功判据不能只找「封面已更新」那个 toast：它可能挂在主文档 frame，
       或一闪而过。可靠信号是**弹层关闭**（可见的「确认」按钮消失）。

    2026-11-13 补：上传刚完成时封面预览未必渲染好了，直接找「编辑」会扑空
    （当晚连着 3 条都死在这一步）。先等它出现；等不到就把封面区域的实际
    文字带回错误里——封面自动化只在 seatonull 上真机验证过，别的号的
    助手界面长什么样，这样一眼就能看出来。
    """
    clicked = False
    for _ in range(16):
        if _mouse_click(page, form, SEL["cover_edit"]):
            clicked = True
            break
        page.wait_for_timeout(500)
    if not clicked:
        probe = _ev(form, """() => {
          const t = (document.body.innerText || '');
          const i = t.indexOf('封面');
          return i < 0 ? t.slice(0, 200) : t.slice(Math.max(0, i - 40), i + 160);
        }""")
        raise PostError("找不到封面上的「编辑」按钮（等了 8 秒）。这个号的封面区域是：「"
                        + " ".join(str(probe).split())[:120] + "」")
    page.wait_for_timeout(1500)
    res = upload_file(form, img_url, name, "image/png", SEL["cover_img"])
    if not res.get("files"):
        raise PostError("封面图片没塞进去。")
    # 「确认/确定」：图插进去后平台可能还在传（弹层有进度提示）——传完前
    # 确认按钮不出现/点了也没用。等「文件上传中」消失再点，上限 2 分钟。
    # 按钮文案**号间有差异**：seatonull 的弹层是「确认」，大海2 是「确定」
    # （2026-11-14 弹层文案实测），两个都试；主文档兜底留给别的号变体。
    clicked = False
    busy_n = 0
    for n in range(240):
        try:
            uploading = bool(form.evaluate(
                "() => (document.body.innerText || '').includes('文件上传中')"))
        except Exception:  # noqa: BLE001  页面跳转时读一下会抛，下一轮再看
            uploading = False
        if uploading:
            busy_n += 1
        if n % 10 == 0:
            _dbg_cover(form, n, uploading)
        if not uploading and (_mouse_click(page, form, "", "确认")
                              or _mouse_click(page, form, "", "确定")
                              or _mouse_click_main(page, "", "确认")
                              or _mouse_click_main(page, "", "确定")):
            clicked = True
            break
        page.wait_for_timeout(500)
    if not clicked:
        # 确认/确定都没找到：把弹层容器里所有短文案列出来——这个号的弹层
        # 按钮到底叫什么（完成/保存…），一眼就能看出来。
        btns = _ev(form, """() => {
          const inp = document.querySelector('.single-cover-uploader-wrap input[type=file]');
          if (!inp) return ['文件输入框不在了'];
          let root = inp;
          for (let i = 0; i < 5 && root && root !== document.body; i++) root = root.parentElement;
          const all = Array.from((root || document).querySelectorAll('button,div,a,span'))
            .map(e => ((e.textContent || '') + '').replace(/\\s+/g, '').trim())
            .filter(t => t && t.length <= 6);
          return [...new Set(all)].slice(0, 30);
        }""")
        # 真没点中：把「四个兜底各自找不找得到坐标」记下来，区分「没这个按钮」
        # 和「有这个按钮但不可见/坐标为空」（只查坐标，不再点，免得误触）。
        probe = {}
        for tag, h, sel in (("form", form, True), ("main", page, False)):
            for txt in ("确认", "确定"):
                try:
                    probe[f"{tag}:{txt}"] = bool(h.evaluate(_RECT_JS, ["", txt]))
                except Exception:  # noqa: BLE001
                    probe[f"{tag}:{txt}"] = "err"
        _dbg_cover(form, n, uploading, {"final": True, "rect_probe": probe,
                                        "btns": [str(b) for b in btns][:14]})
        if busy_n >= 200:
            # 一次都没点：不是「按钮找不到」，是上传卡住了。这两种要分开报，
            # 否则用户照「没有确认按钮」去猜，方向全错（2026-11-14 实测）。
            raise PostError("封面图一直卡在「文件上传中」（两分钟），不是按钮的问题。"
                            "重试一次；连续两次这样就把封面图换小一点。")
        raise PostError("封面弹层里没有可见的「确认/确定」按钮。弹层里的文案有："
                        + "、".join([str(b) for b in btns][:14]))
    page.wait_for_timeout(2500)
    # 成功判据：弹层里**可见的**文件输入框消失 = 弹层关了。用输入框判断比按
    # 按钮文字判断稳：按钮文案号间有差异，还可能有别的弹层模板挂着同名按钮。
    still_open = _ev(form, """() => {
      const inps = Array.from(document.querySelectorAll(
        '.single-cover-uploader-wrap input[type=file]'));
      return inps.some(e => { const r = e.getBoundingClientRect();
        return r.width > 1 && r.height > 1; });
    }""")
    if still_open:
        raise PostError("封面弹层没关掉，平台可能没接受这张图。")
    return {"cover": "updated"}


# ── 各字段 ────────────────────────────────────────────────────────────────
def fill_desc(form, title: str, desc: str = "") -> None:
    """写视频描述。

    视频号的第一行是标题，后面是描述 —— 平台按换行拆。所以这里拼成
    「标题\\n描述」。
    """
    text = (title or "").strip()
    if desc:
        text = text + "\n" + desc.strip()
    if not text:
        return
    if not _set_editor(form, SEL["desc"], text):
        raise PostError("找不到视频描述的输入框。")


def fill_short_title(form, short: str) -> None:
    if not short:
        return
    if not _set_input(form, SEL["short"], short):
        raise PostError("找不到短标题的输入框。")


def clear_location(page, form) -> None:
    """把平台沿用的上次城市清成「不显示位置」。

    MEMORY 铁律：真实发表页**每次打开都会沿用上次选的城市**（2026-11-13
    实测沿用「邵阳市」）。用户在队列里没填位置 = 不想标位置，若不主动清，
    整批作品都会被标到上次那个城市。

    页面提供「不显示位置」这个取消项（点开位置行后的第一条候选），点完
    `.post-position-wrap` 会变成 `<div class="not-display">不显示位置</div>`。
    """
    def _cur() -> str:
        return str(_ev(form, """() => {
          const w = document.querySelector('.post-position-wrap');
          return w ? (w.innerText || '').replace(/\\s+/g, ' ').trim() : '';
        }""") or "")

    if "不显示位置" in _cur():
        return
    if not _mouse_click(page, form, SEL["location"]):
        raise PostError("找不到「位置」那一行，没法清掉沿用城市。")
    page.wait_for_timeout(1200)
    if not _pick_cell(page, form, SEL["location"], "不显示位置"):
        raise PostError("位置候选里没有「不显示位置」，为避免标错城市已停手。")
    page.wait_for_timeout(900)
    if "不显示位置" not in _cur():
        raise PostError("位置没清掉，为避免标错城市已停手。")


def pick_location(page, form, keyword: str, city: str = "") -> None:
    """选位置：平台要地名才给候选，所以先输词再从候选里点。"""
    if not keyword:
        return
    if not _mouse_click(page, form, SEL["location"]):
        raise PostError("找不到「位置」那一行。")
    page.wait_for_timeout(1200)
    if not _set_input(form, SEL["location_in"], keyword):
        raise PostError("位置搜索框弹不出来。")
    page.wait_for_timeout(2500)
    # 候选里第一条永远是「不显示位置」这个取消项，取第一个 `.location-item-info`
    # 会把用户选的城市点成「不显示位置」。按关键词匹配整条候选文字。
    if not _pick_cell(page, form, SEL["location"], keyword,
                      item_sel=SEL["loc_item"], contains=True):
        raise PostError(f"没搜到「{keyword}」这个位置。换个更短的词试试。")
    page.wait_for_timeout(900)


# ── 剧集：接口认领 → 按平台真名点条目（2026-11-14 改）──────────────────────
# 老写法只点了「链接 → 视频号剧集」两层就按文字找条目，而列表是**点开第三层
# 「选择需要添加的视频号剧集」**时才拉的 —— 于是永远是空列表，等 15 秒报
# 「没找到」。2026-11-14 就是这么让整批 8 条在第 4 步全灭的。
# 现在按拉候选那套来：拦 search_drama_component → 重放拿 id/真名 → 按真名点。
# 好处：名字差几个字、带「·N 集」角标、平台侧改过名，都认得出，而不是瞎点。
def _norm_series(s: str) -> str:
    s = re.sub(r"\s+", "", str(s or ""))
    s = re.sub(r"(?:[·・.．]?\s*\d+\s*集)$", "", s)     # 去掉「·36集」角标
    for a, b in (("（", "("), ("）", ")"), ("，", ","), ("：", ":"),
                 ("、", ","), ("　", "")):
        s = s.replace(a, b)
    return s


def _match_series(items: list[dict], want: str):
    """在一堆剧集里认领「用户当初选的那条」。返回 (条目, 依据) 或 (None, 候选名)。

    为什么允许模糊：队列里的名是用户从候选里点下来的，平台侧改名后两边就差
    几个字（实测「……甲方回归」→「……甲方身份回归」）。**认不准宁可报错也不
    猜** —— 猜错等于给整批发错剧集。
    """
    nw = _norm_series(want)
    if not nw:
        return None, []
    pairs = [(_norm_series(x.get("cn") or ""), x) for x in items]
    for n, x in pairs:
        if n and n == nw:
            return x, "exact"
    # 一方包含另一方：剧集名 6 字以上才算，免得「第1集」这种短名误合
    hit = [x for n, x in pairs if len(n) >= 6 and (nw in n or n in nw)]
    if len(hit) == 1:
        return hit[0], "contain"
    # 最后按相似度，只在**唯一最高分且明显领先**时采用
    scored = sorted(((difflib.SequenceMatcher(None, nw, n).ratio(), x)
                     for n, x in pairs if n), key=lambda p: -p[0])
    if scored and scored[0][0] >= 0.85 and (
            len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.03):
        return scored[0][1], f"similar {scored[0][0]:.2f}"
    return None, [x.get("cn") or "" for x in items]


def _series_items(page, tmpl: dict) -> list[dict]:
    """重放剧集接口拿全量条目（接口分页，DOM 只看得到当前屏）。"""
    out: list[dict] = []
    for pg in (1, 2, 3):
        data = wxpub._replay(page, tmpl, "series",
                             {"currentPage": pg, "pageSize": 50})
        if data is None:
            break
        got = wxpub._api_items("series", data)
        if not got:
            break
        out.extend(got)
        if len(got) < 50:
            break
    return out


def _click_series_text(page, form, name: str) -> bool:
    """按文字点一条剧集（先精确，再允许带「·N 集」角标的条目）。"""
    if _mouse_click(page, form, "", name):
        return True
    pt = _ev(form, """(t) => {
      const norm = (x) => ((x.textContent || '') + '').replace(/\\s+/g, '').trim();
      const cands = Array.from(document.querySelectorAll('div,span,a,li,p,button'))
        .filter(x => {
          const s = norm(x);
          return s.includes(t) && s.length <= t.length + 12
            && x.getBoundingClientRect().width > 1;
        });
      const e = cands[cands.length - 1];
      if (!e) return null;
      try { e.scrollIntoView({block: 'center'}); } catch (err) {}
      const r = e.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) return null;
      return {x: r.x + r.width/2, y: r.y + r.height/2};
    }""", name)
    if pt is None:
        return False
    box = form.frame_element().bounding_box() or {"x": 0, "y": 0}
    page.mouse.click(box["x"] + pt["x"], box["y"] + pt["y"])
    return True


def _pick_series_dom(page, form, title: str) -> None:
    """兜底：非剧集的链接类型（文章/红包封面/短剧）仍按文字点。"""
    ok = False
    for _ in range(30):
        if _mouse_click(page, form, "", title):
            ok = True
            break
        page.wait_for_timeout(500)
    if not ok:
        ok = _click_series_text(page, form, title)
    if not ok:
        items = _ev(form, """() => {
          const t = (document.body.innerText || '');
          const i = t.indexOf('视频号剧集');
          return i < 0 ? t.slice(0, 200) : t.slice(i, i + 300);
        }""")
        raise PostError(f"剧集列表里没找到「{title}」（等了 15 秒）。列表区域是：「"
                        + " ".join(str(items).split())[:140] + "」")
    page.wait_for_timeout(1500)


def pick_series(page, form, title: str, tmpl: dict, link_kind: str = "series") -> dict:
    """选扩展链接里的剧集。

    2026-11-14 改成「接口认领 + 按真名点」：
      ① 收集 `post/search_drama_component`（只在弹层被真点开时才发）
      ② 重放拿全部条目（id / name / mediaCount / usable）
      ③ 用真名认领用户选的那条（精确 → 包含 → 相似度）
      ④ 拿平台真名点 DOM 条目（带角标也点得中）
    失败时按「没拦到 / 列表空 / 有条目但点不中」三种分别报，别再一句
    「没找到」让人以为号里没数据。

    `tmpl` 由 publish_one 统一挂监听收集（见 `_go`），这里只负责把请求逼出来。
    """
    if not title:
        return {"via": "none"}
    if (link_kind or "series") != "series":
        _pick_series_dom(page, form, title)
        return {"via": "dom", "kind": link_kind}

    # 必须真点三层才会发请求；_prime_series 就是这套（和拉候选共用实现）
    primed = wxpub._prime_series(page, form, tmpl)
    page.wait_for_timeout(1500)
    items = _series_items(page, tmpl) if primed else []

    if items:
        hit, how = _match_series(items, title)
        if hit is None:
            names = "、".join([n for n in (how or []) if n][:6])
            raise PostError(
                f"剧集列表里有 {len(items)} 部，但没有和「{title}」对得上的。"
                f"号里前几部是：{names}。请回发布页重新选一次剧集。")
        if hit.get("usable") is False:
            raise PostError(f"剧集「{hit.get('cn')}」在平台上不可选："
                            + (hit.get("why") or "未上架/已下架"))
        name = hit.get("cn") or title
        # 选中判据（2026-11-14 真号实测）：选中后 `.post-link-wrap` 里会出现
        # `.choosen-link-wrap`（带图标 + 类型名），同时列表弹层关掉。
        # 只按「点了不报错」算成功是不行的 —— weui 点空了也返回成功，
        # 结果视频发出去没带剧集，不可逆。
        for _ in range(4):
            if not _click_series_text(page, form, name):
                page.wait_for_timeout(600)
                continue
            page.wait_for_timeout(1500)
            st = _ev(form, """() => {
              const w = document.querySelector('.post-link-wrap');
              const chosen = !!(w && w.querySelector('.choosen-link-wrap'));
              const open = Array.from(document.querySelectorAll('div,span'))
                .filter(e => /\\d+\\s*集/.test(e.textContent || '')
                             && e.getBoundingClientRect().width > 1).length;
              return {chosen: chosen, open: open,
                      txt: w ? (w.innerText || '').replace(/\\s+/g, ' ').trim()
                               .slice(0, 60) : ''};
            }""") or {}
            if st.get("chosen") or int(st.get("open") or 0) == 0:
                return {"via": "api", "name": name, "id": hit.get("v") or "",
                        "how": how, "count": hit.get("count") or 0,
                        "shown": str(st.get("txt") or "")[:60]}
            page.wait_for_timeout(600)
        raise PostError(f"接口里有剧集「{name}」（{hit.get('count') or 0} 集），"
                        "但点了没选中（链接行没变成已选状态）。重试一次；"
                        "还不行就重开助手页。")

    # 接口这一路没成：退回按文字点，但报错要说清是哪一段没成
    try:
        _pick_series_dom(page, form, title)
        return {"via": "dom"}
    except PostError as exc:
        if not primed:
            raise PostError(
                "点开「视频号剧集」后平台没发出列表请求（列表拉不起来），"
                "这条没选上。重试一次。") from exc
        raise PostError(
            "平台返回的剧集列表是空的 —— 剧集只列已「上架」的。"
            f"先去视频号助手确认「{title}」还在且已上架，再重试。") from exc


def pick_declare(page, form, want: str, tmpl: dict | None = None) -> dict:
    """选视频标注。

    队列里存的是平台枚举值 `tagType`（0/1/2/3/5/7/8），**不是中文名**
    —— 拿数字去点文字当然点不到（2026-11-14 实测「视频标注里没有「1」」，
    整批就卡在这一步）。所以数字先经接口换成平台显示的文字再点；
    不是数字（老数据/手填）就按原样点。
    """
    want = str(want or "").strip()
    if not want:
        return {}
    cn = want
    via = "text"
    if want.isdigit():
        data = wxpub._replay(page, tmpl, "declare",
                             wxpub.API_REPLAY["declare"][1]) if (tmpl and "declare" in tmpl) else None
        items = wxpub._api_items("declare", data or {})
        hit = next((x for x in items if str(x["v"]) == want), None)
        if hit is None and want == "0":
            # tagType 0 在接口里 v 是空串（= 无需标注）
            hit = next((x for x in items if str(x["v"]) == ""), None)
        if hit is None:
            raise PostError(
                f"视频标注里没有编号「{want}」这一项（平台返回 {len(items)} 项"
                f"{'，接口没读到' if data is None else ''}）。"
                "回发布页把这条的标注重选一次。")
        cn = hit["cn"]
        via = "api"
    if not _mouse_click(page, form, SEL["declare"]):
        raise PostError("找不到「视频标注」那一行。")
    page.wait_for_timeout(1500)
    if not _mouse_click(page, form, "", cn):
        raise PostError(f"视频标注里没有「{cn}」。")
    page.wait_for_timeout(1200)
    return {"via": via, "name": cn, "value": want}



def set_schedule(page, form, at: datetime.datetime) -> dict:
    """设定时发表时间（真实号实测路径，见 data/diag/schedule_*_out*.txt）。

    **这不是普通 input**：`请选择发表时间` 是 readonly，往里赋值会被组件
    内部状态覆盖（实测填 2026-11-14 20:00，读回还是默认的 2026-11-13 23:00）。
    真实结构是 weui 的日历 + 时分下拉，必须一步步点：

      ① 点「定时」radio
      ② 点 readonly 框展开日历
      ③ 按日历头的年月点「下个月」翻到目标月（左箭头是 display:none，
         平台只允许往后排）
      ④ 在日历表格里真实点目标日期（禁用格带 faded/disabled，不能点）
      ⑤ 点「请选择时间」展开时/分下拉
      ⑥ 在 hour / minute 两个 ol 里真实点目标时、分（ol 是内部滚动容器，
         要先 scrollIntoView）
      ⑦ 读回主框，跟目标值逐字比对；对不上就不许往下发表
    """
    # ① 定时 radio
    picked = _ev(form, """() => {
      const labs = Array.from(document.querySelectorAll('.post-time-wrap label'));
      const lab = labs.find((l) => ((l.textContent || '').replace(/\\s+/g,'')) === '定时');
      if (!lab) return false;
      lab.click();
      const r = lab.querySelector('input[type=radio]');
      if (r && !r.checked) r.click();
      return true;
    }""")
    if not picked:
        raise PostError("找不到「定时发表」的开关。")
    page.wait_for_timeout(800)

    # ② 展开日历
    if not _mouse_click(page, form, SEL["schedule_at"]):
        raise PostError("点不开发表时间日历。")
    page.wait_for_timeout(1000)

    # ③ 翻月（最多翻 14 次，够覆盖平台允许的排期范围）
    for _ in range(15):
        ym = _ev(form, """() => {
          const ls = Array.from(document.querySelectorAll(
              '.weui-desktop-picker__panel__label'))
            .map(e => ((e.textContent||'')+'').trim());
          return ls.length >= 2 ? [ls[0], ls[1]] : null;
        }""")
        if not ym:
            raise PostError("读不到日历上的年月，稍后重试一次。")
        cy = int("".join(c for c in ym[0] if c.isdigit()) or 0)
        cm = int("".join(c for c in ym[1] if c.isdigit()) or 0)
        diff = (at.year - cy) * 12 + (at.month - cm)
        if diff <= 0:
            break
        if not _mouse_click(page, form, SEL["cal_right"]):
            raise PostError("翻不到下一个月（平台可能限制了这个定时范围）。")
        page.wait_for_timeout(500)
    else:
        raise PostError("定时时间太远，日历翻不过去。")

    # ④ 点日期
    if not _pick_cell(page, form, SEL["cal_table"], str(at.day)):
        raise PostError(
            f"日历里选不了 {at.month} 月 {at.day} 号 —— 多半超出了平台允许的定时范围，"
            "换个近一点的时间试试。")
    page.wait_for_timeout(600)

    # ⑤ 展开时分下拉
    if not _mouse_click(page, form, SEL["schedule_time"]):
        raise PostError("点不开时/分列表。")
    page.wait_for_timeout(800)

    # ⑥ 点小时、分钟
    if not _pick_cell(page, form, SEL["hour_list"], f"{at.hour:02d}"):
        raise PostError(f"时列表里没有 {at.hour:02d} 点。")
    page.wait_for_timeout(400)
    if not _pick_cell(page, form, SEL["min_list"], f"{at.minute:02d}"):
        raise PostError(f"分列表里没有 {at.minute:02d} 分。")
    page.wait_for_timeout(700)

    # ⑥½ 提交到组件模型。**这一步不做，前面全白点**：
    # 点 hour/minute 的 li 只走组件的 handleTimeChange——它只改内部 pick，
    # 不 emit「input」，父组件（post-time-wrap）的 time 停在旧值。实测表现：
    # 框里显示 20:30、真发表时平台收到的还是点日期那一刻的默认时间
    # （验收时平台存成 00:00 的根因，2026-11-14 探针 sched_steps5~7 实锤）。
    # handleTimeSelect 才是真正对外 emit input/change 的提交入口。
    expect_ms = int(time.mktime(at.timetuple())) * 1000
    committed = _ev(form, """(o) => {
      const grab = (el) => { let n = el, d = 0;
        while (n && d < 6) { if (n.__vue__) return n.__vue__; n = n.parentElement; d++; }
        return null; };
      let pi = null;
      for (const sel of o.sels) {
        for (const el of Array.from(document.querySelectorAll(sel))
             .filter((e) => e.getBoundingClientRect().width > 1)) {
          const inst = grab(el);
          if (inst && typeof inst.handleTimeSelect === 'function') { pi = inst; break; }
        }
        if (pi) break;
      }
      if (!pi) return {found: false};
      if (pi.pick !== o.expect) return {found: true, pickOk: false,
                                        pick: pi.pick, expect: o.expect};
      pi.handleTimeSelect();
      return {found: true, pickOk: true, pick: pi.pick};
    }""", {"sels": [".weui-desktop-picker__date", ".weui-desktop-picker__dt"],
           "expect": expect_ms})
    if not committed or not committed.get("found"):
        raise PostError("发表时间组件没找到，时间设不上，已停手。")
    if not committed.get("pickOk"):
        raise PostError("时分点了但组件没接住（内部值没跟上），时间不可信，已停手。")

    # ⑥¾ 核对**父组件真正提交的值**（不是框里那行显示字）。
    # 框上那行字跟着内部 pick 走，曾出现过「显示 20:00、实际提交 00:00」；
    # 唯一可信的是 post-time-wrap 组件 data 里的 time 时间戳。
    st = _ev(form, """(o) => {
      let n = document.querySelector('.post-time-wrap'), d = 0;
      while (n && d < 6) { if (n.__vue__) break; n = n.parentElement; d++; }
      if (!n || !n.__vue__) return {found: false};
      return {found: true, time: n.__vue__.$data.time};
    }""", {"expect": expect_ms})
    if not st or not st.get("found") or st.get("time") != expect_ms:
        got = st.get("time")
        got_s = time.strftime("%Y-%m-%d %H:%M", time.localtime(got / 1000)) if got else "空"
        raise PostError(f"发表时间没有真正设上（组件里还是「{got_s}」），"
                        f"跟定时「{at.strftime('%Y-%m-%d %H:%M')}」对不上，已停手。")

    # ⑦ 读回逐字核对；对不上绝不能让 worker 往下点发表
    want = at.strftime("%Y-%m-%d %H:%M")
    got = _read_text(form, SEL["schedule_at"]).strip()
    if got != want:
        raise PostError(f"发表时间设成了「{got or '空'}」，跟定时「{want}」对不上，已停手。")
    # 收起弹层，免得挡住后面的「发表」按钮
    _ev(form, "() => { const e = document.querySelector('.input-editor'); if (e) e.focus(); }")
    return {"scheduled_at": got}


# ── 点「发表」之后的原创分成提醒 ─────────────────────────────────────────
# 平台点了发表可能弹「声明原创的视频有机会获得广告分成」，高亮默认常是
# 「声明原创」。不替用户做这个承诺：按队列里自己选的 original 走；这里
# **绝不二次点主发表按钮** —— 点过了再点就是第二条。
_REMINDER_JS = """() => {
  const norm = s => ((s || '') + '').split('')
      .filter(c => c.charCodeAt(0) > 32).join('');
  const vis = e => { const r = e.getBoundingClientRect();
                     return r.width > 1 && r.height > 1; };
  const dialogs = root => Array.from(root.querySelectorAll(
      '.weui-desktop-dialog,[role=dialog]')).filter(vis);
  let reminder = false, rights = false;
  for (const d of dialogs(document)) {
    const txt = norm(d.innerText || d.textContent || '');
    if (txt.includes('声明原创的视频有机会获得广告分成')) {
      reminder = true;
      // 给按钮打自定义属性，后面用真实鼠标按属性点（Vue/weui 不认合成 click）
      for (const b of Array.from(d.querySelectorAll('button,a')).filter(vis)) {
        const t = norm(b.innerText || b.textContent || '');
        if (t === '直接发表') b.setAttribute('data-wbm-direct', '1');
        else if (t === '声明原创') b.setAttribute('data-wbm-original', '1');
        else if (t.includes('不再提醒')) b.setAttribute('data-wbm-hide', '1');
      }
    }
    if (txt.includes('原创权益') || txt.includes('原创声明须知')) {
      rights = true;
      for (const e of Array.from(d.querySelectorAll('label,input,button,a')).filter(vis)) {
        const t = norm(e.innerText || e.textContent || '');
        if (t.includes('我已阅读并同意')) e.setAttribute('data-wbm-agree', '1');
        if (t === '声明原创' && e.tagName === 'BUTTON' && !e.disabled)
          e.setAttribute('data-wbm-confirm', '1');
      }
    }
  }
  return {reminder: reminder, rights: rights};
}"""


def _reminder_state(page, form) -> dict:
    """弹层可能挂主文档，也可能挂表单 iframe；两边都读，都不许抛。"""
    out = {"reminder": False, "rights": False}
    for doc in (page, form):
        try:
            r = doc.evaluate(_REMINDER_JS)
        except Exception:  # noqa: BLE001  页面正在跳转，下一轮再读
            continue
        if isinstance(r, dict):
            out["reminder"] = out["reminder"] or bool(r.get("reminder"))
            out["rights"] = out["rights"] or bool(r.get("rights"))
    return out


def _click_marked(page, form, name: str) -> bool:
    sel = f'[data-wbm-{name}="1"]'
    try:
        return bool(_mouse_click_main(page, sel) or _mouse_click(page, form, sel))
    except Exception:  # noqa: BLE001
        return False


def _confirm_rights(page, form, timeout: float = 5.0) -> bool:
    """点了「声明原创」之后的权益确认弹层：勾同意 → 点确认。只走一次。"""
    try:
        deadline = time.time() + timeout
        agreed = False
        while time.time() < deadline:
            st = _reminder_state(page, form)
            if st.get("rights"):
                if not agreed:
                    agreed = _click_marked(page, form, "agree")
                if agreed and _click_marked(page, form, "confirm"):
                    return True
            page.wait_for_timeout(400)
    except Exception:  # noqa: BLE001
        pass
    return False


def handle_original_reminder(page, form, original: bool = False,
                             timeout: float = 5.0) -> str:
    """点完「发表」后处理原创分成提醒，返回 direct / original / ''。

    ⚠️ 这个函数在点发表**之后**调用：任何异常都必须吞掉。抛出去会让
    worker 以为「没点发表」而重试，那就是再发一条。不碰「不再提醒」，
    不改账号级偏好，处理完直接交给回读去判定结果。
    """
    try:
        deadline = time.time() + timeout
        handled = ""
        while time.time() < deadline:
            st = _reminder_state(page, form)
            if not handled and st.get("reminder"):
                want = "original" if original else "direct"
                if _click_marked(page, form, want):
                    handled = want
                    if original:
                        _confirm_rights(page, form)
            if handled and not st.get("reminder"):
                break
            page.wait_for_timeout(400)
        return handled
    except Exception:  # noqa: BLE001
        return ""


# ── 发表前：浏览器里登的是不是排期时那个号 ────────────────────────────────
_IDENTITY_JS = """() => {
  const sels = ['.finder-nickname', '.finder-nick-name', '.account-nickname',
                '.weui-desktop-account__nickname', '.account-name',
                '[data-account-name]', '[class*=nickname]'];
  const vis = e => { const r = e.getBoundingClientRect();
                     return r.width > 1 && r.height > 1; };
  for (const s of sels) {
    for (const e of Array.from(document.querySelectorAll(s))) {
      if (!vis(e)) continue;
      const t = ((e.innerText || e.textContent || '') + '').trim();
      if (t && t.length <= 24) return t;
    }
  }
  return '';
}"""


def check_account_identity(page, expected: dict) -> dict:
    """读当前登录号名，与排期时冻结的身份比对。

    读不到 / 排期时没冻结（老数据、资料没抓全）就**跳过**，不误伤：
    宁可少一道校验，也不能因为页面改版把整批发不出去。
    """
    want = re.sub(r"\s+", "", str((expected or {}).get("name") or ""))
    if not want:
        return {"checked": False, "reason": "排期时没记下号名"}
    got = ""
    docs = [page] + list(getattr(page, "frames", []) or [])
    for doc in docs:
        try:
            t = str(doc.evaluate(_IDENTITY_JS) or "").strip()
        except Exception:  # noqa: BLE001
            continue
        if t and len(t) <= 24:
            got = t
            break
    if not got:
        return {"checked": False, "reason": "页面上没读到号名"}
    if re.sub(r"\s+", "", got) != want:
        raise IdentityMismatchError(
            f"浏览器里现在登的是「{got}」，这条排的是「{want}」。"
            f"已经停手，没上传也没发表，先去重新登录这个号。")
    return {"checked": True, "name": got}


# ── 发表前：填好的字段统一核两遍（2026-11-15 A 档）────────────────────────
# 平台是 Vue 异步组件，「填了」不等于「生效」：短标题、定时、挂载都可能在
# 我们填完之后被它自己回填 / 清空。这里只读地把能认出的字段摆成一张清单，
# 等 1.2 秒再读一遍，两遍对不上就停手。
_PRECHECK_JS = r"""() => {
  const g = (s) => { const e = document.querySelector(s);
                     return e ? ((e.value || e.innerText || '') + '').replace(/\s+/g, ' ').trim() : ''; };
  const vis = (e) => { const r = e.getBoundingClientRect();
                       return r.width > 4 && r.height > 4; };
  const dialogs = Array.from(document.querySelectorAll(
      '.weui-desktop-dialog, .weui-desktop-dialog__wrp, [role=dialog]'))
    .filter(vis)
    .map((d) => ((d.innerText || '') + '').replace(/\s+/g, ' ').trim().slice(0, 80))
    .filter(Boolean);
  return {
    desc: g('.input-editor'),
    short: g('input[placeholder*="短标题"]'),
    at: g('input[placeholder="请选择发表时间"]'),
    pos: g('.post-position-wrap'),
    link: g('.choosen-link-wrap') || g('.link-input-wrap .content-wrap'),
    declare: g('.post-tag-wrap'),
    dialogs: dialogs,
  };
}"""

# 只有这三项两遍都读到且不一致才拦：位置 / 挂载 / 标注是异步文本，弹层收起
# 时字会变，拿它们当硬判据会把整批卡死。其余字段只记进清单供诊断看。
_PRECHECK_STRICT = ("desc", "short", "at")
_PRECHECK_ALL = ("desc", "short", "at", "pos", "link", "declare")
_PRECHECK_CN = {"desc": "描述", "short": "短标题", "at": "定时时间",
                "pos": "位置", "link": "扩展链接", "declare": "视频标注"}


def verify_form_twice(page, form) -> dict:
    """点「发表」之前核两轮，**只读、绝不改动页面**。

    在这里抛异常是安全的：还没点发表，worker 会把整条重来一遍，不会重复发。
    读不到的字段一律跳过（跟登录身份核对同一口径）——页面改版换个 class
    不该让一整批发不出去。
    """
    def snap() -> dict:
        try:
            return dict(_ev(form, _PRECHECK_JS) or {})
        except Exception:  # noqa: BLE001
            return {}

    a = snap()
    page.wait_for_timeout(1200)
    b = snap()
    # 两遍都还挂着弹窗 = 真没关；一闪而过的 toast 不会连续两遍都在
    if (a.get("dialogs") or []) and (b.get("dialogs") or []):
        raise PostError("发表页还有没关掉的弹窗，已经停手，没有点发表。稍后重试一次。")
    changed = [k for k in _PRECHECK_STRICT
               if (a.get(k) or "") and (b.get(k) or "") and a[k] != b[k]]
    if changed:
        raise PostError(
            "刚填好的「" + "、".join(_PRECHECK_CN.get(k, k) for k in changed)
            + "」又被页面改掉了，已经停手，没有点发表。稍后重试一次。")
    return {"checked": True,
            "fields": [k for k in _PRECHECK_ALL if (a.get(k) or b.get(k))],
            "dialogs": 0}


def click_publish(page, form, original: bool = False) -> None:
    """点「发表」。

    这一步是**不可逆**的：点了内容就进平台了。所以只在真正要发的时候才调，
    调用方必须自己确认过。

    点之前先看按钮是不是灰的：字段没齐时平台会把它置灰，这时鼠标点下去
    什么都不会发生，但调用方会以为已经发出去了 —— 必须先拦住，别往下走。
    """
    st = _ev(form, """() => {
      const norm = (e) => ((e.textContent || '') + '').replace(/\\s+/g, '').trim();
      const wrap = document.querySelector('.form-btns');
      const pool = wrap ? Array.from(wrap.querySelectorAll('button'))
                        : Array.from(document.querySelectorAll('button'));
      const vis = pool.filter((b) => norm(b) === '发表'
                              && b.getBoundingClientRect().width > 1);
      const b = vis[vis.length - 1];
      if (!b) return {found: false};
      return {found: true,
              disabled: b.disabled === true
                        || (b.className || '').toString().includes('disabled'),
              cls: (b.className || '').toString().slice(0, 120)};
    }""")
    if not st or not st.get("found"):
        raise PostError("找不到「发表」按钮，已经停手，没有点发表。")
    if st.get("disabled"):
        raise PostError("「发表」按钮还是灰的，说明有必填项没过。已经停手，没有点发表。")

    # 表单很长，按钮会落在视口外；先把主页面滚回顶部再让它滚进 iframe 视口，
    # 否则 frame 的 bounding_box 可能是负偏移，鼠标点了空处。
    try:
        page.evaluate("() => { try { window.scrollTo(0, 0); } catch (e) {} }")
    except Exception:  # noqa: BLE001
        pass
    if not _mouse_click(page, form, ".form-btns button.weui-desktop-btn_primary"):
        if not _mouse_click(page, form, "", "发表"):
            raise PostError("点不到「发表」按钮，已经停手。")
    # 从这里起**绝不向外抛异常**：点过了再抛，worker 会重试＝再发一条。
    # 平台可能弹「原创分成提醒」，在等提交的这几秒里按队列自己的选择处理掉。
    try:
        handle_original_reminder(page, form, original=original)
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(1200)


def read_scheduled(page, title: str, plan_at: str = "") -> dict:
    """回「待发布」列表核对：这条真的挂上去了吗、时间对不对。

    为什么必须回读：平台点「发表」后可能弹「定时发表需审核」之类的提示，
    也可能直接拒绝。光看点击成功不算数（规格 §9.2 最后一步）。

    `/platform/post/list` 会被重定向到 `/platform`（2026-11-13 实测），
    真正的内容列表在 `micro/content/iframe/post-card.html` 这一帧里，
    定时条目写成「将于2026年11月14日 20:00发表」。所以标题片段和平台
    格式的时间都认，双判据。
    """
    page.goto("https://channels.weixin.qq.com/platform",
              wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(8000)
    title_nd = title[:8] if title else ""
    time_nd = ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})", plan_at or "")
    if m:
        y, mo, d, hh, mm = m.groups()
        # 平台原样保留两位（11月07日 / 02:00），别自作主张去掉前导零
        time_nd = f"{y}年{mo}月{d}日 {hh}:{mm}"

    # 标题和时间**两个都要对上**才能算成功。只认标题的话，
    # 「发出去了但定时发错了」会被判成成功 —— 2026-11-13 真号验收踩到：
    # 表单里 20:00，平台存成 00:00，标题照样出现在待发布列表里。
    seen_title = False
    seen_time = False
    last_body = ""
    deadline = time.time() + 60
    while time.time() < deadline:
        for fr in page.frames:
            try:
                raw = str(fr.evaluate("() => (document.body.innerText || '')") or "")
            except Exception:  # noqa: BLE001
                continue
            body = re.sub(r"\s+", " ", raw)
            if title_nd and title_nd in body:
                seen_title = True
                last_body = body[:500]
            if time_nd and time_nd in body:
                seen_time = True
                last_body = body[:500]
            if (seen_title or not title_nd) and (seen_time or not time_nd):
                return {"found": True, "at": plan_at,
                        "title_found": seen_title, "time_found": seen_time,
                        "time_want": time_nd, "body": last_body}
        page.wait_for_timeout(2000)
    return {"found": False, "at": plan_at,
            "title_found": seen_title, "time_found": seen_time,
            "time_want": time_nd, "body": last_body}


# ── 整条流程 ──────────────────────────────────────────────────────────────
def publish_one(session, job: dict, tpl: dict, media: dict,
                dry_run: bool = False, progress=None,
                on_before_publish=None) -> dict:
    """把一条作业发到平台上（定时发表）。

    `media` = {"video": 本机地址, "cover": 本机地址（可空）}
    `dry_run=True` → 填完所有字段就停手，**不点发表**。开发期核对用这个。
    `progress` = 可选的步骤回调，队列行靠它显示「正在上传 · 设封面」（2026-11-14
    之前整段上传只有一句「正在上传」，用户分不清在传还是卡死）。会在账号线程里
    被调，回调自己负责线程安全；回调坏了不影响发布本身。

    返回 {"ok": True, "scheduled_at": ..., "verified": ...}；
    任何一步失败都抛 PostError（文案已经是人话）。
    """
    title = (job.get("title") or "").strip()
    if not title:
        raise PostError("这条没有标题，先给它起一个标题。")
    plan_at = publisher_parse(job.get("plan_at"))
    if not plan_at:
        raise PostError("这条没有定时时间。")

    def _say(label):
        if progress:
            try:
                progress(label)
            except Exception:  # noqa: BLE001  进度显示坏了不能拖累发布
                pass

    def _go(page):
        _say("打开发表页")
        # 候选 CGI 的模板要**从 goto 之前**就开始收：视频标注那支是表单初始化
        # 自己发的，等打开表单再挂监听就永远收不到（剧集是靠点开弹层才发，
        # 所以两个的时机不一样）。模板只用来重放拿 id/真名，不带用户数据。
        tmpl: dict = {}
        try:
            page.remove_all_listeners("response")   # page 长期复用，别越挂越多
        except Exception:  # noqa: BLE001
            pass
        page.on("response", lambda r: wxpub.collect_tmpl(r, tmpl))
        # 打开发表页**并推进到能填表单的状态**：
        # 直接 goto 的话页面停在「最近视频」弹层 + 引导页，
        # 必须点一次「发表视频」真表单才会出现（见 wxpub.open_post_form）。
        form = wxpub.open_post_form(page, timeout=150)
        if form is None:
            raise PostError("发表页没打开（表单一直没渲染出来）。稍后重试一次。")
        out: dict = {"steps": []}

        # ⓪ 先确认浏览器里登的仍是排期时那个号。读不到就跳过（老数据、
        # 页面改版都可能读不到），别因为一道校验把整批卡死。
        try:
            out["identity"] = check_account_identity(
                page, job.get("account_identity") or {})
        except IdentityMismatchError:
            raise
        except Exception as exc:  # noqa: BLE001
            out["identity"] = {"checked": False, "reason": str(exc)[:100]}

        # ① 视频
        _say("传视频")
        upload_file(form, media["video"], os.path.basename(job.get("file") or "v.mp4"),
                    "video/mp4", 'input[type=file]')
        wait_upload_done(page, form)
        out["steps"].append("uploaded")

        # ② 封面（整批共用那张图；有就换，没有就用平台自己截的帧）
        if media.get("cover"):
            _say("设封面")
            set_cover(page, form, media["cover"], "cover.png")
            out["steps"].append("cover")

        # ③ 文案：标题独占第一行，描述 + 话题放后面
        # 描述的第一行会被平台当成标题。话题若直接追加在标题后面，
        # 发出去就变成「标题#短剧」—— 标题被污染（2026-11-13 干跑实测）。
        desc = ""
        _say("写文案")
        topics = [str(t).strip().lstrip("#") for t in (job.get("topics") or []) if str(t).strip()]
        if topics:
            desc = ((desc + "\n") if desc else "") + " ".join("#" + t for t in topics)
        fill_desc(form, title, desc)
        fill_short_title(form, (job.get("short_title") or "").strip())
        out["steps"].append("text")

        # ④ 位置 / 剧集 / 视频标注
        if (job.get("location") or "").strip():
            pick_location(page, form, job["location"].strip(), job.get("location_city") or "")
            out["steps"].append("location")
        else:
            # 没填 ≠ 平台不带位置：页面会沿用上次城市，必须主动清
            clear_location(page, form)
            out["steps"].append("location_cleared")
        if (job.get("link_title") or "").strip():
            out["series"] = pick_series(page, form, job["link_title"].strip(), tmpl,
                                        job.get("link_kind") or "series")
            out["steps"].append("series")
        if (job.get("declare") or "").strip():
            out["declare"] = pick_declare(page, form, job["declare"].strip(), tmpl)
            out["steps"].append("declare")

        # ⑤ 定时
        _say("设定时")
        sched = set_schedule(page, form, plan_at)
        out["steps"].append("scheduled")
        out["scheduled_at"] = sched["scheduled_at"]

        # ⑤½ 发表前统一核对两轮：填了 ≠ 生效，平台会异步回填。
        # 必须在 on_before_publish / click_publish 之前，抛错是安全的。
        _say("核对填写结果")
        out["precheck"] = verify_form_twice(page, form)

        if dry_run:
            # 干跑：把填好的状态回读出来给界面看，**不点发表**。
            out["ok"] = True
            out["dry_run"] = True
            out["form"] = _form_state(form)
            return out

        # ⑥ 发表（不可逆）：先把「已经要点了」落盘，中途断电也知道
        # 这条可能已经交给平台，重启后绝不自动重来（wxworker 传回调）。
        _say("点发表")
        if on_before_publish:
            on_before_publish()
        click_publish(page, form, original=bool(job.get("original")))
        out["steps"].append("published")
        out["published"] = True
        # 从这里起**任何异常都不能往外抛**：一旦抛出，调度器会重试，
        # 而「发表」已经点过了 —— 重试就等于再发一条。回读失败只记结果，
        # 交给调用方去提示人工核对，绝不自动重来。
        try:
            # 先记下平台当时说了什么（成功 / 审核提示 / 报错），再导航去回读；
            # 否则回读没核对上时连现场都没留下。
            out["after_click"] = re.sub(
                r"\s+", " ",
                str(_ev(form, "() => (document.body.innerText || '')") or ""))[:500]
            out["verified"] = read_scheduled(page, title, sched["scheduled_at"])
        except Exception as exc:  # noqa: BLE001
            out["verified"] = {"found": False, "error": str(exc)[:200]}
        out["ok"] = bool(out["verified"].get("found"))
        return out

    return session.run(_go, timeout=2400)


def _append_editor(form, sel: str, text: str) -> bool:
    """往编辑器末尾追加文字（话题用）。"""
    return bool(_ev(form, """([sel, v]) => {
      const el = document.querySelector(sel);
      if (!el) return false;
      el.focus();
      try {
        const r = document.createRange();
        r.selectNodeContents(el);
        r.collapse(false);
        const s = window.getSelection();
        s.removeAllRanges();
        s.addRange(r);
        document.execCommand('insertText', false, v);
      } catch (err) {
        el.textContent = ((el.textContent || '') + ' ' + v);
        el.dispatchEvent(new Event('input', {bubbles: true}));
      }
      return true;
    }""", [sel, text]))


def _form_state(form) -> dict:
    """回读表单当前状态（干跑时给界面看）。"""
    try:
        return dict(_ev(form, """() => {
          const g = (s) => { const e = document.querySelector(s); return e ? (e.value || e.innerText || '') : ''; };
          return {
            desc: (document.querySelector('.input-editor')||{}).innerText || '',
            short: g('input[placeholder*="短标题"]'),
            at: g('input[placeholder="请选择发表时间"]'),
            pos: ((document.querySelector('.post-position-wrap')||{}).innerText || '')
                 .replace(/\\s+/g, ' ').trim().slice(0, 60),
            body: (document.body.innerText || '').replace(/\\s+/g, ' ').slice(0, 300),
          };
        }""") or {})
    except Exception:  # noqa: BLE001
        return {}


def publisher_parse(s: str | None):
    """把队列里的时间字符串解析成 datetime（认不准就返回 None）。"""
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(str(s)[:16], fmt[:16])
        except ValueError:
            continue
    return None
