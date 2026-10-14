import logging
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from azure.core.exceptions import ResourceNotFoundError
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse, StreamingResponse

from gpt_rag_ui.config.appconfig import AppConfigClient
from gpt_rag_ui.telemetry.boundary_diagnostics import log_boundary_failure
from gpt_rag_ui.clients.blob import BlobClient
from gpt_rag_ui.config.dependencies import get_config
from gpt_rag_ui.config.resources import get_asset_root
from gpt_rag_ui.config.embed_config import (
    configure_chainlit_allowed_origins,
    EmbedConfigError,
    EmbedSettings,
    load_embed_settings,
)


def _configure_logging() -> None:
    # Configure logging before importing any chatty libraries.
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    )

    # Reduce noise from chatty Azure SDK loggers so troubleshooting signals stand out.
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)


_configure_logging()
logger = logging.getLogger("gpt_rag_ui.main")
_MIN_COPILOT_AUTH_SECRET_BYTES = 32


def _mask(value: str, *, keep_end: int = 6) -> str:
    v = (value or "").strip()
    if not v:
        return "<empty>"
    if len(v) <= keep_end:
        return "<redacted>"
    return f"…{v[-keep_end:]}"


def _is_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _is_falsey(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"0", "false", "no", "n", "off"}


@dataclass(frozen=True)
class AuthState:
    oauth_configured: bool
    allow_anonymous: bool
    allow_anonymous_source: str
    allow_anonymous_raw: str
    default_allow_anonymous: bool
    running_in_azure_host: bool
    client_id_value: str
    tenant_id_value: str
    has_client_secret: bool
    client_secret_value: str


def _get_str_config(config: AppConfigClient, key: str, *fallback_keys: str) -> str:
    """Read a string config value, trying fallbacks, and normalize whitespace."""
    for k in (key, *fallback_keys):
        v = (config.get(k, "", str) or "").strip()
        if v:
            return v
    return ""


def _is_running_in_azure_host() -> bool:
    return bool(
        os.environ.get("WEBSITE_SITE_NAME")
        or os.environ.get("CONTAINER_APP_NAME")
        or os.environ.get("CONTAINER_APP_REVISION")
    )


def _clear_oauth_env_vars() -> bool:
    keys = (
        "OAUTH_AZURE_AD_CLIENT_ID",
        "OAUTH_AZURE_AD_TENANT_ID",
        "OAUTH_AZURE_AD_CLIENT_SECRET",
        "OAUTH_AZURE_AD_SCOPES",
        "OAUTH_AZURE_AD_ENABLE_SINGLE_TENANT",
    )
    cleared = False
    for k in keys:
        if k in os.environ:
            os.environ.pop(k, None)
            cleared = True
    return cleared


def _startup_banner() -> None:
    name = "Agent Landing Zone UI"
    version = _read_local_ui_version()

    banner_lines = [
        "",
        "╔══════════════════════════════════════════════╗",
        f"║  {name}{(' v' + version) if version else ''}".ljust(47) + "║",
        "║  FastAPI + Chainlit                          ║",
        "╚══════════════════════════════════════════════╝",
        "",
    ]
    for line in banner_lines:
        logger.info(line)


def _local_version_file_path() -> str:
    return str(get_asset_root() / "VERSION")


def _read_local_ui_version() -> str | None:
    try:
        version_path = _local_version_file_path()
        if os.path.exists(version_path):
            with open(version_path, "r", encoding="utf-8") as f:
                value = (f.read() or "").strip()
                return value or None
    except (OSError, UnicodeError):
        logger.exception("Failed to read local VERSION file")
    return None


def _normalize_version_prefix(value: str | None) -> str | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    if normalized.lower().startswith("v"):
        return normalized
    return f"v{normalized}"


def _format_release_value(value: str | None, missing_message: str) -> str:
    normalized = _normalize_version_prefix(value)
    if normalized:
        return normalized
    return missing_message


def _want_chainlit_spontaneous_file_upload(auth_state: AuthState) -> bool:
    """True whenever the effective allow_anonymous is false.

    Per-conversation uploads require an authenticated caller (each file is bound to the
    user's conversation), so we enable the Chainlit paperclip whenever auth is in force,
    regardless of whether ALLOW_ANONYMOUS came from env, App Config, or the default.
    """

    return not auth_state.allow_anonymous


