"""Short-lived, encrypted, principal/session-bound interactive recovery.

No challenge registry, model-turn replay, provider hints, or claims in user metadata.
"""

import base64
import hashlib
import json
import os
import logging
import secrets
import time
import zlib
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlsplit

import chainlit as cl
from cryptography.fernet import Fernet, InvalidToken

from gpt_rag_ui.auth.auth_common import canonical_principal_id, validated_claims_challenge

RECOVERY_TTL = 180
CALLBACK_PATH = "/auth/oauth/azure-ad/callback"
recovery_context: ContextVar[dict[str, Any] | None] = ContextVar("auth_recovery", default=None)


class RecoveryLogFilter(logging.Filter):
    """Prevent SDK bodies, auth codes and authorize Location from becoming logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith("msal"):
            return False
        if record.name in {"uvicorn.access", "httpx"} and any(
            path in record.getMessage() for path in
            ("/auth/oauth/azure-ad", "/api/auth/recovery", "/copilot/auth/recovery")
        ):
            record.msg = "Authentication HTTP request completed (details redacted)"
            record.args = ()
        return True


def install_recovery_log_protection() -> None:
    protection = RecoveryLogFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(protection)
    for name in {*logging.Logger.manager.loggerDict, "msal", "uvicorn.access", "httpx"}:
        if name.startswith("msal") or name in {"uvicorn.access", "httpx"}:
            logging.getLogger(name).addFilter(protection)


class RecoveryError(ValueError):
    def __init__(self) -> None:
        super().__init__("Sign-in recovery expired or is not available for this session. Start a fresh sign-in.")


def _cipher() -> Fernet:
    secret = os.environ.get("CHAINLIT_AUTH_SECRET", "")
    if len(secret.encode()) < 32:
        raise RecoveryError()
    key = hashlib.sha256(b"alz-ui-auth-recovery-v1\0" + secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def seal_context(payload: dict[str, Any], *, purpose: str) -> str:
    data = json.dumps({**payload, "purpose": purpose, "exp": int(time.time()) + RECOVERY_TTL},
                      separators=(",", ":"), allow_nan=False).encode()
    if len(data) > 32768:
        raise RecoveryError()
    return _cipher().encrypt(zlib.compress(data)).decode("ascii")


def open_context(ticket: str, *, purpose: str) -> dict[str, Any]:
    if not isinstance(ticket, str) or not ticket or len(ticket) > 48000:
        raise RecoveryError()
    try:
        raw = _cipher().decrypt(ticket.encode("ascii"), ttl=RECOVERY_TTL)
        decoder = zlib.decompressobj()
        data = decoder.decompress(raw, 32769)
        if len(data) > 32768 or not decoder.eof or decoder.unused_data:
            raise RecoveryError()
        payload = json.loads(data)
        if (not isinstance(payload, dict) or payload.get("purpose") != purpose
                or not isinstance(payload.get("exp"), int) or payload["exp"] <= time.time()):
            raise RecoveryError()
        return payload
    except (InvalidToken, UnicodeError, zlib.error, ValueError, RecursionError):
        raise RecoveryError() from None


def _binding(metadata: dict[str, Any]) -> tuple[str, str, str]:
    if metadata.get("authorized") is not True:
        raise RecoveryError()
    principal = canonical_principal_id(metadata.get("tenant_id", ""), metadata.get("object_id", ""))
    if metadata.get("principal_id") != principal:
        raise RecoveryError()
    source = metadata.get("auth_source")
    if source == "oauth":
        binding = metadata.get("recovery_binding", "")
        mode = "oauth"
    elif source == "copilot_session" and metadata.get("copilot_auth_mode") == "entra":
        binding = metadata.get("copilot_session_id", "")
        mode = "entra"
    else:
        raise RecoveryError()
    if not isinstance(binding, str) or len(binding) < 32:
        raise RecoveryError()
    return principal, hashlib.sha256(binding.encode()).hexdigest(), mode


def issue_recovery(metadata: dict[str, Any], code: str, claims: object) -> str:
    if code not in {"interaction_required", "authentication_required"}:
        raise RecoveryError()
    validated = validated_claims_challenge(claims)
    if (claims is not None and validated is None) or (code == "interaction_required" and not validated):
        raise RecoveryError()
    principal, binding, mode = _binding(metadata)
    return seal_context({"p": principal, "b": binding, "mode": mode, "claims": validated}, purpose="recovery")


def verify_recovery(ticket: str, metadata: dict[str, Any]) -> dict[str, Any]:
    payload = open_context(ticket, purpose="recovery")
    principal, binding, mode = _binding(metadata)
    if (payload.get("p"), payload.get("b"), payload.get("mode")) != (principal, binding, mode):
        raise RecoveryError()
    claims = payload.get("claims")
    if claims is not None and validated_claims_challenge(claims) is None:
        raise RecoveryError()
    return payload


def configured_public_url() -> str:
    # Never derive redirects from Host/Forwarded, source challenges, or a request parameter.
    from gpt_rag_ui.auth.oauth import get_env_var
    value = (os.environ.get("CHAINLIT_URL") or get_env_var("CHAINLIT_URL") or "").rstrip("/")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise RecoveryError()
    return value


async def offer_auth_recovery(code: str, claims: object, *, user: cl.User | None) -> bool:
    """Offer an explicit action, without exposing plaintext challenge in message persistence."""
    if user is None:
        return False
    try:
        ticket = issue_recovery(user.metadata or {}, code, claims)
        mode = _binding(user.metadata or {})[2]
        href = configured_public_url() + "/api/auth/recovery?ticket=" + ticket if mode == "oauth" else ""
    except (RecoveryError, ValueError):
        return False
    await cl.Message(
        content="Additional sign-in is required. Continue sign-in, then submit a new request. The failed request will not be replayed.",
        elements=[cl.CustomElement(name="AuthRecovery", props={"ticket": ticket, "href": href}, display="inline")],
    ).send()
    return True


def register_recovery_action() -> None:
    @cl.action_callback("continue_protected_signin")
    async def continue_signin(action: cl.Action) -> dict[str, str]:
        from gpt_rag_ui.auth.embed_auth import is_copilot_session_active
        user = cl.user_session.get("user")
        try:
            if not user or not await is_copilot_session_active(user.metadata or {}):
                raise RecoveryError()
            payload = action.payload or {}
            ticket = payload.get("ticket", "")
            context = verify_recovery(ticket, user.metadata or {})
            if context["mode"] != "entra":
                raise RecoveryError()
        except (ValueError, TypeError):
            return {"status": "unavailable"}
        # This notification only installs the portal's explicit sign-in control.
        # No tokens/claims cross the call_fn protocol or get persisted here.
        import asyncio
        try:
            result = await asyncio.wait_for(cl.CopilotFunction(
                name="protectedAuthRecovery", args={"ticket": ticket},
            ).acall(), timeout=10)
        except TimeoutError:
            return {"status": "portal_unavailable"}
        return {"status": "ready" if isinstance(result, dict) and result.get("status") == "ready" else "portal_unavailable"}


def new_recovery_binding() -> str:
    return secrets.token_urlsafe(32)
