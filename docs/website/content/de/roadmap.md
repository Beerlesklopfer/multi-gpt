---
title: "Roadmap"
description: "Die Meilensteine von MultiGPT und ihr Stand."
lead: "MultiGPT wird Meilenstein für Meilenstein gebaut. Nach jedem Meilenstein laufen die Tests, es gibt einen kurzen Bericht, dann geht es weiter. Umgesetzt sind bisher die Meilensteine 1 bis 8 einschließlich 4a (MCP-Werkzeuge). Offen sind Bilder (9), Sprache (10), Musik (11), Betrieb (12) und Scratchpad (13)."
menus:
  main:
    weight: 40
---

{{< roadmap >}}

## Reihenfolge

Der kritische Pfad ist **1 → 2 → 3 → 4 → 4a**: Alles, was Werkzeuge nutzt (Dokumentensuche,
Websuche und Bildbearbeitung als Werkzeug), setzt die MCP-Schleife voraus. Komfort (5) und
Verbrauch/Budgets (6) können nach Meilenstein 3 parallel laufen. HTTPS mit nginx wird vor
den Sprachfunktionen (10) eingerichtet.

## Später (nach Version 1)

Bilder als Eingabe an Modelle, wiederverwendbare Prompt-Vorlagen, Volltextsuche über
alle Nachrichten.
