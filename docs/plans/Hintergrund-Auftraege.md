# M17 Hintergrund-Aufträge: Antworten und Aufgaben ohne offene Seite

Planungsstand: 2026-11-05. Entwurf zur Freigabe; Zielversion legt der Nutzer fest.

## Anlass

Ein Familien-Assistenzsystem muss Aufgaben erledigen, während niemand zusieht: „Überwache das
Backup und melde mir Fehler“, „Sag mir morgen früh, ob die Pakete angekommen sind“, „Recherchiere
das und schick mir den Bericht“. Der Nutzer beschreibt das als Erwartung an MultiGPT (2026-11-04),
nicht als Extra.

Heute geht das nicht. Jede Ausführung ist an die offene Seite gebunden.

## Befund (Stand `main`, 2026-11-05)

- **Antworten laufen im HTTP-Stream.** `api.py` gibt `services.run_turn(turn)` als
  `StreamingHttpResponse` zurück, die Werkzeugschleife (bis `MAX_ROUNDS = 10`) eingeschlossen.
  Schließt der Browser die Verbindung, kommt beim nächsten Delta `GeneratorExit`
  (`services.py`, Klasse `_Loop`, um Zeile 1843). Die Antwort wird als `aborted` gespeichert, offene
  Werkzeugaufrufe werden mit `MSG_ABORTED` geschlossen (`_abort_open_calls`). Die Moduldoku
  von `services.py` beschreibt das als bewusste Entscheidung.
- **Dasselbe passiert bei einem Neustart von gunicorn** (Deploy) und bei dessen Zeitlimit.
- **Der Runner verliert seinen Auftraggeber.** `runner/delivery.py` stellt Rahmen über
  `RunnerMessage` und `LISTEN/NOTIFY` zu. Antworten gehen an den Prozess, der gesendet hat
  (`mgpt_runner_reply_<prozess>`). Endet die Anfrage, wartet dort niemand mehr. Container und
  Sitzung auf dem Runner laufen weiter, die Agentenschleife ist weg.
- **Eine Warteschlange gibt es, aber nicht für Chats.** `chat.models.Job` (Status, Versuche,
  `run_after`, `locked_at`, `cancel_requested`) wird von den `run_worker`-Spuren abgearbeitet,
  kennt aber nur `index_document` und `scan_directory`.
- **Kein Zeitplan, kein Melden nach außen.** Mitteilungen gibt es nur innerhalb von MultiGPT
  (`chat/notices.py`, A15) und nur für geteilte Chats. E-Mail oder Push gibt es nicht.

## Ziel

1. Eine Antwort läuft zu Ende, auch wenn die Seite geschlossen, neu geladen oder auf einem
   anderen Gerät geöffnet wird. Abgebrochen wird nur ausdrücklich (Stopp, Escape).
2. Die Seite hängt sich jederzeit an eine laufende Antwort an und verliert keinen Text.
3. Aufgaben laufen nach Zeitplan oder auf Anstoß, ohne Mensch, mit klaren Grenzen.
4. Aufgaben melden sich von selbst, wenn es etwas zu melden gibt, und schweigen sonst.

## Stufen

Jede Stufe wird für sich geliefert: Tests, volle Suite, Browserprüfung, dann Deploy.

### M17-01 Antworten im Worker

- **Auftrag statt Stream.** Senden, Neu erzeugen, Bearbeiten und Fortsetzen nach Rückfrage
  legen einen `Job` der neuen Art `chat_turn` an (Nutzlast: Nachricht, Turn-Daten) und geben
  sofort die Nachrichten-ID zurück.
- **Eigene Spuren für Chats.** Chat-Aufträge dürfen nicht hinter einer langen Indexierung warten.
  `run_worker` bekommt Spuren nur für `chat_turn` (Anzahl einstellbar, Vorgabe 2).
- **Ereignis-Log.** Neues Modell `TurnEvent` (Nachricht, laufende Nummer, Name, Daten), dieselben
  Ereignisse wie heute im SSE-Stream (`start`, `delta`, `status`, `tool_*`, `runner_*`, `done`,
  `error`). Der Worker schreibt sie und ruft `pg_notify('mgpt_turn_<message>', nummer)` auf.
  Deltas werden gebündelt (etwa alle 100 ms), damit die Tabelle nicht je Token wächst.
- **Abonnieren.** `GET /api/messages/<id>/events/?after=<n>` liefert die gespeicherten Ereignisse
  ab Nummer n und hält dann per `LISTEN` offen, bis `done` oder `error` kommt. Ein Abbruch dieser
  Verbindung beendet nur das Zuhören.
- **Abbrechen.** Stopp und Escape rufen `POST /api/messages/<id>/cancel/` auf. Das setzt
  `cancel_requested`, und der Worker prüft es bei jedem Delta und vor jeder Werkzeugrunde.
  Der heutige Pfad über `GeneratorExit` entfällt.
- **Oberfläche.** `chat.js` hängt sich beim Laden an jede Antwort mit Status „läuft“ an. Neu ist
  dafür der Status `running` statt des Platzhalters `aborted`, den die Moduldoku heute begründet.
  Ein Worker, der stirbt, hinterlässt `running` mit altem `locked_at`. Solche Antworten setzt der
  Worker nach einer Frist auf `aborted`, so wie `chat/rag/jobs.py` hängende Jobs heute über
  `locked_at` und `STALE_AFTER` erkennt.
- **Aufräumen.** Ereignisse fertiger Antworten werden nach einer Frist gelöscht. Der fertige Text
  steht ohnehin in `Message.content`.
- **Betrifft auch:** Vergleichsmodus (mehrere Aufträge), Bildmodus, `ask` über MCP
  (`node/tool_ask.py`).

