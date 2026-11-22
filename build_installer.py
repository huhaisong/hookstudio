"""把 onedir 产物打成一个安装器 exe（2026-11-04）。

用法：python build_installer.py dist2\HookStudio [输出目录 dist3]

做三件事：
  1. 把 docs/使用说明.txt 同步进去（唯一事实来源在 docs/，产物目录里那份是副本）
  2. 用 7-Zip 把整个目录压成 build_tools/payload.7z（已压过就跳过，--force 重压）
  3. PyInstaller 把 installer.py + payload.7z + 7zr.exe 打成单文件 exe

为什么不在 build.bat 里直接写命令：cmd 会把带引号/中文的长命令拆坏（曾经踩过，
报错但退出码仍是 0）。所有复杂逻辑放 Python，bat 只调一行。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, "build_tools")
SEVEN = os.path.join(TOOLS, "7zfull", "7z.exe")
PAYLOAD = os.path.join(TOOLS, "payload.7z")


def sh(cmd: list[str], cwd: str | None = None) -> int:
    print("  $", " ".join(cmd[:6]), "…" if len(cmd) > 6 else "")
    return subprocess.call(cmd, cwd=cwd)


def _check_dist(src: str) -> int:
    """压 payload 前的两道闸（2026-11-22 补上，pack_zip.py 的 zip 流程早就有）：

    1. data/ 只许有 config.json——打完包验证双击跑一次 exe，程序就会把带
       jwt_secret 的 config、app.db、instance.lock（内含开发机绝对路径）写回
       data/；现在直接打安装器这些全进客户机器（实测 dist 里正躺着一批）。
    2. check_release.py 断言扫描：源码/提示词明文、Key 泄露、版本号。
    """
    data = os.path.join(src, "data")
    if os.path.isdir(data):
        leftovers = sorted(set(os.listdir(data)) - {"config.json"})
        if leftovers:
            print(f"[错误] {data} 里有测试残留：{leftovers}")
            print("       先跑 build_post.py 复位（或手动清掉），再打安装器。")
            print("       （zip 流程的 pack_zip.py 一直有这道闸，安装器不能比它松。）")
            return 6
    rc = subprocess.call([sys.executable, os.path.join(HERE, "check_release.py"), src])
    if rc != 0:
        print("[错误] 发布前扫描没过，先修再打包")
        return rc
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("用法：python build_installer.py <onedir 目录> [输出目录]")
        return 2
    src = os.path.abspath(sys.argv[1])
    out = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else os.path.join(HERE, "dist3")
    force = "--force" in sys.argv

    if not os.path.isfile(os.path.join(src, "钩子工坊.exe")):
        print(f"[错误] {src} 不像 onedir 产物（找不到 钩子工坊.exe）")
        return 3

    print("[0/3] 发布前检查（data 残留 + 断言扫描）")
    rc = _check_dist(src)
    if rc != 0:
        return rc

    print("[1/3] 同步使用说明")
    doc = os.path.join(HERE, "docs", "使用说明.txt")
    if os.path.exists(doc):
        shutil.copy2(doc, os.path.join(src, "使用说明.txt"))
        print("  已同步 docs/使用说明.txt")
    else:
        print("  [提醒] 找不到 docs/使用说明.txt，沿用产物里那份")

    print("[2/3] 压缩 payload")
    if force or not os.path.exists(PAYLOAD):
        if os.path.exists(PAYLOAD):
            os.remove(PAYLOAD)
        rc = sh([SEVEN, "a", "-t7z", "-mx=5", "-mmt=on", "-bso0", "-bsp0", PAYLOAD, "."],
                cwd=src)
        if rc != 0:
            print(f"[错误] 压缩失败（{rc}）")
            return 4
    else:
        print("  已有 payload.7z，跳过（要重压加 --force）")
    size = os.path.getsize(PAYLOAD) / 1024 / 1024
    print(f"  payload.7z = {size:.0f} MB")

    print("[3/3] 打包安装器")
    rc = sh([sys.executable, "-m", "PyInstaller", "installer.py", "--onefile", "--console",
             "--name", "钩子工坊-安装", "--distpath", out,
             "--workpath", os.path.join(HERE, "build3"),
             "--add-data", f"{PAYLOAD};.",
             "--add-data", os.path.join(TOOLS, "7zr.exe") + ";.",
             "--noupx", "--clean", "-y"], cwd=HERE)
    if rc != 0:
        print(f"[错误] PyInstaller 失败（{rc}）")
        return 5

    exe = os.path.join(out, "钩子工坊-安装.exe")
    print(f"\n完成：{exe}（{os.path.getsize(exe) / 1024 / 1024:.0f} MB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
