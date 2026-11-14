"""钩子工坊 Key 代理服务端 —— 纯标准库版（部署在香港轻量服务器，0.5G 内存）。

不用 fastapi/uvicorn/pip/apt：低配机器装依赖会被拖死，标准库足够。
零依赖、单文件、内存 ~15MB，由 systemd 直接用系统 python3 拉起。

接口：
  GET  /health
  POST /api/auth/register          X-Admin-Key 无关，注册账号
  POST /api/auth/login             登录换令牌
  GET  /api/auth/me                X-HS-Token   当前账号
  POST /api/auth/password          X-HS-Token   改密
  GET  /api/quota                  X-HS-Token   今日剩余次数（**只认账号令牌**）
  POST /api/proxy/chat/completions X-HS-Token   转发到百炼
  GET  /admin                     运营后台页面（HTML，数据接口都要 X-Admin-Key）
  GET  /admin/stats                X-Admin-Key
  GET  /admin/accounts             X-Admin-Key  账号清单 + 今日用量
  POST /admin/account              X-Admin-Key  封禁/解绑/补次数/重置密码/改等级
  GET  /admin/series               X-Admin-Key  近 N 天趋势
  GET  /admin/release              X-Admin-Key  读版本闸配置
  POST /admin/release              X-Admin-Key  写版本闸配置（推强制更新）
  GET  /admin/logs                 X-Admin-Key
  GET  /admin/syslog               X-Admin-Key  服务端自身日志（异常堆栈 + 运行事件）
"""

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import signal
import sqlite3
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DASHSCOPE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DASHSCOPE_KEY = os.environ["DASHSCOPE_KEY"]
# 转发上游的超时（秒）。**别调回 120**：第 2 步「整集台词 → N 套结构」在推理型
# 模型（qwen3.5-plus 这类）上实测要 90-210 秒，2.3 万 token 输入的探针就跑了 93 秒；
# 原来 120 秒一超就把上游的 200 变成我们自己的 502，用户只看到「对面服务端出错」。
# 与客户端 analyze.LLM_TIMEOUT（600 秒）对齐：第 3 步单次实测就要 170-230 秒，
# 慢模型 / 排队时更长；300 秒会把正常任务掐成「超时」，用户还以为是网络断了。
UPSTREAM_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "600"))
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
DB_PATH = os.environ.get("DB_PATH", "/opt/hsproxy/data.sqlite")
# 每日额度**按等级算**（2026-11-07 加等级体系）。
# 免费额度仍然是按天算，不是一辈子 10 次（2026-11-04 用户决定，勿改回）；
# 每天 0 点（北京时间）自动回满，当天用完只能等明天、让运营改等级或补次数，或者填自己的 Key。
# 三档次数都可在 .env 里改（LEVEL_PRO_CALLS / LEVEL_INTERNAL_CALLS），改完 restart 生效；
# 免费档沿用 FREE_CALLS_PER_DAY，老配置不用动。
FREE_CALLS_PER_DAY = int(os.environ.get("FREE_CALLS_PER_DAY",
                                        os.environ.get("FREE_CALLS", "10")))
LEVELS = {
    "free":     {"label": "免费", "calls": FREE_CALLS_PER_DAY, "desc": "每天 %d 次" % FREE_CALLS_PER_DAY},
    "pro":      {"label": "进阶", "calls": int(os.environ.get("LEVEL_PRO_CALLS", "50")),
                 "desc": "每天 %s 次" % os.environ.get("LEVEL_PRO_CALLS", "50")},
    "internal": {"label": "内部", "calls": int(os.environ.get("LEVEL_INTERNAL_CALLS", "1000000")),
                 "desc": "不限量"},
}
LEVEL_ORDER = ("free", "pro", "internal")
DAY_TZ_OFFSET = 8 * 3600          # 服务器在境外，按北京时间切天，用户才不会觉得"额度乱跳"
LISTEN_PORT = int(os.environ.get("PORT", "8000"))

# 限流按「真实访客 IP」算，不是按 TCP 对端 —— 见 _client_ip()。
# 可改 .env 里 IP_LIMIT_HOUR / IP_LIMIT_DAY 调整，不用改代码。
IP_LIMIT_HOUR = int(os.environ.get("IP_LIMIT_HOUR", "5"))
IP_LIMIT_DAY = int(os.environ.get("IP_LIMIT_DAY", "20"))


def _client_ip(headers, peer: str) -> str:
    """取真实访客 IP。

    2026-11-04 部署踩到的坑：前面有 caddy 反代（127.0.0.1 → 本进程），
    直接拿 client_address 永远是 127.0.0.1，于是「每 IP 每小时 5 次」变成
    **全网合计每小时 5 次**，第 6 个用户就吃 429；后台看到的 IP 也全是 127.0.0.1。
    服务只监听 127.0.0.1（外网连不到），所以信任反代写的 X-Forwarded-For 是安全的；
    仍然做格式校验，防止伪造出带换行/超长的脏值污染限流桶。
    """
    for name in ("X-Forwarded-For", "X-Real-IP"):
        raw = headers.get(name) or ""
        # XFF 可能是 "客户端, 代理1, 代理2"，第一个才是真实客户端
        for part in raw.split(","):
            cand = part.strip()
            if not cand:
                continue
            try:
                ipaddress.ip_address(cand)
            except ValueError:
                continue
            return cand
    return peer or "?"


