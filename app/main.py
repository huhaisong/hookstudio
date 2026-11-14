"""钩子工坊：本机跑的小工具，导入剧集 → 转写 → AI 出结构 → 出片。"""
from __future__ import annotations

import os
import re
import threading
import time

import requests

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from urllib.parse import quote

from . import admin as admin_api
from . import (aigate, analyze, audit, auth, batch, bgmlib, buildstamp, core, db,
               nosub, publishapi, publisher, report, render, shots, shortcut,
               transcribe, update, vmerge, wxbot, wxmon, wxworker)

# 版本号只在这里定义一次，构建指纹模块也用它（buildstamp.VERSION）
APP_VERSION = buildstamp.VERSION
WEB = core.WEB

app = FastAPI(title="钩子工坊")
app.mount("/static", StaticFiles(directory=WEB), name="static")

# ── 用户体系 / 埋点接入（2026-11-04，改动只有这一段）───────────────────────
# 三个 router 各自独立成文件，业务路由一行没动：
#   auth   注册/登录/刷新/登出/改资料/改密码
#   admin  用户管理（封禁/解封/角色）、系统开关、日志查询（在 audit.router 里）
#   audit  操作日志查询/统计/导出
app.include_router(auth.router)
app.include_router(admin_api.router)
app.include_router(audit.router)
# 批量定时发布（2026-11-13）：pro 专属，门禁在 publishapi._require 里，
# 顶栏入口对所有账号可见——免费用户点开会看到「需要专业版」，这是正常的销售钩子。
app.include_router(publishapi.router)


@app.middleware("http")
async def _static_no_cache(request, call_next):
    """前端文件禁止缓存。

    本机工具迭代时改 web/ 很频繁，而浏览器会把 style.css / app.js 缓存住，
    普通 F5 有时只更新了 JS、CSS 还是旧的，界面就出现「代码改了却没生效」的怪现象
    （2026-11-02 实测：位置条画出来了但样式没加载，整列空白）。文件很小，禁用缓存没有代价。
    """
    resp = await call_next(request)
    if request.url.path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


# 唯一新增的全局中间件：鉴权 + 自动埋点（见 audit.GuardMiddleware 的注释）。
# 放在 _static_no_cache 之后注册 = 它在外层，能拿到包含静态处理在内的完整耗时。
# 注意：只拦 /api/，静态资源与页面直接放行（否则登录页自己也进不去）。
app.add_middleware(audit.GuardMiddleware)

# ── 页面输出：注入构建指纹（2026-11-04 版本闸）────────────────────────────
# 为什么不再直接 FileResponse：
#   ① HTML 缓存住以后，用户会拿着旧页面调新接口（点了没反应 / 参数不认），
#      而我们要的是「服务器一更新，旧页面立刻停用，刷新后才能继续」；
#   ② 所以页面本身 no-store，且把 /static/x.js 改成 /static/x.js?v=<指纹>，
#      指纹一变 URL 就变，浏览器不可能再拿旧脚本。
_STATIC_REF = re.compile(r"(/static/[\w./-]+\.(?:js|css))(?:\?v=[\w.-]*)?")
_BUILD_META = re.compile(r'<meta\s+name="app-build"[^>]*>', re.I)
_HEAD_OPEN = re.compile(r"<head[^>]*>", re.I)


def _page(name: str) -> HTMLResponse:
    """读出 HTML → 注入指纹与资源版本号 → no-store 返回。

    每次请求现读文件（几十 KB，不心疼）：这样改完前端不重启也能立刻生效，
    和「指纹一变就锁屏」是配套的。
    """
    build = buildstamp.current()
    with open(os.path.join(WEB, name), encoding="utf-8") as f:
        html = f.read()
    html = _STATIC_REF.sub(lambda m: f"{m.group(1)}?v={build}", html)
    meta = f'<meta name="app-build" content="{build}">'
    if _BUILD_META.search(html):
        html = _BUILD_META.sub(meta, html, count=1)
    else:
        html = _HEAD_OPEN.sub(lambda m: m.group(0) + "\n" + meta, html, count=1)
    return HTMLResponse(html, headers={
        # no-store 是硬要求：宁可每次多读一次磁盘，也不要让人拿着旧页面继续操作
        "Cache-Control": "no-store, must-revalidate",
        "X-App-Build": build,
    })


# 页面路由：登录页 / 管理后台是新增的独立页面，不动 index.html 的主流程。
@app.get("/login")
def login_page():
    return _page("auth.html")


@app.get("/admin")
def admin_page():
    """本机管理后台。

    **只给开发机 / 自用部署**：它管的是这台电脑自己的 sqlite（用户、操作日志）。
    账号已经搬到服务器（2026-11-04），分发版里每个人都是自己电脑上唯一的用户，
    这个页面对终端用户没有意义，还会让他以为自己在管一群人——直接 404。
    运营要看所有用户，去服务器的 /admin（见 deploy/proxy/admin.html）。
    """
    if core.load_config().get("dist_mode"):
        raise HTTPException(404, "Not Found")
    return _page("admin.html")


def _step_incomplete(p: dict, kind: str) -> bool:
    """判断某一步是否确实没做完（用数据说话，不靠内存状态）。"""
    eps = p.get("episodes") or []
    if kind == "transcribe":
        return bool(eps) and any(not ep.get("lines") for ep in eps)
    if kind == "analyze":
        return (bool(eps) and all(ep.get("lines") for ep in eps)
                and not (p.get("edl") or p.get("edl_text")))
    if kind == "render":
        return bool(p.get("edl")) and not p.get("output")
    if kind == "batch":
        plans = [pl for pl in (p.get("plans") or []) if pl.get("on", True)]
        done = {s.get("index") for s in (p.get("sets") or []) if s.get("output")}
        return bool(plans) and any(pl["index"] not in done for pl in plans)
    if kind == "plan":
        return False  # 编排结构是几秒的本地整理，中断了重跑一次即可
    if kind == "nosub":
        # 去字幕副本：看实际生成了几集，没齐就续跑（已完成的集会自动跳过）
        return len((p.get("nosub") or {}).get("files") or {}) < len(eps)
    return False


@app.on_event("startup")
def resume_interrupted():
    """开机自愈：先做路径/Key 迁移，再把上次中断的任务接着跑，不用人再点。"""
    # 用户库：建表 + 首次创建管理员。放在最前面——后面的路由和中间件都要用表。
    # 失败不阻塞启动：本机工具的主要功能（转写/出片）不该因为一个用户库起不来就全废。
    try:
        db.init()
        auth.ensure_admin()
    except Exception as exc:  # noqa: BLE001
        print(f"[auth] 用户库初始化失败（登录/埋点将不可用）：{exc}")
    # 后台预热模型（约 10 秒）：不阻塞启动，用户打开项目就能直接转写，不用先干等加载
    threading.Thread(target=transcribe.warmup, daemon=True).start()
    # 版本巡检（exe 分发必需，见 app/update.py）：本机指纹闸只管「页面 vs 本机后端」，
    # 分发给别人的 exe 两者永远同版，所以必须由服务器说「这版还能不能用」。
    update.start()
    # 日志上报：把本机 audit_logs 增量推到服务器（exe 上日志只在本机，后台看不见）
    report.start()
    # 单文件安装包会解到 %LOCALAPPDATA%，用户找不到入口 → 首次启动补一个桌面图标
    shortcut.ensure()
    # 上次删账号没删干净的浏览器目录（*.trash-*）在这里清掉
    try:
        n = wxbot.purge_trash()
        if n:
            print(f"[wx] 清理残留浏览器目录 {n} 个")
    except Exception as exc:  # noqa: BLE001
        print(f"[wx] 清理残留目录失败：{exc}")
    # 播放流速采样：用户上次开着监控就继续跑（平台只给累计播放量，
    # 流速只能自己隔一段时间采一次看差值）。默认关闭，不替用户耗资源。
    try:
        wxmon.start()
    except Exception as exc:  # noqa: BLE001
        print(f"[pub] 流速采样线程没起来：{exc}")
    # 上次传到一半被关掉的（publishing 卡住的）先放回队列，再决定要不要起线程
    try:
        publisher.reset_stuck_publishing()
    except Exception as exc:  # noqa: BLE001
        print(f"[pub] 卡住条目重置失败：{exc}")
    # 完结超过 7 天的条目（已定时/已提交/已取消）自动归档：已交平台的行不摆
    # 任何按钮，不放归档队列只会越积越多（2026-11-14 批2 遗留清单）
    try:
        n = publisher.archive_old_jobs()
        if n:
            print(f"[pub] 归档了 {n} 条 7 天前的已完成条目（queue_archive.json）")
    except Exception as exc:  # noqa: BLE001
        print(f"[pub] 队列归档失败：{exc}")
    # 排到即上传（2026-11-13 用户定稿）：队列里还有 queued 的 = 上次传到一半
    # 程序关了，开机就接着传。上传完成后到点由平台自己发，不用谁守着。
    #
    # **必须延后到启动钩子外面起**（2026-11-14 实测）：启动钩子里直接
    # wxworker.start()，续传那条会撞上 Playwright 的
    # 「It looks like you are using Playwright Sync API inside the asyncio loop」，
    # 一开机就把队列里的条目挨个记一次失败。这里挪到一个纯 sleep 过的线程里，
    # 等 startup 这一段（连同 uvicorn 的事件循环）彻底跑完再起上传。
    try:
        if any(j.get("status") == "queued"
               for j in publisher.queue().get("jobs", [])):
            def _resume_upload() -> None:
                time.sleep(5)
                try:
                    wxworker.start()
                    print("[pub] 队列里有没传完的，接着逐条上传")
                except Exception as exc:  # noqa: BLE001
                    print(f"[pub] 上传线程没起来：{exc}")
            threading.Thread(target=_resume_upload, name="pub-resume",
                             daemon=True).start()
    except Exception as exc:  # noqa: BLE001
        print(f"[pub] 上传线程没起来：{exc}")
    try:
        mig = core.migrate_legacy()
        if mig["paths_fixed"] or mig["key_migrated"]:
            print(f"[migrate] 修正历史项目路径 {mig['paths_fixed']} 个"
                  f"，迁移 API Key：{'是' if mig['key_migrated'] else '否'}")
    except Exception as exc:  # noqa: BLE001
        print(f"[migrate] 跳过：{exc}")
    for row in core.list_projects():
        pid = row["id"]
        p = core.load(pid)
        if not p:
            continue
        kind = core.task_flag(pid)
        if not kind:
            continue
        if _step_incomplete(p, kind):
            if kind == "batch":
                core.start_task(pid, "batch", lambda pid=pid: batch.render_plans(pid))
            else:
                # 第 2 步（nosub）拆成独立组后不再写 task.json，重启不自动续跑：
                # 它自带「设置没变就跳过已生成集」的断点续做，重新点一次即可，
                # 而且不能让它的中断标记把第 1/3 步的按钮锁在「正在续跑」上。
                # 映射里留着 nosub 只为兼容极老的标记文件。
                fn = {"transcribe": transcribe.run, "analyze": analyze.run,
                      "render": render.run, "nosub": nosub.run}[kind]
                core.start_task(pid, kind, lambda fn=fn, pid=pid: fn(pid))
        else:
            # 标记还在但其实已经做完（比如死在最后一步保存前），清掉即可
            core.clear_task_flag(pid)


class NewProject(BaseModel):
    path: str = ""          # 「只做引流分镜」的项目不导入视频，可以没有目录
    name: str = ""
    target_minutes: int = 10
    # 2026-11-05 起只保留两种：batch=批量混剪 / shots=只做引流分镜。
    # 「单片导流」已下线（用户要求），传 funnel 会按 batch 处理，老项目仍能打开。
    mode: str = "batch"
    # 新建时直接复用另一个项目的转写台词（同一批素材不必重跑 Whisper）
    reuse_from: str = ""


