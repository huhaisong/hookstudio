# -*- coding: utf-8 -*-
"""用 Cython 把核心纯计算模块编译成 .pyd 机器码（防破解第二批）。

为什么不用 pyarmor：trial 版有 Big Script 限制，analyze.py/main.py/render.py
这些核心模块（100KB+）直接 out of license，覆盖不了最值钱的代码。
Cython 把 .py 编译成 C 再编机器码 .pyd，没有反编译器能还原出源码，门槛更高。

编译范围（只编纯计算、无 FastAPI 路由装饰器的模块）：
  路由模块（main/admin/audit/auth/publishapi）保留 .py —— Cython 编译后函数签名
  丢失，FastAPI 靠签名注入参数会全崩。
  core.py 保留 —— 基础设施，被所有模块引用，编译风险/收益比不划算。

产物：build_cython/<模块>.cp313-win_amd64.pyd
用法：python build_cython.py            # 全量编译
      python build_cython.py analyze    # 只编一个（小样验证）
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "build_cython")

# 核心业务逻辑：最值钱、最该藏的纯计算模块
# 不含路由模块（main/admin/audit/auth/publishapi）、不含 core.py
TARGETS = [
    "analyze",     # 编排核心（提示词拼装、块长算法）— 最值钱
    "shots",       # 分镜创作
    "render",      # EDL 渲染
    "retime",      # 词级对时
    "proof",       # 整剧校对
    "batch",       # 出片编排
    "transcribe",  # 转写
    "names",       # 人名归并
    "span_split",  # 区间切分
    "audiofp",     # 音频指纹
    "bgmlib",      # 配乐库
    "reuse",       # 复用检测
]


def zig_exe() -> str:
    """找到 zig 可执行文件（pip 装的 ziglang 包里）。"""
    import ziglang
    return os.path.join(os.path.dirname(ziglang.__file__), "zig.exe")


def cythonize_one(name: str, cc: str) -> str:
    """把一个 app/<name>.py 编译成 .pyd，返回产物路径。"""
    src = os.path.join(BASE, "app", f"{name}.py")
    if not os.path.isfile(src):
        raise FileNotFoundError(src)

    tmp = tempfile.mkdtemp(prefix=f"cython_{name}_")
    try:
        # 生成的 .c 放临时目录
        c_file = os.path.join(tmp, f"{name}.c")
        subprocess.run(
            [sys.executable, "-m", "cython", "-3", "-o", c_file, src],
            check=True, cwd=BASE,
        )
        # 用 zig cc 编成 .pyd（CPython 扩展模块）
        pyd = os.path.join(OUT, f"{name}.cp313-win_amd64.pyd")
        os.makedirs(OUT, exist_ok=True)
        import sysconfig
        inc = sysconfig.get_paths()["include"]
        libdir = sysconfig.get_config_var("LIBDIR")
        # Windows 下必须显式链接 python313.lib（import lib），否则 lld-link 报
        # undefined symbol: PyModuleDef_Init 等一堆 python C API 符号。
        # libdir 来自 sysconfig，保证和当前解释器 ABI 一致（本机指向 WorkBuddy
        # vendor 的 python/libs，与 3.13.14 配套）。
        lib_arg = [f"-L{libdir}", "-lpython313"] if libdir and os.path.isdir(libdir) else []
        cmd = [
            cc, "cc", "-shared", "-O2",
            "-fno-exceptions",
            f"-I{inc}",
            "-DWIN32", "-DNPY_NO_DEPRECATED_API=NPY_1_7_API_VERSION",
            c_file,
            *lib_arg,
            "-o", pyd,
        ]
        subprocess.run(cmd, check=True, cwd=tmp)
        return pyd
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    only = sys.argv[1:] or TARGETS
    cc = zig_exe()
    print(f"编译器: {cc}")
    for name in only:
        print(f"编译 {name} ...", flush=True)
        try:
            pyd = cythonize_one(name, cc)
            print(f"  ✓ {os.path.basename(pyd)} ({os.path.getsize(pyd)} bytes)")
        except Exception as exc:
            print(f"  ✗ {name}: {exc}")
            raise


if __name__ == "__main__":
    main()
