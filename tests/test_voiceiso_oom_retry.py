"""验证 voiceiso.separate 的 OOM 自动重试（2026-11-21 方案C）。"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import voiceiso

# 加速自测：退避改为 0
voiceiso._OOM_BACKOFF = 0

calls = {"n": 0}

def fake_separate(wav, keep_db, on_progress):
    calls["n"] += 1
    if calls["n"] <= 2:
        raise MemoryError("Unable to allocate 40.4 MiB for an array with shape (2, 862, 6144)")
    return "/fake/clean.wav"

voiceiso._separate = fake_separate

# 场景1：前两次 OOM，第三次成功
calls["n"] = 0
r = voiceiso.separate("in.wav")
assert r == "/fake/clean.wav", r
assert calls["n"] == 3, calls
print("PASS 场景1：前2次OOM自动重试，第3次成功（共3次调用）")

# 场景2：一直 OOM，超过重试次数要抛 RuntimeError（不能让裸 MemoryError 冒出去）
def always_oom(wav, keep_db, on_progress):
    calls["n"] += 1
    raise MemoryError("oom")
voiceiso._separate = always_oom
calls["n"] = 0
try:
    voiceiso.separate("in.wav")
    raise AssertionError("应当抛异常")
except RuntimeError as e:
    assert "重试" in str(e), str(e)
    assert calls["n"] == 3, calls
    print("PASS 场景2：持续OOM共3次后抛人话RuntimeError（%s）" % str(e)[:40])

# 场景3：非 OOM 的异常不重试，直接抛出
def other_err(wav, keep_db, on_progress):
    calls["n"] += 1
    raise RuntimeError("模型坏了")
voiceiso._separate = other_err
calls["n"] = 0
try:
    voiceiso.separate("in.wav")
    raise AssertionError("应当抛异常")
except RuntimeError as e:
    assert str(e) == "模型坏了", str(e)
    assert calls["n"] == 1, calls
    print("PASS 场景3：非OOM异常不重试，直接抛出")

print("ALL PASS")
