"""Official standalone Durable Extension clients, with blocking calls off-loop."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from contextvars import ContextVar
from collections import namedtuple
from typing import Any
from urllib.parse import urlsplit
import grpc

from azure.identity import ManagedIdentityCredential
from agent_framework import Content, Message
from agent_framework_durabletask import (
    AgentSessionId, DurableAgentSession, DurableAIAgentClient, DurableWorkflowClient,
    read_agent_state,
)
from durabletask.azuremanaged.client import DurableTaskSchedulerClient
from durabletask.entities import EntityInstanceId

from gpt_rag_ui.config.errors import ConfigurationError
from gpt_rag_ui.config.native import NativeSettings
from gpt_rag_ui.clients.native_attachments import InlineAttachment

_agent_deadline: ContextVar[float | None] = ContextVar("native_agent_deadline", default=None)


class _CallDetails(namedtuple("CallDetails", "method timeout metadata credentials wait_for_ready compression"), grpc.ClientCallDetails):
    pass


class BoundedRpc(grpc.UnaryUnaryClientInterceptor):
    """Use the SDK's supported interceptor injection, not private stub patching."""

    def intercept_unary_unary(self, continuation: Any, details: Any, request: Any) -> Any:
        timeout = min(details.timeout or 30, 30)
        deadline = _agent_deadline.get()
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Native agent polling deadline reached.")
            timeout = min(timeout, remaining)
        return continuation(_CallDetails(
            details.method, timeout, details.metadata,
            details.credentials, details.wait_for_ready, details.compression,
        ), request)


def scheduler_options(binding: str) -> dict[str, str]:
    """Parse only the nonsecret MI binding; endpoints come from real outputs."""
    fields: dict[str, str] = {}
    for part in binding.rstrip(";").split(";"):
        key, separator, value = part.partition("=")
        if not separator or not value.strip() or key.strip().lower() in fields:
            raise ConfigurationError("Malformed DTS managed-identity binding.")
        fields[key.strip().lower()] = value.strip()
    if set(fields) - {"endpoint", "taskhub", "authentication", "clientid"}:
        raise ConfigurationError("DTS binding contains unsupported fields.")
    if fields.get("authentication", "").lower() != "managedidentity":
        raise ConfigurationError("DTS client requires Authentication=ManagedIdentity.")
    endpoint = urlsplit(fields.get("endpoint", ""))
    if (
        endpoint.scheme != "https" or not endpoint.hostname or endpoint.username
        or endpoint.password or endpoint.query or endpoint.fragment or endpoint.path not in {"", "/"}
        or not fields.get("taskhub")
    ):
        raise ConfigurationError("DTS requires the actual HTTPS Endpoint and TaskHub outputs.")
    return {"host_address": endpoint.netloc, "taskhub": fields["taskhub"], "client_id": fields.get("clientid", "")}


class NativeDurableClient:
    def __init__(self, settings: NativeSettings, *, client: Any = None):
        self.settings = settings
        self._credential = None
        if client is None:
            options = scheduler_options(settings.scheduler_binding)
            self._credential = ManagedIdentityCredential(
                client_id=options["client_id"] or os.environ.get("AZURE_CLIENT_ID") or None,
            )
            client = DurableTaskSchedulerClient(
                host_address=options["host_address"], taskhub=options["taskhub"],
                token_credential=self._credential, secure_channel=True,
                interceptors=[BoundedRpc()],
            )
        self.client = client
        # Coordinated new native worker uses isolated_v2, never legacy entity migration.
        self.workflows = DurableWorkflowClient(client, workflow_name=settings.workflow_name or None, entity_version=2)
        self.agents = DurableAIAgentClient(client, max_poll_retries=120, poll_interval_seconds=1, entity_version=2)

    async def start(self, input: Any, instance_id: str) -> str:
        return await asyncio.to_thread(
            self.workflows.start_workflow, input=input, instance_id=instance_id,
        )

    async def progress(self, instance_id: str) -> dict[str, Any]:
        status = await asyncio.to_thread(self.workflows.get_runtime_status, instance_id)
        pending = await asyncio.to_thread(self.workflows.get_pending_hitl_requests, instance_id)
        output = None
        if status == "COMPLETED":
            output = await asyncio.to_thread(self.workflows.await_workflow_output, instance_id, timeout_seconds=5)
        return {"status": status, "pending": pending, "output": output}

    async def reply(self, instance_id: str, request_id: str, response: Any) -> None:
        await asyncio.to_thread(self.workflows.send_hitl_response, instance_id, request_id, response)

    async def converse(
        self, session_key: str, text: str, *, options: dict[str, Any],
        attachments: list[InlineAttachment] | None = None,
    ) -> dict[str, Any]:
        session = DurableAgentSession(
            durable_session_id=AgentSessionId(name=self.settings.agent_name, key=session_key, entity_version=2),
        )
        agent = self.agents.get_agent(self.settings.agent_name)
        kwargs: dict[str, Any] = {"session": session, "options": options}
        if attachments:
            message = Message("user", [
                Content.from_text(text), *(attachment.durable_content() for attachment in attachments),
            ])
            kwargs["context_messages"] = [message.to_dict()]
            kwargs["context_message_ids"] = [str(uuid.uuid4())]
        # Cancelling an asyncio wait does not kill a blocking thread. The supported
        # interceptor also bounds all subsequent SDK polls in the copied thread context.
        deadline = _agent_deadline.set(time.monotonic() + 150)
        try:
            response = await asyncio.wait_for(asyncio.to_thread(agent.run, text, **kwargs), timeout=150)
        finally:
            _agent_deadline.reset(deadline)
        return response.to_dict()

    async def history(self, session_key: str) -> list[dict[str, Any]]:
        address = AgentSessionId(name=self.settings.agent_name, key=session_key, entity_version=2)
        metadata = await asyncio.to_thread(
            self.client.get_entity, EntityInstanceId(address.entity_name, address.key),
        )
        if metadata is None:
            return []
        state = read_agent_state(metadata.get_state())
        # The published SDK owns schema dispatch, not a UI checkpoint interpreter.
        return state.to_dict()["data"]["conversationHistory"]

    async def aclose(self) -> None:
        await asyncio.to_thread(self.client.close)
        if self._credential is not None:
            await asyncio.to_thread(self._credential.close)