def _sync_chainlit_spontaneous_file_upload(auth_state: AuthState) -> None:
    """Write [features.spontaneous_file_upload].enabled before `import chainlit` (Chainlit reads TOML on import)."""

    want_enabled = _want_chainlit_spontaneous_file_upload(auth_state)
    path = get_asset_root() / ".chainlit" / "config.toml"
    if not path.is_file():
        logger.warning("Chainlit config not found at %s; skipping spontaneous file upload sync", path)
        return

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.exception("Failed to read Chainlit config %s", path)
        return

    lines = text.splitlines(keepends=True)
    section = "[features.spontaneous_file_upload]"
    in_section = False
    enabled_idx: int | None = None
    current: bool | None = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped.lower() == section.lower()
            continue
        if not in_section:
            continue
        s = stripped.lower()
        if s.startswith("enabled") and "=" in s:
            val = stripped.split("=", 1)[1].strip().lower()
            if val == "true":
                current = True
            elif val == "false":
                current = False
            enabled_idx = i
            break

    if enabled_idx is None or current is None:
        logger.warning(
            "Could not find 'enabled' under %s in %s; spontaneous file upload not synced",
            section,
            path,
        )
        return

    if current is want_enabled:
        logger.info(
            "Chainlit spontaneous file upload already %s (ALLOW_ANONYMOUS source=%s allow_anonymous=%s)",
            "enabled" if want_enabled else "disabled",
            auth_state.allow_anonymous_source,
            auth_state.allow_anonymous,
        )
        return

    orig = lines[enabled_idx]
    prefix = orig[: len(orig) - len(orig.lstrip(" \t"))]
    if orig.endswith("\r\n"):
        eol = "\r\n"
    elif orig.endswith("\n"):
        eol = "\n"
    else:
        eol = "\n"
    lines[enabled_idx] = f"{prefix}enabled = {str(want_enabled).lower()}{eol}"

    try:
        path.write_text("".join(lines), encoding="utf-8")
    except OSError:
        logger.warning(
            "Could not write Chainlit config %s (read-only filesystem?); "
            "spontaneous file upload remains %s",
            path,
            "enabled" if current else "disabled",
            exc_info=True,
        )
        return

    logger.info(
        "Set Chainlit spontaneous file upload to %s (ALLOW_ANONYMOUS source=%s allow_anonymous=%s)",
        "enabled" if want_enabled else "disabled",
        auth_state.allow_anonymous_source,
        auth_state.allow_anonymous,
    )


def _configure_chainlit_prereqs(
    config: AppConfigClient,
    *,
    require_persistent_auth_secret: bool = False,
) -> None:
    """Configure values needed by Chainlit regardless of auth mode."""

    # Chainlit requires env var CHAINLIT_AUTH_SECRET to sign its session JWT.
    # Prefer storing it in App Configuration (key `CHAINLIT_AUTH_SECRET`) backed by Key Vault.
    # If missing, generate a temporary secret (sessions will be invalidated on restart).
    chainlit_secret = (
        os.environ.get("CHAINLIT_AUTH_SECRET") or ""
    ).strip()
    loaded_from_config = False
    if not chainlit_secret:
        chainlit_secret = _get_str_config(config, "CHAINLIT_AUTH_SECRET")
        loaded_from_config = bool(chainlit_secret)

    if chainlit_secret:
        if (
            require_persistent_auth_secret
            and len(chainlit_secret.encode("utf-8"))
            < _MIN_COPILOT_AUTH_SECRET_BYTES
        ):
            os.environ.pop("CHAINLIT_AUTH_SECRET", None)
            raise EmbedConfigError(
                "Chainlit Copilot requires CHAINLIT_AUTH_SECRET to contain "
                f"at least {_MIN_COPILOT_AUTH_SECRET_BYTES} UTF-8 bytes."
            )
        os.environ["CHAINLIT_AUTH_SECRET"] = chainlit_secret
        if loaded_from_config:
            logger.info(
                "Configured CHAINLIT_AUTH_SECRET from App Configuration key "
                "'CHAINLIT_AUTH_SECRET'"
            )
    elif require_persistent_auth_secret:
        os.environ.pop("CHAINLIT_AUTH_SECRET", None)
        raise EmbedConfigError(
            "Chainlit Copilot requires a persistent CHAINLIT_AUTH_SECRET. "
            "Configure one through an environment variable or a Key "
            "Vault-backed Azure App Configuration entry."
        )
    else:
        os.environ["CHAINLIT_AUTH_SECRET"] = secrets.token_urlsafe(48)
        logger.warning(
            "App Configuration key 'CHAINLIT_AUTH_SECRET' is not set; using a temporary secret. "
            "Set 'CHAINLIT_AUTH_SECRET' (ideally Key Vault-backed) to avoid session resets on restart."
        )

    # Chainlit OAuth providers (including Azure AD) require configuration via environment variables.
    # To keep everything in App Configuration (+ Key Vault references), mirror relevant keys into
    # process environment before importing Chainlit.
    chainlit_url = (os.environ.get("CHAINLIT_URL") or "").strip()
    loaded_url_from_config = False
    if not chainlit_url:
        chainlit_url = _get_str_config(config, "CHAINLIT_URL", "chainlitUrl")
        loaded_url_from_config = bool(chainlit_url)
    if chainlit_url:
        os.environ["CHAINLIT_URL"] = chainlit_url.rstrip("/")
        if loaded_url_from_config:
            logger.info("Configured CHAINLIT_URL from App Configuration")
    else:
        os.environ.pop("CHAINLIT_URL", None)


