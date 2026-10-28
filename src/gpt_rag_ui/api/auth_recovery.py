"""Request-local Entra auth-code recovery using existing callback URI and state/cookies."""

import asyncio
import os
import secrets
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
import requests
from chainlit.auth import create_jwt
from chainlit.auth.cookie import (
    clear_oauth_state_cookie, get_token_from_cookies, set_auth_cookie, set_oauth_state_cookie,
)
from chainlit.auth import decode_jwt
from jwt import InvalidTokenError
from chainlit.user import User
from fastapi import FastAPI, Request, HTTPException
from starlette.responses import Response, RedirectResponse, HTMLResponse

from gpt_rag_ui.auth.auth_common import DelegationError, normalize_guid, canonical_principal_id
from gpt_rag_ui.auth.oauth import (
    resolve_oauth_runtime_config, resolve_msal_scopes_for_refresh, oauth_callback, msal,
)
from gpt_rag_ui.auth.recovery import (
    CALLBACK_PATH, RECOVERY_TTL, RecoveryError, configured_public_url, open_context,
    recovery_context, seal_context, verify_recovery,
    install_recovery_log_protection, validate_recovery_binding,
)

_COOKIE = "alz_auth_flow"
_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"}


def _cookie(request: Request, name: str) -> str:
    values = [
        item.partition("=")[2].strip()
        for header in request.headers.getlist("cookie") for item in header.split(";")
        if item.partition("=")[0].strip() == name
    ]
    if len(values) != 1 or name not in request.cookies:
        raise RecoveryError()
    # Keep raw-header duplicate detection, but honor standard quoted-cookie
    # decoding (including padded encrypted flow values).
    return request.cookies[name]


def _clear_flow(response: Response) -> Response:
    clear_oauth_state_cookie(response)
    for i in range(16):
        response.delete_cookie(f"{_COOKIE}_{i}", secure=True, httponly=True, samesite="lax")
    return response


def _write_flow(response: Response, encrypted: str) -> None:
    chunks = [encrypted[i:i + 3000] for i in range(0, len(encrypted), 3000)]
    if len(chunks) > 16:
        raise RecoveryError()
    for i, chunk in enumerate(chunks):
        response.set_cookie(f"{_COOKIE}_{i}", f"{len(chunks)}.{chunk}" if i == 0 else chunk,
                            max_age=RECOVERY_TTL, secure=True, httponly=True, samesite="lax")


def _read_flow(request: Request) -> dict[str, Any]:
    count_text, _, first = _cookie(request, f"{_COOKIE}_0").partition(".")
    count = int(count_text)
    if not 1 <= count <= 16:
        raise RecoveryError()
    ticket = first + "".join(_cookie(request, f"{_COOKIE}_{i}") for i in range(1, count))
    return open_context(ticket, purpose="oauth-flow")


async def _current_user(request: Request) -> User:
    cookie_name = os.environ.get("CHAINLIT_AUTH_COOKIE_NAME", "access_token")
    for name in request.cookies:
        if name == cookie_name or name.startswith(cookie_name + "_"):
            _cookie(request, name)
    token = get_token_from_cookies(request.cookies)
    if not token:
        raise RecoveryError()
    user = decode_jwt(token)
    if (not user or (user.metadata or {}).get("auth_source") != "oauth"
            or user.identifier != (user.metadata or {}).get("principal_id")):
        raise RecoveryError()
    return user


def _application() -> msal.ConfidentialClientApplication:
    client, secret, tenant = resolve_oauth_runtime_config()
    tenant = normalize_guid(tenant, claim_name="configured tenant")
    return msal.ConfidentialClientApplication(client, client_credential=secret,
                                              authority=f"https://login.microsoftonline.com/{tenant}", timeout=10)


def _failure() -> Response:
    return _clear_flow(HTMLResponse(
        "<h1>Sign-in was not completed</h1><p>Recovery expired, was cancelled, or did not match your session. "
        "Return to the assistant and start a fresh sign-in. No request was replayed.</p>",
        status_code=401, headers=_HEADERS,
    ))


