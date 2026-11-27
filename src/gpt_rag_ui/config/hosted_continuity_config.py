"""Configuration for the opt-in hosted-agent cross-version continuity path.

All settings here are inert unless ``HOSTED_CONTINUITY_ENABLED=true``. While
the flag is false (the default), the existing hosted-agent behavior — a
per-turn managed conversation id round-tripped by the hosted runtime itself
and full history resent from the Chainlit chat context, see
``hosted_agent_client.py`` and ``app.py`` — is completely unchanged, and none
of the other settings below are evaluated.

When enabled, this UI (the BFF) becomes the exclusive owner of the Foundry
managed Conversation used for continuity. It supports two owner-binding
modes, chosen with ``HOSTED_CONVERSATION_OWNER_BINDING``:

* ``delegated`` (preferred/default) — the BFF authenticates to the
  Conversations system-of-record as itself (a trusted middle tier) and
  attaches a platform-trusted ``x-ms-user-identity`` header derived only
  from the caller's validated Entra ``oid`` (never from client input) on
  every Conversations lifecycle call. Live evidence
  (Azure/agent-landing-zone#591, "OQ-OWN") shows Azure AI Foundry hosted agents running
  responses protocol >= 2.0.0 platform-enforce per-asserted-user ownership
  of this state when the middle-tier identity also holds the custom
  ``Microsoft.CognitiveServices/accounts/AIServices/agents/endpoints/UserIdentityImpersonation/action``
  data action at the agent scope: cross-user reads 404, calls missing the
  header from a delegated session 403. Because this depends on a specific
  deployed protocol version and RBAC grant that cannot be discovered at
  runtime, it is only used once explicitly attested via
  ``HOSTED_AGENT_PROTOCOL_VERSION`` (>= 2.0.0) and
  ``HOSTED_CONVERSATION_OWNER_BINDING_VALIDATED=true``; otherwise it fails
  closed at startup rather than silently falling back.
* ``capability`` (disabled fallback) — the BFF mints an opaque, signed
  capability bound to the caller's validated Entra ``oid`` instead of ever
  handing the raw managed conversation id back to the caller (see
  ``hosted_conversation_capability.py``). Still fully supported, but no
  longer the required primary path.

Either way, history is read/written through ``hosted_conversation_store.py``
under an explicit bounded-history policy (max items, then max tokens,
dropping the oldest first), and the ``x-ms-user-identity`` header is never
conflated with the separate on-behalf-of (OBO) token used for Toolbox
per-user document-level retrieval (ADR-0001), which remains untouched.
"""

from __future__ import annotations

import logging
import os

from gpt_rag_ui.config.errors import ConfigurationError
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import urlparse

OwnerBindingMode = Literal["capability", "delegated"]
HistoryTruncation = Literal["drop_oldest"]


class ContinuityConfig(Protocol):
    def get(self, key: str, default: str, type: type) -> str:
        """Return the configured continuity setting or its default."""

_TRUE_VALUES = {"1", "true", "yes", "y", "on"}
_FALSE_VALUES = {"0", "false", "no", "n", "off"}
_LOCAL_HTTP_HOSTS = {"localhost", "127.0.0.1", "::1"}
_MIN_CAPABILITY_KEY_LENGTH = 32


class HostedContinuityConfigError(ValueError):
    """Raised when hosted-agent continuity configuration is missing or invalid."""


_MIN_DELEGATED_PROTOCOL_VERSION = (2, 0, 0)


@dataclass(frozen=True)
class HostedContinuitySettings:
    enabled: bool = False
    owner_binding: OwnerBindingMode = "capability"
    owner_binding_validated: bool = False
    protocol_version: str = ""
    capability_key: str = ""
    capability_key_id: str = ""
    capability_ttl_seconds: int = 900
    history_max_items: int = 40
    history_max_tokens: int = 8000
    history_truncation: HistoryTruncation = "drop_oldest"
    store_base_url: str = ""
    store_resource_scope: str = ""

    @property
    def uses_capability_binding(self) -> bool:
        return self.enabled and self.owner_binding == "capability"

    @property
    def uses_delegated_binding(self) -> bool:
        """True only once the delegated owner-binding mode has been
        selected, explicitly marked validated, *and* the attested hosted
        agent responses protocol version meets the minimum this behavior was
        confirmed under (see Azure/agent-landing-zone#591, "OQ-OWN"); otherwise it stays
        inert even if ``HOSTED_CONVERSATION_OWNER_BINDING=delegated`` is set
        (this can only happen transiently since
        ``load_hosted_continuity_settings`` rejects that combination
        outright when continuity is enabled — this property is defensive for
        settings constructed directly, e.g. in tests)."""
        if not (
            self.enabled
            and self.owner_binding == "delegated"
            and self.owner_binding_validated
        ):
            return False
        return _meets_minimum_protocol_version(self.protocol_version)


