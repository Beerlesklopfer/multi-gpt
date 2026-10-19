# Gemeinsamer Runner (1 Runner : n Benutzer)

Planungsstand: 2026-10-18. Entwurf, noch keine Implementierung. Zielversion legt der Nutzer fest.

## Ziel

Ein Familienrechner (z. B. mit GPU) ist als **ein** Runner gekoppelt. Jeder berechtigte
Benutzer des Webservers kann ihn im Chat benutzen – mit eigenem Arbeitsstand, eigenem
Kontingent und eigenen Rechten, ohne selbst ein Programm zu installieren oder zu koppeln.

Heute gehört ein Runner genau einem Konto (1:1). Das Modell dahinter:

| Stelle | Heute | Folge |
|---|---|---|
| `RunnerToken.owner` | ein Konto je Token; `Runner.token` ist 1:1 | der Runner „gehört“ dem Koppelnden |
| `chat._online_runner(user)` | filtert `token__owner=user` | ein Benutzer sieht nur eigene Runner |
| `mcp_server.ensure_mcp_server` | Eintrag „Runner (<Konto>)“ mit `owner` | nur der Besitzer darf ihn benutzen (`permissions._can_use_mcp_server`) |
| `gateway.Connection.owner_id` | Besitzer des Tokens | Kontingent-Stopp, Rechteänderung und Audit laufen über den Token-Besitzer |
| `RunnerSession.owner`, `RunnerWorkspace.owner`, `RunnerAudit.owner` | Benutzer der Sitzung | **bleiben richtig**: Sitzung, Arbeitsstand und Audit gehören dem Benutzer, nicht dem Rechner |

Gut ist: Sitzung, Arbeitsstand, Kontingent und Audit hängen schon am **Benutzer**. Das Neue ist
nur die Zuordnung „welcher Runner darf für welchen Benutzer arbeiten“ und alles, was dadurch
nicht mehr selbstverständlich ist (Trennung, Ressourcen, Ports, Rechte).

## Vorgeschlagenes Modell

- **Betreiber** koppelt den Runner wie bisher (Token gehört dem Betreiber) und markiert ihn als
  **gemeinsam** (`Runner.shared`). Nur ein Verwalter darf das (Admin oder Recht „Gemeinsamen
  Runner betreiben“).
- Neue Tabelle **`RunnerShare`** (runner, Rolle *oder* Konto, aktiv, optional Grenzen): wer den
  Runner benutzen darf. Ohne Eintrag sieht niemand außer dem Betreiber den Runner.
- Rollenrecht „Runner nutzen“ bleibt **zusätzlich** nötig; es gilt für den **Benutzer**, nicht
  für den Betreiber. Die Grenzen (CPU, Speicher, Netz, Pakete, Arbeitsstände, Laufzeit) kommen
  aus der Rolle des Benutzers, begrenzt durch die Obergrenzen des Rechners (`Runner.shared_limits`).
- **Auswahl im Chat:** `_online_runner(user)` liefert eigene Runner und freigegebene; bei
  mehreren wählt der Benutzer (Standard: eigener vor gemeinsamem). Der Arbeitsstand merkt sich
  den Runner nicht – ein Arbeitsstand ist ein git-Bundle auf dem Server und läuft auf jedem
  Runner.
- **MCP-Eintrag:** statt „ein Eintrag je Konto“ ein Eintrag **je Runner und Benutzer** („Runner
  Familienrechner (<Konto>)“), automatisch angelegt beim ersten Gebrauch. Berechtigung:
  Besitzer des Eintrags *und* Freigabe *und* Rollenrecht.
- **Gateway:** `Connection` kennt weiter den Betreiber (Token), aber `session_start`,
  Kontingent-Stopp und Rechteänderung adressieren **Sitzungen nach Benutzer** statt nach
  `conn.owner_id`. Ein Kontingent-Stopp des Benutzers A beendet nur dessen Sitzungen.

## Trennung und Sicherheit (der kritische Teil)

Alle Container laufen unter **einem** Linux-Benutzer und **einem** Podman des Rechners. Die
Sicherheit gegen andere Benutzer beruht deshalb allein auf der festen Container-Vorlage.

