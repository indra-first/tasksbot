import asyncio
import logging
import os
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db


# ---------------------------------------------------------------------------
# .env loading (works regardless of cwd, handles BOM and quoted keys/values)
# ---------------------------------------------------------------------------

def _read_env_file(path: str) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8-sig") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                idx = line.index("=")
                key = line[:idx].strip().strip("'\"")
                val = line[idx + 1 :].strip().strip("'\"")
                if key:
                    result[key] = val
    except FileNotFoundError:
        pass
    return result


_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
_ENV = _read_env_file(_ENV_PATH)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("Europe/Moscow")

WEEKDAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

RECURRENCE_LABELS = {
    "daily": "каждый день",
    "weekly": "каждую неделю",
    "biweekly": "каждые 2 недели",
    "monthly": "каждый месяц",
}

RECURRENCE_DAYS = {
    "daily": 1,
    "weekly": 7,
    "biweekly": 14,
    "monthly": 30,
}


def recurrence_delta(rec: str) -> timedelta:
    if rec.startswith("every:"):
        return timedelta(days=int(rec.split(":")[1]))
    return timedelta(days=RECURRENCE_DAYS.get(rec, 7))


def recurrence_label(rec: str) -> str:
    if rec.startswith("every:"):
        n = rec.split(":")[1]
        return f"каждые {n} дней"
    return RECURRENCE_LABELS.get(rec, rec)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

@dataclass
class ParsedTask:
    text: str
    deadline: datetime
    reminder_minutes: Optional[int] = None  # None = без напоминания
    recurrence: Optional[str] = None


WEEKDAYS_RU: dict[str, int] = {
    "понедельник": 0, "понедельника": 0,
    "вторник": 1, "вторника": 1,
    "среда": 2, "среды": 2, "среду": 2,
    "четверг": 3, "четверга": 3,
    "пятница": 4, "пятницы": 4, "пятницу": 4,
    "суббота": 5, "субботы": 5, "субботу": 5,
    "воскресенье": 6, "воскресенья": 6,
}

_RECURRENCE_RE = re.compile(
    r"\b("
    r"каждый\s+день|ежедневно"
    r"|каждую\s+неделю|еженедельно"
    r"|каждые\s+2\s+недели|раз\s+в\s+2\s+недели"
    r"|каждый\s+месяц|ежемесячно"
    r"|каждые\s+(\d+)\s+дней?"
    r")\b",
    re.IGNORECASE,
)

# "напомни за N минут/часов" — за сколько до дедлайна
_REMINDER_RE = re.compile(
    r"\bнапомни\s+за\s+(\d+)\s+(минут[уы]?|час[аов]*)\b",
    re.IGNORECASE,
)
# "напомни в HH:MM" — в конкретное время в тот же день
_REMINDER_AT_RE = re.compile(
    r"\bнапомни\s+в\s+(\d{1,2}):(\d{2})\b",
    re.IGNORECASE,
)

_DATE_TIME_RE = re.compile(
    r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\s+(\d{1,2}):(\d{2})\b"
)
_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_DURATION_RE = re.compile(
    r"\bчерез\s+(\d+)\s+(минут[уы]?|час[аов]*)\b", re.IGNORECASE
)
# "в 6 вечера", "в 9 утра", "в 3 дня", "в 11 ночи"
_AMPM_RE = re.compile(
    r"\bв\s+(\d{1,2})(?::(\d{2}))?\s+(утра|утром|дня|днём|вечера|вечером|ночи|ночью)\b",
    re.IGNORECASE,
)

def _ampm_to_hour(h: int, period: str) -> int:
    """Конвертирует '6 вечера' → 18, '9 утра' → 9, '11 ночи' → 23."""
    p = period.lower()
    if p in ("утра", "утром"):
        return h % 12  # 12 утра = 00:00, 6 утра = 06:00
    if p in ("дня", "днём", "вечера", "вечером"):
        return h if h == 12 else h + 12  # 12 дня = 12:00, 6 вечера = 18:00
    # ночи / ночью: 12 ночи = 00:00, 9-11 ночи = 21-23, 1-8 ночи = 01-08
    if h == 12:
        return 0
    return h + 12 if h >= 9 else h
