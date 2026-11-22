"""同镜头干净帧填充：「借用背景」模式。

为什么加这条路
--------------
原来的填充是**镜像**：从字幕带上下各取一条画面 vflip 回来，按行渐变贴上去。
静止、低频的背景（工装、墙面、桌面）看不出问题；但字幕压在「白衬衫 + 深色
领带」这类强对比竖结构上时，翻过来的块会偏亮，一眼看出被动过
（2026-11-12 用户对比第 14 秒截图发现）。

视频修复里最快的 STTN 那一类，思路其实不是「算像素」，而是**去别的帧里抄**：
同一镜头里总有这一块没被字挡住的时候，那几个像素就是真的背景。
我们比它多一个别人没有的条件——**每一帧每一块有没有字，已经全知道**
（subdetect 的逐句包围盒）。所以不需要光流、不需要模型，只维护一张
「每个像素最近一次没被字遮住时的值」，逐帧覆盖就完事：

    clean[p] = frame[p]   当 p 当前没字
    out[p]   = clean[p]   当 p 当前有字

这是「时序背景重建」最简形式：纯 numpy，一趟解码一趟编码，零模型。

三条边界
--------
1. **必须同镜头**。镜头切了，「最近一次干净」就是上一个镜头的像素，
   抄过来会是一块毫无关系的画面。所以做场景切换检测，切镜的那一瞬间
   若正在出字（切完立刻说话，短剧里很常见），那一句退回**镜像填充**。
2. **只处理字幕带那一条**。解码时就 crop 成带区，带区外的像素一个字节
   都不经过 Python——1080×1920 全帧过一遍要 11GB 管道流量，带区只要 1.6GB。
3. **失败必须能退回**。调用方传 force=False 时先试这个模式，抛异常就
   回落原来的纯滤镜路径，不会让一集彻底失败。

代价
----
一集 60 秒多花 20-40 秒（解码 + Python 逐帧 + 编码，大约原来的 2 倍多），
所以做成开关，默认仍走镜像。
"""

import os
import subprocess
import threading
import time

import numpy as np

from . import core, render

# 羽化半径随画面宽度走：1080 宽 → 默认 18px。
# 比镜像那条路（gblur=sigma=8）宽得多，是故意的：抄回来的背景即使只差几个
# 像素，硬边界也会让眼睛立刻盯上那块；把过渡做宽，剩下的偏差就只剩「这块
# 稍微有点虚」，不对比根本不注意。
_BLUR_PER_W = 1.0 / 60.0

# 场景切换：带区灰度平均绝对差。缓推/走动一般 < 5，切镜通常 > 20
_CUT_DIFF = 12.0

# 「最近一次干净值」什么时候不能信——这是这条路唯一的真短板：
# 字幕把某块遮住 2 秒，那 2 秒里背景若在动，抄回来的就是 2 秒前的位置，
# 叠在现在的画面上就是一条错位的边（实测第 1 集 t=40：字幕压在正在下滑的
# 红条纹上，红条纹被擦出一道暗带、「倒」字留了浅影）。三道闸：
#   ① 刚遮住的 0.25 秒一定可信（错位量还不到 1px）；
#   ② 遮久了就看「字出现前这块在不在动」——move 是逐像素灰度差的 EMA，
#      只在没被字遮的帧更新（否则出字本身就会被当成运动，整句全废）；
#   ③ 最长 15 秒，超过就无条件不信 —— 只是防病态情况（视频卡住、静止封面）。
#      **不能设成「一句的时长」那种量级**：字幕带正中间那几行几乎全程被字盖着，
#      实测第 1 集有一批像素连续 391 帧（13 秒）没露出来过，用 2.5 秒当上限
#      会让整片恒定判成不可信、100% 走镜像——功能等于没开（踩过）。
_AGE_TRUST_S = 0.25
_AGE_MAX_S = 15.0
_MOVE_DIFF = 9.0


