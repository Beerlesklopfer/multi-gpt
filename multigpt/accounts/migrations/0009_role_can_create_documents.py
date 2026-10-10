"""Recht „Dokumente erzeugen (PDF)“ (Werkzeug create_pdf).

Neue Rollen: aus (fail closed). Startrollen Verwalter, Erwachsener und
Jugendlicher: an – das PDF entsteht lokal ohne Netz- und Dateizugriff und
kostet nichts. Gast: aus (der Verwalter kann es freischalten).
"""

from django.db import migrations, models

ENABLED_KEYS = ("admin", "adult", "teen")


def enable(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    Role.objects.filter(key__in=ENABLED_KEYS).update(can_create_documents=True)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0008_role_can_compute"),
    ]

    operations = [
        migrations.AddField(
            model_name="role",
            name="can_create_documents",
            field=models.BooleanField(
                default=False,
                help_text="Modelle dürfen Blätter zum Ausdrucken als PDF erzeugen "
                "(Arbeitsblätter, Lineaturen, Briefe, Tabellen).",
                verbose_name="Dokumente erzeugen (PDF)",
            ),
        ),
        migrations.RunPython(enable, migrations.RunPython.noop),
    ]