# ── IP 归属（离线，2026-11-07）─────────────────────────────────────────────
# 后台看得到注册/登录 IP，但一串数字看不出人在哪。ip2region 的 xdb 自带中文
# 「国家|省份|城市|运营商|国家码」，查询纯本地、零依赖：
#   · 不把用户 IP 发给任何第三方接口（运营数据不外泄，也没有外网超时/限流）；
#   · 服务器是 0.5G 的香港轻量机，不装 pip、不常驻 11MB 内存——每次查询
#     临时开文件、只 seek 读几十字节，靠 _GEO_CACHE 把重复 IP 收敛掉。
# 库文件由 deploy.py 上传到 /opt/hsproxy/proxy/ip2region_v4.xdb。
# 没库 / 内网地址 / 查不到一律返回空串，后台就只显示原来的 IP，绝不因此报错。
IP_GEO_DB = os.environ.get(
    "IP_GEO_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "ip2region_v4.xdb"))
_GEO_CACHE = {}
_GEO_LOCK = threading.Lock()
# 常见国家码 → 中文（库里国外条目的国家名是英文）
_GEO_COUNTRY = {
    "US": "美国", "JP": "日本", "KR": "韩国", "SG": "新加坡",
    "GB": "英国", "DE": "德国", "FR": "法国", "CA": "加拿大",
    "AU": "澳大利亚", "RU": "俄罗斯", "IN": "印度",
    "MY": "马来西亚", "TH": "泰国", "VN": "越南", "PH": "菲律宾",
    "ID": "印度尼西亚", "NL": "荷兰", "BR": "巴西",
}
# 港澳台在 IP 库里可能被列成独立条目，统一按中国地区显示
_GEO_CN_REGION = {
    "HK": "中国香港", "TW": "中国台湾", "MO": "中国澳门",
    "HONG KONG": "中国香港", "TAIWAN": "中国台湾",
    "MACAO": "中国澳门", "MACAU": "中国澳门",
}


def _geo_label(raw: str) -> str:
    """xdb 的 region 串 → 一句能看的中文地址；无效条目返回空。"""
    parts = [p.strip() for p in (raw or "").split("|") if p.strip()]
    if not parts or parts[0] in ("0", "Reserved", "reserved", "未知"):
        return ""
    head = parts[0]
    code = parts[-1].upper() if len(parts) >= 5 else ""
    if code in _GEO_CN_REGION:
        head = _GEO_CN_REGION[code]
    elif head.upper() in _GEO_CN_REGION:
        head = _GEO_CN_REGION[head.upper()]
    elif head == "中国" or code == "CN":
        head = "中国"
    elif code in _GEO_COUNTRY:
        head = _GEO_COUNTRY[code]

    def seg(i: int) -> str:
        return parts[i] if len(parts) > i and parts[i] != "0" else ""

    prov, city, isp = seg(1), seg(2), seg(3)
    if head == "中国":
        # 省市同名（北京市|北京市）只留一个；运营商对判断「是不是家宽」有用，带上
        place = prov + (city if city and city != prov else "")
        return (" ".join(x for x in (place, isp) if x) or head)[:28]
    return " ".join([head] + [x for x in (prov, city) if x][:2])[:28]


def _ip_geo(ip: str) -> str:
    """查一个 IPv4 的归属地。xdb 3.0 / IPv4：256B 头 + 向量索引 + 14B 索引项。"""
    try:
        addr = ipaddress.ip_address(ip)
    except (ValueError, TypeError):
        return ""
    if (addr.version != 4 or addr.is_private or addr.is_loopback
            or addr.is_reserved or addr.is_link_local or addr.is_unspecified):
        return ""
    packed = addr.packed
    if not os.path.isfile(IP_GEO_DB):
        return ""
    try:
        with open(IP_GEO_DB, "rb") as f:
            # 向量索引：按前两个字节直接定位到索引子区间，不用全表二分
            f.seek(256 + (packed[0] * 256 + packed[1]) * 8)
            v = f.read(8)
            if len(v) < 8:
                return ""
            s_ptr = int.from_bytes(v[:4], "little")
            e_ptr = int.from_bytes(v[4:], "little")
            if not s_ptr or not e_ptr:
                return ""
            low, high, d_len, d_ptr = 0, (e_ptr - s_ptr) // 14, 0, 0
            while low <= high:
                mid = (low + high) >> 1
                f.seek(s_ptr + mid * 14)
                b = f.read(14)
                if len(b) < 14:
                    break
                # 索引里的 IP 是小端存放，反序后才是网络字节序
                start, end = b[3::-1], b[7:3:-1]
                if packed < start:
                    high = mid - 1
                elif packed > end:
                    low = mid + 1
                else:
                    d_len = int.from_bytes(b[8:10], "little")
                    d_ptr = int.from_bytes(b[10:14], "little")
                    break
            if not d_len:
                return ""
            f.seek(d_ptr)
            return _geo_label(f.read(d_len).decode("utf-8", "replace"))
    except (OSError, ValueError, UnicodeError):
        return ""


def _geo_of(ip: str) -> str:
    """带缓存的查询：后台一页几百行里 IP 高度重复，别把库查几百遍。"""
    if not ip or not isinstance(ip, str):
        return ""
    with _GEO_LOCK:
        hit = _GEO_CACHE.get(ip)
    if hit is not None:
        return hit
    val = _ip_geo(ip)
    with _GEO_LOCK:
        if len(_GEO_CACHE) > 4096:
            _GEO_CACHE.clear()
        _GEO_CACHE[ip] = val
    return val


def _decorate_geo(obj, depth: int = 0) -> None:
    """给后台响应里所有 ip / last_ip 字段旁边补一个 geo，供页面显示括号。

    放在统一发送出口做，新增列表页也自动生效；递归有深度上限，
    免得客户端上报的嵌套 payload 把响应拖慢。
    """
    if depth > 6:
        return
    if isinstance(obj, dict):
        for key in ("ip", "last_ip"):
            v = obj.get(key)
            if isinstance(v, str) and v:
                obj["geo"] = _geo_of(v)
                break
        for v in list(obj.values()):
            _decorate_geo(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _decorate_geo(v, depth + 1)


_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
# 按列名取值（row["id"]）比数位置好维护。sqlite3.Row 同时支持 row[0]，
# 所以历史代码里那些位置索引不用改。
_conn.row_factory = sqlite3.Row
# 老库里的 tokens / daily_usage 两张表是「匿名设备额度」时代的产物（2026-11-04 前）。
# 现在额度只按账号算（account_daily），这两张表不再建、不再读写——
# 旧库里的残留数据留着不动，不影响任何逻辑。
_conn.execute("""
CREATE TABLE IF NOT EXISTS usage(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    token TEXT NOT NULL,
    ip TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    status INTEGER NOT NULL
)""")
_conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts)")
_conn.commit()

# ── 服务端自身日志（2026-11-05 补）──────────────────────────────────────
# 在这之前服务端是「全静默」：log_message 被覆盖成 pass（连访问日志都没有），
# 兜底 except 只把 `type(exc).__name__` 回给客户端、堆栈就地丢弃 —— 线上炸一个
# 接口等于无痕，事后只能靠用户截图猜。两张表补服务端这一侧：
#   server_error  未捕获异常的堆栈（排查「接口 500」用）
#   server_event  运行事件：启停、慢请求、上游 402/429、管理操作审计
# 写入一律走 _log_*，内部自己吞异常 —— 记日志不能反过来把业务搞挂。
_conn.execute("""
CREATE TABLE IF NOT EXISTS server_error(
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    method   TEXT    NOT NULL DEFAULT '',
    path     TEXT    NOT NULL DEFAULT '',
    ip       TEXT    NOT NULL DEFAULT '',
    ua       TEXT    NOT NULL DEFAULT '',
    status   INTEGER NOT NULL DEFAULT 500,
    exc_type TEXT    NOT NULL DEFAULT '',
    exc_msg  TEXT    NOT NULL DEFAULT '',
    trace    TEXT    NOT NULL DEFAULT ''
)""")
_conn.execute("CREATE INDEX IF NOT EXISTS idx_serr_ts ON server_error(ts)")
_conn.execute("""
CREATE TABLE IF NOT EXISTS server_event(
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     INTEGER NOT NULL,
    kind   TEXT    NOT NULL DEFAULT 'app',
    level  TEXT    NOT NULL DEFAULT 'info',
    action TEXT    NOT NULL DEFAULT '',
    actor  TEXT    NOT NULL DEFAULT '',
    detail TEXT    NOT NULL DEFAULT '',
    ip     TEXT    NOT NULL DEFAULT ''
)""")
_conn.execute("CREATE INDEX IF NOT EXISTS idx_sevt_ts ON server_event(ts)")
_conn.commit()

# 超过这个耗时就记一条 slow 事件。全量记请求会把 0.5G 小机器的 journal 刷爆，
# 只挑「慢」和「错」两种真正需要人看的。
SLOW_MS = int(os.environ.get("SLOW_MS", "1000"))
# 服务端日志保留天数：这类表长得最快，过期的堆栈没有价值。
LOG_KEEP_DAYS = int(os.environ.get("LOG_KEEP_DAYS", "30"))

_hits_hour: dict[str, deque] = defaultdict(deque)
_hits_day: dict[str, deque] = defaultdict(deque)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _check_ip_rate(ip: str) -> None:
    now = time.time()
    for store, limit, span in ((_hits_hour, IP_LIMIT_HOUR, 3600),
                               (_hits_day, IP_LIMIT_DAY, 86400)):
        q = store[ip]
        while q and q[0] < now - span:
            q.popleft()
        if len(q) >= limit:
            raise ApiError(429, "这个网络今天的使用次数太多了，请明天再试")


def _mark_ip(ip: str) -> None:
    now = time.time()
    _hits_hour[ip].append(now)
    _hits_day[ip].append(now)


def _cleanup_hits() -> None:
    while True:
        time.sleep(3600)
        for store in (_hits_hour, _hits_day):
            for ip in [k for k, v in store.items() if not v]:
                store.pop(ip, None)
        # 服务端日志顺手过期：每小时一次，和限流桶清理同一个线程，不额外开线程。
        try:
            cutoff = int(time.time()) - LOG_KEEP_DAYS * 86400
            with _lock:
                _conn.execute("DELETE FROM server_error WHERE ts < ?", (cutoff,))
                _conn.execute("DELETE FROM server_event WHERE ts < ?", (cutoff,))
                _conn.commit()
            for ip in [k for k, ts in _authfail_seen.items() if ts < time.time() - 86400]:
                _authfail_seen.pop(ip, None)
        except Exception:  # noqa: BLE001
            pass


threading.Thread(target=_cleanup_hits, daemon=True).start()


def _log_error(method: str, path: str, ip: str, ua: str, exc: BaseException,
               status: int = 500) -> None:
    """把未捕获异常连同堆栈落库。**调用方在 except 里调**，所以能拿到 format_exc。"""
    try:
        tb = traceback.format_exc()
        if tb.strip() == "NoneType: None":   # 不在 except 上下文里调用时的兜底
            tb = ""
        with _lock:
            _conn.execute(
                "INSERT INTO server_error(ts,method,path,ip,ua,status,exc_type,exc_msg,trace)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (int(time.time()), str(method or "")[:10], str(path or "")[:300],
                 str(ip or "")[:64], str(ua or "")[:200], int(status),
                 type(exc).__name__[:80], str(exc)[:500], tb[-6000:]))
            _conn.commit()
    except Exception:  # noqa: BLE001
        pass    # 记日志失败绝不能反过来影响请求


def _log_event(kind: str, action: str, detail: str = "", level: str = "info",
               actor: str = "", ip: str = "") -> None:
    """记一条运行事件。kind 是分类（boot/slow/upstream/admin/http），action 是具体动作。"""
    try:
        with _lock:
            _conn.execute(
                "INSERT INTO server_event(ts,kind,level,action,actor,detail,ip)"
                " VALUES(?,?,?,?,?,?,?)",
                (int(time.time()), str(kind or "app")[:24], str(level or "info")[:8],
                 str(action or "")[:60], str(actor or "")[:80],
                 str(detail or "")[:1000], str(ip or "")[:64]))
            _conn.commit()
    except Exception:  # noqa: BLE001
        pass


# 管理员密钥错误的去重时间戳：后台接口暴露在公网，扫描器一天能撞几千次，
# 每次都记一条会把日志表刷成噪音（真正要看的「谁在撞」反而被埋掉）。
_authfail_seen: dict[str, float] = {}


def _log_admin_auth_fail(ip: str, path: str) -> None:
    now = time.time()
    if now - _authfail_seen.get(ip, 0.0) < 1800:
        return
    _authfail_seen[ip] = now
    _log_event("admin", "admin.auth_fail", f"密钥错误 {path}"[:1000],
               level="warn", ip=ip)



def _day_key(ts: float | None = None) -> str:
    """按北京时间切天（服务器在境外，用 UTC 切会让用户在晚上 8 点"跨天"）。"""
    return time.strftime("%Y-%m-%d", time.gmtime((ts or time.time()) + DAY_TZ_OFFSET))


def _reset_in() -> int:
    """距下一个北京时间 0 点还有多少秒，前端提示「几点恢复」用。"""
    now = time.time() + DAY_TZ_OFFSET
    return int(86400 - (now % 86400))


def _caller(token: str) -> sqlite3.Row:
    """这个令牌对应哪个账号。**只认账号令牌**。

    2026-11-04 白天为了让「已经装出去的老程序」继续能用，这里留过一条退路：
    查不到账号就当匿名设备、照发每天 10 次。当晚确认「还在测试、没有老客户」，
    退路直接砍掉——它的代价是一个白送的后门：随便编一串 uuid 就是一台「新设备」，
    每天又是 10 次，而且后台根本不知道是谁在用。现在不登录就没有 AI。
    """
    if not token:
        raise ApiError(401, "还没有登录，请先登录再使用 AI 功能")
    acc = _account_by_token(token)
    if not acc:
        raise ApiError(401, "登录已失效（可能在别处改过密码或被踢下线），请重新登录")
    if acc["status"] == "banned":
        raise ApiError(403, "账号已被封禁：" + (acc["ban_reason"] or "违反使用规则"))
    return acc


def handle_quota(token: str) -> dict:
    acc = _caller(token)
    day = _day_key()
    used, bonus = _acc_calls(acc["id"], day)
    level = _level_of(acc)
    limit = _acc_limit(level, bonus)
    return {
        "remaining": max(0, limit - used),
        "total": limit,
        "used": used,
        "scope": "day",                 # 前端据此把文案写成「今天还剩 N 次」
        "day": day,
        "reset_in": _reset_in(),
        "scope_kind": "account",
        "who": acc["username"],
        "level": level, "level_label": LEVELS[level]["label"],
    }


