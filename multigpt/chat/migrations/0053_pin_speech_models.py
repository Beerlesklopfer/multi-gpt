"""Sprachmodelle (Spracherkennung, Sprachausgabe) anheften: Der Wächter im Worker
(``model_manager.keep_pinned_loaded``) hält sie geladen, damit Mikrofon und Vorlesen
nicht erst laden müssen. Wer eines nicht angeheftet haben will, löst es im Admin."""

from django.db import migrations


def pin(apps, schema_editor):
    AIModel = apps.get_model("chat", "AIModel")
    AIModel.objects.filter(capability__in=["stt", "tts"]).update(pinned=True)


class Migration(migrations.Migration):
    dependencies = [("chat", "0052_aimodel_intro")]

    operations = [migrations.RunPython(pin, migrations.RunPython.noop)]