1. **Kein „lokales Scratchpad“ im gemeinsamen Betrieb.** Der Modus „lokal“ bindet einen Ordner
   des Rechners ein; auf einem gemeinsamen Runner nur mit eigenem, je Benutzer getrenntem
   Unterordner und Verwalter-Freigabe, sonst gesperrt (Standard).
2. **Sitzungen sind gegeneinander abgeschottet:** je Sitzung ein eigener Container ohne
   gemeinsame Volumes, kein gemeinsames Netz zwischen Sitzungen (Standard „kein Netz“).
   Netzmodus „Paketquellen“ nur über den Proxy der jeweiligen Sitzung.
3. **GPU:** nie in der Standard-Vorlage; Zugriff nur über Dienste und den Makler
   (Abschnitt „GPU-Verwaltung“).
4. **Vorschau-Ports (24080–24089):** nur zehn Paare je Rechner und aus dem Heimnetz offen. Bei
   mehreren Benutzern wird das knapp: Anzahl je Benutzer begrenzen, Zuteilung im Server statt im
   Runner, Token je Vorschau beibehalten.
5. **Sichtbarkeit:** der Betreiber sieht wie bisher nur Metadaten seiner Sitzungen; Sitzungen
   anderer Benutzer auf seinem Rechner erscheinen für ihn **nur als Metadaten** (Benutzer,
   Zeit, Größe), nie Inhalte oder Argumente.
6. **Aufräumen:** `adopt()` übernimmt Container nach Label und der Server beendet Unbekanntes.
   Mit mehreren Benutzern muss jede Sitzung ihren Benutzer im Label tragen, und der Abgleich
   beim `hello` nach Sitzungs-ID erfolgen (nicht nach Besitzer).
7. **Missbrauch:** Ratenbegrenzung und Obergrenzen je Benutzer und insgesamt (Sitzungen,
   CPU, RAM), damit einer den Rechner nicht für alle blockiert; Warteschlange statt Fehler.
8. **Zustimmung:** Der Betreiber muss die Freigabe ausdrücklich erteilen; sie lässt sich
   jederzeit entziehen (laufende Sitzungen werden beendet, Arbeitsstände bleiben).

## GPU-Verwaltung (Kernproblem)

Container teilen sich eine GPU nicht von selbst vernünftig: Jeder Prozess nimmt VRAM, ohne die
anderen zu fragen; läuft er voll, scheitert einer mit „out of memory“. PyTorch selbst verteilt
nichts zwischen Prozessen (nur Grenzen im eigenen Prozess), und MIG gibt es nur bei
Rechenzentrums-GPUs, nicht bei Heimkarten. Auf demselben Rechner läuft außerdem LM Studio mit
geladenen Chat-Modellen und belegt VRAM. Gebraucht wird **eine** Stelle, die den GPU-Speicher
und die Rechenzeit zuteilt.

Vorschlag: **Makler statt freier Zugriff.**

1. **Kein Benutzer-Container bekommt die GPU.** Nur dienste-eigene Container des Runners
   (FLUX, später weitere) sehen sie, über das Gerät aus der CDI-Konfiguration von Podman und nur
   in der festen GPU-Vorlage. Die Werkzeug-Container der Benutzer (`bash`, `python`) bleiben ohne GPU.
2. **Der MultiGPT-Server führt Buch** (neues `GpuLease`-Modell je GPU): Wer hält wie viel VRAM
   für wie lange? Quellen sind die Schätzungen, die MultiGPT für LM-Studio-Modelle schon führt
   (`ModelRuntime`, Auslagern festgehaltener Modelle mit Rückfrage), plus ein VRAM-Bedarf je
   Dienstprofil (FLUX: feste Zahl pro Auflösung/Modell).
3. **Aufträge statt Sitzungen:** Ein GPU-Auftrag (Bild, später Training/Inferenz) kommt als
   strukturierte Anfrage an den Server, nicht als Shell im Container. Der Server entscheidet:
   starten, **warten (Warteschlange mit Position im Chat)** oder ablehnen. Reihenfolge fair je
   Benutzer, Grenze je Rolle (Aufträge, Minuten).
