"""会话重命名回归（全 mock，0 扣子请求）。"""
import asyncio
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth import COOKIE_NAME
from app.db import Database, now
from conftest import PASSWORD, login
from test_chat import new_conv, parse_sse


@pytest.fixture
def auth_client(client):
    login(client)
    return client


def test_rename_ok_and_persists(auth_client):
    cid = new_conv(auth_client)
    r = auth_client.patch(f"/api/conversations/{cid}", json={"title": "晶振电路答疑"})
    assert r.status_code == 200
    assert r.json() == {"id": cid, "title": "晶振电路答疑"}
    convs = auth_client.get("/api/conversations").json()
    assert convs[0]["title"] == "晶振电路答疑"
    assert "晶振电路答疑" in auth_client.get("/history").text


def test_rename_trims_whitespace(auth_client):
    cid = new_conv(auth_client)
    r = auth_client.patch(f"/api/conversations/{cid}", json={"title": "  复位电路  "})
    assert r.json()["title"] == "复位电路"


def test_rename_bad_title(auth_client):
    cid = new_conv(auth_client)
    assert auth_client.patch(f"/api/conversations/{cid}", json={"title": ""}).status_code == 400
    assert auth_client.patch(f"/api/conversations/{cid}", json={"title": "   "}).status_code == 400
    assert auth_client.patch(f"/api/conversations/{cid}", json={"title": "长" * 41}).status_code == 400
    assert auth_client.patch(f"/api/conversations/{cid}", json={"title": 123}).status_code == 400
    assert auth_client.patch(f"/api/conversations/{cid}", json={}).status_code == 400


def test_rename_requires_login(client):
    assert client.patch("/api/conversations/1", json={"title": "x"}).status_code == 401


def test_rename_ownership(app):
    with TestClient(app) as c1, TestClient(app) as c2:
        login(c1)
        login(c2)
        cid = new_conv(c1)
        assert c2.patch(f"/api/conversations/{cid}", json={"title": "偷改"}).status_code == 404
        assert c1.get("/api/conversations").json()[0]["title"] == "新对话"


def test_rename_missing_conv(auth_client):
    assert auth_client.patch("/api/conversations/99999", json={"title": "x"}).status_code == 404


def test_send_after_rename_keeps_custom_title(auth_client):
    cid = new_conv(auth_client)
    auth_client.patch(f"/api/conversations/{cid}", json={"title": "我的自定义名称"})
    r = auth_client.post(f"/api/conversations/{cid}/send", json={"text": "第一个问题"})
    assert parse_sse(r.text)[-1][0] == "done"
    convs = auth_client.get("/api/conversations").json()
    assert convs[0]["title"] == "我的自定义名称"


def test_rename_during_generation_busy(app):
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            await c.post("/login", data={"username": "mcu", "password": PASSWORD},
                         follow_redirects=False)
            cid = (await c.post("/api/conversations")).json()["id"]
            task = asyncio.create_task(
                c.post(f"/api/conversations/{cid}/send", json={"text": "/mock slow 生成中"})
            )
            await asyncio.sleep(0.4)
            r = await c.patch(f"/api/conversations/{cid}", json={"title": "生成中改名"})
            assert r.status_code == 409
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(0.2)
            r = await c.patch(f"/api/conversations/{cid}", json={"title": "生成结束改名"})
            assert r.status_code == 200

    asyncio.run(run())


def test_manual_default_title_not_overwritten(auth_client):
    """审阅 P2 回归：手动命名为与默认相同的「新对话」，首问不得覆盖。"""
    cid = new_conv(auth_client)
    r = auth_client.patch(f"/api/conversations/{cid}", json={"title": "新对话"})
    assert r.status_code == 200
    r = auth_client.post(f"/api/conversations/{cid}/send", json={"text": "第一个问题"})
    assert parse_sse(r.text)[-1][0] == "done"
    convs = auth_client.get("/api/conversations").json()
    assert convs[0]["title"] == "新对话", "手动设置的同名标题被首问覆盖"


def test_auto_title_fires_once(auth_client):
    """自动命名只在首问发生一次，第二问不改标题。"""
    cid = new_conv(auth_client)
    r = auth_client.post(f"/api/conversations/{cid}/send", json={"text": "第一问的内容"})
    assert parse_sse(r.text)[-1][0] == "done"
    r = auth_client.post(f"/api/conversations/{cid}/send", json={"text": "第二问的内容"})
    assert parse_sse(r.text)[-1][0] == "done"
    convs = auth_client.get("/api/conversations").json()
    assert convs[0]["title"] == "第一问的内容"


