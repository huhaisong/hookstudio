"""内置 BGM 曲库：按 AI 的配乐建议自动选曲，并把音频备成本地文件。

设计边界（2026-11-12）：
  * 模型只给「这套适合什么音乐」的一句话（prompts/plans_schema.txt 的 bgm 字段），
    **既不提供下载 URL，也不写歌名**。「情绪文字 → 曲目」的映射放在本模块，
    提示词一行都不用改；曲库换歌、加歌都不影响模型输出。
  * 真实下载地址只来自随包清单 bgm/library.json 的 url 字段（且 host 必须在
    allow_hosts 白名单里），绝不来自模型。默认 allow_hosts 为空 = 只认本地音频。
  * 渲染层 render._mix_bgm 只认本地文件路径，这里负责把它准备好，渲染代码不改。
  * 曲库缺文件 / 下载失败 / 文件损坏，一律抛中文 BgmError，绝不静默丢掉 BGM。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from urllib.parse import urljoin, urlparse

import requests

from . import core

LIB_DIR = os.path.join(core.BASE, "bgm")
USER_DIR = os.path.join(core.DATA_DIR, "bgm")
CACHE_DIR = os.path.join(core.DATA_DIR, "bgm_cache")
LIB_FILE = os.path.join(LIB_DIR, "library.json")
USER_LIB_FILE = os.path.join(USER_DIR, "library.json")

AUDIO_EXT = (".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".wma")
MAX_BYTES = 30 * 1024 * 1024
DOWNLOAD_TIMEOUT = (8, 60)

# AI 的 bgm 是一句中文描述，这里把它归到曲库的情绪档。曲库加歌只要填对
# mood / tags，不用动提示词，也不用动这段词典（词典只认常见的中文描述词）。
MOOD_RULES = {
    "tense": ("紧张", "悬疑", "压迫", "冲突", "弦乐", "鼓点", "惊悚", "危机",
              "对峙", "紧绷", "低音", "急促", "反转", "爆点", "阴冷", "暗黑", "沉重"),
    "emotional": ("抒情", "钢琴", "温情", "压抑", "伤感", "落泪", "亲情", "渐强",
                  "柔软", "舒缓", "治愈", "感动", "温柔", "虐心", "慢节奏", "独白"),
    "upbeat": ("轻快", "明快", "欢快", "燃", "爽", "活泼", "喜剧", "跳跃",
               "轻松", "甜", "高能", "节奏感", "律动", "热血"),
    "neutral": ("铺底", "安静", "氛围", "平缓", "低调", "叙事", "日常", "清淡",
                "留白", "中性", "过渡"),
}


class BgmError(Exception):
    """能直接给用户看的中文错误。"""


def _under(child: str, parent: str) -> bool:
    try:
        return (os.path.normcase(os.path.commonpath(
            [os.path.abspath(child), os.path.abspath(parent)]))
            == os.path.normcase(os.path.abspath(parent)))
    except (ValueError, OSError):
        return False


def _read_json(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _normalize(raw: dict, base: str) -> dict | None:
    tid = str(raw.get("id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tid):
        return None
    rel = str(raw.get("file") or "").strip()
    file_path = ""
    if rel:
        cand = rel if os.path.isabs(rel) else os.path.join(base, rel)
        cand = os.path.abspath(cand)
        # 防目录穿越：只允许落在曲库相关的几个根目录里
        if not any(_under(cand, r) for r in (base, LIB_DIR, USER_DIR, CACHE_DIR)):
            return None
        file_path = cand
    tags = raw.get("tags") or []
    if not isinstance(tags, (list, tuple)):
        tags = [str(tags)]
    return {
        "id": tid,
        "name": str(raw.get("name") or tid),
        "mood": str(raw.get("mood") or "neutral"),
        "tags": [str(t).strip() for t in tags if str(t).strip()],
        "file": file_path,
        "url": str(raw.get("url") or "").strip(),
        "license": str(raw.get("license") or "未标注授权"),
        "source": str(raw.get("source") or ""),
    }


def _from_filename(path: str) -> dict:
    stem = os.path.splitext(os.path.basename(path))[0]
    mood = "neutral"
    for m, kws in MOOD_RULES.items():
        if any(k in stem for k in kws):
            mood = m
            break
    return {
        "id": re.sub(r"[^A-Za-z0-9_-]", "_", stem)[:64] or "track",
        "name": stem,
        "mood": mood,
        "tags": [stem],
        "file": os.path.abspath(path),
        "url": "",
        "license": "用户自备（商用授权请自行确认）",
        "source": "用户曲库目录",
    }


def load_library() -> dict:
    """读随包清单 + 用户清单 + 用户曲库目录里散放的音频，合成一份曲目表。"""
    allow_hosts: list[str] = []
    by_id: dict[str, dict] = {}
    for lib_file in (LIB_FILE, USER_LIB_FILE):
        data = _read_json(lib_file)
        base = os.path.dirname(lib_file)
        for h in (data.get("allow_hosts") or []):
            h = str(h).strip().lower()
            if h:
                allow_hosts.append(h)
        for raw in (data.get("tracks") or []):
            if not isinstance(raw, dict):
                continue
            tr = _normalize(raw, base)
            if tr:
                by_id[tr["id"]] = tr
    # 用户直接丢进来的音频也能用，不必手写清单
    known = {os.path.normcase(t["file"]) for t in by_id.values() if t.get("file")}
    if os.path.isdir(USER_DIR):
        for fn in sorted(os.listdir(USER_DIR)):
            if not fn.lower().endswith(AUDIO_EXT):
                continue
            full = os.path.abspath(os.path.join(USER_DIR, fn))
            if os.path.normcase(full) in known:
                continue
            tr = _from_filename(full)
            by_id.setdefault(tr["id"], tr)
    return {"tracks": list(by_id.values()), "allow_hosts": sorted(set(allow_hosts))}


def _score(hint: str, tr: dict) -> int:
    score = 0
    for mood, kws in MOOD_RULES.items():
        hits = sum(1 for k in kws if k in hint)
        if hits:
            score += 3 * hits if tr.get("mood") == mood else -hits
    score += 2 * sum(1 for t in tr.get("tags") or [] if t and t in hint)
    return score


def pick(hint: str = "", seed: str = "", used: set[str] | None = None,
         lib: dict | None = None, pick_id: str = "") -> dict:
    """按 AI 的配乐建议挑一首。seed 保证同一套重渲还是同一首，used 让各套尽量不撞曲。"""
    lib = lib or load_library()
    tracks = lib["tracks"]
    if not tracks:
        raise BgmError(
            "自动配乐的曲库里还没有音乐：把可商用的音频放进曲库，"
            "或在出片设置里改选「用我自己的音乐」。")
    if pick_id:
        hit = next((t for t in tracks if t["id"] == pick_id), None)
        if not hit:
            raise BgmError(f"曲库里没有指定的这首音乐（{pick_id}），换一首或清空指定。")
        return hit
    hint = " ".join(str(hint or "").split())
    scored = [(_score(hint, t), t) for t in tracks]
    best = max(s for s, _ in scored)
    group = sorted((t for s, t in scored if s == best), key=lambda t: t["id"])
    h = int(hashlib.sha1(f"{seed}|{hint}".encode("utf-8")).hexdigest()[:12], 16)
    start = h % len(group)
    used = used or set()
    for offset in range(len(group)):
        t = group[(start + offset) % len(group)]
        if t["id"] not in used:
            return t
    return group[start]


def _valid_audio(path: str) -> bool:
    if not path or not os.path.isfile(path) or not path.lower().endswith(AUDIO_EXT):
        return False
    try:
        return bool(core.ffprobe(path).get("duration"))
    except Exception:  # noqa: BLE001 —— ffprobe 读不了就是坏文件
        return False


def _unlink(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _download(url: str, dest: str, allow_hosts: list[str]) -> None:
    """只从清单白名单里的 https 地址下载，逐跳校验 host，落盘后还要过音频体检。"""
    if not allow_hosts:
        raise BgmError(
            "曲库清单还没开放下载地址，自动配乐只使用本地音频；"
            "把音乐放进曲库，或在出片设置里改选「用我自己的音乐」。")
    cur = str(url or "").strip()
    if not cur:
        raise BgmError("这首曲子既没有本地音频，也没配下载地址，暂时用不了，换一首试试。")
    for _ in range(4):
        u = urlparse(cur)
        if u.scheme != "https":
            raise BgmError("自动配乐只从 https 地址下载音乐，清单里的地址不合要求，已拦下。")
        host = (u.hostname or "").lower()
        if not any(host == h or host.endswith("." + h) for h in allow_hosts):
            raise BgmError(f"自动配乐的下载地址不在允许名单里（{host}），已拦下，不会去下载。")
        try:
            r = requests.get(cur, stream=True, timeout=DOWNLOAD_TIMEOUT,
                             allow_redirects=False,
                             headers={"User-Agent": "HookStudio/1.0 (bgm library)"})
        except requests.RequestException as exc:
            raise BgmError(f"自动配乐下载失败：{exc}；检查网络，或改选「用我自己的音乐」。") from exc
        if r.status_code in (301, 302, 303, 307, 308):
            loc = (r.headers.get("Location") or "").strip()
            r.close()
            if not loc:
                raise BgmError("自动配乐的下载地址跳转向空地址，已拦下。")
            cur = urljoin(cur, loc)
            continue
        if r.status_code != 200:
            r.close()
            raise BgmError(f"自动配乐下载失败（HTTP {r.status_code}）；"
                           "检查清单里的地址，或改选「用我自己的音乐」。")
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not (ctype.startswith("audio/")
                or ctype in ("application/octet-stream", "binary/octet-stream")):
            r.close()
            raise BgmError(f"自动配乐下载地址返回的不是音频（{ctype or '未知类型'}），已拦下。")
        try:
            clen = int(r.headers.get("Content-Length") or 0)
        except ValueError:
            clen = 0
        if clen > MAX_BYTES:
            r.close()
            raise BgmError("自动配乐的音乐超过 30MB，已拦下，换一首或压缩后再放进曲库。")
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = dest + ".part"
        total = 0
        try:
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise BgmError("自动配乐的音乐超过 30MB，已拦下。")
                    f.write(chunk)
        except BgmError:
            _unlink(tmp)
            raise
        except OSError as exc:
            _unlink(tmp)
            raise BgmError(f"自动配乐保存音乐失败：{exc}") from exc
        finally:
            r.close()
        if not total:
            _unlink(tmp)
            raise BgmError("自动配乐下载到的是空文件，已拦下。")
        os.replace(tmp, dest)
        return
    raise BgmError("自动配乐的下载地址跳转层数过多，已拦下。")


def ensure_file(tr: dict, lib: dict | None = None) -> str:
    """本地已有就用本地；缺文件才按清单地址下载并缓存，最后一定过音频体检。"""
    lib = lib or load_library()
    candidates: list[str] = []
    if tr.get("file"):
        candidates.append(tr["file"])
    ext = os.path.splitext(tr.get("file") or "")[1].lower()
    if ext not in AUDIO_EXT:
        ext = ".m4a"
    cache = os.path.join(CACHE_DIR, tr["id"] + ext)
    candidates.append(cache)
    for c in candidates:
        if _valid_audio(c):
            return c
    if not tr.get("url"):
        raise BgmError(f"曲库里的「{tr['name']}」还没有音频文件，"
                       "把音乐放进曲库，或在出片设置里改选「用我自己的音乐」。")
    _download(tr["url"], cache, lib["allow_hosts"])
    if not _valid_audio(cache):
        _unlink(cache)
        raise BgmError(f"自动配乐下下来的「{tr['name']}」读不了（不是音频或已损坏），已丢掉，换一首试试。")
    return cache


def resolve(hint: str = "", seed: str = "", used: set[str] | None = None,
            pick_id: str = "") -> dict:
    """给一套片子解析出一个能直接交给渲染层的本地音频路径。"""
    lib = load_library()
    tr = pick(hint, seed, used, lib, pick_id=pick_id)
    return {
        "path": ensure_file(tr, lib),
        "id": tr["id"],
        "name": tr["name"],
        "license": tr["license"],
    }


def library_summary() -> dict:
    """给前端显示曲库状态：不暴露内部路径，只报数量与是否允许联网取曲。"""
    lib = load_library()
    tracks = lib["tracks"]
    ready = 0
    for t in tracks:
        if t.get("file") and os.path.isfile(t["file"]) \
                and t["file"].lower().endswith(AUDIO_EXT):
            ready += 1
        elif os.path.isdir(CACHE_DIR) and any(
                os.path.isfile(os.path.join(CACHE_DIR, t["id"] + e)) for e in AUDIO_EXT):
            ready += 1
    return {"count": len(tracks), "ready": ready,
            "downloadable": bool(lib["allow_hosts"])}
