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

Ein eigenes Häkchen für Audio direkt im Chat gibt es nicht: Spracheingabe läuft über ein
Spracherkennungs-Modell, der Text landet im Eingabefeld (M10-01).

## Automatische Erkennung

Neue Modelle bekommen Hauptart, „Werkzeuge“ und „Bilder verstehen“ automatisch:

- **LM Studio** meldet die Fähigkeiten selbst (`GET /api/v0/models`, ab LM Studio 0.3.16):
  `type` `llm`, `vlm` (versteht Bilder) oder `embeddings` und `capabilities` mit `tool_use`.
  MultiGPT fragt das nur ab, wenn neue Modelle anzulegen sind, höchstens einmal je Statusprüfung
  (alle 15 s) und nur bei lokalen Anbietern. Antwortet der Server nicht so (älteres LM Studio,
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
- OpenAI Modelle: <https://developers.openai.com/api/docs/models>
- Anthropic Modelle: <https://docs.claude.com/en/docs/about-claude/models/overview>
- Gemini Modelle: <https://ai.google.dev/gemini-api/docs/models>
