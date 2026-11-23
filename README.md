# Friday

A personal voice assistant for Windows. When you log in, Friday pulls global market data and
geopolitical and macro news, works out what is most likely to move markets, combines it with your
to-do list, and **reads you a spoken briefing**.

```
login ─► wait for network ─► ┌ 9 RSS feeds ──► dedupe ─► rank by market impact ┐
                             ├ 17 global markets (equities, rates, FX, oil, gold, BTC) ├─► Claude ─► voice
                             └ SQLite to-do list (overdue / today) ─────────────────┘      │
                                                                                 saved as Markdown
```

## Features (v0.1)

- **Global market snapshot**: S&P 500, Nasdaq, Euro Stoxx, FTSE, Nikkei, Hang Seng, Shanghai, Nifty,
  VIX, US 10Y, dollar index, EUR/USD, USD/JPY, Brent, gold, copper, Bitcoin (Yahoo Finance).
- **News ranked by market impact**: fetches 9 feeds in parallel, merges the same story across outlets
  (coverage by several outlets raises its score), scores headlines by geopolitical and macro keywords
  and freshness, and limits how many come from any one source.
- **AI-written briefing**: Claude turns the data into a short spoken script that says *why* each story
  matters for markets. It is told to use only the supplied facts and to give no investment advice.
- **Three AI backends with automatic fallback**: Claude Code CLI (billed to a Claude subscription),
  Anthropic API, or a plain no-AI read-out, so a briefing always plays.
- **Natural voice**: Microsoft Edge neural voices, with the offline Windows voice as fallback. Plays
  through the Windows MCI API, so no audio packages are needed.
- **To-do list**: `friday task add "Pay rent" --due fri`; overdue and due-today tasks are part of the briefing.
- **Runs at login, once per day**: a Startup-folder shortcut. No admin rights needed.

## Setup

```powershell
cd friday
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
copy config.example.yaml config.yaml      # then set user_name, voice, markets, feeds
```

Choose how the briefing gets written (at least one is recommended; without one you get the plain read-out):

- **Claude subscription (Pro/Max)**: sign in to the Claude Code CLI once with `claude` then `/login`.
  If `claude` is not on your PATH, set `llm.claude_cli_path` in `config.yaml`.
- **Anthropic API**: set the `ANTHROPIC_API_KEY` environment variable (billed per use, separate from the subscription).

```powershell
.\.venv\Scripts\python -m friday doctor          # checks which backends work
.\.venv\Scripts\python -m friday brief           # full spoken briefing
.\.venv\Scripts\python -m friday brief --no-speak
.\.venv\Scripts\python -m friday task add "Call bank" --due tomorrow
.\.venv\Scripts\python -m friday task list
.\.venv\Scripts\python -m friday task done 1
powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1   # brief me at every login
```

Briefings are archived in `data/briefings/YYYY-MM-DD.md` with links to their sources.

## Tests

```powershell
.\.venv\Scripts\python -m pytest
```

## Roadmap

- [ ] v0.2: wake word ("Hey Friday") + local speech-to-text (faster-whisper) for follow-up questions
- [ ] v0.2: Google Calendar / Outlook events in the briefing
- [ ] v0.3: tray app with a floating briefing card
- [ ] v0.3: use the **Signal** service (companion project) for event extraction and impact scoring instead of keywords
- [ ] Memory of your preferences and the assets you hold

## Project layout

| File | Purpose |
|---|---|
| `friday/news.py` | RSS fetching, de-duplication, market-impact scoring |
| `friday/markets.py` | Global quotes via yfinance |
| `friday/tasks.py` | SQLite to-do list and due-date parsing |
| `friday/llm.py` | Claude CLI / Anthropic API backends |
| `friday/briefing.py` | Prompt, fallback template, archive, once-per-day state |
| `friday/voice.py` | Edge neural TTS, Windows MCI playback, SAPI fallback |
| `friday/__main__.py` | Command line |
