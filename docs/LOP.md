# Liste offener Punkte (LOP)

Stand: 2026-10-11, nach 0.3.1 (veröffentlicht) und M15 „Knoten“ (lokal, erscheint als 0.3.2).
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
| A15 | Chat teilen | Besitzer benachrichtigen, wenn ein Empfänger löscht? Einzelne Nachrichten löschen? | später |
| A16 | Berechnungen | scipy aufnehmen (ca. 100 MB)? | nur bei Bedarf |
| A17 | Darstellung | Mermaid (ca. 3 MB, nur nachgeladen, als `<img>`)? | erst bei Bedarf |
| A19 | Kontextmenü (M13-01) | Herkunft eigener Nachrichten im Zitat „Ich:“ statt „Du:“ (das Modell würde „Du“ auf sich beziehen); „Übersetzen“ flach (ins Deutsche, deutscher Text ins Englische) statt Untermenü mit Sprachen – so lassen? | ja |
| A20 | Werkzeuge nach Bedarf (M4a-12) | Vorgabe „automatisch“ gilt auch für Cloud-Modelle (gpt-5 sieht n8n dann nur über find_tools bzw. Vorauswahl; spart je Anfrage rund 8 000 Tokens) – so lassen oder Cloud-Modelle auf „nie“? | so lassen |
| A21 | Modellverwaltung (M7-09, 0.3.2) | Wie viel Reserve braucht der Monitor an der LM-Studio-Grafikkarte (Auflösung, Anzahl Monitore, Desktop-Effekte)? | 2 GB (Standard); bei 4K bzw. zwei Monitoren 3 GB; Wert aus `nvidia-smi` ohne geladene Modelle ablesen |
| A22 | Modellverwaltung | Chatmodell gpt-oss-20b anheften? | nein: Priorität 60 statt anheften (Indexierung verdrängt es dann nie, ein anderes Chatmodell schon); nomic anheften |
| A23 | Modellverwaltung | Budget für die Karte des LM-Studio-Rechners (Größe der Grafikkarte)? „Modelle verwalten“ ist bis dahin aus. | Grafikspeicher laut `nvidia-smi` in GB eintragen, dann einschalten und JIT/Auto-Evict in LM Studio abschalten |
| A24 | Modellverwaltung | Zeitfenster „Indexierung nachts“ (große Läufe nur zu festen Zeiten)? | erst, wenn der Chat während Läufen stört |

## B. Klärung im Betrieb

| Nr. | Punkt |
|---|---|
| B1 | „failed to parse grammar“: lief der Chat auf der Produktion 0.3.0? Mit 0.3.1 nicht reproduzierbar. |
| B2 | qwen3-coder-30b bricht in LM Studio mit „terminated“ ab – Speicher bzw. Kontext in LM Studio prüfen. |
| B3 | Wer hat die Entwicklungs-DB zwischenzeitlich auf chat 0028 migriert? Ungeklärt. |
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
| C18 | MCP-SSE: Der SDK-Client liest SSE-Events nur bis 1 MB; `get_file` mit großen Dateien (base64) scheitert dort über SSE – dann Streamable HTTP nutzen. |
| C19 | MCP-SSE: Ein Client-Abbruch während eines laufenden `tools/call` (z. B. `ask`) bricht das Werkzeug nicht ab (der POST-Thread merkt die Trennung des Streams nicht). |
| C20 | MCP-SSE: Trennt der Client, merkt der Server das erst beim nächsten Keepalive (bis 15 s); bis dahin zählt der Stream gegen die Grenzen. |
| C21 | Erkennungsliste: `chat-latest` (gpt-5-chat) gilt als ohne Bild-Eingabe; laut OpenAI versteht es Bilder (auf der Dev-DB von Hand korrigiert). |
| C22 | Kontextmenü (M13-01): offen sind „Ins Scratchpad“ (M13-05), eigene Vorlagen (M13-02), Knopf „⋯“ je Nachricht für die ganze Nachricht, die Abschnittsansicht `collections/chunk.html` und „Nachfragen“ aus nur lesbaren Chats in einem neuen Chat (Frage 7c). |
| C23 | Kontextmenü und Mitscrollen nur in headless Firefox geprüft (Auswahl per Skript, Mausrad, Bild auf, Touch nicht echt); langes Tippen am Handy und Chromium ungetestet. |
| C24 | Token-Schätzung (M4a-12) nur an gpt-oss-20b in LM Studio kalibriert (n8n-Schemas 1,5-fach überschätzt); Cloud-Anbieter ungeprüft, deren Kontextlänge nur per Admin-Feld. |
| C25 | find_tools: Stichwortliste deutsch → englisch klein; Zustandsfragen („Ist das Licht im Flur an?“) treffen eher HassTurnOn als GetLiveContext (HASS geht als kleiner Server ohnehin ganz mit). |
| C26 | `![…](anhang:ID)` im Antworttext: Auflösung in `markdown.js` nicht im Browser geprüft (kein Node in der Entwicklung); während des Streams ausgeblendet, sichtbar nach dem Nachladen der Nachricht. |
| C27 | Modellverwaltung: „wird geladen …“ steht nur in der Statuszeile des Chats und im Admin, nicht in der Modellauswahl bzw. der Statusanzeige oben. |
| C28 | Modellverwaltung: Laden und Entladen nur gegen nachgebildetes LM Studio getestet; am echten Gerät (0.4.x) prüfen, ob `parallel` beim Laden angenommen wird (nicht dokumentiert) und welche Fehlermeldung LM Studio bei zu wenig Speicher liefert. |
| C29 | Speicherschätzung nicht an echten Messwerten kalibriert (Rechenpuffer pauschal 0,5 GB, KV-Cache f16); Architekturtabelle nur für gängige Modelle, sonst konservative Näherung. Abhilfe: Messwert am Modell eintragen. |
| C30 | Modellverwaltung: Bild-Erzeugung und Anhänge-Beschreibung lokaler Modelle laufen nicht über die Verwaltung (LM Studio erzeugt keine Bilder). Eine laufende Chat-Antwort hält ihr Modell belegt; ein OCR-Lauf mit Tesseract als Ersatz weicht bei Speichermangel still auf Tesseract aus. |
| C31 | Indexierung: Der Worker bearbeitet ein Dokument nach dem anderen (~40 s je Buch mit Textebene). Mehrere Dokumente gleichzeitig (z. B. 2–4, begrenzt durch die Parallelität in LM Studio) würde große Verzeichnisquellen deutlich schneller machen. Parallele OCR-Seiten lohnen bei Text-PDFs kaum. |

## D. Geplante Meilensteine

| Meilenstein | Stand |
|---|---|
| M9-02 Bilder bearbeiten (Maske) | skizziert |
| M10 Sprache (Spracherkennung, Vorlesen) | offen |
| M11 Musik | Umfang offen |
| M12 Betrieb (Rest) | teilweise |
| M13 Scratchpad | M13-01 Kontextmenü umgesetzt (0.3.2); Rest geplant, wartet auf A13 |
| M14 Runner | geplant, wartet auf A14 |
