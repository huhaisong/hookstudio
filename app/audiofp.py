"""成片音频内容指纹（多号铺量查重用，2026-11-17）。

借鉴短视频工厂 fingerprint.py 的思路，但按我们的场景做了简化：

- 只解音频、不解画面：ffmpeg → 11025Hz 单声道，几百 MB 的片也只要一两秒。
- 16 个对数频带能量 → 每个时间片编成 32 bit：
    前 16 bit = 各频带相对上一帧是升还是降（频率轴动态）；
    后 16 bit = 各频带相对全片均值是高还是低（频谱形状，抗音量归一化）。
- 相似度 = 1 − 汉明距离 / 32；短序列在长序列上滑窗对齐，能扛掐头去尾。

设计取舍：
- 零新依赖，只用现成 ffmpeg + numpy（numpy 可选，缺了就返回空指纹、不影响发布）。
- **两档粒度**（2026-11-17 补段级定位）：
    · 整片级 `similarity()`：还在用，回答「是不是同一条」，快，用于挑候选；
    · 段级 `locate_files()`：回答「撞了哪几秒」，**按需**读文件重算能量形状再比。
  为什么必须补段级：我们的成片是**同一批素材块拼出来的**，两套共用 60% 块时
  整片相似度会被另外 40% 的不同内容稀释到 0.62 左右，低于 MIN_SIM 判不出来；
  而平台侧的判重恰恰是按段比的。段级才是我们的主场景。
- 为什么定位不复用存下来的指纹：那串 32bit 是**符号位**（升/降），对重编码、
  重采样极敏感（实测同源转一次码只剩 0.77~0.84），且长片抽帧后复用起点不是步长
  整数倍时采样相位整体错开，逐窗比对会全对不上。所以定位单独用 16 频带能量的
  形状向量（实测同源 0.83~1.0、异源 ~0.2），只在已怀疑同源的两条片之间现算。
- 指纹 v2 在 base64 头里带 4 字节 stride（长片抽帧步长）：没有它，抽过帧的指纹
  换算不回秒数，`locate()` 这条兜底路径会错位。旧指纹（无头）按 stride=1 读，
  兼容历史台账；重算一次就自动升级到 v2。
- 失败一律返回空串、相似度返回 0：查重是提示，绝不能因为它把发布卡住。
"""
from __future__ import annotations

import base64
import math
import os
import struct
import subprocess

import numpy as np

from . import core

SR = 11025          # 采样率：比对方 11025 一致，够覆盖人声与背景乐主要能量
HOP = 1102          # 每帧 0.1s
NBANDS = 16
MAX_FRAMES = 900    # 长片按步长抽样，控制指纹长度与计算量（90s 等效）
MIN_SIM = 0.85      # 判定「同一条」的阈值，自测时同源 >0.9、异源 <0.7

# ── 段级定位（2026-11-17）────────────────────────────────────────────────
# 窗口参数按「一条钩子/一场戏」的量级取：6 秒窗、3 秒步（50% 重叠）。
# 步长必须小于窗长：撞车段正好跨窗边界时，平铺无重叠会把它切成两半、两边都不够线。
WIN_SEC = 6.0
WIN_STEP = 3.0
WIN_TOL = 0.72       # 窗口级命中线（未标定：先用同源/异源各 10 组标一次再调）
MIN_SPAN_SEC = 1.5   # 短于这么长的命中当噪声丢掉
NEAR_SIM = 0.60      # 整片「值得再做段级定位」的门槛（实测共用 60% 块 ≈0.62）
MIN_RATIO = 0.10     # 段级重叠占比到这条线就提示（10%，未标定）

# ── 按需定位用的能量形状（2026-11-17 实测后加的）────────────────────────
# 存下来的 32bit 指纹是**符号位**（升/降），对重编码、重采样非常敏感：
# 同一条音频转一次码，整片相似度只剩 0.77~0.84（还低于 MIN_SIM），做定位会整段对不上。
# 换成 16 频带对数能量的「形状向量」（每帧去均值 + L2 归一）后，实测：
#   同源重编码 0.83~1.0；不同内容 ~0.2 —— 分离度足够做定位。
# 这套只在**已怀疑同源**的两条片之间按需算（不落盘、不进队列），5 分钟片约 0.4s。
E_WIN = 2048         # 0.186s 窗，比指纹的 0.1s 帧长，抗解码相位差
E_HOP = 2756         # 0.25s 步
E_WIN_SEC = 6.0
E_STEP_SEC = 3.0
E_TOL = 0.70         # 帧/窗的余弦命中线（实测同源 0.83+、异源 0.2，余量很大）

