"""并发收尾回归（审阅二轮 P1，全 mock/本地 ASGI，0 次真实扣子调用）。

覆盖：
- 首包前（响应头阶段）断连 → 锁释放 → 可重发
- 发送分片时断连 → 锁释放 → 可重发
- 客户端提前 disconnect → 锁释放
- 生成中删除 → 409；生成结束后删除成功
- 旧流完成时目标会话已被带外删除且 ID 被复用 → 不写入新会话
- 正常并发发送仍返回 409（保持原行为）
"""
import asyncio
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth import COOKIE_NAME
from app.chat import _GuardedStreamingResponse, _LockGuard
from app.db import now
from conftest import PASSWORD, login
from test_chat import parse_sse


def make_scope(cookie: str, cid: int, body: bytes) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": f"/api/conversations/{cid}/send",
        "raw_path": f"/api/conversations/{cid}/send".encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"cookie", f"{COOKIE_NAME}={cookie}".encode()),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }


async def drive_send(app, cookie: str, cid: int, payload: dict, fail_at: str):
    """直接驱动 ASGI 应用，模拟不同阶段的客户端断连。

    返回实际发出的 ASGI 消息类型序列，供断言故障发生阶段。
    fail_at="body" 在第 3 个 body 分片处抛错（确保已进入流式分片阶段）。
    """
    body = json.dumps(payload).encode()
    scope = make_scope(cookie, cid, body)
    request_sent = False
    seen = []
    body_count = 0
    hold = asyncio.Event()  # 保持连接打开，避免提前触发 starlette 断连监听

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await hold.wait()
        return {"type": "http.disconnect"}

    async def send(msg):
        nonlocal body_count
        seen.append(msg["type"])
        if fail_at == "start" and msg["type"] == "http.response.start":
            raise IOError("simulated disconnect at response headers")
        if fail_at == "body" and msg["type"] == "http.response.body":
            body_count += 1
            if body_count >= 3:
                raise IOError("simulated disconnect during body chunk")

    try:
        await app(scope, receive, send)
    except IOError:
        pass
    return seen


@pytest.fixture
def env(app):
    """登录并建一个聊天，返回 (client, cookie, cid)。"""
    with TestClient(app) as c:
        login(c)
        cookie = c.cookies.get(COOKIE_NAME)
        cid = c.post("/api/conversations").json()["id"]
        yield c, cookie, cid


def test_disconnect_at_headers_then_resend(app, env):
    c, cookie, cid = env

    async def run():
        await drive_send(app, cookie, cid, {"text": "首包前断连"}, fail_at="start")
        assert not app.state.chat_locks[cid].locked(), "首包前断连后锁必须释放"

    asyncio.run(run())
    r = c.post(f"/api/conversations/{cid}/send", json={"text": "断连后的新问题"})
    assert r.status_code == 200
    assert parse_sse(r.text)[-1][0] == "done"


def test_disconnect_mid_body_then_resend(app, env):
    c, cookie, cid = env

    async def run():
        seen = await drive_send(app, cookie, cid, {"text": "/mock slow 分片断连"}, fail_at="body")
        # 故障必须确实发生在分片发送阶段：响应头已发出，且已尝试至少 3 个 body 分片
        assert seen[0] == "http.response.start"
        assert seen.count("http.response.body") >= 3, f"未进入分片阶段：{seen}"
        assert not app.state.chat_locks[cid].locked(), "分片断连后锁必须释放"
        # 中断记录已落库且只写一次
        rows = app.state.db.query(
            "SELECT status, content FROM messages WHERE conversation_id=? AND role='assistant'",
            (cid,),
        )
        assert len(rows) == 1, f"中断记录应只写一次：{len(rows)}"
        assert rows[0]["status"] == "interrupted"
        assert len(rows[0]["content"]) > 0, "应保存已收到的部分文本"

    asyncio.run(run())
    r = c.post(f"/api/conversations/{cid}/send", json={"text": "断连后的新问题"})
    assert r.status_code == 200
    assert parse_sse(r.text)[-1][0] == "done"
    # 重发后共两条 assistant：一条 interrupted、一条 ok
    rows = app.state.db.query(
        "SELECT status FROM messages WHERE conversation_id=? AND role='assistant' ORDER BY id",
        (cid,),
    )
    assert [r["status"] for r in rows] == ["interrupted", "ok"]


