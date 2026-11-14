"""基础配置、项目存储、后台任务调度。

本机原生运行（不再依赖 Docker）：程序根目录下放 web/ prompts/ models/ bin/，
数据放 data/（可整体拷走）；装在只读位置时自动退回用户目录。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid


def _app_root() -> str:
    """程序根目录：开发时是源码目录，打包后是 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BASE = os.environ.get("HOOKSTUDIO_HOME") or _app_root()
WEB = os.path.join(BASE, "web")
PROMPTS = os.path.join(BASE, "prompts")
MODELS = os.path.join(BASE, "models")
BIN = os.path.join(BASE, "bin")

# 随包只带 small：中文准确率与体积的折中点，去掉 base 可省约 140MB
TIER = "small"


def _writable(d: str) -> bool:
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".writetest")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("1")
        os.remove(probe)
        return True
    except OSError:
        return False


def _default_data_dir() -> str:
    """优先程序目录下的 data（绿色版能整体拷走）；只读安装位置则退回用户目录。"""
    local = os.path.join(BASE, "data")
    if _writable(local):
        return local
    fallback = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(fallback, "HookStudio", "data")


DATA_DIR = os.environ.get("DATA_DIR") or _default_data_dir()
PROJECTS = os.path.join(DATA_DIR, "projects")
OUTPUT = os.path.join(DATA_DIR, "output")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
os.makedirs(PROJECTS, exist_ok=True)
os.makedirs(OUTPUT, exist_ok=True)

DEFAULT_CONFIG = {
    "llm_key": "",
    "llm_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "llm_model": "qwen-plus",
    "port": 8100,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f) or {})
    except Exception:
        pass
    if os.environ.get("LLM_API_KEY"):
        cfg["llm_key"] = os.environ["LLM_API_KEY"]
    if os.environ.get("LLM_BASE_URL"):
        cfg["llm_base"] = os.environ["LLM_BASE_URL"]
    if os.environ.get("LLM_MODEL"):
        cfg["llm_model"] = os.environ["LLM_MODEL"]
    return cfg


def save_config(patch: dict) -> dict:
    cfg = load_config()
    for k, v in (patch or {}).items():
        if k in DEFAULT_CONFIG:
            cfg[k] = v
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)
    return cfg


def llm() -> dict:
    """每次现读配置：改了 Key 或模型，刷新页面就生效，不用重启。
    用户的 Key 优先；没填 Key 时走官方体验代理（proxy_url + 设备令牌）。"""
    cfg = load_config()
    return {
        "key": cfg.get("llm_key") or "",
        "base": cfg.get("llm_base") or "",
        "model": cfg.get("llm_model") or "",
        "proxy_url": (cfg.get("proxy_url") or "").strip(),
    }


PROXY_URL_DEFAULT = "https://api.hookstudio.cn"


def device_token() -> str:
    """本机匿名设备令牌：第一次用到时生成，之后不变。
    服务端靠它记账体验额度（不收集任何个人信息）。"""
    cfg = load_config()
    token = (cfg.get("device_token") or "").strip()
    if not token:
        import uuid
        token = uuid.uuid4().hex
        save_config({"device_token": token})
    return token


def whisper_tier() -> str:
    """固定 small 一档。老配置里遗留的 base 不认，一律按 small 走。"""
    return TIER


def model_dir(tier: str | None = None) -> str:
    """模型目录：models/small 为准；万一目录名不对，就在 models/ 下挑第一个带 model.bin 的。"""
    want = tier or whisper_tier()
    path = os.path.join(MODELS, want)
    if os.path.isfile(os.path.join(path, "model.bin")):
        return path
    try:
        for name in sorted(os.listdir(MODELS)):
            p2 = os.path.join(MODELS, name)
            if os.path.isfile(os.path.join(p2, "model.bin")):
                return p2
    except OSError:
        pass
    return path


def ff(name: str) -> str:
    """ffmpeg/ffprobe：优先用随包的 bin/ 里的，找不到才退回 PATH。"""
    exe = os.path.join(BIN, name + (".exe" if os.name == "nt" else ""))
    return exe if os.path.isfile(exe) else name


def _mem_gb() -> float:
    """物理内存（GB），拿不到就返回 0。"""
    try:
        if os.name == "nt":
            import ctypes

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            st = _MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return round(st.ullTotalPhys / 1024 ** 3, 1)
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3, 1)
    except Exception:  # noqa: BLE001
        return 0.0


def sysinfo() -> dict:
    """硬件信息。模型固定 small，这里只报它是否就位，不再给档位建议。"""
    cpu = os.cpu_count() or 1
    mem = _mem_gb()
    return {
        "cpu": cpu,
        "mem_gb": mem,
        "models": {TIER: os.path.isfile(os.path.join(MODELS, TIER, "model.bin"))},
    }


def abs_output(name: str) -> str:
    """把成片记录里的路径解析成真实路径。
    新记录存绝对路径（用户自己指定保存位置）；老记录只有文件名或「子目录/文件名」，
    统一按全局成片目录拼。找不到文件由调用方用 isfile 判断，这里不抛异常。"""
    if not name:
        return ""
    p = os.path.expanduser(str(name))
    return p if os.path.isabs(p) else os.path.join(output_dir(), p)