class ConfigBody(BaseModel):
    llm_key: str | None = None
    llm_base: str | None = None
    llm_model: str | None = None
    shots_model: str | None = None
    ds_key: str | None = None
    whisper_tier: str | None = None
    output_dir: str | None = None
    # 本机渲染偏好：auto=基准测试后自动决定 / off=强制 CPU。是「这台电脑」
    # 的设置而不是全站行为，登录用户都能改（同 output_dir）。
    hw_encode: str | None = None
    # 本机操作日志是否上报到后台。开发机默认关（否则自己的调试动作全进生产库），
    # 打包版默认开。只有非分发版能改，见 put_config。
    report_audit: bool | None = None
    # 同时生成几条（第 2 步多组 / 第 3 步多条共用），1-8，本机偏好。
    max_concurrent: int | None = None


class EdlBody(BaseModel):
    text: str


@app.get("/")
def index():
    return _page("index.html")


@app.get("/api/status")
def status():
    cfg = core.llm()
    tier = core.whisper_tier()
    mdir = core.model_dir(tier)
    ai = aigate.server_ai()
    return {
        # llm_key 保留旧字段名（前端/脚本在读），语义改成「服务端 AI 是否就绪」
        "llm_key": bool(ai["ready"]),
        "ai": ai,
        "model": cfg["model"],
        "provider": cfg.get("provider") or "bailian",
        "whisper_tier": tier,
        "whisper_model": os.path.isfile(os.path.join(mdir, "model.bin")),
        "whisper_dir": mdir,
        "ffmpeg": os.path.isfile(core.ff("ffmpeg")),
        "base": core.BASE,
        "data": core.DATA_DIR,
        "version": APP_VERSION,
        # 构建指纹：前端拿它和页面里的 <meta name="app-build"> 比对，
        # 对不上就说明「服务器换了新版、这个页面还是旧的」→ 锁屏要求刷新
        "build": buildstamp.current(),
        # 前端启动时用它判断「没凭证就直接去登录页」。恒为 True（见 auth.auth_required）
        "auth_required": auth.auth_required(),
        # 远程版本闸：服务器说「低于 min 就不能用了」时前端弹不可关闭的升级卡
        "update": update.state(),
        "dist_mode": bool(core.load_config().get("dist_mode")),
    }


@app.get("/api/config")
def get_config(request: Request):
    """设置页：**Key 一律不回显**，普通人只被告知「由服务端统一提供」。

    2026-11-04 用户要求「AI Key 不由用户自己填写」：普通账号看不到 Key 输入框，
    也看不到掩码提示（掩码本身也是信息）；只有管理员能看到「服务端凭证」那一栏。
    """
    cfg = core.llm()
    key = cfg["key"]
    info = core.sysinfo()
    # 普通用户也能读配置（用来显示成片目录），但拿不到任何 Key 相关线索
    me = auth.optional_user(request)
    is_admin = bool(me and me.get("role") == "admin")
    # 分发版连掩码都不给：aBc123…wxyz 这 6 位已经够判断「是不是我那把 Key」，
    # 而且终端用户本来就不该知道这台机器上有没有配 Key（Key 在服务端）。
    owner = is_admin and not core.load_config().get("dist_mode")
    tiers = [{
        "key": core.TIER,
        "name": "small · 精准",
        "note": "中文准确率高，随安装包提供，无需选择",
        "available": bool(info["models"].get(core.TIER)),
        "recommended": True,
    }]
    out_dir = core.output_dir()# 额度：没填 Key 时才真正走代理；填了 Key 也查一下，让用户知道还有多少次
    trial = {"url": (cfg.get("proxy_url") or core.PROXY_URL_DEFAULT), "quota": None}
    # 令牌只能用账号令牌（2026-11-04 晚起服务端不认匿名设备令牌了），
    # 没登录就别问——问也是 401，白白在界面上闪一下「服务暂时连不上」。
    tok = core.ai_token()
    if not key and tok:
        try:
            r = requests.get(f"{trial['url'].rstrip('/')}/api/quota",
                             headers={"X-HS-Token": tok}, timeout=5)
            if r.status_code == 200:
                trial["quota"] = r.json()
        except Exception:
            pass
    dskey = core.load_config().get("ds_key") or ""
    ai = aigate.server_ai()
    return {
        # admin=True 时前端才渲染「服务端 AI 凭证」两栏（Key 输入框）
        "admin": is_admin,
        "ai": ai,
        "llm_key_set": bool(key),
        "llm_key_hint": ((key[:6] + "…" + key[-4:]) if len(key) > 12 else ("已设置" if key else "")) if owner else "",
        "llm_base": cfg["base"],
        "llm_model": cfg["model"],
        "llm_provider": cfg.get("provider") or "bailian",
        "shots_model": core.load_config().get("shots_model") or "",
        # 本机渲染偏好（登录用户可改，见 put_config）
        "hw_encode": core.load_config().get("hw_encode") or "auto",
        # 同时生成上限：前端第 2 步的「预计耗时」也按这个值算
        "max_concurrent": core.max_concurrent(),
        "ds_key_set": bool(dskey),
        "ds_key_hint": ((dskey[:6] + "…" + dskey[-4:]) if len(dskey) > 12 else ("已设置" if dskey else "")) if owner else "",
        "trial": trial,
        "whisper_tier": core.whisper_tier(),
        "tiers": tiers,
        "system": {"cpu": info["cpu"], "mem_gb": info["mem_gb"]},
        "paths": {"base": core.BASE, "data": core.DATA_DIR,
                  "models": core.MODELS, "prompts": core.PROMPTS,
                  "output": out_dir, "output_free_gb": core.free_gb(out_dir),
                  "data_free_gb": core.free_gb(core.DATA_DIR)},
        "version": APP_VERSION,
        # 分发版标志：前端据此隐藏 Key / 模型 / 服务端凭证这些「只有开发者才该看」的栏位
        "dist_mode": bool(core.load_config().get("dist_mode")),
        # 日志上报**实际生效值**（不是配置里的原值）：没显式设置时跟随分发形态，
        # 前端要显示的就是「现在到底报不报」，否则勾选框和实际行为会打架。
        "report_audit": report.enabled(),
        "update": update.state(),
    }


@app.put("/api/config")
def put_config(body: ConfigBody, request: Request):
    """改设置（Key / 模型 / 成片目录）。

    **按字段分权限**（2026-11-04 用户要求「Key 不由用户自己填」）：
      · 模型 / Key / 转写档位 = 服务端凭证与全站行为 → 只有管理员能改；
      · 成片保存位置 = 个人偏好 → 任何登录用户都能改。
    原来整条接口要求管理员，结果普通用户连「改自己的成片目录」都被 403，
    只能把凭据一起放开或者干脆都不让改，两种都不对。
    """
    me = auth.current_user(request)
    patch = {}
    if body.llm_key is not None:
        patch["llm_key"] = body.llm_key.strip()
    if body.llm_base:
        patch["llm_base"] = body.llm_base.strip()
    if body.llm_model:
        patch["llm_model"] = body.llm_model.strip()
    # shots_model 允许存空串（= 跟主模型一致），所以用 is not None 判断
    if body.shots_model is not None:
        patch["shots_model"] = body.shots_model.strip()
    if body.ds_key is not None:
        patch["ds_key"] = body.ds_key.strip()
    if body.whisper_tier and body.whisper_tier != core.TIER:
        raise HTTPException(400, f"只提供 {core.TIER} 模型")
    if body.output_dir is not None:
        d = body.output_dir.strip()
        if d:
            if not os.path.isabs(d):
                raise HTTPException(400, "成片保存位置要填完整路径，例如 D:\\钩子工坊成片")
            try:
                os.makedirs(d, exist_ok=True)
                probe = os.path.join(d, ".write_test")
                with open(probe, "w", encoding="utf-8") as f:
                    f.write("ok")
                os.remove(probe)
            except OSError as exc:
                raise HTTPException(400, f"这个目录写不进去：{exc}")
        patch["output_dir"] = d
    if body.hw_encode is not None:
        v = body.hw_encode.strip().lower()
        if v not in ("auto", "off"):
            raise HTTPException(400, "显卡加速只能选「自动」或「关闭」")
        patch["hw_encode"] = v
    # 同时生成上限：本机偏好，任何登录用户都能改（同 hw_encode）。
    # 必须夹在 1-8——0 会让线程池直接抛错，999 会把上游账号打限流。
    if body.max_concurrent is not None:
        v = body.max_concurrent
        if isinstance(v, bool) or not isinstance(v, int) \
                or not (core.CONCURRENT_MIN <= v <= core.CONCURRENT_MAX):
            raise HTTPException(400, f"同时生成条数只能设为 {core.CONCURRENT_MIN}-{core.CONCURRENT_MAX}")
        patch["max_concurrent"] = v
    # 日志上报：开发机可开可关（排查「后台为什么没有我的记录」时用）。
    # 分发版不给关——关了你在后台就彻底失去排障线索，用户报「点了没反应」时什么都看不到。
    if body.report_audit is not None:
        if core.load_config().get("dist_mode"):
            raise HTTPException(400, "安装版固定上报运营日志，不能关闭")
        patch["report_audit"] = bool(body.report_audit)
    # 服务端凭证 / 全站模型：普通用户改了会影响所有人（更会改走平台的额度）
    # ds_key 早先漏在这张表外——普通用户填自己的 DeepSeek Key 会把全站调用切到他那边，
    # 反过来也可能被用来「顶掉」管理员的配额，2026-11-04 复查时补上。
    admin_only = {"llm_key", "llm_base", "llm_model", "shots_model", "ds_key"}
    touched = sorted(admin_only & set(patch))
    if touched and me.get("role") != "admin":
        raise HTTPException(
            403, f"只有管理员能改{'、'.join(touched)}；成片保存位置可以自己改")
    # 分发版（exe 给终端用户）：AI 凭证由官方统一提供，界面上不露输入框，
    # 接口这层也要挡住——否则用 curl 照样能改自己的 Key，然后绕过体验额度计数。
    if touched and core.load_config().get("dist_mode"):
        raise HTTPException(
            403, "这是官方分发版，AI 凭证由后台统一提供，不需要也不能自己填写")

    cfg = core.save_config(patch)
    return {"ok": True, "llm_key_set": bool(cfg.get("llm_key")),
            "whisper_tier": core.whisper_tier(),
            "output_dir": core.output_dir(),
            "output_free_gb": core.free_gb(core.output_dir())}


class LlmTestBody(BaseModel):
    """设置页「测试连通」的入参：都留空就用已保存的配置。"""
    model: str | None = None
    llm_key: str | None = None
    ds_key: str | None = None


@app.post("/api/llm-test")
def llm_test(body: LlmTestBody | None = None):
    """3 秒回答「这套 Key + 这个模型现在到底能不能用」。

    13 个模型横评里有 4 个失败，用户都是等十几分钟任务跑完才知道
    「没开通 / 欠费 / 限流」。探针把这三类原因分开说，还能先测通再保存。
    """
    b = body or LlmTestBody()
    return analyze.probe(b.model or None, b.llm_key or None, b.ds_key or None)


@app.get("/api/models-available")
def models_available(refresh: int = 0):
    """哪些模型此刻真能通。前端「模型」下拉只放这里 ok 的型号。

    refresh=1 现场重测（约 8 秒，14 个并发）；平时读 6 小时缓存，毫秒返回。
    """
    return analyze.available_models(refresh=bool(refresh))


# ── AI 授权票据（2026-11-04，见 app/aigate.py）──────────────────────────────
# 用户要求：生成引流分镜 / 生成剪辑结构不许让用户自己填 Key，一律用服务端的。
# 前端不再持有任何 Key，而是在生成前先来这里换一张 15 分钟、绑定「人 + 项目 + 用途」
# 的票据；/plan 与 /shots 验票通过才真正发 AI 请求。
class GrantBody(BaseModel):
    project_id: str
    purpose: str        # plan | shots


@app.post("/api/ai/grant")
def ai_grant(body: GrantBody, request: Request):
    """申领一次 AI 调用授权。**这一步就是埋点里带项目名的位置**。"""
    me = auth.current_user(request)
    g = aigate.issue(me, body.project_id, body.purpose)
    # 手写埋点：中间件只知道「有人打了这个接口」，不知道是哪个项目、什么用途。
    # target 记成 `project:剧名`，后台日志里直接看得到是哪部剧。
    audit.log_event(
        me, "ai.grant", target=audit.target_of(body.project_id),
        payload={"purpose": body.purpose, "purpose_cn": g["purpose_cn"],
                 "model": g["model"], "provider": g["provider"], "mode": g["mode"],
                 "project": g["project"]["name"]},
        request=request, route="/api/ai/grant", status_code=200)
    return g