def test_early_disconnect_releases_lock(app, env):
    c, cookie, cid = env

    async def run():
        # 请求体已送达，随后客户端立即 disconnect（响应阶段客户端已离开）
        body = json.dumps({"text": "提前离开"}).encode()
        scope = make_scope(cookie, cid, body)
        request_sent = False

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(msg):
            pass

        try:
            await app(scope, receive, send)
        except Exception:
            pass  # 断连引发的异常不影响断言：锁必须已释放
        assert not app.state.chat_locks[cid].locked()

    asyncio.run(run())
    r = c.post(f"/api/conversations/{cid}/send", json={"text": "离开后的新问题"})
    assert r.status_code == 200


def test_delete_during_generation_busy(app):
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
            r = await c.delete(f"/api/conversations/{cid}")
            assert r.status_code == 409, "生成中删除必须被拒绝"
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(0.2)
            r = await c.delete(f"/api/conversations/{cid}")
            assert r.status_code == 200, "生成结束后删除应成功"

    asyncio.run(run())


def test_stale_stream_cannot_write_reused_id(app):
    """带外删除 + SQLite ID 复用后，旧流的最终回复不得写入新会话。"""
    class HangClient:
        def __init__(self):
            self.gate = asyncio.Event()

        async def stream_chat(self, req):
            yield {"type": "meta", "conversation_id": "hang-conv"}
            yield {"type": "delta", "text": "旧回复"}
            await self.gate.wait()
            yield {"type": "done", "content": "旧回复完整内容"}

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            await c.post("/login", data={"username": "mcu", "password": PASSWORD},
                         follow_redirects=False)
            cid_a = (await c.post("/api/conversations")).json()["id"]
            hang = HangClient()
            original = app.state.coze
            app.state.coze = hang
            try:
                task = asyncio.create_task(
                    c.post(f"/api/conversations/{cid_a}/send", json={"text": "旧问题"})
                )
                await asyncio.sleep(0.3)  # 旧流已发 meta/delta，阻塞在 gate
                # 模拟修复前的竞态后果：会话被带外删除，新会话复用同一整数 ID
                app.state.db.execute("DELETE FROM messages WHERE conversation_id=?", (cid_a,))
                app.state.db.execute("DELETE FROM conversations WHERE id=?", (cid_a,))
                cid_b = (await c.post("/api/conversations")).json()["id"]
                assert cid_b == cid_a, "前置条件：SQLite 复用了被删除的 ID"
                hang.gate.set()
                r = await task  # 旧流正常完成
                assert r.status_code == 200
                # 旧回复不得出现在新会话
                msgs = (await c.get(f"/api/conversations/{cid_b}/messages")).json()["messages"]
                assert msgs == [], f"旧回复写入了复用 ID 的新会话：{msgs}"
            finally:
                app.state.coze = original
            # 锁已释放：新会话可正常发送
            r = await c.post(f"/api/conversations/{cid_b}/send", json={"text": "新会话问题"})
            assert r.status_code == 200
            assert parse_sse(r.text)[-1][0] == "done"

    asyncio.run(run())


def test_concurrent_send_still_409(app):
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            await c.post("/login", data={"username": "mcu", "password": PASSWORD},
                         follow_redirects=False)
            cid = (await c.post("/api/conversations")).json()["id"]
            task = asyncio.create_task(
                c.post(f"/api/conversations/{cid}/send", json={"text": "/mock slow 第一轮"})
            )
            await asyncio.sleep(0.4)
            r = await c.post(f"/api/conversations/{cid}/send", json={"text": "插队"})
            assert r.status_code == 409
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(run())


def _drive_with_body_mutation(app, cookie, cid, mutate):
    """在请求体读取期间执行 mutate（模拟 await request.json() 时会话变化）。"""
    async def run():
        body = json.dumps({"text": "旧聊天问题"}).encode()
        scope = make_scope(cookie, cid, body)
        mutated = False
        statuses = []

        async def receive():
            nonlocal mutated
            if not mutated:
                mutated = True
                mutate()
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(msg):
            if msg["type"] == "http.response.start":
                statuses.append(msg["status"])

        await app(scope, receive, send)
        return statuses

    return asyncio.run(run())


