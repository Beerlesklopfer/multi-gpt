# PLAN.md – MultiGPT (lokales Multi-KI-Chatsystem)

Planungsdokument für Claude Code. Erst diesen Plan lesen, dann meilensteinweise umsetzen. Nach jedem Meilenstein: Tests laufen lassen, kurz berichten, auf Freigabe warten.

## 1. Ziel

Eine selbst gehostete Web-App im heimischen Intranet, über die eine Familie (zum Start Jörg und seine Frau, später weitere Familienmitglieder) mit verschiedenen KI-Modellen chattet. Jedes Mitglied hat ein eigenes Konto mit Rolle und passenden Rechten. Die Anbieter werden per API-Key angebunden. Oberfläche, Chatverläufe und Keys liegen ausschließlich auf dem eigenen Server.

## 2. Rahmenbedingungen

- **Lizenz:** AGPL-3.0-or-later (`LICENSE`).
- **Bezeichner Englisch, Oberfläche Deutsch:** Alle Namen im Code sind englisch: Apps, Module, Klassen, Felder, Funktionen, Choice-Werte, Templates, URL-Namen, Tests, Make-Ziele, Env-Variablen. Sichtbare Texte und `verbose_name` sind deutsch. Kommentare und diese Planungsdokumente dürfen deutsch sein.
- **Stack:** Python 3.12+ (Debian 13: 3.13), Django 5.2 LTS, gunicorn, Makefile als Bedienoberfläche für die Entwicklung, `mgpt-ctl` für den Betrieb.
- **Betrieb:** läuft dauerhaft (24/7) auf dem NAS, nur im Intranet, kein Zugriff aus dem Internet. Ausgehend nur HTTPS zu den KI-Anbietern sowie HTTP im Intranet zu LM Studio.
- **Lokale Modelle:** LM Studio läuft auf einem anderen Rechner im Intranet und nur bei Bedarf. Die App muss damit umgehen, dass dieser Anbieter meistens offline ist.
- **Nutzer:** Familiensystem mit einer Handvoll Konten, keine Selbstregistrierung. Anlage durch einen Verwalter in der Oberfläche, im Django-Admin oder per `make user`.
- **Installation als Debian-Paket:** `multi-gpt` (gebaut mit dh-virtualenv) bringt sein eigenes venv in `/usr/share/python/multi-gpt` mit und wird über systemd gestartet. Systemnutzer `multi-gpt`, Konfiguration in `/etc/multi-gpt/.env`, Daten in `/var/lib/multi-gpt`. Docker (`Dockerfile`, `compose.yaml`) ist der Ausweichweg für Systeme ohne apt. Im Paket lauscht gunicorn nur auf `127.0.0.1`. Davor steht verpflichtend nginx mit TLS auf demselben Rechner (`Depends: nginx`, `ssl-cert`). Das Paket liefert die Site-Konfiguration mit und fragt Hostname(n) und Zertifikat per debconf ab (ohne eigenes Zertifikat: snakeoil mit Warnung). Docker und die Entwicklung laufen ohne nginx.
- **Kein Node-Buildschritt:** Frontend aus Django-Templates plus schlankem Vanilla-JS, alle Assets lokal (keine CDNs).
- **Sprache der Oberfläche:** Deutsch.

## 3. Nicht-Ziele (v1)

- Kein Starten, Stoppen oder Aufwecken von LM Studio aus der App heraus (kein Wake-on-LAN, kein Laden von Modellen). Die App zeigt nur an, ob LM Studio erreichbar ist (siehe Abschnitt 8a).
- Kein durchgehender Sprachdialog in Echtzeit. Sprache läuft als Aufnahme → Text und Antwort → Vorlesen (Abschnitt 8c).
- Kein Betrieb eigener Bildmodelle auf dem NAS. Erzeugung, Inpainting und Varianten laufen über API-Anbieter oder angebundene MCP-Server, klassische Bildbearbeitung (Zuschneiden, Skalieren, Umwandeln) lokal in Python (Abschnitte 8e und 8g).
- Keine Trennung mehrerer Haushalte in einer Installation. Ein System gehört einer Familie, innerhalb davon gibt es Rollen und Gruppen (Abschnitt 8f).

## 4. Architektur

```
Browser ──HTTPS──> nginx (im Paket Pflicht, Intranet-Hostname/TLS)
                    └──HTTP 127.0.0.1──> gunicorn (gthread) ──> Django
                                                  ├── chat (Views, SSE-Streaming)
                                                  ├── providers (Adapter je Anbieter)
                                                  └── PostgreSQL + pgvector
                                                  
Worker-Prozess (make worker) ──> Indexierung für RAG, gleiche Codebasis und DB
                                                  
Django ──HTTPS──> OpenAI / Anthropic / Google / Mistral / OpenRouter ...
Django ──HTTP (Intranet)──> LM Studio auf dem PC (nur zeitweise online)
```

- **Streaming:** Server-Sent Events über `StreamingHttpResponse`. gunicorn mit `--worker-class gthread`, z. B. 2 Worker × 8 Threads, `--timeout 300`. nginx: `proxy_buffering off` für die Stream-Endpunkte.
- **Datenbank:** PostgreSQL mit der Erweiterung `pgvector`, für alle Daten (Chats und RAG-Vektoren). Verbindung über `DATABASE_URL`. Kein SQLite.
- **Hintergrundarbeit:** Dokumente werden von einem eigenen Worker-Prozess indexiert, der eine Job-Tabelle in PostgreSQL abarbeitet. Kein Redis, kein Celery.
- **Dateien:** Hochgeladene Dokumente, erzeugte Bilder und Audiodateien liegen unter `MEDIA_ROOT` auf dem NAS und werden nur an angemeldete Besitzer ausgeliefert.
- **Statische Dateien:** Im Debian-Paket liefert nginx sie direkt aus dem Paket aus. WhiteNoise bleibt für die Entwicklung und Docker, wo kein nginx davorsteht.

## 5. Projektstruktur

```
multi-gpt/
├── Makefile
├── pyproject.toml       # einzige Quelle der Abhängigkeiten
├── .env.example
├── manage.py
├── multigpt/            # Django-Projekt: settings, urls, wsgi, Projekt-Templates; alle Django-Apps liegen darunter
│   ├── accounts/        # User (erweitert AbstractUser), UserGroup (erweitert auth.Group), Role, can()
│   ├── scratchpad/      # Scratchpad und Gesamtdokument (M13, Abschnitt 8h)
│   └── chat/            # App-Label chat: Modelle, Views, Templates, Static
│       ├── providers/   # base.py, openai_compat.py, anthropic.py, google.py
│       └── mcp/         # MCP-Client, Loop-Thread, Werkzeugschleife, Rechteprüfung
├── mcp_imagetools/      # mitgelieferter MCP-Server für Bildbearbeitung (Pillow)
├── tests/
├── deploy/              # gunicorn.conf.py, nginx/multi-gpt (nginx-Site des Pakets)
├── debian/              # Paketierung: rules, control, multi-gpt.service, postinst, mgpt-ctl
├── Dockerfile, compose.yaml
└── docs/                # Plan.md, Implementierung.md
    └── website/         # Projekt-Website (Hugo), veröffentlicht über GitHub Pages
```

