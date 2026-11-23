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

    # ---- output ------------------------------------------------------------

    def say(self, text: str) -> None:
        if text:
            console.print(f"[bold cyan]Friday:[/bold cyan] {text}")
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
        """Stops current speech and anything queued."""
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
        from .briefing import gather, mark_briefed, save_briefing, write_briefing
        try:
            data = gather(self.cfg)
            briefing = write_briefing(self.cfg, data, log=lambda m: console.print(f"[dim]{m}[/dim]"))
            save_briefing(self.cfg, briefing)
            self.say(briefing.text)
            if mark:
                mark_briefed(self.cfg)
        except Exception as exc:
            self.say(f"I couldn't put the briefing together: {exc}")

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

        if self.use_voice_input:
            self._start_voice_input()

        if self.briefing_on_start and not already_briefed_today(self.cfg):
            hour = datetime.now().hour
            greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"
            self.say(f"{greeting}, {self.cfg.get('user_name', '')}. Give me a moment to prepare your briefing.")
            if wait_for_network(90):
                threading.Thread(target=self.run_briefing, daemon=True).start()
        else:
            self.say("Friday is online.")

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

    def common_phrases(self) -> list[str]:
        """Replies Friday says often. Generating them once means they play instantly afterwards."""
        phrases = ["One moment.", "Yes?", "Sound on.", "Friday is online.", "You're welcome.", "Which app should I open?",
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
        from .voice import prewarm
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
                                 vocabulary=vocabulary, on_heard=ducker.restore)
        self.listener.start()

        def hotkey_pressed():
            self.interrupt()
            self.listener.trigger.set()
        Hotkey(hotkey_pressed).start()
