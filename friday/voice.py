"""Text-to-speech. Edge neural voices (online) with the built-in Windows voice as fallback."""
from __future__ import annotations

import asyncio
import ctypes
import hashlib
import os
import queue
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path


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


CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "tts_cache"
_END = object()


def _cache_path(text: str, voice: str, rate: str) -> Path:
    key = hashlib.sha1(f"{voice}|{rate}|{text}".encode("utf-8")).hexdigest()[:20]
    return CACHE_DIR / f"{key}.mp3"


def _stream_edge(text: str, voice: str, rate: str, out: queue.Queue) -> None:
    """Producer: pushes MP3 chunks from Edge's speech service into a queue as they arrive."""
    import edge_tts

    async def run():
        async for chunk in edge_tts.Communicate(text, voice=voice, rate=rate).stream():
            if chunk["type"] == "audio":
                out.put(chunk["data"])
    try:
        asyncio.run(run())
    except Exception as exc:  # network trouble: let the player know
        out.put(exc)
    finally:
        out.put(_END)


def _play_mp3_chunks(chunks, stop_event: threading.Event | None = None) -> bytes:
    """Decodes MP3 data as it arrives and plays it straight away. Returns all bytes played."""
    import av
    import sounddevice as sd

    codec = av.CodecContext.create("mp3", "r")
    resampler = av.AudioResampler(format="flt", layout="mono", rate=24000)
    played = bytearray()
    with sd.OutputStream(samplerate=24000, channels=1, dtype="float32", latency="low") as out:
        def emit(frames):
            for frame in frames:
                for f in resampler.resample(frame):
                    if stop_event is not None and stop_event.is_set():
                        out.abort()
                        return False
                    out.write(f.to_ndarray().reshape(-1, 1))
            return True

        for data in chunks:
            if isinstance(data, Exception):
                raise data
            played += data
            for packet in codec.parse(data):
                if not emit(codec.decode(packet)):
                    return bytes(played)
        try:  # flush the last frames held in the parser and decoder
            for packet in codec.parse(None):
                emit(codec.decode(packet))
            emit(codec.decode(None))
        except Exception:
            pass
    return bytes(played)


def speak_edge(text: str, voice: str = "en-GB-SoniaNeural", rate: str = "+0%",
               stop_event: threading.Event | None = None) -> None:
    """Speaks with an Edge neural voice. Starts playing as soon as the first audio arrives,
    and caches short phrases so repeats ("Opening Spotify.") play instantly."""
    cached = _cache_path(text, voice, rate)
    if cached.exists():
        _play_mp3_chunks([cached.read_bytes()], stop_event)
        return

    q: queue.Queue = queue.Queue()
    threading.Thread(target=_stream_edge, args=(text, voice, rate, q), daemon=True).start()

    def chunks():
        while True:
            item = q.get(timeout=15)
            if item is _END:
                return
            yield item

    audio = _play_mp3_chunks(chunks(), stop_event)
    complete = stop_event is None or not stop_event.is_set()
    if complete and audio and len(text) <= 120:  # cache short, likely-repeated replies
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(audio)
        except OSError:
            pass


def prewarm(phrases: list[str], voice: str, rate: str) -> None:
    """Synthesises common replies in the background so their first use is instant too."""
    for text in phrases:
        path = _cache_path(text, voice, rate)
        if path.exists():
            continue
        q: queue.Queue = queue.Queue()
        _stream_edge(text, voice, rate, q)
        data = bytearray()
        while (item := q.get()) is not _END:
            if isinstance(item, Exception):
                return
            data += item
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bytes(data))
        except OSError:
            return


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
