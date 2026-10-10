---
title: "MultiGPT"
description: "Selbst gehostetes Multi-KI-Chatsystem für Familie und Haushalt: eigene Daten, viele Anbieter, ein Chat."
eyebrow: "Selbst gehostet · in Entwicklung · Version 0.3"
headline: "Ein KI-Chat für die ganze Familie – auf dem eigenen Server"
lead: "MultiGPT ist eine Web-App für das Heimnetz, über die alle im Haushalt mit verschiedenen KI-Modellen chatten – von OpenAI über Anthropic und Google bis zum lokalen LM Studio. Chats, Konten und API-Keys bleiben dabei auf dem eigenen Server."
pillarsTitle: "Die Idee dahinter"
pillarsLead: "Das sind die Ziele für Version 1. Was davon schon umgesetzt ist, zeigen die Statusmarken weiter unten."
pillars:
  - title: "Eigene Daten"
    text: "Oberfläche, Chatverläufe und API-Keys liegen nur auf dem eigenen Server im Heimnetz, in PostgreSQL. API-Keys werden verschlüsselt gespeichert. Nach außen gehen nur die Anfragen an die gewählten KI-Anbieter."
  - title: "Ein Chat für viele Anbieter"
    text: "Eine Oberfläche für alle angebundenen Modelle, Modellwahl pro Nachricht. Lokale Modelle aus LM Studio laufen kostenlos mit, sobald der PC an ist."
  - title: "Familienkonten mit Rollen und Budgets"
    text: "Jedes Familienmitglied hat ein eigenes Konto. Rollen legen fest, wer welche Modelle und Funktionen nutzen darf. Monatsbudgets halten die Kosten im Rahmen."
menus:
  main:
    name: "Start"
    weight: 1
---

## Für wen ist das?

Für technisch interessierte Menschen, die ihrer Familie oder ihrem Haushalt Zugang zu
KI-Modellen geben wollen, ohne für jede Person ein eigenes Abo abzuschließen und ohne die
Chats bei einem Anbieter zu sammeln. Gedacht ist MultiGPT für einen Server, der ohnehin
läuft – etwa ein NAS oder einen kleinen Heimserver mit Debian – und nur im Heimnetz
erreichbar ist.

## Was es heute schon gibt

Man kann mit eigenen API-Keys chatten: mit OpenAI, Anthropic, Google Gemini, OpenRouter
und LM Studio im Heimnetz, mit gestreamten Antworten, Modellwahl pro Nachricht, Markdown,
Formeln und Code-Hervorhebung. Dazu kommen Familienkonten mit Rollen, ein Kontenrahmen mit
Budgets, ein Vergleichsmodus für zwei oder drei Modelle, MCP-Werkzeuge mit Rückfrage,
Fragen an eigene Dokumente mit zitierfähigen Quellen (auf Wunsch komplett lokal über
LM Studio), die Websuche über ein selbst gehostetes SearXNG, verschlüsselte API-Keys, eine
Verwaltung für Verwalter und ein Debian-Paket, das Datenbank, Schema und nginx selbst
einrichtet.

## Neu in 0.3

- **Anhänge und Bild-Eingabe:** Bilder und Dokumente in den Chat ziehen oder einfügen;
  Ortsdaten aus Fotos werden entfernt.
- **Projekte** und **geteilte Chats** mit Rechten nach RWUD.
- **Zitieren** mit Abschnitt, Seite und Absatz in DIN ISO 690, APA, Harvard, Chicago, MLA
  oder BibTeX; Abbildungen in Dokumenten werden durchsuchbar.
- **Bilder erzeugen** mit OpenAI oder Gemini.
- **Berechnungen** mit numpy, sympy und matplotlib in einer abgeschotteten Sandbox.
- **Seitenabruf** für Modelle und eine Markierung erfundener Links.
- **MCP** mit JSON-Import, Online-Status und Freigabe je Modell, dazu die
  **Fähigkeiten-Matrix** und der **Kontenrahmen** für die Kosten.
- **0.3.1:** druckfertige **Arbeitsblätter als PDF** – Lineaturen, Rechenblätter mit
  Lösungen, Sachunterricht.

Wer von 0.2 aktualisiert, findet die nötigen Schritte unter
[Installation]({{< relref "installation" >}}).

Mit OpenAI und LM Studio läuft es bereits echt, die übrigen Cloud-Anbieter sind bisher
überwiegend simuliert getestet; eine frische Debian-Installation steht noch aus, der
Docker-Build ist ungetestet. Fertige Release-Pakete gibt es noch nicht. Bildbearbeitung,
Sprache, Musik, Backup, das Scratchpad und der Runner folgen – Details in der
[Roadmap]({{< relref "roadmap" >}}).

## Technik in einem Satz

Python und Django 5.2 hinter gunicorn, PostgreSQL mit pgvector als einzige Datenbank,
Oberfläche aus Django-Templates und etwas Vanilla-JavaScript, kein Node-Buildschritt,
keine CDNs. Mehr dazu unter [Architektur]({{< relref "architecture" >}}).
