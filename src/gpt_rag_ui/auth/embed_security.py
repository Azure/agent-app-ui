import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable, Collection
from contextvars import ContextVar
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlsplit
from weakref import WeakValueDictionary

from socketio.exceptions import (
    ConnectionRefusedError as SocketIOConnectionRefusedError,
)
from starlette.responses import PlainTextResponse

from gpt_rag_ui.auth.embed_auth import (
    bind_request_copilot_session,
    COPILOT_SESSION_COOKIE,
    CopilotSessionStore,
    is_valid_session_id,
    reset_request_copilot_session,
)
from gpt_rag_ui.config.embed_config import EmbedSettings
from gpt_rag_ui.telemetry.boundary_diagnostics import log_boundary_failure


logger = logging.getLogger("gpt_rag_ui.embed_security")
_ALWAYS_EMBEDDED_HTTP_PATHS = {
    "/copilot/auth/bootstrap",
    "/copilot/auth/logout",
}
_EMBEDDED_AUTH_PATHS = {
    "/login",
    "/logout",
    "/set-session-cookie",
    "/auth/jwt",
    "/auth/header",
}
_COPILOT_ONLY_PATH_PREFIXES = ("/copilot",)
_COPILOT_SCOPE_SESSION_KEY = "gpt_rag.copilot_session_id"
_ANONYMOUS_DENIED_HTTP_PREFIXES = (
    "/feedback",
    "/mcp",
    "/project/action",
    "/project/element",
    "/project/file",
    "/project/share",
    "/project/thread",
    "/project/threads",
)
_BLOCKED_BRIDGE_EVENTS = {"call_fn", "window_message"}
DEFAULT_MAX_CONNECTIONS_PER_SESSION = 4
_copilot_sio = None
_copilot_socket_registry = None
_copilot_disconnect_socket = None
_copilot_task_context: ContextVar[tuple | None] = ContextVar(
    "copilot_task_context",
    default=None,
)
_copilot_task_collector: ContextVar[list[asyncio.Task] | None] = ContextVar(
    "copilot_task_collector",
    default=None,
)

SocketDisconnect = Callable[[str], Awaitable[None]]


