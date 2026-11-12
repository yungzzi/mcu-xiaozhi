"""图片上传/归属/图文问答的自动化验证（全部 mock，不触网）。"""
import io

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from conftest import PASSWORD, login

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-body" * 10
JPG = b"\xff\xd8\xff\xe0" + b"fake-jpg-body" * 10


@pytest.fixture
def auth_client(client):
    login(client)
    return client


def upload(client, name="pic.png", data=PNG):
    return client.post("/api/uploads", files={"file": (name, io.BytesIO(data), "application/octet-stream")})


def test_upload_png_ok(auth_client, app, tmp_path):
    r = upload(auth_client)
    assert r.status_code == 200
    body = r.json()
    assert body["mime"] == "image/png" and body["size"] == len(PNG)
    att = app.state.db.query_one("SELECT * FROM attachments WHERE id=?", (body["id"],))
    assert att["coze_file_id"].startswith("mock-file-")
    stored = app.state.settings.upload_dir / att["token_hash"][:16] / att["stored_name"]
    assert stored.read_bytes() == PNG
    assert len(app.state.coze.uploads) == 1


def test_upload_jpg_ok(auth_client):
    r = upload(auth_client, "photo.jpg", JPG)
    assert r.status_code == 200
    assert r.json()["mime"] == "image/jpeg"


def test_reject_wrong_extension(auth_client):
    assert upload(auth_client, "a.gif", PNG).status_code == 400
    assert upload(auth_client, "a.txt", b"hello").status_code == 400


def test_reject_magic_mismatch(auth_client):
    r = upload(auth_client, "fake.png", b"not-a-png-at-all")
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_type"


def test_reject_oversize(tmp_path):
    s = Settings(
        data_dir=tmp_path / "d", upload_dir=tmp_path / "u",
        password=PASSWORD, secure_cookies=False, max_upload_bytes=100,
    )
    app = create_app(s)
    with TestClient(app) as c:
        login(c)
        r = upload(c, "big.png", PNG)  # len(PNG) > 100
        assert r.status_code == 400
        assert r.json()["error"] == "too_large"


def test_attachment_ownership(app):
    with TestClient(app) as c1, TestClient(app) as c2:
        login(c1)
        login(c2)
        aid = upload(c1).json()["id"]
        r1 = c1.get(f"/api/attachments/{aid}")
        assert r1.status_code == 200
        assert r1.headers["content-type"] == "image/png"
        assert r1.content == PNG
        assert c2.get(f"/api/attachments/{aid}").status_code == 404


def test_upload_requires_login(client):
    assert upload(client).status_code == 401
    assert client.get("/api/attachments/1").status_code == 401


def test_send_with_image(auth_client, app):
    aid = upload(auth_client).json()["id"]
    cid = auth_client.post("/api/conversations").json()["id"]
    r = auth_client.post(
        f"/api/conversations/{cid}/send",
        json={"text": "这张电路图讲什么？", "attachment_ids": [aid]},
    )
    assert r.status_code == 200
    call = app.state.coze.calls[-1]
    assert call.file_ids and call.file_ids[0].startswith("mock-file-")
    msgs = auth_client.get(f"/api/conversations/{cid}/messages").json()["messages"]
    user = msgs[0]
    assert user["attachments"][0]["id"] == aid
    assert "file_id=" in msgs[1]["content"]


def test_send_foreign_attachment_rejected(app):
    with TestClient(app) as c1, TestClient(app) as c2:
        login(c1)
        login(c2)
        aid = upload(c1).json()["id"]
        cid = c2.post("/api/conversations").json()["id"]
        r = c2.post(f"/api/conversations/{cid}/send", json={"text": "x", "attachment_ids": [aid]})
        assert r.status_code == 404


def test_coze_upload_failure(auth_client):
    r = upload(auth_client, "fail-upload.png", PNG)
    assert r.status_code == 502
    assert r.json()["kind"] == "network"


def test_attachment_no_store(auth_client):
    """审阅 P2：私有附件必须 no-store，退出后不得命中浏览器缓存。"""
    aid = upload(auth_client).json()["id"]
    r = auth_client.get(f"/api/attachments/{aid}")
    assert r.status_code == 200
    assert "no-store" in r.headers["cache-control"]


def test_resend_keeps_old_attachment_link(auth_client, app):
    """审阅 P2 回归：带图重发后两条历史都显示原图，旧关联不被挪走。"""
    aid = upload(auth_client).json()["id"]
    cid = auth_client.post("/api/conversations").json()["id"]
    auth_client.post(f"/api/conversations/{cid}/send",
                     json={"text": "第一次看图", "attachment_ids": [aid]})
    auth_client.post(f"/api/conversations/{cid}/send",
                     json={"text": "重发再看一次", "attachment_ids": [aid]})
    msgs = auth_client.get(f"/api/conversations/{cid}/messages").json()["messages"]
    users = [m for m in msgs if m["role"] == "user"]
    assert len(users) == 2
    assert len(users[0]["attachments"]) == 1
    assert len(users[1]["attachments"]) == 1
    aid1 = users[0]["attachments"][0]["id"]
    aid2 = users[1]["attachments"][0]["id"]
    assert aid1 == aid and aid2 != aid  # 旧关联不变，新消息用复制行
    # 两条历史的图片都能读取且内容一致
    r1 = auth_client.get(f"/api/attachments/{aid1}")
    r2 = auth_client.get(f"/api/attachments/{aid2}")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.content == r2.content == PNG
    # 数据库层面旧行的 message_id 仍指向第一条消息
    old = app.state.db.query_one("SELECT message_id FROM attachments WHERE id=?", (aid,))
    assert old["message_id"] == users[0]["id"]