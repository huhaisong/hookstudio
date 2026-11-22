# -*- coding: utf-8 -*-
"""发布前断言扫描（防破解方案第三批）。

对打包产物目录做硬检查，任一不满足直接非零退出，杜绝「打完包没检查就发出去」。
检查项对应方案 §5 验证清单：
  1. 无 app/*.py 源码明文
  2. 无 prompts/*.txt 明文
  3. config 无 llm_key / ds_key
  4. 版本号两处一致

用法：
  python check_release.py <产物目录>          # 扫产物 + 版本号
  python check_release.py --src               # 只扫源码版本号（开发期自检）
"""
from __future__ import annotations

import json
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))


def fail(msg: str) -> None:
    print(f"  ✗ {msg}")
    raise SystemExit(1)


def scan_dir(target: str) -> None:
    print(f"扫描产物目录: {target}")
    if not os.path.isdir(target):
        fail(f"产物目录不存在: {target}")

    py_files, prompt_files = [], []
    for dp, dns, fns in os.walk(target):
        for fn in fns:
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, target)
            # app 源码 .py（混淆/编译后不该有）
            if fn.endswith(".py") and (os.sep + "app" + os.sep) in (os.sep + rel):
                py_files.append(rel)
            # prompts 明文
            if fn.endswith(".txt") and ("prompts" in rel.lower()):
                prompt_files.append(rel)

    # pyarmor/Cython 产物里会有运行时的 __init__.py（pyarmor_runtime 包自带），
    # 这是正常的；真正不该出现的是业务模块的源码
    biz_py = [p for p in py_files
              if not re.search(r"pyarmor_runtime|__init__", p, re.I)]
    if biz_py:
        fail(f"产物里有业务源码 .py（{len(biz_py)} 个）: {biz_py[:5]}")
    print(f"  ✓ 无业务源码 .py（运行时 __init__.py {len(py_files) - len(biz_py)} 个属正常）")

    if prompt_files:
        fail(f"产物里有 prompts 明文（{len(prompt_files)} 个）: {prompt_files[:5]}")
    print("  ✓ 无 prompts 明文")

    # config 无 Key
    cfg = os.path.join(target, "data", "config.json")
    if os.path.isfile(cfg):
        text = open(cfg, encoding="utf-8", errors="replace").read()
        for key in ("llm_key", "ds_key", "api_key"):
            # 允许空字符串 / 占位，但不允许有真实值
            m = re.search(rf'"{key}"\s*:\s*"([^"]+)"', text)
            if m and m.group(1) not in ("", "YOUR_KEY", "none", "null"):
                fail(f"产物 data/config.json 泄露 {key}")
    print("  ✓ config 无 Key")


def check_version() -> None:
    print("版本号一致性:")
    v1 = re.search(r'VERSION\s*=\s*"([^"]+)"',
                   open(os.path.join(BASE, "app", "buildstamp.py"), encoding="utf-8").read())
    v2 = json.load(open(os.path.join(BASE, "deploy", "proxy", "release.json"), encoding="utf-8"))
    if not v1 or not v2.get("version"):
        fail("版本号读取失败")
    if v1.group(1) != v2["version"]:
        fail(f"版本号不一致: buildstamp={v1.group(1)} release.json={v2['version']}")
    print(f"  ✓ 两处一致: {v1.group(1)}")


if __name__ == "__main__":
    if "--src" in sys.argv:
        check_version()
    else:
        if len(sys.argv) < 2:
            fail("用法: python check_release.py <产物目录>  或  --src")
        scan_dir(sys.argv[1])
        check_version()
    print("全部通过")
