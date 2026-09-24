from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
from typing import Any

import pytest

from jarvis.config import REPO_ROOT
from jarvis.policy.config import PolicyConfigError, QuietHours, load_policies
from tests.policy_helpers import policies


def _kind(**fields: Any) -> dict[str, Any]:
    return {"description": "x", "autonomy": "L2", "risk": "low", **fields}


def test_shipped_policies_file_is_valid() -> None:
    config = load_policies(REPO_ROOT / "config" / "policies.yaml")
    assert "email.send" in config.action_kinds
    assert config.action_kinds["email.send"].autonomy.value == "L2"


@pytest.mark.parametrize(
    ("kind", "fields", "message"),
    [
        ("payment.request", {"autonomy": "L3"}, "cannot be L3"),
        ("file.delete", {"autonomy": "L3"}, "cannot be L3"),
        ("deploy.production", {"autonomy": "L3"}, "cannot be L3"),
        ("outreach.send", {"autonomy": "L3", "risk": "high"}, "high-risk actions cannot be L3"),
        ("BadKind", {}, "must look like"),
        ("test.dupe", {"validators": ["secrets_scan", "secrets_scan"]}, "listed twice"),
    ],
)
def test_unsafe_action_policies_are_refused(
    kind: str, fields: dict[str, Any], message: str
) -> None:
    with pytest.raises(PolicyConfigError, match=message):
        policies(action_kinds={kind: _kind(**fields)})


def test_high_risk_must_require_passkey() -> None:
    channels = {
        "low": ["pwa"],
        "medium": ["pwa"],
        "high": ["pwa", "telegram"],
        "critical": ["pwa_passkey"],
    }
    with pytest.raises(PolicyConfigError, match="exactly \\[pwa_passkey\\]"):
        policies(approval_channels=channels)


def test_every_risk_needs_channels() -> None:
    with pytest.raises(PolicyConfigError, match="at least one channel"):
        policies(approval_channels={"low": ["pwa"], "high": ["pwa_passkey"]})


def test_unknown_timezone_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown timezone"):
        policies(defaults={"timezone": "Mars/Olympus"})


def test_missing_file_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(PolicyConfigError, match="not found"):
        load_policies(tmp_path / "nope.yaml")


class TestQuietHours:
    quiet = QuietHours(start=time(21, 30), end=time(7, 0))

    @pytest.mark.parametrize(
        ("moment", "inside"),
        [(time(23, 0), True), (time(2, 0), True), (time(7, 0), False), (time(12, 0), False)],
    )
    def test_wraps_past_midnight(self, moment: time, inside: bool) -> None:
        assert self.quiet.contains(moment) is inside

    def test_next_end_rolls_to_tomorrow_after_the_start(self) -> None:
        now = datetime(2026, 1, 5, 22, 0)
        assert self.quiet.next_end(now) == datetime(2026, 1, 6, 7, 0)

    def test_next_end_is_today_in_the_small_hours(self) -> None:
        now = datetime(2026, 1, 6, 3, 0)
        assert self.quiet.next_end(now) == datetime(2026, 1, 6, 7, 0)
