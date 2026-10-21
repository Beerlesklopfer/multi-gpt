# Liste offener Punkte (LOP)

Stand: 2026-10-17, 0.4.0 wird veröffentlicht (Runner M14, Schnittstelle „LM Studio“, Scratchpad auf dem Server). Offen nach 0.4.0: Befehl `multigpt-runner images add` (Image-Freigabe beim Agent), inkrementelle Bundles, Container-Logs auf der Seite „Runner“, Docker (8h), Bremse bei wiederholtem Runner-Fehler (C37), Python-Agent `runner_agent/` entfernen (nur noch Referenz/Testorakel), Volumen `/data` je Benutzer (Stufe 3), `runner-rs` auf crates.io (Name `multigpt-runner` frei, nichts veröffentlichen ohne Nutzer).
Erledigte Punkte werden gestrichen bzw. entfernt, neue unten in der passenden Gruppe ergänzt.
Nachtrag 2026-10-21 (nach 0.4.0, Version offen, vom Nutzer festzulegen): Bilder über Eliezer (Bildmodelle, Bearbeiten, Weiterarbeiten, eigene Auflösung), Löschen einzelner Versionen, Anbindung an Eliezer (M16-01). Neue Punkte C38–C44 und Meilensteine unter D.
Entschieden und erledigt bzw. ohne Arbeit (2026-10-12): A3 nein, A5 nein, A8 ja, A19 so lassen; umgesetzt für 0.3.6: A4 (`ask`-Chats im Projekt „API“), A10 (EUR-Konten auf USD umstellen, Admin-Aktion/Befehl), A11 (EZB-Kurs automatisch, Standard an), A15 (Mitteilungen an den Besitzer), A16 (scipy); A14 entschieden (ASGI-Dienst), Umsetzungsplan in Implementierung.md M14; A6 ja (Produktion hat die /mcp-Blöcke bereits), A13 Empfehlungen 7a–7l übernommen, A20 so lassen. Offen bleiben in A nur zurückgestellte Punkte (A12, A17, A24).

## A. Entscheidungen des Nutzers

| Nr. | Thema | Frage | Empfehlung | Entscheidung |
|---|---|---|---|---|
| A12 | Kosten | Kosten für Embeddings/OCR/Bildbeschreibung buchen? | sobald dort Cloud-Modelle genutzt werden | sobald dort Cloud-Modelle genutzt werden |
| A17 | Darstellung | Mermaid (ca. 3 MB, nur nachgeladen, als `<img>`)? | erst bei Bedarf | erst bei Bedarf |
| A24 | Modellverwaltung | Zeitfenster „Indexierung nachts“ (große Läufe nur zu festen Zeiten)? | erst, wenn der Chat während Läufen stört | erst, wenn der Chat während Läufen stört |

## B. Klärung im Betrieb

| Nr. | Punkt |
|---|---|
| B1 | „failed to parse grammar“: lief der Chat auf der Produktion 0.3.0? Mit 0.3.1 nicht reproduzierbar. |
| B2 | qwen3-coder-30b bricht in LM Studio mit „terminated“ ab – Speicher bzw. Kontext in LM Studio prüfen. |
| B3 | Wer hat die Entwicklungs-DB zwischenzeitlich auf chat 0028 migriert? Ungeklärt. |
| B5 | LM Studio nach Neustart des LM-Studio-Rechners: `llmster` lauschte nur auf 127.0.0.1. Behoben mit `lms server start --bind 0.0.0.0` bzw. GUI; dauerhaft per systemd-Unit oder `LMS_SERVER_HOST=0.0.0.0`; Firewall auf 192.168.24.250 beschränken. In LM Studio Auto-Evict („JIT models auto-evict“) und `logSensitiveData` aus – per GUI, die Datei wurde nicht übernommen. Beim nächsten Neustart des LM-Studio-Rechners prüfen. |
| B4 | Nach der Installation: Preise der Cloud-Modelle eintragen, Bildmodell setzen, „Alles neu indexieren“, „Sandbox testen“, bei OpenAI/Anthropic „Online-Status prüfen“ abschalten. |
| B6 | n8n nach Neustart des Servers aus (rootless Podman-Compose `~/n8n-ai`, kein Autostart): seit 2026-10-12 `systemctl --user enable podman-restart.service` (startet alle Container mit `restart=always`, Linger ist an). Offen: `n8n-sandbox-api` startet nicht – `SANDBOX_API_RUNNER_REGISTRATION_TOKEN` fehlt in `compose.yaml`; n8n meldet veraltete Einstellungen (`N8N_RUNNERS_MODE` intern, Python-Runner fehlt). |
| B7 | Produktion nach dem Update auf 0.3.5: „Modelle verwalten“ beim Anbieter LM Studio (Budget 24, Reserve 2, gpt-oss Priorität 60) – nur auf einer Instanz (C33); Cache-Preise der Anthropic-Modelle (C32); beim OCR-Modell „Parallele Anfragen“ passend zu „Dokumente gleichzeitig“ (C31). |

