"""去掉原片字幕：逐集编码成一份独立视频（2026-11-11，2026-11-12 收敛）。

排在「台词整理」之后：每一集先用「柔和抹掉」把原来的字幕处理掉，产出每集
一条独立视频，后面的「剪辑与出片」直接从这些视频切段。

2026-11-12 用户定：这一步只有「柔和抹掉」一种做法、位置全自动识别、不再
加自己的字幕。所以 clean_cfg 里 mode/auto/newsub 三项是写死的，不跟参数走。

时间码为什么还能对上：音频直接复制、不加 setpts、不换帧率，副本跟原片时长
一致、帧位置一致，所以台词时间码不用重算、也不用重跑转写。

参数与滤镜复用 render.subtitle_spec / _subtitle_soft_graph / _subtitle_crop_graph。
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import time

from . import core, render

# 进度条上留给「对齐字幕时间」的那一段（其余按集数走）。开着自动对齐时
# 这段时间可能是一集分钟级，全压在 0% 会让人以为卡住。
RETIME_PCT = 8.0


def out_dir(pid: str) -> str:
    """产物目录：项目自己的数据目录里，删项目时会一起清掉，不污染素材文件夹。"""
    return os.path.join(core.PROJECTS, pid, "nosub")


def default_cfg() -> dict:
    # 界面上的可调项：出场动效、动效幅度、抹除柔和程度、抹掉的地方怎么补，
    # 外加「生成时自动对齐原片字幕时间」开关。
    #   处理方式固定「柔和抹掉」、位置固定自动识别、字幕固定加回去（不能关）。
    # pos/size/fill 由 subdetect 现算；newsub_pos 也跟着识别的字幕带中心走，
    # 这里只是识别失败时的兜底值。
    return {"mode": "soft", "auto": True, "pos": 0.75, "size": 0.10, "soft": 0.5,
            "fill": "above", "fill_mode": "mirror",
            "newsub": True, "newsub_pos": 0.80, "newsub_motion": 0.5,
            "newsub_style": "rise", "retime": True}


def _flag(raw: dict, key: str, dflt: bool) -> bool:
    """勾选框的宽松读取：前端传 true/false，老配置里可能是 "0"/"false"/空。"""
    v = raw.get(key, dflt)
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no", "off")
    return dflt if v is None else bool(v)


def clean_cfg(raw: dict) -> dict:
    """夹取成安全区间。

    **mode / auto / newsub 一律强制**，不跟着传进来的值走：界面上已经没有
    这三项了。老项目里存过 crop、手填位置、newsub=False 的话，如果这里还照
    收，就会出现「界面上没得选、后台还按老设置做」——最典型的是 newsub=False
    的老项目点了生成，出来的副本没有字幕，跟界面上写的行为对不上。
    """
    raw = raw if isinstance(raw, dict) else {}
    d = default_cfg()

    def num(key: str, lo: float, hi: float) -> float:
        try:
            v = raw.get(key)
            return min(hi, max(lo, float(d[key] if v is None or v == "" else v)))
        except (TypeError, ValueError):
            return d[key]

    fill = str(raw.get("fill") or "above").strip().lower()
    style = str(raw.get("newsub_style") or "").strip().lower()
    if style not in render.NEWSUB_STYLES:
        style = "rise"
    # 填充方式：mirror = 当前帧上下镜像（快，一集十几秒）；
    # borrow = 借同镜头干净帧（慢一倍多，强结构背景更自然，见 nosubfill.py）
    fmode = str(raw.get("fill_mode") or "mirror").strip().lower()
    if fmode not in ("mirror", "borrow"):
        fmode = "mirror"
    return {"mode": "soft",
            "auto": True,
            "pos": num("pos", 0.50, 0.92),
            "size": num("size", 0.03, 0.20),
            "soft": num("soft", 0.0, 1.0),
            "fill": fill if fill in ("above", "below") else "above",
            "fill_mode": fmode,
            "newsub": True,
            "newsub_pos": num("newsub_pos", 0.50, 0.92),
            "newsub_motion": num("newsub_motion", 0.0, 1.0),
            "newsub_style": style,
            # 生成前自动按原片字幕重排时间（默认开，2026-11-16 用户定：不再单独点按钮）
            "retime": _flag(raw, "retime", True)}


def sig_of(cfg: dict, episodes: list[dict] | None = None,
           style: bool = True) -> str:
    """参数指纹。episodes=None 只算「设置指纹」（给前端比对面板用）；
    传了 episodes 时把每集台词内容也并进去（给后台判断要不要重做副本）。

    第 1 步改错字走的是 PUT /lines，不会动任何代次，光靠设置指纹会拿回带旧字
    的副本——所以跳过的判断必须用带台词的完整指纹。

    style=False（2026-11-16）：**副本指纹不算字幕样式**。第 2 步的副本现在只
    负责「把原字幕抹干净」，台词是出片时才烧上去的（`src_ver=nosub` 走现场烧字）。
    样式/动效/位置改了跟副本像素无关，算进去只会让改个字幕样式就白重编 31 集。
    """
    s = render.subtitle_spec(cfg)
    m = s.get("mode") or ""
    if not m:
        return ""
    pos = int(round(s["pos"] * 1000))
    if m == "crop":
        base = f"C{pos}"
    else:
        # fill 也要进指纹：同样的位置换个填充方向，出来的画面完全不同
        fill = "d" if str(s.get("fill") or "above") == "below" else "u"
        # 前缀 E2/E3/E4 = 抹除引擎版本。E1 是「整条替换」，E2 是「逐句包围盒掩码」，
        # E3 修了两处会留残影的问题（框与整集文字带取并集、掩码 lut 拉满），
        # E4 让**每集按自己的字幕带中心**烧新字幕（旧版套第一集的位置，多来源项目会跳位）。
        # 画面完全不同，没这个位的话老项目会命中缓存、以为已经做过了（不动）
        # 前缀里的版本号直接取 subdetect.VERSION：识别判据一改，指纹自动失效，
        # 不会再出现「spans 重算了、成品却按旧签名跳过」的情况（踩过：改了
        # 判据后重跑只花 46 秒，一看全是跳过的）。
        from . import subdetect
        base = (f"E{subdetect.VERSION}F{pos}{int(round(s['size'] * 1000))}"
                f"{int(round(s['soft'] * 100))}{fill}")
    # 填充方式换了出来的画面完全不同，必须进指纹，否则切了开关点生成会被跳过
    base += "B" if cfg.get("fill_mode") == "borrow" else "M"
    if not style:
        # 只抹不烧：副本的像素与字幕样式无关，台词也由出片时现算
        return base
    if not cfg.get("newsub"):
        return base
    # 用 newsub_spec 折算后的结果算指纹：style 由它统一判定（老配置按 motion 折算），
    # 这里再自己判一次就会出现「两处口径不一致、指纹永远对不上」。
    # 前缀 N5 = 第五版出场参数（N2 放大动效幅度，N3 加「字幕比声音晚」，
    # N4 又把它删了，N5 烧字去标点）。
    # 参数算法改了但滑杆值没变，不加这个位老项目会被判成「参数没变」直接跳过，
    # 用户点了生成却还是旧观感。N5 与 web/app.js 的 nsSig 必须同步。
    ns = render.newsub_spec(cfg)
    base += (f"|N5{int(round(float(ns.get('pos') or 0.80) * 1000))}"
             f"{int(round(float(ns.get('motion') or 0.0) * 100))}"
             f"{str(ns.get('style') or 'rise')[0]}")
    if episodes is None:
        return base
    h = hashlib.sha1()
    for e in episodes:
        h.update(f"#{e.get('index')}\n".encode("utf-8", "replace"))
        for ln in (e.get("lines") or []):
            h.update(f"{ln.get('start')}|{ln.get('end')}|{ln.get('text')}\n"
                     .encode("utf-8", "replace"))
    # |S1 是**烧字口径**版本（2026-11-19：一条超过 15 字就按标点拆成几条上屏）。
    # 特意只加在带台词的这一版里：settings 指纹（cfg_sig，前端比对 + 副本复用判定）
    # 到上面就 return 了、不含它，所以前端不会误报「副本设置变了」；而出片的
    # ver_sig 用的是带台词这版（batch.py 里 ns.sig + burn_sig / live+sig_of(cfg,eps)），
    # 含它 → 旧成片判过期重渲，不会拿旧观感的片子充数。
    return f"{base}|{render.CAP_VER}|{h.hexdigest()[:10]}"


def _out_name(ep: dict) -> str:
    stem = os.path.splitext(str(ep.get("file") or ""))[0]
    stem = core.safe_name(stem)[:36].strip() or "video"
    return f"{int(ep.get('index') or 0):02d}_{stem}.mp4"


def _patch_span(cfg: dict, sp: dict | None):
    """这一集填充条的上下范围 = 参数带与逐句矩形并集，保证盖住所有要修的地方。

    返回 (spans, pos, size)。没有逐句数据时三段都是 None，调用方走整条替换。
    """
    spans = (sp or {}).get("spans") or None
    if not spans:
        return None, None, None
    u = (sp or {}).get("union") or {}
    try:
        top = min(float(cfg["pos"]), float(u["t"]))
        bot = max(float(cfg["pos"]) + float(cfg["size"]), float(u["b"]))
    except (KeyError, TypeError, ValueError):
        return spans, None, None
    return spans, round(top, 4), round(max(0.02, bot - top), 4)


def _cap_h(sd_ep: dict | None) -> float:
    """这一集原字幕的**字高**（归一化到画面高度），给新字幕定字号用。

    别用 band["size"]：那是**搜索窗高度**，比字高大得多（第 1 集实测
    窗 0.0506 / 字 0.0281），拿它算字号会把新字幕放大近一倍。
    字高有两个来源，按可靠性排：
      ① band 的 text_top/text_bottom —— 整集行密度分析直接给出的文字上下沿；
      ② 逐句框高中位数 ÷ (1 + 2×SPAN_PAD_Y) —— 框就是按字高外扩 pad 得来的，
         实测与 ① 完全一致（都是 0.0281）。
    """
    from . import subdetect
    b = (sd_ep or {}).get("band") or {}
    try:
        h = float(b.get("text_bottom") or 0) - float(b.get("text_top") or 0)
    except (TypeError, ValueError):
        h = 0.0
    if h > 0.002:
        return h
    hs = []
    for x in ((sd_ep or {}).get("spans") or []):
        try:
            hs.append(float(x["b"]) - float(x["t"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not hs:
        return 0.0
    hs.sort()
    return hs[len(hs) // 2] / (1.0 + 2.0 * subdetect.SPAN_PAD_Y)


def _graph(cfg: dict, sub_tail: str = "", spans: list | None = None,
           pos: float | None = None, size: float | None = None) -> str:
    """整片滤镜串，输出标签 [vout]。全部用比例表达式，源片分辨率任意都成立。

    去掉原字幕之后、输出之前，把这一集的新字幕（sub_tail：ASS 的 subtitles
    滤镜，或无 libass 时的 drawtext 串）接上——同一趟编码里「去字幕 + 加字幕」
    一次做完，不多编一遍。

    spans 给了就走「精准抹除」：只修这些矩形覆盖的区域（每集形状不同），
    其余画面一个像素都不动。pos/size 是这一集填充条的上下范围。
    """
    s = render.subtitle_spec(cfg)
    if pos is not None and size is not None:
        s = dict(s, pos=round(float(pos), 4), size=round(float(size), 4))
    if s.get("mode") == "crop":
        base, label = render._subtitle_crop_graph(s), "[__sub_c]"
    elif spans:
        base, label = render._subtitle_mask_graph(s, spans), "[__sub_base]"
    else:
        base, label = render._subtitle_soft_graph(s), "[__sub_base]"
    body = f"{sub_tail},format=yuv420p" if sub_tail else "format=yuv420p"
    return f"{base};{label}{body}[vout]"


def _encode(src: str, dst: str, graph: str, duration: float,
            on_pct) -> None:
    """编码一集，实时把 ffmpeg 的进度报出去。

    两级兜底：① 音频 copy 不兼容源格式时改 AAC 重编一次（时长仍一致）；
    ② 硬件编码器中途失效时退回软编再跑一次，并清掉基准缓存。
    写 .part.mp4，成功才原子替换，中断不会留下被误认成「已完成」的半截文件。
    """
    part = dst + ".part.mp4"
    if os.path.exists(part):
        os.remove(part)
    base = [core.ff("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", src, "-filter_complex", graph,
            "-map", "[vout]", "-map", "0:a?", "-sn", "-dn"]
    vargs = render._venc_args()
    used_hw = vargs and vargs[1] != "libx264"
    attempts = [(vargs, ["-c:a", "copy"]),
                (vargs, ["-c:a", "aac", "-b:a", "160k"])]
    if used_hw:
        attempts.append((list(render._SW_ARGS), ["-c:a", "aac", "-b:a", "160k"]))
    last = ""
    for i, (va, aa) in enumerate(attempts):
        if i == 2:
            render._HW["off"] = True
            render._hw_cache_clear()
        cmd = [*base, *va, *aa, "-movflags", "+faststart",
               "-progress", "pipe:1", "-nostats", part]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="replace", bufsize=1)
        except OSError as exc:
            last = str(exc)
            continue
        # 登记子进程：点「中止」时由 core 直接 kill，不必等这一集编码跑完
        core.register_proc(proc)
        try:
            for line in proc.stdout or []:
                core.check_cancel()
                line = line.strip()
                if line.startswith("out_time_ms=") or line.startswith("out_time_us="):
                    try:
                        raw = float(line.split("=", 1)[1])
                        sec = raw / (1e6 if "_us=" in line else 1e3)
                        if duration > 0:
                            on_pct(min(99.0, sec / duration * 100.0))
                    except (ValueError, IndexError):
                        pass
                elif line.startswith("ffmpeg") or "Error" in line or "error" in line:
                    last = (last + "\n" + line)[-600:]
            core.wait_cancelable(proc)
        except BaseException:
            # 管道被 kill 断掉时会抛各种读异常；只要正在中止，就统一翻成中止信号，
            # 别让上层把它当成「这一集编码失败」继续跑下一集。
            core.check_cancel()
            raise
        finally:
            core.unregister_proc(proc)
        if proc.returncode == 0 and os.path.isfile(part) and os.path.getsize(part) > 1024:
            os.replace(part, dst)
            return
        last = (last + f"\n退出码 {proc.returncode}")[-600:]
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass
    raise RuntimeError("这一集处理失败，请重试")


def _detect_key(p: dict, episodes: list[dict]) -> str:
    """检测缓存键：素材换了（同一个文件名但内容变了）就得重新识别。"""
    from . import subdetect
    # 带算法版本号：判据一改，老项目里存的位置就不能再用（位置可能差一截）
    parts = [f"v{subdetect.VERSION}"]
    for e in episodes:
        f = str(e.get("file") or "")
        try:
            st = os.stat(os.path.join(p["src_dir"], f))
            parts.append(f"{f}:{st.st_size}:{int(st.st_mtime)}")
        except OSError:
            parts.append(f"{f}:0")
    return "|".join(parts)


def _detect(p: dict, episodes: list[dict], on_msg=None, force: bool = False) -> dict:
    """自动识别字幕位置（最多测 6 集取中位数，结果按素材指纹缓存）。

    为什么不每集都测：字幕位置对一部剧是固定的，抽样足够；
    20 集的项目每集抽 24 帧要等十几秒，用户会以为卡住了。
    缓存键含文件名+大小+修改时间，换了素材自动失效。
    """
    from . import subdetect          # 只有这一步需要 numpy，按需导入

    prev = p.get("nosub") or {}
    key = _detect_key(p, episodes)
    old = prev.get("detect") or {}
    if not force and old.get("key") == key and old.get("pos"):
        return old
    if on_msg:
        on_msg("正在识别字幕位置…")
    res = []
    for e in episodes[:5]:
        src = os.path.join(p["src_dir"], str(e.get("file") or ""))
        if not os.path.isfile(src):
            continue
        try:
            dur = float(core.ffprobe(src).get("duration") or 0)
        except Exception:  # noqa: BLE001
            continue
        d = subdetect.detect(src, dur)
        if d:
            d["ep"] = str(e.get("index"))
            res.append(d)
    m = subdetect.median_of(res)
    if not m:
        return {}
    m["key"] = key
    return m


def _spans(p: dict, episodes: list[dict], band: dict | None = None,
           on_msg=None, force: bool = False) -> tuple[dict, list[str]]:
    """逐集量「字幕实际出现的矩形」（宽高都贴合字），按素材指纹缓存。

    跟 _detect 的分工：_detect 只量「整部剧的那一条」（给滑杆用），
    这里量「每一集每一句各自的矩形」——字幕换句时宽度能从 13% 跳到 87%，
    高度也可能一行变两行，只有逐集逐句量才谈得上「只修字幕那一块」。

    一集 60-80 秒要抽 240-320 帧，20 集第一次跑多花一两分钟，之后走缓存。
    单集失败不会影响别的集：那一集会退回整条替换。
    """
    from . import subdetect

    prev = p.get("nosub") or {}
    key = _detect_key(p, episodes)
    old = prev.get("spans") or {}
    if not force and old.get("key") == key and isinstance(old.get("by_ep"), dict):
        # 两处调用（run / ensure_spans）都按 (by_ep, fails) 二元组解包，
        # 命中缓存也必须返回同样的形状——只返回 by_ep 会让解包炸成
        # `too many values to unpack (expected 2)`（2026-11-19 实测，40 集全缓存时必现）。
        return old["by_ep"], []
    out: dict[str, dict] = {}
    fails: list[str] = []          # 2026-11-18 C：逐集识别失败要可见，不再静默吞掉
    total = len(episodes)
    for i, e in enumerate(episodes):
        fname = str(e.get("file") or "")
        src = os.path.join(p["src_dir"], fname)
        if not os.path.isfile(src):
            fails.append(f"第 {i + 1} 集找不到视频文件")
            continue
        if on_msg:
            on_msg(f"正在识别字幕区域 {i + 1}/{total}…")
        try:
            dur = float(core.ffprobe(src).get("duration") or 0)
            # **每集先量自己的字幕带**，不能拿全剧那一条去套。
            # _detect 抽 5 集取中位数，但一个项目里混着不同来源的片子时
            # （真人剧 + AI 生成片，1080×1920 与 720×1280 拼在一起），
            # 只有一种片源能测出来，其余集会拿别人的位置当搜索窗：
            # 窗口错位 → 字顶被切掉、画面下方的白色被并进来，
            # 框高从 2.8% 涨到 10.9%（2026-11-12 实测项目 2934765f8925 第 3 集）。
            b = band
            try:
                own = subdetect.detect(src, dur)
                if own and own.get("pos"):
                    b = own
            except Exception:  # noqa: BLE001
                b = band
            r = subdetect.detect_spans(src, dur, b)
        except Exception as exc:  # noqa: BLE001 —— 单集失败只记这一集、继续
            fails.append(f"第 {i + 1} 集：{str(exc)[:80] or type(exc).__name__}")
            continue
        if r and r.get("spans"):
            out[fname] = {"spans": r["spans"], "union": r["union"],
                          "count": r["count"], "band": b}
        else:
            # 没抛异常但一句都没框出（真的没字幕 / 画面太亮）：也算「没量到」，
            # 出片会退回固定位置抹除，得让用户知道这一集不是精准抹的。
            fails.append(f"第 {i + 1} 集没框出字幕区域，出片时用固定位置抹除")
    return out, fails


def ensure_spans(pid: str, on_msg=None) -> dict:
    """出片「现场做」（src_ver=live）之前调用：保证字幕区域和参数都量好了。

    第 2 步做过就直接复用它的缓存；没做过就在这儿补量一次，结果写回项目的
    `nosub` 字段——下次出片、或回头做第 2 步都能直接命中，不用再等一遍。

    为什么放在出片任务里而不是接口里：一集要抽几百帧（十几秒），20 集就是几分钟，
    不能把 HTTP 请求挂住。调用方在后台任务里，进度照常往外报。
    """
    p = core.load(pid)
    if not p:
        raise RuntimeError("项目不存在")
    eps = list(p.get("episodes") or [])
    if not eps:
        raise RuntimeError("这个项目没有导入视频")
    if not any(e.get("lines") for e in eps):
        raise RuntimeError("还没有整理台词，先去第 1 步「台词整理」做完再来出片")

    cfg = clean_cfg(p.get("nosub_cfg") or {})
    st = dict(p.get("nosub") or {})
    key = _detect_key(p, eps)
    old_d = st.get("detect") or {}
    old_s = st.get("spans") or {}
    need_d = not (old_d.get("key") == key and old_d.get("pos"))
    # 缓存命中条件：by_ep 非空（量到了字），或上次虽没量到但已记下失败（fail 字段在），
    # 两者都算「这批检测跑过」。否则全片真没字幕的项目每次出片都会重扫一遍。
    done_before = (isinstance(old_s.get("by_ep"), dict)
                   and (bool(old_s.get("by_ep")) or "fail" in old_s))
    need_s = not (old_s.get("key") == key and done_before)
    if not (need_d or need_s):
        return {"cfg": st.get("cfg") or cfg, "detect": old_d,
                "by_ep": old_s.get("by_ep") or {}, "fail": old_s.get("fail") or []}

    # 位置变了，逐句窗口也得重算——两个一起强制，别留下「新位置 + 旧窗口」的混合态
    detect = _detect(p, eps, on_msg=on_msg, force=True)
    if detect.get("pos"):
        cfg = dict(cfg, pos=detect["pos"], size=detect["size"],
                   fill=detect.get("fill") or "above",
                   newsub_pos=round(min(0.92, max(
                       0.50, detect["pos"] + detect["size"] / 2.0)), 4))
    band = detect if detect.get("pos") else {
        "pos": cfg["pos"], "size": cfg["size"], "fill": cfg["fill"]}
    by_ep, fails = _spans(p, eps, band, on_msg=on_msg, force=True)
    span_cache = {"key": key, "by_ep": by_ep}
    if fails:                      # C：失败集落盘，界面能显示「哪几集是固定位置抹的」
        span_cache["fail"] = fails
        if on_msg:
            on_msg(f"有 {len(fails)} 集没量到字幕区域，出片时会用固定位置抹除")
        print(f"[nosub] 字幕区域识别失败 {len(fails)} 集：{'；'.join(fails[:5])}",
              flush=True)
    st.update({"detect": detect, "spans": span_cache, "cfg": cfg})
    p["nosub"] = st
    core.save_merge(p, ["nosub"])
    return {"cfg": cfg, "detect": detect, "by_ep": by_ep, "fail": fails}


def run(pid: str, task_kind: str = "nosub", on_progress=None) -> None:
    """后台任务：逐集「去掉原字幕 + 在原位置加回整理好的台词」，产出独立视频。

    单集失败只记录、继续下一集。参数没变且文件还在的集直接跳过（续跑靠这个）。

    task_kind / on_progress：这个函数既独立跑（第 2 步「重做字幕」，注册在
    nosub 组），也会被别的任务当一段子步骤调用（一键全自动、追加视频，都跑在
    cuts 组）。内嵌时必须由调用方接管进度：
      · task_kind 换成调用方的 kind，进度才会落进它那一格；
      · 调用方还要把 0~100 映射到自己的刻度（全自动 ②/④ 是 32%~54%）。
    on_progress(pct, msg) 只要给了，本函数就完全不碰 core.set_task——连收尾也
    不碰，由调用方自己收尾。不给 on_progress 就退回独立跑的默认行为。
    """
    def _tick(pct: float, msg: str) -> None:
        # 内嵌调用时进度和收尾都归调用方：这里写 nosub 槽会让两条产线的进度
        # 分家，末尾 set_task(pid, None) 还会把调用方那一组清掉（2026-11-21）。
        if on_progress is not None:
            on_progress(min(100.0, max(0.0, float(pct))), msg)
        else:
            core.set_task(pid, task_kind, pct, msg)

    p = core.load(pid)
    if not p:
        return
    cfg = clean_cfg((p.get("nosub_cfg") or {}))
    episodes = list(p.get("episodes") or [])
    if not episodes:
        raise RuntimeError("这个项目没有导入视频")
    # 字幕是固定要加回去的：一集台词都没有就没法做，先去做台词整理
    if not any(e.get("lines") for e in episodes):
        raise RuntimeError("还没有整理台词，先去第 1 步「台词整理」做完再来")

    # 自动定位：机器先量出字幕带。量出来的值直接写回 cfg（下面 sig 用的就是它），
    # 所以「一次识别、一直稳定」，不会每点一次生成就换一组数字导致全量重做。
    detect = {}
    if cfg["auto"]:
        detect = _detect(p, episodes, on_msg=lambda m: _tick(0.5, m))
        if detect.get("pos"):
            if cfg["mode"] == "crop":
                # 裁掉：保留到「字的上沿再上一点」，切得比写死的 0.75 更贴
                cfg = dict(cfg, pos=round(max(0.50, detect["text_top"] - 0.012), 4))
            else:
                # 新字幕就烧在原来那行字幕的位置上：带中心 = 上沿 + 半高。
                # 这样用户完全不用管「新字幕高低」，也天然跟原片的观感一致。
                cfg = dict(cfg, pos=detect["pos"], size=detect["size"],
                           fill=detect.get("fill") or "above",
                           newsub_pos=round(min(0.92, max(
                               0.50, detect["pos"] + detect["size"] / 2.0)), 4))

    # 逐集量「字幕实际占的矩形」：只有柔和抹掉需要（裁掉是整帧变换，没有局部可留）。
    # 放在 sig 之前不要紧——它的结果由素材内容决定，不进参数指纹。
    spans_by_ep: dict[str, dict] = {}
    span_fails: list[str] = []
    if cfg["mode"] == "soft":
        band = detect if detect.get("pos") else {
            "pos": cfg["pos"], "size": cfg["size"], "fill": cfg["fill"]}
        spans_by_ep, span_fails = _spans(
            p, episodes, band, on_msg=lambda m: _tick(0.5, m))
        if span_fails:
            print(f"[nosub] 字幕区域识别失败 {len(span_fails)} 集："
                  f"{'；'.join(span_fails[:5])}", flush=True)

    # 字幕时间对齐已挪到第 1 步转写末尾（2026-11-19 用户定）——
    # 转写完紧接着按原片字幕对齐，台词一出来时间就是准的。这里只抹画面，不再动时间码。
    # spans_by_ep 照样要量（抹字幕要用），但它命中第 1 步存的缓存，不重复扫。

    # 副本只负责「把原字幕抹干净」（2026-11-16 用户定，见 REFERENCE「抹一次多套复用」）。
    # 指纹因此只算填充/位置/柔和度，**不算字幕样式和台词**——改了字幕动效或台词，
    # 副本的像素一模一样，没必要重编 31 集；出片时才按 burn_sig 现烧字幕。
    # 「|CV1」是副本格式版本：老副本是「抹+烧」一次成型的，前缀不同即判不匹配，
    # 整批强制重做，绝不会拿带字的旧副本去叠新字幕。
    sig = sig_of(cfg, episodes, style=False) + "|CV1"
    burn_sig = sig_of(cfg, episodes)
    dest = out_dir(pid)
    os.makedirs(dest, exist_ok=True)
    # 空间提前看一眼：整批副本约等于原片大小之和，编到一半写满最亏
    try:
        need_mb = sum(os.path.getsize(os.path.join(p["src_dir"], e["file"]))
                      for e in episodes
                      if os.path.isfile(os.path.join(p["src_dir"], e["file"]))) / (1024 * 1024)
        core.check_space(dest, max(64.0, need_mb * 1.15), "生成去字幕+字幕视频")
    except (OSError, TypeError):
        pass

    prev = p.get("nosub") or {}
    same = str(prev.get("sig") or "") == sig
    files = dict(prev.get("files") or {}) if same else {}
    errors = {} if same else {}
    total = len(episodes)
    # burned：这一批副本里是不是已经烧了字幕。**必须保守**——
    # 一开始就写 False 的话，跑到一半崩了/被中止，项目里就记着「底片是干净的」，
    # 而磁盘上还有一堆旧的带字副本，出片不会拦，结果叠出两层字。
    # 所以先沿用上一次的结论（第一次做 → 当作带字），全部做完才改成 False。
    span_cache_out = {"key": _detect_key(p, episodes), "by_ep": spans_by_ep}
    if span_fails:
        span_cache_out["fail"] = span_fails
    state = {"sig": sig, "burn_sig": burn_sig,
             "burned": bool(prev["burned"]) if "burned" in prev else True,
             "cfg_sig": sig_of(cfg), "cfg": cfg, "detect": detect,
             "spans": span_cache_out,
             "files": files, "errors": errors,
             "retime": bool(cfg.get("retime")),
             "total": total, "updated": ""}
    p["nosub"] = state
    core.save_merge(p, ["nosub"])

    def _pct(i: int, inner: float = 0.0) -> float:
        """第 i 集内部走到 inner%（0~100）时整批的百分比；前段留给对时间。"""
        return RETIME_PCT + (i + inner / 100.0) / max(1, total) * (100.0 - RETIME_PCT)

    # 旧版本会在 _ass 里攒逐集 ASS（那时副本要把台词烧进去）。现在副本不烧字了，
    # 顺手清掉这个残留目录；本机删除守卫按会话记账，所以一次性整目录删，不逐文件删。
    core.rmtree(os.path.join(dest, "_ass"))

    for i, ep in enumerate(episodes):
            fname = str(ep.get("file") or "")
            src = os.path.join(p["src_dir"], fname)
            dst = os.path.join(dest, _out_name(ep))
            _tick(_pct(i), f"第 {i + 1}/{total} 集：{fname}")
            # 参数没变且文件还在 = 上次已经做完，直接跳过（中断续跑就靠这个）
            if same and files.get(fname) and os.path.isfile(dst):
                continue
            if not os.path.isfile(src):
                errors[fname] = "找不到原始视频"
                core.save_merge(p, ["nosub"])
                continue
            try:
                info = core.ffprobe(src)
                dur = float(info.get("duration") or 0)
            except Exception:
                dur = float(ep.get("duration") or 0)
            try:
                sd_ep = spans_by_ep.get(fname)
                # 2026-11-16：副本只抹、不烧字（用户要求「抹完的产物多套复用」）。
                # 台词字幕改在出片时现场烧（render._live_cap），所以这里不再生成
                # ASS / drawtext；「每集按自己字幕带中心居中 + 按本集字高定字号」
                # 那套跟集走的逻辑一并搬进 _live_cap，多来源项目仍然每集各自对位。
                sub_tail = ""
                # 整集一句字幕都没量到 = 原片本来就没有字幕（AI 生成片很常见）。
                # 这时退回「整条替换」会在干净画面上凭空抹掉一条、补进镜像内容；
                # 宁可一个像素都不动。
                done = False
                if (cfg.get("fill_mode") == "borrow" and cfg["mode"] == "soft"
                        and sd_ep and sd_ep.get("spans")):
                    try:
                        from . import nosubfill
                        nosubfill.encode(
                            src, dst, cfg, sd_ep["spans"], sub_tail, dur,
                            lambda pct, i=i: _tick(
                                _pct(i, pct),
                                f"第 {i + 1}/{total} 集：借干净背景 {int(pct)}%"))
                        done = True
                    except Exception as exc:  # noqa: BLE001
                        # 借用背景失败（帧率异常、编码器不吃、带区太偏……）
                        # 当场回落到镜像，绝不让这一集因此失败
                        print(f"[nosub] {fname} 借用背景失败，回落镜像：{exc}",
                              flush=True)
                if not done:
                    if cfg["mode"] == "soft" and spans_by_ep and not sd_ep:
                        graph = "[0:v]format=yuv420p[vout]"
                    else:
                        graph = _graph(cfg, "", *_patch_span(cfg, sd_ep))
                    _encode(src, dst, graph, dur,
                            lambda pct, i=i, total=total: _tick(
                                _pct(i, pct),
                                f"第 {i + 1}/{total} 集：正在处理 {int(pct)}%"))
                files[fname] = os.path.basename(dst)
                errors.pop(fname, None)
            except Exception as exc:  # noqa: BLE001 —— 单集失败不能带走整批
                errors[fname] = (str(exc) or type(exc).__name__).strip()[:200]
            state["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            core.save_merge(p, ["nosub"])

    ok = len(files)
    # 全部集都做好了才宣布「底片已无字」；有失败/被中止就保持上一版结论，
    # 出片那边会继续按「带字」处理并提示重做，绝不会拿半新半旧的副本叠字。
    if ok >= total and not errors:
        state["burned"] = False
        core.save_merge(p, ["nosub"])
    _tick(100, f"完成 {ok}/{total} 集" + (f"，{len(errors)} 集失败" if errors else ""))
    # 显式收尾：清掉 task.json 的「中断续跑」标记。只靠 pct=100 不够——
    # 要等前端轮询 get_task() 才会清，用户关掉页面就留了个假中断标记。
    # 同组只有本任务在跑（cuts 槽互斥），不会误伤别的产线。
    # 内嵌调用（on_progress 由调用方接管）时绝不能清：清的会是调用方那一组，
    # 全自动刚跑到 ②/④ 就会被这里把 cuts 槽清空、task.json 续跑标记一起抹掉。
    if on_progress is None:
        core.set_task(pid, None)
