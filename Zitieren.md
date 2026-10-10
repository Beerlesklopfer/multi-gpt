# Zitieren

MultiGPT gibt Quellen aus Dokumenten und Webseiten so an, dass man sie zitieren kann: mit
**Fundstelle** (Gliederungsabschnitt, Seite, Absatz) und mit **Literaturangaben** im Zitierstil,
den jedes Konto selbst wählt. Die Zitate formatiert der Server nach festen Regeln, nicht das
Modell; dieselben Angaben ergeben immer denselben Text.

## Einstellungen

Unter **„Einstellungen“** oben rechts:

| Einstellung | Wirkung |
|---|---|
| **Zitierstil** | DIN ISO 690 (Grundeinstellung), APA 7, Harvard, Chicago (Author-Date) oder MLA 9. Gilt für die Quellenliste unter Antworten, „Zitat kopieren“ und den Kurzbeleg, den das Modell bekommt. |
| **Quellen im Antworttext als Kurzbeleg zeigen** | Statt „[1]“ erscheint im Text der Kurzbeleg, z. B. „(Müller 2024, S. 12)“. Aus (Grundeinstellung): nur die Nummer. |
| **Abschnitt, Seite und Absatz anzeigen** | Fundstelle in der Quellenliste und im Kurzbeleg. |

Die Seite zeigt für jeden Stil ein Beispiel. Alle Stile erscheinen in ihrer deutschen Fassung
(„S.“, „Abs.“, „Hrsg.“, „Aufl.“, „o. J.“ für ein fehlendes Jahr, „abgerufen am“); MLA lässt ein
fehlendes Jahr weg. Fehlende Angaben fallen einfach weg.

## Unter der Antwort

- Jede **Dokumentquelle** steht im eigenen Stil mit Fundstelle, z. B. in APA:
  „Weber, K. (2021). Kapitel eins. In H. Müller & E. Schmidt (Hrsg.), Handbuch Textanalyse
  (S. 45–67). Springer. https://doi.org/… – Abschn. 3.2, S. 47, Abs. 2“.
- **„Zitat kopieren“** kopiert den Eintrag im eigenen Stil, das Menü **„Stil“** daneben in einem
  anderen Stil oder als **BibTeX**.
- **„Literaturverzeichnis kopieren“** kopiert alle Quellen der Antwort, jede einmal und
  alphabetisch (BibTeX in der Reihenfolge der Quellen).
- **Webquellen** bekommen ebenfalls den Stil: Titel, URL und als Abrufdatum der Zeitpunkt der
  Antwort.
- Auch im **Vergleich** mehrerer Modelle hat jede Spalte ihre Quellenliste.

## Fundstelle

| Angabe | Bedeutung |
|---|---|
| S. 12 | Seite der PDF-Datei (bei Bereichen „S. 12–13“) |
| Abs. 3 | Absatz auf dieser Seite, ab 1 gezählt; ohne Seiten (DOCX, TXT, MD) durchgehend |
| Abschn. 7.5.3 | nummerierte Überschrift, unter der der Text steht |

Beispiele: „S. 12, Abs. 3“, „S. 12, Abs. 3–5“, „S. 12, Abs. 4 – S. 13, Abs. 2“, „Abs. 17“,
„Abschn. 7.5.3, S. 12, Abs. 3“. Abschnitte aus der Zeit vor der Absatzzählung zeigen nur die
Seite, bis ein Verwalter „Alles neu indexieren“ auslöst.

**Fürs Modell:** Jede Dokumentquelle kommt mit der Zeile „[2] Jahresbericht 2024, S. 12,
Abs. 3–5“ und dem Kurzbeleg im eingestellten Stil. Das Modell verweist im Text mit [n], nennt
Seite und Absatz bei wörtlichen Zitaten oder auf Nachfrage und übernimmt auf Wunsch den
Kurzbeleg unverändert.

## Literaturangaben pflegen

