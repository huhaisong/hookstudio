"""分析：把逐句台词交给 qwen-plus，让它出剪辑结构（按台词编号，不算秒数）。"""
from __future__ import annotations

import json
import os
import re

import requests

from . import core, reuse

PROMPT_DIR = core.PROMPTS
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
# 单次调用最多等多久。10 分钟结构这种长 JSON，慢模型（qwen3.8-max 实测）
# 会超过 5 分钟，默认放宽到 10 分钟；环境变量 LLM_TIMEOUT 可覆盖。
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT") or 600)


def _eps_label(eps) -> str:
    """把选集压成「第 20–40 集」这种短写法，给进度文案用。

    只写「选中的 21 集」会被读成「第 21 集」——带上真实范围就不会误解。
    不连续的集压成「第 1、3、7 集」；整段连续才写区间。
    """
    arr = sorted(set(int(x) for x in (eps or [])))
    if not arr:
        return ""
    if len(arr) == 1:
        return f"第 {arr[0]} 集"
    parts: list[str] = []
    s = prev = arr[0]
    for v in arr[1:] + [None]:
        if v is not None and v == prev + 1:
            prev = v
            continue
        parts.append(str(s) if s == prev else f"{s}–{prev}")
        if v is not None:
            s = prev = v
    return "第 " + "、".join(parts) + " 集"


def _custom_prompt_path(name: str) -> str:
    """用户自定义提示词的落盘位置：data/prompts_custom/<name>。

    放 data/ 而不是程序目录的 prompts/：这是**用户数据**，升级重装时安装器会
    备份/搬回 data/，改过的提示词不会丢；prompts/ 属于程序文件，随包覆盖。"""
    return os.path.join(core.DATA_DIR, "prompts_custom", name)


def _default_prompt(name: str) -> str:
    """读内置默认提示词（原样全文）。生成链路和编辑框都要从这里取基准。"""
    path = os.path.join(PROMPT_DIR, name)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
    except OSError as exc:
        raise RuntimeError(f"提示词文件缺失：{path}") from exc
    if not text:
        raise RuntimeError(f"提示词文件是空的：{path}")
    return text


# ── 「固定尾巴」机制 ──────────────────────────────────────────────────────
# 曾经（2026-11-05）把 JSON 交卷示例、字段语义这类「程序解析的命根子」藏成
# 「固定尾巴」：编辑框不显示、生成时强制用内置版。2026-11-08 用户要求
# **所有提示词都暴露出来、让用户自己改**，于是把尾巴全部拆成独立条目
# （plans_schema / hooks_schema / shots_schema），都能看能改。
# 改坏的风险由保存前的体检兜住（见 _check_schema）：JSON 解析不过直接拒绝保存，
# 不会等到跑完一次 API 才发现整批白做。
# 这里保留空表：_split_fixed 的调用点（_read_prompt / 编辑框 / 保存）逻辑不变，
# 只是现在没有任何文件被切走，等于整份都可编辑。
FIXED_TAIL_MARKS: dict[str, str] = {}

# 用户能改「输出格式」的文件。分两档验：
#   · 严格：整份就是合法 JSON（第 3 步分镜的骨架），直接 json.loads，报行号列号；
#   · 宽松：交卷示例里有「…」「[...]」这类省略写法（混剪 / 钩子的示例是示意，不是
#     真 JSON），强解析会误判。只验括号配平 + 关键键名还在——这两样坏了才是真坏。
_SCHEMA_STRICT = {"shots_schema.txt": "episodes"}
_SCHEMA_KEYS = {
    # 2026-11-21 用户定：剪辑结构不再要 label / rating / core / hook / bgm，
    # 一段只要 from/to（不挑高光，按剧情顺序装填）。这里的必检键随之收窄，
    # 否则用户在界面上保存这份格式会被「少了 rating」拦下。
    "plans_schema.txt": ("plans", "segments", "from", "to"),
    "hooks_schema.txt": ("hooks", "ep", "from", "to", "hook"),
}


def _check_schema(name: str, text: str) -> None:
    """保存「输出格式」前体检：改坏了当场拦下，别等跑完一次 API 才发现整批白做。"""
    key = _SCHEMA_STRICT.get(name)
    if key:
        body = text.replace("__DUR__", "60")
        i, j = body.find("{"), body.rfind("}")
        if i < 0 or j <= i:
            raise RuntimeError("这段是输出格式，必须含一个 JSON 示例（从 { 到 }），现在找不到")
        try:
            data = json.loads(body[i:j + 1])
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"JSON 示例解析不了：第 {exc.lineno} 行第 {exc.colno} 列附近「{exc.msg}」。"
                "多半是少逗号、引号没配对或括号没闭合，改好再保存") from exc
        if not isinstance(data, dict) or key not in data:
            raise RuntimeError(f'JSON 示例最外层少了 "{key}" 这个键——程序靠它取结果，删不得')
        return

    need = _SCHEMA_KEYS.get(name)
    if not need:
        return
    for op, cl in (("{", "}"), ("[", "]")):
        if text.count(op) != text.count(cl):
            raise RuntimeError(
                f"{op} 有 {text.count(op)} 个、{cl} 有 {text.count(cl)} 个，没配平——"
                "示例里有括号没闭合或多了，改好再保存")
    missing = [k for k in need if k not in text]
    if missing:
        raise RuntimeError(
            "示例里少了这些键：" + "、".join(missing) + "。程序靠它们读数，删不得")


def _check_user_tpl(name: str, text: str) -> None:
    """第 3 步「分镜要求」的必检项：输出格式段和 {schema} 占位符不能删。

    其余段（任务/规格/分段/画风/人物卡/素材）删了都能跑——只是少了那条要求；
    唯独输出格式删掉，模型不知道该回什么结构，整批分镜都解析不出来。
    """
    if name != "shots_user.txt":
        return
    if not re.search(r"(?m)^##\s*输出格式", text) or "{schema}" not in text:
        raise RuntimeError(
            "「## 输出格式」这一段和段里的 {schema} 不能删——程序靠它把 JSON 结构交给模型，"
            "删了模型就不知道该回什么格式，整批分镜都解析不出来")


def _split_fixed(name: str, text: str) -> tuple[str, str]:
    """把提示词切成（可编辑指令, 固定尾巴）。尾巴从标记行起（含标记行）到文末。"""
    mark = FIXED_TAIL_MARKS.get(name)
    if not mark:
        return text, ""
    lines = text.splitlines(keepends=True)
    for i, ln in enumerate(lines):
        if ln.strip() == mark:
            return "".join(lines[:i]).rstrip(), "".join(lines[i:]).strip()
    return text, ""


def _effective_editable(name: str) -> tuple[str, bool]:
    """当前生效的「可编辑指令」部分 + 是否自定义版。编辑框和生成链路共用。"""
    editable, _ = _split_fixed(name, _default_prompt(name))
    cpath = _custom_prompt_path(name)
    if os.path.isfile(cpath):
        try:
            with open(cpath, encoding="utf-8") as f:
                # 旧版自定义文件存的是全文（含尾巴），统一剥掉再算
                ctext = _split_fixed(name, _strip_dialogue_tail(f.read()))[0].strip()
            if ctext:
                return ctext, True
        except OSError:
            pass  # 读不出来就当没有，退回内置版，别让用户卡死
    return editable, False


def _read_prompt(name: str) -> str:
    """读提示词（生成链路用）：**用户自定义指令优先** + 内置固定尾巴。

    每次现读，所以保存、生成即生效，不用重启。"""
    editable, _ = _effective_editable(name)
    _, fixed = _split_fixed(name, _default_prompt(name))
    text = f"{editable}\n\n{fixed}" if fixed else editable
    if not text.strip():
        raise RuntimeError(f"提示词是空的：{name}")
    return text


def read_prompt_view(name: str) -> dict:
    """给「修改提示词」编辑框用的当前状态：生效中的指令部分 + 是否自定义版。

    编辑框里**只有指令**（2026-11-05 用户要求，两处收口）：
    ① 末尾「## 全剧台词」标题剥掉——Whisper 转写的台词由程序生成时统一拼接；
    ② 「固定尾巴」（输出格式等）整段隐藏——程序解析契约，改坏会导致生成失败，
       生成时由程序用内置默认版原样拼回，用户想改坏都没得改。"""
    content, is_custom = _effective_editable(name)
    return {"content": content, "is_custom": is_custom}


def save_custom_prompt(name: str, text: str) -> None:
    """保存用户改过的提示词（整份存；混进来的台词标题剥掉）。"""
    text = _split_fixed(name, _strip_dialogue_tail(text or ""))[0].strip()
    if not text:
        raise RuntimeError("提示词不能是空的")
    if len(text) > 60000:
        raise RuntimeError("提示词太长了（超过 6 万字），请精简后再保存")
    _check_schema(name, text)
    _check_user_tpl(name, text)
    path = _custom_prompt_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def reset_custom_prompt(name: str) -> None:
    """恢复内置默认：删掉自定义文件即可（文件不存在也当成功）。"""
    try:
        os.remove(_custom_prompt_path(name))
    except FileNotFoundError:
        pass


# 模板末尾的「## 全剧台词」标题行（含前后空行/加粗符）。台词由程序拼接，
# 标题也由程序统一加——模板和编辑框里都不该出现它。
# hooks.txt 的台词区标题是「## 这次要挑的集与台词」（只列选中集），一并剥掉。
_DIALOGUE_TAIL_RE = re.compile(
    r"\n*\s*#{1,6}\s*\**\s*(?:全剧台词|这次要挑的集与台词)\s*\**\s*$")


def _strip_dialogue_tail(tpl: str) -> str:
    """剥掉模板末尾的「## 全剧台词」标题行（用户手打了也剥，统一收口）。"""
    return _DIALOGUE_TAIL_RE.sub("", (tpl or "")).rstrip()


def _fill(tpl: str, **kw) -> str:
    """只替换 {word} 占位符；JSON 里的花括号原样保留，外部文件可以像普通文本一样写。"""
    def rep(m):
        k = m.group(1)
        return str(kw[k]) if k in kw else m.group(0)
    return _PLACEHOLDER.sub(rep, tpl)


# 默认 system：只有一句输出契约。以前放在 prompts/system.txt，2026-11-08 并进代码——
# 它没有界面编辑入口、每次都是同一句、39 字节，单独占一个文件只让盘点时多一个「这是
# 干嘛的」。第 1 步（funnel）/ plans / hooks 这些不传 system 的调用都用它。
_DEFAULT_SYSTEM = "只输出 JSON，不写任何解释。"


def system_prompt() -> str:
    """通用 system（第 1 步单片导流 / 混剪编排 / 挑钩子共用）。

    2026-11-08 用户要求所有提示词都能改：从 prompts/system.txt 现读，
    文件缺失（老版本装完升级、或用户误删）就退回内置那一句，不让整条链路卡死。
    """
    try:
        return _read_prompt("system.txt")
    except RuntimeError:
        return _DEFAULT_SYSTEM


def select_eps(p: dict, eps=None) -> tuple[list[int], list[int]]:
    """把外部传来的集号收成「项目里真实存在、且有台词」的集。

    返回 (选中的集, 被剔除的集)。eps 为空 = 全部有台词的集。
    剔除的分两种：项目里没有这个集号；有集号但还没整理出台词（选了也切不出东西）。

    为什么要单独一个函数：`_all_text`、`_global_lines`、`_to_segs`、reuse 记账
    必须用**同一份**选集口径，否则模型看到的编号和程序换算的位置会错位。
    """
    have = sorted(int(ep["index"]) for ep in p["episodes"] if ep.get("lines"))
    if not eps:
        return have, []
    want: list[int] = []
    for x in eps:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if i not in want:
            want.append(i)
    sel = sorted(set(want) & set(have))
    drop = sorted(set(want) - set(sel))
    return sel, drop


