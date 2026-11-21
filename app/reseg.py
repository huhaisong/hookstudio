"""重排台词行：把「一句」切成和屏幕上一屏字幕等长的一条一条。

为什么要有这一步（2026-11-19 用户定）：
whisper 有时会把一长串连说压进一个 segment（第 10 集第 1 句：159 字 / 13.9 秒），
烧字幕时 `render._cap_split_row` 会按 15 字拆成十几屏，可**台词数据本身**还是那一整句。
于是用户在「查看 / 修改台词」里看到的是一段小作文，改起来也没法定位到某一屏；
更麻烦的是后面的分镜、剪辑都按「句」算，一屏的字数就是一步的量。

做法：**复用烧字幕那套拆分**（`render._cap_split_row`），保证数据里的行和屏幕上的
字幕一一对应，不出现「数据一套、画面另一套」。时间按字数占比分，首尾相接不留空；
原时间备份 `lines_time_bak` 按同一比例拆开，撤销对时仍然对得上。
"""
from __future__ import annotations

from . import core


def _mx(max_chars: int | None) -> int:
    """一屏最多多少字——只有一个来源：`render.CAP_MAX_CHARS`。

    这里刻意不自己写死 15：改烧字上限时数据跟着变，不会出现「画面 12 字、数据 15 字」。
    """
    if max_chars:
        return int(max_chars)
    from . import render
    return int(render.CAP_MAX_CHARS)


def split_lines(lines: list[dict], bak: list | None = None,
                max_chars: int | None = None) -> tuple[list[dict], list | None, dict]:
    """把过长的行按标点拆开。返回 (新台词, 新备份, 统计)。不改入参。

    bak：`lines_time_bak`（每条 [start, end]）。给了一并按同样比例拆，保证条数与
    台词始终相等——否则「撤销对时」会因条数不符被跳过，用户就退不回原时间。
    """
    from . import render
    mx = _mx(max_chars)
    src = list(lines or [])
    keep_bak = isinstance(bak, list) and len(bak) == len(src)
    out: list[dict] = []
    out_bak: list = []
    st = {"lines": len(src), "split": 0, "added": 0, "lens": 0}
    for i, ln in enumerate(src):
        s, e = float(ln.get("start") or 0.0), float(ln.get("end") or 0.0)
        text = str(ln.get("text") or "")
        parts = render._cap_split_row(s, e, text, mx)
        if len(parts) <= 1:                     # 不长，原样留着（连时间都不碰）
            out.append(ln)
            if keep_bak:
                out_bak.append(bak[i])
            continue
        st["split"] += 1
        st["added"] += len(parts) - 1
        span = max(0.0, e - s)
        bs = be = 0.0
        if keep_bak:
            bs, be = float(bak[i][0]), float(bak[i][1])
        for ps, pe, pt in parts:
            row = dict(ln)
            row["start"] = round(float(ps), 3)
            row["end"] = round(float(pe), 3)
            row["text"] = pt
            out.append(row)
            if keep_bak:
                k0 = (float(ps) - s) / span if span > 0 else 0.0
                k1 = (float(pe) - s) / span if span > 0 else 1.0
                out_bak.append([round(bs + (be - bs) * k0, 3),
                                round(bs + (be - bs) * k1, 3)])
    # 收尾校正：整段起点/终点必须与原来一致（拆分只切内部，不许把整句缩短）
    if st["split"]:
        st["lens"] = len(out)
        for j, ln in enumerate(out):
            nxt = out[j + 1]["start"] if j + 1 < len(out) else None
            if nxt is not None and ln["end"] > nxt:
                ln["end"] = nxt
            if ln["end"] < ln["start"]:
                ln["end"] = ln["start"]
    return out, (out_bak if keep_bak else bak), st


def split_episode(ep: dict, max_chars: int | None = None) -> dict:
    """就地重排一集（改 ep["lines"] 与 ep["lines_time_bak"]）。返回统计。"""
    if ep.get("lines"):
        new, new_bak, st = split_lines(ep["lines"], ep.get("lines_time_bak"), max_chars)
        if st["split"]:
            ep["lines"] = new
            if new_bak is not None and new_bak is not ep.get("lines_time_bak"):
                ep["lines_time_bak"] = new_bak
            return st
    return {"lines": len(ep.get("lines") or []), "split": 0, "added": 0, "lens": 0}


def apply_to(p: dict, max_chars: int | None = None, on_ep=None) -> dict:
    """整个项目重排（内存里改，不落盘）。返回汇总，供日志和自测断言。"""
    stat = {"episodes": 0, "touched": 0, "split": 0, "added": 0,
            "before": 0, "after": 0, "touched_list": []}
    for ep in (p.get("episodes") or []):
        if not ep.get("lines"):
            continue
        core.check_cancel()
        stat["episodes"] += 1
        stat["before"] += len(ep["lines"])
        st = split_episode(ep, max_chars)
        stat["after"] += len(ep["lines"])
        if st["split"]:
            stat["touched"] += 1
            stat["split"] += st["split"]
            stat["added"] += st["added"]
            stat["touched_list"].append(int(ep.get("index") or 0))
        if on_ep:
            on_ep(ep, st)
    return stat
