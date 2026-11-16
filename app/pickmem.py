"""记住「上次选过哪儿」，让系统原生选择框下次直接开在上次的目录。

解决的问题：原生「选择文件夹 / 选择文件」每次都从系统默认位置开，而用户的
素材永远在 F:/素材/某剧，每选一次就要重走一遍目录。这里记两件事：
- 对话框起始位置（recall_dir）：下次直接开在用户上次确认的目录；
- 最近用过的路径（recent）：界面摆成「最近用过」快捷项，点一下直接选中。

为什么记在服务端而不是 localStorage：真正弹框的是后端进程，只有它知道用户
最后确认了什么。前端再存一份，迟早出现「输入框写着 A、对话框开在 B」的两套账。

落盘 data/pick_history.json，按用途分 key，每个 key 最多 LIMIT 条；
读的时候丢掉目录已经不在的条目，换盘、删目录后不会给出死路径。
"""
import json
import os
import threading
import time

from . import core

FILE = os.path.join(core.DATA_DIR, "pick_history.json")
LIMIT = 6
# key 由前端传，必须白名单：不然等于让人往数据目录里随便塞没用的键
KEYS = ("proj_out", "merge", "bgm", "lut", "publish_dir", "publish_files", "cover")

_lock = threading.Lock()


def _load() -> dict:
    try:
        with open(FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(FILE), exist_ok=True)
        tmp = FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, FILE)
    except OSError:
        pass  # 记不住历史是小事，绝不能让它把「选文件」本身搞失败


def remember(key: str, path: str, is_dir: bool) -> None:
    """选完就记：同一路径提到最前，超过 LIMIT 丢掉最老的。"""
    if key not in KEYS or not path:
        return
    p = os.path.abspath(str(path)).replace("\\", "/")
    d_ = p if is_dir else os.path.dirname(p)
    if not d_:
        return
    e = {"path": p, "dir": d_, "is_dir": bool(is_dir), "ts": int(time.time())}
    with _lock:
        data = _load()
        rows = [r for r in (data.get(key) or [])
                if isinstance(r, dict) and r.get("path") != p]
        data[key] = [e] + rows[: LIMIT - 1]
        _save(data)


def recall_dir(key: str) -> str:
    """对话框起始目录：上次确认过的那个目录；不存在就当没记过。"""
    for r in _rows(key, LIMIT):
        d = str(r.get("dir") or "")
        if d and os.path.isdir(d):
            return d
    return ""


def recent(key: str, limit: int = 3) -> list[dict]:
    """界面「最近用过」用：只要目录还在的，最多 limit 条。"""
    out = []
    for r in _rows(key, LIMIT):
        d = str(r.get("dir") or "")
        if not d or not os.path.isdir(d):
            continue
        out.append({"path": str(r.get("path") or ""), "dir": d,
                    "is_dir": bool(r.get("is_dir"))})
        if len(out) >= limit:
            break
    return out


def _rows(key: str, limit: int) -> list[dict]:
    if key not in KEYS:
        return []
    return [r for r in (_load().get(key) or []) if isinstance(r, dict)][:limit]