def handle_chat(token: str, ip: str, body: dict) -> dict:
    _check_ip_rate(ip)
    if body.get("stream"):
        raise ApiError(400, "不支持流式调用")
    model = body.get("model") or "qwen-plus"

    acc = _caller(token)
    day = _day_key()
    used, bonus = _acc_calls(acc["id"], day)
    level = _level_of(acc)
    limit = _acc_limit(level, bonus)

    if used >= limit:
        raise ApiError(402, "今天的次数已用完（%s等级，每天 %d 次，北京时间 0 点自动恢复）；"
                            "也可以联系管理员调整等级，或在设置里填自己的 Key 继续用"
                            % (LEVELS[level]["label"], _level_calls(level)))

    _mark_ip(ip)
    t0 = time.time()
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        DASHSCOPE_URL, data=payload, method="POST",
        headers={"Authorization": f"Bearer {DASHSCOPE_KEY}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            status = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ApiError(502, "AI 服务暂时不可用，请稍后再试")

    ok = status == 200
    try:
        out = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        out = {}
    usage = out.get("usage") or {} if ok else {}

    if ok:
        _acc_bump(acc["id"])            # 只有真跑成功才扣次数，失败不扣
    u2, b2 = _acc_calls(acc["id"], day)
    remaining = max(0, _acc_limit(level, b2) - u2)

    with _lock:
        _conn.execute(
            "INSERT INTO usage(ts,token,ip,model,prompt_tokens,completion_tokens,status)"
            " VALUES(?,?,?,?,?,?,?)",
            # 记 `@用户名`：后台一眼看出是哪个账号花的，能直接对上账号表。
            (int(time.time()), "@" + acc["username"], ip, model,
             usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), status))
        _conn.commit()

    if not ok:
        msg = "AI 调用失败，请稍后再试"
        if isinstance(out, dict):
            err = out.get("error")
            if isinstance(err, dict) and err.get("message"):
                msg = str(err["message"])[:200]
        # 上游 402（欠费）/ 429（限流）/ 401（Key 失效）必须留痕：
        # 这几种以前只会体现在 usage 表的一个状态码上，没人盯着就发现不了
        # 「Key 欠费了，全站都在失败」。
        _log_event("upstream", f"upstream.{status}", msg, level="error",
                   actor="@" + acc["username"], ip=ip)
        return {"_status": 502, "_body": {"error": {"message": msg}}}

    out.setdefault("usage", {})
    out["usage"]["remaining"] = remaining
    out["_elapsed"] = round(time.time() - t0, 2)
    return {"_status": 200, "_body": out}


def _admin(auth: str) -> None:
    if not ADMIN_KEY or auth != ADMIN_KEY:
        raise ApiError(401, "管理员密钥错误")


def handle_stats(auth: str) -> dict:
    """概览用的几个数：全部按**账号**口径（设备口径已随匿名令牌一起删除）。"""
    _admin(auth)
    day_ago = int(time.time()) - 86400
    cur = _conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0)"
        " FROM usage WHERE ts>?", (day_ago,))
    calls24, ptok, ctok = cur.fetchone()
    acc_total = _conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    acc_banned = _conn.execute("SELECT COUNT(*) FROM accounts WHERE status='banned'").fetchone()[0]
    acc_today = _conn.execute("SELECT COUNT(*) FROM account_daily WHERE day=? AND calls>0",
                              (_day_key(),)).fetchone()[0]
    acc_calls_today = _conn.execute(
        "SELECT COALESCE(SUM(calls),0) FROM account_daily WHERE day=?", (_day_key(),)).fetchone()[0]
    # 「还有次数的账号」按各自等级的上限分开算，不能再用一个固定 10 次——
    # 进阶 / 内部账号用超了 10 次其实还有额度，统一口径会把他们误判成已用完。
    acc_left_today = _conn.execute(
        "SELECT COUNT(*) FROM account_daily d JOIN accounts a ON a.id=d.account_id"
        " WHERE d.day=? AND d.calls < (CASE COALESCE(a.level,'free')"
        " WHEN 'pro' THEN ? WHEN 'internal' THEN ? ELSE ? END) + d.bonus",
        (_day_key(), LEVELS["pro"]["calls"], LEVELS["internal"]["calls"],
         LEVELS["free"]["calls"])).fetchone()[0]
    level_counts = {k: 0 for k in LEVEL_ORDER}
    for lr in _conn.execute(
            "SELECT COALESCE(level,'free') lv, COUNT(*) n FROM accounts GROUP BY lv"):
        level_counts[lr[0] if lr[0] in level_counts else "free"] += lr[1]
    rows = _conn.execute(
        "SELECT ts,token,ip,model,prompt_tokens,completion_tokens,status FROM usage"
        " ORDER BY id DESC LIMIT 50").fetchall()
    return {
        "accounts_total": acc_total, "accounts_banned": acc_banned,
        "accounts_today": acc_today, "accounts_left_today": acc_left_today,
        "calls_today": int(acc_calls_today or 0), "day": _day_key(),
        "free_calls_per_day": FREE_CALLS_PER_DAY,
        "level_counts": level_counts, "levels": _levels_view(),
        "calls_24h": calls24, "prompt_tokens_24h": ptok, "completion_tokens_24h": ctok,
        "recent": [{"ts": r[0], "token": r[1], "ip": r[2], "model": r[3],
                    "ptok": r[4], "ctok": r[5], "status": r[6]} for r in rows],
    }


# ── 账号体系（2026-11-04）────────────────────────────────────────────────
# 为什么账号必须放在服务器：客户端本地也有一张 users 表，但它是**每台电脑各自一份**，
# A 电脑注册的账号 B 电脑根本不认识；而且「第一个注册的人自动成为管理员」——
# 等于每个用户在自己机器上都是管理员，运营方既看不到是谁在用，也封不了任何人。
# 现在账号在这里，客户端登录向这里要令牌，额度按账号算，后台能看到具体的人。
#
# 断网怎么办：客户端会把密码副本留在本机，服务器连不通时回退本地校验，
# 保证「没网也能剪片」，只是调不了 AI（本来就要网）。
_conn.execute("""
CREATE TABLE IF NOT EXISTS accounts(
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    pwd_hash      TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'active',
    level         TEXT    NOT NULL DEFAULT 'free',   -- free / pro / internal
    note          TEXT    NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL,
    last_login_at INTEGER NOT NULL DEFAULT 0,
    last_ip       TEXT    NOT NULL DEFAULT '',
    device        TEXT    NOT NULL DEFAULT '',
    device_at     INTEGER NOT NULL DEFAULT 0,
    ban_reason    TEXT    NOT NULL DEFAULT '',
    token_version INTEGER NOT NULL DEFAULT 0
)""")
# 账号令牌：随机串，服务端存原文（不是密码，泄露也只影响这一条会话）。
# 没有过期时间——客户端是长期使用的桌面工具，每次开机重新登录会把人逼疯；
# 要让它失效就在后台「踢下线」/ 封禁，直接删这一行。
_conn.execute("""
CREATE TABLE IF NOT EXISTS sessions(
    token      TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    last_used  INTEGER NOT NULL,
    ip         TEXT NOT NULL DEFAULT '',
    ua         TEXT NOT NULL DEFAULT ''
)""")
_conn.execute("CREATE INDEX IF NOT EXISTS idx_sess_acc ON sessions(account_id)")
_conn.execute("""
CREATE TABLE IF NOT EXISTS account_daily(
    account_id INTEGER NOT NULL,
    day        TEXT    NOT NULL,
    calls      INTEGER NOT NULL DEFAULT 0,
    bonus      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(account_id, day)
)""")
# 老库升级（2026-11-07）：线上 accounts 表早就建好了，CREATE TABLE IF NOT EXISTS
# 不会给它补列。缺 level 就加一列，已有账号自动落到「免费」档，不用手工刷数据。
_acc_cols = {r[1] for r in _conn.execute("PRAGMA table_info(accounts)")}
if "level" not in _acc_cols:
    _conn.execute("ALTER TABLE accounts ADD COLUMN level TEXT NOT NULL DEFAULT 'free'")
_conn.commit()

PWD_MIN, PWD_MAX = 8, 72
_PWD_WEAK = {
    "12345678", "123456789", "1234567890", "password", "password1", "qwerty123",
    "abc123456", "a1234567", "11111111", "00000000", "88888888", "admin123",
    "admin123456", "iloveyou", "123123123", "1qaz2wsx", "qwertyui",
    "woaini1314", "5201314520", "asd123456",
}
# 注册限流：同一 IP 每小时最多建几个账号。不加这条，换个号就能无限领每日 10 次。
REG_PER_IP_HOUR = int(os.environ.get("REG_PER_IP_HOUR", "5"))
_reg_hits: dict[str, deque] = defaultdict(deque)


def pwd_error(pwd: str) -> str:
    """和客户端 `security.check_password_strength` 同一套规则，两边不许漂。"""
    if not isinstance(pwd, str):
        return "密码格式不对"
    n = len(pwd.encode("utf-8"))
    if n < PWD_MIN:
        return f"密码至少 {PWD_MIN} 位"
    if n > PWD_MAX:
        return f"密码最多 {PWD_MAX} 字节"
    if pwd.strip() != pwd:
        return "密码首尾不能是空格"
    if pwd.lower() in _PWD_WEAK:
        return "这个密码太常见了，换一个"
    if pwd.isdigit():
        return "密码不能是纯数字"
    if len(set(pwd)) == 1:
        return "密码不能是同一个字符重复"
    return None


