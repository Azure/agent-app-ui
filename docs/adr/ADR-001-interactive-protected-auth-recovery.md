# ADR-001: Request-local protected authentication recovery

Status: accepted for local implementation by the protected-retrieval task.
Platform policy and runtime typed-error decisions remain parent-owned.

## Context and decision

Configured Search can require interactive Conditional Access after UI login.
The authenticated hosted stream supplies a safe code and bounded `access_token`
claims JSON, not redirects, scopes, authority, user identifiers, or provider text.
Sign-out guidance alone cannot satisfy that challenge.

Use authenticated encryption under a domain-separated key derived from the
existing `CHAINLIT_AUTH_SECRET`. Recovery tickets and OAuth flow cookies expire
after 180 seconds and contain no downstream tokens. Bind them to the verified
principal and the current OAuth login binding or embedded opaque session.
Use encrypted, compressed, bounded cookies for MSAL state/nonce/PKCE rather than
a new process-local claims registry. Compression precedes authenticated encryption;
decoded context is bounded to 32 KiB.

Standalone Azure login and recovery share the existing registered callback URI.
Use the configured tenant and UI scopes, Chainlit's state/auth cookie helpers,
and request-local MSAL auth-code flow plus callback refresh exchange. This avoids
the installed provider's mutable refresh-token slot and Graph user-info call.
Check the token-endpoint authenticated identity on both exchanges; rotate the
login binding on completion. Do not mutate global OAuth provider settings.

Embedded recovery stays portal/MSAL/bootstrap-based. An encrypted-ticket
`call_fn` notification prepares a real portal sign-in button. The portal acquires
the configured UI token with claims, then the existing validator and session
store verify the same principal/current session and rotate it atomically.
The bundled helper requires memory-only MSAL caches, no auth-state cookie,
PII logging disabled, and the same active account. It clears widget thread
selection and remounts without sending a message. Anonymous mode cannot recover.

## Alternatives

- Sign-out-only guidance: rejected; does not transmit the source challenge.
- Process-local claims registry: rejected; adds replica affinity and state.
- Per-user mutation of Chainlit provider settings: rejected; cross-user races.

## Invariants, adoption and rollback

No turn, tool, partial output, or download is automatically replayed. Ordinary
source denial does not initiate interaction. Claims never enter chat/history,
user metadata, plaintext recovery links, diagnostic headers, logs, or telemetry.
Plaintext claims appear only in the intentional configured Entra protocol and
authenticated embedded handshake; the helper handles them transiently in memory.

Existing OAuth sessions without a binding must start an ordinary login.
Recovery selects the configured tenant even where older login used `common`.
Deploy the matching runtime typed-error contract before testing recovery.
Rollback restores the previous UI image; secret rotation invalidates all tickets
and existing Chainlit cookies. No new Azure resource or redirect registration.

Fitness checks: local ASGI plus real pinned MSAL with mocked token HTTP; signed
local embedded tokens; negative state/session/account/expiry tests; partial
stream no-replay tests; isolated headless portal-helper checks. Live Entra/CA
and proxy/browser deployment acceptance are explicitly deferred.

Review on Chainlit/MSAL upgrade, callback URI/root-path changes, or identity/
session changes. Embedded session affinity remains the existing limitation.
