"""转写：faster-whisper 本地跑，输出逐句时间码。

模型固定 small（随包只带这一个，见 core.TIER）；换成别的模型只需改 core.TIER 并放好 models/<tier>。
音频解码用随包的 ffmpeg（不走 PyAV）——PyAV 各版本参数变动大，ffmpeg 稳定且我们本来就要带。
"""
from __future__ import annotations

import os
import threading
import time

from . import core, names, zh

_MODEL = None
_MODEL_KEY: tuple | None = None
_LOAD_LOCK = threading.Lock()
# ctranslate2 / faster-whisper 模型不是线程安全的：两个线程同时推理会输出空段甚至崩溃
# （表现为「转写 done 却 0 句」「IndexError: list index out of range」）。
# 转写是 CPU 密集任务，本就该串行，这里用全局锁强制同一时刻只有一个推理在跑。
_INFER_LOCK = threading.Lock()
SR = 16000


def _decode(path: str):
    """解成 16k 单声道 float32 波形，直接喂给模型，绕开 PyAV 版本兼容问题。"""
    import numpy as np

    cmd = [
        core.ff("ffmpeg"), "-nostdin", "-v", "error", "-i", path,
        "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-",
    ]
    r = core.run_cancelable(cmd)
    if r.returncode != 0:
        msg = (r.stderr or b"").decode("utf-8", errors="ignore")[-300:]
        raise RuntimeError(f"音频解码失败：{msg}")
    pcm = np.frombuffer(r.stdout, dtype=np.int16)
    if pcm.size == 0:
        raise RuntimeError("这一集没有可用的音频轨道")
    return pcm.astype(np.float32) / 32768.0


def _model():
    """按当前档位取模型；档位或模型路径变了就重新加载。加锁避免预热与首次转写重复加载。"""
    global _MODEL, _MODEL_KEY
    tier = core.whisper_tier()
    path = core.model_dir(tier)
    key = (tier, path)
    if _MODEL is not None and _MODEL_KEY == key:
        return _MODEL
    with _LOAD_LOCK:
        if _MODEL is not None and _MODEL_KEY == key:  # 等锁期间别人已经加载好了
            return _MODEL
        if not os.path.isfile(os.path.join(path, "model.bin")):
            raise RuntimeError(
                f"找不到 Whisper 模型：{path}\n"
                "安装包不完整，请重新解压 models 目录（应有 models\\small\\model.bin）"
            )
        from faster_whisper import WhisperModel

        _MODEL = WhisperModel(path, device="cpu", compute_type="int8")
        _MODEL_KEY = key
        return _MODEL


def warmup() -> None:
    """提前把模型读进内存，避免用户点「开始转写」后干等半分钟。"""
    try:
        _model()
    except Exception:  # noqa: BLE001
        pass


def transcribe_episode(path: str, on_progress=None) -> list[dict]:
    """on_progress(0~1)：按句增量回调，让前端进度条能平滑地走，而不是每集跳一格。"""
    model = _model()
    audio = _decode(path)
    total_sec = len(audio) / SR if SR else 0.0
    use_zh = zh.enabled()
    # 两层提示合并成**一条** initial_prompt：faster-whisper 只认 prefix，
    # hotwords 参数在 prefix 非空时会被直接忽略（见 app/names.py hint 注释）。
    # 关掉简体开关就只留人名提示；两个都没内容时传 None，等于完全不提示。
    parts = [zh.prompt()] if use_zh else []
    hint = names.hint()
    if hint:
        parts.append(hint)
    prompt = "".join(parts) if parts else None
    with _INFER_LOCK:  # 见 _INFER_LOCK 注释：推理必须全局串行
        segments, _info = model.transcribe(
            audio, language="zh", beam_size=5, vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 400},
            initial_prompt=prompt,
        )
        lines = []
        for seg in segments:
            text = (seg.text or "").strip()
            if not text:
                continue
            lines.append({
                "start": round(float(seg.start), 3),
                "end": round(float(seg.end), 3),
                "text": text,
            })
            if on_progress and total_sec > 0:
                on_progress(min(1.0, float(seg.end) / total_sec), len(lines))
    # 先繁转简、再套人名表，顺序不能反：人名表里可能存着繁体错写法（顧清唐），
    # 只有先转成简体，才跟表里的写法对得上；反过来会漏掉那几句。
    n_zh = zh.apply_lines(lines) if use_zh else 0
    if n_zh:
        print(f"[transcribe] 繁体转简体 {n_zh} 字：{os.path.basename(path)}", flush=True)
    # 人名写法统一（app/names.py）：同音错字按对照表换掉。只动文字，start/end 不动。
    # 放在这里而不是 run() 里，是因为「重转写单集」走的是另一个入口，两边都要生效。
    fixed = names.apply_lines(lines)
    if fixed:
        print(f"[transcribe] 人名写法统一 {fixed} 处：{os.path.basename(path)}", flush=True)
    return lines


def _merge_save(pid: str, ep_index: int, **patch) -> dict:
    """把某一集的结果合并进「磁盘上的最新状态」再保存。

    绝不能用线程开头 load 的旧快照整体覆盖——那样并发转写时，
    A 线程每保存一集都会把 B 线程已转好的集抹回旧值（0 句事故的根源）。
    返回最新版项目 dict，供后续循环继续使用。
    """
    p = core.load(pid) or {}
    for ep in p.get("episodes") or []:
        if ep.get("index") == ep_index:
            ep.update(patch)
            break
    if p:
        core.save_merge(p, ["episodes"])
    return p


def run(pid: str) -> None:
    p = core.load(pid)
    if not p:
        return
    eps = p["episodes"]
    total = len(eps)
    core.set_task(pid, "transcribe", 0, f"准备转写，共 {total} 集（模型：{core.whisper_tier()}）")
    for i, ep in enumerate(eps):
        if ep.get("lines"):
            continue
        base = i / total * 100
        step = 100 / total
        head = f"正在转写第 {ep['index']} 集（{i + 1}/{total}）"
        last = [0.0]
        seen = [0]

        def prog(frac, n=0, base=base, step=step, head=head, last=last, seen=seen):
            # task.json 每次都会落盘：按 0.35s 或每涨 3 句节流一次，既不全丢也不狂写文件
            now = time.time()
            if frac < 0.995 and now - last[0] < 0.35 and n - seen[0] < 3:
                return
            last[0] = now
            seen[0] = n
            core.set_task(pid, "transcribe", base + step * frac, f"{head} · 已识别 {n} 句")

        core.set_task(pid, "transcribe", base, f"{head} · 读取音频中")
        src = os.path.join(p["src_dir"], ep["file"])
        t0 = time.time()
        try:
            lines = transcribe_episode(src, prog)
            p = _merge_save(pid, ep["index"], lines=lines, status="done")
            print(f"[transcribe] {p['name']} 集{ep['index']}：{len(lines)} 句，"
                  f"耗时 {time.time() - t0:.1f}s", flush=True)
        except Exception as exc:  # noqa: BLE001
            p = _merge_save(pid, ep["index"], status="error",
                            error=f"{type(exc).__name__}: {exc}")
            print(f"[transcribe] {p['name']} 集{ep['index']} 失败：{exc}", flush=True)
        if p:
            eps = p["episodes"]
    p = core.load(pid) or p
    if p:
        p["stage"] = "transcribed"
        core.save_merge(p, ["stage"])
    core.set_task(pid, None)
