"""视频号助手自动化底座：浏览器、账号 profile、扫码登录。

三层分工，改版时只动一处：
  `wxbot.py`  怎么驱动浏览器（本文件）—— 起进程、给页面、开登录页、判登录
  `wxpub.py`  发布页怎么填（上传/文案/定时）
  `wxsel.py`  所有选择器集中在这里

### 为什么不用 playwright 自带的 Chromium
安装包已经不小，Chromium 再压进去要 +120~150MB。而客户机上 Chrome/Edge
几乎必然存在（Windows 自带 Edge），所以用 `executable_path` 指本机浏览器，
playwright 只当驱动。找不到浏览器时给一句人话让他去装，不要报 Playwright 的英文错。

### 为什么一个账号一个专用线程
playwright 的同步对象**绑在创建它的线程上**，换线程用直接抛错；而 FastAPI 的
同步路由跑在线程池里，每次请求未必落在同一个线程。所以每个账号配一个专属线程
+ 命令队列：谁调都行，命令统一在那个线程里跑，结果用 Future 回传。

### 登录页的实测结构（2026-11-13 真实页面校准）
- 打开 `channels.weixin.qq.com/platform` 会跳到 `/login.html`
- 二维码不在主文档，在 iframe：`open.weixin.qq.com/connect/qrconnect?...`
- 2026-11-14 起不再截码：扫码一律弹**可见窗口**让用户对着平台自己的登录页
  扫（快捷登录卡/换码/过期刷新平台自己会处理），这边只判登录。
"""
from __future__ import annotations

import asyncio
import os
import queue
import shutil
import subprocess
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutTimeout

from . import core

PLATFORM = "https://channels.weixin.qq.com/platform"
MAX_ACCOUNTS = 10            # 一台电脑最多 10 个号（用户 2026-11-13 定的）
IDLE_CLOSE_SEC = 30 * 60     # 空闲半小时自动关浏览器，省内存；下次用会自己再起
DEFAULT_MS = 20000

# 本机浏览器的常见位置。顺序即优先级：Chrome > Edge。
_BROWSER_PATHS = (
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
)


class WxError(Exception):
    """给前端看的人话错误（路由层转 400）。"""


def headless() -> bool:
    """调试用：`WX_HEADLESS=1` 时不弹窗口（跑回归必须开，否则会抢鼠标）。"""
    return (os.environ.get("WX_HEADLESS") or "").strip() in ("1", "true", "yes")


def find_browser() -> str:
    for raw in _BROWSER_PATHS:
        p = os.path.expandvars(raw)
        if p and os.path.isfile(p):
            return p
    raise WxError("这台电脑上没找到 Chrome 或 Edge。装一个 Chrome 再来，装完不用设置，程序自己认。")


def profile_of(aid: str) -> str:
    """每个账号一个独立浏览器目录：cookie/登录态都在这，删账号就连根一起删。"""
    return os.path.join(core.DATA_DIR, "wxprofiles", aid)


