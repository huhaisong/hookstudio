"""选视频直接出片（2026-11-17）。

一个独立入口：用户选一个视频或一个文件夹里的视频，跳过「排剪辑表」，
按当前出片设置把每个视频各自做成一条成片——原字幕换成自己的台词。

实现方式：内部就是一个标准的 batch 项目，只是打了 `direct=True` 标记：
  · 每个视频当成一「集」，edl 只有一整段（0 → 时长），不需要 AI 排结构；
  · 台词走现有两条路：自动转写 / 粘贴带时间码文本（复用 main 的导入接口）；
  · 字幕用 src_ver=live ——出片这一趟现场抹原字 + 烧自己的台词，不依赖第 2 步底片；
  · 检测字幕区域 + 对时复用 nosub.ensure_spans / retime.apply_to；
  · 出片复用 batch.render_plans，单条失败隔离、进度、成片列表全都现成。

direct 项目不出现在左侧项目列表（core.list_projects 里过滤），避免和正常项目混在一起。
它被打开后按普通项目轮询即可（task / 进度字段都是现成的）。
"""
from __future__ import annotations

import os
from typing import Callable

from . import core, nosub, retime, transcribe, batch

# 视频后缀：和 main._videos_in 保持一致
VIDEO_EXT = (".mp4", ".mov", ".mkv")


def is_direct(p: dict | None) -> bool:
    return bool(p and p.get("direct"))


def _videos_in_folder(path: str) -> list[str]:
    try:
        return [f for f in os.listdir(path)
                if f.lower().endswith(VIDEO_EXT) and os.path.isfile(os.path.join(path, f))]
    except OSError:
        return []


def collect(paths: list[str]) -> tuple[str, list[dict]]:
    """把用户选的文件 / 文件夹展开成 episodes。

    paths 里每一项可以是单个视频文件，也可以是装着视频的文件夹。
    单个视频也统一放进一个 src_dir：它在自己的目录里，src_dir 取该目录，
    这样 transcribe / render 拼 `os.path.join(src_dir, file)` 都能找到文件。
    多个来源目录时以每个文件所在目录为准——core 的模型只有一个 src_dir，
    所以凡是跨目录的多选，先把文件复制进同一个直出素材目录再建项目。
    """
    files: list[tuple[str, str]] = []   # (文件名, 所在目录绝对路径)
    seen: set[str] = set()
    for raw in paths or []:
        path = os.path.abspath(str(raw or ""))
        if os.path.isfile(path):
            if path.lower().endswith(VIDEO_EXT):
                d, f = os.path.split(path)
                key = os.path.normcase(path)
                if key not in seen:
                    seen.add(key)
                    files.append((f, d))
        elif os.path.isdir(path):
            for f in sorted(_videos_in_folder(path), key=core.natural_key):
                key = os.path.normcase(os.path.join(path, f))
                if key not in seen:
                    seen.add(key)
                    files.append((f, path))
    if not files:
        raise ValueError("没有找到视频文件（支持 mp4 / mov / mkv）")

    dirs = {d for _, d in files}
    # 单目录：直接用原位置，不复制、不动用户素材
    if len(dirs) == 1:
        src_dir = next(iter(dirs))
        episodes = _probe_episodes(src_dir, [f for f, _ in files])
        return src_dir, episodes

    # 跨目录多选：复制到一个直出素材目录（core.PROJECTS 下，按时间戳），
    # 同名文件加序号避免互相覆盖。只复制、不移动、不删原文件。
    import shutil
    import time
    stamp = time.strftime("%Y%m%d%H%M%S")
    src_dir = os.path.join(core.PROJECTS, "_direct_src", stamp)
    os.makedirs(src_dir, exist_ok=True)
    names: list[str] = []
    used: set[str] = set()
    for f, d in files:
        name = f
        stem, ext = os.path.splitext(f)
        n = 2
        while name.lower() in used:
            name = f"{stem}_{n}{ext}"
            n += 1
        used.add(name.lower())
        shutil.copy2(os.path.join(d, f), os.path.join(src_dir, name))
        names.append(name)
    return src_dir, _probe_episodes(src_dir, names)


