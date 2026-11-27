"""Fähigkeiten des Runners aus dem signierten Dienstkatalog (z. B. ``data``)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("runner", "0004_chatworkspace_packages"),
    ]

    operations = [
        migrations.AddField(
            model_name="runner",
            name="features",
            field=models.JSONField(blank=True, default=list, verbose_name="Fähigkeiten"),
        ),
    ]
