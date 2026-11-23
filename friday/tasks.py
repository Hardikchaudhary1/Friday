"""A small to-do list stored in SQLite."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path


@dataclass
class Task:
    id: int
    text: str
    due: date | None
    done: bool

    def due_label(self, today: date) -> str:
        if self.due is None:
            return ""
        if self.due < today:
            return f"overdue since {self.due:%a %d %b}"
        if self.due == today:
            return "today"
        if self.due == today + timedelta(days=1):
            return "tomorrow"
        return f"{self.due:%a %d %b}"


def parse_due(text: str | None, today: date | None = None) -> date | None:
    if not text:
        return None
    today = today or date.today()
    t = text.strip().lower()
    if t in ("today", "tod"):
        return today
    if t in ("tomorrow", "tmr", "tom"):
        return today + timedelta(days=1)
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for i, name in enumerate(weekdays):
        if name.startswith(t) and len(t) >= 3:
            delta = (i - today.weekday()) % 7 or 7
            return today + timedelta(days=delta)
    return datetime.strptime(t, "%Y-%m-%d").date()


class TaskStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    due TEXT,
                    done INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    done_at TEXT
                )"""
            )

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    @staticmethod
    def _row(r) -> Task:
        return Task(r[0], r[1], date.fromisoformat(r[2]) if r[2] else None, bool(r[3]))

    def add(self, text: str, due: date | None = None) -> Task:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO tasks (text, due, created_at) VALUES (?, ?, ?)",
                (text.strip(), due.isoformat() if due else None, datetime.now().isoformat(timespec="seconds")),
            )
            return Task(cur.lastrowid, text.strip(), due, False)

    def complete(self, task_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE tasks SET done = 1, done_at = ? WHERE id = ? AND done = 0",
                (datetime.now().isoformat(timespec="seconds"), task_id),
            )
            return cur.rowcount > 0

    def remove(self, task_id: int) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM tasks WHERE id = ?", (task_id,)).rowcount > 0

    def open_tasks(self) -> list[Task]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, text, due, done FROM tasks WHERE done = 0 "
                "ORDER BY due IS NULL, due, id"
            ).fetchall()
        return [self._row(r) for r in rows]

    def for_today(self, today: date | None = None) -> list[Task]:
        """Overdue, due today, and undated open tasks."""
        today = today or date.today()
        return [t for t in self.open_tasks() if t.due is None or t.due <= today]