def _probe_episodes(src_dir: str, files: list[str]) -> list[dict]:
    eps: list[dict] = []
    for i, f in enumerate(files, 1):
        info = core.ffprobe(os.path.join(src_dir, f))
        eps.append({
            "index": i, "file": f, "duration": info["duration"],
            "width": info["width"], "height": info["height"],
            "status": "pending", "lines": [],
        })
    return eps


COPIES_MAX = 5


def copies_of(p: dict) -> int:
    """这个项目一个视频要出几份（1~5）。老项目没这个字段就是 1。"""
    try:
        n = int(p.get("copies") or 1)
    except (TypeError, ValueError):
        n = 1
    return max(1, min(COPIES_MAX, n))


def build_plans(p: dict) -> int:
    """为每个视频生成整片 plan（覆盖式，重选素材时先清旧的）。

    edl 一段 = 整集，out_name 固定成「第NN集.mp4」风格——重渲时同名覆盖，
    不会在成片目录里堆一堆带时间戳的重复文件。

    一个视频出几份（copies）时，每份是**独立的套编号**——出片的随机种子
    （镜像/调色/裁剪位置/指纹/字幕动效）全都按套编号算，编号不同才出得来
    真正不一样的几版；共用编号只会渲出几条一模一样的。
    多份时文件名加 _v2/_v3，各份互不覆盖。
    copies=1 沿用原来的编号（就是集号），老项目重开不会被判成「要重渲」。
    """
    copies = copies_of(p)
    plans = []
    for ep in p.get("episodes") or []:
        dur = float(ep.get("duration") or 0)
        stem = os.path.splitext(ep["file"])[0]
        if copies <= 1:
            plans.append({
                "index": ep["index"],
                "on": True,
                "edl": [{"ep": ep["index"], "start": 0.0, "end": dur, "rating": 10}],
                "out_name": f"{ep['index']:02d}_{stem}.mp4",
                "tag": f"第{ep['index']}集",
                "mode": "direct",
            })
            continue
        for v in range(1, copies + 1):
            plans.append({
                "index": int(ep["index"]) * 100 + v,
                "on": True,
                "edl": [{"ep": ep["index"], "start": 0.0, "end": dur, "rating": 10}],
                "out_name": f"{ep['index']:02d}_{stem}_v{v}.mp4",
                "tag": f"第{ep['index']}集·第{v}份",
                "mode": "direct",
            })
    p["plans"] = plans
    return len(plans)


def create(paths: list[str], name: str = "") -> dict:
    """建直出项目并自动开始转写。返回项目字典。"""
    src_dir, episodes = collect(paths)
    clean_name = (name or "").strip() or (
        os.path.basename(src_dir.rstrip("/\\")) or "选视频直接出片")
    p = core.create(src_dir, clean_name, 1, episodes, "batch")
    p["direct"] = True
    p["stage"] = "imported"
    n = build_plans(p)
    core.save_merge(p, ["direct", "stage", "plans"])
    # 和新建普通项目一致：自动开始转写，只补没台词的集。
    # 直出把"识别字幕并对齐时间"并进同一条链：转写完自动接着做，用户不用再点。
    if any(not (e.get("lines")) for e in p["episodes"]):
        core.start_task(p["id"], "transcribe",
                        lambda pid=p["id"]: transcribe_then_analyze(pid))
    return p


