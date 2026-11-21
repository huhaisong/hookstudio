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
        # 窗口开着就一直占着这个线程。轮询而不是 wait_for_event("close")：
        # 用户直接关整个浏览器窗口时后者会抛 TargetClosed，且脚本退出前
        # 没法让线程干净收尾（见 close_platform）。
        while not rec["stop"].wait(0.5):
            try:
                if page.is_closed():
                    break
            except Exception:  # noqa: BLE001  浏览器断开了，收工
                break
    except Exception as exc:  # noqa: BLE001
        if not sent:
            ready.put({"ok": False, "note": _human(exc)})
    finally:
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
    with _MU:
        old = _VIEWERS.get(aid)
        if old and not old["closed"].is_set():
            return {"ok": True, "aid": aid, "already": True, "logged_in": True,
                    "note": "窗口已经开着了，切过去看就行。"}

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
    th.start()
    try:
        res = ready.get(timeout=45)
    except queue.Empty:
        return {"ok": False, "aid": aid,
                "note": "窗口开了但迟迟没准备好，等一下看一眼屏幕；还不行就再点一次。"}
    return {"aid": aid, **res}


def viewer_open(aid: str) -> bool:
    """给回归 / 状态查询用：这个号的窗口是否还开着。"""
    with _MU:
        rec = _VIEWERS.get(aid)
        return bool(rec and not rec["closed"].is_set())


def close_platform(aid: str) -> bool:
    """让窗口线程干净退出并关掉窗口（工具 / 冒烟脚本用，界面上用户直接关窗即可）。"""
    with _MU:
        rec = _VIEWERS.get(aid)
    if not rec:
        return False
    rec["stop"].set()
    return True
