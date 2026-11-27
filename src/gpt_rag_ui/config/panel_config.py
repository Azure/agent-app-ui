"""Configuration for the optional hosted-agent administrative panel's
user-facing surfaces (issue #611, ADR-0004 in the platform repository).

Every setting here is inert and safe-by-default:

* ``DEPLOY_ADMINISTRATIVE_PANEL`` (default ``false``) — no panel Cosmos
  metadata containers are provisioned or read/written, and the panel routes
  always answer ``503``.
* ``PANEL_HISTORY_ENABLED`` (default ``false``) — the user-facing history,
  feedback, and deletion endpoints stay disabled (``503``) even when the
  panel is deployed, mirroring the ``POST /retrieve`` fail-closed idiom.
* ``PANEL_HISTORY_OWNER_BINDING_VALIDATED`` (default ``false``) — the
  panel's *own* environment evidence gate for switching cross-conversation
  enumeration/read from the Cosmos-backed ``owner_index`` (the pre-gate,
  always-available default) to the native ``delegated`` mechanism. Unlike
  the deploy/history gates, an unmet value here never produces an error: it
  only keeps the panel on ``owner_index``. This gate is independent of
  ADR-0003's ``HOSTED_CONVERSATION_OWNER_BINDING_VALIDATED`` continuity
  gate — neither substitutes for the other.
* ``PANEL_CONVERSATION_ENUMERATION_MODE`` (default ``owner_index``, alt
  ``delegated``) — the panel-only listing/read backend.
* ``PANEL_CONVERSATIONS_TOKEN_AUDIENCE`` — the expected ``aud`` claim on the
  delegated user bearer callers must present to every panel endpoint
  (analogous to ``HOSTED_RETRIEVAL_TOKEN_AUDIENCE`` in gpt-rag-ingestion).
  Required whenever ``PANEL_HISTORY_ENABLED=true``.
* ``PANEL_CONVERSATIONS_TENANT_ID`` — the Entra tenant panel bearers must be
  issued from; falls back to ``OAUTH_AZURE_AD_TENANT_ID`` (the tenant users
  already sign in against) when unset, so a working OAuth deployment does
  not need a second tenant id configured.

Cosmos (panel-only; never provisioned or touched unless
``DEPLOY_ADMINISTRATIVE_PANEL=true``) carries only metadata: the owner
index (``principal_id`` -> conversation id/title/timestamps) and feedback
records (rating/category/comment/message reference). It never carries
message content, citations, or any other protected conversation content.
"""

from __future__ import annotations

import logging
import os

from gpt_rag_ui.config.errors import ConfigurationError
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

EnumerationMode = Literal["owner_index", "delegated"]


class PanelConfig(Protocol):
    def get(self, key: str, default: str, type: type) -> str:
        """Return the configured panel setting or its default."""

_TRUE_VALUES = {"1", "true", "yes", "y", "on"}
_FALSE_VALUES = {"0", "false", "no", "n", "off"}

_DEFAULT_OWNER_INDEX_CONTAINER = "panel-conversation-owner-index"
_DEFAULT_FEEDBACK_CONTAINER = "panel-feedback"
_DEFAULT_CURSOR_TTL_SECONDS = 600


class PanelConfigError(ValueError):
    """Raised when administrative-panel configuration is missing or invalid."""


@dataclass(frozen=True)
class PanelSettings:
    deploy_administrative_panel: bool = False
    history_enabled: bool = False
    owner_binding_validated: bool = False
    enumeration_mode: EnumerationMode = "owner_index"
    token_audience: str = ""
    tenant_id: str = ""
    database_account_name: str = ""
    database_name: str = ""
    owner_index_container: str = _DEFAULT_OWNER_INDEX_CONTAINER
    feedback_container: str = _DEFAULT_FEEDBACK_CONTAINER
    cursor_ttl_seconds: int = _DEFAULT_CURSOR_TTL_SECONDS

    @property
    def user_surfaces_active(self) -> bool:
        """True only when every independent gate for the user-facing
        history/feedback/deletion surfaces is satisfied. This is
        deliberately distinct from ``owner_binding_validated``, which only
        selects the enumeration/read mechanism and never itself disables
        the surface (an unmet value keeps ``owner_index`` active)."""
        return self.deploy_administrative_panel and self.history_enabled

    @property
    def uses_delegated_enumeration(self) -> bool:
        """True only once the panel's own environment evidence gate
        confirms the ``delegated`` mechanism for this call path; otherwise
        the safe ``owner_index`` pre-gate/fallback stays active with no
        error surfaced for the unmet gate."""
        return (
            self.user_surfaces_active
            and self.enumeration_mode == "delegated"
            and self.owner_binding_validated
        )


