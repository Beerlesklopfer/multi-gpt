# Anbieter und Modelle

Verwalter pflegen KI-Anbieter (OpenAI, Anthropic, Google, OpenAI-kompatible wie LM Studio oder
OpenRouter) und ihre Modelle im Django-Admin unter **Chat → Anbieter** bzw. **Chat → KI-Modelle**.
Beim Anbieter steht unten die Tabelle seiner Modelle, die **Fähigkeiten-Matrix**; dieselben Spalten
lassen sich auch in der Liste **KI-Modelle** direkt ändern.

## Fähigkeiten

Jedes Modell hat genau eine **Hauptart** (Spalte „Fähigkeit“) und dazu Häkchen.

| Spalte | Wert | Bewirkt | Genutzt in |
|---|---|---|---|
| Fähigkeit | Chat | Erscheint in der Chat-Auswahl und im Vergleich. Nur Chat-Modelle kommen auch als OCR-Modell und Modell für Abbildungen (RAG) in Frage. | M3, M6, M7 |
| Fähigkeit | Bilderzeugung | Erzeugt Bilder aus Text (z. B. `gpt-image-1`, `dall-e-3`, `gemini-2.5-flash-image`). Nicht in der Chat-Auswahl. | M9-01 |
| Fähigkeit | Embedding | Vektoren für die Dokumentsuche. Nur in der Auswahl „Embedding-Modell“. | M7 |
| Fähigkeit | Spracherkennung | Sprache zu Text (z. B. `whisper-1`, `gpt-4o-transcribe`). | M10-01 |
| Fähigkeit | Sprachausgabe | Text vorlesen (z. B. `tts-1`, `gpt-4o-mini-tts`). | M10-02 |
| Fähigkeit | Musik | Musik erzeugen (z. B. Googles Lyria). | M11 |
| Werkzeuge | Häkchen | Das Modell kann Werkzeuge aufrufen: Websuche als Werkzeug (`web_search`), Dokumentsuche und -werkzeuge, MCP-Server. Ohne Häkchen bekommt es keine Werkzeuge; die Websuche geht dann nur über den Schalter (Suche vorab). | M4a, M7, M8 |
| Bilder verstehen | Häkchen | Bilder aus dem Chat dürfen an das Modell gehen (Bild-Eingabe). Ohne Häkchen lehnt MultiGPT Bildanhänge für dieses Modell ab. | Anhänge, RAG (Abbildungen) |
| Bilder bearbeiten | Häkchen | Das Modell kann vorhandene Bilder bearbeiten bzw. Varianten erzeugen. | M9-02 |
| MCP | kein / alle / ausgewählte | Welche MCP-Server das Modell nutzen darf, siehe unten. Wirkt nur mit „Werkzeuge“. | M4a |
| Werkzeuge nach Bedarf | Standard / automatisch / immer / nie | Ob das Modell alle Werkzeugbeschreibungen sieht oder nur einen Kern plus `find_tools`, siehe unten. „Standard“ = Chat-Einstellungen. | M4a-12 |
| Kontextfenster beim Laden | Vorschlag / 2 048 … 131 072 | Lade-Profil für lokale Modelle mit Modellverwaltung: Damit lädt MultiGPT das Modell in LM Studio. Angeboten werden nur Stufen bis zur Länge, die LM Studio meldet; daneben der erwartete Speicher. | M7-09 (0.3.2) |
| Speicher (Schätzung) | nur Anzeige | Modelldatei plus Kontext-Cache; „größer als das Budget“, wenn es nie hineinpasst. | M7-09 (0.3.2) |
| angeheftet | Häkchen | Wird nie automatisch entladen (z. B. das Embedding-Modell). | M7-09 (0.3.2) |

Ein eigenes Häkchen für Audio direkt im Chat gibt es nicht: Spracheingabe läuft über ein
Spracherkennungs-Modell, der Text landet im Eingabefeld (M10-01).

## Automatische Erkennung

Neue Modelle bekommen Hauptart, „Werkzeuge“ und „Bilder verstehen“ automatisch:

