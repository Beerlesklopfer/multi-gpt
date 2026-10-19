# Runner als Hardware-Dienst: GPU-Wechsel und HPGL-Plotter

Planungsstand: 2026-10-19. Entwurf, noch keine Implementierung. Zielversion legt der Nutzer fest.
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

## Eigener Orchestrator statt LM Studio – Recherche Hugging Face (2026-10-19)

Richtung des Nutzers: LM Studio durch einen eigenen Orchestrator ersetzen (Chat-, Vision- und
Bildmodelle, ein Verwalter je GPU). Frühere Recherche zu diffusers, mistral.rs und candle steht im
Familienrechner-Plan; hier das, was Hugging Face für **Sprachmodelle** bietet.

| Baustein | Was er kann | Was fehlt für den Ersatz |
|---|---|---|
| **TGI** (Text Generation Inference, Apache-2.0) | Produktionsserver, Backends u. a. llama.cpp und TensorRT-LLM | **Wartungsmodus**: nimmt nur noch kleine Korrekturen an und empfiehlt für Neues vLLM, SGLang oder llama.cpp/MLX. Ein Modell je Start (`--model-id`), kein Laden/Entladen. Fällt aus. |
| **`transformers serve`** (HF, ab Transformers v5) | OpenAI-kompatibel (`/v1/chat/completions`, `/v1/responses`, Audio, Tool-Aufrufe, Bilder/Audio/Video); **lädt jedes Modell bei Bedarf** (`/load_model` mit Fortschritt per SSE), **entlädt nach 300 s Leerlauf** (`--model-timeout`, `-1` aus), Continuous Batching | Selbst als „leichtgewichtig für Test und mäßige Last“ beschrieben, für große Produktion verweist HF auf vLLM/SGLang. Keine Warteschlange/Verdrängung zwischen Modellen belegt, VRAM-Meldung nicht dokumentiert, kein GGUF-Weg. |
| **llama.cpp `llama-server` Router-Modus** (jetzt im HF-Umfeld `ggml-org`) | Ohne `-m` gestartet: **ein Prozess je Modell**, Entdeckung aus Cache oder `--models-dir`, Laden bei erster Anfrage, **LRU-Verdrängung** ab `--models-max` (4), `GET /models` (loaded/loading/unloaded), `POST /models/load` und `/models/unload`, Voreinstellungen je Modell (`--models-preset`, z. B. `ctx-size`) | Kein Leerlauf-Entladen im Router belegt (Diskussion „Model router never unloads a model automatically“, nur Titel gelesen); `--sleep-idle-seconds` gilt je Server. Keine VRAM-Rechnung, keine Rangfolge. |
| **candle-vllm** (Community auf HF-Candle) | OpenAI-API, Rust | Ein Modell je Instanz, laut README „instabil“. |
| **llama-swap** (Fremdwerkzeug) | Proxy: liest `model` der Anfrage, startet/stoppt den passenden Backend-Server (llama.cpp, vLLM) | Kein HF-Produkt, aber genau das Muster „Orchestrator vor Servern“. |

**Schluss:** Hugging Face hat **keinen fertigen Orchestrator** im Umfang von LM Studio. Die
tragenden Teile sind zwei Laufzeiten mit Lebenszyklus: `transformers serve` (PyTorch, volle
Gewichte, Laden bei Bedarf plus Leerlaufentladen) und der llama.cpp-Router (GGUF, Prozess je
Modell, LRU). HF selbst rät bei Last zu vLLM/SGLang. Der Rest – VRAM-Buch, Rangfolge,
Warteschlange, Verdrängung zwischen Chat und Bild, Abrechnung – bleibt Eigenbau und passt zum
Runner: Dienst-Container je Laufzeit, Steuerung über den kleinen Vertrag (anfordern, Status/VRAM,
abbrechen, freigeben).

**Was MultiGPT heute von LM Studio nutzt** (muss der Ersatz leisten): Modellliste samt
Zustand und Speicherschätzung, Laden/Entladen mit Kontext (`fit_context`), Parallelität
(`parallel`), Denkoptionen je Modell (`none` = aus), Verdrängungsangebot (SwapPossible),
Statistik je Antwort (Token/s, Prompt-Zeit). Davon deckt der llama.cpp-Router Laden/Entladen,
Kontext je Modell und Status ab; Denkoptionen, Statistik und Speicherschätzung müsste der
Orchestrator selbst liefern.

**Offene Prüfungen vor der Entscheidung:**
1. Reicht llama.cpp-Router + Eigenbau (GGUF, Prozess je Modell) oder soll `transformers serve`/vLLM
   dahinter? Messung auf der 24-GB-Karte: Ladezeit, Token/s, Qualität gegenüber LM Studio.