def _read_setting(
    config: PanelConfig,
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
    raise PanelConfigError(
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
        raise PanelConfigError(f"{key} must be an integer.") from exc
    if not minimum <= parsed <= maximum:
        raise PanelConfigError(f"{key} must be between {minimum} and {maximum}.")
    return parsed


def load_panel_settings(
    config: PanelConfig,
    environ: Mapping[str, str] | None = None,
) -> PanelSettings:
    """Load and validate the optional administrative-panel user-surface
    settings.

    Mirrors ``hosted_continuity_config.load_hosted_continuity_settings``:
    while ``DEPLOY_ADMINISTRATIVE_PANEL`` (or ``PANEL_HISTORY_ENABLED``) is
    false, no other setting is evaluated and no Cosmos configuration is
    required, so existing deployments keep their current (panel-absent)
    behavior with zero additional required configuration.
    """
    environ = environ if environ is not None else os.environ

    deploy_administrative_panel = _parse_bool(
        _read_setting(config, environ, "DEPLOY_ADMINISTRATIVE_PANEL"),
        key="DEPLOY_ADMINISTRATIVE_PANEL",
        default=False,
    )
    history_enabled = _parse_bool(
        _read_setting(config, environ, "PANEL_HISTORY_ENABLED"),
        key="PANEL_HISTORY_ENABLED",
        default=False,
    )
    if not (deploy_administrative_panel and history_enabled):
        return PanelSettings(
            deploy_administrative_panel=deploy_administrative_panel,
            history_enabled=history_enabled,
        )

    owner_binding_validated = _parse_bool(
        _read_setting(config, environ, "PANEL_HISTORY_OWNER_BINDING_VALIDATED"),
        key="PANEL_HISTORY_OWNER_BINDING_VALIDATED",
        default=False,
    )
    enumeration_mode = _read_setting(
        config, environ, "PANEL_CONVERSATION_ENUMERATION_MODE", "owner_index"
    ).lower()
    if enumeration_mode != "owner_index" and enumeration_mode != "delegated":
        raise PanelConfigError(
            "PANEL_CONVERSATION_ENUMERATION_MODE must be 'owner_index' "
            "(default) or 'delegated'."
        )

    token_audience = _read_setting(
        config, environ, "PANEL_CONVERSATIONS_TOKEN_AUDIENCE"
    )
    if not token_audience:
        raise PanelConfigError(
            "PANEL_HISTORY_ENABLED is true but PANEL_CONVERSATIONS_TOKEN_AUDIENCE "
            "is not configured; every panel endpoint requires it to validate "
            "the caller's delegated bearer."
        )

    tenant_id = _read_setting(
        config, environ, "PANEL_CONVERSATIONS_TENANT_ID"
    ) or _read_setting(config, environ, "OAUTH_AZURE_AD_TENANT_ID")
    if not tenant_id:
        raise PanelConfigError(
            "PANEL_HISTORY_ENABLED is true but neither "
            "PANEL_CONVERSATIONS_TENANT_ID nor OAUTH_AZURE_AD_TENANT_ID is "
            "configured; the panel cannot validate the caller's tenant."
        )

    if enumeration_mode == "delegated" and not owner_binding_validated:
        raise PanelConfigError(
            "PANEL_CONVERSATION_ENUMERATION_MODE=delegated is inert until "
            "PANEL_HISTORY_OWNER_BINDING_VALIDATED=true is explicitly set. "
            "Leave PANEL_CONVERSATION_ENUMERATION_MODE=owner_index (the "
            "default) until this ADR's own evidence gate is met."
        )

    database_account_name = _read_setting(config, environ, "DATABASE_ACCOUNT_NAME")
    database_name = _read_setting(config, environ, "DATABASE_NAME")
    if not database_account_name or not database_name:
        raise PanelConfigError(
            "DEPLOY_ADMINISTRATIVE_PANEL and PANEL_HISTORY_ENABLED are true "
            "but DATABASE_ACCOUNT_NAME/DATABASE_NAME are not configured; the "
            "panel owner index and feedback metadata require Cosmos."
        )
    owner_index_container = _read_setting(
        config,
        environ,
        "PANEL_OWNER_INDEX_DATABASE_CONTAINER",
        _DEFAULT_OWNER_INDEX_CONTAINER,
    )
    feedback_container = _read_setting(
        config,
        environ,
        "PANEL_FEEDBACK_DATABASE_CONTAINER",
        _DEFAULT_FEEDBACK_CONTAINER,
    )
    cursor_ttl_seconds = _parse_bounded_int(
        _read_setting(config, environ, "PANEL_CURSOR_TTL_SECONDS"),
        key="PANEL_CURSOR_TTL_SECONDS",
        default=_DEFAULT_CURSOR_TTL_SECONDS,
        minimum=30,
        maximum=3600,
    )

    return PanelSettings(
        deploy_administrative_panel=True,
        history_enabled=True,
        owner_binding_validated=owner_binding_validated,
        enumeration_mode="delegated" if enumeration_mode == "delegated" else "owner_index",
        token_audience=token_audience,
        tenant_id=tenant_id,
        database_account_name=database_account_name,
        database_name=database_name,
        owner_index_container=owner_index_container,
        feedback_container=feedback_container,
        cursor_ttl_seconds=cursor_ttl_seconds,
    )
