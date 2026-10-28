"""HTTP response adaptation for authorized citation downloads."""

import asyncio
import logging
import mimetypes
import os
import html
import json
from typing import Any
from urllib.parse import quote

from azure.core.exceptions import ResourceNotFoundError
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from starlette.types import Scope, Receive, Send
from gpt_rag_ui.auth.auth_common import DelegationError
from gpt_rag_ui.auth.recovery import issue_recovery, configured_public_url, RecoveryError
from gpt_rag_ui.auth.embed_auth import resolve_access_token
from gpt_rag_ui.clients.blob import NativeDownloadClient, NativeBlobDownload, NativeDownloadError
from gpt_rag_ui.clients.hosted_agent_client import HostedAgentAuthenticationError

from gpt_rag_ui.auth.embed_auth import CopilotSessionStore
from gpt_rag_ui.telemetry.boundary_diagnostics import log_boundary_failure
from gpt_rag_ui.services.conversation_security import get_owned_conversation
from gpt_rag_ui.services.download_security import (
    BlobDownloader, ConversationResolver, DownloadStream, DownloadTokenManager,
    is_download_target_allowed, resolve_download_principal,
)

logger = logging.getLogger("gpt_rag_ui.download_security")


def _download_recovery(metadata: dict, code: str, claims: object) -> StreamingResponse:
    """Only offer sign-in for the authenticated download principal; never replay the GET."""
    def page(content: str, *, status_code: int, headers: dict[str, str]) -> StreamingResponse:
        return StreamingResponse(iter((content.encode("utf-8"),)), media_type="text/html",
                                 status_code=status_code, headers=headers)

    try:
        ticket = issue_recovery(metadata, code, claims)
        if metadata.get("auth_source") == "oauth":
            href = configured_public_url() + "/api/auth/recovery?ticket=" + ticket
            action = f'<a rel="noreferrer" href="{html.escape(href, quote=True)}">Continue sign-in</a>'
        else:
            from gpt_rag_ui.config.dependencies import get_config
            from gpt_rag_ui.config.embed_config import load_embed_settings
            origins = html.escape(json.dumps(list(load_embed_settings(get_config()).allowed_origins)), quote=True)
            action = (
                '<p>Return to your portal and use the assistant’s Continue sign-in control. '
                'If this download opened from your portal in a new window, use the button below.</p>'
                f'<button type="button" id="recover" data-origins="{origins}" data-ticket="{html.escape(ticket, quote=True)}">'
                'Continue sign-in in portal</button>'
                '<p id="recovery-status" role="status"></p><script src="/public/download-recovery.js"></script>'
            )
        return page(
            "<h1>Protected download needs sign-in</h1>" + action
            + "<p>After sign-in, request the download again. Nothing will be replayed.</p>",
            status_code=401,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                     "Content-Security-Policy": "default-src 'none'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'"},
        )
    except (ValueError, RecoveryError):
        return page(
            "<h1>Protected document access was not completed</h1>"
            "<p>Access was denied or sign-in recovery is unavailable. Check your permissions, "
            "or sign in through your portal or assistant again. Nothing was replayed.</p>",
            status_code=403 if code == "source_denied" else 401,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )


class _NativeStreamingResponse(StreamingResponse):
    def __init__(self, download: NativeBlobDownload, **kwargs: Any) -> None:
        self.download = download
        super().__init__(download.chunks, **kwargs)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.download.close()


