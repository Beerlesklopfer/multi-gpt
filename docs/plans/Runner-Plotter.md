# Runner als Hardware-Dienst: GPU-Wechsel und HPGL-Plotter

Planungsstand: 2026-10-19. **Nur ein angedachtes Feature**, nicht in der Roadmap und nicht beschlossen.
Der Plotter selbst ist ein eigenes, abgeschlossenes Projekt. Der Modellverwalter dazu steht in
[Eliezer.md](Eliezer.md).
Quelle: Architekturplan „runner-rs“ (PDF des Nutzers), hier an die vorhandene Runner-Architektur
angepasst. Baut auf [FLUX-Runner.md](FLUX-Runner.md) und [Runner-Familienrechner.md](Runner-Familienrechner.md) auf.

## Ziel

Der Runner auf dem Powerrechner übernimmt neben Werkzeug-Containern auch **Hardware-Aufgaben**,
die den Django-Server nicht belasten sollen:

1. **Exklusive GPU-Steuerung** für Rechner mit knappem VRAM: Chat-Modell und Bildmodell (FLUX)
   passen nicht gleichzeitig hinein, also wechselt der Runner nacheinander.
2. **HPGL-Plotter** über ein Blackpill-Board (USB-CDC): Fehlerkoordinaten aus einem
   Vision-Modell werden zu Plotterbefehlen und zeichnen Korrekturen auf ein Schülerblatt.

## Abweichungen vom Quellplan (bewusst)

| Quellplan | Hier | Grund |
|---|---|---|
| Axum-/gRPC-Schnittstelle, Django ruft den Runner an | Der Runner bleibt **nur ausgehend** verbunden (WebSocket/TLS, Protokoll 2); Befehle kommen als signierte Rahmen vom Server | Keine offenen Ports am Rechner; Signatur und Token schützen vor Fremdbefehlen |
| Eigener Orchestrator mit Mutex | Server führt Buch (`GpuLease`, Warteschlange); der Runner setzt durch und meldet Tatsachen | Rechte, Kontingent und Rangfolge liegen schon im Server |
| Modelle per Candle oder mistral.rs im Runner | Chat-Modell bleibt bei **LM Studio** (Laden/Entladen über dessen API, wie umgesetzt); FLUX läuft im Dienst-Container des Runners | Kein zweiter Inferenzstapel; siehe „Einordnung mistral.rs“ im Familienrechner-Plan |
| Plotter-Zugriff direkt | Neuer Dienst im signierten Katalog, Gerät nur in fester Vorlage | Wie `flux`: Dienste sind freigegebene, signierte Einträge |

## Ablauf des VRAM-Wechsels

Auf einer 24-GB-Karte (siehe Familienrechner-Plan) gilt eine strikte Reihenfolge. Der Server
hält die Sperre (Lease), der Runner führt die Schritte aus.

| Schritt | Aktion | GPU / Plotter |
|---|---|---|
| 1. Leerlauf / Chat | Schlankes Chat-Modell (z. B. Qwen 8B/14B) bleibt in LM Studio geladen | GPU: Chat-Modell. Plotter: bereit |
| 2. Lesen und Prüfen | Vision-Modell liest das Schülerblatt, Fehlerkoordinaten landen in der Datenbank | GPU: Vision. Plotter: bereit |
| 3. Bildauftrag | Server vergibt die Sperre; neue Textanfragen warten (Position im Chat sichtbar) | GPU: für Text gesperrt |
| 4. Wechsel | Server bittet LM Studio, die eigenen Instanzen zu entladen; Runner startet den FLUX-Dienst | GPU: wechselt |
| 5. Erzeugen | FLUX rechnet, Bild geht als Anhang an den Server | GPU: FLUX |
| 6. Zurück und Plotten | FLUX entladen, Chat-Modell wieder geladen, danach HPGL-Strom an den Plotter | GPU: Chat-Modell. Plotter: arbeitet |

Der Plotvorgang braucht die GPU nicht; er läuft asynchron, sobald der Speicher wieder frei ist.
Festgehaltene LM-Studio-Modelle werden nie ohne Rückfrage an den Verwalter entladen.

