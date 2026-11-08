"""Chainlit presentation. Human waits live in Foundry/DTS, never AskUserMessage."""

from __future__ import annotations

import json
import asyncio
import base64
import logging
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any

import chainlit as cl
from chainlit.types import ThreadDict
from azure.core.exceptions import AzureError
from openai import OpenAIError
from grpc import RpcError

from gpt_rag_ui.auth.auth_common import DelegationError
from gpt_rag_ui.auth.recovery import offer_auth_recovery
from gpt_rag_ui.clients.hosted_agent_client import HostedAgentAuthenticationError
from gpt_rag_ui.clients.native_hosted import NativeHostedClient, HostedToolFailure
from gpt_rag_ui.clients.native_attachments import InlineAttachment, MAX_ATTACHMENT_BYTES, MAX_ATTACHMENTS
from gpt_rag_ui.clients.native_metadata import CosmosMetadata, MemoryMetadata, MetadataConflict
from gpt_rag_ui.config.dependencies import get_config
from gpt_rag_ui.config.native import load_native_settings
from gpt_rag_ui.services.citations import replace_source_reference_links, format_hosted_citation_sources
from gpt_rag_ui.services.native_presentation import presentations, text_projection
from gpt_rag_ui.services.native import (
    NativeService, NativePrincipal, NativeNotFound, NativeUnavailable,
    ResearchReviewStart, ReviewDecision,
)

logger = logging.getLogger(__name__)
_service: NativeService | None = None
_get_auth_info: Callable[[], Awaitable[dict[str, Any]]] | None = None


def get_service() -> NativeService:
    global _service
    if _service is None:
        config = get_config()
        settings = load_native_settings(config)
        if settings is None:
            raise NativeUnavailable("Native UI is not configured.")
        metadata = CosmosMetadata(settings, credential=config.aiocredential) if settings.persistent_metadata else MemoryMetadata()
        if settings.host == "hosted":
            _service = NativeService(settings, metadata, hosted=NativeHostedClient(settings), config=config)
        else:
            from gpt_rag_ui.clients.native_durable import NativeDurableClient
            _service = NativeService(settings, metadata, durable=NativeDurableClient(settings), config=config)
    return _service


async def principal() -> NativePrincipal:
    assert _get_auth_info is not None
    auth = await _get_auth_info()
    if not auth.get("authorized") or auth.get("copilot_auth_mode") == "anonymous" or not auth.get("access_token"):
        raise PermissionError("Entre para continuar.")
    # This context comes from the existing authenticated session/OAuth refresh boundary,
    # not Action payload, headers, or a service identity.
    return NativePrincipal(
        auth.get("tenant_id", ""), auth.get("object_id", ""), auth["access_token"],
        auth.get("client_principal_name", ""),
    )


def text_output(value: Any) -> str:
    """Render native text/result shapes without executing HTML/model content."""
    if isinstance(value, str) and value.lstrip().startswith("{"):
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and parsed.get("status") in {"approved", "rejected"} and isinstance(parsed.get("draft"), str):
            return text_projection(parsed)
    return text_projection(value)


async def display_output(id: str, value: Any) -> str:
    """Reuse protected citation grants with the current authenticated session binding."""
    assert _get_auth_info is not None
    auth = await _get_auth_info()
    user = await principal()
    await get_service().owned(user, id)
    binding = {
        "conversation_id": id, "principal_id": user.owner,
        "copilot_session_id": auth.get("copilot_session_id", ""),
    }
    references: set[str] = set()
    content = replace_source_reference_links(text_output(value), references, **binding)

    def collect_annotations(item: Any, admitted: bool = False) -> list[dict[str, Any]]:
        if isinstance(item, list):
            return [citation for child in item for citation in collect_annotations(child, admitted)]
        if not isinstance(item, dict):
            return []
        if item.get("type") in ("progress", "response.in_progress", "workflow_progress"):
            return []
        if "role" in item:
            if item["role"] != "assistant":
                return []
            admitted = item.get("type") in (None, "message")
        found = [
            citation for citation in item.get("annotations", []) or []
            if admitted and isinstance(citation, dict) and citation.get("type") == "url_citation"
        ]
        for key in ("output", "messages", "content", "contents", "items", "response", "data", "conversationHistory"):
            if key in item:
                found.extend(collect_annotations(item[key], admitted))
        return found

    def annotations(item: Any) -> list[dict[str, Any]]:
        return collect_annotations(item)

    return content + format_hosted_citation_sources(annotations(value), references, **binding)


def visual_elements(value: Any) -> list[cl.CustomElement]:
    """Optional element failures must not discard already admitted readable text."""
    try:
        return [cl.CustomElement(name="Presentation", props=p, display="inline")
                for p in presentations(value) if p["contributions"]]
    except (TypeError, ValueError, RuntimeError):
        logger.warning("Native presentation unavailable", extra={"reason": "element_construction"})
        return []


