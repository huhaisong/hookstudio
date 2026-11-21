"""字幕区域自动识别（第 2 步用，不需要用户填位置）。

**为什么要有这个**：原来让用户自己拖「字幕在画面哪儿 / 那一条有多高」，
短剧的字幕位置并不统一（实测同一部剧里 78%、75%、86% 都见过），
用户拖动 30 次也不一定对；拖大了就把一条 10% 高的画面全抹了，
拖小了又会留半截白边。让机器先量出来，用户只在明显不对时微调。

**怎么量的**（只用 ffmpeg 抽帧 + numpy，不引入 OpenCV）：
  1. 均匀抽 16-30 帧，缩到 320 宽、转灰度（检测不需要原分辨率，慢在解码不在算）；
  2. 逐帧找「显著亮于邻域」的像素 `g - box_mean(g, r) > 38 且 g > 150`
     —— 字幕是白色细笔画，局部对比强；大块白墙/白衣的邻域也白，
     差值接近 0，所以不会被当成字（这是纯亮度阈值做不到的）；
  3. 横向累积：某个像素在多少比例的帧里被判成字。字幕只在有台词的时段出现，
     所以是「有时有、有时没有」，恒定出现的图案（台标）不会被选中；
  4. 按行投影找连续的行区间 —— 字幕是画面下半部的一条水平带；
  5. 同一部剧各集取中位数，个别集没有字幕/检测失败不影响整体。

**返回口径**跟 nosub 的配置一致：pos = 带上沿（含余量），size = 带高。
"""
from __future__ import annotations

import subprocess

from . import core

VERSION = 10
# 识别算法的版本号，进 nosub 的缓存键。改了判据就必须 bump，
# 否则老项目里「用旧算法量出来的位置」会被当成新鲜结果直接复用
# （v1 用 320 宽 + 邻域对比，v2 改成 640 宽 + 纯白&邻域双判据，位置差 8%；
#   v3 逐句框的上下界与整集文字带取并集——v2 的字顶/字底被密度阈值切掉，
#   抹完字还留半透明残影；
#   v4 由调用方按集传各自的位置带——v3 拿全剧那一条去套，多来源项目会错位，
#   框高从 2.8% 涨到 10.9%；
#   v5 行密度改为「优先跨帧平均、不够再退回第二高」——短片段项目（15 秒 3 句）
#   被空档摊薄到 0.0539，整集被判成没字幕；
#   v6 _frame_box 不再把「行段被画面运动打碎」当没字——碎段 + 高密度仍算一句，
#   否则掩码留洞、原字幕在成片里闪一下；
#   v7 _box_mean 按帧分块——长片+大分辨率时整摞建积分图要 2.44GiB 直接 OOM，
#   被上层吞掉后 by_ep 永远空、抹字退回固定位置（判据没变，但实现变了，
#   bump 强制有「部分集 OOM」的老项目全部重量一遍）。
#   v8 双行字幕：跨帧平均把只在少数帧出现的第二行摊薄到阈值下，band 只框一行、
#   逐句搜索窗下沿截断，成片第二行下半截抹不掉。改用逐行最大密度连通扩展出
#   双行带 + 搜索窗上沿按文字顶收敛（不按膨胀的 size，免得框进人物）；
#   白字掩膜也分块（900 帧整摞减法同样 2.44GiB）。
#   v9 修 v8 的过度扩展：连通门槛 0.03 太低，白衬衫/下巴在个别帧的 row_max
#   有 0.05-0.10，会顺着连续达标一路缝到 0.79（重生八零 ep02 band 高 0.22、
#   逐句框 0.36），镜像补出一块嘴。改成「只合并够强的字行段、允许行间小空档」：
#   强段门槛 max(0.12, 主行峰值×0.45)，两段间隔 <= 0.5 字高才并。真双行第二行
#   峰值 0.21+ 能并进来，白衬衫/下巴峰值 <0.10 进不来。合成测试两种都过。
#   v10 修 v9 漏掉的一类：字幕空档帧，搜索窗下沿按膨胀后的 size 开到 0.82，
#   罩住浅色桌面/手（白月光 ep36 25s），逐帧框把桌面反光当字顶到窗底，
#   经 pad+兜底放大到 0.856，整条下半屏被镜像抹掉。改：① 窗下沿按真实字高
#   只留 SPAN_DOWN_ROOM；② 单帧框优先只认 band 文字带核心区内的强段
#   （真字行密度 0.29+，桌面假段 0.03）；③ SPAN_PAD_Y 0.22→0.10。

