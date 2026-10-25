"""Settings sources: environment variables, and files in SECRETS_DIR (the
shape Vault Agent, Kubernetes secret volumes and cloud secret-store drivers
produce)."""

from __future__ import annotations

from pathlib import Path

import pytest

from air_model_gateway.config import Settings, unknown_keys


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Run from an empty directory so the developer's own .env can't leak in.
    monkeypatch.chdir(tmp_path)


def _secrets(tmp_path: Path, **files: str) -> Path:
    directory = tmp_path / "secrets"
    directory.mkdir()
    for name, value in files.items():
        (directory / name).write_text(value + "\n")
    return directory


def test_secrets_dir_files_are_read_like_env_vars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = _secrets(
        tmp_path,
        AIR_MODEL_GATEWAY_PROVIDERS__ANTHROPIC_API_KEY="sk-file",
        AIR_MODEL_GATEWAY_SECURITY__SERVICE_TOKENS="t1:svc-a,t2:svc-b",
        AIR_MODEL_GATEWAY_REDIS__URL="redis://:pw@redis:6379/0",
    )
    (directory / "..data").mkdir()  # Kubernetes volume bookkeeping, ignored
    monkeypatch.setenv("AIR_MODEL_GATEWAY_SECRETS_DIR", str(directory))

    s = Settings()

    assert s.providers.anthropic_api_key == "sk-file"  # trailing newline stripped
    assert s.security.service_tokens == {"t1": "svc-a", "t2": "svc-b"}
    assert s.redis.url == "redis://:pw@redis:6379/0"


def test_an_env_var_beats_a_secrets_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    directory = _secrets(tmp_path, AIR_MODEL_GATEWAY_PROVIDERS__GEMINI_API_KEY="from-file")
    monkeypatch.setenv("AIR_MODEL_GATEWAY_SECRETS_DIR", str(directory))
    monkeypatch.setenv("AIR_MODEL_GATEWAY_PROVIDERS__GEMINI_API_KEY", "from-env")
    assert Settings().providers.gemini_api_key == "from-env"


def test_a_missing_secrets_dir_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIR_MODEL_GATEWAY_SECRETS_DIR", "/nonexistent/secrets")
    assert Settings().providers.openai_api_key == ""


def test_redis_is_off_by_default() -> None:
    assert Settings().redis.url == ""


def test_unknown_keys_names_retired_and_misspelt_settings_only(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AIR_MODEL_GATEWAY_GATEWAY__DEFAULT_MODEL=llama3.2:3b\n"
        "AIR_MODEL_GATEWAY_OLLAMA__BASE_URL=http://localhost:11434\n"
    )
    environ = {
        "AIR_MODEL_GATEWAY_PROVIDERS__GEMINI_KEY": "secret-value",  # misspelt
        "AIR_MODEL_GATEWAY_SECRETS_DIR": "/run/secrets",  # not a setting, but known
        "AIR_MODEL_GATEWAY_ENVIRONMENT": "local",
        "PATH": "/usr/bin",
    }
    unknown = unknown_keys(environ, env_file)
    assert unknown == [
        "AIR_MODEL_GATEWAY_GATEWAY__DEFAULT_MODEL",
        "AIR_MODEL_GATEWAY_PROVIDERS__GEMINI_KEY",
    ]
    assert "secret-value" not in str(unknown)  # names only, never values
