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
| Netzwerk | **Entscheidung vom 2026-10-09:** Im Debian-Paket lauscht gunicorn ausschließlich auf `127.0.0.1:8000`. **nginx ist Pflicht** und hängt als `Depends` am Paket. Das Paket bringt die Site `/etc/nginx/sites-available/multi-gpt` mit (conffile, im postinst aktiviert, vorher `nginx -t`). Sie enthält TLS, die Weiterleitung von HTTP auf HTTPS, `proxy_buffering off` für SSE, `client_max_body_size` passend zum Upload-Limit, statische Dateien direkt aus dem Paket und geschützte Medien per `X-Accel-Redirect`. debconf fragt Hostname(n) und Zertifikat/Schlüssel ab. Ohne eigenes Zertifikat gilt vorerst das snakeoil-Zertifikat (`ssl-cert`), mit Warnung. `ALLOWED_HOSTS` und `CSRF_TRUSTED_ORIGINS` werden aus dem Hostnamen abgeleitet. `SECURE_COOKIES=True` und `AXES_PROXY_COUNT=1` werden gesetzt. Docker und die Entwicklung sind davon nicht betroffen. |
| Datenbank | Das preinst legt bei lokal laufendem PostgreSQL die Rolle `multi-gpt`, die Datenbank `multi-gpt` und die Extension `vector` an. Das geht idempotent und nur, wenn `DATABASE_URL` auf die lokale Standard-Datenbank zeigt. Die Anmeldung läuft per Peer-Auth als Systemnutzer `multi-gpt`. Das preinst bricht nie ab, `MULTI_GPT_SKIP_DB_SETUP=1` überspringt es. |
| Verwaltung | `/usr/bin/mgpt-ctl`: Wrapper um `manage.py`, läuft als `multi-gpt` mit `/etc/multi-gpt/.env`. Das postinst migriert bei Installation und Upgrade automatisch, bevor der Dienst (neu) startet, sofern die Datenbank erreichbar ist. Sonst gibt es einen Hinweis auf `sudo mgpt-ctl migrate`, die Installation scheitert daran nicht. `MULTI_GPT_SKIP_MIGRATE=1` überspringt die Migration. |
| Statische Dateien | `collectstatic` beim Paketbau, `STATIC_ROOT=/usr/share/python/multi-gpt/static`, im Paket direkt von nginx ausgeliefert, in Entwicklung und Docker über WhiteNoise. |
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

1. **Fähigkeiten eines Modells:** `AIModel.capability` ist die Hauptart: `chat`, `image` (Bilderzeugung, M9-01), `embedding` (M7), `stt` und `tts` (M10), `music` (M11). Nur `chat` erscheint in Chat- und Vergleichsauswahl; OCR und Abbildungen (RAG) nehmen nur Chat-Modelle. Dazu die Häkchen `supports_tools` („Werkzeuge“, 7, 8g), `supports_vision` („Bilder verstehen“) und `can_edit_images` („Bilder bearbeiten“, 8e, M9-02) sowie `mcp_access` (siehe M4a-08). Ein eigenes Häkchen für Audio im Chat gibt es nicht: Spracheingabe läuft über ein `stt`-Modell (M10-01). Im Admin pflegt der Verwalter das als Matrix (Inline beim Anbieter und Liste der KI-Modelle).
   - **Erkennung** (`chat/capabilities.py` ohne Django, `chat/detect.py`): LM Studio meldet unter `GET /api/v0/models` (ab 0.3.16) je Modell `type` (`llm`, `vlm`, `embeddings`) und `capabilities` (`tool_use`). Abgerufen wird das nur für lokale Anbieter und nur, wenn neue Modelle anzulegen sind (höchstens einmal je Statusprüfung, also alle 15 s); Fehler oder andere Server (Ollama, vLLM) fallen auf die Heuristik aus der Modell-ID zurück (`guess_tools`, `guess_vision`, `guess_capability`, gepflegte Liste mit Quellen im Code, im Zweifel nein). Neue Modelle bekommen die Werte überall, wo sie entstehen (Statusprüfung, „Modelle auswählen“, `sync_models`, Combobox). Bestehende Modelle ändert nur die Admin-Aktion „Fähigkeiten automatisch erkennen (Werkzeuge, Bilder)“ nach Vorschau und Bestätigung bzw. `manage.py guess_capabilities --apply` (ohne `--apply` Vorschau); keine Datenmigration.
2. **Kosten als Momentaufnahme:** `Message.cost` und `Attachment.cost` halten die Kosten zum Zeitpunkt der Antwort fest. Spätere Preisänderungen verfälschen Verbrauch und Budget dadurch nicht.
3. **Freigaben:** Die Tabelle `Share` gilt für Chats und Sammlungen gleichermaßen. Ziel ist genau eines von `conversation` und `collection`, Empfänger genau eines von `group` und `user` (Sammlungen nur an Gruppen). Rechte nach RWUD: Lesen immer, dazu `can_write`, `can_update` und `can_delete` (U und D nur bei Chats). `left_by` hält fest, wer einen an seine Gruppe geteilten Chat aus seiner Liste entfernt hat. Details unter M5-06.
4. **Gruppen:** `accounts.UserGroup` erweitert Djangos `auth.Group` per Tabellenvererbung. Die Mitgliedschaft läuft über `User.groups`, Zusatzfelder liegen in `UserGroup`. Im Admin ersetzt `UserGroup` die Django-Gruppen.
5. **Konten:** `accounts.User` erweitert Djangos `AbstractUser` (`AUTH_USER_MODEL = "accounts.User"`). Ein Modell `Profil` gibt es nicht. In M2 kommen die Felder `role`, `display_name`, `monthly_budget_override`, `allow_supervision` (5a) und `auto_read_aloud` (8c) dazu. Gesperrt wird über `is_active`.
6. **Status von Werkzeugaufrufen:** `ToolCall.status` hat die Werte `awaiting_confirmation`, `rejected`, `running`, `ok`, `error` und `timeout`.
7. **Nachrichtenstatus:** `Message.status` hat die Werte `complete`, `aborted` und `error`, dazu `error` als Fehlertext.
8. **Bildherkunft:** `Attachment.source_image` verweist auf das Ausgangsbild (Fremdschlüssel auf `Attachment` selbst).
9. **Vektordimension:** `Chunk.embedding` braucht für den HNSW-Index eine feste Dimension. Sie ergibt sich aus dem OpenAI-Embedding-Modell (Frage 4b), das in M7 festgelegt wird. Die Migration für `Chunk` entsteht deshalb erst in M7.
10. **Scratchpad (M13, geplant am 2026-10-10):** neue App `multigpt.scratchpad` mit `Scratchpad`, `ScratchSource`, `ScratchItem`, `OutlineSection`, `ScratchDraft` und `DraftSection` (Plan 6). Dazu kommen Änderungen an bestehenden Modellen:
    - `Preset`: `system_prompt` → `text`, dazu `kind`, `project`, `web_search`, `document_search`, `send_now`, `sort_order`, `active`
    - `AIModel.publisher`
    - Rollenrecht `can_scratchpad`
    - Kontoeinstellung `context_menu`
    
    `citations.Reference` bekommt die Arten `ai`, `personal` und `dataset` sowie die Felder `prompt`, `model`, `medium` und `note`.
11. **Projekte (M5-07, 2026-10-10):** Modell `chat.Project` (owner, name, description, instructions, default_model, M2M collections, color, pinned, archived, created, updated) und `Conversation.project` (nullable, `SET_NULL`), Migration `chat.0023_projects`. Ein Chat gehört höchstens einem Projekt, immer einem des Chat-Besitzers. Teilen eines Projekts ist vorbereitet, aber noch nicht umgesetzt (siehe M5-07). Das Scratchpad (M13) dockt mit `Scratchpad.project` an.

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
- **M4-03** `GET /api/providers/status/` mit 15-s-Cache. Der Status liegt in der DB am `Provider` (`online`, `last_checked`, `reported_models`) und gilt damit für beide gunicorn-Worker. Ein bedingtes UPDATE auf `last_checked` sorgt dafür, dass nur ein Aufruf prüft. Die Netzprüfung läuft außerhalb jeder Transaktion.
- **M4-04** Statusanzeige im Frontend: Polling alle 30 s nur bei sichtbarem Tab, Hinweis beim Wechsel zwischen online und offline, lokale Modelle bei offline ausgegraut.
- **M4-05** Lokale Modelle kosten 0 €. Ein Senden an ein lokales Modell, dessen Anbieter offline ist, wird vor dem Stream mit 503 abgewiesen. Reißt die Verbindung nach schon empfangenem Text ab (wiederholbarer Fehler, bei allen Anbietern), wird die Antwort mit Teiltext als `aborted` gespeichert. Von LM Studio gemeldete Modelle werden automatisch angelegt, Modelle mit `embed` in der ID als `embedding`.

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
- **M4a-08** **MCP-Freigabe je Modell** (`AIModel.mcp_access`: `none`, `all`, `selected` mit M2M `mcp_servers`). Festlegung: Neue Modelle bekommen `none`, nur Modelle von OpenAI, Anthropic und Google `all`. Begründung: Ein Modell, das Webseiten oder Dokumente liest, lässt sich per Prompt-Injection zu Werkzeugaufrufen verleiten; unbekannte lokale Modelle und Sammelanbieter (OpenRouter) sollen Werkzeuge, die etwas verändern, erst nach Freigabe erreichen. Bestehende Modelle erhielten in der Migration `all` (keine Verhaltensänderung). Es gilt die Schnittmenge mit den Rollenrechten. Durchgesetzt in `tooling` beim Anbieten (`available_servers`, `collect_tools`) und vor jedem Aufruf (`resolve_server`, frisch aus der DB, auch nach Rückfrage und Bestätigung); Ablehnung mit Meldung an das Modell, Log nur mit IDs. Eingebaute Werkzeuge (Websuche, Dokumentsuche) hängen nur an `supports_tools`. `/api/models` liefert `mcp_access` und `mcp_server_ids` (`null` = alle der Rolle); der Chat zeigt nur die erlaubten Server.
- **M4a-09** **Online-Status und Werkzeugliste je MCP-Server** (`chat/mcp/status.py`, Migration chat `0028_mcp_status`). Neue Felder am `McpServer`: `online`, `last_checked`, `last_online`, `last_error` (deutsche Ursache ohne Zugangsdaten und ohne Rohtext des Servers), `reported_tools` (Name, Beschreibung auf 300 Zeichen gekürzt, nur Parameternamen und Pflichtparameter, annotations `readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint`) und `tools_checked` (Zeitpunkt der letzten Änderung der Einstufung durch den Verwalter).
  - **Prüfen** = verbinden, `initialize`, `tools/list` über den Loop-Thread und die Sitzungen aus `client.py` (gleiches Server-Lock wie im Betrieb, ein `stdio`-Prozess je Server und Prozess), Zeitlimit `min(timeout_seconds, 10 s)`. Mehrere Server werden parallel geprüft. Deaktivierte Server werden nie geprüft, also auch kein Prozessstart.
  - **Ursachen:** Verbindung abgelehnt, Rechnername unbekannt (DNS), Rechner nicht erreichbar, TLS-Fehler, Zeitüberschreitung, HTTP 401/403 „Zugang abgelehnt – Token prüfen“, 404 „Adresse nicht gefunden – Pfad prüfen (…/mcp bzw. …/http)“, 5xx, stdio „Programm nicht gefunden“ bzw. „Programm beendet sich sofort“, Protokollfehler (nur JSON-RPC-Code), „Falsch eingerichtet“. Den HTTP-Status meldet das SDK nur als allgemeinen JSON-RPC-Fehler; `client.py` merkt sich deshalb per httpx-Response-Hook den letzten Fehlerstatus je Server (`client.http_errors`).
  - **Wann:** nach dem Speichern im Admin (`transaction.on_commit`), über „Jetzt prüfen“ (ersetzt den Link „Werkzeugliste abrufen“ bzw. `?tools=1`) und die Aktion „Ausgewählte jetzt prüfen“, nach dem JSON-Import für neu angelegte, aktive Server (aktualisierte werden zur Neuprüfung vorgemerkt), periodisch im Worker (`run_worker`, einmal je Minute fällige Server: online alle 5 Minuten, offline jede Minute, ungeprüfte sofort). Der Worker schließt Sitzungen, die er für die Prüfung geöffnet hat, gleich wieder. Seitenaufrufe prüfen nie. Sperre gegen Doppelprüfung wie bei den Anbietern: bedingtes UPDATE von `last_checked` als Anspruch.
  - **Chat:** `/api/mcp-servers/` liefert `online` und `error` (gespeicherter Stand); die MCP-Auswahl zeigt offline-Server ausgegraut und nicht wählbar, Ursache im Tooltip. `tooling.collect_tools` bietet zuletzt offline geprüfte Server nicht an (kein Verbindungsversuch) und vermerkt einen im Betrieb gescheiterten Abruf als offline; der Stream meldet das als `status` (Stufe warning) statt als Fehler in der Antwort.
  - **Statusleiste:** MCP-Server nur für Verwalter (`is_staff` mit Leserecht auf MCP-Server) und nur, wenn sie offline sind, mit Link in den Admin. Begründung: Für Nutzer zählt nur, ob ihre Werkzeuge verfügbar sind – das zeigt die MCP-Auswahl im Chat. Ein dauerhafter grüner Punkt je Server wäre Rauschen; ein offline-Server ist dagegen eine Aufgabe für den Verwalter. Die Anzeige wird beim Seitenaufbau gerendert, ohne eigenes Polling (der Status ändert sich höchstens minütlich).
  - **Admin:** Tabelle der gemeldeten Werkzeuge auf der Änderungsseite (Name, Beschreibung, Parameter, Hinweise des Servers, Einstufung als Auswahl „nicht eingestuft“/„ohne Rückfrage“/„mit Rückfrage“). Für gemeldete Werkzeuge gilt die Tabelle, die JSON-Felder bleiben als Rückfall unter „Erweitert“. Vorschlag aus den annotations (`destructiveHint` → mit Rückfrage, sonst `readOnlyHint` → ohne Rückfrage) nur als Vorbelegung bei nicht eingestuften Werkzeugen; wirksam erst beim Speichern. Neue Werkzeuge sind hervorgehoben, eingestufte, aber nicht mehr gemeldete werden darunter genannt. Liste: Spalten Status (Ursache im Tooltip), zuletzt geprüft, Werkzeuge, davon nicht eingestuft. Alle Texte vom Server werden escaped und gekürzt angezeigt. Logs nur mit Server-ID und Kurzursache.