def _fps(path: str) -> float:
    """取视频平均帧率。解码端与编码端必须用同一个值，否则音画会整体漂。"""
    try:
        out = subprocess.run(
            [core.ff("ffprobe"), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=avg_frame_rate,r_frame_rate",
             "-of", "default=nw=1", path],
            capture_output=True, text=True, timeout=30, **core.POPEN_HIDE).stdout
    except (OSError, subprocess.SubprocessError):
        return 30.0
    vals = {}
    for line in (out or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = vals.get(key) or ""
        if "/" in raw:
            a, b = raw.split("/", 1)
            try:
                a, b = float(a), float(b)
                if b > 0 and 1.0 <= a / b <= 240.0:
                    return a / b
            except ValueError:
                continue
        try:
            f = float(raw)
            if 1.0 <= f <= 240.0:
                return f
        except ValueError:
            continue
    return 30.0


def _blur(m: np.ndarray, r: int) -> np.ndarray:
    """盒式模糊（积分图，两遍近似高斯）。m 是 0..1 的 float32 单通道。"""
    if r < 1:
        return m
    k = 2 * r + 1
    a = m
    for axis in (1, 0):
        pad = [(0, 0), (0, 0)]
        pad[axis] = (r + 1, r)
        # mode="edge" 不是可选项：工作区上下沿就是矩形边，用默认的补零会让
        # 边上的 alpha 被稀释，抹到边缘就抹不干净（留一条淡淡的字底）。
        c = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis)
        sl = [slice(None), slice(None)]
        sl[axis] = slice(k, None)
        c2 = c[tuple(sl)]
        sl2 = [slice(None), slice(None)]
        sl2[axis] = slice(0, -k)
        a = (c2 - c[tuple(sl2)]) / float(k)
    return a.astype(np.float32)


