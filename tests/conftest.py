import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

PASSWORD = "test-pass-123"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        data_dir=tmp_path / "data",
        upload_dir=tmp_path / "uploads",
        password=PASSWORD,
        secure_cookies=False,
        coze_mode="mock",
    )


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def login(client, remember=False, password=PASSWORD, username="mcu"):
    data = {"username": username, "password": password}
    if remember:
        data["remember"] = "on"
    return client.post("/login", data=data, follow_redirects=False)