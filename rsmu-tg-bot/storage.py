"""SQLite storage: users + message log for admin."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "bot_data.db"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                first_seen TEXT,
                last_seen TEXT,
                msg_count INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                direction TEXT NOT NULL,
                content_type TEXT DEFAULT 'text',
                text TEXT,
                file_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, id);
            """
        )


def upsert_user(user_id: int, username: str | None, full_name: str) -> None:
    now = _now()
    with connect() as conn:
        row = conn.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            conn.execute(
                "UPDATE users SET username=?, full_name=?, last_seen=?, msg_count=msg_count+1 WHERE user_id=?",
                (username, full_name, now, user_id),
            )
        else:
            conn.execute(
                "INSERT INTO users (user_id, username, full_name, first_seen, last_seen, msg_count) VALUES (?,?,?,?,?,1)",
                (user_id, username, full_name, now, now),
            )


def log_message(
    user_id: int,
    direction: str,
    text: str | None = None,
    content_type: str = "text",
    file_id: str | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO messages (user_id, direction, content_type, text, file_id, created_at) VALUES (?,?,?,?,?,?)",
            (user_id, direction, content_type, text, file_id, _now()),
        )


def list_users(limit: int = 50, offset: int = 0) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY last_seen DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def count_users() -> int:
    with connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def get_user(user_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_messages(user_id: int, limit: int = 200) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        # chronological
        return [dict(r) for r in reversed(rows)]


def export_html(user_id: int) -> str:
    user = get_user(user_id) or {"user_id": user_id, "full_name": "?", "username": ""}
    msgs = get_messages(user_id, limit=1000)
    uname = f"@{user['username']}" if user.get("username") else "—"
    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        f"<title>Chat {user_id}</title>",
        "<style>body{font-family:sans-serif;max-width:800px;margin:20px auto;background:#1a1a1a;color:#eee}"
        ".in{background:#2a2a2a;padding:8px 12px;margin:6px 0;border-radius:8px;border-left:3px solid #4af}"
        ".out{background:#1e3a2f;padding:8px 12px;margin:6px 0;border-radius:8px;border-left:3px solid #4f4}"
        ".admin{background:#3a2a1e;padding:8px 12px;margin:6px 0;border-radius:8px;border-left:3px solid #fa4}"
        ".meta{font-size:12px;color:#888}</style></head><body>",
        f"<h1>Переписка с ботом</h1>",
        f"<p><b>{_esc(user.get('full_name'))}</b> { _esc(uname) } · id <code>{user_id}</code></p>",
        f"<p class='meta'>Сообщений в логе: {len(msgs)}</p><hr>",
    ]
    for m in msgs:
        cls = m["direction"] if m["direction"] in ("in", "out", "admin") else "in"
        label = {"in": "Пользователь", "out": "Бот", "admin": "Админ"}.get(cls, cls)
        body = _esc(m.get("text") or "")
        extra = ""
        if m.get("file_id"):
            extra = f"<div class='meta'>[{_esc(m.get('content_type') or 'media')}] file_id: {_esc(m['file_id'])}</div>"
        parts.append(
            f"<div class='{cls}'><div class='meta'>{label} · {_esc(m.get('created_at') or '')}</div>"
            f"<div>{body or '<i>(медиа)</i>'}</div>{extra}</div>"
        )
    parts.append("</body></html>")
    return "\n".join(parts)


def _esc(s) -> str:
    import html as h

    return h.escape(str(s) if s is not None else "")
