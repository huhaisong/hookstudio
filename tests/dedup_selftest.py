"""去重改动自测（2026-11-17）：段级定位 / 复用台账 / 帧数预算 / 逐段微变速。

用法（Windows，本机 runtime 的 numpy 还在，只是 venv 的解释器丢了）：
    $env:PYTHONPATH = "<repo>\\hookstudio\\runtime\\Lib\\site-packages"
    python hookstudio\\tests\\dedup_selftest.py

不连网、不改项目数据：合成的指纹数组测算法，ffmpeg 生成几秒测试片测真实链路。
退出码 0 = 全过；1 = 有失败（失败项打明细）。
"""
import base64
import json
import os
import struct
import subprocess
import sys

try:
    import numpy as np
except ImportError:  # pragma: no cover
    print("缺 numpy：把 hookstudio/runtime/Lib/site-packages 加进 PYTHONPATH 再跑")
    raise SystemExit(2)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:  # 控制台默认 GBK，✓/✗ 会直接抛 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from app import analyze, audiofp, core, render, reuse  # noqa: E402

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append((name, detail))
    print(("✓ " if cond else "✗ ") + name + ((" · " + detail) if detail else ""))


def pack(arr, stride=1):
    """按 v2 格式打包（和 audiofp.compute 同构）。"""
    a = np.asarray(arr, dtype="<u4")
    return base64.b64encode(audiofp._MAGIC + struct.pack("<I", int(stride))
                            + a.tobytes()).decode()


def probe(path, fields):
    cmd = [core.ff("ffprobe"), "-v", "error", "-select_streams", "v:0",
           "-count_frames", "-show_entries", fields,
           "-of", "default=noprint_wrappers=1:nokey=1", path]
    out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         timeout=120).stdout.decode("utf-8", "replace")
    return [x for x in out.split() if x]


def probe_dur(path, stream=None):
    sel = ["-select_streams", stream] if stream else []
    entries = "stream=duration" if stream else "format=duration"
    cmd = [core.ff("ffprobe"), "-v", "error", *sel,
           "-show_entries", entries,
           "-of", "default=noprint_wrappers=1:nokey=1", path]
    out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         timeout=120).stdout.decode("utf-8", "replace").strip()
    try:
        return float(out.split()[0])
    except (ValueError, IndexError):
        return 0.0


# ── 1. 指纹 v2 编解码（含旧格式兼容）────────────────────────────────────
arr = np.arange(1, 65, dtype="<u4")
got, st = audiofp._decode(pack(arr, 3))
check("v2 指纹带 stride 且内容一致", st == 3 and got.size == 64 and int(got[5]) == 6,
      f"stride={st} n={got.size}")
legacy, st0 = audiofp._decode(base64.b64encode(arr.tobytes()).decode())
check("旧指纹（无头）按 stride=1 读", st0 == 1 and legacy.size == 64)

# ── 2. 段级定位：同源片段被拼进另一条片 ─────────────────────────────────
rng = np.random.default_rng(7)
base = rng.integers(0, 2 ** 32, 600, dtype=np.uint64).astype("<u4")   # 60s
noise = rng.integers(0, 2 ** 32, 400, dtype=np.uint64).astype("<u4")
b = np.concatenate([noise[:40], base[100:400], noise[40:80]])        # 4s + 30s + 4s
loc = audiofp.locate(pack(base), pack(b))
a_span = loc["spans"][0]["a"] if loc["spans"] else [0, 0]
check("定位：命中 30s 复用段", 0.70 <= loc["ratio"] <= 0.95,
      f"ratio={loc['ratio']} overlap={loc['overlapSeconds']}s")
check("定位：修边后区间落在 10–40s",
      abs(a_span[0] - 10.0) <= 1.0 and abs(a_span[1] - 40.0) <= 1.0,
      f"a={a_span}")

other = rng.integers(0, 2 ** 32, 600, dtype=np.uint64).astype("<u4")
loc0 = audiofp.locate(pack(base), pack(other))
check("定位：无关内容 ratio=0", loc0["ratio"] == 0.0 and not loc0["spans"],
      f"ratio={loc0['ratio']}")

# 长片抽帧（stride=3）：复用起点对齐到步长整数倍时，时间轴换算必须正确
b3 = np.concatenate([noise[:20], base[300:600:3]])
loc3 = audiofp.locate(pack(base[::3], 3), pack(b3, 3))
check("定位：stride=3 时间轴换算正确",
      bool(loc3["spans"]) and abs(loc3["spans"][0]["a"][0] - 30.0) <= 1.0,
      f"spans={loc3['spans'][:1]}")

