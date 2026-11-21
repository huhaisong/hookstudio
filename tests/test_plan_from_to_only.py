# -*- coding: utf-8 -*-
"""验证 2026-11-21：剪辑结构只要 from/to，代码改成「按剧情顺序装填」。

改动：plans_schema 不再要 label / rating / core / hook / bgm。
代码必须跟着改三处，否则会静默出废片：
  * _to_segs 不再给缺失的评分补 5 分（否则界面每块挂「5分」、全被当缓冲块）；
  * _fit_target 无评分时按全局秒顺序装填（原逻辑会把整套块分成 0 个爆点 +
    全部缓冲块，只装出两成时长）；
  * batch._speed_of 无评分不加速（原来 `or 0` → 全套 1.15 倍速）。
运行：python tests/test_plan_from_to_only.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import analyze  # noqa: E402


def make_project(durs, lines_per_ep=20):
    eps = []
    for i, d in enumerate(durs, 1):
        step = float(d) / lines_per_ep
        lines = []
        for k in range(lines_per_ep):
            s = k * step
            lines.append({"start": s, "end": s + step, "text": f"第{i}集第{k + 1}句"})
        eps.append({"index": i, "duration": float(d), "lines": lines})
    return {"episodes": eps}


def blk(ep, start, end, gs, **kw):
    d = {"blk": f"b{ep}_{int(start)}", "ep": ep, "start": float(start), "end": float(end),
         "gs": float(gs), "ge": float(gs) + (end - start), "label": ""}
    d.update(kw)
    return d


def main():
    ok = []
    p = make_project([60.0, 60.0, 60.0, 60.0])

    # 1) 只给 from/to：跨集区间拆两片、同属一块，且不带 rating / label
    segs = analyze._to_segs(p, {"segments": [{"from": 11, "to": 30}]})
    total = sum(s["end"] - s["start"] for s in segs)
    ok.append(("from 11→30 跨集会拆成 2 片", len(segs) == 2, f"{len(segs)} 片"))
    ok.append(("跨集两片合计约 60 秒", abs(total - 60.5) < 0.4, f"{total:.1f}s"))
    ok.append(("缺评分不写 rating 键", all("rating" not in s for s in segs),
               str([("rating" in s) for s in segs])))

    # 2) 缝合后是一块 60 秒，且不被整集块收窗打断（两片各 30 秒、都不贴两头）
    st = analyze._stitch_blocks(p, segs)
    tw = analyze._trim_whole(p, st)
    d = sum(s["end"] - s["start"] for s in tw)
    ok.append(("跨集 60 秒块过完缝合+收窗仍是 60 秒", abs(d - 60.5) < 0.4, f"{d:.1f}s"))
    ok.append(("块上也没有 rating", all("rating" not in s for s in tw), ""))

    # 3) 无评分：按剧情顺序装填，装到目标时长（乱序输入也按 gs 排）
    segs3 = [blk(3, 0, 60, 120), blk(1, 0, 60, 0), blk(4, 0, 60, 180), blk(2, 0, 60, 60)]
    out = analyze._fit_target(p, segs3, 180.0)
    eps = [s["ep"] for s in out]
    got = sum(s["end"] - s["start"] for s in out)
    ok.append(("无评分按剧情顺序装填（取前 3 集）", eps == [1, 2, 3], str(eps)))
    ok.append(("装到目标 180 秒", abs(got - 180.0) < 0.01, f"{got:.0f}s"))

    # 4) 老结构（带 1-10 分）仍按分数挑：9 分和 8 分入选，3 分落选
    segs4 = [blk(1, 0, 60, 0, rating=3.0), blk(2, 0, 60, 60, rating=9.0),
             blk(3, 0, 60, 120, rating=8.0)]
    out = analyze._fit_target(p, segs4, 120.0)
    picked = sorted(s["rating"] for s in out)
    ok.append(("老结构仍按评分挑（9+8）", picked == [8.0, 9.0], str(picked)))

    # 5) 提示词与校验：schema 只认 plans/segments/from/to；plans.txt 不再提评分/块数
    sch = analyze._read_prompt("plans_schema.txt")
    analyze._check_schema("plans_schema.txt", sch)
    ok.append(("新输出格式能通过保存体检", True, "ok"))
    pl = analyze._read_prompt("plans.txt")
    ok.append(("plans.txt 不再出现 {blocks}", "{blocks}" not in pl, ""))
    ok.append(("plans.txt 是 4 条原则", pl.count("**") >= 8 and "4. **套间重复率限额**" in pl, ""))

    bad = [x for x in ok if not x[1]]
    for name, passed, detail in ok:
        print(("  OK  " if passed else "  FAIL") + f"  {name}  ({detail})")
    print("RESULT:", "PASS" if not bad else f"FAIL {len(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
