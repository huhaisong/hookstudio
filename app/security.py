"""密码哈希、JWT、令牌哈希 —— 认证层的最小安全原语。

设计取舍：
  · **密码用 bcrypt**（bcrypt 5.0.0 已在运行时里，纯 C 扩展，零配置）。
    不引 passlib：passlib 1.7 与 bcrypt 4+ 有兼容告警，且它只是 bcrypt 的
    一层包装，这里用不到它的多算法迁移能力。
  · **JWT 自己实现 HS256**，不引 pyjwt/python-jose：
    标准 JWT 就是 `b64url(header).b64url(payload).b64url(HMAC-SHA256(...))`，
    40 行能写对且可审计；打包时不带第三方依赖，PyInstaller 少一个变量。
    如果以后要 RS256/多签发方，换 pyjwt 只需替换这两个函数（接口保持一致）。
  · **refresh token 不用 JWT**：它是随机串，服务端存 sha256。这样能真正做到
    「服务端可撤销、可轮换、可查设备」——JWT 一旦签出去就没法收回。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

import bcrypt

# bcrypt 的算法上限：超过 72 字节的部分会被**静默截断**，等于前 72 字节相同的
# 两个长密码可以互相登录。所以注册时直接拒掉超长密码，不做预哈希（预哈希会让
# 哈希串失去「bcrypt 标准格式」，换语言重写时不好迁移）。
PWD_MIN = 8
PWD_MAX = 72
ROUNDS = int(os.environ.get("BCRYPT_ROUNDS") or 12)

# 弱口令黑名单（前 30 名里最常被撞库的那些）。不追求全，挡住「12345678」这类就行
WEAK = {
    "12345678", "123456789", "1234567890", "password", "password1", "qwerty123",
    "abc123456", "a1234567", "11111111", "00000000", "88888888", "admin123",
    "admin123456", "iloveyou", "123123123", "1qaz2wsx", "qwertyui",
    "woaini1314", "5201314520", "asd123456",
}


# ── 密码 ──────────────────────────────────────────────────────────────────
def hash_password(pwd: str) -> str:
    """bcrypt 哈希。每次调用自带随机 salt，同一密码两次结果不同。"""
    return bcrypt.hashpw(pwd.encode("utf-8"), bcrypt.gensalt(rounds=ROUNDS)).decode("ascii")


def verify_password(pwd: str, hashed: str) -> bool:
    """校验密码。

    任何异常都返回 False：哈希串损坏、空串、非 bcrypt 格式都不该让登录接口 500
    （库被手改过、或历史遗留了明文密码时，表现应该是「密码不对」而不是「服务器崩了」）。
    """
    if not pwd or not hashed:
        return False
    try:
        return bcrypt.checkpw(pwd.encode("utf-8"), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


def check_password_strength(pwd: str) -> str | None:
    """返回错误说明；合格返回 None。规则刻意保守——本机/内部工具，别把人挡在门外。"""
    if not isinstance(pwd, str):
        return "密码格式不对"
    n = len(pwd.encode("utf-8"))
    if n < PWD_MIN:
        return f"密码至少 {PWD_MIN} 位"
    if n > PWD_MAX:
        return f"密码最多 {PWD_MAX} 字节（bcrypt 上限，中文约 24 个字）"
    if pwd.strip() != pwd:
        return "密码首尾不能是空格"
    if pwd.lower() in WEAK:
        return "这个密码太常见了，换一个"
    if pwd.isdigit():
        return "密码不能是纯数字"
    if len(set(pwd)) == 1:
        return "密码不能是同一个字符重复"
    return None


# ── JWT（HS256）────────────────────────────────────────────────────────────
class TokenError(Exception):
    """令牌不合法。调用方统一转 401，不区分「格式错」「过期」「签名不对」，
    避免给爆破者反馈（细节写服务端日志即可）。"""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def jwt_encode(payload: dict, secret: str) -> str:
    """签发 HS256 JWT。payload 里必须已经有 exp。"""
    header = {"alg": "HS256", "typ": "JWT"}
    seg = [
        _b64e(json.dumps(header, separators=(",", ":"), sort_keys=True).encode()),
        _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()),
    ]
    signing_input = ".".join(seg).encode("ascii")
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return ".".join(seg + [_b64e(sig)])


def jwt_decode(token: str, secret: str, leeway: int = 0) -> dict:
    """校验并解出 payload。

    三道检查缺一不可：
      ① 结构必须是三段（少一段就是畸形）；
      ② **alg 必须是 HS256**——不校验 alg 的实现能被 `{"alg":"none"}` 直接绕过
        （JWT 最经典的历史漏洞）；
      ③ 签名用 hmac.compare_digest 比，避免逐字节比较的时序侧信道。
    exp/iat 之外的时间字段由调用方按需校验。
    """
    if not isinstance(token, str) or token.count(".") != 2:
        raise TokenError("malformed")
    h_b64, p_b64, s_b64 = token.split(".")
    try:
        header = json.loads(_b64d(h_b64))
        payload = json.loads(_b64d(p_b64))
        sig = _b64d(s_b64)
    except Exception as exc:  # noqa: BLE001
        raise TokenError("decode") from exc
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        raise TokenError("alg")
    expect = hmac.new(secret.encode("utf-8"),
                      f"{h_b64}.{p_b64}".encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expect):
        raise TokenError("signature")
    if not isinstance(payload, dict):
        raise TokenError("payload")
    now = int(time.time())
    exp = payload.get("exp")
    if not isinstance(exp, int):
        raise TokenError("no-exp")
    if now > exp + max(0, leeway):
        raise TokenError("expired")
    nbf = payload.get("nbf")
    if isinstance(nbf, int) and now + leeway < nbf:
        raise TokenError("not-yet")
    return payload


def make_access_token(secret: str, user: dict, ttl_min: int) -> tuple[str, int]:
    """access token + 有效秒数。带上 tv（token_version），供中间件比对失效。"""
    now = int(time.time())
    ttl = max(60, int(ttl_min * 60))
    payload = {
        "sub": int(user["id"]),
        "uname": user["username"],
        "role": user["role"],
        "tv": int(user.get("token_version") or 0),
        "typ": "access",
        "iat": now,
        "nbf": now,
        "exp": now + ttl,
        "jti": secrets.token_hex(8),
    }
    return jwt_encode(payload, secret), ttl


# ── refresh token ─────────────────────────────────────────────────────────
def new_refresh_token() -> str:
    """256 位随机串。不做成 JWT：它需要服务端可撤销、可轮换、可查设备。"""
    return secrets.token_urlsafe(48)


def sha256(text: str) -> str:
    """令牌/敏感串的落库格式。加固定盐没有意义（随机串本身熵足够），
    但**必须**哈希存储——库文件泄露时明文 token 等于直接把账号送人。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_secret() -> str:
    """JWT 签名密钥：首次启动生成 48 字节随机串，存 config，之后不再变。"""
    return secrets.token_urlsafe(48)
