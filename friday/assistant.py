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

WAKE_PREFIX = re.compile(r"^\s*((hey|hi|ok|okay)[\s,]+)?friday\b[\s,.!?]*", re.I)


def normalise_speech(raw: str) -> str:
    """Cleans a transcript: drops the wake word, the punctuation speech-to-text adds
    ("open, Spotify"), and writes "5 p.m." as "5 pm"."""
    text = (raw or "").replace("﻿", "")
    text = re.sub(r"\b([ap])\.\s?m\b\.?", r"\1m", text, flags=re.I)
    text = WAKE_PREFIX.sub("", text)
    text = re.sub(r"\s*[,;:]\s*", " ", text)
    return re.sub(r"\s{2,}", " ", text).strip(" .!?")


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
R_LOGIN = _rx(r"^/?(login|log in|sign in|signin)( to claude)?$")
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
# ---- media, volume, sites, windows ----
_MEDIA_OBJ = r"( the| this| that| my)?( music| song| track| video| it| playback| spotify| youtube| audio)?"
# A bare "stop" stops Friday talking; "stop the music" pauses media.
R_PAUSE = _rx(rf"^(pause|resume|unpause|continue|play){_MEDIA_OBJ}( please)?$|^play (some )?music$|"
              r"^stop( the| this| my)? (music|song|track|video|playback|spotify|youtube|audio)$")
R_NEXT = _rx(rf"^(next|skip|skip ahead|play the next){_MEDIA_OBJ}( please)?$|^(next one|skip this( one)?)$")
R_PREV = _rx(rf"^(previous|go back|back|last|play the previous|replay){_MEDIA_OBJ}( please)?$|^(previous one)$")
R_VOL_SET = _rx(r"^(set |turn |change )?(the )?volume (to |at )?(\d{1,3})( percent| %|%)?$")
R_VOL_STEP = _rx(r"^(turn (it|the volume|the music) (up|down)|volume (up|down)|(louder|quieter|softer)( please)?|"
                 r"(increase|decrease|raise|lower|reduce) (the )?volume)( a (lot|little|bit))?$")
R_MUTE = _rx(r"^(mute|unmute)( the)?( sound| audio| volume| music| it)?$")
R_WHAT_VOL = _rx(r"^what('s| is) the volume$")
R_PLAY_ON = _rx(r"^play (.+?) on (youtube|spotify)$")
R_PLAY_ANY = _rx(r"^play (.+)$")
R_SEARCH_ON = _rx(r"^(search|look up|find|look for)( for)? (.+?) on (youtube|google|amazon|github|wikipedia|reddit|"
                  r"linkedin|spotify|netflix|twitter|x|maps|google maps|news|images|tradingview)$")
R_SEARCH_SITE_FIRST = _rx(r"^search (youtube|google|amazon|github|wikipedia|reddit|linkedin|spotify|netflix|maps|news)"
                          r" for (.+)$")
