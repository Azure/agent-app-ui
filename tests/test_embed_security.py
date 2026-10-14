import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import gpt_rag_ui.auth.embed_security as embed_security
from fastapi import FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
from socketio.exceptions import (
    ConnectionRefusedError as SocketIOConnectionRefusedError,
)
from starlette.websockets import WebSocketDisconnect

from gpt_rag_ui.auth.embed_auth import COPILOT_SESSION_COOKIE
from gpt_rag_ui.config.embed_config import EmbedSettings
from gpt_rag_ui.auth.embed_security import (
    _authenticated_socket_user,
    _is_copilot_socket,
    _terminate_socket,
    configure_copilot_bridge_guards as _configure_copilot_bridge_guards,
    CopilotSocketRegistry,
    CopilotRequestMiddleware,
    disconnect_copilot_session,
)


PORTAL = "https://portal.example.com"
CHAT = "https://chat.example.com"
SESSION_ID = "s" * 43


def transport_environ(
    *,
    origins: tuple[str, ...] = (PORTAL,),
    session_id: str | None = SESSION_ID,
    scope_type: str = "http",
):
    headers = [(b"origin", origin.encode("latin-1")) for origin in origins]
    environ = {
        "asgi.scope": {
            "type": scope_type,
            "headers": headers,
        }
    }
    if len(origins) == 1:
        environ["HTTP_ORIGIN"] = origins[0]
    if session_id:
        cookie = f"{COPILOT_SESSION_COOKIE}={session_id}"
        headers.append((b"cookie", cookie.encode("latin-1")))
        environ["HTTP_COOKIE"] = cookie
    return environ


def engineio_guard_sio():
    original_connect = AsyncMock(return_value=None)
    original_disconnect = AsyncMock(return_value=None)

    class FakeEngineIo:
        def __init__(self):
            self.handlers = {
                "connect": original_connect,
                "disconnect": original_disconnect,
            }
            self.closed = []

        def on(self, event, handler):
            self.handlers[event] = handler

        async def disconnect(self, engineio_sid):
            self.closed.append(engineio_sid)
            await self.handlers["disconnect"](
                engineio_sid,
                "server disconnect",
            )

    class FakeSio:
        def __init__(self):
            self.emit = AsyncMock()
            self.call = AsyncMock()
            self.eio = FakeEngineIo()
            self.handlers = {"/": {}}

    return FakeSio(), original_connect


def configure_copilot_bridge_guards(sio, *, sessions):
    _configure_copilot_bridge_guards(
        sio,
        sessions=sessions,
        portal_origins=(PORTAL,),
    )


class FakeSessions:
    def __init__(self, session=None):
        self.session = session
        self.deleted_session_ids = []

    async def get(self, session_id):
        return self.session if session_id == SESSION_ID else None

    async def delete(self, session_id):
        self.deleted_session_ids.append(session_id)
        if session_id == SESSION_ID:
            self.session = None


class RealChainlitSio:
    def __init__(self, connect_handler):
        self.emit = AsyncMock()
        self.call = AsyncMock()
        self.disconnect = AsyncMock()
        self.manager = SimpleNamespace(
            eio_sid_from_sid=lambda socket_id, namespace: f"engine-{socket_id}"
        )
        self.eio = SimpleNamespace(disconnect=AsyncMock())
        self.handlers = {"/": {"connect": connect_handler}}

    def on(self, event, handler):
        self.handlers["/"][event] = handler


def create_app(session=None, *, upload_enabled=True):
    app = FastAPI()

    @app.get("/protected")
    async def protected(request: Request):
        return {"cookie": request.cookies.get("access_token")}

    @app.post("/copilot/auth/bootstrap")
    async def bootstrap():
        return {"ok": True}

    @app.post("/auth/jwt")
    async def jwt_auth():
        return {"unsafe": True}

    @app.get("/auth/oauth/azure-ad")
    async def oauth_auth():
        return {"unsafe": True}

    @app.post("/project/threads")
    async def threads():
        return {"unsafe": True}

    @app.post("/project/file")
    async def upload():
        return {"unsafe": True}

    @app.get("/project/settings")
    async def project_settings():
        return {
            "dataPersistence": True,
            "threadResumable": True,
            "threadSharing": True,
            "features": {
                "spontaneous_file_upload": {"enabled": upload_enabled},
            },
        }

    @app.get("/user")
    async def user():
        return {
            "identifier": "copilot-user",
            "metadata": {
                "auth_source": "copilot_session",
                "copilot_session_id": SESSION_ID,
            },
        }

    @app.get("/api/download/grant")
    async def download(request: Request):
        return {"access_token": request.cookies.get("access_token")}

    @app.websocket("/ws/socket.io")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("connected")

    settings = EmbedSettings(
        enabled=True,
        ui_origin=CHAT,
        allowed_origins=(PORTAL,),
    )
    app.add_middleware(
        CopilotRequestMiddleware,
        settings=settings,
        sessions=FakeSessions(session),
    )
    return app


