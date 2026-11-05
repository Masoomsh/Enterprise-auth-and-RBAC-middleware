# Auth & RBAC design

Pluggable authentication and fine-grained authorization for the gateway's inference API, with an
audit record for every call.

## Request flow

```
request ──► AuditMiddleware (outermost; one row per /v1 and /admin request)
              │
              ▼
          get_identity ── tries providers in order, first match wins
              │            1. SuperTokensSessionProvider  (cookie / st-auth-mode: browser logins)
              │            2. ApiKeyProvider              (gk_<prefix>.<secret>)
              │            3. OAuth2BearerProvider        (corporate IdP JWT, verified via JWKS)
              │          bad credentials ⇒ 401 immediately; later providers are NOT tried
              ▼
          authorize_all ── RBACEngine.check(identity, resource, action) for every
              │            model / tool / data source in the request; all are evaluated
              │            and recorded, any denial ⇒ 403 (or log-only if AUTH_ENFORCE=false)
              ▼
          route handler ── inference backend (litellm), admin actions, key management
```

`request.state` carries `identity`, `auth_error`, `decisions` and `inference_meta` to the audit
layer. Code: `app/authentication/`, `app/authorization/`, `app/middleware/audit.py`,
`app/services/audit_service.py`.

## Identity contract

`Identity(subject, tenant, auth_method, roles, scopes, token_id)`. Every provider produces this and
nothing downstream knows which credential type was used, except the audit log. A new provider
implements `AuthProvider.authenticate(request)`: return an `Identity`, return `None` if the request
carries nothing it handles, or raise `AuthenticationError` if credentials are present but invalid.

## Authorization model

- Resources: `model:<id>`, `tool:<id>`, `datasource:<id>`, `admin:<area>`.
  Actions: `invoke`, `read`, `write`, `admin`.
- Policy: `policies/rbac_policy.csv` (casbin, allow-only, default deny). Matrix:
  [rbac-matrix.md](rbac-matrix.md), generated from the policy.
- **Effective permission = role grant ∩ token/key scopes.** Scopes are resource globs
  (`model:small-*`). An empty scope set means "no narrowing" (browser sessions). API keys cannot be
  created without scopes.
- Request-supplied resource ids may not contain wildcards, so a caller cannot ask for `model:*`.

## Token / credential contract

| Credential | Source of truth | Carries | Lifetime |
|---|---|---|---|
| SuperTokens session | SuperTokens Core | `sub`, `st-role`, `tenant`, `aud` (`semantic-gateway`) | SuperTokens access-token TTL |
| IdP bearer token | Corporate IdP (JWKS) | `sub`, `iss`, `aud`, `exp`, group claim, optional `scope`/`scp`, `jti` | IdP-defined |
| API key | `api_keys` table (HMAC of secret only) | owner, role ceiling, scopes, tenant | default 90 days, max 365 |

**API key roles are a ceiling, not a grant.** On every use the owner's *current* roles are looked
up (SuperTokens, via `identity_service.owner_roles`) and the key's effective roles are
`key roles ∩ owner roles`. If the owner lost every role, was deleted, cannot be looked up, or the
lookup fails, the key is rejected. Removing a role therefore takes effect on existing keys at
once, not at expiry. Because only SuperTokens users can be checked live, keys can only be created
from a SuperTokens session (not from an IdP bearer token or another key).

IdP tokens: only `RS256`/`ES256` are accepted; `exp`, `iss`, `aud`, `sub` are required. IdP groups
become gateway roles only through the explicit `OAUTH2_ROLE_MAP`; unmapped groups grant nothing.

## Audit

One row per request in `audit_events`: who (subject, tenant, auth method, token id / key prefix),
what (action, resource), outcome (`allow` / `deny` / `error`, reason, status), where/when (IP, time,
latency, request id) and `details` JSON (every authorization check, model, tools, data sources,
message count, prompt length, token usage). **Prompts, secrets and raw tokens are never stored.**
Rows are insert-only (ORM guard; production must also enforce this with database privileges).
If the write fails the response is not blocked, and the full event is emitted on the `audit`
logger for the SIEM.

## SuperTokens: what it does here and what it does not

| Need | Handled by |
|---|---|
| Browser login, sessions, refresh, CSRF | SuperTokens `emailpassword` + `session` recipes |
| Roles in the token (`st-role`) | `userroles` recipe; assigned with `scripts/assign_role.py` or `/admin/users/{id}/roles` |
| `aud` / `tenant` claims | session override in `app/authentication/supertokens.py` |
| Public keys for downstream verification | `jwt` recipe: `<api_domain>/auth/jwt/jwks.json` |
| Corporate SSO in the browser | **Not wired yet.** Add the `thirdparty` recipe with the IdP as a custom OIDC provider (SAML: SuperTokens enterprise SAML or a `python3-saml` provider). Roles must then be mapped from IdP groups on sign-in. |
| Service/API clients with IdP tokens | `OAuth2BearerProvider` (configure `OAUTH2_*`) |
| API keys, RBAC decisions, audit | This repo; SuperTokens is not involved |

## Configuration

| Variable | Purpose |
|---|---|
| `SUPERTOKENS_*` | Core connection, domains, API key (required) |
| `API_KEY_HMAC_SECRET` | Server secret for hashing API keys. Without it key routes return 503. |
| `OAUTH2_ISSUER`, `OAUTH2_AUDIENCE`, `OAUTH2_JWKS_URI` | Enable IdP bearer tokens (all three required) |
| `OAUTH2_GROUP_CLAIM`, `OAUTH2_ROLE_MAP` | Group claim name and JSON map `{"IdP group": "gateway role"}` |
| `AUTH_ENFORCE` | `false` = audit-only rollout: log would-be denials, allow the request |

## How to extend

- **Add a role:** add it to `GATEWAY_ROLES` (`app/services/identity_service.py`), add grants to the
  policy, regenerate the matrix, extend `tests/test_rbac.py`.
- **Add a model / tool / data source:** add or widen a grant in the policy. No code change.
- **Onboard an IdP:** set the `OAUTH2_*` variables, fill `OAUTH2_ROLE_MAP`, test with a real token.
- **Add a provider:** implement `AuthProvider`, register it in `get_providers()`.

## Rollout

1. Deploy with `AUTH_ENFORCE=false`; review `audit_events` for `decision = 'deny'` rows that would
   have broken legitimate callers; adjust the policy.
2. Set `AUTH_ENFORCE=true`.
3. Rollback: set `AUTH_ENFORCE=false` (authentication stays on; only authorization is relaxed).

## Not in scope of this change

Rate limiting, per-tenant policy isolation, SAML, audit hash-chaining, live SuperTokens
IdP federation, and a role lookup for IdP-token users (so they can own API keys). See [threat-model.md](threat-model.md#known-gaps).
