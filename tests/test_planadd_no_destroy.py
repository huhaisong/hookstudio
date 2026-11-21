"""追加结构（planadd）不能毁掉正在出片的批次。

2026-11-21：用户要求「出片进行中追加新套」。追加必须：
  1. 不 bump gen（否则出片快照的旧 gen 写回的 sets 被前端判旧结构，白渲）；
  2. 保留 batch 的运行时字段 run_id/done/failures/save_dir（出片正在用）；
  3. 新套追加到 plans 尾部，旧套不动。
只在内存里跑，mock 掉 AI 和磁盘。
"""
import sys
from unittest import mock

sys.path.insert(0, ".")
from app import batch, core, analyze  # noqa: E402


def build_fake_project():
    seg = lambda ep, a, b: {"ep": ep, "start": a, "end": b, "rating": 6}
    old_set = {
        "index": 1, "on": True, "ai_name": "旧套",
        "edl": [seg(1, 5, 20)], "edl_text": "旧", "batch_no": 1,
    }
    return {
        "id": "T", "name": "t", "plan_gen": 3,
        "episodes": [
            {"index": 1, "duration": 60, "lines": [{"start": 0, "end": 5, "text": "台词"}]},
        ],
        "plans": [old_set],
        "batch": {
            "gen": 3, "done": 1, "run_id": "RUN-XYZ", "save_dir": "F:/成片",
            "failures": [{"index": 9, "error": "旧失败"}], "plan_batch_no": 1,
            "plan_batches": [{"no": 1, "sets": 1, "eps": [1], "all": True}],
        },
        "sets": [{"index": 1, "gen": 3, "output": "F:/成片/t/old.mp4"}],
    }


def run_append(store, task_kind="planadd", simulate_render_writes=False):
    loads = [0]
    def fake_load(pid):
        loads[0] += 1
        # 模拟：追加开头 load 之后，出片又写了新的 done/run_id（第 2 次 load 起可见）
        if simulate_render_writes and loads[0] >= 2:
            b = dict(store["p"]["batch"])
            b["done"] = 2
            b["run_id"] = "RUN-NEW"
            b["failures"] = [{"index": 9, "error": "旧失败"}, {"index": 10, "error": "新失败"}]
            return dict(store["p"]) | {"batch": b}
        return store["p"]

    def fake_save(proj, keys):
        # save_merge 的真实语义是"只覆盖指定顶层字段"，这里在内存里照做
        base = store["p"]
        for k in keys:
            if k in proj:
                base[k] = proj[k]
            else:
                base.pop(k, None)

    def fake_build_plans(p, count, mn, mx, **kw):
        # 每次返回一个新套（一个 segment 足够过 _blocks）
        return [{"name": "新套", "segments": [{"ep": 1, "start": 25, "end": 45}]} for _ in range(count)]

    def fake_select_eps(p, eps):
        return [1], []

    patches = [
        mock.patch.object(core, "load", fake_load),
        mock.patch.object(core, "save_merge", fake_save),
        mock.patch.object(core, "set_task", lambda *a, **k: None),
        mock.patch.object(core, "bind_call_ctx", lambda *a, **k: None),
        mock.patch.object(core, "max_concurrent", lambda: 1),
        mock.patch.object(analyze, "select_eps", fake_select_eps),
        mock.patch.object(analyze, "build_plans", fake_build_plans),
        mock.patch.object(analyze, "ep_offsets", lambda p: {1: 0.0}),
        mock.patch.object(analyze, "edl_text", lambda edl: "新"),
    ]
    for pt in patches:
        pt.start()
    try:
        batch.ai_plan_multi("T", [{"model": "", "count": 1, "mn": 3, "mx": 5}],
                            order="chrono", append=True, eps=[1], new_batch=True,
                            task_kind=task_kind)
    finally:
        for pt in reversed(patches):
            pt.stop()


