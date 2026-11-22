"""渲染：从原片逐段切出 → 统一规格 → 拼接 → 统一响度。"""
from __future__ import annotations

import json
import math
import os
import random
import re
import shutil
import subprocess
import threading
import time
import unicodedata

from . import core, voiceiso

W, H, FPS = 720, 1280, 30

# ── 顶部标题字幕（2026-11-03 加）──────────────────────────────────────────
# 每一块的顶部烧一行字：默认用这一块的 label（去掉【爆点】【缓冲】这类分类前缀），
# 没有标签就显示「精彩片段」。为什么不用固定四个字：一套十几块全程都是「精彩片段」，
# 观众第二块就无视了；每块换字相当于「这一段演什么」的预告，才有信息量。
# 只在**切段时**烧字（_cut 的 -vf 里加 drawtext），拼接阶段视频流仍是 copy——
# 不重编码、不加时长、拼接流程一行不用动。位置在画面顶部 5.5%，短剧台词字幕
# 都在下方，不会打架。
# ── 出片增强（2026-11-04）────────────────────────────────────────────────
# 缓冲块加速：5-6 分的缓冲/反应镜头放快一点（setpts+atempo，音画同步变速、
# 声调不变），拖节奏的地方收紧，爆点块保持原速。只敢用小倍率——1.2 以上
# 台词开始发飘，短视频平台也容易判「加速素材」。
BUFFER_SPEED = 1.15
# ── 去重缩放（2026-11-04）──────────────────────────────────────────────────
# 整幅放大再居中裁回 720x1280：画面指纹跟原片不一样，边缘一圈（含素材
# 自带的黑边、水印带）被切掉，平台拿原片比对时对不上。
# 2026-11-12 起倍率不再写死 103%，由 fp_spec 按套抽（见下面的指纹随机化）；
# 这里的 1.03 只当「放大这一项开着」的哨兵值，同时也是一条没传 fp 时的兜底。
# 真要处理字幕别靠调这个倍率：到第 2 步（去字幕 + 加自己的字幕）生成处理好的视频，出片时选它。
ZOOM = 1.03

# ── 指纹随机化（2026-11-12 用户要求）────────────────────────────────────────
# 原来放大 3%、横向拉伸 1.5%、颗粒/暗角/光晕是四个写死的常量，整批共用：
# 两套片子在平台上抽帧比对时几何结构完全一致，只有调色不同。现在每套各抽一组
# 参数，同一套隔几天再点出片也换一组（种子由 batch 传，见 render_edl 的 fp_seed）。
#
# 改画面结构的主力是 zoom（裁掉多少边缘）和 sx/sy（取景框偏多少），
# rot 是肉眼看不出、但抽帧哈希全变的微旋转；质感三项只在用户已经开着的基础上浮动
# （用户拉到 0 的不动，不能因为"随机"把人关掉的颗粒又加回来）。
#
# 档位语义：
#   zoom    放大倍率区间（越大切掉的边缘越多）
#   rot     微旋转角度上限（度）
#   shift   裁剪偏移上限（0-1，占可用余量的比例；0 = 严格居中）
#   gain    每段音频增益上限（dB）
#   spm     横向变形的绝对浮动（±，如 0.008 = ±0.8%）
#   tpm     质感三项的乘性浮动（±，如 0.28 = ±28%）
FP_DEFAULT = "std"
# 新增三键（2026-11-22，见下面「指纹新维度」注释）：
#   cn    色度噪声强度（U/V 平面噪点，0 = 关）
#   lens  镜头畸变幅度（±，0 = 关）
#   eqw   动态调色摆动系数（0 = 关，>0 乘在摆幅上）
FP_LEVELS = {
    "soft":   {"zoom": (1.012, 1.032), "rot": 0.00, "shift": 0.00, "gain": 0.0,
               "spm": 0.003, "tpm": 0.10, "cn": 0, "lens": 0.0, "eqw": 0.0},
    "std":    {"zoom": (1.008, 1.056), "rot": 0.30, "shift": 0.55, "gain": 0.5,
               "spm": 0.008, "tpm": 0.28, "cn": 6, "lens": 0.0012, "eqw": 1.0},
    "strong": {"zoom": (1.004, 1.078), "rot": 0.80, "shift": 0.85, "gain": 1.0,
               "spm": 0.014, "tpm": 0.45, "cn": 10, "lens": 0.002, "eqw": 1.4},
}
FP_KEYS = list(FP_LEVELS)

# ── 逐段微变速（2026-11-17）────────────────────────────────────────────────
# 现有指纹只动画面几何，时间轴上是匀速的：同一段素材在两条片子里"第 3 秒的画面"
# 落在同一时刻，按时间抽帧比对时最好抓。加一点逐段变速，时间轴也被打散。
#
# 但变速会改片长：20 段各快 1% 就是十几秒的差，字幕、调色分区、后续拼接全得跟着漂。
# 所以配套三步约束（思路来自竞品的「总时长守恒」，代码按我们的结构重写）：
#   ① 相邻限幅：两段速度差不足半个振幅时强行拉开，否则等于两段同速、白扰一次；
#   ② 总量归一：所有 speed 同乘一个 factor，让 Σ(时长/speed) 回到原计划总长；
#   ③ 整数帧：按 fps 分帧、余帧摊平，再反算 speed —— 拼接要求每段帧数确定。
#
# 振幅按档位给，最大值压在 1.2%：超过 1.5% 台词开始"赶"，平台也容易判「加速素材」。
FP_SPEED = {"soft": 0.003, "std": 0.007, "strong": 0.012}
MIN_FRAMES = 2          # 一段至少留 2 帧，0 帧的段会让 concat 直接失败

# ── 指纹新维度（2026-11-22，借鉴零界 1.5.0 拆解，命名走合规口径）──────────
# 原有维度都压在「单张画面的几何/质感」上，这三条补的是另外三个比对面：
#
# ① 帧率适配（fps_out）：整批固定 30fps 时，输出流的帧率跟素材完全一致，
#    按时间抽帧比对的时间轴一模一样。std/strong 档每套在 FPS_ADAPT 区间里
#    抽一个，fps 滤镜只重排时间线，时长守恒、帧数计划照旧（speed_plan 直接
#    吃这个 fps）。soft 档不动（保持 30）。不敢抽得更散：低于 28 会被部分
#    平台判「低帧率素材」压画质。
# ② 色度噪声（cn）：只动 U/V 两个色度平面，亮度不动——肉眼几乎无感，但
#    逐帧哈希全变。c1/c2 两个 seed 都必须写死：少写一个 ffmpeg 用随机值，
#    重跑不可复现（dedup_selftest 的复现断言会抓住）。
# ③ 画质微调（lens + 锐化）：±0.2% 级镜头畸变 + 两档轻锐化随机选一。作用
#    不是扰动本身，而是让成片「看起来像正常调色导出」而不是「明显加了滤镜」。
# ④ 动态调色（eqw）：调色开着的套，eq 参数不再全程恒定，而是随时间正弦慢摆
#    （幅度 ±2% 内、周期 7–18 秒）。恒定参数是一条全局变换，比对面一下就
#    配准；慢摆让它配不上。没开调色的套不加——不能把用户关掉的东西偷偷
#    开回来，与 fp_grade「只抖已开项」同一原则。
#
# 为什么不学零界的「抽帧去重」：它丢 1–3% 帧再补偿时长，效果等价于我们的
# 逐段微变速（FP_SPEED），而 concat 要求每段帧数**精确**——select 丢帧给不出
# 精确帧数，会把整套路「整数帧 + 总量归一」守恒体系打穿，收益又只有零头，
# 不值得（2026-11-22 评估结论）。
FPS_ADAPT = (28.6, 29.9)

# ── 全自动随机（2026-11-17）────────────────────────────────────────────────
# 用户要的是「一张卡全随机、不用逐项设」：放大/变形每段各抽一个无感级微抖，
# 左右镜像和字幕动效按「集」随机（同一集的方向/动效一致，不按段乱跳——
# 一段戏里忽翻忽不翻会很出戏）。所有随机都挂在 fp_seed（含 run_id）上：
# 同一批同一套可复现，每次重新点出片换一批。
#
# 为什么段级放大幅度压到 ±1%：用户举例的是 103%/105%（差 2%）。逐段换倍率
# 会让接缝处构图跳一下，幅度大了很明显；±1% 足够让抽帧指纹每段不同、肉眼无感。
AUTO_ZOOM_AMP = 0.010       # 每段放大倍率在整套基准上 ±1%（无感级）
AUTO_STRETCH_AMP = 0.006    # 每段横向变形在整套基准上 ±0.6%
AUTO_MIRROR_P = 0.6         # 每集左右镜像的概率（抛硬币）；2026-11-19 用户定 60%
                            # 一处同时管两条随机翻转路径：AI 出片按集、直出按 60 秒时间窗
# 选视频直接出片：没有「集」的概念（一个视频就是一整段），所以镜像随机改成
# 在**这一条片子里按时间窗**抛硬币——每 60 秒一个窗口，各翻不翻独立决定。
# 只用于 direct 项目，AI 编辑出片仍走上面的按集随机（见 render_edl 主循环）。
DIRECT_MIRROR_WIN = 60.0    # 直出镜像随机时间窗长度（秒）
AUTO_MOTION_STYLES = ("rise", "fade", "pop", "float", "type")  # 按集随机挑的动效
AUTO_MOTION_RANGE = (0.35, 0.75)   # 按集随机的动效幅度（避开 0 几乎不动 / 100 太晃）

# ── 画面处理：调色 / 变形 / 特效层（2026-11-05 加）──────────────────────
# 全部在**切段那一趟**编码时烧进画面（跟标题字幕同一层），拼接仍然是视频流
# copy，所以：① 不破坏现有拼接架构；② 不额外多编一遍；③ 每一套可以各自不同。
#
# 为什么暖冷用 curves 而不是 colorbalance：2026-11-05 实测（testsrc2 单帧
# signalstats），colorbalance 就算把 rs/bs 拉到 ±0.25，U/V 均值只动 0.1-0.2，
# 肉眼等于没动（它默认 pl=1 保亮度，红蓝位移被抵消）。改走 R/B 通道曲线，
# ±0.10 的中点位移就能量到 U/V 各动 0.4-0.7，方向也对（暖→V 升 U 降）。
#
# 强度 t（0-1）线性缩放每档的系数：滑杆对四档通用，不用给每档写不同的缩放逻辑。
GRADES = {
    "cold":  {"name": "冷调", "r": -0.10, "b": 0.10, "sat": -0.10, "con": 0.05},
    "warm":  {"name": "暖调", "r": 0.10, "b": -0.08, "sat": 0.06, "con": 0.04},
    "vivid": {"name": "浓郁", "r": 0.0, "b": 0.0, "sat": 0.40, "con": 0.18},
    "film":  {"name": "胶片", "r": 0.04, "b": 0.02, "sat": -0.30, "con": 0.06,
              "grain": 5},
}
GRADE_KEYS = list(GRADES)          # 随机模式从这四档里挑

# ── 出厂默认的画面质感层（2026-11-05 用户要求：四项默认开，开在无感档）──
# 数值不是拍的，是 2026-11-05 在《女总裁霸气休夫》实拍帧上校出来的：
#   grain 0.10 → noise alls=4，1:1 放大隐约可见、正常观看无感；0.20 与 0.10 几乎无差
#   glow 0.05  → screen 叠加透明度 0.18，肤色柔一点、细节不糊；0.12 开始发"雾"
#   vignette 0.08 → 配合下面的新公式 angle=PI/7.52，接近镜头自然失光；
#                旧公式 PI/5 起步就有感，0.05-0.12 全部明显压暗（已弃用）
#   stretch 1.015 → 横向 1.5%，脸宽视觉无差；2.5% 才开始隐约可辨
# 滑杆 0 = 用户主动关闭；出厂默认只是"没动过"时的值。
FX_DEFAULTS = {"glow": 0.05, "grain": 0.10, "vignette": 0.08, "stretch": 1.015}

# ── 开头画面镜像（2026-11-12 加；2026-11-18 用户定默认开、上下翻）─────
# 只把成片最前面约 0.3 秒的画面上下 / 左右翻一下，之后恢复原样：开头那一下
# 的画面指纹跟原片对不上，正片又不受影响。跟第 ⑤ 组的「整幅左右镜像」是两件事
# （那个翻全片、字幕会跟着反），所以界面上单独成一组。
#
# 实现用 ffmpeg 滤镜自带的 enable 时间线，hflip / vflip 都支持（本机实测）：
#   hflip=enable='lt(n,9)'  → 只有前 9 帧走翻转，其余帧直接透传。
# 用帧号 n 而不是时间戳 t：切段是 -ss 之后单独编码、fps=30 固定帧率，n 一定从 0
# 起；t 会被变速的 setpts=PTS/speed 拉偏。帧数按 FPS 换算，改帧率常量自动跟着变。
# 好处是**不用开第二条滤镜支路**：-vf 单链、现场抹字幕的 filter_complex、光晕的
# gbrp 混合链三条路径都只是往 post 末尾多接一个滤镜，时间轴和拼接流程一行不动。
FIRST_FLIP_SEC = 0.3
FIRST_FLIP_FRAMES = max(1, int(round(FIRST_FLIP_SEC * FPS)))


def first_flip_filter(direction: str, fps: float = FPS) -> str:
    """开头镜像用的滤镜串；没开 / 方向不认就返回空串（不能凭空加滤镜）。

    fps（2026-11-22 帧率适配）：镜像的帧数窗口按本套输出帧率换算，不再吃
    模块常量——fps 滤镜换成 28.x 后 n 的节奏跟着变，窗口还按 30 算就会多翻
    或少翻几帧。"""
    d = str(direction or "").strip().lower()
    if d not in ("h", "v"):
        return ""
    n_frames = max(1, int(round(FIRST_FLIP_SEC * (fps or FPS))))
    return f"{'hflip' if d == 'h' else 'vflip'}=enable='lt(n,{n_frames})'"

# ── 去掉原片字幕（2026-11-11，流程最前面单独一步，不并进出片设置）──────
# 由 nosub.py 把整批素材预先编码成独立视频；出片时只挑用哪份（source_path），
# 切段那一趟不再叠字幕滤镜。下面两个 _subtitle_*_graph 是给预处理用的。
# 素材画面里烧死的台词字幕是「像素」，跟视频流里挂着的软字幕轨不是一回事：
# 切段只 map [vout] 和 0:a?，软字幕轨本来就不会带进成片；硬字幕只能靠画面处理。
# 两种办法二选一：
#   crop = 把字幕上边缘以下的整块画面裁掉，窗口贴顶放大回原尺寸。
#          最干净（连残影都没有），代价是构图放大、左右被切。
#   soft = 拿字幕带紧邻上方的一条画面补进来，轻模糊 + 上下羽化过渡。
#          保留构图，实测在真实短剧帧上基本看不出被动过。
# 为什么 soft 不用 delogo / 大半径模糊 / 纯色条（2026-11-11 实帧对比）：
#   delogo 在「横跨整幅的宽矩形」上会插值出明显的竖向拖影；
#   大半径模糊会把白字摊成一条深色糊带，字还隐约可辨；纯色条最扎眼。
#   取相邻画面来补，颜色纹理跟四周是连续的，而且这块画面本身没有字幕，
#   所以既没有残影也没有硬边。所有坐标都写比例表达式，源片尺寸任意都成立。
SUB_DEFAULTS = {"mode": "", "pos": 0.75, "size": 0.10, "soft": 0.5}


def subtitle_spec(fx: dict) -> dict:
    """把去字幕参数夹到安全区间，两种键名都认。

    前置步骤（nosub.py）的配置用 mode/pos/size/soft；历史出片设置存的是
    sub_mode/sub_pos/sub_size/sub_soft。两种都要能读，否则换了入口就静默
    走默认值。独立读 fx，不进 grade_spec：去字幕是「这一批固定怎么做」，
    跟随机调色按区分档没有关系，塞进 grade_spec 会被分区逻辑带偏。
    """
    fx = fx if isinstance(fx, dict) else {}
    def pick(k: str):
        v = fx.get("sub_" + k)
        return v if v is not None and v != "" else fx.get(k)
    mode = str(pick("mode") or "").strip()
    if mode not in ("crop", "soft"):
        return {"mode": ""}
    pos = max(0.50, min(0.92, _num(pick("pos"), SUB_DEFAULTS["pos"])))
    size = max(0.04, min(0.20, _num(pick("size"), SUB_DEFAULTS["size"])))
    size = min(size, 0.98 - pos)          # 字幕带底边不能越出画面
    soft = max(0.0, min(1.0, _num(pick("soft"), SUB_DEFAULTS["soft"])))
    fill = str(pick("fill") or "above").strip().lower()
    if fill not in ("above", "below"):
        fill = "above"
    return {"mode": mode, "pos": pos, "size": size, "soft": soft, "fill": fill}


def _subtitle_soft_graph(s: dict) -> str:
    """柔和抹掉：输出标签 [__sub_base]，供整片预处理接 format=yuv420p。

    2026-11-11 二次改版（用户反馈「模糊区域太宽、不够精准」）：
      旧做法是把字幕带整条替换成「带上方画面下拉 + 大半径模糊」，
      带高固定 10%、整宽，结果是一条横贯画面的糊带，很假。
      现在改成**镜像填充**：取带紧邻的等高一整条画面，垂直翻转后铺回带内
      ——翻转后接缝处正好是相邻行，上下边缘天然连续，没有亮度跳变，
      也没有「整条平涂」的塑料感。
      2026-11-12 再改：单侧镜像只能保证一侧接缝连续，另一侧会撞出一条横线
      （白衬衫、墙面上特别明显），所以改成两侧各补一半、中间渐变过渡。
      带高交给自动识别（subdetect），只比字高一圈，不再是写死的 10%。
    标签统一带 __sub_ 前缀，不能跟光晕分支的 [__b]/[__s]/[__g]/[__o] 撞名。
    crop 的 h/y 用 trunc(.../2)*2 取偶数：yuv420p 要求偶数高，源片分辨率
    又是任意的，只能写表达式，不能写死像素。
    """
    pos, size, soft = s["pos"], s["size"], s["soft"]
    fill = str(s.get("fill") or "above")
    chain = _sub_patch(pos, size, soft, fill)
    ov = f"overlay=0:'trunc(H*{pos:.5f}/2)*2':format=auto[__sub_base]"
    return f"{chain};[0:v][__sub_p]{ov}"


def _sub_patch(pos: float, size: float, soft: float, fill: str) -> str:
    """生成「镜像填充条」滤镜链，输出标签 [__sub_p]（尺寸 = 带高 × 全宽）。

    两个调用方共用：整条替换（_subtitle_soft_graph）和逐段掩码（_subtitle_mask_graph）。
    镜像方向按 fill 选，两侧都有空间时用**渐变双侧**（两侧接缝都连续）。
    """
    sigma = 1.5 + 7.5 * soft            # 0 → 1.5（清楚）/ 1 → 9（更柔）
    h_expr = f"'trunc(ih*{size:.5f}/2)*2'"
    up_y = max(0.0, pos - size)                  # 上方镜像源带的上沿
    dn_y = min(max(0.0, 1.0 - size), pos + size)  # 下方镜像源带的上沿
    up_ok, dn_ok = pos - size >= 0.0, pos + size <= 1.0

    # 两侧都有空间 → **渐变双侧镜像**（2026-11-12 实测改法）：
    # 上半取「上方一条」的翻转、下半取「下方一条」的翻转，中间 30% 线性过渡。
    # 单侧镜像只能保证一侧接缝连续，另一侧接缝会和原画面撞出**一条明显的横线**
    # ——在白衬衫、墙面这类有褶皱/纹理的画面上，那条折痕比字幕本身还显眼
    # （真实素材抽帧已复现）。两侧各补一半之后两条接缝都自然，代价只是
    # 中间那窄窄一条有两边内容叠着，淡化过渡后看不出来。
    # 权重切点按 fill 偏一点：above 时上方占 45% 高度，below 时占 25%。
    if up_ok and dn_ok:
        cut = 0.45 if fill != "below" else 0.25
        w = f"clip((Y/H-{cut:.2f})/0.30,0,1)"
        return (
            "[0:v]"
            f"crop=iw:{h_expr}:0:'trunc(ih*{up_y:.5f}/2)*2',vflip[__sub_u];"
            "[0:v]"
            f"crop=iw:{h_expr}:0:'trunc(ih*{dn_y:.5f}/2)*2',vflip[__sub_d];"
            f"[__sub_u][__sub_d]blend=all_expr='A*(1-{w})+B*{w}'[__sub_m];"
            f"[__sub_m]gblur=sigma={sigma:.1f}[__sub_p]"
        )

    # 只有一侧有空间（带贴着画面顶或底）→ 退回单侧镜像
    src_y = up_y if up_ok else dn_y
    return (
        "[0:v]"
        f"crop=iw:{h_expr}:0:'trunc(ih*{src_y:.5f}/2)*2',vflip,"
        f"gblur=sigma={sigma:.1f}[__sub_p]"
    )


