"""远程版本闸：问服务器「我这版还能不能用」。

### 为什么本地指纹闸不够（2026-11-04 补）

`web/version.js` 比的是「页面指纹 vs 本机后端指纹」。开发时后端一起换，很好用；
但**分发给终端用户的是 exe**——每个用户电脑上跑的是他自己那份后端，
页面和服务器永远同一版，本地指纹闸一次都不会触发。

结果就是：你发了 0.2.0，用户机器上还开着 0.1.0，他点「生成结构」打的是旧接口，
你在后台也看不到他（日志还留在他自己机器上）。用户的要求是「需要更新时必须更新后
才可继续使用」，那就得有一个**机器外的判据**——服务器说什么版本才算数。

### 判定规则（宁可放过，不能误锁）

  · 服务器回 `min_supported`（最低可用版本），本地 < 它就锁；
  · **网络不通 / 接口没上线 → 一律视为最新**。离线的人本来也用不了 AI，
    再把他锁在门外只会变成「软件坏了」的投诉；
  · 节流 6 小时，失败 10 分钟后重试；结果落盘，重启后仍知道上次问到了什么。

### 锁用什么形式

中间件返回 426 Upgrade Required + `code=need-upgrade`，除登录/探活/静态资源外
一律拦死。前端收到后弹**不可关闭**的升级卡片，只留「下载新版」——
这一点和「刷新就好」的本地闸不同：这里是真要换程序，刷新解决不了。
"""
from __future__ import annotations

import json
import os
import threading
import time

from . import buildstamp, core

CHECK_HOURS = 6.0          # 正常间隔
RETRY_MINUTES = 10.0       # 上次失败后的重试间隔
TIMEOUT = 6                # 秒。查版本不能把启动/请求拖住

_STATE_FILE = os.path.join(core.DATA_DIR, "update_state.json")
_lock = threading.Lock()
_state: dict = {
    "latest": "", "min": "", "url": "", "note": "",
    "checked_at": 0.0, "ok": False, "error": "",
}
_started = False


def local_version() -> str:
    return buildstamp.VERSION


def _vtuple(v: str) -> tuple:
    """'0.2.1' → (0, 2, 1)。非数字段按 0 算，保证不抛异常（版本号是人手填的）。"""
    out = []
    for seg in str(v or "").split(".")[:4]:
        try:
            out.append(int(seg.strip() or 0))
        except ValueError:
            out.append(0)
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def base_url() -> str:
    """版本从哪个服务器问——和 AI 代理同一个域名，免得再维护一套配置。"""
    cfg = core.load_config()
    return (cfg.get("proxy_url") or core.PROXY_URL_DEFAULT or "").strip().rstrip("/")


def _load_disk() -> None:
    try:
        with open(_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:  # noqa: BLE001
        return
    with _lock:
        for k, v in data.items():
            if k in _state:
                _state[k] = v


def _save_disk() -> None:
    try:
        os.makedirs(core.DATA_DIR, exist_ok=True)
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False)
        os.replace(tmp, _STATE_FILE)
    except Exception:  # noqa: BLE001
        pass


def check(force: bool = False) -> dict:
    """问一次服务器。返回当前状态（失败也返回，ok=False）。永不抛异常。"""
    now = time.time()
    with _lock:
        last = float(_state["checked_at"] or 0)
        ok = bool(_state["ok"])
    gap = (CHECK_HOURS * 3600) if ok else (RETRY_MINUTES * 60)
    if not force and last and now - last < gap:
        return state()

    base = base_url()
    if not base:
        return state()
    try:
        import requests
        r = requests.get(f"{base}/api/version",
                         params={"app": local_version(), "os": os.name},
                         timeout=TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        d = r.json() or {}
        with _lock:
            _state.update({
                "latest": str(d.get("version") or "").strip(),
                "min": str(d.get("min_supported") or "").strip(),
                "url": str(d.get("url") or "").strip(),
                "note": str(d.get("note") or "").strip()[:300],
                "checked_at": now, "ok": True, "error": "",
            })
        _save_disk()
    except Exception as exc:  # noqa: BLE001
        # 失败保留上一次成功的结果（不能因为一次抖动就把锁解掉，也不能凭空加上锁）
        with _lock:
            _state.update({"checked_at": now, "ok": False, "error": str(exc)[:200]})
        _save_disk()
    return state()


def state() -> dict:
    with _lock:
        snap = dict(_state)
    snap["local"] = local_version()
    snap["need"] = _need_of(snap)
    return snap


def _need_of(snap: dict) -> dict | None:
    """本地是否低于「最低可用版本」。没问到时（min 为空）一律不锁。"""
    low = str(snap.get("min") or "").strip()
    if not low:
        return None
    if _vtuple(local_version()) >= _vtuple(low):
        return None
    return {"min": low, "latest": snap.get("latest") or low,
            "url": snap.get("url") or "", "note": snap.get("note") or ""}


def need() -> dict | None:
    """中间件每个请求都会问；纯内存判断，不加锁、不联网。

    必须把 url/note 一起带出去：中间件要拿它们写进 426 的正文，
    前端那张升级卡上的「下载新版」按钮靠的就是这个 url。
    （只取 min 的话，锁屏卡片会退化成「请联系管理员」，等于没做。）
    """
    with _lock:
        snap = {"min": str(_state.get("min") or "").strip(),
                "latest": _state.get("latest") or "",
                "url": _state.get("url") or "",
                "note": _state.get("note") or ""}
    return _need_of(snap)


def _loop() -> None:
    # 启动先等 8 秒：别和建库、加载模型抢那几秒钟
    time.sleep(8)
    while True:
        try:
            check()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3600)


def start() -> None:
    """启动后台巡检线程。幂等，重复调用无副作用。"""
    global _started
    if _started:
        return
    _started = True
    _load_disk()
    threading.Thread(target=_loop, name="update-check", daemon=True).start()
