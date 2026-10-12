"""Mitteilungen innerhalb von MultiGPT (A15, ``chat/notices.py``).

Eigenes Modul wie ``models_lmstudio``; eingebunden am Ende von
``chat/models.py``. Eine Mitteilung ist ein kurzer deutscher Text an ein Konto,
optional mit Verweis auf einen Chat. Der Text enthält vom Chat höchstens den
Titel (festgehalten, damit er auch nach dem Löschen des Chats lesbar bleibt).
"""

from django.conf import settings
from django.db import models


class Notice(models.Model):
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notices",
        verbose_name="Empfänger",
    )
    text = models.TextField("Text")
    # Leer bei gelöschtem Chat (Mitteilung „gelöscht“ verweist nie auf ihn).
    conversation = models.ForeignKey(
        "chat.Conversation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="Chat",
    )
    created = models.DateTimeField("erstellt", auto_now_add=True)
    read = models.DateTimeField("gelesen", null=True, blank=True)

    class Meta:
        ordering = ["-created", "-pk"]
        verbose_name = "Mitteilung"
        verbose_name_plural = "Mitteilungen"
        indexes = [
            models.Index(fields=["recipient", "read"], name="chat_notice_recipient_read"),
        ]

    def __str__(self):
        # Ohne Text: er nennt Chat-Titel (privat, wie Conversation.__str__).
        return f"Mitteilung {self.pk}"
