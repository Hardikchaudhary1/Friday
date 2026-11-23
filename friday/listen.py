"""Microphone input: wake word with Vosk, command transcription with Whisper, and a
Ctrl+Alt+F hotkey as an alternative to the wake word. Everything runs offline."""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import queue
import re
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
BLOCK = 1600                      # 0.1 s of audio per block: quicker wake-word and end-of-speech detection
VOSK_MODEL_NAME = "vosk-model-small-en-us-0.15"
VOSK_MODEL_URL = f"https://alphacephei.com/vosk/models/{VOSK_MODEL_NAME}.zip"
WHISPER_HINT = "Friday, open, remind me, tomorrow, calendar, event, task, briefing, search for."
# Vosk is limited to a small vocabulary so it stays cheap; without other words to choose
# from it would label any speech "friday". These decoys soak up everything else.
DECOY_WORDS = ["monday", "today", "fried", "free", "right", "five", "the", "what", "is", "it", "a", "and",
               "i", "you", "to", "of", "in", "that", "this", "for", "on", "with", "my", "can", "weather",
               "day", "find", "fine", "why", "time", "red", "ready", "friend", "fight", "eye"]
# "Friday" within the first few words; the 2 s buffer may catch the end of an earlier sentence.
_WAKE_IN_TEXT = re.compile(r"^\W*(\w+\W+){0,3}?fri\s?day\b", re.I)


def build_hotwords(names: list[str], max_chars: int = 600) -> str:
    """A comma-separated list of names for Whisper to favour, shortest (most commonly said) first,
    capped so it fits in Whisper's prompt."""
    unique: dict[str, str] = {}
    for n in names:
        name = re.sub(r"[™®©]", "", n or "").strip()
        if name and len(name.split()) <= 3:
            unique.setdefault(name.lower(), name)      # keep the first spelling given
    picked, total = [], 0
    for name in sorted(unique.values(), key=lambda n: (len(n.split()), len(n))):
        if total + len(name) + 2 > max_chars:
            break
        picked.append(name)
        total += len(name) + 2
    return ", ".join(picked)


def wake_word_confirmed(transcript: str) -> bool:
    """Whisper's transcript must contain the wake word near its start."""
    return bool(_WAKE_IN_TEXT.search(transcript))


def models_dir(cfg) -> Path:
    return cfg.data_dir / "models"


def vosk_model_path(cfg) -> Path:
    return models_dir(cfg) / VOSK_MODEL_NAME


def voice_input_ready(cfg) -> tuple[bool, str]:
    if not vosk_model_path(cfg).exists():
        return False, "wake-word model missing (run: friday setup-voice)"
    if not (models_dir(cfg) / "whisper").exists():
        return False, "speech model missing (run: friday setup-voice)"
    return True, "ready"


def download_models(cfg, log=print) -> None:
    """Downloads the Vosk wake-word model (~40 MB) and the Whisper model (~145 MB for base.en)."""
    import io
    import urllib.request
    import zipfile

    target = models_dir(cfg)
    target.mkdir(parents=True, exist_ok=True)
    if not vosk_model_path(cfg).exists():
        log(f"Downloading {VOSK_MODEL_URL} ...")
        with urllib.request.urlopen(VOSK_MODEL_URL, timeout=120) as resp:
            zipfile.ZipFile(io.BytesIO(resp.read())).extractall(target)
    from faster_whisper import WhisperModel
    name = cfg.get("listen.whisper_model", "base.en")
    log(f"Downloading Whisper model {name} ...")
    WhisperModel(name, device="cpu", compute_type="int8", download_root=str(target / "whisper"))
    log("Voice models ready.")


class Hotkey(threading.Thread):
    """Registers a global Ctrl+Alt+F hotkey and calls on_press when it is pressed."""
    MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, WM_HOTKEY = 0x1, 0x2, 0x4000, 0x0312

    def __init__(self, on_press, vk: int = 0x46):  # 0x46 = F
        super().__init__(daemon=True, name="friday-hotkey")
        self.on_press, self.vk = on_press, vk
        self.registered = False

    def run(self):
        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, 1, self.MOD_CONTROL | self.MOD_ALT | self.MOD_NOREPEAT, self.vk):
            return  # another program owns the shortcut
        self.registered = True
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == self.WM_HOTKEY:
                self.on_press()


