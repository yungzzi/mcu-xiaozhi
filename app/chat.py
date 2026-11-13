"""聊天路由：会话管理 + SSE 流式回复。

- 每个站内登录会话只能访问自己的 conversations/messages/attachments（逐次校验）。
- 一个聊天同时只允许一轮生成（进程内 asyncio.Lock，单容器部署）。
- 增量事件只用于更新网页；入库以最终回复为准，避免重复拼接。
- 中断/失败时保存已收到的部分文本并标记 status，用户可看历史并自行重发。
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

from .auth import COOKIE_NAME
from .coze import ChatRequest, CozeError
from .db import now

BASE_DIR = Path(__file__).resolve().parent


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def build_restore_prefix(db, cid: int) -> str:
    """旧扣子身份找不回时，用近期成功问答重建上下文（审阅调整）。

    只取「成功的问答对」：assistant 必须 status=ok，user 必须有其后的 ok 回答
    （失败轮次的问题与当前尚未回答的问题都不带入）。按时间顺序拼接，
    最多 20 条、单条截断 1000 字。仅作为本次 /v3/chat 请求文本的前缀，
    不入库、不改动站点历史。
    """
    rows = db.query(
        "SELECT role, content, status FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 60",
        (cid,),
    )
    parts: list[str] = []
    answered = False
    for r in rows:  # 从最新往回配对
        if r["role"] == "assistant" and r["status"] == "ok":
            parts.append("助手：" + r["content"][:1000])
            answered = True
        elif r["role"] == "user" and r["status"] == "ok" and answered:
            parts.append("用户：" + r["content"][:1000])
            answered = False
        if len(parts) >= 20:
            break
    if not parts:
        return ""
    parts.reverse()
    return (
        "【以下是本聊天此前的对话记录（节选），用于恢复上下文，请勿重复回答】\n"
        + "\n".join(parts)
        + "\n【对话记录结束，请回答最新问题】\n"
    )


class _LockGuard:
    """幂等释放 holders：endpoint 建立阶段、生成器、响应生命周期共用同一个 guard。

    审阅二轮 P1：客户端在首包前（响应头阶段）断连时生成器尚未启动，
    仅靠生成器 finally 无法释放锁；响应 __call__ 的 finally 兜底释放。
    """

    def __init__(self, lock: asyncio.Lock):
        self._lock = lock
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._lock.release()


class _GuardedStreamingResponse(StreamingResponse):
    """锁释放覆盖整个响应生命周期（含响应头发送失败/生成器未启动即断连）。"""

    def __init__(self, content, guard: _LockGuard, **kwargs):
        super().__init__(content, **kwargs)
        self._guard = guard

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # 显式关闭可能暂停在 yield 的生成器：触发中断记录与上游清理，
            # 收尾完成后再释放锁（审阅三轮 P2）。
            # 内层 finally：aclose 即使抛出取消异常也必须解锁（审阅四轮）。
            try:
                await self.body_iterator.aclose()
            except Exception:
                logging.getLogger("mcu.chat").exception("stream aclose failed")
            finally:
                self._guard.release()


def register_chat_routes(app):
    db = app.state.db
    auth = app.state.auth
    settings = app.state.settings
    templates = app.state.templates
    locks: dict[int, asyncio.Lock] = {}
    app.state.chat_locks = locks

    def session_of(request: Request):
        return auth.get_session(request.cookies.get(COOKIE_NAME))

    def login_redirect():
        return RedirectResponse(settings.root_path + app.url_path_for("login_page"), status_code=303)

    def unauthorized():
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    def owned_conv(session, cid: int):
        # 账号级归属：同一共享账号的所有设备/登录会话共享历史（跨设备需求）
        return db.query_one(
            "SELECT * FROM conversations WHERE id=? AND user_id=?", (cid, session["user_id"])
        )

    @app.get("/chat", name="chat_page")
    def chat_page(request: Request):
        session = session_of(request)
        if session is None:
            return login_redirect()
        opening = json.loads((BASE_DIR / "opening.json").read_text(encoding="utf-8"))
        return templates.TemplateResponse(request, "chat.html", {"opening": opening})

    @app.get("/history", name="history_page")
    def history_page(request: Request):
        session = session_of(request)
        if session is None:
            return login_redirect()
        convs = db.query(
            "SELECT id, title, updated_at FROM conversations WHERE user_id=? ORDER BY updated_at DESC",
            (session["user_id"],),
        )
        return templates.TemplateResponse(request, "history.html", {"convs": convs})

    @app.get("/api/conversations")
    def list_conversations(request: Request):
        session = session_of(request)
        if session is None:
            return unauthorized()
        rows = db.query(
            "SELECT id, title, created_at, updated_at FROM conversations WHERE user_id=? ORDER BY updated_at DESC",
            (session["user_id"],),
        )
        return [dict(r) for r in rows]

    @app.post("/api/conversations")
    def create_conversation(request: Request):
        session = session_of(request)
        if session is None:
            return unauthorized()
        t = now()
        cur = db.execute(
            "INSERT INTO conversations(token_hash, user_id, coze_user_id, title, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (session["token_hash"], session["user_id"], session["coze_user_id"], "新对话", t, t),
        )
        return {"id": cur.lastrowid, "title": "新对话"}

    @app.get("/api/conversations/{cid}/messages")
    def get_messages(request: Request, cid: int):
        session = session_of(request)
        if session is None:
            return unauthorized()
        if owned_conv(session, cid) is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        rows = db.query(
            "SELECT id, role, content, status, created_at FROM messages WHERE conversation_id=? ORDER BY id",
            (cid,),
        )
        msgs = [dict(r) for r in rows]
        for m in msgs:
            atts = db.query(
                "SELECT id, orig_name, mime FROM attachments WHERE message_id=?", (m["id"],)
            )
            m["attachments"] = [dict(a) for a in atts]
        return {"messages": msgs}

    @app.delete("/api/conversations/{cid}")
    async def delete_conversation(request: Request, cid: int):
        session = session_of(request)
        if session is None:
            return unauthorized()
        if owned_conv(session, cid) is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        # 与发送共用同一会话锁：生成中禁止删除（审阅二轮 P1）
        lock = locks.setdefault(cid, asyncio.Lock())
        if lock.locked():
            return JSONResponse(
                {"error": "busy", "message": "当前聊天正在生成回复，请稍候再删除"}, status_code=409
            )
        await lock.acquire()
        try:
            db.execute("DELETE FROM attachments WHERE message_id IN (SELECT id FROM messages WHERE conversation_id=?)", (cid,))
            db.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
            db.execute("DELETE FROM conversations WHERE id=? AND user_id=?", (cid, session["user_id"]))
        finally:
            lock.release()
        return {"ok": True}

    @app.patch("/api/conversations/{cid}")
    async def rename_conversation(request: Request, cid: int):
        session = session_of(request)
        if session is None:
            return unauthorized()
        conv = owned_conv(session, cid)
        if conv is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "bad_json"}, status_code=400)
        title = body.get("title") if isinstance(body, dict) else None
        if not isinstance(title, str):
            return JSONResponse({"error": "bad_title"}, status_code=400)
        title = title.strip()
        if not title or len(title) > 40:
            return JSONResponse({"error": "bad_title", "message": "名称需为 1–40 个字符"}, status_code=400)
        # 与发送/删除共用会话锁，避免生成中自动标题与重命名互相覆盖
        lock = locks.setdefault(cid, asyncio.Lock())
        if lock.locked():
            return JSONResponse({"error": "busy", "message": "当前聊天正在生成回复，请稍候再重命名"}, status_code=409)
        await lock.acquire()
        try:
            # 锁内重校验会话身份（复用发送接口的做法，审阅 P1）：
            # 等待请求体期间旧聊天可能已被删除、整数 ID 被新聊天复用，
            # 必须确认存在、归属一致、created_at 仍是原会话才能改名。
            conv_now = owned_conv(session, cid)
            if conv_now is None or conv_now["created_at"] != conv["created_at"]:
                return JSONResponse({"error": "not_found"}, status_code=404)
            cur = db.execute(
                "UPDATE conversations SET title=?, auto_title=0"
                " WHERE id=? AND user_id=? AND created_at=?",
                (title, cid, session["user_id"], conv["created_at"]),
            )
            if cur.rowcount == 0:
                return JSONResponse({"error": "not_found"}, status_code=404)
        finally:
            lock.release()
        return {"id": cid, "title": title}

    @app.post("/api/conversations/{cid}/send")
    async def send_message(request: Request, cid: int):
        session = session_of(request)
        if session is None:
            return unauthorized()
        conv = owned_conv(session, cid)
        if conv is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "bad_json"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "bad_json"}, status_code=400)
        # 类型校验必须在取锁之前完成，避免异常路径占锁（审阅 P1 修复）
        raw_text = body.get("text", "")
        if not isinstance(raw_text, str):
            return JSONResponse({"error": "bad_text"}, status_code=400)
        text = raw_text.strip()
        attachment_ids = body.get("attachment_ids") or []
        if not isinstance(attachment_ids, list) or any(
            not isinstance(a, int) or isinstance(a, bool) for a in attachment_ids
        ):
            return JSONResponse({"error": "bad_attachment_ids"}, status_code=400)
        if not text and not attachment_ids:
            return JSONResponse({"error": "empty"}, status_code=400)

        lock = locks.setdefault(cid, asyncio.Lock())
        if lock.locked():
            return JSONResponse({"error": "busy", "message": "当前聊天正在生成回复，请稍候"}, status_code=409)
        await lock.acquire()
        guard = _LockGuard(lock)

        # 取锁后到流开始前的所有步骤（含会话重查）都在释放保护内（审阅四轮 P1）
        try:
            # 取锁后、首次写入前重新校验会话（审阅三轮 P1）：
            # 等待请求体期间旧聊天可能已被删除、整数 ID 被新聊天复用；
            # 必须确认存在、归属一致、且仍是原会话（created_at 身份）才能写入。
            conv_now = owned_conv(session, cid)
            if conv_now is None or conv_now["created_at"] != conv["created_at"]:
                guard.release()
                return JSONResponse({"error": "not_found"}, status_code=404)
            conv = conv_now

            file_ids = []
            link_ids = []  # 将关联到新消息的附件行 id
            for aid in attachment_ids:
                att = db.query_one(
                    "SELECT * FROM attachments WHERE id=? AND user_id=?", (aid, session["user_id"])
                )
                if att is None:
                    guard.release()
                    return JSONResponse({"error": "attachment_not_found"}, status_code=404)
                if att["message_id"] is None:
                    link_ids.append(att["id"])
                else:
                    # 重发：复制一条新索引行（共用磁盘文件与 coze_file_id），
                    # 不改动旧消息与附件的关联（审阅 P2 修复）
                    copy_cur = db.execute(
                        "INSERT INTO attachments(token_hash, user_id, orig_name, stored_name, stored_path, mime, size, coze_file_id, created_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (session["token_hash"], session["user_id"], att["orig_name"], att["stored_name"],
                         att["stored_path"], att["mime"], att["size"], att["coze_file_id"], now()),
                    )
                    link_ids.append(copy_cur.lastrowid)
                if att["coze_file_id"]:
                    file_ids.append(att["coze_file_id"])

            user_cur = db.execute(
                "INSERT INTO messages(conversation_id, role, content, status, created_at) VALUES(?,?,?,?,?)",
                (cid, "user", text, "ok", now()),
            )
            user_msg_id = user_cur.lastrowid
            for att_id in link_ids:
                db.execute("UPDATE attachments SET message_id=? WHERE id=?", (user_msg_id, att_id))
            # 自动命名以 auto_title 标志为准（审阅 P2）：手动改过名（含改成
            # 与默认相同的「新对话」）后不再被首问覆盖；自动命名一次后置 0。
            if conv["auto_title"] and text:
                db.execute(
                    "UPDATE conversations SET title=?, auto_title=0"
                    " WHERE id=? AND user_id=? AND created_at=?",
                    (text[:30], cid, session["user_id"], conv["created_at"]),
                )
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now(), cid))
        except Exception:
            guard.release()
            raise

        coze_conv_id = conv["coze_conversation_id"]
        user_id = session["user_id"]
        # 扣子上下文跟聊天走：用该聊天保存的 ID 对；新聊天/找不回旧身份的聊天
        # 回落到账号级标识（审阅调整）
        coze_user_id = conv["coze_user_id"] or session["coze_user_id"]
        conv_created = conv["created_at"]
        restore = bool(conv["context_restore"])
        send_text = text
        if restore:
            prefix = build_restore_prefix(db, cid)
            if prefix:
                send_text = prefix + "\n" + text

        def conv_alive() -> bool:
            # 写入时校验：会话仍存在、归属一致、且是本轮开始时的同一会话
            # （created_at 防止 SQLite 复用已删除整数 ID 后误写新会话，审阅二轮 P1）
            return db.query_one(
                "SELECT id FROM conversations WHERE id=? AND user_id=? AND created_at=?",
                (cid, user_id, conv_created),
            ) is not None

        def save_assistant(content: str, status: str) -> int:
            if not conv_alive():
                return -1
            cur = db.execute(
                "INSERT INTO messages(conversation_id, role, content, status, created_at) VALUES(?,?,?,?,?)",
                (cid, "assistant", content, status, now()),
            )
            db.execute(
                "UPDATE conversations SET updated_at=? WHERE id=? AND user_id=? AND created_at=?",
                (now(), cid, user_id, conv_created),
            )
            return cur.lastrowid

        async def gen():
            acc: list[str] = []
            content = None
            saved = False
            nonlocal coze_conv_id
            stream = app.state.coze.stream_chat(
                ChatRequest(
                    user_id=coze_user_id,
                    conversation_id=coze_conv_id,
                    text=send_text,
                    file_ids=file_ids or None,
                )
            )
            try:
                async for ev in stream:
                    if ev["type"] == "meta":
                        if not coze_conv_id:
                            coze_conv_id = ev["conversation_id"]
                            # 恢复模式：整轮成功后才切换映射（失败则下次仍带历史）
                            if not restore:
                                db.execute(
                                    "UPDATE conversations SET coze_conversation_id=? WHERE id=? AND user_id=? AND created_at=?",
                                    (coze_conv_id, cid, user_id, conv_created),
                                )
                        yield sse("meta", {"conversation_id": coze_conv_id})
                    elif ev["type"] == "delta":
                        acc.append(ev["text"])
                        yield sse("delta", {"text": ev["text"]})
                    elif ev["type"] == "done":
                        content = ev["content"]
                if content is None:
                    content = "".join(acc)
                msg_id = save_assistant(content, "ok")
                saved = True
                if restore and msg_id != -1 and coze_conv_id:
                    # 整轮成功：切换到新会话映射，之后只发新问题
                    db.execute(
                        "UPDATE conversations SET coze_conversation_id=?, coze_user_id=?, context_restore=0"
                        " WHERE id=? AND user_id=? AND created_at=?",
                        (coze_conv_id, coze_user_id, cid, user_id, conv_created),
                    )
                yield sse("done", {"message_id": msg_id, "content": content, "status": "ok"})
            except CozeError as exc:
                content = "".join(acc)
                if not saved:
                    save_assistant(content, exc.kind)
                message = exc.message
                if exc.kind == "conversation_invalid" and not restore and coze_conv_id:
                    # 扣子明确判定旧会话不存在/不可访问：旧 ID 留作记录，
                    # 下次发送自动带近期历史重建（4028/超时/断线不走这里，保留旧映射）
                    db.execute(
                        "UPDATE conversations SET legacy_coze_conversation_id=coze_conversation_id,"
                        " coze_conversation_id=NULL, coze_user_id=NULL, context_restore=1"
                        " WHERE id=? AND user_id=? AND created_at=?",
                        (cid, user_id, conv_created),
                    )
                    message = "原扣子会话不可用，下次发送将自动带上近期历史重建上下文"
                yield sse("error", {"kind": exc.kind, "message": message, "code": exc.code})
            except (asyncio.CancelledError, GeneratorExit):
                if not saved:
                    save_assistant("".join(acc), "interrupted")
                raise
            except Exception:
                # 未知错误：不向浏览器暴露内部细节，保留脱敏日志
                logging.getLogger("mcu.chat").exception("stream failed conv=%s", cid)
                content = "".join(acc)
                if not saved:
                    save_assistant(content, "other")
                yield sse("error", {"kind": "other", "message": "回复生成失败，可查看历史后重发"})
            finally:
                # 先收尾再释放锁（审阅三轮 P2）：显式关闭上游扣子流，
                # 完成连接清理；中断记录已在 except 分支落库。
                # 内层 finally：aclose 抛出取消异常时也保证解锁（审阅四轮）。
                try:
                    await stream.aclose()
                finally:
                    guard.release()

        return _GuardedStreamingResponse(
            gen(),
            guard,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )