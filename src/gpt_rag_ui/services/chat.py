import os
import base64
import json
import re
import uuid
import logging
import time
import urllib.parse
from typing import Optional, Set, Tuple
from datetime import datetime, timedelta

import chainlit as cl
import httpx

from gpt_rag_ui.config.chat_backend import load_chat_backend, select_upload_conversation_id
from gpt_rag_ui.clients.orchestrator_client import call_orchestrator_stream
from gpt_rag_ui.services.feedback import create_feedback_actions
from gpt_rag_ui.services.citations import (
    format_hosted_citation_sources,
    replace_source_reference_links,
)
from gpt_rag_ui.config.dependencies import get_config
from gpt_rag_ui.clients.blob import BlobClient
from gpt_rag_ui.services.conversation_security import get_owned_conversation
from gpt_rag_ui.clients.ingestion_client import ingest_files_session
from gpt_rag_ui.services.download_security import get_download_tokens, is_download_target_allowed
from gpt_rag_ui.auth.embed_auth import is_copilot_session_active, resolve_access_token

from gpt_rag_ui.util.constants import APPLICATION_INSIGHTS_CONNECTION_STRING, APP_NAME, UUID_REGEX, REFERENCE_REGEX, TERMINATE_TOKEN
from gpt_rag_ui.telemetry.monitoring import Telemetry
from gpt_rag_ui.telemetry.boundary_diagnostics import log_boundary_failure
from opentelemetry.trace import SpanKind
from chainlit.types import ThreadDict

logger = logging.getLogger("gpt_rag_ui.app")

config = get_config()

Telemetry.configure_monitoring(config, APPLICATION_INSIGHTS_CONNECTION_STRING, APP_NAME)

CHAT_BACKEND = load_chat_backend(config)

if CHAT_BACKEND == "hosted_agent":
    from gpt_rag_ui.clients.hosted_agent_client import (
        HostedAgentAuthenticationError,
        HostedAgentCancelledError,
        build_invocation_messages,
        call_hosted_agent_stream,
        validate_hosted_agent_config,
    )
    validate_hosted_agent_config()
    logger.info("Chat backend: hosted_agent")

    from gpt_rag_ui.services.hosted_continuity import (
        ContinuityPersistenceError,
        ConversationNotFoundError,
        HostedContinuityCoordinator,
    )
    from gpt_rag_ui.config.hosted_continuity_config import load_hosted_continuity_settings
    from gpt_rag_ui.clients.hosted_conversation_capability import ConversationCapabilityManager
    from gpt_rag_ui.clients.hosted_conversation_store import (
        ConversationStoreClient,
        ConversationStoreSettings,
    )

    HOSTED_CONTINUITY_SETTINGS = load_hosted_continuity_settings(config)
    HOSTED_CONTINUITY_ENABLED = HOSTED_CONTINUITY_SETTINGS.enabled
    if HOSTED_CONTINUITY_ENABLED:
        logger.info("Hosted-agent cross-version continuity: enabled")

    from gpt_rag_ui.config.panel_config import load_panel_settings

    PANEL_SETTINGS = load_panel_settings(config)
    if PANEL_SETTINGS.user_surfaces_active:
        logger.info("Administrative panel user-facing history/feedback: enabled")

    async def _panel_owner_index_writer(oid: str, conversation_id: str) -> None:
        """Best-effort owner-index write for a brand-new hosted conversation
        (issue #611, ADR-0004). Only registered below when the panel's
        user-facing surfaces are active; a failure here is logged by the
        caller (``hosted_continuity.HostedContinuityCoordinator.run_turn``)
        and never fails the user's turn."""
        from gpt_rag_ui.clients.panel_cosmos import get_panel_cosmos_client
        from gpt_rag_ui.services.panel_store import upsert_owner_index_row

        client = get_panel_cosmos_client(PANEL_SETTINGS, config)
        await upsert_owner_index_row(
            client=client, principal_id=oid, conversation_id=conversation_id
        )

    _hosted_continuity_coordinator: "HostedContinuityCoordinator | None" = None

    def get_hosted_continuity_coordinator() -> HostedContinuityCoordinator:
        global _hosted_continuity_coordinator
        if _hosted_continuity_coordinator is None:
            capability_manager = None
            if HOSTED_CONTINUITY_SETTINGS.uses_capability_binding:
                capability_manager = ConversationCapabilityManager(
                    key=HOSTED_CONTINUITY_SETTINGS.capability_key,
                    key_id=HOSTED_CONTINUITY_SETTINGS.capability_key_id,
                    ttl_seconds=HOSTED_CONTINUITY_SETTINGS.capability_ttl_seconds,
                )
            _hosted_continuity_coordinator = HostedContinuityCoordinator(
                settings=HOSTED_CONTINUITY_SETTINGS,
                store=ConversationStoreClient(
                    ConversationStoreSettings(
                        base_url=HOSTED_CONTINUITY_SETTINGS.store_base_url,
                        resource_scope=HOSTED_CONTINUITY_SETTINGS.store_resource_scope,
                    ),
                    owner_binding=HOSTED_CONTINUITY_SETTINGS.owner_binding,
                ),
                capability_manager=capability_manager,
                on_conversation_created=(
                    _panel_owner_index_writer
                    if PANEL_SETTINGS.user_surfaces_active
                    else None
                ),
            )
        return _hosted_continuity_coordinator
else:
    logger.info("Chat backend: orchestrator (explicit fallback)")
    HOSTED_CONTINUITY_ENABLED = False

    from gpt_rag_ui.config.panel_config import PanelSettings as _PanelSettings

    PANEL_SETTINGS = _PanelSettings()

