"""钩子工坊：本机跑的小工具，导入剧集 → 转写 → AI 出结构 → 出片。"""
from __future__ import annotations

import os
import threading

import requests

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from urllib.parse import quote

from . import analyze, batch, core, render, transcribe

APP_VERSION = "0.1.0"
WEB = core.WEB

app = FastAPI(title="钩子工坊")
app.mount("/static", StaticFiles(directory=WEB), name="static")


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
        return False  # 组装结构是秒级操作，中断了重跑一次即可
    if kind == "pool":
        return not (p.get("pool") or {}).get("segments")
    return False


@app.on_event("startup")
def resume_interrupted():
    """开机自愈：先做路径/Key 迁移，再把上次中断的任务接着跑，不用人再点。"""
    # 后台预热模型（约 10 秒）：不阻塞启动，用户打开项目就能直接转写，不用先干等加载
    threading.Thread(target=transcribe.warmup, daemon=True).start()
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
            elif kind == "pool":
                gear = p.get("pool_gear") or analyze.DEFAULT_GEAR
                core.start_task(pid, "pool", lambda pid=pid, g=gear: batch.build_pool_run(pid, g))
            else:
                fn = {"transcribe": transcribe.run, "analyze": analyze.run,
                      "render": render.run}[kind]
                core.start_task(pid, kind, lambda fn=fn, pid=pid: fn(pid))
        else:
            # 标记还在但其实已经做完（比如死在最后一步保存前），清掉即可
            core.clear_task_flag(pid)


class NewProject(BaseModel):
    path: str
    name: str = ""
    target_minutes: int = 10
    mode: str = "funnel"  # funnel=单片导流 / batch=批量混剪
    gear: str = "normal"  # 批量混剪：片段池挖掘挡位


class ConfigBody(BaseModel):
    llm_key: str | None = None
    llm_base: str | None = None
    llm_model: str | None = None
    whisper_tier: str | None = None
    output_dir: str | None = None


class EdlBody(BaseModel):
    text: str


class BatchBody(BaseModel):
    count: int = 3
    min_minutes: float = 5.0
    max_minutes: float = 10.0


@app.get("/")
def index():
    return FileResponse(os.path.join(WEB, "index.html"))


@app.get("/api/status")
def status():
    cfg = core.llm()
    tier = core.whisper_tier()
    mdir = core.model_dir(tier)
    return {
        "llm_key": bool(cfg["key"]),
        "model": cfg["model"],
        "whisper_tier": tier,
        "whisper_model": os.path.isfile(os.path.join(mdir, "model.bin")),
        "whisper_dir": mdir,
        "ffmpeg": os.path.isfile(core.ff("ffmpeg")),
        "base": core.BASE,
        "data": core.DATA_DIR,
        "version": APP_VERSION,
    }


@app.get("/api/config")
def get_config():
    """设置页：Key 只回显掩码；模型固定 small，只报可用性。"""
    cfg = core.llm()
    key = cfg["key"]
    info = core.sysinfo()
    tiers = [{
        "key": core.TIER,
        "name": "small · 精准",
        "note": "中文准确率高，随安装包提供，无需选择",
        "available": bool(info["models"].get(core.TIER)),
        "recommended": True,
    }]
    out_dir = core.output_dir()
    # 体验额度：没填 Key 时才真正调用代理；填了 Key 也查一下，让用户知道还有多少次
    trial = {"url": (cfg.get("proxy_url") or core.PROXY_URL_DEFAULT), "quota": None}
    if not key:
        try:
            r = requests.get(f"{trial['url'].rstrip('/')}/api/quota",
                             headers={"X-HS-Token": core.device_token()}, timeout=5)
            if r.status_code == 200:
                trial["quota"] = r.json()
        except Exception:
            pass
    return {
        "llm_key_set": bool(key),
        "llm_key_hint": (key[:6] + "…" + key[-4:]) if len(key) > 12 else ("已设置" if key else ""),
        "llm_base": cfg["base"],
        "llm_model": cfg["model"],
        "trial": trial,
        "whisper_tier": core.whisper_tier(),
        "tiers": tiers,
        "system": {"cpu": info["cpu"], "mem_gb": info["mem_gb"]},
        "paths": {"base": core.BASE, "data": core.DATA_DIR,
                  "models": core.MODELS, "prompts": core.PROMPTS,
                  "output": out_dir, "output_free_gb": core.free_gb(out_dir),
                  "data_free_gb": core.free_gb(core.DATA_DIR)},
        "version": APP_VERSION,
    }


@app.put("/api/config")
def put_config(body: ConfigBody):
    patch = {}
    if body.llm_key is not None:
        patch["llm_key"] = body.llm_key.strip()
    if body.llm_base:
        patch["llm_base"] = body.llm_base.strip()
    if body.llm_model:
        patch["llm_model"] = body.llm_model.strip()
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
    cfg = core.save_config(patch)
    return {"ok": True, "llm_key_set": bool(cfg.get("llm_key")),
            "whisper_tier": core.whisper_tier(),
            "output_dir": core.output_dir(),
            "output_free_gb": core.free_gb(core.output_dir())}


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


