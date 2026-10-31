import asyncio
import hashlib
import logging
import mimetypes
import os
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from urllib.parse import quote, urlsplit
from typing import Any

from azure.core.exceptions import ResourceNotFoundError
from chainlit.auth import authenticate_user, get_token_from_cookies
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from itsdangerous import BadData, URLSafeTimedSerializer

from gpt_rag_ui.services.conversation_security import (
    canonical_conversation_id,
    get_owned_conversation,
    principal_id_from_metadata,
)
from gpt_rag_ui.auth.embed_auth import (
    CopilotSessionStore,
    get_request_copilot_session,
    session_id_from_request,
)
from gpt_rag_ui.config.chat_backend import ChatBackendConfig, load_chat_backend
from gpt_rag_ui.config.dependencies import get_config


_CONTAINER_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])?$")
logger = logging.getLogger("gpt_rag_ui.download_security")


@dataclass(frozen=True)
class DownloadGrant:
    principal_id: str
    session_id: str
    conversation_id: str
    container: str
    blob_name: str


@dataclass(frozen=True)
class DownloadPrincipal:
    principal_id: str
    session_id: str | None
    metadata: dict


@dataclass(frozen=True)
class DownloadStream:
    chunks: Iterable[bytes]
    size: int


class DownloadTokenManager:
    def __init__(
        self,
        *,
        secret: str,
        public_url: str,
        max_age_seconds: int = 3600,
        native: bool = False,
    ):
        if not secret:
            raise ValueError("A signing secret is required for download grants.")
        parsed_url = urlsplit(public_url)
        try:
            port = parsed_url.port
        except ValueError as exc:
            raise ValueError(
                "The download public URL must be an absolute URL with a "
                "canonical root path."
            ) from exc
        path = parsed_url.path
        invalid_path = (
            path not in {"", "/"}
            and (
                not path.startswith("/")
                or path.endswith("/")
                or "//" in path
                or "\\" in path
                or "%" in path
                or any(
                    segment in {"", ".", ".."}
                    for segment in path.split("/")[1:]
                )
                or any(ord(char) < 32 for char in path)
            )
        )
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.hostname
            or parsed_url.username
            or parsed_url.password
            or invalid_path
            or parsed_url.query
            or parsed_url.fragment
            or (port is not None and port <= 0)
        ):
            raise ValueError(
                "The download public URL must be an absolute URL with a "
                "canonical root path."
            )
        if max_age_seconds < 1:
            raise ValueError("Download grants require a positive lifetime.")

        self.serializer = URLSafeTimedSerializer(
            secret,
            salt="gpt-rag-download-v1",
            signer_kwargs={"digest_method": hashlib.sha256},
        )
        self.public_url = public_url.rstrip("/")
        self.max_age_seconds = max_age_seconds
        self.native = native

    def identity_binding(self, value: str) -> str:
        # Native URL grants must not expose the opaque embedded session cookie.
        return hashlib.sha256(value.encode("utf-8")).hexdigest() if self.native else value

    def _valid_grant_target(self, **target: Any) -> bool:
        if not self.native:
            return self._valid_target(**target)
        # A hosted label is only a scope selector, never proof of ownership.
        cid = target["conversation_id"]
        return (
            isinstance(cid, str) and len(cid) <= 256
            and not any(ord(c) < 33 or c in "/\\%'\"" for c in cid)
            and cid != "NaN"
            and self._valid_target(**{**target, "conversation_id": "11111111-1111-4111-8111-111111111111"})
        )

    @staticmethod
    def _valid_target(
        *,
        principal_id: str,
        session_id: str,
        conversation_id: str,
        container: str,
        blob_name: str,
    ) -> bool:
        normalized_blob = blob_name.replace("\\", "/")
        return bool(
            principal_id
            and session_id
            and len(session_id) <= 256
            and session_id.strip() == session_id
            and not any(ord(char) < 32 for char in session_id)
            and canonical_conversation_id(conversation_id) == conversation_id
            and _CONTAINER_PATTERN.fullmatch(container)
            and normalized_blob
            and len(normalized_blob) <= 1024
            and not normalized_blob.startswith("/")
            and "%" not in normalized_blob
            and not any(ord(char) < 32 for char in normalized_blob)
            and not any(
                part in {"", ".", ".."} for part in normalized_blob.split("/")
            )
        )

    def issue(
        self,
        *,
        principal_id: str,
        session_id: str,
        conversation_id: str,
        container: str,
        blob_name: str,
    ) -> str:
        if not self._valid_grant_target(
            principal_id=principal_id,
            session_id=session_id,
            conversation_id=conversation_id,
            container=container,
            blob_name=blob_name,
        ):
            raise ValueError("Invalid download grant.")
        token = self.serializer.dumps(
            {
                "v": 3 if self.native else 2,
                "p": self.identity_binding(principal_id),
                "s": self.identity_binding(session_id),
                "c": conversation_id,
                "n": container,
                "b": blob_name.replace("\\", "/"),
            }
        )
        return f"{self.public_url}/api/download/{quote(token, safe='')}"

    def verify(self, token: str) -> DownloadGrant | None:
        if not token or len(token) > 8192:
            return None
        try:
            payload = self.serializer.loads(
                token,
                max_age=self.max_age_seconds,
            )
        except BadData:
            return None
        if not isinstance(payload, dict) or payload.get("v") != (3 if self.native else 2):
            return None

        required = ("p", "s", "n", "b") if self.native else ("p", "s", "c", "n", "b")
        if any(
            not isinstance(payload.get(key), str) or not payload[key]
            for key in required
        ):
            return None
        if not isinstance(payload.get("c"), str):
            return None
        if not self._valid_grant_target(
            principal_id=payload["p"],
            session_id=payload["s"],
            conversation_id=payload["c"],
            container=payload["n"],
            blob_name=payload["b"],
        ):
            return None
        return DownloadGrant(
            principal_id=payload["p"],
            session_id=payload["s"],
            conversation_id=payload["c"],
            container=payload["n"],
            blob_name=payload["b"],
        )


