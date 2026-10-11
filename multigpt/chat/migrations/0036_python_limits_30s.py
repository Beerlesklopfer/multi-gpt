"""Berechnungen: Standard-CPU-Zeit 30 s, Wanduhr 60 s (LOP A7).

Hebt nur Einstellungen an, die noch auf den alten Vorgaben 10 s / 20 s stehen –
vom Verwalter geänderte Werte bleiben unverändert.
"""

import django.core.validators
from django.db import migrations, models


def raise_defaults(apps, schema_editor):
    ChatSettings = apps.get_model("chat", "ChatSettings")
    ChatSettings.objects.filter(python_cpu_seconds=10).update(python_cpu_seconds=30)
    ChatSettings.objects.filter(python_wall_seconds=20).update(python_wall_seconds=60)


class Migration(migrations.Migration):
    dependencies = [("chat", "0035_model_management")]

    operations = [
        migrations.AlterField(
            model_name="chatsettings",
            name="python_cpu_seconds",
            field=models.PositiveSmallIntegerField(
                default=30,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(60),
                ],
                verbose_name="CPU-Zeit (s)",
            ),
        ),
        migrations.AlterField(
            model_name="chatsettings",
            name="python_wall_seconds",
            field=models.PositiveSmallIntegerField(
                default=60,
                help_text="Danach wird der Lauf abgebrochen (Wanduhr, auch beim Warten).",
                validators=[
                    django.core.validators.MinValueValidator(2),
                    django.core.validators.MaxValueValidator(120),
                ],
                verbose_name="Laufzeit gesamt (s)",
            ),
        ),
        migrations.RunPython(raise_defaults, migrations.RunPython.noop),
    ]