_WEEKDAY_TIME_RE = re.compile(
    r"\bв\s+(" + "|".join(WEEKDAYS_RU) + r")\b(?:\s+(?:в\s+)?(\d{1,2}):(\d{2}))?",
    re.IGNORECASE,
)


def _now() -> datetime:
    return datetime.now(tz=LOCAL_TZ)


def _next_weekday(weekday: int, hour: int = 9, minute: int = 0) -> datetime:
    now = _now()
    days_ahead = (weekday - now.weekday()) % 7 or 7
    return (now + timedelta(days=days_ahead)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )


def _extract_recurrence(text: str) -> tuple[str, Optional[str]]:
    m = _RECURRENCE_RE.search(text)
    if not m:
        return text, None
    phrase = m.group(0).lower()
    if "каждый день" in phrase or "ежедневно" in phrase:
        rec = "daily"
    elif "каждую неделю" in phrase or "еженедельно" in phrase:
        rec = "weekly"
    elif "каждые 2 недели" in phrase or "раз в 2 недели" in phrase:
        rec = "biweekly"
    elif "каждый месяц" in phrase or "ежемесячно" in phrase:
        rec = "monthly"
    else:
        n = m.group(2) or "1"
        rec = f"every:{n}"
    return (text[: m.start()] + text[m.end() :]).strip(), rec


def _extract_reminder_relative(text: str) -> tuple[str, Optional[int]]:
    """Извлекает 'напомни за N минут/часов' → (очищенный текст, минуты | None)."""
    m = _REMINDER_RE.search(text)
    if not m:
        return text, None
    n = int(m.group(1))
    minutes = n * 60 if "час" in m.group(2).lower() else n
    return (text[: m.start()] + text[m.end() :]).strip(), minutes


def _extract_reminder_at(text: str) -> tuple[str, Optional[tuple[int, int]]]:
    """Извлекает 'напомни в HH:MM' → (очищенный текст, (hour, minute) | None)."""
    m = _REMINDER_AT_RE.search(text)
    if not m:
        return text, None
    return (text[: m.start()] + text[m.end() :]).strip(), (int(m.group(1)), int(m.group(2)))


def _resolve_reminder(
    deadline: datetime,
    relative_min: Optional[int],
    at_hm: Optional[tuple[int, int]],
) -> Optional[int]:
    """Возвращает кол-во минут до дедлайна, в которые нужно напомнить.
    None — напоминание не нужно."""
    if relative_min is not None:
        return relative_min
    if at_hm is not None:
        h, mn = at_hm
        remind_at = deadline.replace(hour=h, minute=mn, second=0, microsecond=0)
        diff = int((deadline - remind_at).total_seconds() / 60)
        return diff if diff > 0 else None
    return None


