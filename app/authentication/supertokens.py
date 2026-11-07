from typing import Any, Dict, Optional

from supertokens_python import InputAppInfo, SupertokensConfig, get_all_cors_headers, init
from supertokens_python.recipe import emailpassword, jwt, session, userroles
from supertokens_python.recipe.session.interfaces import RecipeInterface as SessionRecipeInterface
from supertokens_python.types import RecipeUserId

from app.config.settings import Settings, get_settings


def _gateway_claims_override(settings: Settings):
    """Add the `gateway_aud` and `tenant` claims the gateway verifies to every new access token.

    Do NOT use the registered `aud` claim here: the SuperTokens SDK verifies its own access tokens
    without an expected audience, so PyJWT rejects any token that contains `aud` ("Invalid
    audience") and every session would fail. `gateway_aud` carries the same information safely.
    """

    def override(original: SessionRecipeInterface) -> SessionRecipeInterface:
        original_create_new_session = original.create_new_session

        async def create_new_session(
            user_id: str,
            recipe_user_id: RecipeUserId,
            access_token_payload: Optional[Dict[str, Any]],
            session_data_in_database: Optional[Dict[str, Any]],
            disable_anti_csrf: Optional[bool],
            tenant_id: str,
            user_context: Dict[str, Any],
        ):
            payload = {
                **(access_token_payload or {}),
                "gateway_aud": settings.gateway_audience,
                "tenant": settings.default_tenant,
            }
            return await original_create_new_session(
                user_id,
                recipe_user_id,
                payload,
                session_data_in_database,
                disable_anti_csrf,
                tenant_id,
                user_context,
            )

        original.create_new_session = create_new_session
        return original

    return override


def configure_supertokens() -> Settings:
    settings = get_settings()

    api_key = (
        settings.supertokens_api_key.get_secret_value()
        if settings.supertokens_api_key
        else None
    )

    init(
        app_info=InputAppInfo(
            app_name=settings.supertokens_app_name,
            api_domain=settings.supertokens_api_domain or "",
            website_domain=settings.supertokens_website_domain,
        ),
        framework="fastapi",
        supertokens_config=SupertokensConfig(
            connection_uri=settings.supertokens_connection_uri or "",
            api_key=api_key,
        ),
        recipe_list=[
            emailpassword.init(),
            # Roles are written into the access token as the `st-role` claim.
            userroles.init(),
            # Publishes the signing keys at <api_domain>/auth/jwt/jwks.json for the gateway.
            jwt.init(),
            session.init(
                override=session.InputOverrideConfig(
                    functions=_gateway_claims_override(settings)
                )
            ),
        ],
        mode="asgi",
    )
    return settings


def get_supertokens_cors_headers() -> list[str]:
    return ["Content-Type", *get_all_cors_headers()]
