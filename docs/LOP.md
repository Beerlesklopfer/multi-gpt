# Liste offener Punkte (LOP)

Stand: 2026-10-10, nach 0.3.1 (veröffentlicht) und M15 „Knoten“ (lokal, erscheint als 0.3.2).
Erledigte Punkte werden gestrichen bzw. entfernt, neue unten in der passenden Gruppe ergänzt.

## A. Entscheidungen des Nutzers

| Nr. | Thema | Frage | Empfehlung |
|---|---|---|---|
| A2 | n8n „Pflicht“ | Zusätzlich Warnung in der Statusleiste für Verwalter, wenn n8n offline ist? | ja, als Warnung |
| A3 | API-Keys | Keys auch für Jugendliche? | vorerst nein |
| A4 | API-Keys | `ask`-Chats in einem Projekt „API“ sammeln? | ja, als Option je Key |
| A5 | API-Keys | Werkzeug-Rückfragen über die API bestätigen? | nein, nur in MultiGPT |
| A6 | nginx | Eigene Änderungen an der Site: `location`-Blöcke für `/mcp/` und `/mcp/sse` beim Update übernehmen | Bestätigung genügt |
| A7 | Sandbox | Standard-CPU-Zeit 10 s → 20–30 s? (sympy-Test scheitert unter Last) | ja, 30 s |
| A8 | Projekte | Projekt-Anweisungen gelten auch für Empfänger in geteilten Chats – bestätigen? | ja |
| A10 | Kosten | EUR-Bestandskonten (Altpreise) auf USD umziehen? | ja, beim nächsten Preis-Update |
| A11 | Kosten | EZB-Kurs automatisch abrufen? | ja, wenn der Server ins Internet darf |
| A12 | Kosten | Kosten für Embeddings/OCR/Bildbeschreibung buchen? | sobald dort Cloud-Modelle genutzt werden |
| A13 | M13 Scratchpad | Fragen 7a–7l (siehe Implementierung.md, M13) | Empfehlungen übernehmen |
| A14 | M14 Runner | Rückkanal: eigener ASGI-/WebSocket-Dienst oder Long-Polling? | ASGI-Dienst |
| A15 | Chat teilen | Besitzer benachrichtigen, wenn ein Empfänger löscht? Einzelne Nachrichten löschen? | später |
| A16 | Berechnungen | scipy aufnehmen (ca. 100 MB)? | nur bei Bedarf |
| A17 | Darstellung | Mermaid (ca. 3 MB, nur nachgeladen, als `<img>`)? | erst bei Bedarf |

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

## D. Geplante Meilensteine

| Meilenstein | Stand |
|---|---|
| M9-02 Bilder bearbeiten (Maske) | skizziert |
| M10 Sprache (Spracherkennung, Vorlesen) | offen |
| M11 Musik | Umfang offen |
| M12 Betrieb (Rest) | teilweise |
| M13 Scratchpad | geplant, wartet auf A13 |
| M14 Runner | geplant, wartet auf A14 |
