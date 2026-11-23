"""Connects Friday to the Signal market-events service: starts it in the background,
and reads its events, risk view and dashboard."""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

from .config import PROJECT_ROOT


def base_url(cfg) -> str:
    return cfg.get("signal.url", "http://127.0.0.1:8765").rstrip("/")


def project_dir(cfg) -> Path:
    configured = cfg.get("signal.project_dir", "")
    return Path(configured) if configured else PROJECT_ROOT.parent / "signal"


def _get(cfg, path: str, timeout: float = 4) -> dict | list:
    with urllib.request.urlopen(base_url(cfg) + path, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def is_running(cfg) -> bool:
    try:
        return bool(_get(cfg, "/health", timeout=1.5).get("ok"))
    except (OSError, ValueError):
        return False


def start(cfg, log=lambda m: None) -> bool:
    """Starts Signal hidden in the background (it keeps running if Friday closes).
    Returns True once it answers, False if it couldn't be started."""
    if not cfg.get("signal.enabled", True):
        return False
    if is_running(cfg):
        return True
    folder = project_dir(cfg)
    pythonw = folder / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        log(f"Signal not found at {folder}")
        return False
    env = os.environ.copy()
    # Signal asks Claude to classify the top stories; keep that light on your Claude plan.
    env.setdefault("SIGNAL_LLM_MAX_STORIES", str(cfg.get("signal.llm_max_stories", 3)))
    env.setdefault("SIGNAL_CLAUDE_CLI_MODEL", cfg.get("signal.claude_model", "haiku"))
    interval = str(int(cfg.get("signal.refresh_minutes", 30)) * 60)
    port = base_url(cfg).rsplit(":", 1)[-1].split("/")[0]
    (folder / "data").mkdir(exist_ok=True)
    logfile = open(folder / "data" / "server.log", "a", encoding="utf-8")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) \
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen([str(pythonw), "-m", "signal_engine", "serve", "--port", port, "--interval", interval],
                     cwd=folder, env=env, stdout=logfile, stderr=logfile, stdin=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)
    for _ in range(40):            # wait up to ~20 s for it to answer
        time.sleep(0.5)
        if is_running(cfg):
            log("Signal started in the background.")
            return True
    log("Signal didn't answer after starting; see signal/data/server.log")
    return False


def top_events(cfg, limit: int = 5) -> list[dict]:
    return _get(cfg, f"/v1/briefing?limit={limit}").get("events", [])


def risk(cfg) -> dict:
    return _get(cfg, "/v1/risk?hours=24")


def status(cfg) -> dict:
    return _get(cfg, "/v1/status")
