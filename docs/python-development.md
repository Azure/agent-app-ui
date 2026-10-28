# Python development

## Protected-source interactive authentication recovery

The UI accepts safe `interaction_required`, `authentication_required`, and
`source_denied` errors from the authenticated hosted stream. Both Invocations
`error` frames and SDK `response.failed.response.error` terminals preserve
bounded `claims_challenge` JSON. Only nonempty `access_token` claims are
accepted; duplicate members, malformed Unicode/JSON, and values over 8192 UTF-8
bytes are rejected. Provider messages/bodies are never rendered.

Standalone endpoints, registered before the Chainlit mount:

- `GET /auth/oauth/azure-ad`: ordinary configured-tenant MSAL login.
- `GET /api/auth/recovery?ticket=<encrypted>`: explicit Continue sign-in.
- `GET /auth/oauth/azure-ad/callback`: existing registered redirect URI.

The 180-second encrypted context uses existing `CHAINLIT_AUTH_SECRET`;
`CHAINLIT_URL` must be the configured HTTPS UI origin. No new setting or Azure
redirect registration is required. State, nonce and PKCE remain request-local;
claims go to the configured Entra authorize request, auth-code token exchange,
and refresh-token form `data`. The pinned MSAL refresh method does **not**
translate `claims_challenge` into token form data. Neither resource/authority
hints nor redirects from a source are used. Existing sessions without a
recovery binding require ordinary login. Successful callback rotates the binding.

Recovery context is encrypted in links/cookies, never plaintext/base64-only,
and bound to the current principal/login. Callback verifies the authenticated
identity again; logout, account switching, expiry, tamper, invalid state and
reused browser context fail closed. Concurrent login tabs intentionally use
last-state-wins. Standalone tickets need no new replica-local registry.

No model/tool request, partial stream, or download is replayed. Native downloads
expose the same sign-in control only after the current signed grant is checked;
ordinary source denial stays denied. Embedded downloads can notify an opener
on exact configured portal origins; without an opener, the page explicitly
requires returning to the portal and reopening the assistant.

