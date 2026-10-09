---
title: "Funktionen"
slug: "funktionen"
description: "Was MultiGPT können soll: viele Anbieter, LM Studio, Familienkonten, Budgets, eigene Dokumente, Websuche, Bilder, Sprache und MCP-Werkzeuge."
lead: "Hier steht, was MultiGPT in Version 1 können soll. Fast alles davon ist geplant und noch nicht umgesetzt – jeder Abschnitt trägt seinen Status und den Meilenstein, in dem er kommen soll."
menus:
  main:
    weight: 10
---

## Anmeldung und Konten

{{< status "done" "1" >}}

- Login, Logout und Passwort ändern.
- Login-Drosselung: Nach fünf Fehlversuchen ist die Anmeldung für diesen Nutzernamen und
  diese Adresse 15 Minuten gesperrt.
- Keine Selbstregistrierung. Konten legt ein Verwalter an – heute per
  `mgpt-ctl createsuperuser` bzw. `make user`, später auch in der Oberfläche.

## Chatten mit vielen Anbietern

{{< status "planned" "3" >}}

- Chatliste in der Seitenleiste: neu, umbenennen, archivieren, löschen, Suche im Titel.
- Das Modell lässt sich **pro Nachricht** wählen. Antworten erscheinen gestreamt,
  ein Knopf bricht ab, eine Antwort lässt sich neu erzeugen.
- Anbieter werden per API-Key angebunden: OpenAI und alle OpenAI-kompatiblen Dienste
  (z. B. Mistral, Groq, OpenRouter), dazu Anthropic und Google Gemini (Meilenstein 4).
- Modelle werden in der Verwaltung gepflegt oder per `make sync-models` beim Anbieter abgefragt.
- Später (Meilenstein 5): Markdown mit Code-Hervorhebung und Kopierknopf, automatische
  Chattitel, System-Prompt pro Chat, Export als Markdown.

## Lokale Modelle mit LM Studio

{{< status "planned" "4" >}}

[LM Studio](https://lmstudio.ai/docs) läuft auf einem PC im Heimnetz und stellt einen
OpenAI-kompatiblen Server bereit. MultiGPT bindet ihn als Anbieter an, startet oder weckt
ihn aber nicht.

- Eine Statusanzeige in der Kopfzeile zeigt, ob LM Studio gerade erreichbar ist. Der
  Browser fragt alle 30 Sekunden nach, solange der Tab sichtbar ist.
- Ist LM Studio aus, sind lokale Modelle ausgegraut. Ist es an, werden die Modelle
  angeboten, die LM Studio tatsächlich meldet.
- Fällt LM Studio mitten in einer Antwort aus, bleibt der bisherige Text erhalten.
- Lokale Modelle kosten 0 € und bleiben auch bei ausgeschöpftem Budget nutzbar.

## Rollen, Gruppen und Budgets

{{< status "planned" "2 / 6" >}}

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

{{< status "planned" "4a" >}}

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