DETECT_W = 640          # 检测分辨率宽。
# **不能降到 320**：白字缩小时被抗锯齿拉灰，纯白阈值直接失效；
# 640 上字幕行白像素密度还有 0.16-0.21，320 上只剩 0.01-0.02（等于没有）。
WHITE_MIN = 225         # 字幕白字的亮度下限（短剧字幕一律纯白，画面里的白衣白墙在 200 上下）
WHITE_DIFF = 18         # 还要显著亮于邻域：整块白墙/白衬衫的邻域也是白的，差值≈0
COVER_MAX = 0.55        # 一行里白像素超过这个比例 = 整块白（墙/衬衫），不像字
RATE_MIN = 0.06         # 某列至少这么多帧有白像素，才算「字幕占到这一列」
DENS_MIN = 0.05         # 一行的白像素密度下限（真字幕行 0.2-0.35，噪点行 <0.02）
# 只在画面下部找。原来从 50% 开始，实测把「白衬衫胸口」认成了字幕带（0.508-0.529），
# 真字幕在 0.76-0.87 —— 位置先验比任何阈值都管用，台标/顶部标题也一并避开。
# 62% 是留了余量：见过字幕压在下三分之一的剧，再高就属于特殊构图，交给手动指定。
Y_MIN, Y_MAX = 0.62, 0.98
MIN_TEXT_H = 0.013      # 一行字的「核心行」至少这么高；更矮的多半是反光亮边。
# 注意别调高：字幕上下沿的密度衰减很快，核心行往往只有字实际高度的 60%
# （曾设 0.022，结果 20 集里 6 集直接认不出），宽严要靠密度门槛管，不靠带高。
MAX_TEXT_H = 0.12       # 单行字超过画面 12% 高 → 不像是字幕，判为失败
PAD = 0.40              # 上下各留字高的 40% 做余量（描边、阴影、轻微抖动）

# ── 逐句包围盒（detect_spans）────────────────────────────────────────
# 字幕换句时宽度差一倍是常事（实测 13%–87%），高度也可能从一行变两行。
# 固定一个矩形去抹，必然有一半时间「抹多了」或「留白边」，所以按句量。
SPAN_FPS = 4.0          # 逐句检测抽帧频率（句均 1-3 秒，4fps 够抓住边界）
SPAN_MAX_FRAMES = 900   # 单集最多抽这么多帧，长片自动降频
SPAN_PAD_X = 0.55       # 左右各留「字高 × 0.55」的余量（描边、句末标点）
SPAN_PAD_Y = 0.10       # 上下各留「字高 × 0.10」（v10 从 0.22 收窄：0.22 时假段经 pad 再放大 0.086 个画面高）
SPAN_DOWN_ROOM = 0.60   # 搜索窗下沿 = text_bottom + 真实字高×此值（容双行第二行，不再用膨胀的 size×1.35）
SPAN_MIN_W = 0.035      # 比这还窄的检出（反光点、边角碎片）不是一句字幕
SPAN_MERGE = 0.020      # 相邻帧四边差异都小于此 → 视为同一句，合并取并集
SPAN_HOLE = 0.90        # 相邻两句隔得比这还近，中间的空洞按连续处理（换句淡入会漏检）


def _box_mean(a, k: int = 4):
    """邻域均值（积分图实现，比逐点循环快两个数量级）。

    2026-11-18 内存修复：整摞帧一次性建积分图，长片（383s）+ 大分辨率
    （720×1280 → 检测尺寸 1138×640，最多 900 帧）时单个数组就 2.44GiB，
    积分图 + 四个高级索引中间量同时活着直接 ArrayMemoryError；上层把它
    当成「这集没字幕」吞掉，于是逐句 spans 永远是空、抹字退回固定位置。
    积分图是沿空间轴（1/2）算的，按帧（轴 0）分块结果逐位一致——分块跑，
    峰值内存与帧数无关。判据没变，但 bump VERSION 让曾部分成功的老项目
    全部重量一遍（有的集 OOM、有的集成功的项目，靠空 by_ep 判断不到）。
    """
    import numpy as np
    CHUNK = 24  # 24×1138×640×4 ≈ 70MB/数组，几个中间量峰值 <0.5GB
    n = int(a.shape[0])
    if n <= CHUNK:
        return _box_mean_block(a, k)
    out = np.empty((n, a.shape[1], a.shape[2]), dtype="float32")
    for s in range(0, n, CHUNK):
        e = min(n, s + CHUNK)
        out[s:e] = _box_mean_block(a[s:e], k)
    return out


def _box_mean_block(a, k: int = 4):
    """单块积分图邻域均值。调用方保证块足够小，不会 OOM。"""
    import numpy as np

    ii = np.pad(a, ((0, 0), (1, 0), (1, 0))).cumsum(1).cumsum(2)
    h, w = a.shape[1], a.shape[2]
    y0 = np.clip(np.arange(h) - k, 0, h)
    y1 = np.clip(np.arange(h) + k + 1, 0, h)
    x0 = np.clip(np.arange(w) - k, 0, w)
    x1 = np.clip(np.arange(w) + k + 1, 0, w)
    s = (ii[:, y1][:, :, x1] - ii[:, y0][:, :, x1]
         - ii[:, y1][:, :, x0] + ii[:, y0][:, :, x0])
    c = ((y1 - y0)[:, None] * (x1 - x0)[None, :]).astype("float64")
    return (s / c).astype("float32")


