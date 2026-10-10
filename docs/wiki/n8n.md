# n8n und MultiGPT

[n8n](https://n8n.io) ist im Heimnetz der Automatisierer: Er reagiert auf Ereignisse (neue Datei,
Uhrzeit, E-Mail) und verbindet Dienste. MultiGPT und n8n sprechen über **MCP** in beide
Richtungen miteinander:

| Richtung | Wer ruft wen? | Wofür |
|---|---|---|
| **A: MultiGPT nutzt n8n** | MultiGPT ist MCP-Client, n8n ist MCP-Server („MCP Server Trigger“) | Modelle in MultiGPT rufen n8n-Workflows als Werkzeuge auf (z. B. „Licht aus“, „Termin eintragen“). |
| **B: n8n nutzt MultiGPT** | n8n ist MCP-Client („MCP Client“ bzw. „MCP Client Tool“), MultiGPT ist MCP-Server | n8n lädt Dateien hoch, startet und überwacht die Indexierung, fragt Modelle, holt PDFs ab. |

Ob beides eingerichtet ist, zeigt der Admin unter **Knoten › Integrationen**: n8n-MCP-Server
(eingerichtet? online?) und API-Keys (aktiv? benutzt?).

n8n-Dokumentation:
[MCP Server Trigger](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-langchain.mcptrigger/),
[MCP Client](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-langchain.mcpclient/),
[MCP Client Tool](https://docs.n8n.io/integrations/builtin/cluster-nodes/sub-nodes/n8n-nodes-langchain.toolmcp/).

## A: MultiGPT nutzt n8n (n8n als MCP-Server)

1. **In n8n** einen Workflow mit dem Knoten **MCP Server Trigger** anlegen. Daran hängen die
   Werkzeuge, die MultiGPT nutzen soll (z. B. „Custom n8n Workflow Tool“ oder HTTP Request).
2. Im Trigger **Authentication** auf „Bearer auth“ stellen und ein Token als Zugangsdaten
   anlegen. **Path** bei Bedarf sprechend setzen.
3. Den Workflow **veröffentlichen** und die **Production URL** kopieren (die Test-URL gilt nur,
   solange „Listen for Test Event“ läuft). Der Trigger spricht SSE und Streamable HTTP; MultiGPT
   nutzt Streamable HTTP.
4. **In MultiGPT** (Admin → Chat › MCP-Server) einen Server anlegen: Transport **HTTP**, **URL** =
   Production URL, **Zugangsdaten** = das Token (oder `{"bearer_token": "…"}`). Alternativ
   „Aus JSON importieren“ mit einer `mcpServers`-Konfiguration, z. B.

   ```json
   {"mcpServers": {"n8n": {"type": "http", "url": "https://n8n.example/mcp/haus",
                           "headers": {"Authorization": "Bearer …"}}}}
   ```

5. **Jetzt prüfen** → Status „online“ und die Werkzeugliste erscheinen. Werkzeuge **einstufen**
   (ohne bzw. mit Rückfrage), die Rolle bzw. „MCP“ je Modell freigeben – siehe [MCP-Server](MCP).

Tipp: Den Server „n8n …“ nennen oder die URL von n8n verwenden; daran erkennt die Seite
„Integrationen“ ihn.

## B: n8n nutzt MultiGPT (MultiGPT als MCP-Server)

1. **Key anlegen:** In MultiGPT als das Konto, in dessen Namen n8n arbeiten soll, unter
   Einstellungen → **API-Keys** einen Key mit den nötigen Rechten anlegen (oder
   `sudo mgpt-ctl apikey create …`). Nur so viele Rechte wie nötig, ggf. „Nur diese
   Sammlungen“. Für Verzeichnisquellen muss das Konto Verwalter sein. Siehe [API-Keys](API-Keys).
2. **In n8n** Zugangsdaten vom Typ **Bearer Auth** mit dem Key anlegen.
3. Knoten wählen:
   - **MCP Client** (normaler Workflow-Schritt): **Server Transport** „HTTP Streamable“,
     **MCP Endpoint URL** `https://<hostname>/mcp/`, **Authentication** „Bearer Auth“, dann das
     **Tool** wählen (die Liste kommt von MultiGPT) und die Argumente setzen (**Input Mode**
     „Manual“ oder „JSON“). Für `get_file`: Option **Convert to Binary**.
   - **MCP Client Tool** (Werkzeuge für einen *AI Agent* in n8n): gleiche Adresse und
     Zugangsdaten, **Tools to Include** auf die benötigten Werkzeuge beschränken.
4. Läuft MultiGPT mit dem selbstsignierten snakeoil-Zertifikat, vertraut n8n ihm nicht. Besser
   ein eigenes Zertifikat einspielen ([nginx und TLS](nginx-und-TLS)); sonst dem n8n-Container
   das Zertifikat bekannt machen (`NODE_EXTRA_CA_CERTS`).

Alternativ ohne MCP-Knoten: **HTTP Request** mit `POST https://<hostname>/mcp/`, Header
`Authorization: Bearer …`, `Accept: application/json, text/event-stream` und als JSON-Body z. B.
`{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"run_status","arguments":{"run_id":12}}}`.
Ein `initialize` vorab ist nicht nötig (MultiGPT ist zustandslos).

### Beispiel 1: Neue Datei im NAS-Ordner → hochladen → indexieren → warten

Rechte: `docs.write`. Ablauf:

1. **Local File Trigger** (bzw. Trigger des NAS) auf „Datei hinzugefügt“ im Ordner.
2. **Read/Write Files from Disk** → Datei als Binärdaten; **Extract from File** bzw. ein
   **Code**-Knoten macht daraus base64 (`$binary.data.data`).
3. **MCP Client** → Tool `upload_document`, Argumente:
   `{"collection": "Haus", "filename": "{{ $json.fileName }}", "content_base64": "{{ $json.data }}"}`.
   Ergebnis: `document_id` und `run_id`. (Das Hochladen startet die Indexierung schon selbst;
   `reindex` braucht es nur für bereits vorhandene Dokumente.)
4. **Wait** 30 Sekunden → **MCP Client** → `run_status` mit `{"run_id": {{ $json.run_id }}}`.
5. **If** `open` ist `true` → zurück zu Schritt 4, sonst weiter (z. B. Meldung bei
   `docs_failed > 0`).

Dateien, die schon auf dem Server liegen, besser als **Verzeichnisquelle** einlesen lassen
(Beispiel 2) – dann werden sie nicht kopiert.

### Beispiel 2: Täglich alle Verzeichnisquellen einlesen

Rechte: `index.control`, Konto mit Verwalterrolle.

1. **Schedule Trigger** täglich 02:00.
2. **MCP Client** → `list_sources` → **Split Out** über `sources`.
3. **MCP Client** → `start_scan` mit `{"source": {{ $json.id }}}` (läuft schon ein Lauf, kommt
   dessen ID zurück, `new: false`).
4. Optional wie in Beispiel 1 mit `run_status` bis zum Ende warten und das Ergebnis
   (`files_new`, `files_changed`, `docs_failed`) per Mail schicken.

Die Quellen lesen sich ohnehin periodisch selbst ein (Intervall je Quelle); der Workflow ist für
feste Zeiten oder Abhängigkeiten von anderen Abläufen gedacht.

### Beispiel 3: Frage mit Dokumentsuche, Ergebnis per Mail

Rechte: `chat.ask`, `docs.read`.

1. **Schedule Trigger** (z. B. montags 07:00).
2. **MCP Client** → `ask` mit
   `{"prompt": "Welche Wartungstermine stehen laut Handbüchern diesen Monat an?", "collections": ["Haus"], "model": "gpt-5-mini"}`.
3. **Send Email** mit `{{ $json.content[0].text }}` als Text; die Quellen stehen in
   `structuredContent.sources`, der Chat in MultiGPT unter `structuredContent.chat_url`.

Die Antwort ist Modelltext: Nicht ungeprüft als Anweisung weiterverarbeiten (z. B. nicht als
Befehl in einen weiteren Knoten geben).

## Rechte und Keys – Hinweise

- Ein Key je Workflow bzw. Zweck, mit Namen („n8n NAS-Upload“). Dann zeigt das Audit-Log, wer was
  tut, und ein Widerruf trifft nur diesen Zweck.
- Kosten von `ask` und `generate_image` gehen auf das Konto des Keys und zählen gegen dessen
  Budget.
- `start_scan` und `list_sources` gibt es nur für Verwalter (wie „Jetzt einlesen“ im Admin).
- Ein Key mit „Nur diese Sammlungen“ sieht andere Sammlungen nicht, auch nicht beim Suchen.
- Läuft n8n auf demselben Rechner, zählt seine Adresse für die Drosselung je IP; bei vielen
  Fehlversuchen anderer Programme von dort wird auch n8n kurz gesperrt.

## Pflicht: was MultiGPT prüft

MultiGPT setzt n8n nicht hart voraus und startet auch ohne. Die Übersicht **Admin → Knoten ›
Integrationen** zeigt aber deutlich, wenn

- kein n8n-MCP-Server eingerichtet bzw. keiner online ist (Richtung A),
- es keinen aktiven API-Key gibt (Richtung B),

und wie viele Aufrufe in den letzten 24 Stunden kamen.
