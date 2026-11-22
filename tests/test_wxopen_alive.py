# -*- coding: utf-8 -*-
"""验证 2026-11-21 事故修复：窗口记录必须看活体，僵尸记录不许继续挡人。

事故现象：点「进入视频号助手」只弹「窗口已经开着了，切过去看就行」，而窗口
早被关掉、进程也没有 —— 窗口线程卡在收尾，`_VIEWERS` 里那条记录永不失效。

运行：python tests/test_wxopen_alive.py
"""
import os
import sys
import tempfile
import threading
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import core, wxopen as wx  # noqa: E402


def _rec(*, ready=True, beat=None, since=None, closed=False):
    r = {"closed": threading.Event(), "stop": threading.Event(),
         "since": time.time() if since is None else since, "ready": ready}
    if beat is not None:
        r["beat"] = beat
    if closed:
        r["closed"].set()
    return r


def test_alive_rules():
    # 已关窗
    assert wx._alive(_rec(ready=True, beat=time.time(), closed=True)) is False
    # 正常开着（心跳新鲜）
    assert wx._alive(_rec(beat=time.time() - 1)) is True
    # 线程卡死 / 已死：心跳断了
    assert wx._alive(_rec(beat=time.time() - 30)) is False
    # 从来没有心跳（老记录）
    assert wx._alive(_rec(beat=None, since=time.time() - 30)) is False
    # 启动中（没 ready 也没心跳），宽限期内算活着
    assert wx._alive(_rec(ready=False, since=time.time() - 5)) is True
    # 启动卡住超过宽限期 → 判死
    assert wx._alive(_rec(ready=False, since=time.time() - 999)) is False
    assert wx._alive(None) is False
    print("[ok] _alive 判定：开着 / 卡死 / 启动中 / 宽限过期")


def _open_with(session_exc=RuntimeError("号正忙")):
    """跑一次 open_platform，不真起浏览器（读 cookie 那步就失败）。"""
    sess = mock.Mock()
    sess.call.side_effect = session_exc
    with mock.patch.object(wx.wxbot, "headless", return_value=False), \
            mock.patch.object(wx.wxbot, "session", return_value=sess), \
            mock.patch.object(wx, "_sweep_stale_profiles", lambda: None):
        return wx.open_platform("aid-test")


def test_zombie_record_does_not_block():
    """僵尸记录（心跳早停）必须被丢掉，不能回 already 挡着用户。"""
    stale = _rec(beat=time.time() - 60)
    wx._VIEWERS["aid-test"] = stale
    try:
        res = _open_with()
        assert res.get("already") is not True, res
        assert stale["stop"].is_set(), "旧线程没被叫停"
        assert "aid-test" not in wx._VIEWERS, "僵尸记录没被清掉"
        assert res.get("ok") is False and "忙" in res.get("note", ""), res
        print("[ok] 心跳过期的记录：丢弃并重新尝试开窗")
    finally:
        wx._VIEWERS.pop("aid-test", None)


def test_live_record_answers_already():
    live = _rec(beat=time.time())
    wx._VIEWERS["aid-test"] = live
    try:
        res = _open_with()
        assert res.get("ok") is True and res.get("already") is True, res
        assert "切过去看" in res.get("note", ""), res
        # 不能假报登录态（旧代码写死 logged_in=True）
        assert "logged_in" not in res, res
        print("[ok] 真的开着：回 already，且不假报 logged_in")
    finally:
        wx._VIEWERS.pop("aid-test", None)


def test_starting_record_says_starting():
    starting = _rec(ready=False, since=time.time() - 3)
    wx._VIEWERS["aid-test"] = starting
    try:
        res = _open_with()
        assert res.get("already") is True and res.get("starting") is True, res
        assert "正在打开" in res.get("note", ""), res
        print("[ok] 启动中：回 starting，不误报「已经开着了」")
    finally:
        wx._VIEWERS.pop("aid-test", None)


def test_sweep_only_old_viewer_dirs():
    """只清 1 小时前的 viewer_*，正在用的和新开的都不动。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "_tmp_browser")
        os.makedirs(root)
        old = os.path.join(root, "viewer_old")
        fresh = os.path.join(root, "viewer_fresh")
        live = os.path.join(root, "viewer_live")
        other = os.path.join(root, "playwright_chromiumdev_profile-x")
        for p in (old, fresh, live, other):
            os.makedirs(p)
        stamp = time.time() - 7200
        for p in (old, live, other):
            os.utime(p, (stamp, stamp))

        rec = _rec(beat=time.time())
        rec["prof_dir"] = live
        wx._VIEWERS["aid-test"] = rec
        try:
            with mock.patch.object(core, "DATA_DIR", tmp):
                wx._sweep_stale_profiles()
        finally:
            wx._VIEWERS.pop("aid-test", None)

        assert not os.path.exists(old), "陈旧 viewer 目录没清"
        assert os.path.exists(fresh), "新目录被误删"
        assert os.path.exists(live), "正在用的目录被误删"
        assert os.path.exists(other), "非 viewer_ 目录被误删"
        print("[ok] 陈旧目录清理：只删 1 小时前的 viewer_*，跳过在用目录")


if __name__ == "__main__":
    test_alive_rules()
    test_zombie_record_does_not_block()
    test_live_record_answers_already()
    test_starting_record_says_starting()
    test_sweep_only_old_viewer_dirs()
    print("\nALL PASSED")
