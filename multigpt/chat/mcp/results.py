"""Umwandlung der MCP-Werkzeugergebnisse in einfache Python-Objekte."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolFile:
    """Binärer Inhalt eines Werkzeugergebnisses (Bild, Audio, Datei)."""

    data: bytes = field(repr=False)
    mime_type: str
    name: str = ""


@dataclass(frozen=True)
class ToolResult:
    """Ergebnis eines Werkzeugaufrufs.

    ``text``: alle Textteile (Text, eingebettete Text-Ressourcen, Verweise),
    durch Leerzeilen getrennt; ohne Textteile die strukturierte Antwort als JSON.
    ``is_error``: Das Werkzeug meldet einen Ausführungsfehler (MCP ``isError``);
    der Text ist dann die Fehlermeldung für das Modell.
    ``images``: Bilder (``image``-Inhalte und ``image/*``-Blobs).
    ``files``: übrige Binärinhalte (Audio, Blobs).
    ``raw``: das Ergebnis als JSON (Spezifikationsnamen), Binärdaten ersetzt durch
    ``{"omitted_bytes": n}`` – geeignet für ``ToolCall.result``.
    """

    text: str
    is_error: bool = False
    images: list[ToolFile] = field(default_factory=list)
    files: list[ToolFile] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def _decode(data: str) -> bytes:
    try:
        return base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError):
        return b""


def _strip_binary(value: Any) -> Any:
    if isinstance(value, dict):
        binary_keys = {"blob"}
        if value.get("type") in ("image", "audio"):
            binary_keys.add("data")
        out = {}
        for key, item in value.items():
            if key in binary_keys and isinstance(item, str):
                out[key] = {"omitted_bytes": len(_decode(item))}
            elif key == "structuredContent":
                out[key] = item
            else:
                out[key] = _strip_binary(item)
        return out
    if isinstance(value, list):
        return [_strip_binary(item) for item in value]
    return value


def _file_name(uri: str) -> str:
    return uri.rstrip("/").rsplit("/", 1)[-1] if uri else ""


def convert_result(result) -> ToolResult:
    """Wandelt ein ``mcp_types.CallToolResult`` um."""
    texts: list[str] = []
    images: list[ToolFile] = []
    files: list[ToolFile] = []
    for block in result.content or []:
        kind = getattr(block, "type", "")
        if kind == "text":
            texts.append(block.text)
        elif kind == "image":
            images.append(ToolFile(_decode(block.data), block.mime_type or "image/png"))
        elif kind == "audio":
            files.append(ToolFile(_decode(block.data), block.mime_type or "audio/wav"))
        elif kind == "resource_link":
            label = block.title or block.name or block.uri
            line = f"[Ressource: {label}] {block.uri}"
            if block.description:
                line += f" – {block.description}"
            texts.append(line)
        elif kind == "resource":
            res = block.resource
            if getattr(res, "text", None) is not None:
                texts.append(res.text)
            elif getattr(res, "blob", None) is not None:
                mime = res.mime_type or "application/octet-stream"
                item = ToolFile(_decode(res.blob), mime, _file_name(str(res.uri)))
                (images if mime.startswith("image/") else files).append(item)
    if not texts and result.structured_content is not None:
        texts.append(json.dumps(result.structured_content, ensure_ascii=False))
    raw = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    return ToolResult(
        text="\n\n".join(texts),
        is_error=bool(result.is_error),
        images=images,
        files=files,
        raw=_strip_binary(raw),
    )
