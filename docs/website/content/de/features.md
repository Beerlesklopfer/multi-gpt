---
title: "Funktionen"
slug: "funktionen"
description: "Was MultiGPT können soll: viele Anbieter, LM Studio, Familienkonten, Budgets, eigene Dokumente, Websuche, Bilder, Sprache und MCP-Werkzeuge."
lead: "Hier steht, was MultiGPT in Version 1 können soll. Umgesetzt sind Chatten mit vielen Anbietern, LM Studio, MCP-Werkzeuge, Familienkonten mit Budgets, der Vergleichsmodus, eigene Dokumente und die Websuche; Bilder und Sprache sind noch geplant. Jeder Abschnitt trägt seinen Status und den Meilenstein."
menus:
  main:
    weight: 10
---

## Anmeldung und Konten

{{< status "done" "1" >}}

- Login, Logout und Passwort ändern.
- Login-Drosselung: Nach fünf Fehlversuchen ist die Anmeldung für diesen Nutzernamen und
  diese Adresse 15 Minuten gesperrt.
- Keine Selbstregistrierung. Konten legt ein Verwalter an – per
  `mgpt-ctl createsuperuser`, `make user` (mit Rollenwahl) oder in der Verwaltung.

## Chatten mit vielen Anbietern

{{< status "done" "3 / 4 / 5" >}}

Ende-zu-Ende im Browser getestet, erste echte Gespräche mit OpenAI laufen.

- Chatliste in der Seitenleiste: neu, umbenennen, archivieren, löschen, Suche.
- Das Modell lässt sich **pro Nachricht** wählen. Antworten erscheinen gestreamt,
  ein Knopf bricht ab, eine Antwort lässt sich neu erzeugen.
- Anbieter werden per API-Key angebunden: OpenAI und alle OpenAI-kompatiblen Dienste
  (z. B. OpenRouter und LM Studio), dazu Anthropic und Google Gemini.
- In der Verwaltung prüft „Verbindung jetzt prüfen“ einen Anbieter und nennt bei Problemen
  die Ursache, etwa „Verbindung abgelehnt“, „API-Key prüfen“ oder „Basis-URL prüfen“. Die
  Modelle des Anbieters lassen sich aus seiner Liste auswählen („Modelle auswählen“ bzw. eine
  Auswahlliste am Feld Modell-ID). Abgekündigte Modelle werden nicht mehr angeboten.
- Eigene Nachrichten lassen sich bearbeiten wie in ChatGPT. Bearbeiten und „Neu erzeugen“
  legen Versionen an, „‹ 1/2 ›“ schaltet zwischen ihnen um. Frühere Fassungen bleiben erhalten.
  Jede Nachricht hat einen Kopierknopf.
- Markdown mit Code-Hervorhebung und Kopierknopf. Die Bibliotheken sind lokal eingebunden,
  die Ausgabe wird bereinigt.
- Automatische Chattitel, System-Prompt pro Chat, Export als Markdown.

## Lokale Modelle mit LM Studio

{{< status "done" "4" >}}

