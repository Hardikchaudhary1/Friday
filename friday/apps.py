"""Opens installed apps (from the Start menu) and websites by spoken name.

Only Start-menu apps and http(s) sites can be opened, so a misheard command can never
run an arbitrary program or shell command.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

# Spoken names -> Start-menu name or website. Extend in config.yaml under apps.aliases.
DEFAULT_ALIASES = {
    "chrome": "Google Chrome", "browser": "Brave", "vs code": "Visual Studio Code", "vscode": "Visual Studio Code",
    "code": "Visual Studio Code", "word": "Word", "excel": "Excel", "powerpoint": "PowerPoint",
    "file explorer": "File Explorer", "explorer": "File Explorer", "files": "File Explorer",
    "settings": "Settings", "terminal": "Terminal", "notepad": "Notepad", "calculator": "Calculator",
    "task manager": "Task Manager", "camera": "Camera", "clock": "Clock", "store": "Microsoft Store",
}
WEBSITES = {
    "youtube": "https://www.youtube.com", "gmail": "https://mail.google.com", "google": "https://www.google.com",
    "github": "https://github.com", "linkedin": "https://www.linkedin.com", "google calendar": "https://calendar.google.com",
    "google drive": "https://drive.google.com", "netflix": "https://www.netflix.com", "amazon": "https://www.amazon.com",
    "twitter": "https://x.com", "x": "https://x.com", "reddit": "https://www.reddit.com",
    "whatsapp web": "https://web.whatsapp.com", "chatgpt": "https://chatgpt.com", "claude": "https://claude.ai",
    "tradingview": "https://www.tradingview.com", "yahoo finance": "https://finance.yahoo.com",
    "bloomberg": "https://www.bloomberg.com", "reuters": "https://www.reuters.com", "leetcode": "https://leetcode.com",
}
_DOMAIN = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)*\.(com|org|net|io|ai|dev|in|co|edu|gov|app)(/\S*)?$", re.I)
_SPOKEN_DOT = re.compile(r"\s+dot\s+", re.I)


@dataclass
class App:
    name: str
    app_id: str

    @property
    def spoken_name(self) -> str:
        return re.sub(r"[™®©]", "", self.name).strip()


@dataclass
class LaunchResult:
    ok: bool
    message: str
    target: str = ""


def _norm(s: str) -> str:
    s = re.sub(r"[™®©]", "", s.lower())
    return " ".join(re.sub(r"[^a-z0-9+ ]", " ", s).split())


class AppIndex:
    def __init__(self, cache_path: Path, aliases: dict | None = None, max_age_s: int = 24 * 3600):
        self.cache_path = cache_path
        self.aliases = {**DEFAULT_ALIASES, **{k.lower(): v for k, v in (aliases or {}).items()}}
        self.max_age_s = max_age_s
        self._apps: list[App] | None = None

    def apps(self, refresh: bool = False) -> list[App]:
        if self._apps is not None and not refresh:
            return self._apps
        fresh = self.cache_path.exists() and time.time() - self.cache_path.stat().st_mtime < self.max_age_s
        if fresh and not refresh:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        else:
            data = self._scan()
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(data), encoding="utf-8")
        self._apps = [App(d["Name"], d["AppID"]) for d in data if d.get("Name") and d.get("AppID")]
        return self._apps

    @staticmethod
    def _scan() -> list[dict]:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip()
        data = json.loads(out) if out else []
        return data if isinstance(data, list) else [data]

    def find(self, spoken: str) -> tuple[App | None, float]:
        """Best Start-menu match for a spoken name, with a 0..1 confidence."""
        query = _norm(spoken)
        query = _norm(self.aliases.get(query, query))
        best, best_score = None, 0.0
        for app in self.apps():
            name = _norm(app.name)
            if not name:
                continue
            if name == query:
                score = 1.0
            elif name.startswith(query + " ") or query in name.split():
                score = 0.9 - 0.01 * (len(name) - len(query)) / 10
            elif query in name:
                score = 0.8
            else:
                score = SequenceMatcher(None, query, name).ratio() * 0.85
            if score > best_score:
                best, best_score = app, score
        return best, best_score


def resolve_website(spoken: str) -> str | None:
    s = _SPOKEN_DOT.sub(".", spoken.strip().lower()).replace(" ", "") if " dot " in f" {spoken.lower()} " else spoken.strip().lower()
    if s in WEBSITES:
        return WEBSITES[s]
    if _DOMAIN.match(s):
        return "https://" + s
    return None


def launch_app(app: App) -> None:
    if re.match(r"^[a-z][a-z0-9+.-]*://", app.app_id):  # steam://, ms-settings:, etc.
        os.startfile(app.app_id)
    else:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app.app_id}"],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def open_target(spoken: str, index: AppIndex, dry_run: bool = False) -> LaunchResult:
    """Opens an app or website by spoken name. Apps win over websites unless the name
    is clearly a website ('youtube', 'github dot com')."""
    spoken = re.sub(r"^(the|my)\s+", "", spoken.strip(" .?!"), flags=re.I)
    spoken = re.sub(r"\s+(app|application|website|site)$", "", spoken, flags=re.I)
    if not spoken:
        return LaunchResult(False, "Open what?")

    site = resolve_website(spoken)
    app, score = index.find(spoken)
    if site and (score < 0.95):
        if not dry_run:
            webbrowser.open(site)
        return LaunchResult(True, f"Opening {spoken}.", site)
    if app and score >= 0.6:
        if not dry_run:
            launch_app(app)
        return LaunchResult(True, f"Opening {app.spoken_name}.", app.name)
    if app and score >= 0.45:
        return LaunchResult(False, f"I couldn't find {spoken}. Did you mean {app.spoken_name}?", app.name)
    return LaunchResult(False, f"I couldn't find an app called {spoken}.")


def web_search(query: str, dry_run: bool = False) -> LaunchResult:
    url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)
    if not dry_run:
        webbrowser.open(url)
    return LaunchResult(True, f"Searching the web for {query}.", url)
