"""批量模式：API 只调一次建「精彩片段池」，之后由本机组装 N 套剪辑结构，
用户挑选后再逐套渲染。

流程（对应界面 4 步）：
1. 转写 —— 建项目后自动跑
2. 片段池 —— 转写完成后自动挖（调一次 API），用户可勾选剔除
3. 剪辑结构 —— 本机组装 N 套 EDL（不渲染、不调 API），用户勾选要哪几套
4. 出片 —— 只渲染勾选的套

组装规则：按评分加权随机选段凑时长，同一套内按集数顺序排列（不乱跳），
部分套把最高分片段提前开场抓人。
"""
from __future__ import annotations

import os
import random
import time

from . import analyze, core, render, transcribe


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

# 「接得上」的判定：两块在全局时间轴上的间隙 ≤ 这个秒数就优先续着播。
#
# 挖池时「起意」和「结果」是分开圈的两块，中间隔着被省掉的过程（路程、挑货、
# 上楼），所以这个窗口必须比过去宽（原来 10 秒）——否则同一件事的两半会被拆到
# 不同套里去，成片就成了东一榔头西一棒子。30 秒大致是「一个完整事件」的跨度。
ADJ_GAP = 30.0


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


def _compose(blocks: list[dict], target: float, seed: int,
             usage: dict | None = None, bias: str = "any",
             prevs: list[list[dict]] | None = None) -> list[dict]:
    """从池子里挑块组装一套，返回按时间轴排好的**块列表**。

    核心是「接着上一块挑」：已选内容在时间轴上两头各有一个边界，优先挑紧挨着
    边界的块（间隔 ≤ ADJ_GAP 秒）。由于挖池把一件事拆成了「起意」和「结果」两块、
    中间的过程被省掉，这个「接着挑」正好会把两半都选进来——播出来就是
    「说去镇上 → 拎着东西进门」，过程自动跳过，因果仍然成立。
    只有实在接不上时，才跳到别处继续长。

    **复用控制靠配额，不靠惩罚**（用户 2026-11-02 晚的要求：任意两套 ≤70% 即可，
    不是「前面用过后面就绝不能用」）：
      prevs = 已接受的几套。给每套留出 `MAX_OVERLAP × 90%` 的重合预算，
      挑块时只要这一块加进去还没超任何一套的预算，就照常用——哪怕它已经被
      前面几套大量使用。预算用完了，才开始挑没用过的块。
    这样出来的每一套都是「先在好素材里挑，缺的那点再用新素材补」，
    重合度自然落在 60-70% 附近，而不是像原来那样非 0 即 100%。

    为什么不能用「用过的块扣分」：扣分只能给出两种结果——不扣（两套 100% 一样）
    或扣狠（两套 17%，等于把好素材全让出去）。实测 damp 0→2 之间直接从 100%
    跳到 17%，永远落不进中间区间，于是第 3 套必然凑不出来。
    """
    rng = random.Random(seed * 7919 + 104729)
    usage = usage or {}
    if not blocks:
        return []
    lo = min(b["gs"] for b in blocks)
    hi = max(b["ge"] for b in blocks)
    span = max(1.0, hi - lo)

    # 复用预算按**预期整片时长**（target）算，不按「当前累计」。
    # 按当前累计算的话，第一块 20 秒的片子里 20 秒是旧素材，比例 100%，
    # 所有复用块一上来就被判死，后面的套连开场都选不出来。
    # 先按 target 给额度、照常用好素材，收尾时若实际没长满导致比例超标，
    # 再在下面用「修剪」把超的那几块摘掉（两步走，见函数末尾）。
    prevs = prevs or []
    prev_secs = [sum(s["end"] - s["start"] for s in pv) for pv in prevs]
    caps = [MAX_OVERLAP * 0.92 * min(target, s) for s in prev_secs]
    spent = [0.0] * len(prevs)

    def _fits(b: dict) -> bool:
        for j, pv in enumerate(prevs):
            if spent[j] + _inter_sec(b["pieces"], pv) > caps[j]:
                return False
        return True

    def _bias_bonus(b: dict) -> float:
        mid = (b["gs"] - lo) / span
        if bias == "front":
            return 3.5 if mid <= 0.45 else 0.0
        if bias == "back":
            return 3.5 if mid >= 0.55 else 0.0
        if bias == "peak":
            return 2.0 if (b.get("rating") or 5) >= 8 else 0.0
        return 0.0

    def _score(b: dict) -> float:
        used = [usage.get(_seg_key(ps), 0) for ps in b["pieces"]]
        pen = sum(used) / max(1, len(used))
        sc = ((b.get("rating") or 5) + _bias_bonus(b)
              - 0.6 * pen                      # 轻微偏好新素材，不排斥复用
              + rng.uniform(-1.5, 1.5))
        if chosen:
            # 跳得越远越贵：宁可要近处 8 分的块，也不要 500 秒外 10 分的块。
            # 不加这条，成片就是「第 4 集 → 第 9 集」这种大跳——块内再连贯，
            # 观众也会觉得剧情断了（用户 2026-11-02 实测最刺眼的问题）。
            far = _gap_to(b) - ADJ_GAP
            if far > 0:
                # 上限 5 分（不是 9）：罚太狠会逼着每套都走同一条近路，
                # 套与套之间立刻撞车，反而组不出几套。5 分足以判「远不如近」，
                # 又给「这一带实在没货了」留了往远处走的余地。
                sc -= min(5.0, far / 60.0)
        return sc

    chosen: list[dict] = []
    total = 0.0

    def _hits(b: dict) -> bool:
        return any(b["gs"] < c["ge"] and c["gs"] < b["ge"] for c in chosen)

    def _gap_to(b: dict) -> float:
        """与已选内容在时间轴上的最近距离（0 = 紧贴着）。

        必须**按方向**算：块在已选内容之后，间隙是 b.gs - c.ge；在之前，间隙是
        c.gs - b.ge。原来两边都取 abs，等于把「往前接」的 5 秒间隙算成 30 秒，
        结果前面那半段剧情永远被多罚、永远长不上来——一套片子只会往后走，
        前几集的素材白挖（2026-11-02 修）。
        """
        return min(
            (b["gs"] - c["ge"]) if b["gs"] >= c["ge"] else (c["gs"] - b["ge"])
            for c in chosen)

    while total < target:
        cands = [b for b in blocks if not _hits(b)]
        if not cands:
            break
        if chosen:
            near = [b for b in cands if _gap_to(b) <= ADJ_GAP]
            if near:
                cands = near          # 有能接上的，就先把这一段剧情长完
        # 复用配额：只在这块加进去仍不超各套预算的范围里挑。
        # 剩下的块全都超预算 → 本套就到这儿，不再硬塞（塞了也会被上限拒掉）。
        if prevs and chosen:
            ok = [b for b in cands if _fits(b)]
            if not ok:
                break
            cands = ok
        best = max(cands, key=_score)
        if chosen and total + best["dur"] > target * 1.10:
            room = [b for b in cands if total + b["dur"] <= target * 1.10]
            if not room:
                break
            best = max(room, key=_score)
        chosen.append(best)
        total += best["dur"]
        if prevs:
            for j in range(len(prevs)):
                spent[j] += _inter_sec(best["pieces"], prevs[j])

    chosen.sort(key=lambda b: b["gs"])
    return _trim_reuse(chosen, prevs, prev_secs)


