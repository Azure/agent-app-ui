"""Delegated-user bearer validation for the panel's user-facing surfaces
(issue #611, ADR-0004).

Every panel endpoint requires a delegated Entra user bearer -- never a
client-supplied header or claim -- validated exactly like the reference
fail-closed pattern in gpt-rag-ingestion's ``validate_delegated_user_bearer``
(``POST /retrieve``): RS256 JWKS signature verification, issuer/audience/
tenant checks (reusing ``entra_token.EntraTokenValidator``, already used for
embedded-portal bootstrap tokens in this repo), plus an explicit rejection
of app-only tokens (no ``scp`` delegated-scope claim, or an ``idtyp`` other
than ``user``).

The **only** identity ever produced here is the validated ``oid`` --  it is
never taken from a header, a query string, or any other caller-supplied
value. This is the sole authenticated source of the owner principal for
every panel route.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import Request

from gpt_rag_ui.auth.auth_common import normalize_guid
from gpt_rag_ui.auth.entra_token import EntraTokenError, EntraTokenValidator


class PanelAuthError(ValueError):
    """Raised when the bearer is missing or fails signature/issuer/audience
    validation. Route handlers must map this to HTTP 401."""


class PanelForbiddenError(ValueError):
    """Raised when the bearer is well-formed and trusted but is not a
    delegated user token (app-only / missing required scope / wrong
    ``idtyp``). Route handlers must map this to HTTP 403."""


@dataclass(frozen=True)
class PanelPrincipal:
    """A validated panel caller identity. ``oid`` is the only field used as
    an authorization key anywhere downstream; it is never mixed with any
    caller-supplied identifier."""

    oid: str
    tenant_id: str
    access_token: str = field(repr=False)
    name: str = field(default="", repr=False)


def _bearer_token_from_request(request: Request) -> str:
    header = request.headers.get("Authorization") or ""
    if not header.lower().startswith("bearer "):
        raise PanelAuthError("Missing or malformed bearer token.")
    token = header[len("Bearer "):].strip()
    if not token:
        raise PanelAuthError("Missing bearer token.")
    return token


async def validate_panel_bearer(
    request: Request,
    validator: EntraTokenValidator,
) -> PanelPrincipal:
    """Validate the caller's delegated bearer and return its validated oid.

    Raises ``PanelAuthError`` (401) for a missing/invalid/expired/wrong-
    audience-or-issuer/wrong-signing-key token, and ``PanelForbiddenError``
    (403) for a well-formed, trusted token that is not a delegated user
    token: missing the validator's required delegated scope (``scp``,
    the app-only case) or carrying an ``idtyp`` other than ``user``.

    ``validator`` must already be constructed with the panel's expected
    audience/tenant (``PANEL_CONVERSATIONS_TOKEN_AUDIENCE`` /
    ``PANEL_CONVERSATIONS_TENANT_ID``, see ``panel_config.py``) and its
    required delegated scope; this function only adds the 401-vs-403
    distinction and the idtyp check on top of the shared validator.
    """
    token = _bearer_token_from_request(request)
    try:
        claims = await validator.validate(token)
    except EntraTokenError as exc:
        message = str(exc)
        # entra_token.EntraTokenValidator raises the same exception type for
        # every failure. Only its required-delegated-scope rejection (the
        # app-only-token case) is a 403; every other failure (bad
        # signature, wrong audience/issuer/tenant, expired, untrusted
        # signing key, ...) is a 401.
        if "required" in message and "delegated scope" in message:
            raise PanelForbiddenError(message) from exc
        raise PanelAuthError(message) from exc

    identity_type = claims.get("idtyp")
    if identity_type is not None and identity_type != "user":
        raise PanelForbiddenError("Delegated user token required.")

    try:
        oid = normalize_guid(claims.get("oid"), claim_name="oid")
        tenant_id = normalize_guid(claims.get("tid"), claim_name="tid")
    except ValueError as exc:
        raise PanelForbiddenError("Token missing a valid user object id.") from exc

    return PanelPrincipal(
        oid=oid, tenant_id=tenant_id, access_token=token,
        name=str(claims.get("preferred_username") or claims.get("upn") or ""),
    )
