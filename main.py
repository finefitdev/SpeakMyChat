#===<[✦  SimpleTTS .✦ ݁˖]>===
# reads twitch chat and speaks it with TTS
# Open to improvements, in compactness/efficiency in logic✦
# email improvements in ui or backend to hello@finefit.dev
# -Finefit  2026 ݁˖
#===========================

import re
import json
import queue
import threading
import time
import random
import io
import wave
import socket
import sys
import os
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox

# ──────────────────────────────────────────────────────────────────────────────
# PyInstaller-safe path helper
# ──────────────────────────────────────────────────────────────────────────────
def resource_path(rel: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


#====[˖ Verify externals ✦ ]====
PIPER_AVAILABLE = False
try:
    from piper.voice import PiperVoice
    PIPER_AVAILABLE = True
except ImportError:
    pass

try:
    from gtts import gTTS
    GTTS_AVAILABLE = True
except ImportError:
    GTTS_AVAILABLE = False

if not PIPER_AVAILABLE and not GTTS_AVAILABLE:
    sys.exit(
        "No TTS engine found.\n"
        "  pip install piper-tts   (recommended, offline neural voices)\n"
        "  pip install gtts        (fallback, needs internet)"
    )

try:
    import pygame
    # FIX: Always init at a single fixed rate — never re-init while playing.
    # When a Piper voice has a different sample rate we resample in software
    # instead of touching the mixer, which was the root cause of crashes.
    MIXER_RATE = 22050
    pygame.mixer.pre_init(frequency=MIXER_RATE, size=-16, channels=1, buffer=512)
    pygame.init()
    pygame.mixer.set_num_channels(64)
except ImportError:
    sys.exit("Missing dependency: pip install pygame-ce")

if getattr(sys, "frozen", False):
    data_path = os.path.join(sys._MEIPASS, "espeak-ng-data")
else:
    import piper as _piper_pkg
    data_path = os.path.join(os.path.dirname(_piper_pkg.__file__), "espeak-ng-data")

os.environ["ESPEAK_DATA_PATH"] = data_path

#====[˖ Setup (Pathways) ✦]====
SCRIPT_DIR  = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
SOUNDS_DIR  = os.path.join(SCRIPT_DIR, "sounds")
VOICES_DIR  = os.path.join(SCRIPT_DIR, "voices")
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
os.makedirs(SOUNDS_DIR, exist_ok=True)
os.makedirs(VOICES_DIR, exist_ok=True)

# ── Default state ─────────────────────────────────────────────────────────────

state = {
    "volume":        1.0,
    "max_chars":     200,
    "max_speakers":  6,
    "message_delay": 0.0,   # seconds of silence inserted after each TTS clip
}

SOUND_TRIGGERS: dict = {
    "diamond": "shine.mp3",
}

FORBIDDEN_WORDS: set = {
    "slur1",
    "slur2",
    "badword",
}

FORBIDDEN_STRINGS: set = {
    "amongus",
    "badstring",
}

TWITCH_CHANNEL = ""

PIPER_VOICES: list = [
    "en_ryan.onnx",
    "en_amy.onnx",
]

# gTTS fallback accents
ACCENTS: list = [
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

IRC_HOST      = "irc.chat.twitch.tv"
IRC_PORT      = 6667
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

# ── Single-rate mixer helpers ─────────────────────────────────────────────────
# The mixer is initialised once at MIXER_RATE and never re-inited.
# Audio that was synthesised at a different sample rate is resampled here
# using numpy — no pygame.mixer.quit() calls, no race conditions.

def _resample(pcm_int16, from_rate: int, to_rate: int):
    """Linear resample of int16 PCM array to a new sample rate."""
    import numpy as np
    if from_rate == to_rate:
        return pcm_int16
    ratio      = to_rate / from_rate
    new_len    = max(1, int(len(pcm_int16) * ratio))
    old_idx    = np.linspace(0, len(pcm_int16) - 1, new_len)
    left       = np.floor(old_idx).astype(np.int64)
    right      = np.minimum(left + 1, len(pcm_int16) - 1)
    frac       = (old_idx - left).astype(np.float32)
    resampled  = (pcm_int16[left].astype(np.float32) * (1 - frac) +
                  pcm_int16[right].astype(np.float32) * frac)
    return resampled.clip(-32768, 32767).astype(np.int16)


def _play_pcm_blocking(pcm_int16, sample_rate: int) -> None:
    """Play raw int16 PCM, resampling to MIXER_RATE if needed, blocking until done."""
    import numpy as np
    audio = _resample(pcm_int16, sample_rate, MIXER_RATE)
    buf   = io.BytesIO()
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


#====[ Config persistence ]====

def load_config():
    global TWITCH_CHANNEL, FORBIDDEN_WORDS, FORBIDDEN_STRINGS, SOUND_TRIGGERS
    if not os.path.isfile(CONFIG_FILE):
        return
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        TWITCH_CHANNEL          = data.get("channel", TWITCH_CHANNEL)
        state["volume"]         = data.get("volume", state["volume"])
        state["max_chars"]      = data.get("max_chars", state["max_chars"])
        state["max_speakers"]   = data.get("max_speakers", state["max_speakers"])
        state["message_delay"]  = data.get("message_delay", state["message_delay"])
        FORBIDDEN_WORDS         = set(data.get("forbidden_words",   list(FORBIDDEN_WORDS)))
        FORBIDDEN_STRINGS       = set(data.get("forbidden_strings", list(FORBIDDEN_STRINGS)))
        SOUND_TRIGGERS.clear()
        SOUND_TRIGGERS.update(data.get("sound_triggers", SOUND_TRIGGERS))
    except Exception:
        pass


def save_config(channel: str):
    data = {
        "channel":           channel,
        "volume":            state["volume"],
        "max_chars":         state["max_chars"],
        "max_speakers":      state["max_speakers"],
        "message_delay":     state["message_delay"],
        "forbidden_words":   sorted(FORBIDDEN_WORDS),
        "forbidden_strings": sorted(FORBIDDEN_STRINGS),
        "sound_triggers":    SOUND_TRIGGERS,
    }
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


load_config()


#====[ Filtering ]====

def strip_emojis(text: str) -> str:
    return _EMOJI_RE.sub("", text).strip()


def contains_forbidden(text: str) -> bool:
    lower = text.lower()
    for s in FORBIDDEN_STRINGS:
        if s.lower() in lower:
            return True
    words = re.findall(r"[a-z0-9']+", lower)
    forbidden_lower = {f.lower() for f in FORBIDDEN_WORDS}
    return any(w in forbidden_lower for w in words)


def clean_message(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text)
    text = strip_emojis(text)
    return text.strip()[: state["max_chars"]]


#====[ Sound Playing ]====

def find_sound_for_message(text: str):
    lower = text.lower()
    for keyword, filename in SOUND_TRIGGERS.items():
        if keyword.lower() in lower:
            path = os.path.join(SOUNDS_DIR, filename)
            if os.path.isfile(path):
                return path
            else:
                _log_queue.put(f"⚠  Sound file not found: {filename}")
    return None


def play_sound_blocking(path: str) -> None:
    try:
        sound = pygame.mixer.Sound(path)
        sound.set_volume(state["volume"])
        ch = sound.play()
        if ch:
            while ch.get_busy():
                time.sleep(0.05)
    except Exception as exc:
        _log_queue.put(f"⚠  Sound play error: {exc}")


#====[ Voice assignment ]====

_usr_voice: dict         = {}
_voice_lock              = threading.Lock()
_piper_model_cache: dict = {}
_model_cache_lock        = threading.Lock()


def _available_piper_voices() -> list:
    return [n for n in PIPER_VOICES if os.path.isfile(os.path.join(VOICES_DIR, n))]


def get_user_voice(username: str):
    with _voice_lock:
        if username not in _usr_voice:
            available = _available_piper_voices() if PIPER_AVAILABLE else []
            if available:
                _usr_voice[username] = ("piper", random.choice(available))
            else:
                _usr_voice[username] = ("gtts", random.choice(ACCENTS))
        return _usr_voice[username]


def _load_piper_model(model_filename: str):
    with _model_cache_lock:
        if model_filename not in _piper_model_cache:
            model_path = os.path.join(VOICES_DIR, model_filename)
            _piper_model_cache[model_filename] = PiperVoice.load(model_path)
        return _piper_model_cache[model_filename]


#====[ TTS synthesis ]====

def _speak_piper(model_filename: str, text: str) -> None:
    import numpy as np

    voice  = _load_piper_model(model_filename)
    chunks = []

    for sentence_phonemes in voice.phonemize(text):
        ids   = voice.phonemes_to_ids(sentence_phonemes)
        chunk = voice.phoneme_ids_to_audio(ids)
        if chunk is not None and len(chunk) > 0:
            if chunk.dtype == np.float32:
                chunk = (chunk * 32767).clip(-32768, 32767).astype(np.int16)
            chunks.append(chunk)

    if not chunks:
        _log_queue.put("⚠  Piper produced empty audio — is espeak-ng installed?")
        return

    all_audio   = np.concatenate(chunks)
    sample_rate = voice.config.sample_rate
    # Resample into the fixed MIXER_RATE; never touch pygame.mixer init.
    _play_pcm_blocking(all_audio, sample_rate)


def _speak_gtts(lang: str, tld: str, text: str) -> None:
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


#====[ Per-user queues ]====
# Each user gets exactly one background thread and one queue.
# Messages queue up and play in order; no message is dropped.
# Concurrent speakers are capped by the _speaker_gate Condition.
# The thread stays alive as long as messages keep arriving,
# then exits after a short idle timeout and is recreated next time.

_log_queue: queue.Queue = queue.Queue()

# ── Global speaker gate ───────────────────────────────────────────────────────
# A single Condition + counter replaces the swappable-semaphore approach.
# Workers call _speaker_acquire() before speaking and _speaker_release() after.
# The UI slider just writes to state["max_speakers"]; the gate reads it live,
# so there is no object to swap and no risk of acquire/release mismatches.

_speaker_gate    = threading.Condition(threading.Lock())
_active_speakers = 0   # how many threads are currently inside the gate


def _speaker_acquire() -> None:
    """Block until a speaker slot is free, then claim it."""
    global _active_speakers
    with _speaker_gate:
        while _active_speakers >= state["max_speakers"]:
            _speaker_gate.wait()
        _active_speakers += 1


def _speaker_release() -> None:
    """Release a speaker slot and wake any waiting workers."""
    global _active_speakers
    with _speaker_gate:
        _active_speakers = max(0, _active_speakers - 1)
        _speaker_gate.notify_all()


def _rebuild_semaphore(new_max: int) -> None:
    """Called by the UI slider — update state and wake blocked workers to re-check."""
    state["max_speakers"] = new_max
    with _speaker_gate:
        _speaker_gate.notify_all()

# username -> {"queue": Queue, "thread": Thread}
_user_workers: dict     = {}
_user_workers_lock      = threading.Lock()

_WORKER_IDLE_TIMEOUT = 30.0   # seconds before an idle user thread exits


def _user_worker(username: str, q: queue.Queue) -> None:
    """Dedicated thread for one user — drains their queue sequentially."""
    while True:
        try:
            item = q.get(timeout=_WORKER_IDLE_TIMEOUT)
        except queue.Empty:
            # Idle too long — exit and let the slot be reclaimed.
            with _user_workers_lock:
                _user_workers.pop(username, None)
            return

        text, sound_path = item
        voice_type, voice_data = get_user_voice(username)

        # Acquire a speaker slot — blocks until under the max_speakers limit.
        _speaker_acquire()
        try:
            if sound_path:
                play_sound_blocking(sound_path)

            if voice_type == "piper":
                _speak_piper(voice_data, text)
            else:
                lang, tld = voice_data
                _speak_gtts(lang, tld, text)

            # Configurable gap between messages for this user
            delay = state["message_delay"]
            if delay > 0:
                time.sleep(delay)

        except Exception as exc:
            _log_queue.put(f"⚠  TTS error [{username}]: {exc}")
            if voice_type == "piper" and GTTS_AVAILABLE:
                try:
                    _speak_gtts("en", "com", text)
                except Exception:
                    pass
        finally:
            _speaker_release()
            q.task_done()


def _enqueue_for_user(username: str, text: str, sound_path) -> None:
    """Route a message to the user's dedicated queue, creating the worker if needed."""
    with _user_workers_lock:
        entry = _user_workers.get(username)
        if entry is None or not entry["thread"].is_alive():
            q = queue.Queue()
            t = threading.Thread(
                target=_user_worker, args=(username, q), daemon=True, name=f"tts-{username}"
            )
            entry = {"queue": q, "thread": t}
            _user_workers[username] = entry
            t.start()
        entry["queue"].put((text, sound_path))


#====[ IRC handling ]====

_irc_running = False


def connect_irc(channel: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((IRC_HOST, IRC_PORT))
    sock.send(b"PASS SCHMOOPIIE\r\n")
    sock.send(b"NICK justinfan12345\r\n")
    sock.send(f"JOIN #{channel}\r\n".encode())
    return sock


def read_chat(channel: str) -> None:
    global _irc_running
    _irc_running = True
    sock         = connect_irc(channel)
    buf          = ""
    last_ping    = time.time()
    _log_queue.put(f"✅  Connected to #{channel}")

    if PIPER_AVAILABLE and not _available_piper_voices():
        _log_queue.put(
            "⚠  Piper installed but no .onnx models found in voices/ — using gTTS fallback.\n"
            "    Download models from: https://github.com/rhasspy/piper/releases"
        )

    while _irc_running:
        if time.time() - last_ping > PING_INTERVAL:
            sock.send(b"PING :tmi.twitch.tv\r\n")
            last_ping = time.time()

        sock.settimeout(1.0)
        try:
            data = sock.recv(4096).decode("utf-8", errors="ignore")
        except socket.timeout:
            continue
        except Exception as exc:
            _log_queue.put(f"⚠  IRC error: {exc} — reconnecting in 5s…")
            time.sleep(5)
            if not _irc_running:
                break
            sock = connect_irc(channel)
            continue

        buf += data
        while "\r\n" in buf:
            line, buf = buf.split("\r\n", 1)

            if line.startswith("PING"):
                sock.send(b"PONG :tmi.twitch.tv\r\n")
                continue

            match = re.match(r":(\w+)!\w+@\S+ PRIVMSG #\S+ :(.+)", line)
            if not match:
                continue

            username = match.group(1)
            text     = clean_message(match.group(2))
            if not text:
                continue

            if contains_forbidden(text):
                _log_queue.put(f"🚫  [{username}] blocked")
                continue

            sound_path = find_sound_for_message(text)
            if sound_path:
                _log_queue.put(f"🔔  [{username}] sound triggered: {os.path.basename(sound_path)}")

            _log_queue.put(f"💬  [{username}]  {text}")
            _enqueue_for_user(username, text, sound_path)

    sock.close()
    _log_queue.put("⏹  Disconnected.")


#====[ UI ]====

PURPLE   = "#9147ff"
DARK_BG  = "#0e0e10"
MID_BG   = "#18181b"
CARD_BG  = "#1f1f23"
TEXT_COL = "#efeff1"
MUTED    = "#adadb8"
BORDER   = "#2a2a2e"
GREEN    = "#00c853"
RED_COL  = "#e01a4f"
ORANGE   = "#f0a500"


class TwitchTTSApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Twitch TTS Reader")
        self.configure(bg=DARK_BG)
        self.minsize(520, 500)
        self._irc_thread = None
        self._build_ui()
        self._poll_log()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        hdr = tk.Frame(self, bg=PURPLE, padx=16, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="🎙  Twitch TTS Reader",
                 font=("Segoe UI", 14, "bold"),
                 bg=PURPLE, fg="white").pack(side="left")

        badge_text = "Piper TTS" if PIPER_AVAILABLE else "gTTS fallback"
        badge_bg   = GREEN       if PIPER_AVAILABLE else ORANGE
        tk.Label(hdr, text=badge_text, font=("Segoe UI", 8, "bold"),
                 bg=badge_bg, fg="white", padx=6, pady=2).pack(side="right")

        body = tk.Frame(self, bg=DARK_BG, padx=16, pady=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Dark.TNotebook",     background=DARK_BG, borderwidth=0)
        style.configure("Dark.TNotebook.Tab", background=CARD_BG, foreground=MUTED,
                        font=("Segoe UI", 9, "bold"), padding=[12, 5])
        style.map("Dark.TNotebook.Tab",
                  background=[("selected", PURPLE)],
                  foreground=[("selected", "white")])

        nb = ttk.Notebook(body, style="Dark.TNotebook")
        nb.pack(fill="both", expand=True)

        tab_main = tk.Frame(nb, bg=DARK_BG, padx=12, pady=12)
        nb.add(tab_main, text="  Main  ")
        self._build_main_tab(tab_main)

        tab_voices = tk.Frame(nb, bg=DARK_BG, padx=12, pady=12)
        nb.add(tab_voices, text="  Voices  ")
        self._build_voices_tab(tab_voices)

        tab_sounds = tk.Frame(nb, bg=DARK_BG, padx=12, pady=12)
        nb.add(tab_sounds, text="  Sound Triggers  ")
        self._build_sounds_tab(tab_sounds)

        tab_banned = tk.Frame(nb, bg=DARK_BG, padx=12, pady=12)
        nb.add(tab_banned, text="  Banned Words  ")
        self._build_banned_tab(tab_banned)

        ft = tk.Frame(self, bg=DARK_BG, padx=16, pady=6)
        ft.pack(fill="x")
        tk.Label(ft,
                 text="No OAuth needed  •  Anonymous IRC  •  sounds/ and voices/ folders next to script",
                 font=("Segoe UI", 8), bg=DARK_BG, fg=MUTED).pack()

    def _build_main_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(3, weight=1)   # log now at row 3

        self._card(parent, "Channel").grid(row=0, column=0, sticky="ew", pady=(0, 10))
        row = tk.Frame(self._last_crd, bg=CARD_BG)
        row.pack(fill="x", pady=(4, 0))
        self.channel_var = tk.StringVar(value=TWITCH_CHANNEL)
        tk.Entry(row, textvariable=self.channel_var,
                 font=("Segoe UI", 11), bg=MID_BG, fg=TEXT_COL,
                 insertbackground=TEXT_COL, relief="flat",
                 highlightthickness=1, highlightbackground=BORDER,
                 highlightcolor=PURPLE, width=22).pack(side="left", ipady=5, padx=(0, 8))
        self.connect_btn = tk.Button(
            row, text="Connect",
            font=("Segoe UI", 10, "bold"),
            bg=PURPLE, fg="white", relief="flat",
            activebackground="#7b2fbe", activeforeground="white",
            cursor="hand2", padx=12, pady=4,
            command=self._toggle_connection)
        self.connect_btn.pack(side="left")

        # ── Row 1: Volume + Max Characters ────────────────────────────────────
        ctrl_row1 = tk.Frame(parent, bg=DARK_BG)
        ctrl_row1.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        ctrl_row1.columnconfigure(0, weight=1)
        ctrl_row1.columnconfigure(1, weight=1)

        self._card(ctrl_row1, "Volume").grid(row=0, column=0, sticky="ew", padx=(0, 6))
        vol_card = self._last_crd
        self.vol_label = tk.Label(vol_card, text=f"{int(state['volume'] * 100)}%",
                                  font=("Segoe UI", 11, "bold"), bg=CARD_BG, fg=PURPLE)
        self.vol_label.pack(anchor="e")
        self.vol_var = tk.DoubleVar(value=state["volume"])
        tk.Scale(vol_card, from_=0.0, to=1.0, resolution=0.01,
                 orient="horizontal", variable=self.vol_var, command=self._on_volume,
                 bg=CARD_BG, fg=TEXT_COL, troughcolor=MID_BG, activebackground=PURPLE,
                 highlightthickness=0, sliderrelief="flat", bd=0, showvalue=False,
                 length=160).pack(fill="x")

        self._card(ctrl_row1, "Max Characters").grid(row=0, column=1, sticky="ew", padx=(6, 0))
        char_card = self._last_crd
        self.char_label = tk.Label(char_card, text=f"{state['max_chars']} chars",
                                   font=("Segoe UI", 11, "bold"), bg=CARD_BG, fg=PURPLE)
        self.char_label.pack(anchor="e")
        self.char_var = tk.IntVar(value=state["max_chars"])
        tk.Scale(char_card, from_=10, to=500, resolution=5,
                 orient="horizontal", variable=self.char_var, command=self._on_char_limit,
                 bg=CARD_BG, fg=TEXT_COL, troughcolor=MID_BG, activebackground=PURPLE,
                 highlightthickness=0, sliderrelief="flat", bd=0, showvalue=False,
                 length=160).pack(fill="x")

        # ── Row 2: Max Speakers + Message Delay ───────────────────────────────
        ctrl_row2 = tk.Frame(parent, bg=DARK_BG)
        ctrl_row2.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        ctrl_row2.columnconfigure(0, weight=1)
        ctrl_row2.columnconfigure(1, weight=1)

        self._card(ctrl_row2, "Max Speakers").grid(row=0, column=0, sticky="ew", padx=(0, 6))
        spk_card = self._last_crd
        self.spk_label = tk.Label(spk_card, text=f"{state['max_speakers']}",
                                  font=("Segoe UI", 11, "bold"), bg=CARD_BG, fg=PURPLE)
        self.spk_label.pack(anchor="e")
        self.spk_var = tk.IntVar(value=state["max_speakers"])
        tk.Scale(spk_card, from_=1, to=20, resolution=1,
                 orient="horizontal", variable=self.spk_var, command=self._on_max_speakers,
                 bg=CARD_BG, fg=TEXT_COL, troughcolor=MID_BG, activebackground=PURPLE,
                 highlightthickness=0, sliderrelief="flat", bd=0, showvalue=False,
                 length=160).pack(fill="x")
        tk.Label(spk_card, text="voices at once  (1 = fully sequential)",
                 font=("Segoe UI", 7), bg=CARD_BG, fg=MUTED).pack(anchor="w")

        self._card(ctrl_row2, "Delay Between Messages").grid(row=0, column=1, sticky="ew", padx=(6, 0))
        dly_card = self._last_crd
        self.dly_label = tk.Label(dly_card, text=f"{state['message_delay']:.1f}s",
                                  font=("Segoe UI", 11, "bold"), bg=CARD_BG, fg=PURPLE)
        self.dly_label.pack(anchor="e")
        self.dly_var = tk.DoubleVar(value=state["message_delay"])
        tk.Scale(dly_card, from_=0.0, to=10.0, resolution=0.5,
                 orient="horizontal", variable=self.dly_var, command=self._on_message_delay,
                 bg=CARD_BG, fg=TEXT_COL, troughcolor=MID_BG, activebackground=PURPLE,
                 highlightthickness=0, sliderrelief="flat", bd=0, showvalue=False,
                 length=160).pack(fill="x")
        tk.Label(dly_card, text="pause after each clip per user",
                 font=("Segoe UI", 7), bg=CARD_BG, fg=MUTED).pack(anchor="w")

        # ── Row 3: Chat Log ───────────────────────────────────────────────────
        log_card = self._card(parent, "Chat Log")
        log_card.grid(row=3, column=0, sticky="nsew")
        log_card.columnconfigure(0, weight=1)
        log_card.rowconfigure(0, weight=1)
        self._last_crd.pack_configure(expand=True, fill="both")

        self.log = scrolledtext.ScrolledText(
            self._last_crd, height=14, width=58,
            font=("Consolas", 9), bg=MID_BG, fg=TEXT_COL,
            insertbackground=TEXT_COL, relief="flat",
            state="disabled", wrap="word", highlightthickness=0)
        self.log.pack(fill="both", expand=True)
        self.log.tag_config("blocked", foreground="#ff4d4d")
        self.log.tag_config("skipped", foreground=ORANGE)
        self.log.tag_config("system",  foreground=PURPLE)
        self.log.tag_config("sound",   foreground=GREEN)
        self.log.tag_config("chat",    foreground=TEXT_COL)

    def _build_voices_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(3, weight=1)

        status_text = ("✅  piper-tts installed — neural voices active"
                       if PIPER_AVAILABLE else
                       "⚠  piper-tts not installed — using gTTS (internet required)")
        status_col  = GREEN if PIPER_AVAILABLE else ORANGE
        tk.Label(parent, text=status_text, font=("Segoe UI", 9, "bold"),
                 bg=DARK_BG, fg=status_col, anchor="w").grid(
                 row=0, column=0, sticky="ew", pady=(0, 6))

        tk.Label(parent,
            text=("Drop  .onnx + .onnx.json  model pairs into the  voices/  folder.\n"
                  "Each chatter gets one voice assigned randomly at session start.\n"
                  "Models: https://github.com/rhasspy/piper/releases\n"
                  "Recommended: en_US-lessac-medium, en_US-amy-medium, en_GB-alan-medium"),
            font=("Segoe UI", 9), bg=DARK_BG, fg=MUTED, justify="left").grid(
            row=1, column=0, sticky="w", pady=(0, 10))

        folder_row = tk.Frame(parent, bg=DARK_BG)
        folder_row.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        folder_row.columnconfigure(0, weight=1)
        tk.Label(folder_row, text=f"📁  {VOICES_DIR}",
                 font=("Segoe UI", 8), bg=DARK_BG, fg=MUTED, anchor="w").grid(
                 row=0, column=0, sticky="ew")
        tk.Button(folder_row, text="Open Folder", font=("Segoe UI", 8),
                  bg=CARD_BG, fg=TEXT_COL, relief="flat", cursor="hand2",
                  command=lambda: os.startfile(VOICES_DIR)).grid(row=0, column=1)

        tree_frame = tk.Frame(parent, bg=CARD_BG,
                              highlightthickness=1, highlightbackground=BORDER)
        tree_frame.grid(row=3, column=0, sticky="nsew", pady=(0, 8))
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        style = ttk.Style()
        style.configure("Voices.Treeview",
                         background=MID_BG, foreground=TEXT_COL,
                         fieldbackground=MID_BG, rowheight=26,
                         font=("Segoe UI", 10))
        style.configure("Voices.Treeview.Heading",
                         background=CARD_BG, foreground=MUTED,
                         font=("Segoe UI", 8, "bold"), relief="flat")
        style.map("Voices.Treeview", background=[("selected", PURPLE)])

        self.voice_tree = ttk.Treeview(tree_frame, columns=("Model File", "Status"),
                                        show="headings", style="Voices.Treeview", height=8)
        self.voice_tree.heading("Model File", text="MODEL FILE")
        self.voice_tree.heading("Status",     text="STATUS")
        self.voice_tree.column("Model File",  width=300, anchor="w")
        self.voice_tree.column("Status",      width=100, anchor="center")

        sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.voice_tree.yview)
        self.voice_tree.configure(yscrollcommand=sb.set)
        self.voice_tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        tk.Button(parent, text="↻  Refresh", font=("Segoe UI", 9),
                  bg=CARD_BG, fg=TEXT_COL, relief="flat", cursor="hand2",
                  command=self._refresh_voice_tree).grid(row=4, column=0, sticky="w", pady=(4, 0))

        self._refresh_voice_tree()

    def _refresh_voice_tree(self):
        self.voice_tree.delete(*self.voice_tree.get_children())
        for name in PIPER_VOICES:
            found  = os.path.isfile(os.path.join(VOICES_DIR, name))
            status = "✔ ready" if found else "✘ missing"
            self.voice_tree.insert("", "end", values=(name, status),
                                   tags=("ok" if found else "missing",))
        self.voice_tree.tag_configure("ok",      foreground=GREEN)
        self.voice_tree.tag_configure("missing", foreground=RED_COL)

    def _build_sounds_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        tk.Label(parent,
            text=("Map a keyword to a sound file in your  sounds/  folder.\n"
                  "The sound plays before TTS whenever the keyword appears in a message.\n"
                  "Keyword matching is a substring check, case-insensitive."),
            font=("Segoe UI", 9), bg=DARK_BG, fg=MUTED, justify="left").grid(
            row=0, column=0, sticky="w", pady=(0, 10))

        folder_row = tk.Frame(parent, bg=DARK_BG)
        folder_row.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        folder_row.columnconfigure(0, weight=1)
        tk.Label(folder_row, text=f"📁  {SOUNDS_DIR}",
                 font=("Segoe UI", 8), bg=DARK_BG, fg=MUTED, anchor="w").grid(
                 row=0, column=0, sticky="ew")
        tk.Button(folder_row, text="Open Folder", font=("Segoe UI", 8),
                  bg=CARD_BG, fg=TEXT_COL, relief="flat", cursor="hand2",
                  command=lambda: os.startfile(SOUNDS_DIR)).grid(row=0, column=1)

        tree_frame = tk.Frame(parent, bg=CARD_BG,
                              highlightthickness=1, highlightbackground=BORDER)
        tree_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 8))
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        style = ttk.Style()
        style.configure("Sounds.Treeview",
                         background=MID_BG, foreground=TEXT_COL,
                         fieldbackground=MID_BG, rowheight=26,
                         font=("Segoe UI", 10))
        style.configure("Sounds.Treeview.Heading",
                         background=CARD_BG, foreground=MUTED,
                         font=("Segoe UI", 8, "bold"), relief="flat")
        style.map("Sounds.Treeview", background=[("selected", PURPLE)])

        cols = ("Keyword", "Sound File", "Status")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                                  style="Sounds.Treeview", height=8)
        self.tree.heading("Keyword",    text="KEYWORD")
        self.tree.heading("Sound File", text="SOUND FILE")
        self.tree.heading("Status",     text="STATUS")
        self.tree.column("Keyword",    width=150, anchor="w")
        self.tree.column("Sound File", width=200, anchor="w")
        self.tree.column("Status",     width=100, anchor="center")

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
                  command=self._browse_sound).pack(side="left", padx=(0, 8))
        tk.Button(add_frame, text="＋ Add", font=("Segoe UI", 9, "bold"),
                  bg=GREEN, fg="white", relief="flat", cursor="hand2",
                  command=self._add_trigger).pack(side="left", padx=(0, 6))
        tk.Button(add_frame, text="✕ Remove", font=("Segoe UI", 9),
                  bg=RED_COL, fg="white", relief="flat", cursor="hand2",
                  command=self._remove_trigger).pack(side="left")

        self._refresh_tree()

    def _build_banned_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(1, weight=1)

        tk.Label(parent,
            text=("Words: exact whole-word matches (case-insensitive).\n"
                  "Strings: substring matches anywhere in the message."),
            font=("Segoe UI", 9), bg=DARK_BG, fg=MUTED, justify="left").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        for col, title, attr, var_attr, add_cmd, rem_cmd in [
            (0, "BANNED WORDS",   "words_list",   "new_word_var",
             self._add_banned_word,   self._remove_banned_word),
            (1, "BANNED STRINGS", "strings_list", "new_string_var",
             self._add_banned_string, self._remove_banned_string),
        ]:
            outer = tk.Frame(parent, bg=CARD_BG,
                             highlightthickness=1, highlightbackground=BORDER)
            outer.grid(row=1, column=col, sticky="nsew", padx=(0, 6) if col == 0 else (6, 0))
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
            tk.Button(add_row, text="＋", font=("Segoe UI", 10, "bold"),
                      bg=GREEN, fg="white", relief="flat", cursor="hand2",
                      padx=6, command=add_cmd).pack(side="left", padx=(0, 4))
            tk.Button(add_row, text="✕", font=("Segoe UI", 10),
                      bg=RED_COL, fg="white", relief="flat", cursor="hand2",
                      padx=6, command=rem_cmd).pack(side="left")

        self._refresh_banned_lists()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _card(self, parent, title: str):
        outer = tk.Frame(parent, bg=CARD_BG,
                         highlightthickness=1, highlightbackground=BORDER)
        tk.Label(outer, text=title.upper(), font=("Segoe UI", 7, "bold"),
                 bg=CARD_BG, fg=MUTED).pack(anchor="w", padx=10, pady=(8, 2))
        inner = tk.Frame(outer, bg=CARD_BG, padx=10)
        inner.pack(fill="both", pady=(0, 10))
        self._last_crd = inner
        return outer

    # ── Events ────────────────────────────────────────────────────────────────

    def _on_volume(self, val):
        v = float(val)
        state["volume"] = v
        self.vol_label.config(text=f"{int(v * 100)}%")

    def _on_char_limit(self, val):
        c = int(val)
        state["max_chars"] = c
        self.char_label.config(text=f"{c} chars")

    def _on_max_speakers(self, val):
        n = int(float(val))
        state["max_speakers"] = n
        self.spk_label.config(text=str(n))
        _rebuild_semaphore(n)

    def _on_message_delay(self, val):
        d = round(float(val) * 2) / 2   # snap to 0.5s steps
        state["message_delay"] = d
        self.dly_label.config(text=f"{d:.1f}s")

    def _toggle_connection(self):
        global _irc_running
        if self._irc_thread and self._irc_thread.is_alive():
            _irc_running = False
            self.connect_btn.config(text="Connect", bg=PURPLE)
        else:
            channel = self.channel_var.get().strip().lstrip("#")
            if not channel:
                self._append_log("⚠  Enter a channel name first.", "blocked")
                return
            _irc_running = False
            time.sleep(0.2)
            self._irc_thread = threading.Thread(
                target=read_chat, args=(channel,), daemon=True)
            self._irc_thread.start()
            self.connect_btn.config(text="Disconnect", bg=RED_COL)

    def _browse_sound(self):
        path = filedialog.askopenfilename(
            initialdir=SOUNDS_DIR,
            title="Select sound file",
            filetypes=[("Audio files", "*.wav *.mp3 *.ogg"), ("All files", "*.*")])
        if path:
            self.new_file_var.set(os.path.basename(path))

    def _add_trigger(self):
        kw = self.new_keyword_var.get().strip().lower()
        fn = self.new_file_var.get().strip()
        if not kw or not fn:
            messagebox.showwarning("Missing input", "Enter both a keyword and a file name.")
            return
        SOUND_TRIGGERS[kw] = fn
        self.new_keyword_var.set("")
        self.new_file_var.set("")
        self._refresh_tree()

    def _remove_trigger(self):
        for iid in self.tree.selection():
            SOUND_TRIGGERS.pop(self.tree.item(iid, "values")[0], None)
        self._refresh_tree()

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for kw, fn in sorted(SOUND_TRIGGERS.items()):
            path   = os.path.join(SOUNDS_DIR, fn)
            found  = os.path.isfile(path)
            self.tree.insert("", "end",
                             values=(kw, fn, "✔ found" if found else "✘ missing"),
                             tags=("ok" if found else "missing",))
        self.tree.tag_configure("ok",      foreground=GREEN)
        self.tree.tag_configure("missing", foreground=RED_COL)

    def _add_banned_word(self):
        w = self.new_word_var.get().strip().lower()
        if w:
            FORBIDDEN_WORDS.add(w)
            self.new_word_var.set("")
            self._refresh_banned_lists()

    def _remove_banned_word(self):
        for i in reversed(self.words_list.curselection()):
            FORBIDDEN_WORDS.discard(self.words_list.get(i))
        self._refresh_banned_lists()

    def _add_banned_string(self):
        s = self.new_string_var.get().strip().lower()
        if s:
            FORBIDDEN_STRINGS.add(s)
            self.new_string_var.set("")
            self._refresh_banned_lists()

    def _remove_banned_string(self):
        for i in reversed(self.strings_list.curselection()):
            FORBIDDEN_STRINGS.discard(self.strings_list.get(i))
        self._refresh_banned_lists()

    def _refresh_banned_lists(self):
        self.words_list.delete(0, "end")
        for w in sorted(FORBIDDEN_WORDS):
            self.words_list.insert("end", w)
        self.strings_list.delete(0, "end")
        for s in sorted(FORBIDDEN_STRINGS):
            self.strings_list.insert("end", s)

    def _append_log(self, msg: str, tag: str = "chat"):
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n", tag)
        self.log.see("end")
        self.log.config(state="disabled")

    def _poll_log(self):
        while True:
            try:
                msg = _log_queue.get_nowait()
            except queue.Empty:
                break
            if   msg.startswith("🚫"): tag = "blocked"
            elif msg.startswith("⏭"): tag = "skipped"
            elif msg.startswith("🔔"): tag = "sound"
            elif msg.startswith(("✅", "⏹", "⚠")): tag = "system"
            else: tag = "chat"
            self._append_log(msg, tag)
        self.after(150, self._poll_log)

    def _on_close(self):
        save_config(self.channel_var.get().strip().lstrip("#"))
        self.destroy()


#====[ Entry ]====

def main():
    app = TwitchTTSApp()
    app.mainloop()


if __name__ == "__main__":
    main()