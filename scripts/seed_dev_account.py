import asyncio

from supertokens_python.recipe.emailpassword.asyncio import sign_up, verify_credentials
from supertokens_python.recipe.emailpassword.interfaces import (
    EmailAlreadyExistsError,
    SignInOkResult,
    SignUpOkResult,
    WrongCredentialsError,
)

from app.authentication.supertokens import configure_supertokens
from app.config.settings import Settings


def get_seed_credentials(settings: Settings) -> tuple[str, str]:
    if settings.app_env != "development":
        raise RuntimeError(
            "Development account seeding is disabled. Set APP_ENV=development "
            "for a local development environment."
        )

    missing = []
    if not settings.dev_seed_email or not settings.dev_seed_email.strip():
        missing.append("DEV_SEED_EMAIL")
    if (
        settings.dev_seed_password is None
        or not settings.dev_seed_password.get_secret_value()
    ):
        missing.append("DEV_SEED_PASSWORD")
    if missing:
        raise RuntimeError(
            "Missing required development seed configuration: "
            + ", ".join(missing)
            + ". Set these values in the local .env file."
        )

    return (
        settings.dev_seed_email.strip(),
        settings.dev_seed_password.get_secret_value(),
    )


async def seed_account(email: str, password: str) -> str:
    result = await sign_up("public", email, password)
    if isinstance(result, SignUpOkResult):
        return "Development seed account created."

    if isinstance(result, EmailAlreadyExistsError):
        verification = await verify_credentials("public", email, password)
        if isinstance(verification, SignInOkResult):
            return "Development seed account already exists and matches the configured password."
        if isinstance(verification, WrongCredentialsError):
            raise RuntimeError(
                "The account already exists with a different password; no changes were made."
            )

    raise RuntimeError(
        f"Unable to seed development account: {type(result).__name__}."
    )


def main() -> None:
    settings = Settings()
    email, password = get_seed_credentials(settings)
    settings.validate_supertokens_configuration()
    configure_supertokens()
    print(asyncio.run(seed_account(email, password)))


if __name__ == "__main__":
    main()

