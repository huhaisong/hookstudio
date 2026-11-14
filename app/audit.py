"""埋点与全局守卫：一个中间件同时做「鉴权」和「自动记录用户操作」。

为什么把两件事放一个中间件：
  · 顺序有强依赖——先要知道「这个请求是谁发的」，日志里才写得进用户；
    拆成两个中间件就得靠注册顺序保证（Starlette 后注册的在外层，很容易写反），
    而且会多做一次 token 解析 + 一次查库。
  · 低侵入：业务路由一行都不用改，只要挂上这一个中间件。

鉴权强度只有一种（`auth.auth_required()` 恒为 True，2026-11-04 用户定性）：
白名单以外没登录一律 401；带了凭证但已失效（过期/被封/被踢/改密）同样拦。
**「只审计不拦截」的免登录模式已整体废弃**——那个模式下封禁等于空头文书。

手动埋点：`log_event(...)`，业务代码需要更精确的动作名/失败原因时调一行。
target 里会带项目名（`project:剧名`），日志界面一眼看得出动的是哪个项目。
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware

from . import buildstamp, core, db, update

# ── 白名单：这些接口不登录也能访问 ────────────────────────────────────────
# 登录/注册/刷新是拿凭证的地方，把自己也拦掉就没人能登进来了。
# /api/status 给首页探活，不含任何用户数据。
PUBLIC_PATHS = {
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/refresh",
    "/api/status",
    "/api/models-available",   # 只读模型清单，无用户数据
    # <img src> 带不了 Authorization，没带凭证的 GET 一律会被第二道闸挡成 401。
    # 这个接口只回应用数据目录里的一张封面图（文件名白名单 + 固定目录），
    # 不读账号/队列/任何业务数据，所以按只读资源放行。
    "/api/publish/cover/preview",
    # 发表时页面要从本机取视频/封面图。页面里的 fetch 带不了 Authorization，
    # 只能走白名单；安全性靠地址里的 HMAC 签名（猜不出来），不是靠登录态。
    "/api/publish/media",
}

# ── 「还没改初始密码」时仍然放行的接口 ────────────────────────────────────
# 这是「更新前不能继续用」的第二道闸（第一道是版本闸，见 buildstamp.py）。
# 只留三类：读自己是谁（前端要知道该弹什么框）、改密码本身、退出登录、续期。
# 少了任何一个都会把人锁死：比如不放过 /api/auth/me，前端连「我该改密码」都不知道。
MUST_CHANGE_ALLOW = {
    "/api/auth/me",
    "/api/auth/password",
    "/api/auth/logout",
    "/api/auth/refresh",
}

# ── 业务动作名：按「方法 + 归一化路由」自动命名 ──────────────────────────
# 中间件只知道 HTTP 语义（POST /api/projects），不知道业务叫什么。
# 这张表把常用的翻译成人能读的动作名，日志筛选下拉里直接用它；
# 表里没有的接口 action 留空，靠业务代码手动埋点补。
ACTION_RULES: dict[tuple[str, str], str] = {
    ("POST", "/api/auth/register"): "auth.register",
    ("POST", "/api/auth/login"): "auth.login",
    ("POST", "/api/auth/refresh"): "auth.refresh",
    ("POST", "/api/auth/logout"): "auth.logout",
    ("PUT", "/api/auth/password"): "auth.change_password",
    ("PUT", "/api/auth/profile"): "auth.update_profile",
    ("POST", "/api/projects"): "project.create",
    ("DELETE", "/api/projects/{id}"): "project.delete",
    ("POST", "/api/projects/{id}/transcribe"): "project.transcribe",
    # 第 1 步手改某一集台词（校正错字 / 补漏句 / 改时间码）
    ("PUT", "/api/projects/{id}/lines"): "project.lines_edit",
    # 单集重转写（排障入口）：和整剧转写要分开，不然「转写」一列分不清是重跑哪集
    ("POST", "/api/projects/{id}/transcribe-ep"): "project.transcribe_ep",
    ("POST", "/api/projects/{id}/analyze"): "project.analyze",
    ("POST", "/api/projects/{id}/plan"): "plan.generate",
    # 第 2 步的第二种编排：单集钩子前置（一集一条成片）。与 plan.generate
    # 分开记——这两条产线的 AI 消耗和产物形态完全不同，混在一起看不出用户在走哪条。
    # 前缀故意用 hookcut. 而不是 plan.hookcut.：后台的动作筛选是**前缀匹配**，
    # plan.hook（改片头文案）会把 plan.hookcut* 一起吃掉，两个筛选项就串了。
    ("POST", "/api/projects/{id}/hookcut-plan"): "hookcut.generate",
    ("POST", "/api/projects/{id}/render"): "project.render",
    ("POST", "/api/projects/{id}/render-plans"): "plan.render",
    # 中止后台任务（2026-11-12）：转写/去字幕/编排/出片/分镜共用一条口子，
    # 后台要能看出「谁在哪一步主动停过」，和任务自己失败区分开。
    ("POST", "/api/projects/{id}/task/cancel"): "task.cancel",
    ("POST", "/api/projects/{id}/shots"): "shots.generate",
    # 免 AI 重排（改控制句开关 / 画风）：不花额度，但改了文档就换个样，
    # 后台要能看出「这一步人改过」，和重新生成区分开
    ("POST", "/api/projects/{id}/shots-layout"): "shots.layout",
    # 第 2/3 步的编辑动作（2026-11-06 补齐）：勾选要出哪几套、改片头文案、
    # 单套特效、整批默认值。这几条原来没有动作名，后台全是「—」。
    ("PUT", "/api/projects/{id}/plans-select"): "plan.select",
    ("PUT", "/api/projects/{id}/plan-hook"): "plan.hook",
    ("PUT", "/api/projects/{id}/plan-fx"): "plan.fx",
    ("PUT", "/api/projects/{id}/fx-default"): "fx.default",
    # 第 2 步：去字幕 + 加自己的字幕，产出已处理视频（2026-11-11）。纯本机编码，不占 AI 额度。
    ("PUT", "/api/projects/{id}/nosub-config"): "nosub.config",
    ("POST", "/api/projects/{id}/nosub"): "nosub.generate",
    ("POST", "/api/projects/{id}/nosub/open"): "nosub.open",
    ("POST", "/api/projects/{id}/nosub/preview"): "nosub.preview",
    # 批量定时发布（2026-11-13）：全部挂在 hookcut.publish 前缀下，后台按前缀
    # 一筛就能看到「谁在哪部剧上排了几条发布」。start / mark / 账号增删由业务
    # 代码手动埋点（要带条数和原因），这里只兜住没有手动埋点的几条。
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
    # 数据与体检（2026-11-13）：都是「用户主动去号里读一次」，有动作含义。
    # stat 的 GET 走 noisy 列表（切 Tab 就会读一次缓存），采样品和诊断才记。
    ("POST", "/api/publish/stat/sample"): "hookcut.publish.stat",
    ("POST", "/api/publish/diag/{id}"): "hookcut.publish.diag",
    # 开/关流速监控：会改本机后台是否持续去号里采，算配置变更
    ("POST", "/api/publish/monitor"): "hookcut.publish.monitor",
    # 扫码登录（2026-11-13）：出二维码 / 改账号名 是有动作含义的；
    # login-state 是每 2 秒一次的轮询，进 noisy 列表，不占后台日志。
    ("POST", "/api/publish/accounts/{id}/qr"): "hookcut.publish",
    ("PUT", "/api/publish/accounts/{id}"): "hookcut.publish.rename",
    # 手改剪辑表（第 3/4 步的文本框直接保存）
    ("PUT", "/api/projects/{id}/edl"): "edl.save",
    # 打开成片 / 打开项目文件夹：都是「用户去看产物了」，看得出交付环节走到哪一步
    ("POST", "/api/projects/{id}/open-video"): "video.open",
    ("POST", "/api/projects/{id}/open-folder"): "folder.open",
    # 第 4 步「两两合并」（2026-11-06 补）：纯本机渲染不花 AI 额度，但后台日志里
    # 一排光秃秃的 /api/merge/xxx 用户看不懂，按中间件自动命名给出动作名。
    # 仍不做手动埋点（不带项目 target——合并的对象是文件夹不是项目）。
    ("POST", "/api/merge/preview"): "merge.preview",
    ("POST", "/api/merge/start"): "merge.start",
    ("POST", "/api/merge/cancel"): "merge.cancel",
    ("POST", "/api/merge/open"): "merge.open",
    # AI 授权票据（2026-11-04）：前端生成前先申领，日志里就是「谁在哪个项目上要了授权」
    ("POST", "/api/ai/grant"): "ai.grant",
    ("PUT", "/api/config"): "config.update",
    # 设置页的「测一下这套 Key + 模型通不通」：排障时最需要看的一条
    ("POST", "/api/llm-test"): "config.test_model",
    ("POST", "/api/admin/users/{id}/ban"): "admin.user.ban",
    ("POST", "/api/admin/users/{id}/unban"): "admin.user.unban",
    ("POST", "/api/admin/users/{id}/kick"): "admin.user.kick",
    ("PUT", "/api/admin/users/{id}/role"): "admin.user.set_role",
    ("PUT", "/api/admin/users/{id}/password"): "admin.user.reset_password",
    ("PUT", "/api/admin/settings"): "admin.settings",
    # 导出日志是一次明确的管理动作（会把全站操作导出成 CSV），必须留痕
    ("GET", "/api/admin/logs/export"): "admin.logs.export",
}

# 脱敏：键名命中这些片段的，值一律替换。宁可多盖，也不能把 Key 写进日志。
SENSITIVE_KEYS = (
    "password", "passwd", "pwd", "secret", "token", "authorization",
    "cookie", "api_key", "apikey", "llm_key", "ds_key", "key",
    "id_card", "bank", "card_no",
)
MAX_PAYLOAD = 2000          # 单条 payload 落库上限（字符），再长截断
MAX_STR = 200               # 单个字段值上限


def client_ip(request: Request | None) -> str:
    """取真实客户端 IP。

    本机直连时是 127.0.0.1；挂在 Nginx/Caddy 后面时真实 IP 在 X-Forwarded-For
    的第一段（后续是代理链）。**只可信第一段**，后面的可以被客户端伪造。
    注意：如果直接暴露公网又没有可信反代，XFF 可以被伪造——那种部署要配
    `--forwarded-allow-ips` 限定来源。
    """
    if request is None:
        return ""
    xff = request.headers.get("x-forwarded-for") or ""
    if xff:
        return xff.split(",")[0].strip()[:64]
    real = request.headers.get("x-real-ip") or ""
    if real:
        return real.strip()[:64]
    try:
        return (request.client.host if request.client else "")[:64]
    except Exception:  # noqa: BLE001
        return ""


def user_agent(request: Request | None) -> str:
    if request is None:
        return ""
    return (request.headers.get("user-agent") or "")[:200]


def _mask_scalar(value):
    """字符串截断；邮箱/手机号部分打码。"""
    if not isinstance(value, str):
        return value
    s = value
    if "@" in s and "." in s.split("@")[-1] and len(s) <= 120:
        name, _, domain = s.partition("@")
        return (name[:1] + "***@" + domain) if name else s
    if s.isdigit() and 7 <= len(s) <= 15:
        keep = 3 if len(s) <= 11 else 4
        return s[:keep] + "****" + s[-2:]
    return s[:MAX_STR] + ("…" if len(s) > MAX_STR else "")


def mask(obj, depth: int = 0):
    """递归脱敏。

    三道闸：敏感键整值替换、字符串截断、层级/数量限深。
    为什么限深：恶意/异常客户端可以塞一个 1000 层嵌套的 JSON 把日志表撑爆，
    也能让 json.loads 之后的遍历卡住事件循环。
    """
    if depth > 3:
        return "…"
    if isinstance(obj, dict):
        out = {}
        for i, (k, v) in enumerate(obj.items()):
            if i >= 30:
                out["…"] = f"还有 {len(obj) - 30} 个字段"
                break
            low = str(k).lower()
            if any(t in low for t in SENSITIVE_KEYS):
                out[str(k)[:40]] = "***"
            else:
                out[str(k)[:40]] = mask(v, depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        items = [mask(v, depth + 1) for v in list(obj)[:10]]
        if len(obj) > 10:
            items.append(f"…共 {len(obj)} 项")
        return items
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return _mask_scalar(str(obj))


def dumps_masked(obj) -> str:
    """脱敏 + 序列化，并压到 MAX_PAYLOAD 以内。"""
    try:
        text = json.dumps(mask(obj), ensure_ascii=False, separators=(",", ":"))
    except Exception:  # noqa: BLE001
        return ""
    return text[:MAX_PAYLOAD] + ("…" if len(text) > MAX_PAYLOAD else "")


# 查询串里的令牌同样要盖掉：视频预览走 `?access_token=`（<video> 带不了请求头），
# 这个值会出现在 URL 里，不处理就等于把明文令牌写进日志表和反代访问日志。
_QUERY_SECRET = re.compile(r"(access_token|token|api_key|apikey|key|password)=([^&]*)", re.I)


def mask_query(query: str) -> str:
    return _QUERY_SECRET.sub(lambda m: f"{m.group(1)}=***", str(query or ""))[:300]


def log_event(user: dict | None, action: str, *,
              target: str = "", payload=None, result: str = "ok",
              message: str = "", request: Request | None = None,
              method: str = "", route: str = "", status_code: int = 200,
              duration_ms: int = 0) -> None:
    """手动埋点。业务代码里想记「谁对什么做了什么」就调这一行。

    例：`audit.log_event(me, "admin.user.ban", target=f"user:{uid}",
                        payload={"reason": reason}, request=request)`

    永不抛异常：埋点是旁路，写日志失败（磁盘满、库锁）绝不能把业务请求带崩。
    """
    # 打个标记：中间件看到它就不再写第二条。
    # 没有这个标记，登录/封禁这类「业务自己埋过点」的请求会留下两条日志——
    # 一条有用户名（业务的），一条 username 为空（中间件的），后台看着像两个人干的。
    if request is not None:
        try:
            request.state.audited = True
        except Exception:  # noqa: BLE001
            pass
    try:
        req = request
        with db.db() as c:
            c.execute(
                """INSERT INTO audit_logs
                   (created_at, user_id, username, role, method, path, route, query,
                    status_code, duration_ms, ip, ua, action, target, payload,
                    result, message, request_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    db.now_iso(),
                    (user or {}).get("id"),
                    (user or {}).get("username") or "",
                    (user or {}).get("role") or "",
                    method or (req.method if req else ""),
                    (req.url.path if req else "")[:300],
                    route or (route_of(req.url.path) if req else ""),
                    mask_query(req.url.query) if req else "",                    int(status_code or 0),
                    int(duration_ms or 0),
                    client_ip(req),
                    user_agent(req),
                    action[:60],
                    str(target)[:120],
                    dumps_masked(payload) if payload is not None else "",
                    result[:10],
                    str(message)[:300],
                    uuid.uuid4().hex[:12],
                ),
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[audit] 写日志失败（已忽略）：{exc}")