- **M4a-10** **Berechnungen: eingebautes Werkzeug `run_python`** (`chat/tools_python.py`, `chat/sandbox.py`, `chat/svg_clean.py`, Migrationen chat `0029_python_tool` und accounts `0008_role_can_compute`). Modelle mit `supports_tools` rechnen mit numpy, sympy, mpmath und zeichnen mit matplotlib, statt zu schätzen. **Festlegung Sandbox:** Der Code gilt als nicht vertrauenswürdig (Prompt-Injection). Er läuft nur unter bubblewrap (Debian-Paket `bubblewrap`, `Depends`). Ohne funktionierende Sandbox wird das Werkzeug nicht angeboten; einen Rückfall im Server-Prozess gibt es nicht, auch nicht unter DEBUG.
  - **bwrap:** `--unshare-all` (auch kein Netz), `--die-with-parent`, `--new-session`, `--cap-drop ALL`, `--clearenv` mit festen Variablen (PATH, HOME=/tmp, LANG, `MPLBACKEND=Agg`, `MPLCONFIGDIR=/tmp/matplotlib`, je ein BLAS-Thread). Nur lesend: `/usr` und **einzeln** die Rechenpakete aus dem venv unter `/sandbox/lib` (numpy, sympy, mpmath, matplotlib samt Abhängigkeiten und `*.libs`). Django, MultiGPT, `/etc`, `/home`, `/var/lib/multi-gpt` und DB-Sockets sind nicht sichtbar. `/tmp` (64 MB) und der Arbeitsordner `/work` sind tmpfs mit fester Größe, danach `--remount-ro /`. Python mit `-I -S -B`. **Kein `/proc`:** `ProtectKernelTunables` und `ProtectKernelLogs` der Unit legen gesperrte Overmounts auf /proc, deshalb verweigert der Kernel ein neues procfs im User-Namespace. Die Sandbox ist ohne /proc enger.
  - **seccomp** (`--seccomp`, BPF selbst erzeugt, keine neue Abhängigkeit, x86_64 und aarch64): verboten sind ptrace, process_vm_*, mount und verwandte Aufrufe, unshare/setns, clone mit Namespace-Flags, clone3 (ENOSYS, glibc fällt auf clone zurück), bpf, perf_event_open, userfaultfd, keyctl, io_uring, Kernelmodule, kexec und fremde ABIs (i386, x32). `no_new_privs` setzt bwrap.
  - **Grenzen** (`ChatSettings.python_*`, im Admin, Höchstwerte in `sandbox.LIMIT_RANGES` und zusätzlich im Code begrenzt): CPU 10 s (höchstens 60), Wanduhr 20 s (120) mit SIGKILL auf die Prozessgruppe, Speicher (RLIMIT_AS) 512 MB (4096), Prozesse/Threads 4 (64), Dateigröße 10 MB (100), Ausgabe 64 KB (1024), höchstens 6 Dateien je Lauf und 12 je Antwort. Die Grenzen setzt das Startskript per `resource` vor dem fremden Code, weich gleich hart. RLIMIT_NPROC wirkt erst innerhalb des User-Namespace, sonst zählt der Kernel alle Prozesse des Dienstnutzers mit.
  - **Ein- und Ausgabe:** Code, Startskript und seccomp-Programm gehen über memfd hinein (`--ro-bind-data`, `--seccomp`). Bilder kommen über einen eigenen Pipe-Deskriptor zurück (JSON-Zeilen, base64); der Server liest keine Pfade aus der Sandbox. Der Font-Cache von matplotlib wird einmal je Prozess in einer sauberen Sandbox mit festem Code erzeugt und jedem Lauf per `--file` ins tmpfs gelegt (spart etwa 5 s je Diagramm).
  - **Ausführung** blockierend im Thread der Antwort, wie MCP-Aufrufe. Die Wanduhr liegt weit unter dem gunicorn-Timeout. Über den Worker gäbe es nur Wartezeit und Polling, aber keinen Gewinn an Sicherheit, denn die Grenze ist bwrap. Je Prozess laufen höchstens 2 Läufe gleichzeitig (`MAX_PARALLEL`).
  - **Dateien:** PNG, JPEG, WebP und SVG aus `/work` werden Anhänge der Antwort (`owner` leer, ohne `tool_call`, wie erzeugte Bilder). Raster werden über `attachments.process_image` neu kodiert, ohne Metadaten und mit Vorschau. SVG wird serverseitig bereinigt (`svg_clean`, gleiche Strenge wie `svg_preview.js`: Positivliste, keine DTD und Entities, kein script/foreignObject/a, keine `on*`, `href` nur `#…` bzw. `data:image`-Raster, kein externes `url()`/`@import`). Es wird nur als `<img>` gezeigt bzw. als Download über die geschützte Auslieferung (CSP `sandbox`, `nosniff`). Nutzer-Uploads nehmen weiterhin kein SVG an.
  - **Rechte:** Rollenrecht `can_compute` („Berechnungen ausführen“, `Action.COMPUTE`); Startrollen Verwalter, Erwachsener, Jugendlicher an, Gast aus, neue Rollen aus. Ohne Rückfrage, weil die Sandbox es hält; `ChatSettings.python_confirm` schaltet sie ein. Die MCP-Freigabe je Modell gilt nicht, auch nicht vertrauenswürdige lokale Modelle dürfen rechnen.
  - **Unit:** `RestrictAddressFamilies` um `AF_NETLINK` erweitert, weil bwrap das Loopback-Gerät im leeren Netz-Namespace per NETLINK_ROUTE einrichtet. Sonst bleibt die Härtung unverändert (kein `RestrictNamespaces=`, `NoNewPrivileges` verträgt sich mit bwrap ohne setuid). Debian 13 erlaubt unprivilegierte User-Namespaces (`kernel.unprivileged_userns_clone=1`, keine AppArmor-Sperre wie bei Ubuntu).
  - **Admin:** Chat-Einstellungen, Abschnitt „Berechnungen“, mit Status „Sandbox verfügbar: ja/nein + Ursache“ und dem Knopf „Sandbox testen“ (`print(1+1)`, Pakete, Netz, Server-Dateien, Schreiben, Umgebung, Server-Code, seccomp).
  - **Chat:** `tool_code.js` zeigt bei run_python den Code mit Hervorhebung (highlight.js) und die Ausgabe aufklappbar. chat.js bleibt unverändert.
  - **Logs** nur mit IDs, Dauer, Exit-Status und Abbruchgrund, nie Code oder Ausgabe. Tests: `tests/test_python_tool.py` (echte Sandbox, ohne bwrap übersprungen).

### M5 – Komfort
*Abhängig von: M3. Kann parallel zu M4/M4a laufen.*

- **M5-01** Markdown mit Code-Hervorhebung, Kopierknopf und Formeln. Bibliotheken lokal eingebunden (marked, DOMPurify, highlight.js, KaTeX; `static/chat/vendor/README.md`), die Ausgabe wird vor dem Einfügen bereinigt.
  - Ablauf in `markdown.js`: Formeln schützen (`math.js`) → `marked` → DOMPurify → Platzhalter in Code und Attributen zurück zum Quelltext → Code-Blöcke, Kurzbelege → Formeln mit KaTeX setzen. Gilt überall, wo `markdown.js` rendert: Stream, Vergleichsspalten, Neuladen, Bearbeiten, Neu erzeugen, Versionswechsel.
  - Erkannt außerhalb von Code: Block `\[ … \]`, `$$ … $$`, `\begin{equation|align|gather|multline|…|pmatrix|cases} … \end{…}`; inline `\( … \)` und `$ … $`. Für `$` gilt: kein Leerraum direkt nach dem öffnenden und vor dem schließenden `$`, nach dem schließenden keine Ziffer, keine Leerzeile dazwischen; `\$` ist ein Dollarzeichen. So bleiben „5 $ und 10 $“ und „$5 oder $10“ Text. Formeln enden nie über eine Leerzeile.
  - Platzhalter: zufälliger Kern aus Buchstaben und Ziffern je Seitenaufruf, den `marked` nicht verändert. Was trotzdem in Code landet (z. B. eingerückter Code-Block), wird wieder zum Quelltext.
  - Stream: Eine noch offene Formel am Ende bleibt roher Text (`.math-pending`), bis sie geschlossen ist. Nach dem Stream bleibt ein nie geschlossener Begrenzer als Text stehen. Gesetzte Formeln liegen in einem Zwischenspeicher (400 Einträge) und werden pro Bild nur geklont.
  - Sicherheit: KaTeX setzt erst nach DOMPurify in eigene Elemente (`katex.render`), die Ausgabe stammt also nicht aus dem Modelltext. Optionen `output: "htmlAndMathml"` (MathML für Screenreader), `throwOnError: false` (Fehler rot mit Quelltext), `trust: false` (kein `\href`, `\url`, `\htmlClass`, `\includegraphics`), `maxSize: 20`, `maxExpand: 500`, `strict: "ignore"`, `macros` je Formel neu. Formeln über 20 000 Zeichen bleiben Quelltext. Mit mhchem für `\ce{…}`.
  - Darstellung: KaTeX erbt die Textfarbe (hell und dunkel). Block-Formeln scrollen waagerecht im eigenen Kasten (`.math-display`), die Seite läuft bei 375 px nicht über.
  - Kopierknopf der Nachricht und Markdown-Export geben den Quelltext mit den originalen Formeln aus.
  - CSP: Die Seiten senden keine Content-Security-Policy (weder Django noch die nginx-Vorlage). Käme eine mit `style-src` ohne `'unsafe-inline'`, bräuchte KaTeX dafür `style-src-attr 'unsafe-inline'` (KaTeX setzt Maße als `style`-Attribute, keine `<style>`-Elemente, keine Inline-Skripte).
  - SVG-Vorschau (`svg_preview.js`, Aufruf aus `markdown.js` nach den Code-Blöcken): Code-Blöcke mit Sprache `svg` bzw. `xml`, deren Inhalt mit `<svg` beginnt (optional nach `<?xml …?>`), bekommen in der Kopfzeile „Vorschau“/„Code“ (Knöpfe mit `aria-pressed`), „Als Datei speichern“ (`bild.svg` über Blob-URL) und „Kopieren“ (Code wie bisher). Voreingestellt ist „Vorschau“, sobald der Block geschlossen ist; im Stream bleibt ein offener Block Code (erkannt über `marked.lexer`, nur der letzte Block kann offen sein). Die Wahl „Code“ bleibt beim Neurendern im Stream erhalten. Klick auf die Vorschau öffnet einen Vollbild-`<dialog>` im Aussehen der Anhänge-Lightbox (deren Code ist in `attachments.js` gekapselt).
  - Sicherheit SVG: Dargestellt wird nur als `<img src="data:image/svg+xml,…">`, nie als Inline-SVG im DOM. Im Bildmodus führen Firefox und Chromium keine Skripte aus, werten keine Ereignisse aus und laden keine externen Ressourcen (`<image>`, `<use>`, CSS-`url()`, `@import`, Schriften); nur `data:` wird aufgelöst. Geprüft in Firefox 140 auch mit dem *unbereinigten* SVG als data:- und Blob-URL: kein Skript, kein Abruf (lokaler Lauscher). Chromium war nicht verfügbar; dort gilt dasselbe laut SVG-Integrationsspezifikation (Bildkontext = „secure animated mode“) und Chromium-Verhalten seit Jahren, ein eigener Lauf steht aus. Davor: Wohlgeformtheit per `DOMParser` (`image/svg+xml`, Wurzel `<svg>` im SVG-Namensraum), DOMPurify mit `USE_PROFILES: {svg: true, svgFilters: true}` plus `<use>`, ohne `<script>`, `<foreignObject>`, `<a>` und `on*`; `href`/`xlink:href` nur `#…` (bei `<use>` nur `#…`) oder eingebettete Rasterbilder (`data:image/png|jpeg|gif|webp`); `url()` in `<style>` und Attributen nur `#…`, kein `@import`. Über 512 KB (UTF-8) oder fehlerhaft: Hinweis „Vorschau nicht möglich …“ und Code. Ohne feste Breite (nur `viewBox`) nimmt das Bild die Containerbreite (höchstens 40rem) im Seitenverhältnis der `viewBox`.
  - Darstellung SVG: auf weißem, kariertem Grund (Transparenz sichtbar, auch im Dark Mode), höchstens Containerbreite, Seitenverhältnis bleibt; bei 375 px brechen die Knöpfe der Kopfzeile um. Markdown-Export und Kopierknopf der Nachricht geben den Code aus.
  - CSP für SVG: Käme eine Content-Security-Policy, bräuchte die Vorschau `img-src 'self' data: blob:` (`data:` für das Bild; `blob:` falls die Vorschau künftig Blob-URLs nutzt, der Download-Link selbst fällt nicht unter `img-src`).
- **M5-02** Automatischer Titel, Umbenennen, Archivieren, Löschen, Suche im Titel.
- **M5-03** System-Prompt pro Chat. Der feste Prompt der Rolle wird serverseitig vorangestellt. Test: Der Prompt landet in der Anfrage.
- **M5-04** Export eines Chats als Markdown.
- **M5-05** (Nutzerwunsch, nachgezogen) Eigene Nachrichten bearbeiten und Versionen wie in ChatGPT:
  - `Message.parent` und `Conversation.current_leaf`, ein Chat wird zum Baum.
  - Bearbeiten (`edit_of`) und Neu erzeugen legen Geschwister an. Der Status `superseded` wird nicht mehr vergeben, eine Datenmigration wandelt bestehende Chats in Ketten um.
  - Versionsumschalter „‹ i/n ›“ (`POST /api/conversations/<pk>/branch/`), Stift „Bearbeiten“, Kopierknopf.
  - Verlauf an das Modell, Export und Werkzeugschleife arbeiten nur auf dem angezeigten Pfad. Kosten zählen über alle Zweige.
