"""去掉原片背景音乐：MDX-Net 人声分离（ONNX，纯 CPU）。

为什么不用 Demucs：它要带 torch（1.5~2GB）进 onedir 发布包，代价过高。
MDX-Net 走 onnxruntime（已在 runtime 里，47MB）+ 单个 64MB 模型，
CPU 实测约 1.6x 实时（i5-10400，10 线程），发布包只增 64MB。

分工：
  · 只处理**成片那一条音轨**（通常 6 分钟），不碰整集素材；
  · 结果按音频内容哈希缓存，同一批素材复跑直接命中，秒过；
  · 分离后按 keep_ambient_db 把伴奏轨压到很轻再混回去。
    **默认 -60 等于彻底去掉**：两轨模型的「伴奏轨」就是 BGM+音效的混合，
    留任何可闻的伴奏都会把 BGM 一起带回来（实测 -12 时 BGM 仍清晰可闻，
    再被 loudnorm 拉高一截）。要现场感就得接受 BGM 回来，只能二选一。

模型：models/mdx/Kim_Vocal_2.onnx（UVR 社区评价最高的人声模型）。
注意 UVR 的 *Inst* 系列是「提取伴奏/去人声」的，别拿错。
"""
from __future__ import annotations

import gc
import hashlib
import os
import subprocess
import threading
import time

import numpy as np

from . import core

VERSION = 1
MODEL_NAME = "Kim_Vocal_2"
# 与 whisper 模型同规矩：跟着程序目录走（core.BASE 打包后是 exe 同级）
MODEL_DIR = os.path.join(core.BASE, "models", "mdx")
CACHE_DIR = os.path.join(core.DATA_DIR, "voice_cache")

SR = 44100
N_FFT = 6144
HOP = 1024
DIM_F = 3072
DIM_T = 256
WIN = np.hanning(N_FFT + 1)[:-1].astype(np.float32)  # periodic hann，与 torch 一致

WIN_SEC = 20.0     # 每窗处理长度
OVL_SEC = 0.5      # 窗间重叠（线性淡化，消掉接缝）
THREADS = 10       # 实测 10 线程最快；开到 12（超线程全占）反而掉到 1.17x

# 并行出片偶发 OOM 自动重试（2026-11-21）：numpy 分配瞬时大数组失败时退避再试
_OOM_RETRIES = 2   # 重试次数（总共最多尝试 3 次）
_OOM_BACKOFF = 4   # 每次退避秒数，递增：4s、8s

_SESS = None
# 出片并行（2026-11-19）：多套同时渲染时会有多个子线程一起调 separate。
# 人声分离是 CPU 密集（10 线程 ONNX），并行跑只会抢核、得不偿失；更麻烦的是
# 下面的 _tmp_{pid}.raw 是按进程号命名的固定文件，多线程会互相覆盖导致串音。
# 用一把锁把整段分离串行化：渲染其余阶段照常并行，只有这一步排队。
_SESS_LOCK = threading.Lock()


def model_path() -> str:
    """模型文件路径。打包后 models/ 与 exe 同级（与 whisper 模型同一套规矩）。"""
    return os.path.join(MODEL_DIR, MODEL_NAME + ".onnx")


def available() -> bool:
    return os.path.isfile(model_path())


def _session():
    global _SESS
    if _SESS is None:
        import onnxruntime as ort
        p = model_path()
        if not os.path.isfile(p):
            raise RuntimeError(
                "人声分离模型缺失：%s（需要 %s.onnx，64MB）" % (p, MODEL_NAME))
        so = ort.SessionOptions()
        so.intra_op_num_threads = THREADS
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        _SESS = ort.InferenceSession(p, providers=["CPUExecutionProvider"],
                                     sess_options=so)
    return _SESS


