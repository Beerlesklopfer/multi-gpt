"""Erzeugte Dateien als private Kopie im Server-Scratchpad sichern."""

import mimetypes

from django.core.files.base import ContentFile
from django.db import transaction

from multigpt.chat import svg_clean

from .models import ScratchFile

MAX_BYTES = 20 * 1024 * 1024


def store_attachment(attachment):
    existing = ScratchFile.objects.filter(source_attachment=attachment).first()
    if existing:
        return existing
    message = attachment.message
    owner = message.author or message.conversation.user
    mime = (
        attachment.mime_type
        or mimetypes.guess_type(attachment.file.name)[0]
        or "application/octet-stream"
    )
    with attachment.file.open("rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Datei ist größer als 20 MB.")
    width, height = attachment.width, attachment.height
    if mime == "image/svg+xml":
        data, width, height = svg_clean.clean(data)
    suffix = mimetypes.guess_extension(mime) or ".bin"
    item = ScratchFile(
        owner=owner,
        source_attachment=attachment,
        name=(attachment.original_name or f"werkzeug-{attachment.pk}{suffix}")[:200],
        mime_type=mime,
        size=len(data),
        width=width,
        height=height,
        tool_name=attachment.tool_call.tool if attachment.tool_call_id else "",
    )
    try:
        with transaction.atomic():
            item.file.save(f"ergebnis{suffix}", ContentFile(data), save=False)
            item.save()
    except Exception:
        if item.file.name:
            item.file.delete(save=False)
        raise
    return item
