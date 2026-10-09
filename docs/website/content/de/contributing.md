---
title: "Mitmachen"
slug: "mitmachen"
description: "Wie man bei MultiGPT mitmachen kann."
lead: "MultiGPT ist ein kleines Projekt, das sich noch in Entwicklung befindet. Rückmeldungen, Fragen und Beiträge sind willkommen."
menus:
  main:
    weight: 50
---

{{< repo-info >}}

## Wie du helfen kannst

- **Ausprobieren und berichten:** Die Installation auf einem frischen Debian 13, der
  Docker-Weg sowie Chats mit echten API-Keys und LM Studio sind noch nicht in der Praxis
  erprobt. Erfahrungen damit – gerade auf NAS-Systemen – helfen sehr.
- **Fragen und Ideen** als Issue: Was fehlt dir für den Einsatz im eigenen Haushalt?
- **Code:** Die nächsten Schritte stehen in der [Roadmap]({{< relref "roadmap" >}}). Bitte
  vor größeren Änderungen ein Issue aufmachen, damit wir uns abstimmen können.

## So ist das Projekt organisiert

- Planung und Architektur stehen in `docs/Plan.md`, die Arbeitspakete je Meilenstein in
  `docs/Implementierung.md`.
- Entwicklung mit `make`: `make install`, `make test` (pytest gegen PostgreSQL),
  `make lint` (ruff). Details unter [Installation]({{< relref "installation" >}}).
- Die Oberfläche bleibt ohne Node-Buildschritt und ohne CDNs. Anbieter-Adapter werden
  gegen die aktuelle API-Dokumentation des Anbieters geschrieben, Tests laufen gegen
  gemockte HTTP-Antworten – nie gegen echte APIs.
- Sprache der Oberfläche und der Projektdokumentation ist Deutsch.

## Lizenz

{{< license-info >}}
