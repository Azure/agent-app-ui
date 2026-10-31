"""Ordinary authenticated BFF presentation routes; native protocols stay in clients."""

import logging
from typing import Annotated, Literal, Any

from azure.core.exceptions import AzureError
from fastapi import APIRouter, FastAPI, Request, Depends, HTTPException
from openai import OpenAIError
from grpc import RpcError
from pydantic import BaseModel, ConfigDict, Field

from gpt_rag_ui.auth.entra_token import EntraTokenValidator
from gpt_rag_ui.auth.auth_common import DelegationError
from gpt_rag_ui.auth.panel_auth import validate_panel_bearer, PanelAuthError, PanelForbiddenError
from gpt_rag_ui.clients.native_metadata import MetadataConflict
from gpt_rag_ui.clients.native_attachments import InlineAttachment, MAX_ATTACHMENTS
from gpt_rag_ui.services.native import (
    NativeService, NativePrincipal, NativeNotFound, NativeUnavailable,
    ResearchReviewStart, ReviewDecision,
)

logger = logging.getLogger(__name__)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionCreate(Input):
    application: Literal["conversational", "workflow"]


class Turn(Input):
    text: str = Field(min_length=1, max_length=8000, pattern=r"\S")
    attachments: list[InlineAttachment] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)


class Action(Input):
    request_id: str = Field(min_length=1, max_length=512)
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    response: ReviewDecision


class Feedback(Input):
    native_ref: str = Field(min_length=1, max_length=512)
    rating: Literal[-1, 1]
    comment: str = Field(default="", max_length=2000)


def register_native_routes(app: FastAPI, service: NativeService) -> None:
    validator = EntraTokenValidator(
        tenant_id=service.settings.tenant_id, audience=service.settings.token_audience,
    )
    router = APIRouter(prefix="/api/native")

    async def principal(request: Request) -> NativePrincipal:
        try:
            identity = await validate_panel_bearer(request, validator)
            return NativePrincipal(identity.tenant_id, identity.oid, identity.access_token, identity.name)
        except PanelAuthError as exc:
            raise HTTPException(401, "Authentication required.") from exc
        except PanelForbiddenError as exc:
            raise HTTPException(403, "Delegated user required.") from exc

    User = Annotated[NativePrincipal, Depends(principal)]

    async def call(operation: Any) -> Any:
        try:
            return await operation
        except NativeNotFound as exc:
            raise HTTPException(404, "Session not found.") from exc
        except PermissionError as exc:
            raise HTTPException(403, "Access denied.") from exc
        except DelegationError as exc:
            raise HTTPException(403 if exc.code == "source_denied" else 401, {
                "code": exc.code, "message": str(exc),
            }) from None
        except MetadataConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except NativeUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, "Invalid native operation.") from exc
        except (AzureError, OpenAIError, RpcError, TimeoutError, RuntimeError) as exc:
            logger.error("Native dependency failed (type=%s)", type(exc).__name__)
            raise HTTPException(502, "Native dependency failed; reload before retrying.") from exc

    @router.post("/sessions", status_code=201)
    async def create(body: SessionCreate, user: User) -> dict[str, Any]:
        row = await call(service.create(user, body.application))
        return {key: row[key] for key in ("id", "application", "title", "created_at")}

    @router.get("/sessions")
    async def sessions(user: User) -> dict[str, Any]:
        return {"items": await call(service.list_sessions(user))}

    @router.post("/sessions/{id}/turns")
    async def turn(id: str, body: Turn, user: User) -> dict[str, Any]:
        return {"response": await call(service.converse(user, id, body.text, body.attachments))}

    @router.post("/sessions/{id}/start", status_code=202)
    async def start(id: str, body: ResearchReviewStart, user: User) -> dict[str, Any]:
        return await call(service.start(user, id, body))

    @router.get("/sessions/{id}/progress")
    async def progress(id: str, user: User) -> dict[str, Any]:
        return await call(service.progress(user, id))

    @router.post("/sessions/{id}/actions", status_code=202)
    async def reply(id: str, body: Action, user: User) -> dict[str, Any]:
        return await call(service.reply(user, id, body.request_id, body.revision, body.response))

    @router.get("/sessions/{id}/history")
    async def history(id: str, user: User) -> dict[str, Any]:
        return {"items": await call(service.history(user, id))}

    @router.post("/sessions/{id}/resume")
    async def resume(id: str, user: User) -> dict[str, Any]:
        return await call(service.resume(user, id))

    @router.post("/sessions/{id}/feedback", status_code=201)
    async def feedback(id: str, body: Feedback, user: User) -> dict[str, Any]:
        return await call(service.feedback(user, id, body.rating, body.comment, body.native_ref))

    app.include_router(router)