def _white_mask(fr) -> "np.ndarray":
    """「纯白且显著亮于邻域」的逐帧掩膜，判据与 detect() 完全一致。

    detect_spans 原来整摞算 `fr - _box_mean(fr,5)`：900 帧 ×1138×640 的
    float32 中间量就是 2.44GiB。邻域均值就算分块了，这个减法仍是一次性整摞。
    按帧分块算 bool 掩膜再拼回去，峰值与帧数无关，结果与整摞逐位一致。
    """
    import numpy as np

    CHUNK = 24
    n = int(fr.shape[0])
    out = np.empty(fr.shape, dtype=bool)
    for s in range(0, n, CHUNK):
        e = min(n, s + CHUNK)
        blk = fr[s:e]
        out[s:e] = (blk >= WHITE_MIN) & ((blk - _box_mean_block(blk, 5)) > WHITE_DIFF)
    return out


def _frames(src: str, dur: float, want: int = 12, fps: float | None = None):
    """均匀抽帧（或按指定 fps）→ (ndarray[n,h,w] float32, 分辨率)。失败返回 (None, None)。"""
    import numpy as np

    if not dur or dur <= 0:
        return None, None
    if fps and fps > 0:
        step = 1.0 / float(fps)
        # 长片保护：逐句检测不需要 4fps 走完半小时素材，最多取 SPAN_MAX_FRAMES 帧
        if dur / step > SPAN_MAX_FRAMES:
            step = dur / SPAN_MAX_FRAMES
    else:
        step = max(0.8, float(dur) / max(4, want))
    # 目标高度直接按原片宽高比算出来，然后写死进 scale。
    # **不能靠「字节数反推」**：同一个字节数常有多个因数解，竖屏素材会反推出
    # 1920 这种不存在的行数，行索引跟画面位置就对不上（踩过）。
    h = 0
    try:
        info = core.ffprobe(src)
        ow, oh = int(info.get("width") or 0), int(info.get("height") or 0)
        if ow > 0 and oh > 0:
            h = int(round(oh * DETECT_W / float(ow)))
            h -= h % 2
    except Exception:  # noqa: BLE001
        h = 0
    scale = f"scale={DETECT_W}:{h}" if h else f"scale={DETECT_W}:-2"
    # 只抽十来帧：字幕位置对一部剧是固定的，抽样够用；
    # 多抽只会让 20 集的项目在点生成后干等十几秒。
    cmd = [core.ff("ffmpeg"), "-nostdin", "-v", "error", "-i", src,
           "-vf", f"fps=1/{step:.3f},{scale},format=gray",
           "-f", "rawvideo", "-"]
    try:
        raw = core.run_cancelable(cmd, timeout=120).stdout
    except (subprocess.TimeoutExpired, OSError):
        return None, None
    if not raw:
        return None, None
    px = len(raw)
    if h and px % (DETECT_W * h) == 0:
        n = px // (DETECT_W * h)
    else:                                    # 兜底：反推（尺寸算不出来时）
        h, n = 0, 0
        for cand in range(int(DETECT_W * 3), DETECT_W, -2):
            if px % (DETECT_W * cand) == 0:
                h, n = cand, px // (DETECT_W * cand)
                break
    if not h or n < 2:
        return None, None
    arr = np.frombuffer(raw[: n * DETECT_W * h], dtype=np.uint8)
    return arr.reshape(n, h, DETECT_W).astype("float32"), (DETECT_W, h)


