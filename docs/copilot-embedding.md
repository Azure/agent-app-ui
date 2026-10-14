# Embed Agent Landing Zone with Chainlit Copilot

This guide describes the opt-in Chainlit Copilot integration in Agent Landing Zone UI.
The feature supports two explicit authentication modes:

| Mode | Intended use | Identity and persistence |
| --- | --- | --- |
| `anonymous` | A deliberately public or portal-gated assistant that does not require a Agent Landing Zone Entra identity | A unique ephemeral principal per opaque session. Durable threads, user-bound uploads, and citation downloads are disabled. |
| `entra` | A portal where Agent Landing Zone must validate the signed-in user | A delegated Microsoft Entra v2 token is validated during bootstrap and mapped to the user's tenant and object ID. Existing Entra behavior is preserved. |

Embedding is disabled by default. Enabling it without a valid
`CHAINLIT_COPILOT_AUTH_MODE` fails startup. The service never infers anonymous
mode from missing Entra configuration and never downgrades an Entra failure to
anonymous access.

Entra Copilot upload controls also require the independent
`DEPLOY_INGESTION_CONTAINER_APP` capability read through the existing App
Configuration provider. A missing setting preserves legacy upload eligibility;
`true` keeps the existing Entra and conversation checks. `false` disables the
controls and server-side attachment processing, including ingestion URL/Dapr
fallbacks, with “Attachments are unavailable in this deployment.” Malformed
boolean values fail with an actionable configuration error. Anonymous Copilot
uploads remain disabled regardless of this flag. Text chat, authentication and
retrieval are unchanged; no native conversation upload path is introduced.

## Rendering and token flow

Chainlit 2.12.0 Copilot mode renders a floating widget in an open Shadow DOM. It
is loaded by a script and does not use an iframe. Embedding therefore does not
require relaxing `frame-ancestors` or `X-Frame-Options` on the Agent Landing Zone UI
origin. Do not weaken either protection for this widget; they continue to
protect the standalone Agent Landing Zone UI page from clickjacking.

The portal first calls Agent Landing Zone's bootstrap endpoint and then mounts the widget.
In Entra mode, the portal sends its delegated API access token only to:

```text
POST https://chat.contoso.com/copilot/auth/bootstrap
```

Agent Landing Zone validates the token and exchanges it for an opaque server-side session.
The portal must not pass the Entra token as a widget `accessToken`, and
`mountChainlitWidget` is called without one. Anonymous bootstrap sends no
`Authorization` header.

Both modes return a bounded opaque cookie with `Secure` and `HttpOnly`. The
cookie is not an Entra identity, cannot be read by browser JavaScript, and is
valid only while the corresponding process-local server session exists.

## Required topology

Use distinct exact origins for the Agent Landing Zone UI and embedding portal:

```text
Agent Landing Zone UI: https://chat.contoso.com
Portal:     https://portal.contoso.com
```

Same-origin path multiplexing is intentionally unsupported. When embedding is
enabled:

- `CHAINLIT_URL` must be the exact externally visible Agent Landing Zone UI origin.
- Every value in `CHAINLIT_ALLOWED_ORIGINS` must be an exact portal origin.
- `CHAINLIT_URL` cannot also appear in `CHAINLIT_ALLOWED_ORIGINS`.
- A non-empty `CHAINLIT_ROOT_PATH` is rejected.
- Wildcards, `null`, credentials, paths, query strings, fragments, and
  non-local HTTP origins are rejected.

This separation prevents an opaque embedded cookie from changing standalone
OAuth or anonymous policy. A normal HTTP request is treated as embedded only
when it has an exact configured portal `Origin`. A cookie by itself does not
select embedded authentication.

### Network boundary

Exact-origin enforcement is a browser policy, not authentication. A direct HTTP
client can set a matching `Origin` header without loading the portal. In
`anonymous` mode, anyone who can reach the Agent Landing Zone UI endpoint can create an
ephemeral session and consume model and search capacity.

Use Entra mode unless an authenticated gateway or a private network boundary
authoritatively restricts access before traffic reaches Agent Landing Zone. A Zero Trust or
private Container Apps deployment must remain private and be exposed through
the environment's existing authenticated front door or gateway. Never enable
public ingress solely to make the widget reachable.