def _evaluate_auth_state(
    config: AppConfigClient,
    embed_settings: EmbedSettings | None = None,
) -> AuthState:
    """Compute auth state without importing Chainlit.

    Important: this is used to decide whether to start Chainlit or a "configuration required" app.
    """

    running_in_azure_host = _is_running_in_azure_host()

    client_id_value = (
        (os.environ.get("OAUTH_AZURE_AD_CLIENT_ID") or "").strip()
        or _get_str_config(config, "OAUTH_AZURE_AD_CLIENT_ID", "CLIENT_ID")
    )
    tenant_id_value = (
        (os.environ.get("OAUTH_AZURE_AD_TENANT_ID") or "").strip()
        or _get_str_config(config, "OAUTH_AZURE_AD_TENANT_ID")
    )
    client_secret_value = (
        (os.environ.get("OAUTH_AZURE_AD_CLIENT_SECRET") or "").strip()
        or _get_str_config(config, "OAUTH_AZURE_AD_CLIENT_SECRET", "authClientSecret")
    )

    embed_settings = embed_settings or EmbedSettings()
    oauth_configured = bool(client_id_value and tenant_id_value and client_secret_value)
    default_allow_anonymous = (
        False
        if embed_settings.enabled
        else ((not running_in_azure_host) or (not oauth_configured))
    )

    allow_anonymous_source = "default"
    allow_anonymous_raw = ""
    allow_anonymous_env = os.environ.get("ALLOW_ANONYMOUS")
    if allow_anonymous_env is not None and str(allow_anonymous_env).strip() != "":
        allow_anonymous_raw = str(allow_anonymous_env).strip()
        allow_anonymous_source = "env"
        if _is_truthy(allow_anonymous_raw):
            allow_anonymous = True
        elif _is_falsey(allow_anonymous_raw):
            allow_anonymous = False
        else:
            allow_anonymous = default_allow_anonymous
            allow_anonymous_source = "env-unrecognized"
            logger.warning(
                "ALLOW_ANONYMOUS env var value '%s' is not recognized; using default_allow_anonymous=%s",
                allow_anonymous_raw,
                default_allow_anonymous,
            )
    else:
        allow_anonymous_raw = _get_str_config(config, "ALLOW_ANONYMOUS")
        if allow_anonymous_raw:
            allow_anonymous_source = "appconfig"
            if _is_truthy(allow_anonymous_raw):
                allow_anonymous = True
            elif _is_falsey(allow_anonymous_raw):
                allow_anonymous = False
            else:
                allow_anonymous = default_allow_anonymous
                allow_anonymous_source = "appconfig-unrecognized"
                logger.warning(
                    "App Configuration key 'ALLOW_ANONYMOUS' value '%s' is not recognized; using default_allow_anonymous=%s",
                    allow_anonymous_raw,
                    default_allow_anonymous,
                )
        else:
            allow_anonymous = default_allow_anonymous

    os.environ["ALLOW_ANONYMOUS_EFFECTIVE"] = "true" if allow_anonymous else "false"
    os.environ["ALLOW_ANONYMOUS_SOURCE"] = allow_anonymous_source
    os.environ["ALLOW_ANONYMOUS_RAW"] = allow_anonymous_raw or "<unset>"

    logger.info(
        "Auth decision: running_in_azure_host=%s oauth_min_config_present=%s default_allow_anonymous=%s allow_anonymous=%s allow_anonymous_source=%s",
        running_in_azure_host,
        oauth_configured,
        default_allow_anonymous,
        allow_anonymous,
        allow_anonymous_source,
    )

    if allow_anonymous_source == "default" and running_in_azure_host:
        logger.warning(
            "ALLOW_ANONYMOUS was not provided via env var or App Configuration; using default_allow_anonymous=%s. "
            "Note: this repo loads App Configuration labels 'agent-app-ui', 'gpt-rag-ui' (legacy), 'agent-lz', and <no label>.",
            default_allow_anonymous,
        )

    return AuthState(
        oauth_configured=oauth_configured,
        allow_anonymous=allow_anonymous,
        allow_anonymous_source=allow_anonymous_source,
        allow_anonymous_raw=(allow_anonymous_raw or "<unset>"),
        default_allow_anonymous=default_allow_anonymous,
        running_in_azure_host=running_in_azure_host,
        client_id_value=client_id_value,
        tenant_id_value=tenant_id_value,
        has_client_secret=bool(client_secret_value),
        client_secret_value=client_secret_value,
    )