4. **Austausch mit LM Studio:** Reicht der freie VRAM nicht, bittet der Server LM Studio, nicht
   gebrauchte Modelle zu entladen (nur selbst geladene Instanzen, wie schon umgesetzt) – bei
   festgehaltenen Modellen mit Rückfrage an den Verwalter. Nach dem Auftrag gibt der Dienst
   den Speicher wieder frei (Entladen nach Leerlauf), und das Chat-Modell wird bei Bedarf
   wieder geladen.
5. **Der Runner meldet nur Tatsachen:** GPUs, freien/belegten VRAM (z. B. über `nvidia-smi`
   bzw. NVML), Dienststatus. Die Entscheidung liegt beim Server, die Durchsetzung beim Runner
   (Start nur auf signierten Befehl; Abbruch bei Überschreitung).

Warum nicht gleich ein Fremdprodukt als Verwalter? Ein Scheduler wie Kubernetes mit
GPU-Operator oder Ray löst das allgemein, ist aber für einen Familienrechner viel zu schwer und
kennt weder LM Studio noch das Rechtemodell von MultiGPT. Lohnend ist das erst bei mehreren
GPU-Rechnern. Der Plan hält deshalb die Schnittstelle (Lease, Warteschlange, Meldung) klein,
sodass später ein anderer Verwalter dahinter stehen kann.

### Richtung: ein Lader übernimmt das Laden und Entladen

Wahrscheinlich landet es bei einem **Lader**, der Modelle auf Anforderung lädt, im Leerlauf
entlädt, bei Platzmangel verdrängt und Anfragen in eine Schlange stellt – so wie LM Studio es
für Chat-Modelle heute schon tut (Laden bei Bedarf, Leerlaufzeit, Auto-Evict). Das heißt für
den Plan:

- MultiGPT baut **keinen eigenen GPU-Scheduler**, sondern behält nur die **Richtlinie**: wer
  darf was, Rangfolge (Chat vor Bild?), Grenzen je Rolle, Abrechnung. Das Laden, Entladen und
  die Warteschlange erledigt der Lader.
- `GpuLease` schrumpft zu einer **Anzeige und Vorab-Prüfung** („passt das noch?“), nicht zur
  Quelle der Wahrheit. Maßgeblich ist, was der Lader meldet.
- Der Runner startet den Lader als **Dienst-Container** (feste GPU-Vorlage) und reicht
  Aufträge an dessen Schnittstelle weiter; Benutzer-Container sehen weder GPU noch Lader.
- Der **Vertrag** zwischen MultiGPT und Lader bleibt klein: Modell anfordern, Status/VRAM
  abfragen, Auftrag abbrechen, Modell freigeben. Dann sind Lader austauschbar.
- Für Sprachmodelle ist LM Studio bereits dieser Lader. Für Bildmodelle (FLUX) gibt es keinen
  gleichwertigen Standard; denkbar sind ein eigener schlanker Dienst im Image oder ein
  vorhandenes Werkzeug mit Warteschlange und Modellverwaltung. **Vor der Entscheidung prüfen**,
  was sich als Dienst-Container einbinden lässt und eine stabile Schnittstelle hat.
- Offen bleibt, **ob LM Studio und der Bild-Lader sich VRAM teilen**. Zwei unabhängige Lader
  kennen einander nicht; dann braucht MultiGPT trotzdem die Rangfolge (zuerst entladen, dann
  laden) – oder beide Lasten laufen auf getrennten GPUs.

### Recherche Hugging Face / diffusers (2026-10-18)

Gefunden in der diffusers-Dokumentation („Reduce memory usage“, FLUX-Seite, GGUF, Server-Beispiele):

