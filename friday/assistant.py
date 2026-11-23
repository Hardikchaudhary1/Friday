"""Understands a spoken (or typed) command and carries it out.

Common commands are matched with patterns, so they work instantly and offline.
Anything else goes to Claude for a short spoken answer, when Claude is available.
"""
from __future__ import annotations

import re
import urllib.parse
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
    # Common mishearings of "Spotify".
    text = re.sub(r"\b(sporty\s?(bar|fy|fi|five|phi)|spot\s?(a|the)?\s?fly|spotty\s?(fy|fi|five)|spot\s?fi|"
                  r"spotify'?s)\b", "Spotify", text, flags=re.I)
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
R_CLEAR = _rx(r"^(remove|delete|clear|cancel|wipe|erase)( out)?( all)?( of)?( my| the)?\s+"
              r"(schedules?|calendar|events|appointments|meetings|reminders|tasks|to-?dos?|to-?do list|everything)"
              r"( for (today|tomorrow))?$")
R_YES = _rx(r"^(yes|yeah|yep|sure|confirm|do it|go ahead|ok|okay)\b")
R_CANCEL_REMINDER = _rx(r"^(cancel|delete|remove)\s+(the\s+|my\s+)?(last\s+)?reminder(\s+(to|about|for)\s+(.+))?$")
R_CANCEL_EVENT = _rx(r"^(cancel|delete|remove|drop)\s+(the\s+|my\s+)?(event|meeting|appointment|task|to-?do)?\s*"
                     r"(called|named|about|for)?\s*(.+?)(\s+(event|meeting|task|from my (calendar|list|tasks|schedule)))?$")
R_MOVE = _rx(r"^(move|reschedule|change|push|shift|postpone|bring forward)\s+(the time of\s+|the date of\s+)?"
             r"(the\s+|my\s+)?(event|meeting|appointment|task|to-?do|reminder)?\s*(about|for|called|named|to)?\s*"
             r"(.+?)\s+(to|for|until|till)\s+(.+)$")
_DATE_WORDS = _rx(r"\b(today|tonight|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|next|"
                  r"\d{1,2}(st|nd|rd|th)|january|february|march|april|may|june|july|august|september|october|"
                  r"november|december|in \d+ (days?|weeks?))\b")
# ---- media, volume, sites, windows ----
_MEDIA_OBJ = r"( the| this| that| my)?( music| song| track| video| it| playback| spotify| youtube| audio)?"
# A bare "stop" stops Friday talking; "stop the music" pauses media.
R_PAUSE = _rx(rf"^(pause|resume|unpause|continue|play){_MEDIA_OBJ}( please)?$|^play (some )?music$|"
              r"^stop( the| this| my)? (music|song|track|video|playback|spotify|youtube|audio)$")
R_NEXT = _rx(rf"^(next|skip|skip ahead|play the next){_MEDIA_OBJ}( please)?$|^(next one|skip this( one)?)$")
R_PREV = _rx(r"^((play|go to|go back to|skip back to|back to)\s+)?(the\s+)?(previous|last|prior|earlier)"
             r"(\s+(one|song|track|video))?( please)?$|^(go back|back|skip back|go back one|back one)"
             r"(\s+(a|one)\s+(song|track))?( please)?$|^previous$")
R_REPLAY = _rx(r"^(replay|restart|start over|play again)(\s+(this|the|that))?(\s+(song|track|video|one))?( again)?"
               r"( from the (start|beginning))?$|^(play (this|the|that) (song|track|video) again)$")
R_VOL_SET = _rx(r"^(set |turn |change )?(the )?volume (to |at )?(\d{1,3})( percent| %|%)?$")
R_VOL_STEP = _rx(r"^(turn (it|the volume|the music) (up|down)|volume (up|down)|(louder|quieter|softer)( please)?|"
                 r"(increase|decrease|raise|lower|reduce) (the )?volume)( a (lot|little|bit))?$")
