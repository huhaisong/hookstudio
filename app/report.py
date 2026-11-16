"""把本机操作日志增量上报到服务器。

### 为什么必须有（2026-11-04 补）

埋点写的是**本机** `data/app.db`。开发机上你自己就是这个"本机"，后台日志页看得见；
一旦按 exe 分发出去，日志就躺在**用户自己的电脑**里——你在后台一条都看不到，
用户报「我点了没反应」，你既没有现场也没有时间线。这和上一轮「为什么操作日记没有记录」
是同一个问题的另一个版本。

### 规则

  · 只推**增量**：`audit_logs.reported_at` 为空的行，按 id 升序，一次最多 3 批 × 100 条；
  · 推成功才写 `reported_at`，失败原样留着，下次继续（断网几天也不会丢）；
  · 正文已脱敏（`audit.mask` 在落库时就处理过），这里只做字段裁剪：
    本地自增 id、视频路径这类没意义的东西不外发；
  · **默认只在分发模式开**（`dist_mode`）。开发机推自己的日志到生产库既没用也添乱，
    要用 `report_audit: true` 单独打开。

### 失败不打扰用户

上报是旁路：任何异常都吞掉，只在控制台留一行。它永远不能影响转写、出片、登录。
"""
from __future__ import annotations

import os
import threading
import time

from . import buildstamp, core, db

BATCH = 100                 # 单批条数
MAX_BATCH = 3               # 单次最多推几批（长期离线后别一次怼爆服务器）
OK_GAP = 60.0               # 成功后多久再来看一眼
FAIL_GAP = 300.0            # 失败后退避 5 分钟
TIMEOUT = 8

_started = False
_lock = threading.Lock()

# 外发字段白名单：本地 id 不外发（服务器自己生成），其余是排查真正用得上的
_FIELDS = ("created_at", "user_id", "username", "role", "method", "path", "route",
           "query", "status_code", "duration_ms", "ip", "ua", "action", "target",
           "payload", "result", "message", "request_id")


def enabled() -> bool:
    cfg = core.load_config()
    v = cfg.get("report_audit")
    # "" = 没显式设置 → 跟随分发形态（打包版开，开发机关）
    if v is None or v == "":
        return bool(cfg.get("dist_mode"))
    return bool(v)


def _base() -> str:
    cfg = core.load_config()
    return (cfg.get("proxy_url") or core.PROXY_URL_DEFAULT or "").strip().rstrip("/")


def _batch() -> list[dict]:
    with db.db() as c:
        rows = c.execute(
            "SELECT * FROM audit_logs WHERE reported_at = '' ORDER BY id LIMIT ?",
            (BATCH,)).fetchall()
    out = []
    for r in rows:
        d = {k: r[k] for k in _FIELDS if k in r.keys()}
        d["local_id"] = r["id"]
        out.append(d)
    return out


def _mark(ids: list[int]) -> None:
    if not ids:
        return
    stamp = db.now_iso()
    with db.db() as c:
        c.executemany("UPDATE audit_logs SET reported_at = ? WHERE id = ?",
                      [(stamp, i) for i in ids])


def push_once() -> int:
    """推一轮。返回成功条数（0 表示没东西可推或失败）。"""
    if not enabled():
        return 0
    base = _base()
    if not base:
        return 0
    import requests

    total = 0
    for _ in range(MAX_BATCH):
        items = _batch()
        if not items:
            break
        who = core.account_name() or core.device_token()   # 后台按账号看日志，比一串 uuid 有用
        body = {
            "device": who,
            "app": buildstamp.VERSION,
            "os": "windows" if os.name == "nt" else os.name,
            "items": [{k: v for k, v in it.items() if k != "local_id"} for it in items],
        }
        r = requests.post(f"{base}/api/audit/ingest", json=body, timeout=TIMEOUT,
                          headers={"X-HS-Token": who})
        if r.status_code not in (200, 201, 204):
            raise RuntimeError(f"上报被拒：HTTP {r.status_code} {r.text[:120]}")
        _mark([it["local_id"] for it in items])
        total += len(items)
        if len(items) < BATCH:
            break
    return total


def _loop() -> None:
    time.sleep(20)          # 让启动流程先跑完（建库、迁移、预热）
    while True:
        gap = OK_GAP
        try:
            n = push_once()
            if n:
                print(f"[report] 已上报 {n} 条操作日志", flush=True)
        except Exception as exc:  # noqa: BLE001
            gap = FAIL_GAP
            print(f"[report] 上报失败（下次重试）：{exc}", flush=True)
        time.sleep(gap)


def start() -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, name="audit-report", daemon=True).start()
