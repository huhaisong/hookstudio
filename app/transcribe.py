"""转写：faster-whisper 本地跑，输出逐句时间码。

模型固定 small（随包只带这一个，见 core.TIER）；换成别的模型只需改 core.TIER 并放好 models/<tier>。
音频解码用随包的 ffmpeg（不走 PyAV）——PyAV 各版本参数变动大，ffmpeg 稳定且我们本来就要带。
"""
from __future__ import annotations

import os
import threading
import time

from . import core, zh

_MODEL = None
_MODEL_KEY: tuple | None = None
_LOAD_LOCK = threading.Lock()
# ctranslate2 / faster-whisper 模型不是线程安全的：两个线程同时推理会输出空段甚至崩溃
# （表现为「转写 done 却 0 句」「IndexError: list index out of range」）。
# 转写是 CPU 密集任务，本就该串行，这里用全局锁强制同一时刻只有一个推理在跑。
_INFER_LOCK = threading.Lock()
SR = 16000

# 词级时间戳里，相邻两个字/词之间超过这个秒数的空白 = 中间隔着一段真静音，
# 应当拆成两句。Whisper segment 常把「说完—停顿几秒—再说」并成一条，VAD 只按
# 400ms 切静音、切不开这种长停顿，所以用词级间隔兜底强制断句。
GAP_SPLIT_SEC = 1.6
# 一条识别结果超过这么多字，就不是正常的一句了：whisper 把几十秒对白压成了一句
# （第 10 集实测 159 字 / 13.9 秒 = 11.4 字/秒，人说不出来）。正常一句最长 15 字。
# 命中就整集去掉 initial_prompt 重听（见 transcribe_episode）。
_COLLAPSE_CHARS = 30


def _worst_chars(lines: list[dict]) -> int:
    return max((len(str(l.get("text") or "")) for l in (lines or [])), default=0)


def _split_on_gap(segments, on_segment=None) -> tuple[list[dict], dict]:
    """把带词级时间戳的 segment 按句内长静音拆开，返回 (lines, 统计)。

    只在确实拿到词级时间戳时才拆；没词级数据（word_timestamps 失效）就原样输出
    seg.text，绝不丢字。切点落在词与词之间，中文按字给词，拆出来仍是完整句子。

    on_segment：每拿到一个 segment 调一次。whisper 的 segments 是惰性生成器，
    整集推理就发生在迭代过程里；这是唯一能给长推理插入中止检查的地方
    （B 档 2026-11-19）：用户点中止后不用等整集跑完，产出下一个 segment 就退。
    """
    lines: list[dict] = []
    stat = {"segments": 0, "split": 0, "gaps": 0, "no_words": 0}
    for seg in segments:
        if on_segment:
            on_segment()
        stat["segments"] += 1
        text = (seg.text or "").strip()
        s0, e0 = float(seg.start), float(seg.end)
        words = [w for w in (getattr(seg, "words", None) or [])
                 if getattr(w, "start", None) is not None
                 and getattr(w, "end", None) is not None]
        if len(words) < 2 or not text:
            stat["no_words"] += 1
            if text:
                lines.append({"start": round(s0, 3), "end": round(e0, 3), "text": text})
            continue
        # 按相邻词之间的空白切组：第一组词从 seg 起点起，遇到长空白就收一组。
        groups = []
        cur = [words[0]]
        n_gap = 0
        for prev, w in zip(words, words[1:]):
            if float(w.start) - float(prev.end) > GAP_SPLIT_SEC:
                groups.append(cur)
                cur = [w]
                n_gap += 1
            else:
                cur.append(w)
        groups.append(cur)
        if not n_gap:
            lines.append({"start": round(s0, 3), "end": round(e0, 3), "text": text})
            continue
        # 校验：拆出来的片段拼回去（忽略空白）必须和原 seg.text 一致，否则说明
        # 词级时间戳把标点/字漏在词外了——这种宁可不拆，退回整条，绝不丢字。
        joined = "".join("".join((w.word or "") for w in grp).strip() for grp in groups)
        norm = lambda s: "".join(s.split())
        if norm(joined) != norm(text):
            stat["no_words"] += 1
            lines.append({"start": round(s0, 3), "end": round(e0, 3), "text": text})
            continue
        stat["split"] += 1
        stat["gaps"] += n_gap
        for grp in groups:
            part = "".join((w.word or "") for w in grp).strip()
            if not part:
                continue
            lines.append({
                "start": round(float(grp[0].start), 3),
                "end": round(float(grp[-1].end), 3),
                "text": part,
            })
    return lines, stat


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
        raise RuntimeError("音频读取失败，请重试")
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


