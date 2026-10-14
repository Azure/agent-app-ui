"""Citation rendering shared by live chat and restored history."""

import logging
import os
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional, Set

from gpt_rag_ui.clients.blob import BlobClient
from gpt_rag_ui.config.dependencies import get_config
from gpt_rag_ui.services.download_security import get_download_tokens, is_download_target_allowed
from gpt_rag_ui.util.constants import REFERENCE_REGEX
from gpt_rag_ui.telemetry.boundary_diagnostics import log_boundary_failure

logger = logging.getLogger("gpt_rag_ui.app")
config = get_config()
COPILOT_ENABLED = os.environ.get("CHAINLIT_COPILOT_ENABLED_EFFECTIVE", "").lower() == "true"
STORAGE_ACCOUNT_NAME = config.get("STORAGE_ACCOUNT_NAME", "", str)
IMAGE_EXTENSIONS = {"bmp", "jpeg", "jpg", "png", "tiff"}


def _normalize_container_name(container: Optional[str]) -> str:
    if not container:
        return ""
    return container.strip().strip("/")

DOCUMENTS_CONTAINER = _normalize_container_name(config.get("DOCUMENTS_STORAGE_CONTAINER", "", str))
IMAGES_CONTAINER = _normalize_container_name(config.get("DOCUMENTS_IMAGES_STORAGE_CONTAINER", "", str))
CONVERSATION_DOCUMENTS_CONTAINER = _normalize_container_name(
    config.get("CONVERSATION_DOCUMENTS_STORAGE_CONTAINER", "", str)
)
SHARED_DOWNLOAD_CONTAINERS = {
    _normalize_container_name(container)
    for container in (
        (os.environ.get("CITATION_SHARED_DOWNLOAD_CONTAINERS") or "").strip()
        or str(config.get("CITATION_SHARED_DOWNLOAD_CONTAINERS", "", str) or "")
    ).split(",")
    if _normalize_container_name(container)
}

def generate_blob_sas_url(
    container: str,
    blob_name: str,
    expiry_hours: int = 1,
) -> str:
    """Preserve the standalone direct-SAS citation behavior."""

    blob_url = (
        f"https://{STORAGE_ACCOUNT_NAME}.blob.core.windows.net/"
        f"{container}/{blob_name}"
    )
    blob_client = BlobClient(blob_url=blob_url)
    if not blob_client.exists():
        logger.info(
            "Blob not found: %s/%s - reference will be omitted",
            container,
            blob_name,
        )
        raise FileNotFoundError(f"Blob '{container}/{blob_name}' not found")

    from datetime import timezone

    expiry = datetime.now(timezone.utc) + timedelta(hours=expiry_hours)
    try:
        return blob_client.generate_sas_url(
            expiry=expiry,
            permissions="r",
        )
    except AttributeError:
        logger.warning(
            "SAS generation not supported, using direct blob URL for %s/%s",
            container,
            blob_name,
        )
        return blob_url

def _normalize_same_account_blob_href(href: str) -> Optional[str]:
    """Map an absolute URL that points at the solution's own storage account
    back to a container-relative path, so the regular citation resolution
    applies. Returns None when the URL is external or already signed."""
    if not STORAGE_ACCOUNT_NAME:
        return None

    split_href = urllib.parse.urlsplit(href)
    if split_href.scheme not in {"http", "https"}:
        return None

    host = split_href.netloc.split("@")[-1].split(":")[0].lower()
    if host != f"{STORAGE_ACCOUNT_NAME}.blob.core.windows.net".lower():
        return None
    if "sig=" in (split_href.query or "").lower():
        return None

    path = split_href.path.lstrip("/")
    if not path:
        return None

    fragment = f"#{split_href.fragment}" if split_href.fragment else ""
    return f"{path}{fragment}"