ENABLE_FEEDBACK = config.get("ENABLE_USER_FEEDBACK", False, bool)
_is_running_in_azure_host = bool(
    os.environ.get("WEBSITE_SITE_NAME")
    or os.environ.get("CONTAINER_APP_NAME")
    or os.environ.get("CONTAINER_APP_REVISION")
)


def _oauth_is_configured() -> bool:
    # Consider OAuth configured only when the required AAD fields exist.
    # If OAuth isn't configured, we treat requests as anonymous (do not block).
    client_id = (
        (os.environ.get("OAUTH_AZURE_AD_CLIENT_ID") or "").strip()
        or str(config.get("OAUTH_AZURE_AD_CLIENT_ID", "", str) or "").strip()
        or str(config.get("CLIENT_ID", "", str) or "").strip()
    )
    client_secret = (
        (os.environ.get("OAUTH_AZURE_AD_CLIENT_SECRET") or "").strip()
        or str(config.get("OAUTH_AZURE_AD_CLIENT_SECRET", "", str) or "").strip()
        or str(config.get("authClientSecret", "", str) or "").strip()
    )
    tenant_id = (
        (os.environ.get("OAUTH_AZURE_AD_TENANT_ID") or "").strip()
        or str(config.get("OAUTH_AZURE_AD_TENANT_ID", "", str) or "").strip()
    )
    return bool(client_id and client_secret and tenant_id)


COPILOT_ENABLED = (
    os.environ.get("CHAINLIT_COPILOT_ENABLED_EFFECTIVE", "").lower() == "true"
)
OAUTH_CONFIGURED = _oauth_is_configured()

# If OAuth isn't configured, default to allowing anonymous even in Azure.
_allow_anonymous_effective = os.environ.get("ALLOW_ANONYMOUS_EFFECTIVE")
if _allow_anonymous_effective is not None:
    ALLOW_ANONYMOUS = _allow_anonymous_effective.lower() == "true"
else:
    ALLOW_ANONYMOUS = config.get(
        "ALLOW_ANONYMOUS",
        not _is_running_in_azure_host,
        bool,
    )
SHOW_STATISTICS = config.get("SHOW_STATISTICS", False, bool)




def extract_conversation_id_from_chunk(chunk: str) -> Tuple[Optional[str], str]:
    match = UUID_REGEX.match(chunk)
    if match:
        conv_id = match.group(1)
        logger.debug("Extracted conversation id %s from stream chunk", conv_id)
        return conv_id, chunk[match.end():]
    return None, chunk















def check_authorization() -> dict:
    app_user = cl.user_session.get("user")
    if app_user:
        metadata = app_user.metadata or {}
        is_anonymous_copilot = (
            metadata.get("auth_source") == "copilot_session"
            and metadata.get("copilot_auth_mode") == "anonymous"
        )
        return {
            'authorized': metadata.get('authorized', True),
            'client_principal_id': (
                'no-auth'
                if is_anonymous_copilot
                else metadata.get('client_principal_id', 'no-auth')
            ),
            'client_principal_name': (
                'anonymous'
                if is_anonymous_copilot
                else metadata.get('client_principal_name', 'anonymous')
            ),
            'client_group_names': (
                [] if is_anonymous_copilot else metadata.get('client_group_names', [])
            ),
            'access_token': (
                metadata.get('access_token')
                if metadata.get("auth_source") != "copilot_session"
                else None
            ),
            'principal_id': (
                '' if is_anonymous_copilot else metadata.get('principal_id', '')
            ),
            'tenant_id': (
                '' if is_anonymous_copilot else metadata.get('tenant_id', '')
            ),
            'object_id': (
                '' if is_anonymous_copilot else metadata.get('object_id', '')
            ),
            'copilot_auth_mode': metadata.get('copilot_auth_mode', ''),
            'copilot_session_id': metadata.get('copilot_session_id', ''),
        }

    # If OAuth is configured but we don't have a user in session,
    # treat as unauthorized (forces the UI to require auth).
    # Otherwise, allow anonymous.
    return {
        'authorized': (
            ALLOW_ANONYMOUS
            if not OAUTH_CONFIGURED
            else False
        ),
        'client_principal_id': 'no-auth',
        'client_principal_name': 'anonymous',
        'client_group_names': [],
        'access_token': None
    }


def _feedback_is_available(auth_info: dict) -> bool:
    return auth_info.get("copilot_auth_mode") != "anonymous"


