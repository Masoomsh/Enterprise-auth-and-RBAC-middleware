import pytest
from pydantic import SecretStr

from app.config.settings import Settings
from scripts.seed_dev_account import get_seed_credentials


def test_seed_requires_explicit_development_environment():
    settings = Settings(
        _env_file=None,
        app_env="production",
        dev_seed_email="developer@example.test",
        dev_seed_password=SecretStr("local-test-password"),
    )

    with pytest.raises(RuntimeError, match="seeding is disabled"):
        get_seed_credentials(settings)


def test_seed_requires_credentials_without_exposing_password():
    settings = Settings(
        _env_file=None,
        app_env="development",
        dev_seed_email="developer@example.test",
        dev_seed_password=SecretStr(""),
    )

    with pytest.raises(RuntimeError) as error:
        get_seed_credentials(settings)

    assert "DEV_SEED_PASSWORD" in str(error.value)
    assert "local-test-password" not in str(error.value)


def test_seed_reads_credentials_from_settings():
    settings = Settings(
        _env_file=None,
        app_env="development",
        dev_seed_email=" developer@example.test ",
        dev_seed_password=SecretStr("local-test-password"),
    )

    assert get_seed_credentials(settings) == (
        "developer@example.test",
        "local-test-password",
    )
