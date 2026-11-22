"""批量定时发布：队列、排期、状态机。

### 队列是**全局**的，不属于任何项目（2026-11-13 用户定）
发布和「剪辑那五步」没有必然联系：可以直接选一个文件夹、选几个文件，
也可能是以前别的项目出的片。所以队列存在 `data/publish_queue.json`，
作业上只记一个可选的 `pid`（从哪个项目挑来的，纯作展示和溯源）。

这带来的一个真实区别：删项目**不该**删掉发布队列；但项目输出目录被删时，
队列里指向它的文件会消失 —— 所以删项目前要提醒，而不是静默留着坏记录。

### P1 的边界
P1 只做「选视频 → 排期 → 到点提醒 → 人工确认」这一条完整链路，**不碰浏览器**。
所以这里没有常驻 worker：到点后的推进是**拉取式**的（`due_jobs` 由前端轮询触发），
好处是本地程序随时关都不丢状态、没有后台线程的并发问题。

### 为什么发布不走 core._TASK_GROUP
它不是剪辑产线：不需要和 cuts / nosub / shots 互斥，也不需要 task.json 的
「中断续跑」语义（发布是幂等的，重跑只会跳过已发的）。所以这里自带状态，
不注册产线组——但**停止按钮必须能停**（P2 的 worker 在检查点读 flag）。

### 平台硬约束
视频号定时发布只能提前 7 天、最早 10 分钟，定时同样走审核。这些在排期阶段
就要拦住，不能等上传完才发现设不进去。
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta

from . import core

# ── 平台约束（上线前用真账号实测校准，代码只认这几个常量）─────────────────
LEAD_MIN = 10          # 最早：距现在至少 10 分钟
LEAD_MAX_DAYS = 7      # 最晚：最多提前 7 天
INTERVAL_MIN = 5       # 硬下限：两条定时至少差 5 分钟，再小提交时就挤到一起了
INTERVAL_TIGHT = 30    # 软提醒：低于它只给黄字（同质化风险），不拦
INTERVAL_DEFAULT = 120  # 默认间隔：同号连发太密容易被判同质化
BATCH_MAX = 15         # 软护栏：单批最多 15 条
UPLOAD_MBPS = 12       # 上行估算，只用于「预计上传耗时」提示
UPLOAD_ETA_SEC = 90    # 单条「上传 + 填表 + 设定时」的估算下限（实测约 1-3 分钟）

ACCOUNTS_FILE = os.path.join(core.DATA_DIR, "publish_accounts.json")
QUEUE_FILE = os.path.join(core.DATA_DIR, "publish_queue.json")
FMT = "%Y-%m-%dT%H:%M"

# 能拿去发的视频后缀。视频号实际只吃 mp4/mov，但这里放宽到常见容器，
# 由上传阶段给出「这个格式传不上去，先转一下」的人话，而不是选择阶段就拦死。
VIDEO_EXT = (".mp4", ".mov", ".m4v", ".flv", ".mkv", ".avi", ".wmv", ".ts")
MAX_MB = 2048          # 视频号单条上限约 2GB / 8 小时

# 重试时如果计划时间已经过去（或不到 3 分钟就到期），顺延到「现在 + 这么多分钟」。
# 原因：平台的定时只能定在未来，拿过期时间重发必然死在「设定时」那一步；
# 20 分钟给上传留余量（160MB 的片子实测要几分钟）。
RETRY_LEAD_MIN = 20

_LOCK = threading.Lock()

# 队列互斥（2026-11-22）：所有 mutator 都是「queue() 读整份 → 改字段 →
# save_queue 整份写回」的模式，_LOCK 只罩住写盘那一瞬，罩不住读改写全程。
# worker 线程（set_status / mark_submit_started）和用户在界面上改另一条
# （update_job / apply_fields / mark / retry…）并发时，两个线程各自读→改→写，
# 后落盘的把先落盘的整个补丁抹掉——轻则丢用户编辑、丢 attempts 计数，
# 重则 mark_submit_started 刚写的 submit_started_at 被抹掉，重启后
# reset_stuck_publishing 看到没有时间戳的 publishing 放回 queued，
# 同一批视频重复上传、重复发表（这正是 2026-11-15 那次修复想堵死的路径，
# 2026-11-22 复查发现锁的粒度不够）。所以读改写全程必须持同一把互斥锁；
# 用 RLock 是因为 mutator 之间有嵌套调用（set_status 里还会调 mark）。
_QMUTEX = threading.RLock()


def _qlocked(fn):
    """队列写操作的「读改写全程」互斥（为什么要锁见 _QMUTEX 的注释）。"""
    def wrapper(*args, **kwargs):
        with _QMUTEX:
            return fn(*args, **kwargs)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper

# 状态：draft 未排期 / queued 已排期待发 / scheduled 平台已托管 / published 已发
# manual 需人工补发 / failed 失败 / skipped 跳过
# uncertain = 点过「发表」但没核对上（或断电断在那一刻），平台收没收到不知道。
# 跟 failed 分开：failed 能「重试」，uncertain 绝不能自动重试，先让人去后台核实。
OPEN_STATUS = ("draft", "queued", "failed", "manual", "publishing", "uncertain")

# ── 每条视频要填的发布字段（对齐视频号发布页）───────────────────────────
# 键 → (中文名, 长度上限, 提示)。上限基本都是平台的硬限制：
# 短标题 6-16 字、话题最多 8 个、合集名 30 字……超了不是本地报错，是平台拒。
FIELD_SPEC = {
    "title":       ("标题", 100, "留空就用文件名"),
    "short_title": ("短标题", 16, "6-16 个字；留空让平台自己截"),
    "topics":      ("话题", 8, "最多 8 个，不用打 #"),
    "cover_ts":    ("封面取帧", 0, "取第几秒的画面当封面"),
    "cover_file":  ("封面图片", 0, "整批共用一张；个别视频可单独换"),
    "collection":  ("合集", 30, "只能从号里已有的合集选"),
    "location":    ("位置", 40, "从搜索候选里选，不手打"),
    "activity":    ("活动", 30, "从号里的活动列表选"),
    "link":        ("扩展链接", 500, "从链接类型里选（剧集/文章/…）"),
    "original":    ("声明原创", 0, "号没有原创权限时勾了会被驳回"),
    "declare":     ("视频标注", 0, "自制 / 含 AI 生成"),
}
FIELD_KEYS = tuple(FIELD_SPEC)
# 视频标注的**兜底**清单。正常从 /api/publish/options?kind=declare 现拉真实的
# （按号权限不同，条目会少几个）。这里只用于「还没拉过」时把下拉框填满，
# 值必须是平台真编号，不能自己编（写死 self/ai 会在填页面时对不上任何 tag）。
DECLARES = (("", "无需标注"), ("3", "内容为虚构剧情，仅供娱乐"),
            ("1", "含AI生成内容"), ("2", "内容包含营销广告"),
            ("5", "内容为自行拍摄"), ("7", "内容为转载"),
            ("8", "个人观点，仅供参考"))
# 整批一致的那几项（能一键套到全批）；标题 / 封面取帧秒数逐条不同。
# 短标题（2026-11-21 用户要求「要能统一设置」）默认整批同一句，但单条可以
# 单独改过 —— 套用时只覆盖「空着的」和「还跟着上次整批值」的，见 apply_fields。
# 封面图片（cover_file）是「整批默认 + 单条覆盖」，只写 template、不灌进每条，
# 所以不在 BATCH_KEYS 里，由 apply_fields 单独处理（见下方注释）。
BATCH_KEYS = ("collection", "location", "activity", "link", "original", "declare",
              "topics", "short_title")
# 已经是「平台托管」或「已发」的，任何字段都不许再改（改了也发不出去，只会让人误会）
LOCKED_STATUS = ("scheduled", "published", "publishing", "uncertain")

# 「这批还没发完」的状态。只要还剩一条，账号就锁死：半批发到 A、半批发到 B
# 是最难发现的事故，且发出去收不回来（2026-11-13 用户选 C 档）。
# published / skipped 是历史，不锁；draft 还没提交，也不锁。
ACTIVE_STATUS = ("queued", "publishing", "scheduled", "manual", "failed")

# 只在「某个号」里有效的值。换号时连同 template 和未发作业一起清掉：
# 剧集 id / 位置 poi / 合集 id 都是号内数据，带着走会在发表页选错或选不到。
# declare 是平台通用枚举（tagType 数字）、topics/original/cover 与号无关，不清。
ACC_BOUND_KEYS = ("link", "link_kind", "link_id", "link_title",
                  "location", "location_poi", "location_city",
                  "collection", "collection_id",
                  "activity", "activity_id")

# 链接类型（真实发表页下拉里的四项，2026-11-13 实测）
LINK_KINDS = (("series", "视频号剧集"), ("article", "公众号文章"),
              ("redpack", "红包封面"), ("miniapp", "小程序短剧"))


# ── 封面图片（2026-11-13）────────────────────────────────────────────────
# 用户口径：封面可以指定一张图片，且所有视频默认共用同一张；个别视频允许单独换。
# 所以存两份：template.cover_file = 整批默认，job.cover_file = 单条覆盖（空＝继承）。
# 图片一律复制进 data/publish_cover/，只存相对名：原图被移动/删掉也不会失效，
# 换机器拷走 data 目录也还在。绝不存用户给的绝对路径。
COVER_DIR = "publish_cover"
COVER_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
COVER_MAX_MB = 10
COVER_URL = "/api/publish/cover/preview?name="
_COVER_RE = re.compile(r"^publish_cover/[A-Za-z0-9_.-]+$")


def cover_abs(rel: str) -> str:
    """相对名 → 应用数据目录里的真实路径；不合规一律返回空（防目录穿越）。"""
    v = str(rel or "").strip().replace("\\", "/")
    if not _COVER_RE.match(v):
        return ""
    return os.path.join(core.DATA_DIR, COVER_DIR, os.path.basename(v))


def cover_url(rel: str) -> str:
    """给 <img> 用的预览地址。文件不在了就返回空，别让界面挂个破图。"""
    p = cover_abs(rel)
    return COVER_URL + os.path.basename(p) if p and os.path.isfile(p) else ""


def cover_effective(job: dict, tpl: dict) -> str:
    """这条视频最终用哪张封面：单条覆盖优先，否则继承整批默认。"""
    return str(job.get("cover_file") or "") or str((tpl or {}).get("cover_file") or "")


def save_cover(src: str) -> dict:
    """把用户选的本地图片复制进应用数据目录，返回相对名 / 预览地址。"""
    import shutil

    path = str(src or "")
    if not os.path.isfile(path):
        raise PublishError("找不到这张图片，重新选一次")
    ext = os.path.splitext(path)[1].lower()
    if ext not in COVER_EXTS:
        raise PublishError("封面只认 png / jpg / jpeg / webp / bmp 图片")
    size = os.path.getsize(path)
    if size > COVER_MAX_MB * 1024 * 1024:
        raise PublishError(f"封面图超过 {COVER_MAX_MB}MB 了，换一张小一点的")
    with open(path, "rb") as f:
        digest = hashlib.sha1(f.read()).hexdigest()[:12]
    name = f"cover_{digest}{ext}"
    folder = os.path.join(core.DATA_DIR, COVER_DIR)
    os.makedirs(folder, exist_ok=True)
    dst = os.path.join(folder, name)
    if not os.path.isfile(dst):
        shutil.copy2(path, dst)
    rel = f"{COVER_DIR}/{name}"
    return {"cover_file": rel, "name": os.path.basename(path),
            "url": cover_url(rel), "size": size}


class PublishError(Exception):
    """给前端看的人话错误（main 转 400）。"""


# ── 时间 ────────────────────────────────────────────────────────────────
def now() -> datetime:
    return datetime.now().replace(second=0, microsecond=0)


def fmt(dt: datetime) -> str:
    return dt.strftime(FMT)


def parse(s: str | None) -> datetime | None:
    """接受 'YYYY-MM-DDTHH:MM' 与 'YYYY-MM-DD HH:MM'，坏值返回 None。"""
    s = (s or "").strip().replace(" ", "T")
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).replace(second=0, microsecond=0)
    except ValueError:
        return None


# ── 账号（用户级全局，不属于项目）────────────────────────────────────────
def load_accounts() -> list[dict]:
    try:
        with open(ACCOUNTS_FILE, encoding="utf-8") as f:
            d = json.load(f) or {}
    except Exception:  # noqa: BLE001
        return []
    rows = d.get("accounts") if isinstance(d, dict) else None
    return [r for r in (rows or []) if isinstance(r, dict) and r.get("id")]


def save_accounts(rows: list[dict]) -> None:
    with _LOCK:
        os.makedirs(core.DATA_DIR, exist_ok=True)
        tmp = ACCOUNTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"v": 1, "accounts": rows}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, ACCOUNTS_FILE)


def public_accounts() -> list[dict]:
    """给前端的版本：只留展示与状态，不带 profile 路径（那是本机内部信息）。"""
    out = []
    for r in load_accounts():
        out.append({
            "id": r["id"],
            # 名字优先用用户自己起的，其次微信昵称，都没有就是「待扫码账号」
            "name": r.get("name") or r.get("wx_name") or "待登录账号",
            "wx_name": r.get("wx_name") or "",
            # 视频号 ID（sph 开头那串）。界面上要显示，不然多个号的备注名一混就分不清
            # 哪个是哪号。老账号还没读到就是空，登录/采一次之后自动补。
            "finder_id": r.get("finder_id") or "",
            "fans": int(r.get("fans") or 0),
            "avatar": r.get("avatar") or "",
            "state": r.get("state") or "unknown",
            "last_check": r.get("last_check") or "",
            "last_ok": r.get("last_ok") or "",
            "note": r.get("note") or "",
        })
    return out


MAX_ACCOUNTS = 10  # 一台电脑最多 10 个号（用户 2026-11-13 定），和 wxbot.MAX_ACCOUNTS 同一个数


def add_account(name: str = "", pending: bool = False) -> dict:
    """建账号。

    `pending=True` 是登录流程用的：先建一个空壳（state=pending），窗口里登完再回填
    昵称头像。名字允许空——小V猫那样「点加号直接弹登录窗口」，不该先逼用户起名。
    """
    name = (name or "").strip()
    rows = load_accounts()
    if len(rows) >= MAX_ACCOUNTS:
        raise PublishError(f"最多 {MAX_ACCOUNTS} 个号。要加新的，先删掉一个不用的。")
    if name:
        if len(name) > 30:
            raise PublishError("账号名太长了，30 个字以内")
        if any((r.get("name") or "").strip() == name for r in rows):
            raise PublishError("已经有同名账号了，换个名字区分一下")
    elif not pending:
        raise PublishError("给这个账号起个名字，比如「大彬的绳生」")
    # 纯 12 位十六进制、不带 acc_ 前缀：audit.route_of 才会把它归一成 {id}，
    # 否则每个账号都是一条独立路由，后台统计全碎（job id 同一个道理）。
    aid = hashlib.sha1(f"{name}{datetime.now()}{len(rows)}".encode()).hexdigest()[:12]
    rows.append({"id": aid, "name": name, "wx_name": "", "avatar": "",
                 "profile_dir": "", "state": "pending" if pending else "unknown",
                 "last_check": "", "last_ok": "", "note": "",
                 "created_at": fmt(datetime.now())})
    save_accounts(rows)
    return {"id": aid, "name": name or "待登录账号", "state": "pending" if pending else "unknown"}


def update_account(aid: str, **patch) -> dict:
    """改账号字段（昵称/头像/状态/名字/视频号 ID）。只允许白名单字段。"""
    allow = {"name", "wx_name", "avatar", "state", "last_check", "last_ok", "note",
             "finder_id", "fans"}
    rows = load_accounts()
    for r in rows:
        if r.get("id") != aid:
            continue
        for k, v in patch.items():
            if k in allow:
                r[k] = v
        save_accounts(rows)
        return dict(r)
    raise PublishError("这个账号已经不在了，刷新一下列表")


def get_account(aid: str) -> dict | None:
    for r in load_accounts():
        if r.get("id") == aid:
            return r
    return None


def mark_online(aid: str, wx_name: str = "", avatar: str = "") -> dict:
    """把号坐实成 online。**这是「登录成功」写库的唯一入口**（2026-11-16 重构）。

    所有判定登录成功的地方（对账、轮询、开窗）都只调用这一个函数，避免三处
    各自拼 patch、各自记时间戳，节流和写库逻辑互相打架。
    返回更新后的账号 dict。
    """
    now_s = fmt(now())
    patch = {"state": "online", "last_check": now_s, "last_ok": now_s}
    if wx_name:
        patch["wx_name"] = str(wx_name)[:30]
    if avatar:
        patch["avatar"] = str(avatar)
    return update_account(aid, **patch)


def mark_expired(aid: str) -> dict:
    """把号坐实成 expired（登录态掉了）。同样收敛成单一写库入口。"""
    return update_account(aid, state="expired", last_check=fmt(now()))


@_qlocked
def remove_account(aid: str) -> None:
    save_accounts([r for r in load_accounts() if r.get("id") != aid])
    # 删掉的号不能继续当「当前发到」——否则刷新后界面还指着它，
    # 点排期会拿一个不存在的 id 去 start()。
    q = queue()
    if q.get("current_account_id") == aid:
        q["current_account_id"] = ""
        save_queue(q)


def account_name(aid: str) -> str:
    for r in load_accounts():
        if r.get("id") == aid:
            # 只扫码登录没起备注名的号，name 是空，但 wx_name（平台昵称）在。
            # 校验时回退到 wx_name，和 public_accounts() 前端展示口径一致，
            # 否则这种号会被误判成「账号不存在」，挡在发布门外（2026-11-16）。
            return (r.get("name") or r.get("wx_name") or "").strip()
    return ""


# ── 全局队列 ────────────────────────────────────────────────────────────
def _default_tpl() -> dict:
    return {
        # 短标题（≤16 字）：整批一般用同一句，所以放模板；单条改过的保留（见 apply_fields）
        "short_title": "",
        "topics": [], "cover_file": "", "collection": "", "collection_id": "",
        # 原创默认**不勾**：号没有原创权限时勾了会被驳回，甚至影响账号权重。
        # 默认帮用户勾上等于替他做一个有风险的承诺（2026-11-13 定）。
        "original": False,
        "visibility": "public", "desc_tail": "",
        # 位置默认**不设**：真实页面会沿用上次选的城市，漏看就把整批标到别的城市。
        "location": "", "location_poi": "", "location_city": "",
        "activity": "", "activity_id": "",
        "link": "", "link_kind": "", "link_id": "", "link_title": "",
        "declare": "",
    }


def _norm_tpl(tpl: dict) -> dict:
    out = _default_tpl()
    for k, d in out.items():
        v = tpl.get(k, d) if isinstance(tpl, dict) else d
        if isinstance(d, bool):
            out[k] = bool(v)
        elif isinstance(d, list):
            out[k] = [str(t) for t in (v or [])][:8]
        else:
            out[k] = v if isinstance(v, str) else ("" if v is None else str(v))
    return out


def queue() -> dict:
    """读（并规范化）全局队列。只读语义，不落盘。"""
    try:
        with open(QUEUE_FILE, encoding="utf-8") as f:
            raw = json.load(f) or {}
    except Exception:  # noqa: BLE001
        raw = {}
    jobs = raw.get("jobs") if isinstance(raw.get("jobs"), list) else []
    return {
        "v": 3,
        "template": _norm_tpl(raw.get("template") or {}),
        # 历史排期记录。**不是**当前选中的号（2026-11-13 踩过：前端拿它当
        # 选中态，队列空了还把上次那个号锁着，点任何号都切不动）。
        "batch": raw.get("batch") if isinstance(raw.get("batch"), dict) else {},
        # 界面「当前发到哪个号」的唯一持久化来源，由 select_account() 写。
        "current_account_id": str(raw.get("current_account_id") or ""),
        # 提交台账：条目从队列里删掉/归档后仍要留下「可能已经交给平台」的
        # 证据，否则清空一次队列就绕过了防重复发（2026-11-15）。
        "ledger": [x for x in (raw.get("ledger") or [])
                   if isinstance(x, dict) and x.get("job_id")],
        "jobs": [j for j in jobs if isinstance(j, dict) and j.get("id")],
    }


_BACKUP_AT = 0.0


def save_queue(q: dict) -> None:
    global _BACKUP_AT
    with _LOCK:
        os.makedirs(core.DATA_DIR, exist_ok=True)
        # 覆盖前把旧文件留一份：排期被误清、文件写坏时至少能回滚——
        # 2026-11-14 队列被清空那次就只能认了。10 分钟最多一份，
        # 保留最近 20 份（几十 KB/份，磁盘可忽略）；备份失败不拦正常写。
        if os.path.isfile(QUEUE_FILE) and time.time() - _BACKUP_AT > 600:
            _BACKUP_AT = time.time()
            try:
                bdir = os.path.join(core.DATA_DIR, "queue_backup")
                os.makedirs(bdir, exist_ok=True)
                shutil.copyfile(QUEUE_FILE, os.path.join(
                    bdir, "queue_" + time.strftime("%Y%m%d_%H%M%S") + ".json"))
                olds = sorted(glob.glob(os.path.join(bdir, "queue_*.json")))
                for old in olds[:-20]:
                    try:
                        os.remove(old)
                    except OSError:
                        pass
            except OSError:
                pass
        tmp = QUEUE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(q, f, ensure_ascii=False, indent=1)
        os.replace(tmp, QUEUE_FILE)


def _ledger_add(q: dict, job: dict) -> None:
    """条目离开队列前留一条提交证据。只记能判重的最小字段，不存视频内容。"""
    if not (job.get("submit_started_at") or
            job.get("status") in ("scheduled", "published", "uncertain")):
        return
    rec = {
        "job_id": str(job.get("id") or ""),
        "account_id": str(job.get("account_id") or ""),
        "title": str(job.get("title") or "")[:80],
        "file": str(job.get("file") or "")[:300],
        "status": str(job.get("status") or ""),
        "submit_started_at": str(job.get("submit_started_at") or ""),
        "scheduled_at": str(job.get("scheduled_at") or ""),
        "left_at": fmt(now()),
        "afp": str(job.get("afp") or ""),   # 音频内容指纹：跨平台查重只提示不拦
    }
    q["ledger"] = [x for x in (q.get("ledger") or [])
                   if x.get("job_id") != rec["job_id"]]
    q["ledger"].append(rec)
    q["ledger"] = q["ledger"][-500:]


@_qlocked
def mark_submit_started(jid: str) -> dict:
    """点「发表」**之前**落盘：写不进去就别点（回调抛错会中断发布，安全）。

    有这个时间戳 = 这条可能已经交给平台。重启、失败、重试都不许当成
    「还没点过」重新来一遍。
    """
    q = queue()
    job = next((j for j in q["jobs"] if j["id"] == jid), None)
    if not job:
        raise PublishError("这条已经不在队列里了，没敢点发表")
    job["submit_started_at"] = fmt(now())
    job["status"] = "publishing"
    save_queue(q)
    return job


def account_identity(aid: str) -> dict:
    """排期时把「发到哪个号」冻结成平台昵称，发表前跟浏览器里登的号比对。

    **只认 wx_name**（平台昵称）。name 是用户自己写的备注名，跟页面上的
    昵称不是一回事，拿去比对会把整批误判成「登错号」。资料没抓全就返回
    空 dict，那这次不核对，别误伤。
    """
    for r in load_accounts():
        if r.get("id") != aid:
            continue
        name = str(r.get("wx_name") or "").strip()
        if not name:
            return {}
        return {"name": name[:30],
                "finder_id": str(r.get("finder_id") or "").strip()[:40]}
    return {}


ARCHIVE_FILE = os.path.join(core.DATA_DIR, "queue_archive.json")
# 归档的门槛：完结（已定时/已提交/已取消）超过 7 天的条目。7 天是平台定时
# 的最大提前量——超过它，平台上要么已经发了、要么早该处理完了。
DONE_ARCHIVE = ("scheduled", "published", "skipped")


@_qlocked
def archive_old_jobs(keep_days: int = 7) -> int:
    """把完结超过 7 天的条目挪出队列（2026-11-14 批2 遗留清单）。

    已交平台的行不再摆任何按钮，放着只会越积越多；归档不是删除——数据目录
    下 queue_archive.json 按次追加，随时能翻旧账。只在启动时跑一次：
    运行中归档会让「刚提交」的条目闪烁消失，观感很差。
    """
    q = queue()
    cutoff = fmt(datetime.now() - timedelta(days=keep_days))
    moved, keep = [], []
    for j in q["jobs"]:
        st = j.get("status") or ""
        end = j.get("done_at") or j.get("plan_at") or j.get("created_at") or ""
        if st in DONE_ARCHIVE and end and end < cutoff:
            moved.append(j)
        else:
            keep.append(j)
    if not moved:
        return 0
    for j in moved:
        _ledger_add(q, j)
    arch = {"archived_at": fmt(datetime.now()), "jobs": moved}
    try:
        with open(ARCHIVE_FILE, encoding="utf-8") as f:
            old = json.load(f)
        if isinstance(old, list):
            old.append(arch)
        else:
            old = [arch]
    except Exception:
        old = [arch]
    with open(ARCHIVE_FILE, "w", encoding="utf-8") as f:
        json.dump(old, f, ensure_ascii=False, indent=1)
    q["jobs"] = keep
    save_queue(q)
    return len(moved)


ACCOUNT_FIELDS_EMPTY = {k: "" for k in ACC_BOUND_KEYS}


@_qlocked
def select_account(aid: str) -> dict:
    """切号：当前号写进队列，上一个号专属的配置就地清空。

    2026-11-14 起不再锁号：提交（开始上传）之后这批就是后台任务，任务自己
    带着账号走，跟当前选的是哪个号无关——切号随时自由，不会把已提交的
    拆成两个号。号内数据（剧集 id / 位置 / 合集）仍就地清空，不带过号。
    """
    aid = str(aid or "")
    if not aid or not get_account(aid):
        raise PublishError("这个账号已经不在了，刷新一下列表")
    q = queue()
    if q.get("current_account_id") == aid:
        return {"current_account_id": aid, "cleared": False}
    q["template"].update(ACCOUNT_FIELDS_EMPTY)
    for j in q["jobs"]:
        if j.get("status") in LOCKED_STATUS:
            continue
        j.update(ACCOUNT_FIELDS_EMPTY)
    q["current_account_id"] = aid
    save_queue(q)
    return {"current_account_id": aid, "cleared": True}


@_qlocked
def migrate_legacy() -> int:
    """把旧版「存在 project.json 里的 publish 块」搬到全局队列（只做一次）。

    早期版本把队列挂在项目上；现在是全局的。老数据不能丢也不能重复搬，
    所以只在**队列文件还不存在**时扫一遍，搬完把项目里的块删掉。
    """
    if os.path.exists(QUEUE_FILE):
        return 0
    moved = 0
    q = {"v": 3, "template": _default_tpl(), "batch": {}, "jobs": []}
    try:
        names = [d for d in os.listdir(core.PROJECTS)
                 if os.path.isdir(os.path.join(core.PROJECTS, d))]
    except OSError:
        return 0
    for pid in names:
        p = core.load(pid)
        if not p or not isinstance(p.get("publish"), dict):
            continue
        b = p["publish"]
        tpl = _norm_tpl(b.get("template") or {})
        # v1 的 original=True 是代码默认值（当时界面根本改不了），不是用户的选择。
        # 搬过来一律按「不勾」—— 替客户默认勾上原创，号没权限会被驳回。
        if int(b.get("v") or 1) < 2:
            tpl["original"] = False
        if not q["jobs"]:
            q["template"] = tpl
            q["batch"] = b.get("batch") if isinstance(b.get("batch"), dict) else {}
        for j in (b.get("jobs") or []):
            if isinstance(j, dict) and j.get("id"):
                q["jobs"].append({**j, "pid": pid,
                                  "project": p.get("name") or ""})
                moved += 1
        p.pop("publish", None)
        core.save_merge(p, ["publish"])
    if moved:
        save_queue(q)
    return moved


# ── 字段规范化 ──────────────────────────────────────────────────────────
def _tpl_fields(tpl: dict) -> dict:
    """批次默认值 → 新作业的初始字段。加字段只改这里和 FIELD_SPEC。"""
    return {
        "short_title": str(tpl.get("short_title") or ""),
        "topics": list(tpl.get("topics") or []),
        "cover_ts": 0.0,
        # 封面图不复制进每条：空串＝继承整批默认，界面上才分得清
        # 「跟随全批」和「这条单独换过」。
        "cover_file": "",
        "collection": tpl.get("collection") or "",
        "collection_id": tpl.get("collection_id") or "",
        "location": tpl.get("location") or "",
        "location_poi": tpl.get("location_poi") or "",
        "location_city": tpl.get("location_city") or "",
        "activity": tpl.get("activity") or "",
        "activity_id": tpl.get("activity_id") or "",
        "link": tpl.get("link") or "",
        "link_kind": tpl.get("link_kind") or "",
        "link_id": tpl.get("link_id") or "",
        "link_title": tpl.get("link_title") or "",
        "original": bool(tpl.get("original", False)),
        "declare": tpl.get("declare") or "",
    }


def norm_field(key: str, val):
    """规范化 + 校验一栏。报错文案直接给用户看，所以必须说清「怎么改」。"""
    if key == "topics":
        if not isinstance(val, (list, tuple)):
            val = [val]
        out: list[str] = []
        for t in val:
            s = re.sub(r"\s+", "", str(t or "").strip().lstrip("#"))
            if not s:
                continue
            if len(s) > 20:
                raise PublishError(f"话题「{s[:10]}…」太长了，20 个字以内")
            if s not in out:
                out.append(s)
        if len(out) > 8:
            raise PublishError("话题最多 8 个，多了平台不认")
        return out
    if key == "cover_ts":
        try:
            v = float(val or 0)
        except (TypeError, ValueError):
            raise PublishError("封面时间要填数字（第几秒的画面）") from None
        if v < 0:
            raise PublishError("封面时间不能是负数")
        return v
    if key == "cover_file":
        v = str(val or "").strip().replace("\\", "/")
        if not v:
            return ""
        if not _COVER_RE.match(v):
            raise PublishError("封面图片要从「选择图片」里挑，不能手填路径")
        return v
    if key == "original":
        return bool(val)
    if key == "declare":
        # 真实页面「视频标注」是号里的一套 tag（tagType 数字），2026-11-13 实测：
        # 0 无需标注 / 1 含AI生成内容 / 2 内容包含营销广告 / 3 内容为虚构剧情，仅供娱乐
        # 5 内容为自行拍摄 / 7 内容为转载 / 8 个人观点，仅供参考
        # 这里只存那个数字（""=无需标注），显示名由 /api/publish/options 现拉，
        # 所以别再写死「self / ai」这种自己编的值——填的时候对不上真实 tag。
        v = str(val if val is not None else "").strip()
        if v in ("", "0", "None", "none"):
            return ""
        if not v.isdigit():
            raise PublishError("视频标注要存的是平台给的编号，不是文字。"
                               "点「从号里选」重新选一次。")
        return v
    if key == "link":
        # 链接不是手打的 URL，是「类型 + 该类型下的某一个」。
        # 真实页面「链接」是个下拉，选类型后还要从弹窗里挑具体条目（2026-11-13 实测）。
        v = str(val or "").strip()
        if v and not re.match(r"^https?://", v, re.I):
            raise PublishError("扩展链接要以 http:// 或 https:// 开头")
        return v[:500]
    if key == "link_kind":
        v = str(val or "").strip().lower()
        if v and v not in [k for k, _ in LINK_KINDS]:
            raise PublishError("不认识的链接类型")
        return v
    if key == "short_title":
        v = re.sub(r"\s+", " ", str(val or "")).strip()
        if v and not (6 <= len(v) <= 16):
            raise PublishError(f"短标题要 6-16 个字，现在是 {len(v)} 个")
        return v
    spec = FIELD_SPEC.get(key) or ("", 100, "")
    return str(val or "").strip()[: spec[1] or 100]


# ── 挑视频 ──────────────────────────────────────────────────────────────
def file_hash(path: str) -> str:
    """轻量指纹：路径 + 大小 + 修改时间。

    不读整个文件：成片动辄几百 MB，算 sha1 会让「添加」按钮卡十几秒。
    这三个值变了就认为不是同一条（移动/重渲染都会变），对「防重复发」够用。
    """
    try:
        st = os.stat(path)
        raw = f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        raw = os.path.abspath(path)
    return "h" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _ep_no(name: str) -> str:
    """取「第X集」原样（保留前导零：第03集不能变成第3集，用户是这么编的）。"""
    m = re.search(r"第\s*(\d+)\s*集", name or "")
    return f"第{m.group(1)}集" if m else ""


_PROD_TAIL = re.compile(r"[_-](?:混剪)?set\d+[_-]\d{6,}$", re.I)
_STAMP_TAIL = re.compile(r"[_-]\d{9,}$")


def _trim_product_tail(base: str) -> str:
    """去掉工具自己拼出来的产物尾巴（`_混剪set01_1108105657`）。

    这是出片时加的套序号和下单时间戳，留着当标题发出去很难看。
    只在「认得出是产物尾巴」时才裁 —— 用户自己的「第07集-开头」一个字都不动。
    """
    s = _PROD_TAIL.sub("", base)
    if s == base:
        s = _STAMP_TAIL.sub("", base)
    return s or base


def auto_title(name: str = "", file: str = "", tag: str = "") -> str:
    """L1 模板拼接：永远可用，不依赖任何处理步骤。

    队列是全局的，所以「剧名」只能来自项目名（从项目挑片时才有）或文件名。
    优先级：项目名-集号 > 项目名-文件名 > 文件名。
    绝不依赖第 4 步分镜（可选步骤），也绝不假装能「智能生成」。
    """
    base = _trim_product_tail(os.path.splitext(os.path.basename(file or ""))[0])
    series = (name or "").strip()
    ep = _ep_no(tag or "") or _ep_no(base)
    if series and ep:
        return f"{series}-{ep}"
    if series:
        return f"{series}-{base}"
    # 没有项目上下文：整段文件名就是最完整的信息，别去掉「第7集-」，
    # 去掉反而少了一个能区分同批文件的标记（2026-11-13 回归踩到）。
    return base or "未命名"


def auto_topics(name: str = "") -> list[str]:
    """L2 规则话题：零成本，有剧名才有。"""
    out = []
    if (name or "").strip():
        out.append("#" + re.sub(r"\s+", "", name))
    for t in ("#短剧", "#反转"):
        if t not in out:
            out.append(t)
    return out[:4]


def cur_gen(p: dict) -> int:
    """当前结构版本号（与 web/app.js renderSets 同一口径）。"""
    return int((p.get("batch") or {}).get("gen") or p.get("plan_gen") or 0)


def pickable_sets(p: dict) -> list[dict]:
    """这个项目**现在真的能拿去发**的成片，界面和 add_jobs 共用一份。

    两道过滤都必须有，少一道界面就会摆出点不动的东西：
      ① 只认当前结构版本：结构重生成后，旧 gen 的成片文件还在硬盘上，混进来
         会让人以为「这套已经出好了」，发出去还是上一版结构（主界面 renderSets
         早就是这个口径，这里跟着走，不给两套规则）；
      ② 没有 output 的套不能选：点了只会拿到一句报错。
    """
    cur = cur_gen(p)
    hook = bool((p.get("plans") or []) and (p.get("plans") or [{}])[0].get("mode") == "hook")
    out = []
    for s in (p.get("sets") or []):
        if not s.get("output") or int(s.get("gen") or 0) != cur:
            continue
        idx = s.get("index")
        out.append({
            "index": idx,
            # 单集钩子的「一套」就是一集，列表里写「第 3 套」会让人以为还有第 4 集没出
            "tag": s.get("tag") or f"第 {idx} {'集' if hook else '套'}",
            "file": core.abs_output(s["output"]),
            "name": os.path.basename(s["output"]),
            "dur": s.get("output_duration") or 0,
            "size": s.get("output_size") or 0,
        })
    out.sort(key=lambda x: (x["index"] is None, x["index"]))
    return out


def scan_dir(path: str, recursive: bool = True) -> list[dict]:
    """扫一个文件夹里的视频（选文件夹那条路）。

    按文件名自然排序：`第2集` 要排在 `第10集` 前面，字典序会反过来，用户一眼就看出不对。
    """
    path = os.path.abspath(str(path or ""))
    if not os.path.isdir(path):
        raise PublishError("这个文件夹不存在，请重新选一下")
    found = []
    if recursive:
        for root, _dirs, files in os.walk(path):
            for f in files:
                if os.path.splitext(f)[1].lower() in VIDEO_EXT:
                    found.append(os.path.join(root, f))
    else:
        for f in os.listdir(path):
            fp = os.path.join(path, f)
            if os.path.isfile(fp) and os.path.splitext(f)[1].lower() in VIDEO_EXT:
                found.append(fp)

    def _key(s: str):
        b = os.path.basename(s)
        num = re.search(r"\d+", b)
        return (os.path.dirname(s).lower(), int(num.group()) if num else 10 ** 9, b.lower())

    found.sort(key=_key)
    return [{"path": f, "name": os.path.basename(f),
             "size": os.path.getsize(f) if os.path.isfile(f) else 0} for f in found]


def add_jobs(files: list[str] | None = None, sets: list[int] | None = None,
             pid: str = "", dir_path: str = "") -> dict:
    """把选中的视频加进队列（状态 draft，还没排期）。

    三条入口共用这里：选文件、选文件夹、从某个项目挑成片。

    **入队只记「文件在哪」**（2026-11-21 用户定）：存在性、大小、路径指纹，
    都是 stat 级，毫秒返回。以前这里还同步解整条音轨算内容指纹、再对每组
    疑似同源跑段级定位（22 条 5-11 分钟成片要 34 秒，冷缓存+并发实测 142 秒），
    而查重结果只是提示、从不拦人 —— 用一个可选提示阻塞确定的入队动作，不值。
    现在 `afp` 留空，`repost_conflicts` 只按文件名/台账比（零成本）；预览路径
    也不再比指纹（2026-11-21，见 repost_conflicts 的说明）。
    """
    # 并发提交（连点、双击）会各自读旧队列再写回，后写覆盖先写。入队既然已经
    # 只剩毫秒级操作，串行化不会卡任何人。
    with _ADD_LOCK:
        return _add_jobs(files, sets, pid, dir_path)


_ADD_LOCK = threading.Lock()


@_qlocked
def _add_jobs(files: list[str] | None = None, sets: list[int] | None = None,
              pid: str = "", dir_path: str = "") -> dict:
    q = queue()
    tpl = q["template"]
    p = core.load(pid) if pid else {}
    pname = (p.get("name") or "") if p else ""

    picked: list[dict] = []
    if dir_path:
        for it in scan_dir(dir_path):
            picked.append({"path": it["path"], "src": "dir",
                           "name": "", "dur": 0, "size": it["size"]})

    by_index = {s["index"]: s for s in (pickable_sets(p) if p else [])}
    for idx in (sets or []):
        s = by_index.get(idx)
        if not s:
            raise PublishError(f"第 {idx} 套还没有成片，先生成再发")
        picked.append({"path": s["file"], "src": f"set:{idx}",
                       "name": s["tag"], "dur": s["dur"], "size": s["size"]})

    for f in (files or []):
        path = os.path.abspath(str(f))
        if not os.path.isfile(path):
            raise PublishError(f"找不到这个文件：{os.path.basename(path)}")
        picked.append({"path": path, "src": "file", "name": "",
                       "dur": 0, "size": os.path.getsize(path)})

    if not picked:
        raise PublishError("先选要发的视频")

    bad = [it for it in picked if it["size"] > MAX_MB * 1024 * 1024]
    if bad:
        raise PublishError(f"「{os.path.basename(bad[0]['path'])}」超过 {MAX_MB // 1024}GB，"
                           f"视频号传不上去")

    have = {j.get("hash") for j in q["jobs"] if j.get("status") != "skipped"}
    added, dup = [], []
    for it in picked:
        h = file_hash(it["path"])
        if h in have:
            dup.append(os.path.basename(it["path"]))
            continue
        have.add(h)
        # id 故意用**纯 12 位十六进制**、不带 job_ 前缀：
        # audit.route_of 只把「数字 / 12·24·32·36 位 hex」归一成 {id}，
        # 带前缀的话每个作业都会变成一条独立路由，后台统计全碎掉。
        job = {
            "id": hashlib.sha1(f"{h}{datetime.now()}".encode()).hexdigest()[:12],
            "file": it["path"], "src": it["src"], "hash": h,
            "pid": pid or "", "project": pname,
            "title": "", "title_src": "template", "account_id": "",
            "plan_at": "", "status": "draft", "attempts": 0,
            "error": "", "remote_hint": "", "verified_at": "",
            "dur": it["dur"] or 0, "size": it["size"],
            "created_at": fmt(datetime.now()), "done_at": "",
            **_tpl_fields(tpl),
        }
        job["title"] = auto_title(pname, it["path"], it["name"])
        # 模板里没填话题才自动生成；填了就按用户填的来（别覆盖人家的设置）
        if not job["topics"]:
            job["topics"] = auto_topics(pname)
        # 这里以前会 `audiofp.compute(path)` 解整条音轨算内容指纹。已移除
        # （2026-11-21 用户定：入队这一步不需要查内容）。老条目里的 afp 字段
        # 原样留着不动，repost_conflicts 见到空 afp 就只走文件名口径。
        q["jobs"].append(job)
        added.append(job["id"])

    save_queue(q)
    # 刚加进来的这批里，有以前发过 / 交过平台的吗？只提示，不拦。
    new_ids = set(added)
    rep = repost_conflicts([j for j in q["jobs"] if j.get("id") in new_ids], q)
    return {"added": added, "dup": dup, "total": len(q["jobs"]), "repost": rep}


def job_full(j: dict) -> dict:
    """补全老作业缺的新字段。老数据没有 collection/link 这些，别让界面拿到 undefined。"""
    base = {
        "short_title": "", "topics": [], "cover_ts": 0.0, "cover_file": "",
        "collection": "", "collection_id": "",
        "location": "", "location_poi": "", "location_city": "",
        "activity": "", "activity_id": "",
        "link": "", "link_kind": "", "link_id": "", "link_title": "",
        "original": False, "declare": "",
        "account_identity": {}, "submit_started_at": "",
        "pid": "", "project": "",
    }
    base.update({k: v for k, v in j.items() if v is not None})
    return base


@_qlocked
def update_job(jid: str, patch: dict) -> dict:
    q = queue()
    job = next((j for j in q["jobs"] if j["id"] == jid), None)
    if not job:
        raise PublishError("这条已经不在队列里了")
    if job.get("status") in LOCKED_STATUS:
        raise PublishError("这条已经交给视频号了，这里改不了；要改得去视频号后台")

    for k in FIELD_KEYS:
        if k not in patch:
            continue
        job[k] = norm_field(k, patch[k])
        if k == "title":
            job["title_src"] = "manual"      # 手动改过就别再被自动标题覆盖

    # 选择器类的配套 id：跟名字一起存，上传时用 id 点，用名字回读核对
    # cover_file 已在上面的 FIELD_KEYS 循环里走 norm_field 校验过，
    # 这里不能再拿裸字符串覆盖一遍，否则白名单校验等于白做。
    for k in ("collection_id", "location_poi", "location_city",
              "activity_id", "link_id", "link_title"):
        if k in patch:
            job[k] = str(patch[k] or "")[:200]
    if "link_kind" in patch:
        # 链接类型是枚举（剧集/文章/红包封面/短剧），不是随便一个字符串
        job["link_kind"] = norm_field("link_kind", patch["link_kind"])

    if "account_id" in patch:
        # 提交之后账号随任务走，不能单独改（2026-11-14 用户定稿：状态修改
        # 只在「已提交」里做，形式是取消而不是换号）。界面本来也不提供入口。
        if job.get("status") in ("queued", "publishing", "scheduled"):
            raise PublishError("这条已经在提交队列里了，账号不能单独改；"
                               "不想发就点「不上传这条」收回队列")
        aid = str(patch["account_id"] or "")
        if aid and not account_name(aid):
            raise PublishError("账号不存在，刷新一下列表")
        job["account_id"] = aid

    save_queue(q)
    return job


@_qlocked
def apply_fields(fields: dict) -> dict:
    """把「整批通用设置」套到所有还没交出去的视频上，并记成批次默认值。

    只处理**整批一致**的那几项（BATCH_KEYS）：标题 / 封面取帧秒数是逐条不同
    的，整批套同一个标题等于把 15 条视频发成 15 个同名作品。

    短标题（2026-11-21 用户要求「要能统一设置」）走整批，但**不硬覆盖**：只写
    「还空着」或「还等于上次整批那个值」的条目。单条自己改过的保留下来，否则
    用户只是调一下话题，22 条各自写好的短标题就被冲成同一句了。

    封面图片是唯一的例外：它只写 template（整批默认），不灌进每条 job ——
    job.cover_file 留着当「这条单独换过」的覆盖值，换全批图时也不冲掉它。
    """
    clean: dict = {}
    cover = (norm_field("cover_file", fields["cover_file"])
             if "cover_file" in fields else None)
    for k in BATCH_KEYS:
        if k in fields:
            clean[k] = norm_field(k, fields[k])
    # 选择器类的配套 id 不算「要改的项」，但只要有就一起落
    extra = {k: str(fields[k] or "")[:200]
             for k in ("collection_id", "location_poi", "location_city",
                       "activity_id", "link_kind", "link_id", "link_title")
             if k in fields}
    if not clean and not extra and cover is None:
        raise PublishError("没有要改的项")

    q = queue()
    old_tpl = dict(q["template"] or {})   # 判断「还跟着整批值吗」要用改之前的那份
    for k, v in {**clean, **extra}.items():
        q["template"][k] = v
    if cover is not None:
        q["template"]["cover_file"] = cover
    n = 0
    for j in q["jobs"]:
        if j.get("status") in LOCKED_STATUS:
            continue
        # 只套到还没开始传的（draft）和上次没传成的（failed/manual）——
        # 已经在排队等发的不要动，用户可能一条条改过
        if j.get("status") not in ("draft", "failed", "manual"):
            continue
        for k, v in clean.items():
            if k == "short_title":
                cur = str(j.get("short_title") or "")
                if cur and cur != str(old_tpl.get("short_title") or ""):
                    continue      # 这条自己写过短标题，别被整批覆盖
            j[k] = list(v) if k == "topics" else v
        for k, v in extra.items():
            j[k] = v
        n += 1
    save_queue(q)
    return {"n": n, "template": q["template"]}


@_qlocked
def del_job(jid: str, allow_platform: bool = False) -> None:
    """从队列里删掉一条。

    **已定时的能不能删，看调用方**（2026-11-14 改）：界面上去掉了「记成已取消」
    那个按钮，如果不让删，scheduled 的条目就永远出不了队列、这个号被永久锁住。
    所以允许 allow_platform=True 的删除，但界面必须在确认框里说清「平台到点
    照样会发」，删掉只是不再显示——不能让用户以为删了就没事了。
    """
    q = queue()
    job = next((j for j in q["jobs"] if j["id"] == jid), None)
    if not job:
        return
    st = job.get("status")
    if st == "publishing":
        # 浏览器正在传，删掉本地记录不会让发表停下来，
        # 只会把这条弄丢、并让批次锁号悄悄解开。
        raise PublishError("这条正在上传，删了也停不下来，等它传完再说")
    if st == "uncertain":
        raise PublishError("这条还没核实结果，先点「后台已有这条」或「核实没发出去」")
    if st in ("scheduled", "published") and not allow_platform:
        raise PublishError("这条已经交给视频号了，要取消得去视频号后台")
    _ledger_add(q, job)
    q["jobs"] = [j for j in q["jobs"] if j["id"] != jid]
    save_queue(q)


@_qlocked
def clear_draft() -> dict:
    """清空还没开始排的草稿（不影响已排/已发）。"""
    q = queue()
    keep = [j for j in q["jobs"] if j.get("status") not in ("draft",)]
    n = len(q["jobs"]) - len(keep)
    q["jobs"] = keep
    save_queue(q)
    return {"removed": n}


# ── 排期 ────────────────────────────────────────────────────────────────
def build_plan(first_at: str, interval_min: int, n: int,
               existing: list[dict] | None = None,
               account_id: str = "",
               now_at: datetime | None = None) -> dict:
    """算排期 + 全部告警。**只算不存**，前端拿它渲染预览。

    `now_at` 只给回归脚本传固定时间用（2026-11-15）：排期结果不该依赖
    「跑脚本的钟点」，否则跨天就假红。不传就跟原来一样取当前时间。
    """
    start = parse(first_at)
    if not start:
        raise PublishError("先选一个首条发布时间")
    try:
        gap = int(interval_min)
    except (TypeError, ValueError):
        gap = INTERVAL_DEFAULT
    if n <= 0:
        raise PublishError("队列里还没有视频")

    errors, warns = [], []
    t0 = now_at or now()

    if gap < INTERVAL_MIN:
        errors.append({
            "code": "gap_too_small", "level": "error",
            "msg": f"间隔最少 {INTERVAL_MIN} 分钟。视频号是「先提交、平台到点发」，"
                   f"间隔再小两条会挤在同一分钟，平台那边只成一条",
        })
    elif gap < INTERVAL_TIGHT:
        warns.append({
            "code": "gap_small", "level": "warn",
            "msg": f"间隔 {gap} 分钟。同一个号连发这么密容易被判同质化——"
                   f"同一批素材建议至少 {INTERVAL_TIGHT} 分钟，不同的片子错开就没事",
        })

    # 上传耗时要先算出来，才能判断「首条留的时间够不够传完」
    size_mb = sum(float(j.get("size") or 0) for j in (existing or [])) / (1024 * 1024)
    eta_min = max(int(size_mb / (UPLOAD_MBPS / 8 * 60)) if size_mb else 0,
                  int(round(n * UPLOAD_ETA_SEC / 60.0)))

    if start < t0 + timedelta(minutes=LEAD_MIN):
        errors.append({
            "code": "too_soon", "level": "error",
            "msg": f"首条最早只能设 {LEAD_MIN} 分钟后（视频号的规定），现在设不了这个时间",
        })
    elif n >= 2 and start < t0 + timedelta(minutes=eta_min + LEAD_MIN):
        # 间隔调小后这条最关键：10 条 × 5 分钟只跨 45 分钟，但上传本身要 20 多分钟。
        # 首条留的时间不够，前面几条必然赶不上，得提前说清并给出能设的时间。
        sug = fmt(t0 + timedelta(minutes=eta_min + LEAD_MIN + 5))
        warns.append({
            "code": "too_tight", "level": "warn",
            "msg": f"这 {n} 条要一条条上传再设定时，程序大约要 {eta_min} 分钟，"
                   f"再加上平台要求的 {LEAD_MIN} 分钟提前量，首条最早设 {sug} 才来得及。",
            "suggest_first": sug,
        })
    if n > BATCH_MAX:
        warns.append({
            "code": "batch_big", "level": "warn",
            "msg": f"这一批 {n} 条，超过建议的 {BATCH_MAX} 条。批次越大越容易中途出问题",
        })

    times = [start + timedelta(minutes=gap * i) for i in range(n)]
    last = times[-1]
    limit = t0 + timedelta(days=LEAD_MAX_DAYS)
    split = None
    if last > limit:
        fit = 0
        for t in times:
            if t <= limit:
                fit += 1
            else:
                break
        split = {"fit": fit, "rest": n - fit,
                 "until": fmt(times[max(0, fit - 1)]) if fit else ""}
        errors.append({
            "code": "too_late", "level": "error",
            "msg": f"视频号最多只能提前 {LEAD_MAX_DAYS} 天设定时。"
                   f"这 {n} 条会排到 {fmt(last)}，超了。"
                   f"建议拆两批：先排 {fit} 条到 {fmt(times[max(0, fit - 1)]) if fit else '—'}，"
                   f"剩下 {n - fit} 条过几天再排",
            "split": split,
        })

    # 同账号同日密度：这是「多个视频发同一个号」最主要的翻车点
    by_day: dict[str, int] = {}
    for t in times:
        by_day[t.strftime("%Y-%m-%d")] = by_day.get(t.strftime("%Y-%m-%d"), 0) + 1
    worst_day, worst_n = "", 0
    for d, c in by_day.items():
        if c > worst_n:
            worst_day, worst_n = d, c
    if worst_n >= 6:
        warns.append({
            "code": "same_day_busy", "level": "warn",
            "msg": f"{worst_day} 一天要发 {worst_n} 条。同一账号一天连发这么多，"
                   f"很容易被判同质化搬运，建议跨天铺开",
        })

    # 与已有作业撞时间（同账号 ±10 分钟）
    if existing:
        taken = [parse(j.get("plan_at")) for j in existing
                 if j.get("plan_at") and j.get("account_id") == account_id
                 and j.get("status") in ("queued", "scheduled")]
        taken = [t for t in taken if t]
        for t in times:
            if any(abs((t - o).total_seconds()) < 600 for o in taken):
                warns.append({
                    "code": "conflict", "level": "warn",
                    "msg": f"{fmt(t)} 这条和已定时的内容撞了（10 分钟内），建议错开",
                })
                break

    return {
        "ok": not errors,
        "errors": errors,
        "warns": warns,
        "times": [fmt(t) for t in times],
        "first": fmt(times[0]),
        "last": fmt(last),
        "n": n,
        "interval_min": gap,
        "split": split,
        "eta_min": eta_min,
        "per_day": by_day,
    }


def _norm_file(p) -> str:
    """台账里的路径按「同一个文件」归一，别让 \\ 和 / 把同一条算成两条。"""
    return str(p or "").strip().replace("\\", "/").rstrip("/").lower()


def repost_conflicts(jobs: list[dict], q: dict | None = None) -> list[dict]:
    """这批里哪些视频以前发过 / 交过平台（只看提交台账）。

    **只提醒，不拦**：同一个片子换个时间重发是合法场景（2026-11-15 A 档）。
    队列里现存的条目 `add_jobs` 已经按 hash 挡过，这里专治「删掉记录或归档
    之后又加回来」——那时队列里查不到，但台账里留着证据。

    **2026-11-21 起不比音频指纹**：这个函数挂在 `preview()` 里，而 preview 是
    「改一下时间就调一次」的接口。指纹比对要对最像的一条解两条完整音轨，实测
    22 条 / 7 对 = 10.2 秒（占 preview 全部耗时），界面上就是「设完时间要等十
    几秒」。内容查重已按用户要求从入队与预览两条路径上摘掉；老条目里残留的
    afp 字段不再读（字段留着不碍事）。
    """
    q = q or queue()
    prev: dict[str, list[dict]] = {}
    for rec in (q.get("ledger") or []):
        f = _norm_file(rec.get("file"))
        st = str(rec.get("status") or "")
        if not f or st in ("cancelled", "skipped"):
            continue
        at = str(rec.get("scheduled_at") or rec.get("submit_started_at")
                 or rec.get("left_at") or "")
        prev.setdefault(f, []).append({
            "status": st, "at": at, "title": str(rec.get("title") or "")[:60],
        })
    out = []
    for j in jobs:
        hits = prev.get(_norm_file(j.get("file"))) or []
        if not hits:
            continue
        hits.sort(key=lambda x: x["at"], reverse=True)
        out.append({
            "job_id": str(j.get("id") or ""),
            "file": os.path.basename(str(j.get("file") or "")),
            "times": len(hits),
            "kind": "file", "prev_status": hits[0]["status"],
            "prev_at": hits[0]["at"], "prev_title": hits[0]["title"],
        })
    return out


def _plan_intent(jobs: list[dict], first_at, interval_min,
                 repost: list[dict]) -> str:
    """「确认框当时看的是哪一批」的指纹，只算不存（不留服务端状态）。

    刻意**不含账号**：预览接口拿不到「当前号」，而 batch.account_id 是历史值，
    绝不能参与「当前」判断。账号在确认框里已经写全、start 自己也会校验。
    """
    raw = {
        "first": str(first_at or ""),
        "gap": int(interval_min or 0),
        "files": sorted(str(j.get("hash") or _norm_file(j.get("file")))
                        for j in jobs),
        "repost": sorted(
            f"{_norm_file(r.get('file'))}|{r.get('prev_status')}|{r.get('prev_at')}"
            for r in repost),
    }
    return hashlib.sha1(
        json.dumps(raw, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]


def preview(first_at: str, interval_min: int) -> dict:
    q = queue()
    jobs = [j for j in q["jobs"] if j.get("status") in ("draft", "queued")]
    acc = q["batch"].get("account_id") or ""
    res = build_plan(first_at, interval_min, len(jobs), jobs, acc)
    res["titles"] = [j.get("title") or os.path.basename(j.get("file") or "")
                     for j in jobs]
    # 指纹只覆盖「还没安排」的那批：start 落库时也只看 draft，两边必须同口径。
    drafts = [j for j in jobs if j.get("status") == "draft"]
    res["repost"] = repost_conflicts(drafts, q)
    res["intent"] = _plan_intent(drafts, first_at, interval_min, res["repost"])
    return res


@_qlocked
def start(first_at: str, interval_min: int, account_id: str,
          intent: str = "") -> dict:
    """落库排期：draft → queued，写入计划时间。"""
    q = queue()
    jobs = [j for j in q["jobs"] if j.get("status") == "draft"]
    if not jobs:
        raise PublishError("队列里没有还没安排的视频")
    if not account_id:
        raise PublishError("先选一个发布账号")
    if not account_name(account_id):
        raise PublishError("账号不存在，刷新一下列表")

    res = build_plan(first_at, interval_min, len(jobs), jobs, account_id)
    if not res["ok"]:
        raise PublishError(res["errors"][0]["msg"])
    # 确认框看过之后，视频或时间又变了 → 不能还按旧确认提交。指纹只算不存，
    # 前端把预览返回的 intent 原样带回来即可（留空兼容老前端 / 脚本）。
    want = _plan_intent(jobs, first_at, interval_min,
                        repost_conflicts(jobs, q))
    if intent and intent != want:
        raise PublishError("这批视频或时间刚改过，请重新看一遍预览再提交。")

    ident = account_identity(account_id)
    for job, t in zip(jobs, res["times"]):
        job["plan_at"] = t
        job["account_id"] = account_id
        # 发表前拿它跟浏览器里登的号比对；资料没抓全就是 {}，跳过核对
        job["account_identity"] = ident
        job["submit_started_at"] = ""
        job["status"] = "queued"
        job["attempts"] = 0
        job["error"] = ""
    # 这批的通用设置已经随 job 走了（_tpl_fields 入队快照 + 自动套用实时同步）；
    # 唯一还挂在模板上的是整批封面——提交时快照给每条已排的，然后把模板恢复
    # 成默认的白纸。这样「开始上传」之后队列和通用设置就是干净的一版，下一批
    # 从头填，不会把上一批的话题位置带过去（2026-11-14 用户定稿）。
    cov = (q.get("template") or {}).get("cover_file") or ""
    for job in jobs:
        if cov and not (job.get("cover_file") or "").strip():
            job["cover_file"] = cov
    q["template"] = {}
    q["batch"] = {"id": "bat_" + hashlib.sha1(fmt(datetime.now()).encode()).hexdigest()[:8],
                  "first_at": res["first"], "interval_min": res["interval_min"],
                  "account_id": account_id, "created_at": fmt(datetime.now())}
    # 排期这一刻就把「当前发到」写死成这个号，刷新页面也不会漂到别处。
    q["current_account_id"] = account_id
    save_queue(q)
    return {"started": len(jobs), "first": res["first"], "last": res["last"]}


@_qlocked
def stop(account_id: str = "") -> dict:
    """撤回排期：还没传的（queued）退回 draft，同时数一下已经在平台的条目。

    account_id 非空时只撤**这个号**的（2026-11-14 加）：界面上「批量取消」
    挂在当前号的已提交列表下，操作范围必须跟显示范围一致，否则在 B 号页面上
    点一下会把 A 号正在排队的也退回来——跨号误伤。
    已上传的（scheduled）**本地退不了**：平台的删除自动化还没打通
    （wxdel.py 顶部有记录），只能如实让用户去助手手动删——绝不能假装
    「撤回了」，到点它还是会照发。
    """
    account_id = str(account_id or "")
    q = queue()
    n = 0
    for j in q["jobs"]:
        if account_id and j.get("account_id") != account_id:
            continue
        if j.get("status") == "queued":
            j["status"] = "draft"
            j["plan_at"] = ""
            n += 1
    save_queue(q)
    on_platform = len([j for j in q["jobs"]
                       if j.get("status") == "scheduled"
                       and (not account_id or j.get("account_id") == account_id)])
    return {"reverted": n, "on_platform": on_platform}


@_qlocked
def reset_stuck_publishing() -> int:
    """把上次没跑完就退出的「正在上传」条目放回队列。

    程序在传到一半时被关掉/断电，条目会永远停在 publishing——worker 只认
    queued，不重置就再也不会发。

    **2026-11-15 改**：有 submit_started_at（点过发表）的绝不放回 queued，
    改成「结果待核实」，等人去后台核对。以前无条件重放，退出正好发生在
    「点发表之后、回读之前」时会在平台上多出一条 —— 那是重复发表。
    """
    q = queue()
    n = 0
    for j in q["jobs"]:
        if j.get("status") != "publishing":
            continue
        if j.get("submit_started_at"):
            j["status"] = "uncertain"
            j["error"] = ("上次在点「发表」前后退出了，不知道平台收没收到。"
                          "先去视频号助手「待发布」里核对，别直接重试。")
        else:
            j["status"] = "queued"
            j["attempts"] = 0
            j["error"] = "程序中途退出，已自动重新加入等待上传"
        n += 1
    if n:
        save_queue(q)
    return n


# ── 到点与推进 ──────────────────────────────────────────────────────────
def due_jobs() -> list[dict]:
    """已经到点、还没发的条目（P1 靠前端轮询来问，P2 由 worker 消费）。"""
    q = queue()
    t = now()
    out = []
    for j in q["jobs"]:
        if j.get("status") != "queued":
            continue
        at = parse(j.get("plan_at"))
        if at and at <= t:
            out.append(j)
    return out


@_qlocked
def mark(jid: str, action: str, note: str = "") -> dict:
    """人工操作：退回待排期 / 已发 / 跳过。

    「排到即上传」（2026-11-13 定稿）之后，「已发」按钮已经从界面上撤了
    （程序会传，不需要人回来点确认）；这里保留 action 兼容旧数据。
    queued → draft 是「不上传这条」：退回待排期，随时可以再排。
    """
    q = queue()
    job = next((j for j in q["jobs"] if j["id"] == jid), None)
    if not job:
        raise PublishError("这条已经不在队列里了")
    if action in ("resolved_published", "resolved_cancelled"):
        # 「结果待核实」的收尾：只能由人在后台看过之后选，程序不猜
        if job.get("status") != "uncertain":
            raise PublishError("只有「结果待核实」的条目能做这个确认")
        ok = action == "resolved_published"
        job["status"] = "published" if ok else "skipped"
        job["done_at"] = fmt(datetime.now())
        job["error"] = "" if ok else "核实后台没有这条，已停止"
        job["resolved_by"] = "user"
        if ok:
            job["verified_at"] = job["done_at"]
    elif action == "published":
        job["status"] = "published"
        job["done_at"] = fmt(datetime.now())
        job["verified_at"] = job["done_at"]
        job["error"] = ""
    elif action == "draft":
        # queued：还没传，随时能退；failed：这次没传成，也可以退回去以后重排
        # （2026-11-14 用户要求）。scheduled 及以上不行——那已经在平台上了。
        if job.get("status") not in ("queued", "failed", "manual"):
            raise PublishError("只有还没传上平台的条目能退回「还没安排」；"
                               "已经传到平台的要到视频号助手手动删")
        job["status"] = "draft"
        job["plan_at"] = ""
        job["error"] = ""
        job["attempts"] = 0
    elif action == "skipped":
        if not (note or "").strip():
            raise PublishError("跳过要写一句原因，方便以后回头看为什么没发")
        job["status"] = "skipped"
        job["done_at"] = fmt(datetime.now())
        job["error"] = note.strip()[:120]
    else:
        raise PublishError("不认识的操作")
    save_queue(q)
    return job


@_qlocked
def set_status(jid: str, status: str, note: str = "", extra: dict | None = None) -> dict:
    """给自动发表流程改状态用（worker 调）。

    允许的状态：publishing / scheduled / failed / queued / manual / uncertain。
    `extra` 里的 attempts / scheduled_at / verified_at / submit_evidence 一起写进去。

    为什么单独一个函数：`mark()` 是给人手动点「已发/跳过」用的，
    它只认那几个动作；自动流程要走的状态多得多，混在一起会把
    「人点的」和「机器点的」混为一谈。
    """
    ok = ("publishing", "scheduled", "failed", "queued", "manual", "draft",
          "uncertain")
    if status not in ok:
        raise PublishError("不认识的状态")
    q = queue()
    job = next((j for j in q["jobs"] if j["id"] == jid), None)
    if not job:
        raise PublishError("这条已经不在队列里了")
    job["status"] = status
    if note:
        job["error"] = note[:200]
    elif status in ("scheduled", "publishing"):
        job["error"] = ""
    for k, v in (extra or {}).items():
        job[k] = v
    if status == "scheduled":
        job["done_at"] = fmt(datetime.now())
    save_queue(q)
    return job


@_qlocked
def retry(jid: str) -> dict:
    """把一条失败的作业放回队列。返回 {"job": ..., "moved_to": 新时间或空}。

    `moved_to` 非空 = 原计划时间已经过了（或马上就到），这次给它顺延了；
    界面要如实说出来，否则用户以为还按原来 13:10 发。
    """
    q = queue()
    job = next((j for j in q["jobs"] if j["id"] == jid), None)
    if not job:
        raise PublishError("这条已经不在队列里了")
    # 平台的定时只能定在未来：拿过期时间重发，必然死在「设定时」那一步。
    moved_to = ""
    old = parse(job.get("plan_at"))
    if old and old <= now() + timedelta(minutes=3):
        job["plan_at"] = fmt(now() + timedelta(minutes=RETRY_LEAD_MIN))
        moved_to = job["plan_at"]
    job["status"] = "queued"
    job["attempts"] = 0
    job["error"] = ""
    save_queue(q)
    return {"job": job, "moved_to": moved_to}


# ── 汇总（给状态接口）───────────────────────────────────────────────────
def summarize(q: dict | None = None) -> dict:
    q = q or queue()
    counts: dict[str, int] = {}
    for j in q["jobs"]:
        counts[j.get("status") or "draft"] = counts.get(j.get("status") or "draft", 0) + 1
    due = due_jobs()
    pending = counts.get("draft", 0) + counts.get("queued", 0) + counts.get("manual", 0)
    return {
        "counts": counts,
        "due": [{"id": j["id"], "title": j.get("title") or "",
                 "plan_at": j.get("plan_at") or "",
                 "account": account_name(j.get("account_id") or "")} for j in due],
        "pending": pending,
        "batch": q["batch"],
    }


def overdue() -> list[dict]:
    """错过时间的条目（程序关着的时候到点了）。启动时给用户看一眼。"""
    return due_jobs()


def open_count() -> int:
    return len([j for j in queue()["jobs"] if j.get("status") in OPEN_STATUS])


def jobs_of_project(pid: str) -> int:
    """这个项目还有多少条视频在发布队列里（删项目前提醒用）。"""
    return len([j for j in queue()["jobs"]
                if j.get("pid") == pid and j.get("status") in OPEN_STATUS])


# 删项目时可以安全移除的作业状态。publishing / uncertain 可能已经交到平台，
# 留着才能继续追踪；submit_started_at 非空同理，一律不动。
PURGE_JOB_STATUS = ("draft", "queued", "failed", "manual")


@_qlocked
def purge_project(pid: str) -> dict:
    """把某个项目的发布痕迹从全局队列/归档/备份里摘掉。

    ledger（提交台账）没有 pid 字段，是防重复发的证据，**不删**。
    返回各处的清理数量给接口回话。
    """
    res: dict = {"queue": 0, "archive": 0, "backups": 0, "kept": 0}
    q = queue()
    keep, removed = [], []
    for j in q["jobs"]:
        if (j.get("pid") == pid and j.get("status") in PURGE_JOB_STATUS
                and not j.get("submit_started_at")):
            removed.append(j)
        else:
            keep.append(j)
    if removed:
        for j in removed:
            _ledger_add(q, j)   # 走前留一笔，别把「可能已经提交过」的证据抹掉
        q["jobs"] = keep
        save_queue(q)           # 注意：这一步可能新写一份 queue_backup 快照
        res["queue"] = len(removed)
    res["kept"] = len([j for j in q["jobs"] if j.get("pid") == pid])

    # 归档：按次分批，只摘掉匹配 job，批次壳留着（归档语义是「那次归档」）
    try:
        with open(ARCHIVE_FILE, encoding="utf-8") as f:
            old = json.load(f)
    except Exception:  # noqa: BLE001
        old = []
    if isinstance(old, dict):
        old = [old]
    batches, n = [], 0
    for b in (old if isinstance(old, list) else []):
        if not isinstance(b, dict):
            continue
        jobs = b.get("jobs") or []
        left = [j for j in jobs
                if not (isinstance(j, dict) and j.get("pid") == pid)]
        n += len(jobs) - len(left)
        b = dict(b)
        b["jobs"] = left
        batches.append(b)
    if n:
        try:
            with open(ARCHIVE_FILE, "w", encoding="utf-8") as f:
                json.dump(batches, f, ensure_ascii=False, indent=1)
            res["archive"] = n
        except OSError:
            pass

    # 队列备份：save_queue 刚可能又写了一份新的，所以必须在它之后再扫。
    # 备份里只摘 jobs，ledger / batch / template 没 pid 归属，原样留着。
    bdir = os.path.join(core.DATA_DIR, "queue_backup")
    if os.path.isdir(bdir):
        for fn in os.listdir(bdir):
            if not (fn.startswith("queue_") and fn.endswith(".json")):
                continue
            path = os.path.join(bdir, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                jobs = d.get("jobs") or []
                left = [j for j in jobs
                        if not (isinstance(j, dict) and j.get("pid") == pid)]
                if len(left) == len(jobs):
                    continue
                d["jobs"] = left
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(d, f, ensure_ascii=False, indent=1)
                os.replace(tmp, path)
                res["backups"] += 1
            except Exception:  # noqa: BLE001
                pass
    return res
