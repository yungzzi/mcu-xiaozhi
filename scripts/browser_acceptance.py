#!/usr/bin/env python3
"""本地浏览器验收（mock 模式，0 次真实扣子调用）。

需要 dev 依赖：pip install playwright && playwright install chromium --only-shell
运行：.venv/bin/python scripts/browser_acceptance.py

覆盖：流式增量渲染、代码块、插件配图与失败降级、多 answer 完整性、
Markdown 注入安全、图片上传/带图重发、账号级历史共享（重登后历史/附件仍在）、
课程插画常驻聊天背景。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PORT = 8095
BASE = f"http://127.0.0.1:{PORT}"
PASSWORD = "browser-pass-123"
SAMPLE_PNG = REPO / "app" / "static" / "sample-plugin.png"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="mcu-browser-")
    env = dict(os.environ,
               MCU_DATA_DIR=f"{tmp}/data", MCU_UPLOAD_DIR=f"{tmp}/uploads",
               MCU_PASSWORD=PASSWORD, MCU_SECURE_COOKIES="0", COZE_MODE="mock")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory", "--port", str(PORT)],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(30):
            try:
                import urllib.request
                urllib.request.urlopen(f"{BASE}/healthz", timeout=1)
                break
            except Exception:
                time.sleep(0.5)

        from playwright.sync_api import sync_playwright
        console_errors: list[str] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 390, "height": 844})  # 手机尺寸
            page = context.new_page()
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

            # 1. 登录
            page.goto(f"{BASE}/login")
            page.fill('input[name="username"]', "mcu")
            page.fill('input[name="password"]', PASSWORD)
            page.click('button[type="submit"]')
            page.wait_for_url("**/chat")
            check("登录跳转 /chat", page.url.endswith("/chat"))

            # 1.5 开场白 + 预置问题 + 错误横幅初始隐藏
            page.wait_for_selector(".empty-hero", timeout=5000)
            check("开场白显示（扣子配置原文）", "单片机教学助手" in page.inner_text(".empty-hero"))
            check("预置问题 3 条", page.locator(".suggest").count() == 3)
            check("空态无重复插画卡片（插画已是常驻背景）", page.locator(".hero-art").count() == 0)
            check("错误横幅初始隐藏", not page.locator("#banner").is_visible())
            check("待发送区初始隐藏", not page.locator("#pending").is_visible())

            # 2. 流式增量 + 代码块 + 插件配图
            page.fill("#input", "给我代码示例和配图 /mock image")
            page.click("#send")
            page.wait_for_selector(".msg.assistant .body.streaming", timeout=5000)
            page.wait_for_function(
                "document.querySelector('.msg.assistant .body.streaming') && "
                "document.querySelector('.msg.assistant .body.streaming').textContent.length > 0",
                timeout=5000)
            partial = page.text_content(".msg.assistant .body.streaming") or ""
            check("流式增量渲染（生成中已有部分文本）", len(partial) > 0, f"partial={len(partial)} chars")
            page.wait_for_selector(".msg.assistant .body.streaming", state="detached", timeout=15000)
            body = page.locator(".msg.assistant .body").last
            check("代码块渲染 pre.code", body.locator("pre.code").count() == 1
                  and "hello mcu" in (body.locator("pre.code").inner_text()))
            img = body.locator("img.plugin-img")
            img.wait_for(timeout=5000)
            check("插件配图加载", img.evaluate("el => el.naturalWidth") > 0)
            bg = page.evaluate("getComputedStyle(document.querySelector('.main')).backgroundImage")
            check("插画常驻聊天背景（发消息后仍在）", "chat-bg.jpg" in bg, bg[:60])

            # 3. 配图加载失败降级为链接
            page.fill("#input", "/mock brokenimage")
            page.click("#send")
            page.wait_for_selector(".msg.assistant .body.streaming", state="detached", timeout=15000)
            page.wait_for_selector("text=图片加载失败", timeout=5000)
            check("配图失败降级为可点击链接", page.locator("text=图片加载失败").count() >= 1)

            # 4. 多 answer 完整性（页面 + 刷新后历史）
            page.fill("#input", "/mock multi")
            page.click("#send")
            page.wait_for_selector(".msg.assistant .body.streaming", state="detached", timeout=15000)
            check("多 answer 页面完整", "第二段回答" in page.inner_text("#messages"))
            page.reload()
            page.wait_for_selector(".msg.assistant .body")
            check("多 answer 历史完整", "第二段回答" in page.inner_text("#messages"))

            # 5. Markdown 注入安全（mock 会回显问题文本）
            page.fill("#input", '<img src=x onerror="window.__xss=1"> [点我](javascript:alert(1)) **粗体**')
            page.click("#send")
            page.wait_for_selector(".msg.assistant .body.streaming", state="detached", timeout=15000)
            xss = page.evaluate("window.__xss")
            bad_img = page.locator('img[src="x"]').count()
            bad_a = page.evaluate("document.querySelectorAll('a[href^=\"javascript\"]').length")
            check("HTML 注入未执行", xss is None and bad_img == 0, f"__xss={xss}, img[src=x]={bad_img}")
            check("javascript: 链接未渲染", bad_a == 0)
            check("加粗正常渲染", page.locator(".msg.assistant .body strong").count() >= 1)

            # 6. 图片上传 + 图文问答
            page.set_input_files("#file-input", str(SAMPLE_PNG))
            page.wait_for_selector(".chip", timeout=5000)
            check("上传后待发送预览", page.locator(".chip img").count() == 1)
            page.fill("#input", "这张图讲什么")
            page.click("#send")
            page.wait_for_selector(".msg.assistant .body.streaming", state="detached", timeout=15000)
            check("用户消息带附件缩略图", page.locator(".msg.user .attachment-thumb").count() == 1)
            check("图文问答回复含 file_id", "file_id=" in page.inner_text("#messages"))

            # 7. 带图重发（错误横幅 → 重发上一条）：两条历史都显示原图
            page.set_input_files("#file-input", str(SAMPLE_PNG))
            page.wait_for_selector(".chip", timeout=5000)
            page.fill("#input", "/mock quota 带图重发")
            page.click("#send")
            page.wait_for_selector("#banner:not([hidden])", timeout=15000)
            check("额度错误横幅", "额度已耗尽" in page.inner_text("#banner"))
            page.click("#resend")
            page.wait_for_selector("#banner:not([hidden])", timeout=15000)
            # 步骤6一张 + 步骤7首发一张 + 重发复制一张 = 3
            check("带图重发后各条用户消息都有图",
                  page.locator(".msg.user .attachment-thumb").count() == 3)

            # 记录附件 URL，供退出后隔离验证
            att_urls = page.evaluate(
                "Array.from(document.querySelectorAll('.msg.user .attachment-thumb')).map(i => i.src)")

            # 7.5 生成中删除 → 409 提示，历史列表保留（审阅三轮）
            # 注意：必须用新标签页打开历史页，保持聊天页 SSE 连接存活，否则导航会中断生成
            page.fill("#input", "/mock slow " + "长" * 200)
            page.click("#send")
            page.wait_for_selector(".msg.assistant .body.streaming", timeout=5000)
            hist = page.context.new_page()
            hist.goto(f"{BASE}/history")
            dialog_msgs = []
            hist.on("dialog", lambda d: (dialog_msgs.append(d.message), d.accept()))
            hist.click(".del-conv")
            hist.wait_for_timeout(800)
            check("生成中删除弹出 409 提示",
                  any("正在生成" in m for m in dialog_msgs), str(dialog_msgs))
            check("删除被拒后历史列表保留", hist.locator(".conv-list li").count() >= 1)
            hist.close()
            page.wait_for_selector(".msg.assistant .body.streaming", state="detached", timeout=30000)

            # 7.6 侧栏内联重命名（移动端先打开抽屉）
            if page.locator("#menu-btn").is_visible():
                page.click("#menu-btn")
                page.wait_for_selector("#sidebar.open", timeout=5000)
            page.hover(".conv-item.active")
            page.click(".conv-item.active .conv-pen")
            page.wait_for_selector(".conv-item.active .rename-input", timeout=5000)
            page.fill(".conv-item.active .rename-input", "重命名验收聊天")
            page.press(".conv-item.active .rename-input", "Enter")
            page.wait_for_timeout(600)
            check("侧栏重命名生效", page.inner_text("#conv-title") == "重命名验收聊天")
            check("侧栏列表标题同步", "重命名验收聊天" in page.inner_text("#conv-list"))
            if page.locator("#sidebar.open").count():
                page.click("#scrim", position={"x": 350, "y": 400})
                page.wait_for_timeout(350)

            # 8. 退出 → 重登 → 账号级历史共享：历史与附件仍在（归属键是账号）
            # 移动端视口下退出按钮在侧栏抽屉里，先打开抽屉
            if page.locator("#menu-btn").is_visible():
                page.click("#menu-btn")
                page.wait_for_selector("#sidebar.open", timeout=5000)
            page.click('form[action*="logout"] button')
            page.wait_for_url("**/login")
            page.fill('input[name="username"]', "mcu")
            page.fill('input[name="password"]', PASSWORD)
            page.click('button[type="submit"]')
            page.wait_for_url("**/chat")
            page.wait_for_selector(".msg", timeout=5000)
            check("重登后历史仍在（账号级共享）", page.locator(".msg").count() > 0)
            statuses = []
            for u in att_urls:
                resp = page.goto(u)
                statuses.append(resp.status if resp else None)
            check("重登后旧附件仍可访问（同账号，no-store 每次重新校验）",
                  all(s == 200 for s in statuses), str(statuses))
            browser.close()

        real_errors = [e for e in console_errors
                       if "127.0.0.1:1" not in e and "Failed to load resource" not in e]
        check("浏览器控制台无意外错误", len(real_errors) == 0, str(real_errors[:3]))
    finally:
        server.terminate()
        server.wait(timeout=10)

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())