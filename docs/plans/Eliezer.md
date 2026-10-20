# Eliezer: Modellverwalter als Ersatz für LM Studio

Planungsstand: 2026-10-19. Entwurf. Eliezer ist ein eigenes Projekt (<https://github.com/Beerlesklopfer/eliezer>,
privat) mit eigener Roadmap (M1 bis M6 in dessen `docs/Implementierung.md`). Die Anbindung in
MultiGPT steht in `docs/Implementierung.md` als M16. Dieses Dokument hält Recherche und
Entscheidungen fest. Zielversion legt der Nutzer fest.

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

## Entscheidung 2026-10-19: Bildmodell FLUX.2 (eine Auswahl)

Im Chat gibt es **eine** Bildmodell-Auswahl, kein Diffusor-Menü. Gewählt: **FLUX.2**.

Recherche (Dokumentation und Drittquellen, nicht selbst gemessen):

- **mistral.rs kennt nur FLUX.1** (Beispiel `FLUX.1-schnell`, `Flux`/`FluxOffloaded`). Für FLUX.2
  fand sich kein Beleg. Lücke: Die FLUX.2-Architektur (Transformer, neuer Textencoder, neue
  Autoencoder) müsste in mistral.rs/candle **neu implementiert** werden, oder ein anderer Weg
  dient als Übergang (stable-diffusion.cpp bzw. diffusers als Dienst-Container).
- **Größen:** FLUX.2 [dev] 32 Mrd. Parameter (bf16 etwa 64 GB, fp8 etwa 32 GB, GGUF Q4 etwa
  19 GB) – auf 24 GB nur als GGUF, und mistral.rs quantisiert/lädt keine Diffusions-GGUF.
  [klein] 4B (destilliert, 4 Schritte) etwa 8–13 GB VRAM (Quellen widersprechen sich), 9B etwa 22 GB.
- **Lizenz:** klein 4B Apache-2.0; klein 9B und dev nur nicht-kommerziell (Zustimmung im HF-Konto
  des Tokens nötig).
- **Folge:** Für 24 GB und Familienbetrieb passt **FLUX.2 klein 4B** (Apache-2.0, 4 Schritte).
  Nachladen per HF-Token wie oben. Ob die Rust-Umsetzung vor oder nach einem Übergangsdienst
  kommt, entscheidet der Versuch.

### Vorhanden: `qwen-image-2.1-UC-Q4_K_M.gguf` (Hinweis des Nutzers, 2026-10-19)

Auf dem GPU-Rechner liegt eine Qwen-Image-Datei in GGUF (Q4_K_M). Einordnung:

- **Laufzeit:** stable-diffusion.cpp lädt Qwen-Image als GGUF (Unsloth-Anleitung, ByteShape-Dateien).
  Für mistral.rs ist GGUF bei Diffusionsmodellen nicht belegt. Damit ist stable-diffusion.cpp der
  Kandidat für den **Übergangsdienst** (Dienst-Container hinter dem Vertrag) mit dieser Datei.
- **Speicher:** Für Q4_K_M werden etwa 13–14 GB (VRAM plus RAM) genannt; die Quellen widersprechen
  sich. Mit `--offload-to-cpu`, `--diffusion-fa` und `--vae-tiling` ist auch weniger möglich.
- **Dazu nötig:** Beim GGUF liegt nur der Transformer vor; Textencoder (Qwen2.5-VL) und VAE kommen
  aus eigenen Dateien. Fehlen sie, lädt der Dienst sie mit dem HF-Token nach.
- **Offen:** Herkunft und Lizenz der Datei („2.1-UC“ ist mir nicht bekannt) prüft der Nutzer;
  FLUX.2 klein 4B bleibt die Rust-Linie, Qwen-Image ist bis dahin die Auswahl im Chat.

## Bestand an fertigen Orchestratoren (Recherche 2026-10-19, nach Einwand des Nutzers)

Der Bedarf ist erkannt und bedient. Es gibt Lösungen für „ein Rechner, eine GPU, Chat- **und**
Bildmodelle im Wechsel“ – nur nicht in Rust. Aus Dokumentation und Vergleichsseiten, nicht selbst erprobt:

