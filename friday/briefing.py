"""Gathers news, markets and tasks, then writes the spoken briefing."""
from __future__ import annotations

import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime

from .llm import LLMError, build_backends
from .markets import Quote, biggest_movers, fetch_quotes, format_quote
from .news import Headline, fetch_headlines, select_top
from .tasks import Task, TaskStore

SYSTEM_PROMPT = """You are Friday, a calm, sharp personal assistant who briefs one person each morning \
on global markets, the geopolitical and macro news that moves them, and their own to-do list.

Your briefing is read aloud by a text-to-speech voice, so:
- Write plain spoken English. No markdown, bullet points, headings, emojis, tables or URLs.
- Say numbers the way a person would ("down about three point seven percent", "Brent near eighty-nine dollars").
- Use short sentences and natural transitions.

Content rules:
- Use only the facts in the data you are given. Never invent prices, moves, events or quotes.
- Headlines can be one-sided or speculative; say "reports say" when a claim is unconfirmed.
- For each story you pick, say in one sentence why it matters for markets: which assets, sectors or regions it could move, and in which direction.
- Prefer stories with clear market impact (central banks, inflation, energy, trade, sanctions, conflict, elections, big policy moves) over general news.
- Group related stories into one theme instead of listing near-duplicates.
- This is information, not investment advice. Never tell the listener to buy or sell anything.
"""


@dataclass
class BriefingData:
    today: date
    headlines: list[Headline]
    quotes: list[Quote]
    tasks: list[Task]
    feed_errors: list[str] = field(default_factory=list)
    signal_events: list[dict] = field(default_factory=list)
    agenda: list[str] = field(default_factory=list)   # today's events and reminders, spoken form


@dataclass
class Briefing:
    text: str
    backend: str
    data: BriefingData
    notes: list[str] = field(default_factory=list)


def fetch_signal_events(cfg) -> tuple[list[dict], str | None]:
    """Top events from a running Signal service; empty if it is off or unreachable."""
    if not cfg.get("signal.enabled", False):
        return [], None
    url = cfg.get("signal.url", "http://127.0.0.1:8765").rstrip("/") + "/v1/briefing?limit=8"
    try:
        with urllib.request.urlopen(url, timeout=4) as resp:
            return json.loads(resp.read().decode("utf-8")).get("events", []), None
    except (OSError, ValueError) as exc:
        return [], f"Signal unavailable ({exc})"


def gather(cfg) -> BriefingData:
    store = TaskStore(cfg.data_dir / "friday.db")
    with ThreadPoolExecutor(max_workers=3) as pool:
        news_f = pool.submit(fetch_headlines, cfg.get("feeds", []))
        quotes_f = pool.submit(fetch_quotes, cfg.get("markets", []))
        signal_f = pool.submit(fetch_signal_events, cfg)
        headlines, errors = news_f.result()
        quotes = quotes_f.result()
        events, signal_err = signal_f.result()
    if signal_err:
        errors.append(signal_err)
    return BriefingData(date.today(), headlines, quotes, store.for_today(), errors, events, todays_agenda(cfg))


def todays_agenda(cfg) -> list[str]:
    from .schedule import ScheduleStore
    sched = ScheduleStore(cfg.data_dir / "friday.db")
    today = date.today()
    items = [f"Event: {e.spoken(today)}" for e in sched.events_between(today, today)]
    end_of_day = datetime.combine(today, datetime.max.time())
    items += [f"Reminder at {r.due_at:%H:%M}: {r.text}" for r in sched.pending_reminders(end_of_day)
              if r.event_id is None]
    return items


def _part_of_day(now: datetime) -> str:
    if now.hour < 12:
        return "morning"
    if now.hour < 17:
        return "afternoon"
    return "evening"


