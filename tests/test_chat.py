"""聊天流式/会话归属/单轮锁/错误分类的自动化验证（全部 mock，不触网）。"""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import login


def parse_sse(text: str):
    events = []
    for block in text.split("\n\n"):
        event, data = None, None
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        if event:
            events.append((event, data))
    return events


def new_conv(client):
    r = client.post("/api/conversations")
    assert r.status_code == 200
    return r.json()["id"]


@pytest.fixture
def auth_client(client):
    login(client)
    return client


def test_send_streams_and_persists_once(auth_client, app):
    cid = new_conv(auth_client)
    r = auth_client.post(f"/api/conversations/{cid}/send", json={"text": "你好，介绍下单片机"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    kinds = [e for e, _ in events]
    assert kinds[0] == "meta"
    assert "delta" in kinds and kinds[-1] == "done"
    done = events[-1][1]
    assert "单片机" in done["content"]
    # 入库只有一条 assistant，且内容等于最终回复（无重复拼接）
    rows = app.state.db.query(
        "SELECT role, content, status FROM messages WHERE conversation_id=? ORDER BY id", (cid,)
    )
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[1]["content"] == done["content"]
    assert rows[1]["status"] == "ok"


def test_conversation_id_reused_and_only_new_question(app, auth_client):
    cid = new_conv(auth_client)
    auth_client.post(f"/api/conversations/{cid}/send", json={"text": "第一问"})
    auth_client.post(f"/api/conversations/{cid}/send", json={"text": "第二问"})
    calls = app.state.coze.calls
    assert len(calls) == 2
    assert calls[0].conversation_id is None
    assert calls[1].conversation_id is not None
    saved = app.state.db.query_one("SELECT coze_conversation_id FROM conversations WHERE id=?", (cid,))
    assert saved["coze_conversation_id"] == calls[1].conversation_id
    assert calls[1].text == "第二问"  # 只发新问题
    # 同一登录会话内 coze_user_id 稳定
    assert calls[0].user_id == calls[1].user_id


def test_title_from_first_question(auth_client):
    cid = new_conv(auth_client)
    auth_client.post(f"/api/conversations/{cid}/send", json={"text": "什么是中断向量表？"})
    convs = auth_client.get("/api/conversations").json()
    assert convs[0]["title"] == "什么是中断向量表？"


def test_history_shared_across_logins(app):
    """账号级归属：同一账号的不同登录（跨设备）共享历史，可互读/追问/删除。"""
    with TestClient(app) as c1, TestClient(app) as c2:
        login(c1)
        login(c2)
        cid1 = new_conv(c1)
        c1.post(f"/api/conversations/{cid1}/send", json={"text": "电脑上的问题"})
        # c2（另一设备登录）能看到 c1 的会话与消息
        assert [c["id"] for c in c2.get("/api/conversations").json()] == [cid1]
        assert len(c2.get(f"/api/conversations/{cid1}/messages").json()["messages"]) == 2
        # c2 追问成功，c1 也能看到追问结果
        r = c2.post(f"/api/conversations/{cid1}/send", json={"text": "手机上的追问"})
        assert parse_sse(r.text)[-1][0] == "done"
        assert len(c1.get(f"/api/conversations/{cid1}/messages").json()["messages"]) == 4
        # 删除同样账号级共享
        assert c2.delete(f"/api/conversations/{cid1}").status_code == 200
        assert c1.get(f"/api/conversations/{cid1}/messages").status_code == 404


def test_api_requires_login(client):
    assert client.get("/api/conversations").status_code == 401
    assert client.post("/api/conversations").status_code == 401
    assert client.get("/chat", follow_redirects=False).status_code == 303
    assert client.get("/history", follow_redirects=False).status_code == 303


def test_quota_error_classified(auth_client, app):
    cid = new_conv(auth_client)
    r = auth_client.post(f"/api/conversations/{cid}/send", json={"text": "/mock quota"})
    events = parse_sse(r.text)
    assert events[-1][0] == "error"
    assert events[-1][1]["kind"] == "quota"
    row = app.state.db.query_one(
        "SELECT status FROM messages WHERE conversation_id=? AND role='assistant'", (cid,)
    )
    assert row["status"] == "quota"


def test_auth_error_classified(auth_client, app):
    cid = new_conv(auth_client)
    r = auth_client.post(f"/api/conversations/{cid}/send", json={"text": "/mock auth"})
    events = parse_sse(r.text)
    assert events[-1][1]["kind"] == "auth"


def test_history_page_shared_across_logins(app):
    with TestClient(app) as c1, TestClient(app) as c2:
        login(c1)
        login(c2)
        cid = new_conv(c1)
        c1.post(f"/api/conversations/{cid}/send", json={"text": "我的历史"})
        page1 = c1.get("/history").text
        page2 = c2.get("/history").text
        assert "我的历史" in page1
        assert "我的历史" in page2


def test_delete_conversation(auth_client, app):
    cid = new_conv(auth_client)
    auth_client.post(f"/api/conversations/{cid}/send", json={"text": "待删除"})
    assert auth_client.delete(f"/api/conversations/{cid}").status_code == 200
    assert app.state.db.query("SELECT * FROM messages WHERE conversation_id=?", (cid,)) == []
    assert auth_client.get(f"/api/conversations/{cid}/messages").status_code == 404


def test_busy_lock_and_interrupted_save(app):
    """并发第二轮 → 409；取消第一轮 → 保存 interrupted 部分文本。"""
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            # 登录
            r = await c.post("/login", data={"username": "mcu", "password": "test-pass-123"},
                             follow_redirects=False)
            assert r.status_code == 303
            r = await c.post("/api/conversations")
            cid = r.json()["id"]

            async def slow_send():
                async with c.stream("POST", f"/api/conversations/{cid}/send",
                                    json={"text": "/mock slow 长回复"}) as resp:
                    got_delta = False
                    async for line in resp.aiter_lines():
                        if line.startswith("event: delta"):
                            got_delta = True
                            break
                    assert got_delta

            task = asyncio.create_task(slow_send())
            await asyncio.sleep(0.4)  # 第一轮进行中
            r2 = await c.post(f"/api/conversations/{cid}/send", json={"text": "插队"})
            assert r2.status_code == 409
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(0.2)
            # 锁已释放：同一登录会话可以再次发送
            r3 = await c.post(f"/api/conversations/{cid}/send", json={"text": "恢复后的新问题"})
            assert r3.status_code == 200
            assert parse_sse(r3.text)[-1][0] == "done"

    asyncio.run(run())
    rows = app.state.db.query("SELECT role, status, content FROM messages ORDER BY id")
    assistant = [r for r in rows if r["role"] == "assistant"]
    assert len(assistant) == 2
    assert assistant[0]["status"] == "interrupted"
    assert assistant[1]["status"] == "ok"


def test_code_block_in_reply(auth_client):
    cid = new_conv(auth_client)
    r = auth_client.post(f"/api/conversations/{cid}/send", json={"text": "给我一段代码示例"})
    done = parse_sse(r.text)[-1][1]
    assert "```python" in done["content"]


def test_invalid_types_do_not_hold_lock(auth_client):
    """审阅 P1 回归：取锁前类型校验失败后，同一聊天仍能正常发送。"""
    cid = new_conv(auth_client)
    assert auth_client.post(f"/api/conversations/{cid}/send", json={"text": 123}).status_code == 400
    assert auth_client.post(f"/api/conversations/{cid}/send",
                            json={"text": "x", "attachment_ids": "not-a-list"}).status_code == 400
    assert auth_client.post(f"/api/conversations/{cid}/send",
                            json={"text": "x", "attachment_ids": [True]}).status_code == 400
    assert auth_client.post(f"/api/conversations/{cid}/send",
                            json={"text": "x", "attachment_ids": ["1"]}).status_code == 400
    assert auth_client.post(f"/api/conversations/{cid}/send", json=[1, 2]).status_code == 400
    # 锁未被占用：正常发送成功
    r = auth_client.post(f"/api/conversations/{cid}/send", json={"text": "正常问题"})
    assert r.status_code == 200
    assert parse_sse(r.text)[-1][0] == "done"


def test_missing_attachment_then_send_ok(auth_client):
    """取锁后附件校验失败也必须释放锁。"""
    cid = new_conv(auth_client)
    r = auth_client.post(f"/api/conversations/{cid}/send",
                         json={"text": "x", "attachment_ids": [99999]})
    assert r.status_code == 404
    r = auth_client.post(f"/api/conversations/{cid}/send", json={"text": "后续问题"})
    assert r.status_code == 200
    assert parse_sse(r.text)[-1][0] == "done"


def test_multi_answer_mock_history_complete(auth_client):
    cid = new_conv(auth_client)
    r = auth_client.post(f"/api/conversations/{cid}/send", json={"text": "/mock multi"})
    done = parse_sse(r.text)[-1][1]
    assert "第二段回答" in done["content"]
    msgs = auth_client.get(f"/api/conversations/{cid}/messages").json()["messages"]
    assert msgs[1]["content"] == done["content"]