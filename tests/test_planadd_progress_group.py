# -*- coding: utf-8 -*-
"""验证 2026-11-21：追加新套（planadd）的进度必须落在 planadd 组，中止才点得动。

事故现场：第 2 步「补跑/追加」跑的是 planadd 组，但 analyze.build_plans 的 _tick
把 kind 写死成 "plan"（→ cuts 组），于是
  * 进度写进 cuts 槽，状态条按 cuts 显示「AI 正在编排 6%」；
  * 任务退出只清自己的 planadd 槽 → cuts 槽永远停在中途；
  * 点「中止」前端按 cuts 发请求，_RUNNING 里只有 (pid, planadd) → 一个也没命中，
    界面回「这一步刚好已经结束了」，任务却还在跑 / 状态条还挂着。
修法：task_kind 一路传进 build_plans，_tick 按它落槽；core 再加两道兜底
（get_task 丢掉放错组的槽、start_task 的 runner 退出时清掉自己的非报错槽）。

运行：python tests/test_planadd_progress_group.py
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import analyze, core  # noqa: E402


def make_project(pid, durs=(60.0,) * 4, lines_per_ep=20):
    eps = []
    for i, d in enumerate(durs, 1):
        step = float(d) / lines_per_ep
        lines = []
        for k in range(lines_per_ep):
            s = k * step
            lines.append({"start": s, "end": s + step, "text": f"第{i}集第{k + 1}句"})
        eps.append({"index": i, "duration": float(d), "lines": lines,
                    "status": "done", "lines_time_sig": f"sig{i}"})
    return {"id": pid, "name": "回归测试剧", "episodes": eps, "plans": [], "batch": {}}


def boom(*a, **k):
    raise RuntimeError("测试桩：AI 调用在这里中断")


def main():
    ok = []

    # 1) 追加任务：进度必须落在 planadd 槽，cuts 槽一个字节都不许写
    pid = "t_planadd_1"
    p = make_project(pid)
    real_call = analyze._call
    analyze._call = boom
    try:
        try:
            analyze.build_plans(p, 5, 7.0, 12.0, progress=(0.0, 92.0, ""),
                                task_kind="planadd")
        except RuntimeError:
            pass   # 故意在 AI 调用处中断：这时 pct=6 的进度已经报过了
    finally:
        analyze._call = real_call
    slots = core._TASKS.get(pid) or {}
    ok.append(("追加任务的进度落在 planadd 槽", "planadd" in slots, str(list(slots))))
    ok.append(("追加任务不再污染 cuts 槽", "cuts" not in slots, str(list(slots))))
    if "planadd" in slots:
        # _tick(6) 经 progress=(0, 92, "") 换算成 5.5%，前端四舍五入显示 6%
        ok.append(("进度是编排开头的 6%（5.5 四舍五入）",
                   abs(float(slots["planadd"]["pct"]) - 5.5) < 0.01,
                   str(slots["planadd"]["pct"])))
    core._TASKS.pop(pid, None)

    # 2) 普通编排（kind=plan）行为不变：仍落 cuts
    pid = "t_planadd_2"
    p = make_project(pid)
    analyze._call = boom
    try:
        try:
            analyze.build_plans(p, 5, 7.0, 12.0, progress=(0.0, 92.0, ""))
        except RuntimeError:
            pass
    finally:
        analyze._call = real_call
    slots = core._TASKS.get(pid) or {}
    ok.append(("普通编排仍落 cuts 槽（老行为不变）", "cuts" in slots, str(list(slots))))
    core._TASKS.pop(pid, None)

    # 3) get_task 自愈：放错组的槽位当场丢掉，不许再当成「在跑」
    pid = "t_planadd_3"
    with core._TASKS_LOCK:
        core._TASKS[pid] = {"cuts": {"kind": "planadd", "pct": 6.0, "msg": "AI 正在编排",
                                     "error": "", "started": time.time() - 420}}
    got = core.get_task(pid, "cuts")
    left = core._TASKS.get(pid) or {}
    ok.append(("放错组的槽位 get_task 返回空", got is None, str(got)[:40]))
    ok.append(("放错组的槽位被清掉", "cuts" not in left, str(list(left))))

    # 3b) 组没在跑却还有槽位（全自动里 nosub.run 会顺手写 nosub 槽）也要丢掉
    pid = "t_planadd_3b"
    with core._TASKS_LOCK:
        core._TASKS[pid] = {"nosub": {"kind": "nosub", "pct": 40.0, "msg": "正在抹原字幕",
                                      "error": "", "started": time.time() - 60}}
    ok.append(("组没在跑的残留槽位返回空", core.get_task(pid, "nosub") is None, ""))
    ok.append(("残留槽位已清掉", "nosub" not in (core._TASKS.get(pid) or {}), ""))

    # 3c) 报错槽位必须保留（那是失败原因，要留在界面上）
    pid = "t_planadd_3c"
    with core._TASKS_LOCK:
        core._TASKS[pid] = {"cuts": {"kind": "plan", "pct": 0.0, "msg": "失败",
                                     "error": "模型输出被截断了", "started": time.time() - 30}}
    e = core.get_task(pid, "cuts")
    ok.append(("报错槽位不会被当成残留清掉", bool(e and e.get("error")), str(e)[:40]))
    with core._TASKS_LOCK:
        core._TASKS.pop(pid, None)

    # 4) runner 退出兜底：任务函数忘了收尾，进度槽也不能留在中途
    pid = "t_planadd_4"
    done = threading.Event()

    def forgetful():
        core.set_task(pid, "planadd", 42.0, "写到一半就返回了")
        done.set()          # 不调 set_task(pid, None)

    assert core.start_task(pid, "planadd", forgetful)
    done.wait(5)
    for _ in range(100):
        if not core.task_running(pid, "planadd"):
            break
        time.sleep(0.02)
    ok.append(("任务退出后没有残留进度", core.get_task(pid, "planadd") is None,
               str(core.get_task(pid, "planadd"))[:50]))

    # 5) 中止真的能命中 planadd（这就是用户点不动的那一下）
    pid = "t_planadd_5"
    gate = threading.Event()

    def stuck():
        try:
            while not gate.is_set():
                core.set_task(pid, "planadd", 30.0, "AI 正在编排")
                time.sleep(0.05)
        except core.TaskCancelled:
            pass

    assert core.start_task(pid, "planadd", stuck)
    time.sleep(0.15)
    hit = core.cancel_task(pid, "planadd")
    ok.append(("按 planadd 中止能命中任务", len(hit) == 1, str(hit)))
    ok.append(("中止标记真的打上了", core.is_cancelled(pid, "planadd"), ""))
    gate.set()
    for _ in range(150):
        if not core.task_running(pid, "planadd"):
            break
        time.sleep(0.02)
    ok.append(("中止后任务槽位已清空", core.get_task(pid, "planadd") is None, ""))

    bad = [x for x in ok if not x[1]]
    for name, passed, detail in ok:
        print(("  OK  " if passed else "  FAIL") + f"  {name}  ({detail})")
    print("RESULT:", "PASS" if not bad else f"FAIL {len(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