R_CLOSE = _rx(r"^(close|quit|exit|shut down|kill)\s+(.+)$")
R_FOCUS = _rx(r"^(switch to|go to|focus( on)?|bring up|go back to)\s+(.+)$")
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
        self.on_thinking = None            # called before a slow Claude answer
        self.window_lister = None          # tests replace the live window list
        self.morning_hour = int(cfg.get("reminders.morning_hour", 9))
        self.before_minutes = int(cfg.get("reminders.before_minutes", 30))

    # ---- entry point -----------------------------------------------------

    def handle(self, raw: str) -> Reply:
        text = normalise_speech(raw)
        if not text:
            return Reply("Yes?")

        if self.pending:
            pending, self.pending = self.pending, None
            if not R_STOP.match(text) and self.now() - pending["at"] < timedelta(minutes=2):
                return self._answer_pending(pending, text)

        for pattern, handler in (
            # Media first: these are short and said mid-task, so they must be quick and silent.
            (R_PAUSE, lambda m: self._media("play_pause")),
            (R_NEXT, lambda m: self._media("next")),
            (R_PREV, lambda m: self._media("previous")),
            (R_VOL_SET, lambda m: self._volume_set(int(m.group(4)))),
            (R_VOL_STEP, self._volume_step),
            (R_MUTE, lambda m: self._mute(m.group(1).lower() == "mute")),
            (R_WHAT_VOL, lambda m: self._volume_report()),
            (R_STOP, lambda m: Reply("", "stop")),
            (R_GREET, lambda m: Reply(f"Hello {self.cfg.get('user_name', '')}. How can I help?".replace("  ", " "))),
            (R_THANKS, lambda m: Reply("You're welcome.")),
            (R_HELP, lambda m: Reply(HELP)),
            (R_LOGIN, lambda m: self._login()),
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
            (R_CLOSE, lambda m: self._close(m.group(2))),
            (R_FOCUS, lambda m: self._focus(m.group(3))),
            (R_OPEN, lambda m: self._open(m.group(3))),
            (R_PLAY_ON, lambda m: self._play(m.group(1), m.group(2).lower())),
            (R_PLAY_ANY, lambda m: self._play(m.group(1), None)),
            (R_SEARCH_ON, lambda m: self._search_site(m.group(4), m.group(3))),
            (R_SEARCH_SITE_FIRST, lambda m: self._search_site(m.group(1), m.group(2))),
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
        if pending["kind"] == "open_name":
            return self._open(re.sub(r"^(open|launch|start)\s+", "", text, flags=re.I))
        if pending["kind"] == "open_confirm" and re.match(r"^(yes|yeah|yep|sure|ok|okay|please|do it)\b", text, re.I):
            return self._open(pending["name"])
        return self.handle(text)

    # ---- apps ----------------------------------------------------------------

    def _open(self, name: str) -> Reply:
        result = open_target(name, self.apps, dry_run=self.dry_run)
        if not result.ok and result.target:
            return self._ask("open_confirm", result.message, name=result.target)
        if not result.ok:
            return self._ask("open_name", f"{result.message.rstrip('.')}. Which app should I open?")
        return Reply(result.message, data={"target": result.target})

    # ---- media and windows ---------------------------------------------------

    def _media(self, key: str) -> Reply:
        from . import media
        media.press(key, self.dry_run)
        # Quiet confirmation: an empty reply means Friday doesn't talk over your music.
        return Reply("", data={"media": key})

    def _volume_set(self, percent: int) -> Reply:
        from . import media
        if not self.dry_run:
            percent = media.set_volume(percent)
        return Reply(f"Volume {min(100, percent)} percent.")

    def _volume_step(self, m: re.Match) -> Reply:
        from . import media
        text = m.group(0).lower()
        up = bool(re.search(r"\bup\b|louder|increase|raise", text))
        step = 25 if "a lot" in text else 5 if re.search(r"a (little|bit)", text) else 10
        if self.dry_run:
            return Reply(f"Volume {'up' if up else 'down'}.", data={"step": step if up else -step})
        new = media.set_volume(media.get_volume() + (step if up else -step))
        return Reply(f"Volume {new} percent.")

    def _mute(self, mute: bool) -> Reply:
        from . import media
        if not self.dry_run:
            media.set_mute(mute)
        return Reply("" if mute else "Sound on.")

    def _volume_report(self) -> Reply:
        from . import media
        if media.is_muted():
            return Reply("The sound is muted.")
        return Reply(f"The volume is at {media.get_volume()} percent.")

    def _play(self, what: str, where: str | None) -> Reply:
        from . import media
        what = re.sub(r"^(the song|the video|song|video|some)\s+", "", what.strip(), flags=re.I)
        where = where or self.cfg.get("media.default_player", "youtube")
        if where == "spotify":
            media.search_site("spotify", what, self.dry_run)
            return Reply(f"Searching Spotify for {what}.")
        found, _ = media.play_on_youtube(what, self.dry_run) if not self.dry_run else (True, "")
        return Reply(f"Playing {what} on YouTube." if found else f"Here are the YouTube results for {what}.")

    def _search_site(self, site: str, query: str) -> Reply:
        from . import media
        media.search_site(site.lower(), query, self.dry_run)
        return Reply(f"Searching {site.title() if site.lower() != 'x' else 'X'} for {query}.")

    def _windows(self):
        return self.window_lister() if self.window_lister else None

    def _close(self, name: str) -> Reply:
        from . import media
        if re.fullmatch(r"(friday|yourself|you)", name.strip(), re.I):
            return Reply("To close me, type quit in my window.")
        w, score = media.find_window(name, self._windows())
        if not w or score < 0.6:
            return Reply(f"{name} doesn't seem to be open.")
        if w["name"].lower() in media.PROTECTED:
            return Reply(f"I won't close {w['name']} by voice, to be safe.")
        if not self.dry_run:
            media.close_window(w)
        return Reply(f"Closing {w['name'].title() if w['name'].islower() else w['name']}.")

    def _focus(self, name: str) -> Reply | None:
        from . import media
        if re.fullmatch(r"(my )?(schedule|calendar|agenda|tasks|reminders)", name.strip(), re.I):
            return None  # "show my calendar" is a question, handled later
        w, score = media.find_window(name, self._windows())
        if not w or score < 0.6:
            # Not running: open it instead.
            return self._open(name)
        if not self.dry_run:
            media.focus_window(w)
        return Reply(f"Switching to {w['name'].title() if w['name'].islower() else w['name']}.")

    def _login(self) -> Reply:
        """Opens a PowerShell window that runs the Claude CLI sign-in (browser based)."""
        from .llm import find_claude_cli
        cli = find_claude_cli(self.cfg.get("llm.claude_cli_path", ""))
        if not cli:
            return Reply("I couldn't find the Claude CLI on this computer.")
        if not self.dry_run:
            import subprocess
            subprocess.Popen(["powershell", "-NoExit", "-Command", f"& '{cli}' auth login"],
                             creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        return Reply("I've opened a window to sign in to Claude. Finish the sign-in in your browser, "
                     "then I can answer questions and write your briefings.")

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

        backends = self.llm_backends if self.llm_backends is not None else build_backends(self.cfg, purpose="chat")
        now = self.now()
        system = (
            "You are Friday, a concise voice assistant. Your answer is spoken aloud: plain sentences, no markdown, "
            "no lists, at most three short sentences. If you are unsure or the question needs live data you don't "
            f"have, say so briefly. Current local time: {now:%A %d %B %Y %H:%M}."
        )
        announced = False
        for backend in backends:
            if not backend.available():
                continue
            if not announced and self.on_thinking:
                self.on_thinking()   # e.g. say "One moment." so a slow answer doesn't feel like silence
                announced = True
            try:
                return Reply(backend.complete(system, text))
            except LLMError:
                continue
        return Reply("I can only handle commands right now, like opening apps, reminders, events and tasks. "
                     "Sign in to the Claude CLI and I can answer general questions too.")