class EmbedSecurityTests(unittest.TestCase):
    def test_bootstrap_requires_exact_origin(self):
        with TestClient(create_app(), base_url=CHAT) as client:
            self.assertEqual(
                403,
                client.post("/copilot/auth/bootstrap").status_code,
            )
            self.assertEqual(
                403,
                client.post(
                    "/copilot/auth/bootstrap",
                    headers={"Origin": "https://attacker.example.com"},
                ).status_code,
            )
            self.assertEqual(
                200,
                client.post(
                    "/copilot/auth/bootstrap",
                    headers={"Origin": PORTAL},
                ).status_code,
            )

    def test_bootstrap_rejects_non_origin_url_syntax(self):
        with TestClient(create_app(), base_url=CHAT) as client:
            for origin in (
                "https://user@portal.example.com",
                "https://portal.example.com/path",
                "https://portal.example.com?query=value",
                "https://portal.example.com#fragment",
            ):
                with self.subTest(origin=origin):
                    response = client.post(
                        "/copilot/auth/bootstrap",
                        headers={"Origin": origin},
                    )
                    self.assertEqual(403, response.status_code)

    def test_bootstrap_rejects_duplicate_origin_headers(self):
        with TestClient(create_app(), base_url=CHAT) as client:
            response = client.post(
                "/copilot/auth/bootstrap",
                headers=[("Origin", PORTAL), ("Origin", PORTAL)],
            )
        self.assertEqual(403, response.status_code)

    def test_empty_origin_header_is_rejected_as_malformed(self):
        with TestClient(create_app(), base_url=CHAT) as client:
            response = client.get(
                "/protected",
                headers=[("Origin", "")],
            )
        self.assertEqual(403, response.status_code)

    def test_portal_protected_request_requires_session(self):
        with TestClient(create_app(), base_url=CHAT) as client:
            response = client.get("/protected", headers={"Origin": PORTAL})
        self.assertEqual(401, response.status_code)

    def test_valid_session_injects_internal_chainlit_cookie(self):
        session = SimpleNamespace(chainlit_token="internal-chainlit-jwt")
        with TestClient(create_app(session), base_url=CHAT) as client:
            response = client.get(
                "/protected",
                headers={
                    "Origin": PORTAL,
                    "Cookie": (
                        f"{COPILOT_SESSION_COOKIE}={SESSION_ID}; "
                        "access_token=attacker-controlled"
                    ),
                },
            )
        self.assertEqual(200, response.status_code)
        self.assertEqual("internal-chainlit-jwt", response.json()["cookie"])

    def test_cookie_without_origin_does_not_select_copilot_policy(self):
        session = SimpleNamespace(
            chainlit_token="internal-chainlit-jwt",
            auth_mode="entra",
        )
        with TestClient(create_app(session), base_url=CHAT) as client:
            response = client.get(
                "/protected",
                headers={
                    "Cookie": f"{COPILOT_SESSION_COOKIE}={SESSION_ID}",
                },
            )

        self.assertEqual(200, response.status_code)
        self.assertIsNone(response.json()["cookie"])

    def test_referer_never_enables_copilot_auth(self):
        session = SimpleNamespace(chainlit_token="internal-chainlit-jwt")
        with TestClient(create_app(session), base_url=CHAT) as client:
            for referer in (
                f"{PORTAL}/page",
                "https://attacker@portal.example.com/page",
            ):
                with self.subTest(referer=referer):
                    response = client.get(
                        "/protected",
                        headers={
                            "Referer": referer,
                        },
                    )
                    self.assertEqual(200, response.status_code)
                    self.assertIsNone(response.json()["cookie"])

    def test_standalone_exact_origin_is_unchanged(self):
        with TestClient(create_app(), base_url=CHAT) as client:
            response = client.get("/protected", headers={"Origin": CHAT})
        self.assertEqual(200, response.status_code)
        self.assertIsNone(response.json()["cookie"])

    def test_top_level_download_can_use_standalone_auth_without_copilot_cookie(self):
        with TestClient(create_app(), base_url=CHAT) as client:
            response = client.get(
                "/api/download/grant",
                headers={"Cookie": "access_token=standalone-token"},
            )
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            "standalone-token",
            response.json()["access_token"],
        )

    def test_download_preserves_standalone_cookie_when_opaque_session_exists(self):
        session = SimpleNamespace(
            chainlit_token="internal-chainlit-jwt",
            auth_mode="entra",
        )
        with TestClient(create_app(session), base_url=CHAT) as client:
            response = client.get(
                "/api/download/grant",
                headers={
                    "Cookie": (
                        f"{COPILOT_SESSION_COOKIE}={SESSION_ID}; "
                        "access_token=standalone-token"
                    ),
                },
            )
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            "standalone-token",
            response.json()["access_token"],
        )

    def test_chainlit_jwt_and_header_routes_are_disabled_for_portal(self):
        with TestClient(create_app(), base_url=CHAT) as client:
            response = client.post("/auth/jwt", headers={"Origin": PORTAL})
        self.assertEqual(404, response.status_code)

    def test_anonymous_denies_manual_identity_and_persistence_routes(self):
        session = SimpleNamespace(
            chainlit_token="internal-chainlit-jwt",
            auth_mode="anonymous",
        )
        denied = (
            "/feedback",
            "/mcp",
            "/project/action",
            "/project/element",
            "/project/file",
            "/project/share",
            "/project/thread",
            "/project/threads",
        )
        with TestClient(create_app(session), base_url=CHAT) as client:
            for route in denied:
                with self.subTest(route=route):
                    response = client.post(
                        route,
                        headers={
                            "Origin": PORTAL,
                            "Cookie": (
                                f"{COPILOT_SESSION_COOKIE}={SESSION_ID}"
                            ),
                        },
                    )
                    self.assertEqual(403, response.status_code)

    def test_embedded_auth_routes_are_not_available(self):
        session = SimpleNamespace(
            chainlit_token="internal-chainlit-jwt",
            auth_mode="entra",
        )
        denied = (
            "/login",
            "/auth/jwt",
            "/auth/header",
            "/auth/oauth/azure-ad/callback",
            "/set-session-cookie",
        )
        with TestClient(create_app(session), base_url=CHAT) as client:
            for route in denied:
                with self.subTest(route=route):
                    response = client.get(
                        route,
                        headers={
                            "Origin": PORTAL,
                            "Cookie": (
                                f"{COPILOT_SESSION_COOKIE}={SESSION_ID}"
                            ),
                        },
                    )
                    self.assertEqual(404, response.status_code)

    def test_copilot_user_response_hides_opaque_session_id(self):
        session = SimpleNamespace(
            chainlit_token="internal-chainlit-jwt",
            auth_mode="anonymous",
        )
        with TestClient(create_app(session), base_url=CHAT) as client:
            response = client.get(
                "/user",
                headers={
                    "Origin": PORTAL,
                    "Cookie": f"{COPILOT_SESSION_COOKIE}={SESSION_ID}",
                },
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual("copilot-user", response.json()["identifier"])
        self.assertEqual(
            {"auth_source": "copilot_session"},
            response.json()["metadata"],
        )

    def test_native_embedded_logout_invalidates_session_and_cookie(self):
        session = SimpleNamespace(
            chainlit_token="internal-chainlit-jwt",
            auth_mode="anonymous",
        )
        app = create_app(session)
        sessions = app.user_middleware[0].kwargs["sessions"]
        with TestClient(app, base_url=CHAT) as client:
            response = client.get(
                "/logout",
                headers={
                    "Origin": PORTAL,
                    "Cookie": f"{COPILOT_SESSION_COOKIE}={SESSION_ID}",
                },
            )

        self.assertEqual(204, response.status_code)
        self.assertEqual([SESSION_ID], sessions.deleted_session_ids)
        self.assertIn(
            f"{COPILOT_SESSION_COOKIE}=",
            response.headers["set-cookie"],
        )

    def test_chainlit_oauth_is_disabled_only_for_copilot_session(self):
        session = SimpleNamespace(chainlit_token="internal-chainlit-jwt")
        with TestClient(create_app(session), base_url=CHAT) as client:
            standalone = client.get(
                "/auth/oauth/azure-ad",
                headers={"Origin": CHAT},
            )
            copilot = client.get(
                "/auth/oauth/azure-ad",
                headers={
                    "Origin": PORTAL,
                    "Cookie": f"{COPILOT_SESSION_COOKIE}={SESSION_ID}",
                },
            )
        self.assertEqual(200, standalone.status_code)
        self.assertEqual(404, copilot.status_code)

    def test_websocket_rejects_wrong_origin_and_missing_session(self):
        with TestClient(create_app(), base_url=CHAT) as client:
            with self.assertRaises(WebSocketDisconnect) as wrong_origin:
                with client.websocket_connect(
                    "/ws/socket.io",
                    headers={"Origin": "https://attacker.example.com"},
                ):
                    pass
            self.assertEqual(1008, wrong_origin.exception.code)

            with self.assertRaises(WebSocketDisconnect) as missing_session:
                with client.websocket_connect(
                    "/ws/socket.io",
                    headers={"Origin": PORTAL},
                ):
                    pass
            self.assertEqual(4401, missing_session.exception.code)

            with self.assertRaises(WebSocketDisconnect) as missing_origin:
                with client.websocket_connect(
                    "/ws/socket.io",
                    headers={
                        "Cookie": f"{COPILOT_SESSION_COOKIE}={SESSION_ID}",
                    },
                ):
                    pass
            self.assertEqual(1008, missing_origin.exception.code)

    def test_websocket_accepts_exact_origin_with_session(self):
        session = SimpleNamespace(chainlit_token="internal-chainlit-jwt")
        with TestClient(create_app(session), base_url=CHAT) as client:
            with client.websocket_connect(
                "/ws/socket.io",
                headers={
                    "Origin": PORTAL,
                    "Cookie": f"{COPILOT_SESSION_COOKIE}={SESSION_ID}",
                },
            ) as websocket:
                self.assertEqual("connected", websocket.receive_text())

    def test_websocket_cookie_does_not_select_embedded_policy_from_ui_origin(self):
        session = SimpleNamespace(chainlit_token="internal-chainlit-jwt")
        with TestClient(create_app(session), base_url=CHAT) as client:
            with client.websocket_connect(
                "/ws/socket.io",
                headers={
                    "Origin": CHAT,
                    "Cookie": f"{COPILOT_SESSION_COOKIE}={SESSION_ID}",
                },
            ) as websocket:
                self.assertEqual("connected", websocket.receive_text())

    def test_anonymous_copilot_disables_identity_bound_routes(self):
        session = SimpleNamespace(
            chainlit_token="internal-chainlit-jwt",
            auth_mode="anonymous",
        )
        with TestClient(create_app(session), base_url=CHAT) as client:
            headers = {
                "Origin": PORTAL,
                "Cookie": f"{COPILOT_SESSION_COOKIE}={SESSION_ID}",
            }
            threads = client.post("/project/threads", headers=headers)
            upload = client.post("/project/file", headers=headers)
            feedback = client.put("/feedback", headers=headers)

        self.assertEqual(403, threads.status_code)
        self.assertEqual(403, upload.status_code)
        self.assertEqual(403, feedback.status_code)

    def test_anonymous_copilot_hides_identity_bound_capabilities(self):
        session = SimpleNamespace(
            chainlit_token="internal-chainlit-jwt",
            auth_mode="anonymous",
        )
        with TestClient(create_app(session), base_url=CHAT) as client:
            response = client.get(
                "/project/settings",
                headers={
                    "Origin": PORTAL,
                    "Cookie": f"{COPILOT_SESSION_COOKIE}={SESSION_ID}",
                },
            )

        self.assertEqual(200, response.status_code)
        settings = response.json()
        self.assertFalse(settings["dataPersistence"])
        self.assertFalse(settings["threadResumable"])
        self.assertFalse(settings["threadSharing"])
        self.assertFalse(
            settings["features"]["spontaneous_file_upload"]["enabled"]
        )

    def test_entra_copilot_keeps_identity_bound_capabilities(self):
        session = SimpleNamespace(
            chainlit_token="internal-chainlit-jwt",
            auth_mode="entra",
        )
        with TestClient(create_app(session), base_url=CHAT) as client:
            response = client.get(
                "/project/settings",
                headers={
                    "Origin": PORTAL,
                    "Cookie": f"{COPILOT_SESSION_COOKIE}={SESSION_ID}",
                },
            )

        self.assertEqual(200, response.status_code)
        settings = response.json()
        self.assertTrue(settings["dataPersistence"])
        self.assertTrue(settings["threadResumable"])
        self.assertTrue(
            settings["features"]["spontaneous_file_upload"]["enabled"]
        )

    def test_entra_copilot_enables_upload_when_standalone_is_anonymous(self):
        session = SimpleNamespace(
            chainlit_token="internal-chainlit-jwt",
            auth_mode="entra",
        )
        with TestClient(
            create_app(session, upload_enabled=False),
            base_url=CHAT,
        ) as client:
            response = client.get(
                "/project/settings",
                headers={
                    "Origin": PORTAL,
                    "Cookie": f"{COPILOT_SESSION_COOKIE}={SESSION_ID}",
                },
            )

        self.assertEqual(200, response.status_code)
        settings = response.json()
        self.assertTrue(settings["dataPersistence"])
        self.assertTrue(settings["threadResumable"])
        self.assertTrue(
            settings["features"]["spontaneous_file_upload"]["enabled"]
        )


class RealChainlitBridgeGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import chainlit
        from chainlit.config import config

        self.assertEqual("2.12.0", chainlit.__version__)
        self.auth_patches = [
            patch.object(config.code, "oauth_callback", None),
            patch.object(config.code, "password_auth_callback", None),
            patch.object(config.code, "header_auth_callback", None),
            patch.dict("os.environ", {"CHAINLIT_CUSTOM_AUTH": ""}),
            patch("chainlit.session.ws_sessions_id", {}),
            patch("chainlit.session.ws_sessions_sid", {}),
        ]
        for auth_patch in self.auth_patches:
            auth_patch.start()
            self.addCleanup(auth_patch.stop)

    @staticmethod
    def copilot_user(
        *,
        principal_id: str,
        session_id: str,
        auth_mode: str,
    ):
        from chainlit.user import User

        tenant_id, object_id = principal_id.split(":", 1)
        return User(
            identifier=principal_id,
            display_name="Copilot user",
            metadata={
                "auth_source": "copilot_session",
                "copilot_auth_mode": auth_mode,
                "copilot_session_id": session_id,
                "tenant_id": tenant_id,
                "object_id": object_id,
                "principal_id": principal_id,
            },
        )

    @staticmethod
    def active_sessions(sessions_by_id):
        async def get(session_id):
            return sessions_by_id.get(session_id)

        return SimpleNamespace(get=get)

    async def connect_with_real_handler(
        self,
        *,
        user,
        auth_mode: str,
        socket_id: str,
        chainlit_session_id: str,
    ):
        from chainlit.session import WebsocketSession
        from chainlit.socket import connect

        sio = RealChainlitSio(connect)
        active_session = SimpleNamespace(
            principal_id=user.identifier,
            auth_mode=auth_mode,
        )
        configure_copilot_bridge_guards(
            sio,
            sessions=self.active_sessions(
                {user.metadata["copilot_session_id"]: active_session}
            ),
        )

        with patch(
            "gpt_rag_ui.auth.embed_security._authenticated_socket_user",
            AsyncMock(return_value=user),
        ):
            result = await sio.handlers["/"]["connect"](
                socket_id,
                transport_environ(
                    session_id=user.metadata["copilot_session_id"]
                ),
                {
                    "clientType": "copilot",
                    "sessionId": chainlit_session_id,
                    "userEnv": "{}",
                },
            )

        return result, WebsocketSession.get(socket_id)

    async def test_real_chainlit_connect_binds_canonical_entra_user(self):
        tenant_id = "11111111-1111-1111-1111-111111111111"
        object_id = "22222222-2222-2222-2222-222222222222"
        principal_id = f"{tenant_id}:{object_id}"
        user = self.copilot_user(
            principal_id=principal_id,
            session_id=SESSION_ID,
            auth_mode="entra",
        )

        result, websocket_session = await self.connect_with_real_handler(
            user=user,
            auth_mode="entra",
            socket_id="entra-socket",
            chainlit_session_id="entra-chainlit-session",
        )

        self.assertTrue(result)
        self.assertIsNotNone(websocket_session)
        self.assertIs(user, websocket_session.user)
        self.assertEqual(principal_id, websocket_session.user.identifier)

    async def test_real_chainlit_connect_binds_opaque_anonymous_user(self):
        principal_id = (
            "00000000-0000-0000-0000-000000000000:"
            "33333333-3333-3333-3333-333333333333"
        )
        user = self.copilot_user(
            principal_id=principal_id,
            session_id=SESSION_ID,
            auth_mode="anonymous",
        )

        result, websocket_session = await self.connect_with_real_handler(
            user=user,
            auth_mode="anonymous",
            socket_id="anonymous-socket",
            chainlit_session_id="anonymous-chainlit-session",
        )

        self.assertTrue(result)
        self.assertIsNotNone(websocket_session)
        self.assertIs(user, websocket_session.user)
        self.assertEqual(principal_id, websocket_session.user.identifier)
        self.assertNotEqual("anonymous", websocket_session.user.identifier)

    async def test_real_chainlit_standalone_no_auth_remains_unbound(self):
        from chainlit.session import WebsocketSession
        from chainlit.socket import connect

        sio = RealChainlitSio(connect)
        configure_copilot_bridge_guards(
            sio,
            sessions=self.active_sessions({}),
        )
        authenticate = AsyncMock(
            side_effect=AssertionError(
                "standalone must not use Copilot authentication"
            )
        )

        with patch(
            "gpt_rag_ui.auth.embed_security._authenticated_socket_user",
            authenticate,
        ):
            result = await sio.handlers["/"]["connect"](
                "standalone-socket",
                transport_environ(origins=(CHAT,), session_id=None),
                {
                    "clientType": "webapp",
                    "sessionId": "standalone-chainlit-session",
                    "userEnv": "{}",
                },
            )

        self.assertTrue(result)
        authenticate.assert_not_awaited()
        websocket_session = WebsocketSession.get("standalone-socket")
        self.assertIsNotNone(websocket_session)
        self.assertIsNone(websocket_session.user)

    async def test_real_chainlit_connect_keeps_admission_rejections(self):
        from chainlit.session import WebsocketSession
        from chainlit.socket import connect

        user = self.copilot_user(
            principal_id=(
                "11111111-1111-1111-1111-111111111111:"
                "22222222-2222-2222-2222-222222222222"
            ),
            session_id=SESSION_ID,
            auth_mode="entra",
        )
        cases = {
            "mismatched": (
                (PORTAL,),
                SimpleNamespace(principal_id="other:principal"),
            ),
            "expired": ((PORTAL,), None),
            "stale": ((PORTAL,), None),
            "unlisted": (("https://attacker.example.com",), None),
            "no-origin": ((), None),
        }

        for name, (origins, active_session) in cases.items():
            with self.subTest(name=name):
                sio = RealChainlitSio(connect)
                configure_copilot_bridge_guards(
                    sio,
                    sessions=self.active_sessions(
                        {SESSION_ID: active_session}
                        if active_session
                        else {}
                    ),
                )
                with (
                    patch(
                        "gpt_rag_ui.auth.embed_security._authenticated_socket_user",
                        AsyncMock(return_value=user),
                    ),
                    self.assertRaises(SocketIOConnectionRefusedError),
                ):
                    await sio.handlers["/"]["connect"](
                        f"{name}-socket",
                        transport_environ(
                            origins=origins,
                            session_id=SESSION_ID,
                        ),
                        {
                            "clientType": "copilot",
                            "sessionId": f"{name}-chainlit-session",
                            "userEnv": "{}",
                        },
                    )

                self.assertIsNone(WebsocketSession.get(f"{name}-socket"))

    async def test_concurrent_real_connects_do_not_cross_bind_principals(self):
        from chainlit.session import WebsocketSession
        from chainlit.socket import connect

        session_ids = ("a" * 43, "b" * 43)
        users = {
            session_ids[0]: self.copilot_user(
                principal_id=(
                    "11111111-1111-1111-1111-111111111111:"
                    "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
                ),
                session_id=session_ids[0],
                auth_mode="entra",
            ),
            session_ids[1]: self.copilot_user(
                principal_id=(
                    "22222222-2222-2222-2222-222222222222:"
                    "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
                ),
                session_id=session_ids[1],
                auth_mode="entra",
            ),
        }
        arrived = 0
        both_arrived = asyncio.Event()
        release = asyncio.Event()
        created_by = None
        created_session = None

        async def synchronized_connect(socket_id, environ, auth):
            nonlocal arrived, created_by, created_session
            arrived += 1
            if arrived == 2:
                both_arrived.set()
            await release.wait()
            result = await connect(socket_id, environ, auth)
            websocket_session = WebsocketSession.get(socket_id)
            if websocket_session and not websocket_session.restored:
                created_by = socket_id
                created_session = websocket_session
            return result

        sio = RealChainlitSio(synchronized_connect)
        configure_copilot_bridge_guards(
            sio,
            sessions=self.active_sessions(
                {
                    session_id: SimpleNamespace(
                        principal_id=user.identifier,
                        auth_mode="entra",
                    )
                    for session_id, user in users.items()
                }
            ),
        )

        async def authenticate(environ):
            session_id = embed_security._copilot_session_id_from_environ(
                environ
            )
            return users[session_id]

        async def attempt(socket_id, session_id):
            try:
                result = await sio.handlers["/"]["connect"](
                    socket_id,
                    transport_environ(session_id=session_id),
                    {
                        "clientType": "copilot",
                        "sessionId": "shared-chainlit-session",
                        "userEnv": "{}",
                    },
                )
                websocket_session = WebsocketSession.get(socket_id)
                return (
                    result,
                    (
                        websocket_session.user.identifier
                        if websocket_session and websocket_session.user
                        else None
                    ),
                )
            except SocketIOConnectionRefusedError:
                return False, None

        with patch(
            "gpt_rag_ui.auth.embed_security._authenticated_socket_user",
            side_effect=authenticate,
        ):
            tasks = [
                asyncio.create_task(
                    attempt(f"socket-{index}", session_id)
                )
                for index, session_id in enumerate(session_ids)
            ]
            await asyncio.wait_for(both_arrived.wait(), timeout=1)
            release.set()
            outcomes = await asyncio.gather(*tasks)

        expected_by_socket = {
            f"socket-{index}": users[session_id]
            for index, session_id in enumerate(session_ids)
        }
        self.assertIsNotNone(created_by)
        self.assertIsNotNone(created_session)
        self.assertIs(
            expected_by_socket[created_by],
            created_session.user,
        )
        self.assertIn((False, None), outcomes)
        for index, (connected, principal_id) in enumerate(outcomes):
            if connected:
                self.assertEqual(
                    users[session_ids[index]].identifier,
                    principal_id,
                )


class BridgeGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_engineio_admission_failure_releases_reservation_and_propagates(self):
        for failure in (RuntimeError("connect failed"), asyncio.CancelledError()):
            with self.subTest(failure=type(failure).__name__):
                sio, connect = engineio_guard_sio()
                connect.side_effect = failure
                configure_copilot_bridge_guards(
                    sio, sessions=FakeSessions(SimpleNamespace(principal_id="tenant:user")),
                )
                with self.assertRaises(type(failure)) as raised:
                    await sio.eio.handlers["connect"]("failed-connect", transport_environ())
                self.assertIs(failure, raised.exception)
                self.assertIsNone(
                    await embed_security._copilot_socket_registry.engineio_session("failed-connect")
                )

    async def _exercise_real_engineio_cleanup_failure(self, stage):
        from contextlib import ExitStack

        from engineio.async_socket import AsyncSocket
        from engineio.packet import MESSAGE, Packet
        from socketio import AsyncServer
        from socketio.async_server import task_reference_holder

        # Real Engine.IO close -> maintained adapter -> Socket.IO callback ->
        # real manager. Only the failing dependency is replaced.
        # Match Chainlit 2.12.0's server configuration, including background
        # Socket.IO event dispatch (Engine.IO dispatch is synchronous).
        sio = AsyncServer(cors_allowed_origins=[], async_mode="asgi")
        delivered = AsyncMock()
        failing = True

        async def receive_message(transport, message):
            existing_tasks = set(task_reference_holder)
            await transport.receive(Packet(
                MESSAGE, f'2["client_message",{{"message":"{message}"}}]',
            ))
            dispatched_tasks = set(task_reference_holder) - existing_tasks
            if dispatched_tasks:
                await asyncio.wait_for(asyncio.gather(*dispatched_tasks), 2)

        async def original_disconnect(*args):
            if failing:
                raise RuntimeError("application disconnect failed")

        sio.on("disconnect", original_disconnect, namespace="/other")
        sio.on("disconnect", original_disconnect)
        sio.on("client_message", delivered)
        configure_copilot_bridge_guards(
            sio, sessions=FakeSessions(SimpleNamespace(principal_id="tenant:user")),
        )
        registry = embed_security._copilot_socket_registry
        registry.max_connections_per_session = 1
        sock = AsyncSocket(sio.eio, "engine")
        sio.eio.sockets["engine"] = sock
        await sio.eio.handlers["connect"]("engine", transport_environ())
        # Lookup failure must precede the independent namespace in the real
        # manager's insertion order. For callback/manager failure, the other
        # namespace fails first so root has not been pre-disconnected for us.
        if stage == "lookup":
            root = await sio.manager.connect("engine", "/")
            other = await sio.manager.connect("engine", "/other")
        else:
            other = await sio.manager.connect("engine", "/other")
            root = await sio.manager.connect("engine", "/")
        # The maintained Chainlit adapter authorizes only the root namespace.
        self.assertTrue(await registry.bind_connection(
            session_id=SESSION_ID, socket_id=root,
            chainlit_session_id="logical-root", engineio_sid="engine",
            disconnect=embed_security._copilot_disconnect_socket,
        ))

        lookup = sio.manager.sid_from_eio_sid
        disconnect = sio.manager.disconnect

        def failing_lookup(eio_sid, namespace):
            if namespace == "/":
                raise RuntimeError("lookup failed")
            return lookup(eio_sid, namespace)

        async def failing_disconnect(sid, namespace, **kwargs):
            if namespace == "/":
                raise RuntimeError("manager disconnect failed")
            return await disconnect(sid, namespace, **kwargs)

        reached_release = asyncio.Event()
        finish_release = asyncio.Event()
        release = registry.release_engineio_transport

        async def paused_release(eio_sid):
            await release(eio_sid)
            reached_release.set()
            await finish_release.wait()

        with ExitStack() as stack:
            if stage == "namespaces":
                stack.enter_context(patch.object(
                    sio.manager, "get_namespaces",
                    side_effect=RuntimeError("enumeration failed"),
                ))
            elif stage == "lookup":
                stack.enter_context(patch.object(
                    sio.manager, "sid_from_eio_sid", side_effect=failing_lookup,
                ))
            elif stage == "disconnect":
                stack.enter_context(patch.object(
                    sio.manager, "disconnect", side_effect=failing_disconnect,
                ))
            stack.enter_context(patch.object(
                registry, "release_engineio_transport", side_effect=paused_release,
            ))
            with self.assertLogs(level="ERROR"):
                closing = asyncio.create_task(sock.close(wait=False, abort=True))
                try:
                    await asyncio.wait_for(reached_release.wait(), 2)
                    # Actual installed Engine.IO ordering, not a fake's promise:
                    self.assertTrue(sock.closing)
                    self.assertFalse(sock.closed)
                    self.assertIs(sio.eio._get_socket("engine"), sock)
                    self.assertIsNone(await registry.engineio_session("engine"))
                    self.assertFalse(await registry.socket_is_active(root))
                    self.assertEqual(
                        await registry.socket_is_tracked(root), stage != "original",
                    )
                    if stage != "original":
                        self.assertFalse(await registry.bind_connection(
                            session_id=SESSION_ID, socket_id="not-admitted",
                            chainlit_session_id="different-logical-session",
                            disconnect=embed_security._copilot_disconnect_socket,
                        ))
                    if stage != "namespaces":
                        self.assertFalse(sio.manager.is_connected(other, "/other"))
                        self.assertFalse(await registry.socket_is_tracked(other))
                    else:
                        self.assertTrue(sio.manager.is_connected(other, "/other"))
                    # Restore lookup solely for packet dispatch, while teardown
                    # and application callbacks still fail. Exercise the actual
                    # Engine.IO -> Socket.IO -> guarded client_message chain.
                    with patch.object(
                        sio.manager, "sid_from_eio_sid", side_effect=lookup,
                    ):
                        await receive_message(sock, "denied")
                    delivered.assert_not_awaited()
                finally:
                    finish_release.set()
                    await asyncio.wait_for(closing, 2)
        self.assertTrue(sock.closed)
        # Retry through the maintained recovery adapter after dependencies heal.
        # Retained bindings must remain discoverable until this succeeds.
        failing = False
        if await registry.socket_is_tracked(root):
            await embed_security._copilot_disconnect_socket(root)
        self.assertFalse(await registry.socket_is_tracked(root))
        self.assertFalse(sio.manager.is_connected(root, "/"))
        # A new authenticated transport can reserve capacity after reconciliation.
        await sio.eio.handlers["connect"]("replacement", transport_environ())
        self.assertEqual(await registry.engineio_session("replacement"), SESSION_ID)
        replacement = await sio.manager.connect("replacement", "/")
        self.assertTrue(await registry.bind_connection(
            session_id=SESSION_ID, socket_id=replacement,
            chainlit_session_id="logical-root", engineio_sid="replacement",
            disconnect=embed_security._copilot_disconnect_socket,
        ))
        await receive_message(AsyncSocket(sio.eio, "replacement"), "recovered")
        delivered.assert_awaited_once()

    async def test_real_engineio_original_failure_denies_messages_while_closing(self):
        await self._exercise_real_engineio_cleanup_failure("original")

    async def test_real_engineio_enumeration_failure_retains_invalidated_binding(self):
        await self._exercise_real_engineio_cleanup_failure("namespaces")

    async def test_real_engineio_lookup_failure_isolates_namespaces_and_recovers(self):
        await self._exercise_real_engineio_cleanup_failure("lookup")

    async def test_real_engineio_manager_failure_retains_binding_until_recovery(self):
        await self._exercise_real_engineio_cleanup_failure("disconnect")

    async def test_engineio_cancellation_retains_invalidated_association(self):
        from socketio import AsyncServer

        sio = AsyncServer(async_handlers=False)
        cancellation = asyncio.CancelledError("cancel teardown")

        async def cancelled_disconnect(*args):
            raise cancellation

        sio.eio.on("disconnect", cancelled_disconnect)
        configure_copilot_bridge_guards(
            sio, sessions=FakeSessions(SimpleNamespace(principal_id="tenant:user")),
        )
        registry = embed_security._copilot_socket_registry
        await sio.eio.handlers["connect"]("engine", transport_environ())
        root = await sio.manager.connect("engine", "/")
        self.assertTrue(await registry.bind_connection(
            session_id=SESSION_ID, socket_id=root,
            chainlit_session_id="logical-root", engineio_sid="engine",
            disconnect=embed_security._copilot_disconnect_socket,
        ))
        # Engine.IO and Socket.IO swallow CancelledError at event dispatchers;
        # the maintained callback must not change cancellation into success.
        with self.assertRaises(asyncio.CancelledError) as raised:
            await sio.eio.handlers["disconnect"]("engine", "transport close")
        self.assertIs(raised.exception, cancellation)
        self.assertTrue(await registry.socket_is_tracked(root))
        self.assertFalse(await registry.socket_is_active(root))
        self.assertEqual(await registry.engineio_session("engine"), SESSION_ID)
        self.assertFalse(await registry.reserve_engineio_transport(SESSION_ID, "engine"))

    async def test_transport_invalidation_blocks_inflight_binding_not_other_transport(self):
        registry = CopilotSocketRegistry()
        for engine in ("old", "new", "independent"):
            self.assertTrue(await registry.reserve_engineio_transport(SESSION_ID, engine))
        for sid, engine, logical in (
            ("old-root", "old", "logical"),
            ("independent-root", "independent", "separate"),
        ):
            self.assertTrue(await registry.bind_connection(
                session_id=SESSION_ID, socket_id=sid,
                chainlit_session_id=logical, engineio_sid=engine,
                disconnect=AsyncMock(),
            ))

        async def disconnect_replaced(sid):
            # The replacement is awaiting cleanup when its own transport ends.
            await registry.invalidate_engineio_sockets("new")
            await registry.release_engineio_transport("new")

        self.assertFalse(await registry.bind_connection(
            session_id=SESSION_ID, socket_id="new-root",
            chainlit_session_id="logical", engineio_sid="new",
            disconnect=disconnect_replaced,
        ))
        self.assertFalse(await registry.socket_is_tracked("new-root"))
        self.assertTrue(await registry.socket_is_active("independent-root"))
        await registry.invalidate_engineio_sockets("independent")
        self.assertFalse(await registry.bind_connection(
            session_id=SESSION_ID, socket_id="another-root",
            chainlit_session_id="another", engineio_sid="independent",
            disconnect=AsyncMock(),
        ))
        self.assertTrue(await registry.socket_is_tracked("independent-root"))
        self.assertFalse(await registry.socket_is_active("independent-root"))

    async def test_engineio_cleanup_dependency_failures_release_transport_reservation(self):
        for stage in ("namespaces", "lookup", "disconnect"):
            with self.subTest(stage=stage):
                sio, _ = engineio_guard_sio()
                sio.eio.handlers["disconnect"].side_effect = RuntimeError("disconnect callback failed")
                sio.manager = SimpleNamespace(
                    get_namespaces=Mock(return_value=("/",)),
                    sid_from_eio_sid=Mock(return_value="socket"),
                    disconnect=AsyncMock(),
                )
                method = {"namespaces": "get_namespaces", "lookup": "sid_from_eio_sid",
                          "disconnect": "disconnect"}[stage]
                getattr(sio.manager, method).side_effect = RuntimeError(f"{stage} failed")
                configure_copilot_bridge_guards(
                    sio, sessions=FakeSessions(SimpleNamespace(principal_id="tenant:user")),
                )
                await sio.eio.handlers["connect"]("engine", transport_environ())
                with self.assertLogs(embed_security.logger, level="ERROR") as logs:
                    self.assertIsNone(await sio.eio.handlers["disconnect"]("engine"))
                expected = {
                    "namespaces": "Failed to enumerate Socket.IO namespaces",
                    "lookup": "Failed to resolve a Socket.IO transport",
                    "disconnect": "Failed to force Socket.IO manager cleanup",
                }[stage]
                self.assertTrue(any(expected in line for line in logs.output))
                self.assertNotIn(f"{stage} failed", "\n".join(logs.output))
                self.assertTrue(all(record.exc_info is None for record in logs.records))
                self.assertIsNone(await embed_security._copilot_socket_registry.engineio_session("engine"))

    async def test_failed_session_disconnect_retains_denied_binding(self):
        registry = CopilotSocketRegistry(max_connections_per_session=1)
        await registry.bind_connection(
            session_id=SESSION_ID, socket_id="socket", chainlit_session_id="chainlit", disconnect=AsyncMock(),
        )
        with self.assertLogs(embed_security.logger, level="ERROR"):
            count = await registry.disconnect_session(
                session_id=SESSION_ID, additional_socket_ids=set(),
                disconnect=AsyncMock(side_effect=RuntimeError("disconnect failed")),
            )
        self.assertEqual(0, count)
        self.assertFalse(await registry.socket_is_active("socket"))
        self.assertEqual(("socket",), (await registry.session_resources(SESSION_ID))[0])

    async def test_failed_session_delete_still_unbinds_terminated_socket(self):
        registry = CopilotSocketRegistry(max_connections_per_session=1)
        await registry.bind_connection(
            session_id=SESSION_ID, socket_id="socket", chainlit_session_id="chainlit", disconnect=AsyncMock(),
        )
        session = SimpleNamespace(
            to_clear=False, current_task=None, delete=AsyncMock(side_effect=RuntimeError("delete failed")),
        )
        with (
            patch("chainlit.session.WebsocketSession.get", return_value=session),
            self.assertLogs(embed_security.logger, level="ERROR"),
        ):
            self.assertTrue(await _terminate_socket(SimpleNamespace(disconnect=AsyncMock()), registry, "socket"))
        self.assertEqual((), (await registry.session_resources(SESSION_ID))[0])
        self.assertTrue(session.to_clear)

    async def test_bridge_recipient_lookup_failure_is_conservative(self):
        sio = SimpleNamespace(manager=SimpleNamespace(
            get_participants=Mock(side_effect=RuntimeError("manager lookup failed")),
        ))
        with (
            patch.object(embed_security, "_has_copilot_sockets", return_value=True),
            patch.object(embed_security, "_is_copilot_socket", return_value=False),
            self.assertLogs(embed_security.logger, level="ERROR"),
        ):
            self.assertTrue(embed_security._target_has_copilot_sockets(sio, "room"))

    async def test_disconnect_callback_failure_closes_transport_then_propagates(self):
        sio = RealChainlitSio(AsyncMock())
        failure = RuntimeError("disconnect failed")
        sio.disconnect.side_effect = failure
        configure_copilot_bridge_guards(sio, sessions=FakeSessions())
        with patch.object(embed_security, "_close_engineio_transport", AsyncMock()) as close:
            with self.assertRaises(RuntimeError) as raised:
                await embed_security._copilot_disconnect_socket("socket")
        self.assertIs(failure, raised.exception)
        close.assert_awaited_once_with(sio, "engine-socket")

    async def test_disconnect_double_failure_preserves_primary_with_safe_diagnostic(self):
        sio = RealChainlitSio(AsyncMock())
        failure = RuntimeError("disconnect-private")
        sio.disconnect.side_effect = failure
        configure_copilot_bridge_guards(sio, sessions=FakeSessions())
        with (
            patch.object(embed_security, "_close_engineio_transport",
                         AsyncMock(side_effect=ValueError("close-private"))) as close,
            self.assertLogs(embed_security.logger, level="ERROR") as logs,
        ):
            with self.assertRaises(RuntimeError) as raised:
                await embed_security._copilot_disconnect_socket("socket")
        self.assertIs(failure, raised.exception)
        close.assert_awaited_once_with(sio, "engine-socket")
        self.assertNotIn("private", "".join(logs.output))
        self.assertIn("transport", "".join(logs.output))

    async def test_disconnect_cancellation_remains_distinct(self):
        for cancel_close in (False, True):
            with self.subTest(cancel_close=cancel_close):
                sio = RealChainlitSio(AsyncMock())
                cancellation = asyncio.CancelledError()
                sio.disconnect.side_effect = RuntimeError("disconnect") if cancel_close else cancellation
                configure_copilot_bridge_guards(sio, sessions=FakeSessions())
                with patch.object(embed_security, "_close_engineio_transport",
                                  AsyncMock(side_effect=cancellation)) as close:
                    with self.assertRaises(asyncio.CancelledError) as raised:
                        await embed_security._copilot_disconnect_socket("socket")
                self.assertIs(cancellation, raised.exception)
                self.assertEqual(int(cancel_close), close.await_count)

    async def test_rejected_socket_remains_denied_if_transport_close_fails(self):
        sio = RealChainlitSio(AsyncMock())
        configure_copilot_bridge_guards(sio, sessions=FakeSessions())
        with (
            patch.object(embed_security, "_close_engineio_transport", AsyncMock(side_effect=RuntimeError("close failed"))),
            self.assertLogs(embed_security.logger, level="ERROR"),
            self.assertRaises(SocketIOConnectionRefusedError),
        ):
            await sio.handlers["/"]["connect"]("socket", transport_environ(), {"clientType": "copilot"})

    async def test_invalidation_transport_failures_are_logged_without_counting_success(self):
        registry = CopilotSocketRegistry(max_connections_per_session=1)
        await registry.reserve_engineio_transport(SESSION_ID, "engine")
        sio = SimpleNamespace(disconnect=AsyncMock(side_effect=RuntimeError("disconnect failed")))
        session = SimpleNamespace(
            user=SimpleNamespace(metadata={"copilot_session_id": SESSION_ID}),
            socket_id="socket", to_clear=False, current_task=None,
        )
        with (
            patch.object(embed_security, "_copilot_sio", sio),
            patch.object(embed_security, "_copilot_disconnect_socket", None),
            patch.object(embed_security, "_close_engineio_transport", AsyncMock(side_effect=RuntimeError("close failed"))),
            patch("chainlit.session.ws_sessions_sid", {"socket": session}),
        ):
            for active_registry in (None, registry):
                with (
                    self.subTest(registry=active_registry is not None),
                    patch.object(embed_security, "_copilot_socket_registry", active_registry),
                    self.assertLogs(embed_security.logger, level="ERROR"),
                ):
                    self.assertEqual(0, await disconnect_copilot_session(SESSION_ID))
        self.assertTrue(session.to_clear)

    async def test_portal_engineio_admits_both_modes_and_transports(self):
        for auth_mode in ("anonymous", "entra"):
            for scope_type in ("http", "websocket"):
                with self.subTest(
                    auth_mode=auth_mode,
                    scope_type=scope_type,
                ):
                    active_session = SimpleNamespace(
                        principal_id="tenant:user",
                        auth_mode=auth_mode,
                    )
                    sio, original_connect = engineio_guard_sio()
                    configure_copilot_bridge_guards(
                        sio,
                        sessions=FakeSessions(active_session),
                    )
                    engineio_sid = f"portal-{auth_mode}-{scope_type}"

                    self.assertIsNone(
                        await sio.eio.handlers["connect"](
                            engineio_sid,
                            transport_environ(scope_type=scope_type),
                        )
                    )

                    original_connect.assert_awaited_once()
                    self.assertEqual(
                        SESSION_ID,
                        await embed_security._copilot_socket_registry.engineio_session(
                            engineio_sid
                        ),
                    )

    async def test_standalone_engineio_ignores_stale_copilot_cookie(self):
        sio, original_connect = engineio_guard_sio()
        configure_copilot_bridge_guards(
            sio,
            sessions=FakeSessions(),
        )

        self.assertIsNone(
            await sio.eio.handlers["connect"](
                "standalone-stale",
                transport_environ(origins=(CHAT,)),
            )
        )

        original_connect.assert_awaited_once()
        self.assertIsNone(
            await embed_security._copilot_socket_registry.engineio_session(
                "standalone-stale"
            )
        )

    async def test_standalone_engineio_active_cookie_is_not_invalidated(self):
        for auth_mode in ("anonymous", "entra"):
            for scope_type in ("http", "websocket"):
                with self.subTest(
                    auth_mode=auth_mode,
                    scope_type=scope_type,
                ):
                    active_session = SimpleNamespace(
                        principal_id="tenant:user",
                        auth_mode=auth_mode,
                    )
                    sio, original_connect = engineio_guard_sio()
                    configure_copilot_bridge_guards(
                        sio,
                        sessions=FakeSessions(active_session),
                    )

                    self.assertIsNone(
                        await sio.eio.handlers["connect"](
                            f"standalone-{auth_mode}-{scope_type}",
                            transport_environ(
                                origins=(CHAT,),
                                scope_type=scope_type,
                            ),
                        )
                    )
                    original_connect.assert_awaited_once()
                    self.assertIsNone(
                        await embed_security._copilot_socket_registry.engineio_session(
                            f"standalone-{auth_mode}-{scope_type}"
                        )
                    )

                    with patch("chainlit.session.ws_sessions_sid", {}):
                        self.assertEqual(
                            0,
                            await disconnect_copilot_session(SESSION_ID),
                        )
                    self.assertFalse(sio.eio.closed)

    async def test_only_exact_portal_origin_can_select_copilot_engineio(self):
        active_session = SimpleNamespace(principal_id="tenant:user")
        origin_cases = {
            "same-origin standalone": (CHAT,),
            "unlisted": ("https://attacker.example.com",),
            "null": ("null",),
            "malformed": ("https://portal.example.com/path",),
            "missing": (),
            "duplicate": (PORTAL, PORTAL),
        }

        for name, origins in origin_cases.items():
            with self.subTest(name=name):
                sio, original_connect = engineio_guard_sio()
                configure_copilot_bridge_guards(
                    sio,
                    sessions=FakeSessions(active_session),
                )

                self.assertIsNone(
                    await sio.eio.handlers["connect"](
                        f"non-copilot-{name}",
                        transport_environ(origins=origins),
                    )
                )
                original_connect.assert_awaited_once()
                self.assertIsNone(
                    await embed_security._copilot_socket_registry.engineio_session(
                        f"non-copilot-{name}"
                    )
                )

    async def test_exact_portal_origin_rejects_stale_copilot_cookie(self):
        sio, original_connect = engineio_guard_sio()
        configure_copilot_bridge_guards(
            sio,
            sessions=FakeSessions(),
        )

        self.assertFalse(
            await sio.eio.handlers["connect"](
                "portal-stale",
                transport_environ(),
            )
        )

        original_connect.assert_not_awaited()
        self.assertIsNone(
            await embed_security._copilot_socket_registry.engineio_session(
                "portal-stale"
            )
        )

    async def test_standalone_socketio_bypasses_copilot_admission(self):
        original_connect = AsyncMock(return_value=True)
        original_disconnect = AsyncMock(return_value=None)

        class FakeSio:
            def __init__(self):
                self.emit = AsyncMock()
                self.call = AsyncMock()
                self.manager = SimpleNamespace(
                    eio_sid_from_sid=lambda socket_id, namespace: (
                        f"engine-{socket_id}"
                    )
                )
                self.eio = SimpleNamespace(disconnect=AsyncMock())
                self.handlers = {
                    "/": {
                        "connect": original_connect,
                        "disconnect": original_disconnect,
                    }
                }

            def on(self, event, handler):
                self.handlers["/"][event] = handler

        for active in (False, True):
            with self.subTest(active_cookie=active):
                original_disconnect.reset_mock()
                session = (
                    SimpleNamespace(principal_id="tenant:user")
                    if active
                    else None
                )
                sio = FakeSio()
                configure_copilot_bridge_guards(
                    sio,
                    sessions=FakeSessions(session),
                )
                authenticate = AsyncMock(
                    side_effect=AssertionError(
                        "standalone must not use Copilot admission"
                    )
                )

                with patch(
                    "gpt_rag_ui.auth.embed_security._authenticated_socket_user",
                    authenticate,
                ):
                    self.assertTrue(
                        await sio.handlers["/"]["connect"](
                            f"standalone-{active}",
                            transport_environ(origins=(CHAT,)),
                            {"clientType": "webapp"},
                        )
                    )

                authenticate.assert_not_awaited()
                self.assertFalse(
                    await embed_security._copilot_socket_registry.socket_is_tracked(
                        f"standalone-{active}"
                    )
                )
                self.assertIsNone(
                    await sio.handlers["/"]["disconnect"](
                        f"standalone-{active}",
                        "client disconnect",
                    )
                )
                original_disconnect.assert_awaited_once_with(
                    f"standalone-{active}"
                )
                sio.eio.disconnect.assert_not_awaited()

    async def test_invalid_origin_cannot_request_copilot_socketio(self):
        original_connect = AsyncMock(return_value=True)

        class FakeSio:
            def __init__(self):
                self.emit = AsyncMock()
                self.call = AsyncMock()
                self.handlers = {"/": {"connect": original_connect}}

            def on(self, event, handler):
                self.handlers["/"][event] = handler

        user = SimpleNamespace(
            identifier="tenant:user",
            metadata={
                "auth_source": "copilot_session",
                "copilot_session_id": SESSION_ID,
            },
        )
        active_session = SimpleNamespace(principal_id=user.identifier)
        origin_cases = {
            "unlisted": ("https://attacker.example.com",),
            "null": ("null",),
            "malformed": ("https://portal.example.com/path",),
            "missing": (),
        }

        for name, origins in origin_cases.items():
            with self.subTest(name=name):
                sio = FakeSio()
                configure_copilot_bridge_guards(
                    sio,
                    sessions=FakeSessions(active_session),
                )
                authenticate = AsyncMock(return_value=user)

                with (
                    patch(
                        "gpt_rag_ui.auth.embed_security._authenticated_socket_user",
                        authenticate,
                    ),
                    patch(
                        "chainlit.session.WebsocketSession.get",
                        return_value=SimpleNamespace(
                            id="chainlit-session",
                            user=user,
                        ),
                    ),
                    self.assertRaises(SocketIOConnectionRefusedError),
                ):
                    await sio.handlers["/"]["connect"](
                        f"invalid-origin-{name}",
                        transport_environ(origins=origins),
                        {"clientType": "copilot"},
                    )

                original_connect.assert_not_awaited()
                self.assertFalse(
                    await embed_security._copilot_socket_registry.socket_is_tracked(
                        f"invalid-origin-{name}"
                    )
                )

    async def test_engineio_handshake_cap_closes_unconnected_transports(self):
        original_engineio_connect = AsyncMock(return_value=None)
        original_engineio_disconnect = AsyncMock(return_value=None)

        class FakeEngineIo:
            def __init__(self):
                self.handlers = {
                    "connect": original_engineio_connect,
                    "disconnect": original_engineio_disconnect,
                }
                self.closed = []

            def on(self, event, handler):
                self.handlers[event] = handler

            async def disconnect(self, engineio_sid):
                self.closed.append(engineio_sid)
                await self.handlers["disconnect"](
                    engineio_sid,
                    "server disconnect",
                )

        class FakeSio:
            def __init__(self):
                self.emit = AsyncMock()
                self.call = AsyncMock()
                self.eio = FakeEngineIo()
                self.handlers = {"/": {}}

        active_session = SimpleNamespace(principal_id="tenant:user")
        sio = FakeSio()
        configure_copilot_bridge_guards(
            sio,
            sessions=FakeSessions(active_session),
        )
        cookie_environ = transport_environ()

        for index in range(4):
            self.assertIsNone(
                await sio.eio.handlers["connect"](
                    f"engine-{index}",
                    cookie_environ,
                )
            )
        self.assertFalse(
            await sio.eio.handlers["connect"](
                "engine-overflow",
                cookie_environ,
            )
        )
        self.assertEqual(4, original_engineio_connect.await_count)

        with patch("chainlit.session.ws_sessions_sid", {}):
            self.assertEqual(
                0,
                await disconnect_copilot_session(SESSION_ID),
            )

        self.assertEqual(
            {f"engine-{index}" for index in range(4)},
            set(sio.eio.closed),
        )
        self.assertFalse(
            await embed_security._copilot_socket_registry.session_engineio_sids(
                SESSION_ID
            )
        )

    async def test_failed_engineio_disconnect_releases_handshake_reservation(
        self,
    ):
        original_engineio_connect = AsyncMock(return_value=None)
        original_engineio_disconnect = AsyncMock(
            side_effect=RuntimeError("disconnect failed")
        )

        class FakeEngineIo:
            def __init__(self):
                self.handlers = {
                    "connect": original_engineio_connect,
                    "disconnect": original_engineio_disconnect,
                }

            def on(self, event, handler):
                self.handlers[event] = handler

        class FakeSio:
            def __init__(self):
                self.emit = AsyncMock()
                self.call = AsyncMock()
                self.eio = FakeEngineIo()
                self.handlers = {"/": {}}
                self.manager = SimpleNamespace(get_namespaces=lambda: ())

        active_session = SimpleNamespace(principal_id="tenant:user")
        sio = FakeSio()
        configure_copilot_bridge_guards(
            sio,
            sessions=FakeSessions(active_session),
        )
        cookie_environ = transport_environ()

        self.assertIsNone(
            await sio.eio.handlers["connect"](
                "failed-disconnect",
                cookie_environ,
            )
        )
        self.assertIsNone(
            await sio.eio.handlers["disconnect"](
                "failed-disconnect",
                "transport close",
            )
        )
        self.assertFalse(
            await embed_security._copilot_socket_registry.session_engineio_sids(
                SESSION_ID
            )
        )
        self.assertIsNone(
            await sio.eio.handlers["connect"](
                "replacement",
                cookie_environ,
            )
        )

    async def test_physical_socket_cap_disconnects_all_hundred_restores(self):
        registry = CopilotSocketRegistry(max_connections_per_session=4)
        active_socket_ids = set()
        disconnected_socket_ids = []

        async def disconnect(socket_id):
            disconnected_socket_ids.append(socket_id)
            active_socket_ids.discard(socket_id)

        admitted_socket_ids = []
        max_active_sockets = 0
        for index in range(4):
            socket_id = f"socket-{index}"
            self.assertTrue(
                await registry.bind_connection(
                    session_id=SESSION_ID,
                    socket_id=socket_id,
                    chainlit_session_id=f"chainlit-{index}",
                    disconnect=disconnect,
                )
            )
            admitted_socket_ids.append(socket_id)
            active_socket_ids.add(socket_id)
            max_active_sockets = max(
                max_active_sockets,
                len(active_socket_ids),
            )

        self.assertFalse(
            await registry.bind_connection(
                session_id=SESSION_ID,
                socket_id="socket-overflow",
                chainlit_session_id="chainlit-overflow",
                disconnect=disconnect,
            )
        )

        for index in range(4, 100):
            socket_id = f"socket-{index}"
            self.assertTrue(
                await registry.bind_connection(
                    session_id=SESSION_ID,
                    socket_id=socket_id,
                    chainlit_session_id="chainlit-0",
                    disconnect=disconnect,
                )
            )
            admitted_socket_ids.append(socket_id)
            active_socket_ids.add(socket_id)
            max_active_sockets = max(
                max_active_sockets,
                len(active_socket_ids),
            )

        self.assertEqual(4, max_active_sockets)
        self.assertEqual(4, len(active_socket_ids))
        self.assertEqual(
            set(admitted_socket_ids) - active_socket_ids,
            set(disconnected_socket_ids),
        )

        disconnected = await registry.disconnect_session(
            session_id=SESSION_ID,
            additional_socket_ids=set(),
            disconnect=disconnect,
        )

        self.assertEqual(4, disconnected)
        self.assertFalse(active_socket_ids)
        self.assertEqual(
            set(admitted_socket_ids),
            set(disconnected_socket_ids),
        )
        self.assertEqual(100, len(disconnected_socket_ids))

    async def test_rebinding_same_physical_socket_is_idempotent(self):
        registry = CopilotSocketRegistry(max_connections_per_session=1)
        disconnect = AsyncMock()

        for _ in range(2):
            self.assertTrue(
                await registry.bind_connection(
                    session_id=SESSION_ID,
                    socket_id="socket",
                    chainlit_session_id="chainlit",
                    disconnect=disconnect,
                )
            )

        disconnect.assert_not_awaited()

    async def test_concurrent_restores_leave_one_socket_for_logical_session(
        self,
    ):
        registry = CopilotSocketRegistry(max_connections_per_session=4)
        disconnected_socket_ids = []

        async def disconnect(socket_id):
            await asyncio.sleep(0)
            disconnected_socket_ids.append(socket_id)

        self.assertTrue(
            await registry.bind_connection(
                session_id=SESSION_ID,
                socket_id="initial",
                chainlit_session_id="chainlit",
                disconnect=disconnect,
            )
        )

        results = await asyncio.gather(
            *(
                registry.bind_connection(
                    session_id=SESSION_ID,
                    socket_id=f"replacement-{index}",
                    chainlit_session_id="chainlit",
                    disconnect=disconnect,
                )
                for index in range(99)
            )
        )

        socket_ids, chainlit_session_ids = await registry.session_resources(
            SESSION_ID
        )
        accepted = sum(results)
        self.assertGreaterEqual(accepted, 1)
        self.assertEqual(1, len(socket_ids))
        self.assertEqual(("chainlit",), chainlit_session_ids)
        self.assertEqual(accepted, len(disconnected_socket_ids))
        self.assertEqual(
            accepted,
            len(set(disconnected_socket_ids)),
        )

    async def test_blocked_replacement_does_not_queue_other_session(self):
        registry = CopilotSocketRegistry(max_connections_per_session=1)
        disconnect_started = asyncio.Event()
        release_disconnect = asyncio.Event()
        other_session_id = "o" * 43

        async def blocked_disconnect(socket_id):
            disconnect_started.set()
            await release_disconnect.wait()

        self.assertTrue(
            await registry.bind_connection(
                session_id=SESSION_ID,
                socket_id="old",
                chainlit_session_id="chainlit",
                disconnect=AsyncMock(),
            )
        )
        replacement = asyncio.create_task(
            registry.bind_connection(
                session_id=SESSION_ID,
                socket_id="replacement",
                chainlit_session_id="chainlit",
                disconnect=blocked_disconnect,
            )
        )
        await asyncio.wait_for(disconnect_started.wait(), timeout=1)

        self.assertTrue(
            await asyncio.wait_for(
                registry.bind_connection(
                    session_id=other_session_id,
                    socket_id="other",
                    chainlit_session_id="other-chainlit",
                    disconnect=AsyncMock(),
                ),
                timeout=1,
            )
        )
        self.assertFalse(
            await asyncio.wait_for(
                registry.bind_connection(
                    session_id=SESSION_ID,
                    socket_id="queued",
                    chainlit_session_id="chainlit",
                    disconnect=AsyncMock(),
                ),
                timeout=1,
            )
        )

        release_disconnect.set()
        self.assertTrue(await replacement)

    async def test_failed_replacement_disconnect_keeps_old_association(self):
        registry = CopilotSocketRegistry(max_connections_per_session=1)
        self.assertTrue(
            await registry.bind_connection(
                session_id=SESSION_ID,
                socket_id="old-socket",
                chainlit_session_id="chainlit",
                disconnect=AsyncMock(),
            )
        )

        self.assertFalse(
            await registry.bind_connection(
                session_id=SESSION_ID,
                socket_id="new-socket",
                chainlit_session_id="chainlit",
                disconnect=AsyncMock(
                    side_effect=RuntimeError("disconnect failed")
                ),
            )
        )

        socket_ids, chainlit_session_ids = await registry.session_resources(
            SESSION_ID
        )
        self.assertEqual(("old-socket",), socket_ids)
        self.assertEqual(("chainlit",), chainlit_session_ids)
        self.assertFalse(await registry.socket_is_active("old-socket"))
        self.assertFalse(await registry.socket_is_active("new-socket"))

    async def test_failed_termination_retains_invalidated_association(self):
        registry = CopilotSocketRegistry(max_connections_per_session=1)
        self.assertTrue(
            await registry.bind_connection(
                session_id=SESSION_ID,
                socket_id="socket",
                chainlit_session_id="chainlit",
                disconnect=AsyncMock(),
            )
        )
        active_task = asyncio.create_task(asyncio.sleep(60))
        socket_session = SimpleNamespace(
            to_clear=False,
            current_task=active_task,
        )
        sio = SimpleNamespace(
            disconnect=AsyncMock(
                side_effect=RuntimeError("disconnect failed")
            )
        )

        with patch(
            "chainlit.session.WebsocketSession.get",
            return_value=socket_session,
        ):
            await _terminate_socket(sio, registry, "socket")
            await asyncio.sleep(0)

        socket_ids, _ = await registry.session_resources(SESSION_ID)
        self.assertEqual(("socket",), socket_ids)
        self.assertFalse(await registry.socket_is_active("socket"))
        self.assertTrue(socket_session.to_clear)
        self.assertTrue(active_task.cancelled())

    async def test_successful_termination_deletes_orphan_chainlit_session(self):
        registry = CopilotSocketRegistry(max_connections_per_session=1)
        self.assertTrue(
            await registry.bind_connection(
                session_id=SESSION_ID,
                socket_id="socket",
                chainlit_session_id="chainlit",
                disconnect=AsyncMock(),
            )
        )
        socket_session = SimpleNamespace(
            to_clear=False,
            current_task=None,
            delete=AsyncMock(),
        )
        sio = SimpleNamespace(disconnect=AsyncMock())

        with patch(
            "chainlit.session.WebsocketSession.get",
            return_value=socket_session,
        ):
            self.assertTrue(
                await _terminate_socket(sio, registry, "socket")
            )

        self.assertTrue(socket_session.to_clear)
        socket_session.delete.assert_awaited_once()
        socket_ids, _ = await registry.session_resources(SESSION_ID)
        self.assertFalse(socket_ids)

    async def test_copilot_token_is_authenticated_without_global_oauth(self):
        user = SimpleNamespace(identifier="anonymous-session")
        authenticate = AsyncMock(return_value=user)
        environ = {}
        with (
            patch("chainlit.socket._get_token", return_value="internal-token"),
            patch("chainlit.auth.authenticate_user", authenticate),
        ):
            result = await _authenticated_socket_user(environ)

        self.assertIs(user, result)
        authenticate.assert_awaited_once_with("internal-token")
        self.assertEqual(
            "internal-token",
            environ["_gpt_rag_authenticated_token"],
        )

    async def test_copilot_bridge_events_are_denied(self):
        original_window_handler = AsyncMock()

        class FakeSio:
            def __init__(self):
                self.emit = AsyncMock()
                self.call = AsyncMock()
                self.handlers = {"/": {"window_message": original_window_handler}}

            def on(self, event, handler):
                self.handlers["/"][event] = handler

        sio = FakeSio()
        configure_copilot_bridge_guards(sio, sessions=FakeSessions())

        with patch("gpt_rag_ui.auth.embed_security._is_copilot_socket", return_value=True):
            self.assertIsNone(
                await sio.emit("window_message", {"secret": True}, to="socket")
            )
            with self.assertRaises(PermissionError):
                await sio.call("call_fn", {}, to="socket")
            self.assertIsNone(
                await sio.handlers["/"]["window_message"](
                    "socket",
                    {"secret": True},
                )
            )

        original_window_handler.assert_not_awaited()

    async def test_positional_bridge_target_is_denied(self):
        class FakeSio:
            def __init__(self):
                self.emit = AsyncMock()
                self.call = AsyncMock()
                self.handlers = {"/": {}}

            def on(self, event, handler):
                self.handlers["/"][event] = handler

        sio = FakeSio()
        configure_copilot_bridge_guards(sio, sessions=FakeSessions())

        with patch("gpt_rag_ui.auth.embed_security._is_copilot_socket", return_value=True):
            self.assertIsNone(
                await sio.emit("window_message", {"secret": True}, "socket")
            )
            with self.assertRaises(PermissionError):
                await sio.call("call_fn", {}, "socket")

    async def test_window_message_broadcast_is_denied_with_copilot_connected(self):
        class FakeSio:
            def __init__(self):
                self.emit = AsyncMock()
                self.call = AsyncMock()
                self.handlers = {"/": {}}

            def on(self, event, handler):
                self.handlers["/"][event] = handler

        sio = FakeSio()
        configure_copilot_bridge_guards(sio, sessions=FakeSessions())

        with patch("gpt_rag_ui.auth.embed_security._has_copilot_sockets", return_value=True):
            self.assertIsNone(
                await sio.emit("window_message", {"secret": True})
            )

    async def test_bridge_event_to_room_with_copilot_member_is_denied(self):
        class FakeSio:
            def __init__(self):
                self.emit = AsyncMock()
                self.call = AsyncMock()
                self.handlers = {"/": {}}
                self.manager = SimpleNamespace(
                    get_participants=lambda namespace, room: [
                        ("copilot-socket", "engine-socket")
                    ]
                )

            def on(self, event, handler):
                self.handlers["/"][event] = handler

        sio = FakeSio()
        original_emit = sio.emit
        configure_copilot_bridge_guards(sio, sessions=FakeSessions())

        with patch(
            "gpt_rag_ui.auth.embed_security._is_copilot_socket",
            side_effect=lambda socket_id: socket_id == "copilot-socket",
        ):
            self.assertIsNone(
                await sio.emit(
                    "window_message",
                    {"secret": True},
                    room="mixed-room",
                )
            )

        original_emit.assert_not_awaited()

    async def test_session_invalidation_cancels_active_work_and_disconnects(self):
        session_id = "i" * 43
        active_task = asyncio.create_task(asyncio.sleep(60))
        socket_session = SimpleNamespace(
            user=SimpleNamespace(
                metadata={"copilot_session_id": session_id}
            ),
            to_clear=False,
            current_task=active_task,
            socket_id="copilot-socket",
        )
        sio = SimpleNamespace(disconnect=AsyncMock())

        with (
            patch("gpt_rag_ui.auth.embed_security._copilot_disconnect_socket", None),
            patch("gpt_rag_ui.auth.embed_security._copilot_sio", sio),
            patch("gpt_rag_ui.auth.embed_security._copilot_socket_registry", None),
            patch(
                "chainlit.session.ws_sessions_sid",
                {"copilot-socket": socket_session},
            ),
        ):
            disconnected = await disconnect_copilot_session(session_id)
            await asyncio.sleep(0)

        self.assertEqual(1, disconnected)
        self.assertTrue(socket_session.to_clear)
        self.assertTrue(active_task.cancelled())
        sio.disconnect.assert_awaited_once_with("copilot-socket")

    async def test_invalidation_reaches_every_tracked_socket_and_task(self):
        registry = CopilotSocketRegistry(max_connections_per_session=4)
        socket_ids = [f"copilot-socket-{index}" for index in range(4)]
        for index, socket_id in enumerate(socket_ids):
            self.assertTrue(
                await registry.bind_connection(
                    session_id=SESSION_ID,
                    socket_id=socket_id,
                    chainlit_session_id=f"chainlit-{index}",
                    disconnect=AsyncMock(),
                )
            )

        tasks = [
            asyncio.create_task(asyncio.sleep(60)) for _ in socket_ids
        ]
        tracked_tasks = [
            asyncio.create_task(asyncio.sleep(60)) for _ in socket_ids
        ]
        for socket_id, task in zip(
            socket_ids,
            tracked_tasks,
            strict=True,
        ):
            self.assertEqual(
                SESSION_ID,
                await registry.track_task(socket_id, task),
            )
        socket_sessions = {
            socket_id: SimpleNamespace(
                user=SimpleNamespace(
                    metadata={"copilot_session_id": SESSION_ID}
                ),
                to_clear=False,
                current_task=task,
                socket_id=socket_id,
            )
            for socket_id, task in zip(socket_ids, tasks, strict=True)
        }
        sio = SimpleNamespace(disconnect=AsyncMock())

        with (
            patch("gpt_rag_ui.auth.embed_security._copilot_disconnect_socket", None),
            patch("gpt_rag_ui.auth.embed_security._copilot_sio", sio),
            patch("gpt_rag_ui.auth.embed_security._copilot_socket_registry", registry),
            patch(
                "chainlit.session.ws_sessions_sid",
                socket_sessions,
            ),
        ):
            disconnected = await disconnect_copilot_session(SESSION_ID)
            await asyncio.sleep(0)

        self.assertEqual(4, disconnected)
        self.assertTrue(all(session.to_clear for session in socket_sessions.values()))
        self.assertTrue(
            all(task.cancelled() for task in tasks + tracked_tasks)
        )
        self.assertEqual(
            set(socket_ids),
            {call.args[0] for call in sio.disconnect.await_args_list},
        )
        tracked_socket_ids, _ = await registry.session_resources(SESSION_ID)
        self.assertFalse(tracked_socket_ids)

    async def test_active_handler_task_is_cancelled_on_invalidation(self):
        started = asyncio.Event()

        async def client_message(socket_id, *args, **kwargs):
            started.set()
            await asyncio.sleep(60)

        class FakeSio:
            def __init__(self):
                self.emit = AsyncMock()
                self.call = AsyncMock()
                self.disconnect = AsyncMock()
                self.handlers = {"/": {"client_message": client_message}}

            def on(self, event, handler):
                self.handlers["/"][event] = handler

        sio = FakeSio()
        configure_copilot_bridge_guards(sio, sessions=FakeSessions())
        registry = embed_security._copilot_socket_registry
        self.assertIsNotNone(registry)
        self.assertTrue(
            await registry.bind_connection(
                session_id=SESSION_ID,
                socket_id="socket",
                chainlit_session_id="chainlit",
                disconnect=AsyncMock(),
            )
        )

        handler_task = asyncio.create_task(
            sio.handlers["/"]["client_message"]("socket", "message")
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        with patch("chainlit.session.ws_sessions_sid", {}):
            self.assertEqual(
                1,
                await disconnect_copilot_session(SESSION_ID),
            )
        with self.assertRaises(asyncio.CancelledError):
            await handler_task

    async def test_spawned_handler_tasks_are_cancelled_on_invalidation(self):
        spawned_tasks = []

        async def client_message(socket_id, *args, **kwargs):
            spawned_tasks.extend(
                asyncio.create_task(asyncio.sleep(60))
                for _ in range(2)
            )

        class FakeSio:
            def __init__(self):
                self.emit = AsyncMock()
                self.call = AsyncMock()
                self.disconnect = AsyncMock()
                self.handlers = {"/": {"client_message": client_message}}

            def on(self, event, handler):
                self.handlers["/"][event] = handler

        sio = FakeSio()
        configure_copilot_bridge_guards(sio, sessions=FakeSessions())
        registry = embed_security._copilot_socket_registry
        self.assertIsNotNone(registry)
        self.assertTrue(
            await registry.bind_connection(
                session_id=SESSION_ID,
                socket_id="socket",
                chainlit_session_id="chainlit",
                disconnect=AsyncMock(),
            )
        )

        await sio.handlers["/"]["client_message"]("socket", "message")
        with patch("chainlit.session.ws_sessions_sid", {}):
            self.assertEqual(
                1,
                await disconnect_copilot_session(SESSION_ID),
            )
        await asyncio.sleep(0)

        self.assertEqual(2, len(spawned_tasks))
        self.assertTrue(all(task.cancelled() for task in spawned_tasks))

    async def test_disconnect_untracks_only_after_handler_succeeds(self):
        tracked_during_disconnect = []

        async def disconnect_handler(socket_id):
            tracked_during_disconnect.append(
                await registry.socket_is_tracked(socket_id)
            )

        class FakeSio:
            def __init__(self):
                self.emit = AsyncMock()
                self.call = AsyncMock()
                self.manager = SimpleNamespace(
                    eio_sid_from_sid=lambda socket_id, namespace: (
                        f"engine-{socket_id}"
                    )
                )
                self.eio = SimpleNamespace(disconnect=AsyncMock())
                self.handlers = {"/": {"disconnect": disconnect_handler}}

            def on(self, event, handler):
                self.handlers["/"][event] = handler

        sio = FakeSio()
        configure_copilot_bridge_guards(sio, sessions=FakeSessions())
        registry = embed_security._copilot_socket_registry
        self.assertIsNotNone(registry)
        self.assertTrue(
            await registry.bind_connection(
                session_id=SESSION_ID,
                socket_id="socket",
                chainlit_session_id="chainlit",
                disconnect=AsyncMock(),
            )
        )

        await sio.handlers["/"]["disconnect"](
            "socket",
            "client disconnect",
        )

        self.assertEqual([True], tracked_during_disconnect)
        self.assertFalse(await registry.socket_is_tracked("socket"))
        sio.eio.disconnect.assert_awaited_once_with("engine-socket")

    async def test_failed_disconnect_handler_retains_association(self):
        async def disconnect_handler(socket_id):
            raise RuntimeError("cleanup failed")

        class FakeSio:
            def __init__(self):
                self.emit = AsyncMock()
                self.call = AsyncMock()
                self.handlers = {"/": {"disconnect": disconnect_handler}}

            def on(self, event, handler):
                self.handlers["/"][event] = handler

        sio = FakeSio()
        configure_copilot_bridge_guards(sio, sessions=FakeSessions())
        registry = embed_security._copilot_socket_registry
        self.assertIsNotNone(registry)
        self.assertTrue(
            await registry.bind_connection(
                session_id=SESSION_ID,
                socket_id="socket",
                chainlit_session_id="chainlit",
                disconnect=AsyncMock(),
            )
        )

        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            await sio.handlers["/"]["disconnect"](
                "socket",
                "client disconnect",
            )

        self.assertTrue(await registry.socket_is_tracked("socket"))

    async def test_pending_disconnect_is_retried_before_replacement(self):
        disconnect_attempts = 0

        async def disconnect_handler(socket_id):
            nonlocal disconnect_attempts
            disconnect_attempts += 1
            if disconnect_attempts == 1:
                raise RuntimeError("cleanup failed")

        class FakeSio:
            def __init__(self):
                self.emit = AsyncMock()
                self.call = AsyncMock()
                self.pending = False
                self.manager = SimpleNamespace(
                    disconnect=AsyncMock(),
                    eio_sid_from_sid=lambda socket_id, namespace: (
                        f"engine-{socket_id}"
                    ),
                )
                self.eio = SimpleNamespace(disconnect=AsyncMock())
                self.handlers = {"/": {"disconnect": disconnect_handler}}

            def on(self, event, handler):
                self.handlers["/"][event] = handler

            async def disconnect(self, socket_id):
                if self.pending:
                    return
                self.pending = True
                await self.handlers["/"]["disconnect"](
                    socket_id,
                    "server disconnect",
                )

        sio = FakeSio()
        configure_copilot_bridge_guards(sio, sessions=FakeSessions())
        registry = embed_security._copilot_socket_registry
        disconnect_socket = embed_security._copilot_disconnect_socket
        self.assertIsNotNone(registry)
        self.assertIsNotNone(disconnect_socket)
        self.assertTrue(
            await registry.bind_connection(
                session_id=SESSION_ID,
                socket_id="old",
                chainlit_session_id="chainlit",
                disconnect=AsyncMock(),
            )
        )

        self.assertFalse(
            await registry.bind_connection(
                session_id=SESSION_ID,
                socket_id="first-replacement",
                chainlit_session_id="chainlit",
                disconnect=disconnect_socket,
            )
        )
        self.assertTrue(await registry.socket_is_tracked("old"))

        self.assertTrue(
            await registry.bind_connection(
                session_id=SESSION_ID,
                socket_id="second-replacement",
                chainlit_session_id="chainlit",
                disconnect=disconnect_socket,
            )
        )

        self.assertEqual(2, disconnect_attempts)
        sio.manager.disconnect.assert_awaited_once_with(
            "old",
            namespace="/",
            ignore_queue=True,
        )
        self.assertGreaterEqual(sio.eio.disconnect.await_count, 1)
        self.assertFalse(await registry.socket_is_tracked("old"))
        self.assertTrue(
            await registry.socket_is_tracked("second-replacement")
        )

    async def test_replacement_awaits_in_progress_client_disconnect(self):
        handler_calls = 0
        handler_started = asyncio.Event()
        release_handler = asyncio.Event()
        timeout_tasks = []

        async def disconnect_handler(socket_id):
            nonlocal handler_calls
            handler_calls += 1

            async def clear_on_timeout():
                await asyncio.sleep(60)

            timeout_tasks.append(asyncio.ensure_future(clear_on_timeout()))
            handler_started.set()
            await release_handler.wait()

        class FakeSio:
            def __init__(self):
                self.emit = AsyncMock()
                self.call = AsyncMock()
                self.pending = False
                self.manager = SimpleNamespace(
                    disconnect=AsyncMock(),
                    eio_sid_from_sid=lambda socket_id, namespace: (
                        f"engine-{socket_id}"
                    ),
                )
                self.eio = SimpleNamespace(disconnect=AsyncMock())
                self.handlers = {"/": {"disconnect": disconnect_handler}}

            def on(self, event, handler):
                self.handlers["/"][event] = handler

            async def disconnect(self, socket_id):
                if self.pending:
                    return
                self.pending = True
                await self.handlers["/"]["disconnect"](
                    socket_id,
                    "server disconnect",
                )

        sio = FakeSio()
        configure_copilot_bridge_guards(sio, sessions=FakeSessions())
        registry = embed_security._copilot_socket_registry
        disconnect_socket = embed_security._copilot_disconnect_socket
        self.assertIsNotNone(registry)
        self.assertIsNotNone(disconnect_socket)
        self.assertTrue(
            await registry.bind_connection(
                session_id=SESSION_ID,
                socket_id="old",
                chainlit_session_id="chainlit",
                disconnect=AsyncMock(),
            )
        )

        sio.pending = True
        client_disconnect = asyncio.create_task(
            sio.handlers["/"]["disconnect"](
                "old",
                "client disconnect",
            )
        )
        await asyncio.wait_for(handler_started.wait(), timeout=1)
        replacement = asyncio.create_task(
            registry.bind_connection(
                session_id=SESSION_ID,
                socket_id="replacement",
                chainlit_session_id="chainlit",
                disconnect=disconnect_socket,
            )
        )
        await asyncio.sleep(0)
        self.assertEqual(1, handler_calls)

        release_handler.set()
        await client_disconnect
        self.assertTrue(await replacement)
        self.assertEqual(1, handler_calls)
        self.assertEqual(
            1,
            await registry.cancel_restore_cleanup_tasks(
                SESSION_ID,
                "chainlit",
            ),
        )
        await asyncio.sleep(0)
        self.assertTrue(timeout_tasks[0].cancelled())

    def test_copilot_client_type_is_guarded_without_metadata_marker(self):
        session = SimpleNamespace(client_type="copilot", user=None)
        with patch(
            "chainlit.session.WebsocketSession.get",
            return_value=session,
        ):
            self.assertTrue(_is_copilot_socket("socket"))

    async def test_cross_principal_socket_restore_is_denied(self):
        original_connect = AsyncMock(return_value=True)

        class FakeSio:
            def __init__(self):
                self.emit = AsyncMock()
                self.call = AsyncMock()
                self.manager = SimpleNamespace(
                    eio_sid_from_sid=lambda socket_id, namespace: (
                        f"engine-{socket_id}"
                    )
                )
                self.eio = SimpleNamespace(disconnect=AsyncMock())
                self.handlers = {"/": {"connect": original_connect}}

            def on(self, event, handler):
                self.handlers["/"][event] = handler

        existing_user = SimpleNamespace(
            identifier="tenant:old-user",
            metadata={
                "auth_source": "copilot_session",
                "copilot_session_id": "old-copilot-session",
            },
        )
        existing_session = SimpleNamespace(user=existing_user)
        current_user = SimpleNamespace(
            identifier="tenant:new-user",
            metadata={
                "auth_source": "copilot_session",
                "copilot_session_id": SESSION_ID,
            },
        )
        sio = FakeSio()
        configure_copilot_bridge_guards(sio, sessions=FakeSessions())

        with (
            patch(
                "gpt_rag_ui.auth.embed_security._existing_socket_session",
                return_value=existing_session,
            ),
            patch(
                "gpt_rag_ui.auth.embed_security._authenticated_socket_user",
                AsyncMock(return_value=current_user),
            ),
        ):
            with self.assertRaises(SocketIOConnectionRefusedError):
                await sio.handlers["/"]["connect"](
                    "socket",
                    transport_environ(),
                    {
                        "clientType": "copilot",
                        "sessionId": "reused-session",
                    },
                )

        original_connect.assert_not_awaited()
        sio.eio.disconnect.assert_awaited_once_with("engine-socket")

    async def test_matching_socket_restore_reaches_chainlit(self):
        original_connect = AsyncMock(return_value=True)

        class FakeSio:
            def __init__(self):
                self.emit = AsyncMock()
                self.call = AsyncMock()
                self.handlers = {"/": {"connect": original_connect}}

            def on(self, event, handler):
                self.handlers["/"][event] = handler

        user = SimpleNamespace(
            identifier="tenant:user",
            metadata={"copilot_session_id": "copilot-session"},
        )
        sio = FakeSio()
        configure_copilot_bridge_guards(sio, sessions=FakeSessions())

        with (
            patch(
                "gpt_rag_ui.auth.embed_security._existing_socket_session",
                return_value=SimpleNamespace(user=user),
            ),
            patch(
                "gpt_rag_ui.auth.embed_security._authenticated_socket_user",
                AsyncMock(return_value=user),
            ),
        ):
            self.assertTrue(
                await sio.handlers["/"]["connect"](
                    "socket",
                    {},
                    {"sessionId": "existing-session"},
                )
            )

        original_connect.assert_awaited_once()

    async def test_revoked_copilot_transport_cannot_connect(self):
        original_connect = AsyncMock(return_value=True)

        class FakeSio:
            def __init__(self):
                self.emit = AsyncMock()
                self.call = AsyncMock()
                self.handlers = {"/": {"connect": original_connect}}

            def on(self, event, handler):
                self.handlers["/"][event] = handler

        user = SimpleNamespace(
            identifier="tenant:user",
            metadata={
                "auth_source": "copilot_session",
                "copilot_session_id": SESSION_ID,
            },
        )
        sio = FakeSio()
        configure_copilot_bridge_guards(sio, sessions=FakeSessions())

        with patch(
            "gpt_rag_ui.auth.embed_security._authenticated_socket_user",
            AsyncMock(return_value=user),
        ):
            with self.assertRaises(SocketIOConnectionRefusedError):
                await sio.handlers["/"]["connect"](
                    "socket",
                    transport_environ(),
                    {"clientType": "copilot"},
                )

        original_connect.assert_not_awaited()

    async def test_chainlit_connect_failure_disconnects_before_untracking(self):
        original_connect = AsyncMock(
            side_effect=KeyError("missing session state")
        )

        class FakeSio:
            def __init__(self):
                self.emit = AsyncMock()
                self.call = AsyncMock()
                self.disconnect = AsyncMock()
                self.handlers = {"/": {"connect": original_connect}}

            def on(self, event, handler):
                self.handlers["/"][event] = handler

        user = SimpleNamespace(
            identifier="tenant:user",
            metadata={
                "auth_source": "copilot_session",
                "copilot_session_id": SESSION_ID,
            },
        )
        active_session = SimpleNamespace(principal_id=user.identifier)
        sio = FakeSio()
        configure_copilot_bridge_guards(
            sio,
            sessions=FakeSessions(active_session),
        )

        with patch(
            "gpt_rag_ui.auth.embed_security._authenticated_socket_user",
            AsyncMock(return_value=user),
        ), patch(
            "chainlit.session.WebsocketSession.get",
            return_value=None,
        ):
            with self.assertRaises(SocketIOConnectionRefusedError):
                await sio.handlers["/"]["connect"](
                    "socket",
                    transport_environ(),
                    {"clientType": "copilot"},
                )

        sio.disconnect.assert_awaited_once_with("socket")
        socket_ids, _ = (
            await embed_security._copilot_socket_registry.session_resources(
                SESSION_ID
            )
        )
        self.assertFalse(socket_ids)

    async def test_successful_restore_cancels_chainlit_timeout_task(self):
        timeout_tasks = []
        original_connect = AsyncMock(return_value=True)

        async def disconnect_handler(socket_id):
            async def clear_on_timeout():
                await asyncio.sleep(60)

            timeout_tasks.append(asyncio.ensure_future(clear_on_timeout()))

        class FakeSio:
            def __init__(self):
                self.emit = AsyncMock()
                self.call = AsyncMock()
                self.manager = SimpleNamespace(
                    disconnect=AsyncMock(),
                    eio_sid_from_sid=lambda socket_id, namespace: (
                        f"engine-{socket_id}"
                    ),
                )
                self.eio = SimpleNamespace(disconnect=AsyncMock())
                self.handlers = {
                    "/": {
                        "connect": original_connect,
                        "disconnect": disconnect_handler,
                    }
                }

            def on(self, event, handler):
                self.handlers["/"][event] = handler

            async def disconnect(self, socket_id):
                await self.handlers["/"]["disconnect"](
                    socket_id,
                    "server disconnect",
                )
                await self.manager.disconnect(
                    socket_id,
                    namespace="/",
                    ignore_queue=True,
                )

        user = SimpleNamespace(
            identifier="tenant:user",
            metadata={
                "auth_source": "copilot_session",
                "copilot_session_id": SESSION_ID,
            },
        )
        active_session = SimpleNamespace(principal_id=user.identifier)
        old_session = SimpleNamespace(id="chainlit", user=user)
        restored_session = SimpleNamespace(id="chainlit", user=user)
        sio = FakeSio()
        configure_copilot_bridge_guards(
            sio,
            sessions=FakeSessions(active_session),
        )
        registry = embed_security._copilot_socket_registry
        self.assertIsNotNone(registry)
        self.assertTrue(
            await registry.bind_connection(
                session_id=SESSION_ID,
                socket_id="old",
                chainlit_session_id="chainlit",
                disconnect=AsyncMock(),
            )
        )
        with (
            patch(
                "gpt_rag_ui.auth.embed_security._authenticated_socket_user",
                AsyncMock(return_value=user),
            ),
            patch(
                "chainlit.session.WebsocketSession.get_by_id",
                return_value=old_session,
            ),
            patch(
                "chainlit.session.WebsocketSession.get",
                return_value=restored_session,
            ),
        ):
            self.assertTrue(
                await sio.handlers["/"]["connect"](
                    "replacement",
                    transport_environ(),
                    {
                        "clientType": "copilot",
                        "sessionId": "chainlit",
                    },
                )
            )
            await asyncio.sleep(0)

        self.assertEqual(1, len(timeout_tasks))
        self.assertTrue(timeout_tasks[0].cancelled())
        sio.eio.disconnect.assert_awaited_once_with("engine-old")

    async def test_copilot_identity_requires_copilot_client_type(self):
        original_connect = AsyncMock(return_value=True)

        class FakeSio:
            def __init__(self):
                self.emit = AsyncMock()
                self.call = AsyncMock()
                self.manager = SimpleNamespace(
                    eio_sid_from_sid=lambda socket_id, namespace: (
                        f"engine-{socket_id}"
                    )
                )
                self.eio = SimpleNamespace(disconnect=AsyncMock())
                self.handlers = {"/": {"connect": original_connect}}

            def on(self, event, handler):
                self.handlers["/"][event] = handler

        user = SimpleNamespace(
            identifier="tenant:user",
            metadata={
                "auth_source": "copilot_session",
                "copilot_session_id": SESSION_ID,
            },
        )
        active_session = SimpleNamespace(principal_id=user.identifier)
        sio = FakeSio()
        configure_copilot_bridge_guards(
            sio,
            sessions=FakeSessions(active_session),
        )

        with patch(
            "gpt_rag_ui.auth.embed_security._authenticated_socket_user",
            AsyncMock(return_value=user),
        ),         patch(
            "chainlit.session.WebsocketSession.get",
            return_value=SimpleNamespace(
                id="generated-session",
                user=user,
            ),
        ):
            self.assertTrue(
                await sio.handlers["/"]["connect"](
                    "copilot-socket",
                    transport_environ(),
                    {"clientType": "copilot"},
                )
            )
            with self.assertRaises(SocketIOConnectionRefusedError):
                await sio.handlers["/"]["connect"](
                    "webapp-socket",
                    transport_environ(),
                    {"clientType": "webapp"},
                )

        original_connect.assert_awaited_once()
        sio.eio.disconnect.assert_awaited_once_with(
            "engine-webapp-socket"
        )

    async def test_copilot_client_type_cannot_fall_back_to_standalone_user(self):
        original_connect = AsyncMock(return_value=True)

        class FakeSio:
            def __init__(self):
                self.emit = AsyncMock()
                self.call = AsyncMock()
                self.handlers = {"/": {"connect": original_connect}}

            def on(self, event, handler):
                self.handlers["/"][event] = handler

        standalone_user = SimpleNamespace(
            identifier="tenant:user",
            metadata={"auth_source": "oauth"},
        )
        sio = FakeSio()
        configure_copilot_bridge_guards(sio, sessions=FakeSessions())

        with patch(
            "gpt_rag_ui.auth.embed_security._authenticated_socket_user",
            AsyncMock(return_value=standalone_user),
        ):
            with self.assertRaises(SocketIOConnectionRefusedError):
                await sio.handlers["/"]["connect"](
                    "socket",
                    transport_environ(),
                    {"clientType": "copilot"},
                )

        original_connect.assert_not_awaited()

    async def test_copilot_socket_requires_opaque_cookie_binding(self):
        original_connect = AsyncMock(return_value=True)

        class FakeEngineIo:
            def __init__(self):
                self.handlers = {
                    "connect": AsyncMock(return_value=True),
                }
                self.disconnect = AsyncMock()

            def on(self, event, handler):
                self.handlers[event] = handler

        class FakeSio:
            def __init__(self):
                self.emit = AsyncMock()
                self.call = AsyncMock()
                self.disconnect = AsyncMock()
                self.manager = SimpleNamespace(
                    eio_sid_from_sid=lambda socket_id, namespace: (
                        f"engine-{socket_id}"
                    )
                )
                self.eio = FakeEngineIo()
                self.handlers = {"/": {"connect": original_connect}}

            def on(self, event, handler):
                self.handlers["/"][event] = handler

        user = SimpleNamespace(
            identifier="tenant:user",
            metadata={
                "auth_source": "copilot_session",
                "copilot_session_id": SESSION_ID,
            },
        )
        active_session = SimpleNamespace(principal_id=user.identifier)
        sio = FakeSio()
        configure_copilot_bridge_guards(
            sio,
            sessions=FakeSessions(active_session),
        )

        with patch(
            "gpt_rag_ui.auth.embed_security._authenticated_socket_user",
            AsyncMock(return_value=user),
        ):
            with self.assertRaises(SocketIOConnectionRefusedError):
                await sio.handlers["/"]["connect"](
                    "socket",
                    transport_environ(session_id=None),
                    {"clientType": "copilot"},
                )
        original_connect.assert_not_awaited()
        sio.eio.disconnect.assert_awaited_once_with("engine-socket")


if __name__ == "__main__":
    unittest.main()
