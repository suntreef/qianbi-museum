"""多用户登录：pbkdf2 口令哈希、HMAC 签名 Cookie、失败锁定、角色权限。

角色：admin（全部权限）> staff（录入，看不到价格/估值）> viewer（只读，看不到价格）。
首次启动自动用 APP_PASSWORD 创建管理员账号 admin。
"""
import hashlib
import hmac
import secrets
import time
from typing import Optional

from fastapi import HTTPException, Request

from .config import APP_PASSWORD, SESSION_SECRET
from .models import get_conn

COOKIE = "qb_session"
_TTL = 30 * 86400          # 30 天免登录
_MAX_FAILS = 5             # 连续失败 5 次
_LOCK_SECONDS = 600        # 锁定 10 分钟
_fails: dict = {}          # ip -> [失败次数, 锁定截止时间]

ROLES = ("admin", "staff", "viewer")


def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120_000).hex()
    return f"{salt}${dk}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120_000).hex()
    return hmac.compare_digest(dk, h)


def _sign(payload: str) -> str:
    return hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_token(user_id: int) -> str:
    payload = f"{user_id}.{int(time.time()) + _TTL}"
    return f"{payload}.{_sign(payload)}"


def _uid_from(token: Optional[str]) -> Optional[int]:
    if not token or token.count(".") < 2:
        return None
    payload, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(_sign(payload), sig):
        return None
    try:
        uid, exp = payload.split(".")
        if int(exp) < time.time():
            return None
        return int(uid)
    except (ValueError, TypeError):
        return None


def login_user(ip: str, username: str, password: str) -> Optional[dict]:
    """登录成功返回用户信息；失败返回 None（连续失败触发 IP 临时锁定）。"""
    count, locked_until = _fails.get(ip, [0, 0])
    if locked_until > time.time():
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, username, role, password_hash FROM users "
            "WHERE username=? AND disabled=0", (username,)).fetchone()
    if row and verify_password(password, row["password_hash"]):
        _fails.pop(ip, None)
        return {"id": row["id"], "username": row["username"], "role": row["role"]}
    count += 1
    _fails[ip] = [count, time.time() + _LOCK_SECONDS if count >= _MAX_FAILS else 0]
    return None


def current_user(request: Request) -> Optional[dict]:
    uid = _uid_from(request.cookies.get(COOKIE))
    if uid is None:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, username, role, disabled FROM users WHERE id=?", (uid,)).fetchone()
    if not row or row["disabled"]:
        return None
    return {"id": row["id"], "username": row["username"], "role": row["role"]}


def require_role(*roles):
    """API 依赖：校验登录与角色，返回当前用户。不传 roles = 只要登录即可。"""
    def dep(request: Request) -> dict:
        user = current_user(request)
        if not user:
            raise HTTPException(status_code=401, detail="未登录或登录已过期")
        if roles and user["role"] not in roles:
            raise HTTPException(status_code=403, detail="权限不够")
        return user
    return dep


def ensure_default_admin() -> None:
    """首次启动：users 表为空时用 APP_PASSWORD 建管理员 admin。"""
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if n == 0:
            conn.execute(
                "INSERT INTO users(username, password_hash, role) VALUES('admin', ?, 'admin')",
                (hash_password(APP_PASSWORD),))