def detect(src: str, dur: float, want: int = 24) -> dict | None:
    """识别一集的字幕带。识别不出（没字幕 / 画面太白 / 解码失败）返回 None。"""
    import numpy as np

    fr, (w, h) = _frames(src, dur, want)
    if fr is None:
        return None
    # **判据是「接近纯白的像素密度」，不是「比邻域亮多少」**。
    # 短剧字幕一律是纯白字 + 黑描边（编码后仍在 225 以上），而白衬衫、白墙、
    # 白大褂这些「画面里的白」只有 200-235。原来用「比邻域亮 38」，遇到
    # 「白字压在白衬衫上」时邻域本身就是亮的，差值不成立 → 字幕整行漏检，
    # 反而把黑裤子上的皮带金属扣（白衬衫旁边的强对比）选成了字幕带，
    # 实测《解雇我》第 1 集因此锁到 0.838（真字幕在 0.762-0.800）。
    # 换成纯白密度后，字幕行 25%-33%、皮带 0%、白衬衫 <1%，一次就分开。
    # **判据 =「纯白」且「显著亮于邻域」，两条缺一不可**。
    # 短剧字幕一律是纯白字 + 黑描边（编码后仍在 225 以上），而白衬衫、白墙、
    # 白大褂这些「画面里的白」只有 200-235。
    #   · 只用「比邻域亮 38」（最初的写法）：白字压在白衬衫上时邻域本来就亮，
    #     差值不成立 → 字幕整行漏检，反而把黑裤子上的皮带金属扣选成了字幕带
    #     （《解雇我》第 1 集锁到 0.838，真字幕在 0.762-0.800）；
    #   · 只用「够白」：白衬衫大面积过 205，直接把带子拉到胸口（ep02/ep03 锁到 0.620）。
    # 两条合起来，四个样本（两部剧不同集）全部落在 0.760-0.799，极差不到 0.005。
    white = (fr >= WHITE_MIN) & ((fr - _box_mean(fr, 5)) > WHITE_DIFF)
    if not white.any():
        return None
    # 逐行「字像素占比」，跨帧有两种汇总方式，**优先平均、平均不够再退回第二高**：
    #   · 平均：位置最准（每帧都投票），但会被「没字幕的时段」摊薄——短片段项目
    #     （15 秒 3 句、句间还淡出）实测只有 0.0539，勉强贴下限，只有一行达标，
    #     整集被判成「没字幕」（2026-11-12 项目 2934765f8925 第 3 集就返 None）；
    #   · 第二高：字幕只要出现 ≥2 帧就过，不怕空档；但被摊薄的持续亮物也会浮上来
    #     （混剪那几集上沿从 77.6% 涨到 75.8%，多抹一整条）。
    # 所以先按平均走完整套判据，出不了合规行段才换第二高——精准的优先，兜底的后手。
    per = white.mean(axis=2)
    cands = [per.mean(axis=0)]
    if per.shape[0] >= 3:
        cands.append(np.partition(per, -2, axis=0)[-2])

    y0, y1 = int(h * Y_MIN), int(h * Y_MAX)
    segs: list[list[int]] = []
    rows = None
    for dens in cands:
        rows = np.where(dens > COVER_MAX, 0.0, dens)
        band_rows = rows[y0:y1]
        if not band_rows.size or float(band_rows.max()) < DENS_MIN:
            continue
        peak = float(band_rows.max())
        th = max(DENS_MIN, peak * 0.30)
        idx = np.where(band_rows >= th)[0]
        if not idx.size:
            continue
        # 允许 1 行空洞：字行有高有低（「一」和「警」不一样高），但不能多，
        # 多留空洞就会把上下相邻的亮物缝进来。
        ss: list[list[int]] = []
        cur = [int(idx[0])]
        for v in idx[1:]:
            if int(v) - cur[-1] <= 1:
                cur.append(int(v))
            else:
                ss.append(cur)
                cur = [int(v)]
        ss.append(cur)
        # 带高不合规的段先剔掉：太矮是反光/亮边，太高是一片亮墙，都不是一行字
        ss = [s for s in ss
              if MIN_TEXT_H <= (s[-1] + 1 - s[0]) / float(h) <= MAX_TEXT_H]
        if ss:
            segs = ss
            break
    if not segs or rows is None:
        return None

    # 再在合规段里选「面积最大 × 越靠下越优先」。
    # 面积 = 行数 × 该行的字覆盖率，比单看最长更准（一行稀疏的字不该赢过整句）；
    # 位置权重是温和的（最下 vs 最上只差 20% 出头），只在两块画面打平时起作用。
    def _score(s: list[int]) -> float:
        area = sum(rows[y0 + i] for i in s)
        mid = (s[0] + s[-1] + 1) / 2.0 / h
        return area * (0.55 + 0.60 * mid)

    best = max(segs, key=_score)
    top_i, bot_i = y0 + best[0], y0 + best[-1] + 1
    text_top, text_bot = top_i / h, bot_i / h
    text_h = text_bot - text_top

    # **双行字幕连通扩展**（2026-11-18 项目 74ac63113705「八万八」）：
    # 短剧一句台词常占上下两行，但双行只在部分帧出现（以上视频 24 帧里只有 4 帧
    # 双行）。跨帧平均（cands[0]）把第二行摊薄到阈值以下，best 只框住中间一行
    # （实测 0.770~0.787，真字 0.748~0.831）→ 逐句搜索窗下沿 0.814 截断，
    # 成片第二行下半截抹不掉。这里用「逐行跨帧最大密度」把被摊薄的字行捞回来，
    # 再把 best 上下**连续**达标的行并进来——只连通扩展，碰到空档就停，
    # 免得把字幕上方的人物/下方道具也缝进带里。
    # 用「逐行跨帧最大密度」把被摊薄的字行捞回来。
    #
    # 2026-11-20 修（v9）：原来是 row_max >= 0.03 就「连续」往上下吃。0.03 只压得住
    # 噪点（max<0.02），压不住白衬衫/下巴——这些在个别帧的 row_max 有 0.05-0.10，
    # 一帧达标就接着下一帧，连成 113 行，text_bottom 从 0.698 被撑到 0.79
    # （重生八零 ep02：band 高 0.22、逐句框 0.36，镜像补出一块嘴）。
    # 试过「加跨帧平均门槛」：真双行第二行只在 4/24 帧出现，平均被摊薄到 0.019，
    # 跟误判的 0.004 拉不开，会把真双行也砍掉（合成测试 FAIL，不能用）。
    # 正解是分两段判：**先看每行够不够强（像字），再看段间空档够不够小（像两行）**。
    # 真字行 row_max 0.21+，白衬衫/下巴 <0.10，中间有干净的缝；两行字幕之间
    # 的空档通常 <0.5 个行高。只合并「强段 + 小空档」，弱亮物连强段的边都摸不到。
    # 门槛取 max(0.12, 主行峰值×0.45)：绝对下限挡衬衫，相对项自适应淡字幕。
    try:
        row_max = per.max(axis=0)
        row_max = np.where(row_max > COVER_MAX, 0.0, row_max)
        main_peak = float(row_max[top_i:bot_i].max())
        barrier = max(0.12, main_peak * 0.45)
        # 只在 best 上下 3 倍字高的窗口里找强段，别摸到更远的画面
        lo, hi = int(text_top * h) - max(2, int(text_h * h) * 3), int(text_bot * h) + max(2, int(text_h * h) * 3)
        lo, hi = max(y0, lo), min(y1, hi)
        strong = row_max[lo:hi] >= barrier
        # 连续的强行先成段
        runs: list[list[int]] = []
        inrun = False
        for yy in range(lo, hi):
            if strong[yy - lo] and not inrun:
                st, inrun = yy, True
            elif not strong[yy - lo] and inrun:
                runs.append([st, yy])
                inrun = False
        if inrun:
            runs.append([st, hi])
        # 段间空档 <= 0.5 字高才并（两行字幕的间距）；大空档是画面亮物，停手
        gap_tol = max(2, int(text_h * h * 0.5))
        merged: list[list[int]] = []
        for rr in runs:
            if merged and rr[0] - merged[-1][1] <= gap_tol:
                merged[-1][1] = rr[1]
            else:
                merged.append(list(rr))
        # 只取跟 best 行段重叠的那一条合并段
        for m in merged:
            if not (m[1] <= top_i or m[0] >= bot_i):
                top_i, bot_i = m[0], m[1]
                break
    except Exception:  # noqa: BLE001 —— 扩展只是优化，失败就用原来的单行带
        pass
    # 扩展后重新算 text_top/bot，后续填充条、逐句搜索窗全部跟着放大（一处改、全局生效）
    text_top, text_bot = top_i / h, bot_i / h
    text_h = text_bot - text_top

    # 上下各留余量；带上沿不能顶到画面一半以上（否则等于又抹一大条）
    top = max(Y_MIN, text_top - text_h * PAD)
    bot = min(0.995, text_bot + text_h * PAD)
    size = max(0.03, bot - top)

    # 补全用「上方镜像」还是「下方镜像」：哪边画面更平就用哪边。
    # 短剧字幕通常在人物下方、背景是衣服/墙面，上方往往比下方（手、道具、
    # 桌面）干净得多；但也见过字幕压在下三分之一、上方是脸的情况，
    # 所以按梯度能量现算，不写死方向。
    # **留 15% 的容差**：两块差不多平时保持「上方」，否则同一部剧里各集会来回翻，
    # 一半用上补一半用下补，观感不一致。哪边没空间就直接排除。
    fill = "above"
    try:
        need = int(size * h)

        def _tex(a0: int, a1: int) -> float:
            if a0 < 0 or a1 > h or a1 - a0 < 4:
                return 9e9
            b = fr[:, a0:a1, :]
            return float(np.abs(b - _box_mean(b, 2)).mean())

        up_tex = _tex(int(top * h) - need, int(top * h))
        dn_tex = _tex(int(bot * h), int(bot * h) + need)
        # 容差 0.70（不是 0.85）：实测同一部剧各集上下两侧的平整度差异很小，
        # 0.85 会让一半集选「下」一半选「上」，出片后一半画面从上方补、
        # 一半从下方补，同一部剧观感不统一。只有下方明显更平时才翻。
        fill = "below" if dn_tex < up_tex * 0.70 else "above"
    except Exception:  # noqa: BLE001 —— 方向只是优化，算不出来就用上方
        fill = "above"

    # 水平跨度：带内逐列的「白像素密度」（跨帧 + 跨行平均）。
    # 用并集：字幕换句时长句决定边界，短句不会把范围收小。
    # 旧写法是 `white.mean(0)[top:bot].max(0) >= 0.06`（任一帧任一像素），
    # 太松，随便一点背景反光就把右边界推到 1.0（八万八 ep20 给 0.011-0.941）。
    cols = white[:, top_i:bot_i, :].mean(axis=(0, 1))
    cth = max(0.012, float(cols.max()) * 0.06)
    cx = np.where(cols >= cth)[0]
    if cx.size:
        # 左右各留出「字高的六成」当余量：描边、斜体、句末标点都在字框外，
        # 不留就会被切掉半个字边（放大看是一道白线）。
        mx = max(0.006, text_h * 0.6)
        left = max(0.0, float(cx[0]) / w - mx)
        right = min(1.0, float(cx[-1] + 1) / w + mx)
    else:
        left, right = 0.0, 1.0

    return {"pos": round(float(top), 4), "size": round(float(size), 4),
            "text_top": round(float(text_top), 4), "text_bottom": round(float(text_bot), 4),
            "left": round(left, 4), "right": round(right, 4), "fill": fill,
            # 峰值密度 ≈ 0.1 就是「一行字占满十个字」的典型值，到 0.05 以下基本是噪点
            "conf": round(min(1.0, peak / 0.10), 3), "frames": int(fr.shape[0])}