def _configure_auth_environment(config: AppConfigClient, auth_state: AuthState | None = None) -> None:
    """Populate env vars for Chainlit auth/OAuth.

    Must run before importing Chainlit.
    """

    _configure_chainlit_prereqs(config)

    # Azure AD OAuth provider (Chainlit built-in provider id: azure-ad)
    # Docs callback path: {CHAINLIT_URL}/auth/oauth/azure-ad/callback
    #
    # Conditional auth behavior:
    # - If minimum OAuth config (client_id + tenant_id + client_secret) is present => enable OAuth.
    # - If minimum OAuth config is missing AND ALLOW_ANONYMOUS is true => run fully anonymous and ensure
    #   no partial OAuth env vars remain (so Chainlit doesn't attempt OAuth).
    # - If minimum OAuth config is missing AND ALLOW_ANONYMOUS is false => fail fast with a clear error.

    auth_state = auth_state or _evaluate_auth_state(config)
    client_id_value = auth_state.client_id_value
    tenant_id_value = auth_state.tenant_id_value
    client_secret_value = auth_state.client_secret_value
    oauth_configured = auth_state.oauth_configured
    allow_anonymous = auth_state.allow_anonymous

    if oauth_configured:
        os.environ["OAUTH_AZURE_AD_CLIENT_ID"] = client_id_value
        os.environ["OAUTH_AZURE_AD_TENANT_ID"] = tenant_id_value
        os.environ["OAUTH_AZURE_AD_CLIENT_SECRET"] = client_secret_value

        # Scopes for Chainlit Azure AD provider.
        # Important: if this is not set, Chainlit/MSAL may fall back to Microsoft Graph defaults (e.g. User.Read),
        # which produces access_tokens with aud=00000003-... and the orchestrator correctly rejects them.
        #
        # Default to the orchestrator API scope to keep the system in "single token" mode.
        scopes_from_environment = (
            os.environ.get("OAUTH_AZURE_AD_SCOPES") or ""
        ).strip()
        if scopes_from_environment:
            os.environ["OAUTH_AZURE_AD_SCOPES"] = scopes_from_environment
        else:
            scopes_value = _get_str_config(config, "OAUTH_AZURE_AD_SCOPES")
            if str(scopes_value or "").strip():
                os.environ["OAUTH_AZURE_AD_SCOPES"] = str(scopes_value)
                logger.info("Configured OAUTH_AZURE_AD_SCOPES")
            else:
                os.environ["OAUTH_AZURE_AD_SCOPES"] = (
                    f"api://{client_id_value}/user_impersonation,openid,profile,offline_access"
                )
                logger.info("Defaulted OAUTH_AZURE_AD_SCOPES to orchestrator API scope")

        # Single-tenant toggle.
        # Default to true (most deployments use a tenant-specific app registration).
        # Allow explicitly forcing false for multi-tenant scenarios.
        enable_single_tenant = (
            os.environ.get("OAUTH_AZURE_AD_ENABLE_SINGLE_TENANT") or ""
        ).strip()
        if not enable_single_tenant:
            enable_single_tenant = _get_str_config(
                config,
                "OAUTH_AZURE_AD_ENABLE_SINGLE_TENANT",
            )
        normalized = str(enable_single_tenant or "").strip().lower()
        if normalized in {"0", "false", "no", "n", "off"}:
            os.environ["OAUTH_AZURE_AD_ENABLE_SINGLE_TENANT"] = "false"
        else:
            os.environ["OAUTH_AZURE_AD_ENABLE_SINGLE_TENANT"] = "true"
    else:
        cleared = _clear_oauth_env_vars()

        if allow_anonymous:
            logger.warning(
                "OAuth is not configured (missing client_id/tenant_id/client_secret). "
                "Running in anonymous mode (ALLOW_ANONYMOUS=true). Cleared OAuth env vars=%s",
                cleared,
            )
        else:
            logger.error(
                "OAuth is not configured (missing client_id/tenant_id/client_secret) and ALLOW_ANONYMOUS=false. "
                "Starting without Chainlit (auth-required mode)."
            )
            return

    # Safe auth config health log (no secrets).
    client_id_env = (os.environ.get("OAUTH_AZURE_AD_CLIENT_ID") or "").strip()
    tenant_id_env = (os.environ.get("OAUTH_AZURE_AD_TENANT_ID") or "").strip()
    secret_env = (os.environ.get("OAUTH_AZURE_AD_CLIENT_SECRET") or "").strip()
    chainlit_url_env = (os.environ.get("CHAINLIT_URL") or "").strip()
    logger.info(
        "OAuth config health: enabled=%s allow_anonymous=%s chainlit_url=%s client_id=%s tenant_id=%s has_client_secret=%s single_tenant=%s",
        bool(client_id_env and tenant_id_env and secret_env),
        allow_anonymous,
        (chainlit_url_env or "<unset>"),
        (_mask(client_id_env) if client_id_env else "<unset>"),
        (_mask(tenant_id_env) if tenant_id_env else "<unset>"),
        bool(secret_env),
        _is_truthy(os.environ.get("OAUTH_AZURE_AD_ENABLE_SINGLE_TENANT")),
    )


