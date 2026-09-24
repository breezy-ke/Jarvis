"""The setup helpers: `jarvis secrets`, `jarvis local-models` and `jarvis doctor`."""

from __future__ import annotations

import base64
import stat
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import ec

from jarvis import doctor
from jarvis.cli import fill_env_file, generate_secrets, local_model_ids, main
from jarvis.config import REPO_ROOT, Settings
from jarvis.llm.config import parse_models_config

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
