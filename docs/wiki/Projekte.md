# Projekte

Mit Projekten fasst du zusammengehörige Chats zusammen, so wie in ChatGPT oder Claude: zum Beispiel
„Umzug 2027“, „Steuererklärung“ oder „Gartenplanung“. Ein Projekt gibt seinen Chats gemeinsame
Vorgaben mit:

- **Anweisungen:** gelten in jedem Chat des Projekts, zusätzlich zum System-Prompt des Chats,
- **Standardmodell:** im Eingabefeld vorausgewählt,
- **Sammlungen:** die Dokumente dieser Sammlungen sind im Eingabefeld vorausgewählt.

Projekte sieht nur, wer sie angelegt hat. Diese Seite richtet sich an alle Nutzer und an Verwalter.

## Projekt anlegen

1. In der Seitenleiste neben **„Projekte“** auf **„Neues Projekt“** klicken und einen Namen
   eingeben.
2. Die Projektseite öffnet sich. Unter **„Einstellungen“** trägst du Beschreibung, Anweisungen,
   Standardmodell, Sammlungen und eine Farbe ein und klickst **„Speichern“**.

Ohne JavaScript geht das über die Seite **Projekte** (`/projekte/`).

## Chats im Projekt

- **Neuer Chat im Projekt:** im Menü (⋯) des Projekts in der Seitenleiste oder oben auf der
  Projektseite. Über dem Eingabefeld steht dann „Neuer Chat im Projekt …“.
- **Vorhandenen Chat verschieben:** im Menü (⋯) des Chats in der Seitenleiste **„In Projekt
  verschieben …“** wählen oder im geöffneten Chat oben auf **„Projekt …“** klicken. Dort lässt
  sich auch „Ohne Projekt“ wählen. **„Aus Projekt nehmen“** im Chatmenü geht direkt.
- Alle Menüs funktionieren mit der Tastatur: Tab zum Knopf (⋯), Enter öffnet, Pfeiltasten wählen,
  Escape schließt.

Einen Chat kann nur sein Besitzer einem Projekt zuordnen, und nur einem eigenen Projekt.

## Anweisungen, Modell und Sammlungen

Die **Anweisungen** gehen bei jeder Antwort im Chat an das Modell, und zwar in dieser Reihenfolge:

1. Grundregeln von MultiGPT und fester Prompt deiner Rolle (legt der Verwalter fest),
2. Hinweise von MultiGPT (z. B. zu Quellen und Websuche),
3. **Anweisungen des Projekts**,
4. System-Prompt des Chats.

Gilt etwas für alle Chats des Projekts, gehört es in die Anweisungen. Gilt es nur für einen Chat,
gehört es in dessen System-Prompt; der steht danach und kann die Projektanweisungen damit ergänzen
oder für diesen Chat abwandeln. Das Modell erfährt, dass die Anweisungen von dir stammen und nicht
von MultiGPT.

Das **Standardmodell** ist vorausgewählt, wenn deine Rolle es erlaubt und der Anbieter erreichbar
ist. Sonst bleibt das zuletzt gewählte Modell. Hat ein Chat schon ein eigenes zuletzt genutztes
Modell, bleibt es dabei.

Die **Sammlungen** des Projekts sind im Eingabefeld unter „Dokumente“ angehakt, solange du die
Auswahl in diesem Chat nicht selbst geändert hast. Sammlungen, die du nicht (mehr) lesen darfst,
fallen weg; die Leserechte gelten wie immer.

Die **Kreativität** des Projekts (Präzise 0,2, Ausgewogen 0,7, Kreativ 1,0 oder Standard) gilt
für Chats des Projekts, die unter „System-Prompt“ keine eigene Kreativität gewählt haben. Bei
„Standard“ gilt die Einstellung des Verwalters. Details unter
[Chat-Einstellungen](Chat-Einstellungen).

## Seitenleiste, Suche und Archiv