def _frame_box(mask, y0: int, y1: int, h: int, w: int,
               core0: int | None = None, core1: int | None = None):
    """单帧在搜索窗里找字幕包围盒（像素 l,r,t,b）；这帧没字返回 None。

    跟整片 detect 的区别：这里只能靠**单帧**密度，没有「跨帧累积」可用，
    所以门槛要放松（跨帧平均会被「字幕时有时无」摊薄，单帧峰值高得多）。

    **v10 浅背景误判修复**：搜索窗下沿为了容双行字幕开到 pos+size*1.35，
    但 size 是被 PAD 撑大的整条带高，窗底常常伸到画面 0.82（浅色桌面/手）。
    真字幕行密度 0.29-0.44、且落在 band 的 text_top/text_bottom 核心区内；
    桌面反光只有 0.03、落在核心区下方。原来纯按「密度总量」选段会选中桌面
    那条假段（它更宽更靠下），框被顶到窗底 0.82，再经 span 的 pad/兜底放大
    到 0.856 —— 整条下半屏被镜像抹掉（白月光 ep36 25s）。
    传入 core0/core1（band 文字带核心区的像素 y）后：核心区内有达标段就只认
    核心区内的段；核心区密度 <0.12（真字带连一行强字都没有）直接判这帧无字，
    不退回全窗——否则窗外的浅色背景段会被当字、放大成半屏抹除带。
    """
    import numpy as np

    band = mask[y0:y1]
    if not band.any():
        return None
    rows = band.mean(axis=1)
    peak = float(rows.max())
    if peak < DENS_MIN * 0.45:
        return None
    th = max(DENS_MIN * 0.45, peak * 0.28)
    idx = np.where(rows >= th)[0]
    segs, cur = [], [int(idx[0])]
    for v in idx[1:]:
        if int(v) - cur[-1] <= 1:
            cur.append(int(v))
        else:
            segs.append(cur)
            cur = [int(v)]
    segs.append(cur)

    # v10：核心区（真字幕带）里有达标段就只在核心区里选，挡掉窗外浅色背景。
    # 门槛用「真字行密度下限」而非相对峰值：真字幕段峰 0.29+，桌面假段 0.03，
    # 中间隔着一个数量级，0.12 足够分开又不会误伤淡字幕。
    if core0 is not None and core1 is not None and core1 > core0:
        c_lo, c_hi = max(0, core0 - y0), min(len(rows), core1 - y0)
        core_segs = []
        for s in segs:
            if not s or s[-1] < c_lo or s[0] >= c_hi:
                continue
            a, b = max(c_lo, s[0]), min(c_hi, s[-1] + 1)
            if b > a and float(rows[a:b].max()) >= 0.12:
                core_segs.append(s)
        c_peak = float(rows[c_lo:c_hi].max()) if c_hi > c_lo else 0.0
        if core_segs:
            segs = core_segs
        elif c_peak < 0.12:
            # 核心区（band 量出的真字带）连一行强字都没有 → 这一帧没有字幕。
            # 实测量级：真字幕行密度 0.29-0.44，浅色桌面/衣服反光 0.028-0.03，
            # 隔一个数量级。此时窗外若还有弱段（桌面白边、下巴），那是背景不是字，
            # 判无字。否则那块会被当字幕框、经 pad 放大成半屏镜像抹除带。
            return None
    # 按「密度总量」选段：一行稀疏的点不该赢过两行密字
    seg = max(segs, key=lambda s: float(rows[s].sum()))
    ti, bi = y0 + seg[0], y0 + seg[-1] + 1
    if (bi - ti) / float(h) < MIN_TEXT_H * 0.5:
        # 太矮通常是反光/亮边，但**画面一动就会把真字幕打碎**：推镜、手持抖动
        # 让一行字的笔画在行投影上裂成 3-4 段（实测《解雇我》ep1 17.25-18.5 秒，
        # 「在这家工厂干了三年」被切成 4 段、最长 5 行 < 7.4 行门槛），
        # 整句判成「没字」→ 掩码里留一个 1 秒的洞，原字幕在成片里闪一下才消失。
        # 所以矮不等于没有：峰值仍然很高就说明这帧确实有字，只是段碎了，
        # 高度退回本帧各段的总跨度（宽度照旧按列投影逐句取）。
        if peak < DENS_MIN * 2.0:
            return None
        ti, bi = y0 + min(s[0] for s in segs), y0 + max(s[-1] for s in segs) + 1
    cols = mask[ti:bi].mean(axis=0)
    cp = float(cols.max())
    if cp <= 0:
        return None
    cx = np.where(cols >= max(0.012, cp * 0.10))[0]
    if not cx.size:
        return None
    return int(cx[0]), int(cx[-1] + 1), int(ti), int(bi)