def parse_task(raw: str) -> Optional[ParsedTask]:
    text, recurrence = _extract_recurrence(raw)
    text, at_hm = _extract_reminder_at(text)
    text, rel_min = _extract_reminder_relative(text)
    now = _now()

    def _clean(t: str, m: re.Match) -> str:
        return (t[: m.start()] + t[m.end() :]).strip(" ,.-") or raw.strip()

    # DD.MM[.YYYY] HH:MM
    m = _DATE_TIME_RE.search(text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else now.year
        try:
            deadline = datetime(year, month, day, int(m.group(4)), int(m.group(5)), tzinfo=LOCAL_TZ)
        except ValueError:
            return None
        return ParsedTask(_clean(text, m), deadline, _resolve_reminder(deadline, rel_min, at_hm), recurrence)

    # через N минут / часов
    m = _DURATION_RE.search(text)
    if m:
        n = int(m.group(1))
        delta = timedelta(hours=n) if "час" in m.group(2).lower() else timedelta(minutes=n)
        deadline = now + delta
        return ParsedTask(_clean(text, m), deadline, _resolve_reminder(deadline, rel_min, at_hm), recurrence)

    # в [день недели] [HH:MM]
    m = _WEEKDAY_TIME_RE.search(text)
    if m:
        wd = WEEKDAYS_RU.get(m.group(1).lower())
        if wd is not None:
            h = int(m.group(2)) if m.group(2) else 9
            mn = int(m.group(3)) if m.group(3) else 0
            deadline = _next_weekday(wd, h, mn)
            return ParsedTask(_clean(text, m), deadline, _resolve_reminder(deadline, rel_min, at_hm), recurrence)

    lower = text.lower()

    # послезавтра HH:MM
    m_str = re.search(r"послезавтра\s+(\d{1,2}):(\d{2})", lower)
    if m_str:
        deadline = (now + timedelta(days=2)).replace(
            hour=int(m_str.group(1)), minute=int(m_str.group(2)), second=0, microsecond=0
        )
        desc = re.sub(r"послезавтра\s+\d{1,2}:\d{2}", "", text, flags=re.IGNORECASE).strip(" ,.-") or raw.strip()
        return ParsedTask(desc, deadline, _resolve_reminder(deadline, rel_min, at_hm), recurrence)

    # завтра HH:MM
    m_str = re.search(r"завтра\s+(\d{1,2}):(\d{2})", lower)
    if m_str:
        deadline = (now + timedelta(days=1)).replace(
            hour=int(m_str.group(1)), minute=int(m_str.group(2)), second=0, microsecond=0
        )
        desc = re.sub(r"завтра\s+\d{1,2}:\d{2}", "", text, flags=re.IGNORECASE).strip(" ,.-") or raw.strip()
        return ParsedTask(desc, deadline, _resolve_reminder(deadline, rel_min, at_hm), recurrence)

    # "в 6 вечера", "в 9 утра", "в 3 дня", "в 11 ночи"
    m = _AMPM_RE.search(text)
    if m:
        h = _ampm_to_hour(int(m.group(1)), m.group(3))
        mn = int(m.group(2)) if m.group(2) else 0
        deadline = now.replace(hour=h, minute=mn, second=0, microsecond=0)
        if deadline <= now:
            deadline += timedelta(days=1)
        return ParsedTask(_clean(text, m), deadline, _resolve_reminder(deadline, rel_min, at_hm), recurrence)

    # сегодня HH:MM или просто HH:MM
    m = _TIME_RE.search(text)
    if m:
        deadline = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if deadline <= now:
            deadline += timedelta(days=1)
        return ParsedTask(_clean(text, m), deadline, _resolve_reminder(deadline, rel_min, at_hm), recurrence)

    return None


# ---------------------------------------------------------------------------
# Job callbacks
# ---------------------------------------------------------------------------

async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    data: dict = context.job.data
    user_id: int = data["user_id"]
    task_id: int = data["task_id"]
    task_text: str = data["task_text"]
    deadline: datetime = data["deadline"]
    recurrence: Optional[str] = data.get("recurrence")
    reminder_minutes: int = data.get("reminder_minutes", 0)

    try:
        rec_note = f"\n↩ Повтор: {recurrence_label(recurrence)}" if recurrence else ""
        mins_left = int((deadline - _now()).total_seconds() / 60)
        time_note = f"(через {mins_left} мин.)" if mins_left > 0 else ""
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"🔔 *Напоминание!*\n\n"
                f"📝 *{task_text}*\n"
                f"🕐 Срок: {deadline.strftime('%d.%m.%Y %H:%M')} {time_note}{rec_note}"
            ),
            parse_mode="Markdown",
            reply_markup=_task_keyboard(task_id),
        )
        db.mark_notified(task_id)

        if recurrence:
            next_deadline = deadline + recurrence_delta(recurrence)
            new_id = db.save_task(user_id, task_text, next_deadline, reminder_minutes, recurrence)
            _schedule_task(context.application, new_id, user_id, task_text, next_deadline,
                           reminder_minutes, recurrence)
            logger.info("Created next recurrence task %d.", new_id)

    except Exception:
        logger.exception("Failed to send reminder for task %d.", task_id)


async def send_daily_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    data: dict = context.job.data
    user_id: int = data["user_id"]
    now = _now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    tasks = db.get_tasks_for_range(user_id, start, end)
    if not tasks:
        return

    lines = [f"📋 *Задачи на сегодня, {now.strftime('%d.%m.%Y')}* ({len(tasks)}):"]
    for i, t in enumerate(tasks, 1):
        dl = datetime.fromisoformat(t["deadline"])
        rec = f" ↩ {recurrence_label(t['recurrence'])}" if t["recurrence"] else ""
        lines.append(f"{i}. {t['text']} — 🕐 {dl.strftime('%H:%M')}{rec} [#{t['id']}]")

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text="\n".join(lines),
            parse_mode="Markdown",
        )
    except Exception:
        logger.exception("Failed to send digest to user %d.", user_id)


