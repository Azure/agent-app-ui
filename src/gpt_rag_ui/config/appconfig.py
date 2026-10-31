import os
import logging
import threading

from typing import Dict, Any, Protocol
from azure.identity import ChainedTokenCredential, ManagedIdentityCredential, AzureCliCredential
from azure.identity.aio import ChainedTokenCredential as AsyncChainedTokenCredential, ManagedIdentityCredential as AsyncManagedIdentityCredential, AzureCliCredential as AsyncAzureCliCredential
from azure.appconfiguration import AzureAppConfigurationClient
from azure.core.exceptions import AzureError, ClientAuthenticationError
from azure.appconfiguration.provider import (
    AzureAppConfigurationKeyVaultOptions,
    load,
    SettingSelector
)

from tenacity import retry, retry_if_exception_type, wait_random_exponential, stop_after_attempt, RetryError

from gpt_rag_ui.config.errors import ConfigurationError

BASE_LABEL = 'agent-lz'
APP_LABEL = 'agent-app-ui'
LEGACY_APP_LABEL = 'gpt-rag-ui'

ATTACHMENTS_UNAVAILABLE_MESSAGE = "Attachments are unavailable in this deployment."

# Only UI consumers, including protected downloads. SQL/MCP tools stay runtime-owned.
NATIVE_UI_KEYS = (
    "ORCHESTRATION_HOST", "APPLICATION_MODE", "CHAT_BACKEND",
    "HOSTED_AGENT_BASE_URL", "HOSTED_WORKFLOW_BASE_URL", "HOSTED_AGENT_RESOURCE_SCOPE",
    "HOSTED_AGENT_AUTH_MODE", "DURABLE_TASK_SCHEDULER_CONNECTION_STRING",
    "DURABLE_AGENT_NAME", "DURABLE_WORKFLOW_NAME", "WORKFLOW_DISPLAY_NAME",
    "ENABLE_CHAT_HISTORY", "ENABLE_USER_FEEDBACK", "ENABLE_PRIVATE_ATTACHMENTS",
    "NATIVE_UI_METADATA_CONTAINER", "NATIVE_UI_TOKEN_AUDIENCE",
    "DATABASE_ACCOUNT_NAME", "DATABASE_NAME", "STORAGE_ACCOUNT_NAME",
    "CONVERSATION_DOCUMENTS_STORAGE_CONTAINER", "DOCUMENTS_STORAGE_CONTAINER",
    "DOCUMENTS_IMAGES_STORAGE_CONTAINER", "CITATION_SHARED_DOWNLOAD_CONTAINERS",
    "SEARCH_SERVICE_QUERY_ENDPOINT", "SEARCH_RAG_INDEX_NAME", "SEARCH_API_VERSION",
    "CHAINLIT_AUTH_SECRET", "CHAINLIT_URL",
    "ALLOW_ANONYMOUS", "CLIENT_ID", "AZURE_TENANT_ID",
    "ALLOWED_USER_NAMES", "ALLOWED_USER_PRINCIPALS",
    "OAUTH_AZURE_AD_CLIENT_ID", "OAUTH_AZURE_AD_TENANT_ID", "OAUTH_AZURE_AD_CLIENT_SECRET",
    "authClientSecret", "OAUTH_AZURE_AD_SCOPES", "OAUTH_AZURE_AD_ENABLE_SINGLE_TENANT",
    "CHAINLIT_COPILOT_ENABLED", "CHAINLIT_ALLOWED_ORIGINS", "CHAINLIT_COPILOT_AUTH_MODE",
    "CHAINLIT_COPILOT_ENTRA_TENANT_ID", "CHAINLIT_COPILOT_ENTRA_AUDIENCE",
    "CHAINLIT_COPILOT_ENTRA_REQUIRED_SCOPE", "CHAINLIT_COPILOT_SESSION_TTL_SECONDS",
    "CHAINLIT_COPILOT_MAX_SESSIONS", "CHAINLIT_COPILOT_BOOTSTRAP_RATE_LIMIT_PER_MINUTE",
    "CHAINLIT_COOKIE_SAMESITE", "CHAINLIT_ROOT_PATH",
    "APPLICATIONINSIGHTS_CONNECTION_STRING", "SHOW_RELEASE_FOOTER", "RELEASE",
    "HOSTED_AGENT_CONTINUITY_ENABLED", "DEPLOY_ADMINISTRATIVE_PANEL",
)