def build_prompt(cfg, data: BriefingData, now: datetime | None = None) -> str:
    now = now or datetime.now()
    n = int(cfg.get("briefing.max_headlines_for_llm", 30))
    words = int(cfg.get("briefing.target_words", 320))
    top = select_top(data.headlines, n)

    market_lines = "\n".join(
        f"- [{q.group}] {format_quote(q)}" + (f" (last close {q.as_of})" if q.as_of else "")
        for q in data.quotes
    )
    news_lines = "\n".join(
        f"{i}. [{h.source}{', also ' + ', '.join(h.also_in) if h.also_in else ''}"
        f"{f', {h.age_hours:.0f}h ago' if h.age_hours is not None else ''}] {h.title}"
        + (f" | {h.summary[:200]}" if h.summary and h.summary not in h.title else "")
        for i, h in enumerate(top, 1)
    )
    signal_block = ""
    if data.signal_events:
        signal_block = "\n\nSTRUCTURED MARKET EVENTS (from the Signal engine; clustered across outlets, with expected market reactions)\n" + "\n".join(
            f"{i}. [{e.get('type')}, severity {e.get('severity')}/5, {e.get('sources')} sources, "
            f"{', '.join(e.get('regions', [])[:2]) or 'global'}] {e.get('title')}"
            + (f" | {e['summary']}" if e.get("summary") else "")
            + (f" | expected: {'; '.join(e.get('impacts', []))}" if e.get("impacts") else "")
            for i, e in enumerate(data.signal_events, 1)
        ) + "\nPrefer these events for the news section; the expected reactions are model estimates, so say \"could\" or \"tends to\"."

    task_lines = "\n".join(
        f"- {t.text}" + (f" (due {t.due_label(data.today)})" if t.due else "")
        for t in data.tasks
    ) or "- (no open tasks)"
    agenda_lines = "\n".join(f"- {a}" for a in data.agenda) or "- (nothing scheduled)"

    return f"""Write my {_part_of_day(now)} briefing.

Listener's name: {cfg.get('user_name', 'there')}
Now: {now:%A %d %B %Y, %H:%M} local time

MARKET SNAPSHOT (daily change vs previous close; dates differ because markets close at different times)
{market_lines}

NEWS HEADLINES (ranked by a keyword score for likely market impact; higher first)
{news_lines}{signal_block}

MY TASKS FOR TODAY (overdue, due today, or undated)
{task_lines}

MY CALENDAR AND REMINDERS TODAY
{agenda_lines}

Structure, about {words} words in total:
1. A one-line greeting with the day.
2. Markets in two to four sentences: the overall mood, then the biggest moves worth knowing.
3. The three to five themes from the news most likely to move global markets today, each with why it matters.
4. My day: today's events and reminders with their times, then my tasks (how many, anything overdue,
   and a sensible order if it is obvious).
5. One short closing line.
"""


_PUBLISHER = re.compile(r"\s+[-|–]\s+[^-|–]{2,40}$")


def _spoken_headline(title: str) -> str:
    """Drops the ' - Publisher' tail and trailing punctuation so a headline reads naturally."""
    return _PUBLISHER.sub("", title).strip().rstrip(".")


def _spoken_move(q) -> str:
    direction = "up" if q.change_pct >= 0 else "down"
    return f"{q.name} {direction} {abs(q.change_pct):.1f} percent"


def template_briefing(cfg, data: BriefingData, now: datetime | None = None) -> str:
    """No-AI fallback: a plain read-out of the data."""
    now = now or datetime.now()
    name = cfg.get("user_name", "there")
    parts = [f"Good {_part_of_day(now)}, {name}. It's {now:%A} {now.day} {now:%B}."]

    movers = biggest_movers(data.quotes)
    if movers:
        parts.append("The biggest market moves: " + ", ".join(_spoken_move(q) for q in movers) + ".")
    top = select_top(data.headlines, 5, per_source=2)
    if data.signal_events:
        parts.append("Top market-moving events.")
        for e in data.signal_events[:5]:
            line = f"{_spoken_headline(e['title'])}."
            if e.get("impacts"):
                line += " Could mean " + ", ".join(i.split(" (")[0] for i in e["impacts"][:3]) + "."
            parts.append(line)
    elif top:
        parts.append("Here are the top market-moving headlines.")
        parts.extend(f"{_spoken_headline(h.title)}." for h in top)
    if data.agenda:
        parts.append("On your calendar today: " + "; ".join(a.split(": ", 1)[-1] for a in data.agenda) + ".")
    if data.tasks:
        parts.append(f"You have {len(data.tasks)} task{'s' if len(data.tasks) != 1 else ''} on your list.")
        parts.extend(
            f"{t.text}" + (f", {t.due_label(data.today)}" if t.due else "") + "."
            for t in data.tasks[:6]
        )
    else:
        parts.append("Your task list is clear.")
    parts.append("That's the briefing.")
    return " ".join(parts)


