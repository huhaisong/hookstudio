"""往「已有项目」追加新视频，并一键跑完 转写新集 → 对时+校对 → 抹原字幕。

设计要点（2026-11-20）：
- 只碰新增的集：旧集的 index / 台词 / 校对结果 / 已出成片一律不动。
- 转写走 transcribe.run(pid, force=False) —— 它本来就只转「没有台词」的集，
  正好只跑新集；对时/校对在转写末尾自动跟着走，且按台词指纹缓存，旧集不重算。
- 抹字幕走 nosub.run(pid, on_progress=…) —— 副本指纹不含剧集列表、逐集按文件名跳过，
  旧副本不重编；on_progress 把它的 0~100 接到本任务 92%~100% 的刻度上
  （不接管的话进度会落到 nosub 槽、收尾还会把本任务的 cuts 槽清掉）。
- 视频路径：src_dir 里（含子目录扫描来源在本模块外处理）记相对文件名；外部选来的
  记绝对路径。读取处统一 os.path.join(src_dir, file)，绝对路径会被原样采用。
"""
from __future__ import annotations

import os

from . import core

VIDEO_EXT = (".mp4", ".mov", ".mkv")


def _videos_in(path: str) -> list[str]:
    try:
        return [f for f in os.listdir(path) if f.lower().endswith(VIDEO_EXT)]
    except OSError:
        return []


def _full(p: dict, file_field: str) -> str:
    """episode.file → 绝对路径（绝对路径字段在 join 时会被原样采用）。"""
    f = str(file_field or "")
    return os.path.abspath(f if os.path.isabs(f) else os.path.join(p.get("src_dir") or "", f))


def _file_field(p: dict, ap: str) -> str:
    """绝对路径 → episode.file：在 src_dir 下记文件名，否则记绝对路径。"""
    src = os.path.abspath(p.get("src_dir") or "")
    if os.path.dirname(ap).lower() == src.lower():
        return os.path.basename(ap)
    return ap.replace("\\", "/")


def add(pid: str, paths: list[str] | None, scan_src: bool) -> dict:
    """扫描来源、追加新集到 episodes 并存盘。返回 {added, skipped, errors, indexes}。

    幂等：按解析后的绝对路径去重，已导入的（哪怕文件名写法不同）不会再进一次。
    """
    p = core.load(pid)
    if not p:
        raise RuntimeError("项目不存在")
    src = os.path.abspath(p.get("src_dir") or "")
    if scan_src and not os.path.isdir(src):
        raise RuntimeError("项目素材目录不存在，请用「选视频」添加")

    existing = list(p.get("episodes") or [])
    seen = {_full(p, e.get("file")).lower() for e in existing}
    candidates: list[str] = []           # 绝对路径
    if scan_src:
        candidates += [os.path.join(src, f) for f in _videos_in(src)]
    for raw in (paths or []):
        raw = str(raw or "")
        if not raw:
            continue
        if os.path.isdir(raw):
            candidates += [os.path.join(raw, f) for f in _videos_in(raw)]
        elif os.path.isfile(raw) and raw.lower().endswith(VIDEO_EXT):
            candidates.append(raw)

    added, skipped, errors, indexes = [], 0, [], []
    next_i = max((int(e.get("index") or 0) for e in existing), default=0) + 1
    for ap in candidates:
        ap = os.path.abspath(ap)
        key = ap.lower()
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        if not os.path.isfile(ap):
            continue
        try:
            info = core.ffprobe(ap)
        except Exception as exc:  # noqa: BLE001 —— 单个文件坏掉不拖垮整批
            errors.append(f"{os.path.basename(ap)}：{exc}")
            continue
        ep = {
            "index": next_i, "file": _file_field(p, ap),
            "duration": info["duration"], "width": info["width"],
            "height": info["height"], "status": "pending", "lines": [],
        }
        added.append(ep)
        indexes.append(next_i)
        next_i += 1

    if added:
        p["episodes"] = existing + added
        core.save_merge(p, ["episodes"])
    return {"added": len(added), "skipped": skipped, "errors": errors,
            "indexes": indexes, "total": len(p["episodes"])}


def run(pid: str) -> None:
    """一键后台任务：新集转写（含对时+校对）→ 抹字幕。归 cuts 组，可被中止。"""
    from . import nosub, transcribe

    core.set_task(pid, "addvideos", 1, "新增视频已加入，开始整理台词…")
    # force=False 只转没有台词的集 = 本次新增的集；末尾自动对时+整剧校对
    transcribe.run(pid, force=False)
    if core.is_cancelled(pid, "cuts"):
        return
    core.set_task(pid, "addvideos", 92, "台词整理好了，正在给新集抹掉原字幕…")
    # 内嵌调用要接管进度：默认写 nosub 槽的话，本任务在 cuts 的进度会停在 92%，
    # 而 nosub.run 末尾的 set_task(None) 又会把 cuts 槽清掉（2026-11-21）。
    nosub.run(pid, on_progress=lambda pct, msg: core.set_task(
        pid, "addvideos", 92 + pct * 0.08, f"抹字幕 · {msg}"))
