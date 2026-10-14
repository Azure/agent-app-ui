import asyncio
import base64
import hashlib
import logging
import math
import os
import re
import secrets
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from typing import Literal, Protocol

import httpx
import jwt
from chainlit.auth import create_jwt
from chainlit.user import User
from fastapi import FastAPI, status
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from gpt_rag_ui.auth.auth_common import canonical_principal_id, is_user_authorized
from gpt_rag_ui.config.appconfig import AppConfigClient
from gpt_rag_ui.config.embed_config import EmbedSettings
from gpt_rag_ui.auth.entra_token import EntraTokenError
from gpt_rag_ui.telemetry.boundary_diagnostics import log_boundary_failure


COPILOT_SESSION_COOKIE = "gpt_rag_copilot_session"
ANONYMOUS_TENANT_ID = "00000000-0000-0000-0000-000000000000"
logger = logging.getLogger("gpt_rag_ui.embed_auth")
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_SESSION_FAMILY_BYTES = 16

SessionInvalidationCallback = Callable[[str], Awaitable[None]]


def _session_family_id(session_id: str | None) -> bytes | None:
    if not session_id or not _SESSION_ID_PATTERN.fullmatch(session_id):
        return None
    try:
        decoded = base64.urlsafe_b64decode(session_id + "=")
    except ValueError:
        return None
    if len(decoded) != 32:
        return None
    return decoded[:_SESSION_FAMILY_BYTES]


def _new_session_id(family_id: bytes | None = None) -> str:
    family = family_id or secrets.token_bytes(_SESSION_FAMILY_BYTES)
    session_bytes = family + secrets.token_bytes(_SESSION_FAMILY_BYTES)
    return base64.urlsafe_b64encode(session_bytes).rstrip(b"=").decode()


@dataclass(frozen=True)
class CopilotSession:
    session_id: str
    principal_id: str
    tenant_id: str
    object_id: str
    auth_mode: Literal["anonymous", "entra"]
    access_token: str | None = field(repr=False)
    chainlit_token: str = field(repr=False)
    expires_at: int
    display_name: str
    principal_name: str
    group_ids: tuple[str, ...] = ()

    def user_metadata(self) -> dict:
        is_entra = self.auth_mode == "entra"
        return {
            "authorized": True,
            "auth_source": "copilot_session",
            "copilot_auth_mode": self.auth_mode,
            "copilot_session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "object_id": self.object_id,
            "principal_id": self.principal_id,
            # Downstream ACL and orchestrator contracts use a bare Entra oid.
            "client_principal_id": self.object_id if is_entra else "no-auth",
            "client_principal_name": (
                self.principal_name if is_entra else "anonymous"
            ),
            "user_name": self.principal_id,
            "client_group_names": list(self.group_ids) if is_entra else [],
        }


