---
applyTo: "app.py,main.py,auth_common.py,auth_oauth.py,entra_token.py,embed_auth.py,embed_config.py,embed_security.py,conversation_security.py,download_security.py,src/gpt_rag_ui/auth/**/*.py,src/gpt_rag_ui/api/**/*.py,src/gpt_rag_ui/bootstrap.py,src/gpt_rag_ui/config/embed_config.py,src/gpt_rag_ui/services/chat.py,src/gpt_rag_ui/services/citations.py,src/gpt_rag_ui/services/conversation_security.py,src/gpt_rag_ui/services/download_security.py,tests/test_app_citations.py,tests/test_auth_common.py,tests/test_entra_token.py,tests/test_embed_*.py,tests/test_download_security.py,tests/test_main_policy.py"
---

# Authentication and session security rules

- Preserve standalone OAuth, embedded Entra, and embedded anonymous policy as
  explicit, separate modes with no implicit downgrade.
- Keep tenant, audience, scope, lifetime, principal, state, origin, cookie,
  transport, thread, conversation, citation, and download checks at their
  established boundaries.
- A matching origin is not authentication. Do not weaken private-ingress,
  gateway, CSP, session, or authorization requirements.
- Never expose tokens, cookies, claims with personal data, signing secrets, or
  authorization headers in logs, errors, tests, examples, or fixtures.
- Logout, expiry, eviction, replacement, and account switching must revoke all
  associated session and transport state.
- Every security change requires negative tests for unauthorized,
  cross-principal, cross-session, malformed, missing, duplicate, expired, or
  disallowed input as applicable.
- Keep the central [embedding guide](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/embed-chat/)
  synchronized with embedding security behavior. Its source is
  `Azure/AI-Landing-Zones` on `main` under `docs/agent-landing-zone`.
  Internal security policies and runtime ownership remain local.