def _denoise_audio(path: str, on_stage=None) -> str:
    """开关开着且分离模型可用时，先去掉原片背景音，返回人声 wav 路径。

    原样返回 path 的两种情况：开关没开、模型缺失——都让后续 _decode 直接读原片。
    分离结果按音频内容哈希缓存（voiceiso.separate 内部），复跑不重复算。
    """
    if not core.load_config().get("asr_denoise"):
        return path
    from . import voiceiso
    if not voiceiso.available():
        return path  # 模型没带就静默降级，绝不因为增强项让转写失败
    core.check_cancel()
    if on_stage:
        on_stage("原片背景音较重，先把人声分离出来")
    tmp_wav = os.path.join(core.DATA_DIR, "_asr_denoise_%d.wav" % threading.get_ident())
    try:
        voiceiso.extract_audio(path, tmp_wav)
        core.check_cancel()
        clean = voiceiso.separate(
            tmp_wav,
            on_progress=lambda f: (core.check_cancel(),
                                  on_stage(f"正在分离人声 {int(f * 100)}%") if on_stage else None),
        )
        core.check_cancel()
        return clean  # 缓存目录下的 wav，不删（voiceiso 自己管缓存）
    finally:
        try:
            if os.path.isfile(tmp_wav):
                os.remove(tmp_wav)
        except OSError:
            pass


def transcribe_episode(path: str, on_progress=None, on_stage=None) -> list[dict]:
    """on_progress(0~1)：按句增量回调，让前端进度条能平滑地走，而不是每集跳一格。"""
    model = _model()
    audio = _decode(_denoise_audio(path, on_stage))
    total_sec = len(audio) / SR if SR else 0.0
    use_zh = zh.enabled()
    # 人名写法统一已交给第 1 步末尾的「整剧 AI 校对」（2026-11-19 用户定）：
    # 没有标准来源时对照表填不出来，改由 AI 通读全剧归并、取出现最多的写法为准。
    # 所以这里不再喂人名提示，转完也不再套对照表，避免两套口径打架。
    prompt = zh.prompt() if use_zh else None
    with _INFER_LOCK:  # 见 _INFER_LOCK 注释：推理必须全局串行
        # 参数取舍（2026-11-19 晚修正，实测见下）：
        #  · condition_on_previous_text=False：默认 True 会把上一句当成下一句的开头去续写，
        #    「说完—停几秒—再说」经常被粘成一条。关掉是断句问题的主因修复。
        #  · word_timestamps=True：拿到字级时间戳，才能在长静音处把合并的句子拆开（见
        #    _split_on_gap）。retime 那一步本来也跑词级对齐，这里同一趟 generate 顺带出，
        #    不是重新推理一遍音频。
        #  · **vad_filter=False（今天从 True 翻过来）**：VAD 会把整段台词当静音丢掉。
        #    第 7 集实测：开 VAD 只转出 18 句 / 158 字，原片字幕有 67 秒、其中 25.8 秒
        #    台词完全不出现；关掉 VAD 变成 35 句 / 235 字、漏的只剩 7.5 秒（多出来的
        #    0 秒幻听：没有任何一句落在原片没字幕的区间）。VAD 的 threshold 怎么调都没用，
        #    它丢的是「带背景音的人声」整段。代价是慢一些（79 秒音频 10s→16s）。
        #  · no_speech_threshold=0.9：默认 0.6，背景音重时 no_speech_prob 轻易过 0.6，
        #    Whisper 直接把整段有台词的话当静音跳过（「有背景音就识别不出台词」的元凶）。
        #    提到 0.9 让它只在几乎确定没人说话时才跳；配合 log_prob 兜底不会满屏幻听。
        #  · initial_prompt：能压住繁体、补上标点，但**它会被模型当成「上文」接着写**，
        #    偶尔把几十秒的对白压成一句（第 10 集实测：带提示词 7 句 / 最长 159 字，
        #    不带 21 句 / 最长 10 字）。所以先用带提示词的，一旦出现「长到不可能」的句子
        #    就整集去掉提示词重跑一遍（见下面的 _COLLAPSE_CHARS）。
        def _infer(ip):
            segments, _info = model.transcribe(
                audio, language="zh", beam_size=5,
                vad_filter=False,
                condition_on_previous_text=False,
                no_speech_threshold=0.9,
                word_timestamps=True,
                initial_prompt=ip,
            )
            # segments 是生成器，必须在锁内、迭代中读完（ctranslate2 结果是 lazy 的）。
            # 每产出一个 segment 查一次中止：长音频推理几分钟，用户点中止后不必干等。
            return _split_on_gap(segments, on_segment=core.check_cancel)

        lines, split_stat = _infer(prompt)
        worst = _worst_chars(lines)
        if prompt and worst > _COLLAPSE_CHARS:
            print(f"[transcribe] 出现 {worst} 字的长句（像是把整段压成一句），"
                  f"去掉提示词重听这一集：{os.path.basename(path)}", flush=True)
            if on_stage:
                on_stage("这一集听出来有点乱，换个方式再听一遍")
            alt, alt_stat = _infer(None)
            if _worst_chars(alt) < worst:
                lines, split_stat = alt, alt_stat
                print(f"[transcribe] 改听完成：最长句 {_worst_chars(lines)} 字，"
                      f"{len(lines)} 句：{os.path.basename(path)}", flush=True)
        # 拆句后进度按最后一句的终点算，避免拆句导致进度条回退
        if on_progress and total_sec > 0 and lines:
            on_progress(min(1.0, float(lines[-1]["end"]) / total_sec), len(lines))
    # 只做繁转简（确定性规则，零成本）；人名交给第 1 步末尾的整剧 AI 校对。
    n_zh = zh.apply_lines(lines) if use_zh else 0
    if n_zh:
        print(f"[transcribe] 繁体转简体 {n_zh} 字：{os.path.basename(path)}", flush=True)
    if split_stat["split"]:
        print(f"[transcribe] 长静音拆句 {split_stat['split']} 段 → "
              f"多出 {split_stat['gaps']} 句：{os.path.basename(path)}", flush=True)
    # 再切一道：连说不停的长句（静音拆不开的那种）按一屏 15 字切开，
    # 和烧字幕用同一个算法（见 app/reseg.py）。不让数据里出现「一句一小作文」。
    from . import reseg
    lines, _, st = reseg.split_lines(lines)
    if st["split"]:
        print(f"[transcribe] 长句按一屏拆开 {st['split']} 句 → "
              f"多出 {st['added']} 行：{os.path.basename(path)}", flush=True)
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