## 6. Datenmodell

| Modell | Felder (Kern) | Zweck |
|---|---|---|
| `Provider` | name, kind (`openai_compat` / `anthropic` / `google`), base_url, api_key (verschlüsselt, optional), active, is_local, check_status, online, last_checked, last_online, last_error, reported_models, billing_account | Ein Anbieterzugang, gehört zu genau einem Abrechnungskonto |
| `AIModel` | provider, model_id, display_name, capability (`chat` / `image` / `embedding` / `stt` / `tts`), supports_tools, can_edit_images, active, sort_order | Ein auswählbares Modell; Preise in `ModelPrice` |
| `BillingAccount` (`billing`) | name, kind (`monetary` / `tokens` / `flat`), currency (`EUR` / `USD`, nur monetär), note, active | Abrechnungskonto; bündelt einen oder mehrere Anbieter |
| `ModelPrice` (`billing`) | ai_model, valid_from, input, cached_input, cache_write, cache_write_1h, output (je 1 Mio. Tokens, Kontowährung), long_context_threshold, long_* , unit_prices (JSON je Einheit), note | Preisversion eines Modells; gilt bis zur nächsten |
| `ExchangeRate` (`billing`) | date, usd_eur, source (`manual` / `ecb`) | Kurs USD → EUR ab dem Tag |
| `UsageEntry` (`billing`) | kind (`answer` / `attachment` / `tool`), account, user (Verfasser), ai_model, model_name, message, attachment (beide `SET_NULL`), created, tokens_in, tokens_out, cached_read, cache_write, cache_write_1h, reasoning, requests, units, rounds, amount, currency, amount_eur, rate, rate_date, price, legacy | Buchung; Grundlage für Verbrauch und Budgets |
| `AccountBudget` (`billing`) | account, role oder user, monthly_budget (EUR, monetär), monthly_tokens (Token-Konten) | Budget bzw. Kontingent je Konto; Nutzer vor Rolle |
| `Conversation` | user, title, default_model, system_prompt, project (optional, `SET_NULL`), current_leaf (Ende des angezeigten Zweigs), created, updated, archived | Ein Chat, gespeichert als Baum von Nachrichten |
| `Project` | owner, name, description, instructions (zusätzlicher System-Prompt aller Chats im Projekt), default_model (optional), collections (Standard-Sammlungen), color, pinned, archived, created, updated | Gruppiert Chats eines Kontos mit gemeinsamen Vorgaben; privat für den Besitzer |
| `Message` | conversation, parent (vorherige Nachricht, null für die erste), role, content, model, author, tokens_in, tokens_out, cost (EUR-Momentaufnahme aus der Buchung), status (`complete` / `aborted` / `error` / `awaiting_confirmation`; `superseded` nur noch für Altdaten), error, tool_state (Zwischenrunden der Werkzeugschleife inkl. `provider_state`), created | Eine Nachricht. Nachrichten mit gleichem `parent` sind Versionen (Geschwister): Bearbeiten einer eigenen Nachricht und „Neu erzeugen“ legen eine neue Version an, ältere bleiben erhalten und zählen im Verbrauch |
| `Preset` (optional) | user, name, system_prompt | Wiederverwendbare Rollen |
| `Attachment` | message, kind (`image` / `audio` / `file`), file, generated_by_model, source_image (Verweis auf das Ausgangsbild), cost | Erzeugte Bilder, Audio, Anhänge |
| `Collection` | owner, name | Eine Wissenssammlung für RAG |
| `Share` | Ziel (`Conversation` oder `Collection`), group, can_write | Freigabe an eine Gruppe, lesend oder schreibend |
| `Role` | key (`admin` / `adult` / `teen` / `guest`, stabil), name, is_admin, all_models, allowed_models, all_mcp_servers, allowed_mcp_servers, can_web_search, can_images, can_voice, can_upload_documents, can_share, monthly_budget (Gesamtbudget EUR über alle monetären Konten), fixed_system_prompt | Rechtepaket, im Admin änderbar. Eine leere Liste erlaubt nichts, „alle“ nur über `all_models` bzw. `all_mcp_servers` |
| `McpServer` | name, transport (`stdio` / `http`), command oder url, credentials (verschlüsselt, JSON mit `env` / `headers` / `bearer_token`), active, tools_requiring_confirmation, known_tools, timeout_seconds | Ein angebundener MCP-Server |
| `ToolCall` | message, server, tool, provider_call_id, arguments, result, status (`awaiting_confirmation` / `rejected` / `running` / `ok` / `error` / `timeout`), duration | Protokoll jedes Werkzeugaufrufs |
| `User` (`accounts.User`) | erweitert Djangos `AbstractUser` (`AUTH_USER_MODEL`): role, display_name, monthly_budget_override (optional), allow_supervision, auto_read_aloud; Sperren über `is_active` | Ein Familienkonto mit Rolle |
| `UserGroup` (`accounts.UserGroup`) | erweitert Djangos `auth.Group` per Tabellenvererbung (name, Mitglieder über `User.groups`) plus eigene Zusatzfelder | Zum Teilen, z. B. "Familie", "Eltern" |
| `Document` | collection, file, title, status (`pending` / `indexed` / `error`), error_text | Ein hochgeladenes Dokument |
| `Chunk` | document, position, text, page, embedding (`vector`) | Textabschnitt mit Vektor |
| `Job` | kind, payload, status, attempts, created | Warteschlange für den Worker |
| `SourceRef` | message, kind (`web` / `document`), title, url oder chunk | Quellenangaben einer Antwort |
| `Scratchpad` (M13) | owner, project (optional, eindeutig), title, citation_style (leer = Projekt bzw. Konto), ibid (an/aus), created, updated | Sammelstelle je Projekt bzw. eine persönliche je Konto |
| `ScratchSource` (M13) | scratchpad, key (stabil, z. B. `mueller2024`), fingerprint (eindeutig je Scratchpad), kind (`document` / `web` / `ai` / `tool` / `attachment` / `manual`), csl (`citations.Reference` als JSON), document, message, tool_call, attachment (alle optional, `SET_NULL`), url | Ein zitierbares Werk, je Scratchpad einmal (Dedup im Literaturverzeichnis) |
| `ScratchItem` (M13) | scratchpad, source (leer bei Notiz), origin (`answer` / `document` / `web` / `note` / `image` / `tool` / `upload` / `paste`), text_kind (`quote` / `paraphrase` / `own` / `ai`), text, comment, page, page_end, paragraph, paragraph_end, section, section_end, locator_text, attachment, provenance (JSON), snapshot, verified, tags, outline_section, position, created_by, created, updated | Ein Eintrag mit Herkunft, Textart und Fundstelle |
| `OutlineSection` (M13) | scratchpad, parent, position, title, brief | Gliederung |
| `ScratchDraft` / `DraftSection` (M13) | Entwurf: scratchpad, title, style; Abschnittsversion: draft, outline_section, version, text (Marker `[@item:n]`), model, cost, checks (JSON), created_by, is_current | Gesamtdokument mit Versionen je Abschnitt |
| `Preset` (erweitert in M13) | user, project (optional), name, kind (`system` / `selection`), text (Platzhalter `{auswahl}`, `{quelle}`), web_search, document_search, send_now, sort_order, active | System-Prompt-Vorlagen und Prompt-Bausteine fürs Kontextmenü |