def detect_spans(src: str, dur: float, band: dict | None = None,
                 fps: float | None = None) -> dict | None:
    """逐句量字幕包围盒（宽高都贴合字），给「只修字幕区域」用。

    返回 {"spans":[{t0,t1,l,r,t,b}], "union":{...}, "count": n, ...}

    **时间窗前后各外扩一个采样间隔**：4fps 只能定位到 0.25 秒，字幕可能
    3.02 秒就出现、3.25 秒才被抽到。外扩后宁可多抹 0.25 秒——那段本来就是
    同一块背景，抹掉看不出来；反过来不外扩，开头半个字会留在画面上。
    """
    band = band if band else detect(src, dur)
    if not band:
        return None
    fr, (w, h) = _frames(src, dur, fps=fps or SPAN_FPS)
    if fr is None:
        return None
    n = int(fr.shape[0])
    if n < 2:
        return None
    step = float(dur) / n
    white = _white_mask(fr)
    # 搜索窗：字幕永远在带里、双行只会向下多一行。上沿按「文字顶再往上一点」
    # 就够（不能按膨胀后的 size 放宽，否则冲到画面 0.59 把人物脸框进来——
    # 实测 202/224s 帧），下沿才按整个 size 放宽容下第二行。
    pos, size = float(band["pos"]), float(band["size"])
    text_top = float(band.get("text_top") or pos)
    text_bot = float(band.get("text_bottom") or (pos + size))
    up_room = max(0.012, (text_bot - text_top) * 0.35)  # 上沿只留约 1/3 带高的余量
    y0 = max(0, int((text_top - up_room) * h))
    # v10：下沿不再用 pos + size*1.35。size 含 PAD 膨胀（实测这集 0.1424），
    # 1.35 倍直接把窗底顶到 0.82，正好罩住浅色桌面。双行字幕第二行最多再低
    # 「一个字高」，用 (text_bot-text_top) 这个真实字高去留余量即可。
    text_h = max(0.001, text_bot - text_top)
    y1 = min(h, int((text_bot + text_h * SPAN_DOWN_ROOM) * h))

    core0, core1 = int(text_top * h), int(text_bot * h)
    boxes = [_frame_box(white[i], y0, y1, h, w, core0, core1) for i in range(n)]

    def close(a, b) -> bool:
        return (abs(a[0] - b[0]) <= SPAN_MERGE * w
                and abs(a[1] - b[1]) <= SPAN_MERGE * w
                and abs(a[2] - b[2]) <= SPAN_MERGE * h * 0.5
                and abs(a[3] - b[3]) <= SPAN_MERGE * h * 0.5)

    raw, cur = [], None
    for i, b in enumerate(boxes):
        if b is None:
            if cur:
                raw.append(cur)
                cur = None
            continue
        if cur and close(cur["b"], b):
            cur["b"] = (min(cur["b"][0], b[0]), max(cur["b"][1], b[1]),
                        min(cur["b"][2], b[2]), max(cur["b"][3], b[3]))
            cur["i1"] = i
        else:
            if cur:
                raw.append(cur)
            cur = {"i0": i, "i1": i, "b": b}
    if cur:
        raw.append(cur)

    spans = []
    # 上下界的兜底：单帧行段用的是「峰值 × 0.28」的密度阈值，笔画稀少的字顶/字底
    # 会被切掉（实测真字 77.42–80.31%，单帧框只给到 79.2–80.0）。而整集统计出来的
    # 文字带（text_top/text_bottom）是准的（77.5 / 80.3 对上真字位）。下面对每句取
    # **并集**：宽度仍逐句（这才是「只抹字实际占的那块」），高度至少盖住整集文字带，
    # 否则字顶留一条白边、抹完还看得见半个字（2026-11-12 实测：alpha 只有 0.62，
    # 字形处亮度 145/233，肉眼是灰字）。
    b_top = band.get("text_top")
    b_bot = band.get("text_bottom")
    for r in raw:
        l, rr, tp, bt = r["b"]
        bw, bh = (rr - l) / float(w), (bt - tp) / float(h)
        if bw < SPAN_MIN_W or bh <= 0:
            continue                       # 反光点、边角碎片，不是一句字幕
        if l / float(w) > 0.88 and bw < 0.12:
            continue                       # 贴右边缘的台标/水印
        top_n = tp / float(h)
        bot_n = bt / float(h)
        if b_top is not None:
            top_n = min(top_n, float(b_top))
        if b_bot is not None:
            bot_n = max(bot_n, float(b_bot))
        bh_n = max(0.001, bot_n - top_n)
        # 句首多让一点：字幕比「第一帧被认出来」出现得更早（淡入、笔画少、
        # 半透明），4fps 下只外扩一个间隔不够——实测首句 0.66 秒就出字，
        # 第一帧认到是 0.75，成片里那 0.1 秒原字幕会闪一下。往前让 1.4 个间隔
        # （0.35 秒）刚好盖住；相邻句由下面的中点切分兜着，不会互相覆盖。
        t0 = max(0.0, r["i0"] * step - step * 1.4)
        t1 = min(float(dur), (r["i1"] + 1) * step + step)
        px, py = bh_n * SPAN_PAD_X, bh_n * SPAN_PAD_Y
        spans.append({"t0": round(t0, 3), "t1": round(t1, 3),
                      "l": round(max(0.0, l / float(w) - px), 4),
                      "r": round(min(1.0, rr / float(w) + px), 4),
                      "t": round(max(0.0, top_n - py), 4),
                      "b": round(min(1.0, bot_n + py), 4)})
    if not spans:
        return None

    # 外扩到相邻句之间的一半就停：一开始是「重叠就并集」，结果 4fps 外扩一个
    # 采样间隔后相邻句几乎全部互相接触，60 秒的片子被粘成 3 大段（实测踩过）。
    # 现在两句各让一半，谁都不越界，矩形保持各自的大小。
    #
    # 反过来，**挨得很近的两句之间的小洞要补齐**（2026-11-12）：换句的一瞬间字幕
    # 在淡入，单帧判据会认成「没字」，4fps 抽帧外扩后仍会留下 0.3–0.6 秒的空洞。
    # 洞里字幕原样留着 —— 成片会闪一下原字幕（实测 302.0s 正好落在洞里，
    # 「清洁工第三天」整句没被抹）。间隔小于 SPAN_HOLE 就按连续处理。
    for i in range(len(spans) - 1):
        a, b = spans[i], spans[i + 1]
        if a["t1"] > b["t0"]:
            mid = round((a["t1"] + b["t0"]) / 2.0, 3)
            a["t1"], b["t0"] = mid, mid
        elif b["t0"] - a["t1"] <= SPAN_HOLE:
            a["t1"] = b["t0"]

    union = {"l": min(s["l"] for s in spans), "r": max(s["r"] for s in spans),
             "t": min(s["t"] for s in spans), "b": max(s["b"] for s in spans)}
    cover = sum(s["t1"] - s["t0"] for s in spans)
    return {"spans": spans, "union": union, "count": len(spans),
            "pos": round(pos, 4), "size": round(size, 4),
            "fill": band.get("fill") or "above",
            "cover": round(cover, 2), "dur": round(float(dur), 2),
            "frames": n, "fps": round(1.0 / step, 2)}


