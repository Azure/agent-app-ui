"""Bounded private native input; no ingestion, public URL, SAS or corporate index."""

import base64
import binascii
import json
from pathlib import PurePath

from agent_framework import Content, Message
from pydantic import BaseModel, ConfigDict, Field

MAX_ATTACHMENT_BYTES = 64 * 1024
MAX_ATTACHMENTS = 3
# The published DTS worker has a 1 MiB serialized-state budget. Leave room for
# request/session framing, response delivery and native history. This is input
# admission, not a replacement for the SDK's state-capacity/retention policies.
MAX_INLINE_TURN_BYTES = 256 * 1024
ATTACHMENT_TYPES = {
    ".txt": "text/plain", ".md": "text/markdown", ".pdf": "application/pdf",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
}


class InlineAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=128)
    content_type: str = Field(min_length=1, max_length=64)
    data_base64: str = Field(min_length=1, max_length=4 * ((MAX_ATTACHMENT_BYTES + 2) // 3))

    def decode(self) -> bytes:
        if (
            self.name != PurePath(self.name).name or "/" in self.name or "\\" in self.name
            or any(ord(c) < 32 for c in self.name) or self.name.startswith(".")
            or ATTACHMENT_TYPES.get(PurePath(self.name).suffix.lower()) != self.content_type
        ):
            raise ValueError("Unsupported attachment filename or content type.")
        try:
            data = base64.b64decode(self.data_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Attachment must contain valid base64.") from exc
        if not data or len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError("Each attachment must contain 1–65536 bytes.")
        if self.content_type.startswith("text/"):
            data.decode("utf-8")
        elif self.content_type == "application/pdf" and not data.startswith(b"%PDF-"):
            raise ValueError("Invalid PDF signature.")
        elif self.content_type == "image/png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Invalid PNG signature.")
        elif self.content_type == "image/jpeg" and not data.startswith(b"\xff\xd8\xff"):
            raise ValueError("Invalid JPEG signature.")
        return data

    @property
    def data_uri(self) -> str:
        return f"data:{self.content_type};base64,{self.data_base64}"

    def hosted_content(self) -> dict[str, str]:
        if self.content_type.startswith("image/"):
            return {"type": "input_image", "image_url": self.data_uri}
        return {"type": "input_file", "filename": self.name, "file_data": self.data_uri}

    def durable_content(self) -> Content:
        if self.content_type.startswith("text/"):
            return Content.from_text(f"[File: {self.name}]\n{self.decode().decode('utf-8')}")
        kind = "input_image" if self.content_type.startswith("image/") else "input_file"
        # Metadata is the official core/OpenAI file-content envelope, not a UI protocol.
        return Content.from_uri(
            self.data_uri, name=self.name, media_type=self.content_type,
            additional_properties={
                "openai": {"content_type": kind}, "openai_content_type": kind, "filename": self.name,
            },
        )


def validate_inline_turn(text: str, attachments: list[InlineAttachment]) -> list[bytes]:
    """Validate the whole batch and both native content envelopes before writes."""
    if len(attachments) > MAX_ATTACHMENTS:
        raise ValueError("At most three private attachments per turn.")
    decoded = [attachment.decode() for attachment in attachments]
    if not attachments:
        return decoded
    hosted = [{"role": "user", "content": [
        {"type": "input_text", "text": text},
        *(attachment.hosted_content() for attachment in attachments),
    ]}]
    durable = Message("user", [
        Content.from_text(text), *(attachment.durable_content() for attachment in attachments),
    ]).to_dict()
    # Include UTF-8/JSON escaping, file names/MIME, data URI expansion and the
    # core content metadata. Raw decoded bytes alone are not a native state size.
    size = max(len(json.dumps(value, ensure_ascii=True).encode("utf-8")) for value in (hosted, durable))
    if size > MAX_INLINE_TURN_BYTES:
        raise ValueError("Private inline turn exceeds 262144 serialized bytes; use fewer or smaller files.")
    return decoded
