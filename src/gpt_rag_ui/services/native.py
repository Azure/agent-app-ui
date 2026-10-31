"""Authenticated product boundary over two deliberately distinct native clients."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from gpt_rag_ui.auth.auth_common import normalize_guid, is_user_authorized
from gpt_rag_ui.clients.native_hosted import NativeHostedClient, hosted_pending, hosted_reply
from gpt_rag_ui.clients.native_metadata import MetadataConflict, MetadataStore
from gpt_rag_ui.clients.native_attachments import InlineAttachment, MAX_ATTACHMENTS
from gpt_rag_ui.config.appconfig import AppConfigClient
from gpt_rag_ui.config.native import NativeSettings, load_native_settings


class NativeUnavailable(RuntimeError):
    """Disabled or incompatible product operation, not an apparent success."""


class NativeNotFound(LookupError):
    """Indistinguishable missing/wrong-owner selector."""


class NativeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResearchReviewStart(NativeInput):
    topic: str = Field(min_length=1, max_length=8000, pattern=r"\S")


class ReviewDecision(NativeInput):
    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=4000)


class ReviewRequest(NativeInput):
    topic: str
    draft: str
    configuration_revision: str


class ResearchReviewResult(NativeInput):
    status: Literal["approved", "rejected"]
    topic: str
    draft: str
    review_comment: str
    configuration_revision: str


@dataclass(frozen=True)
class NativePrincipal:
    tenant_id: str
    object_id: str
    token: str = field(repr=False)
    name: str = field(default="", repr=False)

    @property
    def owner(self) -> str:
        return f"{normalize_guid(self.tenant_id, claim_name='tid')}:{normalize_guid(self.object_id, claim_name='oid')}"


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def revision(native_id: str, pending: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(
        [native_id, pending], sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


class NativeService:
    def __init__(
        self, settings: NativeSettings, metadata: MetadataStore,
        *, hosted: NativeHostedClient | None = None, durable: Any = None,
        config: AppConfigClient | None = None,
    ):
        self.settings = settings
        self.metadata = metadata
        self.hosted = hosted
        self.durable = durable
        self.config = config

    async def current(self, principal: NativePrincipal) -> NativeSettings:
        if not principal.token or principal.tenant_id.lower() != self.settings.tenant_id.lower():
            raise PermissionError("Authenticated tenant user required.")
        _ = principal.owner
        if self.config is None:
            return self.settings
        await asyncio.to_thread(self.config.refresh)
        if not is_user_authorized(self.config, principal.name, principal.owner):
            raise PermissionError("Current user policy denies this operation.")
        current = load_native_settings(self.config)
        if current is None or current.structural != self.settings.structural:
            raise NativeUnavailable("Structural configuration changed; redeploy the UI.")
        if current.persistent_metadata and not self.settings.persistent_metadata:
            raise NativeUnavailable("Enabling persisted features requires redeployment.")
        return current

    async def owned(self, principal: NativePrincipal, id: str) -> dict[str, Any]:
        await self.current(principal)
        try:
            if str(uuid.UUID(id)) != id:
                raise ValueError("Noncanonical selector.")
        except (ValueError, TypeError, AttributeError) as exc:
            raise NativeNotFound("Session not found.") from exc
        row = await self.metadata.get(principal.owner, id)
        if (
            row is None or row.get("principal_id") != principal.owner
            or row.get("kind") != "session" or row.get("host") != self.settings.host
            or not isinstance(row.get("application"), str)
            or row["application"] not in {"conversational", "workflow"}
            or row.get("publication") != self.publication(row["application"])
            or not getattr(self.settings, row["application"])
        ):
            raise NativeNotFound("Session not found.")
        return row

    def publication(self, application: str) -> str:
        namespace: Any
        if self.settings.host == "hosted":
            namespace = self.settings.workflow_url if application == "workflow" else self.settings.agent_url
        else:
            name = self.settings.workflow_name if application == "workflow" else self.settings.agent_name
            namespace = [self.settings.scheduler_binding, name, 2]
        return hashlib.sha256(json.dumps([self.settings.host, namespace]).encode()).hexdigest()

    async def create(self, principal: NativePrincipal, application: str) -> dict[str, Any]:
        settings = await self.current(principal)
        if application not in {"conversational", "workflow"}:
            raise ValueError("Unknown application.")
        if not getattr(settings, application):
            raise NativeUnavailable("Application is disabled.")
        id = str(uuid.uuid4())
        return await self.metadata.create({
            "id": id, "principal_id": principal.owner, "kind": "session",
            "host": settings.host, "application": application,
            "publication": self.publication(application), "native_id": "",
            "title": "Conversar" if application == "conversational" else settings.workflow_title,
            "created_at": timestamp(), "updated_at": timestamp(), "busy": False,
        })

    async def _lock(self, row: dict[str, Any]) -> dict[str, Any]:
        if row.get("busy"):
            raise MetadataConflict("An operation is in progress or its outcome is uncertain; reload/contact support.")
        return await self.metadata.replace({**row, "busy": True})

    async def _commit(self, row: dict[str, Any], native_id: str) -> dict[str, Any]:
        return await self.metadata.replace({
            **row, "native_id": native_id, "busy": False, "updated_at": timestamp(),
        })

    async def converse(
        self, principal: NativePrincipal, id: str, text: str,
        attachments: list[InlineAttachment] | None = None,
    ) -> dict[str, Any]:
        row = await self.owned(principal, id)
        if row["application"] != "conversational":
            raise ValueError("This selector is not a conversation.")
        if not text.strip() or len(text) > 8000:
            raise ValueError("A turn requires 1–8000 characters.")
        attachments = attachments or []
        if attachments and not (await self.current(principal)).private_uploads:
            raise NativeUnavailable("Private attachments are disabled.")
        if len(attachments) > MAX_ATTACHMENTS:
            raise ValueError("At most three private attachments per turn.")
        for attachment in attachments:
            data = attachment.decode()
            await self.metadata.create({
                "id": "attachment-" + str(uuid.uuid4()), "kind": "attachment",
                "principal_id": principal.owner, "session_id": id,
                "name": attachment.name, "content_type": attachment.content_type,
                "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "created_at": timestamp(),
            })
        row = await self._lock(row)
        if self.settings.host == "hosted":
            assert self.hosted is not None
            response = await self.hosted.create(
                principal.token, workflow=False, text=text, previous_response_id=row["native_id"],
                input_items=[{"role": "user", "content": [
                    {"type": "input_text", "text": text},
                    *(attachment.hosted_content() for attachment in attachments),
                ]}] if attachments else None,
            )
            native_ref = response["id"]
            await self._commit(row, response["id"])
            if response.get("status") != "completed" or response.get("error"):
                raise NativeUnavailable("Native conversation did not complete; inspect its progress.")
        else:
            # DTS service access authenticates the BFF, never the end user.
            # Tool delegation is an independent runtime contract, not caller metadata.
            response = await self.durable.converse(row["id"], text, options={}, attachments=attachments)
            native_ref = row["id"]
            await self._commit(row, row["id"])
        return {"response": response, "native_ref": native_ref}

    async def start(self, principal: NativePrincipal, id: str, input: ResearchReviewStart) -> dict[str, Any]:
        row = await self.owned(principal, id)
        if row["application"] != "workflow" or row["native_id"]:
            raise MetadataConflict("This is not an unstarted process.")
        row = await self._lock(row)
        if self.settings.host == "hosted":
            assert self.hosted is not None
            response = await self.hosted.create(
                principal.token, workflow=True, text=json.dumps(input.model_dump(mode="json")),
            )
            native_id = response["id"]
        else:
            native_id = await self.durable.start(input.model_dump(mode="json"), row["id"])
        await self._commit(row, native_id)
        return await self.progress(principal, id)

    async def progress(self, principal: NativePrincipal, id: str) -> dict[str, Any]:
        row = await self.owned(principal, id)
        if not row["native_id"]:
            return {"id": id, "status": "not_started", "pending": [], "revision": "", "output": None}
        if self.settings.host == "hosted":
            assert self.hosted is not None
            response = await self.hosted.retrieve(
                principal.token, row["native_id"], workflow=row["application"] == "workflow",
            )
            pending = hosted_pending(response)
            status = "waiting_for_input" if pending else response["status"]
            output = response.get("output")
        elif row["application"] == "workflow":
            response = await self.durable.progress(row["native_id"])
            pending, status, output = response["pending"], response["status"], response["output"]
            if pending:
                status = "waiting_for_input"
        else:
            pending, status, output = [], "conversation", None
        if row["application"] == "workflow":
            try:
                for item in pending:
                    if (
                        not isinstance(item.get("request_id"), str)
                        or not 1 <= len(item["request_id"]) <= 512
                        or (
                            self.settings.host == "container_apps"
                            and item.get("source_executor_id") != "human-review"
                        )
                        or (
                            self.settings.host == "hosted"
                            and item.get("wire_type") != "function_call_output"
                        )
                    ):
                        raise ValueError("Unsupported native review action.")
                    if not isinstance(item.get("data"), dict):
                        raise ValueError("Review payload must be a JSON object.")
                    ReviewRequest.model_validate(item["data"], strict=True)
                if self.settings.host == "container_apps" and status == "COMPLETED":
                    if not isinstance(output, list) or len(output) != 1 or not isinstance(output[0], dict):
                        raise ValueError("Unsupported native result.")
                    ResearchReviewResult.model_validate(output[0], strict=True)
            except (ValueError, TypeError, AttributeError) as exc:
                raise NativeUnavailable("Runtime published an unsupported research/review contract.") from exc
        return {
            "id": id, "status": status, "pending": pending, "output": output,
            "revision": revision(row["native_id"], pending),
        }

    async def reply(
        self, principal: NativePrincipal, id: str, request_id: str,
        expected_revision: str, response: ReviewDecision,
    ) -> dict[str, Any]:
        row = await self.owned(principal, id)
        if row["application"] != "workflow" or not row["native_id"]:
            raise MetadataConflict("No pending process.")
        progress = await self.progress(principal, id)
        pending = progress["pending"]
        if (
            progress["revision"] != expected_revision or len(pending) != 1
            or pending[0]["request_id"] != request_id
        ):
            raise MetadataConflict("Wrong, stale or non-pending action; reload.")
        # The product publishes one human-review step. Refuse a different graph/action.
        if self.settings.host == "container_apps" and pending[0].get("source_executor_id") != "human-review":
            raise NativeUnavailable("This graph's human action is not supported by this UI.")
        receipt_id = "action-" + hashlib.sha256(f"{id}:{expected_revision}:{request_id}".encode()).hexdigest()
        await self.metadata.create({
            "id": receipt_id, "principal_id": principal.owner, "kind": "action",
            "session_id": id, "request_id": request_id, "created_at": timestamp(),
        })
        row = await self._lock(row)
        if self.settings.host == "hosted":
            assert self.hosted is not None
            result = await self.hosted.create(
                principal.token, workflow=True, previous_response_id=row["native_id"],
                replies=[hosted_reply(pending[0], response.model_dump(mode="json"))],
            )
            native_id = result["id"]
        else:
            await self.durable.reply(row["native_id"], request_id, response.model_dump(mode="json"))
            native_id = row["native_id"]
        await self._commit(row, native_id)
        result = await self.progress(principal, id)
        # Delivery is not admission: be honest when the durable worker has not consumed it yet.
        result["delivery"] = "submitted"
        return result

    async def history(self, principal: NativePrincipal, id: str) -> list[dict[str, Any]]:
        settings = await self.current(principal)
        row = await self.owned(principal, id)
        if not settings.history:
            raise NativeUnavailable("History is disabled.")
        if not row["native_id"]:
            return []
        if settings.host == "hosted":
            assert self.hosted is not None
            return await self.hosted.history(
                principal.token, row["native_id"], workflow=row["application"] == "workflow",
            )
        if row["application"] == "workflow":
            # The native output/pending progress is the process history, not an invented transcript.
            return [await self.progress(principal, id)]
        return await self.durable.history(row["native_id"])

    async def list_sessions(self, principal: NativePrincipal) -> list[dict[str, Any]]:
        if not (await self.current(principal)).history:
            raise NativeUnavailable("History is disabled.")
        return [
            {key: row[key] for key in ("id", "application", "title", "created_at", "updated_at")}
            for row in await self.metadata.list(principal.owner, "session")
            if row.get("application") in {"conversational", "workflow"}
            and row["host"] == self.settings.host and row["publication"] == self.publication(row["application"])
            and getattr(self.settings, row["application"], False)
        ]

    async def resume(self, principal: NativePrincipal, id: str) -> dict[str, Any]:
        settings = await self.current(principal)
        row = await self.owned(principal, id)
        if row["application"] == "conversational" and not settings.history:
            raise NativeUnavailable("Conversation reopening requires ENABLE_CHAT_HISTORY.")
        return await self.progress(principal, id)

    async def feedback(
        self, principal: NativePrincipal, id: str, rating: int, comment: str, native_ref: str,
    ) -> dict[str, Any]:
        if not (await self.current(principal)).feedback:
            raise NativeUnavailable("Feedback is disabled.")
        row = await self.owned(principal, id)
        if native_ref != row["native_id"]:
            raise MetadataConflict("Feedback references a stale native result.")
        if not row["native_id"] or rating not in {-1, 1} or len(comment) > 2000:
            raise ValueError("Feedback requires a native result, rating -1/1 and at most 2000 comment characters.")
        # Reference the native execution, not untrusted UI message/owner IDs or content.
        await self.progress(principal, id)
        record = await self.metadata.create({
            "id": "feedback-" + str(uuid.uuid4()), "kind": "feedback",
            "principal_id": principal.owner, "session_id": id, "native_ref": row["native_id"],
            "rating": rating, "comment": comment, "created_at": timestamp(),
        })
        return {"id": record["id"], "created_at": record["created_at"]}

    async def aclose(self) -> None:
        await self.metadata.aclose()
        if self.durable is not None:
            await self.durable.aclose()