def _subtitle_mask_graph(s: dict, spans: list[dict]) -> str:
    """精准抹除：只修 spans 标出的那几块，画面其余像素一个都不动。

    为什么还要这一层：字幕换句时宽度能从 13% 跳到 87%、高度可能从一行变两行，
    固定一个矩形去抹，一半时间在抹不需要抹的地方（用户原话「只修改字幕出现
    的区域」）。这里按逐句包围盒生成**随时间开关的 alpha 掩码**，
    alpha=0 的地方完全保留原画面——没有字幕的时段、字幕左右两侧的空白都不受影响。

    掩码是画在灰度帧上的白矩形，再 gblur 羽化（否则矩形边缘是一条硬直线），
    最后 `lut` 把 235（gray 的 limited-range 白）拉到 255：
    不拉的话只替换 92%，字会留下淡淡一圈残影（实测可见）。
    """
    pos, size, soft = s["pos"], s["size"], s["soft"]
    fill = str(s.get("fill") or "above")
    boxes = []
    u_t, u_b = 1.0, 0.0
    for sp in spans:
        try:
            l, r = float(sp["l"]), float(sp["r"])
            tp, bt = float(sp["t"]), float(sp["b"])
            t0, t1 = float(sp["t0"]), float(sp["t1"])
        except (KeyError, TypeError, ValueError):
            continue
        if r - l <= 0.001 or bt - tp <= 0.001 or t1 - t0 <= 0.02:
            continue
        u_t, u_b = min(u_t, tp), max(u_b, bt)
        boxes.append(
            f"drawbox=x='trunc(iw*{l:.5f}/2)*2':y='trunc(ih*{tp:.5f}/2)*2'"
            f":w='trunc(iw*{r - l:.5f}/2)*2':h='trunc(ih*{bt - tp:.5f}/2)*2'"
            f":color=white@1.0:t=fill:enable='between(t,{t0:.3f},{t1:.3f})'")
    if not boxes:
        # 一句都没检出来 → 退回整条替换，总比留着字幕强
        chain = _sub_patch(pos, size, soft, fill)
        return (f"{chain};[0:v][__sub_p]overlay=0:"
                f"'trunc(H*{pos:.5f}/2)*2':format=auto[__sub_base]")
    # 填充条按**所有框的并集**定高度和位置，跟掩码严丝合缝。
    # 原来用整集那一条（cfg 的 pos/size）：框的上下界是与整集文字带取并集
    # 再加 pad 得来的，常常比 cfg 的带更高（实测 union 6.3% vs 带 5.1%），
    # 多出来的那截 alpha=255 但填充条没盖到 → 露的就是原画面（等于没抹）。
    if u_b - u_t >= 0.02:
        pos, size = round(u_t, 5), round(u_b - u_t, 5)
    chain = _sub_patch(pos, size, soft, fill)
    p = f"{pos:.5f}"

    # **掩码底必须是纯黑**。原来直接拿 `[0:v]format=gray` 当底，再往上画白框：
    # 画面本身就是 alpha，白衬衫（亮度 220+）在 lut 的对比拉伸后直接饱和成
    # 不透明 → 整片白衬衫都被镜像重填，看起来「抹除区域大得离谱」
    # （2026-11-12 用户对比第 14 秒截图发现；深色背景上看不出，浅色衣服必现）。
    mask = ("[0:v]format=gray,lut=y='0'," + ",".join(boxes)
            + ",gblur=sigma=8,lut=y='min(255,max(0,val*1.4-40))',format=gray[__sub_mk]")
    # lut 是**对比拉伸**，不是简单提亮：gblur 会把窄矩形（小尺寸素材上字高只有
    # 30 来像素）的中心也稀释到 0.9 上下，只乘 1.085 拉不满，字就留 12% 残影
    # （实测「字形处 233 / 背景 91」处理完还剩 16.7 的差，肉眼是一层灰字）。
    # 先减 40 再乘 1.4：值 ≥211 一律饱和到 255，100 以下压到接近 0，
    # 中间段保留过渡，边缘仍是羽化而不是硬切。实测残影差从 16.7 降到 11.9。
    # 先把填充条叠回整帧，再和掩码合成：alphamerge 要求两路**同尺寸**，
    # 而 [__sub_p] 只有带那么高（124px vs 1920px，直接合会报
    # "Input frame sizes do not match"，踩过）。
    # 用 alphamerge 而不是 maskedmerge：maskedmerge 在 yuv 上按 235/255 混，
    # 字只淡掉 92%，会留一圈灰影。
    return (f"{chain};"
            f"[0:v][__sub_p]overlay=0:'trunc(H*{p}/2)*2':format=auto[__sub_filled];"
            f"{mask};"
            f"[__sub_filled][__sub_mk]alphamerge[__sub_fg];"
            f"[0:v][__sub_fg]overlay=0:0:format=auto[__sub_base]")


def _subtitle_crop_graph(s: dict) -> str:
    """裁掉：输出标签 [__sub_c]，供整片预处理（nosub.py）接 format=yuv420p。

    从原图取「字幕上边缘以上」那块等比窗口（高 = 原高 × pos，宽同比例，
    左右居中、贴顶），再放大回原尺寸——字幕和它下面的画面一起被切出镜头。
    全部写比例表达式：源片是 720×1280 还是 1080×1920 都成立，不写死像素。
    取偶数是为了 yuv420p（奇数高会直接被编码器拒绝）。
    """
    p = f"{s['pos']:.5f}"
    return (
        "[0:v]"
        f"crop=w='trunc(iw*{p}/2)*2':h='trunc(ih*{p}/2)*2'"
        f":x='trunc((iw-iw*{p})/2/2)*2':y=0,"
        f"scale=w='trunc(iw/{p}/2)*2':h='trunc(ih/{p}/2)*2',"
        "setsar=1[__sub_c]"
    )


def source_path(p: dict, ep: dict, src_ver: str = "") -> str:
    """这一集这一套该用哪份视频：默认原始素材；src_ver=nosub 用去字幕版。

    已处理版是第 2 步预先编码好的独立文件，跟原片时长/音轨一致，
    所以 EDL 里的时间码不用换算，直接拿它当切段输入即可。
    选了它却没生成（或文件被删）必须当场报错，不能悄悄退回带字幕的原片。
    """
    if str(src_ver or "").strip() == "nosub":
        ns = p.get("nosub") or {}
        f = (ns.get("files") or {}).get(str(ep.get("file") or ""))
        if not f:
            raise RuntimeError("这一集还没有生成已处理的视频，请先到第 2 步生成")
        path = os.path.join(core.PROJECTS, str(p.get("id") or ""), "nosub", f)
        if not os.path.isfile(path):
            raise RuntimeError("已处理版视频文件不见了，请回到第 2 步重新生成")
        return path
    return os.path.join(p["src_dir"], ep["file"])


def _esc_lut_path(path: str) -> str:
    """lut3d 的路径转义：反斜杠换正斜杠，冒号和空格都要转义。
    实测（2026-11-05）：中文名 + 空格 + 盘符冒号不转义直接报
    「No such file or directory」——ffmpeg 把冒号当成参数分隔符了。"""
    return (path.replace("\\", "/")
            .replace(":", r"\:")
            .replace(" ", r"\ ")
            .replace("'", r"\\\'"))


def _grade_filters(g: dict | None, wobble: dict | None = None) -> list[str]:
    """把「一档调色 + 强度」翻成 ffmpeg 滤镜列表（顺序：先色相曲线，再饱和度）。

    wobble（2026-11-22 动态调色）：给了就在 eq 上叠随时间正弦慢摆——恒定参数
    是一条全局变换，按帧比对面一下就配准；慢摆（周期 7–18 秒、幅度 ±2% 内）
    让它配不上。只作用于预设调色的 eq：LUT 是用户自己带的效果不碰，没开调色
    也不凭空加 eq（把用户关掉的东西开回来 = 改了他的观感）。

    返回空列表 = 原片直出。LUT 优先于预设：用户自己带来的 .cube 是他想要的效果，
    再叠一层预设只会互相打架。
    """
    if not g:
        return []
    t = max(0.0, min(1.0, _num(g.get("int"), 0.65)))
    lut = (g.get("lut") or "").strip()
    if lut:
        # LUT 的强度交给用户自己（做 LUT 时已经定死），这里不叠预设
        return [f"lut3d=file='{_esc_lut_path(lut)}':interp=tetrahedral"]
    preset = GRADES.get((g.get("grade") or "").strip())
    if not preset or t <= 0.001:
        return []
    out: list[str] = []
    r, b = preset.get("r", 0) * t, preset.get("b", 0) * t
    if abs(r) > 0.001 or abs(b) > 0.001:
        # 中点位移最难看出断层（把 0.5 抬/压一点点），端点保持不动=黑白点不偏
        out.append("curves="
                   f"r='0/0 0.5/{0.5 + r:.4f} 1/1'"
                   f":b='0/0 0.5/{0.5 + b:.4f} 1/1'")
    sat, con = preset.get("sat", 0) * t, preset.get("con", 0) * t
    if abs(sat) > 0.001 or abs(con) > 0.001:
        if wobble:
            # 摆幅压在 ±2% 内：sin(t*ω) 全程摆动，但 eq 的基准还是用户选的档
            out.append("eq="
                       f"saturation='{1 + sat:.4f}+{wobble['sat']:.4f}*sin(t*{wobble['f_sat']:.3f}+{wobble['p_sat']:.2f})'"
                       f":contrast='{1 + con:.4f}+{wobble['con']:.4f}*sin(t*{wobble['f_con']:.3f}+{wobble['p_con']:.2f})'")
        else:
            out.append(f"eq=saturation={1 + sat:.4f}:contrast={1 + con:.4f}")
    grain = preset.get("grain", 0) * t
    if grain >= 1:
        # 胶片档自带的细颗粒；单独勾「颗粒」时会再叠一层（两层不冲突，都是噪点）
        out.append(f"noise=alls={int(round(grain))}:allf=t+u")
    return out


def _fx_filters(g: dict | None) -> list[str]:
    """特效层：暗角、颗粒、光晕以外的轻效果。光晕要拆两路，见 _cut。"""
    if not g:
        return []
    out: list[str] = []
    vig = max(0.0, min(1.0, _num(g.get("vignette"), FX_DEFAULTS["vignette"])))
    if vig > 0.004:
        # angle 越大压得越狠。公式 2026-11-05 重校：旧版 PI/(5-3t) 的低端
        # 起点就是滤镜默认 PI/5，实测 t=0.05 都明显压暗（高调画面尤其明显）。
        # 新版 t=0.1 → PI/7.4（镜头自然失光级），t=0.5 → PI/5，t=1 → PI/2（很重）。
        out.append(f"vignette=angle=PI/{8.0 - 6.0 * vig:.2f}:mode=forward")
    grain = max(0.0, min(1.0, _num(g.get("grain"), FX_DEFAULTS["grain"])))
    if grain > 0.01:
        out.append(f"noise=alls={int(round(2 + 16 * grain))}:allf=t+u")
    return out


def _num(v, default: float) -> float:
    """参数可能来自前端输入框（字符串、空串、null），一律转成可用数字。"""
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def grade_spec(fx: dict, seg_i: int, out_start: float, seed: str = "") -> dict:
    """这一块用哪档调色。

    2026-11-05 晚重整：**随机不再是独立开关，而是档位里的第 5 个选项**（grade="rand"）。
    以前的写法里「选了暖调 + 勾随机」是互相打架的——随机会把用户选的档直接覆盖掉，
    用户根本猜不到最后是哪一档。现在语义只有一条：选哪档就是哪档，选随机就每区换。

    随机的两条规则：
      ① 分区长度本身也是随机的：每区在 [rand_min, rand_max] 秒之间随机取一个长度
         （用户说「分区的话也是随机分几分钟」），按成片时间轴累计，不是死板的 N 分钟；
      ② 每区从四档里抽一档，**相邻两区不重样**；强度围绕用户设的基准上下浮动 ±18%。
    随机但可复现：种子 =「项目+套+代次」，同一套重渲出来颜色一样；
    想换一套配色就重新生成结构（代次变了）。
    """
    base = {
        "grade": (fx.get("grade") or "").strip(),
        "int": _num(fx.get("grade_int"), 0.65),
        "lut": (fx.get("lut") or "").strip(),
        "mirror": bool(fx.get("mirror")),
        # 质感四项的缺省走 FX_DEFAULTS（出厂即开在无感档）：
        # 前端没传、老项目数据没有这几个字段时，渲染出来的画面和默认面板一致
        "stretch": _num(fx.get("stretch"), FX_DEFAULTS["stretch"]),
        "glow": _num(fx.get("glow"), FX_DEFAULTS["glow"]),
        "grain": _num(fx.get("grain"), FX_DEFAULTS["grain"]),
        "vignette": _num(fx.get("vignette"), FX_DEFAULTS["vignette"]),
    }
    if base["lut"]:
        return base          # 用户指定 LUT，就按他挑的来，不参与随机
    # 兼容老数据：以前是 grade_rand 勾选框 + rand_sec 固定分钟数
    if base["grade"] == "rand" or fx.get("grade_rand"):
        is_rand = True
    else:
        is_rand = False
    if not is_rand:
        return base
    # 每区时长：老数据只有 rand_sec（固定），新数据有 min/max（随机区间）
    legacy = _num(fx.get("rand_sec"), 180.0)
    lo = min(900.0, max(30.0, _num(fx.get("rand_min"), legacy)))
    hi = min(900.0, max(30.0, _num(fx.get("rand_max"), legacy)))
    if hi < lo:
        lo, hi = hi, lo
    if not fx.get("rand_min") and not fx.get("rand_max"):
        lo = hi = legacy   # 老数据：固定分区长度，保持原样
    import random as _random
    # ① 先按种子把分区边界铺出来（长度随机），看这一块落在第几区。
    #    按顺序抽长度、抽到超过 out_start 就停：前面的抽取序列是同一前缀，
    #    所以不同块算出来的分区边界完全一致。
    zrng = _random.Random(seed + "|z")
    bound, zone = 0.0, 0
    while zone < 400:
        nxt = bound + zrng.uniform(lo, hi)
        if out_start < nxt:
            break
        bound, zone = nxt, zone + 1
    # ② 再按区顺序重放随机流选档：每次从「上一区之外的档」里抽。
    #    四选一纯随机实测会连着三区都抽到冷调，观众看到的是「前九分钟一个色」。
    rng = _random.Random(seed)
    last = None
    for _r in range(zone + 1):
        pool = [k for k in GRADE_KEYS if k != last] or list(GRADE_KEYS)
        last = rng.choice(pool)
    base["grade"] = last
    # 强度围绕用户设的基准浮动 ±0.18，再夹到可用区间：
    # 全随机（0-1）会出现一片几乎没调、一片调到发紫，反而像出错
    base["int"] = min(1.0, max(0.2, base["int"] + rng.uniform(-0.18, 0.18)))
    return base

def fp_spec(level: str, seed: str, pos: float | None = None) -> dict:
    """这一套的指纹随机参数（整套统一，不按段变）。

    为什么一套只抽一次：每段的旋转角/取景偏移都不一样的话，段与段的接缝处
    构图会轻微跳一下，十几段看下来像画面在抖。整套一组参数才像「另一版成片」。

    种子由调用方给（项目+套+代次+本次出片批次），同一批里重渲结果稳定，
    不同套之间必然不同，换一批出片就是另一组。

    pos（0-1，可选）：放大倍率在区间里的落点。纯均匀抽会撞车——两套抽到
    1.030 / 1.031，裁出来的画面差不到 1 像素，等于没做。调用方按套号铺开
    （见 batch 里的黄金分割），同一批十几套就能把区间占满。
    """
    lv = FP_LEVELS.get(str(level or "").strip().lower()) or FP_LEVELS[FP_DEFAULT]
    r = random.Random(f"{seed}|fp")
    lo, hi = lv["zoom"]
    if pos is None:
        z = r.uniform(lo, hi)
    else:
        # 落点本身再抖一点，避免"第 1 套永远最小、第 20 套永远最大"的规律感
        f = (float(pos) + r.uniform(-0.12, 0.12)) % 1.0
        z = lo + f * (hi - lo)
    return {
        "zoom": z,
        "rot": r.uniform(-1.0, 1.0) * lv["rot"],
        "sx": r.uniform(-1.0, 1.0) * lv["shift"],
        "sy": r.uniform(-1.0, 1.0) * lv["shift"],
        "gain": r.uniform(-1.0, 1.0) * lv["gain"],
        "d_stretch": r.uniform(-1.0, 1.0) * lv["spm"],
        "k_glow": 1.0 + r.uniform(-1.0, 1.0) * lv["tpm"] * 0.6,
        "k_grain": 1.0 + r.uniform(-1.0, 1.0) * lv["tpm"],
        "k_vignette": 1.0 + r.uniform(-1.0, 1.0) * lv["tpm"] * 0.8,
        # ── 指纹新维度（2026-11-22）：全部挂同一颗种子，soft 档一律回退到「不加」
        "fps_out": FPS if not lv.get("cn") else round(r.uniform(*FPS_ADAPT), 3),
        "cn_s": float(lv.get("cn") or 0),
        "cn_seed": r.randrange(1, 2 ** 31),
        "lens_k1": r.uniform(-1.0, 1.0) * float(lv.get("lens") or 0.0),
        "lens_k2": r.uniform(-1.0, 1.0) * float(lv.get("lens") or 0.0) * 0.6,
        "sharp": r.choice(("3", "5")) if lv.get("lens") else "",
        "k_eqw": float(lv.get("eqw") or 0.0),
    }


def fp_grade(g: dict, fp: dict | None, seg_index: int | None = None,
             seed: str = "") -> dict:
    """把指纹浮动叠到这一块的画面参数上（原地改 g 并返回）。

    只浮动用户已经开着的项：质感滑杆拉到 0 表示「不要」，随机不能把它加回来。
    横向变形同理——100%（不拉伸）的人不该被随机推成 101%。

    seg_index（2026-11-17）：给了就再叠一层**逐段**的质感微抖（±10%）。
    为什么只抖质感、不抖几何：zoom/rot/sx/sy 逐段变会让段与段的接缝构图跳一下，
    十几段看下来像画面在抖（这也是 fp_spec 坚持整套一组的原因）；而颗粒/暗角/
    光晕是叠在画面上的质感，逐段差 10% 肉眼看不出来，却让「同一套里每段参数
    完全相同」这件事不再成立——帧级比对要抓规律就更难。
    """
    if not fp:
        return g
    try:
        st = float(g.get("stretch") or 1.0)
    except (TypeError, ValueError):
        st = 1.0
    if abs(st - 1.0) > 0.002:
        g["stretch"] = max(0.94, min(1.06, st + float(fp.get("d_stretch") or 0.0)))
    for k, kk in (("glow", "k_glow"), ("grain", "k_grain"),
                  ("vignette", "k_vignette")):
        try:
            v = float(g.get(k) or 0.0)
        except (TypeError, ValueError):
            v = 0.0
        if v > 0.004:
            g[k] = max(0.0, min(1.0, v * float(fp.get(kk) or 1.0)))
    if seg_index is not None:
        jr = random.Random(f"{seed}|tex|{int(seg_index)}")
        for k in ("glow", "grain", "vignette"):
            try:
                v = float(g.get(k) or 0.0)
            except (TypeError, ValueError):
                continue
            if v > 0.004:      # 用户关掉的项不抖，0 还是 0
                g[k] = max(0.0, min(1.0, v * (1.0 + jr.uniform(-0.10, 0.10))))
    # 动态调色摆动参数（2026-11-22）：只生成参数，_grade_filters 在「调色预设
    # 真的产生了 eq」的分支才会用——这里不判断是否开调色，保持本函数只管
    # 指纹参数的分工。逐段抽频率/相位，段间波形不同但摆幅一致（±2% 内无感）。
    k_eqw = float(fp.get("k_eqw") or 0.0)
    if k_eqw > 0.01:
        wr = random.Random(f"{seed}|eqw|{int(seg_index) if seg_index is not None else 'all'}")
        g["eq_wobble"] = {
            "sat": 0.020 * k_eqw, "con": 0.015 * k_eqw,
            "f_sat": wr.uniform(0.35, 0.90), "f_con": wr.uniform(0.25, 0.80),
            "p_sat": wr.uniform(0.0, 6.283), "p_con": wr.uniform(0.0, 6.283),
        }
    return g


def auto_seg_view(fp: dict, seg_index: int, seed: str) -> dict:
    """全自动随机：给这一段单独抽一组几何参数（在整套基准上做无感级微抖）。

    返回一份新的 fp dict，不改动传进来的整套 fp。放大/变形各段不同但幅度极小
    （±1% / ±0.6%），rot/sx/sy 保持整套一致——几何主结构不动，只把"每段像素级
    指纹"错开，接缝处看不出跳。关掉画面放大时 fp 为空，这里直接返回空。
    """
    if not fp:
        return {}
    out = dict(fp)
    try:
        base = float(fp.get("zoom") or 1.0)
        if base > 1.0005:
            jr = random.Random(f"{seed}|azoom|{int(seg_index)}")
            out["zoom"] = max(1.001, base * (1.0 + jr.uniform(-AUTO_ZOOM_AMP, AUTO_ZOOM_AMP)))
    except (TypeError, ValueError):
        pass
    try:
        d = float(fp.get("d_stretch") or 0.0)
        jr = random.Random(f"{seed}|astretch|{int(seg_index)}")
        out["d_stretch"] = d + jr.uniform(-AUTO_STRETCH_AMP, AUTO_STRETCH_AMP)
    except (TypeError, ValueError):
        pass
    return out


def auto_ep_pick(ep_key, seed: str) -> dict:
    """全自动随机：按「集」决定这一集要不要左右镜像、字幕用哪种出场动效。

    ep_key 用集号（同一集的多段拿到同一个结果，不会一段翻一段不翻）。
    mirror 只给「可以镜像」的片子用——调用方负责按素材判断能不能翻。
    """
    key = str(ep_key)
    mr = random.Random(f"{seed}|amirror|{key}")
    sr = random.Random(f"{seed}|amotion|{key}")
    return {
        "mirror": mr.random() < AUTO_MIRROR_P,
        "newsub_style": sr.choice(AUTO_MOTION_STYLES),
        "newsub_motion": round(sr.uniform(*AUTO_MOTION_RANGE), 4),
    }


def direct_mirror_plan(start: float, end: float, seed: str) -> str | bool:
    """选视频直接出片专用的镜像随机：在**一条片子内部按时间窗**抛硬币。

    和 auto_ep_pick 的区别（别混）：那个按「集」翻、整集一致，给 AI 编辑出片用；
    这里直出没有集（一个视频一整段），所以按 DIRECT_MIRROR_WIN 秒一段，逐段独立
    决定翻不翻。返回 ffmpeg 的 hflip 时间线 enable 表达式；全部不翻返回 False
    （调用方据此不镜像），全部都翻返回 True（省掉 enable，整段直接翻）。

    时间线用**帧号 n** 而不是秒 t：-ss 之后滤镜的 n 从 0 开始数，且 hflip 在
    setpts 变速之前，n 对应源片帧位，不受变速压缩；字幕在 hflip 之后才烧，
    所以翻的是画面、字幕仍是正的（现场换字这条路的立身之本）。
    窗口边界按源片秒数换算成帧，最后一窗不足一个整窗也单独抛一次。
    """
    dur = max(0.0, float(end) - float(start))
    if dur <= 0:
        return False
    total_frames = max(1, int(round(dur * FPS)))
    win_frames = max(1, int(round(DIRECT_MIRROR_WIN * FPS)))
    bands: list[str] = []
    flips = 0
    wins = 0
    n = 0
    while n < total_frames:
        hi = min(n + win_frames, total_frames)
        wins += 1
        # 种子含窗口序号：每次出片换 run_id → 整批重抽；同一批内同一段稳定复现
        if random.Random(f"{seed}|dmirror|{wins}").random() < AUTO_MIRROR_P:
            bands.append(f"between(n,{n},{hi - 1})")
            flips += 1
        n = hi
    if flips == 0:
        return False
    if flips == wins:
        return True
    return "hflip=enable='" + "+".join(bands) + "'"


