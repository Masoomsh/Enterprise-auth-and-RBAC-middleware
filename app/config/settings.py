from pathlib import Path
from urllib.parse import urlsplit

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str | None = None
    supertokens_app_name: str = "Enterprise Auth RBAC"
    supertokens_api_domain: str | None = None
    supertokens_website_domain: str | None = None
    supertokens_connection_uri: str | None = None
    supertokens_api_key: SecretStr | None = None
    # Audience and tenant claims added to access tokens for the gateway to verify.
    gateway_audience: str = "semantic-gateway"
    default_tenant: str = "default"
    # --- OAuth2/OIDC bearer tokens from the corporate IdP (provider disabled if unset) ---
    oauth2_issuer: str | None = None
    oauth2_audience: str | None = None
    oauth2_jwks_uri: str | None = None
    oauth2_group_claim: str = "groups"
    # JSON object mapping IdP group -> gateway role, e.g. {"AI-Platform-Admins": "admin"}.
    oauth2_role_map: str = "{}"
    # --- API keys ---
    # Server-side secret for HMAC-hashing API key secrets. Required to create/verify keys.
    api_key_hmac_secret: SecretStr | None = None
    # False = audit-only rollout: denials are logged as would-be denies but not enforced.
    auth_enforce: bool = True
    dev_seed_email: str | None = None
    dev_seed_password: SecretStr | None = None

    def validate_supertokens_configuration(self) -> None:
        required = {
            "SUPERTOKENS_API_DOMAIN": self.supertokens_api_domain,
            "SUPERTOKENS_WEBSITE_DOMAIN": self.supertokens_website_domain,
            "SUPERTOKENS_CONNECTION_URI": self.supertokens_connection_uri,
            "SUPERTOKENS_API_KEY": (
                self.supertokens_api_key.get_secret_value()
                if self.supertokens_api_key
                else None
            ),
        }
        missing = [name for name, value in required.items() if not value or not value.strip()]
        if missing:
            raise RuntimeError(
                "Missing required authentication configuration: "
                + ", ".join(missing)
                + ". Set these values in the local .env file."
            )

        for name, value in (
            ("SUPERTOKENS_API_DOMAIN", self.supertokens_api_domain),
            ("SUPERTOKENS_WEBSITE_DOMAIN", self.supertokens_website_domain),
        ):
            parsed = urlsplit(value or "")
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"{name} must be an absolute http(s) URL.")
            if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                raise ValueError(f"{name} must not include a path, query, or fragment.")

        for name, value in (("SUPERTOKENS_CONNECTION_URI", self.supertokens_connection_uri),):
            parsed = urlsplit(value or "")
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"{name} must be an absolute http(s) URL.")


def get_settings() -> Settings:
    settings = Settings()
    settings.validate_supertokens_configuration()
    return settings