class CopilotSessionStore:
    def __init__(
        self,
        *,
        max_sessions: int,
        ttl_seconds: int,
        on_invalidate: SessionInvalidationCallback | None = None,
    ):
        if max_sessions < 1 or ttl_seconds < 1:
            raise ValueError("Copilot session bounds must be positive.")
        self.max_sessions = max_sessions
        self.ttl_seconds = ttl_seconds
        self._sessions: OrderedDict[str, CopilotSession] = OrderedDict()
        self._principal_sessions: dict[str, str] = {}
        self._lineage_sessions: dict[bytes, str] = {}
        self._expiry_tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._on_invalidate = on_invalidate

    def _cancel_expiry_locked(self, session_id: str) -> None:
        task = self._expiry_tasks.pop(session_id, None)
        if task and task is not asyncio.current_task():
            task.cancel()

    def _remove_locked(self, session_id: str) -> CopilotSession | None:
        session = self._sessions.pop(session_id, None)
        self._cancel_expiry_locked(session_id)
        if (
            session
            and self._principal_sessions.get(session.principal_id) == session_id
        ):
            self._principal_sessions.pop(session.principal_id, None)
        family_id = _session_family_id(session_id)
        if (
            family_id
            and self._lineage_sessions.get(family_id) == session_id
        ):
            self._lineage_sessions.pop(family_id, None)
        return session

    def _current_lineage_session_locked(self, session_id: str) -> str:
        family_id = _session_family_id(session_id)
        if not family_id:
            return session_id
        return self._lineage_sessions.get(family_id, session_id)

    def _prune_locked(self, now: int) -> list[str]:
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if session.expires_at <= now
        ]
        for session_id in expired:
            self._remove_locked(session_id)
        return expired

    async def _expire_at(self, session_id: str, expires_at: int) -> None:
        try:
            while True:
                delay = expires_at - time.time()
                if delay > 0:
                    await asyncio.sleep(delay)

                async with self._lock:
                    session = self._sessions.get(session_id)
                    if not session or session.expires_at != expires_at:
                        return
                    if session.expires_at > time.time():
                        continue
                    self._remove_locked(session_id)
                await self._notify_invalidated([session_id])
                return
        except asyncio.CancelledError:
            return

    async def _notify_invalidated(self, session_ids: list[str]) -> None:
        if not self._on_invalidate:
            return
        for session_id in dict.fromkeys(session_ids):
            try:
                await self._on_invalidate(session_id)
            except Exception:
                log_boundary_failure(logger,
                    "Failed to disconnect invalidated Copilot session"
                )

    async def replace(
        self,
        *,
        previous_session_id: str | None,
        access_token: str | None,
        claims: dict,
        display_name: str,
        principal_name: str,
        auth_mode: Literal["anonymous", "entra"] = "entra",
    ) -> CopilotSession:
        if auth_mode == "entra" and not access_token:
            raise ValueError("An Entra Copilot session requires an access token.")
        if auth_mode == "anonymous" and access_token:
            raise ValueError("An anonymous Copilot session cannot retain an access token.")

        now = int(time.time())
        token_expires_at = int(claims["exp"])
        expires_at = min(token_expires_at, now + self.ttl_seconds)
        if expires_at <= now:
            raise ValueError("The Copilot session credential has expired.")

        tenant_id = str(claims["tid"])
        object_id = str(claims["oid"])
        principal_id = canonical_principal_id(tenant_id, object_id)
        tenant_id, object_id = principal_id.split(":", 1)
        group_ids = tuple(
            str(group)
            for group in claims.get("groups", [])
            if isinstance(group, str)
        )
        invalidated: list[str] = []
        async with self._lock:
            invalidated.extend(self._prune_locked(now))
            family_id = _session_family_id(previous_session_id)
            if previous_session_id:
                current_session_id = self._current_lineage_session_locked(
                    previous_session_id
                )
                self._remove_locked(current_session_id)
                invalidated.extend(
                    [previous_session_id, current_session_id]
                )
            principal_session_id = self._principal_sessions.get(principal_id)
            if principal_session_id:
                if family_id is None:
                    family_id = _session_family_id(principal_session_id)
                previous = self._remove_locked(principal_session_id)
                if previous:
                    invalidated.append(principal_session_id)

            session_id = _new_session_id(family_id)
            family_id = _session_family_id(session_id)
            if family_id is None:
                raise RuntimeError("Failed to create a Copilot session ID.")
            user = User(
                identifier=principal_id,
                display_name=display_name,
                metadata={
                    "authorized": True,
                    "auth_source": "copilot_session",
                    "copilot_auth_mode": auth_mode,
                    "copilot_session_id": session_id,
                    "tenant_id": tenant_id,
                    "object_id": object_id,
                    "principal_id": principal_id,
                    "client_principal_id": (
                        object_id if auth_mode == "entra" else "no-auth"
                    ),
                    "client_principal_name": (
                        principal_name if auth_mode == "entra" else "anonymous"
                    ),
                    "user_name": principal_id,
                    "client_group_names": (
                        list(group_ids) if auth_mode == "entra" else []
                    ),
                },
            )
            chainlit_token = create_embed_session_jwt(user, expires_at)
            session = CopilotSession(
                session_id=session_id,
                principal_id=principal_id,
                tenant_id=tenant_id,
                object_id=object_id,
                auth_mode=auth_mode,
                access_token=access_token,
                chainlit_token=chainlit_token,
                expires_at=expires_at,
                display_name=display_name,
                principal_name=principal_name,
                group_ids=group_ids,
            )
            self._sessions[session_id] = session
            self._principal_sessions[principal_id] = session_id
            self._lineage_sessions[family_id] = session_id
            self._expiry_tasks[session_id] = asyncio.create_task(
                self._expire_at(session_id, expires_at),
                name=f"copilot-session-expiry-{session_id[:8]}",
            )
            while len(self._sessions) > self.max_sessions:
                evicted_id = next(iter(self._sessions))
                self._remove_locked(evicted_id)
                invalidated.append(evicted_id)
        await self._notify_invalidated(invalidated)
        return session

    async def replace_anonymous(
        self,
        *,
        previous_session_id: str | None,
    ) -> CopilotSession:
        return await self.replace(
            previous_session_id=previous_session_id,
            access_token=None,
            claims={
                "tid": ANONYMOUS_TENANT_ID,
                "oid": str(uuid.uuid4()),
                "exp": int(time.time()) + self.ttl_seconds,
            },
            display_name="Anonymous",
            principal_name="anonymous",
            auth_mode="anonymous",
        )

    async def get(self, session_id: str | None) -> CopilotSession | None:
        if not is_valid_session_id(session_id):
            return None
        now = int(time.time())
        async with self._lock:
            invalidated = self._prune_locked(now)
            session = self._sessions.get(session_id)
            if session:
                self._sessions.move_to_end(session_id)
        await self._notify_invalidated(invalidated)
        return session

    async def delete(self, session_id: str | None) -> None:
        if not is_valid_session_id(session_id):
            return
        async with self._lock:
            current_session_id = self._current_lineage_session_locked(
                session_id
            )
            self._remove_locked(current_session_id)
        # Always notify. The process may still have a live upgraded socket after
        # the bounded state entry was evicted or expired.
        await self._notify_invalidated(
            [session_id, current_session_id]
        )

    async def count(self) -> int:
        async with self._lock:
            invalidated = self._prune_locked(int(time.time()))
            count = len(self._sessions)
        await self._notify_invalidated(invalidated)
        return count


