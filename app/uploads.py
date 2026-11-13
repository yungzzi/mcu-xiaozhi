"""图片上传与附件读取：格式/大小校验 → 持久卷存原图 → 扣子 uploadFile 换 file_id。

- 首版仅支持 JPG/PNG：扩展名 + 魔数双重校验（不做完整解码）。
- 附件按账号共享（跨设备），读取逐次校验账号归属。
- 删除聊天只删索引，磁盘文件保留（避免不可逆删除；容量治理后续再做）。
"""
from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from .auth import COOKIE_NAME
from .coze import CozeError
from .db import now

ALLOWED = {
    ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
}


def register_upload_routes(app):
    db = app.state.db
    auth = app.state.auth
    settings = app.state.settings
    client = app.state.coze

    def session_of(request: Request):
        return auth.get_session(request.cookies.get(COOKIE_NAME))

    @app.post("/api/uploads")
    async def upload(request: Request, file: UploadFile):
        session = session_of(request)
        if session is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        name = file.filename or "image"
        ext = Path(name).suffix.lower()
        if ext not in ALLOWED:
            return JSONResponse({"error": "invalid_type", "message": "仅支持 JPG/PNG 图片"}, status_code=400)
        data = await file.read()
        if len(data) > settings.max_upload_bytes:
            return JSONResponse(
                {"error": "too_large", "message": f"图片不能超过 {settings.max_upload_bytes // (1024*1024)}MB"},
                status_code=400,
            )
        mime, magic = ALLOWED[ext]
        if not data.startswith(magic):
            return JSONResponse({"error": "invalid_type", "message": "文件内容与图片格式不符"}, status_code=400)

        subdir = settings.upload_dir / f"user-{session['user_id']}"
        subdir.mkdir(parents=True, exist_ok=True)
        stored_name = secrets.token_hex(12) + ext
        (subdir / stored_name).write_bytes(data)
        stored_path = f"user-{session['user_id']}/{stored_name}"

        try:
            file_id = await client.upload_file(name, data, mime)
        except CozeError as exc:
            return JSONResponse(
                {"error": "upload_failed", "kind": exc.kind, "message": "图片上传到扣子失败，请稍后重试"},
                status_code=502,
            )
        cur = db.execute(
            "INSERT INTO attachments(token_hash, user_id, orig_name, stored_name, stored_path, mime, size, coze_file_id, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (session["token_hash"], session["user_id"], name, stored_name, stored_path, mime, len(data), file_id, now()),
        )
        return {"id": cur.lastrowid, "orig_name": name, "mime": mime, "size": len(data)}

    @app.get("/api/attachments/{aid}")
    def get_attachment(request: Request, aid: int):
        session = session_of(request)
        if session is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        att = db.query_one(
            "SELECT * FROM attachments WHERE id=? AND user_id=?", (aid, session["user_id"])
        )
        if att is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        path = settings.upload_dir / att["stored_path"]
        if not path.is_file():
            return JSONResponse({"error": "not_found"}, status_code=404)
        return FileResponse(
            path,
            media_type=att["mime"],
            # no-store：附件不得命中浏览器缓存，每次访问都重新走归属校验（审阅 P2）
            headers={"Cache-Control": "no-store", "Content-Disposition": "inline"},
        )