2. Leerlauf-Entladen und Wechsel Chat ↔ Bild: macht der Orchestrator selbst (Zeitgeber), nicht
   die Laufzeit.
3. Aufwand: LM-Studio-Anbindung (Provider `lmstudio`, Verwaltung, Statistik) läuft weiter, bis
   der Orchestrator denselben Vertrag erfüllt – Umstieg je Provider, nicht auf einmal.

## Entscheidung 2026-10-19: alles Rust

Der Nutzer legt fest: Orchestrator, Treiber und Laufzeiten werden **in Rust** gebaut. Das ersetzt
die Python-/C++-Laufzeiten der Recherche oben als Ziel; sie bleiben Vergleichsmaßstab und Übergang.

Folgen für den Entwurf:

- **Ein Programm, ein Verwalter je GPU:** Der Orchestrator wird ein Modul/Crate im Runner
  (`runner-rs`) oder ein eigener Dienst-Prozess desselben Workspaces. Er hält VRAM-Buch, Warteschlange
  und Zeitgeber (Leerlauf-Entladen) selbst und spricht mit MultiGPT über den bestehenden signierten
  Kanal; Benutzer-Container sehen weder GPU noch Orchestrator.
- **Laufzeit für Sprachmodelle:** **mistral.rs** (Rust, MIT) ist der natürliche Kandidat: Chat,
  Vision, Sprache und Embeddings in einer Laufzeit, OpenAI-kompatibel, `unload`/`reload`/`status`
  zur Laufzeit, auch als Bibliothek einbettbar. Zweiter Weg: Anbindung an llama.cpp über eine
  Rust-Bindung (GGUF), dann aber C++ im Prozess. **candle** (HF) bleibt Baukasten darunter.
- **Bildmodelle sind die Lücke:** In Rust gibt es FLUX derzeit nur in mistral.rs, dort **nur in
  voller Genauigkeit** (`Flux`/`FluxOffloaded`, keine Quantisierung); candle hat kein FLUX-Beispiel.
  Auf 24 GB ist das knapp (siehe Familienrechner-Plan). Bis das gemessen oder gelöst ist, gilt:
  Bilddienst als **ausgetauschter Dienst-Container** hinter demselben Vertrag, erst später Rust.
- **HPGL und Blackpill** sind ohnehin Rust: reines Rechenmodul plus serieller Treiber
  (`tokio-serial`), ohne fremde Laufzeit.
- **Was fehlt und selbst zu bauen ist:** Denkoptionen je Modell, Statistik je Antwort (Token/s),
  Speicherschätzung, Verdrängung und Rangfolge, VRAM-Meldung (NVML-Bindung oder `nvidia-smi`).
- **Vertrag bleibt klein** (anfordern, Status/VRAM, abbrechen, freigeben), damit die Rust-Laufzeit
  Schritt für Schritt hinter dem Provider `lmstudio` hervortreten kann.

Nächster Schritt (Versuch): mistral.rs auf dem GPU-Rechner messen – ein Chat-Modell und FLUX.1 –
mit Ladezeit, Token/s, VRAM, `unload`/`reload` und Anfrage an ein entladenes Modell.

## Entscheidung 2026-10-19: mistral.rs

Die Laufzeit ist **mistral.rs**. Modelle werden mit einem **Hugging-Face-Token** nachgeladen.

- **Token:** nur auf dem GPU-Rechner, als Datei mit Rechten 0600 oder Umgebungsvariable des
  Dienstes (`HF_TOKEN`); nie im Repository, nie im Chat, nie in Logs und nicht in der Datenbank
  des Servers. Lesezugriff genügt (Token-Art „read“, möglichst auf die nötigen Modelle begrenzt).
  Der Orchestrator reicht ihn nur an die Laufzeit weiter, die Benutzer-Container sehen ihn nicht.
- **Modellauswahl:** eine Positivliste (Modell-ID und Revision/Digest) im Orchestrator; geladen
  wird nur, was dort steht. Gesperrte oder lizenzpflichtige Modelle (z. B. FLUX.1 dev) brauchen
  zuerst die Zustimmung zur Lizenz im HF-Konto des Tokens.
- **Speicherort:** ein eigenes Cache-Verzeichnis (`HF_HOME`) auf einem Volume mit Größengrenze;
  Herunterladen nur auf Anforderung des Verwalters, mit Fortschrittsmeldung.
- **Nächster Schritt:** Versuch auf dem GPU-Rechner (dieser Entwicklungsrechner hat keine GPU).

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
