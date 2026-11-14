"""只读冒烟检查：一条命令确认「这个安装能不能正常用」。

用法：runtime\\Scripts\\python.exe tests\\smoke.py
2026-11-14 批2 B3（遗留清单）。纯只读——不建项目、不发视频、不改任何数据，
只查关键端点、关键文件和单实例锁，全部通过说明主干是通的。
退出码 0 = 全过；1 = 有失败（失败项会打明细）。
"""
import glob
import json
import os
import sqlite3
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BASE = "http://127.0.0.1:8100"
OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append((name, detail))
    print(("✓ " if cond else "✗ ") + name + ((" · " + detail) if detail else ""))


# ── 1. 服务在跑 ──────────────────────────────────────────────────────────
try:
    with urllib.request.urlopen(BASE + "/api/status", timeout=5) as r:
        json.loads(r.read())
    check("服务 /api/status 应答", True)
except Exception as exc:
    check("服务 /api/status 应答", False, str(exc))
    print("\n服务没起来，后面的检查全部跳过。先用 run.py（或 run_dev.bat）启动再跑。")
    sys.exit(1)

# ── 2. 管理员令牌（本地签发，只读用，5 分钟过期）────────────────────────
from app import core, security                                   # noqa: E402

db = sqlite3.connect(os.path.join(core.DATA_DIR, "app.db"))
db.row_factory = sqlite3.Row
me = db.execute("select * from users where status='active' order by id limit 1").fetchone()
check("存在可用账号", me is not None)
tok, _ = security.make_access_token(core.load_config()["jwt_secret"], dict(me), 5)
HDR = {"Authorization": "Bearer " + tok}


def get(path):
    req = urllib.request.Request(BASE + path, headers=HDR)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


# ── 3. 关键端点 ──────────────────────────────────────────────────────────
for path, name in [("/api/projects", "项目列表"),
                   ("/api/publish/queue", "发布队列"),
                   ("/api/publish/accounts", "发布账号"),
                   ("/api/config", "配置")]:
    try:
        get(path)
        check("端点 " + name, True)
    except Exception as exc:
        check("端点 " + name, False, str(exc))

# ── 4. 发布队列文件可解析 ────────────────────────────────────────────────
qf = os.path.join(core.DATA_DIR, "publish_queue.json")
try:
    q = json.load(open(qf, encoding="utf-8"))
    check("发布队列文件可解析", isinstance(q.get("jobs"), list),
          f"{len(q.get('jobs') or [])} 条作业")
except Exception as exc:
    check("发布队列文件可解析", False, str(exc))

# ── 5. 关键文件 / 目录 ───────────────────────────────────────────────────
for rel, name in [("web/index.html", "首页"), ("web/publish.js", "发布前端"),
                  ("web/style.css", "样式表"), ("bin/ffmpeg.exe", "ffmpeg"),
                  ("models/small", "whisper 模型"), ("prompts", "提示词目录")]:
    check("文件 " + name, os.path.exists(os.path.join(ROOT, rel)),
          os.path.join(ROOT, rel))

# ── 6. 实例锁：有且指向活着的进程 ────────────────────────────────────────
lockf = os.path.join(core.DATA_DIR, "instance.lock")
try:
    lock_pid = int(open(lockf, encoding="utf-8").read().strip() or 0)
    alive = core._pid_alive(lock_pid)
    check("单实例锁指向存活进程", alive and lock_pid != os.getpid(),
          f"PID {lock_pid}")
except FileNotFoundError:
    check("单实例锁存在", False, "instance.lock 不存在（服务可能不是用 run.py 起的）")
except Exception as exc:
    check("单实例锁存在", False, str(exc))

# ── 7. 所有前端 JS 语法可解析（防并行编辑写坏）──────────────────────────
import subprocess                                                 # noqa: E402
for js in sorted(glob.glob(os.path.join(ROOT, "web", "*.js"))):
    r = subprocess.run(["node", "--check", js], capture_output=True)
    check("JS 语法 " + os.path.basename(js), r.returncode == 0,
          r.stderr.decode("utf-8", "replace")[:120])

print(f"\n通过 {len(OK)} · 失败 {len(BAD)}")
if BAD:
    for name, detail in BAD:
        print("  失败:", name, ("· " + detail) if detail else "")
    sys.exit(1)
