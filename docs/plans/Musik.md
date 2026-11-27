# M11 Musik: Songs erzeugen mit acestep.cpp auf dem GPU-Rechner

Planungsstand: 2026-11-27. Entwurf, noch keine Umsetzung, kein Download und keine
Installation auf dem GPU-Rechner.

## Anlass und Umfang

Der Umfang von M11 war offen (Plan.md, offene Frage 6). Anlass für diesen Entwurf ist das
Projekt [music-generator](https://github.com/rabbitfiremediacreation/music-generator)
(Hinweis des Nutzers, 2026-11-27). Es ist eine Web-App für einzelne Rechner. Für MultiGPT ist
weniger die App interessant als der Motor darunter und die gesammelten Erfahrungen.

Vorschlag für M11:

- **Musik erzeugen** im Chat: Beschreibung, Stil, Länge, Tempo, Tonart und Liedtext (oder
  „Liedtext erfinden“ bzw. „instrumental“) → ein Song als Audio-Anhang der Antwort.
- **Songs beschreiben:** Ein hochgeladener Song wird nach Stil, Tempo, Tonart und Liedtext
  beschrieben. Das deckt „Musik erkennen“ aus Frage 6 teilweise ab.
- **Erzeugungsdatensatz** für Musik **und Bilder**: Jede Erzeugung hält fest, womit genau sie
  entstand, und lässt sich daraus gezielt wiederholen oder abwandeln. Dieser Teil ist von der
  Musik unabhängig und kann vorher für Bilder umgesetzt werden (Stufe M11-01).

Nicht in M11: Musiksammlung auf dem NAS durchsuchen und abspielen, Akkorde, Cloud-Anbieter für
Musik. Bei Bedarf später eigene Punkte.

## Motor: acestep.cpp

[acestep.cpp](https://github.com/ServeurpersoCom/acestep.cpp) setzt das Musikmodell
[ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5) in C++17 mit GGML um. Text und
Liedtext gehen hinein, Stereo mit 48 kHz als WAV oder MP3 kommt heraus. Rechnet auf CPU,
CUDA, ROCm, Metal und Vulkan.

| Punkt | Stand (geprüft 2026-11-27) |
|---|---|
| Lizenz Code | acestep.cpp: MIT. ACE-Step 1.5: MIT |
| Lizenz Gewichte | `ACE-Step/Ace-Step1.5` und `Serveurperso/ACE-Step-1.5-GGUF` auf Hugging Face: MIT |
| Modelldateien (GGUF) | Sprachmodell `acestep-5Hz-lm-{0.6B,1.7B,4B}`, Klangmodell `acestep-v15-{sft,turbo,base,…}` (Standard, 2 Mrd. Parameter) und XL (4 Mrd.), Textencoder `Qwen3-Embedding-0.6B` |
| Server | `ace-server` mit Weboberfläche und HTTP-Schnittstelle, asynchron (siehe unten) |
| Pflege | aktiv, letzter Push 2026-11-17 |

**Abweichung von „alles Rust“:** acestep.cpp ist C++. Für Musik gibt es keine Rust-Umsetzung.
Wie bei den Bildmodellen (Eliezer.md: Übergangsweg über einen Dienst) startet und überwacht
Eliezer `ace-server` als verwalteten Nebenprozess auf dem GPU-Rechner. MultiGPT spricht nur
mit Eliezer, nie direkt mit `ace-server`. So bleibt ein späterer Wechsel des Motors eine Sache
von Eliezer.

Der GPU-Rechner hat eine AMD Navi 31 (FLUX-Runner.md). Ob acestep.cpp dort über ROCm oder
Vulkan stabil läuft, klärt der Versuch in Stufe M11-02.

## Erfahrungen aus music-generator

Quelle: `app/engines.py`, `app/klang.py`, `app/db.py`, `app/main.py` und README des Projekts
(gelesen 2026-11-27, nicht selbst nachgemessen).

**Ablauf von ace-server**

- Zwei Schritte, beide asynchron: `POST /lm` (Sprachmodell ergänzt Metadaten und Liedtext
  und erzeugt Audio-Codes) und `POST /synth` (Klangmodell und Decoder rendern das Audio).
  Beide liefern eine Auftrags-ID; `GET /job?id=…` meldet den Status, `&result=1` liefert das
  Ergebnis (`/synth` als `multipart/mixed`).
- Das Ergebnis bleibt auf dem Server abrufbar, auch wenn die Verbindung beim Abholen abreißt.
  Deshalb mit neuer Verbindung erneut abholen statt einen Fehler zu melden.
- Abgebrochene oder abgelaufene Aufträge ausdrücklich beim Server stoppen, sonst rechnet die GPU
  weiter. Für Eliezer wichtig, weil die GPU geteilt ist.
- `/understand` beschreibt einen hochgeladenen Song (Grundlage für „Songs beschreiben“).

**Einstellungen, die sie durch Messen gefunden haben**

- Klangmodell **SFT** statt „turbo“: klingt deutlich besser (50 Schritte).
- `peak_clip: 0`: Sonst normalisiert der Server auf das 99,999. Perzentil und schneidet darüber
  hart ab, laute Stellen knacken.
- Ausgabe als WAV mit 48 kHz und 16 Bit; MP3 erst beim Herunterladen erzeugen.
- `use_cot_caption: false`: Die Beschreibung wird wörtlich übernommen, nicht umgeschrieben.

**Grenzen und Fehlerbilder**

- Höchstens 600 Sekunden (3000 Codes bei 5 Codes je Sekunde). Das Sprachmodell liefert
  gelegentlich mehr Codes als bestellt, dann bricht die Synthese ab: auf Länge × 5 kürzen.
- Liedtext vom Modell erfinden lassen: Die Sprache muss ausgeschrieben übergeben werden („de“
  ergab Türkisch), die Temperatur höchstens 0,85, sonst zerfällt der Text in fremde Zeichen.
  Kommt trotz bestelltem Gesang nur „[Instrumental]“, das Sprachmodell noch einmal laufen
  lassen und den Seed des Songs behalten.
- Auf Apple M4 lieferte das Sprachmodell unter Metal Unsinn; sie rechnen es dann auf der CPU.
  Für AMD ist Ähnliches nicht ausgeschlossen: im Versuch prüfen.
- Speicherfehler erkennen sie in den Server-Logs ab Beginn der Synthese und melden sie
  verständlich. Eliezer sollte das ebenso tun und den Grund an MultiGPT weitergeben.

**Klang**

- Der Decoder verliert über 16 kHz rund 8 dB und zwischen 8 und 16 kHz knapp 3 dB (gemessen
  durch Kodieren und Dekodieren eines fertigen Stücks). Keine Synthese-Einstellung behebt das,
  XL bringt etwa 2 dB. Ausgleich danach: High-Shelf +6 dB ab 12 kHz (Steilheit 0,7, Biquad nach
  dem Audio-EQ-Cookbook), danach die Spitze auf 0,95 begrenzen. Etwa 70 Zeilen, in Rust leicht
  nachzubauen; gehört in Eliezer.

**Laufzeit und Speicher (ihre Messwerte)**

- 30-Sekunden-Song auf einem M4 mit 16 GB: 49 s mit dem Standard-Klangmodell, 99 s mit XL.
- 10-Minuten-Song: etwa 21 Minuten auf einem Mac mit 16 GB, etwa 32 Minuten auf einer
  GTX 1060 (6 GB).
- XL ab etwa 12 GB Grafikspeicher. Das gehört in die Speicherplanung von Eliezer und in die
  Austausch-Frage, wenn der Grafikspeicher voll ist.

**Reproduzierbarkeit**

- Jeder Song speichert die vollständige Anfrage als JSON und den Seed; ein zufälliger Seed
  wird vorher gezogen und ebenfalls gespeichert. Varianten zählen den Seed hoch (`seed + i`)
  und bilden eine Gruppe (A, B, C …).
- Einstellungen: Vorgaben im Code, in der Datenbank nur Abweichungen. Ein Wert gleich der
  Vorgabe wird gelöscht, damit spätere Vorgaben greifen.
- Lücken: Klang- und Sprachmodell, Höhenausgleich und die Vorgaben des Servers (`0` = Vorgabe)
  kommen beim Erzeugen aus den *aktuellen* Einstellungen und stehen nicht am Song. Der Seed des
  Sprachmodells wird gar nicht gesetzt. Mit gleichem Seed entsteht deshalb nicht sicher
  derselbe Song. Der Erzeugungsdatensatz unten schließt diese Lücken.

**Nicht übernehmen:** eigene Oberfläche, Installer, Song-Bibliothek und Ordner. Bei MultiGPT
ist ein Song ein Anhang im Chat; Versionen laufen über `Message.parent` wie bei Bildern.

## Erzeugungsdatensatz (Musik und Bilder)

### Befund heute (Bilder)

Ein erzeugtes Bild ist ein `Attachment` mit `generated_by_model`, `source_image` (bei
Bearbeitung), `tool_call` (Argumente des Werkzeugaufrufs mit Prompt, Format, Qualität) und
`cost`. Nicht festgehalten werden: Seed, Modelldatei und -stand, die tatsächlich verwendeten
Werte (Schritte, Guidance, Größe nach dem Einrasten), die Version von Eliezer und
Nachbearbeitungen. FLUX.2 klein auf Eliezer ließe sich mit festem Seed wiederholen; wir merken
ihn uns nur nicht. FLUX-Runner.md verlangt das bereits für jede Erzeugung.

### Modell `Generation` (Vorschlag)

Ein Datensatz je erzeugtem Ergebnis, nur anhängend (wird nach dem Anlegen nicht geändert):

| Feld | Inhalt |
|---|---|
| `kind` | `image`, `image_edit`, `music` |
| `attachment` | das Ergebnis (1:1) |
| `message`, `owner` | Antwort und auslösendes Konto |
| `ai_model` | Modell in MultiGPT (SET_NULL; Kennung zusätzlich als Text) |
| `requested` | JSON: was Nutzer oder Modell verlangt haben (Prompt, Stil, Länge, „Qualität“ …) |
| `resolved` | JSON: alle tatsächlich verwendeten Werte, Vorgaben eingesetzt, **alle Seeds** (bei Musik Sprachmodell und Synthese), Größe nach dem Einrasten |
| `engine` | JSON: Anbieter, Motor und Version (z. B. Eliezer-Version, acestep.cpp-Commit), Modelldateien mit SHA-256 |
| `postprocess` | JSON-Liste: Schritte mit Parametern (z. B. Höhenausgleich +6 dB/12 kHz/0,7, Spitze 0,95; Einrasten auf Zielgröße) |
| `sources` | Eingaben (Ausgangsbilder, Maske, hochgeladener Song) als Anhänge |
| `variant_of`, `variant_index` | Gruppe und Platz einer Variante (A, B, C …) |
| `reproducible` | ja/nein, mit Grund (z. B. „Anbieter meldet keinen Seed“) |
| `duration_ms`, `created` | Laufzeit und Zeitpunkt |

Regeln:

1. **Wiederholen und Varianten nur aus `resolved`**, nie aus den aktuellen Einstellungen.
   „Erneut erzeugen“ übernimmt alles samt Seed; „weitere Variante“ zählt den Seed der Synthese
   hoch. Fehlt eine Modelldatei mit dem gespeicherten Hash, sagt MultiGPT das, statt still eine
   andere zu nehmen.
2. **Cloud-Anbieter** (OpenAI, Gemini) melden meist keinen Seed: Datensatz trotzdem anlegen,
   `reproducible = nein` mit Grund. Varianten gehen dann nur als neue Erzeugung.
3. Ein gleicher Seed erlaubt kontrollierte Vergleiche, garantiert aber **keine bitgleichen
   Ergebnisse** über andere Hardware oder Softwarestände (FLUX-Runner.md). Die Oberfläche sagt
   „mit denselben Einstellungen“, nicht „identisch“.
4. Vorgaben je Modell: wie bei music-generator Vorgaben im Code bzw. in Eliezer, in der
   Datenbank nur Abweichungen des Verwalters. In `resolved` steht immer der eingesetzte Wert.
5. Sichtbar im Chat unter dem Anhang („Einstellungen“ aufklappen: Modell, Seed, Werte) und im
   Export. Über MCP liefert `get_file` die Werte mit.

### Anforderung an Eliezer

Eliezer meldet in jeder Antwort einer Erzeugung die tatsächlich verwendeten Werte, ohne das
OpenAI-Format zu brechen (unbekannte Felder ignorieren OpenAI-kompatible Clients). Beispiel,
Dateiname und Motor nur zur Veranschaulichung:

```json
{"created": 0, "data": [{"b64_json": "…", "revised_prompt": null}],
 "eliezer": {"version": "…", "engine": "stable-diffusion.cpp@…",
             "model_files": [{"name": "flux-2-klein-4b-Q8_0.gguf", "sha256": "…"}],
             "resolved": {"seed": 1234, "steps": 4, "guidance": 1.0, "width": 1024, "height": 1024},
             "postprocess": []}}
```

`seed` darf in der Anfrage mitkommen; ohne Seed zieht Eliezer einen und meldet ihn. Die Namen
der Felder stimmt die Eliezer-Sitzung ab.

## Schnittstelle Eliezer ↔ MultiGPT für Musik (Vorschlag)

Ein Song dauert Minuten. Deshalb asynchron, angelehnt an `ace-server`, aber stabil gegenüber
einem Motorwechsel:

- `POST /v1/music` → `{"id": …}`. Felder: `model`, `caption`, `lyrics` (leer = erfinden,
  `"[Instrumental]"`), `duration`, `bpm`, `keyscale`, `timesignature`, `vocal_language`
  (Code; Eliezer übersetzt in den ausgeschriebenen Namen), `seed`, `lm_seed`, `steps`,
  `lm_temperature`, `variant` (XL oder Standard), `postprocess` (z. B. `highs`).
- `GET /v1/jobs/{id}` → Status und Fortschritt; `GET /v1/jobs/{id}/result` → WAV und der
  `eliezer`-Block wie oben (beide Seeds, Modelldateien, Werte, Nachbearbeitung).
- `DELETE /v1/jobs/{id}` → Abbruch bis in `ace-server`.
- `POST /v1/music/describe` (Datei) → Stil, Tempo, Tonart, Takt, Liedtext.
- Modellmeldung: `capabilities.music = true` und `max_duration` (Sekunden) in `GET /api/v1/models`; MultiGPT ordnet das
  Modell der vorhandenen Fähigkeit `music` zu (`AIModel.Capability.MUSIC`).

Eliezer übernimmt die Erfahrungen oben: SFT als Vorgabe, `peak_clip: 0`, Kürzen der Codes,
Wiederholung bei fehlendem Liedtext, Abholen mit Wiederholung, Logs auswerten, Höhenausgleich,
ein Musikauftrag je GPU und Abstimmung mit geladenen Sprach- und Bildmodellen.

## MultiGPT-Seite

- **Werkzeug `generate_music`** und ein Modus „Musik“ im Eingabefeld, wie bei Bildern. Freigabe
  je Rolle über die vorhandene Modellfreigabe; Jugendliche nur, wenn freigegeben.
- **Hintergrund:** Ein Song passt nicht in eine Anfrage (gunicorn-Timeout 300 s). Der Auftrag
  läuft im Worker (M17-01 bzw. M17-02); bis dahin zeigt die Antwort „Song wird erzeugt …“ mit
  Fortschritt und Abbruch. M11-03 hängt deshalb an M17.
- **Ergebnis:** `Attachment` vom Typ `audio` an der Antwort, Player im Chat (HTML `audio`),
  Herunterladen als WAV oder MP3 (MP3 bei Bedarf erzeugt und zwischengespeichert).
- **Versionen:** „Neu erzeugen“ und „weitere Variante“ legen Geschwister über `Message.parent`
  an, mit dem Erzeugungsdatensatz als Quelle.
- **Kosten (entschieden):** Lokale Musik kostet kein Geld. Gezählt werden Sekunden Audio
  (`units={"audio_second": n}`), keine GPU- oder Container-Minuten.
- **Höchstlänge (entschieden):** je Rolle, Vorgabe 20 Minuten, begrenzt durch die Höchstlänge,
  die Eliezer meldet (siehe Entscheidungen).
- **Aufbewahrung (entschieden):** dauerhaft wie andere Anhänge.
- **MCP:** `generate_music` auch über den MCP-Server, Ergebnis wie bei Bildern als Anhang mit
  `get_file`.

## Stufen

| Stufe | Ergebnis | Abnahme |
|---|---|---|
| M11-01 Erzeugungsdatensatz für Bilder | Modell `Generation`, Anlage bei jeder Bilderzeugung und -bearbeitung, Anzeige „Einstellungen“, „Erneut erzeugen“ aus `resolved`; Eliezer meldet Seed und Werte | Bild mit festem Seed zweimal erzeugen → gleiche Werte im Datensatz; Cloud-Bild hat `reproducible = nein` mit Grund; Verwalter ändert Vorgabe → „Erneut erzeugen“ nutzt trotzdem die alten Werte |
| M11-02 Versuch auf dem GPU-Rechner | acestep.cpp gebaut (ROCm oder Vulkan), Modelle geladen, Laufzeit und Grafikspeicher für 30 s und 3 min gemessen, Standard gegen XL, Sprachmodell auf GPU gegen CPU | Messwerte dokumentiert; nur nach OK des Nutzers installieren und laden (Eliezer.md: dort nichts laden/entladen ohne OK) |
| M11-03 Eliezer Musik | `ace-server` als verwalteter Nebenprozess, Schnittstelle oben, Höhenausgleich in Rust, Speicherplanung | Song über Eliezer erzeugen, abbrechen, Ergebnis nach Verbindungsabbruch erneut abholen |
| M11-04 MultiGPT Musik | Werkzeug, Modus, Hintergrundauftrag (setzt M17 voraus), Audio-Anhang mit Player, Versionen, Kosten, MCP | Song im Chat, Variante mit hochgezähltem Seed, Abbruch stoppt die GPU, Jugendkonto ohne Freigabe sieht das Werkzeug nicht |
| M11-05 Songs beschreiben | `describe` über Eliezer, Upload eines Songs im Chat | Beschreibung mit Tempo und Tonart; „Daraus einen Song machen“ füllt die Felder vor |
| M11-06 Doku | Wiki-Seite „Musik“, Website, Release-Notes | — |

M11-01 hängt technisch nicht von der Musik ab, wird aber nach Entscheidung des Nutzers nicht
vorgezogen: Reihenfolge M11-02 → M11-03 → M11-01 → M11-04 → M11-05 → M11-06.

## Entscheidungen des Nutzers (2026-11-27)

1. **Aufbewahrung:** Erzeugte Songs werden dauerhaft gespeichert, wie andere Anhänge (Antwort
   „ja“ auf „dauerhaft speichern?“; keine eigene Frist).
2. **Kontierung:** nach **Sekunden Audio** (`units={"audio_second": n}`), keine GPU-Minuten.
3. **Höchstlänge:** **20 Minuten** als Grenze je Rolle (Feld an `Role`, Vorgabe 20 min, der
   Verwalter kann sie je Rolle senken). **Achtung:** acestep.cpp erzeugt höchstens 600 s
   (10 min) am Stück. Für mehr als 10 Minuten braucht es ein Fortsetzen bzw. Verlängern eines
   Songs; ob ACE-Step 1.5 bzw. acestep.cpp das sauber kann, klärt M11-02. Bis dahin begrenzt
   MultiGPT auf das, was Eliezer als Höchstlänge meldet (`max_duration` in der Modellmeldung),
   und sagt das an.
4. **XL:** freigeschaltet. Eliezer bietet Standard und XL an; XL braucht etwa 12 GB
   Grafikspeicher und läuft über die normale Speicherplanung und Austausch-Frage von Eliezer.
5. **M11-01 nicht vorziehen:** Der Erzeugungsdatensatz entsteht zusammen mit der Musik, nicht
   vorab für Bilder. Reihenfolge: M11-02 (Versuch) → M11-03 (Eliezer) → M11-01 (Datensatz für
   Bilder und Musik) → M11-04 ff.

## Offene Fragen

1. Umfang so recht (Erzeugen, Beschreiben, Erzeugungsdatensatz; ohne NAS-Sammlung und Akkorde)?
2. Wie geht es über 10 Minuten: Fortsetzen im Motor (falls M11-02 das hergibt) oder mehrere
   Teile mit Überblendung? Erst nach dem Versuch entscheidbar.

Keine Installation, kein Modell-Download und keine Änderung an Eliezer werden allein durch
dieses Planungsdokument ausgelöst.