Regeln:
- Modell-IDs werden **nicht hart kodiert**, sondern im Admin gepflegt. Zusätzlich `make sync-models`, das die Modellliste beim Anbieter abfragt, soweit dessen API das anbietet.
- Jeder Nutzer sieht nur seine eigenen Chats. Jede View filtert nach `request.user`.
- Ein Chat gehört höchstens einem Projekt, immer einem des Chat-Besitzers. System-Prompt in dieser Reihenfolge: Grundregeln, fester Prompt der Rolle, Hinweise von MultiGPT, Anweisungen des Projekts (als Nutzerinhalt gekennzeichnet), System-Prompt des Chats.
- Angezeigt, exportiert und an das Modell geschickt wird immer nur der Pfad von der ersten Nachricht bis `current_leaf`. Umschalten der Version setzt `current_leaf` auf das neueste Blatt des gewählten Zweigs.
- Private Inhalte (Chats, Nachrichten, Dokumente, Anhänge) zeigt auch der Django-Admin nur als Metadaten an, ohne Inhalt. Sie lassen sich dort weder ändern noch löschen.
- Ein Konto ohne ausdrücklich gewählte Rolle bekommt `guest`, ein Superuser `admin`. So führt eine vergessene Auswahl zu zu wenig Rechten, nie zu zu vielen.

## 7. Anbieter-Adapter

Gemeinsame Schnittstelle in `providers/base.py`:

```python
class ProviderAdapter:
    def stream(self, model_id, messages, system=None, **params) -> Iterator[Event]: ...
    def list_models(self) -> list[str]: ...
```

`Event` ist eines von: `delta(text)`, `tool_call(id, name, argumente)`, `usage(tokens_in, tokens_out)`, `error(message)`, `done`.

`stream()` nimmt zusätzlich `tools=[...]` (Name, Beschreibung, JSON-Schema) entgegen. Jeder Adapter übersetzt das in das Werkzeugformat seines Anbieters und zurück. Modelle ohne Werkzeugunterstützung werden im Admin als solche markiert und bekommen keine Werkzeuge angeboten.

Weitere Methoden, die ein Adapter je nach Anbieter umsetzt (sonst `NotImplementedError`): `embed(model_id, texts)`, `transcribe(model_id, audio)`, `speak(model_id, text, voice)`, `generate_image(model_id, prompt, **params)`.

- `openai_compat`: deckt OpenAI, Mistral, Groq, OpenRouter und LM Studio ab (nur `base_url` unterschiedlich). Für LM Studio ist kein API-Key nötig, das Feld darf leer sein.
- Zusätzliche Methode `is_online(timeout=2) -> bool` an der Basisklasse: kurzer Abruf der Modellliste. Für LM Studio liefert derselbe Abruf zugleich die aktuell verfügbaren Modelle.
- `anthropic`: eigene Messages-API.
- `google`: Gemini-API.

Umsetzung mit `httpx` direkt gegen die HTTP-APIs (wenige Abhängigkeiten, einheitliche Fehlerbehandlung). **Vor der Implementierung jedes Adapters die aktuelle API-Dokumentation des Anbieters lesen**, nicht aus dem Gedächtnis implementieren.

## 8. Funktionen v1

1. Login/Logout, Passwort ändern.
2. Chatliste in der Seitenleiste: neu, umbenennen, archivieren, löschen, Suche im Titel. Chats lassen sich zu Projekten gruppieren (Anweisungen, Standardmodell und Sammlungen je Projekt, wie in ChatGPT bzw. Claude).
3. Chatansicht: Modellauswahl pro Nachricht, gestreamte Antwort, Abbrechen-Knopf, Antwort neu erzeugen, eigene Nachricht bearbeiten (wie in ChatGPT). Bearbeiten und Neu erzeugen erzeugen Versionen; an jeder Nachricht mit mehreren Versionen schaltet „‹ 1/2 ›“ den Zweig um. Kopierknopf an Nachrichten.
4. Markdown-Darstellung mit Code-Hervorhebung und Kopierknopf (Bibliotheken lokal eingebunden, Ausgabe bereinigt).
5. System-Prompt pro Chat.
6. Automatischer Chattitel aus der ersten Nachricht.
7. **Vergleichsmodus:** dieselbe Frage an 2–3 Modelle parallel, Antworten nebeneinander.
8. Verbrauchsübersicht: Tokens und geschätzte Kosten je Nutzer, Modell und Monat.
9. Admin: Anbieter, Keys, Modelle, Nutzer.
10. Export eines Chats als Markdown.

11. LM Studio als lokaler Anbieter mit Online-Anzeige (Abschnitt 8a).

12. RAG über eigene Dokumente mit PostgreSQL/pgvector (Abschnitt 8b).
13. Sprach-Eingabe und -Ausgabe (Abschnitt 8c).
14. Websuche mit Quellenangaben (Abschnitt 8d).
15. Bildgenerierung (Abschnitt 8e).
16. Rollen, Gruppen und Budgets für die Familie (Abschnitt 8f).
17. MCP-Anbindung: Modelle nutzen Werkzeuge aus angebundenen MCP-Servern (Abschnitt 8g).
18. Bildbearbeitung: Inpainting, Varianten und klassische Bearbeitung (Abschnitt 8e).

19. **Gedächtnis über Chats** (Vorschlag vom 2026-10-09, noch nicht freigegeben). Das Modell selbst vergisst, MultiGPT hat aber alle Chats lokal:
    - **Stufe 1:** Eingebaute Werkzeuge `search_chats` / `read_chat`, Volltextsuche nur über eigene und geteilte Chats.
    - **Stufe 2:** Suche nach Bedeutung über pgvector. Offen ist, ob die Embeddings über OpenAI oder lokal über LM Studio entstehen.
    - **Stufe 3:** „Erinnerungen“, die das Modell vorschlägt. Gespeichert wird erst nach Bestätigung, je Mitglied einsehbar und löschbar, für Rollen schaltbar.
20. **Scratchpad** (M13, geplant am 2026-10-10): Material aus allen Quellen sammeln und daraus ein belegtes Gesamtdokument erzeugen, dazu ein Kontextmenü im Chat und Prompt-Vorlagen (Abschnitt 8h).