def _create_not_ready_app() -> FastAPI:
    """Return an app that clearly signals configuration is missing/unavailable."""

    @asynccontextmanager
    async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.error(
            "APPLICATION STARTED IN NOT-READY MODE: Azure App Configuration is unavailable. "
            "This instance will return HTTP 503 until configuration is fixed."
        )
        yield

    app = FastAPI(title="Agent Landing Zone UI (configuration required)", lifespan=_lifespan)

    @app.get("/")
    async def _config_required_root():
        message = (
            "Agent Landing Zone UI is not ready. Azure App Configuration is required but could not be reached.\n\n"
            "How to fix:\n"
            "- Ensure Azure CLI is installed and run: az login\n"
            "- Or set APP_CONFIG_ENDPOINT / AZURE_APPCONFIG_CONNECTION_STRING\n"
        )
        return Response(
            message,
            status_code=503,
            media_type="text/plain",
            headers={"Retry-After": "30"},
        )

    @app.get("/healthz")
    async def _healthz_config_required():
        return Response(
            "not-ready",
            status_code=503,
            media_type="text/plain",
            headers={"Retry-After": "30"},
        )

    return app


def _create_auth_required_app(auth_state: AuthState) -> FastAPI:
    """Return an app that stays up but signals OAuth configuration is required."""

    @asynccontextmanager
    async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.error(
            "APPLICATION STARTED IN AUTH-REQUIRED MODE: OAuth is required but not configured. "
            "This instance will return HTTP 503 until OAuth configuration is provided. "
            "allow_anonymous=%s source=%s",
            auth_state.allow_anonymous,
            auth_state.allow_anonymous_source,
        )
        yield

    app = FastAPI(title="Agent Landing Zone UI (authentication required)", lifespan=_lifespan)

    @app.get("/")
    async def _auth_required_root():
        message = (
            "Agent Landing Zone UI is not ready. Authentication is required, but OAuth is not configured.\n\n"
            "Required settings:\n"
            "- OAUTH_AZURE_AD_CLIENT_ID\n"
            "- OAUTH_AZURE_AD_TENANT_ID\n"
            "- OAUTH_AZURE_AD_CLIENT_SECRET\n\n"
            "Recommended setup:\n"
            "Create the keys in Azure App Configuration using label: agent-lz\n"
            "Optional: use label agent-app-ui only for UI-specific overrides.\n\n"
            "Alternative setup:\n"
            "Set the same values as container environment variables.\n"
        )
        return Response(
            message,
            status_code=503,
            media_type="text/plain",
            headers={"Retry-After": "30"},
        )

    @app.get("/healthz")
    async def _healthz_auth_required():
        return Response(
            "auth-required",
            status_code=200,
            media_type="text/plain",
            headers={"X-App-Mode": "auth-required"},
        )

    return app


def _configure_embed_environment(settings: EmbedSettings) -> None:
    os.environ["CHAINLIT_COPILOT_ENABLED_EFFECTIVE"] = (
        "true" if settings.enabled else "false"
    )
    if not settings.enabled:
        return

    os.environ["CHAINLIT_COPILOT_AUTH_MODE_EFFECTIVE"] = settings.auth_mode
    os.environ["CHAINLIT_COOKIE_SAMESITE"] = settings.cookie_samesite
    os.environ["CHAINLIT_PUBLIC_URL"] = settings.ui_origin


def _configure_copilot_user_dependency(chainlit_app) -> None:
    """Authenticate injected Copilot JWTs even when standalone OAuth is off."""

    from fastapi import Depends, HTTPException, status
    from chainlit.auth import (
        authenticate_user,
        get_current_user,
        reuseable_oauth,
    )
    from gpt_rag_ui.auth.embed_auth import get_request_copilot_session

    async def resolve_user(token: str | None = Depends(reuseable_oauth)):
        copilot_session = get_request_copilot_session()
        if not copilot_session:
            return await get_current_user(token)
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication token",
            )
        user = await authenticate_user(token)
        metadata = dict((user.metadata or {}) if user else {})
        if (
            not user
            or user.identifier != copilot_session.principal_id
            or metadata.get("auth_source") != "copilot_session"
            or metadata.get("copilot_session_id")
            != copilot_session.session_id
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication token",
            )
        return user

    chainlit_app.dependency_overrides[get_current_user] = resolve_user


def _configure_copilot_upload_validation(chainlit_server) -> None:
    """Allow Entra Copilot uploads without enabling standalone anonymous uploads."""

    from gpt_rag_ui.auth.embed_auth import get_request_copilot_session

    original_validate = getattr(
        chainlit_server,
        "_gpt_rag_original_validate_file_upload",
        chainlit_server.validate_file_upload,
    )
    chainlit_server._gpt_rag_original_validate_file_upload = (
        original_validate
    )

    def validate_file_upload(file, spec=None):
        session = get_request_copilot_session()
        if session and session.auth_mode == "entra" and spec is None:
            chainlit_server.validate_file_mime_type(file, spec)
            chainlit_server.validate_file_size(file, spec)
            return
        return original_validate(file, spec)

    chainlit_server.validate_file_upload = validate_file_upload


