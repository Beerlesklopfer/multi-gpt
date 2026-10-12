# Liste offener Punkte (LOP)

Stand: 2026-10-12, nach 0.3.3 (0.3.4 lokal: Entladen nach Indexierung, Prompt-Caching, Eingabe-Historie, Antwort löschen). Modellverwaltung (A21–A23) entschieden: 24 GB Grafikspeicher, ein Monitor (Reserve 2 GB); Indexierungsmodelle nur für den Lauf laden, danach entladen (statt nomic anzuheften).
Erledigte Punkte werden gestrichen bzw. entfernt, neue unten in der passenden Gruppe ergänzt.

## A. Entscheidungen des Nutzers

| Nr. | Thema | Frage | Empfehlung |
|---|---|---|---|
| A2 | n8n „Pflicht“ | Zusätzlich Warnung in der Statusleiste für Verwalter, wenn n8n offline ist? | ja, als Warnung |
| A3 | API-Keys | Keys auch für Jugendliche? | vorerst nein |
| A4 | API-Keys | `ask`-Chats in einem Projekt „API“ sammeln? | ja, als Option je Key |
| A5 | API-Keys | Werkzeug-Rückfragen über die API bestätigen? | nein, nur in MultiGPT |
| A6 | nginx | Eigene Änderungen an der Site: `location`-Blöcke für `/mcp/` und `/mcp/sse` beim Update übernehmen | Bestätigung genügt |
| A8 | Projekte | Projekt-Anweisungen gelten auch für Empfänger in geteilten Chats – bestätigen? | ja |
| A10 | Kosten | EUR-Bestandskonten (Altpreise) auf USD umziehen? | ja, beim nächsten Preis-Update |
| A11 | Kosten | EZB-Kurs automatisch abrufen? | ja, wenn der Server ins Internet darf |
| A12 | Kosten | Kosten für Embeddings/OCR/Bildbeschreibung buchen? | sobald dort Cloud-Modelle genutzt werden |
| A13 | M13 Scratchpad | Fragen 7a–7l (siehe Implementierung.md, M13) | Empfehlungen übernehmen |
| A14 | M14 Runner | Rückkanal: eigener ASGI-/WebSocket-Dienst oder Long-Polling? | ASGI-Dienst |
| A15 | Chat teilen | Besitzer benachrichtigen, wenn ein Empfänger löscht (Chat oder seit 0.3.4 eine Antwort samt Folgeverlauf, Recht D)? | später |
| A16 | Berechnungen | scipy aufnehmen (ca. 100 MB)? | nur bei Bedarf |
| A17 | Darstellung | Mermaid (ca. 3 MB, nur nachgeladen, als `<img>`)? | erst bei Bedarf |
| A19 | Kontextmenü (M13-01) | Herkunft eigener Nachrichten im Zitat „Ich:“ statt „Du:“ (das Modell würde „Du“ auf sich beziehen); „Übersetzen“ flach (ins Deutsche, deutscher Text ins Englische) statt Untermenü mit Sprachen – so lassen? | ja |
| A20 | Werkzeuge nach Bedarf (M4a-12) | Vorgabe „automatisch“ gilt auch für Cloud-Modelle (gpt-5 sieht n8n dann nur über find_tools bzw. Vorauswahl; spart je Anfrage rund 8 000 Tokens) – so lassen oder Cloud-Modelle auf „nie“? | so lassen |
| A24 | Modellverwaltung | Zeitfenster „Indexierung nachts“ (große Läufe nur zu festen Zeiten)? | erst, wenn der Chat während Läufen stört |

## B. Klärung im Betrieb

| Nr. | Punkt |
|---|---|
| B1 | „failed to parse grammar“: lief der Chat auf der Produktion 0.3.0? Mit 0.3.1 nicht reproduzierbar. |
| B2 | qwen3-coder-30b bricht in LM Studio mit „terminated“ ab – Speicher bzw. Kontext in LM Studio prüfen. |
| B3 | Wer hat die Entwicklungs-DB zwischenzeitlich auf chat 0028 migriert? Ungeklärt. |
| B5 | LM Studio nach Neustart des Rechners: `llmster` lauschte nur auf 127.0.0.1 (Einstellung „im lokalen Netz“ nicht übernommen). Behoben mit `lms server start --bind 0.0.0.0` bzw. GUI; dauerhaft per systemd-Unit oder `LMS_SERVER_HOST=0.0.0.0`; Firewall auf 192.168.24.250 beschränken. In LM Studio Auto-Evict (`unloadPreviousJITModelOnLoad`) und `logSensitiveData` aus. Beim nächsten Neustart prüfen. |
| B4 | Nach der Installation: Preise der Cloud-Modelle eintragen, Bildmodell setzen, „Alles neu indexieren“, „Sandbox testen“, bei OpenAI/Anthropic „Online-Status prüfen“ abschalten. |