Später (v2): Bilder als Eingabe an Modelle, Presets.

## 8a. LM Studio und Online-Anzeige

LM Studio stellt einen OpenAI-kompatiblen Server bereit (Standard: `http://<PC-IP>:1234/v1`). Vor der Umsetzung die aktuelle LM-Studio-Dokumentation zu Server, Port und Netzwerkfreigabe lesen.

- **Anlage:** als `Provider` mit `kind=openai_compat`, `is_local=True`, `check_status=True`, `base_url` auf den PC im Intranet.
- **Statusprüfung:** Endpunkt `GET /api/providers/status/` prüft alle Anbieter mit `check_status=True` per `is_online()` (Timeout 2 s) und liefert `online`, Modellliste und `last_online`. Ergebnis wird 15 s serverseitig gecacht, damit zwei offene Browser den PC nicht doppelt abfragen.
- **Anzeige:** Statuspunkt in der Kopfzeile und an den lokalen Modellen in der Modellauswahl: grün "LM Studio online", grau "offline, zuletzt online um …". Der Browser fragt alle 30 s nach, solange der Tab sichtbar ist.
- **Meldung:** Wechselt der Status von offline auf online, erscheint ein kurzer Hinweis ("LM Studio ist jetzt online"). Umgekehrt ebenso.
- **Modellauswahl:** Lokale Modelle sind bei offline ausgegraut und nicht wählbar. Bei online werden die tatsächlich von LM Studio gemeldeten Modelle angeboten, ohne Pflege im Admin.
- **Fehlerfall:** Geht LM Studio während einer Antwort offline, bleibt der bereits empfangene Text erhalten, die Nachricht wird als abgebrochen markiert, und der Nutzer kann mit einem anderen Modell neu erzeugen.
- **Kosten:** Lokale Modelle zählen in der Verbrauchsübersicht mit Tokens, aber mit 0 € Kosten.

## 8b. RAG mit PostgreSQL und pgvector

- **Sammlungen:** Jeder Nutzer legt Sammlungen an und lädt Dokumente hoch (PDF, DOCX, TXT, MD). Eine Sammlung ist privat oder mit Gruppen geteilt (Abschnitt 8f).
- **Indexierung (Worker):** Text extrahieren → in überlappende Abschnitte teilen (ca. 800 Tokens, 100 Überlappung, Seitenzahl merken) → Embeddings über das konfigurierte Embedding-Modell → in `Chunk` speichern. Status und Fehler sind in der Oberfläche sichtbar. **OCR für gescannte Seiten:** wahlweise mit dem Vision-Modell **olmOCR** (`allenai/olmocr-2-7b`) über LM Studio, empfohlen für Tabellen, Spalten und Formeln, oder mit Tesseract (`deu+eng`). Ist LM Studio nicht erreichbar, springt Tesseract ein (Einstellung „Tesseract als Ersatz“, standardmäßig an); sonst wartet die Indexierung. Die Auswahl steht in den RAG-Einstellungen.
- **Embedding-Modell:** genau eines, in den RAG-Einstellungen festgelegt. **Entscheidung vom 2026-10-09: lokal über LM Studio** (`text-embedding-nomic-embed-text-v1.5`, 768 Dimensionen), damit keine Dokumentinhalte das Haus verlassen. Indexierung und Dokumentsuche brauchen dann einen laufenden LM-Studio-PC: Der Worker wartet und wiederholt, die Chat-Antwort entsteht mit Hinweis ohne Dokumente. Ein Wechsel des Modells oder der Dimension erfordert eine Migration und „Alles neu indexieren“.
- **Abfrage:** Im Chat wählt der Nutzer eine oder mehrere Sammlungen. Die Frage wird eingebettet, die ähnlichsten Abschnitte (Kosinus-Abstand, HNSW-Index, Top 6) kommen als Kontext in die Anfrage. Optional zusätzlich PostgreSQL-Volltextsuche und Zusammenführung beider Trefferlisten.
- **Quellen:** Unter der Antwort stehen die verwendeten Dokumente mit Seitenzahl, anklickbar zum Textabschnitt.
- **Zugriff:** Abschnitte fremder privater Sammlungen dürfen nie in einer Abfrage landen. Dafür gibt es einen eigenen Test.
- **Verzeichnisquellen (Verwalter):** Eine Sammlung kann einen Ordner auf dem Server bzw. NAS einlesen. Der Ordner wird periodisch gecrawlt: Neue und geänderte Dateien werden indexiert, gelöschte entfernt. Die Dateien bleiben am Ort und werden nicht kopiert. Dabei gilt:
  - Erlaubt sind nur Pfade unterhalb der Wurzeln in `RAG_SOURCE_ROOTS`, geprüft per `realpath` bei der Anlage und bei jedem Lauf.
  - Der Dienst liest nur. Für Pfade unter `/home` ist ein systemd-Drop-in nötig.
  - Die Nutzer sehen die Dokumente, können sie aber nicht einzeln löschen.
- **Verwaltung im Admin:** eigener Abschnitt „Dokumente (RAG)“, nur für Verwalter. Er umfasst:
  - die Übersicht: Konfiguration, Embedding-Test, Zahlen je Status, Speicherbedarf, Zustand des Workers und Warteschlange;
  - die Aktionen „Alles neu indexieren“ (z. B. nach einem Modellwechsel), „Fehlgeschlagene erneut versuchen“ und „Hängende Aufträge zurücksetzen“;
  - Listen der Sammlungen, Dokumente und Indexierungsaufträge sowie die Einstellungen.

  Wie bei den Chats sehen Verwalter dort nur Metadaten, keinen Dokument- oder Abschnittsinhalt.

## 8c. Sprach-Eingabe und -Ausgabe

- **Eingabe:** Mikrofonknopf im Eingabefeld. Der Browser nimmt auf (`MediaRecorder`), die Aufnahme geht an den Server und von dort an ein Modell mit `capability=stt`. Der erkannte Text landet zum Korrigieren im Eingabefeld, nicht direkt im Chat.
- **Ausgabe:** Lautsprecherknopf an jeder Antwort. Der Text geht an ein Modell mit `capability=tts`, die Audiodatei wird gespeichert und im Browser abgespielt. Optional "Antworten automatisch vorlesen" je Nutzer.
- **Voraussetzung:** Browser geben das Mikrofon nur über HTTPS frei. TLS im Intranet ist damit Pflicht (siehe Abschnitt 9).
- **Grenzen:** maximale Aufnahmelänge und Dateigröße konfigurierbar.

## 8d. Websuche

