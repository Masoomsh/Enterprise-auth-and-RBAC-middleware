import pytest

from app.config.settings import Settings


def test_supertokens_settings_report_missing_names_without_values(monkeypatch):
    for name in (
        "SUPERTOKENS_API_DOMAIN",
        "SUPERTOKENS_WEBSITE_DOMAIN",
        "SUPERTOKENS_CONNECTION_URI",
        "SUPERTOKENS_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError) as error:
        settings = Settings(_env_file=None)
        settings.validate_supertokens_configuration()

    assert "SUPERTOKENS_API_KEY" in str(error.value)
    assert "secret" not in str(error.value).lower()


def test_supertokens_settings_accept_valid_local_configuration(monkeypatch):
    values = {
        "SUPERTOKENS_API_DOMAIN": "http://localhost:8000",
        "SUPERTOKENS_WEBSITE_DOMAIN": "http://localhost:3000",
        "SUPERTOKENS_CONNECTION_URI": "http://localhost:3567",
        "SUPERTOKENS_API_KEY": "test-key-not-a-real-secret",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    settings = Settings(_env_file=None)
    settings.validate_supertokens_configuration()

    assert settings.supertokens_app_name == "Enterprise Auth RBAC"
