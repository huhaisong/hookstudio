"""整批短标题（2026-11-21 用户要求「短标题要可以统一设置」）。

口径：
- 模板里有 short_title（≤16 字，留空＝让平台自己截），新入队的条目继承它；
- 在 ② 改一次就套到这批还没发的条目上（BATCH_KEYS）；
- **但单条自己写过的保留**：否则调一下话题就把 22 条各自写好的短标题冲掉。
"""
import json
import os
import sys
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import publisher  # noqa: E402

TMP = tempfile.mkdtemp(prefix="stbatch-")
_old = publisher.QUEUE_FILE
publisher.QUEUE_FILE = os.path.join(TMP, "q.json")


def _reset():
    q = {"v": 3, "jobs": [], "ledger": [], "template": {}, "batch": {},
         "current_account_id": ""}
    json.dump(q, open(publisher.QUEUE_FILE, "w", encoding="utf-8"),
              ensure_ascii=False)


_N = [0]


def _mkfile(name):
    p = os.path.join(TMP, name)
    with open(p, "wb") as f:
        f.write(b"x" * 1024)
    return p


def _seed(short="", n=2):
    """空队列 + n 条草稿（每次文件名唯一，避开 hash 去重）。"""
    _reset()
    if short:
        publisher.apply_fields({"short_title": short})
    files = []
    for _ in range(n):
        _N[0] += 1
        files.append(_mkfile(f"seed{_N[0]}.mp4"))
    publisher.add_jobs(files, [], "", "")
    return publisher.queue()["jobs"]


def test_default_tpl_has_short_title():
    assert "short_title" in publisher.BATCH_KEYS
    assert publisher._default_tpl().get("short_title") == ""


def test_validation():
    assert publisher.norm_field("short_title", "  婆媳 力挽 狂澜 ") == "婆媳 力挽 狂澜"
    try:
        publisher.norm_field("short_title", "太短了")
    except publisher.PublishError:
        pass
    else:
        raise AssertionError("5 个字应该被拒")
    assert publisher.norm_field("short_title", "") == ""
    try:
        publisher.norm_field("short_title", "一" * 20)
    except publisher.PublishError:
        pass
    else:
        raise AssertionError("20 个字应该被拒（前端 maxlength=16 拦在前面）")


def test_apply_sets_template_and_pushes_to_drafts():
    jobs = _seed(short="婆媳一开口全屋安静")
    assert publisher.queue()["template"]["short_title"] == "婆媳一开口全屋安静"
    assert len(jobs) == 2
    # 新入队继承模板
    assert all(j["short_title"] == "婆媳一开口全屋安静" for j in jobs), jobs

    # 改整批：跟着整批的那两条一起变
    publisher.apply_fields({"short_title": "换个短标题七字以上"})
    assert all(j["short_title"] == "换个短标题七字以上"
               for j in publisher.queue()["jobs"])


def test_apply_keeps_per_job_custom():
    _seed(short="一开始的整批短标题")
    q = publisher.queue()
    jobs = q["jobs"]
    jobs[0]["short_title"] = "这一条我自己写的"
    publisher.save_queue(q)

    publisher.apply_fields({"short_title": "整批统一的新短标题"})
    after = {j["id"]: j["short_title"] for j in publisher.queue()["jobs"]}
    assert after[jobs[0]["id"]] == "这一条我自己写的", after
    assert after[jobs[1]["id"]] == "整批统一的新短标题", after

    # 清空整批：跟着整批的变空，单条自定义的还在
    publisher.apply_fields({"short_title": ""})
    after2 = {j["id"]: j["short_title"] for j in publisher.queue()["jobs"]}
    assert after2[jobs[0]["id"]] == "这一条我自己写的", after2
    assert after2[jobs[1]["id"]] == "", after2


def test_queued_jobs_untouched():
    _seed(short="原始短标题七字以上")
    q = publisher.queue()
    for j in q["jobs"]:
        j["status"] = "queued"
    publisher.save_queue(q)
    publisher.apply_fields({"short_title": "排队了就不该动"})
    assert all(j["short_title"] != "排队了就不该动"
               for j in publisher.queue()["jobs"])


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
    publisher.QUEUE_FILE = _old
    shutil.rmtree(TMP, ignore_errors=True)
    print("失败" if fails else "全部通过", fails)
    sys.exit(1 if fails else 0)