def register_auth_recovery_routes(app: FastAPI) -> None:
    install_recovery_log_protection()
    async def begin(request: Request, ticket: str | None) -> Response:
        try:
            if request.headers.getlist("origin") not in ([], [configured_public_url()]):
                raise RecoveryError()
            context = None
            if ticket is not None:
                user = await _current_user(request)
                context = verify_recovery(ticket, user.metadata or {})
                if context["mode"] != "oauth":
                    raise RecoveryError()
            client_id, _, tenant = resolve_oauth_runtime_config()
            tenant = normalize_guid(tenant, claim_name="configured tenant")
            scopes = resolve_msal_scopes_for_refresh(client_id=client_id)
            state = secrets.token_urlsafe(32)
            msal_app = await asyncio.to_thread(_application)
            flow = await asyncio.to_thread(
                msal_app.initiate_auth_code_flow, scopes=scopes, state=state,
                redirect_uri=configured_public_url() + CALLBACK_PATH,
                claims_challenge=context["claims"] if context else None,
                response_mode="query", prompt="login" if context else None,
            )
            uri = flow.pop("auth_uri")
            parsed = urlsplit(uri)
            if (parsed.scheme != "https" or parsed.netloc != "login.microsoftonline.com"
                    or parsed.path != f"/{tenant}/oauth2/v2.0/authorize"):
                raise RecoveryError()
            # Keep all MSAL state/nonce/PKCE data, but not a second copy of the authorize URL.
            response = _clear_flow(RedirectResponse(uri, headers=_HEADERS))
            _write_flow(response, seal_context({"flow": flow, "recovery": context}, purpose="oauth-flow"))
            set_oauth_state_cookie(response, state)
            response.raw_headers = [
                (key, value + b"; Secure" if key == b"set-cookie" and value.startswith(b"oauth_state=")
                 and b"; Secure" not in value else value)
                for key, value in response.raw_headers
            ]
            return response
        except (ValueError, KeyError, TypeError, RuntimeError, HTTPException, InvalidTokenError,
                requests.RequestException, httpx.HTTPError):
            return _failure()

    @app.get("/api/auth/recovery")
    async def recover(request: Request) -> Response:
        if set(request.query_params) != {"ticket"} or len(request.query_params.getlist("ticket")) != 1:
            return _failure()
        return await begin(request, request.query_params["ticket"])

    @app.get("/auth/oauth/azure-ad")
    async def login(request: Request) -> Response:
        return await begin(request, None)

    @app.get(CALLBACK_PATH)
    async def callback(request: Request) -> Response:
        context_token = None
        try:
            flow_context = _read_flow(request)
            flow = flow_context["flow"]
            state = request.query_params.getlist("state")
            code = request.query_params.getlist("code")
            if (len(state) != 1 or len(code) != 1 or not code[0] or len(code[0]) > 16384
                    or request.query_params.get("error")
                    or state[0] != _cookie(request, "oauth_state") or state[0] != flow.get("state")):
                raise RecoveryError()
            context = flow_context.get("recovery")
            if context is not None:
                current = await _current_user(request)
                # Recheck binding on callback, including logout/account switch while at Entra.
                validate_recovery_binding(context, current.metadata or {})
            msal_app = await asyncio.to_thread(_application)
            result = await asyncio.to_thread(
                msal_app.acquire_token_by_auth_code_flow, flow,
                {"state": state[0], "code": code[0]},
            )
            if "error" in result or not result.get("refresh_token"):
                raise RecoveryError()
            signed_identity = result.get("id_token_claims") or {}
            if not isinstance(signed_identity.get("exp"), int) or signed_identity["exp"] <= time.time():
                raise RecoveryError()
            signed_principal = canonical_principal_id(signed_identity.get("tid", ""), signed_identity.get("oid", ""))
            configured_tenant = normalize_guid(resolve_oauth_runtime_config()[2], claim_name="configured tenant")
            if signed_principal.split(":")[0] != configured_tenant or (context and signed_principal != context["p"]):
                raise RecoveryError()
            context = context or {"p": signed_principal, "claims": None}
            context_token = recovery_context.set(context)
            user = await oauth_callback(
                "azure-ad", "", {}, User(identifier="", metadata={"refresh_token": result["refresh_token"]}),
            )
            if not user:
                raise RecoveryError()
            # oauth_callback checks MSAL-verified tid/oid against this context and configured tenant.
            response = RedirectResponse(configured_public_url() + "/login/callback", status_code=303, headers=_HEADERS)
            set_auth_cookie(request, response, create_jwt(user))
            return _clear_flow(response)
        except (ValueError, KeyError, TypeError, RuntimeError, HTTPException, DelegationError, InvalidTokenError,
                requests.RequestException, httpx.HTTPError):
            return _failure()
        finally:
            if context_token is not None:
                recovery_context.reset(context_token)
