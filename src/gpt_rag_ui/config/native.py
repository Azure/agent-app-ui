"""ADR-0034 native UI configuration. Host selection is a deployment boundary."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict, cast
from urllib.parse import urlsplit

from gpt_rag_ui.config.errors import ConfigurationError

ApplicationMode = Literal["conversational", "workflow", "both"]
OrchestrationHost = Literal["hosted", "container_apps"]


class NativeConfig(Protocol):
    def get(self, key: str, default: str = "", type: type = str) -> str: ...
    def refresh(self) -> None: ...


class _ProductOptions(TypedDict):
    history: bool
    feedback: bool
    private_uploads: bool
    database_account: str
    database: str
    metadata_container: str
    storage_account: str
    private_container: str
    token_audience: str
    tenant_id: str
    workflow_title: str


def read(config: NativeConfig, key: str, default: str = "") -> str:
    value = os.environ.get(key)
    configured = str(config.get(key, default, str)).strip()
    if key in {"ORCHESTRATION_HOST", "APPLICATION_MODE"} and value and configured != default and configured != value:
        raise ConfigurationError(f"Contradictory {key}; update deployment configuration.")
    return (configured if value is None else value).strip()


def flag(config: NativeConfig, key: str) -> bool:
    value = read(config, key, "false").lower()
    if value not in {"true", "false"}:
        raise ConfigurationError(f"{key} must be true or false.")
    return value == "true"


def endpoint(value: str, key: str) -> str:
    parsed = urlsplit(value)
    if (
        not parsed.hostname or parsed.username or parsed.password
        or parsed.query or parsed.fragment
        or parsed.scheme not in {"https", "http"}
        or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})
    ):
        raise ConfigurationError(f"{key} must be an HTTPS SDK base URL (HTTP only on loopback).")
    return value.rstrip("/")


@dataclass(frozen=True)
class NativeSettings:
    host: OrchestrationHost
    mode: ApplicationMode = "conversational"
    agent_url: str = ""
    workflow_url: str = ""
    resource_scope: str = ""
    scheduler_binding: str = ""
    agent_name: str = ""
    workflow_name: str = ""
    workflow_title: str = "Pesquisa e revisão"
    history: bool = False
    feedback: bool = False
    private_uploads: bool = False
    database_account: str = ""
    database: str = ""
    metadata_container: str = "native-ui-metadata"
    storage_account: str = ""
    private_container: str = ""
    token_audience: str = ""
    tenant_id: str = ""

    @property
    def conversational(self) -> bool:
        return self.mode in {"conversational", "both"}

    @property
    def workflow(self) -> bool:
        return self.mode in {"workflow", "both"}

    @property
    def persistent_metadata(self) -> bool:
        # Durable owner mappings are product metadata, never execution checkpoints.
        return self.workflow or self.history or self.feedback or self.private_uploads

    @property
    def structural(self) -> tuple[object, ...]:
        return (
            self.host, self.mode, self.agent_url, self.workflow_url,
            self.resource_scope, self.scheduler_binding, self.agent_name,
            self.workflow_name, self.database_account, self.database,
            self.metadata_container, self.storage_account, self.private_container,
            self.token_audience, self.tenant_id,
        )


def load_native_settings(config: NativeConfig) -> NativeSettings | None:
    """An absent host preserves the previous deployment; explicit native never falls back."""
    host = read(config, "ORCHESTRATION_HOST").lower()
    if not host:
        if read(config, "APPLICATION_MODE"):
            raise ConfigurationError("APPLICATION_MODE requires explicit ORCHESTRATION_HOST.")
        return None
    if host not in {"hosted", "container_apps"}:
        raise ConfigurationError("ORCHESTRATION_HOST must be hosted or container_apps.")
    mode = read(config, "APPLICATION_MODE", "conversational").lower()
    if mode not in {"conversational", "workflow", "both"}:
        raise ConfigurationError("APPLICATION_MODE must be conversational, workflow or both.")
    legacy = read(config, "CHAT_BACKEND")
    if legacy:
        raise ConfigurationError("Native ORCHESTRATION_HOST cannot be combined with legacy CHAT_BACKEND.")
    if flag(config, "ALLOW_ANONYMOUS"):
        raise ConfigurationError("Native owner-bound sessions require authenticated users.")
    if flag(config, "HOSTED_AGENT_CONTINUITY_ENABLED") or flag(config, "DEPLOY_ADMINISTRATIVE_PANEL"):
        raise ConfigurationError("Legacy continuity/panel cannot back native execution.")

    values = _ProductOptions(
        history=flag(config, "ENABLE_CHAT_HISTORY"),
        feedback=flag(config, "ENABLE_USER_FEEDBACK"),
        private_uploads=flag(config, "ENABLE_PRIVATE_ATTACHMENTS"),
        database_account=read(config, "DATABASE_ACCOUNT_NAME"),
        database=read(config, "DATABASE_NAME"),
        metadata_container=read(config, "NATIVE_UI_METADATA_CONTAINER", "native-ui-metadata"),
        storage_account=read(config, "STORAGE_ACCOUNT_NAME"),
        private_container=read(config, "CONVERSATION_DOCUMENTS_STORAGE_CONTAINER"),
        token_audience=read(config, "NATIVE_UI_TOKEN_AUDIENCE", read(config, "CLIENT_ID")),
        tenant_id=read(config, "OAUTH_AZURE_AD_TENANT_ID", read(config, "AZURE_TENANT_ID")),
        workflow_title=read(config, "WORKFLOW_DISPLAY_NAME", "Pesquisa e revisão"),
    )
    if host == "hosted":
        if read(config, "DURABLE_TASK_SCHEDULER_CONNECTION_STRING"):
            raise ConfigurationError("Hosted UI cannot bind a Durable Task Scheduler.")
        auth_mode = read(config, "HOSTED_AGENT_AUTH_MODE", "user_delegated")
        if auth_mode != "user_delegated":
            raise ConfigurationError("Native Hosted uses user-delegated OBO, never service MI as the user.")
        scope = read(config, "HOSTED_AGENT_RESOURCE_SCOPE")
        if not scope.endswith("/.default") or "management.azure.com" in scope.lower():
            raise ConfigurationError("HOSTED_AGENT_RESOURCE_SCOPE must be an explicit data-plane /.default scope.")
        settings = NativeSettings(
            host="hosted", mode=cast(ApplicationMode, mode), resource_scope=scope,
            agent_url=endpoint(read(config, "HOSTED_AGENT_BASE_URL"), "HOSTED_AGENT_BASE_URL")
            if mode != "workflow" else "",
            workflow_url=endpoint(read(config, "HOSTED_WORKFLOW_BASE_URL"), "HOSTED_WORKFLOW_BASE_URL")
            if mode != "conversational" else "",
            **values,
        )
    else:
        if read(config, "HOSTED_AGENT_BASE_URL") or read(config, "HOSTED_WORKFLOW_BASE_URL"):
            raise ConfigurationError("Container Apps UI cannot also select Hosted endpoints.")
        binding = read(config, "DURABLE_TASK_SCHEDULER_CONNECTION_STRING")
        if not binding:
            raise ConfigurationError("Container Apps requires DURABLE_TASK_SCHEDULER_CONNECTION_STRING.")
        agent_name = read(config, "DURABLE_AGENT_NAME")
        workflow_name = read(config, "DURABLE_WORKFLOW_NAME")
        for key, name, needed in (
            ("DURABLE_AGENT_NAME", agent_name, mode != "workflow"),
            ("DURABLE_WORKFLOW_NAME", workflow_name, mode != "conversational"),
        ):
            if needed and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", name):
                raise ConfigurationError(f"{key} requires the explicit published runtime registration name.")
        settings = NativeSettings(
            host="container_apps", mode=cast(ApplicationMode, mode), scheduler_binding=binding,
            agent_name=agent_name, workflow_name=workflow_name, **values,
        )
    if settings.persistent_metadata and not (settings.database_account and settings.database):
        raise ConfigurationError("Enabled native product features require DATABASE_ACCOUNT_NAME and DATABASE_NAME.")
    if settings.private_uploads and not settings.conversational:
        raise ConfigurationError("Private native attachments are supported for conversational turns, not workflow starts.")
    if not settings.token_audience or not settings.tenant_id:
        raise ConfigurationError("Native BFF requires NATIVE_UI_TOKEN_AUDIENCE/CLIENT_ID and the authenticated tenant.")
    return settings