def route_of(path: str) -> str:
    """把实际路径归一成「路由模板」，用于聚合统计。

    没有现成的模板可用：中间件阶段路由还没匹配（匹配发生在 router 内部），
    所以按项目实际情况做启发式替换——数字、十六进制 id、UUID 段都换成 {id}。
    这样 `/api/projects/585dba7b8761/plan` 和 `/api/projects/779b3e51d3ae/plan`
    才能归成同一行统计，否则一部剧一个项目，Top 路由永远统计不出来。
    """
    parts = []
    for seg in (path or "").split("/"):
        if not seg:
            continue
        if seg.isdigit() or (len(seg) in (12, 24, 32, 36) and all(
                ch in "0123456789abcdefABCDEF-" for ch in seg)):
            parts.append("{id}")
        else:
            parts.append(seg)
    return "/" + "/".join(parts)


# 项目名缓存：一次出片任务会连打几十个请求，每次都读 project.json 没必要。
# 30 秒足够覆盖一次点击到任务启动的全过程；改了项目名最多 30 秒后生效。
_NAME_TTL = 30
_NAME_CACHE: dict[str, tuple[float, str]] = {}


def project_name(pid: str) -> str:
    """项目 id → 剧名。读不到就返回空串（日志照记，不能因为改名就丢日志）。"""
    if not pid:
        return ""
    now = time.time()
    hit = _NAME_CACHE.get(pid)
    if hit and now - hit[0] < _NAME_TTL:
        return hit[1]
    name = ""
    try:
        p = core.load(pid) or {}
        name = str(p.get("name") or "").strip()[:60]
    except Exception:  # noqa: BLE001
        name = ""
    if len(_NAME_CACHE) > 300:      # 长期运行别无限增长
        _NAME_CACHE.clear()
    _NAME_CACHE[pid] = (now, name)
    return name