- **Nur Werkzeuge je Pipeline, kein Verwalter.** diffusers kennt `device_map="balanced"` (mehrere
  GPUs), `enable_model_cpu_offload()` (ganze Teilmodelle wandern bei Bedarf auf die GPU),
  `enable_sequential_cpu_offload()` (sehr sparsam, „extrem langsam“), Gruppen-Offloading
  (`enable_group_offload`, block- oder blockweise, mit CUDA-Streams, auch auf Platte), fp8-
  Layerwise-Casting, Quantisierung (bitsandbytes, optimum-quanto, GGUF) sowie VAE-Slicing und
  -Tiling. Nichts davon regelt, was **mehrere Prozesse oder Modelle** auf derselben GPU tun.
- **FLUX-Bedarf:** ohne Optimierung etwa 50 GB (RAM/VRAM); mit fp8-Gewichten und Offloading
  „unter 16 GB VRAM“; 4–32 GB mit sequenziellem Offloading (langsamer).
- **GGUF:** in diffusers über `from_single_file` am Modell (nicht an der Pipeline) mit
  `GGUFQuantizationConfig`; dequantisiert wird je Schicht beim Vorwärtslauf. Sequenzielles
  Offloading ist laut Praxisberichten damit nicht kompatibel.
- **Server-Beispiele:** `examples/server` (eine Pipeline, mehrere Anfragen) und `server-async`
  (parallele Anfragen, nur ab etwa 35 GB VRAM empfohlen). Eine eingebaute Warteschlange oder
  Modellverdrängung gibt es nicht; Community-Beispiele nutzen eine begrenzte FastAPI-Schlange.
- **Muster aus der Forschung/Industrie:** Modelle bei Leerlauf aus dem GPU-Speicher in einen
  CPU-Speicherpool auslagern und bei Anfrage zurückholen (z. B. Computron; Snowflake für vLLM).
  Genau das ist die Aufgabe des gesuchten Laders.
- **Alternative Laufzeit:** stable-diffusion.cpp (GGUF, eigener Server, FLUX auch mit 4–6 GB) im
  Umfeld von llama.cpp. Achtung: GGUF-Dateien sind nicht untereinander austauschbar (ComfyUI-
  Dateien von city96 laden dort nicht, die von leejet schon).

**Schluss für die Vereinheitlichung:** Es gibt bei Hugging Face **keinen fertigen Lader** wie
LM Studio. Machbar ist ein kleiner **GPU-Arbeiter**: ein einziger langlebiger Prozess je GPU
(Dienst-Container des Runners), der die Pipelines hält, bei Bedarf auf die GPU holt und im
Leerlauf in den CPU-Speicher zurückschiebt, Aufträge nacheinander aus einer Schlange abarbeitet
und freien/belegten VRAM (`torch.cuda.mem_get_info`) meldet. Ein Prozess je GPU vermeidet
den Streit mehrerer PyTorch-Prozesse. Gegenüber LM Studio bleibt die Kopplung zu beachten
(zwei Lader auf derselben GPU kennen einander nicht, siehe oben). Eine gemeinsame Laufzeit für
Chat- **und** Bildmodelle wäre nur in einem der beiden Ökosysteme möglich; das ist eine größere
Entscheidung und bisher nicht untersucht (insbesondere Chat-Modelle über PyTorch/vLLM).

### Recherche Rust (2026-10-18)

- **mistral.rs** (MIT, Rust, CUDA, OpenAI-kompatibler Server `mistralrs serve`): Chat-, Bild-,
  Sprach- und Embedding-Modelle in **einer** Laufzeit. Mehrere Modelle laufen je in einer
  eigenen Engine (Konfiguration per TOML), Auswahl über das Feld `model`; zur Laufzeit lassen
  sich Modelle mit `POST /v1/models/unload`, `…/reload` und `…/status` entladen, nachladen und
  abfragen. **FLUX.1** ist als Architektur `Flux` bzw. `FluxOffloaded` aufgeführt (Beispiel
  schnell; dev nicht bestätigt, FLUX.2 nicht erwähnt), aber nur in **voller Genauigkeit** –
  keine Quantisierung für Diffusionsmodelle. Offen/unbelegt: automatisches Nachladen bei
  Anfrage, Leerlauf-Entladen, Warteschlange, ob Bildmodelle im Mehrmodell-Betrieb laufen,
  Speicherverteilung zwischen Modellen. Es ist damit der **nächste Kandidat für „ein Lader
  für alles“**, aber nicht LM-Studio-gleich.