@app.post("/api/projects")
def create(body: NewProject):
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
    proj = core.create(path, name, max(1, body.target_minutes), episodes, body.mode)
    # 建完项目立刻自动转写：不用用户点「开始转写」
    # 批量混剪：转完再自动按所选挡位挖片段池；单片导流：转完等用户点「让 AI 出表」
    if proj.get("mode") == "batch":
        gear = body.gear if body.gear in analyze.POOL_GEARS else analyze.DEFAULT_GEAR
        proj["pool_gear"] = gear
        core.save(proj)
        core.start_task(proj["id"], "transcribe", lambda pid=proj["id"], g=gear: batch.auto_run(pid, g))
    else:
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
    slim["pool_text"] = analyze.pool_text(p["pool"]) if p.get("pool") else ""
    if slim.get("pool") and (p["pool"].get("segments") or []) and not p["pool"].get("gear"):
        # 旧池子没有挡位标记，按「标准」对待（原来的默认提示词就是标准档）
        slim["pool"]["gear"] = "normal"
        slim["pool"]["gear_name"] = "标准"
    slim["task"] = core.get_task(pid)
    return slim


@app.delete("/api/projects/{pid}")
def delete(pid: str):
    d = os.path.join(core.PROJECTS, pid)
    if os.path.isdir(d):
        core.rmtree(d)
    return {"ok": True}


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
    if core.task_running(pid):
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
        core.save(latest)
        raise HTTPException(500, f"{type(exc).__name__}: {exc}")
    # 基于磁盘最新状态合并保存，避免覆盖任务期间其它线程写入的数据
    latest = core.load(pid) or p
    for e in latest["episodes"]:
        if e["index"] == ep["index"]:
            e["lines"] = lines
            e["status"] = "done"
            e["error"] = ""
            break
    core.save(latest)
    return {"index": ep["index"], "lines": len(lines),
            "seconds": round(_time.time() - t0, 1)}


@app.post("/api/projects/{pid}/analyze")
def do_analyze(pid: str):
    if not core.load(pid):
        raise HTTPException(404, "项目不存在")
    if not core.start_task(pid, "analyze", lambda: analyze.run(pid)):
        raise HTTPException(409, "这个项目已有任务在跑")
    return {"ok": True}


class PoolSelectBody(BaseModel):
    on: list[bool]


@app.put("/api/projects/{pid}/pool-select")
def put_pool_select(pid: str, body: PoolSelectBody):
    """保存片段池的勾选状态（与 pool.segments 按下标一一对应）。"""
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    segs = (p.get("pool") or {}).get("segments") or []
    if len(body.on) != len(segs):
        raise HTTPException(400, f"勾选数量({len(body.on)})和片段数量({len(segs)})不一致")
    for s, on in zip(segs, body.on):
        s["on"] = bool(on)
    core.save(p)
    return {"ok": True, "selected": sum(1 for s in segs if s.get("on", True))}


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
    core.save(p)
    return {"ok": True, "segments": len(edl),
            "total": round(sum(s["end"] - s["start"] for s in edl), 1)}


@app.post("/api/projects/{pid}/render")
def do_render(pid: str):
    if not core.load(pid):
        raise HTTPException(404, "项目不存在")
    if not core.start_task(pid, "render", lambda: render.run(pid)):
        raise HTTPException(409, "这个项目已有任务在跑")
    return {"ok": True}


class PoolReq(BaseModel):
    gear: str = "normal"   # 挖掘挡位：strict / normal / loose / max
    loose: bool = False    # 兼容旧调用，等价于 gear=loose


@app.get("/api/pool-gears")
def pool_gears():
    """片段池挖掘挡位清单（前端下拉用）。"""
    return analyze.gear_list()


@app.post("/api/projects/{pid}/pool")
def do_pool(pid: str, body: PoolReq | None = None):
    """批量模式第 2 步：AI 挖全剧精彩片段池（只调一次 API）。gear 选挖掘挡位。"""
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    if not core.llm()["key"]:
        raise HTTPException(400, "没有配置 API Key，请先在「设置」里填写")
    gear = "loose" if (body and body.loose) else ((body.gear if body else "normal") or "normal")
    if gear not in analyze.POOL_GEARS:
        raise HTTPException(400, f"未知挡位：{gear}")
    if not core.start_task(pid, "pool", lambda: batch.build_pool_run(pid, gear)):
        raise HTTPException(409, "这个项目已有任务在跑")
    return {"ok": True, "gear": gear}


@app.put("/api/projects/{pid}/pool")
def put_pool(pid: str, body: EdlBody):
    """保存编辑后的片段池（删掉不要的行 = 剔除该片段）。"""
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    segs = analyze.parse_pool_text(body.text)
    if not segs:
        raise HTTPException(400, "片段池里没有可用的行")
    # 越界的行夹回该集时长
    durs = {ep["index"]: ep["duration"] for ep in p["episodes"]}
    for s in segs:
        cap = durs.get(s["ep"], 0)
        if not cap:
            continue
        s["start"] = max(0.0, min(s["start"], cap - 1))
        s["end"] = max(s["start"] + 1, min(s["end"], cap))
    old = p.get("pool") or {}
    p["pool"] = {"summary": old.get("summary", ""), "segments": segs}
    core.save(p)
    return {"ok": True, "segments": len(segs),
            "total": round(sum(s["end"] - s["start"] for s in segs), 1)}