[LM Studio](https://lmstudio.ai/docs) läuft auf einem PC im Heimnetz und stellt einen
OpenAI-kompatiblen Server bereit. MultiGPT bindet ihn als Anbieter an, startet oder weckt
ihn aber nicht.

- Eine Statusanzeige in der Kopfzeile zeigt, ob LM Studio gerade erreichbar ist. Der
  Browser fragt alle 30 Sekunden nach, solange der Tab sichtbar ist.
- Ist LM Studio aus, sind lokale Modelle ausgegraut. Ist es an, werden die Modelle
  angeboten, die LM Studio tatsächlich meldet.
- Fällt LM Studio mitten in einer Antwort aus, bleibt der bisherige Text erhalten.
- Lokale Modelle kosten 0 € und bleiben auch bei ausgeschöpftem Budget nutzbar.
- Im echten Heimnetz im Einsatz: Chat (z. B. gpt-oss-20b), Embeddings (nomic-embed-text) und OCR (olmOCR) laufen über LM Studio.

## Rollen, Gruppen und Budgets

{{< status "done" "2 / 6" >}}

Rollen, Gruppen, Konten und die zentrale Rechteprüfung kamen mit Meilenstein 2, Budgets,
die Verbrauchsübersicht und die Seite „Familie“ mit Meilenstein 6.

Vier Startrollen, in der Verwaltung anpassbar:

| Rolle | Darf |
|---|---|
| Verwalter | Alles: Anbieter, Keys, Modelle, Konten, Rollen, Gruppen, Budgets, Verbrauch aller |
| Erwachsener | Alle freigegebenen Modelle und Funktionen, eigene Sammlungen, Teilen mit Gruppen |
| Jugendlicher | Nur freigegebene Modelle und Funktionen, fester System-Prompt der Rolle, Monatsbudget |
| Gast | Chat mit einem festgelegten Modell, kein Upload, kein Teilen, kleines Budget |

- Rechte werden auf dem Server geprüft, vor jeder Seite und vor jedem Anbieteraufruf –
  nicht nur durch Ausblenden in der Oberfläche.
- **Budgets:** Monatsbudget in Euro je Rolle, je Person überschreibbar. Ab 80 % erscheint
  ein Hinweis, ab 100 % sind kostenpflichtige Modelle bis zum Monatswechsel gesperrt.
  Kostenfreie Modelle (lokale Anbieter wie LM Studio oder Modelle ohne hinterlegte Preise)
  bleiben nutzbar.
- **Verbrauch:** Jede Person sieht auf der Seite „Mein Verbrauch“ (`/verbrauch/`) Tokens
  und geschätzte Kosten des Monats je Modell, den Stand des Budgets und die letzten
  Monate. Verwalter sehen den Verbrauch aller.
- **Privatsphäre:** Chats sind privat. Auch Verwalter sehen fremde Chats nicht, nur
  Verbrauchszahlen. Einzige Ausnahme: Für Jugendlichen-Konten gibt es die Option
  „Einsicht in Chats erlaubt“ (standardmäßig aus). Ist sie an, können Verwalter die Chats
  dieses Kontos lesen, aber nicht schreiben, und das Mitglied sieht dauerhaft einen Hinweis.
- **Gruppen:** Sammlungen und einzelne Chats lassen sich mit Gruppen teilen, lesend oder
  mit Schreibrecht.
- Eine Seite „Familie“ (`/familie/`) für Verwalter: Konten anlegen und sperren, Rollen
  zuweisen, Passwörter zurücksetzen, Budgets setzen, die Einsicht schalten, Gruppen pflegen
  und den Verbrauch aller ansehen.

## Vergleichsmodus

{{< status "done" "6" >}}

Dieselbe Frage an zwei oder drei Modelle gleichzeitig stellen und die Antworten
nebeneinander lesen.

- Ein Schalter im Eingabefeld öffnet die Auswahl von zwei oder drei Modellen.
- Die Antworten erscheinen als Spalten und werden im Chat-Baum als Versionen derselben
  Nachricht gespeichert. Später schaltet „‹ 1/3 ›“ zwischen ihnen um.
- Wählt man eine Antwort aus, wird ihr Modell zum Standardmodell des Chats.
- Im Vergleich sind MCP-Werkzeuge aus, damit keine Rückfragen in mehreren Spalten
  gleichzeitig entstehen.

## Werkzeuge über MCP

{{< status "done" "4a" >}}

Umgesetzt und mit einem MCP-Testserver getestet: Werkzeuge in allen Anbieter-Adaptern, der
MCP-Client (offizielles Python-SDK) mit Verbindungstest und Einstufung in der Verwaltung, die
Werkzeugschleife im Chat, die Rückfrage vor nicht freigegebenen Werkzeugen und die Anzeige
jedes Aufrufs. Ein Test mit echten Modellen steht noch aus.

MultiGPT wird Client für das [Model Context Protocol](https://modelcontextprotocol.io/).
Modelle, die Werkzeuge unterstützen, können damit Funktionen aus angebundenen MCP-Servern
aufrufen.

- MCP-Server legt nur ein Verwalter an – lokal (`stdio`) oder per Streamable HTTP.
- Jede Rolle hat eine Liste erlaubter Server; geprüft wird vor jedem Aufruf.
- Werkzeuge, die etwas verändern oder nach außen senden, laufen erst nach einer
  Bestätigung im Chat. Unbekannte Werkzeuge gelten als bestätigungspflichtig.
- Jeder Aufruf erscheint im Chat als aufklappbare Zeile mit Argumenten, Ergebnis und Dauer.
- Höchstens 10 Werkzeugrunden je Antwort, Zeitlimit je Aufruf.

## Fragen an eigene Dokumente (RAG)

{{< status "done" "7" >}}

Sammlungen, Upload, Indexierung im Hintergrund mit Texterkennung (OCR), die Suche mit
Quellen im Chat, die Verwaltung im Admin, die komplett lokale Verarbeitung über LM Studio
und Verzeichnisquellen sind umgesetzt.

- Sammlungen anlegen und Dokumente hochladen (PDF, DOCX, TXT, MD, bis 25 MB je Datei),
  privat oder mit Gruppen geteilt – nur lesend oder mit Schreibrecht.
- Ein Hintergrunddienst (Worker) liest den Text, erkennt gescannte Seiten per OCR, teilt den Text in Abschnitte und berechnet Embeddings. Status und Fehler
  stehen bei jedem Dokument; vorübergehende Fehler werden automatisch wiederholt.
- Im Chat wählt man Sammlungen aus. Gesucht wird mit PostgreSQL und pgvector, auf Wunsch
  zusammen mit der deutschen Volltextsuche. Modelle mit Werkzeugen können auch selbst suchen.
- Unter der Antwort stehen die verwendeten Dokumente mit Seitenzahl, anklickbar zum Abschnitt.
- Abschnitte aus fremden privaten Sammlungen landen nie in einer Abfrage; der Zugriff wird
  in der Suchabfrage selbst geprüft.
- Verwalter haben im Admin eine RAG-Übersicht (Worker, Warteschlange, Speicher), können neu
  indexieren und Fehlgeschlagenes erneut versuchen – sie sehen dabei nur Metadaten, keine
  Inhalte.
- **Komplett lokal:** Embeddings berechnet LM Studio im Heimnetz mit nomic-embed-text
  (768 Dimensionen, mit den Präfixen `search_document:` und `search_query:`). Gescannte
  Seiten liest das Vision-Modell olmOCR über LM Studio, Tesseract auf dem Server springt
  auf Wunsch als Ersatz ein. So verlassen keine Dokumentinhalte das Haus. Die Knöpfe
  „Speichern und Embedding testen“ und „Speichern und OCR testen“ prüfen die Einstellungen
  sofort.
- **Verzeichnisquellen:** Ein Verwalter kann eine Sammlung aus einem Ordner auf dem Server
  oder NAS füllen, der periodisch neu eingelesen wird. Erlaubt sind nur Ordner unterhalb
  der in `RAG_SOURCE_ROOTS` freigegebenen Verzeichnisse.

## Websuche mit Quellen

{{< status "done" "8" >}}

- Ein Schalter „Websuche“ im Eingabefeld. Die Treffer gehen als nummerierte Quellen an
  das Modell, die Links stehen unter der Antwort. Das funktioniert auch mit lokalen Modellen.
  Modelle mit Werkzeugen können die Websuche auch selbst aufrufen.
- Such-Backend ist ein selbst gehostetes [SearXNG](https://docs.searxng.org/) im Heimnetz.
  Der Verwalter trägt die Adresse im Admin ein und prüft sie mit „SearXNG testen“. Die
  Schnittstelle ist austauschbar, eine Such-API ist bisher nicht angebunden.
- Abgerufene Seiten gelten als nicht vertrauenswürdig: Sie werden als Quellmaterial
  markiert, nicht als Anweisung, und Adressen im Heimnetz sind gesperrt.

## Bilder erzeugen und bearbeiten

{{< status "planned" "9" >}}

- Modus „Bild“ mit Formatwahl (quadratisch, quer, hoch) über einen Bild-Anbieter.
- Bereich auf dem Bild markieren und ändern lassen (Inpainting) oder Varianten erzeugen –
  sofern der gewählte Anbieter das kann.
- Klassische Bearbeitung (zuschneiden, skalieren, drehen, umwandeln, Text einsetzen,
  Collage) über einen mitgelieferten MCP-Server – auch per Anweisung wie
  „mach das Bild quadratisch“.
- Jede Bearbeitung erzeugt ein neues Bild, das Original bleibt erhalten.

## Sprache

{{< status "planned" "10" >}}

- Mikrofonknopf: Die Aufnahme wird zu Text, den man vor dem Senden korrigieren kann.
- Lautsprecherknopf an jeder Antwort, optional automatisches Vorlesen.
- Braucht HTTPS im Heimnetz, weil Browser das Mikrofon sonst nicht freigeben.

## Bewusst nicht in Version 1

- Kein Starten oder Aufwecken von LM Studio aus der App heraus.
- Kein Sprachdialog in Echtzeit.
- Keine eigenen Bildmodelle auf dem Server.
- Keine Trennung mehrerer Haushalte in einer Installation.
