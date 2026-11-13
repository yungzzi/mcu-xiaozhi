"""RealCozeClient 的 HTTP 模拟测试：本地 SSE 服务器，0 次真实扣子调用。

覆盖审阅 P1：整轮完成标志、提前 EOF、requires_action、多条 answer、
chat.failed 双格式、HTTP 鉴权/额度错误、上传接口。
场景通过 Authorization: Bearer scenario:<name> 选择（测试专用，非真实凭证）。
"""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.config import Settings
from app.coze.base import ChatRequest, CozeError
from app.coze.real import RealCozeClient


def ev(name, data):
    return (name, data)


DONE = ("__done__", None)

SCENARIOS = {
    "ok": [
        ev("conversation.chat.created", {"id": "c1", "conversation_id": "conv-ok", "status": "created"}),
        ev("conversation.chat.in_progress", {"id": "c1", "status": "in_progress"}),
        ev("conversation.message.delta", {"id": "m1", "type": "answer", "content": "你好"}),
        ev("conversation.message.delta", {"id": "m1", "type": "answer", "content": "世界"}),
        ev("conversation.message.completed", {"id": "m1", "type": "answer", "content": "你好世界"}),
        ev("conversation.message.completed", {"id": "mv", "type": "verbose",
                                              "content": '{"msg_type":"generate_answer_finish"}'}),
        ev("conversation.chat.completed", {"id": "c1", "status": "completed"}),
        DONE,
    ],
    "multi": [
        ev("conversation.chat.created", {"id": "c1", "conversation_id": "conv-multi", "status": "created"}),
        ev("conversation.message.delta", {"id": "m1", "type": "answer", "content": "第一段"}),
        ev("conversation.message.completed", {"id": "m1", "type": "answer", "content": "第一段回答。"}),
        ev("conversation.message.delta", {"id": "m2", "type": "answer", "content": "第二段"}),
        ev("conversation.message.completed", {"id": "m2", "type": "answer", "content": "第二段回答。"}),
        ev("conversation.message.completed", {"id": "mv", "type": "verbose",
                                              "content": '{"msg_type":"generate_answer_finish"}'}),
        ev("conversation.chat.completed", {"id": "c1", "status": "completed"}),
        DONE,
    ],
    "earlyeof": [
        ev("conversation.chat.created", {"id": "c1", "conversation_id": "conv-eof", "status": "created"}),
        ev("conversation.message.delta", {"id": "m1", "type": "answer", "content": "部分文本"}),
        # 无 chat.completed、无 done：连接正常 EOF
    ],
    "requires_action": [
        ev("conversation.chat.created", {"id": "c1", "conversation_id": "conv-ra", "status": "created"}),
        ev("conversation.message.delta", {"id": "m1", "type": "answer", "content": "部分"}),
        ev("conversation.chat.requires_action", {"id": "c1", "status": "requires_action",
                                                 "required_action": {"type": "submit_tool_outputs"}}),
        DONE,
    ],
    "failed": [
        ev("conversation.chat.created", {"id": "c1", "conversation_id": "conv-f", "status": "created"}),
        ev("conversation.chat.failed", {"code": 701231, "msg": "error"}),
        DONE,
    ],
    "errorevent": [
        ev("conversation.chat.created", {"id": "c1", "conversation_id": "conv-e", "status": "created"}),
        ev("error", {"code": 4015, "msg": "quota exceeded"}),
        DONE,
    ],
    "conv_invalid": [
        ev("conversation.chat.failed", {"code": 40003, "msg": "conversation not found or no permission"}),
        DONE,
    ],
    "conv_invalid_code": [
        # 官方错误码 4002 = invalid conversation；msg 无关键字，验证码优先判定
        ev("conversation.chat.failed", {"code": 4002, "msg": "bad request"}),
        DONE,
    ],
}

LAST_PAYLOAD: dict = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _scenario(self):
        auth = self.headers.get("Authorization", "")
        return auth.replace("Bearer scenario:", "")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        if self.path.startswith("/v3/chat"):
            LAST_PAYLOAD.clear()
            try:
                LAST_PAYLOAD.update(json.loads(raw))
            except ValueError:
                pass
        name = self._scenario()
        if self.path.startswith("/v1/files/upload"):
            if name == "upload-fail":
                body = json.dumps({"code": 4000, "msg": "upload rejected"}).encode()
                self.send_response(200)
            else:
                body = json.dumps({"code": 0, "data": {"id": "local-file-1"}}).encode()
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if name == "http401":
            body = json.dumps({"code": 700012006, "msg": "token invalid"}).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if name == "http429":
            body = json.dumps({"code": 4029, "msg": "rate limit"}).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for name_, data in SCENARIOS.get(name, SCENARIOS["ok"]):
            if name_ == "__done__":
                self.wfile.write(b"event: done\ndata: [DONE]\n\n")
            else:
                payload = f"event: {name_}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                self.wfile.write(payload.encode())
            self.wfile.flush()


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def make_client(server, scenario):
    s = Settings(coze_mode="real", coze_api_base=server, coze_bot_id="test-bot",
                 coze_token=f"scenario:{scenario}")
    return RealCozeClient(s)


def collect(client, text="问题"):
    async def run():
        events = []
        try:
            async for e in client.stream_chat(ChatRequest(user_id="u1", conversation_id=None, text=text)):
                events.append(e)
        finally:
            await client.aclose()
        return events
    return asyncio.run(run())


