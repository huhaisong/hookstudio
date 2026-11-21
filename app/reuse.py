"""素材复用台账：跨批次记住「这一块素材用过几次」（2026-11-17）。

为什么需要它
------------
`_fit_target` 的 avoid 只管**同一批**里的套间重合：这批出了 6 套，第 3 套不会
再用第 1 套的块。但用户是「同一部剧反复出片」的：今天出 6 套、明天再出 6 套、
下周换号再来 6 套。跨批次的重复完全看不见——上周第 3 套和这周第 1 套用同一场戏，
平台侧就是同一条素材反复发。

竞品（短视频工厂）用「每段最多用 3 次 + 组合不重样 + sidecar penalty」治这件事，
但账本存在浏览器 localStorage 里（换机器/清缓存即丢），生产 sidecar 还要手动跑
bat。我们把它做成项目级：跟 project.json 走，换机器、重装、备份都还在。

记什么、什么时候记
------------------
- 按**套**记：`sets = { "代次:套号": [块键…] }`。同一套因为改参数重渲（fx 变了）
  会覆盖同一个键，不会重复计数——「这块素材被几条片子用过」才是要回答的问题。
- 块键 = `集号:起-止`（0.1 秒取整）。块边界从台词切点来，同一块每代重算基本不变；
  差 0.1 秒以上视为「另一段素材」。
- **出片成功才记**（batch 落库那一刻）。计划阶段不记：AI 编排可能重跑、可能整套
  被丢弃，那时候记账会把没人看过的块也耗掉次数。
- 次数不在文件里存冗余：读的时候由 sets 现算（几十套 × 几十块，量级可忽略），
  改口径/清历史只动一个地方。

怎么用
------
- 选段：`weight(n)` 让用过越多的块排序越靠后，`is_capped(n)` 让超过上限的块
  只在「独有素材不够、宁可重复也别凑不够时长」时才回用（和 avoid 同一套降级逻辑）。
- 记录：`record()` 只负责写；任何异常都不该冒泡到出片流程——台账是提示性数据，
  坏了顶多降权算不准，绝不能挡住出片。
"""
from __future__ import annotations

from datetime import datetime

from . import core

MAX_USE = 3          # 同一块最多用在 3 套里；超过就降权，独有素材不够时才回用
SOFT = 0.6           # 权重 = 1/(1+n*SOFT)：用 3 次 → 0.36，用 6 次 → 0.22
KEEP_SETS = 500      # 保留最近多少套的记忆（一部剧几十套，留足跨月余量）


def key_of(seg: dict) -> str:
    """一块素材的稳定键。集号 + 起止（0.1 秒取整）。"""
    try:
        ep = int(seg.get("ep") or 0)
        s = round(float(seg.get("start") or 0.0), 1)
        e = round(float(seg.get("end") or 0.0), 1)
    except (TypeError, ValueError):
        return ""
    if not ep or e <= s:
        return ""
    return f"{ep}:{s:.1f}-{e:.1f}"


def _clean_sets(raw) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if not isinstance(raw, dict):
        return out
    for sid, keys in raw.items():
        if not isinstance(keys, list):
            continue
        clean = [str(k) for k in keys if isinstance(k, str) and k]
        if clean and str(sid):
            out[str(sid)] = clean
    return out


def load(p: dict | None) -> dict:
    """读台账并做形状兜底（老项目没这字段；手改坏了的也不能让选段崩）。"""
    raw = (p or {}).get("reuse") or {}
    sets = _clean_sets(raw.get("sets") if isinstance(raw.get("sets"), dict) else {})
    keys: dict[str, int] = {}
    for used in sets.values():
        for k in used:
            keys[k] = keys.get(k, 0) + 1
    return {"sets": sets, "keys": keys, "updated": str(raw.get("updated") or "")}


def count(keys: dict | None, seg: dict) -> int:
    """这块素材被几条片子用过。先按精确键查，查不到再按「同集重叠过半」兜底。

    为什么要兜底：AI 每代重排时块边界会挪一两秒（台词切点本来就允许浮动），
    精确键对不上，台账就白记了。同集、时间重叠超过较短块一半，视为同一块素材。
    """
    keys = keys or {}
    k = key_of(seg)
    try:
        n = int(keys.get(k, 0))
    except (TypeError, ValueError):
        n = 0
    if n or not k:
        return n
    try:
        ep_s, rng = k.split(":", 1)
        s_s, e_s = rng.split("-", 1)
        ep_i, s, e = int(ep_s), float(s_s), float(e_s)
    except (ValueError, AttributeError):
        return 0
    best = 0
    for kk, v in keys.items():
        try:
            kep, rng2 = kk.split(":", 1)
            if int(kep) != ep_i:
                continue
            ks_s, ke_s = rng2.split("-", 1)
            ks, ke = float(ks_s), float(ke_s)
        except (ValueError, AttributeError):
            continue
        ov = min(e, ke) - max(s, ks)
        if ov > min(e - s, ke - ks) * 0.5:
            try:
                best = max(best, int(v))
            except (TypeError, ValueError):
                continue
    return best


def weight(n: int) -> float:
    """评分权重。边际递减而不是一刀切：用过 1 次仍可能是最优块。"""
    return 1.0 / (1.0 + max(0, int(n)) * SOFT)


def is_capped(n: int) -> bool:
    return int(n) >= MAX_USE


def record(p: dict, entries: list[tuple[str, list[dict]]]) -> dict:
    """把已出片的套记进台账。entries = [(代次:套号, 该套的段列表), …]。

    同一个 sid 覆盖而不是累加：改参数重渲同一套不该让块多算一次。
    """
    pid = (p or {}).get("id")
    if not pid or not entries:
        return load(p)
    latest = core.load(pid) or p
    data = load(latest)
    sets = data["sets"]
    for sid, segs in entries:
        used: list[str] = []
        for seg in (segs or []):
            k = key_of(seg)
            if k and k not in used:
                used.append(k)
        if sid and used:
            sets[sid] = used
    # 只留最近 KEEP_SETS 套：按 sid 里的代次粗排不可靠，按写入顺序留最新的
    if len(sets) > KEEP_SETS:
        drop = max(0, len(sets) - KEEP_SETS)
        for sid in list(sets)[:drop]:
            sets.pop(sid, None)
    out = {"sets": sets, "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    latest = dict(latest)
    latest["reuse"] = out
    core.save_merge(latest, ["reuse"])
    return load(latest)


def stats(p: dict | None) -> dict:
    """给界面/日志看的概览：用过几块、平均几次、最多几次。"""
    data = load(p)
    keys = data["keys"]
    if not keys:
        return {"blocks": 0, "avg": 0.0, "max": 0, "sets": 0}
    vals = list(keys.values())
    return {
        "blocks": len(vals),
        "avg": round(sum(vals) / len(vals), 2),
        "max": max(vals),
        "sets": len(data["sets"]),
    }
