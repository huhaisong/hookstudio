"""用当前号打开视频号助手（免扫码的可见窗口）。

### 为什么另起一个浏览器，而不是把 Session 的窗口「显示出来」
`wxbot.Session` 一个号一个专用线程，平时是**无头**的，后台在拉数据 / 预取候选 /
自动发表都靠它。要把它变成可见，`ensure_mode(True)` 会直接 `_shutdown_browser()`
（headless 是启动参数，运行中改不了）——正在跑的活当场被打断；反过来，之后
任何后台任务 `run(visible=False)` 又会把用户正看着的窗口关掉。两边互相踩。

所以这里只**借一份 cookie**：在 Session 自己的线程里读 `context.cookies()`，
不切模式、不重启它的浏览器；再拿这份 cookie 起一个**独立、非持久**的可见
Chromium（新进程、临时 profile）。两者完全隔离：
  · 后台任务照常无头跑，不受影响
  · 用户关掉这个窗口就结束，Session 一动不动
  · 这个窗口也不占账号 profile，不会和后台实例抢目录

只带 cookie 不带 localStorage：视频号助手的登录态在 cookie 里，实测够用。
万一还是跳到登录页，就如实告诉前端「没带过去，在窗口里扫一下」，绝不假装免扫码。
"""
from __future__ import annotations

import os
import queue
import shutil
import threading
import time
import uuid

from . import core, wxbot

# Playwright `add_cookies()` 认得的字段。context.cookies() 在新版 Chromium 上
# 会多出 partitionKey / sourceScheme 之类，原样塞回去会直接抛参数错误。
_COOKIE_KEYS = ("name", "value", "url", "domain", "path", "expires",
                "httpOnly", "secure", "sameSite")
_SAME_SITE = ("strict", "lax", "none")

# aid -> 当前开着的窗口。重复点不要连开一堆窗口。
_VIEWERS: dict[str, dict] = {}
_MU = threading.Lock()


def _human(exc: Exception) -> str:
    s = str(exc or "").strip()
    if "Executable doesn't exist" in s or "chrome" in s.lower() and "not found" in s.lower():
        return "没找到 Chrome 或 Edge，装一个再来。"
    if "TargetClosed" in s or "has been closed" in s.lower():
        return "窗口被关掉了。"
    return s[:160] or "窗口没打开，重试一次。"


# ── 「窗口还开着吗」必须看活体，不能只信一条内存记录 ────────────────────
# 2026-11-21 事故：21:06 打开的窗口被用户关掉后，窗口线程卡在收尾（Playwright
# 的 ctx.close() / pw.stop() 在浏览器已退出时会无限等），`finally` 到不了 →
# `rec["closed"]` 永不置位、临时 profile 也删不掉。之后每次点「进入视频号助手」
# 都命中 already 分支，只弹「窗口已经开着了」而其实一个进程都没有，用户被彻底
# 挡住，只能重启软件。所以：
#   ① 线程活着就每 0.5s 打一次心跳，心跳停了＝线程死了/卡住了；
#   ② `finally` 第一行先把 closed 置位，再去做可能卡住的收尾；
#   ③ 判定记录失效的当场丢掉，允许重新开窗。
_HEARTBEAT_STALE = 3.0     # 秒：心跳停这么久就当线程已死
_STARTUP_GRACE = 75.0      # 秒：启动阶段没法打心跳，用「点了多久」兜（判完登录约 45s）
_PROFILE_STALE = 3600.0    # 秒：临时 profile 超过 1 小时视为陈旧目录


def _alive(rec: dict | None) -> bool:
    """这条记录对应的窗口线程是否还活着（活着＝用户可以切过去看的那个窗口）。"""
    if not rec or rec["closed"].is_set():
        return False
    if not rec.get("ready"):
        # 还在借 cookie / 起浏览器 / 等页面稳定，这段时间由启动宽限期兜住
        return (time.time() - rec["since"]) < _STARTUP_GRACE
    return (time.time() - (rec.get("beat") or 0)) < _HEARTBEAT_STALE