# ── 一个账号一个线程 ─────────────────────────────────────────────────────
class Session:
    """把一个账号的浏览器封在自己的线程里。所有方法线程安全。"""

    def __init__(self, aid: str):
        self.aid = aid
        self._q: queue.Queue = queue.Queue()
        self._th: threading.Thread | None = None
        self._mu = threading.Lock()
        self._ctx = None
        self._pw = None
        self._page = None
        self._busy = False
        self.last_used = time.time()
        # 浏览器要不要有窗口。**默认无头**：登录之后的活（预取候选、读数据、
        # 上传设封面）都在后台跑，弹窗口会抢用户焦点、让人以为程序在乱弹东西
        # （2026-11-13 用户要求「用户无感」）。只有扫码那一步必须可见——
        # 用户要亲眼看到程序在操作哪个浏览器。
        self._visible = False

    # 线程生命周期 --------------------------------------------------------
    def _ensure_thread(self) -> None:
        with self._mu:
            if self._th and self._th.is_alive():
                return
            self._th = threading.Thread(target=self._loop, name=f"wx-{self.aid}", daemon=True)
            self._th.start()

    def _loop(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            fn, fut = item
            try:
                fut.set_result(fn(self))
            except BaseException as exc:  # noqa: BLE001  业务异常要原样回传给调用方
                if not fut.done():
                    fut.set_exception(exc)
        self._shutdown_browser()

    def call(self, fn, timeout: float = 120):
        """把 fn(session) 丢进该账号的线程执行，同步等结果。"""
        self._ensure_thread()
        fut: Future = Future()
        self._q.put((fn, fut))
        try:
            return fut.result(timeout=timeout)
        except FutTimeout:
            raise WxError("浏览器等了太久没回应。点「重试」会自动重开浏览器再来一次；"
                          "老是这样就把这个号删掉重新扫码登录。")

    def close(self) -> None:
        try:
            self._q.put_nowait(None)
        except Exception:  # noqa: BLE001
            pass

    def wait_dead(self, timeout: float = 8.0) -> bool:
        """等浏览器真的关干净。Windows 上文件被 Chrome 占着就删不掉，
        所以要等线程退出后再删 profile（否则 rmtree 静默半失败）。"""
        th = self._th
        if not th:
            return True
        th.join(timeout)
        return not th.is_alive()

    # 浏览器 --------------------------------------------------------------
    def _shutdown_browser(self) -> None:
        for obj, name in ((self._ctx, "context"), (self._pw, "pw")):
            if obj is None:
                continue
            try:
                obj.close() if name == "context" else obj.stop()
            except Exception:  # noqa: BLE001
                pass
        self._ctx = self._pw = self._page = None
        # **摘掉旧实例留在线程上的「运行中循环」标记**（2026-11-14 扫码卡
        # 实测的根因）。playwright 的每次同步调用结束后都会把旧实例的事件
        # 循环标记成本线程的 running loop（_greenlets.py:131）——一个线程
        # 同时只能有一个活着的 sync 实例。stop() 半路炸掉时这个标记没人清
        # （__exit__ 的 _exit_was_called 还会挡住二次收尾），下一次
        # sync_playwright().start() 一查线程里有 running loop 就报
        # 「Sync API inside the asyncio loop」，重试多少次都穿不过——线程
        # 等于被毒死。旧实例在这里已经弃用，把标记摘掉对它无影响（它自己
        # 每次调用还会再标回去），但对新实例是生死攸关。
        asyncio._set_running_loop(None)

    def ensure_mode(self, visible: bool) -> None:
        """切可见/无头。**只在真的要换模式时才重启浏览器**。

        为什么要分两种：登录之后所有操作都该在后台悄悄跑（用户要求无感），
        但扫码那一步必须看得见。所以窗口是「按需出现」的，不是常驻的。
        """
        visible = bool(visible)
        if self._visible == visible and self._ctx is not None:
            return
        self._visible = visible
        if self._ctx is not None:
            # 模式变了只能重开浏览器：Playwright 的 headless 是启动参数，
            # 运行中改不了。profile 里的登录态不受影响。
            self._shutdown_browser()
            time.sleep(0.35)   # 给旧实例内部循环的收尾留出时间（见 _open 重试注释）

    def _kill_stale_chrome(self) -> None:
        """强杀占着本号 profile 的孤儿 Chrome。

        实例被强杀（重启/崩溃）时，它拉起的 Chrome 浏览器成了孤儿进程，还锁着
        wxprofiles/<aid> 这个 profile 目录——新实例 launch_persistent_context
        一启动就 TargetClosedError 退出（2026-11-14 实测：当晚所有重登/扫码
        全挂在这上面，快捷登录卡只是表象）。

        用 PowerShell 按命令行里的**账号 ID**（纯十六进制，不受中文路径的
        编码影响）精确匹配击杀，绝不误伤用户自己开的 Chrome；输出强制
        utf-8/replace，避免 wmic 的 GBK 输出把解码搞崩。
        """
        aid = (self.aid or "").lower()
        if not aid:
            return
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                 "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=30).stdout or ""
        except Exception:  # noqa: BLE001  PowerShell 不可用就跳过这层保护
            return
        for line in out.splitlines():
            low = line.lower()
            if f"wxprofiles{os.sep}{aid}" in low or f"wxprofiles/{aid}" in low \
                    or f"wxprofiles\\{aid}" in low:
                pid = line.split("\t")[0].strip()
                if pid.isdigit():
                    subprocess.run(["cmd", "/c", f"taskkill /F /T /PID {pid}"],
                                   capture_output=True, timeout=20)

    def _open(self):
        """（线程内）确保浏览器开着，返回 page。"""
        if self._ctx is not None:
            try:
                # **一个标签关了 ≠ 整个浏览器该关**。微信扫码成功后经常是新开
                # 一个标签（后台页），老标签（登录页）自己关掉；这时如果把整个
                # 浏览器关掉，刚登录上的那个标签也一起没了，登录态当场作废
                # （现象：扫完码界面永远没反应，刷新后浏览器里其实已经登录）。
                pages = [p for p in self._ctx.pages if not p.is_closed()]
                if pages:
                    if self._page is None or self._page.is_closed():
                        self._page = pages[0]
                    return self._page
            except Exception:  # noqa: BLE001
                pass
            # 浏览器整个没了（用户关了窗口）→ 重新起一个（profile 里登录态还在）
            self._shutdown_browser()
        from playwright.sync_api import sync_playwright

        # 上一个实例被强杀时拉起的孤儿 Chrome 还锁着本号 profile，先清掉再启动
        self._kill_stale_chrome()
        prof = profile_of(self.aid)
        os.makedirs(prof, exist_ok=True)
        # 启动失败要**连着清场重试**（2026-11-14 实测）：launch 报
        # TargetClosedError 时常把 Chrome 留成孤儿——进程活着、profile 的
        # lockfile 被它握着，下一次启动秒退，这个号就永远起不来了。所以每一轮
        # 失败都要收尸（杀孤儿）+ 删残留 lockfile 再试，而不是只重试一次。
        # 同线程「关旧实例 → 起新实例」以前还报「Sync API inside the asyncio
        # loop」——真凶是旧实例 stop() 半路炸时留在线程上的「运行中循环」标记
        # （不是时序竞态，重试永远穿不过；标记现在在 _shutdown_browser 里
        # 每次都摘），这里只剩孤儿 Chrome / lockfile 一类需要清场重试的情况。
        last = None
        for _ in range(3):
            try:
                self._pw = sync_playwright().start()
                self._ctx = self._pw.chromium.launch_persistent_context(
                    user_data_dir=prof,
                    executable_path=find_browser(),
                    headless=(True if headless() else (not self._visible)),
                    viewport={"width": 1280, "height": 900},
                    locale="zh-CN",
                    # 让自动化特征弱一点；只影响 navigator.webdriver，不动其它
                    args=["--disable-blink-features=AutomationControlled",
                          # 允许页面 fetch 127.0.0.1（2026-11-13 踩过）：
                          # 发表时页面要从本机取视频文件（几十上百兆），base64 塞不进
                          # evaluate（8MB 上限），只能让页面自己去本机 fetch。
                          # 但 Chrome 130+ 的 Local Network Access 会以
                          # 「Permission was denied for this request to access the
                          # loopback address space」把公网页面 → 127.0.0.1 全拦掉
                          # ——网页端拿不到许可，这条路在网页里根本走不通。
                          # 这个浏览器是我们自己拉起来的自动化实例，只干这一件事，
                          # 所以把这个检查关掉。实测这一条 flag 就够。
                          "--disable-features=LocalNetworkAccessChecks"],
                )
                last = None
                break
            except Exception as exc:  # noqa: BLE001
                last = exc
                # 失败的启动可能已经把 Chrome 拉起来了（孤儿），收尸 + 清锁再试
                self._shutdown_browser()
                self._kill_stale_chrome()
                self._drop_lockfile()
                time.sleep(0.6)
        if last is not None:
            raise last
        self._ctx.set_default_timeout(DEFAULT_MS)
        pages = self._ctx.pages
        self._page = pages[0] if pages else self._ctx.new_page()
        return self._page

    def _drop_lockfile(self) -> None:
        """删掉 profile 里残留的 lockfile。Chrome 被硬杀时来不及自己删，
        留着它下一次 launch_persistent_context 就秒退 TargetClosedError
        （2026-11-14 实测：孤儿进程被杀掉后 lockfile 还在，这个号一直起不来）。
        删不掉（文件还被握着）就留给下一轮重试再删——上面刚跑过收尸。"""
        try:
            os.remove(os.path.join(profile_of(self.aid), "lockfile"))
        except OSError:
            pass

    # 内部：判登录 --------------------------------------------------------
    @staticmethod
    def _logged_in(page) -> bool:
        try:
            u = page.url or ""
        except Exception:  # noqa: BLE001
            return False
        return "channels.weixin.qq.com" in u and "/login" not in u

    @staticmethod
    def _qr_frame(page):
        for f in page.frames:
            if "qrconnect" in (f.url or ""):
                return f
        return None

    def _is_logged_in(self, page) -> bool:
        """**登录态判定的唯一可靠判据**（2026-11-16 收敛）。

        三条子判据，按可靠度从高到低：
          ① 二维码框（qrconnect）挂出来 = 铁证「没登录」。被平台踢下线后登录墙
            是 SPA 自己跳的，地址栏常常还没变（仍停在 /platform、不含 /login），
            残留 DOM 也还在——光看地址栏/DOM 会把「没登录」误判成「已登录」。
            所以二维码框优先于一切，一出现就判「没登录」。
          ② 地址栏不在 channels.weixin.qq.com 或进了 /login = 「没登录」。
          ③ 地址栏判「在线」了，还要看 DOM 有没有后台标记元素。

        关键（2026-11-16 用户报「大海2 明明登录了却要重新登录」）：③不能当成
        「必须为 true」的硬门槛。视频号后台页是 SPA，hard 导航后 1.5 秒内冷加载
        还没渲染出 .finder-nickname 等元素，此时 DOM 判「没登录」是**暂时的假象**，
        不代表真没登录。所以 DOM 只在「地址栏判不出（域名/路由改版）」时才作为
        补充，地址栏明确在线时不能被 DOM 的暂时空态推翻。
        """
        if self._qr_frame(page) is not None:
            return False
        if not self._logged_in(page):
            # 地址栏判「没登录」时，DOM 作补充：域名/路由改版时地址栏可能不认，
            # 但后台标记元素还在，说明其实在线。
            return self._dom_logged_in(page)
        # 地址栏明确在线（channels 且不含 /login），这就够了。SPA 冷加载中
        # DOM 元素暂时没渲染出来是正常现象，不能据此判「没登录」。
        return True

    def _pick_page(self):
        """挑一个「已经进了后台」的标签页。

        微信扫码成功后经常**在新标签**打开后台，老标签（登录页）原地不动，
        甚至自己关掉。只看 `self._page` 会永远判成没登录 —— 用户明明扫了，
        界面一直转圈（踩过）。
        """
        page = self._open()
        try:
            for p in self._ctx.pages:
                try:
                    if p.is_closed():
                        continue
                except Exception:  # noqa: BLE001
                    continue
                if self._is_logged_in(p):
                    self._page = p
                    return p
        except Exception:  # noqa: BLE001
            pass
        return page

    # ── 对外：扫码 ───────────────────────────────────────────────────────
    def _logout(self) -> None:
        """把这个号的登录态清掉（下次打开要重新扫码）。

        为什么需要：用户点「重登」是想**换一个微信重新登录**。如果浏览器里
        还留着上一次的登录态，平台会弹快捷登录卡，一键就把旧微信又登回来了，
        界面看上去就是「点了重登没反应」（2026-11-13 踩过）。
        """
        try:
            self._ctx.clear_cookies()
        except Exception:  # noqa: BLE001
            pass
        try:
            page = self._open()
            page.goto("https://channels.weixin.qq.com/logout", wait_until="domcontentloaded",
                      timeout=15000)
        except Exception:  # noqa: BLE001  没有这个页面也不影响，cookie 已经清了
            pass

    def open_login(self, force: bool = False, reopen: bool = False) -> dict:
        """把这个号的登录页在**可见浏览器窗口**里弹出来（2026-11-14 用户定：
        登录一律直接弹窗口手动做，界面里不再显示二维码）。
        平台自己的登录页自带快捷登录卡、自动换码、过期刷新，用户对着真窗口
        操作最直接；登没登上由前端轮询 login-state 判断，这个方法只负责开窗口。
        force=True（重登）＝先把旧登录态清掉再开登录页，保证换一个微信登录。
        reopen=True（再开一次窗口）＝用户说上一个窗口已经没了，**不信任任何
        残留状态**，先把浏览器整个关掉再起一个可见窗口。为什么不能复用 force
        或 `_open()` 的现有逻辑：Windows 上用户关掉窗口后 Chrome 进程常常还
        残留着（`about:blank` 空白标签），`_open()` 一看「还有活着的标签页」
        就复用它、不重新 launch，`page.goto` 在没了窗口的后台页上静默执行，
        屏幕上一个窗口都不弹——用户点「再开一次」就是没反应（2026-11-16 实测）。

        换可见模式必须**在账号线程里**做：这个方法是 API 线程直接调进来的，原来
        在外面跨线程 `_shutdown_browser()`，而 sync Playwright 不是线程安全的
        ——跨线程 stop 炸一半被吞，旧实例的事件循环残留在账号线程上，紧接着
        起浏览器就报「Sync API inside the asyncio loop」（2026-11-14 实测）。
        排进账号线程还顺带消掉另一层竞争：无头验号/预取还在跑时
        不会再被当场拆浏览器。
        """
        def _go(_s):
            if reopen:
                # 重开窗口：直接丢弃旧实例，别让 `_open()` 去复用残留的空白页。
                self._visible = True
                self._shutdown_browser()
                time.sleep(0.35)
            self.ensure_mode(True)
            page = self._pick_page()
            # 判「已经登录」统一走可靠判据（2026-11-16）：二维码框是登录墙铁证，
            # 残留旧标签页会被 _is_logged_in 一起识破，不再光看地址栏。
            if self._is_logged_in(page) and not force:
                return {"logged_in": True}
            if force and self._logged_in(page):
                # 用户明确要换一个微信重新登录：先登出，别让快捷登录卡
                # 一键就把旧微信又登回来。
                self._logout()
                page = self._open()
            try:
                url = page.url or ""
            except Exception:  # noqa: BLE001
                url = ""
            if force or "channels.weixin.qq.com" not in url:
                # 刚登出过要重新走登录入口；窗口开着但没停在助手上（刚重启/
                # 停在空白页）也要带到登录页。已经在登录墙上就别动了——
                # 用户可能正登着。
                try:
                    page.goto(PLATFORM, wait_until="domcontentloaded", timeout=45000)
                    self._settle(page)
                except Exception:  # noqa: BLE001  导航失败不致命，窗口反正已经开了
                    pass
            return {"logged_in": bool(self._is_logged_in(page))}
        return self.call(_go, timeout=150)

    # ── 对外：查登录状态 / 取昵称头像 ─────────────────────────────────────
    @staticmethod
    def _dom_logged_in(page) -> bool:
        """地址栏判不出来时，再问一次页面本身。

        为什么加这一层：登录成功后的后台页有平台自己的标记元素，登录页没有。
        只信地址栏的话，改版时地址一变（换域名、换路由、带 query）就会把
        「已经登录」判成「没登录」，用户看到的现象就是「我明明扫了码，
        界面一直转圈」。
        """
        try:
            return bool(page.evaluate("""() => !!document.querySelector(
                '.finder-nickname, .finder-avatar, .finder-nickname-editor, '
              + '[class*="finder-nickname"], .platform-home, .home-entry')"""))
        except Exception:  # noqa: BLE001
            return False

    def _profile(self, page) -> dict:
        """取昵称头像。取不到不算错误——账号名还能用用户自己填的。"""
        info = {"logged_in": True, "wx_name": "", "avatar": ""}
        try:
            info.update(page.evaluate("""() => {
              const pick = (sels) => { for (const s of sels) {
                  const e = document.querySelector(s);
                  const t = e && (e.innerText || e.textContent || '').trim();
                  if (t && t.length <= 24) return t;
                } return ''; };
              const img = (sels) => { for (const s of sels) {
                  const e = document.querySelector(s);
                  if (e && e.src) return e.src;
                } return ''; };
              return {
                wx_name: pick(['.finder-nickname', '.nickname', '.account-name', '[class*=nickname]']),
                avatar: img(['.finder-avatar img', '.avatar img', '[class*=avatar] img']),
              };
            }"""))
        except Exception:  # noqa: BLE001
            pass
        return info

    def _settle(self, page) -> None:
        """导航后等登录墙跳转尘埃落定，再判。

        为什么不能只等 1.5 秒：被平台踢下线后，导航到 /platform 会先停在
        /platform（登录墙的跳转脚本还没跑），约 1 秒后才跳到 /login.html。这段
        「空窗期」里地址栏还是 /platform、不含 /login，光看地址栏会把「没登录」
        误判成「在线」（2026-11-16 狼王华少反复开/关登录窗就是它）。
        等一个足够长的固定时长，让跳转必然发生：真在线就停在 /platform，真没
        登录就落到 login.html/二维码框。判定本身仍交给 `_is_logged_in`。
        """
        try:
            page.wait_for_timeout(2500)
        except Exception:  # noqa: BLE001
            pass

    def _state(self, hard: bool = False) -> dict:
        """查登录态，统一走 `_is_logged_in` 一条判据（2026-11-16 收敛）。

        轻量（hard=False）：挑一个「看着在线」的标签页，直接判。
        硬查（hard=True）：对「看着像登录」的旧标签页做一次真导航再判——
        残留标签页会把死会话误报成在线（2026-11-14 实测），cookie 活着就停在
        后台页、死了会被弹回登录墙，导航一次才算数。

        一条硬规则：**登录确认进行中绝不导航**（地址在 oauth 回调、或码框还挂着
        = 用户手机刚点了确认）——这时 goto 会让这次登录当场作废。所以看到这两样
        时，无论 hard 与否都直接按当前状态返回，交给下一轮轮询出结论。
        """
        page = self._pick_page()
        if self._is_logged_in(page):
            # 看着在线。硬查的话再导航验证一次（防残留标签页骗人），
            # 但登录确认进行中不能动。
            if hard:
                mid = "/oauth" in (page.url or "") or self._qr_frame(page) is not None
                if not mid:
                    try:
                        page.goto(PLATFORM, wait_until="domcontentloaded", timeout=45000)
                        self._settle(page)
                    except Exception:  # noqa: BLE001
                        pass
                    if not self._is_logged_in(page):
                        return {"logged_in": False}
            return self._profile(page)
        # 看着没登录。页面可能停在登录页（二维码由前端触发取），也可能停在
        # 别的网址（刚重启/空白页）。后者导航到平台首页再判一次，拿导航后的
        # 真实结论——但登录页上绝不 goto/reload（用户可能刚扫完正要点确认）。
        try:
            if "channels.weixin.qq.com" not in (page.url or ""):
                page.goto(PLATFORM, wait_until="domcontentloaded", timeout=45000)
                self._settle(page)
        except Exception:  # noqa: BLE001
            pass
        if self._is_logged_in(page):
            return self._profile(page)
        return {"logged_in": False}

    def is_visible(self) -> bool:
        """这个号当前是不是「可见窗口」模式（用户正在对着它扫码/操作）。

        对账复查 online 号时要用它：可见窗口开着 = 用户正在登录或看这个号，
        无头复查一去导航就会把窗口顶掉，把正在扫的码弄丢。所以这类号跳过复查。
        """
        return bool(self._visible)

    def state(self, hard: bool = False) -> dict:
        """查登录态。**这一步不许切无头**：它属于扫码流程，浏览器得留着。

        hard=True：对「看着像登录」的旧标签页也做一次真导航验证，用来回答
        「这个号现在到底还能不能用」。代价是一次真实页面跳转，别放进高频轮询。
        """
        return self.call(lambda _s: self._state(hard), timeout=45)

    # ── 对外：诊断（只在排查「登录了没反应」时用）────────────────────────
    def _diag(self) -> dict:
        """把浏览器内部真实状态倒出来：有哪些标签、停在哪个网址、帧列表。

        为什么要有这个：登录失败时外面只能看到 `logged_in: false`，分不清是
        「窗口被关掉了」「停在别的页上」还是「微信拦了自动化」。没这个只能猜。
        登录页本身用户就看得见，这里不再去抓页面上的字。
        """
        page = self._pick_page()
        out: dict = {"url": "", "pages": [], "frames": []}
        try:
            out["url"] = page.url or ""
        except Exception:  # noqa: BLE001
            pass
        try:
            for p in self._ctx.pages:
                out["pages"].append((p.url or "")[:160])
        except Exception:  # noqa: BLE001
            pass
        try:
            for f in page.frames:
                out["frames"].append((f.url or "")[:160])
        except Exception:  # noqa: BLE001
            pass
        return out

    def diag(self) -> dict:
        return self.call(lambda _s: self._diag(), timeout=40)

    # ── 对外：给发布流程直接拿 page（必须在自己的线程里用）──────────────
    def run(self, fn, timeout: float = 900, visible: bool = False):
        """在账号线程里执行 fn(page)，用于上传/填写这类长动作。

        `visible=False`（默认）＝**无头跑**：不弹窗口、不抢焦点，用户全程无感
        （2026-11-13 用户要求）。只有扫码那种「用户要看着」的场景才传 True。
        """
        def _wrap(s):
            s.ensure_mode(visible)
            page = s._open()
            return fn(page)
        return self.call(_wrap, timeout=timeout)


# ── 会话注册表 ───────────────────────────────────────────────────────────
_SESS: dict[str, Session] = {}
_MU = threading.Lock()


def session(aid: str) -> Session:
    with _MU:
        s = _SESS.get(aid)
        if s is None:
            s = _SESS[aid] = Session(aid)
        return s


def _rm_later(path: str) -> None:
    """把「删几百个文件的浏览器目录」挪到后台线程，并容忍文件被占用。

    为什么不在请求里删：Chrome 的 profile 有 260+ 个文件，Windows 上同步删
    要好几秒，接口就会转圈；而且刚关掉的 Chrome 还占着句柄，第一次删必失败。
    改名是瞬时的，改名之后慢慢删，删不掉的下次启动再清（`purge_trash`）。
    """
    def _go():
        for _ in range(6):
            try:
                shutil.rmtree(path)
                return
            except BaseException:  # noqa: BLE001  沙箱/占用都可能抛，重试就行
                time.sleep(1.0)
    threading.Thread(target=_go, name="wx-rm", daemon=True).start()


def purge_trash() -> int:
    """清掉上次没删干净的 `*.trash-*`。启动时调一次。"""
    root = os.path.join(core.DATA_DIR, "wxprofiles")
    n = 0
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    for name in names:
        if ".trash-" not in name:
            continue
        p = os.path.join(root, name)
        if os.path.isdir(p):
            _rm_later(p)
            n += 1
    return n


def drop(aid: str, remove_profile: bool = False) -> None:
    """关掉这个账号的浏览器；remove_profile=True 时连登录态一起删（删账号用）。"""
    with _MU:
        s = _SESS.pop(aid, None)
    if s:
        s.close()
        if remove_profile:
            s.wait_dead()
    if remove_profile:
        p = profile_of(aid)
        # 只删 data/wxprofiles/<aid>，绝不递归到别处
        if not (os.path.isdir(p) and os.path.abspath(p).startswith(os.path.abspath(core.DATA_DIR))):
            return
        # 先改名（瞬时、不挡请求），再后台慢慢删；删不掉留到下次启动清
        trash = p + ".trash-" + str(int(time.time()))
        try:
            os.rename(p, trash)
        except OSError:
            trash = p   # 改不了名（文件还被占着）就直接后台硬删
        _rm_later(trash)


def close_all() -> None:
    for aid in list(_SESS):
        drop(aid)


def profile_size(aid: str) -> int:
    """profile 占了多少字节（界面上让人知道磁盘去哪了）。"""
    total = 0
    for root, _dirs, files in os.walk(profile_of(aid)):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total