def _hash_pwd(pwd: str) -> str:
    """PBKDF2-HMAC-SHA256。服务器上不能装 bcrypt（纯标准库部署），
    PBKDF2 是标准库里唯一像样的选择，26 万轮约 0.1 秒，够挡在线爆破。"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt, 260000)
    return f"pbkdf2_sha256$260000${salt.hex()}${dk.hex()}"


def _check_pwd(pwd: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, want = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), want)
    except (ValueError, TypeError):
        return False


def _account_by_name(name: str):
    return _conn.execute("SELECT * FROM accounts WHERE username = ? COLLATE NOCASE",
                         (name,)).fetchone()


def _account_by_token(token: str):
    """令牌 → 账号。同时刷新 last_used，后台能看出谁最近还在用。"""
    if not token:
        return None
    row = _conn.execute(
        "SELECT a.* FROM sessions s JOIN accounts a ON a.id = s.account_id"
        " WHERE s.token = ?", (token,)).fetchone()
    if row:
        with _lock:
            _conn.execute("UPDATE sessions SET last_used = ? WHERE token = ?",
                          (int(time.time()), token))
            _conn.commit()
    return row


def _norm_level(level) -> str:
    """脏值一律当免费档，绝不因为库里一个拼错的等级把人放进不限量。"""
    lv = (str(level or "")).strip()
    return lv if lv in LEVELS else "free"


def _level_of(row) -> str:
    try:
        return _norm_level(row["level"])
    except (IndexError, KeyError, TypeError):
        return "free"          # 极老的库还没加列时兜底


def _level_calls(level) -> int:
    return LEVELS[_norm_level(level)]["calls"]


def _acc_limit(level, bonus: int = 0) -> int:
    """当日总上限 = 等级基础额度 + 后台补的次数（补次数只对今天有效）。"""
    return _level_calls(level) + max(0, int(bonus or 0))


def _levels_view() -> list[dict]:
    """给后台渲染等级下拉 / 说明用，档位额度只有服务端一处定义。"""
    return [{"key": k, "label": LEVELS[k]["label"], "calls": LEVELS[k]["calls"],
             "desc": LEVELS[k]["desc"], "unlimited": LEVELS[k]["calls"] >= 100000}
            for k in LEVEL_ORDER]


def _acc_view(row) -> dict:
    level = _level_of(row)
    return {
        "id": row["id"], "username": row["username"], "status": row["status"],
        "level": level, "level_label": LEVELS[level]["label"],
        "level_calls": _level_calls(level),
        "note": row["note"], "created_at": row["created_at"],
        "last_login_at": row["last_login_at"], "last_ip": row["last_ip"],
        "device": row["device"], "device_at": row["device_at"],
        "ban_reason": row["ban_reason"], "token_version": row["token_version"],
    }


def _acc_calls(acc_id: int, day: str) -> tuple[int, int]:
    row = _conn.execute("SELECT calls, bonus FROM account_daily WHERE account_id=? AND day=?",
                        (acc_id, day)).fetchone()
    return (int(row[0]), int(row[1])) if row else (0, 0)


def _acc_token_new(acc_id: int, ip: str, ua: str) -> str:
    tok = secrets.token_urlsafe(32)
    with _lock:
        _conn.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)",
                      (tok, acc_id, int(time.time()), int(time.time()), ip, ua[:200]))
        _conn.commit()
    return tok


def handle_register(ip: str, ua: str, body: dict) -> dict:
    """注册。**第一个注册的账号不是管理员**——管理权限在运营方手里，
    用户账号只有一个身份：普通用户。否则又回到「人人都是管理员」。"""
    name = (body.get("username") or "").strip()
    pwd = body.get("password") or ""
    device = (body.get("device") or "").strip()[:64]

    if not (3 <= len(name) <= 20):
        raise ApiError(400, "用户名 3-20 位")
    if not re.match(r"^[\w\u4e00-\u9fa5.\-]+$", name):
        raise ApiError(400, "用户名只能用中文、字母、数字、下划线、点、横线")
    err = pwd_error(pwd)
    if err:
        raise ApiError(400, err)

    now = time.time()
    q = _reg_hits[ip]
    while q and q[0] < now - 3600:
        q.popleft()
    if len(q) >= REG_PER_IP_HOUR:
        raise ApiError(429, "这个网络注册的账号太多了，请稍后再试")
    if _account_by_name(name):
        raise ApiError(409, "这个用户名已被占用")

    with _lock:
        try:
            cur = _conn.execute(
                "INSERT INTO accounts(username,pwd_hash,created_at,last_login_at,last_ip,device,device_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (name, _hash_pwd(pwd), int(now), int(now), ip, device, int(now) if device else 0))
            _conn.commit()
        except sqlite3.IntegrityError:
            raise ApiError(409, "这个用户名已被占用")
        acc_id = cur.lastrowid
    q.append(now)
    tok = _acc_token_new(acc_id, ip, ua)
    row = _conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
    return {"token": tok, "account": _acc_view(row), "created": True}


def handle_login(ip: str, ua: str, body: dict) -> dict:
    """登录。失败一律回「用户名或密码不对」，不给账号枚举的口子。

    设备绑定：一个账号默认只认一台电脑（`device` 由客户端首次登录时上报）。
    换机器会拒绝，要换让运营在后台「解绑」。这是防一个号传遍全公司的唯一手段。
    """
    name = (body.get("username") or "").strip()
    pwd = body.get("password") or ""
    device = (body.get("device") or "").strip()[:64]
    if not name or not pwd:
        raise ApiError(400, "请输入用户名和密码")

    row = _account_by_name(name)
    if not row or not _check_pwd(pwd, row["pwd_hash"]):
        raise ApiError(401, "用户名或密码不对")
    if row["status"] == "banned":
        raise ApiError(403, "账号已被封禁：" + (row["ban_reason"] or "违反使用规则"))

    bound = (row["device"] or "").strip()
    if bound and device and bound != device:
        raise ApiError(403, "这个账号已在另一台电脑登录，换电脑请先让管理员解绑")

    with _lock:
        _conn.execute("UPDATE accounts SET last_login_at=?, last_ip=?, device=?,"
                      " device_at=CASE WHEN device='' THEN ? ELSE device_at END WHERE id=?",
                      (int(time.time()), ip, device or bound,
                       int(time.time()), row["id"]))
        _conn.commit()
    tok = _acc_token_new(row["id"], ip, ua)
    fresh = _conn.execute("SELECT * FROM accounts WHERE id=?", (row["id"],)).fetchone()
    return {"token": tok, "account": _acc_view(fresh)}


def handle_account_me(token: str) -> dict:
    row = _account_by_token(token)
    if not row:
        raise ApiError(401, "登录状态已失效，请重新登录")
    if row["status"] == "banned":
        raise ApiError(403, "账号已被封禁：" + (row["ban_reason"] or "违反使用规则"))
    day = _day_key()
    used, bonus = _acc_calls(row["id"], day)
    limit = _acc_limit(_level_of(row), bonus)
    return {"account": _acc_view(row),
            "quota": {"remaining": max(0, limit - used), "total": limit,
                      "used": used, "bonus": bonus, "day": day,
                      "scope": "day", "reset_in": _reset_in()}}


def handle_change_pwd(token: str, ip: str, ua: str, body: dict) -> dict:
    """用户自己改密码。

    改完把**所有旧会话删掉**再发一张新的：别的电脑上如果还登着，必须重新登录。
    只删不发的做法会把自己也踢出去，用户下次打开就得重新登录，体验很差。
    """
    row = _account_by_token(token)
    if not row:
        raise ApiError(401, "登录状态已失效，请重新登录")
    old = body.get("old") or ""
    new = body.get("new") or ""
    if not _check_pwd(old, row["pwd_hash"]):
        raise ApiError(400, "原密码不对")
    err = pwd_error(new)
    if err:
        raise ApiError(400, err)
    if old == new:
        raise ApiError(400, "新密码不能和原密码一样")
    with _lock:
        _conn.execute("UPDATE accounts SET pwd_hash=?, token_version=token_version+1"
                      " WHERE id=?", (_hash_pwd(new), row["id"]))
        _conn.execute("DELETE FROM sessions WHERE account_id=?", (row["id"],))
        _conn.commit()
    return {"ok": True, "token": _acc_token_new(row["id"], ip, ua)}


def _acc_bump(acc_id: int) -> None:
    with _lock:
        _conn.execute(
            "INSERT INTO account_daily(account_id,day,calls) VALUES(?,?,1) "
            "ON CONFLICT(account_id,day) DO UPDATE SET calls=calls+1",
            (acc_id, _day_key()))
        _conn.commit()


def handle_accounts(auth: str, qs: dict) -> dict:
    """后台账号列表：谁在用、今天用了几次、绑在哪台机器。"""
    _admin(auth)
    day = _day_key()
    q = (qs.get("q") or [""])[0].strip()
    where, args = ["1=1"], []
    if q:
        where.append("(a.username LIKE ? OR a.note LIKE ? OR a.last_ip LIKE ?)")
        like = "%" + q + "%"
        args += [like, like, like]
    st = (qs.get("status") or [""])[0]
    if st in ("active", "banned"):
        where.append("a.status = ?")
        args.append(st)
    lv = (qs.get("level") or [""])[0]
    if lv in LEVELS:
        # COALESCE 兜住加列前遗留的 NULL，别让老账号在筛选里凭空消失
        where.append("COALESCE(a.level,'free') = ?")
        args.append(lv)
    sql = " AND ".join(where)
    rows = _conn.execute(
        f"""SELECT a.*,
                   COALESCE(d.calls,0) calls, COALESCE(d.bonus,0) bonus,
                   (SELECT COUNT(*) FROM sessions s WHERE s.account_id=a.id) sessions,
                   (SELECT MAX(last_used) FROM sessions s WHERE s.account_id=a.id) seen
              FROM accounts a
              LEFT JOIN account_daily d ON d.account_id=a.id AND d.day=?
             WHERE {sql} ORDER BY a.id DESC LIMIT 500""", [day] + args).fetchall()
    items = []
    for r in rows:
        v = _acc_view(r)
        cap = _acc_limit(v["level"], r["bonus"] or 0)
        v.update({"calls": int(r["calls"] or 0), "bonus": int(r["bonus"] or 0),
                  "limit": cap, "remaining": max(0, cap - int(r["calls"] or 0)),
                  "sessions": int(r["sessions"] or 0), "last_seen": int(r["seen"] or 0)})
        items.append(v)
    counts = {
        "total": _conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0],
        "banned": _conn.execute("SELECT COUNT(*) FROM accounts WHERE status='banned'").fetchone()[0],
        "today": _conn.execute("SELECT COUNT(*) FROM account_daily WHERE day=? AND calls>0",
                               (day,)).fetchone()[0],
        # 全站各档人数（不受当前筛选影响），账号页顶部的分布小标签直接用这个
        "levels": {k: 0 for k in LEVEL_ORDER},
    }
    for lr in _conn.execute(
            "SELECT COALESCE(level,'free') lv, COUNT(*) n FROM accounts GROUP BY lv"):
        counts["levels"][lr[0] if lr[0] in counts["levels"] else "free"] += lr[1]
    return {"day": day, "free_calls_per_day": FREE_CALLS_PER_DAY,
            "levels": _levels_view(), "items": items, "counts": counts}


def handle_account_action(auth: str, body: dict, ip: str = "") -> dict:
    """后台对单个账号的动作。集中成一个接口，省得前端记一堆路径。

    ban / unban / kick / unbind / grant / reset_pwd / set_level / note

    每次动作都写一条 server_event 审计：以前「谁封了谁、谁补了次数」在后台
    一点痕迹都没有，账号异常时无法回溯是谁做的（2026-11-05 补）。
    """
    _admin(auth)
    try:
        acc_id = int(body.get("id") or 0)
    except (TypeError, ValueError):
        acc_id = 0
    action = (body.get("action") or "").strip()
    row = _conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
    if not row:
        raise ApiError(404, "账号不存在")
    now = int(time.time())
    audit = ""          # 审计摘要：密码这类敏感值绝不进日志

    if action == "ban":
        reason = (body.get("reason") or "").strip()[:200] or "违反使用规则"
        with _lock:
            _conn.execute("UPDATE accounts SET status='banned', ban_reason=?,"
                          " token_version=token_version+1 WHERE id=?", (reason, acc_id))
            _conn.execute("DELETE FROM sessions WHERE account_id=?", (acc_id,))
            _conn.commit()
        audit = "理由：" + reason
    elif action == "unban":
        with _lock:
            _conn.execute("UPDATE accounts SET status='active', ban_reason='',"
                          " token_version=token_version+1 WHERE id=?", (acc_id,))
            _conn.commit()
    elif action == "kick":
        with _lock:
            _conn.execute("DELETE FROM sessions WHERE account_id=?", (acc_id,))
            _conn.commit()
    elif action == "unbind":
        with _lock:
            _conn.execute("UPDATE accounts SET device='', device_at=0 WHERE id=?", (acc_id,))
            _conn.commit()
    elif action == "note":
        note = (body.get("note") or "").strip()[:200]
        with _lock:
            _conn.execute("UPDATE accounts SET note=? WHERE id=?", (note, acc_id))
            _conn.commit()
        audit = "备注：" + note
    elif action == "grant":
        try:
            add = int(body.get("add") or 0)
        except (TypeError, ValueError):
            add = 0
        if add <= 0:
            raise ApiError(400, "补的次数要是正整数")
        with _lock:
            _conn.execute(
                "INSERT INTO account_daily(account_id,day,calls,bonus) VALUES(?,?,0,?) "
                "ON CONFLICT(account_id,day) DO UPDATE SET bonus=bonus+?",
                (acc_id, _day_key(), add, add))
            _conn.commit()
        audit = "补 %d 次" % add
    elif action == "set_level":
        lv = (body.get("level") or "").strip()
        if lv not in LEVELS:
            raise ApiError(400, "等级只能是 free / pro / internal")
        old = _level_of(row)
        with _lock:
            _conn.execute("UPDATE accounts SET level=? WHERE id=?", (lv, acc_id))
            _conn.commit()
        # 不踢会话、不重签令牌：额度每次请求都实时查库按等级算，改完下一次调用就生效，
        # 用户完全无感。等级和「补次数」是两条独立的额度来源，互不覆盖。
        audit = "等级：%s → %s（%s）" % (LEVELS[old]["label"], LEVELS[lv]["label"],
                                      LEVELS[lv]["desc"])
    elif action == "reset_pwd":
        pwd = body.get("password") or ""
        err = pwd_error(pwd)
        if err:
            raise ApiError(400, err)
        with _lock:
            _conn.execute("UPDATE accounts SET pwd_hash=?, token_version=token_version+1"
                          " WHERE id=?", (_hash_pwd(pwd), acc_id))
            _conn.execute("DELETE FROM sessions WHERE account_id=?", (acc_id,))
            _conn.commit()
        audit = "密码已重置，会话全部踢下线"
    else:
        raise ApiError(400, "未知操作")

    _to_level = (body.get("level") or "").strip()
    _sensitive = action in ("ban", "reset_pwd") or (
        action == "set_level" and "internal" in (_to_level, _level_of(row)))
    _log_event("admin", "account." + action,
               ("%s(#%d) %s" % (row["username"], acc_id, audit)).strip(),
               level="warn" if _sensitive else "info",
               actor="admin", ip=ip)

    fresh = _conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
    day = _day_key()
    used, bonus = _acc_calls(acc_id, day)
    limit = _acc_limit(_level_of(fresh), bonus)
    return {"ok": True, "account": _acc_view(fresh),
            "quota": {"remaining": max(0, limit - used), "total": limit,
                      "used": used, "bonus": bonus, "day": day}}


# ── 运营后台（2026-11-04）────────────────────────────────────────────────
# 之前只有 /admin/stats 三个 JSON 接口，运营要看数据得自己 curl；发次数要拼命令。
# 现在补一个网页后台：GET /admin 返回 admin.html（只是一个空壳，页面自己拿密钥登录），
# 数据接口一律要求 X-Admin-Key，和原来的 /admin/stats 同一个密钥。

def handle_series(auth: str, qs: dict) -> dict:
    """近 N 天趋势：每天总调用次数 + 当天有调用的账号数。没有数据的日子显式补 0。"""
    _admin(auth)
    try:
        days = max(3, min(60, int((qs.get("days") or ["14"])[0])))
    except ValueError:
        days = 14
    now = time.time() + DAY_TZ_OFFSET
    labels = [time.strftime("%Y-%m-%d", time.gmtime(now - i * 86400))
              for i in range(days - 1, -1, -1)]
    got = {}
    for d, calls, accs in _conn.execute(
            "SELECT day, SUM(calls), COUNT(*) FROM account_daily WHERE day >= ?"
            " GROUP BY day", (labels[0],)):
        got[d] = (int(calls or 0), int(accs or 0))
    return {"days": days,
            "items": [{"day": d, "calls": got.get(d, (0, 0))[0],
                       "accounts": got.get(d, (0, 0))[1]} for d in labels]}


def handle_release_get(auth: str) -> dict:
    """看当前版本闸配置（客户端问 /api/version 读的就是这个文件）。"""
    _admin(auth)
    return handle_version()


def handle_release_save(auth: str, body: dict, ip: str = "") -> dict:
    """改版本闸配置。

    `min_supported` 填了版号 = 低于它的客户端会被锁在升级页（重启立即生效，
    其余客户端 6 小时内生效）。填 `""` = 不强制任何人。写之前先校验格式：
    写成 `0.1` 这种两段式客户端也认，但写错成 `v0.1.0` 会导致所有版本比较错乱，
    所以只接受数字和点。
    """
    _admin(auth)
    ver = str(body.get("version") or "").strip()
    minv = str(body.get("min_supported") or "").strip()
    url = str(body.get("url") or "").strip()
    note = str(body.get("note") or "").strip()[:500]

    def _ok_ver(v: str) -> bool:
        if not v:
            return True
        parts = v.split(".")
        return 1 <= len(parts) <= 4 and all(p.isdigit() for p in parts)

    if not _ok_ver(ver) or not _ok_ver(minv):
        raise ApiError(400, "版本号只能写数字和点，例如 0.2.0")
    if url and not (url.startswith("http://") or url.startswith("https://")):
        raise ApiError(400, "下载地址必须以 http:// 或 https:// 开头")

    # 读旧文件再合并：`_说明` 这类给人看的字段不该被后台操作抹掉
    try:
        with open(RELEASE_FILE, encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:  # noqa: BLE001
        data = {}
    data["version"] = ver
    data["min_supported"] = minv
    data["url"] = url
    data["note"] = note
    tmp = RELEASE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, RELEASE_FILE)
    _release_cache.update({"at": 0.0, "data": None})   # 立刻让 /api/version 读到新值
    _log_event("admin", "release.save",
               "version=%s min=%s url=%s" % (ver or "-", minv or "-（不强制）", url or "-"),
               level="warn" if minv else "info", actor="admin", ip=ip)
    return {"ok": True, **handle_version()}


def _admin_html() -> bytes:
    """把后台页面读成 bytes。文件缺失时回一句能看懂的错误，别抛栈。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin.html")
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return ("<!doctype html><meta charset=utf-8>"
                "<h1>后台页面缺失</h1><p>服务器上找不到 admin.html，"
                "请重新执行 deploy.py 上传。</p>").encode("utf-8")


