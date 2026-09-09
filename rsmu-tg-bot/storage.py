"""Локальное хранилище: избранное, настройки, конспекты, сессии ГС, фидбек."""
from __future__ import annotations

import json
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
            CREATE TABLE IF NOT EXISTS favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                ref TEXT NOT NULL,
                title TEXT NOT NULL,
                payload TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, kind, ref)
            );
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                group_id TEXT,
                reminders INTEGER DEFAULT 0,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS notes_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT,
                notes TEXT NOT NULL,
                transcript TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS voice_sessions (
                user_id INTEGER PRIMARY KEY,
                active INTEGER DEFAULT 0,
                chunks TEXT,
                started_at TEXT
            );
            CREATE TABLE IF NOT EXISTS feedback_wait (
                user_id INTEGER PRIMARY KEY,
                waiting INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS reminder_sent (
                user_id INTEGER NOT NULL,
                group_id TEXT NOT NULL,
                day_id TEXT NOT NULL,
                time_key TEXT NOT NULL,
                date_key TEXT NOT NULL,
                PRIMARY KEY (user_id, group_id, day_id, time_key, date_key)
            );
            """
        )


# ---- favorites ----
def fav_add(user_id: int, kind: str, ref: str, title: str, payload: dict | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO favorites (user_id, kind, ref, title, payload, created_at) VALUES (?,?,?,?,?,?)",
            (user_id, kind, ref, title, json.dumps(payload or {}, ensure_ascii=False), _now()),
        )


def fav_remove(user_id: int, kind: str, ref: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM favorites WHERE user_id=? AND kind=? AND ref=?", (user_id, kind, ref))


def fav_has(user_id: int, kind: str, ref: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM favorites WHERE user_id=? AND kind=? AND ref=?",
            (user_id, kind, ref),
        ).fetchone()
        return bool(row)


def fav_list(user_id: int, limit: int = 50) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM favorites WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d.get("payload") or "{}")
            except Exception:
                d["payload"] = {}
            out.append(d)
        return out


# ---- settings / reminders ----
def set_group(user_id: int, group_id: str | None) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO user_settings (user_id, group_id, reminders, updated_at)
            VALUES (?, ?, COALESCE((SELECT reminders FROM user_settings WHERE user_id=?), 0), ?)
            ON CONFLICT(user_id) DO UPDATE SET group_id=excluded.group_id, updated_at=excluded.updated_at
            """,
            (user_id, group_id, user_id, _now()),
        )


def set_reminders(user_id: int, on: bool) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO user_settings (user_id, group_id, reminders, updated_at)
            VALUES (?, NULL, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET reminders=excluded.reminders, updated_at=excluded.updated_at
            """,
            (user_id, 1 if on else 0, _now()),
        )


def get_settings(user_id: int) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else {"user_id": user_id, "group_id": None, "reminders": 0}


def users_with_reminders() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM user_settings WHERE reminders=1 AND group_id IS NOT NULL AND group_id != ''"
        ).fetchall()
        return [dict(r) for r in rows]


def reminder_was_sent(user_id: int, group_id: str, day_id: str, time_key: str, date_key: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM reminder_sent WHERE user_id=? AND group_id=? AND day_id=? AND time_key=? AND date_key=?",
            (user_id, group_id, day_id, time_key, date_key),
        ).fetchone()
        return bool(row)


def reminder_mark_sent(user_id: int, group_id: str, day_id: str, time_key: str, date_key: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO reminder_sent (user_id, group_id, day_id, time_key, date_key) VALUES (?,?,?,?,?)",
            (user_id, group_id, day_id, time_key, date_key),
        )


# ---- notes history ----
def notes_save(user_id: int, notes: str, transcript: str | None = None, title: str | None = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO notes_history (user_id, title, notes, transcript, created_at) VALUES (?,?,?,?,?)",
            (user_id, title or "Конспект", notes, transcript, _now()),
        )
        return int(cur.lastrowid)


def notes_list(user_id: int, limit: int = 15) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, title, created_at, substr(notes,1,120) AS preview FROM notes_history WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def notes_get(user_id: int, note_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM notes_history WHERE user_id=? AND id=?",
            (user_id, note_id),
        ).fetchone()
        return dict(row) if row else None


# ---- voice session ----
def session_start(user_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO voice_sessions (user_id, active, chunks, started_at) VALUES (?,?,?,?)",
            (user_id, 1, "[]", _now()),
        )


def session_stop(user_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE voice_sessions SET active=0, chunks=? WHERE user_id=?",
            ("[]", user_id),
        )


def session_is_active(user_id: int) -> bool:
    with connect() as conn:
        row = conn.execute("SELECT active FROM voice_sessions WHERE user_id=?", (user_id,)).fetchone()
        return bool(row and row["active"])


def session_add_transcript(user_id: int, text: str) -> int:
    with connect() as conn:
        row = conn.execute("SELECT chunks, active FROM voice_sessions WHERE user_id=?", (user_id,)).fetchone()
        if not row or not row["active"]:
            return 0
        chunks = json.loads(row["chunks"] or "[]")
        chunks.append(text)
        conn.execute("UPDATE voice_sessions SET chunks=? WHERE user_id=?", (json.dumps(chunks, ensure_ascii=False), user_id))
        return len(chunks)


def session_get_chunks(user_id: int) -> list[str]:
    with connect() as conn:
        row = conn.execute("SELECT chunks FROM voice_sessions WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return []
        return json.loads(row["chunks"] or "[]")


# ---- feedback ----
def feedback_set_waiting(user_id: int, waiting: bool) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO feedback_wait (user_id, waiting) VALUES (?,?)",
            (user_id, 1 if waiting else 0),
        )


def feedback_is_waiting(user_id: int) -> bool:
    with connect() as conn:
        row = conn.execute("SELECT waiting FROM feedback_wait WHERE user_id=?", (user_id,)).fetchone()
        return bool(row and row["waiting"])
