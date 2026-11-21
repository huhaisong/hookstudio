"""按「原片字幕屏」和「句中标点」切分台词行——让每行都是一句能单独改的字。

为什么要有这一步（2026-11-19 用户反馈）：
第 25 集第 3 行「大山掩护我﹑周叔你弄你的﹑」被 whisper 压成一句，
`reseg` 只按 15 字切（这句可见字只有 11），所以没切开；可原片字幕
明明白白是两屏（19.693-21.622 / 21.622-22.75）。用户在「查看 / 修改台词」
里想分别改这两句却做不到。

规则演进（S2，2026-11-19）：
S1 只按屏切——原片同一屏里的多个小句（「陆正、别分神」「来吧、老子的
支援到了」「你不是猴子，谁是猴子啊」）被合并，用户反馈"顿号/逗号也应
该拆成两句"。S2 改成：**句中有标点且后面还有字，就切成独立行**，与原片
是否同屏无关。时间分两种：跨屏的取该屏真实边界（保留 S1 的精度）；同屏
多句按词流逐字时间分、词流对不上按字数比例，并 clamp 在屏内不倒挂。

做法（只用已经算好的数据，不额外扫素材、不调用模型）：
1. 用词流（`retime` 对时那趟已经生成并缓存的逐字时间）把每个字对到时间；
2. 按句读标点把一行切成小句（引号括号不是句读，不切）；
3. 给每个小句定时间：独占一屏→整屏边界；同屏多句→词流字时间；都没有→比例；
4. `lines_time_bak` 按同样字数占比拆开，撤销对时仍然对得上。

判据版本 `VERSION`：改了分句/归属/时间规则就 +1。
"""
from __future__ import annotations

import difflib
import json
import os
import re
import unicodedata

from . import core

KEEP = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")
# 句读标点：只在这些符号处切。引号、括号、破折号等不是句边界，留在段内。
SPLIT_PUNCT = frozenset("、﹐﹑，,；;。！!？?")
WIN = 6.0             # 每行前后各看多少秒找词（与 retime.WIN 同口径）
MIN_COV = 0.60        # 词流覆盖低于这个值就按比例分时间——但仍切文字（标点才是切的依据）
VERSION = "S2"


def _vis(t: str) -> int:
    """可见字数：只数中文/字母/数字，标点空格不算（与 render._cap_vis_len 同口径）。"""
    return sum(1 for c in (t or "") if KEEP.match(c))


def _segments(text: str) -> list[tuple[str, int]]:
    """按句读标点切成 [(原文片段, 可见字数)]，标点留在前一段末尾。

    只有标点在句中（后面还有可见字）时才产生新的一段；末尾标点跟着最后一段，
    不会多切出空行。引号/括号等非句读符号不切。
    """
    out: list[tuple[str, int]] = []
    cur, n = "", 0
    for ch in text or "":
        cur += ch
        if KEEP.match(ch):
            n += 1
        if ch in SPLIT_PUNCT and n > 0:
            out.append((cur, n))
            cur, n = "", 0
    if cur.strip() and n > 0:
        out.append((cur, n))
    return out


def _cached_words(pid: str, ep: dict) -> list[tuple[str, float, float]]:
    """**只读缓存**的词流，绝不触发重新转写。没有就返回空，让这一集跳过切分。"""
    from . import retime
    path = retime._cache_path(pid, ep)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        if int(raw.get("v") or 0) != retime.VERSION:
            return []
        return [(str(x[0]), float(x[1]), float(x[2])) for x in raw.get("words") or []]
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        return []


def _align_chars(want: str, chars, ws: float, we: float) -> tuple[list, float]:
    """每个可见字的时间 (起, 止)。校对改过的字对不上词流时用邻居插值补上。"""
    from . import retime
    stream_text = "".join(c for c, _, _ in chars)
    times: list[tuple[float, float] | None] = [None] * len(want)
    idx = [i for i in range(len(chars)) if ws - WIN <= chars[i][1] <= we + WIN]
    if idx and want:
        lo, hi = idx[0], idx[-1] + 1
        sm = difflib.SequenceMatcher(None, stream_text[lo:hi], want, autojunk=False)
        for b in sm.get_matching_blocks():
            for k in range(b.size):
                times[b.b + k] = (chars[lo + b.a + k][1], chars[lo + b.a + k][2])
    cov = sum(1 for t in times if t) / max(1, len(want))
    known = [i for i, t in enumerate(times) if t]
    if known:
        first, last = known[0], known[-1]
        for i in range(len(times)):
            if times[i]:
                continue
            if i < first:
                times[i] = times[first]
            elif i > last:
                times[i] = times[last]
            else:
                prev = max(j for j in known if j < i)
                nxt = min(j for j in known if j > i)
                t0, t1 = times[prev][1], times[nxt][0]
                k = (i - prev) / (nxt - prev)
                v = t0 + (t1 - t0) * k
                times[i] = (v, v + 0.01)
    else:
        # 没有匹配到任何字（覆盖率 0）——返回 None 让调用方走比例降级
        return [], cov
    return times, cov


def _span_of(mid: float, spans: list[tuple[float, float]]) -> int:
    """字中点落在哪一屏；落在间隙就归最近的一屏。"""
    for i, (s0, s1) in enumerate(spans):
        if s0 <= mid <= s1:
            return i
    return min(range(len(spans)),
               key=lambda i: min(abs(mid - spans[i][0]), abs(mid - spans[i][1])))