async def get_auth_info() -> dict:
    """Return the effective auth info for the current session.

    If OAuth is configured and a user session exists, automatically refreshes the access token
    when it is close to expiry to avoid "invalid token" failures in the orchestrator.
    """

    app_user = cl.user_session.get("user")
    if app_user:
        metadata = app_user.metadata or {}
        if metadata.get("auth_source") == "copilot_session":
            active = await is_copilot_session_active(metadata)
            access_token = await resolve_access_token(metadata)
            auth_mode = metadata.get("copilot_auth_mode")
            if not active or (auth_mode == "entra" and not access_token):
                logger.warning(
                    "Embedded Copilot session expired for user=%s",
                    metadata.get("client_principal_name")
                    or metadata.get("client_principal_id")
                    or app_user.identifier,
                )
                cl.user_session.set("user", None)
                return {
                    'authorized': False,
                    'client_principal_id': 'no-auth',
                    'client_principal_name': 'anonymous',
                    'client_group_names': [],
                    'access_token': None,
                    'auth_error': 'session_expired',
                }
        else:
            auth_mode = ""
            access_token = metadata.get("access_token")

        # Opportunistic token refresh (OAuth mode only).
        if metadata.get("auth_source") == "oauth":
            try:
                # Import is safe because we import auth_oauth only when OAUTH_CONFIGURED.
                refreshed = await auth_oauth.ensure_fresh_user_access_token(app_user, min_ttl_seconds=120)
                if refreshed:
                    cl.user_session.set("user", app_user)
            except Exception:
                # If refresh fails, clear the user session so the UI can re-auth.
                log_boundary_failure(
                    logger, "User access token refresh failed; clearing session to force re-auth",
                    level=logging.WARNING,
                )
                cl.user_session.set("user", None)
                return {
                    'authorized': False,
                    'client_principal_id': 'no-auth',
                    'client_principal_name': 'anonymous',
                    'client_group_names': [],
                    'access_token': None,
                    'auth_error': 'session_expired',
                }

        is_anonymous_copilot = (
            metadata.get("auth_source") == "copilot_session"
            and auth_mode == "anonymous"
        )
        return {
            'authorized': metadata.get('authorized', True),
            'client_principal_id': (
                'no-auth'
                if is_anonymous_copilot
                else metadata.get('client_principal_id', 'no-auth')
            ),
            'client_principal_name': (
                'anonymous'
                if is_anonymous_copilot
                else metadata.get('client_principal_name', 'anonymous')
            ),
            'client_group_names': (
                [] if is_anonymous_copilot else metadata.get('client_group_names', [])
            ),
            'access_token': access_token,
            'principal_id': (
                '' if is_anonymous_copilot else metadata.get('principal_id', '')
            ),
            'tenant_id': (
                '' if is_anonymous_copilot else metadata.get('tenant_id', '')
            ),
            'object_id': (
                '' if is_anonymous_copilot else metadata.get('object_id', '')
            ),
            'copilot_auth_mode': auth_mode,
            'copilot_session_id': (
                metadata.get('copilot_session_id', '')
                if metadata.get("auth_source") == "copilot_session"
                else ""
            ),
        }

    return {
        'authorized': (
            ALLOW_ANONYMOUS
            if not OAUTH_CONFIGURED
            else False
        ),
        'client_principal_id': 'no-auth',
        'client_principal_name': 'anonymous',
        'client_group_names': [],
        'access_token': None
    }


