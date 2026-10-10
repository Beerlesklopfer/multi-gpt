"""Recht „API-Keys/MCP-Zugang“ und Höchstmenge der API-Rechte je Rolle (M15).

Neue Rollen: aus, keine Rechte (fail closed). Startrollen Verwalter und
Erwachsener: an, alle Rechte – ein Key entsteht trotzdem nur, wenn das Mitglied
ihn selbst anlegt. Jugendlicher und Gast: aus (der Verwalter kann es je Rolle
freischalten).
"""

from django.db import migrations, models

ENABLED_KEYS = ("admin", "adult")
ALL_SCOPES = [
    "chat.ask",
    "docs.read",
    "docs.write",
    "index.control",
    "files.read",
    "tools.run",
    "usage.read",
]


def enable(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    Role.objects.filter(key__in=ENABLED_KEYS).update(can_use_api=True, api_scopes=ALL_SCOPES)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0009_role_can_create_documents"),
    ]

    operations = [
        migrations.AddField(
            model_name="role",
            name="can_use_api",
            field=models.BooleanField(
                default=False,
                help_text="Mitglieder dürfen API-Keys anlegen, mit denen andere Programme MultiGPT über den MCP-Server steuern. Ein Key kann nie mehr als Rolle und Konto.",
                verbose_name="API-Keys/MCP-Zugang",
            ),
        ),
        migrations.AddField(
            model_name="role",
            name="api_scopes",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Höchstmenge der Rechte, die ein API-Key dieser Rolle haben kann. Wirksam ist die Schnittmenge mit den Rechten des Keys und den übrigen Rollenrechten.",
                verbose_name="Erlaubte API-Rechte",
            ),
        ),
        migrations.RunPython(enable, migrations.RunPython.noop),
    ]
