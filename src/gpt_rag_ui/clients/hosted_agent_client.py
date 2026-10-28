"""Async client for the default Microsoft Foundry hosted-agent chat path."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import uuid
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

import httpx
import msal
from azure.core.credentials import AccessToken
from azure.core.exceptions import AzureError
from azure.identity.aio import (
    AzureCliCredential,
    ChainedTokenCredential,
    ManagedIdentityCredential,
)

from gpt_rag_ui.config.dependencies import get_config
from gpt_rag_ui.config.errors import ConfigurationError
from gpt_rag_ui.auth.auth_common import (
    DelegationError, normalize_guid, validated_claims_challenge, http_claims_challenge,
)
from gpt_rag_ui.auth.entra_token import EntraTokenError, EntraTokenValidator

logger = logging.getLogger("gpt_rag_ui.hosted_agent_client")
config = get_config()

DEFAULT_SSE_IDLE_TIMEOUT_SECONDS = 60.0
SEARCH_USER_SCOPE = "https://search.azure.com/.default"

# "user_delegated" (default) performs an OAuth2 On-Behalf-Of exchange of the
# signed-in Chainlit user's own access token, so Microsoft Foundry resolves the
# end user as the caller identity. Protected retrieval independently forwards
# an audience-specific Search token under ADR-0028; a Foundry token alone is
# not downstream authorization.
#
# "service_identity" is an explicit, non-default opt-out that uses this
# service's own managed-identity/Azure CLI credential. It must be configured
# deliberately (HOSTED_AGENT_AUTH_MODE=service_identity) and is never selected
# implicitly or as a fallback.
HostedAgentAuthMode = Literal["user_delegated", "service_identity"]
_VALID_AUTH_MODES: frozenset[str] = frozenset({"user_delegated", "service_identity"})


class HostedAgentError(RuntimeError):
    """Base error for the hosted-agent BFF path."""


class HostedAgentConfigError(HostedAgentError):
    """Raised when hosted-agent configuration is missing or invalid."""


class HostedAgentAuthenticationError(HostedAgentError):
    """Raised when the server cannot acquire a hosted data-plane token."""

    def __init__(
        self, message: str, *, code: str = "authentication_required",
        claims_challenge: object = None,
    ):
        super().__init__(message)
        recovery = DelegationError(code, claims_challenge=claims_challenge)
        self.code = recovery.code
        self.claims_challenge = recovery.claims_challenge


class HostedAgentHTTPError(HostedAgentError):
    """Raised when the hosted endpoint returns a non-success status."""

    def __init__(self, status_code: int, *, claims_challenge: object = None):
        self.status_code = status_code
        self.claims_challenge = validated_claims_challenge(claims_challenge)
        self.code = (
            "interaction_required" if self.claims_challenge is not None
            else "authentication_required" if status_code == 401
            else "source_denied" if status_code == 403
            else "hosted_http_error"
        )
        super().__init__(f"Hosted agent returned HTTP {status_code}.")


class HostedAgentProtocolError(HostedAgentError):
    """Raised when the hosted endpoint violates the frozen wire contract."""


class HostedAgentResponseError(HostedAgentError):
    """Raised for a terminal Responses ``error`` frame."""

    def __init__(
        self, message: str, *, code: str = "", retryable: bool = False,
        claims_challenge: object = None,
    ):
        super().__init__(message)
        self.code = code if code in {
            "authentication_required", "interaction_required", "consent_required",
            "source_denied", "configuration_error", "authentication_unavailable",
            "tool_failed", "retrieval_failed", "retrieval_required",
            "missing_delegation", "invalid_delegation",
        } else "hosted_error"
        self.retryable = retryable
        self.claims_challenge = validated_claims_challenge(claims_challenge)


class HostedAgentCancelledError(HostedAgentError):
    """Raised for a terminal ``response.cancelled`` frame."""


class AsyncCredential(Protocol):
    async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class HostedAgentSettings:
    base_url: str
    resource_scope: str
    idle_timeout_seconds: float = DEFAULT_SSE_IDLE_TIMEOUT_SECONDS
    auth_mode: HostedAgentAuthMode = "user_delegated"

    @property
    def invocations_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/invocations"


@dataclass(frozen=True)
class InvocationMessage:
    role: Literal["user", "assistant"]
    content: str

    def to_payload(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class HostedAgentEvent:
    event_type: str
    data: dict[str, Any] = field(default_factory=dict)


def _get_config_value(key: str, default: str | None = None) -> str | None:
    value = os.getenv(key)
    if value is not None:
        return value
    try:
        return config.get(key, default, str)
    except ConfigurationError:
        logger.debug("Configuration key '%s' not found", key)
        return default


def _validate_base_url(value: str | None) -> str:
    base_url = (value or "").strip().rstrip("/")
    parsed = urlparse(base_url)
    if not base_url:
        raise HostedAgentConfigError(
            "CHAT_BACKEND is 'hosted_agent' but HOSTED_AGENT_BASE_URL is not configured."
        )
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HostedAgentConfigError(
            "HOSTED_AGENT_BASE_URL must be an absolute HTTP(S) URL."
        )
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise HostedAgentConfigError(
            "HOSTED_AGENT_BASE_URL must use HTTPS except for local development."
        )
    return base_url


def _validate_resource_scope(value: str | None) -> str:
    scope = (value or "").strip()
    if not scope:
        raise HostedAgentConfigError(
            "CHAT_BACKEND is 'hosted_agent' but HOSTED_AGENT_RESOURCE_SCOPE is not configured. "
            "Set it to the deployed hosted-agent data-plane scope ending in '/.default'."
        )
    if "management.azure.com" in scope.lower():
        raise HostedAgentConfigError(
            "HOSTED_AGENT_RESOURCE_SCOPE must be the hosted-agent data-plane scope, not Azure ARM."
        )
    if not scope.endswith("/.default"):
        raise HostedAgentConfigError(
            "HOSTED_AGENT_RESOURCE_SCOPE must be an explicit scope ending in '/.default'."
        )
    return scope


def _validate_idle_timeout(value: str | None) -> float:
    try:
        timeout = float(value or DEFAULT_SSE_IDLE_TIMEOUT_SECONDS)
    except (TypeError, ValueError) as exc:
        raise HostedAgentConfigError(
            "HOSTED_AGENT_SSE_IDLE_TIMEOUT_SECONDS must be a positive number."
        ) from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise HostedAgentConfigError(
            "HOSTED_AGENT_SSE_IDLE_TIMEOUT_SECONDS must be a finite positive number."
        )
    return timeout


def _validate_auth_mode(value: str | None) -> HostedAgentAuthMode:
    mode = (value or "user_delegated").strip().lower()
    if mode not in _VALID_AUTH_MODES:
        raise HostedAgentConfigError(
            "HOSTED_AGENT_AUTH_MODE must be 'user_delegated' (default; required for "
            "per-user Toolbox identity passthrough per ADR-0001) or the explicit "
            f"opt-out 'service_identity'. Got {value!r}."
        )
    return mode  # type: ignore[return-value]


def load_hosted_agent_settings() -> HostedAgentSettings:
    return HostedAgentSettings(
        base_url=_validate_base_url(_get_config_value("HOSTED_AGENT_BASE_URL")),
        resource_scope=_validate_resource_scope(
            _get_config_value("HOSTED_AGENT_RESOURCE_SCOPE")
        ),
        idle_timeout_seconds=_validate_idle_timeout(
            _get_config_value(
                "HOSTED_AGENT_SSE_IDLE_TIMEOUT_SECONDS",
                str(DEFAULT_SSE_IDLE_TIMEOUT_SECONDS),
            )
        ),
        auth_mode=_validate_auth_mode(
            _get_config_value("HOSTED_AGENT_AUTH_MODE", "user_delegated")
        ),
    )


def validate_hosted_agent_config() -> None:
    """Fail startup when hosted mode lacks an endpoint or data-plane scope.

    For the default ``user_delegated`` auth mode this also validates that the
    OAuth confidential-client configuration required for the on-behalf-of
    exchange (``OAUTH_AZURE_AD_CLIENT_ID`` / ``_CLIENT_SECRET`` / ``_TENANT_ID``)
    is present, so a misconfigured deployment fails fast at startup rather
    than on the first user request.
    """
    settings = load_hosted_agent_settings()
    _validate_retrieval_auth_mode()
    _validate_retrieval_transport(settings)
    _work_iq_app_config()
    if settings.auth_mode == "user_delegated":
        _resolve_confidential_client_config()


def _validate_retrieval_auth_mode() -> None:
    mode = (_get_config_value("RETRIEVAL_AUTH_MODE", "native_search") or "native_search").strip()
    if mode != "native_search":
        raise HostedAgentConfigError(
            "RETRIEVAL_AUTH_MODE must be 'native_search'. "
            "Other protected retrieval modes are not supported."
        )


def _validate_retrieval_transport(settings: HostedAgentSettings) -> None:
    # Retain the explicit legacy service-only transport for unconfigured
    # protocol callers. It cannot participate in configured native retrieval.
    if settings.auth_mode != "user_delegated" and _get_config_value("RETRIEVAL_AUTH_MODE") is not None:
        raise HostedAgentConfigError(
            "Native protected retrieval requires HOSTED_AGENT_AUTH_MODE=user_delegated."
        )


def _credential_header(value: object) -> str:
    if (
        not isinstance(value, str) or not value.strip()
        or len(value.encode("utf-8")) > 8192
        or any(ord(char) < 33 or ord(char) > 126 for char in value)
    ):
        raise HostedAgentAuthenticationError("A delegated credential is missing or invalid.")
    return value


# Retain the former owner export for compatibility; parsing belongs to auth.
_http_claims_challenge = http_claims_challenge


def _work_iq_app_config() -> tuple[str, str] | None:
    enabled = (_get_config_value("WORK_IQ_ENABLED", "false") or "false").strip().lower()
    if enabled in {"false", "0", "no", "n", "off"}:
        return None
    if enabled not in {"true", "1", "yes", "y", "on"}:
        raise HostedAgentConfigError("WORK_IQ_ENABLED must be a boolean.")
    try:
        app_id = normalize_guid(
            _get_config_value("WORK_IQ_APPLICATION_ID") or "", claim_name="Work IQ app",
        )
        ui_app = normalize_guid(
            _get_config_value("OAUTH_AZURE_AD_CLIENT_ID") or "", claim_name="UI app",
        )
        ui_tenant = normalize_guid(
            _get_config_value("OAUTH_AZURE_AD_TENANT_ID") or "", claim_name="UI tenant",
        )
        tenant = normalize_guid(
            _get_config_value("WORK_IQ_TENANT_ID") or ui_tenant,
            claim_name="Work IQ tenant",
        )
    except ValueError:
        raise HostedAgentConfigError(
            "Work IQ requires WORK_IQ_APPLICATION_ID and the configured UI tenant."
        ) from None
    if app_id != ui_app or tenant != ui_tenant:
        raise HostedAgentConfigError(
            "Work IQ assertion application and tenant must match the UI API."
        )
    return tenant, app_id


def build_invocation_messages(
    history: Sequence[Mapping[str, Any]],
    current_ask: str,
) -> list[InvocationMessage]:
    """Build ordered runtime messages ending in the current user ask."""
    ask = current_ask.strip()
    if not ask:
        raise HostedAgentProtocolError("The current user message must not be empty.")

    messages: list[InvocationMessage] = []
    for index, raw_message in enumerate(history):
        role = raw_message.get("role")
        content = raw_message.get("content")
        if role not in {"user", "assistant"}:
            raise HostedAgentProtocolError(
                f"Chat history message {index} has unsupported role {role!r}."
            )
        if not isinstance(content, str):
            raise HostedAgentProtocolError(
                f"Chat history message {index} content must be text."
            )
        messages.append(InvocationMessage(role=role, content=content))

    if not (
        messages
        and messages[-1].role == "user"
        and messages[-1].content.strip() == ask
    ):
        messages.append(InvocationMessage(role="user", content=ask))
    return messages


def _parse_sse_block(raw: str) -> HostedAgentEvent | None:
    event_type = ""
    data_lines: list[str] = []
    for line in raw.splitlines():
        if not line or line.startswith(":"):
            continue
        field_name, separator, value = line.partition(":")
        if not separator:
            continue
        value = value.lstrip()
        if field_name == "event":
            event_type = value
        elif field_name == "data":
            data_lines.append(value)

    if not event_type and not data_lines:
        return None

    raw_data = "\n".join(data_lines)
    try:
        data = json.loads(raw_data) if raw_data else {}
    except json.JSONDecodeError as exc:
        raise HostedAgentProtocolError(
            "Hosted agent returned invalid JSON in an SSE event."
        ) from None
    if not isinstance(data, dict):
        raise HostedAgentProtocolError("Hosted-agent SSE data must be a JSON object.")

    resolved_type = event_type or str(data.get("type") or "")
    if not resolved_type:
        raise HostedAgentProtocolError("Hosted-agent SSE frame is missing an event type.")
    return HostedAgentEvent(event_type=resolved_type, data=data)


async def _iter_sse_events(
    response: httpx.Response,
) -> AsyncGenerator[HostedAgentEvent, None]:
    lines: list[str] = []
    async for line in response.aiter_lines():
        if line:
            lines.append(line)
            continue
        event = _parse_sse_block("\n".join(lines))
        lines.clear()
        if event is not None:
            yield event

    if lines:
        event = _parse_sse_block("\n".join(lines))
        if event is not None:
            yield event


def _default_credential() -> AsyncCredential:
    client_id = (os.getenv("AZURE_CLIENT_ID") or "").strip() or None
    return ChainedTokenCredential(
        ManagedIdentityCredential(client_id=client_id),
        AzureCliCredential(),
    )


def _resolve_confidential_client_config() -> tuple[str, str, str]:
    """Return (client_id, client_secret, tenant_id) for the OBO exchange.

    Reuses the same OAUTH_AZURE_AD_* configuration as the Chainlit OAuth login
    (see auth_oauth.py), since the signed-in user's access token was issued to
    that same app registration and is therefore the correct assertion for an
    on-behalf-of exchange against the hosted-agent data-plane scope.
    """
    client_id = (_get_config_value("OAUTH_AZURE_AD_CLIENT_ID") or "").strip()
    client_secret = (_get_config_value("OAUTH_AZURE_AD_CLIENT_SECRET") or "").strip()
    tenant_id = (_get_config_value("OAUTH_AZURE_AD_TENANT_ID") or "").strip()
    if not client_id or not client_secret or not tenant_id:
        raise HostedAgentConfigError(
            "Hosted-agent user-delegated auth (HOSTED_AGENT_AUTH_MODE=user_delegated, "
            "the default) requires OAUTH_AZURE_AD_CLIENT_ID, OAUTH_AZURE_AD_CLIENT_SECRET, "
            "and OAUTH_AZURE_AD_TENANT_ID to perform the on-behalf-of token exchange."
        )
    return client_id, client_secret, tenant_id


async def _acquire_obo_token(user_access_token: str, resource_scope: str) -> str:
    """Exchange the signed-in user's token for a hosted-agent data-plane token.

    Uses the OAuth2 On-Behalf-Of flow (MSAL) so Microsoft Foundry's gateway
    resolves the *end user*, not this service, as the caller identity when
    used for Foundry. Other resource scopes acquire separate delegated tokens
    for protected retrieval. A fresh confidential client is created for
    each call (matching the pattern in auth_oauth.py); nothing is cached and
    the token value is never logged.
    """
    client_id, client_secret, tenant_id = _resolve_confidential_client_config()
    authority = f"https://login.microsoftonline.com/{tenant_id}"
    msal_app = msal.ConfidentialClientApplication(
        client_id,
        authority=authority,
        client_credential=client_secret,
        timeout=10,
    )

    def _run_obo() -> dict[str, Any]:
        return msal_app.acquire_token_on_behalf_of(
            user_assertion=user_access_token,
            scopes=[resource_scope],
        )

    result = await asyncio.to_thread(_run_obo)
    if "error" in result:
        recovery = DelegationError.from_msal(result)
        logger.warning(
            "Hosted-agent on-behalf-of token exchange failed: reason=%s",
            recovery.code,
        )
        raise HostedAgentAuthenticationError(
            str(recovery), code=recovery.code, claims_challenge=recovery.claims_challenge,
        ) from None
    delegated_token = result.get("access_token")
    if not delegated_token:
        raise HostedAgentAuthenticationError(
            "The on-behalf-of exchange did not return a hosted-agent access token."
        )
    return _credential_header(delegated_token)


async def acquire_obo_token(user_access_token: str, resource_scope: str) -> str:
    """Public wrapper reusing the OBO exchange for other BFF-owned
    data-plane calls that also require the signed-in user's own delegated
    identity (for example the hosted-continuity Conversations system-of-record
    in ``hosted_conversation_store.py`` when
    ``HOSTED_CONVERSATION_OWNER_BINDING=capability``). Kept as a thin wrapper
    so the MSAL on-behalf-of logic and its confidential-client configuration
    validation stay defined in exactly one place.

    This is a distinct trust model from ``acquire_service_identity_token``
    below: this function asserts the *signed-in user's own* delegated
    identity to Foundry (required for ADR-0001 Toolbox passthrough), never a
    trusted-middle-tier identity asserting another user's oid via a header.
    Do not conflate the two.
    """
    return await _acquire_obo_token(user_access_token, resource_scope)


async def acquire_service_identity_token(resource_scope: str) -> str:
    """Acquire this service's own managed-identity/Azure CLI credential token
    for the *trusted middle-tier* delegated-header owner-binding model
    (``HOSTED_CONVERSATION_OWNER_BINDING=delegated``, see
    ``hosted_conversation_store.py``): the BFF authenticates as itself and
    separately asserts the validated end user's oid via the platform's
    ``x-ms-user-identity`` header, rather than exchanging the user's own
    token via on-behalf-of.

    This reuses the exact same credential chain as
    ``HOSTED_AGENT_AUTH_MODE=service_identity`` (``_default_credential``), but
    is intentionally a separate function from ``acquire_obo_token``: the two
    represent different trust models and must never be conflated. A fresh
    credential is created and closed for each call (no caching), matching the
    OBO helper's pattern; nothing about the returned token is ever logged.
    """
    credential = _default_credential()
    try:
        try:
            token = await credential.get_token(resource_scope)
        except AzureError as exc:
            raise HostedAgentAuthenticationError(
                "Unable to acquire a service-identity data-plane token for "
                "the hosted-continuity delegated owner-binding path."
            ) from exc
    finally:
        await credential.close()
    if not token.token:
        raise HostedAgentAuthenticationError(
            "The service-identity credential returned an empty access token."
        )
    return token.token


class HostedAgentClient:
    """Reusable authenticated HTTP/SSE client for one hosted endpoint."""

    def __init__(
        self,
        settings: HostedAgentSettings,
        *,
        credential: AsyncCredential | None = None,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.settings = settings
        self._assertion_validator: EntraTokenValidator | None = None
        self._credential = credential
        self._owns_credential = False
        if self.settings.auth_mode == "service_identity" and self._credential is None:
            self._credential = _default_credential()
            self._owns_credential = True
        self._http_client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=settings.idle_timeout_seconds,
                write=30.0,
                pool=10.0,
            ),
            follow_redirects=False,
        )
        self._owns_http_client = http_client is None

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()
        if self._owns_credential and self._credential is not None:
            await self._credential.close()

    async def _acquire_data_plane_token(
        self, user_access_token: str | None
    ) -> AccessToken:
        """Resolve the bearer token to send as Authorization on /invocations.

        Fail-closed by design: the default ("user_delegated") mode never falls
        back to a service identity when a user token is missing or invalid.
        Only an explicit HOSTED_AGENT_AUTH_MODE=service_identity configuration
        uses the server's own managed-identity/Azure CLI credential.
        """
        if self.settings.auth_mode == "service_identity":
            logger.warning(
                "Hosted-agent auth_mode=service_identity is active: Microsoft "
                "Foundry will resolve this service, not the signed-in user, as "
                "the caller. Per ADR-0001 this must not be the default release "
                "path and should be scoped to explicit, reviewed exceptions."
            )
            if self._credential is None:
                raise HostedAgentAuthenticationError(
                    "Hosted-agent service identity credential is not configured."
                )
            try:
                token = await self._credential.get_token(
                    self.settings.resource_scope
                )
            except AzureError as exc:
                raise HostedAgentAuthenticationError(
                    "Unable to acquire a hosted-agent data-plane token."
                ) from exc
            if not token.token:
                raise HostedAgentAuthenticationError(
                    "The hosted-agent credential returned an empty access token."
                )
            return token

        stripped_user_token = (user_access_token or "").strip()
        if not stripped_user_token:
            raise HostedAgentAuthenticationError(
                "A signed-in user access token is required to call the hosted "
                "agent (HOSTED_AGENT_AUTH_MODE=user_delegated, the default). "
                "Refusing to fall back to a service identity."
            )
        delegated_token = await _acquire_obo_token(
            stripped_user_token, self.settings.resource_scope
        )
        return AccessToken(delegated_token, 0)

    async def _work_iq_assertion(self, assertion: str) -> str | None:
        app = _work_iq_app_config()
        if app is None:
            return None
        tenant_id, app_id = app
        validator = self._assertion_validator
        if validator is None or validator.tenant_id != tenant_id or validator.audience != app_id:
            validator = EntraTokenValidator(
                tenant_id=tenant_id, audience=app_id, required_scope="access_as_user",
            )
            self._assertion_validator = validator
        try:
            await validator.validate(assertion)
        except EntraTokenError:
            raise HostedAgentAuthenticationError(
                "Work IQ requires a valid UI API user assertion with access_as_user.",
                code="authentication_required",
            ) from None
        except httpx.HTTPError:
            raise HostedAgentAuthenticationError(
                "The user assertion could not be verified.",
                code="authentication_unavailable",
            ) from None
        return assertion

    async def stream(
        self,
        messages: Sequence[InvocationMessage],
        *,
        conversation_id: str = "",
        question_id: str | None = None,
        correlation_id: str | None = None,
        user_access_token: str | None = None,
    ) -> AsyncGenerator[tuple[str, dict[str, Any]], None]:
        if not messages or messages[-1].role != "user" or not messages[-1].content.strip():
            raise HostedAgentProtocolError(
                "Invocation messages must end in a non-empty user message."
            )

        _validate_retrieval_auth_mode()
        _validate_retrieval_transport(self.settings)
        forwarded_headers: dict[str, str] = {}
        if self.settings.auth_mode != "user_delegated" and _work_iq_app_config() is not None:
            raise HostedAgentConfigError("Work IQ requires user-delegated hosted authentication.")
        if self.settings.auth_mode == "user_delegated":
            # The original API assertion is used independently for each audience.
            # Neither token is an OBO assertion for the other.
            assertion = _credential_header((user_access_token or "").strip())
            work_iq_assertion = await self._work_iq_assertion(assertion)
            if work_iq_assertion is not None:
                forwarded_headers["x-client-work-iq-assertion"] = work_iq_assertion
        access_token = await self._acquire_data_plane_token(user_access_token)
        transport_token = _credential_header(access_token.token)
        if self.settings.auth_mode == "user_delegated":
            forwarded_headers["x-client-search-token"] = _credential_header(
                await _acquire_obo_token(assertion, SEARCH_USER_SCOPE)
            )

        metadata: dict[str, str] = {}
        if question_id:
            metadata["question_id"] = question_id
        if correlation_id:
            metadata["correlation_id"] = correlation_id

        payload: dict[str, Any] = {
            "messages": [message.to_payload() for message in messages],
            "metadata": metadata,
        }
        managed_conversation_id = conversation_id.strip()
        if not managed_conversation_id and self.settings.auth_mode == "user_delegated":
            managed_conversation_id = str(uuid.uuid4())
        if managed_conversation_id:
            payload["conversation_id"] = managed_conversation_id

        headers = {
            "Authorization": f"Bearer {transport_token}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            **forwarded_headers,
        }
        if sum(len(value.encode("utf-8")) for value in headers.values()) > 16384:
            raise HostedAgentAuthenticationError("Delegated credentials exceed the request header limit.")
        logger.info(
            "Invoking hosted agent: conversation_id=%s question_id=%s url=%s",
            managed_conversation_id or "new",
            question_id or "n/a",
            self.settings.invocations_url,
        )

        terminal_event_seen = False
        async with self._http_client.stream(
            "POST",
            self.settings.invocations_url,
            params={"api-version": "v1"},
            json=payload,
            headers=headers,
        ) as response:
            if not 200 <= response.status_code < 300:
                # Provider bodies can contain credentials, excerpts or internal
                # diagnostics. Do not read them into errors or telemetry.
                raise HostedAgentHTTPError(
                    response.status_code,
                    claims_challenge=(
                        http_claims_challenge(response)
                        if response.status_code in {401, 403} else None
                    ),
                )
            if "text/event-stream" not in response.headers.get("content-type", "").lower():
                raise HostedAgentProtocolError(
                    "Hosted agent response must use content type text/event-stream."
                )

            async for event in _iter_sse_events(response):
                event_type = event.event_type
                data = event.data

                if event_type == "response.created":
                    response_data = data.get("response")
                    if not isinstance(response_data, dict):
                        raise HostedAgentProtocolError(
                            "response.created is missing its response object."
                        )
                    new_conversation_id = response_data.get("conversation_id")
                    conversation = response_data.get("conversation")
                    if conversation is not None:
                        if not isinstance(conversation, dict):
                            raise HostedAgentProtocolError(
                                "response.created has an invalid conversation object."
                            )
                        nested_id = conversation.get("id")
                        if new_conversation_id is not None and new_conversation_id != nested_id:
                            raise HostedAgentProtocolError(
                                "response.created has conflicting conversation identifiers."
                            )
                        new_conversation_id = nested_id
                    if not isinstance(new_conversation_id, str) or not new_conversation_id.strip():
                        raise HostedAgentProtocolError(
                            "response.created is missing a conversation identifier."
                        )
                    managed_conversation_id = new_conversation_id
                    yield "", {"conversation_id": new_conversation_id}
                elif event_type == "response.output_text.delta":
                    delta = data.get("delta")
                    if not isinstance(delta, str):
                        raise HostedAgentProtocolError(
                            "response.output_text.delta is missing text in data.delta."
                        )
                    if delta:
                        yield delta, {}
                elif event_type == "response.output_text.annotation.added":
                    annotation = data.get("annotation")
                    if not isinstance(annotation, dict):
                        raise HostedAgentProtocolError(
                            "Citation frame is missing data.annotation."
                        )
                    yield "", {"citation": annotation}
                elif event_type in {
                    "response.function_call_arguments.delta",
                    "response.function_call_arguments.done",
                }:
                    yield "", {
                        "tool_activity": {
                            "event_type": event_type,
                            "call_id": data.get("call_id", ""),
                            "name": data.get("name", ""),
                            "status": data.get("status", ""),
                            "message": data.get("message", ""),
                        }
                    }
                elif event_type == "response.completed":
                    terminal_event_seen = True
                    yield "", {
                        "completed": True,
                        "conversation_id": managed_conversation_id,
                    }
                    return
                elif event_type == "response.cancelled":
                    terminal_event_seen = True
                    reason = data.get("reason")
                    safe_reason = reason if isinstance(reason, str) and reason in {
                        "client_cancelled", "timeout", "shutdown",
                    } else "cancelled"
                    raise HostedAgentCancelledError(f"Hosted agent {safe_reason}.")
                elif event_type in {"error", "response.failed"}:
                    terminal_event_seen = True
                    if event_type == "response.failed":
                        failed_response = data.get("response")
                        failed_error = failed_response.get("error") if isinstance(failed_response, dict) else None
                        data = failed_error if isinstance(failed_error, dict) else {}
                    raise HostedAgentResponseError(
                        "The hosted agent could not complete this request.",
                        code=str(data.get("code") or ""),
                        retryable=bool(data.get("retryable", False)),
                        claims_challenge=data.get("claims_challenge"),
                    )

        if not terminal_event_seen:
            raise HostedAgentProtocolError(
                "Hosted-agent SSE stream ended without a terminal response frame."
            )


_default_client: HostedAgentClient | None = None
_default_client_lock = asyncio.Lock()


async def _get_default_client() -> HostedAgentClient:
    global _default_client
    if _default_client is None:
        async with _default_client_lock:
            if _default_client is None:
                _default_client = HostedAgentClient(load_hosted_agent_settings())
    return _default_client


async def close_hosted_agent_client() -> None:
    global _default_client
    if _default_client is not None:
        await _default_client.aclose()
        _default_client = None


async def call_hosted_agent_stream(
    messages: Sequence[InvocationMessage],
    *,
    conversation_id: str = "",
    question_id: str | None = None,
    correlation_id: str | None = None,
    user_access_token: str | None = None,
    client: HostedAgentClient | None = None,
) -> AsyncGenerator[tuple[str, dict[str, Any]], None]:
    """Stream parsed Responses events without any classic-backend fallback."""
    hosted_client = client or await _get_default_client()
    async for item in hosted_client.stream(
        messages,
        conversation_id=conversation_id,
        question_id=question_id,
        correlation_id=correlation_id,
        user_access_token=user_access_token,
    ):
        yield item