- **Ablauf:** Schalter "Websuche" im Eingabefeld. Ist er an: Suchanfrage aus der Nutzerfrage bilden → Such-Backend abfragen → die besten Treffer abrufen und auf Text reduzieren → als Kontext mit nummerierten Quellen an das Modell. Dieser Ablauf funktioniert mit jedem Modell, auch mit lokalen.
- **Such-Backend:** hinter einer kleinen Schnittstelle `search(query) -> [Treffer]` austauschbar. Backend ist **SearXNG**, selbst gehostet im Intranet (Entscheidung vom 2026-10-09). Die Schnittstelle bleibt austauschbar, eine Such-API mit Key kann später folgen. Die Einstellungen (URL, Trefferzahl, Seitenabruf) legt der Verwalter im Admin unter „Sucheinstellungen“ fest. Die Einrichtung von SearXNG beschreibt das GitHub-Wiki in zwei Varianten: Docker und nativ.
- **Quellen:** Unter der Antwort stehen Titel und Links der verwendeten Seiten.
- **Sicherheit:** Abgerufene Seiteninhalte sind nicht vertrauenswürdig. Sie werden klar als Quellmaterial markiert an das Modell gegeben, nie als Anweisung. Der Abruf darf keine Intranet-Adressen ansprechen (Schutz gegen SSRF), hat Timeouts und Größenlimits.

## 8e. Bildgenerierung

- **Ablauf:** Modus "Bild" im Eingabefeld mit Auswahl eines Modells mit `capability=image` sowie Format (quadratisch, quer, hoch). Das Ergebnis wird als `Attachment` gespeichert und im Chat angezeigt, mit Herunterladen und "Neu erzeugen".
- **Adapter:** eigene Methode `generate_image(model_id, prompt, **params)`, getrennt vom Chat-Streaming.
- **Bearbeitung mit Bildmodell:** Bild hochladen oder ein erzeugtes Bild auswählen, dann "Bereich ändern" (Inpainting: Bereich im Browser auf einer Zeichenfläche markieren, Maske geht mit) oder "Varianten". Adaptermethode `edit_image(model_id, image, prompt, mask=None)`. Nicht jeder Anbieter kann das, die Fähigkeit wird je Modell im Admin markiert (`can_edit_images`).
- **Klassische Bearbeitung in Python:** Zuschneiden, Skalieren, Drehen, Format umwandeln, Hintergrund füllen, Text einsetzen, Collage. Umgesetzt mit Pillow als mitgelieferter MCP-Server `mcp_imagetools` (Abschnitt 8g), damit jedes werkzeugfähige Modell es per Sprache bedienen kann ("mach das Bild quadratisch und 1024 Pixel breit").
- **Verlauf:** Jede Bearbeitung erzeugt ein neues `Attachment` mit Verweis auf das Ausgangsbild. Das Original bleibt erhalten.
- **Kosten:** Bilder werden pro Stück in der Verbrauchsübersicht geführt.

## 8f. Rollen, Gruppen und Budgets

**Rollen** (als Startdaten angelegt, im Admin anpassbar und erweiterbar):

| Rolle | Darf |
|---|---|
| Verwalter | Alles: Anbieter, Keys, Modelle, Konten, Rollen, Gruppen, Budgets, Verbrauch aller Mitglieder |
| Erwachsener | Alle freigegebenen Modelle und Funktionen, eigene Sammlungen, Teilen mit Gruppen, eigener Verbrauch |
| Jugendlicher | Nur die für die Rolle freigegebenen Modelle und Funktionen, fester System-Prompt der Rolle, Monatsbudget |
| Gast | Nur Chat mit einem festgelegten Modell, kein Upload, kein Teilen, kleines Budget |

- **Durchsetzung:** Rechte werden serverseitig in jeder View und vor jedem Anbieteraufruf geprüft, zentral über eine Funktion `can(user, action, obj=None)`. Ausblenden in der Oberfläche reicht nicht.
- **Fester System-Prompt:** Hat eine Rolle einen, wird er jedem Chat vorangestellt und ist für das Mitglied weder sichtbar änderbar noch abschaltbar.
- **Budgets:** Kontenrahmen (M6-06 bis M6-10): Jeder Anbieter gehört zu einem Abrechnungskonto (monetär in EUR/USD, nur Tokens wie LM Studio, Pauschale). Budgets je Konto und Rolle bzw. Mitglied: monetär in EUR, Token-Konten als Token-Kontingent, Pauschalkonten ohne Budget. Dazu ein Gesamtbudget (EUR) je Rolle, je Mitglied überschreibbar. Bei 80 % ein Hinweis, bei 100 % sind die Modelle des betroffenen Kontos (beim Gesamtbudget alle kostenpflichtigen) bis zum Monatswechsel gesperrt.
- **Gruppen:** Sammlungen (RAG) und einzelne Chats lassen sich mit Gruppen teilen, lesend oder mit Schreibrecht. Standardgruppe "Familie" enthält alle Mitglieder.
- **Privatsphäre:** Chats sind privat. Auch Verwalter sehen fremde Chats nicht in der Oberfläche, nur Verbrauchszahlen. Eine Einsicht in Chats von Jugendlichen-Konten ist als Option je Konto vorgesehen, standardmäßig aus, und wird dem betroffenen Mitglied in der Oberfläche angezeigt.
- **Verwaltung:** Eigene Seite "Familie" für Verwalter: Konten anlegen und sperren, Rolle zuweisen, Passwort zurücksetzen, Gruppen pflegen, Verbrauch je Mitglied.

## 8g. MCP (Model Context Protocol)

Die App ist **MCP-Client**: Sie verbindet sich mit MCP-Servern, reicht deren Werkzeuge an das Modell weiter und führt die Aufrufe aus. Damit lassen sich Fähigkeiten nachrüsten, ohne die App zu ändern. Umsetzung mit dem offiziellen Python-SDK `mcp`. **Vor der Umsetzung die aktuelle MCP-Spezifikation und SDK-Dokumentation lesen.**

- **Brücke sync ↔ async:** Das SDK ist asyncio-basiert, Django läuft synchron in gunicorn-`gthread`-Threads. Je gunicorn-Prozess gibt es genau einen Thread mit dauerhaftem Event-Loop, in dem alle MCP-Sitzungen leben. Request-Threads übergeben Aufrufe per `asyncio.run_coroutine_threadsafe()` und warten mit Timeout. Mutexe: ein `threading.Lock` für den verzögerten Start des Loop-Threads (erst nach dem Fork), ein `asyncio.Lock` je Server für Verbindungsaufbau und Neuverbindung (und für Aufrufe, falls ein Server keine parallelen Anfragen verträgt). Beim Beenden des Workers werden alle Sitzungen und `stdio`-Prozesse geschlossen.