- **LM Studio** meldet die Fähigkeiten selbst (`GET /api/v0/models`, ab LM Studio 0.3.16):
  `type` `llm`, `vlm` (versteht Bilder) oder `embeddings` und `capabilities` mit `tool_use`.
  MultiGPT fragt das bei lokalen Anbietern einmal je Statusprüfung ab (alle 15 s, dabei auch die
  geladene Kontextlänge) und übernimmt die Fähigkeiten nur für neu anzulegende Modelle. Antwortet der Server nicht so (älteres LM Studio,
  Ollama, vLLM), gilt die Liste bekannter Modelle.
- **Sonst** schätzt MultiGPT aus der Modell-ID. Werkzeuge können u. a. OpenAI `gpt-4o`, `gpt-4.1`,
  `gpt-5.x`, `o3`, `o4-mini`; Anthropic ab `claude-3`; Google `gemini-1.5`, `2.x`, `3.x`; lokal
  `gpt-oss`, `qwen2.5`/`qwen3` (auch `-coder`), `llama-3.1` bis `3.3` und `4`, Mistral-Familie,
  `command-r`, Hermes, Granite 3+, Functionary, `deepseek-v3`. Keine Werkzeuge: Embedding-,
  OCR- (olmOCR) und reine Bildmodelle, Gemma, `deepseek-r1`-Destillate. Im Zweifel nein.

Das gilt überall, wo Modelle entstehen: Statusprüfung lokaler Anbieter, „Modelle auswählen“ beim
Anbieter, `mgpt-ctl sync_models` und die Auswahl in der Combobox „Modell-ID“ (sie belegt die
Häkchen vor, speichern muss der Verwalter).

**Bestehende Modelle** ändert MultiGPT nie von selbst, der Verwalter kann sie angepasst haben.
Zum Nachziehen:

- Admin, **KI-Modelle**, Modelle markieren, Aktion **„Fähigkeiten automatisch erkennen
  (Werkzeuge, Bilder)“**. Die Seite zeigt je Modell die Änderungen (z. B. „Werkzeuge: nein → ja“),
  gespeichert wird erst mit **„Übernehmen“**.
- Auf der Kommandozeile: `mgpt-ctl guess_capabilities` zeigt die Vorschau,
  `mgpt-ctl guess_capabilities --apply` speichert (optional `--provider <Name oder ID>`).

Beides ändert Hauptart, „Werkzeuge“ und „Bilder verstehen“, nicht aber die MCP-Freigabe.

## Werkzeuge nach Bedarf und Kontextlänge

Jedes angebotene Werkzeug kostet Platz im Kontext des Modells: Name, Beschreibung und
Parameter gehen bei jeder Anfrage mit. Mit Home Assistant (8 Werkzeuge), n8n (54) und den
eingebauten Werkzeugen sind das über 70 Beschreibungen, gemessen rund 16 000 bis 25 000 Tokens.
Ein lokales Modell mit 16 384 Tokens Kontext hat dann keinen Platz mehr für die Antwort.

**Werkzeuge nach Bedarf:** Das Modell bekommt nur einen kleinen Kern und das Werkzeug
`find_tools`. Damit sucht es weitere Werkzeuge nach Stichworten (auch deutsch: „Licht aus“,
„n8n-Ausführungen“) und kann die Treffer im nächsten Schritt aufrufen. Zum Kern gehören

- die **Basiswerkzeuge** aus den Chat-Einstellungen (Vorgabe `web_search`, `search_documents`,
  `create_pdf`),
- bei **gewählten Sammlungen** alle Dokumentwerkzeuge: `search_documents`, `read_document`,
  `list_documents` und `document_info`. Zum Zählen oder Auflisten („Zähle die Punkte je Gruppe“)
  muss das Modell ein Dokument ganz lesen können, die Suche liefert nur Ausschnitte,
- **kleine MCP-Server ganz** (höchstens 10 Werkzeuge und etwa 1 500 Tokens, z. B. Home
  Assistant). „Schalte das Licht aus“ geht so ohne Suche direkt an das passende Werkzeug.
  Große Server wie n8n erreicht das Modell über `find_tools`,