# ── 3. 整片相似度仍然可用 ────────────────────────────────────────────────
check("相似度：同一条=1.0", audiofp.similarity(pack(base), pack(base)) == 1.0)
check("相似度：无关 <0.6", audiofp.similarity(pack(base), pack(other)) < 0.6)

# ── 4. 逐段微变速：帧数守恒 ──────────────────────────────────────────────
durs = [20.0] * 12 + [5.5] * 3
bases = [1.0] * 12 + [1.15] * 3
plan = render.speed_plan(durs, bases, 30, "seed-a", "std")
base_total = sum(d / b for d, b in zip(durs, bases))
out_total = sum(p["out_dur"] for p in plan)
check("变速：段数一致、帧数≥2", len(plan) == 15 and all(p["frames"] >= 2 for p in plan))
check("变速：总时长守恒（误差 < 1 帧）", abs(out_total - base_total) <= 1.0 / 30 + 1e-6,
      f"{base_total:.3f} vs {out_total:.3f}")
check("变速：总帧数取整后严格相等",
      sum(p["frames"] for p in plan) == round(base_total * 30))
speeds = [p["speed"] for p in plan]
check("变速：速度确实拉开了", len(set(speeds)) >= 5 and max(speeds) - min(speeds) > 0.004,
      f"min={min(speeds):.4f} max={max(speeds):.4f} distinct={len(set(speeds))}")
check("变速：速度不越界（基准 ±3%）",
      all(abs(s / b - 1) <= 0.03 for s, b in zip(speeds, bases)))
plan2 = render.speed_plan(durs, bases, 30, "seed-a", "std")
check("变速：同种子可复现", [p["frames"] for p in plan] == [p["frames"] for p in plan2])
plan3 = render.speed_plan(durs, bases, 30, "seed-b", "std")
check("变速：换种子会变", [p["frames"] for p in plan] != [p["frames"] for p in plan3])
check("变速：关档（空输入）不炸", render.speed_plan([], None) == [])

# ── 5. 复用台账 ──────────────────────────────────────────────────────────
seg = {"ep": 3, "start": 12.34, "end": 30.0, "rating": 8}
check("台账：键按 0.1s 取整", reuse.key_of(seg) == "3:12.3-30.0", reuse.key_of(seg))
check("台账：无集号返回空键", reuse.key_of({"start": 1, "end": 2}) == "")
data = reuse.load({"reuse": {"sets": {
    "0:1": ["3:12.3-30.0", "2:0.0-9.0"],
    "0:2": ["3:12.3-30.0"],
}}})
check("台账：次数由 sets 现算", data["keys"].get("3:12.3-30.0") == 2)
check("台账：权重随次数递减",
      reuse.weight(0) == 1.0 and reuse.weight(3) < 0.4 < reuse.weight(1))
check("台账：满 3 次算用满", reuse.is_capped(3) and not reuse.is_capped(2))
shifted = {"ep": 3, "start": 12.9, "end": 30.4}      # AI 重排后边界挪了 0.6s
check("台账：边界挪动后仍能匹配（重叠过半）",
      reuse.count(data["keys"], shifted) == 2,
      f"count={reuse.count(data['keys'], shifted)}")
far = {"ep": 3, "start": 44.0, "end": 60.0}
check("台账：同集不同段不误配", reuse.count(data["keys"], far) == 0)

saved = {}
_orig_load, _orig_merge = reuse.core.load, reuse.core.save_merge
reuse.core.load = lambda pid: {"id": pid, "reuse": {}}
reuse.core.save_merge = lambda p, keys: saved.update(p)
try:
    rec = reuse.record({"id": "t1"}, [("1:1", [seg, seg])])
    check("台账：同一套里重复的块只记 1 次", rec["keys"].get("3:12.3-30.0") == 1)
    rec = reuse.record({"id": "t1"}, [("1:1", [seg])])
    check("台账：同一套重渲覆盖不累加", rec["keys"].get("3:12.3-30.0") == 1)
finally:
    reuse.core.load, reuse.core.save_merge = _orig_load, _orig_merge

# ── 6. 选段时避开用满的块 ───────────────────────────────────────────────
fresh = [{"ep": 1, "start": i * 40.0, "end": i * 40.0 + 20.0,
          "rating": 8, "blk": f"a{i}", "label": "爆点"} for i in range(4)]
