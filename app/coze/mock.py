"""本地模拟扣子客户端：开发/测试默认使用，不触网、不耗额度。

特殊触发词（仅 mock 模式有效）：
  /mock quota        → 模拟额度耗尽
  /mock auth         → 模拟凭证失效
  /mock slow         → 慢速流（用于中断/单轮锁测试）
  /mock image        → 回复带插件配图（本站静态样例图）
  /mock brokenimage  → 回复带加载失败的图床 URL（测试降级为链接）
  含「代码」          → 回复带代码块
"""
from __future__ import annotations

import asyncio
import secrets

from .base import ChatRequest, CozeError

SAMPLE_IMAGE = "/static/sample-plugin.png"
BROKEN_IMAGE = "http://127.0.0.1:1/broken-plugin-image.png"


class MockCozeClient:
    def __init__(self, settings):
        self.settings = settings
        self.calls: list[ChatRequest] = []  # 供测试断言：只发新问题、复用 conversation_id
        self.uploads: list[dict] = []

    async def upload_file(self, filename: str, data: bytes, mime: str) -> str:
        await asyncio.sleep(0)
        if "fail-upload" in filename:
            raise CozeError("network", "模拟：上传失败", code="mock-upload")
        file_id = "mock-file-" + secrets.token_hex(6)
        self.uploads.append({"file_id": file_id, "filename": filename, "size": len(data), "mime": mime})
        return file_id

    async def stream_chat(self, req: ChatRequest):
        self.calls.append(req)
        text = req.text
        if "/mock quota" in text:
            raise CozeError("quota", "模拟：额度耗尽", code="mock-quota")
        if "/mock auth" in text:
            raise CozeError("auth", "模拟：凭证失效", code="mock-auth")

        conv_id = req.conversation_id or "mock-conv-" + secrets.token_hex(6)
        yield {"type": "meta", "conversation_id": conv_id}

        parts = [f"（模拟回复）我是 MCU 小智。收到你的问题：{text}"]
        if "代码" in text:
            parts.append("\n示例代码：\n```python\nprint('hello mcu')\n```\n")
        if "/mock image" in text or "配图" in text:
            parts.append(f"\n课程配图：\n![课程配图]({SAMPLE_IMAGE})\n")
        if "/mock brokenimage" in text:
            parts.append(f"\n![加载失败的图]({BROKEN_IMAGE})\n")
        if "/mock multi" in text:
            parts.append("\n\n【第二段回答】这是多 answer 场景的第二条回复，页面与历史都应完整保留。")
        if req.file_ids:
            parts.append(f"\n（已收到 {len(req.file_ids)} 张图片，file_id={','.join(req.file_ids)}）")
        reply = "".join(parts)

        slow = "/mock slow" in text
        chunk = 8
        for i in range(0, len(reply), chunk):
            if slow:
                await asyncio.sleep(0.15)
            else:
                await asyncio.sleep(0.01)
            yield {"type": "delta", "text": reply[i : i + chunk]}
        yield {"type": "done", "content": reply}