## C. Bekannte Lücken (kleine Folgeschritte)

| Nr. | Punkt |
|---|---|
| C2 | Bilder und PDFs aus Werkzeugen erscheinen erst nach Ende der Antwort (kein Live-Event). |
| C5 | Arbeitsblätter: Hundertertafel, Zwanzigerfeld, Stellenwerttafel, Geobrett fehlen; Antwortlinien ungleich kräftig; PDF-Link fehlt in der Live-Anzeige (`attachments.js`). |
| C6 | Projekte lassen sich noch nicht als Ganzes teilen. |
| C8 | Ein abgelaufener Key, der erst mitten im Chat auffällt, schaltet den Anbieter nicht offline. |
| C9 | `gpt-3.5-turbo` gilt in der Heuristik als ohne Werkzeuge (abgekündigt). |
| C10 | Lokale Reasoning-Schalter außer gpt-oss (z. B. Qwen3 `enable_thinking`) nicht unterstützt. |
| C11 | Gemerkte Parameter-Ablehnungen (Temperatur, Denktiefe) gelten nur bis zum Neustart, je Prozess. |
| C12 | Nicht im Browser geprüft: Denktiefe-Auswahl, MCP-Werkzeugtabelle, API-Key-Seite, Zitat kopieren. Chromium ungetestet (SVG-Vorschau, Layout-Fix). |
| C13 | Sandbox: Speichergrenze nur je Prozess, kein Gesamtlimit (bräuchte cgroup). |
| C14 | Docker-Image: Sandbox abgeschaltet, bubblewrap fehlt im Dockerfile. |
| C15 | Kontenrahmen: Batch-/Flex-Tarife, Gemini-Cache-Speicher und Freikontingente nicht abgebildet. |
| C16 | Sandbox-Tests scheitern unter Last der vollen Suite gelegentlich an der CPU-Grenze (siehe A7). |
| C17 | MCP-SSE: Ein beendeter Stream (nach 30 Min., Neustart, Widerruf) wird nur von Clients mit automatischem Neuverbinden überbrückt; mit n8n, mcp-remote und Claude Desktop nicht im Betrieb geprüft. |
| C18 | MCP-SSE: Der SDK-Client liest SSE-Events nur bis 1 MB. Seit 0.3.3 hält MultiGPT über SSE eingebettete Daten unter 700 KB je Ergebnis (Bilder verkleinert, sonst `resource_link`; `get_file` zu groß → Verweis mit Hinweis auf Streamable HTTP). Offen: große Dateien über SSE gar nicht abholbar; Grenze nur am Python-SDK gemessen, nicht an n8n, mcp-remote, Claude Desktop. |
| C19 | MCP-SSE: Ein Client-Abbruch während eines laufenden `tools/call` (z. B. `ask`) bricht das Werkzeug nicht ab (der POST-Thread merkt die Trennung des Streams nicht). |
| C20 | MCP-SSE: Trennt der Client, merkt der Server das erst beim nächsten Keepalive (bis 15 s); bis dahin zählt der Stream gegen die Grenzen. |
| C21 | Erkennungsliste: `chat-latest` (gpt-5-chat) gilt als ohne Bild-Eingabe; laut OpenAI versteht es Bilder (auf der Dev-DB von Hand korrigiert). |
| C22 | Kontextmenü (M13-01): offen sind „Ins Scratchpad“ (M13-05), eigene Vorlagen (M13-02), Knopf „⋯“ je Nachricht für die ganze Nachricht, die Abschnittsansicht `collections/chunk.html` und „Nachfragen“ aus nur lesbaren Chats in einem neuen Chat (Frage 7c). |
| C23 | Kontextmenü und Mitscrollen nur in headless Firefox geprüft (Auswahl per Skript, Mausrad, Bild auf, Touch nicht echt); langes Tippen am Handy und Chromium ungetestet. |
| C24 | Token-Schätzung (M4a-12) nur an gpt-oss-20b in LM Studio kalibriert (n8n-Schemas 1,5-fach überschätzt); Cloud-Anbieter ungeprüft, deren Kontextlänge nur per Admin-Feld. |
| C25 | find_tools: Stichwortliste deutsch → englisch klein; Zustandsfragen („Ist das Licht im Flur an?“) treffen eher HassTurnOn als GetLiveContext (HASS geht als kleiner Server ohnehin ganz mit). Beispiel Chat 21 (2026-10-12): „Zeichne die Funktion x²“ – gpt-oss behauptete, keine Grafik erzeugen zu können; erst „Nimm Python“ führte zu run_python (Stichworte Zeichnen/Plotten/Diagramm fehlen). |
| C26 | `![…](attachment:NAME)` bzw. `anhang:ID` im Antworttext: seit 0.3.5 im Browser geprüft (Firefox) – ein im Text gezeigtes Bild verschwindet aus der Anhangsliste (vorher doppelt), Klick öffnet die Großansicht. Offen: während des Streams ausgeblendet (sichtbar nach dem Nachladen); in PDFs (`create_pdf`) gilt weiter nur `anhang:ID`; Chromium ungetestet. |
| C27 | Modellverwaltung: „wird geladen …“ steht nur in der Statuszeile des Chats und im Admin, nicht in der Modellauswahl bzw. der Statusanzeige oben. |
| C28 | Modellverwaltung am echten LM Studio (0.4, 2026-10-12): Laden mit `context_length` und `parallel` wird angenommen und per `echo_load_config` bestätigt (olmOCR `parallel: 2`); Laden dauert 0,4 s (nomic) bzw. 1,8 s (olmOCR). **Embedding-Modelle lehnen `parallel` ab** (HTTP 400) – MultiGPT schickt es dort nicht mehr. Offen: Fehlermeldung bei zu wenig Grafikspeicher (nicht getestet, um ein laufendes gpt-oss nicht zu verdrängen). |
| C29 | Speicherschätzung nicht an echten Messwerten kalibriert (Rechenpuffer pauschal 0,5 GB, KV-Cache f16); Architekturtabelle nur für gängige Modelle, sonst konservative Näherung. Abhilfe: Messwert am Modell eintragen. |
| C30 | Modellverwaltung: Bild-Erzeugung und Anhänge-Beschreibung lokaler Modelle laufen nicht über die Verwaltung (LM Studio erzeugt keine Bilder). Eine laufende Chat-Antwort hält ihr Modell belegt; ein OCR-Lauf mit Tesseract als Ersatz weicht bei Speichermangel still auf Tesseract aus. |
| C31 | Parallele Indexierung seit 0.3.5 (RAG-Einstellung „Dokumente gleichzeitig“, Standard 2): zuerst als Threads gebaut – gemessen ohne Gewinn, weil der GIL sie auf einen Kern zwang; jetzt eigene Prozesse (`run_worker --lane N`). Durchsatz noch nicht sauber gemessen (Messungen gestört durch apt-Upgrade, parallele Tests und entladene Modelle, C33). Auf der Produktion beobachten: Dauer je Dokument, CPU, LM-Studio-Last. |
| C32 | Prompt-Caching: Anthropic seit 0.3.4 (Breakpoint am Systemprompt plus automatisches Caching des Verlaufs, 5 Min.). Offen: nicht gegen die echte API geprüft (kein Anthropic-Anbieter in der Entwicklung) – auf der Produktion `cache_read_input_tokens` in den Buchungen beobachten; Cache-Preise an den Anthropic-Modellen eintragen; 1-Stunden-Haltezeit für Chats mit langen Pausen erwägen; OpenAI `prompt_cache_key` je Chat für bessere Trefferquote. |
| C33 | Zwei MultiGPT-Installationen an einem LM Studio: seit 0.3.6 entlädt jede nach Leerlauf/Indexierung nur selbst geladene Instanzen (`ModelRuntime.loaded_instance`); beobachtet 2026-10-12 – Produktion und Entwicklung verwalteten beide, olmOCR wurde im Minutentakt geladen und entladen, OCR fiel auf Tesseract. Die OCR wiederholt bei „Model is unloaded“ bzw. „Model unloaded by user or API request“ einmal. Offen: Platz schaffen (Verdrängen für eine wartende Anfrage) trifft weiterhin fremde Instanzen; Chat und Embeddings wiederholen nicht. |
| C34 | Persönliches Menü und Profilbild (0.3.5): Profilbild erscheint nur in der Kopfzeile, noch nicht an Nachrichten in geteilten Chats; Verwalter können fremde Profilbilder nicht entfernen; nur in Firefox geprüft (Chromium, Handy mit Touch offen). „Mein Verbrauch“ und „Familie“ stehen nur noch im Menü, nicht mehr in der Seitenleiste. |
| C35 | USD-Umstellung (A10): Eine Antwort, die vor der Umstellung begann und danach gebucht wird, rechnet mit der alten EUR-Preisversion, wird aber als USD gebucht (`ModelPrice` hat kein eigenes Währungsfeld). Seltener Randfall; Abhilfe wäre `ModelPrice.currency` (Migration). |
| C36 | erledigt: `read_chat`/`list_chats` sind seit 0.3.6 auf der Produktion, Key `multigpt-prod` mit `chat.read` eingetragen. |
| C37 | Runner (Test 2026-10-13 auf WS-JOERG): Scheitert der Sitzungsstart, versucht das Modell es immer wieder (5× in einer Antwort, ~22 000 Tokens). Bremse: nach zwei gleichen Fehlern derselben Ursache keine weiteren Runner-Aufrufe in dieser Antwort, klare Meldung an den Nutzer. Außerdem: Python-Agent zieht fehlende Images nicht (nur Rust behoben) – entfällt mit dem Python-Agent. |
| C38 | Bilder (M9-02, Rest): Maske auf einer Zeichenfläche, Varianten, Werkzeug `edit_image` für Chatmodelle (Knopf „Weiter bearbeiten“ erledigt). Der Knopf „Weiter bearbeiten“ am erzeugten Bild ist da (kopiert das Bild als Entwurf in die Anhangsleiste, `POST /api/attachments/<id>/copy/`). |
| C39 | Bildmodus nur per Tests und Lesen abgesichert, nie im Browser gesehen: Modellwahl, Auflösung, Weiterarbeiten, Knopf unter abgelehnten Bildwünschen, Löschknöpfe (Antwort-Version, Frage-Version). Sichtlauf nach dem Deploy (Firefox, Chromium, 375 px). Das JavaScript hat keine automatischen Tests. |
| C40 | Eigene Auflösung: Bilder über 2 Megapixel sind hochgerechnet (mittig zugeschnitten, Lanczos), nicht neu berechnet; ein Upscaler-Modell (Real-ESRGAN o. ä.) als Dienst bei Eliezer wäre der nächste Schritt. Nur bei Anbietern mit beliebigen Größen (Eliezer/LM Studio). |
| C41 | „Am letzten Bild weiterarbeiten“ ist standardmäßig an und gilt für Nachrichten bis 200 Zeichen (`images.CONTINUE_MAX_CHARS`); längere sind ein neues Bild. Offen: je Chat/Modell einstellbar? (Das Aufräumen der Versionen geht jetzt: „Alle anderen Versionen löschen“ in der Versionsleiste.) |
| C43 | Eliezer-Abgleich: `device` (GPU/CPU je Modell) wird bei `load`/`estimate` nicht mitgeschickt (Eliezer wählt `auto`); die Hub-Suche (`/api/v1/hub/search`, `/api/v1/hub/models/…`) ist ungenutzt; die Seite „geladene Modelle“ rechnet bei LM Studio weiter mit `vram.py`. Fähigkeiten (Werkzeuge) meldet Eliezer nicht: Namensregel (`guess_tools`), z. B. Qwen3-VL und Gemma 4 stehen auf „nein“. |
| C44 | Prod-Chat 31 (2026-10-20): Mistral 7B antwortete mit einer Hugo-/nginx-Anleitung statt auf die Frage; vermutlich ein Systemtext, den Eliezer bei Vorlagen ohne System-Rolle in die erste Nutzernachricht schiebt. Ungeklärt, welcher Text es war. Modell `qwen3-vl-8b-abliterated` (Eliezer, ohne Sicherheitsfilter): nur den Rollen freigeben, für die es gedacht ist. |

