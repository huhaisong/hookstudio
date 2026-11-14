"""第 3 步：镜号提示词——把全剧台词当剧本，重新设计 15/30/60 秒引流短片的分镜提示词。

2026-11-03 用户定调：引流分镜不是「从原片台词里挑高光段」，而是「读完剧重新写分镜」。

旧版（选段式，合并 hook-studio 原型时的实现）把内容锁在原片真实高光上：必须同一集、
一场连续戏、时间码来自原片——写出来更像「把某几段戏重演一遍」，不是一条新的引流片。
现在改为创作式：

- 台词只当剧情素材；时间码是本条成片的秒数，不对应原片任何一句；
- 允许改写台词、补旁白、设计原片没有的镜头，改动必须进 additions 交代；
- 素材没写明的细节可以补，补到能连成一条看得懂、生成得出来的片子；
  已写明的人物关系与关键事实不改，一条片里别前后矛盾；
- 每条拆成单镜 ≤5 秒的镜头，适配图生视频模型一次能出的长度
  （2026-11-08 用户定：上限从 8 秒收到 5 秒，与出片模型一次能出的最长时长对齐）。

2026-11-03 又定两条：
- **音色锁定**：A/B/C/D 四段最后要剪成一条片，同一个人的声音不能一段一个样。
  所以声线拆成两层——镜号级 `voices[].voice` 只写「音色锚点」（音高/音质/口音/基线音量
  语速/尾音习惯），**同一条片（同一镜号）内**同一人只留一份；段级只补 `tone`
  （本段情绪+语速+重音落点）。音色由程序原样带入，模型改不动。
  2026-11-08 用户明确：镜号 = 一条独立成片，**不同镜号可以有不同音色**，
  锁定只在条内做，绝不跨镜号互相覆盖（见 `_unify_voices`）。
- **套与套独立**（2026-11-08 用户定）：count 套 = count 次独立请求，各自出各自的人
  物卡 / 音色 / 场景 / 分镜，并回时只按套顺序拼接，**任何字段都不许跨套去重、覆盖、
  取第一份**（人物卡曾按名字全局去重，第 2 套的设定被第 1 套顶掉，已修）。
  跨套只共用整篇文档的标题（doc.title）和用户在界面上设的画风/画幅——那是整份文件
  级参数，不属于某一套。
- **素材来源可换**：默认读本剧 Whisper 台词；也可以调用方直接给一段剧情素材
  （大纲/小说/口述稿），此时不读转写、也不要求项目里有转写结果。

数字类校验（总时长、镜头数、台词密度、最长无台词段、单镜时长）由 stats() 算，
不指望模型自报。
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import time
from datetime import date
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import analyze, core

CN = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]


def shots_model() -> str | None:
    """第 3 步单独配的模型；留空 = 跟第 2 步用同一个（llm_model）。"""
    sm = (core.load_config().get("shots_model") or "").strip()
    return sm or None

DEFAULT_STYLE = ("真人写实风格，4K 高清，电影级光影，细腻皮肤质感，自然人物动作，"
                 "{aspect}，画面干净，色调统一，精致高级")

# 第 3 步多条分镜的并发路数不再写死：每条独立成一趟请求并联发出（见 shots_run），
# 上限由 core.max_concurrent() 现读设置页的「同时生成几条」，与第 2 步共用同一档
# ——上游是同一个账号，分开调会叠成双倍并发。2026-11-08 前这里恒为 3。
# 失败的那条走错峰串行重试兜底，不至于整批卡死。

# 写进文档的三句固定短句：控制句 / 每镜要求 / 合规声明。
# 2026-11-08 用户要求「所有提示词都暴露出来、能自己改」，这三句原来写死在代码里，
# 现在从 prompts/shots_rules.txt 按 `## 段名` 读（用户改的字现读现生效）。
# 语义约定：**整段删掉 = 恢复内置那句；段留着但内容清空 = 主动不要这句**。
_RULES_DEFAULT = {
    # 对话式生成器才需要：那种模型会反问规格、把一次生成拆成几次任务。
    # 走 API / 工作流传参时模型不会反问，这几行纯属噪声，所以由开关控制
    # （p["shots"]["ctl"]），默认勾选。
    "控制句": (
        "【直接生成控制】按下面的内容直接生成视频：不要反问，不要让我确认模型 / 比例 / 时长，"
        "不要列举可选规格；这是一次连续生成，不要拆成多次任务，只回一句「已开始生成」。"
    ),
    # 不生成画面内文字：这是**能被模型执行**的硬要求（不写就会被烧字），
    # 所以每段都带，不放在可选的控制句里——关掉控制句不该连带丢掉它。
    "每镜要求": "画面中不要出现任何文字（标题、水印、字幕卡都不要）。",
    # 合规声明：不改变任何一帧，只是给平台审核 / 客户看的。放文档头一次就够，
    # 塞进每段只是白占 token。
    "合规声明": (
        "全程使用 AI 生成虚拟参考图、无真实人脸素材，规避真人肖像相关版权与隐私风险，"
        "画面人物均为原创虚拟形象。"
    ),
}


def _rules() -> dict:
    """读 prompts/shots_rules.txt（`## 段名` 分段），缺段用内置默认。"""
    out = dict(_RULES_DEFAULT)
    try:
        text = analyze._read_prompt("shots_rules.txt")
    except RuntimeError:
        return out
    found, cur, buf = {}, None, []
    for line in text.splitlines():
        m = re.match(r"^##\s*(.+?)\s*$", line)
        if m:
            if cur:
                found[cur] = "\n".join(buf).strip()
            cur, buf = m.group(1).strip(), []
        elif cur is not None:
            buf.append(line)
    if cur:
        found[cur] = "\n".join(buf).strip()
    for key in out:
        if key in found:
            out[key] = found[key]
    return out

# 「节奏骨架：钩子 → 升级 → 反杀 → 再钩」+ 每段该写多少字，曾经写死在
# BUDGETS 里逐段注入（2026-11-03 为「台词偏少」打的补丁）。2026-11-07 用户定：
# 根本原则之外不限制 AI 自由发挥，整段删掉。现在只给程序算得出的可数尺子
# （整条时长 / 镜数 / 总字数 / 单镜上限 / 分段切点），台词是否匀由 stats() 的
# 密度、无台词段预警兜底，不再替模型规定每段演什么、说多少字。
#
# 镜头数区间与单镜上限：
# 区间是按「台词写满」倒算的：60 秒 215-240 字、每镜 15 字左右 → 每镜约 3.8 秒 → 15-16 镜。
# （2026-11-03 用户要求「台词多一点、内容紧凑」；只给总字数模型会写成 13 短镜 × 10 字）
SHOT_RANGE = {15: (4, 5), 30: (8, 10), 60: (14, 18)}
# 单镜上限 **5 秒**（2026-11-08 用户定：出片模型一次最长只出 5 秒，8 秒的镜头要二次裁）。
# 收到 5 秒后有一个连带的硬约束：5 秒 ≈ 22 字（4.5 字/秒），所以一句台词最多 20 字，
# 超了必然被 `_unpack_long` 拆开——拆出来的第二镜是「同机位续拍」，口型和机位都得重调，
# 不如一开始就写短。这个字数上限要在提示词里明确给出，不能只给秒数（模型算不出换算）。
MAX_SHOT = 5.0
MIN_SHOT = 1.5
LINE_MAX_CHARS = 20

# 台词字数目标（约每秒 4.17 字）——直接给模型一个能数的数，
# 写「占比 50%-70%」它算不出来（规律 12：百分比要换算成具体数字）。
# CHARS_PER_SEC=4.5 是口播满速，预算取它的 ~93%：每镜 3.75 秒里留 0.25-0.3 秒给动作。
# 历史：2026-11-03 用户反馈「台词偏少」，把原 65% 提到 ~89%；2026-11-08 用户要求
# 再 +10%（实测 6 条 60s 产物均 228 字 / 3.79 字每秒，每镜剩 0.65 秒太空）。
CHARS_PER_SEC = 4.5
# 每镜 15-16 字 × 3.75 秒：15 秒 4 镜=62、30 秒 8 镜=125、60 秒 16 镜=250
WORD_BUDGET = {15: 62, 30: 125, 60: 250}

# 输出格式（JSON 骨架）：2026-11-08 用户要求所有提示词都能改，这里原来是硬编码常量，
# 现在从 prompts/shots_schema.txt 现读（用户改的字下一次生成就生效）。
# __DUR__ 是「整条时长」的占位符：示例里的数字就是规格，写死 60 会让选 30 秒的片子
# 照抄 60 秒的值。当前 schema 已经不写死任何时长，替换成了空转，留着是给用户改回
# 带时间窗的写法时不至于又踩坑。
def _schema(duration: int) -> str:
    return analyze._read_prompt("shots_schema.txt").replace("__DUR__", str(duration))


# ---------------------------------------------------------------- 校验/规范化

def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value, default=0):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _s(value, default=""):
    return default if value is None else str(value).strip()


# 音色锚点里不该出现的两样东西：重音词、本集情绪。重音词一旦写进音色，分段后每段
# 都会挂着一个本段没说的词，配音和字幕全对不上；情绪则应该跟着镜头走，不该钉死人。
_STRESS_CLAUSE = re.compile(r"[+、，]?\s*重音[^+、，]{0,4}?[‘'\"「『][^’'\"」』]{1,12}[’'\"」』]")
_QUOTED_ANY = re.compile(r"[‘'\"「『][^’'\"」』]{1,12}[’'\"」』]")


def _timbre(text: str) -> str:
    """把模型写的声线整串压成「纯音色锚点」——去掉重音词和引号，只留音色本体。

    模型会把 `低频气声+中等音量+重音咬'契约'‘清算’+尾音极短` 整串塞进音色，
    分段时这段的音色就带着别的段的词。这里剥干净，重音改由每段的 tone 承担。
    """
    t = (text or "").strip()
    t = _STRESS_CLAUSE.sub("", t)
    t = _QUOTED_ANY.sub("", t)          # 残留的「'已发'」「'三十天'」这类裸引号词
    t = t.replace("重音", "")
    t = re.sub(r"[+、]\s*[+、]", "+", t)
    t = re.sub(r"^[+、\s,，;；]+|[+、\s,，;；]+$", "", t)
    # 模型常在句尾多留一个右引号（“...虚浮上扬”），剥掉
    t = t.strip(" 　+、,，;；").rstrip("\"”’」』")
    return t.strip()[:80]


def _unify_voices(episodes: list) -> list:
    """**同一条片（一个镜号）内部**同一个人的音色只留一份，以该条里首次出现的版本为准。

    作用域只到镜号，不跨镜号（2026-11-08 用户定：镜号 = 一条独立成片，
    各条可以有自己的配音人选；只有同一条拆出来的 A/B/C/D 四段最后要拼成
    一个视频，音色必须字字相同）。段与段本来就读同一份 `ep["voices"]`，
    所以这里真正要处理的只有「同一条里同一个人写了两版」这种情况。

    返回被改掉的清单，交给 warnings 如实报出来。
    """
    notes = []
    for ep in episodes:
        canon = {}                    # ← 每条片各自一份，不跨镜号共用
        fixed = []
        for v in ep.get("voices") or []:
            name = (v.get("name") or "").strip()
            text = _timbre(v.get("timbre") or v.get("voice") or "")
            if not name or not text:
                continue
            if name in canon:
                if text != canon[name]:
                    notes.append((name, ep.get("no"), canon[name], text))
                text = canon[name]
            else:
                canon[name] = text
            fixed.append({"name": name, "voice": text})
        ep["voices"] = fixed
    return notes


def _voice_text(v: dict) -> str:
    """段级声线：**只给音色锚点**。

    语气 / 重音不写在这里——它们已经在每句台词前的括号里（`沈清瓷说（低沉偏冷、
    重音落'清算'）"…"`）。同一件事写两遍：① 占篇幅；② 两处描述可能不一致，
    模型遇到冲突只能二选一，出来是什么样不可控。就近的那一处（台词括号）更有效。
    """
    timbre = _timbre((v.get("timbre") or v.get("voice") or "").strip())
    return f"音色：{timbre}" if timbre else ""


def _rescale(shots, duration):
    """把每个镜头的时间码校准到 0—duration（模型偶尔给 0—50 却要 60 秒）。

    时间码就是本条成片的秒数，不是原片时间——所以这里只做整体缩放，不做来源校验。
    """
    shots = [s for s in shots if s["t_end"] > s["t_start"]]
    if not shots:
        return shots
    end_max = max(s["t_end"] for s in shots)
    factor = 1.0
    if end_max > 0 and abs(end_max - duration) > 0.6:
        factor = duration / end_max
    for seg in shots:
        seg["t_start"] = round(max(0.0, seg["t_start"] * factor), 1)
        seg["t_end"] = round(max(0.0, seg["t_end"] * factor), 1)
    shots.sort(key=lambda s: s["t_start"])
    shots[0]["t_start"] = 0.0
    for seg in shots:
        if seg["t_end"] <= seg["t_start"]:
            seg["t_end"] = round(seg["t_start"] + MIN_SHOT, 1)
    shots[-1]["t_end"] = float(duration)
    # 前后压叠只往里收起点，不挪终点（终点是节奏，挪了总时长就变）
    for prev, cur in zip(shots, shots[1:]):
        if cur["t_start"] < prev["t_end"]:
            cur["t_start"] = prev["t_end"]
        if cur["t_end"] <= cur["t_start"]:
            cur["t_end"] = round(min(duration, cur["t_start"] + MIN_SHOT), 1)
    return shots


_PUNCT = "，。！？；、…—"

def _split_line(line: str, parts: int) -> list:
    """把一句长台词切成 parts 段，优先在标点后断，实在断不开才按字数硬切。"""
    line = (line or "").strip()
    if parts <= 1 or len(line) < parts * 4:
        return [line] if line else []
    chunks, buf = [], ""
    for i, ch in enumerate(line):
        buf += ch
        # 连续标点（——、……）要归到同一处，不然会把破折号劈成两半
        nxt = line[i + 1] if i + 1 < len(line) else ""
        if ch in _PUNCT and nxt not in _PUNCT:
            chunks.append(buf)
            buf = ""
    if buf:
        chunks.append(buf)

    if len(chunks) < parts:                    # 标点不够，按字数均分
        size = len(line) / parts
        out, i = [], 0
        while i < len(line) and len(out) < parts - 1:
            cut = min(len(line), int(round(i + size)))
            out.append(line[i:cut])
            i = cut
        out.append(line[i:])
        return [x for x in out if x]

    # 标点够：在「累计字数最接近均分点」的句子边界处切，句子不劈开
    cum, acc = [], 0
    for ck in chunks:
        acc += len(ck)
        cum.append(acc)
    groups, start = [], 0
    for g in range(parts - 1):
        goal = len(line) * (g + 1) / parts
        best, best_d = None, float("inf")
        for j in range(start, len(chunks)):
            if len(chunks) - j - 1 < parts - g - 1:   # 剩下的块不够分给后面的段
                break
            d = abs(cum[j] - goal)
            if d < best_d:
                best_d, best = d, j
        if best is None:
            break
        groups.append("".join(chunks[start:best + 1]))
        start = best + 1
    groups.append("".join(chunks[start:]))
    return [g for g in groups if g]


def _unpack_long(shots: list) -> tuple:
    """单镜台词超过 5 秒说不完的，拆成两镜（同机位续拍）。

    台词写满以后这个坑必现：模型会把 40 多字塞进一镜，给到 5 秒上限也说不完，
    结果是配音赶拍、口型对不上。宁可多切一个镜头——出片模型一次本来就只出 5 秒。
    """
    out, count = [], 0
    for seg in shots:
        line = (seg.get("line") or "").strip()
        need = len(line) / CHARS_PER_SEC
        if need <= MAX_SHOT - 0.2:
            out.append(seg)
            continue
        parts = min(4, max(2, int(math.ceil(need / (MAX_SHOT - 1.5)))))
        pieces = _split_line(line, parts)
        if len(pieces) < 2:
            out.append(seg)
            continue
        count += 1
        for i, piece in enumerate(pieces):
            new = dict(seg)
            new["line"] = piece
            if i:
                new["action"] = "续上一镜（机位不变）：" + (seg.get("action") or "")
            out.append(new)
    return out, count


def _clamp_long(shots: list) -> tuple:
    """把超过 MAX_SHOT（5 秒）的镜头按几何时长原地切开，同机位续拍。

    为什么必须有这一步：`_rescale` 是整体等比缩放，模型给了 0-50 却要 60 秒时
    每个镜头都会被拉长 1.2 倍，一个 5 秒镜变 6 秒——上限就破了；`_rescale` 还会把
    最后一镜的终点硬掰到 duration，末镜更容易变长。出片模型一次只出 5 秒，
    多出来的部分要么生成不出来、要么被裁掉，所以这里必须切，不能只靠 warnings。

    切法：台词**整句留在第一片**（不劈句，劈开的两片各自都要对口型）；
    后续片只写「续上一镜（机位不变）：」+ 原动作，speaker/tone 一并清空。
    台词确实比第一片长的，交给 `_unpack_long` 按字数拆，再由 `_fit_by_dialogue` 分时长。
    """
    out, count = [], 0
    for seg in shots:
        t0, t1 = _f(seg.get("t_start")), _f(seg.get("t_end"))
        length = t1 - t0
        parts = int(math.ceil(length / MAX_SHOT)) if length > MAX_SHOT + 0.05 else 1
        if parts < 2 or length / parts < MIN_SHOT:
            out.append(seg)          # 切出来会碎成 1 秒多的片，宁可留个略长的镜头
            continue
        count += 1
        step = length / parts
        for i in range(parts):
            piece = dict(seg)
            piece["t_start"] = round(t0 + step * i, 1)
            piece["t_end"] = round(t1 if i == parts - 1 else t0 + step * (i + 1), 1)
            if i:
                piece["action"] = "续上一镜（机位不变）：" + (seg.get("action") or "")
                piece["line"] = piece["speaker"] = piece["tone"] = ""
            out.append(piece)
    return out, count


def _distribute(lens: list, duration: float) -> list:
    """把一组镜头时长按原比例校准到总长 duration，同时夹在 MIN_SHOT-MAX_SHOT 之间。

    两处要用同一套算法，所以抽出来：
    - 整条：`_fit_by_dialogue` 按台词量重分（模型常把 7.6 秒台词塞进 5 秒镜）；
    - 分段：`_segments_of` 把一段里几拍等比拉到正好 seg_len 秒。只按比例拉
      **会把 4.5 秒的拍拉成 5.36 秒**（2026-11-08 实测 S3 段两拍破 5 秒），
      而 5 秒是出片模型一次能出的上限，所以最后落文档的秒数也必须守这条线。

    夹不住的时候（这一组镜头太少、塞不满 duration）按比例一起放大，
    宁可每拍略超，也不把余量全堆到最后一拍上（会出来一个十几秒的大块）。
    """
    lens = [max(0.1, float(x)) for x in lens]
    total = sum(lens)
    if total <= 0 or duration <= 0:
        return lens
    lens = [x * duration / total for x in lens]
    # 先掐一次上下限再补差：只等比缩放的话，4.9/5.5/4.1 拉 3% 就变成 5.07/5.69/4.24，
    # 而 sum 正好等于 duration，下面的循环一次都不会进，超限的拍就被原样发出去了
    # （2026-11-08 桩测发现 A 段 5.1/5.7 两拍越界，就是这个洞）。
    lens = [min(MAX_SHOT, max(MIN_SHOT, x)) for x in lens]
    for _ in range(4):
        diff = duration - sum(lens)
        if abs(diff) < 0.05:
            break
        free = ([i for i, x in enumerate(lens) if x < MAX_SHOT - 0.05] if diff > 0
                else [i for i, x in enumerate(lens) if x > MIN_SHOT + 0.05])
        base = sum(lens[i] for i in free)
        if not free or base <= 0:
            break
        # 按现有比例增减，不平均分：保住模型原本的节奏比例
        for i in free:
            lens[i] = min(MAX_SHOT, max(MIN_SHOT, lens[i] + diff * lens[i] / base))
    short = duration - sum(lens)
    if short > 0.5:
        lens = [x * duration / sum(lens) for x in lens]
    return lens


def _round_fill(lens: list, total: float) -> list:
    """把时长列表取到 0.1 秒，并把四舍五入的零头摊给还有余量的拍。

    零头不能堆在最后一拍：14.9 秒的四段取整后合计少 0.1，全补给末拍就把它从
    5.0 顶成 5.1（2026-11-08 桩测 B 段就是这么越界的）。
    """
    out = [round(float(x), 1) for x in lens]
    drift = round(total - sum(out), 1)
    j, guard = 0, 0
    while abs(drift) >= 0.05 and out and guard < 400:
        i = j % len(out)
        if drift > 0 and out[i] < MAX_SHOT - 0.05:
            out[i] = round(out[i] + 0.1, 1)
            drift = round(drift - 0.1, 1)
        elif drift < 0 and out[i] > MIN_SHOT + 0.05:
            out[i] = round(out[i] - 0.1, 1)
            drift = round(drift + 0.1, 1)
        j += 1
        guard += 1
    return out


def _fit_by_dialogue(shots, duration):
    """按台词量重新分配镜头时长——只在「有镜头说不完」时调用。

    模型给的时间码是它想要的节奏，但它常把 7.6 秒的台词塞进 5 秒镜头；
    这里按每镜需要的秒数（字数 ÷ 4.5，留 8% 余量；纯动作镜至少 2 秒）等比分配，
    夹在 1.5-5 秒之间，总长仍等于 duration。改的是时长，不改台词、不改顺序。
    """
    weights = []
    for seg in shots:
        chars = len((seg.get("line") or "").strip())
        need = chars / CHARS_PER_SEC * 1.08 if chars else max(2.0, MIN_SHOT)
        weights.append(min(MAX_SHOT, max(MIN_SHOT, need)))
    if sum(weights) <= 0:
        return shots
    lens = _round_fill(_distribute(weights, duration), duration)

    t = 0.0
    for seg, length in zip(shots, lens):
        seg["t_start"] = round(t, 1)
        t += length
        seg["t_end"] = round(t, 1)
    shots[-1]["t_end"] = float(duration)
    return shots


def _overruns(shots):
    """哪些镜头的台词比镜长还长——(估时, 镜长) 列表。"""
    out = []
    for seg in shots:
        line = (seg.get("line") or "").strip()
        if not line:
            continue
        need = len(line) / CHARS_PER_SEC
        length = float(seg.get("t_end", 0)) - float(seg.get("t_start", 0))
        if need > length + 0.15:
            out.append((need, length))
    return out


# 身份词出现在 logline 但素材里查不到时提示人看一眼：可能是有意补的设定，也可能是套模板
_IDENTITY_WORDS = ("总裁", "董事长", "首富", "亿万", "财阀", "集团", "上市",
                   "千金", "少爷", "神医", "战神", "特种兵", "博士", "教授",
                   "黑客", "王爷", "将军", "豪门")


def _clean_text(s: str) -> str:
    return re.sub(r"[\s，。！？、,.!?：:；;「」『』\"'（）()]", "", s or "")


def _content_warnings(data: dict, episodes: list, source_text: str) -> list[str]:
    """数字之外的内容体检：剧情是不是拼的、补的内容有没有交代、人名是不是写岔了。

    数字类（镜头数 / 字数 / 时长 / 间隔）由 stats() 管，这里只管模型才会犯的错。
    2026-11-07「八万八买来的媳妇」那次：14 句台词横跨 10 集、logline 凭空冒出
    「亿万总裁」、同一个人写成「孙巧云 / 孙巧仁」——数字全部达标，warnings 却是空的。
    这些只提示、不拦截：补写细节和台词都是允许的，只要登记进 additions 且一条片前后接得上，最终由人看一眼决定。
    """
    out: list[str] = []
    src = _clean_text(source_text)[:30000]
    grams = {src[i:i + 4] for i in range(max(0, len(src) - 3))}
    names = {_s(c.get("name")) for c in (data.get("characters") or [])
             if isinstance(c, dict)} - {""}

    for ep in episodes:
        no = ep.get("no")
        # source 已于 2026-11-08 从 SCHEMA 删除（用户要求）：它是自由文本，
        # 约束不了模型，只会制造「跨度超过两集，很可能是挑狠话拼的」这类误报。
        # 老档里的 source 仍保留在数据里、仍会渲染成「取材」行。

        logline = _s(ep.get("logline"))
        invented = [w for w in _IDENTITY_WORDS if w in logline and w not in src]
        if invented:
            out.append(f"镜号{no} 的一句话剧情里「{'、'.join(invented[:3])}」"
                       "素材台词里没写到；若是有意补的设定，确认它和这场戏、人物卡接得上")

        declared = {_clean_text(a.get("content")) for a in (ep.get("additions") or [])
                    if isinstance(a, dict)}
        untraced = 0
        for s in ep.get("shots") or []:
            line = _clean_text(s.get("line"))
            if len(line) < 4 or line in declared:
                continue
            if any(line[i:i + 4] in grams for i in range(len(line) - 3)):
                continue
            untraced += 1
        if untraced:
            out.append(f"镜号{no} 有 {untraced} 句台词在素材里找不到对应原句、"
                       "也没登记进 additions；补写的台词请登记，便于逐条否决")

        # 人名变体：同姓、字数相同、只差一个字（孙巧云 / 孙巧仁 这类转写错字）
        local = set(names)
        local |= {_s(x) for x in (ep.get("cast_in") or [])}
        local |= {_s(v.get("name")) for v in (ep.get("voices") or []) if isinstance(v, dict)}
        local |= {_s(s.get("speaker")) for s in (ep.get("shots") or [])}
        local.discard("")
        variants = set()
        for name in local:
            if len(name) < 2:
                continue
            for s in ep.get("shots") or []:
                line = _clean_text(s.get("line"))
                for i in range(len(line) - len(name) + 1):
                    sub = line[i:i + len(name)]
                    # 「朱家」「孙氏」这类以家/氏/门/府收尾的不是人名变体，是「某家」这种说法；
                    # 不过滤的话「朱兄/朱家」会把正常台词报成转写错字（2026-11-08 实测误报）。
                    if sub[-1] in "家氏门府":
                        continue
                    if sub != name and sub[0] == name[0] \
                            and sum(a != b for a, b in zip(sub, name)) == 1:
                        variants.add((name, sub))
        if variants:
            shown = "、".join(f"{a}/{b}" for a, b in sorted(variants)[:3])
            out.append(f"镜号{no} 台词里出现疑似同名不同字：{shown}——"
                       "转写错字没归一，同一个人会被当成两个人")
    return out


def normalize(raw: dict, payload: dict, source_text: str = "") -> dict:
    """模型原始 JSON → 规范镜号数据。

    source_text 是这次喂给模型的素材原文，用来做内容级预警（数字校验用不上它）。
    """
    duration = _i(payload.get("duration") or 30)
    data = copy.deepcopy(raw) if isinstance(raw, dict) else {}

    doc_raw = data.get("doc") or {}
    doc = {"title": _s(doc_raw.get("title"), "未命名")[:20],
           "subtitle": _s(doc_raw.get("subtitle"))}

    characters = []
    for char in data.get("characters") or []:
        if not isinstance(char, dict) or not _s(char.get("name")):
            continue
        characters.append({
            "name": _s(char.get("name")),
            "desc": _s(char.get("desc")),
            "has_ref": bool(char.get("has_ref", True)),
        })

    episodes = []
    warnings = []
    for idx, ep in enumerate(data.get("episodes") or [], 1):
        if not isinstance(ep, dict):
            continue
        no = _i(ep.get("no"), idx)

        # 兼容模型把 shots 写成旧名 segments：字段含义一样，只是名字不同
        shots = []
        for seg in (ep.get("shots") or ep.get("segments") or []):
            if not isinstance(seg, dict):
                continue
            shots.append({
                "t_start": round(_f(seg.get("t_start")), 1),
                "t_end": round(_f(seg.get("t_end")), 1),
                # 这一镜在哪儿（2026-11-08 用户提出：场景直接落到每镜里）。
                # 原来场景只写在段头（scene_changes 按 30 秒切两窗），可 60 秒的片子
                # 常常一镜换一个地方，段头只能写一个，模型就把「律师事务所 / 新公司门口」
                # 塞进 shot 字段——有的镜写、有的镜不写，读起来一半有一半没有。
                # 现在每镜自带场景，段头只做汇总，分镜可以单独复制、单独重生成。
                "scene": _s(seg.get("scene")).removeprefix("场景：").strip(),
                "shot": _s(seg.get("shot")),
                "action": _s(seg.get("action")),
                "speaker": _s(seg.get("speaker")),
                "tone": _s(seg.get("tone")),
                "line": _s(seg.get("line")),
            })
        _fill_scene(shots)
        raw_total = max((s["t_end"] for s in shots), default=0.0)
        shots = _rescale(shots, duration)
        # 超过 5 秒的镜头先按几何时长切开（同机位续拍），再把说不完的台词拆镜。
        # 顺序不能反：_unpack_long 给它的两片用的是同一段时间码，若先拆句再切几何，
        # 同一段会被切开两遍、时间码翻倍。
        shots, clamped = _clamp_long(shots)
        if clamped:
            warnings.append(f"镜号{no} 有 {clamped} 个镜头超过 {MAX_SHOT:.0f} 秒，"
                            "已按长度切成同机位续拍的两三镜（台词留在第一镜）——"
                            "检查一下画面接得上不顺")
        shots, unpacked = _unpack_long(shots)
        if unpacked:
            warnings.append(f"镜号{no} 有 {unpacked} 句台词一镜说不完，已自动拆成两镜"
                            "（同机位续拍），看片时留意接得顺不顺")
        # 有镜头台词说不完：按台词量重分时长（只在必要时动节奏）
        if _overruns(shots) or unpacked or clamped:
            shots = _fit_by_dialogue(shots, duration)

        item = {
            "no": no,
            "title": _s(ep.get("title"), f"镜号{no}")[:12],
            "logline": _s(ep.get("logline"))[:60],
            "hook": _s(ep.get("hook")),
            "source": _s(ep.get("source")),
            "highlights": [str(h).strip() for h in (ep.get("highlights") or []) if str(h).strip()][:5],
            "cast_in": [str(n).strip() for n in (ep.get("cast_in") or []) if str(n).strip()][:8],
            "extras": [{
                "name": _s(x.get("name")),
                "desc": _s(x.get("desc")),
            } for x in (ep.get("extras") or []) if isinstance(x, dict) and _s(x.get("name"))],
            "scene": _s(ep.get("scene")).removeprefix("场景（文字）：").removeprefix("场景："),
            "scene_changes": _norm_scene_changes(ep),
            "staging": _s(ep.get("staging")).removeprefix("站位与轴线：").removeprefix("站位："),
            "voices": [{
                "name": _s(v.get("name")),
                "voice": _timbre(_s(v.get("voice"))),
            } for v in (ep.get("voices") or []) if isinstance(v, dict) and _s(v.get("name"))],
            "shots": shots,
            "constraints": [str(c).strip() for c in (ep.get("constraints") or []) if str(c).strip()][:8],
            "additions": [{
                "type": _s(a.get("type"), "新增"),
                "who": _s(a.get("who")),
                "content": _s(a.get("content")),
                "reason": _s(a.get("reason")),
                "deletable": _s(a.get("deletable"), "可删"),
            } for a in (ep.get("additions") or []) if isinstance(a, dict) and _s(a.get("content"))],
            "duration": duration,
        }
        item["stats"] = stats(item)
        episodes.append(item)

        s = item["stats"]
        low, high = SHOT_RANGE.get(duration, (5, 12))
        if not s["shots"]:
            warnings.append(f"镜号{no} 没有任何镜头，这条无法生成")
        elif not (low <= s["shots"] <= high):
            warnings.append(f"镜号{no} 镜头数 {s['shots']}，{duration} 秒常规是 {low}-{high} 镜")
        if s["max_shot"] > MAX_SHOT + 0.05:
            warnings.append(f"镜号{no} 最长单镜 {s['max_shot']}s，超过 {MAX_SHOT:.0f}s——"
                            "出片模型一次只出 5 秒，这条要手动再切一刀")
        too_long = _overruns(shots)
        if too_long:
            est_line, seg_len = max(too_long)
            warnings.append(f"镜号{no} 台词总量超出时长，重新分配后仍有 {len(too_long)} 个镜头说不完"
                            f"（最长 {est_line:.1f}s 的台词塞进 {seg_len:.1f}s）——"
                            f"{duration}s 装不下这么多字，需要删一截或换更长的时长")
        if shots and not (shots[0].get("line") or "").strip():
            open_len = round(shots[0]["t_end"] - shots[0]["t_start"], 1)
            if open_len >= 4:
                warnings.append(f"镜号{no} 开场 {open_len}s 没有台词——"
                                "超过 4 秒的无台词开场容易掉节奏，可把第一句台词提到开场")
        if s["density"] < 0.7:
            warnings.append(f"镜号{no} 台词只有 {s['chars']} 字（约 {s['est']}s），"
                            f"占 {round(s['density'] * 100)}%，台词偏少——"
                            f"{duration}s 该说到 {WORD_BUDGET.get(duration, duration * 4)} 字左右，"
                            "空出来的秒数观众会划走")
        elif s["chars"] > WORD_BUDGET.get(duration, duration * 4) * 1.15:
            warnings.append(f"镜号{no} 台词 {s['chars']} 字，{duration}s 说不完"
                            f"（每秒约 4.5 字，最多约 {int(duration * CHARS_PER_SEC)} 字）——"
                            "生成前要删掉一截，或把这条改成更长的时长")
        if s["max_gap"] > 5:
            warnings.append(f"镜号{no} 最长无台词段 {s['max_gap']}s（{s['why']}），超过 5s 容易掉节奏")
        if raw_total and (raw_total < duration * 0.7 or raw_total > duration * 1.3):
            warnings.append(f"镜号{no} 模型给的镜头总长 {raw_total:.0f}s，和 {duration}s 差得多，"
                            "已按比例拉伸，节奏需看一遍")

    if source_text:
        warnings.extend(_content_warnings(data, episodes, source_text))

    if not episodes:
        warnings.append("模型没有返回任何镜号")

    return {
        "doc": doc,
        "characters": characters,
        "episodes": episodes,
        "risks": [str(r).strip() for r in (data.get("risks") or []) if str(r).strip()],
        "warnings": warnings,
        "duration": duration,
        "aspect": payload.get("aspect") or "9:16",
        "style": (payload.get("style") or "").strip() or None,
        "material": "custom" if (payload.get("script") or "").strip() else "script",
        "count": len(episodes),
    }


def stats(ep: dict) -> dict:
    """校验表一行所需的所有数字，代码算，不信模型。"""
    shots = ep.get("shots") or []
    chars = sum(len((s.get("line") or "").strip()) for s in shots)
    est = round(chars / 4.5, 1)
    duration = float(ep.get("duration") or 0) or 1.0

    gaps = []
    prev_end = None
    for seg in shots:
        a, b = float(seg.get("t_start", 0) or 0), float(seg.get("t_end", 0) or 0)
        if (seg.get("line") or "").strip():
            if prev_end is not None and a - prev_end > 0.05:
                gaps.append((round(a - prev_end, 1), "镜头间隙"))
            prev_end = b
        else:
            gaps.append((round(b - a, 1), (seg.get("action") or "纯动作镜头").strip()[:14]))
    max_gap, why = max(gaps, key=lambda g: g[0]) if gaps else (0.0, "")

    lengths = [round(float(s.get("t_end", 0)) - float(s.get("t_start", 0)), 1) for s in shots]

    return {
        "shots": len(shots),
        "chars": chars,
        "est": est,
        "density": round(est / duration, 3),
        "max_gap": max_gap,
        "why": why,
        "max_shot": max(lengths) if lengths else 0.0,
        "total": round(sum(lengths), 1),
        "hook_count": len(ep.get("highlights") or []),  # 老档兼容；新产物不再产出 highlights
    }


# ---------------------------------------------------------------- 15 秒分段
#
# 2026-11-03 用户定调：一条 60 秒的镜号，要能拆成 A/B/C/D 四个 15 秒的生成单元，
# 每个单元都是「能单独复制去投喂图生视频模型」的完整提示词——不是把原文对半砍。
# 所以分两件事：
#   1) 切点由程序定（DP，只切在镜头之间，每段严格 ≤seg_len 秒）——模型改不动时间码；
#   2) 每段的「按段变化」字段（声线 tone、本段出场的人、约束）由程序
#      按这一段实际的镜头和台词现算兜底值。2026-11-07 起不再为段字段调第二次 AI
#      （原 _split_run 那一套已删），段里的值只来自这里。

SEG_LEN = 15.0          # 图生视频模型一次能出的长度（默认 15 秒）
# 可选段长（2026-11-05 用户要求加 30 秒）：有些模型一次能出 30 秒，
# 切成 30 秒段段数减半（60 秒整条 = A/B 两段），投喂次数少、拼接缝也少。
# 段长只影响「怎么切」，不影响镜数、台词量和时间预算——那些由整条 duration 决定。
SEG_CHOICES = (15, 30)
SEG_LETTERS = "ABCDEF"
SEG_SUFFIX = {
    1: [""],
    2: ["·上", "·下"],
    3: ["·上", "·中", "·下"],
    4: ["·上", "·中", "·下", "·终"],
}


def _bounds(shots: list, seg_len: float = SEG_LEN, want: int | None = None) -> list:
    """按镜头边界把一条切成 want 段（默认 总长/15 四舍五入），每段长度尽量接近 15 秒。

    只切在镜头之间——一个镜头是一镜到底，劈开会把动作切断。段数不再「能切几段切几段」：
    段数先定死（60 秒就是 4 段），再用 DP 找一组镜头边界让每段最接近 15 秒，
    最后由 _segments_of 把每段等比拉到正好 15 秒。
    这样 AI 没把边界对齐 15 秒线，也照样得到整齐的 A/B/C/D。
    """
    lens = [max(0.1, float(s.get("t_end", 0) or 0) - float(s.get("t_start", 0) or 0))
            for s in shots]
    n = len(lens)
    if not n:
        return []
    total = sum(lens)
    k = want or max(1, int(round(total / seg_len)))
    k = max(1, min(k, n))
    target = total / k
    INF = float("inf")
    dp = [[INF] * (n + 1) for _ in range(k + 1)]
    back = [[0] * (n + 1) for _ in range(k + 1)]
    dp[0][0] = 0.0
    for j in range(1, k + 1):
        for i in range(1, n + 1):
            acc = 0.0
            for p in range(i - 1, -1, -1):
                acc += lens[p]
                if dp[j - 1][p] == INF:
                    continue
                cost = dp[j - 1][p] + (acc - target) ** 2
                if cost < dp[j][i] - 1e-9:
                    dp[j][i] = cost
                    back[j][i] = p
    if dp[k][n] == INF:
        return [(0, n)]
    spans, i = [], n
    for j in range(k, 0, -1):
        p = back[j][i]
        spans.append((p, i))
        i = p
    spans.reverse()
    return spans


def _suffix(idx: int, total: int) -> str:
    table = SEG_SUFFIX.get(total)
    if table:
        return table[idx]
    return f"·{idx + 1}"


def _fill_scene(shots: list) -> list:
    """每镜的「在哪儿」：模型漏写的沿用上一镜，第一镜就漏写的空着（渲染时不写这段）。

    不拿整条的 scene 总述兜底——那是一句「多场景切换，旧厂冷光、新公司暖光」的总述，
    塞进单镜会变成「这一镜在多场景切换」，给视频模型的信息还不如空着。
    """
    prev = ""
    for s in shots:
        cur = (s.get("scene") or "").strip()
        if cur:
            prev = cur
        elif prev:
            s["scene"] = prev
    return shots


def _norm_scene_changes(ep: dict) -> list:
    """把模型给的 scene_changes 规整成 [(from, to, scene), ...]，scene 已剥「场景：」前缀。

    **2026-11-08 起场景改成每镜自带，schema 里已经没有 scene_changes 了**；
    这里只服务老产物（它们的 doc 已经渲染过，但「重新排版」还要能跑）。
    模型没给就返回空列表，渲染时退回每镜自己的 scene 或整条 scene。
    """
    out = []
    for c in (ep.get("scene_changes") or []):
        if not isinstance(c, dict):
            continue
        s = (_s(c.get("scene")).removeprefix("场景（文字）：")
             .removeprefix("场景：").strip())
        if not s:
            continue
        out.append((_f(c.get("from_sec")), _f(c.get("to_sec")), s))
    out.sort(key=lambda x: x[0])
    return out


def _scene_at(changes: list, t: float) -> str:
    """返回覆盖时刻 t 的场景；一个都没匹配到就退回第一条（兜底），都没有返回空串。"""
    if not changes:
        return ""
    best = changes[0][2]
    for a, b, s in changes:
        if a <= t < b:
            return s
    return best


def _segments_of(ep: dict, seg_len: float = SEG_LEN) -> list:
    """先把段切出来（每段等比拉到正好 seg_len 秒），字段填程序兜底值，之后由模型逐段改写。"""
    shots = ep.get("shots") or []
    dur = float(ep.get("duration") or 0) or sum(
        max(0.1, float(s.get("t_end") or 0) - float(s.get("t_start") or 0)) for s in shots)
    want = max(1, int(round(dur / seg_len))) if dur else None
    spans = _bounds(shots, seg_len, want)
    total = len(spans)
    lead = (ep.get("title") or "未命名").strip()
    out = []
    for idx, (a, b) in enumerate(spans):
        raw = shots[a:b]
        if not raw:
            continue
        # 镜头边界不动，只把这一段的秒数归到正好 seg_len（15 秒）。
        # 不能纯等比拉：一段 12.6 秒的戏塞进 15 秒要放大 1.19 倍，4.5 秒的拍
        # 会被拉成 5.36 秒（2026-11-08 实测），而 5 秒是出片模型一次的上限，
        # 落文档的秒数也必须守住，所以走 _distribute 带上下限校准。
        actual = sum(max(0.1, float(s.get("t_end") or 0) - float(s.get("t_start") or 0))
                     for s in raw)
        scale = (seg_len / actual) if actual > 0 else 1.0
        t0 = round(idx * seg_len, 1)
        end = round(t0 + seg_len, 1)
        # 每段对应自己的场景：新产物每镜自带 scene，取本段第一镜的；老产物没有
        # per-shot scene，才按段中点查 scene_changes 时间窗，再退回整条 scene。
        mid = round((t0 + end) / 2, 1)
        seg_scene = ((raw[0].get("scene") or "").strip()
                     or _scene_at(ep.get("scene_changes"), mid)
                     or (ep.get("scene") or "").strip())
        lens = _round_fill(_distribute(
            [max(0.1, float(s.get("t_end") or 0) - float(s.get("t_start") or 0)) for s in raw],
            seg_len), seg_len)
        beats, cur = [], t0
        for s, length in zip(raw, lens):
            b2 = dict(s)
            b2["t_start"] = round(cur, 1)
            cur = round(cur + length, 1)
            b2["t_end"] = cur
            beats.append(b2)
        beats[-1]["t_end"] = end

        speakers = []
        for s in beats:
            who = (s.get("speaker") or "").strip()
            if who and who not in speakers:
                speakers.append(who)
        cast = speakers or [n for n in (ep.get("cast_in") or []) if n]
        # 音色锚点原样带入每一段：四段最后要拼成一条片，同一个人的声音必须一模一样。
        # 段里只留一个空的 tone 位，等模型填「这一段这句怎么念」。
        anchors = {(v.get("name") or "").strip(): (v.get("voice") or "").strip()
                   for v in (ep.get("voices") or []) if (v.get("name") or "").strip()}
        voices = [{"name": n, "timbre": anchors.get(n, ""), "tone": ""} for n in cast]
        if not voices:
            voices = [{"name": n, "timbre": t, "tone": ""} for n, t in anchors.items()]
        out.append({
            "i": idx,
            "letter": SEG_LETTERS[idx] if idx < len(SEG_LETTERS) else str(idx + 1),
            "seg_title": lead + _suffix(idx, total),
            "t_start": t0,
            "t_end": end,
            "scene": seg_scene,
            "scale": round(scale, 2),
            "beats": beats,
            "cast_in": cast,
            "staging": (ep.get("staging") or "").strip(),
            "voices": voices,
            "constraints": [c for c in (ep.get("constraints") or []) if (c or "").strip()],
            "hooks": [],
            "ai": False,
        })
    return out


def _seg_sec(seg: dict) -> float:
    return round(float(seg.get("t_end") or 0) - float(seg.get("t_start") or 0), 1)


# ---------------------------------------------------------------- Markdown 渲染

def _cn(index: int) -> str:
    return CN[index] if index < len(CN) else str(index + 1)


def _num(value) -> str:
    x = round(float(value), 1)
    if abs(x - int(x)) < 1e-9:
        return str(int(x))
    return f"{x:.1f}"


def _span(a, b) -> str:
    return f"{_num(a)}—{_num(b)}秒"


def _figure_map(ep: dict) -> dict:
    """出镜顺序 → 图一、图二……每条独立重排。"""
    return {name: f"图{_cn(i)}" for i, name in enumerate(ep.get("cast_in") or [])}


def _shot_line(seg: dict) -> str:
    """样例格式：`**0—3秒**：客厅｜中景，他跪下。许广平说（压扁发干）"清辞，是我没教好许砚"。`

    「在哪儿」放在最前、用 ｜ 隔开：一镜单独投喂视频模型时，场景是它最先要知道的事，
    而「看着像一镜」的地方差了（办公室 / 门口）整条片就跳戏。
    """
    action = (seg.get("action") or "").strip()
    speaker = (seg.get("speaker") or "").strip()
    tone = (seg.get("tone") or "").strip()
    line = (seg.get("line") or "").strip()
    lens = (seg.get("shot") or "").strip()
    scene = (seg.get("scene") or "").strip()

    head = f"**{_span(seg.get('t_start', 0), seg.get('t_end', 0))}**："
    desc = "，".join(x for x in (lens, action) if x)
    if scene:
        desc = f"{scene}｜{desc}" if desc else scene
    if not line:
        return head + (desc or "（空镜）")
    if desc and desc[-1] not in "。！？…；":
        desc += "。"
    tail = f'{speaker}说（{tone}）"{line}"' if speaker else f'"{line}"'
    return head + desc + tail


def _scene_summary(beats: list, fallback: str = "") -> str:
    """段头那行「场景」：把本段各镜的 scene 按出场顺序去重拼起来（最多列 3 个）。

    段头只给视频模型一个整体坐标，真正管用的是每镜行首的场景。所以这里**不写死一个地点**：
    一段 15 秒里跑三个地方时硬写一个，段头就和下面分镜自相矛盾——2026-11-08 用户贴的
    那张图正是这样（段头写「工厂办公室」，下面三镜却在律所 / 新公司门口 / 工厂大门）。
    """
    names = []
    for b in beats:
        s = (b.get("scene") or "").strip()
        if s and s not in names:
            names.append(s)
    if not names:
        return (fallback or "").strip()
    if len(names) <= 3:
        return " → ".join(names)
    return " → ".join(names[:3]) + " 等"


def render_segment(ep: dict, seg: dict, data: dict) -> str:
    """一段 15 秒的完整生成单元——整块可以原样复制去投喂视频模型。

    **只留模型能执行的东西**（2026-11-07 清理）：段标题之外的每一行都要能改变画面
    或声音。写给人看的注释（"四段共用""由下游 AI 自己写""见人物卡"）一律不进段——
    模型读不懂它跟自己有什么关系，只会稀释真正的指令。
    """
    style = data.get("style") or DEFAULT_STYLE.format(aspect=data.get("aspect") or "9:16")
    letter = seg.get("letter") or "A"
    cast = [n for n in (seg.get("cast_in") or []) if n]
    figures = {name: f"图{_cn(i)}" for i, name in enumerate(cast)}

    refs = [f"{figures[n]}{n}" for n in cast]

    # 无图人物只在（台词/动作里）真的提到时才列，避免每段都拖着用不上的群演
    said = " ".join(((s.get("action") or "") + (s.get("line") or "")) for s in seg.get("beats") or [])
    extras = [x for x in (ep.get("extras") or [])
              if (x.get("name") or "").strip() and (x["name"] in said or x["name"] in " ".join(cast))]
    extra_block = ""
    if extras:
        extra_block = "\n" + "\n".join(
            f"（无图）{x.get('name')}：{(x.get('desc') or '').strip()}。" for x in extras)

    voice_lines = []
    for voice in seg.get("voices") or []:
        name = (voice.get("name") or "").strip()
        text = _voice_text(voice)
        if not name or not text:
            continue
        if name not in figures:
            figures[name] = f"图{_cn(len(figures))}"   # 兜底：模型漏排的人也别丢
        tag = f"（{figures[name]}）"
        voice_lines.append(f"- **{name}{tag}**：{text}")

    head = (f"### {letter}段｜镜号{ep.get('no', 1)}-{letter}《{seg.get('seg_title') or ''}》"
            f"{_num(seg.get('t_start', 0))}—{_num(seg.get('t_end', 0))}秒"
            f"（{_num(_seg_sec(seg))}s）")

    rules = _rules()
    parts = [head, ""]
    if data.get("ctl") and rules["控制句"]:
        parts += [rules["控制句"], ""]
    parts.append(f"画风：{style}")
    if refs:                                  # 没有人物就不出这一行，别写「见人物卡」
        parts.append("**人物参考**：" + "，".join(refs) + extra_block)
    parts += [
        f"**场景**：{_scene_summary(seg.get('beats') or [], seg.get('scene') or ep.get('scene') or '') or '—'}",
        f"**站位与轴线**：{(seg.get('staging') or '').strip() or '—'}",
        "**声线**",
    ]
    parts += voice_lines or ["- （本段无出声人物，纯动作叙事）"]
    parts += ["", *(f"{_shot_line(s)}  " for s in seg.get("beats") or []), ""]
    # 约束/爽点/叠加字幕（后期）已于 2026-11-07 移除：这些内容由下游 AI 自己写。
    # 声音设计（sound）已于 2026-11-08 按用户要求删除：生成的是无声视频，这段只在
    # 后期配音剪辑用，模型又只会写「环境声铺底、音乐渐入」这类空话。
    # 唯一保留的约束是「不要烧字」——不写模型就会在画面里生成台词字幕。
    if rules["每镜要求"]:
        parts += [rules["每镜要求"]]
    return "\n".join(parts)


def render_whole(ep: dict, data: dict) -> str:
    duration = int(ep.get("duration") or data.get("duration") or 30)
    figures = _figure_map(ep)
    style = data.get("style") or DEFAULT_STYLE.format(aspect=data.get("aspect") or "9:16")

    ref_parts = [f"{figures[name]}{name}" for name in ep.get("cast_in") or [] if figures.get(name)]
    ref_line = ("**人物参考**：" + "，".join(ref_parts) + "。") if ref_parts else ""
    extra_lines = []
    for extra in ep.get("extras") or []:
        extra_lines.append(f"（无图）{extra.get('name', '')}：{extra.get('desc', '')}。")
    extras_block = ("\n" + "\n".join(extra_lines)) if extra_lines else ""

    voice_lines = []
    for voice in ep.get("voices") or []:
        name = (voice.get("name") or "").strip()
        if not name:
            continue
        fig = figures.get(name, "")
        tag = f"（{fig}）" if fig else ""
        voice_lines.append(f"- **{name}{tag}**：音色：{_timbre(voice.get('voice') or '')}")

    shot_lines = [_shot_line(seg) for seg in ep.get("shots") or []]

    rules = _rules()
    constraints = [c for c in (ep.get("constraints") or []) if (c or "").strip()]
    if rules["每镜要求"] and not any("文字" in c for c in constraints):
        constraints.append(rules["每镜要求"])

    parts = [
        f"# 镜号{ep.get('no', 1)}《{ep.get('title', '未命名')}》{duration}s｜钩子：{ep.get('hook', '')}",
        "",
    ]
    if data.get("ctl") and rules["控制句"]:
        parts += [rules["控制句"], ""]
    parts += [f"画风：{style}", ""]
    if ref_line or extras_block:
        parts.append((ref_line + extras_block).strip())
    if ep.get("logline"):
        parts += ["", f"**一句话剧情**：{ep['logline']}"]
    parts += [
        "",
        f"**场景**：{_scene_summary(ep.get('shots') or [], ep.get('scene') or '')}",
        "",
        f"**站位与轴线**：{(ep.get('staging') or '').strip()}",
        "",
        "**声线**",
    ]
    parts += voice_lines or ["- （本条无出声人物，纯动作叙事）"]
    parts += ["", "**分镜**", ""]
    parts += [s + "\n" for s in shot_lines]
    parts += [
        f"**约束**：{'；'.join(constraints)}",
        "",
    ]
    return "\n".join(parts)


def render_episode(ep: dict, data: dict) -> str:
    """一条镜号的完整产物：先一行总览，再把它拆出来的 A/B/C/D 段整块铺开。"""
    segs = ep.get("segments") or []
    if not segs:
        return render_whole(ep, data)
    duration = int(ep.get("duration") or data.get("duration") or 30)
    rhythm = " · ".join(
        f"{seg['letter']} {_num(seg['t_start'])}—{_num(seg['t_end'])}秒"
        f"（{len(seg.get('beats') or [])}镜）" for seg in segs)
    parts = [
        f"## 镜号{ep.get('no', 1)}《{ep.get('title', '')}》{duration}s"
        f"｜钩子：{ep.get('hook', '')}",
        "",
        f"**一句话剧情**：{ep.get('logline') or '—'}",
    ]
    src = (ep.get("source") or "").strip()
    if src:
        parts.append(f"**取材**：{src}")
    parts += ["", f"**整条节奏**：{rhythm}", ""]
    for seg in segs:
        parts.append(render_segment(ep, seg, data))
    return "\n".join(parts)


def _episodes_md(episodes: list[dict], data: dict) -> list[dict]:
    """前端要的那份结构：每条镜号的整篇 md + 每个生成单元的 md。

    抽成函数是因为「重新排版（不调 AI）」也要重建它——前端「复制 X 段」读的就是
    这里的 segments[].md，只重写整篇文档的话，复制出去的还是旧版本。
    """
    return [{
        "no": ep["no"], "title": ep["title"], "hook": ep["hook"],
        "logline": ep["logline"], "seconds": ep.get("duration"),
        "seg_count": len(ep.get("segments") or []),
        "md": render_episode(ep, data),
        "segments": [{
            "label": f"{ep['no']}-{g['letter']}",
            "letter": g["letter"],
            "seg_title": g["seg_title"],
            "t_start": g["t_start"], "t_end": g["t_end"], "sec": _seg_sec(g),
            "shots": len(g["beats"]),
            "lines": sum(1 for b in g["beats"] if (b.get("line") or "").strip()),
            "hooks": g.get("hooks") or [],
            "md": render_segment(ep, g, data),
        } for g in (ep.get("segments") or [])],
    } for ep in episodes]


def render_doc(data: dict, pname: str = "") -> str:
    rules = _rules()
    duration = int(data.get("duration") or 30)
    seg_len = float(data.get("seg_len") or SEG_LEN)
    nseg = max(1, int(round(duration / seg_len))) if duration else 1
    letters = "/".join(SEG_LETTERS[i] for i in range(min(nseg, len(SEG_LETTERS))))
    # doc 在生成时是 {title, subtitle}，存进 project.json 后标题被摊平成 doc_title、
    # doc 变成整篇 Markdown 字符串——重新排版（不调 AI）时两种都要认，
    # 否则 render_doc 会在 `.get()` 上直接崩（2026-11-07 修）。
    meta = data.get("doc") if isinstance(data.get("doc"), dict) else {}
    title = data.get("doc_title") or meta.get("title") or pname or "未命名"
    subtitle = data.get("subtitle") or meta.get("subtitle") or ""
    eps = data.get("episodes") or []

    head = [
        f"# {title}｜{duration}秒引流分镜｜{seg_len:g}秒分段提示词"
        f"（镜号1—{len(eps)}）",
        "",
        f"> 说明：本文件把每条 {duration} 秒镜号拆成若干 **{seg_len:g} 秒的生成单元**（{letters}），"
        "每段都是一个能单独复制、单独投喂视频模型的完整提示单元——画风、人物参考、场景、"
        "站位轴线、声线、逐镜时间码全部自带，不依赖上下文；"
        "语气和重音写在每句台词前的括号里。图号为段内独立编号，画面内不生成文字。",
        "",
        (f"> 合规：{rules['合规声明']}" if rules["合规声明"] else ""),
        "",
        f"日期：{date.today().isoformat()}",
        (f"主源：钩子工坊（{pname}）｜剧情来自用户提供的素材，分镜为重新创作"
         if data.get("material") == "custom" else
         f"主源：钩子工坊（{pname}）｜剧情来自 Whisper 台词，分镜为重新创作")
        + (f"：{subtitle}" if subtitle else ""),
        "",
        "## 编号规则",
        "",
        f"- **镜号N ＝ 一条完整的 {duration} 秒成片**；**镜号N-A ~ N-{SEG_LETTERS[max(0, nseg - 1)]} ＝ 它拆出来的 {seg_len:g} 秒生成单元**，"
        f"一段生成一个视频，最后按 {'→'.join(SEG_LETTERS[i] for i in range(min(nseg, len(SEG_LETTERS))))} 顺序拼。",
        f"- 段内时间码沿用的是**整条的秒数**（A 段 `0—{seg_len:g}秒`、B 段 `{seg_len:g}—{seg_len * 2:g}秒`……），"
        "方便你对着整条拼接，不是每段从 0 重新数。",
        "- **图片编号按段独立重排**：每段都从**图一**开始，同一个人在不同段里图号可以不同——"
        "上传参考图时按段对照，不要跨段沿用。",
        "- **音色锁定**：**同一条片（镜号N）里**同一人只有一份音色锚点，A/B/C/D 四段原样共用；"
        "**不同镜号各自独立**，同一个人在不同条里可以是不同音色（表里按镜号分开列，别跨条照抄）。"
        "每句怎么念（语气 / 语速 / 重音）写在台词前的括号里，配音照括号走——"
        "音色锚点里不再重复写情绪，免得两处描述打架。",
        "",
        "## 音色锁定表（同一条片内一人一版；不同镜号可以不同）",
        "",
        "| 人物 | 音色锚点（音色/音量/语速/尾音，不含情绪） | 适用镜号 |",
        "|---|---|---|",
    ]

    # 锁定只在条内做（_unify_voices），跨镜号允许不同版本：这里按「人物 + 音色文本」
    # 分组，同一人在不同条里写法不同就会各占一行，各带自己适用的镜号——
    # 不再 setdefault 成一行，那样会把「镜号2 用的是另一版」这个事实藏掉。
    rows, row_eps = {}, {}
    for ep in eps:
        for v in ep.get("voices") or []:
            name = (v.get("name") or "").strip()
            text = _timbre(v.get("timbre") or v.get("voice") or "")
            if not name or not text:
                continue
            key = (name, text)
            rows.setdefault(key, None)
            row_eps.setdefault(key, [])
            no = ep.get("no", 1)
            if no not in row_eps[key]:
                row_eps[key].append(no)
    for (name, text) in rows:
        head.append(f"| {name} | {text} | "
                    f"{'、'.join('镜号%s' % n for n in row_eps[(name, text)])} |")
    if not rows:
        head.append("| — | （本条无台词人物） | — |")
    head += [
        "",
        "## 校验表",
        "",
        "| 镜号 | 标题 | 一句话 | 段数 | 镜头数 | 台词字数 | 估算台词时长 | 最长无台词段 |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]

    for ep in eps:
        s = stats(ep)
        segs = ep.get("segments") or []
        gap = f"{s['max_gap']}s（{s['why']}）" if s["why"] and s["max_gap"] > 0 else f"{s['max_gap']}s"
        head.append(
            f"| 镜号{ep.get('no', 1)} | {ep.get('title', '')} | {ep.get('logline', '')} "
            f"| {len(segs)}（{'/'.join(g['letter'] for g in segs) or '—'}） | {s['shots']} "
            f"| {s['chars']} | {s['est']}s（{round(s['density'] * 100)}%） | {gap} |"
        )

    body = ["", "---", ""]
    for ep in eps:
        body.append(render_episode(ep, data))
        body.append("---")
        body.append("")

    tail = ["# 改写与新增台词清单（可逐条否决）", ""]
    additions = [(ep, a) for ep in eps for a in (ep.get("additions") or [])]
    if additions:
        tail += ["| 镜号 | 类型 | 谁 | 内容 | 理由 | 可否删 |", "|---|---|---|---|---|---|"]
        for ep, a in additions:
            tail.append(
                f"| 镜号{ep.get('no', 1)} | {a.get('type', '新增')} | {a.get('who', '')} "
                f"| {a.get('content', '')} | {a.get('reason', '')} | {a.get('deletable', '可删')} |"
            )
    else:
        tail.append("（全部沿用原剧台词，没有改写）")
    tail.append("")

    risks = data.get("risks") or []
    if risks:
        tail += ["# 生成前提醒", ""]
        tail += [f"- {r}" for r in risks]
        tail.append("")

    tail += [
        "---",
        "",
        "## 用法（按段生成，别整条丢进去）",
        "",
        "1. 从 `A段｜镜号1-A` 开始，**整块复制**（含控制头、画风、人物参考、场景、站位、声线、"
        "逐镜、约束）直接投喂视频模型，得到 `镜号1-A.mp4`。",
        f"2. 再按顺序复制剩下的段（{'、'.join(SEG_LETTERS[i] + '段' for i in range(1, min(nseg, 6)))}），"
        f"同样整块投喂；**每段 {seg_len:g} 秒正好是一次生成的时长**，"
        "段内不要再拆，也不要两段合起来一次生成。",
        f"3. {'、'.join(SEG_LETTERS[i] for i in range(min(nseg, len(SEG_LETTERS))))} 按字母顺序拼接，"
        "就是一条完整的成片；段与段之间是硬切，不需要转场。",
        "4. 配音按「音色锁定表」找音色：四段同一个人用同一个音色，只有重音和情绪按段落走；"
        "一条片里换音色，观众一秒就出戏。",
        "",
        "## 生成说明",
        "",
        ("1. 本文以" + ("**用户提供的素材**" if data.get("material") == "custom" else "**原剧台词**")
         + "为剧情依据，**分镜是按投流节奏重新构思的**；素材没写明的细节由 AI 补足"
           "（见「改写与新增台词清单」）。"),
        f"2. 单镜 ≤{MAX_SHOT:.0f} 秒（出片模型一次能出的最长时长）；分段切点全部落在镜头之间，不会把一个动作劈成两半。",
        "3. 上传人物参考图时**按段对照图号**，不要跨段沿用；同一人同一张脸是硬要求。",
        "4. 台词时长按 4.5 字/秒估算，实际配音有出入时，优先压动作段，不动关键台词。",
        "5. 音色**按条锁定**：同一条片（镜号N）内同一人的音色锚点是同一个版本，A/B/C/D 要一致；"
        "改的话要改该条的所有段，不能只改一段。不同镜号允许用不同音色，互不影响。",
    ]
    return "\n".join(head + body + tail)


# ---------------------------------------------------------------- 生成入口

# 第 3 步 user 提示词的段序：模板按 `## 段名` 分段，这里定拼回去的顺序。
# 用户删掉某段 = 删掉那条要求；**段名别改**（改了程序找不到，那一段内容就不会发出去）。
# 2026-11-08 用户要求所有提示词都能改：原来这一大段措辞全拼在代码里，现在读
# prompts/shots_user.txt。
_USER_SECTIONS = ("任务", "规格", "分段", "画风", "人物卡", "素材", "输出格式")


def _sections(name: str) -> dict:
    """把 `## 段名` 分段的模板拆成 {段名: 内容}。"""
    out, cur, buf = {}, None, []
    for line in analyze._read_prompt(name).splitlines():
        m = re.match(r"^##\s*(.+?)\s*$", line)
        if m:
            if cur:
                out[cur] = "\n".join(buf).strip()
            cur, buf = m.group(1).strip(), []
        elif cur is not None:
            buf.append(line)
    if cur:
        out[cur] = "\n".join(buf).strip()
    return out


def _render_user_prompt(**kw) -> str:
    """按 prompts/shots_user.txt 拼第 3 步 user 提示词。

    值为空的段整段跳过：没填画风就不发画风段（跟原来一致）；用户把某段内容清空，
    也等于主动不要这条要求。花括号里的东西全由程序填，用户改措辞别删占位符。
    """
    secs = _sections("shots_user.txt")
    kw = dict(kw, schema=_schema(kw["duration"]))
    parts = []
    for key in _USER_SECTIONS:
        body = secs.get(key, "").strip()
        if not body or (key == "画风" and not kw.get("style")):
            continue
        body = analyze._fill(body, **kw).strip()
        if body:
            parts.append(body)
    return "\n\n".join(parts)


def _build_user_prompt(p: dict, payload: dict, set_no: int = 0, total: int = 0) -> str:
    """只给程序算得出的可数尺子；节奏、爽点、口味全交给模型自由发挥。

    2026-11-07 用户定：根本原则在 system 提示词里说一遍就够，用户消息里
    不要再叠口味偏好、字数反复强调、字段清单这些不必要的限制。
    """
    duration = int(payload.get("duration") or 30)
    # 并联模式（set_no>0）：这一趟只写 1 条，count 退化为 1，避免模型又回 N 条
    count = 1 if set_no else int(payload.get("count") or 3)
    aspect = payload.get("aspect") or "9:16"
    style = (payload.get("style") or "").strip()
    cast = (payload.get("cast") or "").strip()
    script = (payload.get("script") or "").strip()
    # 段长（15/30 秒）：只影响「分段边界」那一条要求，镜数/台词量仍按整条时长算
    seg_len = _f(payload.get("seg_len"), SEG_LEN)
    if seg_len not in SEG_CHOICES:
        seg_len = SEG_LEN

    low, high = SHOT_RANGE.get(duration, (5, 12))
    words = WORD_BUDGET.get(duration, duration * 4)
    # 字数的三个数一次算清：整条下限、每镜均值区间（由镜数倒算，别让模型自己除）
    w_min = int(round(words * 0.9))
    per_lo, per_hi = int(round(words / high)), int(round(words / low))

    # 素材来源二选一：本剧台词，或调用方直接给的一段剧情素材。
    # 台词格式说明属于「模型真能判断的事」，保留；其余口味/节奏要求一律不写。
    # 「1 条」时不能说「N 条互不重复」——只有一个对象，没有可对比的另一条。
    # 并行各条是彼此独立的请求（各写各的、同时发出），提示词里**不提别的条**：
    # 2026-11-08 用户定——不解释「这批共 N 条、你是第几条、和别人互不相干」，
    # 那些是实现机制，模型不需要知道；说多了反而引它去补前情、给别的条留扣子。
    many = f"{count} 条互不重复的 " if count > 1 else ""
    # 素材来源二选一：本剧台词，或调用方直接给的一段剧情素材。
    # 「1 条」时不能说「N 条互不重复」——只有一个对象，没有可对比的另一条。
    # 并行各条是彼此独立的请求（各写各的、同时发出），提示词里**不提别的条**：
    # 2026-11-08 用户定——不解释「这批共 N 条、你是第几条、和别人互不相干」，
    # 那些是实现机制，模型不需要知道；说多了反而引它去补前情、给别的条留扣子。
    if set_no:
        # 2026-11-08 用户定（选 A）：**不再给每条划集区间**（原 `_focus_for`）。
        # 划 5 集等于让模型在 5 集里挑，天然诱发跨集拼贴，和「连贯第一」直接冲突；
        # 且强戏集中在少数几集时，切 4 段后多数条拿不到好戏，只能硬凑。
        # 也不再补一句「别跨集拼」——那条规则 2026-11-08 已由用户明确删除，别偷偷加回。
        task_head, task_verb = "", "只写 **1 条** "
    elif script:
        task_head = f"\n下面是用户提供的剧情素材（{len(script)} 字，没有集号也没有时间码）。"
        task_verb = "构思 "
    else:
        total = sum(float(ep.get("duration") or 0) for ep in p.get("episodes") or [])
        # 原来这里还有一句「句首括号 (0.0-0.7) 是这句在本集里的起止秒数，不是集号」——
        # 台词改用 plain 版后已经没有括号了，说它只会让模型去找不存在的括号。
        task_head = (
            f"\n《{p.get('name') or '本剧'}》全剧 {len(p.get('episodes') or [])} 集、"
            f"共 {total / 60:.1f} 分钟。\n"
            "台词每集以【第NN集】开头，同一集的台词按播出顺序排列。"
        )
        task_verb = "构思 "

    # 分段要求单独算：整条比段还短（15 秒整条选了 30 秒段）时就没有切点，
    # 硬塞一条「切点必须落在 30 秒」的空要求只会让模型困惑。
    if seg_len < duration:
        # 切点只是「程序把一条片切开输送」的位置，不是片子的段落分界。
        # 2026-11-08 用户指出：原句「切点前后要是两个完整的镜头」容易被读成
        # 「切点处要有一个完整的收束/新起」，于是四段各自收尾、接起来不像一条片。
        # 保留硬约束（不许跨切点、不劈台词动作），同时明确否定段落感。
        seg_req = (
            f"# 分段边界（硬要求）\n成片会按 {seg_len:g} 秒切成 "
            f"{max(1, int(round(duration / seg_len)))} 段分别生成、再按顺序拼回"
            f"这一条 {duration} 秒的片子，所以 "
            f"**{'、'.join(str(int(x)) + '秒' for x in range(int(seg_len), duration, int(seg_len)))}"
            f" 必须落在镜头边界上**：一个镜头不许跨过这些时间点，"
            "也不许把一句台词或一个动作劈成两半。\n"
            "这几个切点只是切开输送的位置，**不是片子的段落分界**："
            "不要在切点处收束、停顿、做转场或换场景；切点两侧仍是同一条时间线，"
            "接起来要像一条连续拍下来的片子。")
    else:
        seg_req = f"# 分段\n整条 {duration} 秒一次生成完，不用考虑切点。"

    # 人物卡口径：用户给了卡就照卡用，粘了素材让模型按素材设定。读本剧台词时这一句
    # 必须常驻——原来默认路径什么都不注入，模型就把 has_ref 一律填 true（实测 5 个
    # 人物全 true，连只出现一句的厂长都是），后面按 has_ref 分参考图就分不开。
    if cast:
        cast_rule = ("characters 覆盖这些人，cast_in 从中取本条出镜者；"
                     "没有参考图的 has_ref 填 false 并放进 extras。")
    elif script:
        cast_rule = ("按素材里出现的人自行设定外观写进 characters；"
                     "外形不明确的小角色 has_ref 填 false 并放进 extras。")
    else:
        cast_rule = ("characters 里放本条出镜的人；有形象参考的 has_ref 填 true，"
                     "只被提一句、没有独立形象的填 false 并写进 extras。"
                     "同一人只用一个名字，不要简称全名混着写。")

    if script:
        material = (f"# 剧情素材（不要引用别的作品）\n{script}\n\n"
                    "# 标题\ndoc.title 自拟（≤6 字），不要用项目名。")
    else:
        # source（取材集号）已于 2026-11-08 删：它只是个备注，既约束不了模型跨集取材，
        # 还会和取材说明打架触发误报，多一个字段多一处出错面。
        # plain=True：只给集号和台词，不带「序号 + (集内起止秒)」前缀。这一步是重写分镜，
        # 原片时间码用不上（t_start/t_end 是本条秒数），前缀却占台词块 67%、全提示词 54%，
        # 还会让模型把它误当成台词时长或本条时间码。plans 那条路仍用默认带编号的版本。
        material = f"# 台词原文\n{analyze._all_text(p, plain=True)}"

    return _render_user_prompt(
        duration=duration, aspect=aspect, low=low, high=high,
        words=words, w_min=w_min, per_lo=per_lo, per_hi=per_hi,
        min_shot=f"{MIN_SHOT:.1f}", max_shot=f"{MAX_SHOT:.0f}", line_max=LINE_MAX_CHARS,
        task_head=task_head, task_verb=task_verb, many=many,
        seg_req=seg_req, style=style, cast_rule=cast_rule, cast=cast,
        material=material)


def _gen_one(p: dict, payload: dict, set_no: int, total: int,
             text: str) -> tuple[dict, dict]:
    """并联里的一条：单独出一趟请求，只写 1 条分镜。

    模型偶尔会在「只写 1 条」的调用里回 2-3 条，这里只取第一条，
    保证每条请求严格对应一条成品（count 和界面显示对得上）。

    返回 (规整后的产物, 模型原始响应)。原始响应必须一起带出来——
    每条请求各有自己的响应，落盘存档时要按条存，不能只留最后一份。
    """
    prompt = _build_user_prompt(p, payload, set_no=set_no, total=total)
    raw = analyze._call(prompt, system=analyze._read_prompt("shots.txt"),
                        model=shots_model())
    res = normalize(raw, payload, source_text=text)
    eps = res.get("episodes") or []
    if len(eps) > 1:
        res["episodes"] = eps[:1]
    elif not eps:
        raise RuntimeError("AI 没有返回镜号，请重试")
    # 警告里的「镜号X」是 normalize 按模型填的 no 写的，而并行时每趟各自从 1 开始，
    # 并回后会被重编号成请求序号（1/2/3/4）。这里先对齐，否则 4 条的警告全指向「镜号1」，
    # 用户根本看不出是哪一条的问题（2026-11-08 桩测发现）。
    if set_no:
        res["warnings"] = [re.sub(r"镜号\s*\d+", f"镜号{set_no}", w)
                           for w in (res.get("warnings") or [])]
    return res, raw


def shots_run(pid: str, count: int, duration: int, aspect: str = "9:16",
              style: str = "", cast: str = "", script: str = "",
              seg_len: float = SEG_LEN, ctl: bool = True) -> None:
    """第 3 步后台任务：只调一次 AI，生成引流片分镜提示词，落到 p['shots'] 和 .md 文件。

    seg_len：每段多长时间（15 / 30 秒，对应视频模型一次能生成的时长）。
    只影响怎么切，不影响镜数、台词量、时间预算——那些由整条 duration 定。
    ctl：段首是否带「对话式生成器控制句」（禁止反问、一次生成）。默认带，
    因为对话式生成器最常用；纯 API / 工作流不需要时可在界面关掉。
    """
    p = core.load(pid)
    if not p:
        return
    script = (script or "").strip()
    if not script and not (p.get("episodes") or []):
        raise RuntimeError("还没有转写结果，请先转写；或者在「素材来源」里选「我自己提供剧情素材」并粘贴素材")
    count = max(1, min(10, count))
    duration = 60 if int(duration) >= 60 else (30 if int(duration) >= 30 else 15)
    # 段长只能是 15 / 30；整条比段还短（15 秒整条 + 30 秒段）就退回 15，
    # 否则会切出「1 段 30 秒的片子装 15 秒内容」这种对不上的产物
    try:
        seg_len = float(seg_len or SEG_LEN)
    except (TypeError, ValueError):
        seg_len = SEG_LEN
    if seg_len not in SEG_CHOICES or seg_len > duration:
        seg_len = SEG_LEN

    payload = {
        "count": count, "duration": duration, "aspect": aspect,
        "style": style, "cast": cast, "script": script,
        "seg_len": seg_len,
    }
    # 与喂给模型的一致（plain 版）：warnings 里「台词在素材里找不到对应原句」的
    # 4 字串比对不该被「12. (16.1-18.4)」这些前缀干扰。
    text = script or analyze._all_text(p, plain=True)
    if not text.strip():
        raise RuntimeError("这个项目还没有转写结果，读不到台词；请先转写，或改用「我自己提供剧情素材」")
    conc_cap = core.max_concurrent()      # 一次读、整批用，中途改设置不影响这一批
    core.set_task(pid, "shots", 8,
                  f"整理{'剧情素材' if script else '台词'}（{len(text)} 字），组装创作提示词")
    core.set_task(pid, "shots", 25,
                  f"AI 正在读剧情、写 {count} 条 {duration}s 分镜"
                  + (f"（{min(conc_cap, count)} 路并行）" if count > 1 else ""))
    # 每条请求各自的模型原始响应，只用于「出问题时回溯模型到底给了什么」，
    # 单条分支放 1 份、并行分支放 count 份。必须在这里就初始化：
    # 2026-11-08 就是因为只在单条分支里赋值，并行分支落盘时引用未定义变量，
    # 4 套都生成成功后整批判失败、产物全丢（用户白花 4 次调用）。
    raws: list = []
    if count <= 1:
        # 单条：保持原来的「一次调用出整条」行为，提示词与产物完全不变
        single = _build_user_prompt(p, payload)
        raw = analyze._call(single, system=analyze._read_prompt("shots.txt"),
                            model=shots_model())
        raws.append(raw)
        shots = normalize(raw, payload, source_text=text)
    else:
        # 多条：每条独立成一趟请求、并联发出，再用程序并回一份完整产物。
        # 这样单趟输出只有 1 条的量，不会因为「count 条一起写」被 600s 上限拖垮
        # （八万八那次就是 4×60s 一股脑塞进一次调用、等到超时）。
        # 各条不再划集区间（2026-11-08 选 A）；某条失败先串行重试一次
        # （并发触发上游限流时很有用），仍失败就先交付其余条并告警，
        # 不再像以前那样整批卡死、把生成按钮锁住。
        results = [None] * count

        def _worker(k):
            try:
                return _gen_one(p, payload, k + 1, count, text)
            except Exception as exc:  # noqa: BLE001
                return exc

        conc = min(conc_cap, count)
        with ThreadPoolExecutor(max_workers=conc) as pool:
            futs = {pool.submit(_worker, k): k for k in range(count)}
            done = 0
            for fut in as_completed(futs):
                k = futs[fut]
                results[k] = fut.result()
                done += 1
                core.set_task(pid, "shots", 25 + 40 * done / count,
                              f"AI 已写完 {done}/{count} 条，正在汇总")

        # 并行阶段失败的，串行重试一次（多半是并发触发的 429 / 排队）。
        # 并发开到 8 时若一批同时被限流，立刻连着重发会再次挤在一起撞墙，
        # 白等一整轮超时；按失败顺序错峰 2/4/6/8 秒，让上游限流窗先过去。
        retry_i = 0
        for k in range(count):
            if isinstance(results[k], Exception):
                if retry_i:
                    time.sleep(min(2 + 2 * retry_i, 8))
                retry_i += 1
                try:
                    results[k] = _gen_one(p, payload, k + 1, count, text)
                except Exception as exc:  # noqa: BLE001
                    results[k] = exc

        episodes, characters, risks, warnings = [], [], [], []
        doc = None
        style0 = None
        material = "custom" if script else "script"
        failed = []
        seen_chars = set()
        for k, res in enumerate(results):
            if isinstance(res, Exception):
                failed.append((k + 1, str(res)))
                continue
            res, raw_one = res
            raws.append(raw_one)
            if doc is None:
                doc = res.get("doc") or {}
            if style0 is None:
                style0 = res.get("style")
            for ep in (res.get("episodes") or []):
                ep["no"] = k + 1              # 并回后按请求顺序重编号，失败的留空号
                episodes.append(ep)
            # 人物卡**按套独立**保留（2026-11-08 用户定：每套独立、互不干扰）。
            # 旧写法按名字全局去重、先到先得，第 2 套对同一个人的不同设定会被第 1 套
            # 顶掉，等于套间互相覆盖。同一套内部同名重复才去重（模型偶尔写两遍）。
            for c in res.get("characters") or []:
                if not (isinstance(c, dict) and c.get("name")):
                    continue
                key = (k + 1, str(c.get("name")).strip())
                if key in seen_chars:
                    continue
                seen_chars.add(key)
                item = dict(c)
                item["no"] = k + 1            # 这份人物卡属于哪条镜号
                characters.append(item)
            for r in res.get("risks") or []:
                if r and r not in risks:
                    risks.append(r)
            warnings.extend(res.get("warnings") or [])

        if not episodes:
            # failed 里存的是错误描述字符串，直接 raise 会变成「exceptions must
            # derive from BaseException」的 TypeError，把真实原因吃掉。
            raise RuntimeError(failed[0][1] if failed
                               else "AI 没有返回任何镜号，请重试")
        if failed:
            warnings.append(
                f"有 {len(failed)} 条没生成成功（第 {', '.join(str(n) for n, _ in failed)} 条），"
                f"已先交付其余 {len(episodes)} 条；失败原因：{failed[0][1][:120]}")
        shots = {
            "doc": doc or {"title": (p.get("name") or "未命名"), "subtitle": ""},
            "characters": characters,
            "episodes": episodes,
            "risks": risks,
            "warnings": warnings,
            "duration": duration,
            "aspect": aspect,
            "style": style0,
            "material": material,
            "count": len(episodes),
        }

    core.set_task(pid, "shots", 70, "校验镜头数与时长")
    shots["ctl"] = bool(ctl)      # 渲染时决定段首带不带控制句
    if not shots["episodes"]:
        raise RuntimeError("AI 没有返回任何镜号，请重试")

    # 音色锁定：只在**同一条片内部**统一，保证 A/B/C/D 四段拼起来是同一个人。
    # 不同镜号是各自独立的成片，允许用不同音色，不跨条覆盖（2026-11-08 用户定）。
    swapped = _unify_voices(shots["episodes"])
    if swapped:
        shots["warnings"].append(
            "同一条片里同一个人的音色写了两个版本，已按该条首次出现的版本统一"
            "（A/B/C/D 拼起来才不会像换了配音）："
            + "、".join(f"{n}（镜号{no}）" for n, no, _, _ in swapped[:5]))

    # 切段：切点用 DP 对齐镜头边界，保证每段 ≤ seg_len（视频模型一次的时长）
    shots["seg_len"] = seg_len
    for ep in shots["episodes"]:
        ep["segments"] = _segments_of(ep, seg_len)
    seg_total = sum(len(ep["segments"]) for ep in shots["episodes"])
    want = max(1, int(round(int(shots.get("duration") or 0) / seg_len)))
    off = [(ep.get("no"), len(ep["segments"])) for ep in shots["episodes"]
           if len(ep["segments"]) != want]
    if off:
        shots["warnings"].append(
            "分段数对不上（" + "、".join(f"镜号{a} 切了 {b} 段" for a, b in off)
            + f"，预期 {want} 段）——镜头太少或太长，切不出来，出片前手工拆一下")
    stretched = [(ep.get("no"), g["letter"], g["scale"], len(g["beats"]))
                 for ep in shots["episodes"] for g in ep["segments"]
                 if abs(float(g.get("scale") or 1) - 1) > 0.2]
    if stretched:
        shots["warnings"].append(
            f"为了凑满 {seg_len:g} 秒，这些段的镜头时长被拉伸超过 20%（"
            + "、".join(f"镜号{a}-{b} ×{c}" for a, b, c, _ in stretched)
            + "）——节奏会比原设计松或紧，看片时留意")

    # 2026-11-07 用户定稿：只调一次 AI。切段全由程序做（_segments_of），
    # 按段重写（声线 tone/人物清单/约束/爽点）的第二次调用整条砍掉——
    # 这些字段由下游视频模型侧的 AI 自己写，程序兜底值够用。
    # 2026-11-08 清理：这条死链的代码（_split_run / _build_split_prompt /
    # _apply_split / _scrub_voices / SPLIT_SCHEMA）与 prompts/split.txt 已一并删除，
    # 别再照旧版加回来——产物里根本没有「AI 重写过的段字段」这一层。
    core.set_task(pid, "shots", 85, f"程序切分 {seg_total} 个生成单元，渲染文档")

    md = render_doc(shots, pname=p.get("name") or "")
    ts = time.strftime("%m%d%H%M%S")
    fname = f"{core.safe_name(p.get('name') or '')}_引流分镜_{duration}s_{ts}.md"
    out_path = core.abs_output(fname)
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(md)
        saved = fname
    except OSError:
        saved = ""

    # 原始 AI 响应单独落盘：出问题时能回溯「模型到底给了什么」，不用只看
    # normalize 之后的结果猜。不塞进 project.json——那份每次打开项目都要传给前端。
    # 并行时每条请求各有一份响应，按条存成列表（单条仍存成对象，保持老格式）。
    # 先在内存里序列化成功再开文件写：直接用 "w" 打开会在序列化失败时把
    # 上一次的存档截成空文件（2026-11-08 已经踩过一次）。
    raw_file = ""
    try:
        dump = {
            "gen": int(time.time()),
            "model": shots_model() or (core.load_config().get("llm_model") or ""),
            "duration": duration, "count": count, "aspect": aspect,
            "material": "custom" if script else "script",
            "source_chars": len(text),
            "calls": len(raws),
            "raw": raws[0] if len(raws) == 1 else raws,
        }
        blob = json.dumps(dump, ensure_ascii=False, indent=2)
        raw_dir = os.path.join(core.PROJECTS, pid)
        os.makedirs(raw_dir, exist_ok=True)
        with open(os.path.join(raw_dir, "shots_raw.json"), "w", encoding="utf-8") as f:
            f.write(blob)
        raw_file = "shots_raw.json"
    except Exception:  # noqa: BLE001
        # 存档只是排查用的附件，绝不能因为它让「已经生成好的分镜」整体失败。
        raw_file = ""

    p = core.load(pid) or p
    p["shots"] = {
        "gen": int(time.time()),
        "duration": duration, "count": shots["count"], "aspect": aspect,
        "style": shots.get("style"),   # 存下来：以后重新渲染（不需要重调 AI）也不丢自定义画风
        "ctl": bool(ctl),              # 控制句开关，重新排版时沿用
        "seg_len": seg_len,
        "material": "custom" if script else "script",
        "script_chars": len(script),
        "doc_title": shots["doc"]["title"],
        "characters": shots["characters"],   # 人物卡：以后传参考图、查「图一」是谁都靠它
        "raw_file": raw_file,
        "episodes": shots["episodes"],
        "seg_total": seg_total,
        "episodes_md": _episodes_md(shots["episodes"], shots),
        "warnings": shots["warnings"],
        "risks": shots["risks"],
        "doc": md,
        "file": saved,
    }
    # 第 2 步可能正在并行保存结构/成片，整份覆盖会把对方结果清掉——只写分镜这一块。
    core.save_merge(p, ["shots"])
    # 任务要收尾：留一个 pct=100 的 task 会一直被前端当成「还在跑」，
    # 把这一页所有按钮都锁死（2026-11-03 用户反馈：生成按钮点不动）。
    # 结果本身已经落在 p['shots'] 里，前端照样显示「已出 N 条 / M 段」。
    core.set_task(pid, None)


def shots_layout(pid: str, ctl: bool | None = None, style: str | None = None) -> dict:
    """按当前设置重排第 3 步的文档，**不调 AI、不花额度**。

    为什么需要它：控制句开关（ctl）和画风都只影响「排版」，不影响 AI 写出来的
    分镜内容。生成之后想改，只能重排——否则为了一个开关再烧一次调用。
    """
    p = core.load(pid)
    if not p or not (p.get("shots") or {}).get("episodes"):
        raise RuntimeError("还没有分镜产物，请先生成一次")
    sh = p["shots"]
    if ctl is not None:
        sh["ctl"] = bool(ctl)
    if style is not None:
        sh["style"] = (style or "").strip() or None

    data = {
        "doc_title": sh.get("doc_title"),
        "duration": sh.get("duration"),
        "seg_len": sh.get("seg_len") or SEG_LEN,
        "aspect": sh.get("aspect"),
        "style": sh.get("style"),
        "material": sh.get("material"),
        "ctl": bool(sh.get("ctl")),
        "episodes": sh.get("episodes") or [],
        "warnings": sh.get("warnings") or [],
        "risks": sh.get("risks") or [],
    }
    md = render_doc(data, pname=p.get("name") or "")
    sh["lay"] = int(sh.get("lay") or 0) + 1      # 前端靠它判断「文档变了，重画」
    sh["episodes_md"] = _episodes_md(data["episodes"], data)
    sh["doc"] = md

    # 落盘：沿用原来的文件名（覆盖），没有就按当前时间新起一个
    fname = sh.get("file") or (
        f"{core.safe_name(p.get('name') or '')}_引流分镜_"
        f"{int(sh.get('duration') or 60)}s_{time.strftime('%m%d%H%M%S')}.md")
    try:
        with open(core.abs_output(fname), "w", encoding="utf-8") as f:
            f.write(md)
        sh["file"] = fname
    except OSError:
        pass                                   # 输出目录不可写也不该让重排失败
    core.save_merge(p, ["shots"])
    return {"ok": True, "ctl": bool(sh.get("ctl")), "style": sh.get("style") or "",
            "file": sh.get("file") or "", "seg_total": sh.get("seg_total") or 0}
