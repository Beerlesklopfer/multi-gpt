"""Abbildungen im RAG (rag.figures): Einstellungen und Zähler je Dokument."""

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("chat", "0019_citation"),
    ]

    operations = [
        migrations.AddField(
            model_name="document",
            name="figures_described",
            field=models.PositiveIntegerField(default=0, verbose_name="Abbildungen beschrieben"),
        ),
        migrations.AddField(
            model_name="ragsettings",
            name="describe_figures",
            field=models.BooleanField(
                default=False,
                help_text="Bilder und Diagramme in PDF- und Word-Dokumenten sowie Bilddateien beschreibt ein Vision-Modell; die Beschreibung wird mit durchsucht. Kostet einen Modellaufruf je Abbildung (Dauer bzw. Gebühren). Lokales Modell (LM Studio): Die Bilder bleiben im Haus. Cloud-Modell: Die Bilder gehen an den Anbieter. Wirkt für vorhandene Dokumente erst nach „Alles neu indexieren“.",
                verbose_name="Abbildungen beschreiben",
            ),
        ),
        migrations.AddField(
            model_name="ragsettings",
            name="figure_max_edge",
            field=models.PositiveSmallIntegerField(
                default=1024,
                help_text="Längste Kante; größere Bilder werden vor dem Senden verkleinert.",
                validators=[
                    django.core.validators.MinValueValidator(256),
                    django.core.validators.MaxValueValidator(4096),
                ],
                verbose_name="Bildgröße für das Modell (Pixel)",
            ),
        ),
        migrations.AddField(
            model_name="ragsettings",
            name="figure_max_per_document",
            field=models.PositiveSmallIntegerField(
                default=50,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(1000),
                ],
                verbose_name="Abbildungen je Dokument (höchstens)",
            ),
        ),
        migrations.AddField(
            model_name="ragsettings",
            name="figure_max_per_page",
            field=models.PositiveSmallIntegerField(
                default=10,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(100),
                ],
                verbose_name="Abbildungen je Seite (höchstens)",
            ),
        ),
        migrations.AddField(
            model_name="ragsettings",
            name="figure_min_edge",
            field=models.PositiveSmallIntegerField(
                default=150,
                help_text="Kürzere Kante einer Abbildung; kleinere Bilder (Symbole, Logos) werden übersprungen, ebenso sehr schmale Linien und Wiederholungen.",
                validators=[
                    django.core.validators.MinValueValidator(16),
                    django.core.validators.MaxValueValidator(2000),
                ],
                verbose_name="Mindestgröße (Pixel)",
            ),
        ),
        migrations.AddField(
            model_name="ragsettings",
            name="figure_model",
            field=models.ForeignKey(
                blank=True,
                help_text="Allgemeines Vision-Modell, z. B. qwen/qwen3-vl-8b in LM Studio (olmOCR liest nur Text). Wird auch genutzt, wenn es für den Chat deaktiviert ist.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="chat.aimodel",
                verbose_name="Modell für Abbildungen",
            ),
        ),
    ]