def test_conv_replaced_during_body_read(app, env):
    """审阅三轮 P1 回归：读请求体期间旧会话被删且 ID 被复用 → 404，不写入新会话。"""
    c, cookie, cid = env
    token_hash = app.state.db.query_one(
        "SELECT token_hash FROM conversations WHERE id=?", (cid,))["token_hash"]

    def mutate():
        app.state.db.execute("DELETE FROM conversations WHERE id=?", (cid,))
        app.state.db.execute(
            "INSERT INTO conversations(id, token_hash, title, created_at, updated_at) VALUES(?,?,?,?,?)",
            (cid, token_hash, "新对话", now() + 10, now() + 10),
        )

    statuses = _drive_with_body_mutation(app, cookie, cid, mutate)
    assert statuses == [404], f"应拒绝复用 ID 的新会话：{statuses}"
    assert not app.state.chat_locks[cid].locked()
    msgs = app.state.db.query("SELECT * FROM messages WHERE conversation_id=?", (cid,))
    assert msgs == [], "用户消息不得写入复用 ID 的新会话"
    # 新会话仍可正常使用
    r = c.post(f"/api/conversations/{cid}/send", json={"text": "新会话的问题"})
    assert r.status_code == 200
    assert parse_sse(r.text)[-1][0] == "done"


def test_conv_deleted_during_body_read(app, env):
    """读请求体期间会话被删除（无复用）→ 404，锁释放。"""
    c, cookie, cid = env

    def mutate():
        app.state.db.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
        app.state.db.execute("DELETE FROM conversations WHERE id=?", (cid,))

    statuses = _drive_with_body_mutation(app, cookie, cid, mutate)
    assert statuses == [404]
    assert not app.state.chat_locks[cid].locked()


def test_recheck_query_failure_releases_lock(app, env):
    """审阅四轮 P1 回归：锁内会话重查瞬时失败 → 500，但锁必须释放，恢复后可重发。"""
    c, cookie, cid = env
    db = app.state.db
    original = db.query_one
    calls = {"n": 0}

    def flaky(sql, params=()):
        if sql.startswith("SELECT * FROM conversations"):
            calls["n"] += 1
            if calls["n"] == 2:  # 第 1 次=取锁前初查，第 2 次=锁内重查
                raise sqlite3.OperationalError("simulated transient db failure")
        return original(sql, params)

    db.query_one = flaky
    try:
        with TestClient(app, raise_server_exceptions=False) as c2:
            c2.cookies.set(COOKIE_NAME, cookie)
            r = c2.post(f"/api/conversations/{cid}/send", json={"text": "重查失败"})
            assert r.status_code == 500
    finally:
        db.query_one = original
    assert calls["n"] >= 2, "前置条件：锁内重查确实被执行并失败"
    assert not app.state.chat_locks[cid].locked(), "重查异常后锁必须释放"
    r = c.post(f"/api/conversations/{cid}/send", json={"text": "恢复后的正常重发"})
    assert r.status_code == 200
    assert parse_sse(r.text)[-1][0] == "done"


def test_aclose_cancelled_still_releases_lock(app):
    """审阅四轮：响应收尾 aclose 抛取消异常时，内层 finally 仍须解锁。"""
    async def run():
        lock = asyncio.Lock()
        await lock.acquire()
        guard = _LockGuard(lock)

        async def g():
            yield "data: x\n\n"

        class CancelOnClose:
            """代理异步迭代器：aclose 时抛取消异常（async_generator.aclose 只读，无法直接 patch）。"""

            def __init__(self, agen):
                self._agen = agen

            def __aiter__(self):
                return self

            async def __anext__(self):
                return await self._agen.__anext__()

            async def aclose(self):
                raise asyncio.CancelledError()

        resp = _GuardedStreamingResponse(CancelOnClose(g()), guard, media_type="text/event-stream")
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "http", "path": "/", "raw_path": b"/",
            "query_string": b"", "root_path": "", "headers": [],
            "client": ("127.0.0.1", 1), "server": ("test", 80),
        }
        hold = asyncio.Event()

        async def receive():
            await hold.wait()
            return {"type": "http.disconnect"}

        async def send(msg):
            pass

        try:
            await resp(scope, receive, send)
        except asyncio.CancelledError:
            pass
        assert not lock.locked(), "aclose 取消异常后锁必须释放"

    asyncio.run(run())