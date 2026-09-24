from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from jarvis.ingestion import documents, github, google, style


class TestStyle:
    emails = [
        "Hi Achieng,\n\nThanks for the brief. I'll send the proposal tomorrow.\n\nBest regards,\nBrian",
        "Hello team,\n\nThe staging site is live. Let me know what you think.\n\nBest regards\nBrian\n"
        "On Mon, Jan 5, 2026 at 9:00 AM Someone <a@b.co> wrote:\n> old quoted text",
        "Hi James,\n\nHabari! Asante for the quick reply, sawa, we'll start Monday.\n\nCheers,\nBrian",
    ]

    def test_learns_greeting_sign_off_and_register(self) -> None:
        profile = style.analyze(self.emails)
        assert profile.emails_analyzed == 3
        assert profile.greetings[0] == "Hi"
        assert profile.sign_offs[0] == "Best Regards"
        assert profile.uses_swahili

    def test_quoted_replies_are_ignored(self) -> None:
        assert "old quoted text" not in style.strip_quoted(self.emails[1])

    def test_suggestions(self) -> None:
        suggestions = style.to_suggestions(style.analyze(self.emails))
        assert suggestions["communication.sign_off"] == "Best Regards"
        assert suggestions["communication.languages"] == ["English", "Swahili"]

    def test_no_emails_means_no_suggestions(self) -> None:
        assert style.to_suggestions(style.analyze([])) == {}


class TestGitHub:
    def test_manifest_detection(self) -> None:
        findings = github.GitHubFindings(repos_scanned=2)
        github.analyze_manifest(
            "package.json",
            '{"dependencies":{"next":"15","react":"19"},"devDependencies":{"typescript":"5",'
            '"eslint":"9","vitest":"2"},"packageManager":"pnpm@10.1.0"}',
            findings,
        )
        github.analyze_manifest(
            "composer.json", '{"require":{"laravel/framework":"^11"}}', findings
        )
        github.analyze_manifest("tsconfig.json", '{"compilerOptions":{"strict":true}}', findings)
        github.analyze_manifest("pubspec.yaml", "flutter:\n  sdk: flutter", findings)
        assert {"Next.js", "React", "Laravel", "Flutter"} <= set(findings.frameworks)
        assert {"TypeScript", "ESLint", "Vitest", "TypeScript strict mode"} <= set(
            findings.conventions
        )
        assert findings.package_managers["pnpm"] == 1

    def test_bad_json_is_ignored(self) -> None:
        findings = github.GitHubFindings()
        github.analyze_manifest("package.json", "{not json", findings)
        assert not findings.frameworks


class TestLinkedIn:
    def _zip(self, files: dict[str, str]) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        return buffer.getvalue()

    def test_parses_an_export(self) -> None:
        data = self._zip(
            {
                "Profile.csv": "First Name,Last Name,Headline,Geo Location\nBrian,Otieno,Founder,Nairobi\n",
                "Skills.csv": "Name\nNext.js\nLaravel\n",
                "Languages.csv": "Name,Proficiency\nEnglish,Native\nSwahili,Native\n",
                "Projects.csv": "Title,Description,Url\nClinic site,Bookings,https://x.co\n",
            }
        )
        parsed = documents.parse_linkedin_export(data)
        suggestions = documents.linkedin_suggestions(parsed)
        assert suggestions["identity.full_name"] == "Brian Otieno"
        assert suggestions["engineering.also_uses"] == ["Next.js", "Laravel"]
        assert suggestions["portfolio.highlights"][0]["title"] == "Clinic site"  # type: ignore[index]

    def test_rejects_non_exports_and_bombs(self) -> None:
        with pytest.raises(documents.DocumentError, match="LinkedIn"):
            documents.parse_linkedin_export(self._zip({"notes.txt": "hi"}))
        with pytest.raises(documents.DocumentError, match="valid ZIP"):
            documents.parse_linkedin_export(b"not a zip")
        big = self._zip({"Profile.csv": "x" * (documents.MAX_UNZIPPED_BYTES + 1)})
        with pytest.raises(documents.DocumentError, match="too large"):
            documents.parse_linkedin_export(big)

    def test_document_types(self) -> None:
        assert documents.document_text("cv.txt", b"Senior developer") == "Senior developer"
        with pytest.raises(documents.DocumentError):
            documents.document_text("cv.exe", b"MZ")
        with pytest.raises(documents.DocumentError, match="10 MB"):
            documents.document_text("cv.txt", b"x" * (documents.MAX_UPLOAD_BYTES + 1))


class TestGoogleParsing:
    def test_message_text_prefers_plain_parts(self) -> None:
        import base64

        encoded = base64.urlsafe_b64encode(b"Plain body").decode().rstrip("=")
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": encoded}},
                {"mimeType": "text/html", "body": {"data": encoded}},
            ],
        }
        assert google.message_text(payload) == "Plain body"

    def test_calendar_suggests_hours_and_routines(self) -> None:
        tz = ZoneInfo("Africa/Nairobi")
        base = datetime(2026, 1, 5, 6, 0, tzinfo=UTC)  # Monday 09:00 Nairobi
        events = []
        for day in range(10):
            start = base + timedelta(days=day % 5)
            events.append(
                {
                    "start": {"dateTime": start.isoformat()},
                    "end": {"dateTime": (start + timedelta(hours=8)).isoformat()},
                    "summary": "Client standup",
                    "recurringEventId": "abc",
                }
            )
        suggestions = google.calendar_suggestions(events, tz)
        assert suggestions["schedule.working_hours"] == "Mon-Fri 09:00-17:00"
        assert suggestions["schedule.routines"] == ["Client standup"]

    def test_too_few_events_say_nothing(self) -> None:
        assert google.calendar_suggestions([], ZoneInfo("UTC")) == {}
