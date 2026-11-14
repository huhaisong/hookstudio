"""批量模式：AI 通读全剧台词，一次排出 N 套剪辑结构，用户勾选后再逐套渲染。

流程（对应界面 3 步）：
1. 转写 —— 建项目后自动跑
2. 剪辑结构 —— AI 一次看完整部剧，直接排出 N 套 EDL（调 1 次 API），用户勾选要哪几套
3. 出片 —— 只渲染勾选的套

（2026-11-02 移除了「精彩片段池」这一步：池子只是本机组装的素材来源，
AI 编排直接读台词，不再需要先挖池、也不再有本机组装这条路。）
"""
from __future__ import annotations

import os
import re
import time

from . import analyze, core, render

# 套间重合度上限（70%）：只用于提示和前端标色，不拦截落盘。
# 同一部剧剪 N 套，素材复用是必然的——6 套 × 4.5 分钟要 27 分钟素材，全剧只有 21 分钟。
MAX_OVERLAP = 0.7


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


# 播放顺序策略：允许改变时序（后面的片段可以到前面来），但每种都是观众能看懂的叙事结构，
# 而不是随便洗牌。轮换着用可以让 N 套片子的节奏不一样。
ORDER_MODES: dict[str, dict] = {
    "chrono": {
        "name": "纯集序推进",
        "desc": "严格按集号从小到大播，最稳、最容易看懂",
    },
    "hook_first": {
        "name": "最炸开场 → 集序正文",
        "desc": "开场放全片最抓人的一段（可来自任意集），之后回到集序一路推进",
    },
    "climax_last": {
        "name": "集序推进 → 最炸收尾",
        "desc": "全程按集序走，把最炸的一段压到最后当高潮结尾",
    },
    "sandwich": {
        "name": "最炸开场 → 集序正文 → 次炸收尾",
        "desc": "首尾各放一段高能，中间按集序讲清楚，两头抓人",
    },
    "flashback": {
        "name": "后段冲突冷开场 → 回到起因按序推进",
        "desc": "先给一个结果型冲突（取自剧情中后段）吊住人，再回到前面的集讲怎么走到这一步",
    },
    "two_act": {
        "name": "两幕：前段交代关系 → 后段集中爆发",
        "desc": "前一半集数按序交代人物关系，后一半集数按序爆发，每幕各自以最炸的一段开场",
    },
}
DEFAULT_ORDER = "auto"

def _best_of(segs: list[dict], exclude: tuple = ()) -> dict | None:
    cand = [s for s in segs if _blk_key(s) not in exclude]
    return max(cand, key=lambda s: (s.get("rating") or 5)) if cand else None


def _apply_order(chosen: list[dict], mode: str) -> tuple[list[dict], str]:
    """把选中的**块**按某种「有逻辑的顺序」重新排列。返回 (块列表, 实际生效的策略 key)。

    入参是块（含 gs/ge 全局坐标、rating），不是碎片——整块搬动才不会把一段连贯剧情劈成两半。
    """
    blocks = sorted(chosen, key=lambda b: b["gs"])
    if mode not in ORDER_MODES or len(blocks) <= 2:
        return blocks, "chrono"
    if mode == "chrono":
        return blocks, mode

    lo = blocks[0]["gs"]
    hi = blocks[-1]["ge"]
    span = max(1.0, hi - lo)

    if mode == "hook_first":
        b = _best_of(blocks)
        return [b] + [s for s in blocks if s is not b], mode

    if mode == "climax_last":
        b = _best_of(blocks)
        return [s for s in blocks if s is not b] + [b], mode

    if mode == "sandwich":
        b1 = _best_of(blocks)
        b2 = _best_of(blocks, (_blk_key(b1),))
        if b2 is None:
            return [b1] + [s for s in blocks if s is not b1], "hook_first"
        mid = [s for s in blocks if s is not b1 and s is not b2]
        return [b1] + mid + [b2], mode

    if mode == "flashback":
        # 冷开场必须取自剧情后三分之一（最像"结果"的位置），否则「先给结果再回溯」就不成立
        tail = [s for s in blocks if s["gs"] >= lo + span * 0.66]
        b = _best_of(tail) or _best_of(blocks)
        return [b] + [s for s in blocks if s is not b], mode

    if mode == "two_act":
        half = lo + span / 2
        out: list[dict] = []
        for act in ([s for s in blocks if s["ge"] <= half], [s for s in blocks if s["ge"] > half]):
            if not act:
                continue
            b = _best_of(act)
            out.append(b)
            out.extend([s for s in act if s is not b])
        return out, mode

    return blocks, "chrono"


def _pick_modes(order: str) -> list[str]:
    """按用户选的方式决定每套用哪些顺序策略（轮换）。"""
    if order == "chrono":
        return ["chrono"]
    if order == "varied":
        return [k for k in ORDER_MODES if k != "chrono"] or ["chrono"]
    return list(ORDER_MODES)  # auto：全部策略轮换


