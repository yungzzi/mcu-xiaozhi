"""会话重命名回归（全 mock，0 扣子请求）。"""
import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

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