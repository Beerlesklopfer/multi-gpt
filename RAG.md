# Fragen an eigene Dokumente (RAG)

Mit **RAG** (Retrieval-Augmented Generation) beantwortet ein Modell Fragen anhand eigener
Dokumente: MultiGPT sucht zu jeder Frage die passendsten Textabschnitte aus den gewählten
Sammlungen heraus und gibt sie dem Modell als Quellmaterial mit. Unter der Antwort stehen die
verwendeten Dokumente im eingestellten Zitierstil mit Fundstelle (Abschnitt, Seite, Absatz), ein
Klick zeigt den Abschnitt. Wie zitiert wird, steht unter [Zitieren](Zitieren).

Diese Seite richtet sich an alle Nutzer und an Verwalter. Die Einrichtung des Servers (Embedding-
Modell, Worker, OCR) steht unter [RAG einrichten](RAG-Einrichtung), das Anbinden von NAS-Ordnern
unter [Verzeichnisquellen](RAG-Verzeichnisquellen).

> **Stand:** Meilenstein 7 ist umgesetzt: Sammlungen, Upload, Indexierung im Hintergrund, Suche
> mit Quellen, die Verwaltung im Admin, die lokale Verarbeitung über LM Studio (Embeddings mit
> nomic-embed-text, OCR mit olmOCR, Tesseract als Ersatz) und die Verzeichnisquellen.

## So funktioniert es