# ── 版本闸 + 日志上报（2026-11-04）───────────────────────────────────────
# 这两件事只有在「程序发到用户电脑上」时才需要：
#   · 版本闸：本地指纹比的是「页面 vs 本机后端」，exe 里两者永远同版，
#     必须由服务器说「低于 X 的版本不许用」才能实现强制更新；
#   · 日志上报：埋点写的是用户本机的 sqlite，不推上来你就一条都看不到。
RELEASE_FILE = os.environ.get("RELEASE_FILE", "/opt/hsproxy/release.json")
_release_cache = {"at": 0.0, "data": None}


def handle_version() -> dict:
    """告诉客户端「最新版是多少、最低能用到哪个版本」。

    读 release.json（运维改这个文件就能推一次强制更新，不用重启服务）：
        {"version": "0.2.0", "min_supported": "0.2.0",
         "url": "https://.../钩子工坊.zip", "note": "修了什么"}
    `min_supported` 留空 = 不强制任何人，所有版本都放行。
    """
    now = time.time()
    if _release_cache["data"] is not None and now - _release_cache["at"] < 60:
        return _release_cache["data"]
    data = {}
    try:
        with open(RELEASE_FILE, encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:  # noqa: BLE001
        data = {}
    out = {
        "version": str(data.get("version") or os.environ.get("LATEST_VERSION") or ""),
        "min_supported": str(data.get("min_supported") or ""),
        "url": str(data.get("url") or ""),
        "note": str(data.get("note") or ""),
        "ts": int(now),
    }
    _release_cache.update({"at": now, "data": out})
    return out


_conn.execute("""
CREATE TABLE IF NOT EXISTS client_audit(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at INTEGER NOT NULL,
    device      TEXT    NOT NULL,
    app         TEXT    NOT NULL DEFAULT '',
    os          TEXT    NOT NULL DEFAULT '',
    ts          TEXT    NOT NULL DEFAULT '',
    username    TEXT    NOT NULL DEFAULT '',
    role        TEXT    NOT NULL DEFAULT '',
    method      TEXT    NOT NULL DEFAULT '',
    path        TEXT    NOT NULL DEFAULT '',
    route       TEXT    NOT NULL DEFAULT '',
    query       TEXT    NOT NULL DEFAULT '',
    status      INTEGER NOT NULL DEFAULT 0,
    ms          INTEGER NOT NULL DEFAULT 0,
    ip          TEXT    NOT NULL DEFAULT '',
    ua          TEXT    NOT NULL DEFAULT '',
    action      TEXT    NOT NULL DEFAULT '',
    target      TEXT    NOT NULL DEFAULT '',
    payload     TEXT    NOT NULL DEFAULT '',
    result      TEXT    NOT NULL DEFAULT '',
    message     TEXT    NOT NULL DEFAULT '',
    rid         TEXT    NOT NULL DEFAULT '',
    UNIQUE(device, rid)
)""")
_conn.execute("CREATE INDEX IF NOT EXISTS idx_ca_ts ON client_audit(ts DESC)")
_conn.commit()

INGEST_MAX = 200        # 单次最多几条：客户端一批 100，这里留一倍余量

# ── 路由 → 动作名兜底（2026-11-06）─────────────────────────────────────────
# 客户端 audit.py 的 ACTION_RULES 才是正本，这里是它在服务端的镜像。为什么要有：
#   ① **老客户端不会上报动作名**。0.1.4 及以前的版本压根没给这些接口命名，
#      上报上来 action 是空的，后台就只能显示原始路由 + 「—」，用户看不懂
#      「/api/merge/preview 是什么」。改客户端代码要等用户升级才生效。
#   ② **已经入库的历史行**同样是空的，回填一遍就全有名字了（见 _backfill_actions）。
# 维护约定：客户端 ACTION_RULES 加了新动作，这里必须同步加一行，
# 否则那一版之前的用户日志仍然显示「—」。
_ROUTE_ACTION = {
    ("POST", "/api/merge/preview"): "merge.preview",
    ("POST", "/api/merge/start"): "merge.start",
    ("POST", "/api/merge/cancel"): "merge.cancel",
    ("POST", "/api/merge/open"): "merge.open",
    ("POST", "/api/projects/{id}/transcribe-ep"): "project.transcribe_ep",
    ("PUT", "/api/projects/{id}/lines"): "project.lines_edit",
    ("PUT", "/api/projects/{id}/plans-select"): "plan.select",
    ("PUT", "/api/projects/{id}/plan-hook"): "plan.hook",
    ("PUT", "/api/projects/{id}/plan-fx"): "plan.fx",
    ("PUT", "/api/projects/{id}/fx-default"): "fx.default",
    # 最前面一步：生成去字幕版素材（2026-11-11）
    ("PUT", "/api/projects/{id}/nosub-config"): "nosub.config",
    ("POST", "/api/projects/{id}/nosub"): "nosub.generate",
    ("POST", "/api/projects/{id}/nosub/open"): "nosub.open",
    ("POST", "/api/projects/{id}/nosub/preview"): "nosub.preview",
    # 批量定时发布（2026-11-13）：老客户端不上报动作名，靠这些兜底。
    # 前缀统一 hookcut.publish，后台按前缀筛一次就能看全。
    ("POST", "/api/publish/add"): "hookcut.publish.add",
    ("PUT", "/api/publish/job/{id}"): "hookcut.publish.edit",
    ("DELETE", "/api/publish/job/{id}"): "hookcut.publish.del",
    ("POST", "/api/publish/plan"): "hookcut.publish.plan",
    ("POST", "/api/publish/start"): "hookcut.publish",
    ("POST", "/api/publish/stop"): "hookcut.publish.stop",
    ("POST", "/api/publish/job/{id}/retry"): "hookcut.publish.retry",
    ("POST", "/api/publish/job/{id}/mark"): "hookcut.publish",
    ("POST", "/api/publish/clear-draft"): "hookcut.publish.clear",
    ("POST", "/api/publish/apply"): "hookcut.publish.set",
    # 数据与体检（2026-11-13）：用户主动去号里读一次，有动作含义。
    # 读缓存的 GET 不记日志（纯读本地文件）。
    ("POST", "/api/publish/stat/sample"): "hookcut.publish.stat",
    ("POST", "/api/publish/diag/{id}"): "hookcut.publish.diag",
    ("POST", "/api/publish/monitor"): "hookcut.publish.monitor",
    ("POST", "/api/publish/accounts"): "hookcut.publish",
    ("DELETE", "/api/publish/accounts/{id}"): "hookcut.publish",
    # 扫码登录：出二维码有动作含义；login-state 是 2 秒轮询，不记。
    ("POST", "/api/publish/accounts/{id}/qr"): "hookcut.publish",
    ("PUT", "/api/publish/accounts/{id}"): "hookcut.publish.rename",
    ("POST", "/api/projects/{id}/task/cancel"): "task.cancel",
    ("PUT", "/api/projects/{id}/edl"): "edl.save",
    # 单集钩子前置（2026-11-06）：老客户端不上报动作名，靠这条兜底。
    # 前缀是 hookcut. 不是 plan.hookcut.：后台筛选按前缀匹配，会被 plan.hook 吃掉
    ("POST", "/api/projects/{id}/hookcut-plan"): "hookcut.generate",
    ("POST", "/api/projects/{id}/open-video"): "video.open",
    ("POST", "/api/projects/{id}/shots-layout"): "shots.layout",
    ("POST", "/api/projects/{id}/open-folder"): "folder.open",
    ("POST", "/api/llm-test"): "config.test_model",
    ("GET", "/api/admin/logs/export"): "admin.logs.export",
    ("PUT", "/api/admin/users/{id}/password"): "admin.user.reset_password",
}
# 同一个路由 GET 是读、POST 才是动作的那些，**只能按方法精确匹配**：
# 比如 /api/prompts/{id} GET 是读模板（噪音），PUT 才是改模板（有动作名）。
_ROUTE_ACTION_ANY = {}


def _fallback_action(method: str, route: str) -> str:
    """客户端没给动作名时按「方法 + 路由」补一个。查不到返回空串。"""
    method = (method or "").upper()
    route = route or ""
    return _ROUTE_ACTION.get((method, route)) or _ROUTE_ACTION_ANY.get(route) or ""


def _backfill_actions() -> int:
    """把库里「有路由、没动作名」的历史行补上动作名。

    幂等：已经是命名过的行不动，重复跑无副作用。启动时跑一次即可——
    历史行是有限的，新行在 handle_ingest 入库时就补好了。
    """
    fixed = 0
    for (method, route), action in _ROUTE_ACTION.items():
        cur = _conn.execute(
            "UPDATE client_audit SET action=? WHERE action='' AND method=? AND route=?",
            (action, method, route))
        fixed += cur.rowcount or 0
    _conn.commit()
    if fixed:
        print(f"[ingest] 历史日志回填动作名 {fixed} 条")
    return fixed



# ── 上报过滤：这些记录进后台看板纯属噪音，收到就直接丢 ─────────────────────
# 客户端本地库照记不误（本地排查有它自己的用处），只是不进服务器的后台。
# ① 无动作名、由页面加载/轮询/弹框产生的请求（2026-11-05 用户圈出的「—」行）：
#    项目列表每开一页就刷一遍、merge/status 每秒轮询、pick-folder 只是弹个文件框。
_INGEST_NOISE_ROUTES = {
    "/api/projects", "/api/projects/{id}", "/api/projects/{id}/plan",
    "/api/projects/{id}/transcript", "/api/projects/{id}/lines",
    "/api/projects/{id}/video",
    "/api/merge/status", "/api/pick-folder", "/api/pick-file",
    "/api/status", "/api/config", "/api/roots", "/api/browse",
    "/api/models-available", "/api/auth/me", "/api/quota",
    "/api/bgm/library",   # 开设置页读一次曲库状态，只读无副作用
    # 提示词原文读取：一进设置页就 GET 一次（route 里的 key 是 plans/shots，
    # 不是 id，所以只能按具体路由列）。PUT/DELETE 是真的改模板，带动作名，不会被丢。
    "/api/prompts/plans", "/api/prompts/shots", "/api/prompts/hooks",
}


def _is_noise(it: dict) -> bool:
    """是不是「纯噪音」——**有动作名的记录永远不丢**。

    2026-11-05 踩过的坑：这里原来只按 route 整条过滤，而 /api/projects、
    /api/projects/{id}、/api/projects/{id}/plan、/api/config 这几条既是
    轮询/加载路由，又挂着真实业务动作（project.create / project.delete /
    plan.generate / config.update）。结果是「新建项目」「生成结构」在后台
    一条都没有，用户来问「到底有没有上报」。过滤口径必须是
    「没有动作名 + 属于轮询/弹框路由」，不能只看路由。
    """
    method = (it.get("method") or "").upper()
    action = (it.get("action") or "").strip()
    # ① <video>/<img> 标签自发发起、压根没带凭证的 GET 撞 401：不是用户动作，
    #    放进来就是一排「登录被拒」刷屏（视频预览没带 access_token 的那类）。
    if action == "auth.reject" and method == "GET" \
            and (it.get("message") or "") == "no-token":
        return True
    # ② 有动作名 = 用户真实动作，不管落在哪条路由上都留下
    if action:
        return False
    # ③ 没有动作名的页面加载 / 轮询 / 弹框请求
    return (it.get("route") or "") in _INGEST_NOISE_ROUTES


def _purge_noise_rows() -> int:
    """清掉历史遗留的纯噪音行（有路由、没动作名、落在噪音路由表里）。

    为什么要有：过滤是 2026-11-05 才加的，之前入库的行还在——后台「动作」列
    显示「—」，正是用户点名看不懂的那种。新行在 handle_ingest 就被挡掉了，
    所以这里只可能清到旧数据，跑一次之后恒为 0。
    只删「没有动作名」的行：带动作名的（新建项目/生成结构等）永远保留。
    """
    marks = ",".join("?" * len(_INGEST_NOISE_ROUTES))
    cur = _conn.execute(
        f"DELETE FROM client_audit WHERE action='' AND route IN ({marks})",
        tuple(_INGEST_NOISE_ROUTES))
    _conn.commit()
    if cur.rowcount:
        print(f"[ingest] 清掉历史噪音日志 {cur.rowcount} 条")
    return cur.rowcount


def handle_ingest(token: str, body: dict) -> dict:
    """接收客户端批量上报的操作日志。

    **不需要管理员 Key**：这是每台装了 exe 的机器都会做的事，用的是它自己的设备
    令牌（第一次用 AI 时生成的匿名 id，不含个人信息）。真正的门槛在下面的限额：
    单次 200 条、总量按设备自然增长，刷接口也刷不出有价值的东西。
    `UNIQUE(device, rid)` + INSERT OR IGNORE：客户端重试不会重复入库（断网重推很常见）。
    """
    device = (token or "").strip() or (body.get("device") or "").strip()
    if not device:
        raise ApiError(401, "缺少设备令牌")
    items = body.get("items") or []
    if not isinstance(items, list):
        raise ApiError(400, "items 必须是数组")
    if len(items) > INGEST_MAX:
        raise ApiError(400, f"单次最多 {INGEST_MAX} 条")
    app = str(body.get("app") or "")[:20]
    oss = str(body.get("os") or "")[:20]
    now = int(time.time())
    n = 0
    skipped = 0
    with _lock:
        for it in items:
            if not isinstance(it, dict):
                continue
            # 老客户端不上报动作名：**先按路由补一个再判噪音**，否则这些真实动作
            # 会因为没有动作名而被当成轮询请求丢掉（2026-11-06）。
            # 注意顺序：补完再判，补出来的动作能享受「有动作名永不丢」的规则。
            if not str(it.get("action") or "").strip():
                guess = _fallback_action(str(it.get("method") or ""),
                                         str(it.get("route") or ""))
                if guess:
                    it = dict(it, action=guess)
            if _is_noise(it):
                skipped += 1
                continue
            try:
                _conn.execute(
                    "INSERT OR IGNORE INTO client_audit"
                    "(received_at,device,app,os,ts,username,role,method,path,route,query,"
                    " status,ms,ip,ua,action,target,payload,result,message,rid)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (now, device, app, oss,
                     str(it.get("created_at") or "")[:32],
                     str(it.get("username") or "")[:60],
                     str(it.get("role") or "")[:20],
                     str(it.get("method") or "")[:10],
                     str(it.get("path") or "")[:300],
                     str(it.get("route") or "")[:300],
                     str(it.get("query") or "")[:300],
                     int(it.get("status_code") or 0),
                     int(it.get("duration_ms") or 0),
                     str(it.get("ip") or "")[:64],
                     str(it.get("ua") or "")[:200],
                     str(it.get("action") or "")[:60],
                     str(it.get("target") or "")[:120],
                     str(it.get("payload") or "")[:2000],
                     str(it.get("result") or "")[:10],
                     str(it.get("message") or "")[:300],
                     str(it.get("request_id") or "")[:24]))
                n += 1
            except Exception:  # noqa: BLE001
                continue        # 单条坏数据不该让整批失败
        _conn.commit()
    return {"ok": True, "accepted": n, "got": len(items), "skipped": skipped}


