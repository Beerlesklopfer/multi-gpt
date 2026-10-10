# MCP-Server

Über MCP-Server (Model Context Protocol) bekommen Chat-Modelle zusätzliche Werkzeuge, z. B.
Zugriff auf Dateien, Kalender oder die Hausautomation. MultiGPT ist dabei der Client: Es verbindet
sich mit den Servern, bietet deren Werkzeuge dem Modell an und führt Aufrufe aus, je nach
Einstufung erst nach einer Rückfrage im Chat.

Alles auf dieser Seite erledigt der Verwalter im Admin unter **Chat › MCP-Server**.

Umgekehrt ist MultiGPT seit 0.3.2 auch selbst **MCP-Server** unter `https://<hostname>/mcp/`: Andere
Programme steuern es mit dem API-Key eines Kontos – siehe [API-Keys](API-Keys) und, für beide
Richtungen mit n8n, [n8n](n8n).

## Server anlegen

Zwei Transporte gibt es:

| Transport | Feld | Beispiel |
|---|---|---|
| stdio (lokaler Prozess) | **Befehl**: Programm mit Argumenten, ohne Shell | `npx -y @modelcontextprotocol/server-filesystem /srv/daten` |
| HTTP (Streamable HTTP) | **URL** des MCP-Endpunkts | `https://mcp.example.org/mcp` |

- **Zugangsdaten** werden verschlüsselt gespeichert und nie angezeigt (nur die letzten 4 Zeichen).
  Format: bei stdio `{"env": {"API_KEY": "…"}}`, bei HTTP `{"bearer_token": "…"}` oder
  `{"headers": {"Name": "Wert"}}`; bei HTTP genügt auch das Token allein.
- **Zeitlimit (s)** gilt je Werkzeugaufruf. Die Statusprüfung wartet höchstens 10 Sekunden.
- **aktiv**: Deaktivierte Server werden weder angeboten noch geprüft. Bei stdio startet MultiGPT
  den Prozess also nur für aktive Server.

### Aus JSON importieren

In der Liste der MCP-Server oben rechts **„Aus JSON importieren“**. Dort lässt sich eine
Konfiguration im Format von Claude Desktop, Claude Code, Cursor oder n8n einfügen
(`{"mcpServers": {…}}`). Eine Vorschau zeigt, was angelegt wird.

- Neue Server werden angelegt und, wenn sie aktiv sind, **sofort geprüft**; das Ergebnis steht
  als Meldung oben in der Liste.
- Enthält ein Eintrag Platzhalter wie `<YOUR_ACCESS_TOKEN_HERE>`, wird der Server deaktiviert
  angelegt und nicht geprüft. Erst die echten Zugangsdaten eintragen, dann aktivieren.
- Mit **„Gleichnamige Server aktualisieren“** werden vorhandene Server überschrieben;
  Einstufungen bleiben erhalten. Der Worker prüft sie beim nächsten Durchlauf neu.

## Status

Jeder Server hat einen Status: **online**, **offline** (mit Ursache), **ungeprüft** oder
**deaktiviert**. Prüfen heißt: verbinden (bei stdio den Prozess starten), MCP-Handshake
(`initialize`) und Werkzeugliste abrufen (`tools/list`).

Geprüft wird

- nach dem Speichern eines Servers im Admin,
- mit dem Knopf **„Jetzt prüfen“** oben rechts auf der Seite des Servers oder der Aktion
  **„Ausgewählte jetzt prüfen“** in der Liste,
- nach dem JSON-Import für neu angelegte, aktive Server,
- regelmäßig durch den Worker (`multi-gpt-worker.service`): Server, die online sind, alle
  5 Minuten, Server, die offline sind, jede Minute.

Beim bloßen Öffnen von Seiten prüft MultiGPT nicht. Läuft der Worker nicht, ändert sich der
Status nur über die Prüfungen im Admin.

### Ursachen bei offline

| Meldung | Was tun? |
|---|---|
| Verbindung abgelehnt | Läuft der Server? Stimmt der Port in der URL? |
| Rechnername unbekannt | Rechnername in der URL prüfen (DNS). |
| Rechner nicht erreichbar | Ist der Rechner an und im Netz? |
| TLS-Fehler | Zertifikat prüfen bzw. `http`/`https` verwechselt? |
| Zeitüberschreitung | Server überlastet oder Firewall; bei stdio: Startet das Programm sehr langsam? |
| Zugang abgelehnt (HTTP 401/403): Token prüfen | Zugangsdaten neu eintragen. |
| Adresse nicht gefunden (HTTP 404): Pfad prüfen | Der Endpunkt heißt je nach Server z. B. `…/mcp` oder `…/http`. |
| Serverfehler (HTTP 5xx) | Fehler beim Server, später erneut prüfen. |
| Programm nicht gefunden | stdio: Programm installiert? Pfad im Befehl richtig? |
| Programm beendet sich sofort | stdio: Argumente oder Umgebungsvariablen falsch; das Programm in einer Konsole von Hand starten. |
| Protokollfehler | Unter der Adresse antwortet kein MCP-Server (z. B. eine Webseite). |
| Falsch eingerichtet | Befehl nicht lesbar oder Zugangsdaten im falschen Format. |

Die Meldungen enthalten keine Zugangsdaten und keinen Text des Servers. Im Protokoll stehen nur die
Server-ID und die Kurzursache, und nur, wenn sich der Status ändert.

### Im Chat

- In der Werkzeugauswahl erscheint ein offline-Server **ausgegraut** mit „(offline)“; die Ursache
  steht im Tooltip. Seine Werkzeuge werden dem Modell nicht angeboten.