def _grant_of(request: Request, body_grant: str = "") -> str:
    """票据位置：优先请求头（前端用这个），其次 body（方便脚本/curl 调试）。"""
    return (request.headers.get("x-ai-grant") or body_grant or "").strip()


@app.get("/api/roots")
def roots():
    """列本机所有盘符（不只是 C）。用系统位掩码拿，比逐个字母试探快，也不会卡在断开的网络盘上。"""
    out = []
    if os.name == "nt":
        mask = 0
        try:
            import ctypes

            mask = ctypes.windll.kernel32.GetLogicalDrives()
        except Exception:  # noqa: BLE001
            pass
        letters = [chr(ord("A") + i) for i in range(26) if mask & (1 << i)] or list("CDEFG")
        for letter in letters:
            d = f"{letter}:\\"
            if os.path.isdir(d):
                out.append({"label": f"{letter}: 盘", "path": d})
    else:
        out.append({"label": "/", "path": "/"})
    return out


def _videos_in(path: str) -> list[str]:
    try:
        return [f for f in os.listdir(path) if f.lower().endswith((".mp4", ".mov", ".mkv"))]
    except OSError:
        return []


@app.get("/api/browse")
def browse(path: str = ""):
    """浏览本机目录：这是本机工具，用户读自己的盘，不再限制挂载点。"""
    if not path:
        path = (roots()[0]["path"] if roots() else core.BASE)
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise HTTPException(404, "目录不存在")
    dirs = []
    try:
        entries = sorted(os.listdir(path), key=core.natural_key)
    except OSError as exc:
        raise HTTPException(403, f"无法读取这个目录：{exc.strerror}") from exc
    for name in entries:
        full = os.path.join(path, name)
        if not os.path.isdir(full) or name.startswith("."):
            continue
        dirs.append({"name": name, "path": full, "videos": len(_videos_in(full))})
    parent = os.path.dirname(path.rstrip("\\/"))
    if parent == path or len(path.rstrip("\\/")) <= 3:
        parent = None
    return {
        "path": path,
        "win": core.win_path(path),
        "parent": parent,
        "dirs": dirs,
        "videos": len(_videos_in(path)),
    }


@app.get("/api/projects")
def projects():
    return core.list_projects()


"""转写台词复用 / 导入（2026-11-05 用户需求）：
「第一个项目已经转写好了，新建一个同样的项目能不能把台词复制过来；
或者我从别处拿到台词，能不能直接粘进来。」

两条路：
  ① 复用已有项目：按**文件名**匹配集（同一批素材，文件名必然一致），
     源项目该集有台词才复制，没转的集留空，之后照常自动转写补上。
  ② 粘贴台词：只收带时间码的文本——剪辑要靠时间码切视频，没有时间码等于没有台词。
     认三种格式：本程序「下载台词 .txt」的原样（`1. (2.7-4.9) 文本`）、
     SRT（`00:00:02,700 --> 00:00:04,940`）、以及 `起-止 文本` 的简写；
     多集用 `【第03集】` 分段，没有分段时整段归到用户选的那一集。
"""


class ReuseLines(BaseModel):
    from_pid: str


class ImportLines(BaseModel):
    text: str
    ep: int = 0             # 0 = 按【第NN集】自动分段；>0 = 整段都算这一集


class EditLines(BaseModel):
    ep: int                 # 只改这一集，其余集原样不动
    text: str


def _copy_lines_from(src_pid: str, proj: dict) -> int:
    """把 src_pid 的台词按文件名搬到 proj 上，只填空集。返回补了几集。

    为什么按文件名而不是集序：同一批素材重新建项目，文件名一模一样，
    但索引可能因为多了/少了文件而错位，按索引搬会把第 3 集的台词贴到第 2 集上。
    """
    if not src_pid or src_pid == proj.get("id"):
        return 0
    src = core.load(src_pid)
    if not src:
        raise HTTPException(404, "要复用的项目不存在")
    by_file = {}
    for ep in src.get("episodes") or []:
        lines = ep.get("lines") or []
        if lines:
            by_file[ep.get("file") or ""] = lines
    if not by_file:
        raise HTTPException(400, f"「{src.get('name')}」还没有转写好的台词")
    n = 0
    for ep in proj.get("episodes") or []:
        if ep.get("lines"):
            continue                      # 已经有台词的集不覆盖
        lines = by_file.get(ep.get("file") or "")
        if lines:
            ep["lines"] = [dict(ln) for ln in lines]
            ep["status"] = "done"
            ep.pop("error", None)
            n += 1
    if n:
        proj["stage"] = "transcribed"
        core.save_merge(proj, ["episodes", "stage"])
    return n


def _ts_to_sec(s: str) -> float:
    """`00:00:02,700` / `00:02.7` / `2.7` 都转成秒。"""
    s = str(s).strip().replace(",", ".")
    if "-->" in s:
        s = s.split("-->")[0]
    parts = s.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except ValueError:
        return -1.0


_RE_EP = re.compile(r"^【?\s*第\s*(\d+)\s*集")
_RE_SRT = re.compile(r"^(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})")
# 本程序导出的行：`12. (2.7-4.9) 文本`；也认 `(2.7-4.9) 文本`、`2.7-4.9 文本`
_RE_SPAN = re.compile(r"^\s*(?:\d+\s*[.、]\s*)?\(?\s*(\d+(?:\.\d+)?)\s*[-–~]\s*"
                      r"(\d+(?:\.\d+)?)\s*\)?\s*(.*)$")


def _parse_transcript(text: str, default_ep: int = 0) -> dict[int, list[dict]]:
    """把带时间码的文本解析成 {集号: [{start,end,text}]}。

    default_ep=0 表示按【第NN集】分段；整篇没有分段时抛 400，让用户自己选是哪一集
    ——猜错集的代价是整集切错，必须让用户明确。
    """
    out: dict[int, list[dict]] = {}
    cur_ep = default_ep
    pending = None            # SRT 两行时间码之后、等待文字的那一对
    buf: list[str] = []

    def flush():
        nonlocal pending, buf
        if pending and buf:
            txt = " ".join(x.strip() for x in buf if x.strip()).strip()
            if txt:
                out.setdefault(cur_ep, []).append(
                    {"start": round(pending[0], 2), "end": round(pending[1], 2), "text": txt})
        pending, buf = None, []

    for raw in str(text or "").splitlines():
        s = raw.strip().replace("\ufeff", "")
        if not s:
            flush()                       # 空行是 SRT 一条的结束
            continue
        m = _RE_EP.match(s)
        if m:
            flush()
            cur_ep = int(m.group(1))
            continue
        m = _RE_SRT.match(s)
        if m:
            flush()
            pending = (_ts_to_sec(m.group(1)), _ts_to_sec(m.group(2)))
            continue
        m = _RE_SPAN.match(s)
        # 纯数字行（SRT 序号）不要当成时间码
        if m and not s.isdigit():
            flush()
            a, b, txt = float(m.group(1)), float(m.group(2)), (m.group(3) or "").strip()
            if b > a >= 0:
                if txt:
                    out.setdefault(cur_ep, []).append(
                        {"start": round(a, 2), "end": round(b, 2), "text": txt})
                else:
                    pending = (a, b)      # 时间码在上一行、文字在下一行
                continue
        if pending is not None:
            buf.append(s)                 # SRT 正文（可能多行）
    flush()

    if not out:
        raise HTTPException(400, "没认出带时间码的台词。请粘贴「下载台词 .txt」的原样内容，"
                                 "或 SRT 字幕（每句前面有 00:00:02,700 --> 00:00:04,940）")
    for lines in out.values():
        lines.sort(key=lambda x: x["start"])
    return out


def _apply_lines(proj: dict, parsed: dict[int, list[dict]]) -> int:
    """把解析结果写进项目（按集号或按文件名顺序对齐），返回写了几集。"""
    eps = proj.get("episodes") or []
    if not eps:
        raise HTTPException(400, "这个项目没有导入视频，不能导入台词")
    by_index = {int(e.get("index") or 0): e for e in eps}
    # 允许「只写了 1 段、没标集号」的情况：整段给第 1 集
    if 0 in parsed and 0 not in by_index:
        parsed = {eps[0]["index"]: parsed[0]}
    n = 0
    for ep_i, lines in parsed.items():
        ep = by_index.get(int(ep_i))
        if not ep or not lines:
            continue
        ep["lines"] = lines
        ep["status"] = "done"
        ep.pop("error", None)
        n += 1
    if n:
        proj["stage"] = "transcribed"
        core.save_merge(proj, ["episodes", "stage"])
    return n


@app.post("/api/projects/{pid}/reuse-lines")
def reuse_lines(pid: str, body: ReuseLines, request: Request):
    """把另一个项目的台词按文件名搬过来（只填还没台词的集）。"""
    me = auth.current_user(request)
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    n = _copy_lines_from(body.from_pid, p)
    if not n:
        raise HTTPException(400, "没有可复用的集：要么源项目没转写，要么文件名对不上")
    total = sum(len(e.get("lines") or []) for e in p["episodes"])
    audit.log_event(me, "project.transcript_reuse", target=audit.target_of(pid),
                    payload={"from": body.from_pid, "copied": n, "lines": total},
                    request=request, status_code=200)
    return {"copied": n, "episodes": len(p["episodes"]), "line_total": total}


@app.post("/api/projects/{pid}/import-lines")
def import_lines(pid: str, body: ImportLines, request: Request):
    """粘贴带时间码的台词，覆盖对应集。"""
    me = auth.current_user(request)
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    text = (body.text or "").strip()
    if len(text) < 10:
        raise HTTPException(400, "台词太短了，检查一下是不是没粘上")
    parsed = _parse_transcript(text, default_ep=int(body.ep or 0))
    n = _apply_lines(p, parsed)
    if not n:
        raise HTTPException(400, "第几集对不上：项目里没有这些集号")
    total = sum(len(e.get("lines") or []) for e in p["episodes"])
    audit.log_event(me, "project.transcript_import", target=audit.target_of(pid),
                    payload={"episodes": n, "lines": total, "chars": len(text)},
                    request=request, status_code=200)
    return {"episodes": n, "line_total": total}


@app.post("/api/projects")
def create(body: NewProject, request: Request):
    """建项目。mode=shots 是「只做引流分镜」：不导入视频、不转写，只留一个名字，
    第 3 步直接粘剧情素材就能跑（用户要求引流分镜可独立使用）。
    手动埋点带**最终确定的项目名**：名字留空时服务端默认取文件夹名，中间件记的
    是请求体原文（name:""），后台看到的就成了一条空名记录（2026-11-05 用户指出）。
    手动埋完中间件自动跳过（audited 标记），失败请求仍由中间件兜底记录。
    """
    me = auth.current_user(request)
    # 「单片导流」模式 2026-11-05 下线：老项目照常打开，但不允许再新建
    mode = body.mode if body.mode in ("batch", "shots") else "batch"
    if mode == "shots":
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "给这个分镜项目起个名字")
        proj = core.create("", name, max(1, body.target_minutes), [], "shots")
        proj["stage"] = "shots"
        core.save_merge(proj, ["stage"])
        audit.log_event(me, "project.create", target=f"project:{name}",
                        payload={"mode": "shots",
                                 "target_minutes": max(1, body.target_minutes)},
                        request=request, status_code=200)
        return proj

    path = os.path.abspath(body.path)
    if not os.path.isdir(path):
        raise HTTPException(404, "目录不存在")
    files = sorted(_videos_in(path), key=core.natural_key)
    if not files:
        raise HTTPException(400, "这个目录里没有视频文件")
    episodes = []
    for i, f in enumerate(files, 1):
        info = core.ffprobe(os.path.join(path, f))
        episodes.append({
            "index": i, "file": f, "duration": info["duration"],
            "width": info["width"], "height": info["height"],
            "status": "pending", "lines": [],
        })
    name = body.name.strip() or os.path.basename(path)
    proj = core.create(path, name, max(1, body.target_minutes), episodes, mode)
    audit.log_event(me, "project.create", target=f"project:{name}",
                    payload={"mode": mode, "name": name, "path": path,
                             "episodes": len(episodes),
                             "target_minutes": max(1, body.target_minutes),
                             "reuse_from": body.reuse_from or ""},
                    request=request, status_code=200)
    # 复用台词：同一批素材不必重跑一遍 Whisper（用户 2026-11-05 要求）。
    # 按文件名对齐，只填源项目已转好的集；没对上的集下面照常自动转写补上。
    if body.reuse_from:
        try:
            _copy_lines_from(body.reuse_from, proj)
        except HTTPException:
            # 复用失败不该把建项目一起搞崩：留空转写即可
            pass
    # 还有没台词的集才自动转写：全复用上了就不用跑 Whisper。
    if any(not (e.get("lines")) for e in proj["episodes"]):
        core.start_task(proj["id"], "transcribe", lambda pid=proj["id"]: transcribe.run(pid))
    return proj


