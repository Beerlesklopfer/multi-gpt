"""Profilbild (Avatar) eines Kontos: Aufbereitung, Initialen und Farbe.

Hochgeladene Bilder werden wie Anhänge geprüft (Format, Pixelgrenze, kaputte
Dateien – ``chat.attachments``), nach EXIF gedreht, mittig quadratisch
zugeschnitten, auf ``AVATAR_EDGE`` Pixel verkleinert und ohne Metadaten als WebP
neu kodiert. Ohne Bild zeigt die Oberfläche die Initialen auf einer Farbe, die
fest zum Konto gehört (aus der ID, nicht aus dem Namen).
"""

from __future__ import annotations

import io
import logging

from PIL import Image, ImageOps

from multigpt.chat import attachments as chat_attachments

logger = logging.getLogger(__name__)

AVATAR_EDGE = 256
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# Hintergrundfarben für Initialen: dunkel genug für weiße Schrift (Kontrast ≥ 4.5:1).
COLORS = ("#2f6fdb", "#1a7f37", "#b42318", "#8250df", "#b35900", "#0b7285", "#c2255c", "#5c6370")


class AvatarError(ValueError):
    """Bild nicht verwendbar; Meldung deutsch für die Oberfläche."""


def process(fileobj, size: int | None = None) -> bytes:
    """Hochgeladenes Bild -> quadratisches WebP (``AVATAR_EDGE`` px) ohne Metadaten."""
    if size is not None and size > MAX_UPLOAD_BYTES:
        raise AvatarError("Das Bild ist zu groß (höchstens 10 MB).")
    try:
        image = chat_attachments._open_image(fileobj)
    except chat_attachments.UploadError as exc:
        raise AvatarError(exc.message) from None
    try:
        image = ImageOps.exif_transpose(image) or image
    except Exception:  # noqa: BLE001 - kaputte EXIF-Daten: ohne Drehung weiter
        logger.info("Profilbild: EXIF-Drehung nicht möglich")
    image = chat_attachments._without_metadata(image, "WEBP")
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA" if "A" in image.mode else "RGB")
    image = ImageOps.fit(image, (AVATAR_EDGE, AVATAR_EDGE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, "WEBP", quality=85)
    return out.getvalue()


def initials(user) -> str:
    """Bis zu zwei Buchstaben aus Anzeigename bzw. Vor-/Nachname bzw. Benutzername."""
    name = (
        getattr(user, "display_name", "")
        or " ".join(filter(None, [user.first_name, user.last_name]))
        or user.get_username()
    )
    parts = [p for p in name.replace("-", " ").replace(".", " ").split() if p[:1].isalnum()]
    if len(parts) >= 2:
        return (parts[0][0] + parts[-1][0]).upper()
    return (parts[0][:2] if parts else "?").upper()


def color(user) -> str:
    return COLORS[(user.pk or 0) % len(COLORS)]