def run(pid: str, force: bool = False) -> None:
    """整理台词。force=True 时不管哪几集已经有台词，**全部**重新识别一遍。

    「全部重来」是用户 2026-11-19 要的：整剧重转不能一集一集点，而且重转必须把
    繁转简、对时、校对一起走完（都在本函数末尾/transcribe_episode 里自动串好）。

    force 下每集保存时要清掉 lines_time_bak / lines_time_sig：新台词句数恰好和旧的
    一样时，retime 会按「已经用同一把尺子对过」直接跳过，用户会以为对时没做。
    """
    p = core.load(pid)
    if not p:
        return
    eps = p["episodes"]
    total = len(eps)
    core.set_task(pid, "transcribe", 0,
                  f"从头逐集听音整理，共 {total} 集（会覆盖现有台词）" if force
                  else f"接着听音整理没做好的集，共 {total} 集")
    for i, ep in enumerate(eps):
        if ep.get("lines") and not force:
            continue
        base = i / total * 100
        step = 100 / total
        head = f"听音转写第 {ep['index']} 集（{i + 1}/{total}）"
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

        def stage(msg):
            core.set_task(pid, "transcribe", base, f"{head} · {msg}")

        core.set_task(pid, "transcribe", base, f"{head} · 读取音频中")
        src = os.path.join(p["src_dir"], ep["file"])
        t0 = time.time()
        try:
            lines = transcribe_episode(src, prog, stage)
            patch = {"lines": lines, "status": "done", "error": ""}
            if force:
                # 整集台词换了：旧对时备份与尺子指纹作废，末尾对时必须重算
                patch["lines_time_bak"] = None
                patch["lines_time_sig"] = None
            p = _merge_save(pid, ep["index"], **patch)
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
    # 第 1 步收尾（2026-11-19 用户定）：转写完紧接着对时 → 整剧 AI 校对 → 自动写回。
    # 对时扫的 spans 按素材指纹缓存，第 2 步抹字幕直接命中，不重复扫。
    # 校对里「人名统一＋全部文字修正（错字/乱码/漏字/断句）」都自动写回
    # （2026-11-19 用户定：默认自动改，不再逐条审核）；只有「删行」留给人勾。
    # 任何一步失败只记日志，不推翻已转好的台词。
    _retime_after_transcribe(pid)
    _proof_after_transcribe(pid)
    core.set_task(pid, None)


