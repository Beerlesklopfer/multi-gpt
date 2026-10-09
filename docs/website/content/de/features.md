---
title: "Funktionen"
slug: "funktionen"
description: "Was MultiGPT können soll: viele Anbieter, LM Studio, Familienkonten, Budgets, eigene Dokumente, Websuche, Bilder, Sprache und MCP-Werkzeuge."
lead: "Hier steht, was MultiGPT in Version 1 können soll. Chatten mit vielen Anbietern, LM Studio und Familienkonten sind umgesetzt, MCP-Werkzeuge teilweise; Budgets, eigene Dokumente, Websuche, Bilder und Sprache sind noch geplant. Jeder Abschnitt trägt seinen Status und den Meilenstein."
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
- Lokale Modelle kosten 0 € und sollen auch bei ausgeschöpftem Budget nutzbar bleiben
  (Budgets kommen mit Meilenstein 6).
- Mit simulierten Anbietern getestet; der Test mit LM Studio im echten Heimnetz steht noch aus.

## Rollen, Gruppen und Budgets

{{< status "partial" "2 / 6" >}}

Umgesetzt sind Rollen, Gruppen, Konten und die zentrale Rechteprüfung (Meilenstein 2).
Budgets, Verbrauchsübersicht und die Seite „Familie“ folgen mit Meilenstein 6.

Vier Startrollen, in der Verwaltung anpassbar:

| Rolle | Darf |
|---|---|
| Verwalter | Alles: Anbieter, Keys, Modelle, Konten, Rollen, Gruppen, Budgets, Verbrauch aller |
| Erwachsener | Alle freigegebenen Modelle und Funktionen, eigene Sammlungen, Teilen mit Gruppen |
| Jugendlicher | Nur freigegebene Modelle und Funktionen, fester System-Prompt der Rolle, Monatsbudget |
| Gast | Chat mit einem festgelegten Modell, kein Upload, kein Teilen, kleines Budget |

- Rechte werden auf dem Server geprüft, vor jeder Seite und vor jedem Anbieteraufruf –
  nicht nur durch Ausblenden in der Oberfläche.
- **Budgets:** Monatsbudget je Rolle, je Person überschreibbar. Bei 80 % gibt es einen
  Hinweis, bei 100 % sind kostenpflichtige Modelle bis zum Monatswechsel gesperrt.
- **Verbrauch:** Tokens und geschätzte Kosten je Person, Modell und Monat.
- **Privatsphäre:** Chats sind privat. Auch Verwalter sehen fremde Chats nicht, nur
  Verbrauchszahlen. Ob Eltern Chats von Jugendlichen-Konten einsehen können, ist noch offen;
  vorgesehen ist höchstens eine Option je Konto, standardmäßig aus und für das Mitglied sichtbar.
- **Gruppen:** Sammlungen und einzelne Chats lassen sich mit Gruppen teilen, lesend oder
  mit Schreibrecht.
- Eine Seite „Familie“ für Verwalter: Konten anlegen und sperren, Rollen zuweisen,
  Passwörter zurücksetzen, Gruppen pflegen.

## Vergleichsmodus

{{< status "planned" "6" >}}

Dieselbe Frage an zwei oder drei Modelle gleichzeitig stellen und die Antworten
nebeneinander lesen.

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

{{< status "planned" "7" >}}

- Sammlungen anlegen und Dokumente hochladen (PDF, DOCX, TXT, MD), privat oder mit
  Gruppen geteilt.
- Ein Hintergrundprozess zerlegt die Texte und berechnet Embeddings; gesucht wird mit
  PostgreSQL und pgvector.
- Unter der Antwort stehen die verwendeten Dokumente mit Seitenzahl.
- Abschnitte aus fremden privaten Sammlungen landen nie in einer Abfrage.

## Websuche mit Quellen

{{< status "planned" "8" >}}

- Ein Schalter „Websuche“ im Eingabefeld. Die Treffer gehen als nummerierte Quellen an
  das Modell, die Links stehen unter der Antwort. Das funktioniert auch mit lokalen Modellen.
- Als Such-Backend ist ein selbst gehostetes [SearXNG](https://docs.searxng.org/) oder
  eine Such-API vorgesehen.
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