R_MUTE = _rx(r"^(mute|unmute)( the)?( sound| audio| volume| music| it)?$")
R_WHAT_VOL = _rx(r"^what('s| is) the volume$")
R_PLAY_ON = _rx(r"^play (.+?) (on|from|in|using|with) (youtube|spotify)$")
R_PLAY_THAT = _rx(r"^play (that|this|the) (song|track|one|first one|first song|top result)$|^play it$")
# "next track" is sometimes heard as "next check": a short next/previous phrase is still the media key.
R_NEXT_LOOSE = _rx(r"^(next|skip)\s+\w+$")
R_PREV_LOOSE = _rx(r"^(previous)\s+\w+$")
R_SPOTIFY_PLAY = _rx(r"^(play|start|resume|put on) (spotify|music on spotify|something on spotify)$")
GENERIC_MUSIC = _rx(r"^(something|anything|music|some music|my music|songs|some songs|a song|my songs|"
                    r"my playlist|a playlist|my liked songs|liked songs)$")
# Single-word commands Whisper often mishears when said alone.
MISHEARD = {"late": "play", "plate": "play", "played": "play", "lay": "play", "pay": "play",
            "paws": "pause", "pours": "pause", "pose": "pause", "pause it": "pause", "posts": "pause",
            "necks": "next", "nexed": "next", "text": "next", "skipped": "skip", "scape": "skip"}
R_PLAY_ANY = _rx(r"^play (.+)$")
R_SEARCH_ON = _rx(r"^(search|look up|find|look for)( for)? (.+?) on (youtube|google|amazon|github|wikipedia|reddit|"
                  r"linkedin|spotify|netflix|twitter|x|maps|google maps|news|images|tradingview)$")
R_SEARCH_SITE_FIRST = _rx(r"^search (youtube|google|amazon|github|wikipedia|reddit|linkedin|spotify|netflix|maps|news)"
                          r" for (.+)$")
_BROWSER = r"(brave|chrome|google chrome|edge|microsoft edge|firefox|opera)"
R_SEARCH_BROWSER = _rx(rf"^(search|google|look up|find)( the web)?( for)? (.+?) (on|in|using|with) {_BROWSER}( browser)?$")
R_WEATHER = _rx(r"\b(weather|forecast|temperature outside|is it (going to )?rain(ing)?|will it rain|"
                r"do i need an umbrella|how (hot|cold) is it)\b")
R_WEATHER_CITY = _rx(r"\b(?:in|for|at)\s+([a-z][a-z .'-]+?)(?:\s+(?:today|tomorrow|now|right now|this week))?$")
R_SET_CITY = _rx(r"^(my city is|i live in|i'm in|i am in|set my (city|location) to|my location is|"
                 r"use) (.+?)( for (the )?weather)?$")
R_DASHBOARD = _rx(r"^(open|show|show me|launch|bring up|pull up)( me)?( the| my)? (market |signal |markets )?"
                  r"dashboard$")
R_READ_DASHBOARD = _rx(r"^(read|summari[sz]e|go through|tell me about)( me)?( the| my)? (market |signal )?dashboard$")
R_READ_IT = _rx(r"^(read|read it|read it out|read it to me|read that|read this|summari[sz]e it|summari[sz]e that|"
                r"what does it say|what's on it|what is on it|tell me what it says|go through it)$")
R_MARKET_EVENTS = _rx(r"\b(what'?s|what is|what are) (moving|driving) (the )?markets?\b|\b(top|biggest|main) market "
                      r"(events|news|stories|movers)\b|\bmarket events\b")
