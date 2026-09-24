"""jarvis-satellite: pair this PC with Jarvis, then say “Hey Jarvis”."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import socket
import sys
import threading
import time
import wave
from logging.handlers import RotatingFileHandler
from typing import Any

from jarvis_satellite import __version__
from jarvis_satellite.audio import Microphone, Speaker, rms
from jarvis_satellite.config import Settings, data_dir, default_path, load, save
from jarvis_satellite.pairing import KeyringStore, PairingError, pair
from jarvis_satellite.protocol import INPUT_RATE
from jarvis_satellite.session import Satellite
from jarvis_satellite.tray import ConsoleIndicator, start_hotkey
from jarvis_satellite.wake import OpenWakeWord, WakeError, fetch_models, models_ready

log = logging.getLogger("jarvis.satellite")
LOCK_PORT = 47813  # held while running, so only one satellite listens at a time


def models_dir() -> Any:
    return data_dir() / "models"


def setup_logging(*, console: bool) -> None:
    handlers: list[logging.Handler] = []
    log_dir = data_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers.append(
        RotatingFileHandler(
            log_dir / "satellite.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
    )
    if console:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def alert(message: str) -> None:
    """Tell the owner about a problem that stops the app, console or not."""
    log.error(message)
    if sys.stdout is not None:
        print(message)
    elif sys.platform == "win32":  # started at logon: no console to print to
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "Jarvis", 0x40)


def single_instance() -> socket.socket | None:
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        lock.close()
        return None
    return lock


# --- Commands -----------------------------------------------------------------------


def cmd_pair(settings: Settings, args: argparse.Namespace) -> int:
    changed = False
    if args.server:
        settings = Settings(**{**vars(settings), "server": args.server})
        changed = True
    if args.name:
        settings = Settings(**{**vars(settings), "name": args.name})
        changed = True
    try:
        pair(settings, args.code, store=KeyringStore())
    except PairingError as exc:
        print(exc)
        return 1
    if changed or not default_path().exists():
        save(settings)
    print(f"Paired “{settings.name}” with Jarvis at {settings.server}.")
    return 0


def cmd_unpair(settings: Settings, _args: argparse.Namespace) -> int:
    KeyringStore().delete(settings.server)
    print("This PC's device token is gone. Remove the device in Jarvis → Settings → Voice too.")
    return 0


def cmd_fetch_models(_settings: Settings, _args: argparse.Namespace) -> int:
    try:
        fetched = fetch_models(models_dir())
    except WakeError as exc:
        print(exc)
        return 1
    print("Downloaded: " + ", ".join(fetched) if fetched else "The wake-word models are ready.")
    return 0


def cmd_status(settings: Settings, _args: argparse.Namespace) -> int:
    paired = KeyringStore().get(settings.server) is not None
    print(f"jarvis-satellite {__version__}")
    print(f"Settings:     {default_path()}")
    print(f"Jarvis:       {settings.server}")
    print(f"This PC:      {settings.name} ({'paired' if paired else 'not paired'})")
    print(f"Wake models:  {'ready' if models_ready(models_dir()) else 'missing (fetch-models)'}")
    print(f"Mute hotkey:  {settings.mute_hotkey}")
    return 0


def cmd_devices(_settings: Settings, _args: argparse.Namespace) -> int:
    import sounddevice as sd

    print(sd.query_devices())
    return 0


async def _test_mic(settings: Settings, seconds: float) -> None:
    try:
        wake: OpenWakeWord | None = OpenWakeWord(models_dir(), threshold=settings.wake_threshold)
    except WakeError as exc:
        print(f"(no wake-word score: {exc})")
        wake = None
    end = time.monotonic() + seconds
    async for frame in Microphone(settings.input_device).frames():
        level = rms(frame)
        heard = wake.detect(frame) if wake else False
        score = wake.last_score if wake else 0.0
        bar = "#" * int(min(1.0, level * 8) * 30)
        note = "  <- Hey Jarvis!" if heard else ""
        print(f"\r{bar:<30} level {level:.3f}  wake {score:.2f}{note}   ", end="", flush=True)
        if time.monotonic() > end:
            print()
            return


def cmd_test_mic(settings: Settings, args: argparse.Namespace) -> int:
    print(f"Listening for {args.seconds:.0f} s. Talk, and try “Hey Jarvis”.")
    asyncio.run(_test_mic(settings, args.seconds))
    return 0


async def _record(settings: Settings, minutes: float, out: str) -> int:
    frames_needed = int(minutes * 60 * INPUT_RATE)
    written = 0
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(INPUT_RATE)
        async for frame in Microphone(settings.input_device).frames():
            wav.writeframes(frame.tobytes())
            written += len(frame)
            if written % (INPUT_RATE * 30) < len(frame):
                print(f"\r{written / INPUT_RATE / 60:.1f} of {minutes:g} minutes", end="")
            if written >= frames_needed:
                break
    print(f"\nSaved {out}")
    return 0


def cmd_record(settings: Settings, args: argparse.Namespace) -> int:
    print(f"Recording the room for {args.minutes:g} minutes into {args.out}.")
    print("Carry on as normal (TV, music, talking), just don't say “Hey Jarvis”.")
    return asyncio.run(_record(settings, args.minutes, args.out))


def cmd_run(settings: Settings, args: argparse.Namespace) -> int:
    lock = single_instance()
    if lock is None:
        if sys.stdout is not None:
            print("The satellite is already running.")
        return 1
    try:
        wake = OpenWakeWord(models_dir(), threshold=settings.wake_threshold)
    except WakeError as exc:
        alert(str(exc))
        return 1
    tokens = KeyringStore()
    if tokens.get(settings.server) is None:
        alert(
            "This PC isn't paired with Jarvis yet. In Jarvis, open Settings → Voice → Pair "
            "the Windows app, then run: jarvis-satellite pair CODE"
        )
        return 1
    speaker = Speaker(settings.output_device)
    speaker.start()
    mic = Microphone(settings.input_device)
    try:
        if not args.no_tray and sys.platform == "win32":
            return _run_with_tray(settings, tokens, mic, speaker, wake)
        return _run_in_console(settings, tokens, mic, speaker, wake)
    finally:
        speaker.close()
        lock.close()


def _run_in_console(
    settings: Settings, tokens: KeyringStore, mic: Microphone, speaker: Speaker, wake: Any
) -> int:
    satellite = Satellite(
        settings, tokens=tokens, mic=mic, speaker=speaker, wake=wake, indicator=ConsoleIndicator()
    )
    loop = asyncio.new_event_loop()
    hotkey = start_hotkey(
        settings.mute_hotkey, lambda: loop.call_soon_threadsafe(satellite.toggle_mute)
    )
    print(f"Say “Hey Jarvis”. Mute with {settings.mute_hotkey}; stop with Ctrl+C.")
    try:
        loop.run_until_complete(satellite.run())
    except KeyboardInterrupt:
        pass
    finally:
        if hotkey is not None:
            hotkey.stop()
        loop.close()
    return 0


def _run_with_tray(
    settings: Settings, tokens: KeyringStore, mic: Microphone, speaker: Speaker, wake: Any
) -> int:
    from jarvis_satellite.tray import Tray

    loop = asyncio.new_event_loop()
    task: dict[str, asyncio.Task[None]] = {}

    def quit_app() -> None:
        if "run" in task:
            loop.call_soon_threadsafe(task["run"].cancel)
        tray.stop()

    tray = Tray(on_mute=lambda: loop.call_soon_threadsafe(satellite.toggle_mute), on_quit=quit_app)
    satellite = Satellite(
        settings, tokens=tokens, mic=mic, speaker=speaker, wake=wake, indicator=tray
    )

    def background() -> None:
        asyncio.set_event_loop(loop)
        task["run"] = loop.create_task(satellite.run())
        with contextlib.suppress(asyncio.CancelledError):
            loop.run_until_complete(task["run"])

    worker = threading.Thread(target=background, name="satellite", daemon=True)
    hotkey = start_hotkey(
        settings.mute_hotkey, lambda: loop.call_soon_threadsafe(satellite.toggle_mute)
    )
    tray.run(setup=worker.start)
    worker.join(timeout=5)
    if hotkey is not None:
        hotkey.stop()
    return 0


COMMANDS = {
    "pair": cmd_pair,
    "unpair": cmd_unpair,
    "run": cmd_run,
    "fetch-models": cmd_fetch_models,
    "status": cmd_status,
    "devices": cmd_devices,
    "test-mic": cmd_test_mic,
    "record": cmd_record,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jarvis-satellite", description="Say “Hey Jarvis” to this PC and talk to Jarvis."
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pair", help="pair this PC using a code from Jarvis → Settings → Voice")
    p.add_argument("code")
    p.add_argument("--server", help="Jarvis's address (default http://localhost:8080)")
    p.add_argument("--name", help="what to call this PC in Jarvis")
    sub.add_parser("unpair", help="forget this PC's device token")
    p = sub.add_parser("run", help="listen for “Hey Jarvis” (with a tray icon on Windows)")
    p.add_argument("--no-tray", action="store_true", help="run in this console instead")
    sub.add_parser("fetch-models", help="download the wake-word models (checksum-pinned)")
    sub.add_parser("status", help="show the settings and whether this PC is paired")
    sub.add_parser("devices", help="list microphones and speakers")
    p = sub.add_parser("test-mic", help="show the microphone level and wake-word score")
    p.add_argument("--seconds", type=float, default=15)
    p = sub.add_parser("record", help="record the room (for the false-accept test)")
    p.add_argument("--minutes", type=float, default=60)
    p.add_argument("--out", default="room.wav")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(console=args.command == "run" and sys.stderr is not None)
    try:
        settings = load()
    except (ValueError, OSError) as exc:
        print(f"Your settings file ({default_path()}) has a problem: {exc}")
        return 1
    return COMMANDS[args.command](settings, args)


def tray_main() -> None:
    """Started at logon (no console window)."""
    sys.exit(main(["run"]))


if __name__ == "__main__":
    sys.exit(main())