- Fällt ein Server erst während einer Antwort aus, erscheint ein kurzer Hinweis in der
  Statuszeile („MCP-Server „…“ ist offline – seine Werkzeuge stehen nicht zur Verfügung“); die
  Antwort läuft ohne diese Werkzeuge weiter.
- Verwalter sehen offline-Server zusätzlich dezent in der Statusleiste oben, mit Link zum Server
  im Admin. Server, die online sind, erscheinen dort nicht. Andere Konten sehen MCP-Server in der
  Statusleiste nie.

## Werkzeuge einstufen

Jedes Werkzeug eines Servers ist eingestuft als

| Einstufung | Bedeutung |
|---|---|
| ohne Rückfrage | Das Modell darf es direkt aufrufen. Nur für Werkzeuge, die nichts verändern oder deren Wirkung harmlos ist. |
| mit Rückfrage | Im Chat erscheint vor jedem Aufruf eine Rückfrage mit den Argumenten; erst nach „Ausführen“ läuft es. |
| nicht eingestuft | Wie „mit Rückfrage“. Gilt für alle neuen Werkzeuge, bis der Verwalter sie einstuft. |

Auf der Seite des Servers zeigt der Abschnitt **„Werkzeuge“** die zuletzt gemeldeten Werkzeuge als
Tabelle, ohne dass dafür eine Verbindung nötig ist: Name, Beschreibung, Parameter (Pflichtparameter
mit `*`), Hinweise des Servers und eine Auswahl für die Einstufung. Einstufung wählen und
**Speichern**.

- **Neue Werkzeuge** (gemeldet, aber noch nicht eingestuft) sind farbig hervorgehoben.
  Werkzeuge, die eingestuft sind, aber nicht mehr gemeldet werden, stehen unter der Tabelle.
- **Vorschlag des Servers:** Liefert der Server Hinweise (annotations), ist die Auswahl bei neuen
  Werkzeugen vorbelegt: „verändernd“ (`destructiveHint`) → mit Rückfrage, „nur lesend“
  (`readOnlyHint`) → ohne Rückfrage. Das ist nur ein Vorschlag: Der Server kann sich irren oder
  absichtlich falsch melden. Wirksam wird die Einstufung erst beim Speichern; vorher bitte prüfen.
- Beschreibungen kommen vom Server und werden nur als Text (gekürzt) angezeigt.
- In der Liste der MCP-Server zeigen die Spalten **Werkzeuge** und **davon nicht eingestuft**, wo
  noch etwas zu tun ist.
- **Erweitert: Einstufung als JSON** (zugeklappt) enthält die Listen „Werkzeuge mit Rückfrage“ und
  „eingestufte Werkzeuge“ als Rückfall, z. B. solange ein Server offline ist. Für Werkzeuge, die in
  der Tabelle stehen, gilt die Tabelle.

## Freigabe je Modell

Welche KI-Modelle einen Server nutzen dürfen, legt die Spalte **MCP** bei den KI-Modellen fest
(kein / alle / ausgewählte), siehe [Anbieter und Modelle](Anbieter-und-Modelle#welche-modelle-dürfen-mcp-nutzen).
Zusätzlich gelten die Rechte der Rolle (Schnittmenge). Auf der Seite des Servers zeigt der
Abschnitt **„Modelle“**, welche Modelle ihn nutzen dürfen. Das Modell braucht außerdem das
Häkchen „Werkzeuge“.

## Eingebaute Werkzeuge für die Indexierung

Unabhängig von MCP-Servern bekommen werkzeugfähige Modelle für Konten mit passenden Rechten
eingebaute Werkzeuge, um die Indexierung im Chat zu steuern („Lies den NAS-Ordner neu ein und
sag mir, wann er fertig ist“):

| Werkzeug | Wirkung | Rückfrage | Angeboten, wenn |
|---|---|---|---|
| `index_status` | Läufe mit Fortschritt (für Verwalter auch Warteschlange und Verzeichnisquellen) | nein | das Konto eine Sammlung schreiben darf oder Verwalter ist |
| `start_reindex` | Sammlung bzw. Dokument neu indexieren | **immer** | das Konto eine Sammlung schreiben darf |
| `start_scan` | Verzeichnisquelle jetzt einlesen | **immer** | Verwalter, Verzeichnisquellen eingerichtet |
| `cancel_run` | Lauf abbrechen | **immer** | wie `index_status` (abbrechen nur eigene bzw. schreibbare; Verwalter alle) |

Die Rechte entsprechen dem Admin bzw. den Sammlungen und werden vor jedem Aufruf neu geprüft. Die
Rückfrage hängt nur an der Registrierung, nicht an Text aus Dokumenten oder Webseiten.

## MultiGPT als MCP-Server

Siehe [API-Keys](API-Keys): Adresse `/mcp/`, Rechte (Scopes) je Key, Werkzeuge (`ask`,
Dokumente hochladen und durchsuchen, Läufe starten, überwachen und abbrechen, Dateien abholen,
`create_pdf` u. a.), Sicherheit, Audit-Log. Beispiel-Workflows: [n8n](n8n).

## Fehlersuche

- **Status bleibt „ungeprüft“:** Läuft der Worker (`systemctl status multi-gpt-worker`)? Oder
  „Jetzt prüfen“ im Admin.
- **stdio-Server startet von Hand, aber nicht in MultiGPT:** Der Prozess läuft als Benutzer des
  Dienstes mit minimaler Umgebung (`HOME`, `PATH`, …). Fehlende Variablen über die Zugangsdaten
  (`{"env": {…}}`) setzen, Programme mit vollem Pfad angeben.
- **Werkzeug fehlt im Chat:** Server offline? Modell ohne „Werkzeuge“ oder ohne MCP-Freigabe?
  Rolle ohne Recht auf den Server?