@app.get("/api/projects/{pid}")
def project(pid: str):
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    # 前端不需要完整台词，只给统计
    slim = dict(p)
    slim["episodes"] = [
        {k: v for k, v in ep.items() if k != "lines"} | {"line_count": len(ep.get("lines") or [])}
        for ep in p["episodes"]
    ]
    slim["line_total"] = sum(len(ep.get("lines") or []) for ep in p["episodes"])
    slim["edl_total"] = round(sum(s["end"] - s["start"] for s in p.get("edl") or []), 1)
    slim["task"] = core.get_task(pid)                  # 第 1 步 + 第 3 步（cuts）
    slim["task_shots"] = core.get_task(pid, "shots")   # 第 4 步引流分镜
    slim["task_nosub"] = core.get_task(pid, "nosub")   # 第 2 步去字幕（独立并行）
    return slim


class CancelTaskBody(BaseModel):
    group: str = ""     # "" = 三条产线都停；"cuts"/"nosub"/"shots" 只停对应那条


@app.post("/api/projects/{pid}/task/cancel")
def cancel_project_task(pid: str, body: CancelTaskBody | None = None):
    """中止这个项目正在跑的后台任务（转写 / 去字幕 / 编排 / 出片 / 分镜都算）。

    协作式退出：下一个进度点就停，正在跑的 ffmpeg 由 core 直接杀掉；
    同时清掉 task.json，重启时不会把「用户主动中止」当成中断任务自动续跑。
    """
    if not core.load(pid):
        raise HTTPException(404, "项目不存在")
    group = ((body.group if body else "") or "").strip()
    # 白名单从任务分组表动态取，别写死：以后再加一条产线，这里不用跟着改，
    # 也不会出现「能跑起来却中止不了」的口子。
    if group and group not in set(core._TASK_GROUP.values()):
        raise HTTPException(400, "产线参数不对")
    hit = core.cancel_task(pid, group or None)
    return {"ok": True, "cancelled": hit}


@app.delete("/api/projects/{pid}")
def delete(pid: str, force: int = 0):
    """删项目。这个项目出的片还被发布队列指着时先拦一次，用户确认后带 force=1 再来。

    队列是全局的（不挂在项目下），所以删项目**不会**删掉发布记录 —— 但那几条
    作业指向的文件就在这个项目的输出目录里，删了之后它们会变成「文件找不到了」。
    与其留一堆坏死记录，不如提前问一句（2026-11-13 用户定）。
    """
    p = core.load(pid)
    if p:
        n = publisher.jobs_of_project(pid)
        if n and not force:
            raise HTTPException(
                409,
                f"这个项目还有 {n} 条视频等着发布（在「发布」里排着）。"
                f"删掉项目会把这些视频文件一起删掉，那几条发布记录就发不出去了。"
                f"确实要删就再点一次。")
    d = os.path.join(core.PROJECTS, pid)
    if os.path.isdir(d):
        core.rmtree(d)
    return {"ok": True}


# ── 台词整理之后的一步：去掉原字幕 + 在原位置加回自己的字幕（2026-11-11）──
# 逐集把原字幕抹掉，再按该集台词（lines）把新字幕烧在原字幕的位置上，
# 产出每集一条独立视频；后面的「剪辑与出片」直接从这些视频切段。
# 2026-11-12 用户定：做法固定「柔和抹掉」、位置全自动识别、字幕固定加回去，
# 所以 mode/auto/newsub 不再由界面传，nosub.clean_cfg 里一律强制。
class NoSubCfg(BaseModel):
    soft: float = 0.5         # 柔和程度（唯一必调的参数）
    newsub_motion: float = 0.5  # 动效幅度 0-1（只影响上浮位移与句间错位）
    newsub_style: str = "rise"  # 出场动效：fade/rise/pop/float/type
    # 抹掉的地方怎么补：mirror 照旁边画面镜像（快）/ borrow 借同镜头干净帧
    # （慢一倍，强纹理背景更自然）。没声明的字段 Pydantic 会直接丢掉，
    # 前端传上来也会被吃——加新可调项时别忘了在这里同步加一行。
    fill_mode: str = "mirror"


@app.put("/api/projects/{pid}/nosub-config")
def put_nosub_config(pid: str, body: NoSubCfg):
    """只记住这一步的设置，不开跑——调滑杆时防抖保存，下次打开还在。"""
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    cfg = nosub.clean_cfg(body.model_dump())
    p["nosub_cfg"] = cfg
    core.save_merge(p, ["nosub_cfg"])
    return {"ok": True, "cfg": cfg}


@app.post("/api/projects/{pid}/nosub")
def do_nosub(pid: str, body: NoSubCfg | None = None):
    """按当前设置逐集生成「已处理」视频（去原字幕 + 在原位置加自己的字幕）。

    已生成且参数（含台词内容）没变的集会自动跳过。"""
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    raw = body.model_dump() if body else (p.get("nosub_cfg") or {})
    cfg = nosub.clean_cfg(raw)
    # 字幕固定要加，没台词就没得加：当场说清楚，别等后台任务失败
    if not any((e.get("lines") or []) for e in (p.get("episodes") or [])):
        raise HTTPException(400, "还没有整理台词，先去第 1 步「台词整理」做完再来")
    p["nosub_cfg"] = cfg
    core.save_merge(p, ["nosub_cfg"])
    # 第 2 步要按台词烧字幕，不能和「第 1 步自动整理台词」同时跑（那边正在
    # 改写台词）。第 3 步的编排/出片不写台词，可以并行——这正是拆组的目的。
    if core.running_kind(pid, "cuts") == "transcribe":
        raise HTTPException(409, "正在自动整理台词，等它跑完再生成处理好的视频")
    if not core.start_task(pid, "nosub", lambda: nosub.run(pid)):
        raise HTTPException(409, "这一步已经在生成了，等它跑完再试")
    return {"ok": True}


def _open_native(path: str) -> None:
    if os.name == "nt":
        os.startfile(path)  # noqa: S606 —— 本机工具，打开自己生成的文件
    else:
        import subprocess

        subprocess.Popen(["xdg-open", path])


@app.post("/api/projects/{pid}/nosub/open")
def open_nosub(pid: str):
    """打开「已处理」视频所在的文件夹，方便直接拿去别的工具里用。"""
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    d = nosub.out_dir(pid)
    if not os.path.isdir(d) or not (p.get("nosub") or {}).get("files"):
        raise HTTPException(404, "还没有生成处理好的视频")
    try:
        _open_native(d)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"打不开文件夹：{exc}") from exc
    return {"ok": True, "path": d}


@app.post("/api/projects/{pid}/nosub/preview")
def preview_nosub(pid: str, ep: int = 0):
    """用系统播放器打开某一集「已处理」的视频，先看清效果再决定出片用哪份。"""
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    files = (p.get("nosub") or {}).get("files") or {}
    target = None
    for e in p.get("episodes") or []:
        if ep and int(e.get("index") or 0) != ep:
            continue
        f = files.get(str(e.get("file") or ""))
        if f and os.path.isfile(os.path.join(nosub.out_dir(pid), f)):
            target = os.path.join(nosub.out_dir(pid), f)
            break
    if not target:
        raise HTTPException(404, "这一集还没有生成处理好的视频")
    try:
        _open_native(target)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"打不开视频：{exc}") from exc
    return {"ok": True, "path": target}


@app.post("/api/projects/{pid}/transcribe")
def do_transcribe(pid: str):
    if not core.load(pid):
        raise HTTPException(404, "项目不存在")
    if not core.start_task(pid, "transcribe", lambda: transcribe.run(pid)):
        raise HTTPException(409, "这个项目已有任务在跑")
    return {"ok": True}


@app.post("/api/projects/{pid}/transcribe-ep")
def do_transcribe_ep(pid: str, body: dict):
    """重转写单集（同步返回）：排障用，也是「这一集没转好，重试一下」的入口。"""
    import time as _time

    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    if core.task_running(pid, "cuts"):
        raise HTTPException(409, "这个项目已有任务在跑，等它结束再试")
    idx = body.get("index")
    ep = next((e for e in p["episodes"] if e["index"] == idx), None)
    if not ep:
        raise HTTPException(404, f"没有第 {idx} 集")
    src = os.path.join(p["src_dir"], ep["file"])
    if not os.path.isfile(src):
        raise HTTPException(400, f"源文件不存在：{ep['file']}")
    t0 = _time.time()
    try:
        lines = transcribe.transcribe_episode(src)
    except Exception as exc:  # noqa: BLE001
        latest = core.load(pid) or p
        for e in latest["episodes"]:
            if e["index"] == ep["index"]:
                e["status"] = "error"
                e["error"] = f"{type(exc).__name__}: {exc}"
                break
        core.save_merge(latest, ["episodes"])
        raise HTTPException(500, f"{type(exc).__name__}: {exc}")
    # 基于磁盘最新状态合并保存，避免覆盖任务期间其它线程写入的数据
    latest = core.load(pid) or p
    for e in latest["episodes"]:
        if e["index"] == ep["index"]:
            e["lines"] = lines
            e["status"] = "done"
            e["error"] = ""
            break
    core.save_merge(latest, ["episodes"])
    return {"index": ep["index"], "lines": len(lines),
            "seconds": round(_time.time() - t0, 1)}


@app.post("/api/projects/{pid}/analyze")
def do_analyze(pid: str):
    if not core.load(pid):
        raise HTTPException(404, "项目不存在")
    if not core.start_task(pid, "analyze", lambda: analyze.run(pid)):
        raise HTTPException(409, "这个项目已有任务在跑")
    return {"ok": True}


@app.put("/api/projects/{pid}/edl")
def put_edl(pid: str, body: EdlBody):
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    edl = analyze.parse_text(body.text)
    if not edl:
        raise HTTPException(400, "表格里没有可用的行")
    # 越界的行夹回该集时长，避免 ffmpeg 切到黑帧
    durs = {ep["index"]: ep["duration"] for ep in p["episodes"]}
    for seg in edl:
        cap = durs.get(seg["ep"], 0)
        if not cap:
            continue
        seg["start"] = max(0.0, min(seg["start"], cap - 1))
        seg["end"] = max(seg["start"] + 1, min(seg["end"], cap))
    p["edl"] = edl
    p["edl_text"] = body.text
    p["stage"] = "analyzed"
    core.save_merge(p, ["edl", "edl_text", "stage"])
    return {"ok": True, "segments": len(edl),
            "total": round(sum(s["end"] - s["start"] for s in edl), 1)}


@app.post("/api/projects/{pid}/render")
def do_render(pid: str):
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    fx0 = (p.get("batch") or {}).get("fx_default") or {}
    ver = str(fx0.get("src_ver") or "").strip() if isinstance(fx0, dict) else ""
    if ver in ("nosub", "live") and core.task_running(pid, "nosub"):
        raise HTTPException(409, "第 2 步还在生成处理好的视频，这次出片要用的素材还没做完。"
                                 "等它跑完再出片，或者把「用哪份视频」改成原始视频")
    if not core.start_task(pid, "render", lambda: render.run(pid)):
        raise HTTPException(409, "这个项目已有任务在跑")
    return {"ok": True}