def handle_client_logs(auth: str, qs: dict) -> dict:
    """看上报上来的日志。用 `/admin/logs?limit=50&device=xxx&only_fail=1`。"""
    _admin(auth)
    try:
        limit = max(1, min(500, int((qs.get("limit") or ["100"])[0])))
    except ValueError:
        limit = 100
    where, args = ["1=1"], []
    if qs.get("device"):
        where.append("device = ?")
        args.append(qs["device"][0])
    # 动作筛选支持**逗号分隔多组前缀**：「project.render,plan.render」= 出片的两个动作。
    # 为什么不选一个前缀完事：动作名是「模块.动作」式（project.render / plan.render），
    # 「出片」散在两个前缀下；单前缀要么漏（render. 什么都匹配不上）、要么滥
    # （plan. 会把出片的 plan.render 也算进「生成结构」）。
    if qs.get("action"):
        alts = [a.strip() for a in qs["action"][0].split(",") if a.strip()]
        if alts:
            where.append("(" + " OR ".join(["action LIKE ?"] * len(alts)) + ")")
            args += [a + "%" for a in alts]
    if qs.get("q"):
        like = "%" + qs["q"][0] + "%"
        where.append("(path LIKE ? OR message LIKE ? OR target LIKE ? OR username LIKE ?)")
        args += [like, like, like, like]
    if (qs.get("only_fail") or [""])[0] in ("1", "true"):
        where.append("status >= 400")
    sql = " AND ".join(where)
    # 详情框要展示参数和耗时，这两列也要带出来（payload 已在客户端脱敏、截 2000 字）。
    rows = _conn.execute(
        "SELECT ts,device,app,username,method,route,status,action,target,message,result,ms,payload"
        " FROM client_audit WHERE " + sql + " ORDER BY id DESC LIMIT ?", args + [limit]).fetchall()
    cols = ["ts", "device", "app", "username", "method", "route", "status",
            "action", "target", "message", "result", "ms", "payload"]
    total = _conn.execute("SELECT COUNT(*) FROM client_audit WHERE " + sql, args).fetchone()[0]
    devices = _conn.execute(
        "SELECT device, COUNT(*) n, MAX(ts) last FROM client_audit GROUP BY device"
        " ORDER BY last DESC LIMIT 50").fetchall()
    return {"total": total, "items": [dict(zip(cols, r)) for r in rows],
            "devices": [{"device": d, "logs": n, "last": t} for d, n, t in devices]}