def main():
    store = {"p": build_fake_project()}
    before = store["p"]
    old_batch = dict(before["batch"])
    old_plans = len(before["plans"])

    # 模拟并发：追加开头 load 之后，出片又写了 done=2 / run_id=RUN-NEW / 多一条失败。
    # 追加写回必须保住这些最新值，而不是被 569 行那份旧快照盖回去。
    run_append(store, simulate_render_writes=True)
    after = store["p"]
    b = after["batch"]

    print(f"gen: {old_batch['gen']} -> {b.get('gen')}")
    print(f"plan_gen: {before['plan_gen']} -> {after.get('plan_gen')}")
    print(f"plans: {old_plans} -> {len(after['plans'])}")
    print(f"run_id: {old_batch['run_id']} -> {b.get('run_id')} (期望 RUN-NEW)")
    print(f"done: {old_batch['done']} -> {b.get('done')} (期望 2)")
    print(f"save_dir: {old_batch['save_dir']} -> {b.get('save_dir')}")
    print(f"failures: {len(old_batch['failures'])} -> {len(b.get('failures') or [])} (期望 2)")
    print(f"旧套 ai_name: {after['plans'][0].get('ai_name')}")
    print(f"新套 index/ai_name: {after['plans'][-1].get('index')}/{after['plans'][-1].get('ai_name')}")

    ok = True
    if b.get("gen") != old_batch["gen"]:
        print("FAIL: gen 被 bump——正在渲的套会被判旧结构白渲"); ok = False
    if after.get("plan_gen") != before["plan_gen"]:
        print("FAIL: plan_gen 被 bump"); ok = False
    if b.get("run_id") != "RUN-NEW":
        print("FAIL: run_id 没保住出片期间的新值——续跑会换指纹重渲"); ok = False
    if b.get("done") != 2:
        print("FAIL: done 没保住出片期间的新进度"); ok = False
    if b.get("save_dir") != old_batch["save_dir"]:
        print("FAIL: save_dir 丢失"); ok = False
    if len(b.get("failures") or []) != 2:
        print("FAIL: failures 没保住出片期间的新失败记录"); ok = False
    if len(after["plans"]) != old_plans + 1:
        print("FAIL: 新套没追加到 plans 尾部"); ok = False
    if after["plans"][0].get("ai_name") != "旧套":
        print("FAIL: 旧套被改动"); ok = False
    print("PASS" if ok else "FAILED")
    if not ok:
        return False

    # 反向场景：覆盖式重排（append=False）仍要 bump gen、清 done、换掉旧套
    store2 = {"p": build_fake_project()}
    def run_overwrite(store):
        def fake_load(pid): return store["p"]
        def fake_save(proj, keys):
            for k in keys:
                if k in proj: store["p"][k] = proj[k]
        def fake_build(p, count, mn, mx, **kw):
            return [{"name": "全新", "segments": [{"ep": 1, "start": 25, "end": 45}]} for _ in range(count)]
        patches = [
            mock.patch.object(core, "load", fake_load),
            mock.patch.object(core, "save_merge", fake_save),
            mock.patch.object(core, "set_task", lambda *a, **k: None),
            mock.patch.object(core, "bind_call_ctx", lambda *a, **k: None),
            mock.patch.object(core, "max_concurrent", lambda: 1),
            mock.patch.object(analyze, "select_eps", lambda p, e: ([1], [])),
            mock.patch.object(analyze, "build_plans", fake_build),
            mock.patch.object(analyze, "ep_offsets", lambda p: {1: 0.0}),
            mock.patch.object(analyze, "edl_text", lambda edl: "新"),
        ]
        for pt in patches: pt.start()
        try:
            batch.ai_plan_multi("T", [{"model": "", "count": 1, "mn": 3, "mx": 5}], order="chrono")
        finally:
            for pt in reversed(patches): pt.stop()
    run_overwrite(store2)
    a2 = store2["p"]
    print(f"[覆盖式] gen: 3 -> {a2['batch'].get('gen')}, done: -> {a2['batch'].get('done')}, "
          f"plans: -> {len(a2['plans'])}, 首套: {a2['plans'][0].get('ai_name')}")
    ok2 = (a2["batch"].get("gen") == 4 and a2["batch"].get("done") == 0
           and len(a2["plans"]) == 1 and a2["plans"][0].get("ai_name") == "全新")
    if not ok2:
        print("FAIL: 覆盖式重排语义被破坏"); return False
    print("PASS (both)")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