def expect_error(client, kind):
    async def run():
        try:
            async for _ in client.stream_chat(ChatRequest(user_id="u1", conversation_id=None, text="x")):
                pass
        except CozeError as exc:
            return exc
        finally:
            await client.aclose()
        return None
    exc = asyncio.run(run())
    assert exc is not None, f"应当抛出 CozeError({kind})"
    assert exc.kind == kind
    return exc


def test_normal_round(server):
    events = collect(make_client(server, "ok"))
    assert events[0] == {"type": "meta", "conversation_id": "conv-ok"}
    assert [e["text"] for e in events if e["type"] == "delta"] == ["你好", "世界"]
    assert events[-1] == {"type": "done", "content": "你好世界"}


def test_multi_answer_all_kept(server):
    events = collect(make_client(server, "multi"))
    assert events[-1]["content"] == "第一段回答。\n\n第二段回答。"


def test_early_eof_is_interrupted(server):
    expect_error(make_client(server, "earlyeof"), "interrupted")


def test_requires_action(server):
    expect_error(make_client(server, "requires_action"), "action_required")


def test_chat_failed_direct_format(server):
    exc = expect_error(make_client(server, "failed"), "other")
    assert exc.code == 701231


def test_error_event_quota(server):
    expect_error(make_client(server, "errorevent"), "quota")


def test_conversation_invalid_classified(server):
    """扣子明确判定会话不存在/不可访问 → conversation_invalid（触发重建流程）。"""
    exc = expect_error(make_client(server, "conv_invalid"), "conversation_invalid")
    assert exc.code == 40003


def test_conversation_invalid_official_code_first(server):
    """官方错误码 4002 优先判定（msg 无关键字也成立）。"""
    exc = expect_error(make_client(server, "conv_invalid_code"), "conversation_invalid")
    assert exc.code == 4002


def test_history_sent_as_role_messages(server):
    """审阅 P2：重建历史经 additional_messages 带 role 发送，最后一条是本次问题。"""
    client = make_client(server, "ok")

    async def run():
        events = []
        try:
            req = ChatRequest(
                user_id="u1", conversation_id=None, text="新问题",
                history=[{"role": "user", "content": "旧问"},
                         {"role": "assistant", "content": "旧答"}],
            )
            async for e in client.stream_chat(req):
                events.append(e)
        finally:
            await client.aclose()
        return events

    events = asyncio.run(run())
    assert events[-1]["type"] == "done"
    msgs = LAST_PAYLOAD["additional_messages"]
    assert msgs == [
        {"role": "user", "content": "旧问", "content_type": "text"},
        {"role": "assistant", "content": "旧答", "content_type": "text"},
        {"role": "user", "content": "新问题", "content_type": "text"},
    ]


def test_http_401_auth(server):
    expect_error(make_client(server, "http401"), "auth")


def test_http_429_quota(server):
    expect_error(make_client(server, "http429"), "quota")


def test_upload_ok(server):
    client = make_client(server, "upload-ok")
    async def run():
        try:
            return await client.upload_file("a.png", b"\x89PNG\r\n\x1a\n", "image/png")
        finally:
            await client.aclose()
    assert asyncio.run(run()) == "local-file-1"


def test_upload_failure(server):
    client = make_client(server, "upload-fail")
    async def run():
        try:
            await client.upload_file("a.png", b"x", "image/png")
        except CozeError as exc:
            return exc
        finally:
            await client.aclose()
        return None
    exc = asyncio.run(run())
    assert exc is not None


def test_app_level_early_eof_saved_interrupted(server, tmp_path):
    """应用层：real 客户端指向本地模拟服务器，提前 EOF → 历史标记 interrupted。"""
    from fastapi.testclient import TestClient
    from app.main import create_app

    s = Settings(data_dir=tmp_path / "d", upload_dir=tmp_path / "u",
                 password="test-pass-123", secure_cookies=False,
                 coze_mode="real", coze_api_base=server, coze_bot_id="test-bot",
                 coze_token="scenario:earlyeof")
    app = create_app(s)
    with TestClient(app) as c:
        c.post("/login", data={"username": "mcu", "password": "test-pass-123"})
        cid = c.post("/api/conversations").json()["id"]
        r = c.post(f"/api/conversations/{cid}/send", json={"text": "x"})
        assert "event: error" in r.text
        assert '"kind": "interrupted"' in r.text
        row = app.state.db.query_one(
            "SELECT status, content FROM messages WHERE conversation_id=? AND role='assistant'", (cid,))
        assert row["status"] == "interrupted"
        assert row["content"] == "部分文本"


def test_app_level_multi_answer_history_complete(server, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app

    s = Settings(data_dir=tmp_path / "d", upload_dir=tmp_path / "u",
                 password="test-pass-123", secure_cookies=False,
                 coze_mode="real", coze_api_base=server, coze_bot_id="test-bot",
                 coze_token="scenario:multi")
    app = create_app(s)
    with TestClient(app) as c:
        c.post("/login", data={"username": "mcu", "password": "test-pass-123"})
        cid = c.post("/api/conversations").json()["id"]
        r = c.post(f"/api/conversations/{cid}/send", json={"text": "x"})
        assert "event: done" in r.text
        msgs = c.get(f"/api/conversations/{cid}/messages").json()["messages"]
        assert msgs[1]["content"] == "第一段回答。\n\n第二段回答。"
        assert msgs[1]["status"] == "ok"