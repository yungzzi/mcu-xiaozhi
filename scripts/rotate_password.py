#!/usr/bin/env python3
"""运维操作：轮换站内共享密码并撤销全部旧登录。

仅限部署管理员在服务器上执行（需要能读写 SQLite 数据目录）：

    # 宿主机直接执行
    MCU_DATA_DIR=/srv/mcu-xiaozhi/data .venv/bin/python scripts/rotate_password.py

    # 或容器内执行（数据卷已挂载）
    docker compose exec mcu-xiaozhi python scripts/rotate_password.py

新密码交互输入（不回显）；也可用 --password-file 从受限文件读取。
轮换后所有设备的旧登录立即失效，需用新密码重新登录。
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth import AuthService
from app.config import Settings
from app.db import Database


def main() -> int:
    parser = argparse.ArgumentParser(description="轮换共享密码（运维操作）")
    parser.add_argument("--password-file", help="从受限文件读取新密码（默认交互输入）")
    args = parser.parse_args()

    if args.password_file:
        new = Path(args.password_file).read_text(encoding="utf-8").strip()
    else:
        new = getpass.getpass("新密码（至少 8 位）: ")
        confirm = getpass.getpass("再次输入: ")
        if new != confirm:
            print("两次输入不一致", file=sys.stderr)
            return 1

    settings = Settings.from_env()
    db = Database(settings.db_path)
    auth = AuthService(db, settings)
    try:
        revoked = auth.rotate_password(settings.username, new)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        db.close()
    print(f"密码已轮换，撤销 {revoked} 个旧登录会话。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())