"""Bounded private native input; no ingestion, public URL, SAS or corporate index."""

import base64
import binascii
from pathlib import PurePath

from agent_framework import Content
from pydantic import BaseModel, ConfigDict, Field

MAX_ATTACHMENT_BYTES = 1024 * 1024
MAX_ATTACHMENTS = 3
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
            raise ValueError("Each attachment must contain 1–1048576 bytes.")
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
