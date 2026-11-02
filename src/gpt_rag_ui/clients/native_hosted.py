"""Official Responses client; no /invocations SSE parser or classic history replay."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import Any, cast

from openai import AsyncOpenAI, Timeout, APIStatusError

from gpt_rag_ui.auth.auth_common import DelegationError, http_claims_challenge
from gpt_rag_ui.clients.hosted_agent_client import (
    HostedAgentAuthenticationError, acquire_obo_token, acquire_service_identity_token,
)
from gpt_rag_ui.config.native import HostedAuthMode, NativeSettings


class HostedConsentRequired(DelegationError):
    """Managed Foundry consent, not a UI OAuth refresh or claims challenge."""

    def __init__(self) -> None:
        super().__init__("consent_required")

    def __str__(self) -> str:
        return (
            "The Foundry tool requires consent. Complete the approved Microsoft Foundry "
            "consent process for this signed-in user, or ask your administrator to grant "
            "the required downstream permissions, then return and retry. This UI does not "
            "open consent links, confirm consent, or automatically resume the request."
        )


class HostedToolFailure(RuntimeError):
    """Safe tool failure without provider bodies, consent links or source tokens."""

    def __init__(self, *, auth_mode: HostedAuthMode = "user_delegated") -> None:
        super().__init__(
            "The source could not be accessed with service identity. Check its service permissions. "
            "If it requires user delegation (OBO), ask the application operator to enable OBO."
            if auth_mode == "service_identity" else
            "The Foundry tool failed. Ask the operator to check its authorization and configuration."
        )


def check_hosted_response(response: dict[str, Any], *, auth_mode: HostedAuthMode) -> None:
    """Reject explicit managed-tool outcomes before displaying any response content."""
    output = response.get("output", [])
    if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
        raise HostedToolFailure(auth_mode=auth_mode)
    if any(item.get("type") == "oauth_consent_request" for item in output):
        # No initial consent-link origin/path contract is configured here.
        # Suppress every supplied URL rather than accepting an unbound browser flow.
        if auth_mode == "service_identity":
            raise DelegationError("user_delegation_required")
        raise HostedConsentRequired()
    if response.get("error") or response.get("status") == "failed" or any(
        item.get("type") in {"mcp_call", "mcp_list_tools"} and (
            item.get("error") is not None or item.get("status") in {"failed", "incomplete"}
        )
        for item in output
    ):
        raise HostedToolFailure(auth_mode=auth_mode)


class NativeHostedClient:
    def __init__(self, settings: NativeSettings):
        self.settings = settings

    @asynccontextmanager
    async def _client(self, token: str, *, workflow: bool) -> AsyncIterator[AsyncOpenAI]:
        if not token:
            raise PermissionError("An authenticated user is required.")
        if self.settings.auth_mode not in {"user_delegated", "service_identity"}:
            raise PermissionError("Unsupported hosted authentication mode.")
        try:
            async with asyncio.timeout(30):
                if self.settings.auth_mode == "service_identity":
                    bearer = await acquire_service_identity_token(self.settings.resource_scope)
                else:
                    bearer = await acquire_obo_token(token, self.settings.resource_scope)
        except HostedAgentAuthenticationError:
            if self.settings.auth_mode == "user_delegated":
                raise
            raise DelegationError("authentication_unavailable") from None
        except TimeoutError:
            raise DelegationError("authentication_unavailable") from None
        # This is the exact official SDK base URL published by composition.
        # No x-ms-user-identity is fabricated from caller metadata.
        async with AsyncOpenAI(
            base_url=self.settings.workflow_url if workflow else self.settings.agent_url,
            api_key=bearer, max_retries=0,
            timeout=Timeout(120, connect=10, pool=10),
        ) as client:
            try:
                yield client
            except APIStatusError as exc:
                if exc.status_code not in {401, 403}:
                    raise
                if self.settings.auth_mode == "service_identity":
                    # A workload failure cannot be repaired by user consent.
                    raise DelegationError(
                        "authentication_unavailable" if exc.status_code == 401 else "source_denied",
                    ) from None
                # The official SDK response exposes the same get_list header
                # interface used by our bounded, destination-scoped challenge parser.
                challenge = http_claims_challenge(cast(Any, exc.response))
                raise DelegationError(
                    "interaction_required" if challenge else
                    "authentication_required" if exc.status_code == 401 else "source_denied",
                    claims_challenge=challenge,
                ) from None

    async def create(
        self, token: str, *, workflow: bool, text: str = "",
        previous_response_id: str = "", replies: list[dict[str, Any]] | None = None,
        input_items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "input": replies if replies is not None else input_items if input_items is not None else text,
            "store": True,
        }
        if previous_response_id:
            kwargs["previous_response_id"] = previous_response_id
        async with self._client(token, workflow=workflow) as client:
            response = await client.responses.create(**kwargs)
            # Foundry extends the pinned SDK output union. Serialization warnings
            # can include an opaque consent URL; retain extras without logging it.
            return response.model_dump(mode="json", warnings=False)

    async def retrieve(self, token: str, response_id: str, *, workflow: bool) -> dict[str, Any]:
        async with self._client(token, workflow=workflow) as client:
            response = await client.responses.retrieve(response_id)
            return response.model_dump(mode="json", warnings=False)

    async def history(self, token: str, response_id: str, *, workflow: bool) -> list[dict[str, Any]]:
        """Read bounded native input and output, never a duplicated UI transcript."""
        async with self._client(token, workflow=workflow) as client:
            response = await client.responses.retrieve(response_id)
            check_hosted_response(response.model_dump(mode="json", warnings=False), auth_mode=self.settings.auth_mode)
            inputs = await client.responses.input_items.list(response_id, limit=100, order="asc")
            history = [
                item.model_dump(mode="json", warnings=False) for item in inputs.data
            ] + [item.model_dump(mode="json", warnings=False) for item in response.output]
            check_hosted_response({"output": history}, auth_mode=self.settings.auth_mode)
            return history


def hosted_pending(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Use the published hosting wheel's request_info function-call projection."""
    pending = []
    for item in response.get("output", []):
        if item.get("type") == "function_call" and item.get("name") == "request_info":
            arguments = json.loads(item["arguments"])
            if arguments.get("request_id") != item.get("call_id"):
                raise ValueError("Hosted request_info has an invalid correlation.")
            pending.append({
                "request_id": item["call_id"], "data": arguments["request"],
                "wire_type": "function_call_output",
            })
        elif item.get("type") == "mcp_approval_request":
            pending.append({
                "request_id": item["id"], "data": item.get("arguments"),
                "wire_type": "mcp_approval_response",
            })
    return pending


def hosted_reply(pending: dict[str, Any], response: Any) -> dict[str, Any]:
    if pending["wire_type"] == "mcp_approval_response":
        if type(response) is not bool:
            raise ValueError("An MCP approval requires a boolean.")
        return {"type": "mcp_approval_response", "approval_request_id": pending["request_id"], "approve": response}
    return {
        "type": "function_call_output", "call_id": pending["request_id"],
        "output": json.dumps(response, allow_nan=False),
    }
