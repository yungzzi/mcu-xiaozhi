#!/usr/bin/env python3
"""从升级前备份找回扣子会话映射（备份始终只读）。

背景：0.2.0 升级曾把旧聊天的 coze_conversation_id 置空；升级前备份里仍保留
旧会话 ID 与旧登录的 coze_user_id。本脚本把映射找回写入当前库，供新版迁移
保留原会话或标记上下文重建。

用法（服务器上；--apply 须在应用容器停止时执行）：
  python3 recover_coze_mapping.py --backup /path/to/backup/mcu.sqlite3           # 只读统计
  python3 recover_coze_mapping.py --backup /path/to/backup/mcu.sqlite3 --apply   # 写入当前库

当前库路径：MCU_DATA_DIR（默认 ./data）/mcu.sqlite3。
只处理 coze_conversation_id 与 coze_user_id 均为空且未标记重建的聊天；幂等。
找回旧登录身份的 → 保留旧 ID 对续聊；找不到的 → 旧会话 ID 存 legacy 并标记
context_restore（首次续问自动带近期历史重建）。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path


def _ensure_columns(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(conversations)")}
    if "coze_user_id" not in cols:
        conn.execute("ALTER TABLE conversations ADD COLUMN coze_user_id TEXT")
    if "context_restore" not in cols:
        conn.execute("ALTER TABLE conversations ADD COLUMN context_restore INTEGER NOT NULL DEFAULT 0")
    if "legacy_coze_conversation_id" not in cols:
        conn.execute("ALTER TABLE conversations ADD COLUMN legacy_coze_conversation_id TEXT")


def recover(backup_path: Path, live_path: Path, apply: bool = False) -> dict:
    stats = {"recoverable": 0, "conv_only": 0, "not_in_backup": 0, "no_context": 0, "skipped": 0}
    b = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    b.row_factory = sqlite3.Row
    live_uri = f"file:{live_path}?mode=ro" if not apply else str(live_path)
    l = sqlite3.connect(live_uri, uri=not apply)
    l.row_factory = sqlite3.Row
    try:
        if apply:
            _ensure_columns(l)
        # 只读统计时当前库可能还没有新列（如 0.2.1）：缺失列按空值处理
        live_cols = {r[1] for r in l.execute("PRAGMA table_info(conversations)")}
        sel_cuid = "coze_user_id" if "coze_user_id" in live_cols else "NULL AS coze_user_id"
        sel_restore = "context_restore" if "context_restore" in live_cols else "0 AS context_restore"
        sel_legacy = ("legacy_coze_conversation_id" if "legacy_coze_conversation_id" in live_cols
                      else "NULL AS legacy_coze_conversation_id")
        for row in l.execute(
            f"SELECT id, created_at, coze_conversation_id, {sel_cuid},"
            f" {sel_restore}, {sel_legacy} FROM conversations"
        ):
            if (row["coze_conversation_id"] is not None or row["coze_user_id"] is not None
                    or row["context_restore"] or row["legacy_coze_conversation_id"] is not None):
                stats["skipped"] += 1
                continue
            old = b.execute(
                "SELECT token_hash, coze_conversation_id, created_at FROM conversations WHERE id=?",
                (row["id"],),
            ).fetchone()
            if old is None or abs(old["created_at"] - row["created_at"]) > 1e-6:
                stats["not_in_backup"] += 1
                continue
            if not old["coze_conversation_id"]:
                stats["no_context"] += 1
                continue
            sess = b.execute(
                "SELECT coze_user_id FROM sessions WHERE token_hash=?", (old["token_hash"],)
            ).fetchone()
            if sess is not None:
                stats["recoverable"] += 1
                if apply:
                    l.execute(
                        "UPDATE conversations SET coze_conversation_id=?, coze_user_id=? WHERE id=?",
                        (old["coze_conversation_id"], sess["coze_user_id"], row["id"]),
                    )
            else:
                stats["conv_only"] += 1
                if apply:
                    l.execute(
                        "UPDATE conversations SET legacy_coze_conversation_id=?, context_restore=1 WHERE id=?",
                        (old["coze_conversation_id"], row["id"]),
                    )
        if apply:
            l.commit()
    finally:
        b.close()
        l.close()
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backup", required=True, help="升级前备份的 mcu.sqlite3 路径（只读）")
    ap.add_argument("--apply", action="store_true", help="写入当前库（默认只读统计；须先停应用容器）")
    args = ap.parse_args()
    live = Path(os.environ.get("MCU_DATA_DIR", "./data")) / "mcu.sqlite3"
    stats = recover(Path(args.backup), live, apply=args.apply)
    mode = "写入" if args.apply else "只读统计"
    print(f"[{mode}] 可找回旧身份续聊: {stats['recoverable']}；"
          f"仅有旧会话ID(将重建上下文): {stats['conv_only']}；"
          f"备份中无此聊天: {stats['not_in_backup']}；"
          f"备份中无扣子上下文: {stats['no_context']}；"
          f"已有映射跳过: {stats['skipped']}")


if __name__ == "__main__":
    main()