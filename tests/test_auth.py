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


def test_two_logins_have_distinct_coze_user_ids(app):
    with TestClient(app) as c1, TestClient(app) as c2:
        login(c1)
        login(c2)
        rows = app.state.db.query("SELECT coze_user_id FROM sessions")
        ids = [r["coze_user_id"] for r in rows]
        assert len(ids) == 2 and ids[0] != ids[1]
        assert all(i.startswith("mcu-") for i in ids)


def test_password_change_revokes_all_sessions(app):
    with TestClient(app) as c1, TestClient(app) as c2:
        login(c1)
        login(c2)
        r = c1.post(
            "/settings/password",
            data={"current": PASSWORD, "new": "new-pass-456"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert c1.get("/home", follow_redirects=False).status_code == 303
        assert c2.get("/home", follow_redirects=False).status_code == 303
        assert login(c1, password=PASSWORD).status_code == 401
        assert login(c1, password="new-pass-456").status_code == 303


def test_password_change_wrong_current(app, client):
    login(client)
    r = client.post(
        "/settings/password",
        data={"current": "bad", "new": "new-pass-456"},
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert client.get("/home", follow_redirects=False).status_code == 200


def test_password_change_too_short(app, client):
    login(client)
    r = client.post(
        "/settings/password",
        data={"current": PASSWORD, "new": "short"},
        follow_redirects=False,
    )
    assert r.status_code == 400


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