class PlanJob(BaseModel):
    """第 2 步的一个「模型任务」：用哪个模型、出几套、每套多长。"""
    model: str = ""          # 留空 = 跟设置里的默认模型（llm_model）
    count: int = 3
    min_minutes: float = 3.0
    max_minutes: float = 7.0


class PlanBody(BaseModel):
    count: int = 3
    min_minutes: float = 3.0
    max_minutes: float = 7.0
    # 顺序只剩集序一种（2026-11-03 用户决定）。老请求体里传来的 "auto"/"varied"
    # 照收，但 batch.ai_plan_run 里一律归一到集序，不再做任何重排。
    order: str = "chrono"
    model: str = ""                    # 单模型（老前端/脚本用）
    jobs: list[PlanJob] | None = None  # 多模型对比：每个模型各自的套数/时长


class HookcutBody(BaseModel):
    """单集钩子前置：要出哪几集、钩子多长、用哪个模型。"""
    eps: list[int] = []       # 空 = 全部集
    hook_sec: int = 15        # 10 / 15 / 20 三档，别的值就近归一
    model: str = ""           # 留空 = 跟设置里的默认模型


class PlansSelectBody(BaseModel):
    on: list[bool]


class PromptEditBody(BaseModel):
    """「修改提示词」保存的内容（编辑框提交的完整模板文本）。"""
    text: str


# 可被用户自定义的提示词：界面名 -> 文件名。加新的一步就往这里登记，
# 路由/埋点/前端都按这个名字走，不用再复制三份专用接口。
# 2026-11-08 用户要求「所有提示词都暴露出来、自己改」，于是把原来写死在代码里的
# 五份也搬成文件登记进来：通用 system、第 3 步的 user 模板 / 输出格式 / 每镜固定要求。
# 「输出格式」三份保存前会验 JSON（analyze._check_schema），改坏字段名直接拒绝保存。
_PROMPTS = {
    "plans": "plans.txt",               # 第 2 步：批量混剪编排（指令）
    "plans_schema": "plans_schema.txt",  # 第 2 步：编排的输出格式（JSON 交卷示例）
    "hooks": "hooks.txt",               # 第 2 步：单集钩子前置（指令）
    "hooks_schema": "hooks_schema.txt",  # 第 2 步：钩子的输出格式
    "shots": "shots.txt",               # 第 3 步：分镜创作通则（system）
    "shots_user": "shots_user.txt",     # 第 3 步：分镜要求（user 模板，含花括号占位符）
    "shots_schema": "shots_schema.txt",  # 第 3 步：分镜输出格式
    "shots_rules": "shots_rules.txt",   # 第 3 步：控制句 / 每镜要求 / 合规声明
    "funnel": "funnel.txt",             # 第 1 步：单片导流（仅历史项目还能跑到）
    "system": "system.txt",             # 通用输出要求（第 1/2 步共用）
}


# 提示词对使用者只读（2026-11-12 用户要求）：前端入口已收，这里再兜一层，
# 免得直接调接口把模板改坏、整批生成解析不出来。开发时带
# HOOK_ALLOW_PROMPT_EDIT=1 启动即可放开。
_PROMPT_EDIT_ON = os.environ.get("HOOK_ALLOW_PROMPT_EDIT") == "1"


def _prompt_file(key: str) -> str:
    fname = _PROMPTS.get(key)
    if not fname:
        raise HTTPException(404, f"没有叫 {key} 的提示词")
    return fname


@app.get("/api/prompts/{key}")
def get_prompt(key: str):
    """「修改提示词」：返回当前生效的提示词（自定义版优先）。"""
    return analyze.read_prompt_view(_prompt_file(key))


@app.put("/api/prompts/{key}")
def put_prompt(key: str, body: PromptEditBody, request: Request):
    """保存用户改过的提示词，下次生成立即生效。

    落盘到 data/prompts_custom/<文件名>（用户数据，重装不丢）。
    **必须埋点**：运营侧要知道用户改成了什么样——payload 里带修改后的全文，
    会随日志上报进服务器 /admin/logs。"""
    fname = _prompt_file(key)
    if not _PROMPT_EDIT_ON:
        raise HTTPException(403, "提示词已锁定：这一版不开放修改")
    me = auth.current_user(request)
    try:
        analyze.save_custom_prompt(fname, body.text)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit.log_event(me, f"prompt.{key}.save",
                    payload={"chars": len(body.text.strip()), "text": body.text.strip()},
                    request=request)
    return {"ok": True, "chars": len(body.text.strip()), "is_custom": True}


@app.delete("/api/prompts/{key}")
def delete_prompt(key: str, request: Request):
    """恢复内置默认提示词（删自定义文件）。同样埋点。"""
    _prompt_file(key)
    if not _PROMPT_EDIT_ON:
        raise HTTPException(403, "提示词已锁定：这一版不开放修改")
    me = auth.current_user(request)
    analyze.reset_custom_prompt(_prompt_file(key))
    audit.log_event(me, f"prompt.{key}.reset", request=request)
    return {"ok": True, "is_custom": False}


@app.post("/api/projects/{pid}/plan")
def do_plan(pid: str, body: PlanBody, request: Request):
    """第 2 步：把全剧台词交给 AI，一次排出 N 套结构（走 1 次 API）。

    AI 看得到完整剧情，能把这套排成讨债线、那套排成反击线。结果落在 p["plans"]，
    后续出片流程完全一样。界面上的「生成 N 套结构」就是它。

    **AI 用服务端的 Key**：前端不持有 Key，必须先 `POST /api/ai/grant` 换票据，
    带 `X-AI-Grant` 头来调用（见 app/aigate.py）。
    """
    me = auth.current_user(request)
    aigate.require(_grant_of(request), pid, "plan", me)
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    if not (p.get("episodes") or []):
        raise HTTPException(400, "还没有转写结果，请先转写")
    # AI 一次输出不了太多套（每套十几块，输出会过长易截断），上限压到 20
    order = body.order if body.order in ("auto", "chrono", "varied") else "chrono"
    if body.jobs:
        # 多模型对比：每个模型各自的套数/时长，依次各走 1 次 API
        jobs = [{"model": (j.model or "").strip(), "count": max(1, min(20, int(j.count))),
                 "min_minutes": max(0.5, float(j.min_minutes)),
                 "max_minutes": max(0.5, float(j.max_minutes))} for j in body.jobs]
        total = sum(j["count"] for j in jobs)
        runner = lambda: batch.ai_plan_multi(pid, jobs, order)
    else:
        count = max(1, min(20, int(body.count)))
        mn = max(0.5, float(body.min_minutes))
        mx = max(0.5, float(body.max_minutes))
        jobs = [{"model": (body.model or "").strip(), "count": count,
                 "min_minutes": mn, "max_minutes": mx}]
        total = count
        runner = lambda: batch.ai_plan_multi(pid, jobs, order)

    # 任务跑在后台线程里，中间件那条日志只知道「有人点了生成」，不知道跑成功没有。
    # 真正的消耗在下面这条：谁、哪个项目、几个模型、跑了几分钟、成没成。
    t0 = time.perf_counter()
    payload = {"sets": total, "models": [j["model"] or "(默认)" for j in jobs],
               "minutes": [f"{j['min_minutes']}-{j['max_minutes']}" for j in jobs],
               "order": order}

    def job():
        try:
            runner()
        except Exception as exc:  # noqa: BLE001
            audit.log_event(me, "plan.fail", target=audit.target_of(pid), payload=payload,
                            result="fail", message=str(exc)[:300], status_code=500,
                            method="POST", route="/api/projects/{id}/plan",
                            duration_ms=int((time.perf_counter() - t0) * 1000),
                            request=request)
            raise
        audit.log_event(me, "plan.done", target=audit.target_of(pid), payload=payload,
                        method="POST", route="/api/projects/{id}/plan",
                        duration_ms=int((time.perf_counter() - t0) * 1000),
                        request=request)

    if not core.start_task(pid, "plan", job):
        raise HTTPException(409, "这个项目已有任务在跑")
    return {"ok": True, "count": total, "order": order, "models": len(jobs)}


@app.post("/api/projects/{pid}/hookcut-plan")
def do_hookcut_plan(pid: str, body: HookcutBody, request: Request):
    """第 2 步的第二种编排方式：**单集钩子前置**。

    每集挑一段 10/15/20 秒的爆点提到最前面，接这一集的完整正片，一集一条成片。
    跟 /plan 平级、共用同一份转写结果（不重新转写），结构同样落在 p["plans"]
    ——**会覆盖现有的多套混剪结构**，前端点之前会确认。

    为什么复用 p["plans"] 而不是另起一个字段：出片、勾选、单集失败隔离、
    fx 指纹重渲、断点续跑全都是按 plans 走的，另起一份等于把整条管线复制一遍。

    **AI 用服务端的 Key**：与 /plan 一样先换票据（purpose=hook）。
    """
    me = auth.current_user(request)
    aigate.require(_grant_of(request), pid, "hook", me)
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    if not (p.get("episodes") or []):
        raise HTTPException(400, "还没有转写结果，请先转写")
    all_eps = [int(e["index"]) for e in p["episodes"]]
    eps = [e for e in (body.eps or []) if e in set(all_eps)]
    hk = batch._norm_hook_sec(body.hook_sec)
    model = (body.model or "").strip()
    total = len(eps) or len(all_eps)
    t0 = time.perf_counter()
    payload = {"eps": total, "hook_sec": hk, "model": model or "(默认)"}

    def job():
        try:
            batch.hookcut_run(pid, eps, hk, model or None)
        except Exception as exc:  # noqa: BLE001
            audit.log_event(me, "hookcut.fail", target=audit.target_of(pid),
                            payload=payload, result="fail", message=str(exc)[:300],
                            status_code=500, method="POST",
                            route="/api/projects/{id}/hookcut-plan",
                            duration_ms=int((time.perf_counter() - t0) * 1000),
                            request=request)
            raise
        audit.log_event(me, "hookcut.done", target=audit.target_of(pid),
                        payload=payload, method="POST",
                        route="/api/projects/{id}/hookcut-plan",
                        duration_ms=int((time.perf_counter() - t0) * 1000),
                        request=request)

    if not core.start_task(pid, "plan", job):
        raise HTTPException(409, "这个项目已有任务在跑")
    return {"ok": True, "count": total, "hook_sec": hk}


class ShotsBody(BaseModel):
    count: int = 3
    duration: int = 60      # 15 / 30 / 60 秒
    seg_len: int = 15       # 分段长度：15 / 30 秒（视频模型一次能出多长）
    aspect: str = "9:16"
    style: str = ""
    cast: str = ""
    script: str = ""        # 自定义剧情素材：填了就用它当唯一剧情依据，不再读本剧台词
    ctl: bool = False       # 段首带「对话式生成器控制句」：粘进对话框才需要，默认不带


class ShotsLayoutBody(BaseModel):
    ctl: bool | None = None      # 不传 = 不动
    style: str | None = None     # 不传 = 不动；传空串 = 回到默认画风