def _decode_jwt_unverified(token: str) -> dict | None:
    """Decode JWT payload without verifying signature.

    Debug-only helper. Never use this to authorize.
    """

    try:
        parts = (token or "").split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = base64.urlsafe_b64decode(payload_b64.encode("utf-8"))
        data = json.loads(payload.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def _access_token_debug_summary(access_token: str) -> dict:
    claims = _decode_jwt_unverified(access_token) or {}
    aud = claims.get("aud")
    if isinstance(aud, list):
        aud_value = ",".join(str(x) for x in aud)
    else:
        aud_value = str(aud) if aud is not None else None

    def _short(value: object) -> str:
        s = str(value or "")
        if len(s) <= 10:
            return s
        return f"{s[:4]}…{s[-4:]}"

    return {
        "aud": aud_value,
        "tid": _short(claims.get("tid")) if claims.get("tid") else None,
        "oid": _short(claims.get("oid")) if claims.get("oid") else None,
        "iss": claims.get("iss"),
        "scp": claims.get("scp"),
        "ver": claims.get("ver"),
    }

if OAUTH_CONFIGURED:
    ENABLE_AUTHENTICATION = True
    import gpt_rag_ui.auth.oauth as auth_oauth
    logger.info("Authentication enabled: Chainlit OAuth (Azure AD)")
else:
    ENABLE_AUTHENTICATION = False
    if ALLOW_ANONYMOUS:
        logger.warning(
            "Authentication disabled: OAuth not configured; running in anonymous mode (ALLOW_ANONYMOUS=true)"
        )
    elif COPILOT_ENABLED:
        logger.warning(
            "Standalone OAuth is unavailable and standalone anonymous access is "
            "disabled; explicitly configured Copilot sessions remain available."
        )
    else:
        raise RuntimeError(
            "OAuth is not configured (missing client_id/tenant_id/client_secret) and ALLOW_ANONYMOUS=false. "
            "Set OAUTH_AZURE_AD_CLIENT_ID, OAUTH_AZURE_AD_TENANT_ID, and OAUTH_AZURE_AD_CLIENT_SECRET (or authClientSecret)."
        )

tracer = Telemetry.get_tracer(__name__)

# Chainlit event handlers
async def on_chat_start():
    if CHAT_BACKEND == "hosted_agent":
        cl.user_session.set("hosted_agent_conversation_id", "")
        if HOSTED_CONTINUITY_ENABLED:
            cl.user_session.set("hosted_continuity_capability", "")
    # app_user = cl.user_session.get("user")
    # if app_user:
        # await cl.Message(content=f"Hello {app_user.metadata.get('user_name')}").send()

async def on_chat_resume(thread: ThreadDict):
    app_user = cl.user_session.get("user")
    if (
        not app_user
        or not await is_copilot_session_active(app_user.metadata)
        or thread.get("userIdentifier") != app_user.identifier
    ):
        logger.warning("Blocked unauthorized chat resume: thread=%s", thread["id"])
        raise PermissionError("Thread access denied.")
    cl.user_session.set("conversation_id", thread["id"])
    if CHAT_BACKEND == "hosted_agent":
        # A Chainlit thread ID is not proof of a runtime-managed conversation.
        # Ordered history restores context; the runtime will issue a new managed ID.
        cl.user_session.set("hosted_agent_conversation_id", "")
        if HOSTED_CONTINUITY_ENABLED:
            # Same reasoning: a resumed Chainlit thread id is not proof of
            # ownership of a hosted-continuity managed conversation, so the
            # opaque capability is dropped too. The next turn mints a fresh
            # managed conversation and capability for the current oid.
            cl.user_session.set("hosted_continuity_capability", "")
    logger.info("Chat resumed: thread=%s", thread["id"])

async def handle_message(message: cl.Message):

    with tracer.start_as_current_span('handle_message', kind=SpanKind.SERVER) as span:

        message.id = message.id or str(uuid.uuid4())
        prior_conversation_id = cl.user_session.get("conversation_id")
        conversation_id = prior_conversation_id or ""
        existing_conversation_id = str(conversation_id).strip()
        hosted_conversation_id = (
            str(cl.user_session.get("hosted_agent_conversation_id") or "").strip()
            if CHAT_BACKEND == "hosted_agent"
            else ""
        )
        response_msg = cl.Message(content="")
        hosted_history = (
            cl.chat_context.to_openai()
            if CHAT_BACKEND == "hosted_agent"
            else None
        )

        def _trim_for_log(value: str, limit: int = 400) -> str:
            clean_value = (value or "").strip().replace("\n", " ")
            if len(clean_value) > limit:
                return f"{clean_value[:limit].rstrip()}..."
            return clean_value

        auth_info = await get_auth_info()
        principal = auth_info.get('client_principal_name', 'anonymous')

        if auth_info.get('auth_error') == 'session_expired':
            await response_msg.stream_token(
                "Your session has expired. Please sign out and sign in again to continue."
            )
            logger.warning(
                "Blocked request due to expired auth session: conversation=%s",
                conversation_id or "new",
            )
            return

        if not auth_info.get('authorized', False):
            await response_msg.stream_token(
                "Oops! It looks like you don’t have access to this service. "
                "If you think you should, please reach out to your administrator for help."
            )
            logger.warning(
                "Blocked unauthorized request: conversation=%s user=%s",
                conversation_id or "new",
                auth_info.get('client_principal_id', 'unknown'),
            )
            return


        await response_msg.send()
        handler_start = time.time()
        # ====== FILES PROCESSING ======
        allowed_mimes = {
            "application/pdf",
        }
        max_files = 5
        max_file_bytes = 15 * 1024 * 1024
        max_total_bytes = 25 * 1024 * 1024

        uploaded_files: list[dict] = []
        rejected: list[str] = []
        total_declared_bytes = 0
        file_reply_parts: list[str] = []

        if message.elements:
            for element in message.elements:
                if not isinstance(element, cl.File):
                    continue

                mime = (getattr(element, "mime", "") or "").lower()
                name = getattr(element, "name", "upload")
                path = getattr(element, "path", None)
                size = getattr(element, "size", 0) or 0

                if auth_info.get("copilot_auth_mode") == "anonymous":
                    rejected.append(f"{name} (uploads require Entra Copilot mode)")
                    continue

                if not path:
                    rejected.append(f"{name} (missing path)")
                    continue

                if mime not in allowed_mimes:
                    rejected.append(f"{name} (unsupported type: {mime or 'unknown'})")
                    continue

                if isinstance(size, int) and size > max_file_bytes:
                    rejected.append(f"{name} (too large)")
                    continue

                total_declared_bytes += int(size) if isinstance(size, int) else 0
                if total_declared_bytes > max_total_bytes:
                    rejected.append(f"{name} (total upload too large)")
                    continue

                uploaded_files.append({"name": name, "path": path, "mime": mime, "size": int(size)})
                logger.info(
                    "File queued for ingestion: name=%s mime=%s conversation=%s",
                    name,
                    mime,
                    conversation_id or "new",
                )

        if len(uploaded_files) > max_files:
            rejected.extend([f["name"] + " (too many files)" for f in uploaded_files[max_files:]])
            uploaded_files = uploaded_files[:max_files]

        upload_conversation_id = select_upload_conversation_id(
            CHAT_BACKEND,
            classic_conversation_id=existing_conversation_id,
            hosted_conversation_id=hosted_conversation_id,
        )
        if uploaded_files and not upload_conversation_id and CHAT_BACKEND == "hosted_agent":
            rejected.extend(
                f"{file['name']} (start the hosted conversation with a text message first)"
                for file in uploaded_files
            )
            uploaded_files = []

        if (
            uploaded_files
            and CHAT_BACKEND == "orchestrator"
            and upload_conversation_id
        ):
            owned_conversation = await get_owned_conversation(
                upload_conversation_id,
                auth_info,
            )
            if not owned_conversation:
                logger.warning(
                    "Blocked upload to missing or unauthorized conversation=%s",
                    upload_conversation_id,
                )
                rejected.extend(
                    f"{file['name']} (conversation access denied)"
                    for file in uploaded_files
                )
                uploaded_files = []

        if rejected:
            _skip_msg = "Some files were skipped:\n- " + "\n- ".join(rejected) + "\n\n"
            file_reply_parts.append(_skip_msg)
            await response_msg.stream_token(_skip_msg)

        if uploaded_files and not upload_conversation_id:
            conversation_id = str(uuid.uuid4())
            cl.user_session.set("conversation_id", conversation_id)
        elif uploaded_files:
            conversation_id = upload_conversation_id

        if uploaded_files:
            try:
                ingestion_success = await ingest_files_session(
                    conversation_id=conversation_id,
                    question_id=message.id,
                    auth_info=auth_info,
                    files=uploaded_files,
                )
            except Exception:
                log_boundary_failure(logger,
                    "File ingestion failed: conversation=%s question_id=%s",
                    conversation_id or "new",
                    message.id,
                )
                ingestion_success = False
            else:
                if not ingestion_success:
                    logger.warning(
                        "File ingestion was not confirmed: conversation=%s question_id=%s",
                        conversation_id or "new",
                        message.id,
                    )

            if ingestion_success:
                session_docs = cl.user_session.get("uploaded_docs") or []
                session_docs.extend([f["name"] for f in uploaded_files])
                cl.user_session.set("uploaded_docs", session_docs)
                _ok_msg = f"{len(uploaded_files)} file(s) processed successfully.\n\n"
                file_reply_parts.append(_ok_msg)
                await response_msg.stream_token(_ok_msg)
            else:
                if not upload_conversation_id:
                    # No turn created this new conversation. Do not make a retry
                    # treat its abandoned upload ID as an owned history thread.
                    cl.user_session.set("conversation_id", prior_conversation_id)
                _fail_msg = (
                    "File ingestion failed. Your question was not sent. "
                    "Please retry by attaching the files and sending your question again. "
                    "If the problem persists, contact the application support team and share reference "
                    f"{message.id}.\n\n"
                )
                file_reply_parts.append(_fail_msg)
                await response_msg.stream_token(_fail_msg)
                response_msg.content = "".join(file_reply_parts).strip()
                await response_msg.update()
                return

        user_ask = (message.content or "").strip()
        if CHAT_BACKEND == "hosted_agent" and not user_ask:
            final_text = "".join(file_reply_parts).strip()
            if not final_text:
                final_text = "Enter a question to start or continue the hosted conversation."
            if SHOW_STATISTICS:
                final_text += f"\n\n*\u23f1 {time.time() - handler_start:.2f}s*"
            response_msg.content = final_text
            await response_msg.update()
            logger.info(
                "Skipping hosted agent (empty ask): conversation=%s question_id=%s",
                hosted_conversation_id or "new",
                message.id,
            )
            return

        if uploaded_files and not user_ask:
            final_text = "".join(file_reply_parts).strip() or "Files received."
            if SHOW_STATISTICS:
                final_text += f"\n\n*\u23f1 {time.time() - handler_start:.2f}s*"
            cl.user_session.set("conversation_id", conversation_id)
            span.set_attribute("question_id", message.id)
            span.set_attribute("conversation_id", conversation_id)
            span.set_attribute("user_id", auth_info.get("client_principal_id", "anonymous"))
            logger.info(
                "Skipping orchestrator (files only, empty ask): conversation=%s question_id=%s",
                conversation_id or "new",
                message.id,
            )
            response_msg.content = final_text
            await response_msg.update()
            logger.info(
                "Response delivered: conversation=%s question_id=%s chunks=0 characters=%s preview='%s'",
                conversation_id,
                message.id,
                len(final_text),
                _trim_for_log(final_text),
            )
            return

        # ----------------------------------------------


        app_user = cl.user_session.get("user")

        span.set_attribute('question_id', message.id)
        span.set_attribute('conversation_id', conversation_id)
        span.set_attribute('user_id', auth_info.get('client_principal_id', 'anonymous'))
        logger.info(
            "User request received: conversation=%s question_id=%s user=%s preview='%s'",
            conversation_id or "new",
            message.id,
            principal,
            _trim_for_log(message.content),
        )

        await response_msg.stream_token(" ")

        response_start_time = time.time()
        buffer = ""
        full_text = ""
        references = set()
        chunk_count = 0

        if CHAT_BACKEND == "hosted_agent" and HOSTED_CONTINUITY_ENABLED:
            # ------------------------------------------------------------------
            # Hosted-agent cross-version continuity (HOSTED_CONTINUITY_ENABLED)
            # ------------------------------------------------------------------
            # This BFF, not the hosted runtime, owns the managed Conversation:
            # history is read/appended here and the hosted runtime receives a
            # complete, bounded, stateless Responses input with no top-level
            # conversation reference. Continuity across turns is bound to the
            # caller's opaque capability (never a raw conversation id).
            object_id = str(auth_info.get("object_id") or "").strip()
            stored_capability = str(
                cl.user_session.get("hosted_continuity_capability") or ""
            ).strip()
            logger.info(
                "Forwarding request to hosted agent (continuity): question_id=%s user=%s authorized=%s",
                message.id,
                principal,
                auth_info.get("authorized"),
            )
            hosted_citations = []
            generator = None

            try:
                if not object_id:
                    raise HostedAgentAuthenticationError(
                        "Hosted-agent continuity requires a validated Entra oid; "
                        "the current session is anonymous."
                    )
                coordinator = get_hosted_continuity_coordinator()
                generator = coordinator.run_turn(
                    capability=stored_capability,
                    oid=object_id,
                    user_ask=message.content,
                    client_turn_id=message.id,
                    question_id=message.id,
                    correlation_id=message.id,
                    user_access_token=auth_info.get("access_token"),
                )
                async for text_chunk, meta in generator:
                    if meta.get("capability"):
                        cl.user_session.set(
                            "hosted_continuity_capability", meta["capability"]
                        )
                    if meta.get("conversation_id"):
                        conversation_id = meta["conversation_id"]
                    if meta.get("citation"):
                        hosted_citations.append(meta["citation"])
                    if meta.get("tool_activity"):
                        tool = meta["tool_activity"]
                        logger.info(
                            "Hosted-agent tool activity: conversation=%s question_id=%s "
                            "tool=%s status=%s event=%s",
                            conversation_id or "pending",
                            message.id,
                            tool.get("name") or "unknown",
                            tool.get("status") or "unknown",
                            tool.get("event_type") or "unknown",
                        )

                    if not text_chunk:
                        continue

                    chunk_refs: Set[str] = set()
                    text_chunk = replace_source_reference_links(
                        text_chunk,
                        chunk_refs,
                        conversation_id=conversation_id,
                        principal_id=str(auth_info.get("principal_id") or ""),
                        copilot_session_id=str(auth_info.get("copilot_session_id") or ""),
                    )
                    if chunk_refs:
                        references.update(chunk_refs)

                    full_text += text_chunk
                    chunk_count += 1
                    await response_msg.stream_token(text_chunk)

                citation_text = format_hosted_citation_sources(
                    hosted_citations,
                    references,
                    conversation_id=conversation_id,
                    principal_id=str(auth_info.get("principal_id") or ""),
                    copilot_session_id=str(auth_info.get("copilot_session_id") or ""),
                )
                if citation_text:
                    full_text += citation_text
                    await response_msg.stream_token(citation_text)

            except ContinuityPersistenceError as exc:
                # The turn may already have streamed an answer to the user,
                # but it was not durably saved. Never present this as a
                # successfully committed turn.
                logger.error(
                    "Hosted-agent continuity turn could not be durably saved: "
                    "conversation=%s question_id=%s error=%s",
                    conversation_id or "pending",
                    message.id,
                    exc,
                )
                user_error_message = (
                    "We couldn't reliably save this turn, so it may not be there "
                    "next time you return. Please retry your last message and "
                    f"share reference {message.id} if this continues."
                )
                full_text = user_error_message
                await response_msg.stream_token(user_error_message)

            except ConversationNotFoundError as exc:
                # A client-presented conversation handle failed validation
                # for the current identity (cross-user, forged, malformed,
                # missing, or an arbitrary guess -- all indistinguishable
                # here by design). Fail closed: no conversation was created,
                # the hosted agent was never invoked, and nothing was
                # persisted for this turn. Never present this as an
                # assistant answer or start a new thread automatically.
                logger.warning(
                    "Hosted-agent continuity conversation reference was "
                    "rejected for the current identity: question_id=%s error=%s",
                    message.id,
                    exc,
                )
                user_error_message = (
                    "We couldn't find that conversation. Please start a new "
                    "chat and try again, and share reference "
                    f"{message.id} if this continues."
                )
                full_text = user_error_message
                await response_msg.stream_token(user_error_message)

            except HostedAgentAuthenticationError as exc:
                logger.warning(
                    "Hosted-agent continuity authentication failed: question_id=%s error=%s",
                    message.id,
                    exc,
                )
                user_error_message = (
                    "You need to be signed in to continue this hosted conversation. "
                    "Please sign in and try again."
                )
                full_text = user_error_message
                await response_msg.stream_token(user_error_message)

            except HostedAgentCancelledError as exc:
                logger.warning(
                    "Hosted agent cancelled response: conversation=%s question_id=%s reason=%s",
                    conversation_id or "pending",
                    message.id,
                    exc,
                )
                user_error_message = (
                    "The hosted agent cancelled this request. "
                    "Please retry or contact the application support team and share reference "
                    f"{message.id}."
                )
                full_text = user_error_message
                await response_msg.stream_token(user_error_message)

            except httpx.ConnectError as e:
                logger.error(
                    "Hosted agent unreachable (connection error): conversation=%s question_id=%s error=%s",
                    conversation_id or "pending",
                    message.id,
                    e,
                )
                user_error_message = (
                    "We couldn't reach the hosted agent service. "
                    "Please contact the application support team and share reference "
                    f"{message.id}."
                )
                full_text = user_error_message
                await response_msg.stream_token(user_error_message)

            except httpx.TimeoutException as e:
                logger.error(
                    "Hosted agent request timed out: conversation=%s question_id=%s error=%s",
                    conversation_id or "pending",
                    message.id,
                    e,
                )
                user_error_message = (
                    "The hosted agent service took too long to respond. "
                    "Please contact the application support team and share reference "
                    f"{message.id}."
                )
                full_text = user_error_message
                await response_msg.stream_token(user_error_message)

            except Exception:
                user_error_message = (
                    "We hit a technical issue while processing your request. "
                    "Please contact the application support team and share reference "
                    f"{message.id}."
                )
                log_boundary_failure(logger,
                    "Failed while processing hosted agent continuity response: "
                    "conversation=%s question_id=%s",
                    conversation_id or "pending",
                    message.id,
                )
                full_text = user_error_message
                await response_msg.stream_token(user_error_message)

            finally:
                if generator is not None:
                    try:
                        await generator.aclose()
                    except RuntimeError as exc:
                        if "async generator ignored GeneratorExit" not in str(exc):
                            raise

        elif CHAT_BACKEND == "hosted_agent":
            hosted_messages = build_invocation_messages(
                hosted_history or [],
                message.content,
            )
            logger.info(
                "Forwarding request to hosted agent: conversation=%s question_id=%s user=%s authorized=%s",
                hosted_conversation_id or "new",
                message.id,
                principal,
                auth_info.get("authorized"),
            )

            generator = call_hosted_agent_stream(
                hosted_messages,
                conversation_id=hosted_conversation_id,
                question_id=message.id,
                correlation_id=message.id,
                user_access_token=auth_info.get("access_token"),
            )
            hosted_citations: list[dict] = []

            try:
                async for text_chunk, meta in generator:
                    if meta.get("conversation_id"):
                        hosted_conversation_id = meta["conversation_id"]
                        conversation_id = hosted_conversation_id
                        cl.user_session.set(
                            "hosted_agent_conversation_id",
                            hosted_conversation_id,
                        )
                    if meta.get("citation"):
                        hosted_citations.append(meta["citation"])
                    if meta.get("tool_activity"):
                        tool = meta["tool_activity"]
                        logger.info(
                            "Hosted-agent tool activity: conversation=%s question_id=%s "
                            "tool=%s status=%s event=%s",
                            conversation_id or "pending",
                            message.id,
                            tool.get("name") or "unknown",
                            tool.get("status") or "unknown",
                            tool.get("event_type") or "unknown",
                        )

                    if not text_chunk:
                        continue

                    # Rewrite reference links as authenticated download URLs.
                    chunk_refs: Set[str] = set()
                    text_chunk = replace_source_reference_links(
                        text_chunk,
                        chunk_refs,
                        conversation_id=conversation_id,
                        principal_id=str(auth_info.get("principal_id") or ""),
                        copilot_session_id=str(auth_info.get("copilot_session_id") or ""),
                    )
                    if chunk_refs:
                        references.update(chunk_refs)
                        logger.info(
                            "Hosted-agent response references detected: conversation=%s "
                            "question_id=%s reference_count=%s",
                            conversation_id or "pending",
                            message.id,
                            len(chunk_refs),
                        )

                    full_text += text_chunk
                    chunk_count += 1
                    await response_msg.stream_token(text_chunk)

                citation_text = format_hosted_citation_sources(
                    hosted_citations,
                    references,
                    conversation_id=conversation_id,
                    principal_id=str(auth_info.get("principal_id") or ""),
                    copilot_session_id=str(auth_info.get("copilot_session_id") or ""),
                )
                if citation_text:
                    full_text += citation_text
                    await response_msg.stream_token(citation_text)

            except HostedAgentCancelledError as exc:
                logger.warning(
                    "Hosted agent cancelled response: conversation=%s question_id=%s reason=%s",
                    conversation_id or "pending",
                    message.id,
                    exc,
                )
                user_error_message = (
                    "The hosted agent cancelled this request. "
                    "Please retry or contact the application support team and share reference "
                    f"{message.id}."
                )
                full_text = user_error_message
                await response_msg.stream_token(user_error_message)

            except httpx.ConnectError as e:
                logger.error(
                    "Hosted agent unreachable (connection error): conversation=%s question_id=%s error=%s",
                    conversation_id or "pending",
                    message.id,
                    e,
                )
                user_error_message = (
                    "We couldn't reach the hosted agent service. "
                    "Please contact the application support team and share reference "
                    f"{message.id}."
                )
                full_text = user_error_message
                await response_msg.stream_token(user_error_message)

            except httpx.TimeoutException as e:
                logger.error(
                    "Hosted agent request timed out: conversation=%s question_id=%s error=%s",
                    conversation_id or "pending",
                    message.id,
                    e,
                )
                user_error_message = (
                    "The hosted agent service took too long to respond. "
                    "Please contact the application support team and share reference "
                    f"{message.id}."
                )
                full_text = user_error_message
                await response_msg.stream_token(user_error_message)

            except Exception:
                user_error_message = (
                    "We hit a technical issue while processing your request. "
                    "Please contact the application support team and share reference "
                    f"{message.id}."
                )
                log_boundary_failure(logger,
                    "Failed while processing hosted agent response: conversation=%s question_id=%s",
                    conversation_id or "pending",
                    message.id,
                )
                full_text = user_error_message
                await response_msg.stream_token(user_error_message)

            finally:
                try:
                    await generator.aclose()
                except RuntimeError as exc:
                    if "async generator ignored GeneratorExit" not in str(exc):
                        raise

        else:
            # ------------------------------------------------------------------
            # Explicit classic orchestrator fallback (CHAT_BACKEND=orchestrator)
            # ------------------------------------------------------------------
            logger.info(
                "Forwarding request to orchestrator: conversation=%s question_id=%s user=%s authorized=%s groups=%d",
                conversation_id or "new",
                message.id,
                principal,
                auth_info.get("authorized"),
                len(auth_info.get("client_group_names", [])),
            )

            if logger.isEnabledFor(logging.DEBUG) and auth_info.get("access_token"):
                logger.debug(
                    "Orchestrator call access token claims (unverified): conversation=%s question_id=%s %s",
                    conversation_id or "new",
                    message.id,
                    _access_token_debug_summary(str(auth_info.get("access_token"))),
                )
            logger.debug(
                "Orchestrator payload preview: conversation=%s question_id=%s preview='%s'",
                conversation_id or "new",
                message.id,
                _trim_for_log(message.content),
            )
            generator = call_orchestrator_stream(conversation_id, message.content, auth_info, message.id)

            first_content_seen = False
            is_first_chunk = True
            uuid_buffer = ""

            try:
                async for raw_chunk in generator:
                    if not raw_chunk:
                        continue

                    if "[ERROR en MAF Streaming]:" in raw_chunk or "[ERROR]:" in raw_chunk:
                        await cl.ErrorMessage(content=f"Error de Servicio: {raw_chunk.strip()}").send()
                        break

                    if is_first_chunk:
                        uuid_buffer += raw_chunk
                        if len(uuid_buffer) >= 37:
                            is_first_chunk = False
                            chunk = uuid_buffer
                            uuid_buffer = ""
                        else:
                            continue
                    else:
                        chunk = raw_chunk

                    # Extract and update conversation ID
                    extracted_id, cleaned_chunk = extract_conversation_id_from_chunk(chunk)
                    if extracted_id:
                        conversation_id = extracted_id

                    cleaned_chunk = cleaned_chunk.replace("\\n", "\n")

                    normalized_preview = cleaned_chunk.strip().lower()
                    if not first_content_seen and normalized_preview:
                        if (
                            normalized_preview.startswith("<!doctype")
                            or normalized_preview.startswith("<html")
                            or "<html" in normalized_preview[:120]
                            or "azure container apps" in normalized_preview
                        ):
                            logger.error(
                                "Received HTML payload from orchestrator: conversation=%s question_id=%s",
                                conversation_id or "pending",
                                message.id,
                            )
                            raise RuntimeError("orchestrator returned html placeholder")
                        first_content_seen = True

                    # Track and rewrite references as blob download links
                    chunk_refs_orch: Set[str] = set()
                    cleaned_chunk = replace_source_reference_links(
                        cleaned_chunk,
                        chunk_refs_orch,
                        conversation_id=conversation_id,
                        principal_id=str(auth_info.get("principal_id") or ""),
                        copilot_session_id=str(
                            auth_info.get("copilot_session_id") or ""
                        ),
                    )
                    if chunk_refs_orch:
                        references.update(chunk_refs_orch)
                        logger.info(
                            "Streaming response references detected: conversation=%s "
                            "question_id=%s reference_count=%s",
                            conversation_id or "pending",
                            message.id,
                            len(chunk_refs_orch),
                        )

                    buffer += cleaned_chunk
                    full_text += cleaned_chunk
                    chunk_count += 1

                    # Handle TERMINATE token
                    token_index = buffer.find(TERMINATE_TOKEN)
                    if token_index != -1:
                        if token_index > 0:
                            await response_msg.stream_token(buffer[:token_index])
                        logger.debug(
                            "Terminate token detected, draining remaining orchestrator stream: conversation=%s question_id=%s",
                            conversation_id or "pending",
                            message.id,
                        )
                        async for _ in generator:
                            pass  # drain
                        break

                    # Stream safe part of buffer
                    if token_index != -1:
                        safe_flush_length = len(buffer) - (len(TERMINATE_TOKEN) - 1)
                    else:
                        safe_flush_length = len(buffer)

                    if safe_flush_length > 0:
                        await response_msg.stream_token(buffer[:safe_flush_length])
                        buffer = buffer[safe_flush_length:]

            except httpx.ConnectError as e:
                logger.error(
                    "Orchestrator unreachable (connection error): conversation=%s question_id=%s error=%s",
                    conversation_id or "pending",
                    message.id,
                    e,
                )
                user_error_message = (
                    "We couldn't reach the orchestrator service. "
                    "Please contact the application support team and share reference "
                    f"{message.id}."
                )
                full_text = user_error_message
                buffer = ""
                await response_msg.stream_token(user_error_message)

            except httpx.TimeoutException as e:
                logger.error(
                    "Orchestrator request timed out: conversation=%s question_id=%s error=%s",
                    conversation_id or "pending",
                    message.id,
                    e,
                )
                user_error_message = (
                    "The orchestrator service took too long to respond. "
                    "Please contact the application support team and share reference "
                    f"{message.id}."
                )
                full_text = user_error_message
                buffer = ""
                await response_msg.stream_token(user_error_message)

            except Exception as e:
                user_error_message = (
                    "We hit a technical issue while processing your request. "
                    "Please contact the application support team and share reference "
                    f"{message.id}."
                )
                log_boundary_failure(logger,
                    "Failed while processing orchestrator response: conversation=%s question_id=%s",
                    conversation_id or "pending",
                    message.id,
                )
                full_text = user_error_message
                buffer = ""
                await response_msg.stream_token(user_error_message)

            finally:
                try:
                    await generator.aclose()
                except RuntimeError as exc:
                    if "async generator ignored GeneratorExit" not in str(exc):
                        raise

        cl.user_session.set("conversation_id", conversation_id)
        if references:
            logger.info(
                "Aggregated response references: conversation=%s question_id=%s "
                "reference_count=%s",
                conversation_id,
                message.id,
                len(references),
            )
        if (
            ENABLE_FEEDBACK
            and _feedback_is_available(auth_info)
            and (message.content or "").strip()
        ):
            response_msg.actions = create_feedback_actions(
                message.id, conversation_id, message.content
            )
        final_text = replace_source_reference_links(
            full_text.replace(TERMINATE_TOKEN, ""),
            references,
            conversation_id=conversation_id,
            principal_id=str(auth_info.get("principal_id") or ""),
            copilot_session_id=str(
                auth_info.get("copilot_session_id") or ""
            ),
        )
        if SHOW_STATISTICS:
            elapsed = time.time() - response_start_time
            final_text += f"\n\n*\u23f1 {elapsed:.2f}s*"
        response_msg.content = final_text
        await response_msg.update()

        logger.info(
            "Response delivered: conversation=%s question_id=%s chunks=%s characters=%s preview='%s'",
            conversation_id,
            message.id,
            chunk_count,
            len(final_text),
            _trim_for_log(final_text),
        )