The [canonical Agent Landing Zone documentation](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/) remains
the source for architecture and infrastructure deployment. This repository
guide defines the widget's application contract; it does not replace ingress,
gateway, WAF, or private networking controls owned by the Agent Landing Zone
infrastructure.

Browsers normally omit `Origin` on top-level download navigation. The secure
download endpoint has a narrow exception: it rechecks the opaque session and a
signed grant bound to the principal, session, conversation, container, and blob
path. Anonymous downloads are denied.

## Configuration

Configuration may come from environment variables or the existing application
configuration source. Environment variables take precedence.

| Setting | Required | Description |
| --- | --- | --- |
| `CHAINLIT_COPILOT_ENABLED` | No | Defaults to `false`. Set to `true` to enable embedding. |
| `CHAINLIT_COPILOT_AUTH_MODE` | When enabled | Must be exactly `anonymous` or `entra`. |
| `CHAINLIT_URL` | When enabled | Exact HTTPS origin serving Agent Landing Zone UI, with no path. |
| `CHAINLIT_ALLOWED_ORIGINS` | When enabled | Comma-separated exact portal origins. The UI origin is not allowed here. This is a CORS-style browser control, not authentication or a network access boundary. |
| `CHAINLIT_COOKIE_SAMESITE` | No | `lax` by default. Use `none` only when a cross-site portal requires it; HTTPS is mandatory. |
| `CHAINLIT_COPILOT_SESSION_TTL_SECONDS` | No | Maximum opaque-session lifetime. The session also expires when its Entra token expires. |
| `CHAINLIT_COPILOT_MAX_SESSIONS` | No | Process-local bound on active embedded sessions. |
| `CHAINLIT_COPILOT_BOOTSTRAP_RATE_LIMIT_PER_MINUTE` | No | Process-local bootstrap limit. Add authoritative gateway throttling for distributed enforcement. |
| `CHAINLIT_COPILOT_ENTRA_TENANT_ID` | Entra | Tenant GUID expected in the token. |
| `CHAINLIT_COPILOT_ENTRA_AUDIENCE` | Entra | Agent Landing Zone API audience expected in the token. |
| `CHAINLIT_COPILOT_ENTRA_REQUIRED_SCOPE` | No | Delegated scope expected in `scp`; defaults to `user_impersonation`. |

`CHAINLIT_AUTH_SECRET` remains required by Chainlit and must be an
operator-managed secret of at least 32 UTF-8 bytes. Store it in Key Vault or the
repository's existing secret mechanism. Do not commit it.

### Anonymous example

```text
CHAINLIT_COPILOT_ENABLED=true
CHAINLIT_COPILOT_AUTH_MODE=anonymous
CHAINLIT_URL=https://chat.contoso.com
CHAINLIT_ALLOWED_ORIGINS=https://portal.contoso.com
CHAINLIT_COPILOT_SESSION_TTL_SECONDS=1800
CHAINLIT_COPILOT_MAX_SESSIONS=1000
CHAINLIT_COPILOT_BOOTSTRAP_RATE_LIMIT_PER_MINUTE=60
```

Choosing anonymous mode is a security decision. `CHAINLIT_ALLOWED_ORIGINS`
stops browser pages on unlisted origins from calling Agent Landing Zone, but a non-browser
client can forge a matching `Origin` header. Anyone who can reach the Agent Landing Zone UI
endpoint can create an ephemeral assistant session, whether or not they can
load the portal. The portal's own authentication does not protect the Agent Landing Zone
endpoint unless an authenticated gateway or private network boundary enforces
that identity before forwarding the request. Prefer Entra mode when Agent Landing Zone
must authenticate each user.

### Entra example

```text
CHAINLIT_COPILOT_ENABLED=true
CHAINLIT_COPILOT_AUTH_MODE=entra
CHAINLIT_URL=https://chat.contoso.com
CHAINLIT_ALLOWED_ORIGINS=https://portal.contoso.com
CHAINLIT_COPILOT_ENTRA_TENANT_ID=11111111-1111-4111-8111-111111111111
CHAINLIT_COPILOT_ENTRA_AUDIENCE=api://22222222-2222-4222-8222-222222222222
CHAINLIT_COPILOT_ENTRA_REQUIRED_SCOPE=user_impersonation
```

