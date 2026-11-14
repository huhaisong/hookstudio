"""排到即上传（2026-11-13 用户定稿）的执行端。

点「排到」就把这批逐条传上视频号助手、按计划时间设**平台自己的定时发表**；
全部传完后程序/电脑都可以关——到点由平台照发，不再需要谁守着。

一条一条来，**不并发**：家用带宽 + 内存 + 风控都吃不住并发上传（规格 §9.5）。

### 三条硬规矩
1. **不可逆的只有最后一步**。前面所有步骤（上传、填字段、设定时）都可以
   重来；只有点「发表」那一下是真发出去。所以每条都要能从头重试。
2. **失败要说人话**。错误文案直接写进队列里，界面上用户一眼能看懂，
   不要把 Playwright 的英文栈甩给客户。
3. **能中止**。撤回排期后不要再往下传；正在传的那一条让它传完、
   如实标记结果，让人来决定怎么办，不自动续跑。
"""
from __future__ import annotations

import os
import threading
import time

from . import publisher, wxbot, wxpost

TICK = 15            # 多久看一眼队列有没有到点的
MAX_ATTEMPTS = 2     # 自动重试几次（规格：重试 2 次仍失败就降级）
_RUNNING = False
_STOP = threading.Event()
_THREAD: threading.Thread | None = None


def _next_queued() -> dict | None:
    """下一条要传的（status=queued）。按队列顺序取，正好就是排期顺序。

    不再看计划时间：排到即上传，时间只交给平台的定时发表去守。
    """
    for j in publisher.queue().get("jobs", []):
        if j.get("status") != "queued":
            continue
        aid = j.get("account_id") or ""
        if not aid:
            publisher.set_status(j["id"], "failed", "这条没指定发布账号。")
            continue
        return j
    return None


def _cover_media(job: dict, tpl: dict) -> str:
    """这条要不要用封面图；返回队列里的相对名（空＝用平台自己截的帧）。"""
    rel = (job.get("cover_file") or "").strip()
    if rel:
        return rel
    return (tpl.get("cover_file") or "").strip()


def run_job(job: dict, dry_run: bool = False) -> dict:
    """发一条。失败抛异常（文案已经是人话）。"""
    from . import publishapi   # 延迟导入：避免循环依赖

    q = publisher.queue()
    tpl = q.get("template") or {}
    aid = job.get("account_id") or ""
    media = {"video": publishapi.media_url(job["id"], "video")}
    cover = _cover_media(job, tpl)
    if cover:
        media["cover"] = publishapi.media_url(job["id"], "cover")

    publisher.set_status(job["id"], "publishing", "")

    def _step(label: str) -> None:
        # 把当前步骤透给队列行（「正在上传 · 设封面」）。set_status 是读改写，
        # 和别处的状态写并发时会偶发互相覆盖——覆盖的只是这行字，下一个步骤
        # 回调马上再写，不影响正确性（2026-11-14 批量优化 A3/D1）。
        try:
            publisher.set_status(job["id"], "publishing", "", {"step": label})
        except Exception:  # noqa: BLE001  进度显示坏了不能拖累发布
            pass

    try:
        res = wxpost.publish_one(wxbot.session(aid), publisher.job_full(job), tpl,
                                  media, dry_run=dry_run, progress=_step)
    except wxpost.PostError as exc:
        attempts = int(job.get("attempts") or 0) + 1
        if attempts < MAX_ATTEMPTS and not dry_run:
            publisher.set_status(job["id"], "queued", str(exc)[:200],
                                 {"attempts": attempts})
            # 退避一下再试，别连着撞平台
            time.sleep(20)
            return {"ok": False, "retry": True, "error": str(exc)}
        publisher.set_status(job["id"], "failed", str(exc)[:200],
                             {"attempts": attempts})
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001  浏览器炸了之类
        # 浏览器超时/崩溃这类也记次数：跟 PostError 一样有据可查，界面上
        # 「重试」放回队列后从零再来。
        # 顺手把堆栈打到 service.log：这类错误（尤其 Playwright 的
        # "inside the asyncio loop"）光看一句话根本定位不到是哪一层起的浏览器。
        import traceback
        traceback.print_exc()
        attempts = int(job.get("attempts") or 0) + 1
        publisher.set_status(job["id"], "failed",
                             ("浏览器出错：" + str(exc))[:200],
                             {"attempts": attempts})
        return {"ok": False, "error": str(exc)}

    if dry_run:
        publisher.set_status(job["id"], "queued", "（干跑，没发表）")
        return {"ok": True, "dry_run": True, "form": res.get("form")}

    verified = bool((res.get("verified") or {}).get("found"))
    if verified:
        publisher.set_status(job["id"], "scheduled", "",
                             {"scheduled_at": res.get("scheduled_at"),
                              "verified_at": publisher.fmt(publisher.now())})
        return {"ok": True, "scheduled_at": res.get("scheduled_at")}

    # 点完发表但没在「待发布」里找到 —— 不当成成功。
    publisher.set_status(job["id"], "failed", "点完发表，但在「待发布」里没找到这条。"
                                             "去视频号助手确认一下。")
    return {"ok": False, "error": "回读没核对上"}


def loop() -> None:
    """后台线程：一条条把 queued 的传上去，传完一条歇 TICK 秒再传下一条。

    两条之间的这个停顿正好是天然的节奏：不并发、也不背靠背撞平台。
    """
    while not _STOP.is_set():
        try:
            job = _next_queued()
            if job is not None:
                _STOP.wait(0)
                run_job(job)
        except Exception as exc:  # noqa: BLE001  循环不能因为一条失败就退出
            print(f"[pub] 上传线程出错：{exc}")
        _STOP.wait(TICK)


def start() -> None:
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        # stop() 之后线程要到下一个 TICK 才真正退出；这个窗口里再点「排到」，
        # 不能因为「线程还活着」就直接返回——否则旧线程退干净后就没人传了。
        # 把停止信号撤掉等于救回同一线程。
        _STOP.clear()
        return
    _STOP.clear()
    _THREAD = threading.Thread(target=loop, name="pub-worker", daemon=True)
    _THREAD.start()


def stop() -> None:
    _STOP.set()


def running() -> bool:
    return bool(_THREAD and _THREAD.is_alive())
