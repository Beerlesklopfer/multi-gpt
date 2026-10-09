# Implementierungsgerüst MultiGPT

Abgeleitet aus [Plan.md](Plan.md), Stand 2026-10-09. Dieses Dokument zerlegt den Plan in Arbeitspakete, hält die Betriebsentscheidungen fest und ordnet die offenen Fragen den Meilensteinen zu, die sie blockieren.

Arbeitsweise wie im Plan: meilensteinweise umsetzen, nach jedem Meilenstein Tests, kurzer Bericht, Freigabe abwarten.

---

## 1. Festgelegte Betriebsentscheidungen

Diese Entscheidungen sind in [Plan.md](Plan.md) eingearbeitet (Abschnitte 2, 5, 8g, 9, 10).

| Thema | Festlegung |
|---|---|
| Namen | Paket, Systemnutzer, Unit, `/etc`- und `/var/lib`-Pfade heißen `multi-gpt`. Python-Pakete: `multigpt` mit allen Django-Apps darunter (`multigpt.chat`, `multigpt.accounts`), dazu der eigenständige MCP-Server `mcp_imagetools`. Bezeichner im Code sind englisch, die Oberfläche ist deutsch (Plan 2). |
| Projektlayout | Nach Plan 5: Alle Django-Apps liegen unter `multigpt/`. Abhängigkeiten nur in `pyproject.toml`, keine `requirements.txt` (dh-virtualenv installiert sie nur, wenn vorhanden, danach immer `pip install .`). |
| Installation | `.deb` mit dh-virtualenv nach `/usr/share/python/multi-gpt`, Unit `multi-gpt.service` über `dh_installsystemd`. Entwicklung mit `make install` in `.venv`. |
| Konfiguration | `/etc/multi-gpt/.env` (`root:multi-gpt`, 0640) als `EnvironmentFile`, auch für Gunicorn-Variablen. Das postinst erzeugt sie einmalig mit generierten Schlüsseln, `purge` entfernt sie. Entwicklung: `.env` im Projektordner. Die Settings lesen zusätzlich die Datei aus `MULTI_GPT_ENV_FILE`. |
| Netzwerk | debconf fragt bei der Installation nach Bind-Adresse (`MULTI_GPT_BIND`, vorgeschlagen wird die erste IPv4-Adresse mit Port 8000), `ALLOWED_HOSTS` und `CSRF_TRUSTED_ORIGINS`. Das postinst übernimmt die Antworten geprüft in `/etc/multi-gpt/.env`. Handänderungen dort werden bei der nächsten Abfrage zum Vorschlag. Erneut abfragen mit `dpkg-reconfigure multi-gpt`. |
| Datenbank | Das preinst legt bei lokal laufendem PostgreSQL die Rolle `multi-gpt`, die Datenbank `multi-gpt` und die Extension `vector` an. Das geht idempotent und nur, wenn `DATABASE_URL` auf die lokale Standard-Datenbank zeigt. Die Anmeldung läuft per Peer-Auth als Systemnutzer `multi-gpt`. Das preinst bricht nie ab, `MULTI_GPT_SKIP_DB_SETUP=1` überspringt es. |
| Verwaltung | `/usr/bin/mgpt-ctl`: Wrapper um `manage.py`, läuft als `multi-gpt` mit `/etc/multi-gpt/.env`. Das postinst migriert bei Installation und Upgrade automatisch, bevor der Dienst (neu) startet, sofern die Datenbank erreichbar ist. Sonst gibt es einen Hinweis auf `sudo mgpt-ctl migrate`, die Installation scheitert daran nicht. `MULTI_GPT_SKIP_MIGRATE=1` überspringt die Migration. |
| Statische Dateien | `collectstatic` beim Paketbau, `STATIC_ROOT=/usr/share/python/multi-gpt/static`, ausgeliefert über WhiteNoise. |
| Medien | `MEDIA_ROOT=/var/lib/multi-gpt/media`. Bei einem Pfad außerhalb von `/var/lib/multi-gpt` muss `ReadWritePaths=` in der Unit ergänzt werden. |
| Gunicorn | `gthread`, 2 Worker × 8 Threads, Timeout 300, über `MULTI_GPT_*` änderbar. |
| Worker-Prozess | Zweite Unit `multi-gpt-worker.service` im selben Paket, ab M7. |
| MCP | Ein Loop-Thread je gunicorn-Prozess, Mutexe für Start und Verbindungsaufbau (Plan 8g, M4a-01). |
| Docker | Ausweichweg: Das Image installiert dasselbe `.deb`. `compose.yaml` mit `db` (PostgreSQL + pgvector). |
| Health-Check | `/healthz/` als Middleware ganz vorne: ohne Login, ohne `ALLOWED_HOSTS`-Prüfung, prüft die DB-Verbindung. Bei 503 eine WARNING ohne Stacktrace. |
| Konten und Gruppen | App `multigpt.accounts`: `accounts.User` erweitert `AbstractUser` (`AUTH_USER_MODEL`), `accounts.UserGroup` erweitert `auth.Group` per Tabellenvererbung. Kein separates `Profil`. |
| Login-Drosselung | django-axes, Sperre je Nutzername und IP nach 5 Fehlversuchen für 15 Minuten. |
| Lizenz | AGPL-3.0-or-later: `LICENSE`, Metadaten in `pyproject.toml`, `debian/copyright`, Website. |
| Versionen | Python 3.13 (Debian 13), Django 5.2 LTS. Entwicklungs-DB: PostgreSQL 18 mit pgvector 0.8. |