## HPGL-Vektorengine

Ein reines Rechenmodul (ohne Hardwarezugriff, daher gut testbar): rechnet Bounding-Boxes der
Fehler in Plotterkoordinaten um – Skalierung, Kalibrierung (Papiergröße, Ursprung, Ausrichtung)
und Reihenfolge der Wege (kurze Fahrwege). Ausgabe ist das kompakte Format:

```
IN;                      Plotter initialisieren
SP1;                     Stift 1 wählen (roter Korrekturstift)
PU1200,3400;             Pen Up: zur Fehlerkoordinate X,Y
PD1250,3400,1250,3450,1200,3450,1200,3400;   Pen Down: Fehlerbox zeichnen
PU0,0;                   Pen Up: zurück in die Parkposition
SP0;                     Stift ablegen
```

Grenzen, die das Modul vor dem Senden prüft: Koordinaten im Zeichenbereich, höchstens N Befehle
je Auftrag, nur die erlaubte Befehlsmenge (`IN`, `SP`, `PU`, `PD`). Alles andere lehnt es ab –
ein Modell oder eine Datenbankzeile kann also keinen beliebigen Text an das Gerät schicken.

## Serieller Treiber (Blackpill)

- Eigener asynchroner Task je Gerät, Übertragung zeilenweise über USB-CDC (`/dev/ttyACM*`).
- Fluss: nächste Zeile erst nach Quittung des Boards; Abbruch und Parkfahrt bei Fehler oder
  wenn die Verbindung zum Server abbricht.
- Gerät nur für den Plotter-Dienst sichtbar (Gerät in der festen Vorlage, kein freier Zugriff
  aus Benutzer-Containern); Gerätename in `config.env`, nicht vom Server wählbar.
- Der Runner meldet dem Server den Zustand (bereit, beschäftigt, Fehler, Blatt/Stift), der
  Server entscheidet über Reihenfolge und Rechte.

## Server-Seite

- Modelle: `GpuLease` (je GPU), `PlotJob` (Quelle, HPGL, Status, Besitzer). Ein Auftrag gehört
  dem Benutzer, der ihn auslöst; Kontingent und Audit wie bei Sitzungen.
- Neues Recht, z. B. `USE_PLOTTER`; Schreibfläche bleibt Vorschau und Bestätigung im Chat
  (Rückfrage vor dem ersten Strich, mit Bild der geplanten Linien).
- Katalogeinträge `flux` und `plotter` im signierten Dienstkatalog.

## Umsetzungsschritte

1. HPGL-Modul als Rust-Bibliothek mit Tests (Koordinaten, Grenzen, Befehlsmenge). Keine Hardware.
2. Serieller Treiber gegen ein Test-Pty, danach gegen das Blackpill-Board.
3. Dienst `plotter` im Katalog, Rahmen für Auftrag und Zustand im Protokoll.
4. `GpuLease` und Warteschlange im Server (zusammen mit FLUX-Plan, Schritt 3).
5. Fluss „Blatt lesen → Fehler → Vorschau → Plot“ im Chat, danach Bildauftrag mit Wechsel.

## Offene Fragen

1. Welches Vision-Modell liest die Blätter, und läuft es neben dem Chat-Modell oder ersetzt es
   dieses (Auswirkung auf Schritt 1/2 und den VRAM-Bedarf)?
2. Koordinatensystem: Wer kalibriert (Papier einlegen, Referenzpunkte), und wo liegt die
   Kalibrierung (Runner-Konfiguration oder Server je Plotter)?
3. Blackpill-Protokoll: reicht reines HPGL mit Quittung je Zeile, oder gibt es eigene Befehle
   (Stift holen/ablegen, Notaus)?
4. Darf der Plotter von mehreren Benutzern des gemeinsamen Runners benutzt werden (Reihenfolge,
   physisches Blatt als Engpass)?
5. Soll Schritt 2 (Prüfung von Schülerarbeiten) ein eigener Chat-Ablauf werden oder ein
   Werkzeug des Runner-MCP-Servers?