1. Du legst eine **Sammlung** an und lädst Dokumente hoch.
2. Der **Worker** (ein Hintergrunddienst auf dem Server) liest den Text, teilt ihn in
   überlappende Abschnitte (Grundeinstellung ca. 800 Tokens, 100 Überlappung) und berechnet für
   jeden Abschnitt einen Vektor (Embedding). Jeder Abschnitt merkt sich seine Fundstelle:
   erste und letzte Seite, Absatz von–bis und, bei nummeriert gegliederten Dokumenten wie
   Normen, die Gliederungsnummer („7.5.3“). Gescannte PDF-Seiten ohne Textebene und
   Bilddateien werden per Texterkennung (OCR) gelesen. Auf Wunsch des Verwalters beschreibt ein
   Vision-Modell außerdem Bilder und Diagramme (siehe [Abbildungen](#abbildungen)).
3. Im Chat wählst du eine oder mehrere Sammlungen. MultiGPT sucht zur Frage die ähnlichsten
   Abschnitte (Vektorsuche mit pgvector, auf Wunsch zusammen mit der deutschen Volltextsuche)
   und gibt die besten (Grundeinstellung: 6) nummeriert an das Modell.
4. Das Modell antwortet und verweist mit [1], [2] … auf die Quellen. Unter der Antwort stehen
   die Dokumente im Zitierstil mit Fundstelle.

Die Suche geschieht in der Datenbank, die Abschnitte gehen nur an das Modell, das du für die
Antwort gewählt hast. Wer keine Dokumentinhalte nach außen geben will, wählt ein lokales Modell.

## Sammlungen

Die Sammlungen erreichst du über **„Sammlungen“** in der Seitenleiste.

- **Anlegen:** Unter „Neue Sammlung“ einen Namen eingeben und „Anlegen“. Der Name muss unter
  deinen eigenen Sammlungen eindeutig sein. Umbenennen und Löschen gehen auf der Seite der
  Sammlung.
- **Privat:** Eine neue Sammlung sieht nur, wer sie angelegt hat.
- **Teilen:** Auf der Seite der Sammlung unter „Freigeben“ eine Gruppe wählen und den Zugriff
  festlegen:
  - **nur lesen:** Mitglieder der Gruppe können die Sammlung im Chat nutzen und die Dokumente
    lesen.
  - **lesen und schreiben:** Sie dürfen zusätzlich Dokumente hochladen und löschen.

  Teilen kann nur, wer die Sammlung angelegt hat und laut Rolle teilen darf (in der
  Grundeinstellung Verwalter und Erwachsene). „Entziehen“ beendet eine Freigabe sofort.
  Gruppen legt ein Verwalter im Admin an.
- **Mit dir geteilt:** Sammlungen, die jemand mit einer deiner Gruppen geteilt hat, stehen in
  der Liste unter „Mit dir geteilt“.

Fremde private Sammlungen sind nirgends sichtbar, auch nicht in der Suche: Der Zugriff wird in
der Suchabfrage selbst geprüft.

## Dokumente hochladen

Auf der Seite einer Sammlung unter **„Dokumente hochladen“**.

| | |
|---|---|
| Dateitypen | **PDF, DOCX, TXT, MD** und Bilder (**JPG, PNG, TIFF** auch mehrseitig, **WEBP**). Der Typ wird am Inhalt erkannt (Magic Bytes), die Endung muss dazu passen. HEIC-Fotos vorher als JPEG speichern. |
| Größe | höchstens **25 MB** je Datei (Grundeinstellung, siehe `DOCUMENT_MAX_UPLOAD_MB`) |
| Anzahl | eine Datei je Upload |
| Recht | Rolle mit „Dokumente hochladen“ (in der Grundeinstellung alle außer Gästen) und Schreibrecht auf die Sammlung |

Der Dateiname dient nur als Titel, gespeichert wird die Datei unter einem zufälligen Namen.
Hochgeladene Dokumente lassen sich wieder herunterladen und löschen. Wer in die Sammlung
schreiben darf, pflegt über **„Literaturangaben“** Autor, Jahr, Verlag, Normnummer usw. für
Zitate (siehe [Zitieren](Zitieren)); beim Indexieren werden sie soweit möglich vorbelegt.

**Ordner vom Server:** Ein Verwalter kann eine Sammlung auch aus einem Ordner auf
dem Server oder NAS füllen lassen ([Verzeichnisquellen](RAG-Verzeichnisquellen)). Die Seite der
Sammlung zeigt dann „Wird aus einem Serververzeichnis eingelesen“. Solche Dokumente werden
regelmäßig abgeglichen und lassen sich nicht einzeln löschen; sie verschwinden, wenn die Datei
im Ordner entfernt wird oder ein Verwalter die Quelle löscht.

## Abbildungen

Text in Bildern und Diagrammen fände die Suche sonst nicht. Hat ein Verwalter in den
RAG-Einstellungen **„Abbildungen beschreiben“** eingeschaltet, gilt beim Indexieren:

- **Bilder in PDFs** (mit Textebene) und **in Word-Dokumenten** beschreibt ein Vision-Modell.
  Die Beschreibung steht als eigener Absatz „[Abbildung: …]“ an der Stelle des Bildes im Text
  und wird mit durchsucht – im Chat kann das Modell sie also wie Text zitieren (mit Seite und
  Absatz).
- Die Beschreibung ist sachlich: was das Bild zeigt, bei Diagrammen Achsen, Größen, Trends und
  wichtige Werte, enthaltener Text wörtlich, in 2 bis 6 Sätzen in der Sprache des Dokuments
  (sonst Deutsch). Steht nahe am Bild eine Bildunterschrift („Abb. 3 …“, „Abbildung …“,
  „Figure …“, „Fig. …“), steht sie vorn in der Beschreibung.
- **Bilddateien** (Fotos von Schildern, eingescannte Seiten als JPG/PNG/TIFF) liest die
  Texterkennung; ist die Beschreibung eingeschaltet, kommt eine Beschreibung des Bildes dazu.
  Ohne Beschreibung zählt nur der erkannte Text.
- Übersprungen werden kleine Bilder und Symbole, schmale Linien, Wiederholungen (etwa ein Logo
  auf jeder Seite) und Bilder über der eingestellten Höchstzahl (Grundeinstellung 50 je
  Dokument, 10 je Seite). Gescannte Seiten bekommen keine eigene Beschreibung, dort liest die
  OCR den Text.
- Vor dem Senden werden Bilder verkleinert und ohne Metadaten neu gespeichert; EXIF-Angaben wie
  Kamera oder GPS-Position gehen nicht an das Modell.
- Ist das Beschreiben ausgeschaltet (Grundeinstellung), werden Bilder in PDF und DOCX nicht
  indexiert.

## Status und Fehler

Jedes Dokument zeigt seinen Status:

| Status | Bedeutung |
|---|---|
| **wartet** | Das Dokument ist eingereiht oder wird gerade verarbeitet. Nach einem vorübergehenden Fehler steht hier auch der Hinweis „Versuch n fehlgeschlagen: … Neuer Versuch ab hh:mm Uhr.“ Ist LM Studio nicht erreichbar: „Wartet: … Neuer Versuch ab hh:mm Uhr.“ |
| **indexiert** | Fertig, das Dokument ist durchsuchbar. |
| **Fehler** | Die Verarbeitung ist endgültig gescheitert. Der Fehlertext nennt den Grund. |

Typische Fehlertexte:

- „Im Dokument wurde kein Text gefunden.“ – zum Beispiel eine leere Datei oder ein Scan, bei
  dem auch die Texterkennung nichts lesen konnte.
- „Die Datei des Dokuments fehlt auf dem Server.“
- „Das Dokument ist zu umfangreich für die Verarbeitung.“
- „Es ist kein Embedding-Modell eingerichtet. …“ – ein Verwalter muss die Dokumentsuche erst
  einrichten. Solange das fehlt, zeigt auch die Seite der Sammlung einen Hinweis.

Bleibt ein Dokument lange auf „wartet“, läuft vermutlich der Worker nicht. Das prüft ein
Verwalter, siehe [RAG einrichten → Fehlersuche](RAG-Einrichtung#fehlersuche).

## Im Chat nutzen

- Unter dem Eingabefeld erscheint der Bereich **„Dokumente“**, sobald du mindestens eine
  Sammlung lesen darfst. Dort die gewünschten Sammlungen anhaken; „Verwalten“ führt zur Liste
  der Sammlungen.
- Bei gewählten Sammlungen sucht MultiGPT vor der Antwort („Durchsuche Dokumente …“). Findet es
  nichts Passendes, sagt das Modell das, statt etwas zu erfinden. Scheitert die Suche (zum
  Beispiel weil der Embedding-Dienst nicht erreichbar ist), entsteht die Antwort mit einem
  Hinweis ohne Dokumentquellen.
- Modelle, die Werkzeuge beherrschen, können zusätzlich selbst suchen, Dokumente auflisten und
  nachlesen, siehe den nächsten Abschnitt.

## Was das Modell mit Dokumenten tun kann

Modelle mit Werkzeugen bekommen vier eingebaute Dokumentwerkzeuge. Sie lesen nur und laufen
deshalb ohne Rückfrage. Angeboten werden sie nur, wenn du mindestens ein Dokument lesen darfst.
Sie sehen genau die Sammlungen, die du selbst lesen darfst: deine eigenen und die für deine
Gruppen freigegebenen. Fremde Dokumente beantworten sie mit „nicht gefunden“, als gäbe es sie
nicht.

| Werkzeug | Wofür | Beispiel |
| --- | --- | --- |
| `search_documents` | Passende Abschnitte zu einer Frage finden | „Wie hoch ist die Kaution?“ |
| `list_documents` | Überblick: welche Dokumente es gibt | „Welche Normen zum Qualitätsmanagement habe ich?“ |
| `document_info` | Angaben und Inhaltsverzeichnis eines Dokuments | „Wie ist die ISO 9001 gegliedert?“ |
| `read_document` | Einen Abschnitt oder Seitenbereich im Wortlaut lesen | „Was steht in Abschnitt 7.5?“ |

So arbeitet das Modell typischerweise: Es sucht mit `search_documents` und liest danach mit
`read_document` den Zusammenhang eines Treffers. Bei Überblicksfragen listet es zuerst mit
`list_documents` und liest dann gezielt nach.

**`list_documents`** zeigt je Dokument ID, Titel, Sammlung, Seiten- und Abschnittszahl,
Datum, Status und einen Kurzbeleg, falls Literaturangaben gepflegt sind. Normen erscheinen mit
Normnummer und Ausgabe, z. B. „DIN EN ISO 9001:2015-11“. Ohne weitere Angabe gelten die
Sammlungen, die du unter dem Eingabefeld angehakt hast, sonst alle lesbaren. Filtern lässt
sich nach:

- Sammlung (Name oder ID),
- Text in Titel, Normnummer, Autoren, Herausgebern, Reihe oder Pfad,
- Jahr,
- Dokumentart (z. B. Norm, Buch, Artikel, Bericht),
- Status (Standard: nur fertig indexierte),
- Thema in eigenen Worten (`topic`): findet Dokumente nach ihrem Inhalt, auch wenn das Wort
  nicht im Titel steht. Dafür muss die Suche eingerichtet sein.

Die Liste kommt in Seiten zu höchstens 50 Einträgen. Sortiert wird nach Sammlung, Titel und ID,
bei einem Thema nach Relevanz. Jede Antwort nennt „Seite X von Y (N Dokumente insgesamt)“, damit
das Modell bei Bedarf alles durchblättern kann.

**`document_info`** liefert die Literaturangaben, die Sammlung, bei Verzeichnisquellen den Pfad
innerhalb der Quelle (nie den Pfad auf dem Server), die Zahl der Seiten und Abschnitte, den
Status und ein Inhaltsverzeichnis. Das Verzeichnis kommt aus der nummerierten Gliederung
(„7.5 Dokumentierte Information – S. 5, Abs. 3“). Ohne Gliederung wird es aus Überschriften
geschätzt.

**`read_document`** liest einen Gliederungsabschnitt samt Unterabschnitten (z. B. 7.5 mit 7.5.1
bis 7.5.3) oder einen Seiten- bzw. Absatzbereich. Absätze werden je Seite ab 1 gezählt, bei
Formaten ohne Seiten durchgehend. Je Aufruf gibt es höchstens 12 000 Zeichen bzw. 10 Seiten.
Ist der Abschnitt länger, sagt die Antwort dem Modell, wie es weiterliest. Jede gelesene Seite
wird eine eigene Quelle mit Abschnitt, Seite und Absatz.

Grenzen: Der Text wird aus den gespeicherten Abschnitten zusammengesetzt, nicht aus der
Originaldatei. Inhalt und Absätze bleiben dabei erhalten. Liegen aber mehrere sehr kurze Seiten
(z. B. Folien) in einem Abschnitt, ist die genaue Seitengrenze nicht bekannt. Die Quelle nennt
dann einen Seitenbereich wie „S. 4–6“.

## Quellen

Unter der Antwort stehen die verwendeten Quellen. Dokumente erscheinen im **Zitierstil deines
Kontos** (Grundeinstellung DIN ISO 690, einstellbar unter „Einstellungen“) mit **Fundstelle**,
z. B. „DIN EN ISO 9001:2015-11. Qualitätsmanagementsysteme – Anforderungen. Berlin: DIN Media.
Abschn. 7.5.3, S. 12, Abs. 3“. Daneben stehen „Zitat kopieren“ (mit Menü für andere Stile und
BibTeX) und für die ganze Antwort „Literaturverzeichnis kopieren“. Einzelheiten unter
[Zitieren](Zitieren).

- **Seite:** bei PDF die Seite der Datei; geht ein Abschnitt über eine Seitengrenze, steht ein
  Bereich da („S. 12–13“ bzw. „S. 12, Abs. 4 – S. 13, Abs. 2“). DOCX, TXT und MD haben keine
  Seiten.
- **Absatz:** durch eine Leerzeile getrennter Textblock, je Seite ab 1 gezählt (ohne Seiten
  durchgehend). Überschriften und Tabellen zählen als Absatz, leere Absätze nicht. Bei PDFs, deren
  Textebene keine Leerzeilen hat, schätzt MultiGPT die Absatzgrenzen vorsichtig (Zeile mit
  Satzende, deutlich kürzer als eine volle Zeile); im Zweifel bleibt ein Absatz zusammen. Weil
  sich Abschnitte überlappen, kann der erste genannte Absatz schon früher begonnen haben.
- **Gliederungsabschnitt:** Nummerierte Überschriften („7.5.3 Lenkung dokumentierter
  Information“, „Anhang A“, „A.2.1 …“) werden erkannt, wenn die Nummern plausibel aufsteigen;
  Inhaltsverzeichnisse, Aufzählungen („1. …“) und Zahlen in Tabellen zählen nicht.

Ein Klick öffnet den Abschnitt mit Fundstelle, Blättern zum vorherigen und nächsten Abschnitt
und einem Link zum Herunterladen des Dokuments. Ist das Dokument inzwischen gelöscht, bleibt die
Quellenangabe samt Literaturangaben mit dem Vermerk „(entfernt)“ stehen.

**Vorhandene Dokumente:** Absätze, Gliederung und Seitenbereiche bekommen Dokumente erst beim
nächsten Indexieren. Ältere Abschnitte zeigen nur die Seite. Ein Verwalter stößt das über
„Alles neu indexieren“ im Admin an (siehe [RAG einrichten](RAG-Einrichtung)).

Dokumentinhalte gelten als **Quellmaterial, nicht als Anweisung**: Ein Dokument kann aus fremder
Hand stammen und Text enthalten, der das Modell zu etwas überreden soll. MultiGPT grenzt den
Text deshalb klar ab und weist das Modell im System-Prompt darauf hin.

## Datenschutz

- **Verwalter sehen keine Inhalte.** Im Admin-Abschnitt „Dokumente (RAG)“ stehen nur
  Metadaten: Titel, Sammlung, Besitzer, Status, Fehlertext, Größe, Zahl der Abschnitte und
  Freigaben. Dokumenttext, Abschnitte und Download gibt es dort nicht. Ein Test stellt das
  sicher.
  Bearbeiten lassen sich dort nur die Literaturangaben (Autor, Jahr, Verlag, Normnummer …).
- Dokumente einer privaten Sammlung erreicht nur der Besitzer; geteilte Sammlungen nur die
  Mitglieder der freigegebenen Gruppen. Fremde Sammlungen, Dokumente und Abschnitte liefern
  „nicht gefunden“, damit nicht einmal ihre Existenz sichtbar wird.
- Logs enthalten nur IDs und Zahlen, keine Dateinamen oder Inhalte.
- Die Abfrage von Literaturangaben bei **Crossref** (per DOI) ist standardmäßig aus. Schaltet
  ein Verwalter sie ein, erfährt Crossref, welche Dokumente (DOIs) hier liegen, siehe
  [Zitieren](Zitieren#vorbelegung-beim-indexieren).
- Mit Embeddings, OCR und Abbildungen über LM Studio (siehe [RAG einrichten](RAG-Einrichtung))
  verlassen Dokumentinhalte beim Indexieren das Heimnetz nicht. Wählt ein Verwalter für
  Abbildungen ein Cloud-Modell, gehen die Bilder der Dokumente an diesen Anbieter.
