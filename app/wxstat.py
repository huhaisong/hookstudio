"""视频号「数据」：作品列表、账号概览、播放流速。

分工：`wxpub.py` 管发表页（怎么填进去），本文件管数据页（怎么读回来）。
数据一律走「拦页面自己发的请求 → 读响应」，绝不自己伪造鉴权：视频号那几个接口
要 `_log_finder_id` + `x-wechat-uin` + referer，造错一个就给你一个空列表。

「播放流速」不是平台给的字段 —— 平台只给累计播放量。要算流速，只能自己隔一段时间
采一次、把两次的差画成柱。所以它依赖**程序开着**，关了就有断点（`samples` 里能看到）。

为什么单独一个文件：拉数据要开浏览器、一次 8-15 秒，和发表页那套（要填、要核对）
节奏完全不同。混在一起以后改版会互相牵。
"""
from __future__ import annotations

import datetime
import json
import os
import re
import time

from . import core, wxpub

# 旧版是全号共用一份缓存：发布页选 A、数据页显示的却可能是最后采过的 B。
# 2026-11-13 改成按号分文件。老文件不迁移（不知道当初是哪个号），
# 第一次打开会显示「这个号还没采过」—— 采一次即可，不会串号。
POSTS_FILE = os.path.join(core.DATA_DIR, "publish_stats.json")   # 只读遗留，不再写
STATS_DIR = os.path.join(core.DATA_DIR, "publish_stats")


def _path(aid: str) -> str:
    """一个号一个文件。aid 只留安全字符，绝不让账号 id 拼出 `../`。"""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(aid or "unknown"))
    return os.path.join(STATS_DIR, "stats_" + safe + ".json")
KEEP_HOURS = 48          # 多久以前的采样点可以丢
# 采样最密 3 分钟（用户 2026-11-13 定）。SPACING 必须小于它，否则 3 分钟的点会被
# 「4 分钟内重复采样不新增点」吃掉，界面写每 3 分钟、实际还是 4 分钟一个点。
SAMPLE_MAX = KEEP_HOURS * 20  # 3 分钟一个点 → 48 小时 960 个
SPACING = 150            # 2.5 分钟内重复采样只更新最后一个点，不新增
SHOW_POINTS = 120        # 前端柱带只画最近 120 个点（3 分钟 × 120 = 6 小时）

# 作品列表页一次性就会发这些；deep 时再补权限/加热相关的几项
_WANT = ("post/post_list", "auth/auth_data", "notification/notification_list")
_WANT_DEEP = _WANT + (
    "vip/get-user-member-service-status",
    "active-auth/is-in-finder-whitelist",
    "auth/mp_finder_window_init",
)


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M")


def _num(v, default: int = 0) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return default


def _collect(session, url: str, parts: tuple, wait_ms: int, got: dict) -> None:
    """打开一个页面，把它自己发的几个接口的 JSON 响应存进 `got`。

    直接读响应、不重放第一页 —— 页面加载时本来就发了，白拿一份。
    监听器必须 remove：这个 page 是长期复用的，挂着不放会一次比一次多。
    """
    def _run(page):
        def _on_resp(resp):
            try:
                u = resp.url or ""
                for p in parts:
                    if p in u and p not in got:
                        got[p] = resp.json()
                        return
            except Exception:  # noqa: BLE001
                # 二进制/非 JSON 响应会抛，监听器一抛就把 sync 通道卡死
                pass

        page.on("response", _on_resp)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(wait_ms)
        finally:
            try:
                page.remove_listener("response", _on_resp)
            except Exception:  # noqa: BLE001
                pass

    session.run(_run, timeout=200)