def _sweep_stale_profiles() -> None:
    """清掉以前卡死留下的临时 profile（浏览器进程已退，目录没人删）。

    只动 `_tmp_browser/viewer_*`，且 mtime 超过 1 小时；正在用的目录跳过。
    纯卫生动作，失败一律忽略，不耽误开窗。
    """
    root = os.path.join(core.DATA_DIR, "_tmp_browser")
    try:
        names = os.listdir(root)
    except OSError:
        return
    with _MU:
        live = {r.get("prof_dir") for r in _VIEWERS.values() if _alive(r)}
    now = time.time()
    for n in names:
        if not n.startswith("viewer_"):
            continue
        p = os.path.join(root, n)
        if p in live:
            continue
        try:
            if now - os.path.getmtime(p) < _PROFILE_STALE:
                continue
        except OSError:
            continue
        shutil.rmtree(p, ignore_errors=True)


def _clean_cookies(cookies) -> list[dict]:
    out: list[dict] = []
    for c in cookies or []:
        d = {k: c[k] for k in _COOKIE_KEYS if k in c and c[k] is not None}
        if not d.get("name") or "value" not in d:
            continue
        if not (d.get("domain") or d.get("url")):
            continue
        if not d.get("path") and d.get("domain"):
            d["path"] = "/"
        ss = d.get("sameSite")
        # Chrome 会拒掉 sameSite=None 但没带 secure 的 cookie；平台自己的
        # cookie 偶尔就是这个组合，去掉 sameSite 让浏览器按默认处理即可。
        if ss and (str(ss).lower() not in _SAME_SITE
                   or (str(ss).lower() == "none" and not d.get("secure"))):
            d.pop("sameSite", None)
        out.append(d)
    return out


def _grab_cookies(sess):
    """必须在账号自己的线程里跑：Playwright 同步对象绑线程。

    只 `_open()` 不 `ensure_mode`，所以不会把后台无头浏览器重启掉。
    """
    page = sess._open()
    return page.context.cookies()


def _viewer(aid: str, cookies: list[dict], ready: queue.Queue, rec: dict) -> None:
    """持有可见窗口，直到用户关掉。ready 只回一次（打开 + 判完登录）。"""
    pw = ctx = None
    prof_dir = None
    sent = False
    try:
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        # 临时 profile 必须落在 D 盘（core.DATA_DIR），不再让 Playwright 在
        # 系统 %TEMP%（C 盘）里建 playwright_chromiumdev_profile-* 目录。
        # 每次开窗用一个唯一名字，关窗后在 finally 里删掉。
        prof_dir = os.path.join(core.DATA_DIR, "_tmp_browser",
                                "viewer_" + uuid.uuid4().hex)
        os.makedirs(prof_dir, exist_ok=True)
        rec["prof_dir"] = prof_dir      # 清陈旧目录时要跳过它
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=prof_dir,
            executable_path=wxbot.find_browser(),
            headless=False,
            viewport={"width": 1360, "height": 900},
            locale="zh-CN",
            args=["--disable-blink-features=AutomationControlled"],
        )
        if cookies:
            ctx.add_cookies(cookies)
        # persistent context 自带一个空白页，直接用，避免多开一个空标签
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(wxbot.PLATFORM, wait_until="domcontentloaded", timeout=30000)
        # 只拿刚 domcontentloaded 的 URL 当结论会假阳性：cookie 不够时 SPA
        # 是过几秒才把二维码弹出来。给页面一点稳定时间，再双判据核对。
        try:
            page.wait_for_url(lambda u: "/login" not in (u or ""), timeout=8000)
        except Exception:  # noqa: BLE001  超时就是没跳回来，如实判未登录
            pass
        page.wait_for_timeout(2500)
        qr = wxbot.Session._qr_frame(page)   # 扫码 iframe 在＝没登进去
        # 只当诊断回传：窗口里到底显示了什么，真号冒烟时用它核对，
        # 不拿页面文字当登录判据（文案会随平台改版变）。
        try:
            body = page.evaluate(
                "() => ((document.body && document.body.innerText) || '')"
                ".replace(/\\s+/g, ' ').slice(0, 240)")
        except Exception:  # noqa: BLE001
            body = ""
        logged = "/login" not in (page.url or "") and qr is None
        ready.put({"ok": True, "logged_in": logged, "qr": bool(qr),
                   "body": body, "cookies": len(cookies),
                   "url": (page.url or "")[-120:]})
        sent = True
        rec["ready"] = True          # 从这里开始算心跳，不再走启动宽限期
        rec["beat"] = time.time()
        # 窗口开着就一直占着这个线程。轮询而不是 wait_for_event("close")：
        # 用户直接关整个浏览器窗口时后者会抛 TargetClosed，且脚本退出前
        # 没法让线程干净收尾（见 close_platform）。
        while not rec["stop"].wait(0.5):
            # 心跳：线程还活着。卡在 Playwright 同步调用上时心跳会停，
            # open_platform 据此判死、允许重开，不会被一条僵尸记录永远挡住。
            rec["beat"] = time.time()
            try:
                if page.is_closed():
                    break
            except Exception:  # noqa: BLE001  浏览器断开了，收工
                break
    except Exception as exc:  # noqa: BLE001
        if not sent:
            ready.put({"ok": False, "note": _human(exc)})
    finally:
        # 先落地状态，再做可能卡住的收尾（ctx.close / pw.stop 在浏览器已退出时
        # 会无限等，卡住也不能让这条记录继续挡着用户）。
        rec["closed"].set()
        try:
            if ctx is not None:
                ctx.close()
        except Exception:  # noqa: BLE001  用户先把窗口关了，close 会抛，正常
            pass
        try:
            if pw is not None:
                pw.stop()
        except Exception:  # noqa: BLE001
            pass
        # 浏览器进程退出后再删临时 profile，避免占着文件删不掉
        if prof_dir:
            shutil.rmtree(prof_dir, ignore_errors=True)
        rec["closed"].set()
        with _MU:
            if _VIEWERS.get(aid) is rec:
                _VIEWERS.pop(aid, None)


