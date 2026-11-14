"""生成内置曲库的占位氛围音（纯程序合成，无任何第三方录音版权）。

这些只是「能跑通链路」的示例音，不是成品配乐。正式使用请把你已获授权的
音频放进 bgm/audio/（或 data/bgm/），并同步维护 bgm/library.json 的 mood / tags。

运行：runtime/Scripts/python.exe bgm/make_samples.py
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
AUDIO = os.path.join(ROOT, "bgm", "audio")
DUR = 24


def _exe(name: str) -> str:
    p = os.path.join(BIN, name + (".exe" if os.name == "nt" else ""))
    return p if os.path.isfile(p) else name


# (曲目 id, 波形表达式, 低通截止频率)
TRACKS = [
    ("tense_01",
     "0.16*sin(2*PI*110*t)*(0.75+0.25*sin(2*PI*0.7*t))"
     "+0.09*sin(2*PI*146.83*t)+0.05*sin(2*PI*220*t)", 900),
    ("emotional_01",
     "0.13*sin(2*PI*220*t)*(0.8+0.2*sin(2*PI*0.25*t))"
     "+0.08*sin(2*PI*277.18*t)+0.05*sin(2*PI*329.63*t)", 1500),
    ("upbeat_01",
     "0.11*sin(2*PI*261.63*t)*(0.7+0.3*sin(2*PI*1.6*t))"
     "+0.08*sin(2*PI*329.63*t)+0.05*sin(2*PI*392*t)", 2400),
    ("neutral_01",
     "0.12*sin(2*PI*174.61*t)*(0.85+0.15*sin(2*PI*0.2*t))"
     "+0.06*sin(2*PI*261.63*t)", 800),
]


def main() -> int:
    os.makedirs(AUDIO, exist_ok=True)
    ffmpeg, ffprobe = _exe("ffmpeg"), _exe("ffprobe")
    for tid, expr, lowpass in TRACKS:
        out = os.path.join(AUDIO, tid + ".m4a")
        cmd = [
            ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"aevalsrc={expr}:s=48000:d={DUR}",
            "-af", f"lowpass=f={lowpass},afade=t=in:st=0:d=2,"
                   f"afade=t=out:st={DUR - 3}:d=3",
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2", out,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[FAIL] {tid}: {(r.stderr or '').strip()[:400]}")
            return 1
        probe = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", out], capture_output=True, text=True)
        dur = (probe.stdout or "").strip()
        if probe.returncode != 0 or not dur:
            print(f"[FAIL] {tid}: 生成的文件读不出时长")
            return 1
        print(f"[OK] {tid}.m4a  {dur}s  {os.path.getsize(out) // 1024}KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
