"""钩子工坊启动器：起本地服务 + 自动开浏览器。

开发：runtime\\Scripts\\python.exe run.py --reload
打包：由 PyInstaller 以本文件为入口，双击 exe 即可。
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser


def _port_free(port: int) -> bool:
    """注意：Windows 上不能设 SO_REUSEADDR，否则能重复绑定已占用端口，判断就失效了。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _ours(port: int) -> bool:
    """这个端口上跑的是不是已经开着的钩子工坊。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1.2) as r:
            return r.status == 200 and b"whisper_tier" in r.read()
    except Exception:  # noqa: BLE001
        return False


def _pick_port(base: int) -> tuple[int, bool]:
    """返回 (端口, 是否已在运行)。已经开着就直接复用，避免开第二个实例。"""
    for p in range(base, base + 20):
        if _ours(p):
            return p, True
        if _port_free(p):
            return p, False
    return base, False


def _open_when_ready(port: int) -> None:
    url = f"http://127.0.0.1:{port}/"
    for _ in range(120):
        time.sleep(0.4)
        if _ours(port):
            webbrowser.open(url)
            return
    webbrowser.open(url)


def _force_utf8_console() -> None:
    """Windows 控制台默认 GBK，中文横幅会变乱码；这里统一成 UTF-8。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:  # noqa: BLE001
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    _force_utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--reload", action="store_true", help="开发模式：改代码自动重启")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from app import core
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] 导入后端失败：{exc}")
        input("按回车退出…")
        return 1

    # 单实例锁：先于端口检测。端口检测挡不住「8100 被占后落到 8101 再起一个」
    # ——两个实例的 worker 会同时写同一份发布队列，数据写花（2026-11-14 实测）。
    if not core.acquire_instance_lock():
        try:
            hint_port = int(core.load_config().get("port") or 8100)
        except Exception:
            hint_port = 8100
        print("钩子工坊已经在运行（检测到另一个实例的锁）。", flush=True)
        if not args.no_browser:
            webbrowser.open(f"http://127.0.0.1:{hint_port}/")
        return 0

    base = args.port or int(core.load_config().get("port") or 8100)
    if base < 1:
        base = 8100
    port, running = _pick_port(base)
    url = f"http://127.0.0.1:{port}/"

    if running:
        print(f"钩子工坊已经在运行：{url}", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    missing = []
    if not os.path.isfile(core.ff("ffmpeg")):
        missing.append("bin\\ffmpeg.exe")
    if not os.path.isfile(os.path.join(core.model_dir(), "model.bin")):
        missing.append("models\\small\\model.bin")
    if missing:
        print("[警告] 以下文件缺失，转写或渲染会失败：", flush=True)
        for m in missing:
            print("   -", m, flush=True)

    print("=" * 56)
    print("  钩子工坊 已启动")
    print(f"  地址：{url}")
    print(f"  数据：{core.DATA_DIR}")
    if args.reload:
        print("  开发：改 app/ 自动重启，改 web/ 按 Ctrl+F5，改 prompts/ 刷新即生效")
    print("  关闭这个窗口即退出程序")
    print("=" * 56, flush=True)

    if not args.no_browser:
        threading.Thread(target=_open_when_ready, args=(port,), daemon=True).start()

    import uvicorn

    try:
        uvicorn.run(
            "app.main:app",
            host="127.0.0.1",
            port=port,
            reload=args.reload,
            reload_excludes=[
                "runtime/*", "data/*", "dist/*", "bin/*", "models/*", "deploy/*",
                "logs/*", "__pycache__/*", "*.log",
            ] if args.reload else None,
            log_level="info",
            access_log=False,
        )
    finally:
        # 干净退出时放掉锁；崩溃残留由下次启动的「进程存活判断」接管
        core.release_instance_lock()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