def _resolve_legacy_reference_href(raw_href: str) -> Optional[str]:
    href = (raw_href or "").strip()
    if not href:
        return None

    split_href = urllib.parse.urlsplit(href)
    if split_href.scheme or split_href.netloc:
        normalized = _normalize_same_account_blob_href(href)
        if normalized is None:
            return href
        href = normalized
        split_href = urllib.parse.urlsplit(href)
    if href.startswith("/api/download/") or href.startswith("api/download/"):
        return href

    path = urllib.parse.unquote(
        split_href.path.replace("\\", "/")
    ).lstrip("/")
    query = f"?{split_href.query}" if split_href.query else ""
    fragment = f"#{split_href.fragment}" if split_href.fragment else ""
    extension = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    container = DOCUMENTS_CONTAINER
    if extension in IMAGE_EXTENSIONS and IMAGES_CONTAINER:
        container = IMAGES_CONTAINER
    elif not container and IMAGES_CONTAINER:
        container = IMAGES_CONTAINER

    if (
        CONVERSATION_DOCUMENTS_CONTAINER
        and not (extension in IMAGE_EXTENSIONS and IMAGES_CONTAINER)
        and (
            path.startswith(f"{CONVERSATION_DOCUMENTS_CONTAINER}/")
            or path.startswith("conversations/")
        )
    ):
        if path.startswith(f"{CONVERSATION_DOCUMENTS_CONTAINER}/"):
            blob_name = path[len(CONVERSATION_DOCUMENTS_CONTAINER) + 1 :]
        else:
            blob_name = path
        container = CONVERSATION_DOCUMENTS_CONTAINER
    elif container and path.startswith(f"{container}/"):
        blob_name = path[len(container) + 1 :]
    else:
        blob_name = path

    if not blob_name:
        return None
    try:
        sas_url = generate_blob_sas_url(container, blob_name)
    except FileNotFoundError:
        logger.info(
            "Reference '%s' points to missing blob %s/%s",
            raw_href,
            container,
            blob_name,
        )
        return None
    except Exception:
        log_boundary_failure(
            logger,
            "Failed to build SAS URL for legacy reference; reference omitted",
            level=logging.WARNING,
        )
        return None

    if sas_url and (query or fragment):
        separator = "&" if "?" in sas_url else "?"
        return f"{sas_url}{separator}{query.lstrip('?')}{fragment}"
    return sas_url

def _resolve_secure_reference_href(
    raw_href: str,
    *,
    conversation_id: str,
    principal_id: str,
    copilot_session_id: str,
) -> Optional[str]:
    """Create an authenticated, session-bound absolute citation URL."""
    href = (raw_href or "").strip()
    if (
        not href
        or not conversation_id
        or not principal_id
        or not copilot_session_id
    ):
        return None

    split_href = urllib.parse.urlsplit(href)
    if split_href.scheme or split_href.netloc:
        normalized = _normalize_same_account_blob_href(href)
        if normalized is None:
            try:
                download_prefix = (
                    f"{get_download_tokens().public_url}/api/download/"
                )
            except RuntimeError:
                return None
            return href if href.startswith(download_prefix) else None
        href = normalized
        split_href = urllib.parse.urlsplit(href)

    if href.startswith("/api/download/") or href.startswith("api/download/"):
        return None

    path = urllib.parse.unquote(split_href.path.replace("\\", "/")).lstrip("/")
    fragment = f"#{split_href.fragment}" if split_href.fragment else ""
    if not path or any(part in {"", ".", ".."} for part in path.split("/")):
        return None

    extension = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    container = DOCUMENTS_CONTAINER
    if extension in IMAGE_EXTENSIONS and IMAGES_CONTAINER:
        container = IMAGES_CONTAINER
    elif not container and IMAGES_CONTAINER:
        container = IMAGES_CONTAINER

    blob_name: str
    # Per-conversation uploads (separate blob container), not used for image extensions.
    if (
        CONVERSATION_DOCUMENTS_CONTAINER
        and not (extension in IMAGE_EXTENSIONS and IMAGES_CONTAINER)
        and (
            path.startswith(f"{CONVERSATION_DOCUMENTS_CONTAINER}/")
            or path.startswith("conversations/")
        )
    ):
        if path.startswith(f"{CONVERSATION_DOCUMENTS_CONTAINER}/"):
            blob_name = path[len(CONVERSATION_DOCUMENTS_CONTAINER) + 1 :]
        else:
            blob_name = path
        container = CONVERSATION_DOCUMENTS_CONTAINER
    elif container:
        if path.startswith(f"{container}/"):
            blob_name = path[len(container)+1:]
        elif path:
            blob_name = path
        else:
            blob_name = ""
    else:
        blob_name = path

    if not blob_name:
        return None
    if not is_download_target_allowed(
        conversation_id=conversation_id,
        container=container,
        blob_name=blob_name,
        conversation_container=CONVERSATION_DOCUMENTS_CONTAINER,
        shared_containers=SHARED_DOWNLOAD_CONTAINERS,
    ):
        logger.warning(
            "Citation download omitted by container authorization policy: container=%s",
            container,
        )
        return None

    try:
        download_url = get_download_tokens().issue(
            principal_id=principal_id,
            session_id=copilot_session_id,
            conversation_id=conversation_id,
            container=container,
            blob_name=blob_name,
        )
    except (RuntimeError, ValueError):
        logger.warning(
            "Unable to issue an authenticated citation URL for '%s'",
            raw_href,
        )
        return None
    return f"{download_url}{fragment}"