def _norm_post(it: dict) -> dict:
    """一条作品 → 我们要的字段。字段名按 2026-11-13 实测的 post_list 返回写。"""
    d = it.get("desc") or {}
    media = (d.get("media") or [{}])[0] or {}
    st = d.get("shortTitle") or []
    title = ((st[0] or {}).get("shortTitle") if st else "") or d.get("description") or ""
    comp = d.get("component") or {}
    loc = d.get("location") or {}
    dis = it.get("disableInfo") or {}
    return {
        "id": it.get("objectId") or "",
        "ts": _num(it.get("createTime")),
        "live_at": _num(it.get("effectiveTime")) or _num(it.get("createTime")),
        "title": str(title).strip(),
        "play": _num(it.get("readCount")),
        "like": _num(it.get("likeCount")),
        "comment": _num(it.get("commentCount")),
        "forward": _num(it.get("forwardCount")),
        "fav": _num(it.get("favCount")),
        "dur": _num(media.get("videoPlayLen")),
        "cover": media.get("thumbUrl") or media.get("coverUrl") or "",
        "collection": (comp.get("title") or "").strip(),
        "city": (loc.get("city") or "").strip(),
        # 注意：这个是平台的「功能级禁用」（比如禁止下载/禁止转载），**不是违规下架**。
        # 2026-11-13 实测：首页能正常看的作品这里也可能是 true，拿它判违规会全员误报。
        # 真正的违规只能从通知中心的推送里看（见 notify）。
        "disabled": bool(dis.get("isDisabled")),
        "status": _num(it.get("status")),
        "visible": _num(it.get("visibleType")),
        "original": _num((it.get("originalInfo") or {}).get("isDeclared")),
    }


def _norm_acc(data: dict) -> dict:
    fu = data.get("finderUser") or {}
    tal = data.get("talentItem") or {}
    return {
        "nick": (fu.get("nickname") or "").strip(),
        "avatar": fu.get("headImgUrl") or "",
        "fans": _num(fu.get("fansCount")),
        "feeds": _num(fu.get("feedsCount")),
        # uniqId 就是视频号 ID（界面上「ID sph...」那一串）。finderUsername 是内部长串，
        # 用户认的是 uniqId —— 2026-11-13 实测两者同时返回，别拿错。
        "finder_id": (fu.get("uniqId") or "").strip(),
        "uid": fu.get("uniqId") or "",
        "admin_nick": (fu.get("adminNickname") or "").strip(),
        "spam": _num(fu.get("spamFlag")),
        "master": bool(fu.get("isMasterFinder")),
        "acc_status": _num(tal.get("accountStatus"), -1),
    }


def fetch(session, deep: bool = False) -> dict:
    """采一次原始数据（不落盘）。deep=True 时多开一次首页，补权限/加热那几项。"""
    got: dict = {}
    _collect(session, wxpub.LIST_URL, _WANT_DEEP if deep else _WANT, 9000, got)
    if deep and "vip/get-user-member-service-status" not in got:
        _collect(session, wxpub.PLATFORM_URL, _WANT_DEEP, 9000, got)

    raw_list = got.get("post/post_list") or {}
    lst = ((raw_list.get("data") or {}).get("list")) or []
    auth = ((got.get("auth/auth_data") or {}).get("data")) or {}
    posts = [_norm_post(x) for x in lst if isinstance(x, dict)]
    acc = _norm_acc(auth)

    vip = (((got.get("vip/get-user-member-service-status") or {}).get("data")) or {})
    wl = (((got.get("active-auth/is-in-finder-whitelist") or {}).get("data")) or {})
    win = (((got.get("auth/mp_finder_window_init") or {}).get("data")) or {})
    ntf = (((got.get("notification/notification_list") or {}).get("data")) or {})

    note = ""
    if not posts and not acc.get("nick"):
        note = "没读到作品列表。这个号可能掉登录了，去②区点「重登」扫一下。"
    return {
        "ok": bool(posts or acc.get("nick")),
        "at": _now(),
        "account": acc,
        "posts": posts,
        "total": _num((raw_list.get("data") or {}).get("totalCount"), len(posts)),
        "notify": _norm_notify(ntf),
        "heat": {
            "open": _num(vip.get("openStatus"), -1),
            "conds": vip.get("conditionList") or [],
            "white": wl.get("isInWhiteList"),
            "acc_status": _num((win.get("talentItem") or {}).get("accountStatus"), -1),
        },
        "note": note,
    }


def _norm_notify(data: dict) -> dict:
    """通知中心 → 违规/处罚类提醒。

    违规作品只能从这里看：作品列表里的 `disableInfo.isDisabled` 是功能级开关，
    真作品违规时它不一定亮（实测首页正常的作品也有 true）。

    顺手把能指到具体作品的 id 留下来（objectId 一类的字段名各版本不一样，
    多试几个），这样「近期异常视频」才能把通知落到某一条作品上。
    """
    lst = data.get("list") or []
    hits = []
    for it in lst:
        if not isinstance(it, dict):
            continue
        txt = " ".join(str(it.get(k) or "") for k in
                       ("title", "content", "digest", "desc", "subTitle"))
        if not any(k in txt for k in ("违规", "处罚", "限制", "下架", "申诉", "不通过")):
            continue
        raw = it.get("raw") if isinstance(it.get("raw"), dict) else it
        oid = ""
        for k in ("objectId", "object_id", "feedId", "feed_id", "exportId"):
            v = raw.get(k) if isinstance(raw, dict) else ""
            if v:
                oid = str(v)
                break
        hits.append({"text": txt.strip()[:120],
                     "title": str(it.get("title") or "").strip()[:60],
                     "oid": oid,
                     "ts": _num(it.get("createTime") or it.get("timestamp"))})
    return {"unread": _num(data.get("unreadCount")), "hits": hits[:20],
            "ok": bool(lst) or "list" in data}