- **candle** (Hugging Face, Rust): Stable-Diffusion-Beispiele (SD 1.5/2.1/SDXL, Turbo), GGUF
  nur für Sprachmodelle. Ein FLUX-Beispiel oder FLUX-GGUF fand sich nicht.
- **diffusers-rs** (auf `tch`/libtorch): nur Stable Diffusion 1.5/2.1, für FLUX unbrauchbar.
- **diffusion-rs** und **rs-flint**: Rust-Anbindungen an stable-diffusion.cpp. Für FLUX
  nur bei rs-flint (FLUX.2 Klein) belegt, dessen aktuelle docs.rs-Version nicht baut – also
  unausgereift.
- **Folge für den Runner:** Der Runner selbst bleibt reiner Steuerer (Podman, Aufträge,
  Meldungen). Die Bildlaufzeit gehört in einen **Dienst-Container**; ob Python/diffusers (am
  weitesten, alle Optimierungen), mistral.rs (Rust, ein Server für alles, FLUX nur unquantisiert)
  oder stable-diffusion.cpp (GGUF, wenig VRAM) – das ist eine Messfrage auf der Zielhardware
  (VRAM, Geschwindigkeit, Qualität). Der kleine Vertrag zwischen MultiGPT und Lader hält die Wahl offen.

### Einordnung mistral.rs gegenüber dem Runner

mistral.rs und der Runner liegen auf **verschiedenen Ebenen** und überschneiden sich nur beim Laden:

| Aufgabe | mistral.rs | MultiGPT-Runner |
|---|---|---|
| Modelle laden/entladen, Inferenz, OpenAI-API, Batching | ja (Kern) | nein |
| Sandbox-Container, feste Vorlage, Rechte, signierte Befehle, Arbeitsstände, Audit | nein | ja (Kern) |
| Wer darf was, Kontingent, Abrechnung, Chat-Versionen | nein | Server |

Daraus folgt: Der Lader (Modelle laden, entladen, bedienen) muss **nicht neu gebaut werden** – er
wäre mistral.rs (oder ein anderer) als Dienst-Container, vom Runner gestartet und überwacht.
Gebaut wird weiter die Schicht darüber: Richtlinie, Warteschlange und Rangfolge, VRAM-Anzeige,
Rechte. Ob mistral.rs auch **LM Studio** ersetzen könnte (ein Lader für Chat und Bild), ist eine
eigene Prüfung: Ersatz nur, wenn es Modelle automatisch nachlädt und im Leerlauf entlädt –
das ist in der Dokumentation nicht belegt und muss ausprobiert werden.

**Nächster Schritt (Versuch statt Entwurf):** mistral.rs auf dem GPU-Rechner in einem
Container mit FLUX.1 messen – VRAM-Bedarf, Zeit je Bild, Verhalten von `unload`/`reload`,
Anfrage an ein entladenes Modell, zwei gleichzeitige Anfragen, Zusammenspiel mit LM Studio auf
derselben GPU.

### Randbedingung: eine GPU mit 24 GB (geklärt 2026-10-18)

- **Exklusiv statt geteilt.** FLUX.1 hat einen Transformer mit etwa 12,5 Mrd. Parametern; in
  bf16 sind das rund 25 GB allein für die Gewichte (dazu T5-Textencoder, CLIP, VAE und
  Aktivierungen). Das passt **nicht** in 24 GB, erst recht nicht neben geladenen Chat-Modellen
  von LM Studio. Es gibt also immer nur **einen** GPU-Nutzer zur Zeit (eine Lease für die ganze
  Karte); die VRAM-Aufteilung zwischen Benutzern entfällt, übrig bleibt eine Warteschlange.
- **Folge für die Laufzeit:** FLUX läuft hier nur mit Verkleinerung – fp8 (z. B. optimum-quanto)
  oder GGUF (Q8 etwa halb so groß wie bf16) und/oder Offloading des Textencoders. diffusers
  und stable-diffusion.cpp bieten das; mistral.rs kann laut Dokumentation Diffusionsmodelle
  nicht quantisieren und käme nur über `FluxOffloaded` (langsamer) infrage. Das verschiebt die
  Gewichtung zugunsten diffusers oder stable-diffusion.cpp; der Messversuch bleibt nötig.
