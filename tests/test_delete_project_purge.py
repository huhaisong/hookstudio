"""删项目同步清理自测：全程临时目录，不碰真实 data/。

跑法：python tests/test_delete_project_purge.py
覆盖：项目目录 / 渲染临时 / 中转盘 / 续跑标记 / 日志备份 / 发布队列 /
归档与备份 / 审计日志的清理；共享缓存不动；墓碑挡住 worker 迟到写回。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException

from app import audit, core, db, main, publisher


def touch(path: str, text: str = "x") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def jdump(path: str, obj) -> None:
    touch(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def run() -> None:
    tmp = tempfile.mkdtemp(prefix="hs_purge_")
    pid = "abc123def456"
    other = "999888777666"
    name = "自测专用剧名_purge_unique"
    old: dict = {}
    mods = (core, publisher, audit, db)
    keys = {
        core: ["DATA_DIR", "PROJECTS", "OUTPUT", "CONFIG_FILE", "RESUME_PENDING",
               "DELETED_FILE", "BASE"],
        publisher: ["QUEUE_FILE", "ARCHIVE_FILE"],
        db: ["DB_FILE"],
    }
    try:
        for mod, names in keys.items():
            for k in names:
                old[(mod, k)] = getattr(mod, k)
        core.DATA_DIR = os.path.join(tmp, "data")
        core.PROJECTS = os.path.join(core.DATA_DIR, "projects")
        core.OUTPUT = os.path.join(core.DATA_DIR, "output")
        core.CONFIG_FILE = os.path.join(core.DATA_DIR, "config.json")
        core.RESUME_PENDING = os.path.join(core.DATA_DIR, "resume_pending.json")
        core.DELETED_FILE = os.path.join(core.DATA_DIR, "deleted_pids.json")
        core.BASE = tmp
        publisher.QUEUE_FILE = os.path.join(core.DATA_DIR, "publish_queue.json")
        publisher.ARCHIVE_FILE = os.path.join(core.DATA_DIR, "queue_archive.json")
        db.DB_FILE = os.path.join(core.DATA_DIR, "app.db")
        os.makedirs(core.PROJECTS, exist_ok=True)
        os.makedirs(core.OUTPUT, exist_ok=True)
        core._DELETED.clear()
        audit._NAME_CACHE.clear()

        out = os.path.join(tmp, "out")
        core.save({"id": pid, "name": name, "batch": {"save_dir": out},
                   "sets": [], "episodes": []})
        # 项目内部 + 出片盘 + 中转盘 + logs 散落的分段临时
        touch(os.path.join(core.PROJECTS, pid, "nosub", "e01.mp4"))
        touch(os.path.join(core.PROJECTS, pid, "words", "01.json"))
        touch(os.path.join(out, "_render_tmp", pid, "s1", "s000.mp4"))
        touch(os.path.join(core.DATA_DIR, "_render_stage", pid,
                           "_render_tmp", pid, "s1", "s000.mp4"))
        touch(os.path.join(core.DATA_DIR, "_render_tmp", pid, "s000.mp4"))
        touch(os.path.join(tmp, "logs", "_render_tmp", pid, "s000.mp4"))
        # 别的项目在同盘的分段临时，绝不能跟着删
        touch(os.path.join(out, "_render_tmp", other, "s1", "s000.mp4"))
        core.set_resume_pending(pid, {"kind": "cuts", "name": name})
        logbak = os.path.join(tmp, "logs", f"backup_project_{pid}_20261121.json")
        touch(logbak)
        touch(os.path.join(tmp, "logs", "archive_x", "proj", pid + ".project.json"))
        touch(os.path.join(tmp, "logs", f"backup_project_{other}_20261121.json"))
        # 跨项目共享的缓存，删项目时一律不动
        shared = [os.path.join(core.DATA_DIR, "voice_cache", "a" * 16 + ".wav"),
                  os.path.join(core.DATA_DIR, "bgm_cache", "t1.json"),
                  os.path.join(tmp, "bgm", "library.json")]
        for f in shared:
            touch(f)

        q = publisher.queue()
        q["jobs"] = [
            {"id": "j1", "pid": pid, "status": "draft", "file": "a.mp4"},
            {"id": "j2", "pid": pid, "status": "publishing", "file": "b.mp4"},
            {"id": "j3", "pid": other, "status": "queued", "file": "c.mp4"},
        ]
        publisher.save_queue(q)
        jdump(publisher.ARCHIVE_FILE, [{"archived_at": "x", "jobs": [
            {"id": "a1", "pid": pid}, {"id": "a2", "pid": other}]}])
        bdir = os.path.join(core.DATA_DIR, "queue_backup")
        jdump(os.path.join(bdir, "queue_20260101_000000.json"),
              {"jobs": [{"id": "b1", "pid": pid}, {"id": "b2", "pid": other}]})

        db.init()
        with db.db() as c:
            # 一条只靠 path 里的 pid 命中，一条只靠唯一剧名命中（body 埋点那种）
            c.execute("INSERT INTO audit_logs (created_at, path, target) VALUES (?,?,?)",
                      ("2026-01-01T00:00:00+00:00", f"/api/projects/{pid}/plan", ""))
            c.execute("INSERT INTO audit_logs (created_at, path, target) VALUES (?,?,?)",
                      ("2026-01-01T00:00:00+00:00", "/api/ai/grant", f"project:{name}"))
            c.execute("INSERT INTO audit_logs (created_at, path, target) VALUES (?,?,?)",
                      ("2026-01-01T00:00:00+00:00", f"/api/projects/{other}/plan",
                       "project:别的项目"))

        # 有未发条目时先拦一次（没带 force）
        try:
            main.delete(pid, force=0)
            raise AssertionError("应当返回 409")
        except HTTPException as e:
            assert e.status_code == 409, e.status_code

        res = main.delete(pid, force=1)
        assert res["ok"] is True
        assert res["files"]["project"] is True
        assert len(res["files"]["render_tmp"]) >= 3, res["files"]
        assert res["files"]["stage"] is True and res["files"]["resume"] is True
        assert res["jobs"]["queue"] == 1, res["jobs"]
        assert res["jobs"]["kept"] == 1, res["jobs"]      # publishing 留着
        assert res["logs"] == 2, res["logs"]              # path + 剧名各一条

        # 该删的都没了
        assert not os.path.isdir(os.path.join(core.PROJECTS, pid))
        for f in (os.path.join(out, "_render_tmp", pid),
                  os.path.join(core.DATA_DIR, "_render_stage", pid),
                  os.path.join(tmp, "logs", "_render_tmp", pid), logbak,
                  os.path.join(tmp, "logs", "archive_x", "proj", pid + ".project.json")):
            assert not os.path.exists(f), f
        assert pid not in core.load_resume_pending()
        # 不该删的都还在
        assert os.path.isdir(os.path.join(out, "_render_tmp", other))
        assert os.path.isfile(os.path.join(tmp, "logs",
                                           f"backup_project_{other}_20261121.json"))
        for f in shared:
            assert os.path.isfile(f), f
        q2 = publisher.queue()
        assert [j["id"] for j in q2["jobs"]] == ["j2", "j3"], q2["jobs"]
        with open(publisher.ARCHIVE_FILE, encoding="utf-8") as f:
            arch = json.load(f)
        assert [j["pid"] for b in arch for j in b["jobs"]] == [other]
        for fn in os.listdir(bdir):
            with open(os.path.join(bdir, fn), encoding="utf-8") as f:
                d = json.load(f)
            assert all(j.get("pid") != pid for j in d["jobs"]), fn
        with db.db() as c:
            left = [dict(r) for r in c.execute(
                "SELECT path FROM audit_logs").fetchall()]
            assert len(left) == 1 and other in left[0]["path"], left

        # 墓碑：迟到的 save / save_merge 都不许把项目写回来
        core.save({"id": pid, "name": "ghost"})
        core.save_merge({"id": pid, "edl": [1]}, ["edl"])
        assert not os.path.isdir(os.path.join(core.PROJECTS, pid)), "项目被写回复活了"
        assert core.is_deleted(pid)

        print("PASS 删项目同步清理：缓存/队列/归档/备份/日志全清，共享数据保留，墓碑生效")
    finally:
        for (mod, k), v in old.items():
            setattr(mod, k, v)
        core._DELETED.clear()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    run()
