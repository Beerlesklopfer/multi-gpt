---
title: "Roadmap"
description: "Die Meilensteine von MultiGPT und ihr Stand."
lead: "MultiGPT wird Meilenstein für Meilenstein gebaut. Nach jedem Meilenstein laufen die Tests, es gibt einen kurzen Bericht, dann geht es weiter. Stand Version 0.3.1: Die Meilensteine 1 bis 8 einschließlich 4a sind umgesetzt, Bilder (9) und Betrieb (12) teilweise. Offen sind Sprache (10), Musik (11), Scratchpad (13) und Runner (14)."
menus:
  main:
    weight: 40
---

{{< roadmap >}}

## Reihenfolge

Der kritische Pfad ist **1 → 2 → 3 → 4 → 4a**: Alles, was Werkzeuge nutzt (Dokumentensuche,
Websuche, Berechnungen, PDF-Blätter, Bilderzeugung und der Runner), setzt die MCP-Schleife
voraus. Komfort (5) und Verbrauch/Budgets (6) konnten nach Meilenstein 3 parallel laufen. HTTPS
mit nginx ist bereits im Paket und damit vor den Sprachfunktionen (10) fertig.

Beim Scratchpad (13) kommen Kontextmenü und Prompt-Vorlagen zuerst. Der Runner (14) baut auf
MCP, dem Kontenrahmen aus Meilenstein 6 und nginx mit TLS auf; zuerst gibt es dort nur
Werkzeuge, keinen Coding-Agenten im Container.

## Später (nach Version 1)

Volltextsuche über alle Nachrichten und ein „Gedächtnis über Chats“ (Vorschlag, noch nicht
freigegeben).