class Listener(threading.Thread):
    """Waits for "Friday" (or the hotkey), records what follows, and hands the text to on_command."""

    def __init__(self, cfg, on_command, is_speaking, on_wake=None, log=print, vocabulary=None, on_heard=None):
        super().__init__(daemon=True, name="friday-listener")
        self.cfg, self.on_command, self.is_speaking, self.on_wake, self.log = cfg, on_command, is_speaking, on_wake, log
        self.on_heard = on_heard   # called once recording ends (e.g. to bring the music volume back)
        # Names Whisper should expect (installed apps, websites), so "open Spotify" isn't heard as "open 45".
        self.hotwords = build_hotwords(vocabulary() if vocabulary else [])
        self.audio: queue.Queue[bytes] = queue.Queue()
        self.trigger = threading.Event()
        self.stop = threading.Event()
        self.ring: deque[bytes] = deque(maxlen=20)  # last 2 s, so "Friday, open Spotify" in one breath works
        self.noise_floor = 300.0
        self.whisper = None

    # ---- audio -------------------------------------------------------------

    def _callback(self, indata, frames, t, status):
        self.audio.put(bytes(indata))

    @staticmethod
    def _rms(block: bytes) -> float:
        a = np.frombuffer(block, dtype=np.int16).astype(np.float32)
        return float(np.sqrt(np.mean(a * a))) if a.size else 0.0

    def _drain(self):
        while not self.audio.empty():
            try:
                self.audio.get_nowait()
            except queue.Empty:
                break

    # ---- main loop -----------------------------------------------------------

    def run(self):
        import sounddevice as sd
        from vosk import KaldiRecognizer, Model, SetLogLevel

        SetLogLevel(-1)
        wake_words = self.cfg.get("listen.wake_words", ["friday", "hey friday"])
        model = Model(str(vosk_model_path(self.cfg)))
        rec = KaldiRecognizer(model, SAMPLE_RATE, json.dumps(wake_words + DECOY_WORDS + ["[unk]"]))
        self._load_whisper()
        self.log("Listening for \"Friday\" (or press Ctrl+Alt+F).")

        with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=BLOCK, dtype="int16", channels=1,
                               callback=self._callback):
            while not self.stop.is_set():
                if self.trigger.is_set():
                    self.trigger.clear()
                    while self.is_speaking():          # a follow-up waits until Friday finishes talking
                        time.sleep(0.1)
                    self._drain()
                    self._take_command(include_ring=False)
                    rec.Reset()
                    continue
                try:
                    block = self.audio.get(timeout=0.2)
                except queue.Empty:
                    continue
                if self.is_speaking():
                    self.ring.clear()
                    rec.Reset()
                    continue
                self.ring.append(block)
                level = self._rms(block)
                self.noise_floor = 0.98 * self.noise_floor + 0.02 * min(level, 2000)
                heard = ""
                if rec.AcceptWaveform(block):
                    heard = json.loads(rec.Result()).get("text", "")
                else:
                    heard = json.loads(rec.PartialResult()).get("partial", "")
                if re.search(r"\bfriday\b", heard):
                    rec.Reset()
                    self._take_command(include_ring=True)
                    rec.Reset()

    def _load_whisper(self):
        from faster_whisper import WhisperModel
        name = self.cfg.get("listen.whisper_model", "base.en")
        self.whisper = WhisperModel(name, device="cpu", compute_type="int8",
                                    download_root=str(models_dir(self.cfg) / "whisper"))
        # The first transcription is slow while the model warms up; do it now, not on your first command.
        list(self.whisper.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), language="en", beam_size=1)[0])

    def _take_command(self, include_ring: bool):
        if self.on_wake:
            self.on_wake()
        try:
            audio = self._record(list(self.ring) if include_ring else [])
        finally:
            if self.on_heard:
                self.on_heard()
        self.ring.clear()
        if audio is None:
            self.log("(heard nothing)")
            return
        text = self._transcribe(audio)
        if include_ring and not wake_word_confirmed(text):
            # Woken by the voice trigger but Whisper didn't hear "Friday": a false alarm.
            self.log(f"(ignored: {text!r})")
            return
        if text:
            self.on_command(text)

    def _record(self, prefix: list[bytes], max_s: float = 9.0, wait_s: float = 5.0, silence_s: float | None = None):
        """Records until the speaker pauses. Returns float32 audio, or None if nobody spoke."""
        # How long a pause ends the command. Shorter is snappier; too short cuts you off mid-sentence.
        silence_s = silence_s if silence_s is not None else float(self.cfg.get("listen.end_silence_s", 0.6))
        threshold = max(450.0, self.noise_floor * 2.5)
        blocks = list(prefix)
        started = any(self._rms(b) > threshold for b in prefix[-5:])
        quiet = 0.0
        t0 = time.time()
        while time.time() - t0 < max_s:
            try:
                block = self.audio.get(timeout=0.5)
            except queue.Empty:
                continue
            blocks.append(block)
            loud = self._rms(block) > threshold
            if loud:
                started, quiet = True, 0.0
            elif started:
                quiet += BLOCK / SAMPLE_RATE
                if quiet >= silence_s:
                    break
            elif time.time() - t0 > wait_s:
                return None
        if not started:
            return None
        pcm = np.frombuffer(b"".join(blocks), dtype=np.int16).astype(np.float32) / 32768.0
        return pcm

    def _transcribe(self, audio: np.ndarray) -> str:
        # Greedy decoding (beam 1): about 20% faster, with the app-name hints keeping accuracy.
        segments, _ = self.whisper.transcribe(audio, language="en", beam_size=1, initial_prompt=WHISPER_HINT,
                                              hotwords=self.hotwords or None,
                                              condition_on_previous_text=False, vad_filter=False)
        return " ".join(s.text.strip() for s in segments).strip()
