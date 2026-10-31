"""Owner mappings and product records only. Never transcripts/checkpoints/tokens."""

from __future__ import annotations

from copy import deepcopy
import os
from typing import Any, Protocol

from azure.core import MatchConditions
from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import CosmosResourceNotFoundError, CosmosHttpResponseError
from azure.identity.aio import ManagedIdentityCredential

from gpt_rag_ui.config.native import NativeSettings


class MetadataConflict(RuntimeError):
    """A concurrent or duplicate mutation must not be retried as a new action."""


class MetadataStore(Protocol):
    async def get(self, owner: str, id: str) -> dict[str, Any] | None: ...
    async def create(self, row: dict[str, Any]) -> dict[str, Any]: ...
    async def replace(self, row: dict[str, Any]) -> dict[str, Any]: ...
    async def list(self, owner: str, kind: str) -> list[dict[str, Any]]: ...
    async def aclose(self) -> None: ...


class MemoryMetadata:
    """Session-only conversational deployments; cross-BFF continuity is explicitly off."""

    def __init__(self):
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}

    async def get(self, owner: str, id: str) -> dict[str, Any] | None:
        return deepcopy(self.rows.get((owner, id)))

    async def create(self, row: dict[str, Any]) -> dict[str, Any]:
        key = (row["principal_id"], row["id"])
        if key in self.rows:
            raise MetadataConflict("Duplicate product record.")
        if len(self.rows) >= 1000:
            raise MetadataConflict("Session-local metadata capacity reached; close/restart this session-only deployment.")
        result = {**row, "_etag": "1"}
        self.rows[key] = deepcopy(result)
        return result

    async def replace(self, row: dict[str, Any]) -> dict[str, Any]:
        key = (row["principal_id"], row["id"])
        current = self.rows.get(key)
        if current is None or current["_etag"] != row["_etag"]:
            raise MetadataConflict("Concurrent product operation.")
        result = {**row, "_etag": str(int(row["_etag"]) + 1)}
        self.rows[key] = deepcopy(result)
        return result

    async def list(self, owner: str, kind: str) -> list[dict[str, Any]]:
        return [
            deepcopy(row) for (principal, _), row in self.rows.items()
            if principal == owner and row["kind"] == kind
        ][:100]

    async def aclose(self) -> None:
        self.rows.clear()


class CosmosMetadata:
    """Provisioned container, /principal_id partition. No runtime resource creation."""

    def __init__(self, settings: NativeSettings, *, credential: Any = None):
        if not settings.persistent_metadata:
            raise ValueError("Cosmos metadata is only enabled by product features.")
        self._owns_credential = credential is None
        self._credential = credential or ManagedIdentityCredential(client_id=os.environ.get("AZURE_CLIENT_ID"))
        self.client = CosmosClient(
            f"https://{settings.database_account}.documents.azure.com",
            credential=self._credential, connection_timeout=10, timeout=30,
        )
        self.container = self.client.get_database_client(settings.database).get_container_client(settings.metadata_container)

    async def get(self, owner: str, id: str) -> dict[str, Any] | None:
        try:
            return await self.container.read_item(id, partition_key=owner)
        except CosmosResourceNotFoundError:
            return None

    async def create(self, row: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self.container.create_item(row)
        except CosmosHttpResponseError as exc:
            if exc.status_code == 409:
                raise MetadataConflict("Duplicate product record.") from exc
            raise

    async def replace(self, row: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self.container.replace_item(
                row["id"], row, etag=row["_etag"], match_condition=MatchConditions.IfNotModified,
            )
        except CosmosHttpResponseError as exc:
            if exc.status_code in {409, 412}:
                raise MetadataConflict("Concurrent product operation.") from exc
            raise

    async def list(self, owner: str, kind: str) -> list[dict[str, Any]]:
        return [row async for row in self.container.query_items(
            query="SELECT TOP 100 * FROM c WHERE c.principal_id=@owner AND c.kind=@kind",
            parameters=[{"name": "@owner", "value": owner}, {"name": "@kind", "value": kind}],
            partition_key=owner,
        )]

    async def aclose(self) -> None:
        await self.client.close()
        if self._owns_credential:
            await self._credential.close()
