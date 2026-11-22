"""出片预演（2026-11-23）：点「开始」之前，把这一单要花多少先算给用户看。

借鉴竞品拆解里的「点击前任务摘要」思路（_竞品分析 2026-11-22 报告）：
autopipe / 直出在启动前用纯计算接口报出规模——将生成几套、选几集、
预计调几次模型、排结构和出片各约多少分钟、素材够不够。

铁律：**只读项目数据，不写盘、不调 AI、不动任务槽**。所有数字都有出处，
改口径先改对应注释。
"""
from __future__ import annotations

import math

from . import core

# 出片耗时估算比：交付文档口径「10 分钟片约 2 分钟」≈ 0.2，留余量取 0.25。
# 直出是整片重编码，接近 1:1。
RENDER_EST_RATIO = 0.25
DIRECT_EST_RATIO = 1.0

# 首次跑才有的开销不硬编秒数（机器差异太大），只给文字提示。
FIRST_RUN_NOTES = ["首次转写较慢（含模型预热）", "抹字幕逐集十几秒到一倍时长"]


def _jobs_of(cfg: dict) -> list[dict]:
    """规范化 autopipe 的 jobs——口径与 batch.ai_plan_multi 的规范化一致
    （batch.py:417-424），别在这里发明第二套。"""
    jobs: list[dict] = []
    for j in (cfg.get("jobs") or []):
        if not isinstance(j, dict):
            continue
        try:
            count = max(1, min(20, int(j.get("count") or 1)))
        except (TypeError, ValueError):
            count = 1
        try:
            mn = max(0.5, float(j.get("min_minutes") or 3.0))
            mx = max(0.5, float(j.get("max_minutes") or 7.0))
        except (TypeError, ValueError):
            mn, mx = 3.0, 7.0
        if mn > mx:
            mn, mx = mx, mn
        jobs.append({"model": (j.get("model") or "").strip(),
                     "count": count, "mn": mn, "mx": mx})
    if not jobs:   # 与前端默认面板同款（3 个模型各 5 套只取一个兜底位）
        jobs = [{"model": "", "count": 5, "mn": 3.0, "mx": 7.0}]
    return jobs


def _plan_est_min(n_jobs: int) -> int:
    """排结构耗时：照抄 batch.ai_plan_multi 的进度公式（batch.py:466-478）。
    每组一批、实测每组约 3 分钟（batch.PLAN_EST_MIN，本地 4 次任务均值 194 秒）。"""
    from . import batch
    conc = min(core.max_concurrent(), max(1, n_jobs))
    return math.ceil(max(1, n_jobs) / conc) * batch.PLAN_EST_MIN


def _sel_eps(p: dict, eps_want) -> list[dict]:
    """选集口径同 batch.py:388-394：cfg.eps 空 = 全集。"""
    eps = p.get("episodes") or []
    if not eps_want:
        return eps
    try:
        sel_set = {int(e) for e in eps_want}
    except (TypeError, ValueError):
        return eps
    return [e for e in eps if int(e.get("index") or 0) in sel_set]


def _budget_warn(jobs: list[dict], source_sec: float) -> list[str]:
    """素材够不够的预警——公式照抄 batch.py:431-438，只预警不阻止。"""
    out: list[str] = []
    if source_sec <= 0:
        return out
    for j in jobs:
        budget = j["count"] * (j["mn"] + j["mx"]) / 2 * 60
        if budget > source_sec * 0.8:
            out.append(
                f"{j['model'] or '默认模型'}：{j['count']} 套 × 约 "
                f"{(j['mn'] + j['mx']) / 2:.1f} 分钟，素材只有 "
                f"{source_sec / 60:.1f} 分钟——套间大量重复是必然的")
    return out


def _proof_done_whole(p: dict) -> bool:
    """整剧台词校对是否已做过且还有效（指纹没变，再跑一次也不花钱）。"""
    cur = p.get("proof") or {}
    if not cur or (cur.get("mode") or "whole") != "whole":
        return False
    try:
        from . import proof
        return p.get("proof_sig") == proof.all_sig(p)
    except Exception:  # noqa: BLE001 — 预演绝不能因为校验失败崩掉
        return False


