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


def _rate(v) -> float | None:
    """平台完播率 → 百分比数值（14.3 表示 14.3%）。读不到返回 None。

    平台给的是 0~1 小数；偶尔也见到已经是 0~100 的，做个范围判断。
    绝不当 0 处理：0% 是「真的一条没看完」，None 是「平台没给这个字段」，
    两者混在一起会让新号误以为作品全崩了。
    """
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f < 0:
        return None
    return round(f * 100, 1) if f <= 1 else round(f, 1)


def _seconds(v) -> float | None:
    """平台平均观看秒数 → 秒。读不到返回 None，不补 0。"""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 1) if f >= 0 else None


def _collect(session, url: str, parts: tuple, wait_ms: int, got: dict,
             scroll: bool = False) -> None:
    """打开一个页面，把它自己发的几个接口的 JSON 响应存进 `got`。

    直接读响应、不重放第一页 —— 页面加载时本来就发了，白拿一份。
    监听器必须 remove：这个 page 是长期复用的，挂着不放会一次比一次多。

    scroll=True（作品列表页用）：列表页是懒加载的，首屏只渲染前几条，
    往下滚动才会发新的 post_list 请求。这里滚到底触发加载更多，把多次
    post_list 响应都累积下来，否则永远只拿到第一屏那 4-5 条。
    """
    def _run(page):
        def _on_resp(resp):
            try:
                u = resp.url or ""
                for p in parts:
                    if p not in u:
                        continue
                    body = resp.json()
                    # post_list 要累积多次（懒加载会发好几屏）；其余只取第一条
                    if p == "post/post_list":
                        got.setdefault(p, [])
                        if body not in got[p]:
                            got[p].append(body)
                    elif p not in got:
                        got[p] = body
                    return
            except Exception:  # noqa: BLE001
                # 二进制/非 JSON 响应会抛，监听器一抛就把 sync 通道卡死
                pass

        page.on("response", _on_resp)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(wait_ms)
            if scroll:
                # 滚 6 轮，每轮滚到底 + 等 1.5 秒，让懒加载把全部作品吐出来
                for _ in range(6):
                    try:
                        page.evaluate("""() => {
                            const sc = document.scrollingElement || document.documentElement;
                            sc.scrollTop = sc.scrollHeight;
                            window.scrollTo(0, document.body.scrollHeight);
                        }""")
                    except Exception:  # noqa: BLE001
                        pass
                    page.wait_for_timeout(1500)
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
        # 完播率 / 平均观看时长（2026-11-16 实数据确认：平台 post_list 每条都给）。
        # fullPlayRate 是 0~1 的小数（0.1428＝14.3%），avgPlayTimeSec 是秒。
        # 字段缺版本时存 None —— 界面显示「—」，绝不补 0（0% 和「平台没给」是两回事）。
        "finish_rate": _rate(it.get("fullPlayRate")),
        "avg_play_sec": _seconds(it.get("avgPlayTimeSec")),
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


