"""构建指纹（版本闸的判据）。

**要解决的问题**：后端已经换成新代码，而浏览器里还在跑旧的 app.js。
这种状态最坑人——按钮点了没反应、参数按旧格式提交、AI 授权接口不认，
用户只会觉得「坏了」，排查时又要重新怀疑一遍后端。
（本项目的热重载与缓存已经踩过好几轮，靠人工提醒 Ctrl+F5 不是办法。）

**做法**：把 web/ app/ prompts/ 下所有文件的名字 + 大小 + 修改时间混成一个
10 位短哈希。任何文件被改动 → 指纹变。

**怎么用**：
  · 页面路由把指纹注入 `<meta name="app-build">`，并给静态资源加 `?v=<指纹>`；
  · `/api/status` 回 `build`；
  · 每个响应带 `X-App-Build` 头；
  · 前端 `web/version.js` 拿三者互相比对，对不上就锁屏，只留「立即刷新」。

指纹本身不参与安全判断——它只是「一致 / 不一致」的标记，泄露没有风险，
所以用带 mtime 的快速哈希而不是内容哈希（内容哈希每次启动要读几 MB）。
"""
from __future__ import annotations

import hashlib
import os
import threading
import time

from . import core

VERSION = "0.1.27"

# 参与指纹的目录（相对 core.BASE）。web 是前端资源，app 是后端代码，
# prompts 是提示词——提示词改一个字，AI 的输出结构就可能变，前端要重新拉一次。
_ROOTS = (("app", os.path.join(core.BASE, "app")),
          ("web", core.WEB),
          ("prompts", core.PROMPTS))

_SKIP_EXT = (".pyc", ".pyo", ".log", ".tmp")
_SKIP_NAME = ("__pycache__",)

_lock = threading.Lock()
_cache = {"value": "", "at": 0.0}
_TTL = 2.0          # 秒。开发时改完文件两秒内就能被前端发现；线上多算几次也无所谓


def _walk(root: str):
    """按稳定顺序吐出 (相对路径, size, mtime_ns)。

    排过序是必须的：os.walk 的顺序不保证一致，顺序一变指纹就跟着抖，
    结果就是「什么都没改也提示要刷新」。
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_NAME)
        for fn in sorted(filenames):
            if fn.startswith("_") or fn.endswith(_SKIP_EXT):
                continue
            p = os.path.join(dirpath, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            yield os.path.relpath(p, root), st.st_size, st.st_mtime_ns


def _compute() -> str:
    h = hashlib.sha1()
    h.update(VERSION.encode())
    for tag, root in _ROOTS:
        if not os.path.isdir(root):
            continue
        for rel, size, mtime in _walk(root):
            h.update(f"{tag}/{rel}|{size}|{mtime}\n".encode())
    return h.hexdigest()[:10]


def current() -> str:
    """当前构建指纹。带 2 秒缓存——中间件每个请求都会问一次。"""
    now = time.time()
    with _lock:
        if _cache["value"] and now - _cache["at"] < _TTL:
            return _cache["value"]
    val = _compute()
    with _lock:
        _cache["value"], _cache["at"] = val, now
    return val