def preview_autopipe(p: dict, cfg: dict | None = None) -> dict:
    """一键全自动的预演。cfg 不传用项目里存的（重跑/续跑口径）。"""
    from . import autopipe

    ap = p.get("autopipe") or {}
    cfg = cfg or ap.get("cfg") or {}
    jobs = _jobs_of(cfg)
    n_jobs = len(jobs)
    sets = sum(j["count"] for j in jobs)

    eps = _sel_eps(p, cfg.get("eps") or [])
    eps_no_lines = sum(1 for e in eps if not e.get("lines"))
    source_sec = sum(float(e.get("duration") or 0) for e in eps)

    # 断点沿用判定复用 autopipe.resume_entry：结构参数没变且套都还在
    # → 第③步整批跳过，不重复花钱（autopipe.py:73-99）。
    reuse = autopipe.resume_entry(ap, cfg, p.get("plans") or [])
    reuse_plan = reuse[0] == "plan" and bool(reuse[1])

    plan_calls = 0 if reuse_plan else n_jobs
    proof_calls = 0 if _proof_done_whole(p) else 1
    ai_calls = plan_calls + proof_calls

    # 出片耗时：结构沿用时用已生成套的真实目标秒数；否则按本次请求的目标估。
    if reuse_plan:
        idx_set = set(reuse[1])
        target_sec = sum(
            float(pl.get("sec") or (pl.get("target_sec") or 0) or 0)
            for pl in (p.get("plans") or []) if int(pl.get("index") or 0) in idx_set)
        if target_sec <= 0:
            target_sec = sets * (sum((j["mn"] + j["mx"]) / 2 for j in jobs) / n_jobs) * 60
    else:
        target_sec = sum(j["count"] * (j["mn"] + j["mx"]) / 2 * 60 for j in jobs)
    est_render_min = target_sec * RENDER_EST_RATIO / 60

    need_transcribe = any(not e.get("lines") for e in (p.get("episodes") or []))
    need_nosub = not autopipe._nosub_ready(p, len(p.get("episodes") or []))
    notes = list(FIRST_RUN_NOTES) if (need_transcribe or need_nosub) else []

    return {
        "kind": "autopipe",
        "sets": sets,
        "reuse_sets": len(reuse[1]) if reuse_plan else 0,
        "resume": reuse_plan,
        "jobs": [{"model": j["model"], "count": j["count"],
                  "minutes": f"{j['mn']:g}-{j['mx']:g}"} for j in jobs],
        "n_jobs": n_jobs,
        "eps": len(eps),
        "eps_no_lines": eps_no_lines,
        "source_minutes": round(source_sec / 60, 1),
        "ai_calls": ai_calls,
        "ai_breakdown": {"plan": plan_calls, "proof": proof_calls},
        "est_plan_min": 0 if reuse_plan else _plan_est_min(n_jobs),
        "est_render_min": round(est_render_min, 1),
        "need_transcribe": need_transcribe,
        "need_nosub": need_nosub,
        "first_run_notes": notes,
        "budget_warn": _budget_warn(jobs, source_sec),
    }


def preview_direct(p: dict) -> dict:
    """直出（选视频直接出片）的预演。不调 AI 排结构；AI 只花在台词校对上。"""
    from . import direct, proof

    eps = p.get("episodes") or []
    copies = direct.copies_of(p)
    source_sec = sum(float(e.get("duration") or 0) for e in eps)
    transcribed = sum(1 for e in eps if e.get("lines"))

    # 校对按视频各一次，已校对（清单指纹与当前台词一致）的不重复花钱。
    cur = p.get("proof") or {}
    by_ep = (cur.get("by_ep") or {}) if cur.get("mode") == "per_ep" else {}
    proofed = 0
    for e in eps:
        item = by_ep.get(str(int(e.get("index") or 0))) or {}
        if item and item.get("sig") == proof._ep_sig(e):
            proofed += 1
    ai_calls = max(0, len(eps) - proofed)

    return {
        "kind": "direct",
        "videos": len(eps),
        "copies": copies,
        "sets": len(eps) * copies,
        "transcribed": transcribed,
        "need_transcribe": transcribed < len(eps),
        "source_minutes": round(source_sec / 60, 1),
        "ai_calls": ai_calls,
        "est_render_min": round(source_sec * copies * DIRECT_EST_RATIO / 60, 1),
        "first_run_notes": list(FIRST_RUN_NOTES) if transcribed < len(eps) else [],
    }
