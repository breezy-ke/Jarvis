"""The setup helpers: `jarvis secrets`, `jarvis local-models` and `jarvis doctor`."""

from __future__ import annotations

import base64
import stat
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import ec

from jarvis import doctor
from jarvis.cli import fill_env_file, generate_secrets, local_model_ids, main
from jarvis.config import REPO_ROOT, Settings
from jarvis.llm.config import parse_models_config
from jarvis.voice.speech import SpeechStatus

REPO_CONFIG = REPO_ROOT / "config"


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def test_generated_secrets_are_valid() -> None:
    values = generate_secrets()
    Fernet(values["JARVIS_SECRET_KEY"].encode())  # raises if malformed
    private = _b64url_decode(values["VAPID_PRIVATE_KEY"])
    public = _b64url_decode(values["VAPID_PUBLIC_KEY"])
    assert len(private) == 32
    assert len(public) == 65
    assert public[0] == 4  # uncompressed P-256 point, as browsers expect
    derived = ec.derive_private_key(int.from_bytes(private, "big"), ec.SECP256R1())
    assert derived.public_key().public_numbers().x.to_bytes(32, "big") == public[1:33]
    assert len(values["POSTGRES_PASSWORD"]) >= 32
    assert set(values["POSTGRES_PASSWORD"]) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )
    assert len(values["SEARXNG_SECRET"]) == 64
    assert generate_secrets() != values


def test_fill_env_file_copies_the_example_and_fills_only_blanks(tmp_path: Path) -> None:
    example = tmp_path / ".env.example"
    example.write_text(
        "JARVIS_ENV=production   # comment\n"
        "JARVIS_SECRET_KEY=\n"
        "POSTGRES_PASSWORD=   # generated\n"
        "VAPID_PUBLIC_KEY=\n"
        "VAPID_PRIVATE_KEY=\n"
        "SEARXNG_SECRET=\n"
        "SPEECH_API_KEY=\n"
        "GROQ_API_KEY=\n",
        encoding="utf-8",
    )
    env = tmp_path / ".env"
    filled = fill_env_file(env, example)
    assert sorted(filled) == sorted(
        [
            "JARVIS_SECRET_KEY",
            "POSTGRES_PASSWORD",
            "VAPID_PUBLIC_KEY",
            "VAPID_PRIVATE_KEY",
            "SEARXNG_SECRET",
            "SPEECH_API_KEY",
        ]
    )
    text = env.read_text(encoding="utf-8")
    assert "JARVIS_ENV=production   # comment" in text
    assert "GROQ_API_KEY=\n" in text  # never invents a key you must supply
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    first = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)

    # Running it again changes nothing: your secrets are never overwritten.
    assert fill_env_file(env, example) == []
    second = dict(
        line.split("=", 1) for line in env.read_text(encoding="utf-8").splitlines() if "=" in line
    )
    assert second == first


def test_an_older_env_gets_the_secrets_a_newer_jarvis_needs(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "JARVIS_SECRET_KEY=keep-me\nPOSTGRES_PASSWORD=mine\n# SPEECH_API_KEY=old-comment\n",
        encoding="utf-8",
    )
    filled = fill_env_file(env)
    text = env.read_text(encoding="utf-8")
    assert "JARVIS_SECRET_KEY=keep-me\n" in text  # yours are never touched
    assert "POSTGRES_PASSWORD=mine\n" in text
    assert "SPEECH_API_KEY" in filled
    added = dict(
        line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#")
    )
    assert len(added["SPEECH_API_KEY"]) >= 40
    assert fill_env_file(env) == []  # and only once