def _inter_sec(pieces: list[dict], other: list[dict]) -> float:
    """两组片在时间轴上重合的秒数（同一集内按区间求交）。"""
    tot = 0.0
    for s in pieces:
        for t in other:
            if s["ep"] != t["ep"]:
                continue
            tot += max(0.0, min(s["end"], t["end"]) - max(s["start"], t["start"]))
    return tot


def _overlap(a: list[dict], b: list[dict]) -> float:
    """两套结构的时间重合度（重合秒数 / 较短一套的总秒数）。"""
    da = sum(s["end"] - s["start"] for s in a)
    db = sum(s["end"] - s["start"] for s in b)
    if da <= 0 or db <= 0:
        return 0.0
    return _inter_sec(a, b) / min(da, db)


def ai_plan_run(pid: str, count: int, min_minutes: float = 4.0,
                max_minutes: float = 5.0, order: str = DEFAULT_ORDER) -> None:
    """第 2 步：让 AI 一次看完整部剧，直接排出 N 套结构。

    AI 看得见因果链，六套能分到不同主线（这套讲讨债、那套讲反击）；
    代价是走 1 次 API，且时长/重合度只能事后校验——所以时长由 analyze.build_plans
    里的 _fit_target 收紧，重合度在这里如实算出来上报。

    **重合度不拦截**：同一部剧剪 N 套，素材必然复用（6 套 × 4.5 分钟 = 27 分钟，
    而全剧只有 21 分钟）。强行卡 70% 只会让这次 API 白花钱、还一套都拿不到。
    所以这里如实算出来，超线就在 note 里点名「第 3 套和第 5 套重合 78%」，
    由用户决定重排还是接受。
    """
    p = core.load(pid)
    if not p:
        return
    if not (p.get("episodes") or []):
        raise RuntimeError("还没有转写结果，请先转写")
    if min_minutes > max_minutes:
        min_minutes, max_minutes = max_minutes, min_minutes

    sets = analyze.build_plans(p, count, min_minutes, max_minutes)

    modes = _pick_modes(order)
    plans: list[dict] = []
    accepted: list[list[dict]] = []
    for i, st in enumerate(sets, 1):
        segs = _ensure_geo(p, st["segments"])
        blocks = _blocks(segs)
        if not blocks:
            continue
        mode = modes[(i - 1) % len(modes)]
        ordered, used_mode = _apply_order(blocks, mode)
        edl = [ps for b in ordered for ps in sorted(b["pieces"], key=lambda s: s["gs"])]
        sec = sum(b["dur"] for b in ordered)

        # 与前面每一套的重合度都算，取最高的一对，用于「点名」
        vs, ov = None, 0.0
        for j, a in enumerate(accepted, 1):
            o = _overlap(edl, a)
            if o > ov:
                vs, ov = j, o
        accepted.append(edl)

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
            "index": i,
            "edl": edl,
            "edl_text": analyze.edl_text(edl),
            "target_sec": round((min_minutes + max_minutes) / 2 * 60, 1),
            "sec": round(sec, 1),
            "eps": [eps[0], eps[-1]] if eps else [],
            "style": f"AI 编排 · {name}",
            "ai_name": name,
            "order": used_mode,
            "order_desc": ORDER_MODES[used_mode]["desc"],
            "seq": "→".join(str(s["ep"]) for s in edl[:6]) + ("…" if len(edl) > 6 else ""),
            "overlap": round(ov, 3),
            "vs": vs,
            "blocks": len(ordered),
            "jumps": len(gaps),
            "avg_gap": round(sum(gaps) / len(gaps), 1) if gaps else 0.0,
            "avg_block": round(sec / max(1, len(ordered)), 1),
            "whole": whole,
            "on": True,
        })
        core.set_task(pid, "plan", 70 + i / max(1, len(sets)) * 28,
                      f"已整理 {i}/{len(sets)} 套")

    if not plans:
        core.set_task(pid, None)
        raise RuntimeError("AI 给出的结构都不可用，请重试")

    total_all = sum(float(ep.get("duration") or 0) for ep in p["episodes"])
    t_avg = sum(pl["sec"] for pl in plans) / len(plans)
    avgs = [pl["overlap"] for pl in plans[1:]]
    avg_ov = sum(avgs) / len(avgs) if avgs else 0.0
    over = [pl for pl in plans if pl["overlap"] > MAX_OVERLAP]
    note = (f"AI 通读全剧 {total_all / 60:.1f} 分钟，直接排出 {len(plans)} 套"
            f"（每套约 {t_avg / 60:.1f} 分钟）；套间平均重合 {avg_ov * 100:.0f}%")
    if over:
        detail = "、".join(f"第{pl['index']}套与第{pl['vs']}套 {pl['overlap'] * 100:.0f}%"
                           for pl in over)
        note += (f"；其中 {len(over)} 套超过 {int(MAX_OVERLAP * 100)}% 上限（{detail}）。"
                 "同一部剧剪多套，素材复用是必然的——要更不一样，就减少套数或缩短单套时长")
    else:
        note += f"，全部在 {int(MAX_OVERLAP * 100)}% 以内"

    p = core.load(pid) or p
    gen = int(p.get("plan_gen") or 0) + 1
    p["plan_gen"] = gen
    p["plans"] = plans
    p["batch"] = {
        "count": len(plans), "min_minutes": min_minutes, "max_minutes": max_minutes,
        "order": order, "plan_note": note,
        "summary": " / ".join(pl.get("ai_name") or f"第{pl['index']}套" for pl in plans[:8]),
        "done": 0, "gen": gen, "by": "ai",
    }
    core.set_task(pid, None)
    core.save(p)


