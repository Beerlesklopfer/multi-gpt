"""Persönliche Zitier-Einstellungen am Konto (Stil, Kurzbeleg, Seite/Absatz)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0004_role_model_permissions"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="citation_locator",
            field=models.BooleanField(default=True, verbose_name="Seite/Absatz anzeigen"),
        ),
        migrations.AddField(
            model_name="user",
            name="citation_short",
            field=models.BooleanField(
                default=False,
                help_text="Statt nur [n] erscheint im Text der Kurzbeleg, z. B. (Müller 2024, S. 12).",
                verbose_name="Quellen im Antworttext als Kurzbeleg",
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="citation_style",
            field=models.CharField(
                choices=[
                    ("din", "DIN ISO 690"),
                    ("apa", "APA 7"),
                    ("harvard", "Harvard"),
                    ("chicago", "Chicago (Author-Date)"),
                    ("mla", "MLA 9"),
                ],
                default="din",
                max_length=10,
                verbose_name="Zitierstil",
            ),
        ),
    ]