_MAGIC = b"AF2\x00"  # v2 头：4 字节魔数 + 4 字节 stride（小端）


def _bands(frame: np.ndarray, edges: np.ndarray, freqs: np.ndarray) -> np.ndarray:
    spec = np.abs(np.fft.rfft(frame * np.hanning(len(frame))))
    out = np.empty(NBANDS, dtype=np.float64)
    for i in range(NBANDS):
        m = (freqs >= edges[i]) & (freqs < edges[i + 1])
        out[i] = float(spec[m].mean()) if m.any() else 0.0
    return out


def _extract(path: str) -> tuple[np.ndarray, int] | None:
    """解音频 → (每帧 32bit 的数组, 抽帧步长)。失败返回 None。

    stride 是抽帧步长：90s 以内是 1，超过就隔 stride 帧取一帧。
    它必须跟指纹一起存：光看数组长度反推不出来，而段级定位要靠它把
    帧下标换算回秒。
    """
    try:
        if not path or not os.path.isfile(path):
            return None
        cmd = [core.ff("ffmpeg"), "-v", "error", "-nostdin", "-i", path,
               "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"]
        raw = subprocess.run(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=120,
                             **core.POPEN_HIDE).stdout
        x = np.frombuffer(raw, dtype="<i2")
        if x.size < HOP * 8:
            return None
        x = x.astype(np.float64) / 32768.0
        n = x.size // HOP
        stride = max(1, math.ceil(n / MAX_FRAMES))

        edges = np.geomspace(60.0, 5000.0, NBANDS + 1)
        freqs = np.fft.rfftfreq(HOP, 1.0 / SR)
        frames = [x[i * HOP:(i + 1) * HOP] for i in range(0, n, stride)]
        bands = np.array([_bands(f, edges, freqs) for f in frames])
        if len(bands) < 8:
            return None
        # 后 16 bit 用「本帧各频带 vs 全片该频带均值」，对整体增益变化稳健
        mean = bands.mean(axis=0)
        prev = np.zeros(NBANDS, dtype=bool)
        words = []
        for row in bands:
            freq_up = row > prev
            time_up = row > mean
            bits = np.concatenate([freq_up, time_up])
            packed = np.packbits(bits)
            words.append(int.from_bytes(packed.tobytes(), "little"))
            prev = row  # 频率升降用相邻帧的绝对能量比，别拿"相对均值"的布尔串下去
        return np.array(words, dtype="<u4"), stride
    except Exception:
        return None


def compute(path: str) -> str:
    """算一条视频的音频指纹，返回 base64 字符串；失败返回空串。"""
    got = _extract(path)
    if not got:
        return ""
    arr, stride = got
    blob = _MAGIC + struct.pack("<I", int(stride)) + arr.tobytes()
    return base64.b64encode(blob).decode("ascii")


def frames(path: str) -> tuple[np.ndarray, int]:
    """原始帧数组 + 步长（调试/自测用）。失败返回 (空, 1)。"""
    got = _extract(path)
    if not got:
        return np.empty(0, dtype=np.uint32), 1
    return got


