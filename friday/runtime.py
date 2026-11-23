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

    def interrupt(self):
        """Stops current speech and anything queued."""
        self.stop_speech.set()
        while not self.say_queue.empty():
            try:
                self.say_queue.get_nowait()
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
            self.say(reply.text)
        return reply

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

        def on_wake():
            self.interrupt()
            if beep:
                beep()

        self.listener = Listener(self.cfg, on_command=lambda t: self.handle(t), is_speaking=self.speaking.is_set,
                                 on_wake=on_wake, log=lambda m: console.print(f"[dim]{m}[/dim]"))
        self.listener.start()

        def hotkey_pressed():
            self.interrupt()
            self.listener.trigger.set()
        Hotkey(hotkey_pressed).start()
