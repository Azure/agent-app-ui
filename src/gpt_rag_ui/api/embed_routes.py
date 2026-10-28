"""Copilot authentication HTTP routes over the canonical session store."""

import logging
import asyncio

import httpx
from fastapi import FastAPI, status
from starlette.requests import Request
from starlette.responses import JSONResponse

from gpt_rag_ui.auth.auth_common import canonical_principal_id, is_user_authorized
from gpt_rag_ui.auth.embed_auth import (
    BootstrapRateLimiter, CopilotSession, CopilotSessionStore, TokenValidator,
    bootstrap_rate_limit_key, clear_copilot_session_cookie, session_id_from_request,
    set_copilot_session_cookie,
)
from gpt_rag_ui.auth.entra_token import EntraTokenError
from gpt_rag_ui.auth.recovery import RecoveryError, verify_recovery
from gpt_rag_ui.config.appconfig import AppConfigClient
from gpt_rag_ui.config.embed_config import EmbedSettings

logger = logging.getLogger("gpt_rag_ui.embed_auth")


def _auth_error_response(
    *,
    status_code: int,
    detail: str,
    settings: EmbedSettings,
    clear_cookie: bool = True,
    extra_headers: dict[str, str] | None = None,
) -> JSONResponse:
    headers = {
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
    }
    headers.update(extra_headers or {})
    response = JSONResponse(
        {"detail": detail},
        status_code=status_code,
        headers=headers,
    )
    if clear_cookie:
        clear_copilot_session_cookie(
            response,
            same_site=settings.cookie_samesite,
        )
    return response

def _session_response(
    session: CopilotSession,
    settings: EmbedSettings,
) -> JSONResponse:
    response = JSONResponse(
        {
            "success": True,
            "authMode": session.auth_mode,
            "expiresAt": session.expires_at,
        },
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
        },
    )
    set_copilot_session_cookie(
        response,
        session,
        same_site=settings.cookie_samesite,
    )
    return response