async def send_morning_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    data: dict = context.job.data
    user_id: int = data["user_id"]
    now = _now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    tasks = db.get_tasks_for_range(user_id, start, end)
    if not tasks:
        return

    lines = [f"☀️ *Доброе утро!*\n📋 Задачи на сегодня, {now.strftime('%d.%m.%Y')} ({len(tasks)}):"]
    for i, t in enumerate(tasks, 1):
        dl = datetime.fromisoformat(t["deadline"])
        rec = f" ↩ {recurrence_label(t['recurrence'])}" if t["recurrence"] else ""
        lines.append(f"{i}. {t['text']} — 🕐 {dl.strftime('%H:%M')}{rec} [#{t['id']}]")

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text="\n".join(lines),
            parse_mode="Markdown",
        )
    except Exception:
        logger.exception("Failed to send morning digest to user %d.", user_id)


async def send_evening_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    data: dict = context.job.data
    user_id: int = data["user_id"]
    now = _now()
    tomorrow_start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_end = tomorrow_start + timedelta(days=1)
    tasks = db.get_tasks_for_range(user_id, tomorrow_start, tomorrow_end)
    if not tasks:
        return

    lines = [f"🌙 *Вечерняя сводка*\n📋 Задачи на завтра, {tomorrow_start.strftime('%d.%m.%Y')} ({len(tasks)}):"]
    for i, t in enumerate(tasks, 1):
        dl = datetime.fromisoformat(t["deadline"])
        rec = f" ↩ {recurrence_label(t['recurrence'])}" if t["recurrence"] else ""
        lines.append(f"{i}. {t['text']} — 🕐 {dl.strftime('%H:%M')}{rec} [#{t['id']}]")

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text="\n".join(lines),
            parse_mode="Markdown",
        )
    except Exception:
        logger.exception("Failed to send evening digest to user %d.", user_id)


# ---------------------------------------------------------------------------
# Scheduling helpers
# ---------------------------------------------------------------------------

def _schedule_task(
    app: Application,
    task_id: int,
    user_id: int,
    task_text: str,
    deadline: datetime,
    reminder_minutes: Optional[int] = None,
    recurrence: Optional[str] = None,
) -> None:
    if not reminder_minutes:
        logger.info("Task %d: no reminder configured.", task_id)
        return
    remind_at = deadline - timedelta(minutes=reminder_minutes)
    delay = (remind_at - _now()).total_seconds()
    if delay <= 0:
        logger.info("Task %d reminder time already passed, skipping.", task_id)
        return
    app.job_queue.run_once(
        send_reminder,
        when=delay,
        data={
            "user_id": user_id,
            "task_id": task_id,
            "task_text": task_text,
            "deadline": deadline,
            "reminder_minutes": reminder_minutes,
            "recurrence": recurrence,
        },
        name=f"task_{task_id}",
    )
    logger.info("Scheduled reminder for task %d in %.0fs.", task_id, delay)


def _schedule_digest(app: Application, user_id: int, time_str: str) -> None:
    for job in app.job_queue.get_jobs_by_name(f"digest_{user_id}"):
        job.schedule_removal()
    try:
        h, m = map(int, time_str.split(":"))
    except ValueError:
        return
    digest_time = time(hour=h, minute=m, tzinfo=LOCAL_TZ)
    app.job_queue.run_daily(
        send_daily_digest,
        time=digest_time,
        data={"user_id": user_id},
        name=f"digest_{user_id}",
    )
    logger.info("Scheduled daily digest for user %d at %s.", user_id, time_str)


