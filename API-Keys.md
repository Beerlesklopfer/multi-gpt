# API-Keys und MCP-Server

MultiGPT ist nicht nur MCP-**Client** (siehe [MCP-Server](MCP)), sondern auch selbst
**MCP-Server**: Andere Programme – [n8n](n8n), Claude Desktop, Claude Code, eigene Agenten oder
Skripte – steuern MultiGPT mit dem API-Key eines Kontos. Sie können Modelle fragen, Dokumente
hochladen und durchsuchen, die Indexierung starten und überwachen, Verzeichnisquellen (Crawler)
einlesen lassen und erzeugte Dateien abholen.

Der Grundsatz: **Ein Key kann nie mehr als sein Konto.** Es gelten Rolle, Freigaben der
Sammlungen und Budgets des Kontos; Kosten gehen auf das Konto. Verwalterrechte (Konten, Rollen,
Anbieter …) gibt es über Keys nicht.

## Adresse

```
https://<hostname>/mcp/          (auch ohne Schrägstrich: https://<hostname>/mcp)
Authorization: Bearer mgpt_…
```

- Transport: **Streamable HTTP** (POST, Antwort als JSON oder als SSE-Stream mit Fortschritt).
- Protokoll: **2026-07-28** (aktuelle Spezifikation, zustandslos) und die älteren Versionen
  **2025-03-26 bis 2025-11-25** mit `initialize`-Handshake (n8n, Claude Desktop). Der Server
  vergibt keine Sitzungs-IDs; GET und DELETE ergeben 405.
- Quellen: [Streamable HTTP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http),
  [Transports 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).

## Key anlegen

**Im Browser:** Einstellungen → „API-Keys (MCP-Zugang …)“ → Name, Rechte, Gültigkeit und
optional „Nur diese Sammlungen“ wählen → **Key anlegen**. Der Key erscheint **genau einmal**;
MultiGPT speichert nur einen Hash (SHA-256). Verloren = widerrufen und neu anlegen.

**Auf der Konsole** (Betrieb):

```bash
sudo mgpt-ctl apikey create anna --name "n8n NAS" --scopes docs.read,docs.write,index.control --expires 365d
sudo mgpt-ctl apikey list
sudo mgpt-ctl apikey revoke mgpt_ab12cd34ef
```