async def render_progress(id: str, progress: dict[str, Any]) -> None:
    if progress["status"] == "conversation":
        # Restore from the existing owner-admitted native history, never a
        # visual-specific source query or duplicate transcript store.
        history = await get_service().history(await principal(), id)
        content = await display_output(id, history)
        elements = visual_elements(history)
        await cl.Message(content=content or "Nenhuma mensagem salva.", elements=elements).send()
        return
    pending = progress["pending"]
    actions = [cl.Action(name="native_refresh", payload={"id": id}, label="Atualizar andamento")]
    if len(pending) == 1:
        payload = {"id": id, "request_id": pending[0]["request_id"], "revision": progress["revision"]}
        actions.extend([
            cl.Action(name="native_approve", payload=payload, label="Aprovar"),
            cl.Action(name="native_reject", payload=payload, label="Rejeitar"),
        ])
    description = text_output(progress["output"])
    if pending:
        description = text_output(pending[0].get("data")) or json.dumps(pending[0].get("data"), ensure_ascii=False)
    description = await display_output(id, description)
    visual_result = pending[0].get("data") if pending else progress["output"]
    elements = visual_elements(visual_result)
    await cl.Message(
        content=(
            f"Processo: **{progress['status']}**\n\n{description}\n\n"
            f"Para retomar após reconectar, envie `/reabrir {id}`."
        ),
        actions=actions,
        elements=elements,
    ).send()


async def on_start() -> None:
    service = get_service()
    user = await principal()
    settings = await service.current(user)
    profile = cl.user_session.get("chat_profile")
    application = "workflow" if settings.mode == "workflow" or profile == settings.workflow_title else "conversational"
    row = await service.create(user, application)
    cl.user_session.set("native_session_id", row["id"])
    await cl.Message(content=(
        f"**{row['title']}**\n\nDescreva o tema para iniciar o processo."
        if application == "workflow" else "**Conversar**"
    )).send()
    if application == "conversational" and settings.private_uploads:
        await cl.Message(content=(
            "Anexos privados: TXT/MD, PDF, PNG/JPEG; até 3 arquivos de 64 KiB cada. "
            "O conteúdo serializado do turno deve caber em 256 KiB. "
            "Inclua uma mensagem de texto; anexos não são publicados no índice corporativo."
        )).send()
    if settings.history:
        await cl.Message(content="Sessões salvas", actions=[
            cl.Action(name="native_history", payload={}, label="Abrir histórico"),
        ]).send()


async def on_resume(thread: ThreadDict) -> None:
    # Thread IDs remain selectors; no client-supplied userIdentifier grants access.
    try:
        user = await principal()
        await get_service().resume(user, thread["id"])
        cl.user_session.set("native_session_id", thread["id"])
        await render_progress(thread["id"], await get_service().progress(user, thread["id"]))
    except (DelegationError, HostedToolFailure) as exc:
        await cl.Message(content=str(exc)).send()


async def handle_message(message: cl.Message) -> None:
    try:
        service, user = get_service(), await principal()
        if message.content.startswith("/reabrir "):
            id = message.content[len("/reabrir "):].strip()
            progress = await service.resume(user, id)
            cl.user_session.set("native_session_id", id)
            await render_progress(id, progress)
            return
        id = cl.user_session.get("native_session_id")
        if not id:
            raise NativeNotFound("Abra uma sessão.")
        row = await service.owned(user, id)
        attachments = []
        if message.elements:
            if row["application"] != "conversational" or not (await service.current(user)).private_uploads:
                raise NativeUnavailable("Anexos privados não estão habilitados para esta experiência.")
            attachments = await read_session_attachments(message.elements)
        if row["application"] == "workflow":
            if row["native_id"]:
                await render_progress(id, await service.progress(user, id))
            else:
                await render_progress(id, await service.start(user, id, ResearchReviewStart(topic=message.content)))
        else:
            turn = await service.converse(user, id, message.content, attachments)
            content = await display_output(id, turn["response"])
            if not content:
                raise NativeUnavailable("O runtime não retornou texto de conversa.")
            actions = []
            if (await service.current(user)).feedback:
                actions = [cl.Action(
                    name="native_feedback", label=label,
                    payload={"id": id, "rating": rating, "native_ref": turn["native_ref"]},
                ) for rating, label in ((1, "Útil"), (-1, "Não útil"))]
            elements = visual_elements(turn["response"])
            await cl.Message(content=content, actions=actions, elements=elements).send()
    except (DelegationError, HostedAgentAuthenticationError) as exc:
        if not await offer_auth_recovery(exc.code, exc.claims_challenge, user=cl.user_session.get("user")):
            await cl.Message(content=str(exc)).send()
    except (PermissionError, NativeNotFound, NativeUnavailable, MetadataConflict, HostedToolFailure) as exc:
        await cl.Message(content=str(exc)).send()
    except (ValueError, OSError) as exc:
        logger.warning("Native input denied (type=%s)", type(exc).__name__)
        await cl.Message(content="Entrada inválida. Verifique o texto, tipo e tamanho dos anexos.").send()
    except (AzureError, OpenAIError, RpcError, TimeoutError, RuntimeError) as exc:
        logger.error("Native chat failed (type=%s)", type(exc).__name__)
        await cl.Message(content="Não foi possível concluir. Atualize antes de tentar novamente.").send()


