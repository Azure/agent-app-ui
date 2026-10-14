"""HTTP response adaptation for authorized citation downloads."""

import asyncio
import logging
import mimetypes
import os
from urllib.parse import quote

from azure.core.exceptions import ResourceNotFoundError
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from gpt_rag_ui.auth.embed_auth import CopilotSessionStore
from gpt_rag_ui.telemetry.boundary_diagnostics import log_boundary_failure
from gpt_rag_ui.services.conversation_security import get_owned_conversation
from gpt_rag_ui.services.download_security import (
    BlobDownloader, ConversationResolver, DownloadStream, DownloadTokenManager,
    is_download_target_allowed, resolve_download_principal,
)

logger = logging.getLogger("gpt_rag_ui.download_security")


def register_secure_download_route(
    app: FastAPI,
    *,
    manager: DownloadTokenManager,
    download_blob: BlobDownloader,
    allowed_containers: set[str],
    conversation_container: str,
    shared_containers: set[str],
    sessions: CopilotSessionStore,
    conversation_resolver: ConversationResolver = get_owned_conversation,
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

        principal = await resolve_download_principal(
            request,
            sessions,
            expected_principal_id=grant.principal_id,
        )
        if not principal:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"Cache-Control": "no-store"},
            )

        if (
            grant.principal_id != principal.principal_id
            or grant.session_id != principal.session_id
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Not found",
                headers={"Cache-Control": "no-store"},
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