def render_plans(pid: str, save_dir: str | None = None) -> None:
    """只渲染勾选的套。每套完成立即落盘，中断重启自动续跑未完成的。

    save_dir：这批成片存哪个文件夹（用户自己指定）。不传就沿用上次记下的，
    再没有就用全局「成片保存位置」。**不替用户起子文件夹名**——指哪打哪。

    铁律：单套失败只记录、不中断——批量任务里任何一套出问题（磁盘、文件、
    临时目录清不动……）都不许把剩下十几套一起带走。失败的套记进
    batch.failures，前端展示；重新点「出片」会自动只补做没成功的。"""
    p = core.load(pid)
    if not p:
        return
    plans = [pl for pl in (p.get("plans") or []) if pl.get("on", True)]
    if not plans:
        raise RuntimeError("没有勾选任何结构，请先在第 3 步勾选要出片的套")

    total = len(plans)
    # 用户填的是根目录（如 F:/钩子工坊_成片），片子归到 <根>/<项目名>/ 下面。
    # 记的是根目录、不是实际目录：存实际目录的话，下次回填输入框会再套一层项目名。
    root = (save_dir or (p.get("batch") or {}).get("save_dir") or "").strip()
    root = os.path.abspath(os.path.expanduser(root)) if root else core.output_dir()
    if (p.get("batch") or {}).get("save_dir") != root:
        p.setdefault("batch", {})["save_dir"] = root
        core.save(p)
    target = os.path.join(root, core.safe_name(p.get("name") or ""))
    os.makedirs(target, exist_ok=True)

    failures: list[dict] = []
    ok = 0
    # 只认「当前结构版本」的成片记录。旧版本的记录一律当没渲过——结构都换了，
    # 拿旧片子当新产物比重新渲一遍糟糕得多。
    gen = int((p.get("batch") or {}).get("gen") or p.get("plan_gen") or 0)
    for k, pl in enumerate(plans):
        p = core.load(pid) or p
        sets = p.setdefault("sets", [])
        done = next((s for s in sets if s.get("index") == pl["index"] and s.get("output")
                     and int(s.get("gen") or 0) == gen), None)
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
        name = f"{p['name']}_混剪set{pl['index']:02d}_{time.strftime('%m%d%H%M%S')}.mp4"
        try:
            info = render.render_edl(
                p, edl, name, cleanup=False, out_dir=target,
                progress=lambda pct, msg, k=k, total=total: core.set_task(
                    pid, "batch", (k + pct / 100.0) / total * 100,
                    f"第 {k + 1}/{total} 套（set{pl['index']:02d}）：{msg}"))
        except Exception as exc:  # noqa: BLE001 —— 单套失败必须与整批隔离
            msg = (str(exc) or type(exc).__name__).strip()[:200]
            failures.append({"index": pl["index"], "error": msg})
            p = core.load(pid) or p
            p.setdefault("batch", {})["failures"] = failures
            core.set_task(pid, "batch", (k + 1) / total * 100,
                          f"第 {k + 1}/{total} 套（set{pl['index']:02d}）失败：{msg[:80]}，跳过继续")
            core.save(p)
            time.sleep(1)
            continue
        notes = "；".join(filter(None, [
            f"主题：{pl['ai_name']}" if pl.get("ai_name") else "",
            f"{len(edl)} 段 / {info['duration'] / 60:.1f} 分钟（目标 {pl.get('target_sec', 0) / 60:.1f}）",
            pl.get("style") or "",
        ]))
        sets = [s for s in sets if s.get("index") != pl["index"] or int(s.get("gen") or 0) != gen]
        sets.append({
            "index": pl["index"], "gen": gen,
            "edl": edl, "edl_text": pl.get("edl_text", ""),
            "notes": notes, "output": os.path.join(target, info["name"]),
            "output_duration": info["duration"], "output_size": info["size"],
        })
        sets.sort(key=lambda s: s.get("index") or 0)
        p["sets"] = sets
        ok += 1
        p.setdefault("batch", {})["done"] = ok
        core.set_task(pid, "batch", (k + 1) / total * 100, f"已完成 {ok}/{total} 套")
        core.save(p)

    # 整批只清一次临时目录（复用覆盖写，见 render_edl 的 cleanup 说明）
    core.rmtree(os.path.join(target, "_render_tmp", pid))

    p = core.load(pid) or p
    if p.get("batch") is not None:
        p["batch"]["failures"] = failures
    core.save(p)
    if failures and not ok:
        raise RuntimeError(f"全部 {total} 套都失败了，第一套的原因：{failures[0]['error']}")
    core.set_task(pid, None)
