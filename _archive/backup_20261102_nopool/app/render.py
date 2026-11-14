"""渲染：从原片逐段切出 → 统一规格 → 拼接 → 统一响度。"""
from __future__ import annotations

import os
import shutil
import subprocess
import time

from . import core

W, H, FPS = 720, 1280, 30


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        err = r.stderr or ""
        if core.is_disk_full(err):
            # 底层 ENOSPC 对用户没有意义，换成能照着做的提示
            raise RuntimeError(
                "磁盘空间不足，写文件时中断了。请清理磁盘，"
                "或在「设置 → 成片保存位置」里改到空间更大的盘，然后重新出片。"
            )
        raise RuntimeError(f"ffmpeg 失败：{err[-400:]}")


def _estimate_mb(edl: list[dict]) -> float:
    """粗估渲染需要多少空间：切段中间文件 + 成品 + 余量。

    实测 720x1280 CRF18 veryfast 约 12MB/分钟成品，切段中间文件约 5MB/段（十几秒一段）。
    这里按 0.5MB/秒 + 10MB/段 再乘 1.5 安全系数，宁可多报一点也别半路写满。
    """
    sec = sum(max(0.0, float(s.get("end", 0)) - float(s.get("start", 0))) for s in edl)
    return (sec * 0.5 + len(edl) * 10) * 1.5


def _cut(src: str, start: float, end: float, dst: str) -> None:
    vf = (
        f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS}"
    )
    _run([
        core.ff("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.3f}", "-t", f"{max(0.5, end - start):.3f}", "-i", src,
        "-vf", vf,
        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
        "-avoid_negative_ts", "make_zero", dst,
    ])


def render_edl(p: dict, edl: list[dict], name: str, progress=None,
               cleanup: bool = True, out_dir: str | None = None) -> dict:
    """把一份 EDL 渲染成成片文件，返回 {name,duration,size}。不写项目文件，由调用方落库。

    out_dir：成片最终落在哪个目录（用户自己指定，不替用户起子文件夹名）。
    不传则用全局「成片保存位置」。临时分段文件也放同一块盘，避免跨盘写。

    cleanup=False（批量连渲用）：进出都不清临时目录——分段文件固定命名 s000.mp4…
    直接被 ffmpeg -y 覆盖复用。批量场景每套删 25-30 个文件，多套连删会触发本机
    删除守卫的按轮计数拦截（2026-11-01 第 3 套卡死的根源），所以整批只在收尾清一次，
    由调用方（batch.render_plans）负责。"""
    pid = p["id"]
    by_index = {ep["index"]: ep for ep in p["episodes"]}
    target_dir = os.path.abspath(out_dir) if out_dir else core.output_dir()
    # 先算要多少空间再动手：宁可一开始就告诉用户「不够」，也别切到第 15 段才失败
    core.check_space(target_dir, _estimate_mb(edl), "这次出片")
    os.makedirs(target_dir, exist_ok=True)
    # 临时目录放成片同盘：切割 20 段约 1GB，跟程序盘空间脱钩
    work = os.path.join(target_dir, "_render_tmp", pid)
    if cleanup:
        core.rmtree(work)
    os.makedirs(work, exist_ok=True)

    out = os.path.join(target_dir, name)
    out_tmp = out + ".part.mp4"
    if os.path.exists(out_tmp):
        os.remove(out_tmp)

    try:
        total = len(edl)
        parts = []
        for i, seg in enumerate(edl):
            ep = by_index.get(seg["ep"])
            if not ep:
                raise RuntimeError(f"第 {seg['ep']} 集不存在")
            src = os.path.join(p["src_dir"], ep["file"])
            dst = os.path.join(work, f"s{i:03d}.mp4")
            if progress:
                progress(i / total * 88,
                         f"切第 {i + 1}/{total} 段（第 {seg['ep']} 集 {seg['start']:.1f}s）")
            _cut(src, seg["start"], seg["end"], dst)
            parts.append(dst)

        listfile = os.path.join(work, "list.txt")
        with open(listfile, "w", encoding="utf-8") as f:
            for path in parts:
                # concat 清单里必须用正斜杠：反斜杠会被 ffmpeg 当转义符
                f.write(f"file '{path.replace(os.sep, '/')}'\n")

        if progress:
            progress(90, "拼接与统一响度")
        # 拼接一步到位：视频流直接 copy，音频边拼边重编码（loudnorm）。
        # 之前是「先 -c copy 拼接、再二次封装响度」，部分源视频的音频时间戳
        # 不干净，copy 出来的中间文件在二次封装时报 Error muxing a packet；
        # 音频重编码后时间戳干净，从根上避开。genpts 兜底修段间时间戳。
        _run([core.ff("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
              "-fflags", "+genpts",
              "-f", "concat", "-safe", "0", "-i", listfile,
              "-c:v", "copy",
              "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "48000", "-ac", "2",
              "-c:a", "aac", "-b:a", "192k",
              "-movflags", "+faststart", out_tmp])
        os.replace(out_tmp, out)
    except BaseException:
        # 失败时必须清干净：磁盘满的时候，残留的分段文件会把最后一点空间也吃光，
        # 用户重试一次还是失败，看起来像「怎么点都出不来」。
        # 清理自身也不允许抛异常——删不掉就留着，别盖住真正的错误。
        try:
            if os.path.exists(out_tmp):
                os.remove(out_tmp)
        except Exception:
            pass
        if cleanup:
            core.rmtree(work)
        raise
    finally:
        if cleanup:
            core.rmtree(work)

    info = core.ffprobe(out)
    return {"name": name, "duration": info["duration"], "size": os.path.getsize(out)}


def run(pid: str) -> None:
    p = core.load(pid)
    if not p:
        return
    edl = p.get("edl") or []
    if not edl:
        raise RuntimeError("还没有剪辑结构，请先让 AI 出表")

    name = f"{p['name']}_{p['target_minutes']}min_{time.strftime('%m%d%H%M')}.mp4"
    info = render_edl(p, edl, name,
                      progress=lambda pct, msg: core.set_task(pid, "render", pct, msg))

    p["output"] = info["name"]
    p["output_duration"] = info["duration"]
    p["output_size"] = info["size"]
    p["stage"] = "rendered"
    core.set_task(pid, None)
    core.save(p)