def open_platform(aid: str) -> dict:
    """打开当前号的视频号助手，同步返回「窗口开没开 / 是不是免扫码」。"""
    if wxbot.headless():
        return {"ok": False, "aid": aid,
                "note": "现在是调试用的无头模式，不弹窗口。正常打开软件时点这里会直接弹出已登录的视频号助手。"}
    _sweep_stale_profiles()
    with _MU:
        old = _VIEWERS.get(aid)
        if old is not None and _alive(old):
            if not old.get("ready"):
                return {"ok": True, "aid": aid, "already": True, "starting": True,
                        "note": "窗口正在打开，等一两秒看一眼屏幕。"}
            return {"ok": True, "aid": aid, "already": True,
                    "note": "窗口已经开着了，切过去看就行。"}
        if old is not None:
            # 记录还在但线程已经死了（窗口被关 / 卡在收尾）：不能让它继续挡着，
            # 叫旧线程收工、丢掉这条，下面重新开一个。
            old["stop"].set()
            _VIEWERS.pop(aid, None)

    try:
        # 号正忙（发表 / 采样 / 预取）时这里会排队，25 秒等不到就如实说，
        # 绝不为了开窗口去打断后台任务。
        cookies = _clean_cookies(wxbot.session(aid).call(_grab_cookies, timeout=25))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "aid": aid,
                "note": "这个号正忙或登录态读不到：" + _human(exc) + " 等它跑完再点一次。"}

    ready: queue.Queue = queue.Queue()
    rec = {"closed": threading.Event(), "stop": threading.Event(),
           "since": time.time()}
    with _MU:
        _VIEWERS[aid] = rec
    th = threading.Thread(target=_viewer, args=(aid, cookies, ready, rec),
                          name="wx-view-" + aid, daemon=True)
    try:
        th.start()
    except Exception as exc:  # noqa: BLE001  线程起不来就别留僵尸记录
        with _MU:
            if _VIEWERS.get(aid) is rec:
                _VIEWERS.pop(aid, None)
        rec["closed"].set()
        return {"ok": False, "aid": aid, "note": _human(exc)}
    try:
        res = ready.get(timeout=45)
    except queue.Empty:
        return {"ok": False, "aid": aid,
                "note": "窗口开了但迟迟没准备好，等一下看一眼屏幕；还不行就再点一次。"}
    return {"aid": aid, **res}


def viewer_open(aid: str) -> bool:
    """给回归 / 状态查询用：这个号的窗口是否还真的开着（不看僵尸记录）。"""
    with _MU:
        return _alive(_VIEWERS.get(aid))


def close_platform(aid: str) -> bool:
    """让窗口线程干净退出并关掉窗口（工具 / 冒烟脚本用，界面上用户直接关窗即可）。"""
    with _MU:
        rec = _VIEWERS.get(aid)
    if not rec:
        return False
    rec["stop"].set()
    return True
