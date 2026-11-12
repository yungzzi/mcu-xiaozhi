"""真实扣子（Coze 国内版）OpenAPI 客户端。

- 聊天：POST /v3/chat，stream=true、auto_save_history=true；
  首次不带 conversation_id，从 conversation.chat.created 事件取得并复用；
  追问只发送新问题（历史由扣子侧 auto_save_history 维护）。
- 上传：POST /v1/files/upload（multipart），取 data.id 作为 file_id。
- 图文消息：content_type=object_string，content 为 JSON 数组字符串。

错误分类映射为初版，真实错误码在联调时核对并更新本表。
"""
from __future__ import annotations

import json

import httpx

from .base import ChatRequest, CozeError

# 待联调核对：HTTP 状态 → kind
_HTTP_KIND = {401: "auth", 403: "auth", 429: "quota"}
# 待联调核对：业务错误消息关键字 → kind（保留原始 code 供日志/审阅）
_QUOTA_HINTS = ("quota", "额度", "exceed", "limit", "balance")
_AUTH_HINTS = ("token", "auth", "unauthorized", "credential", "pat ")


def classify(code, message: str, http_status: int | None = None) -> str:
    if http_status in _HTTP_KIND:
        return _HTTP_KIND[http_status]
    low = (message or "").lower()
    if any(h in low for h in _QUOTA_HINTS):
        return "quota"
    if any(h in low for h in _AUTH_HINTS):
        return "auth"
    return "other"


class RealCozeClient:
    def __init__(self, settings):
        self.settings = settings
        if not settings.coze_token or not settings.coze_bot_id:
            raise RuntimeError("real 模式需要 COZE_TOKEN（secret 文件）与 COZE_BOT_ID")
        self._client = httpx.AsyncClient(
            base_url=settings.coze_api_base,
            timeout=httpx.Timeout(connect=10, read=180, write=60, pool=10),
            headers={"Authorization": f"Bearer {settings.coze_token}"},
        )

    async def aclose(self):
        await self._client.aclose()

    async def upload_file(self, filename: str, data: bytes, mime: str) -> str:
        try:
            resp = await self._client.post(
                "/v1/files/upload", files={"file": (filename, data, mime)}
            )
        except httpx.HTTPError as exc:
            raise CozeError("network", f"上传网络错误：{exc.__class__.__name__}") from exc
        body = self._json(resp)
        if resp.status_code != 200 or body.get("code") != 0:
            kind = classify(body.get("code"), body.get("msg", ""), resp.status_code)
            raise CozeError(kind, f"上传失败：{body.get('msg', '')}", code=body.get("code"))
        return str(body["data"]["id"])

    async def stream_chat(self, req: ChatRequest):
        if req.file_ids:
            content = json.dumps(
                [{"type": "text", "text": req.text}]
                + [{"type": "image", "file_id": fid} for fid in req.file_ids],
                ensure_ascii=False,
            )
            message = {"role": "user", "content": content, "content_type": "object_string"}
        else:
            message = {"role": "user", "content": req.text, "content_type": "text"}
        payload = {
            "bot_id": self.settings.coze_bot_id,
            "user_id": req.user_id,
            "stream": True,
            "auto_save_history": True,
            "additional_messages": [message],
        }
        params = {}
        if req.conversation_id:
            params["conversation_id"] = req.conversation_id

        # 整轮状态（审阅 P1 修复）：
        # - 以 conversation.chat.completed 为整轮完成标志；正常 EOF 但无完成事件 → interrupted
        # - conversation.chat.requires_action → action_required（需上报工具结果，首版不自动处理）
        # - 多 answer 场景：按序收集每条 message.completed(type=answer) 的完整内容，全部保留
        completed_answers: list[str] = []
        delta_buf: dict[str, list[str]] = {}
        chat_completed = False
        requires_action = False
        conv_id = req.conversation_id
        meta_sent = False
        event_name = ""
        try:
            async with self._client.stream("POST", "/v3/chat", json=payload, params=params) as resp:
                if resp.status_code != 200:
                    raw = (await resp.aread()).decode("utf-8", "replace")
                    body = {}
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        pass
                    kind = classify(body.get("code"), body.get("msg", raw[:200]), resp.status_code)
                    raise CozeError(kind, f"chat 失败：{body.get('msg', raw[:200])}", code=body.get("code"))
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    data_raw = line[5:].strip()
                    if data_raw in ("[DONE]", '"[DONE]"'):
                        break
                    try:
                        data = json.loads(data_raw)
                    except ValueError:
                        continue
                    if event_name == "conversation.chat.created":
                        conv_id = str(data.get("conversation_id") or conv_id or "")
                        if conv_id and not meta_sent:
                            meta_sent = True
                            yield {"type": "meta", "conversation_id": conv_id}
                    elif event_name == "conversation.message.delta":
                        if data.get("type") == "answer":
                            piece = data.get("content", "")
                            mid = str(data.get("id", ""))
                            delta_buf.setdefault(mid, []).append(piece)
                            yield {"type": "delta", "text": piece}
                    elif event_name == "conversation.message.completed":
                        if data.get("type") == "answer":
                            completed_answers.append(data.get("content", ""))
                    elif event_name == "conversation.chat.completed":
                        chat_completed = True
                    elif event_name == "conversation.chat.requires_action":
                        requires_action = True
                    elif event_name == "conversation.chat.failed":
                        # 官方示例 data 直接为 {code, msg}；兼容 last_error 包裹格式
                        err = data.get("last_error") or data
                        kind = classify(err.get("code"), err.get("msg", ""))
                        raise CozeError(kind, f"生成失败：{err.get('msg', '')}", code=err.get("code"))
                    elif event_name == "error":
                        kind = classify(data.get("code"), data.get("msg", ""))
                        raise CozeError(kind, f"生成失败：{data.get('msg', '')}", code=data.get("code"))
        except httpx.HTTPError as exc:
            raise CozeError("network", f"网络错误：{exc.__class__.__name__}") from exc

        if requires_action:
            raise CozeError(
                "action_required",
                "智能体需要额外操作（如端插件授权），本轮未完成",
            )
        if not chat_completed:
            raise CozeError("interrupted", "连接提前结束，未收到整轮完成事件")

        if completed_answers:
            content = "\n\n".join(completed_answers)
        else:
            content = "".join("".join(parts) for parts in delta_buf.values())
        if not meta_sent and conv_id:
            yield {"type": "meta", "conversation_id": conv_id}
        yield {"type": "done", "content": content}

    @staticmethod
    def _json(resp: httpx.Response) -> dict:
        try:
            return resp.json()
        except ValueError:
            return {}