def _mount_panel_routes(host_app, config: AppConfigClient, chainlit_handlers) -> None:
    """Mount the panel's user-facing conversation routes (issue #611,
    ADR-0004) when running the hosted-agent chat backend.

    Never imports or touches ``panel_routes``/``panel_cosmos`` when the
    classic ``CHAT_BACKEND=orchestrator`` path is active, so the classic
    deployment carries zero additional dependency surface or behavior
    change. ``chainlit_handlers`` is the imported ``app`` module (accepted
    as a parameter, rather than imported here directly, so this function is
    independently testable with a fake namespace).
    """
    if getattr(chainlit_handlers, "CHAT_BACKEND", None) != "hosted_agent":
        return
    panel_settings = getattr(chainlit_handlers, "PANEL_SETTINGS", None)
    if panel_settings is None:
        return

    from gpt_rag_ui.api.panel_routes import register_panel_routes

    register_panel_routes(
        host_app,
        config=config,
        settings=panel_settings,
        continuity_active=lambda: bool(
            getattr(chainlit_handlers, "HOSTED_CONTINUITY_ENABLED", False)
        ),
        get_conversation_store=(
            lambda: chainlit_handlers.get_hosted_continuity_coordinator().store
        ),
    )
    logger.info(
        "Panel user-facing conversation routes mounted (active=%s)",
        panel_settings.user_surfaces_active,
    )


