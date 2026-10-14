import asyncio
import base64
import json
import logging
import os
import uuid
from typing import Optional

import httpx

from gpt_rag_ui.config.appconfig import (
    ATTACHMENTS_UNAVAILABLE_MESSAGE,
    load_ingestion_uploads_enabled,
)
from gpt_rag_ui.config.dependencies import get_config
from gpt_rag_ui.clients.orchestrator_client import (
    _format_outgoing_request_debug,
    _get_config_value,
    _get_dapr_api_token,
    _get_orchestrator_base_url,
    _headers_summary,
)

logger = logging.getLogger("gpt_rag_ui.ingestion_client")

_MAX_INGEST_FILES = 5
_MAX_INGEST_FILE_BYTES = 15 * 1024 * 1024
_MAX_INGEST_TOTAL_BYTES = 25 * 1024 * 1024


def _get_ingestion_base_url() -> Optional[str]:
    value = os.getenv("INGESTION_BASE_URL")
    if value:
        return value.rstrip("/")

    value = _get_config_value("INGESTION_BASE_URL", default=None, allow_none=True)
    if value:
        return str(value).rstrip("/")

    orch = _get_orchestrator_base_url()
    if orch and "-orchestrator" in orch:
        derived = orch.replace("-orchestrator", "-dataingest", 1)
        logger.debug("INGESTION_BASE_URL not set; derived from orchestrator host: %s", derived)
        return derived

    return None


def _build_ingestion_url() -> tuple[str, dict]:
    """Return (url, context) for POST /ingest-documents (direct HTTP or Dapr invoke)."""
    ingestion_app_id = "ingestion"
    ingest_method_path = "ingest-documents"
    base_url = _get_ingestion_base_url()
    if base_url:
        url = base_url if base_url.endswith(f"/{ingest_method_path}") else f"{base_url}/{ingest_method_path}"
        return url, {"mode": "direct", "base_url": base_url, "dapr_port": None, "app_id": ingestion_app_id}

    dapr_port = _get_config_value("DAPR_HTTP_PORT", default="3500")
    url = f"http://127.0.0.1:{dapr_port}/v1.0/invoke/{ingestion_app_id}/method/{ingest_method_path}"
    return url, {"mode": "dapr", "base_url": None, "dapr_port": str(dapr_port), "app_id": ingestion_app_id}


def _hint_for_ingestion_connect_error(context: dict) -> str:
    if context.get("mode") == "dapr":
        return (
            "Connection failed to local Dapr sidecar (ingestion). Ensure Dapr is running on "
            f"127.0.0.1:{context.get('dapr_port')} and app-id '{context.get('app_id')}' is registered. "
            "If you are not using Dapr locally, set INGESTION_BASE_URL to the ingestion HTTP root "
            "(e.g. Azure Container App URL without trailing slash)."
        )
    return (
        "Connection failed to ingestion service. Verify INGESTION_BASE_URL, network access, and that the "
        "ingestion service is healthy and reachable from this container."
    )


def _read_file_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _ingest_response_failure_detail(body: object) -> Optional[str]:
    if not isinstance(body, dict):
        return "Ingestion response is not a JSON object"
    values = body.get("values")
    if not isinstance(values, list):
        return "Ingestion response missing or invalid 'values' array"

    messages: list[str] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        errs = item.get("errors") or []
        if not errs:
            continue
        rid = item.get("recordId", "?")
        for err in errs:
            if isinstance(err, dict):
                msg = err.get("message", str(err))
            else:
                msg = str(err)
            messages.append(f"recordId={rid}: {msg}")

    if not messages:
        return None
    cap = 20
    tail = " ..." if len(messages) > cap else ""
    return "; ".join(messages[:cap]) + tail