The token validator checks the RS256 signature, exact v2 issuer and token
version, audience, tenant, delegated scope, expiry, `tid`, and `oid`. Agent Landing Zone
does not add a new portal-client `azp` allow-list requirement, so existing
validated Entra deployments retain their behavior.

## Portal integration

Load the Copilot bundle from the Agent Landing Zone origin. Bootstrap first, verify that
the browser accepted the cookie, and mount only after bootstrap succeeds.

This is a script/widget integration, not an iframe integration. The portal's
Content Security Policy must allow the Agent Landing Zone UI origin in `script-src` and
`connect-src`, including its secure WebSocket endpoint. For example:

```text
Content-Security-Policy:
  script-src 'self' https://chat.contoso.com;
  connect-src 'self' https://chat.contoso.com wss://chat.contoso.com;
```

Keep the portal's other CSP directives intact. Do not loosen
`frame-ancestors` or `X-Frame-Options` on the portal or Agent Landing Zone UI for this
widget.

```html
<div id="agent-lz-status" role="status">Loading assistant...</div>
<script>
  const chainlitServer = "https://chat.contoso.com";

  async function bootstrapAssistant(accessToken) {
    const headers = accessToken
      ? { Authorization: `Bearer ${accessToken}` }
      : {};
    return fetch(`${chainlitServer}/copilot/auth/bootstrap`, {
      method: "POST",
      credentials: "include",
      headers,
    });
  }

  async function loadCopilotBundle() {
    if (typeof window.mountChainlitWidget === "function") return;
    await new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = `${chainlitServer}/copilot/index.js`;
      script.onload = resolve;
      script.onerror = reject;
      document.head.appendChild(script);
    });
  }

  async function startAssistant({ accessToken } = {}) {
    const status = document.getElementById("agent-lz-status");
    const response = await bootstrapAssistant(accessToken);
    if (!response.ok) {
      status.textContent =
        response.status === 403
          ? "You do not have access to this assistant."
          : "The assistant is temporarily unavailable.";
      return;
    }

    const probe = await fetch(`${chainlitServer}/project/settings`, {
      credentials: "include",
      headers: { Accept: "application/json" },
    });
    if (!probe.ok) {
      status.textContent =
        "The browser did not establish the assistant session.";
      return;
    }

    await loadCopilotBundle();
    window.mountChainlitWidget({
      chainlitServer,
      theme: "light",
    });
    status.hidden = true;
  }

  // Anonymous:
  // startAssistant();
  //
  // Entra:
  // const token = await portalAuth.getAgentAccessToken();
  // startAssistant({ accessToken: token });
</script>
```

Do not call anonymous bootstrap as a fallback after Entra bootstrap fails. A
`401` means the token is absent, expired, or invalid. A `403` means the origin
or authorization policy denied access. A `429` should honor `Retry-After`.

## Origin and transport enforcement

The exact portal origin is enforced for bootstrap, embedded HTTP requests,
Socket.IO polling and upgrades, and WebSockets. Browser bootstrap with a
missing, `null`, duplicate, malformed, or unlisted `Origin` is denied. Referer
and forwarded headers never establish embedded trust.

The reverse proxy must preserve:

- `/copilot/index.js`
- `/copilot/auth/bootstrap`
- `/copilot/auth/logout`
- `/project/settings`
- `/ws/socket.io` and Socket.IO polling or upgrade requests
- `/api/download/{grant_token}`

Forward WebSocket upgrades and keep session affinity. The current implementation
stores sessions, sockets, and task registries in process memory, so the supported
deployment contract is one Uvicorn process, one active Container Apps revision,
and one replica.

## Safe rollout and rollback

Process-local sessions require Container Apps Single revision mode with exactly
one active revision and one replica. Keep the existing private ingress and
authenticated gateway in place.

1. Configure the Copilot settings and one-replica limit, then deploy the new
   revision without adding the widget to the production portal.
2. Use a staged browser or staged portal on an allow-listed origin to validate
   bootstrap, cookie delivery, CSP, WebSocket connectivity, logout, expiry, and
   Entra rejection behavior.