def write_briefing(cfg, data: BriefingData, log=lambda msg: None) -> Briefing:
    prompt = build_prompt(cfg, data)
    notes: list[str] = []
    for backend in build_backends(cfg):
        if not backend.available():
            notes.append(f"{backend.name}: not available")
            continue
        log(f"Writing briefing with {backend.name}...")
        try:
            return Briefing(backend.complete(SYSTEM_PROMPT, prompt), backend.name, data, notes)
        except LLMError as exc:
            notes.append(f"{backend.name}: {exc}")
            log(f"{backend.name} failed: {exc}")
    return Briefing(template_briefing(cfg, data), "template", data, notes)


def write_briefing_streaming(cfg, data: BriefingData, on_part, log=lambda msg: None) -> Briefing:
    """Like write_briefing, but calls on_part(text) for each paragraph as soon as Claude has
    written it, so Friday can start speaking after the first paragraph instead of the last."""
    prompt = build_prompt(cfg, data)
    notes: list[str] = []
    for backend in build_backends(cfg):
        if not backend.available():
            notes.append(f"{backend.name}: not available")
            continue
        log(f"Writing briefing with {backend.name}...")
        full, buffer, spoken_any = [], "", False
        try:
            for piece in backend.stream(SYSTEM_PROMPT, prompt):
                full.append(piece)
                buffer += piece
                # Hand over every finished paragraph; long paragraphs are split at a sentence end.
                while True:
                    cut = buffer.find("\n\n")
                    if cut == -1 and len(buffer) > 400:
                        m = list(re.finditer(r"[.!?]\s", buffer[:400]))
                        cut = m[-1].end() if m else -1
                    if cut == -1:
                        break
                    part, buffer = buffer[:cut].strip(), buffer[cut:].lstrip()
                    if part:
                        on_part(part)
                        spoken_any = True
            if buffer.strip():
                on_part(buffer.strip())
            return Briefing("".join(full).strip(), backend.name, data, notes)
        except LLMError as exc:
            notes.append(f"{backend.name}: {exc}")
            log(f"{backend.name} failed: {exc}")
            if spoken_any:  # part of it was already read out; don't start over with another backend
                return Briefing("".join(full).strip(), backend.name, data, notes)
    text = template_briefing(cfg, data)
    on_part(text)
    return Briefing(text, "template", data, notes)


# ---- once-per-day bookkeeping and archive -------------------------------------

def _state_path(cfg):
    return cfg.data_dir / "state.json"


def already_briefed_today(cfg) -> bool:
    try:
        state = json.loads(_state_path(cfg).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    return state.get("last_briefing_date") == date.today().isoformat()


def mark_briefed(cfg) -> None:
    _state_path(cfg).write_text(
        json.dumps({"last_briefing_date": date.today().isoformat()}), encoding="utf-8"
    )


def save_briefing(cfg, briefing: Briefing):
    folder = cfg.data_dir / "briefings"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{briefing.data.today.isoformat()}.md"
    sources = "\n".join(
        f"- [{h.title}]({h.link}) ({h.source})" for h in select_top(briefing.data.headlines, 15)
    )
    path.write_text(
        f"# Friday briefing, {briefing.data.today:%A %d %B %Y}\n\n"
        f"_Written by: {briefing.backend}_\n\n{briefing.text}\n\n## Top sources\n\n{sources}\n",
        encoding="utf-8",
    )
    return path