def _read_setting(
    config: ContinuityConfig,
    environ: Mapping[str, str],
    key: str,
    default: str = "",
) -> str:
    env_value = environ.get(key)
    if env_value is not None and str(env_value).strip():
        return str(env_value).strip()
    try:
        value = config.get(key, default, str)
    except ConfigurationError:
        logging.getLogger(__name__).warning("Configuration key '%s' is unavailable; using default", key)
        return default
    return str(value or default).strip()


def _parse_bool(value: str, *, key: str, default: bool) -> bool:
    if not value:
        return default
    normalized = value.lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise HostedContinuityConfigError(
        f"{key} must be one of true/false, 1/0, yes/no, or on/off."
    )


def _parse_bounded_int(
    value: str,
    *,
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise HostedContinuityConfigError(f"{key} must be an integer.") from exc
    if not minimum <= parsed <= maximum:
        raise HostedContinuityConfigError(
            f"{key} must be between {minimum} and {maximum}."
        )
    return parsed


def _parse_protocol_version(value: str) -> tuple[int, int, int]:
    parts = value.strip().split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError(f"Invalid protocol version: {value!r}")
    major, minor, patch = (int(part) for part in parts)
    return (major, minor, patch)


def _meets_minimum_protocol_version(
    value: str, minimum: tuple[int, int, int] = _MIN_DELEGATED_PROTOCOL_VERSION
) -> bool:
    try:
        parsed = _parse_protocol_version(value)
    except ValueError:
        return False
    return parsed >= minimum


def _validate_store_base_url(value: str) -> str:
    base_url = value.rstrip("/")
    if not base_url:
        raise HostedContinuityConfigError(
            "HOSTED_CONTINUITY_ENABLED is true but "
            "HOSTED_CONVERSATION_STORE_BASE_URL is not configured."
        )
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HostedContinuityConfigError(
            "HOSTED_CONVERSATION_STORE_BASE_URL must be an absolute HTTP(S) URL."
        )
    if parsed.scheme != "https" and parsed.hostname not in _LOCAL_HTTP_HOSTS:
        raise HostedContinuityConfigError(
            "HOSTED_CONVERSATION_STORE_BASE_URL must use HTTPS except for "
            "local development."
        )
    return base_url


def _validate_store_resource_scope(value: str) -> str:
    scope = value.strip()
    if not scope:
        raise HostedContinuityConfigError(
            "HOSTED_CONTINUITY_ENABLED is true but "
            "HOSTED_CONVERSATION_STORE_RESOURCE_SCOPE (or HOSTED_AGENT_RESOURCE_SCOPE "
            "as a fallback) is not configured."
        )
    if urlparse(scope).hostname == "management.azure.com":
        raise HostedContinuityConfigError(
            "HOSTED_CONVERSATION_STORE_RESOURCE_SCOPE must be a data-plane scope, "
            "not Azure ARM."
        )
    if not scope.endswith("/.default"):
        raise HostedContinuityConfigError(
            "HOSTED_CONVERSATION_STORE_RESOURCE_SCOPE must be an explicit scope "
            "ending in '/.default'."
        )
    return scope


def load_hosted_continuity_settings(
    config: ContinuityConfig,
    environ: Mapping[str, str] | None = None,
) -> HostedContinuitySettings:
    """Load and validate the opt-in hosted continuity settings.

    Settings other than the enable flag are intentionally ignored while the
    feature is disabled, matching the pattern used by ``embed_config.py``, so
    existing hosted-agent deployments keep their current behavior with zero
    additional required configuration.
    """
    environ = environ if environ is not None else os.environ
    enabled = _parse_bool(
        _read_setting(config, environ, "HOSTED_CONTINUITY_ENABLED"),
        key="HOSTED_CONTINUITY_ENABLED",
        default=False,
    )
    if not enabled:
        return HostedContinuitySettings()

    owner_binding = _read_setting(
        config, environ, "HOSTED_CONVERSATION_OWNER_BINDING", "delegated"
    ).lower()
    if owner_binding != "capability" and owner_binding != "delegated":
        raise HostedContinuityConfigError(
            "HOSTED_CONVERSATION_OWNER_BINDING must be 'delegated' (preferred "
            "default) or 'capability' (disabled fallback)."
        )

    owner_binding_validated = _parse_bool(
        _read_setting(config, environ, "HOSTED_CONVERSATION_OWNER_BINDING_VALIDATED"),
        key="HOSTED_CONVERSATION_OWNER_BINDING_VALIDATED",
        default=False,
    )
    protocol_version = ""
    if owner_binding == "delegated":
        # Fails closed (the operational equivalent of a 503: the process
        # will not come up in this mode) unless the operator has explicitly
        # attested BOTH gates live evidence (Azure/agent-landing-zone#591, "OQ-OWN")
        # showed are required for the platform to enforce per-user ownership
        # of this managed conversation state: the deployed hosted agent runs
        # responses protocol >= 2.0.0, and the middle-tier identity has been
        # reviewed and granted the custom UserIdentityImpersonation data
        # action at the agent scope. Neither can be discovered at runtime,
        # so both must be operator-attested config, never inferred.
        if not owner_binding_validated:
            raise HostedContinuityConfigError(
                "HOSTED_CONVERSATION_OWNER_BINDING=delegated is inert until "
                "HOSTED_CONVERSATION_OWNER_BINDING_VALIDATED=true is "
                "explicitly set, confirming the middle-tier identity has "
                "been granted the custom "
                "Microsoft.CognitiveServices/accounts/AIServices/agents/"
                "endpoints/UserIdentityImpersonation/action data action at "
                "the agent scope. Set HOSTED_CONVERSATION_OWNER_BINDING="
                "capability to use the disabled fallback until validated."
            )
        protocol_version = _read_setting(config, environ, "HOSTED_AGENT_PROTOCOL_VERSION")
        if not protocol_version:
            raise HostedContinuityConfigError(
                "HOSTED_CONVERSATION_OWNER_BINDING=delegated requires "
                "HOSTED_AGENT_PROTOCOL_VERSION to be set to the deployed "
                "hosted agent's responses protocol version."
            )
        if not _meets_minimum_protocol_version(protocol_version):
            raise HostedContinuityConfigError(
                "HOSTED_CONVERSATION_OWNER_BINDING=delegated requires "
                "HOSTED_AGENT_PROTOCOL_VERSION >= "
                f"{'.'.join(str(part) for part in _MIN_DELEGATED_PROTOCOL_VERSION)} "
                "as a MAJOR.MINOR.PATCH string (per-user response/session "
                "ownership enforcement was only confirmed live at or above "
                "that version; see Azure/agent-landing-zone#591)."
            )

    capability_key = _read_setting(config, environ, "HOSTED_CONVERSATION_CAPABILITY_KEY")
    capability_key_id = _read_setting(
        config, environ, "HOSTED_CONVERSATION_CAPABILITY_KEY_ID"
    )
    if owner_binding == "capability":
        if not capability_key or len(capability_key) < _MIN_CAPABILITY_KEY_LENGTH:
            raise HostedContinuityConfigError(
                "HOSTED_CONVERSATION_CAPABILITY_KEY is required (at least "
                f"{_MIN_CAPABILITY_KEY_LENGTH} characters) when "
                "HOSTED_CONVERSATION_OWNER_BINDING=capability. Provide it via a "
                "Key Vault reference, never a literal secret."
            )
        if not capability_key_id:
            raise HostedContinuityConfigError(
                "HOSTED_CONVERSATION_CAPABILITY_KEY_ID is required when "
                "HOSTED_CONVERSATION_OWNER_BINDING=capability, so retired keys "
                "can be rejected after rotation."
            )

    capability_ttl_seconds = _parse_bounded_int(
        _read_setting(config, environ, "HOSTED_CONVERSATION_CAPABILITY_TTL_SECONDS"),
        key="HOSTED_CONVERSATION_CAPABILITY_TTL_SECONDS",
        default=900,
        minimum=60,
        maximum=86400,
    )
    history_max_items = _parse_bounded_int(
        _read_setting(config, environ, "HOSTED_HISTORY_MAX_ITEMS"),
        key="HOSTED_HISTORY_MAX_ITEMS",
        default=40,
        minimum=1,
        maximum=500,
    )
    history_max_tokens = _parse_bounded_int(
        _read_setting(config, environ, "HOSTED_HISTORY_MAX_TOKENS"),
        key="HOSTED_HISTORY_MAX_TOKENS",
        default=8000,
        minimum=256,
        maximum=200_000,
    )
    history_truncation = _read_setting(
        config, environ, "HOSTED_HISTORY_TRUNCATION", "drop_oldest"
    ).lower()
    if history_truncation != "drop_oldest":
        raise HostedContinuityConfigError(
            "HOSTED_HISTORY_TRUNCATION only supports 'drop_oldest'."
        )

    store_base_url = _validate_store_base_url(
        _read_setting(config, environ, "HOSTED_CONVERSATION_STORE_BASE_URL")
    )
    store_resource_scope = _validate_store_resource_scope(
        _read_setting(config, environ, "HOSTED_CONVERSATION_STORE_RESOURCE_SCOPE")
        or _read_setting(config, environ, "HOSTED_AGENT_RESOURCE_SCOPE")
    )

    return HostedContinuitySettings(
        enabled=True,
        owner_binding="delegated" if owner_binding == "delegated" else "capability",
        owner_binding_validated=owner_binding_validated,
        protocol_version=protocol_version,
        capability_key=capability_key,
        capability_key_id=capability_key_id,
        capability_ttl_seconds=capability_ttl_seconds,
        history_max_items=history_max_items,
        history_max_tokens=history_max_tokens,
        history_truncation="drop_oldest",
        store_base_url=store_base_url,
        store_resource_scope=store_resource_scope,
    )
