"""Sentence-splitting data the voice pipeline needs (NLTK `punkt_tab`).

Pipecat splits Jarvis's replies into sentences so speech starts before the
whole answer is written. That needs NLTK's `punkt_tab` tables, which aren't
shipped with the package. We fetch a pinned copy at build time (Docker image,
CI, `make install`) and verify its checksum, instead of letting the first
voice turn download it.
"""

from __future__ import annotations

import hashlib
import io
import os
import zipfile
from pathlib import Path

import httpx

PUNKT_URL = (
    "https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/tokenizers/punkt_tab.zip"
)
# The sha256 NLTK publishes for this file in its data index.
PUNKT_SHA256 = "e57f64187974277726a3417ca6f181ec5403676c717672eef6a748a7b20e0106"


def punkt_installed(nltk_data: Path) -> bool:
    return (nltk_data / "tokenizers" / "punkt_tab" / "english").is_dir()


def install_punkt(archive: bytes, nltk_data: Path) -> None:
    digest = hashlib.sha256(archive).hexdigest()
    if digest != PUNKT_SHA256:
        raise ValueError(f"punkt_tab checksum mismatch: got {digest}")
    target = nltk_data / "tokenizers"
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        for member in zf.namelist():
            path = (target / member).resolve()
            if not path.is_relative_to(target.resolve()):
                raise ValueError(f"unsafe path in archive: {member}")
        zf.extractall(target)  # paths checked above


def fetch_punkt(nltk_data: Path, *, client: httpx.Client | None = None) -> bool:
    """Download and install punkt_tab into `nltk_data`. False if already there."""
    if punkt_installed(nltk_data):
        return False
    http = client or httpx.Client(timeout=120, follow_redirects=True)
    try:
        response = http.get(PUNKT_URL)
        response.raise_for_status()
        install_punkt(response.content, nltk_data)
    finally:
        if client is None:
            http.close()
    return True


def nltk_data_dir() -> Path:
    """Where NLTK looks first: the first folder in $NLTK_DATA, else ~/nltk_data."""
    configured = os.environ.get("NLTK_DATA", "").split(os.pathsep)[0].strip()
    return Path(configured) if configured else Path.home() / "nltk_data"


def punkt_available() -> bool:
    """True if NLTK can find punkt_tab anywhere on its search path."""
    import nltk

    try:
        nltk.data.find("tokenizers/punkt_tab/english/")
    except LookupError:
        return False
    return True


def ensure_punkt() -> str | None:
    """Fetch punkt_tab if NLTK can't find it. Returns what's wrong, or None if ready."""
    if punkt_available():
        return None
    try:
        fetch_punkt(nltk_data_dir())
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return (
            f"Voice is missing its sentence data and couldn't download it "
            f"({type(exc).__name__}). Run `make update` (or `jarvis fetch-text-data`) "
            "with internet access."
        )
    if not punkt_available():
        return "Voice sentence data was downloaded but NLTK can't find it. Check NLTK_DATA."
    return None
