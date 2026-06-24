import random
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
                recurrence       TEXT,
                priority         INTEGER NOT NULL DEFAULT 0,
                category         TEXT
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS family_links (
                user_id    INTEGER NOT NULL,
                partner_id INTEGER NOT NULL,
                PRIMARY KEY (user_id, partner_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS family_invites (
                code       TEXT    PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                created_at TEXT    NOT NULL
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
        "priority": "INTEGER NOT NULL DEFAULT 0",
        "category": "TEXT",
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
    reminder_minutes: Optional[int] = None,
    recurrence: Optional[str] = None,
    priority: int = 0,
    category: Optional[str] = None,
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO tasks (user_id, text, deadline, reminder_minutes, recurrence, priority, category)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, text, deadline.isoformat(), reminder_minutes or 0, recurrence, priority, category),
        )
        conn.commit()
    task_id: int = cur.lastrowid
    logger.info("Saved task %d for user %d: '%s' @ %s (priority=%d, cat=%s)",
                task_id, user_id, text, deadline, priority, category)
    return task_id


def get_task(task_id: int) -> Optional[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def get_pending_tasks(user_id: Optional[int] = None) -> list[sqlite3.Row]:
    with get_connection() as conn:
        if user_id is not None:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE user_id=? AND status='active' AND notified=0 ORDER BY priority DESC, deadline",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status='active' AND notified=0 ORDER BY priority DESC, deadline"
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
               ORDER BY priority DESC, deadline""",
            (user_id, start.isoformat(), end.isoformat()),
        ).fetchall()
    return rows


def get_overdue_tasks(user_id: int) -> list[sqlite3.Row]:
    now = datetime.now().astimezone()
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM tasks
               WHERE user_id=? AND status='active' AND deadline < ?
               ORDER BY priority DESC, deadline""",
            (user_id, now.isoformat()),
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


def set_task_category(task_id: int, category: Optional[str]) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE tasks SET category=? WHERE id=?", (category, task_id))
        conn.commit()
    logger.info("Set category=%s for task %d.", category, task_id)


def set_task_priority(task_id: int, priority: int) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE tasks SET priority=? WHERE id=?", (priority, task_id))
        conn.commit()
    logger.info("Set priority=%d for task %d.", priority, task_id)


def reschedule_task(task_id: int, new_deadline: datetime) -> bool:
    """Переносит дедлайн задачи (для снуза и /overdue reschedule)."""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE tasks SET deadline=?, notified=0 WHERE id=? AND status='active'",
            (new_deadline.isoformat(), task_id),
        )
        conn.commit()
    updated = cur.rowcount > 0
    if updated:
        logger.info("Rescheduled task %d to %s.", task_id, new_deadline)
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


# ---------------------------------------------------------------------------
# Family linking
# ---------------------------------------------------------------------------

def create_family_invite(user_id: int) -> str:
    """Создаёт 6-значный код приглашения. Старые коды пользователя удаляются."""
    code = str(random.randint(100000, 999999))
    now_iso = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute("DELETE FROM family_invites WHERE user_id=?", (user_id,))
        conn.execute(
            "INSERT INTO family_invites (code, user_id, created_at) VALUES (?, ?, ?)",
            (code, user_id, now_iso),
        )
        conn.commit()
    logger.info("Created family invite code %s for user %d.", code, user_id)
    return code


def accept_family_invite(code: str, acceptor_id: int) -> Optional[int]:
    """Принимает приглашение. Возвращает user_id отправителя или None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT user_id FROM family_invites WHERE code=?", (code,)
        ).fetchone()
        if not row:
            return None
        inviter_id: int = row["user_id"]
        if inviter_id == acceptor_id:
            return None
        conn.execute("DELETE FROM family_invites WHERE code=?", (code,))
        # Двусторонняя связь
        conn.execute(
            "INSERT OR REPLACE INTO family_links (user_id, partner_id) VALUES (?, ?)",
            (inviter_id, acceptor_id),
        )
        conn.execute(
            "INSERT OR REPLACE INTO family_links (user_id, partner_id) VALUES (?, ?)",
            (acceptor_id, inviter_id),
        )
        conn.commit()
    logger.info("Family linked: %d <-> %d.", inviter_id, acceptor_id)
    return inviter_id


def get_family_partner(user_id: int) -> Optional[int]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT partner_id FROM family_links WHERE user_id=?", (user_id,)
        ).fetchone()
    return row["partner_id"] if row else None


def unlink_family(user_id: int) -> None:
    partner_id = get_family_partner(user_id)
    with get_connection() as conn:
        conn.execute("DELETE FROM family_links WHERE user_id=? OR partner_id=?", (user_id, user_id))
        conn.commit()
    logger.info("Unlinked family for user %d (partner was %s).", user_id, partner_id)