def _dump_post_list(raw_lists: list) -> None:
    """探测用：把 post_list 的完整原始响应落一份盘，供人工核对字段/翻页。

    只写文件、不影响主流程。看完可删。目标是确认两件事：
      ① 列表页一屏到底返回多少条、有没有 totalCount / 翻页字段；
      ② 单条 item 里有没有完播率 / 平均时长 / 起始播放这些字段。
    """
    try:
        p = os.path.join(core.DATA_DIR, "diag", "post_list_raw.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(raw_lists, f, ensure_ascii=False, indent=1)
    except Exception:  # noqa: BLE001
        pass


def _merge_lists(raw_lists: list) -> list:
    """把多次 post_list 响应里的 item 合并去重，返回完整作品列表。

    懒加载会发好几屏，每屏 data.list 是一段；用 objectId 去重（同一条
    可能在滚动时被重复返回）。
    """
    seen: set = set()
    out: list = []
    for raw in raw_lists or []:
        if not isinstance(raw, dict):
            continue
        data = raw.get("data") or {}
        for it in data.get("list") or []:
            if not isinstance(it, dict):
                continue
            oid = it.get("objectId") or it.get("id") or ""
            if oid and oid in seen:
                continue
            if oid:
                seen.add(oid)
            out.append(it)
    return out


def fetch(session, deep: bool = False) -> dict:
    """采一次原始数据（不落盘）。deep=True 时多开一次首页，补权限/加热那几项。"""
    got: dict = {}
    _collect(session, wxpub.LIST_URL, _WANT_DEEP if deep else _WANT, 9000, got,
             scroll=True)
    if deep and "vip/get-user-member-service-status" not in got:
        _collect(session, wxpub.PLATFORM_URL, _WANT_DEEP, 9000, got)

    raw_lists = got.get("post/post_list") or []
    if isinstance(raw_lists, dict):
        raw_lists = [raw_lists]
    _dump_post_list(raw_lists)
    lst = _merge_lists(raw_lists)
    raw_list = raw_lists[0] if raw_lists else {}
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
    all_notices = []
    for it in lst:
        if not isinstance(it, dict):
            continue
        txt = " ".join(str(it.get(k) or "") for k in
                       ("title", "content", "digest", "desc", "subTitle"))
        # 通知记录：把平台发的每条通知都留下（小v猫「通知记录」口径）。
        # 只有一个概数 unread 没用——客户要能点开逐条看。字段逐个兜底，
        # 平台各版本 title/content 名字不一，全为空就跳过（不造空白行）。
        title = str(it.get("title") or it.get("subTitle") or "").strip()
        text = str(it.get("content") or it.get("digest") or it.get("desc") or "").strip()
        nts = _num(it.get("createTime") or it.get("timestamp"))
        if title or text:
            all_notices.append({"title": title[:60], "text": text[:200], "ts": nts})
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
            "all": all_notices[:50], "ok": bool(lst) or "list" in data}


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
        p["base"] = int(s[0][1]) if s else 0
        rec["last"] = {k: v for k, v in zip(keys, vals)}
        rec["last"].update({
            "title": p["title"], "ts": p["ts"], "dur": p["dur"],
            "cover": p["cover"], "collection": p["collection"],
            "finish_rate": p.get("finish_rate"), "avg_play_sec": p.get("avg_play_sec"),
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
    plays = [p["play"] for p in snap.get("posts") or []]
    snap["summary"] = {
        "count": len(plays),
        "max_play": max(plays) if plays else 0,
        "min_play": min(plays) if plays else 0,
        "total_play": sum(plays),
    }
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
            # 老缓存没有这两个键 → None，界面显示「—」，不伪造 0
            "finish_rate": last.get("finish_rate"), "avg_play_sec": last.get("avg_play_sec"),
            "disabled": bool(last.get("disabled")), "original": _num(last.get("original")),
            "visible": _num(last.get("visible")), "status": _num(last.get("status")),
            "series": [int(x[1]) for x in s][-SHOW_POINTS:],
            "inc": (int(s[-1][1]) - int(s[-2][1])) if len(s) >= 2 else None,
            "span": max(0, s[-1][0] - s[-2][0]) if len(s) >= 2 else 0,
            "points": len(s), "since": s[0][0],
            # 起始播放 = 第一次采样值（小v猫口径）。只有 1 个点时就是它自己。
            "base": int(s[0][1]) if s else 0,
        })
    posts.sort(key=lambda x: x["ts"], reverse=True)
    # 顶部汇总（小v猫式一眼看全）：只在有作品时算，全是 0 也不假装热闹
    plays = [p["play"] for p in posts]
    return {"aid": aid, "at": h.get("at") or "", "account": h.get("account") or {},
            "posts": posts, "points": max((p["points"] for p in posts), default=0),
            "summary": {
                "count": len(posts),
                "max_play": max(plays) if plays else 0,
                "min_play": min(plays) if plays else 0,
                "total_play": sum(plays),
            },
            "abnormal": abnormal(posts, h.get("notify") or {}),
            "notify_unread": _num((h.get("notify") or {}).get("unread")),
            "note": ""}


def notify_records(aid: str = "") -> dict:
    """通知记录：从上次采样的缓存读，不打浏览器（小v猫「通知记录」口径）。

    没采过或缓存里没存通知（老版本）就如实说，不伪造。
    """
    h = _load(aid)
    ntf = h.get("notify") or {}
    return {"aid": aid, "at": h.get("at") or "",
            "unread": _num(ntf.get("unread")),
            "items": list(ntf.get("all") or []),
            "note": "" if ntf.get("ok") or ntf.get("all") is not None
                    else "这个号还没读到通知，点「采一次」后再看。"}


def clear_data(aid: str = "") -> dict:
    """清空这个号的采样记录（小v猫「清空数据」口径）。

    只删这一个号的缓存文件，不碰别的号、不碰队列、不碰成片。
    """
    p = _path(aid)
    removed = False
    try:
        if os.path.isfile(p):
            os.remove(p)
            removed = True
    except OSError:
        pass
    return {"aid": aid, "removed": removed}


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
