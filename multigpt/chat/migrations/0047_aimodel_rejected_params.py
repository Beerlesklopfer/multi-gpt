from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("chat", "0046_mcpserver_connection_revision")]

    operations = [
        migrations.AddField(
            model_name="aimodel",
            name="rejected_params",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Parameter, die der Anbieter für dieses Modell abgelehnt hat "
                "(temperature, reasoning): MultiGPT sendet sie nicht mehr. Wird beim ersten HTTP "
                "400 gemerkt und bleibt über Neustarts; zum Neuprobieren leeren (z. B. nach einem "
                "Update des Anbieters).",
                verbose_name="Abgelehnte Parameter",
            ),
        ),
    ]