def register_secure_download_route(
    app: FastAPI,
    *,
    manager: DownloadTokenManager,
    download_blob: BlobDownloader,
    allowed_containers: set[str],
    conversation_container: str,
    shared_containers: set[str],
    sessions: CopilotSessionStore | None,
    conversation_resolver: ConversationResolver = get_owned_conversation,
    native_client: NativeDownloadClient | None = None,
) -> None:
    @app.get("/api/download/{grant_token}")
    async def download_blob_file(grant_token: str, request: Request) -> StreamingResponse:
        grant = manager.verify(grant_token)
        if not grant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Not found",
                headers={"Cache-Control": "no-store"},
            )

        pending_auth_error = None
        try:
            principal = await resolve_download_principal(
                request, sessions, expected_principal_id=None if manager.native else grant.principal_id,
                refresh_oauth=manager.native,
            )
        except DelegationError as exc:
            if not manager.native:
                raise HTTPException(401, "Sign in again to download this document.") from None
            # Recover identity without retrying the failed token exchange. The
            # grant must still match before any recovery context can be issued.
            principal = await resolve_download_principal(request, sessions, refresh_oauth=False)
            pending_auth_error = exc
        if not principal:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"Cache-Control": "no-store"},
            )

        if (
            grant.principal_id != manager.identity_binding(principal.principal_id)
            or grant.session_id != (
                manager.identity_binding(principal.session_id or "oauth")
                if manager.native else principal.session_id
            )
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Not found",
                headers={"Cache-Control": "no-store"},
            )
        if manager.native:
            if pending_auth_error is not None:
                return _download_recovery(principal.metadata, pending_auth_error.code, pending_auth_error.claims_challenge)
            if native_client is None or grant.container not in allowed_containers:
                raise HTTPException(404, "Not found")
            try:
                assertion = await resolve_access_token(principal.metadata)
                if not assertion:
                    raise HTTPException(401, "Authentication required")
                native_download = await native_client.open(
                    container=grant.container, blob_name=grant.blob_name,
                    conversation_id=grant.conversation_id, assertion=assertion,
                    object_id=str(principal.metadata.get("object_id") or ""),
                )
            except (DelegationError, HostedAgentAuthenticationError) as exc:
                return _download_recovery(principal.metadata, exc.code, exc.claims_challenge)
            except NativeDownloadError as exc:
                if exc.status_code in {401, 403}:
                    return _download_recovery(principal.metadata, exc.code, exc.claims_challenge)
                raise HTTPException(exc.status_code, "Protected document access denied or unavailable.") from None
            return _NativeStreamingResponse(
                native_download, media_type="application/octet-stream",
                headers={
                    "Content-Disposition": "attachment; filename*=UTF-8''" + quote(os.path.basename(grant.blob_name), safe=""),
                    "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer",
                    "Content-Length": str(native_download.size),
                },
            )
        if not await conversation_resolver(
            grant.conversation_id,
            principal.metadata,
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Not found",
                headers={"Cache-Control": "no-store"},
            )
        if grant.container not in allowed_containers:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Not found",
                headers={"Cache-Control": "no-store"},
            )
        if not is_download_target_allowed(
            conversation_id=grant.conversation_id,
            container=grant.container,
            blob_name=grant.blob_name,
            conversation_container=conversation_container,
            shared_containers=shared_containers,
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Not found",
                headers={"Cache-Control": "no-store"},
            )

        try:
            download = await asyncio.to_thread(
                download_blob,
                f"{grant.container}/{grant.blob_name}",
            )
        except ResourceNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Not found",
                headers={"Cache-Control": "no-store"},
            ) from exc
        except Exception as exc:
            log_boundary_failure(logger,
                "Authorized download failed: conversation=%s container=%s",
                grant.conversation_id,
                grant.container,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Download failed",
                headers={"Cache-Control": "no-store"},
            ) from exc

        if isinstance(download, DownloadStream):
            if download.size <= 0:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Not found",
                    headers={"Cache-Control": "no-store"},
                )
            response_body = download.chunks
            content_length = download.size
        elif download:
            response_body = iter((download,))
            content_length = len(download)
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Not found",
                headers={"Cache-Control": "no-store"},
            )

        file_name = os.path.basename(grant.blob_name)
        content_type = (
            mimetypes.guess_type(file_name)[0]
            or "application/octet-stream"
        )
        return StreamingResponse(
            response_body,
            media_type=content_type,
            headers={
                "Content-Disposition": (
                    "attachment; filename*=UTF-8''"
                    f"{quote(file_name, safe='')}"
                ),
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Length": str(content_length),
            },
        )