class IngestionUploadConfig(Protocol):
    def get(self, key: str, default: str, type: type) -> str: ...


def load_ingestion_uploads_enabled(config: IngestionUploadConfig) -> bool:
    """Read the independent ingestion capability without changing provider precedence.

    Missing preserves legacy upload eligibility; a present malformed value is
    an operator error, never an implicit opt-in.
    """
    value = config.get("DEPLOY_INGESTION_CONTAINER_APP", "true", str).strip().lower()
    if value in {"true", "1", "yes", "y", "on"}:
        return True
    if value in {"false", "0", "no", "n", "off"}:
        return False
    raise ConfigurationError(
        "DEPLOY_INGESTION_CONTAINER_APP must be a boolean (true or false). "
        "Set false for deployments without the ingestion service."
    )


def candidate_keys(key: str) -> list[str]:
    """Return the lookup keys for ``key``; only the exact key name is read."""
    return [key]

class AppConfigClient:

    credential = None
    aiocredential = None

    def __init__(self):
        """
        Bulk-loads configuration keys into an in-memory dict.
        """
        # ==== Load all config parameters in one place ====
        self.tenant_id = os.environ.get('AZURE_TENANT_ID')
        self.client_id = os.environ.get('AZURE_CLIENT_ID')

        self.connected: bool = False
        self.native = bool(os.environ.get("ORCHESTRATION_HOST"))
        self._refresh_lock = threading.Lock()

        endpoint = os.getenv("APP_CONFIG_ENDPOINT")

        # Local/dev friendly behavior: if endpoint is not provided, run with env vars only.
        if not endpoint:
            logging.getLogger("gpt_rag_ui.appconfig").warning(
                "APP_CONFIG_ENDPOINT is not set; running with environment variables only."
            )
            self.client = {}
            return

        self.credential = ChainedTokenCredential(
            ManagedIdentityCredential(client_id=self.client_id),
            AzureCliCredential()
        )
        self.aiocredential = AsyncChainedTokenCredential(
            AsyncManagedIdentityCredential(client_id=self.client_id),
            AsyncAzureCliCredential()
        )

        app_label_selector = SettingSelector(label_filter=APP_LABEL, key_filter='*')
        legacy_app_label_selector = SettingSelector(label_filter=LEGACY_APP_LABEL, key_filter='*')
        agentlz_label_selector = SettingSelector(label_filter=BASE_LABEL, key_filter='*')
        no_label_selector = SettingSelector(label_filter=None, key_filter='*')

        logger = logging.getLogger("gpt_rag_ui.appconfig")

        try:
            # Discover the native deployment using the canonical label, before any
            # wildcard/KV load. Never resolve unrelated runtime secret references.
            probe = load(
                selects=[SettingSelector(label_filter=BASE_LABEL, key_filter=k)
                         for k in ("ORCHESTRATION_HOST", "APPLICATION_MODE")],
                endpoint=endpoint, credential=self.credential,
            )
            self.native = self.native or bool(probe.get("ORCHESTRATION_HOST"))
            if self.native:
                self.client = load(
                    selects=[SettingSelector(label_filter=BASE_LABEL, key_filter=k) for k in NATIVE_UI_KEYS],
                    endpoint=endpoint, credential=self.credential,
                    refresh_on=[(k, BASE_LABEL) for k in NATIVE_UI_KEYS],
                    refresh_interval=30,
                    key_vault_options=AzureAppConfigurationKeyVaultOptions(credential=self.credential),
                )
                self.connected = True
                return
            logger.info(
                "Loading Azure App Configuration keys using labels: 'agent-app-ui', 'gpt-rag-ui' (legacy), 'agent-lz', and <no label>"
            )
            self.client = load(
                selects=[app_label_selector, legacy_app_label_selector, agentlz_label_selector, no_label_selector],
                endpoint=endpoint,
                credential=self.credential,
                key_vault_options=AzureAppConfigurationKeyVaultOptions(credential=self.credential),
            )
            self.connected = True
        except (ClientAuthenticationError, AzureError) as e:
            if self.native:
                raise ConfigurationError("Native UI App Configuration is unavailable.") from e
            # Most common local dev issue: not logged in / no managed identity.
            logger.warning(
                "Azure App Configuration unavailable (auth/network). Running with env vars only. "
                "If using Azure CLI auth, run: az login. Error: %s",
                e,
            )
            self.client = {}
        except ValueError as e:
            if self.native:
                raise ConfigurationError("Native UI App Configuration is invalid.") from e
            # Fallback: try connection string if provided, otherwise keep env-only.
            logger.warning(
                "Unable to connect to Azure App Configuration endpoint; trying connection string (if set). Error: %s",
                e,
            )
            try:
                connection_string = os.environ["AZURE_APPCONFIG_CONNECTION_STRING"]
                self.client = load(
                    connection_string=connection_string,
                    key_vault_options=AzureAppConfigurationKeyVaultOptions(credential=self.credential),
                )
                self.connected = True
            except (KeyError, IndexError, ValueError, AzureError) as e2:
                logger.warning(
                    "Azure App Configuration connection string not available/failed; running with env vars only. Error: %s",
                    e2,
                )
                self.client = {}

    def refresh(self) -> None:
        """Official cached refresh; callers offload synchronous provider I/O."""
        refresh = getattr(self.client, "refresh", None)
        if self.native and callable(refresh):
            with self._refresh_lock:
                refresh()

    def get(self, key: str, default: Any = None, type: type = str) -> Any:
        return self.get_value(key, default=default, allow_none=False, type=type)

    def get_value(self, key: str, default: str = None, allow_none: bool = False, type: type = str) -> str:

        if key is None:
            raise ConfigurationError('The key parameter is required for get_value().')

        value = None

        for candidate in candidate_keys(key):
            try:
                value = self.get_config_with_retry(name=candidate)
                break
            except KeyError:
                logging.getLogger("gpt_rag_ui.appconfig").debug(
                    "Configuration key '%s' is absent.", candidate,
                )
            except RetryError as exc:
                if not isinstance(exc.last_attempt.exception(), AzureError):
                    raise
                logging.getLogger("gpt_rag_ui.appconfig").warning(
                    "Configuration lookup exhausted retries for key '%s'.", candidate,
                )

        if value is not None:
            if type is not None:
                if type is bool:
                    if isinstance(value, str):
                        value = value.lower() in ['true', '1', 'yes']
                else:
                    try:
                        value = type(value)
                    except ValueError as e:
                        raise ConfigurationError(f'Value for {key} could not be converted to {type.__name__}. Error: {e}') from e
            return value
        else:
            if default is not None or allow_none is True:
                return default

            raise ConfigurationError(f'The configuration variable {key} not found.')

    @staticmethod
    def retry_before_sleep(retry_state):
        # Log the outcome of each retry attempt.
        message = f"""Retrying {retry_state.fn}:
                        attempt {retry_state.attempt_number}
                        ended with: {retry_state.outcome}"""
        if retry_state.outcome.failed:
            ex = retry_state.outcome.exception()
            message += f"; Exception: {ex.__class__.__name__}: {ex}"
        if retry_state.attempt_number < 1:
            logging.info(message)
        else:
            logging.warning(message)

    @retry(
        retry=retry_if_exception_type(AzureError),
        wait=wait_random_exponential(multiplier=1, max=5),
        stop=stop_after_attempt(5),
        before_sleep=retry_before_sleep
    )
    def get_config_with_retry(self, name):
        return self.client[name]

    # Helper functions for reading environment variables
    def read_env_variable(self, var_name, default=None):
        value = self.get_value(var_name, default)
        return value.strip() if value else default

    def read_env_list(self, var_name):
        value = self.get_value(var_name, "")
        return [item.strip() for item in value.split(",") if item.strip()]

    def read_env_boolean(self, var_name, default=False):
        value = self.get_value(var_name, str(default)).strip().lower()
        return value in ['true', '1', 'yes']
