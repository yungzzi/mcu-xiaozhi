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

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth import COOKIE_NAME
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
    """直接驱动 ASGI 应用，模拟不同阶段的客户端断连。"""
    body = json.dumps(payload).encode()
    scope = make_scope(cookie, cid, body)
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(msg):
        if fail_at == "start" and msg["type"] == "http.response.start":
            raise IOError("simulated disconnect at response headers")
        if fail_at == "body" and msg["type"] == "http.response.body":
            raise IOError("simulated disconnect during body chunk")

    try:
        await app(scope, receive, send)
    except IOError:
        pass


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
        await drive_send(app, cookie, cid, {"text": "/mock slow 分片断连"}, fail_at="body")
        assert not app.state.chat_locks[cid].locked(), "分片断连后锁必须释放"

    asyncio.run(run())
    r = c.post(f"/api/conversations/{cid}/send", json={"text": "断连后的新问题"})
    assert r.status_code == 200
    assert parse_sse(r.text)[-1][0] == "done"


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