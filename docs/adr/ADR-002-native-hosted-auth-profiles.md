# ADR-002: Native Hosted outbound authentication profiles

- Status: Accepted (approved product plan; component implementation)
- Date: 2026-11-02
- Owners: Agent Landing Zone UI

## Context and decision

`HOSTED_AGENT_AUTH_MODE` remains the sole Hosted authentication selector in
the current App Configuration provider: `user_delegated` (default) or
`service_identity`. Both require an authenticated, admitted Entra tenant user
at the BFF. This changes outbound credentials, not user admission or ownership.

Reuse the official Responses client and existing asynchronous credential
helpers, with the exact published base URL/scope, bounded acquisition and SDK
timeouts, no retries or fallback, and no fabricated trusted-user headers.
Workload failures never initiate user consent. Structural configuration and
publication fingerprints include the selected authentication profile.

## Priorities and alternatives

1. Isolation: owner `tenant:oid`, UUID selectors and publication checks precede
   every native operation; missing metadata denies access.
2. Compatibility: delegated OBO remains the default; no new public flags,
   persistence stack, enabled capabilities or transcript replay.
3. Operability: mode changes require redeployment, not a hot switch.

A separate service-only client would duplicate protocol and security behavior.
Treating a workload as the user, or falling back from OBO, would violate
authorization. No change would leave the approved common-corpus profile
unsupported.

## Sources and downloads

Platform/runtime composition does not restrict service mode to Blob or impose
a source-family allowlist. Each source enforces its supported authentication
and permissions. Missing required delegation fails at invocation, without a
credential fallback. The UI does not infer source authorization from a citation
or invent a parallel source registry.

Common-source downloads use the selected credential behind the existing signed
principal/session/citation grants, fixed account and container allowlist.
Changing the live profile fails closed. Service mode rejects the protected
conversation-upload corpus before acquiring credentials; it never falls back
to delegated source access. Delegated mode retains fresh Search authorization
and exact document/uploader checks before workload Blob reads. Private inline
turn attachments remain owner-bound and unchanged; they are not corpus uploads.

## Adoption, recovery and consequences

Deploy compatible platform/runtime authorization handling before enabling service
mode. New Hosted publication fingerprints intentionally invalidate pre-change
owner mappings, including delegated mappings; start new sessions rather than
migrating native IDs across caller partitions. Profile, scope, endpoint,
tenant or BFF audience changes cannot reopen another publication's sessions.
Existing metadata is retained; no transcripts or tokens are copied.

Rollback requires a compatible runtime, delegated configuration and UI
redeployment. Previously inaccessible sessions are not migrated automatically.
Live Azure permissions, native runtime behavior and deployment readiness are
not established by offline UI tests.

## Managed Toolbox outcomes

The official runtime's managed Foundry Toolbox resolves downstream user
authorization from its per-request platform context. UI-to-Foundry OBO remains
separate: the UI neither acquires a Search assertion for Toolbox nor fabricates
trusted identity or call-ID headers. Existing direct protected download
authorization is a separate boundary and is unchanged.

An outward Responses `oauth_consent_request` is an explicit `consent_required`
denial for delegated requests, not successful empty text or a workflow approval.
The UI tells the authenticated user to complete the approved Foundry consent
process (or obtain administrator consent), return and retry. It suppresses
**all** returned consent URLs: no trusted initial consent origin/path binding is
defined in this component. It does not open a window, confirm consent, implement
`/consent/done`, or automatically resume/replay a request. Service-identity
consent requests return `403 user_delegation_required` with operator guidance.
Other explicit failed tool outcomes are safe `tool_failed` dependency failures,
never a reason to exchange another credential. Both API and chat retain safe
service-permission and OBO guidance without rendering provider error bodies.

The native response ID is committed only to the existing owner/publication
mapping before surfacing a received consent/tool failure. Consent URLs and
provider error bodies are not copied into UI metadata, UI responses or messages.
History and progress checks refuse these outcomes before rendering content.
An explicit later user turn still requires the same owner, session and
publication checks; it is not proof that consent succeeded.

## Verification, documentation and review

Focused `test_native_ui.py` and `test_native_downloads.py` cover both
credentials, no service OBO/fallback, actual SDK payloads, ownership,
unauthenticated requests, invalid/contradictory settings, publication changes,
source grants and private-upload isolation. Existing native upload and
presentation fixtures remain applicable. Static checks use existing tooling.

The parent owns central product documentation under
`Azure/AI-Landing-Zones/docs/agent-landing-zone`; this record is internal.
Revisit this decision before adding trusted-user headers,
hot profile changes or a different conversation system of record.