- **M5-06** (Nutzerwunsch) Chats teilen mit RWUD (`chat/sharing.py`, `api_sharing.py`, `static/chat/sharing.js`, Migration `chat.0021_chat_sharing`, Wiki-Seite `Chats-teilen`):
  - **Modell:** Kein eigenes `ConversationShare`. `Share` ist erweitert (Konto als Empfänger, `can_update`, `can_delete`, `left_by`), damit `can()` die einzige Stelle für Lese- und Änderungsrechte bleibt und Chats wie Sammlungen funktionieren. Constraints: genau ein Ziel, genau ein Empfänger, Sammlungen nur an Gruppen, je Chat höchstens eine Freigabe pro Konto bzw. Gruppe.
  - **Rechte:** R immer: lesen mit allen Versionen, Quellen und Anhängen, kopieren, exportieren, „Als eigene Kopie fortsetzen“, Versionen umschalten (eigene Ansicht). W: senden mit Anhängen, Neu erzeugen, Modellwahl. U: Nachrichten bearbeiten (braucht zum Senden zusätzlich W), umbenennen, System-Prompt. D: archivieren und löschen, für alle, mit Rückfrage „Der Chat gehört Anna und wird für alle gelöscht.“ W, U und D sind einzeln wählbar. Über eine Freigabe gibt es W/U/D nur für Konten mit Chat-Recht (Rolle). Freigaben verwaltet nur der Besitzer mit dem Rollenrecht „Teilen“. Empfänger tragen sich mit „Aus meiner Liste entfernen“ aus (Kontofreigabe gelöscht, Gruppenfreigabe über `left_by`).
  - **Widerruf:** `can()` prüft bei jeder Anfrage neu. Im laufenden Stream prüft `sharing.check_turn` vor jedem Anbieteraufruf und bei jedem Zwischenspeichern (alle 2 s); danach endet die Antwort als `aborted` mit „Die Freigabe für diesen Chat wurde beendet.“
  - **current_leaf je Konto:** `Conversation.current_leaf` bleibt der Hauptpfad des Besitzers. Empfänger folgen ihm, bis sie umschalten oder auf einem Nebenzweig schreiben; dann hält `ConversationView` ihr eigenes Ende. Umschalten ändert nie die Ansicht anderer und schließt keine Rückfragen. Neue Nachrichten spulen den Hauptpfad nur vor, wenn sie direkt an ihm hängen; wer genau am Elternteil steht, wandert mit. Neu erzeugen und Bearbeiten durch Empfänger lassen Bestehendes unverändert.
  - **Gleichzeitigkeit:** `prepare_turn` sperrt die Chat-Zeile (`select_for_update`) und bestimmt das Elternteil erst unter der Sperre. Zwei gleichzeitige Züge werden so hintereinander gehängt, der Baum bleibt konsistent. `chat.js` sendet in geteilten Chats `leaf` (zuletzt angezeigte Nachricht); weicht es vom Stand ab, antwortet der Server mit 409 („Inzwischen gibt es neue Nachrichten …“), und nichts wird angelegt. Der Hinweis „Neue Nachrichten in diesem Chat“ kommt aus `GET …/state/` beim Fokus und alle 30 s, keine Live-Synchronisation.
  - **Budget:** `Message.author` hält fest, wer geschrieben bzw. die Antwort ausgelöst hat. Die Buchung (`billing.UsageEntry.user`) geht auf `author`, bei Altdaten ohne Verfasser auf den Besitzer. Modellauswahl, Rollenrecht und Budget gelten immer für den Absender.
  - **Datenschutz:** Antworten bekommen den Kontext des Absenders: fester Prompt seiner Rolle, sein Zitierstil, seine Sammlungen, Werkzeuge und Websuche. Der System-Prompt des Chats bleibt. Ein Gedächtnis über Chats gibt es noch nicht (Vorschlag offen). Werkzeugrunden aus Antworten anderer Personen gehen nur als Antworttext an das Modell, nie mit Rohergebnissen (`sharing.own_rounds`). Dokumentquellen aus Sammlungen, die der Betrachter nicht lesen darf, zeigen nur Titel und Seite, ohne Link und ohne Literaturangaben; die Abschnittsansicht antwortet 404. Werkzeug-Rückfragen bestätigt nur, wer die Antwort ausgelöst hat. Verwalter sehen geteilte Chats nur als Empfänger; der Admin zeigt Freigaben als Metadaten (Besitzer, Empfänger, Rechte), und die Einsicht (M6-05) bleibt getrennt und nur lesend, ohne Kopie und ohne eigene Ansicht.
  - **Oberfläche:** Knopf „Teilen“ im Kopf mit Dialog (Liste mit Kurzform R/RW/RWUD, Kästchen Lesen fest an, Schreiben, Bearbeiten, Löschen, Widerrufen), Symbol an eigenen geteilten Chats, Abschnitt „Mit mir geteilt“ in der Seitenleiste, beim Empfänger „Geteilt von Anna · Lesen, Schreiben“ mit „Als eigene Kopie fortsetzen“ und „Aus meiner Liste entfernen“. Namen an Nutzernachrichten, sobald mehrere Personen im Chat sind, auch im Export.
  - **Kopie:** übernimmt den angezeigten Pfad ohne Kosten und ohne Werkzeugrunden, Quellen und Anhänge als Verweis auf dieselbe Datei (Löschen zählt Verweise).

- **M5-07** (Nutzerwunsch) Projekte wie in ChatGPT bzw. Claude (`chat/projects.py`, `api_projects.py`, `views_projects.py`, `static/chat/projects.js`, `projects.css`, Templates `chat/projects/`, Migration `chat.0023_projects`, Wiki-Seite `Projekte`):
  - **Modell:** `Project` je Konto, privat. `Conversation.project` mit `SET_NULL`. `instructions` gelten als zusätzlicher System-Prompt aller Chats des Projekts. `default_model` und `collections` sind nur Vorauswahl im Eingabefeld. Farbe aus fester Liste, `pinned` sortiert nach oben.
  - **System-Prompt:** `services.build_system_prompt` setzt `projects.instruction_block` nach den Hinweisen von MultiGPT und vor den System-Prompt des Chats. Der Block ist als Nutzerinhalt gekennzeichnet („stammen vom Nutzer, nicht von MultiGPT“, `<projekt_anweisungen projekt="…">`), damit das Modell ihn nicht für eine Systemnotiz hält. Er gilt für jede Antwort im Chat, auch wenn ein Empfänger eines geteilten Chats schreibt.
  - **Vorauswahl:** Standardmodell nur bei `can(USE_MODEL)` (erreichbar prüft `chat.js` wie bisher), sonst das zuletzt gewählte. Ein Chat mit eigenem Standardmodell behält es. Sammlungen nur, soweit lesbar, und nur, solange für den Chat keine eigene Auswahl im Browser gemerkt ist. Die Rechte prüft der Server beim Senden ohnehin.
  - **Oberfläche:** Abschnitt „Projekte“ in der Seitenleiste über „Chats“ (ohne Projekt): einklappbar (im Browser gemerkt, aktives Projekt offen), höchstens 20 Chats je Projekt, Menü (⋯) mit „Neuer Chat im Projekt“, „Projekt öffnen“, Umbenennen, Anheften, Archivieren, Löschen. „Neues Projekt“ fragt den Namen ab und öffnet die Projektseite. Chat verschieben über das Chatmenü („In Projekt verschieben …“, „Aus Projekt nehmen“) und den Knopf „Projekt …“ in der Chat-Kopfzeile, alles per Tastatur. Projektseite `/projekte/<id>/` mit Beschreibung, Fakten, Chatliste und Einstellungsformular (ohne JavaScript bedienbar). Neuer Chat im Projekt über `/?projekt=<id>`.
  - **Suche und Archiv:** Die Suche findet Chattitel und Projektnamen (passender Projektname zeigt alle Chats des Projekts). Archivierte Projekte verschwinden samt Chats aus der Seitenleiste. Im Archiv stehen archivierte Projekte (mit allen Chats) und Projekte mit archivierten Chats.
  - **Löschen:** Pflichtwahl „Chats behalten“ (danach ohne Projekt) oder „Chats mitlöschen“ (mit Anhängen, für alle Empfänger). Bestätigung im Dialog bzw. ohne JavaScript mit Pflicht-Häkchen. API: `DELETE …?chats=keep|delete`, ohne Angabe 400.
  - **Rechte und Datenschutz:** Fremde Projekte gibt es nicht (404, auch für Verwalter). Chats ordnet nur ihr Besitzer zu (Empfänger 403). Empfänger eines geteilten Chats sehen ihn unter „Mit mir geteilt“, ohne das Projekt des Besitzers: kein Name, kein Knopf, keine Vorauswahl, im Export keine Projektzeile. Der Admin zeigt Projekte nur mit Besitzer, Name und Anzahl der Chats.
  - **API:** `GET/POST /api/projects/`, `GET/PATCH/DELETE /api/projects/<id>/`, `POST /api/conversations/<id>/project/` (`{"project": id|null}`), `POST /api/conversations/` nimmt `project`. CSRF wie überall.
  - **Export:** Der Markdown-Export nennt das Projekt („Projekt: …“), aber nur für den Besitzer.
  - **Offen: Projekte teilen.** Geplant über denselben Mechanismus wie Chats: `Share.project` als drittes Ziel (Constraint „genau ein Ziel“ erweitern), `can()` prüft bei Chats zusätzlich die Freigaben ihres Projekts, `sharing.shared_with` liefert die Chats geteilter Projekte. Die Projektfreigabe gilt dann für alle Chats des Projekts. Mit chatshare abgestimmt: kommt als eigenes Paket, `Project` passt schon dazu (Besitzerfeld `owner`, kein Rechte-Sonderweg).
  - **Andockpunkt Scratchpad (M13):** `Scratchpad.project` als nullbarer, eindeutiger Fremdschlüssel auf `Project` (CASCADE), `Preset.project` für Projektvorlagen, Seite `/projekte/<id>/scratchpad/` neben der Projektseite. `projects.delete_project` ist die Stelle, an der beim Löschen eines Projekts das Scratchpad mitgeht, und die Projektseite bekommt dort einen Abschnitt. Ein Zitierstil je Projekt („Scratchpad → Projekt → Konto“) wäre ein weiteres Feld an `Project`.

### M6 – Vergleich, Verbrauch, Budgets, Familie
*Abhängig von: M3, M2. Frage 5/5a.*

- **M6-01** Vergleichsmodus mit 2–3 Modellen. Die Antworten sind Geschwister derselben Frage im Chat-Baum. Umsetzung:
  - `POST messages` nimmt `compare: true`. Weitere Spalten (`regenerate`) setzen `current_leaf` und `default_model` nicht um, bis der Nutzer wählt.
  - `POST branch` nimmt `adopt_model: true`. Bei der Wahl wird das Modell der Spalte zum Standardmodell des Chats.
  - Das SSE-Event `usage` liefert zusätzlich `cost`.
  - **Werkzeuge (MCP) sind im Vergleich aus.** Parallele Rückfragen würden sich gegenseitig schließen. Websuche und Sammlungen bleiben nutzbar.
  - Jede Spalte prüft das Budget einzeln. Nach der Wahl schaltet sich der Vergleich wieder aus.
  - Jede Spalte belegt einen gunicorn-Thread, bei 2 × 8 Threads reicht das für etwa 5 gleichzeitige Vergleiche.
- **M6-02** Verbrauchsübersicht je Nutzer, Abrechnungskonto, Modell und Monat auf Basis der Buchungen (`billing.UsageEntry`, M6-08). „Mein Verbrauch“ zeigt je Konto Ein- und Ausgabe, Cache gelesen und geschrieben, Reasoning und den Betrag (monetär in Kontowährung und EUR, Token-Konten nur Tokens, Pauschalkonten nur gezählt), Monat wählbar (`?monat=JJJJ-MM`, letzte 12 Monate). „Familie“ zeigt dasselbe je Mitglied, ohne Inhalte.
- **M6-03** Budgets: Hinweis bei 80 %, bei 100 % sind die Modelle des betroffenen Kontos gesperrt (M6-10). Die Prüfung läuft vor jeder Antwort (`can(USE_MODEL)`), die Sperre erscheint in der Modellauswahl ausgegraut mit Text je Konto.
- **M6-04** Seite "Familie": Konten anlegen und sperren, Rolle zuweisen, Passwort zurücksetzen, Gruppen, Verbrauch.
- **M6-05** Einsicht in Jugendlichen-Chats als Option je Konto (`allow_supervision`, standardmäßig aus). Das Mitglied sieht in der Oberfläche, dass die Option aktiv ist.
- **M6-06** Kontenrahmen (Nutzerwunsch 2026-10-10, eigene App `multigpt/billing`, Wiki „Kosten-und-Budgets“). `BillingAccount` mit Name, Art (`monetary` mit Währung EUR/USD, `tokens`, `flat`), Notiz, aktiv. Jeder `Provider` gehört zu genau einem Konto (`Provider.billing_account`, `PROTECT`), ein Konto bündelt beliebig viele Anbieter (z. B. OpenRouter und OpenAI direkt). Vorbelegung beim Speichern (`billing.signals`): lokal → gemeinsames Token-Konto „Lokale Modelle“, sonst ein monetäres Konto mit dem Namen des Anbieters in USD (ein vorhandenes Konto gleichen Namens wird verwendet). Art und Währung sind fest, sobald Preise oder Buchungen existieren. Ein inaktives Konto sperrt seine Modelle.
  - **Warum eigene App:** Abrechnung ist ein abgeschlossener Bereich mit eigenen Migrationen, eigenem Admin-Abschnitt und Logik (`pricing`, `booking`, `budgets`, `ecb`). `chat` kennt nur den Fremdschlüssel am Anbieter, `accounts.usage` bleibt Fassade für Oberfläche und Rechte.
- **M6-07** Preise mit Gültigkeit: `ModelPrice` je Modell ab `valid_from`, in der Währung des Kontos, je 1 Mio. Tokens: `input`, `cached_input`, `cache_write` (5 Min.), `cache_write_1h`, `output` (inkl. Reasoning), optional Langkontext (`long_context_threshold` plus `long_*`), `unit_prices` als JSON mit geprüften Schlüsseln (`web_search`, `web_fetch`, `image` mit Varianten `image:<qualität>[:<größe>]`, `audio_minute`, `tts_characters` je 1 Mio. Zeichen, `request`). Preise trägt der Verwalter ein; es gibt bewusst keine eingebauten Preise und keinen Knopf „Preisvorschlag übernehmen“ (Preise ändern sich zu oft, siehe Frage im Bericht). `AIModel.price_in/price_out` entfallen (`chat 0026`).
- **M6-08** Buchungen: `UsageEntry` je Antwort (eine, bei Fortsetzung nach Rückfrage aktualisiert), je Anhang bzw. Werkzeuggebühr: Konto, Nutzer (Verfasser, sonst Besitzer), Modell (plus Name als Momentaufnahme), Zeitpunkt (Beginn der Antwort), alle Token-Arten, Anfragen, Einheiten, `rounds` (Tokens je Anbieteraufruf für den Langkontext-Tarif), Betrag in Kontowährung, EUR-Betrag, Kurs und Kursdatum, Preisversion. `Message.cost` bleibt (EUR-Momentaufnahme aus der Buchung; Token- und Pauschalkonten 0). Buchungen bleiben erhalten, wenn ein Chat gelöscht wird (`SET_NULL`); Löschen gibt also kein Budget mehr frei. `providers.base.Usage` hat dafür `cached_read`, `cache_write`, `cache_write_1h`, `reasoning`, `units` (Vergleich `==` weiter nur über die Summen, abwärtskompatibel).
- **M6-09** Währung: `ExchangeRate` (Datum, 1 USD in EUR), im Admin pflegbar. Verwendet wird der letzte Kurs am oder vor dem Buchungstag (Europe/Berlin). Ohne Kurs bleibt `amount_eur` leer (nie 0), der Admin warnt, und beim Speichern eines Kurses werden fehlende EUR-Beträge nachgetragen (auch `Message.cost`). Für Budgets zählen Beträge ohne Kurs vorläufig mit dem neuesten Kurs, ohne jeden Kurs 1:1. Optional: EZB-Referenzkurs (`BILLING_ECB_FETCH=true`, standardmäßig aus; nur die feste HTTPS-Adresse, ohne Weiterleitungen, Größenlimit; Knopf im Admin und `manage.py fetch_ecb_rate`).
- **M6-10** Budgets je Konto (`AccountBudget`, Rolle oder Nutzer, Nutzer vor Rolle): monetär Monatsbudget in EUR, Token-Konten Monatskontingent in Tokens (Ein- plus Ausgabe), leer = unbegrenzt, Pauschalkonten ohne Budget. Das bisherige Budget an Rolle und Konto gilt weiter als **Gesamtbudget (EUR)** über alle monetären Konten (Migration: Werte bleiben, nur umbenannt). Gesperrt werden nur die Modelle des ausgeschöpften Kontos, beim Gesamtbudget alle kostenpflichtigen; Modelle ohne Preis (oder nur mit Preis 0) bleiben frei. Hinweis ab 80 % je Konto (SSE `status`, Kopfzeile, „Mein Verbrauch“). **Rennen:** Geprüft wird vor jeder Antwort gegen die abgeschlossenen Buchungen (wie bisher); parallel laufende Antworten können das Budget um ihre eigenen Kosten überschreiten, danach greift die Sperre.
  - **Datenmigration** (`chat 0025_provider_billing_account`, idempotent, rückwärts: Preise zurück an das Modell): Konten für bestehende Anbieter (monetär in EUR, wenn ein Modell schon Preise hatte, weil die alten Preise Euro waren, sonst USD), `price_in/price_out` → `ModelPrice` ab der frühesten Nutzung (sonst 1.1.2000), je Antwort mit Modell oder Kosten und je Anhang mit Kosten eine Buchung mit den gespeicherten Werten (`legacy`). Antworten gelöschter Modelle buchen auf „Altbestand (gelöschte Modelle)“.
  - **Admin** „Kosten und Abrechnung“: Konten (mit Budgets als Inline und Liste der Anbieter), Modellpreise mit Historie (auch als Inline am Modell), Wechselkurse, Buchungen nur lesend mit CSV-Export je Monat bzw. Konto (Semikolon, Dezimalkomma, UTF-8 mit BOM, Formeln entschärft).
  - **Abrechnungsregeln** (Quellen im Wiki): Anthropic `input_tokens` ohne Cache, Summe = Eingabe + Cache schreiben + Cache lesen, Schreiben 1,25× (5 Min.) bzw. 2× (1 Std.), Lesen 0,1× (modellabhängig), Thinking als Ausgabe; OpenAI `prompt_tokens` inkl. `cached_tokens`, `completion_tokens` inkl. `reasoning_tokens`, ab GPT-5.6 auch `cache_write_tokens`; Gemini `promptTokenCount` inkl. `cachedContentTokenCount`, `thoughtsTokenCount` zusätzlich zu den Kandidaten und als Ausgabe abgerechnet. Langkontext gilt je Anfrage für alle Tokens der Anfrage.