# visibleType：1 公开、2 仅好友可见、3 仅自己可见（2026-11-13 与列表页文案对齐核对过）
_VISIBLE_TEXT = {2: "仅好友可见", 3: "仅自己可见"}


def abnormal(posts: list[dict], notify: dict) -> list[dict]:
    """近期异常作品：只放**真的读到了信号**的，一条不猜。

    两类来源，界面上分得清：
      ① `notify` —— 平台通知里点名到作品的（违规/下架/不通过），处置说明写通知原文；
      ② `visible ≠ 1` —— 作品不在公域可见。这是平台给的状态，不是我们判断的违规，
         所以只写「仅自己可见 · 没在公域推」，绝不写成「推荐量减少」那种平台没说过的结论。

    为什么不做第三种「根据播放量低推测被限流」：低播放的正常作品占大多数，
    列出来等于给客户一堆假警报，他拿着去申诉反而对不上平台的处置记录。
    """
    out: list[dict] = []
    seen: set = set()

    def _row(p, kind, why, notice=""):
        return {"id": p["id"], "title": p["title"], "cover": p["cover"],
                "ts": p["ts"], "play": p["play"], "dur": p["dur"],
                "kind": kind, "why": why, "notice": notice}

    for h in (notify or {}).get("hits") or []:
        oid = h.get("oid") or ""
        hit = next((p for p in posts if oid and p["id"] == oid), None)
        if hit is None and h.get("title"):
            t = h["title"]
            hit = next((p for p in posts if p["title"] and p["title"][:8] in t), None)
        if hit is None or hit["id"] in seen:
            # 对不上作品的照样要显示：客户需要知道平台发了这条通知
            out.append({"id": "", "title": h.get("title") or "（平台通知）",
                        "cover": "", "ts": h.get("ts") or 0, "play": 0, "dur": 0,
                        "kind": "notify", "why": "平台通知", "notice": h.get("text") or ""})
            continue
        seen.add(hit["id"])
        out.append(_row(hit, "notify", "平台通知", h.get("text") or ""))
        out[-1]["notice"] = h.get("text") or ""

    for p in posts:
        if p["id"] in seen:
            continue
        vt = p.get("visible")
        if vt in _VISIBLE_TEXT:
            out.append(_row(p, "hidden", _VISIBLE_TEXT[vt]))
            seen.add(p["id"])

    out.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    return out[:30]


# ── 采样与流速 ──────────────────────────────────────────────────────────
def _load(aid: str = "") -> dict:
    p = _path(aid)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            d.setdefault("posts", {})
            return d
    except Exception:  # noqa: BLE001
        pass
    return {"posts": {}}


def _save(h: dict, aid: str = "") -> None:
    p = _path(aid)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False)
    os.replace(tmp, p)


def _attach(posts: list[dict], hist: dict) -> None:
    """把这次的播放量接到历史序列上，顺便算出「这一格涨了多少」。

    柱带的每一根柱子 = 相邻两次采样的差。差值只有在**两次采样之间**才有意义，
    所以第一次采完不可能有流速，前端要显示成「—」而不是 0。
    """
    now = int(time.time())
    keep = {}
    for p in posts:
        rec = keep.setdefault(p["id"], hist["posts"].get(p["id"]) or {"samples": []})
        rec["samples"] = [list(x) for x in (rec.get("samples") or []) if isinstance(x, (list, tuple))]

        vals = [p["play"], p["like"], p["comment"], p["forward"], p["fav"]]
        keys = ("play", "like", "comment", "forward", "fav")
        s = rec["samples"]
        if not s or now - s[-1][0] >= SPACING:
            s.append([now, p["play"]])
        else:
            s[-1][1] = p["play"]
        if len(s) > SAMPLE_MAX:
            del s[: len(s) - SAMPLE_MAX]

        p["series"] = [int(x[1]) for x in s][-SHOW_POINTS:]
        p["inc"] = (int(s[-1][1]) - int(s[-2][1])) if len(s) >= 2 else None
        p["span"] = max(0, s[-1][0] - s[-2][0]) if len(s) >= 2 else 0
        p["since"] = s[0][0] if s else now
        p["points"] = len(s)
        rec["last"] = {k: v for k, v in zip(keys, vals)}
        rec["last"].update({
            "title": p["title"], "ts": p["ts"], "dur": p["dur"],
            "cover": p["cover"], "collection": p["collection"],
            "disabled": p["disabled"], "original": p["original"],
            # visible 要落盘：诊断页的「近期异常视频」读的是缓存，
            # 不存的话每次都得重新开浏览器才知道哪条没在公域推
            "visible": p["visible"], "status": p["status"],
        })
    hist["posts"] = keep


