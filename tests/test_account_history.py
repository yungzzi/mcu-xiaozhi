"""账号级历史共享 + 扣子上下文跟聊天走（审阅调整）回归。全 mock，0 扣子请求。

覆盖：
- 跨设备共享历史/附件，追问复用聊天保存的扣子 ID 对
- 旧库升级：旧登录仍在 → 保留旧 ID 对续聊
- 旧库升级：旧登录已删 → legacy 记录 + 首次续问带近期历史重建，整轮成功才切换
- 重建失败（quota）保留标记，下次仍带历史
- 扣子明确判定会话不可用 → 进入重建流程
- 两个浏览器同一聊天使用同一对扣子 ID
- 备份映射找回脚本（只读统计 / 写入 / 幂等）
"""
import sqlite3

from fastapi.testclient import TestClient

from app.auth import hash_password, token_hash
from app.coze.base import CozeError
from app.main import create_app
from conftest import PASSWORD, login
from test_chat import new_conv, parse_sse
from test_uploads import PNG, upload


def test_cross_device_followup_reuses_coze_context(app):
    """电脑建聊天发问 → 手机登录可见 → 手机追问复用同一扣子 ID 对。"""
    with TestClient(app) as pc, TestClient(app) as phone:
        login(pc)
        cid = new_conv(pc)
        pc.post(f"/api/conversations/{cid}/send", json={"text": "电脑上的第一问"})
        first_call = app.state.coze.calls[-1]
        saved = app.state.db.query_one(
            "SELECT coze_conversation_id, coze_user_id FROM conversations WHERE id=?", (cid,))
        login(phone)
        assert [c["id"] for c in phone.get("/api/conversations").json()] == [cid]
        r = phone.post(f"/api/conversations/{cid}/send", json={"text": "手机上的追问"})
        assert parse_sse(r.text)[-1][0] == "done"
        followup = app.state.coze.calls[-1]
        assert followup.conversation_id == saved["coze_conversation_id"]
        assert followup.conversation_id is not None
        assert followup.user_id == first_call.user_id == saved["coze_user_id"]
        assert followup.text == "手机上的追问"
        msgs = pc.get(f"/api/conversations/{cid}/messages").json()["messages"]
        assert len(msgs) == 4


def test_two_browsers_same_chat_same_pair(app):
    """两个浏览器（登录会话）打开同一聊天，调用扣子用同一对 ID。"""
    with TestClient(app) as b1, TestClient(app) as b2:
        login(b1)
        login(b2)
        cid = new_conv(b1)
        b1.post(f"/api/conversations/{cid}/send", json={"text": "浏览器1的问题"})
        b2.post(f"/api/conversations/{cid}/send", json={"text": "浏览器2的问题"})
        c1, c2 = app.state.coze.calls[-2], app.state.coze.calls[-1]
        assert c1.user_id == c2.user_id
        saved = app.state.db.query_one(
            "SELECT coze_conversation_id FROM conversations WHERE id=?", (cid,))
        assert c2.conversation_id == saved["coze_conversation_id"] is not None


def test_history_survives_logout_and_relogin(app):
    with TestClient(app) as c:
        login(c)
        cid = new_conv(c)
        c.post(f"/api/conversations/{cid}/send", json={"text": "退出前的历史"})
        c.post("/logout", follow_redirects=False)
        login(c)
        assert [x["id"] for x in c.get("/api/conversations").json()] == [cid]
        msgs = c.get(f"/api/conversations/{cid}/messages").json()["messages"]
        assert len(msgs) == 2


def test_cross_device_attachment_upload_and_read(app):
    with TestClient(app) as pc, TestClient(app) as phone:
        login(pc)
        login(phone)
        aid = upload(pc).json()["id"]
        assert phone.get(f"/api/attachments/{aid}").content == PNG
        aid2 = upload(phone, "phone.png").json()["id"]
        assert pc.get(f"/api/attachments/{aid2}").content == PNG


def test_unauthenticated_still_rejected(client):
    assert client.get("/api/conversations").status_code == 401
    assert client.get("/api/attachments/1").status_code == 401
    assert client.post("/api/uploads").status_code in (401, 422)