def _all_text(p: dict, gid: bool = False, pos: bool = False, plain: bool = False,
              eps=None) -> str:
    """全剧台词拼成一段文本。

    eps 给定 = 只拼这些集（2026-11-19 选集生成）。编号 n 在**选集内**从 1 连续，
    模型要引用的编号空间从 1225 个缩到几百个，串号概率随之下降。
    但「全剧第几分几秒」仍按原片真实时间轴累计——跳过的集照样占时间，
    模型才知道第 20 集离全剧开头有多远。

    gid=True 时用**全局连续编号**：把 27 集当成一条时间轴通读，模型可以用一个
    区间跨集选段（第 3 集末句 → 第 4 集开头），这正是短剧真实的叙事单位。
    AI 编排必须用这种编号，否则模型只能在集内切，天然拼不出连贯剧情。

    pos=True 时在每集标题标出「全剧第几分几秒」。AI 编排多套时要给每套分配
    「主用区域」（这套偏重前半还是后半），它得先有位置感——光看集号它不知道
    第 13 集是全剧正中间（2026-11-02 晚）。

    plain=True 只留集号和台词本身，不带序号、不带集内时间码（2026-11-08 用户定）。
    给第 3 步（重写分镜）用：那一步不吃原片时间码——时间码是本条成片的秒数，
    分镜要重新构思；序号和时间码占掉 484 句里的 6816 字（台词块 67%、整个提示词
    54%），既不参与判断，又会让模型把「(0.0-0.7)」误当成本条时间码或台词时长。
    第 2 步（剪辑编排）**必须**保留编号与时间码，那条路继续用默认值。
    """
    buf = []
    n = 0
    at = 0.0          # 累计到这一集为止的全剧秒数
    sel = {int(x) for x in eps} if eps else None
    for ep in p["episodes"]:
        lines = ep.get("lines") or []
        if sel is not None and int(ep["index"]) not in sel:
            # 不参与这批的集：不写进台词块，但它占的时间照算——否则后面几集的
            # 「全剧第几分几秒」会整体前移，模型对位置的判断就假了。
            at += float(ep["duration"])
            continue
        if not lines:
            if plain:
                buf.append(f"\n【第{ep['index']:02d}集】【无对白】")
            else:
                buf.append(f"\n【第{ep['index']:02d}集】【无对白】总长 {ep['duration']:.0f} 秒")
            at += float(ep["duration"])
            continue
        if plain:
            buf.append(f"\n【第{ep['index']:02d}集】")
            buf.extend(str(ln["text"]).strip() for ln in lines)
        elif gid:
            head = (f"\n【第{ep['index']:02d}集】总长 {ep['duration']:.0f} 秒，"
                    f"共 {len(lines)} 句（全局编号 {n + 1}-{n + len(lines)}）")
            if pos:
                head += f"｜全剧第 {int(at) // 60}:{int(at) % 60:02d} 起"
            buf.append(head)
            at += float(ep["duration"])
            for ln in lines:
                n += 1
                buf.append(f"{n}. ({ln['start']:.1f}-{ln['end']:.1f}) {ln['text']}")
        else:
            buf.append(f"\n【第{ep['index']:02d}集】总长 {ep['duration']:.0f} 秒，共 {len(lines)} 句")
            for i, ln in enumerate(lines, 1):
                buf.append(f"{i}. ({ln['start']:.1f}-{ln['end']:.1f}) {ln['text']}")
    return "\n".join(buf)


def ep_offsets(p: dict) -> dict[int, float]:
    """每集在整个素材时间轴上的起点（按集号顺序首尾相接）。
    跨集选段、判断「两段是否紧挨着」都靠它。"""
    off, out = 0.0, {}
    for ep in p["episodes"]:
        out[ep["index"]] = off
        off += float(ep.get("duration") or 0)
    return out


def _global_lines(p: dict, eps=None) -> list[dict]:
    """全局编号 -> 真实位置：gid / ep / start / end（集内秒）/ gs / ge（全局秒）。

    eps 给定 = 只编号这些集，gid 在选集内从 1 连续（和 `_all_text(eps=)` 同一口径）。
    **gs/ge 仍按原片真实时间轴算**（`ep_offsets` 用完整 p，不按选集重排）——
    这是选集最容易踩的坑：如果按选集重排时间轴，第 3 集和第 5 集之间就不再有
    第 4 集的时长，`_stitch_blocks` 会认为两段「只隔几秒」把它们缝成一场戏，
    剪出来就会跨过一整集、剧情直接断掉。
    """
    off = ep_offsets(p)
    sel = {int(x) for x in eps} if eps else None
    out = []
    for ep in p["episodes"]:
        if sel is not None and int(ep["index"]) not in sel:
            continue
        for ln in ep.get("lines") or []:
            out.append({
                "ep": ep["index"], "start": ln["start"], "end": ln["end"],
                "gs": off[ep["index"]] + ln["start"],
                "ge": off[ep["index"]] + ln["end"],
            })
    for i, r in enumerate(out, 1):
        r["gid"] = i
    return out


def _parse_json(content: str) -> dict:
    """从模型回复里取 JSON：去代码围栏、取最外层大括号、容忍尾逗号。"""
    content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.M).strip()
    start, end = content.find("{"), content.rfind("}")
    if start < 0:
        raise RuntimeError("模型没有返回 JSON")
    blob = content[start:end + 1]
    try:
        return json.loads(blob)
    except json.JSONDecodeError as exc:
        fixed = re.sub(r",\s*([}\]])", r"\1", blob)  # 尾逗号是模型最常犯的错
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            tail = blob[max(0, exc.pos - 60):exc.pos + 60].replace("\n", " ")
            raise RuntimeError(
                f"AI 返回的 JSON 不完整（第 {exc.lineno} 行附近：…{tail}…）——"
                "通常是被截断或引号没转义，重试一次通常就好") from exc


def _endpoint(cfg: dict) -> tuple[str, dict]:
    """这一单打哪个地址、带哪个头。正式调用和连通性测试共用，避免两套规则。"""
    if cfg["key"]:
        return (f"{cfg['base'].rstrip('/')}/chat/completions",
                {"Authorization": f"Bearer {cfg['key']}", "Content-Type": "application/json"})
    # 没填自己的 Key：走官方体验代理，按次数扣额度。
    # 令牌只认**账号令牌**——不登录就是空串，服务端回 401，界面提示「请重新登录」。
    proxy = (cfg.get("proxy_url") or core.PROXY_URL_DEFAULT).rstrip("/")
    return (f"{proxy}/api/proxy/chat/completions",
            {"X-HS-Token": core.ai_token(), "Content-Type": "application/json"})


# 一眼就能判定「重试也没用」的报错：欠费、没开通、Key 错。
# 这些不能走「摘参数重发」的阶梯，否则同一个错会连着发三四遍。
_HARD_ERRORS = ("arrearage", "overdue", "access denied", "token is unusable",
                "invalid_api_key", "incorrect api key", "model_not_found",
                "model not exist", "does not exist", "arrearage")


def _http_error(status: int, text: str, cfg: dict) -> str:
    """把各家五花八门的报错翻成人话。

    2026-11-04 实测：百炼欠费时只回一个 400 + `{"type":"Arrearage"}`，
    界面原样甩出去就是一大串英文字典，用户根本看不出「该充值了」。
    同类还有「模型没开通」「产品没开通」「Key 失效」，各家的文案都不一样，
    统一在这里认一遍关键词，认不出来再退回原文截断。
    """
    low = (text or "").lower()
    who = "DeepSeek 官方" if cfg.get("provider") == "deepseek" else "百炼"
    if "arrearage" in low or "overdue" in low:
        tip = (f"{who}账号欠费了：余额或资源包已用尽，所有模型都会报这个错。"
               f"到{'DeepSeek 开放平台' if who.startswith('DeepSeek') else '阿里云百炼控制台'}充值后立刻恢复，Key 不用换。")
        if who == "百炼" and (core.load_config().get("ds_key") or "").strip():
            tip += " 想先用着：在设置里把模型换成 deepseek-v4-pro（走 DeepSeek 官方 Key，不受影响）。"
        return tip
    if status == 401 or "invalid_api_key" in low or "incorrect api key" in low:
        return f"{who}的 Key 不对或已失效（401），到设置里重新填一次。"
    if "token is unusable" in low:
        return ("这个模型所属的产品还没开通（接口原话 token is unusable）。"
                "到百炼「模型广场」找到它申请开通，开通后立刻可用。")
    if "access denied" in low or "model.accessdenied" in low or status == 403:
        return ("没有这个模型的调用权限（403）：多半是还没在百炼「模型广场」开通它，"
                "或者是 Key 绑定的业务空间没加这个模型。")
    if "model_not_found" in low or "model not exist" in low or "does not exist" in low or status == 404:
        return (f"模型名对不上（{status}）：检查拼写和大小写，比如 ZHIPU/GLM-5.2 要带厂商前缀。"
                "换过接口地址的话，也要确认那边有这个模型。")
    if status == 429 or "throttling" in low or "rate limit" in low:
        return "被限流了（429）：同一账号并发太多或今日额度用完，等一两分钟再试，或换个模型。"
    if "data_inspection" in low or "inappropriate" in low:
        return "内容被审核拦了（输入或输出触发敏感词），把台词里那几句删掉再试。"
    if "range of input length" in low or "too long" in low or "maximum context" in low:
        return "输入太长了：这个模型的上下文装不下整部剧，换 1M 上下文那档（GLM-5.3）或减少集数。"
    if status >= 500:
        return f"对面服务端出错了（{status}），不是你的问题，过几分钟再试。"
    return f"接口返回 {status}: {(text or '').strip()[:220]}"