def sample(session, deep: bool = False, aid: str = "") -> dict:
    """采一次并落盘（按号分文件），返回带增量的视图。

    aid 必须由调用方从「当前确认的账号」传进来，不许自己挑一个在线号：
    采错号的数据比没有数据更危险（2026-11-13 用户要求按号隔离）。
    """
    snap = fetch(session, deep=deep)
    hist = _load(aid)
    hist.setdefault("posts", {})
    if snap.get("ok"):
        _attach(snap["posts"], hist)
        hist["at"] = snap["at"]
        hist["account"] = snap["account"]
        hist["notify"] = snap.get("notify") or {}
        _save(hist, aid)
    snap["aid"] = aid
    snap["points"] = max((p.get("points") or 0) for p in snap.get("posts") or [{}]) if snap.get("posts") else 0
    snap["abnormal"] = abnormal(snap.get("posts") or [], snap.get("notify") or {})
    return snap


def cached(aid: str = "") -> dict:
    """这个号上次采到的数据（不打浏览器，毫秒级）。没采过就返回空壳。"""
    h = _load(aid)
    if not h.get("at"):
        return {"aid": aid, "at": "", "account": {}, "posts": [], "points": 0,
                "abnormal": [], "note": "这个号还没采过。点「采一次」去号里读一遍。"}
    posts = []
    for pid, rec in (h.get("posts") or {}).items():
        s = rec.get("samples") or []
        if not s:
            continue
        last = rec.get("last") or {}
        posts.append({
            "id": pid, "ts": _num(last.get("ts")),
            "title": last.get("title") or "",
            "play": _num(last.get("play")), "like": _num(last.get("like")),
            "comment": _num(last.get("comment")), "forward": _num(last.get("forward")),
            "fav": _num(last.get("fav")),
            "dur": _num(last.get("dur")), "cover": last.get("cover") or "",
            "collection": last.get("collection") or "",
            "disabled": bool(last.get("disabled")), "original": _num(last.get("original")),
            "visible": _num(last.get("visible")), "status": _num(last.get("status")),
            "series": [int(x[1]) for x in s][-SHOW_POINTS:],
            "inc": (int(s[-1][1]) - int(s[-2][1])) if len(s) >= 2 else None,
            "span": max(0, s[-1][0] - s[-2][0]) if len(s) >= 2 else 0,
            "points": len(s), "since": s[0][0],
        })
    posts.sort(key=lambda x: x["ts"], reverse=True)
    return {"aid": aid, "at": h.get("at") or "", "account": h.get("account") or {},
            "posts": posts, "points": max((p["points"] for p in posts), default=0),
            "abnormal": abnormal(posts, h.get("notify") or {}),
            "note": ""}


def profile(session) -> dict:
    """只读账号资料：视频号名称 + 视频号 ID + 头像 + 粉丝数。

    为什么要单独一个（而不是复用 `fetch`）：`fetch` 要开作品列表页、等 9 秒，
    只为拿个名字太贵。这里开首页，页面自己会发 `auth/auth_data`，拦下来就够。
    登录成功后跑一次，把 ID 写进账号记录 —— 界面上要显示「ID sph...」。
    """
    got: dict = {}
    for url in (wxpub.PLATFORM_URL, wxpub.LIST_URL):
        _collect(session, url, ("auth/auth_data",), 5000, got)
        if "auth/auth_data" in got:
            break
    auth = ((got.get("auth/auth_data") or {}).get("data")) or {}
    return _norm_acc(auth)