Das Zielsystem hat Debian/apt und ein PostgreSQL mit pgvector (Fragen 1 und 4, geklärt am 2026-10-09).

---

## 2. Ergänzungen zum Datenmodell (entschieden 2026-10-09)

Diese Lücken im ursprünglichen Datenmodell sind geschlossen und in [Plan.md](Plan.md) Abschnitt 6 eingearbeitet:

1. **Fähigkeiten eines Modells:** `AIModel.capability` bleibt die Hauptart. Dazu kommen die booleschen Felder `supports_tools` (7, 8g) und `can_edit_images` (8e). Bild als Eingabe folgt in v2.
2. **Kosten als Momentaufnahme:** `Message.cost` und `Attachment.cost` halten die Kosten zum Zeitpunkt der Antwort fest. Spätere Preisänderungen verfälschen Verbrauch und Budget dadurch nicht.
3. **Freigaben:** Die Tabelle `Share` (Ziel `Conversation` oder `Collection`, `group`, `can_write`) gilt für Chats und Sammlungen gleichermaßen.
4. **Gruppen:** `accounts.UserGroup` erweitert Djangos `auth.Group` per Tabellenvererbung. Die Mitgliedschaft läuft über `User.groups`, Zusatzfelder liegen in `UserGroup`. Im Admin ersetzt `UserGroup` die Django-Gruppen.
5. **Konten:** `accounts.User` erweitert Djangos `AbstractUser` (`AUTH_USER_MODEL = "accounts.User"`). Ein Modell `Profil` gibt es nicht. In M2 kommen die Felder `role`, `display_name`, `monthly_budget_override`, `allow_supervision` (5a) und `auto_read_aloud` (8c) dazu. Gesperrt wird über `is_active`.
6. **Status von Werkzeugaufrufen:** `ToolCall.status` hat die Werte `awaiting_confirmation`, `rejected`, `running`, `ok`, `error` und `timeout`.
7. **Nachrichtenstatus:** `Message.status` hat die Werte `complete`, `aborted` und `error`, dazu `error` als Fehlertext.
8. **Bildherkunft:** `Attachment.source_image` verweist auf das Ausgangsbild (Fremdschlüssel auf `Attachment` selbst).
9. **Vektordimension:** `Chunk.embedding` braucht für den HNSW-Index eine feste Dimension. Sie ergibt sich aus dem OpenAI-Embedding-Modell (Frage 4b), das in M7 festgelegt wird. Die Migration für `Chunk` entsteht deshalb erst in M7.

