"""共享账号认证与登录会话管理。

- 密码：PBKDF2-HMAC-SHA256 哈希存服务端。
- Cookie：只持有随机 token；服务端存其 SHA-256。
- 记住登录：15 天持久 Cookie；未记住：浏览器会话 Cookie + 服务端 12 小时上限。
- 每个登录会话分配独立 coze_user_id；改密撤销全部会话。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

from .config import REMEMBER_SECONDS, SESSION_SECONDS, Settings
from .db import Database, now

COOKIE_NAME = "mcu_session"
_PBKDF2_ITERATIONS = 240_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iters, salt_hex, hash_hex = stored.split("$")
        if scheme != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AuthService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self._ensure_user()

    def _ensure_user(self) -> None:
        row = self.db.query_one("SELECT id FROM users WHERE username=?", (self.settings.username,))
        if row is None:
            if not self.settings.password:
                raise RuntimeError(
                    "首次启动需要设置共享密码：MCU_PASSWORD 或 MCU_PASSWORD_FILE（secret 挂载）"
                )
            self.db.execute(
                "INSERT INTO users(username, password_hash) VALUES(?, ?)",
                (self.settings.username, hash_password(self.settings.password)),
            )

    def check_login(self, username: str, password: str) -> bool:
        row = self.db.query_one("SELECT password_hash FROM users WHERE username=?", (username,))
        if row is None:
            # 恒定时间比较，避免用户名枚举 timing 差异
            verify_password(password, hash_password("dummy"))
            return False
        return verify_password(password, row["password_hash"])

    def create_session(self, username: str, remember: bool) -> tuple[str, float]:
        user = self.db.query_one("SELECT id, password_version FROM users WHERE username=?", (username,))
        token = secrets.token_urlsafe(32)
        ttl = REMEMBER_SECONDS if remember else SESSION_SECONDS
        expires = now() + ttl
        self.db.execute(
            "INSERT INTO sessions(token_hash, user_id, coze_user_id, remember, created_at, expires_at, password_version)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                token_hash(token),
                user["id"],
                "mcu-" + secrets.token_hex(8),
                1 if remember else 0,
                now(),
                expires,
                user["password_version"],
            ),
        )
        return token, ttl if remember else 0  # max_age=0 → 浏览器会话 Cookie

    def get_session(self, token: str | None):
        if not token:
            return None
        row = self.db.query_one("SELECT * FROM sessions WHERE token_hash=?", (token_hash(token),))
        if row is None:
            return None
        if row["expires_at"] < now():
            self.revoke(token)
            return None
        user = self.db.query_one("SELECT password_version FROM users WHERE id=?", (row["user_id"],))
        if user is None or user["password_version"] != row["password_version"]:
            self.revoke(token)
            return None
        return row

    def revoke(self, token: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))

    def change_password(self, username: str, current: str, new: str) -> bool:
        if not self.check_login(username, current):
            return False
        if len(new) < 8:
            raise ValueError("新密码至少 8 位")
        self.db.execute(
            "UPDATE users SET password_hash=?, password_version=password_version+1 WHERE username=?",
            (hash_password(new), username),
        )
        self.db.execute("DELETE FROM sessions")  # 撤销全部旧登录
        return True