def target_of(pid: str) -> str:
    """统一的目标写法：`project:剧名`。取不到名字退回 id，至少不丢线索。"""
    if not pid:
        return ""
    name = project_name(pid)
    return f"project:{name}" if name else f"project:{pid}"


def _target_from_body(body) -> str:
    """路径里没有 id 时，从请求体里找项目 id（`POST /api/ai/grant` 就是这种）。

    否则「申领授权被拒」这类记录只能记一个光秃秃的路由，看不出是哪个项目。
    """
    if isinstance(body, dict):
        pid = body.get("project_id") or body.get("pid") or ""
        if isinstance(pid, str) and pid.strip():
            return target_of(pid.strip())
    return ""


def _extract_target(path: str) -> str:
    """从路径里挖出操作对象，日志列表里一眼能看出「动的是哪个项目」。

    2026-11-04 用户要求「把项目名称也一起带过来」：原来只记 12 位 id，
    后台看到的是 `585dba7b8761`，还得自己去项目列表里对照。现在记 `project:剧名`。
    """
    for seg in (path or "").split("/"):
        if len(seg) in (12, 24, 32) and all(ch in "0123456789abcdef" for ch in seg):
            return target_of(seg)
    return ""


# ── 全局中间件 ─────────────────────────────────────────────────────────────
# 拒绝原因的中文对照：日志的「说明」列是给人看的，机器码看不懂
# （security.TokenError 会抛 malformed/decode/alg/signature/payload/no-exp/expired/not-yet）
_REJECT_CN = {
    "banned": "账号已被封禁", "revoked": "令牌已作废（被踢下线或改过密码）",
    "expired": "令牌已过期", "not-yet": "令牌尚未生效", "no-exp": "令牌缺少过期时间",
    "malformed": "令牌格式不对", "decode": "令牌解码失败（伪造或被篡改）",
    "alg": "令牌算法被篡改", "signature": "令牌签名不符", "payload": "令牌内容异常",
    "no-token": "未带凭证", "user-missing": "用户不存在",
    "wrong-type": "令牌类型不对", "bad-sub": "令牌主体异常", "db-error": "查库失败",
}


