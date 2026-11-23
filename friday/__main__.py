"""Command line for Friday.

    friday brief                  fetch data, write and speak the briefing
    friday brief --no-speak       same, text only
    friday brief --startup        what runs at logon: waits for the network, skips if
                                  today's briefing already played, keeps the window open
    friday task add "Call bank" --due today
    friday task list
    friday task done 3
    friday say "Hello"
    friday doctor                 check which AI and voice backends work
"""
from __future__ import annotations

import argparse
import socket
import sys
import time
from datetime import date

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .briefing import already_briefed_today, gather, mark_briefed, save_briefing, write_briefing
from .config import load_config
from .llm import build_backends, find_claude_cli
from .markets import format_quote
from .news import select_top
from .tasks import TaskStore, parse_due

console = Console()


def _log(msg: str) -> None:
    console.print(f"[dim]{msg}[/dim]")


def wait_for_network(timeout: float = 90) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.create_connection(("1.1.1.1", 443), timeout=3).close()
            return True
        except OSError:
            time.sleep(3)
    return False


def _market_table(quotes) -> Table:
    table = Table(title="Markets", show_header=False, box=None, padding=(0, 1))
    for q in quotes:
        colour = "white"
        if q.ok:
            colour = "green" if q.change_pct > 0 else "red" if q.change_pct < 0 else "white"
        table.add_row(f"[dim]{q.group}[/dim]", f"[{colour}]{format_quote(q)}[/{colour}]")
    return table


def cmd_brief(args, cfg) -> int:
    if args.startup:
        if cfg.get("briefing.once_per_day", True) and already_briefed_today(cfg) and not args.force:
            _log("Today's briefing already played. Use --force to run it again.")
            return 0
        _log("Waiting for the network...")
        if not wait_for_network():
            console.print("[red]No network after 90 seconds; giving up.[/red]")
            return 1

    with console.status("Gathering news and markets..."):
        data = gather(cfg)
    _log(f"{len(data.headlines)} unique headlines, "
         f"{sum(q.ok for q in data.quotes)}/{len(data.quotes)} quotes, {len(data.tasks)} tasks")
    for err in data.feed_errors:
        _log(f"feed issue: {err}")

    with console.status("Writing the briefing..."):
        briefing = write_briefing(cfg, data, log=_log)
    for note in briefing.notes:
        _log(note)

    console.print(_market_table(data.quotes))
    console.print(Panel(briefing.text, title=f"Friday  ·  {date.today():%A %d %B}",
                        subtitle=f"written by {briefing.backend}", border_style="cyan"))
    path = save_briefing(cfg, briefing)
    _log(f"Saved to {path}")

    if args.speak:
        from .voice import speak
        try:
            engine = speak(briefing.text, cfg, log=_log)
            _log(f"Spoken with the {engine} voice.")
        except KeyboardInterrupt:
            _log("Stopped speaking.")
        except Exception as exc:
            console.print(f"[red]Could not speak: {exc}[/red]")
    if args.speak or args.startup:
        mark_briefed(cfg)

    if args.startup:
        console.print("\n[bold]Top stories[/bold]")
        for h in select_top(data.headlines, 8, per_source=2):
            console.print(f"  • {h.title} [dim]({h.source})[/dim]")
        try:
            input("\nPress Enter to close.")
        except EOFError:
            pass
    return 0


def cmd_task(args, cfg) -> int:
    store = TaskStore(cfg.data_dir / "friday.db")
    today = date.today()
    if args.action == "add":
        task = store.add(" ".join(args.text), parse_due(args.due))
        console.print(f"Added #{task.id}: {task.text}" + (f" (due {task.due_label(today)})" if task.due else ""))
    elif args.action == "done":
        ok = store.complete(args.id)
        console.print(f"Marked #{args.id} done." if ok else f"[yellow]No open task #{args.id}.[/yellow]")
        return 0 if ok else 1
    elif args.action == "rm":
        ok = store.remove(args.id)
        console.print(f"Removed #{args.id}." if ok else f"[yellow]No task #{args.id}.[/yellow]")
        return 0 if ok else 1
    else:
        tasks = store.open_tasks()
        if not tasks:
            console.print("No open tasks.")
        for t in tasks:
            label = t.due_label(today)
            colour = "red" if t.due and t.due < today else "yellow" if t.due == today else "white"
            console.print(f"[dim]#{t.id:<3}[/dim] {t.text}" + (f"  [{colour}]{label}[/{colour}]" if label else ""))
    return 0