worn = {"ep": 2, "start": 0.0, "end": 20.0, "rating": 9, "blk": "old", "label": "爆点"}
usage = {reuse.key_of(worn): reuse.MAX_USE}
picked = analyze._fit_target({}, fresh + [worn], 60.0, 10.0, usage=usage)
check("选段：用满的块被跳过（即使分最高）",
      bool(picked) and all(reuse.key_of(s) != reuse.key_of(worn) for s in picked),
      f"picked={[reuse.key_of(s) for s in picked]}")
picked_none = analyze._fit_target({}, [worn], 60.0, 10.0, usage=usage)
check("选段：只剩用满的块时仍会用它（宁重复不交白卷）",
      bool(picked_none) and reuse.key_of(picked_none[0]) == reuse.key_of(worn))

# ── 7. 质感逐段微抖 ──────────────────────────────────────────────────────
g0 = {"glow": 0.0, "grain": 0.10, "vignette": 0.08, "stretch": 1.0}
g1 = render.fp_grade(dict(g0), {"d_stretch": 0.0}, seg_index=3, seed="s")
g2 = render.fp_grade(dict(g0), {"d_stretch": 0.0}, seg_index=3, seed="s")
g3 = render.fp_grade(dict(g0), {"d_stretch": 0.0}, seg_index=4, seed="s")
check("质感：关掉的项不抖", g1["glow"] == 0.0 and g1["stretch"] == 1.0)
check("质感：抖动幅度 ≤10%", abs(g1["grain"] / 0.10 - 1) <= 0.10 + 1e-9,
      f"grain={g1['grain']:.4f}")
check("质感：同段可复现、异段不同",
      g1["grain"] == g2["grain"] and g1["grain"] != g3["grain"])

