"""Recht „Chats lesen“ (chat.read): Rollen, die Modelle fragen dürfen (chat.ask),
bekommen es dazu. Keys behalten ihre Rechte – chat.read muss am Key gewählt werden."""

from django.db import migrations


def add_chat_read(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    for role in Role.objects.all():
        scopes = list(role.api_scopes or [])
        if "chat.ask" in scopes and "chat.read" not in scopes:
            scopes.insert(scopes.index("chat.ask") + 1, "chat.read")
            role.api_scopes = scopes
            role.save(update_fields=["api_scopes"])


def remove_chat_read(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    for role in Role.objects.all():
        scopes = [s for s in (role.api_scopes or []) if s != "chat.read"]
        if scopes != list(role.api_scopes or []):
            role.api_scopes = scopes
            role.save(update_fields=["api_scopes"])


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0012_avatar"),
        ("node", "0004_apikey_collect_in_project"),
    ]

    operations = [migrations.RunPython(add_chat_read, remove_chat_read)]