R_MARKET_RISK = _rx(r"\b(market risk|risk (view|summary|overview)|which assets are under pressure|"
                    r"where('?s| is) the (risk|pressure))\b")
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
        self.last_search: tuple[str, str] | None = None   # for "play that song"
        self.signal_api = None       # tests replace the Signal connection
        self.context: str | None = None   # what Friday last showed you, for "read it"
        self.weather_lookup = None   # tests replace the live geocoder and forecast
        self.weather_report = None
        self.window_lister = None          # tests replace the live window list
        self.morning_hour = int(cfg.get("reminders.morning_hour", 9))
        self.before_minutes = int(cfg.get("reminders.before_minutes", 30))

    # ---- entry point -----------------------------------------------------

    def handle(self, raw: str) -> Reply:
        text = normalise_speech(raw)
        if not text:
            return Reply("Yes?")
        text = MISHEARD.get(text.lower(), text)

        if self.pending:
            pending, self.pending = self.pending, None
            if not R_STOP.match(text) and self.now() - pending["at"] < timedelta(minutes=2):
                return self._answer_pending(pending, text)

        for pattern, handler in (
            # Media first: these are short and said mid-task, so they must be quick and silent.
            (R_PLAY_THAT, lambda m: self._play_that()),   # before play/pause: "play that song" after a search
            (R_SPOTIFY_PLAY, lambda m: self._spotify_play()),
            (R_PAUSE, lambda m: self._media("play_pause")),
            (R_NEXT, lambda m: self._media("next")),
            (R_REPLAY, lambda m: self._media("previous", restart=True)),
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
            (R_CLEAR, self._clear),
            (R_MOVE, self._move),
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
            (R_READ_DASHBOARD, lambda m: self._read_dashboard()),
            (R_READ_IT, lambda m: self._read_it()),
            (R_DASHBOARD, lambda m: self._dashboard()),
            (R_MARKET_EVENTS, lambda m: self._market_events()),
            (R_MARKET_RISK, lambda m: self._market_risk()),
            (R_CLOSE, lambda m: self._close(m.group(2))),
            (R_FOCUS, lambda m: self._focus(m.group(3))),
            (R_OPEN, lambda m: self._open(m.group(3))),
            (R_PLAY_ON, lambda m: self._play(m.group(1), m.group(3).lower())),
            (R_PLAY_ANY, lambda m: self._play(m.group(1), None)),
            (R_SET_CITY, lambda m: self._set_city(m.group(3))),
            (R_WEATHER, lambda m: self._weather(text)),
            (R_SEARCH_BROWSER, lambda m: self._search_in_browser(m.group(4), m.group(6))),
            (R_SEARCH_ON, lambda m: self._search_site(m.group(4), m.group(3))),
            (R_SEARCH_SITE_FIRST, lambda m: self._search_site(m.group(1), m.group(2))),
            (R_SEARCH, lambda m: Reply(web_search(m.group(5), self.dry_run).message)),
            (R_NEXT_LOOSE, lambda m: self._media("next")),
            (R_PREV_LOOSE, lambda m: self._media("previous")),
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
        if pending["kind"] == "weather_city":
            city = re.sub(r"^(it's|it is|i'm in|i am in|i live in|in)\s+", "", text, flags=re.I)
            return self._set_city(city, then_report=pending.get("day", "today"))
        if pending["kind"] == "clear_confirm":
            if R_YES.match(text):
                return self._do_clear(pending["what"], pending["day"])
            return Reply("Okay, I've left everything as it is.")
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

    def _media(self, key: str, restart: bool = False) -> Reply:
        from . import media
        if key == "previous" and not restart:
            # Players (Spotify, YouTube) treat the first "previous" press as "restart this song"
            # once a track is a few seconds in; a second quick press goes to the previous track.
            media.press_previous_track(self.dry_run)
            return Reply("", data={"media": "previous"})
        media.press(key, self.dry_run)
        # Quiet confirmation: an empty reply means Friday doesn't talk over your music.
        return Reply("", data={"media": "restart" if restart else key})

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
        if where == "spotify" and GENERIC_MUSIC.match(what):
            return self._spotify_play()
        if where == "spotify":
            return self._spotify_song(what)
        found, _ = media.play_on_youtube(what, self.dry_run) if not self.dry_run else (True, "")
        return Reply(f"Playing {what} on YouTube." if found else f"Here are the YouTube results for {what}.")

    def _spotify_play(self) -> Reply:
        """'Play music on Spotify': resume Spotify, opening it first if it isn't running."""
        from . import media
        running = any(w["name"].lower() == "spotify" for w in (self._windows() or
                                                                 ([] if self.dry_run else media.running_windows())))
        if running:
            media.press("play_pause", self.dry_run)
            return Reply("", data={"media": "play_pause"})
        result = open_target("spotify", self.apps, dry_run=self.dry_run)
        if not result.ok:
            return Reply("Spotify doesn't seem to be installed.")
        if not self.dry_run:
            # Give Spotify a few seconds to start, then press play (it resumes your last track).
            import threading
            threading.Timer(6.0, media.press, args=("play_pause",)).start()
        return Reply("Opening Spotify and starting your music.")

    # ---- Signal: dashboard, market events, risk ----------------------------------

    def _signal(self):
        from . import signal_link
        return self.signal_api or signal_link

    def _signal_up(self) -> bool:
        link = self._signal()
        if link.is_running(self.cfg):
            return True
        if self.dry_run:
            return False
        return link.start(self.cfg)

    def _dashboard(self) -> Reply:
        import webbrowser
        if not self._signal_up():
            return Reply("I couldn't start the market dashboard. Check that the Signal project is installed.")
        if not self.dry_run:
            webbrowser.open(self._signal().base_url(self.cfg) + "/")
        self.context = "dashboard"
        return Reply("Opening your market dashboard. Say read it if you'd like me to go through it.")

    def _read_it(self) -> Reply:
        """'Read it' refers to whatever Friday last showed you."""
        if self.context == "dashboard":
            return self._read_dashboard()
        return Reply("Read what? I can read your dashboard, your tasks, your reminders or your schedule.")

    def _read_dashboard(self) -> Reply:
        """A spoken version of the dashboard: how much is tracked, top events, and where the pressure is."""
        if not self._signal_up():
            return Reply("The market dashboard isn't running right now.")
        link = self._signal()
        try:
            st = link.status(self.cfg)
        except (OSError, ValueError, AttributeError):
            st = {}
        events = self._market_events().text
        risk = self._market_risk().text
        head = (f"Your dashboard is tracking {st['stories']} events from {st['articles']} articles. "
                if st.get("stories") else "")
        return Reply(head + events + " " + risk)

    def _market_events(self) -> Reply:
        if not self._signal_up():
            return Reply("The market events service isn't running. Say brief me for the full briefing instead.")
        try:
            events = self._signal().top_events(self.cfg, 3)
        except (OSError, ValueError):
            return Reply("I couldn't read the market events just now.")
        if not events:
            return Reply("Nothing market-moving has come up yet. Signal is still gathering news.")
        parts = []
        for e in events:
            line = e["title"]
            for _ in range(2):   # drop " - Publisher" / " | Site" / " – business live" tails
                line = re.sub(r"\s+[-|–]\s+[^-|–]{2,60}$", "", line)
            line = line.rstrip(".")
            if e.get("impacts"):
                line += ". It could push " + ", ".join(i.split(" (")[0] for i in e["impacts"][:2])
            parts.append(line + ".")
        return Reply("The top market events: " + " Next, ".join(parts))

    def _market_risk(self) -> Reply:
        if not self._signal_up():
            return Reply("The market events service isn't running right now.")
        try:
            r = self._signal().risk(self.cfg)
        except (OSError, ValueError):
            return Reply("I couldn't read the risk view just now.")
        assets = r.get("assets", [])[:4]
        if not assets:
            return Reply("No clear pressure on any asset in the last day.")
        parts = [f"{a['label']} {'upward' if a['pressure'] > 0 else 'downward'}" for a in assets]
        regions = [x["region"] for x in r.get("regions", [])[:2]]
        tail = f" The busiest regions are {' and '.join(regions)}." if regions else ""
        return Reply("Most pressure in the last day: " + ", ".join(parts) + "." + tail)

    # ---- weather and browsers ----------------------------------------------------

    def _prefs_path(self):
        return self.cfg.data_dir / "prefs.json"

    def _load_prefs(self) -> dict:
        import json
        try:
            return json.loads(self._prefs_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_prefs(self, prefs: dict) -> None:
        import json
        self._prefs_path().write_text(json.dumps(prefs, indent=2), encoding="utf-8")

    def _set_city(self, city: str, then_report: str | None = None) -> Reply:
        from . import weather
        try:
            place = self.weather_lookup(city) if self.weather_lookup else weather.find_place(city)
        except OSError:
            return Reply("I can't reach the weather service right now.")
        if not place:
            return self._ask("weather_city", f"I couldn't find {city}. Which city should I use?", day=then_report)
        prefs = self._load_prefs()
        prefs["city"] = {"name": place.name, "country": place.country, "lat": place.lat, "lon": place.lon}
        self._save_prefs(prefs)
        if then_report:
            return self._weather_for(place, then_report, prefix=f"Got it, I'll use {place.name}. ")
        return Reply(f"Okay, I'll use {place.name}, {place.country} for the weather.")

    def _weather(self, text: str) -> Reply:
        from . import weather
        day = "tomorrow" if re.search(r"\btomorrow\b", text, re.I) else "today"
        # Drop "search for ... on Brave" wrapping so only a real place name is left.
        cleaned = re.sub(rf"\s+(on|in|using|with) {_BROWSER}( browser)?$", "", text, flags=re.I)
        cleaned = re.sub(r"^(search|google|look up|find)( the web)?( for)?\s+", "", cleaned, flags=re.I)
        m = R_WEATHER_CITY.search(cleaned)
        city = m.group(1).strip() if m else None
        if city and (re.search(r"\b(weather|forecast|today|tomorrow|now|week|morning|evening|afternoon)\b", city, re.I)
                     or city.lower() in ("here", "my city", "my area")):
            city = None
        try:
            if city:
                place = self.weather_lookup(city) if self.weather_lookup else weather.find_place(city)
                if not place:
                    return Reply(f"I couldn't find a place called {city}.")
            else:
                saved = self._load_prefs().get("city")
                if not saved:
                    return self._ask("weather_city", "Which city are you in? I'll remember it for next time.", day=day)
                place = weather.Place(saved["name"], saved.get("country", ""), saved["lat"], saved["lon"])
            return self._weather_for(place, day)
        except OSError:
            return Reply("I can't reach the weather service right now.")

    def _weather_for(self, place, day: str, prefix: str = "") -> Reply:
        from . import weather
        report = self.weather_report(place, day) if self.weather_report else weather.weather_report(place, day)
        return Reply(prefix + report)

    def _search_in_browser(self, query: str, browser: str) -> Reply:
        from . import media
        if re.fullmatch(r"(the )?(weather|forecast)( today| tomorrow)?", query.strip(), re.I):
            return self._weather(query)
        url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)
        used = media.open_in_browser(browser, url, self.dry_run)
        name = browser.title()
        return Reply(f"Searching {query} in {name}." if used else
                     f"I couldn't find {name}, so I've searched {query} in your default browser.")

    def _spotify_song(self, what: str) -> Reply:
        """Plays a specific song: Spotify's search, then its Play button (see spotify.py)."""
        self.last_search = ("spotify", what)
        if self.dry_run:
            return Reply(f"Playing {what} on Spotify.")
        from . import spotify
        if not spotify.spotify_window():
            opened = open_target("spotify", self.apps)
            if not opened.ok:
                return Reply("Spotify doesn't seem to be installed.")
            import time as _t
            for _ in range(40):                    # wait up to ~10 s for Spotify to start
                _t.sleep(0.25)
                if spotify.spotify_window():
                    break
            _t.sleep(2)
        ok, title = spotify.play_song(what)
        if ok:
            return Reply("", data={"media": f"playing {title}"})
        from . import media
        media.search_site("spotify", what)
        return Reply(f"I couldn't start {what} automatically, so I've opened the search in Spotify.")

    def _play_that(self) -> Reply:
        if not getattr(self, "last_search", None):
            return self._media("play_pause")
        site, query = self.last_search
        if site == "spotify":
            return self._spotify_song(query)
        return self._play(query, "youtube")

    def _search_site(self, site: str, query: str) -> Reply:
        from . import media
        self.last_search = (site.lower(), query)
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

    # ---- clearing in bulk (always confirmed) ------------------------------------

    def _clear_targets(self, what: str, day: str | None):
        today = self.now().date()
        if day == "today":
            start = end = today
        elif day == "tomorrow":
            start = end = today + timedelta(days=1)
        else:
            start, end = today - timedelta(days=3650), today + timedelta(days=3650)
        events = self.schedule.events_between(start, end) if what in ("events", "everything") else []
        reminders = []
        if what in ("reminders", "everything"):
            reminders = [r for r in self.schedule.pending_reminders()
                         if r.event_id is None and (day is None or start <= r.due_at.date() <= end)]
        tasks = self.tasks.open_tasks() if what in ("tasks", "everything") and day is None else []
        return events, reminders, tasks

    @staticmethod
    def _clear_kind(word: str) -> str:
        word = word.lower()
        if word.startswith(("schedule", "calendar", "event", "appointment", "meeting")):
            return "events"
        if word.startswith("reminder"):
            return "reminders"
        if word.startswith(("task", "to")):
            return "tasks"
        return "everything"

    def _clear(self, m: re.Match) -> Reply:
        what, day = self._clear_kind(m.group(6)), (m.group(8) or "").lower() or None
        events, reminders, tasks = self._clear_targets(what, day)
        parts = [f"{n} {label}{'s' if n != 1 else ''}" for n, label in
                 ((len(events), "event"), (len(reminders), "reminder"), (len(tasks), "task")) if n]
        if not parts:
            # Point at what *is* there: people often call tasks "events".
            _, other_rem, other_tasks = self._clear_targets("everything", None)
            hints = []
            if what != "tasks" and other_tasks:
                hints.append(f"{len(other_tasks)} task{'s' if len(other_tasks) != 1 else ''} on your to-do list "
                             f"(say delete all my tasks)")
            if what != "reminders" and other_rem:
                hints.append(f"{len(other_rem)} reminder{'s' if len(other_rem) != 1 else ''} "
                             f"(say delete all my reminders)")
            label = {"events": "events", "reminders": "reminders", "tasks": "tasks"}.get(what, "items")
            if hints:
                return Reply(f"You have no {label}{' for ' + day if day else ''}. You do have " + " and ".join(hints) + ".")
            return Reply("There's nothing to remove.")
        listing = " and ".join(parts) if len(parts) <= 2 else ", ".join(parts[:-1]) + " and " + parts[-1]
        return self._ask("clear_confirm", f"Delete {listing}{' for ' + day if day else ''}? Say yes to confirm.",
                         what=what, day=day)

    def _do_clear(self, what: str, day: str | None) -> Reply:
        events, reminders, tasks = self._clear_targets(what, day)
        for e in events:
            self.schedule.delete_event(e.id)       # its reminders go with it
        for r in reminders:
            self.schedule.cancel_reminder(r.id)
        for t in tasks:
            self.tasks.remove(t.id)
        return Reply(f"Done. Removed {len(events) + len(reminders) + len(tasks)} item"
                     f"{'s' if len(events) + len(reminders) + len(tasks) != 1 else ''}.")

    # ---- finding, deleting and moving one item by name ---------------------------

    def _find_item(self, name: str, kind_hint: str | None = None):
        """Best match for a spoken name across events, standalone reminders and tasks.
        Returns (kind, item, score) or None. A kind hint ("event", "task") is preferred, not required:
        you may call a task an event."""
        today = self.now().date()
        name = re.sub(r"\s+(event|task|reminder|meeting|appointment)$", "", name.strip(), flags=re.I)
        candidates = []
        for e in self.schedule.events_between(today - timedelta(days=1), today + timedelta(days=366)):
            candidates.append(("event", e, e.title))
        for r in self.schedule.pending_reminders():
            if r.event_id is None:
                candidates.append(("reminder", r, r.text))
        for t in self.tasks.open_tasks():
            candidates.append(("task", t, t.text))
        best = None
        for kind, item, title in candidates:
            q, t = name.lower(), title.lower()
            words_q, words_t = set(q.split()), set(t.split())
            score = 1.0 if q == t else 0.9 if q in t else max(
                SequenceMatcher(None, q, t).ratio(),
                len(words_q & words_t) / max(1, len(words_q)) * 0.85)
            if kind_hint and kind == kind_hint:
                score += 0.05
            if best is None or score > best[2]:
                best = (kind, item, score)
        return best if best and best[2] >= 0.5 else None

    @staticmethod
    def _kind_from_word(word: str | None) -> str | None:
        if not word:
            return None
        word = word.lower()
        if word in ("event", "meeting", "appointment"):
            return "event"
        if word.startswith(("task", "to")):
            return "task"
        return "reminder" if word == "reminder" else None

    def _cancel_event(self, m: re.Match) -> Reply | None:
        name = m.group(5)
        if re.fullmatch(r"(it|that|this|all|everything)", name.strip(), re.I):
            return None
        found = self._find_item(name, self._kind_from_word(m.group(3)))
        if not found:
            return Reply(f"I couldn't find an event, reminder or task called {name}.")
        kind, item, _ = found
        today = self.now().date()
        if kind == "event":
            self.schedule.delete_event(item.id)
            return Reply(f"Cancelled {item.spoken(today)}.")
        if kind == "reminder":
            self.schedule.cancel_reminder(item.id)
            return Reply(f"Cancelled the reminder: {item.text}.")
        self.tasks.remove(item.id)
        return Reply(f"Removed the task: {item.text}.")

    def _move(self, m: re.Match) -> Reply | None:
        name, when_text = m.group(6), m.group(8)
        if re.match(r"^(the\s+)?volume\b", name, re.I):
            return None
        found = self._find_item(name, self._kind_from_word(m.group(4)))
        if not found:
            return Reply(f"I couldn't find an event, reminder or task called {name}.")
        kind, item, _ = found
        now = self.now()
        p = parse(when_text, now, self.morning_hour)
        if p.when is None:
            return Reply(f"Sorry, I didn't catch the new time for {name}.")
        said_date = bool(_DATE_WORDS.search(when_text))
        if kind == "task":
            self.tasks.set_due(item.id, p.when.date())
            return Reply(f"Moved the task {item.text} to {p.when:%A} {p.when.day} {p.when:%B}.")
        if kind == "event":
            day = p.when.date() if said_date or not p.has_time else item.day
            at = p.when.time() if p.has_time else item.at
            event, reminders = self.schedule.reschedule_event(item.id, day, at, morning_hour=self.morning_hour,
                                                              before_minutes=self.before_minutes, now=now)
            when = event.spoken(now.date()).removeprefix(event.title).strip().removeprefix("on ")
            return Reply(f"Moved {event.title} to {when}.")
        due = p.when
        if p.has_time and not said_date:
            due = datetime.combine(item.due_at.date(), p.when.time())
            if due <= now:
                due = p.when
        if not p.has_time:
            due = datetime.combine(p.when.date(), item.due_at.time())
        if due <= now:
            return Reply(f"{spoken_datetime(due, now)} has already passed.")
        self.schedule.reschedule_reminder(item.id, due)
        return Reply(f"Okay, I'll remind you to {item.text} {spoken_datetime(due, now)} instead.")

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
            "You are the conversational part of Friday, a voice assistant on the user's Windows laptop. Your answer "
            "is spoken aloud: plain sentences, no markdown, no lists, at most three short sentences. If you are "
            "unsure or the question needs live data you don't have, say so briefly.\n"
            "Friday handles actions itself through fixed phrases; you only get requests those phrases did not match. "
            "If the user seems to want one of these actions, tell them the exact phrase to say instead of saying it "
            "can't be done: 'open Spotify', 'close Spotify', 'switch to Brave', 'pause', 'resume', 'next song', "
            "'previous song', 'replay this song', 'set volume to 40', 'mute', 'play Believer on Spotify', "
            "'play lofi on YouTube', 'search X on Amazon', 'remind me to X tomorrow at 5 pm', "
            "'what are my reminders', 'cancel the reminder about X', 'add dentist on 5 December at 4 pm', "
            "'what's on my schedule tomorrow', 'move the dentist to Friday at 3 pm', 'delete X' (any event, "
            "reminder or task by name), 'delete all my events', 'delete all my reminders', 'delete all my tasks', "
            "'add task X', 'what are my tasks', 'mark X as done', 'move the task X to Friday', 'brief me'.\n"
            f"Current local time: {now:%A %d %B %Y %H:%M}."
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
