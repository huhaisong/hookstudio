"""播放流速的后台采样。

平台的播放量只有累计值，「流速」只能靠隔一段时间采一次、看两次的差。所以必须有一个
常驻线程按间隔去号里读 —— 这决定了它两个天生的限制，界面上会写清，别藏：

  · **程序开着才采，关了就断点**（不是丢数据，是那段时间没有点，柱带上会是空的）
  · 一轮要开浏览器，号多了要几分钟；这期间点发布会排队，不会坏，但要等

默认关闭：这是本机资源消耗（内存 + 同一个号短时间反复请求），得用户自己点「开始监控」。
"""
from __future__ import annotations

import json
import os
import threading
import time

from . import core, publisher, wxbot, wxstat

FILE = os.path.join(core.DATA_DIR, "publish_monitor.json")
EVERY_MIN = 3      # 默认 3 分钟一轮（用户 2026-11-13 定：要更细的流速）
TICK = 20          # 每 20 秒看一眼该不该采
FLOOR = 180        # 间隔下限 3 分钟：再密就是在刷接口了，而且播放量本身有延迟

_LOCK = threading.Lock()
_S = {
    "on": False, "every": EVERY_MIN * 60, "last": 0, "last_at": "",
    "busy": False, "err": "", "rounds": 0, "ok": 0, "miss": 0,
    # 监控是绑号的：换号后绝不继续偷偷采旧号（2026-11-13 用户要求按号隔离）
    "aid": "",
}
_BOOT = False
# 用户手动停过的标记（2026-11-14 批2 A2）：排期成功后要自动开监控兜底涨幅数据，
# 但用户明确点过「停止」就尊重，本进程内不再自动开。重启后标记归零，下次排期照常。
_USER_STOP = False


def user_stopped() -> bool:
    return _USER_STOP


def _load() -> None:
    try:
        with open(FILE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            _S["on"] = bool(d.get("on"))
            _S["every"] = max(FLOOR, int(d.get("every") or EVERY_MIN * 60))
            _S["last"] = int(d.get("last") or 0)
            _S["aid"] = str(d.get("aid") or "")
    except Exception:  # noqa: BLE001
        pass


def _save() -> None:
    try:
        os.makedirs(os.path.dirname(FILE), exist_ok=True)
        with open(FILE, "w", encoding="utf-8") as f:
            json.dump({"on": _S["on"], "every": _S["every"], "last": _S["last"],
                       "aid": _S["aid"]},
                      f, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def status() -> dict:
    nxt = -1
    if _S["on"]:
        nxt = max(0, int(_S["last"] + _S["every"] - time.time())) // 60
    return {
        "on": _S["on"], "every_min": max(1, _S["every"] // 60),
        "last_at": _S["last_at"], "next_in_min": nxt,
        "busy": _S["busy"], "err": _S["err"], "aid": _S["aid"],
        "rounds": _S["rounds"], "ok": _S["ok"], "miss": _S["miss"],
    }


def set_on(on: bool, every_min: int | None = None, aid: str = "") -> dict:
    global _USER_STOP
    # 先 start 再改状态：start() 首次会 _load() 读磁盘旧配置，
    # 放在后面会把用户刚点的「开/关 + 绑哪个号」覆盖掉。
    if not on:
        _USER_STOP = True           # 手动停的：排期自动开监控时尊重这个选择
    start()
    with _LOCK:
        was = _S["on"]
        _S["on"] = bool(on)
        if aid:
            _S["aid"] = aid
        if every_min:
            _S["every"] = max(FLOOR, int(every_min) * 60)
        if _S["on"] and not was:
            _S["last"] = 0      # 刚打开就立刻采第一轮，别让用户等 3 分钟
            _S["err"] = ""
        _save()
    start()
    return status()


def _round() -> None:
    """只采「开监控时确认的那个号」。换号／没确认号就直接停，不猜。"""
    aid = str(_S.get("aid") or "")
    cur = str((publisher.queue().get("current_account_id") or ""))
    if not aid or (cur and cur != aid):
        _S["on"] = False
        _S["err"] = ("换号了，监控已自动停止；到「数据」页重新点「开始监控」"
                     if aid and cur and cur != aid else "没确认账号，监控已停止")
        _save()
        return
    _S["busy"] = True
    ok = miss = 0
    try:
        acc = publisher.get_account(aid)
        if not acc or acc.get("state") != "online":
            raise RuntimeError("这个号的登录态不在了")
        snap = wxstat.sample(wxbot.session(aid), aid=aid)
        if snap.get("ok"):
            ok += 1
        else:
            miss += 1
    except Exception as exc:  # noqa: BLE001
        miss += 1
        _S["err"] = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        _S["busy"] = False
        _S["ok"] += ok
        _S["miss"] += miss
        _S["rounds"] += 1
        _S["last"] = int(time.time())
        _S["last_at"] = time.strftime("%Y-%m-%dT%H:%M")
        _save()


def _loop() -> None:
    while True:
        time.sleep(TICK)
        if not _S["on"] or _S["busy"]:
            continue
        if time.time() - _S["last"] < _S["every"]:
            continue
        try:
            _round()
        except Exception as exc:  # noqa: BLE001
            _S["err"] = f"{type(exc).__name__}: {exc}"[:200]


def start() -> None:
    """开机起一次（读配置决定采不采）。重复调用无副作用。"""
    global _BOOT
    if _BOOT:
        return
    _BOOT = True
    _load()
    # 老配置没有 aid（全号混采时代）。开机就纠偏，别让监控悄悄采旧号：
    # 没绑定号、或绑定的号和现在确认的不一致，一律停掉并把原因写出来。
    cur = str((publisher.queue().get("current_account_id") or ""))
    if _S["on"] and (not _S["aid"] or (cur and cur != _S["aid"])):
        _S["on"] = False
        _S["err"] = "换过号或还没确认账号，监控已停；到「数据」页重新点「开始监控」"
        _save()
    threading.Thread(target=_loop, daemon=True, name="pubmon").start()
