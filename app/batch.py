"""批量模式：AI 通读全剧台词，一次排出 N 套剪辑结构，用户勾选后再逐套渲染。

流程（对应界面 3 步）：
1. 转写 —— 建项目后自动跑
2. 剪辑结构 —— AI 一次看完整部剧，直接排出 N 套 EDL（调 1 次 API），用户勾选要哪几套
3. 出片 —— 只渲染勾选的套

（2026-11-02 移除了「精彩片段池」这一步：池子只是本机组装的素材来源，
AI 编排直接读台词，不再需要先挖池、也不再有本机组装这条路。）
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import analyze, core, nosub, render

# 套间重复率上限（80%）：**只用于提示和前端标色，不拦截落盘**（2026-11-12 用户：
# 「原来程序只在事后上报重复率，不拦截。我不需要你去拦截」）。
# 同一部剧剪 N 套，素材复用是必然的——6 套 × 4.5 分钟要 27 分钟素材，全剧只有 21 分钟。
# 数字的**唯一来源是 analyze.OVERLAP_CAP**：plans.txt 第 5 条按它给模型下约束，
# 这里按它核算，两边必须是同一个数（2026-11-12 用户：重复率不能高于 80%）。
MAX_OVERLAP = analyze.OVERLAP_CAP

# 第 2 步多组对比（2026-11-07）：原来一组一组串行，5 组 = 5 × 约 3 分钟 = 15-23 分钟。
# 各组互不依赖，改成几组同时跑；上限由 core.max_concurrent() 现读设置页的
# 「同时生成几条」（默认 3，可在 1-8 之间调），与第 3 步共用同一档。
# 2026-11-08 前这里写死 PLAN_MAX_CONCURRENT = 3。
# 多组时单组只等 5 分钟：一个模型卡住不该拖死整批，失败会被 job_errors 单独记下，
# 其它组照常落盘。单组（界面默认）仍用 analyze.LLM_TIMEOUT=600，避免误杀慢模型。
PLAN_JOB_TIMEOUT = 300
# 进度文案里的预计时间：本地 4 次任务实测每次调用平均 194 秒 ≈ 3 分钟。
PLAN_EST_MIN = 3

# 用户可见的「档位名」，与 web/app.js 的 MODEL_LABEL 必须同步维护。
# 后端 note / 报错里也不许再出现模型代号（2026-11-07 界面清理定的规矩）。
MODEL_LABELS = {
    "qwen3.5-plus": "默认", "ZHIPU/GLM-5.2": "快速",
    "ZHIPU/GLM-5.3": "快速·长剧本", "deepseek-v4-pro": "精细",
    "deepseek-flash": "轻快", "qwen-max": "备用",
}


def _model_label(v: str) -> str:
    if not v:
        return "默认"
    return MODEL_LABELS.get(v, "自定义")


def legacy_plan_result(p: dict) -> dict | None:
    """老项目（2026-11-16 之前生成的）没有 batch.plan_result，读项目时补一份**只读推断**。

    只认两个既有事实：batch.jobs 每组要几套、plans[] 每套的 model 标记；差额就是
    那组没出来的套数。不猜失败原因、不猜时间，原因指向已有的 plan_note 原文。
    推断结果不落盘：下一次生成（含补跑）会写真正的 plan_result 把它覆盖掉。
    """
    b = p.get("batch") or {}
    jobs = b.get("jobs") or []
    plans = p.get("plans") or []
    if not jobs or not plans or any(pl.get("mode") == "hook" for pl in plans):
        return None
    requested = sum(max(0, int(j.get("count") or 0)) for j in jobs)
    if requested <= len(plans):
        return None
    got_by_model: dict[str, int] = {}
    for pl in plans:
        m = str(pl.get("model") or "")
        got_by_model[m] = got_by_model.get(m, 0) + 1
    left = dict(got_by_model)
    failed = []
    for j in jobs:
        m = str(j.get("model") or "")
        want = max(0, int(j.get("count") or 0))
        have = min(left.get(m, 0), want)
        left[m] = left.get(m, 0) - have
        missing = want - have
        if missing <= 0:
            continue
        failed.append({
            "model": m, "label": _model_label(m), "count": missing,
            "min_minutes": float(j.get("min_minutes") or 3.0),
            "max_minutes": float(j.get("max_minutes") or 7.0),
            "reason": "上次这一组没生成完，具体原因见下方生成说明",
        })
    if not failed:
        return None
    return {"requested": requested, "got": len(plans), "total": len(plans),
            "append": False, "failed": failed, "legacy": True}


def _seg_key(s: dict) -> tuple:
    return (s["ep"], round(s["start"], 1), round(s["end"], 1))


def _blk_key(b: dict):
    return b.get("blk") or _seg_key(b)


def _ensure_geo(p: dict, segs: list[dict]) -> list[dict]:
    """补齐全局坐标。老池子（按集内切）和用户手改过的池子都没有 gs/ge/blk，这里统一补上。

    gs/ge = 整条素材时间轴上的秒数（每集按时长首尾相接累加）。有了它才能判断
    「这两段是不是紧挨着」——跨集衔接全靠这个。
    """
    off = analyze.ep_offsets(p)
    for s in segs:
        if s.get("gs") is None:
            s["gs"] = round(off.get(s["ep"], 0.0) + s["start"], 3)
        if s.get("ge") is None:
            s["ge"] = round(off.get(s["ep"], 0.0) + s["end"], 3)
        if not s.get("blk"):
            s["blk"] = f"s{_seg_key(s)}"
    return segs


def _blocks(segs: list[dict]) -> list[dict]:
    """碎片 → 块。一个块可能跨集（由 2-3 片组成），搬动时整块搬，剧情不断。"""
    groups: dict[str, list[dict]] = {}
    for s in segs:
        groups.setdefault(s["blk"], []).append(s)
    out = []
    for k, ps in groups.items():
        ps.sort(key=lambda s: s["gs"])
        out.append({
            "blk": k,
            "pieces": ps,
            "gs": ps[0]["gs"],
            "ge": max(p["ge"] for p in ps),
            "dur": sum(p["end"] - p["start"] for p in ps),
            "rating": sum(p.get("rating") or 5 for p in ps) / len(ps),
            "label": ps[0].get("label") or "",
        })
    return sorted(out, key=lambda b: b["gs"])


# 播放顺序：**只有集序一种**（2026-11-03 用户决定，勿改回）。
# 原来这里轮换 6 种叙事策略（最炸开场 / 首尾双爆点 / 后段冷开场再回溯 / 两幕爆发…），
# auto 模式下 6 套里 5 套都把高分的块挪到最前——观众开场看到的就是后面才发生的戏，
# 因果断了，违背连贯性第一原则。提示词没有让模型这么做（它给的 segments 本来就是
# 集序），是**本地重排代码**干的。凡是「顺序」层面的花样，一律不再做：套与套的不同
# 靠选材不同，不靠把块搬来搬去。
ORDER_CHRONO = "chrono"
ORDER_DESC = "严格集序：按剧情先后推进，只保证观众不用脑补就能跟上"


def _merge_spans(segs: list[dict], ep: int) -> list[list[float]]:
    """某一集里的片，合并成互不重叠的区间。

    为什么必须合并：同一套里同一集可能有两块（收窗后一块只占一集的一小段，
    这套取前半场、那块取后半场），它们和别套的同一段都相交时，双重循环会把
    同一段重合**算两遍**，重合度就冒出 120%、131% 这种不可能的数字
    （2026-11-03 实测，见下方 _overlap）。
    """
    spans = sorted((float(s["start"]), float(s["end"]))
                   for s in segs if s["ep"] == ep)
    out: list[list[float]] = []
    for a, b in spans:
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _span_sec(segs: list[dict]) -> float:
    """一套片子实际占用的素材秒数（同一集内不重复计）。"""
    return sum(sum(b - a for a, b in _merge_spans(segs, ep))
               for ep in {s["ep"] for s in segs})


def _inter_sec(pieces: list[dict], other: list[dict]) -> float:
    """两组片在时间轴上重合的秒数（同一集内按区间求交，区间先合并）。"""
    tot = 0.0
    for ep in {s["ep"] for s in pieces} & {s["ep"] for s in other}:
        A, B = _merge_spans(pieces, ep), _merge_spans(other, ep)
        i = j = 0
        while i < len(A) and j < len(B):
            lo = max(A[i][0], B[j][0])
            hi = min(A[i][1], B[j][1])
            if hi > lo:
                tot += hi - lo
            if A[i][1] < B[j][1]:
                i += 1
            else:
                j += 1
    return tot


def _overlap(a: list[dict], b: list[dict]) -> float:
    """两套结构的时间重合度（重合秒数 / 较短一套的素材秒数）。

    分母用合并后的素材秒数，保证结果落在 0-100%：同一套里两块都落在同一集时，
    简单把块长相加会把这一集的时长算两遍，分母偏大、比例偏小。
    """
    da, db = _span_sec(a), _span_sec(b)
    if da <= 0 or db <= 0:
        return 0.0
    return min(1.0, _inter_sec(a, b) / min(da, db))


def _anchor_hit(a: list[dict], b: list[dict], tol: float = 3.0) -> int:
    """两套的「名场面锚点」撞车条数：core 时刻相差 3 秒以内算同一条。

    用户 2026-11-02 第 4 条：说「重合 50% 健康」容易被理解成随机共用素材，
    实际上可以共用的是**普通推进块**，不能共用的是**名场面**——A 套拿「当众揭穿」
    当结尾、B 套拿同一个镜头当开头，观众刷到第二套前五秒就认出，直接划走。
    素材重合百分比看不出这件事（一个 20 秒的锚点只占 5% 时长，撞了也才 5%），
    所以单独数一遍。这里只统计、只上报，不拦截。
    """
    def _cs(x: list[dict]) -> list[tuple[int, float]]:
        out = []
        for s in x:
            try:
                if s.get("core") is not None:
                    # 必须连集号一起带上：只比 core 秒数会把「第 3 集 20 秒」和
                    # 「第 11 集 19 秒」算成同一条名场面（每集都是 0-100 秒，
                    # 不同集落在同一个秒数太常见），锚点撞车数会虚高。
                    out.append((int(s.get("ep") or 0), float(s["core"])))
            except (TypeError, ValueError):
                pass
        return out

    ca, cb = _cs(a), _cs(b)
    return sum(1 for ep, x in ca
               if any(ep == ep2 and abs(x - y) <= tol for ep2, y in cb))


def ai_plan_run(pid: str, count: int, min_minutes: float = 4.0,
                max_minutes: float = 5.0, order: str = ORDER_CHRONO,
                model: str | None = None) -> None:
    """第 2 步：让 AI 一次看完整部剧，直接排出 N 套结构（单模型入口）。

    model 不为空 = 这次编排单独指定模型（第 2 步的「选模型」下拉/输入框）；
    路由仍走 core.route 同一条规则。多模型各配参数走 ai_plan_multi。
    """
    return ai_plan_multi(pid, [{
        "model": model or "", "count": count,
        "min_minutes": min_minutes, "max_minutes": max_minutes,
    }], order)


def ai_plan_multi(pid: str, jobs: list[dict], order: str = ORDER_CHRONO,
                  append: bool = False) -> None:
    """第 2 步（多模型版）：jobs 里每个模型各配一套 {model, count, min/max_minutes}，
    各走 1 次 API（多组时按设置里的「同时生成几条」并行），所有套落到同一个项目里、index 连续编号、
    每套带 model 标记。

    **并发不改变产物**：AI 各跑各的，跑完后仍严格按 jobs 顺序汇总，跨组重合度、
    index 编号、note 都跟老版串行一字不差。并发阶段各组静默不写进度，由这里统一
    按「已完成 N/M 组」推进，避免进度条来回跳。

    后面模型的套在算重合度时能看到前面**所有**套（含别的模型出的）——
    这正是用户要的「这批 12 套之间有多重复」的如实数字。

    **重合度不拦截**：同一部剧剪 N 套，素材必然复用。强行卡线只会让
    API 白花钱、还一套都拿不到。超线就在 note 里点名，由用户决定。
    （2026-11-12 用户明确：「原来程序只在事后上报重复率，不拦截。我不需要你去拦截。」
    之前那版在 _fit_target 里逐块卡 70%，已按此撤销。）

    append=True 是「只补跑没生成成功的组」（2026-11-16 A 档）：产出**追加**到现有
    plans 后面，index 继续编号，绝不覆盖已有结构或已出片的进度。accepted 先装进
    旧套的 edl，这样新套的重复度会跟全部旧套一起算，点名里的套号也是全局的。
    batch.plan_result 记本次「要几套 / 到手几套 / 哪几组没出来（带可重跑参数）」，
    界面据此显示提示条和补跑按钮；全部成功时 failed 为空，提示条自动消失。
    """
    p = core.load(pid)
    if not p:
        return
    if not (p.get("episodes") or []):
        raise RuntimeError("还没有转写结果，请先转写")
    if not jobs:
        raise RuntimeError("没有指定任何模型")
    existing: list[dict] = list(p.get("plans") or []) if append else []
    if append and any(pl.get("mode") == "hook" for pl in existing):
        raise RuntimeError("当前是单集钩子结构，不能往里追加混剪的套")
    total_all = sum(float(ep.get("duration") or 0) for ep in p["episodes"])
    plans: list[dict] = list(existing)   # 旧套先入列，index = len(plans)+1 天然连续
    accepted: list[list[dict]] = [pl["edl"] for pl in existing if pl.get("edl")]
    fresh_start = len(existing)
    job_summ: list[dict] = []       # 每个模型的产出小结（写进 note）
    budget_notes: list[str] = []
    total_sets = sum(max(1, min(20, int(j.get("count") or 1))) for j in jobs)
    done_sets = 0
    job_errors: list[str] = []
    # 结构化失败清单：前端提示条按它列明细，补跑按钮按它带参数重跑
    failed_jobs: list[dict] = []
    errored_ji: set[int] = set()
    n_jobs = len(jobs)
    multi = n_jobs > 1
    prepared: list[dict] = []
    for ji, job in enumerate(jobs):
        count = max(1, min(20, int(job.get("count") or 1)))
        mn = max(0.5, float(job.get("min_minutes") or 3.0))
        mx = max(0.5, float(job.get("max_minutes") or 7.0))
        if mn > mx:
            mn, mx = mx, mn
        label = (job.get("model") or "").strip()
        # 失败的模型也占一个位置：回填界面按行号对齐，漏掉一行后面的套数和时长会串行
        job_summ.append({"model": label, "plans": [], "mn": mn, "mx": mx, "count": count})
        span = 100.0 / n_jobs
        base = ji * span
        prefix = (f"第 {ji + 1}/{n_jobs} 组" + (f"（{_model_label(label)}）" if label else "") + "："
                  if n_jobs > 1 else "")

        # 素材够不够，先算清楚再花钱（只预警、不阻止，见函数 docstring）
        budget = count * (mn + mx) / 2 * 60
        if total_all > 0 and budget > total_all * 0.8:
            cap = max(1.0, total_all * 0.8 / count / 60)
            seat = max(1, int(total_all * 0.8 / ((mn + mx) / 2 * 60)))
            budget_notes.append(
                f"{_model_label(label)}：素材共 {total_all / 60:.1f} 分钟，{count} 套 × 约 {(mn + mx) / 2:.1f} 分钟"
                f"需要 {budget / 60:.1f} 分钟，超出可复用范围——套间大量重复是必然的。"
                f"要么每套压到 {cap:.1f} 分钟以内，要么只出 {seat} 套")
        prepared.append({"ji": ji, "count": count, "mn": mn, "mx": mx,
                         "label": label, "span": span, "base": base, "prefix": prefix})

    # ── 第一阶段：各组的 AI 调用 ────────────────────────────────────────
    # 各组互不依赖（每组只读同一份 p，不改它），多组时按设置的上限并行；单组保持
    # 老行为（进度由 build_plans 自己写、超时用全局 600 秒）。异常当结果带回，
    # 到第二阶段按组顺序记进 job_errors，避免线程池里抛异常把整批带走。
    def _one(it: dict):
        # 池子里的子线程没有 start_task 设的上下文，AI 请求认不出自己属于
        # 哪个任务，中止标记就查不到（2026-11-16）。绑一次，finally 里解绑。
        core.bind_call_ctx(pid, "cuts")
        try:
            return analyze.build_plans(
                p, it["count"], it["mn"], it["mx"], model=it["label"] or None,
                progress=False if multi else (it["base"], it["span"] * 0.92, it["prefix"]),
                timeout=PLAN_JOB_TIMEOUT if multi else None)
        except Exception as exc:  # noqa: BLE001
            return exc
        finally:
            core.bind_call_ctx(None, None)

    results: list = [None] * n_jobs
    if multi:
        conc = min(core.max_concurrent(), n_jobs)
        batches = math.ceil(n_jobs / conc)
        core.set_task(pid, "plan", 3,
                      f"共 {n_jobs} 组，预计约 {batches * PLAN_EST_MIN} 分钟")
        pool = ThreadPoolExecutor(max_workers=conc)
        try:
            futs = {pool.submit(_one, it): it["ji"] for it in prepared}
            finished = 0
            for fut in as_completed(futs):
                results[futs[fut]] = fut.result()
                finished += 1
                left = math.ceil((n_jobs - finished) / conc) * PLAN_EST_MIN
                tail = f" · 预计还需约 {left} 分钟" if left else " · 正在整理结果"
                core.set_task(pid, "plan", 3 + 89 * finished / n_jobs,
                              f"已完成 {finished}/{n_jobs} 组{tail}")
        finally:
            # 原来 with 的 __exit__ 是 wait=True：只要有一组抛 TaskCancelled，
            # 也会被剩下几组 300/600 秒的请求拖住，中止看起来还是「没反应」。
            # 正常跑完时所有 future 都已完成，wait=False 不改变任何行为。
            pool.shutdown(wait=False, cancel_futures=True)
    else:
        results[0] = _one(prepared[0])

    # ── 第二阶段：严格按 jobs 顺序汇总（跟老版串行逐字一致）─────────────
    for it in prepared:
        ji, label = it["ji"], it["label"]
        prefix, base, span = it["prefix"], it["base"], it["span"]
        mn, mx = it["mn"], it["mx"]
        sets = results[ji]
        if isinstance(sets, Exception):
            # 单组失败只记录、不中断：5 组里 1 组超时，不能把另外 4 组的成果带走
            job_errors.append(f"{_model_label(label)}：{sets}")
            errored_ji.add(ji)
            failed_jobs.append({
                "model": label, "label": _model_label(label),
                "count": it["count"], "min_minutes": mn, "max_minutes": mx,
                "reason": str(sets),
            })
            continue
        job_plans: list[dict] = []
        for i, st in enumerate(sets, 1):
            segs = _ensure_geo(p, st["segments"])
            blocks = _blocks(segs)
            if not blocks:
                continue
            # 顺序只有集序一种（2026-11-03 用户决定）：把高分块挪到开头当钩子，
            # 观众开场看到的就是后面才发生的戏，因果断了。order 参数照收不误，
            # 但不管传什么都归一到集序——老请求体里存的 "auto" 也不会再触发重排。
            ordered = blocks
            used_mode = ORDER_CHRONO
            edl = [ps for b in ordered for ps in sorted(b["pieces"], key=lambda s: s["gs"])]
            sec = sum(b["dur"] for b in ordered)

            # 与前面每一套的重合度都算，取最高的一对，用于「点名」
            vs, ov = None, 0.0
            av, ah = None, 0
            for j, a in enumerate(accepted, 1):
                o = _overlap(edl, a)
                if o > ov:
                    vs, ov = j, o
                h = _anchor_hit(edl, a)
                if h > ah:
                    av, ah = j, h
            accepted.append(edl)

            # 连续爆点：rating≥8 的块连着出现几块。超过四块就是「一路耳光」，
            # 中间没有让观众喘气的地方——用户 2026-11-02 第 3、5 条说的就是这个。
            run = best_run = 0
            for b in ordered:
                if float(b.get("rating") or 5) >= 8.0:
                    run += 1
                    best_run = max(best_run, run)
                else:
                    run = 0

            gaps = [b["gs"] - a["ge"] for a, b in zip(edl, edl[1:]) if b["gs"] - a["ge"] > 3.0]
            eps = sorted({s["ep"] for s in edl})
            # 整集块：从 0:00 圈到集尾。模型很容易把「这一集正好是一场戏」当成「一块」，
            # 于是 4.5 分钟只装得下 5 集——等于把这 5 集重播一遍，混剪就没了意义
            # （2026-11-02 实测：块长一放开就往这个方向过冲）。这里**只统计、只上报**，
            # 不自动裁——裁哪一头都可能掐断因果，得让用户看着片子判断。
            ep_dur = {e["index"]: float(e.get("duration") or 0) for e in p["episodes"]}
            whole = 0
            for b in ordered:
                eps_in = {x["ep"] for x in b["pieces"]}
                if len(eps_in) != 1:
                    continue
                e0 = next(iter(eps_in))
                a0 = min(float(x["start"]) for x in b["pieces"])
                z0 = max(float(x["end"]) for x in b["pieces"])
                if a0 <= 0.8 and z0 >= ep_dur.get(e0, 0) - 0.8:
                    whole += 1
            # AI 常把套名写成「第一套：时间线主干…」，前面已经有「第 1 套」了，剥掉避免重复
            name = re.sub(r"^第[一二三四五六七八九十\d]+套\s*[：:、.．-]?\s*", "", str(st["name"]).strip())
            plans.append({
                "index": len(plans) + 1,  # 每次循环都紧跟一条 append，这样多模型/跳块时编号都连续
                "edl": edl,
                "edl_text": analyze.edl_text(edl),
                "target_sec": round((mn + mx) / 2 * 60, 1),
                "sec": round(sec, 1),
                "eps": [eps[0], eps[-1]] if eps else [],
                "style": f"AI 编排 · {name}",
                # 开场钩子卡文案（出片时黑屏大字用）。AI 没给就留空，出片时用套名兜底
                "hook": st.get("hook") or "",
                # BGM 建议（结构卡片里显示给用户参考，用户照着挑自己电脑里的曲子）
                "bgm": st.get("bgm") or "",
                "model": label,
                "ai_name": name,
                "order": used_mode,
                "order_desc": ORDER_DESC,
                "seq": "→".join(str(s["ep"]) for s in edl[:6]) + ("…" if len(edl) > 6 else ""),
                "overlap": round(ov, 3),
                "vs": vs,
                "anchor_hit": ah,
                "anchor_vs": av,
                "climax": best_run,
                "blocks": len(ordered),
                "jumps": len(gaps),
                "avg_gap": round(sum(gaps) / len(gaps), 1) if gaps else 0.0,
                "avg_block": round(sec / max(1, len(ordered)), 1),
                "whole": whole,
                "on": True,
            })
            if multi:
                # 并发时进度只往前走：各组结果已经收齐，这里按组顺序整理，
                # 不能再用「第 N 组占一截」的算法（会倒退回前面组的位置）。
                core.set_task(pid, "plan", 92 + 7 * (ji + 1) / n_jobs,
                              f"正在整理第 {ji + 1}/{n_jobs} 组的结果")
            else:
                core.set_task(pid, "plan", base + span * (0.92 + 0.08 * i / max(1, len(sets))),
                              f"{prefix}已整理 {done_sets + i}/{total_sets} 套")
            job_plans.append(plans[-1])
            done_sets += 1
            job_summ[ji]["plans"] = job_plans

    if not plans:
        core.set_task(pid, None)
        # 全军覆没时把每个模型的报错原文带出来，别只说「不可用」
        raise RuntimeError("；".join(job_errors) or "AI 给出的结构都不可用，请重试")

    fresh = plans[fresh_start:]
    # 没抛异常却一套都没排出来（AI 给的片段全被 _ensure_geo / _blocks 筛掉）也要能补跑
    for ji, summ in enumerate(job_summ):
        if ji in errored_ji or summ["plans"]:
            continue
        failed_jobs.append({
            "model": summ["model"], "label": _model_label(summ["model"]),
            "count": summ["count"], "min_minutes": summ["mn"], "max_minutes": summ["mx"],
            "reason": "这次返回的片段都用不上，一套也没排出来",
        })

    if not fresh:
        note = f"这次没生成出新的套；已有的 {len(existing)} 套保持不变"
    else:
        # 只统计真正跟前面比过的套：非追加时第一套没有前套可比（vs=None）；
        # 追加时第一套已经跟全部旧套比过，要计入。
        avgs = [pl["overlap"] for pl in fresh if pl.get("vs")]
        avg_ov = sum(avgs) / len(avgs) if avgs else 0.0
        # 带上组号：多组选了同一档位时，只写档位名会出现四行一模一样的「自定义 出 1 套」
        desc = "、".join(
            f"第 {i} 组（{_model_label(j['model'])}）出 {len(j['plans'])} 套"
            f"（均 {sum(x['sec'] for x in j['plans']) / len(j['plans']) / 60:.1f} 分钟）"
            for i, j in enumerate(job_summ, 1) if j["plans"])
        over = [pl for pl in fresh if pl["overlap"] > MAX_OVERLAP]
        note = ("；".join(budget_notes) + " " if budget_notes else "") + (
            f"AI 通读全剧 {total_all / 60:.1f} 分钟：{desc}；套间平均重合 {avg_ov * 100:.0f}%")
        if over:
            detail = "、".join(f"第{pl['index']}套与第{pl['vs']}套 {pl['overlap'] * 100:.0f}%"
                               for pl in over)
            note += (f"；其中 {len(over)} 套超过 {int(MAX_OVERLAP * 100)}% 上限（{detail}）。"
                     "同一部剧剪多套，素材复用是必然的——要更不一样，就减少套数或缩短单套时长")
        else:
            note += f"，全部在 {int(MAX_OVERLAP * 100)}% 以内"
        hit = [pl for pl in fresh if pl["anchor_hit"] >= 2]
        if hit:
            detail = "、".join(f"第{pl['index']}套与第{pl['anchor_vs']}套撞 {pl['anchor_hit']} 条"
                               for pl in hit)
            # 2026-11-12 用户改口径：名场面可以两套都用，只要总重复率没破 80%。
            # 所以这里只报数，不再劝「同一个最狠的镜头别两套都用」。
            note += f"；名场面锚点与前面撞 {detail}（可以共用，只看上面那行总重复率）"
        boom = [pl for pl in fresh if pl["climax"] >= 6]
        if boom:
            detail = "、".join(f"第{pl['index']}套连爆 {pl['climax']} 块" for pl in boom)
            note += f"；{detail}，中间缺缓冲，看着会累"
    if job_errors:
        note += "；有组没生成成功（其它组的套都在）：" + "；".join(job_errors)
    if append:
        note = "补跑：" + note

    p = core.load(pid) or p
    if append and fresh_start:
        # 补跑跑了几分钟，期间用户可能改过勾选 / 每套设置。旧套以最新盘为准，
        # 只把新套接在后面，别拿开头的快照把用户的改动盖回去。
        # 数量对不上说明期间又整批重排过，这时保留本次结果，不硬拼两套编号。
        latest_old = list(p.get("plans") or [])
        if len(latest_old) == fresh_start:
            plans = latest_old + plans[fresh_start:]
    gen = int(p.get("plan_gen") or 0) + 1
    p["plan_gen"] = gen
    p["plans"] = plans
    # 上一次可能是「单集钩子前置」：切回混剪要显式记下模式，否则前端还按钩子
    # 模式的卡片和出片按钮显示（结构已经被下面这份 plans 覆盖了，界面就自相矛盾）。
    # hook_sec / hook_eps 保留着，用户切回钩子模式时上次的选择还在。
    old = p.get("batch") or {}
    new_jobs = [{"model": j["model"], "count": j["count"],
                 "min_minutes": j["mn"], "max_minutes": j["mx"]} for j in job_summ]
    new_mn = min(j["mn"] for j in job_summ)
    new_mx = max(j["mx"] for j in job_summ)
    # 补跑只是往这批结构里添几套：参数面板仍回填最初那次的配置；出片进度也不能清零，
    # done 归零会让已经出片的套被重渲一遍。
    saved_jobs = (old.get("jobs") or new_jobs) if append else new_jobs
    saved_mn = old.get("min_minutes", new_mn) if append else new_mn
    saved_mx = old.get("max_minutes", new_mx) if append else new_mx
    p["batch"] = {
        "count": len(plans),
        "cut_mode": "mix",
        "hook_sec": old.get("hook_sec") or 15,
        "hook_eps": old.get("hook_eps") or [],
        "min_minutes": saved_mn,
        "max_minutes": saved_mx,
        "order": order, "plan_note": note,
        "summary": " / ".join(pl.get("ai_name") or f"第{pl['index']}套" for pl in plans[:8]),
        # count 记用户填的套数，不记实际产出。产出少一套时回填会把输入框改掉。
        "jobs": saved_jobs,
        "done": old.get("done", 0) if append else 0, "gen": gen, "by": "ai",
        # 本次生成的结构化结果：前端提示条与「只补跑没出来的套」按钮的唯一数据来源
        "plan_result": {
            "requested": total_sets,
            "got": len(fresh),
            "total": len(plans),
            "append": bool(append),
            "failed": failed_jobs,
        },
    }
    core.set_task(pid, None)
    # 只写结构相关的字段：第 3 步分镜可能正在并行保存，整份覆盖会把分镜清掉。
    core.save_merge(p, ["plan_gen", "plans", "batch"])


# ── 单集钩子前置（2026-11-06）──────────────────────────────────────────────
# 第 2 步的第二种编排方式：不排 N 套混剪，而是**一集一条片子**——
# 从这一集里挑一段 10/15/20 秒的爆点提到最前面，接这一集的完整正片。
# 钩子那段在正片里会再出现一次，这是故意的（预告感），不做剔除。
#
# 为什么不做成「新建项目时的第三种模式」：转写是整个流程里最贵的一步
# （20 集约 4 分钟），做成新模式就得把同一批剧再转一遍。改成第 2 步的一个
# 编排方式，转写结果、集数、台词全部复用，切换成本为零。
#
# 出片管线一行不改：每集照样是一个 plan，edl 只有两段（钩子段 + 整集段）。
# 勾选出片、单集失败隔离、参数变了自动重渲、断点续跑，全都免费继承。
HOOK_CHOICES = (10, 15, 20)

# 本地兜底时的「情绪词」：不是要它挑得多好，是两害相权——模型漏了这一集时，
# 挑一段带冲突词的，总比挑开头一句寒暄强。
_HOOK_MOOD = ("滚", "闭嘴", "凭什么", "不可能", "够了", "别", "求", "死", "钱",
              "证据", "离婚", "跪", "打", "谁", "敢", "偷", "骗", "还", "走",
              "出去", "签字", "按手印")


def _norm_hook_sec(sec) -> int:
    """钩子长度归到 10/15/20 三档（别的值就近取），全项目统一。"""
    try:
        v = float(sec)
    except (TypeError, ValueError):
        return 15
    return min(HOOK_CHOICES, key=lambda x: abs(x - v))


def _hook_window(hook_sec: float) -> tuple[float, float]:
    """钩子时长的接受区间。档位越大容差越宽——20 秒档卡 16-24，
    模型不必为了凑整数把半句话切掉。"""
    if hook_sec <= 10:
        return 8.0, 12.0
    if hook_sec >= 20:
        return 16.0, 24.0
    return 12.0, 18.0


def _hook_eps_text(p: dict, eps: list[int]) -> str:
    """只列选中集的台词，但**保留全剧连续编号**。

    编号必须跟全剧一致：模型回的 from/to 是全局台词编号，程序靠它反查集内秒数
    （analyze._global_lines）。要是给选中集重新从 1 编号，第 3 集和第十一集的
    「1 号」就撞在一起，挑出来的钩子会落到别的集里去。

    每集只报总长和这一集的全局编号区间，**不给位置暗示**——挑哪一段是模型自己
    按台词判断的事（2026-11-06 去掉了原来的「40% 线」标注：位置不设限）。
    """
    want = set(eps)
    buf: list[str] = []
    n = 0
    for ep in p["episodes"]:
        lines = ep.get("lines") or []
        gid0 = n + 1
        n += len(lines)
        idx = int(ep["index"])
        if idx not in want:
            continue
        dur = float(ep.get("duration") or 0)
        if not lines:
            buf.append(f"\n【第{idx:02d}集】总长 {dur:.0f} 秒【无对白】")
            continue
        buf.append(f"\n【第{idx:02d}集】总长 {dur:.1f} 秒，共 {len(lines)} 句"
                   f"（全局编号 {gid0}-{gid0 + len(lines) - 1}）")
        for k, ln in enumerate(lines):
            buf.append(f"{gid0 + k}. ({float(ln['start']):.1f}-{float(ln['end']):.1f}) "
                       f"{ln.get('text') or ''}")
    return "\n".join(buf)


def _resolve_hook(rec: dict, gid_map: dict[int, dict], ep: int,
                  ep_dur: float, lo: float, hi: float) -> tuple[float, float] | None:
    """把 AI 给的「起句 / 止句」换算成集内秒，逐条校验，不合规返回 None。

    校验三项，任何一项不过就交给本地兜底：
    ① 编号真的存在；② 起止同属这一集（不许跨集拼）；③ 时长落在容差内。
    **不限制位置**：本集任何地方都可以是钩子（2026-11-06 用户明确去掉「必须
    落在 40% 之后」这条）——模型偶尔会把全局编号跟集内编号混着填，所以编号
    和时长必须真验一遍，位置不验。
    """
    try:
        a = gid_map.get(int(rec.get("from")))
        b = gid_map.get(int(rec.get("to")))
    except (TypeError, ValueError):
        return None
    if not a or not b or int(a["ep"]) != ep or int(b["ep"]) != ep:
        return None
    s, e = float(a["start"]), float(b["end"])
    if float(b["start"]) < float(a["start"]):
        s, e = float(b["start"]), float(a["end"])
    if e - s < 1.0:
        return None
    if not (lo <= e - s <= hi):
        return None
    return round(max(0.0, s), 2), round(min(ep_dur, e) if ep_dur else e, 2)


def _local_hook(ep: dict, target: float) -> tuple[float, float]:
    """AI 漏了这一集、或给的区间不合法时的本地兜底。

    原则：**宁可挑得平淡，也不能让整批失败**——19 集都挑好了，因为第 7 集模型
    漏了一条就整批报错，用户只会觉得「这功能不能用」。漏掉的集会在 note 里点名，
    点开卡片看得到具体位置，不满意可以重来。

    位置不设限（2026-11-06 去掉了「只在 40% 之后」的门槛），全集的句界都是候选，
    打分只看这段本身够不够爆：长度贴近档位、带问号感叹号、命中的情绪词多。
    """
    dur = float(ep.get("duration") or 0)
    lines = ep.get("lines") or []
    best: tuple[float, float] | None = None
    best_score = -1e9
    for i, ln in enumerate(lines):
        s = float(ln["start"])
        j = i
        while j + 1 < len(lines) and float(lines[j]["end"]) - s < target:
            j += 1
        e = float(lines[j]["end"])
        d = e - s
        if d < 2.0:
            continue
        text = "".join(str(lines[k].get("text") or "") for k in range(i, j + 1))
        score = -abs(d - target) / max(1.0, target)       # 长度越贴近档位越好
        score += 0.5 if any(ch in text for ch in "？！") else 0.0
        score += 0.45 * sum(1 for w in _HOOK_MOOD if w in text)
        if score > best_score:
            best, best_score = (s, e), score
    if best:
        return round(best[0], 2), round(min(dur, best[1]) if dur else best[1], 2)
    # 连台词都没有（静音集）：没有任何信息可判断，取正中一段，至少别让这一集没产物
    if dur > 2.0:
        s = max(0.0, (dur - target) / 2)
        return round(s, 2), round(min(dur, s + target), 2)
    return 0.0, round(dur, 2)


def hookcut_run(pid: str, eps: list[int] | None = None, hook_sec: float = 15,
                model: str | None = None) -> None:
    """第 2 步（单集钩子前置）：AI 给每集挑一个爆点，一集组一个 plan。

    eps 为空 = 全部集。每集一个 plan（不是一套），edl 只有两段：
    钩子段（本集任一处，位置不设限）+ 整集正片。所以出片勾选、
    单集失败隔离、fx 指纹重渲、断点续跑全部原样可用。

    与多套混剪共用 p["plans"] 和 prompts 之外的一切；提示词是独立的
    prompts/hooks.txt——**绝不能改 plans.txt 来兼容这个模式**，那边有一套
    相反的规定（播放顺序必须严格集序、不许把后面的戏提到开头），混在
    一起会让两条产线互相污染。
    """
    p = core.load(pid)
    if not p:
        return
    eps_all = [int(e["index"]) for e in p["episodes"]]
    if not eps_all:
        raise RuntimeError("还没有转写结果，请先转写")
    want: set[int] = set()
    for x in (eps or []):
        try:
            want.add(int(x))
        except (TypeError, ValueError):
            pass
    sel = sorted(want & set(eps_all)) or eps_all

    hk = _norm_hook_sec(hook_sec)
    lo, hi = _hook_window(hk)
    by_index = {int(e["index"]): e for e in p["episodes"]}
    off = analyze.ep_offsets(p)

    core.set_task(pid, "plan", 5, f"AI 正在给 {len(sel)} 集挑 {hk} 秒钩子")
    prompt = analyze._fill(
        analyze._read_prompt("hooks.txt"),
        name=p.get("name") or "", n_ep=len(p["episodes"]), count=len(sel),
        hook_sec=hk, min_sec=f"{lo:.0f}", max_sec=f"{hi:.0f}",
    ) + "\n\n" + analyze._read_prompt("hooks_schema.txt") \
      + "\n" + _hook_eps_text(p, sel)
    data = analyze._call(prompt, model=(model or "").strip() or None)
    raw = [x for x in (data.get("hooks") or []) if isinstance(x, dict)]
    if not raw:
        raise RuntimeError("AI 没有返回任何钩子，请重试")
    recs: dict[int, dict] = {}
    for x in raw:
        try:
            e = int(x.get("ep"))
        except (TypeError, ValueError):
            continue
        recs.setdefault(e, x)          # 同一集给了两条，只认第一条

    gid_map = {g["gid"]: g for g in analyze._global_lines(p)}
    plans: list[dict] = []
    fallback: list[int] = []
    for i, ep_idx in enumerate(sel, 1):
        ep = by_index[ep_idx]
        dur = float(ep.get("duration") or 0)
        rec = recs.get(ep_idx) or {}
        span = _resolve_hook(rec, gid_map, ep_idx, dur, lo, hi)
        if not span:
            span = _local_hook(ep, hk)
            fallback.append(ep_idx)
        hs, he = span
        # 短集保护：钩子差点吞掉整集，前置段占了九成片长就没意义了，退到中段取一段。
        # （这里的 0.4 只是个「别贴着片头」的短集兜底位置，跟已经删掉的 40% 规则无关）
        if dur > 4.0 and he - hs >= dur - 1.0:
            hs = min(dur * 0.4, max(0.0, dur - hk - 1))
            he = min(dur, hs + max(hk, 2.0))
        hook_txt = analyze._clean_hook(rec.get("hook"))
        base = off.get(ep_idx, 0.0)
        edl = [
            {"ep": ep_idx, "start": round(hs, 2), "end": round(he, 2),
             "gs": round(base + hs, 3), "ge": round(base + he, 3),
             "blk": f"h{ep_idx}", "rating": 9, "core": round((hs + he) / 2, 2),
             "label": f"【钩子】{hook_txt or f'第{ep_idx}集高光'}"},
            {"ep": ep_idx, "start": 0.0, "end": round(dur, 3),
             "gs": round(base, 3), "ge": round(base + dur, 3),
             "blk": f"f{ep_idx}", "rating": 7,
             "label": f"【正片】第{ep_idx}集"},
        ]
        sec = (he - hs) + dur
        plans.append({
            "index": i,                      # 连续编号：set01 就是第 1 集
            "mode": "hook",
            "ep": ep_idx,
            "tag": f"第{ep_idx}集",
            "edl": edl,
            "edl_text": analyze.edl_text(edl),
            "target_sec": round(sec, 1),
            "sec": round(sec, 1),
            "eps": [ep_idx, ep_idx],
            "style": f"单集钩子前置 · {hk}s",
            "hook": hook_txt,
            "bgm": "",
            "model": (model or "").strip(),
            "ai_name": "",
            "hook_span": [round(hs, 2), round(he, 2)],
            "ep_dur": round(dur, 2),
            # 成片文件名固定下来（不带时间戳）：参数变了靠 fx 指纹判重渲，
            # 重渲时直接覆盖同名文件，文件夹里不会越堆越多
            "out_name": f"第{ep_idx:02d}集_钩子版.mp4",
            "order": ORDER_CHRONO,
            "order_desc": "钩子段在前，接本集完整正片",
            "seq": str(ep_idx),
            "on": True,
        })
        core.set_task(pid, "plan", 5 + i / max(1, len(sel)) * 88,
                      f"已挑好 {i}/{len(sel)} 集")

    if not plans:
        core.set_task(pid, None)
        raise RuntimeError("没有可用的集，请检查转写结果")

    note = (f"AI 通读 {len(sel)} 集台词，给每集挑了一个 {hk} 秒钩子；"
            f"成片 = 钩子 + 本集完整正片（钩子那段在正片里会再出现一次），共 {len(plans)} 条")
    if fallback:
        name = "、".join(f"第{i}集" for i in fallback[:6]) + ("等" if len(fallback) > 6 else "")
        note += (f"；其中 {len(fallback)} 集是本地兜底挑的（{name}）——"
                 "AI 漏了这几集，或给的区间不合规（编号对不上 / 跨集 / 时长不在容差内）。"
                 "程序按台词密度补了一段，点开卡片能看具体位置，不满意就重新生成一次")

    p = core.load(pid) or p
    gen = int(p.get("plan_gen") or 0) + 1
    p["plan_gen"] = gen
    p["plans"] = plans
    p["batch"] = {
        "count": len(plans),
        "cut_mode": "hook",
        "hook_sec": hk,
        "hook_eps": sel,
        "plan_note": note,
        "summary": "、".join(f"第{i}集" for i in sel[:8]) + ("…" if len(sel) > 8 else ""),
        "done": 0, "gen": gen, "by": "ai",
    }
    core.set_task(pid, None)
    core.save_merge(p, ["plan_gen", "plans", "batch"])


def _lines_sig(p: dict, edl: list[dict]) -> str:
    """这套成片用到的台词内容指纹（只在开了「加回自己的字幕」时算）。

    第 1 步手改台词走的是 PUT /lines，只覆盖 ep["lines"]，**不会**动
    batch.gen / plan_gen。所以光靠代次判断，用户改完错字再点出片会拿到
    还带着旧字的成片，看起来像「改了没生效」。台词内容本身必须进指纹。
    只取这套 EDL 真会切到的集，别让别集的一句话把整套都判成要重渲。
    """
    eps = {int(s.get("ep") or 0) for s in edl}
    by = {int(e.get("index") or 0): e for e in (p.get("episodes") or [])}
    h = hashlib.sha1()
    for i in sorted(eps):
        for ln in (by.get(i, {}).get("lines") or []):
            h.update(f"{i}|{ln.get('start')}|{ln.get('end')}|{ln.get('text')}\n"
                     .encode("utf-8", "replace"))
    return h.hexdigest()[:10]


def _pic_sig(fx: dict) -> str:
    """画面处理的指纹片段（调色/镜像/开头镜像/拉伸/特效/随机/用哪份视频/新字幕）。

    为什么要单独一段：这些参数不改时长、不改结构，只改画面——不参与指纹的话，
    用户把「暖调」改成「冷调」再点出片，进度一闪而过拿到的还是暖调旧片，
    这种 bug 用户根本不会想到是「没重渲」，只会觉得「调色坏了」。
    出厂默认（四档全空、不镜像、不拉伸、无特效）返回空串 → 指纹与老版本一致，
    老项目不会被误判成需要重渲。
    """
    out: list[str] = []
    grade = (fx.get("grade") or "").strip()
    lut = (fx.get("lut") or "").strip()
    is_rand = grade == "rand" or bool(fx.get("grade_rand"))
    if grade and not is_rand:
        out.append(f"G{grade}{int(round(float(fx.get('grade_int') or 0.65) * 100))}")
    if lut:
        # 只取文件名：整条路径太长，且同一份 LUT 换个目录不该触发重渲
        out.append("L" + os.path.basename(lut))
    if is_rand:
        # 随机档：区间（未必等长）+ 基准强度都进指纹，改区间/改基准就要重渲
        lo = int(float(fx.get("rand_min") or fx.get("rand_sec") or 120))
        hi = int(float(fx.get("rand_max") or fx.get("rand_sec") or 300))
        out.append(f"R{min(lo, hi)}-{max(lo, hi)}"
                   f"I{int(round(float(fx.get('grade_int') or 0.65) * 100))}")
    if fx.get("mirror"):
        out.append("M")
    # 开头镜像（2026-11-12）：开关和方向都得进指纹，否则勾上 / 换方向再出片，
    # 进度一闪而过拿到的还是没翻的旧片。默认关 → 空串，老项目指纹不变。
    if fx.get("first_mirror"):
        d = str(fx.get("first_mirror_dir") or "h").strip().lower()
        out.append("FH" if d != "v" else "FV")
    try:
        st = float(fx.get("stretch") or render.FX_DEFAULTS["stretch"])
    except (TypeError, ValueError):
        st = render.FX_DEFAULTS["stretch"]
    if abs(st - 1.0) > 0.002:
        # 精度到千分位：默认 1.015 用两位整数会四舍五入成 102，跟 1.02 撞车
        out.append(f"S{int(round(st * 1000))}")
    for key, tag in (("glow", "X"), ("grain", "N"), ("vignette", "V")):
        try:
            v = float(fx.get(key) if fx.get(key) is not None else render.FX_DEFAULTS[key])
        except (TypeError, ValueError):
            v = render.FX_DEFAULTS[key]
        if v > 0.004:
            out.append(f"{tag}{int(round(v * 100))}")
    # 用哪份视频（2026-11-11）：从「原始视频」切到「已处理（去字幕+新字幕）」不重渲，
    # 进度一闪而过，拿到的还是旧片。默认原始视频 → 空串，
    # 老项目的指纹不变，不会被误判成要重渲。
    ver = str(fx.get("src_ver") or "").strip()
    if ver == "nosub":
        out.append("SUBV")
    elif ver == "live":
        # 现场做：抹除参数、新字幕风格、台词内容都影响画面，全在 ver_sig 里算，
        # 这里只要一个「不是原片」的标记，别和预生成的 SUBV 混在一起
        out.append("LIVEV")
    # 字幕样式（2026-11-16 从第 2 步搬到出片）：只在真要烧字的两档里算。
    # 不加这一位的后果是「改了动效点出片，进度一闪而过，拿回的还是旧动感」。
    if ver in ("nosub", "live"):
        try:
            mo = int(round(float(fx.get("newsub_motion") or 0.5) * 100))
        except (TypeError, ValueError):
            mo = 50
        out.append(f"ST{str(fx.get('newsub_style') or 'rise')[0]}{mo}")
    return "".join(out)


def render_plans(pid: str, save_dir: str | None = None,
                 fx: dict | None = None, sets_fx: dict | None = None) -> None:
    """只渲染勾选的套。每套完成立即落盘，中断重启自动续跑未完成的。

    save_dir：这批成片存哪个文件夹（用户自己指定）。不传就沿用上次记下的，
    再没有就用全局「成片保存位置」。**不替用户起子文件夹名**——指哪打哪。

    fx：出片增强开关（各项独立）{"title": 每块顶部「看点：XXX」,
    "zoom": 画面放大 103% 裁边去重, "hook_card": 片头 1.8 秒黑屏钩子卡,
    "hook_sec": 卡片时长, "buffer_speed": 缓冲段加速 1.15×,
    "bgm": 混入背景音乐, "bgm_path": 本地音乐路径（手选，或自动配乐由曲库解析所得）,
    "bgm_vol": BGM 音量（0.05-0.6，默认 0.22，台词响时程序自动压低音乐）}，
    前四项缺省都开，bgm 缺省关（不开就不碰音乐文件）。
    段间音频淡入淡出不设开关——60/120 毫秒听不出来，只把切口的「咔」声磨掉。

    钩子卡文案：每套 pl["hook"]（AI 第 2 步随结构给的，前端可改）；
    没有就用套名兜底。**任何出片参数或文案变了，对应套会自动重渲**
    （sets 记录里存了参数指纹 fx，见循环内的 fx_sig）。

    sets_fx：每套单独的画面设置（{"3": {grade:"warm", grade_int:0.7, ...}}），
    没传的套就用 fx 里的全局值。两者合并后才是这一套真正用的参数——
    「默认都一样、想改单套就改单套」就是靠这个覆盖实现的。
    取值优先级：本次请求的 sets_fx > 项目里存过的 pl["fx"] > 全局 fx。

    铁律：单套失败只记录、不中断——批量任务里任何一套出问题（磁盘、文件、
    临时目录清不动……）都不许把剩下十几套一起带走。失败的套记进
    batch.failures，前端展示；重新点「出片」会自动只补做没成功的。"""
    p = core.load(pid)
    if not p:
        return
    plans = [pl for pl in (p.get("plans") or []) if pl.get("on", True)]
    if not plans:
        raise RuntimeError("没有勾选任何结构，请先勾选要出片的套")

    total = len(plans)
    # 用户填的是根目录（如 F:/钩子工坊_成片），片子归到 <根>/<项目名>/ 下面。
    # 记的是根目录、不是实际目录：存实际目录的话，下次回填输入框会再套一层项目名。
    root = (save_dir or (p.get("batch") or {}).get("save_dir") or "").strip()
    root = os.path.abspath(os.path.expanduser(root)) if root else core.output_dir()
    if (p.get("batch") or {}).get("save_dir") != root:
        p.setdefault("batch", {})["save_dir"] = root
        core.save_merge(p, ["batch"])
    target = os.path.join(root, core.safe_name(p.get("name") or ""))
    os.makedirs(target, exist_ok=True)
    # 整批共用的临时目录：只在**整个任务**退出时清一次（跑完 / 失败 / 用户中止）。
    # 以前只写在函数最后那一行，中止信号一穿透就跳过去，留下一堆切段文件占磁盘。
    core.add_cleanup(lambda: core.rmtree(os.path.join(target, "_render_tmp", pid)))

    fx = fx if isinstance(fx, dict) else {}
    # 进程重启后的自动续跑不传 fx：拿项目里存的默认值兜底。否则用户设的
    # 指纹档位/调色会退回出厂默认，而 fx_sig 又记着这一版——同一套会出出
    # 两种画面，用户还以为是随机指纹"没生效"。
    if not fx:
        fx = dict((p.get("batch") or {}).get("fx_default") or {})
    try:
        base_hook_sec = float(fx.get("hook_sec") or render.HOOK_SEC)
    except (TypeError, ValueError):
        base_hook_sec = render.HOOK_SEC
    base_hook_sec = min(4.0, max(1.0, base_hook_sec))

    def _flags(px: dict) -> dict:
        """这一套最终生效的出片开关（全局默认已被单套覆盖过的合并结果）。

        2026-11-05 改成按套算：以前这些开关在批次开头读一次，单套改了也不生效。
        现在「每套都能单独设」——第 3 套不要钩子卡、第 4 套换一首 BGM，都是
        在 setfx 里带 title/hook_card/bgm… 覆盖掉全局值。

        缺省语义（2026-11-05 晚按用户要求改）：看点字幕、开场钩子卡**默认关**，
        放大、缓冲加速默认开，BGM 默认关。缺省值必须和前端面板、RenderFx 三处一致，
        否则「面板显示关、出片却烧了字幕」这种错最难查。
        """
        try:
            hs = float(px.get("hook_sec") or base_hook_sec)
        except (TypeError, ValueError):
            hs = base_hook_sec
        has_bgm = bool(px.get("bgm"))
        bp = (px.get("bgm_path") or "").strip() if has_bgm else ""
        try:
            bv = min(0.6, max(0.05, float(px.get("bgm_vol") or 0.22)))
        except (TypeError, ValueError):
            bv = 0.22
        return {
            "title": bool(px.get("title", False)),
            "zoom": px.get("zoom", True) is not False,
            "hook": bool(px.get("hook_card", False)),
            "speed": px.get("buffer_speed", True) is not False,
            "hook_sec": min(4.0, max(1.0, hs)),
            "bgm_path": bp,
            "bgm_vol": bv if bp else 0.22,
            # 指纹强度（2026-11-12）：每套按这个档位抽自己的画面参数。
            # 认不出的值一律落回「标准」，别让前端传个怪字符串把整批带崩。
            "fp_level": (str(px.get("fp_level") or "").strip().lower()
                         if str(px.get("fp_level") or "").strip().lower() in render.FP_LEVELS
                         else render.FP_DEFAULT),
        }

    def _speed_of(seg: dict) -> float:
        # 缓冲块（评分 5-6）加速 1.15×：反应、沉默、过场收紧，爆点块保持原速
        try:
            return render.BUFFER_SPEED if float(seg.get("rating") or 0) <= 6 else 1.0
        except (TypeError, ValueError):
            return 1.0

    failures: list[dict] = []
    ok = 0
    # 只认「当前结构版本」的成片记录。旧版本的记录一律当没渲过——结构都换了，
    # 拿旧片子当新产物比重新渲一遍糟糕得多。
    gen = int((p.get("batch") or {}).get("gen") or p.get("plan_gen") or 0)

    # 指纹随机批次号（2026-11-12 用户要求：同一套隔几天再点一次出片，
    # 出来的必须是另一版画面，不能还是上次那一份）。
    # 每次点「出片」换一个 run_id → 每套每段重新抽一组几何参数；
    # 但上次没跑完（还有勾选的套没成品）时沿用旧 run_id：中断重开要是换新号，
    # 已经出好的套会被判成「参数变了」全部重渲，续跑省下的时间全吐回去。
    _bmeta = p.setdefault("batch", {})
    _done_idx = {s.get("index") for s in (p.get("sets") or [])
                 if s.get("output") and int(s.get("gen") or 0) == gen}
    _prev_run = str(_bmeta.get("run_id") or "")
    _need_idx = {pl["index"] for pl in plans}
    if _prev_run and not _need_idx <= _done_idx:
        run_id = _prev_run
    else:
        # 尾缀 6 位随机：# 时间戳只到秒，一秒内点两次（或第一套立刻失败后再点）
        # 会拿到同一个号，等于「再点一次还是老指纹」，用户以为没生效。
        run_id = time.strftime("%m%d%H%M%S") + uuid.uuid4().hex[:6]
    if _bmeta.get("run_id") != run_id:
        _bmeta["run_id"] = run_id
        core.save_merge(p, ["batch"])

    # 「现场做」（src_ver=live）要在切段前把逐句字幕区域量好：第 2 步做过就复用
    # 它的缓存，没做过就在这儿补量一次（一集十几秒，所以放在后台任务里做）。
    # 「要烧台词字幕」的套（现场做 live，或干净副本 nosub）都得先有字幕区域：
    # 第 2 步做过就复用缓存，没做过就在这儿补量一次（一集十几秒，放后台做）。
    def _needs_spans() -> bool:
        burned = render._copy_burned(p)
        for pl in plans:
            ov = (sets_fx or {}).get(str(pl.get("index")))
            if ov is None:
                ov = (sets_fx or {}).get(pl.get("index")) or {}
            ov = dict(ov or {})
            if ov.get("src_ver") is None:
                ov["src_ver"] = (fx or {}).get("src_ver")
            ver = str(ov.get("src_ver") or "").strip()
            if ver == "live" or (ver == "nosub" and not burned):
                return True
        return False

    if _needs_spans():
        nosub.ensure_spans(pid, on_msg=lambda m: core.set_task(pid, "batch", 0, m))

    for k, pl in enumerate(plans):
        p = core.load(pid) or p
        sets = p.setdefault("sets", [])
        # 这一套实际用的参数：全局默认 + 单套覆盖（请求里带的优先，其次项目里存的）
        ov = {}
        if isinstance(sets_fx, dict):
            ov = sets_fx.get(str(pl["index"])) or sets_fx.get(pl["index"]) or {}
        if not ov:
            ov = pl.get("fx") or {}
        setfx = dict(fx)
        # 注意变量名：外层 k 是套序号，这里不能用 k 覆盖（否则下面进度回调拿到字符串）
        for fk, fv in (ov or {}).items():
            if fv is not None:
                setfx[fk] = fv
        # 开关按套算（2026-11-05）：单套可以关掉标题/放大/钩子卡/变速，或换自己的 BGM
        fl = _flags(setfx)
        # 开场钩子卡文案：AI 给的优先，没有就用套名兜底（第 2 步之前的项目没这个字段）
        this_hook = ""
        if fl["hook"]:
            this_hook = (pl.get("hook") or "").strip() or (pl.get("ai_name") or "").strip()
        # 出片参数指纹：片头/标题/画面/素材版本/变速/BGM 任一开关变了，旧成片就不算这次的结果，
        # 必须重渲——否则用户打开钩子卡点「出片」，进度一闪而过，拿到的还是
        # 上一版没有片头的片子。前四位依次是 标题/变速/钩子/缩放。
        # 老记录没有 fx 字段，按「有标题、有变速、无钩子、无缩放」(1101|) 算。
        # BGM 部分追加在第二个 | 后（音量+文件路径）；没开 BGM 就不加这一段，
        # 老的无 BGM 记录指纹不变，不会被误判成要重渲。
        bgm_sig = (f"B{round(fl['bgm_vol'] * 100):03d}{fl['bgm_path']}"
                   if fl["bgm_path"] else "")
        pic_sig = _pic_sig(setfx)
        # 这一套用了去字幕版：副本本身的参数指纹也要算进来。用户把副本从
        # 「裁掉」重做「柔和抹掉」后点出片，必须重渲，不能拿旧版素材出的片充数。
        _ver = str(setfx.get("src_ver") or "").strip()
        if _ver == "nosub":
            # 副本指纹 + 烧字指纹（2026-11-16）：副本只负责抹，台词和字幕样式是
            # 出片时才烧上去的。只算副本指纹的话，改了台词/样式点出片会被判成
            # 「没变」直接跳过，用户拿回的还是旧字的片子。
            ns = p.get("nosub") or {}
            ver_sig = (str(ns.get("sig") or "") + "|"
                       + str(ns.get("burn_sig") or ""))
        elif _ver == "live":
            # 现场做：抹除参数 + 新字幕风格 + 每集台词内容都由 sig_of 算，
            # 改了任一项都必须重渲（台词改了字幕就得跟着变）
            ver_sig = "live" + nosub.sig_of((p.get("nosub") or {}).get("cfg") or {},
                                            p.get("episodes") or [])
        else:
            ver_sig = ""
        fx_sig = (f"{int(fl['title'])}{int(fl['speed'])}"
                  f"{int(bool(this_hook))}{int(fl['zoom'])}|{this_hook[:40]}"
                  # 指纹批次号 + 强度档：换了批次（又重新点了出片）或改了档位，
                  # 这套的旧成片就不再是这次要的结果，必须重渲
                  + f"|R{fl['fp_level']}{run_id}"
                  + (f"|{bgm_sig}" if bgm_sig else "")
                  + (f"|P{pic_sig}" if pic_sig else "")
                  + (f"|V{ver_sig}" if ver_sig else ""))
        done = next((s for s in sets if s.get("index") == pl["index"] and s.get("output")
                     and int(s.get("gen") or 0) == gen
                     and (s.get("fx") or "1101|") == fx_sig), None)
        if done:
            # 续跑跳过的前提是成片文件真的还在**且就在这次的目标目录里**。
            # 换过保存位置（含 2026-11-02 起「根目录/项目名」这层子目录）后，
            # 旧记录指向的是别处的文件，继续当「已完成」会让人以为这片子是刚出的。
            old = core.abs_output(done["output"])
            same_dir = os.path.normcase(os.path.dirname(os.path.normpath(old))) == \
                os.path.normcase(os.path.normpath(target))
            if os.path.isfile(old) and same_dir:
                continue
            sets.remove(done)
        edl = pl["edl"]
        # 单集钩子前置：文件名固定成「第03集_钩子版.mp4」（不带时间戳）——
        # 每集本来就是一条独立成片，带时间戳的话重渲一次就在文件夹里多一条，
        # 用户分不清哪条是新的。重渲由 fx 指纹判定，直接覆盖同名文件。
        name = (pl.get("out_name")
                or f"{p['name']}_混剪set{pl['index']:02d}_{time.strftime('%m%d%H%M%S')}.mp4")
        # 进度文案的单位：混剪是「套」，钩子前置是「条」（一条 = 一集）
        unit = pl.get("tag") or f"set{pl['index']:02d}"
        kind = "条" if pl.get("mode") == "hook" else "套"
        try:
            info = render.render_edl(
                p, edl, name, cleanup=False, out_dir=target,
                # 顶部标题：每块烧一行它的 label（去【分类】前缀），见 render._title_text
                title_of=render._title_text if fl["title"] else None,
                # 缓冲加速只对多套混剪有意义（它按 rating≤6 判缓冲段）。单集钩子
                # 前置的两段是「钩子 + 整集」，加速会把整集正片变快、钩子却是爆点，
                # 所以这个模式强制原速（前端对应选项也置灰）。
                speed_of=_speed_of if (fl["speed"] and pl.get("mode") != "hook") else None,
                hook=this_hook or None, hook_sec=fl["hook_sec"],
                zoom=render.ZOOM if fl["zoom"] else 1.0,
                bgm_path=fl["bgm_path"] or None, bgm_vol=fl["bgm_vol"],
                fx=setfx, grade_seed=f"{pid}:{pl['index']}:{gen}",
                # 指纹随机：一套一个种子（含本次批次号），整批里每套各不相同；
                # fp_pos 按套号用黄金分割铺开，保证十几套的放大倍率不撞在同一档
                fp_level=fl["fp_level"],
                fp_seed=f"{pid}:{pl['index']}:{gen}:{run_id}",
                fp_pos=(int(pl["index"]) * 0.6180339887) % 1.0,
                progress=lambda pct, msg, k=k, total=total: core.set_task(
                    pid, "batch", (k + pct / 100.0) / total * 100,
                    f"第 {k + 1}/{total} {kind}（{unit}）：{msg}"))
        except Exception as exc:  # noqa: BLE001 —— 单套失败必须与整批隔离
            msg = (str(exc) or type(exc).__name__).strip()[:200]
            failures.append({"index": pl["index"], "error": msg})
            p = core.load(pid) or p
            p.setdefault("batch", {})["failures"] = failures
            core.set_task(pid, "batch", (k + 1) / total * 100,
                          f"第 {k + 1}/{total} {kind}（{unit}）失败：{msg[:80]}，跳过继续")
            core.save_merge(p, ["batch"])
            time.sleep(1)
            continue
        if pl.get("mode") == "hook":
            hs, he = (list(pl.get("hook_span") or [0, 0]) + [0, 0])[:2]
            notes = "；".join(filter(None, [
                f"第 {pl.get('ep')} 集：钩子 {float(he) - float(hs):.1f}s"
                f" + 正片 {float(pl.get('ep_dur') or 0):.1f}s",
                f"成片 {info['duration']:.1f}s",
                pl.get("style") or "",
            ]))
        else:
            notes = "；".join(filter(None, [
                f"主题：{pl['ai_name']}" if pl.get("ai_name") else "",
                f"{len(edl)} 段 / {info['duration'] / 60:.1f} 分钟（目标 {pl.get('target_sec', 0) / 60:.1f}）",
                pl.get("style") or "",
            ]))
        sets = [s for s in sets if s.get("index") != pl["index"] or int(s.get("gen") or 0) != gen]
        sets.append({
            "index": pl["index"], "gen": gen, "fx": fx_sig,
            "tag": pl.get("tag") or "",
            "edl": edl, "edl_text": pl.get("edl_text", ""),
            "notes": notes, "output": os.path.join(target, info["name"]),
            "output_duration": info["duration"], "output_size": info["size"],
        })
        sets.sort(key=lambda s: s.get("index") or 0)
        p["sets"] = sets
        ok += 1
        p.setdefault("batch", {})["done"] = ok
        core.set_task(pid, "batch", (k + 1) / total * 100, f"已完成 {ok}/{total} {kind}")
        # p 是这一轮开头 load 的快照，从这里整份 save 会把并行的第 3 步分镜清掉。
        core.save_merge(p, ["sets", "batch"])

    # 整批只清一次临时目录（复用覆盖写，见 render_edl 的 cleanup 说明）
    p = core.load(pid) or p
    if p.get("batch") is not None:
        p["batch"]["failures"] = failures
    core.save_merge(p, ["batch"])
    if failures and not ok:
        what = "条" if (plans[0].get("mode") == "hook") else "套"
        raise RuntimeError(f"全部 {total} {what}都失败了，第一条的原因：{failures[0]['error']}")
    core.set_task(pid, None)

    # 整批只清一次临时目录（复用覆盖写，见 render_edl 的 cleanup 说明）。
    # 2026-11-04 起放在**最后**：清理属于收尾，不能挡在落库和收任务前面——
    # 之前顺序相反，删除被本机守卫拦下的那一刻进程直接没了，成片明明已经写完，
    # 任务却停在 100% 不收尾、failures 也没落盘。清理失败就当没清。
    try:
        core.rmtree(os.path.join(target, "_render_tmp", pid))
    except Exception:
        pass