def median_of(results: list[dict]) -> dict | None:
    """多集结果取中位数：个别集的构图特殊（特写/空镜）不该带偏整部剧。"""
    rs = [r for r in results if r]
    if not rs:
        return None

    def med(key: str) -> float:
        vals = sorted(float(r[key]) for r in rs)
        n = len(vals)
        return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0

    def outer(key: str, pick) -> float:
        """左右边界要取**并集**，不能取中位数。

        字幕换句长短不一，各集最宽的那句决定真实跨度；取中位数会把最长那句的
        头尾切掉，残留半个字。但个别集识别跑偏（把背景反光当成字）会把范围
        撑到满屏，所以先剔掉离中位数超过 8% 的异常集，再取最外。
        """
        vals = sorted(float(r[key]) for r in rs)
        mid = vals[len(vals) // 2]
        ok = [v for v in vals if abs(v - mid) <= 0.08] or vals
        return pick(ok)

    fills = [r.get("fill") or "above" for r in rs]
    fill = "below" if fills.count("below") > fills.count("above") else "above"
    return {"pos": round(med("pos"), 4), "size": round(med("size"), 4),
            "text_top": round(med("text_top"), 4), "text_bottom": round(med("text_bottom"), 4),
            "left": round(outer("left", min), 4), "right": round(outer("right", max), 4),
            "fill": fill,
            "conf": round(max(float(r.get("conf") or 0) for r in rs), 3),
            "episodes": len(rs), "per_ep": {r.get("ep") or "": r for r in rs if r.get("ep")}}