class GuardMiddleware(BaseHTTPMiddleware):
    """鉴权 + 审计。只关心 /api/，静态资源和首页直接放行（否则登录页都打不开）。"""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/"):
            resp = await call_next(request)
            if path.startswith("/static/"):
                # 页面里的引用已经带 ?v=<指纹> 了，这里再加一道「必须回源校验」：
                # 万一有人的浏览器把 HTML 缓存住、引用的是老 URL，也不会拿到 304 的旧体。
                resp.headers.setdefault("Cache-Control", "no-cache, must-revalidate")
            # 每个响应都盖一个构建指纹，前端 fetch 时顺手比对（见 web/version.js）
            resp.headers["X-App-Build"] = buildstamp.current()
            return resp

        # 延迟导入：auth 顶层 import 了本模块的 log_event，
        # 这里再在顶层 import auth 就成环了。中间件是运行时才执行，放函数里没有代价。
        from . import auth as auth_mod

        started = time.perf_counter()
        req_id = uuid.uuid4().hex[:12]
        user, auth_err = None, ""
        token = auth_mod.token_from_headers(request)
        if token:
            user, auth_err = auth_mod.resolve_token(token)

        # ── 第一道：带了凭证但凭证已经作废 ──────────────────────────────
        # 过期、被踢、被封、改过密码——只要带了这样一个 token 就一律 401，
        # **不管 auth_required 开没开**。
        # 为什么这一条必须有：只审计模式下业务接口本来就不要求登录，如果这里
        # 把「失效 token」当成「未登录」直接放行，封禁就成了一张空头文书——
        # 被封的人拿着旧 token 照样能调接口，直到两周后它自然过期。
        # 白名单例外：登录/刷新/注册本身就是拿凭证的地方，不能被旧凭证挡住。
        if token and not user and auth_err and auth_err != "no-token" \
                and path not in PUBLIC_PATHS:
            code = 403 if auth_err == "banned" else 401
            if auth_err == "banned":
                reason = auth_mod.ban_reason_by_token(token)
                msg = "账号已被封禁" + ("：" + reason if reason else "，请联系管理员")
            else:
                msg = "登录已过期，请重新登录"
            log_event(None, "auth.reject", request=request, method=request.method,
                      route=route_of(path), status_code=code, result="fail",
                      message=_REJECT_CN.get(auth_err, auth_err),
                      duration_ms=int((time.perf_counter() - started) * 1000))
            return JSONResponse({"detail": msg, "code": auth_err,
                                 "request_id": req_id}, status_code=code)

        need_auth = auth_mod.auth_required() and path not in PUBLIC_PATHS

        # 第二道：强制登录模式下，压根没带凭证也拦
        if need_auth and not user:
            # <video>/<img> 标签自发请求没法带头，压根没带 token 的 GET 撞 401
            # 不是用户动作，不入库——否则视频预览一放就是一排「登录被拒」刷屏。
            # 带着**失效** token 来的仍然要记，那是有价值的线索（被踢/改密/过期）。
            if not (request.method == "GET" and auth_err in ("", "no-token")):
                log_event(None, "auth.reject", request=request, method=request.method,
                          route=route_of(path), status_code=401, result="fail",
                          message=auth_err or "no-token",
                          duration_ms=int((time.perf_counter() - started) * 1000))
            return JSONResponse({"detail": "未登录或登录已过期", "code": "unauthorized",
                                 "request_id": req_id}, status_code=401)

        # ── 第三道：还在用初始密码（或管理员重置过的密码）→ 只能改密，别的一律不许 ──
        # 为什么要在中间件拦、而不是只在前端弹窗提示：
        # 「提示」是可以被无视的（关掉页面、换个标签页、直接 curl）。用户要的是
        # **更新之后才可继续使用**，那就得是硬拦。改完密码 must_change_pwd 归零，
        # 这条自动失效，不需要任何额外状态。
        if user and int(user.get("must_change_pwd") or 0) \
                and path not in MUST_CHANGE_ALLOW and path not in PUBLIC_PATHS:
            log_event(user, "auth.reject", request=request, method=request.method,
                      route=route_of(path), status_code=403, result="fail",
                      message="需先修改初始密码",
                      duration_ms=int((time.perf_counter() - started) * 1000))
            return JSONResponse({
                "detail": "你还在使用初始密码，请先修改密码再继续使用",
                "code": "must-change-pwd", "request_id": req_id,
            }, status_code=403)

        # ── 第四道：服务器要求的最低版本高于本机 → 除登录/探活外全部拦死 ──
        # 为什么本地指纹闸管不了这一档：exe 分发时，用户电脑上的页面和后端**永远**
        # 是同一版（都来自他那份安装包），本地比对一次都不会触发。只有服务器说了算
        # 才能实现「必须更新之后才可继续使用」。没问到版本（离线）时不拦，见 update.py。
        up = update.need()
        if up and path not in PUBLIC_PATHS:
            log_event(user, "auth.reject", request=request, method=request.method,
                      route=route_of(path), status_code=426, result="fail",
                      message=f"版本过低（{update.local_version()} < {up['min']}），需升级",
                      duration_ms=int((time.perf_counter() - started) * 1000))
            return JSONResponse({
                "detail": f"当前版本 {update.local_version()} 已不再支持，请升级到 {up['latest']} 后继续使用",
                "code": "need-upgrade", "min": up["min"], "latest": up["latest"],
                "url": up["url"], "note": up["note"], "request_id": req_id,
            }, status_code=426, headers={"X-App-Upgrade": up["min"],
                                          "X-App-Build": buildstamp.current()})

        # 读请求体做参数埋点。只处理 JSON 且限制大小：
        # 视频上传（multipart）和超大 body 一律不读——读进内存再重放是拿内存换日志，
        # 不值当，而且大 body 重放很容易把上传接口搞坏。
        body_info = None
        ctype = (request.headers.get("content-type") or "").lower()
        if "application/json" in ctype:
            try:
                raw = await request.body()
            except Exception:  # noqa: BLE001
                raw = b""
            if raw and len(raw) <= 8192:
                try:
                    body_info = json.loads(raw)
                except Exception:  # noqa: BLE001
                    body_info = {"_raw": raw[:200].decode("utf-8", "replace")}

                # 关键：body 已经被读走，下游再读就是空。必须把流重放一遍。
                async def _receive(body=raw):
                    return {"type": "http.request", "body": body, "more_body": False}

                request = Request(request.scope, _receive)

        # 把用户挂到 scope["state"]：下游依赖直接复用，省一次查库
        request.state.user = user
        request.state.request_id = req_id
        request.state.auth_error = auth_err

        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001
            # 业务炸了也要留一条：否则日志里只剩成功的请求，排查时看不到现场
            log_event(user, ACTION_RULES.get((request.method, route_of(path)), ""),
                      request=request, method=request.method, route=route_of(path),
                      payload=body_info, status_code=500, result="fail",
                      message=str(exc)[:200],
                      duration_ms=int((time.perf_counter() - started) * 1000))
            raise

        cost = int((time.perf_counter() - started) * 1000)
        route = route_of(path)
        # 前端每个响应都要能看出「这是哪一版后端给的」——版本闸靠它即时发现
        # 「服务器已经更新，页面还是旧的」（web/version.js 的 fetch 包装在读它）
        response.headers["X-App-Build"] = buildstamp.current()
        action = getattr(request.state, "action", "") or ACTION_RULES.get(
            (request.method, route), "")
        # 只记「有信息量」的请求：
        #   GET 只记按用户/项目的查询（列表接口），高频的状态轮询（/api/status、
        #   /api/projects/{id} 每 1.5 秒一次）记进去会把日志淹掉，查什么也查不出来；
        #   弹系统文件框（pick-folder/pick-file）任何方法都不记。
        if not getattr(request.state, "force_audit", False):
            if (request.method == "GET" and not _worth_logging_get(route)) \
                    or route in _SILENT_ROUTES:
                response.headers["X-Request-Id"] = req_id
                return response

        # 业务层已经手动埋过（登录/封禁这类，动作名和失败原因更准）：
        # 不再重复写一条 username 为空的记录
        if not getattr(request.state, "audited", False):
            log_event(user, action, request=request, method=request.method, route=route,
                      target=(getattr(request.state, "target", "")
                              or _extract_target(path) or _target_from_body(body_info)),
                      payload=body_info,
                      status_code=response.status_code,
                      result="ok" if response.status_code < 400 else "fail",
                      duration_ms=cost)
        response.headers["X-Request-Id"] = req_id
        return response


