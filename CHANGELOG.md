# Changelog

## [Unreleased]

## [v3.4.1] - 2026-10-30

### Changed

- **Release changelog lifecycle.** Keep `Unreleased` only on `develop`, never
  on a release branch or `main`, matching the ingestion and orchestrator
  release process. Restore an empty development section separately after
  the release is merged.
- **Reviewed dependency maintenance.** Require Pydantic 2.13.5 or newer within
  version 2, retaining the adopted Chainlit and patched authentication stack.
- **Grouped Actions maintenance.** Check GitHub Actions weekly against
  `develop`, grouped with a seven-day cooldown, without adding a duplicate
  update stream against `main`.
- **Shared repository protection standard.** Require pull requests and passing
  quality, behavior and container checks without mandatory second-person review.
  Preserve protected-base integrity, immutable Actions references and narrowly
  scoped privileged workflow authorization.

### Fixed

- **Security dependency updates.** Upgrade aiohttp and the setuptools build
  backend to patched releases. Require PyJWT 2.15 or newer within version 2
  so new installations cannot select the reported vulnerable token library.
- **Immutable GitHub Actions references.** Pin the remaining workflow actions
  to verified full-length commit SHAs without changing their selected versions,
  and require agents to preserve SHA pinning when editing or regenerating CI.

## [v3.4.0] - 2026-10-28

### Added

- **Interactive protected-source sign-in recovery.** Preserve authenticated
  downstream Conditional Access challenges through hosted errors and native
  downloads. Offer explicit principal/session-bound encrypted recovery,
  request-local Entra authorize/callback handling, and a bundled portal/MSAL
  recovery handshake without replaying failed or partially emitted turns.
  Keep claims out of chat persistence and diagnostics; ordinary denial remains
  denial. Existing OAuth sessions require a fresh login to obtain a binding.

- **Native protected retrieval delegation.** Preserve user-delegated Foundry
  invocation while forwarding a separately acquired Search user token. Require
  the matching UI-app assertion for configured Work IQ, use refreshed API
  tokens on the same turn, and surface safe consent and authentication failures
  without provider bodies or credential-bearing diagnostic headers. Reject
  protected uploads without the verified session's explicit uploader object ID
  before reading files or calling ingestion. Native downloads now use signed
  authenticated UI grants, current-user Storage OBO for shared documents, and
  a fresh exact-document native Search gate before workload upload reads;
  native citations never mint Blob SAS or reach legacy anonymous readers.

### Fixed

- **Recovery authentication module boundaries.** Share bounded HTTP claims
  parsing through its authentication owner, expose typed OAuth configuration
  and recovery-binding validation interfaces, and resolve the recovery origin
  through the configuration provider without an OAuth dependency cycle.
  Preserve legacy exports, current authorization checks, and recovery protocol.

### Validation

- Full unittest, non-editable installed-wheel and offline Linux-container
  acceptance, together with protected lint, typing, architecture, exception,
  policy and aggregate quality checks.

## [v3.3.1] - 2026-10-27

### Fixed

- **Application resource-group targeting.** Resolve Container Apps, registry
  operations and deployment identities from `AZURE_APP_RESOURCE_GROUP` under
  `agent-lz`. Confirmed missing or empty values retain the legacy
  `AZURE_RESOURCE_GROUP` fallback; configuration access failures stop deployment.

## [v3.3.0] - 2026-10-14

### Added

- **Optional-ingestion upload capability.** Honor
  `DEPLOY_INGESTION_CONTAINER_APP` independently of the chat backend: `false`
  hides standalone and Entra Copilot upload controls and rejects attachments
  before ingestion URL or Dapr resolution. Missing preserves legacy upload
  eligibility; `true` retains existing authentication and conversation checks;
  malformed boolean values fail with a configuration error. Anonymous Copilot
  uploads remain disabled. Text chat and retrieval are unchanged; no native
  Foundry IQ conversation upload path is introduced.

### Validation

- Offline acceptance covers the source suite, non-editable installed-wheel
  acceptance, ephemeral Linux-container behavior, and protected lint, typing,
  architecture, exceptions, policy, and aggregate quality checks. No live Azure
  reads, deployment checks, or Azure acceptance are included.

## [v3.2.2] - 2026-10-14

### Fixed

- **Hosted invocation compatibility.** Include the required `api-version=v1`
  query parameter and accept the current Responses `conversation.id` envelope
  alongside legacy `conversation_id`, rejecting malformed or conflicting IDs.
- **Backend-specific history registration.** Hosted chat no longer registers
  the classic Dapr-backed data layer; OAuth, feedback, session context and
  classic authenticated history remain unchanged.

### Changed

- Technically validate candidate exception registry updates without mandatory
  independent approval or registry-only policy blocking. Preserve expiry,
  source-bound passing behavior evidence, protected evaluator/tool/workflow
  integrity and fail-closed CI aggregation. Protected-checker adoption remains
  required; this does not change runtime behavior or repository review settings.

### Validation

- Offline release acceptance uses installed-wheel and Linux-container tests
  plus the adopted protected quality checks; Azure deployment validation is
  intentionally not included.

