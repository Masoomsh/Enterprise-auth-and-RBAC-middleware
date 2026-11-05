# Threat model (STRIDE)

Scope: the gateway's authentication, authorization and audit layer. Test names refer to files in
`tests/`. "Mitigated" means a mitigation exists in code **and** a test covers it.

## Assets

Model access (cost and data exposure), tool and data-source access, API keys and their secrets,
role assignments, the audit trail.

## STRIDE

| # | Threat | Category | Mitigation | Test |
|---|---|---|---|---|
| 1 | Forged or tampered IdP token | Spoofing | Signature verified against JWKS; `exp`/`iss`/`aud`/`sub` required | `test_auth.py::test_token_signed_by_wrong_key_rejected`, `test_bad_claims_rejected` |
| 2 | `alg=none` / HS256 key-confusion token | Spoofing | Algorithm allow-list `RS256`, `ES256` only | `test_alg_none_rejected`, `test_hs256_signed_with_public_key_rejected` |
| 3 | Guessed or stolen API key secret | Spoofing | 256-bit secret; only HMAC stored; constant-time compare; same cost for unknown prefix | `test_api_key.py::test_wrong_secret_rejected`, `test_unknown_prefix_rejected`, `test_hash_is_stored_not_secret` |
| 4 | Database leak reveals usable keys | Info disclosure | Hash is keyed with a server-side secret, so a DB-only leak cannot be verified offline | `test_key_signed_with_other_server_secret_rejected` |
| 5 | Revoked / expired credential reused | Spoofing | Revocation and expiry checked on every use | `test_revoked_key_rejected`, `test_expired_key_rejected`, `test_identity_flow.py::test_users_only_see_and_revoke_their_own_keys` |
| 6 | Bad credential falls through to a weaker provider | Spoofing | First provider that sees credentials decides; failure ends the chain | `test_identity_flow.py::test_bad_credentials_stop_the_chain_and_are_not_retried` |
| 7 | Role escalation by minting keys | Elevation | Keys cannot create keys; a key may only carry roles its creator holds | `test_inference.py::test_key_cannot_create_keys`, `test_identity_flow.py::test_user_cannot_mint_a_key_with_roles_they_do_not_hold` |
| 8 | Non-admin changes roles | Elevation | `admin:roles` required on every admin route | `test_inference.py::test_non_admin_cannot_manage_roles` |
| 9 | Wildcard resource (`model:*`) matches every grant | Elevation | Request identifiers validated; wildcards rejected before and inside the engine | `test_rbac.py::test_wildcard_or_malformed_resource_denied_even_for_admin`, `test_inference.py::test_wildcards_and_traversal_rejected` |
| 10 | Scoped key used beyond its scope; scope used to widen a role | Elevation | Effective permission = role ∩ scope | `test_rbac.py::test_scopes_narrow_role_permissions`, `test_scopes_cannot_widen_role_permissions` |
| 11 | Partial authorization (allowed model, denied tool) | Elevation | All resources checked; any denial blocks the whole request | `test_inference.py::test_one_denied_resource_blocks_whole_request` |
| 12 | Unmapped IdP group grants access | Elevation | Roles come only from the explicit map | `test_auth.py::test_valid_token_maps_groups_to_roles_and_scopes` |
| 13 | Unknown role / no role gets access | Elevation | Default deny | `test_rbac.py::test_role_matrix`, `test_no_roles_denied` |
| 14 | Caller denies having made a call | Repudiation | Every request under `/v1` and `/admin` gets one audit row, including 401/403/errors | `test_audit.py::test_exactly_one_row_per_request` |
| 15 | Audit rows altered | Tampering | ORM insert-only guard (production: DB privileges, see gaps) | `test_audit.py::test_audit_rows_are_immutable` |
| 16 | Prompts or secrets leak via logs | Info disclosure | Only lengths and counts are logged; secrets never stored | `test_audit.py::test_prompt_content_never_stored`, `test_bad_credentials_are_audited_without_the_secret` |
| 17 | Listing keys leaks secrets or other users' keys | Info disclosure | Only prefix and metadata returned; lookups are owner-scoped, other users get 404 | `test_identity_flow.py::test_users_only_see_and_revoke_their_own_keys` |
| 18 | Audit outage hides activity | Repudiation | Failure never blocks silently; event is emitted on the `audit` logger | `test_audit.py::test_audit_write_failure_does_not_break_response_and_is_logged` |
| 19 | Unauthenticated probing reaches business logic | Elevation | Authentication runs before body validation and the backend | `test_inference.py::test_unauthenticated_request_gets_401_before_body_validation` |
| 20 | Oversized or malformed input | DoS | Field length and list-size limits | `test_inference.py` (422 cases) |
| 21 | Key outlives its owner's access (role removed, user deleted) | Elevation | Effective key roles = key roles ∩ owner's **current** roles, looked up on every use; no roles left, unverifiable owner or lookup failure ⇒ key rejected (fail closed) | `test_api_key.py::test_owner_losing_all_key_roles_disables_the_key`, `test_owner_losing_one_role_narrows_the_key`, `test_unverifiable_owner_rejects_the_key`, `test_role_lookup_failure_fails_closed`; `test_inference.py::test_demoting_the_owner_narrows_existing_keys`, `test_removing_all_owner_roles_disables_existing_keys`, `test_deleted_owner_disables_existing_keys` |
| 22 | Owner later gains roles and silently widens an old key | Elevation | Key roles are a ceiling; the intersection can only shrink them | `test_api_key.py::test_owner_gaining_roles_does_not_widen_the_key` |
| 23 | Keys created by identities whose roles cannot be re-checked (IdP-token users) | Elevation | Keys can only be created from a SuperTokens session | `test_identity_flow.py::test_oauth2_identities_cannot_create_keys`, `test_owner_roles_resolver_only_trusts_supertokens_sessions` |