3. Add the widget script to the production portal only after that validation
   succeeds.

To roll back, remove or disable the portal widget first so browsers stop
creating sessions. Then set `CHAINLIT_COPILOT_ENABLED=false` and deploy the
configuration or previous image through the normal Single revision flow. A
restart, revision switch, or rollback terminates active embedded sessions and
invalidates their opaque cookies and download grants; those sessions are not
restored.

## Session and thread isolation

Anonymous bootstrap creates a random unique principal. It does not attach an
Entra token, tenant groups, or a reusable user identity. Anonymous requests are
denied access to identity-bound and persistence routes, including feedback,
MCP, project actions, elements, files, sharing, and thread APIs.

Each opaque session is bounded by TTL and the configured process capacity. A
single session may have at most four admitted physical Socket.IO transport
connections. Expiry, eviction, logout, or account reset invalidates registered
sockets and tasks. Anonymous sessions cannot resume a previous anonymous
session or another user's thread.

Entra sessions retain the canonical tenant/object principal and existing
thread ownership checks. Embedded user responses remove the internal opaque
session marker before returning metadata to browser code.

## Downloads and citations

The UI never exposes a direct unrestricted private blob route. Citation links
use absolute, signed, short-lived grants. Redemption rechecks:

- the active opaque session;
- the canonical principal;
- the embedded session identifier;
- the conversation or thread;
- the allowed container; and
- the exact blob path.

Anonymous mode denies citation download redemption because it has no durable
authenticated identity. Entra and standalone downloads remain separated by
their respective authentication policy.

## Logout, account switching, and expiry

On logout or account switch:

1. Unmount the widget and remove its DOM element.
2. Remove `chainlit-copilot-thread-id` from local storage.
3. Call `POST /copilot/auth/logout` with `credentials: "include"`.
4. Clear portal-held Entra state as appropriate.
5. Bootstrap and mount a new session only after the new account is ready.

The embedded native `/logout` path also clears the opaque session. Both paths
invalidate sockets, registered tasks, and session-owned thread state through
the session cleanup callback. Standalone `/logout` is unchanged because a
request without the exact portal origin remains under standalone policy.

Treat HTTP `401`, WebSocket `4401`, or Socket.IO authentication failure as an
expired assistant session. Unmount first, clear local thread state, and make at
most one controlled bootstrap attempt. In Entra mode acquire a fresh token; in
anonymous mode send no token.

## Operational limitations

- The implementation is pinned and tested against Chainlit 2.12.0.
- Session, rate-limit, socket, and task state is process-local.
- Restarts, revision switches, or capacity eviction sign embedded users out.
- Cross-site embedding requires `SameSite=None; Secure`, but browser
  third-party-cookie policy can still block the session.
- Anonymous mode is intentionally non-durable and cannot download private
  citations.
- Exact-origin checks are not authentication and are not a substitute for
  private ingress, an authenticated gateway, WAF policy, or CSP.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Startup fails | Embedding has an explicit mode, origins are distinct and exact, and `CHAINLIT_ROOT_PATH` is empty. |
| Bootstrap 403 | The browser `Origin` exactly matches `CHAINLIT_ALLOWED_ORIGINS`; in Entra mode also check tenant, audience, delegated scope, and user policy. |
| Bootstrap 401 | The Entra token is present, unexpired, and issued as a v2 delegated token for the configured Agent Landing Zone audience. |
| Bootstrap succeeds but widget requests return 401 | The browser may have rejected the cookie. Check HTTPS, `CHAINLIT_COOKIE_SAMESITE`, and third-party-cookie policy. |
| Portal blocks the bundle or socket | Allow the Agent Landing Zone UI origin in the portal's `script-src` and `connect-src`, including `wss://`; do not change framing protections. |
| Socket connects then closes | Verify exact origin, cookie delivery, affinity, WebSocket upgrade forwarding, and the one-process/one-active-revision/one-replica contract. |
| Anonymous thread or download returns 403 | This is intentional. Anonymous mode denies durable identity-bound state and private citation downloads. |
| Standalone OAuth behaves unexpectedly | Ensure the portal origin differs from `CHAINLIT_URL`; a cookie alone must not select embedded policy. |