class CopilotSocketRegistry:
    """Track admitted physical Socket.IO connections for Copilot sessions."""

    def __init__(
        self,
        *,
        max_connections_per_session: int = DEFAULT_MAX_CONNECTIONS_PER_SESSION,
    ):
        if max_connections_per_session < 1:
            raise ValueError("The Copilot socket connection bound must be positive.")
        self.max_connections_per_session = max_connections_per_session
        self._session_engineio_sids: dict[str, set[str]] = {}
        self._engineio_sessions: dict[str, str] = {}
        self._invalidating_engineio_sids: set[str] = set()
        self._session_sockets: dict[str, set[str]] = {}
        self._socket_sessions: dict[str, str] = {}
        self._socket_engineio_sids: dict[str, str] = {}
        self._socket_chainlit_sessions: dict[str, str] = {}
        self._session_tasks: dict[str, set[asyncio.Task]] = {}
        self._restore_cleanup_tasks: dict[
            tuple[str, str],
            set[asyncio.Task],
        ] = {}
        self._invalidating_sockets: set[str] = set()
        self._invalidating_sessions: set[str] = set()
        self._admitting_sessions: set[str] = set()
        self._lock = asyncio.Lock()

    def _remove_socket_locked(self, socket_id: str) -> None:
        session_id = self._socket_sessions.pop(socket_id, None)
        self._socket_engineio_sids.pop(socket_id, None)
        self._socket_chainlit_sessions.pop(socket_id, None)
        self._invalidating_sockets.discard(socket_id)
        if not session_id:
            return
        session_sockets = self._session_sockets.get(session_id)
        if session_sockets is None:
            return
        session_sockets.discard(socket_id)
        if not session_sockets:
            self._session_sockets.pop(session_id, None)
        self._clear_completed_invalidation(session_id)

    def _clear_completed_invalidation(self, session_id: str) -> None:
        if (
            session_id not in self._session_engineio_sids
            and session_id not in self._session_sockets
            and session_id not in self._session_tasks
            and session_id not in self._admitting_sessions
        ):
            self._invalidating_sessions.discard(session_id)

    async def reserve_engineio_transport(
        self,
        session_id: str,
        engineio_sid: str,
    ) -> bool:
        async with self._lock:
            if (
                session_id in self._invalidating_sessions
                or engineio_sid in self._invalidating_engineio_sids
            ):
                return False
            existing_session_id = self._engineio_sessions.get(engineio_sid)
            if existing_session_id:
                return existing_session_id == session_id
            engineio_sids = self._session_engineio_sids.setdefault(
                session_id,
                set(),
            )
            if len(engineio_sids) >= self.max_connections_per_session:
                return False
            engineio_sids.add(engineio_sid)
            self._engineio_sessions[engineio_sid] = session_id
            return True

    async def release_engineio_transport(self, engineio_sid: str) -> None:
        async with self._lock:
            self._invalidating_engineio_sids.discard(engineio_sid)
            session_id = self._engineio_sessions.pop(
                engineio_sid,
                None,
            )
            if not session_id:
                return
            engineio_sids = self._session_engineio_sids.get(session_id)
            if engineio_sids:
                engineio_sids.discard(engineio_sid)
                if not engineio_sids:
                    self._session_engineio_sids.pop(session_id, None)
            self._clear_completed_invalidation(session_id)

    async def engineio_session(
        self,
        engineio_sid: str,
    ) -> str | None:
        async with self._lock:
            return self._engineio_sessions.get(engineio_sid)

    def register_spawned_task(
        self,
        session_id: str,
        task: asyncio.Task,
    ) -> None:
        if session_id in self._invalidating_sessions:
            task.cancel()
            return
        self._session_tasks.setdefault(session_id, set()).add(task)

        def remove_completed_task(completed_task: asyncio.Task) -> None:
            tasks = self._session_tasks.get(session_id)
            if tasks:
                tasks.discard(completed_task)
                if not tasks:
                    self._session_tasks.pop(session_id, None)
            self._clear_completed_invalidation(session_id)

        task.add_done_callback(remove_completed_task)

    def register_restore_cleanup_tasks(
        self,
        session_id: str,
        chainlit_session_id: str,
        tasks: list[asyncio.Task],
    ) -> None:
        cleanup_tasks = [
            task
            for task in tasks
            if "clear_on_timeout"
            in getattr(task.get_coro(), "__qualname__", "")
        ]
        if not cleanup_tasks:
            return
        if session_id in self._invalidating_sessions:
            for task in cleanup_tasks:
                task.cancel()
            return
        cleanup_key = (session_id, chainlit_session_id)
        tracked_tasks = self._restore_cleanup_tasks.setdefault(
            cleanup_key,
            set(),
        )
        tracked_tasks.update(cleanup_tasks)

        def remove_completed_task(completed_task: asyncio.Task) -> None:
            current_tasks = self._restore_cleanup_tasks.get(
                cleanup_key
            )
            if not current_tasks:
                return
            current_tasks.discard(completed_task)
            if not current_tasks:
                self._restore_cleanup_tasks.pop(
                    cleanup_key,
                    None,
                )

        for task in cleanup_tasks:
            task.add_done_callback(remove_completed_task)

    async def cancel_restore_cleanup_tasks(
        self,
        session_id: str,
        chainlit_session_id: str,
    ) -> int:
        async with self._lock:
            tasks = tuple(
                self._restore_cleanup_tasks.pop(
                    (session_id, chainlit_session_id),
                    set(),
                )
            )
        cancelled = 0
        for task in tasks:
            if not task.done():
                task.cancel()
                cancelled += 1
        return cancelled

    async def bind_connection(
        self,
        *,
        session_id: str,
        socket_id: str,
        chainlit_session_id: str | None,
        disconnect: SocketDisconnect,
        engineio_sid: str | None = None,
    ) -> bool:
        async with self._lock:
            if (
                session_id in self._invalidating_sessions
                or session_id in self._admitting_sessions
            ):
                return False
            if (
                engineio_sid
                and (
                    self._engineio_sessions.get(engineio_sid) != session_id
                    or engineio_sid in self._invalidating_engineio_sids
                )
            ):
                return False
            if (
                self._socket_sessions.get(socket_id) == session_id
                and self._socket_chainlit_sessions.get(socket_id)
                == chainlit_session_id
                and socket_id not in self._invalidating_sockets
            ):
                return True
            if (
                socket_id in self._socket_sessions
                or socket_id in self._socket_chainlit_sessions
            ):
                return False

            session_sockets = self._session_sockets.get(session_id, set())
            replaced_socket_ids = tuple(
                sorted(
                    admitted_socket_id
                    for admitted_socket_id in session_sockets
                    if chainlit_session_id
                    and self._socket_chainlit_sessions.get(
                        admitted_socket_id
                    )
                    == chainlit_session_id
                )
            )
            if (
                not replaced_socket_ids
                and len(session_sockets)
                >= self.max_connections_per_session
            ):
                return False
            self._admitting_sessions.add(session_id)
            self._invalidating_sockets.update(replaced_socket_ids)

        try:
            try:
                for replaced_socket_id in replaced_socket_ids:
                    await disconnect(replaced_socket_id)
            except Exception:
                log_boundary_failure(logger,
                    "Failed to disconnect a superseded Copilot socket"
                )
                return False

            async with self._lock:
                for replaced_socket_id in replaced_socket_ids:
                    self._remove_socket_locked(replaced_socket_id)
                if session_id in self._invalidating_sessions:
                    return False
                if engineio_sid and (
                    self._engineio_sessions.get(engineio_sid) != session_id
                    or engineio_sid in self._invalidating_engineio_sids
                ):
                    return False
                if (
                    socket_id in self._socket_sessions
                    or socket_id in self._socket_chainlit_sessions
                ):
                    return False
                session_sockets = self._session_sockets.setdefault(
                    session_id,
                    set(),
                )
                if len(session_sockets) >= self.max_connections_per_session:
                    return False
                session_sockets.add(socket_id)
                self._socket_sessions[socket_id] = session_id
                if engineio_sid:
                    self._socket_engineio_sids[socket_id] = engineio_sid
                if chainlit_session_id:
                    self._socket_chainlit_sessions[
                        socket_id
                    ] = chainlit_session_id
                return True
        finally:
            async with self._lock:
                self._admitting_sessions.discard(session_id)
                self._clear_completed_invalidation(session_id)

    async def associate_chainlit_session(
        self,
        *,
        socket_id: str,
        chainlit_session_id: str,
    ) -> None:
        async with self._lock:
            if socket_id in self._socket_sessions:
                self._socket_chainlit_sessions[
                    socket_id
                ] = chainlit_session_id

    async def unbind_socket(self, socket_id: str) -> None:
        async with self._lock:
            self._remove_socket_locked(socket_id)

    async def socket_is_active(self, socket_id: str) -> bool:
        async with self._lock:
            return bool(
                socket_id in self._socket_sessions
                and socket_id not in self._invalidating_sockets
            )

    async def socket_is_tracked(self, socket_id: str) -> bool:
        async with self._lock:
            return socket_id in self._socket_sessions

    async def socket_binding(
        self,
        socket_id: str,
    ) -> tuple[str, str | None] | None:
        async with self._lock:
            session_id = self._socket_sessions.get(socket_id)
            if not session_id:
                return None
            return (
                session_id,
                self._socket_chainlit_sessions.get(socket_id),
            )

    async def invalidate_socket(self, socket_id: str) -> None:
        async with self._lock:
            if socket_id in self._socket_sessions:
                self._invalidating_sockets.add(socket_id)

    async def invalidate_engineio_sockets(self, engineio_sid: str) -> None:
        """Deny bound sockets without depending on a failing manager lookup."""
        async with self._lock:
            self._invalidating_engineio_sids.add(engineio_sid)
            self._invalidating_sockets.update(
                socket_id
                for socket_id, transport_id in self._socket_engineio_sids.items()
                if transport_id == engineio_sid
            )

    async def track_task(
        self,
        socket_id: str,
        task: asyncio.Task,
    ) -> str | None:
        async with self._lock:
            session_id = self._socket_sessions.get(socket_id)
            if (
                not session_id
                or socket_id in self._invalidating_sockets
            ):
                return None
            self._session_tasks.setdefault(session_id, set()).add(task)
            return session_id

    async def untrack_task(
        self,
        session_id: str,
        task: asyncio.Task,
    ) -> None:
        async with self._lock:
            tasks = self._session_tasks.get(session_id)
            if not tasks:
                return
            tasks.discard(task)
            if not tasks:
                self._session_tasks.pop(session_id, None)
            self._clear_completed_invalidation(session_id)

    async def session_resources(
        self,
        session_id: str,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        async with self._lock:
            socket_ids = tuple(
                sorted(self._session_sockets.get(session_id, set()))
            )
            chainlit_session_ids = tuple(
                dict.fromkeys(
                    self._socket_chainlit_sessions[socket_id]
                    for socket_id in socket_ids
                    if socket_id in self._socket_chainlit_sessions
                )
            )
            return socket_ids, chainlit_session_ids

    async def session_engineio_sids(
        self,
        session_id: str,
    ) -> tuple[str, ...]:
        async with self._lock:
            return tuple(
                sorted(
                    self._session_engineio_sids.get(
                        session_id,
                        set(),
                    )
                )
            )

    async def disconnect_session(
        self,
        *,
        session_id: str,
        additional_socket_ids: set[str],
        disconnect: SocketDisconnect,
    ) -> int:
        async with self._lock:
            tracked_socket_ids = set(
                self._session_sockets.get(session_id, set())
            )
            self._invalidating_sessions.add(session_id)
            self._invalidating_sockets.update(tracked_socket_ids)
            tasks = tuple(self._session_tasks.get(session_id, set()))
            restore_cleanup_tasks = tuple(
                task
                for cleanup_key, cleanup_tasks in tuple(
                    self._restore_cleanup_tasks.items()
                )
                if cleanup_key[0] == session_id
                for task in self._restore_cleanup_tasks.pop(
                    cleanup_key,
                    cleanup_tasks,
                )
            )
            socket_ids = tuple(
                sorted(tracked_socket_ids | additional_socket_ids)
            )

        current_task = asyncio.current_task()
        for task in tasks:
            if task is not current_task and not task.done():
                task.cancel()
        for task in restore_cleanup_tasks:
            if not task.done():
                task.cancel()

        disconnected_socket_ids = []
        for socket_id in socket_ids:
            try:
                await disconnect(socket_id)
                disconnected_socket_ids.append(socket_id)
            except Exception:
                log_boundary_failure(logger, "Failed to disconnect a Copilot socket")

        async with self._lock:
            for socket_id in disconnected_socket_ids:
                self._remove_socket_locked(socket_id)
            self._clear_completed_invalidation(session_id)
        return len(disconnected_socket_ids)


def _ensure_copilot_task_factory() -> None:
    loop = asyncio.get_running_loop()
    existing_factory = loop.get_task_factory()
    if getattr(existing_factory, "_gpt_rag_copilot_tasks", False):
        return

    def copilot_task_factory(loop, coroutine, **kwargs):
        if existing_factory:
            task = existing_factory(loop, coroutine, **kwargs)
        else:
            task = asyncio.Task(coroutine, loop=loop, **kwargs)
        context = kwargs.get("context")
        marker = (
            context.get(_copilot_task_context)
            if context is not None
            else _copilot_task_context.get()
        )
        collector = (
            context.get(_copilot_task_collector)
            if context is not None
            else _copilot_task_collector.get()
        )
        if collector is not None:
            collector.append(task)
        if marker:
            registry, session_id = marker
            registry.register_spawned_task(session_id, task)
        return task

    copilot_task_factory._gpt_rag_copilot_tasks = True
    loop.set_task_factory(copilot_task_factory)


def canonical_origin(value: str) -> str:
    if not value or value != value.strip() or any(ord(char) < 32 for char in value):
        return ""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    if (
        parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (port is not None and port <= 0)
    ):
        return ""
    default_port = 80 if parsed.scheme == "http" else 443
    host = (
        f"[{parsed.hostname.lower()}]"
        if ":" in parsed.hostname
        else parsed.hostname.lower()
    )
    suffix = f":{port}" if port and port != default_port else ""
    return f"{parsed.scheme}://{host}{suffix}"


def _header_values(scope, name: bytes) -> list[str]:
    return [
        value.decode("latin-1")
        for key, value in scope.get("headers", [])
        if key.lower() == name
    ]


def _parsed_cookies(scope) -> dict[str, str] | None:
    parsed: dict[str, str] = {}
    for raw_cookie in _header_values(scope, b"cookie"):
        segments = [segment.strip() for segment in raw_cookie.split(";")]
        if any(not segment or "=" not in segment for segment in segments):
            return None
        names = [segment.partition("=")[0].strip() for segment in segments]
        if any(not name or name in parsed for name in names):
            return None
        if len(names) != len(set(names)):
            return None

        cookie = SimpleCookie()
        try:
            cookie.load(raw_cookie)
        except (CookieError, TypeError, ValueError):
            return None
        if raw_cookie.strip() and not cookie:
            return None
        for name, morsel in cookie.items():
            if name in parsed:
                return None
            parsed[name] = morsel.value
    return parsed


def _cookie_value(scope, name: str) -> str | None:
    cookies = _parsed_cookies(scope)
    if cookies is None:
        return None
    return cookies.get(name)


def _has_cookie_name(scope, name: str) -> bool:
    return any(
        segment.partition("=")[0].strip() == name
        for raw_cookie in _header_values(scope, b"cookie")
        for segment in raw_cookie.split(";")
    )


def _inject_chainlit_cookie(
    scope,
    token: str,
    cookie_name: str,
) -> dict:
    cookies = _parsed_cookies(scope)
    existing = []
    if cookies:
        for name, value in cookies.items():
            if name == cookie_name or name.startswith(f"{cookie_name}_"):
                continue
            cookie = SimpleCookie()
            cookie[name] = value
            existing.append(f"{name}={cookie[name].coded_value}")
    existing.append(f"{cookie_name}={token}")

    headers = [
        (key, value)
        for key, value in scope.get("headers", [])
        if key.lower() != b"cookie"
    ]
    headers.append((b"cookie", "; ".join(existing).encode("latin-1")))
    updated = dict(scope)
    updated["headers"] = headers
    return updated


def _copilot_project_settings_send(send, auth_mode: str):
    """Expose only the capabilities supported by this Copilot auth mode."""

    pending_start = None

    async def send_response(message):
        nonlocal pending_start
        if message["type"] == "http.response.start":
            pending_start = dict(message)
            return

        if pending_start is not None:
            if (
                message["type"] == "http.response.body"
                and not message.get("more_body", False)
            ):
                try:
                    payload = json.loads(message.get("body", b""))
                    if auth_mode == "anonymous":
                        payload["dataPersistence"] = False
                        payload["threadResumable"] = False
                        payload["threadSharing"] = False
                    upload = (
                        payload.get("features", {})
                        .get("spontaneous_file_upload", {})
                    )
                    if isinstance(upload, dict):
                        upload["enabled"] = auth_mode == "entra"
                    body = json.dumps(
                        payload,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    message = dict(message)
                    message["body"] = body
                    headers = [
                        (name, value)
                        for name, value in pending_start.get("headers", [])
                        if name.lower() != b"content-length"
                    ]
                    headers.append(
                        (b"content-length", str(len(body)).encode("ascii"))
                    )
                    pending_start["headers"] = headers
                except (TypeError, ValueError, AttributeError):
                    logger.exception(
                        "Could not apply Copilot project settings for auth mode %s",
                        auth_mode,
                    )

            await send(pending_start)
            pending_start = None

        await send(message)

    return send_response


def _copilot_user_send(send):
    """Remove the opaque session marker from browser-visible user metadata."""

    pending_start = None

    async def send_response(message):
        nonlocal pending_start
        if message["type"] == "http.response.start":
            pending_start = dict(message)
            return

        if pending_start is not None:
            if (
                message["type"] == "http.response.body"
                and not message.get("more_body", False)
            ):
                try:
                    payload = json.loads(message.get("body", b""))
                    metadata = payload.get("metadata")
                    if isinstance(metadata, dict):
                        metadata.pop("copilot_session_id", None)
                    body = json.dumps(
                        payload,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    message = dict(message)
                    message["body"] = body
                    headers = [
                        (name, value)
                        for name, value in pending_start.get("headers", [])
                        if name.lower() != b"content-length"
                    ]
                    headers.append(
                        (b"content-length", str(len(body)).encode("ascii"))
                    )
                    pending_start["headers"] = headers
                except (TypeError, ValueError, AttributeError):
                    logger.exception(
                        "Could not sanitize the Copilot user response"
                    )

            await send(pending_start)
            pending_start = None

        await send(message)

    return send_response


class CopilotRequestMiddleware:
    def __init__(
        self,
        app,
        *,
        settings: EmbedSettings,
        sessions: CopilotSessionStore,
        standalone_available: bool = True,
    ):
        self.app = app
        self.settings = settings
        self.sessions = sessions
        self.allowed_origins = set(settings.runtime_allowed_origins)
        self.portal_origins = set(settings.allowed_origins)
        self.standalone_available = standalone_available
        self.chainlit_auth_cookie_name = os.environ.get(
            "CHAINLIT_AUTH_COOKIE_NAME",
            "access_token",
        )

    @staticmethod
    def _is_public_copilot_path(path: str) -> bool:
        return path == "/copilot" or path.startswith("/copilot/")

    @staticmethod
    def _is_explicit_copilot_path(path: str) -> bool:
        return any(
            path == prefix or path.startswith(f"{prefix}/")
            for prefix in _COPILOT_ONLY_PATH_PREFIXES
        )

    @staticmethod
    def _is_download_path(path: str) -> bool:
        return path == "/api/download" or path.startswith("/api/download/")

    @staticmethod
    def _is_embedded_auth_path(path: str) -> bool:
        return path in _EMBEDDED_AUTH_PATHS or path.startswith(
            "/auth/oauth/"
        )

    def _is_copilot_http(
        self,
        *,
        path: str,
        origin: str,
        has_copilot_cookie: bool,
    ) -> bool:
        if origin in self.portal_origins:
            return True
        if path in _ALWAYS_EMBEDDED_HTTP_PATHS:
            return True
        return self._is_download_path(path) and has_copilot_cookie

    async def _reject(self, scope, receive, send, status_code: int) -> None:
        if scope["type"] == "websocket":
            await send(
                {
                    "type": "websocket.close",
                    "code": 4401 if status_code == 401 else 1008,
                }
            )
            return
        response = PlainTextResponse(
            "Unauthorized" if status_code == 401 else "Origin not allowed",
            status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )
        await response(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "/")
        raw_origins = _header_values(scope, b"origin")
        raw_origin = raw_origins[0] if len(raw_origins) == 1 else ""
        origin = canonical_origin(raw_origin) if raw_origin else ""
        raw_session_id = _cookie_value(scope, COPILOT_SESSION_COOKIE)
        session_id = (
            raw_session_id if is_valid_session_id(raw_session_id) else None
        )
        is_allowed_origin = origin in self.allowed_origins
        has_copilot_cookie = _has_cookie_name(scope, COPILOT_SESSION_COOKIE)
        if scope["type"] == "http":
            is_copilot = self._is_copilot_http(
                path=path,
                origin=origin,
                has_copilot_cookie=has_copilot_cookie,
            )
        else:
            is_copilot = origin in self.portal_origins

        if len(raw_origins) > 1 or (
            raw_origins and not is_allowed_origin
        ):
            await self._reject(scope, receive, send, 403)
            return
        if scope["type"] == "websocket" and not raw_origins:
            await self._reject(scope, receive, send, 403)
            return
        if (
            path in _ALWAYS_EMBEDDED_HTTP_PATHS
            and origin not in self.portal_origins
        ):
            await self._reject(scope, receive, send, 403)
            return
        if is_copilot and path == "/logout":
            if session_id:
                await self.sessions.delete(session_id)
            response = PlainTextResponse(
                "",
                status_code=204,
                headers={"Cache-Control": "no-store"},
            )
            response.delete_cookie(
                COPILOT_SESSION_COOKIE,
                secure=True,
                httponly=True,
                samesite=self.settings.cookie_samesite,
            )
            await response(scope, receive, send)
            return
        if is_copilot and self._is_embedded_auth_path(path):
            response = PlainTextResponse(
                "Not found",
                status_code=404,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return

        is_preflight = (
            scope["type"] == "http"
            and scope.get("method", "").upper() == "OPTIONS"
        )
        is_download = self._is_download_path(path)
        request_session = None
        if (
            is_copilot
            and not self._is_public_copilot_path(path)
            and not is_preflight
        ):
            request_session = await self.sessions.get(session_id)
            if not request_session and not is_download:
                await self._reject(scope, receive, send, 401)
                return
            if request_session:
                if not is_download:
                    scope = _inject_chainlit_cookie(
                        scope,
                        request_session.chainlit_token,
                        self.chainlit_auth_cookie_name,
                    )
                scope[_COPILOT_SCOPE_SESSION_KEY] = session_id

            if (
                request_session
                and getattr(request_session, "auth_mode", "entra") == "anonymous"
                and any(
                    path == prefix or path.startswith(f"{prefix}/")
                    for prefix in _ANONYMOUS_DENIED_HTTP_PREFIXES
                )
            ):
                response = PlainTextResponse(
                    "This operation requires Entra Copilot mode.",
                    status_code=403,
                    headers={"Cache-Control": "no-store"},
                )
                await response(scope, receive, send)
                return

        if not is_copilot and not self.standalone_available:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1013})
                return
            response = PlainTextResponse(
                "Standalone authentication is not configured.",
                status_code=503,
                headers={
                    "Cache-Control": "no-store",
                    "Retry-After": "30",
                },
            )
            await response(scope, receive, send)
            return

        if not request_session:
            await self.app(scope, receive, send)
            return

        context_token = bind_request_copilot_session(request_session)
        try:
            downstream_send = send
            if scope["type"] == "http":
                if path == "/project/settings":
                    downstream_send = _copilot_project_settings_send(
                        send,
                        request_session.auth_mode,
                    )
                elif path == "/user":
                    downstream_send = _copilot_user_send(send)
            await self.app(scope, receive, downstream_send)
        finally:
            reset_request_copilot_session(context_token)


def _is_copilot_socket(socket_id: str) -> bool:
    from chainlit.session import WebsocketSession

    session = WebsocketSession.get(socket_id)
    if not session:
        return False
    if session.client_type == "copilot":
        return True
    return bool(
        session.user
        and (session.user.metadata or {}).get("auth_source") == "copilot_session"
    )


def _has_copilot_sockets() -> bool:
    from chainlit.session import ws_sessions_sid

    return any(
        _is_copilot_socket(socket_id)
        for socket_id in list(ws_sessions_sid)
    )


def _socket_target(args, kwargs) -> str | None:
    target = kwargs.get("to") or kwargs.get("room")
    if target is None and len(args) > 1:
        target = args[1]
    return target if isinstance(target, str) and target else None


def _target_has_copilot_sockets(sio, target: str | None) -> bool:
    if not target:
        return _has_copilot_sockets()
    if _is_copilot_socket(target):
        return True

    try:
        participants = sio.manager.get_participants("/", target)
        for participant in participants:
            socket_id = participant[0] if isinstance(participant, tuple) else participant
            if isinstance(socket_id, str) and _is_copilot_socket(socket_id):
                return True
        return False
    except Exception:
        log_boundary_failure(logger, "Could not resolve Socket.IO bridge recipients")
        # Default deny only when a Copilot socket could receive the event.
        return _has_copilot_sockets()


def _existing_socket_session(session_id: str | None):
    if not session_id:
        return None
    from chainlit.session import WebsocketSession

    return WebsocketSession.get_by_id(session_id)


async def _authenticated_socket_user(environ):
    from chainlit.auth import authenticate_user
    from chainlit.socket import _get_token

    token = _get_token(environ)
    user = await authenticate_user(token) if token else None
    environ["_gpt_rag_authenticated_token"] = token
    return user


def _copilot_session_id_from_environ(environ) -> str | None:
    scope = environ.get("asgi.scope") if isinstance(environ, dict) else None
    if isinstance(scope, dict):
        session_id = _cookie_value(scope, COPILOT_SESSION_COOKIE)
        return session_id if is_valid_session_id(session_id) else None
    raw_cookie = (
        environ.get("HTTP_COOKIE", "")
        if isinstance(environ, dict)
        else ""
    )
    cookie = SimpleCookie()
    try:
        cookie.load(raw_cookie)
    except (CookieError, TypeError, ValueError):
        return None
    morsel = cookie.get(COPILOT_SESSION_COOKIE)
    session_id = morsel.value if morsel else None
    return session_id if is_valid_session_id(session_id) else None


def _has_verified_portal_origin(
    environ,
    portal_origins: Collection[str],
) -> bool:
    if not isinstance(environ, dict):
        return False

    scope = environ.get("asgi.scope")
    if isinstance(scope, dict):
        raw_origins = _header_values(scope, b"origin")
        if len(raw_origins) != 1:
            return False
        raw_origin = raw_origins[0]
    else:
        raw_origin = environ.get("HTTP_ORIGIN")
        if not isinstance(raw_origin, str):
            return False

    origin = canonical_origin(raw_origin)
    return bool(origin and origin in portal_origins)


def _copilot_session_marker(user) -> str:
    return str(
        ((getattr(user, "metadata", None) or {}).get("copilot_session_id") or "")
    )


def _engineio_sid_for_socket(sio, socket_id: str) -> str | None:
    manager = getattr(sio, "manager", None)
    resolve = getattr(manager, "eio_sid_from_sid", None)
    if not callable(resolve):
        return None
    return resolve(socket_id, "/")


async def _close_engineio_transport(sio, engineio_sid: str | None) -> None:
    engineio_server = getattr(sio, "eio", None)
    if engineio_sid and engineio_server:
        await engineio_server.disconnect(engineio_sid)


async def disconnect_copilot_session(session_id: str) -> int:
    """Disconnect every live Chainlit socket bound to an opaque session."""

    if not is_valid_session_id(session_id):
        return 0

    from chainlit.session import WebsocketSession, ws_sessions_sid

    tracked_socket_ids: tuple[str, ...] = ()
    tracked_chainlit_session_ids: tuple[str, ...] = ()
    if _copilot_socket_registry:
        (
            tracked_socket_ids,
            tracked_chainlit_session_ids,
        ) = await _copilot_socket_registry.session_resources(session_id)

    matches = {
        id(session): session
        for session in list(ws_sessions_sid.values())
        if _copilot_session_marker(session.user) == session_id
    }
    for chainlit_session_id in tracked_chainlit_session_ids:
        if session := WebsocketSession.get_by_id(chainlit_session_id):
            matches[id(session)] = session

    current_task = asyncio.current_task()
    for session in matches.values():
        session.to_clear = True
        task = getattr(session, "current_task", None)
        if task and task is not current_task and not task.done():
            task.cancel()

    socket_ids = set(tracked_socket_ids)
    socket_ids.update(
        session.socket_id
        for session in matches.values()
        if getattr(session, "socket_id", None)
    )
    if not _copilot_sio:
        return 0

    async def disconnect(socket_id: str) -> None:
        if _copilot_disconnect_socket:
            await _copilot_disconnect_socket(socket_id)
        else:
            await _copilot_sio.disconnect(socket_id)

    if _copilot_socket_registry:
        disconnected = await _copilot_socket_registry.disconnect_session(
            session_id=session_id,
            additional_socket_ids=socket_ids,
            disconnect=disconnect,
        )
        for engineio_sid in (
            await _copilot_socket_registry.session_engineio_sids(
                session_id
            )
        ):
            try:
                await _close_engineio_transport(
                    _copilot_sio,
                    engineio_sid,
                )
            except Exception:
                log_boundary_failure(logger,
                    "Failed to disconnect a Copilot Engine.IO transport"
                )
        return disconnected

    disconnected = 0
    for socket_id in sorted(socket_ids):
        try:
            await disconnect(socket_id)
            disconnected += 1
        except Exception:
            log_boundary_failure(logger, "Failed to disconnect a Copilot socket")
    return disconnected


async def _terminate_socket(
    sio,
    registry: CopilotSocketRegistry,
    socket_id: str,
    disconnect: SocketDisconnect | None = None,
) -> bool:
    from chainlit.session import WebsocketSession

    session = WebsocketSession.get(socket_id)
    if session:
        session.to_clear = True
        task = getattr(session, "current_task", None)
        if (
            task
            and task is not asyncio.current_task()
            and not task.done()
        ):
            task.cancel()
    await registry.invalidate_socket(socket_id)
    try:
        await (disconnect or sio.disconnect)(socket_id)
    except Exception:
        log_boundary_failure(logger, "Failed to terminate an invalid Copilot socket")
        return False
    try:
        if (
            session
            and WebsocketSession.get(socket_id) is session
            and callable(delete_session := getattr(session, "delete", None))
        ):
            await delete_session()
    except Exception:
        log_boundary_failure(logger, "Failed to delete an invalid Chainlit session")
    finally:
        await registry.unbind_socket(socket_id)
    return True


def configure_copilot_bridge_guards(
    sio,
    *,
    sessions: CopilotSessionStore,
    portal_origins: Collection[str],
) -> None:
    global _copilot_disconnect_socket, _copilot_sio, _copilot_socket_registry
    _copilot_sio = sio

    if getattr(sio, "_gpt_rag_copilot_guards", False):
        return

    configured_portal_origins = frozenset(portal_origins)
    registry = CopilotSocketRegistry()
    _copilot_socket_registry = registry
    original_emit = sio.emit
    original_call = sio.call
    handlers = sio.handlers.get("/", {})
    original_connect = handlers.get("connect")
    original_disconnect = handlers.get("disconnect")
    teardown_locks: WeakValueDictionary[
        str,
        asyncio.Lock,
    ] = WeakValueDictionary()
    teardown_waiters: dict[str, asyncio.Future[None]] = {}
    disconnect_handler_completions: dict[
        str,
        asyncio.Future,
    ] = {}

    def teardown_lock(socket_id: str) -> asyncio.Lock:
        lock = teardown_locks.get(socket_id)
        if lock is None:
            lock = asyncio.Lock()
            teardown_locks[socket_id] = lock
        return lock

    async def disconnect_socket(
        socket_id: str,
        capture_tasks: list[asyncio.Task] | None = None,
    ) -> None:
        async with teardown_lock(socket_id):
            _ensure_copilot_task_factory()
            engineio_sid = _engineio_sid_for_socket(sio, socket_id)
            completion = asyncio.get_running_loop().create_future()
            teardown_waiters[socket_id] = completion
            collector_token = _copilot_task_collector.set(capture_tasks)
            try:
                try:
                    await sio.disconnect(socket_id)
                except Exception:
                    try:
                        await _close_engineio_transport(sio, engineio_sid)
                    except Exception:
                        # Do not expose transport details or replace the primary failure.
                        logger.error("Failed to close Copilot transport after disconnect failure")
                    raise
                if completion.done():
                    completion.result()
                elif handler_completion := (
                    disconnect_handler_completions.get(socket_id)
                ):
                    await asyncio.shield(handler_completion)
                elif original_disconnect:
                    await original_disconnect(socket_id)
                    if manager := getattr(sio, "manager", None):
                        await manager.disconnect(
                            socket_id,
                            namespace="/",
                            ignore_queue=True,
                        )
                await _close_engineio_transport(sio, engineio_sid)
                await registry.unbind_socket(socket_id)
            finally:
                _copilot_task_collector.reset(collector_token)
                if teardown_waiters.get(socket_id) is completion:
                    teardown_waiters.pop(socket_id, None)

    async def close_rejected_socket(socket_id: str) -> None:
        try:
            await _close_engineio_transport(
                sio,
                _engineio_sid_for_socket(sio, socket_id),
            )
        except Exception:
            log_boundary_failure(logger,
                "Failed to close a rejected Copilot transport"
            )

    _copilot_disconnect_socket = disconnect_socket

    engineio_server = getattr(sio, "eio", None)
    engineio_handlers = getattr(engineio_server, "handlers", None)
    engineio_admission_enabled = False
    if isinstance(engineio_handlers, dict):
        original_engineio_connect = engineio_handlers.get("connect")
        original_engineio_disconnect = engineio_handlers.get("disconnect")

        if original_engineio_connect:
            engineio_admission_enabled = True

            async def guarded_engineio_connect(engineio_sid, environ):
                if not _has_verified_portal_origin(
                    environ,
                    configured_portal_origins,
                ):
                    return await original_engineio_connect(
                        engineio_sid,
                        environ,
                    )

                session_id = _copilot_session_id_from_environ(environ)
                if not session_id or not await sessions.get(session_id):
                    return False
                reserved = await registry.reserve_engineio_transport(
                    session_id,
                    engineio_sid,
                )
                if not reserved:
                    return False
                try:
                    result = await original_engineio_connect(
                        engineio_sid,
                        environ,
                    )
                except BaseException:
                    if reserved:
                        await registry.release_engineio_transport(
                            engineio_sid
                        )
                    raise
                if result is False and reserved:
                    await registry.release_engineio_transport(engineio_sid)
                return result

            engineio_server.on(
                "connect",
                handler=guarded_engineio_connect,
            )

        if original_engineio_disconnect:

            async def guarded_engineio_disconnect(
                engineio_sid,
                *args,
                **kwargs,
            ):
                if not await registry.engineio_session(engineio_sid):
                    return await original_engineio_disconnect(
                        engineio_sid,
                        *args,
                        **kwargs,
                    )
                # Engine.IO 4.x calls this while closing, before closed=True.
                # Keep failed cleanup discoverable and deny messages even when
                # namespace enumeration or SID resolution is unavailable.
                await registry.invalidate_engineio_sockets(engineio_sid)
                try:
                    result = await original_engineio_disconnect(
                        engineio_sid,
                        *args,
                        **kwargs,
                    )
                except Exception:
                    log_boundary_failure(logger,
                        "Engine.IO disconnect cleanup failed; forcing "
                        "transport cleanup"
                    )
                    manager = getattr(sio, "manager", None)
                    try:
                        namespaces = tuple(manager.get_namespaces())
                    except Exception:
                        namespaces = ()
                        log_boundary_failure(logger,
                            "Failed to enumerate Socket.IO namespaces "
                            "during Engine.IO cleanup"
                        )
                    for namespace in namespaces:
                        try:
                            socket_id = manager.sid_from_eio_sid(
                                engineio_sid,
                                namespace,
                            )
                        except Exception:
                            log_boundary_failure(logger,
                                "Failed to resolve a Socket.IO transport "
                                "during Engine.IO cleanup"
                            )
                            continue
                        if not socket_id:
                            continue
                        try:
                            await manager.disconnect(
                                socket_id,
                                namespace=namespace,
                                ignore_queue=True,
                            )
                        except Exception:
                            log_boundary_failure(logger,
                                "Failed to force Socket.IO manager cleanup"
                            )
                            continue
                        await registry.unbind_socket(socket_id)
                    result = None
                await registry.release_engineio_transport(engineio_sid)
                return result

            engineio_server.on(
                "disconnect",
                handler=guarded_engineio_disconnect,
            )

    async def guarded_emit(event, *args, **kwargs):
        target = _socket_target(args, kwargs)
        if event in _BLOCKED_BRIDGE_EVENTS and _target_has_copilot_sockets(
            sio, target
        ):
            logger.warning("Blocked outbound %s for a Copilot session", event)
            return None
        return await original_emit(event, *args, **kwargs)

    async def guarded_call(event, *args, **kwargs):
        target = _socket_target(args, kwargs)
        if event in _BLOCKED_BRIDGE_EVENTS and _target_has_copilot_sockets(
            sio, target
        ):
            raise PermissionError(
                "Browser bridge calls are disabled for Copilot sessions."
            )
        return await original_call(event, *args, **kwargs)

    sio.emit = guarded_emit
    sio.call = guarded_call

    if original_connect:

        async def guarded_connect(socket_id, environ, auth):
            auth_payload = auth if isinstance(auth, dict) else {}
            requested_copilot = auth_payload.get("clientType") == "copilot"
            if not _has_verified_portal_origin(
                environ,
                configured_portal_origins,
            ):
                if requested_copilot:
                    logger.warning(
                        "Blocked Copilot Socket.IO connection from an "
                        "unverified portal origin"
                    )
                    await close_rejected_socket(socket_id)
                    raise SocketIOConnectionRefusedError(
                        "authentication failed"
                    )
                return await original_connect(socket_id, environ, auth)

            current_user = await _authenticated_socket_user(environ)
            metadata = (
                (getattr(current_user, "metadata", None) or {})
                if current_user
                else {}
            )
            copilot_session_id = _copilot_session_marker(current_user)
            is_copilot_identity = (
                metadata.get("auth_source") == "copilot_session"
            )
            if not requested_copilot or not is_copilot_identity:
                logger.warning(
                    "Blocked Socket.IO connection with mismatched Copilot "
                    "client type and authenticated identity"
                )
                await close_rejected_socket(socket_id)
                raise SocketIOConnectionRefusedError(
                    "authentication failed"
                )

            requested_chainlit_session_id = auth_payload.get("sessionId")
            chainlit_session_id = (
                requested_chainlit_session_id
                if isinstance(requested_chainlit_session_id, str)
                and requested_chainlit_session_id
                else None
            )
            existing = _existing_socket_session(chainlit_session_id)
            if existing and existing.user:
                same_principal = bool(
                    current_user
                    and current_user.identifier == existing.user.identifier
                )
                same_copilot_session = (
                    _copilot_session_marker(current_user)
                    == _copilot_session_marker(existing.user)
                )
                if not same_principal or not same_copilot_session:
                    logger.warning(
                        "Blocked Socket.IO session restore across "
                        "authenticated sessions"
                    )
                    await close_rejected_socket(socket_id)
                    raise SocketIOConnectionRefusedError(
                        "authentication failed"
                    )

            active_session = await sessions.get(copilot_session_id)
            engineio_sid = _engineio_sid_for_socket(sio, socket_id)
            if (
                not active_session
                or active_session.principal_id != current_user.identifier
                or (
                    engineio_admission_enabled
                    and (
                        not engineio_sid
                        or await registry.engineio_session(engineio_sid)
                        != copilot_session_id
                    )
                )
            ):
                logger.warning(
                    "Blocked Socket.IO connection for an inactive or "
                    "invalid Copilot session"
                )
                await close_rejected_socket(socket_id)
                raise SocketIOConnectionRefusedError("authentication failed")

            # Chainlit requires a sessionId for new sockets. It is a recovery
            # request only when that logical session already exists.
            if (
                getattr(active_session, "auth_mode", "entra") == "anonymous"
                and (existing or auth_payload.get("threadId"))
            ):
                logger.warning(
                    "Blocked thread recovery for an anonymous Copilot session"
                )
                await close_rejected_socket(socket_id)
                raise SocketIOConnectionRefusedError(
                    "authorization failed"
                )

            thread_id = auth_payload.get("threadId")
            if isinstance(thread_id, str) and thread_id:
                from chainlit.data import get_data_layer

                if data_layer := get_data_layer():
                    thread = await data_layer.get_thread(thread_id)
                    if (
                        thread
                        and thread.get("userIdentifier")
                        != current_user.identifier
                    ):
                        logger.warning(
                            "Blocked Copilot thread recovery across principals"
                        )
                        await close_rejected_socket(socket_id)
                        raise SocketIOConnectionRefusedError(
                            "authorization failed"
                        )

            restore_cleanup_tasks: list[asyncio.Task] = []

            async def disconnect_superseded(
                replaced_socket_id: str,
            ) -> None:
                try:
                    await disconnect_socket(
                        replaced_socket_id,
                        restore_cleanup_tasks,
                    )
                finally:
                    if chainlit_session_id:
                        registry.register_restore_cleanup_tasks(
                            copilot_session_id,
                            chainlit_session_id,
                            restore_cleanup_tasks,
                        )

            reserved = await registry.bind_connection(
                session_id=copilot_session_id,
                socket_id=socket_id,
                chainlit_session_id=chainlit_session_id,
                disconnect=disconnect_superseded,
                engineio_sid=(
                    engineio_sid
                    if engineio_admission_enabled
                    else None
                ),
            )
            if not reserved:
                await close_rejected_socket(socket_id)
                raise SocketIOConnectionRefusedError(
                    "authentication failed"
                )

            try:
                result = await original_connect(socket_id, environ, auth)
                if result is False:
                    await _terminate_socket(
                        sio,
                        registry,
                        socket_id,
                        disconnect_socket,
                    )
                    return result
                from chainlit.session import WebsocketSession

                websocket_session = WebsocketSession.get(socket_id)
                if (
                    existing is None
                    and websocket_session
                    and websocket_session.user is None
                    and not getattr(websocket_session, "restored", False)
                ):
                    websocket_session.user = current_user
                restored_chainlit_session_id = getattr(
                    websocket_session,
                    "id",
                    None,
                )
                if (
                    isinstance(restored_chainlit_session_id, str)
                    and restored_chainlit_session_id
                ):
                    await registry.associate_chainlit_session(
                        socket_id=socket_id,
                        chainlit_session_id=restored_chainlit_session_id,
                    )
                active_session = await sessions.get(copilot_session_id)
                if (
                    not websocket_session
                    or (
                        chainlit_session_id
                        and websocket_session.id != chainlit_session_id
                    )
                    or _copilot_session_marker(websocket_session.user)
                    != copilot_session_id
                    or not active_session
                    or active_session.principal_id
                    != current_user.identifier
                    or not await registry.socket_is_active(socket_id)
                ):
                    raise SocketIOConnectionRefusedError(
                        "authentication failed"
                    )
                await registry.cancel_restore_cleanup_tasks(
                    copilot_session_id,
                    websocket_session.id,
                )
                return result
            except asyncio.CancelledError:
                await _terminate_socket(
                    sio,
                    registry,
                    socket_id,
                    disconnect_socket,
                )
                raise
            except SocketIOConnectionRefusedError:
                await _terminate_socket(
                    sio,
                    registry,
                    socket_id,
                    disconnect_socket,
                )
                raise
            except Exception:
                log_boundary_failure(logger, "Chainlit Socket.IO connection failed")
                await _terminate_socket(
                    sio,
                    registry,
                    socket_id,
                    disconnect_socket,
                )
                raise SocketIOConnectionRefusedError(
                    "authentication failed"
                ) from None

        sio.on("connect", handler=guarded_connect)

    if original_disconnect:

        async def guarded_disconnect(socket_id, *args, **kwargs):
            binding = await registry.socket_binding(socket_id)
            if not binding:
                return await original_disconnect(socket_id)

            if existing_completion := disconnect_handler_completions.get(
                socket_id
            ):
                return await asyncio.shield(existing_completion)

            handler_completion = (
                asyncio.get_running_loop().create_future()
            )
            handler_completion.add_done_callback(
                lambda future: (
                    future.exception()
                    if not future.cancelled()
                    else None
                )
            )
            disconnect_handler_completions[
                socket_id
            ] = handler_completion
            engineio_sid = _engineio_sid_for_socket(sio, socket_id)
            captured_tasks: list[asyncio.Task] = []
            _ensure_copilot_task_factory()
            collector_token = _copilot_task_collector.set(
                captured_tasks
            )
            try:
                try:
                    result = await original_disconnect(socket_id)
                finally:
                    _copilot_task_collector.reset(collector_token)
                    if binding and binding[1]:
                        registry.register_restore_cleanup_tasks(
                            binding[0],
                            binding[1],
                            captured_tasks,
                        )
                completion = teardown_waiters.get(socket_id)
                if completion and not completion.done():
                    completion.set_result(None)
                else:
                    reason = args[0] if args else None
                    if reason == "client disconnect":
                        await _close_engineio_transport(
                            sio,
                            engineio_sid,
                        )
                    await registry.unbind_socket(socket_id)
                handler_completion.set_result(result)
                return result
            except asyncio.CancelledError:
                handler_completion.cancel()
                raise
            except Exception as error:
                handler_completion.set_exception(error)
                raise
            finally:
                if (
                    disconnect_handler_completions.get(socket_id)
                    is handler_completion
                ):
                    disconnect_handler_completions.pop(socket_id, None)

        sio.on("disconnect", handler=guarded_disconnect)

    guarded_active_events = (
        "audio_chunk",
        "audio_end",
        "audio_start",
        "chat_settings_change",
        "clear_session",
        "client_message",
        "connection_successful",
        "edit_message",
        "stop",
    )
    for event in guarded_active_events:
        original_handler = handlers.get(event)
        if not original_handler:
            continue

        async def guarded_active_handler(
            socket_id,
            *args,
            _event=event,
            _handler=original_handler,
            **kwargs,
        ):
            is_copilot_socket = (
                _is_copilot_socket(socket_id)
                or await registry.socket_is_tracked(socket_id)
            )
            if not is_copilot_socket:
                return await _handler(socket_id, *args, **kwargs)
            if not await registry.socket_is_active(socket_id):
                logger.warning(
                    "Blocked %s for an invalidated Copilot socket",
                    _event,
                )
                await _terminate_socket(
                    sio,
                    registry,
                    socket_id,
                    disconnect_socket,
                )
                return None
            task = asyncio.current_task()
            if task is None:
                await _terminate_socket(
                    sio,
                    registry,
                    socket_id,
                    disconnect_socket,
                )
                return None
            session_id = await registry.track_task(socket_id, task)
            if not session_id:
                await _terminate_socket(
                    sio,
                    registry,
                    socket_id,
                    disconnect_socket,
                )
                return None
            _ensure_copilot_task_factory()
            context_token = _copilot_task_context.set(
                (registry, session_id)
            )
            try:
                return await _handler(socket_id, *args, **kwargs)
            finally:
                _copilot_task_context.reset(context_token)
                await registry.untrack_task(session_id, task)

        sio.on(event, handler=guarded_active_handler)

    for event in ("call_fn", "window_message"):
        original_handler = handlers.get(event)
        if not original_handler:
            continue

        async def guarded_handler(
            socket_id,
            *args,
            _event=event,
            _handler=original_handler,
            **kwargs,
        ):
            if (
                _is_copilot_socket(socket_id)
                or await registry.socket_is_tracked(socket_id)
            ):
                logger.warning(
                    "Blocked inbound %s for a Copilot session",
                    _event,
                )
                return None
            return await _handler(socket_id, *args, **kwargs)

        sio.on(event, handler=guarded_handler)

    sio._gpt_rag_copilot_guards = True
