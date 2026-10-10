# Kontextmenü im Chat (M13-01): persönliche Einstellung, Standard an.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0010_role_api_access"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="context_menu",
            field=models.BooleanField(
                default=True,
                help_text="Markierter Text im Chat lässt sich zitieren, nachfragen und kopieren. Aus: überall das Browsermenü.",
                verbose_name="Eigenes Kontextmenü",
            ),
        ),
    ]
