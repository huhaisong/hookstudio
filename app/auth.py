"""用户认证：注册、登录、刷新、登出、改资料、改密码 + 鉴权依赖。

### 三条主线的状态流转

**登录**：username/email + password → access token（JWT，默认 120 分钟）
        + refresh token（随机串，默认 14 天，服务端可撤销）。

**为什么 access 短、refresh 长**：access 无状态、不查库就能验签（这里仍然查一次库
是为了封禁即时生效），被偷了也只能用两小时；refresh 走库，能随时撤销、能轮换、
能列出「登录过哪些设备」。

**封禁即时生效**：users.token_version 是个自增计数器，JWT 里带着签发时的值。
封禁时 +1，之后所有旧 token 在 resolve_token 里对不上 → 立刻失效（包括 refresh，
它们同时被标记撤销）。不需要黑名单、不依赖内存，重启后照样生效。

### 鉴权强度：只有一种（全局强制登录，2026-11-04 用户定性）

`auth.auth_required()` 恒为 True：白名单（登录/注册/刷新/status）以外，没登录一律 401。
`audit.GuardMiddleware` 是第一道，本文件里的依赖是第二道。

**曾经有过「false = 只审计不拦截」的模式，已废弃**：那是本机单机工具的遗留设定，
在多人/联网场景下会让封禁与踢下线彻底失效（被封的人清掉本地凭证就以匿名身份继续用），
日志里也记不出用户。留着开关只会再踩一次，所以直接去掉。

**为什么依赖里还要再验一次**：中间件按路径白名单放行，如果以后有人改了中间件、
或者某个路由挂在别的 ASGI 应用下绕过中间件，依赖这一层仍然拦得住——
用户要的就是"登录 + 全局中间件双重校验"。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import audit, core, db, remote, security

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 用户名允许中文：这是个内容工具，用户更愿意用「小王」而不是 wang_xiao_2026。
# 但禁止空格和标点，避免出现在 URL/日志里要转义。
USERNAME_RE = re.compile(r"^[\w\u4e00-\u9fa5.-]{3,20}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

MAX_FAIL = 5           # 连续失败几次锁
LOCK_MIN = 10          # 锁多久
_SECRET_CACHE: str | None = None


# ── 配置读取 ──────────────────────────────────────────────────────────────
def jwt_secret() -> str:
    """签名密钥。首次用自动生成并存进 config，之后固定。

    缓存一份在内存里：每个请求都要用，而 config.json 是磁盘文件；
    但换了 config 里的密钥（罕见）必须重启才生效——文档里写清。
    """
    global _SECRET_CACHE
    if _SECRET_CACHE:
        return _SECRET_CACHE
    cfg = core.load_config()
    secret = (cfg.get("jwt_secret") or "").strip()
    if not secret:
        secret = security.new_secret()
        core.save_config({"jwt_secret": secret})
    _SECRET_CACHE = secret
    return secret


def reset_secret_cache() -> None:
    """config 里的 jwt_secret 被改了之后调用（目前只有测试用）。"""
    global _SECRET_CACHE
    _SECRET_CACHE = None


def auth_required() -> bool:
    """是否全局强制登录。

    **2026-11-04 用户定性：本产品是「需要登录」的工具，不再提供免登录模式。**
    之前做成可关的开关（false = 只审计不拦截），结果是一整类安全漏洞：
    封禁/踢下线在免登录部署下形同虚设——被封的人清掉本地凭证就以「匿名访客」
    继续用，日志里也全是没有用户的空行。开关留着只会再踩一次。

    所以这里恒返回 True，配置项 `auth_required` 只为兼容旧 config.json 保留，
    运行期不再读取。
    """
    return True


def access_minutes() -> int:
    try:
        return max(1, min(1440, int(core.load_config().get("access_minutes") or 1440)))
    except (TypeError, ValueError):
        return 1440


def refresh_days() -> int:
    try:
        return max(1, min(90, int(core.load_config().get("refresh_days") or 14)))
    except (TypeError, ValueError):
        return 14


# ── 令牌解析 ──────────────────────────────────────────────────────────────
def token_from_headers(request: Request) -> str:
    """从请求里取 access token。

    支持两种位置：
      ① `Authorization: Bearer xxx`（正常接口）；
      ② `?access_token=xxx`（**只给 GET 用**）。为什么需要：`<video src>` /
         `<img src>` 这类标签没法自定义请求头，视频预览接口要能带凭证只能走 query。
         代价是 token 可能进浏览器历史/反代日志——所以仅限 GET，且日志里
         token 字段会被脱敏成 ***。
    """
    h = request.headers.get("authorization") or ""
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    if request.method == "GET":
        return (request.query_params.get("access_token") or "").strip()
    return ""


def resolve_token(token: str) -> tuple[dict | None, str]:
    """验签 + 查库 + 三重校验（存在 / 版本号 / 封禁状态）。

    返回 (user, reason)。reason 是给日志用的机器码，不直接回给客户端
    （只回「未登录或已过期」，避免给爆破者判断依据）。
    """
    if not token:
        return None, "no-token"
    try:
        payload = security.jwt_decode(token, jwt_secret())
    except security.TokenError as exc:
        return None, str(exc)
    if payload.get("typ") != "access":
        return None, "wrong-type"
    uid = payload.get("sub")
    if not isinstance(uid, int):
        return None, "bad-sub"
    try:
        with db.db() as c:
            row = c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    except Exception as exc:  # noqa: BLE001
        print(f"[auth] 查用户失败：{exc}")
        return None, "db-error"
    if not row:
        return None, "user-missing"
    user = dict(row)
    # 封禁判定必须排在版本号前面（2026-11-04 修）。
    # 封禁时 token_version 也会 +1，如果先比版本号，被封的人拿到的是 "revoked"
    # → 前端显示「登录已过期」，用户完全不知道自己是被封了，还以为掉线重登就好。
    if user.get("status") != "active":
        return None, "banned"
    # 版本号对不上 = 期间改过密码 / 被踢下线，这个 token 立即作废
    if int(payload.get("tv") or 0) != int(user.get("token_version") or 0):
        return None, "revoked"
    return public_user(user), ""


def public_user(row: dict) -> dict:
    """对外可见的用户字段。**绝不带 password_hash**——一个漏出去整个库就废了。"""
    return {
        "id": row["id"],
        "username": row["username"],
        "email": row.get("email") or "",
        "phone": row.get("phone") or "",
        "display_name": row.get("display_name") or "",
        "avatar": row.get("avatar") or "",
        "role": row.get("role") or "user",
        "status": row.get("status") or "active",
        "must_change_pwd": int(row.get("must_change_pwd") or 0),
        "last_login_at": row.get("last_login_at"),
        "created_at": row.get("created_at"),
    }


# ── FastAPI 依赖 ──────────────────────────────────────────────────────────
def ban_reason_by_token(token: str) -> str:
    """被封禁时取一句可读理由，直接回给客户端。

    只在「已经被判定为 banned」这一条错误路径上调用，正常情况下一次都不会走到，
    所以多解一次 token、多查一次库不计入热路径成本。
    """
    try:
        payload = security.jwt_decode(token, jwt_secret())
        uid = payload.get("sub")
        if not isinstance(uid, int):
            return ""
        with db.db() as c:
            row = c.execute("SELECT ban_reason FROM users WHERE id = ?", (uid,)).fetchone()
        return (row["ban_reason"] or "").strip() if row else ""
    except Exception:  # noqa: BLE001
        return ""


def optional_user(request: Request) -> dict | None:
    """有 token 就解析出来，没有就 None。不抛异常。

    中间件已经解析过一次（存在 scope["state"] 里），这里直接复用；
    标记位 `auth_checked` 保证「确实没登录」时不会重复解析。
    """
    if getattr(request.state, "auth_checked", False):
        return getattr(request.state, "user", None)
    token = token_from_headers(request)
    user, _reason = resolve_token(token) if token else (None, "")
    request.state.user = user
    request.state.auth_checked = True
    return user


def current_user(request: Request) -> dict:
    """必须登录。第二道校验的落点。"""
    user = optional_user(request)
    if not user:
        raise HTTPException(401, "未登录或登录已过期，请重新登录",
                            headers={"WWW-Authenticate": "Bearer"})
    return user


def require_admin(request: Request) -> dict:
    user = current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user


def require_admin_request(request: Request | None) -> dict:
    """给「路由函数里手动调」用（audit 模块的日志接口就是这么做的）。

    request 为空说明调用姿势不对（比如单测直接调函数）——直接 401，别静默放行。
    """
    if request is None:
        raise HTTPException(401, "登录状态失效，请重新登录")
    return require_admin(request)


def require_admin_if_enforced(request: Request) -> None:
    """「登录模式下要求管理员」的轻量依赖。

    名字保留是为了少改调用点。`auth_required()` 现在恒为 True，所以它等价于
    `require_admin`——原来「本机单机不拦」的分支已经不存在了。
    """
    require_admin(request)


# ── 入参模型 ──────────────────────────────────────────────────────────────
class RegisterBody(BaseModel):
    username: str
    password: str
    email: str = ""
    display_name: str = ""


class LoginBody(BaseModel):
    username: str      # 用户名或邮箱，二选一
    password: str


class RefreshBody(BaseModel):
    refresh_token: str


class LogoutBody(BaseModel):
    refresh_token: str = ""
    all_devices: bool = False


class ProfileBody(BaseModel):
    display_name: str | None = None
    email: str | None = None
    avatar: str | None = None


class PasswordBody(BaseModel):
    old_password: str
    new_password: str


# ── 签发令牌 ──────────────────────────────────────────────────────────────
def _issue(user: dict, request: Request) -> dict:
    """签发一对令牌：access（内存里用的）+ refresh（存库、可撤销）。"""
    access, ttl = security.make_access_token(jwt_secret(), user, access_minutes())
    rt = security.new_refresh_token()
    now = datetime.now(timezone.utc)
    exp = now + timedelta(days=refresh_days())
    with db.db() as c:
        c.execute(
            """INSERT INTO refresh_tokens
               (user_id, token_hash, issued_at, expires_at, ip, ua)
               VALUES (?,?,?,?,?,?)""",
            (int(user["id"]), security.sha256(rt), now.isoformat(timespec="seconds"),
             exp.isoformat(timespec="seconds"), audit.client_ip(request),
             audit.user_agent(request)),
        )
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ttl,
        "refresh_token": rt,
        "refresh_expires_in": int((exp - now).total_seconds()),
        "user": public_user(user),
    }


def _revoke_all(c, user_id: int, reason: str) -> None:
    c.execute("UPDATE refresh_tokens SET revoked_at = ?, revoked_reason = ? "
              "WHERE user_id = ? AND revoked_at IS NULL",
              (db.now_iso(), reason, user_id))


# ── 接口：注册 / 登录 ─────────────────────────────────────────────────────
def _upsert_local(username: str, password: str, display: str = "") -> dict:
    """把服务器账号镜像到本地 users 表，返回本地用户行。

    为什么本地还要留一份：项目、审计、票据全都挂在本地 `users.id` 上，换掉要动
    半个程序。保留本地行 = 其他地方一行不用改；密码哈希也留着，是为了**断网时
    还能登录**（没网照样能剪片，只是调不了 AI）。

    角色一律 user：本机那个后台本来是给「第一个注册者」当管理员的，现在账号在
    服务器上，一个人一台电脑的终端用户不需要本机管理权限。
    """
    now = db.now_iso()
    with db.db() as c:
        row = c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if row:
            # 服务器放行 = 这个号现在是好的：顺手把可能留着的 banned 标记清掉
            # （运营解封之后，用户下次登录就该恢复正常）
            c.execute("UPDATE users SET password_hash = ?, status = 'active',"
                      " ban_reason = '', updated_at = ? WHERE id = ?",
                      (security.hash_password(password), now, row["id"]))
            uid = row["id"]
        else:
            cur = c.execute(
                """INSERT INTO users (username, password_hash, display_name, role,
                                      created_at, updated_at)
                   VALUES (?,?,?,?,?,?)""",
                (username, security.hash_password(password), display or username,
                 "user", now, now))
            uid = cur.lastrowid
        fresh = c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return dict(fresh)


def _accept_remote(res: dict, username: str, password: str, request: Request,
                   action: str, display: str = "") -> dict:
    """服务器认证通过之后统一走的收尾：镜像本地、存令牌、发本地 JWT、写审计。"""
    acc = res.get("account") or {}
    name = (acc.get("username") or username).strip()
    user = _upsert_local(name, password, display)
    core.save_config({"account_token": res.get("token") or "", "account_name": name})

    now = db.now_iso()
    with db.db() as c:
        c.execute("UPDATE users SET fail_count = 0, locked_until = NULL, "
                  "last_login_at = ?, last_login_ip = ?, updated_at = ? WHERE id = ?",
                  (now, audit.client_ip(request), now, user["id"]))
        user["last_login_at"] = now
        user["fail_count"] = 0
    tokens = _issue(user, request)
    audit.log_event(tokens["user"], action, target=f"user:{user['id']}",
                    payload={"username": name}, request=request)
    return tokens


@router.post("/register")
def register(body: RegisterBody, request: Request):
    """注册。账号建在**服务器**上，不是本机。

    2026-11-04 之前这里是本机注册、第一个注册者自动当管理员。那样有两个死结：
    换台电脑账号就不存在；每个用户在自己机器上都是管理员，运营方看不到也管不了。
    现在服务器是唯一权威，本机只留一份副本。
    """
    username = (body.username or "").strip()
    if not USERNAME_RE.match(username):
        raise HTTPException(400, "用户名 3-20 位，只能用中文、字母、数字、下划线、点、横线")
    err = security.check_password_strength(body.password or "")
    if err:
        raise HTTPException(400, err)

    try:
        res = remote.register(username, body.password or "", core.device_token())
    except remote.RemoteError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    except remote.NetworkError as exc:
        raise HTTPException(
            503, "注册要连服务器，现在连不上。请检查网络后重试；"
                 "已经注册过的账号断网也能登录。") from exc

    tokens = _accept_remote(res, username, body.password or "", request, "auth.register",
                            display=(body.display_name or "").strip()[:40])
    tokens["user"]["is_first_admin"] = False     # 账号体系里不再有「注册就当管理员」
    return tokens


def _local_verify(ident: str, pwd: str) -> tuple[dict | None, int, str]:
    """本地校验用户名 / 密码。返回 (用户行, HTTP 状态, 提示)。

    用在两个地方：断网时登录、以及「服务器上还没有这个账号」的老用户迁移。
    失败统一回「用户名或密码不对」，不区分没这人和密码错（否则等于送一个账号枚举接口）。
    """
    with db.db() as c:
        row = c.execute("SELECT * FROM users WHERE username = ? OR email = ?",
                        (ident, ident)).fetchone()
    if not row:
        return None, 401, "用户名或密码不对"

    user = dict(row)
    locked = user.get("locked_until") or ""
    if locked and locked > db.now_iso():
        left = max(1, int((datetime.fromisoformat(locked)
                           - datetime.now(timezone.utc)).total_seconds() // 60) + 1)
        return None, 429, f"密码错误次数过多，请 {left} 分钟后再试"

    if not security.verify_password(pwd, user["password_hash"]):
        with db.db() as c:
            fail = int(user.get("fail_count") or 0) + 1
            lock_until = None
            if fail >= MAX_FAIL:
                lock_until = (datetime.now(timezone.utc)
                              + timedelta(minutes=LOCK_MIN)).isoformat(timespec="seconds")
            c.execute("UPDATE users SET fail_count = ?, locked_until = ?, updated_at = ? "
                      "WHERE id = ?", (fail, lock_until, db.now_iso(), user["id"]))
        return None, 401, "用户名或密码不对"

    if user["status"] != "active":
        return None, 403, f"账号已被封禁：{user.get('ban_reason') or '请联系管理员'}"
    return user, 200, ""


def _mark_local_banned(ident: str, reason: str) -> None:
    """服务器说这个号被封了 → 本地也标上。

    不做这一步的话，被封的人拔掉网线就能用本地校验登进来（本地表里还是 active），
    虽然调 AI 会被服务器拒，但「封了还能打开用」本身就不对。
    标记之后，断网登录走 `_local_verify` 也会被拦。
    """
    try:
        with db.db() as c:
            c.execute("UPDATE users SET status = 'banned', ban_reason = ?, updated_at = ? "
                      "WHERE username = ? COLLATE NOCASE", (reason[:200], db.now_iso(), ident))
    except Exception:  # noqa: BLE001
        pass


@router.post("/login")
def login(body: LoginBody, request: Request):
    """登录。用户名或邮箱都可以。

    **以服务器为准**：服务器说密码不对就是不对，不会回退本地校验——
    否则被运营方封掉的人拔掉网线就能继续用，封禁形同虚设。
    只有「连不上服务器」才回退本地（保证没网也能打开软件剪片）。
    """
    ident = (body.username or "").strip()
    pwd = body.password or ""
    if not ident or not pwd:
        raise HTTPException(400, "请输入用户名和密码")

    try:
        res = remote.login(ident, pwd, core.device_token())
        return _accept_remote(res, ident, pwd, request, "auth.login")
    except remote.RemoteError as exc:
        # 服务器说什么就是什么。2026-11-04 白天这里还有一条「服务器不认识 =
        # 可能是老用户，本地密码对就自动注册过去」的迁移岔路；当晚确认没有老客户，
        # 已删除——留着等于给「随便编个用户名 + 本机密码」留后门。
        audit.log_event(None, "auth.login", payload={"username": ident},
                        result="fail", message=f"remote:{exc.status}", request=request,
                        status_code=exc.status)
        # 被封禁：顺手把本地也标上，否则他拔网线就能绕过（见 _mark_local_banned）
        if exc.status == 403 and "封禁" in (exc.message or ""):
            _mark_local_banned(ident, exc.message)
        raise HTTPException(exc.status, exc.message) from exc
    except remote.NetworkError:
        # 断网：本地校验。这里**不碰 account_token**——可能只是暂时没网，
        # 等网络恢复后继续用原来那张，额度还算在同一个账号上。
        user, st, msg = _local_verify(ident, pwd)
        if not user:
            audit.log_event(None, "auth.login", payload={"username": ident},
                            result="fail", message="offline-local-fail",
                            request=request, status_code=st)
            raise HTTPException(st, msg)
        now = db.now_iso()
        with db.db() as c:
            c.execute("UPDATE users SET fail_count = 0, locked_until = NULL, "
                      "last_login_at = ?, last_login_ip = ?, updated_at = ? WHERE id = ?",
                      (now, audit.client_ip(request), now, user["id"]))
            user["last_login_at"] = now
            user["fail_count"] = 0
        tokens = _issue(user, request)
        audit.log_event(tokens["user"], "auth.login", target=f"user:{user['id']}",
                        payload={"username": ident, "offline": True}, request=request)
        return tokens


@router.post("/refresh")
def refresh_token(body: RefreshBody, request: Request):
    """用 refresh token 换新的一对令牌（**轮换**：旧的立刻作废）。

    轮换不只是洁癖：refresh token 一旦泄露，攻击者用它刷新时会把用户的旧令牌
    挤掉，用户下次刷新就掉线——这个"异常掉线"本身就是泄露信号。不轮换则
    泄露的 token 可以跟用户并行用 14 天，谁也不知道。
    """
    rt = (body.refresh_token or "").strip()
    if not rt:
        raise HTTPException(400, "缺少 refresh_token")
    h = security.sha256(rt)
    now = db.now_iso()
    with db.db() as c:
        row = c.execute("SELECT * FROM refresh_tokens WHERE token_hash = ?", (h,)).fetchone()
        if not row:
            raise HTTPException(401, "登录状态无效，请重新登录")
        if row["revoked_at"]:
            raise HTTPException(401, "登录状态已失效（已登出或已换新），请重新登录")
        if row["expires_at"] <= now:
            raise HTTPException(401, "登录已过期，请重新登录")
        # 原子轮换：并发拿同一个 refresh 来刷，只有一次能成功
        cur = c.execute(
            "UPDATE refresh_tokens SET revoked_at = ?, revoked_reason = 'rotated' "
            "WHERE id = ? AND revoked_at IS NULL", (now, row["id"]))
        if cur.rowcount != 1:
            raise HTTPException(401, "登录状态已失效，请重新登录")
        urow = c.execute("SELECT * FROM users WHERE id = ?", (row["user_id"],)).fetchone()
        if not urow:
            raise HTTPException(401, "用户不存在")
        if urow["status"] != "active":
            raise HTTPException(403, "账号已被封禁，请联系管理员")
    return _issue(dict(urow), request)


@router.post("/logout")
def logout(body: LogoutBody, request: Request):
    """登出。只撤销这一个 refresh（其他设备不受影响）；all_devices=true 全下。"""
    user = optional_user(request)
    rt = (body.refresh_token or "").strip()
    now = db.now_iso()
    with db.db() as c:
        if rt:
            c.execute("UPDATE refresh_tokens SET revoked_at = ?, revoked_reason = 'logout' "
                      "WHERE token_hash = ? AND revoked_at IS NULL",
                      (now, security.sha256(rt)))
        if body.all_devices and user:
            _revoke_all(c, int(user["id"]), "logout-all")
            # 版本号 +1：access token 也一起作废（否则它还能再用两小时）
            c.execute("UPDATE users SET token_version = token_version + 1, updated_at = ? "
                      "WHERE id = ?", (now, user["id"]))
    if user:
        audit.log_event(user, "auth.logout",
                        target=f"user:{user['id']}",
                        payload={"all_devices": bool(body.all_devices)}, request=request)
    # 账号令牌留在本机就等于「退出登录没退干净」——下一个人打开界面，
    # AI 调用还是记在上一个人头上。这里必须清掉。
    core.save_config({"account_token": ""})
    return {"ok": True}


# ── 接口：当前用户 / 改资料 / 改密码 ──────────────────────────────────────
@router.get("/me")
def me(request: Request):
    """当前登录用户。前端启动时调一次：拿到身份 + 决定显示不显示「管理后台」。"""
    user = optional_user(request)
    # 分发版的账号在服务器上，本机没有「管理员」这回事：一个人一台电脑，
    # 「管理后台」里点开只有他自己。所以权限一律不给，入口也就不会渲染。
    is_admin = bool(user and user.get("role") == "admin"
                    and not core.load_config().get("dist_mode"))
    return {
        "logged_in": bool(user),
        "user": user,
        "auth_required": auth_required(),
        "permissions": {
            "admin": is_admin,
            "manage_users": is_admin,
            "view_audit": is_admin,
        },
    }


@router.put("/profile")
def update_profile(body: ProfileBody, request: Request):
    user = current_user(request)
    sets, args = [], []
    if body.display_name is not None:
        sets.append("display_name = ?")
        args.append(body.display_name.strip()[:40])
    if body.avatar is not None:
        sets.append("avatar = ?")
        args.append(body.avatar.strip()[:300])
    email = None
    if body.email is not None:
        email = body.email.strip()
        if email and not EMAIL_RE.match(email):
            raise HTTPException(400, "邮箱格式不对")
    if email is not None:
        with db.db() as c:
            if email and c.execute("SELECT 1 FROM users WHERE email = ? AND id <> ?",
                                   (email, user["id"])).fetchone():
                raise HTTPException(409, "这个邮箱已被其他账号使用")
        sets.append("email = ?")
        args.append(email or None)
    if not sets:
        return {"ok": True, "user": user}
    sets.append("updated_at = ?")
    args += [db.now_iso(), int(user["id"])]
    with db.db() as c:
        c.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", args)
        row = c.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    fresh = public_user(dict(row))
    audit.log_event(fresh, "auth.update_profile", target=f"user:{user['id']}",
                    payload=body.model_dump(exclude_none=True), request=request)
    return {"ok": True, "user": fresh}


@router.put("/password")
def change_password(body: PasswordBody, request: Request):
    """改密码：校验旧密码 → 更新 → **所有旧 token 立即失效** → 给当前设备发新的一对。

    为什么要让所有设备下线：改密码的常见场景就是「怀疑账号被别人用了」。
    只改哈希不废 token 等于没改——对方手里的 token 还能用到过期。
    """
    user = current_user(request)
    with db.db() as c:
        row = c.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    if not row:
        raise HTTPException(404, "用户不存在")
    if not security.verify_password(body.old_password or "", row["password_hash"]):
        audit.log_event(user, "auth.change_password", target=f"user:{user['id']}",
                        result="fail", message="bad-old-password", request=request,
                        status_code=400)
        raise HTTPException(400, "原密码不对")
    err = security.check_password_strength(body.new_password or "")
    if err:
        raise HTTPException(400, err)
    if security.verify_password(body.new_password or "", row["password_hash"]):
        raise HTTPException(400, "新密码不能和原密码一样")

    # 账号在服务器上，**先改服务器再改本地**。顺序反了的话，服务器改失败时
    # 用户会以为改成功了，下次登录用的还是旧密码，等于把自己锁在外面。
    tok = core.account_token()
    if tok:
        try:
            res = remote.change_password(tok, body.old_password or "",
                                         body.new_password or "")
            core.save_config({"account_token": res.get("token") or tok})
        except remote.RemoteError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        except remote.NetworkError as exc:
            raise HTTPException(
                503, "改密码要连服务器，现在连不上。请检查网络后重试。") from exc

    now = db.now_iso()
    with db.db() as c:
        c.execute("UPDATE users SET password_hash = ?, must_change_pwd = 0, "
                  "token_version = token_version + 1, updated_at = ? WHERE id = ?",
                  (security.hash_password(body.new_password), now, user["id"]))
        _revoke_all(c, int(user["id"]), "password-changed")
        urow = c.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    fresh = dict(urow)
    audit.log_event(public_user(fresh), "auth.change_password",
                    target=f"user:{fresh['id']}", request=request)
    # 当前设备继续用：直接发新的一对，前端替换掉本地的旧 token
    return {"ok": True, **(_issue(fresh, request))}


# ── 初始化 ────────────────────────────────────────────────────────────────
def ensure_admin() -> None:
    """库里一个用户都没有时创建一个管理员，否则登录页永远进不去。

    密码取 config.admin_init_password（默认 admin123456），并标 must_change_pwd=1，
    前端登录后会一直提示改密。**上线前务必改掉默认值**（文档里写了）。

    **分发版直接不建**（2026-11-04）：账号已经搬到服务器，本机这个 admin 没有
    任何作用，但会让每个装包的人都能用 `admin / admin123456` 登进来——
    等于全体用户共用一个默认密码。终端用户必须自己注册。
    """
    try:
        if core.load_config().get("dist_mode"):
            return
        with db.db() as c:
            n = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
            if n:
                return
            pwd = (core.load_config().get("admin_init_password") or "").strip() or "admin123456"
            now = db.now_iso()
            c.execute(
                """INSERT INTO users (username, password_hash, display_name, role,
                                      must_change_pwd, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                ("admin", security.hash_password(pwd), "管理员", "admin", 1, now, now))
        print(f"[auth] 已创建初始管理员：admin / {pwd}（请登录后立即改密）")
        audit.log_event(None, "admin.init", target="user:1", result="ok",
                        message="初始管理员已创建")
    except Exception as exc:  # noqa: BLE001
        print(f"[auth] 初始化管理员失败：{exc}")