def test_fill_env_file_needs_an_example(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        fill_env_file(tmp_path / ".env", tmp_path / "missing.example")


def test_the_shipped_env_example_gets_every_generated_secret(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    filled = fill_env_file(env, REPO_ROOT / ".env.example")
    assert sorted(filled) == sorted(generate_secrets())


def test_local_model_ids_lists_ollama_models_once(tmp_path: Path) -> None:
    config = tmp_path / "models.yaml"
    config.write_text(
        """
version: 1
providers:
  ollama: {kind: ollama, local: true, trains_on_data: false, zero_data_retention: true}
  groq: {kind: groq, api_key_env: GROQ_API_KEY, trains_on_data: false}
models:
  a: {provider: ollama, model: "qwen3:8b"}
  b: {provider: groq, model: some-cloud-model}
  c: {provider: ollama, model: "qwen3:8b"}
  d: {provider: ollama, model: "gemma3:4b"}
tasks:
  chat: {privacy: personal, candidates: [a, b]}
""",
        encoding="utf-8",
    )
    assert local_model_ids(config) == ["qwen3:8b", "gemma3:4b"]


def test_local_models_command_reads_the_repo_config(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVIS_CONFIG_DIR", str(REPO_CONFIG))
    monkeypatch.delenv("JARVIS_MODELS_FILE", raising=False)
    from jarvis.config import get_settings

    get_settings.cache_clear()
    try:
        assert main(["local-models"]) == 0
    finally:
        get_settings.cache_clear()
    printed = capsys.readouterr().out.split()
    assert printed == local_model_ids(REPO_CONFIG / "models.yaml")
    assert printed  # the default config always has a private local model


@pytest.mark.parametrize(
    ("vram", "model"),
    [
        (6, "qwen3:4b"),
        (9.9, "qwen3:4b"),
        (12, "qwen3:8b"),
        (16, "qwen3:14b"),
        (24, "qwen3:30b-a3b"),
    ],
)
def test_local_model_fits_the_gpu(vram: float, model: str) -> None:
    assert doctor.local_model_for_vram(vram) == model


def test_vram_can_be_passed_in_from_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    monkeypatch.setenv("JARVIS_GPU_VRAM_GB", "12.0")
    assert doctor.gpu_vram_gb() == 12.0
    for unusable in ("", "0", "not-a-number"):
        monkeypatch.setenv("JARVIS_GPU_VRAM_GB", unusable)
        assert doctor.gpu_vram_gb() is None


def test_repo_configs_pass_the_doctor() -> None:
    settings = Settings(JARVIS_CONFIG_DIR=REPO_CONFIG)
    checks, models = doctor.check_configs(settings)
    assert models is not None
    assert [c.status for c in checks] == [doctor.OK, doctor.OK]


def test_doctor_flags_a_training_provider_allowed_personal_data() -> None:
    models = parse_models_config(
        {
            "version": 1,
            "providers": {
                "gemini": {
                    "kind": "google",
                    "api_key_env": "GEMINI_API_KEY",
                    "trains_on_data": False,  # wrong: the free tier trains on data
                    "zero_data_retention": False,
                }
            },
            "models": {"g": {"provider": "gemini", "model": "gemini-flash-latest"}},
            "tasks": {"chat": {"privacy": "public", "candidates": ["g"]}},
        }
    )
    checks = doctor.check_privacy(models, {"GEMINI_API_KEY": "x"})
    assert any(c.status == doctor.FAIL and c.title == "Gemini free tier" for c in checks)


def test_doctor_warns_when_a_task_has_no_usable_model() -> None:
    models = parse_models_config(
        {
            "version": 1,
            "providers": {
                "groq": {"kind": "groq", "api_key_env": "GROQ_API_KEY", "trains_on_data": False}
            },
            "models": {"g": {"provider": "groq", "model": "m"}},
            "tasks": {"chat": {"privacy": "personal", "candidates": ["g"]}},
        }
    )
    [check] = doctor.check_privacy(models, {})
    assert check.status == doctor.WARN
    assert "GROQ_API_KEY" in check.fix


def test_render_shows_fixes_only_for_problems() -> None:
    out = doctor.render(
        [
            doctor.Check(doctor.OK, "Database", "fine", "never shown"),
            doctor.Check(doctor.FAIL, "Encryption key", "missing", "Run `make secrets`."),
        ]
    )
    assert "never shown" not in out
    assert "fix: Run `make secrets`." in out


def test_task_fixes_name_only_what_that_task_may_use() -> None:
    from jarvis.llm.config import load_models_config

    models = load_models_config(REPO_CONFIG / "models.yaml")
    checks = {c.title: c for c in doctor.check_privacy(models, {})}
    public = checks["Task 'public_summarize' (public)"]
    assert public.status == doctor.WARN
    assert "GEMINI_API_KEY" in public.fix
    assert "GROQ_API_KEY" in public.fix
    assert "Ollama" not in public.fix  # the public task has no local candidate
    # Personal tasks fall back to the private local model, so they're usable.
    assert checks["Task 'chat' (personal)"].status == doctor.OK


def test_bench_voice_explains_what_it_needs(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from jarvis.config import get_settings

    get_settings.cache_clear()
    try:
        assert main(["bench-voice", "--runs", "0"]) == 1  # the default question, asked 0 times
        assert "Ask at least once" in capsys.readouterr().out
        assert main(["bench-voice", "--audio", str(tmp_path / "missing.wav")]) == 1
        assert "Can't read" in capsys.readouterr().out
    finally:
        get_settings.cache_clear()


# --- Voice and Telegram ------------------------------------------------------------------

SPEECH_KEY = "k" * 43
TELEGRAM_TOKEN = "123456789:" + "A" * 35


class StubSpeech:
    """Stands in for the doctor's SpeechClient: reports a fixed status."""

    def __init__(self, status: SpeechStatus) -> None:
        self._status = status
        self.api_key: str | None = None

    def __call__(self, base_url: str, config: object, *, api_key: str | None = None) -> StubSpeech:
        self.api_key = api_key
        return self

    async def status(self) -> SpeechStatus:
        return self._status

    async def aclose(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _no_real_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SPEECH_API_KEY", "TELEGRAM_BOT_TOKEN", "JARVIS_VOICE_FILE"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("status", "outcome", "fix"),
    [
        (SpeechStatus(True, True, True, "ready"), doctor.OK, ""),
        (SpeechStatus(True, True, False, "not downloaded: kokoro"), doctor.WARN, "pull-models"),
        (
            SpeechStatus(True, False, False, "it refused SPEECH_API_KEY", key_refused=True),
            doctor.FAIL,
            "make up",
        ),
        (SpeechStatus(False, False, False, "unreachable (ConnectError)"), doctor.WARN, "make up"),
    ],
    ids=["ready", "models missing", "key refused", "down"],
)
async def test_doctor_checks_the_speech_server(
    monkeypatch: pytest.MonkeyPatch, status: SpeechStatus, outcome: str, fix: str
) -> None:
    speech = StubSpeech(status)
    monkeypatch.setattr(doctor, "SpeechClient", speech)
    monkeypatch.setattr(doctor, "punkt_available", lambda: True)
    settings = Settings(JARVIS_CONFIG_DIR=REPO_CONFIG, SPEECH_API_KEY=SPEECH_KEY)
    checks = {c.title: c for c in await doctor.check_voice(settings)}
    assert checks["voice.yaml"].status == doctor.OK
    assert checks["Voice sentence data"].status == doctor.OK
    assert "Speech server key" not in checks
    server = checks.get("Speech server") or checks["Speech models"]
    assert server.status == outcome
    assert fix in server.fix
    assert speech.api_key == SPEECH_KEY  # it asks with the key
    assert SPEECH_KEY not in doctor.render(list(checks.values()))


async def test_doctor_explains_missing_voice_pieces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(doctor, "SpeechClient", StubSpeech(SpeechStatus(True, True, True, "ok")))
    monkeypatch.setattr(doctor, "punkt_available", lambda: False)
    checks = {c.title: c for c in await doctor.check_voice(Settings(JARVIS_CONFIG_DIR=REPO_CONFIG))}
    assert checks["Speech server key"].status == doctor.WARN
    assert "make secrets" in checks["Speech server key"].fix
    assert checks["Voice sentence data"].status == doctor.WARN
    assert "make restart" in checks["Voice sentence data"].fix

    broken = tmp_path / "voice.yaml"
    broken.write_text("version: 1\nspeech: {}\n", encoding="utf-8")
    settings = Settings(JARVIS_CONFIG_DIR=REPO_CONFIG, JARVIS_VOICE_FILE=broken)
    [check] = await doctor.check_voice(settings)
    assert (check.status, check.title) == (doctor.FAIL, "voice.yaml")


def _telegram(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_doctor_checks_the_telegram_bot() -> None:
    asked: list[str] = []

    def get_me(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "result": {"username": "jarvis_test_bot"}})

    configured = Settings(TELEGRAM_BOT_TOKEN=TELEGRAM_TOKEN)
    async with _telegram(get_me) as client:
        unset = await doctor.check_telegram(Settings(), client, online=True)
        offline = await doctor.check_telegram(configured, client, online=False)
        assert asked == []  # neither asks Telegram anything
        online = await doctor.check_telegram(configured, client, online=True)
        malformed = await doctor.check_telegram(
            Settings(TELEGRAM_BOT_TOKEN="not-a-token"), client, online=True
        )
    assert (unset.status, offline.status, online.status) == (doctor.OK, doctor.OK, doctor.OK)
    assert "not set up" in unset.detail
    assert "ONLINE=1" in offline.detail
    assert "@jarvis_test_bot" in online.detail
    assert asked == [f"/bot{TELEGRAM_TOKEN}/getMe"]
    assert malformed.status == doctor.FAIL
    assert "@BotFather" in malformed.fix


async def test_doctor_reports_telegram_problems_without_showing_the_token() -> None:
    def unauthorized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"no route to {request.url}")

    configured = Settings(TELEGRAM_BOT_TOKEN=TELEGRAM_TOKEN)
    async with _telegram(unauthorized) as client:
        rejected = await doctor.check_telegram(configured, client, online=True)
    async with _telegram(offline) as client:
        unreachable = await doctor.check_telegram(configured, client, online=True)
    assert rejected.status == doctor.FAIL
    assert unreachable.status == doctor.WARN
    assert TELEGRAM_TOKEN.split(":")[1] not in doctor.render([rejected, unreachable])
