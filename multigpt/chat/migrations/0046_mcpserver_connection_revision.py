from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("chat", "0045_load_parallel_default")]

    operations = [
        migrations.AddField(
            model_name="mcpserver",
            name="connection_revision",
            field=models.PositiveBigIntegerField(default=0, editable=False),
        ),
    ]
