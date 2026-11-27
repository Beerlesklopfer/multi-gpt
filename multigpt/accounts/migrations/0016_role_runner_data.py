"""Volumen /data je Runner (Nutzer, 2026-10-14; umgesetzt 2026-11-27).

Grenze des Volumens ``multigpt-data`` je Rolle (``runner_data_mb``, Vorgabe 2 GB,
0 = kein /data).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0015_role_runner_packages"),
    ]

    operations = [
        migrations.AddField(
            model_name="role",
            name="runner_data_mb",
            field=models.PositiveIntegerField(
                default=2048,
                help_text="Volumen /data auf dem Rechner des Runners: bleibt über Sitzungen hinweg (Caches, venv, node_modules, Build-Ausgaben), wird nicht gesichert. Darüber lehnt der Runner Schreiben nach /data ab. 0 = kein /data.",
                verbose_name="Runner: Volumen /data (MB)",
            ),
        ),
    ]
