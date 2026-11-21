"""台词校对（2026-11-19）：AI 出清单，人勾选才写回。

两种模式，按项目类型自动分流（2026-11-19 用户定）：
  · 整剧模式（主项目，一整部连续剧）：全部台词拼成一份、一次调用。人名跨集统一
    是对的——同一部戏里「顾清唐/顾青棠/顾清堂」确实是同一人。
  · 逐集模式（选视频直接出片，direct=True）：**每个视频单独一次调用**。直出选进来
    的多个视频互相独立（可能是完全不同的剧），合并会出实质错误：
      - 不同视频里的同名角色会被当成同一人，一改全改、把名字改坏；
      - 整剧只送前 40 集，选 50 个视频时后面 10 个永远得不到校对；
      - 整剧指纹也只算前 40 集，改后面的台词旧清单不会作废，套上去会错位。

两类建议，都只出清单、你勾选才写回：
  1. 人名归并：同一角色被写成几种同音字时，**取出现次数最多的写法当标准**；
     你能在清单里把标准改成想要的写法。
  2. 错别字：普通词的同音/形近字、明显断句、明显语病。

一律不碰：时间码（只改 lines[].text）、繁简（zh.py 已转完，不许改回繁体）、
数字/语气词。改字会让 nosub 指纹失效，界面提示重新生成/重新出片。

模型：deepseek-flash（2026-11-19 用户定，先由 GLM-5.2 换到 pro，再用真实环境
横评换到 flash）。换的理由不是"便宜"而是**精确率**——自动写回无人复核，误报
等于把台词改坏：真转 10 集 2463 字的同输入横评里，pro 一致率 94.3% 但会造出
「李家村→李家庄」、把「她」改成「他」；flash 一致率 92.1%、真误报≈0、只报 1
组人名，且快 4 倍、价格约 1/4。证据见 logs/cmp10/。
另一条旧的选型依据（GLM 误杀率最高：CheckBench）只用于排除 GLM，别再据此选回 pro。
走 ds_key 官方接口、reasoning_effort=low 省额度，不吃百炼额度。
选型号不能按 deepseek 前缀判——白名单只有 pro/flash 两个精确名。
复用 analyze._call：接口路由、欠费/限流报错、思考模型降级全继承。
"""
from __future__ import annotations

import hashlib
import re
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from . import analyze, core, zh

# 校对模型（2026-11-19 用户定：用 flash）。
# 依据是**真实环境横评**（whisper 真转 10 集 → 同一份 2463 字原文喂四家）：
#   pro 60 改对/4 行真错/239s/0.53 元；flash 51/≈0/57s/0.12 元。
# 自动写回模式下「误报＝改坏台词」且无人复核，所以按精确率选，不按命中数选；
# flash 还只报 1 组人名（陆正），pro 会把人名组扩充到「李家村→李家庄」。
# 别再凭"贵的就是好的"换回 pro。横评脚本 logs/_real_cmp_models.py、对照页 logs/cmp10/。
# 官方 Key 白名单精确名，改名前先核对 core.DEEPSEEK_OFFICIAL。
MODEL = "deepseek-flash"
# 单次「文字修正」条数上限。这是**整剧一次调用**的总额，不是每集——40 的时候
# 40 集平均一集只够 1 条，一集错好几句话就会被砍掉（2026-11-19 用户指出）。
# 提到 200 = 平均每集 5 条，够用；同时它也是防 JSON 截断的护栏：粗略每条 60~70
# token，200 条约 1.3 万 token，在 32768 里安全；再往上（500+）就有半截风险，
# 一旦截断整份清单作废。所以是"提高"，不是"取消"。
MAX_TYPO = 200      # 单次文字修正条数上限
MAX_NAME = 20       # 单次人名组上限
MAX_DROP = 40       # 单次「建议删除的杂音行」上限
MAX_EPISODES = 40   # 整剧模式最多送前 40 集（用户定的量，几万字，装得下）

# ── 整剧校对要不要分批（2026-11-19 用户定：按台词量分，不按集数）──────
# 为什么需要：整剧一次调用时，40 集约 1000 行台词要吐几百条 JSON；推理模型的
# 思考 token 也算在 max_tokens 里，思考吃掉大半后正文被截断，JSON 不完整 → 400。
# 实测 40 集一次跑 3 分 57 秒后失败（12:51 那次）。分批后单批输出只有几十条，
# 离上限远，失败还能只重跑那一批。
# **判据只用台词字数，不用集数**（用户 2026-11-19 定）：剧集长短差很多，
# 10 集可能是 2500 字也可能是 500 字；直出场景还没有「集」的概念。字数才是
# 真正的成本/输出量，短集就多装几集，长集自然少装。
# 实测锚点：2467 字一批 → 37 条错字 + 4 组人名，182 秒成功。
BATCH_MAX_CHARS = 3000   # 单批台词字数上限（约 250 行，输出 50 条上下，留足余量）
BATCH_MIN_TAIL = 0.5     # 末批不足上限的一半就并回上一批，别为几行字单跑一次
BATCH_TAIL_FOLD = 1.5    # 并回去之后允许超上限到 1.5 倍（4000 字一批仍在安全区）
MAX_NAME_ALL = 40        # 分批合并后的人名组总上限（单批是 MAX_NAME=20）
AUTO_LOG_MAX = 400       # 自动写回的明细最多记多少条（给界面查看，防 project.json 膨胀）

# 逐视频校对同时跑几个（2026-11-19 用户要求开并行）。只开 2：DeepSeek 官方接口
# 的并发/TPM 上限没公开，昨天实测撞过 429；校对是低频操作，2 路已能把
# 50 个视频从「逐个数」压到一半时间，再高收益递减而限流风险线性上升。
PARALLEL = 2
KEEP = re.compile(r"[\u4e00-\u9fff]")

# 直出项目 = 每个视频一集，互相独立 → 逐集校对，不许合并
def _direct(p: dict) -> bool:
    return bool(p.get("direct"))


def _is_whole(cur: dict) -> bool:
    """这份清单是不是「整剧」形态。

    2026-11-19 之前的老项目清单没有 mode 字段，那时候只有整剧一种形态——
    缺字段一律当整剧，别让升级后老项目的清单突然变成「不存在」。
    """
    return (cur.get("mode") or "whole") == "whole"