## C. Bekannte Lücken (kleine Folgeschritte)

| Nr. | Punkt |
|---|---|
| C1 | „Neu erzeugen“ an einer Bild-Antwort nutzt das Chatmodell statt des Bildmodells. |
| C2 | Bilder und PDFs aus Werkzeugen erscheinen erst nach Ende der Antwort (kein Live-Event). |
| C3 | Eingeklappte Eingabeleiste zeigt den Modus „Bild“ nicht an. |
| C4 | gpt-image wird je Stück abgerechnet, nicht nach Tokens (`book_attachment` ohne Token-Parameter). |
| C5 | Arbeitsblätter: Hundertertafel, Zwanzigerfeld, Stellenwerttafel, Geobrett fehlen; Antwortlinien ungleich kräftig; PDF-Link fehlt in der Live-Anzeige (`attachments.js`). |
| C6 | Projekte lassen sich noch nicht als Ganzes teilen. |
| C7 | Besitzer wird bei Löschen/Archivieren durch Empfänger nicht benachrichtigt. |
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
| C25 | find_tools: Stichwortliste deutsch → englisch klein; Zustandsfragen („Ist das Licht im Flur an?“) treffen eher HassTurnOn als GetLiveContext (HASS geht als kleiner Server ohnehin ganz mit). |
| C26 | `![…](attachment:NAME)` bzw. `anhang:ID` im Antworttext: seit 0.3.5 im Browser geprüft (Firefox) – ein im Text gezeigtes Bild verschwindet aus der Anhangsliste (vorher doppelt), Klick öffnet die Großansicht. Offen: während des Streams ausgeblendet (sichtbar nach dem Nachladen); in PDFs (`create_pdf`) gilt weiter nur `anhang:ID`; Chromium ungetestet. |
| C27 | Modellverwaltung: „wird geladen …“ steht nur in der Statuszeile des Chats und im Admin, nicht in der Modellauswahl bzw. der Statusanzeige oben. |
| C28 | Modellverwaltung am echten LM Studio (0.4, 2026-10-12): Laden mit `context_length` und `parallel` wird angenommen und per `echo_load_config` bestätigt (olmOCR `parallel: 2`); Laden dauert 0,4 s (nomic) bzw. 1,8 s (olmOCR). **Embedding-Modelle lehnen `parallel` ab** (HTTP 400) – MultiGPT schickt es dort nicht mehr. Offen: Fehlermeldung bei zu wenig Grafikspeicher (nicht getestet, um ein laufendes gpt-oss nicht zu verdrängen). |
| C29 | Speicherschätzung nicht an echten Messwerten kalibriert (Rechenpuffer pauschal 0,5 GB, KV-Cache f16); Architekturtabelle nur für gängige Modelle, sonst konservative Näherung. Abhilfe: Messwert am Modell eintragen. |
| C30 | Modellverwaltung: Bild-Erzeugung und Anhänge-Beschreibung lokaler Modelle laufen nicht über die Verwaltung (LM Studio erzeugt keine Bilder). Eine laufende Chat-Antwort hält ihr Modell belegt; ein OCR-Lauf mit Tesseract als Ersatz weicht bei Speichermangel still auf Tesseract aus. |
| C31 | Parallele Indexierung seit 0.3.5 (RAG-Einstellung „Dokumente gleichzeitig“, Standard 2): nur mit Tests geprüft, nicht am echten Lauf (Produktion indexierte gerade). Auf der Produktion beobachten: Dauer je Dokument, LM-Studio-Last; beim OCR-Modell „Parallele Anfragen“ passend setzen. |
| C32 | Prompt-Caching: Anthropic seit 0.3.4 (Breakpoint am Systemprompt plus automatisches Caching des Verlaufs, 5 Min.). Offen: nicht gegen die echte API geprüft (kein Anthropic-Anbieter in der Entwicklung) – auf der Produktion `cache_read_input_tokens` in den Buchungen beobachten; Cache-Preise an den Anthropic-Modellen eintragen; 1-Stunden-Haltezeit für Chats mit langen Pausen erwägen; OpenAI `prompt_cache_key` je Chat für bessere Trefferquote. |

## D. Geplante Meilensteine

| Meilenstein | Stand |
|---|---|
| M9-02 Bilder bearbeiten (Maske) | skizziert |
| M10 Sprache (Spracherkennung, Vorlesen) | offen |
| M11 Musik | Umfang offen |
| M12 Betrieb (Rest) | teilweise |
| M13 Scratchpad | M13-01 Kontextmenü umgesetzt (0.3.2); Rest geplant, wartet auf A13 |
| M14 Runner | geplant, wartet auf A14 |
