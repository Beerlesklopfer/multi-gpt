"""Die private Mediendatei nach erfolgreichem Löschen des Eintrags entfernen."""

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import ScratchFile


@receiver(post_delete, sender=ScratchFile, dispatch_uid="scratchpad_delete_file")
def delete_file(sender, instance, **kwargs):
    if instance.file.name:
        storage, name = instance.file.storage, instance.file.name
        transaction.on_commit(lambda: storage.delete(name), robust=True)
