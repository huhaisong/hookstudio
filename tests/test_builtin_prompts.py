# -*- coding: utf-8 -*-
"""独立校验内置提示词表（2026-11-22 防破解第一批）。

运行：python tests/test_builtin_prompts.py

故意不复用 gen_builtin_prompts.py 的任何逻辑，否则「生成错了、校验也跟着错」
会一起漏过去。校验三件事：
  1. _builtin_prompts.BUILTIN 的 10 份字节与 prompts/*.txt 逐字节一致；
  2. 默认模式（开关关）下 _default_prompt 的返回 == 旧口径文本模式读磁盘的 strip 结果；
  3. HOOK_DEV_PROMPTS 开关双路径：开→读磁盘、关→读内置、开但磁盘缺失→退回内置。
"""
from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

NAMES = [
    "plans.txt", "plans_schema.txt", "hooks.txt", "hooks_schema.txt",
    "shots.txt", "shots_user.txt", "shots_schema.txt", "shots_rules.txt",
    "funnel.txt", "system.txt",
]


def disk_bytes(name: str) -> bytes:
    with open(os.path.join(BASE, "prompts", name), "rb") as f:
        return f.read()


def test_byte_identity() -> None:
    from app import _builtin_prompts
    for name in NAMES:
        got = _builtin_prompts.BUILTIN.get(name)
        assert got == disk_bytes(name), (
            f"{name}: 内置字节与磁盘不一致（内置 {len(got) if got else 0} vs "
            f"磁盘 {len(disk_bytes(name))}）"
        )
    print("[OK] 1. 10 份提示词逐字节一致")


def test_default_equals_old() -> None:
    """默认模式必须和加固前的口径一模一样：文本模式读全文 + strip。"""
    from app import analyze
    assert not analyze._DEV_PROMPTS_ON, "默认模式开关必须是关的"
    for name in NAMES:
        with open(os.path.join(BASE, "prompts", name), encoding="utf-8") as f:
            old = f.read().strip()
        assert analyze._default_prompt(name) == old, f"{name}: 默认模式读取结果变了"
    print("[OK] 2. 默认模式与加固前口径一致（逐字）")


def test_dev_switch() -> None:
    from app import analyze as a
    saved_dir = a.PROMPT_DIR

    # 关：把 PROMPT_DIR 指向不存在的目录，仍应从内置取到（证明没走磁盘）
    a.PROMPT_DIR = os.path.join(BASE, "__no_such_prompts_dir__")
    try:
        assert a._default_prompt("plans.txt"), "开关关时磁盘缺失，内置兜底失败"
    finally:
        a.PROMPT_DIR = saved_dir
    print("[OK] 3a. 开关关 → 走内置（磁盘缺失也不崩）")

    # 开：reload 让模块顶部重读环境变量，用临时目录里改过的内容验证走磁盘
    old_env = os.environ.get("HOOK_DEV_PROMPTS")
    os.environ["HOOK_DEV_PROMPTS"] = "1"
    tmp = tempfile.mkdtemp(prefix="hook_dev_prompts_")
    try:
        with open(os.path.join(tmp, "plans.txt"), "wb") as f:
            f.write("DEV-ONLY-MARK\n".encode("utf-8"))
        importlib.reload(a)
        assert a._DEV_PROMPTS_ON, "开关开了但模块没认"
        a.PROMPT_DIR = tmp
        assert a._default_prompt("plans.txt") == "DEV-ONLY-MARK", "开关开时没读磁盘"

        # 开但磁盘缺失 → 退回内置
        a.PROMPT_DIR = os.path.join(BASE, "__no_such_prompts_dir__")
        assert a._default_prompt("plans.txt") != "DEV-ONLY-MARK", "开关开但磁盘缺失时没退回内置"
    finally:
        a.PROMPT_DIR = saved_dir
        if old_env is None:
            os.environ.pop("HOOK_DEV_PROMPTS", None)
        else:
            os.environ["HOOK_DEV_PROMPTS"] = old_env
        importlib.reload(a)
        shutil.rmtree(tmp, ignore_errors=True)
    print("[OK] 3b. 开关开 → 读磁盘；开但磁盘缺失 → 退回内置")


if __name__ == "__main__":
    test_byte_identity()
    test_default_equals_old()
    test_dev_switch()
    print("全部通过：内置提示词字节一致、默认口径不变、开发开关双路径正常")
