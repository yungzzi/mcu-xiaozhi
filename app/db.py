"""SQLite 存储：用户、登录会话、聊天会话、消息、附件索引。

会话归属以 session token 的 SHA-256 为键；Cookie 只持有随机 token 本身。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    password_version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    coze_user_id TEXT NOT NULL,
    remember INTEGER NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    password_version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY,
    token_hash TEXT NOT NULL,
    user_id INTEGER,
    coze_conversation_id TEXT,
    coze_user_id TEXT,
    context_restore INTEGER NOT NULL DEFAULT 0,
    legacy_coze_conversation_id TEXT,
    title TEXT NOT NULL DEFAULT '新对话',
    auto_title INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_token ON conversations(token_hash, updated_at DESC);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ok',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY,
    token_hash TEXT NOT NULL,
    user_id INTEGER,
    message_id INTEGER,
    orig_name TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    stored_path TEXT,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    coze_file_id TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_att_token ON attachments(token_hash);
"""


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            # 幂等迁移：旧库补 auto_title 列（是否允许首问自动命名；
            # 手动重命名后置 0，避免同名默认标题被首问覆盖，审阅 P2）
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(conversations)")}
            # 谱系判定：迁移前已有 user_id 列 → 0.2.x 账号级版本。
            # 该版本创建的扣子上下文一律用账号级身份，创建者登录被删也不需重建。
            had_account_migration = "user_id" in cols
            if "auto_title" not in cols:
                # 补列与回填必须在同一事务内（审阅迁移 P2）：sqlite3 默认
                # 隔离级别下 ALTER TABLE 会立即自动提交，若两者之间中断，
                # 重启将因列已存在而跳过回填。显式事务保证失败时一起回滚。
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    self._conn.execute(
                        "ALTER TABLE conversations ADD COLUMN auto_title INTEGER NOT NULL DEFAULT 1"
                    )
                    # 旧版已自动命名过的聊天（非默认标题）升级后不得再被追问覆盖：
                    # 只有仍是默认「新对话」的空聊天保留自动命名资格
                    self._conn.execute(
                        "UPDATE conversations SET auto_title=0 WHERE title != '新对话'"
                    )
                    self._conn.execute("COMMIT")
                except BaseException:
                    if self._conn.in_transaction:
                        self._conn.execute("ROLLBACK")
                    raise
            # 幂等迁移：账号归属 + 扣子上下文映射必须在同一事务内（审阅 P1）：
            # 若分两次提交、期间中断，重启会因 user_id 已存在而把 0.1.3 旧库
            # 误判为 0.2.x 谱系，跳过旧身份缺失时的重建标记。
            # - 归属：单共享账号应用，全部旧数据归属该账号；附件记录相对路径
            # - 上下文：每条聊天保存调用扣子使用的 coze_user_id；在 sessions
            #   归一化之前（AuthService 在本迁移之后初始化）用 token_hash 找回
            #   旧登录身份。可找回→保留旧 ID 对续聊；找不回→旧会话 ID 存 legacy
            #   作记录并标记 context_restore（首次续问用近期问答重建）
            att_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(attachments)")}
            need_account = ("user_id" not in cols or "user_id" not in att_cols
                            or "stored_path" not in att_cols)
            need_context = ("coze_user_id" not in cols or "context_restore" not in cols
                            or "legacy_coze_conversation_id" not in cols)
            if need_account or need_context:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    if need_account:
                        if "user_id" not in cols:
                            self._conn.execute("ALTER TABLE conversations ADD COLUMN user_id INTEGER")
                        if "user_id" not in att_cols:
                            self._conn.execute("ALTER TABLE attachments ADD COLUMN user_id INTEGER")
                        if "stored_path" not in att_cols:
                            self._conn.execute("ALTER TABLE attachments ADD COLUMN stored_path TEXT")
                        self._conn.execute(
                            "UPDATE conversations SET user_id=(SELECT id FROM users ORDER BY id LIMIT 1)"
                            " WHERE user_id IS NULL"
                        )
                        self._conn.execute(
                            "UPDATE attachments SET user_id=(SELECT id FROM users ORDER BY id LIMIT 1)"
                            " WHERE user_id IS NULL"
                        )
                        self._conn.execute(
                            "UPDATE attachments SET stored_path=substr(token_hash,1,16) || '/' || stored_name"
                            " WHERE stored_path IS NULL"
                        )
                    if need_context:
                        if "coze_user_id" not in cols:
                            self._conn.execute("ALTER TABLE conversations ADD COLUMN coze_user_id TEXT")
                        if "context_restore" not in cols:
                            self._conn.execute(
                                "ALTER TABLE conversations ADD COLUMN context_restore INTEGER NOT NULL DEFAULT 0")
                        if "legacy_coze_conversation_id" not in cols:
                            self._conn.execute(
                                "ALTER TABLE conversations ADD COLUMN legacy_coze_conversation_id TEXT")
                        self._conn.execute(
                            "UPDATE conversations SET coze_user_id=("
                            " SELECT s.coze_user_id FROM sessions s WHERE s.token_hash=conversations.token_hash)"
                            " WHERE coze_user_id IS NULL AND coze_conversation_id IS NOT NULL"
                            " AND EXISTS (SELECT 1 FROM sessions s WHERE s.token_hash=conversations.token_hash)"
                        )
                        if not had_account_migration:
                            # 仅 0.1.3 直升级路径：旧身份找不回才标记重建。
                            # 0.2.x 谱系的上下文本就账号级，coze_user_id 留空回落即可。
                            self._conn.execute(
                                "UPDATE conversations SET legacy_coze_conversation_id=coze_conversation_id,"
                                " coze_conversation_id=NULL, context_restore=1"
                                " WHERE coze_user_id IS NULL AND coze_conversation_id IS NOT NULL"
                            )
                    self._conn.execute("COMMIT")
                except BaseException:
                    if self._conn.in_transaction:
                        self._conn.execute("ROLLBACK")
                    raise
            self._conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    @contextmanager
    def transaction(self):
        """显式事务：yield 原生连接供调用方直接执行 SQL，成功整体提交、
        失败整体回滚（用于回复入库+映射切换等原子写入，审阅建议）。
        注意：事务内不要再调用 execute/query（会重复取锁）。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def now() -> float:
    return time.time()