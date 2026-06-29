---
name: tasksbot-context
description: >-
  Provides full architecture context for the «Быстрый задачник» Telegram task-management bot.
  Use when making ANY changes to bot.py or db.py, adding new features, fixing bugs, or
  when the agent needs to understand how the bot works before touching code.
---

# Tasksbot — Architecture Context

## What this bot is

**«Быстрый задачник»** — a Russian-language Telegram task planner.  
Users send natural-language messages (e.g. `Позвонить врачу завтра в 18 #личное`) and the bot:
- Parses text, deadline, priority, category, reminder, recurrence
- Stores tasks in SQLite
- Sends morning (09:00) and evening (21:00) digests
- Fires reminder notifications
- Supports family task sharing with a partner

**Core principle:** Tasks must be created as fast and simply as possible. The longer/more complex the flow, the less users want to use the bot. Every UX decision must respect this.

## File map

| File | Role |
|---|---|
| `bot.py` | Only Python entry point: parser, handlers, scheduler, keyboards |
| `db.py` | SQLite CRUD only. No business logic |
| `tasks.db` | Runtime data (not in repo) |

## Key data flow

```
User text → parse_task() → db.save_task() → _schedule_task() → job_queue
                                          → message with inline keyboard
Inline button → callback_handler() → db.set_task_*() / mark_done() / etc.
job_queue timer → send_reminder() → Telegram message
run_daily 09:00 → send_morning_digest() → Telegram message
```

## Parser (`parse_task`) — pattern priority order

Patterns are checked top-to-bottom; first match wins:

1. Russian month + time: `16 июля 10:00`, `16 июля в 10:00`, `16 июля 2026 10:00`
2. Russian month + bare hour: `16 июля в 18`
3. `DD.MM[.YYYY] HH:MM` — e.g. `12.06 14:00`
4. `через N минут/часов`
5. `в [день недели] [HH:MM]` — weekday, max 7 days ahead
6. `послезавтра HH:MM / в HH:MM`
7. `послезавтра в X утра/дня/вечера/ночи`
8. `послезавтра в N` — bare 24h hour
9. `завтра HH:MM / в HH:MM`
10. `завтра в X утра/дня/вечера/ночи`
11. `завтра в N` — bare 24h hour
12. `в X утра/дня/вечера/ночи` — am/pm style
13. `в N` — bare 24h hour (0–23), no colon
14. `HH:MM` — today at that time, tomorrow if past
15. `16 июля` alone — date only, defaults to 09:00

**Before parsing**, the text is stripped of: recurrence phrases, `напомни за/в`, priority keywords (`срочно`/`важно`), `#hashtags`.

**After parsing** succeeds: reminder_minutes=None means no reminder; 0 in DB also means no reminder.

## Task creation flow (UX: 1 message only!)

When user sends a task:

- **Category AND priority already in text** → single final message with `_task_keyboard`
- **Category missing** → send "Задача сохранена" message with `_category_keyboard`
  - User taps category → **edit that same message** (no new message!) with `_priority_keyboard`
  - User taps priority → **edit that same message** to final card with `_task_keyboard`
- **Category set, priority missing** → send "Задача сохранена" message with `_priority_keyboard`
  - User taps priority → **edit that same message** to final card

This means **only 1 message exists** in chat after task creation. Use `query.edit_message_text()` NOT `query.message.reply_text()` in `cat:` and `pri:` callback handlers.

## /edit flow (interactive picker)

- `/edit` with no args → show `_edit_pick_keyboard(tasks)` for task selection
- User taps task → `edit_pick:` callback sets `context.user_data["editing_task_id"] = task_id`
- Next user message → `message_handler` pops `editing_task_id` and applies edit
- User types "отмена" → cancel editing
- If new text fails to parse → restore `editing_task_id` and ask to retry

## Digest format (morning)

Morning digest at 09:00 shows TWO separate sections:
1. Today's tasks (if any)
2. Overdue tasks listed inline (NOT just a count) with date shown

Critical: overdue tasks must NOT appear mixed with today's tasks. They are a separate `⚠️` section.

## Database key facts

- `reminder_minutes = 0` in DB = no reminder
- `status` = `'active'` | `'done'`
- `linked_task_id` — family task link
- `category` can be None (not set yet) or any string (hashtag value)
- `priority` = 0 (normal) | 1 (important 🟡) | 2 (urgent 🔴)
- All deadlines stored as ISO string with timezone

## Timezone

Always use `LOCAL_TZ = ZoneInfo("Europe/Moscow")`. Never use naive datetimes.

## Common pitfalls to avoid

- **Never** send `reply_text` for category/priority steps — always `edit_message_text` on `query`
- **Never** break the 1-message rule for task creation flow
- **Always** check `context.user_data.get("editing_task_id")` before processing a message as new task
- **Always** roll year forward if `_ru_month_deadline` result is in the past
- When editing a task, cancel old `job_queue` job (`task_{id}`) before scheduling a new one
- The `_BARE_HOUR_RE` must NOT conflict with `_AMPM_RE` (am/pm checked first, bare hour is fallback)
- Family cascade: when done/delete/edit affects a task with `linked_task_id`, always propagate to partner

## Adding new date formats

Add new regex to the parser section. Place it in the correct priority position. Test edge cases:
- time in the past → next day / next year
- bare hour 0–23 boundary check
- do NOT conflict with existing patterns (check all patterns above in priority list)