- Der Abschnitt **„Projekte“** steht über **„Chats“** (Chats ohne Projekt). Mit dem Pfeil klappst
  du ein Projekt auf oder zu; der Browser merkt sich das. Das Projekt des geöffneten Chats ist
  immer aufgeklappt. Angeheftete Projekte stehen oben. Je Projekt zeigt die Leiste die 20 neuesten
  Chats, alle stehen auf der Projektseite.
- Die **Suche** findet Chattitel und Projektnamen. Passt der Name eines Projekts, erscheint es mit
  allen seinen Chats.
- **Archivieren** (Projektmenü oder Projektseite) blendet das Projekt samt Chats aus der
  Seitenleiste aus. Unter **„Archiv“** stehen archivierte Projekte mit ihren Chats und Projekte mit
  archivierten Chats. Wiederherstellen geht über das Menü oder die Projektseite.

## Projekt löschen

Im Projektmenü **„Löschen …“** oder unten auf der Projektseite. Du musst wählen:

- **Chats behalten:** Die Chats bleiben und stehen danach unter „Chats“ ohne Projekt.
- **Chats mitlöschen:** Alle Chats des Projekts werden endgültig gelöscht, mit allen Nachrichten
  und Anhängen. Geteilte Chats verschwinden damit auch für die Personen, mit denen du sie geteilt
  hast.

Der Knopf nennt die Folgen ausdrücklich („Projekt und 3 Chats löschen“). Rückgängig machen lässt
sich das nicht.

## Teilen

Projekte selbst lassen sich noch nicht teilen; das ist geplant (eine Freigabe des Projekts gilt
dann für alle seine Chats). Einzelne Chats eines Projekts kannst du wie jeden Chat teilen (siehe
[Chats teilen](Chats-teilen)). Die Empfänger finden den Chat unter **„Mit mir geteilt“** und sehen
dein Projekt nicht: weder Namen noch Anweisungen noch die Projektzeile im Export. Die Anweisungen
des Projekts gelten trotzdem für alle Antworten im Chat, auch wenn ein Empfänger schreibt, damit
sich der Chat für alle gleich verhält.

## Export

Der Markdown-Export eines Chats nennt oben das Projekt („Projekt: Umzug 2027“), aber nur für dich
als Besitzer. Die Anweisungen stehen nicht im Export.

## Für Verwalter

- Im Admin unter **Chat → Projekte** stehen nur Besitzer, Name und Anzahl der Chats.
  Beschreibung und Anweisungen sind dort nicht sichtbar, ändern oder löschen geht im Admin nicht.
- Auch Verwalter sehen fremde Projekte in der Oberfläche nicht (die Seiten antworten mit „nicht
  gefunden“).
- Die Datenbank-Migration heißt `chat.0023_projects`. Sie legt die Tabelle für Projekte an und
  ergänzt Chats um die Spalte für das Projekt; bestehende Chats bleiben ohne Projekt.

## Schnittstelle (für Entwickler)

| Methode und Pfad | Zweck |
|---|---|
| `GET /api/projects/` (`?archived=1`) | eigene Projekte, ohne Inhalte |
| `POST /api/projects/` | anlegen: `name`, optional `description`, `instructions`, `default_model`, `collections`, `color` |
| `GET`, `PATCH /api/projects/<id>/` | lesen bzw. ändern, zusätzlich `pinned`, `archived` |
| `DELETE /api/projects/<id>/?chats=keep` bzw. `?chats=delete` | löschen, die Angabe ist Pflicht |
| `POST /api/conversations/<id>/project/` | Chat zuordnen: `{"project": <id>}` bzw. `{"project": null}` |
| `POST /api/conversations/` | neuer Chat, optional mit `project` |

Fremde Projekte und nicht lesbare Chats antworten mit 404, geteilte fremde Chats beim Zuordnen mit
403. Alle ändernden Anfragen brauchen das CSRF-Token im Header `X-CSRFToken`.