def reselect(pid: str, paths: list[str]) -> dict:
    """重选素材：换掉 episodes 和 plans，重置台词/检测/成片。用于面板里改选视频。"""
    p = core.load(pid)
    if not p or not is_direct(p):
        raise ValueError("直出项目不存在")
    src_dir, episodes = collect(paths)
    p["src_dir"] = src_dir
    p["win_dir"] = core.win_path(src_dir)
    p["episodes"] = episodes
    p["stage"] = "imported"
    for k in ("nosub", "nosub_cfg", "batch", "sets", "proof"):
        p.pop(k, None)
    p.pop("proof_sig", None)   # 换了素材，旧的校对清单全作废（集号和内容都可能变）
    build_plans(p)
    core.save(p)
    if any(not (e.get("lines")) for e in p["episodes"]):
        core.start_task(pid, "transcribe", lambda: transcribe_then_analyze(pid))
    return p


def remove_episode(pid: str, index: int) -> dict:
    """从直出项目移除一个视频（只动项目记录，磁盘上的原片和成片都不碰）。

    按集号精确清理四份关联数据：视频本身、出片计划、成片记录、逐句字幕区域。
    不重排余下的集号——成片记录和 EDL 都按 index 关联，重排会把已有成片错配到
    别的视频上；跳号反而一眼看得出移除过哪个。
    """
    p = core.load(pid)
    if not p or not is_direct(p):
        raise ValueError("直出项目不存在")
    eps = p.get("episodes") or []
    ep = next((e for e in eps if int(e.get("index") or 0) == int(index)), None)
    if not ep:
        raise ValueError("项目里没有这个视频")
    if core.task_running(pid, "cuts") or core.task_running(pid, "nosub"):
        raise RuntimeError("正在整理台词或出片，等它跑完再移除")
    if len(eps) <= 1:
        raise ValueError("至少留一个视频；要全换掉请用「重选」")

    fname = str(ep.get("file") or "")
    p["episodes"] = [e for e in eps if int(e.get("index") or 0) != int(index)]
    p["plans"] = [
        pl for pl in (p.get("plans") or [])
        if int(pl.get("index") or 0) != int(index)
        and not any(int((x or {}).get("ep") or 0) == int(index)
                    for x in (pl.get("edl") or []))
    ]
    # 成片记录也按 EDL 里的集号清：一个视频出多份时，套编号是「集号*100+份号」，
    # 只比 index 会漏掉其余几份，成片列表里留下指向已删视频的条目。
    p["sets"] = [s for s in (p.get("sets") or [])
                 if int(s.get("index") or 0) != int(index)
                 and not any(int((x or {}).get("ep") or 0) == int(index)
                             for x in (s.get("edl") or []))]
    if p.get("batch"):
        p["batch"]["done"] = len(p["sets"])

    ns = p.get("nosub") or {}
    sp = ns.get("spans") or {}
    if fname:
        (sp.get("by_ep") or {}).pop(fname, None)
        if isinstance(sp.get("fail"), list):
            sp["fail"] = [x for x in sp["fail"] if fname not in str(x)]
    if sp:
        # 余下各集的包围盒没变，只更新尺子指纹，避免移除后把全部集重扫一遍。
        sp["key"] = nosub._detect_key(p, p["episodes"])
        ns["spans"] = sp
        p["nosub"] = ns
    # 校对清单也按集清：直出是每个视频一份，删掉的那个不能留在里面。
    pv = p.get("proof") or {}
    if pv.get("mode") == "per_ep":
        (pv.get("by_ep") or {}).pop(str(index), None)
        p["proof"] = pv
    core.save_merge(p, ["episodes", "plans", "sets", "batch", "nosub", "proof"])
    return p


