"""Import the owner's own documents: a CV (PDF/text) or a LinkedIn data export.

LinkedIn exports are parsed deterministically; there is no scraping, since the
owner downloads their own archive. CV text goes to the extraction model as
untrusted content. Uploads are size-limited, and ZIPs are checked against
decompression bombs.
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass, field

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_UNZIPPED_BYTES = 50 * 1024 * 1024
_LINKEDIN_FILES = {
    "profile.csv",
    "positions.csv",
    "skills.csv",
    "languages.csv",
    "projects.csv",
    "certifications.csv",
}


class DocumentError(ValueError):
    pass


@dataclass
class LinkedInData:
    full_name: str | None = None
    headline: str | None = None
    location: str | None = None
    summary: str | None = None
    positions: list[dict[str, str]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    projects: list[dict[str, str]] = field(default_factory=list)


def _rows(raw: bytes) -> list[dict[str, str]]:
    text = raw.decode("utf-8-sig", errors="replace")
    # LinkedIn sometimes prefixes CSVs with notes; start at the header row.
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if "," in line), 0)
    return list(csv.DictReader(io.StringIO("\n".join(lines[start:]))))


def parse_linkedin_export(data: bytes) -> LinkedInData:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise DocumentError("That file isn't a valid ZIP archive.") from exc
    total = sum(info.file_size for info in archive.infolist())
    if total > MAX_UNZIPPED_BYTES:
        raise DocumentError("That archive is too large once unpacked.")
    files = {
        info.filename.rsplit("/", 1)[-1].lower(): info
        for info in archive.infolist()
        if info.filename.rsplit("/", 1)[-1].lower() in _LINKEDIN_FILES
    }
    if not files:
        raise DocumentError("This doesn't look like a LinkedIn data export (no Profile.csv).")
    result = LinkedInData()
    if "profile.csv" in files:
        rows = _rows(archive.read(files["profile.csv"]))
        if rows:
            row = rows[0]
            name = f"{row.get('First Name', '').strip()} {row.get('Last Name', '').strip()}".strip()
            result.full_name = name or None
            result.headline = row.get("Headline") or None
            result.location = row.get("Geo Location") or row.get("Location") or None
            result.summary = row.get("Summary") or None
    if "positions.csv" in files:
        result.positions = [
            {k: (v or "").strip() for k, v in row.items() if k}
            for row in _rows(archive.read(files["positions.csv"]))[:20]
        ]
    if "skills.csv" in files:
        result.skills = [
            r.get("Name", "").strip()
            for r in _rows(archive.read(files["skills.csv"]))
            if r.get("Name")
        ][:50]
    if "languages.csv" in files:
        result.languages = [
            r.get("Name", "").strip()
            for r in _rows(archive.read(files["languages.csv"]))
            if r.get("Name")
        ]
    if "projects.csv" in files:
        result.projects = [
            {k: (v or "").strip() for k, v in row.items() if k}
            for row in _rows(archive.read(files["projects.csv"]))[:10]
        ]
    return result


def linkedin_suggestions(data: LinkedInData) -> dict[str, object]:
    suggestions: dict[str, object] = {}
    if data.full_name:
        suggestions["identity.full_name"] = data.full_name
    if data.location:
        suggestions["identity.location"] = data.location
    if data.languages:
        suggestions["identity.languages"] = data.languages
    if data.skills:
        suggestions["engineering.also_uses"] = data.skills[:25]
    highlights = [
        {
            "title": p.get("Title", "")[:120],
            "summary": (p.get("Description") or p.get("Title", ""))[:500],
            "url": p.get("Url") or None,
        }
        for p in data.projects
        if p.get("Title")
    ]
    if highlights:
        suggestions["portfolio.highlights"] = highlights
    return suggestions


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages[:15]]
    except (PdfReadError, ValueError) as exc:
        raise DocumentError("Couldn't read that PDF.") from exc
    text = "\n".join(pages).strip()
    if not text:
        raise DocumentError("That PDF has no extractable text (is it a scanned image?).")
    return text


def document_text(filename: str, data: bytes) -> str:
    if len(data) > MAX_UPLOAD_BYTES:
        raise DocumentError("Files must be under 10 MB.")
    name = filename.lower()
    if name.endswith(".pdf"):
        return pdf_text(data)
    if name.endswith((".txt", ".md")):
        return data.decode("utf-8", errors="replace")
    raise DocumentError("Upload a PDF, a .txt/.md file, or your LinkedIn export .zip.")
