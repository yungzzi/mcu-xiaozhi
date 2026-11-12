"""FastAPI 应用入口：登录/退出/改密 + 页面骨架。聊天与上传在后续分支加入。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .auth import COOKIE_NAME, AuthService
from .config import Settings
from .db import Database

BASE_DIR = Path(__file__).resolve().parent


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)

    # root_path 由服务器层传入（uvicorn --root-path / TestClient root_path），
    # 此处仅用于 Cookie path 与文档说明。
    app = FastAPI(docs_url=None, redoc_url=None)
    db = Database(settings.db_path)
    auth = AuthService(db, settings)
    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

    app.state.settings = settings
    app.state.db = db
    app.state.auth = auth
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    def current_session(request: Request):
        return auth.get_session(request.cookies.get(COOKIE_NAME))

    def login_redirect() -> RedirectResponse:
        return RedirectResponse(app.url_path_for("login_page"), status_code=303)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    @app.get("/ready", include_in_schema=False)
    def ready():
        db.query_one("SELECT 1")
        return {"status": "ready"}

    @app.get("/", include_in_schema=False)
    def index(request: Request):
        if current_session(request):
            return RedirectResponse(app.url_path_for("home_page"), status_code=303)
        return login_redirect()

    @app.get("/login", name="login_page")
    def login_page(request: Request, msg: str = ""):
        if current_session(request):
            return RedirectResponse(app.url_path_for("home_page"), status_code=303)
        return templates.TemplateResponse(request, "login.html", {"msg": msg})

    @app.post("/login")
    def login_submit(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        remember: str | None = Form(None),
    ):
        if not auth.check_login(username, password):
            return templates.TemplateResponse(
                request, "login.html", {"msg": "账号或密码不正确"}, status_code=401
            )
        token, max_age = auth.create_session(username, remember == "on")
        resp = RedirectResponse(app.url_path_for("home_page"), status_code=303)
        resp.set_cookie(
            COOKIE_NAME,
            token,
            max_age=max_age if max_age else None,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="lax",
            path=settings.root_path or "/",
        )
        return resp

    @app.post("/logout")
    def logout(request: Request):
        token = request.cookies.get(COOKIE_NAME)
        if token:
            auth.revoke(token)
        resp = RedirectResponse(app.url_path_for("login_page"), status_code=303)
        resp.delete_cookie(COOKIE_NAME, path=settings.root_path or "/")
        return resp

    @app.get("/home", name="home_page")
    def home_page(request: Request):
        session = current_session(request)
        if session is None:
            return login_redirect()
        return templates.TemplateResponse(request, "home.html", {"session": session})

    @app.post("/settings/password")
    def change_password(
        request: Request,
        current: str = Form(...),
        new: str = Form(...),
    ):
        session = current_session(request)
        if session is None:
            return login_redirect()
        try:
            ok = auth.change_password(settings.username, current, new)
        except ValueError as exc:
            return templates.TemplateResponse(
                request, "home.html", {"session": session, "msg": str(exc)}, status_code=400
            )
        if not ok:
            return templates.TemplateResponse(
                request, "home.html", {"session": session, "msg": "当前密码不正确"}, status_code=401
            )
        resp = RedirectResponse(
            app.url_path_for("login_page") + "?msg=" + "密码已更新，请重新登录", status_code=303
        )
        resp.delete_cookie(COOKIE_NAME, path=settings.root_path or "/")
        return resp

    return app