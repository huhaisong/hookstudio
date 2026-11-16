"""批量定时发布的路由层（P1：不含浏览器自动化）。

拆成独立文件，沿用项目里 auth / admin / audit 的写法：main.py 只做
`include_router`，业务路由一行不动。

### 门禁为什么要「三态」
`pro` 专属（2026-11-13 用户定）。但客户可能在没网的地方打开软件：
- 明确知道是 free → 提示「需要专业版」
- 网络断了判不出来 → 提示「网络异常」，**绝不能提示升级**

所以 `_level` 返回空串表示「判不出来」，`_require` 按两种错误分开抛
（403 需要升级 / 503 网络异常）。前端据此显示不同文案。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import (audit, auth, core, nativedlg, pickmem, publisher, remote, wxbot,
               wxdel, wxmon, wxopen, wxpub, wxstat, wxworker)

router = APIRouter()

# 等级缓存：命中就不打网络。断网时 24 小时内的旧值还能用（客户可能只是临时断网）。
_LEVEL_TTL = 30 * 60
_LEVEL_KEEP = 24 * 3600
_LEVELS: dict[int, tuple[str, float]] = {}

# ── 发布页候选缓存（只给界面显示，绝不进发表链路）────────────────────────
# 拉一次候选要开无头 Chrome 把发表页真点一遍（约 18 秒）。剧集和标注是号里
# 相对静态的数据，按号落盘缓存后开页秒出。失效时机：TTL 到期（前端静默更新）、
# 用户点「重新读一次」、登录态重新坐实（可能换了微信）。
# **发表时剧集 id 仍走接口认领**（wxpost.py），这里的数据只用来画下拉，
# 不参与选值校验 —— 否则选到已下架的剧集也照发不误。
_OPTS_TTL = 6 * 3600
_OPTS_DIR = os.path.join(core.DATA_DIR, "options_cache")


def _opts_path(aid: str) -> str:
    # aid 从 URL 来，文件名只留安全字符 + 短 hash，别让它拼出目录
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", aid)[:48] or "acc"
    h = hashlib.sha256(aid.encode("utf-8")).hexdigest()[:10]
    return os.path.join(_OPTS_DIR, f"{safe}_{h}.json")


def _opts_read(aid: str) -> tuple[float, dict | None]:
    try:
        with open(_opts_path(aid), encoding="utf-8") as f:
            rec = json.load(f)
        data = rec.get("data")
        if isinstance(data, dict) and "declare" in data and "link" in data:
            return float(rec.get("ts") or 0), data
    except (OSError, ValueError, TypeError):
        pass
    return 0.0, None


def _opts_write(aid: str, data: dict) -> None:
    # 先写临时文件再 replace：多开页面同时读也不会读到半截 JSON
    try:
        os.makedirs(_OPTS_DIR, exist_ok=True)
        p = _opts_path(aid)
        tmp = f"{p}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "aid": aid, "data": data},
                      f, ensure_ascii=False)
        os.replace(tmp, p)
    except OSError:
        pass


def invalidate_opts_cache(aid: str) -> None:
    """登录态变了之后调用：旧候选可能属于上一个微信。"""
    try:
        os.remove(_opts_path(aid))
    except OSError:
        pass


# 「加载中 / 暂无 / 创建合集」这些是页面的占位文案，不是号里真有的条目。
# 拦 CGI 时偶尔混进响应，落进缓存就会在下拉里躺 6 小时（2026-11-15）。
_PLACEHOLDER_RE = re.compile(
    r"^(暂无|暂无数据|暂无记录|暂无合集|暂无短剧|暂无剧集|无数据|"
    r"加载中|创建合集|添加合集|选择合集|请选择|搜索内容|合集名称|短剧名称|名称|标题)$")


def _item_key(it) -> str:
    """候选的稳定 key：有平台 id 用 id，没有就用归一化后的名字。

    同一条剧集/标注常常在 DOM 和 CGI 响应里各出现一次，落进缓存就是下拉里
    两个一模一样的选项，用户不知道选哪个（2026-11-15 A 档）。空 key 不去重，
    宁可留重复也别把看不懂结构的条目丢掉。
    """
    if isinstance(it, dict):
        ident = str(it.get("v") or it.get("id") or "").strip()
        name = str(it.get("name") or it.get("label")
                   or it.get("title") or it.get("cn") or "").strip()
    elif isinstance(it, str):
        ident, name = "", it.strip()
    else:
        return ""
    return re.sub(r"\s+", "", (ident or name)).lower()


def _clean_opts(data: dict) -> dict:
    """过滤占位文案 + 按稳定 key 去重，别动 via / note 这些状态字段。"""
    for kind in ("declare", "link", "location"):
        part = data.get(kind)
        if not isinstance(part, dict):
            continue
        items = part.get("items")
        if not isinstance(items, list):
            continue
        kept, seen = [], set()
        for it in items:
            if isinstance(it, dict):
                name = str(it.get("name") or it.get("label")
                           or it.get("title") or "").strip()
            elif isinstance(it, str):
                name = it.strip()
            else:
                name = ""
            if name and _PLACEHOLDER_RE.match(name):
                continue
            key = _item_key(it)
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            kept.append(it)
        part["items"] = kept
    return data


def _opts_valid(data: dict) -> bool:
    """只有两支接口都真拿到模板才落缓存。

    `via: none` = 没拦到 CGI 模板（弹层没点开 / 网络抖动），和「这个号真的
    没有剧集」长得一模一样。把这种结果缓存 6 小时，用户会一直以为号里没
    剧集 —— 所以宁可不缓存，下次照常重读。
    """
    return bool((data.get("declare") or {}).get("via") == "api"
                and (data.get("link") or {}).get("via") == "api")


def _dev_mode() -> bool:
    """开发机 / 自用部署不做门禁。

    `dist_mode=True` 只出现在打包发给客户的版本里（build.bat 写死），
    开发机上根本没有「pro 账号」，卡门禁等于自己没法测。
    分发版仍然严格判，绕不过去。
    """
    return not bool(core.load_config().get("dist_mode"))


def _level(request: Request, me: dict) -> dict:
    """取当前账号的等级，返回 {"level": str} 或 {"level": None, "why": "..."}。

    why 分三种，前端要给三种不同的话：
      · `offline` 连不上服务器 —— 「网络异常」
      · `stale`   服务器说令牌失效 —— 「请重新登录一次」
      · `nobind`  本机从没在服务器登录过 —— 按 free 算

    把 `stale` 说成「网络异常」是最容易挨骂的错：用户会去查网络、重启电脑，
    实际只要重新登录一下。
    """
    uid = int(me.get("id") or 0)
    hit = _LEVELS.get(uid)
    if hit and time.time() - hit[1] < _LEVEL_TTL:
        return {"level": hit[0]}

    # **必须用服务器发的那张 token**（core.account_token()），不能用请求头里的
    # 本地 JWT —— 本地 JWT 是本机自己签的，服务器不认，拿它调 /api/auth/me 一律
    # 401，会被误判成「断网」或「没开通」。
    token = core.account_token()
    if not token:
        return {"level": "", "why": "nobind"}
    try:
        info = remote.me(token)
    except remote.RemoteError as exc:
        if hit and time.time() - hit[1] < _LEVEL_KEEP:
            return {"level": hit[0]}
        # 401/403 = 令牌被服务端作废（换机器登录、改过密码、会话过期）
        if getattr(exc, "status", 0) in (401, 403):
            return {"level": None, "why": "stale"}
        return {"level": None, "why": "offline"}
    except Exception:  # noqa: BLE001  纯网络问题
        if hit and time.time() - hit[1] < _LEVEL_KEEP:
            return {"level": hit[0]}
        return {"level": None, "why": "offline"}

    lv = ((info.get("account") or {}).get("level") or "").strip().lower()
    _LEVELS[uid] = (lv, time.time())
    return {"level": lv}


def gate(request: Request, me: dict) -> dict:
    """功能门禁判定。"""
    # 本机管理员（自己）和开发机直接放行
    if (me.get("role") or "") == "admin":
        return {"ok": True, "level": "admin"}
    if _dev_mode():
        return {"ok": True, "level": "dev"}
    r = _level(request, me)
    lv = r.get("level")
    if lv is None:
        return {"ok": False, "reason": r.get("why") or "offline"}
    if core.feature_allowed(lv):
        return {"ok": True, "level": lv}
    return {"ok": False, "reason": "need_pro", "level": lv}


def _require(request: Request) -> dict:
    me = auth.current_user(request)
    g = gate(request, me)
    if not g["ok"]:
        if g["reason"] == "offline":
            raise HTTPException(503, "网络异常，暂时无法确认账号权限，请稍后重试")
        if g["reason"] == "stale":
            raise HTTPException(403, "登录状态已失效，请退出后重新登录一次，再打开发布计划")
        raise HTTPException(403, "此功能需要专业版")
    return me


def _proj(pid: str) -> dict:
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    return p


def _run(fn, *a, **kw):
    """业务校验错误统一转 400（PublishError 的文案本来就是给人看的）。"""
    try:
        return fn(*a, **kw)
    except publisher.PublishError as exc:
        raise HTTPException(400, str(exc)) from exc


class PlanBody(BaseModel):
    first_at: str = ""
    interval_min: int = publisher.INTERVAL_DEFAULT


class StartBody(PlanBody):
    account_id: str = ""
    # 预览时算出的「这批的指纹」。带了就核一遍：确认框看过之后视频或时间
    # 又被改过的话，不能还按旧确认提交。留空兼容老前端/脚本。
    intent: str = ""


class JobPatch(BaseModel):
    """单条视频的可改项。

    名字和 id 成对出现：界面上是从候选里「选」而不是「填」，存名字用于回读核对、
    存 id 用于上传时点击（视频号的位置/合集/剧集都是 id 驱动的异步组件）。
    """
    title: str | None = None
    short_title: str | None = None
    topics: list[str] | None = None
    cover_ts: float | None = None
    cover_file: str | None = None
    collection: str | None = None
    collection_id: str | None = None
    location: str | None = None
    location_poi: str | None = None
    location_city: str | None = None
    activity: str | None = None
    activity_id: str | None = None
    link: str | None = None
    link_kind: str | None = None
    link_id: str | None = None
    link_title: str | None = None
    original: bool | None = None
    declare: str | None = None
    account_id: str | None = None


class ApplyBody(BaseModel):
    fields: dict = {}


class MarkBody(BaseModel):
    action: str
    note: str = ""


class AccountBody(BaseModel):
    name: str = ""


class AccountPatch(BaseModel):
    name: str | None = None


def _wx_msg(exc: Exception) -> str:
    """浏览器层的异常一律翻成人话。Playwright 的英文栈直接给客户看等于骂人。"""
    s = str(exc or "").strip()
    if isinstance(exc, (publisher.PublishError, wxpub.WxPubError)):
        return s
    low = s.lower()
    if "executable doesn't exist" in low or "no such file" in low:
        return "启动浏览器失败：找不到浏览器程序。装一个 Chrome 再试。"
    if "target page, context or browser has been closed" in low:
        return "浏览器被关掉了。重新点一次登录，别关那个窗口。"
    if "timeout" in low:
        return "等页面等太久了（网速慢或者视频号助手打不开）。重试一次。"
    if not s:
        return "浏览器出了点问题，重试一次。"
    return "浏览器出错：" + s[:160]


class SelectBody(BaseModel):
    account_id: str = ""


class OpenPlatformBody(BaseModel):
    aid: str = ""


class AbnDeleteBody(BaseModel):
    aid: str = ""
    post_id: str = ""
    title: str = ""
    dry_run: bool = True          # 默认只定位，不点删除
    confirm: bool = False         # 真删必须界面二次确认过


@router.post("/api/publish/job/{jid}/publish")
def publish_now(jid: str, request: Request, dry_run: bool = False):
    """手动立刻发这一条。

    `dry_run=1`：填完所有字段就停手，**不点发表**（开发/核对用）。
    """
    me = _require(request)
    job = next((j for j in publisher.queue().get("jobs", []) if j.get("id") == jid), None)
    if not job:
        raise HTTPException(404, "这条已经不在队列里了")
    try:
        r = wxworker.run_job(job, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, _wx_msg(exc)) from exc
    audit.log_event(me, "hookcut.publish", target=f"job:{jid}",
                    payload={"op": "publish_now", "dry_run": bool(dry_run),
                             "ok": bool(r.get("ok"))},
                    request=request, route="/api/publish/job/publish", status_code=200)
    return r


# ── 把本地文件喂给浏览器页面（发表时用）────────────────────────────────
def _media_sig(job_id: str, kind: str) -> str:
    """媒体地址的签名。

    为什么要有签名：发表时页面（channels.weixin.qq.com）要从本机取视频和封面
    图。这个地址一旦被猜出来，任何网页都能让用户电脑把本地文件读出去。
    签名是「job_id + kind + 本机密钥」的 HMAC，猜不出来。

    注意 digestmod 要写 'sha256'（或 hashlib.sha256 这个可调用对象），
    写成 'hashlib.sha256' 这个字符串会直接抛 ValueError（2026-11-13 踩过）。
    """
    import hmac
    secret = auth.jwt_secret().encode("utf-8")
    raw = f"{job_id}|{kind}".encode("utf-8")
    return hmac.new(secret, raw, hashlib.sha256).hexdigest()[:32]


def media_url(job_id: str, kind: str = "video") -> str:
    """给页面用的本机文件地址（带签名）。"""
    sig = _media_sig(job_id, kind)
    port = int(core.load_config().get("port") or 8100)
    return (f"http://127.0.0.1:{port}/api/publish/media"
            f"?job={job_id}&kind={kind}&t={sig}")


@router.options("/api/publish/media")
def publish_media_preflight(request: Request):
    """浏览器的 CORS 预检。

    为什么必须有这个：页面在 **https**://channels.weixin.qq.com，要来本机
    **http**://127.0.0.1 取文件。浏览器对这种「公网页面 → 本机地址」会走
    Private Network Access：先发一个 OPTIONS 预检，要求响应里带
    `Access-Control-Allow-Private-Network: true`，不给就在正式请求前就
    直接失败——页面里表现为 `Failed to fetch`（2026-11-13 实测踩到）。
    """
    from fastapi.responses import Response
    resp = Response(status_code=204)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    # 私有网络许可：不给这行，浏览器在正式请求前就掐掉。
    resp.headers["Access-Control-Allow-Private-Network"] = "true"
    resp.headers["Access-Control-Max-Age"] = "600"
    return resp


@router.get("/api/publish/media")
def publish_media(request: Request, job: str = "", kind: str = "video", t: str = ""):
    """把队列里那条视频（或它的封面图）喂给浏览器页面。

    **这个地址在白名单里**（页面里的 fetch 带不了 Authorization），所以安全性
    全靠上面那个签名：拿不到签名就 403。路径固定、参数带签名，就是为了能进
    白名单（白名单是精确匹配路径的）。

    为什么要它：页面要拿到**整个视频文件**才能上传。base64 塞不进 evaluate
    （8MB 上限，真实成片几十上百兆），只能让页面自己去本机取。
    """
    from fastapi.responses import FileResponse
    if not job or t != _media_sig(job, kind):
        raise HTTPException(403, "这个地址无效")
    j = next((x for x in publisher.queue().get("jobs", []) if x.get("id") == job), None)
    if not j:
        raise HTTPException(404, "这条已经不在队列里了")
    if kind == "cover":
        rel = j.get("cover_file") or ""
        if not rel:
            tpl = publisher.queue().get("template") or {}
            rel = tpl.get("cover_file") or ""
        path = publisher.cover_abs(rel) if rel else ""
        if not path or not os.path.isfile(path):
            raise HTTPException(404, "这条没有封面图")
        name = os.path.basename(path)
    else:
        path = j.get("file") or ""
        if not path or not os.path.isfile(path):
            raise HTTPException(404, "视频文件不在了")
        name = os.path.basename(path)
    resp = FileResponse(path, filename=name, media_type=None)
    # 页面在 https 里，要读本机 http 的内容必须给 CORS 头；
    # 127.0.0.1 属于「可信来源」，浏览器不会按混合内容拦它。
    resp.headers["Access-Control-Allow-Origin"] = "*"
    # 正式请求也要带私有网络许可（预检过了不代表正式请求免检）。
    resp.headers["Access-Control-Allow-Private-Network"] = "true"
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── 账号（真账号：扫码登录，不再只是存个名字）────────────────────────────
@router.get("/api/publish/accounts")
def accounts(request: Request):
    me = auth.current_user(request)
    g = gate(request, me)
    return {"accounts": publisher.public_accounts(), "gate": g,
            "max": publisher.MAX_ACCOUNTS}


@router.post("/api/publish/accounts")
def add_account(body: AccountBody, request: Request):
    """建账号并直接把登录窗口弹出来。

    **为什么不先要名字**：点加号就弹视频号助手的登录页，登完用微信昵称回填。
    先逼用户起名再登录，登完发现名字和号对不上还得改一次。
    窗口打不开（没装 Chrome 之类）就把这条空壳账号删掉，别在列表里留垃圾。
    """
    me = _require(request)
    acc = _run(publisher.add_account, body.name, pending=not (body.name or "").strip())
    try:
        wxbot.session(acc["id"]).open_login()
    except Exception as exc:  # noqa: BLE001
        wxbot.drop(acc["id"], remove_profile=True)
        publisher.remove_account(acc["id"])
        raise HTTPException(400, _wx_msg(exc)) from exc
    audit.log_event(me, "hookcut.publish", target=f"account:{acc['id']}",
                    payload={"op": "add_account", "login": True}, request=request,
                    route="/api/publish/accounts", status_code=200)
    return {"account": acc, "accounts": publisher.public_accounts()}


@router.post("/api/publish/accounts/{aid}/login")
def account_login(aid: str, request: Request, force: bool = False):
    """把这个号的登录页在**可见浏览器窗口**里弹出来（2026-11-14 用户定：
    登录一律弹窗口手动做，界面里不再显示二维码——平台登录页自带快捷登录卡、
    换码、过期刷新，用户对着真窗口比自己截一张码清楚得多）。
    登没登上由前端轮询 `login-state` 判断，这个接口只负责开窗口。
    `force=1`：「重登」= 换一个微信登录。先把浏览器里上一次的登录态清掉再开
    登录页；不清的话平台快捷登录卡一键就把旧微信又登回来了，界面看上去就是
    「点了重登没反应」（2026-11-13 用户反馈）。
    """
    _require(request)
    if not publisher.get_account(aid):
        raise HTTPException(404, "这个账号已经不在了")
    try:
        r = wxbot.session(aid).open_login(force=force)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, _wx_msg(exc)) from exc
    if force:
        # 强制重登＝这个号的登录态已经被清掉了，列表里就该显示「待登录」，
        # 别还挂着「已登录」骗用户。
        publisher.update_account(aid, state="pending",
                                 last_check=publisher.fmt(publisher.now()))
    return {"logged_in": bool(r.get("logged_in"))}


@router.get("/api/publish/accounts/state")
def accounts_state(request: Request):
    """**对一遍账**：把每个号的真实登录态读出来，更新列表后返回。

    为什么要有这个：账号状态是存在本地的，登录那一刻如果前端没赶上
    （页面刷新、轮询被打断、浏览器崩了），库里就永远停在「待扫码」，
    而实际早就登录好了——用户看到的就是「明明登录了还说要扫码」。
    打开页面时对一次账，这条路就通了。
    """
    me = _require(request)
    fixed = []
    for acc in publisher.load_accounts():
        aid = acc.get("id")
        if not aid:
            continue
        st = (acc.get("state") or "")
        # 只查「还没坐实」的那几类：pending / unknown / expired。
        # 已经是 online 的不去碰 —— 每开一次页面都要起一个浏览器，
        # 对着好账号反复查纯属浪费，还可能把正在扫码的窗口顶掉。
        if st not in ("pending", "unknown", "expired"):
            continue
        try:
            s = wxbot.session(aid).state()
        except Exception:  # noqa: BLE001  对账失败不是致命错，跳过这个号
            continue
        if s.get("logged_in"):
            now = publisher.fmt(publisher.now())
            patch = {"state": "online", "last_check": now, "last_ok": now}
            if s.get("wx_name"):
                patch["wx_name"] = s["wx_name"][:30]
            if s.get("avatar"):
                patch["avatar"] = s["avatar"]
            publisher.update_account(aid, **patch)
            fixed.append({"id": aid, "from": st,
                          "wx_name": patch.get("wx_name") or acc.get("wx_name") or ""})
            # 对账把号坐实成 online = 登录这回事刚被证实，号资料也重新拉一遍
            # （2026-11-14 用户定：每次登录之后都重取，别拿旧昵称/旧 ID）。
            _fill_profile_async(aid, force=True)
        elif st == "expired":
            publisher.update_account(aid, state="expired",
                                     last_check=publisher.fmt(publisher.now()))
    # 只有「真的把状态改过来了」才留一条日志。
    # 每次开页都调一次这个接口，如果每次都记，后台日志全是「查了一下没变化」，
    # 真正有意义的那条（扫完码但前端没赶上，程序自己纠正的）反而被淹掉。
    if fixed:
        audit.log_event(me, "hookcut.publish", target="accounts:reconcile",
                        payload={"op": "reconcile", "fixed": fixed},
                        request=request, route="/api/publish/accounts/state",
                        status_code=200)
    return {"accounts": [{**a, "label": _acc_label(a)}
                         for a in publisher.public_accounts()]}


@router.get("/api/publish/accounts/{aid}/login-state")
def account_login_state(aid: str, request: Request, diag: int = 0, hard: int = 0):
    """前端每 2 秒问一次。登录成功后把微信昵称/头像写回账号并置 online。

    `?hard=1`：对「看着像登录」的旧标签页做一次真导航验证（2026-11-14 加）。
    读取候选失败后前端用它对账——旧标签页会把死会话误报成已登录，不真导航
    一次就永远查不出「账号卡亮着已登录、实际每个读取都撞登录墙」这种局。
    `?diag=1` 时额外返回浏览器内部状态（有哪些标签、停在哪个网址、帧列表）。
    登录失败时外面只能看到 `logged_in: false`，分不清「窗口被关了」「停在别的
    页上」「微信拦了自动化」，没有这个就只能猜。
    """
    _require(request)
    acc = publisher.get_account(aid)
    if not acc:
        raise HTTPException(404, "这个账号已经不在了")
    try:
        st = wxbot.session(aid).state(hard=bool(hard))
    except Exception as exc:  # noqa: BLE001
        return {"logged_in": False, "error": _wx_msg(exc),
                "account": publisher.public_accounts()}
    if st.get("logged_in"):
        now = publisher.fmt(publisher.now())
        was_online = (acc.get("state") or "") == "online"
        patch = {"state": "online", "last_check": now, "last_ok": now}
        if st.get("wx_name"):
            patch["wx_name"] = st["wx_name"][:30]
        if st.get("avatar"):
            patch["avatar"] = st["avatar"]
        acc = publisher.update_account(aid, **patch)
        # 昵称/头像上面已按这次读到的覆盖；视频号 ID/粉丝数要从 auth_data 拉 →
        # 后台补一次（不堵这个 2 秒轮询）。2026-11-14 用户定：**每次登录之后都
        # 重新拉一遍号资料**——以前只在 ID 缺失时补一次，换了微信或改了号名
        # 界面就一直显示旧值。刚从非 online 翻过来 = 这次轮询亲眼看到的登录
        # 成功，绕过 10 分钟节流；切号验号这类平时探测仍按节流来，别为资料
        # 反复开浏览器。
        if not was_online:
            # 刚坐实成 online（多半是重新登录，甚至换了微信）：这个号之前
            # 缓存的剧集/标注可能属于上一个登录态，删掉让它重读一次。
            invalidate_opts_cache(aid)
        _fill_profile_async(aid, force=not was_online)
    elif (acc.get("state") or "") == "online":
        # 之前是好的，现在掉到登录页了 = 登录态过期（微信那边会踢）。标出来让人重扫，
        # 别等发布失败了才说「未登录」。
        acc = publisher.update_account(aid, state="expired",
                                       last_check=publisher.fmt(publisher.now()))
    out = {"logged_in": bool(st.get("logged_in")), "account": acc,
           "accounts": publisher.public_accounts()}
    if diag and not st.get("logged_in"):
        try:
            out["_diag"] = wxbot.session(aid).diag()
        except Exception as exc:  # noqa: BLE001
            out["_diag"] = {"err": _wx_msg(exc)}
    return out


@router.put("/api/publish/accounts/{aid}")
def rename_account(aid: str, body: AccountPatch, request: Request):
    _require(request)
    patch = {}
    if body.name is not None:
        nm = body.name.strip()
        if not nm:
            raise HTTPException(400, "名字不能是空的")
        if len(nm) > 30:
            raise HTTPException(400, "账号名太长了，30 个字以内")
        patch["name"] = nm
    _run(publisher.update_account, aid, **patch)
    return {"accounts": [{**a, "label": _acc_label(a)}
                         for a in publisher.public_accounts()]}


@router.delete("/api/publish/accounts/{aid}")
def del_account(aid: str, request: Request):
    me = _require(request)
    # 删号前只拦一件事：这个号还有没传完的任务在排队（2026-11-14 起不再锁号，
    # 但删号会让排队任务失去目标，还是得先取消）。
    inflight = [j for j in publisher.queue().get("jobs", [])
                if j.get("account_id") == aid
                and j.get("status") in ("queued", "publishing")]
    if inflight:
        raise HTTPException(409, f"这个号还有 {len(inflight)} 条在排队上传；"
                            "先到「已提交」里取消（或等传完），再删号")
    # 连登录态一起删：留着 profile 等于把别人的微信登录凭证留在硬盘上
    wxbot.drop(aid, remove_profile=True)
    publisher.remove_account(aid)
    audit.log_event(me, "hookcut.publish", target=f"account:{aid}",
                    payload={"op": "del_account"}, request=request,
                    route="/api/publish/accounts", status_code=200)
    return {"ok": True, "accounts": publisher.public_accounts()}


# ── 本机选文件（浏览器拿不到绝对路径，只能让后端弹系统框）─────────────────
# 起始位置走 pickmem：上次在这个用途下确认过哪儿就开在哪儿（见 app/pickmem.py）
@router.post("/api/publish/pick-files")
def pick_files(request: Request):
    """一次多选本地视频。浏览器拖拽拿不到绝对路径，所以走原生对话框。"""
    _require(request)
    result: dict = {"paths": []}
    err: dict = {}

    def _pick():
        try:
            result["paths"] = nativedlg.pick_files(
                "选择要发布的视频", pickmem.recall_dir("publish_files"),
                filetypes=[("视频文件", "*.mp4 *.mov *.m4v *.flv *.mkv *.avi *.wmv"),
                           ("所有文件", "*.*")])
        except Exception as exc:  # noqa: BLE001
            err["msg"] = str(exc)

    t = threading.Thread(target=_pick, daemon=True)
    t.start()
    t.join(timeout=300)
    if err:
        raise HTTPException(500, f"弹不出选择框（{err['msg']}）")
    for p in result["paths"][:1]:          # 只记一个目录，多选的文件同属一个文件夹
        pickmem.remember("publish_files", p, False)
    return {"paths": result["paths"]}


# ── 封面图片：选一张整批共用，图片复制进应用数据目录 ───────────────────────
@router.post("/api/publish/pick-cover")
def pick_cover(request: Request):
    """弹原生对话框选封面图；返回的是相对名，不把用户原始路径存进队列。"""
    _require(request)
    result = {"path": ""}
    err: dict = {}

    def _pick():
        try:
            result["path"] = nativedlg.pick_file(
                "选择封面图片", pickmem.recall_dir("cover"),
                filetypes=[("图片文件", "*.png *.jpg *.jpeg *.webp *.bmp"),
                           ("所有文件", "*.*")])
        except Exception as exc:  # noqa: BLE001
            err["msg"] = str(exc)

    t = threading.Thread(target=_pick, daemon=True)
    t.start()
    t.join(timeout=300)
    if err:
        raise HTTPException(500, f"弹不出选择框（{err['msg']}）")
    if not result["path"]:
        return {"ok": False}
    pickmem.remember("cover", result["path"], False)
    try:
        out = publisher.save_cover(result["path"])
    except publisher.PublishError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, **out}


@router.get("/api/publish/cover/preview")
def cover_preview(name: str = ""):
    """给 <img> 看的封面缩略图。<img> 带不了 Authorization，所以这里不鉴权：
    只认固定目录里的白名单文件名，不接路径、不接 ../，读不到就 404。"""
    if os.path.basename(name) != name or \
            os.path.splitext(name)[1].lower() not in publisher.COVER_EXTS:
        raise HTTPException(404, "图片不在了")
    p = publisher.cover_abs(f"{publisher.COVER_DIR}/{name}")
    if not p or not os.path.isfile(p):
        raise HTTPException(404, "图片不在了")
    return FileResponse(p)


# ── 发布队列（全局，不属于任何项目）──────────────────────────────────────
def _view() -> dict:
    q = publisher.queue()
    tpl = q["template"]
    tpl_out = {**tpl, "cover_url": publisher.cover_url(tpl.get("cover_file"))}
    accounts = publisher.public_accounts()
    names = {a["id"]: a["name"] for a in accounts}
    # 「当前发到哪个号」只由后端说了算，前端不许自己从 batch 或 DOM 里推。
    # 2026-11-14 起不再锁号：提交过的任务跟着自己的账号走，切号随时自由。
    cur = q.get("current_account_id") or ""
    cur_acc = next((a for a in accounts if a.get("id") == cur), None)
    jobs = []
    for j in q["jobs"]:
        jj = publisher.job_full(j)
        eff = publisher.cover_effective(jj, tpl)
        jobs.append({**jj,
                     # 「这条最终用哪张」由后端算一份，前端别自己拼优先级
                     "cover_file_eff": eff,
                     "cover_url": publisher.cover_url(eff),
                     "cover_override": bool(jj.get("cover_file")),
                     "account_name": names.get(j.get("account_id") or "", ""),
                     "file_name": os.path.basename(j.get("file") or ""),
                     "file_exists": os.path.isfile(j.get("file") or "")})
    jobs.sort(key=lambda j: (j.get("plan_at") or "9", j.get("created_at") or ""))
    return {"jobs": jobs,
            "template": tpl_out, "batch": q["batch"],
            "max_accounts": publisher.MAX_ACCOUNTS,
            "summary": publisher.summarize(q),
            # 每个号都带一份「备注名 · 视频号名 · ID」的完整标签，界面只管用，
            # 别再自己拼一遍（两处拼法早晚会分叉）。
            "accounts": [{**a, "label": _acc_label(a)} for a in accounts],
            "current_account_id": cur,
            "current_account": ({**cur_acc, "label": _acc_label(cur_acc)}
                                if cur_acc else None),
            # 字段说明由后端下发：长度上限只在 publisher.FIELD_SPEC 维护一份，
            # 前端别自己写死数字（改一处漏一处，客户看到的限制和实际不符）
            "fields": {
                "spec": {k: {"cn": v[0], "max": v[1], "hint": v[2]}
                         for k, v in publisher.FIELD_SPEC.items()},
                "declares": [{"v": v, "cn": cn} for v, cn in publisher.DECLARES],
                "link_kinds": [{"v": v, "cn": cn} for v, cn in publisher.LINK_KINDS],
                "batch": list(publisher.BATCH_KEYS),
            },
            "limits": {"lead_min": publisher.LEAD_MIN,
                       "lead_max_days": publisher.LEAD_MAX_DAYS,
                       "interval_min": publisher.INTERVAL_MIN,
                       "interval_default": publisher.INTERVAL_DEFAULT,
                       "batch_max": publisher.BATCH_MAX,
                       "max_mb": publisher.MAX_MB}}


def _acc_label(a: dict | None) -> str:
    """账号卡/确认框里那一行：备注名 · 视频号名 · ID sph…"""
    if not a:
        return "已删除的号"
    bits = [a.get("name") or ""]
    wx = a.get("wx_name") or ""
    if wx and wx != a.get("name"):
        bits.append(wx)
    if a.get("finder_id"):
        bits.append("ID " + a["finder_id"])
    return " · ".join(x for x in bits if x) or "未命名账号"


@router.post("/api/publish/account/select")
def select_account(body: SelectBody, request: Request):
    """界面选号：服务端记下「当前发到」，并清掉上一个号专属的字段值。

    2026-11-14 起不再锁号（提交过的任务跟自己的账号走，切号随时自由）。
    """
    me = _require(request)
    res = _run(publisher.select_account, body.account_id)
    if res.get("cleared"):
        audit.log_event(me, "hookcut.publish.set", target="queue",
                        payload={"op": "select_account", "account": body.account_id},
                        request=request, route="/api/publish/account/select",
                        status_code=200)
    return {**res, "queue": _view()}


def _check_option_aid(aid: str) -> None:
    """候选预取按号隔离的检查点（2026-11-14 起不再锁号）：切号时前端会清空
    旧候选并按新号重读，这里不再拦跨号读取，只保留占位以免调用点散改。"""
    return None


@router.get("/api/publish/queue")
def get_queue(request: Request):
    me = auth.current_user(request)
    g = gate(request, me)
    return {**_view(), "gate": g}


class AddBody(BaseModel):
    sets: list[int] = []
    files: list[str] = []
    dir_path: str = ""
    pid: str = ""


@router.post("/api/publish/add")
def add(body: AddBody, request: Request):
    """加视频进队列。三条入口共用：选文件 / 选文件夹 / 从某个项目挑成片。"""
    _require(request)
    if body.pid:
        _proj(body.pid)
    res = _run(publisher.add_jobs, body.files, body.sets, body.pid, body.dir_path)
    return {**res, "queue": _view()}


@router.put("/api/publish/job/{jid}")
def patch_job(jid: str, body: JobPatch, request: Request):
    _require(request)
    _run(publisher.update_job, jid, body.model_dump(exclude_none=True))
    return _view()


@router.delete("/api/publish/job/{jid}")
def drop_job(jid: str, request: Request, platform: int = 0):
    """platform=1 = 用户已确认「只从队列删掉，平台上那条定时还在」。
    只有「已交平台·自动发」的行才会带这个参数（前端确认框里写明了后果）。"""
    _require(request)
    _run(publisher.del_job, jid, bool(platform))
    return _view()


@router.post("/api/publish/apply")
def apply_batch(body: ApplyBody, request: Request):
    """整批通用设置：合集 / 位置 / 活动 / 扩展链接 / 原创 / 标注 / 话题。"""
    me = _require(request)
    res = _run(publisher.apply_fields, body.fields)
    audit.log_event(me, "hookcut.publish.set", target="queue",
                    payload={"op": "apply_fields", "n": res.get("n"),
                             "keys": sorted((body.fields or {}).keys())},
                    request=request, route="/api/publish/apply", status_code=200)
    return {**res, "queue": _view()}


@router.post("/api/publish/clear-draft")
def clear_draft(request: Request):
    _require(request)
    res = publisher.clear_draft()
    return {**res, "queue": _view()}


@router.post("/api/publish/plan")
def plan(body: PlanBody, request: Request):
    """只算不存：前端调时间/间隔时实时预览。"""
    _require(request)
    return _run(publisher.preview, body.first_at, body.interval_min)


@router.post("/api/publish/start")
def start(body: StartBody, request: Request):
    me = _require(request)
    res = _run(publisher.start, body.first_at, body.interval_min,
               body.account_id, body.intent)
    # 排到即上传（2026-11-13 用户定稿）：排完立刻开传，不等到点——
    # 全部传完后电脑关了平台照发。worker 自己会把 queued 一条条处理掉。
    wxworker.start()
    # 涨幅靠「自动记录」采样（至少两次才有差）。排了期就自动开，别让客户忘了
    # 开导致视频发出去了却没有涨幅可看（2026-11-14 批2 A2）。用户手动停过的
    # 尊重选择，本进程内不再自动开；关机重启后标记归零，下次排期照常兜底。
    mon_started = False
    try:
        if not wxmon.status().get("on") and not wxmon.user_stopped():
            wxmon.set_on(True, 3, aid=body.account_id)
            mon_started = True
    except Exception:
        mon_started = False
    audit.log_event(me, "hookcut.publish.plan", target="queue",
                    payload={"op": "start", "count": res.get("started", 0),
                             "first": res.get("first", ""), "last": res.get("last", "")},
                    request=request, route="/api/publish/start", status_code=200)
    return {**res, "monitor_started": mon_started, "queue": _view()}


class StopBody(BaseModel):
    account_id: str = ""


@router.post("/api/publish/stop")
def stop(request: Request, body: StopBody | None = None):
    """批量取消。body 里带 account_id 时只撤这个号的（界面「批量取消」
    只显示当前号的任务，操作范围跟显示范围一致，不跨号误伤）。"""
    _require(request)
    res = publisher.stop((body or StopBody()).account_id)
    return {**res, "queue": _view()}


@router.post("/api/publish/job/{jid}/retry")
def retry(jid: str, request: Request):
    _require(request)
    res = _run(publisher.retry, jid)
    # 重试把条目放回 queued，得把上传线程喊起来，不然没人接手
    wxworker.start()
    out = _view()
    # 原计划时间已经过了 → 这次顺延了，界面上要说出来（不能默默改时间）
    out["moved_to"] = (res or {}).get("moved_to") or ""
    return out


@router.post("/api/publish/job/{jid}/mark")
def mark(jid: str, body: MarkBody, request: Request):
    """人工确认：已发 / 跳过。P1 的全部推进都走这里。"""
    me = _require(request)
    _run(publisher.mark, jid, body.action, body.note)
    audit.log_event(me, "hookcut.publish.retry", target=jid,
                    payload={"op": body.action, "job": jid},
                    request=request, route="/api/publish/job/{id}/mark",
                    status_code=200)
    return _view()


# ── 从项目挑片（便利入口：队列是全局的，项目只是来源之一）─────────────────
@router.get("/api/publish/projects")
def projects_with_sets(request: Request):
    """哪些项目有能直接发的成片。发布队列不依赖项目，这只是「顺手挑」的列表。"""
    _require(request)
    out = []
    try:
        names = [d for d in os.listdir(core.PROJECTS)
                 if os.path.isdir(os.path.join(core.PROJECTS, d))]
    except OSError:
        names = []
    for pid in names:
        p = core.load(pid)
        if not p:
            continue
        sets = publisher.pickable_sets(p)
        if sets:
            out.append({"id": pid, "name": p.get("name") or pid, "n": len(sets)})
    out.sort(key=lambda x: x["name"])
    return {"projects": out}


@router.get("/api/projects/{pid}/publish/sets")
def project_sets(pid: str, request: Request):
    _require(request)
    p = _proj(pid)
    return {"sets": publisher.pickable_sets(p), "pid": pid,
            "name": p.get("name") or pid}


# ── 发布页候选（合集 / 位置 / 活动 / 剧集）：必须从号里真实拉 ──────────────
@router.get("/api/publish/options")
def publish_options(request: Request, aid: str = "", kind: str = "",
                    q: str = "", link_kind: str = "series"):
    """从一个已登录的号里拉候选列表。

    为什么不让用户手打：视频号的位置是「搜索候选」、合集/剧集是号里自己的 id。
    手打的文字既填不进页面（那里根本没有可输入的框），也没法回读核对。
    """
    _require(request)
    if not aid or not publisher.get_account(aid):
        raise HTTPException(404, "这个账号已经不在了")
    _check_option_aid(aid)
    me = auth.current_user(request)
    try:
        data = wxpub.options(wxbot.session(aid), kind, q=q, link_kind=link_kind)
    except wxpub.LoginLostError as exc:
        # 登录墙单独标出来：前端看到 login=True 会直接探登录态、弹登录窗口，
        # 不让用户对着「重试」按钮无限点下去（2026-11-14 实测）。
        return {"items": [], "note": str(exc), "login": True}
    except Exception as exc:  # noqa: BLE001
        return {"items": [], "note": _wx_msg(exc)}
    # 拉一次候选就是一次真页面操作，量不大但能看出「客户在配什么」，记一条
    audit.log_event(me, "hookcut.publish", target=f"account:{aid}",
                    payload={"op": "options", "kind": kind, "n": len(data.get("items") or []),
                             "q": (q or "")[:20]},
                    request=request, route="/api/publish/options", status_code=200)
    return data


@router.get("/api/publish/options/all")
def publish_options_all(request: Request, aid: str = "", refresh: int = 0):
    """选好号之后静默预取：命中缓存秒回，过期先给旧数据再由前端静默更新。

    `refresh=1` 强制真开一次页面重读（用户点「重新读一次」、缓存过期后
    前端后台更新时用）。位置（location）不在这里 —— 平台要地名才给候选，
    没有关键词时预取不到东西，只能等用户输词。
    """
    _require(request)
    if not aid or not publisher.get_account(aid):
        raise HTTPException(404, "这个账号已经不在了")
    _check_option_aid(aid)

    ts, cached = _opts_read(aid)
    age = int(time.time() - ts) if ts else 0
    if cached is not None and not refresh:
        # 有缓存就永远秒回。过期只打个标记，真读交给前端的后台静默更新，
        # 绝不让「开个页面」为了刷剧集列表干等 18 秒。
        out = dict(cached)
        out.update({"cached": True, "stale": age >= _OPTS_TTL, "age": age})
        return out

    def _stale(note: str = "") -> dict:
        # 有旧数据就先把旧数据给出去，别让界面因为一次网络抖动整个清空
        if cached is not None:
            out = dict(cached)
            out.update({"cached": True, "stale": True, "age": age})
            if note:
                out["note"] = note
            return out
        return {"error": note or "这次没读到，点一下重试。", "stale": True}

    try:
        data = wxpub.options_batch(wxbot.session(aid))
    except wxpub.LoginLostError as exc:
        invalidate_opts_cache(aid)
        return _stale(str(exc)) | {"login": True}
    except Exception as exc:  # noqa: BLE001
        return _stale(_wx_msg(exc))
    if not isinstance(data, dict) or data.get("error"):
        return _stale((data or {}).get("error") or "这次没读到，点一下重试。")

    data = _clean_opts(data)
    if _opts_valid(data):
        _opts_write(aid, data)
    out = dict(data)
    out.update({"cached": False, "stale": False, "age": 0})
    me = auth.current_user(request)
    audit.log_event(me, "hookcut.publish", target=f"account:{aid}",
                    payload={"op": "options_all", "refresh": bool(refresh),
                             "n": {k: len((v or {}).get("items") or [])
                                   for k, v in data.items() if isinstance(v, dict)}},
                    request=request, route="/api/publish/options/all", status_code=200)
    return out


# ── 选文件夹（和选文件一样，必须走原生对话框拿绝对路径）───────────────────
@router.post("/api/publish/pick-folder")
def pick_folder(request: Request):
    _require(request)
    result: dict = {"path": ""}
    err: dict = {}

    def _pick():
        try:
            result["path"] = nativedlg.pick_folder("选择放着视频的文件夹",
                                                   pickmem.recall_dir("publish_dir"))
        except Exception as exc:  # noqa: BLE001
            err["msg"] = str(exc)

    t = threading.Thread(target=_pick, daemon=True)
    t.start()
    t.join(timeout=300)
    if err:
        raise HTTPException(500, f"弹不出选择框（{err['msg']}）")
    path = result["path"] or ""
    if not path:
        return {"path": "", "files": []}
    pickmem.remember("publish_dir", path, True)
    files = publisher.scan_dir(path)
    return {"path": path, "files": files}


# ── 「最近用过」的文件夹点一下就列视频：不走原生对话框 ─────────────────────
@router.get("/api/publish/dir-files")
def dir_files(request: Request, path: str = ""):
    """界面上点历史文件夹时用：直接扫这个目录，等于「选文件夹」但省掉弹框。"""
    _require(request)
    if not path or not os.path.isdir(path):
        raise HTTPException(404, "这个文件夹不在了，重新选一个吧")
    try:
        return {"path": path, "files": publisher.scan_dir(path)}
    except publisher.PublishError as exc:
        raise HTTPException(400, str(exc)) from exc


# ── 全局概览（顶栏角标 / 到点提醒）────────────────────────────────────────
@router.get("/api/publish/overview")
def overview(request: Request):
    """待发汇总。队列是全局的，直接读一个文件，不再扫全部项目。"""
    me = auth.current_user(request)
    g = gate(request, me)
    if not g["ok"]:
        return {"gate": g, "pending": 0, "due": [], "accounts": []}
    s = publisher.summarize()
    return {"pending": s["pending"], "due": s["due"], "counts": s["counts"],
            "accounts": [{"id": a["id"], "name": a["name"]}
                         for a in publisher.public_accounts()],
            "gate": g}


# ── 数据（作品列表 / 播放流速）与账号诊断 ────────────────────────────────
# 这两块都要开浏览器去号里拉，一次 8-15 秒，所以和发布主流程严格分开：
# 前端只有切到「数据 / 诊断」Tab 才调，绝不在打开发布页时预热 ——
# 不然客户每次进这一页都要干等十几秒，还以为软件卡了。
DIAG_FILE = os.path.join(core.DATA_DIR, "publish_diag.json")


def _require_current(aid: str = "", what: str = "读数据") -> str:
    """发布 / 数据 / 诊断的号，只认页面当前确认的那一个。

    旧版 `_any_online` 会回退到 batch.account_id、再回退到「随便一个在线号」，
    实测结果就是发布页选 A、数据页显示 B（2026-11-13 截图）。现在宁可拒绝，
    也不猜 —— 号错了比没数据危险得多。
    """
    cur = str((publisher.queue().get("current_account_id") or "")).strip()
    if not cur:
        raise HTTPException(409, "先在页面顶部确认是哪个账号，再看数据")
    if aid and aid != cur:
        raise HTTPException(
            409, f"当前确认的是【{_acc_label(publisher.get_account(cur))}】，"
                 f"不能对别的号{what}。要换号请到「发布」页点另一张账号卡片。")
    return cur


def _absorb_profile(aid: str, ac: dict) -> None:
    """把号里读到的名称 / 视频号 ID / 粉丝数写回账号记录。

    界面上要显示「ID sph...」，而登录时只抓了 DOM 里的昵称 —— ID 只能从
    `auth/auth_data` 拿。这里统一收口：登录后补一次、采数据时也顺手更新。
    """
    patch: dict = {}
    if ac.get("nick"):
        patch["wx_name"] = str(ac["nick"])[:30]
    if ac.get("finder_id"):
        patch["finder_id"] = str(ac["finder_id"])[:40]
    if ac.get("fans"):
        patch["fans"] = int(ac["fans"])
    if ac.get("avatar"):
        patch["avatar"] = ac["avatar"]
    if not patch:
        return
    try:
        publisher.update_account(aid, **patch)
    except Exception:  # noqa: BLE001  账号刚好被删了，不该把这次请求弄挂
        pass


_PROF_BUSY: set = set()
_PROF_TRIED: dict = {}
_PROF_GAP = 600          # 同一个号补资料失败后，10 分钟内不再试（别每 2 秒开一次浏览器）


def _fill_profile_async(aid: str, force: bool = False) -> None:
    """登录成功后补一次视频号资料（ID/粉丝数/昵称头像）。放后台线程：
    登录态是 2 秒轮询的，不能在这儿等 6 秒。
    force=True（登录成功这种明确事件）绕过失败节流；平时探测仍按节流，
    别为了资料反复开浏览器。"""
    now = time.time()
    if aid in _PROF_BUSY or (not force and now - _PROF_TRIED.get(aid, 0) < _PROF_GAP):
        return
    _PROF_BUSY.add(aid)
    _PROF_TRIED[aid] = now

    def _run():
        try:
            _absorb_profile(aid, wxstat.profile(wxbot.session(aid)))
        except Exception:  # noqa: BLE001
            pass
        finally:
            _PROF_BUSY.discard(aid)

    threading.Thread(target=_run, daemon=True, name=f"prof-{aid}").start()


def _load_diag() -> dict:
    try:
        import json
        with open(DIAG_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_diag(d: dict) -> None:
    import json
    os.makedirs(os.path.dirname(DIAG_FILE), exist_ok=True)
    tmp = DIAG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, DIAG_FILE)


@router.get("/api/publish/stat")
def publish_stat(request: Request, aid: str = ""):
    """这个号上次采到的作品数据。读本地文件，毫秒级 —— 打开发布页不等它。"""
    _require(request)
    cur = _require_current(aid)
    return wxstat.cached(cur)


@router.get("/api/publish/monitor")
def monitor_get(request: Request, aid: str = ""):
    """流速监控的状态（开没开、下一轮还有几分钟）。"""
    _require(request)
    cur = _require_current(aid)
    return {**wxmon.status(), "aid": cur}


@router.post("/api/publish/monitor")
def monitor_set(request: Request, on: bool = False, every_min: int = 0, aid: str = ""):
    """开/关后台采样。只采当前确认的号；换号会自动停，绝不继续采旧号。"""
    _require(request)
    cur = _require_current(aid, "开监控")
    return wxmon.set_on(on, every_min or None, aid=cur)


@router.post("/api/publish/stat/sample")
def publish_stat_sample(request: Request, aid: str = ""):
    """真去号里采一次：作品列表 + 账号概览，顺带把播放流速的点接上。"""
    _require(request)
    aid = _require_current(aid, "采样")
    acc = publisher.get_account(aid)
    if not acc:
        raise HTTPException(404, "先在页面顶部确认账号，数据要从这个号里读")
    if acc.get("state") != "online":
        raise HTTPException(400, "这个号的登录态不在了。先点「重登」登录一下，再来读数据")
    try:
        snap = wxstat.sample(wxbot.session(aid), aid=aid)
        _absorb_profile(aid, snap.get("account") or {})
        return snap
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, _wx_msg(exc)) from exc


@router.get("/api/publish/diag")
def publish_diag_list(request: Request, aid: str = ""):
    """所有号上一次的体检结果（不触发检测）。界面默认只展开当前号，
    其余放进折叠区 —— 「一眼看全号」留着，但不许拿别的号的数据当主视图。"""
    _require(request)
    cur = _require_current(aid, "看体检")
    rows = _load_diag()
    accs = publisher.public_accounts()
    out = []
    for a in accs:
        r = rows.get(a["id"]) or {}
        out.append({"aid": a["id"], "name": a.get("name") or "",
                    "avatar": a.get("avatar") or "",
                    "state": a.get("state") or "unknown",
                    "checked_at": r.get("checked_at") or "",
                    "login": r.get("login"), "wxstat": r.get("wxstat"),
                    "live": r.get("live"), "heat": r.get("heat"),
                    "fans_limit": r.get("fans_limit"), "popup": r.get("popup"),
                    "bad_posts": r.get("bad_posts") or [],
                    "abnormal": r.get("abnormal") or [],
                    # 没体检过的号也要能显示视频号 ID：从账号记录兜底，
                    # 不能因为「还没体检」就让这一列空着
                    "account": r.get("account") or {
                        "nick": a.get("wx_name") or "",
                        "finder_id": a.get("finder_id") or "",
                        "avatar": a.get("avatar") or "",
                        "fans": a.get("fans") or 0},
                    "note": r.get("note") or ""})
    return {"rows": out, "max_accounts": publisher.MAX_ACCOUNTS,
            "current_account_id": cur}


@router.post("/api/publish/diag/{aid}")
def publish_diag_one(aid: str, request: Request):
    """检测一个号；判不准的项一律返回 unknown，绝不假装「正常」。

    为什么要讲这一条：客户看到假的「账号正常」，第二天被封了会认为软件骗人。
    「未判定」虽然不好看，但它是诚实的 —— 页面上会写清「这一项平台没给出可读信号」。
    """
    _require(request)
    _require_current(aid, "体检")
    acc = publisher.get_account(aid)
    if not acc:
        raise HTTPException(404, "这个账号已经不在了")

    row = {"aid": aid, "name": acc.get("name") or "",
           "state": acc.get("state") or "unknown",
           "checked_at": time.strftime("%Y-%m-%dT%H:%M"),
           "bad_posts": [], "account": {}, "note": ""}

    st = acc.get("state") or "unknown"
    row["login"] = (
        {"ok": True, "text": "已登录"} if st == "online" else
        {"ok": False, "text": "登录过期，要重扫"} if st == "expired" else
        {"ok": False, "text": "还没登录"} if st == "pending" else
        {"ok": None, "text": "未判定"}
    )

    if st != "online":
        # 没登录就什么都读不到，别硬跑（跑了也是 15 秒后报错）
        for k in ("wxstat", "live", "heat", "fans_limit", "popup"):
            row[k] = {"ok": None, "text": "未判定"}
        row["note"] = "先把这个号重新登录，才能读平台上的状态"
        _persist_diag(aid, row)
        return row

    try:
        snap = wxstat.sample(wxbot.session(aid), deep=True, aid=aid)
    except Exception as exc:  # noqa: BLE001
        row["wxstat"] = {"ok": None, "text": "读不到（网络或页面异常）"}
        row["live"] = {"ok": None, "text": "未判定"}
        row["heat"] = {"ok": None, "text": "未判定"}
        row["fans_limit"] = {"ok": None, "text": "未判定"}
        row["popup"] = {"ok": None, "text": "未判定"}
        row["note"] = _wx_msg(exc)
        _persist_diag(aid, row)
        return row

    ac = snap.get("account") or {}
    _absorb_profile(aid, ac)
    spam = int(ac.get("spam") or 0)
    row["account"] = {"nick": ac.get("nick") or "", "avatar": ac.get("avatar") or "",
                      "finder_id": ac.get("finder_id") or "",
                      "fans": ac.get("fans") or 0, "feeds": ac.get("feeds") or 0}
    if ac.get("nick"):
        row["wxstat"] = ({"ok": True, "text": "正常"}
                         if spam == 0 else
                         {"ok": False, "text": "平台给了风险标记"})
    else:
        row["wxstat"] = {"ok": None, "text": "未判定"}

    # 直播状态：作品列表页拿不到，也不值得为它多开一页 → 明确写未判定
    row["live"] = {"ok": None, "text": "未判定"}

    heat = snap.get("heat") or {}
    conds = [c for c in (heat.get("conds") or []) if isinstance(c, dict)]
    # 平台的 conditionList 只有「达成/未达成」两态，不给原因。所以这里只报「开没开通」，
    # 把条件原文放进 detail 让人自己判断 —— 直接写「不符合内容规范」会被客户读成
    # 「我的号违规了」，而它可能只是粉丝不够（2026-11-13 实测本号 9 粉就出现这一项）。
    if heat.get("open") == 1:
        row["heat"] = {"ok": True, "text": "已开通加热"}
    elif conds:
        row["heat"] = {
            "ok": None, "text": "未开通加热",
            "detail": "；".join(
                f"{c.get('description') or ''}"
                f"（{'达成' if c.get('conditionStatus') != 2 else '未达成'}）"
                for c in conds),
        }
    else:
        row["heat"] = {"ok": None, "text": "未判定"}

    # 这两项平台没有可读信号，只能标未判定 —— 猜一个「正常」比不显示更糟
    row["fans_limit"] = {"ok": None, "text": "未判定"}
    row["popup"] = {"ok": None, "text": "未判定"}

    # 违规只能从通知中心看：作品列表里的 disableInfo 是功能级开关，
    # 实测首页正常的作品也会是 true，拿它判违规会全员误报（2026-11-13）。
    ntf = snap.get("notify") or {}
    hits = ntf.get("hits") or []
    row["bad_posts"] = hits
    # 「近期异常视频」：只有真读到信号的才列（平台通知 / 不在公域可见），
    # 不按播放量低去推测限流 —— 那种猜测客户拿去申诉会对不上平台记录。
    row["abnormal"] = snap.get("abnormal") or []
    row["notify"] = {"ok": ntf.get("ok"), "unread": ntf.get("unread") or 0,
                     "n": len(hits)}
    if snap.get("note"):
        row["note"] = snap["note"]
    _persist_diag(aid, row)
    return row


def _persist_diag(aid: str, row: dict) -> None:
    d = _load_diag()
    d[aid] = row
    _save_diag(d)


# ── 开发期探针（分发版不存在）────────────────────────────────────────────
class ProbeBody(BaseModel):
    aid: str
    what: str = "create"          # create / list
    click: str = ""
    net: bool = False
    visible: bool = False         # 探针要不要有窗口（默认无头）
    steps: list[dict] | None = None   # 给了就按步骤操作（校准弹窗用）


@router.post("/api/publish/open-platform")
def open_platform(body: OpenPlatformBody, request: Request):
    """用当前确认的号打开视频号助手（免扫码的可见窗口）。

    只认页面当前那个号；窗口是独立的可见浏览器，不会重启后台无头实例，
    也就不会打断正在跑的采样 / 预取 / 自动发表。
    """
    _require(request)
    aid = _require_current(body.aid, "打开视频号助手")
    try:
        res = wxopen.open_platform(aid)
    except Exception as exc:  # noqa: BLE001
        res = {"ok": False, "note": _wx_msg(exc)}
    me = auth.current_user(request)
    audit.log_event(
        me, "hookcut.publish.set",
        target="account:" + aid,
        payload={"op": "open_platform", "aid": aid,
                 "ok": bool(res.get("ok")),
                 "logged_in": bool(res.get("logged_in"))},
        request=request, route="/api/publish/open-platform", status_code=200)
    return {**res, "aid": aid}


@router.post("/api/publish/abn/delete")
def abn_delete(body: AbnDeleteBody, request: Request):
    """删掉当前号里的一条作品（不可逆）。

    两道闸：① 只能删「当前确认的号」，不许跨号；② 默认 dry_run，真删还要
    confirm=True，界面上已经把作品名 / 时间 / 不可恢复写全问过一次。
    删完后端会回读列表，标题还在就不算成功。
    """
    _require(request)
    aid = _require_current(body.aid, "删作品")
    if not body.dry_run and not body.confirm:
        raise HTTPException(400, "删除不可恢复，请先在界面上确认一次")
    if not (body.title or "").strip() and not (body.post_id or "").strip():
        raise HTTPException(400, "没给作品标题或编号，不会猜要删哪一条")
    try:
        res = wxdel.delete_post(wxbot.session(aid), post_id=body.post_id,
                                title=body.title, dry_run=body.dry_run)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "found": False, "aid": aid, "note": _wx_msg(exc)}
    if not body.dry_run:
        me = auth.current_user(request)
        audit.log_event(
            me, "hookcut.publish.set",
            target="post:" + (body.post_id or body.title[:20]),
            payload={"op": "delete_post", "aid": aid, "title": body.title[:40],
                     "ok": bool(res.get("ok")), "verified": res.get("verified") or ""},
            request=request, route="/api/publish/abn/delete", status_code=200)
    return {**res, "aid": aid}


@router.post("/api/publish/_probe")
def probe(body: ProbeBody, request: Request):
    """只读把视频号页面的真实结构倒出来，用来校准选择器。

    为什么要有：发布页是异步组件（合集下拉、位置搜索、定时开关都是后加载的），
    选择器靠猜能跑一次、改版后静默填错。分销版直接 404 —— 客户不需要、也不该碰。
    """
    _require(request)
    if not _dev_mode():
        raise HTTPException(404, "Not Found")
    if not publisher.get_account(body.aid):
        raise HTTPException(404, "这个账号已经不在了")
    # 探针是开发期在改的文件，每次调用重新加载，省得为了看一眼 DOM 重启服务
    # （重启会把正在登录的浏览器一起关掉）。分发版走不到这里。
    wx = wxpub
    try:
        import importlib
        wx = importlib.reload(wxpub)
    except Exception:  # noqa: BLE001
        pass
    url = {
        "create": wx.CREATE_URL,
        "list": wx.LIST_URL,
        "platform": wx.PLATFORM_URL,
    }.get(body.what, wx.CREATE_URL)
    try:
        if body.steps:
            data = wx.act(wxbot.session(body.aid), body.steps, url=url, net=body.net,
                          visible=body.visible)
        else:
            data = wx.dump(wxbot.session(body.aid), url=url,
                           click=body.click, net=body.net, visible=body.visible)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, _wx_msg(exc)) from exc
    name = f"probe-{body.what}" + ("-net" if body.net else "")
    p = wx.save_dump(data, name)
    data["saved"] = p
    # 用户发回排查时优先给这一份：完整 dump 只留开发机
    data["summary_saved"] = p[:-5] + ".summary.json"
    return data