- **klare Treffer der Frage**: MultiGPT sucht vorab selbst mit der Frage des Nutzers. Passt ein
  Werkzeug deutlich („Waren alle Workflows auf n8n erfolgreich?“ → Ausführungen von n8n), geht
  es gleich mit.

`find_tools` findet nur, was das Modell ohnehin nutzen dürfte: Rechte der Rolle, MCP-Freigabe
des Modells, Auswahl im Chat, nur Server, die online sind. Rückfrage und Einstufung je Werkzeug
gelten unverändert (siehe [MCP-Server](MCP#werkzeuge-einstufen)). Ein Werkzeug, das nicht
angeboten bzw. nicht freigeschaltet wurde, führt MultiGPT nicht aus.

Einstellung: Admin, **Chat-Einstellungen → Werkzeuge**, je Modell überschreibbar in der Spalte
**Werkzeuge nach Bedarf**:

| Wert | Bedeutung |
|---|---|
| automatisch (Vorgabe) | Nach Bedarf, sobald es mehr als 12 Werkzeuge bzw. mehr als etwa 4 000 Tokens sind oder der Kontext des Modells knapp wird. Sonst gehen alle direkt mit. |
| immer | Immer nur Kern plus `find_tools`. |
| nie | Immer alle Werkzeuge, z. B. für Cloud-Modelle mit großem Kontext. |

**Kontextlänge:** LM Studio meldet unter `/api/v0/models` für geladene Modelle die
`loaded_context_length`. MultiGPT liest sie bei der Statusprüfung lokaler Anbieter mit; sie steht
beim Anbieter unter „Verbindung“ und im Tooltip der Statusanzeige oben. Für andere Modelle lässt
sie sich im Detailformular des Modells unter **Kontextlänge (Tokens)** eintragen. Ist sie bekannt,
schätzt MultiGPT vor dem Senden die Größe der Anfrage (grob nach Zeichen, eher etwas zu hoch)
und hält ein Viertel des Kontexts (2 048 bis 8 192 Tokens) für die Antwort frei. Wird es knapp:

1. Werkzeuge nach Bedarf (außer bei „nie“),
2. die ältesten Nachrichten des Chats weglassen (Hinweis in der Statuszeile),
3. sonst eine Fehlermeldung vor dem Senden mit der Schätzung.

Meldet LM Studio trotzdem „exceeds the available context size“, zeigt der Chat das mit den
Zahlen („Benötigt: 25 743 von 16 384 Tokens“). Liefert ein Modell gar keinen Text (Kontext voll,
Ausgabegrenze erreicht), gilt die Antwort als Fehler statt als leere Antwort.

**Kontextlänge in LM Studio erhöhen:** Mit Modellverwaltung (Anbieter → „Modelle verwalten“,
siehe [RAG einrichten → Modellverwaltung](RAG-Einrichtung#modellverwaltung-automatisch-laden-speicherbudget))
das **Kontextfenster beim Laden** in der Fähigkeiten-Matrix oder im Formular des Modells wählen
(gpt-oss-20b z. B. 32 768 statt 16 384); daneben steht, wie viel Grafikspeicher das voraussichtlich
braucht und ob es ins Budget passt. Der Wert gilt ab dem nächsten Laden – zum sofortigen Wechsel
auf „LM Studio: geladene Modelle“ das Modell entladen und laden. Ohne Modellverwaltung: in LM
Studio unter „Context Length“ bzw. mit `lms load <modell> --context-length 32768` neu laden.
MultiGPT übernimmt den geladenen Wert bei der nächsten Statusprüfung (spätestens nach 15 s).

## Lokale Modelle verwalten (LM Studio)

Am Anbieter (nur lokal, OpenAI-kompatibel, LM Studio ab 0.4) im Abschnitt
**Modellverwaltung (LM Studio)**:

| Feld | Standard | Bedeutung |
|---|---|---|
| Modelle verwalten | aus | MultiGPT lädt Modelle vor dem Gebrauch selbst (ohne TTL) und entlädt andere, wenn der Platz nicht reicht |
| Speicherbudget für Modelle (GB) | leer | Grafikspeicher, den LM Studio nutzen darf; Pflicht, wenn verwaltet. LM Studio meldet ihn nicht |
| Reserve für Anzeige bzw. System (GB) | 2 | bleibt immer frei (Monitor an derselben Karte, Desktop, Schätzfehler) |

Je Modell kommen im Detailformular dazu: Parallele Anfragen, Priorität (0–100, Standard 50),
automatisch laden (an), Entladen nach Leerlauf (30 min, 0 = nie), nach der Indexierung
entladen (an: Indexierungsmodelle 2 Minuten nach dem Lauf) und ein gemessener
Speicherbedarf, der die Schätzung ersetzt. Regeln, Schätzung und typische Werte stehen unter
[RAG einrichten → Modellverwaltung](RAG-Einrichtung#modellverwaltung-automatisch-laden-speicherbudget).

## Welche Modelle dürfen MCP nutzen?

MCP-Server können Dinge verändern (Dateien, Kalender, Hausautomation). Ein Modell, das
Webseiten oder Dokumente liest, kann durch versteckte Anweisungen darin (Prompt-Injection) zu
Aufrufen verleitet werden. Deshalb legt der Verwalter je Modell fest, welche MCP-Server es
nutzen darf (Spalte **MCP**):

| Wert | Bedeutung |
|---|---|
| kein | Keine MCP-Werkzeuge. Websuche und Dokumentsuche bleiben (sie lesen nur). |
| alle | Alle MCP-Server, die die Rolle des Nutzers erlaubt. |
| ausgewählte | Nur die im Detailformular des Modells gewählten Server („erlaubte MCP-Server“). |

- **Vorgabe für neue Modelle:** „alle“ bei OpenAI, Anthropic und Google, sonst „kein“, also bei
  lokalen Modellen und bei Sammelanbietern wie OpenRouter. Modelle, die es vor dieser Einstellung
  schon gab, stehen auf „alle“.
- Es gilt die **Schnittmenge** mit den Rechten der Rolle: Ein Server, den die Rolle nicht erlaubt,
  bleibt gesperrt, auch wenn das Modell ihn nutzen darf.
- MultiGPT prüft das **auf dem Server**: beim Anbieten der Werkzeuge und vor jedem Aufruf erneut,
  auch nach einer Rückfrage. Entzieht der Verwalter die Erlaubnis zwischen Rückfrage und
  Bestätigung, wird der Aufruf nicht ausgeführt; das Modell erhält „Nicht ausgeführt: Dieses
  Modell darf den MCP-Server nicht nutzen“. Im Log stehen nur Modell- und Server-ID.
- Im Chat zeigt die Werkzeugauswahl nur die erlaubten Server. Ist MCP für das Modell aus, steht
  dort „Für dieses Modell sind keine MCP-Werkzeuge freigegeben“. Im Vergleich gibt es weiterhin
  keine MCP-Werkzeuge.
- Beim MCP-Server im Admin zeigt der Abschnitt **„Modelle“**, welche Modelle ihn nutzen dürfen.
- Server anlegen, Status und Einstufung der Werkzeuge: siehe [MCP-Server](MCP).

## Quellen

Gelesen am 2026-10-10:

- LM Studio REST API: <https://lmstudio.ai/docs/developer/rest/endpoints>
- LM Studio API-Changelog (0.3.16: `capabilities` in `/api/v0/models`):
  <https://lmstudio.ai/docs/developer/api-changelog>
- LM Studio Tool Use: <https://lmstudio.ai/docs/developer/openai-compat/tools>
- LM Studio REST-API v1 (ab 0.4, Modellverwaltung): <https://lmstudio.ai/docs/developer/rest/list>,
  <https://lmstudio.ai/docs/developer/rest/load>, <https://lmstudio.ai/docs/developer/rest/unload>
- OpenAI Modelle: <https://developers.openai.com/api/docs/models>
- Anthropic Modelle: <https://docs.claude.com/en/docs/about-claude/models/overview>
- Gemini Modelle: <https://ai.google.dev/gemini-api/docs/models>