def register_copilot_auth_routes(
    app: FastAPI,
    *,
    settings: EmbedSettings,
    sessions: CopilotSessionStore,
    validator: TokenValidator | None,
    config: AppConfigClient,
    rate_limiter: BootstrapRateLimiter | None = None,
) -> None:
    if settings.uses_entra and validator is None:
        raise ValueError("Entra Copilot mode requires a token validator.")
    if settings.auth_mode not in {"anonymous", "entra"}:
        raise ValueError("Copilot auth mode is not configured.")

    limiter = rate_limiter or BootstrapRateLimiter(
        max_attempts=settings.bootstrap_rate_limit_per_minute,
        max_keys=max(256, min(settings.max_sessions * 2, 20000)),
    )

    @app.post("/copilot/auth/recovery")
    async def recover_copilot(request: Request) -> JSONResponse:
        # The existing middleware also protects this path; keep exact Origin at
        # this credential-release boundary even when the router is used alone.
        if request.headers.getlist("Origin") not in [[origin] for origin in settings.allowed_origins]:
            return auth_recovery_error(403)
        previous = await sessions.get(session_id_from_request(request))
        if not settings.uses_entra or not previous or previous.auth_mode != "entra":
            return auth_recovery_error(401)
        if request.headers.get("content-length", "").isdigit() and int(request.headers["content-length"]) > 65536:
            return auth_recovery_error(400)
        body = bytearray()
        try:
            async with asyncio.timeout(10):
                async for part in request.stream():
                    body.extend(part)
                    if len(body) > 65536:
                        return auth_recovery_error(400)
        except TimeoutError:
            return auth_recovery_error(400)
        try:
            import json
            payload = json.loads(body)
            if not isinstance(payload, dict) or set(payload) != {"ticket", "operation"}:
                raise RecoveryError()
            context = verify_recovery(payload["ticket"], previous.user_metadata())
            if payload["operation"] == "challenge":
                return JSONResponse({"claims": context["claims"]}, headers={"Cache-Control": "no-store"})
            if payload["operation"] != "complete" or validator is None:
                raise RecoveryError()
            authorization = request.headers.getlist("Authorization")
            if len(authorization) != 1 or not authorization[0].startswith("Bearer "):
                raise RecoveryError()
            access_token = authorization[0][7:]
            claims = await validator.validate(access_token)
            principal = canonical_principal_id(str(claims["tid"]), str(claims["oid"]))
            principal_name = str(claims.get("preferred_username") or claims.get("email") or claims.get("upn") or "")
            if principal != previous.principal_id or not is_user_authorized(config, principal_name, principal):
                raise RecoveryError()
            session = await sessions.replace(
                previous_session_id=previous.session_id, access_token=access_token,
                claims=claims, display_name=previous.display_name,
                principal_name=principal_name, auth_mode="entra", require_current_session=True,
            )
            return _session_response(session, settings)
        except (ValueError, KeyError, TypeError, EntraTokenError):
            return auth_recovery_error(401)
        except httpx.HTTPError:
            return auth_recovery_error(503)

    def auth_recovery_error(code: int) -> JSONResponse:
        return JSONResponse(
            {"detail": "Protected sign-in was not completed. Sign in through your portal and reopen the assistant."},
            status_code=code, headers={"Cache-Control": "no-store"},
        )

    @app.post("/copilot/auth/bootstrap")
    async def bootstrap_copilot(request: Request) -> JSONResponse:
        retry_after = await limiter.retry_after(
            bootstrap_rate_limit_key(request)
        )
        if retry_after is not None:
            logger.warning("Copilot bootstrap rate limit exceeded")
            return _auth_error_response(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many authentication attempts",
                settings=settings,
                clear_cookie=False,
                extra_headers={"Retry-After": str(retry_after)},
            )

        previous_session_id = session_id_from_request(request)
        previous_session = await sessions.get(previous_session_id)

        def auth_error(
            *,
            status_code: int,
            detail: str,
            clear_cookie: bool | None = None,
        ) -> JSONResponse:
            return _auth_error_response(
                status_code=status_code,
                detail=detail,
                settings=settings,
                clear_cookie=(
                    previous_session is None
                    if clear_cookie is None
                    else clear_cookie
                ),
            )

        authorization_values = request.headers.getlist("Authorization")
        if settings.auth_mode == "anonymous":
            if authorization_values:
                logger.warning(
                    "Anonymous Copilot bootstrap rejected an Authorization header"
                )
                return auth_error(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Authorization is not accepted in anonymous mode",
                    clear_cookie=False,
                )
            session = await sessions.replace_anonymous(
                previous_session_id=previous_session_id,
            )
            return _session_response(session, settings)

        authorization = (
            authorization_values[0] if len(authorization_values) == 1 else ""
        )
        scheme, separator, access_token = authorization.partition(" ")
        if (
            not separator
            or scheme.lower() != "bearer"
            or not access_token.strip()
        ):
            return auth_error(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
            )

        access_token = access_token.strip()
        try:
            assert validator is not None
            claims = await validator.validate(access_token)
        except EntraTokenError:
            logger.warning("Copilot bootstrap rejected an invalid Entra token")
            return auth_error(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication failed",
            )
        except httpx.HTTPError:
            logger.exception("Copilot bootstrap could not reach Entra JWKS")
            return auth_error(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication service unavailable",
            )

        try:
            tenant_id = str(claims["tid"])
            object_id = str(claims["oid"])
            principal_id = canonical_principal_id(tenant_id, object_id)
        except (KeyError, TypeError, ValueError):
            logger.warning("Copilot bootstrap rejected invalid identity claims")
            return auth_error(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication failed",
            )

        principal_name = str(
            claims.get("preferred_username")
            or claims.get("email")
            or claims.get("upn")
            or ""
        )
        if not is_user_authorized(
            config,
            principal_name,
            principal_id,
        ):
            logger.warning("Copilot bootstrap denied principal=%s", principal_id)
            if previous_session_id:
                await sessions.delete(previous_session_id)
            return auth_error(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied",
                clear_cookie=True,
            )

        try:
            session = await sessions.replace(
                previous_session_id=previous_session_id,
                access_token=access_token,
                claims=claims,
                display_name=str(
                    claims.get("name") or principal_name or principal_id
                ),
                principal_name=principal_name,
                auth_mode="entra",
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("Copilot bootstrap rejected invalid token claims")
            return auth_error(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication failed",
            )

        return _session_response(session, settings)

    @app.post("/copilot/auth/logout")
    async def logout_copilot(request: Request) -> JSONResponse:
        await sessions.delete(session_id_from_request(request))
        response = JSONResponse(
            {"success": True},
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
            },
        )
        clear_copilot_session_cookie(
            response,
            same_site=settings.cookie_samesite,
        )
        return response
