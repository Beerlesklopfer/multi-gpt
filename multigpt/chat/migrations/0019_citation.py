"""Zitieren: Fundstelle von–bis an Abschnitten und Quellen, Literaturangaben.

- ``Chunk``/``SourceRef``: ``page_end``, ``paragraph``, ``paragraph_end`` (NULL
  bei Bestand; Absätze gibt es erst nach „Alles neu indexieren“). ``page`` ist
  künftig die erste Seite eines Abschnitts.
- ``SourceRef.biblio``: Literaturangaben zur Zeit der Antwort.
- ``Chunk``/``SourceRef``: ``section`` (Gliederungsnummer, z. B. 7.5.3) und
  ``section_end``, am Abschnitt zusätzlich ``section_title``.
- ``Document.bib_*``: Literaturangaben je Dokument (auch Sammelwerk, Norm).
- ``RagSettings.crossref_*``: optionale Abfrage bei Crossref (aus).
"""

from django.db import migrations, models

import multigpt.chat.citations
import multigpt.core.fields


class Migration(migrations.Migration):
    dependencies = [
        ("chat", "0018_attachments_upload"),
    ]

    operations = [
        migrations.AddField(
            model_name="chunk",
            name="page_end",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Letzte Seite des Abschnitts.",
                null=True,
                verbose_name="bis Seite",
            ),
        ),
        migrations.AddField(
            model_name="chunk",
            name="paragraph",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Absatz des Abschnittsbeginns ab 1 (je Seite neu gezählt); leer bei Abschnitten aus der Zeit vor der Absatzzählung.",
                null=True,
                verbose_name="Absatz",
            ),
        ),
        migrations.AddField(
            model_name="chunk",
            name="paragraph_end",
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name="bis Absatz"),
        ),
        migrations.AddField(
            model_name="document",
            name="bib_authors",
            field=models.TextField(
                blank=True,
                help_text="Eine Person je Zeile als „Nachname, Vorname“; Körperschaften ohne Komma.",
                verbose_name="Autor(en)",
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="bib_date",
            field=models.CharField(
                blank=True,
                help_text="JJJJ, JJJJ-MM oder JJJJ-MM-TT.",
                max_length=10,
                validators=[multigpt.chat.citations.validate_bib_date],
                verbose_name="Jahr bzw. Datum",
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="bib_doi",
            field=models.CharField(
                blank=True,
                max_length=200,
                validators=[multigpt.chat.citations.validate_doi],
                verbose_name="DOI",
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="bib_edited",
            field=models.BooleanField(
                default=False,
                help_text="Dann überschreibt die Indexierung die Angaben nicht mehr.",
                verbose_name="Angaben von Hand gepflegt",
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="bib_edition",
            field=models.CharField(blank=True, max_length=50, verbose_name="Auflage"),
        ),
        migrations.AddField(
            model_name="document",
            name="bib_isbn",
            field=models.CharField(
                blank=True,
                max_length=20,
                validators=[multigpt.chat.citations.validate_isbn],
                verbose_name="ISBN",
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="bib_issue",
            field=models.CharField(blank=True, max_length=30, verbose_name="Heft"),
        ),
        migrations.AddField(
            model_name="document",
            name="bib_journal",
            field=models.CharField(blank=True, max_length=300, verbose_name="Zeitschrift"),
        ),
        migrations.AddField(
            model_name="document",
            name="bib_place",
            field=models.CharField(blank=True, max_length=200, verbose_name="Ort"),
        ),
        migrations.AddField(
            model_name="document",
            name="bib_publisher",
            field=models.CharField(blank=True, max_length=300, verbose_name="Verlag/Herausgeber"),
        ),
        migrations.AddField(
            model_name="document",
            name="bib_title",
            field=models.CharField(
                blank=True,
                help_text="Leer = Titel des Dokuments.",
                max_length=500,
                verbose_name="Titel (Zitat)",
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="bib_url",
            field=models.URLField(blank=True, max_length=2000, verbose_name="URL"),
        ),
        migrations.AddField(
            model_name="document",
            name="bib_volume",
            field=models.CharField(blank=True, max_length=30, verbose_name="Band"),
        ),
        migrations.AddField(
            model_name="sourceref",
            name="biblio",
            field=models.JSONField(
                blank=True,
                default=dict,
                encoder=multigpt.core.fields.UnicodeJSONEncoder,
                help_text="Stand der Dokumentangaben zur Zeit der Antwort (citations.Reference).",
                verbose_name="Literaturangaben",
            ),
        ),
        migrations.AddField(
            model_name="sourceref",
            name="page_end",
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name="bis Seite"),
        ),
        migrations.AddField(
            model_name="sourceref",
            name="paragraph",
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name="Absatz"),
        ),
        migrations.AddField(
            model_name="sourceref",
            name="paragraph_end",
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name="bis Absatz"),
        ),
        migrations.AlterField(
            model_name="chunk",
            name="page",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Erste Seite des Abschnitts ab 1; leer bei Formaten ohne Seiten.",
                null=True,
                verbose_name="Seite",
            ),
        ),
        migrations.AddField(
            model_name="chunk",
            name="section",
            field=models.CharField(
                blank=True,
                help_text="Nummer der am Abschnittsbeginn geltenden Überschrift, z. B. 7.5.3.",
                max_length=30,
                verbose_name="Gliederungsabschnitt",
            ),
        ),
        migrations.AddField(
            model_name="chunk",
            name="section_end",
            field=models.CharField(
                blank=True,
                help_text="Nur gesetzt, wenn der Abschnitt über eine Gliederungsgrenze reicht.",
                max_length=30,
                verbose_name="bis Gliederungsabschnitt",
            ),
        ),
        migrations.AddField(
            model_name="chunk",
            name="section_title",
            field=models.CharField(blank=True, max_length=200, verbose_name="Abschnittstitel"),
        ),
        migrations.AddField(
            model_name="document",
            name="bib_container",
            field=models.CharField(
                blank=True,
                help_text="Buchtitel beim Kapitel bzw. Titel der Proceedings.",
                max_length=500,
                verbose_name="Sammelwerk/Tagungsband",
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="bib_editors",
            field=models.TextField(
                blank=True,
                help_text="Eine Person je Zeile als „Nachname, Vorname“ (Sammelband, Kapitel).",
                verbose_name="Herausgeber (Hrsg.)",
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="bib_institution",
            field=models.CharField(
                blank=True,
                help_text="Norm: DIN, ISO, IEC …; Bericht: Institution.",
                max_length=300,
                verbose_name="Herausgebende Stelle",
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="bib_isbn_e",
            field=models.CharField(
                blank=True,
                max_length=20,
                validators=[multigpt.chat.citations.validate_isbn],
                verbose_name="ISBN (eBook)",
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="bib_number",
            field=models.CharField(
                blank=True,
                help_text="z. B. DIN EN ISO 9001",
                max_length=100,
                verbose_name="Normnummer",
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="bib_replaces",
            field=models.CharField(blank=True, max_length=300, verbose_name="Ersatz für"),
        ),
        migrations.AddField(
            model_name="document",
            name="bib_series",
            field=models.CharField(blank=True, max_length=300, verbose_name="Reihe"),
        ),
        migrations.AddField(
            model_name="document",
            name="bib_series_number",
            field=models.CharField(blank=True, max_length=30, verbose_name="Band der Reihe"),
        ),
        migrations.AddField(
            model_name="document",
            name="bib_status",
            field=models.CharField(
                blank=True,
                choices=[("valid", "gültig"), ("withdrawn", "zurückgezogen")],
                max_length=10,
                verbose_name="Status (Norm)",
            ),
        ),
        migrations.AddField(
            model_name="ragsettings",
            name="crossref_enabled",
            field=models.BooleanField(
                default=False,
                help_text="Erkennt die Indexierung eine DOI, werden fehlende Angaben (Autoren, Sammelwerk, Verlag …) bei api.crossref.org abgefragt. Achtung: Crossref erfährt dabei, welche Dokumente hier liegen. Von Hand gepflegte Angaben bleiben unverändert.",
                verbose_name="Literaturangaben bei Crossref nachschlagen",
            ),
        ),
        migrations.AddField(
            model_name="ragsettings",
            name="crossref_mailto",
            field=models.EmailField(
                blank=True,
                help_text="Optional; Crossref bittet um eine Adresse im User-Agent („polite pool“).",
                max_length=254,
                verbose_name="Kontakt-Adresse für Crossref",
            ),
        ),
        migrations.AddField(
            model_name="sourceref",
            name="section",
            field=models.CharField(blank=True, max_length=30, verbose_name="Gliederungsabschnitt"),
        ),
        migrations.AddField(
            model_name="sourceref",
            name="section_end",
            field=models.CharField(
                blank=True, max_length=30, verbose_name="bis Gliederungsabschnitt"
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="bib_pages",
            field=models.CharField(
                blank=True,
                help_text="Artikel bzw. Kapitel im Sammelwerk, z. B. 45–67.",
                max_length=30,
                validators=[multigpt.chat.citations.validate_pages],
                verbose_name="Seitenbereich",
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="bib_type",
            field=models.CharField(
                choices=[
                    ("book", "Buch"),
                    ("edited", "Sammelband (herausgegeben)"),
                    ("chapter", "Beitrag im Sammelwerk (Kapitel)"),
                    ("article", "Zeitschriftenartikel"),
                    ("conference", "Konferenzbeitrag (Proceedings)"),
                    ("report", "Bericht"),
                    ("standard", "Norm/Standard"),
                    ("web", "Webseite"),
                    ("other", "Sonstiges"),
                ],
                default="other",
                max_length=10,
                verbose_name="Art",
            ),
        ),
    ]
