#  -=< simpleTTS ✦ >=-
### reads twitch chat out loud. enter a channel, hit connect, done. ݁˖

![screenshot](example/example1.png)

---

## -=< download ✦ >=-

**just want to run it?**
grab the exe from [Releases](../../releases) — it'll create the `voices/` and `sounds/` folders automatically on first launch. no install, no setup.

**want to run from source?**
```
pip install piper-tts gtts pygame-ce
python main.py
```

> ⚠ windows might warn you it's suspicious — that's just SmartScreen flagging unknown exes. the source is right here if you want to check it yourself.

---

## -=< voices ✦ >=-

simpleTTS uses **piper-tts** for offline neural voices. each chatter gets one assigned randomly when they first speak.

**to add voices:**
1. download `.onnx` + `.onnx.json` pairs from the [piper releases page](https://github.com/rhasspy/piper/releases)
2. drop both files into the `voices/` folder next to the exe
3. restart the app — it'll pick them up automatically

**recommended models** ✦
- `en_US-lessac-medium`
- `en_US-amy-medium`
- `en_GB-alan-medium`

no voices found? it falls back to gTTS automatically (needs internet).

---

## -=< sound triggers ✦ >=-

play a sound effect when a keyword appears in chat.

1. drop `.mp3 / .wav / .ogg` files into the `sounds/` folder
2. open the **Sound Triggers** tab in the app
3. map a keyword → filename

the sound plays before the message is read out. matching is case-insensitive substring so `diamond` catches `DIAMOND`, `diamonds`, etc.

---

## -=< controls ✦ >=-

| setting | what it does |
|---|---|
| volume | how loud the voices are |
| max characters | cuts messages off after N chars |
| max speakers | how many voices can talk at once (1 = fully sequential) |
| delay between messages | gap of silence after each clip, per user |

banned words and strings live in their own tab — words are whole-word matched, strings are substring matched anywhere in the message.

---

## -=< folder structure ✦ >=-

```
SimpleTTS/
├── SimpleTTS.exe
├── voices/          ← drop .onnx + .onnx.json pairs here
└── sounds/          ← drop .mp3/.wav/.ogg files here
```

config saves automatically to `config.json` when you close the app. ✦

---

## -=< license ✦ >=-
MIT — [finefit](https://finefit.dev) 2026 ݁˖