def _worth_logging_get(route: str) -> bool:
    """GET 请求要不要入库。

    三类不记：
    1. 轮询类（状态、任务进度、对话/台词全文预览）——一次出片任务打上百次；
    2. 看板自查询（管理后台每切一次 Tab 五连请求，会把真用户操作挤出第一页）；
    3. 页面加载顺带刷的普通查询（项目列表、plan 全文）——2026-11-05 用户明确
       「非必要就过滤掉」，这类请求没有动作名，进了后台只是一排「—」。
    """
    noisy = (
        # 高频轮询
        "/api/status", "/api/projects/{id}", "/api/config", "/api/roots",
        "/api/browse", "/api/models-available", "/api/projects/{id}/transcript",
        "/api/projects/{id}/lines", "/api/projects/{id}/video", "/api/merge/status",
        # 页面加载顺带刷的查询（无动作名，后台纯噪音）
        # 曲库状态也是开设置页时读一次，只读、无副作用，别占后台日志
        "/api/projects", "/api/projects/{id}/plan", "/api/bgm/library",
        # 后台/前端的身份与看板自查询（读，无副作用）
        "/api/auth/me", "/api/admin/users", "/api/admin/logs",
        "/api/admin/logs/filters", "/api/admin/logs/stats", "/api/admin/settings",
        # 发布计划：抽屉一打开就拉一次，角标每 15 秒轮询一次（2026-11-13）。
        # 真正有动作含义的（加/排/标已发）是 POST，那些都挂着 hookcut.publish*。
        "/api/publish/accounts", "/api/publish/overview",
        "/api/publish/queue", "/api/publish/projects",
        "/api/projects/{id}/publish/sets",
        # 扫码时前端每 2 秒问一次登录好了没（2026-11-13）
        "/api/publish/accounts/{id}/login-state",
        # 数据页 / 诊断页一进去就读上一次的缓存（2026-11-13）：
        # 纯读本地文件，无副作用；真去号里采一次是 POST，那些记日志
        "/api/publish/stat", "/api/publish/diag",
        # 流速监控的状态：数据页一进去就问一次，纯读内存
        "/api/publish/monitor",
        # 选好号后自动预取候选（2026-11-13）：一进页面就发，没动作含义。
        # 真正有价值的那条由处理函数手动记 op=options_all，别让中间件再记一条无名 GET。
        "/api/publish/options/all",
        # 账号状态对账（2026-11-13）：页面打开时问一次「这个号到底登录了没」。
        # 中间件不记（一次开页一条，噪音）；真的把「待扫码」纠正成「已登录」时，
        # 由处理函数手动埋一条带 op=reconcile 的日志——那才是用户关心的事。
        "/api/publish/accounts/state",
    )
    # 提示词原文读取：一进设置页就 GET 一次，读的是模板文本，没有任何动作含义。
    # 路由里的 key 是 plans/shots（不是 id），没法写进上面的固定列表，单独按前缀判断。
    # 注意 PUT/DELETE 同路由是真的在改模板，那些走手动埋点，不受这里影响。
    if route.startswith("/api/prompts/"):
        return False
    return route not in noisy


