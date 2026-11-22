# -*- coding: utf-8 -*-
"""一次性脚本：把 prompts/*.txt 按原字节生成为 app/_builtin_prompts.py。

用法（在 hookstudio/ 下）：python gen_builtin_prompts.py

为什么必须按原字节、用脚本生成而不是手搬：
  analyze._split_fixed() 靠 FIXED_TAIL_MARKS 的标记行逐字符匹配来切「固定尾巴」。
  现在 FIXED_TAIL_MARKS 是空表，机制没在跑；但代码里保留了恢复路径，一旦将来
  恢复，任何空行/换行的改动都会让锚点匹配失败，固定尾巴被当成指令拼进请求，
  整条生成链路解析全崩。所以内置时严格保留原文，并做逐字节断言。

生成后请运行 python tests/test_builtin_prompts.py 做独立校验（不复用本脚本）。
"""
from __future__ import annotations

import os

BASE = os.path.dirname(os.path.abspath(__file__))
# 顺序与 main.py 的 _PROMPTS 表一致，便于人工核对
FILES = [
    "plans.txt", "plans_schema.txt", "hooks.txt", "hooks_schema.txt",
    "shots.txt", "shots_user.txt", "shots_schema.txt", "shots_rules.txt",
    "funnel.txt", "system.txt",
]


def read_bytes(name: str) -> bytes:
    with open(os.path.join(BASE, "prompts", name), "rb") as f:
        return f.read()


def main() -> None:
    lines = [
        '"""内置提示词常量表（生成物，勿手工编辑）。',
        "",
        "由 gen_builtin_prompts.py 从 prompts/*.txt 按原字节生成。",
        "每个值是 bytes，读取处（analyze._default_prompt）负责 decode + 换行归一化 + strip，
口径与旧的文本模式 open(..., encoding="utf-8") 完全一致。",
        '"""',
        "",
        "BUILTIN: dict[str, bytes] = {",
    ]
    for name in FILES:
        raw = read_bytes(name)
        # repr(bytes) 是纯 ASCII，避免任何编码/换行在写入时被二次改写
        lines.append(f"    {name!r}: {raw!r},")
    lines.append("}")
    lines.append("")

    out = os.path.join(BASE, "app", "_builtin_prompts.py")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))

    # 生成即自检：逐字节比对，防止 repr/换行在写入过程被改写
    ns: dict = {}
    with open(out, encoding="utf-8") as f:
        exec(compile(f.read(), out, "exec"), ns)
    got = ns["BUILTIN"]
    for name in FILES:
        assert got.get(name) == read_bytes(name), f"内置字节校验失败：{name}"
    print(f"已生成 {out}（{len(FILES)} 份，逐字节一致）")


if __name__ == "__main__":
    main()
