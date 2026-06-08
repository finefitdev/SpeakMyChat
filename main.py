from __future__ import annotations

import re
import json
import queue
import ssl
import threading
import time
import random
import io
import wave
import socket
import sys
import os
import platform
import subprocess
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
from pathlib import Path
from typing import TypedDict


class _State(TypedDict):
    volume: float
    max_chars: int
    max_speakers: int
    message_delay: float
    mods_only: bool
    channel_points_only: bool
    reward_id_filter: str


PIPER_AVAILABLE = False
try:
    from piper.voice import PiperVoice
    PIPER_AVAILABLE = True
except ImportError:
    PiperVoice = None  # type: ignore[assignment, misc]

PYTTSX3_AVAILABLE = False
try:
    import pyttsx3 as _pyttsx3
    PYTTSX3_AVAILABLE = True
except ImportError:
    _pyttsx3 = None  # type: ignore[assignment]

GTTS_AVAILABLE = False
try:
    from gtts import gTTS
    GTTS_AVAILABLE = True
except ImportError:
    gTTS = None  # type: ignore[assignment, misc]

if not PIPER_AVAILABLE and not PYTTSX3_AVAILABLE and not GTTS_AVAILABLE:
    sys.exit("need piper-tts, pyttsx3, or gtts\npip install piper-tts")

try:
    import pygame
except ImportError:
    sys.exit("pip install pygame-ce")

MIXER_RATE: int = 22050
pygame.mixer.pre_init(frequency=MIXER_RATE, size=-16, channels=1, buffer=512)
pygame.init()
pygame.mixer.set_num_channels(64)

_MEIPASS: str = getattr(sys, "_MEIPASS", "")

if getattr(sys, "frozen", False) and _MEIPASS:
    _espeak_path: str = str(Path(_MEIPASS) / "espeak-ng-data")
elif PIPER_AVAILABLE:
    import piper as _piper_pkg
    _piper_file: str = _piper_pkg.__file__ or ""
    _espeak_path = str(Path(_piper_file).parent / "espeak-ng-data")
else:
    _espeak_path = ""

if _espeak_path:
    os.environ["ESPEAK_DATA_PATH"] = _espeak_path


def resource_path(rel: str) -> str:
    base: str = _MEIPASS or str(Path(__file__).resolve().parent)
    return str(Path(base) / rel)


def _open_folder(path: str) -> None:
    system = platform.system()
    if system == "Windows":
        os.startfile(path)
    elif system == "Darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


_script_base: Path = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
SOUNDS_DIR = str(_script_base / "sounds")
VOICES_DIR = str(_script_base / "voices")
CONFIG_FILE = str(_script_base / "config.json")
Path(SOUNDS_DIR).mkdir(exist_ok=True)
Path(VOICES_DIR).mkdir(exist_ok=True)

USER_QUEUE_MAX = 3
_RECONNECT_BASE = 5.0
_RECONNECT_MAX = 300.0

state: _State = {
    "volume": 1.0,
    "max_chars": 200,
    "max_speakers": 6,
    "message_delay": 0.0,
    "mods_only": False,
    "channel_points_only": False,
    "reward_id_filter": "",
}

SOUND_TRIGGERS: dict[str, str] = {
    "diamond": "shine.mp3",
}

FORBIDDEN_WORDS: set[str] = set()
FORBIDDEN_STRINGS: set[str] = set()
TWITCH_CHANNEL = ""

ACCENTS: list[tuple[str, str]] = [
    ("en", "com"),
    ("en", "co.uk"),
    ("en", "com.au"),
    ("en", "co.in"),
    ("en", "ca"),
    ("en", "ie"),
    ("en", "co.za"),
    ("fr", "fr"),
    ("de", "de"),
    ("es", "es"),
]

IRC_HOST = "irc.chat.twitch.tv"
IRC_PORT = 6697
PING_INTERVAL = 60

_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "\U0001f926-\U0001f937"
    "\U00010000-\U0010ffff"
    "\u2640-\u2642"
    "\u2600-\u2B55"
    "\u200d\u23cf\u23e9\u231a\ufe0f\u3030"
    "]+",
    flags=re.UNICODE,
)

_VoiceData = str | tuple[str, str] | None

# log queue holds (category, text) tuples that the gui pulls off and renders
_log_queue: queue.Queue[tuple[str, str]] = queue.Queue()


def log(category: str, message: str) -> None:
    _log_queue.put((category, message))


def _scan_voices_dir() -> list[str]:
    p = Path(VOICES_DIR)
    if not p.is_dir():
        return []
    return sorted(f.name for f in p.iterdir() if f.suffix == ".onnx" and f.is_file())


def _resample(pcm_int16, from_rate: int, to_rate: int):
    import numpy as np
    if from_rate == to_rate:
        return pcm_int16
    ratio = to_rate / from_rate
    new_len = max(1, int(len(pcm_int16) * ratio))
    old_idx = np.linspace(0, len(pcm_int16) - 1, new_len)
    left = np.floor(old_idx).astype(np.int64)
    right = np.minimum(left + 1, len(pcm_int16) - 1)
    frac = (old_idx - left).astype(np.float32)
    out = (pcm_int16[left].astype(np.float32) * (1 - frac) +
           pcm_int16[right].astype(np.float32) * frac)
    return out.clip(-32768, 32767).astype(np.int16)


def _play_pcm_blocking(pcm_int16, sample_rate: int) -> None:
    import numpy as np
    audio = _resample(pcm_int16, sample_rate, MIXER_RATE)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(MIXER_RATE)
        wf.writeframes(audio.tobytes())
    buf.seek(0)
    sound = pygame.mixer.Sound(buf)
    sound.set_volume(state["volume"])
    ch = sound.play()
    if ch is None:
        return
    while ch.get_busy():
        time.sleep(0.05)


def load_config() -> None:
    global TWITCH_CHANNEL, FORBIDDEN_WORDS, FORBIDDEN_STRINGS, SOUND_TRIGGERS
    if not Path(CONFIG_FILE).is_file():
        return
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        TWITCH_CHANNEL = str(data.get("channel", TWITCH_CHANNEL))
        state["volume"] = float(data.get("volume", state["volume"]))
        state["max_chars"] = int(data.get("max_chars", state["max_chars"]))
        state["max_speakers"] = int(data.get("max_speakers", state["max_speakers"]))
        state["message_delay"] = float(data.get("message_delay", state["message_delay"]))
        state["mods_only"] = bool(data.get("mods_only", state["mods_only"]))
        state["channel_points_only"] = bool(data.get("channel_points_only", state["channel_points_only"]))
        state["reward_id_filter"] = str(data.get("reward_id_filter", state["reward_id_filter"]))
        FORBIDDEN_WORDS = set(data.get("forbidden_words", list(FORBIDDEN_WORDS)))
        FORBIDDEN_STRINGS = set(data.get("forbidden_strings", list(FORBIDDEN_STRINGS)))
        SOUND_TRIGGERS.clear()
        SOUND_TRIGGERS.update(data.get("sound_triggers", SOUND_TRIGGERS))
    except Exception:
        pass