def _stft(x: np.ndarray) -> np.ndarray:
    """[2,T] → [2,DIM_F,T] 复数谱（center + reflect pad，裁到 DIM_F，与 UVR 一致）"""
    pad = N_FFT // 2
    xp = np.pad(x, ((0, 0), (pad, pad)), mode="reflect")
    t = 1 + (xp.shape[1] - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(t)[:, None]
    sp = np.fft.rfft(xp[:, idx] * WIN, axis=-1)[:, :, :DIM_F].transpose(0, 2, 1)
    return sp.astype(np.complex64)


def _istft(sp: np.ndarray, t_out: int) -> np.ndarray:
    """[2,DIM_F,T] → [2,t_out]（重叠相加 + 窗平方归一）"""
    full = np.zeros((2, N_FFT // 2 + 1, sp.shape[2]), dtype=np.complex64)
    full[:, :DIM_F, :] = sp
    fr = np.fft.irfft(full, n=N_FFT, axis=1).transpose(0, 2, 1) * WIN
    total = (sp.shape[2] - 1) * HOP + N_FFT
    out = np.zeros((2, total), dtype=np.float32)
    ws = np.zeros(total, dtype=np.float32)
    for i in range(sp.shape[2]):
        a = i * HOP
        out[:, a:a + N_FFT] += fr[:, i, :]
        ws[a:a + N_FFT] += WIN * WIN
    out /= np.maximum(ws, 1e-8)
    return out[:, N_FFT // 2: N_FFT // 2 + t_out]


def _run(sess, sp: np.ndarray) -> np.ndarray:
    """按 DIM_T 帧分块过模型，输出人声谱（同形状）"""
    t_all = sp.shape[2]
    out = np.zeros_like(sp)
    name = sess.get_inputs()[0].name
    for s in range(0, t_all, DIM_T):
        e = min(t_all, s + DIM_T)
        seg = sp[:, :, s:e]
        if e - s < DIM_T:
            seg = np.pad(seg, ((0, 0), (0, 0), (0, DIM_T - (e - s))))
        inp = np.concatenate([seg.real, seg.imag], axis=0)[None].astype(np.float32)
        o = sess.run(None, {name: inp})[0][0]
        out[:, :, s:e] = (o[:2] + 1j * o[2:]).astype(np.complex64)[:, :, : e - s]
    return out


def _key(src: str, keep_db: float) -> str:
    """内容指纹：头尾各 1MB + 长度。比全量 md5 快得多，改内容必变。"""
    size = os.path.getsize(src)
    h = hashlib.md5()
    h.update(("%s|%s|%d|%.2f" % (VERSION, MODEL_NAME, size, keep_db)).encode())
    with open(src, "rb") as f:
        h.update(f.read(1 << 20))
        if size > (2 << 20):
            f.seek(-(1 << 20), 2)
            h.update(f.read(1 << 20))
    return h.hexdigest()[:16]


def separate(wav: str, keep_ambient_db: float = -60.0,
             on_progress=None) -> str:
    """线程安全入口：串行化人声分离（见 _SESS_LOCK 说明）。

    并行出片时偶发 OOM（numpy "Unable to allocate ... MiB"）：几路 ffmpeg +
    ONNX 在同一时刻抢内存，分离里的瞬时大数组（_stft 的 (2,T,6144)）分配失败。
    不是真缺内存（失败后机器仍有数 GB 空闲），是瞬时峰值。这里自动重试：
    释放资源、短暂退避让别的路先跑完释放内存，再试，最多 RETRY 次。
    """
    for attempt in range(_OOM_RETRIES + 1):
        try:
            with _SESS_LOCK:
                return _separate(wav, keep_ambient_db, on_progress)
        except MemoryError as e:
            if attempt >= _OOM_RETRIES:
                raise RuntimeError(
                    "分离人声时内存不足，重试 %d 次仍失败：%s" % (_OOM_RETRIES, e)) from e
            # 退避：让别的渲染路跑完、把瞬时峰值让出去。锁在 with 退出时已释放。
            gc.collect()
            time.sleep(_OOM_BACKOFF * (attempt + 1))


def _separate(wav: str, keep_ambient_db: float, on_progress) -> str:
    """把 wav 里原片的背景音乐去掉，只留人声。

    keep_ambient_db：伴奏保留多少 dB。**默认 -60 = 彻底去掉**。
        别想着「留一点环境底」——MDX 只有人声/伴奏两轨，伴奏轨里装的就是
        BGM+音效，压得不够 BGM 会一直跟着（-12 时安静处只降 10.8 dB，
        再经 loudnorm 归一拉高，听感上等于没去）。要现场感就得接受 BGM 回来。
    返回分离后的 wav 路径（可能是缓存）。不修改输入文件。
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    dst = os.path.join(CACHE_DIR, _key(wav, keep_ambient_db) + ".wav")
    if os.path.isfile(dst) and os.path.getsize(dst) > 1024:
        return dst

    sess = _session()
    raw = os.path.join(CACHE_DIR, "_tmp_%d_%d.raw" % (os.getpid(), threading.get_ident()))
    try:
        # 解码成 44.1k 立体声 f32，用 memmap 读，避免整片进内存
        r = subprocess.run([core.ff("ffmpeg"), "-v", "error", "-y", "-i", wav,
                            "-vn", "-ac", "2", "-ar", str(SR),
                            "-f", "f32le", raw], capture_output=True)
        if r.returncode != 0:
            raise RuntimeError("解码音频失败：%s" % r.stderr.decode("utf-8", "ignore")[:200])
        size = os.path.getsize(raw)
        n = size // 8  # 2ch * float32
        if n <= 0:
            raise RuntimeError("音频为空：%s" % wav)
        data = np.memmap(raw, dtype=np.float32, mode="r", shape=(n, 2)).T

        g = float(10.0 ** (keep_ambient_db / 20.0))  # 保留的伴奏比例
        step = int(WIN_SEC * SR)
        ovl = int(OVL_SEC * SR)
        out = np.zeros((2, n), dtype=np.float32)
        pos = 0
        done = 0
        while pos < n:
            end = min(n, pos + step)
            x = np.ascontiguousarray(data[:, pos:end], dtype=np.float32)
            sp = _stft(x)
            vsp = _run(sess, sp)
            # 人声 + g×伴奏 = g×原谱 + (1-g)×人声谱（频域残差，少一次逆变换）
            mixed = (g * sp + (1.0 - g) * vsp).astype(np.complex64)
            y = _istft(mixed, x.shape[1])
            if pos == 0:
                out[:, pos:end] = y
            else:
                k = min(prev_end - pos, y.shape[1])
                if k > 0:
                    w = np.linspace(0.0, 1.0, k, dtype=np.float32)[None, :]
                    out[:, pos:pos + k] = out[:, pos:pos + k] * (1 - w) + y[:, :k] * w
                    if k < y.shape[1]:
                        out[:, pos + k:end] = y[:, k:]
                else:
                    out[:, pos:end] = y
            prev_end = end
            done = end
            if on_progress:
                on_progress(min(0.99, done / max(1, n)))
            if end >= n:
                break
            pos = end - ovl

        tmp = dst + ".part.wav"
        p = subprocess.Popen([core.ff("ffmpeg"), "-v", "error", "-y", "-f", "f32le",
                              "-ar", str(SR), "-ac", "2", "-i", "-",
                              "-c:a", "pcm_s16le", tmp], stdin=subprocess.PIPE)
        p.communicate(np.clip(out.T, -1.0, 1.0).astype(np.float32).tobytes())
        if p.returncode != 0 or not os.path.isfile(tmp):
            raise RuntimeError("写出分离结果失败")
        os.replace(tmp, dst)
        return dst
    finally:
        try:
            if os.path.isfile(raw):
                os.remove(raw)
        except Exception:  # noqa: BLE001
            pass


def extract_audio(video: str, dst: str) -> str:
    """从成片里抽一条 44.1k 立体声 wav（分离只认这个规格）。"""
    r = subprocess.run([core.ff("ffmpeg"), "-v", "error", "-y", "-i", video,
                        "-vn", "-ac", "2", "-ar", str(SR), "-c:a", "pcm_s16le", dst],
                       capture_output=True)
    if r.returncode != 0 or not os.path.isfile(dst):
        raise RuntimeError("抽取成片音轨失败：%s" % r.stderr.decode("utf-8", "ignore")[:200])
    return dst


def replace_audio(video: str, wav: str, dst: str) -> str:
    """视频流 copy，只换音频并重新归一到 -16 LUFS（分离后电平会掉）。"""
    r = subprocess.run([core.ff("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error",
                        "-y", "-fflags", "+genpts",
                        "-i", video, "-i", wav,
                        "-map", "0:v:0", "-map", "1:a:0",
                        "-c:v", "copy",
                        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                        "-ar", "48000", "-ac", "2", "-c:a", "aac", "-b:a", "192k",
                        "-map_metadata", "-1", "-movflags", "+faststart", "-shortest", dst],
                       capture_output=True)
    if r.returncode != 0 or not os.path.isfile(dst):
        raise RuntimeError("替换音轨失败：%s" % r.stderr.decode("utf-8", "ignore")[:200])
    return dst