@app.post("/api/projects/{pid}/shots")
def do_shots(pid: str, body: ShotsBody, request: Request):
    """第 3 步：引流分镜——AI 读完剧情重新设计 15/30/60s 短片，再切成 15 秒生成单元。

    剧情来源二选一：默认读本剧 Whisper 台词；body.script 非空时只认这段素材
    （此时项目里没有转写结果也能跑）。
    只走 1 轮调用：AI 写分镜。切段全由程序做（对齐镜头边界、等比拉齐），
    每段都是能单独投喂视频模型的完整提示词（2026-11-07 起不再二次调 AI 按段重写）。
    结果落在 p["shots"]（结构化数据 + 整篇 Markdown + 每段单独的 Markdown），
    并在成片输出目录存一份 .md 方便直接拿去投喂视频模型。

    **AI 用服务端的 Key**：与 /plan 一样，先换 `ai-grant` 票据（purpose=shots）。
    """
    me = auth.current_user(request)
    aigate.require(_grant_of(request), pid, "shots", me)
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    script = (body.script or "").strip()
    if not script and not (p.get("episodes") or []):
        raise HTTPException(400, "还没有转写结果，请先转写；或提供一段剧情素材")
    duration = 60 if body.duration >= 60 else (30 if body.duration >= 30 else 15)
    # 分段长度（2026-11-05 加 30 秒选项）：只能 15 / 30，且不能比整条还长
    seg_len = 30 if int(body.seg_len or 15) >= 30 else 15
    if seg_len > duration:
        seg_len = duration

    # 分镜是 1 轮调用，几十秒到几分钟，结果只能在这里记
    t0 = time.perf_counter()
    payload = {"count": max(1, min(10, body.count)), "duration": duration,
               "seg_len": seg_len,
               "aspect": body.aspect, "material": "custom" if script else "script"}

    def job():
        try:
            shots.shots_run(pid, body.count, duration, body.aspect, body.style,
                            body.cast, script, seg_len, bool(body.ctl))
        except Exception as exc:  # noqa: BLE001
            audit.log_event(me, "shots.fail", target=audit.target_of(pid), payload=payload,
                            result="fail", message=str(exc)[:300], status_code=500,
                            method="POST", route="/api/projects/{id}/shots",
                            duration_ms=int((time.perf_counter() - t0) * 1000),
                            request=request)
            raise
        audit.log_event(me, "shots.done", target=audit.target_of(pid), payload=payload,
                        method="POST", route="/api/projects/{id}/shots",
                        duration_ms=int((time.perf_counter() - t0) * 1000),
                        request=request)

    if not core.start_task(pid, "shots", job):
        raise HTTPException(409, "这个项目已有任务在跑")
    return {"ok": True, "count": max(1, min(10, body.count)), "duration": duration,
            "seg_len": seg_len, "material": "custom" if script else "script"}


@app.post("/api/projects/{pid}/shots-layout")
def do_shots_layout(pid: str, body: ShotsLayoutBody, request: Request):
    """按开关重排第 3 步的文档，**不调 AI、不花额度**。

    控制句（ctl）和画风只影响排版，不影响 AI 写出来的分镜内容。生成之后想改，
    走这里就够了——为一行排版再烧一次调用不划算。
    """
    auth.current_user(request)
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    if not (p.get("shots") or {}).get("episodes"):
        raise HTTPException(400, "还没有分镜产物，请先生成一次")
    try:
        return shots.shots_layout(pid, body.ctl, body.style)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, (str(exc) or type(exc).__name__)[:200])


@app.put("/api/projects/{pid}/plans-select")
def put_plans_select(pid: str, body: PlansSelectBody):
    """保存「哪些套要出片」的勾选状态。"""
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    plans = p.get("plans") or []
    if len(body.on) != len(plans):
        raise HTTPException(400, f"勾选数量({len(body.on)})和结构数量({len(plans)})不一致")
    for pl, on in zip(plans, body.on):
        pl["on"] = bool(on)
    core.save_merge(p, ["plans"])
    return {"ok": True, "selected": sum(1 for pl in plans if pl.get("on", True))}


class PlanHookBody(BaseModel):
    index: int      # 第几套（1 起）
    hook: str = ""  # 开场黑屏那一句，空 = 这套不出钩子卡


@app.put("/api/projects/{pid}/plan-hook")
def put_plan_hook(pid: str, body: PlanHookBody):
    """改某套的开场钩子文案（结构卡片里直接编辑）。

    AI 给的那句常常要人审一遍——剧名、人称、剧透都在这一句里出问题，
    为了改一句话重新调一次模型不值当。
    """
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    pl = next((x for x in (p.get("plans") or []) if x.get("index") == body.index), None)
    if not pl:
        raise HTTPException(404, f"第 {body.index} 套不存在")
    hook = " ".join((body.hook or "").split()).strip()[:60]
    pl["hook"] = hook
    core.save_merge(p, ["plans"])
    return {"ok": True, "index": body.index, "hook": hook}


class PlanFxBody(BaseModel):
    index: int = 0                 # 第几套（1 起）
    fx: dict | None = None         # 只放要覆盖的字段；null/空 = 恢复「跟随默认」


@app.put("/api/projects/{pid}/plan-fx")
def put_plan_fx(pid: str, body: PlanFxBody):
    """保存某一套自己的画面设置（调色/镜像/拉伸/特效/随机）。

    为什么要落库：用户调完第 3 套的调色、关掉页面再回来，设置得还在——
    只存在浏览器内存里等于每次都要重调一遍。
    传 null / {} 表示这套回到「跟随默认」，不是「全部关掉」。
    """
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    pl = next((x for x in (p.get("plans") or []) if x.get("index") == body.index), None)
    if not pl:
        raise HTTPException(404, f"第 {body.index} 套不存在")
    if not body.fx:
        pl.pop("fx", None)
        core.save_merge(p, ["plans"])
        return {"ok": True, "index": body.index, "fx": None}
    fx = _clean_fx(dict(body.fx))
    pl["fx"] = fx
    core.save_merge(p, ["plans"])
    return {"ok": True, "index": body.index, "fx": fx}


class FxDefaultBody(BaseModel):
    fx: dict = {}


@app.put("/api/projects/{pid}/fx-default")
def put_fx_default(pid: str, body: FxDefaultBody):
    """记住全局「出片增强 / 画面」的默认值：下次打开这个项目还按上次的设置。

    只存不校验业务含义（校验在出片时做）——这里存的是用户正在调的状态。
    """
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    # batch 容器里还有出片任务在写的进度/失败记录：重读磁盘最新，
    # 只改 fx_default 这一个子键，别用接口开头的旧快照整份盖回去。
    latest = core.load(pid) or p
    latest.setdefault("batch", {})["fx_default"] = dict(body.fx or {})
    core.save_merge(latest, ["batch"])
    return {"ok": True}


@app.get("/api/bgm/library")
def bgm_library():
    """自动配乐的曲库状态：只报数量，不暴露本机路径。"""
    return {"ok": True, **bgmlib.library_summary()}


class RenderFx(BaseModel):
    """出片增强开关 + 画面参数。

    缺省值（2026-11-05 晚按用户要求定稿）：看点字幕、钩子卡**默认关**；
    放大、缓冲加速默认开；BGM 默认关；调色默认「随机」档。
    前端面板、batch._flags、这里三处的缺省必须一致。

    「画面」这几项都是**只改画面、不改时长**的参数：切段那一趟顺手烧进去，
    拼接仍然是视频流 copy。所以每套可以各设一套，互不影响。
    """
    buffer_speed: bool = True   # 缓冲段（评分 5-6）加速 1.15×，爆点块原速
    hook_card: bool = False     # 片头 1.8 秒黑屏大字（默认关，用户要就单套开）
    hook_sec: float = 1.8       # 卡片时长，1-4 秒
    zoom: bool = True           # 画面放大 103% 再裁回，去重 + 切掉边缘一圈
    title: bool = False         # 每块画面顶部烧一行「看点：XXX」（默认关）
    bgm: bool = False           # 配乐 BGM：混入音乐，台词响时自动压低
    bgm_auto: bool = True       # 2026-11-12：默认自动配乐（曲库按 AI 的配乐建议选曲）
    bgm_pick: str = ""          # 可选：指定曲库曲目 id，空 = 按情绪自动挑
    bgm_path: str = ""          # 本地音乐路径；自动模式由曲库在出片时解析后临时写入
    bgm_vol: float = 0.22       # BGM 基础音量 0.05-0.6（闪避之外的基础垫底音量）
    # ── 调色 / 画面（2026-11-05）─────────────────────────────────────
    # grade 五个值："" 原片 / cold 冷调 / warm 暖调 / vivid 浓郁 / film 胶片 /
    #              rand 随机（每区从四档里换着抽，相邻区不重样）——
    #              随机并进档位（2026-11-05 晚用户定）：以前它是独立勾选框，
    #              跟四档同时开会互相覆盖，语义拧巴。
    grade: str = "rand"         # 默认随机：新项目一建好就有「每区换色」的去重保护
    grade_int: float = 0.65     # 强度 0-1（0 = 等于原片；随机时是每区浮动的基准）
    rand_min: float = 120       # 随机分区最短一区（秒）
    rand_max: float = 300       # 随机分区最长一区（秒）——分区长度本身也随机
    grade_rand: bool = False    # 兼容老数据：以前随机是独立勾选框，读进来合并成 grade=rand
    rand_sec: float = 180       # 兼容老数据：固定分区秒数
    lut: str = ""               # 自己的 .cube LUT 文件（给了就替代预设档）
    mirror: bool = False        # 左右镜像（去重强；想连字幕一起翻反，先选去字幕版素材）
    # ── 开头画面镜像（2026-11-12）：界面上单独一组，跟上面的整幅镜像不是一回事 ──
    # 只翻成片最前面约 0.3 秒，之后恢复原样；默认不勾，老项目读进来就是关。
    first_mirror: bool = False
    first_mirror_dir: str = "h"     # "h" 左右 / "v" 上下
    stretch: float = render.FX_DEFAULTS["stretch"]  # 横向变形 0.94-1.06（默认 1.5%，视觉无感）
    glow: float = render.FX_DEFAULTS["glow"]        # 光晕（亮部溢光）0-1
    grain: float = render.FX_DEFAULTS["grain"]      # 颗粒 0-1
    vignette: float = render.FX_DEFAULTS["vignette"]  # 暗角 0-1
    # ── 指纹强度（2026-11-12）：每套按这个档位抽自己的放大/旋转/取景偏移 ──
    # soft 保守 / std 标准（默认）/ strong 强力。不传就是标准，老项目行为不变。
    fp_level: str = render.FP_DEFAULT
    # ── 用哪份视频（界面上独立一组，不与上面四项混用）──
    # 去字幕 + 加自己的字幕已在「台词整理」之后那一步做成独立视频文件；
    # 出片这一步只挑素材，不再改画面。默认原始视频，老项目不受影响。
    src_ver: str = ""        # "" 原始视频 / "nosub" 已处理（去字幕+新字幕）的视频


