"""和服务器（api.hookstudio.cn）打交道的唯一出口。

2026-11-04 之前账号只存在本机 `data/app.db`，于是出现三个绕不过去的问题：

  ① 「第一个注册的人自动是管理员」——每个用户在自己电脑上都是管理员，
     本机那个后台对他毫无意义，运营方也永远看不到他是谁；
  ② 装在 A 电脑的账号，换台电脑就不存在，用户以为「号被盗了」；
  ③ 额度按匿名设备令牌算，封了设备他重装一次就换个新令牌，等于没封。

所以账号搬到服务器：注册、登录、改密都在这里发请求。**本机仍然保留一份用户表**，
但性质变了——它只是服务器账号的本地副本（密码哈希留着是为了断网时能登录）。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from . import core

TIMEOUT = 6.0          # 认证接口不能像排结构那样等几分钟，超时就当没网

# 本机地址一律直连。开发机上常有 127.0.0.1:xxxx 的系统代理（Clash 之类），
# urllib 会老老实实把 127.0.0.1 也转给代理，结果连自己的测试服务都连不上，
# 报出来的还是 502，极难排查。正式域名照旧走用户的代理设置。
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _open(req, timeout=None):
    host = (urllib.parse.urlsplit(req.full_url).hostname or "").lower()
    if host in ("127.0.0.1", "localhost", "::1"):
        return _DIRECT.open(req, timeout=timeout or TIMEOUT)
    return urllib.request.urlopen(req, timeout=timeout or TIMEOUT)


class NetworkError(Exception):
    """连不上服务器：没网、DNS 挂了、超时。调用方据此决定「回退本地校验」。"""


class RemoteError(Exception):
    """服务器明确拒绝了（密码错、被封、用户名占用）。**不能回退本地校验**——
    否则被封的人拔掉网线就能用。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _base() -> str:
    cfg = core.load_config()
    return (cfg.get("proxy_url") or core.PROXY_URL_DEFAULT or "").rstrip("/")


def _post(path: str, payload: dict, token: str = "") -> dict:
    url = _base() + path
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-HS-Token"] = token
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with _open(req) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        msg = f"服务器返回 {exc.code}"
        try:
            err = json.loads(raw or "{}").get("error") or {}
            if err.get("message"):
                msg = str(err["message"])
        except Exception:  # noqa: BLE001
            pass
        raise RemoteError(exc.code, msg) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise NetworkError(str(exc)) from exc


def register(username: str, password: str, device: str) -> dict:
    """注册。成功返回 {token, account}。用户名占用 → RemoteError(409)。"""
    return _post("/api/auth/register",
                 {"username": username, "password": password, "device": device})


def login(username: str, password: str, device: str) -> dict:
    """登录。成功返回 {token, account}。密码错 401 / 被封 403 / 换机 403。"""
    return _post("/api/auth/login",
                 {"username": username, "password": password, "device": device})


def change_password(token: str, old: str, new: str) -> dict:
    """改密。服务器那边也要改，否则下次登录用的还是旧密码。"""
    return _post("/api/auth/password", {"old": old, "new": new}, token=token)


def me(token: str) -> dict:
    """当前账号 + 今日额度。令牌失效抛 RemoteError(401)。"""
    url = _base() + "/api/auth/me"
    req = urllib.request.Request(url, headers={"X-HS-Token": token})
    try:
        with _open(req) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raise RemoteError(exc.code, "登录状态已失效，请重新登录") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise NetworkError(str(exc)) from exc