- **Wechsel zwischen Chat und Bild:** Ein Bildauftrag verdrängt die Chat-Modelle (Entladen in
  LM Studio, danach Laden von FLUX), und danach muss das Chat-Modell wieder geladen werden. Das
  kostet je Wechsel Sekunden bis Minuten. Die Warteschlange sollte deshalb **Aufträge
  bündeln** (mehrere Bilder nacheinander, bevor das Chat-Modell zurückkommt) und im Chat
  anzeigen, dass die GPU gerade für Bilder belegt ist. Die Rangfolge (Chat vor Bild oder
  umgekehrt) ist eine Richtlinie des Verwalters.

## Umsetzungsschritte

1. **Datenmodell:** `Runner.shared`, `RunnerShare`, Migration; Admin (Freigabe je Rolle/Konto).
2. **Auswahl und Rechte:** `_online_runner` auf eigene + freigegebene erweitern; `permissions`
   um die Freigabe ergänzen; MCP-Eintrag je Runner und Benutzer.
3. **Gateway:** Steuerbefehle (Kontingent, Rechte) je Benutzer statt je Token-Besitzer;
   `session_start` trägt den Benutzer; Tests mit zwei Benutzern auf einem Runner.
4. **Protokoll/Rust-Runner:** Benutzerkennung als Label; Abgleich nach Sitzungs-ID; Limits der
   Sitzung bleiben signiert vom Server. Protokollversion erhöhen (alte Runner werden mit
   4426 abgelehnt).
5. **Ressourcen:** Zuteilung der Vorschau-Ports, Warteschlange und Obergrenzen je Benutzer;
   GPU-Profil aus dem FLUX-Plan anbinden.
6. **Oberfläche:** Seite „Runner“ zeigt freigegebene Runner (Name, online, belegt, Warteschlange);
   Chat-Auswahl bei mehreren Runnern.
7. **Doku:** Wiki „Runner“ (Betrieb mit mehreren Benutzern, Sicherheit), Hinweis zu dev/prod.

## Abgrenzung zu „mehrere Umgebungen“

Das ist eine andere Achse: **ein Runner, mehrere Benutzer** desselben Servers (dieser Plan) –
im Gegensatz zu **ein Rechner, mehrere MultiGPT-Server** (dev/prod). Letzteres ist heute nur
mit getrennten Benutzerkonten am Rechner sauber (Wiki, Abschnitt „Mehrere Umgebungen“). Ein
Runner-Name im Label und im Unit-Namen würde beides vereinfachen und kann in Schritt 4
mitgenommen werden.

## Offene Fragen

- Wer darf freigeben: nur Verwalter oder auch der Betreiber selbst für ausgewählte Konten?
- Gemeinsamer Runner **und** persönliche Runner gleichzeitig pro Benutzer? (Vorschlag: ja,
  Auswahl im Chat.)
- Kontingent: Laufzeit zählt beim Benutzer (Vorschlag) – oder zusätzlich ein Topf für den Rechner?
- GPU: Nacheinander (einfach, sicher) oder VRAM-Aufteilung (mehr Durchsatz, braucht genaue
  Bedarfswerte)? Hängt von der Kartengröße und den Diensten ab. (Familienrechner: eine GPU, 24 GB.)
- LM Studio und Runner auf demselben Rechner: Hat Chat Vorrang, oder darf ein Bildauftrag
  Chat-Modelle verdrängen?
- Sollen Jugendliche den Familienrechner nutzen dürfen (Rolle ohne „Runner nutzen“ heute)?
  Eigenes Recht „Gemeinsamen Runner nutzen“ mit anderen Grenzen wäre denkbar.
- Arbeitsstände mit Ablage „lokal“ (Scratchpad auf dem Rechner): im Gemeinschaftsbetrieb
  verbieten oder je Benutzer getrennt erlauben?