def _schedule_auto_digests(app: Application, user_id: int) -> None:
    if not app.job_queue.get_jobs_by_name(f"morning_{user_id}"):
        app.job_queue.run_daily(
            send_morning_digest,
            time=time(hour=9, minute=0, tzinfo=LOCAL_TZ),
            data={"user_id": user_id},
            name=f"morning_{user_id}",
        )
        logger.info("Scheduled morning digest for user %d at 09:00.", user_id)
    if not app.job_queue.get_jobs_by_name(f"evening_{user_id}"):
        app.job_queue.run_daily(
            send_evening_digest,
            time=time(hour=21, minute=0, tzinfo=LOCAL_TZ),
            data={"user_id": user_id},
            name=f"evening_{user_id}",
        )
        logger.info("Scheduled evening digest for user %d at 21:00.", user_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "Напиши задачу и время — я сохраню её в список.\n\n"
    "*Примеры без напоминания:*\n"
    "  Позвонить врачу 15:30\n"
    "  Купить продукты завтра 10:00\n"
    "  Встреча 12.06 14:00\n"
    "  Таблетки через 2 часа\n\n"
    "*Примеры с напоминанием:*\n"
    "  Встреча 14:00 напомни в 13:30\n"
    "  Отчёт завтра 17:00 напомни в 16:00\n"
    "  Звонок в пятницу 11:00 напомни за 30 минут\n"
    "  Дедлайн 20.06 09:00 напомни за 1 час\n\n"
    "*Команды:*\n"
    "  /list — 📋 активные задачи\n"
    "  /today — 📅 задачи на сегодня\n"
    "  /week — 📆 задачи на неделю\n"
    "  /done <id> — ✅ отметить выполненной\n"
    "  /del <id> — 🗑 удалить задачу\n"
    "  /edit <id> <текст + время> — ✏️ изменить\n"
    "  /history — 📜 выполненные задачи\n"
    "  /setdigest 08:30 — ⏰ утренняя сводка\n"
    "  /setdigest off — отключить сводку"
)

WELCOME_TEXT = (
    "👋 Привет! Я *Быстрый задачник* — твой планировщик прямо в Telegram.\n\n"
    "📝 Напиши задачу и время — я добавлю её в список.\n"
    "🔔 Укажи *напомни в HH:MM* или *напомни за N минут* — пришлю уведомление.\n"
    "🔕 Без этих слов задача просто сохранится в список — без напоминания.\n"
    "☀️ Каждое утро в 09:00 пришлю сводку дел на день.\n\n"
)


def _task_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Выполнено", callback_data=f"done:{task_id}"),
        InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{task_id}"),
    ]])


def _fmt_task(t: sqlite3.Row, num: Optional[int] = None) -> str:
    dl = datetime.fromisoformat(t["deadline"])
    rec = f" ↩ {recurrence_label(t['recurrence'])}" if t["recurrence"] else ""
    if num is not None:
        return f"{num}. *{t['text']}*\n    🕐 {dl.strftime('%d.%m.%Y %H:%M')}{rec} [#{t['id']}]"
    return f"[#{t['id']}] *{t['text']}*\n🕐 {dl.strftime('%d.%m.%Y %H:%M')}{rec}"


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _schedule_auto_digests(context.application, update.effective_user.id)
    await update.message.reply_text(
        WELCOME_TEXT + HELP_TEXT,
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tasks = db.get_pending_tasks(update.effective_user.id)
    if not tasks:
        await update.message.reply_text("📭 Нет активных задач.")
        return
    await update.message.reply_text(f"📋 *Активные задачи* ({len(tasks)}):", parse_mode="Markdown")
    for t in tasks:
        await update.message.reply_text(
            _fmt_task(t),
            parse_mode="Markdown",
            reply_markup=_task_keyboard(t["id"]),
        )


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now = _now()
    tasks = db.get_tasks_for_range(
        update.effective_user.id,
        now.replace(hour=0, minute=0, second=0, microsecond=0),
        now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1),
    )
    if not tasks:
        await update.message.reply_text("📭 На сегодня задач нет.")
        return
    await update.message.reply_text(
        f"📅 *Задачи на сегодня, {now.strftime('%d.%m.%Y')}* ({len(tasks)}):",
        parse_mode="Markdown",
    )
    for t in tasks:
        await update.message.reply_text(
            _fmt_task(t),
            parse_mode="Markdown",
            reply_markup=_task_keyboard(t["id"]),
        )


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now = _now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tasks = db.get_tasks_for_range(update.effective_user.id, start, start + timedelta(days=7))
    if not tasks:
        await update.message.reply_text("📭 На ближайшие 7 дней задач нет.")
        return

    by_day: dict[date, list] = defaultdict(list)
    for t in tasks:
        by_day[datetime.fromisoformat(t["deadline"]).date()].append(t)

    await update.message.reply_text(f"📆 *Задачи на неделю* ({len(tasks)}):", parse_mode="Markdown")
    for d in sorted(by_day):
        lines = [f"📌 *{d.strftime('%d.%m')} ({WEEKDAY_NAMES[d.weekday()]})*"]
        for j, t in enumerate(by_day[d], 1):
            dl = datetime.fromisoformat(t["deadline"])
            rec = f" ↩ {recurrence_label(t['recurrence'])}" if t["recurrence"] else ""
            lines.append(f"  {j}. {t['text']} — {dl.strftime('%H:%M')}{rec} [#{t['id']}]")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Использование: /done <id>")
        return
    task_id = int(context.args[0])
    for job in context.job_queue.get_jobs_by_name(f"task_{task_id}"):
        job.schedule_removal()
    if db.mark_done(task_id, user_id):
        await update.message.reply_text(f"✅ Задача #{task_id} выполнена! Молодец 🎉")
    else:
        await update.message.reply_text("❌ Задача не найдена или уже выполнена.")


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Использование: /del <id>")
        return
    task_id = int(context.args[0])
    for job in context.job_queue.get_jobs_by_name(f"task_{task_id}"):
        job.schedule_removal()
    if db.delete_task(task_id, user_id):
        await update.message.reply_text(f"🗑 Задача #{task_id} удалена.")
    else:
        await update.message.reply_text("❌ Задача не найдена.")


