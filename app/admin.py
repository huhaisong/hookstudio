"""管理员后台接口：用户管理 + 系统开关。

所有接口都挂 `require_admin` 依赖（= 已登录 + role=admin），并且**每个写操作都
写一条审计**。这里不做任何"悄悄执行"的捷径：谁在什么时候封了谁、理由是什么，
就是被封用户来问的时候唯一能拿出来的东西。

三条护栏（都是防自己把自己锁在门外）：
  ① 不能封禁自己；
  ② 不能封禁 / 降级**最后一个**管理员——否则没人能再进后台解封；
  ③ 封禁后 tokens version +1 + 撤销全部 refresh，旧 token 立即失效。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import audit, auth, core, db, security

router = APIRouter(prefix="/api/admin", tags=["admin"])


class RoleBody(BaseModel):
    role: str          # user / admin


class BanBody(BaseModel):
    reason: str = ""   # 封禁理由，会写进用户表和审计日志
    notify: bool = True


class ResetPwdBody(BaseModel):
    new_password: str
    force_change: bool = True


class SettingsBody(BaseModel):
    auth_required: bool | None = None
    access_minutes: int | None = None
    refresh_days: int | None = None
    admin_init_password: str | None = None


def _admin_row(uid: int) -> dict:
    with db.db() as c:
        row = c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not row:
        raise HTTPException(404, "用户不存在")
    return dict(row)


def _admin_view(row: dict, last_active: str = "") -> dict:
    """管理员看到的用户视图：多出封禁信息，**依然没有 password_hash**。"""
    out = auth.public_user(row)
    out.update({
        "ban_reason": row.get("ban_reason") or "",
        "banned_at": row.get("banned_at"),
        "banned_by": row.get("banned_by"),
        "token_version": int(row.get("token_version") or 0),
        "last_login_ip": row.get("last_login_ip") or "",
        "last_active": last_active,
    })
    return out


def _active_admins(c, exclude: int = 0) -> int:
    return c.execute(
        "SELECT COUNT(*) n FROM users WHERE role = 'admin' AND status = 'active' "
        "AND id <> ?", (exclude,)).fetchone()["n"]


@router.get("/users")
def list_users(request: Request, page: int = 1, size: int = 20, q: str = "",
               role: str = "", status: str = ""):
    """用户列表：搜索（用户名/昵称/邮箱）+ 角色/状态筛选 + 分页。"""
    auth.require_admin(request)
    limit, offset = db.page_args(page, size)
    cond, args = ["1=1"], []
    if q:
        cond.append("(username LIKE ? OR display_name LIKE ? OR email LIKE ?)")
        like = f"%{q}%"
        args += [like, like, like]
    if role in ("user", "admin"):
        cond.append("role = ?")
        args.append(role)
    if status in ("active", "banned"):
        cond.append("status = ?")
        args.append(status)
    where = " AND ".join(cond)
    with db.db() as c:
        total = c.execute(f"SELECT COUNT(*) n FROM users WHERE {where}", args).fetchone()["n"]
        rows = c.execute(
            f"""SELECT u.*,
                       (SELECT MAX(created_at) FROM audit_logs a WHERE a.user_id = u.id)
                       AS last_active
                FROM users u WHERE {where} ORDER BY u.id DESC LIMIT ? OFFSET ?""",
            args + [limit, offset]).fetchall()
        counts = {
            "total": c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"],
            "banned": c.execute("SELECT COUNT(*) n FROM users WHERE status='banned'")
                        .fetchone()["n"],
            "admins": c.execute("SELECT COUNT(*) n FROM users WHERE role='admin'")
                        .fetchone()["n"],
        }
    return {"items": [_admin_view(dict(r), r["last_active"] or "") for r in rows],
            "total": total, "page": max(1, page), "size": limit,
            "pages": (total + limit - 1) // limit, "counts": counts}


@router.put("/users/{uid}/role")
def set_role(uid: int, body: RoleBody, request: Request):
    me = auth.require_admin(request)
    role = (body.role or "").strip()
    if role not in ("user", "admin"):
        raise HTTPException(400, "角色只能是普通用户或管理员")
    row = _admin_row(uid)
    if row["role"] == role:
        return {"ok": True, "user": _admin_view(row)}
    if role == "user" and row["role"] == "admin" and _active_admins_except(uid) < 1:
        raise HTTPException(400, "至少保留一个管理员，不能降级最后一个")
    if uid == me["id"] and role != "admin":
        raise HTTPException(400, "不能降级自己")
    with db.db() as c:
        c.execute("UPDATE users SET role = ?, updated_at = ? WHERE id = ?",
                  (role, db.now_iso(), uid))
        # 角色变了就重签：旧 token 里还带着旧 role，不改版本号的话权限判断会滞后
        c.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?", (uid,))
        fresh = dict(c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())
    audit.log_event(me, "admin.user.set_role", target=f"user:{uid}",
                    payload={"from": row["role"], "to": role, "username": row["username"]},
                    request=request)
    return {"ok": True, "user": _admin_view(fresh)}


def _active_admins_except(uid: int) -> int:
    with db.db() as c:
        return _active_admins(c, exclude=uid)


def _enforce_login_wall(me: dict, request: Request, why: str) -> bool:
    """历史遗留：曾经在「免登录模式」下，封禁/踢下线要顺手把登录墙打开。

    2026-11-04 用户把产品定性为「需要登录」，`auth.auth_required()` 恒为 True，
    这堵墙从一开始就是立着的，所以这里永远返回 False——**保留函数只为让
    封禁/踢下线的返回值（`auth_required_enabled`）字段不消失**，前端不用改判断。

    为什么当初需要它（留作教训）：只审计不拦截时，被封的人把本地 token 清掉
    （或重开窗口）就变成匿名访问 → 200，后台显示「已封禁」人却照样在用。
    """
    return False


@router.post("/users/{uid}/ban")
def ban_user(uid: int, body: BanBody, request: Request):
    """封禁。

    做了三件事，缺一不可：
      ① status='banned' + 记录理由/时间/操作人（后台和用户申诉时都要看）；
      ② token_version + 1 —— 手里那个还没过期的 access token 立刻作废；
      ③ 撤销该用户所有 refresh token —— 否则他还能拿 refresh 换新的 access。
    """
    me = auth.require_admin(request)
    if uid == me["id"]:
        raise HTTPException(400, "不能封禁自己")
    row = _admin_row(uid)
    if row["status"] == "banned":
        return {"ok": True, "user": _admin_view(row)}
    if row["role"] == "admin" and _active_admins_except(uid) < 1:
        raise HTTPException(400, "至少保留一个管理员，不能封禁最后一个")
    now = db.now_iso()
    reason = (body.reason or "").strip()[:200] or "违反使用规则"
    with db.db() as c:
        c.execute(
            """UPDATE users SET status='banned', ban_reason=?, banned_at=?, banned_by=?,
                                token_version = token_version + 1, updated_at = ?
               WHERE id = ?""", (reason, now, me["id"], now, uid))
        c.execute("UPDATE refresh_tokens SET revoked_at = ?, revoked_reason = 'banned' "
                  "WHERE user_id = ? AND revoked_at IS NULL", (now, uid))
        fresh = dict(c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())
    audit.log_event(me, "admin.user.ban", target=f"user:{uid}",
                    payload={"username": row["username"], "reason": reason},
                    request=request)
    wall = _enforce_login_wall(me, request, "ban")
    return {"ok": True, "user": _admin_view(fresh), "auth_required_enabled": wall}


@router.post("/users/{uid}/unban")
def unban_user(uid: int, request: Request):
    """解封。token_version 再 +1：解封不是"恢复原状"——
    封禁期间泄露的旧 token 不该因为解封就重新可用。"""
    me = auth.require_admin(request)
    row = _admin_row(uid)
    if row["status"] == "active":
        return {"ok": True, "user": _admin_view(row)}
    now = db.now_iso()
    with db.db() as c:
        c.execute(
            """UPDATE users SET status='active', ban_reason='', banned_at=NULL, banned_by=NULL,
                                token_version = token_version + 1, fail_count = 0,
                                locked_until = NULL, updated_at = ?
               WHERE id = ?""", (now, uid))
        fresh = dict(c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())
    audit.log_event(me, "admin.user.unban", target=f"user:{uid}",
                    payload={"username": row["username"],
                             "last_reason": row.get("ban_reason") or ""},
                    request=request)
    return {"ok": True, "user": _admin_view(fresh)}


@router.post("/users/{uid}/kick")
def kick_user(uid: int, request: Request):
    """踢下线：不封号，只让所有设备立刻退出登录。"""
    me = auth.require_admin(request)
    row = _admin_row(uid)
    now = db.now_iso()
    with db.db() as c:
        c.execute("UPDATE users SET token_version = token_version + 1, updated_at = ? "
                  "WHERE id = ?", (now, uid))
        c.execute("UPDATE refresh_tokens SET revoked_at = ?, revoked_reason = 'kicked' "
                  "WHERE user_id = ? AND revoked_at IS NULL", (now, uid))
        fresh = dict(c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())
    audit.log_event(me, "admin.user.kick", target=f"user:{uid}",
                    payload={"username": row["username"]}, request=request)
    # 踢下线同理：免登录模式下踢完人家回到首页照样用，等于没踢
    wall = _enforce_login_wall(me, request, "kick")
    return {"ok": True, "user": _admin_view(fresh), "auth_required_enabled": wall}


@router.put("/users/{uid}/password")
def reset_password(uid: int, body: ResetPwdBody, request: Request):
    """管理员重置密码（用户忘了密码时唯一的救济途径：本机工具没做邮件找回）。"""
    me = auth.require_admin(request)
    err = security.check_password_strength(body.new_password)
    if err:
        raise HTTPException(400, err)
    row = _admin_row(uid)
    now = db.now_iso()
    with db.db() as c:
        c.execute(
            """UPDATE users SET password_hash = ?, must_change_pwd = ?, fail_count = 0,
                                locked_until = NULL, token_version = token_version + 1,
                                updated_at = ? WHERE id = ?""",
            (security.hash_password(body.new_password), 1 if body.force_change else 0,
             now, uid))
        c.execute("UPDATE refresh_tokens SET revoked_at = ?, revoked_reason = 'pwd-reset' "
                  "WHERE user_id = ? AND revoked_at IS NULL", (now, uid))
    # 审计里绝不记密码本身，只记"谁重置了谁的密码"
    audit.log_event(me, "admin.user.reset_password", target=f"user:{uid}",
                    payload={"username": row["username"],
                             "force_change": bool(body.force_change)}, request=request)
    return {"ok": True}


@router.get("/settings")
def get_settings(request: Request):
    """后端开关，管理后台用。只回显安全相关的键，API Key 不在这里。"""
    auth.require_admin(request)
    cfg = core.load_config()
    return {
        # 恒为 True：登录模式是本版本的固定行为，不再提供关闭入口
        "auth_required": auth.auth_required(),
        "auth_required_fixed": True,
        "access_minutes": auth.access_minutes(),
        "refresh_days": auth.refresh_days(),
        "admin_init_password_set": bool(cfg.get("admin_init_password")),
        "db_file": db.DB_FILE,
    }


@router.put("/settings")
def put_settings(body: SettingsBody, request: Request):
    """改令牌有效期 / 初始密码。

    `auth_required` 字段仍然收（老前端会传），但**不生效**：2026-11-04 用户定性
    本产品必须登录，免登录模式已整体删除（见 app/auth.py 的 auth_required）。
    """
    me = auth.require_admin(request)
    patch: dict = {}
    if body.access_minutes is not None:
        patch["access_minutes"] = max(1, min(1440, int(body.access_minutes)))
    if body.refresh_days is not None:
        patch["refresh_days"] = max(1, min(90, int(body.refresh_days)))
    if body.admin_init_password is not None and body.admin_init_password.strip():
        err = security.check_password_strength(body.admin_init_password)
        if err:
            raise HTTPException(400, err)
        patch["admin_init_password"] = body.admin_init_password.strip()
    if not patch:
        # 老前端可能只传 auth_required（想关掉登录模式）。这不是错误，只是不生效——
        # 回一句人话 + 当前值，别甩 400 让人以为接口坏了。
        if body.auth_required is not None:
            return {"ok": True, "ignored": ["auth_required"],
                    "note": "登录模式已固定启用，不能关闭",
                    **get_settings(request)}
        raise HTTPException(400, "没有要改的项")
    core.save_config(patch)
    audit.log_event(me, "admin.settings", payload=patch, request=request)
    return {"ok": True, **get_settings(request)}
