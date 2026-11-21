# -*- coding: utf-8 -*-
"""验证 2026-11-21 A1：批量出片每渲完一套立即写回 sets，不再等整批渲完。

隔离测试：mock 掉 render_edl / 磁盘 / 任务状态，只跑 batch.render_plans 的编排逻辑。
不依赖真实视频、模型、网络。运行：python tests/test_incremental_writeback.py
"""
import os
import sys
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import batch, core, render  # noqa: E402


def make_project(n=3):
    plans = []
    for i in range(1, n + 1):
        plans.append({
            "index": i, "on": True, "mode": "mix", "ai_name": f"套{i}",
            "edl": [{"a": 1}], "target_sec": 60, "style": "", "tag": "",
            "out_name": f"out{i}.mp4",
        })
    return {
        "id": "T_INC", "name": "中途可见测试", "plans": plans,
        "plan_gen": 0, "sets": [],
        "batch": {"gen": 1, "save_dir": os.path.join(os.path.dirname(__file__), "_inc_out")},
    }


def run():
    p = make_project(3)
    store = {"p": p}
    progress = []
    # 每次 save_merge 落盘时，记录当时 sets 里「已带 output」的套数时间线。
    # 逐套写回 = 这个数字会在渲染途中依次 1→2→3，而不是最后一次性跳到 3。
    writeback_timeline = []
    renders_done = [0]
    observed = {}

    def fake_load(pid):
        return store["p"]

    def fake_save(proj, keys):
        store["p"] = proj
        if "sets" in keys:
            n = len([s for s in proj.get("sets", []) if s.get("output")])
            if not writeback_timeline or writeback_timeline[-1] != n:
                writeback_timeline.append(n)

    def fake_set_task(pid, kind, pct=0.0, msg="", error="", ignore_cancel=False):
        progress.append((kind, round(float(pct), 1), msg))

    def fake_render_edl(proj, edl, name, **kw):
        # 让 out1 先完成、out2、out3 依次完成，制造明确的先后。
        # 每套完成前先看一眼「此刻已写回几套」，验证前一套在更早的套还在渲时已落盘。
        cb = kw.get("progress")
        if cb:
            cb(0, "准备渲染")
        delay = {"out1.mp4": 0.05, "out2.mp4": 0.4, "out3.mp4": 0.8}.get(name, 0.4)
        # 长任务(out3)渲染中途采样：等前序两套写回后记下当时的套数（带上限，防死等）
        if name == "out3.mp4":
            waited = 0.0
            while len([s for s in store["p"].get("sets", []) if s.get("output")]) < 2 and waited < 0.7:
                time.sleep(0.02)
                waited += 0.02
            observed["out3_mid"] = len([s for s in store["p"].get("sets", []) if s.get("output")])
            time.sleep(max(0, delay - waited))
        else:
            time.sleep(delay)
        renders_done[0] += 1
        if cb:
            cb(95, "完成")
        return {"name": name, "duration": 30.0, "size": 1024 * 1024}

    patches = [
        mock.patch.object(core, "load", fake_load),
        mock.patch.object(core, "save_merge", fake_save),
        mock.patch.object(core, "set_task", fake_set_task),
        mock.patch.object(core, "add_cleanup", lambda fn: None),
        mock.patch.object(core, "rmtree", lambda path: None),
        mock.patch.object(core, "abs_output", lambda x: x),
        mock.patch.object(render, "render_edl", fake_render_edl),
        mock.patch.object(render, "_copy_burned", lambda p: True),
        mock.patch.object(batch.reuse, "record", lambda p, rows: None),
    ]
    for pt in patches:
        pt.start()
    try:
        batch.render_plans("T_INC")
    finally:
        for pt in reversed(patches):
            pt.stop()

    sets = store["p"].get("sets", [])
    outputs = [s for s in sets if s.get("output")]
    pcts = [pct for kind, pct, msg in progress if kind == "batch"]

    print(f"写回套数: {len(outputs)} / 3")
    print(f"写回时间线(每次落盘时已成片套数): {writeback_timeline}")
    print(f"out3 渲染中途已写回套数: {observed.get('out3_mid', 'N/A')}")
    print(f"进度序列(批): {pcts}")
    print(f"最终进度: {pcts[-1] if pcts else 'N/A'}")
    print(f"单调不减: {all(b >= a for a, b in zip(pcts, pcts[1:]))}")

    ok = True
    if len(outputs) != 3:
        print("FAIL: 应为 3 套都写回"); ok = False
    # 关键断言：写回是逐套递增出现的（1→2→3），不是最后一次性到 3
    if not writeback_timeline or writeback_timeline[0] >= 3:
        print("FAIL: 写回没有逐套前移（第一笔就是全部）"); ok = False
    if len(set(writeback_timeline)) < 3:
        print("FAIL: 写回时间线没有覆盖 1/2/3 三种状态"); ok = False
    # out3 还在渲的时候，前 2 套应已写回
    if observed.get("out3_mid", 0) < 2:
        print("FAIL: 最后一套渲染中途，前序套还没写回"); ok = False
    if pcts and pcts[-1] != 100.0:
        print("FAIL: 最终进度不是 100%"); ok = False
    if not all(b >= a for a, b in zip(pcts, pcts[1:])):
        print("FAIL: 进度回退"); ok = False
    print("PASS" if ok else "FAILED")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
