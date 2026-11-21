# -*- coding: utf-8 -*-
"""验证 2026-11-21：nosub.run 被内嵌调用时必须由调用方接管进度。

事故链（与 planadd 那次同源）：一键全自动 / 追加视频都跑在 cuts 组，里面直接
调 nosub.run(pid)，而 nosub.run 无条件
  * 把进度写进 nosub 槽 → 调用方自己的 cuts 进度条停在 32% / 92% 不动；
  * 末尾 set_task(pid, None) 清的是 _CUR.group（= cuts）→ 把调用方那一格
    连同 task.json 的「中断续跑」标记一起清掉，界面状态条闪断、崩了不能续跑。
修法：run(pid, task_kind="nosub", on_progress=None)。给了 on_progress 就一次
core.set_task 都不碰，0~100 的本步骤进度交给调用方映射到自己的刻度。

运行：python tests/test_nosub_embedded_progress.py
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import core, nosub  # noqa: E402

ok = []


def check(name, cond, extra=""):
    ok.append((name, bool(cond), str(extra)))


class FakeCore:
    """把 nosub.run 里会碰的真世界调用全换成记录器（不写盘、不删目录）。"""

    def __init__(self):
        self.tasks = []
        self._orig = {}

    def __enter__(self):
        for fn in ("set_task", "save_merge", "rmtree", "check_space", "load"):
            self._orig[fn] = getattr(core, fn)
        core.set_task = lambda pid, kind, pct=0.0, msg="", **kw: self.tasks.append(
            (kind, round(float(pct), 2), msg))
        core.save_merge = lambda p, keys=None, **kw: None
        core.rmtree = lambda path: None
        core.check_space = lambda *a, **kw: None
        return self

    def __exit__(self, *exc):
        for fn, f in self._orig.items():
            setattr(core, fn, f)


def fake_project(tmp, pid="t_nosub_embed"):
    """一期视频，但源文件不存在 → 循环里只会记「找不到原始视频」，不碰 ffmpeg。"""
    return {
        "id": pid, "name": "测试", "src_dir": tmp,
        "episodes": [{"file": "e1.mp4", "duration": 60.0,
                      "lines": [{"text": "台词", "start": 0.0, "end": 1.0}]}],
        # auto=False + mode=crop：跳过字幕带识别与逐集量矩形（那两步要解码视频）
        "nosub_cfg": {"auto": False, "mode": "crop"},
        "nosub": {},
    }


with tempfile.TemporaryDirectory() as tmp:
    pid = "t_nosub_embed"
    proj = fake_project(tmp, pid)

    # ── 1) 内嵌调用（传 on_progress）：一次 core.set_task 都不许调 ──────────
    seen = []
    with FakeCore() as fc:
        core.load = lambda _pid: proj
        orig_out = nosub.out_dir
        nosub.out_dir = lambda _pid: os.path.join(tmp, "out")
        try:
            nosub.run(pid, on_progress=lambda pct, msg: seen.append(
                (round(float(pct), 2), msg)))
        finally:
            nosub.out_dir = orig_out
    check("内嵌调用不碰 core.set_task", len(fc.tasks) == 0, fc.tasks[:2])
    check("内嵌调用把进度交给 on_progress", len(seen) >= 2, seen[:3])
    check("内嵌调用报过 100%（收尾进度也给调用方）",
          any(abs(p - 100.0) < 0.01 for p, _ in seen), seen[-2:])
    check("内嵌调用第一格进度是集起点（不是 0 硬编码）",
          seen and 0 <= seen[0][0] <= 100, seen[:1])

    # ── 2) 独立跑（不传 on_progress）：仍写 nosub 槽并收尾 ──────────────────
    with FakeCore() as fc:
        core.load = lambda _pid: proj
        nosub.out_dir = lambda _pid: os.path.join(tmp, "out2")
        try:
            nosub.run(pid)
        finally:
            nosub.out_dir = orig_out
    kinds = [k for k, _, _ in fc.tasks]
    check("独立跑写的是 nosub 槽", kinds and set(kinds[:-1]) == {"nosub"}, kinds[:3])
    check("独立跑末尾 set_task(None) 收尾", kinds and kinds[-1] is None, kinds[-2:])
    check("独立跑报过 100%",
          any(k == "nosub" and abs(p - 100.0) < 0.01 for k, p, _ in fc.tasks),
          fc.tasks[-2:])

# ── 3) 签名与两个内嵌调用点都按新约定改过 ──────────────────────────────────
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src_nosub = open(os.path.join(root, "app", "nosub.py"), encoding="utf-8").read()
m = re.search(r"def run\(pid: str, task_kind: str = \"nosub\", on_progress=None\)", src_nosub)
check("run 签名带 task_kind / on_progress", bool(m))
check("内嵌时不收尾（on_progress 有值就跳过 set_task(None)）",
      "if on_progress is None:\n        core.set_task(pid, None)" in src_nosub)
check("nosub 内不再有写死的 set_task(pid, \"nosub\")",
      'core.set_task(pid, "nosub"' not in src_nosub)

src_ap = open(os.path.join(root, "app", "autopipe.py"), encoding="utf-8").read()
check("全自动接管进度（nosub.run 传 on_progress）",
      "nosub.run(pid, on_progress=" in src_ap)
check("全自动映射到 ②/④ 刻度 32%→54%",
      "32 + pct * 0.22" in src_ap)

src_av = open(os.path.join(root, "app", "addvideos.py"), encoding="utf-8").read()
check("追加视频接管进度（nosub.run 传 on_progress）",
      "nosub.run(pid, on_progress=" in src_av)
check("追加视频映射到 92%→100%", "92 + pct * 0.08" in src_av)

src_main = open(os.path.join(root, "app", "main.py"), encoding="utf-8").read()
check("第 2 步「重做字幕」仍独立跑（默认 nosub 组）",
      src_main.count("nosub.run(pid)") >= 2, src_main.count("nosub.run(pid)"))

fail = [t for t in ok if not t[1]]
for name, passed, extra in ok:
    print(("PASS  " if passed else "FAIL  ") + name
          + (f"   [{extra}]" if not passed and extra else ""))
print(f"\n{len(ok) - len(fail)}/{len(ok)} PASS")
sys.exit(1 if fail else 0)