def cmd_say(args, cfg) -> int:
    from .voice import speak
    engine = speak(" ".join(args.text), cfg, log=_log)
    _log(f"Spoken with the {engine} voice.")
    return 0


def cmd_run(args, cfg) -> int:
    from .runtime import Runtime
    if args.startup and cfg.get("briefing.once_per_day", True) and already_briefed_today(cfg):
        _log("Today's briefing already played; starting without it.")
    Runtime(cfg, use_voice_input=not args.no_mic, briefing_on_start=not args.no_briefing).run()
    return 0


def cmd_ask(args, cfg) -> int:
    """One command, text in and text out. Add --speak to hear the answer."""
    from .assistant import Assistant
    reply = Assistant(cfg, dry_run=args.dry_run).handle(" ".join(args.text))
    console.print(reply.text or f"({reply.action})")
    if args.speak and reply.text:
        from .voice import speak
        speak(reply.text, cfg)
    return 0


def cmd_setup_voice(args, cfg) -> int:
    from .listen import download_models
    download_models(cfg, log=_log)
    return 0


def cmd_doctor(args, cfg) -> int:
    console.print(f"Claude CLI: {find_claude_cli(cfg.get('llm.claude_cli_path', '')) or '[red]not found[/red]'}")
    for b in build_backends(cfg):
        if not b.available():
            console.print(f"  {b.name}: [yellow]not configured[/yellow]")
            continue
        t = time.time()
        try:
            reply = b.complete("Reply with exactly the word: ready", "Status check.")
            console.print(f"  {b.name}: [green]ok[/green] ({time.time() - t:.1f}s) -> {reply[:40]!r}")
        except Exception as exc:
            console.print(f"  {b.name}: [red]failed[/red] {exc}")
    console.print(f"Voice engine: {cfg.get('voice.engine')} / {cfg.get('voice.edge_voice')}")
    from . import signal_link
    if signal_link.is_running(cfg):
        st = signal_link.status(cfg)
        console.print(f"Signal: [green]running[/green] at {signal_link.base_url(cfg)} "
                      f"({st.get('stories')} events from {st.get('articles')} articles)")
    else:
        console.print(f"Signal: [yellow]not running[/yellow] (Friday starts it automatically; "
                      f"project at {signal_link.project_dir(cfg)})")
    from .listen import voice_input_ready
    ok, reason = voice_input_ready(cfg)
    console.print(f"Voice input: {'[green]' if ok else '[yellow]'}{reason}")
    try:
        import sounddevice as sd
        console.print(f"Microphone: {sd.query_devices(kind='input')['name']}")
    except Exception as exc:
        console.print(f"Microphone: [red]{exc}[/red]")
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="friday", description="Your morning markets and tasks briefing.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("brief", help="write and speak today's briefing")
    p.add_argument("--no-speak", dest="speak", action="store_false")
    p.add_argument("--startup", action="store_true", help="logon mode")
    p.add_argument("--force", action="store_true", help="run even if already briefed today")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("task", help="manage your to-do list")
    tsub = p.add_subparsers(dest="action")
    a = tsub.add_parser("add"); a.add_argument("text", nargs="+"); a.add_argument("--due")
    d = tsub.add_parser("done"); d.add_argument("id", type=int)
    r = tsub.add_parser("rm"); r.add_argument("id", type=int)
    tsub.add_parser("list")
    p.set_defaults(func=cmd_task)

    p = sub.add_parser("say", help="speak some text")
    p.add_argument("text", nargs="+")
    p.set_defaults(func=cmd_say)

    p = sub.add_parser("doctor", help="check AI and voice backends")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("run", help="always-on assistant: voice, reminders, briefing")
    p.add_argument("--no-mic", action="store_true", help="typed commands only")
    p.add_argument("--no-briefing", action="store_true")
    p.add_argument("--startup", action="store_true", help="logon mode")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("ask", help="run one command, e.g. friday ask remind me to stretch in 10 minutes")
    p.add_argument("text", nargs="+")
    p.add_argument("--speak", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="don't actually open apps or websites")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("setup-voice", help="download the offline speech models (~190 MB)")
    p.set_defaults(func=cmd_setup_voice)

    args = parser.parse_args(argv)
    return args.func(args, load_config())


if __name__ == "__main__":
    sys.exit(main())
