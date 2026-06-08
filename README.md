# 🎙 Speak My Chat!

**Reads your Twitch chat out loud. Type a channel name, hit Connect, done.**
No login, no token, no setup.

![screenshot](example/example1.png)

---

## ✦ Download & Run

### Easiest — grab the ready-to-go version

1. Go to the [**Releases**](../../releases) page.
2. Download **`SpeakMyChat-windows.zip`**.
3. Right-click the zip → **Extract All**.
4. Open the extracted folder and double-click **`SimpleTTS.exe`**.

That's it. It comes with a voice and a sound effect already set up, so it talks the moment you open it.

> **Windows says "Windows protected your PC"?**
> That's the normal warning for any app that isn't from a big paid publisher — it doesn't mean anything is wrong. Click **More info → Run anyway**. If you'd rather not trust a prebuilt file at all, run from source or build it yourself (both below).

> ⚠️ **Keep the `voices/` and `sounds/` folders next to the .exe.** The app looks for them right beside it — don't drag the .exe out on its own.

### Run from source (Python)

If you have Python installed:

```
pip install piper-tts gtts pygame-ce
python main.py
```

### Build the .exe yourself

Every release is built automatically from this repo by GitHub Actions — nothing is uploaded by hand. If you want to verify exactly what's in the file you run, clone the repo and reproduce the build. The complete, working build command (including the voice-engine data it needs to bundle) lives in **`.github/workflows/build.yml`**.

---

## ✦ Adding more voices

Speak My Chat uses **piper-tts** for offline neural voices. Each chatter gets a random voice the first time they talk.

To add your own:

1. Download `.onnx` + `.onnx.json` pairs from the [Piper releases page](https://github.com/rhasspy/piper/releases).
2. Drop **both** files into the `voices/` folder (next to the .exe).
3. Restart the app — it picks them up automatically.

**Good ones to start with:**

- `en_US-lessac-medium`
- `en_US-amy-medium`
- `en_GB-alan-medium`

> Empty `voices/` folder? The app falls back to your computer's built-in voice (or an online one), so it'll still talk.

---

## ✦ Sound triggers

Play a sound effect when a keyword shows up in chat.

1. Drop `.mp3`, `.wav`, or `.ogg` files into the `sounds/` folder.
2. Open the **Sound Triggers** tab in the app.
3. Map a keyword → a filename.

The sound plays right before the message is read. Matching is case-insensitive, so `diamond` also catches `DIAMOND`, `diamonds`, and so on.

---

## ✦ Other things you can set up

- **Banned words** — block messages with certain words or phrases (Banned Words tab).
- **Filters** — read only moderators, or only channel-point redemptions (Main tab).
- **Volume, message length, speakers at once, delay** — all on the Main tab.

---

## License

MIT — [finefit](https://finefit.dev) 2026
