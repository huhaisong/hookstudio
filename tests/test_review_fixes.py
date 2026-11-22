# -*- coding: utf-8 -*-
"""2026-11-22 复查修复的回归测试（断点保留 / 并行中止 / 队列互斥 / 台词版本）。

逐条对应复查清单：
  1. autopipe.resume_entry：重试入口不再清断点（结构参数没变 + 套还在 → 沿用）。
  2. core._cancel_key：池子线程（只有 _CALL_CTX）也能取到中止键，ffmpeg 可被杀。
  3. publisher._qlocked：队列读改写全程互斥，并发写不再互相抹掉。
  4. core.bump_lines_ver：改台词 +1，出片指纹据此重渲烧旧字的套。
  5. main._step_incomplete：autopipe 四步任一没做完都判「没跑完」，重启会登记续跑。
"""
import os
import sys
import tempfile
import threading
from unittest import mock

sys.path.insert(0, ".")
from app import autopipe, core, publisher  # noqa: E402


def test_resume_entry_keeps_indexes():
    cfg = {"jobs": [{"model": "m1", "count": 3, "min_minutes": 2, "max_minutes": 5}],
           "order": "chrono", "eps": [1, 2]}
    plans = [{"index": i, "batch_no": 2} for i in (4, 5, 6)]
    old_ap = {"cfg": cfg, "stage": "error", "indexes": [4, 5, 6]}
    stage, idx = autopipe.resume_entry(old_ap, cfg, plans)
    assert (stage, idx) == ("plan", [4, 5, 6]), (stage, idx)

    # 只改了画面/存盘参数（fx/save_dir）：断点照样保留
    cfg2 = dict(cfg, fx={"mirror": False}, save_dir="F:/x")
    stage, idx = autopipe.resume_entry(old_ap, cfg2, plans)
    assert (stage, idx) == ("plan", [4, 5, 6]), (stage, idx)

    # 换了模型/套数：断点作废，从头开新批
    cfg3 = {"jobs": [{"model": "m2", "count": 3, "min_minutes": 2, "max_minutes": 5}],
            "order": "chrono", "eps": [1, 2]}
    assert autopipe.resume_entry(old_ap, cfg3, plans) == ("init", [])

    # 套被用户删了：断点作废
    assert autopipe.resume_entry(old_ap, cfg, plans[:1]) == ("init", [])
    # 没有旧断点：全新开始
    assert autopipe.resume_entry({}, cfg, plans) == ("init", [])


def test_cancel_key_fallback_to_call_ctx():
    # 什么都不绑（普通 API 线程）：取不到键
    assert core._cancel_key() is None
    # 只绑了 _CALL_CTX（出片并行 worker 的现状，2026-11-22 修复点）：
    # 之前返回 None → register_proc/check_cancel 全空操作 → 中止杀不掉 ffmpeg
    core.bind_call_ctx("P1", "cuts")
    try:
        assert core._cancel_key() == ("P1", "cuts")
        # 显式传参仍然优先（nosub 等调用方带 pid/group 的口径不变）
        assert core._cancel_key("P2", "nosub") == ("P2", "nosub")
        # 解绑后回到 None
        core.bind_call_ctx(None, None)
        assert core._cancel_key() is None
    finally:
        core.bind_call_ctx(None, None)


def test_bump_lines_ver(monkeypatch=None):
    store = {"n": 0}
    proj = {"id": "T", "lines_ver": 2}
    with mock.patch.object(core, "load", return_value=proj), \
         mock.patch.object(core, "save_merge") as sv:
        core.bump_lines_ver("T")
        assert proj["lines_ver"] == 3
        sv.assert_called_once_with(proj, ["lines_ver"])
        # 项目不存在：安静返回
        with mock.patch.object(core, "load", return_value=None):
            core.bump_lines_ver("X")   # 不应抛
    assert store["n"] == 0


def test_step_incomplete_autopipe():
    from app import main
    cfg = {"jobs": [{"model": "m", "count": 2, "min_minutes": 2, "max_minutes": 4}]}
    eps = [{"index": 1, "lines": [{"start": 0, "end": 1, "text": "a"}]},
           {"index": 2, "lines": [{"start": 0, "end": 1, "text": "b"}]}]

    def proj(ap, plans=None, sets=None, gen=1):
        return {"id": "T", "episodes": eps, "autopipe": ap,
                "plans": plans or [], "sets": sets or [],
                "batch": {"gen": gen}, "plan_gen": gen,
                "nosub": {"files": {1: "a.mp4", 2: "b.mp4"}}}

    ap_done = {"cfg": cfg, "done": True, "indexes": [1, 2]}
    assert main._step_incomplete(proj(ap_done), "autopipe") is False

    ap_full = {"cfg": cfg, "indexes": [1, 2]}
    both_out = [{"index": 1, "gen": 1, "output": "x.mp4"},
                {"index": 2, "gen": 1, "output": "y.mp4"}]
    assert main._step_incomplete(proj(ap_full, plans=[{"index": 1}, {"index": 2}],
                                      sets=both_out), "autopipe") is False

    # ④ 差一套：没跑完（重启应登记续跑）
    assert main._step_incomplete(proj(ap_full, plans=[{"index": 1}, {"index": 2}],
                                      sets=[both_out[0]]), "autopipe") is True
    # ③ 套数不够：没跑完
    ap_half = dict(ap_full, indexes=[1])
    assert main._step_incomplete(proj(ap_half, plans=[{"index": 1}]), "autopipe") is True
    # ② 底片缺一集：没跑完
    p = proj(ap_full)
    p["nosub"] = {"files": {1: "a.mp4"}}
    assert main._step_incomplete(p, "autopipe") is True
    # ① 有集没台词：没跑完
    p = proj(ap_full)
    p["episodes"] = [eps[0], {"index": 2}]
    assert main._step_incomplete(p, "autopipe") is True
    # 没配置 jobs：谈不上没跑完
    assert main._step_incomplete(proj({}), "autopipe") is False


def test_queue_mutex_no_lost_update():
    """两个线程并发读改写队列：全程持锁后，谁写的都不能被抹掉。"""
    tmp = tempfile.mkdtemp(prefix="hs_qlock_")
    qfile = os.path.join(tmp, "publish_queue.json")
    with mock.patch.object(publisher, "QUEUE_FILE", qfile):
        publisher.save_queue({"jobs": [
            {"id": "A", "status": "queued", "title": ""},
            {"id": "B", "status": "queued", "title": ""},
        ]})
        errs = []

        def churn_a():
            try:
                for i in range(120):
                    publisher.update_job("A", {"title": f"a{i}"})
            except Exception as exc:  # noqa: BLE001
                errs.append(exc)

        def churn_b():
            try:
                for i in range(120):
                    publisher.set_status("B", "queued", f"b{i}")
            except Exception as exc:  # noqa: BLE001
                errs.append(exc)

        ts = [threading.Thread(target=churn_a), threading.Thread(target=churn_b)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert not errs, errs
        jobs = {j["id"]: j for j in publisher.queue()["jobs"]}
        # 没有互斥时这里会随机丢更新：A 的标题停在半路，或 B 的 note 丢了
        assert jobs["A"]["title"] == "a119", jobs["A"]["title"]
        assert jobs["B"]["error"] == "b119", jobs["B"]
        assert jobs["A"]["status"] == "queued" and jobs["B"]["status"] == "queued"


def main() -> int:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    bad = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            bad += 1
            import traceback
            print(f"FAIL {fn.__name__}: {exc}")
            traceback.print_exc()
    print(f"{len(fns) - bad}/{len(fns)} PASS")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