# 任何方法都不记的接口：弹系统文件框（选文件夹/选音乐/选要发布的视频），没有审计价值。
# /api/publish/media 是发表时页面回本机取视频/封面的通道（带 HMAC 签名），
# 它是文件字节流而不是用户动作，记进去只会把后台日志刷成一条条 media。
_SILENT_ROUTES = ("/api/pick-folder", "/api/pick-file", "/api/publish/pick-files",
                  "/api/publish/pick-folder", "/api/publish/pick-cover",
                  "/api/publish/cover/preview", "/api/publish/_probe",
                  "/api/publish/media")


# ── 管理员：日志查询 ───────────────────────────────────────────────────────
router = APIRouter(prefix="/api/admin", tags=["admin-audit"])


def _where(q: dict) -> tuple[str, list]:
    """把筛选条件拼成 SQL。全部走参数绑定，不做字符串拼接——防注入是底线。"""
    cond, args = ["1=1"], []
    if q.get("user_id"):
        cond.append("user_id = ?")
        args.append(int(q["user_id"]))
    if q.get("username"):
        cond.append("username LIKE ?")
        args.append(f"%{q['username']}%")
    if q.get("action"):
        cond.append("action LIKE ?")
        args.append(f"{q['action']}%")
    if q.get("route"):
        cond.append("route LIKE ?")
        args.append(f"%{q['route']}%")
    if q.get("method"):
        cond.append("method = ?")
        args.append(q["method"].upper())
    if q.get("status"):
        cond.append("status_code = ?")
        args.append(int(q["status"]))
    if q.get("fail_only"):
        cond.append("status_code >= 400")
    if q.get("from"):
        cond.append("created_at >= ?")
        args.append(q["from"])
    if q.get("to"):
        cond.append("created_at <= ?")
        args.append(q["to"])
    if q.get("q"):
        cond.append("(path LIKE ? OR payload LIKE ? OR message LIKE ? OR target LIKE ?)")
        like = f"%{q['q']}%"
        args += [like, like, like, like]
    return " AND ".join(cond), args