def _ep_sig(ep: dict) -> str:
    """单集台词指纹（只认文字）。逐集模式每集各存一份，各管各的作废。"""
    h = hashlib.sha1()
    for ln in (ep.get("lines") or []):
        h.update(str(ln.get("text") or "").encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()[:12]


def all_sig(p: dict) -> str:
    """整剧台词指纹：**全量**，任何一集台词变了清单就作废。

    以前只算前 MAX_EPISODES 集——第 41 集之后改了台词指纹不变，旧清单不会作废，
    应用到已经变过的台词上会错位。哈希很便宜，全量算。
    """
    h = hashlib.sha1()
    for ep in (p.get("episodes") or []):
        h.update(str(ep.get("index") or 0).encode("utf-8"))
        h.update(_ep_sig(ep).encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()[:12]


def _name_counts(eps: list[dict]) -> dict[str, int]:
    """统计这批台词里 2-4 字中文串的出现次数，给「取最多」定标准用。

    只用于校验/定标准，不用来猜人名——AI 负责指出「哪几个写法是同一人」。
    """
    c: Counter = Counter()
    for ep in eps:
        for ln in (ep.get("lines") or []):
            t = str(ln.get("text") or "")
            for n in (2, 3, 4):
                for i in range(len(t) - n + 1):
                    seg = t[i:i + n]
                    if all(KEEP.match(ch) for ch in seg):
                        c[seg] += 1
    return dict(c)


def _system(whole: bool, only_index: int | None = None) -> str:
    """提示词。2026-11-19 用户要求：不再附「高频词及次数」参考表。

    标准写法本来就由本地 _clean_names 按 counts 重算（次数最多者胜，AI 的
    standard 只在次数相同时作 tie-break），所以去掉参考表不影响结果，
    只是让提示词更短、少一段可能误导模型的清单。
    """
    scope = "通读一整部剧" if whole else "通读这一个视频的台词（它和别的视频没有关系）"
    idx_hint = ("ep 是集号（项目的 index），i 是该集台词行号（从 0 开始）。\n"
                if whole else
                f"这里只有一个视频，ep 一律填 {only_index}，i 是该视频台词行号（从 0 开始）。\n")
    return (
        "你是电视剧台词的文字校对员，" + scope + "，只做三件事。\n"
        "不要润色、不要改写剧情、不要调整语序、不要把口语改成书面语。\n\n"
        "【任务一：人名写法统一】\n"
        "转写常把同一个人名写成几种同音字（如「顾清唐」「顾青棠」「顾清堂」是同一人）。\n"
        "找出这些「明显是同一人却写法不同」的组，每组给：\n"
        "  · variants：这个人的几种写法（只列你确认是同一人的，别把不同角色合并）；\n"
        "  · standard：推荐统一的写法，取出现次数最多的那种。\n"
        "只报你高度确信是同一人的组，拿不准的不要报——把两个不同角色合并是严重错误。\n"
        "最多 " + str(MAX_NAME) + " 组。没有就输出空数组。\n\n"
        "【任务二：文字修正】\n"
        "只修下面四类，其余一律不动：\n"
        "  · 同音/形近错字：kind 填 typo（「即然」应为「既然」）；\n"
        "  · 识别乱码、无意义符号：kind 填 fix（「狼栽◆唯独」应为「狼灾，唯独」）；\n"
        "  · 漏字多字：kind 填 fix（「他说说说」应为「他说」）；\n"
        "  · 断句明显错乱、明显语病：kind 填 fix。\n"
        "下面是已经处理好的，一律不要当错误报：\n"
        "  · 台词已经是简体，不要把任何字改成繁体或异体字；\n"
        "  · 人名写法问题交给任务一，这里不要再报；\n"
        "  · 数字、时间、量词、语气词按原样保留；\n"
        "  · 不润色、不改剧情、不重写整句，口语化的说法照原样留着。\n"
        "没十足把握就不要报。old 必须是台词里逐字出现的一段，new 只替换那一段。\n"
        "最多 " + str(MAX_TYPO) + " 条，按可信度排（最该改的排最前）。\n\n"
        "【任务三：建议删除的行】\n"
        "只挑**整行都是**纯杂音、无意义填充的行：整行只有「嗯」「啊」「哦」「呃」\n"
        "「那个那个」这类，没有任何实际台词内容。\n"
        "有实义的表演台词即使带语气词也保留；长句里的语气词不要删行。\n"
        "最多 " + str(MAX_DROP) + " 条。\n\n"
        "只输出 JSON，格式：\n"
        '{"names":[{"variants":["顾清唐","顾青棠","顾清堂"],"standard":"顾清唐"}],'
        '"typos":[{"ep":1,"i":0,"kind":"typo","old":"即然","new":"既然","why":"同音错别字"},'
        '{"ep":1,"i":3,"kind":"fix","old":"狼栽◆唯独","new":"狼灾，唯独","why":"乱码+错字"}],'
        '"drops":[{"ep":1,"i":5,"why":"整行只有嗯啊"}]}\n'
        + idx_hint
        + "没有的项输出空数组，三项都没有就输出 {\"names\":[],\"typos\":[],\"drops\":[]}。"
    )


def _prompt(eps: list[dict], only_index: int | None = None) -> str:
    """拼送模型的台词。only_index 给定时只列那一集（逐集模式）。"""
    rows = []
    for ep in eps:
        idx = int(ep.get("index") or 0)
        if only_index is not None and idx != only_index:
            continue
        lines = ep.get("lines") or []
        if not lines:
            continue
        rows.append(f"=== 第 {idx} 集 ===")
        for i, ln in enumerate(lines):
            t = str(ln.get("text") or "").strip()
            if t:
                rows.append(f"{i}. {t}")
    return "\n".join(rows)


def _clean_names(data: dict, counts: dict[str, int]) -> list[dict]:
    """校验人名组：≥2 个不同写法、每个都在这批台词里出现过、standard 取出现最多的。"""
    out: list[dict] = []
    seen: set[frozenset] = set()
    for raw in (data or {}).get("names") or []:
        if not isinstance(raw, dict):
            continue
        variants = []
        for v in (raw.get("variants") or []):
            v = str(v).strip()
            if v and v not in variants and counts.get(v, 0) > 0:
                variants.append(v)
        if len(variants) < 2:
            continue
        key = frozenset(variants)
        if key in seen:
            continue
        seen.add(key)
        ai_std = str(raw.get("standard") or "").strip()

        def rank(v):
            return (counts.get(v, 0), 1 if v == ai_std else 0, len(v))

        standard = max(variants, key=rank)
        out.append({"variants": variants, "standard": standard})
        if len(out) >= MAX_NAME:
            break
    return out


def _ep_of(raw: dict, by_ep: dict[int, list[dict]],
           only_index: int | None) -> int | None:
    """取一条修正所属的集号。

    必须容得下 `"ep": null`（2026-11-19 实测 flash：只送一集时它不知道集号，
    干脆填 null）。旧写法 `int(raw.get("ep"))` 会在这儿抛 TypeError 把整条丢掉，
    而且是在「逐集模式强制归到这一集」之前就炸——界面显示校对完成、实际一条没改。
    顺序：逐集模式直接用传入的集号（模型填什么都不影响）→ 显式 ep →
      整批只有一集时就用那一集 → 其余放弃（多集场景下猜集号会改错别集）。
    """
    if only_index is not None:
        return int(only_index)
    v = raw.get("ep")
    if v is None or v == "":
        return next(iter(by_ep)) if len(by_ep) == 1 else None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _clean_typos(data: dict, by_ep: dict[int, list[dict]],
                 only_index: int | None = None) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[int, int, str]] = set()
    for raw in (data or {}).get("typos") or []:
        if not isinstance(raw, dict):
            continue
        try:
            i = int(raw.get("i"))
        except (TypeError, ValueError):
            continue
        ep = _ep_of(raw, by_ep, only_index)
        if ep is None:
            continue
        lines = by_ep.get(ep)
        if not lines:
            continue
        old = str(raw.get("old") or "").strip()
        new = str(raw.get("new") or "").strip()
        why = str(raw.get("why") or "").strip()[:30]
        # 行号纠偏（2026-11-19 实测加的）：提示词要求 i 从 0 开始，但 deepseek-v4-pro
        # 偶发按「从 1 开始」返回，整批 48 条 old 全部错位一行 → 全被下面的
        # `old not in text` 丢掉，界面却显示「校对完成」，一条都没改。
        # 处理：这一行对不上时，在同一集里找 old，**全剧唯一命中才纠**。
        # 多处命中（同名错字出现在两句）不猜，宁可这条不应用。
        text = str(lines[i].get("text") or "") if 0 <= i < len(lines) else ""
        if old and old not in text:
            hits = [k for k, ln in enumerate(lines) if old in str(ln.get("text") or "")]
            if len(hits) != 1:
                continue
            i = hits[0]
            text = str(lines[i].get("text") or "")
        if not (0 <= i < len(lines)):
            continue
        # typo=同音错字（长度几乎不变）；fix=乱码/漏字多字/断句（长度差可能大）。
        # 两类守卫不同：不给 fix 放宽容差的话，AI 报了「狼栽◆唯独→狼灾，唯独」
        # 也会被本地按 typo 的 4 字容差丢掉（2026-11-19 加）。
        kind = str(raw.get("kind") or "typo").strip().lower()
        if kind not in ("typo", "fix"):
            kind = "typo"
        if not old or old == new or not new or old not in text:
            continue
        if kind == "fix":
            if len(old) > 40:              # 一段就超 40 字，已不是局部修正，防整句重写
                continue
            tol = max(6, len(old) // 2)
            if len(new) > max(len(old) * 2, 30):
                continue
        else:
            tol = min(4, max(2, len(old) // 4))
            if len(new) > len(old) + tol or len(new) < max(1, len(old) - tol):
                continue
        if zh.count(new) > 0:              # 改出了繁体，丢掉
            continue
        key = (ep, i, old)
        if key in seen:
            continue
        seen.add(key)
        out.append({"ep": ep, "i": i, "kind": kind, "old": old, "new": new,
                    "why": why or ("识别修正" if kind == "fix" else "错别字")})
        if len(out) >= MAX_TYPO:
            break
    return out


def _clean_drops(data: dict, by_ep: dict[int, list[dict]],
                 only_index: int | None = None) -> list[dict]:
    """校验「建议删除的行」：行必须存在、必须有台词。

    只校验存在性——「是不是纯杂音」由模型判断，本地没法可靠复算。
    但**绝不自动删**：列进清单给用户勾选，勾了才由 apply 删掉那一整行
    （文字和时间一起没，属于不可逆改动，必须人点头）。
    """
    out: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for raw in (data or {}).get("drops") or []:
        if not isinstance(raw, dict):
            continue
        try:
            i = int(raw.get("i"))
        except (TypeError, ValueError):
            continue
        ep = _ep_of(raw, by_ep, only_index)
        if ep is None:
            continue
        lines = by_ep.get(ep)
        if not lines or not (0 <= i < len(lines)):
            continue
        text = str(lines[i].get("text") or "").strip()
        if not text:                       # 空行不用报，本来就是空的
            continue
        key = (ep, i)
        if key in seen:
            continue
        seen.add(key)
        why = str(raw.get("why") or "").strip()[:30]
        # text 带原文片段，让用户看清要删的是哪一行（截断防清单过长）
        out.append({"ep": ep, "i": i, "text": text[:30],
                    "why": why or "纯杂音行"})
        if len(out) >= MAX_DROP:
            break
    return out


def _run_one(eps: list[dict], only_index: int | None, whole: bool) -> dict:
    """调一次模型，出清单（不写盘）。whole=True 是整剧，否则是单个视频。"""
    counts = _name_counts(eps)
    by_ep = {int(e.get("index") or 0): (e.get("lines") or []) for e in eps}
    data = analyze._call(_prompt(eps, only_index), system=_system(whole, only_index),
                         json_mode=True, model=MODEL)
    res = {
        "names": _clean_names(data, counts),
        "typos": _clean_typos(data, by_ep, only_index),
        "drops": _clean_drops(data, by_ep, only_index),
    }
    # 护栏（2026-11-19 实测）：模型偶发把行号写成从 1 开始，纠偏也救不回来时会
    # 「返回几十条、本地一条不认」。这种批次必须当失败报出来——否则界面显示
    # 「校对完成」、实际一个字没改，用户以为没问题（真金白银还白花）。
    # 阈值定 3：实测 flash 只给 4 条却全部被丢，卡在 5 就静默放过了。
    raw_n = len([r for r in (data.get("typos") or []) if isinstance(r, dict)])
    if raw_n >= 3 and not res["typos"]:
        raise RuntimeError(
            f"模型给了 {raw_n} 条文字修正，但没有一条能和台词对上（行号或原文不符），"
            "这一批结果已作废")
    return res


def suggest(pid: str, ep: int | None = None) -> dict:
    """出校对清单，存项目，不改台词。

    · ep 给定 → 只校对这一集（直出场景前端点「检查这一集」走这里）
    · ep 未给 + 直出项目 → **每个视频各调一次**（用户 2026-11-19 定 B 档）
    · ep 未给 + 主项目 → 整剧一次
    台词没变不重复花钱（命中缓存直接返回）。
    """
    p = core.load(pid)
    if not p:
        raise RuntimeError("项目不存在")
    episodes = p.get("episodes") or []
    if not any((e.get("lines") for e in episodes)):
        raise RuntimeError("还没有台词，先在第 1 步整理台词")

    # ── 指定了某一集：只校这一集 ──
    if ep is not None:
        return _suggest_one(p, int(ep))

    # ── 直出：每个视频单独一次（B 档：全跑）──
    if _direct(p):
        return _suggest_all_direct(p)

    # ── 主项目：整剧一次 ──
    return _suggest_whole(p)


def _suggest_one(p: dict, index: int) -> dict:
    ep = next((e for e in (p.get("episodes") or [])
               if int(e.get("index") or 0) == index), None)
    if not ep:
        raise RuntimeError("项目里没有这一集")
    if not (ep.get("lines") or []):
        raise RuntimeError(f"第 {index} 集还没有台词")
    sig = _ep_sig(ep)
    cur = (p.get("proof") or {})
    by_ep = cur.get("by_ep") or {}
    old = by_ep.get(str(index)) or {}
    if cur.get("mode") == "per_ep" and old and old.get("sig") == sig:
        return dict(old, ep=index, cached=True)

    r = _run_one([ep], index, whole=False)
    item = {"names": r["names"], "typos": r["typos"], "drops": r.get("drops") or [],
            "sig": sig, "ep": index}
    cur = dict(p.get("proof") or {})
    if cur.get("mode") != "per_ep":
        cur = {"mode": "per_ep", "by_ep": {}}
    cur.setdefault("by_ep", {})[str(index)] = item
    p["proof"] = cur
    p.pop("proof_sig", None)
    core.save_merge(p, ["proof"])
    return dict(item, cached=False)


def _suggest_all_direct(p: dict) -> dict:
    """直出：每个视频各调一次模型，逐集存。单集失败不影响其他集。

    并行（2026-11-19 用户要求开）：同时最多 PARALLEL 个视频在调模型。
    分工是硬的——**worker 线程只做「跑模型、把结果返回」这一件纯事**，
    by_ep / done / errors 的修改、save_merge 落盘、进度上报全在收集循环
    （主线程）里做。所以共享的 by_ep 不需要加锁，也不会出现两个线程同时
    save_merge 把对方刚写好的那集抹掉的旧事故。
    """
    eps = [e for e in (p.get("episodes") or []) if e.get("lines")]
    if not eps:
        raise RuntimeError("还没有台词，先在第 1 步整理台词")
    cur = dict(p.get("proof") or {})
    if cur.get("mode") != "per_ep":
        cur = {"mode": "per_ep", "by_ep": {}}
    by_ep = cur.setdefault("by_ep", {})

    total = len(eps)
    done: list[int] = []
    errors: list[str] = []
    todo: list[tuple[dict, int, str]] = []   # (ep, index, sig)
    for ep in eps:
        index = int(ep.get("index") or 0)
        sig = _ep_sig(ep)
        old = by_ep.get(str(index)) or {}
        if old.get("sig") == sig:
            done.append(index)
            continue
        todo.append((ep, index, sig))
    cached_n = total - len(todo)

    def _work(ep: dict, index: int) -> dict:
        # 纯计算：只读 ep、只返回结果，不碰 p / by_ep / 磁盘。
        # 绑调用上下文（2026-11-19）：线程池子线程没有 _CUR，不绑的话
        # analyze._call → ai_post_cancelable 读不到 key，会退化成同步 fn()，
        # 在飞的 AI 请求打不断、中止要等它自然返回。绑了就能即刻打断。
        core.bind_call_ctx(p["id"], "cuts")
        try:
            return _run_one([ep], index, whole=False)
        finally:
            core.bind_call_ctx(None, None)   # 池线程复用，残留会串到下一个任务

    if todo:
        workers = max(1, min(PARALLEL, len(todo)))
        core.set_task(p["id"], "transcribe", 99,
                      f"校对台词（共 {total} 个视频，同时跑 {workers} 个）")
        meta: dict = {}
        it = iter(todo)
        pending: set = set()

        def _submit() -> bool:
            """补一个进窗口。返回 False 表示没有剩下的了。"""
            nxt = next(it, None)
            if nxt is None:
                return False
            ep, index, sig = nxt
            fut = ex.submit(_work, ep, index)
            meta[fut] = (index, sig)
            pending.add(fut)
            return True

        ex = ThreadPoolExecutor(max_workers=workers)
        try:
            # 滑动窗口：只在途 workers 个，不把 50 个请求一次性排满队列
            # ——取消时队列里不该还躺着一堆没人要的活儿。
            core.check_cancel()
            for _ in range(workers):
                if not _submit():
                    break
            while pending:
                finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                # 取消检查点放在主线程：一按中止立刻停手，不再补新任务
                # （已经在飞的 1~2 个请求没法打断，等它们自然结束）
                core.check_cancel()
                for fut in finished:
                    index, sig = meta.pop(fut)
                    try:
                        r = fut.result()
                        by_ep[str(index)] = {"names": r["names"], "typos": r["typos"],
                                             "drops": r.get("drops") or [],
                                             "sig": sig, "ep": index}
                        done.append(index)
                        print(f"[proof] 第 {index} 个视频校对完成：人名 {len(r['names'])} 组，"
                              f"文字修正 {len(r['typos'])} 条，"
                              f"建议删行 {len(r.get('drops') or [])} 条", flush=True)
                    except Exception as exc:  # noqa: BLE001 — 单个视频失败不拖垮整批
                        errors.append(f"第 {index} 个：{exc}")
                        print(f"[proof] 第 {index} 个视频校对失败：{exc}", flush=True)
                    # 每完成一个就存，中途失败/中断时已完成的集不白跑
                    p["proof"] = cur
                    core.save_merge(p, ["proof"])
                    core.set_task(p["id"], "transcribe", 99,
                                  f"校对台词（已完成 {len(done)}/{total} 个视频）")
                # 补满窗口
                while len(pending) < workers:
                    if not _submit():
                        break
        finally:
            # 别 wait：取消时不该为已经发出去的请求再站等一分钟
            ex.shutdown(wait=False, cancel_futures=True)

    p["proof"] = cur
    p.pop("proof_sig", None)
    core.save_merge(p, ["proof"])
    total_names = sum(len((by_ep.get(str(i)) or {}).get("names") or []) for i in done)
    total_typos = sum(len((by_ep.get(str(i)) or {}).get("typos") or []) for i in done)
    total_drops = sum(len((by_ep.get(str(i)) or {}).get("drops") or []) for i in done)
    # 注意键名：这里是**汇总条数（数字）**，不是清单本身。用 name_total/typo_total
    # 是为了不和单集/整剧返回的 names/typos（列表）混淆——同名字段两种类型最容易踩坑。
    return {"mode": "per_ep", "by_ep": by_ep, "episodes": len(done),
            "name_total": total_names, "typo_total": total_typos,
            "drop_total": total_drops,
            "cached": cached_n == len(eps) and bool(eps),
            "errors": errors}


def _ep_chars(ep: dict) -> int:
    """一集台词的字数。分批按字数算，比按集数准：有的集两句、有的集四十句。"""
    return sum(len(str(ln.get("text") or "")) for ln in (ep.get("lines") or []))


def plan_batches(eps: list[dict]) -> list[list[dict]]:
    """整剧校对按**台词字数**分几批送。**量小就不分**——一次看全剧，人名归并最准。

    分批的唯一目的是躲开模型的输出上限（40 集一次实测跑 3 分 57 秒后 400 失败），
    不是为了省时间；所以只要装得下就一批跑完。
    **不按集数**（2026-11-19 用户定）：剧集长短差得多，10 集可能 2500 字也可能 500 字；
    直出场景甚至没有「集」的概念。字数才是真正的输出量。
    """
    if not eps:
        return []
    total = sum(_ep_chars(e) for e in eps)
    if total <= BATCH_MAX_CHARS:
        return [list(eps)]
    out: list[list[dict]] = []
    cur: list[dict] = []
    n = 0
    for e in eps:
        c = _ep_chars(e)
        if cur and n + c > BATCH_MAX_CHARS:
            out.append(cur)
            cur, n = [], 0
        cur.append(e)
        n += c
    if cur:
        out.append(cur)
    # 末批只剩几行字时并回上一批：单独为 200 字跑一次模型不划算，
    # 合并后最多到上限的 1.5 倍（4000 字一批，输出约 60 条），仍在安全区。
    if len(out) >= 2:
        tail = sum(_ep_chars(e) for e in out[-1])
        prev = sum(_ep_chars(e) for e in out[-2])
        if (tail < BATCH_MAX_CHARS * BATCH_MIN_TAIL
                and prev + tail <= BATCH_MAX_CHARS * BATCH_TAIL_FOLD):
            out[-2].extend(out[-1])
            out.pop()
    return out


def _union_by_shared(groups: list[dict]) -> list[list[dict]]:
    """写法有交集的组直接并。同一串出现在两组里，必是同一个人，零风险。"""
    parent = list(range(len(groups)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            si = set(groups[i].get("variants") or [])
            sj = set(groups[j].get("variants") or [])
            if si & sj:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri
    buckets: dict[int, list[dict]] = {}
    for i, g in enumerate(groups):
        buckets.setdefault(find(i), []).append(g)
    return list(buckets.values())


def _ai_merge_names(buckets: list[list[dict]]) -> list[list[dict]]:
    """跨批人名归并：把分组送模型确认一次「哪些其实是同一个人」。

    **只要有 2 组以上就送**，不在本地做「长得像」预筛——同一个人被写成不同字，
    首字常常就是不同的（陆征 / 路征），本地判据要么漏、要么把「张三 / 张四」
    这种不同角色也送进去。输入只有名字、输出只有组号，几秒回来，不会被截断。

    失败或格式不对就原样返回——**宁可不合并，也不能把两个角色错并成一个人**，
    那会把台词里的名字全改错。
    """
    if len(buckets) < 2:
        return buckets
    vs: list[list[str]] = []
    for bucket in buckets:
        names: list[str] = []
        for g in bucket:
            for v in g.get("variants") or []:
                v = str(v).strip()
                if v and v not in names:
                    names.append(v)
        vs.append(names)
    lines = [f"{i}: {' / '.join(v)}" for i, v in enumerate(vs)]
    system = (
        "你在校对一部剧的台词，任务是把「同一个人被写成不同字」的人名组合并。\n"
        "规则：只合并确实指同一个人的组；不同角色即使字形相近（张三 / 张四）、"
        "读音相近也绝不合并；拿不准就不合并。\n"
        '只输出 JSON：{"groups": [[组号, ...], ...]}，每个组号必须出现且只出现一次。'
    )
    try:
        data = analyze._call("\n".join(lines), system=system, json_mode=True, model=MODEL)
    except Exception as exc:  # noqa: BLE001
        print(f"[proof] 跨批人名归并没做成，保留原分组：{exc}", flush=True)
        return buckets
    raw = (data or {}).get("groups")
    if not isinstance(raw, list) or not raw:
        return buckets
    used: set[int] = set()
    out: list[list[dict]] = []
    for grp in raw:
        if not isinstance(grp, list) or not grp:
            return buckets
        idxs: list[int] = []
        for v in grp:
            try:
                k = int(v)
            except (TypeError, ValueError):
                return buckets
            if not (0 <= k < len(buckets)) or k in used:
                return buckets          # 组号越界/重复 → 整份不采信
            used.add(k)
            idxs.append(k)
        out.append([g for k in idxs for g in buckets[k]])
    if len(used) != len(buckets):        # 有组没被提到 = 输出不全，不采信
        return buckets
    return out


def _merge_name_groups(groups: list[dict], eps: list[dict], cross: bool = True) -> list[dict]:
    """把各批的人名组合成一份：写法交集先硬并 → 跨批时让模型确认同人 → 定标准写法。

    cross=False（只有一批）时不再调模型：那一批内部已经由模型自己归并过了。
    """
    groups = [g for g in (groups or []) if isinstance(g, dict) and g.get("variants")]
    if not groups:
        return []
    buckets = _union_by_shared(groups)
    if cross:
        buckets = _ai_merge_names(buckets)
    counts = _name_counts(eps)
    out: list[dict] = []
    seen: set[frozenset] = set()
    for bucket in buckets:
        variants: list[str] = []
        for g in bucket:
            for v in g.get("variants") or []:
                v = str(v).strip()
                if v and v not in variants:
                    variants.append(v)
        if len(variants) < 2:
            continue
        key = frozenset(variants)
        if key in seen:
            continue
        seen.add(key)
        # 标准写法仍是「出现次数最多」的那个（用户 2026-11-19 定的口径）
        out.append({"variants": variants,
                    "standard": max(variants, key=lambda v: (counts.get(v, 0), len(v)))})
        if len(out) >= MAX_NAME_ALL:
            break
    return out


def _merge_typos(parts: list[list[dict]]) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple] = set()
    for lst in parts:
        for it in lst or []:
            key = (it.get("ep"), it.get("i"), it.get("old"))
            if key in seen:
                continue
            seen.add(key)
            out.append(it)
            if len(out) >= MAX_TYPO:
                return out
    return out


def _merge_drops(parts: list[list[dict]]) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple] = set()
    for lst in parts:
        for it in lst or []:
            key = (it.get("ep"), it.get("i"))
            if key in seen:
                continue
            seen.add(key)
            out.append(it)
            if len(out) >= MAX_DROP:
                return out
    return out


def _suggest_whole(p: dict) -> dict:
    eps = [e for e in (p.get("episodes") or [])[:MAX_EPISODES] if e.get("lines")]
    if not eps:
        raise RuntimeError("还没有台词，先在第 1 步整理台词")
    sig = all_sig(p)
    cur = p.get("proof") or {}
    if p.get("proof_sig") == sig and _is_whole(cur):
        return dict(cur, cached=True)

    batches = plan_batches(eps)
    names: list[dict] = []
    typo_parts: list[list[dict]] = []
    drop_parts: list[list[dict]] = []
    errs: list[str] = []
    for k, group in enumerate(batches):
        first, last = group[0].get("index"), group[-1].get("index")
        t0 = time.time()
        try:
            r = _run_one(group, None, whole=True)
        except Exception as exc:  # noqa: BLE001 — 一批失败不能拖垮整份
            errs.append(f"第 {k + 1} 批（第 {first}-{last} 集）没成：{exc}")
            print(f"[proof] 第 {k + 1}/{len(batches)} 批失败：{exc}", flush=True)
            continue
        names.extend(r["names"])
        typo_parts.append(r["typos"])
        drop_parts.append(r.get("drops") or [])
        print(f"[proof] 第 {k + 1}/{len(batches)} 批（第 {first}-{last} 集，{len(group)} 集）"
              f"完成：人名 {len(r['names'])} 组、错字 {len(r['typos'])} 条，"
              f"耗时 {time.time() - t0:.1f}s", flush=True)

    typos = _merge_typos(typo_parts)
    drops = _merge_drops(drop_parts)
    if errs and not names and not typos and not drops:
        raise RuntimeError(errs[0])

    names = _merge_name_groups(names, eps, cross=len(batches) > 1)
    result = {
        "mode": "whole",
        "names": names,
        "typos": typos,
        "drops": drops,
        "sig": sig,
        "episodes": len(eps),
        "batches": len(batches),
    }
    if errs:
        result["errors"] = errs
    p["proof"] = result
    if errs:
        # 有批次失败 → 不写指纹，下次点「校对台词」整体重跑，别把残缺清单当有效缓存
        p.pop("proof_sig", None)
        core.save_merge(p, ["proof"])
    else:
        p["proof_sig"] = sig
        core.save_merge(p, ["proof", "proof_sig"])
    return dict(result, cached=False)


# ── 写回 ──────────────────────────────────────────────────────────────────

def _replace_names(eps: list[dict], groups: list[dict], picks: list,
                   standards: dict | None = None, log: list | None = None) -> int:
    """按勾选的人名组，把 variants 统一成 standard。长写法先替换，避免切半。

    standards 是界面上用户改过的标准写法（{组索引字符串: 新写法}），优先于 AI 推荐。
    只动传入的 eps，别的集/别的视频不受影响。
    log 给了就把每一处改动的**整句前后**追加进去（用户要能查、能反悔）。
    """
    standards = standards or {}
    chosen = []
    for k in picks or []:
        try:
            g = groups[int(k)]
        except (TypeError, ValueError, IndexError):
            continue
        if not isinstance(g, dict):
            continue
        std = str(standards.get(str(int(k))) or "").strip() or str(g.get("standard") or "")
        if not std:
            continue
        pairs = [(v, std) for v in g.get("variants") or []
                 if v and v != std]
        pairs.sort(key=lambda x: -len(x[0]))
        chosen.append(pairs)
    if not chosen:
        return 0
    n = 0
    for ep in eps:
        for i, ln in enumerate(ep.get("lines") or []):
            t = str(ln.get("text") or "")
            if not t:
                continue
            changed = t
            for pairs in chosen:
                for old, new in pairs:
                    changed = changed.replace(old, new)
            if changed != t:
                ln["text"] = changed
                n += 1
                if log is not None:
                    log.append({"kind": "name", "ep": int(ep.get("index") or 0),
                                "i": i, "before": t, "after": changed})
    return n


def _apply_typos(eps: list[dict], items: list[dict], picks: list,
                 log: list | None = None) -> int:
    by_ep = {int(e.get("index") or 0): e for e in eps}
    n = 0
    for k in picks or []:
        try:
            it = items[int(k)]
        except (TypeError, ValueError, IndexError):
            continue
        if not isinstance(it, dict):
            continue
        ep = by_ep.get(int(it.get("ep") or -1))
        lines = (ep.get("lines") or []) if ep else []
        try:
            i = int(it["i"])       # 不写 `or -1`：i=0（第 1 句）会被 0 or -1 吃掉
        except (KeyError, TypeError, ValueError):
            continue
        old, new = str(it.get("old") or ""), str(it.get("new") or "")
        if not (0 <= i < len(lines)) or not old or old == new:
            continue
        text = str(lines[i].get("text") or "")
        if old not in text:
            continue
        after = text.replace(old, new, 1)
        lines[i]["text"] = after
        n += 1
        if log is not None:
            log.append({"kind": str(it.get("kind") or "typo"),
                        "ep": int(it.get("ep") or 0), "i": i,
                        "old": old, "new": new, "before": text, "after": after})
    return n


def _apply_drops(eps: list[dict], items: list[dict], picks: list) -> int:
    """删掉勾选的整行（文字和时间码一起没）。返回删了几行。

    **必须在文字替换之后调用**：先删行会让后面 typo 的行号整体前移，
    「第 8 行」的修改会打到第 7 行上。同一集内还要**从后往前删**，
    否则删掉第 3 行后原来的第 5 行变成第 4 行，再按 5 删就删错了。

    删完清掉该集的 lines_time_bak / lines_time_sig：行都没了，对时备份里
    还留着这些行，之后点「撤销对时」会把删掉的句子又恢复出来。
    """
    by_ep = {int(e.get("index") or 0): e for e in eps}
    todo: dict[int, set[int]] = {}
    for k in picks or []:
        try:
            it = items[int(k)]
        except (TypeError, ValueError, IndexError):
            continue
        if not isinstance(it, dict):
            continue
        try:
            ep = int(it.get("ep"))
            i = int(it["i"])
        except (KeyError, TypeError, ValueError):
            continue
        todo.setdefault(ep, set()).add(i)
    n = 0
    for ep_index, idxs in todo.items():
        e = by_ep.get(ep_index)
        if not e:
            continue
        lines = e.get("lines") or []
        removed = 0
        for i in sorted(idxs, reverse=True):
            if 0 <= i < len(lines):
                lines.pop(i)
                removed += 1
        if removed:
            e.pop("lines_time_bak", None)
            e.pop("lines_time_sig", None)
            n += removed
    return n


def apply(pid: str, name_picks: list | None = None,
          typo_picks: list | None = None, standards: dict | None = None,
          ep: int | None = None, drop_picks: list | None = None) -> dict:
    """把勾选的改动写回台词。改字不动时间码；删行会连时间码一起删。

    · ep 给定 → 只改这一集（用该集的清单）
    · ep 未给 + 直出项目 → 逐集套用各自清单（每集只用自己那份，绝不跨集）
    · ep 未给 + 主项目 → 整剧清单
    """
    p = core.load(pid)
    if not p:
        raise RuntimeError("项目不存在")

    if ep is not None:
        return _apply_one(p, int(ep), name_picks, typo_picks, standards, drop_picks)

    if _direct(p):
        return _apply_all_direct(p)

    # 整剧
    cur = p.get("proof") or {}
    if not _is_whole(cur):
        raise RuntimeError("还没有检查清单，请先检查一次")
    if p.get("proof_sig") != all_sig(p):
        raise RuntimeError("台词变过了，旧的检查清单已作废，请重新检查一次")
    eps = p.get("episodes") or []
    n_name = _replace_names(eps, cur.get("names") or [], name_picks or [], standards)
    n_typo = _apply_typos(eps, cur.get("typos") or [], typo_picks or [])
    # 删行放最后：先删会让上面按旧行号的修改全部错位
    n_drop = _apply_drops(eps, cur.get("drops") or [], drop_picks or [])
    p["proof"] = {"mode": "whole", "names": [], "typos": [], "drops": []}
    p["proof_sig"] = all_sig(p)
    if n_name or n_typo or n_drop:
        core.save_merge(p, ["episodes", "proof", "proof_sig"])
    return {"names": n_name, "typos": n_typo, "dropped": n_drop}


def _apply_one(p: dict, index: int, name_picks, typo_picks, standards,
               drop_picks=None) -> dict:
    eps = p.get("episodes") or []
    ep = next((e for e in eps if int(e.get("index") or 0) == index), None)
    if not ep:
        raise RuntimeError("项目里没有这一集")
    cur = p.get("proof") or {}
    if cur.get("mode") != "per_ep":
        raise RuntimeError("还没有检查清单，请先检查这一集")
    item = (cur.get("by_ep") or {}).get(str(index)) or {}
    if not item:
        raise RuntimeError(f"第 {index} 集还没有检查清单，请先检查")
    if item.get("sig") != _ep_sig(ep):
        raise RuntimeError("这一集的台词变过了，旧的检查清单已作废，请重新检查")

    n_name = _replace_names([ep], item.get("names") or [], name_picks or [], standards)
    n_typo = _apply_typos([ep], item.get("typos") or [], typo_picks or [])
    n_drop = _apply_drops([ep], item.get("drops") or [], drop_picks or [])
    by_ep = cur.setdefault("by_ep", {})
    by_ep.pop(str(index), None)      # 这集的清单用完即清
    p["proof"] = cur
    if n_name or n_typo or n_drop:
        core.save_merge(p, ["episodes", "proof"])
    else:
        core.save_merge(p, ["proof"])
    return {"names": n_name, "typos": n_typo, "dropped": n_drop}


def _apply_all_direct(p: dict) -> dict:
    """直出不指定集：每集只用自己那份清单，逐集应用。绝不把一集的替换用到别的集。

    **不自动删行**：删除不可逆（文字和时间码一起没），必须用户在清单里逐个勾。
    这条路没有逐条勾选的信息，所以只应用人名和文字修正，drops 留在清单里等人确认。
    """
    cur = p.get("proof") or {}
    if cur.get("mode") != "per_ep":
        raise RuntimeError("还没有检查清单，请先检查")
    by_ep = cur.get("by_ep") or {}
    if not by_ep:
        raise RuntimeError("还没有检查清单，请先检查")
    episodes = {int(e.get("index") or 0): e for e in (p.get("episodes") or [])}
    total_name = total_typo = total_drop = 0
    for key, item in list(by_ep.items()):
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        ep = episodes.get(index)
        if not ep or not isinstance(item, dict):
            continue
        if item.get("sig") != _ep_sig(ep):
            continue                     # 台词变过，这份清单作废，跳过
        total_name += _replace_names([ep], item.get("names") or [], list(range(len(item.get("names") or []))), None)
        total_typo += _apply_typos([ep], item.get("typos") or [], list(range(len(item.get("typos") or []))))
        by_ep.pop(key, None)
    p["proof"] = cur
    if total_name or total_typo or total_drop:
        core.save_merge(p, ["episodes", "proof"])
    return {"names": total_name, "typos": total_typo, "dropped": total_drop}


# 自动写回的范围（2026-11-19 用户第二次调整）：**人名统一 + 全部文字修正**。
# 最初只自动改 kind=typo（同音错字），fix（乱码/漏字多字/断句语病）留给人勾；
# 实测 40 集里 fix 只有 12 条、每条都要人点一次，用户明确要求「默认自动改，无需审核」。
# 仍然自动的只有「删行」不碰：删行会连时间码一起没、不可逆，留在清单等人勾。
# fix 的长度守卫早在 _clean_typos 里卡过（old≤40、new≤max(2×old,30)），不会整段重写。
def auto_apply(pid: str) -> dict:
    """整理台词后自动写回：人名统一 + 全部文字修正（typo/fix）。返回 {names, typos, left}。

    改完**不清空清单**：把已经改掉的条目剔掉，剩下的（只有删行）留着，
    并按改后的台词重算指纹——否则清单立刻作废，用户点「校对台词」等于再花一次
    额度重新算一遍。返回的 left 是还需要人工确认的条数（给状态提示用）。
    每处改动的整句前后记在 proof["auto"]["items"]，界面可展开查看。
    """
    p = core.load(pid)
    if not p:
        return {"names": 0, "typos": 0, "left": 0}
    if _direct(p):
        return _auto_all_direct(p)
    cur = dict(p.get("proof") or {})
    if not _is_whole(cur) or p.get("proof_sig") != all_sig(p):
        return {"names": 0, "typos": 0, "left": 0}
    eps = p.get("episodes") or []
    groups = cur.get("names") or []
    typos = cur.get("typos") or []
    drops = cur.get("drops") or []
    # 全部文字修正都自动写回（typo + fix）；只剩「删行」要人点头。
    picks = list(range(len(typos)))
    log: list = []
    n_name = _replace_names(eps, groups, list(range(len(groups))), None, log)
    n_typo = _apply_typos(eps, typos, picks, log)
    if not (n_name or n_typo):
        return {"names": 0, "typos": 0, "left": len(typos) + len(drops)}
    keep = [it for k, it in enumerate(typos) if k not in set(picks)]
    # 计数与明细**累加**上一次：同一次整理可能因为分批/分步调用 auto_apply 多次，
    # 覆盖会把「这轮一共改了多少」算成最后一批的量（用户看到 12 而不是 232）。
    # 重新核对台词时会写一份不带 auto 的新清单，旧统计自然清空，不会跨清单污染。
    prev = cur.get("auto") or {}
    items = list(prev.get("items") or []) + log
    after = all_sig(p)
    p["proof"] = {"mode": "whole", "names": [], "typos": keep, "drops": drops,
                  "sig": after,
                  "auto": {"names": int(prev.get("names") or 0) + n_name,
                           "typos": int(prev.get("typos") or 0) + n_typo,
                           "items": items[-AUTO_LOG_MAX:]}}
    p["proof_sig"] = after
    core.save_merge(p, ["episodes", "proof", "proof_sig"])
    return {"names": n_name, "typos": n_typo, "left": len(keep) + len(drops)}


def _auto_all_direct(p: dict) -> dict:
    """直出：每个视频各按自己那份清单自动写回，绝不跨视频套用。"""
    cur = dict(p.get("proof") or {})
    by_ep = dict(cur.get("by_ep") or {})
    episodes = {int(e.get("index") or 0): e for e in (p.get("episodes") or [])}
    n_name = n_typo = n_left = 0
    changed = False
    for key, item in list(by_ep.items()):
        if not isinstance(item, dict):
            continue
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        ep = episodes.get(index)
        if not ep or item.get("sig") != _ep_sig(ep):
            continue                    # 台词变过，这份清单作废，不自作主张

        groups = item.get("names") or []
        typos = item.get("typos") or []
        drops = item.get("drops") or []
        picks = list(range(len(typos)))   # 同整剧：typo + fix 都自动改
        log: list = []
        got_name = _replace_names([ep], groups, list(range(len(groups))), None, log)
        got_typo = _apply_typos([ep], typos, picks, log)
        n_name += got_name
        n_typo += got_typo
        if not (got_name or got_typo):
            n_left += len(typos) + len(drops)
            continue
        keep = [it for k, it in enumerate(typos) if k not in set(picks)]
        prev = item.get("auto") or {}
        by_ep[key] = {"names": [], "typos": keep, "drops": drops,
                      "sig": _ep_sig(ep), "ep": index,
                      "auto": {"names": int(prev.get("names") or 0) + got_name,
                               "typos": int(prev.get("typos") or 0) + got_typo,
                               "items": (list(prev.get("items") or []) + log)[-AUTO_LOG_MAX:]}}
        n_left += len(keep) + len(drops)
        changed = True
    if changed:
        cur["by_ep"] = by_ep
        p["proof"] = cur
        core.save_merge(p, ["episodes", "proof"])
    return {"names": n_name, "typos": n_typo, "left": n_left}


def read(pid: str, ep: int | None = None) -> dict:
    """只读已存清单（打开弹窗用），绝不触发 AI。

    · ep 给定 → 返回该集清单（直出场景切集时调）
    · 否则按项目类型：整剧返回 names/typos；直出返回各集摘要
    """
    p = core.load(pid)
    if not p:
        return {"mode": "", "names": [], "typos": [], "drops": [],
                "valid": False, "by_ep": {}}
    cur = p.get("proof") or {}
    mode = cur.get("mode") or ""

    if ep is not None:
        item = (cur.get("by_ep") or {}).get(str(int(ep))) or {}
        e = next((x for x in (p.get("episodes") or [])
                  if int(x.get("index") or 0) == int(ep)), None)
        ok = bool(item and e and item.get("sig") == _ep_sig(e))
        return {"mode": mode, "ep": int(ep),
                "names": (item.get("names") or []) if ok else [],
                "typos": (item.get("typos") or []) if ok else [],
                "drops": (item.get("drops") or []) if ok else [],
                "auto": (item.get("auto") or {}) if ok else {},
                "valid": bool(ok and (item.get("names") or item.get("typos")
                                      or item.get("drops"))),
                "by_ep": {}}

    if mode == "per_ep":
        brief = {}
        for k, item in (cur.get("by_ep") or {}).items():
            e = next((x for x in (p.get("episodes") or [])
                      if str(x.get("index")) == str(k)), None)
            ok = bool(e and isinstance(item, dict) and item.get("sig") == _ep_sig(e))
            brief[str(k)] = {
                "names": len(item.get("names") or []) if ok else 0,
                "typos": len(item.get("typos") or []) if ok else 0,
                "drops": len(item.get("drops") or []) if ok else 0,
                "valid": bool(ok and (item.get("names") or item.get("typos")
                                      or item.get("drops"))),
            }
        return {"mode": mode, "names": [], "typos": [], "drops": [],
                "valid": False, "by_ep": brief}

    sig_ok = p.get("proof_sig") == all_sig(p)
    names = (cur.get("names") or []) if sig_ok else []
    typos = (cur.get("typos") or []) if sig_ok else []
    drops = (cur.get("drops") or []) if sig_ok else []
    return {"mode": "whole", "names": names, "typos": typos, "drops": drops,
            "auto": (cur.get("auto") or {}) if sig_ok else {},
            "valid": bool(sig_ok and (names or typos or drops)), "by_ep": {}}


def clear(pid: str) -> None:
    p = core.load(pid)
    if not p:
        return
    p["proof"] = {}
    p["proof_sig"] = ""
    core.save_merge(p, ["proof", "proof_sig"])
