"""登录/会话/改密行为的自动化验证（全部本地模拟，不触网）。"""
from fastapi.testclient import TestClient

from app.auth import COOKIE_NAME, hash_password, verify_password
from app.config import REMEMBER_SECONDS, Settings
from app.main import create_app
from conftest import PASSWORD, login


def test_login_page_ok(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "记住登录" in r.text


def test_wrong_password_rejected(client):
    r = login(client, password="wrong-password")
    assert r.status_code == 401
    assert COOKIE_NAME not in r.cookies


def test_login_success_sets_cookie(client):
    r = login(client)
    assert r.status_code == 303
    assert r.cookies.get(COOKIE_NAME)
    assert client.get("/home", follow_redirects=False).status_code == 200


def test_home_requires_login(client):
    r = client.get("/home", follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers["location"]


def test_remember_cookie_max_age_15_days(client):
    r = login(client, remember=True)
    set_cookie = r.headers["set-cookie"]
    assert f"Max-Age={REMEMBER_SECONDS}" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie


def test_no_remember_is_session_cookie(client):
    r = login(client, remember=False)
    assert "Max-Age" not in r.headers["set-cookie"]


def test_logout_revokes_server_side(app, client):
    login(client)
    token = client.cookies.get(COOKIE_NAME)
    client.post("/logout", follow_redirects=False)
    # 用旧 token 直接构造 Cookie：服务端必须已撤销
    client.cookies.set(COOKIE_NAME, token)
    assert client.get("/home", follow_redirects=False).status_code == 303


def test_server_side_expiry_12h(app, client):
    login(client)
    app.state.db.execute("UPDATE sessions SET expires_at=0")
    assert client.get("/home", follow_redirects=False).status_code == 303


def test_two_logins_share_account_coze_user_id(app):
    """账号级历史共享：同一账号的不同登录共用稳定的 coze_user_id（跨设备追问连续）。"""
    with TestClient(app) as c1, TestClient(app) as c2:
        login(c1)
        login(c2)
        rows = app.state.db.query("SELECT coze_user_id FROM sessions")
        ids = [r["coze_user_id"] for r in rows]
        assert len(ids) == 2 and ids[0] == ids[1]
        assert all(i.startswith("mcu-") for i in ids)


def test_rotate_password_revokes_all_sessions(app):
    with TestClient(app) as c1, TestClient(app) as c2:
        login(c1)
        login(c2)
        revoked = app.state.auth.rotate_password("mcu", "new-pass-456")
        assert revoked == 2
        assert c1.get("/home", follow_redirects=False).status_code == 303
        assert c2.get("/home", follow_redirects=False).status_code == 303
        assert login(c1, password=PASSWORD).status_code == 401
        assert login(c1, password="new-pass-456").status_code == 303


def test_no_self_service_password_endpoint(app, client):
    """共享账号的普通登录会话不得拥有改密接口（P1 审阅修复）。"""
    login(client)
    r = client.post(
        "/settings/password",
        data={"current": PASSWORD, "new": "new-pass-456"},
        follow_redirects=False,
    )
    assert r.status_code in (404, 405)
    # 密码未被改动，会话仍有效
    assert client.get("/home", follow_redirects=False).status_code == 200
    assert login(client, password=PASSWORD).status_code == 303


def test_rotate_password_too_short(app):
    try:
        app.state.auth.rotate_password("mcu", "short")
        raise AssertionError("应当拒绝过短密码")
    except ValueError:
        pass


def test_rotate_password_script(app, tmp_path):
    """运维脚本端到端：--password-file 轮换后旧会话失效、新密码可登录。"""
    import subprocess
    import sys
    from pathlib import Path

    with TestClient(app) as c:
        login(c)
        pw_file = tmp_path / "new-pw.txt"
        pw_file.write_text("script-pass-789", encoding="utf-8")
        repo = Path(__file__).resolve().parents[1]
        env = {
            "MCU_DATA_DIR": str(app.state.settings.data_dir),
            "MCU_UPLOAD_DIR": str(app.state.settings.upload_dir),
            "MCU_USERNAME": "mcu",
            "MCU_PASSWORD": PASSWORD,
            "PATH": "/usr/bin:/bin",
        }
        r = subprocess.run(
            [sys.executable, str(repo / "scripts" / "rotate_password.py"),
             "--password-file", str(pw_file)],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert r.returncode == 0, r.stderr
        assert "撤销 1 个旧登录会话" in r.stdout
        assert c.get("/home", follow_redirects=False).status_code == 303
        assert login(c, password="script-pass-789").status_code == 303


def test_first_boot_requires_password(tmp_path):
    s = Settings(data_dir=tmp_path / "d", upload_dir=tmp_path / "u", password="")
    try:
        create_app(s)
        raise AssertionError("应当因缺少初始密码而失败")
    except RuntimeError as exc:
        assert "MCU_PASSWORD" in str(exc)


def test_password_hash_roundtrip():
    stored = hash_password("abc12345")
    assert verify_password("abc12345", stored)
    assert not verify_password("abc123456", stored)
    assert not verify_password("abc12345", "garbage")


def test_root_path_urls(tmp_path):
    s = Settings(
        data_dir=tmp_path / "d",
        upload_dir=tmp_path / "u",
        password=PASSWORD,
        secure_cookies=False,
        root_path="/mcu-xiaozhi",
    )
    app = create_app(s)
    with TestClient(app, root_path="/mcu-xiaozhi") as c:
        r = c.get("/login")
        assert "/mcu-xiaozhi/static/style.css" in r.text
        r = login(c)
        assert "Path=/mcu-xiaozhi" in r.headers["set-cookie"]
        # 重定向 Location 必须带反代前缀
        assert r.headers["location"] == "/mcu-xiaozhi/chat"
        # TestClient 用裸路径模拟「反代剥前缀」；浏览器实际都在前缀下，
        # 故把 Cookie 改挂到 / 以便裸路径请求也能携带。
        token = c.cookies.get(COOKIE_NAME)
        c.cookies.set(COOKIE_NAME, token, path="/")
        r = c.get("/", follow_redirects=False)
        assert r.headers["location"] == "/mcu-xiaozhi/chat"
        r = c.post("/logout", follow_redirects=False)
        assert r.headers["location"] == "/mcu-xiaozhi/login"
        r = c.get("/chat", follow_redirects=False)
        assert r.headers["location"] == "/mcu-xiaozhi/login"