---

## 3. Arbeitspakete je Meilenstein

Konvention: `Mx-nn` ist ein Arbeitspaket. Ein Paket ist fertig, wenn Code, Tests und eine Zeile im Meilensteinbericht vorliegen.

### M1 – Grundgerüst
*Abhängig von: Frage 1 (NAS), Frage 4 (PostgreSQL).*

- **M1-01** Django-5.2-Projekt `multigpt` mit der App `multigpt.chat` im Layout nach Plan. `pyproject.toml` mit `django`, `django-environ`, `django-axes`, `gunicorn`, `psycopg[binary]`, `pgvector`, `whitenoise`, Extra `dev` mit `pytest`, `pytest-django`, `ruff`.
- **M1-02** Settings ausschließlich aus der Umgebung: `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `DATABASE_URL`, `MEDIA_ROOT`, `STATIC_ROOT`, `FIELD_ENCRYPTION_KEY`. Sprache `de`, Zeitzone `Europe/Berlin`. Logging ohne Nachrichteninhalte und Keys. `.env.example` anlegen.
- **M1-03** PostgreSQL mit pgvector: Migration `CREATE EXTENSION IF NOT EXISTS vector`. Für die Entwicklung kommt der Dienst `db` (Image `pgvector/pgvector`) in `compose.yaml`.
- **M1-04** Login, Logout und Passwort ändern mit den Django-Auth-Views und deutschen Templates unter `/konto/`. Login-Drosselung mit django-axes.
- **M1-05** Basislayout: Kopfzeile, Seitenleiste, leere Chatseite. Vanilla-JS und CSS lokal, Auslieferung über WhiteNoise.
- **M1-06** Makefile mit `install`, `db-create`, `migrate`, `user`, `dev`, `run`, `static`, `test`, `lint` und `deb`. `make install` erzeugt `.env` mit generiertem `SECRET_KEY` und `FIELD_ENCRYPTION_KEY`.
- **M1-07** `deploy/gunicorn.conf.py` mit `gthread` 2 × 8 und Timeout 300. Endpunkt `/healthz/`.
- **M1-08** Paketierung angleichen:
  - Unit startet `multigpt.wsgi:application`.
  - Env-Datei `/etc/multi-gpt/.env`, vom postinst einmalig mit generiertem `SECRET_KEY` und `FIELD_ENCRYPTION_KEY` erzeugt. `debian/multi-gpt.default` entfällt.
  - `collectstatic` läuft im Paketbau.
  - Wrapper `/usr/bin/mgpt-ctl`.
  - Docker-Healthcheck zeigt auf `/healthz/`.
- **M1-09** Testgerüst (pytest-django, Test-DB auf PostgreSQL) und ruff-Konfiguration.

*Abnahme (Plan):* `make install migrate user run`, der Login im Browser funktioniert. *Zusätzlich:* `make deb` baut, das Paket installiert sich auf einem frischen Debian 13, die Unit startet, `/healthz/` liefert 200.

### M2 – Datenmodell, Admin, Rollen
*Abhängig von: M1, Frage 5b (Mandanten), Abschnitt 2 dieses Dokuments.*

- **M2-01** Modelle aus Plan 6 einschließlich der Ergänzungen aus Abschnitt 2. `Chunk` kommt erst in M7.
- **M2-02** Verschlüsseltes Feld mit Fernet für API-Keys und MCP-Zugangsdaten. Im Admin erscheinen nur die letzten 4 Zeichen.
- **M2-03** Admin-Masken für `Provider`, `AIModel`, `Rolle`, `McpServer` sowie die Zusatzfelder in den bestehenden Masken für Konten und Gruppen.
- **M2-04** Datenmigration mit den vier Startrollen und der Gruppe "Familie". Neue Konten kommen automatisch in "Familie".
- **M2-05** Zentrale Prüfung `can(user, action, obj=None)`, dazu Decorator und Mixin für die Views. Aktionen als Aufzählung festlegen.
- **M2-06** `make user` als Management-Kommando mit Rollenwahl.

*Abnahme (Plan):* Anbieter und Modell lassen sich anlegen, der Key ist in der DB nicht lesbar, die vier Startrollen existieren, ein Gast erreicht keine Verwaltungsseite. *Tests:* Verschlüsselungs-Roundtrip, falscher Schlüssel schlägt sauber fehl, `can()` je Rolle und Aktion.

### M3 – Erster Adapter und Streaming
*Abhängig von: M2, Frage 3 (mindestens ein Key für die Abnahme).*

- **M3-01** `providers/base.py`: `ProviderAdapter`, Event-Typen, `is_online()`, Fehlertypen.
- **M3-02** `openai_compat` mit `httpx` und Streaming. Vorher die aktuelle API-Dokumentation lesen.
- **M3-03** SSE-Endpunkt mit `StreamingHttpResponse`. Weil `EventSource` nur GET kann, sendet der Browser per `fetch` als POST und liest den Stream aus. Prüfung mit `can()` vor dem Anbieteraufruf.
- **M3-04** Abbrechen: Bricht der Client die Verbindung ab, stoppt der Server den Anbieterstream. Der bisherige Text bleibt mit `status=aborted` gespeichert.
- **M3-05** Chatansicht: Nachrichten senden, Modellauswahl pro Nachricht, Antwort neu erzeugen. Der Verlauf bleibt nach Neuladen erhalten.
- **M3-06** Tests mit gemocktem HTTP (z. B. `respx`): Reihenfolge der Events, Fehlerfall, Abbruch. Außerdem: Nutzer A kann Chats von Nutzer B weder lesen noch ändern.

### M4 – Weitere Adapter und LM Studio
*Abhängig von: M3, Frage 1a (LM-Studio-Rechner).*

- **M4-01** Adapter `anthropic` (Messages-API) und `google` (Gemini). Vorher jeweils die aktuelle Dokumentation lesen.
- **M4-02** `make sync-models` als Management-Kommando.
- **M4-03** `GET /api/providers/status/` mit 15-s-Cache. **Achtung:** Djangos Standard-Cache (`LocMemCache`) gilt nur je Prozess. Bei 2 Gunicorn-Workern daher den Datenbank-Cache verwenden, damit der Cache wirklich prozessübergreifend greift.
- **M4-04** Statusanzeige im Frontend: Polling alle 30 s nur bei sichtbarem Tab, Hinweis beim Wechsel zwischen online und offline, lokale Modelle bei offline ausgegraut.
- **M4-05** Lokale Modelle kosten 0 €. Fällt LM Studio mitten im Stream aus, wird die Nachricht als abgebrochen markiert.

### M4a – MCP
*Abhängig von: M4, Frage 4d.*

- **M4a-01** **Brücke sync ↔ async:** Das MCP-SDK ist asyncio-basiert, Django läuft synchron in `gthread`-Threads. Festgelegtes Muster: ein eigener Thread je Gunicorn-Prozess.
  - **Loop-Thread:** Jeder Gunicorn-Prozess hat genau einen Daemon-Thread mit einem dauerhaften asyncio-Event-Loop. Alle MCP-Sitzungen leben ausschließlich in diesem Loop.
  - **Aufruf aus den Views:** Request-Threads übergeben Coroutinen mit `asyncio.run_coroutine_threadsafe()` und warten mit `future.result(timeout=…)`. Bei Timeout wird das Future abgebrochen.
  - **Mutexe:**
    - Ein `threading.Lock` schützt das verzögerte Starten des Loop-Threads (Double-Checked Locking). Der Thread startet erst beim ersten Aufruf nach dem Fork, weil Threads einen Fork nicht überleben. Gestartet wird nicht in `wsgi.py`, sondern im ersten Aufruf oder im Gunicorn-Hook `post_fork`.
    - Ein `asyncio.Lock` je MCP-Server im Loop schützt Verbindungsaufbau und Neuverbindung. Mehrere Threads, die gleichzeitig denselben Server brauchen, starten den `stdio`-Prozess dann nur einmal.
    - Falls ein Server keine parallelen Anfragen auf einer Sitzung verträgt, serialisiert dasselbe Lock auch die `call_tool`-Aufrufe.
  - **Aufräumen:** Beim Beenden des Workers (Gunicorn-Hook `worker_exit`) schließt der Loop alle Sitzungen und beendet die `stdio`-Kindprozesse.
  - Vorher die aktuelle MCP-Spezifikation und die SDK-Dokumentation lesen und prüfen, ob eine Sitzung parallele Anfragen zulässt.
- **M4a-02** Werkzeugformat in allen drei Adaptern, hin und zurück.
- **M4a-03** MCP-Client für die Transporte `stdio` und Streamable HTTP. Im Admin: Verbindungstest und Werkzeugliste.
- **M4a-04** Werkzeugschleife mit höchstens 10 Runden und Timeout je Aufruf. `can()` prüft vor jedem Aufruf. Jeder Aufruf wird als `ToolCall` protokolliert.
- **M4a-05** Rückfrage: Der Stream endet mit dem Ereignis "Bestätigung nötig". Nach der Bestätigung setzt eine neue Anfrage die Schleife fort. Unbekannte Werkzeuge gelten als rückfragepflichtig.
- **M4a-06** Anzeige im Chat als aufklappbare Zeile. Werkzeugergebnisse mit Dateien werden zu `Attachment`.
- **M4a-07** Tests mit einem MCP-Testserver im Prozess: Rundenlimit, Timeout, keine Ausführung ohne Bestätigung, Rechte je Rolle.

### M5 – Komfort
*Abhängig von: M3. Kann parallel zu M4/M4a laufen.*

- **M5-01** Markdown mit Code-Hervorhebung und Kopierknopf. Bibliotheken lokal eingebunden (z. B. marked, DOMPurify, highlight.js), die Ausgabe wird vor dem Einfügen bereinigt.
- **M5-02** Automatischer Titel, Umbenennen, Archivieren, Löschen, Suche im Titel.
- **M5-03** System-Prompt pro Chat. Der feste Prompt der Rolle wird serverseitig vorangestellt. Test: Der Prompt landet in der Anfrage.
- **M5-04** Export eines Chats als Markdown.

### M6 – Vergleich, Verbrauch, Budgets, Familie
*Abhängig von: M3, M2. Frage 5/5a.*

- **M6-01** Vergleichsmodus mit 2–3 parallelen Streams. Jeder Stream belegt einen Gunicorn-Thread: bei 2 × 8 Threads reicht das für etwa 5 gleichzeitige Vergleiche.
- **M6-02** Verbrauchsübersicht je Nutzer, Modell und Monat auf Basis von `Message.cost`.
- **M6-03** Budgets: Hinweis bei 80 %, bei 100 % sind kostenpflichtige Modelle gesperrt, lokale bleiben nutzbar. Die Prüfung läuft vor jedem Anbieteraufruf.
- **M6-04** Seite "Familie": Konten anlegen und sperren, Rolle zuweisen, Passwort zurücksetzen, Gruppen, Verbrauch.
- **M6-05** Einsicht in Jugendlichen-Chats als Option je Konto (`allow_supervision`, standardmäßig aus). Das Mitglied sieht in der Oberfläche, dass die Option aktiv ist.

### M7 – RAG
*Abhängig von: M2, M4a (für die Suche als Werkzeug), Fragen 4b, 4c.*

- **M7-01** `Collection`, `Document`, Freigaben an Gruppen über `Share`. Upload mit Prüfung von Dateityp und Größe, Dateinamen werden nicht übernommen.
- **M7-02** Job-Tabelle und Kommando `make worker`. Der Worker holt Jobs mit `SELECT … FOR UPDATE SKIP LOCKED`, wiederholt fehlgeschlagene Jobs mit Obergrenze. Dazu die Unit `multi-gpt-worker.service`.
- **M7-03** Textextraktion für PDF, DOCX, TXT und MD, Zerteilung in ca. 800 Tokens mit 100 Überlappung und Seitenzahl, Embeddings über OpenAI. **OCR für gescannte PDFs** mit Tesseract und deutschem Sprachpaket: Seiten ohne Textebene werden erkannt und per OCR gelesen. Das Paket bekommt dafür `tesseract-ocr` und `tesseract-ocr-deu` als Abhängigkeit.
- **M7-04** Migration für `Chunk` mit fester Vektordimension (aus dem gewählten OpenAI-Embedding-Modell) und HNSW-Index (Kosinus). `make reindex`.
- **M7-05** Abfrage: Top 6 mit Zugriffsfilter **in der SQL-Abfrage selbst**, optional zusätzlich Volltextsuche. Quellen mit Seitenangabe unter der Antwort.
- **M7-06** RAG-Suche zusätzlich als Werkzeug für werkzeugfähige Modelle.
- **M7-07** Tests: Zerteilung, Trefferqualität, Zugriffsgrenzen (fremde private Sammlung ist nie im Ergebnis), Entzug einer Freigabe wirkt sofort, Worker-Wiederholung.

### M8 – Websuche
*Abhängig von: M3, M4a (als Werkzeug), Frage 4a.*

- **M8-01** Schnittstelle `search(query)` mit zwei Umsetzungen, SearXNG und eine Such-API. Welche aktiv ist, legt der Verwalter in den Einstellungen fest.
- **M8-02** Seitenabruf mit SSRF-Schutz: DNS auflösen, private und lokale Adressbereiche sperren, Weiterleitungen erneut prüfen. Dazu Timeouts und Größenlimit.
- **M8-03** Inhalte auf Text reduzieren und klar als Quellmaterial markiert mit nummerierten Quellen an das Modell geben.
- **M8-04** Quellenanzeige unter der Antwort. Websuche zusätzlich als Werkzeug.
- **M8-05** Tests: Such-Backend gemockt, Intranet-Adressen und Weiterleitungen auf solche werden abgelehnt.

### M9 – Bilder
*Abhängig von: M4a, Frage 4e.*

- **M9-01** `generate_image` für den gewählten Anbieter. Modus "Bild" mit Formatwahl, Ergebnis als `Attachment`.
- **M9-02** `edit_image` über OpenAI: Bereich auf einer Zeichenfläche markieren, Maske mitsenden, außerdem Varianten. Vorher in der aktuellen API-Dokumentation prüfen, welches Modell das kann.
- **M9-03** MCP-Server `mcp_imagetools` (Pillow, `stdio`): Zuschneiden, Skalieren, Drehen, Umwandeln, Füllen, Text, Collage. Er arbeitet nur im Arbeitsordner des jeweiligen Nutzers.
- **M9-04** Geschützte Auslieferung der Medien nur nach Besitzprüfung, mit nginx über `X-Accel-Redirect`, ohne nginx über `FileResponse`.
- **M9-05** Tests: jede Pillow-Funktion, kein Zugriff außerhalb des Arbeitsordners, das Original bleibt erhalten.

### M10 – Sprache
*Abhängig von: TLS (aus M11 vorgezogen), Frage 2, Frage 4b.*

- **M10-01** Aufnahme mit `MediaRecorder`, Spracherkennung über ein `stt`-Modell, der Text landet zum Korrigieren im Eingabefeld.
- **M10-02** Vorlesen über ein `tts`-Modell, die Audiodatei wird als `Attachment` gespeichert. Option "automatisch vorlesen" je Nutzer.
- **M10-03** Konfigurierbare Grenzen für Aufnahmelänge und Dateigröße.

### M11 – Betrieb
- **M11-01** `deploy/nginx.conf.example` als Baustein für den bestehenden nginx: `server`-/`location`-Block mit TLS, `proxy_buffering off` für den Stream-Endpunkt und `X-Accel-Redirect` für die Medien. Dazu `SECURE_COOKIES=True` und `AXES_PROXY_COUNT=1`. **Vor M10 umsetzen.**
- **M11-02** `make backup`: `pg_dump`, Medienordner und `/etc/multi-gpt/.env` als datiertes Archiv.
- **M11-03** README mit Installationsanleitung (Paket, PostgreSQL, `/etc/multi-gpt/.env`, nginx).
- **M11-04** Prüfen, ob das Paket sauber aktualisiert und entfernt wird: `apt install` über eine ältere Version, `apt remove` und `apt purge`.

---

## 4. Reihenfolge und kritischer Pfad

```
M1 ─> M2 ─> M3 ─┬─> M4 ─> M4a ─┬─> M7 (RAG)
                │              ├─> M8 (Websuche)
                │              └─> M9 (Bilder)
                ├─> M5 (Komfort)            ┐
                └─> M6 (Verbrauch/Budgets)  ├─ parallel möglich