## Known gaps

These are open. Security should treat them as findings to rank, not as controls.

1. **Stale roles in sessions.** Roles live in the access token, so a role change takes effect only
   at the next refresh or sign-in.
2. **No rate limiting or quota.** Brute force against keys and cost-based abuse are not throttled.
3. **No tenant isolation in policy.** `tenant` is recorded but the policy does not use it.
4. **Audit integrity.** No hash chain. The insert-only ORM guard is bypassed by bulk SQL; production
   needs a database role without `UPDATE`/`DELETE` and log shipping to a write-once store. Audit
   writes are fail-open (logged to the `audit` logger on failure).
5. **Logins are not in `audit_events`.** `/auth/*` is handled by SuperTokens and not audited here.
6. **TLS is out of scope.** API keys and bearer tokens are only safe over TLS terminated in front of
   the service. Run uvicorn with `--proxy-headers` and a restricted `--forwarded-allow-ips`, or
   audited IP addresses will be the proxy's.
7. **Transitive dependency advisory.** `filelock` 3.32.7 (via litellm, which pins `<4`) has known
   advisories. It is not used on the auth path.
8. **Not verified against live systems.** The session provider is tested with a fake
   `get_session`; no test runs against a real SuperTokens Core or corporate IdP. SAML and browser
   SSO federation are not implemented.
9. **Inference backend untested for real.** Tool execution and data access are performed by other
    gateway components; this layer authorizes names only.
10. **API-key requests depend on SuperTokens.** Each key use makes one role lookup against SuperTokens Core. If Core is down, keys are rejected (fail closed), and there is no cache, so each request adds that call's latency. A short-TTL cache would bound the cost at the price of a matching delay before role removals take effect.
11. **IdP-token users cannot create API keys** until there is a live role lookup for the corporate IdP (for example a directory or SCIM query) to plug into `OwnerRoleResolver`.