def speed_plan(durations: list[float], base_speeds: list[float] | None = None,
               fps: int = FPS, seed: str = "", level: str = "") -> list[dict]:
    """逐段微变速计划（帧数守恒）。返回 [{speed, frames, out_dur}]，与输入等长。

    三步约束见文件头 FP_SPEED 的注释。durations 是**源素材**时长，
    base_speeds 是原本就要的倍速（缓冲段 1.15），微抖是在它之上乘一个系数。

    边界情况一律退回基准速度：宁可不扰，也不能让某段算出 0 帧或负数时长——
    那会让 concat 当场失败，整套片子白渲。
    """
    n = len(durations or [])
    if n == 0:
        return []
    dur = [max(0.1, float(d or 0.1)) for d in durations]
    base = []
    for b in (base_speeds or [1.0] * n):
        try:
            base.append(min(2.0, max(0.5, float(b or 1.0))))
        except (TypeError, ValueError):
            base.append(1.0)
    if len(base) != n:
        base = (base + [1.0] * n)[:n]

    amp = float(FP_SPEED.get(str(level or "").strip().lower(),
                             FP_SPEED[FP_DEFAULT]))
    rng = random.Random(f"{seed}|spd")
    d0 = [d / b for d, b in zip(dur, base)]        # 基准输出时长（不含微抖）
    total = sum(d0)
    if total <= 0:
        return [{"speed": round(base[i], 6), "frames": max(MIN_FRAMES, int(round(dur[i] * fps / base[i]))),
                 "out_dur": round(max(MIN_FRAMES, int(round(dur[i] * fps / base[i]))) / fps, 6)}
                for i in range(n)]

    mul = [1.0 + rng.uniform(-amp, amp) for _ in range(n)]
    # ① 相邻限幅：差不足半个振幅就推到刚好拉开，别让两段速度落在同一档
    step = amp * 0.5
    for i in range(1, n):
        if abs(mul[i] - mul[i - 1]) < step:
            mul[i] = mul[i - 1] + step if mul[i] >= mul[i - 1] else mul[i - 1] - step
    mul = [min(1.0 + 2 * amp, max(1.0 - 2 * amp, m)) for m in mul]

    # ② 总量归一：Σ d0/m 拉回 Σ d0（因子乘在 m 上，等价于所有段同比例伸缩）
    denom = sum(x / m for x, m in zip(d0, mul))
    if denom <= 0:
        mul = [1.0] * n
    else:
        k = denom / total
        mul = [m * k for m in mul]

    # ③ 分帧 + 余帧摊平
    frames = [max(MIN_FRAMES, int(round(d0[i] * fps / mul[i]))) for i in range(n)]
    target = max(n * MIN_FRAMES, int(round(total * fps)))
    diff = target - sum(frames)
    guard = 0
    while diff and guard < 100000:
        j = guard % n
        if diff > 0:
            frames[j] += 1
            diff -= 1
        elif frames[j] > MIN_FRAMES:
            frames[j] -= 1
            diff += 1
        guard += 1

    out: list[dict] = []
    for i in range(n):
        out_dur = frames[i] / float(fps)
        speed = dur[i] / out_dur if out_dur > 0 else base[i]
        out.append({
            "speed": round(speed, 6),
            "frames": frames[i],
            "out_dur": round(out_dur, 6),
        })
    return out


# ── 配乐 BGM（2026-11-05）────────────────────────────────────────────────
# 用户在出片设置里勾选「配乐 BGM」并选一首自己电脑里的音乐，渲染收尾混进成片。
# 音量控制是这一步的全部难点，四层保险：
#   ① 响度归一（2026-11-18 新增，最关键）：曲目之间的响度差能到 6-8 LU——
#      曲库里占位合成音 −23.1 LUFS、真人音乐 −19.1 LUFS，同一个滑杆档位听起来
#      差一倍。混音前先把每首拉到 BGM_TARGET_LUFS，滑杆的百分比才有统一语义。
#   ② 基础音量压低：默认 0.35（用户滑杆 5%-60%）；
#   ③ 侧链闪避（sidechaincompress）：拿成片的人声当侧链，台词一响音乐就被
#      压下去、台词停了音乐再浮上来——这是「不盖台词」的根本保证，光靠
#      调低音量在安静段会把音乐衬得刺耳、在爆点段又被台词盖没；
#   ④ 结尾淡出 2.5 秒 + 开头淡入 1 秒：音乐戛然而止比没音乐更出戏。
# amix 必须 normalize=0：默认会把两路各乘 1/2，人声凭空小一半。
# -stream_loop -1 让音乐循环铺满全片（amix duration=first 到人声长度为止）。
#
# 2026-11-18 实测教训（用户报「明明设了 BGM 却没有」）：旧参数 0.22 + 闪避
# ratio=8/threshold=0.03，挑到 −23 LUFS 的占位音时，BGM 落在 −38 dB，台词段
# 再被压 11 dB 到 −49 dB，而人声床是 −18 dB——相差 30 dB，波形相关分析显示
# 「静场 r=1.00、台词段 r=0.03」，等于整片没有配乐。所以默认音量抬到 0.35，
# 闪避改成 ratio=4/threshold=0.08（台词下只压 4-5 dB），并加响度归一。
BGM_VOL_DEFAULT = 0.35
BGM_FADE_OUT = 2.5
BGM_TARGET_LUFS = -18.0
# 混音器版本位：改上面的音量/闪避/归一化口径时必须 +1，它会进 fx 指纹，
# 让上一版参数出过的成片被判成「不是这次要的结果」而重渲。
BGM_MIX_VER = "M1"
# threshold=0.08：人声响度归一（loudnorm -16 LUFS）后超过这个电平就算「在说话」；
# ratio=4 只压 4-5 dB，够让台词清楚又不把音乐压没；attack=20ms 跟得上台词起音，
# release=400ms 收得快点，句间音乐不会一直塌着。
BGM_DUCK = "threshold=0.08:ratio=4:attack=20:release=400:makeup=1"

# ── 显卡硬件加速（2026-11-05）────────────────────────────────────────────
# 切段是最花时间的环节（20 段 × libx264 veryfast），硬件编码器通常快 2-4 倍。
# 四个坑决定了实现方式：
#   ① 编码器「注册了」≠「能用」——ffmpeg 二进制里三种都编进去了，但没有对应
#      显卡照样报错。必须真编一段（4 秒 720x1280）才算数，别信 `-encoders` 列表；
#   ② 「能跑」≠「更快」——本机实测 QSV 编 8 秒反而比软编慢 0.7x：每段都是
#      独立 ffmpeg 进程，硬件会话初始化开销在小段上吃掉全部收益。所以探测
#      就是基准测试：硬件必须比软编快 ≥10% 才启用，否则老实用软编；
#   ③ 结果按进程缓存，一次出片 20 段只测一次；render_edl 开头预热，探测的
#      几秒钟不计入第一段进度；
#   ④ 驱动中途重置、显存不够这类「测过了、用着用着挂了」的情况，允许整段
#      退回软编重试一次（_run_v），并把硬件开关永久关掉——宁慢勿败。
# 质量对位：libx264 crf18 ≈ NVENC cq19 / QSV global_quality 22 / AMF cqp 20-23。
_HW: dict = {"det": None, "off": False, "t_sw": None}
# det: None=没测过 / {}=软编更快或没硬件 / {...}=启用哪个硬件
_HW_PARAMS = {
    # NVIDIA：p5+hq 档质量接近 veryfast 软编的观感，cq 恒定质量省码率
    "h264_nvenc": (["-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "19",
                    "-b:v", "0", "-spatial-aq", "1"], "yuv420p"),
    # Intel 核显：global_quality 22 对位 crf 18
    "h264_qsv": (["-preset", "medium", "-global_quality", "22"], "nv12"),
    # AMD：cqp 两档最稳，参数复杂的花样探错一次就再也用不上了
    "h264_amf": (["-quality", "balanced", "-rc", "cqp", "-qp_i", "20", "-qp_p", "23"],
                 "yuv420p"),
}
# 基准片段：与真实切段同尺寸同帧率，4 秒够稳定又不拖探测
_BENCH_SRC = "testsrc=size=720x1280:rate=30:duration=4"
_SW_ARGS = ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
# 硬件至少要比软编快这个比例才值得用（省下的时间必须盖过会话初始化开销）
_HW_MIN_GAIN = 0.90


def _time_encode(vargs: list[str]) -> float:
    """编一段基准片计时。返回秒数；编码失败返回 -1。

    **必须带超时**（30 秒）：终端用户的显卡驱动可能坏得千奇百怪——探测
    不报错但永远不返回是最恶心的一种，没有超时整个出片任务就挂死在
    「准备渲染」上，用户只能杀进程。"""
    cmd = [core.ff("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", _BENCH_SRC, *vargs, "-f", "null", "-"]
    t0 = time.perf_counter()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                           **core.POPEN_HIDE)
    except (OSError, subprocess.TimeoutExpired):
        return -1.0
    if r.returncode != 0:
        return -1.0
    return time.perf_counter() - t0


# 基准结果落盘缓存：基准一次要几秒（三种编码器各编一段 4 秒片），exe 每次启动
# 都重测太浪费。显卡/驱动不常换，14 天内直接复用上次的结论；驱动升级后结论
# 过期自然重测，中途失效（_run_v 退软编）也会把缓存清掉。
_HW_CACHE_FILE = os.path.join(core.DATA_DIR, "hw_cache.json")
_HW_CACHE_TTL = 14 * 24 * 3600


def _hw_mode() -> str:
    """设置里的硬编码模式：auto（默认，测过快才用）/ off（强制 CPU）。

    每台电脑自己的偏好——有的机器显卡要留给直播/游戏，用户有权说不用。"""
    try:
        m = str((core.load_config() or {}).get("hw_encode") or "auto").strip().lower()
    except Exception:
        return "auto"
    return m if m in ("auto", "off") else "auto"


def _hw_cache_load() -> dict | None:
    try:
        with open(_HW_CACHE_FILE, encoding="utf-8") as f:
            c = json.load(f) or {}
        if time.time() - float(c.get("ts") or 0) > _HW_CACHE_TTL:
            return None
        det = c.get("det")
        # 缓存里的编码器必须是还认识的（程序升级删了某档也不至于用错参数）；
        # {} 表示「上次测过、没有更快的硬件」，同样是有效结论
        if isinstance(det, dict) and (not det or det.get("name") in _HW_PARAMS):
            return det
    except Exception:
        pass
    return None


def _hw_cache_save(det: dict) -> None:
    try:
        with open(_HW_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"det": det, "ts": time.time()}, f)
    except Exception:
        pass  # 缓存写不进去就每次重测，功能不受影响


# 状态页透出用的人话标签（2026-11-22）：键是 _HW_PARAMS 的编码器名
_HW_LABELS = {"h264_nvenc": "N 卡 NVENC", "h264_qsv": "Intel 核显 QSV", "h264_amf": "AMD AMF"}


def hw_encoder_label() -> str | None:
    """当前出片实际用的编码器（给人看的标签）：None=尚未探测、""=CPU 软编、
    否则是「N 卡 NVENC」这类标签。

    **只读落盘缓存，绝不触发基准探测**——探测要真编好几秒片子，挂在
    /api/status 会把一个轻量轮询接口拖成重接口。没测过/缓存过期/中途失效
    都返回 None，前端显示「尚未探测」。
    """
    if _hw_mode() == "off":
        return ""
    det = _hw_cache_load()
    if not det:
        return None
    name = str(det.get("name") or "")
    return _HW_LABELS.get(name) or (name or "")


def _hw_cache_clear() -> None:
    try:
        os.remove(_HW_CACHE_FILE)
    except OSError:
        pass


def _detect_hw() -> dict:
    """基准测试式探测：能用**且**比软编快 10% 以上的第一个硬件编码器。

    结果按进程缓存 + 落盘（14 天），设置里选「关闭」则跳过一切探测。"""
    if _HW["det"] is None:
        if _hw_mode() == "off":
            det: dict = {}
        else:
            det = _hw_cache_load()
            if det is None:
                det = {}
                if _HW["t_sw"] is None:
                    _HW["t_sw"] = _time_encode(_SW_ARGS)
                if _HW["t_sw"] > 0:
                    for name, (args, pix) in _HW_PARAMS.items():
                        t_hw = _time_encode(["-c:v", name, *args, "-pix_fmt", pix])
                        # 硬件编 4 秒基准片都要比软编快才配启用；否则就是伪加速
                        if 0 < t_hw < _HW["t_sw"] * _HW_MIN_GAIN:
                            det = {"name": name, "args": list(args), "pix": pix}
                            break
                _hw_cache_save(det)
        _HW["det"] = det
    return _HW["det"]


def _venc_args() -> list[str]:
    """当前该用的视频编码参数：硬件赢了基准、没被中途关掉、设置没关，才用硬件。"""
    if _hw_mode() == "off":
        return list(_SW_ARGS)
    det = _detect_hw()
    if det and not _HW["off"]:
        return ["-c:v", det["name"], *det["args"], "-pix_fmt", det["pix"]]
    return list(_SW_ARGS)


# ── 流级标记（2026-11-22，借鉴零界的码流 SEI 注入；默认关）────────────────
# H.264 码流里可以带一条自定义 SEI user data（bsf 与编码器无关，NVENC/QSV/
# AMF 软编都能挂）。作用：给成片一个**不影响画面**的流级差异维度。默认关，
# 只对懂配置的用户开放（data/config.json 里加 "sei_tag"）。
# 为什么只在最终封装挂一次：切段每一趟都挂的话 bsf 会在 concat copy 时原样
# 保留，没必要重复；封装是唯一收口，两条渲染分支（有无帧数计划）都汇到这。
_SEI_UUID = "0f5a3c8e2b7d4a196e6c4d2f8b1a3c5d"   # 固定标识（16 字节 hex），不可改


def _sei_args() -> list[str]:
    """按配置返回 H.264 SEI user data 参数；没配或配置不合法返回空（= 不挂）。"""
    try:
        tag = str((core.load_config() or {}).get("sei_tag") or "").strip()
    except Exception:  # noqa: BLE001
        return []
    # 只许安全字符、限 32 字节：SEI payload 会原样进码流，混入引号/空格
    # 可能产生畸形流；超长的也没意义。
    if not tag or len(tag.encode("utf-8")) > 32 or not re.fullmatch(r"[\w\-.]+", tag):
        return []
    return ["-bsf:v", f"h264_metadata=sei_user_data={_SEI_UUID}+{tag}"]


# ── 音频适配（2026-11-22，独立开关默认关）─────────────────────────────────
# 开 = 出片收尾对整条音轨做「保时长纯变调 + 频谱微倾」：asetrate 把采样率
# 抬到 k×48000（音调 ×k、时长 ÷k），atempo=1/k 把时长拉回来（音调不动），
# 两步互为倒数 → **时长严格不变**，音画同步不受影响；末尾 aresample 回 48k
# 交给 aac。再叠 bass/treble 一对反向增益做频谱微倾。每套随机抽 k 和倾角，
# 挂 fp_seed 同一颗种子（重跑可复现）。
# 为什么幅度压在 ±1.5%：台词是本工具的核心，零界敢开 ±6% 是因为它不保台词
# 自然度；±1.5% 在人耳的「同一个人说话」判断之内。
# 为什么放在最终封装而不是 _audio_piece 逐段做：① 一个插入点同时覆盖帧数/
# 非帧数两条渲染分支；② 整套一个音高才像「同一版成片」，逐段换调反而像故障。
# BGM 混音（_mix_bgm）在这之后，不在变调范围——原声轨是比对对象，曲库音乐不是。
def _audio_armor_on() -> bool:
    try:
        return bool((core.load_config() or {}).get("audio_armor"))
    except Exception:  # noqa: BLE001
        return False


def _audio_armor_chain(pitch_k: float, tilt_db: float) -> list[str]:
    """音频适配的 -af 片段（loudnorm 之前）。k≈1 时返回空 = 链路与现状一致。"""
    if abs(pitch_k - 1.0) < 5e-4:
        return []
    return [f"asetrate={48000 * pitch_k:.2f}", f"atempo={1.0 / pitch_k:.6f}",
            "aresample=48000",
            f"bass=g={tilt_db:+.2f}", f"treble=g={-tilt_db:+.2f}"]


def _run_v(build, on_frame=None) -> None:
    """跑一条带视频编码的命令。硬件编码器中途翻车（驱动重置/显存不够/RDP
    会话把 GPU 收走）时整套退回软编重试一次并永久关闭硬件——一张卡坏掉
    不能让 20 段白切。同时清掉基准缓存：这次实测已经证明缓存结论不可信，
    别让下次启动还拿它开硬件。"""
    det = _detect_hw()
    used_hw = bool(det) and not _HW["off"] and _hw_mode() == "auto"
    try:
        _run(build(), on_frame)
    except RuntimeError as exc:
        if not used_hw or "磁盘空间不足" in str(exc):
            raise
        _HW["off"] = True
        _hw_cache_clear()
        if on_frame:
            on_frame(0)     # 软编重来：进度从头计，别让条子卡在半路不动
        _run(build(), on_frame)


# ── ASS 动态字幕（2026-11-05）────────────────────────────────────────────
# 看点标题和钩子卡的文字从 drawtext 静态字升级成 ASS 动效（淡入＋滑入），
# 靠的是捆绑 ffmpeg 自带的 libass（subtitles 滤镜）——不加任何新依赖。
# 能力探测也按进程缓存；没有 libass 时自动退回旧的 drawtext 路径，两版观感
# 只差动效，不会坏。
_HAS_LIBASS: bool | None = None

_ASS_HEADER = (
    "[Script Info]\n"
    "ScriptType: v4.00+\n"
    # PlayRes 与成片尺寸一致， ASS 坐标就是像素坐标，字号不用二次换算
    "PlayResX: 720\n"
    "PlayResY: 1280\n"
    "WrapStyle: 2\n"
    "ScaledBorderAndShadow: yes\n"
    "\n"
    "[V4+ Styles]\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
    "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
    "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
)

_ASS_EVENTS = (
    "[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
)

_FONTS_DIR = "C:/Windows/Fonts"
_ASS_FONT = "Microsoft YaHei"


def _has_libass() -> bool:
    global _HAS_LIBASS
    if _HAS_LIBASS is None:
        try:
            r = subprocess.run([core.ff("ffmpeg"), "-hide_banner", "-filters"],
                               capture_output=True, text=True, **core.POPEN_HIDE)
            _HAS_LIBASS = "subtitles" in (r.stdout or "")
        except OSError:
            _HAS_LIBASS = False
    return _HAS_LIBASS


def _ass_time(sec: float) -> str:
    """ASS 时间戳 H:MM:SS.CC（厘秒）。"""
    cs = max(0, int(round(sec * 100)))
    return f"{cs // 360000}:{(cs // 6000) % 60:02d}:{(cs // 100) % 60:02d}.{cs % 100:02d}"


def _ass_text(t: str) -> str:
    """文本进 ASS Dialogue 的转义：花括号是标签语法必须清掉，换行变 \\N。"""
    return (t or "").replace("{", "（").replace("}", "）").replace("\r", "").replace("\n", "\\N")


def _write_ass(path: str, style: str, dialogue: str) -> None:
    # newline="\n" 必须显式给：Windows 默认 \r\n，libass 会把 \r 当内容渲染
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(_ASS_HEADER + style + "\n" + _ASS_EVENTS + dialogue)