def _make_full_old_db(settings, with_session=True):
    """0.1.3 完整旧库：users/sessions/conversations/messages/attachments 均无新列。"""
    path = settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            password_version INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            coze_user_id TEXT NOT NULL,
            remember INTEGER NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            password_version INTEGER NOT NULL
        );
        CREATE TABLE conversations (
            id INTEGER PRIMARY KEY,
            token_hash TEXT NOT NULL,
            coze_conversation_id TEXT,
            title TEXT NOT NULL DEFAULT '新对话',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            conversation_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ok',
            created_at REAL NOT NULL
        );
        CREATE TABLE attachments (
            id INTEGER PRIMARY KEY,
            token_hash TEXT NOT NULL,
            message_id INTEGER,
            orig_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            mime TEXT NOT NULL,
            size INTEGER NOT NULL,
            coze_file_id TEXT,
            created_at REAL NOT NULL
        );
    """)
    th = token_hash("old-device-token")
    conn.execute("INSERT INTO users(username, password_hash) VALUES('mcu', ?)",
                 (hash_password(PASSWORD),))
    if with_session:
        conn.execute(
            "INSERT INTO sessions(token_hash, user_id, coze_user_id, remember, created_at, expires_at, password_version)"
            " VALUES(?,1,'mcu-oldrandom',0,1,9999999999,1)", (th,))
    conn.execute(
        "INSERT INTO conversations(id, token_hash, coze_conversation_id, title, created_at, updated_at)"
        " VALUES(1,?,'old-coze-conv','旧课程聊天',1,1)", (th,))
    conn.execute("INSERT INTO messages(conversation_id, role, content, status, created_at)"
                 " VALUES(1,'user','旧问题','ok',1)")
    conn.execute("INSERT INTO messages(conversation_id, role, content, status, created_at)"
                 " VALUES(1,'assistant','旧回答','ok',2)")
    conn.execute(
        "INSERT INTO attachments(token_hash, message_id, orig_name, stored_name, mime, size, coze_file_id, created_at)"
        " VALUES(?,1,'old.png','old.png','image/png',?,'old-file',1)", (th, len(PNG)))
    conn.commit()
    conn.close()
    d = settings.upload_dir / th[:16]
    d.mkdir(parents=True, exist_ok=True)
    (d / "old.png").write_bytes(PNG)


def test_old_db_upgrade_keeps_recoverable_mapping(settings):
    """旧登录仍在：保留旧 (coze_user_id, coze_conversation_id) 对，续用原会话。"""
    _make_full_old_db(settings, with_session=True)
    app = create_app(settings)
    with TestClient(app) as c:
        login(c)
        convs = c.get("/api/conversations").json()
        assert [x["title"] for x in convs] == ["旧课程聊天"]
        cid = convs[0]["id"]
        row = app.state.db.query_one(
            "SELECT coze_user_id, coze_conversation_id, context_restore FROM conversations WHERE id=?", (cid,))
        assert row["coze_user_id"] == "mcu-oldrandom"
        assert row["coze_conversation_id"] == "old-coze-conv"
        assert row["context_restore"] == 0
        msgs = c.get(f"/api/conversations/{cid}/messages").json()["messages"]
        assert [m["content"] for m in msgs] == ["旧问题", "旧回答"]
        aid = msgs[0]["attachments"][0]["id"]
        assert c.get(f"/api/attachments/{aid}").content == PNG
        # 追问用旧 ID 对续聊，只发新问题
        r = c.post(f"/api/conversations/{cid}/send", json={"text": "升级后的追问"})
        assert parse_sse(r.text)[-1][0] == "done"
        call = app.state.coze.calls[-1]
        assert call.user_id == "mcu-oldrandom"
        assert call.conversation_id == "old-coze-conv"
        assert call.text == "升级后的追问"


def test_old_db_upgrade_unrecoverable_restores_context(settings):
    """旧登录已删：legacy 记录 + 首次续问带近期历史重建；整轮成功才切换映射。"""
    _make_full_old_db(settings, with_session=False)
    app = create_app(settings)
    with TestClient(app) as c:
        login(c)
        cid = c.get("/api/conversations").json()[0]["id"]
        row = app.state.db.query_one(
            "SELECT coze_user_id, coze_conversation_id, legacy_coze_conversation_id, context_restore"
            " FROM conversations WHERE id=?", (cid,))
        assert row["coze_user_id"] is None
        assert row["coze_conversation_id"] is None
        assert row["legacy_coze_conversation_id"] == "old-coze-conv"
        assert row["context_restore"] == 1
        # 首次续问：新扣子会话 + 近期问答前缀 + 账号级身份
        r = c.post(f"/api/conversations/{cid}/send", json={"text": "重建后的新问题"})
        assert parse_sse(r.text)[-1][0] == "done"
        call = app.state.coze.calls[-1]
        assert call.conversation_id is None
        assert "旧问题" in call.text and "旧回答" in call.text
        assert call.text.endswith("重建后的新问题")
        assert call.user_id.startswith("mcu-") and call.user_id != "mcu-oldrandom"
        # 整轮成功：切换到新映射，清除标记；用户消息入库仍为原文
        row = app.state.db.query_one(
            "SELECT coze_user_id, coze_conversation_id, context_restore FROM conversations WHERE id=?", (cid,))
        assert row["context_restore"] == 0
        assert row["coze_conversation_id"] is not None
        assert row["coze_conversation_id"].startswith("mock-conv-")
        assert row["coze_user_id"] == call.user_id
        msgs = c.get(f"/api/conversations/{cid}/messages").json()["messages"]
        assert msgs[-2]["content"] == "重建后的新问题"
        # 之后只发新问题
        r = c.post(f"/api/conversations/{cid}/send", json={"text": "再追问"})
        assert parse_sse(r.text)[-1][0] == "done"
        assert app.state.coze.calls[-1].text == "再追问"
        assert app.state.coze.calls[-1].conversation_id is not None


def test_restore_failure_keeps_flag_and_retries(settings):
    """重建首轮失败（quota）：保留标记与 legacy，下次仍带历史。"""
    _make_full_old_db(settings, with_session=False)
    app = create_app(settings)
    with TestClient(app) as c:
        login(c)
        cid = c.get("/api/conversations").json()[0]["id"]
        r = c.post(f"/api/conversations/{cid}/send", json={"text": "/mock quota 恢复尝试"})
        assert parse_sse(r.text)[-1][0] == "error"
        row = app.state.db.query_one(
            "SELECT context_restore, coze_conversation_id, legacy_coze_conversation_id"
            " FROM conversations WHERE id=?", (cid,))
        assert row["context_restore"] == 1
        assert row["coze_conversation_id"] is None
        assert row["legacy_coze_conversation_id"] == "old-coze-conv"
        # 重试：仍带历史，成功后切换
        r = c.post(f"/api/conversations/{cid}/send", json={"text": "重试的问题"})
        assert parse_sse(r.text)[-1][0] == "done"
        call = app.state.coze.calls[-1]
        assert "旧问题" in call.text and "重试的问题" in call.text
        row = app.state.db.query_one(
            "SELECT context_restore, coze_conversation_id FROM conversations WHERE id=?", (cid,))
        assert row["context_restore"] == 0
        assert row["coze_conversation_id"] is not None


def test_conversation_invalid_triggers_rebuild(app):
    """扣子明确判定旧会话不可用 → 旧 ID 留记录，下次发送自动重建。"""
    class InvalidOnce:
        def __init__(self, inner):
            self.inner = inner
            self.fired = False

        async def upload_file(self, *a, **k):
            return await self.inner.upload_file(*a, **k)

        async def stream_chat(self, req):
            if not self.fired:
                self.fired = True
                raise CozeError("conversation_invalid", "conversation not found", code=40003)
            async for e in self.inner.stream_chat(req):
                yield e

    with TestClient(app) as c:
        login(c)
        cid = new_conv(c)
        c.post(f"/api/conversations/{cid}/send", json={"text": "第一问"})
        old_conv_id = app.state.db.query_one(
            "SELECT coze_conversation_id FROM conversations WHERE id=?", (cid,))["coze_conversation_id"]
        mock = app.state.coze
        app.state.coze = InvalidOnce(mock)
        try:
            r = c.post(f"/api/conversations/{cid}/send", json={"text": "第二问"})
            events = parse_sse(r.text)
            assert events[-1][0] == "error"
            assert events[-1][1]["kind"] == "conversation_invalid"
        finally:
            app.state.coze = mock
        row = app.state.db.query_one(
            "SELECT coze_conversation_id, coze_user_id, legacy_coze_conversation_id, context_restore"
            " FROM conversations WHERE id=?", (cid,))
        assert row["coze_conversation_id"] is None
        assert row["coze_user_id"] is None
        assert row["legacy_coze_conversation_id"] == old_conv_id
        assert row["context_restore"] == 1
        # 下次发送自动带历史重建
        r = c.post(f"/api/conversations/{cid}/send", json={"text": "第三问"})
        assert parse_sse(r.text)[-1][0] == "done"
        call = app.state.coze.calls[-1]
        assert "第一问" in call.text and "第三问" in call.text
        row = app.state.db.query_one(
            "SELECT context_restore, coze_conversation_id FROM conversations WHERE id=?", (cid,))
        assert row["context_restore"] == 0
        assert row["coze_conversation_id"] is not None


def test_quota_keeps_old_mapping(app):
    """4028/额度错误不触发重建：保留旧映射，由用户决定重试。"""
    with TestClient(app) as c:
        login(c)
        cid = new_conv(c)
        c.post(f"/api/conversations/{cid}/send", json={"text": "第一问"})
        before = app.state.db.query_one(
            "SELECT coze_conversation_id, coze_user_id, context_restore FROM conversations WHERE id=?", (cid,))
        r = c.post(f"/api/conversations/{cid}/send", json={"text": "/mock quota"})
        assert parse_sse(r.text)[-1][1]["kind"] == "quota"
        after = app.state.db.query_one(
            "SELECT coze_conversation_id, coze_user_id, context_restore FROM conversations WHERE id=?", (cid,))
        assert dict(before) == dict(after)


def test_02x_lineage_keeps_context_without_restore(tmp_path):
    """0.2.x 谱系（已有 user_id 列）：上下文本就账号级身份，
    创建者登录被删也不标记重建；会话仍在则显式记录账号标识。"""
    from app.db import Database

    path = tmp_path / "lineage.db"
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, password_version INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE sessions (
            token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, coze_user_id TEXT NOT NULL,
            remember INTEGER NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
            password_version INTEGER NOT NULL
        );
        CREATE TABLE conversations (
            id INTEGER PRIMARY KEY, token_hash TEXT NOT NULL, user_id INTEGER,
            coze_conversation_id TEXT, title TEXT NOT NULL DEFAULT '新对话',
            auto_title INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
    """)
    conn.execute("INSERT INTO users(username, password_hash) VALUES('mcu','x')")
    # 聊天1：创建者登录已删；聊天2：创建者登录仍在（已归一为账号标识）
    conn.execute("INSERT INTO conversations(id, token_hash, user_id, coze_conversation_id, created_at, updated_at)"
                 " VALUES(1,'gone',1,'conv-a',1,1)")
    conn.execute("INSERT INTO conversations(id, token_hash, user_id, coze_conversation_id, created_at, updated_at)"
                 " VALUES(2,'alive',1,'conv-b',1,1)")
    conn.execute("INSERT INTO sessions(token_hash, user_id, coze_user_id, remember, created_at, expires_at, password_version)"
                 " VALUES('alive',1,'mcu-account-id',0,1,9999999999,1)")
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        r1 = db.query_one("SELECT coze_conversation_id, coze_user_id, context_restore,"
                          " legacy_coze_conversation_id FROM conversations WHERE id=1")
        assert r1["coze_conversation_id"] == "conv-a"
        assert r1["context_restore"] == 0
        assert r1["legacy_coze_conversation_id"] is None
        r2 = db.query_one("SELECT coze_conversation_id, coze_user_id, context_restore FROM conversations WHERE id=2")
        assert r2["coze_conversation_id"] == "conv-b"
        assert r2["coze_user_id"] == "mcu-account-id"
        assert r2["context_restore"] == 0
    finally:
        db.close()