def test_rename_during_body_read_id_reuse(app):
    """审阅 P1 回归：读请求体期间旧聊天被删、同登录会话新建聊天复用 ID
    → 旧重命名请求 404，新聊天标题保持原样，锁释放。"""
    with TestClient(app) as c:
        login(c)
        cookie = c.cookies.get(COOKIE_NAME)
        cid = c.post("/api/conversations").json()["id"]
        token_hash = app.state.db.query_one(
            "SELECT token_hash FROM conversations WHERE id=?", (cid,))["token_hash"]

        async def run():
            body = json.dumps({"title": "迟到的改名"}).encode()
            path = f"/api/conversations/{cid}"
            scope = {
                "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1", "method": "PATCH", "scheme": "http",
                "path": path, "raw_path": path.encode(), "query_string": b"",
                "root_path": "",
                "headers": [
                    (b"cookie", f"{COOKIE_NAME}={cookie}".encode()),
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
                "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
            }
            mutated = False
            statuses = []

            async def receive():
                nonlocal mutated
                if not mutated:
                    mutated = True
                    # 请求体送达前：删除旧聊天，新建聊天复用同一整数 ID
                    app.state.db.execute("DELETE FROM conversations WHERE id=?", (cid,))
                    app.state.db.execute(
                        "INSERT INTO conversations(id, token_hash, title, created_at, updated_at)"
                        " VALUES(?,?,?,?,?)",
                        (cid, token_hash, "新对话", now() + 10, now() + 10),
                    )
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(msg):
                if msg["type"] == "http.response.start":
                    statuses.append(msg["status"])

            await app(scope, receive, send)
            return statuses

        statuses = asyncio.run(run())
        assert statuses == [404], f"延迟重命名应被拒绝：{statuses}"
        assert not app.state.chat_locks[cid].locked(), "重命名拒绝后锁必须释放"
        row = app.state.db.query_one("SELECT title FROM conversations WHERE id=?", (cid,))
        assert row["title"] == "新对话", "新聊天标题被旧请求改写"
        # 新聊天仍可正常重命名与发送
        r = c.patch(f"/api/conversations/{cid}", json={"title": "新聊天的名字"})
        assert r.status_code == 200
        r = c.post(f"/api/conversations/{cid}/send", json={"text": "新会话的问题"})
        assert parse_sse(r.text)[-1][0] == "done"


def _make_old_db(path, rows):
    """构造 0.1.3 旧库（conversations 无 auto_title 列）。rows: (token_hash, title)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE conversations (
            id INTEGER PRIMARY KEY,
            token_hash TEXT NOT NULL,
            coze_conversation_id TEXT,
            title TEXT NOT NULL DEFAULT '新对话',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
    """)
    for th, title in rows:
        conn.execute(
            "INSERT INTO conversations(token_hash, title, created_at, updated_at) VALUES(?,?,1,1)",
            (th, title),
        )
    conn.commit()
    conn.close()


def test_migration_adds_auto_title(tmp_path):
    """旧库启动幂等补列：已有非默认标题置 0，默认「新对话」保留 1。"""
    path = tmp_path / "old.db"
    _make_old_db(path, [("h", "晶振电路答疑"), ("h", "新对话")])
    db = Database(path)
    try:
        cols = {r[1] for r in db.query("PRAGMA table_info(conversations)")}
        assert "auto_title" in cols
        rows = {r["title"]: r["auto_title"] for r in db.query("SELECT title, auto_title FROM conversations")}
        assert rows["晶振电路答疑"] == 0, "旧版已命名的聊天升级后不得再被自动命名"
        assert rows["新对话"] == 1, "尚未命名的空聊天应保留自动命名资格"
    finally:
        db.close()
    # 再次启动不重复加列、不重置标志（幂等）
    db2 = Database(path)
    try:
        rows = {r["title"]: r["auto_title"] for r in db2.query("SELECT title, auto_title FROM conversations")}
        assert rows == {"晶振电路答疑": 0, "新对话": 1}
    finally:
        db2.close()


def test_old_db_titles_survive_followup(settings):
    """审阅迁移 P2 回归：从 0.1.3 旧库启动，已有课程标题追问后保持原样；
    旧库里尚未命名的空聊天仍能自动命名。"""
    from app.main import create_app

    _make_old_db(settings.db_path, [("placeholder", "晶振电路答疑"), ("placeholder", "新对话")])
    app = create_app(settings)  # 迁移在此发生
    with TestClient(app) as c:
        login(c)
        token_hash = app.state.db.query_one("SELECT token_hash FROM sessions")["token_hash"]
        # 把旧数据挂到当前登录会话名下（迁移已按标题设好标志）
        app.state.db.execute("UPDATE conversations SET token_hash=?", (token_hash,))
        convs = {r["title"]: r["id"] for r in app.state.db.query("SELECT id, title FROM conversations")}
        titled_id, empty_id = convs["晶振电路答疑"], convs["新对话"]

        # 已有标题的聊天继续追问：标题不得被覆盖
        r = c.post(f"/api/conversations/{titled_id}/send", json={"text": "负载电容怎么选？"})
        assert parse_sse(r.text)[-1][0] == "done"
        row = app.state.db.query_one("SELECT title FROM conversations WHERE id=?", (titled_id,))
        assert row["title"] == "晶振电路答疑", "旧库已有标题被追问覆盖"

        # 尚未命名的空聊天发首问：仍自动命名
        r = c.post(f"/api/conversations/{empty_id}/send", json={"text": "复位电路的作用"})
        assert parse_sse(r.text)[-1][0] == "done"
        row = app.state.db.query_one("SELECT title FROM conversations WHERE id=?", (empty_id,))
        assert row["title"] == "复位电路的作用", "旧库空聊天应仍能自动命名"