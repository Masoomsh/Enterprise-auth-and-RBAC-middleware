import asyncio
from types import SimpleNamespace

from app.authentication.supertokens import _gateway_claims_override
from app.config.settings import Settings


def _issued_payload(settings):
    captured = {}

    async def original_create_new_session(
        user_id, recipe_user_id, access_token_payload, session_data, disable_anti_csrf, tenant_id, ctx
    ):
        captured.update(access_token_payload)
        return "session"

    original = SimpleNamespace(create_new_session=original_create_new_session)
    wrapped = _gateway_claims_override(settings)(original)
    asyncio.run(wrapped.create_new_session("u", "ru", {"existing": 1}, None, None, "public", {}))
    return captured


def test_session_tokens_never_carry_the_registered_aud_claim():
    # The SuperTokens SDK verifies its own tokens without an audience; a token with `aud`
    # is rejected as "Invalid audience", which broke every session before this was fixed.
    payload = _issued_payload(Settings(_env_file=None, gateway_audience="semantic-gateway"))
    assert "aud" not in payload


def test_gateway_audience_and_tenant_are_carried_in_custom_claims():
    payload = _issued_payload(Settings(_env_file=None, gateway_audience="semantic-gateway", default_tenant="acme"))
    assert payload["gateway_aud"] == "semantic-gateway"
    assert payload["tenant"] == "acme"
    assert payload["existing"] == 1  # claims added by other recipes are preserved