def _call(prompt: str, system: str | None = None, json_mode: bool = True,
          model: str | None = None, timeout: int | None = None) -> dict:
    """model 不为空时覆盖配置里的模型（第 3 步写分镜可单独配 shots_model），
    Key 仍按配置走同一条路由——所以探针、结构、分镜三家规则永远一致。

    timeout 只给「第 2 步多模型对比」用：那是一个模型拖垮整批的场景，
    多组同时跑时单组没必要等满 LLM_TIMEOUT。第 3 步 / 钩子前置不传，
    仍用全局 600 秒（它们的单次调用实测就要 170-230 秒，砍短会误杀）。

    2026-11-07 实测（重要，别轻易改这里）：qwen3.5-plus 默认开思考，
    enable_thinking=false 能把思考 token 关到 0、第 3 步快 3 倍多，但
    **关掉之后模型不守数字规格**——同样 60s×1 条，改动前 14 镜/228 字/0 警告，
    关思考后连续两次 20 镜/441、454 字/3 警告，程序被迫拆镜和重分配时长。
    思考过程在帮它规划「分几镜、写多少字」。所以这里故意不加关思考开关，
    提速方案需连同提示词的数字规格一起重新设计并实测，不能只换参数。
    """
    cfg = core.route(model) if model else core.llm()
    url, headers = _endpoint(cfg)
    budget = int(timeout or LLM_TIMEOUT)

    body = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system or system_prompt()},
                     {"role": "user", "content": prompt}],
        "temperature": 0.3,
        # 分镜/编排的输出都是长 JSON。3 条 60 秒分镜约 7000-9000 token，
        # 卡在 8k 时模型会把每条都写短（实测每条台词从 227 字掉到 150 字），
        # 所以放到 16k；不认这个上限的模型在下面回退到 8k。
        "max_tokens": 16384,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    model_l = (cfg.get("model") or "").lower()
    if cfg.get("provider") == "deepseek":
        # DeepSeek 官方 V4：思考也算输出额度，降强度 + 放大上限
        body["reasoning_effort"] = "low"
        body["max_tokens"] = 32768
    elif _is_thinker(model_l):
        # 百炼上的推理模型：**先试关掉思考**——最省额度也最快（实测 GLM-5.2、
        # MiniMax-M3、step-5 都能关）。GLM-5.3 这类「始终思考」的会 400，
        # 下面按顺序退到 reasoning_effort=low、再退回原样，不让它整批失败。
        body["enable_thinking"] = False

    def _post(payload: dict):
        try:
            # 可中止版：这趟请求可能等 600 秒，中间没有进度上报。包一层之后，
            # 用户点「中止」最多 0.2 秒就能让本步立刻退出；请求格式、超时、
            # 错误翻译、参数回退全都不变。TaskCancelled 继承 BaseException，
            # 下面两个 except 不会把它吃掉，会直达 start_task 的收尾。
            return core.ai_post_cancelable(
                lambda: requests.post(url, headers=headers, json=payload, timeout=budget))
        except requests.Timeout as exc:
            # 「超时」和「连不上」必须分开说：超时的意思是请求发出去了、对面还在算，
            # 换网络、换 Key 都没用，能做的只有换生成方式或缩小任务。
            # 原文 Read timed out 会被读成「网络断了」；这里也不写模型代号和实测数字，
            # 那是给作者看的实现备注，不该出现在用户能看到的报错里。
            raise RuntimeError(
                f"等了 {budget} 秒还没返回：这次生成太慢或排队太久，不是网络断了，"
                "也不是配置的问题。换一个生成方式，或者把套数 / 单套时长调小一点再试。") from exc
        except requests.RequestException as exc:
            if cfg["key"]:
                raise RuntimeError(f"连不上 AI 接口：{exc}") from exc
            raise RuntimeError("体验服务暂时连不上，请稍后再试，或换成你自己的服务") from exc

    resp = _post(body)
    # 欠费/没开通/Key 错这类错误，摘参数重发也还是同一个错，白等三四轮
    def _hard(r) -> bool:
        low = (r.text or "").lower()
        return any(k in low for k in _HARD_ERRORS)

    # 有些模型输出上限只有 8k：不认 16k 就退回去重发
    if resp.status_code == 400 and "max_tokens" in resp.text and not _hard(resp):
        body["max_tokens"] = 8192
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        resp = _post(body)
    # 有些模型/代理不认 response_format：退回普通模式再试一次
    if resp.status_code == 400 and json_mode and "response_format" in resp.text and not _hard(resp):
        body.pop("response_format", None)
        resp = _post(body)
    # 关不掉思考的模型（如 GLM-5.3「始终思考」）：退到降强度 + 放大上限
    if resp.status_code == 400 and body.get("enable_thinking") is not None and not _hard(resp):
        body.pop("enable_thinking", None)
        body["reasoning_effort"] = "low"
        body["max_tokens"] = 32768
        resp = _post(body)
    # 连 reasoning_effort 都不认：摘掉参数，退回原样
    if resp.status_code == 400 and body.get("reasoning_effort") and not _hard(resp):
        body.pop("reasoning_effort", None)
        resp = _post(body)
    if resp.status_code != 200:
        if resp.status_code == 402:
            raise RuntimeError("今天的免费次数已用完（每天 10 次，0 点自动回满）。"
                               "急用的话找管理员补次数。")
        if resp.status_code in (401, 403) and not cfg["key"]:
            # 这两个码来自我们自己的代理：401 = 没登录或令牌过期，403 = 被运营方封禁。
            # 走 _http_error 会被翻成「Key 不对」，用户去设置页翻半天也找不到问题。
            try:
                msg = ((resp.json().get("error") or {}).get("message") or "").strip()
            except Exception:  # noqa: BLE001
                msg = ""
            raise RuntimeError(msg or ("登录已失效，请重新登录后再试" if resp.status_code == 401
                                       else "没有调用 AI 的权限，请联系管理员"))
        if resp.status_code == 429:
            # 429 有两种来源：体验代理的「次数用完」和自己 Key 的「账号级限流」，
            # 提示必须分开——自己的 Key 报「明天再试」会让人以为 Key 坏了。
            if cfg["key"]:
                raise RuntimeError("用的人有点多，请稍后再试")
            raise RuntimeError("这个网络今天用得太频繁了（同一网络共用额度），请稍后再试；"
                               "用你自己的服务不受这个限制。")
        raise RuntimeError(_http_error(resp.status_code, resp.text, cfg))
    choice = resp.json()["choices"][0]
    content = choice.get("message", {}).get("content") or ""
    if choice.get("finish_reason") == "length" and not content.strip():
        # 推理型模型（qwen3.5-plus、GLM-5.x、MiniMax-M3、step-5 等）把思考算进
        # completion_tokens，16k 常被 thinking 吃光、正文一个字不剩。按「最省额度」
        # 顺序重试，任何一种拿到正文就用它：
        #   enable_thinking=False —— 实测 qwen3.5-plus 能关到 0 个思考 token；
        #                          GLM-5.3 不认会秒回 400，接着退下一档；
        #   reasoning_effort=low —— 百炼统一参数，GLM 明确只认 low/high/max；
        #   都不认就只放大上限 + 要求不写思考过程。
        # 2026-11-07 修正顺序：原来先试 low，但 qwen3.5-plus 带 low 仍烧 198 个
        # 思考 token，等于白等一整轮；enable_thinking 排在第三位才轮到。
        def _again(extra: dict) -> str:
            b = dict(body)
            # 两个思考参数不能叠加：同时带上很多模型直接 400，又白等一轮。
            b.pop("enable_thinking", None)
            b.pop("reasoning_effort", None)
            b["max_tokens"] = 32768
            b.update(extra)
            msgs = list(b.get("messages") or [])
            if msgs:
                msgs[0] = {"role": "system",
                           "content": msgs[0]["content"] + "\n\n直接输出最终 JSON，不要输出思考过程或解释。"}
            b["messages"] = msgs
            r = _post(b)
            if r.status_code != 200:
                return ""
            return (r.json()["choices"][0].get("message", {}).get("content") or "")

        for extra in ({"enable_thinking": False}, {"reasoning_effort": "low"}, {}):
            got = _again(extra)
            if got.strip():
                content = got
                choice = {"finish_reason": "stop"}
                break
    if choice.get("finish_reason") == "length":
        # 被上限截断时 JSON 一定不完整，别让它以「JSON 解析失败」的面目出现
        if not content.strip():
            raise RuntimeError(
                "模型的思考过程把额度烧完了，正文一个字没写（推理型模型常见）。"
                "已自动把推理强度降到 low 并放大上限；如果还这样，就把目标时长/条数改小一点。")
        raise RuntimeError(
            f"模型输出被截断了（已写 {len(content)} 字，达到上限）。"
            "把条数改少一点（先出 1-2 条）或把单条时长调短，再试一次。")
    if not content.strip():
        raise RuntimeError("模型返回了空内容，请重试一次。")
    return _parse_json(content)


# 会「先想再答」的模型：思考 token 算在输出额度里，不降强度就轮不到正文。
# 直接写死名字不划算（每家都在上新），按厂商/型号关键词判更耐用。
_THINK_KEYS = ("glm", "minimax", "step-5", "stepfun", "kimi-k3", "kimi/k3", "deepseek-v4", "v4.1-flash")


def _is_thinker(model: str) -> bool:
    m = (model or "").lower()
    # deepseek-v4.1-flash 虽然名字命中 deepseek-v4 / v4.1-flash，但实测关思考
    # 会退化成「换标题重剪」：2026-11-19 同一部剧横评，关思考套间重合 96%
    # （套1与套3有 33/35 块完全相同），保留思考只有 47%（优于 GLM-5.2 的 72%）。
    # 故此处精确排除，让 _call / probe 都不给它加 enable_thinking=False。
    if "v4.1-flash" in m:
        return False
    return any(k in m for k in _THINK_KEYS)


PROBE_TEXT = "这是一次连通性测试，请只回复四个字：接口可用"


def probe(model: str | None = None, llm_key: str | None = None,
          ds_key: str | None = None, timeout: int = 120) -> dict:
    """连通性测试：按当前（或传入的）配置发一次极小的请求，当场说清「能不能用」。

    为什么值得单独做一个：2026-11-04 的 13 模型横评里 4 个失败，用户每次都是
    等十几分钟任务跑完才知道「这个模型我没开通 / 账号欠费 / 被限流」。
    探针只要三四秒，就能把这三类原因分开。

    它和正式调用共用 core.route + _endpoint + 同一套降级阶梯，所以
    「探针通过」基本等于「正式调用能通」；反过来探针失败时，正式调用也一定会失败。

    传 None 表示用已保存的配置；设置页会传用户在输入框里刚改、还没保存的值，
    这样「先测通再保存」不会测了旧配置。
    """
    import time as _t

    t0 = _t.time()
    cfg = core.route(model, llm_key, ds_key)
    has_ds = bool((core.load_config().get("ds_key") or "").strip())
    out = {
        "ok": False, "model": cfg["model"], "provider": cfg["provider"],
        "base": cfg["base"], "has_key": bool(cfg["key"]), "has_ds_key": has_ds,
        "sec": 0.0, "status": 0, "detail": "", "reply": "", "tokens": 0,
        "tries": 0,
    }
    if not cfg["model"]:
        out["detail"] = "还没填模型名。"
        return out

    body = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": PROBE_TEXT}],
        "temperature": 0,
        "max_tokens": 1024,
    }
    if cfg["provider"] == "deepseek":
        body["reasoning_effort"] = "low"
        body["max_tokens"] = 4096
    elif _is_thinker(cfg["model"]):
        body["enable_thinking"] = False
    url, headers = _endpoint(cfg)

    for _ in range(4):
        out["tries"] += 1
        try:
            r = requests.post(url, headers=headers, json=body, timeout=timeout)
        except requests.RequestException as exc:
            # 超时和「连不上」要分开说：前者是模型排队/太慢，后者是网络或地址错
            if "timed out" in str(exc).lower() or "timeout" in str(exc).lower():
                out["detail"] = (f"{timeout} 秒内没响应。多半是这个模型排队太狠或本身极慢"
                                 "（step-5 实测 34 分钟都没出结果），换一个模型吧。")
            else:
                out["detail"] = f"连不上接口：{exc.__class__.__name__}（检查网络、接口地址，或代理）"
            break
        out["status"] = r.status_code
        if r.status_code == 200:
            try:
                j = r.json()
            except Exception:  # noqa: BLE001
                out["detail"] = "接口通了，但返回的不是 JSON（地址是不是填错了？）"
                break
            ch = (j.get("choices") or [{}])[0]
            msg = ch.get("message") or {}
            content = (msg.get("content") or "").strip()
            out["tokens"] = int((j.get("usage") or {}).get("completion_tokens") or 0)
            out["reply"] = content[:60]
            out["ok"] = bool(content)
            out["sec"] = round(_t.time() - t0, 1)
            if content:
                out["detail"] = f"通了：{out['sec']} 秒返回「{content[:24]}」"
            else:
                out["detail"] = ("接口通了、Key 也对，但模型没给正文（只有思考过程）——"
                                 "这是推理模型的常见现象，正式生成时程序会自动降思考强度、"
                                 "放大输出上限，能出结果，只是更慢。")
            break
        # 参数不认 → 按「关思考 → 降强度 → 摘参数」退，和 _call 的顺序一致
        low = (r.text or "").lower()
        if r.status_code == 400 and not any(k in low for k in _HARD_ERRORS):
            if body.get("enable_thinking") is not None:
                body.pop("enable_thinking", None)
                body["reasoning_effort"] = "low"
                body["max_tokens"] = 32768
                continue
            if body.get("reasoning_effort"):
                body.pop("reasoning_effort", None)
                continue
            if body.get("max_tokens") != 8192:
                body["max_tokens"] = 8192
                continue
        out["sec"] = round(_t.time() - t0, 1)
        if r.status_code == 402:
            out["detail"] = "今天的免费次数已用完（每天 10 次，0 点回满），填自己的 Key 可继续。"
        else:
            out["detail"] = _http_error(r.status_code, r.text, cfg)
        break
    if not out["detail"]:
        out["detail"] = "测试没跑完，请再点一次。"
    return out


# ---- 可用模型清单（第 2 步下拉 / 设置页候选的来源）---------------------------
# 只列「百炼或 DeepSeek 官方真有的型号」。2026-11-04 全量探针实测：
#   qwen3.5、qwen3.5-397b、deepseek-chat、ZHIPU/GLM-4.7、kimi/kimi-k2.5 → 全 404，
# 已剔除。**加新名字前先 probe 一次**，凭印象加等于给用户一个注定报错的选项。
MODEL_CANDIDATES = [
    "qwen3.5-plus", "qwen-plus", "qwen-max",
    "deepseek-v4-pro", "deepseek-flash", "deepseek-v4.1-flash",
    "ZHIPU/GLM-5.3", "ZHIPU/GLM-5.2", "ZHIPU/GLM-5",
    "MiniMax/MiniMax-M2.7", "MiniMax/MiniMax-M3",
    "kimi/kimi-k3.1-pro", "stepfun/step-5-preview",
    # 2026-11-22 加：8 套档唯一又快又稳的（88 秒/8 套、套间重合 77%）。
    # 加进这张表才会被「检测可用模型」探到；漏了它前端会按旧缓存把它藏起来。
    "glm-5.3-prime",
]
_MODELS_CACHE = os.path.join(core.DATA_DIR, "models_cache.json")