@router.get("/logs")
def list_logs(page: int = 1, size: int = 20, user_id: int = 0, username: str = "",
              action: str = "", route: str = "", method: str = "", status: int = 0,
              fail_only: int = 0, date_from: str = "", date_to: str = "", q: str = "",
              request: Request = None):
    """日志列表（管理员）。时间倒序，支持按用户/动作/路由/状态码/关键字筛选。"""
    from . import auth  # 延迟导入避免环
    auth.require_admin_request(request)
    limit, offset = db.page_args(page, size)
    where, args = _where({"user_id": user_id, "username": username, "action": action,
                          "route": route, "method": method, "status": status,
                          "fail_only": fail_only, "from": date_from, "to": date_to, "q": q})
    with db.db() as c:
        total = c.execute(f"SELECT COUNT(*) AS n FROM audit_logs WHERE {where}",
                          args).fetchone()["n"]
        rows = c.execute(
            f"SELECT * FROM audit_logs WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            args + [limit, offset]).fetchall()
    return {"items": db.rowslist(rows), "total": total, "page": max(1, page),
            "size": limit, "pages": (total + limit - 1) // limit}


@router.get("/logs/filters")
def log_filters(request: Request = None):
    """筛选下拉的候选值：现成的动作名 + 最近出现过的路由。"""
    from . import auth
    auth.require_admin_request(request)
    with db.db() as c:
        actions = [r["action"] for r in c.execute(
            "SELECT action, COUNT(*) n FROM audit_logs WHERE action <> '' "
            "GROUP BY action ORDER BY n DESC LIMIT 60")]
        routes = [r["route"] for r in c.execute(
            "SELECT route, COUNT(*) n FROM audit_logs WHERE route <> '' "
            "GROUP BY route ORDER BY n DESC LIMIT 60")]
        users = db.rowslist(c.execute(
            "SELECT user_id, username, COUNT(*) n FROM audit_logs WHERE user_id IS NOT NULL "
            "GROUP BY user_id ORDER BY n DESC LIMIT 50"))
    return {"actions": actions, "routes": routes, "users": users}


@router.get("/logs/stats")
def log_stats(days: int = 7, request: Request = None):
    """概览：总量、失败量、活跃用户、按天曲线、Top 动作 / 路由 / 用户。"""
    from . import auth
    auth.require_admin_request(request)
    days = max(1, min(90, days))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with db.db() as c:
        total = c.execute("SELECT COUNT(*) n FROM audit_logs WHERE created_at >= ?",
                          (since,)).fetchone()["n"]
        fails = c.execute("SELECT COUNT(*) n FROM audit_logs WHERE created_at >= ? "
                          "AND status_code >= 400", (since,)).fetchone()["n"]
        users = c.execute("SELECT COUNT(DISTINCT user_id) n FROM audit_logs "
                          "WHERE created_at >= ? AND user_id IS NOT NULL",
                          (since,)).fetchone()["n"]
        # substr 取日期部分做按天聚合：created_at 是定长 ISO，切前 10 位就是 YYYY-MM-DD
        by_day = db.rowslist(c.execute(
            "SELECT substr(created_at,1,10) d, COUNT(*) n, "
            "SUM(CASE WHEN status_code>=400 THEN 1 ELSE 0 END) f "
            "FROM audit_logs WHERE created_at >= ? GROUP BY d ORDER BY d", (since,)))
        top_actions = db.rowslist(c.execute(
            "SELECT action, COUNT(*) n FROM audit_logs WHERE created_at >= ? "
            "AND action <> '' GROUP BY action ORDER BY n DESC LIMIT 10", (since,)))
        top_routes = db.rowslist(c.execute(
            "SELECT route, COUNT(*) n FROM audit_logs WHERE created_at >= ? "
            "GROUP BY route ORDER BY n DESC LIMIT 10", (since,)))
        top_users = db.rowslist(c.execute(
            "SELECT username, COUNT(*) n FROM audit_logs WHERE created_at >= ? "
            "AND username <> '' GROUP BY username ORDER BY n DESC LIMIT 10", (since,)))
    return {"days": days, "total": total, "failed": fails, "active_users": users,
            "by_day": by_day, "top_actions": top_actions,
            "top_routes": top_routes, "top_users": top_users}


@router.get("/logs/export")
def export_logs(user_id: int = 0, action: str = "", route: str = "", method: str = "",
                status: int = 0, fail_only: int = 0,
                date_from: str = "", date_to: str = "", q: str = "",
                request: Request = None):
    """导出 CSV（上限 5 万行）。

    带 UTF-8 BOM：Excel 不认没有 BOM 的 UTF-8，双击打开会是乱码。
    """
    from . import auth
    auth.require_admin_request(request)
    where, args = _where({"user_id": user_id, "action": action, "route": route,
                          "method": method, "status": status,
                          "fail_only": fail_only, "from": date_from, "to": date_to, "q": q})
    with db.db() as c:
        rows = c.execute(
            f"SELECT * FROM audit_logs WHERE {where} ORDER BY id DESC LIMIT 50000",
            args).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["时间(UTC)", "用户ID", "用户名", "角色", "动作", "方法", "路由",
                "实际路径", "状态码", "耗时ms", "IP", "结果", "说明", "对象", "参数", "UA"])
    for r in rows:
        w.writerow([r["created_at"], r["user_id"] or "", r["username"], r["role"],
                    r["action"], r["method"], r["route"], r["path"], r["status_code"],
                    r["duration_ms"], r["ip"], r["result"], r["message"], r["target"],
                    r["payload"], r["ua"]])
    data = ("\ufeff" + buf.getvalue()).encode("utf-8")
    name = f"audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(iter([data]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})