def handle_log_stats(auth: str, qs: dict) -> dict:
    """操作统计：把 client_audit 按动作/接口聚合计数。用 `/admin/log_stats?days=7`。

    两张表分开看：
    - ops：有动作名的业务埋点（登录、生成结构、出片、改提示词……）——这些是
      客户端手写的 event，一条就是一个真实用户动作；
    - routes：没动作名的普通 API 请求（中间件记的），按「方法+路由」归并——
      新增项目这类没有专门埋点、但路由本身够明确的操作在这里看。
    时区口径：client_audit.ts 是客户端的 **UTC** ISO 串（db.now_iso 带 +00:00），
    窗口下界必须同为 UTC 字符串才能比。别再加 8 小时——那会把窗口错位 8 小时；
    按「天」分组时用 datetime(ts,'+8 hours') 折成北京时间，不然晚上 8 点后的
    记录会被算进前一天（2026-11-05 用户指出时间不对的另一半根源）。
    """
    _admin(auth)
    try:
        days = max(1, min(90, int((qs.get("days") or ["7"])[0])))
    except ValueError:
        days = 7
    since = time.strftime("%Y-%m-%dT%H:%M:%S",
                          time.gmtime(time.time() - days * 86400))
    total = _conn.execute(
        "SELECT COUNT(*) FROM client_audit WHERE ts >= ?", (since,)).fetchone()[0]
    users = _conn.execute(
        "SELECT COUNT(DISTINCT username) FROM client_audit"
        " WHERE ts >= ? AND username <> ''", (since,)).fetchone()[0]
    ops = _conn.execute(
        "SELECT action, COUNT(*) n, COUNT(DISTINCT NULLIF(username,'')) users,"
        " COUNT(DISTINCT device) devices,"
        " SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) fails, MAX(ts) last"
        " FROM client_audit WHERE ts >= ? AND action <> ''"
        " GROUP BY action ORDER BY n DESC LIMIT 60", (since,)).fetchall()
    routes = _conn.execute(
        "SELECT method, route, COUNT(*) n,"
        " COUNT(DISTINCT NULLIF(username,'')) users,"
        " SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) fails, MAX(ts) last"
        " FROM client_audit WHERE ts >= ? AND action = '' AND route <> ''"
        " GROUP BY method, route ORDER BY n DESC LIMIT 15", (since,)).fetchall()
    # 按天聚合要折成北京时间再取日期，否则 UTC 晚上 8 点后的记录算前一天
    daily = _conn.execute(
        "SELECT substr(datetime(ts,'+8 hours'),1,10) d, COUNT(*) n FROM client_audit"
        " WHERE ts >= ? GROUP BY d ORDER BY d", (since,)).fetchall()
    return {
        "days": days, "total": total, "users": users,
        "ops": [{"action": r[0], "n": r[1], "users": r[2], "devices": r[3],
                 "fails": r[4] or 0, "last": r[5]} for r in ops],
        "routes": [{"method": r[0], "route": r[1], "n": r[2], "users": r[3],
                    "fails": r[4] or 0, "last": r[5]} for r in routes],
        "daily": [{"d": r[0], "n": r[1]} for r in daily],
    }