def _make_backup(path, rows):
    """旧库备份样例。rows: (id, token_hash, coze_conversation_id, created_at, session_coze_user_id|None)。"""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE sessions (
            token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, coze_user_id TEXT NOT NULL,
            remember INTEGER NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
            password_version INTEGER NOT NULL
        );
        CREATE TABLE conversations (
            id INTEGER PRIMARY KEY, token_hash TEXT NOT NULL, coze_conversation_id TEXT,
            title TEXT NOT NULL DEFAULT '新对话', created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
    """)
    for cid, th, conv_id, ts, sess_uid in rows:
        conn.execute(
            "INSERT INTO conversations(id, token_hash, coze_conversation_id, created_at, updated_at)"
            " VALUES(?,?,?,?,?)", (cid, th, conv_id, ts, ts))
        if sess_uid is not None:
            conn.execute(
                "INSERT OR IGNORE INTO sessions(token_hash, user_id, coze_user_id, remember, created_at, expires_at, password_version)"
                " VALUES(?,1,?,0,1,9999999999,1)", (th, sess_uid))
    conn.commit()
    conn.close()


def test_recover_script_stats_apply_idempotent(tmp_path):
    from scripts.recover_coze_mapping import recover

    live = tmp_path / "live.sqlite3"
    backup = tmp_path / "backup.sqlite3"
    # 当前库（新架构，扣子字段为空）：1 可找回 / 2 仅旧会话ID / 3 备份中不存在
    conn = sqlite3.connect(str(live))
    conn.executescript("""
        CREATE TABLE conversations (
            id INTEGER PRIMARY KEY, token_hash TEXT NOT NULL, user_id INTEGER,
            coze_conversation_id TEXT, coze_user_id TEXT,
            context_restore INTEGER NOT NULL DEFAULT 0, legacy_coze_conversation_id TEXT,
            title TEXT NOT NULL DEFAULT '新对话', auto_title INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
    """)
    for cid in (1, 2, 3):
        conn.execute(
            "INSERT INTO conversations(id, token_hash, created_at, updated_at) VALUES(?,?,1,1)",
            (cid, f"th{cid}"))
    conn.commit()
    conn.close()
    _make_backup(backup, [
        (1, "th1", "old-conv-1", 1.0, "mcu-old-1"),
        (2, "th2", "old-conv-2", 1.0, None),
    ])

    stats = recover(backup, live, apply=False)
    assert stats["recoverable"] == 1 and stats["conv_only"] == 1 and stats["not_in_backup"] == 1
    conn = sqlite3.connect(str(live))
    assert conn.execute("SELECT coze_conversation_id FROM conversations WHERE id=1").fetchone()[0] is None
    conn.close()

    stats = recover(backup, live, apply=True)
    assert stats["recoverable"] == 1 and stats["conv_only"] == 1
    conn = sqlite3.connect(str(live))
    conn.row_factory = sqlite3.Row
    r1 = conn.execute("SELECT * FROM conversations WHERE id=1").fetchone()
    assert r1["coze_conversation_id"] == "old-conv-1" and r1["coze_user_id"] == "mcu-old-1"
    r2 = conn.execute("SELECT * FROM conversations WHERE id=2").fetchone()
    assert r2["coze_conversation_id"] is None
    assert r2["legacy_coze_conversation_id"] == "old-conv-2" and r2["context_restore"] == 1
    conn.close()

    # 幂等：再跑一次不重复处理
    stats = recover(backup, live, apply=True)
    assert stats["recoverable"] == 0 and stats["conv_only"] == 0 and stats["skipped"] == 2


def test_recover_script_adds_columns_to_021_db(tmp_path):
    """0.2.1 当前库（无 coze_user_id 等新列）：脚本补列并找回映射。"""
    from scripts.recover_coze_mapping import recover

    live = tmp_path / "live.sqlite3"
    backup = tmp_path / "backup.sqlite3"
    conn = sqlite3.connect(str(live))
    conn.executescript("""
        CREATE TABLE conversations (
            id INTEGER PRIMARY KEY, token_hash TEXT NOT NULL, user_id INTEGER,
            coze_conversation_id TEXT, title TEXT NOT NULL DEFAULT '新对话',
            auto_title INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
    """)
    conn.execute("INSERT INTO conversations(id, token_hash, created_at, updated_at) VALUES(1,'th1',1,1)")
    conn.commit()
    conn.close()
    _make_backup(backup, [(1, "th1", "old-conv-1", 1.0, "mcu-old-1")])

    # 只读统计不要求当前库有新列（生产 0.2.1 场景）
    stats = recover(backup, live, apply=False)
    assert stats["recoverable"] == 1

    stats = recover(backup, live, apply=True)
    assert stats["recoverable"] == 1
    conn = sqlite3.connect(str(live))
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM conversations WHERE id=1").fetchone()
    assert r["coze_conversation_id"] == "old-conv-1" and r["coze_user_id"] == "mcu-old-1"
    cols = {x[1] for x in conn.execute("PRAGMA table_info(conversations)")}
    assert {"coze_user_id", "context_restore", "legacy_coze_conversation_id"} <= cols
    conn.close()
    # 新版迁移启动后保留脚本找回的映射（不被覆盖）
    from app.db import Database
    db = Database(live)
    try:
        r = db.query_one("SELECT coze_conversation_id, coze_user_id, context_restore FROM conversations WHERE id=1")
        assert r["coze_conversation_id"] == "old-conv-1"
        assert r["coze_user_id"] == "mcu-old-1"
        assert r["context_restore"] == 0
    finally:
        db.close()