- **Einordnung:** MCP ist die Steckverbindung für Werkzeuge, kein Bildmodell. Inpainting braucht weiterhin ein Bildmodell (API-Anbieter oder ein MCP-Server, der eines anspricht). MCP macht es für alle Modelle einheitlich bedienbar.
- **Server anbinden:** Verwalter legen `McpServer` im Admin an. Transport `stdio` (lokaler Prozess auf dem NAS, Befehl und Umgebung) oder `http` (Streamable HTTP, URL und Zugangsdaten). Verbindungstest und Werkzeugliste im Admin sichtbar.
- **Werkzeugschleife:** Modell meldet `tool_call` → Rechte prüfen → ggf. Rückfrage → Werkzeug über MCP ausführen → Ergebnis zurück an das Modell → weiter streamen. Höchstens 10 Runden je Antwort, Timeout je Aufruf.
- **Anzeige:** Jeder Aufruf erscheint im Chat als aufklappbare Zeile (Werkzeug, Argumente, Ergebnis, Dauer). Liefert ein Werkzeug ein Bild oder eine Datei, wird daraus ein `Attachment`.
- **Auswahl im Chat:** Schalter je Server im Eingabefeld, voreingestellt nach Rolle.
- **Mitgelieferte Server:** `mcp_imagetools` (Pillow). Websuche (8d) und RAG-Suche (8b) werden zusätzlich als Werkzeuge bereitgestellt, damit werkzeugfähige Modelle selbst entscheiden, wann sie suchen. Der feste Ablauf aus 8b/8d bleibt für Modelle ohne Werkzeugunterstützung.
- **Rechte:** `Role.allowed_mcp_servers` legt fest, wer welche Server nutzen darf. Prüfung über `can()` vor jedem Aufruf.
- **Rückfrage:** Werkzeuge, die etwas verändern oder nach außen senden, stehen in `tools_requiring_confirmation` und werden erst nach Bestätigung des Nutzers im Chat ausgeführt. Neue, unbekannte Werkzeuge eines Servers gelten bis zur Einstufung durch den Verwalter als rückfragepflichtig.
- **Dateizugriff:** Mitgelieferte Server arbeiten nur in einem Arbeitsordner je Nutzer unter `MEDIA_ROOT`.

## 8h. Scratchpad und Gesamtdokument

Im Scratchpad sammelt der Nutzer Material aus Chats, eigenen Dokumenten, dem Web, Werkzeugen, Bildern, Uploads und eigenen Notizen. Daraus erzeugt er ein belegtes Gesamtdokument. Arbeitspakete: Implementierung M13.

- **Kontextmenü im Chat** (vorab lieferbar): auf markiertem Text in Antworten, Nutzernachrichten, Quellen, Dokumentabschnitten und Vergleichsspalten. Öffnen per Rechtsklick, über eine schwebende Leiste bei Auswahl (Handy) oder die Kontextmenü-Taste bzw. Umschalt+F10. Umschalt+Rechtsklick öffnet das Browsermenü. Einträge:
  - „Ins Scratchpad“ (als Zitat, als Notiz, in Abschnitt …)
  - „Im Eingabefeld zitieren“, „Nachfragen …“
  - Schnellaktionen (Erklären, Vereinfachen, Übersetzen, Belege suchen, Gegenargumente)
  - eigene Prompt-Vorlagen mit `{auswahl}`
  - Kopieren mit Quellenangabe, Suchen im Web bzw. in Dokumenten
- **Eintrag:** Text oder Bild mit Herkunft (Antwort, Dokument, Web, Notiz, Bild, Werkzeug, Upload, Zwischenablage), Textart (wörtliches Zitat, Paraphrase, eigene Formulierung, KI-Formulierung), Quelle mit Fundstelle, Tags, Reihenfolge und Gliederungsabschnitt. Herkunft, Fundstelle und Snapshot des Originals setzt der Server, nicht der Browser.
- **Zitieren für alle Quellenarten:** `citations.Reference` ist die gemeinsame, CSL-ähnliche Darstellung, im eigenen Code.
  - Neue Arten: KI-Antwort (nach den aktuellen Empfehlungen von APA und MLA, Chicago nur im Text), persönliche Mitteilung, Werkzeug- bzw. Datenergebnis.
  - Gleiche Quellen erscheinen im Literaturverzeichnis einmal, über einen Fingerprint und einen stabilen Schlüssel.
  - „ebd.“ setzt nur DIN.
  - Paraphrasen tragen ihre Fundstelle, bei DIN und Harvard mit „vgl.“.
  - Export auch als BibTeX bzw. CSL-JSON.
- **Gesamtdokument:**
  1. Gliederung festlegen oder vorschlagen lassen.
  2. Einträge zuordnen (Vorschlag lokal über Embeddings).
  3. Je Abschnitt schreibt ein Modell **nur aus den zugeordneten Einträgen**, mit Belegen [n] auf Einträge und ohne Werkzeuge.
  4. Der Server setzt Kurzbelege und das Literaturverzeichnis im Stil von Scratchpad, Projekt oder Konto.
  5. Er prüft wörtliche Zitate gegen den Quelltext und markiert Fehlbelege, unbelegte Aussagen und Lücken.
  
  Jeder Abschnitt lässt sich überarbeiten, jede Fassung bleibt als Version erhalten.
- **Export:** Markdown (auch Pandoc-kompatibel mit `.bib`), DOCX über python-docx und PDF über WeasyPrint aus HTML, ohne Netzabrufe.
- **Projekte:** Je Projekt gibt es ein Scratchpad, das die Projektfreigabe (RWUD) erbt. Dazu hat jedes Konto ein persönliches Scratchpad.
- **Datenschutz:** Die Inhalte gehören dem Nutzer, Verwalter sehen nur Metadaten. Quellmaterial gilt als nicht vertrauenswürdig: Es wird markiert und gelangt nie in den System-Prompt. Bilder werden ohne Metadaten gespeichert.

## 9. Sicherheit

- API-Keys mit Fernet (`cryptography`) verschlüsselt in der DB. Schlüssel aus `FIELD_ENCRYPTION_KEY` in `.env`. Keys werden im Admin nie im Klartext angezeigt, nur die letzten 4 Zeichen.
- Konfiguration im Betrieb in `/etc/multi-gpt/.env` (`root:multi-gpt`, 0640), beim ersten Installieren mit generierten Schlüsseln erzeugt und nie überschrieben. In der Entwicklung `.env` im Projektordner (600). Nie im Repository. Alle Settings kommen aus der Umgebung.
- `DEBUG=False` im Betrieb. Sichere Cookies, sobald TLS aktiv ist.
- Markdown-Ausgabe der Modelle wird vor dem Einfügen ins DOM bereinigt (XSS).
- Login-Drosselung gegen Durchprobieren von Passwörtern.
- Keys und Nachrichteninhalte erscheinen nicht in Logs.
- **TLS ist Pflicht**, weil das Mikrofon im Browser sonst nicht verfügbar ist. nginx mit Zertifikat für den Intranet-Hostnamen gehört damit fest zum Aufbau.
- Uploads: erlaubte Dateitypen und Maximalgröße prüfen, Dateinamen nicht übernehmen, Auslieferung nur nach Besitzprüfung.
- Websuche: Schutz gegen SSRF und Behandlung fremder Inhalte wie in Abschnitt 8d.
- MCP: Ein `stdio`-Server ist ein Programm, das mit den Rechten der App auf dem NAS läuft. Nur Verwalter dürfen Server anlegen, und nur aus vertrauenswürdiger Quelle. Der Dienst läuft unter einem eigenen Systemnutzer ohne Zugriff auf andere NAS-Freigaben. Werkzeugergebnisse sind wie Webinhalte nicht vertrauenswürdig und dürfen keine Rückfragepflicht aushebeln. Zugangsdaten der Server werden wie API-Keys verschlüsselt.

