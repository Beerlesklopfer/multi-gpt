"""Recht „Runner nutzen“ (runner.use, M14): Rollen mit API-Zugang und Rollenrecht
„Runner nutzen“ bekommen es in die Höchstmenge. Keys behalten ihre Rechte –
runner.use muss am Key gewählt werden."""

from django.db import migrations


def add_runner_use(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    for role in Role.objects.filter(can_use_api=True, can_use_runner=True):
        scopes = list(role.api_scopes or [])
        if "runner.use" not in scopes:
            scopes.append("runner.use")
            role.api_scopes = scopes
            role.save(update_fields=["api_scopes"])


def remove_runner_use(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    for role in Role.objects.all():
        scopes = [s for s in (role.api_scopes or []) if s != "runner.use"]
        if scopes != list(role.api_scopes or []):
            role.api_scopes = scopes
            role.save(update_fields=["api_scopes"])


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0013_role_runner"),
        ("node", "0005_chat_read_scope"),
    ]

    operations = [migrations.RunPython(add_runner_use, remove_runner_use)]
