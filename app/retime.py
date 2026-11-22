"""按「原片字幕的真实出现时刻」校正台词时间码。

背景（2026-11-15 用户反馈「第一集前半段正常、后半段字幕跑在语音前面」）：
whisper 在这类带配乐、对白密集的短剧上会丢音频，每丢一截，它给出的时间码就比
真实语音更早，误差沿时间轴累积（第 1 集到 130 秒时早 1.7 秒、到 146 秒早 4.0 秒）。
不是整体平移，所以「统一减 N 毫秒」修不了。

做法分三步，全部只改时间、不动一个字：
1. 用 word_timestamps 重算一遍词级时间（词的落点比段落准，实测与画面字幕
   出现时刻相差 0.2 秒内），并按相似度把每句台词对到词流上；
2. 去字幕那一步已经把原片每一条字幕的精确起止量出来了（spans，像素级），
   把对上的起点吸附到最近的原字幕出现时刻、终点吸附到该字幕的消失时刻；
3. 单调化 + 夹到片子时长内，对不上的句子原样保留。

结果写回 lines 的 start/end，并把原值存在 ep["lines_time_bak"] 里以便回退。
只改时间：渲染那边直接用这份 start/end，不再叠任何人为偏移。

2026-11-16 用户定：**不再需要单独点一次按钮**，第 2 步生成时自动做（见 apply_to）。
所以这里的 apply_to 必须能被反复调用而不漂移——已按同一份原字幕校正过的集直接
跳过（ep["lines_time_sig"] 记住上次用的是哪份尺子）。实测二次重算会有 4/86 句
因为词流窗口移位而再次小改，若每点一次生成都重算，指纹每次都变、整批无限重做。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re

from . import core, transcribe

KEEP = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")
WIN = 6.0        # 每句在当前时间码前后各看多少秒来找词
SNAP = 0.60      # 词级起点离原字幕出现时刻多近才吸附
MIN_COV = 0.50   # 词流覆盖该句多少字才认这个对齐
MIN_DUR = 0.28
VERSION = 3      # 对齐算法/词流参数版本，改判据或词流参数时 bump，老缓存作废
                 # v2（2026-11-19）：词流参数与主转写同步（no_speech_threshold=0.9），
                 # 且词流可走人声分离；缓存键加 denoise 标记区分两份词流。
                 # v3（2026-11-19 晚）：**关掉 VAD**——它会把整段带背景音的人声丢掉，
                 # 词流因此缺字，句子对不上只能原样保留（第 7 集实测漏 25.8 秒台词）。


def words_dir(pid: str) -> str:
    return os.path.join(core.PROJECTS, pid, "words")


def _cache_path(pid: str, ep: dict) -> str:
    return os.path.join(words_dir(pid), f"{int(ep.get('index') or 0):02d}.json")


def words_of(pid: str, ep: dict) -> list[tuple[str, float, float]]:
    """这一集的词级时间（带缓存）。换模型/算法版本/降噪开关时缓存自动作废。"""
    denoise = bool(core.load_config().get("asr_denoise"))
    path = _cache_path(pid, ep)
    if os.path.isfile(path):
        try:
            raw = json.load(open(path, encoding="utf-8"))
            if (raw.get("v") == VERSION and raw.get("tier") == core.whisper_tier()
                    and raw.get("denoise") == denoise):
                return [tuple(x) for x in raw["words"]]
        except (OSError, ValueError, KeyError):
            pass
    p = core.load(pid) or {}
    src = os.path.join(p.get("src_dir") or "", ep.get("file") or "")
    if not os.path.isfile(src):
        raise RuntimeError(f"找不到素材文件：{ep.get('file')}")
    # 与台词转写走同一份音频：开了人声分离就先分离再解码，否则背景音下词流缺失、
    # 台词对不上词、时间码就飘（2026-11-19 与主转写同步的第二个缺口）。
    audio = transcribe._decode(transcribe._denoise_audio(src))
    model = transcribe._model()
    out: list[tuple[str, float, float]] = []
    with transcribe._INFER_LOCK:
        segs, _ = model.transcribe(
            audio, language="zh", beam_size=5,
            # 与主转写同步：**关掉 VAD**。VAD 会把整段带背景音的人声当静音丢掉，
            # 词流缺了这些字，对应句子的覆盖率不够就只能原样保留时间码——
            # 表现为「这一集有几段字幕明明在动，台词时间却没被校正」。
            vad_filter=False,
            word_timestamps=True, condition_on_previous_text=False,
            # no_speech_threshold 提到 0.9（默认 0.6 时背景音让 no_speech_prob
            # 轻易超标，整段有台词的词流被当静音跳过）。
            no_speech_threshold=0.9,
        )
        for seg in segs:
            # 词级推理的 segments 是惰性生成器，整集推理就在迭代里发生。
            # 每拿到一段查一次中止（B 档 2026-11-19），别让用户点中止后干等整集。
            core.check_cancel()
            for w in (seg.words or []):
                out.append((w.word, float(w.start), float(w.end)))
    os.makedirs(words_dir(pid), exist_ok=True)
    try:
        json.dump({"v": VERSION, "tier": core.whisper_tier(), "denoise": denoise,
                   "words": out},
                  open(path, "w", encoding="utf-8"), ensure_ascii=False)
    except OSError:
        pass
    return out


def _stream(words) -> list[tuple[str, float, float]]:
    """词 -> 逐字时间：同一个词里的几个字按字均分它占的时长。"""
    chars = []
    for raw, ws, we in words:
        pos = [i for i, c in enumerate(raw) if KEEP.match(c)]
        if not pos:
            continue
        n = len(pos)
        span = max(0.0, we - ws)
        for k, i in enumerate(pos):
            chars.append((raw[i], ws + span * k / n, ws + span * (k + 1) / n))
    return chars


def _align(lines: list[dict], chars) -> list[tuple[float, float, float]]:
    """每句 -> (词级起点, 词级终点, 覆盖率)。覆盖率低说明词流里没有这句的字。"""
    text = "".join(c for c, _, _ in chars)
    out = []
    for ln in lines:
        cs, ce = float(ln["start"]), float(ln["end"])
        idx = [i for i in range(len(chars)) if cs - WIN <= chars[i][1] <= ce + WIN]
        want = "".join(c for c in str(ln.get("text") or "") if KEEP.match(c))
        if not idx or not want:
            out.append((cs, ce, 0.0))
            continue
        lo, hi = idx[0], idx[-1] + 1
        sm = difflib.SequenceMatcher(None, text[lo:hi], want, autojunk=False)
        blocks = [b for b in sm.get_matching_blocks() if b.size >= 1]
        if not blocks:
            out.append((cs, ce, 0.0))
            continue
        cover = sum(b.size for b in blocks) / len(want)
        best = max(blocks, key=lambda b: b.size)
        out.append((chars[lo + best.a][1], chars[lo + best.a + best.size - 1][2], cover))
    return out


def _snap(ws: float, we: float, spans: list[tuple[float, float]]) -> tuple[float, float]:
    """起点吸到最近的原字幕出现时刻，终点吸到该条字幕的消失时刻。"""
    if not spans:
        return ws, we
    d, a = min(((abs(ws - s0), s0) for s0, _ in spans))
    s = a if d <= SNAP else ws
    for s0, s1 in spans:
        if s0 <= we <= s1:
            return s, s1
    ahead = [s0 for s0, _ in spans if s0 >= we]
    if ahead and ahead[0] - we <= SNAP:
        return s, ahead[0]
    return s, we


def fix_times(lines: list[dict], spans: list[tuple[float, float]],
              words, dur: float) -> tuple[list[tuple[float, float]], dict]:
    """算出每句的新起止。返回 (新时间, 统计)。"""
    chars = _stream(words)
    pairs = _align(lines, chars)
    new: list[list[float]] = []
    moved = kept = 0
    for (cs, ce), (ws, we, cov) in zip([(float(l["start"]), float(l["end"])) for l in lines], pairs):
        if cov < MIN_COV:
            new.append([cs, ce])
            kept += 1
            continue
        s, e = _snap(ws, we, spans)
        if abs(s - cs) >= 0.01:
            moved += 1
        new.append([max(0.0, s), max(0.0, e)])

    # 单调 + 最短时长 + 不许超出片长
    prev_s = -1.0
    for i in range(len(new)):
        if new[i][0] < prev_s + 0.02:
            new[i][0] = prev_s + 0.02 if prev_s >= 0 else 0.0
        if new[i][1] < new[i][0] + MIN_DUR:
            new[i][1] = new[i][0] + MIN_DUR
        if dur > 0:
            new[i][0] = min(new[i][0], max(0.0, dur - MIN_DUR))
            new[i][1] = min(new[i][1], dur)
        prev_s = new[i][0]
    for i in range(len(new) - 1):
        if new[i][1] > new[i + 1][0]:
            new[i][1] = max(new[i][0] + MIN_DUR, new[i + 1][0])

    good = [i for i in range(len(lines)) if pairs[i][2] >= MIN_COV]
    shift = [abs(new[i][0] - float(lines[i]["start"])) for i in good]
    stats = {
        "moved": moved, "kept": kept, "aligned": len(good),
        "median_shift": sorted(shift)[len(shift) // 2] if shift else 0.0,
        "max_shift": max(shift) if shift else 0.0,
    }
    # 大面积对不上 = 词流跟台词不是同一段话，这时宁可不改
    if len(good) < len(lines) * 0.6:
        raise RuntimeError(f"只有 {len(good)}/{len(lines)} 句能对上词流，这一集不改")
    return [(round(s, 3), round(e, 3)) for s, e in new], stats


def spans_sig(spans) -> str:
    """这份「尺子」的指纹：原字幕位置变了（重识别、换素材）才需要重新校正。"""
    h = hashlib.sha1()
    for s0, s1 in sorted(spans):
        h.update(f"{s0:.3f}|{s1:.3f};".encode())
    return h.hexdigest()[:12]


def spans_of(entry) -> list[tuple[float, float]]:
    """nosub._spans 的单集结果 -> 排好序的 (出现, 消失)。"""
    out = []
    for x in (entry or {}).get("spans") or []:
        try:
            out.append((float(x["t0"]), float(x["t1"])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out)


def apply_to(p: dict, by_ep: dict, on_msg=None, strict: bool = False,
             force: bool = False) -> dict:
    """按原片字幕把 p（**内存里**的项目）的台词时间重排一遍，不改盘。

    by_ep = {文件名: {"spans": [...]}}，就是 nosub._spans 的结果——这一步在
    第 2 步里本来就要算（抹字幕要用），所以「先量位置再对时间」不额外花扫描钱。

    已按同一份 spans 校正过的集直接跳过：
      · 有 lines_time_bak（说明校正过）且里面存的尺子指纹 = 现在这份；
      · 老项目没有指纹但有 bak，视为「用当前尺子校正过」，补写指纹不再重算——
        否则第一次上自动模式会把这 31 集重算一遍，虽然只有几毫米的差，
        但指纹一变整批就要重做，白白多等二十分钟。
    strict=False 时单集失败只记下来、继续下一集（批量生成不能因为一集垮掉）。
    """
    eps = [e for e in (p.get("episodes") or []) if e.get("lines")]
    todo = [e for e in eps if spans_of(by_ep.get(e.get("file")))]
    stat = {"total": len(todo), "fixed": 0, "kept": 0, "skipped": 0,
            "aligned": 0, "moved": 0, "errors": {}, "miss": len(eps) - len(todo)}
    if not todo:
        if strict:
            raise RuntimeError("还没有量出原片字幕的位置，先在第 2 步生成一次")
        return stat
    pid = str(p.get("id") or "")
    for i, ep in enumerate(todo):
        core.check_cancel()
        if on_msg:
            on_msg(i, len(todo), ep)
        spans = spans_of(by_ep.get(ep.get("file")))
        sig = spans_sig(spans)
        bak = ep.get("lines_time_bak")
        if (not force and isinstance(bak, list) and len(bak) == len(ep["lines"])
                and ep.get("lines_time_sig") in (None, sig)):
            ep["lines_time_sig"] = sig
            stat["skipped"] += 1
            continue
        try:
            words = words_of(pid, ep)
            new, st = fix_times(ep["lines"], spans, words,
                                float(ep.get("duration") or 0.0))
        except Exception as exc:  # noqa: BLE001
            if strict:
                raise
            stat["errors"][str(ep.get("file") or ep.get("index"))] = str(exc)[:120]
            print(f"[retime] 集{ep.get('index')} 跳过：{exc}", flush=True)
            continue
        # 备份只在「没有可信备份」时写：第一次校正前的时间码才是要能回退到的那个。
        # 台词增删会让旧备份与 lines 对不上（revert 也会因此跳过），这时重建。
        if not isinstance(bak, list) or len(bak) != len(ep["lines"]):
            ep["lines_time_bak"] = [[float(x["start"]), float(x["end"])]
                                    for x in ep["lines"]]
        for ln, (s, t) in zip(ep["lines"], new):
            ln["start"], ln["end"] = s, t
        ep["lines_time_sig"] = sig
        stat["fixed"] += 1
        stat["aligned"] += int(st["aligned"])
        stat["moved"] += int(st["moved"])
        print(f"[retime] 集{ep.get('index')}：对齐 {st['aligned']} 句，改动 {st['moved']} 句，"
              f"中位位移 {st['median_shift']:.2f}s，最大 {st['max_shift']:.2f}s",
              flush=True)
    return stat


def run(pid: str, force: bool = False) -> None:
    """后台任务：逐集校正时间码。只改 lines 的 start/end，不动文字。

    force=True（手动入口）时连「已经校正过」的集也重算一遍。
    """
    p = core.load(pid)
    if not p:
        return
    by = ((p.get("nosub") or {}).get("spans") or {}).get("by_ep") or {}
    if not any(e.get("lines") for e in (p.get("episodes") or [])):
        raise RuntimeError("还没有台词，先去第 1 步「台词整理」")
    stat = apply_to(
        p, by, strict=True, force=force,
        on_msg=lambda i, n, ep: core.set_task(
            pid, "retime", i / max(1, n) * 100,
            f"正在校正第 {ep['index']} 集（{i + 1}/{n}）"))
    # 对时之后按「原片字幕的一屏」切行（见 app/span_split.py）。
    from . import span_split
    sst = span_split.apply_to(p, by)
    core.save_merge(p, ["episodes"])
    core.set_task(pid, None)
    # 时间码/切行变了，烧出来的字幕跟着变：记一版，下次出片重渲烧了旧时间的套
    # （fx_sig 的 LN 位，2026-11-22）。没动任何行就不 bump，别白让成片作废。
    if stat["fixed"] or sst.get("cut") or sst.get("added"):
        core.bump_lines_ver(pid)
    print(f"[retime] 完成 {stat['fixed'] + stat['skipped']}/{stat['total']} 集"
          f"（重算 {stat['fixed']}、已对过 {stat['skipped']}）"
          f"，按字幕屏切行 {sst['cut']} 行 → 多 {sst['added']} 行"
          + (f"，{stat['miss']} 集没有字幕位置未处理" if stat["miss"] else ""),
          flush=True)


def revert(pid: str) -> int:
    """回退到校正前的时间码（只回时间，不动文字）。返回还原的集数。
    同时清掉尺子指纹，下次不会再判成「已经对过了」。"""
    p = core.load(pid) or {}
    n = 0
    for e in p.get("episodes") or []:
        bak = e.get("lines_time_bak")
        if not bak or len(bak) != len(e.get("lines") or []):
            continue
        for ln, (s, t) in zip(e["lines"], bak):
            ln["start"], ln["end"] = float(s), float(t)
        e.pop("lines_time_bak", None)
        e.pop("lines_time_sig", None)
        n += 1
    if n:
        core.save_merge(p, ["episodes"])
        core.bump_lines_ver(pid)   # 回退也是时间码变更，烧过的套同样要重渲
    return n