Der Key steht auf stdout (z. B. `> key.txt`), die Hinweise auf stderr. Weitere Optionen:
[mgpt-ctl](mgpt-ctl#apikey--api-keys-für-den-mcp-server).

**Widerrufen** wirkt sofort, ebenso das Ablaufdatum: Jeder Aufruf prüft Key, Konto und Rolle neu.

## Rechte (Scopes)

| Recht | Bedeutung | Werkzeuge |
|---|---|---|
| `chat.ask` | Einem Modell eine Aufgabe geben (Modell im Rahmen der Rolle, Budget des Kontos) | `ask`, `list_models` |
| `docs.read` | Sammlungen und Dokumente auflisten, durchsuchen, lesen | `list_collections`, `search_documents`, `list_documents`, `document_info`, `read_document` |
| `docs.write` | Dateien hochladen, Dokumente löschen, neu indexieren (eigene bzw. schreibbare Sammlungen) und die Läufe dazu verfolgen | `upload_document`, `delete_document`, `reindex`, `run_status`, `list_runs` |
| `index.control` | Läufe überwachen und abbrechen; Verzeichnisquellen einlesen (**nur Verwalter**) | `run_status`, `list_runs`, `cancel_run`, `list_sources`, `start_scan` |
| `files.read` | Erzeugte Anhänge und Dokumente abholen | `get_file` |
| `tools.run` | Werkzeuge direkt aufrufen, mit denselben Rollenrechten und Budgets wie im Chat | `create_pdf`, `generate_image`, `run_python`, `fetch_url`, `web_search` |
| `usage.read` | Eigener Verbrauch und Budgetstand | `usage` |

Wirksam ist immer die **Schnittmenge**:

1. Rechte des Keys,
2. Höchstmenge der Rolle (Admin → Rollen → „API-Keys und MCP-Zugang“: Häkchen
   „API-Keys/MCP-Zugang“ und „Erlaubte API-Rechte“),
3. die übrigen Rechte des Kontos auf das konkrete Objekt: Modellfreigabe, Lese- und Schreibrecht
   auf Sammlungen, „Dokumente hochladen“, „Websuche“, „Bilder“, „Berechnungen“, „Dokumente
   erzeugen“, Verwalter bei Verzeichnisquellen.

`tools/list` zeigt nur, was davon gerade wirksam ist. Ein Werkzeug außerhalb der Rechte gilt wie
ein unbekanntes. Zusätzlich: `ask` mit `collections` braucht `docs.read`, `ask` mit `tools: true`
braucht `tools.run`.

Startwerte: Verwalter und Erwachsene haben das Recht mit allen Scopes, Jugendliche und Gäste nicht.

## Werkzeuge im Überblick

| Werkzeug | Argumente | Ergebnis |
|---|---|---|
| `ask` | `prompt`, optional `model`, `collections`, `web_search`, `tools`, `chat` | Antworttext; strukturiert: `chat_id`, `message_id`, `status`, Tokens, `cost_eur`, `sources` |
| `list_models` | – | erlaubte Chat-Modelle (ID, Name, Fähigkeiten, Budgetsperre) |
| `list_collections` | – | Sammlungen mit Zahlen je Status |
| `search_documents` | `query`, optional `collections` | nummeriertes Quellmaterial mit Fundstellen |
| `list_documents`, `document_info`, `read_document` | wie die Dokument-Werkzeuge im Chat | Liste, Angaben, Text mit Fundstellen |
| `upload_document` | `collection`, `filename` + `content_base64` **oder** `url` | `document_id`, `run_id` |
| `delete_document` | `document_id` | – |
| `reindex` | `collection` **oder** `document`/`documents` | Lauf (`id`) |
| `run_status`, `list_runs` | `run_id` bzw. `open_only`, `limit` | Status, Zähler, Fortschritt |
| `cancel_run` | `run_id` | entfernte/markierte Aufträge |
| `list_sources`, `start_scan` | – bzw. `source` | Verzeichnisquellen bzw. Lauf |
| `get_file` | `attachment_id` **oder** `document_id` | Datei als eingebettete Ressource (base64) |
| `create_pdf`, `generate_image`, `run_python`, `fetch_url`, `web_search` | wie im Chat | Text; erzeugte Dateien als `attachments` (IDs für `get_file`) |
| `usage` | – | Verbrauch im laufenden Monat |

Hinweise:

- **`ask`** legt je Aufruf einen Chat „API: …“ an, der im Konto sichtbar ist; mit `chat: <ID>`
  geht es in einem eigenen bzw. schreibbar geteilten Chat weiter. Ohne `tools` bekommt das Modell
  keine Werkzeuge. Braucht ein Werkzeug eine Rückfrage, endet `ask` mit Status
  `awaiting_confirmation` – bestätigen lässt sich das nur in MultiGPT.
- **Lange Aufgaben** (Indexierung, Verzeichnis einlesen) laufen im Worker. Das Werkzeug liefert
  sofort eine Lauf-ID; `run_status` zeigt den Fortschritt, bis `open` `false` ist.
- **Fortschritt per SSE:** Schickt der Client `Accept: text/event-stream` und ein
  `progressToken` mit, sendet `ask` `notifications/progress`, bevor das Ergebnis kommt. Schließt
  der Client die Verbindung, wird die Antwort abgebrochen.
- **Direkte Werkzeuge** (`tools.run`) erscheinen im Chat „API: <Key> – Werkzeuge“. Die Rückfrage,
  die der Verwalter für `generate_image` bzw. `run_python` einstellen kann, gilt im Chat für
  Aufrufe eines *Modells*; über die API ruft das Programm des Key-Inhabers das Werkzeug selbst
  auf – dieser Aufruf ist die Bestätigung.
- **Uploads**: Typ und Größe wie in der Oberfläche (`DOCUMENT_MAX_UPLOAD_MB`). `url` lädt nur
  über den geschützten Abruf der Websuche: keine Adressen im lokalen Netz, Weiterleitungen werden
  neu geprüft, Zeit- und Größengrenze. Dateien aus dem Heimnetz deshalb als `content_base64`.

## Beispiele

### curl (älteres Protokoll, ohne Handshake-Zustand)

```bash
KEY=mgpt_…
curl -s https://nas.example/mcp/ -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

curl -s https://nas.example/mcp/ -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"ask","arguments":{"prompt":"Fasse die Heizungsanleitung zusammen.",
                 "collections":["Haus"]}}}'
```

### Claude Desktop

Claude Desktop spricht HTTP-Server über `mcp-remote` an:

```json
{
  "mcpServers": {
    "multigpt": {
      "command": "npx",
      "args": ["mcp-remote", "https://nas.example/mcp/", "--header", "Authorization:${AUTH_HEADER}"],
      "env": { "AUTH_HEADER": "Bearer mgpt_…" }
    }
  }
}
```

Bei einem selbstsignierten Zertifikat (snakeoil) braucht Node zusätzlich
`NODE_EXTRA_CA_CERTS=/pfad/zum/zertifikat.pem` – besser ein eigenes Zertifikat einspielen
(siehe [nginx und TLS](nginx-und-TLS)).

## Sicherheit und Datenschutz

- **Keys** stehen nur als Hash in der Datenbank, nie im Klartext in Logs. In Listen erscheint nur
  das Präfix (`mgpt_ab12cd34ef…`). Verwalter sehen im Admin unter „Knoten“ nur Metadaten.
- **Keine Cookies:** `/mcp/` wertet weder Sitzung noch Login-Cookie aus, nur den Bearer-Key.
  Deshalb gilt der CSRF-Schutz dort nicht. Ein `Origin`-Header aus einem fremden Host wird
  abgewiesen (Schutz gegen DNS-Rebinding).
- **Drosselung:** nach `API_FAILURE_LIMIT` (10) ungültigen Keys in
  `API_FAILURE_WINDOW_MINUTES` (15) sperrt `/mcp/` die IP-Adresse (HTTP 429), auch für gültige
  Keys; je Key höchstens `API_RATE_LIMIT_PER_MINUTE` (120) Aufrufe je Minute. Siehe
  [Konfiguration](Konfiguration#mcp-server-und-api-keys).
- **Fehler ohne Details:** fehlender, falscher, abgelaufener oder widerrufener Key → 401 ohne
  Inhalt; Konto bzw. Rolle ohne API-Recht → 403 ohne Inhalt.
- **Audit-Log:** Jeder Aufruf wird mit Key, Methode, Werkzeug, Status, Dauer, Größen und
  IP-Adresse protokolliert – **ohne Inhalte**. Das Mitglied sieht die letzten 50 auf der Seite
  „API-Keys“, Verwalter alle im Admin unter „Knoten › API-Aufrufe“. Aufbewahrung
  `API_AUDIT_DAYS` (90 Tage).
- **Prompt-Injection:** Was über die API hereinkommt (Prompts, Dateien, URLs) und was
  zurückgeht (Modellantworten, Dokument- und Webtext), ist Material, keine Anweisung. Für
  Werkzeuge mit Rückfrage gibt es über `ask` keine Abkürzung.
- **Budgets** gelten unverändert: Ein ausgeschöpftes Budget sperrt die betroffenen Modelle auch
  über die API.

## Fehlersuche

| Meldung | Ursache |
|---|---|
| HTTP 401 | Key falsch, abgelaufen, widerrufen oder Konto gesperrt. |
| HTTP 403 | Rolle ohne „API-Keys/MCP-Zugang“ bzw. Key ohne wirksame Rechte; oder fremder `Origin`. |
| HTTP 429 | Zu viele Fehlversuche von dieser IP oder zu viele Aufrufe mit diesem Key. |
| HTTP 413 | Anfrage zu groß (Upload über `DOCUMENT_MAX_UPLOAD_MB`). |
| `Unknown tool` | Werkzeug gibt es nicht oder der Key hat das Recht nicht. |
| `isError: true` | Das Werkzeug lehnt ab (z. B. „Sammlung nicht gefunden.“, „Diese Sammlung darf nur gelesen werden.“). |