class BootstrapRateLimiter:
    """Bound bootstrap attempts without retaining client identifiers."""

    def __init__(
        self,
        *,
        max_attempts: int,
        window_seconds: int = 60,
        max_keys: int = 4096,
    ):
        if max_attempts < 1 or window_seconds < 1 or max_keys < 1:
            raise ValueError("Bootstrap rate-limit bounds must be positive.")
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._windows: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def retry_after(self, key: str) -> int | None:
        now = time.monotonic()
        async with self._lock:
            while self._windows:
                oldest_key, (started_at, _) = next(iter(self._windows.items()))
                if now - started_at < self.window_seconds:
                    break
                self._windows.pop(oldest_key, None)

            window = self._windows.get(key)
            if window is None:
                self._windows[key] = (now, 1)
                while len(self._windows) > self.max_keys:
                    self._windows.popitem(last=False)
                return None

            started_at, attempts = window
            if attempts >= self.max_attempts:
                return max(
                    1,
                    math.ceil(self.window_seconds - (now - started_at)),
                )

            self._windows[key] = (started_at, attempts + 1)
            return None


def bootstrap_rate_limit_key(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    origins = request.headers.getlist("Origin")
    origin = origins[0].lower().rstrip("/") if len(origins) == 1 else "<invalid>"
    material = f"{peer}\n{origin}".encode("utf-8", errors="replace")
    return hashlib.sha256(material).hexdigest()


_session_store: CopilotSessionStore | None = None
_request_copilot_session: ContextVar[CopilotSession | None] = ContextVar(
    "request_copilot_session",
    default=None,
)


def configure_session_store(
    *,
    max_sessions: int,
    ttl_seconds: int,
    on_invalidate: SessionInvalidationCallback | None = None,
) -> CopilotSessionStore:
    global _session_store
    _session_store = CopilotSessionStore(
        max_sessions=max_sessions,
        ttl_seconds=ttl_seconds,
        on_invalidate=on_invalidate,
    )
    return _session_store


def get_session_store() -> CopilotSessionStore:
    if _session_store is None:
        raise RuntimeError("Copilot session state has not been configured.")
    return _session_store


def bind_request_copilot_session(
    session: CopilotSession,
) -> Token[CopilotSession | None]:
    return _request_copilot_session.set(session)


def reset_request_copilot_session(token: Token[CopilotSession | None]) -> None:
    _request_copilot_session.reset(token)


def get_request_copilot_session() -> CopilotSession | None:
    return _request_copilot_session.get()


def is_valid_session_id(value: str | None) -> bool:
    return bool(value and _SESSION_ID_PATTERN.fullmatch(value))


def create_embed_session_jwt(user: User, expires_at: int) -> str:
    session_token = create_jwt(user)
    payload = jwt.decode(
        session_token,
        options={"verify_signature": False},
        algorithms=["HS256"],
    )
    payload["exp"] = min(int(payload["exp"]), int(expires_at))
    secret = os.environ.get("CHAINLIT_AUTH_SECRET")
    if not secret:
        raise RuntimeError(
            "CHAINLIT_AUTH_SECRET is required for authenticated sessions."
        )
    return jwt.encode(payload, secret, algorithm="HS256")


def set_copilot_session_cookie(
    response: Response,
    session: CopilotSession,
    *,
    same_site: str,
    path: str = "/",
) -> None:
    response.set_cookie(
        key=COPILOT_SESSION_COOKIE,
        value=session.session_id,
        max_age=max(0, session.expires_at - int(time.time())),
        path=path,
        secure=True,
        httponly=True,
        samesite=same_site,
    )
    if path != "/":
        response.delete_cookie(
            key=COPILOT_SESSION_COOKIE,
            path="/",
            secure=True,
            httponly=True,
            samesite=same_site,
        )


def clear_copilot_session_cookie(
    response: Response,
    *,
    same_site: str,
    path: str = "/",
) -> None:
    response.delete_cookie(
        key=COPILOT_SESSION_COOKIE,
        path=path,
        secure=True,
        httponly=True,
        samesite=same_site,
    )
    if path != "/":
        response.delete_cookie(
            key=COPILOT_SESSION_COOKIE,
            path="/",
            secure=True,
            httponly=True,
            samesite=same_site,
        )


def session_id_from_request(request: Request) -> str | None:
    raw_cookie_headers = request.headers.getlist("Cookie")
    occurrences = 0
    session_id = None
    for raw_cookie in raw_cookie_headers:
        cookie = SimpleCookie()
        try:
            cookie.load(raw_cookie)
        except (CookieError, TypeError, ValueError):
            return None
        if raw_cookie.strip() and not cookie:
            return None
        occurrences += sum(
            1
            for part in raw_cookie.split(";")
            if part.partition("=")[0].strip() == COPILOT_SESSION_COOKIE
        )
        if COPILOT_SESSION_COOKIE in cookie:
            if session_id is not None:
                return None
            session_id = cookie[COPILOT_SESSION_COOKIE].value
    if occurrences != 1:
        return None
    return session_id if is_valid_session_id(session_id) else None


class TokenValidator(Protocol):
    async def validate(self, token: str) -> dict:
        ...








async def _active_copilot_session(
    metadata: dict | None,
) -> CopilotSession | None:
    metadata = metadata or {}
    if metadata.get("auth_source") != "copilot_session":
        return None
    try:
        store = get_session_store()
    except RuntimeError:
        return None
    session = await store.get(str(metadata.get("copilot_session_id") or ""))
    if (
        not session
        or session.principal_id != metadata.get("principal_id")
        or session.auth_mode != metadata.get("copilot_auth_mode")
    ):
        return None
    return session


async def resolve_access_token(metadata: dict | None) -> str | None:
    metadata = metadata or {}
    if metadata.get("auth_source") != "copilot_session":
        token = str(metadata.get("access_token") or "").strip()
        return token or None

    session = await _active_copilot_session(metadata)
    return session.access_token if session else None


async def is_copilot_session_active(metadata: dict | None) -> bool:
    metadata = metadata or {}
    if metadata.get("auth_source") != "copilot_session":
        return True
    return bool(await _active_copilot_session(metadata))
