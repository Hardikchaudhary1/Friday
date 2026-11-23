"""Text-to-speech. Edge neural voices (online) with the built-in Windows voice as fallback."""
from __future__ import annotations

import asyncio
import ctypes
import os
import subprocess
import tempfile
import threading
import uuid


def _play_mp3(path: str, stop_event: threading.Event | None = None) -> None:
    """Plays an MP3 with the Windows MCI API until it ends or stop_event is set. No extra packages needed."""
    winmm = ctypes.windll.winmm
    alias = f"friday{uuid.uuid4().hex[:8]}"
    err = winmm.mciSendStringW(f'open "{path}" type mpegvideo alias {alias}', None, 0, None)
    if err:
        raise RuntimeError(f"MCI could not open audio (error {err})")
    try:
        if stop_event is None:
            winmm.mciSendStringW(f"play {alias} wait", None, 0, None)
            return
        winmm.mciSendStringW(f"play {alias}", None, 0, None)
        status = ctypes.create_unicode_buffer(64)
        while not stop_event.wait(0.1):
            winmm.mciSendStringW(f"status {alias} mode", status, 64, None)
            if status.value != "playing":
                break
    finally:
        winmm.mciSendStringW(f"close {alias}", None, 0, None)


async def _edge_synthesise(text: str, voice: str, rate: str, out_path: str) -> None:
    import edge_tts

    await edge_tts.Communicate(text, voice=voice, rate=rate).save(out_path)


def speak_edge(text: str, voice: str = "en-GB-SoniaNeural", rate: str = "+0%",
               stop_event: threading.Event | None = None) -> None:
    fd, path = tempfile.mkstemp(prefix="friday-", suffix=".mp3")
    os.close(fd)
    try:
        asyncio.run(_edge_synthesise(text, voice, rate, path))
        if stop_event is None or not stop_event.is_set():
            _play_mp3(path, stop_event)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def speak_sapi(text: str) -> None:
    """The offline Windows voice, through PowerShell's System.Speech. Text goes in on stdin."""
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$t=[Console]::In.ReadToEnd();"
        "(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak($t)"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        input=text, text=True, encoding="utf-8", check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def speak(text: str, cfg, log=lambda msg: None, stop_event: threading.Event | None = None) -> str:
    """Speaks text and returns the engine that was used. Setting stop_event cuts it short."""
    if not cfg.get("voice.enabled", True):
        return "off"
    if cfg.get("voice.engine", "edge") == "edge":
        try:
            speak_edge(text, cfg.get("voice.edge_voice", "en-GB-SoniaNeural"), cfg.get("voice.rate", "+0%"),
                       stop_event)
            return "edge"
        except Exception as exc:  # offline, service change, audio device issue
            log(f"Edge voice failed ({exc}); using the Windows voice.")
    speak_sapi(text)
    return "sapi"
