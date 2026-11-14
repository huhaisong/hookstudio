"""用户 / 令牌 / 操作日志的 SQLite 存储层。

为什么单开一个 sqlite 库，不塞进 project.json：
  · 用户、令牌、操作日志是**跨项目**的全局数据，没有 pid 归属；
  · 日志是写多读少、要按时间/用户/动作/状态码过滤，一天几百条、一个月上万条，
    用 JSON 文件每次全量读进来过滤，几十万条就会把内存和响应时间吃穿；
  · sqlite3 是标准库，零依赖、零安装、打包不加体积，单文件 `data/app.db` 拷走就能搬。

对外只暴露 `conn()` 上下文管理器和几个小工具，业务层不用碰 sqlite3 细节。

注意：本文件只负责「建表 + 连接 + 轻量迁移」，**不写业务 SQL**。
业务查询留在 auth.py / admin.py / audit.py 里，方便各自演进。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import core

DB_FILE = os.path.join(core.DATA_DIR, "app.db")

# ── 表结构 ────────────────────────────────────────────────────────────────
# 三条约定，别改：
#   ① 时间统一存 UTC ISO 字符串（`2026-11-04T08:00:00+00:00`）。字符串字典序
#     正好等于时间序，`WHERE created_at >= ?` 可以直接走索引；前端用
#      `new Date(iso)` 转本地时区显示。混用本地时间会让跨时区部署的统计全错。
#   ② 软状态一律用 TEXT + CHECK，不用 0/1 魔法数字——日志/后台里直接看得懂。
#   ③ 用得到的外键都声明 ON DELETE CASCADE，删用户自动清令牌。
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL COLLATE NOCASE UNIQUE,
    email         TEXT    COLLATE NOCASE,
    phone         TEXT,
    password_hash TEXT    NOT NULL,
    display_name  TEXT    NOT NULL DEFAULT '',
    avatar        TEXT    NOT NULL DEFAULT '',
    role          TEXT    NOT NULL DEFAULT 'user'   CHECK (role IN ('user', 'admin')),
    status        TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'banned')),
    ban_reason    TEXT    NOT NULL DEFAULT '',
    banned_at     TEXT,
    banned_by     INTEGER,
    -- 令牌代次：改密 / 封禁 / 解封 / 踢下线时 +1。JWT 里带着签发时的版本号，
    -- 跟库里对不上就立即失效——这是「封禁后旧 token 立刻作废」的实现方式，
    -- 不需要维护黑名单，也不依赖内存（重启后照样生效）。
    token_version INTEGER NOT NULL DEFAULT 0,
    -- 登录失败节流：连续失败 5 次锁 10 分钟，防止弱口令被在线爆破
    fail_count    INTEGER NOT NULL DEFAULT 0,
    locked_until  TEXT,
    -- 首次初始化的管理员 / 被管理员重置密码后置 1，前端提示改密（不强制拦）
    must_change_pwd INTEGER NOT NULL DEFAULT 0,
    last_login_at TEXT,
    last_login_ip TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- 只存 sha256，不存明文：库文件泄露也拿不到能用的 refresh token
    token_hash     TEXT    NOT NULL UNIQUE,
    issued_at      TEXT    NOT NULL,
    expires_at     TEXT    NOT NULL,
    revoked_at     TEXT,
    revoked_reason TEXT    NOT NULL DEFAULT '',
    ip             TEXT    NOT NULL DEFAULT '',
    ua             TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT    NOT NULL,
    user_id     INTEGER,
    username    TEXT    NOT NULL DEFAULT '',
    role        TEXT    NOT NULL DEFAULT '',
    method      TEXT    NOT NULL DEFAULT '',
    path        TEXT    NOT NULL DEFAULT '',
    -- 归一化路由：/api/projects/abc123/plan → /api/projects/{id}/plan
    -- 按它聚合统计才有意义（否则每个项目一条，永远统计不出热点）
    route       TEXT    NOT NULL DEFAULT '',
    query       TEXT    NOT NULL DEFAULT '',
    status_code INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    ip          TEXT    NOT NULL DEFAULT '',
    ua          TEXT    NOT NULL DEFAULT '',
    -- action 是**业务动作名**（auth.login / admin.user.ban / project.create），
    -- 中间件按「方法 + 路由」推不出来时留空，由业务代码手动埋点补上
    action      TEXT    NOT NULL DEFAULT '',
    target      TEXT    NOT NULL DEFAULT '',
    payload     TEXT    NOT NULL DEFAULT '',
    result      TEXT    NOT NULL DEFAULT '',
    message     TEXT    NOT NULL DEFAULT '',
    request_id  TEXT    NOT NULL DEFAULT '',
    -- 已上报到服务器的时刻（空 = 还没推）。exe 分发时日志在本机生成，
    -- 由 app/report.py 增量推到云端，否则开发者在后台一条都看不到。
    reported_at TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_user    ON audit_logs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action  ON audit_logs(action, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_route   ON audit_logs(route, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_status  ON audit_logs(status_code, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rt_user       ON refresh_tokens(user_id, expires_at DESC);
CREATE INDEX IF NOT EXISTS idx_rt_expires    ON refresh_tokens(expires_at);
"""