def resolve_reference_href(
    raw_href: str,
    *,
    conversation_id: str = "",
    principal_id: str = "",
    copilot_session_id: str = "",
) -> Optional[str]:
    if not COPILOT_ENABLED or not copilot_session_id:
        return _resolve_legacy_reference_href(raw_href)
    return _resolve_secure_reference_href(
        raw_href,
        conversation_id=conversation_id,
        principal_id=principal_id,
        copilot_session_id=copilot_session_id,
    )

def replace_source_reference_links(
    text: str,
    references: Optional[Set[str]] = None,
    *,
    conversation_id: str = "",
    principal_id: str = "",
    copilot_session_id: str = "",
) -> str:
    """
    Replace source reference links in text. Links that point to non-existent blobs are completely removed.
    """
    def replacer(match):
        display_text = match.group(1)
        raw_href = match.group(2)
        # Resolve the original link into a signed blob URL when possible, otherwise drop it.
        resolved_href = resolve_reference_href(
            raw_href,
            conversation_id=conversation_id,
            principal_id=principal_id,
            copilot_session_id=copilot_session_id,
        )
        if resolved_href:
            if references is not None:
                references.add(resolved_href)
            logger.debug("Resolved reference '%s' -> '%s'", raw_href, resolved_href)
            return f"[{display_text}]({resolved_href})"
        if copilot_session_id:
            logger.debug(
                "Rendering citation '%s' without an unauthorized link",
                display_text,
            )
            return display_text
        logger.debug(
            "Omitting reference '[%s](%s)' - target not found",
            display_text,
            raw_href,
        )
        return ""

    return REFERENCE_REGEX.sub(replacer, text)

def format_hosted_citation_sources(
    citations: list[dict],
    references: Set[str],
    *,
    conversation_id: str,
    principal_id: str,
    copilot_session_id: str,
) -> str:
    """Render deduplicated hosted-agent citations as a Markdown "Sources"
    block, reused by both the classic and continuity-aware hosted-agent
    streaming branches."""
    citation_lines: list[str] = []
    seen_citations: set[tuple[str, str]] = set()
    for citation in citations:
        title = str(citation.get("title") or citation.get("citation_id") or "Source")
        url = str(citation.get("url") or "")
        citation_key = (title, url)
        if citation_key in seen_citations:
            continue
        seen_citations.add(citation_key)
        line = f"- [{title}]({url})" if url else f"- {title}"
        line = replace_source_reference_links(
            line,
            references,
            conversation_id=conversation_id,
            principal_id=principal_id,
            copilot_session_id=copilot_session_id,
        )
        if line.strip():
            citation_lines.append(line)
    if not citation_lines:
        return ""
    return "\n\n### Sources\n" + "\n".join(citation_lines)