def _trim_reuse(chosen: list[dict], prevs: list[list[dict]],
                prev_secs: list[float]) -> list[dict]:
    """收尾修剪：素材不够、套没长满 target 时，绝对额度（0.7×target）可能超过
    「实际总长×0.7」。这时从尾部摘掉带重合贡献的块，直到比例真的合规。

    只摘「与前面某套有重合」的块，纯新素材不动——摘掉的永远是可以从别套里
    再看到的画面，不牺牲独有内容。摘到一块都不剩就返回空，由 plan_run 判定
    「素材到顶」并停下，而不是塞一套超线的片子。
    """
    if not prevs:
        return chosen
    while chosen:
        da = sum(b["dur"] for b in chosen)
        allp = [ps for b in chosen for ps in b["pieces"]]
        worst = max((_inter_sec(allp, pv) / max(1e-6, min(da, prev_secs[j]))
                     for j, pv in enumerate(prevs)), default=0.0)
        if worst <= MAX_OVERLAP * 0.97:
            break
        cut = -1
        for idx in range(len(chosen) - 1, -1, -1):
            if any(_inter_sec(chosen[idx]["pieces"], pv) > 0 for pv in prevs):
                cut = idx
                break
        if cut < 0:
            break
        chosen.pop(cut)
    return chosen