## [v3.2.1] - 2026-10-08

### Security

- Upgrade Chainlit to 2.12.0, aiohttp to 3.14.3 and setuptools to 83.0.0 to
  resolve Dependabot security alerts. Adds `get_favorite_steps` to the data
  layer, required by Chainlit 2.12.

## [v3.2.0] - 2026-10-06

### Removed

- Legacy `gpt-rag` App Configuration label fallback and `GPT_RAG_` key
  fallback (Azure/agent-landing-zone#695). The app now reads only its app label,
  then `agent-lz`, then no label. Make sure your deployment publishes settings
  under `agent-lz` (Agent Landing Zone v4.0.0 or later) before upgrading.

## [v3.1.1] - 2026-10-06

### Fixed

- **Fail early when the ACR Task agent pool is missing under network isolation**
  (Azure/agent-landing-zone#741). With `NETWORK_ISOLATION=true` and
  `BUILD_MODE=acr-task`, the deploy script now stops with a clear message if
  `ACR_TASK_AGENT_POOL_NAME` is not set, instead of failing during the remote
  build because the registry's public access is blocked.

## [v3.1.0] - 2026-10-05

### Added

- **Keyless service-to-service auth to the orchestrator** (ADR-0019). The UI
  acquires a Microsoft Entra token with its managed identity (scope from
  `ORCHESTRATOR_AUTH_AUDIENCE`) and sends it in `X-Service-Authorization`,
  keeping `Authorization` for the user's OBO token. API key and Dapr remain
  opt-in fallbacks.

## [v3.0.0] - 2026-10-03

### Changed

- **Agent Landing Zone dual-read and rebrand** (Azure/GPT-RAG#695). App
  Configuration now loads the `agent-lz` label, which overrides the legacy
  `gpt-rag` label; `AGENTLZ_*` keys are read before legacy `GPT_RAG_*` keys.
  Tracer names use the `agentlz.` prefix. Visible branding (assistant name,
  welcome title, release footer) now reads "Agent Landing Zone". Existing
  `gpt-rag` deployments keep working unchanged.

### Fixed

- **Retry failed classic-chat uploads without losing attachments.** Restore the
  previous session conversation ID when a failed upload allocated a new one.
  Reattached files are ingested under a fresh conversation before the question
  runs; existing conversation ownership checks remain unchanged.
- **Stop questions after unconfirmed ingestion.** False results and raised
  failures finalize a reference-bearing retry notice without sending the
  question or claiming successful file processing. Only confirmed batches
  update uploaded-document bookkeeping.
- **Keep OpenAPI failures retryable and truthful.** Failed schema generation
  propagates instead of returning a successful metadata-only schema; later
  requests retry generation and cache only success.
- **Expose owner-index repair needs without weakening panel access.** A failed
  optional index write logs that repair is required while the turn continues;
  missing ownership rows still hide panel listings and deny read, feedback,
  and delete operations.

### Changed

- **Preserve dependency failures without masking implementation defects.**
  Narrow configuration, HTTP/JSON, Blob, JWT, logging and VERSION handlers to
  their concrete failure contracts. Fix the unbound retry notification and
  avoid retrying absent in-memory settings. Record retained application
  boundaries individually as unapproved proposals with failure-test evidence.

- **Separate Chainlit history adaptation from history operations.** Put the
  data-layer callbacks, factory and consume-once session bridge in `api.history`;
  keep history authorization, orchestrator operations and the single user cache
  in `services.history` with explicit context. Preserve legacy exports and
  ownership-before-session-selection ordering without a storage rewrite.

- **Canonical UI runtime package with legacy adapters.** Move runtime ownership
  into `src/gpt_rag_ui`, share citation rendering between chat and restored
  history, and separate callback registration from reusable operations. Keep
  `uvicorn main:app`, existing configuration/defaults and externally staged
  Chainlit assets; install the package in the existing container build.

### Added

- **Isolate protected quality-tool execution.** Run static tools without
  candidate module shadowing or candidate package/build-backend installation
  in the evaluator; analyze the explicit source graph in an isolated worker.
  Add executable shadowing and installation-isolation regression fixtures.

- **Proposed protected-base Python quality gates.** Add pinned development-only
  Ruff, mypy and import tooling, individual typing debt records, full static
  dependency checks, an explicit broad-handler inventory and a fail-closed
  `quality-gate` aggregate alongside unittest. Initial handler approvals,
  bootstrap review and administrator activation remain outstanding; this
  change does not claim required merge enforcement is active.

- **Exact quality policy and executed failure evidence.** Validate closed
  governance schemas, stable move/debt identities, annotation/suppression sites,
  adapter exports and dynamic imports. Bind exception approvals to distinct
  protected operations and same-run, non-skipped unittest results. Require
  protected aggregation and ephemeral offline Linux-image acceptance without
  publishing an image or changing repository settings.

- **Close binding and namespace quality-check bypasses.** Detect qualified
  suppression decorators on untyped functions, invalidate exception records
  when effective catch bindings change, and reject unresolved catches.
  Discover root namespace modules and retain their full typing/import identity
  without changing application imports or exception behavior.

## [v2.6.2] - 2026-09-03

### Fixed

- **Citations returned as absolute URLs for the solution's own storage account
  were dropped.** The legacy resolution path returned such a URL unsigned,
  producing a broken link, and the secured path discarded the citation
  entirely. Both paths now normalize the URL back to a container-relative path
  before resolution, so the legacy path signs it with a SAS and the secured
  path issues a session-bound `/api/download/` grant after its existing
  per-document authorization check. URLs pointing at external hosts, and URLs
  that already carry a signature, are left untouched.

### Validation

- `unit-tests` workflow green on the release commit: 410 tests, up from 405,
  the five new ones covering both resolution paths plus the external-host and
  already-signed cases.
## [v2.6.1] - 2026-09-03

### Changed

- **Dependency maintenance release.** No application behavior changed; this
  release only refreshes pinned runtime dependencies on top of v2.6.0.
  - `chainlit` 2.9.4 -> 2.11.0
  - `uvicorn` 0.35.0 -> 0.52.4
  - `azure-identity` 1.23.0 -> 1.25.3
  - `azure-appconfiguration-provider` 2.1.0 -> 2.5.0
  - `azure-monitor-opentelemetry-exporter` bumped to its current release

### Validation

- Repository test workflow green on `develop` at the release commit.

## [v2.6.0] - 2026-08-07

### Added
- **Repository-local release skill:** Added a reusable GitHub Copilot `release` skill that discovers and reconciles authoritative version evidence, prepares SemVer-aligned release metadata and sanitized notes, validates branch and pull-request targets, and documents rollback paths while requiring explicit human approval before publishing tags, releases, packages, images, deployments, or Azure changes.
- **Opt-in hosted-agent cross-version continuity (`HOSTED_CONTINUITY_ENABLED`, default `false`)**: Added an alternative, BFF-owned continuity model for the `CHAT_BACKEND=hosted_agent` runtime so multi-turn conversations survive hosted-runtime version upgrades. When enabled, this UI — not the hosted runtime — becomes the exclusive owner of the Foundry managed Conversation used for continuity: it creates, reads, appends to, and deletes the managed conversation through the standard Conversations REST surface, while the hosted runtime itself stays fully stateless and history-blind (it never receives a `conversation_id` or `previous_response_id` and needs zero Conversations RBAC). Every turn acquires a one-in-flight lock per conversation (`hosted_conversation_store.py`), reads ordered history from the system-of-record, applies an explicit bounded-history policy (drop-oldest by item count then by an estimated token budget, never dropping the single most recent item), and appends the completed turn back fail-closed: an append or read failure raises `ContinuityPersistenceError` and the turn is never presented as a successfully saved completion, while an idempotent client turn id prevents a duplicate append on retry. While the feature flag is `false` (the default for all existing deployments), none of this new code path runs and the current hosted-agent behavior — a Foundry-issued `conversation_id` round-tripped by the hosted runtime and full history resent by the Chainlit client each turn — is completely unchanged. See the README's "Optional: hosted-agent cross-version continuity" section for the full settings table, both owner-binding modes, and residual operational notes.
- **Delegated, platform-enforced owner binding as the preferred/default continuity model ([Azure/GPT-RAG#591](https://github.com/Azure/GPT-RAG/issues/591), "OQ-OWN")**: Live evidence showed Azure AI Foundry hosted agents on responses protocol `>= 2.0.0` platform-enforce per-asserted-user ownership of managed Conversation/response state when a trusted middle tier asserts an `x-ms-user-identity` header (derived only from the caller's validated Entra `oid`, never client input) and additionally holds the custom `Microsoft.CognitiveServices/accounts/AIServices/agents/endpoints/UserIdentityImpersonation/action` data action at the agent scope — cross-user continuation/reads return `404`, and non-delegated/missing-header access to a delegated session returns `403`. `HOSTED_CONVERSATION_OWNER_BINDING` now defaults to `delegated` once continuity is enabled (previously `capability`, which was itself previously a reserved, unimplemented name). `hosted_conversation_store.py` now supports both modes: `delegated` acquires a service-identity bearer (new `hosted_agent_client.acquire_service_identity_token`, kept explicitly distinct from the OBO token used for Toolbox retrieval and for `capability` mode's own Conversations calls) and asserts the identity header on every create/read/append/delete call, while the client-held continuity handle is simply the real managed conversation id (no cryptographic wrapping needed, since the platform — not a BFF signature — rejects a mismatched `oid`); `capability` mode is unchanged and remains a fully supported, explicit disabled fallback. `delegated` mode fails closed at startup (the operational equivalent of a `503`) unless both `HOSTED_CONVERSATION_OWNER_BINDING_VALIDATED=true` and the new `HOSTED_AGENT_PROTOCOL_VERSION` (required, must be `>= 2.0.0`) are explicitly set, since neither the RBAC grant nor the deployed protocol version can be discovered at runtime. A new `ConversationStoreAccessDeniedError` distinguishes platform-denied access (401/403/404) from generic transport failures; see the Security entry below for how a rejected *presented* conversation reference is actually handled. **Residual risk**: the live evidence tested the raw Foundry Responses API, not the OpenAI-compatible Conversations/items API this UI actually calls against; this generalization has not been independently live-verified and should be validated before enabling `delegated` mode in a production tenant. Session membership itself is not fenced by this mechanism, and downstream Toolbox retrieval data is not platform-partitioned by it — per-user document-level authorization (ADR-0001) remains required regardless.
- **Panel user-facing conversation history/feedback/deletion surfaces (issue [Azure/GPT-RAG#611](https://github.com/Azure/GPT-RAG/issues/611), ADR-0004)**: Added the gpt-rag-ui side of the optional administrative panel's user-facing contract: `GET /panel/conversations` (paginated list of the caller's own conversations), `GET /panel/conversations/{id}/messages` (owner-gated ordered history read against the existing hosted-continuity managed-Conversations store), `POST`/`GET /panel/conversations/{id}/feedback` (strict, bounded, sanitized metadata-only feedback — rating/category/comment — never chat transcript or citation content, idempotent by client-supplied `feedback_id`), and owner-initiated `DELETE /panel/conversations/{id}` (deletes the managed conversation, then panel metadata; a metadata-cleanup failure after a successful system-of-record delete is reported as an explicit `partial` status, never a plain success). Every endpoint is mounted unconditionally so a disabled deployment answers a genuine `503` (never a bare `404`) until `DEPLOY_ADMINISTRATIVE_PANEL`, `PANEL_HISTORY_ENABLED`, and hosted-agent continuity are all active; requires a delegated Entra bearer validated against the new `PANEL_CONVERSATIONS_TOKEN_AUDIENCE` (app-only tokens are rejected with `403`, mirroring gpt-rag-ingestion's `validate_delegated_user_bearer`); every read/feedback/delete is preceded by a Cosmos-only owner-index check (`principal_id == oid`) before the managed-Conversations store is ever touched, and a non-owner or missing conversation returns an identical opaque `404` (no existence oracle). New panel-only Cosmos containers (`panel-conversation-owner-index`, `panel-feedback`, both partitioned by `/principal_id`) carry metadata only — identifiers, titles, timestamps, and feedback fields, never message bodies or document/citation content — and are only ever constructed when the panel is deployed. Pagination uses an opaque, signed, `oid`-bound, expiring cursor (`panel_cursor.py`, reusing the same `CHAINLIT_AUTH_SECRET`-keyed signing pattern already used for download grants and hosted-continuity capabilities); a tampered, expired, or cross-user cursor is rejected with `422`. The BFF-created managed conversation's owner-index row is written by a new optional `on_conversation_created` hook on `HostedContinuityCoordinator`, invoked once per newly created conversation and best-effort (a write failure is logged and never fails the user's turn — the accepted residual risk is a stale/missing panel list row, never content disclosure). All new settings default to fully inert/disabled and the classic and no-panel hosted paths are unaffected.

### Changed
- **Microsoft Foundry hosted/no-panel is now the default chat runtime for fresh deployments:** When `CHAT_BACKEND` is absent or blank, the UI now selects `hosted_agent` and validates its explicit base URL, data-plane scope, finite timeout, signed-in user token path, and OBO confidential-client credentials without falling back to the Container Apps orchestrator or a managed identity. `CHAT_BACKEND=orchestrator` remains an explicit supported fallback. Existing deployments retain classic behavior only through the umbrella deployment's sticky Azure App Configuration selection; the UI does not infer a backend from legacy settings.

### Security
- **Rejected client-presented hosted-continuity conversation handles now fail closed instead of silently starting a new conversation (ADR-0003)**: The initial `delegated`/`capability` owner-binding resolution logic treated *any* rejection of a client-presented conversation reference (`ConversationStoreAccessDeniedError` in `delegated` mode; `ConversationCapabilityError` in `capability` mode) as "start a fresh managed conversation instead", covering a cross-user id, a forged/stolen/guessed id or capability, an expired or retired-key capability, and a genuinely malformed reference identically. This produced a success-shaped response — a normal-looking new conversation and a hosted-agent reply — for what may be an attempted IDOR/BOLA probe against another user's conversation, and let "did I get a fresh conversation?" double as an existence oracle. `hosted_continuity.py` now distinguishes a genuinely *absent* handle (a legitimate new chat, which still creates a conversation) from a *presented* handle that fails validation for any reason, which now raises a single new opaque `ConversationNotFoundError` instead: the coordinator never creates a conversation, never invokes the hosted agent, and never appends anything for that turn, and `app.py` maps this to an explicit not-found failure message rather than an assistant reply. Generic transport/5xx failures (the plain `ConversationStoreError`/`ConversationStoreHTTPError` base classes) are unaffected and continue to surface as explicit dependency/persistence errors, never a 404-equivalent and never a fresh conversation.

## [v2.5.1] - 2026-08-05

### Fixed
- **Hosted-agent calls now use the signed-in user's delegated identity instead of the UI's service identity ([Azure/GPT-RAG#591](https://github.com/Azure/GPT-RAG/issues/591))**: `hosted_agent_client.py` previously acquired a server-side managed-identity token for `HOSTED_AGENT_RESOURCE_SCOPE` and sent it as the `/invocations` bearer, so Microsoft Foundry always saw the Chainlit UI's own service principal as the caller. This broke Toolbox's OAuth identity-passthrough per-user document-level authorization, which requires the actual end user to be visible as the Hosted Agent caller (ADR-0001 in Azure/GPT-RAG freezes native identity passthrough as the required release path; the document-level group-filter fallback cannot be the default). The client now performs a fail-closed server-side on-behalf-of (OBO) token exchange: it takes the authenticated Chainlit user's own Entra access token, exchanges it via MSAL `acquire_token_on_behalf_of` for a delegated token scoped to `HOSTED_AGENT_RESOURCE_SCOPE` using the existing OAuth app registration, and sends that delegated token as the literal `Authorization` bearer on `/invocations` — so Foundry authorizes the request as the actual signed-in user (who must hold the Foundry Agent Consumer role) and can inject the opaque `x-agent-foundry-call-id` correlation header for Toolbox. This is now the default and only implicit behavior (`HOSTED_AGENT_AUTH_MODE=user_delegated`): a missing, empty, or invalid user token raises `HostedAgentAuthenticationError` **before** any network call, with no fallback to managed identity. A `HOSTED_AGENT_AUTH_MODE=service_identity` escape hatch is preserved for deployments that genuinely still need a service-identity caller, but it must be set explicitly — it is never selected implicitly or as a fallback, so issue #591 cannot silently reoccur. Neither the user's access token nor the delegated token is ever placed in request payloads, `x-client-*` headers, or log/exception output; only non-sensitive OBO error codes are logged on failure. The classic `orchestrator` backend is unaffected — it already forwards the user's own token unchanged.
- **`validate_hosted_agent_config()` now fails startup when OBO credentials are missing (follow-up to [Azure/GPT-RAG#591](https://github.com/Azure/GPT-RAG/issues/591))**: startup validation previously only checked `HOSTED_AGENT_BASE_URL`, `HOSTED_AGENT_RESOURCE_SCOPE`, and `HOSTED_AGENT_AUTH_MODE`, so a deployment left in the default `user_delegated` mode without `OAUTH_AZURE_AD_CLIENT_ID` / `OAUTH_AZURE_AD_CLIENT_SECRET` / `OAUTH_AZURE_AD_TENANT_ID` would start successfully and only fail on the first user's chat request when the on-behalf-of exchange ran. `validate_hosted_agent_config()` now also resolves the confidential-client configuration at startup whenever `auth_mode` is (or defaults to) `user_delegated`, raising `HostedAgentConfigError` immediately if any of the three values is missing. The explicit `service_identity` opt-out is unaffected, since it never performs an OBO exchange.

## [v2.5.0] - 2026-07-30

### Added
- **Contract-accurate Chainlit hosted-agent BFF path ([Azure/GPT-RAG#587](https://github.com/Azure/GPT-RAG/issues/587))**: Added an opt-in `CHAT_BACKEND=hosted_agent` mode that sends ordered messages and the managed `conversation_id` to `POST /invocations`, then consumes the hosted orchestrator's Responses SSE text, citation, tool, completed, cancelled, and error frames. The reusable async client obtains credentials only on the server, requires the deployed data-plane scope explicitly, fails fast on configuration or token acquisition, enforces a finite SSE idle timeout, and closes its HTTP and credential resources at shutdown. Caller-controlled thread, token, and identity fields are excluded from the request. The classic `orchestrator` backend remains the default, and invalid backend values fail instead of silently falling back.
- **GitHub Copilot engineering-agent framework:** Reorganized repository guidance into a concise operating contract, UI-specific engineering agents, reusable skills, path-scoped instructions, evidence-oriented pull request guidance, and pinned CI validation for YAML frontmatter. These assets govern repository development and operations only; Chainlit runtime, authentication, feedback, UI, and backend behavior are unchanged.

## [v2.4.0] - 2026-07-17

### Added
- **Secure opt-in Chainlit Copilot embedding ([Azure/GPT-RAG#556](https://github.com/Azure/GPT-RAG/issues/556), [#80](https://github.com/Azure/gpt-rag-ui/pull/80))**: Added explicit `anonymous` and `entra` modes with no default or downgrade, distinct exact-origin enforcement, bounded opaque `HttpOnly` sessions, unique anonymous principals, thread and socket isolation, cleanup on logout or eviction, and principal/session/conversation/blob-bound citation grants. Anonymous mode deliberately denies durable threads, user-bound uploads, and private citation downloads. Entra and standalone OAuth behavior remain independent. The operator guide documents the Shadow DOM widget, bootstrap-only Entra token flow, both configurations, security tradeoffs, private ingress and CSP requirements, and the one-process/one-active-revision/one-replica limitation.

### Fixed
- **Copilot identity binding with Chainlit no-auth sockets:** Bound the already verified Copilot principal only to the new `WebsocketSession` created by Chainlit 2.9.4 when standalone login is disabled and Chainlit leaves `user` unset. Fresh anonymous sockets are distinguished from forbidden session recovery, while existing or restored users are never overwritten, preserving standalone auth behavior and per-session identity isolation.
- **Bound and isolated Copilot Socket.IO transports:** Enforced a fixed limit of four physical Socket.IO connections per opaque Copilot session, explicitly disconnected superseded transports before Chainlit session restoration, and made logout, expiry, eviction, account switch, and same-principal session replacement invalidate all tracked transports and cancel associated active Chainlit tasks. Exact configured portal origins now gate Copilot Engine.IO and Socket.IO admission, tracking, caps, and cleanup, so stale or active Copilot cookies cannot reject, associate, or later disconnect standalone Chainlit transports.
- **Session-bound citation grants with standalone compatibility:** Bound each embedded citation grant to the issuing opaque Copilot session as well as its principal, conversation, container, and blob path, so logout or same-principal session replacement invalidates previously issued grants. Standalone OAuth and anonymous sessions continue using the existing citation path when Copilot embedding is enabled.

## [v2.3.13] - 2026-06-15

### Reverted
- **`opentelemetry-instrumentation-httpx` bump to 0.63b1 ([#67](https://github.com/Azure/gpt-rag-ui/pull/67))** reverted back to `0.52b1`. The bumped version pins `opentelemetry-instrumentation==0.63b1`, which conflicts with the transitive instrumentation versions required by `azure-monitor-opentelemetry==1.6.10` and breaks `pip install -r requirements.txt` in the runtime image. The bump will be re-evaluated when `azure-monitor-opentelemetry` is upgraded.

## [v2.3.12] - 2026-06-15

### Changed
- **Dependency refresh:** Absorbed Dependabot bumps merged into `develop`:
  - `fastapi` requirement updated from `>=0.116.1` to `>=0.137.1` ([#66](https://github.com/Azure/gpt-rag-ui/pull/66))
  - `opentelemetry-instrumentation-httpx` ([#67](https://github.com/Azure/gpt-rag-ui/pull/67))
  - `azure-storage-blob` 12.25.1 → 12.30.0 ([#68](https://github.com/Azure/gpt-rag-ui/pull/68))
  - `tenacity` 9.1.2 → 9.1.4 ([#69](https://github.com/Azure/gpt-rag-ui/pull/69))
  - `aiohttp` 3.13.4 → 3.14.1 ([#70](https://github.com/Azure/gpt-rag-ui/pull/70))

## [v2.3.11] - 2026-06-14

### Changed
- **Warn when `$env:APP_CONFIG_ENDPOINT` diverges from the azd environment during component deploy (issue [Azure/GPT-RAG#491](https://github.com/Azure/GPT-RAG/issues/491)):** `scripts/deploy.ps1` and `scripts/deploy.sh` now read both the shell `APP_CONFIG_ENDPOINT` and the azd env value and, when both are present and disagree (trimmed, case-insensitive), print a yellow warning that shows both values, states which one is being used (the shell value still wins, preserving existing precedence for jumpbox and CI flows), and tells the operator how to clear the shell override (`Remove-Item env:APP_CONFIG_ENDPOINT` in PowerShell, `unset APP_CONFIG_ENDPOINT` in bash). When only one source is set, the previous behavior is unchanged.

## [v2.3.10] - 2026-06-05

### Fixed
- **Forward uploader identity for document ACLs (issue #478):** The ingestion payload built by `ingestion_client.py` now includes the authenticated uploader's object id as `securityUserIds` so the ingestion service can stamp document ACLs. This keeps uploaded documents retrievable by their uploader on permission-trimmed search indexes. Anonymous/placeholder ids are not sent.

## [v2.3.9] - 2026-05-27

### Changed
- **Dependency refresh:** Updated `python-dotenv` to 1.2.2 for runtime configuration loading.

## [v2.3.8] - 2026-05-27

### Changed
- **Dependency refresh:** Updated `aiohttp` to 3.13.4 for the Chainlit/FastAPI runtime dependencies.

All notable changes to this project will be documented in this file.  
This format follows [Keep a Changelog](https://keepachangelog.com/) and adheres to [Semantic Versioning](https://semver.org/).

## [v2.3.7] - 2026-05-26

### Fixed
- **Conversation rename persistence:** Chainlit thread rename events now call the orchestrator `PATCH /conversations/{id}` API with the authenticated user's access token, so renamed conversations keep their new name after page refresh. Fixes [Azure/GPT-RAG#435](https://github.com/Azure/GPT-RAG/issues/435).

## [v2.3.6] - 2026-05-26

### Fixed
- **Azure CLI warning-safe deploy verification**: Filter Azure CLI warning and progress lines from App Configuration, Container Apps update, and image verification output before consuming TSV values, so Windows deploys do not fail when the Azure CLI or Container Apps extension emits non-data output. Fixes [Azure/GPT-RAG#449](https://github.com/Azure/GPT-RAG/issues/449).

## [v2.3.5] - 2026-05-26

### Fixed
- **Container Apps image update verification**: Replaced the mandatory latest-revision restart with explicit image verification after `az containerapp update --image`, avoiding transient `Not Found` failures immediately after revision creation while still confirming the new image is configured. Fixes [Azure/GPT-RAG#449](https://github.com/Azure/GPT-RAG/issues/449).

## [v2.3.4] - 2026-05-25

### Fixed
- **Docker-free component deployment**: Updated Bash and PowerShell deploy scripts to choose the build mode before touching Docker, use `az acr build` when Docker is unavailable or remote build is requested, configure Container App registry identity, and restart the latest revision after image updates. Fixes [Azure/GPT-RAG#449](https://github.com/Azure/GPT-RAG/issues/449).

## [v2.3.3] – 2026-05-25

### Fixed
- **Bash deploy scripts on WSL/Linux**: Added repository line-ending attributes so `*.sh` files are checked out with LF endings, preventing `$'\r': command not found` and `set: pipefail` failures when running `scripts/deploy.sh` or `scripts/preProvision.sh` from WSL. Fixes [Azure/GPT-RAG#451](https://github.com/Azure/GPT-RAG/issues/451).

## [v2.3.2] – 2026-05-19

### Added
- **Per-conversation file upload UI**: Users can now upload files directly from the chat interface using the Chainlit `spontaneous_file_upload` paperclip. Uploaded files are sent to the new `POST /ingest-documents` endpoint in `gpt-rag-ingestion`, which persists the bytes to the `conversation-documents` storage container and indexes them in Azure AI Search tagged with the current `conversationId`, so the orchestrator can answer from them (and cite them) within that conversation. The paperclip is hidden whenever anonymous access is effectively enabled. Implements [Azure/GPT-RAG#401](https://github.com/Azure/GPT-RAG/issues/401). ([#51](https://github.com/Azure/gpt-rag-ui/pull/51))

### Fixed
- **Component deploy with explicit `APP_CONFIG_ENDPOINT`**: `scripts/deploy.ps1` now uses the parsed App Configuration endpoint directly instead of reconstructing it from `RESOURCE_TOKEN`, so direct jumpbox redeploys work even when only `APP_CONFIG_ENDPOINT` is exported.
- **Managed identity authentication in Container Apps**: Azure App Configuration loading no longer passes `"*"` as the default `AZURE_CLIENT_ID`, allowing system-assigned managed identity authentication to work when no user-assigned client ID is configured.
- **PowerShell deploy script encoding on Windows jumpbox**: `scripts/deploy.ps1` is now stored with a UTF-8 BOM so Windows PowerShell 5.1 reads Unicode status messages correctly instead of parsing corrupted script text.
- **ACR remote build log streaming on Windows**: `scripts/deploy.ps1` now forces UTF-8 console/Python output and uses `az acr build --no-logs` so Unicode build output (for example Vite's checkmark) does not break Azure CLI log streaming when running from the jumpbox.
- **Ingestion API key naming**: `ingestion_client.py` now sends the `X-API-KEY` header sourcing from `DATA_INGEST_APP_APIKEY` (with fallback to `INGESTION_APP_APIKEY` and `ORCHESTRATOR_APP_APIKEY`) to match the canonical name produced by the core infra for the `DATA_INGEST_APP` Container App.
- **Upload paperclip gated too narrowly**: `_want_chainlit_spontaneous_file_upload` now enables the paperclip whenever `auth_state.allow_anonymous` is effectively `false`, regardless of whether the value came from env, App Config, or default. The previous version silently hid the upload UI when `ALLOW_ANONYMOUS` was unset even if OAuth was fully configured.

## [v2.3.1] – 2026-03-31

### Fixed
- **Conversation History Listing**: Fixed threads not appearing in the sidebar by improving `_get_session_metadata()` in `datalayer.py` with a secondary fallback via `cl.user_session`, ensuring user metadata is reliably retrieved across all Chainlit context scenarios.
- **ThreadDict Missing Fields**: Added missing `tags` and `steps` fields to `ThreadDict` entries returned by `list_threads`, preventing potential rendering issues in Chainlit's thread sidebar.

### Added
- **Response Time Statistics**: Added optional response time display after each assistant answer, controlled by the `SHOW_STATISTICS` App Configuration setting (default `false`). When enabled, shows elapsed time in seconds (e.g., `⏱ 3.42s`) as subtle light-gray text.
- **Conversation History Diagnostic Logging**: Added detailed logging throughout the `list_threads` flow and orchestrator list conversations API for easier troubleshooting of thread listing issues.

## [v2.3.0] – 2026-03-31

### Added
- **Conversation History**: Implemented full conversation history support allowing users to list, resume, and delete past conversations. Introduced `datalayer.py` with a Chainlit `BaseDataLayer` backed by the orchestrator API, enabling persistent thread management without direct database access.
- **Conversation Resume with Markdown Links**: Source reference links (e.g., `[document](file.pdf)`) are now correctly rendered when resuming past conversations. The `replace_source_reference_links()` transform is applied in `_messages_to_steps()` within the data layer so that Chainlit's native thread resume renders clickable SAS-URL links.
- **Conversation Delete**: Added soft-delete support for conversations via `call_orchestrator_delete_conversation()` in `orchestrator_client.py`, wired through `delete_thread()` in the data layer.
- **Auth Error Toast Suppression**: Added a `MutationObserver` in `footer-version.js` that detects and removes authentication error toasts (e.g., "invalid authentication token") triggered by stale JWT cookies after container restarts or logout.
- **Release Footer**: Added a configurable release footer that displays GPT-RAG and GPT-RAG UI version numbers at the bottom of the chat interface. The footer fetches version data from a new `/version-footer` endpoint and is controlled by the `SHOW_RELEASE_FOOTER` App Configuration setting (default `true`). Missing version values display a descriptive fallback message, and non-prefixed values receive an automatic `v` prefix.
- **Version Footer JavaScript Module**: Introduced `public/footer-version.js`, a self-contained script that creates, positions, and updates the footer element, including layout-aware spacing to prevent overlap with the Chainlit composer input area.
- **Version Footer CSS Styles**: Added footer styling in `public/custom.css` with fixed positioning, responsive font sizing for mobile, and visually subtle divider between the two version labels.

### Changed
- **Login Page Styling**: Centered the login form and refined the login page with a professional "Welcome to GPT-RAG" title (1.1rem, #64748b), rounded button corners, and hover shadow for a polished appearance.
- **OAuth Metadata Enhancement**: Added `principal_id` to the user metadata in `auth_oauth.py` to support secure thread authorization during conversation resume.
- **Application Architecture Refactor**: Restructured `main.py` to use a host `FastAPI` app that mounts both the Chainlit app (`/`) and the blob download sub-app (`/api/download`), enabling top-level routes like `/version-footer` that are independent of the Chainlit middleware stack.
- **VERSION File Reading**: Consolidated duplicated VERSION file reading logic into reusable `_read_local_ui_version()` and `_local_version_file_path()` helpers, eliminating code duplication and improving error handling.

### Fixed
- **Thread Resume Authorization**: Fixed "Authorization for the thread failed" errors when resuming conversations by sourcing `userIdentifier` from session metadata (`metadata.get("user_name")`) instead of the orchestrator conversation document, ensuring it matches Chainlit's internal auth check.

## [v2.2.3] – 2026-03-24

## [v2.2.2] – 2026-03-01
### Added
- Integrated **Low Latency Streaming** compatibility with MAF V2 Orchestrator. The UI now implements native `fetch` with `Transfer-Encoding: chunked`.
- Added reactive buffering UI logic parsing to safely extract 36-char `conversation_id` from the raw byte stream chunk.
### Fixed
- Fixed streaming network fragmentation loss when reading UTF-8 characters via `TextDecoder(stream=True)`.

## [v2.2.1] – 2026-02-04
### Fixed
- Simplified docker image
- Fixed Docker builds on ARM-based machines by explicitly setting the target platform to `linux/amd64`, preventing Azure Container Apps deployment failures.
### Changed
- Pinned the Docker base image to `mcr.microsoft.com/devcontainers/python:3.12-bookworm` to ensure stable package verification behavior across environments.
- Bumped `aiohttp` to `3.13.3`.
- Standardized on the container best practice of using a non-privileged port (`8080`) instead of a privileged port (`80`), reducing the risk of runtime/permission friction and improving stability of long-running ingestion workloads.

## [v2.2.0] – 2026-01-15
### Added
- Added support for Microsoft Entra ID authentication in the UI and forwarding the end-user access token to the orchestrator; this token is used to validate the user and propagate retrieval authorization, enabling document-level security.

## [v2.1.1] – 2025-10-21
### Added
- Added more troubleshooting logs.
### Fixed
- Citations [387](https://github.com/Azure/GPT-RAG/issues/387)

## [v2.1.0] – 2025-08-31
### Added
- User Feedback Loop. [#358](https://github.com/Azure/GPT-RAG/issues/358) 
### Changed
- Standardized resource group variable as `AZURE_RESOURCE_GROUP`. [#365](https://github.com/Azure/GPT-RAG/issues/365)

## [v2.0.2] – 2025-08-18
### Added
- Early Docker validation in the PowerShell deployment script (`deploy.ps1`), including checks for CLI presence, service status, and Docker Desktop availability, with clearer error messages and guidance.

### Fixed
- Orchestrator client (`orchestrator_client.py`) now defaults `ORCHESTRATOR_APP_APIKEY` to an empty string if not set, preventing key errors.
- Dapr API token handling improved: header included only if token is present, with missing token warnings downgraded to debug-level logs.
- Refined error messages for orchestrator invocation failures to clarify the source of errors.
- Improved debug mode toggle handling in the deployment script for clearer output.

## [v2.0.1] – 2025-08-08
### Fixed
- Corrected v2.0.0 deployment issues.

## [v2.0.0] – 2025-07-22
### Changed
- Major architecture refactor to support the vNext architecture.

## [v1.0.0] 
- Original version.