def save_config(channel: str) -> None:
    data = {
        "channel": channel,
        "volume": state["volume"],
        "max_chars": state["max_chars"],
        "max_speakers": state["max_speakers"],
        "message_delay": state["message_delay"],
        "mods_only": state["mods_only"],
        "channel_points_only": state["channel_points_only"],
        "reward_id_filter": state["reward_id_filter"],
        "forbidden_words": sorted(FORBIDDEN_WORDS),
        "forbidden_strings": sorted(FORBIDDEN_STRINGS),
        "sound_triggers": SOUND_TRIGGERS,
    }
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


load_config()


def strip_emojis(text: str) -> str:
    return _EMOJI_RE.sub("", text).strip()


def contains_forbidden(text: str) -> bool:
    lower = text.lower()
    for s in FORBIDDEN_STRINGS:
        if s.lower() in lower:
            return True
    words = re.findall(r"[a-z0-9']+", lower)
    fw = {f.lower() for f in FORBIDDEN_WORDS}
    return any(w in fw for w in words)


def clean_message(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text)
    text = strip_emojis(text)
    return text.strip()[:state["max_chars"]]


def find_sound_for_message(text: str) -> str | None:
    lower = text.lower()
    for keyword, filename in SOUND_TRIGGERS.items():
        if keyword.lower() in lower:
            path = str(Path(SOUNDS_DIR) / filename)
            if Path(path).is_file():
                return path
            log("ERROR", f"Missing sound file: {filename}")
    return None


def play_sound_blocking(path: str) -> None:
    try:
        sound = pygame.mixer.Sound(path)
        sound.set_volume(state["volume"])
        ch = sound.play()
        if ch:
            while ch.get_busy():
                time.sleep(0.05)
    except Exception as e:
        log("ERROR", f"Sound error: {e}")


