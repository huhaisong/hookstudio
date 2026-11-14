"""第 4 步：两两合并（2026-11-05）。

用户选两个文件夹 A、B，把 A 里每个视频和 B 里每个视频按顺序两两拼成一条：
A1+B1、A1+B2、…、A2+B1、…（外层循环 A、内层循环 B，和用户举例的顺序一致）。

设计要点：
- **相对独立**：不依赖项目（不需要导入剧集、转写），自己一套任务状态，
  不占 core.start_task 的项目任务槽——用户的合并和项目渲染互不妨碍。
- **逐对一条 ffmpeg**：filter_complex 里 scale/pad/fps + concat + loudnorm
  一次完成，任意分辨率/帧率/编码的源都能拼，输出统一 720x1280 / 30fps。
- **断点续跑**：输出文件名 = A名+B名.mp4，已存在的直接跳过——中断了
  （或上次跑了一半）再点一次，只补缺的，不重做已有的。
- **取消**：只在对与对之间检查，正在编码的那对让它编完（杀 ffmpeg 半路
  会留半个坏文件，得不偿失）。
- **显卡加速**：复用 render 的基准式探测与软编兜底（_venc_args/_run_v），
  与出片共用同一套编码参数。
"""
from __future__ import annotations

import os
import re
import threading

from app import core, render

W, H, FPS = render.W, render.H, render.FPS

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".wmv",
              ".webm", ".m4v", ".ts", ".mpg", ".mpeg"}

# ── 任务状态（全局同一时间只跑一个合并任务）────────────────────────────────
_LOCK = threading.Lock()
_ST: dict = {
    "running": False, "pct": 0.0, "msg": "", "error": "",
    "total": 0, "done": 0, "skipped": 0, "failed": [],
    "out_dir": "", "cancel": False, "at": 0.0,
}


def list_videos(d: str) -> list[str]:
    """文件夹里的视频文件，按自然序排（a2 在 a10 前面，不是字典序）。"""
    if not d or not os.path.isdir(d):
        return []
    out = []
    for name in os.listdir(d):
        path = os.path.join(d, name)
        if os.path.isfile(path) and os.path.splitext(name)[1].lower() in VIDEO_EXTS:
            out.append(path)
    out.sort(key=lambda p: _natkey(os.path.basename(p)))
    return out