def _proof_after_transcribe(pid: str) -> None:
    """转写+对时完成后 AI 校对，出清单并把机械项直接写回。

    主项目（一整部剧）整剧一次；直出项目（选视频直接出片）里每个视频互相独立，
    **各调一次模型**——合并会把不同视频里的同名角色当成同一人改坏（见 app/proof.py）。
    清单带台词指纹，台词没变重复调不重复花钱。失败只记日志：校对是增强项，
    绝不能让它把已经转写好的台词弄丢。

    自动写回的范围（用户 2026-11-19 定，第二次扩张）：人名统一 + 全部文字修正
    （typo 同音错字、fix 乱码/漏字多字/断句语病）；只有「只有杂音的行」仍留在清单里等人勾。
    """
    from . import proof
    try:
        p = core.load(pid) or {}
        n_ep = len([e for e in (p.get("episodes") or []) if e.get("lines")])
        direct = bool(p.get("direct"))
        core.set_task(pid, "transcribe", 99,
                      (f"{n_ep} 个视频台词已转写完，正在最后一步 AI 校对"
                       if direct else f"{n_ep}/{n_ep} 集台词已转写完，正在最后一步 AI 校对（约 3~18 分钟，不能停）"))
        r = proof.suggest(pid)
        if r.get("mode") == "per_ep":
            print(f"[transcribe] 逐视频校对完成：{r.get('episodes')} 个，"
                  f"人名 {r.get('name_total')} 组、错字 {r.get('typo_total')} 条"
                  f"{'（全部命中缓存）' if r.get('cached') else ''}", flush=True)
            for e in (r.get("errors") or []):
                print(f"[transcribe] 校对失败 {e}", flush=True)
        else:
            print(f"[transcribe] 整剧校对完成：人名 {len(r.get('names') or [])} 组，"
                  f"错字 {len(r.get('typos') or [])} 条"
                  f"{'（命中缓存）' if r.get('cached') else ''}", flush=True)
        _apply_proof_auto(pid)
    except Exception as exc:  # noqa: BLE001
        print(f"[transcribe] 校对未完成（不影响台词）：{exc}", flush=True)


def _apply_proof_auto(pid: str) -> None:
    """把校对清单里「人名统一 + 全部文字修正」直接写回，只剩删行等人工确认。

    只打日志、不再改任务文案：run() 紧接着就把任务标成结束，写了也看不见；
    真正的结果由前端的「校对台词」面板按清单里的 auto 统计说明。
    """
    from . import proof
    try:
        core.set_task(pid, "transcribe", 99, "自动统一人名、修正错字和病句")
        r = proof.auto_apply(pid)
        print(f"[transcribe] 校对自动写回：人名 {r.get('names') or 0} 组、"
              f"错字与病句 {r.get('typos') or 0} 处，还剩 {r.get('left') or 0} 条要人工确认",
              flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[transcribe] 自动写回未完成（清单还在，可手动应用）：{exc}", flush=True)


def _retime_after_transcribe(pid: str) -> None:
    """转写全部完成后紧接着对时。对时失败只记日志，不推翻已经转好的台词。

    这里延迟导入 nosub/retime——它们都反向依赖本模块（retime 用词级对齐），
    顶层 import 会成环。扫描字幕位置用的 spans 缓存、词级对齐缓存都按素材/算法，
    第 2 步抹字幕、直出 live 渲染会命中同一份，不额外花扫描钱。
    """
    from . import nosub, retime
    p = core.load(pid) or {}
    if not any(e.get("lines") for e in (p.get("episodes") or [])):
        return
    try:
        got = nosub.ensure_spans(
            pid, on_msg=lambda m: core.set_task(pid, "transcribe", 96, m))
        by_ep = got.get("by_ep") or {}
        if not by_ep:
            return
        p = core.load(pid) or p
        stat = retime.apply_to(
            p, by_ep,
            on_msg=lambda i, n, ep: core.set_task(
                pid, "transcribe", 96 + i / max(1, n) * 3,
                f"对齐第 {ep['index']} 集字幕时间（{i + 1}/{n}）"))
        # 对时之后按「原片字幕的一屏」切行：时间已吸附到屏，切出来的边界最准。
        # 独立于 retime 的「已对过跳过」——旧项目就算被跳过也照样切。
        from . import span_split
        sst = span_split.apply_to(p, by_ep)
        if stat.get("fixed") or stat.get("skipped") or sst.get("cut"):
            core.save_merge(p, ["episodes"])
        print(f"[transcribe] 对时完成：{stat['fixed']} 集对齐，"
              f"{stat['skipped']} 集跳过，{len(stat['errors'])} 集失败"
              f"；按字幕屏切行 {sst['cut']} 行 → 多 {sst['added']} 行", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[transcribe] 对时未完成（不影响台词）：{exc}", flush=True)