# ── 8. 真实 ffmpeg 链路：指纹 + 帧数预算 ─────────────────────────────────
tmp = os.path.join(ROOT, "logs", "_dedup_selftest")
os.makedirs(tmp, exist_ok=True)
src = os.path.join(tmp, "src.mp4")
# 测试音频用扫频（chirp）而不是单一正弦：恒定音调的各频带能量不变，
# 「相对上一帧升/降」这 16 bit 全靠数值噪声决定，重编码后整串翻转——
# 那是测试信号退化，不是指纹算法的问题。
subprocess.run([core.ff("ffmpeg"), "-v", "error", "-nostdin", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=320x568:rate=30:duration=8",
                "-f", "lavfi", "-i",
                "aevalsrc=0.5*sin(2*PI*(300+120*t)*t):s=44100:d=8",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest", src],
               check=False, timeout=180)
fp = audiofp.compute(src)
check("真片：指纹非空且是 v2", bool(fp) and base64.b64decode(fp)[:4] == audiofp._MAGIC)
re_msg = os.path.join(tmp, "re.mp4")
subprocess.run([core.ff("ffmpeg"), "-v", "error", "-nostdin", "-y", "-i", src,
                "-c:v", "libx264", "-crf", "30", "-preset", "ultrafast",
                "-c:a", "aac", "-b:a", "96k", re_msg],
               check=False, timeout=180)
# 整片级符号指纹对重编码敏感（同源也可能只有 0.8x），所以「是不是同一条」
# 的最终判断走按需的能量定位：重编码后 ratio 应接近 1
loc_re = audiofp.locate_files(src, re_msg)
check("真片：重编码后仍能判成同一条（能量定位）",
      loc_re["ratio"] >= 0.8 and bool(loc_re["spans"]),
      f"ratio={loc_re['ratio']} best={loc_re['best']}")

# 真片：部分复用（A 的 3–9 秒拼进另一条 10 秒的片）要能定位出来
def _wav(args, out):
    subprocess.run([core.ff("ffmpeg"), "-v", "error", "-nostdin", "-y", *args,
                    "-c:a", "pcm_s16le", out], check=False, timeout=120)


seg_w = os.path.join(tmp, "seg.wav")
pre_w = os.path.join(tmp, "pre.wav")
post_w = os.path.join(tmp, "post.wav")
_wav(["-ss", "3", "-t", "6", "-i", src, "-vn", "-ac", "1", "-ar", "44100"], seg_w)
_wav(["-f", "lavfi", "-i", "sine=frequency=200:duration=2",
      "-ac", "1", "-ar", "44100"], pre_w)
_wav(["-f", "lavfi", "-i", "sine=frequency=900:duration=2",
      "-ac", "1", "-ar", "44100"], post_w)
lst = os.path.join(tmp, "parts.txt")
with open(lst, "w", encoding="utf-8") as f:
    for p in (pre_w, seg_w, post_w):
        f.write(f"file '{p.replace(os.sep, '/')}'\n")
part = os.path.join(tmp, "partial.wav")
subprocess.run([core.ff("ffmpeg"), "-v", "error", "-nostdin", "-y",
                "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", part],
               check=False, timeout=120)
loc_p = audiofp.locate_files(src, part)
check("真片：部分复用定位到约 6 秒",
      loc_p["overlapSeconds"] >= 5.0 and bool(loc_p["spans"]),
      f"overlap={loc_p['overlapSeconds']}s ratio={loc_p['ratio']} spans={loc_p['spans'][:1]}")
silent = os.path.join(tmp, "silent.mp4")
subprocess.run([core.ff("ffmpeg"), "-v", "error", "-nostdin", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=160x160:rate=30:duration=3",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                silent], check=False, timeout=120)
check("真片：无音轨返回空指纹", audiofp.compute(silent) == "")

render._HW["det"] = {}          # 强制软编，别在自测里跑硬件基准
dst = os.path.join(tmp, "cut.mp4")
try:
    render._cut(src, 0.0, 8.0, dst, speed=1.0, zoom=1.0, grade={}, fp={},
                frames=150, out_dur=5.0)
    ok = True
except Exception as exc:  # noqa: BLE001
    ok = False
    print("   _cut 失败：", exc)
check("切段：frames/out_dur 能跑通", ok)
if ok:
    frames = probe(dst, "stream=nb_read_frames")
    vdur = probe_dur(dst, "v:0")
    check("切段：视频恰好 150 帧", frames and int(frames[0]) == 150,
          f"nb_frames={frames[0] if frames else '?'}")
    check("切段：时长 5.00s（±0.05）", abs(vdur - 5.0) <= 0.05, f"{vdur:.3f}s")
    # 帧数路径下分段是纯视频：音轨由渲染阶段单独重建（避免 AAC 填充把拼接挤歪）
    check("切段：分段不带音轨", probe_dur(dst) <= 5.05,
          f"container={probe_dur(dst):.3f}s")

# ── 9. 端到端：render_edl 的帧预算 + 连续音轨 ───────────────────────────
# 用同一份 8 秒素材切 3 段（中间一段走缓冲加速），确认成片帧数/时长与计划一致，
# 且音轨不因分段 AAC 填充而变长——这正是「越往后越不同步」的根。
proj = {"id": "selftest", "name": "自测", "src_dir": tmp,
        "episodes": [{"index": 1, "file": "src.mp4"}]}
edl3 = [{"ep": 1, "start": 0.0, "end": 2.5},
        {"ep": 1, "start": 2.5, "end": 5.0},
        {"ep": 1, "start": 5.0, "end": 8.0}]
bases3 = [1.0, 1.15, 1.0]
plan3 = render.speed_plan([2.5, 2.5, 3.0], bases3, 30, "e2e", "std")
want_f3 = sum(p["frames"] for p in plan3)
want_d3 = sum(p["out_dur"] for p in plan3)
e2e = os.path.join(tmp, "e2e.mp4")
try:
    render.render_edl(
        proj, edl3, "e2e.mp4", out_dir=tmp, zoom=1.03, fx={},
        speed_of=lambda s: 1.15 if abs(float(s["start"]) - 2.5) < 1e-6 else 1.0,
        grade_seed="e2e", fp_level="std", fp_seed="e2e", fp_pos=0.2)
    got_f3 = probe(e2e, "stream=nb_read_frames")
    got_d3 = probe_dur(e2e)
    aud_d3 = probe_dur(e2e, "a:0")
    check("端到端：总帧数与计划一致",
          got_f3 and int(got_f3[0]) == want_f3,
          f"got={got_f3[0] if got_f3 else '?'} want={want_f3}")
    check("端到端：总时长与计划一致", abs(got_d3 - want_d3) <= 1.5 / 30,
          f"{got_d3:.3f}s want {want_d3:.3f}s")
    check("端到端：音轨与视频等长（不累积填充）",
          abs(aud_d3 - got_d3) <= 0.05, f"audio={aud_d3:.3f}s video={got_d3:.3f}s")
except Exception as exc:  # noqa: BLE001
    check("端到端：render_edl 跑通", False, str(exc)[:120])

# ── 汇总 ─────────────────────────────────────────────────────────────────
print(f"\n{len(OK)} 通过 / {len(BAD)} 失败")
if BAD:
    for name, detail in BAD:
        print("  ✗", name, detail)
    print(f"\n失败时保留现场：{tmp}")
    raise SystemExit(1)
import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)
