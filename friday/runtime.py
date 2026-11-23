"""The always-on assistant: speaks, listens, fires reminders, and runs the morning briefing.

    friday run     voice + typed commands, reminders, briefing once per day
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from datetime import datetime, timedelta

from rich.console import Console

from .assistant import Assistant, Reply
from .notify import toast
from .schedule import ScheduleStore, spoken_datetime
from .voice import speak

console = Console()


class Runtime:
    def __init__(self, cfg, use_voice_input: bool = True, briefing_on_start: bool = True):
        self.cfg = cfg
        self.assistant = Assistant(cfg)
        self.assistant.on_thinking = lambda: self.say("One moment.")
        self.schedule = ScheduleStore(cfg.data_dir / "friday.db")
        self.use_voice_input = use_voice_input
        self.briefing_on_start = briefing_on_start
        self.say_queue: queue.Queue[str] = queue.Queue()
        self.speaking = threading.Event()
        self.stop_speech = threading.Event()
        self.shutdown = threading.Event()
        self.command_lock = threading.Lock()
        self.listener = None
        self.briefing_lock = threading.Lock()
        self.speech_epoch = 0   # bumped by interrupt(); output from an older epoch is dropped
        self.prepared = None    # (timestamp, Briefing) kept ready by the refresh loop

    # ---- output ------------------------------------------------------------

    def say(self, text: str) -> None:
        if text:
            console.print(f"[bold cyan]Friday:[/bold cyan] {text}")
            # If something is already playing, start generating this one's audio now,
            # so it plays the moment the current sentence ends (no gap between paragraphs).
            if self.cfg.get("voice.engine", "edge") == "edge" and (self.speaking.is_set() or not self.say_queue.empty()):
                from .voice import prewarm
                threading.Thread(target=prewarm, args=([text], self.cfg.get("voice.edge_voice", "en-GB-SoniaNeural"),
                                                       self.cfg.get("voice.rate", "+0%")), daemon=True).start()
            self.say_queue.put(text)

    def _speaker_loop(self):
        while not self.shutdown.is_set():
            try:
                text = self.say_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self.stop_speech.clear()
            self.speaking.set()
            try:
                speak(text, self.cfg, log=lambda m: console.print(f"[dim]{m}[/dim]"), stop_event=self.stop_speech)
            except Exception as exc:
                console.print(f"[red]Could not speak: {exc}[/red]")
            finally:
                time.sleep(0.3)  # let the speaker echo die down before listening again
                self.speaking.clear()
                self.say_queue.task_done()

    def interrupt(self):
        """Stops current speech, anything queued, and the rest of a briefing still being written."""
        self.speech_epoch += 1
        self.stop_speech.set()
        while not self.say_queue.empty():
            try:
                self.say_queue.get_nowait()
                self.say_queue.task_done()
            except queue.Empty:
                break

    # ---- commands -------------------------------------------------------------

    def handle(self, text: str, source: str = "voice") -> Reply:
        console.print(f"[bold]You ({source}):[/bold] {text}")
        with self.command_lock:
            try:
                reply = self.assistant.handle(text)
            except Exception as exc:
                reply = Reply(f"Sorry, something went wrong: {exc}")
        if reply.action == "stop":
            self.interrupt()
        elif reply.action == "briefing":
            self.say(reply.text)
            # Started after the reply so its epoch is current.
            threading.Thread(target=self.run_briefing, daemon=True).start()
        else:
            if not reply.text and reply.data.get("media"):
                console.print(f"[dim]({reply.data['media'].replace('_', '/')})[/dim]")
            self.say(reply.text)
            # Friday asked something ("When should I remind you?"): listen for the answer
            # without needing the wake word again.
            if source == "voice" and self.listener and self.assistant.pending:
                threading.Thread(target=self._follow_up, daemon=True).start()
        return reply

    def _follow_up(self):
        self.say_queue.join()          # returns once everything queued has been spoken
        self.listener.trigger.set()

    def run_briefing(self, mark: bool = True):
        """Plays the prepared briefing if it's recent; otherwise writes one and starts
        speaking as soon as its first paragraph is ready."""
        from .briefing import gather, mark_briefed, save_briefing, write_briefing_streaming
        log = lambda m: console.print(f"[dim]{m}[/dim]")  # noqa: E731
        epoch = self.speech_epoch

        def say_part(part: str):
            if self.speech_epoch == epoch:   # dropped once you've said "stop"
                self.say(part)

        try:
            fresh_s = float(self.cfg.get("briefing.fresh_minutes", 45)) * 60
            with self.briefing_lock:
                prepared = self.prepared
            if prepared and time.time() - prepared[0] < fresh_s:
                log(f"Using the briefing prepared {int((time.time() - prepared[0]) / 60)} min ago.")
                for part in [p.strip() for p in prepared[1].text.split("\n\n") if p.strip()]:
                    say_part(part)
            else:
                data = gather(self.cfg)
                if self.speech_epoch != epoch:
                    return
                briefing = write_briefing_streaming(self.cfg, data, on_part=say_part, log=log)
                save_briefing(self.cfg, briefing)
                with self.briefing_lock:
                    self.prepared = (time.time(), briefing)
            if mark:
                mark_briefed(self.cfg)
        except Exception as exc:
            self.say(f"I couldn't put the briefing together: {exc}")

    def _refresh_loop(self):
        """Keeps a recent briefing ready so "brief me" can start immediately."""
        from .briefing import gather, save_briefing, write_briefing
        minutes = float(self.cfg.get("briefing.refresh_minutes", 60))
        if minutes <= 0:
            return
        while not self.shutdown.wait(minutes * 60):
            if not 6 <= datetime.now().hour < 23:   # no point refreshing overnight
                continue
            try:
                briefing = write_briefing(self.cfg, gather(self.cfg))
                save_briefing(self.cfg, briefing)
                with self.briefing_lock:
                    self.prepared = (time.time(), briefing)
                console.print("[dim]Briefing refreshed in the background.[/dim]")
            except Exception as exc:
                console.print(f"[dim]Background briefing refresh failed: {exc}[/dim]")

    # ---- reminders -------------------------------------------------------------

    def _reminder_loop(self):
        while not self.shutdown.is_set():
            now = datetime.now()
            for r in self.schedule.due_reminders(now):
                late = now - r.due_at > timedelta(minutes=10)
                spoken = (f"Reminder from {spoken_datetime(r.due_at, now)}: {r.text}" if late
                          else f"Reminder: {r.text}")
                toast("Friday reminder", r.text if not late else f"{r.text} (was due {r.due_at:%a %d %b %H:%M})")
                self.say(spoken)
                self.schedule.mark_fired(r.id)
            self.shutdown.wait(15)

    # ---- main ------------------------------------------------------------------

    def run(self):
        from .briefing import already_briefed_today
        from .__main__ import wait_for_network

        threading.Thread(target=self._speaker_loop, daemon=True, name="friday-speaker").start()
        threading.Thread(target=self._reminder_loop, daemon=True, name="friday-reminders").start()
        threading.Thread(target=self._prewarm_voice, daemon=True, name="friday-prewarm").start()
        threading.Thread(target=self._refresh_loop, daemon=True, name="friday-briefing-refresh").start()
        # Start Signal (market events + dashboard) in the background if it isn't already running.
        if self.cfg.get("signal.enabled", True) and self.cfg.get("signal.autostart", True):
            from . import signal_link
            threading.Thread(target=signal_link.start, args=(self.cfg, lambda m: console.print(f"[dim]{m}[/dim]")),
                             daemon=True, name="friday-signal-start").start()

        if self.use_voice_input:
            self._start_voice_input()

        # Greet once, when Friday comes online; the briefing itself starts straight with the news.
        self.say(self.greeting())
        if self.briefing_on_start and not already_briefed_today(self.cfg):
            self.say("Your briefing is coming up.")
            if wait_for_network(90):
                threading.Thread(target=self.run_briefing, daemon=True).start()

        console.print("[dim]Type a command and press Enter (or say \"Friday ...\"). Type 'quit' to exit.[/dim]")
        try:
            for line in sys.stdin:
                line = line.strip()
                if line.lower() in ("quit", "exit"):
                    break
                if line:
                    self.handle(line, source="typed")
        except KeyboardInterrupt:
            pass
        self.shutdown.set()
        self.interrupt()

    def greeting(self, hour: int | None = None) -> str:
        hour = datetime.now().hour if hour is None else hour
        part = "Good morning" if 5 <= hour < 12 else "Good afternoon" if 12 <= hour < 17 else "Good evening"
        name = self.cfg.get("user_name", "")
        return f"{part}{', ' + name if name else ''}. Friday is online."

    def common_phrases(self) -> list[str]:
        """Replies Friday says often. Generating them once means they play instantly afterwards."""
        phrases = [self.greeting(h) for h in (8, 14, 20)] + ["Your briefing is coming up."]
        phrases += ["One moment.", "Yes?", "Sound on.", "You're welcome.", "Which app should I open?",
                   "What should I remind you about?", "Your task list is empty.", "You have no upcoming reminders.",
                   "Nothing on your calendar today.", "Nothing on your calendar tomorrow.", "Here's your briefing."]
        phrases += [f"Volume {v} percent." for v in range(0, 101, 5)]
        try:
            apps = self.assistant.apps.apps()
        except Exception:
            apps = []
        phrases += [f"Opening {a.spoken_name}." for a in apps]
        from .apps import WEBSITES
        phrases += [f"Opening {site}." for site in WEBSITES]
        return phrases

    def _prewarm_voice(self):
        if self.cfg.get("voice.engine", "edge") != "edge":
            return
        from .voice import clean_cache, prewarm
        clean_cache()
        time.sleep(20)  # let startup (briefing, models) go first
        prewarm(self.common_phrases(), self.cfg.get("voice.edge_voice", "en-GB-SoniaNeural"),
                self.cfg.get("voice.rate", "+0%"))

    def _start_voice_input(self):
        from .listen import Hotkey, Listener, voice_input_ready

        ok, reason = voice_input_ready(self.cfg)
        if not ok:
            console.print(f"[yellow]Voice input off: {reason}. Typed commands still work.[/yellow]")
            return
        try:
            import winsound
            beep = lambda: winsound.Beep(880, 120)  # noqa: E731
        except Exception:
            beep = None

        from .media import Ducker
        ducker = Ducker(level=float(self.cfg.get("listen.duck_level", 0.3)))

        def on_wake():
            self.interrupt()
            if beep:
                beep()
            # Turn music down while you speak, so Friday can hear you over it.
            if self.cfg.get("listen.duck_while_listening", True):
                ducker.duck()

        def vocabulary():
            from .apps import WEBSITES
            try:
                apps = [a.name for a in self.assistant.apps.apps()]
            except Exception:
                apps = []
            return list(self.assistant.apps.aliases) + list(WEBSITES) + apps

        self.listener = Listener(self.cfg, on_command=lambda t: self.handle(t), is_speaking=self.speaking.is_set,
                                 on_wake=on_wake, log=lambda m: console.print(f"[dim]{m}[/dim]"),
                                 vocabulary=vocabulary, on_heard=ducker.restore,
                                 on_stop_word=self.interrupt)
        self.listener.start()

        def hotkey_pressed():
            self.interrupt()
            self.listener.trigger.set()
        Hotkey(hotkey_pressed).start()