def available_models(refresh: bool = False, ttl: int = 6 * 3600) -> dict:
    """哪些模型此刻真能通——前端下拉只放这里 ok 的型号。

    为什么要做：13 模型横评里 4 个失败全是「名字对不上 / 没开通 / 欠费」，
    用户不该在下拉里选到一个注定 404 的名字。探针单个 0.2-7 秒，14 个开 8 线程
    大约 8 秒，所以结果落盘缓存 6 小时：平时读缓存（毫秒级），
    点「检测可用模型」才现场重测（refresh=True）。
    """
    import time as _t
    from concurrent.futures import ThreadPoolExecutor

    now = _t.time()
    if not refresh:
        # 只读缓存，过期也先用着——页面加载时不能卡 8 秒去探 14 个模型；
        # 没有缓存就返回空表，前端继续用它自带的「已知能用」清单。
        try:
            with open(_MODELS_CACHE, encoding="utf-8") as f:
                cached = json.load(f)
            cached["cached"] = True
            cached["stale"] = now - float(cached.get("checked_at") or 0) >= ttl
            return cached
        except (OSError, ValueError):
            return {"checked_at": 0, "cached": False, "stale": True, "items": []}

    def one(model: str) -> dict:
        try:
            r = probe(model, timeout=25)
            return {"model": model, "ok": bool(r["ok"]), "provider": r["provider"],
                    "sec": round(float(r["sec"]), 1), "status": int(r["status"]),
                    "detail": (r["detail"] or "")[:120]}
        except Exception as exc:  # noqa: BLE001  一个模型炸了不能带走整张表
            return {"model": model, "ok": False, "provider": "", "sec": 0.0,
                    "status": 0, "detail": f"{type(exc).__name__}: {exc}"[:120]}

    with ThreadPoolExecutor(max_workers=8) as ex:
        items = list(ex.map(one, MODEL_CANDIDATES))
    out = {"checked_at": now, "cached": False, "items": items}
    try:
        with open(_MODELS_CACHE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
    except OSError:
        pass
    return out


def _line_dur(ep: dict, a: int, b: int) -> float:
    lines = ep["lines"]
    a = max(1, min(a, len(lines)))
    b = max(a, min(b, len(lines)))
    return lines[b - 1]["end"] - lines[a - 1]["start"]


def _to_edl(p: dict, data: dict) -> list[dict]:
    by_index = {ep["index"]: ep for ep in p["episodes"]}
    out = []
    for seg in data.get("segments") or []:
        ep = by_index.get(int(seg.get("ep") or 0))
        if not ep or not ep.get("lines"):
            continue
        n = len(ep["lines"])
        a = max(1, int(seg.get("from") or 1))
        b = max(a, min(int(seg.get("to") or a), n))
        start = max(0.0, ep["lines"][a - 1]["start"] - 0.20)
        end = min(ep["duration"], ep["lines"][b - 1]["end"] + 0.30)
        if end - start < 3:
            continue
        out.append({
            "ep": ep["index"], "from": a, "to": b,
            "label": str(seg.get("label") or "")[:40],
            "start": round(start, 3), "end": round(end, 3),
        })
    return out


def _total(edl: list[dict]) -> float:
    return sum(s["end"] - s["start"] for s in edl)


def _chrono(p: dict, edl: list[dict]) -> list[dict]:
    """把结构按「集号 + 集内起点」重排。

    2026-11-04 用户拍板：导流片也必须是严格集序，不许把后面的戏提到片头。
    只在提示词里写「不许调序」模型仍会照做（它优先满足「开场抓人」），所以
    顺序必须由程序兜底——模型只负责选内容，顺序归我们。
    """
    return sorted(edl, key=lambda s: (int(s.get("ep") or 0), float(s.get("start") or 0)))


def _fit(p: dict, edl: list[dict], target: float) -> list[dict]:
    """用整句收缩/扩张，把总时长拉进目标区间。"""
    by_index = {ep["index"]: ep for ep in p["episodes"]}
    total = _total(edl)
    if total <= 0:
        return edl

    if total > target * 1.02:
        ratio = target / total
        for seg in edl:
            ep = by_index[seg["ep"]]
            want = (seg["end"] - seg["start"]) * ratio
            while seg["to"] > seg["from"] and _line_dur(ep, seg["from"], seg["to"]) > want:
                seg["to"] -= 1
            seg["end"] = round(min(ep["duration"], ep["lines"][seg["to"] - 1]["end"] + 0.3), 3)
    elif total < target * 0.95:
        for i, seg in enumerate(edl):
            ep = by_index[seg["ep"]]
            limit = len(ep["lines"])
            for nxt in edl[i + 1:]:
                if nxt["ep"] == seg["ep"]:
                    limit = nxt["from"] - 1
                    break
            while _total(edl) < target and seg["to"] < limit:
                seg["to"] += 1
                seg["end"] = round(min(ep["duration"], ep["lines"][seg["to"] - 1]["end"] + 0.3), 3)
            if _total(edl) >= target:
                break
    return edl


def edl_text(edl: list[dict]) -> str:
    rows = ["集|起|止|标签"]
    for s in edl:
        rows.append(f"{s['ep']:02d}|{s['start']:.2f}|{s['end']:.2f}|{s['label']}")
    return "\n".join(rows)


def parse_text(text: str) -> list[dict]:
    edl = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("集"):
            continue
        parts = re.split(r"[|｜,，\t]", line)
        if len(parts) < 3:
            continue
        try:
            ep = int(re.sub(r"\D", "", parts[0]) or 0)
            start = float(parts[1])
            end = float(parts[2])
        except ValueError:
            continue
        if ep <= 0 or end <= start:
            continue
        edl.append({
            "ep": ep, "from": 0, "to": 0,
            "start": round(start, 3), "end": round(end, 3),
            "label": (parts[3].strip() if len(parts) > 3 else ""),
        })
    return edl


def _pipeline(p: dict, kind: str, tpl_name: str, strict: bool, variation: str = "") -> tuple[list[dict], dict]:
    """公共管线：组装台词 → 调模型 → 落到时间轴 → 质检重试 → 配平。返回 (edl, 模型原始 JSON)。"""
    pid = p["id"]
    target = float(p["target_minutes"]) * 60
    eps = [ep for ep in p["episodes"] if ep.get("lines")]
    if not eps:
        raise RuntimeError("还没有台词，请先整理台词")

    src_total = sum(ep["duration"] for ep in eps)
    per_ep = int(target / max(1, len(eps)))
    text = _all_text(p)
    core.set_task(pid, kind, 5, "正在整理台词，准备挑选片段")
    core.set_task(pid, kind, 30, f"正在读 {len(text)} 字台词，挑选片段")
    data = _call(_fill(
        _read_prompt(tpl_name),
        name=p["name"], n_ep=len(p["episodes"]), src_min=round(src_total / 60, 1),
        target=int(target), per_ep=per_ep, variation=variation, lines=text))
    edl = _to_edl(p, data)
    if not edl:
        raise RuntimeError("模型给的段落都无法落到台词上")

    core.set_task(pid, kind, 70, f"初稿 {len(edl)} 段 / {_total(edl):.0f} 秒，正在配平")
    if strict:
        need_fix = abs(_total(edl) - target) > target * 0.10 or len({s["ep"] for s in edl}) < len(eps) * 0.85
    else:
        need_fix = abs(_total(edl) - target) > target * 0.10
    if need_fix:
        missing = sorted({ep["index"] for ep in eps} - {s["ep"] for s in edl})
        fix = (
            f"你上一次给的结构有问题：{len(edl)} 段、合计 {_total(edl):.0f} 秒，目标是 {int(target)} 秒。\n"
            + (f"而且这些集完全没被选中：第 {missing} 集——除无对白集外，每一集都必须至少出现一次。\n" if strict and missing else "")
            + ("从第 1 集开始按集号排列；stop 落在最后一条片段内部。\n" if strict else "时长不够就多给几段，同一集可以取多个不重叠片段。\n")
            + "请重新设计，把总时长做到目标值附近，输出同样格式的 JSON。\n"
            "上一次的结构：\n" + json.dumps(data.get("segments") or [], ensure_ascii=False)
            + "\n\n台词如下：\n" + text
        )
        try:
            data2 = _call(fix)
            edl2 = _to_edl(p, data2)
            if edl2 and abs(_total(edl2) - target) < abs(_total(edl) - target):
                edl, data = edl2, data2
        except Exception:  # noqa: BLE001
            pass

    edl = _chrono(p, edl)
    edl = _fit(p, edl, target)
    return edl, data


def _note_text(data: dict, stop: dict | None = None) -> str:
    parts = []
    summary = str(data.get("summary") or "").strip()
    if summary:
        parts.append("主题：" + summary)
    if stop:
        parts.append(f"卡点：第{stop.get('ep')}集 第{stop.get('line')}句 —— {stop.get('reason') or ''}")
    parts.extend(str(n).strip() for n in (data.get("notes") or []) if str(n).strip())
    return "；".join(parts)


def _core_gs(seg: dict, glines: list[dict], off: dict[int, float]) -> float | None:
    """把模型标的 core（全局句号）换算成全局秒，作为收紧时的锚点。"""
    try:
        c = int(seg.get("core") or 0)
    except (TypeError, ValueError):
        return None
    if c <= 0:
        return None
    for r in glines:
        if r["gid"] == c:
            return round(off.get(r["ep"], 0.0) + (float(r["start"]) + float(r["end"])) / 2, 3)
    return None


def _to_segs(p: dict, data: dict, min_len: float = 3.0, eps=None) -> list[dict]:
    """把模型的片段池 JSON 落到真实时间轴，附上评分。

    两种格式都吃：
      新（全局编号）：{"from": 128, "to": 210, ...}  —— 允许跨集，落地时按集拆成多片
      旧（集内编号）：{"ep": 3, "from": 12, "to": 20, ...}
    每片都带 blk（同一块的编号）、gs/ge（全局秒），组装时靠它们判断「是否紧挨着」。
    一块跨集时拆成多片，但 blk 相同：渲染时仍是连续画面，只是分文件切。

    eps 给定 = 本批只认这些集：gid 按选集编号解析（和 `_all_text(eps=)` 同口径），
    模型如果越界引用了没参与的集，整片丢弃——宁可少一块，也不让成片里冒出
    用户明确排除掉的集。
    """
    # 模型不一定守规矩：qwen-max 实测把 segments 写成了字符串数组，
    # 直接 .get() 会抛 'str' object has no attribute 'get'，整批白跑。
    # 这里只做「能解析就继续、不能就跳过」，不猜它想问什么。
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return []
    if not isinstance(data, dict):
        return []
    segs_in = data.get("segments")
    if isinstance(segs_in, str):
        try:
            segs_in = json.loads(segs_in)
        except Exception:
            segs_in = []
    if not isinstance(segs_in, list):
        segs_in = []

    by_index = {ep["index"]: ep for ep in p["episodes"]}
    off = ep_offsets(p)
    glines = _global_lines(p, eps)
    sel = {int(x) for x in eps} if eps else None
    out = []

    def _push(ep_i, a_line, b_line, meta, blk):
        if sel is not None and int(ep_i) not in sel:
            return
        ep = by_index.get(ep_i)
        if not ep or not ep.get("lines"):
            return
        start = max(0.0, a_line["start"] - 0.20)
        end = min(ep["duration"], b_line["end"] + 0.30)
        if end - start < 2.0:
            return
        item = {
            "ep": ep_i,
            "start": round(start, 3), "end": round(end, 3),
            "gs": round(off[ep_i] + start, 3), "ge": round(off[ep_i] + end, 3),
            "blk": blk,
            "label": meta["label"],
            # 这块「最狠那一句」的时刻（全局秒）。模型给了就用它当收紧的锚点，
            # 没给就留空，程序退回「居中裁」——见 _cap_quota。
            "core": meta.get("core"),
        }
        # 模型没给评分就不写这个键（2026-11-21）：造一个 5 分会让界面每块都挂
        # 「5分」，还会被 _fit_target 当成「整套都是缓冲块」。
        if "rating" in meta:
            item["rating"] = meta["rating"]
        out.append(item)

    for idx, seg in enumerate(segs_in):
        if not isinstance(seg, dict):
            continue
        meta = {"label": str(seg.get("label") or "")[:40]}
        if seg.get("rating") is not None:
            try:
                meta["rating"] = max(1.0, min(10.0, float(seg["rating"])))
            except (TypeError, ValueError):
                pass
        blk = f"b{idx + 1}"

        ep_i = None
        if seg.get("ep") is not None:
            ep_i = int(seg.get("ep") or 0)
        try:
            f_raw = int(seg.get("from") or 1)
            t_raw = int(seg.get("to") or f_raw)
        except (TypeError, ValueError):
            continue
        ep = by_index.get(ep_i) if ep_i else None
        # 「最狠那一句」的全局秒：模型标了 core（全局句号）就换算成时间，
        # 给 _cap_quota 当收紧锚点。没标就留 None，退回居中裁。
        meta["core"] = _core_gs(seg, glines, off)
        # 判定格式：没给集号，或给的 from/to 超出了那一集的句数 → 必是全局编号
        is_old = bool(ep and (ep.get("lines") or [])) and \
            1 <= f_raw <= len(ep["lines"]) and 1 <= t_raw <= len(ep["lines"])
        if is_old:
            a = max(1, f_raw)
            b = max(a, t_raw)
            _push(ep["index"], ep["lines"][a - 1], ep["lines"][b - 1], meta, blk)
            continue

        # 新格式：全局编号，可跨集
        g0 = max(1, f_raw)
        g1 = max(g0, t_raw)
        rows = [r for r in glines if g0 <= r["gid"] <= g1]
        if not rows:
            continue
        # 按集切片：一段可能跨 2-3 集，每集一片，片与片在全局时间轴上首尾相接
        run, cur = [], rows[0]["ep"]
        for r in rows:
            if r["ep"] != cur:
                _push_ep_run(run, by_index, meta, blk, _push)
                run, cur = [], r["ep"]
            run.append(r)
        if run:
            _push_ep_run(run, by_index, meta, blk, _push)

    # 过滤过短的块：按「整块总时长」判断，**不能按片**——跨集块拆开后每片可能都不长，
    # 但合起来是完整一场戏；按片砍会把块的头或尾切掉，剧情照样断。
    by_blk: dict[str, list[dict]] = {}
    for s in out:
        by_blk.setdefault(s["blk"], []).append(s)
    kept: list[dict] = []
    for ps in by_blk.values():
        if sum(s["end"] - s["start"] for s in ps) < max(2.0, min_len * 0.6):
            continue
        longest = max(ps, key=lambda s: s["end"] - s["start"])
        for s in ps:
            # 边缘只剩零点几秒的，接上去也是黑场，单独丢掉
            if s is longest or (s["end"] - s["start"]) >= 1.5:
                kept.append(s)
    out = kept
    out.sort(key=lambda s: (s["ep"], s["start"]))
    return out


def _push_ep_run(run: list[dict], by_index: dict, meta: dict, blk: str, push) -> None:
    if not run:
        return
    push(run[0]["ep"], {"start": run[0]["start"]}, {"end": run[-1]["end"]}, meta, blk)


# 缝合参数：两块之间只隔「几句台词的时间」，本来就不该留空洞（观众会看到跳帧）。
#
# 这里从 4 秒放宽到 9 秒（2026-11-02 第二次调整）。原因：一集只有 40-60 秒，
# 一场交锋里「施压」和「反应」之间常隔 5-8 秒的沉默或一个眼神，缝不上就变成
# 两块互不相干的碎片，观众看不懂因果。9 秒以内基本还是同一场戏；超过 9 秒
# 多半是换了场景（走路、赶路、转场），那才是该省略的地方。
MERGE_GAP = 9.0       # 秒：两块间隙 ≤ 9 秒（同一场交锋内）才缝成一块
MAX_BLOCK = 90.0      # 秒：缝合后单块上限，只防「把两场不相干的戏连成一条巨块」

# 一块的硬下限（2026-11-22 C 档）：程序在编排末尾逐块检查，不足就向两头延长，
# 延不到就整块丢掉。为什么要程序判：提示词写「一块 ≥60 秒」也好、「40-75 秒」也好，
# 模型都不执行——6 批 240 个块的中位数是 30-38 秒、最短 10.9 秒，它按「一场戏」切，
# 而算秒数是它做不了的长程算术。凡是可计算的硬指标，提示词说完后必须在代码里再判一次。
# 取 40 而不是 60：本剧一集只有 60.8 秒，_grow_to 的 GROW_CAP=0.72 让**单集块最长
# 43.8 秒**，要求 60 只能靠跨集拼接，会把「一场戏一块」的结构整个打散。
BLOCK_MIN = 40.0      # 秒：一块的硬下限

# 衔接缓冲块：rating 低于这个分，就是「让前一记重拳落地」的过渡（沉默、错愕、
# 一句承上的话）。2026-11-12 用户明确**不再要 AI 专门去找缓冲块**（plans.txt 里
# 那条要求已删），但模型排结构时仍会顺手带几块 5-6 分的反应/过渡，程序这边继续
# 给它们独立预算——纯按分数排序的话，它们永远排最后、超额时第一个被砍，结果一套
# 片子全是爆点，一路耳光，观众两分钟就疲劳（2026-11-02 用户指出）。
# 模型不给就没有：下面的 soft_cap 取 min(cap×0.22, soft 实际总长)，不会去 spare 硬拉。
SOFT_RATING = 7.0
# 缓冲块的最短时长：一个错愕镜头 6 秒是完整的，一句爆点 6 秒是断的。
# 下限对两类块必须分开，否则 _fill 会按同一把尺子把缓冲块全删掉。
SOFT_MIN = 5.0
MIN_PIECE = 1.2       # 秒：跨集切分后边缘不足这么长的碎片丢掉

# 整集块收窗（2026-11-03）。这部剧一集只有 50 秒左右，「一整块 = 一整集」不是
# 「一场 50 秒的长戏」，而是**把那一集原封不动播一遍**——一套 4.5 分钟装 6 块，
# 播出效果就是连播 6 集，混剪感全没了。提示词治不住（块数是离散的、整集是语义
# 判断，实测同一版提示词下整集块在 0-12 之间随机跳，见 BUG清单 #20），所以改成
# 程序兜底：认出整集块，围绕模型给的 core（最狠那一句）收一个窗口。
#
# 这不违反「只挑整块、不裁块」：整集块本身就是一个没剪过的原材料，不是一场戏。
# 从一整集里取出那场交锋，跟把一场戏砍半是两件事——块还是完整的，只是不再把
# 整集塞进来。收窗后套总长会掉，_fit_target 会多挑几块补回，块数自然上去。
# 2026-11-21 用户要求「每块不短于 60 秒，不够就接相邻集的同一场戏续段」，
# 判定必须跟着改：「块够长就算整集」改成「**真的覆盖了整集**」。
# 原因：60 秒的块跨集拆成两片（如上一集后 20 秒 + 下一集前 40 秒），单片很容易
# ≥ 集长×0.72（46-60 秒的剧几乎必中），老判定会把它当整集收回 28 秒——
# 等于提示词写 60 秒、程序又缩回去，改动等于没做。
# 收窗的目的只有「别把一集原封不动播一遍」，所以只认贴着集首又贴着集尾的块。
WHOLE_EDGE = 2.0      # 秒：片起点 ≤2 秒且终点 ≥ 集尾-2 秒，才算「整集没剪」
WHOLE_MIN = 30.0      # 秒：块短于这个就不收——35 秒以内本来就可能是一场完整的戏
WIN_BEFORE = 10.0     # 秒：锚点前留多少（对方那句挑事的台词、起因）
WIN_AFTER = 18.0      # 秒：锚点后留多少（反应、还嘴、对方愣住的收尾）
WIN_MIN = 15.0        # 秒：窗口下限，再短就不像一场交锋了
WIN_MAX = 35.0        # 秒：窗口上限。用户说过「超过 30 秒完全可能」，别硬卡 30
GROW_CAP = 0.72       # _grow_to 延长后不得超集长×此值，否则又长回整集

# 套间重复率上限（2026-11-12 用户定 80%，2026-11-16 收紧到 70%）。**提示词和程序用同一个数**：
# plans.txt 第 5 条的全片限额写 0.7，batch._overlap 事后按它核算
# （2026-11-20 已删掉模板里把比例换算成秒数的「自查」行，本常量仍供 batch 核算）
# （重合秒数 ÷ 较短那套的素材秒数）。**只核算、只上报、不拦截**（用户明确：
# 「原来程序只在事后上报重复率，不拦截。我不需要你去拦截」）——同一部剧剪 N 套，
# 素材复用是必然的，硬卡只会让某几套凑不够时长，一套 2.9 分钟的片子发不出去，
# 比重复更糟（见 _fit_target 的 spare 借块注释）。分数超线由前端标红提示。
OVERLAP_CAP = 0.7


def _stitch_blocks(p: dict, segs: list[dict],
                   merge_gap: float = MERGE_GAP, max_block: float = MAX_BLOCK) -> list[dict]:
    """把隔得太近的块缝成一个连续块。**只缝合，不裁切，不丢内容。**

    为什么缝：同一场戏里两块只隔一两句台词，中间的空洞观众只会看成跳帧。

    为什么不按覆盖率砍：短剧一集只有 70-90 秒，一集里两处爆点往往只隔十几秒
    （同一场戏的两个冲突）。按 70% 覆盖率硬砍，等于把其中一段连带着中间的对白
    一起挖掉，剪出来就是「跳」。

    但「缝」的边界要跟着「一场戏」走（MERGE_GAP=9 秒）。同一场交锋以内，
    施压和反应之间隔几秒沉默、一个眼神、一句旁人插话，都必须缝上——缝不上
    就成两块互不相干的碎片，观众看不懂谁在跟谁较劲。间隙超过 9 秒，多半是
    换了场景（起身、走动、赶路），那才是该整段省略的过程。
    缝不缝的中段判断权仍在挖池的 AI 手上，这里只是兜底。
    套与套之间的相似度不靠砍素材控制，靠 MAX_OVERLAP（组装阶段）控制。
    """
    if not segs:
        return segs
    off = ep_offsets(p)
    dur = {ep["index"]: float(ep.get("duration") or 0) for ep in p["episodes"]}
    order = [ep["index"] for ep in p["episodes"]]

    # 1) 碎片归块：同一 blk 的片（跨集时一集一片）合成一块，用全局秒记边界
    groups: dict[str, list[dict]] = {}
    for s in segs:
        groups.setdefault(s.get("blk") or f"e{s['ep']}_{s['start']}", []).append(s)
    blocks = []
    for k, ps in groups.items():
        ps = sorted(ps, key=lambda x: x["gs"])
        head = max(ps, key=lambda x: x.get("rating") or 5)
        edges: dict[int, list[float]] = {}
        for s in ps:
            e = edges.setdefault(s["ep"], [float(s["gs"]), float(s["ge"])])
            e[0] = min(e[0], float(s["gs"]))
            e[1] = max(e[1], float(s["ge"]))
        blk_out = {
            "gs0": float(ps[0]["gs"]), "ge0": float(ps[-1]["ge"]),
            "edges": edges,
            "label": head.get("label") or "",
            "core": head.get("core"),
        }
        if "rating" in head:            # 模型没给评分就不带这个键
            blk_out["rating"] = float(head["rating"])
        blocks.append(blk_out)
    blocks.sort(key=lambda b: b["gs0"])

    # 2) 贪心缝合：间隙够小、缝完不超长 → 连成一块；否则保留独立两块
    merged: list[dict] = []
    for b in blocks:
        if merged:
            cur = merged[-1]
            gap = b["gs0"] - cur["ge0"]
            if gap <= merge_gap and (b["ge0"] - cur["gs0"]) <= max_block:
                cur["ge0"] = max(cur["ge0"], b["ge0"])
                for ep, (a, z) in b["edges"].items():
                    e = cur["edges"].setdefault(ep, [a, z])
                    e[0], e[1] = min(e[0], a), max(e[1], z)
                if "rating" in b and float(b["rating"]) > float(cur.get("rating") or 0):
                    cur["rating"], cur["label"] = b["rating"], b["label"]
                continue
        merged.append({**b, "edges": {k: list(v) for k, v in b["edges"].items()}})

    # 3) 重新按集切片：一块跨集就拆成每集一片，仍共用同一 blk。
    #    边界取「该集里原始片的起止范围」——合并在同集内填洞（剧情中间不漏），
    #    但不把范围扩到集末黑场（那不是剧情，接上去就是几秒静默）。
    out = []
    for i, b in enumerate(merged):
        blk = f"s{i + 1}"
        for ep_i in order:
            if ep_i not in b["edges"]:
                continue
            a, z = b["edges"][ep_i]
            a = max(a, off.get(ep_i, 0.0))
            z = min(z, off.get(ep_i, 0.0) + dur.get(ep_i, 0.0))
            if z - a < MIN_PIECE:
                continue
            piece = {
                "ep": ep_i,
                "start": round(a - off.get(ep_i, 0.0), 3),
                "end": round(z - off.get(ep_i, 0.0), 3),
                "gs": round(a, 3), "ge": round(z, 3),
                "blk": blk,
                "label": b["label"],
                "core": b.get("core"),
            }
            if "rating" in b:
                piece["rating"] = round(float(b["rating"]), 1)
            out.append(piece)
    out.sort(key=lambda s: (s["ep"], s["start"]))
    return out


def _trim_whole(p: dict, segs: list[dict]) -> list[dict]:
    """把「整集块」收成一场戏：围绕最狠那一句取 15-35 秒窗口。

    只动 start≈0 且 end≈集尾的块，**其他块一律不碰**。锚点优先用模型给的 core
    （这块最狠那一句的全局秒），core 缺失或落在块外时，退回「块内最长的一句」
    ——短剧里最长的那句通常就是当面揭穿/威胁，信息量最大的那一下。

    切点一律吸附到台词边界、且向块内收（不越出原块），所以不会从半句话中间切开；
    吸附后窗口窄于 WIN_MIN×0.8 就放弃这一块，保留原样——宁可这段是整集，
    也不能切成一个看不懂的碎片。

    为什么要收：这部剧一集只有 50 秒，整集块 = 把一集原样播一遍。4.5 分钟一套
    装 6 块整集，等于连播 6 集，观众看的是「重播」不是「混剪」。
    """
    by_idx = {ep["index"]: ep for ep in p["episodes"]}
    off = ep_offsets(p)
    out: list[dict] = []
    for s in segs:
        ep = by_idx.get(s["ep"])
        if not ep:
            out.append(s)
            continue
        ep_dur = float(ep.get("duration") or 0)
        a0, z0 = float(s["start"]), float(s["end"])
        dur = z0 - a0
        # 太短的集（30 秒以下）本来就只能整块用，收窗没有意义
        if ep_dur < 30.0 or dur < WHOLE_MIN:
            out.append(s)
            continue
        # 只收「整集没剪」的块：必须同时贴着集首和集尾。不看长度比例——
        # 60 秒的块在 46-60 秒的剧里占比必然超标，按比例收会把跨集拼块的
        # 每一片都打断（2026-11-21，见上方 WHOLE_EDGE 注释）。
        if a0 > WHOLE_EDGE or z0 < ep_dur - WHOLE_EDGE:
            out.append(s)
            continue
        lines = ep.get("lines") or []

        anchor = None
        core = s.get("core")
        if core is not None:
            try:
                c = float(core) - off.get(s["ep"], 0.0)
                if a0 + 1.0 <= c <= z0 - 1.0:
                    anchor = c
            except (TypeError, ValueError):
                anchor = None
        has_core = anchor is not None     # 模型标的 core 有效，才保留这个锚点
        if anchor is None and lines:
            inside = [ln for ln in lines
                      if float(ln["end"]) > a0 and float(ln["start"]) < z0]
            if inside:
                ln = max(inside, key=lambda x: float(x["end"]) - float(x["start"]))
                anchor = (float(ln["start"]) + float(ln["end"])) / 2
        if anchor is None:
            anchor = (a0 + z0) / 2

        a = max(a0, anchor - WIN_BEFORE)
        z = min(z0, anchor + WIN_AFTER)
        if z - a < WIN_MIN:            # 窗口太窄：往锚点两侧补到下限
            need = WIN_MIN - (z - a)
            a = max(a0, a - need / 2)
            z = min(z0, z + need / 2)
        if z - a > WIN_MAX:            # 窗口太宽：以锚点为中心收到上限
            a = max(a0, anchor - WIN_MAX / 2)
            z = min(z0, a + WIN_MAX)
            a = max(a0, z - WIN_MAX)
        a2 = _snap(lines, a, a0, anchor)
        z2 = _snap(lines, z, anchor, z0)
        if z2 - a2 < WIN_MIN * 0.8:
            out.append(s)
            continue
        ns = dict(s)
        ns["start"], ns["end"] = round(a2, 3), round(z2, 3)
        ns["gs"] = round(off.get(s["ep"], 0.0) + a2, 3)
        ns["ge"] = round(off.get(s["ep"], 0.0) + z2, 3)
        if not has_core:
            # core 没标、或标到了块外：锚点是程序推的「最长那句」，不是模型标的
            # 名场面锚点。清掉，别让锚点撞车统计虚高（两套的「最长句」很容易撞）。
            ns["core"] = None
        out.append(ns)
    return _dedupe_ep(out, by_idx, off)


def _free_gaps(a: float, z: float, taken: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """[a,z] 里还没被别人占用的空档。"""
    gaps = [(a, z)]
    for ta, tz in taken:
        nxt = []
        for ga, gz in gaps:
            if tz <= ga or ta >= gz:
                nxt.append((ga, gz))
                continue
            if ta > ga:
                nxt.append((ga, ta))
            if tz < gz:
                nxt.append((tz, gz))
        gaps = nxt
    return [(x, y) for x, y in gaps if y - x > 0.5]


def _dedupe_ep(segs: list[dict], by_idx: dict, off: dict) -> list[dict]:
    """同一集里不许有两块压在一起——那等于同一段戏播两遍。

    收窗之前不会撞：一集 50 秒，一块就占满一集，同集再来一块会先被 _stitch_blocks
    缝成一块。收窗之后每块只剩二三十秒，模型若在这一集里给了两段（前半场、后半场），
    各自围绕 core 取窗就会压在一起（实测 6 套共 21 处）。这里分数高的先占位，
    低分块被挤到剩下的空档里；挤完不足 8 秒就整块丢掉——留着也是闪一下。
    """
    by_ep: dict[int, list[dict]] = {}
    for s in segs:
        by_ep.setdefault(s["ep"], []).append(s)
    res: list[dict] = []
    for ep, ss in by_ep.items():
        if len(ss) == 1:
            res.extend(ss)
            continue
        ss.sort(key=lambda x: (-_rt(x), float(x["start"])))
        lines = (by_idx.get(ep) or {}).get("lines") or []
        taken: list[tuple[float, float]] = []
        for s in ss:
            a, z = float(s["start"]), float(s["end"])
            gaps = _free_gaps(a, z, taken)
            if not gaps:
                continue
            ga, gz = max(gaps, key=lambda t: t[1] - t[0])
            if abs(ga - a) < 1e-6 and abs(gz - z) < 1e-6:
                res.append(s)          # 没被挤，原样保留，别为 0.9 秒的吸附动它
                taken.append((a, z))
                continue
            if gz - ga < 8.0:
                continue
            a2 = _snap(lines, ga, ga, gz)
            z2 = _snap(lines, gz, ga, gz)
            if z2 - a2 < 8.0:
                continue
            ns = dict(s)
            ns["start"], ns["end"] = round(a2, 3), round(z2, 3)
            ns["gs"] = round(off.get(ep, 0.0) + a2, 3)
            ns["ge"] = round(off.get(ep, 0.0) + z2, 3)
            res.append(ns)
            taken.append((a2, z2))
    res.sort(key=lambda s: (s["ep"], s["start"]))
    return res


def _snap(lines: list[dict], t: float, lo: float, hi: float) -> float:
    """把裁切点吸附到最近的台词边界，别从半句话中间切开（观众会听到断句）。"""
    if not lines:
        return t
    best, bd = t, 0.9
    for ln in lines:
        for v in (float(ln["start"]), float(ln["end"])):
            if lo <= v <= hi:
                d = abs(v - t)
                if d < bd:
                    best, bd = v, d
    return best


def _drop_slivers(segs: list[dict], min_sec: float) -> list[dict]:
    """清掉跨集切开后只剩零点几秒的残片。

    一块跨两集时会拆成两片，若第二片只剩 2 秒，它在成片里就是「闪一下」，
    观众只会觉得卡了。块本身够长就留主片，残片丢掉。

    阈值 0.3×min_sec（约 3 秒）而不是 0.6×（6 秒）：以前 _fit_target 会裁块，
    裁完留下的尾巴要清；现在一块要么整块留、要么整块不留，跨集拆开的每一片
    都是真实内容，只清真正「闪一下」的，别把剧情删断。
    """
    by: dict[str, list[dict]] = {}
    for s in segs:
        by.setdefault(s["blk"], []).append(s)
    drop_blk: set[str] = set()
    drop_one: set[tuple] = set()
    for blk, ps in by.items():
        tot = sum(float(s["end"]) - float(s["start"]) for s in ps)
        if tot < min_sec * 0.6:
            drop_blk.add(blk)
            continue
        if len(ps) == 1:
            continue
        for s in ps:
            if (float(s["end"]) - float(s["start"])) < max(2.0, min_sec * 0.3):
                drop_one.add((blk, s["ep"], round(float(s["start"]), 3)))
    return [s for s in segs
            if s["blk"] not in drop_blk
            and (s["blk"], s["ep"], round(float(s["start"]), 3)) not in drop_one]


def _ovl_one(s: dict, others: list[dict]) -> float:
    """这一片与一组片段的实际重合秒数（只在同一集内做区间求交）。"""
    n = 0.0
    for t in others:
        if t["ep"] != s["ep"]:
            continue
        n += max(0.0, min(float(s["end"]), float(t["end"]))
                 - max(float(s["start"]), float(t["start"])))
    return n


def _rt(s: dict) -> float:
    try:
        return float(s.get("rating") or 5)
    except (TypeError, ValueError):
        return 5.0


def _min_len_of(s: dict, min_sec: float) -> float:
    """这块的最短可用时长。缓冲块（rating<7）可以短，爆点块不行。"""
    return SOFT_MIN if _rt(s) < SOFT_RATING else min_sec


def _fit_target(p: dict, segs: list[dict], target: float, min_sec: float = 10.0,
                avoid: list[list[dict]] | None = None,
                spare: list[dict] | None = None,
                usage: dict | None = None) -> list[dict]:
    """把**一套**收到 target 秒：**只挑整块，不裁块**。AI 编排多套时用。

    为什么需要程序兜底：AI 一次给出 6 套、上百个区间，它算不准总秒数——让它保证
    「每套 4-5 分钟」，交出来可能是 2 分钟也可能是 7 分钟。跟挖池那次同一个道理：
    **凡是可计算的硬指标，提示词说完之后必须在代码里再判一次**
    （见 docs/BUG清单.md 第 14 条）。

    **为什么整块取舍、不裁**（2026-11-02 用户指出）：一块就是一整场交锋。
    把 55 秒的戏砍到 28 秒，观众看到的是「话说一半就跳走了」——这比这场戏
    根本不出现更伤。所以这里只做两件事：按分数挑块、把总长装进 target 附近。
    素材多了就少挑几块，绝不把一块切两半。装不满的那点差额（通常 5-10%）
    由 _grow_to 把已选块向两头延长来补——延长是把这场戏补完整，不是塞碎戏。

    avoid = 前面几套已经用掉的素材。与它们重复过半的块**第一轮不挑**，
    只有独有块凑不够长度时才回头用它们——这是「AI 多给素材、程序做取舍」的分工：
    让模型自己算 6 套的两两重合，它一定算错（实测第 6 套照样排出 82%）。

    **缓冲块要单独给预算**（2026-11-02 用户第 5 条）：5-6 分的过渡块分数最低，
    纯按分数挑的话永远排最后、超额时第一个被砍，一套片子就全是爆点——
    一路耳光看两分钟，观众会疲劳划走。所以先给它们留出约两成时长，
    再从剩下的预算里挑爆点块。这也是提示词里那句「程序会单独留预算」的实现。

    spare = 这一次 AI 给出的**其他套**的块。这套自己的素材装不满时从这里借：
    「打脸专线」在这部剧里只找得到 4 处硬碰硬，自己那 4 块加起来才 2.9 分钟，
    怎么延也到不了 4 分钟——一套 2.9 分钟的片子发不出去，比重合度上去更糟。
    但借块是「别条线的戏」，**借多了一定断连贯**（2026-11-03：一套 12 块全借、
    同一结局播四遍），所以借块有三条约束：就近借、预算封顶 30%、每个故事节拍
    （label）只借一次、同一集只借一块——真不够就接受这套短一点，别凑。
    """
    if not segs or target <= 0:
        return segs
    usage = usage or {}
    total = _segs_sec(segs)
    # 2026-11-21：plans_schema 不再要 rating（一段只要 from/to，不挑高光）。
    # 这时原来那套「爆点先挑、缓冲块留 22% 预算」失去依据——所有块都会被当成
    # 5 分的缓冲块，一套只能装出两成时长。改成按剧情顺序装填。
    # 判定看「有没有一块带真实评分」：老结构（1-10 分）仍走原逻辑，互不影响。
    no_rate = not any("rating" in s for s in segs)

    def _floor(s: dict) -> float:
        # 无评分时不能按「缓冲块 5 秒」放行，否则顺序装填会把碎块也装进来
        return min_sec if no_rate else _min_len_of(s, min_sec)

    # 台账里已经有块用满时，不能走「够长就直接用」这条快路径：那条路一块都不换，
    # 跨批次的重复就是这么漏掉的（2026-11-17）。只有全部新鲜时才允许直接放行。
    fresh = not any(reuse.is_capped(reuse.count(usage, s)) for s in segs)
    if fresh and target * 0.82 <= total <= target * 1.08:
        # 够长就直接用，只清碎片：AI 偶尔给 3-4 秒的块（「失语」一个眼神那种），
        # 播出去就是闪一下，观众只觉得卡了。这条原来漏在 early return 之外，
        # 实测第 3 套保留了一块 3.6 秒的（2026-11-02）。
        # 区间是 [82%, 108%]：低于 82% 要往下补（原来只要不超目标就直接返回，
        # 一套只有 2.9 分钟也照样放行）；高于 108% 要往下挑块（改这个门槛时
        # 一度写成「≥82% 就返回」，第 1 套直接出了 9.89 分钟，2026-11-02）。
        keep = [s for s in segs
                if float(s["end"]) - float(s["start"]) >= _floor(s)]
        return _drop_slivers(keep or segs, min_sec)
    avoid = avoid or []
    flat = [x for a in avoid for x in a]

    def _dup(s: dict) -> bool:
        dur = float(s["end"]) - float(s["start"])
        return bool(flat) and dur > 0 and _ovl_one(s, flat) > dur * 0.5

    def _used(s: dict) -> int:
        return reuse.count(usage, s)

    # 排序：分数高的先挑；同分先挑**短**的——长块吃预算多，先挑短的能让这套
    # 多装几块、结构更完整；长块装不下就不装，也不会被砍成半场。
    # 复用台账（2026-11-17）：按「用过几次」降权，跨批次不再反复用同几场戏。
    # 只降权不删除——用过 1 次的块仍可能是这套唯一能用的戏，一刀切会凑不够时长。
    # 无评分（2026-11-21）= 不挑高光，改成**按剧情顺序**（全局秒）装填：块按时间
    # 从小到大装，装到目标时长为止。套与套的差异由下面的 avoid 承担（前面套用过
    # 过半的块第一轮不挑），模型自己也会给不同套挑不同的段落。
    if no_rate:
        ranked = sorted(segs, key=lambda s: float(s.get("gs") or 0))
    else:
        ranked = sorted(segs, key=lambda s: (-(_rt(s) * reuse.weight(_used(s))),
                                             float(s["end"]) - float(s["start"])))
    # 重复块（跟前面几套撞过半 / 台账里已用满）第一轮一律不挑，只有独有块
    # 凑不够长度时才回用——和 avoid 同一套降级逻辑，宁可重复也别出 2.9 分钟的片。
    uniq = [s for s in ranked if not _dup(s) and not reuse.is_capped(_used(s))]
    dup = [s for s in ranked if _dup(s) or reuse.is_capped(_used(s))]

    chosen: set[int] = set()
    picked: list[dict] = []
    used = 0.0

    def _fill(pool: list[dict], cap: float, floor: float | None = None) -> None:
        nonlocal used
        for s in pool:
            if id(s) in chosen:
                continue
            dur = float(s["end"]) - float(s["start"])
            if dur < (floor if floor is not None else _floor(s)):
                continue           # 太碎：观众还没进戏就被切走
            if used + dur > cap:
                continue
            # 跟这套已经选过的块时间上撞掉一半以上 → 这段戏这套里播过一遍了。
            # 必查：spare 里带着**别的套**的同名块（各自是不同对象，id 判重抓不到），
            # 借块时会把第 3 套用过的块整段借进第 4 套，同一套里出现两遍同一场戏
            # （2026-11-03 实测 6 套里 4 处，最长整块 14 秒完全重复）。
            # 比例按**较小那块**算：21.9 秒的块和 10.6 秒的块重叠 10.6 秒，
            # 按大块算不到 50%（10.6/21.9）会漏放，但小块的 100% 被它吃掉了。
            if any(_ovl_one(s, [o]) > min(dur, float(o["end"]) - float(o["start"])) * 0.5
                   for o in picked):
                continue
            chosen.add(id(s))
            picked.append(s)
            used += dur

    # 先给缓冲块留出预算，再挑爆点——顺序反过来，缓冲永远抢不到位置。
    # 无评分时（2026-11-21）没有「爆点/缓冲」之分，全部按顺序装填，不预留预算。
    cap = target * 1.05
    if no_rate:
        boom, soft = uniq, []
        soft_cap = 0.0
    else:
        boom = [s for s in uniq if _rt(s) >= SOFT_RATING]
        soft = [s for s in uniq if _rt(s) < SOFT_RATING]
        soft_cap = min(cap * 0.22, _segs_sec(soft))
    _fill(boom, cap - soft_cap)
    if soft:
        _fill(soft, min(cap, used + soft_cap * 1.1), SOFT_MIN)
    if used < target * 0.85:       # 独有的块不够长，才回头用重复块
        _fill(dup, cap)
    if used < target * 0.92:       # 还差一截，放宽上限再装一轮，别让这套明显偏短
        # 这一步不按套间重复率卡（2026-11-12 用户明确「不需要你去拦截」）：
        # 走到这里说明这套独有素材不够凑够时长，宁可重复也不能交一套 2.9 分钟的
        # 废片；真超了由 batch._overlap 事后核算、在界面上标红报出来。
        _fill(ranked, target * 1.10)
    if used < target * 0.88 and spare:
        # 这套自己实在没料（某条主线本来就只有几场戏）→ 从别的套的块里借。
        # 借来的块不是按这条线挑的，乱借会把另一条线的戏整段搬进来，缝全断开
        # （实测：一套 12 块全部借自另一套，ep4→ep8 跳 4 集、ep15→ep19 跳 4 集）。
        # 所以三条约束：
        # ① **就近借**：优先借时间轴上跟这套已选块挨着的，分数只当次序。
        # ② **预算封顶 30%**：借块超过一套的三成，这套就不是它自己那条线了；
        #    真不够就接受这套短一点（_grow_to 会把每块延完整），别凑。
        # ③ **每个故事节拍只借一次**：同一个结局每套都写了自己的版本
        #    （label 相同/相近），不加筛的话全搬进同一套，同一情节播四遍
        #    （2026-11-03 实测 ep24-ep25 连着四块都在讲「营养餐三方验收」）。
        #    同一集也只借一块，防止把别套里相邻的两块成对搬进来。
        def _dist(s: dict) -> float:
            return min((max(0.0, float(s["gs"]) - float(o["ge"]),
                            float(o["gs"]) - float(s["ge"])) for o in picked),
                       default=0.0)
        seen_lbl: set[str] = {(s.get("label") or "").strip() for s in picked if s.get("label")}
        seen_ep: set = {s["ep"] for s in picked}
        pool: list[dict] = []
        for s in sorted((x for x in spare if id(x) not in chosen),
                        key=lambda x: (_dist(x), -float(x.get("rating") or 5))):
            lbl = (s.get("label") or "").strip()
            if lbl and lbl in seen_lbl:
                continue
            if s["ep"] in seen_ep:
                continue
            pool.append(s)
            if lbl:
                seen_lbl.add(lbl)
            seen_ep.add(s["ep"])
        _fill(pool, min(target * 1.02, used + target * 0.30))
    if not picked:
        return segs
    picked.sort(key=lambda s: (s["ep"], s["start"]))
    return _drop_slivers(picked, min_sec)


def _grow_to(p: dict, segs: list[dict], floor: float, want: float = 55.0) -> list[dict]:
    """一套素材不够长时，把每块**向两头延长**，补到 floor（秒）。

    _fit_target 只会削，补不了。AI 给「情绪专线」这类子集时，能找到的素材本来就少，
    而且块被切得很碎（实测一套 14 块只有 2.1 分钟，均块 9 秒）——直接播出去太短，
    发不了。延长的是同一块的前后台词（观众看到更完整的一场交锋），**不引入新素材**，
    所以不会平白多出跟别的套的重合。

    want 是单块延长的上限，原来是 24 秒。**块长本来就没有上限**（只有下限约 10 秒），
    24 秒会把「这场戏本来就该演 50 秒」的长块提前封顶，所以放到 55——补时长时
    让块先长到能自己站住，而不是多塞几块碎戏进来。
    """
    if not segs or floor <= 0:
        return segs
    cur = _segs_sec(segs)
    if cur >= floor:
        return segs
    by_idx = {ep["index"]: ep for ep in p["episodes"]}
    off = ep_offsets(p)
    need = floor - cur
    out: list[dict] = []
    for s in segs:
        ep = by_idx.get(s["ep"])
        if not ep:
            out.append(s)
            continue
        lines = ep.get("lines") or []
        ep_dur = float(ep.get("duration") or 0)
        a0, z0 = float(s["start"]), float(s["end"])
        dur = z0 - a0
        # 缓冲块（rating<7）的价值**就在它短**：它是让前一记重拳落地的留白。
        # 把 8 秒的错愕延到 40 秒，四周围的寒暄、走位全被带进来，留白就没了，
        # 还顺带把别人的素材拉进这套、推高重合度。所以它最多长到两倍。
        limit = want if _rt(s) >= SOFT_RATING else min(want, max(dur * 2.2, SOFT_MIN * 2))
        # 但不能延回整集：一集只有 50 秒，延到 55 秒就等于把这集原样播一遍，
        # 前面 _trim_whole 刚收掉的窗口又被撑回去了。
        limit = min(limit, max(dur, ep_dur * GROW_CAP))
        room = limit - dur
        if room <= 0 or not lines:
            out.append(s)
            continue
        add = min(room, max(0.0, need) / max(1, len(segs) - len(out)))
        if add < 1.0:
            out.append(s)
            continue
        # 前后各扩一半，但两头都要留在本集之内、并且吸附到台词边界
        # 同一集里若还有别的块，不能延到它身上——延过去就是两块重叠，同一段播两遍
        sib = [x for x in segs if x.get("ep") == s["ep"] and x is not s]
        lo = max([float(x["end"]) for x in sib
                  if float(x["end"]) <= a0 + 0.05] + [0.0])
        hi = min([float(x["start"]) for x in sib
                  if float(x["start"]) >= z0 - 0.05] + [ep_dur])
        a = max(lo, a0 - add / 2)
        b = min(hi, z0 + add / 2)
        a2 = _snap(lines, a, lo, a0)
        b2 = _snap(lines, b, z0, hi)
        if b2 - a2 <= dur + 0.3:      # 吸附把扩出来的部分吃掉了，这块没法再长
            out.append(s)
            continue
        ns = dict(s)
        ns["start"], ns["end"] = round(a2, 3), round(b2, 3)
        ns["gs"] = round(off.get(s["ep"], 0.0) + a2, 3)
        ns["ge"] = round(off.get(s["ep"], 0.0) + b2, 3)
        out.append(ns)
        need -= (b2 - a2) - dur
    return out


def _lift_blocks(p: dict, segs: list[dict], min_block: float = BLOCK_MIN) -> list[dict]:
    """把不足 min_block 的块向两头延长；延不到就整块丢掉。（2026-11-22 C 档）

    为什么要在程序里补：plans.txt 改成「40-75 秒」后，GLM-5.3 交出来的块反而更碎
    （中位 34.3 秒、最短 10.9 秒），因为模型把「一场戏」当一块、从不按秒数算。
    延长只动这一块**前后的台词**（观众看到的是更完整的一场交锋），不新增剧情、
    不跨场；上限受同集邻块与集首尾约束——延到邻块身上就是同一段播两遍。

    跨集块按片处理：尾片往后延、首片往前延，各自吸附到台词边界。
    两块都延不出空间（贴满集首尾、或邻块紧贴）时整块丢弃——宁可这套少一块，
    也不让 25 秒的碎片进成片（用户口径：话说一半就跳比不出现更伤）。
    """
    if not segs:
        return segs
    by_idx = {ep["index"]: ep for ep in p["episodes"]}
    off = ep_offsets(p)
    groups: dict[str, list[dict]] = {}
    for s in segs:
        groups.setdefault(s.get("blk") or f"e{s['ep']}_{s['start']}", []).append(s)

    out: list[dict] = []
    for ps in groups.values():
        ps = [dict(x) for x in sorted(ps, key=lambda x: float(x["gs"]))]
        dur = sum(float(x["end"]) - float(x["start"]) for x in ps)
        if dur >= min_block:
            out.extend(ps)
            continue
        # 两轮：先往后延尾片，再往前延首片。每轮都用最新的 dur 重算要补多少。
        for rnd in range(2):
            if dur >= min_block:
                break
            s = ps[-1] if rnd == 0 else ps[0]
            ep = by_idx.get(s["ep"])
            lines = (ep or {}).get("lines") or []
            if not lines:
                continue
            ep_dur = float(ep.get("duration") or 0)
            a0, z0 = float(s["start"]), float(s["end"])
            # 同集的其他块把它夹在中间，不能延过去
            sib = [x for x in segs
                   if x.get("ep") == s["ep"] and x.get("blk") != s.get("blk")]
            lo = max([float(x["end"]) for x in sib if float(x["end"]) <= a0 + 0.05] + [0.0])
            hi = min([float(x["start"]) for x in sib
                      if float(x["start"]) >= z0 - 0.05] + [ep_dur])
            want = min_block - dur
            if rnd == 0:
                if hi <= z0 + 0.3:
                    continue
                b2 = _snap(lines, min(hi, z0 + want), z0, hi)
                if b2 - z0 < 0.3:
                    continue
                s["end"], s["ge"] = round(b2, 3), round(off.get(s["ep"], 0.0) + b2, 3)
                dur += b2 - z0
            else:
                if lo >= a0 - 0.3:
                    continue
                a2 = _snap(lines, max(lo, a0 - want), lo, a0)
                if a0 - a2 < 0.3:
                    continue
                s["start"], s["gs"] = round(a2, 3), round(off.get(s["ep"], 0.0) + a2, 3)
                dur += a0 - a2
        if dur < min_block - 0.5:
            continue
        out.extend(ps)
    out.sort(key=lambda s: (s["ep"], float(s["start"])))
    return out


def _segs_sec(segs: list[dict]) -> float:
    """池子里的素材总时长（秒）。判断池子够不够用它，不要用条数。"""
    return sum(float(s["end"]) - float(s["start"]) for s in segs)


def _clean_hook(raw) -> str:
    """钩子卡文案：去掉 AI 爱加的引号和首尾标点，压成一行。"""
    s = str(raw or "").strip()
    if not s:
        return ""
    s = s.translate(str.maketrans("", "", "“”\"'‘’「」『』"))
    s = " ".join(s.split())
    # 结尾的句号/逗号在黑屏大字上很扎眼；「……」是设计里要的停顿，保留
    while s and s[-1] in "。，,、；;":
        s = s[:-1]
    return s[:40]


def _clean_bgm(raw) -> str:
    """BGM 建议：压成一行、去掉引号壳。AI 偶尔会输出数组（BUG 53 同款毛病），一并兜住。"""
    if isinstance(raw, (list, tuple)):
        raw = "、".join(str(x) for x in raw)
    s = " ".join(str(raw or "").split()).strip()
    if not s or s.lower() in ("none", "null"):
        return ""
    return s[:60]


def build_plans(p: dict, count: int, min_minutes: float, max_minutes: float,
                model: str | None = None, progress: tuple | bool | None = None,
                timeout: int | None = None, eps=None,
                task_kind: str = "plan") -> list[dict]:
    """调一次 API，直接从**全剧台词**生成 count 套互相不同的剪辑结构。

    eps 给定 = 本批只在这些集里排（2026-11-19 用户定）。台词块只拼选中的集、
    编号在选集内重新连续，素材总时长也按选集算。**时间轴仍用原片真实位置**，
    所以跨到未选中的集不会被误缝（见 `_global_lines` 的注释）。

    progress=False = 静默：多组并发时各组不能自己写同一个项目的进度条
    （会来回跳），由 batch.ai_plan_multi 统一按「已完成 N/M 组」推进。

    task_kind：这一趟属于哪种任务，进度按它落到对应的组。**不能写死 "plan"**
    （2026-11-21 事故）：追加新套走独立组 planadd，进度却按 "plan"（= cuts 组）
    落槽，收尾时只清自己的 planadd 槽 → cuts 那格永远停在最后一个进度点上，
    界面显示「AI 正在编排 6%」却点不动中止（_RUNNING 里只有 planadd，按 cuts
    发起的中止找不到任何任务，只能回「这一步刚好已经结束了」）。

    这是批量混剪唯一的出结构方式（第 2 步「片段池」已整体移除，2026-11-02）：
    AI 一次看完整部剧，直接排出 N 套。它看得见因果链，排得出「这套讲讨债线、
    那套讲反击线」。代价是**时长和重合度只能事后校验**——所以下面每套都要过
    _fit_target 收紧，重合度如实上报。

    返回 [{"name": 套名, "segments": [...]}, ...]，每套已按目标时长裁过。
    """
    pid = p["id"]
    sel: list[int] | None = None
    if eps:
        sel, drop = select_eps(p, eps)
        if not sel:
            raise RuntimeError("选中的集都还没整理出台词——先在第 1 步整理台词，或把选集放宽一点")
    sel_set = set(sel) if sel else None
    total = sum(float(ep.get("duration") or 0) for ep in p["episodes"]
                if sel_set is None or int(ep["index"]) in sel_set)
    if total < 60:
        raise RuntimeError(
            f"选中的素材只有 {total:.0f} 秒，太短了没法编排——多勾几集，或者把每套时长调短")

    def _tick(local_pct: float, msg: str) -> None:
        if progress is False:
            return
        # progress=(起点, 这一段占全程的百分比, 前缀)。多模型时每个模型只占
        # 全程的一截，否则每换一个模型进度都会从 6% 重新跳回去。
        if progress:
            base, span, prefix = progress
            core.set_task(pid, task_kind, base + local_pct / 100.0 * span, f"{prefix}{msg}")
        else:
            core.set_task(pid, task_kind, local_pct, msg)
    avg = (min_minutes + max_minutes) / 2 * 60
    need = avg * count
    # 2026-11-21：plans.txt 删掉了「每套给约 N 块备选」（用户不再挑高光，块数由模型
    # 按「一场戏一块、每块 60 秒以上」自己决定）。估算块数的 per_blocks 一并删除，
    # 不留死代码；要恢复「模型给富余、程序做取舍」时再从 _backup_prompts_20261121 取回。
    # 选集时集数/时长都按选集报：明明只给 8 集台词，却告诉它「共 40 集」，
    # 模型只会去猜没给出的那些集，或者反复引用不存在的编号。
    n_ep_in = len(sel) if sel else len(p["episodes"])
    # 2026-11-20 用户定：可用集清单不再单列一段——集数/时长和首行重复、
    # 禁令和 plans.txt 第 1 条重复，只留集号并进台词区标题（紧贴台词，指代更清楚）。
    # 台词标题由程序统一拼（模板/编辑框里不出现「## 全剧台词」，
    # 用户自定义模板末尾残留这行也会被剥掉，避免出现两遍标题）
    prompt = _fill(
        _strip_dialogue_tail(_read_prompt("plans.txt")),
        name=p["name"], n_ep=n_ep_in, count=count,
        total_min=f"{total / 60:.1f}", total_sec=f"{total:.0f}",
        min_sec=f"{min_minutes * 60:.0f}", max_sec=f"{max_minutes * 60:.0f}",
        avg_sec=f"{avg:.0f}", need_sec=f"{need:.0f}",
        reuse=f"{need / max(1.0, total):.2f}",
    ) + "\n\n" + _read_prompt("plans_schema.txt") \
      + (f"\n\n## 本批台词（第 {'、'.join(str(i) for i in sel)} 集）\n"
         if sel else "\n\n## 全剧台词\n") \
      + _all_text(p, gid=True, pos=True, eps=sel)

    _tick(6, (f"AI 正在通读选中的这 {len(sel)} 集（{_eps_label(sel)}，{total / 60:.0f} 分钟），编排 {count} 套不同剪法"
              if sel else f"AI 正在通读全剧 {total / 60:.0f} 分钟，编排 {count} 套不同剪法"))
    # model 不为空 = 这次编排单独指定模型（第 2 步的多模型对比：每个模型各自出几套），
    # Key/路由仍走 core.route 同一条规则，探针、结构、分镜的行为完全一致。
    data = _call(prompt, model=model or None, timeout=timeout)
    raw = [x for x in (data.get("plans") or []) if isinstance(x, dict)]
    if not raw:
        raise RuntimeError("AI 没有返回任何结构，请重试")

    # 第一阶段：把所有套先落到时间轴上（都不裁）。必须先收齐才能裁——
    # 裁第 3 套时要看得到前两套用了哪些素材，才能把重复部分先削掉。
    staged: list[dict] = []
    for pl in raw[:count]:
        b0 = _stitch_blocks(p, _to_segs(p, pl, 6.0, eps=sel))
        # 缝合之后再收整集块：缝合可能把两段并成「从集首到集尾」，所以顺序不能反。
        segs = _trim_whole(p, b0)
        if segs:
            staged.append({"name": str(pl.get("name") or f"第 {len(staged) + 1} 套")[:40],
                           "hook": _clean_hook(pl.get("hook")),
                           "bgm": _clean_bgm(pl.get("bgm")),
                           "segments": segs})

    # 每套的 blk 都是 s1/s2… 自己编号，跨套借块时会撞号（_drop_slivers 按 blk
    # 分组，撞号会把两套的块当成同一块合并）。加套号前缀区分开。
    spare: list[dict] = []
    for i, st in enumerate(staged):
        for s in st["segments"]:
            s["blk"] = f"{i}_{s['blk']}"
        spare.extend(st["segments"])

    # 第二阶段：逐套收到目标时长，收的时候避开前面已经用掉的素材
    # 台账在这里只读一次：本批各套之间的重复由 avoid 管，台账管的是「以前用过」。
    usage = reuse.load(p)["keys"]
    out: list[dict] = []
    accepted: list[list[dict]] = []
    for i, st in enumerate(staged, 1):
        segs = _fit_target(p, st["segments"], avg, 10.0, avoid=accepted,
                           spare=spare, usage=usage)
        # 逐块补到硬下限（2026-11-22 C 档）：AI 按「一场戏」切块，从不按秒数算，
        # 6 批实测中位数 30-38 秒、最短 10.9 秒。提示词改了三轮都压不住，
        # 这里在已选定的块上补——延不动就整块丢，不让碎片进成片。
        segs = _lift_blocks(p, segs, BLOCK_MIN)
        # 裁完还不到目标的 85%，说明 AI 给这套的素材本身就少——把块向两头延一延，
        # 补到能发出去的长度（不新增素材，只让每场戏更完整）。
        segs = _grow_to(p, segs, avg * 0.85)
        if not segs:
            continue
        accepted.append(segs)
        out.append({"name": st["name"], "hook": st.get("hook") or "",
                    "bgm": st.get("bgm") or "", "segments": segs})
        _tick(8 + i / max(1, len(staged)) * 55,
              f"已编排 {i}/{len(staged)} 套，正在收紧时长")
    if not out:
        raise RuntimeError("AI 给出的结构都不可用（时长不足或编号越界），请重试")
    # 台账不在这里记：编排可能重跑/整套被丢，计划阶段记账会把没人看过的块也耗掉次数。
    # 真正的记账在出片成功那一刻（batch.render_plans → reuse.record）。
    return out


def run(pid: str) -> None:
    p = core.load(pid)
    if not p:
        return
    target = float(p["target_minutes"]) * 60
    if target < 20:
        raise RuntimeError("目标时长太短（至少 20 秒）")
    edl, data = _pipeline(p, "analyze", "funnel.txt", strict=True)
    p["edl"] = edl
    p["edl_text"] = edl_text(edl)
    p["notes"] = _note_text(data, data.get("stop") or {})
    p["stage"] = "analyzed"
    core.set_task(pid, None)
    # 只写本步拥有的字段：第 3 步（引流分镜）可能正在并行保存 p["shots"]，
    # 整份覆盖会把对方刚写好的结果清掉。
    core.save_merge(p, ["edl", "edl_text", "notes", "stage"])
