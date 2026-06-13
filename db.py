import sqlite3
import logging
from datetime import datetime
from typing import Optional

DB_PATH = "tasks.db"
logger = logging.getLogger(__name__)


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id          INTEGER NOT NULL,
                text             TEXT    NOT NULL,
                deadline         TEXT    NOT NULL,
                status           TEXT    NOT NULL DEFAULT 'active',
                notified         INTEGER NOT NULL DEFAULT 0,
                reminder_minutes INTEGER NOT NULL DEFAULT 10,
                recurrence       TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id     INTEGER PRIMARY KEY,
                digest_time TEXT NOT NULL DEFAULT 'off'
            )
            """
        )
        _migrate(conn)
        conn.commit()
    logger.info("Database initialised.")


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    additions = {
        "status": "TEXT NOT NULL DEFAULT 'active'",
        "reminder_minutes": "INTEGER NOT NULL DEFAULT 10",
        "recurrence": "TEXT",
    }
    for col, definition in additions.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {definition}")
            logger.info("Migrated: added column tasks.%s", col)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

def save_task(
    user_id: int,
    text: str,
    deadline: datetime,
    reminder_minutes: int = 10,
    recurrence: Optional[str] = None,
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO tasks (user_id, text, deadline, reminder_minutes, recurrence)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, text, deadline.isoformat(), reminder_minutes, recurrence),
        )
        conn.commit()
    task_id: int = cur.lastrowid
    logger.info("Saved task %d for user %d: '%s' @ %s", task_id, user_id, text, deadline)
    return task_id


def get_task(task_id: int) -> Optional[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def get_pending_tasks(user_id: Optional[int] = None) -> list[sqlite3.Row]:
    with get_connection() as conn:
        if user_id is not None:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE user_id=? AND status='active' AND notified=0 ORDER BY deadline",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status='active' AND notified=0 ORDER BY deadline"
            ).fetchall()
    return rows


def get_tasks_for_range(
    user_id: int, start: datetime, end: datetime
) -> list[sqlite3.Row]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM tasks
               WHERE user_id=? AND status='active'
               AND deadline >= ? AND deadline < ?
               ORDER BY deadline""",
            (user_id, start.isoformat(), end.isoformat()),
        ).fetchall()
    return rows


def get_done_tasks(user_id: int, limit: int = 20) -> list[sqlite3.Row]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE user_id=? AND status='done' ORDER BY deadline DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return rows


def mark_notified(task_id: int) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE tasks SET notified=1 WHERE id=?", (task_id,))
        conn.commit()
    logger.info("Marked task %d as notified.", task_id)


def mark_done(task_id: int, user_id: int) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE tasks SET status='done' WHERE id=? AND user_id=? AND status='active'",
            (task_id, user_id),
        )
        conn.commit()
    done = cur.rowcount > 0
    if done:
        logger.info("Marked task %d as done.", task_id)
    return done


def delete_task(task_id: int, user_id: int) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            "DELETE FROM tasks WHERE id=? AND user_id=?",
            (task_id, user_id),
        )
        conn.commit()
    deleted = cur.rowcount > 0
    if deleted:
        logger.info("Deleted task %d for user %d.", task_id, user_id)
    return deleted


def update_task(
    task_id: int, user_id: int, text: str, deadline: datetime
) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            """UPDATE tasks SET text=?, deadline=?, notified=0
               WHERE id=? AND user_id=? AND status='active'""",
            (text, deadline.isoformat(), task_id, user_id),
        )
        conn.commit()
    updated = cur.rowcount > 0
    if updated:
        logger.info("Updated task %d.", task_id)
    return updated


# ---------------------------------------------------------------------------
# User settings
# ---------------------------------------------------------------------------

def get_digest_time(user_id: int) -> str:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT digest_time FROM user_settings WHERE user_id=?", (user_id,)
        ).fetchone()
    return row["digest_time"] if row else "off"


def set_digest_time(user_id: int, digest_time: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO user_settings (user_id, digest_time) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET digest_time=excluded.digest_time""",
            (user_id, digest_time),
        )
        conn.commit()
    logger.info("Set digest_time=%s for user %d.", digest_time, user_id)


def get_all_digest_settings() -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM user_settings WHERE digest_time != 'off'"
        ).fetchall()


def get_all_users() -> list[int]:
    with get_connection() as conn:
        rows = conn.execute("SELECT DISTINCT user_id FROM tasks").fetchall()
    return [row["user_id"] for row in rows]