## 10. Makefile-Ziele

| Ziel | Wirkung |
|---|---|
| `make install` | `.venv` anlegen, Abhängigkeiten installieren, `.env` aus Vorlage erzeugen (inkl. generierter Schlüssel) |
| `make db-create` | Entwicklungs-DB samt pgvector anlegen (braucht sudo) |
| `make migrate` | Datenbankmigrationen |
| `make user` | Nutzer anlegen (ab M2 mit Rollenwahl) |
| `make dev` | Entwicklungsserver |
| `make run` | gunicorn im Vordergrund |
| `make static` | `collectstatic` |
| `make test` / `make lint` | pytest / ruff |
| `make sync-models` | Modelllisten der Anbieter abrufen |
| `make backup` | `pg_dump`, Medienordner und `.env` als Archiv mit Datum sichern |
| `make worker` | Worker für Indexierung im Vordergrund starten |
| `make reindex` | Alle Dokumente neu einbetten (nach Wechsel des Embedding-Modells) |
| `make deb` | Debian-Paket bauen (`dpkg-buildpackage`), statische Dateien werden dabei gesammelt |
| `make release VERSION=x.y.z` | Neue Version vorbereiten: setzt die Version in `pyproject.toml`, stellt einen Eintrag in `debian/changelog` voran (Commits seit dem letzten Tag), committet und setzt das Tag `v<VERSION>`. Danach `make deb` und `git push origin main v<VERSION>`. Ohne neue Version installiert apt kein Update. |
| `make website` / `make website-serve` | Projekt-Website (Hugo, `docs/website/`) bauen bzw. lokal mit Live-Reload anzeigen |
| `make deploy` | Website auf GitHub Pages veröffentlichen: prüft Build sowie Commit- und Push-Stand und startet den Workflow `website.yml` (braucht `gh` und ein Git-Remote) |

Im Betrieb ersetzt das Paket die früheren Ziele `service-install` und `update`: Installieren und Aktualisieren mit `apt install ./multi-gpt_<version>_<arch>.deb`, danach `mgpt-ctl migrate`. `mgpt-ctl` ist ein Wrapper um `manage.py`, der als Nutzer `multi-gpt` mit `/etc/multi-gpt/.env` läuft (z. B. `mgpt-ctl createsuperuser`). Der Worker bekommt eine eigene Unit `multi-gpt-worker.service`.

## 11. Meilensteine

1. **Grundgerüst:** Projekt, Settings über `.env`, Makefile, Login mit Drosselung, leere Chatseite, gunicorn (`gthread`) startet, `/healthz/`, Debian-Paket und systemd-Unit. *Abnahme: `make install migrate user run`, Login im Browser funktioniert. `make deb` baut, das Paket installiert sich, die Unit startet.*
2. **Datenmodell und Admin:** Modelle aus Abschnitt 6, Key-Verschlüsselung, Admin-Masken, Rollen, Konten und Gruppen (Erweiterungen von Djangos User und Group) und die zentrale Rechteprüfung `can()`. *Abnahme: Anbieter und Modell anlegbar, Key in der DB nicht lesbar. Vier Startrollen vorhanden, ein Gast-Konto erreicht keine Verwaltungsseite.*
3. **Erster Adapter und Streaming:** `openai_compat`, SSE-Endpunkt, Chatansicht mit Abbrechen. *Abnahme: gestreamte Antwort, Verlauf bleibt nach Neuladen erhalten.*
4. **Weitere Adapter und LM Studio:** Anthropic, Google, `sync-models`, LM Studio mit Online-Anzeige. *Abnahme: Modellwechsel mitten im Chat funktioniert. LM Studio starten und beenden ändert die Anzeige innerhalb von 30 s, ohne die Seite neu zu laden.*
4a. **MCP:** Werkzeugunterstützung in den Adaptern, MCP-Client, Werkzeugschleife, Rückfrage, Anzeige im Chat, Admin-Maske. *Abnahme: Ein Test-MCP-Server wird angebunden, ein Modell ruft dessen Werkzeug auf, der Aufruf ist im Chat sichtbar. Ein Konto ohne Freigabe bekommt das Werkzeug nicht.*
5. **Komfort:** Markdown, Titel, Umbenennen, Archiv, Export, System-Prompt.
6. **Vergleichsmodus, Verbrauchsübersicht, Budgets und Seite "Familie".** *Abnahme: Konto mit ausgeschöpftem Budget kann nur noch lokale Modelle nutzen.*
7. **RAG:** Sammlungen, Upload, Worker, pgvector-Suche, Quellenanzeige. *Abnahme: Frage zu einem hochgeladenen PDF wird mit Seitenangabe beantwortet. Private Sammlung des anderen Nutzers ist unsichtbar.*
8. **Websuche:** Such-Schnittstelle, Abruf, Quellenanzeige. *Abnahme: Frage zu einem aktuellen Ereignis liefert Antwort mit Links.*
9. **Bildgenerierung und Bildbearbeitung:** Erzeugung, Inpainting mit Maske, Varianten, MCP-Server `mcp_imagetools`. *Abnahme: Bild erscheint im Chat und bleibt nach Neuladen erhalten. Ein markierter Bereich wird ersetzt. "Schneide das Bild quadratisch zu" liefert per Werkzeug ein neues Bild, das Original bleibt.*
10. **Sprache:** Aufnahme → Text, Antwort → Vorlesen. *Abnahme: funktioniert über HTTPS im Browser an PC und Handy.*
11. **Musik** (neu eingeplant am 2026-10-09, Umfang wird noch geklärt, siehe offene Frage 6).
12. **Betrieb:** Worker-Unit, nginx mit TLS, Backup, README mit Installationsanleitung, Upgrade und Purge des Pakets geprüft.
13. **Scratchpad** (neu eingeplant am 2026-10-10, Abschnitt 8h, offene Frage 7): Kontextmenü im Chat und Prompt-Vorlagen (vorab lieferbar), Zitieren für alle Quellenarten, Sammeln mit Herkunft, Gliederung, belegtes Gesamtdokument mit Versionen und Prüfung, Export als Markdown, DOCX und PDF, Andocken an Projekte. *Abnahme: Aus einer Antwort, einem PDF-Abschnitt, einer Webquelle, einer Notiz und einem Bild entsteht ein Dokument mit geprüften Zitaten, Kurzbelegen im Stil des Kontos und einem Literaturverzeichnis ohne doppelte Quellen; eine unbelegte Aussage ist markiert; DOCX und PDF lassen sich öffnen.*

