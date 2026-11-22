"""桌面快捷方式（2026-11-04，配合单文件安装包）。

为什么需要：安装包把程序解到 `%LOCALAPPDATA%\\HookStudio`——用户永远不该自己去翻那个目录。
第一次启动成功后，在桌面放一个「钩子工坊」图标，以后双击图标即可。

只在分发版（dist_mode）生效：开发机上桌面多一个指向 python.exe 的图标纯属污染。
失败一律静默——快捷方式建不出来是小事，不能影响程序启动。
"""
from __future__ import annotations

import os
import subprocess
import sys

from . import core

_MARK = ".shortcut_done"


def _is_frozen() -> bool:
    """PyInstaller 打包后运行时 sys.frozen=True，sys.executable 就是那个 exe。"""
    return bool(getattr(sys, "frozen", False))


def ensure() -> None:
    cfg = core.load_config()
    if not cfg.get("dist_mode") or not _is_frozen():
        return
    base = core.BASE
    mark = os.path.join(base, "data", _MARK)
    if os.path.exists(mark):
        return

    exe = sys.executable
    name = "钩子工坊.lnk"
    ps = (
        "$ErrorActionPreference='Stop';"
        "$d=[Environment]::GetFolderPath('Desktop');"
        "$w=New-Object -ComObject WScript.Shell;"
        f"$s=$w.CreateShortcut((Join-Path $d '{name}'));"
        f"$s.TargetPath='{exe}';"
        f"$s.WorkingDirectory='{base}';"
        f"$s.IconLocation='{exe},0';"
        "$s.Description='钩子工坊 · 短剧钩子混剪';"
        "$s.Save()"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-Command", ps],
            timeout=25, capture_output=True,
            creationflags=core.POPEN_HIDE.get("creationflags", 0),
        )
        # 桌面可能被 OneDrive 重定向，或者用户压根没权限；无论哪种都别再重试
        os.makedirs(os.path.dirname(mark), exist_ok=True)
        open(mark, "w", encoding="utf-8").write(exe)
        print(f"[shortcut] 已创建桌面图标：{name}")
    except Exception as exc:  # noqa: BLE001
        print(f"[shortcut] 跳过（{type(exc).__name__}）")
