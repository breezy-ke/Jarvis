"""Canonical JSON and hashing.

Approvals and the audit chain both depend on hashing *exactly* the same bytes
every time. Canonical JSON means sorted keys, no insignificant whitespace, and
UTF-8 output. Only JSON-native types are accepted: anything else (datetimes,
sets, NaN) is a programming error, so it raises instead of being coerced.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

GENESIS_HASH = "0" * 64


class NonCanonicalValueError(TypeError):
    pass


def _check(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, bool | str):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NonCanonicalValueError(f"non-finite number at {path}")
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _check(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise NonCanonicalValueError(f"non-string key at {path}")
            _check(item, f"{path}.{key}")
        return
    raise NonCanonicalValueError(f"unsupported type {type(value).__name__} at {path}")


def canonical_json(value: Any) -> str:
    _check(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: str | bytes) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def payload_digest(kind: str, payload: dict[str, Any]) -> tuple[str, str]:
    """Return (canonical_text, sha256) for an action. The kind is part of the hash."""
    canonical = canonical_json({"kind": kind, "payload": payload})
    return canonical, sha256_hex(canonical)