Claims are excluded from user metadata/history and diagnostics; encrypted
element props can expire in restored history. OAuth/recovery URLs are excluded
from HTTP tracing, authorization `Location` headers sanitized, MSAL body logging
suppressed, and access-log query strings redacted. See
[ADR-001](adr/ADR-001-interactive-protected-auth-recovery.md) and the
[portal helper contract](copilot-embedding.md#protected-source-sign-in-recovery).

Local regression commands:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_auth_recovery.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_native_downloads.py -v
```

`tests/auth_recovery_browser.html` exercises the bundled helper locally with
synthetic MSAL/fetch boundaries; it makes no application Azure requests. These
checks do not prove live Entra CA, browser/proxy settings or Azure deployment.

This guide covers the shipped UI package and contributor workflow. For
platform setup, deployment, identity permissions, and supported component
versions, use the [central product documentation](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/).
The umbrella release manifest, not this guide, pins the compatible runtime set.

## Set up a development environment

Clone this repository and run the following commands from its root.
Use Python 3.12 and a virtual environment:

```text
python -m venv .venv
```

Activate it in PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Or activate it in Bash:

```bash
source .venv/bin/activate
```

Install runtime dependencies, contributor tools, and the editable package:

```text
python -m pip install -r requirements.txt
python -m pip install -r requirements-quality.txt
python -m pip install --no-deps -e .
```

`requirements.txt` is the runtime dependency list. The setuptools distribution
installs canonical code and supported legacy modules without a second runtime
dependency list. Deployment uses `python -m pip install --no-deps .`.

### Start against a configured platform

This is not an offline chat demo. Set `APP_CONFIG_ENDPOINT` in the shell to
the provisioned Azure App Configuration endpoint. The provider tries managed
identity, then the local Azure CLI credential, and resolves Key Vault references.
The identity needs access to the configuration and referenced secrets.
Follow the central [configuration](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/configuration/)
and [delegated authentication](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/hosted-agents/#configure-the-uis-delegated-authentication)
guidance before starting:

```text
uvicorn main:app --host 0.0.0.0 --port 8080
```

Docker uses the same command from `/app`. A missing or disconnected App
Configuration provider produces an HTTP 503 not-ready app. Setting individual
environment variables does not make that app ready. Environment overrides
exist only where a specific settings loader reads them.

`CHAT_BACKEND` defaults to `hosted_agent` when absent or blank.
`orchestrator` is the explicit Container Apps alternative. Unknown values
fail, and hosted failures do not trigger fallback. Hosted startup validates
the endpoint, data-plane scope, timeout, and authentication settings. The
default `HOSTED_AGENT_AUTH_MODE=user_delegated` also requires the three
`OAUTH_AZURE_AD_*` confidential-client settings for OBO. Do not switch to
`service_identity` to work around a failed user login or consent problem.

## Ownership and imports

Runtime code lives under `src/gpt_rag_ui/`:

| Area | Responsibility |
| --- | --- |
| `bootstrap.py` | ASGI composition and startup |
| `api/` | Routes, callback registration, Chainlit history adapter, and session bridge |
| `services/` | Chat, citations, history, continuity, conversation/download policy, and panel metadata |
| `auth/` | Identity, session, token, and transport security |
| `clients/` | Orchestrator, ingestion, hosted runtime, Conversations, Blob, and Cosmos transports |
| `config/` | App Configuration provider/cache, settings, and asset-root resolution |
| `telemetry/` | Instrumentation and boundary diagnostics |
| `util/` | Constants |

Root modules such as `main.py`, `app.py`, and `datalayer.py`, plus
`connectors/`, are supported compatibility adapters. Public exports forward
to canonical objects. Patch canonical owners in tests, not private adapter
globals. `.quality/migration.json` records the frozen legacy inventory.
`.quality/policy.json` records current modules and adapters.

`api.history.OrchestratorDataLayer` adapts Chainlit callbacks.
`get_data_layer()` creates a fresh adapter.
`services.history.HistoryService` owns history/user operations and the shared
user cache. The API owns ambient session selection and consume-once request
metadata. Services receive explicit `HistoryOperationContext`.

Registration in `api.callbacks`, `api.history`, and `api.oauth` is idempotent.
The classic history data layer is registered only for
`CHAT_BACKEND=orchestrator`. Hosted chat retains OAuth, chat, and feedback
callbacks but does not register classic orchestrator/Dapr history.
Active-session context is not durable history after reload or a new chat.

## Backend contracts for contributors

The UI invokes the orchestrator agent through
`POST /invocations?api-version=v1` on the configured hosted base URL.
The request contains `messages` as ordered text `user` and `assistant`
objects, plus `metadata`. The default path adds `conversation_id` only when
the hosted runtime has issued one. It does not send a top-level
`conversation` or `previous_response_id`.

The SSE `response.created` frame must contain a `response` object with
`conversation.id` or the legacy `conversation_id`. If both are present they
must agree. Missing, malformed, or conflicting identifiers fail explicitly.
The default path resends the available Chainlit history without a local
item or token cap. It adds the current ask if it is not already the final
user message. A Chainlit thread ID is not a hosted conversation ID.

The default hosted bearer is obtained through OBO from the current user's
access token. A missing token or failed exchange stops the hosted invocation.
`RETRIEVAL_AUTH_MODE` defaults to `native_search`: the same current API
assertion is exchanged separately for `https://search.azure.com/.default`;
the resulting raw token is forwarded only as `x-client-search-token`.
Configured Work IQ additionally requires `WORK_IQ_APPLICATION_ID` to match
the UI API, optional `WORK_IQ_TENANT_ID` to match its tenant, and an API
assertion obtained with `api://<WORK_IQ_APPLICATION_ID>/access_as_user`.
It forwards that original validated assertion separately, never the Search
token. `WORK_IQ_FEDERATED_CREDENTIAL_ID` is platform-owned source configuration,
not a UI credential. Separate Work IQ app OBO is not implemented.
See the central product documentation for provisioning and consent.
Hosted uploads require the existing authenticated session's explicit valid
`object_id` before file reads or ingestion dispatch; a display/partition ID
is not an uploader ACL. Native Search then combines that uploader ACL with
the conversation filter.

Native hosted citations use the existing signed download route, never the
legacy SAS/container-only readers. Standalone requests authenticate the
current Chainlit cookie and refresh its OAuth assertion; embedded grants also
bind the active opaque session. Shared Blob/ADLS downloads use that user's
Storage OBO (`https://storage.azure.com/.default`), with no workload fallback.
Conversation uploads first run a fresh native Search query against
`SEARCH_SERVICE_QUERY_ENDPOINT` / `SEARCH_RAG_INDEX_NAME`, using Search OBO
and workload query transport. Only an exact `url` and conversation match
with the pinned uploader-only ACL permits subsequent workload Blob access.
No classic Cosmos conversation-owner lookup is used for native downloads.

The existing platform index must have native permission filtering enabled.
`SEARCH_API_VERSION` must support it (default `2025-11-01-preview`); the UI
workload requires Search Index Data Reader and upload-container Blob Data
Reader. Shared users need Storage delegated consent and applicable Blob RBAC
or ADLS ACLs. Existing `STORAGE_ACCOUNT_NAME`, document/image/conversation
container settings, `CHAINLIT_URL`, and `CHAINLIT_AUTH_SECRET` configure the
fixed destination and signed UI grants. No customer URL or ID grants access.
Responses stream asynchronously in 64 KiB chunks, cap documents at 100 MiB
and stream time at 120 seconds, and close on cancellation. The upload gate
caps results at 1,000 rows/4 MiB/30 seconds and fails closed if the exact
authorized document is absent, including beyond that cap. There is no
started-response retry. Classic downloads outside hosted mode are unchanged.
OBO itself can contact Entra. There is no fallback to service identity.
`HOSTED_AGENT_AUTH_MODE=service_identity` is an explicit exception that makes
the UI service the caller, not the user. Login, OBO consent, managed
conversation ownership, and retrieval permission enforcement are separate
boundaries. None alone proves per-user document authorization. In the
orchestrator's strict retrieval mode (`ALLOW_ANONYMOUS=false`), missing or
failed required OBO and retrieval failures remain errors, not anonymous
fallbacks. Disabling ingestion does not relax that policy.

### Optional continuity

`HOSTED_CONTINUITY_ENABLED` is `false` by default. When enabled, the UI owns
the managed Conversations store and invokes the hosted runtime without a
conversation reference. `HOSTED_CONVERSATION_OWNER_BINDING` defaults to
`delegated` when enabled. That mode uses a service bearer with a
server-derived `x-ms-user-identity` header for store calls. Explicitly
selecting `capability` instead uses a signed owner handle and user OBO for
those calls. Neither changes the hosted invocation's authentication mode.

The settings below are ignored while continuity is disabled. When enabled,
nonblank environment values override provider values. Invalid required
settings fail startup rather than selecting another binding mode.

| Setting | Default | Requirements when continuity is enabled |
| --- | --- | --- |
| `HOSTED_CONVERSATION_OWNER_BINDING_VALIDATED` | `false` | Boolean. Required as `true` together with the protocol version in `delegated` mode. Set it only after validating the UI service identity's agent-scoped `Microsoft.CognitiveServices/accounts/AIServices/agents/endpoints/UserIdentityImpersonation/action` grant. Not required as `true` in `capability` mode. |
| `HOSTED_AGENT_PROTOCOL_VERSION` | Unset | Required together with the validation flag in `delegated` mode. Set the deployed hosted agent's responses protocol version as three numeric components, `MAJOR.MINOR.PATCH`, with a value `>= 2.0.0`. Ignored in `capability` mode. |
| `HOSTED_CONVERSATION_CAPABILITY_KEY` | Unset | Required together with the key ID in `capability` mode, with at least 32 characters after trimming. Supply it through a Key Vault reference, never a literal secret. Not required in `delegated` mode. |
| `HOSTED_CONVERSATION_CAPABILITY_KEY_ID` | Unset | Nonblank key identifier required together with the signing key in `capability` mode. Identifies the current key so retired keys can be rejected after rotation. Not required in `delegated` mode. |
| `HOSTED_CONVERSATION_CAPABILITY_TTL_SECONDS` | `900` | Integer from `60` through `86400`, inclusive. Sets the capability handle lifetime in seconds. The loader validates this range in both binding modes. |
| `HOSTED_CONVERSATION_STORE_BASE_URL` | Unset | Required in both modes. Absolute HTTPS URL for the Conversations store. HTTP is accepted only for `localhost`, `127.0.0.1`, or `::1`. Trailing slashes are removed. There is no hosted base URL fallback. |
| `HOSTED_CONVERSATION_STORE_RESOURCE_SCOPE` | Falls back to `HOSTED_AGENT_RESOURCE_SCOPE` | Required in both modes after fallback, with no built-in scope value. Must end in `/.default` and must not contain `management.azure.com`, ignoring case for that ARM check. |

Booleans accept `true`/`false`, `1`/`0`, `yes`/`no`, `y`/`n`, or `on`/`off`,
ignoring case. A supplied validation flag must be valid in either binding mode.

The store client sends `{}` to `POST /conversations` and reads `id` from the
response. `GET /conversations/{id}/items` expects a `data` array.
`POST /conversations/{id}/items` sends an `items` array. These store operations
are separate from the hosted invocation envelope above.

The shipped store client reads one page with `order=asc` and a request limit
clamped to `1..100`. It does not follow pagination, so long conversations can
omit later items. `HOSTED_HISTORY_MAX_ITEMS` defaults to `40` and accepts
`1..500`, but values above 100 do not increase this one-page read.
`HOSTED_HISTORY_MAX_TOKENS` defaults to `8000` and accepts `256..200000`.
`drop_oldest` first limits items within the returned page, then drops the
oldest remaining items to meet an estimated token budget. The estimate is
one token per four text characters, rounded up, with a minimum of one.
The final remaining item is never dropped even if it exceeds the budget.
The current ask is added afterward, so these are not hard request-token
limits or a guarantee of the latest complete history.

A rejected presented owner handle never starts a replacement conversation.
History-read or append failures are explicit failures. A failed append must
not be presented as a durably saved turn. Locks and the bounded idempotency
cache are process-local, not distributed.
Delegated ownership enforcement on the deployed Conversations/items surface
still needs independent live validation. Do not treat mocked store tests as
proof of platform authorization.

### Optional ingestion

`DEPLOY_INGESTION_CONTAINER_APP` is read through the App Configuration provider,
not a separate environment-only flag. Missing preserves legacy upload
eligibility. `false` hides standalone and Entra Copilot controls and rejects
attachments in the handler and ingestion client. The client checks the flag
before URL/Dapr resolution, payload construction, file reads, or HTTP client
creation. Invalid boolean values raise a configuration error.

`true` retains authentication, anonymous-Copilot denial, file validation, and
conversation checks. Hosted uploads require an already issued hosted
conversation ID. There is no native conversation upload replacement.
The flag does not change text chat, OBO, or retrieval authorization.

## Installed resources

The installed artifact is code plus staged assets, not a self-contained wheel.
Keep `.chainlit/`, `public/`, `chainlit.config.yaml`, `chainlit.md`, and
`VERSION` together in a writable application root. Set `CHAINLIT_APP_ROOT`
for an explicitly staged root, or start from that directory. A source-checkout
`main.py` passes its adjacent asset root.

The root is resolved before importing Chainlit and is shared by VERSION,
configuration synchronization, public assets, and upload paths. Do not write
to site-packages or add `sys.path`/`sys.modules` compatibility workarounds.
Missing VERSION is optional. Missing `.chainlit/config.toml` retains a warning.

## Contributor validation

The complete local behavioral suite is:

```text
python -m unittest discover -s tests -v
```

Focused package checks are:

```text
python -m pip wheel --no-deps --wheel-dir .artifacts/wheel .
python -m unittest discover -s tests -p test_module_compatibility.py -v
python -m unittest discover -s tests -p test_installed_package.py -v
```

Installed-package acceptance builds a wheel, creates a clean non-editable
environment, installs runtime requirements, runs `pip check`, and verifies
canonical and legacy import origins outside the checkout. It copies
behavioral tests, not runtime sources. This can install dependencies and is
not evidence of a Linux image or live Azure integration.

### Protected quality checks

Replace `<protected-base-sha>` below with the immutable base SHA for the
actual target branch. Do not substitute the candidate head to avoid findings.

```text
python .github/scripts/check-quality.py --check all --base-ref <protected-base-sha> --report .artifacts/quality.json
python -m unittest discover -s tests -p test_quality_policy.py -v
```

`--check` accepts `all`, `lint`, `typing`, `architecture`, `exceptions`, and
`policy`. Exit 0 means passed, 1 means violations, and 2 means invalid inputs
or incomplete execution. Checks do not rewrite source, policy, or baselines
and do not connect to Azure. Tool pins are in `requirements-quality.txt`.

Exception and dynamic-import validation also requires a same-run execution
receipt. In PowerShell, replace the base placeholder in both commands:

```powershell
$env:QUALITY_RUN_ID = [guid]::NewGuid().ToString()
python .github\scripts\run-unittest.py --base-ref <protected-base-sha> --report .artifacts\unittest.json
python .github\scripts\check-quality.py --check all --base-ref <protected-base-sha> --test-evidence .artifacts\unittest.json --report .artifacts\quality.json
```

Set `QUALITY_RUN_ID` once per local run in other shells too. The receipt
binds exact test methods, source fingerprints, base/head, and run context.
Skipped, expected-failure, stale, or failed evidence cannot validate a site.
Local `--pattern` supports focused runs. CI aggregation requires full
`test_*.py` discovery.

Active exception records are eligible for technical validation, not blanket
approval. Each needs exact-source passing evidence, an unexpired date, and a
valid review stage. Proposed or retired records cannot be consumed.
Registry edits alone do not block, and an independent human-review field is
not an execution requirement. Protected checker, tool, workflow, schema,
typing, import, and suppression rules still apply. Do not grow baselines,
disable checks, or fabricate reports to make a change pass.

CI selects the protected-base evaluator and tool pins in a separate
environment that does not install candidate code. The `quality-gate` job
requires actual unit-test, quality-matrix, and ephemeral Linux container-test
results plus fresh bound reports and the unittest receipt. The container
helper runs without networking and does not publish or deploy an image.
A local command or workflow file does not prove branch protection or required
merge checks. Those settings remain separate repository controls.

For engineering-document and agent-asset validation, see
[the engineering framework](agentic-engineering.md#validation).
For embedding security, see [Chainlit Copilot embedding](copilot-embedding.md).