def _clean_fx(fx: dict) -> dict:
    """画面参数夹到安全区间，顺带把 LUT 路径的合法性检查掉。

    为什么要在这里拦：LUT 路径错了 ffmpeg 是「切到第 1 段才报错」，
    用户看到的是整批失败；在接口层拦下能直接告诉他「这个文件不是 .cube / 不存在」。

    **只处理传进来的字段**（2026-11-05）：单套覆盖只带「和默认不一样」的几项，
    这里要是顺手补默认值（比如 stretch=1.0、grade_int=0.65），合并时就会把
    全局的调色/变形顶掉——用户明明只关了钩子卡，调色却回原片了。
    """
    if "grade_int" in fx:
        try:
            fx["grade_int"] = min(1.0, max(0.0, float(fx.get("grade_int") or 0.65)))
        except (TypeError, ValueError):
            fx["grade_int"] = 0.65
    if "grade" in fx:
        # grade_rand/rand_sec 是老版随机开关的字段：读进来合并成 grade=rand 后废弃
        if fx.get("grade_rand") and not fx.get("grade"):
            fx["grade"] = "rand"
        if fx.get("grade") == "rand" and "rand_min" not in fx and "rand_max" not in fx:
            # 只有老 rand_sec：换算成「min=max=老值」，分区长度退化为固定（保持原行为）
            try:
                legacy = float(fx.get("rand_sec") or 180)
                fx.setdefault("rand_min", legacy)
                fx.setdefault("rand_max", legacy)
            except (TypeError, ValueError):
                pass
        if fx.get("grade") not in ("", "cold", "warm", "vivid", "film", "rand"):
            fx["grade"] = ""
    for rk in ("rand_min", "rand_max"):
        if rk in fx:
            try:
                fx[rk] = min(900.0, max(30.0, float(fx.get(rk) or (120 if rk == "rand_min" else 300))))
            except (TypeError, ValueError):
                fx[rk] = 120.0 if rk == "rand_min" else 300.0
    if "rand_min" in fx and "rand_max" in fx and fx["rand_max"] < fx["rand_min"]:
        fx["rand_min"], fx["rand_max"] = fx["rand_max"], fx["rand_min"]
    if "rand_sec" in fx:
        try:
            fx["rand_sec"] = min(900.0, max(30.0, float(fx.get("rand_sec") or 180)))
        except (TypeError, ValueError):
            fx["rand_sec"] = 180.0
    if "stretch" in fx:
        try:
            fx["stretch"] = min(1.06, max(0.94, float(fx.get("stretch") or render.FX_DEFAULTS["stretch"])))
        except (TypeError, ValueError):
            fx["stretch"] = render.FX_DEFAULTS["stretch"]
    for k in ("glow", "grain", "vignette"):
        if k not in fx:
            continue
        try:
            fx[k] = min(1.0, max(0.0, float(fx.get(k) or render.FX_DEFAULTS[k])))
        except (TypeError, ValueError):
            fx[k] = render.FX_DEFAULTS[k]
    if "grade" in fx and (fx.get("grade") or "") not in ("", "cold", "warm", "vivid", "film"):
        fx["grade"] = ""
    if "lut" in fx:
        lut = (fx.get("lut") or "").strip()
        if lut:
            if not os.path.isfile(lut):
                raise HTTPException(400, f"LUT 文件不存在：{lut}")
            if not lut.lower().endswith(".cube"):
                raise HTTPException(400, "LUT 只支持 .cube 格式（剪映/达芬奇的调色包导出成 .cube）")
        fx["lut"] = lut
    # 开头画面镜像（独立一组）：同样只夹传进来的字段，方向收敛到 h / v 两个值，
    # 免得前端传个怪字符串下来让 ffmpeg 滤镜链拼错。
    if "first_mirror" in fx:
        fx["first_mirror"] = bool(fx.get("first_mirror"))
    if "first_mirror_dir" in fx:
        d = str(fx.get("first_mirror_dir") or "h").strip().lower()
        fx["first_mirror_dir"] = d if d in ("h", "v") else "h"
    # 用哪份视频（独立一组）：同样只夹传进来的字段，绝不顺手补默认值。
    if "src_ver" in fx:
        v = str(fx.get("src_ver") or "").strip()
        fx["src_ver"] = "nosub" if v == "nosub" else ""
    # 指纹强度：只认三个值，认不出就落回标准档（和老项目行为一致）
    if "fp_level" in fx:
        lv = str(fx.get("fp_level") or "").strip().lower()
        fx["fp_level"] = lv if lv in render.FP_LEVELS else render.FP_DEFAULT
    return fx


class RenderPlansBody(BaseModel):
    save_dir: str = ""   # 用户指定的成片保存目录，空则沿用上次/全局设置
    fx: RenderFx | None = None
    # 每套单独的画面设置：key 是套号（"1"/"2"…），值只包含要覆盖的字段。
    # 不传的套就用 fx 里的全局值——「默认都一样，想改单套就改单套」。
    sets_fx: dict[str, dict] | None = None


def _wants_live(plans: list[dict], fx: dict, sets_fx: dict) -> bool:
    """这次出片有没有套选了「出片时现场做」（src_ver=live）。

    单套覆盖优先于全局：设了 `src_ver` 就按它，没设才看全局。
    """
    for pl in plans:
        ov = (sets_fx or {}).get(str(pl.get("index")))
        if ov is None:
            ov = (sets_fx or {}).get(pl.get("index")) or {}
        v = (ov or {}).get("src_ver")
        if v is None:
            v = (fx or {}).get("src_ver")
        if str(v or "").strip() == "live":
            return True
    return False


def _missing_nosub(p: dict, plans: list[dict], fx: dict, sets_fx: dict) -> list[str]:
    """这次出片里选了「去掉字幕的视频」的套，用到的集有没有都生成好。

    按每套实际生效的设置算（单套覆盖优先于全局），只查 EDL 真会切到的集，
    免得某一集没生成就把整批全拦下。缺文件当场说清是哪几集，别等切到
    第 15 段才由渲染层抛错。"""
    need: set[int] = set()
    for pl in plans:
        ov = (sets_fx or {}).get(str(pl.get("index"))) or {}
        ver = str(ov.get("src_ver", fx.get("src_ver", "")) or "").strip()
        if ver != "nosub":
            continue
        for seg in pl.get("edl") or []:
            need.add(int(seg.get("ep") or 0))
    if not need:
        return []
    files = (p.get("nosub") or {}).get("files") or {}
    by_index = {int(e.get("index") or 0): str(e.get("file") or "")
                for e in p.get("episodes") or []}
    dest = nosub.out_dir(p["id"])
    return [str(i) for i in sorted(need)
            if not files.get(by_index.get(i, ""))
            or not os.path.isfile(os.path.join(dest, files[by_index.get(i, "")]))]


def _nosub_busy_conflict(pid: str, plans: list[dict], fx: dict, sets_fx: dict) -> str:
    """第 2 步正在生成时，这次出片能不能跟它同时跑。

    用**原始视频**出片完全不碰第 2 步的产物，直接放行（这就是拆组的目的）。
    选了「已处理版」(nosub) 要读它正在写的视频文件，「出片时现场做」(live)
    要走字幕检测状态，两者都可能读到半截文件/半截状态，必须等它跑完。
    """
    if not core.task_running(pid, "nosub"):
        return ""
    if not _wants_live(plans, fx, sets_fx):
        uses_nosub = any(
            str(((sets_fx or {}).get(str(pl.get("index"))) or {}).get(
                "src_ver", fx.get("src_ver", "")) or "").strip() == "nosub"
            for pl in plans)
        if not uses_nosub:
            return ""
    return ("第 2 步还在生成处理好的视频，这次出片要用的素材还没做完。"
            "等它跑完再出片，或者把「用哪份视频」改成原始视频——用原片可以和第 2 步同时跑。")


def _prepared_mirror_conflict(p: dict, plans: list[dict], fx: dict, sets_fx: dict) -> str:
    """用「已处理（去字幕+新字幕）」素材时不能左右镜像（2026-11-11）。

    新字幕已经烧进那一份素材的画面里，出片再 hflip 会把字翻成反字。
    只有副本生成时真的开了「加自己的字幕」（nosub.cfg.newsub）才拦；
    只去字幕、没加字幕的副本，镜像没问题。

    2026-11-12 补：「出片时现场做」（src_ver=live）**不受这条限制**——
    它的顺序是抹字幕 → hflip → 烧新字幕，字是翻转之后才画的，翻不到。
    所以下面只拦 nosub，live 直接放行。
    """
    ns = (p.get("nosub") or {}).get("cfg") or {}
    if not bool(ns.get("newsub")):
        return ""
    for pl in plans:
        ov = (sets_fx or {}).get(str(pl.get("index"))) or {}
        ver = str(ov.get("src_ver", fx.get("src_ver", "")) or "").strip()
        if ver != "nosub":
            continue
        if bool(ov.get("mirror", fx.get("mirror", False))):
            return (f"第 {pl.get('index')} 套用的是「已处理（去字幕+新字幕）」的素材，"
                    "自己的字幕已经烧进画面，再左右镜像会把字翻反。"
                    "这一套关掉镜像，或者改回原始视频。")
    return ""


@app.post("/api/projects/{pid}/render-plans")
def do_render_plans(pid: str, body: RenderPlansBody | None = None):
    """出片：只渲染勾选的套，成片存到用户指定的目录。"""
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    if not [pl for pl in (p.get("plans") or []) if pl.get("on", True)]:
        raise HTTPException(400, "没有勾选任何结构")
    save_dir = ((body.save_dir if body else "") or "").strip()
    if save_dir:
        # 提前校验：路径不合法就当场告诉用户，别等切了 20 段才报错
        try:
            os.makedirs(save_dir, exist_ok=True)
            probe = os.path.join(save_dir, ".hs_write_test")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe)
        except OSError as exc:
            raise HTTPException(400, f"这个保存位置用不了：{exc}") from exc
    fx = (body.fx.model_dump() if body and body.fx else {})
    # 兼容老页面/老项目：没带 bgm_auto 却勾了 BGM 并给了文件路径 = 手动模式，
    # 别让默认值 True 把用户已经选好的歌悄悄吞掉。
    if body and body.fx and "bgm_auto" not in getattr(
            body.fx, "model_fields_set", getattr(body.fx, "__fields_set__", set())):
        if (fx.get("bgm_path") or "").strip():
            fx["bgm_auto"] = False
    chosen = [pl for pl in (p.get("plans") or []) if pl.get("on", True)]
    # BGM（2026-11-12）：勾了就一定要拿到一个能播的本地音频文件。自动配乐由曲库
    # 按 AI 的「BGM 建议」选曲（模型不给 URL、不写歌名），逐套解析见下面 run_sets_fx；
    # 手动模式沿用原来的文件校验。两条路都在接口层当场验完，别等切完 20 段才炸。
    if fx.get("bgm"):
        try:
            fx["bgm_vol"] = min(0.6, max(0.05, float(fx.get("bgm_vol") or 0.22)))
        except (TypeError, ValueError):
            fx["bgm_vol"] = 0.22
        if fx.get("bgm_auto") is False:
            bp = (fx.get("bgm_path") or "").strip()
            if not bp:
                raise HTTPException(400, "选了「用我自己的音乐」，但还没选音乐文件")
            if not os.path.isfile(bp):
                raise HTTPException(400, f"BGM 音乐文件不存在：{bp}")
            fx["bgm_path"] = bp
        else:
            fx["bgm_path"] = ""   # 逐套解析后写进 run_sets_fx，不落库
    else:
        fx["bgm_path"] = ""
    fx = _clean_fx(fx)
    # 每套的画面覆盖：只留合法字段，顺手校验 LUT（每套各可能带自己的 LUT）
    sets_fx: dict[str, dict] = {}
    for k, v in ((body.sets_fx or {}) if body else {}).items():
        if not isinstance(v, dict):
            continue
        try:
            idx = str(int(k))
        except (TypeError, ValueError):
            continue
        sets_fx[idx] = _clean_fx(dict(v))
        ov = sets_fx[idx]
        if ov.get("bgm"):
            try:
                ov["bgm_vol"] = min(0.6, max(0.05, float(ov.get("bgm_vol") or 0.22)))
            except (TypeError, ValueError):
                ov["bgm_vol"] = 0.22
    # 存进项目：下次打开页面还按这套设置显示，不用每次重新调。
    # 重读最新再改 plans[].fx——接口开头的 p 是旧快照，整份保存会擦掉
    # 并行产线刚写好的分镜/去字幕结果。
    if sets_fx:
        latest = core.load(pid) or p
        for idx, cfg in sets_fx.items():
            for pl in (latest.get("plans") or []):
                if str(pl.get("index")) == idx:
                    pl["fx"] = cfg
        core.save_merge(latest, ["plans"])

    # 自动配乐：逐套按自己的 AI 配乐建议到曲库选曲。解析出的本地路径只放进
    # run_sets_fx 传给 batch，绝不写回项目——曲库缓存路径不该固化进项目文件。
    run_sets_fx = {k: dict(v) for k, v in sets_fx.items()}
    used_bgm: set[str] = set()
    if fx.get("bgm") and fx.get("bgm_auto") is not False:
        for pl in chosen:
            idx = str(pl.get("index"))
            ov = run_sets_fx.setdefault(idx, {})
            if ov.get("bgm", True) is False:
                continue                      # 这一套单独关了 BGM
            if ov.get("bgm_auto", True) is False:
                bp = (ov.get("bgm_path") or fx.get("bgm_path") or "").strip()
                if not bp or not os.path.isfile(bp):
                    raise HTTPException(400, f"第 {idx} 套选了「用我自己的音乐」，但还没选可用的音乐文件")
                ov["bgm_path"] = bp
                continue
            try:
                got = bgmlib.resolve(
                    str(pl.get("bgm") or ""), f"{pid}:{idx}", used_bgm,
                    pick_id=str(ov.get("bgm_pick") or fx.get("bgm_pick") or "").strip())
            except bgmlib.BgmError as exc:
                raise HTTPException(400, str(exc)) from exc
            used_bgm.add(got["id"])
            ov["bgm_path"] = got["path"]
    busy_ns = _nosub_busy_conflict(pid, chosen, fx, sets_fx)
    if busy_ns:
        raise HTTPException(409, busy_ns)
    conflict = _prepared_mirror_conflict(p, chosen, fx, sets_fx)
    if conflict:
        raise HTTPException(400, conflict)
    miss = _missing_nosub(p, chosen, fx, sets_fx)
    if miss:
        raise HTTPException(400, f"第 {','.join(miss)} 集还没生成「去掉字幕」的视频，"
                                 "先到「台词整理」后面那一步生成完，再回来出片")
    # 现场做（src_ver=live）要按台词烧字幕：没整理过台词当场说清楚，
    # 别等后台任务跑到切第一段才失败
    if _wants_live(chosen, fx, sets_fx) and not any(
            (e.get("lines") or []) for e in (p.get("episodes") or [])):
        raise HTTPException(400, "还没有整理台词。「出片时现场做」要按台词重做字幕，"
                                 "先去第 1 步「台词整理」做完")
    if not core.start_task(pid, "batch",
                          lambda: batch.render_plans(pid, save_dir or None, fx, run_sets_fx)):
        raise HTTPException(409, "这个项目已有任务在跑")
    if save_dir:
        root = os.path.abspath(os.path.expanduser(save_dir))
    else:
        root = ((p.get("batch") or {}).get("save_dir") or "").strip()
        root = os.path.abspath(os.path.expanduser(root)) if root else core.output_dir()
    out_dir = os.path.join(root, core.safe_name(p.get("name") or ""))
    return {"ok": True, "save_dir": root, "out_dir": out_dir}


