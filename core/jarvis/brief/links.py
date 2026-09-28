"""Links as the brief shows them, and the key that tells two copies of a story apart."""

from __future__ import annotations

import re
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

# Parameters that say where you came from, not what you're reading.
_TRACKING = re.compile(
    r"^(utm_\w+|fbclid|gclid|dclid|msclkid|yclid|mc_cid|mc_eid|mkt_tok|igshid|si|ref|ref_src|"
    r"ref_url|cmpid|ncid|sr_share|guccounter|_hsenc|_hsmi|vero_id|oly_anon_id|oly_enc_id|"
    r"at_medium|at_campaign|rss|source|src|smid|share)$",
    re.IGNORECASE,
)


def clean_url(url: str) -> str:
    """The link to show: tracking parameters and the #fragment removed."""
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return url.strip()
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)
    ]
    return urlunsplit(
        (parts.scheme.lower(), _netloc(parts), parts.path or "/", urlencode(query), "")
    )


def _netloc(parts: SplitResult) -> str:
    """Host and port only: a "user@" part can make a link look like another site's."""
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"  # IPv6
    try:
        port = parts.port
    except ValueError:
        return parts.netloc.lower().rpartition("@")[2]
    return f"{host}:{port}" if port else host


def link_key(url: str) -> str:
    """The same story at a slightly different address gets the same key."""
    parts = urlsplit(clean_url(url))
    host = (parts.hostname or "").removeprefix("www.").removeprefix("m.")
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/"
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return f"{host}{path}" + (f"?{query}" if query else "")
