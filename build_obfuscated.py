# -*- coding: utf-8 -*-
"""用 Cython 编译产物临时替换 app/*.py，打一个「机器码版」安装包（可逆）。

原理：PyInstaller 把 app/ 下的 .py 编成 .pyc 塞进 PYZ（可被反汇编）；.pyd 则作为
二进制扩展模块收集（机器码，无反编译器）。打包前把核心模块的 .py 暂时移走、换成
同名 .pyd，打完包在 finally 里无条件还原——任何失败都不破坏源码。

打包用基础 python + PYTHONPATH 指向 runtime/Lib/site-packages（runtime 的
python.exe 因中文路径坏了，实测不可用）。本脚本只负责「替换 → 打包 → 还原」；
发布前断言扫描（check_release.py）挂在 build.bat 第 4.5 步，组装资源时必经。

用法：
  python build_cython.py              # 先编译出 .pyd（首次/改代码后要重编）
  python build_obfuscated.py --dry    # 只演练替换/还原，不打包
  python build_obfuscated.py          # 临时替换 + 打包 + 还原 + 扫描
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(BASE, "app")
CY = os.path.join(BASE, "build_cython")
BAK = os.path.join(BASE, "_cython_bak")
SITE = os.path.join(BASE, "runtime", "Lib", "site-packages")
MODULES = [
    "analyze", "shots", "render", "retime", "proof", "batch",
    "transcribe", "names", "span_split", "audiofp", "bgmlib", "reuse",
]


def swap_in() -> list[str]:
    os.makedirs(BAK, exist_ok=True)
    swapped = []
    for mod in MODULES:
        pyd = os.path.join(CY, f"{mod}.cp313-win_amd64.pyd")
        if not os.path.isfile(pyd):
            print(f"  [跳过] {mod}: 没有编译产物 .pyd，请先跑 build_cython.py")
            continue
        py = os.path.join(APP, f"{mod}.py")
        if os.path.isfile(py):
            shutil.move(py, os.path.join(BAK, f"{mod}.py"))
        shutil.copy(pyd, os.path.join(APP, os.path.basename(pyd)))
        swapped.append(mod)
    return swapped


def swap_out(swapped: list[str]) -> None:
    for mod in swapped:
        bak = os.path.join(BAK, f"{mod}.py")
        if os.path.isfile(bak):
            shutil.move(bak, os.path.join(APP, f"{mod}.py"))
        for f in list(os.listdir(APP)):
            if f.startswith(mod + ".") and f.endswith(".pyd"):
                os.remove(os.path.join(APP, f))
    try:
        os.rmdir(BAK)
    except OSError:
        pass


def main() -> None:
    dry = "--dry" in sys.argv
    print("临时替换 app 源码为编译产物...")
    swapped = swap_in()
    print(f"  替换了 {len(swapped)} 个模块: {', '.join(swapped)}")
    if not swapped:
        swap_out(swapped)
        raise SystemExit("没有任何编译产物，退出")
    try:
        if dry:
            print("  [演练] 替换后状态:")
            for mod in swapped:
                py = os.path.join(APP, f"{mod}.py")
                pyd = [f for f in os.listdir(APP) if f.startswith(mod + ".") and f.endswith(".pyd")]
                print(f"    {mod}: .py存在={os.path.isfile(py)} .pyd={pyd}")
        else:
            print("PyInstaller 打包（onedir，基础 python + PYTHONPATH）...")
            env = os.environ.copy()
            env["PYTHONPATH"] = SITE + os.pathsep + BASE
            cmd = [
                sys.executable, "-m", "PyInstaller", "run.py",
                "--name", "HookStudio", "--onedir", "--console", "--noconfirm", "--clean", "--noupx",
                "--collect-submodules", "app", "--collect-submodules", "uvicorn",
                "--collect-all", "faster_whisper", "--collect-all", "ctranslate2",
                "--collect-all", "onnxruntime", "--hidden-import", "app.main",
                "--exclude-module", "torch", "--exclude-module", "torchaudio",
                "--exclude-module", "transformers", "--exclude-module", "scipy",
                "--exclude-module", "pandas", "--exclude-module", "matplotlib",
                "--exclude-module", "PySide6", "--exclude-module", "tkinter",
            ]
            subprocess.run(cmd, check=True, cwd=BASE, env=env)
            print("  打包完成")
    finally:
        print("  还原源码（无论成功失败）...")
        swap_out(swapped)
        print("  还原完成")

    if not dry:
        # 打包后检查源码目录干净
        leftover = [f for f in os.listdir(APP) if f.endswith(".pyd")]
        if leftover:
            raise SystemExit(f"源码目录有残留 .pyd: {leftover}")
        print("源码目录干净（无残留 .pyd）")
        # 提示后续步骤（资源组装 + 前端压缩 + 安装器）走 build.bat 的第 3-5 步逻辑
        print("下一步：把 dist/HookStudio/ 按 build.bat 第 3-5 步组装资源、压缩前端、打安装器")


if __name__ == "__main__":
    main()
