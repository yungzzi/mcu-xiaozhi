"""扣子客户端抽象：mock 与 real 产出相同的事件流。

事件协议（dict）：
  {"type": "meta",  "conversation_id": str}        # 首次返回的扣子会话 id
  {"type": "delta", "text": str}                   # 增量文本
  {"type": "done",  "content": str}                # 最终完整回复（入库以此为准）

错误统一抛 CozeError，kind 用于前端区分提示：
  quota | auth | interrupted | network | action_required | conversation_invalid | other
"""
from __future__ import annotations

from dataclasses import dataclass


class CozeError(Exception):
    def __init__(self, kind: str, message: str, code: str | int | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.code = code


@dataclass
class ChatRequest:
    user_id: str
    conversation_id: str | None  # None = 新会话；之后复用首次返回值
    text: str
    file_ids: list[str] | None = None  # 图文问答时携带


def get_client(settings):
    if settings.coze_mode == "real":
        from .real import RealCozeClient

        return RealCozeClient(settings)
    from .mock import MockCozeClient

    return MockCozeClient(settings)