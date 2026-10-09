# Fragen an eigene Dokumente (RAG)

Mit **RAG** (Retrieval-Augmented Generation) beantwortet ein Modell Fragen anhand eigener
Dokumente: MultiGPT sucht zu jeder Frage die passendsten Textabschnitte aus den gewählten
Sammlungen heraus und gibt sie dem Modell als Quellmaterial mit. Unter der Antwort stehen die
verwendeten Dokumente mit Seitenzahl, ein Klick zeigt den Abschnitt.

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
   jeden Abschnitt einen Vektor (Embedding). Gescannte PDF-Seiten ohne Textebene werden per
   Texterkennung (OCR) gelesen.
3. Im Chat wählst du eine oder mehrere Sammlungen. MultiGPT sucht zur Frage die ähnlichsten
   Abschnitte (Vektorsuche mit pgvector, auf Wunsch zusammen mit der deutschen Volltextsuche)
   und gibt die besten (Grundeinstellung: 6) nummeriert an das Modell.
4. Das Modell antwortet und verweist auf die Quellen. Unter der Antwort stehen die Dokumente
   mit Seitenzahl.

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
| Dateitypen | **PDF, DOCX, TXT, MD**. Der Typ wird am Inhalt erkannt, die Endung muss dazu passen. |
| Größe | höchstens **25 MB** je Datei (Grundeinstellung, siehe `DOCUMENT_MAX_UPLOAD_MB`) |
| Anzahl | eine Datei je Upload |
| Recht | Rolle mit „Dokumente hochladen“ (in der Grundeinstellung alle außer Gästen) und Schreibrecht auf die Sammlung |

Der Dateiname dient nur als Titel, gespeichert wird die Datei unter einem zufälligen Namen.
Hochgeladene Dokumente lassen sich wieder herunterladen und löschen.

**Ordner vom Server:** Ein Verwalter kann eine Sammlung auch aus einem Ordner auf
dem Server oder NAS füllen lassen ([Verzeichnisquellen](RAG-Verzeichnisquellen)). Die Seite der
Sammlung zeigt dann „Wird aus einem Serververzeichnis eingelesen“. Solche Dokumente werden
regelmäßig abgeglichen und lassen sich nicht einzeln löschen; sie verschwinden, wenn die Datei
im Ordner entfernt wird oder ein Verwalter die Quelle löscht.

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
- Modelle, die Werkzeuge beherrschen, können zusätzlich selbst über das eingebaute Werkzeug
  `search_documents` suchen, ohne Rückfrage (es liest nur): in den gewählten Sammlungen, sonst
  in allen, die du lesen darfst.

## Quellen

Unter der Antwort stehen die verwendeten Dokumente als **„Titel, S. 12“**. Bei Formaten ohne
Seiten (DOCX, TXT, MD) fehlt die Seitenzahl. Ein Klick öffnet den Abschnitt mit Blättern zum
vorherigen und nächsten Abschnitt und einem Link zum Herunterladen des Dokuments. Ist das
Dokument inzwischen gelöscht, bleibt die Quellenangabe mit dem Vermerk „(entfernt)“ stehen.

Dokumentinhalte gelten als **Quellmaterial, nicht als Anweisung**: Ein Dokument kann aus fremder
Hand stammen und Text enthalten, der das Modell zu etwas überreden soll. MultiGPT grenzt den
Text deshalb klar ab und weist das Modell im System-Prompt darauf hin.

## Datenschutz

- **Verwalter sehen keine Inhalte.** Im Admin-Abschnitt „Dokumente (RAG)“ stehen nur
  Metadaten: Titel, Sammlung, Besitzer, Status, Fehlertext, Größe, Zahl der Abschnitte und
  Freigaben. Dokumenttext, Abschnitte und Download gibt es dort nicht. Ein Test stellt das
  sicher.
- Dokumente einer privaten Sammlung erreicht nur der Besitzer; geteilte Sammlungen nur die
  Mitglieder der freigegebenen Gruppen. Fremde Sammlungen, Dokumente und Abschnitte liefern
  „nicht gefunden“, damit nicht einmal ihre Existenz sichtbar wird.
- Logs enthalten nur IDs und Zahlen, keine Dateinamen oder Inhalte.
- Mit Embeddings und OCR über LM Studio (siehe [RAG einrichten](RAG-Einrichtung))
  verlassen Dokumentinhalte beim Indexieren das Heimnetz nicht.
