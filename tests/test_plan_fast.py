"""排期预览不该再解音轨（2026-11-21）。

现场：改一下时间就要等 10~15 秒，审计里全是 `/api/publish/plan`。
实测 22 条 draft（老条目各带 afp）→ repost_conflicts 10.25 秒，7 次
`locate_files`，每次解两条完整音轨。内容查重已从入队与预览两条路径摘掉，
这里守住「preview 路径不许出现音频解码」。
"""
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import audiofp, publisher  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = io.open(os.path.join(ROOT, "app", "publisher.py"), encoding="utf-8").read()
# 只查代码：注释里写着「以前用 audiofp.compute」不算调用
code = "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())


def test_no_audio_decode_on_plan_path():
    assert "locate_files" not in code, "publisher 里又在解音轨了（preview 会变慢）"
    assert "audiofp.compute" not in code, "publisher 里又在算指纹了"
    assert "import audiofp" not in code, "publisher 不该再依赖 audiofp"


def test_repost_ignores_afp():
    """老条目带 afp 也只按文件名提示，且不因为 afp 去碰文件系统。"""
    old = audiofp.locate_files

    def boom(*_a, **_k):
        raise AssertionError("不该在查重时解码音频")
    audiofp.locate_files = boom
    orig_sim = audiofp.similarity
    audiofp.similarity = lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("不该再比指纹"))
    try:
        jobs = [{"id": "j1", "file": "F:/a/set01.mp4", "afp": "x" * 20},
                {"id": "j2", "file": "F:/a/set02.mp4", "afp": "x" * 20}]
        q = {"ledger": [{"job_id": "h1", "file": "F:/a/set01.mp4",
                         "status": "published", "afp": "x" * 20,
                         "scheduled_at": "2026-11-20T20:00"}], "jobs": []}
        out = publisher.repost_conflicts(jobs, q)
        assert [r["job_id"] for r in out] == ["j1"], out
        assert out[0]["kind"] == "file"
        # 换成不同路径 + 同一 afp：以前会报「内容重复」，现在不报（已摘掉）
        jobs2 = [{"id": "j3", "file": "F:/b/other.mp4", "afp": "x" * 20}]
        assert publisher.repost_conflicts(jobs2, q) == []
    finally:
        audiofp.locate_files = old
        audiofp.similarity = orig_sim


def test_preview_fast_with_legacy_afp(tmp_path=None):
    """22 条带 afp 的老草稿：预览必须毫秒级返回（以前 10 秒+）。"""
    import json
    import tempfile

    jobs = [{"id": f"j{i:02d}", "status": "draft", "file": f"F:/x/set{i:02d}.mp4",
             "size": 100 * 1048576, "afp": "a" * 40, "title": f"第{i}条",
             "created_at": "2026-11-21T20:41:00"}
            for i in range(22)]
    q = {"v": 3, "jobs": jobs, "ledger": [], "template": {},
         "batch": {}, "current_account_id": ""}
    tmpdir = tempfile.mkdtemp(prefix="planfast-")
    path = os.path.join(tmpdir, "q.json")
    json.dump(q, io.open(path, "w", encoding="utf-8"), ensure_ascii=False)
    old_file = publisher.QUEUE_FILE
    publisher.QUEUE_FILE = path
    try:
        t = time.time()
        res = publisher.preview("2026-11-21T22:00", 30)
        cost = time.time() - t
        assert res["n"] == 22, res
        assert cost < 1.0, f"preview 用了 {cost:.2f}s，说明又在解码音频"
    finally:
        publisher.QUEUE_FILE = old_file
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("  PASS", name)
            except AssertionError as e:
                fails += 1
                print("  FAIL", name, "->", e)
    print("失败" if fails else "全部通过", fails)
    sys.exit(1 if fails else 0)
