"""Official Responses client; no /invocations SSE parser or classic history replay."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import Any, cast

from openai import AsyncOpenAI, Timeout, APIStatusError

from gpt_rag_ui.auth.auth_common import DelegationError, http_claims_challenge
from gpt_rag_ui.clients.hosted_agent_client import acquire_obo_token
from gpt_rag_ui.config.native import NativeSettings


class NativeHostedClient:
    def __init__(self, settings: NativeSettings):
        self.settings = settings

    @asynccontextmanager
    async def _client(self, token: str, *, workflow: bool) -> AsyncIterator[AsyncOpenAI]:
        if not token:
            raise PermissionError("An authenticated delegated user is required.")
        bearer = await acquire_obo_token(token, self.settings.resource_scope)
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
            return response.model_dump(mode="json")

    async def retrieve(self, token: str, response_id: str, *, workflow: bool) -> dict[str, Any]:
        async with self._client(token, workflow=workflow) as client:
            response = await client.responses.retrieve(response_id)
            return response.model_dump(mode="json")

    async def history(self, token: str, response_id: str, *, workflow: bool) -> list[dict[str, Any]]:
        """Read bounded native input and output, never a duplicated UI transcript."""
        async with self._client(token, workflow=workflow) as client:
            response = await client.responses.retrieve(response_id)
            inputs = await client.responses.input_items.list(response_id, limit=100, order="asc")
            return [
                item.model_dump(mode="json") for item in inputs.data
            ] + [item.model_dump(mode="json") for item in response.output]


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