Wer in eine Sammlung schreiben darf, öffnet auf der Seite der Sammlung beim Dokument
**„Literaturangaben“**; Verwalter können sie auch im Admin unter „Dokumente (RAG)“ ändern. Die
Seite zeigt eine Vorschau in allen Stilen und als BibTeX.

| Art | Wichtige Felder |
|---|---|
| Buch | Autor(en), Titel, Jahr, Auflage, Reihe und Band, Ort, Verlag, ISBN (Print/eBook), DOI |
| Sammelband | Herausgeber statt Autoren, sonst wie Buch |
| Beitrag im Sammelwerk (Kapitel) | Autor(en), Kapiteltitel, Herausgeber, Titel des Sammelwerks, Seitenbereich, Verlag, DOI |
| Zeitschriftenartikel | Autor(en), Titel, Zeitschrift, Band, Heft, Seitenbereich, DOI |
| Konferenzbeitrag | wie Kapitel, Sammelwerk = Titel der Proceedings |
| Bericht | Autor(en) oder herausgebende Stelle, Titel, Jahr, Ort, Verlag |
| Norm/Standard | Normnummer (z. B. „DIN EN ISO 9001“), Ausgabedatum unter „Jahr bzw. Datum“ (z. B. 2015-11), Titel, herausgebende Stelle (DIN, ISO …), Verlag/Bezugsquelle (z. B. DIN Media, früher Beuth), Status, Ersatz für |
| Webseite | Titel, URL, Datum |

- Personen je Zeile als **„Nachname, Vorname“**; Körperschaften ohne Komma („Statistisches
  Bundesamt“).
- Datum als 2024, 2024-03 oder 2024-03-12 (auch 12.03.2024).
- ISBN werden mit Prüfziffer geprüft, DOIs auch als „https://doi.org/…“ angenommen.
- Ohne eigenen Titel gilt der Dokumenttitel.

**Normen** werden in jedem Stil über Nummer und Ausgabe zitiert: DIN ISO 690 „DIN EN ISO
9001:2015-11. Qualitätsmanagementsysteme – Anforderungen. Berlin: DIN Media.“, Kurzbeleg
„(DIN EN ISO 9001:2015-11, Abschn. 7.5.3, S. 12)“; in APA steht die herausgebende Stelle als
Autor und die Normnummer in Klammern hinter dem Titel; BibTeX als `@techreport` mit
`type = {Norm}`.

## Vorbelegung beim Indexieren

Leere Felder füllt die Indexierung, solange niemand die Angaben gespeichert hat (danach ändert
sie nichts mehr):

- **Titel, Autor, Jahr** aus den PDF-Metadaten bzw. den Eigenschaften der Word-Datei. Platzhalter
  wie „Microsoft Word - …“, „Unbenannt“ oder „Administrator“ werden ignoriert.
- **DOI** aus den PDF-Metadaten oder von den ersten zwei Seiten (Verlags-PDFs, z. B. Springer).
- **Normnummer mit Ausgabedatum** aus Dateiname oder Titelseite („DIN EN ISO 9001:2015-11“,
  „DIN 1450 Ausgabe 2013-04“). Ohne erkennbares Ausgabedatum wird nichts gesetzt.
- **Crossref** (optional): Ist im Admin unter „RAG-Einstellungen → Literaturangaben“
  „Literaturangaben bei Crossref nachschlagen“ eingeschaltet, fragt MultiGPT zur erkannten DOI
  `api.crossref.org` (kurzer Timeout, nur diese Adresse, Kontakt-Adresse im User-Agent, falls
  eingetragen). Damit kommen Autoren, Herausgeber, Sammelwerk, Reihe, Verlag, Seiten und ISBN
  dazu. **Standardmäßig aus**, weil Crossref so erfährt, welche Dokumente die Familie hat.
  Fehler werden ignoriert.

## Urheberrecht

DIN-Normen und Verlagstexte (z. B. Springer) sind urheberrechtlich geschützt. Ihre Lizenz
erlaubt die gemeinsame Nutzung in der Familie, etwa in einer geteilten Sammlung, unter Umständen
nicht. Vor dem Teilen die Lizenzbedingungen prüfen.