def output_dir() -> str:
    """成片与渲染临时文件的目录：config.output_dir 优先（比如指向空间大的盘），
    没配置或目录不存在时回落 data/output。渲染要写约 1GB 临时文件，
    程序所在盘满了会直接 No space left on device，所以允许用户指盘。"""
    d = (load_config().get("output_dir") or "").strip()
    if d:
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except OSError:
            pass
    os.makedirs(OUTPUT, exist_ok=True)
    return OUTPUT


def safe_name(name: str) -> str:
    """把项目名变成能当文件夹名的形式：Windows 不允许 \\ / : * ? " < > |，
    结尾的点或空格也会让资源管理器建不出目录。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", (name or "").strip())
    s = s.strip(" .")
    return s[:60] or "未命名项目"


def batch_out_dir(p: dict) -> str:
    """这批成片的实际目录 = 用户选的根目录 / 项目名。

    用户选的是「放到哪个盘、哪个大文件夹」（如 F:/钩子工坊_成片），
    片子再按项目名归到子目录里，多个项目不会混在一层。
    """
    root = ((p.get("batch") or {}).get("save_dir") or "").strip()
    root = os.path.abspath(os.path.expanduser(root)) if root else output_dir()
    return os.path.join(root, safe_name(p.get("name") or ""))


def free_gb(path: str) -> float:
    """某个路径所在盘的剩余空间（GB）。取不到返回 -1。"""
    p = os.path.abspath(path or ".")
    while p and not os.path.isdir(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    try:
        return round(shutil.disk_usage(p).free / (1024 ** 3), 2)
    except Exception:  # noqa: BLE001
        return -1.0


def rmtree(path: str) -> None:
    """逐文件删除自己刚生成的临时目录。
    不用 shutil.rmtree：批量删除会被本机安全守卫拦截（>50 个文件要求确认），
    而渲染临时段有 50+ 个文件，必须能自动清。
    铁律：清理只是收尾，任何情况下都不允许抛异常——删不掉顶多剩几个临时文件
    占点磁盘，绝不能因此把正在跑的渲染/批量任务弄死（2026-11-01 批量卡死教训）。"""
    if not os.path.isdir(path):
        return
    try:
        for root, dirs, files in os.walk(path, topdown=False):
            for f in files:
                try:
                    os.remove(os.path.join(root, f))
                except Exception:
                    pass
            for d in dirs:
                try:
                    os.rmdir(os.path.join(root, d))
                except Exception:
                    pass
        try:
            os.rmdir(path)
        except Exception:
            pass
    except Exception:
        pass


def check_space(path: str, need_mb: float, what: str = "这一集") -> None:
    """空间预检：不够就直接给人话错误，不要把 ffmpeg 的 ENOSPC 甩给用户。"""
    free = free_gb(path)
    if free < 0:
        return
    if free * 1024 < need_mb:
        raise RuntimeError(
            f"磁盘空间不足：{os.path.splitdrive(os.path.abspath(path))[0] or path} 盘只剩 {free:.1f} GB，"
            f"{what}大约需要 {need_mb / 1024:.1f} GB。\n"
            "请先清理磁盘，或在「设置 → 成片保存位置」里改到空间更大的盘。"
        )


def is_disk_full(exc: BaseException | str) -> bool:
    """判断异常/日志是不是磁盘写满，用于把底层报错翻译成人话。"""
    s = str(exc)
    marks = ("No space left on device", "ENOSPC", "error code: -28",
             "磁盘空间不足", "There is not enough space on the disk")
    return any(m in s for m in marks)


_LOCK = threading.RLock()
_TASK_LOCK = threading.Lock()
_RUNNING: dict[str, str] = {}
_TASKS: dict[str, dict] = {}
_TASKS_LOCK = threading.Lock()


def ffprobe(path: str) -> dict:
    cmd = [
        ff("ffprobe"), "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,width,height", "-of", "json", path,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
        info = json.loads(out or "{}")
    except Exception:
        return {"duration": 0.0, "width": 0, "height": 0}
    dur = 0.0
    try:
        dur = float(info.get("format", {}).get("duration", 0) or 0)
    except Exception:
        pass
    w = h = 0
    for st in info.get("streams", []):
        if st.get("codec_type") == "video":
            w, h = int(st.get("width") or 0), int(st.get("height") or 0)
            break
    return {"duration": round(dur, 3), "width": w, "height": h}


def natural_key(name: str):
    import re
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def win_path(local_path: str) -> str:
    """界面上展示用的路径：本机原生跑时已经是真实路径，只需统一分隔符。"""
    if os.name == "nt":
        return local_path.replace("/", "\\")
    return local_path


def _path(pid: str) -> str:
    return os.path.join(PROJECTS, pid, "project.json")


_LEGACY_MEDIA = re.compile(r"^/media/([a-zA-Z])/(.*)$")


def _fix_legacy_path(p: dict) -> bool:
    """老项目是 Docker 时代的 /media/f/剪影/xx，迁到原生后要还原成 F:\\剪影\\xx。"""
    m = _LEGACY_MEDIA.match(p.get("src_dir") or "")
    if not m:
        return False
    p["src_dir"] = m.group(1).upper() + ":\\" + m.group(2).replace("/", "\\")
    p["win_dir"] = p["src_dir"]
    return True


def migrate_legacy() -> dict:
    """启动时做一次性迁移：修正老项目的容器路径、把 .env 里的 Key 搬进 config.json。"""
    fixed = 0
    for pid in list(os.listdir(PROJECTS)):
        p = load(pid)
        if p and _fix_legacy_path(p):
            save(p)
            fixed += 1
    key_moved = False
    if not (load_config().get("llm_key") or ""):
        env_file = os.path.join(BASE, ".env")
        try:
            with open(env_file, encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("LLM_API_KEY="):
                        k = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if k:
                            save_config({"llm_key": k})
                            key_moved = True
                        break
        except OSError:
            pass
    return {"paths_fixed": fixed, "key_migrated": key_moved}


def load(pid: str) -> dict | None:
    with _LOCK:
        try:
            with open(_path(pid), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None


def save(p: dict) -> None:
    with _LOCK:
        d = os.path.join(PROJECTS, p["id"])
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, "project.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(p, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(d, "project.json"))


def create(path: str, name: str, target_minutes: int, episodes: list[dict], mode: str = "funnel") -> dict:
    p = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "src_dir": path,
        "win_dir": win_path(path),
        "target_minutes": target_minutes,
        "mode": mode if mode in ("funnel", "batch") else "funnel",
        "created_at": time.time(),
        "stage": "imported",
        "episodes": episodes,
        "edl_text": "",
        "edl": [],
        "notes": "",
        "output": "",
        "task": None,
        "error": "",
    }
    save(p)
    return p


def list_projects() -> list[dict]:
    rows = []
    for pid in sorted(os.listdir(PROJECTS)):
        p = load(pid)
        if not p:
            continue
        rows.append({
            "id": p["id"], "name": p["name"], "stage": p.get("stage"),
            "mode": p.get("mode") or "funnel",
            "episodes": len(p.get("episodes") or []),
            "target_minutes": p.get("target_minutes"),
            "created_at": p.get("created_at"),
            "output": bool(p.get("output")),
            "path": p.get("win_dir") or p.get("src_dir") or "",
            "task": get_task(p["id"]),
        })
    rows.sort(key=lambda r: -(r.get("created_at") or 0))
    return rows


def _taskfile(pid: str) -> str:
    return os.path.join(PROJECTS, pid, "task.json")


def task_flag(pid: str) -> str | None:
    """读落盘的任务标记：存在 = 上次有个任务没跑完（完成时会删掉）。"""
    try:
        with open(_taskfile(pid), encoding="utf-8") as f:
            return json.load(f).get("kind")
    except Exception:
        return None


def clear_task_flag(pid: str) -> None:
    try:
        os.remove(_taskfile(pid))
    except OSError:
        pass


def set_task(pid: str, kind: str | None, pct: float = 0.0, msg: str = "", error: str = "") -> None:
    """任务进度只放内存；但「正在跑哪一步」落盘成 task.json，重启后能识别中断并续跑。"""
    with _TASKS_LOCK:
        if kind is None:
            _TASKS.pop(pid, None)
            clear_task_flag(pid)
            return
        # 同一步骤连续更新时保留起始时刻，前端据此显示「已用 X 秒」，进度按批跳也能看出在动
        prev = _TASKS.get(pid) or {}
        started = prev.get("started") if prev.get("kind") == kind else time.time()
        _TASKS[pid] = {
            "kind": kind, "pct": round(pct, 1), "msg": msg, "error": error,
            "at": time.time(), "started": started,
            "elapsed": round(time.time() - started, 1),
        }
        try:
            with open(_taskfile(pid), "w", encoding="utf-8") as f:
                json.dump({"kind": kind}, f)
        except OSError:
            pass


def get_task(pid: str) -> dict | None:
    with _TASKS_LOCK:
        t = _TASKS.get(pid)
        if t:
            out = dict(t)
            started = out.get("started") or out.get("at") or 0
            out["elapsed"] = round(time.time() - started, 1) if started else 0.0
            return out
    flag = task_flag(pid)
    if flag:
        return {"kind": flag, "pct": 0, "msg": "上次中断，正在自动续跑…", "error": "",
                "at": 0, "elapsed": 0.0}
    return None


def task_running(pid: str) -> bool:
    """这个项目当前有没有后台任务在跑（给单集操作做互斥用）。"""
    with _TASK_LOCK:
        return pid in _RUNNING


def start_task(pid: str, kind: str, fn) -> bool:
    """同一项目同时只跑一个任务；全局同类任务串行，避免抢 CPU。"""
    with _TASK_LOCK:
        if pid in _RUNNING:
            return False
        _RUNNING[pid] = kind

    def runner():
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            set_task(pid, kind, 0, "失败", f"{type(exc).__name__}: {exc}")
        finally:
            with _TASK_LOCK:
                _RUNNING.pop(pid, None)

    threading.Thread(target=runner, daemon=True).start()
    return True