async def handle_action(action: cl.Action) -> None:
    try:
        service, user = get_service(), await principal()
        payload = action.payload
        id = payload.get("id", "")
        if action.name == "native_history":
            items = await service.list_sessions(user)
            await cl.Message(content="Histórico" if items else "Nenhuma sessão salva.", actions=[
                cl.Action(name="native_reopen", label=item["title"], payload={"id": item["id"]})
                for item in items
            ]).send()
        elif action.name == "native_reopen":
            row = await service.owned(user, id)
            if not (await service.current(user)).history:
                raise NativeUnavailable("Histórico desativado.")
            cl.user_session.set("native_session_id", id)
            if row["application"] == "conversational":
                history = await service.history(user, id)
                content = await display_output(id, history) or "Sessão aberta."
                elements = visual_elements(history)
                await cl.Message(content=content, elements=elements).send()
            else:
                await render_progress(id, await service.progress(user, id))
        elif action.name == "native_feedback":
            await service.feedback(user, id, payload["rating"], "", payload["native_ref"])
            await cl.Message(content="Feedback salvo.").send()
        elif action.name in {"native_approve", "native_reject"}:
            result = await service.reply(
                user, id, payload["request_id"], payload["revision"],
                ReviewDecision(decision="approve" if action.name == "native_approve" else "reject"),
            )
            await render_progress(id, result)
        elif action.name == "native_refresh":
            await render_progress(id, await service.progress(user, id))
        else:
            raise ValueError("Ação não suportada.")
    except (DelegationError, HostedAgentAuthenticationError) as exc:
        if not await offer_auth_recovery(exc.code, exc.claims_challenge, user=cl.user_session.get("user")):
            await cl.Message(content=str(exc)).send()
    except HostedToolFailure as exc:
        await cl.Message(content=str(exc)).send()
    except (PermissionError, NativeNotFound, NativeUnavailable, MetadataConflict, ValueError, KeyError) as exc:
        await cl.Message(content="Ação indisponível. Atualize a sessão.").send()
        logger.warning("Native action denied (type=%s)", type(exc).__name__)
    except (AzureError, OpenAIError, RpcError, TimeoutError, RuntimeError) as exc:
        logger.error("Native action failed (type=%s)", type(exc).__name__)
        await cl.Message(content="A ação falhou. Atualize antes de tentar novamente.").send()


def register(get_auth_info: Callable[[], Awaitable[dict[str, Any]]]) -> None:
    global _get_auth_info
    _get_auth_info = get_auth_info

    @cl.set_chat_profiles
    async def profiles(current_user: cl.User | None) -> list[cl.ChatProfile]:
        settings = get_service().settings
        items = []
        if settings.conversational:
            items.append(cl.ChatProfile(name="Conversar", markdown_description="Conversa com o agente."))
        if settings.workflow:
            items.append(cl.ChatProfile(name=settings.workflow_title, markdown_description="Pesquisa com revisão humana."))
        return items

    cl.on_chat_start(on_start)
    cl.on_chat_resume(on_resume)
    cl.on_message(handle_message)
    for name in (
        "native_refresh", "native_approve", "native_reject", "native_history",
        "native_reopen", "native_feedback",
    ):
        cl.action_callback(name)(handle_action)


async def read_session_attachments(elements: list[Any]) -> list[InlineAttachment]:
    """Only Chainlit's authenticated session file registry may supply local paths."""
    if len(elements) > MAX_ATTACHMENTS:
        raise ValueError("At most three attachments.")
    from chainlit.context import context
    session = context.session
    result = []
    for element in elements:
        path = Path(element.path or "").resolve()
        registered = next((file for file in session.files.values() if Path(file["path"]).resolve() == path), None)
        if (
            registered is None or not path.is_relative_to(session.files_dir.resolve())
            or path.is_symlink() or not path.is_file()
            or path.stat().st_size > MAX_ATTACHMENT_BYTES
        ):
            raise PermissionError("Attachment is not owned by this authenticated session.")
        def bounded_read() -> bytes:
            with path.open("rb") as stream:
                return stream.read(MAX_ATTACHMENT_BYTES + 1)
        data = await asyncio.to_thread(bounded_read)
        attachment = InlineAttachment(
            name=registered["name"], content_type=registered["type"],
            data_base64=base64.b64encode(data).decode("ascii"),
        )
        attachment.decode()
        result.append(attachment)
    return result
