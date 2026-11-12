#!/usr/bin/env python3
"""带 /mcu-xiaozhi 反代前缀的浏览器检查（mock，0 次真实扣子调用）。

用本地剥前缀代理模拟生产 nginx 入口（proxy_pass 尾斜杠 + Host 透传），
验证：前缀下登录跳转、JS 站内资源 BASE 解析、插件配图加载、上传预览。

需要 dev 依赖：pip install playwright && playwright install chromium --only-shell
运行：.venv/bin/python scripts/browser_prefix_check.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = 8096
PROXY = 8097
PREFIX = "/mcu-xiaozhi"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


class Proxy(BaseHTTPRequestHandler):
    """等价于 nginx：location /mcu-xiaozhi/ { proxy_pass http://backend/; proxy_set_header Host $host; }"""

    def log_message(self, *a):
        pass

    def _fwd(self):
        path = self.path
        if path.startswith(PREFIX):
            path = path[len(PREFIX):] or "/"
        url = f"http://127.0.0.1:{BACKEND}{path}"
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else None
        req = urllib.request.Request(url, data=body, method=self.command)
        for h in ("Cookie", "Content-Type", "Accept", "Host"):
            if self.headers.get(h):
                req.add_header(h, self.headers[h])
        try:
            resp = OPENER.open(req, timeout=60)
            status, headers, data = resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            status, headers, data = e.code, e.headers, e.read()
        self.send_response(status)
        for k, v in headers.items():
            if k.lower() in ("transfer-encoding", "connection", "content-length"):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _fwd
    do_POST = _fwd


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="mcu-prefix-")
    env = dict(os.environ,
               MCU_DATA_DIR=f"{tmp}/data", MCU_UPLOAD_DIR=f"{tmp}/uploads",
               MCU_PASSWORD="prefix-pass-123", MCU_SECURE_COOKIES="0", COZE_MODE="mock",
               MCU_ROOT_PATH=PREFIX)
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory",
         "--port", str(BACKEND), "--root-path", PREFIX],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proxy = ThreadingHTTPServer(("127.0.0.1", PROXY), Proxy)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    failures = 0
    try:
        for _ in range(30):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{BACKEND}/healthz", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            pg = b.new_page()
            pg.goto(f"http://127.0.0.1:{PROXY}{PREFIX}/login")
            assert f"{PREFIX}/static/style.css" in pg.content()
            pg.fill('input[name="username"]', "mcu")
            pg.fill('input[name="password"]', "prefix-pass-123")
            pg.click('button[type="submit"]')
            pg.wait_for_url(f"**{PREFIX}/chat", timeout=10000)
            print("PASS 前缀下登录跳转", pg.url)
            pg.fill("#input", "配图 /mock image")
            pg.click("#send")
            pg.wait_for_selector(".msg.assistant .body.streaming", state="detached", timeout=15000)
            img = pg.locator("img.plugin-img")
            img.wait_for(timeout=5000)
            src = img.get_attribute("src")
            ok = img.evaluate("el => el.naturalWidth") > 0
            print("img src =", src)
            if ok and f"{PREFIX}/static/" in src:
                print("PASS 前缀下配图解析并加载")
            else:
                print("FAIL 配图"); failures += 1
            pg.set_input_files("#file-input", str(REPO / "app/static/sample-plugin.png"))
            pg.wait_for_selector(".chip", timeout=5000)
            if pg.locator(".chip img").evaluate("el => el.naturalWidth") > 0:
                print("PASS 前缀下上传预览")
            else:
                print("FAIL 上传预览"); failures += 1
            b.close()
    finally:
        server.terminate()
        server.wait(timeout=10)
        proxy.shutdown()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())