async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Использование: /edit <id> <новый текст + время>")
        return
    task_id = int(args[0])
    new_text = " ".join(args[1:])
    if not new_text:
        await update.message.reply_text("Укажи новый текст и время задачи.")
        return

    parsed = parse_task(new_text)
    if parsed is None:
        await update.message.reply_text("Не смог распознать время в новом тексте.")
        return

    for job in context.job_queue.get_jobs_by_name(f"task_{task_id}"):
        job.schedule_removal()

    if db.update_task(task_id, user_id, parsed.text, parsed.deadline):
        _schedule_task(context.application, task_id, user_id, parsed.text,
                       parsed.deadline, parsed.reminder_minutes, parsed.recurrence)
        await update.message.reply_text(
            f"✏️ Задача #{task_id} обновлена:\n\n"
            f"*{parsed.text}*\n"
            f"🕐 {parsed.deadline.strftime('%d.%m.%Y %H:%M')}",
            parse_mode="Markdown",
            reply_markup=_task_keyboard(task_id),
        )
    else:
        await update.message.reply_text("❌ Задача не найдена.")


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tasks = db.get_done_tasks(update.effective_user.id)
    if not tasks:
        await update.message.reply_text("📭 История пуста.")
        return
    lines = [f"📜 *Выполненные задачи* ({len(tasks)}):"]
    for i, t in enumerate(tasks, 1):
        dl = datetime.fromisoformat(t["deadline"])
        lines.append(f"{i}. ✅ {t['text']} — {dl.strftime('%d.%m.%Y %H:%M')} [#{t['id']}]")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_setdigest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not context.args:
        current = db.get_digest_time(user_id)
        status = f"включена в *{current}*" if current != "off" else "отключена"
        await update.message.reply_text(
            f"⏰ Утренняя сводка: {status}.\n"
            "Использование: /setdigest 08:30 или /setdigest off",
            parse_mode="Markdown",
        )
        return

    val = context.args[0].lower()
    if val in ("off", "выкл", "нет"):
        for job in context.job_queue.get_jobs_by_name(f"digest_{user_id}"):
            job.schedule_removal()
        db.set_digest_time(user_id, "off")
        await update.message.reply_text("🔕 Утренняя сводка отключена.")
        return

    if not re.match(r"^\d{1,2}:\d{2}$", val):
        await update.message.reply_text("❌ Неверный формат. Пример: /setdigest 08:30")
        return

    db.set_digest_time(user_id, val)
    _schedule_digest(context.application, user_id, val)
    await update.message.reply_text(f"✅ Утренняя сводка настроена на *{val}* каждый день.", parse_mode="Markdown")


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = update.message.text.strip()
    parsed = parse_task(text)

    if parsed is None:
        await update.message.reply_text(
            "🤔 Не смог распознать время. Примеры:\n\n"
            "  Позвонить врачу 15:30\n"
            "  Встреча завтра 09:00\n"
            "  Презентация 15.06 13:00\n"
            "  Таблетки через 30 минут\n"
            "  Отчёт в пятницу 17:00"
        )
        return

    now = _now()
    if parsed.deadline <= now:
        await update.message.reply_text(
            f"⏰ Срок уже прошёл ({parsed.deadline.strftime('%d.%m.%Y %H:%M')}). Уточни дату."
        )
        return

    try:
        task_id = db.save_task(
            user_id, parsed.text, parsed.deadline, parsed.reminder_minutes, parsed.recurrence
        )
    except Exception:
        logger.exception("Failed to save task for user %d.", user_id)
        await update.message.reply_text("⚠️ Не удалось сохранить задачу. Попробуй ещё раз.")
        return

    _schedule_task(
        context.application, task_id, user_id, parsed.text,
        parsed.deadline, parsed.reminder_minutes, parsed.recurrence,
    )
    _schedule_auto_digests(context.application, user_id)

    rec_note = f"\n↩ Повтор: *{recurrence_label(parsed.recurrence)}*" if parsed.recurrence else ""
    if parsed.reminder_minutes:
        remind_at = parsed.deadline - timedelta(minutes=parsed.reminder_minutes)
        rem_note = f"\n🔔 Напомню в {remind_at.strftime('%H:%M')}"
    else:
        rem_note = "\n🔕 Без напоминания"
    await update.message.reply_text(
        f"✅ Задача добавлена!\n\n"
        f"📝 *{parsed.text}*\n"
        f"🕐 {parsed.deadline.strftime('%d.%m.%Y %H:%M')}"
        f"{rec_note}{rem_note}",
        parse_mode="Markdown",
        reply_markup=_task_keyboard(task_id),
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data.startswith("done:"):
        task_id = int(data.split(":")[1])
        for job in context.job_queue.get_jobs_by_name(f"task_{task_id}"):
            job.schedule_removal()
        if db.mark_done(task_id, user_id):
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(f"✅ Задача #{task_id} выполнена! Молодец 🎉")
        else:
            await query.answer("Задача не найдена или уже выполнена.", show_alert=True)

    elif data.startswith("del:"):
        task_id = int(data.split(":")[1])
        for job in context.job_queue.get_jobs_by_name(f"task_{task_id}"):
            job.schedule_removal()
        if db.delete_task(task_id, user_id):
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(f"🗑 Задача #{task_id} удалена.")
        else:
            await query.answer("Задача не найдена.", show_alert=True)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

async def on_startup(app: Application) -> None:
    db.init_db()

    pending = db.get_pending_tasks()
    for t in pending:
        deadline = datetime.fromisoformat(t["deadline"])
        _schedule_task(
            app,
            task_id=t["id"],
            user_id=t["user_id"],
            task_text=t["text"],
            deadline=deadline,
            reminder_minutes=t["reminder_minutes"],
            recurrence=t["recurrence"],
        )
    logger.info("Restored %d pending tasks.", len(pending))

    for row in db.get_all_digest_settings():
        _schedule_digest(app, row["user_id"], row["digest_time"])
    logger.info("Restored digest schedules.")

    for user_id in db.get_all_users():
        _schedule_auto_digests(app, user_id)
    logger.info("Scheduled auto morning/evening digests.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    token = _ENV.get("BOT_TOKEN") or os.getenv("BOT_TOKEN")
    if not token or token == "your_telegram_bot_token_here":
        raise RuntimeError("BOT_TOKEN is not set. Edit .env and add your bot token.")

    app = Application.builder().token(token).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("del", cmd_delete))
    app.add_handler(CommandHandler("edit", cmd_edit))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("setdigest", cmd_setdigest))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    logger.info("Bot started. Polling...")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
