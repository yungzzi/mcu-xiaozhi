"""FastAPI 应用入口：登录/退出 + 页面骨架；聊天与上传路由分别注册。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .auth import COOKIE_NAME, AuthService
from .chat import register_chat_routes
from .config import Settings
from .coze import get_client
from .db import Database
from .uploads import register_upload_routes

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
    app.state.coze = get_client(settings)

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    register_chat_routes(app)
    register_upload_routes(app)

    def current_session(request: Request):
        return auth.get_session(request.cookies.get(COOKIE_NAME))

    def prefixed(name: str) -> str:
        # app.url_path_for 不含 root_path；重定向必须带反代前缀
        return settings.root_path + app.url_path_for(name)

    def login_redirect() -> RedirectResponse:
        return RedirectResponse(prefixed("login_page"), status_code=303)

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
            return RedirectResponse(prefixed("chat_page"), status_code=303)
        return login_redirect()

    @app.get("/login", name="login_page")
    def login_page(request: Request, msg: str = ""):
        if current_session(request):
            return RedirectResponse(prefixed("chat_page"), status_code=303)
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
        resp = RedirectResponse(prefixed("chat_page"), status_code=303)
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
        resp = RedirectResponse(prefixed("login_page"), status_code=303)
        resp.delete_cookie(COOKIE_NAME, path=settings.root_path or "/")
        return resp

    @app.get("/home", name="home_page")
    def home_page(request: Request):
        session = current_session(request)
        if session is None:
            return login_redirect()
        return templates.TemplateResponse(request, "home.html", {"session": session})

    return app