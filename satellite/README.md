# Jarvis satellite: “Hey Jarvis” on your Windows PC

A small tray app. Say **“Hey Jarvis”**, then talk; Jarvis answers through your
speakers. It's the same Jarvis as the app and Telegram: same memory, same
approvals, same audit log.

## Install

In **Windows PowerShell** (not WSL), from the Jarvis folder:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\install-satellite.ps1
```

It installs the app, downloads the wake-word models (checked against pinned
SHA-256 sums), asks for a pairing code, and starts it at every logon. Get the
code in Jarvis: **Settings → Voice → Pair the Windows app** (it needs a passkey
tap, works once, and expires in 10 minutes).

Windows must let desktop apps use the microphone: **Settings → Privacy &
security → Microphone → Let desktop apps access your microphone**.

## Using it

* Say “Hey Jarvis”, wait for the chime, and speak. You can also say it in one
  go: “Hey Jarvis, what's on my calendar today?”
* After Jarvis answers, you have a few seconds for a follow-up without saying
  “Hey Jarvis” again.
* To stop Jarvis mid-answer, say “Hey Jarvis” over it.
* When Jarvis reads an action back, say **“confirm”** or **“cancel”**.
  High-risk actions always need the app and your passkey instead.
* **Mute:** the tray menu, or **Ctrl+Alt+J**. Muted, the microphone is ignored
  completely, even for the wake word.
* “Jarvis, stand down” pauses everything Jarvis does on its own.

The tray icon's colour shows what it's doing: dim blue asleep, bright blue
listening, amber thinking, green speaking, red muted, grey for a problem (with a
notification saying what's wrong).

## Privacy

* Until it hears the wake word, **nothing leaves your PC** and no connection is
  open. The wake word is detected locally by openWakeWord.
* While Jarvis speaks, your microphone isn't sent, so Jarvis never hears itself.
* It only connects to your own Jarvis (default `http://localhost:8080`), with a
  device token kept in Windows Credential Manager. Remove the PC in
  **Settings → Voice** to cut it off.

## Commands

```text
jarvis-satellite status         settings, pairing and wake-word models
jarvis-satellite pair CODE      pair this PC (--server URL, --name NAME)
jarvis-satellite unpair         forget this PC's token
jarvis-satellite run            run with the tray icon (--no-tray: in the console)
jarvis-satellite test-mic       live microphone level and wake-word score
jarvis-satellite devices        list microphones and speakers
jarvis-satellite fetch-models   download the wake-word models
jarvis-satellite record         record the room for the false-accept test
```

Settings live in `%APPDATA%\Jarvis\satellite.toml`:

| Setting | Default | Meaning |
|---|---|---|
| `server` | `http://localhost:8080` | Jarvis's address |
| `name` | the PC's name | how it appears in Settings → Voice |
| `wake_threshold` | `0.5` | higher means fewer false wakes and more misses |
| `follow_up_seconds` | `6` | listening time for a follow-up |
| `confirm_seconds` | `20` | listening time while an action waits for “confirm” |
| `input_device`, `output_device` | system default | part of a device name (see `devices`) |
| `mute_hotkey` | `<ctrl>+<alt>+j` | pynput format |
| `chime` | `true` | the “I'm listening” chime |

The log is in `%LOCALAPPDATA%\Jarvis\satellite\satellite.log`. It never
contains what you said or the token.

## Wake-word check (the < 1 false wake per hour target)

```powershell
jarvis-satellite record --minutes 60 --out room.wav   # normal life: TV, music, talking
python bench\false_accepts.py room.wav                  # from the satellite folder
```

It reports false wakes per hour at your threshold. If it's 1 or more, raise
`wake_threshold` (for example to 0.6) and check again. Add `--positives DIR`
with recordings of you saying “Hey Jarvis” to see how often it still wakes.

## Troubleshooting

| Problem | Fix |
|---|---|
| “Can't reach Jarvis” | Is Jarvis running? In WSL: `make up`, then `make doctor`. |
| “Not paired” | Pair again: Settings → Voice → Pair the Windows app. |
| Never wakes | `jarvis-satellite test-mic`: the level should move when you talk. Check the Windows microphone privacy setting and `input_device`. |
| Wakes by itself | Raise `wake_threshold`; run the wake-word check above. |
| “Voice is already open somewhere else” | Jarvis takes two voice conversations at a time. Close the Talk page on another device, or finish the conversation on another PC. |

## Development

```bash
uv sync
uv run pytest        # Linux runs everything except the real wake-word model
uv run ruff check . && uv run pyright
```

The wake-word model (openWakeWord) only installs on Windows (on Linux it needs
`tflite-runtime`, which has no Python 3.12 build), so CI runs the real-model
tests on Windows and everything else on both.