class PlanBody(BaseModel):
    count: int = 3
    min_minutes: float = 3.0
    max_minutes: float = 7.0
    order: str = "auto"   # auto=每套轮换叙事顺序 / chrono=全部严格集序 / varied=全部变序开场


class PlansSelectBody(BaseModel):
    on: list[bool]


@app.post("/api/projects/{pid}/plan")
def do_plan(pid: str, body: PlanBody):
    """第 3 步：本机组装 N 套剪辑结构（不调 API、不渲染），供用户挑选。"""
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    if not ((p.get("pool") or {}).get("segments")):
        raise HTTPException(400, "还没有片段池，请先挖片段池")
    count = max(1, min(200, int(body.count)))
    mn = max(0.5, float(body.min_minutes))
    mx = max(0.5, float(body.max_minutes))
    order = body.order if body.order in ("auto", "chrono", "varied") else "auto"
    if not core.start_task(pid, "plan", lambda: batch.plan_run(pid, count, mn, mx, order)):
        raise HTTPException(409, "这个项目已有任务在跑")
    return {"ok": True, "count": count, "order": order}


@app.post("/api/projects/{pid}/ai-plan")
def do_ai_plan(pid: str, body: PlanBody):
    """第 3 步（AI 版）：把全剧台词交给 AI，一次排出 N 套结构。

    跟 /plan 的区别：/plan 从片段池本机组装（免费、可无限重试）；
    这个走 1 次 API，AI 看得到完整剧情，能把这套排成讨债线、那套排成反击线。
    两者结果都落在 p["plans"] 上，后续出片流程完全一样。
    """
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    if not (p.get("episodes") or []):
        raise HTTPException(400, "还没有转写结果，请先转写")
    # AI 一次输出不了太多套（每套十几块，输出会过长易截断），上限压到 20
    count = max(1, min(20, int(body.count)))
    mn = max(0.5, float(body.min_minutes))
    mx = max(0.5, float(body.max_minutes))
    order = body.order if body.order in ("auto", "chrono", "varied") else "auto"
    if not core.start_task(pid, "plan", lambda: batch.ai_plan_run(pid, count, mn, mx, order)):
        raise HTTPException(409, "这个项目已有任务在跑")
    return {"ok": True, "count": count, "order": order}


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
    core.save(p)
    return {"ok": True, "selected": sum(1 for pl in plans if pl.get("on", True))}


class RenderPlansBody(BaseModel):
    save_dir: str = ""   # 用户指定的成片保存目录，空则沿用上次/全局设置


@app.post("/api/projects/{pid}/render-plans")
def do_render_plans(pid: str, body: RenderPlansBody | None = None):
    """第 4 步：只渲染勾选的套，成片存到用户指定的目录。"""
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
    if not core.start_task(pid, "batch", lambda: batch.render_plans(pid, save_dir or None)):
        raise HTTPException(409, "这个项目已有任务在跑")
    if save_dir:
        root = os.path.abspath(os.path.expanduser(save_dir))
    else:
        root = ((p.get("batch") or {}).get("save_dir") or "").strip()
        root = os.path.abspath(os.path.expanduser(root)) if root else core.output_dir()
    out_dir = os.path.join(root, core.safe_name(p.get("name") or ""))
    return {"ok": True, "save_dir": root, "out_dir": out_dir}


@app.post("/api/projects/{pid}/batch")
def do_batch(pid: str, body: BatchBody):
    """一步到位：先出结构再全部出片（保留兼容，界面上走 plan + render-plans）。"""
    p = core.load(pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    count = max(1, min(200, int(body.count)))
    mn = max(0.5, float(body.min_minutes))
    mx = max(0.5, float(body.max_minutes))

    def _job():
        batch.plan_run(pid, count, mn, mx)
        batch.render_plans(pid)

    if not core.start_task(pid, "plan", _job):
        raise HTTPException(409, "这个项目已有任务在跑")
    return {"ok": True, "count": count}


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
            out.append(f"{i}. ({ln['start']:.1f}-{ln['end']:.1f}) {ln['text']}")
        out.append("")
    text = "\n".join(out).strip() or "还没有转写文本"
    headers = {}
    if download:
        name = p["name"] + ("_台词" if not ep else f"_第{ep:02d}集台词") + ".txt"
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(name)}"
    return Response(text, media_type="text/plain; charset=utf-8", headers=headers)


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

    result = {"path": ""}
    err: dict = {}

    def _pick():
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            init = initial if initial and os.path.isdir(initial) else os.path.expanduser("~")
            path = filedialog.askdirectory(initialdir=init, title="选择成片保存位置",
                                           mustexist=False)
            root.destroy()
            result["path"] = path or ""
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
