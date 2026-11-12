"""应用配置：全部来自环境变量或受限 secret 文件，仓库内只有占位值。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REMEMBER_SECONDS = 15 * 24 * 3600  # 记住登录：从登录起 15 天
SESSION_SECONDS = 12 * 3600        # 未记住：服务端最长 12 小时


def _read_secret_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path("data"))
    upload_dir: Path = field(default_factory=lambda: Path("uploads"))
    root_path: str = ""
    username: str = "mcu"
    password: str = ""  # 仅首次初始化用；之后以数据库哈希为准
    secure_cookies: bool = True
    coze_mode: str = "mock"  # mock | real
    coze_api_base: str = "https://api.coze.cn"
    coze_bot_id: str = ""
    coze_token: str = ""
    max_upload_bytes: int = 8 * 1024 * 1024

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mcu.sqlite3"

    @classmethod
    def from_env(cls) -> "Settings":
        password = os.environ.get("MCU_PASSWORD", "")
        pw_file = os.environ.get("MCU_PASSWORD_FILE", "")
        if pw_file:
            password = _read_secret_file(pw_file)
        token = os.environ.get("COZE_TOKEN", "")
        token_file = os.environ.get("COZE_TOKEN_FILE", "")
        if token_file:
            token = _read_secret_file(token_file)
        return cls(
            data_dir=Path(os.environ.get("MCU_DATA_DIR", "data")),
            upload_dir=Path(os.environ.get("MCU_UPLOAD_DIR", "uploads")),
            root_path=os.environ.get("MCU_ROOT_PATH", "").rstrip("/"),
            username=os.environ.get("MCU_USERNAME", "mcu"),
            password=password,
            secure_cookies=os.environ.get("MCU_SECURE_COOKIES", "1") == "1",
            coze_mode=os.environ.get("COZE_MODE", "mock"),
            coze_api_base=os.environ.get("COZE_API_BASE", "https://api.coze.cn"),
            coze_bot_id=os.environ.get("COZE_BOT_ID", ""),
            coze_token=token,
            max_upload_bytes=int(os.environ.get("MCU_MAX_UPLOAD_BYTES", str(8 * 1024 * 1024))),
        )