### M17-02 Werkzeuge und Runner im Worker

- Werkzeugaufrufe laufen im Worker-Prozess. Antworten des Runners gehen damit an einen Prozess,
  der bleibt.
- **Rückfragen ohne Seite.** Wartet ein Aufruf auf Bestätigung (`awaiting_confirmation`, gibt es
  schon), endet der Auftrag mit gespeichertem Zustand. Bestätigen auf irgendeinem Gerät legt einen
  neuen Auftrag an, der fortsetzt (`prepare_resume`, gibt es schon).
- Der Runner-Dienst merkt sich laufende Befehle je Sitzung, damit ein neuer Worker nach einem
  Neustart die Ergebnisse abholen kann, statt sie zu verlieren. Ob dafür `RunnerMessage` reicht
  oder eine eigene Befehlstabelle nötig ist, klärt die Umsetzung (mit Messung).

### M17-03 Aufgaben

- **Modell `Task`:** Besitzer, Name, Auftrag (Prompt), Modell, erlaubte MCP-Server und Werkzeuge,
  Zeitplan, Ziel-Chat, Grenzen (Runden je Lauf, Laufzeit, Kosten je Lauf und je Monat), aktiv
  ja/nein, letzter Lauf und dessen Ergebnis.
- **Zeitplan:** einfache Formen statt freiem Cron: „alle N Minuten/Stunden“, „täglich um HH:MM“,
  „werktags um HH:MM“, „einmal am …“. Der Worker legt fällige Läufe als `chat_turn` an.
- **Ein Chat je Aufgabe.** Jeder Lauf ist eine normale Antwort in diesem Chat. Verlauf, Kosten,
  Werkzeugaufrufe und Fehler sind damit sichtbar, ohne neue Ansicht. Der Verlauf dient als
  Gedächtnis zwischen den Läufen; damit er nicht unbegrenzt wächst, gehen nur die letzten Läufe mit.
- **Anlegen:** im Chat („Mach das jeden Morgen um 7“ → eingebautes Werkzeug `schedule_task` mit
  Rückfrage) und auf einer Seite „Aufgaben“ (Liste, Bearbeiten, Pausieren, „Jetzt ausführen“).

### M17-04 Melden

- **Eingebautes Werkzeug `notify_user`** (Titel, Text, Dringlichkeit). Ruft das Modell es nicht
  auf, entsteht keine Meldung. So schweigt „melde mir Fehler“ bei einem guten Backup.
- **In MultiGPT:** als Mitteilung (`Notice`, Menü „Mitteilungen“) mit Verweis auf den Lauf.
- **Nach außen:** ein Kanal, den der Nutzer wählt (siehe Entscheidungen). Grenze gegen Flut:
  höchstens N Meldungen je Aufgabe und Stunde, gleiche Meldung nicht doppelt.

## Sicherheit

- Aufgaben laufen ohne Mensch. Werkzeuge mit Rückfrage können nicht bestätigt werden, solange
  niemand da ist. Siehe Entscheidung zu Rückfragen.
- Inhalte, die eine Aufgabe liest (Logs, Webseiten, Mails), können Anweisungen enthalten
  (Prompt-Injection). Vorgabe für Aufgaben deshalb nur lesende Werkzeuge. Schreibende nur einzeln
  freigegeben, wie heute je MCP-Server und Modell.
- Rechte wie im Chat (`can`), dazu ein eigenes Recht „Aufgaben anlegen“ je Rolle. Budget je Konto
  gilt auch für Aufgaben, eine Aufgabe ohne Budget pausiert und meldet das.
- Jeder Lauf ist im Chat der Aufgabe nachvollziehbar; Werkzeugaufrufe wie heute im Audit.

## Beispiel „Backup überwachen“

Für das Beispiel fehlt neben M17 ein Werkzeug, das den Backup-Stand sieht. Auf diesem Rechner läuft
Bareos (`bareos-dir`). Möglich wären ein kleiner MCP-Server für Bareos (nur lesend: letzte Jobs,
Status, Fehler) oder ein freigegebener Befehl über den Runner. Die Aufgabe wäre dann: täglich um
7:00, Modell mit Werkzeugen, Server „Bareos“ nur lesend, Auftrag „Prüfe die Backup-Jobs der
letzten 24 Stunden. Melde dich nur, wenn ein Job fehlgeschlagen ist oder fehlt“.

## Entscheidungen für den Nutzer

| Nr. | Frage | Empfehlung |
|---|---|---|
| A25 | Kanal für Meldungen nach außen: E-Mail, Matrix/Element, ntfy oder Web-Push? | Matrix/Element (Element ist in der Familie offen) oder ntfy; E-Mail als Rückfall |
| A26 | Werkzeug mit Rückfrage in einer Aufgabe: Lauf anhalten und melden, oder in Aufgaben gar nicht anbieten? | anhalten und melden; Bestätigung setzt fort |
| A27 | Wer darf Aufgaben anlegen? | Erwachsene und Verwalter; Kinder nicht |
| A28 | Wie viele Chat-Spuren im Worker? | 2, im Admin einstellbar |
| A29 | Aufgaben nur im Chat anlegen oder zusätzlich eigene Seite? | beides; Seite für Übersicht und Pause |

## Aufwand und Reihenfolge

M17-01 ist der größte Schritt: Er ändert den Kern (`services.py`, Stream-API, `chat.js`,
Worker) und viele Tests. M17-02 bis M17-04 bauen darauf auf. Grob geschätzt sind es mehrere
Arbeitstage für M17-01 und je ein bis zwei für die folgenden Stufen. Die Schätzung ist ungeprüft.
