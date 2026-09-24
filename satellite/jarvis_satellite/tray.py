"""The tray icon: shows what the satellite is doing, with Mute and Quit.

Windows only (pystray + Pillow). The icon's colour is the state; problems also
pop up as a Windows notification (at most once a minute for the same message).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger("jarvis.satellite")

COLOURS = {
    "sleeping": (70, 110, 130),
    "connecting": (90, 170, 200),
    "listening": (40, 200, 240),
    "thinking": (240, 180, 60),
    "speaking": (60, 210, 130),
    "muted": (200, 60, 60),
    "unpaired": (150, 150, 150),
    "error": (150, 150, 150),
}
LABELS = {
    "sleeping": "Say “Hey Jarvis”",
    "connecting": "Connecting…",
    "listening": "Listening",
    "thinking": "Thinking",
    "speaking": "Speaking",
    "muted": "Muted",
    "unpaired": "Not paired",
    "error": "Problem",
}
NOTIFY_EVERY = 60.0


def icon_image(state: str, size: int = 64) -> Any:
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    colour = COLOURS.get(state, COLOURS["error"])
    draw.ellipse((4, 4, size - 4, size - 4), fill=(*colour, 255))
    draw.ellipse((size * 0.3, size * 0.3, size * 0.7, size * 0.7), fill=(255, 255, 255, 90))
    if state == "muted":
        draw.line((10, size - 10, size - 10, 10), fill=(255, 255, 255, 255), width=6)
    return image


class Tray:
    def __init__(self, *, on_mute: Callable[[], object], on_quit: Callable[[], object]) -> None:
        import pystray

        self._on_mute = on_mute
        self._on_quit = on_quit
        self._muted = False
        self._last_note: tuple[str, float] = ("", 0.0)
        menu = pystray.Menu(
            pystray.MenuItem("Mute", self._mute, checked=lambda _item: self._muted),
            pystray.MenuItem("Quit", self._quit),
        )
        self._icon = pystray.Icon(
            "jarvis-satellite", icon_image("sleeping"), "Jarvis: " + LABELS["sleeping"], menu
        )

    def show(self, state: str, detail: str | None = None) -> None:
        self._muted = state == "muted"
        self._icon.icon = icon_image(state)
        self._icon.title = f"Jarvis: {LABELS.get(state, state)}"[:127]
        if detail and state in ("error", "unpaired"):
            now = time.monotonic()
            if detail != self._last_note[0] or now - self._last_note[1] > NOTIFY_EVERY:
                self._last_note = (detail, now)
                try:
                    self._icon.notify(detail, "Jarvis")
                except Exception:  # notifications are a nicety
                    log.debug("couldn't show a notification", exc_info=True)

    def run(self, setup: Callable[[], None]) -> None:
        """Show the icon (blocks the main thread); `setup` runs once it's visible."""

        def ready(icon: Any) -> None:
            icon.visible = True
            setup()

        self._icon.run(setup=ready)

    def stop(self) -> None:
        self._icon.stop()

    def _mute(self, _icon: Any, _item: Any) -> None:
        self._on_mute()

    def _quit(self, _icon: Any, _item: Any) -> None:
        self._on_quit()


class ConsoleIndicator:
    """Prints state changes (when running without the tray)."""

    def __init__(self) -> None:
        self._last = ""

    def show(self, state: str, detail: str | None = None) -> None:
        line = LABELS.get(state, state) + (f": {detail}" if detail else "")
        if line != self._last:
            self._last = line
            print(f"[{time.strftime('%H:%M:%S')}] {line}", flush=True)


def start_hotkey(combo: str, callback: Callable[[], object]) -> Any:
    """A global hotkey (e.g. <ctrl>+<alt>+j) on its own thread. None if unavailable."""
    try:
        from pynput import keyboard
    except ImportError:
        log.info("no global hotkey on this system")
        return None

    def pressed() -> None:
        callback()

    try:
        listener = keyboard.GlobalHotKeys({combo: pressed})
    except ValueError as exc:
        log.warning("mute hotkey %r isn't valid: %s", combo, exc)
        return None
    listener.daemon = True
    listener.start()
    return listener
