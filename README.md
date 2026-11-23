# Friday

A voice-controlled personal assistant for Windows. It starts when you log in, reads you a briefing on
global markets, geopolitics and your day, and then keeps running: say **"Friday, ..."** to open apps,
set reminders, add calendar events, manage tasks or ask questions. Reminders pop up as Windows
notifications and are spoken aloud when they are due.

```
 "Friday, remind me to call mom tomorrow at 5"            login
        │                                                   │
  mic ─► Vosk wake word ─► Whisper speech-to-text           ├─► briefing: Signal events + 17 markets
        (offline, low CPU)   (offline)                      │             + calendar + tasks ─► Claude ─► voice
        │                                                   │
        ▼                                                   ▼
  command parser ──► apps / websites / search        reminder loop (every 15 s)
   (instant, offline)  reminders / events / tasks  ──►  Windows notification + spoken alert
        │
        └─► anything else ─► Claude (short spoken answer)
```

## What you can say

| Say | Friday does |
|---|---|
| "Friday, open Spotify" / "open VS Code" / "open YouTube" / "open github dot com" | Opens Start-menu apps (fuzzy matched) or websites |
| "Search for Brent crude price" | Web search in your browser |
| "Pause" / "Resume" / "Next song" / "Skip" / "Previous track" / "Stop the music" | Media keys: works with Spotify, YouTube in the browser, most players |
| "Set volume to 40" / "Turn it up" / "Quieter" / "Mute" / "What's the volume?" | Exact system volume |
| "Play lofi beats on YouTube" / "Play interstellar soundtrack" | Opens and plays the top YouTube result |
| "Play Blinding Lights on Spotify" | Opens Spotify's search for it |
| "Search mechanical keyboards on Amazon" / "Look up fastapi on GitHub" | Site search (Amazon, GitHub, Wikipedia, Reddit, LinkedIn, Maps, ...) |
| "Close Spotify" / "Switch to Brave" | Closes (like clicking X) or brings forward a running app |
| "Remind me to call mom tomorrow at 5 PM" / "...in 20 minutes" / "...on Friday" | Reminder; asks "when?" if you leave the time out |
| "Add dentist appointment on 5th December at 4 PM" / "Put mom's birthday on 12 January in my calendar" | Calendar event, reminded that morning and 30 minutes before |
| "What's on my schedule tomorrow?" / "What do I have this week?" | Reads your events |
| "What are my reminders?" / "Cancel the reminder about the bank" | Lists or cancels reminders |
| "Add task pay rent due Friday" / "What are my tasks?" / "Mark pay rent as done" | To-do list |
| "Brief me" | Full spoken markets and geopolitics briefing |
| "Open the dashboard" | Opens the Signal market dashboard (Friday starts Signal for you) |
| "What's moving the markets?" / "Market risk" | Signal's top events and which assets are under pressure |
| "What's the weather?" / "Weather in London tomorrow" | Live weather; Friday remembers your city |
| "Move the dentist to Friday at 3 PM" / "Delete the readme project" | Edit or remove events, reminders and tasks by name |
| "What time is it?" / "Stop" / anything else | Answers; general questions go to Claude |

Wake it by saying **"Friday"** or pressing **Ctrl+Alt+F**. You can also type commands in Friday's window.

## Features

- **Offline voice pipeline**: Vosk listens only for the wake word (cheap enough to run all day); Whisper
  (faster-whisper, int8 on CPU) transcribes the command. The last 2 s of audio are kept, so
  "Friday, open Spotify" works in one breath. Friday stops listening while it speaks so it doesn't hear itself.
- **Built for speed**: replies stream as they are generated, common phrases are pre-generated and cached
  (about 0.1 s to start speaking), media commands answer silently, music is turned down while you speak,
  and spoken questions use a fast model with an instant "One moment" while it thinks.
- **Natural dates**: "tomorrow at 5", "next Wednesday at 3 PM", "in 2 hours", "on the 28th at 11:30"
  (dateparser plus fixes for bare hours and false matches like "email to May").
- **Reminders that survive restarts**: stored in SQLite; anything missed while the laptop was off is
  announced at the next start as "Reminder from ...".
- **Safe app launching**: only apps in your Start menu and http(s) websites can be opened, never arbitrary commands.
- **Market briefing**: 9 RSS feeds ranked by market impact, 17 global markets, events from the companion
  **Signal** engine, and today's calendar, written by Claude and read aloud (Edge neural voice).
- **Claude without an API key**: uses the Claude Code CLI (billed to a Claude subscription), the Anthropic API,
  or a no-AI fallback, so it always works.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev,voice]"
copy config.example.yaml config.yaml                 # set user_name, voice, aliases, markets
.\.venv\Scripts\python -m friday setup-voice         # downloads offline speech models (~190 MB)
.\.venv\Scripts\python -m friday doctor              # checks Claude, voice, microphone
.\.venv\Scripts\python -m friday run                 # start the assistant
powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1   # start at every login
```

To let Friday answer general questions and write the AI briefing, sign in to the Claude Code CLI once
(`claude` then `/login`) or set `ANTHROPIC_API_KEY`.

Other commands: `friday ask "remind me to stretch in 10 minutes"`, `friday brief`, `friday task list`, `friday say "hello"`.

## Tests

```powershell
.\.venv\Scripts\python -m pytest
```
The tests cover date parsing, reminders, events, tasks, app matching and the briefing, using a temporary
database and a fake clock. No microphone or network is needed.

## Roadmap

- [ ] Google Calendar / Outlook sync (events are local for now)
- [ ] Tray icon and a floating card for the briefing
- [ ] Close apps, control volume and media by voice
- [ ] Remember preferences and the assets you follow

## Project layout

| File | Purpose |
|---|---|
| `friday/runtime.py` | Always-on loop: speaker, listener, reminder checker, typed commands |
| `friday/listen.py` | Microphone, Vosk wake word, Whisper transcription, Ctrl+Alt+F hotkey |
| `friday/assistant.py` | Command parser and handlers; Claude fallback |
| `friday/timeparse.py` | Natural-language dates and times |
| `friday/schedule.py` | Reminders and calendar events (SQLite) |
| `friday/apps.py` | Start-menu app index, fuzzy matching, websites |
| `friday/notify.py` | Windows toast notifications |
| `friday/news.py`, `markets.py`, `briefing.py` | Briefing data and writing |
| `friday/llm.py`, `voice.py`, `tasks.py` | Claude backends, speech output, to-do list |
