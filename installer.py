"""单文件安装器（2026-11-04）。

交付形态：**一个 exe**，双击即安装。它内部带两样东西：
  · payload.7z —— 整个运行目录（exe + _internal + models + web + prompts + bin）
  · 7zr.exe    —— 7-Zip 独立解压器（589 KB，只用来解 payload）

安装到 `%LOCALAPPDATA%\\HookStudio`，然后建桌面图标、启动程序、退出。
为什么不把程序本身做成单文件：见 docs/交付形态说明.md（900MB 每次启动解压到 temp，
启动要一两分钟，还会被杀软反复扫）。

重装不丢数据：先把老 `data` 目录整体挪走，解压完再搬回来——
项目、登录库、配置都在里面，绝不能被安装包覆盖。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

APP_NAME = "钩子工坊"
EXE_NAME = "钩子工坊.exe"


def _init_console() -> None:
    """中文 Windows 控制台默认是 GBK 代码页，直接 print 中文会乱码。
    切到 65001 并把 stdout 改成 UTF-8；失败就算了，不能因为显示问题装不上。"""
    try:
        if os.name == "nt":
            os.system("chcp 65001 > nul")
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def _pause(msg: str = "按回车退出...") -> None:
    """双击运行时等用户看一眼；从脚本/管道调用时没有 stdin，不能让它抛异常。"""
    try:
        input(msg)
    except (EOFError, OSError):
        pass


def _base() -> str:
    """PyInstaller 解包目录（onefile 运行时是临时目录）。"""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def _target() -> str:
    root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(root, "HookStudio")


def _running() -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {EXE_NAME}", "/NH"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout or ""
        return EXE_NAME in out
    except Exception:  # noqa: BLE001
        return False


def _shortcut(target: str) -> None:
    exe = os.path.join(target, EXE_NAME)
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$d=[Environment]::GetFolderPath('Desktop');"
        "$w=New-Object -ComObject WScript.Shell;"
        f"$s=$w.CreateShortcut((Join-Path $d '{APP_NAME}.lnk'));"
        f"$s.TargetPath='{exe}';"
        f"$s.WorkingDirectory='{target}';"
        f"$s.IconLocation='{exe},0';"
        "$s.Description='钩子工坊 · 短剧钩子混剪';"
        "$s.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   timeout=25, capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def main() -> int:
    _init_console()
    base = _base()
    payload = os.path.join(base, "payload.7z")
    z7 = os.path.join(base, "7zr.exe")
    target = _target()

    print("=" * 52)
    print(f"  {APP_NAME} 安装程序")
    print(f"  安装到：{target}")
    print("=" * 52)

    if not os.path.exists(payload) or not os.path.exists(z7):
        print("[错误] 安装包不完整（缺少 payload.7z 或 7zr.exe）")
        return 2

    if _running():
        print(f"\n[请先关闭] {APP_NAME} 正在运行，关掉那个黑色窗口再重新双击本安装包。")
        _pause()
        return 3

    # 1) 保住老数据：整个 data 目录挪到临时位置
    old_data = os.path.join(target, "data")
    saved = None
    if os.path.isdir(old_data):
        saved = os.path.join(tempfile.gettempdir(), f"hs_data_{int(time.time())}")
        print("\n[1/3] 备份原有数据…")
        try:
            shutil.move(old_data, saved)
        except Exception as exc:  # noqa: BLE001
            print(f"[错误] 旧数据挪不动（{exc}）。请关掉所有相关窗口后重试。")
            _pause()
            return 4
    else:
        print("\n[1/3] 全新安装，无旧数据")

    # 2) 解压
    print("[2/3] 正在解压（约 900 MB，需要 1-3 分钟，请勿关闭本窗口）…")
    os.makedirs(target, exist_ok=True)
    r = subprocess.run([z7, "x", payload, f"-o{target}", "-y", "-bso0", "-bsp0"])
    if r.returncode != 0:
        print(f"[错误] 解压失败（7zr 退出码 {r.returncode}）")
        _pause()
        return 5

    # 3) 放回老数据（覆盖安装包里那份干净 config）
    if saved and os.path.isdir(saved):
        print("[3/3] 恢复原有数据（项目/账号/配置）…")
        shutil.rmtree(os.path.join(target, "data"), ignore_errors=True)
        shutil.move(saved, os.path.join(target, "data"))
    else:
        print("[3/3] 完成")

    _shortcut(target)
    exe = os.path.join(target, EXE_NAME)
    print(f"\n安装完成。桌面已生成「{APP_NAME}」图标，正在启动…")
    try:
        # 两个坑叠在一起，缺一个都起不来（都实测过）：
        # ① 用 CREATE_NEW_CONSOLE 单开窗口：程序是控制台程序，靠那个窗口活着，
        #    DETACHED_PROCESS（不给控制台）会直接起不来。
        # ② 必须清掉 PyInstaller 的 _MEIPASS2 等变量：本安装器自己也是单文件打包，
        #    子进程继承这些变量后会误用「安装器的临时解包目录」，安装器一退出该目录被删，
        #    刚启动的程序随即消失（现象是端口起来几秒又没）。
        flags = (getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("_MEI") and not k.startswith("_PYI")}
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        subprocess.Popen([exe], cwd=target, close_fds=True,
                         creationflags=flags, env=env)
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 自动启动失败（{exc}），请双击桌面图标。")
        _pause()
        return 6
    time.sleep(2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
