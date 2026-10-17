"""Dauerhafte private Ablage erzeugter Dateien, unabhängig vom Chat."""

import uuid
from pathlib import PurePath

from django.conf import settings
from django.db import models
from django.urls import reverse


def file_path(instance, filename):
    suffix = PurePath(filename).suffix.lower()[:10]
    return f"scratchpad/{instance.owner_id}/{uuid.uuid4().hex}{suffix}"


class ScratchFile(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    source_attachment = models.OneToOneField(
        "chat.Attachment", on_delete=models.SET_NULL, null=True, blank=True
    )
    name = models.CharField(max_length=200)
    mime_type = models.CharField(max_length=100, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    file = models.FileField(upload_to=file_path)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    tool_name = models.CharField(max_length=200, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created", "-pk"]

    def __str__(self):
        return self.name

    @property
    def url(self):
        return reverse("scratchpad:file", args=[self.pk])

    @property
    def is_image(self):
        return self.mime_type in {
            "image/svg+xml",
            "image/png",
            "image/jpeg",
            "image/webp",
            "image/gif",
        }