def _parse_irc_tags(line: str) -> tuple[dict[str, str], str]:
    if not line.startswith("@"):
        return {}, line
    tag_section, rest = line[1:].split(" ", 1)
    tags: dict[str, str] = {}
    for pair in tag_section.split(";"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            tags[k] = v
    return tags, rest


_usr_voice: dict[str, tuple[str, _VoiceData]] = {}
_voice_lock = threading.Lock()
_piper_model_cache: dict[str, object] = {}
_model_cache_lock = threading.Lock()

_pyttsx3_voices_list: list[str] = []
_pyttsx3_q: queue.Queue[tuple[str | None, str, threading.Event] | None] = queue.Queue()


def _pyttsx3_startup() -> None:
    global _pyttsx3_voices_list
    if _pyttsx3 is None:
        return
    try:
        engine = _pyttsx3.init()
        raw = engine.getProperty("voices")
        _pyttsx3_voices_list = [v.id for v in (raw or [])]
    except Exception:
        _pyttsx3_voices_list = []
        return
    while True:
        item = _pyttsx3_q.get()
        if item is None:
            break
        voice_id, text, done = item
        try:
            if voice_id:
                engine.setProperty("voice", voice_id)
            engine.say(text)
            engine.runAndWait()
        except Exception:
            pass
        finally:
            done.set()


if PYTTSX3_AVAILABLE:
    threading.Thread(target=_pyttsx3_startup, daemon=True, name="pyttsx3").start()


def _available_piper_voices() -> list[str]:
    return _scan_voices_dir()


def get_user_voice(username: str) -> tuple[str, _VoiceData]:
    with _voice_lock:
        if username not in _usr_voice:
            piper_voices = _available_piper_voices() if PIPER_AVAILABLE else []
            if piper_voices:
                _usr_voice[username] = ("piper", random.choice(piper_voices))
            elif PYTTSX3_AVAILABLE:
                vid: str | None = (
                    random.choice(_pyttsx3_voices_list) if _pyttsx3_voices_list else None
                )
                _usr_voice[username] = ("pyttsx3", vid)
            else:
                _usr_voice[username] = ("gtts", random.choice(ACCENTS))
        return _usr_voice[username]


def _load_piper_model(model_filename: str):
    with _model_cache_lock:
        if model_filename not in _piper_model_cache:
            path = str(Path(VOICES_DIR) / model_filename)
            if PiperVoice is not None:
                _piper_model_cache[model_filename] = PiperVoice.load(path)
        return _piper_model_cache[model_filename]


def _speak_piper(model_filename: str, text: str) -> None:
    import numpy as np
    voice = _load_piper_model(model_filename)
    chunks = []
    for sentence_phonemes in voice.phonemize(text):
        ids = voice.phonemes_to_ids(sentence_phonemes)
        chunk = voice.phoneme_ids_to_audio(ids)
        if chunk is not None and len(chunk) > 0:
            if chunk.dtype == np.float32:
                chunk = (chunk * 32767).clip(-32768, 32767).astype(np.int16)
            chunks.append(chunk)
    if not chunks:
        log("ERROR", "Piper returned empty audio (espeak-ng installed?)")
        return
    all_audio = np.concatenate(chunks)
    _play_pcm_blocking(all_audio, voice.config.sample_rate)


def _speak_pyttsx3(voice_id: str | None, text: str) -> None:
    done = threading.Event()
    _pyttsx3_q.put((voice_id, text, done))
    done.wait(timeout=60)


def _speak_gtts(lang: str, tld: str, text: str) -> None:
    if gTTS is None:
        return
    tts = gTTS(text=text, lang=lang, tld=tld, slow=False)
    buf = io.BytesIO()
    tts.write_to_fp(buf)
    buf.seek(0)
    sound = pygame.mixer.Sound(buf)
    sound.set_volume(state["volume"])
    ch = sound.play()
    if ch is None:
        return
    while ch.get_busy():
        time.sleep(0.05)


def _preview_voice(voice_type: str, voice_data: _VoiceData) -> None:
    try:
        if voice_type == "piper" and isinstance(voice_data, str):
            _speak_piper(voice_data, "Hello, this is a voice preview.")
        elif voice_type == "pyttsx3":
            vid = voice_data if isinstance(voice_data, str) else None
            _speak_pyttsx3(vid, "Hello, this is a voice preview.")
        elif isinstance(voice_data, tuple):
            _speak_gtts(voice_data[0], voice_data[1], "Hello, this is a voice preview.")
    except Exception:
        pass


TYPE_LABEL = {"piper": "Piper", "pyttsx3": "System", "gtts": "Google"}


def _pretty_voice(vtype: str, vdata: _VoiceData) -> str:
    if vtype == "piper" and isinstance(vdata, str):
        name = vdata[:-5] if vdata.endswith(".onnx") else vdata
        parts = name.split("-")
        if len(parts) >= 2 and "_" in parts[0]:
            locale = parts[0].replace("_", "-")
            speaker = parts[1].capitalize()
            quality = parts[2] if len(parts) >= 3 else ""
            tail = locale + (f", {quality}" if quality else "")
            return f"{speaker}  ({tail})"
        return name

    if vtype == "pyttsx3":
        if not isinstance(vdata, str):
            return "System default"
        token = vdata.replace("/", "\\").split("\\")[-1]
        # windows sapi looks like TTS_MS_EN-US_DAVID_11.0
        if token.upper().startswith("TTS_MS_"):
            segs = token[7:].split("_")
            locale = segs[0].lower() if segs else ""
            speaker = segs[1].capitalize() if len(segs) > 1 else token
            return f"{speaker}  ({locale})" if locale else speaker
        # mac is like com.apple.speech.synthesis.voice.Alex
        if "." in token:
            return token.split(".")[-1]
        return token

    if isinstance(vdata, tuple):
        return f"Google  ({vdata[0]}, .{vdata[1]})"
    return str(vdata)


_speaker_gate = threading.Condition(threading.Lock())
_active_speakers = 0


def _speaker_acquire() -> None:
    global _active_speakers
    with _speaker_gate:
        while _active_speakers >= state["max_speakers"]:
            _speaker_gate.wait()
        _active_speakers += 1


def _speaker_release() -> None:
    global _active_speakers
    with _speaker_gate:
        _active_speakers = max(0, _active_speakers - 1)
        _speaker_gate.notify_all()


def _rebuild_semaphore(new_max: int) -> None:
    state["max_speakers"] = new_max
    with _speaker_gate:
        _speaker_gate.notify_all()


_user_workers: dict[str, dict] = {}
_user_workers_lock = threading.Lock()
_WORKER_IDLE_TIMEOUT = 30.0


def _user_worker(username: str, q: queue.Queue[tuple[str, str | None]]) -> None:
    while True:
        try:
            item = q.get(timeout=_WORKER_IDLE_TIMEOUT)
        except queue.Empty:
            with _user_workers_lock:
                _user_workers.pop(username, None)
            return

        text, sound_path = item
        voice_type, voice_data = get_user_voice(username)

        _speaker_acquire()
        try:
            if sound_path:
                play_sound_blocking(sound_path)

            log("TTS", f"Speaking {username}")

            if voice_type == "piper" and isinstance(voice_data, str):
                _speak_piper(voice_data, text)
            elif voice_type == "pyttsx3":
                vid = voice_data if isinstance(voice_data, str) else None
                _speak_pyttsx3(vid, text)
            else:
                if isinstance(voice_data, tuple):
                    lang, tld = voice_data[0], voice_data[1]
                else:
                    lang, tld = "en", "com"
                _speak_gtts(lang, tld, text)

            if state["message_delay"] > 0:
                time.sleep(state["message_delay"])

        except Exception as e:
            log("ERROR", f"TTS [{username}]: {e}")
            if PYTTSX3_AVAILABLE and voice_type != "pyttsx3":
                try:
                    _speak_pyttsx3(None, text)
                except Exception:
                    pass
            elif GTTS_AVAILABLE and voice_type != "gtts":
                try:
                    _speak_gtts("en", "com", text)
                except Exception:
                    pass
        finally:
            _speaker_release()
            q.task_done()


def _enqueue_for_user(username: str, text: str, sound_path: str | None) -> None:
    with _user_workers_lock:
        entry = _user_workers.get(username)
        if entry is None or not entry["thread"].is_alive():
            q: queue.Queue[tuple[str, str | None]] = queue.Queue()
            t = threading.Thread(
                target=_user_worker, args=(username, q),
                daemon=True, name=f"tts-{username}"
            )
            entry = {"queue": q, "thread": t}
            _user_workers[username] = entry
            t.start()
        if entry["queue"].qsize() >= USER_QUEUE_MAX:
            log("SKIPPED", f"{username}: queue full")
            return
        entry["queue"].put((text, sound_path))


_irc_running = False


def connect_irc(channel: str) -> ssl.SSLSocket:
    context = ssl.create_default_context()
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock = context.wrap_socket(raw, server_hostname=IRC_HOST)
    sock.connect((IRC_HOST, IRC_PORT))
    sock.send(b"PASS SCHMOOPIIE\r\n")
    sock.send(b"NICK justinfan12345\r\n")
    sock.send(b"CAP REQ :twitch.tv/tags\r\n")
    sock.send(f"JOIN #{channel}\r\n".encode())
    return sock


def read_chat(channel: str) -> None:
    global _irc_running
    _irc_running = True
    reconnect_delay = _RECONNECT_BASE

    try:
        sock: ssl.SSLSocket | None = connect_irc(channel)
    except Exception as e:
        log("ERROR", f"Could not connect: {e}")
        _irc_running = False
        return

    buf = ""
    last_ping = time.time()
    log("CONNECTED", f"Joined #{channel}")

    if PIPER_AVAILABLE and not _available_piper_voices():
        fallback = "pyttsx3 (offline)" if PYTTSX3_AVAILABLE else "gtts"
        log("SYSTEM", f"No .onnx models in voices/ — falling back to {fallback}")
        log("SYSTEM", "Models: https://github.com/rhasspy/piper/releases")

    while _irc_running:
        if sock is None:
            waited = 0.0
            while waited < reconnect_delay and _irc_running:
                time.sleep(1.0)
                waited += 1.0
            if not _irc_running:
                break
            reconnect_delay = min(reconnect_delay * 2, _RECONNECT_MAX)
            log("RETRY", f"Reconnecting to #{channel}…")
            try:
                sock = connect_irc(channel)
                buf = ""
                last_ping = time.time()
                reconnect_delay = _RECONNECT_BASE
                log("CONNECTED", f"Reconnected to #{channel}")
            except Exception as e:
                log("ERROR", f"Reconnect failed: {e} — retry in {reconnect_delay:.0f}s")
            continue

        if time.time() - last_ping > PING_INTERVAL:
            try:
                sock.send(b"PING :tmi.twitch.tv\r\n")
            except Exception:
                pass
            last_ping = time.time()

        sock.settimeout(1.0)
        try:
            data = sock.recv(4096).decode("utf-8", errors="ignore")
        except socket.timeout:
            continue
        except Exception as e:
            log("ERROR", f"Connection lost: {e}")
            try:
                sock.close()
            except Exception:
                pass
            sock = None
            continue

        if not data:
            log("ERROR", "Server closed connection")
            try:
                sock.close()
            except Exception:
                pass
            sock = None
            continue

        buf += data
        while "\r\n" in buf:
            line, buf = buf.split("\r\n", 1)

            if line.startswith("PING"):
                try:
                    sock.send(b"PONG :tmi.twitch.tv\r\n")
                except Exception:
                    pass
                continue

            tags, stripped = _parse_irc_tags(line)

            match = re.match(r":(\w+)!\w+@\S+ PRIVMSG #\S+ :(.+)", stripped)
            if not match:
                continue

            username = match.group(1)

            if state["mods_only"]:
                badges = tags.get("badges", "")
                if (
                    tags.get("mod") != "1"
                    and "broadcaster" not in badges
                    and "moderator" not in badges
                ):
                    continue

            if state["channel_points_only"]:
                reward_id = tags.get("custom-reward-id", "")
                if not reward_id:
                    continue
                filter_id = state["reward_id_filter"].strip()
                if filter_id and reward_id != filter_id:
                    continue

            text = clean_message(match.group(2))
            if not text:
                continue

            if contains_forbidden(text):
                log("BLOCKED", f"{username}: banned word")
                continue

            log("CHAT", f"{username}: {text}")

            sound_path = find_sound_for_message(text)
            if sound_path:
                log("SOUND", f"{username}: {Path(sound_path).name}")

            _enqueue_for_user(username, text, sound_path)

    if sock is not None:
        try:
            sock.close()
        except Exception:
            pass
    log("OFFLINE", "Disconnected")


PURPLE = "#9147ff"
DARK_BG = "#0e0e10"
MID_BG = "#18181b"
CARD_BG = "#1f1f23"
TEXT_COL = "#efeff1"
MUTED = "#adadb8"
BORDER = "#2a2a2e"
GREEN = "#00c853"
RED_COL = "#e01a4f"
ORANGE = "#f0a500"
TTS_BLUE = "#7da7ff"
TRACK_BG = "#3a3a40"   # slider trough, a bit lighter so you can see it
ROW_EVEN = "#1a1a1d"
ROW_ODD = "#222227"

LOG_COLORS: dict[str, str] = {
    "CONNECTED": GREEN,
    "RETRY": ORANGE,
    "OFFLINE": MUTED,
    "CHAT": TEXT_COL,
    "TTS": TTS_BLUE,
    "SKIPPED": ORANGE,
    "BLOCKED": RED_COL,
    "ERROR": RED_COL,
    "SOUND": GREEN,
    "SYSTEM": PURPLE,
}


class Slider(tk.Canvas):
    """flat slider with a filled track + round handle. fires command(value) on change."""

    def __init__(self, parent, *, from_: float, to: float, value: float,
                 step: float = 1.0, command=None, width: int = 200,
                 height: int = 30, fill: str = PURPLE) -> None:
        super().__init__(parent, width=width, height=height, bg=CARD_BG,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.from_ = float(from_)
        self.to = float(to)
        self.step = float(step) if step else 1.0
        self.value = self._snap(float(value))
        self.command = command
        self._fill = fill
        self._cw = width
        self._ch = height
        self._pad = 11
        self._dragging = False
        self.bind("<Configure>", self._on_configure)
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._redraw()

    def _snap(self, raw: float) -> float:
        steps = round((raw - self.from_) / self.step)
        v = self.from_ + steps * self.step
        return max(self.from_, min(self.to, round(v, 6)))

    def _x_for_value(self, v: float) -> float:
        span = self.to - self.from_
        frac = (v - self.from_) / span if span else 0.0
        usable = self._cw - 2 * self._pad
        return self._pad + frac * usable

    def _value_for_x(self, x: float) -> float:
        usable = self._cw - 2 * self._pad
        frac = (x - self._pad) / usable if usable else 0.0
        frac = max(0.0, min(1.0, frac))
        return self._snap(self.from_ + frac * (self.to - self.from_))

    def _redraw(self) -> None:
        self.delete("all")
        cy, th, r = self._ch / 2, 6, 9
        self.create_rectangle(self._pad, cy - th / 2,
                              self._cw - self._pad, cy + th / 2,
                              fill=TRACK_BG, outline="")
        hx = self._x_for_value(self.value)
        self.create_rectangle(self._pad, cy - th / 2, hx, cy + th / 2,
                              fill=self._fill, outline="")
        self.create_oval(hx - r, cy - r, hx + r, cy + r,
                        fill=TEXT_COL, outline=self._fill, width=3)

    def _on_configure(self, event) -> None:
        self._cw = max(event.width, 2 * self._pad + 1)
        self._redraw()

    def _apply(self, x: float) -> None:
        v = self._value_for_x(x)
        changed = v != self.value
        self.value = v
        self._redraw()
        if changed and self.command:
            self.command(v)

    def _on_press(self, e) -> None:
        self._dragging = True
        self._apply(e.x)

    def _on_drag(self, e) -> None:
        if self._dragging:
            self._apply(e.x)

    def _on_release(self, e) -> None:
        self._dragging = False

    def set(self, v: float) -> None:
        self.value = self._snap(float(v))
        self._redraw()


class TwitchTTSApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Twitch TTS Reader")
        self.configure(bg=DARK_BG)
        self.minsize(560, 640)
        self._irc_thread: threading.Thread | None = None
        self._build_ui()
        self._poll_log()
        self._refresh_speaker_tree_periodic()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        hdr = tk.Frame(self, bg=PURPLE, padx=16, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="🎙  Twitch TTS Reader",
                 font=("Segoe UI", 14, "bold"),
                 bg=PURPLE, fg="white").pack(side="left")

        if PIPER_AVAILABLE:
            badge_text, badge_bg = "Piper TTS", GREEN
        elif PYTTSX3_AVAILABLE:
            badge_text, badge_bg = "pyttsx3 offline", ORANGE
        else:
            badge_text, badge_bg = "gTTS online", ORANGE
        tk.Label(hdr, text=badge_text, font=("Segoe UI", 8, "bold"),
                 bg=badge_bg, fg="white", padx=6, pady=2).pack(side="right")

        body = tk.Frame(self, bg=DARK_BG, padx=16, pady=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Dark.TNotebook", background=DARK_BG, borderwidth=0)
        style.configure("Dark.TNotebook.Tab", background=CARD_BG, foreground=MUTED,
                        font=("Segoe UI", 9, "bold"), padding=[12, 5])
        style.map("Dark.TNotebook.Tab",
                  background=[("selected", PURPLE)],
                  foreground=[("selected", "white")])

        nb = ttk.Notebook(body, style="Dark.TNotebook")
        nb.pack(fill="both", expand=True)

        for label, builder in [
            ("  Main  ", self._build_main_tab),
            ("  Voices  ", self._build_voices_tab),
            ("  Sound Triggers  ", self._build_sounds_tab),
            ("  Banned Words  ", self._build_banned_tab),
            ("  Speakers  ", self._build_speakers_tab),
        ]:
            tab = tk.Frame(nb, bg=DARK_BG, padx=12, pady=12)
            nb.add(tab, text=label)
            builder(tab)

        ft = tk.Frame(self, bg=DARK_BG, padx=16, pady=12)
        ft.pack(fill="x")
        tk.Label(ft, text="No OAuth needed     •     TLS-encrypted IRC connection",
                 font=("Segoe UI", 8), bg=DARK_BG, fg=MUTED).pack(pady=(0, 4))
        tk.Label(ft, text="Keep your   sounds/   and   voices/   folders next to the executable",
                 font=("Segoe UI", 8), bg=DARK_BG, fg=MUTED).pack()

    def _card(self, parent: tk.Widget, title: str) -> tuple[tk.Frame, tk.Frame]:
        outer = tk.Frame(parent, bg=CARD_BG,
                         highlightthickness=1, highlightbackground=BORDER)
        tk.Label(outer, text=title.upper(), font=("Segoe UI", 7, "bold"),
                 bg=CARD_BG, fg=MUTED).pack(anchor="w", padx=10, pady=(8, 2))
        inner = tk.Frame(outer, bg=CARD_BG, padx=10)
        inner.pack(fill="both", pady=(0, 10))
        return outer, inner

    def _build_main_tab(self, parent: tk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(4, weight=1)

        chan_outer, chan_inner = self._card(parent, "Channel")
        chan_outer.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        row = tk.Frame(chan_inner, bg=CARD_BG)
        row.pack(fill="x", pady=(4, 0))
        self.channel_var = tk.StringVar(value=TWITCH_CHANNEL)
        tk.Entry(row, textvariable=self.channel_var,
                 font=("Segoe UI", 11), bg=MID_BG, fg=TEXT_COL,
                 insertbackground=TEXT_COL, relief="flat",
                 highlightthickness=1, highlightbackground=BORDER,
                 highlightcolor=PURPLE, width=20).pack(side="left", ipady=6, padx=(0, 8))
        self.connect_btn = tk.Button(
            row, text="Connect",
            font=("Segoe UI", 11, "bold"),
            bg=PURPLE, fg="white", relief="flat",
            activebackground="#7b2fbe", activeforeground="white",
            cursor="hand2", padx=18, pady=7,
            command=self._toggle_connection)
        self.connect_btn.pack(side="left")
        self.stop_btn = tk.Button(
            row, text="⏹  STOP",
            font=("Segoe UI", 11, "bold"),
            bg=ORANGE, fg="white", relief="flat",
            activebackground="#c98000", activeforeground="white",
            cursor="hand2", padx=18, pady=7,
            command=self._skip_current)
        self.stop_btn.pack(side="left", padx=(8, 0))

        ctrl_row1 = tk.Frame(parent, bg=DARK_BG)
        ctrl_row1.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        ctrl_row1.columnconfigure(0, weight=1)
        ctrl_row1.columnconfigure(1, weight=1)

        vol_outer, vol_inner = self._card(ctrl_row1, "Volume")
        vol_outer.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.vol_label = tk.Label(vol_inner, text=f"{int(state['volume'] * 100)}%",
                                  font=("Segoe UI", 11, "bold"), bg=CARD_BG, fg=PURPLE)
        self.vol_label.pack(anchor="e")
        Slider(vol_inner, from_=0.0, to=1.0, value=state["volume"], step=0.01,
               command=self._on_volume).pack(fill="x", pady=(2, 4))

        char_outer, char_inner = self._card(ctrl_row1, "Max Characters")
        char_outer.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.char_label = tk.Label(char_inner, text=f"{state['max_chars']} chars",
                                   font=("Segoe UI", 11, "bold"), bg=CARD_BG, fg=PURPLE)
        self.char_label.pack(anchor="e")
        Slider(char_inner, from_=10, to=500, value=state["max_chars"], step=5,
               command=self._on_char_limit).pack(fill="x", pady=(2, 4))

        ctrl_row2 = tk.Frame(parent, bg=DARK_BG)
        ctrl_row2.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        ctrl_row2.columnconfigure(0, weight=1)
        ctrl_row2.columnconfigure(1, weight=1)

        spk_outer, spk_inner = self._card(ctrl_row2, "Max Speakers")
        spk_outer.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.spk_label = tk.Label(spk_inner, text=f"{state['max_speakers']}",
                                  font=("Segoe UI", 11, "bold"), bg=CARD_BG, fg=PURPLE)
        self.spk_label.pack(anchor="e")
        Slider(spk_inner, from_=1, to=20, value=state["max_speakers"], step=1,
               command=self._on_max_speakers).pack(fill="x", pady=(2, 2))
        tk.Label(spk_inner, text="voices at once  (1 = fully sequential)",
                 font=("Segoe UI", 7), bg=CARD_BG, fg=MUTED).pack(anchor="w")

        dly_outer, dly_inner = self._card(ctrl_row2, "Delay Between Messages")
        dly_outer.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.dly_label = tk.Label(dly_inner, text=f"{state['message_delay']:.1f}s",
                                  font=("Segoe UI", 11, "bold"), bg=CARD_BG, fg=PURPLE)
        self.dly_label.pack(anchor="e")
        Slider(dly_inner, from_=0.0, to=10.0, value=state["message_delay"], step=0.5,
               command=self._on_message_delay).pack(fill="x", pady=(2, 2))
        tk.Label(dly_inner, text="pause after each clip per user",
                 font=("Segoe UI", 7), bg=CARD_BG, fg=MUTED).pack(anchor="w")

        flt_outer, flt_inner = self._card(parent, "Filters")
        flt_outer.grid(row=3, column=0, sticky="ew", pady=(0, 10))

        chk_row = tk.Frame(flt_inner, bg=CARD_BG)
        chk_row.pack(fill="x")
        self.mods_only_var = tk.BooleanVar(value=state["mods_only"])
        tk.Checkbutton(chk_row, text="Moderators only",
                       variable=self.mods_only_var, command=self._on_mods_only,
                       bg=CARD_BG, fg=TEXT_COL, selectcolor=MID_BG,
                       activebackground=CARD_BG, activeforeground=TEXT_COL,
                       font=("Segoe UI", 9), relief="flat",
                       highlightthickness=0).pack(side="left", padx=(0, 20))
        self.chan_pts_var = tk.BooleanVar(value=state["channel_points_only"])
        tk.Checkbutton(chk_row, text="Channel point redemptions only",
                       variable=self.chan_pts_var, command=self._on_channel_points_only,
                       bg=CARD_BG, fg=TEXT_COL, selectcolor=MID_BG,
                       activebackground=CARD_BG, activeforeground=TEXT_COL,
                       font=("Segoe UI", 9), relief="flat",
                       highlightthickness=0).pack(side="left")
        tk.Label(flt_inner, text="mod status sourced from Twitch IRC tags (TLS-verified connection)",
                 font=("Segoe UI", 7), bg=CARD_BG, fg=MUTED).pack(anchor="w", pady=(3, 0))

        # reward id row only shows up when channel points mode is on
        self.rid_row = tk.Frame(flt_inner, bg=CARD_BG)
        tk.Label(self.rid_row, text="Reward ID (blank = any):",
                 font=("Segoe UI", 8), bg=CARD_BG, fg=MUTED).pack(side="left")
        self.reward_id_var = tk.StringVar(value=state["reward_id_filter"])
        self.reward_id_var.trace_add("write", self._on_reward_id_change)
        tk.Entry(self.rid_row, textvariable=self.reward_id_var, width=30,
                 font=("Segoe UI", 9), bg=MID_BG, fg=TEXT_COL,
                 insertbackground=TEXT_COL, relief="flat",
                 highlightthickness=1, highlightbackground=BORDER,
                 highlightcolor=PURPLE).pack(side="left", ipady=3, padx=(6, 0))
        if state["channel_points_only"]:
            self.rid_row.pack(fill="x", pady=(6, 0))

        log_outer, log_inner = self._card(parent, "Chat Log")
        log_outer.grid(row=4, column=0, sticky="nsew")
        log_inner.pack_configure(expand=True, fill="both")

        self.log = scrolledtext.ScrolledText(
            log_inner, height=12, width=58,
            font=("Consolas", 9), bg=MID_BG, fg=TEXT_COL,
            insertbackground=TEXT_COL, relief="flat",
            state="disabled", wrap="word", highlightthickness=0,
            spacing1=1, spacing3=1)
        self.log.pack(fill="both", expand=True)
        for cat, col in LOG_COLORS.items():
            self.log.tag_config(f"lbl_{cat.lower()}", foreground=col,
                                font=("Consolas", 9, "bold"))
        self.log.tag_config("msg", foreground=TEXT_COL)
        self.log.tag_config("msg_alert", foreground=RED_COL)

    def _build_voices_tab(self, parent: tk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(3, weight=1)

        if PIPER_AVAILABLE:
            status_text, status_col = "✅  piper-tts installed", GREEN
        elif PYTTSX3_AVAILABLE:
            status_text, status_col = "⚠  piper not found — using pyttsx3 (offline)", ORANGE
        else:
            status_text, status_col = "⚠  piper not found — using gtts (needs internet)", ORANGE
        tk.Label(parent, text=status_text, font=("Segoe UI", 9, "bold"),
                 bg=DARK_BG, fg=status_col, anchor="w").grid(
                 row=0, column=0, sticky="ew", pady=(0, 6))

        tk.Label(parent,
            text=("put .onnx + .onnx.json pairs in the voices/ folder\n"
                  "each chatter gets a random voice at session start\n"
                  "models: https://github.com/rhasspy/piper/releases\n"
                  "good ones: en_US-lessac-medium, en_US-amy-medium, en_GB-alan-medium"),
            font=("Segoe UI", 9), bg=DARK_BG, fg=MUTED, justify="left").grid(
            row=1, column=0, sticky="w", pady=(0, 10))

        folder_row = tk.Frame(parent, bg=DARK_BG)
        folder_row.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        folder_row.columnconfigure(0, weight=1)
        tk.Label(folder_row, text=f"📁  {VOICES_DIR}",
                 font=("Segoe UI", 8), bg=DARK_BG, fg=MUTED, anchor="w").grid(
                 row=0, column=0, sticky="ew")
        tk.Button(folder_row, text="Open Folder", font=("Segoe UI", 9),
                  bg=CARD_BG, fg=TEXT_COL, relief="flat", cursor="hand2",
                  padx=12, pady=4,
                  command=lambda: _open_folder(VOICES_DIR)).grid(row=0, column=1)

        tree_frame = tk.Frame(parent, bg=CARD_BG,
                              highlightthickness=1, highlightbackground=BORDER)
        tree_frame.grid(row=3, column=0, sticky="nsew", pady=(0, 8))
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        style = ttk.Style()
        style.configure("Voices.Treeview",
                         background=MID_BG, foreground=TEXT_COL,
                         fieldbackground=MID_BG, rowheight=28,
                         font=("Segoe UI", 10))
        style.configure("Voices.Treeview.Heading",
                         background=CARD_BG, foreground=MUTED,
                         font=("Segoe UI", 8, "bold"), relief="flat")
        style.map("Voices.Treeview", background=[("selected", PURPLE)])

        self.voice_tree = ttk.Treeview(tree_frame, columns=("Model File", "Config"),
                                        show="headings", style="Voices.Treeview", height=8)
        self.voice_tree.heading("Model File", text="MODEL FILE")
        self.voice_tree.heading("Config", text="CONFIG")
        self.voice_tree.column("Model File", width=300, anchor="w")
        self.voice_tree.column("Config", width=100, anchor="center")

        sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.voice_tree.yview)
        self.voice_tree.configure(yscrollcommand=sb.set)
        self.voice_tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        tk.Button(parent, text="↻  Refresh", font=("Segoe UI", 9),
                  bg=CARD_BG, fg=TEXT_COL, relief="flat", cursor="hand2",
                  padx=14, pady=5,
                  command=self._refresh_voice_tree).grid(row=4, column=0, sticky="w", pady=(4, 0))

        self._refresh_voice_tree()

    def _refresh_voice_tree(self) -> None:
        self.voice_tree.delete(*self.voice_tree.get_children())
        found = _scan_voices_dir()
        for name in found:
            has_cfg = Path(VOICES_DIR, name + ".json").is_file()
            self.voice_tree.insert("", "end",
                                   values=(name, "✔ found" if has_cfg else "✘ missing"),
                                   tags=("ok" if has_cfg else "missing",))
        if not found:
            self.voice_tree.insert("", "end", values=("no .onnx files in voices/", "—"),
                                   tags=("missing",))
        self.voice_tree.tag_configure("ok", foreground=GREEN)
        self.voice_tree.tag_configure("missing", foreground=RED_COL)

    def _build_sounds_tab(self, parent: tk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        tk.Label(parent,
            text=("keyword → sound file in sounds/\n"
                  "sound plays before TTS when keyword shows up in chat\n"
                  "case-insensitive substring match"),
            font=("Segoe UI", 9), bg=DARK_BG, fg=MUTED, justify="left").grid(
            row=0, column=0, sticky="w", pady=(0, 10))

        folder_row = tk.Frame(parent, bg=DARK_BG)
        folder_row.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        folder_row.columnconfigure(0, weight=1)
        tk.Label(folder_row, text=f"📁  {SOUNDS_DIR}",
                 font=("Segoe UI", 8), bg=DARK_BG, fg=MUTED, anchor="w").grid(
                 row=0, column=0, sticky="ew")
        tk.Button(folder_row, text="Open Folder", font=("Segoe UI", 9),
                  bg=CARD_BG, fg=TEXT_COL, relief="flat", cursor="hand2",
                  padx=12, pady=4,
                  command=lambda: _open_folder(SOUNDS_DIR)).grid(row=0, column=1)

        tree_frame = tk.Frame(parent, bg=CARD_BG,
                              highlightthickness=1, highlightbackground=BORDER)
        tree_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 8))
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        style = ttk.Style()
        style.configure("Sounds.Treeview",
                         background=MID_BG, foreground=TEXT_COL,
                         fieldbackground=MID_BG, rowheight=28,
                         font=("Segoe UI", 10))
        style.configure("Sounds.Treeview.Heading",
                         background=CARD_BG, foreground=MUTED,
                         font=("Segoe UI", 8, "bold"), relief="flat")
        style.map("Sounds.Treeview", background=[("selected", PURPLE)])

        cols = ("Keyword", "Sound File", "Status")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                                  style="Sounds.Treeview", height=8)
        self.tree.heading("Keyword", text="KEYWORD")
        self.tree.heading("Sound File", text="SOUND FILE")
        self.tree.heading("Status", text="STATUS")
        self.tree.column("Keyword", width=150, anchor="w")
        self.tree.column("Sound File", width=200, anchor="w")
        self.tree.column("Status", width=100, anchor="center")

        sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        add_frame = tk.Frame(parent, bg=DARK_BG)
        add_frame.grid(row=3, column=0, sticky="ew", pady=(0, 6))

        tk.Label(add_frame, text="Keyword:", font=("Segoe UI", 9),
                 bg=DARK_BG, fg=MUTED).pack(side="left")
        self.new_keyword_var = tk.StringVar()
        tk.Entry(add_frame, textvariable=self.new_keyword_var, width=14,
                 font=("Segoe UI", 10), bg=MID_BG, fg=TEXT_COL,
                 insertbackground=TEXT_COL, relief="flat",
                 highlightthickness=1, highlightbackground=BORDER,
                 highlightcolor=PURPLE).pack(side="left", ipady=4, padx=(4, 12))

        tk.Label(add_frame, text="File:", font=("Segoe UI", 9),
                 bg=DARK_BG, fg=MUTED).pack(side="left")
        self.new_file_var = tk.StringVar()
        tk.Entry(add_frame, textvariable=self.new_file_var, width=16,
                 font=("Segoe UI", 10), bg=MID_BG, fg=TEXT_COL,
                 insertbackground=TEXT_COL, relief="flat",
                 highlightthickness=1, highlightbackground=BORDER,
                 highlightcolor=PURPLE).pack(side="left", ipady=4, padx=(4, 4))

        tk.Button(add_frame, text="Browse…", font=("Segoe UI", 9),
                  bg=CARD_BG, fg=TEXT_COL, relief="flat", cursor="hand2",
                  padx=12, pady=5,
                  command=self._browse_sound).pack(side="left", padx=(0, 8))
        tk.Button(add_frame, text="＋  Add", font=("Segoe UI", 9, "bold"),
                  bg=GREEN, fg="white", relief="flat", cursor="hand2",
                  padx=14, pady=5,
                  command=self._add_trigger).pack(side="left", padx=(0, 6))
        tk.Button(add_frame, text="✕  Remove", font=("Segoe UI", 9),
                  bg=RED_COL, fg="white", relief="flat", cursor="hand2",
                  padx=14, pady=5,
                  command=self._remove_trigger).pack(side="left")

        self._refresh_tree()

    def _build_banned_tab(self, parent: tk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(1, weight=1)

        tk.Label(parent,
            text=("words: whole-word match (case-insensitive)\n"
                  "strings: substring match anywhere in the message"),
            font=("Segoe UI", 9), bg=DARK_BG, fg=MUTED, justify="left").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        for col, title, attr, var_attr, add_cmd, rem_cmd in [
            (0, "BANNED WORDS", "words_list", "new_word_var",
             self._add_banned_word, self._remove_banned_word),
            (1, "BANNED STRINGS", "strings_list", "new_string_var",
             self._add_banned_string, self._remove_banned_string),
        ]:
            outer = tk.Frame(parent, bg=CARD_BG,
                             highlightthickness=1, highlightbackground=BORDER)
            outer.grid(row=1, column=col, sticky="nsew",
                       padx=(0, 6) if col == 0 else (6, 0))
            outer.columnconfigure(0, weight=1)
            outer.rowconfigure(1, weight=1)

            tk.Label(outer, text=title, font=("Segoe UI", 7, "bold"),
                     bg=CARD_BG, fg=MUTED).grid(row=0, column=0, sticky="w",
                                                 padx=10, pady=(8, 2))
            lb = tk.Listbox(outer, bg=MID_BG, fg=TEXT_COL,
                            selectbackground=PURPLE, selectforeground="white",
                            font=("Consolas", 10), relief="flat",
                            highlightthickness=0, activestyle="none",
                            selectmode="extended")
            lb.grid(row=1, column=0, sticky="nsew", padx=10)
            setattr(self, attr, lb)

            add_row = tk.Frame(outer, bg=CARD_BG)
            add_row.grid(row=2, column=0, sticky="ew", padx=10, pady=(6, 8))
            var = tk.StringVar()
            setattr(self, var_attr, var)
            tk.Entry(add_row, textvariable=var,
                     font=("Segoe UI", 10), bg=MID_BG, fg=TEXT_COL,
                     insertbackground=TEXT_COL, relief="flat",
                     highlightthickness=1, highlightbackground=BORDER,
                     highlightcolor=PURPLE).pack(side="left", fill="x",
                                                 expand=True, ipady=4, padx=(0, 6))
            tk.Button(add_row, text="＋", font=("Segoe UI", 11, "bold"),
                      bg=GREEN, fg="white", relief="flat", cursor="hand2",
                      padx=9, pady=2, command=add_cmd).pack(side="left", padx=(0, 4))
            tk.Button(add_row, text="✕", font=("Segoe UI", 11),
                      bg=RED_COL, fg="white", relief="flat", cursor="hand2",
                      padx=9, pady=2, command=rem_cmd).pack(side="left")

        self._refresh_banned_lists()

    def _build_speakers_tab(self, parent: tk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        tk.Label(parent,
            text="chatters assigned voices this session  •  voices reset on disconnect",
            font=("Segoe UI", 9), bg=DARK_BG, fg=MUTED, justify="left").grid(
            row=0, column=0, sticky="w", pady=(0, 8))

        tree_frame = tk.Frame(parent, bg=CARD_BG,
                              highlightthickness=1, highlightbackground=BORDER)
        tree_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 8))
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        style = ttk.Style()
        style.configure("Speakers.Treeview",
                         background=MID_BG, foreground=TEXT_COL,
                         fieldbackground=MID_BG, rowheight=30,
                         font=("Segoe UI", 10))
        style.configure("Speakers.Treeview.Heading",
                         background=CARD_BG, foreground=MUTED,
                         font=("Segoe UI", 8, "bold"), relief="flat")
        style.map("Speakers.Treeview", background=[("selected", PURPLE)])

        self.speaker_tree = ttk.Treeview(tree_frame,
                                          columns=("User", "Engine", "Voice"),
                                          show="headings",
                                          style="Speakers.Treeview", height=10)
        self.speaker_tree.heading("User", text="USER")
        self.speaker_tree.heading("Engine", text="ENGINE")
        self.speaker_tree.heading("Voice", text="VOICE")
        self.speaker_tree.column("User", width=150, anchor="w")
        self.speaker_tree.column("Engine", width=80, anchor="center")
        self.speaker_tree.column("Voice", width=280, anchor="w")
        self.speaker_tree.tag_configure("even", background=ROW_EVEN)
        self.speaker_tree.tag_configure("odd", background=ROW_ODD)
        self.speaker_tree.tag_configure("empty", foreground=MUTED)

        sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.speaker_tree.yview)
        self.speaker_tree.configure(yscrollcommand=sb.set)
        self.speaker_tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        btn_row = tk.Frame(parent, bg=DARK_BG)
        btn_row.grid(row=2, column=0, sticky="ew")

        for label, cmd, bg, fg in [
            ("▶  Preview", self._preview_selected_voice, CARD_BG, TEXT_COL),
            ("↺  Reassign", self._reassign_selected_voice, CARD_BG, TEXT_COL),
            ("✕  Clear All", self._clear_all_speakers, RED_COL, "white"),
        ]:
            tk.Button(btn_row, text=label, font=("Segoe UI", 10, "bold"),
                      bg=bg, fg=fg, relief="flat", cursor="hand2",
                      padx=16, pady=7,
                      command=cmd).pack(side="left", padx=(0, 8))

        self._refresh_speaker_tree()

    def _refresh_speaker_tree(self) -> None:
        if not hasattr(self, "speaker_tree"):
            return
        self.speaker_tree.delete(*self.speaker_tree.get_children())
        with _voice_lock:
            snapshot = dict(_usr_voice)
        if not snapshot:
            self.speaker_tree.insert(
                "", "end", values=("— no active speakers yet —", "", ""),
                tags=("empty",))
            return
        for i, (username, (vtype, vdata)) in enumerate(sorted(snapshot.items())):
            engine = TYPE_LABEL.get(vtype, vtype)
            voice = _pretty_voice(vtype, vdata)
            stripe = "even" if i % 2 == 0 else "odd"
            self.speaker_tree.insert("", "end",
                                     values=(username, engine, voice),
                                     tags=(stripe,))

    def _refresh_speaker_tree_periodic(self) -> None:
        self._refresh_speaker_tree()
        self.after(3000, self._refresh_speaker_tree_periodic)

    def _preview_selected_voice(self) -> None:
        for iid in self.speaker_tree.selection():
            username = self.speaker_tree.item(iid, "values")[0]
            with _voice_lock:
                entry = _usr_voice.get(username)
            if entry is not None:
                vtype, vdata = entry
                threading.Thread(
                    target=_preview_voice, args=(vtype, vdata),
                    daemon=True).start()
            break

    def _reassign_selected_voice(self) -> None:
        for iid in self.speaker_tree.selection():
            username = self.speaker_tree.item(iid, "values")[0]
            with _voice_lock:
                _usr_voice.pop(username, None)
            get_user_voice(username)
            self._refresh_speaker_tree()
            break

    def _clear_all_speakers(self) -> None:
        with _voice_lock:
            _usr_voice.clear()
        self._refresh_speaker_tree()

    def _on_volume(self, val) -> None:
        v = float(val)
        state["volume"] = v
        self.vol_label.config(text=f"{int(v * 100)}%")

    def _on_char_limit(self, val) -> None:
        c = int(float(val))
        state["max_chars"] = c
        self.char_label.config(text=f"{c} chars")

    def _on_max_speakers(self, val) -> None:
        n = int(float(val))
        state["max_speakers"] = n
        self.spk_label.config(text=str(n))
        _rebuild_semaphore(n)

    def _on_message_delay(self, val) -> None:
        d = round(float(val) * 2) / 2
        state["message_delay"] = d
        self.dly_label.config(text=f"{d:.1f}s")

    def _on_mods_only(self) -> None:
        state["mods_only"] = self.mods_only_var.get()

    def _on_channel_points_only(self) -> None:
        on = self.chan_pts_var.get()
        state["channel_points_only"] = on
        if on:
            self.rid_row.pack(fill="x", pady=(6, 0))
        else:
            self.rid_row.pack_forget()

    def _on_reward_id_change(self, *_: object) -> None:
        state["reward_id_filter"] = self.reward_id_var.get().strip()

    def _skip_current(self) -> None:
        pygame.mixer.stop()

    def _toggle_connection(self) -> None:
        global _irc_running
        if self._irc_thread and self._irc_thread.is_alive():
            _irc_running = False
            self.connect_btn.config(text="Connect", bg=PURPLE)
        else:
            channel = self.channel_var.get().strip().lstrip("#")
            if not channel:
                self._log_line("ERROR", "Enter a channel name first")
                return
            _irc_running = False
            time.sleep(0.2)
            self._irc_thread = threading.Thread(
                target=read_chat, args=(channel,), daemon=True)
            self._irc_thread.start()
            self.connect_btn.config(text="Disconnect", bg=RED_COL)

    def _browse_sound(self) -> None:
        path = filedialog.askopenfilename(
            initialdir=SOUNDS_DIR,
            title="Select sound file",
            filetypes=[("Audio files", "*.wav *.mp3 *.ogg"), ("All files", "*.*")])
        if path:
            self.new_file_var.set(Path(path).name)

    def _add_trigger(self) -> None:
        kw = self.new_keyword_var.get().strip().lower()
        fn = self.new_file_var.get().strip()
        if not kw or not fn:
            messagebox.showwarning("Missing input", "need both a keyword and a filename")
            return
        SOUND_TRIGGERS[kw] = fn
        self.new_keyword_var.set("")
        self.new_file_var.set("")
        self._refresh_tree()

    def _remove_trigger(self) -> None:
        for iid in self.tree.selection():
            SOUND_TRIGGERS.pop(self.tree.item(iid, "values")[0], None)
        self._refresh_tree()

    def _refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for kw, fn in sorted(SOUND_TRIGGERS.items()):
            found = Path(SOUNDS_DIR, fn).is_file()
            self.tree.insert("", "end",
                             values=(kw, fn, "✔ found" if found else "✘ missing"),
                             tags=("ok" if found else "missing",))
        self.tree.tag_configure("ok", foreground=GREEN)
        self.tree.tag_configure("missing", foreground=RED_COL)

    def _add_banned_word(self) -> None:
        w = self.new_word_var.get().strip().lower()
        if w:
            FORBIDDEN_WORDS.add(w)
            self.new_word_var.set("")
            self._refresh_banned_lists()

    def _remove_banned_word(self) -> None:
        for i in reversed(self.words_list.curselection()):
            FORBIDDEN_WORDS.discard(self.words_list.get(i))
        self._refresh_banned_lists()

    def _add_banned_string(self) -> None:
        s = self.new_string_var.get().strip().lower()
        if s:
            FORBIDDEN_STRINGS.add(s)
            self.new_string_var.set("")
            self._refresh_banned_lists()

    def _remove_banned_string(self) -> None:
        for i in reversed(self.strings_list.curselection()):
            FORBIDDEN_STRINGS.discard(self.strings_list.get(i))
        self._refresh_banned_lists()

    def _refresh_banned_lists(self) -> None:
        self.words_list.delete(0, "end")
        for w in sorted(FORBIDDEN_WORDS):
            self.words_list.insert("end", w)
        self.strings_list.delete(0, "end")
        for s in sorted(FORBIDDEN_STRINGS):
            self.strings_list.insert("end", s)

    def _log_line(self, category: str, message: str) -> None:
        cat = category if category in LOG_COLORS else "SYSTEM"
        lbl_tag = f"lbl_{cat.lower()}"
        msg_tag = "msg_alert" if cat in ("BLOCKED", "ERROR") else "msg"
        self.log.config(state="normal")
        self.log.insert("end", f"{cat:<11}", (lbl_tag,))
        self.log.insert("end", message + "\n", (msg_tag,))
        self.log.see("end")
        self.log.config(state="disabled")

    def _poll_log(self) -> None:
        while True:
            try:
                category, message = _log_queue.get_nowait()
            except queue.Empty:
                break
            self._log_line(category, message)
        self.after(150, self._poll_log)

    def _on_close(self) -> None:
        save_config(self.channel_var.get().strip().lstrip("#"))
        self.destroy()


if __name__ == "__main__":
    app = TwitchTTSApp()
    app.mainloop()