### M7 – RAG
*Abhängig von: M2, M4a (für die Suche als Werkzeug), Fragen 4b, 4c.*

- **M7-01** `Collection`, `Document`, Freigaben an Gruppen über `Share`. Upload mit Prüfung von Dateityp und Größe, Dateinamen werden nicht übernommen.
- **M7-02** Job-Tabelle und Kommando `make worker`. Der Worker holt Jobs mit `SELECT … FOR UPDATE SKIP LOCKED`, wiederholt fehlgeschlagene Jobs mit Obergrenze. Dazu die Unit `multi-gpt-worker.service`.
- **M7-03** Textextraktion für PDF, DOCX, TXT und MD (seit M7-11 auch Bilddateien), Zerteilung in ca. 800 Tokens mit 100 Überlappung und Fundstelle (seit M7-13: erste/letzte Seite, Absatz von–bis, Gliederungsabschnitt), Embeddings (ursprünglich über OpenAI, seit M7-09 lokal über LM Studio). **OCR für gescannte PDFs** mit Tesseract und deutschem Sprachpaket: Seiten ohne Textebene werden erkannt und per OCR gelesen. Das Paket bekommt dafür `tesseract-ocr` und `tesseract-ocr-deu` als Abhängigkeit.
- **M7-04** Migration für `Chunk` mit fester Vektordimension (zuerst 1536 für OpenAI, mit M7-09 auf 768 für nomic-embed-text) und HNSW-Index (Kosinus). `make reindex`.
- **M7-10** (Nutzerwunsch) Verzeichnisquellen:
  - Modell `DirectorySource` (App `rag`), dazu die Dokumentfelder `source`, `source_path`, `source_mtime`, `source_size` und `source_sha256`.
  - Scan-Job im vorhandenen Worker, periodisch und ohne Dubletten. Abgleich über mtime und Größe, bei Änderung zusätzlich über SHA-256.
  - Admin unter „Dokumente (RAG)“ → „Verzeichnisquellen“ mit „Jetzt einlesen“ und „Pausieren“.
  - Sicherheit: Wurzeln aus `RAG_SOURCE_ROOTS`, `realpath`-Prüfung je Datei, keine Symlinks.
- **M7-09** (Entscheidung 2026-10-09) Dokumentverarbeitung komplett lokal:
  - Embeddings über LM Studio (nomic-embed-text, Dimension 768 statt 1536). Dafür Migration der Vektorspalte und „Alles neu indexieren“.
  - Die Sperre von LM-Studio-Anbietern in `RagSettings.clean()` entfällt.
  - Neue OCR-Einstellung „olmOCR (LM Studio)“ oder „Tesseract“, mit Auswahl des Vision-Modells.
  - `openai_compat` bekommt Bild-Eingaben (Seite als PNG) mit dem Prompt aus der olmOCR-Dokumentation.
  - Ist LM Studio offline: Der Worker wartet und wiederholt, bei OCR springt optional Tesseract ein.
- **M7-11** (Nutzerwunsch) Abbildungen indexieren, Modul `rag/figures.py`:
  - Neue `RagSettings`-Felder `describe_figures` (Standard aus), `figure_model` (getrennt vom OCR-Modell, nur OpenAI-kompatible Anbieter) und Grenzen `figure_max_per_document` (50), `figure_max_per_page` (10), `figure_min_edge` (150 px) und `figure_max_edge` (1024 px). Zähler `Document.figures_described`. Migration `chat 0020_figures`.
  - Herauslösen: PDF mit Textebene über pypdf (Größe aus dem Bildobjekt vor dem Dekodieren, Position über die `Do`-Befehle im Inhaltsstrom), nicht über `pdfimages`. DOCX über `a:blip`/`v:imagedata` und die Beziehungen des Dokumentteils in Dokumentreihenfolge. Aussortiert werden kleine Bilder, Linien (Seitenverhältnis über 6) und Wiederholungen (SHA-256 der aufbereiteten Bilddaten, bei PDF zusätzlich der Objektverweis). Pillow verkleinert, wendet die EXIF-Ausrichtung an und kodiert ohne Metadaten neu (PNG bei wenigen Farben, sonst JPEG). Pixelgrenze gegen Dekompressionsbomben.
  - Beschreiben: ein Aufruf von `describe_image` je Abbildung mit deutschem Prompt, Temperatur 0,1, Sprache des Dokuments (Wortliste, sonst Deutsch), Bildunterschrift aus der Umgebung. Ergebnis als eigener Absatz „[Abbildung: …]“ an der Bildposition (passt zur Absatzzählung). Fehler wie bei der OCR: nicht erreichbar → Auftrag wartet, sonst wiederholbar; Ablehnung eines Bildes (HTTP 400/413/415/422, abgeschnitten) lässt nur diese Abbildung weg. Abbruchprüfung und Lebenszeichen vor jedem Aufruf.
  - Bilddateien JPG, PNG, TIFF (mehrseitig) und WEBP als Dokument, erkannt an den Magic Bytes. Jede Seite geht als einseitiges PDF an die bestehende OCR, mit `describe_figures` kommt die Beschreibung dazu. HEIC nicht, das bräuchte `pillow-heif`.
  - Admin: Abschnitt „Abbildungen“ in den Einstellungen, Zeile in der RAG-Übersicht, Zähler am Dokument.
- **M7-05** Abfrage: Top 6 mit Zugriffsfilter **in der SQL-Abfrage selbst**, optional zusätzlich Volltextsuche. Quellen mit Fundstelle und Literaturangaben im Zitierstil des Kontos unter der Antwort (M7-13).
- **M7-06** RAG-Suche zusätzlich als Werkzeug für werkzeugfähige Modelle.
- **M7-12** (Nutzerwunsch) Dokumentwerkzeuge `list_documents`, `document_info` und `read_document` in `chat/rag/doc_tools.py`, registriert wie `search_documents`:
  - Eingebaut, nur lesend, ohne Rückfrage. Angeboten nur, wenn das Konto lesbare Dokumente hat; `available` wird vor jedem Aufruf neu geprüft. Rechte über `readable_collections`/`accessible_chunks` in SQL. Fremde, unlesbare und nicht vorhandene IDs ergeben dieselbe Meldung „Dokument nicht gefunden.“. Ungültige Argumente sind Fehler mit deutschem Text. Ins Log kommen nur IDs und Zahlen.
  - `list_documents`: Seitenabruf mit `page` (ab 1) und `page_size` (Standard und höchstens 50), stabile Sortierung Sammlung → Titel → ID. Jede Antwort nennt „Seite X von Y (N Dokumente insgesamt)“ und den Aufruf für die nächste Seite. Filter: `collection` (Name oder ID; ohne Angabe die für die Antwort gewählten Sammlungen aus `tool_state`, sonst alle lesbaren), `query` (Titel, Normnummer, Autoren, Herausgeber, Reihe, Pfad), `year` (Literaturdatum, ersatzweise Hochladejahr), `kind` (Art aus `bib_type`, Wert oder Bezeichnung, z. B. „Norm“), `status` (Standard: indexiert) und `topic`. `topic` rankt wie die Suche (Vektor, ggf. RRF mit Volltext), aber nur über die Abschnitte der bereits gefilterten Dokumente; je Dokument zählt der beste Abschnitt. Normen erscheinen als „Normnummer:Ausgabe“.
  - `document_info`: Literaturangaben (`bib_*`, dazu der Eintrag im Stil des Kontos), Sammlung, relativer Pfad bei Verzeichnisquellen (nie der Serverpfad), Seiten und Abschnitte, Status, Inhaltsverzeichnis. Das Verzeichnis kommt aus der nummerierten Gliederung (`chunking.find_sections`, dieselbe Regel wie beim Indexieren), sonst aus Markdown-Überschriften, nummerierten und kurzen Zeilen am Absatzanfang. Kopfzeilen, die sich wiederholen, fallen weg. Höchstens 150 Einträge bzw. 8 000 Zeichen.
  - `read_document`: Gliederungsabschnitt (`section`, mit Unterabschnitten bis zum nächsten Abschnitt derselben oder höheren Ebene) oder Seiten-/Absatzbereich (`paragraph_from` gilt auf `page_from`, `paragraph_to` auf `page_to`). Höchstens 12 000 Zeichen bzw. 10 Seiten je Aufruf; bei Kürzung nennt die Ausgabe den Aufruf zum Weiterlesen. Fehlt der Abschnitt, kommt die Liste der obersten Ebene. Ausgabe als `<quellmaterial>` mit einer Quelle [n] je Seite (`SourceRef` mit Abschnitt, Seite, Absatz und Literaturangaben).
  - Text aus den Chunks: Der Seitentext ist nicht gespeichert. Die überlappenden Chunks werden auf Wortebene zusammengesetzt. Die Überlappung wird zuerst mit der Länge geprüft, die der Chunker mit den aktuellen Einstellungen gewählt hätte (wichtig bei Wortwiederholungen), sonst gilt die längste Übereinstimmung. Ohne Übereinstimmung entscheidet die Fundstelle über den Absatzumbruch. Seite und Absatz je Absatz ergeben sich aus den Ankern der Chunks (Anfang und Ende). Liegen mehrere sehr kurze Seiten im Inneren eines Chunks, ist die Seitengrenze nicht gespeichert: Der Absatz bekommt dann einen Seitenbereich ohne Absatznummer.
  - `search_documents` verweist in seiner Beschreibung auf `read_document` (Zusammenhang) und `list_documents` (Überblick).
  - Tests in `tests/test_rag_doc_tools.py`, darunter ein Ende-zu-Ende-Lauf mit respx: Normen zum Qualitätsmanagement auflisten, dann Abschnitt 7.5 lesen.
- **M7-13** (Nutzerwunsch) Zitieren mit Seite, Absatz und Gliederung; Zitierstile:
  - **Absätze:** Absatz = durch Leerzeile getrennter Block, je Seite ab 1, ohne Seiten durchgehend; Seitenwechsel beendet einen Absatz. `extract.py` liefert die Grenzen: DOCX ein Absatz je nicht leerem `w:p` (Tabellen als ein Absatz), olmOCR-Tabellen als eigener Absatz, Tesseract/TXT/MD unverändert. PDF-Textebene (pypdf) ohne Leerzeilen: vorsichtige Heuristik `pdf_paragraphs` (Zeile mit Satzende und unter 85 % der vollen Zeilenbreite = 90-%-Quantil, bzw. kurze Überschrift ohne Satzzeichen nach Satzende; nächste Zeile beginnt groß/mit Ziffer). pdftotext und HTML werden nicht verwendet bzw. nicht unterstützt.
  - **Gliederung:** `chunking.find_sections` erkennt nummerierte Überschriften (`^\d+(\.\d+){0,5}\s+[A-ZÄÖÜ]`, „Anhang A“, „A.2.1“) am Absatzanfang oder nach Satzende, kurz, ohne Satzzeichen/Seitenzahl am Ende, nicht vor klein weitergehender Zeile. Die Nummern müssen plausibel aufsteigen (nächste derselben Ebene, eine darf fehlen; erste tiefere; höhere); zwei zueinander passende Ausreißer setzen die Folge neu auf. Inhaltsverzeichnisse (mindestens 4 nummerierte Zeilen ohne Text dazwischen) werden übersprungen.
  - **Felder** (Migration `chat 0019_citation`): `Chunk.page` ist jetzt die erste Seite, dazu `page_end`, `paragraph`, `paragraph_end` (NULL bei Bestand), `section`, `section_title`, `section_end`; `SourceRef` ebenso (ohne Titel) plus `biblio` (JSON-Stand der Literaturangaben). `Document.bib_*`: Art (Buch, Sammelband, Kapitel, Artikel, Konferenzbeitrag, Bericht, Norm, Webseite, Sonstiges), Autoren, Herausgeber, Titel, Sammelwerk, Datum, Verlag, Ort, Auflage, Reihe/Band, URL, ISBN Print/eBook, DOI, Zeitschrift/Band/Heft/Seiten, Normnummer, herausgebende Stelle, Status, Ersatz für, `bib_edited`. `RagSettings.crossref_enabled` (aus) und `crossref_mailto`. Konto: `accounts 0005_citation_prefs` (`citation_style` Standard DIN ISO 690, `citation_short`, `citation_locator`). Bestand braucht „Alles neu indexieren“.
  - **Formatierung** in `chat/citations.py`, eigene Funktionen statt citeproc/CSL: `citation_label` (Fundstelle), `entry`/`short` für DIN ISO 690, APA 7, Harvard, Chicago Author-Date, MLA 9 (deutsche Fassungen), `bibtex` (biblatex-kompatibel, Normen als `@techreport` mit `type = {Norm}`). Normen im Kurzbeleg über Nummer und Ausgabe.
  - **Verwendung:** Kontextblock (`fundstelle`, `kurzbeleg`, erste Zeile „[n] Titel, Fundstelle“), `SYSTEM_NOTE`, Werkzeug `search_documents`, SSE-Event `sources` und `GET …/messages/` (je Quelle `location`, `entry`, `short`, `inline`, `formats`), Template `_sources.html` (`data-citations`) und `static/chat/citations.js` (Liste, „Zitat kopieren“ mit Stilmenü, „Literaturverzeichnis kopieren“, Kurzbeleg statt [n] über einen Hook in `markdown.js`; auch im Vergleich). Embedding-Kopf: „Seiten: 12–13“ und „Abschnitt: 7.5.3 Titel“, keine Absätze.
  - **Vorbelegung** (`ingest.detect_metadata`, nur leere Felder, nie nach `bib_edited`): PDF-/DOCX-Metadaten (Platzhalter verworfen), DOI aus Metadaten bzw. den ersten 2 Seiten, Normnummer mit Ausgabedatum aus Dateiname/Titelseite, optional Crossref (`rag/crossref.py`: fester Host, kein Redirect, 5 s, Größengrenze, Fehler → nichts).
  - **Bearbeiten:** Seite `dokumente/<id>/angaben/` (WRITE; fremd 404, nur lesend 403) mit Vorschau aller Stile, Admin „Dokumente (RAG)“ (nur `bib_*` bearbeitbar). Einstellungsseite `/einstellungen/` im Nutzermenü.
  - Tests in `tests/test_rag_citation.py`.
