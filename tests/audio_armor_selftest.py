"""音频适配（audio_armor）自测（2026-11-22）。

用法同 dedup_selftest：
    $env:PYTHONPATH = "<repo>\\hookstudio\\runtime\\Lib\\site-packages"
    python hookstudio\\tests\\audio_armor_selftest.py

验证四件事：
  1. 链路单元：k≈1 返回空（off 档 -af 与现状逐字一致）；k>1 时 asetrate/atempo
     互为倒数（保时长纯变调的硬要求）；
  2. 同一素材 off vs on 各渲一条：都跑通，音画时长差 <0.1s（时长严格不变的
     验收线，复用漂移告警的口径）；
  3. off 两次重渲（同种子）指纹完全一致——armor 没开时不引入任何不确定性；
  4. off vs on 的整片相似度显著低于 off vs off——扰动真的起效。
不连网、不改项目数据：config 打桩在 core.load_config 上，跑完还原。
退出码 0 = 全过；1 = 有失败。
"""
import base64
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:  # 控制台默认 GBK，✓/✗ 会直接抛 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from app import audiofp, core, render  # noqa: E402

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append((name, detail))
    print(("✓ " if cond else "✗ ") + name + ((" · " + detail) if detail else ""))


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


# ── 1. 链路单元 ──────────────────────────────────────────────────────────
check("链路：off（k≈1）返回空——-af 与现状逐字一致",
      render._audio_armor_chain(1.0, 0.0) == [])
ch = render._audio_armor_chain(1.01, 1.2)
k = 1.01
check("链路：asetrate/atempo 互为倒数（保时长）",
      bool(ch) and abs(float(ch[1].split("=")[1]) - 1.0 / k) < 1e-4
      and ch[0] == f"asetrate={48000 * k:.2f}" and "aresample=48000" in ch,
      " | ".join(ch))

# ── 2. 真渲对比：off vs on ───────────────────────────────────────────────
tmp = os.path.join(ROOT, "logs", "_armor_selftest")
os.makedirs(tmp, exist_ok=True)
src = os.path.join(tmp, "src.mp4")
# 扫频（chirp）源：恒定音调的频带能量不变，指纹会退化成数值噪声（见 dedup_selftest）
subprocess.run([core.ff("ffmpeg"), "-v", "error", "-nostdin", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=320x568:rate=30:duration=8",
                "-f", "lavfi", "-i",
                "aevalsrc=0.5*sin(2*PI*(300+120*t)*t):s=44100:d=8",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest", src],
               check=False, timeout=180)

proj = {"id": "armorselftest", "name": "自测", "src_dir": tmp,
        "episodes": [{"index": 1, "file": "src.mp4"}]}
edl = [{"ep": 1, "start": 0.0, "end": 4.0},
       {"ep": 1, "start": 4.0, "end": 8.0}]

# config 打桩：真实值打底，只覆写 audio_armor（hw_encode 等保持本机原样）
_real_load = core.load_config


def _patched(armor):
    def _load(*a, **k):
        cfg = dict(_real_load() or {})
        cfg["audio_armor"] = armor
        return cfg
    return _load


render._HW["det"] = {}          # 强制软编，别在自测里跑硬件基准
outs = {}
core.load_config = _patched(False)
try:
    outs["off_a"] = os.path.join(tmp, "off_a.mp4")
    render.render_edl(proj, edl, "off_a.mp4", out_dir=tmp, zoom=1.03, fx={},
                      grade_seed="armor", fp_level="std", fp_seed="armor",
                      fp_pos=0.3)
    outs["off_b"] = os.path.join(tmp, "off_b.mp4")
    render.render_edl(proj, edl, "off_b.mp4", out_dir=tmp, zoom=1.03, fx={},
                      grade_seed="armor", fp_level="std", fp_seed="armor",
                      fp_pos=0.3)
    core.load_config = _patched(True)
    outs["on_b"] = os.path.join(tmp, "on_b.mp4")
    render.render_edl(proj, edl, "on_b.mp4", out_dir=tmp, zoom=1.03, fx={},
                      grade_seed="armor", fp_level="std", fp_seed="armor",
                      fp_pos=0.3)
finally:
    core.load_config = _real_load

for tag, path in outs.items():
    vd, ad = probe_dur(path, "v:0"), probe_dur(path, "a:0")
    check(f"{tag}：渲通且音画等长（<0.1s）",
          vd > 1.0 and abs(vd - ad) <= 0.1, f"video={vd:.3f}s audio={ad:.3f}s")

# ── 3. 指纹对比 ──────────────────────────────────────────────────────────
fp_off_a = audiofp.compute(outs["off_a"])
fp_off_b = audiofp.compute(outs["off_b"])
fp_on_b = audiofp.compute(outs["on_b"])
sim_re = audiofp.similarity(fp_off_a, fp_off_b)
sim_ar = audiofp.similarity(fp_off_a, fp_on_b)
check("指纹：off 重渲完全可复现（同种子相似度 ≈1）", sim_re >= 0.99,
      f"sim={sim_re:.4f}")
check("指纹：on 之后相似度显著下降（扰动起效）",
      sim_ar < sim_re - 0.05, f"sim(off,on)={sim_ar:.4f} vs {sim_re:.4f}")
loc = audiofp.locate_files(outs["off_a"], outs["on_b"])
# 信息项不设硬断言：定位比值取决于抽到的音高——打印出来供量化报告引用
print(f"  ℹ 能量定位（内容层）：ratio={loc['ratio']:.3f} "
      f"overlap={loc['overlapSeconds']:.1f}s —— 音频适配不改变内容结构，"
      f"台账口径仍以块键为准")

# ── 汇总 ─────────────────────────────────────────────────────────────────
print(f"\n{len(OK)} 通过 / {len(BAD)} 失败")
if BAD:
    for name, detail in BAD:
        print("  ✗", name, detail)
    print(f"\n失败时保留现场：{tmp}")
    raise SystemExit(1)
import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)