def _decode(fp: str) -> tuple[np.ndarray, int]:
    """base64 → (帧数组, 步长)。v1（无头）按 stride=1 读，历史台账不断。"""
    if not fp:
        return np.empty(0, dtype=np.uint32), 1
    try:
        raw = base64.b64decode(fp)
        if raw[:4] == _MAGIC and len(raw) >= 8:
            stride = int(struct.unpack("<I", raw[4:8])[0]) or 1
            body = raw[8:]
        else:
            stride, body = 1, raw
        body = body[:len(body) // 4 * 4]
        return np.frombuffer(body, dtype="<u4"), stride
    except Exception:
        return np.empty(0, dtype=np.uint32), 1


def similarity(a: str, b: str) -> float:
    """两条指纹的相似度 0~1；短序列在长序列上滑窗对齐，扛掐头去尾。"""
    A, _ = _decode(a)
    B, _ = _decode(b)
    if A.size < 8 or B.size < 8:
        return 0.0
    short, long = (A, B) if A.size <= B.size else (B, A)
    ls, ll = short.size, long.size
    if ls / ll < 0.7:          # 长度差超过 30%，不是同一条（掐头去尾兜底）
        return 0.0
    best = 0.0
    steps = min(24, ll - ls + 1)
    offs = sorted(set(int(round(i * (ll - ls) / max(1, steps - 1)))
                      for i in range(steps))) if steps > 1 else [0]
    for off in offs:
        diff = np.bitwise_xor(short, long[off:off + ls])
        dist = int(np.bitwise_count(diff).sum())   # numpy 2.x 有 bitwise_count
        best = max(best, 1.0 - dist / (ls * 32.0))
    return round(best, 4)


def _locate_arrays(A: np.ndarray, sa: int, B: np.ndarray, sb: int,
                   win_sec: float = WIN_SEC, step_sec: float = WIN_STEP,
                   tol: float = WIN_TOL) -> dict:
    """段级定位的内核：直接吃帧数组（便于用全帧率重算后再比）。

    做法（不抄竞品的 DP + RANSAC，按我们的场景降一档）：
      ① 6s 窗 / 3s 步，逐窗算 32bit 汉明相似度；
      ② 每个 A 窗取 B 里最像的一窗，过线就把 A 的 [i, i+win) 记成命中；
      ③ 命中的窗按时间并成区间（窗本身 50% 重叠，天然连得上）；
      ④ 区间两头逐帧修边：窗口跨在边界上时会有半窗命中，不修边会虚报 1-2 秒；
      ⑤ ratio = 命中秒数 / min(两片总时长)。
    为什么不做对角 DP：段级定位只需要指出「哪几秒撞」，不需要拟合变速斜率。
    我们自己的微变速上限 1.2%，跨 6 秒窗只漂 72ms，比一帧（100ms）还小，
    逐窗直接比不会错位；DP/RANSAC 是给「容忍 ±20% 变速」的场景用的，白花算力。

    返回的 spans 里 a/b 都是秒，score 是这一段的平均窗口相似度。
    """
    empty = {"ratio": 0.0, "overlapSeconds": 0.0, "spans": [], "method": "window"}
    if A.size < 8 or B.size < 8:
        return empty

    fd_a = HOP / SR * sa          # 每帧代表多少秒
    fd_b = HOP / SR * sb
    wa = max(4, int(round(win_sec / fd_a)))
    wb = max(4, int(round(win_sec / fd_b)))
    if A.size < wa or B.size < wb:
        return empty
    n = min(wa, wb)               # 两边步长不同时，按较短窗比
    step_a = max(1, int(round(step_sec / fd_a)))
    step_b = max(1, int(round(step_sec / fd_b)))

    hits: list[tuple[int, int, float, int]] = []
    for i in range(0, A.size - wa + 1, step_a):
        head = A[i:i + n]
        best_s, best_j = 0.0, -1
        for j in range(0, B.size - wb + 1, step_b):
            diff = np.bitwise_xor(head, B[j:j + n])
            s = 1.0 - float(np.bitwise_count(diff).sum()) / (n * 32.0)
            if s > best_s:
                best_s, best_j = s, j
        if best_s >= tol and best_j >= 0:
            hits.append((i, best_j, best_s, n))

    if not hits:
        return empty

    # 命中的 A 窗并成区间：按起点排序，重叠/相接就合并
    hits.sort(key=lambda h: h[0])
    spans: list[dict] = []
    cur: dict | None = None
    for i, j, s, width in hits:
        if cur and i <= cur["_end"]:
            cur["_end"] = max(cur["_end"], i + width)
            cur["_b_end"] = max(cur["_b_end"], j + width)
            cur["_scores"].append(s)
        else:
            if cur:
                spans.append(cur)
            cur = {"_start": i, "_end": i + width, "_b": j,
                   "_b_end": j + width, "_scores": [s]}
    if cur:
        spans.append(cur)

    out: list[dict] = []
    total = 0.0
    for sp in spans:
        i0, i1, jb = sp["_start"], sp["_end"], sp["_b"]
        # ④ 逐帧修边：窗口以 3s 步进，边界窗里可能只有一半是真重叠。按
        # a[i] ↔ b[jb + (i-i0)*fd_a/fd_b] 对齐逐帧看，命中线以下就切掉。
        # 最多各修一个窗长：单帧可能有噪声，不能让修边把整段吃光。
        limit = wa
        trimmed = 0
        while i0 < i1 and trimmed < limit:
            j = jb + int(round((i0 - sp["_start"]) * fd_a / fd_b))
            if j >= B.size:
                break
            if 1.0 - float(np.bitwise_count(A[i0] ^ B[j])) / 32.0 < tol:
                i0 += 1
                trimmed += 1
            else:
                break
        trimmed = 0
        while i1 > i0 and trimmed < limit:
            i = i1 - 1
            j = jb + int(round((i - sp["_start"]) * fd_a / fd_b))
            if j >= B.size:
                i1 -= 1
                trimmed += 1
                continue
            if 1.0 - float(np.bitwise_count(A[i] ^ B[j])) / 32.0 < tol:
                i1 -= 1
                trimmed += 1
            else:
                break
        sec = (i1 - i0) * fd_a
        if sec < MIN_SPAN_SEC:
            continue
        j0 = jb + int(round((i0 - sp["_start"]) * fd_a / fd_b))
        j1 = jb + int(round((i1 - sp["_start"]) * fd_a / fd_b))
        total += sec
        out.append({
            "a": [round(i0 * fd_a, 2), round(i1 * fd_a, 2)],
            "b": [round(max(0, j0) * fd_b, 2), round(max(0, j1) * fd_b, 2)],
            "score": round(sum(sp["_scores"]) / len(sp["_scores"]), 4),
        })
    dur_a = A.size * fd_a
    dur_b = B.size * fd_b
    ratio = total / max(1e-6, min(dur_a, dur_b))
    return {
        "ratio": round(min(1.0, ratio), 4),
        "overlapSeconds": round(total, 2),
        "spans": out,
        "method": "window",
    }


def locate(a: str, b: str, win_sec: float = WIN_SEC,
           step_sec: float = WIN_STEP, tol: float = WIN_TOL) -> dict:
    """段级定位（吃已存指纹）。两条都是抽帧指纹时结果可能为空——见 locate_files。"""
    A, sa = _decode(a)
    B, sb = _decode(b)
    return _locate_arrays(A, sa, B, sb, win_sec, step_sec, tol)


def _energy(path: str) -> np.ndarray | None:
    """16 频带对数能量的逐帧形状向量（去均值 + L2 归一）。失败返回 None。

    归一化做两件事：去均值去掉整体增益/EQ 倾斜，L2 归一让余弦只看「形状」。
    这样同一段音频即使换码率、换容器、音量差几 dB，形状仍然对得上。
    """
    try:
        if not path or not os.path.isfile(path):
            return None
        cmd = [core.ff("ffmpeg"), "-v", "error", "-nostdin", "-i", path,
               "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"]
        raw = subprocess.run(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=120,
                             **core.POPEN_HIDE).stdout
        x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
        if x.size < E_WIN * 2:
            return None
        edges = np.geomspace(60.0, 5000.0, NBANDS + 1)
        freqs = np.fft.rfftfreq(E_WIN, 1.0 / SR)
        masks = [(freqs >= edges[b]) & (freqs < edges[b + 1]) for b in range(NBANDS)]
        han = np.hanning(E_WIN)
        rows = []
        for i in range(0, x.size - E_WIN + 1, E_HOP):
            spec = np.abs(np.fft.rfft(x[i:i + E_WIN] * han))
            row = np.array([math.log1p(float(spec[m].mean())) if m.any() else 0.0
                            for m in masks])
            row -= row.mean()
            n = float(np.linalg.norm(row))
            rows.append(row / n if n > 1e-9 else row)
        return np.array(rows) if rows else None
    except Exception:
        return None


def _window_starts(n: int, w: int, step: int) -> list[int]:
    """窗口起点：等步进 + 必带尾窗；短片自动加密。

    只按等步进排窗口时，8 秒的片子只有一个 0–6s 的窗，最后 2 秒永远覆盖不到，
    ratio 会凭空少一截（实测 0.75）。补一个 n-w 的尾窗就能盖满；
    片长不足 4 个窗时把步长也收小，否则短片的命中窗太少、一半落在边界上。
    """
    if n < w:
        return []
    if n > w:
        step = max(1, min(step, max(1, (n - w) // 8)))
    out = list(range(0, n - w + 1, step))
    if out and out[-1] != n - w:
        out.append(n - w)
    return out


def _locate_energy(MA: np.ndarray, MB: np.ndarray) -> dict:
    """两条能量形状矩阵的段级重叠定位。"""
    empty = {"ratio": 0.0, "overlapSeconds": 0.0, "spans": [], "best": 0.0,
             "method": "energy"}
    fd = E_HOP / float(SR)
    w = max(4, int(round(E_WIN_SEC / fd)))
    step = max(1, int(round(E_STEP_SEC / fd)))
    if MA.shape[0] < w or MB.shape[0] < w:
        return empty

    ai = _window_starts(MA.shape[0], w, step)
    bj = _window_starts(MB.shape[0], w, step)
    if not ai or not bj:
        return empty
    # 先整块做矩阵乘：Wa·Wbᵀ/w 就是每对窗口的平均逐帧余弦，比 Python 双循环快两个量级
    Wa = np.stack([MA[i:i + w].ravel() for i in ai])
    Wb = np.stack([MB[j:j + w].ravel() for j in bj])
    S = Wa @ Wb.T / float(w)
    best_idx = S.argmax(axis=1)
    best_val = S.max(axis=1)

    hits: list[tuple[int, int, float]] = []
    for k, i in enumerate(ai):
        if best_val[k] >= E_TOL:
            hits.append((i, bj[int(best_idx[k])], float(best_val[k])))
    if not hits:
        empty["best"] = round(float(best_val.max()) if best_val.size else 0.0, 4)
        return empty

    # 合并相邻命中窗（窗 6s、步 3s，天然重叠）
    hits.sort(key=lambda h: h[0])
    spans: list[dict] = []
    cur: dict | None = None
    for i, j, s in hits:
        if cur and i <= cur["_end"]:
            cur["_end"] = max(cur["_end"], i + w)
            cur["_scores"].append(s)
        else:
            if cur:
                spans.append(cur)
            cur = {"_start": i, "_end": i + w, "_b": j, "_scores": [s]}
    if cur:
        spans.append(cur)

    out: list[dict] = []
    total = 0.0
    limit = w
    for sp in spans:
        i0, i1, jb = sp["_start"], sp["_end"], sp["_b"]
        # 逐帧修边：窗口跨在重叠边界上时会带半窗命中，不修会多报 1-2 秒。
        # 单帧余弦可能被噪声影响，最多各修一个窗长。
        cut = 0
        while i0 < i1 and cut < limit:
            j = jb + (i0 - sp["_start"])
            if j < 0 or j >= MB.shape[0] or float(MA[i0] @ MB[j]) < E_TOL:
                i0 += 1
                cut += 1
            else:
                break
        cut = 0
        while i1 > i0 and cut < limit:
            j = jb + (i1 - 1 - sp["_start"])
            if j < 0 or j >= MB.shape[0] or float(MA[i1 - 1] @ MB[j]) < E_TOL:
                i1 -= 1
                cut += 1
            else:
                break
        sec = (i1 - i0) * fd
        if sec < MIN_SPAN_SEC:
            continue
        j0 = jb + (i0 - sp["_start"])
        j1 = jb + (i1 - sp["_start"])
        total += sec
        out.append({
            "a": [round(i0 * fd, 2), round(i1 * fd, 2)],
            "b": [round(max(0, j0) * fd, 2), round(max(0, j1) * fd, 2)],
            "score": round(sum(sp["_scores"]) / len(sp["_scores"]), 4),
        })
    dur_a = MA.shape[0] * fd
    dur_b = MB.shape[0] * fd
    return {
        "ratio": round(min(1.0, total / max(1e-6, min(dur_a, dur_b))), 4),
        "overlapSeconds": round(total, 2),
        "spans": out,
        "best": round(float(best_val.max()), 4),
        "method": "energy",
    }


def locate_files(path_a: str, path_b: str, win_sec: float = WIN_SEC,
                 step_sec: float = WIN_STEP, tol: float = WIN_TOL) -> dict:
    """段级定位（直接读文件，用能量形状按需算）。

    win_sec/step_sec/tol 只为兼容旧调用签名保留，实际用 E_* 常量。
    """
    MA = _energy(path_a)
    MB = _energy(path_b)
    if MA is None or MB is None:
        return {"ratio": 0.0, "overlapSeconds": 0.0, "spans": [],
                "best": 0.0, "method": "unavailable"}
    return _locate_energy(MA, MB)


def is_duplicate(a: str, b: str) -> tuple[bool, float]:
    s = similarity(a, b)
    return s >= MIN_SIM, s
