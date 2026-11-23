"""Reminders and calendar events, stored in the same SQLite file as tasks."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path


@dataclass
class Reminder:
    id: int
    text: str
    due_at: datetime
    event_id: int | None = None


@dataclass
class Event:
    id: int
    title: str
    day: date
    at: time | None

    def spoken(self, today: date | None = None) -> str:
        today = today or date.today()
        if self.day == today:
            when = "today"
        elif self.day == today + timedelta(days=1):
            when = "tomorrow"
        else:
            when = f"on {self.day:%A} {self.day.day} {self.day:%B}"
        if self.at:
            when += f" at {_spoken_time(self.at)}"
        return f"{self.title} {when}"


def _spoken_time(t: time) -> str:
    h = t.hour % 12 or 12
    suffix = "AM" if t.hour < 12 else "PM"
    return f"{h} {suffix}" if t.minute == 0 else f"{h}:{t.minute:02d} {suffix}"


def spoken_datetime(dt: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now()
    if dt.date() == now.date():
        day = "today"
    elif dt.date() == now.date() + timedelta(days=1):
        day = "tomorrow"
    else:
        day = f"on {dt:%A} {dt.day} {dt:%B}"
    return f"{day} at {_spoken_time(dt.time())}"


class ScheduleStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    day TEXT NOT NULL,
                    at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    fired INTEGER NOT NULL DEFAULT 0,
                    event_id INTEGER REFERENCES events(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_reminders_due ON reminders(fired, due_at);
                """
            )

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # ---- reminders -------------------------------------------------------

    def add_reminder(self, text: str, due_at: datetime, event_id: int | None = None) -> Reminder:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO reminders (text, due_at, event_id, created_at) VALUES (?, ?, ?, ?)",
                (text, due_at.isoformat(timespec="minutes"), event_id, datetime.now().isoformat(timespec="seconds")),
            )
        return Reminder(cur.lastrowid, text, due_at, event_id)

    def due_reminders(self, now: datetime | None = None) -> list[Reminder]:
        now = now or datetime.now()
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, text, due_at, event_id FROM reminders WHERE fired = 0 AND due_at <= ? ORDER BY due_at",
                (now.isoformat(timespec="minutes"),),
            ).fetchall()
        return [Reminder(r[0], r[1], datetime.fromisoformat(r[2]), r[3]) for r in rows]

    def mark_fired(self, reminder_id: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE reminders SET fired = 1 WHERE id = ?", (reminder_id,))

    def pending_reminders(self, until: datetime | None = None) -> list[Reminder]:
        sql = "SELECT id, text, due_at, event_id FROM reminders WHERE fired = 0"
        args: tuple = ()
        if until:
            sql += " AND due_at <= ?"
            args = (until.isoformat(timespec="minutes"),)
        with self._conn() as c:
            rows = c.execute(sql + " ORDER BY due_at", args).fetchall()
        return [Reminder(r[0], r[1], datetime.fromisoformat(r[2]), r[3]) for r in rows]

    def cancel_reminder(self, reminder_id: int) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM reminders WHERE id = ? AND fired = 0", (reminder_id,)).rowcount > 0

    # ---- events ----------------------------------------------------------

    def add_event(self, title: str, day: date, at: time | None = None, *,
                  morning_hour: int = 9, before_minutes: int = 30, now: datetime | None = None
                  ) -> tuple[Event, list[Reminder]]:
        """Saves an event and schedules its reminders: one on the morning of the day,
        and one shortly before it when it has a time. Reminders already in the past are skipped."""
        now = now or datetime.now()
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO events (title, day, at, created_at) VALUES (?, ?, ?, ?)",
                (title, day.isoformat(), at.strftime("%H:%M") if at else None, now.isoformat(timespec="seconds")),
            )
        event = Event(cur.lastrowid, title, day, at)

        reminders = []
        morning = datetime.combine(day, time(morning_hour))
        start = datetime.combine(day, at) if at else None
        if morning > now and (start is None or morning < start - timedelta(minutes=before_minutes)):
            reminders.append(self.add_reminder(f"Today: {event.spoken(day)}", morning, event.id))
        if start:
            before = start - timedelta(minutes=before_minutes)
            if before > now:
                reminders.append(self.add_reminder(f"In {before_minutes} minutes: {title}", before, event.id))
            elif start > now:
                reminders.append(self.add_reminder(f"Starting now: {title}", start, event.id))
        return event, reminders

    def events_between(self, start: date, end: date) -> list[Event]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, title, day, at FROM events WHERE day BETWEEN ? AND ? ORDER BY day, at IS NOT NULL, at",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return [Event(r[0], r[1], date.fromisoformat(r[2]), time.fromisoformat(r[3]) if r[3] else None) for r in rows]

    def delete_event(self, event_id: int) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM events WHERE id = ?", (event_id,)).rowcount > 0