@app.get("/api/projects/{pid}/transcript")
def transcript(pid: str, ep: int = 0, download: int = 0):
    """第一步的产物：全部（或单集）台词全文，浏览器直接打开/下载。"""
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    out = []
    for e in p["episodes"]:
        if ep and e["index"] != ep:
            continue
        ls = e.get("lines") or []
        if not ls:
            continue
        out.append(f"【第{e['index']:02d}集】{e['file']}（{e['duration']:.0f} 秒，{len(ls)} 句）")
        for i, ln in enumerate(ls, 1):
            # 和编辑器同一套无损格式：下载后再粘回来，时间码不会掉精度
            out.append(f"{i}. ({float(ln['start']):g}-{float(ln['end']):g}) {ln['text']}")
        out.append("")
    text = "\n".join(out).strip() or "还没有转写文本"
    headers = {}
    if download:
        name = p["name"] + ("_台词" if not ep else f"_第{ep:02d}集台词") + ".txt"
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(name)}"
    return Response(text, media_type="text/plain; charset=utf-8", headers=headers)


@app.get("/api/projects/{pid}/lines")
def get_lines(pid: str):
    """第 1 步台词编辑器：一次取回每集的可编辑文本（和「下载台词 .txt」同格式）。"""
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    # :g 保留转写原始精度（Whisper 常给 0.68 / 1.24 这类两位小数）。
    # 用 :.1f 会把「打开编辑器 → 原样保存」变成一次静默的精度截断，
    # 剪辑定位会偏，所以这里和下载全文必须用同一套无损格式。
    out = []
    for e in p.get("episodes") or []:
        ls = e.get("lines") or []
        body = "\n".join(
            f"{i}. ({float(ln.get('start') or 0):g}-{float(ln.get('end') or 0):g}) "
            f"{ln.get('text') or ''}"
            for i, ln in enumerate(ls, 1))
        out.append({
            "index": int(e.get("index") or 0), "file": e.get("file") or "",
            "duration": round(float(e.get("duration") or 0), 1),
            "status": e.get("status") or "", "count": len(ls), "text": body,
        })
    return {"name": p.get("name") or "", "episodes": out,
            "line_total": sum(x["count"] for x in out)}


@app.put("/api/projects/{pid}/lines")
def save_lines(pid: str, body: EditLines, request: Request):
    """第 1 步手改某一集的台词：校正错字、补漏句、改时间码，只覆盖这一集。

    走和「粘贴台词」同一个解析器，所以改完的格式、排序、时间码校验完全一致；
    但严格拒绝跨集内容，避免一次误粘贴把别人的集号写进来。
    """
    me = auth.current_user(request)
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    # 后台还在自动转写时不要手改：两边都 core.save 会互相覆盖，改完像没保存
    task = core.get_task(pid)
    if task and task.get("kind") == "transcribe":
        raise HTTPException(409, "正在自动整理台词，等这一步跑完再改")
    ep_i = int(body.ep or 0)
    by = {int(e.get("index") or 0): e for e in (p.get("episodes") or [])}
    if ep_i not in by:
        raise HTTPException(400, "项目里没有这一集")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "这一集不能整集清空，至少留一句台词")
    parsed = _parse_transcript(text, default_ep=ep_i)
    other = [k for k in parsed if k != ep_i]
    if other:
        raise HTTPException(400, "这段里混进了别的集号，请切到那一集单独改")
    lines = parsed.get(ep_i) or []
    if not lines:
        raise HTTPException(400, "没认出带时间码的台词，检查一下每行的 (起-止) 时间")
    ep = by[ep_i]
    max_end = max(float(ln["end"]) for ln in lines)
    dur = float(ep.get("duration") or 0)
    if dur and max_end > dur + 1:
        raise HTTPException(400, f"有台词的时间超过了这一集（{dur:.0f} 秒），改一下时间码")
    ep["lines"] = lines
    ep["status"] = "done"
    ep.pop("error", None)
    p["stage"] = "transcribed"
    core.save_merge(p, ["episodes", "stage"])
    total = sum(len(e.get("lines") or []) for e in p["episodes"])
    audit.log_event(me, "project.lines_edit", target=audit.target_of(pid),
                    payload={"ep": ep_i, "lines": len(lines), "line_total": total},
                    request=request, status_code=200)
    return {"ok": True, "ep": ep_i, "line_count": len(lines), "line_total": total}


@app.get("/api/projects/{pid}/video")
def video(pid: str, set: int = 0):
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    if set:
        # 只认当前结构版本的成片；重生成结构后旧记录指向的是上一版片子
        gen = int((p.get("batch") or {}).get("gen") or p.get("plan_gen") or 0)
        s = next((s for s in (p.get("sets") or []) if s.get("index") == set
                  and int(s.get("gen") or 0) == gen), None)
        name = (s or {}).get("output")
        if not name:
            raise HTTPException(404, f"第 {set} 套还没有成片")
    else:
        name = p.get("output")
        if not name:
            raise HTTPException(404, "还没有成片")
    path = core.abs_output(name)
    if not os.path.isfile(path):
        raise HTTPException(404, "文件已丢失")
    return FileResponse(path, media_type="video/mp4", filename=os.path.basename(path))


@app.post("/api/projects/{pid}/open-video")
def open_video(pid: str, set: int = 0):
    """用系统默认播放器打开成片——本机工具就该这样，不该在浏览器里开个页面。"""
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    if set:
        # 只认当前结构版本的成片；重生成结构后旧记录指向的是上一版片子
        gen = int((p.get("batch") or {}).get("gen") or p.get("plan_gen") or 0)
        s = next((s for s in (p.get("sets") or []) if s.get("index") == set
                  and int(s.get("gen") or 0) == gen), None)
        name = (s or {}).get("output")
        if not name:
            raise HTTPException(404, f"第 {set} 套还没有成片")
    else:
        name = p.get("output")
        if not name:
            raise HTTPException(404, "还没有成片")
    path = core.abs_output(name)
    if not os.path.isfile(path):
        raise HTTPException(404, "文件已丢失，可能被移动或删除了")
    try:
        if os.name == "nt":
            os.startfile(path)  # noqa: S606 —— 本机工具，打开自己的成片
        else:
            import subprocess

            subprocess.Popen(["xdg-open", path])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"打不开文件：{exc}") from exc
    return {"ok": True, "path": path}


@app.post("/api/pick-folder")
def pick_folder(initial: str = ""):
    """弹系统原生的「选择文件夹」对话框（本机工具才有的能力）。
    浏览器拿不到真实磁盘路径，所以由后端弹框、把选中的路径回给前端。"""
    import threading

    from . import nativedlg

    result = {"path": ""}
    err: dict = {}

    def _pick():
        try:
            init = initial if initial and os.path.isdir(initial) else ""
            result["path"] = nativedlg.pick_folder("选择成片保存位置", init)
        except Exception as exc:  # noqa: BLE001
            err["msg"] = str(exc)

    t = threading.Thread(target=_pick, daemon=True)
    t.start()
    t.join(timeout=300)
    if err:
        raise HTTPException(500, f"弹不出选择框（{err['msg']}），可以直接手动输入路径")
    if not result["path"]:
        return {"ok": False, "path": ""}
    return {"ok": True, "path": result["path"].replace("\\", "/")}


@app.post("/api/pick-file")
def pick_file(initial: str = ""):
    """弹系统原生的「选择文件」对话框（选 BGM 音乐文件用），机制同 pick-folder。"""
    import threading

    from . import nativedlg

    result = {"path": ""}
    err: dict = {}

    def _pick():
        try:
            init = (initial if initial and (os.path.isdir(initial) or os.path.isfile(initial))
                    else "")
            result["path"] = nativedlg.pick_file(
                "选择 BGM 音乐文件", init,
                filetypes=[("音频文件", "*.mp3 *.m4a *.aac *.wav *.flac *.ogg *.wma"),
                           ("所有文件", "*.*")])
        except Exception as exc:  # noqa: BLE001
            err["msg"] = str(exc)

    t = threading.Thread(target=_pick, daemon=True)
    t.start()
    t.join(timeout=300)
    if err:
        raise HTTPException(500, f"弹不出选择框（{err['msg']}），可以直接手动输入路径")
    if not result["path"]:
        return {"ok": False, "path": ""}
    return {"ok": True, "path": result["path"].replace("\\", "/")}


class MergeBody(BaseModel):
    """第 4 步「两两合并」：A、B 两个文件夹 + 可选的输出目录。"""
    a: str
    b: str
    out: str = ""


@app.post("/api/merge/preview")
def merge_preview(body: MergeBody):
    """开跑前的确认单：两边各有哪些视频、共组合出多少条。"""
    if not body.a or not os.path.isdir(body.a):
        raise HTTPException(400, f"第一个文件夹不存在：{body.a}")
    if not body.b or not os.path.isdir(body.b):
        raise HTTPException(400, f"第二个文件夹不存在：{body.b}")
    return vmerge.preview(body.a, body.b)


@app.post("/api/merge/start")
def merge_start(body: MergeBody):
    """启动两两合并。纯本机渲染、不花 AI 额度，**不埋点**（2026-11-05 用户定）。"""
    r = vmerge.start(body.a or "", body.b or "", body.out or "")
    if not r.get("ok"):
        raise HTTPException(409, r.get("reason") or "启动失败")
    return r


@app.get("/api/merge/status")
def merge_status():
    """合并任务进度（独立于项目的任务系统，前端单独轮询这一个口）。"""
    return vmerge.status()


@app.post("/api/merge/cancel")
def merge_cancel():
    vmerge.cancel()
    return {"ok": True}


@app.post("/api/merge/open")
def merge_open():
    """在资源管理器里打开合并输出目录。"""
    if not vmerge.open_out():
        raise HTTPException(400, "还没有输出目录（先跑一次合并）")
    return {"ok": True}


@app.post("/api/projects/{pid}/open-folder")
def open_folder(pid: str):
    """在资源管理器里打开这个项目的成片目录（<保存位置>/<项目名>）。"""
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    target = core.batch_out_dir(p)
    os.makedirs(target, exist_ok=True)
    try:
        if os.name == "nt":
            os.startfile(target)  # noqa: S606 —— 本机工具，打开自己的成片目录
        else:
            import subprocess

            subprocess.Popen(["xdg-open", target])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"打不开文件夹：{exc}") from exc
    return {"ok": True, "path": target}
