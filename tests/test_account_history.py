"""账号级历史共享（跨设备）与 0.1.3 旧库升级回归（全 mock，0 扣子请求）。"""
import sqlite3

from fastapi.testclient import TestClient

from app.auth import hash_password, token_hash
from app.main import create_app
from conftest import PASSWORD, login
from test_chat import new_conv, parse_sse
from test_uploads import PNG, upload


def test_cross_device_followup_reuses_coze_context(app):
    """电脑建聊天发问 → 手机登录可见 → 手机追问复用同一扣子上下文与 user_id。"""
    with TestClient(app) as pc, TestClient(app) as phone:
        login(pc)
        cid = new_conv(pc)
        pc.post(f"/api/conversations/{cid}/send", json={"text": "电脑上的第一问"})
        first_call = app.state.coze.calls[-1]
        saved = app.state.db.query_one(
            "SELECT coze_conversation_id FROM conversations WHERE id=?", (cid,))
        login(phone)
        assert [c["id"] for c in phone.get("/api/conversations").json()] == [cid]
        r = phone.post(f"/api/conversations/{cid}/send", json={"text": "手机上的追问"})
        assert parse_sse(r.text)[-1][0] == "done"
        followup = app.state.coze.calls[-1]
        # 追问复用首轮创建并入库的扣子会话（首问请求本身不携带 ID）
        assert followup.conversation_id == saved["coze_conversation_id"]
        assert followup.conversation_id is not None
        assert followup.user_id == first_call.user_id
        assert followup.text == "手机上的追问"
        msgs = pc.get(f"/api/conversations/{cid}/messages").json()["messages"]
        assert len(msgs) == 4


def test_history_survives_logout_and_relogin(app):
    """退出再登录（含换设备）历史仍在：归属键是账号而非登录会话。"""
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
    """账号级共享不等于免登录：未登录仍一律 401。"""
    assert client.get("/api/conversations").status_code == 401
    assert client.get("/api/attachments/1").status_code == 401
    assert client.post("/api/uploads").status_code in (401, 422)


def _make_full_old_db(settings):
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
    # 旧附件磁盘文件（旧目录约定 token_hash[:16]/stored_name）
    d = settings.upload_dir / th[:16]
    d.mkdir(parents=True, exist_ok=True)
    (d / "old.png").write_bytes(PNG)


def test_old_db_upgrade_shares_history(settings):
    """0.1.3 旧库升级：新设备登录可见旧聊天/消息/附件；旧扣子上下文已重置，
    追问开启新扣子会话而不报错；站点历史完整。"""
    _make_full_old_db(settings)
    app = create_app(settings)
    with TestClient(app) as c:
        login(c)  # 新设备登录
        convs = c.get("/api/conversations").json()
        assert [x["title"] for x in convs] == ["旧课程聊天"]
        cid = convs[0]["id"]
        msgs = c.get(f"/api/conversations/{cid}/messages").json()["messages"]
        assert [m["content"] for m in msgs] == ["旧问题", "旧回答"]
        assert msgs[0]["attachments"][0]["orig_name"] == "old.png"
        aid = msgs[0]["attachments"][0]["id"]
        r = c.get(f"/api/attachments/{aid}")
        assert r.status_code == 200 and r.content == PNG
        row = app.state.db.query_one(
            "SELECT coze_conversation_id FROM conversations WHERE id=?", (cid,))
        assert row["coze_conversation_id"] is None
        r = c.post(f"/api/conversations/{cid}/send", json={"text": "升级后的追问"})
        assert parse_sse(r.text)[-1][0] == "done"
        assert app.state.coze.calls[-1].conversation_id is None
        # 旧会话的 coze_user_id 已归一为账号级标识
        sess = app.state.db.query("SELECT coze_user_id FROM sessions")
        assert len({s["coze_user_id"] for s in sess}) == 1