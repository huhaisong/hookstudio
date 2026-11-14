"""视频号助手自动化底座：浏览器、账号 profile、扫码登录。

三层分工，改版时只动一处：
  `wxbot.py`  怎么驱动浏览器（本文件）—— 起进程、给页面、截二维码、判登录
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
- 二维码本身是 `img[src*="/connect/qrcode/"]`，160×160
- 对 iframe 元素整块截图最干净（208×208，白底无遮挡，实测可扫）
"""
from __future__ import annotations

import base64
import os
import queue
import shutil
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

        prof = profile_of(self.aid)
        os.makedirs(prof, exist_ok=True)
        # 同线程「关旧实例 → 起新实例」偶发撞上旧实例内部循环的收尾：
        # 新实例一 start 就报「Sync API inside the asyncio loop」
        # （2026-11-14 实测：无头预取 → 可见扫码连着切模式就撞）。
        # 旧循环的退出是毫秒级，小间隔重试三次即可穿过。
        last = None
        for _ in range(3):
            try:
                self._pw = sync_playwright().start()
                last = None
                break
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(0.4)
        if last is not None:
            raise last
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
        self._ctx.set_default_timeout(DEFAULT_MS)
        pages = self._ctx.pages
        self._page = pages[0] if pages else self._ctx.new_page()
        return self._page

    # 内部：判登录 --------------------------------------------------------
    @staticmethod
    def _logged_in(page) -> bool:
        try:
            u = page.url or ""
        except Exception:  # noqa: BLE001
            return False
        return "channels.weixin.qq.com" in u and "/login" not in u

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
                if self._logged_in(p):
                    self._page = p
                    return p
        except Exception:  # noqa: BLE001
            pass
        return page

    @staticmethod
    def _qr_frame(page):
        for f in page.frames:
            if "qrconnect" in (f.url or ""):
                return f
        return None

    def _wait_qr(self, page, seconds: float = 25):
        end = time.time() + seconds
        while time.time() < end:
            if self._logged_in(page):
                return "in"
            fr = self._qr_frame(page)
            if fr:
                try:
                    if fr.locator('img[src*="/connect/qrcode/"]').count():
                        return fr
                except Exception:  # noqa: BLE001  页面正在跳转时查元素会抛，正常
                    pass
            page.wait_for_timeout(400)
        return None

    # ── 对外：扫码 ───────────────────────────────────────────────────────
    def _logout(self) -> None:
        """把这个号的登录态清掉（下次打开要重新扫码）。

        为什么需要：用户点「重登」是想**换一个微信重新登录**。如果浏览器里
        还留着上一次的登录态，`_get_qr` 会直接回「已登录 + 空码」，前端拿不到
        二维码，界面看上去就是「点了没反应」（2026-11-13 踩过）。
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

    def _get_qr(self, refresh: bool = False, force: bool = False) -> dict:
        """取一张二维码。

        两种模式，区别是**会不会重新导航**：
        - `refresh=False`：页面上已经有二维码就原地截一张。用户可能刚扫完、iframe
          正在轮询确认，这时导航一下这次登录就作废了（现象：手机上显示已登录，
          这边永远没反应）。
        - `refresh=True`：明确要换一张（手动点「换一张」，或前端发现这张超过
          4 分钟快过期了）才 reload 重新出码。
        - `force=True`：「重登」= 一定要重新扫码。先清掉登录态再出码。
        """
        page = self._pick_page()
        if self._logged_in(page):
            if not force:
                return {"logged_in": True, "qr": ""}
            # 用户明确要换一个微信重新登录：先登出，再要一张新码。
            self._logout()
            page = self._open()
        try:
            url = page.url or ""
        except Exception:  # noqa: BLE001
            url = ""
        on_site = "channels.weixin.qq.com" in url
        if force:
            # 刚登出过，重新走一遍登录入口
            try:
                page.goto(PLATFORM, wait_until="domcontentloaded", timeout=45000)
            except Exception:  # noqa: BLE001
                pass
        elif refresh and on_site:
            try:
                page.reload(wait_until="domcontentloaded", timeout=45000)
            except Exception:  # noqa: BLE001
                pass
        elif not on_site:
            try:
                page.goto(PLATFORM, wait_until="domcontentloaded", timeout=45000)
            except Exception:  # noqa: BLE001  导航超时不致命，下面照样等二维码
                pass
        got = self._wait_qr(page, 25.0)
        fr = self._qr_frame(page) if got != "in" else None
        # 平台记住这个微信时，码框里先弹「快捷登录」卡（头像 + 一键确认，
        # 二维码收在后面）——点卡里的「使用其他头像、昵称或账号」把码换出来
        # （2026-11-14 用户反馈：要的是二维码，不是快捷登录）。
        end = time.time() + 12.0
        while not got and fr is not None and time.time() < end:
            try:
                if self._qr_img_visible(fr):
                    got = fr
                    break
                self._quick_login_switch(fr)
            except Exception:  # noqa: BLE001  跳转中查元素会抛，正常
                pass
            page.wait_for_timeout(400)
        if got == "in":
            self._page = page
            return {"logged_in": True, "qr": ""}
        if not got:
            raise WxError("二维码没出来。多半是网络卡了，点「重新获取」再来一次；"
                          "一直不行就检查这台电脑能不能打开视频号助手。")
        fr = got
        png = fr.frame_element().screenshot()
        return {"logged_in": False,
                "qr": "data:image/png;base64," + base64.b64encode(png).decode()}

    def qr(self, refresh: bool = False, force: bool = False,
           visible: bool = True) -> dict:
        # 扫码那一步必须看得见：用户要亲眼看到程序在操作哪个浏览器，
        # 而且微信的扫码页在无头模式下偶发不刷新二维码。
        # 「登录失效就地出码」（2026-11-14）想不弹窗：先无头截一张，截不到
        # （无头下偶发不出码）再回退可见窗口重试一次。
        if visible:
            self.ensure_mode(True)
            return self.call(lambda _s: self._get_qr(refresh, force), timeout=150)

        def _fallback(_s):
            self.ensure_mode(True)   # 会重启浏览器弹窗；截不到码才走到这
            return self._get_qr(refresh, force)
        try:
            out = self.call(lambda _s: self._get_qr(refresh, force), timeout=120)
            out["headless"] = True
            return out
        except WxError:
            out = self.call(_fallback, timeout=150)
            out["fallback"] = True
            return out

    @staticmethod
    def _qr_img_visible(fr) -> bool:
        """码框里的二维码是不是真的摆出来了（快捷登录卡盖着时 img 在 DOM 里
        但不可见，只数 count 会误判）。"""
        try:
            img = fr.locator('img[src*="/connect/qrcode/"]')
            return bool(img.count()) and img.first.is_visible()
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _quick_login_switch(fr) -> None:
        """快捷登录卡（平台记住了这个微信）→ 点「使用其他头像、昵称或账号」
        把二维码换出来。找不到就不动（外层照常重试/报错）。"""
        try:
            link = fr.locator("text=使用其他头像")
            if link.count() and link.first.is_visible():
                link.first.click()
        except Exception:  # noqa: BLE001
            pass

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

    def _state(self) -> dict:
        page = self._pick_page()
        if not self._logged_in(page):
            # 别每次都重新导航：没登录时页面本来就停在登录页，重新取二维码由前端触发。
            # **绝不在登录页上 goto/reload** —— 用户可能刚扫完、正要在手机上点确认，
            # 这时导航会让这次登录当场作废（手机显示成功、这边永远转圈）。
            try:
                if "channels.weixin.qq.com" not in (page.url or ""):
                    page.goto(PLATFORM, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(1500)
            except Exception:  # noqa: BLE001
                pass
            # 导航过了就**再看一次**。拿导航之前的旧答案当结论 = 界面永远差一拍，
            # 用户看到的正是「扫完码没反应」。
            if not self._logged_in(page) and not self._dom_logged_in(page):
                return {"logged_in": False}
        return self._profile(page)

    def state(self) -> dict:
        """查登录态。**这一步不许切无头**：它属于扫码流程，浏览器得留着。"""
        return self.call(lambda _s: self._state(), timeout=45)

    # ── 对外：诊断（只在排查「扫了没反应」时用）────────────────────────────
    def _diag(self) -> dict:
        """把浏览器内部真实状态倒出来：有哪些标签、停在哪个网址、
        二维码 iframe 里写着什么（微信会在 iframe 里写「二维码已失效」这类字）。

        为什么要有这个：扫码失败时外面只能看到 `logged_in: false`，无法区分
        「码过期了」「手机没点确认」「微信拦了自动化」。没这个就只能猜。
        """
        page = self._pick_page()
        out: dict = {"url": "", "pages": [], "frames": [], "qr_text": "", "qr_src": ""}
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
        fr = self._qr_frame(page)
        if fr:
            try:
                out["qr_text"] = " ".join((fr.locator("body").inner_text(timeout=2000) or "").split())[:160]
            except Exception:  # noqa: BLE001
                pass
            try:
                src = fr.locator('img[src*="/connect/qrcode/"]').get_attribute("src", timeout=2000)
                out["qr_src"] = (src or "")[:120]
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