- **M7-07** Tests: Zerteilung, Trefferqualität, Zugriffsgrenzen (fremde private Sammlung ist nie im Ergebnis), Entzug einer Freigabe wirkt sofort, Worker-Wiederholung.
- **M7-08** (Nutzerwunsch) Eigene RAG-Verwaltung im Admin: App `multigpt.rag` ohne eigene Tabellen, mit Proxy-Modellen für die Abschnitte Einstellungen, Sammlungen, Dokumente und Aufträge. Die bisherigen Einträge unter „Chat“ entfallen.
  - **Übersichtsseite:** Konfiguration, Zahlen, Worker-Zustand, Warteschlange.
  - **Aktionen:** neu indexieren (alles, je Sammlung, je Dokument), erneut versuchen, hängende Aufträge zurücksetzen.
  - **Datenschutz:** kein Inhalt sichtbar, ein Test prüft das.

### M8 – Websuche
*Abhängig von: M3, M4a (als Werkzeug), Frage 4a.*

- **M8-01** Schnittstelle `search(query)`, umgesetzt für SearXNG. `SearchSettings` im Admin mit Knopf „SearXNG testen“. Einrichtungsanleitung im GitHub-Wiki (`docs/wiki/`, Docker und nativ).
- **M8-02** Seitenabruf mit SSRF-Schutz: DNS auflösen, private und lokale Adressbereiche sperren, Weiterleitungen erneut prüfen. Dazu Timeouts und Größenlimit.
- **M8-03** Inhalte auf Text reduzieren und klar als Quellmaterial markiert mit nummerierten Quellen an das Modell geben.
- **M8-04** Quellenanzeige unter der Antwort. Websuche zusätzlich als Werkzeug.
  - **Wenn das Modell sagt, es könne nicht suchen:** Das Werkzeug `web_search` bekommen nur Modelle mit `supports_tools` (vorher standen alle auf False, Erkennung siehe Abschnitt 2, Punkt 1). Ein fester Satz im System-Prompt (`websearch.system_hint`, ohne persönliche Daten) sagt dem Modell, was gilt: Werkzeug angeboten → „nutze web_search für aktuelle Fakten“; Websuche verfügbar, aber weder Schalter an noch Werkzeug → „weise auf den Schalter ‚Websuche‘ hin“. Im Chat steht bei Modellen mit Werkzeugen „· Werkzeuge“ in der Auswahl, bei Modellen ohne unter dem Schalter „Dieses Modell kann nicht selbst suchen – mit dem Schalter sucht MultiGPT vorab“.
- **M8-05** Tests: Such-Backend gemockt, Intranet-Adressen und Weiterleitungen auf solche werden abgelehnt.
- **M8-06** **Seiten abrufen** (`websearch/pages.py`, Werkzeug `fetch_url(url, offset)`): Abruf über `fetch.fetch_raw` (derselbe SSRF-Schutz, `guard` prüft gesperrte Domains bei jedem Schritt, auch nach Weiterleitungen). Inhaltstypen HTML, Text, JSON und PDF (Textebene über `rag.extract`, ohne OCR; gescannte PDFs werden mit Meldung abgelehnt), bis 5 MB. Die Ausgabe ist ein `<quellmaterial>`-Block mit einer Quelle [n], Titel, URL und Abrufdatum (`SourceRef.biblio["accessed"]`, `sources.accessed_date`). Höchstens 12 000 Zeichen, bei Kürzung ein Hinweis mit `offset` zum Weiterlesen. Kein robots.txt, weil ein einzelner Abruf wie im Browser.
- **M8-07** **Websites durchsuchen** (Werkzeug `crawl_site(url, max_pages=10, same_site=True, path_prefix=None)`): Breitensuche, Tiefe ≤ 2, höchstens 20 Seiten (Admin-Grenze `crawl_max_pages`), Gesamtzeitlimit `crawl_time_seconds`. robots.txt über denselben sicheren Abruf (`urllib.robotparser`, Fehler oder Fehlen = erlaubt), User-Agent `MultiGPT (+Familien-Instanz)`, je Rechner mindestens 0,5 s Abstand (bzw. `Crawl-delay`, höchstens 2 s). URLs werden normalisiert (Fragment und Tracking-/Sitzungsparameter weg, Query sortiert), je Pfad höchstens drei Query-Varianten, Liste ausgeschlossener Endungen, `rel=nofollow` wird nicht verfolgt, Formulare nie. `same_site=False` erlaubt Subdomains derselben Domain, nie fremde Websites. Ausgabe: je Seite Titel, URL, Auszug, für die ersten Seiten der volle Text im Budget von etwa 20 000 Zeichen. Jede Seite ist eine Quelle.
  - **Verfügbarkeit:** Recht `WEB_SEARCH`, „Websuche aktiv“ und der jeweilige Schalter (`fetch_url_enabled`, `crawl_enabled`, Standard an); eine SearXNG-URL ist nicht nötig. Angeboten nur Modellen mit `supports_tools`, ohne Rückfrage, weil sie nur lesen. Hinweissatz im System-Prompt: `pages.system_hint`.
  - **Fester Ablauf:** Mit dem Schalter „Websuche“ werden bis zu drei URLs aus der Frage zusätzlich zur Suche abgerufen (`pages.fixed_entries`), so geht „Fasse diese Seite zusammen: https://…“ auch ohne Werkzeuge. Nicht abrufbare Seiten werden dem Modell genannt; scheitert nur die Suche, bleiben die Seiten erhalten.
  - **Admin:** `SearchSettings` mit `fetch_url_enabled`, `crawl_enabled`, `crawl_max_pages`, `crawl_time_seconds` und `blocked_domains` (gilt auch für Suchtreffer), Knopf „Abruf testen“ neben „SearXNG testen“. Migration chat `0024_web_fetch`.
  - **Logs:** nur Anzahlen und Fehlerarten, nie URLs oder Inhalte.
- **M8-08** **Gegen erfundene Fakten und Links:**
  - **Festlegung: Grundregeln als DB-Standard per Datenmigration.** `ChatSettings.base_instructions` („Grundregeln für alle Modelle“, Singleton im Admin unter „Chat“ → „Chat-Einstellungen“) ist der erste Teil jedes System-Prompts, für alle Modelle und Anbieter (Einzelchat, Vergleich, Neu erzeugen, Bearbeiten). Der Standardtext steht in `models.DEFAULT_BASE_INSTRUCTIONS`. Die Migration `0024_web_fetch` kopiert ihn (sie importiert die Konstante nicht), legt den Datensatz an, falls er fehlt, und setzt den Text nur, wenn das Feld leer ist. Rückwärts tut sie nichts. postinst migriert bei jedem Update, so bekommen auch bestehende Installationen den Standard, ohne dass ein geänderter Text überschrieben wird. Ein leeres Feld bedeutet: nichts einfügen. Reihenfolge in `services.build_system_prompt`: Grundregeln → fester Prompt der Rolle → Hinweise von MultiGPT (`sources.SYSTEM_NOTE`, Websuche, `fetch_url`/`crawl_site`) → Projekt-Anweisungen → System-Prompt des Chats.
  - **Kennzeichnung ungeprüfter Links** (`citations.js`, `markLinks`, aufgerufen nach jedem Rendern): Links, deren URL (normalisiert) bzw. Rechnername in keiner Quelle der Antwort, keinem Werkzeugergebnis und nicht in der Frage vorkommt, bekommen ⚠ mit `title` und Text für Screenreader. Darunter steht der Hinweis „Diese Antwort enthält Links ohne Quelle“ mit dem Knopf „Belege prüfen“: Websuche an, Prüfbitte mit den Links ins Eingabefeld, nicht gesendet. Läuft nur im Client, ohne Netzaufruf. Alle Links in Antworten bekommen `rel="noopener noreferrer nofollow"`. Kontoeinstellung „Ungeprüfte Links markieren“ (`User.mark_unverified_links`, Standard an, Migration accounts `0006_mark_unverified_links`).
  - *Tests:* `tests/test_web_pages.py` (respx: Text, Kürzung und `offset`, JSON, PDF ohne Textebene, Weiterleitung ins Heimnetz, gesperrte Domains, robots.txt, Seitenzahl, Tiefe, gleiche Website, Tracking-Parameter, Zeitlimit, Rechte, `supports_tools`, URLs im festen Ablauf, Abrufdatum, Grundregeln im System-Prompt und im Request-Body, Vergleich, leeres Feld, Migration, Admin, Einstellung). Die Link-Markierung ist im Browser geprüft (headless Firefox).

### M9 – Bilder
*Abhängig von: M4a, Frage 4e.*