def _analyze_body(pid: str) -> None:
    """识别字幕 + 对时的实际动作，不碰 start_task / task 收尾。

    抽出来是为了让「转写完自动对齐」能在同一个 cuts 组任务里串行接着跑——
    analyze_direct 和 transcribe 同属 cuts 组，另起 start_task 会抢锁失败。
    手动 analyze 和自动链共用这一份，进度都写到 analyze_direct 槽位。
    """
    p = core.load(pid)
    if not p or not is_direct(p):
        return
    if not any(e.get("lines") for e in (p.get("episodes") or [])):
        raise RuntimeError("还没有台词，请先整理台词或粘贴台词再识别字幕")

    def msg(m):
        core.set_task(pid, "analyze_direct", 0, m)

    got = nosub.ensure_spans(pid, on_msg=msg)
    by_ep = got.get("by_ep") or {}
    p = core.load(pid) or p
    if by_ep:
        stat = retime.apply_to(
            p, by_ep,
            on_msg=lambda i, n, ep: core.set_task(
                pid, "analyze_direct", i / max(1, n) * 60,
                f"对齐第 {ep['index']} 集字幕时间（{i + 1}/{n}）"))
        from . import span_split
        sst = span_split.apply_to(p, by_ep)
        if stat.get("fixed") or stat.get("skipped") or sst.get("cut"):
            core.save_merge(p, ["episodes"])
    core.set_task(pid, None)


def analyze(pid: str) -> None:
    """手动入口：识别字幕并对齐时间（后台任务，由 start_task 包着调）。"""
    _analyze_body(pid)


def transcribe_then_analyze(pid: str) -> None:
    """选完视频后的自动链：转写 + 对时 + 切行 + AI 校对 + 自动写回，一次点击都不用。

    2026-11-19：transcribe.run 末尾已经串行做了对时 → 按原片字幕屏切行 →
    AI 校对 → 自动写回（_retime_after_transcribe / _proof_after_transcribe），
    直出项目走的是逐视频校对（per_ep）。所以这里不再重复调 _analyze_body——
    那段是早期 transcribe.run 还没收尾步骤时留下的冗余，重复跑一遍对时+切行。
    transcribe.run 结尾已 set_task(None)，本函数不必再收尾。
    手动「识别字幕并对齐时间」入口 analyze() 仍保留 _analyze_body，不受影响。
    """
    transcribe.run(pid)


def ready_to_render(pid: str) -> tuple[dict, dict]:
    """出片前的接口层校验：有台词、有 spans、项目在跑分析/转写就拦。返回 (p, fx)。"""
    p = core.load(pid)
    if not p or not is_direct(p):
        raise ValueError("直出项目不存在")
    if core.task_running(pid, "cuts") or core.task_running(pid, "nosub"):
        raise RuntimeError("还在整理台词或识别字幕，等它跑完再出片")
    eps = p.get("episodes") or []
    if not any(e.get("lines") for e in eps):
        raise RuntimeError("还没有台词，请先整理台词或粘贴台词")
    # 只要求「识别这一步跑过」：ensure_spans 会把 spans.key 写上，哪怕所有集都
    # 没量出包围盒（by_ep 为空也不拦——live 渲染对没包围盒的集会整条替换兜底）。
    # 若硬性要求 by_ep 非空，检测失败的片子就永远出不了片，不符合单集隔离原则。
    ns = p.get("nosub") or {}
    if not (ns.get("spans") or {}).get("key"):
        raise RuntimeError("还没识别字幕位置，先点「识别字幕并对齐时间」")
    plans = [pl for pl in (p.get("plans") or []) if pl.get("on", True)]
    if not plans:
        raise RuntimeError("没有要出片的视频")
    # 直出固定 live：无论面板里选了「原始 / 干净底片」都一样——
    # 用户要的就是换字幕，原始视频带原字、干净底片这里又不存在。
    # 默认值优先用直出面板自己存的那份（fx_default_direct，2026-11-18）：直出多一套
    # 份数/配乐设置，而且「素材版本」被强制 live，跟 AI 编辑出片共用一个键会互相串味。
    _b = p.get("batch") or {}
    fx = dict(_b.get("fx_default_direct") or _b.get("fx_default") or {})
    # 「去掉原片自带音乐」在直出里默认开（2026-11-18 用户定）：直出的素材基本都是从
    # 别处导出的、已经配过乐的成片，不去掉就会和自己选的配乐一起响。项目里显式存过
    # false 才关；前端面板总会显式传，这里兜住直接调接口的情况。
    if "voice_iso" not in fx:
        fx["voice_iso"] = True
    fx["src_ver"] = "live"
    return p, fx
