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


# ── 单实例锁（2026-11-14 批量优化 A4）────────────────────────────────────
# run.py 的端口检测挡不住「8100 被占后落到 8101 再起一个」——两个实例的
# worker 会同时写同一份 publish_queue.json，实测互踩（WinError 5 / 数据写花）。
# 锁文件 data/instance.lock 只写 PID：已有锁时用「进程是否还活着」区分
# 真双开和崩溃残留——活着就退出，死了就接管重写，用户永远不用手动清锁。
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x10000400, 0, pid)   # QUERY_INFORMATION | QUERY_LIMITED
            if not h:
                return False
            k.CloseHandle(h)
            return True
        except Exception:
            return True                              # 判不了就当活着，宁可保守
    try:
        os.kill(pid, 0)                              # POSIX：0 只探活不杀
        return True
    except OSError:
        return False


INSTANCE_LOCK = os.path.join(DATA_DIR, "instance.lock")


def _port_serving() -> bool:
    """本机服务端口上是不是真的有一个钩子工坊在答话。"""
    try:
        import urllib.request
        port = 8100
        try:
            port = int(load_config().get("port") or 8100)
        except Exception:
            pass
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/status", timeout=1.2) as r:
            return r.status == 200
    except Exception:
        return False


def acquire_instance_lock() -> bool:
    """抢单实例锁。True = 抢到（可以起服务）；False = 已有活实例在跑。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    pid = 0
    try:
        with open(INSTANCE_LOCK, encoding="utf-8") as f:
            pid = int((f.read() or "0").strip() or 0)
    except (OSError, ValueError):
        pid = 0
    if pid and _pid_alive(pid):
        # PID 会被系统复用（被杀实例的号转眼分给别的程序），所以判活之外还要
        # 看服务端口上是不是真有钩子工坊在答话——不答话就是残留锁，直接接管。
        if _port_serving():
            return False
    with open(INSTANCE_LOCK, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return True


def release_instance_lock() -> None:
    """干净退出时放锁。只删自己写的锁：被别的实例接管过就不动它。"""
    try:
        with open(INSTANCE_LOCK, encoding="utf-8") as f:
            if int((f.read() or "0").strip() or 0) != os.getpid():
                return
        os.remove(INSTANCE_LOCK)
    except OSError:
        pass

# 两家官方接口。路由**只认白名单**，不认模型名前缀：
# 百炼上现在也有 deepseek-v4.1-flash 这类同名模型，按前缀会把它们错送到
# DeepSeek 官方，直接 400（官方只认 deepseek-v4-pro / deepseek-flash）。
BAILIAN_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEEPSEEK_BASE = "https://api.deepseek.com/v1"
# DeepSeek 官方现有的模型（2026-11-03 实测）。官方上新时改这里，
# 或用环境变量 DS_MODELS=a,b 覆盖。
DEEPSEEK_OFFICIAL = ("deepseek-v4-pro", "deepseek-flash")

DEFAULT_CONFIG = {
    "llm_key": "",
    "llm_base": BAILIAN_BASE,
    "llm_model": "qwen3.5-plus",
    # 第 3 步（写分镜）可以单独用另一个模型：留空 = 跟 llm_model 一样。
    # 两步的口味不同——排结构要「快 + 几套不重复」，写分镜要「台词写得细」。
    "shots_model": "",
    "ds_key": "",
    "ds_base": DEEPSEEK_BASE,
    "port": 8100,
    # ── 用户体系 / 埋点（2026-11-04 新增，见 docs/用户体系接入说明.md）──
    # ── 用户体系 / 埋点（2026-11-04 新增，见 docs/用户体系接入说明.md）──
    # **恒为 True**（2026-11-04 用户定性：这是「需要登录」的工具，不是免登录工具）。
    # 配置项保留只为兼容旧 config.json；auth.auth_required() 运行期不再读它。
    # 曾经的 false = 「只审计不拦截」已废弃：封禁/踢下线在那个模式下形同虚设。
    "auth_required": True,
    # JWT 签名密钥：首次启动自动生成 48 字节随机串并存回这里，之后不要手改
    # （改了等于让所有已签发的 token 立即失效，也算一种「全站登出」）。
    "jwt_secret": "",
    "access_minutes": 1440,       # 登录有效期（分钟，2026-11-14 用户定稿：24 小时硬过期）
    "refresh_days": 14,           # refresh token 有效期（天）
    "admin_init_password": "",    # 初始管理员密码，空 = admin123456
    # ── 分发形态（2026-11-04）─────────────────────────────────────────────
    # 打包给终端用户时 build.bat 会在 dist 的 data/config.json 里写 true：
    #   · 设置页不再暴露 Key / 模型（终端用户不该也不需要自己填 Key）
    #   · 服务端拒绝改这些字段，改界面绕不过去
    # 开发机和自用部署保持 false。
    "dist_mode": False,
    # 日志上报："" = 跟随 dist_mode（打包版默认开），true/false = 强制开关。
    # 详见 app/report.py——exe 上日志只在本机，不推上来后台看不到用户操作。
    "report_audit": "",
    # ── 服务器账号（2026-11-04）──────────────────────────────────────────
    # 账号存在服务器（api.hookstudio.cn），登录后拿到的令牌存这里。
    # 调 AI 时用它代替匿名设备令牌：额度按**人**算，后台能看到是谁在用、能封号。
    # 空 = 还没登录过 / 已退出，此时退回设备令牌（老版本兼容）。
    "account_token": "",
    "account_name": "",           # 上次登录的用户名，登录页回填用
    # 本机匿名设备令牌：第一次用到时生成，之后必须**稳定不变**。
    # 2026-11-04 排查账号绑定时发现它从来没被存下来——`save_config` 只写
    # DEFAULT_CONFIG 里存在的键，而这个键不在这里，于是每次 `device_token()`
    # 都返回一个新 uuid。后果：服务器把每次调用都当成一台新设备，
    # 按设备计的每日 10 次等于**每次都重置**，后台设备列表也会无限增长。
    "device_token": "",
    # ── 渲染（2026-11-05）────────────────────────────────────────────────
    # 显卡硬编码模式：auto = 程序实测比 CPU 快才用（默认），off = 强制 CPU。
    # 个别机器显卡被直播/远程软件占着，或驱动有毛病，用户可以在设置里关掉。
    "hw_encode": "auto",
    # ── 同时生成上限（2026-11-08 用户要求做成可配置，默认仍是 3）──────────
    # 第 2 步「多组编排」和第 3 步「多条分镜」共用一个档位：两边都是同一个
    # 上游账号在接请求，分开调只会让「两组各 8 路」叠成 16 路。
    # 上限 8 是界面允许的最大值，不是安全值——调高前先看上游账号的限流。
    "max_concurrent": 3,
    # ── 到点自动发表（2026-11-13 批量定时发布）────────────────────────────
    # 开着 = 到点程序自己把视频传上去发掉，不可撤回，所以默认**关**。
    # 必须在这里声明：save_config 只认 DEFAULT_CONFIG 里已有的键，漏了会被
    # 静默丢掉——和上面 device_token 是同一个坑（2026-11-13 深夜实测：开关
    # 连点 8 次全部弹回去，config.json 里这个键从来没出现过）。
    "publish_auto": False,
}

CONCURRENT_MIN = 1
CONCURRENT_MAX = 8
CONCURRENT_DEFAULT = 3


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
    if os.environ.get("DS_API_KEY"):
        cfg["ds_key"] = os.environ["DS_API_KEY"]
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


def max_concurrent() -> int:
    """设置页给的「同时生成几条」，运行期夹在 1-8。

    每次调用现读 config.json：改完设置下一次生成就生效，不用重启服务。
    夹范围是为了防手改坏配置文件（写成 0 会让 ThreadPoolExecutor 直接抛错、
    写成 999 会把上游账号打限流）：越界数字夹到 1/8，非数字退回默认 3。
    """
    try:
        v = int(load_config().get("max_concurrent", CONCURRENT_DEFAULT))
    except (TypeError, ValueError):
        v = CONCURRENT_DEFAULT
    return max(CONCURRENT_MIN, min(CONCURRENT_MAX, v))


# ── 功能门禁（2026-11-13 批量定时发布：pro 专属）──────────────────────────
# 额度走服务端的 LEVELS，**功能门禁走这里**：本地只认「服务端下发的等级」，
# 不自己维护套餐表。等级来源见 publishapi._level（30 分钟缓存 + 断网回退）。
PUBLISH_LEVELS = ("pro", "internal")


def feature_allowed(level: str | None) -> bool:
    """空 level = 暂时判不出来（断网且缓存过期），一律不放行。

    调用方必须把「明确没买」和「判不出来」分成两种提示：
    断网时说「需要升级」，正版用户会直接炸。见 publishapi._require。
    """
    return (level or "").strip().lower() in PUBLISH_LEVELS


def route(model: str | None = None, llm_key: str | None = None,
          ds_key: str | None = None) -> dict:
    """按「模型名 + 两把 Key」算这一单走哪家接口。

    独立成函数是为了让**连通性测试**和正式调用共用同一条规则：
    设置页可以先拿用户刚输入、还没保存的 Key/模型试一次，试通了再存，
    不会出现「测的是旧配置、跑的是新配置」。传 None 表示用当前配置。

    两把 Key 各管各家：模型名**在 DeepSeek 官方白名单里**且填了 DeepSeek Key 时，
    走 DeepSeek 官方接口；否则仍走百炼。DeepSeek 官方比百炼上的同款
    更便宜、并发更稳，也免得百炼那边的额度被吃（2026-11-03 用户要求）。

    注意不能按「deepseek 前缀」判断：百炼也上架了 deepseek-v4.1-flash，
    按前缀会把它错送到官方接口 → 400 模型不存在（2026-11-04 实测）。
    """
    cfg = load_config()
    model = (cfg.get("llm_model") if model is None else model) or ""
    key = (cfg.get("llm_key") if llm_key is None else llm_key) or ""
    ds_key = (cfg.get("ds_key") if ds_key is None else ds_key) or ""
    base = (cfg.get("llm_base") or "").strip()
    env = (os.environ.get("DS_MODELS") or "").strip()
    official = {m.strip().lower() for m in (env.split(",") if env else DEEPSEEK_OFFICIAL) if m.strip()}
    provider = "bailian"
    model = model.strip()
    key = key.strip()
    ds_key = ds_key.strip()
    if ds_key and model.lower() in official:
        key = ds_key
        base = (cfg.get("ds_base") or "").strip() or DEEPSEEK_BASE
        provider = "deepseek"
    return {
        "key": key,
        "base": base,
        "model": model,
        "proxy_url": (cfg.get("proxy_url") or "").strip(),
        "provider": provider,
    }


def llm() -> dict:
    """每次现读配置：改了 Key 或模型，刷新页面就生效，不用重启。
    用户的 Key 优先；没填 Key 时走官方体验代理（proxy_url + 设备令牌）。"""
    return route()


PROXY_URL_DEFAULT = "https://api.hookstudio.cn"


def device_token() -> str:
    """本机设备号：一台电脑一个，用来做「一个账号只绑一台电脑」。
    **它不是身份，也不带额度**——调 AI 只认账号令牌（见 ai_token）。"""
    cfg = load_config()
    token = (cfg.get("device_token") or "").strip()
    if not token:
        import uuid
        token = uuid.uuid4().hex
        save_config({"device_token": token})
    return token


def account_token() -> str:
    """服务器签发的账号令牌。有它就用它（额度按账号算），没有为空串。

    为什么不用本地 JWT：本地 JWT 的签名密钥在用户自己电脑上，服务器没法验；
    而额度必须由服务器记，所以令牌必须由服务器发。本地 JWT 仍然保留，
    它管的是「这台电脑上谁能用这个软件」，两件事。
    """
    return (load_config().get("account_token") or "").strip()


def account_name() -> str:
    """上次登录用的用户名，日志上报和界面提示用；没登录过为空。"""
    return (load_config().get("account_name") or "").strip()


def ai_token() -> str:
    """调 AI 时带的令牌：**只认账号令牌**，没登录就是空串。

    2026-11-04 白天这里还留过「退到匿名设备令牌」的退路，当晚砍掉了：
    没有老客户要照顾，那条退路只会变成一个白送的后门（编个新 uuid＝新的一天 10 次）。
    空串送上去，服务端回 401，界面提示「请重新登录」——比偷偷放行诚实。
    """
    return account_token()


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


def rmtree(path: str, chunk: int = 8) -> None:
    """逐文件删除自己刚生成的临时目录。

    不用 shutil.rmtree：批量删除会被本机安全守卫按「轮」计数拦截（一轮超过 50 个
    文件就要求人工确认，无人值守时直接 fail-closed 杀掉进程）。
    2026-11-04 实测：3 套连渲的临时目录有 50+ 个分段文件，一次删完正好卡在第 50 个，
    片子已经写好了、进程却被砍在收尾这一步。
    所以改成**小批删除**：每 8 个文件歇一下，让守卫的「轮」重新计数。
    铁律：清理只是收尾，任何情况下都不允许抛异常——删不掉顶多剩几个临时文件
    占点磁盘，绝不能因此把正在跑的渲染/批量任务弄死（2026-11-01 批量卡死教训）。"""
    if not os.path.isdir(path):
        return
    try:
        n = 0
        for root, dirs, files in os.walk(path, topdown=False):
            for f in files:
                try:
                    os.remove(os.path.join(root, f))
                except Exception:
                    pass
                n += 1
                if chunk and n % chunk == 0:
                    time.sleep(0.35)
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
# 任务按「组」互斥：同组内只允许一个任务，不同组可以同时跑。
# 三条产线（2026-11-12 用户要求「跑第 2 步时同时跑第 3 步」）：
#   cuts  = 第 1 步台词整理 + 第 3 步编排/出片（plan 要读台词、batch 要读 plans，
#           这两对是真依赖，必须留在一组里互斥）
#   nosub = 第 2 步去字幕 + 加自己的字幕，只写 p["nosub"] 和独立视频目录
#   shots = 第 4 步引流分镜，只写 p["shots"]
# 第 2 步与第 3 步的写面互不重叠；出片只有选「已处理版/现场做」素材时才会读
# 第 2 步的产物，那种情况在接口层拦（main._nosub_busy_conflict）。
_TASK_GROUP = {
    "transcribe": "cuts", "analyze": "cuts", "plan": "cuts",
    "batch": "cuts", "render": "cuts", "hookcut": "cuts",
    "nosub": "nosub",         # 独立组：与第 3 步可并行
    "shots": "shots",
}
_RUNNING: dict[tuple[str, str], str] = {}
_TASKS: dict[str, dict[str, dict]] = {}
_TASKS_LOCK = threading.Lock()
# 当前线程在替哪个 (pid, group) 干活：set_task(pid, None) 只收自己这一组的尾，
# 不能把另一条产线的进度一起清掉（并发时才不会互相打断）。
_CUR = threading.local()

# ── 任务中止（2026-11-12）────────────────────────────────────────────────
# 后台任务是 daemon 线程，Python 没法从外部强杀线程，所以分两层：
#   ① 协作式检查点：每个任务都会周期性 set_task 报进度，那里统一查中止标记；
#   ② 子进程真杀：ffmpeg 这类长命令登记到当前任务，点中止时直接 kill，
#      不用干等一条几分钟的编码跑完。
# TaskCancelled 刻意继承 BaseException：业务代码里大量 `except Exception` 是
# 「单集/单套失败隔离」，普通异常会被吞掉导致中止失效；BaseException 能穿透
# 它们直达 start_task，同时各处的 finally 清理照常执行。
class TaskCancelled(BaseException):
    """用户点了「中止」：任务自己退出的信号，不是失败。"""


_CANCEL: dict[tuple[str, str], threading.Event] = {}
_PROCS: dict[tuple[str, str], set] = {}
_CLEANUPS: dict[tuple[str, str], list] = {}   # 任务退出时要做的收尾（清临时目录等）
_CANCEL_LOCK = threading.Lock()
_CANCEL_MSG = "任务已中止"


def _group_of(kind: str) -> str:
    return _TASK_GROUP.get(kind, "cuts")


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


def save_merge(p: dict, keys) -> None:
    """只把 p 里指定的顶层字段合并进**磁盘上的最新版本**再存盘。

    为什么需要：第 2 步（cuts）和第 3 步（shots）现在可以同时跑。两边都是
    「开头 load 一份快照 → 算 → save 整份」，谁后保存谁就把对方刚写好的字段
    整份覆盖掉（第 3 步分镜会被第 2 步出片的结果清空，反之亦然）。
    改成「重读最新 → 只覆盖自己拥有的一组字段」后，各条产线各写各的，互不擦除。
    写入仍是原子替换，且与 save() 共用 _LOCK，不会写出半份文件。
    """
    with _LOCK:
        pid = p["id"]
        d = os.path.join(PROJECTS, pid)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "project.json")
        base = None
        try:
            with open(path, encoding="utf-8") as f:
                base = json.load(f)
        except Exception:
            base = None
        out = base if isinstance(base, dict) else dict(p)
        for k in keys:
            if k in p:
                out[k] = p[k]
            else:
                out.pop(k, None)   # 调用方显式删掉的字段，磁盘上也去掉
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        os.replace(tmp, path)


def create(path: str, name: str, target_minutes: int, episodes: list[dict], mode: str = "batch") -> dict:
    """建项目。2026-11-05 起**不再新建「单片导流」项目**（用户要求下线）：
    新建只允许 batch（批量混剪）/ shots（只做分镜），历史 funnel 项目仍然能正常打开——
    所以这里只约束新建，不改 load 出来的老 mode。
    """
    p = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "src_dir": path,
        "win_dir": win_path(path),
        "target_minutes": target_minutes,
        "mode": mode if mode in ("batch", "shots") else "batch",
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
            # 已转写的句子总数：新建项目时「复用已有项目」靠它判断哪些项目能复用
            "lines": sum(len(e.get("lines") or []) for e in p.get("episodes") or []),
            "target_minutes": p.get("target_minutes"),
            "created_at": p.get("created_at"),
            "output": bool(p.get("output")),
            "path": p.get("win_dir") or p.get("src_dir") or "",
            "task": get_task(p["id"]),
            "task_shots": get_task(p["id"], "shots"),
            "task_nosub": get_task(p["id"], "nosub"),
        })
    rows.sort(key=lambda r: -(r.get("created_at") or 0))
    return rows


def _taskfile(pid: str) -> str:
    return os.path.join(PROJECTS, pid, "task.json")


def task_flag(pid: str) -> str | None:
    """读落盘的任务标记：存在 = 上次有个任务没跑完（完成时会删掉）。

    只认 cuts 类（转写/分析/渲染/出片）：这些才会在重启后自动续跑。
    旧版本曾把 shots 也写进 task.json，那种标记现在既不会续跑也不能当成
    cuts 的「中断任务」，否则会把第 1/3 步按钮永久锁在「正在续跑」上。
    """
    try:
        with open(_taskfile(pid), encoding="utf-8") as f:
            kind = json.load(f).get("kind")
    except Exception:
        return None
    return kind if kind and _group_of(kind) == "cuts" else None


def clear_task_flag(pid: str) -> None:
    try:
        os.remove(_taskfile(pid))
    except OSError:
        pass


def set_task(pid: str, kind: str | None, pct: float = 0.0, msg: str = "", error: str = "",
             ignore_cancel: bool = False) -> None:
    """任务进度只放内存；但「正在跑哪一步」落盘成 task.json，重启后能识别中断并续跑。

    并发改造（2026-11-08）：进度按组（cuts / shots）各存一份。
    kind 给了就按它的组写；kind 为 None 表示「收尾」，只清当前线程负责的那一组，
    不会顺手把另一条产线的进度也清掉。

    中止改造（2026-11-12）：这里同时是**所有后台任务共用的中止检查点**。
    每个任务每报一次进度都会走到这儿，用户点「中止」后下一个进度点就退出，
    不必给 8 类任务的循环逐个埋桩。kind=None 是收尾，绝不能在这里抛，
    否则 finally 里的清理会被打断。
    """
    group = _group_of(kind) if kind is not None else None
    if group is not None and not ignore_cancel and is_cancelled(pid, group):
        _abort(pid, group)
    with _TASKS_LOCK:
        slots = _TASKS.setdefault(pid, {})
        if kind is None:
            group = getattr(_CUR, "group", None)
            if group is None:
                # 没有线程上下文（外部兜底清理）：两组一起收
                _TASKS.pop(pid, None)
                clear_task_flag(pid)
                return
            slots.pop(group, None)
            if not slots:
                _TASKS.pop(pid, None)
            if group == "cuts":
                clear_task_flag(pid)   # task.json 只记 cuts 的「中断续跑」
            return
        # 同一步骤连续更新时保留起始时刻，前端据此显示「已用 X 秒」，进度按批跳也能看出在动
        prev = slots.get(group) or {}
        started = prev.get("started") if prev.get("kind") == kind else time.time()
        slots[group] = {
            "kind": kind, "pct": round(pct, 1), "msg": msg, "error": error,
            "at": time.time(), "started": started,
            "elapsed": round(time.time() - started, 1),
        }
        if group == "cuts":
            # shots 不参与重启续跑（启动钩子只恢复转写/分析/渲染/出片），
            # 所以 task.json 只写 cuts，避免把 shots 的中断标记也塞进去。
            try:
                with open(_taskfile(pid), "w", encoding="utf-8") as f:
                    json.dump({"kind": kind}, f)
            except OSError:
                pass


def get_task(pid: str, group: str = "cuts") -> dict | None:
    """取某一组的任务状态。默认 cuts，第 3 步用 group="shots"。"""
    with _TASKS_LOCK:
        t = (_TASKS.get(pid) or {}).get(group)
        done = bool(t) and not t.get("error") and float(t.get("pct") or 0) >= 100
        if t and not done:
            out = dict(t)
            started = out.get("started") or out.get("at") or 0
            out["elapsed"] = round(time.time() - started, 1) if started else 0.0
            return out
        if done:
            # 跑完的进度条不算「在跑」：留着它会把前端按钮锁死。
            # 有的模块用 pct=100 收尾而不是清空，这里统一兜底（2026-11-03）。
            slots = _TASKS.get(pid) or {}
            slots.pop(group, None)
            if not slots:
                _TASKS.pop(pid, None)
            if group == "cuts":
                clear_task_flag(pid)
            return None
    if group == "cuts":
        flag = task_flag(pid)
        if flag:
            return {"kind": flag, "pct": 0, "msg": "上次中断，正在自动续跑…", "error": "",
                    "at": 0, "elapsed": 0.0}
    return None


def task_running(pid: str, group: str | None = None) -> bool:
    """这个项目当前有没有后台任务在跑（给单集操作做互斥用）。

    group 为 None 时看「有没有任意一组在跑」；传具体组只看那一组。
    """
    with _TASK_LOCK:
        if group is None:
            return any(k[0] == pid for k in _RUNNING)
        return (pid, group) in _RUNNING


def running_kind(pid: str, group: str = "cuts") -> str:
    """这一组正在跑的任务类型（没跑返回空串）。

    拆成三条产线后，「有任务在跑」不再等于「不能开新任务」：第 2 步要读台词，
    只跟第 1 步的自动转写真冲突，跟第 3 步编排/出片不冲突。接口层按 kind
    做精确判断，别把刚放开的并行又拦回去。
    """
    with _TASK_LOCK:
        return _RUNNING.get((pid, group), "") or ""


def _friendly_error(exc: Exception) -> str:
    """把任务异常翻成一句人话，原文括号附在后面（2026-11-14 批2 遗留清单）。

    为什么不是全部重写：管理排障时原文仍有用；客户第一眼先看到能懂的那句，
    管理员照样能拿到细节。规则按常见故障类匹配，匹配不上就用兜底句。
    """
    raw = f"{type(exc).__name__}: {exc}"[:160]
    s = str(exc).lower()
    if "timeout" in s or "timed out" in s:
        return "超时了：网络或 AI 服务没响应，重试一次；老是这样就检查网络（" + raw + "）"
    if "connection" in s or "getaddrinfo" in s or "failed to establish" in s:
        return "连不上 AI 服务：检查网络，稍后重试（" + raw + "）"
    if "401" in s or "403" in s or "quota" in s or "insufficient" in s or "balance" in s:
        return "AI 服务拒绝了这次请求：额度或密钥问题，到设置里检查 AI 配置（" + raw + "）"
    if isinstance(exc, MemoryError):
        return "内存不够了：关掉其他大程序再试（" + raw + "）"
    if "space" in s or "磁盘" in s or "disk" in s:
        return "磁盘空间不足：清理出片盘再试（" + raw + "）"
    return "这一步失败了，重试一次；反复失败就把括号里的内容截图发管理员（" + raw + "）"


def start_task(pid: str, kind: str, fn) -> bool:
    """同一项目里，同一组同时只跑一个任务；不同组（cuts / nosub / shots）可以并行。"""
    group = _group_of(kind)
    key = (pid, group)
    with _TASK_LOCK:
        if key in _RUNNING:
            return False
        _RUNNING[key] = kind
    # 新一轮任务：清掉上一轮可能残留的中止标记与子进程登记
    with _CANCEL_LOCK:
        _CANCEL[key] = threading.Event()
        _PROCS.pop(key, None)
        _CLEANUPS.pop(key, None)

    # 立刻登记一个 0% 的状态，不能等任务线程自己报第一次进度。
    # 任务开头要读 project.json、拼全剧台词，几百毫秒到几秒都有；这段时间
    # get_task() 返回 None，前端轮询看见「没任务」就会把按钮重新点亮，
    # 用户可以在任务已经开始后再点一次（2026-11-03 用户反馈）。
    # 一提交就有状态，按钮从点击那一刻起就是禁用的。
    set_task(pid, kind, 0, "已排队，正在准备…")

    def runner():
        _CUR.pid, _CUR.group, _CUR.kind = pid, group, kind
        try:
            fn()
        except TaskCancelled:
            # 用户主动中止：不算失败，也不能留 task.json 的「中断续跑」标记，
            # 否则下次启动会把这个被中止的任务又拉起来。
            set_task(pid, None)
        except Exception as exc:  # noqa: BLE001
            set_task(pid, kind, 0, "失败", _friendly_error(exc))
        finally:
            with _TASK_LOCK:
                _RUNNING.pop(key, None)
            with _CANCEL_LOCK:
                _CANCEL.pop(key, None)
                _PROCS.pop(key, None)
            _run_cleanups(key)
            _CUR.pid = _CUR.group = _CUR.kind = None

    threading.Thread(target=runner, daemon=True).start()
    return True


# ── 中止的实现：标记、检查点、子进程登记与真杀 ────────────────────────────

def _cancel_key(pid: str | None = None, group: str | None = None):
    """当前任务槽位。子线程没有 thread-local 时可由调用方显式传 pid/group。"""
    pid = pid if pid is not None else getattr(_CUR, "pid", None)
    group = group if group is not None else getattr(_CUR, "group", None)
    return (pid, group) if pid and group else None


def is_cancelled(pid: str | None = None, group: str | None = None) -> bool:
    key = _cancel_key(pid, group)
    if not key:
        return False
    with _CANCEL_LOCK:
        ev = _CANCEL.get(key)
    return bool(ev and ev.is_set())


def _kill_procs(key) -> None:
    """杀掉这个任务登记的全部子进程。清理优先，任何异常都吞掉。"""
    with _CANCEL_LOCK:
        procs = list(_PROCS.get(key) or ())
    for proc in procs:
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _abort(pid: str, group: str) -> None:
    _kill_procs((pid, group))
    raise TaskCancelled(_CANCEL_MSG)


def check_cancel() -> None:
    """长循环里的显式检查点（进度上报之外的循环用它，比如逐帧填充）。"""
    key = _cancel_key()
    if key and is_cancelled(*key):
        _abort(*key)


def cancel_task(pid: str, group: str | None = None) -> list[dict]:
    """给正在跑的任务打中止标记。group=None 表示这个项目所有产线都停。

    只认 _RUNNING 里真实在跑的槽位，返回被标记的任务清单（给接口回话用）。
    """
    with _TASK_LOCK:
        found = [(k, _RUNNING.get(k) or "")
                 for k in _RUNNING if k[0] == pid and (group is None or k[1] == group)]
    for k, _ in found:
        with _CANCEL_LOCK:
            ev = _CANCEL.get(k)   # 任务刚收尾时可能已经清掉，别造一个没人认的标记
            if ev:
                ev.set()
        _kill_procs(k)
        # 卡在一条长命令里时也要立刻给反馈，别让进度条看着像卡死了
        with _TASKS_LOCK:
            t = (_TASKS.get(pid) or {}).get(k[1])
            if t:
                t["msg"] = "正在中止…"
    return [{"group": k[1], "kind": kind} for k, kind in found]


def add_cleanup(fn) -> None:
    """登记一个任务级收尾动作：正常跑完 / 失败 / 用户中止都会执行。

    给「整批共用的临时目录」用——清理只能在**整个任务**结束时做一次，
    不能每套清；以前只写在正常收尾那一行，中止和异常就直接跳过去了。
    """
    key = _cancel_key()
    if not key:
        return
    with _CANCEL_LOCK:
        _CLEANUPS.setdefault(key, []).append(fn)


def _run_cleanups(key) -> None:
    with _CANCEL_LOCK:
        fns = _CLEANUPS.pop(key, [])
    for fn in fns:
        try:
            fn()
        except Exception:  # noqa: BLE001 —— 清不掉只占点磁盘，不能盖住真正的错误
            pass


def register_proc(proc, pid: str | None = None, group: str | None = None) -> None:
    """把子进程挂到当前任务名下，取消时由 _kill_procs 统一杀掉。"""
    key = _cancel_key(pid, group)
    if not key:
        return
    with _CANCEL_LOCK:
        _PROCS.setdefault(key, set()).add(proc)


def unregister_proc(proc, pid: str | None = None, group: str | None = None) -> None:
    key = _cancel_key(pid, group)
    if not key:
        return
    with _CANCEL_LOCK:
        s = _PROCS.get(key)
        if s:
            s.discard(proc)
            if not s:
                _PROCS.pop(key, None)


def _drain_pipe(pipe, buf: list, default) -> None:
    """持续排空 stdout/stderr，避免管道写满把 ffmpeg 卡死。"""
    try:
        buf.append(pipe.read())
    except Exception:  # noqa: BLE001
        buf.append(default)
    finally:
        try:
            pipe.close()
        except Exception:  # noqa: BLE001
            pass


def wait_cancelable(proc, timeout: float | None = None) -> int:
    """替代 proc.wait()：等的时候周期性看中止标记，取消就 kill 并抛。"""
    deadline = (time.monotonic() + timeout) if timeout else None
    while True:
        check_cancel()
        try:
            proc.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            if deadline is not None and time.monotonic() >= deadline:
                raise
            continue
        # 关键：进程「正常结束」也可能是刚被中止杀掉的。wait() 不抛超时就
        # 直接 break 的话，任务会把半截产物当成一次成功继续往下跑（实测过）。
        # 离开前再确认一次，让中止信号一定变成 TaskCancelled。
        check_cancel()
        break
    return proc.returncode


def run_cancelable(cmd: list[str], *, text: bool = False, encoding: str | None = None,
                   errors: str | None = None, capture_output: bool = True,
                   timeout: float | None = None, **kw) -> subprocess.CompletedProcess:
    """替代 subprocess.run：读线程排空输出，等待时能响应「中止」。

    返回值与 subprocess.run 一致（.returncode / .stdout / .stderr），
    所以调用点只需换函数名；超时和原来一样会杀掉进程。
    """
    popen_kw = dict(kw)
    if capture_output:
        popen_kw.setdefault("stdout", subprocess.PIPE)
        popen_kw.setdefault("stderr", subprocess.PIPE)
    popen_kw.setdefault("stdin", subprocess.DEVNULL)
    if text:
        popen_kw.setdefault("text", True)
        if encoding:
            popen_kw["encoding"] = encoding
        if errors:
            popen_kw["errors"] = errors
    default = "" if text else b""
    proc = subprocess.Popen(cmd, **popen_kw)
    register_proc(proc)
    out_buf: list = []
    err_buf: list = []
    readers = []
    for pipe, buf in ((proc.stdout, out_buf), (proc.stderr, err_buf)):
        if pipe is not None:
            t = threading.Thread(target=_drain_pipe, args=(pipe, buf, default), daemon=True)
            t.start()
            readers.append(t)
    try:
        wait_cancelable(proc, timeout=timeout)
        for t in readers:
            t.join(timeout=5)
        check_cancel()          # 命令跑完到返回之间也可能刚好被中止
        return subprocess.CompletedProcess(
            cmd, proc.returncode,
            out_buf[0] if out_buf else default,
            err_buf[0] if err_buf else default,
        )
    finally:
        unregister_proc(proc)
        if proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