def split_lines(lines: list[dict], bak, words,
                spans: list[tuple[float, float]]) -> tuple[list[dict], list | None, dict]:
    """把一行切成「一句一行」：句中有标点就切，与原片是否同屏无关。不改入参。

    时间规则：
      * 该小句独占一屏 → 取该屏的真实起止（S1 精度）；
      * 同一屏有多句 → 用逐字时间分，clamp 在该屏内；
      * 词流对不上 → 按可见字数比例在整行时间范围内分；
      * 没有 spans → 直接用逐字时间（或比例）。
    """
    from . import retime
    chars = retime._stream(words)
    src = list(lines or [])
    keep_bak = isinstance(bak, list) and len(bak) == len(src)
    out: list[dict] = []
    out_bak: list = []
    st = {"lines": len(src), "cut": 0, "added": 0, "lowcov": 0}
    for i, ln in enumerate(src):
        text = str(ln.get("text") or "")
        segs = _segments(text)
        want = "".join(c for c in text if KEEP.match(c))
        if len(segs) < 2 or not want:
            out.append(ln)
            if keep_bak:
                out_bak.append(bak[i])
            continue
        line_s = float(ln.get("start") or 0.0)
        line_e = float(ln.get("end") or 0.0)
        times: list | None = None
        if chars:
            got, cov = _align_chars(want, chars, line_s, line_e)
            if cov >= MIN_COV and got:
                times = got
            else:
                st["lowcov"] += 1
        # 每个小句：文字、可见字数、真实起止、屏号
        infos: list[tuple[str, int, float, float, int | None]] = []
        pos = 0
        acc = 0
        total_vis = sum(n for _, n in segs) or 1
        for seg, n in segs:
            if times:
                a, b = times[pos][0], times[pos + n - 1][1]
            else:
                a = line_s + (line_e - line_s) * acc / total_vis
                acc += n
                b = line_s + (line_e - line_s) * acc / total_vis
            pos += n
            mid = (a + b) / 2
            idx = _span_of(mid, spans) if spans else None
            infos.append((seg, n, a, b, idx))
        # 哪些屏被多句共享（独占的取整屏边界，共享的按字时间分）
        idx_count: dict[int, int] = {}
        for _, _, _, _, idx in infos:
            if idx is not None:
                idx_count[idx] = idx_count.get(idx, 0) + 1
        parts: list[dict] = []
        for seg, _, a, b, idx in infos:
            row = dict(ln)
            row["text"] = seg
            if idx is not None:
                s0, s1 = spans[idx]
                if len(idx_count) >= 2 and idx_count[idx] == 1:
                    row["start"], row["end"] = round(float(s0), 3), round(float(s1), 3)
                else:
                    aa, bb = max(a, s0), min(b, s1)
                    if bb <= aa:
                        aa, bb = a, b
                    row["start"], row["end"] = round(aa, 3), round(bb, 3)
            else:
                row["start"], row["end"] = round(a, 3), round(b, 3)
            parts.append(row)
        st["cut"] += 1
        st["added"] += len(parts) - 1
        # 备份按可见字数占比拆，条数始终和台词一致（撤销对时才能用）
        bs = be = 0.0
        if keep_bak:
            bs, be = float(bak[i][0]), float(bak[i][1])
        total = sum(_vis(p["text"]) for p in parts) or 1
        acc2 = 0
        for p in parts:
            out.append(p)
            if keep_bak:
                k0 = acc2 / total
                acc2 += _vis(p["text"])
                k1 = acc2 / total
                out_bak.append([round(bs + (be - bs) * k0, 3),
                                round(bs + (be - bs) * k1, 3)])
    # 收尾：行间不许倒挂（防御）
    if st["cut"]:
        for j in range(len(out) - 1):
            if out[j]["end"] > out[j + 1]["start"]:
                out[j]["end"] = out[j + 1]["start"]
            if out[j]["end"] < out[j]["start"]:
                out[j]["end"] = out[j]["start"]
    return out, (out_bak if keep_bak else bak), st


def apply_to(p: dict, by_ep: dict, on_ep=None) -> dict:
    """整个项目切分（内存里改，不落盘）。调用方负责 save_merge(["episodes"])。"""
    from . import retime
    stat = {"episodes": 0, "touched": 0, "cut": 0, "added": 0,
            "before": 0, "after": 0, "touched_list": []}
    pid = str(p.get("id") or "")
    for ep in (p.get("episodes") or []):
        lines = ep.get("lines") or []
        if not lines:
            continue
        core.check_cancel()
        stat["episodes"] += 1
        stat["before"] += len(lines)
        spans = retime.spans_of(by_ep.get(ep.get("file")))
        words = _cached_words(pid, ep)
        new, new_bak, st = split_lines(lines, ep.get("lines_time_bak"), words, spans)
        stat["after"] += len(new)
        if st["cut"]:
            ep["lines"] = new
            if new_bak is not None:
                ep["lines_time_bak"] = new_bak
            stat["touched"] += 1
            stat["cut"] += st["cut"]
            stat["added"] += st["added"]
            stat["touched_list"].append(int(ep.get("index") or 0))
        if on_ep:
            on_ep(ep, st)
    return stat