def build_pool_run(pid: str, gear: str = analyze.DEFAULT_GEAR) -> None:
    """「AI 挖片段池」任务：调一次 API 建池并落盘。gear 为挖掘挡位（精选/标准/宽松/极限）。"""
    p = core.load(pid)
    if not p:
        return
    if not core.llm()["key"]:
        raise RuntimeError("没有配置 API Key，请在页面「设置」里填写")
    pool = analyze.build_pool(p, gear=gear)
    p["pool"] = pool
    p["pool_gear"] = pool.get("gear") or gear
    core.set_task(pid, None)
    core.save(p)


def auto_run(pid: str, gear: str = analyze.DEFAULT_GEAR) -> None:
    """批量混剪的自动起步：没转写完就转写，转完没池子就挖池子。全程不需要人点。"""
    p = core.load(pid)
    if not p:
        return
    if not gear:
        gear = p.get("pool_gear") or analyze.DEFAULT_GEAR
    if any(not (ep.get("lines") or []) for ep in p.get("episodes") or []):
        transcribe.run(pid)
        p = core.load(pid) or p
    if any(not (ep.get("lines") or []) for ep in p.get("episodes") or []):
        return  # 还有集没台词（出错），不继续挖池子
    if not ((p.get("pool") or {}).get("segments")):
        build_pool_run(pid, gear)


def _selected_segments(p: dict) -> list[dict]:
    """池子里的全部片段。

    2026-11-02 用户定：片段池不再勾选，AI 圈出什么就全部用上。原来按 on 过滤、
    还要为跨集块做「取消一片整块不用」的连带处理，那套逻辑一并删掉——挑选没有
    净收益，反而让用户替 AI 的判断兜底。
    """
    segs = (p.get("pool") or {}).get("segments") or []
    if not segs:
        raise RuntimeError("片段池还是空的，请先在第 2 步挖片段池")
    return segs


# 套间重合度硬上限（重合秒数 / 较短一套的总秒数）。超过说明两套大部分素材雷同，
# 用户定的 70%（2026-11-02 从 60% 上调）——组不出来就停，别硬塞一套「换皮」的。
MAX_OVERLAP = 0.7


def _max_sets_heuristic(total_pool: float, t_sec: float) -> int:
    """重合度 ≤ MAX_OVERLAP 时素材池理论上最多撑几套（估算，供提示用）。

    用 Bonferroni 下界反推：n 套各长 T、两两重合 ≤ cT 时，池子至少要
    nT - c*T*C(n,2)/2 ≥ ... 解不等式取可行侧的根；池子够大时返回 0 表示「很难触顶」。"""
    if t_sec <= 0:
        return 0
    r = total_pool / t_sec
    c = MAX_OVERLAP / 2
    # 可行条件：r ≥ n - c*n*(n-1)，即 c*n² - (1+c)*n + r ≥ 0 有可行小根
    disc = (1 + c) ** 2 - 4 * c * r
    if disc < 0:
        return 0  # 池子相对单套时长很充裕，正常组不到上限
    root = ((1 + c) - disc ** 0.5) / (2 * c)
    return max(0, int(root))


