"""繁体转简体（第 1 步「整理台词」的兜底项）。

为什么要这一层（2026-11-16 实测）：whisper 偶尔整段输出繁体——同一个项目里
第 3 集 24 句、第 25 集 11 句是「會員」「顧清棠」「餐廳」这种写法，其余集是简体。
观众看到的就是「同一部剧字幕一会儿简一会儿繁」。这跟人名错字是两码事：
人名靠 `app/names.py` 的对照表，繁简靠这里。

两层一起做（用户 2026-11-16 定 A+B）：
- **B｜源头**：转写时给模型一句「用简体中文」的提示（`prompt()` → initial_prompt），
  减少繁体产出；
- **A｜兜底**：转写完按 `app/zhdict.py` 的字表把繁体字换成简体，兜住 B 没管住的。

三条约束（都是防误伤，比"看起来都对"更重要）：
1. **一字对多的字不转**：OpenCC 里 `乾 干 乾`、`於 于 於`、`藉 藉 借` 这类，值里含
   自身的说明简体环境也要用这个字，硬转会把「乾隆」写成「干隆」。这类字只在
   `_PATCH` 里逐个确认后才加。
2. **先词组、后逐字**：字级替换转不对的就是「項鍊→项炼（应为项链）」「帳戶→帐户
   （应为账户）」这类，所以词组表（OpenCC TSPhrases 357 条）必须走在字表前面。
   除词组表外不再造词（不自己编词表，错了看不出来）。
3. 只改 `lines[].text`，**时间码一个字都不动**。改字会让 nosub 指纹失效
   （`nosub.sig_of` 把台词并进去了），所以第 2 步成品要重新生成才显示新字。
"""
from __future__ import annotations

import threading

from . import core, zhdict

CFG_KEY = "zh_t2s"

# 一字对多、但简繁转换确实该转的字：OpenCC 的 TSCharacters 因为「简体也用这个字」
# 把它们排除了，逐个确认后补在这里（右边必须是单字）。
#   瞭→了（瞭解/瞭望，字幕里几乎都是「了解」）、甦→苏（甦醒/苏醒）、
#   牠→它、祂→他、祇→只（祇是「只」的繁体）、祕→秘、昇→升、夥→伙、剋→克、纔→才
_PATCH = {
    "瞭": "了", "甦": "苏", "牠": "它", "祂": "他", "祇": "只",
    "祕": "秘", "昇": "升", "夥": "伙", "剋": "克", "纔": "才",
}

# 词组补丁：OpenCC 的 TSPhrases 没有「帳」这一类（它把 帳→帐 当通用规则），
# 但现代汉语规范里**跟钱有关的写「账」**（账户/账单），「帐」只留给帐篷、蚊帐。
# 字幕里「帐户」「查帐」看着就是错字，所以这几条自己定（只收这一类，不扩）。
_WORD_PATCH = {
    "帳戶": "账户", "帳號": "账号", "帳單": "账单", "帳本": "账本", "帳目": "账目",
    "轉帳": "转账", "記帳": "记账", "查帳": "查账", "對帳": "对账", "賒帳": "赊账",
}

_MAP: dict[str, str] = {}
_WORDS: list[tuple[str, str]] = []
_LOCK = threading.Lock()


def _table() -> dict[str, str]:
    """字表只建一次（2558 项，建表后是纯 dict 查表，比逐字 recreate 快一个量级）。"""
    global _MAP
    if _MAP:
        return _MAP
    with _LOCK:
        if _MAP:
            return _MAP
        m = dict(zip(zhdict.T, zhdict.S))
        m.update(_PATCH)
        _MAP = m
        return _MAP


def _word_list() -> list[tuple[str, str]]:
    """词组表（357 条，已按长度降序）。

    为什么非要词组在前面：字级替换会把「項鍊」变成「项炼」（鍊→炼），
    而正确的是「项链」；「帳戶」会变成「帐户」，简体里写「账户」。
    这类只能靠词组消歧，见 zhdict 头注释。
    """
    global _WORDS
    if _WORDS:
        return _WORDS
    with _LOCK:
        if not _WORDS:
            merged = dict(zip(zhdict.W_T, zhdict.W_S))
            merged.update(_WORD_PATCH)
            # 长的先替换：先「帳戶」再「帳」，免得短词把长词切走
            _WORDS = sorted(merged.items(), key=lambda x: -len(x[0]))
        return _WORDS


def enabled() -> bool:
    """开关默认开：默认关等于用户每次都要先去勾一下，这个功能本来就是「默认该对」。"""
    v = core.load_config().get(CFG_KEY, True)
    return bool(v)


def set_enabled(on: bool) -> bool:
    core.save_config({CFG_KEY: bool(on)})
    return bool(on)


def prompt() -> str:
    """给 whisper 的 initial_prompt（B 层）。

    只写一句、不写长段：这段文本会被模型当成「上文」接着写，越长越容易被它
    当成台词复述出来。`以下是普通话` 而不是「粤语」这类判断，是要把语言钉在
    普通话简体上，避免它跟着音频里的口音漂到台配用词。
    """
    return "以下是普通话的句子，请用简体中文转写。"


def to_simplified(s: str) -> tuple[str, int]:
    """返回（新文本，改了几个字）。没有繁体字时原样返回，n=0。

    顺序是**先词组、后逐字**，不能反（理由见 `_word_list`）。
    短语料里绝大多数句子一个繁体字都没有，先用 `any` 快速放行，
    免得每句都跑 357 次 replace。
    """
    if not s:
        return s, 0
    m = _table()
    if not any(ch in m for ch in s):
        return s, 0
    n = 0
    for t, v in _word_list():
        c = s.count(t)
        if c:
            s = s.replace(t, v)
            n += c * len(t)
    out: list[str] = []
    for ch in s:
        v = m.get(ch)
        if v is None:
            out.append(ch)
        else:
            out.append(v)
            n += 1
    return "".join(out), n


def count(s: str) -> int:
    """一句话里有几个繁体字（界面上报「还有多少处」用）。"""
    if not s:
        return 0
    m = _table()
    return sum(1 for ch in s if ch in m)


def size() -> int:
    """字表能转的字数（界面显示，也用来核对字表是否装进去了）。"""
    return len(_table())


def apply_lines(lines: list[dict]) -> int:
    """就地转一集台词，返回改了几个字。时间码不动。"""
    if not lines:
        return 0
    n = 0
    for ln in lines:
        t = str(ln.get("text") or "")
        nt, c = to_simplified(t)
        if c:
            ln["text"] = nt
            n += c
    return n


def apply_project(p: dict) -> dict:
    """扫整个项目，就地转字。返回概况（界面提示用）。幂等：转过的再跑一次是 0。"""
    hit: list[dict] = []
    total = 0
    for ep in p.get("episodes") or []:
        n = apply_lines(ep.get("lines") or [])
        if n:
            total += n
            hit.append({"index": int(ep.get("index") or 0), "changed": n})
    return {"changed": total, "episodes": hit}