def is_download_target_allowed(
    *,
    conversation_id: str,
    container: str,
    blob_name: str,
    conversation_container: str,
    shared_containers: set[str],
) -> bool:
    if container == conversation_container:
        normalized = blob_name.replace("\\", "/").lstrip("/")
        return normalized.startswith(f"conversations/{conversation_id}/")
    return container in shared_containers


async def resolve_download_principal(
    request: Request,
    sessions: CopilotSessionStore | None,
    *,
    expected_principal_id: str | None = None,
    refresh_oauth: bool = False,
) -> DownloadPrincipal | None:
    request_session = get_request_copilot_session()
    mismatched_principal = None
    if request_session and sessions is not None:
        opaque_session = await sessions.get(
            getattr(request_session, "session_id", None)
            or session_id_from_request(request)
        )
        if (
            not opaque_session
            or opaque_session.principal_id != request_session.principal_id
        ):
            return None
        opaque_principal = DownloadPrincipal(
            principal_id=opaque_session.principal_id,
            session_id=opaque_session.session_id,
            metadata=opaque_session.user_metadata(),
        )
        if (
            not expected_principal_id
            or opaque_principal.principal_id == expected_principal_id
        ):
            return opaque_principal
        mismatched_principal = opaque_principal

    if sessions is not None and not request_session and not request.headers.getlist("Origin"):
        opaque_session = await sessions.get(session_id_from_request(request))
        if opaque_session:
            mismatched_principal = DownloadPrincipal(
                principal_id=opaque_session.principal_id,
                session_id=opaque_session.session_id,
                metadata=opaque_session.user_metadata(),
            )
            if (
                not expected_principal_id
                or opaque_session.principal_id == expected_principal_id
            ):
                return mismatched_principal

    chainlit_token = get_token_from_cookies(request.cookies)
    if not chainlit_token:
        return mismatched_principal
    try:
        user = await authenticate_user(chainlit_token)
    except HTTPException:
        return mismatched_principal
    if not user or not (user.metadata or {}).get("authorized", True):
        return mismatched_principal

    if refresh_oauth and (user.metadata or {}).get("auth_source") == "oauth":
        from gpt_rag_ui.auth.oauth import ensure_fresh_user_access_token
        await ensure_fresh_user_access_token(user, min_ttl_seconds=120)
    metadata = dict(user.metadata or {})
    principal_id = principal_id_from_metadata(metadata)
    if not principal_id or principal_id != user.identifier:
        return mismatched_principal
    standalone_principal = DownloadPrincipal(
        principal_id=principal_id,
        session_id=None,
        metadata=metadata,
    )
    if expected_principal_id and principal_id != expected_principal_id:
        return mismatched_principal or standalone_principal
    return standalone_principal


ConversationResolver = Callable[[str, dict | None], Awaitable[dict | None]]
BlobDownloader = Callable[[str], bytes | DownloadStream]




_manager: DownloadTokenManager | None = None


def configure_download_tokens(
    *,
    secret: str,
    public_url: str,
    native: bool = False,
) -> DownloadTokenManager:
    global _manager
    _manager = DownloadTokenManager(secret=secret, public_url=public_url, native=native)
    return _manager


def get_download_tokens() -> DownloadTokenManager:
    if _manager is None:
        raise RuntimeError("Download token manager has not been configured.")
    return _manager


def native_downloads_enabled(config: ChatBackendConfig | None = None) -> bool:
    cfg = config if config is not None else get_config()
    host = os.environ.get("ORCHESTRATION_HOST")
    if host is None:
        host = cfg.get("ORCHESTRATION_HOST", "", str)
    return host.strip() in {"hosted", "container_apps"} or load_chat_backend(cfg) == "hosted_agent"