async def _build_ingest_documents_payload(conversation_id: str, question_id: str, files: list[dict], auth_info: dict | None = None) -> dict:
    cid = (conversation_id or "").strip() or (question_id or "").strip() or str(uuid.uuid4())

    to_process = files[:_MAX_INGEST_FILES]
    if len(files) > _MAX_INGEST_FILES:
        logger.warning(
            "Ingestion allows at most %d files per request; %d provided, processing the first %d only",
            _MAX_INGEST_FILES,
            len(files),
            _MAX_INGEST_FILES,
        )

    total_size = 0
    values: list[dict] = []
    for idx, fdict in enumerate(to_process):
        path = fdict.get("path")
        name = fdict.get("name") or fdict.get("fileName") or "upload"
        mime = fdict.get("mime") or fdict.get("contentType") or "application/octet-stream"

        if not path:
            raise RuntimeError(f"Ingestion: missing file path for {name!r}")

        declared_size = fdict.get("size")
        if isinstance(declared_size, int) and declared_size > 0:
            if declared_size > _MAX_INGEST_FILE_BYTES:
                raise RuntimeError(
                    f"Ingestion: file too large ({declared_size} bytes). Max per file is {_MAX_INGEST_FILE_BYTES}."
                )
            total_size += declared_size
            if total_size > _MAX_INGEST_TOTAL_BYTES:
                raise RuntimeError(
                    f"Ingestion: total upload too large ({total_size} bytes). Max total is {_MAX_INGEST_TOTAL_BYTES}."
                )

        try:
            raw = await asyncio.to_thread(_read_file_bytes, path)
        except OSError as e:
            raise RuntimeError(f"Ingestion: cannot read file {name!r}: {e}") from e

        if len(raw) > _MAX_INGEST_FILE_BYTES:
            raise RuntimeError(
                f"Ingestion: file too large ({len(raw)} bytes). Max per file is {_MAX_INGEST_FILE_BYTES}."
            )

        b64 = base64.standard_b64encode(raw).decode("ascii")
        record_id = (fdict.get("recordId") or "").strip()
        if not record_id:
            q = question_id or "q"
            record_id = f"{q}-{idx}"

        values.append(
            {
                "recordId": record_id,
                "data": {
                    "fileName": name,
                    "contentType": mime,
                    "fileBase64": b64,
                },
            }
        )

    payload: dict = {"conversationId": cid, "values": values}

    # Stamp the uploader's object id as the document ACL so that, when the
    # search index has permission trimming enabled, the uploader can retrieve
    # their own uploaded chunks (issue #478). Skip anonymous/placeholder ids.
    _ACL_PLACEHOLDERS = {"", "no-auth", "anonymous", "00000000-0000-0000-0000-000000000000"}
    uploader_oid = str(
        (auth_info or {}).get("object_id")
        or (auth_info or {}).get("client_principal_id")
        or ""
    ).strip()
    if uploader_oid.lower() not in _ACL_PLACEHOLDERS:
        try:
            uploader_oid = str(uuid.UUID(uploader_oid))
        except ValueError:
            logger.warning(
                "Ingestion ACL omitted because the uploader identity is not an oid"
            )
        else:
            payload["securityUserIds"] = [uploader_oid]

    return payload


async def ingest_files_session(conversation_id: str, question_id: str, auth_info: dict, files: list[dict]) -> bool:
    if not load_ingestion_uploads_enabled(get_config()):
        raise RuntimeError(ATTACHMENTS_UNAVAILABLE_MESSAGE)

    url, target_context = _build_ingestion_url()
    headers = {"Content-Type": "application/json"}

    dapr_token = _get_dapr_api_token()
    if dapr_token:
        headers["dapr-api-token"] = dapr_token
    else:
        logger.debug("DAPR_API_TOKEN not set; omitting 'dapr-api-token' header for ingestion")

    # Prefer the canonical infra-provided key (DATA_INGEST_APP_APIKEY, generated from
    # canonical_name=DATA_INGEST_APP in main.parameters.json); fall back to legacy aliases.
    api_key = _get_config_value("DATA_INGEST_APP_APIKEY", default="")
    if not api_key:
        api_key = _get_config_value("INGESTION_APP_APIKEY", default="")
    if not api_key:
        api_key = _get_config_value("ORCHESTRATOR_APP_APIKEY", default="")
    if api_key:
        headers["X-API-KEY"] = api_key

    access_token = auth_info.get("access_token")
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    payload = await _build_ingest_documents_payload(conversation_id, question_id, files, auth_info)

    logger.info(
        "Invoking ingestion: question_id=%s conversation_id=%s mode=%s url=%s headers=%s files_count=%d",
        question_id or "n/a",
        conversation_id or "new",
        target_context.get("mode"),
        url,
        _headers_summary(headers),
        len(payload.get("values") or []),
    )

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "Outgoing ingestion request (sanitized):\n%s",
            _format_outgoing_request_debug(method="POST", url=url, headers=headers, json_body=payload),
        )

    timeout = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code >= 400:
                body_text = response.text
                snippet = (body_text[:2000] + "...") if len(body_text) > 2000 else body_text
                raise RuntimeError(
                    f"Ingestion call failed (HTTP {response.status_code} {response.reason_phrase}). "
                    f"url={url} details={snippet}"
                )
            try:
                body = response.json()
            except json.JSONDecodeError as e:
                raise RuntimeError(f"Ingestion returned non-JSON body (HTTP {response.status_code}). url={url}") from e

            failure = _ingest_response_failure_detail(body)
            if failure:
                raise RuntimeError(f"Ingestion completed with errors. {failure}")
            return True
    except httpx.ConnectError as e:
        hint = _hint_for_ingestion_connect_error(target_context)
        raise RuntimeError(f"Ingestion connection failed. {hint}") from e
    except httpx.TimeoutException as e:
        raise RuntimeError(f"Ingestion request timed out. url={url}") from e
    except httpx.HTTPError as e:
        raise RuntimeError(f"Ingestion HTTP error. url={url} error={e}") from e