def _natkey(s: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def preview(dir_a: str, dir_b: str) -> dict:
    """开跑前的确认单：两边各有哪些视频、会组合出多少条。"""
    a, b = list_videos(dir_a), list_videos(dir_b)
    return {
        "a_dir": dir_a, "b_dir": dir_b,
        "a_files": [os.path.basename(x) for x in a],
        "b_files": [os.path.basename(x) for x in b],
        "pairs": len(a) * len(b),
    }


def default_out() -> str:
    """默认输出目录：设置里的「成片保存位置」下开一个「两两合并」子目录。
    **与文件夹 A 无关**（2026-11-05 用户要求：输出不能默认写进源文件夹），
    用户也可以在界面上自己指定任意目录。"""
    return os.path.join(core.output_dir(), "两两合并")


def _safe_stem(stem: str) -> str:
    """文件名清洗：Windows 非法字符换成下划线，掐到 80 字（留扩展名余量）。"""
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .")
    return stem[:80] or "video"


def _has_audio(path: str) -> bool:
    cmd = [core.ff("ffprobe"), "-v", "error", "-select_streams", "a",
           "-show_entries", "stream=index", "-of", "csv=p=0", path]
    try:
        r = core.subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return bool(r.stdout.strip())
    except Exception:  # noqa: BLE001  探不出就当没有，静音兜底比报错友好
        return False


def _dur(path: str) -> float:
    d = core.ffprobe(path).get("duration") or 0.0
    return d if d > 0 else 1.0


def _merge_pair(a: str, b: str, dst: str) -> None:
    """A + B → dst。一条 ffmpeg：两边各自统一规格再 concat，音频统一响度。

    没有音轨的源用 aevalsrc 补一条等长静音（concat 要求每个片段 v/a 都有，
    缺一边整个 filter_complex 直接报错——用户手机竖录关麦的视频很常见）。
    """
    inputs = ["-i", a, "-i", b]
    # 音轨缺失时把静音源追加在后面，索引记下来
    silent: list[str] = []   # 待追加的 lavfi 静音输入
    amap: dict[int, int] = {}  # 视频输入序号 -> 对应音频输入序号
    nxt = 2
    for i, src in ((0, a), (1, b)):
        if _has_audio(src):
            amap[i] = i
        else:
            amap[i] = nxt
            silent.append(f"aevalsrc=0:s=48000:c=stereo:d={_dur(src):.3f}")
            nxt += 1
    for s in silent:
        inputs += ["-f", "lavfi", "-i", s]

    def norm(v: int) -> str:
        return (f"[{v}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS}")

    fc = (
        f"{norm(0)}[v0];{norm(1)}[v1];"
        f"[{amap[0]}:a]aformat=sample_rates=48000:channel_layouts=stereo[a0];"
        f"[{amap[1]}:a]aformat=sample_rates=48000:channel_layouts=stereo[a1];"
        f"[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][ca];"
        f"[ca]loudnorm=I=-16:TP=-1.5:LRA=11[aout]"
    )
    cmd = [core.ff("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
           *inputs, "-filter_complex", fc,
           "-map", "[v]", "-map", "[aout]",
           *render._venc_args(),
           "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart", dst]
    render._run_v(lambda: cmd)


def start(dir_a: str, dir_b: str, out_dir: str) -> dict:
    """启动合并任务。同一时间只允许一个；返回 {ok} 或 {ok:False, reason}。"""
    with _LOCK:
        if _ST["running"]:
            return {"ok": False, "reason": "已有合并任务在跑"}
        va, vb = list_videos(dir_a), list_videos(dir_b)
        if not va:
            return {"ok": False, "reason": f"第一个文件夹里没有视频：{dir_a}"}
        if not vb:
            return {"ok": False, "reason": f"第二个文件夹里没有视频：{dir_b}"}
        out = os.path.abspath(out_dir or default_out())
        os.makedirs(out, exist_ok=True)
        # 文件名先全量生成，重名的（清洗后撞名）补序号，避免跑一半互相覆盖
        names: set[str] = set()
        pairs: list[tuple[str, str, str]] = []
        for pa in va:
            for pb in vb:
                stem = f"{_safe_stem(os.path.splitext(os.path.basename(pa))[0])}+{_safe_stem(os.path.splitext(os.path.basename(pb))[0])}"
                name = f"{stem}.mp4"
                k = 2
                while name.lower() in names:
                    name = f"{stem}({k}).mp4"
                    k += 1
                names.add(name.lower())
                pairs.append((pa, pb, os.path.join(out, name)))
        _ST.update({"running": True, "pct": 0.0, "msg": "已排队…", "error": "",
                    "total": len(pairs), "done": 0, "skipped": 0, "failed": [],
                    "out_dir": out, "cancel": False, "at": core.time.time()})

    def runner() -> None:
        total = len(pairs)
        try:
            render._venc_args()  # 开跑前预热编码探测，不算进第一对的时间
            for i, (pa, pb, dst) in enumerate(pairs):
                if _ST["cancel"]:
                    _ST["msg"] = "已取消"
                    break
                an, bn = os.path.basename(pa), os.path.basename(pb)
                if os.path.isfile(dst):
                    _ST["skipped"] += 1
                else:
                    try:
                        _merge_pair(pa, pb, dst)
                        _ST["done"] += 1
                    except Exception as exc:  # noqa: BLE001
                        # 一对失败不拖垮整批：记下来继续拼别的（输出文件可能
                        # 是半个坏的，删掉，下次重跑会补）
                        try:
                            if os.path.isfile(dst):
                                os.remove(dst)
                        except OSError:
                            pass
                        _ST["failed"].append(f"{an} + {bn}：{exc}")
                _ST["pct"] = round((i + 1) / total * 100, 1)
                _ST["msg"] = f"合并 {an} + {bn}（{i + 1}/{total}）"
            if not _ST["cancel"] and not _ST["failed"]:
                _ST["msg"] = f"完成：新拼 {_ST['done']} 条，跳过已有 {_ST['skipped']} 条"
        except Exception as exc:  # noqa: BLE001
            _ST["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            _ST["running"] = False
            _ST["pct"] = 100.0

    threading.Thread(target=runner, daemon=True).start()
    return {"ok": True, "pairs": len(pairs), "out_dir": out.replace("\\", "/")}


def status() -> dict:
    with _LOCK:
        out = {k: (list(v) if isinstance(v, list) else v)
               for k, v in _ST.items() if k != "cancel"}
        out["cancellable"] = _ST["running"]
        return out


def cancel() -> None:
    with _LOCK:
        if _ST["running"]:
            _ST["cancel"] = True
            _ST["msg"] = "正在取消（等当前这一对编完）…"


def open_out() -> bool:
    d = _ST.get("out_dir") or ""
    if d and os.path.isdir(d):
        os.startfile(d)  # noqa: S606  本机工具，资源管理器打开输出目录
        return True
    return False