M11-01 (TLS/nginx) ─────────────> M10 (Sprache)
```

Der kritische Pfad ist **M1 → M2 → M3 → M4 → M4a**. Alles mit Werkzeugen (M7-06, M8-04, M9-03) setzt die MCP-Schleife voraus. M5 und M6 können vorgezogen werden, falls die Fragen zu M4/M4a noch offen sind.

---

## 5. Risiken

| Risiko | Auswirkung | Gegenmaßnahme |
|---|---|---|
| MCP-SDK ist async, die App läuft synchron | Hängende Threads, Verbindungslecks bei `stdio`-Servern, doppelt gestartete Server | Ein Loop-Thread je Prozess, Mutexe für Start und Verbindungsaufbau, Timeouts auf jedem `future.result()`, Aufräumen in `worker_exit` (M4a-01). Tests mit parallelen Aufrufen aus mehreren Threads |
| Inpainting nur bei einem Anbieter (OpenAI) | Fällt OpenAI aus oder ändert die API, fehlt Inpainting | Fähigkeit je Modell im Admin markieren, Adapter gekapselt. Ein zweiter Anbieter ist später nachrüstbar |
| Gunicorn-Threads durch lange Streams belegt | Neue Anfragen warten | Thread-Zahl über Env konfigurierbar, Auslastung in M6 beobachten |
| Anbieter-APIs ändern sich | Adapter brechen | Laut Plan vor jedem Adapter die aktuelle Dokumentation lesen, Tests mit aufgezeichneten Antworten |
| Prompt-Injection über Webinhalte, Dokumente oder Werkzeugergebnisse | Unerwünschte Werkzeugaufrufe | Rückfragepflicht wird serverseitig erzwungen und hängt nie von Modellinhalten ab (Test in M4a-07) |

---

## 6. Offene Fragen nach blockiertem Meilenstein

| Frage (Plan 13) | Blockiert | Bemerkung |
|---|---|---|
| 1 – Welches NAS? | – | **Geklärt:** Debian/Ubuntu mit apt, das `.deb` ist der Betriebsweg |
| 4 – PostgreSQL + pgvector vorhanden? | – | **Geklärt:** vorhanden, wird genutzt |
| 5b – Eine Familie oder mehrere Haushalte? | – | **Geklärt:** eine Familie, keine Mandantentrennung |
| 5 – Konten, Alter der Kinder | – | **Geklärt:** 2 Erwachsene und Jugendliche, die vier Startrollen passen |
| 3 – Anbieter zum Start | – | **Geklärt:** OpenRouter, OpenAI, Anthropic, Gemini |
| 1a – LM-Studio-Rechner | M4 | |
| 4d – MCP-Server zum Start | M4a (Abnahme) | Ein Testserver genügt |
| 5a – Einsicht in Jugendlichen-Chats | – | **Geklärt:** Option je Konto, standardmäßig aus |
| 4b – Anbieter für Embedding, STT, TTS, Bild | – | **Geklärt:** OpenAI für alles. Das Modell wird in M7 festgelegt und bestimmt die Vektordimension |
| 4c – Sprache der Dokumente, OCR? | – | **Geklärt:** deutsch, mit Scans, OCR mit Tesseract in v1 |
| 4a – SearXNG oder Such-API | – | **Geklärt:** beides, umschaltbar |
| 4e – Inpainting-Anbieter | – | **Geklärt:** OpenAI |
| 2 – Reverse Proxy, Hostname, TLS | M10, M11 | **Teilweise geklärt:** nginx ist vorhanden. Offen: Hostname und Zertifikat |

---

## 7. Stand

- **M1 umgesetzt** (2026-10-09). Nachgewiesen:
  - `make test` (15 Tests gegen PostgreSQL 18 mit pgvector), `ruff`, `manage.py check` und `makemigrations --check` sind grün. Die Entwicklungs-DB ist mit `accounts.User` neu aufgesetzt und migriert.
  - `make run` startet gunicorn (`gthread`), `/healthz/` liefert 200, `/` leitet auf den Login um.
  - `make deb` baut `multi-gpt_0.1.0_amd64.deb`:
    - Abhängigkeiten `python3.13, python3 (>= 3.12), adduser`.
    - `collectstatic` im Paket.
    - Im postinst läuft die Migration einmal und vor dem Start bzw. Neustart des Dienstes.
  - Repository https://github.com/Beerlesklopfer/multi-gpt (öffentlich, AGPL-3.0-or-later). Website über GitHub Pages: https://beerlesklopfer.github.io/multi-gpt/
- **Offen für die Abnahme von M1:**
  - Installation, automatische Migration und `purge` des Pakets auf Debian 13.
  - Login im Browser.
  - Docker-Build (ungetestet, auf dem Entwicklungsrechner gibt es kein Docker).
- **M2 umgesetzt** (2026-10-09, Commit `3077e51`, 210 Tests grün, Paket baut):
  - Alle Modelle aus Plan 6 außer `Chunk`.
  - `EncryptedTextField` in `multigpt/core/` (Fernet).
  - Admin mit maskierten Keys. Private Inhalte erscheinen dort nur als Metadaten.
  - Vier Startrollen, Standardgruppe „Familie“.
  - `can()` mit `require_can` und `CanRequiredMixin`.
  - `FamilyAdminSite` nur für Verwalter, `make user` mit Rollenwahl.
  - Nachzuziehen in späteren Meilensteinen:
    - M4a: `ToolCall` braucht die Aufruf-ID des Anbieters, `McpServer` die Liste der vom Verwalter eingestuften Werkzeuge.
    - M7: `Job` braucht einen Fehlertext und einen späteren Startzeitpunkt (`run_after`).
    - M6: Budget in `budget_allows()`, Einsicht über `allow_supervision`.
  - Betrieb: Jugendliche und Gäste haben anfangs keine Modelle. Der Verwalter gibt sie im Admin unter Rolle → Erlaubte Modelle frei.
- **M3–M5 in Arbeit** (freigegeben am 2026-10-09). Umsetzung im Verbund mehrerer Agenten, die sich über einen FIFO-Bus abstimmen.
- Geklärt sind die Fragen 1, 3, 4, 4a–4c, 4e, 5, 5a und 5b, Frage 2 teilweise. Offen sind noch 1a, 2 (Hostname und Zertifikat) und 4d. Die Datenmodell-Lücken aus Abschnitt 2 sind entschieden.