def _mirror(sub: np.ndarray, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
    """带区内的上下镜像填充（和 render._sub_patch 同一套做法，这里是 numpy 版）。

    只给「背景还没学到」或「刚切镜」的像素兜底用：正常情况一块都用不到，
    但刚好切镜后立刻出字时，它比抄上一个镜头的像素强得多。
    """
    band = sub[:, x0:x1]
    h = y1 - y0
    if h <= 0:
        return np.zeros((0, x1 - x0, 3), np.float32)
    up_ok, dn_ok = y0 - h >= 0, y1 + h <= band.shape[0]
    if up_ok and dn_ok:
        up = band[y0 - h:y0][::-1].astype(np.float32)
        dn = band[y1:y1 + h][::-1].astype(np.float32)
        w = np.clip((np.arange(h, dtype=np.float32) / h - 0.45) / 0.30, 0.0, 1.0)
        w = w[:, None, None]
        return up * (1.0 - w) + dn * w
    if up_ok:
        return band[y0 - h:y0][::-1].astype(np.float32)
    if dn_ok:
        return band[y1:y1 + h][::-1].astype(np.float32)
    return band[y0:y1].astype(np.float32)


def _band_rect(spans: list, w: int, h: int, pad_x: int) -> tuple:
    """要处理的带区（像素坐标，偶数对齐）。返回 (x0, y0, bw, bh)。"""
    xs0 = min(float(s["l"]) for s in spans)
    xs1 = max(float(s["r"]) for s in spans)
    ys0 = min(float(s["t"]) for s in spans)
    ys1 = max(float(s["b"]) for s in spans)
    rh = max(0.008, ys1 - ys0)
    # 上下各留出「一整块矩形高度」以上：镜像兜底要在这里取源，
    # 羽化半径也要有余量，否则边界上会出现一条没被处理的硬边。
    pad_y = int(round(rh * h * 1.15)) + 16
    y0 = max(0, int(round(ys0 * h)) - pad_y)
    y1 = min(h, int(round(ys1 * h)) + pad_y)
    x0 = max(0, int(round(xs0 * w)) - pad_x)
    x1 = min(w, int(round(xs1 * w)) + pad_x)
    bw, bh = x1 - x0, y1 - y0
    bw -= bw % 2
    bh -= bh % 2
    return x0, y0, bw, bh


def encode(src: str, dst: str, cfg: dict, spans: list, sub_tail: str,
           duration: float, on_pct=None) -> None:
    """用「同镜头干净帧」替换字幕区域并编码，输出 dst。

    sub_tail 是接在后面的字幕滤镜（subtitles=…），没有就传空串。
    任何一步出错都抛 RuntimeError，调用方据此回落到镜像路径。
    """
    if not spans:
        raise RuntimeError("这一集没有逐句框，借用背景模式用不了")

    info = core.ffprobe(src)
    w, h = int(info.get("width") or 0), int(info.get("height") or 0)
    if w <= 0 or h <= 0:
        raise RuntimeError("读不到视频尺寸")
    fps = _fps(src)
    dur = float(duration or info.get("duration") or 0) or 1.0

    try:
        soft = min(1.0, max(0.0, float(cfg.get("soft") or 0.5)))
    except (TypeError, ValueError):
        soft = 0.5
    r = max(2, int(round(w * _BLUR_PER_W * (0.6 + 0.8 * soft))))
    x0, y0, bw, bh = _band_rect(spans, w, h, pad_x=max(12, r * 2))

    # 每句在**带区局部坐标**里的矩形 + 时间窗，逐帧直接查
    rects = []
    for s in spans:
        try:
            l, rr = float(s["l"]), float(s["r"])
            tp, bt = float(s["t"]), float(s["b"])
            t0, t1 = float(s["t0"]), float(s["t1"])
        except (KeyError, TypeError, ValueError):
            continue
        if rr - l <= 0.001 or bt - tp <= 0.001 or t1 - t0 <= 0.02:
            continue
        rx0 = max(0, int(round(l * w)) - x0)
        rx1 = min(bw, int(round(rr * w)) - x0)
        ry0 = max(0, int(round(tp * h)) - y0)
        ry1 = min(bh, int(round(bt * h)) - y0)
        if rx1 - rx0 < 2 or ry1 - ry0 < 2:
            continue
        rects.append((t0 - 0.05, t1 + 0.05, rx0, rx1, ry0, ry1))
    if not rects:
        raise RuntimeError("逐句框都在带区外，借用背景模式用不了")
    # 所有矩形的整体上下沿：镜像兜底一次算到这里，不必逐帧重新求
    ry_all0 = min(r[4] for r in rects)
    ry_all1 = max(r[5] for r in rects)

    part = dst + ".part.mp4"
    # 不预删残留的 part：本机删除守卫会把「删一个正被占用的大文件」判成失败并
    # 抛出来（trash 操作被中止），而 ffmpeg 的 -y 本来就覆盖写。收尾的删除也都
    # 吞掉异常——清不掉最多留个临时文件，不能让一集因为清理失败而失败。
    errpath = part + ".err"
    # 解码：只出带区。这样管道流量只有全帧的 1/8 左右。
    derr = open(part + ".derr", "wb")
    dec = subprocess.Popen(
        [core.ff("ffmpeg"), "-nostdin", "-v", "error", "-i", src, "-an", "-sn", "-dn",
         "-vf", f"crop={bw}:{bh}:{x0}:{y0},format=rgb24",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, stderr=derr, **core.POPEN_HIDE)
    core.register_proc(dec)       # 中止时由 core 直接杀掉整条管道
    enc = None
    sub = f"{sub_tail}," if sub_tail else ""
    graph = (f"[1:v][0:v]overlay={x0}:{y0}:format=auto[__cv];"
             f"[__cv]{sub}format=yuv420p[vout]")
    base = [core.ff("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{bw}x{bh}",
            "-r", f"{fps:.6f}", "-i", "-",
            "-i", src,
            "-filter_complex", graph, "-map", "[vout]", "-map", "1:a?", "-sn", "-dn"]
    # 只用一组参数：这条路上重试要重跑整条管道（解码已读完），成本比
    # 「抛错让上层回落镜像路径」高得多——回落那一步本来就是完整可用的。
    vargs = list(render._venc_args()) + ["-c:a", "copy"]

    errf = open(errpath, "wb")
    try:
        enc = subprocess.Popen(base + vargs + ["-movflags", "+faststart", part],
                               stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                               stderr=errf, **core.POPEN_HIDE)
        core.register_proc(enc)
        # 管道看门狗（2026-11-22，render._run 同款思路的变体）：这条路是 rawvideo
        # 管道，没有 -progress 行可当心跳，**产物文件涨没涨**就是唯一的心跳——
        # dec 断粮（坏盘/坏索引卡在读入）或 enc 假死，part 都会停止增长，一条
        # 观测兜住整条管道。读线程的 readinto/写线程的 write 没有超时可设，
        # 唯一解法是从外面杀进程让它们立刻解除阻塞，所以超阈值就双杀。
        # 收尾的 faststart 重排只动 moov（几秒），120 秒阈值不会被它误杀。
        # 阈值走 core.stall_sec()（设置页/环境变量统一解析，R0.1）。
        _stall = core.stall_sec()
        stall = {"hit": False}
        if _stall > 0:
            def _watch_pipe() -> None:
                last_sig = None
                last_move = time.monotonic()
                while dec.poll() is None or enc.poll() is None:
                    sig = None
                    try:
                        if os.path.exists(part):
                            st = os.stat(part)
                            sig = (st.st_size, st.st_mtime_ns)
                    except OSError:
                        pass
                    if sig != last_sig:
                        last_sig = sig
                        last_move = time.monotonic()
                    elif time.monotonic() - last_move > _stall:
                        stall["hit"] = True
                        for pr in (dec, enc):
                            try:
                                if pr.poll() is None:
                                    pr.kill()
                            except Exception:  # noqa: BLE001
                                pass
                        return
                    time.sleep(2.0)

            threading.Thread(target=_watch_pipe, daemon=True).start()
        nbytes = bw * bh * 3
        # 工作区 = 所有矩形的整体上下沿。带区其余部分（上下那两截镜像源）
        # 永远不需要改，直接透传。只算这几十行比整块带区快 3-4 倍——
        # 这条路能不能做到「一集一分半」全看这个。
        w0, w1 = ry_all0, ry_all1
        wh = w1 - w0
        rects_w = [(a, b, x, xx, y - w0, yy - w0)
                   for (a, b, x, xx, y, yy) in rects]
        clean = None      # 每个像素最近一次「没被字遮住」时的颜色
        stale = None      # True = 这块背景不可信，本帧走镜像
        age = None        # 这块被连续遮了多少帧
        move = None       # 逐像素灰度差 EMA（只在没被遮的帧更新）
        prev_gray = None
        age_trust = max(4, int(round(fps * _AGE_TRUST_S)))
        age_max = max(age_trust + 4, int(round(fps * _AGE_MAX_S)))
        buf = bytearray(nbytes)
        view = memoryview(buf)
        n = 0
        while True:
            got = 0
            while got < nbytes:
                k = dec.stdout.readinto(view[got:])
                if not k:
                    break
                got += k
            if got < nbytes:
                break
            frame = np.frombuffer(buf, dtype=np.uint8).reshape(bh, bw, 3)
            t = n / fps
            work = frame[w0:w1]

            hard = np.zeros((wh, bw), dtype=bool)
            for t0, t1, rx0, rx1, ry0, ry1 in rects_w:
                if t0 <= t <= t1:
                    hard[ry0:ry1, rx0:rx1] = True

            gray = work[:, :, 1].astype(np.int16)
            cut = False
            diff = None
            if prev_gray is not None:
                d = np.abs(gray - prev_gray)
                cut = float(d.mean()) > _CUT_DIFF
                diff = d.astype(np.float32)
            prev_gray = gray

            if clean is None or clean.shape != work.shape:
                clean = work.copy()
                stale = np.ones((wh, bw), dtype=bool)
                age = np.zeros((wh, bw), dtype=np.int32)
                move = np.zeros((wh, bw), dtype=np.float32)
            if cut:
                # 镜头换了：旧「最近一次干净」属于上一个场景，必须作废。
                # **不能只把 stale 置 True**——下面 `stale = hard & ~ok` 会把
                # ok 为真的像素重新判回可信，等于当场撤销（第一版就是这么
                # 错的：第 1 集 t=14 那块一直填着上一个镜头的深蓝西裤，而当时
                # 画面是白衬衫，亮度差 55）。用 age 表达「这块还没在新镜头里
                # 露过面」，等它变 free 一次（age 归零）才恢复可信。
                age[:] = age_max + 1
                move[:] = 0.0

            if not hard.any():
                # 整块都没字：直接把这一帧当背景，全部可信
                clean[:] = work
                stale[:] = False
                age[:] = 0
                if diff is not None:
                    move *= 0.7
                    move += diff * 0.3
                out = frame
            else:
                free = ~hard
                clean[free] = work[free]
                age[free] = 0
                age[hard] += 1
                if diff is not None:
                    # **只更新没被字遮的像素**：出字本身会让差值爆掉，
                    # 如果连遮住的像素一起更新，每一句开头都会判成「在动」，
                    # 整个功能就退化成镜像了（第一版就是这么废的）。
                    move[free] = move[free] * 0.7 + diff[free] * 0.3
                # 刚遮住的一定可信；遮久了看「字出现前这块在不在动」。
                # 这个式子整体重算 stale（不是就地修改）——切镜时置进去的
                # 「age 超大」会在这里自然判成不可信，不会再被 undo。
                ok = (age <= age_trust) | ((age <= age_max) & (move <= _MOVE_DIFF))
                stale = hard & ~ok
                # alpha：**矩形内恒为 1**，只在矩形外羽化。不能直接用模糊值——
                # 半径 18 的模糊会把字贴上沿那几行也稀释到 0.7 左右，原字幕的
                # 笔画上下缘就半透明地留在下面，整句看着像一排浅虚线（实测过）。
                # 矩形本身已经含了按字高外扩的 pad，里面不需要再羽化。
                hf = hard.astype(np.float32)
                m = np.maximum(hf, _blur(hf, r))
                a = m[:, :, None]
                if stale.any():
                    # 不可信的地方（背景在动 / 切镜后立刻出字）→ 上下镜像兜底
                    fill = clean.astype(np.float32)
                    mir = _mirror(frame, w0, w1, 0, bw)
                    fill = np.where(stale[:, :, None], mir, fill)
                else:
                    fill = clean.astype(np.float32)
                blend = (work.astype(np.float32) * (1.0 - a) + fill * a)
                out = frame.copy()
                out[w0:w1] = blend.round().clip(0, 255)

            try:
                enc.stdin.write(np.ascontiguousarray(out, dtype=np.uint8).tobytes())
            except (BrokenPipeError, OSError):
                break
            n += 1
            if n % 15 == 0:
                core.check_cancel()      # 逐帧长循环里的中止检查点
                if on_pct and dur > 0:
                    on_pct(min(99.0, (n / fps) / dur * 100.0))
        try:
            enc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        core.wait_cancelable(dec, 120)
        core.wait_cancelable(enc, 600)
        errf.close()
        if stall["hit"]:
            raise RuntimeError(
                f"这一集处理卡死（超过 {core.stall_sec()} 秒没有任何进展），已自动终止；"
                "直接重试通常能过，反复出现请带日志反馈")
        if dec.returncode not in (0, None):
            raise RuntimeError("读取这一集失败，请重试")
        if enc.returncode != 0 or not os.path.isfile(part) \
                or os.path.getsize(part) < 1024:
            raise RuntimeError("这一集处理失败，请重试")
        os.replace(part, dst)
        # 成功就把错误日志清掉（失败时留着，排查要用）
        try:
            os.remove(errpath)
        except Exception:  # noqa: BLE001
            pass
        if on_pct:
            on_pct(100.0)
    finally:
        core.unregister_proc(dec)
        if enc is not None:
            core.unregister_proc(enc)
        try:
            errf.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if dec.stdout:
                dec.stdout.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            derr.close()
        except Exception:  # noqa: BLE001
            pass
        for p in (dec, enc):
            if p is None:
                continue
            try:
                if p.poll() is None:
                    p.kill()
            except Exception:  # noqa: BLE001
                pass
        for f in (part + ".derr",):
            try:
                if os.path.exists(f):
                    os.remove(f)
            except Exception:  # noqa: BLE001 —— 清理失败不影响交付
                pass


def _tail(path: str, n: int = 400) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - n))
            return f.read().decode("utf-8", "replace").strip()
    except OSError:
        return "未知原因"