def _title_ass_file(text: str, dur: float, path: str) -> None:
    """看点标题的动效版：淡入 0.28s + 从上往下滑 12px 落位。样式对齐旧
    drawtext 版（白字 + 45% 黑底 + 顶部 5.5%），只是加了动效。"""
    size = min(34, max(21, (W - 80) // max(4, len(text))))
    # BorderStyle=3 的底框颜色取 OutlineColour；&H72 前缀 alpha≈0.45 对位 boxcolor=black@0.45
    style = (f"Style: Title,{_ASS_FONT},{size},&H00FFFFFF,&H00FFFFFF,&H72000000,"
             f"&H72000000,0,0,0,0,100,100,0,0,3,14,0,8,20,20,70,1\n")
    top = round(H * 0.055)
    dlg = (f"Dialogue: 0,{_ass_time(0)},{_ass_time(dur)},Title,,0,0,0,,"
           f"{{\\fad(280,0)}}{{\\move({W // 2},{top + 12},{W // 2},{top},0,260)}}"
           f"{_ass_text(text)}\n")
    _write_ass(path, style, dlg)


def _hook_ass_file(lines: list[str], size: int, dur: float, path: str) -> None:
    """钩子卡文字动效：淡入 0.42s + 从下往上浮 16px。粗体、细描边（压暗的
    模糊背景上白字对比足够，描边只为边缘干净）。"""
    style = (f"Style: Hook,{_ASS_FONT},{size},&H00FFFFFF,&H00FFFFFF,&HC8000000,"
             f"&HC8000000,1,0,0,0,100,100,0,0,1,2,0,5,20,20,0,1\n")
    body = _ass_text("\n".join(lines))
    cy = H // 2
    dlg = (f"Dialogue: 0,{_ass_time(0)},{_ass_time(dur)},Hook,,0,0,0,,"
           f"{{\\fad(420,250)}}{{\\move({W // 2},{cy + 16},{W // 2},{cy},0,350)}}"
           f"{body}\n")
    _write_ass(path, style, dlg)


def _subs_filter(ass_path: str) -> str:
    """subtitles 滤镜片段。fontsdir 指到系统字体目录，libass 按「微软雅黑」
    字族名取字，不依赖 fontconfig。"""
    return (f"subtitles=filename='{_esc_filter_path(ass_path)}'"
            f":fontsdir='{_esc_filter_path(_FONTS_DIR)}'")


# ── 自己的字幕（2026-11-11：最终改为在第 2 步那一趟编码里烧进副本）──────
# 演变：先做过「切段 post 链叠字」（不烧素材），用户最终要的是「第 2 步一步
# 产出带自己字幕的成品素材」，于是改成 nosub.py 里同趟叠。烧进像素的三个
# 代价都换了别的手段兜：
#   ① 改一句台词要重做 → nosub.sig_of 把每集 lines 的 sha1 并入指纹；
#   ② 出片镜像会把字翻反 → main._prepared_mirror_conflict 出片前拦；
#   ③ 第 3/4 步复用素材也被带上字 → 出片设置第 ⑤ 组明示「用哪份视频」。
# 本文件只保留生成器（cap_rows / _cap_ass_file / _cap_drawtext_filters /
# newsub_spec）供 nosub 复用；_cut 的 cap= 参数已无调用方。
#
# 动效为什么是「句内落位 + 句间轻微交替」，不是整层持续上下漂：
#   短剧每句 1–3 秒、密度高，全程运动迫使眼睛反复重新对焦，看几分钟就累。
#   字幕的第一任务是被读：淡入给动感、落位后静止保证稳定、淡出避免硬闪。
#   每条 Dialogue 的动效基准是它自己的 Start/End，跟切段边界无关——
#   所以切段 ffmpeg 的 t 从 0 起算也不会让字幕位置在切点跳回起点。
NEWSUB_DEFAULTS = {"pos": 0.80, "motion": 0.5, "style": "rise"}
# 2026-11-16 删掉「字幕比声音晚」这一项：真正治「字比声音先出来」的是
# retime（拿原片字幕当真值逐句对时）。微调只是把偏差往另一个方向推，准的集
# 会被推晚，留着还会让人以为得手动补偏移。现在出场时刻＝台词时间码本身。
# 新字幕的出场动效。原来只有一条 0-100 的「动感」滑杆，实际只有三档
# （静止 / 落位 / 漂浮），用户看不出自己在选什么，也没法要「弹出」「逐字」。
# 现在给成枚举：每种风格对应一串确定的 ASS 标签，观感可预期。
NEWSUB_STYLES = {
    "fade": "淡入淡出",
    "rise": "上浮落位",
    "pop": "缩放弹出",
    "float": "缓缓漂浮",
    "type": "逐字打出",
}
# 注意 newsub_pos 是**字幕中心**高度，不是 nosub.pos（原来那行字幕的带上沿）。
# 实测帧：pos=0.75 时新字幕正好压在旧字幕上沿、两行贴在一起；带中心
# = pos + size/2 = 0.75 + 0.05 = 0.80 才是「原来那行字幕的位置」。
# 2026-11-11 二次改版：这里只给 app/nosub.py（第 2 步）用——字幕在那一趟
# 编码里就烧进副本了，出片不再从 fx 读 newsub*。


def newsub_spec(fx: dict) -> dict:
    """把「加回自己的字幕」参数夹到安全区间。关着就返回 {"on": False}。"""
    fx = fx if isinstance(fx, dict) else {}
    if not bool(fx.get("newsub")):
        return {"on": False}

    def num(key: str, d: float, lo: float, hi: float) -> float:
        try:
            v = fx.get(key)
            return min(hi, max(lo, float(d if v is None or v == "" else v)))
        except (TypeError, ValueError):
            return d

    m = num("newsub_motion", NEWSUB_DEFAULTS["motion"], 0.0, 1.0)
    style = str(fx.get("newsub_style") or "").strip().lower()
    if style not in NEWSUB_STYLES:
        # 老项目没有 style 字段：按原来的 motion 值折算，出来的观感不变
        style = "float" if m >= 0.9 else ("fade" if m <= 0.01 else "rise")
    return {"on": True,
            "pos": num("newsub_pos", NEWSUB_DEFAULTS["pos"], 0.50, 0.92),
            "motion": m, "style": style}


def cap_rows(lines: list[dict], seg_start: float, seg_end: float,
             speed: float) -> list[list]:
    """集内绝对时间 → 段内（变速后）时间轴上的 (起, 止, 文本)。

    -ss 放在 -i 前，滤镜看到的 t 已经是段内 0 起；pre 里的 setpts=PTS/speed
    又把整条时间轴压成 1/speed，所以两个时间码都要减 seg_start 再除 speed。
    不这么做的话，缓冲段加速后字幕会整段落后于口型。
    相邻句重叠（转写碎片常见）时把前一句收住，避免两句叠在一起。
    """
    sp = speed if speed and speed > 0 else 1.0
    out: list[list] = []
    for ln in lines or []:
        if not isinstance(ln, dict):
            continue
        try:
            s, e = float(ln.get("start") or 0.0), float(ln.get("end") or 0.0)
        except (TypeError, ValueError):
            continue
        if e <= s or s >= seg_end or e <= seg_start:
            continue
        a, b = max(0.0, (s - seg_start) / sp), max(0.0, (e - seg_start) / sp)
        text = " ".join(str(ln.get("text") or "").split())
        if b <= a or not text:
            continue
        out.append([a, b, text])
    out.sort(key=lambda r: (r[0], r[1]))
    for i in range(len(out) - 1):
        if out[i][1] > out[i + 1][0] - 0.02:
            out[i][1] = max(out[i][0] + 0.05, out[i + 1][0] - 0.02)
    return out


# 字号 ↔ 视觉字高的标定（2026-11-12 实测，灰底截图量的白字上下沿）：
#   fs  36  40  44  48  52  56  60
#   高  26  29  31  35  38  40  43    → 线性拟合 h = 0.7143·fs + 0.286
# 成片是 720×1280（PlayRes 与之一致，1 单位 = 1 像素），所以
#   fs = 原字幕带高(归一化) × 1280 / 0.7143 ≈ × 1792
# 再补 1~2px 描边余量（检测出的「文字带」只到字形外沿，不含描边），取 1880。
_CAP_FS_PER_H = 1880.0
_CAP_FS_MIN, _CAP_FS_MAX = 26, 62
# 中文字宽 ≈ 0.75 字号（同一批实测：fs=52 → 每字 39px）。折行数和宽度兜底都按它算。
_CAP_CH_RATIO = 0.75


def _spec_cap_h(spec: dict) -> float:
    """spec 里的原字幕带高（归一化）。没有就 0，调用方据此退回老字号规则。"""
    try:
        return max(0.0, float((spec or {}).get("cap_h") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _cap_layout(text: str, cap_h: float) -> tuple[int, list[str]]:
    """定这一句的字号与折行。

    cap_h > 0（识别到了原字幕带高）时**按原片字号渲染**：新字幕和原字幕
    一样大，换上去观众看不出被人换过。2026-11-12 实测第 1 集：原片字高
    57px（1080 尺度），旧代码写死 fs≤44 → 成品只有 48px，整体小 16%、
    窄 19%，放大看就是「这字不是原来的」。
    cap_h = 0（没识别到，例如原本就没字幕的集）时退回老办法：按字数把
    字号压到一行装得下，min(44, …)。

    折行**必须跟着字号走**，不能固定 18 字：字号对齐到 52 之后，
    18 个字宽 = 936px，720 宽的成片根本放不下，宽度兜底又会把字号
    打回 40 —— 等于白对齐（踩过）。
    """
    text = text or ""
    if not text:
        return 0, []
    if cap_h and cap_h > 0:
        size = int(round(cap_h * _CAP_FS_PER_H))
        size = min(_CAP_FS_MAX, max(_CAP_FS_MIN, size))
        cols = max(6, int((W - 72) / (size * _CAP_CH_RATIO)))
        lines = _wrap_cn(text, cols)
        if len(lines) > 2:
            # 一行放不下就缩，但**只允许两行**——三行字幕会压住半张脸，
            # 短剧里比字小一点难受得多。缩完再折一次。
            size = max(_CAP_FS_MIN, int(size * 2 / len(lines)))
            cols = max(6, int((W - 72) / (size * _CAP_CH_RATIO)))
            lines = _wrap_cn(text, cols)
        n = max((len(x) for x in lines), default=1)
        # 极长的无标点单行仍可能超宽 → 宽度兜底（按真实字宽算，不是按字数）
        size = min(size, max(_CAP_FS_MIN, int((W - 72) / (_CAP_CH_RATIO * n))))
        return size, lines
    lines = _wrap_cn(text, 18)
    n = max(6, max((len(x) for x in lines), default=6))
    return min(44, max(26, (W - 72) // n)), lines


def _cap_text_lines(text: str, cap_h: float) -> tuple[int, list[str]]:
    """定字号 + 折行 + **去掉标点**（烧到画面上的字不要句读）。

    2026-11-16 用户定：台词文本（第 1 步查看/导出）保留标点，那是给人读的；
    但烧到画面上的字幕一行没几个字，标点既占位置又扎眼，一律不显示。

    顺序很关键——**先按带标点的原文折行，再逐行去标点**：
    `_wrap_cn` 是「在标点后断」，标点先删掉的话「你们到底，想干什么？」
    会被硬切成「你们到底想干」「什么」；先折行就还是断在逗号处，
    去标点只是把那一个字符拿掉。

    返回的 lines 可能比原文短一两个字，字宽只会更富余，不会溢出。
    """
    size, lines = _cap_layout(text, cap_h)
    return size, [x for x in (_cap_strip_punct(l) for l in lines) if x]


# 不删的符号：百分号、千分比不是句读，删了「涨了 5%」就成「涨了 5」
_CAP_KEEP = set("%％‰")
# 只有夹在数字/字母中间才有意义的：3.5 万、10:30、3-5、A/B
_CAP_KEEP_BETWEEN = set(".:/-")


def _cap_strip_punct(s: str) -> str:
    """去掉这一行里的标点（Unicode P* 类），保留数字里的点、冒号、斜杠。"""
    if not s:
        return s
    out: list[str] = []
    n = len(s)
    for i, ch in enumerate(s):
        if ch.isspace() or ch in _CAP_KEEP:
            out.append(ch)
            continue
        if not unicodedata.category(ch).startswith("P"):
            out.append(ch)
            continue
        if ch in _CAP_KEEP_BETWEEN:
            prev = s[i - 1] if i else ""
            nxt = s[i + 1] if i + 1 < n else ""
            if prev.isalnum() and nxt.isalnum():
                out.append(ch)
        # 其余标点直接丢
    # 删掉句中逗号后可能留下两格空格；中文一般没有空格，顺手收紧
    return " ".join("".join(out).split())


# 一条字幕最多几个字（2026-11-19 用户定 15）。超过就按标点拆成几条依次上屏。
# 为什么在这儿拆、不在台词数据里拆：转写出来的「一口气连说 32 字」在词级时间戳
# 里根本没有停顿（第 1 集 338 个词里 303 处间隔 <0.15 秒），靠调 GAP_SPLIT_SEC
# 拆不开——实测阈值降到 0.5 秒，第 5 集最长句还是 71 字，只会多出一堆「我」「陆正」
# 这类碎行。所以在烧字这一步按标点切：台词、时间码、对时指纹、分镜全都不动，
# 只有成片里的一屏变几屏。
CAP_MAX_CHARS = 15
# 烧字口径版本：改分句/折行/去标点/动效算法就 +1。**只进带台词的完整指纹**
# （nosub.sig_of(cfg, episodes)），不进 settings 指纹——否则前端会把「烧字方式
# 变了」误报成「副本设置变了」，让人白重做一遍 31 集副本。
CAP_VER = "S1"


def _cap_vis_len(t: str) -> int:
    """这一行烧出来实际占几个字宽（标点会被 _cap_strip_punct 删掉，不算）。"""
    n = 0
    for c in (t or ""):
        if c.isspace():
            continue
        if c in _CAP_KEEP:
            n += 1
            continue
        if unicodedata.category(c).startswith("P"):
            continue
        n += 1
    return n


def _cap_pieces(text: str) -> list[str]:
    """按标点切成小片段，标点留在前一片段末尾（好让断点落在句读处）。"""
    text = text or ""
    out: list[str] = []
    cur = ""
    n = len(text)
    for i, ch in enumerate(text):
        cur += ch
        if ch.isspace():
            continue
        if not unicodedata.category(ch).startswith("P"):
            continue
        if ch in _CAP_KEEP:                     # % ‰ 不是句读
            continue
        if ch in _CAP_KEEP_BETWEEN:             # 3.5 / 10:30 中间的点冒号
            prev = text[i - 1] if i else ""
            nxt = text[i + 1] if i + 1 < n else ""
            if prev.isalnum() and nxt.isalnum():
                continue
        out.append(cur)
        cur = ""
    if cur.strip():
        out.append(cur)
    return [p for p in out if p.strip()]


def _cap_hard_cut(text: str, max_chars: int) -> list[str]:
    """整段没有一个标点（连说不停）时按字数切，**按块数均分**而不是切满再留尾巴。

    切满再留尾巴会出「15 字 + 3 字」：3 字那一屏一闪而过，并回前一条又变 18 字、
    折行后末行只剩一个字。18 字按 9+9 切，两屏都完整（2026-11-19 实测全剧
    938 条里靠这个把越界条数从 9 条压到 0）。
    """
    total = _cap_vis_len(text)
    if total <= max_chars:
        return [text]
    k = max(2, -(-total // max_chars))      # 需要几块（向上取整）
    per = -(-total // k)                    # 每块目标字数（向上取整，均分）
    out: list[str] = []
    cur, n = "", 0
    for ch in text or "":
        cur += ch
        if ch.isspace():
            continue
        if not unicodedata.category(ch).startswith("P") and ch not in _CAP_KEEP:
            n += 1
        if n >= per:
            out.append(cur)
            cur, n = "", 0
    if cur.strip():
        out.append(cur)
    return [x for x in out if x.strip()] or [text]


def _cap_split_row(s: float, e: float, text: str,
                   max_chars: int = CAP_MAX_CHARS) -> list[tuple]:
    """把一条过长台词按标点拆成几条，时间按字数占比分。返回 [(s,e,text)]。

    字数不超上限的原样返回（绝大多数台词走这条）。拆出来的片段首尾相接，
    不留空隙——留着会看到字幕一闪一停。
    """
    if _cap_vis_len(text) <= max_chars:
        return [(s, e, text)]
    pieces: list[str] = []
    for p in _cap_pieces(text):
        if _cap_vis_len(p) <= max_chars:
            pieces.append(p)
        else:
            pieces.extend(_cap_hard_cut(p, max_chars))
    chunks: list[str] = []
    cur = ""
    for p in pieces:
        if cur and _cap_vis_len(cur) + _cap_vis_len(p) > max_chars:
            chunks.append(cur)
            cur = p
        else:
            cur += p
    if cur.strip():
        chunks.append(cur)
    # 末尾只剩一两个字就并回上一条：单独闪一下反而更跳。**但不许超过上限**——
    # 放宽到 15+3 会折行，末行只剩一个孤字，比单独闪更难看的（2026-11-19 实测）。
    if len(chunks) > 1 and _cap_vis_len(chunks[-1]) < 4 \
            and _cap_vis_len(chunks[-2]) + _cap_vis_len(chunks[-1]) <= max_chars:
        chunks[-2] += chunks[-1]
        chunks.pop()
    if len(chunks) < 2:
        return [(s, e, text)]
    total = sum(_cap_vis_len(c) for c in chunks) or 1
    dur = max(0.0, float(e) - float(s))
    out: list[tuple] = []
    t = float(s)
    for i, c in enumerate(chunks):
        if i == len(chunks) - 1:
            out.append((t, float(e), c))
        else:
            share = dur * _cap_vis_len(c) / total
            out.append((t, t + share, c))
            t += share
    return out


def _cap_split_rows(rows: list) -> list[tuple]:
    """整段台词按上限拆开，返回 [(s,e,text,gid,first,last)]。

    gid 是原始句号——动效的上下交替按它算，同一句拆出来的几条 y 保持一致，
    不然 3 条字幕会在两个高度来回跳。first/last 用来只在整句开头淡入、结尾淡出，
    中间两条不淡，避免接缝处闪一下。
    """
    out: list[tuple] = []
    for gid, (s, e, text) in enumerate(rows):
        parts = _cap_split_row(s, e, text)
        for i, (ps, pe, pt) in enumerate(parts):
            out.append((ps, pe, pt, gid, i == 0, i == len(parts) - 1))
    return out


def _cap_ass_file(rows: list, spec: dict, out_dur: float, path: str) -> None:
    """整段台词的 ASS。一句一条 Dialogue，各自带淡入淡出与句内动效。

    字号按 spec["cap_h"]（原字幕带高）对齐原片，见 _cap_layout；
    Alignment=5 是正中锚点，\\move/\\pos 的 y 直接就是字幕中心，上下偏移好算。

    超过 CAP_MAX_CHARS 的长句先按标点拆成几条（见 _cap_split_rows），
    一屏变几屏，台词数据本身一个字不动。
    """
    ms = str(spec.get("style") or "rise")
    if ms not in NEWSUB_STYLES:
        ms = "rise"
    m = spec["motion"]
    cy = H * spec["pos"]
    cap_h = _spec_cap_h(spec)
    style = (f"Style: Cap,{_ASS_FONT},36,&H00FFFFFF,&H00FFFFFF,&H00000000,"
             f"&H80000000,-1,0,0,0,100,100,0,0,1,2,1,5,20,20,0,1\n")
    dlg: list[str] = []
    for s, e, text, gid, first, last in _cap_split_rows(rows):
        s, e = min(max(0.0, s), out_dur), min(max(0.0, e), out_dur)
        d = e - s
        if d < 0.12:
            continue
        size, lines = _cap_text_lines(text, cap_h)
        body = _ass_text("\n".join(lines))
        if not body:
            continue
        # 淡入淡出别拖太长：实帧上 200ms 淡出让句尾明显发灰，短剧一句才
        # 1-3 秒，静止可读的那段才是观众真正在读的，收短到 160/130ms。
        # 拆出来的同一句只在**头尾**淡：中间几条不淡，接缝处不会闪一下。
        fi = min(160, int(d * 220)) if first else 0
        fo = min(130, int(d * 180)) if last else 0
        # 相邻句基准位差一点点：整条字幕带看着在原位置附近呼吸，但每句内部是稳的。
        # 按原始句号 gid 交替——同一句拆出来的几条必须同高，否则会来回跳。
        y = cy + (6.0 * m if gid % 2 else -6.0 * m)
        at = f"{{\\pos({W // 2},{round(y)})}}"
        fs = f"{{\\fs{size}}}"
        if ms == "type":
            # 从左往右擦出。**不能用 \k 逐字变色**：\k 只改填充色，
            # 还没轮到的字会露出黑色描边，实帧上是一排灰影（已复现）。
            # \clip 是把整个矩形裁掉，描边阴影一起消失，才真的「一个字都看不见」。
            # 实测 libass 支持 \t 动画 \clip（VSFilter 也支持）。
            nch = max((len(x) for x in lines), default=1)
            tw = int(nch * size)                 # 中文字宽 ≈ 0.75 字号，这里取满留余量
            x0 = max(4, int((W - tw) / 2))       # 居中时的左边界
            x1 = min(W - 4, x0 + tw)
            ty0, ty1 = round(y - size * 1.7), round(y + size * 1.7)
            ms_total = int(min(d * 800, nch * 90))
            tags = (f"{{\\fad(0,{fo})}}{fs}{at}"
                    f"{{\\clip({x0},{ty0},{x0},{ty1})"
                    f"\\t(0,{ms_total},\\clip({x0},{ty0},{x1},{ty1}))}}")
            dlg.append(f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Cap,,0,0,0,,"
                       f"{tags}{body}\n")
            continue
        # 动效幅度一律跟滑杆走 m（0~1），且**滑杆拉到 0 时真要接近静止**——
        # 界面上 0 那端写的是「几乎不动」。2026-11-15 用户反馈"动效幅度不够大、
        # 上浮看不出，像直接出现"：旧版 rise 只浮 6+8m（最大 14px，1280 高的 1%），
        # float 写死 ±12px、pop 写死 86%，三个风格在实帧上都接近硬切。
        # 现在起点拉开：rise 4+96m（默认 m=0.5 → 52px，约画面 4%），
        # float 2+28m，pop 起始 100-(4+24m)%（默认与旧版同为 86%）。
        if ms == "fade":
            tags = f"{{\\fad({fi},{fo})}}{fs}{at}"
        elif ms == "float":
            # 整句一直在慢慢上浮：幅度太小等于没动，太大又干扰阅读
            amp = round(2 + 28 * m)
            tags = (f"{{\\fad({fi},{fo})}}{fs}"
                    f"{{\\move({W // 2},{round(y + amp)},{W // 2},{round(y - amp)},"
                    f"0,{int(d * 1000)})}}")
        elif ms == "pop":
            # 从偏小弹到 100%：起始缩放跟滑杆走
            sc = round(100 - (4 + 24 * m))
            t1 = min(300, max(140, int(d * 1000 * 0.35)))
            tags = (f"{{\\fad({fi},{fo})}}{fs}"
                    f"{{\\fscx{sc}\\fscy{sc}\\pos({W // 2},{round(y)})}}"
                    f"{{\\t(0,{t1},\\fscx100\\fscy100)}}")
        else:  # rise
            # 从明显更低的位置浮上来，再用 200~520ms 落位，之后静止到句末。
            # 落位时长比旧版长：一句才 0.8 秒时也得让"浮上来"这段看得见。
            amp = round(4 + 96 * m)
            # 不许浮到画面外：起始位置要留出「半个字高 + 8px」的余量，
            # 不然 pos 拉到 0.92 那种贴底的设置会把字顶出底边。
            room = int(H * (0.985 - float(spec.get("pos") or 0.80)))
            amp = max(4, min(amp, int(room - 8 - size * 0.55)))
            t1 = min(520, max(200, int(d * 1000 * 0.45)))
            tags = (f"{{\\fad({fi},{fo})}}{fs}"
                    f"{{\\move({W // 2},{round(y + amp)},{W // 2},{round(y)},"
                    f"0,{t1})}}")
        dlg.append(f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Cap,,0,0,0,,"
                   f"{tags}{body}\n")
    _write_ass(path, style, "".join(dlg))


def _cap_drawtext_filters(rows: list, spec: dict, out_dur: float,
                          dst: str, font: str) -> list[str]:
    """没 libass 时的兜底：每句一条 drawtext，静态位置、按时间显隐。

    这里不做动效（drawtext 的 y 表达式要叠多个 enable 段，滤镜串会又长又脆）；
    观感只差那点入场移动，字照常跟着台词换。textfile 走临时目录，
    由收尾的 core.rmtree(work) 统一清，绝不在这里逐文件 os.remove。
    """
    out: list[str] = []
    for j, (s, e, text, _gid, _first, _last) in enumerate(_cap_split_rows(rows)):
        s, e = min(s, out_dur), min(e, out_dur)
        if e - s < 0.12:
            continue
        size, lines = _cap_text_lines(text, _spec_cap_h(spec))
        body = "\n".join(lines)
        if not body:
            continue
        tfile = f"{dst}.cap{j:02d}.txt"
        with open(tfile, "w", encoding="utf-8") as f:
            f.write(body)
        out.append(
            f"drawtext=fontfile='{_esc_filter_path(font)}'"
            f":textfile='{_esc_filter_path(tfile)}':expansion=none"
            f":fontsize={size}:fontcolor=white:borderw=2:bordercolor=black"
            f":shadowcolor=black@0.5:shadowx=1:shadowy=1"
            f":x=(w-text_w)/2:y=h*{spec['pos']:.4f}-text_h/2"
            f":enable='between(t,{s:.3f},{e:.3f})'")
    return out


TITLE_FALLBACK = "精彩片段"
# 标题前缀（2026-11-03 用户觉得「名场面」不合适）。
# 原来是「名场面：XXX」，三个问题：
#   ① 5-6 分的缓冲块也被叫「名场面」，名不副实（标签本身是【缓冲】…）；
#   ② 一套十几块全程同一个词，像解说号的口播，不像原剧片段；
#   ③ 四个字吃掉一行宽度，长标签的字号被压小一档。
# 换成「看点」：只说「这一段值得看」，爆点和缓冲都成立，两字省宽。
# 想换别的词或彻底不要前缀，只改这一行（改成 "" 就只烧标签原文）。
TITLE_PREFIX = "看点："
# 标签里的台词常被加上引号（‘男人逢场作戏是本事’、「我会让所有人都看见」）。
# 烧在画面顶部，这点引号又碎又占宽度，观众看的是那句话本身，不需要引号这层壳
# （2026-11-03 用户要求去掉）。书名号不是引号，保留。
TITLE_DROP = str.maketrans("", "", "‘’“”\"'「」『』")
TITLE_FONTS = [
    "C:/Windows/Fonts/msyh.ttc",     # 微软雅黑（本机工具直接用系统字体）
    "C:/Windows/Fonts/simhei.ttf",   # 黑体兜底
    "C:/Windows/Fonts/simsun.ttc",   # 宋体最后兜底
]


def _title_font() -> str | None:
    for f in TITLE_FONTS:
        if os.path.isfile(f):
            return f
    return None


def _clean_label(raw: str) -> str:
    """label 去掉【爆点】【缓冲】这类内部分类和引号，剩下给观众看的短句。"""
    raw = (raw or "").strip()
    while True:
        stripped = raw
        if stripped.startswith("【") and "】" in stripped:
            raw = stripped.split("】", 1)[1].strip()
        elif stripped.startswith("[") and "]" in stripped:
            raw = stripped.split("]", 1)[1].strip()
        if raw == stripped:
            break
    raw = raw.strip(" 　·•-—–")
    # 去掉引号后再收一次空格：有的标签是 「‘A’ 「B」」 这种套着写的，
    # 删完引号会留下并排的空格。
    return " ".join(raw.translate(TITLE_DROP).split())


def _title_text(seg: dict) -> str | None:
    """这一块的标题。label 里的【爆点】【缓冲】是给排片的内部分类，不能给观众看。"""
    if _title_font() is None:
        return None
    raw = _clean_label(str(seg.get("label") or ""))
    # 去掉开头的【xxx】/ [xxx] 分类标记，可能连着空格
    while True:
        stripped = raw
        if stripped.startswith("【") and "】" in stripped:
            raw = stripped.split("】", 1)[1].strip()
        elif stripped.startswith("[") and "]" in stripped:
            raw = stripped.split("]", 1)[1].strip()
        if raw == stripped:
            break
    return f"{TITLE_PREFIX}{raw}" if raw else TITLE_FALLBACK


def _esc_filter_path(path: str) -> str:
    """ffmpeg filtergraph 里的路径：反斜杠换正斜杠，盘符冒号要转义。"""
    return path.replace("\\", "/").replace(":", r"\:")


def _drawtext_filter(text: str, font: str, textfile: str) -> str:
    # 字号跟着字数缩：一行最多 (W-80) 像素宽，中文一字约等于一个字号。
    # 整体压小一档（34 封顶），标题是辅助信息，不能抢过底下的台词字幕
    size = min(34, max(21, (W - 80) // max(4, len(text))))
    return (
        f"drawtext=fontfile='{_esc_filter_path(font)}'"
        f":textfile='{_esc_filter_path(textfile)}'"
        # expansion=none 必须开：默认的展开语法把 % 当保留字符（%{...} 序列），
        # 标题里出现「5%」直接报 Stray % → 滤镜失败 → 编码器收不到帧 →
        # libx264 报 -22，这一段整块报废（2026-11-03 set02/set05 实测翻车，
        # 两套恰好共用同一个带 % 的 label）。标题不需要任何展开语法。
        f":expansion=none:fontsize={size}:fontcolor=white"
        f":box=1:boxcolor=black@0.45:boxborderw=14"
        f":x=(w-text_w)/2:y=h*0.055"
    )


def _even(v: float) -> int:
    """取偶数宽高：libx264 + yuv420p 要求偶数，奇数会直接报错。"""
    return max(2, int(round(v / 2.0)) * 2)


def _shift_spans(spans: list[dict], start: float, end: float) -> list[dict]:
    """逐句包围盒 → 段内时间轴。

    `-ss` 放在 `-i` 前，滤镜看到的 t 从 0 起，而 spans 存的是**整集绝对时间**，
    不减 start 的话一句话都不会命中（掩码全 0，字原样留着）。
    只留与本段有交集的块，首尾夹到段边界。
    """
    out: list[dict] = []
    seg = max(0.02, float(end) - float(start))
    for sp in spans or []:
        try:
            t0 = float(sp["t0"]) - float(start)
            t1 = float(sp["t1"]) - float(start)
        except (KeyError, TypeError, ValueError):
            continue
        if t1 <= 0.02 or t0 >= seg:
            continue
        out.append({**sp, "t0": max(0.0, t0), "t1": min(seg, max(0.1, t1))})
    return out


def _live_clean(p: dict, ep: dict, start: float, end: float) -> dict | None:
    """src_ver=live 时，本段要现场抹掉的字幕区域。

    和第 2 步用同一份逐句包围盒（`nosub` 存在项目里的 spans）和同一套参数，
    只是不在副本里预先编码，而是在切段这一趟顺手做掉——这样「烧新字幕」
    可以排在镜像之后，镜像就不会把字翻反。
    """
    ns = p.get("nosub") or {}
    cfg = ns.get("cfg") or {}
    by_ep = (ns.get("spans") or {}).get("by_ep") or {}
    one = by_ep.get(str(ep.get("file") or "")) or {}
    spans = _shift_spans(one.get("spans") or [], start, end)
    spec = subtitle_spec(cfg)
    if not spec.get("mode"):
        return None
    if not spans:
        # 这一集没量出包围盒 → 用整条替换兜底（和 nosub 里的退路一致）
        return {"spec": spec, "spans": []}
    return {"spec": spec, "spans": spans}


def _copy_burned(p: dict) -> bool:
    """第 2 步的副本里是不是**已经烧了字幕**（旧版产物）。

    2026-11-16 起副本只抹不烧，产物里写 `burned=False`；没有这个字段的老项目
    一律当作「已烧」——拿它再烧一遍台词会出现两层字，必须让用户先重做第 2 步。
    """
    ns = p.get("nosub") or {}
    if "burned" in ns:
        return bool(ns.get("burned"))
    return True


def _cap_h_of(sd_ep: dict | None) -> float:
    """这一集原字幕的**字高**（归一化到画面高），给新字幕定字号。

    算法放在 nosub._cap_h 里（和识别判据放一起，免得两处漂移），这里只借来用。
    """
    try:
        from . import nosub
        return float(nosub._cap_h(sd_ep) or 0.0)
    except Exception:  # noqa: BLE001  拿不到就用全局兜底字号，不该让出片失败
        return 0.0


def _live_cap(p: dict, ep: dict, start: float, end: float,
              speed: float, fx: dict | None = None,
              motion_override: dict | None = None) -> dict | None:
    """本段要烧回去的台词字幕（干净副本 nosub / 现场做 live 两条路共用）。

    字幕样式（动效/幅度）2026-11-16 搬到出片这一层：底片里没有字，样式改的是
    「这一套怎么画」，所以优先读本次出片的 fx，老项目没有这两个字段时回落到
    第 2 步存的 nosub_cfg，观感和以前一样。

    位置和字号**按这一集自己量出来的字幕带走**：一个项目里常混着不同来源的片子
    （真人剧 1080×1920 字幕在 78%，AI 生成片 720×1280 在 72%），套全局值会一眼
    看出字幕跳了位。位置取各句框中心的**中位数**，不用并集的上下沿中点——一句
    误检就能把范围拉偏（实测把中心从 72.8% 拽到 75.7%，整行压低 40px）。
    """
    ns = p.get("nosub") or {}
    cfg = dict((ns.get("cfg") or {}))
    # 出片这一层永远要烧字，不看 nosub_cfg.newsub（那是旧版「副本里烧不烧」的开关）
    cfg["newsub"] = True
    for k in ("newsub_style", "newsub_motion"):
        v = (fx or {}).get(k)
        if v is not None and v != "":
            cfg[k] = v
    # 全自动随机：按集随机挑的出场动效覆盖手写设置（同一集同一动效）
    for k in ("newsub_style", "newsub_motion"):
        if motion_override and motion_override.get(k) is not None:
            cfg[k] = motion_override[k]
    spec = newsub_spec(cfg)
    if not spec.get("on"):
        return None
    by_ep = (ns.get("spans") or {}).get("by_ep") or {}
    sd = by_ep.get(str(ep.get("file") or "")) or {}
    cs = sorted((float(x["t"]) + float(x["b"])) / 2.0
                for x in (sd.get("spans") or [])
                if x.get("t") is not None and x.get("b") is not None)
    if cs:
        spec = dict(spec, pos=round(min(0.92, max(0.50, cs[len(cs) // 2])), 4))
    ch = _cap_h_of(sd)
    if ch > 0:
        spec = dict(spec, cap_h=round(ch, 5))
    rows = cap_rows(ep.get("lines") or [], start, end, speed)
    if not rows:
        return None
    return {"rows": rows, "spec": spec}


def _cut(src: str, start: float, end: float, dst: str, title: str | None = None,
         speed: float = 1.0, zoom: float = 1.0, fade_in: bool = False,
         grade: dict | None = None, cap: dict | None = None,
         clean: dict | None = None, first_flip: str = "",
         fp: dict | None = None, frames: int | None = None,
         out_dur: float | None = None, on_frame=None,
         fps: float = FPS) -> None:
    # 去重缩放（2026-11-04 用户要求）：整幅放大 3% 再居中裁回 720x1280，
    # 画面指纹变一点、边缘一圈（含素材自带黑边）被切掉，平台比对原片时对不上。
    # 实现上是「把目标尺寸放大再 crop」，不是「缩到 720 再放大」——后者要重采样
    # 两次，画面会明显发糊；这样从 1080 源到 742 只缩一次，再裁掉 11px，最干净。
    # 去字幕有两条路（2026-11-12）：① 第 2 步预先生成独立视频（source_path），
    # 这里只选素材；② src_ver=live 现场做——clean 给了就在这一趟里先抹原字幕。
    # 现场做的顺序是 **抹除 → 缩放/镜像/调色 → 烧新字幕**，字在翻转之后才画上去，
    # 所以「左右镜像」和「自己的字幕」可以同时要（预先做进副本的那份做不到）。
    # 指纹随机（2026-11-12）：fp 由 render_edl 按套算好传进来，整套一个值。
    # 关掉「画面放大」时不传 fp，这里就退回 zoom 参数原样（1.0 = 不缩放）。
    # 旋转会把四角转出画外，所以余量不够时按几何精确补放大，绝不留黑角。
    fp = fp or {}
    rot = float(fp.get("rot") or 0.0)
    if fp.get("zoom"):
        zoom = float(fp.get("zoom") or zoom)
    zoom = max(1.0, min(1.25, float(zoom or 1.0)))
    if abs(rot) > 0.01:
        t = math.radians(abs(rot))
        # 旋转 θ 后要不露黑角，横竖各需要的额外边距（轴对齐内接原尺寸）
        need_w = (W * math.cos(t) + H * math.sin(t) - W) / 2.0
        need_h = (W * math.sin(t) + H * math.cos(t) - H) / 2.0
        zoom = min(1.25, zoom * max(1.0,
                                    (W + 2 * need_w + 2) / (W * zoom),
                                    (H + 2 * need_h + 2) / (H * zoom)))
    if zoom > 1.001:
        tw, th = _even(W * zoom), _even(H * zoom)
    else:
        tw, th = W, H
    # pre = 画面本身的处理（缩放/变形/调色/特效），post = 文字与转场。
    # 分开是为了两件事：① 标题字幕不能被调色染色、也不能被镜像翻反；
    # ② 光晕要拆两路混合，混合完再把字幕叠上去才清晰。
    pre = [
        f"scale={tw}:{th}:force_original_aspect_ratio=decrease,"
        f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    ]
    if abs(rot) > 0.01:
        # 微旋转放在裁回之前：转出来的四角黑边正好被下一次 crop 切掉
        pre.append(f"rotate={rot:.4f}*PI/180:ow=iw:oh=ih:fillcolor=black")
    if (tw, th) != (W, H) or abs(rot) > 0.01:
        # 居中裁回，再按指纹给出的比例偏移取景框（sx/sy ∈ -1..1）。
        # 留 1px 安全边：偏移到极限时也不会把 pad 的黑边带进来。
        mx, my = (tw - W) / 2.0, (th - H) / 2.0
        sx = max(-1.0, min(1.0, float(fp.get("sx") or 0.0)))
        sy = max(-1.0, min(1.0, float(fp.get("sy") or 0.0)))
        cx = int(round(mx + sx * max(0.0, mx - 1.0)))
        cy = int(round(my + sy * max(0.0, my - 1.0)))
        cx = max(0, min(tw - W, cx))
        cy = max(0, min(th - H, cy))
        pre.append(f"crop={W}:{H}:{cx}:{cy}")
    g = grade or {}
    # 现场抹原字幕：放在最前面（原片方向），后面的 hflip 才不会把抹除坐标带偏。
    # 抹完之后那块是用镜像画面补的，再 hflip 一次仍然连续，看不出接缝。
    head, extra = "[0:v]", ""
    if clean:
        extra = _subtitle_mask_graph(clean["spec"], clean.get("spans") or []) + ";"
        head = "[__sub_base]"
    if g.get("mirror"):
        # 左右镜像：去重很强（画面指纹完全变了），代价是素材自带的台词字幕会
        # 翻反——所以只有能先抹/后烧字的素材才走到这里（调用方已按素材把关）。
        # True  = 整段翻：AI 编辑出片按「集」决定（auto_ep_pick）。
        # 字符串 = "hflip=enable='…'" 时间线：选视频直接出片按 60 秒时间窗决定
        #         （direct_mirror_plan），一段里翻几段、哪几段各随机。
        # 两者的 hflip 都在烧字幕之前，字是画面翻完才画的，不会被翻反。
        _m = g["mirror"]
        pre.append(_m if isinstance(_m, str) else "hflip")
    try:
        stretch = float(g.get("stretch") or FX_DEFAULTS["stretch"])
    except (TypeError, ValueError):
        stretch = FX_DEFAULTS["stretch"]
    stretch = max(0.94, min(1.06, stretch))
    if abs(stretch - 1.0) > 0.002:
        # 横向拉伸再裁回：纵向不动，人物变瘦/变胖。1.06 ≈ 瘦 6%，再多脸会变形。
        pre.append(f"scale={_even(W * stretch)}:{H}")
        pre.append(f"crop={W}:{H}")
    # 画质微调（2026-11-22 指纹新维度）：±0.2% 级镜头畸变 + 轻锐化。放在几何
    # 定形之后、fps 之前——畸变基于最终构图算才准；幅度压在「像正常调色导出」
    # 的量级，肉眼读不出「加过滤镜」，但像素指纹全变。
    _k1 = float(fp.get("lens_k1") or 0.0)
    if abs(_k1) > 1e-5:
        pre.append(f"lenscorrection=k1={_k1:.5f}:k2={float(fp.get('lens_k2') or 0.0):.5f}")
    _sharp = str(fp.get("sharp") or "")
    if _sharp == "3":
        pre.append("unsharp=3:3:0.4:3:3:0.4")
    elif _sharp == "5":
        pre.append("unsharp=5:5:0.6:5:5:0.6")
    # 帧率适配（2026-11-22）：每套一个 fps（soft/未开指纹 = 30 原样）。fps 滤镜
    # 只重排时间线，时长守恒；帧数计划（speed_plan）吃的同一个值，两边必然一致。
    pre.append(f"fps={fps:.3f}")
    dur = max(0.5, end - start)
    if speed and abs(speed - 1.0) > 0.001:
        # 变速：setpts 压时间轴 + atempo 变声速（音调不变），音画同步、口型对得上
        pre.append(f"setpts=PTS/{speed:.4f}")
    pre.extend(_grade_filters(g, g.get("eq_wobble")))
    pre.extend(_fx_filters(g))
    # 色度噪声（2026-11-22 指纹新维度）：只动 U/V 平面，亮度不动——肉眼几乎
    # 无感（色调轻微闪烁），逐帧哈希全变。先 format=yuv420p 把平面语义钉死
    # （gbrp 的 c1/c2 是 G/B 平面，语义就错了），两个 seed 都写死保证重跑可复现。
    _cn = float(fp.get("cn_s") or 0.0)
    if _cn > 0.4:
        _cseed = int(fp.get("cn_seed") or 1)
        pre.append(f"format=yuv420p,"
                   f"noise=c1s={int(round(_cn))}:c1f=t+u:c2s={int(round(_cn))}:c2f=t+u"
                   f":c1_seed={_cseed}:c2_seed={_cseed * 2654435761 % (2 ** 31)}")
    # 帧数预算（2026-11-17）：调用方给了 frames/out_dur 就以它为准。
    # 变速后的输出时长必须落在整数帧上，否则每段差半帧，二十段拼下来音画不同步。
    if frames:
        # 源片比 EDL 记录的短（末尾被裁过）时补最后一帧，保证 -frames:v 要的帧数够；
        # 正常情况 tpad 一帧都不产生，只是兜底。
        pre.append("tpad=stop_mode=clone:stop_duration=0.5")
    if not out_dur or out_dur <= 0:
        out_dur = dur / (speed if speed and speed > 0 else 1.0)
    post: list[str] = []
    if cap and cap.get("rows"):
        # 新字幕排在 post 最前：在缩放/镜像/调色/光晕之后叠，字不会被翻反、
        # 被染色、被柔化；紧接着的看点标题在画面顶部，两者位置不打架。
        # 两个 subtitles 串联互不影响，拼接阶段仍然是视频流 copy。
        rows = [(s, e, t) for s, e, t in cap["rows"] if s < out_dur and e > 0]
        if rows:
            if _has_libass():
                assfile = dst + ".cap.ass"
                _cap_ass_file(rows, cap["spec"], out_dur, assfile)
                post.append(_subs_filter(assfile))
            else:
                font = _title_font()
                if font:
                    post.extend(_cap_drawtext_filters(rows, cap["spec"],
                                                      out_dur, dst, font))
            # 字幕文件（.cap.ass / .capNN.txt）留在临时目录里，收尾统一清。
            # 绝不在这里逐文件 os.remove：本机删除守卫按会话计数，
            # 渲染到第 50 个文件会抛 SAFE_DELETE_BULK_CONFIRM_REQUIRED，
            # 不是 OSError，except 接不住，整批渲染当场死（2026-11-03 实测）。
    if title:
        font = _title_font()
        if font:
            if _has_libass():
                # ASS 动效版：淡入 + 下滑落位（样式对齐 drawtext，只多动效）。
                # 字号公式和 _title_ass_file 里保持一致。
                assfile = dst + ".title.ass"
                _title_ass_file(title, out_dur, assfile)
                post.append(_subs_filter(assfile))
            else:
                # 标题走 textfile：drawtext 的 text= 参数转义地狱（冒号、引号、%），
                # 文件没有这问题
                tfile = dst + ".title.txt"
                with open(tfile, "w", encoding="utf-8") as f:
                    f.write(title)
                post.append(_drawtext_filter(title, font, tfile))
            # 注意：这里**不**逐段删文案文件。它们就在临时目录里，渲染收尾
            # core.rmtree(work) 会连它一起清（逐文件吞异常，删不掉也不伤任务）。
            # 之前在这里 os.remove，本机文件守卫按「一次会话删除总数」记账，
            # 渲染删到第 50 个文件时守卫抛 SAFE_DELETE_BULK_CONFIRM_REQUIRED，
            # 不是 OSError，except 接不住——渲染任务当场死在第 2 套（2026-11-03 实测）。
    # 开头镜像排在字幕 / 标题之后、淡入之前：连字幕一起翻（开头几个字会反，
    # 面板里已写清楚），而淡入只是亮度渐变，翻不翻都一样，放后面不干扰。
    # 它是 post 的普通一项，所以 -vf 单链 / 现场抹字幕 / 光晕 gbrp 三条路径
    # 都会自动带上，不需要各自再改一遍滤镜图。
    if first_flip:
        flt = first_flip_filter(first_flip, fps)
        if flt:
            post.append(flt)
    if fade_in:
        # 钩子卡收尾淡出到黑后，第一段从黑淡入 0.18s——不重编码的「转场」：
        # 拼接仍是 copy 流程，靠「卡淡出 + 段淡入」两个各自独立的小动作拼出
        # 叠化观感。只给第一段开（fade_in 由 render_edl 按 i==0 传入）。
        post.append("fade=t=in:st=0:d=0.18")
    # 段间淡入淡出：切口永远是硬切，音频 60ms 进 / 120ms 出把「咔」声磨掉，
    # 观感是台词接得顺；视频保持硬切（叠化会把两场戏糊在一起，短剧混剪不用）。
    af = ["afade=t=in:st=0:d=0.06",
          f"afade=t=out:st={max(0.0, out_dur - 0.12):.3f}:d=0.12"]
    if frames:
        # 音频也钉到同一时长：apad 补静音、atrim 切齐。-t 只能按源片截，
        # 变速/补帧之后音频的真实长度会差几十毫秒，拼起来就是「越往后越不同步」。
        af = ["apad", f"atrim=0:{out_dur:.4f}"] + af
    # 指纹：每段整体音量轻微上下浮动（正负不到 1dB，听不出，但波形的采样值变了）
    _gain = float(fp.get("gain") or 0.0)
    if abs(_gain) > 0.05:
        af.insert(0, f"volume={_gain:.2f}dB")
    if speed and abs(speed - 1.0) > 0.001:
        af.insert(0, f"atempo={speed:.4f}")

    try:
        glow = max(0.0, min(1.0, float(g.get("glow") or 0.0)))
    except (TypeError, ValueError):
        glow = 0.0
    vargs: list[str]
    if glow > 0.01:
        # 光晕（bloom）：把画面复制一份模糊 + 提亮，用 screen 混合叠回原图——
        # 亮部（灯、窗、皮肤高光）周围会「溢」出一层柔光，暗部不被提亮。
        # 单链做不了（要把同一帧喂给两条支路），所以这一段走 -filter_complex。
        # 字幕排在混合之后：字是清晰的，光晕只糊画面（否则标题会发虚）。
        sigma = 8 + 26 * glow
        bright = 0.04 + 0.12 * glow
        opac = 0.16 + 0.44 * glow
        # format=gbrp 不是可省的：blend 是**逐平面**混合，在 yuv420p 上做会把
        # U/V（色度）也当亮度混，实测 G 通道被压到 82、画面发紫（2026-11-05 实测）。
        # 换成 RGB 三平面混合，三通道按各自亮度同幅提亮，色相才不跑。
        chain = (extra + f"{head}{','.join(pre)},format=gbrp,split=2[__b][__s];"
                 f"[__s]gblur=sigma={sigma:.1f},eq=brightness={bright:.3f}[__g];"
                 f"[__b][__g]blend=all_mode=screen:all_opacity={opac:.2f}[__o]")
        tail = ",".join(post) if post else "null"
        chain += f";[__o]{tail}[vout]"
        vargs = ["-filter_complex", chain, "-map", "[vout]", "-map", "0:a?"]
    elif extra:
        # 现场抹字幕本来就是多路滤镜（掩码要单独一条支路），只能走 filter_complex
        chain = extra + f"{head}{','.join(pre + post)}[vout]"
        vargs = ["-filter_complex", chain, "-map", "[vout]", "-map", "0:a?"]
    else:
        vargs = ["-vf", ",".join(pre + post)]

    _run_v(lambda: [
        core.ff("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src,
        *vargs,
        # 帧数路径下分段**不带音轨**：带的话容器时长 = max(视频, 音频)，而 AAC
        # 比视频长几十毫秒，concat 按文件时长排会把下一段往后挤（实测 4 段丢 2 帧）。
        # 音轨由 render_edl 用 _audio_track 单独重建，这里只出画面。
        *(("-an",) if frames else
          ("-af", ",".join(af),
           "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k")),
        # 帧数上限：给了就精确输出这么多帧。配合 tpad 兜底，这一段的时长
        # 就是 frames/fps，拼接时不会因为某段少一帧而整体漂。
        *(("-frames:v", str(int(frames))) if frames else ()),
        *_venc_args(),
        "-avoid_negative_ts", "make_zero", dst,
    ], on_frame)


# ── 开场钩子卡（2026-11-04 用户要求）────────────────────────────────────
# 成片最前面 1.8 秒：纯黑底 + 一句大字，比如「结婚三年，她才发现丈夫还有另一个家」。
# 为什么值得多这 1.8 秒：混剪的第一块是剧情里最早发生的戏，往往不够炸；观众在
# 抖音/快手上判断一条片子要不要看，只看开头 1-2 秒。黑屏大字给的是「这条片子在
# 讲什么矛盾」——悬念而不是画面，成本 1 秒多，抢的是「划走」那一下。
# 文案由 AI 在第 2 步随结构一起给（每套一句，见 prompts/plans.txt），不额外调 API。
#
# 注意三个坑：
#   ① concat 要求所有段的编码参数一致（尺寸/帧率/像素格式/采样率/声道），所以
#      卡片也走 libx264 + aac 48k 立体声，跟 _cut 完全对齐；
#   ② 卡片没有真实音频会被 concat 拒绝，必须用 anullsrc 补一条静音轨；
#   ③ 文案含 % 会让 drawtext 的默认展开语法报 Stray %，跟标题一样走 textfile +
#      expansion=none，并且这里**不做**任何滤镜表达式拼接。
HOOK_SEC = 1.8

# ── 段间转场空境（2026-11-18）───────────────────────────────────────────────
# 在两个镜头之间插一段「虚化推镜过渡空境」：取前一段尾帧 → 放大裁满 →
# 高斯模糊 + 压暗去色 → 缓推近 → 淡入淡出，配静音轨。复用钩子卡的滤镜配方，
# 但**不放文字**。拼接仍是 copy，只在切段那一趟多编一张 0.7s 的卡。
TRANS_SEC = 0.7          # 默认时长（秒）；前端滑杆 0.4-1.5，开关独立
TRANS_BLUR = 20          # 虚化 sigma（跟钩子卡一致）
TRANS_ZOOM = 0.05        # 推镜幅度 5%
TRANS_FADE = 0.25        # 头尾各淡入/淡出 0.25s
# 场景切换检测阈值：ffmpeg select 的 scene 分数，0-1。实测（30s 短剧测试片）：
# 0.28 会把镜头内快动作误判成切镜（30s 切出 8 段），0.35 只留真镜头切换（4 段），
# 0.5 一个都检测不到。取 0.35——宁可少插一个转场，也别把快动作镜头劈开插转场。
TRANS_SCENE_TH = 0.35
TRANS_MIN_GAP = 2.5      # 两个切点最小间隔（秒），避免密集切点把片子切碎
# 黑屏卡用粗体：细体在大屏上撑不住，一眼像「视频卡住了」
HOOK_FONTS = [
    "C:/Windows/Fonts/msyhbd.ttc",   # 微软雅黑 Bold
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
]
# 允许在这种标点后断行：钩子文案几乎都是「A，B」结构，断在逗号后读起来最顺
_HOOK_BREAKS = "，。！？；：、…—·"


def _hook_font() -> str | None:
    for f in HOOK_FONTS:
        if os.path.isfile(f):
            return f
    return None


def _wrap_cn(text: str, per_line: int) -> list[str]:
    """按标点优先、字数兜底折行。中文没有空格可断，只能自己算。"""
    text = " ".join((text or "").split())
    if len(text) <= per_line:
        return [text] if text else []
    # 先在标点后切成一节一节，再把小节贪心合并成行
    chunks: list[str] = []
    cur = ""
    for ch in text:
        cur += ch
        if ch in _HOOK_BREAKS:
            chunks.append(cur)
            cur = ""
    if cur:
        chunks.append(cur)
    lines: list[str] = []
    buf = ""
    for c in chunks:
        if buf and len(buf) + len(c) > per_line:
            lines.append(buf)
            buf = c
        else:
            buf += c
        # 单节本身就超长（没标点的一长句）：硬切
        while len(buf) > per_line:
            lines.append(buf[:per_line])
            buf = buf[per_line:]
    if buf:
        lines.append(buf)
    return lines


def _hook_card(text: str, dst: str, sec: float = HOOK_SEC,
               bg_src: str | None = None, bg_ts: float = 1.0,
               fps: float = FPS) -> bool:
    """生成开场钩子卡。返回是否真的生成了（没字体/没文案就跳过）。

    v2（2026-11-05）：从「纯黑底静字」升级为
      ① 背景 = 本套第一段里的某一帧，放大裁满 → 高斯模糊 → 压暗去色 →
         整卡时长里缓缓推近 5%（zoompan Ken Burns）——告诉观众「这条片子
         就出自这个场景」，比纯黑有质感；取帧失败自动退回纯黑底；
      ② 文字 = ASS 动效（淡入 0.42s + 上浮 16px 落位）；没有 libass 退回
         drawtext + 整体淡入（旧版观感）；
      ③ 收尾淡出到黑 0.25s，配合第一段的淡入 0.18s（render_edl 给 i==0 开
         fade_in），拼出「卡 → 正片」的转场——拼接仍是 copy，不用重编码。

    注意三个坑：
      ① concat 要求所有段的编码参数一致，卡片编码参数也走 _venc_args()，
         和切段完全对齐（同一台机器上硬件/软编必然一致）；
      ② 卡片没有真实音频会被 concat 拒绝，必须用 anullsrc 补静音轨；
      ③ 文案含 % 会让 drawtext 的默认展开语法报 Stray %，drawtext 路径
         走 textfile + expansion=none；ASS 路径由 _ass_text 清洗。"""
    text = " ".join((text or "").split()).strip()
    font = _hook_font()
    if not text or not font:
        return False
    # 先按 11 字一行试排，按行数定字号，再用定下来的字号重排一次——
    # 20 字用 62 号会挤出画面，40 字用 62 号更是不可能，字号必须跟着行数退。
    lines = _wrap_cn(text, 11)
    longest = max((len(x) for x in lines), default=1)
    size = min(62, max(36, int((W - 96) / max(6, min(longest, 14)))))
    per_line = max(6, int((W - 96) / size))
    lines = _wrap_cn(text, per_line)

    # ── 背景：从第一段取一帧（失败就纯黑底） ──
    frame = os.path.splitext(dst)[0] + ".bg.jpg"
    bg_ok = False
    if bg_src and os.path.isfile(bg_src):
        try:
            _run([
                core.ff("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{max(0.0, float(bg_ts)):.3f}", "-i", bg_src,
                "-frames:v", "1", "-q:v", "3", frame,
            ])
            bg_ok = os.path.isfile(frame)
        except Exception:  # noqa: BLE001  取不到帧就纯黑底，一张卡不值得整套失败
            bg_ok = False

    use_ass = _has_libass()
    assfile = dst + ".ass"
    tfile = dst + ".txt"

    # ── 输入：有底图用 loop 图片，没有用 lavfi 黑场 ──
    if bg_ok:
        inputs = ["-loop", "1", "-framerate", f"{fps:.3f}", "-t", f"{sec:.3f}", "-i", frame]
        # 放大到 900x1600 再推近：zoompan 在比输出大的底图上 crop 推镜不露边。
        # 线性推 5%，肉眼是缓推、不晃。模糊压暗去色——背景只做氛围暗示。
        frames = max(1, int(round(sec * fps)))
        vf = (
            "scale=900:1600:force_original_aspect_ratio=increase,crop=900:1600,"
            "gblur=sigma=20,eq=brightness=-0.17:saturation=0.72,"
            f"zoompan=z='min(1+0.05*in/{frames},1.05)'"
            f":x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2':d=1:s={W}x{H}:fps={fps:.3f}"
        )
    else:
        inputs = ["-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={fps:.3f}:d={sec:.3f}"]
        vf = ""

    # ── 文字 ──
    if use_ass:
        _hook_ass_file(lines, size, sec, assfile)
        vf = vf + "," + _subs_filter(assfile) if vf else _subs_filter(assfile)
    else:
        # drawtext 兜底（无 libass 的 ffmpeg 精简包才会走到）。
        # line_spacing 保持 0：微软雅黑自身行高已有 1.37 倍字号，再加间距
        # 三行字会被撑成「隔行」（2026-11-04 实测 18/25 都太松）。
        with open(tfile, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines))
        vf = vf + "," if vf else ""
        vf += (
            f"drawtext=fontfile='{_esc_filter_path(font)}'"
            f":textfile='{_esc_filter_path(tfile)}'"
            f":expansion=none:fontsize={size}:fontcolor=white"
            f":x=(w-text_w)/2:y=(h-text_h)/2"
        )

    # ── 头尾淡变：有底图时头尾都淡（进出场都柔和）；纯黑底只有头部需要 ──
    vf += ",fade=t=in:st=0:d=0.30"
    if bg_ok:
        vf += f",fade=t=out:st={max(0.0, sec - 0.25):.3f}:d=0.25"

    _run_v(lambda: [
        core.ff("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        *inputs,
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-t", f"{sec:.3f}",
        "-vf", vf,
        *_venc_args(),
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
        dst,
    ])
    # 文案/字幕/底图文件不在这里删：跟标题文案一样交给 core.rmtree(work) 收尾
    # （逐文件删会计入本机删除守卫的按轮计数，详见 _cut 里的注释）
    return True


def scene_cuts(src: str, start: float, end: float,
               th: float = TRANS_SCENE_TH, min_gap: float = TRANS_MIN_GAP) -> list[float]:
    """检测 [start,end) 里的镜头切换点，返回**绝对秒**列表（已按 min_gap 去重）。

    用 ffmpeg 的 select='gt(scene,th)' 输出切镜帧的 pts_time，比自己读帧逐像素比
    快得多，口径也和 nosubfill 的切镜检测方向一致（都是画面突变）。scene 分数是
    ffmpeg 内建的相邻帧差异度量，硬切通常 >0.25，缓推/走动远小于此。

    返回的时间点是**段的边界**：调用方据此把一整段切成多段，转场卡插在切点处。
    只返回段内切点，不含 start/end 自身——两端不是「镜头之间」，不插转场。
    取不到/探测失败都返回空列表——检测是增强项，绝不能让整条片子出不来。
    """
    dur = float(end) - float(start)
    if dur < 2 * min_gap:
        return []
    try:
        r = subprocess.run(
            # showinfo 在 info 级别输出 pts_time，这里必须是 info——用 error
            # 会把切点信息一起屏蔽，检测永远返回空。
            [core.ff("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "info",
             "-ss", f"{float(start):.3f}", "-t", f"{dur:.3f}",
             "-i", src, "-vf", f"select='gt(scene,{th:.3f})',showinfo",
             "-an", "-f", "null", "-"],
            capture_output=True, text=True, timeout=max(30, int(dur * 2) + 30),
            **core.POPEN_HIDE)
        times: list[float] = []
        for m in re.finditer(r"pts_time:([0-9.]+)", r.stderr or ""):
            # -ss 后再解码，showinfo 的 pts 是相对片段开头的，加回 start 得绝对秒
            t = float(start) + float(m.group(1))
            if start + min_gap <= t <= end - min_gap:
                times.append(round(t, 3))
        times.sort()
        out: list[float] = []
        for t in times:
            if not out or t - out[-1] >= min_gap:
                out.append(t)
        return out
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def split_edl_at_cuts(p: dict, edl: list[dict], src_ver: str,
                      min_len: float = 2.0) -> list[dict]:
    """把「一集一整段」的 EDL 按场景切点切成多段，供段间插转场。

    只用于选视频直接出片（direct）：它的 EDL 是一集一整段，片内没有切点，
    先用 scene_cuts 找出镜头切换处，把整段拆成若干子段，render_edl 才有段间
    可以插转场。AI 剪辑出片的 EDL 本来就是多段，不走这里。

    切点两侧的台词归属：字幕是按段内的绝对时间烧的（cap_rows 用 seg 的
    start/end 换算），切段不改变绝对时间，只是把一个段变成首尾相接的几个段，
    所以台词不会丢、也不会重复。每段仍指向同一集、同一个源文件。
    """
    by_index = {ep["index"]: ep for ep in (p.get("episodes") or [])}
    out: list[dict] = []
    for seg in edl:
        ep = by_index.get(seg["ep"])
        s, e = float(seg["start"]), float(seg["end"])
        cuts: list[float] = []
        if ep and e - s >= 2 * min_len:
            cuts = scene_cuts(source_path(p, ep, src_ver), s, e)
        # 切点太靠近两端会切出极短段（转场自己就占 0.7s），丢掉
        cuts = [c for c in cuts if c - s >= min_len and e - c >= min_len]
        bounds = [s] + cuts + [e]
        for a, b in zip(bounds, bounds[1:]):
            sub = dict(seg)
            sub["start"], sub["end"] = a, b
            out.append(sub)
    return out


def _trans_card(prev_src: str, cut_ts: float, dst: str, sec: float = TRANS_SEC,
                fps: float = FPS) -> None:
    """生成一张段间转场空境卡（无文字），编码参数与 _cut 完全一致供 concat copy。

    prev_src：前一段的源视频文件；cut_ts：切点绝对秒（=前一段的 end）。
    背景取**切点前一点**的一帧（画面还在前一段里），放大裁满 → 虚化压暗 →
    缓推近 → 头尾淡入淡出。取帧失败退回纯黑底——一张 0.7s 的卡不值得让
    整套片子失败（跟钩子卡同一个容错原则）。
    fps：本套的输出帧率（2026-11-22 帧率适配）——卡和正片必须同一帧率，
    concat copy 才不会拒绝。
    """
    sec = min(1.5, max(0.4, float(sec)))
    frame = os.path.splitext(dst)[0] + ".bg.jpg"
    bg_ok = False
    if prev_src and os.path.isfile(prev_src):
        # 取切点前 1 帧多的位置：正落在切点上会取到下一镜头的画面
        ts = max(0.0, float(cut_ts) - 0.08)
        try:
            _run([core.ff("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                  "-ss", f"{ts:.3f}", "-i", prev_src,
                  "-frames:v", "1", "-q:v", "3", frame])
            bg_ok = os.path.isfile(frame)
        except Exception:  # noqa: BLE001
            bg_ok = False
    if bg_ok:
        inputs = ["-loop", "1", "-framerate", f"{fps:.3f}", "-t", f"{sec:.3f}", "-i", frame]
        frames = max(1, int(round(sec * fps)))
        vf = (
            "scale=900:1600:force_original_aspect_ratio=increase,crop=900:1600,"
            f"gblur=sigma={TRANS_BLUR},eq=brightness=-0.12:saturation=0.70,"
            f"zoompan=z='min(1+{TRANS_ZOOM}*in/{frames},1+{TRANS_ZOOM})'"
            f":x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2':d=1:s={W}x{H}:fps={fps:.3f}"
        )
    else:
        inputs = ["-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={fps:.3f}:d={sec:.3f}"]
        vf = ""
    fade_st = max(0.0, sec - TRANS_FADE)
    vf += ("," if vf else "") + f"fade=t=in:st=0:d={TRANS_FADE}"
    vf += f",fade=t=out:st={fade_st:.3f}:d={TRANS_FADE}"
    _run_v(lambda: [
        core.ff("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        *inputs,
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-t", f"{sec:.3f}",
        "-vf", vf,
        *_venc_args(),
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
        dst,
    ])


def _run_failure(err: str) -> None:
    """ffmpeg 非零退出的统一报错：磁盘满换成可操作的提示。"""
    if core.is_disk_full(err):
        # 底层 ENOSPC 对用户没有意义，换成能照着做的提示
        raise RuntimeError(
            "磁盘空间不足，写文件时中断了。请清理磁盘，"
            "或在「设置 → 成片保存位置」里改到空间更大的盘，然后重新出片。"
        )
    raise RuntimeError("视频处理没成功，请重试")


# 渲染卡死看门狗（2026-11-22，借鉴竞品的 stall watchdog 思路）：ffmpeg 假死时
# （显卡驱动重置、滤镜死循环、盘抖动）进程不死、也不再吐任何进度，过去只能等
# 用户自己点「中止」，批量出片一夜全卡在这种进程上。解法是把 -progress 输出当
# 心跳——frame/out_time/total_size 任一变化都算活着；超过阈值没有心跳就杀进程，
# 主循环随即收尾报人话。阈值 240 秒：慢盘 4K、收尾 faststart 封装（只重写 moov，
# 进度会安静几十秒）都算正常安静期。阈值默认 120 秒（R0.1，2026-11-22 从 240
# 收紧——更长只是让假死多挂两分钟，没有换来更低的误杀率），设置页 60–600 可调、
# 可关；HS_RENDER_STALL_SEC 环境变量是排查逃生口，优先于设置。解析统一在
# core.stall_sec()：nosub / nosubfill 的看门狗吃同一个值，改一处全局生效。
# 只杀 ffmpeg 本体不追进程树：ffmpeg 单体进程不派生别的进程，kill 足够。
STALL_DEFAULT = core.STALL_DEFAULT


def _stall_sec() -> int:
    return core.stall_sec()


def _run(cmd: list[str], on_frame=None) -> None:
    # 统一一条路（2026-11-22）：不管要不要回报进度，都挂 -progress——它的输出
    # 除了给「选视频直接出片」逐帧回报，更是全路径共用的卡死心跳（见上面注释）。
    # AI 出片不传 on_frame 时 progress 行只喂看门狗、不回报进度，「AI 出片不逐帧
    # 上报」的行为不变（2026-11-18 说的两条路径隔离指的是回报，不是心跳）。
    # R0.2（2026-11-22）：熔断后**同参数**重试一次——卡死多半是盘/驱动的瞬时
    # 抖动，原参数再跑通常就过；卡死≠硬件坏，所以不换软编（真硬件坏走 _run_v
    # 的既有回退链）。中止（TaskCancelled）永远优先于熔断，直接穿透。
    sec = _stall_sec()
    for attempt in (0, 1):
        cmd2 = list(cmd)
        cmd2[1:1] = ["-progress", "pipe:1", "-nostats"]   # -progress 是全局选项，紧跟 ffmpeg
        proc = subprocess.Popen(cmd2, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, errors="replace", bufsize=1,
                                stdin=subprocess.DEVNULL, **core.POPEN_HIDE)
        core.register_proc(proc)
        err_buf: list[str] = []
        stall = {"killed": False}

        def drain_err() -> None:
            try:
                err_buf.append(proc.stderr.read())
            except Exception:  # noqa: BLE001
                pass

        te = threading.Thread(target=drain_err, daemon=True)
        te.start()

        last = {"t": time.monotonic()}

        def beat() -> None:
            last["t"] = time.monotonic()

        if sec > 0:
            def watchdog() -> None:
                # 只看心跳时间戳，不碰管道——排空全在主循环/err 线程手里，别来抢。
                while proc.poll() is None:
                    if time.monotonic() - last["t"] > sec:
                        stall["killed"] = True
                        try:
                            proc.kill()
                        except Exception:  # noqa: BLE001
                            pass
                        return
                    time.sleep(5)

            threading.Thread(target=watchdog, daemon=True).start()

        try:
            assert proc.stdout is not None
            for line in proc.stdout:            # 逐行读 -progress，不攒到结束
                key, _, val = line.partition("=")
                if key in ("frame", "out_time_us", "out_time_ms", "total_size"):
                    beat()
                if key != "frame" or on_frame is None:
                    continue
                try:
                    n = int(val.strip())
                except (ValueError, IndexError):
                    continue
                if n > 0:
                    try:
                        on_frame(n)
                    except Exception:  # noqa: BLE001 —— 进度回调绝不能挡住渲染
                        pass
            code = core.wait_cancelable(proc)
            te.join(timeout=5)
            core.check_cancel()                 # 跑完到返回之间也可能刚好被中止
            if code != 0:
                if stall["killed"]:
                    if attempt == 0 and sec > 0:
                        continue                # 同参数重试一次（R0.2）
                    raise RuntimeError(
                        f"渲染卡死了：ffmpeg 超过 {sec} 秒没有任何进展，已自动终止"
                        + ("，重试一次仍未恢复" if attempt else "") + "。"
                        "反复出现请带日志反馈。")
                _run_failure("".join(err_buf))
            break                       # 正常结束，离开重试循环
        finally:
            core.unregister_proc(proc)
            # 异常退出（解码错误、中止等）时进程可能还活着，收尾补杀——
            # 与 core.run_cancelable 的 finally 同款兜底。
            if proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    pass


def _estimate_mb(edl: list[dict]) -> float:
    """粗估渲染需要多少空间：切段中间文件 + 成品 + 余量。

    实测 720x1280 CRF18 veryfast 约 12MB/分钟成品，切段中间文件约 5MB/段（十几秒一段）。
    这里按 0.5MB/秒 + 10MB/段 再乘 1.5 安全系数，宁可多报一点也别半路写满。
    """
    sec = sum(max(0.0, float(s.get("end", 0)) - float(s.get("start", 0))) for s in edl)
    return (sec * 0.5 + len(edl) * 10) * 1.5


_BGM_LUFS_CACHE: dict[str, tuple[float, int, float]] = {}


def bgm_gain_db(path: str, target: float = BGM_TARGET_LUFS) -> float:
    """量一首曲子的整体响度（LUFS），返回「拉到 target 需要加多少 dB」。

    为什么要它：曲库里的曲目响度差异能到 6-8 LU（占位合成音 −23.1、真人音乐
    −19.1），同一个音量滑杆档位在不同曲目上听起来差一倍——用户滑到 35% 却
    「听不到配乐」，就是这么来的。先按响度拉平，滑杆才有一致语义。

    用 loudnorm 的测量模式（不改音频，只打印 JSON），拿到的 input_i 做线性增益，
    不动曲目的动态；比单遍 loudnorm 动态归一稳，也不会让音乐忽大忽小。
    量不出来（静音、坏文件、"inf"）就返回 0 dB——宁可小声也别把噪声放大。
    缓存按 mtime+size 失效，一场出片只量一次。
    """
    try:
        st = os.stat(path)
    except OSError:
        return 0.0
    sig = (st.st_mtime, st.st_size)
    hit = _BGM_LUFS_CACHE.get(path)
    if hit and hit[0] == sig[0] and hit[1] == sig[1]:
        return hit[2]
    gain = 0.0
    try:
        pr = subprocess.run(
            [core.ff("ffmpeg"), "-nostdin", "-hide_banner", "-i", path,
             "-af", "loudnorm=print_format=json", "-f", "null", "-"],
            capture_output=True, text=True, timeout=180,
            creationflags=core.POPEN_HIDE.get("creationflags", 0))
        m = re.search(r'"input_i"\s*:\s*"(-?[\d.]+|-?inf)"', pr.stderr or "")
        if m:
            val = m.group(1)
            if "inf" not in val:
                gain = max(-12.0, min(18.0, target - float(val)))
    except Exception:  # noqa: BLE001 —— 量不出来就按原始电平混，别为这件事炸掉出片
        gain = 0.0
    _BGM_LUFS_CACHE[path] = (sig[0], sig[1], gain)
    return gain


def _mix_bgm(video: str, audio: str, vol: float = BGM_VOL_DEFAULT) -> None:
    """把音乐混进已拼好的成片（原地替换）：响度拉平 + 压低 + 台词侧链闪避 + 头尾淡入淡出。

    视频流 copy 不重编码；音乐 -stream_loop -1 循环到人声长度，amix duration=first
    收尾。调用前先 ffprobe 过音乐文件（render_edl 里做了），这里失败就是真失败，
    直接抛——用户明确要了 BGM，悄悄丢掉比报错更糟。"""
    vol = min(0.6, max(0.05, float(vol or BGM_VOL_DEFAULT)))
    dur = float(core.ffprobe(video).get("duration") or 0)
    fade_st = max(0.0, dur - BGM_FADE_OUT)
    # 归一化增益并进同一个 volume：vol × 10^(gain/20)，少一次滤镜
    g = vol * (10.0 ** (bgm_gain_db(audio) / 20.0))
    fc = (
        f"[1:a]volume={g:.4f},"
        f"afade=t=in:st=0:d=1.0,afade=t=out:st={fade_st:.3f}:d={BGM_FADE_OUT:.3f}[bgm];"
        f"[0:a]asplit=2[voice][sc];"
        f"[bgm][sc]sidechaincompress={BGM_DUCK}[duck];"
        f"[voice][duck]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]"
    )
    tmp = video + ".bgm.mp4"
    _run([core.ff("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
          "-i", video, "-stream_loop", "-1", "-i", audio,
          "-filter_complex", fc,
          "-map", "0:v:0", "-map", "[a]",
          "-c:v", "copy", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
          "-map_metadata", "-1",
          "-movflags", "+faststart", tmp])
    os.replace(tmp, video)


def _audio_piece(src: str, start: float, dur: float, dst: str,
                 speed: float, out_dur: float, gain_db: float = 0.0) -> None:
    """切一段音频成**无损 PCM**，时长精确等于 out_dur。

    为什么不直接拿 _cut 出来的 mp4 音轨拼：AAC 每段都带编码器填充（约 30ms），
    拼接时按段累加，20 段就是半秒的差——这才是「越往后越不同步」的根。
    PCM 没有填充，atrim 之后一位不差；整条时间线最后由一次 AAC 编码，
    只在片头产生一次填充。
    """
    af: list[str] = []
    if abs(speed - 1.0) > 5e-4:
        af.append(f"atempo={speed:.6f}")
    # 先 atrim 到计划时长、再 apad=whole_dur 兜底：AAC 源带编码器垫帧，
    # 0 起点的段解出来会比请求的短 ~20ms（1024 采样粒度），旧写法 apad 在
    # atrim 之前补不上这段——实测第一段音轨短 21ms，是「开头音画差半帧」
    # 的根（2026-11-22 armor 自测抓出）。whole_dur 把输出钉到与视频计划
    # 完全一致的时长，长补短切双向都精确。
    af += [f"atrim=0:{out_dur:.4f}", "asetpts=PTS-STARTPTS",
           f"apad=whole_dur={out_dur:.4f}"]
    if abs(gain_db) > 0.05:
        af.append(f"volume={gain_db:.2f}dB")
    # 段间淡入淡出沿用切段的 60/120ms：连续音轨上是硬切，不加会「咔」一声。
    af.append("afade=t=in:st=0:d=0.06")
    af.append(f"afade=t=out:st={max(0.0, out_dur - 0.12):.3f}:d=0.12")
    try:
        _run([core.ff("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
              "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src,
              "-vn", "-af", ",".join(af), "-ac", "2", "-ar", "48000",
              "-c:a", "pcm_s16le", dst])
    except Exception:  # noqa: BLE001
        # 这一集没有音轨（极少见，转写/切段阶段能过、这里才会发现）：
        # 补一段等长静音，别让整套片子因为某一集没声音直接失败。
        _run([core.ff("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
              "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
              "-t", f"{out_dur:.4f}", "-c:a", "pcm_s16le", dst])


def _audio_track(p: dict, edl: list[dict], plans: list[dict], src_ver: str,
                 work: str, card_dur: float = 0.0,
                 gain_db: float = 0.0,
                 trans_before: list[bool] | None = None,
                 trans_sec: float = 0.0) -> str:
    """按 EDL 重建**一条连续音轨**（含片头卡的静音），返回 wav 路径。

    这条音轨与视频时间线逐段对齐：每段长度 = 该段计划的 out_dur（整数帧换算来的），
    所以拼完的总长必然等于视频总长，段间不会再累积填充差。
    """
    by_index = {ep["index"]: ep for ep in p["episodes"]}
    parts: list[str] = []
    if card_dur > 0:
        dst = os.path.join(work, "a_hook.wav")
        _run([core.ff("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
              "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
              "-t", f"{card_dur:.4f}", "-c:a", "pcm_s16le", dst])
        parts.append(dst)
    for i, seg in enumerate(edl):
        # 段间转场卡是静音的：视频里这张卡占了 trans_sec，音轨必须同位置补一段
        # 等长静音，否则音轨比视频短、逐段累积漂移。位置跟 cut_all 的 trans_before
        # 严格一致。
        if trans_before and i < len(trans_before) and trans_before[i] and trans_sec > 0:
            dst_t = os.path.join(work, f"a_t{i:03d}.wav")
            _run([core.ff("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                  "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                  "-t", f"{trans_sec:.4f}", "-c:a", "pcm_s16le", dst_t])
            parts.append(dst_t)
        ep = by_index.get(seg["ep"])
        if not ep:
            raise RuntimeError(f"第 {seg['ep']} 集不存在")
        dst = os.path.join(work, f"a{i:03d}.wav")
        _audio_piece(source_path(p, ep, src_ver),
                     float(seg["start"]), float(seg["end"]) - float(seg["start"]),
                     dst, float(plans[i]["speed"]), float(plans[i]["out_dur"]),
                     gain_db)
        parts.append(dst)
    listfile = os.path.join(work, "audio_list.txt")
    with open(listfile, "w", encoding="utf-8") as f:
        for path in parts:
            f.write(f"file '{path.replace(os.sep, '/')}'\n")
    out = os.path.join(work, "audio.wav")
    _run([core.ff("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
          "-f", "concat", "-safe", "0", "-i", listfile,
          "-c:a", "pcm_s16le", out])
    return out


# ── 目标盘不让 ffmpeg 写时的中转（2026-11-18）───────────────────────────────
# 现象：某些受保护盘（本机 F 盘，第三方防护/过滤驱动）Python 能写、能复制文件，
# 但 ffmpeg.exe 这个未签名程序往该盘任何目录写都报 Permission denied。os.access
# 看不出来，只有真跑一次 ffmpeg 才知道。所以渲染前用一次极小的写入探测：
#   · 能写 → 原地渲染（大多数用户，行为不变，临时分段仍放成片同盘）；
#   · 写不进 → 整趟挪到程序数据盘渲染，成功后由 Python 把成片复制回用户目录。
# 结果按目录缓存，进程内只探一次；缓存只影响效率，不影响正确性。
_FF_WRITABLE_CACHE: dict[str, bool] = {}


def _ffmpeg_can_write(directory: str) -> bool:
    key = os.path.abspath(directory)
    if key in _FF_WRITABLE_CACHE:
        return _FF_WRITABLE_CACHE[key]
    ok = False
    probe = os.path.join(key, f"._ffwrite_{os.getpid()}_{int(time.time() * 1000)}.mp4")
    try:
        os.makedirs(key, exist_ok=True)
        r = subprocess.run(
            [core.ff("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc=size=16x16:rate=1", "-frames:v", "1",
             "-c:v", "libx264", probe],
            capture_output=True, timeout=60, **core.POPEN_HIDE,
        )
        ok = r.returncode == 0 and os.path.isfile(probe) and os.path.getsize(probe) > 0
    except Exception:  # noqa: BLE001 —— 探测失败就当中转，别让出片直接崩
        ok = False
    finally:
        try:
            if os.path.exists(probe):
                os.remove(probe)
        except OSError:
            pass
    _FF_WRITABLE_CACHE[key] = ok
    return ok


# ── 音画漂移分级（2026-11-22 R3.2，借鉴零界的 AV 诊断思路）────────────────
# 切段/拼接/混音任何一环出岔子，最终都表现为「画面和声音长度对不上」。
# 三级口径（改造清单 R3.2）：
#   ≤0.1s  ✅ 正常——mp4 容器层结尾 padding 的零点几帧出入算这里
#   ≤0.5s  ⚠️ 只记不拦——轻微，用户几乎感知不到
#   >0.5s  ❌——段级触发原参数重渲一次（R3.4），成片级标红不动片
# 探测失败一律 ok：校验是提示不是闸门（audiofp 同款取舍），绝不卡交付。
def av_drift_check(path: str) -> tuple[str, float, str]:
    """返回 (级别, 相差秒数, 人话描述)。级别 "ok"/"warn"/"bad"，ok 时描述为空。"""
    v, a = core.stream_durations(path)
    if v <= 0 or a <= 0:
        return "ok", 0.0, ""
    d = abs(v - a)
    if d <= 0.1:
        return "ok", d, ""
    note = f"画面 {v:.2f}s / 声音 {a:.2f}s，相差 {d:.2f} 秒"
    return ("warn", d, note) if d <= 0.5 else ("bad", d, note)


def render_edl(p: dict, edl: list[dict], name: str, progress=None,
               cleanup: bool = True, out_dir: str | None = None,
               title_of=None, speed_of=None, hook: str | None = None,
               hook_sec: float = HOOK_SEC, zoom: float = 1.0,
               bgm_path: str | None = None, bgm_vol: float = BGM_VOL_DEFAULT,
               voice_iso: bool = False,
               fx: dict | None = None, grade_seed: str = "",
               fp_level: str = "", fp_seed: str = "",
               fp_pos: float | None = None,
               auto_fx: bool = True,
               trans_sec: float | None = None,
               work_tag: str = "") -> dict:
    """把一份 EDL 渲染成成片文件，返回 {name,duration,size}。不写项目文件，由调用方落库。

    out_dir：成片最终落在哪个目录（用户自己指定，不替用户起子文件夹名）。
    不传则用全局「成片保存位置」。临时分段文件也放同一块盘，避免跨盘写。

    hook：开场钩子卡的文案（每套一句，AI 在第 2 步随结构给）。空串/None = 不加卡。
    卡片是 1.8 秒纯黑大字，纯本地 ffmpeg 生成，不占 API、不加分辨率。

    zoom：去重缩放倍率（1.0 = 不缩放，默认传 render.ZOOM 用 103%）。
    整幅放大一点点再居中裁回原尺寸，见 _cut 里的说明。

    bgm_path：本地 BGM 音频文件（手选，或自动配乐由曲库解析所得）。给了就在拼接完成后
    混入（_mix_bgm）。这里先把文件探一遍——文件不存在或不是音频，当场报错，
    别等切完 20 段才发现音乐是坏的。

    cleanup=False（批量连渲用）：进出都不清临时目录——分段文件固定命名 s000.mp4…
    直接被 ffmpeg -y 覆盖复用。批量场景每套删 25-30 个文件，多套连删会触发本机
    删除守卫的按轮计数拦截（2026-11-01 第 3 套卡死的根源），所以整批只在收尾清一次，
    由调用方（batch.render_plans）负责。

    fx：这一套的画面处理参数（调色/镜像/拉伸/光晕/颗粒/暗角 + 随机分区）。
    每一套可以不一样——每套卡片都能单独设，默认跟全局一致。
    grade_seed：随机调色的种子（项目+套+代次），保证重渲出来还是同一套颜色。

    用哪份视频（src_ver）也从 fx 里读：'' = 原始素材，'nosub' = 第 2 步
    （去字幕 + 加自己的字幕）预先生成的那份。它跟随机分区无关，整套固定一份，
    不能跟着 grade_spec 每块变；每套的 fx 可以不一样，所以不同套能用不同的
    素材版本。

    自己的字幕在出片这一趟烧（2026-11-16 改版）：副本只抹不烧，现场换字也是
    先翻画面后烧字，所以只有这两档能整幅镜像；原始视频带原字，主循环会强制关。

    fp_level / fp_seed：指纹随机化的档位与种子（2026-11-12）。不传就走出厂
    「标准」档、用 grade_seed 兜底当种子——单独调 render_edl 的老代码不会因为
    这两个参数缺失而拿不到随机参数。"""
    pid = p["id"]
    # 选视频直接出片（direct=True）：一个视频一整段、没有「集」，整幅镜像的随机
    # 在片内按时间窗抛硬币（direct_mirror_plan）；AI 编辑出片仍是按集随机。
    # 判定只认项目标记，两条路径彻底隔离，互不串味（2026-11-18 用户强调）。
    is_direct = bool(p.get("direct"))
    src_ver = str((fx or {}).get("src_ver") or "").strip()
    # 段间转场时长：参数优先（单片/新调用），否则从这一套的 fx 读（批量），
    # 两者都没有则用默认 TRANS_SEC（2026-11-18 默认开）。
    # 用 `is None` 而不是 `or`——否则单套显式关（0.0）会被默认 0.7 覆盖，
    # 前端勾掉了却照样插转场。0.0 是合法的「关」，必须传得进来。
    # 开且是直出时，先把「一集一整段」按场景切点切成多段，才有段间可插。
    if trans_sec is None:
        trans_sec = (fx or {}).get("trans_sec", TRANS_SEC)
    try:
        trans_sec = float(trans_sec)
    except (TypeError, ValueError):
        trans_sec = TRANS_SEC
    trans_sec = min(1.5, max(0.0, trans_sec))
    if trans_sec > 0 and is_direct:
        edl = split_edl_at_cuts(p, edl, src_ver)
    # 指纹随机参数：一套抽一次，整套共用（见 fp_spec 的注释）。关掉画面放大时
    # 不抽——放大是这些参数的载体，不放大就没有可裁的余量，硬加旋转会露黑角。
    fpv = (fp_spec(fp_level, (fp_seed or grade_seed or pid), fp_pos)
           if zoom > 1.001 else {})
    # 帧率适配（2026-11-22）：本套输出帧率。没开指纹（没放大）= 30 原样，
    # 行为与老版完全一致；开了才抽 28.6–29.9。卡、切段、变速计划、进度帧数
    # 估算全部用这一个值——concat copy 对帧率一致性有硬要求，差一处就被拒。
    fps_out = float(fpv.get("fps_out") or FPS)
    # 音频适配（2026-11-22，独立开关默认关）：每套抽一个音高系数与频谱倾角。
    # 挂 fp_seed 同一颗种子：重跑同一套结果一致，换一批就换一组。
    armor_k, armor_tilt = 1.0, 0.0
    if _audio_armor_on():
        _ar = random.Random(f"{fp_seed or grade_seed or pid}|armor")
        armor_k = _ar.uniform(0.985, 1.015)
        armor_tilt = _ar.choice((-1.0, 1.0)) * _ar.uniform(0.8, 1.5)
    armor_af = _audio_armor_chain(armor_k, armor_tilt)
    by_index = {ep["index"]: ep for ep in p["episodes"]}
    # BGM 文件提前体检（2026-11-05）：不存在的、ffprobe 读不出时长的（不是音频/
    # 已损坏），都别等切完二十段才炸。音符必须在这里就能播，后面才谈得上混。
    bgm = os.path.abspath(bgm_path) if bgm_path and str(bgm_path).strip() else ""
    if bgm:
        if not os.path.isfile(bgm):
            raise RuntimeError(f"BGM 文件不存在：{bgm}")
        if not core.ffprobe(bgm).get("duration"):
            raise RuntimeError("这个 BGM 文件读不了（不是音频或已损坏），换一个音乐文件试试")
    target_dir = os.path.abspath(out_dir) if out_dir else core.output_dir()
    # 先算要多少空间再动手：宁可一开始就告诉用户「不够」，也别切到第 15 段才失败
    core.check_space(target_dir, _estimate_mb(edl), "这次出片")
    os.makedirs(target_dir, exist_ok=True)
    # 中转判定（2026-11-18）：目标盘若不让 ffmpeg 写（受保护盘），整趟渲染挪到程序
    # 数据盘，成片最后由 Python 复制回目标盘——复制不受该拦截，路径对调用方不变。
    # 能写就原地渲染：临时分段仍放成片同盘，跟程序盘空间脱钩（原行为不动）。
    staged = not _ffmpeg_can_write(target_dir)
    render_dir = os.path.join(core.DATA_DIR, "_render_stage", pid) if staged else target_dir
    if staged:
        core.check_space(render_dir, _estimate_mb(edl), "这次出片(中转盘)")
        os.makedirs(render_dir, exist_ok=True)
    # 临时目录放渲染盘：切割 20 段约 1GB，跟程序盘空间脱钩。
    # work_tag（2026-11-19）：多套并行渲染时每套一个子目录，避免 s000.mp4 等
    # 分段文件被并发覆盖；单套调用不传，路径与老版完全一致。
    work = os.path.join(render_dir, "_render_tmp", pid, work_tag.strip())
    if staged:
        # 批量出片(cleanup=False)时调用方按「目标盘/_render_tmp/pid」清临时目录，
        # 中转后渲染盘变了，这条清理得自己登记到任务级，否则中转分段会一直留着。
        core.add_cleanup(lambda w=work: core.rmtree(w))
    if cleanup:
        core.rmtree(work)
    os.makedirs(work, exist_ok=True)

    out = os.path.join(render_dir, name)
    out_tmp = out + ".part.mp4"
    if os.path.exists(out_tmp):
        os.remove(out_tmp)

    try:
        # 开头就探测硬件编码器 / libass（各只探一次、进程内缓存），顺便把
        # 探测的几秒钟从第一段的进度里挪走。探测结果决定整批用哪套编码。
        _has_libass()
        enc0 = _venc_args()
        total = len(edl)
        parts: list[str] = []
        # 每段前是否真的插了转场卡（第 0 段恒 False）。音轨按这份标记在同位置
        # 插等长静音，保证重建音轨与含转场卡的视频逐段对齐。cut_all 可能因编码
        # 一致性重跑，所以每次进来都重置。
        trans_before: list[bool] = [False] * len(edl)

        # 逐段微变速计划（2026-11-17）：只在开「画面放大」时启用——指纹随机化
        # 是一整套机制，关掉放大就是不想要扰动，别偷偷只变速。
        # 必须在 cut_all 外面算一次：cut_all 可能因编码一致性重跑，重跑若换一组
        # 速度，前后两半的帧数就对不上，拼接照样会漂。
        seg_plans: list[dict] | None = None
        if zoom > 1.001 and edl:
            _bases: list[float] = []
            for seg in edl:
                sp0 = 1.0
                if speed_of:
                    try:
                        sp0 = float(speed_of(seg) or 1.0)
                    except (TypeError, ValueError):
                        sp0 = 1.0
                _bases.append(min(1.5, max(0.5, sp0)))
            seg_plans = speed_plan(
                [float(s["end"]) - float(s["start"]) for s in edl],
                _bases, fps_out, fp_seed or grade_seed, fp_level)

        def cut_all() -> None:
            """钩子卡 + 全部切段。抽成函数是为了编码一致性兜底：硬件编码器
            中途失效（_run_v 退软编）会让前后两半的 H.264 参数不一致，
            concat 直接 copy 拼出来的片子可能在部分播放器上花屏——所以
            编码参数一变就整批重出一次（罕见路径，多花几十秒换稳定）。"""
            parts.clear()
            trans_before[:] = [False] * len(edl)
            # 成片时间轴的游标：随机分区调色要按「观众看到的第几分钟」切区，
            # 所以钩子卡的时长也得算进去。每次重出都从 0 起（cut_all 可能被调两次）。
            at = hook_sec if hook else 0.0
            # 开头镜像只给第一个**画面段**：钩子卡是纯黑大字，翻它既没意义
            # 又会把字翻反，所以翻的是卡后面那段正片的开头。整套一个方向，
            # 不跟着随机调色每块变（那会变成每段开头都闪一下）。
            first_flip = ""
            if (fx or {}).get("first_mirror"):
                d = str((fx or {}).get("first_mirror_dir") or "v").strip().lower()
                first_flip = d if d in ("h", "v") else "v"
            # 钩子卡排在最前。它自己失败（没字体、文案异常）就跳过——一张
            # 1.8 秒的卡不值得让整套 5 分钟的片子渲染失败。
            # 卡的背景取第一段的某一帧（模糊缓推），取不到就自动纯黑。
            if hook:
                if progress:
                    progress(0, "生成开场钩子卡")
                card = os.path.join(work, "hook.mp4")
                bg_src, bg_ts = None, 1.0
                if edl:
                    e0 = by_index.get(edl[0]["ep"])
                    if e0:
                        bg_src = source_path(p, e0, src_ver)
                        seg0 = max(0.5, float(edl[0]["end"]) - float(edl[0]["start"]))
                        # 取段内靠前但不顶头的一帧：开头一帧常是黑场/转场
                        bg_ts = float(edl[0]["start"]) + min(1.0, seg0 / 3)
                try:
                    if _hook_card(hook, card, hook_sec, bg_src, bg_ts, fps=fps_out):
                        parts.append(card)
                except Exception:  # noqa: BLE001
                    pass
            # 全自动随机：按集缓存镜像/动效决定（同一集多段一致），种子含 run_id。
            # 只在「能翻字」的素材上给镜像——原始视频带原字幕，翻了字会反。
            _ep_pick: dict[str, dict] = {}
            can_mirror = src_ver in ("nosub", "live")
            prev_src: str | None = None        # 前一段的源文件，给转场卡取尾帧
            prev_end: float = 0.0
            for i, seg in enumerate(edl):
                ep = by_index.get(seg["ep"])
                if not ep:
                    raise RuntimeError(f"第 {seg['ep']} 集不存在")
                src = source_path(p, ep, src_ver)
                dst = os.path.join(work, f"s{i:03d}.mp4")
                # 段间转场空境：在第 i 段之前插一张（i>0）。背景取前一段尾帧，
                # 切点 = 前一段 end（也就是本段 start，两者首尾相接）。
                # 失败只跳过这张卡——0.7s 的增强项不值得让整套片子失败。
                if trans_sec > 0 and i > 0 and prev_src:
                    if progress:
                        progress(i / total * 88, "生成段间转场")
                    tcard = os.path.join(work, f"t{i:03d}.mp4")
                    try:
                        _trans_card(prev_src, prev_end, tcard, trans_sec, fps=fps_out)
                        parts.append(tcard)
                        trans_before[i] = True
                        at += trans_sec     # 转场占成片时长，调色分区要算进去
                    except Exception:  # noqa: BLE001
                        pass
                # 这一集的随机决定（镜像 + 字幕动效），按集号取，段间稳定
                ep_pick = (_ep_pick.setdefault(str(seg["ep"]),
                           auto_ep_pick(seg["ep"], fp_seed or grade_seed))
                           if auto_fx else {})
                if progress:
                    progress(i / total * 88,
                             f"切第 {i + 1}/{total} 段（第 {seg['ep']} 集 {seg['start']:.1f}s）")
                # 段计划和变速必须先算出来：下面估段内总帧数要用 plan["frames"]，
                # fallback 还要用 sp。这两个变量曾在赋值之前被引用，导致第一集
                # 直接 UnboundLocalError「plan 未绑定」、整批出片失败（2026-11-18）。
                plan = seg_plans[i] if seg_plans else None
                sp = 1.0
                if speed_of:
                    try:
                        sp = float(speed_of(seg) or 1.0)
                    except (TypeError, ValueError):
                        sp = 1.0
                    sp = min(1.5, max(1.0, sp))
                if plan:
                    # 计划里的 speed 已含微抖与归一，不能再走上面的 1.0-1.5 夹取
                    sp = float(plan["speed"])
                # 逐帧进度：只在「选视频直接出片」启用。帧号 → 本段内 0~1 → 全局百分比。
                # 段与段之间留一点边界（本段最多到 (i+1)/total），不越到下一段的格子。
                # AI 出片不传 on_frame（is_direct=False），整条逻辑原样不动。
                seg_total = (plan["frames"] if plan else None) or \
                    max(1, int((float(seg["end"]) - float(seg["start"])) * fps_out / max(sp, 0.01)))
                on_frame = None
                if progress and is_direct:
                    def _frame(n, _i=i, _t=seg_total, _tot=total):
                        progress((_i + min(1.0, float(n) / _t)) / _tot * 88,
                                 f"切第 {_i + 1}/{_tot} 段 · 已编码 {min(99, int(float(n) / _t * 100))}%")
                    on_frame = _frame
                title = title_of(seg) if title_of else None
                # 首段从黑淡入：接在钩子卡的淡出后面，拼出不重编码的入场转场。
                # 卡没生成成功（字体缺失等）时首段淡入也无害——从黑进来本来自然。
                # 即使没传 fx 也要过 grade_spec：里面的质感四项有出厂默认
                # （颗粒/光晕/暗角/横向变形开在无感档），不传就等于全关，
                # 会导致「面板显示已开、成片却没有」的对不上。
                # 全自动随机：每段一套无感级几何微抖（放大/变形各段不同），
                # 在整套基准上抽，rot/sx/sy/gain 沿用整套值不抖（避免接缝构图跳）。
                seg_fp = auto_seg_view(fpv, i, fp_seed or grade_seed) if auto_fx else fpv
                gspec = fp_grade(grade_spec(fx or {}, i, at, grade_seed), seg_fp,
                                 seg_index=i, seed=grade_seed)
                # 整幅镜像必须按素材来：原始视频带原字幕，翻了字会反，所以
                # 无论自动随机还是手勾的单套设置，一律强制不翻（2026-11-17）。
                # 只有能翻字的素材（干净底片 / 现场换字）才参与随机。
                # 两条出片路径的随机粒度不同（2026-11-18 用户强调，别混）：
                #   · AI 编辑出片 → 按集：整集一起翻/不翻（auto_ep_pick）。
                #   · 选视频直接出片 → 按时间窗：一条片子每 60 秒独立翻/不翻
                #     （direct_mirror_plan），因为直出一个视频就是一整段、没有集。
                if not can_mirror:
                    gspec["mirror"] = False
                elif auto_fx and is_direct:
                    gspec["mirror"] = direct_mirror_plan(
                        float(seg["start"]), float(seg["end"]),
                        fp_seed or grade_seed)
                elif auto_fx:
                    gspec["mirror"] = bool(ep_pick.get("mirror"))
                # 去字幕有两条路（2026-11-16 起副本只抹不烧）：
                #   src_ver=nosub → 用第 2 步编好的干净副本，这一趟只切 + 烧自己的字；
                #   src_ver=live  → 用原片，这一趟里「抹原字幕 → 画面处理 → 烧新字幕」。
                # 两者都是先镜像后烧字，才能和整幅翻转共存。
                live = src_ver == "live"
                # 2026-11-16：副本是「干净版」（第 2 步只抹不烧）时，字幕也在这一趟
                # 现烧，但画面不用再抹一遍——抹是全流程最贵的动作（实测同一段
                # 重算掩码 60s、从干净副本切只要 18s，3.4 倍）。所以能复用副本就复用。
                burn = live or (src_ver == "nosub" and not _copy_burned(p))
                clean = _live_clean(p, ep, float(seg["start"]), float(seg["end"])) if live else None
                cap = _live_cap(p, ep, float(seg["start"]), float(seg["end"]), sp,
                                fx, motion_override=(ep_pick if auto_fx and burn else None)
                                ) if burn else None
                def _do_cut() -> None:
                    _cut(src, seg["start"], seg["end"], dst, title, sp, zoom,
                         fade_in=bool(hook) and i == 0, grade=gspec,
                         cap=cap, clean=clean,
                         first_flip=first_flip if i == 0 else "", fp=seg_fp,
                         frames=plan["frames"] if plan else None,
                         out_dur=plan["out_dur"] if plan else None,
                         on_frame=on_frame, fps=fps_out)

                _do_cut()
                # 段级漂移守卫（2026-11-22 R3.3①/R3.4）：只测**变速段**（缓冲加速
                # /逐段微抖的段是漂移高危，原速段不受测——20 段全测要 40-80s 的
                # ffprobe，白拖）。帧数路径分段不带音轨，测的是「视频时长 vs 计划
                # 时长」（音频由 _audio_track 按同一份计划重建，视频对上=对上）；
                # 嵌音轨路径两条流都在，测视频时长 vs 预期（音画等长由成片级
                # 终检兜底）。❌ 原参数重渲一次，仍 ❌ 整套失败——人话里带段号。
                if (plan and abs(float(plan["speed"]) - 1.0) > 0.005) or abs(sp - 1.0) > 0.005:
                    exp_v = (float(plan["out_dur"]) if plan
                             else max(0.5, float(seg["end"]) - float(seg["start"])) / (sp or 1.0))
                    got = core.probe_stream_duration(dst, "v")
                    if got > 0 and abs(got - exp_v) > 0.5:
                        if progress:
                            progress(i / total * 88, f"第 {i + 1} 段时长异常，原参数重渲一次")
                        _do_cut()
                        got2 = core.probe_stream_duration(dst, "v")
                        if got2 > 0 and abs(got2 - exp_v) > 0.5:
                            raise RuntimeError(
                                f"第 {i + 1} 段渲出来 {got2:.2f}s，与计划 {exp_v:.2f}s 差 "
                                f"{abs(got2 - exp_v):.2f} 秒，重渲一次仍未恢复——"
                                "这条会有音画不同步，请带日志反馈")
                parts.append(dst)
                at += (plan["out_dur"] if plan else
                       max(0.5, float(seg["end"]) - float(seg["start"])) / (sp or 1.0))
                # 记下这一段，给下一个段间转场卡当背景（取尾帧）
                prev_src = src
                prev_end = float(seg["end"])

        if progress:
            det0 = _detect_hw()
            how = (det0.get("name", "").replace("h264_", "").upper() + " 显卡加速"
                   if det0 and _hw_mode() == "auto" else "CPU 编码")
            progress(0, f"准备渲染（{how}）")
        cut_all()
        if _venc_args() != enc0:
            if progress:
                progress(2, "显卡编码中途失效，改用 CPU 重出各段（只需一次）")
            cut_all()

        listfile = os.path.join(work, "list.txt")
        with open(listfile, "w", encoding="utf-8") as f:
            for path in parts:
                # concat 清单里必须用正斜杠：反斜杠会被 ffmpeg 当转义符
                f.write(f"file '{path.replace(os.sep, '/')}'\n")

        if progress:
            progress(90, "拼接与统一响度")
        if seg_plans:
            # 有帧数计划时走「视频流 copy + 独立音轨」：分段 mp4 的 AAC 填充
            # 会在拼接时按段累加（实测每段约 30ms），必须丢掉它们自带的音轨，
            # 用 _audio_track 按同一份计划重建一条连续音轨，最后只编一次 AAC。
            vid = os.path.join(work, "video_only.mp4")
            _run([core.ff("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                  "-fflags", "+genpts",
                  "-f", "concat", "-safe", "0", "-i", listfile,
                  "-c:v", "copy", "-an", vid])
            card_dur = 0.0
            if parts and os.path.basename(parts[0]) == "hook.mp4":
                try:
                    card_dur = float(core.ffprobe(parts[0]).get("duration") or 0.0)
                except Exception:  # noqa: BLE001
                    card_dur = 0.0
            aud = _audio_track(p, edl, seg_plans, src_ver, work,
                               card_dur=card_dur,
                               gain_db=float(fpv.get("gain") or 0.0),
                               trans_before=trans_before if trans_sec > 0 else None,
                               trans_sec=trans_sec)
            _run([core.ff("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                  "-fflags", "+genpts",
                  "-i", vid, "-i", aud,
                  "-map", "0:v:0", "-map", "1:a:0",
                  "-c:v", "copy",
                  # 音频适配排在 loudnorm 之前：先变调/倾谱，再统一响度
                  "-af", ",".join([*armor_af, "loudnorm=I=-16:TP=-1.5:LRA=11"]),
                  "-ar", "48000", "-ac", "2",
                  "-c:a", "aac", "-b:a", "192k",
                  # 指纹：把编码器版本、原始创建时间这类容器元数据一起清掉，
                  # 不让成片带着"同一台机器同一套参数"的痕迹
                  "-map_metadata", "-1",
                  *_sei_args(),
                  "-movflags", "+faststart", "-shortest", out_tmp])
        else:
            # 没开画面放大 = 没有帧数计划，保持原路子：分段自带音轨直接拼。
            # 拼接一步到位：视频流直接 copy，音频边拼边重编码（loudnorm）。
            # 之前是「先 -c copy 拼接、再二次封装响度」，部分源视频的音频时间戳
            # 不干净，copy 出来的中间文件在二次封装时报 Error muxing a packet；
            # 音频重编码后时间戳干净，从根上避开。genpts 兜底修段间时间戳。
            _run([core.ff("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                  "-fflags", "+genpts",
                  "-f", "concat", "-safe", "0", "-i", listfile,
                  "-c:v", "copy",
                  # 音频适配排在 loudnorm 之前（同帧数路径）
                  "-af", ",".join([*armor_af, "loudnorm=I=-16:TP=-1.5:LRA=11"]),
                  "-ar", "48000", "-ac", "2",
                  "-c:a", "aac", "-b:a", "192k",
                  # 指纹：把编码器版本、原始创建时间这类容器元数据一起清掉，
                  # 不让成片带着"同一台机器同一套参数"的痕迹
                  "-map_metadata", "-1",
                  *_sei_args(),
                  "-movflags", "+faststart", out_tmp])
        # 去掉原片背景音乐（2026-11-18 用户定）：只处理**成片这一条音轨**，
        # 排在封装完成之后、混 BGM 之前——两条渲染分支（有无帧数计划）最后都汇到
        # out_tmp，这里只换音频不动画面，原有的音画同步逻辑一行都不用改。
        # 分离会连关门、碗筷、雨声一起去掉（模型只分「人声/不是人声」），
        # 所以按 keep_ambient_db 把伴奏压到很轻再混回去，留一点现场底。
        if voice_iso:
            if not voiceiso.available():
                raise RuntimeError(
                    "去原片背景音乐缺少分离模型：%s（64MB）" % voiceiso.model_path())
            if progress:
                progress(91, "去掉原片背景音乐（首次较慢，之后走缓存）")
            iso_in = os.path.join(work, "iso_in.wav")
            voiceiso.extract_audio(out_tmp, iso_in)
            clean = voiceiso.separate(
                iso_in,
                on_progress=(lambda f: progress(91 + int(f * 3),
                                                "分离人声 %d%%" % int(f * 100))
                             if progress else None))
            iso_out = out_tmp + ".iso.mp4"
            voiceiso.replace_audio(out_tmp, clean, iso_out)
            os.replace(iso_out, out_tmp)
        os.replace(out_tmp, out)
        # BGM 混音排在拼接之后（视频流 copy、只重编音频），失败即整套失败——
        # 用户明确勾了「配乐 BGM」，悄悄出个没音乐的片子才是事故。
        if bgm:
            if progress:
                progress(95, "混入 BGM（自动压低音量，不盖台词）")
            _mix_bgm(out, bgm, bgm_vol)
    except BaseException:
        # 失败时必须清干净：磁盘满的时候，残留的分段文件会把最后一点空间也吃光，
        # 用户重试一次还是失败，看起来像「怎么点都出不来」。
        # 清理自身也不允许抛异常——删不掉就留着，别盖住真正的错误。
        try:
            if os.path.exists(out_tmp):
                os.remove(out_tmp)
            # BGM 混音的中间产物（out.bgm.mp4）也要收走
            bgm_tmp = out + ".bgm.mp4"
            if os.path.exists(bgm_tmp):
                os.remove(bgm_tmp)
            # 人声分离的中间产物（out.iso.mp4）
            iso_tmp = out_tmp + ".iso.mp4"
            if os.path.exists(iso_tmp):
                os.remove(iso_tmp)
        except Exception:
            pass
        if cleanup:
            core.rmtree(work)
        raise

    info = core.ffprobe(out)
    # 音画漂移终检（2026-11-22 R3.2 分级口径，替代旧的「1 秒且 2%」单级）：
    # ⚠️ ≤0.5s 只记不拦（轻微，几乎感知不到）；❌ >0.5s 标红进摘要。成片级
    # **只标红不动片**（R3.4）——段级已经在变速段守过一道，到这里的 ❌ 多半是
    # 拼接/混音环节的问题，自动重渲整套代价太大，交给用户试看判断。
    _lvl, _drift, _note = av_drift_check(out)
    if _lvl != "ok":
        note = (f"成片音画可能不同步：{_note}，建议试看结尾确认"
                + ("（轻微，一般感知不到）" if _lvl == "warn" else ""))
        print(f"[render] {note}", flush=True)
        info["av_note"] = note
        info["av_drift"] = round(_drift, 2)
        if progress:
            try:
                progress(100, note)
            except Exception:  # noqa: BLE001 —— 告警绝不挡交付
                pass
    # 中转盘渲染：成片先落在可写盘，这里由 Python 复制回用户指定的目标盘。
    # 复制用 shutil（Python 进程写 F 盘正常，只有 ffmpeg.exe 被拦），带 .part 临时名
    # 再原子改名，避免中途失败留下半截成片。返回给调用方的 name 不变，成片列表照常指向
    # 目标盘同一位置。
    if staged:
        final = os.path.join(target_dir, name)
        final_tmp = final + ".copying"
        shutil.copy2(out, final_tmp)
        os.replace(final_tmp, final)
        # 中转盘上的成片使命已完成（2026-11-22）：以前一直留着，批量出几十套
        # 就把数据盘吃满几十 G。复制成功后就地删掉（真删，不进回收站）；
        # 删失败不碍事——成片已在目标盘，顶多剩一个文件占点磁盘。
        try:
            os.remove(out)
        except Exception:  # noqa: BLE001
            pass
        result = {"name": name, "duration": info["duration"], "size": os.path.getsize(final)}
    else:
        result = {"name": name, "duration": info["duration"], "size": os.path.getsize(out)}
    # 清理排在最后：成片已落盘、结果已组装完再删临时目录。
    # 之前放在 finally 里，等于「删临时文件」挡在「返回结果」前面——
    # 一旦删除被守卫拦下，片子写好了但项目记录没更新（BUG 44 的同款顺序错误）。
    if cleanup:
        core.rmtree(work)
    return result


def run(pid: str) -> None:
    p = core.load(pid)
    if not p:
        return
    edl = p.get("edl") or []
    if not edl:
        raise RuntimeError("还没有剪辑结构，请先让 AI 出表")

    name = f"{core.safe_name(p['name'])}_{p['target_minutes']}min_{time.strftime('%m%d%H%M')}.mp4"
    # 历史单片导流项目也认「用哪份视频」这个全局默认；其余画面参数不动，
    # 免得老项目突然换了颜色/变形。
    fx0 = (p.get("batch") or {}).get("fx_default") or {}
    ver = str(fx0.get("src_ver") or "").strip() if isinstance(fx0, dict) else ""
    info = render_edl(p, edl, name, fx={"src_ver": ver},
                      progress=lambda pct, msg: core.set_task(pid, "render", pct, msg))

    p["output"] = info["name"]
    p["output_duration"] = info["duration"]
    p["output_size"] = info["size"]
    p["stage"] = "rendered"
    # 音画漂移告警落项目（2026-11-22）：任务消息马上会被清掉，日志只在黑窗里，
    # 存进 project.json 用户回来还能看到这条提醒。没有告警就不写字段。
    keys = ["output", "output_duration", "output_size", "stage"]
    if info.get("av_note"):
        p["output_av_note"] = info["av_note"]
        keys.append("output_av_note")
    core.set_task(pid, None)
    core.save_merge(p, keys)
