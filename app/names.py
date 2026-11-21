"""人名（专名）写法统一：转写出来的人名常是同音错字，用一张对照表纠正。

为什么需要这一层（2026-11-16 实测四模型）：
whisper 各家对同一个人名的写法都不一致——原片烧录字幕放大后确认是「顾清**棠**」，
small 写「顾清**唐**」、large-v3-turbo 写「顾**青棠**」、large-v3 写「顾清**堂**」，
只有 Belle 对（而 Belle 分句彻底失效，已被淘汰）。换模型救不了这件事，
所以不靠模型猜，改成「用户给一张对照表，转写后按表替换」。

对照表：`data/names.txt`，一行一个名字——

    清棠 = 清唐, 清堂, 青棠

左边是真名，右边是识别出来的各种错写法（逗号/顿号分隔）。`#` 开头是注释。
换剧时直接改这个文件，或者在第 1 步界面上改（两个入口写的是同一个文件）。

三条设计约束：
1. **只替换「整词完全相同」的错写法**，不做拼音/相似度模糊匹配——人名误替换
   比错字更难发现，宁可漏改也不能改错。
2. 规则按「错写法长度从长到短」执行：表里同时有 `顾清唐` 和 `清唐` 时，
   「顾清唐」先被整词换掉，不会再被短规则切一半。
3. **拒绝「错写法是正确写法的子串」这类规则**（如 `清棠 = 顾清棠`）：那会让
   每跑一次文本就变长一截。这类行在保存时直接标为无效并回报给界面。

时间码一个字都不动：这里只改 lines[].text，start/end 原样。
改字会让 nosub 指纹失效（sig_of 把台词内容并进去了），所以要重新点一次生成。
"""
from __future__ import annotations

import os
import re

from . import core

NAMES_FILE = os.path.join(core.DATA_DIR, "names.txt")

DEFAULT_TEXT = """# 人名写法统一表（2026-11-16 建）
#
# 左边 = 正确的写法，右边 = 识别出来的各种错写法，逗号分开。
# 一行一个名字；# 开头是注释。改完保存，下次转写自动套用；
# 已经在项目里的台词，点「现在统一一次」立刻生效。
#
# 下面这组是按当前这部剧填的（原片字幕确认是「顾清棠」），换剧就换成你的名字。

清棠 = 清唐, 清堂, 青棠, 清塘
顾清棠 = 顧清棠, 顧清唐, 顾清唐
"""

_SPLIT = re.compile(r"^([^=＝:：]+)[=＝:：](.+)$")
_SEP = re.compile(r"[,，、;；\s]+")


def _read() -> str:
    if not os.path.isfile(NAMES_FILE):
        try:
            os.makedirs(os.path.dirname(NAMES_FILE), exist_ok=True)
            with open(NAMES_FILE, "w", encoding="utf-8") as f:
                f.write(DEFAULT_TEXT)
        except OSError:
            return DEFAULT_TEXT
        return DEFAULT_TEXT
    try:
        with open(NAMES_FILE, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return DEFAULT_TEXT


def text() -> str:
    """对照表原文（给界面编辑用）。"""
    return _read()


def parse(raw: str) -> tuple[list[tuple[str, str]], list[str]]:
    """解析成 [(错写法, 正确写法)]，按错写法长度降序；第二个返回值是无效行说明。"""
    bad: list[str] = []
    pairs: list[tuple[str, str]] = []
    for ln, line in enumerate((raw or "").splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = _SPLIT.match(s)
        if not m:
            bad.append(f"第 {ln} 行没看懂：{s[:24]}")
            continue
        good = m.group(1).strip()
        if not good:
            bad.append(f"第 {ln} 行左边是空的：{s[:24]}")
            continue
        for w in _SEP.split(m.group(2).strip()):
            w = w.strip()
            if not w:
                continue
            if w == good:
                bad.append(f"第 {ln} 行左右一样（{w}），已忽略")
                continue
            if w in good:
                # 例：清棠 = 顾清棠 —— 每替换一次文本就变长，会把整篇台词改烂
                bad.append(f"第 {ln} 行「{w}」是「{good}」的一部分，会把名字越替越长，已忽略")
                continue
            pairs.append((w, good))
    # 长的先替换：表里同时有「顾清唐」和「清唐」时，整词先被换掉，短规则不会再切一半
    pairs.sort(key=lambda x: -len(x[0]))
    return pairs, bad


def save(raw: str) -> tuple[list[tuple[str, str]], list[str]]:
    pairs, bad = parse(raw)
    os.makedirs(os.path.dirname(NAMES_FILE), exist_ok=True)
    with open(NAMES_FILE, "w", encoding="utf-8") as f:
        f.write(raw if raw is not None else "")
    return pairs, bad


def rules() -> list[tuple[str, str]]:
    """当前生效的规则（每次现读文件，改完不用重启）。"""
    return parse(_read())[0]


def hint(limit: int = 8) -> str:
    """给转写模型的人名提示（并进 initial_prompt，不是 hotwords 参数）。

    为什么不能用 hotwords：faster-whisper 的 transcribe.py:1542 是
    `if previous_tokens or (hotwords and not prefix)`，**prefix 非空时
    hotwords 整个被忽略**；本项目默认传了 initial_prompt（简体提示），
    照字面加 hotwords 等于什么都没做。所以只能把名字写进提示词本身。

    短名是长名的一部分时只留长的（「清棠」⊂「顾清棠」），否则一句里
    同一个名字出现两次，反而容易让模型把它当台词复述出来。
    """
    goods: list[str] = []
    for _bad, good in rules():
        if good not in goods:
            goods.append(good)
        if len(goods) >= limit:
            break
    goods = [g for g in goods if not any(g != o and g in o for o in goods)]
    if not goods:
        return ""
    return "剧中人物：" + "、".join(goods) + "。"


def apply_text(s: str, rs: list[tuple[str, str]] | None = None) -> tuple[str, int]:
    """替换一段文本，返回（新文本，改了几处）。"""
    rs = rules() if rs is None else rs
    n = 0
    for bad, good in rs:
        c = s.count(bad)
        if c:
            s = s.replace(bad, good)
            n += c
    return s, n


def apply_lines(lines: list[dict], rs: list[tuple[str, str]] | None = None) -> int:
    """就地改一集台词的字，返回改了几处。时间码不动。"""
    rs = rules() if rs is None else rs
    if not rs or not lines:
        return 0
    n = 0
    for ln in lines:
        t = str(ln.get("text") or "")
        if not t:
            continue
        nt, c = apply_text(t, rs)
        if c:
            ln["text"] = nt
            n += c
    return n


def apply_project(p: dict) -> dict:
    """扫一个项目的全部台词，就地改字。返回改动概况（给界面提示用）。"""
    rs = rules()
    hit: list[dict] = []
    total = 0
    for ep in p.get("episodes") or []:
        n = apply_lines(ep.get("lines") or [], rs)
        if n:
            total += n
            hit.append({"index": int(ep.get("index") or 0), "changed": n})
    return {"changed": total, "episodes": hit, "rules": len(rs)}