def plan_run(pid: str, count: int, min_minutes: float = 3.0, max_minutes: float = 7.0,
             order: str = DEFAULT_ORDER) -> None:
    """「生成 N 套剪辑结构」：纯本机组装，不调 API、不渲染，落盘等用户挑选。

    order 控制播放顺序策略：
      auto  = 各套轮换不同叙事结构（默认）
      chrono= 全部严格集序
      varied= 全部使用变序开场（不做纯集序）
    """
    p = core.load(pid)
    if not p:
        return
    if min_minutes > max_minutes:
        min_minutes, max_minutes = max_minutes, min_minutes
    segs = _ensure_geo(p, _selected_segments(p))
    blocks = _blocks(segs)
    total_pool = sum(s["end"] - s["start"] for s in segs)
    summary = (p.get("pool") or {}).get("summary") or ""
    if total_pool < 20:
        raise RuntimeError("片段池太短（不足 20 秒），请重新挖池")

    core.set_task(pid, "plan", 5, f"从 {len(blocks)} 块（{len(segs)} 片）里组装 {count} 套结构")

    # 预检：哪怕每套都压到「最短」，两套重合 ≤MAX_OVERLAP 也至少要
    # (2-MAX_OVERLAP) 倍单套时长的池子（70% 上限即 1.3 倍）。
    # 一眼能看出组不出来的，别让用户等组装跑完才报错。
    if count >= 2 and total_pool < (2 - MAX_OVERLAP) * min_minutes * 60:
        need = (2 - MAX_OVERLAP) * min_minutes / 60
        raise RuntimeError(
            f"片段池太小：共 {total_pool / 60:.1f} 分钟，装不下两套 {min_minutes:g} 分钟起、"
            f"重合度 ≤{int(MAX_OVERLAP * 100)}% 的片子（最少要约 {need:.1f} 分钟）。"
            "请压低「最短」时长，或回第 2 步多勾选些片段。")

    plans = []
    accepted: list[list[dict]] = []
    usage: dict[tuple, int] = {}     # 片段 -> 已被前几套用了几次
    seed_base = int(time.time()) % 100000
    # 差异上限：套数越多，每套时长越要压在池子总量的一小部分里，否则数学上必然重合。
    #   ≤2 套：可用到池子 92%；3-5 套：62%；≥6 套：55%
    div_ratio = 0.92 if count <= 2 else (0.62 if count <= 5 else 0.55)
    cap_max = max(min_minutes, min(max_minutes, total_pool / 60 * div_ratio))
    capped = cap_max < max_minutes
    # 侧重轮换：高分优先 → 前半段 → 后半段 → 全程随机，循环
    biases = ["peak", "front", "back", "any"]
    modes = _pick_modes(order)
    rng_t = random.Random(seed_base)
    stop_reason = ""
    for i in range(1, count + 1):
        target = rng_t.uniform(min_minutes, cap_max) * 60
        bias = biases[(i - 1) % len(biases)] if count > 1 else "peak"
        mode = modes[(i - 1) % len(modes)]

        # 每套生成多个候选，挑「时长最准」的那份。重合度已经在 _compose 里用
        # 配额控住了（预算是 MAX_OVERLAP×90%），所以这里不再靠重试去碰运气，
        # 只用来挑时长更贴 target 的版本。
        best = None
        for k in range(12):
            cand = _compose(blocks, target, seed_base + i * 131 + k * 17,
                            usage, bias, accepted)
            if not cand:
                continue
            pieces = sorted([ps for b in cand for ps in b["pieces"]], key=lambda s: s["gs"])
            sec_c = sum(b["dur"] for b in cand)
            ov = max((_overlap(pieces, a) for a in accepted), default=0.0)
            if accepted and ov > MAX_OVERLAP:
                continue
            cost = abs(sec_c - target) / max(target, 1.0) + ov * 0.5
            if best is None or cost < best[0]:
                best = (cost, cand, sec_c, ov)
        # 配额用完时 _compose 会提前收尾，套可能比最短时长还短。那种套播出去
        # 就是个半截子，不如如实停下来报告素材到顶。
        if best is None or best[2] < min_minutes * 60 * 0.85:
            stop_reason = (
                f"素材池到顶了：再组新的只能反复用前面几套的素材，会超过重合度上限"
                if accepted else "片段池凑不满一套")
            break
        _, cand_blocks, sec, ov = best
        ordered, used_mode = _apply_order(cand_blocks, mode)
        # 整块按顺序展开成逐集切片；同一块跨集时两片紧挨着，渲染后就是连续的
        edl = [ps for b in ordered for ps in sorted(b["pieces"], key=lambda s: s["gs"])]

        for s in edl:  # 记录用量，后面的套会主动避开
            usage[_seg_key(s)] = usage.get(_seg_key(s), 0) + 1
        accepted.append(edl)

        # 节奏体检：跳切次数 + 平均每次跳过多少秒。跳切本身是省略过程，不是缺点；
        # 真正要看的是「跳过的那段有多长」——跳过十几秒是省过程，跳过几百秒是换剧情。
        gaps = [b["gs"] - a["ge"] for a, b in zip(edl, edl[1:]) if b["gs"] - a["ge"] > 3.0]
        jumps = len(gaps)
        avg_gap = sum(gaps) / jumps if jumps else 0.0
        avg_blk = sum(b["dur"] for b in cand_blocks) / max(1, len(cand_blocks))

        eps = sorted({s["ep"] for s in edl})
        bias_name = {"peak": "高分优先", "front": "剧情前半", "back": "后半爆发", "any": "全程随机"}[bias]
        order_name = ORDER_MODES[used_mode]["name"]
        seq = "→".join(str(s["ep"]) for s in edl[:6]) + ("…" if len(edl) > 6 else "")
        plans.append({
            "index": i,
            "edl": edl,
            "edl_text": analyze.edl_text(edl),
            "target_sec": round(target, 1),
            "sec": round(sec, 1),
            "eps": [eps[0], eps[-1]] if eps else [],
            "style": f"{order_name} · {bias_name}",
            "order": used_mode,
            "order_desc": ORDER_MODES[used_mode]["desc"],
            "seq": seq,
            "overlap": round(ov, 3),
            "blocks": len(cand_blocks),
            "jumps": jumps,
            "avg_gap": round(avg_gap, 1),
            "avg_block": round(avg_blk, 1),
            "on": True,
        })
        core.set_task(pid, "plan", 5 + i / count * 90, f"已组装 {i}/{count} 套")

    if not plans:
        core.set_task(pid, None)
        raise RuntimeError(
            f"一套都没组出来（{stop_reason or '片段池太短'}）。"
            "请回第 2 步多勾选些片段，或压低时长要求。")

    avgs = [pl["overlap"] for pl in plans[1:]]
    avg_ov = sum(avgs) / len(avgs) if avgs else 0.0
    # 理论下限：目标时长 T、池子总量 P 时，任何两套至少要重合 max(0, 2T-P)/T
    t_avg = sum(pl["sec"] for pl in plans) / len(plans)
    floor_ov = max(0.0, (2 * t_avg - total_pool) / t_avg) if t_avg else 0.0
    note = (f"片段池 {len(blocks)} 块（{len(segs)} 片）共 {total_pool / 60:.1f} 分钟；"
            f"本批平均重合度 {avg_ov * 100:.0f}%（上限 {int(MAX_OVERLAP * 100)}%"
            + (f"，理论下限 {floor_ov * 100:.0f}%" if floor_ov else "") + "）")
    if plans:
        jumps_avg = sum(pl.get("jumps") or 0 for pl in plans) / len(plans)
        blk_avg = sum(pl.get("avg_block") or 0 for pl in plans) / len(plans)
        gap_avg = sum(pl.get("avg_gap") or 0 for pl in plans) / len(plans)
        note += (f"；每套平均每块 {blk_avg:.0f} 秒、跳切 {jumps_avg:.0f} 次"
                 f"（平均每次跳过 {gap_avg:.0f} 秒——跳过十几秒是省过程，跳过几分钟是换剧情）")
    else:
        note += "；每套平均每块偏短或偏长，节奏可调"
    if capped:
        note += (f"；为了让 {len(plans)} 套互不雷同，每套时长已限制在 {cap_max:.1f} 分钟以内"
                 f"（池子的 {int(div_ratio * 100)}%）")

    # 组不满用户要的套数：把已经组好的先落盘（能用先用，不白等），
    # 再明确告诉用户「套数设多了」和该怎么调——不静默缩水。
    short = len(plans) < count
    if short:
        note += f"；只组出 {len(plans)}/{count} 套（重合度上限卡住了）"

    used_modes = []
    for pl in plans:
        nm = ORDER_MODES[pl["order"]]["name"]
        if nm not in used_modes:
            used_modes.append(nm)
    note += "；本批用到的叙事顺序：" + " / ".join(used_modes)

    # —— 落盘：凑满没凑满都走这里，一处写，避免两套逻辑走岔 ——
    # 2026-11-02 踩到：组不满的分支自己拼了一个**不带 gen** 的 batch，
    # 版本号于是回落到 plan_gen（旧值），上一版同号的成片被当成「已完成」直接跳过，
    # 勾了 2 套只出 1 套。版本号必须由这里统一发。
    p = core.load(pid) or p
    gen = int(p.get("plan_gen") or 0) + 1
    p["plan_gen"] = gen
    p["plans"] = plans
    p["batch"] = {
        "count": len(plans), "min_minutes": min_minutes, "max_minutes": max_minutes,
        "order": order, "plan_note": note, "summary": summary, "done": 0, "gen": gen,
    }
    core.set_task(pid, None)
    core.save(p)

    if short:
        est = _max_sets_heuristic(total_pool, (min_minutes + cap_max) / 2 * 60)
        hint = (f"按当前片段池和时长设置，重合度 ≤{int(MAX_OVERLAP * 100)}% 时"
                f"大约最多能出 {est} 套" if est else "请减少套数或压低时长")
        raise RuntimeError(
            f"套数设多了：素材池只撑得起 {len(plans)}/{count} 套（{stop_reason}）。"
            + (f"能出的 {len(plans)} 套已留在下面，可直接勾选出片。" if plans else "")
            + f"{hint}。也可以压低「最长」分钟数，或回第 2 步多勾片段。")


