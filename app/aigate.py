"""AI 调用票据（grant）：用「服务端持有的 Key」换一张一次一用的短期授权。

### 为什么前端不该拿 Key

用户提出「生成引流分镜 / 生成剪辑结构 都由后端出 Key，不由用户自己填」。原因是
Key 一旦进浏览器就等于公开：F12、插件、录屏、粘贴给同事，都能拿到；一份 Key 被
刷走是整个账号的额度损失。而这个项目的 AI 调用**本来就发生在后端**
（`analyze._call` / `shots.shots_run`），前端需要的东西从来不是 Key 本身，
而是「这一步允许我调 AI」的许可。

### 所以这里做的是「凭证换票据」

```
前端  POST /api/ai/grant {project_id, purpose}
        ↳ 校验：登录身份 + 项目存在 + 服务端 AI 可用
        ↳ 返回：15 分钟有效的 grant（JWT，绑定 uid / pid / purpose / tv）
前端  带 X-AI-Grant 调 /plan 或 /shots
        ↳ 服务端验票通过才真正发 AI 请求
```

票据里带 `tv`（token_version）：封禁、改密、踢下线之后旧票据立刻失效，
不需要额外的黑名单表——和 access token 共用同一套失效机制。

### 为什么票据要绑定「项目 + 用途」

不绑定的话，一张票可以拿去刷任意项目、任意接口。绑了之后：
  · 同样的票重放到别的项目 → 403；
  · 拿「生成分镜」的票去生成结构 → 403；
  · 日志里每个项目各自一条授权记录，谁在哪个剧上花了 AI 调用一目了然。
"""
from __future__ import annotations

import secrets
import time

from fastapi import HTTPException

from . import core, security

# 只有这三步需要票据（2026-11-04 用户明确要求的范围，11-06 加钩子前置）：
# 第 2 步排结构、第 2 步单集钩子、第 3 步写分镜，都是「一次能烧几分钟、
# 几十次调用」的重活。
PURPOSES = {
    "plan": "生成剪辑结构",
    "hook": "生成单集钩子",
    "shots": "生成引流分镜",
}

GRANT_MINUTES = 15      # 票据有效期：够一次点击到任务启动，不够被人捡去长期刷


def server_ai() -> dict:
    """服务端这一单能用什么 AI。**永远不回 Key 本身**，只说「能不能用、用哪个模型」。

    两种可用形态：管理员在后台配了 Key（走自己的额度），或者没配 Key 走官方体验
    代理（按次扣体验额度）。两者都没有才是真的不可用。
    """
    cfg = core.llm()
    proxy = (cfg.get("proxy_url") or core.PROXY_URL_DEFAULT or "").strip()
    mode = "key" if cfg.get("key") else ("proxy" if proxy else "")
    return {
        "ready": bool(mode),
        "mode": mode,                       # key / proxy / ""（不可用）
        "provider": cfg.get("provider") or "bailian",
        "model": cfg.get("model") or "",
    }


def _token_version(uid: int) -> int:
    """取当前版本号：票据里带上它，封禁/改密后旧票自动作废。"""
    from . import db

    with db.db() as c:
        row = c.execute("SELECT token_version FROM users WHERE id = ?", (uid,)).fetchone()
    # sqlite3.Row 没有 .get()，只能按列名下标的写法取
    return int(row["token_version"] or 0) if row else 0


def issue(user: dict, project_id: str, purpose: str) -> dict:
    """签发一张票据。调用方（main.ai_grant）负责埋点。"""
    from . import auth  # 延迟导入：auth 顶层 import 了 audit，避免初始化期成环

    if purpose not in PURPOSES:
        raise HTTPException(400, f"未知的用途：{purpose}")
    project_id = (project_id or "").strip()
    if not project_id:
        raise HTTPException(400, "缺少项目 id")
    project = core.load(project_id)
    if not project:
        raise HTTPException(404, "项目不存在")

    st = server_ai()
    if not st["ready"]:
        raise HTTPException(
            409, "服务端还没有可用的 AI 凭证：请让管理员在「设置 → 服务端 AI 凭证」里配置，"
                 "或确认官方体验代理可连通")

    now = int(time.time())
    payload = {
        "sub": int(user["id"]),
        "uname": user.get("username") or "",
        "pid": project_id,
        "purpose": purpose,
        "tv": _token_version(int(user["id"])),
        "typ": "ai-grant",
        "iat": now,
        "nbf": now,
        "exp": now + GRANT_MINUTES * 60,
        "jti": secrets.token_hex(8),
    }
    return {
        "grant": security.jwt_encode(payload, auth.jwt_secret()),
        "expires_in": GRANT_MINUTES * 60,
        "purpose": purpose,
        "purpose_cn": PURPOSES[purpose],
        "provider": st["provider"],
        "model": st["model"],
        "mode": st["mode"],
        "project": {"id": project_id, "name": project.get("name") or project_id},
    }


def require(grant: str, project_id: str, purpose: str, user: dict) -> dict:
    """验票。不通过一律 403（不是 401：人已经登录了，是这张票不行）。"""
    from . import auth

    if not grant:
        raise HTTPException(
            403, "缺少 AI 授权：请从界面点「生成」，前端会先向后端申领一次性授权")
    try:
        payload = security.jwt_decode(grant, auth.jwt_secret())
    except security.TokenError as exc:
        why = "已过期" if str(exc) == "expired" else "无效或已被篡改"
        raise HTTPException(403, f"AI 授权{why}，请重新发起") from exc

    if payload.get("typ") != "ai-grant":
        raise HTTPException(403, "这不是 AI 授权票据")
    if payload.get("sub") != int(user["id"]):
        raise HTTPException(403, "这张授权不是发给当前账号的")
    if payload.get("pid") != project_id:
        raise HTTPException(403, "授权与当前项目不匹配，请重新发起")
    if payload.get("purpose") != purpose:
        raise HTTPException(403, "授权用途不匹配（比如拿分镜的票去排结构）")
    if int(payload.get("tv") or 0) != _token_version(int(user["id"])):
        raise HTTPException(403, "账号状态已变化（改密 / 被封禁 / 被踢下线），请重新登录")
    return payload
