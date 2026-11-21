# -*- coding: utf-8 -*-
"""验证 2026-11-21：整集块判定从「长度比例」改成「真贴着集首+集尾」。

背景：plans.txt 要求每块不短于 60 秒（不够就接相邻集的同一场戏续段）。
跨集块拆成两片后，单片很容易 ≥ 集长×0.72，老判定会把它当整集收回 ~28 秒，
提示词写的 60 秒被程序又缩回去。所以：
  * 真的整集块（start≈0 且 end≈集尾）仍然收窗 —— 防止连播整集；
  * 跨集拼块的每一片（不贴两头）一律不动。
运行：python tests/test_whole_block_edge.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import analyze  # noqa: E402


def make_project(durs):
    eps = []
    for i, d in enumerate(durs, 1):
        lines, t = [], 0.0
        while t + 3 <= d:
            lines.append({"start": t, "end": t + 3, "text": f"第{i}集台词{len(lines) + 1}"})
            t += 5
        eps.append({"index": i, "duration": float(d), "lines": lines})
    return {"episodes": eps}


def piece(ep, start, end, core=None):
    return {"blk": f"b{ep}_{start}", "ep": ep, "start": float(start), "end": float(end),
            "gs": float(start), "ge": float(end), "rating": 8, "core": core,
            "label": "【爆点】测试块"}


def span(segs):
    """一块（可能跨集拆成两片）的总时长。"""
    return sum(s["end"] - s["start"] for s in segs)


def main():
    p = make_project([60.0, 60.0, 46.6])
    ok = []

    # 1) 真的整集块：仍然要被收成一场戏（防连播整集）
    out = analyze._trim_whole(p, [piece(1, 0.0, 60.0, core=30.0)])
    d = span(out)
    ok.append(("整集块仍收窗", 15.0 <= d <= 35.5, f"{d:.1f}s"))

    # 2) 跨集拼块：上一集后 20 秒 + 下一集前 40 秒 = 60 秒，两片都不许动
    cross = [piece(1, 40.0, 60.0, core=55.0), piece(2, 0.0, 40.0, core=10.0)]
    out = analyze._trim_whole(p, cross)
    d = span(out)
    ok.append(("跨集 20+40 保持 60 秒", abs(d - 60.0) < 0.01, f"{d:.1f}s"))

    # 3) 短剧集（46.6 秒）里的 40 秒片：老逻辑按 0.72 比例会收，现在必须保留
    out = analyze._trim_whole(p, [piece(3, 0.0, 40.0, core=20.0)])
    d = span(out)
    ok.append(("46.6 秒集里的 40 秒片不收", abs(d - 40.0) < 0.01, f"{d:.1f}s"))

    # 4) 贴尾不贴首（跨集块的另一种切法）：也不收
    out = analyze._trim_whole(p, [piece(2, 20.0, 60.0, core=40.0)])
    d = span(out)
    ok.append(("贴尾不贴首不收", abs(d - 40.0) < 0.01, f"{d:.1f}s"))

    # 5) 2026-11-21 起模板不再要求「每套给约 N 块备选」（per_blocks 已删）：
    #    块数由模型按「一场戏一块、每块 60 秒以上」自己定，这里不再有断言。

    bad = [x for x in ok if not x[1]]
    for name, passed, detail in ok:
        print(("  OK  " if passed else "  FAIL") + f"  {name}  ({detail})")
    print("RESULT:", "PASS" if not bad else f"FAIL {len(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
