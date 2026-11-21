"""一键全自动：转写 → 抹字幕 → 生成 N 套 → 出片，串成一条可重入的后台任务。

这是给普通用户用的正式功能（不是 agent 用的外挂脚本）：新建项目时勾「全自动」
或已有项目点「一键全自动」，配置一次，程序自己跑完整条链。

可靠性（2026-11-21 那次踩坑换来的硬规矩）：
- 完成判据只读本地完整 project.json，绝不读被裁剪的 API（/api/projects 不给 lines）。
- 每一步做之前先查产物够不够，够就跳过（幂等）——重跑/续跑不重复花钱。
- 生成套用 append+new_batch 批次隔离，出片用 only 限定本次套号，
  绝不碰用户已有的套和别的批次的成片。
- 断点存 project["autopipe"]，失败写 last_error 留现场；永远不关机。
"""
from __future__ import annotations

import os
import time

from . import core

# 全自动默认出片参数：默认随机选集 + 干净底片按集随机镜像；BGM 默认关（可选）。
# 用户在参数面板里改过就用改过的（存进 cfg.fx）。
DEFAULT_FX = {
    "auto_fx": True,
    "src_ver": "nosub",
    "mirror": True,
    "grade": "rand",
    "zoom": True,
    "buffer_speed": True,
    "voice_iso": True,
    "first_mirror": True,
    "first_mirror_dir": "v",
    "bgm": False,
    "bgm_mode": "folder",
}


def _check_cancel(pid: str) -> None:
    """本流程所有步骤都跑在 cuts 组，中止按 cuts 打标就停。

    nosub 那一组也查一下：抹字幕**独立**跑时在 nosub 组（第 2 步「重做字幕」），
    用户可能单独点了那一步的中止，标记留着就该一起停。
    """
    if core.is_cancelled(pid, "cuts") or core.is_cancelled(pid, "nosub"):
        raise core.TaskCancelled()


def _new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()


def _stage(pid: str, stage: str, **kw) -> None:
    """把阶段断点写回项目（save_merge 只覆盖 autopipe 字段，不碰别的产线）。"""
    p = core.load(pid)
    if not p:
        return
    ap = p.setdefault("autopipe", {})
    ap["stage"] = stage
    ap["updated"] = time.time()
    for k, v in kw.items():
        ap[k] = v
    core.save_merge(p, ["autopipe"])


def _nosub_ready(p: dict, n_eps: int) -> bool:
    """底片是否已经齐了：文件覆盖所有集且没有错误。"""
    ns = p.get("nosub") or {}
    files = ns.get("files") or {}
    errs = ns.get("errors") or []
    return bool(files) and len(files) >= n_eps and not errs


def run(pid: str, cfg: dict | None = None) -> None:
    """全自动主流程。cfg 不传就用项目里存的（重跑/续跑走这条）。"""
    from . import batch, nosub, transcribe

    p = core.load(pid)
    if not p:
        raise RuntimeError("项目不存在")
    ap = p.get("autopipe") or {}
    if ap.get("done"):
        core.set_task(pid, "autopipe", 100, "全自动已完成，不用重跑")
        return

    cfg = cfg or ap.get("cfg") or {}
    run_id = ap.get("run_id") or _new_run_id()
    if not ap or ap.get("stage") in (None, "", "init"):
        _stage(pid, "init", cfg=cfg, run_id=run_id, indexes=[],
               last_error="", done=False)

    try:
        # ① 台词整理（force=False 只补没台词的集，已整理的不重做）
        core.set_task(pid, "autopipe", 4, "全自动 ①/④ 正在整理台词…")
        eps = p.get("episodes") or []
        if not eps:
            raise RuntimeError("这个项目还没有导入视频")
        if any(not e.get("lines") for e in eps):
            transcribe.run(pid, force=False)
        _check_cancel(pid)
        p = core.load(pid)
        if any(not e.get("lines") for e in (p.get("episodes") or [])):
            raise RuntimeError("还有集没整理出台词，全自动先停在这里（可到「台词整理」看哪一集失败）")
        _stage(pid, "transcribe")

        # ② 干净底片（nosub.run 自带按指纹跳过已完成集；底片齐就直接跳过）
        core.set_task(pid, "autopipe", 32, "全自动 ②/④ 正在做干净底片…")
        if not _nosub_ready(p, len(eps)):
            # 内嵌调用：nosub.run 默认往 nosub 槽写进度、末尾还会 set_task(None)，
            # 那会把全自动自己的 cuts 进度撂在 32% 不动、跑完一格还被清空
            # （2026-11-21）。这里接管进度，把抹字幕的 0~100 映射到 ②/④ 的
            # 32%→54%，收尾仍由本函数负责。
            nosub.run(pid, on_progress=lambda pct, msg: core.set_task(
                pid, "autopipe", 32 + pct * 0.22,
                f"全自动 ②/④ 抹字幕 · {msg}"))
            _check_cancel(pid)
            p = core.load(pid)
        _stage(pid, "nosub")

        # ③ 生成 N 套（批次隔离：已有套就 append 新批，绝不覆盖；断点防止重复花钱）
        core.set_task(pid, "autopipe", 54, "全自动 ③/④ 正在生成剪辑结构…")
        jobs = cfg.get("jobs") or []
        if not jobs:
            raise RuntimeError("还没配置要生成的模型和套数，请到「全自动设置」里填")
        need = sum(max(1, int(j.get("count") or 0)) for j in jobs)
        p = core.load(pid)
        ap = p.get("autopipe") or {}
        indexes = [int(i) for i in (ap.get("indexes") or [])]
        if len(indexes) < need:
            before = len(p.get("plans") or [])
            batch.ai_plan_multi(
                pid, jobs, order=cfg.get("order") or "chrono",
                append=before > 0, new_batch=True, eps=cfg.get("eps") or [],
            )
            _check_cancel(pid)
            p = core.load(pid)
            indexes = [int(pl["index"]) for pl in (p.get("plans") or [])[before:]]
            if len(indexes) < need:
                raise RuntimeError(f"这批改只生成了 {len(indexes)} 套，期望 {need} 套")
            _stage(pid, "plan", indexes=indexes, batch_no=(p.get("batch") or {}).get("plan_batch_no"))
        else:
            _stage(pid, "plan")

        # ④ 出片（only 限定本次生成的套，不碰用户别的批次；render_plans 自带按指纹跳过已渲的）
        core.set_task(pid, "autopipe", 76, f"全自动 ④/④ 正在出片（{len(indexes)} 套）…")
        save_dir = (cfg.get("save_dir") or "").strip()
        fx = dict(DEFAULT_FX)
        fx.update(cfg.get("fx") or {})
        # 用户勾了配乐但没选文件夹：接口层会拦；这里提前给句人话
        if fx.get("bgm") and fx.get("bgm_mode", "folder") == "folder" and not (fx.get("bgm_dir") or "").strip():
            raise RuntimeError("勾了配乐但没选音乐文件夹，请到「全自动设置」里选一个放着多首歌的文件夹")
        batch.render_plans(pid, save_dir=save_dir, fx=fx, only=indexes)
        _check_cancel(pid)

        _stage(pid, "done", done=True, last_error="")
        core.set_task(pid, "autopipe", 100, "全自动完成：台词、底片、剪辑、出片都好了")
    except core.TaskCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        msg = core._friendly_error(exc)
        try:
            _stage(pid, "error", last_error=msg)
        except Exception:
            pass
        raise
