"""Understands a spoken (or typed) command and carries it out.

Common commands are matched with patterns, so they work instantly and offline.
Anything else goes to Claude for a short spoken answer, when Claude is available.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher

from .apps import AppIndex, open_target, web_search
from .schedule import ScheduleStore, spoken_datetime
from .tasks import TaskStore
from .timeparse import parse

WAKE_PREFIX = re.compile(r"^\s*((hey|hi|ok|okay)\s+)?friday\b[\s,.!?]*", re.I)


@dataclass
class Reply:
    text: str
    action: str = ""          # "", "briefing", "stop"
    data: dict = field(default_factory=dict)


HELP = ("You can say things like: open Spotify. Search for oil prices. Remind me to call mom tomorrow at 5 PM. "
        "Add dentist appointment on the 5th of December at 4 PM. Add task pay rent due Friday. "
        "What's on my schedule tomorrow? What are my tasks? Brief me. Or ask me a question.")


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.I)


R_STOP = _rx(r"^(stop|cancel|never ?mind|be quiet|shut up|quiet|that's all|nothing)$")
R_GREET = _rx(r"^(hi|hello|hey|good (morning|afternoon|evening))( there)?$")
R_THANKS = _rx(r"^(thanks|thank you|cheers)\b")
R_TIME = _rx(r"\bwhat('s| is)? ?(the )?time\b|\btime is it\b")
R_DATE = _rx(r"\bwhat('s| is) (the |today's )?date\b|\bwhat day is (it|today)\b")
R_HELP = _rx(r"^(help|what can you do|what do you do)\b")
R_OPEN = _rx(r"^(please )?(open|launch|start|run|fire up)\s+(.+)$")
R_SEARCH = _rx(r"^(please )?(search|google|look up)( the web| online)?( for)?\s+(.+)$")
R_REMIND = _rx(r"^(please )?(remind me|set (a |an )?(reminder|alarm)|create (a )?reminder)\s*(to|about|that|for|of)?\s*(.*)$")
R_TASK_ADD = _rx(r"^(please )?(add|create|new)\s+(a\s+)?(task|to-?do|todo)\s*(to|called|named)?\s*(.+)$")
R_TASK_ADD2 = _rx(r"^(please )?add\s+(.+?)\s+to\s+my\s+(task|to-?do|todo)s?( list)?\s*(.*)$")
R_TASK_LIST = _rx(r"\b(what are|read|list|show)( me)? my (tasks|to-?dos?|todo list|to-?do list)\b|^my (tasks|to-?do list)$|what do i (have|need) to do")
R_TASK_DONE = _rx(r"^(mark|complete|finish|tick off|check off|i finished|i('ve| have) done|done with)\s+(task\s+)?(number\s+)?(.+?)(\s+as)?(\s+(done|complete|completed|finished))?$")
R_EVENT = _rx(r"^(please )?(add|create|schedule|put|set up|book)\s+(an?\s+)?(event|meeting|appointment|calendar event)?\s*(called|named|for)?\s*(.+)$")
R_CALENDAR_SUFFIX = _rx(r"\s+(to|in|on|into)\s+(my|the)\s+(calendar|schedule|agenda)\b")
R_SCHEDULE = _rx(r"\b(what('s| is)|show|read|tell me)( me)? (on )?my (schedule|calendar|agenda|day)\b|\bwhat do i have (on )?(today|tomorrow|this week)\b|\bany (events|meetings|appointments)\b|\bmy (schedule|calendar|agenda)\b")
R_REMINDERS = _rx(r"\b(what are|list|show|read)( me)? my reminders\b|\bupcoming reminders\b|\bany reminders\b")
R_CANCEL_REMINDER = _rx(r"^(cancel|delete|remove)\s+(the\s+|my\s+)?(last\s+)?reminder(\s+(to|about|for)\s+(.+))?$")
R_CANCEL_EVENT = _rx(r"^(cancel|delete|remove)\s+(the\s+|my\s+)?(event|meeting|appointment)\s+(.+)$")
R_BRIEF = _rx(r"\b(brief me|briefing|market (update|news|summary)|what('s| is) (happening|going on)|the news|headlines)\b")


def _best_match(query: str, options: list[tuple[int, str]]) -> tuple[int | None, float]:
    best, best_score = None, 0.0
    q = query.lower()
    for oid, text in options:
        t = text.lower()
        score = 1.0 if q == t else 0.9 if q in t or t in q else SequenceMatcher(None, q, t).ratio()
        if score > best_score:
            best, best_score = oid, score
    return best, best_score


class Assistant:
    def __init__(self, cfg, app_index: AppIndex | None = None, now_fn=datetime.now, dry_run: bool = False,
                 llm_backends: list | None = None):
        self.cfg = cfg
        db = cfg.data_dir / "friday.db"
        self.tasks = TaskStore(db)
        self.schedule = ScheduleStore(db)
        self.apps = app_index or AppIndex(cfg.data_dir / "apps.json", cfg.get("apps.aliases", {}) or {})
        self.now = now_fn
        self.dry_run = dry_run
        self.llm_backends = llm_backends
        self.pending: dict | None = None   # a follow-up question waiting for an answer
        self.morning_hour = int(cfg.get("reminders.morning_hour", 9))
        self.before_minutes = int(cfg.get("reminders.before_minutes", 30))

    # ---- entry point -----------------------------------------------------

    def handle(self, raw: str) -> Reply:
        text = WAKE_PREFIX.sub("", (raw or "").replace("﻿", "")).strip(" .,!?")
        if not text:
            return Reply("Yes?")

        if self.pending:
            pending, self.pending = self.pending, None
            if not R_STOP.match(text) and self.now() - pending["at"] < timedelta(minutes=2):
                return self._answer_pending(pending, text)

        for pattern, handler in (
            (R_STOP, lambda m: Reply("", "stop")),
            (R_GREET, lambda m: Reply(f"Hello {self.cfg.get('user_name', '')}. How can I help?".replace("  ", " "))),
            (R_THANKS, lambda m: Reply("You're welcome.")),
            (R_HELP, lambda m: Reply(HELP)),
            (R_CANCEL_REMINDER, self._cancel_reminder),
            (R_CANCEL_EVENT, self._cancel_event),
            (R_REMIND, self._remind),
            (R_TASK_ADD2, lambda m: self._add_task(f"{m.group(2)} {m.group(5)}")),
            (R_TASK_ADD, lambda m: self._add_task(m.group(6))),
            (R_TASK_LIST, lambda m: self._list_tasks()),
            (R_TASK_DONE, self._task_done),
            (R_REMINDERS, lambda m: self._list_reminders()),
            (R_EVENT, self._event),   # before R_SCHEDULE: "put X in my calendar" adds, it doesn't ask
            (R_SCHEDULE, lambda m: self._schedule(text)),
            (R_OPEN, lambda m: self._open(m.group(3))),
            (R_SEARCH, lambda m: Reply(web_search(m.group(5), self.dry_run).message)),
            (R_TIME, lambda m: Reply(f"It's {self._clock(self.now())}.")),
            (R_DATE, lambda m: Reply(f"It's {self.now():%A} {self.now().day} {self.now():%B} {self.now().year}.")),
            (R_BRIEF, lambda m: Reply("Here's your briefing.", "briefing")),
        ):
            m = pattern.search(text)
            if m:
                reply = handler(m)
                if reply is not None:
                    return reply
        return self._ask_claude(text)

    # ---- helpers -----------------------------------------------------------

    @staticmethod
    def _clock(dt: datetime) -> str:
        h = dt.hour % 12 or 12
        return f"{h}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"

    def _ask(self, kind: str, question: str, **data) -> Reply:
        self.pending = {"kind": kind, "at": self.now(), **data}
        return Reply(question)

    def _answer_pending(self, pending: dict, text: str) -> Reply:
        if pending["kind"] == "reminder_time":
            return self._create_reminder(pending["what"], text)
        if pending["kind"] == "reminder_what":
            return self._remind_text(text)
        if pending["kind"] == "event_date":
            return self._event_text(f"{pending['title']} {text}")
        if pending["kind"] == "open_confirm" and re.match(r"^(yes|yeah|yep|sure|ok|okay|please|do it)\b", text, re.I):
            return self._open(pending["name"])
        return self.handle(text)

    # ---- apps ----------------------------------------------------------------

    def _open(self, name: str) -> Reply:
        result = open_target(name, self.apps, dry_run=self.dry_run)
        if not result.ok and result.target:
            return self._ask("open_confirm", result.message, name=result.target)
        return Reply(result.message, data={"target": result.target})

    # ---- reminders -----------------------------------------------------------

    def _remind(self, m: re.Match) -> Reply:
        return self._remind_text(m.group(7) or "")

    def _remind_text(self, text: str) -> Reply:
        if not text.strip():
            return self._ask("reminder_what", "What should I remind you about?")
        p = parse(text, self.now(), self.morning_hour)
        if p.when is None:
            return self._ask("reminder_time", f"When should I remind you to {p.text}?", what=p.text)
        return self._save_reminder(p.text, p.when, p.has_time)

    def _create_reminder(self, what: str, when_text: str) -> Reply:
        p = parse(when_text, self.now(), self.morning_hour)
        if p.when is None:
            return self._ask("reminder_time", "Sorry, I didn't catch the time. When should I remind you?", what=what)
        return self._save_reminder(what, p.when, p.has_time)

    def _save_reminder(self, what: str, when: datetime, has_time: bool) -> Reply:
        now = self.now()
        if when <= now:
            if not has_time and when.date() == now.date():
                when = now + timedelta(hours=1)  # "remind me today" with no time: in an hour
            else:
                return Reply(f"{spoken_datetime(when, now)} has already passed. Try another time.")
        what = what or "your reminder"
        self.schedule.add_reminder(what, when)
        return Reply(f"Okay, I'll remind you to {what} {spoken_datetime(when, now)}.",
                     data={"what": what, "when": when.isoformat()})

    def _list_reminders(self) -> Reply:
        items = self.schedule.pending_reminders()
        if not items:
            return Reply("You have no upcoming reminders.")
        now = self.now()
        parts = [f"{r.text}, {spoken_datetime(r.due_at, now)}" for r in items[:6]]
        more = f" And {len(items) - 6} more." if len(items) > 6 else ""
        return Reply(f"You have {len(items)} reminder{'s' if len(items) != 1 else ''}. " + ". ".join(parts) + "." + more)

    def _cancel_reminder(self, m: re.Match) -> Reply:
        items = self.schedule.pending_reminders()
        if not items:
            return Reply("You have no reminders to cancel.")
        if m.group(6):
            rid, score = _best_match(m.group(6), [(r.id, r.text) for r in items])
            if score < 0.5:
                return Reply(f"I couldn't find a reminder about {m.group(6)}.")
            target = next(r for r in items if r.id == rid)
        else:
            target = max(items, key=lambda r: r.id)  # the most recently created
        self.schedule.cancel_reminder(target.id)
        return Reply(f"Cancelled the reminder: {target.text}.")

    # ---- events --------------------------------------------------------------

    def _event(self, m: re.Match) -> Reply | None:
        body = m.group(6)
        # "add milk" with no date and no calendar word is probably not an event.
        if not m.group(4) and not R_CALENDAR_SUFFIX.search(" " + body) and parse(body, self.now()).when is None:
            return None
        return self._event_text(body)

    def _event_text(self, body: str) -> Reply:
        body = R_CALENDAR_SUFFIX.sub(" ", " " + body).strip()
        p = parse(body, self.now(), self.morning_hour)
        title = re.sub(r"^(called|named|for|about)\s+", "", p.text, flags=re.I).strip() or "Event"
        if p.when is None:
            return self._ask("event_date", f"What date is {title} on?", title=title)
        event, reminders = self.schedule.add_event(
            title, p.when.date(), p.when.time() if p.has_time else None,
            morning_hour=self.morning_hour, before_minutes=self.before_minutes, now=self.now())
        when = event.spoken(self.now().date()).removeprefix(title).strip()
        if reminders:
            r_words = " and ".join(
                "that morning" if r.text.startswith("Today:") else
                f"{self.before_minutes} minutes before" if r.text.startswith("In ") else "when it starts"
                for r in reminders)
            tail = f" I'll remind you {r_words}."
        else:
            tail = ""
        return Reply(f"Added {title} {when}.{tail}", data={"event_id": event.id})

    def _cancel_event(self, m: re.Match) -> Reply:
        today = self.now().date()
        events = self.schedule.events_between(today, today + timedelta(days=366))
        eid, score = _best_match(m.group(4), [(e.id, e.title) for e in events])
        if not events or score < 0.5:
            return Reply(f"I couldn't find an event called {m.group(4)}.")
        event = next(e for e in events if e.id == eid)
        self.schedule.delete_event(event.id)
        return Reply(f"Cancelled {event.spoken(today)}.")

    def _schedule(self, text: str) -> Reply:
        today = self.now().date()
        if re.search(r"\btomorrow\b", text, re.I):
            start = end = today + timedelta(days=1)
            label = "tomorrow"
        elif re.search(r"\b(this|the) week\b|\bweek\b", text, re.I):
            start, end, label = today, today + timedelta(days=6), "in the next seven days"
        else:
            start = end = today
            label = "today"
        events = self.schedule.events_between(start, end)
        if not events:
            return Reply(f"Nothing on your calendar {label}.")
        listed = ". ".join(e.spoken(today) for e in events[:8])
        return Reply(f"You have {len(events)} event{'s' if len(events) != 1 else ''} {label}. {listed}.")

    # ---- tasks ---------------------------------------------------------------

    def _add_task(self, body: str) -> Reply:
        p = parse(body, self.now(), self.morning_hour)
        text = re.sub(r"\s+due$", "", p.text, flags=re.I).strip()
        if not text:
            return Reply("What's the task?")
        task = self.tasks.add(text, p.when.date() if p.when else None)
        due = f", due {task.due_label(self.now().date())}" if task.due else ""
        return Reply(f"Added task: {task.text}{due}.")

    def _list_tasks(self) -> Reply:
        tasks = self.tasks.open_tasks()
        if not tasks:
            return Reply("Your task list is empty.")
        today = self.now().date()
        parts = [f"{t.text}" + (f", {t.due_label(today)}" if t.due else "") for t in tasks[:8]]
        return Reply(f"You have {len(tasks)} open task{'s' if len(tasks) != 1 else ''}. " + ". ".join(parts) + ".")

    def _task_done(self, m: re.Match) -> Reply | None:
        target = m.group(5).strip()
        tasks = self.tasks.open_tasks()
        if not tasks:
            return Reply("You have no open tasks.")
        words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
        num = int(target) if target.isdigit() else words.get(target.lower())
        if num is not None:
            match = next((t for t in tasks if t.id == num), None)
        else:
            tid, score = _best_match(target, [(t.id, t.text) for t in tasks])
            match = next((t for t in tasks if t.id == tid), None) if score >= 0.5 else None
        if not match:
            return Reply(f"I couldn't find a task matching {target}.")
        self.tasks.complete(match.id)
        return Reply(f"Marked done: {match.text}.")

    # ---- general questions -------------------------------------------------

    def _ask_claude(self, text: str) -> Reply:
        from .llm import LLMError, build_backends

        backends = self.llm_backends if self.llm_backends is not None else build_backends(self.cfg)
        now = self.now()
        system = (
            "You are Friday, a concise voice assistant. Your answer is spoken aloud: plain sentences, no markdown, "
            "no lists, at most three short sentences. If you are unsure or the question needs live data you don't "
            f"have, say so briefly. Current local time: {now:%A %d %B %Y %H:%M}."
        )
        for backend in backends:
            if not backend.available():
                continue
            try:
                return Reply(backend.complete(system, text))
            except LLMError:
                continue
        return Reply("I can only handle commands right now, like opening apps, reminders, events and tasks. "
                     "Sign in to the Claude CLI and I can answer general questions too.")