def handle_syslog(auth: str, qs: dict) -> dict:
    """服务端自身日志：未捕获异常（带堆栈）+ 运行事件。

    用 `/admin/syslog?limit=50&kind=upstream&level=error&q=xxx`。
    和「客户端日志」的分工：那边是**用户机器上**发生了什么（谁点了转写、失败没），
    这边是**服务器自己**怎么了（接口炸了、上游欠费、有人撞管理员密钥、谁封了号）。
    两类日志合在一起看，才能区分「用户操作有问题」和「我们服务有问题」。
    """
    _admin(auth)

    def _int(name: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int((qs.get(name) or [default])[0])))
        except (TypeError, ValueError):
            return default

    limit = _int("limit", 50, 1, 200)
    kind = (qs.get("kind") or [""])[0].strip()
    level = (qs.get("level") or [""])[0].strip()
    q = (qs.get("q") or [""])[0].strip()

    now = time.time()
    today0 = int(now) - int((now + DAY_TZ_OFFSET) % 86400)   # 北京时间 0 点
    day_ago = int(now) - 86400
    week_ago = int(now) - 7 * 86400

    today_errors = _conn.execute(
        "SELECT COUNT(*) FROM server_error WHERE ts>=?", (today0,)).fetchone()[0]
    today_events = _conn.execute(
        "SELECT COUNT(*) FROM server_event WHERE ts>=?", (today0,)).fetchone()[0]
    slow_24h = _conn.execute(
        "SELECT COUNT(*) FROM server_event WHERE kind='slow' AND ts>=?",
        (day_ago,)).fetchone()[0]
    last_error = _conn.execute(
        "SELECT COALESCE(MAX(ts),0) FROM server_error").fetchone()[0]

    # 曲线按北京时间切天：ts 是整数秒，必须先转 unixepoch 再加 8 小时
    labels = [time.strftime("%Y-%m-%d", time.gmtime(now + DAY_TZ_OFFSET - i * 86400))
              for i in range(6, -1, -1)]
    errs = {r[0]: r[1] for r in _conn.execute(
        "SELECT substr(datetime(ts,'unixepoch','+8 hours'),1,10) d, COUNT(*) n"
        " FROM server_error WHERE ts>=? GROUP BY d", (week_ago,))}
    evts = {r[0]: r[1] for r in _conn.execute(
        "SELECT substr(datetime(ts,'unixepoch','+8 hours'),1,10) d, COUNT(*) n"
        " FROM server_event WHERE ts>=? GROUP BY d", (week_ago,))}
    daily = [{"day": d, "errors": errs.get(d, 0), "events": evts.get(d, 0)} for d in labels]
    kinds = [{"kind": r[0], "n": r[1]} for r in _conn.execute(
        "SELECT kind, COUNT(*) n FROM server_event WHERE ts>=?"
        " GROUP BY kind ORDER BY n DESC LIMIT 12", (week_ago,))]

    where, args = ["1=1"], []
    if q:
        like = "%" + q + "%"
        where.append("(path LIKE ? OR exc_msg LIKE ? OR exc_type LIKE ? OR ip LIKE ?)")
        args += [like] * 4
    # trace 截 4000 字：单条堆栈实际很少超过 2KB，但极端情况（递归）能几十 KB，
    # 一次拉 50 条会把后台页面拖慢。
    rows = _conn.execute(
        "SELECT id,ts,method,path,ip,ua,status,exc_type,exc_msg,substr(trace,1,4000) trace"
        " FROM server_error WHERE " + " AND ".join(where) +
        " ORDER BY id DESC LIMIT ?", args + [limit]).fetchall()
    ecols = ["id", "ts", "method", "path", "ip", "ua", "status",
             "exc_type", "exc_msg", "trace"]
    errors = [dict(zip(ecols, r)) for r in rows]

    vwhere, vargs = ["1=1"], []
    if kind:
        vwhere.append("kind=?")
        vargs.append(kind)
    if level:
        vwhere.append("level=?")
        vargs.append(level)
    if q:
        like = "%" + q + "%"
        vwhere.append("(action LIKE ? OR detail LIKE ? OR actor LIKE ? OR ip LIKE ?)")
        vargs += [like] * 4
    vrows = _conn.execute(
        "SELECT id,ts,kind,level,action,actor,detail,ip FROM server_event WHERE " +
        " AND ".join(vwhere) + " ORDER BY id DESC LIMIT ?", vargs + [limit]).fetchall()
    vcols = ["id", "ts", "kind", "level", "action", "actor", "detail", "ip"]
    events = [dict(zip(vcols, r)) for r in vrows]

    return {
        "today_errors": today_errors, "today_events": today_events,
        "slow_24h": slow_24h, "last_error_ts": int(last_error or 0),
        "daily": daily, "kinds": kinds,
        "errors": errors, "events": events,
        "keep_days": LOG_KEEP_DAYS, "slow_ms": SLOW_MS,
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        """默认访问日志丢掉 —— 但**不是**什么都不记，见 _after()。

        BaseHTTPRequestHandler 每个请求都会调这里，全量落盘在 0.5G 内存的
        小机器上能把 journal 刷爆、还把真正要看的错误埋掉；所以这里保持静默，
        由 _after() 只挑「慢请求」和「5xx」两种进 server_event。
        """
        pass

    def _after(self, status: int) -> None:
        """请求收尾埋点：慢请求 / 主动抛出的 5xx。

        耗时从 do_GET/do_POST 开头记的 _t0 算。500 如果已经在 except 里
        进了 server_error（带堆栈），这里就不再重复记一条事件。
        """
        t0 = getattr(self, "_t0", 0.0)
        if not t0:
            return
        ms = int((time.time() - t0) * 1000)
        path = self.path.split("?")[0][:300]
        if status >= 500 and not getattr(self, "_logged_err", False):
            # 主动抛的 5xx（典型是上游 502）：没有堆栈，记一条事件够用
            _log_event("http", "http.%d" % status,
                       "%s %s %dms" % (self.command, path, ms),
                       level="error", ip=self._client_ip())
        elif ms >= SLOW_MS:
            _log_event("slow", "http.slow",
                       "%s %s %dms status=%d" % (self.command, path, ms, status),
                       level="warn", ip=self._client_ip())

    def _client_ip(self) -> str:
        # 走 caddy 反代后 client_address 恒为 127.0.0.1，必须看 X-Forwarded-For，
        # 否则限流退化成全网共用一个桶（2026-11-04 实测踩到）。
        return _client_ip(self.headers, self.client_address[0] if self.client_address else "?")

    def _send(self, status: int, obj) -> None:
        # 后台列表里的 IP 顺带带一个离线查出来的地名。库缺失/损坏一律静默跳过——
        # 这是锦上添花，绝不能让它把运营后台接口拖成 500。
        if self.path.startswith("/admin/"):
            try:
                _decorate_geo(obj)
            except Exception:  # noqa: BLE001
                pass
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        finally:
            # 放 finally：客户端半路断开（BrokenPipe）也要留下耗时痕迹
            self._after(status)

    def _send_html(self, data: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        # 后台页面不缓存：改了后台要刷新就能看到，不用记着 Ctrl+F5
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _token(self) -> str:
        return (self.headers.get("X-HS-Token") or "").strip()

    def _admin_key(self) -> str:
        return (self.headers.get("X-Admin-Key") or "").strip()

    def _body_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            raise ApiError(400, "请求体不是合法 JSON")

    def do_GET(self):
        path = self.path.split("?")[0]
        self._t0 = time.time()
        self._logged_err = False
        try:
            if path == "/health":
                self._send(200, {"ok": True})
            elif path in ("/admin", "/admin/"):
                # 页面本身不含数据，所以不校验密钥；页面加载后第一件事就是要求输入，
                # 拿到的密钥只放在 sessionStorage，关掉标签页即失效。
                self._send_html(_admin_html())
            elif path == "/api/version":
                # 公开接口：谁都能问「现在最新版是多少」，不含任何业务数据
                self._send(200, handle_version())
            elif path == "/api/quota":
                self._send(200, handle_quota(self._token()))
            elif path == "/api/auth/me":
                self._send(200, handle_account_me(self._token()))
            elif path == "/admin/stats":
                self._send(200, handle_stats(self._admin_key()))
            elif path == "/admin/accounts":
                qs = parse_qs(urlparse(self.path).query)
                self._send(200, handle_accounts(self._admin_key(), qs))
            elif path == "/admin/series":
                qs = parse_qs(urlparse(self.path).query)
                self._send(200, handle_series(self._admin_key(), qs))
            elif path == "/admin/release":
                self._send(200, handle_release_get(self._admin_key()))
            elif path == "/admin/log_stats":
                qs = parse_qs(urlparse(self.path).query)
                self._send(200, handle_log_stats(self._admin_key(), qs))
            elif path == "/admin/logs":
                qs = parse_qs(urlparse(self.path).query)
                self._send(200, handle_client_logs(self._admin_key(), qs))
            elif path == "/admin/syslog":
                qs = parse_qs(urlparse(self.path).query)
                self._send(200, handle_syslog(self._admin_key(), qs))
            else:
                raise ApiError(404, "接口不存在")
        except ApiError as exc:
            # 管理员密钥错是安全事件：但后台接口在公网，扫描器一天撞几千次，
            # 同 IP 半小时只记一条（_log_admin_auth_fail 内部去重）。
            if exc.status == 401 and path.startswith("/admin/"):
                _log_admin_auth_fail(self._client_ip(), path)
            self._send(exc.status, {"error": {"message": exc.message}})
        except Exception as exc:  # noqa: BLE001
            # 先落库再回 500：以前这里只回类型名，堆栈就地丢弃，线上等于无痕
            self._logged_err = True
            _log_error(self.command, path, self._client_ip(),
                       self.headers.get("User-Agent") or "", exc)
            self._send(500, {"error": {"message": f"服务内部错误: {type(exc).__name__}"}})

    def do_POST(self):
        path = self.path.split("?")[0]
        self._t0 = time.time()
        self._logged_err = False
        try:
            if path == "/api/proxy/chat/completions":
                body = self._body_json()
                r = handle_chat(self._token(), self._client_ip(), body)
                self._send(r["_status"], r["_body"])
            elif path == "/api/auth/register":
                body = self._body_json()
                self._send(200, handle_register(self._client_ip(),
                                                self.headers.get("User-Agent") or "", body))
            elif path == "/api/auth/login":
                body = self._body_json()
                self._send(200, handle_login(self._client_ip(),
                                             self.headers.get("User-Agent") or "", body))
            elif path == "/api/auth/password":
                body = self._body_json()
                self._send(200, handle_change_pwd(self._token(), self._client_ip(),
                                                  self.headers.get("User-Agent") or "", body))
            elif path == "/admin/account":
                body = self._body_json()
                self._send(200, handle_account_action(self._admin_key(), body,
                                                      self._client_ip()))
            elif path == "/admin/release":
                body = self._body_json()
                self._send(200, handle_release_save(self._admin_key(), body,
                                                    self._client_ip()))
            elif path == "/api/audit/ingest":
                body = self._body_json()
                self._send(200, handle_ingest(self._token(), body))
            else:
                raise ApiError(404, "接口不存在")
        except ApiError as exc:
            if exc.status == 401 and path.startswith("/admin/"):
                _log_admin_auth_fail(self._client_ip(), path)
            self._send(exc.status, {"error": {"message": exc.message}})
        except Exception as exc:  # noqa: BLE001
            self._logged_err = True
            _log_error(self.command, path, self._client_ip(),
                       self.headers.get("User-Agent") or "", exc)
            self._send(500, {"error": {"message": f"服务内部错误: {type(exc).__name__}"}})


if __name__ == "__main__":
    # 启动/停止事件：出问题第一句要能回答「服务是不是重启过、什么时候」。
    # 模块级已经起了一个 cleanup 线程（限流桶清理），这里不再重复起。
    def _on_term(signum, frame):  # noqa: ARG001
        _log_event("boot", "service.stop", "signal=%d" % signum)
        raise SystemExit(0)

    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, _on_term)
        except (ValueError, OSError):
            pass
    # 历史日志回填动作名（幂等，启动跑一次）：老客户端上报的 action 为空，
    # 补上之后后台「动作」列和筛选下拉才认得这些行。
    try:
        _backfill_actions()
        _purge_noise_rows()
    except Exception as exc:  # noqa: BLE001
        print(f"[hsproxy] 回填动作名失败（忽略）：{exc}", flush=True)
    _log_event("boot", "service.start",
               "port=%d pid=%d levels=free:%d/pro:%d/internal:%s keep=%dd slow>=%dms"
               % (LISTEN_PORT, os.getpid(), LEVELS["free"]["calls"],
                  LEVELS["pro"]["calls"], LEVELS["internal"]["desc"],
                  LOG_KEEP_DAYS, SLOW_MS))
    print(f"[hsproxy] listening 127.0.0.1:{LISTEN_PORT}, "
          f"levels=" + "/".join("%s:%d" % (k, LEVELS[k]["calls"]) for k in LEVEL_ORDER),
          flush=True)
    ThreadingHTTPServer(("127.0.0.1", LISTEN_PORT), Handler).serve_forever()