def ai_plan_run(pid: str, count: int, min_minutes: float = 4.0,
                max_minutes: float = 5.0, order: str = DEFAULT_ORDER) -> None:
    """第 3 步的另一种做法：让 AI 一次看完整部剧，直接排出 N 套结构。

    跟 plan_run（本机组装）的分工，两个都要留着：
      plan_run    → 从片段池里拼。免费、秒级、重合度精确可控，但贪心看不出故事线。
      ai_plan_run → 从全剧台词里排。看得见因果链，六套能分到不同主线，
                    代价是走 1 次 API，且时长/重合度只能事后校验。

    **重合度不拦截**：同一部剧剪 N 套，素材必然复用（6 套 × 4.5 分钟 = 27 分钟，
    而全剧只有 21 分钟）。强行卡 70% 只会让这次 API 白花钱、还一套都拿不到。
    所以这里如实算出来，超线就在 note 里点名「第 3 套和第 5 套重合 78%」，
    由用户决定重排还是接受——跟本地组装那种「必须合规才落盘」的策略不同。
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
        plans.append({
            "index": i,
            "edl": edl,
            "edl_text": analyze.edl_text(edl),
            "target_sec": round((min_minutes + max_minutes) / 2 * 60, 1),
            "sec": round(sec, 1),
            "eps": [eps[0], eps[-1]] if eps else [],
            "style": f"AI 编排 · {st['name']}",
            "ai_name": st["name"],
            "order": used_mode,
            "order_desc": ORDER_MODES[used_mode]["desc"],
            "seq": "→".join(str(s["ep"]) for s in edl[:6]) + ("…" if len(edl) > 6 else ""),
            "overlap": round(ov, 3),
            "vs": vs,
            "blocks": len(ordered),
            "jumps": len(gaps),
            "avg_gap": round(sum(gaps) / len(gaps), 1) if gaps else 0.0,
            "avg_block": round(sec / max(1, len(ordered)), 1),
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
        "order": order, "plan_note": note, "summary": p.get("pool", {}).get("summary") or "",
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

    summary = (p.get("batch") or {}).get("summary") or ""
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
            f"主题：{summary}" if summary else "",
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