## D. Geplante Meilensteine

| Meilenstein | Stand |
|---|---|
| M9-02 Bilder bearbeiten | teilweise: Bilder anhängen und verändern (`/images/edits`, OpenAI und Eliezer), Weiterarbeiten am letzten Bild, eigene Auflösung; offen: Maske, Varianten, Werkzeug `edit_image` (C38); Knopf „Weiter bearbeiten“ erledigt |
| M9-03 `mcp_imagetools` (Pillow: Zuschneiden, Skalieren, Drehen, Text, Collage) | offen, Verzeichnis existiert nicht |
| FLUX.1 über Rust-Runner | Planung 2026-10-17: GPU-Container auf WS-Joerg; Laufzeit im Image, Gewichte/Cache in Volumes, Ergebnisse als Chat-Versionen in MultiGPT. Bestandsaufnahme von `~/D/f/flux1-project` offen. [Plan und Umsetzungsschritte](plans/FLUX-Runner.md). |
| M10 Sprache (Spracherkennung, Vorlesen) | offen |
| M11 Musik | Umfang offen |
| M12 Betrieb (Rest) | teilweise |
| M13 Scratchpad | M13-01 Kontextmenü umgesetzt (0.3.2); Fragen 7a–7l entschieden (Empfehlungen), Umsetzung offen |
| M14 Runner | **Alle Fragen entschieden (2026-10-13)**, nächster Schritt: Umsetzung. Runner = Coding-Werkzeug wie Claude Code im Container; git-Bundle in der DB, tmpfs; ein Vorschau-Port nur an 127.0.0.1 mit Token; ssh/telnet nur zu freigegebenen Hosts mit Rückfrage; Agententeam über M15-Keys (`runner.use`, ohne Netz). Einzelheiten Implementierung.md M14 |
| M16 Eliezer (Ersatz für LM Studio) | Eliezer ist ein eigenes Projekt ([Beerlesklopfer/eliezer](https://github.com/Beerlesklopfer/eliezer), Roadmap dort). Angebunden: Schnittstelle „LM Studio“ mit `estimate`/`status`, Modellliste mit Denkoptionen, Embeddings, Bildmodelle, Bearbeiten. Offen: Ablösung von LM Studio je Anbieter, Betrieb über den Runner (`GpuLease`), Fähigkeiten-Meldung (Werkzeuge). [Plan](plans/Eliezer.md). |