def _create_chainlit_app(
    config: AppConfigClient,
    auth_state: AuthState | None = None,
    embed_settings: EmbedSettings | None = None,
) -> FastAPI:
    """Create the main Chainlit ASGI app.

    Important: this must configure env vars before importing Chainlit.
    """

    embed_settings = embed_settings or EmbedSettings()
    _configure_embed_environment(embed_settings)
    _configure_auth_environment(config, auth_state)
    effective_auth = auth_state or _evaluate_auth_state(config, embed_settings)
    _sync_chainlit_spontaneous_file_upload(effective_auth)

    download_tokens = None
    if embed_settings.enabled:
        from gpt_rag_ui.services.download_security import configure_download_tokens

        download_tokens = configure_download_tokens(
            secret=os.environ["CHAINLIT_AUTH_SECRET"],
            public_url=embed_settings.ui_origin,
        )

    copilot_sessions = None
    if embed_settings.enabled:
        from gpt_rag_ui.auth.embed_auth import configure_session_store
        from gpt_rag_ui.auth.embed_security import disconnect_copilot_session

        copilot_sessions = configure_session_store(
            max_sessions=embed_settings.max_sessions,
            ttl_seconds=embed_settings.session_ttl_seconds,
            on_invalidate=disconnect_copilot_session,
        )

    # Importing chainlit.config does not create the server app, so enabled
    # origins can be applied in memory without modifying the deployment files.
    from chainlit.config import config as chainlit_config

    configure_chainlit_allowed_origins(embed_settings, chainlit_config)
    import chainlit.server as chainlit_server

    chainlit_app = chainlit_server.app
    sio = chainlit_server.sio
    if embed_settings.enabled:
        _configure_copilot_user_dependency(chainlit_app)
        _configure_copilot_upload_validation(chainlit_server)
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from starlette.middleware.cors import CORSMiddleware

    account_name = _get_str_config(config, "STORAGE_ACCOUNT_NAME")
    documents_container = _get_str_config(config, "DOCUMENTS_STORAGE_CONTAINER")
    images_container = _get_str_config(config, "DOCUMENTS_IMAGES_STORAGE_CONTAINER")
    conversation_documents_container = _get_str_config(
        config,
        "CONVERSATION_DOCUMENTS_STORAGE_CONTAINER",
    )
    shared_download_container_value = (
        os.environ.get("CITATION_SHARED_DOWNLOAD_CONTAINERS") or ""
    ).strip()
    if not shared_download_container_value:
        shared_download_container_value = _get_str_config(
            config,
            "CITATION_SHARED_DOWNLOAD_CONTAINERS",
        )
    shared_download_containers = {
        container.strip().strip("/")
        for container in shared_download_container_value.split(",")
        if container.strip().strip("/")
    }

    def download_from_blob(file_name: str) -> bytes:
        logger.info("Preparing blob download for '%s'", file_name)
        blob_url = (
            f"https://{account_name}.blob.core.windows.net/"
            f"{quote(file_name, safe='/')}"
        )
        logger.debug("Constructed blob URL %s", blob_url)

        blob_client = BlobClient(blob_url=blob_url)
        blob_data = blob_client.download_blob()
        logger.debug("Successfully downloaded blob data for '%s'", file_name)
        return blob_data

    blob_download_app = None
    if not embed_settings.enabled:
        # Preserve the existing standalone route. Copilot
        # mode registers a different, authenticated grant route on host_app.
        blob_download_app = FastAPI()

        def handle_file_download(file_path: str):
            try:
                file_bytes = download_from_blob(file_path)
                if not file_bytes:
                    return Response(
                        "File not found or empty.",
                        status_code=404,
                        media_type="text/plain",
                    )
            except ResourceNotFoundError:
                logger.error("Standalone download blob not found")
                return Response(
                    "Blob not found.",
                    status_code=404,
                    media_type="text/plain",
                )
            except Exception:
                logger.error("Standalone download failed")
                return Response(
                    "Internal server error.",
                    status_code=500,
                    media_type="text/plain",
                )

            actual_file_name = os.path.basename(file_path)
            return StreamingResponse(
                BytesIO(file_bytes),
                media_type="application/octet-stream",
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="{actual_file_name}"'
                    )
                },
            )

        @blob_download_app.get("/{container_name}/{file_path:path}")
        async def download_standalone_blob(
            container_name: str,
            file_path: str,
        ):
            logger.info(
                "Download request received: container=%s file=%s",
                container_name,
                file_path,
            )
            normalized = container_name.strip().strip("/")
            target_container = None
            if normalized == documents_container:
                target_container = documents_container
            elif normalized == images_container:
                target_container = images_container
            if not target_container:
                return Response(
                    "Container not found",
                    status_code=404,
                    media_type="text/plain",
                )
            return handle_file_download(f"{target_container}/{file_path}")

    # One-time runtime auth-mode log (useful when operators attach to logs after startup).
    _auth_mode_logged = False

    @chainlit_app.middleware("http")
    async def _log_auth_mode_once(request, call_next):
        nonlocal _auth_mode_logged
        if not _auth_mode_logged:
            _auth_mode_logged = True
            oauth_enabled = bool(
                (os.environ.get("OAUTH_AZURE_AD_CLIENT_ID") or "").strip()
                and (os.environ.get("OAUTH_AZURE_AD_TENANT_ID") or "").strip()
                and (os.environ.get("OAUTH_AZURE_AD_CLIENT_SECRET") or "").strip()
            )
            logger.info(
                "Auth effective: oauth_enabled=%s allow_anonymous=%s source=%s",
                oauth_enabled,
                (os.environ.get("ALLOW_ANONYMOUS_EFFECTIVE") or "<unset>"),
                (os.environ.get("ALLOW_ANONYMOUS_SOURCE") or "<unset>"),
            )
        return await call_next(request)

    # Import Chainlit event handlers.
    import gpt_rag_ui.services.chat as chainlit_handlers
    from gpt_rag_ui.api.callbacks import register_callbacks

    register_callbacks()
    logger.info("Chainlit handlers imported")

    # Provide friendly app metadata used by OpenAPI.
    chainlit_app.title = getattr(chainlit_app, "title", "Agent Landing Zone UI")
    version = _read_local_ui_version()
    if version:
        chainlit_app.version = version

    from fastapi.openapi.utils import get_openapi

    def _safe_openapi():
        if getattr(chainlit_app, "openapi_schema", None):
            return chainlit_app.openapi_schema
        try:
            chainlit_app.openapi_schema = get_openapi(
                title=chainlit_app.title,
                version=chainlit_app.version,
                routes=chainlit_app.routes,
            )
        except Exception:
            log_boundary_failure(logger, "OpenAPI generation failed; schema unavailable, retry required")
            raise
        return chainlit_app.openapi_schema

    chainlit_app.openapi = _safe_openapi

    @asynccontextmanager
    async def _host_lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if chainlit_handlers.CHAT_BACKEND == "hosted_agent":
                from gpt_rag_ui.clients.hosted_agent_client import close_hosted_agent_client

                await close_hosted_agent_client()

    host_app = FastAPI(title="Agent Landing Zone UI host", lifespan=_host_lifespan)
    if embed_settings.enabled:
        from gpt_rag_ui.auth.embed_security import (
            configure_copilot_bridge_guards,
            CopilotRequestMiddleware,
        )

        host_app.add_middleware(
            CopilotRequestMiddleware,
            settings=embed_settings,
            sessions=copilot_sessions,
            standalone_available=(
                effective_auth.oauth_configured
                or effective_auth.allow_anonymous
            ),
        )
        # CORS must be outermost so allowed portals can read authentication and
        # session errors instead of receiving an opaque browser failure.
        host_app.add_middleware(
            CORSMiddleware,
            allow_origins=list(embed_settings.runtime_allowed_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
        )
        configure_copilot_bridge_guards(
            sio,
            sessions=copilot_sessions,
            portal_origins=embed_settings.allowed_origins,
        )
        logger.info(
            "Chainlit Copilot enabled: auth_mode=%s public_url=%s "
            "origins=%s cookie_samesite=%s "
            "max_sessions=%s session_ttl_seconds=%s "
            "bootstrap_rate_limit_per_minute=%s",
            embed_settings.auth_mode,
            embed_settings.ui_origin,
            list(embed_settings.runtime_allowed_origins),
            embed_settings.cookie_samesite,
            embed_settings.max_sessions,
            embed_settings.session_ttl_seconds,
            embed_settings.bootstrap_rate_limit_per_minute,
        )
        logger.warning(
            "Chainlit Copilot stores session and Entra token state in this "
            "process. Run exactly one UI replica, or configure and verify "
            "end-to-end affinity for bootstrap, HTTP, Socket.IO polling and "
            "upgrades, and WebSocket traffic. Affinity is not high availability."
        )

    if embed_settings.enabled:
        from gpt_rag_ui.services.download_security import DownloadStream
        from gpt_rag_ui.api.download_routes import register_secure_download_route
        from gpt_rag_ui.api.embed_routes import register_copilot_auth_routes
        from gpt_rag_ui.auth.entra_token import EntraTokenValidator

        def stream_from_blob(file_name: str) -> DownloadStream:
            logger.info("Preparing blob download stream for '%s'", file_name)
            blob_url = (
                f"https://{account_name}.blob.core.windows.net/"
                f"{quote(file_name, safe='/')}"
            )
            blob_client = BlobClient(blob_url=blob_url)
            chunks, size = blob_client.download_blob_chunks()
            return DownloadStream(chunks=chunks, size=size)

        validator = None
        if embed_settings.auth_mode == "entra":
            validator = EntraTokenValidator(
                tenant_id=embed_settings.entra_tenant_id,
                audience=embed_settings.entra_audience,
                required_scope=embed_settings.entra_required_scope,
            )
        register_copilot_auth_routes(
            host_app,
            settings=embed_settings,
            sessions=copilot_sessions,
            validator=validator,
            config=config,
        )
        register_secure_download_route(
            host_app,
            manager=download_tokens,
            download_blob=stream_from_blob,
            allowed_containers={
                container
                for container in (
                    documents_container,
                    images_container,
                    conversation_documents_container,
                )
                if container
            },
            conversation_container=conversation_documents_container,
            shared_containers=shared_download_containers,
            sessions=copilot_sessions,
        )

    @host_app.get("/version-footer")
    async def get_version_footer_data():
        show_release_footer = config.get("SHOW_RELEASE_FOOTER", True, bool)
        gpt_rag_release = (config.get("RELEASE", "", str) or os.environ.get("RELEASE", "")).strip()
        gpt_rag_ui_release = _read_local_ui_version()

        payload = {
            "show_release_footer": show_release_footer,
            "gpt_rag_release": _format_release_value(
                gpt_rag_release,
                "Agent Landing Zone release information is missing",
            ),
            "gpt_rag_ui_release": _format_release_value(
                gpt_rag_ui_release,
                "agent-app-ui release information is missing",
            ),
        }
        return JSONResponse(payload)

    _mount_panel_routes(host_app, config, chainlit_handlers)

    if blob_download_app is not None:
        host_app.mount("/api/download", blob_download_app)
        logger.info("Mounted standalone blob downloads at /api/download")

    host_app.mount("/", chainlit_app)

    logger.info("Mounted Chainlit app at / on host app")

    FastAPIInstrumentor.instrument_app(host_app)
    HTTPXClientInstrumentor().instrument()
    return host_app


def build_app() -> FastAPI:
    get_asset_root()
    config: AppConfigClient = get_config()
    _startup_banner()

    connected = bool(getattr(config, "connected", False))
    if connected:
        logger.info("Configuration loaded from Azure App Configuration")

        try:
            embed_settings = load_embed_settings(config)
        except EmbedConfigError:
            logger.exception("Invalid Chainlit Copilot configuration")
            raise
        # Configure Chainlit prerequisites even when OAuth is missing. Copilot
        # requires an operator-managed secret rather than the standalone
        # development fallback.
        _configure_chainlit_prereqs(
            config,
            require_persistent_auth_secret=embed_settings.enabled,
        )
        auth_state = _evaluate_auth_state(config, embed_settings)
        if (
            auth_state.oauth_configured
            or auth_state.allow_anonymous
            or embed_settings.enabled
        ):
            return _create_chainlit_app(config, auth_state, embed_settings)

        logger.error(
            "OAuth is required but not configured and anonymous mode is disabled; starting in auth-required mode (HTTP 503)."
        )
        return _create_auth_required_app(auth_state)

    logger.warning(
        "Running without Azure App Configuration (not logged in or unavailable). "
        "Set env vars locally or run 'az login' to enable App Configuration."
    )
    return _create_not_ready_app()


# ASGI entry point (used by: `uvicorn main:app`)
app = build_app()