- **M9-01** `generate_image` für den gewählten Anbieter. Modus "Bild" mit Formatwahl, Ergebnis als `Attachment`. **Umgesetzt** (Anlass: Auf „Zeichne mir die Deutschlandflagge mit Bundesadler“ lieferte ein Chatmodell nur SVG-Code, ohne ein Bildmodell zu suchen):
  - **Adapter** `generate_image(model_id, prompt, size, quality, background, n) -> ImageResult` (Bilder als Rohbytes plus `Usage`, `providers/base.py`). OpenAI-kompatibel über `POST /images/generations` (Doku gelesen 2026-10-10: GPT-Image-Modelle `gpt-image-1`, `-1-mini`, `-1.5`, `-2` …, `size` 1024x1024/1536x1024/1024x1536/auto, `quality` low/medium/high/auto, `background` transparent/opaque, `output_format`, Antwort immer `b64_json`, `usage.input_tokens`/`output_tokens`; DALL·E mit `response_format=b64_json` und eigenen Größen). Eine URL statt base64 wird nie nachgeladen. Google über `generateContent` mit `responseModalities ["TEXT","IMAGE"]` und `imageConfig.aspectRatio` (Bilder als `inlineData`, Gedanken-Teile übersprungen; Imagen ist laut Google abgeschaltet). Anthropic kann keine Bilder erzeugen. Fehlertexte wie bei den übrigen Adaptern (ohne Key, ohne Rohtext); Inhaltsfilter (OpenAI `moderation_blocked`/`content_policy_violation`, Gemini `promptFeedback.blockReason` bzw. `finishReason` `IMAGE_SAFETY` u. a.) als `ContentBlocked` mit eigener Meldung „… wegen seiner Inhaltsrichtlinien abgelehnt. Bitte die Beschreibung umformulieren.“
  - **Auswahl des Bildmodells** (`chat/images.py`, `pick_image_model`): Recht der Rolle `can_images`, dann das Standard-Bildmodell aus „Chat-Einstellungen“ (`ChatSettings.default_image_model`, Migration `chat 0027_image_settings`), sonst das erste aktive Bildmodell (`capability=image`, Reihenfolge, Name) eines aktiven OpenAI-kompatiblen oder Google-Anbieters, das die Rolle erlaubt (`model_permitted`), das Budget nicht sperrt (`can(USE_MODEL)`) und nach dem gespeicherten Status online ist. Ohne Treffer die Meldung „Es ist kein Modell zur Bilderzeugung eingerichtet bzw. freigegeben.“ (bzw. Budget- oder Offline-Grund). Erkennung `gpt-image-*`, `chatgpt-image`, `gemini-*-image` → „Bilderzeugung“ in `capabilities.py` (tools).
  - **Werkzeug `generate_image(prompt, size?, quality?, transparent_background?)`** (eingebaut, `tooling.register_builtin`): nur für Chatmodelle mit `supports_tools` und nur, wenn ein Bildmodell nutzbar ist. Die Beschreibung verlangt, Bilder nicht als SVG/ASCII/Code zu zeichnen, außer ausdrücklich gewünscht; ein Satz im System-Prompt (`images.system_hint`, nach den Websuche-Hinweisen) sagt dasselbe. Ergebnis an das Modell nur „Bild erzeugt … (Anhang #n, B×H px, Modell)“, nie das Bild. Höchstens 4 Bilder je Antwort. **Festlegung Rückfrage:** Standard ohne Rückfrage (der Nutzer hat das Bild verlangt; Budget wird vor jedem Bild geprüft), umschaltbar mit `ChatSettings.image_tool_confirm` („Rückfrage vor Bilderzeugung“). Dafür kennt `tooling.BuiltinTool` jetzt ein optionales `confirm()`; die Schleife pausiert eingebaute Werkzeuge damit wie MCP-Werkzeuge.
  - **Modus „Bild“** im Eingabefeld (`_image_mode.html`, `static/chat/image_mode.js`, `api_images.py`): Schalter „Bild erzeugen“ mit Format (quadratisch, hoch, quer) und Qualität (automatisch, niedrig, mittel, hoch). Der Request trägt `"image": {format, quality}`, die Nachricht geht ohne Chatmodell an das Bildmodell; Antwort mit `Message.model` = Bildmodell und `tool_state["image"]`, Standardmodell des Chats bleibt. Gleiche Rechte- und Budgetprüfung; nur neue Nachrichten, ohne Anhänge.
  - **Hinweis ohne Werkzeuge:** Kann das gewählte Chatmodell keine Werkzeuge und klingt die Nachricht wie ein Bildauftrag (`images.REQUEST_PATTERNS`, vorsichtig: „zeichne mir/die …“, „male ein …“, „erstelle/erzeuge … ein Bild/Foto/Logo …“, „draw me“, „generate an image“; nicht bei SVG/Code/ASCII …), zeigt die Oberfläche vor dem Senden „Mit Bildmodell erzeugen“ bzw. „Trotzdem an das Chatmodell“. Das erste Senden wird angehalten; nie ein Umleiten ohne Zustimmung. Muster kommen per `json_script` vom Server.
  - **Ergebnis als Anhang:** `attachments.process_image` (neu kodiert ohne EXIF/XMP/ICC/Textblöcke, längste Kante 2048 px, Vorschau WebP, Größengrenze `ATTACHMENT_MAX_IMAGE_MB`), `Attachment` an der Antwort mit `generated_by_model` (Herkunft „erzeugt“), `owner` leer. Anzeige wie Uploads: Vorschau, Lightbox, Download über die geschützte Auslieferung (Leserecht am Chat).
  - **Kosten:** je Bild eine Buchung `billing.booking.book_attachment` (Konto des Bildmodells) mit der Einheit `image:<qualität>:<größe>` (Preis in `ModelPrice.unit_prices`, Rückfall `image:<qualität>` → `image`); Tokens aus `usage` werden mitgegeben, sobald `book_attachment` sie annimmt. Gebucht auf den Absender (geteilte Chats: wer W hat und sendet), `Attachment.cost` setzt die Buchung; die Bild-Antwort selbst bucht nichts.
  - **Datenschutz:** Beschreibung nur an den Anbieter des Bildmodells, Bilder nie an das Chatmodell, Logs nur mit IDs; der Verwalter sieht im Admin nur die Einstellungen, keine Bilder.
  - **Vorbereitet für M9-02:** `Attachment.source_image`, `ProviderAdapter.edit_image`, `AIModel.can_edit_images`. Geplant: Werkzeug `edit_image(attachment_id, prompt, mask?)`, angeboten, wenn an der Nachricht ein Bild hängt und ein Modell mit `can_edit_images` nutzbar ist; OpenAI `POST /images/edits` (GPT Image, `images[]`, `mask`).
  - Tests in `tests/test_image_generation.py` (respx mit Beispiel-Payloads der Doku, Fake-Adapter), Browserlauf mit Firefox 140 headless (Werkzeug, Hinweis, Modus „Bild“, Lightbox, hell/dunkel, 375 px).
- **M9-02** `edit_image` über OpenAI: Bereich auf einer Zeichenfläche markieren, Maske mitsenden, außerdem Varianten. Vorher in der aktuellen API-Dokumentation prüfen, welches Modell das kann.
- **M9-03** MCP-Server `mcp_imagetools` (Pillow, `stdio`): Zuschneiden, Skalieren, Drehen, Umwandeln, Füllen, Text, Collage. Er arbeitet nur im Arbeitsordner des jeweiligen Nutzers.
- **M9-04** Geschützte Auslieferung der Medien nur nach Besitzprüfung, mit nginx über `X-Accel-Redirect`, ohne nginx über `FileResponse`.
- **M9-05** Tests: jede Pillow-Funktion, kein Zugriff außerhalb des Arbeitsordners, das Original bleibt erhalten.

### M10 – Sprache
*Abhängig von: TLS (aus M12 vorgezogen), Frage 2, Frage 4b.*

- **M10-01** Aufnahme mit `MediaRecorder`, Spracherkennung über ein `stt`-Modell, der Text landet zum Korrigieren im Eingabefeld.
- **M10-02** Vorlesen über ein `tts`-Modell, die Audiodatei wird als `Attachment` gespeichert. Option "automatisch vorlesen" je Nutzer.
- **M10-03** Konfigurierbare Grenzen für Aufnahmelänge und Dateigröße.

### M11 – Musik
*Umfang offen (Plan, offene Frage 6).* Arbeitspakete folgen, sobald geklärt ist, was der Meilenstein leisten soll.

### M12 – Betrieb
- **M12-01** (vorgezogen, siehe Abschnitt 1 Netzwerk: nginx im Paket ist Pflicht) `deploy/nginx.conf.example` als Baustein für den bestehenden nginx: `server`-/`location`-Block mit TLS, `proxy_buffering off` für den Stream-Endpunkt und `X-Accel-Redirect` für die Medien. Dazu `SECURE_COOKIES=True` und `AXES_PROXY_COUNT=1`. **Vor M10 umsetzen.**
  **Umgesetzt** (Commit `d9c76a9`) als nginx-Site im Paket statt als Beispieldatei: `deploy/nginx/multi-gpt` → `/etc/nginx/sites-available/multi-gpt` (conffile), rechnerspezifische Teile erzeugt das postinst unter `/etc/multi-gpt/nginx/`. Siehe Abschnitt 7.
- **M12-02** `make backup`: `pg_dump`, Medienordner und `/etc/multi-gpt/.env` als datiertes Archiv.
- **M12-03** README mit Installationsanleitung (Paket, PostgreSQL, `/etc/multi-gpt/.env`, nginx).
- **M12-04** Prüfen, ob das Paket sauber aktualisiert und entfernt wird: `apt install` über eine ältere Version, `apt remove` und `apt purge`.

### M13 – Scratchpad
*Abhängig von: M7-13 (Zitieren), M8 (Webquellen), M4a (Werkzeugergebnisse), Anhänge, M5-06 (Teilen), Projekte (M5-07, Modell `Project`; Projektfreigabe noch offen). Fragen 7a–7l. Neu eingeplant am 2026-10-10 (Nutzerwunsch).*

Im Scratchpad sammelt der Nutzer Material aus sehr unterschiedlichen Quellen. Daraus entsteht ein belegtes Gesamtdokument. Jeder Eintrag trägt seine Herkunft und eine strukturierte Zitierangabe. Das Modell schreibt nur aus den Einträgen. Kurzbelege und Literaturverzeichnis setzt der Server. Plan 8h beschreibt den Ablauf, Plan 6 das Datenmodell.

**Festlegungen:**

- **Eintrag (`ScratchItem`)** = ein Stück Text bzw. ein Bild mit drei getrennten Angaben:
  - **Herkunft** (`origin`): `answer` (markierter Text einer Modellantwort oder Nutzernachricht), `document` (Abschnitt bzw. Auswahl aus einem eigenen Dokument), `web` (Webquelle), `note` (eigene Notiz), `image` (Bild bzw. Anhang), `tool` (Ergebnis eines MCP-Werkzeugs), `upload` (eigene Datei), `paste` (Zwischenablage).
  - **Textart** (`text_kind`): `quote` (wörtlich, wird geprüft), `paraphrase` (sinngemäß, Fundstelle bleibt, Beleg mit „vgl.“ je Stil) und `own` (eigene Formulierung, kein Beleg nötig). Hinzu kommt `ai` für KI-Formulierungen, die als KI-Antwort zitiert werden.
  - **Quelle** (`source` → `ScratchSource`, leer bei eigener Notiz) mit Fundstelle am Eintrag (Seite, Absatz, Abschnitt wie `SourceRef`, dazu freies `locator_text`, z. B. „Folie 4“ oder „Min. 12:30“).
  
  Dazu kommen Tags, Reihenfolge, Gliederungsabschnitt und Kommentar. `provenance` (JSON) hält die technischen Verweise fest: Chat, Nachricht, Modell, `SourceRef`-Nummer [n], Chunk, Werkzeug und Server, URL, Erfassungszeit und Auswahl-Offsets. `snapshot` hält den Originalkontext zur Zeit der Erfassung (Chunk-Text, Nachrichtentext, Seitenausschnitt; gekürzt). Er dient der Zitatprüfung und bleibt erhalten, auch wenn die Quelle gelöscht wird.
- **Quelle (`ScratchSource`)** = ein zitierbares Werk, je Scratchpad genau einmal. Dedup über `fingerprint`, gebildet in dieser Reihenfolge: DOI → ISBN → Normnummer + Ausgabe → Dokument-ID + Angabenstand → normalisierte URL → KI: Chat + Modell + Datum → manuell: Hash der Angaben. Dazu ein stabiler, lesbarer Schlüssel `key` (BibTeX-artig `mueller2024`, `din9001_2015`; bei Kollision `a`, `b`, …). Der Schlüssel ändert sich nie, auch nicht beim Bearbeiten der Angaben. `csl` hält die Angaben als `citations.Reference.to_dict()`.
- **Zitieren verallgemeinert** (M13-03): `citations.Reference` bleibt die eine Zitier-Repräsentation, CSL-JSON-ähnlich, aber eigener Code (keine citeproc-Abhängigkeit, Begründung im Modulkopf). Erweiterungen:
  - neue Arten `ai` (generative KI), `personal` (persönliche Mitteilung bzw. Notiz) und `dataset` (Werkzeug- bzw. Datenergebnis)
  - neue Felder `prompt`, `model`, `medium` (z. B. „Generative-KI-Chat“) und `note`
  - `to_csl()` bzw. `from_csl()` für echten CSL-JSON-Export
  
  Neu sind `Cite` (Referenz + `Locator` + `mode` quote/paraphrase) und `Bibliography`. `Bibliography` übernimmt Dedup, Schlüssel, Sortierung je Stil, Jahresbuchstaben „2024a/b“ (APA, Harvard, Chicago) und „ebd.“. `SourceRef.biblio` und die vorhandenen Funktionen bleiben kompatibel.
- **KI-Ausgaben zitieren** je Stil:
  - **APA 7** (APA Style Blog, Sept. 2025): Autor ist das Unternehmen (`AIModel.publisher`, z. B. OpenAI), Titel ist der Chattitel, dazu „[Generative AI chat]“, das Modell und der Link. Ein interner Intranet-Link ist für Leser nicht abrufbar und entfällt deshalb. Kurzbeleg „(OpenAI, 2026)“.
  - **MLA 9:** Der Prompt bzw. seine Beschreibung ist der Titel, das Werkzeug der Container, das Modell die Version, dazu Unternehmen und Datum. KI nie als Autor. Kurzbeleg mit gekürztem Prompt.
  - **Chicago:** Nur im Text bzw. in der Anmerkung („Text erzeugt von GPT-4o, OpenAI, 7. März 2026“), nicht im Literaturverzeichnis.
  - **DIN ISO 690 und Harvard** haben keine eigene Regel. Sie werden analog zu APA gesetzt, als eigene Konvention gekennzeichnet.
  
  Optional gibt es den Abschnitt „Verwendete KI-Werkzeuge“ (Frage 7d).
- **„ebd.“**: Steht derselbe Beleg direkt vor dem nächsten im selben Abschnitt und ohne anderen Beleg dazwischen, setzt DIN „(ebd., S. 13)“. APA, MLA, Harvard und Chicago (CMOS 18 rät von „ibid.“ ab) wiederholen den Kurzbeleg. Je Scratchpad abschaltbar (Frage 7e).
- **Paraphrasen:** Der Eintrag erbt die Fundstelle der markierten Stelle. Der Kurzbeleg trägt sie immer, bei DIN und Harvard mit „vgl.“, bei APA, MLA und Chicago ohne Zusatz. Eine Paraphrase über mehrere Stellen speichert den Bereich (`page`–`page_end`).

- **M13-01** **Kontextmenü im Chat**, vor dem eigentlichen Scratchpad lieferbar (Nutzerwunsch). Es zielt auf markierten Text in Antworten, Nutzernachrichten, Quellenliste, Abschnittsansicht (`collections/chunk.html`) und Vergleichsspalten. Über den Knopf „⋯“ an jeder Nachricht gilt es für die ganze Nachricht.
  - **Öffnen:**
    - Rechtsklick bzw. langes Tippen
    - schwebende Leiste bei Textauswahl (`selectionchange`, unterhalb der Auswahl, damit sie das native Auswahlmenü am Handy nicht verdeckt)
    - Tastatur: Kontextmenü-Taste bzw. Umschalt+F10 (lösen beide `contextmenu` aus)
  - **Barrierearm:** Muster „Menu“ der WAI-ARIA APG: `role="menu"`/`menuitem`, wandernder Fokus mit Pfeiltasten, Untermenüs mit →/←, Anfangsbuchstabe springt, Esc schließt und gibt den Fokus zurück. Abgefangen wird nur in Nachrichteninhalten, nie auf Links, Eingabefeldern und Code-Kopierknöpfen.
  - **Natives Menü:** Bei Umschalt+Rechtsklick (`event.shiftKey`) gibt es kein `preventDefault`. Firefox zeigt dann ohnehin das native Menü. Das eigene Menü enthält „Browsermenü: Umschalt+Rechtsklick“ als Hinweiszeile. In `/einstellungen/` lässt sich das eigene Menü abschalten (Frage 7j).
  - **Einträge:**
    - „Ins Scratchpad“ mit „als Zitat“, „als Notiz“ und „in Abschnitt …“. Sichtbar erst ab M13-05; Herkunft und Fundstelle werden automatisch mitgenommen.
    - „Im Eingabefeld zitieren“ (Markdown `> `, Cursor dahinter)
    - „Nachfragen …“ (Zitat und Platzhalter „Frage dazu …“)
    - Schnellaktionen „Erklären“, „Vereinfachen“, „Übersetzen ▸ (Englisch, Deutsch, …)“, „Belege suchen“ (schaltet für diese Nachricht die Websuche bzw. die Dokumentsuche ein) und „Gegenargumente“
    - eigene Vorlagen (M13-02)
    - „Kopieren“, „Kopieren mit Quellenangabe“ (Stil des Kontos über `citations.short`/`entry`)
    - „Im Web suchen“, „In Dokumenten suchen“ (füllt das Eingabefeld und setzt den Schalter)
  - **Herkunft der Auswahl** (`static/chat/selection.js`):
    - **Nachricht:** Die nächste `[data-message-id]` liefert Nachricht und Modell. Steht in der Auswahl [n], kommen diese Quellen mit.
    - **Quellenliste:** `data-citations` liefert Referenz und Fundstelle.
    - **Abschnittsansicht:** Chunk-Anker plus Absatzzählung innerhalb des Chunks ergeben die Fundstelle; die Logik `doc_tools._units` wird wiederverwendet.
  - **Rechte:** Schnellaktionen und „Nachfragen“ im selben Chat brauchen W. Bei nur lesendem Zugriff landen sie in einem neuen eigenen Chat mit dem Zitat. „Kopieren“ und „Ins Scratchpad“ sind mit R erlaubt (Frage 7c).
  - *Tests:* Browserlauf (Maus, Umschalt+F10, Esc, Fokusrückgabe), Umschalt+Rechtsklick ruft kein `preventDefault` auf, Links und Eingabefelder bleiben unberührt, Handy-Breite 375 px, Auswahl über Formeln und Code ergibt Quelltext, keine Aktion in fremden Chats ohne Recht (serverseitig).
- **M13-02** **Prompt-Vorlagen** (Prompt-Bausteine), mit M13-01 lieferbar. Das vorhandene, bisher nur im Admin sichtbare `Preset` wird erweitert statt neu gebaut:
  - `system_prompt` → `text`
  - neu: `kind` (`system` = System-Prompt-Vorlage, `selection` = Auswahl-Aktion), `project` (FK, leer = persönlich), `web_search`, `document_search`, `send_now` (Standard aus: nur ins Eingabefeld), `sort_order`, `active`
  - Unique (`user`, `project`, `name`)
  
  Platzhalter: `{auswahl}`, `{quelle}` (Kurzbeleg der Auswahl), `{{`/`}}` für geschweifte Klammern. Fehlt `{auswahl}`, wird die Auswahl als Zitat vorangestellt. Höchstens 4 000 Zeichen. Die eingebauten Schnellaktionen stehen im Code (`chat/quick_actions.py`) und lassen sich je Konto ausblenden. Verwaltung unter `/einstellungen/vorlagen/`. Projektvorlagen sehen alle mit R am Projekt, bearbeiten darf nur, wer U hat. *Tests:* Platzhalter-Ersetzung samt Escapes, keine fremden Vorlagen, Projektrechte, Migration bestehender `Preset`-Zeilen.
- **M13-03** **Zitieren verallgemeinern** in `chat/citations.py` (Festlegungen oben):
  - neue Arten, Felder und `Cite`/`Bibliography`
  - `AIModel.publisher` (Unternehmen, vorbelegt aus Modell-ID bzw. Anbieterart: `openai/…` → OpenAI, `anthropic` → Anthropic, `google` → Google; LM Studio: Herausgeber des Modells, sonst leer)
  - Adapterfunktionen `reference_from_message`, `reference_from_tool_call`, `reference_from_attachment` und `manual_reference`; DOI-Abfrage über `rag/crossref.py`, wenn `crossref_enabled`
  - Export als BibTeX und CSL-JSON
  
  *Tests:* je Stil und Art ein Golden-Test (auch KI, persönliche Mitteilung, Norm, Webseite mit Abrufdatum), „vgl.“ und „ebd.“, Jahresbuchstaben, gleicher Fingerprint ergibt denselben Schlüssel, Rückwärtskompatibilität von `SourceRef.biblio`.
- **M13-04** **Datenmodell** in der neuen App `multigpt.scratchpad`: `Scratchpad`, `ScratchSource`, `ScratchItem`, `OutlineSection`, `ScratchDraft`, `DraftSection` (Felder in Plan 6). Aktionen in `can()` (`VIEW_SCRATCHPAD`, `EDIT_SCRATCHPAD`, …) und Rollenrecht `can_scratchpad`. Admin nur mit Metadaten (`MetadataOnlyAdmin`). Grenzen: `SCRATCH_MAX_ITEMS` (2 000 je Scratchpad), 20 000 Zeichen je Eintrag, Snapshot 8 000 Zeichen. *Tests:* Constraints (ein Scratchpad je Projekt, ein persönliches je Konto), Zugriff A/B, Admin zeigt keinen Inhalt.
- **M13-05** **Erfassen**: `POST /api/scratchpad/<id>/items/` mit `origin` und Verweis (Nachricht + Auswahl, `SourceRef`, Chunk + Auswahl, URL, Attachment, ToolCall, Text). Der Server baut daraus Quelle, Fundstelle, `snapshot` und `verified`; Angaben aus dem Browser werden nicht übernommen. Je Herkunft:
  - **Webquelle:** Titel, URL und Abrufdatum aus dem `SourceRef`. Ein Ausschnitt entsteht nur auf Wunsch, über den vorhandenen SSRF-geschützten Abruf.
  - **Bild:** wird über die Anhang-Pipeline neu kodiert (keine Metadaten).
  - **Werkzeugergebnis:** Text gekürzt, Werkzeug, Server und Zeit als Quelle der Art `dataset`. Von Werkzeugen genannte URLs werden nicht automatisch abgerufen.
  - **Upload:** wie Anhänge (Typprüfung am Inhalt, Extraktion).
  
  Im Kontextmenü (M13-01) und am Knopf „Ins Scratchpad“ an Antworten, Quellen, Dokumentabschnitten und Webquellen. *Tests:* jede Herkunft, wörtliches Zitat aus einem Chunk gilt als `verified`, eine manipulierte Auswahl nicht, fremde Nachricht bzw. fremder Chunk → 404, EXIF fällt weg.
- **M13-06** **Oberfläche**:
  - **Seitenpanel** im Chat (umschaltbar, zeigt das Scratchpad des Projekts bzw. das persönliche) und **eigene Seite** `/scratchpad/` bzw. `/projekte/<id>/scratchpad/`
  - Spalten: Einträge (Filter nach Herkunft, Textart, Tag, Abschnitt; Volltextsuche), Gliederung, Entwurf
  - Ziehen und Ablegen. Mit der Tastatur: Alt+↑/↓ sortiert, Alt+→ ordnet zu.
  - Eintrag bearbeiten: Textart, Fundstelle, Angaben der Quelle (mit Vorschau aller Stile wie M7-13), Tags
  - Mobil: eine Spalte mit Reitern, Panel als Vollbild-Blatt
  
  *Tests:* Browserlauf hell/dunkel, 375 px ohne waagerechtes Scrollen, Sortieren per Tastatur.
- **M13-07** **Gliederung**: Abschnitte von Hand (verschachtelt, nummeriert) oder „Gliederung vorschlagen“. Dabei liefert ein Modell aus Titeln, Tags und Anfängen der Einträge eine JSON-Gliederung, der Nutzer übernimmt sie oder ändert sie. „Zuordnung vorschlagen“ arbeitet lokal über Embeddings (nomic, wie RAG), ohne Text nach außen. Der Nutzer bestätigt jede Zuordnung. *Tests:* ungültiges JSON → Fehlermeldung ohne Änderung, Vorschlag ändert nichts ohne Bestätigung.
- **M13-08** **Abschnitte erzeugen und überarbeiten**: je Abschnitt ein Anbieteraufruf (SSE wie im Chat, Budget- und Modellprüfung für den Auslöser, keine Werkzeuge). Auch hier stehen die Grundregeln (`ChatSettings.base_instructions`, M8-08) als erster Teil im System-Prompt.
  - **Kontext:**
    - fester Prompt in `scratchpad/prompts.py`: nur aus den Einträgen schreiben, keine neuen Fakten; jede Sachaussage mit [n]; wörtliche Zitate nur aus `quote`-Einträgen und unverändert in „…“; eigene Notizen [Nk] ohne Beleg nutzbar; Lücken als `[FEHLT: …]`
    - die Einträge als `<quellmaterial>` (`sources.defuse`) mit Nummer, Textart und Kurzbeleg
    - Ziel des Abschnitts und Kurzfassungen der Nachbarabschnitte
  - **Speichern:** Der Server bildet die lokalen [n] auf Eintrags-IDs ab (`[@item:123]`) und speichert eine neue `DraftSection`-Version (Modell, Kosten, Prüfergebnis).
  - **Überarbeiten:** mit Anweisung („kürzer“, „formeller“) oder von Hand; beides ergibt eine neue Version. Versionen umschalten wie „‹ i/n ›“.
  
  *Tests:* Abbildung [n] → Eintrag, unbekannte Nummer → Fehlbeleg, Einträge anderer Scratchpads gelangen nie in den Kontext, Abbruch speichert den Teiltext.
- **M13-09** **Belegprüfung** (`scratchpad/verify.py`), ohne Modell:
  - **Fehlbelege:** [n] ohne Eintrag oder auf einen Eintrag eines anderen Abschnitts (Warnung)
  - **Zitatprüfung:** Text in „…“/"…" bzw. `>` mit Beleg muss nach Normalisierung (Leerraum, typografische Zeichen, Silbentrennung, „[…]“ teilt in Stücke) im Eintragstext stehen, und der Eintrag muss `verified` sein
  - **unbelegte Sätze:** Sätze ohne Beleg, außer reinen Überleitungen und eigenen Notizen
  - **`[FEHLT]`-Stellen**
  
  Anzeige als Markierung im Entwurf und als Liste. Optional „Belege mit KI prüfen“: ein zweiter Aufruf beurteilt je Satz, ob der Eintrag ihn stützt (Frage 7i). *Tests:* Golden-Tests für jede Prüfregel, Auslassung „[…]“, Zitat aus einer Paraphrase gilt als Fehler.
- **M13-10** **Satz**: Der Server setzt die Marker beim Anzeigen und Exportieren in Kurzbelege des Stils (Scratchpad → Projekt → Konto) um. Zitate werden mit Fundstelle gesetzt, Paraphrasen mit „vgl.“ je Stil, dazu „ebd.“ je Stil. Es folgt das Literaturverzeichnis aller verwendeten Quellen (dedupliziert, sortiert je Stil, Chicago ohne KI). Optional kommen „Verwendete KI-Werkzeuge“ und der Hinweis „Mit KI-Unterstützung erstellt (Modelle …)“ dazu. *Tests:* Stilwechsel ändert nur den Satz, nicht den gespeicherten Text; eine Quelle erscheint nur einmal; Reihenfolge stabil.
- **M13-11** **Export**:
  - **Markdown:** gesetzt, oder für Pandoc mit `[@key, S. 12]` plus `.bib`/CSL-JSON als ZIP
  - **DOCX:** über `python-docx` (bereits Abhängigkeit, MIT). Überschriften, Absätze, Blockzitate, Listen, einfache Tabellen, Bilder, Literaturverzeichnis mit hängendem Einzug. Keine Fußnoten, die kann python-docx nicht nativ (Frage 7g).
  - **PDF:** HTML aus einem Django-Template mit Druck-CSS, gesetzt mit **WeasyPrint** (BSD-3, pip im venv; Debian-Paket `weasyprint` 62.3 in trixie). Neue `Depends`: `libpango-1.0-0`, `libpangoft2-1.0-0`, `fonts-dejavu-core`. Ein eigener `url_fetcher` erlaubt nur eigene Anhänge (kein Netzzugriff, SSRF).
  - **Markdown-Parser serverseitig:** `markdown-it-py` (MIT, auch als `python3-markdown-it` in Debian), Formeln als Quelltext. Pandoc (Debian 3.1.11, GPL-2+, als eigener Prozess lizenzrechtlich unkritisch) bleibt ein optionaler späterer Weg (`Suggests`), weil PDF darüber LaTeX oder ohnehin WeasyPrint bräuchte (Frage 7f).
  
  *Tests:* Export je Format mit Zitaten, Bild und Verzeichnis (DOCX mit python-docx zurückgelesen, PDF-Text mit pypdf). Externe Bild-URL wird nicht abgerufen. Dateiname ohne Nutzereingaben.
- **M13-12** **Projekte und Teilen**:
  - Je Projekt genau ein Scratchpad, dazu genau eines je Konto ohne Projekt („Mein Scratchpad“). Einträge lassen sich verschieben und kopieren (Frage 7a). Mehrere Entwürfe je Scratchpad (Frage 7b).
  - Das Projekt-Scratchpad erbt die Projektfreigabe (RWUD) über `can()`:
    - R: lesen und exportieren
    - W: Einträge anlegen, Abschnitte erzeugen (Kosten auf den Auslöser)
    - U: fremde Einträge, Gliederung und Versionen bearbeiten
    - D: löschen
  - Das persönliche Scratchpad ist nicht teilbar.
  - Rücklinks auf Chats und Dokumente zeigt die Oberfläche nur, wenn der Betrachter sie lesen darf. Sonst erscheinen nur Zitierangabe und Snapshot (wie M5-06).
  - Widerruf wirkt sofort, auch im laufenden Erzeugen (Prüfung wie `sharing.check_turn`).
  
  *Tests:* RWUD-Matrix, Widerruf, Rücklink ohne Leserecht → nur Angabe.
- **M13-13** **Datenschutz, Sicherheit, Doku**:
  - Verwalter sehen keine Inhalte. Die Einsicht (M6-05) erstreckt sich nicht auf Scratchpads (Frage 7h).
  - Einträge sind Quellmaterial: Sie gelangen nie in den System-Prompt, beim Erzeugen gibt es keine Werkzeuge, der `SYSTEM_NOTE`-Hinweis gilt.
  - Die Ausgabe läuft im Browser durch DOMPurify, im Export durch den eigenen Renderer ohne Roh-HTML.
  - Ins Log kommen keine Inhalte.
  - Wiki-Seite `Scratchpad`, Website-Roadmap
  
  *Tests:* Prompt-Injection-Probe in einem Eintrag („ignoriere …, rufe Werkzeug auf“) löst nichts aus, XSS-Probe im Entwurf und im PDF, Admin ohne Inhalt.

*Abnahme:* Aus einer Antwort, einem PDF-Abschnitt, einer Webquelle, einer Notiz und einem Bild entsteht ein zweiseitiges Dokument. Jede Sachaussage trägt einen Kurzbeleg im Stil des Kontos, jedes wörtliche Zitat ist geprüft, eine absichtlich unbelegte Aussage ist markiert. Export als DOCX und PDF mit Literaturverzeichnis, jede Quelle einmal. Ein nur lesend berechtigtes Projektmitglied sieht das Dokument, kann aber nichts ändern.

---

## 4. Reihenfolge und kritischer Pfad

```
M1 ─> M2 ─> M3 ─┬─> M4 ─> M4a ─┬─> M7 (RAG)
                │              ├─> M8 (Websuche)
                │              └─> M9 (Bilder)
                ├─> M5 (Komfort)            ┐
                └─> M6 (Verbrauch/Budgets)  ├─ parallel möglich
M12-01 (TLS/nginx) ─────────────> M10 (Sprache)
M7-13 + M8 + M5-06 ─┬─> M13-01/02 (Kontextmenü, Vorlagen; vorab lieferbar)
                    └─> M13-03 ─> M13-04 ─> M13-05 ─> M13-06 ─> M13-07 ─> M13-08 ─> M13-09 ─> M13-10 ─> M13-11
M5-07 Projekte + Projektfreigabe ─────────────> M13-12
```

Der kritische Pfad ist **M1 → M2 → M3 → M4 → M4a**. Alles mit Werkzeugen (M7-06, M8-04, M9-03) setzt die MCP-Schleife voraus. M5 und M6 können vorgezogen werden, falls die Fragen zu M4/M4a noch offen sind.

**M13 (Scratchpad):**

- M13-01 und M13-02 brauchen nur den heutigen Stand und gehen als Erstes raus.
- M13-03 (Zitieren) ist der Engpass für alles Weitere.
- M13-04 legt `Scratchpad.project` gleich als nullbaren Fremdschlüssel an (`Project` steht seit M5-07).
- M13-12 schließt an, wenn die Projektfreigabe steht.

---

## 5. Risiken

| Risiko | Auswirkung | Gegenmaßnahme |
|---|---|---|
| MCP-SDK ist async, die App läuft synchron | Hängende Threads, Verbindungslecks bei `stdio`-Servern, doppelt gestartete Server | Ein Loop-Thread je Prozess, Mutexe für Start und Verbindungsaufbau, Timeouts auf jedem `future.result()`, Aufräumen in `worker_exit` (M4a-01). Tests mit parallelen Aufrufen aus mehreren Threads |
| Inpainting nur bei einem Anbieter (OpenAI) | Fällt OpenAI aus oder ändert die API, fehlt Inpainting | Fähigkeit je Modell im Admin markieren, Adapter gekapselt. Ein zweiter Anbieter ist später nachrüstbar |
| Gunicorn-Threads durch lange Streams belegt | Neue Anfragen warten | Thread-Zahl über Env konfigurierbar, Auslastung in M6 beobachten |
| Anbieter-APIs ändern sich | Adapter brechen | Laut Plan vor jedem Adapter die aktuelle Dokumentation lesen, Tests mit aufgezeichneten Antworten |
| Prompt-Injection über Webinhalte, Dokumente oder Werkzeugergebnisse | Unerwünschte Werkzeugaufrufe | Rückfragepflicht wird serverseitig erzwungen und hängt nie von Modellinhalten ab (Test in M4a-07) |
| Vom Modell erzeugter Python-Code (`run_python`) | Zugriff auf Server-Dateien, Netz oder Geheimnisse, Überlast | Nur in bubblewrap ohne Netz, ohne Server-Dateien und ohne Umgebung, mit seccomp und Grenzen für CPU, Speicher, Prozesse, Dateien und Ausgabe. Ohne Sandbox kein Werkzeug (M4a-10) |
| Scratchpad: Modell erfindet Belege oder verändert Zitate (M13) | Falsche Aussagen mit scheinbarem Beleg im Gesamtdokument | Marker nur auf Einträge, Kurzbelege setzt der Server, Zitatprüfung gegen Eintrag und Snapshot, unbelegte Sätze markiert (M13-09) |
| Scratchpad: Auswahl im Browser passt nicht zum Quelltext (gerendertes Markdown, Formeln, überlappende Chunks) | Zitat gilt fälschlich als ungeprüft, Fundstelle ungenau | Auswahl serverseitig gegen Nachricht bzw. Chunk normalisiert abgleichen, bei Unsicherheit `verified=False` statt raten (M13-05) |
| Scratchpad: große Kontexte je Abschnitt | Kosten, Kontextgrenze lokaler Modelle | Je Abschnitt nur zugeordnete Einträge, Grenze je Aufruf, Hinweis vor dem Senden mit geschätzten Tokens |
| Eigenes Kontextmenü am Handy | Konflikt mit dem nativen Auswahlmenü | Schwebende Leiste statt Abfangen des langen Tippens, Abschaltbar in den Einstellungen (M13-01) |
| PDF-Export mit WeasyPrint | Neue Systembibliotheken, Netzabrufe aus HTML | `Depends` auf Pango, eigener `url_fetcher` ohne Netz (M13-11) |
| Projektfreigabe fehlt noch | `Project` steht (M5-07), Teilen eines Projekts noch nicht | M13-12 als letztes Paket; bis die Projektfreigabe steht, sieht nur der Besitzer das Projekt-Scratchpad |
| Zitierregeln für KI ändern sich (APA zuletzt Sept. 2025) | Veraltete KI-Belege | Regeln je Stil an einer Stelle in `citations.py`, Golden-Tests, Quelle im Modulkopf |

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
| 4b – Anbieter für Embedding, STT, TTS, Bild | – | **Geklärt:** Embeddings lokal über LM Studio (nomic, 768), sonst OpenAI |
| 4c – Sprache der Dokumente, OCR? | – | **Geklärt:** deutsch, mit Scans. OCR über olmOCR in LM Studio, Tesseract als Ersatz |
| 4a – SearXNG oder Such-API | – | **Geklärt:** SearXNG, Such-API später optional |
| 4e – Inpainting-Anbieter | – | **Geklärt:** OpenAI |
| 2 – Reverse Proxy, Hostname, TLS | – | **Geklärt:** nginx mit TLS kommt mit dem Paket (Commit `d9c76a9`), Hostname und Zertifikat per debconf; ohne eigenes Zertifikat snakeoil |
| 7a – Ein Scratchpad je Projekt plus ein persönliches? | M13-04, M13-12 | Empfehlung: ja, Einträge verschieb- und kopierbar |
| 7b – Mehrere Gesamtdokumente je Scratchpad? | M13-04 | Empfehlung: ja (z. B. Kurz- und Langfassung aus demselben Material) |
| 7c – Ins Scratchpad aus nur lesend geteiltem Chat? | M13-01, M13-05 | Empfehlung: ja (R umfasst Kopieren), Rücklink nur mit Leserecht; Nachfragen dann in neuem eigenen Chat |
| 7d – KI-Antworten im Literaturverzeichnis? | M13-03, M13-10 | Empfehlung: je Stil (APA/MLA ja, Chicago nur im Text, DIN/Harvard analog APA), zusätzlich „Verwendete KI-Werkzeuge“ standardmäßig an |
| 7e – „ebd.“ verwenden? | M13-03 | Empfehlung: nur DIN, abschaltbar; andere Stile wiederholen |
| 7f – PDF über WeasyPrint oder Pandoc + LaTeX? | M13-11 | Empfehlung: WeasyPrint; Pandoc später optional |
| 7g – Fußnoten-Stile (DIN mit Fußnoten, Chicago Notes)? | M13-11 | Empfehlung: v1 nur Belege im Text, Fußnoten später |
| 7h – Einsicht in Jugendlichen-Konten auch für Scratchpads? | M13-13 | Empfehlung: nein, nur Chats |
| 7i – KI-Belegprüfung standardmäßig? | M13-09 | Empfehlung: aus, per Knopf, lokales Modell bevorzugt |
| 7j – Eigenes Kontextmenü standardmäßig an? | M13-01 | Empfehlung: ja, abschaltbar, Umschalt+Rechtsklick öffnet das Browsermenü |
| 7k – Prompt-Vorlagen je Projekt teilen? | M13-02 | Empfehlung: ja über die Projektfreigabe (R nutzen, U bearbeiten) |
| 7l – Crossref für manuell angelegte Quellen? | M13-03 | Empfehlung: ja, am vorhandenen Schalter `crossref_enabled` |

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
- **M3 umgesetzt** (Commit `e6c031e`, 297 Tests grün):
  - Adapter `openai_compat`, gebaut nach der aktuellen Dokumentation von OpenAI, OpenRouter und LM Studio.
  - SSE-Endpunkt mit Rechteprüfung vor dem ersten Byte. Abbruch speichert den Teiltext als `aborted`.
  - Chatansicht, Ende-zu-Ende in headless Firefox getestet.
  - Festlegungen:
    - Eine laufende Antwort steht vorläufig als `aborted` in der DB und wird am Ende auf `complete` oder `error` gesetzt.
    - Abgebrochene Antworten gehen in den Verlauf an das Modell ein, fehlerhafte nicht.
    - „Neu erzeugen“ setzt die alte Antwort auf `superseded`.
    - Der Titel kommt vorläufig aus der ersten Zeile der Nachricht.
  - Offen für die Abnahme: eine gestreamte Antwort mit echtem API-Key.
  - Bekannte Grenze: Einen Abbruch bemerkt der Server erst beim nächsten Textstück, bei einem hängenden Anbieter also erst nach dem Lese-Timeout von 300 s.
- **M4 und M5 umgesetzt, Backend von M4a fertig** (Commit `ceb8a35`, 548 Tests grün):
  - Adapter `anthropic` und `google`, `make sync-models` (legt neue Modelle inaktiv an).
  - LM-Studio-Status mit Anzeige.
  - Markdown mit lokal eingebundenen Bibliotheken: marked 18.0.14, DOMPurify 3.4.16, highlight.js 11.12.0, KaTeX 0.19.0 (Formeln, nachgezogen).
  - Umbenennen, Archiv, Löschen, Suche, Export, System-Prompt je Chat.
  - Werkzeuge in allen drei Adaptern (`provider_state` für Denk-Signaturen).
  - MCP-Brücke und MCP-Client auf dem SDK `mcp` 2.3. Zugangsdaten sind ein JSON-Objekt mit `env`, `headers` und `bearer_token`.
  - Offen für die Abnahme: echte Aufrufe mit Keys für Anthropic, Gemini und OpenRouter sowie LM Studio im LAN (Frage 1a).
- **M4a umgesetzt** (Commit `cc3788d`, 623 Tests grün):
  - Werkzeugschleife mit höchstens 10 Anbieteraufrufen, der letzte mit `tool_choice="none"`.
  - Werkzeugnamen haben die Form `<Server>__<Werkzeug>`.
  - Rückfrage pausiert die ganze Runde (`awaiting_confirmation`). Zwischenrunden und `provider_state` liegen in `Message.tool_state`.
  - `can()` wird vor jedem Aufruf erneut geprüft. Werkzeugergebnisse heben die Rückfrage nicht auf.
  - Anzeige im Chat (Browserlauf ohne JS-Fehler, XSS-Probe).
  - Offen: Bilder aus Werkzeugergebnissen gehen nur als Hinweis an das Modell, ausgeliefert werden Anhänge erst in M9. Der Export enthält noch keine Werkzeugaufrufe.
- **Anbieter-Admin** (nach Rückmeldung des Nutzers):
  - „Verbindung jetzt prüfen“ mit konkreter Fehlerursache (`Provider.last_error`), auch im Chat-Status sichtbar.
  - Prüfung beim Speichern.
  - Seite „Modelle auswählen“ mit Live-Liste des Anbieters.
- **Datenbank-Kodierung:** Der Entwicklungs-Cluster ist SQL_ASCII. preinst und `make db-create` legen die MultiGPT-Datenbanken deshalb ausdrücklich als UTF-8 aus `template0` an. Die Test-Datenbanken nutzen die UTF-8-Vorlage `multigpt_template` (`DB_TEST_TEMPLATE`).
- **M5-05 umgesetzt** (Commit `5545ac8`, 656 Tests grün, Browserlauf ohne JS-Fehler):
  - Bearbeiten mit Inline-Editor, Versionen „‹ i/n ›“, Kopierknopf, „Neu erzeugen“ an jeder Antwort.
  - Datenmigration `chat.0010` wandelt bestehende Chats in Ketten um.
  - Der Titel bleibt beim Bearbeiten unverändert. Umschalten ändert die Reihenfolge in der Seitenleiste nicht.
- **M8 (Websuche) vorgezogen und in Arbeit** (Nutzerwunsch): Agenten websearch, webui und wiki.
- **M6, M7 und M8 umgesetzt** (Commit `b7e9a00`, 1165 Tests grün). Damit auch M7-08 (RAG-Verwaltung), M7-09 (lokal: nomic und olmOCR; OCR-Test gegen das echte LM Studio erfolgreich) und M7-10 (Verzeichnisquellen).
- Ehemals „in Arbeit“, Zuständigkeit bei M6:
  - **budget:** Verbrauch, Monatsbudget mit Warnung ab 80 % und Sperre ab 100 %, Seite „Mein Verbrauch“.
  - **family:** Seite „Familie“ mit Einsicht in Jugendlichen-Chats nur lesend und nur mit Option.
  - **compare:** Vergleichsmodus. Die Antworten der Modelle sind Geschwister im Chat-Baum, nebeneinander angezeigt, „Mit dieser Antwort weiter“ setzt den Zweig.
- **M7 (RAG) in Arbeit**, parallel zu M8 (retrieval, ingest und ragui sind fertig, ragadmin baut die RAG-Verwaltung im Admin):
  - **retrieval:** `Chunk` mit HNSW- und Volltextindex, `RagSettings`, `embed()`, Suche mit Zugriffsfilter in SQL, Chat-Einbindung, Werkzeug `search_documents`.
  - **ingest:** Extraktion, OCR mit Tesseract, Zerteilung, Worker mit `SKIP LOCKED`, `multi-gpt-worker.service`.
  - **ragui:** Seiten für Sammlungen, Upload, Teilen, Abschnittsansicht, Auswahl im Chat.
- **M7 umgesetzt** (Commit folgt):
  - **retrieval:** `Chunk` (Vektor mit HNSW-Index, Kosinus; Volltext als von PostgreSQL berechnete Spalte mit GIN-Index), `RagSettings`, Embeddings mit klaren Fehlermeldungen, Suche mit Zugriffsfilter in SQL und optionaler Zusammenführung mit der Volltextsuche (RRF), fester Ablauf im Chat und Werkzeug `search_documents`, `SourceRef` mit Abschnitt und Seite, `make reindex`.
  - **ingest:** Upload mit Typprüfung am Inhalt (PDF, DOCX, TXT, MD) und Größengrenze `DOCUMENT_MAX_UPLOAD_MB`, Extraktion, OCR mit Tesseract (`OCR_LANGUAGES`), Zerteilung mit Seitenzahl, Job-Tabelle mit `SKIP LOCKED`, Backoff bis `JOB_MAX_ATTEMPTS`, Lebenszeichen und Neueinreihen hängender Jobs, `make worker` und `multi-gpt-worker.service`. Das Paket hängt von `poppler-utils`, `tesseract-ocr`, `tesseract-ocr-deu` und `tesseract-ocr-eng` ab.
  - **ragui:** Seiten „Sammlungen“ mit Upload, Status, Teilen, Download und Abschnittsansicht; Auswahl „Dokumente“ im Eingabefeld; Quellen mit Seitenzahl unter der Antwort.
  - **ragadmin:** App `multigpt.rag` mit Admin-Abschnitt „Dokumente (RAG)“: RAG-Übersicht mit Warnung „Worker läuft nicht?“, „Embedding testen“, „Alles neu indexieren“, „Fehlgeschlagene erneut versuchen“, „Hängende Aufträge zurücksetzen“, Listen für Sammlungen, Dokumente und Indexierungsaufträge, nur Metadaten.
  - **In Arbeit:** M7-09 (localrag: Embeddings und OCR lokal über LM Studio) und M7-10 (crawler: Verzeichnisquellen).
  - Wiki-Seiten `RAG`, `RAG-Einrichtung` und `RAG-Verzeichnisquellen`.
- **LM Studio im Heimnetz läuft echt** (2026-10-09, `openai/gpt-oss-20b`).
- **Vorschlag „Gedächtnis über Chats“** (Plan 8, Punkt 19): Die Freigabe durch den Nutzer steht aus.
- **Damit sind M2–M5 abgeschlossen.** Für die Abnahme offen: echte Anbieter und LM Studio im Heimnetz, Installation des Pakets auf Debian 13.
- Geklärt sind die Fragen 1, 3, 4, 4a–4c, 4e, 5, 5a und 5b, Frage 2 inzwischen ebenfalls (nginx im Paket). Offen sind noch 1a (beantwortet in der Praxis: LM Studio unter 192.168.24.127) und 4d. Die Datenmodell-Lücken aus Abschnitt 2 sind entschieden.
- **M12-01 umgesetzt** (Commit `d9c76a9`, 1184 Tests grün, `make test-packaging` ohne Fehlschlag):
  - gunicorn lauscht im Paket fest auf `127.0.0.1` (Port aus `MULTI_GPT_BIND` bleibt, eine LAN-Bindung wird beim Upgrade umgestellt). `Depends: nginx (>= 1.25.1), ssl-cert`.
  - debconf fragt Hostname(n) und Zertifikat/Schlüssel ab (die Fragen nach Bind-Adresse, `ALLOWED_HOSTS` und CSRF-Origins entfallen). `ALLOWED_HOSTS` und `CSRF_TRUSTED_ORIGINS` werden aus den Namen und den IPv4-Adressen des Rechners abgeleitet.
  - Site `/etc/nginx/sites-available/multi-gpt` als conffile; `upstream.conf`, `http.conf`, `https.conf` und `headers.conf` erzeugt das postinst unter `/etc/multi-gpt/nginx/`, eigene Ergänzungen in `/etc/multi-gpt/nginx/local/*.conf`.
  - TLS nach Mozilla „intermediate“, HTTP/2, 301 auf HTTPS, SSE ungepuffert, `client_max_body_size` = `DOCUMENT_MAX_UPLOAD_MB` + 10 MB, statische Dateien direkt. Ohne eigenes Zertifikat snakeoil mit Warnung, HSTS nur mit eigenem Zertifikat.
  - Default-Server, außer eine andere Site ist es schon; Debians unveränderte Site `default` wird nur dann abgeschaltet und bei `remove`/`purge` wiederhergestellt. `nginx -t` vor dem Aktivieren, bei Fehler Rücknahme ohne Abbruch (`*.failed`).
  - `SECURE_COOKIES=True` und `AXES_PROXY_COUNT=1` bei der ersten Einrichtung; axes nutzt die eigene Client-IP-Funktion `multigpt.accounts.client_ip` (Setting `REVERSE_PROXY_COUNT`).
  - `MULTI_GPT_SKIP_NGINX=1` für Docker, `MULTI_GPT_FORWARDED_ALLOW_IPS` für gunicorn.
  - Doku: Wiki-Seite `nginx-und-TLS`, Konfiguration, Website (Installation, Architektur, Roadmap).
  - Offen: `USE_X_ACCEL_REDIRECT` ist vorbereitet, aber `www-data` darf `MEDIA_ROOT` (`0750 multi-gpt:multi-gpt`) nicht lesen. Testinstallation auf Debian 13.