Hinweis zur Reihenfolge: PostgreSQL mit pgvector wird schon in Meilenstein 1 eingerichtet, TLS spätestens vor Meilenstein 10.

## 12. Tests

- Adapter gegen aufgezeichnete bzw. gemockte HTTP-Antworten (kein echter API-Aufruf in Tests).
- Zugriffsschutz: Nutzer A kann Chats von Nutzer B weder lesen noch ändern.
- Streaming-Endpunkt: Reihenfolge der Events, Fehlerfall, Abbruch.
- Verschlüsselung: Roundtrip, falscher Schlüssel schlägt sauber fehl.
- Rollen: je Rolle und Aktion ein Test, dass Verbotenes serverseitig abgelehnt wird (auch bei direktem Aufruf der URL). Fester System-Prompt landet in der Anfrage. Budgetsperre greift.
- MCP: Werkzeugschleife gegen einen Test-Server im Prozess, Rundenlimit, Timeout, Rückfrage wird ohne Bestätigung nicht ausgeführt, Rechteprüfung je Rolle, Übersetzung der Werkzeugformate je Adapter.
- Bildwerkzeuge: jede Pillow-Funktion mit Beispielbild, kein Zugriff außerhalb des Arbeitsordners.
- Teilen: Gruppenmitglied sieht geteilte Sammlung, Nichtmitglied nicht, Entzug wirkt sofort.
- RAG: Zerteilung, Suche liefert erwartete Abschnitte, Zugriffsgrenzen zwischen Nutzern, Worker-Wiederholung bei Fehler.
- Websuche: Such-Backend gemockt, Intranet-Adressen werden abgelehnt.
- Sprache und Bild: Adapteraufrufe gemockt, Dateien werden gespeichert und nur dem Besitzer ausgeliefert.
- Statusprüfung: online, offline (Verbindung abgelehnt), Timeout, Cache greift, Abbruch mitten im Stream.

## 13. Offene Fragen

1. ~~Welches NAS?~~ **Geklärt (2026-10-09):** Das Zielsystem läuft mit Debian/Ubuntu und apt. Das Debian-Paket ist der Betriebsweg, Docker bleibt Ausweichweg.
1a. Welche feste IP oder welchen Hostnamen hat der PC mit LM Studio, und ist dort die Freigabe des Servers im lokalen Netz aktiviert?
2. ~~Reverse Proxy und TLS?~~ **Geklärt (2026-10-09, umgesetzt in Commit `d9c76a9`):** nginx kommt mit dem Paket auf demselben Rechner (`Depends`), gunicorn lauscht nur auf `127.0.0.1`. Das Paket bringt die Site `/etc/nginx/sites-available/multi-gpt` mit (Stream ohne Puffer, `X-Accel-Redirect` vorbereitet). Den Hostnamen fragt debconf ab (Vorschlag: `hostname -f`). Zertifikat: ein eigenes (Pfad per debconf) oder das selbstsignierte snakeoil-Zertifikat aus `ssl-cert`.
3. ~~Anbieter zum Start?~~ **Geklärt (2026-10-09):** OpenRouter, OpenAI, Anthropic und Google Gemini. Damit sind alle drei Adapterarten (`openai_compat`, `anthropic`, `google`) zum Start im Einsatz.
4. ~~PostgreSQL mit pgvector?~~ **Geklärt (2026-10-09):** Auf dem Zielsystem ist PostgreSQL mit pgvector vorhanden und wird genutzt. Kein eigener Container.
4a. ~~SearXNG oder Such-API?~~ **Geklärt (2026-10-09):** SearXNG im Intranet. Eine Such-API bleibt als spätere Erweiterung möglich.
4b. ~~Anbieter für Embeddings, Sprache, Bilder?~~ **Geklärt (2026-10-09), Embeddings geändert am selben Tag:** Embeddings **lokal über LM Studio** (nomic-embed-text, 768 Dimensionen). Spracherkennung, Sprachausgabe und Bilder kommen über OpenAI.
4c. ~~Sprache der Dokumente, OCR?~~ **Geklärt (2026-10-09):** Überwiegend deutsch, auch gescannte PDFs. OCR mit olmOCR über LM Studio, Tesseract (`tesseract-ocr`, `tesseract-ocr-deu`) als Ersatz.
4d. Welche MCP-Server sollen zum Start angebunden werden (außer den mitgelieferten), und laufen schon welche im Intranet?
4e. ~~Anbieter für Inpainting?~~ **Geklärt (2026-10-09):** OpenAI (Bildbearbeitung mit Maske). Vor M9 in der aktuellen API-Dokumentation prüfen, welches Modell Masken und Varianten unterstützt.
5. ~~Wer gehört zur Familie?~~ **Geklärt (2026-10-09):** Zwei Erwachsene und Jugendliche. Die vier Startrollen (Verwalter, Erwachsener, Jugendlicher, Gast) passen.
5a. ~~Einsicht in Jugendlichen-Chats?~~ **Geklärt (2026-10-09):** Nur als Option je Konto, standardmäßig aus, für das Mitglied sichtbar angezeigt (Feld `allow_supervision` an `accounts.User`).
6. **Musik (M11):** Was genau soll der Meilenstein leisten? Zum Beispiel Musik mit KI-Modellen erzeugen (welcher Anbieter?), eine Musiksammlung auf dem NAS durchsuchen und abspielen, Songtexte und Akkorde, Musik erkennen?
7. **Scratchpad (M13)**, jeweils mit Empfehlung:
   - 7a: ein Scratchpad je Projekt plus ein persönliches je Konto? Empfehlung: ja.
   - 7b: mehrere Gesamtdokumente je Scratchpad? Empfehlung: ja.
   - 7c: „Ins Scratchpad“ aus einem nur lesend geteilten Chat? Empfehlung: ja, mit Rücklink nur bei Leserecht.
   - 7d: KI-Antworten im Literaturverzeichnis? Empfehlung: je Stil, dazu „Verwendete KI-Werkzeuge“.
   - 7e: „ebd.“? Empfehlung: nur DIN, abschaltbar.
   - 7f: PDF über WeasyPrint statt Pandoc + LaTeX? Empfehlung: ja.
   - 7g: Fußnoten-Stile? Empfehlung: später.
   - 7h: Einsicht in Jugendlichen-Konten auch für Scratchpads? Empfehlung: nein.
   - 7i: KI-Belegprüfung standardmäßig? Empfehlung: aus, per Knopf.
   - 7j: eigenes Kontextmenü standardmäßig an? Empfehlung: ja, abschaltbar.
   - 7k: Prompt-Vorlagen je Projekt teilen? Empfehlung: ja.
   - 7l: Crossref für manuelle Quellen? Empfehlung: ja, am vorhandenen Schalter.
   
   Details: Implementierung Abschnitt 6.
5b. ~~Eine Familie oder mehrere Haushalte?~~ **Geklärt (2026-10-09):** Eine Familie pro Installation, keine Mandantentrennung (siehe Nicht-Ziele).