def now_iso() -> str:
    """当前 UTC 时间（秒级），全库统一用这一个来源。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn() -> sqlite3.Connection:
    """新开一个连接。调用方负责 close（用下面的 with 更省心）。

    没做连接池：本机单机工具，QPS 个位数，sqlite 打开连接 <1ms，
    池子带来的生命周期/线程安全复杂度不值得。
    """
    os.makedirs(core.DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_FILE, timeout=10.0)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    # WAL：读写不互相阻塞（出片时后台在写日志、前端同时在查列表）
    c.execute("PRAGMA synchronous = NORMAL")
    return c


@contextmanager
def db():
    """`with db() as c:` —— 正常走 commit，异常回滚，退出必关连接。"""
    c = conn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _columns(c: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}


def _ensure_column(c: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    """轻量迁移：老库缺列就补上。

    不引 alembic 之类迁移框架——这个库只有三张表、全部是新增列、
    没有改列类型/改约束的场景，一行 PRAGMA 判断足够。
    """
    if name not in _columns(c, table):
        c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init() -> None:
    """建表 + 补列。幂等，启动时调一次；测试里也可以反复调。"""
    first = not os.path.isfile(DB_FILE)
    c = conn()
    try:
        c.execute("PRAGMA journal_mode = WAL")   # 持久设置，只需一次
        c.executescript(SCHEMA)
        # 以后加字段就写在这里，老库启动即升级
        _ensure_column(c, "users", "display_name", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(c, "users", "avatar", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(c, "users", "token_version", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(c, "users", "fail_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(c, "users", "locked_until", "TEXT")
        _ensure_column(c, "users", "must_change_pwd", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(c, "audit_logs", "target", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(c, "audit_logs", "request_id", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(c, "audit_logs", "reported_at", "TEXT NOT NULL DEFAULT ''")
        # 这个索引必须等列补齐后再建：老库 execute(SCHEMA) 时还没有 reported_at 列，
        # 把 CREATE INDEX 写在 SCHEMA 里会直接报 "no such column"。
        c.execute("CREATE INDEX IF NOT EXISTS idx_audit_report "
                  "ON audit_logs(reported_at, id)")
        c.commit()
    finally:
        c.close()
    if first:
        print(f"[db] 初始化用户库：{DB_FILE}")


def rowdict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def rowslist(rows) -> list[dict]:
    return [dict(r) for r in rows]


# 给「按时间倒序 + 分页」用的小工具：所有列表接口都走同一套，翻页语义一致
def page_args(page: int = 1, size: int = 20) -> tuple[int, int]:
    page = max(1, int(page or 1))
    size = min(200, max(1, int(size or 20)))
    return size, (page - 1) * size
