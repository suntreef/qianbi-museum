"""极简登录：口令 + HMAC 签名 Cookie。内网单人/小团队够用，不做多用户。"""
import hashlib
import hmac
import time
from typing import Optional

from fastapi import HTTPException, Request

from .config import APP_PASSWORD, SESSION_SECRET

COOKIE = "qb_session"
_TTL = 30 * 86400  # 30 天免登录


def _sign(payload: str) -> str:
    return hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_token() -> str:
    payload = f"auth.{int(time.time()) + _TTL}"
    return f"{payload}.{_sign(payload)}"


def verify_token(token: Optional[str]) -> bool:
    if not token or token.count(".") < 2:
        return False
    payload, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(_sign(payload), sig):
        return False
    try:
        _, exp = payload.split(".")
        return int(exp) > time.time()
    except (ValueError, TypeError):
        return False


def require_auth(request: Request) -> None:
    """API 用：未登录抛 401，前端 JS 捕获后跳登录页。"""
    if not verify_token(request.cookies.get(COOKIE)):
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