| Projekt | Sprache/Art | Was es für unseren Fall leistet |
|---|---|---|
| **llama-swap** | Go, Proxy vor beliebigen OpenAI-kompatiblen Servern | Liest `model` der Anfrage, startet/stoppt den passenden Server; `ttl` je Modell (Leerlauf-Entladen), `groups` (was gleichzeitig laden darf); fertiges Container-Image mit llama-server, **stable-diffusion.cpp** und whisper.cpp; Bild-Endpunkte (`/v1/images/generations`). |
| **LocalAI** | Go, Backends (llama.cpp, stable-diffusion u. a.) | `--max-active-backends` mit **LRU-Verdrängung** (1 = immer nur ein Modell), Watchdog (Leerlauf 15 min, Busy 5 min), angeheftete Modelle, laufende Anfragen werden beim Verdrängen abgewartet. |
| **Lemonade** (AMD) | Orchestrator über llama.cpp, whisper.cpp, stable-diffusion.cpp, Kokoro | Wählt Backend je Hardware; läuft seit 10.7 auch auf NVIDIA (CUDA). |
| **GPUStack / Xinference** | Python, Cluster-Verwalter | Text, Bild (diffusers), Audio; für mehrere Rechner ausgelegt, für einen Familienrechner schwer. |
| Rust | – | Kein Projekt gefunden, das LLM **und** Diffusion unter einem VRAM-Plan verwaltet (mistral.rs ist Laufzeit, kein Verwalter). |

Folge für „alles Rust“: Der Verwalter selbst muss nicht neu erfunden werden. Zwei Wege:

1. **Fertigen Verwalter einsetzen** (llama-swap oder LocalAI) als Dienst-Container des Runners,
   mit mistral.rs und stable-diffusion.cpp dahinter. Schnell, erprobt; der Verwalter ist Go.
2. **Eigener Verwalter in Rust**, aber klein: Proxy mit `model`-Routing, TTL, Gruppen, VRAM-Buch,
   so wie llama-swap es vormacht. Mehr Aufwand, dafür ein Programm im Runner-Workspace.

MultiGPT behält in beiden Fällen die Richtlinie (Rechte, Rangfolge, Abrechnung) und den kleinen
Vertrag; die Wahl kann später gewechselt werden.

## Anforderung an Eliezer: Bilder bearbeiten (2026-10-20)

MultiGPT kann hochgeladene Bilder von einem Bildmodell verändern lassen (M9-02, Modus „Bild“ mit
angehängten Bildern). Bei Cloud-Anbietern läuft das über den OpenAI-Pfad; für Eliezer fehlt der
Endpunkt. Was MultiGPT sendet und erwartet:

- `POST /v1/images/edits`, `multipart/form-data` (wie OpenAI). Felder: `model` (Schlüssel), `prompt`,
  `image` (eine Datei) bzw. `image[]` (mehrere, höchstens 4), `n` (1), `size` (`BxH`), außerdem
  `quality`, `background`, `output_format`: werden ignoriert. Bilder PNG, JPEG oder WebP.
- Antwort wie bei `/v1/images/generations`: `{"created": …, "data": [{"b64_json": "…", "revised_prompt": …}]}`.
- Fehler wie dort (`400 invalid_request`, wenn das Modell keine Bearbeitung kann oder kein Bild
  mitkommt; Bildgröße über dem Limit → 413).
- Modellmeldung: `capabilities.image_edit = true` in `GET /api/v1/models` für Bildmodelle mit
  Bearbeitung. Der Admin von MultiGPT stellt „Bilder bearbeiten“ am Modell heute von Hand ein;
  mit der Meldung kann er es vorbelegen.
- Laufzeit: stable-diffusion.cpp bearbeitet mit Referenzbild (FLUX.2 klein kann das von Haus aus,
  Qwen-Image über eine Edit-Fassung des Modells); ob das mit den vorhandenen GGUF-Dateien läuft,
  prüft die Eliezer-Sitzung.
