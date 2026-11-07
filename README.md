# Enterprise Auth RBAC

Pluggable authentication (SuperTokens sessions, API keys, corporate-IdP OAuth2/OIDC bearer
tokens), role-based access control over models, tools and data sources, and an audit record for
every inference call.

| Doc | What it covers |
|---|---|
| [docs/design.md](docs/design.md) | Architecture, request flow, token contract, configuration, rollout |
| [docs/rbac-matrix.md](docs/rbac-matrix.md) | Who can access what (generated from `policies/rbac_policy.csv`) |
| [docs/threat-model.md](docs/threat-model.md) | STRIDE table, covering tests, known gaps (for the security review) |

### API

| Endpoint | Purpose |
|---|---|
| `POST /v1/inference` | Inference. Authorizes the model, each tool and each data source. |
| `POST /v1/chat/completions` | Semantic-cache + cost-aware model routing (Pinecone, Ollama). Authorizes the model, tools and data sources, then answers from cache or the routed model. |
| `GET /dashboard`, `/dashboard/ui`, `/reports/cost` | Cache/cost analytics. Needs `endpoint:reports` read (analyst, admin). |
| `POST /v1/feedback` | A/B quality score. Needs `endpoint:feedback` write (engineer, analyst, admin). |
| `POST/GET /v1/api-keys`, `DELETE /v1/api-keys/{prefix}` | Self-service API keys |
| `GET/POST/DELETE /admin/users/{id}/roles`, `DELETE /admin/api-keys/{prefix}` | Admin only |

Call it with `Authorization: Bearer <gk_... key | IdP token>` or `X-API-Key`, or a SuperTokens
session cookie. Every request under `/v1` and `/admin` is written to `audit_events`.

### Semantic cache gateway (merged)

`semantic_router/` (from the former SemanticCaching&ModelRoutingLayer project) is mounted into
`app.main`, so **authentication is in front of every semantic-cache route**: no valid credential
means 401 before any embedding, cache lookup or model call. Then Casbin RBAC checks the model
(requested and router-chosen), tools and data sources. The router only picks models the caller may
invoke, cache entries are partitioned by tenant and roles, and every call is audited in
`audit_events` (model, category, cache hit, token counts, cost; never prompts or answers).
The old standalone `semantic_router.auth` package and `create_app()` are removed.

Extra settings are in `.env.example` (Pinecone, Ollama, routing). If Pinecone or Ollama is down at
startup, the auth layer still starts; `/health/ready` returns 503 and the cache connects lazily.
`streamlit run streamlit_app.py` is the demo client; give it a `gk_...` API key in the sidebar.

#### Running the original SemanticCaching&ModelRoutingLayer app behind the gateway

The original project stays unmodified. Terminal 1 (its folder, its venv; auth off by environment variable only):

```powershell
$env:AUTH_ENABLED="false"
.\.venv\Scripts\python.exe -m uvicorn semantic_router.main:app --host 127.0.0.1 --port 8012
```

Terminal 2 (this repo): set `SEMANTIC_UPSTREAM_URL=http://127.0.0.1:8012` in `.env`, then
`.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000`. Clients only talk to port 8000.
Never expose port 8012. In this mode the upstream cannot see the caller, so its cache is shared
across callers and the model it routed to is checked after the fact (answer withheld with 403).
Leave `SEMANTIC_UPSTREAM_URL` unset to serve the embedded copy (per-tenant cache, router-aware RBAC).

### Tests and checks

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m scripts.generate_rbac_matrix --check
```

After editing `policies/rbac_policy.csv`, regenerate the matrix with
`python -m scripts.generate_rbac_matrix`; the test suite fails if the doc is stale.

## SuperTokens backend setup

The FastAPI backend uses SuperTokens' EmailPassword recipe and the session
recipe for browser sessions. SuperTokens endpoints are handled under `/auth`
by the SDK middleware. Corporate IdP tokens for API clients are verified by the
OAuth2 bearer provider (set the `OAUTH2_*` variables). Browser single sign-on
through the corporate IdP is not wired up yet; see
[docs/design.md](docs/design.md#supertokens-what-it-does-here-and-what-it-does-not).

1. Copy `.env.example` to `.env`.
2. Set the backend and frontend origins, SuperTokens Core connection URI, and
   API key. For local development, the origins may be
   `http://localhost:8000` and `http://localhost:3000`.
3. Start the backend with `uvicorn app.main:app --reload`.

The backend validates that required configuration is present and fails startup
with the missing variable names if it is not. `.env` is excluded from Git;
never put real credentials in `.env.example` or commit them.

### Troubleshooting: "Something went wrong" on sign-in

- **Company network that inspects HTTPS (Cisco Secure Access, Zscaler, etc.).** Python does not
  use the Windows certificate store, so calls to SuperTokens Core fail with
  `CERTIFICATE_VERIFY_FAILED` and the UI shows "Something went wrong". Build a bundle that
  contains your company's root certificate and set **both** variables before starting the
  backend (two different HTTP libraries are in use, and each reads its own variable):

  ```bat
  set SSL_CERT_FILE=C:\path\to\corp-ca-bundle.pem
  set REQUESTS_CA_BUNDLE=C:\path\to\corp-ca-bundle.pem
  ```

  Do not disable certificate verification.
- **Frontend must run on `http://localhost:3000`.** CORS only allows `SUPERTOKENS_WEBSITE_DOMAIN`.
- **The shared demo Core (`try.supertokens.com`) rejects the user-roles API.** Sign-in works, but
  assigning roles fails with "explicit deny in an identity-based policy", so RBAC cannot be used
  end to end. Use a Core you control (the compose stack, or a managed SuperTokens instance).

### Development-only seed account

To create a local test login, set `APP_ENV=development`, `DEV_SEED_EMAIL`, and
`DEV_SEED_PASSWORD` in the root `.env` file. Keep the password out of frontend
variables and source control. Then run this explicit one-shot command from the
repository root:

```powershell
.\.venv\Scripts\python.exe -m scripts.seed_dev_account
```

The command refuses to run outside development mode. It creates the account
only when it does not exist; if it already exists, the command verifies the
configured password without changing the existing account. It does not run
automatically when the backend starts and does not assign roles or privileges.

## React frontend

The React app in `frontend/` uses the SuperTokens EmailPassword and Session
recipes. Its Vite environment points to the FastAPI origin; it contains no
SuperTokens API key.

Run the backend and frontend in separate PowerShell terminals:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

```powershell
Set-Location frontend
npm.cmd run dev -- --host localhost --port 3000 --strictPort
```

Open `http://localhost:3000/auth` to sign in or register. After sign-in, the
frontend calls the session-protected `GET /api/me` endpoint. Keep
`SUPERTOKENS_API_DOMAIN=http://localhost:8000` and
`SUPERTOKENS_WEBSITE_DOMAIN=http://localhost:3000` aligned with these local
origins.