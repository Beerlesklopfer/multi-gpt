# RAG einrichten (Betrieb)

Diese Seite beschreibt, was ein Verwalter für die Dokumentsuche einrichten muss: Embedding-Modell,
Texterkennung (OCR), das Beschreiben von Abbildungen, den Worker und die Fehlersuche. Was Nutzer mit Sammlungen tun, steht unter
[Fragen an eigene Dokumente](RAG), das Anbinden von NAS-Ordnern unter
[Verzeichnisquellen](RAG-Verzeichnisquellen).

> **Stand:** Alles auf dieser Seite ist umgesetzt: Worker, Suche, die Verwaltung im Admin und die
> komplett lokale Verarbeitung – Embeddings mit nomic-embed-text über LM Studio (768 Dimensionen,
> Präfixe `search_document: ` und `search_query: `), OCR mit olmOCR über LM Studio und Tesseract
> als Ersatz, die Auswahl der von LM Studio gemeldeten Modelle in den Einstellungen sowie die
> Knöpfe „Speichern und Embedding testen“ und „Speichern und OCR testen“. Ein OCR-Test gegen ein
> echtes LM Studio mit olmOCR war erfolgreich.
> Seit 0.3.2 lädt MultiGPT die Modelle in LM Studio auf Wunsch selbst und hält sich an ein
> Speicherbudget ([Modellverwaltung](#modellverwaltung-automatisch-laden-speicherbudget)); gegen
> ein echtes LM Studio ist nur das Lesen der Modellliste geprüft, Laden und Entladen nur mit
> nachgebildeter API.

**Platzhalter**, die du ersetzen musst, stehen in spitzen Klammern, z. B. `<schlüssel>`.

## Überblick

| Teil | Aufgabe |
|---|---|
| **Weboberfläche** (`multi-gpt.service`) | Upload, Sammlungen, Suche im Chat |
| **Worker** (`multi-gpt-worker.service`) | liest hochgeladene Dokumente, OCR, Zerteilung, Embeddings |
| **Embedding-Modell** | rechnet Text in Vektoren um, für Indexierung und jede Suchanfrage |
| **OCR** | liest gescannte PDF-Seiten ohne Textebene und Bilddateien: olmOCR über LM Studio (Tesseract auf Wunsch als Ersatz) oder Tesseract auf dem Server |
| **Abbildungen** (optional) | ein allgemeines Vision-Modell beschreibt Bilder und Diagramme in PDF und DOCX sowie Bilddateien; die Beschreibung wird mit durchsucht |
| **PostgreSQL mit pgvector** | speichert Abschnitte und Vektoren, sucht per HNSW-Index und Volltext |

Ohne eingerichtetes Embedding-Modell lassen sich Dokumente zwar hochladen, sie werden aber nicht
indexiert („Es ist kein Embedding-Modell eingerichtet …“), und die Seiten der Sammlungen zeigen
einen Hinweis.

## Admin-Abschnitt „Dokumente (RAG)“

Im Admin (nur Verwalter) gibt es einen eigenen Abschnitt **„Dokumente (RAG)“** mit:

- **RAG-Übersicht:** Konfiguration (Embedding-Modell, Anbieter, Vektordimension), Zahlen je
  Status, Speicherbedarf, Zustand des Workers und Warteschlange. Knöpfe „Embedding testen“,
  „OCR testen“, „Alles neu indexieren“, „Fehlgeschlagene erneut versuchen“ und „Hängende Aufträge
  zurücksetzen“.
- **Einstellungen:** Embedding-Modell, Zerteilung, Suche (siehe unten).
- **Sammlungen**, **Dokumente**, **Indexierungsaufträge:** Listen mit Aktionen wie „Neu
  indexieren“, „Erneut versuchen“ und „Abbrechen und löschen“.
- **Verzeichnisquellen** (siehe [Verzeichnisquellen](RAG-Verzeichnisquellen)).

Verwalter sehen dort nur Metadaten (Titel, Status, Größe, Fehlertext), nie Dokument- oder
Abschnittsinhalte.

## 1. Pakete für die Texterkennung

Das Debian-Paket `multi-gpt` hängt von `poppler-utils` (`pdftoppm`, rendert PDF-Seiten als
Bild) sowie `tesseract-ocr`, `tesseract-ocr-deu` und `tesseract-ocr-eng` ab; apt installiert sie
mit. In der Entwicklung von Hand:

```sh
sudo apt install poppler-utils tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng
```

Prüfen, welche Sprachen Tesseract kennt:

```sh
tesseract --list-langs
```

Die Sprachen der Texterkennung legt `OCR_LANGUAGES` fest (Grundeinstellung `deu+eng`). Für
weitere Sprachen das passende Paket `tesseract-ocr-<sprache>` installieren (Liste der
Sprachkürzel in der [Tesseract-Doku](https://tesseract-ocr.github.io/tessdoc/Data-Files-in-different-versions.html)),
dann in `/etc/multi-gpt/.env` zum Beispiel:

```sh
OCR_LANGUAGES=deu+eng+fra
```

und den Worker neu starten: `sudo systemctl restart multi-gpt-worker`.

## 2. Einstellungen in `/etc/multi-gpt/.env`

Alle Werte sind optional; ohne Eintrag gilt die Grundeinstellung.

Die vollständige Liste **aller** Schlüssel (Datenbank, Netz, Login-Sperre, gunicorn, Entwicklung) steht auf der Seite [Konfiguration](Konfiguration). Hier nur die für Dokumente:

| Variable | Grundeinstellung | Wirkung |
|---|---|---|
| `DOCUMENT_MAX_UPLOAD_MB` | `25` | Höchstgröße je hochgeladenem Dokument in MB |
| `OCR_LANGUAGES` | `deu+eng` | Sprachen für Tesseract, mit `+` verbunden |
| `JOB_MAX_ATTEMPTS` | `5` | Versuche je Indexierungsauftrag bei Fehlern, danach „Fehler“ |
| `RAG_SOURCE_ROOTS` | leer (aus) | erlaubte Wurzeln für [Verzeichnisquellen](RAG-Verzeichnisquellen) |
| `RAG_SOURCE_MAX_FILES` | `5000` | Höchstzahl Dateien je Einlesevorgang einer Verzeichnisquelle |

Die nginx-Site des Pakets setzt `client_max_body_size` automatisch auf `DOCUMENT_MAX_UPLOAD_MB` + 10 MB;
nach einer Änderung des Werts `sudo dpkg-reconfigure multi-gpt` ausführen (siehe [nginx und TLS](nginx-und-TLS)). Nach Änderungen an der `.env`:

```sh
sudo systemctl restart multi-gpt multi-gpt-worker
```

Die Vektordimension (768) ist fest im Code und keine Umgebungsvariable: Eine Änderung braucht eine
Migration der Datenbank und „Alles neu indexieren“.

## 3. LM Studio vorbereiten

Damit keine Dokumentinhalte das Haus verlassen, rechnet LM Studio im Heimnetz die Embeddings und
auf Wunsch die Texterkennung. LM Studio muss dafür in MultiGPT als Anbieter eingerichtet sein
(wie für lokale Chat-Modelle).

**Modelle:**

| Zweck | Modell | Hinweis |
|---|---|---|
| Embeddings | `text-embedding-nomic-embed-text-v1.5` (GGUF von `nomic-ai/nomic-embed-text-v1.5-GGUF`) | 768 Dimensionen, passt zur Datenbank |
| OCR | `allenai/olmocr-2-7b` (GGUF-Variante von `allenai/olmOCR-2-7B-1025`) | Vision-Modell, laut Modellseite ca. 5 GB Speicher |
| Abbildungen (optional) | `qwen/qwen3-vl-8b` (Qwen3-VL-8B, GGUF oder MLX) | allgemeines Vision-Modell, laut Modellseite ab 6 GB Speicher; Alternativen: `google/gemma-3-12b` (ab 11 GB) oder Qwen2.5-VL-7B-Instruct |

olmOCR eignet sich nur zum Lesen von Text, nicht zum Beschreiben von Abbildungen; dafür ein
allgemeines Vision-Modell nehmen. Empfohlen ist Qwen3-VL-8B: aktuell, in LM Studio als
`qwen/qwen3-vl-8b` mit Bild-Eingabe gelistet und für Diagramme mit Achsen und Beschriftungen
gut geeignet. Wer wenig Speicher hat, nimmt `qwen/qwen3-vl-4b`.

**Speicher:** Sind nomic-embed, olmOCR und das Vision-Modell gleichzeitig geladen, braucht LM
Studio zusammen grob 12 GB (mit Gemma 3 12B statt Qwen3-VL-8B rund 17 GB) Grafik- bzw.
Arbeitsspeicher, dazu Platz für den Kontext. Reicht der Speicher nicht, entweder ein kleineres
Vision-Modell wählen oder das Beschreiben der Abbildungen ausgeschaltet lassen.

### Modellverwaltung: automatisch laden, Speicherbudget

Von sich aus lädt LM Studio ein Modell erst bei der ersten Anfrage (JIT), entlädt es nach 60
Minuten ohne Nutzung wieder und verdrängt mit „Auto-Evict“ ein JIT-geladenes Modell, sobald ein
anderes kommt ([LM Studio: TTL und Auto-Evict](https://lmstudio.ai/docs/developer/core/ttl-and-auto-evict)).
Wechseln sich bei der Indexierung olmOCR und nomic-embed ab, während im Chat gpt-oss läuft,
lädt LM Studio ständig um, Anfragen warten oder scheitern mit „Modell nicht geladen“, und die
Modelle drängen sich im Grafikspeicher – an dem auch der Monitor hängt.

Deshalb kann MultiGPT die Modelle selbst verwalten (ab LM Studio 0.4, REST-API v1):

- **Vor jedem Gebrauch laden:** Chat, Vergleich, `ask` über die API, Embeddings (Indexierung
  und Suche), OCR und Abbildungen prüfen, ob das Modell geladen ist, und laden es sonst mit
  seinem **Lade-Profil** (Kontextfenster, Parallelität, **ohne TTL**). Im Chat steht solange
  „Lade openai/gpt-oss-20b …“ in der Statuszeile, beim Lauf im Fortschritt.
- **Nie über das Budget:** Am Anbieter stehen **Speicherbudget für Modelle (GB)** (was LM Studio
  an Grafikspeicher nutzen darf) und **Reserve für Anzeige bzw. System (GB)** (Standard 2 GB,
  bleibt immer frei). Passt ein Modell nicht in den freien Rest, entlädt MultiGPT zuerst andere –
  aber nur, wenn das genug Platz schafft. Sonst lädt es nichts: Der Chat zeigt „Zu wenig
  Grafikspeicher für …: belegt durch …“, ein Lauf wartet mit dieser Meldung (neuer Versuch
  alle 5 Minuten, wie bei ausgeschaltetem LM Studio).
- **Entladeregeln** (Reihenfolge: Priorität, dann am längsten nicht genutzt):
  - nie **angeheftete** Modelle (z. B. nomic-embed: klein und für die Suche immer nötig);
  - nie Modelle, die gerade eine Anfrage bearbeiten (Zähler je Modell in der Datenbank, für
    alle Prozesse; läuft nach 20 Minuten ab, falls ein Prozess abstürzt);
  - nie Modelle, die MultiGPT nicht kennt (von Hand in LM Studio geladen);
  - Indexierungsmodelle (Embedding, OCR, Abbildungen) weichen einem Chatmodell, auf das ein
    Mensch wartet – aber nicht, solange ein Lauf offen ist;
  - die Indexierung verdrängt nur Modelle mit niedrigerer **Priorität**, das Chatmodell bei
    gleicher Priorität also nie; im Chat weichen auch gleich hohe.
- **Leerlauf:** Der Worker entlädt jede Minute Modelle, die länger als **Entladen nach Leerlauf**
  (Standard 30 Minuten, 0 = nie) nicht genutzt wurden – nie angeheftete, belegte oder
  Indexierungsmodelle während eines Laufs. Die TTL von LM Studio bleibt ungenutzt.
- **Nach der Indexierung:** Modelle der Indexierung (Embedding, OCR, Abbildungen) werden nur für
  den Lauf geladen: Ist kein Lauf mehr offen, entlädt der Worker sie nach **2 Minuten** ohne
  Gebrauch (Schalter „nach der Indexierung entladen“, Standard an). So bleibt die Grafikkarte
  außerhalb der Läufe dem Chat. Die Suche im Chat lädt das Embedding-Modell bei Bedarf kurz
  nach (nomic: etwa 0,4 GB, wenige Sekunden). Wer das vermeiden will, heftet es an.
- **Gleichzeitig:** Laden und Entladen laufen nacheinander (Sperre in PostgreSQL). Wer währenddessen
  dasselbe Modell braucht, wartet und nutzt es dann.
- **Ladefehler:** Meldet LM Studio einen Fehler (z. B. zu wenig Speicher), wartet MultiGPT mit
  dem nächsten Versuch 1, 2, 4 … höchstens 30 Minuten; „Laden“ von Hand geht sofort.

**Einrichten:** Admin → Anbieter → LM Studio → Abschnitt „Modellverwaltung (LM Studio)“:
„Modelle verwalten“ an, Budget und Reserve eintragen. Ohne diesen Schalter ändert sich nichts.
In LM Studio die Einstellung „JIT models auto-evict“ ausschalten bzw. JIT-Laden ganz
abschalten, damit LM Studio nicht selbst nebenher lädt.

**Lade-Profil je Modell** (KI-Modell im Admin, Kontextfenster auch in der Fähigkeiten-Matrix,
in den RAG-Einstellungen und auf der Seite „LM Studio: geladene Modelle“):

| Feld | Standard | Bedeutung |
|---|---|---|
| Kontextfenster beim Laden | Vorschlag: Chat 32 768, OCR 8 192, Embedding Modellstandard | Stufen 2 048 … 131 072, nur bis zur Länge, die LM Studio für das Modell meldet |
| Parallele Anfragen | leer (LM Studio) | nur setzen, wenn nötig |
| angeheftet | aus | nie automatisch entladen |
| Priorität | 50 | 0–100, niedrigere gehen zuerst |
| automatisch laden | an | aus: nur von Hand laden, sonst Meldung |
| Entladen nach Leerlauf | 30 min | 0 = nie; gilt nicht für angeheftete |
| nach der Indexierung entladen | an | nur Indexierungsmodelle: 2 min nach dem Lauf statt nach dem Leerlauf |
| gemessener Speicherbedarf | leer | überschreibt die Schätzung (z. B. Wert aus `nvidia-smi`) |

**Erwarteter Speicher:** Neben jeder Auswahl „Kontextfenster“ steht live die Schätzung, das
Budget (frei bzw. belegt durch die anderen geladenen Modelle, Reserve) und – wenn es nicht
passt – eine Warnung („passt nicht: 3,2 GB zu viel – kleinere Stufe wählen oder anderes Modell
entladen“). Speichern geht trotzdem; das Laden hält sich an das Budget. Die Schätzung:

- **Modelldatei:** Größe laut LM Studio (bei Vision-Modellen mit Bild-Projektor).
- **Kontext-Cache (KV-Cache):** 2 × Schichten × KV-Köpfe × Kopfdimension × 2 Byte × Kontext
  ([NVIDIA: Mastering LLM Techniques](https://developer.nvidia.com/blog/mastering-llm-techniques-inference-optimization/)).
  LM Studio meldet die Architekturdaten nicht; für bekannte Modelle stehen sie in MultiGPT
  (aus deren `config.json`: gpt-oss-20b 24 Schichten, 8 KV-Köpfe, 64 Dimensionen; olmOCR-2-7B
  bzw. Qwen2.5-VL-7B 28/4/128; nomic-embed-text 12/12/64). gpt-oss nutzt in jeder zweiten
  Schicht ein Fenster von 128 Tokens; diese Schichten zählen nur mit Fenster plus Puffer.
  Unbekannte Modelle: konservative Näherung nach Größe (8 KV-Köpfe × 128, reichlich Schichten).
- **Zuschlag** für Rechenpuffer: 0,5 GB je Sprachmodell, 0,3 GB mehr mit Bild-Eingabe,
  0,25 GB bei Embedding-Modellen.

Einheit GB = 10⁹ Byte wie in LM Studio (eine Karte mit 16 GiB hat 17,2 GB; wer 16 einträgt,
lässt ungefähr 1 GB zusätzlich frei).

Typische Werte (Schätzung, nicht gemessen):

| Modell (Datei) | 8 k | 16 k | 32 k | 64 k |
|---|---|---|---|---|
| nomic-embed-text-v1.5 (0,08 GB, höchstens 2 048 Tokens) | – (bei 2 048: 0,4 GB) | – | – | – |
| olmocr-2-7b Q4_K_M (6,0 GB) | 7,3 GB | 7,8 GB | 8,7 GB | 10,6 GB |
| gpt-oss-20b MXFP4 (12,1 GB) | 12,9 GB | 13,1 GB | 13,5 GB | 14,3 GB |

**Empfehlung Kontextlängen:** gpt-oss-20b 32 768 (Werkzeuge brauchen Platz), olmOCR 8 192
(eine Seite plus 8 000 Tokens Antwort), nomic 2 048. Alle drei zusammen brauchen dann rund
21,2 GB; mit einer 24-GB-Karte und 2 GB Reserve passt das knapp (Budget 24, nutzbar 22). Bei
16 GB Grafikspeicher passen gpt-oss und olmOCR nicht gleichzeitig: Dann lädt die Indexierung
olmOCR erst, wenn der Chat ruht und gpt-oss nach Leerlauf entladen wurde, bzw. der Chat
verdrängt olmOCR, solange kein Lauf offen ist.

**Chat während der Indexierung:** Läuft ein großes Vision-Modell (olmOCR, Qwen3-VL), teilen sich
Chat und Indexierung die Grafikkarte; Antworten kommen dann spürbar langsamer. Wer das nicht will,
startet große Läufe abends (ein Zeitfenster „Indexierung nachts“ gibt es noch nicht).

**Anzeige:** RAG-Übersicht → „LM Studio: geladene Modelle“ zeigt wie `lms ps` Modell, Größe,
Kontext, Status (geladen, wird geladen, Ladefehler), angeheftet, zuletzt genutzt, dazu einen
Budgetbalken (belegt, frei, Reserve) und Knöpfe Laden, Entladen, Anheften. Modelle, die nie ins
Budget passen, sind rot markiert. Auf der Konsole:

```sh
sudo mgpt-ctl lms ps                      # geladene Modelle mit Schätzung
sudo mgpt-ctl lms budget                  # Budget und Lade-Profile
sudo mgpt-ctl lms load allenai/olmocr-2-7b --context 8192
sudo mgpt-ctl lms unload openai/gpt-oss-20b
```

#### Rückfallweg: Modelle von Hand dauerhaft laden

Ohne Modellverwaltung (oder mit LM Studio vor 0.4) die Indexierungsmodelle auf dem
LM-Studio-Rechner von Hand laden. Mit `lms load` **ohne `--ttl`** geladene Modelle bleiben
geladen, bis man sie entlädt:

```sh
# Vorhandene Modelle und ihre Schlüssel anzeigen
lms ls

# Fehlt ein Modell: herunterladen (fragt bei mehreren Treffern nach)
lms get nomic-embed-text-v1.5
lms get allenai/olmocr-2-7b
lms get qwen/qwen3-vl-8b          # nur für „Abbildungen beschreiben“

# Laden, ohne --ttl; <schlüssel> aus der Ausgabe von "lms ls" übernehmen
lms load <schlüssel-nomic-embed>
lms load <schlüssel-olmocr> --context-length 8192
lms load <schlüssel-qwen3-vl>     # nur für „Abbildungen beschreiben“

# Prüfen, was geladen ist
lms ps
```

Typische Schlüssel sind `text-embedding-nomic-embed-text-v1.5`, `allenai/olmocr-2-7b` und
`qwen/qwen3-vl-8b`. Nach einem Neustart des LM-Studio-Rechners mit `lms ps` prüfen und bei
Bedarf erneut laden.

**Ohne Bildschirm betreiben (headless):** Entweder den Dienst `llmster` installieren und mit
`lms daemon up` starten, oder in der Desktop-App in den Einstellungen den LLM-Server beim Anmelden
starten lassen und den Server mit `lms server start` starten
([LM Studio: Headless](https://lmstudio.ai/docs/app/api/headless),
[lms-Befehle](https://lmstudio.ai/docs/cli)).

Ist LM Studio aus, wartet die Indexierung (siehe [Fehlersuche](#fehlersuche)), und Antworten im
Chat entstehen mit einem Hinweis ohne Dokumentquellen.

## 4. RAG-Einstellungen im Admin

**Admin → „Dokumente (RAG)“ → „Einstellungen“.**

| Feld | Grundeinstellung | Bedeutung |
|---|---|---|
| Embedding-Modell | leer | genau ein Modell mit der Fähigkeit „Embedding“ |
| Präfix für Abschnitte / Präfix für Suchanfragen | leer | wird jedem Text beim Einbetten vorangestellt; bei nomic-embed laut Modellkarte `search_document: ` bzw. `search_query: ` (wird bei Wahl eines nomic-Modells vorbelegt) |
| OCR-Verfahren | Tesseract (auf dem Server) | oder „olmOCR (Vision-Modell, z. B. über LM Studio)“ |
| OCR-Modell | leer | Vision-Modell für olmOCR, Pflicht bei olmOCR |
| Kontextfenster (bei Embedding-, OCR- und Abbildungsmodell) | Vorschlag | Lade-Profil des Modells für die [Modellverwaltung](#modellverwaltung-automatisch-laden-speicherbudget), daneben der erwartete Speicher |
| Tesseract als Ersatz | an | ist das OCR-Modell nicht erreichbar, liest Tesseract die Seite |
| Abbildungen beschreiben | aus | Bilder in PDF/DOCX und Bilddateien von einem Vision-Modell beschreiben lassen |
| Modell für Abbildungen | leer | allgemeines Vision-Modell (z. B. `qwen/qwen3-vl-8b`), Pflicht, wenn eingeschaltet; nur OpenAI-kompatible Anbieter wie LM Studio |
| Abbildungen je Dokument / je Seite (höchstens) | 50 / 10 | weitere Abbildungen werden übersprungen |
| Mindestgröße (Pixel) | 150 | kürzere Kante; kleinere Bilder (Symbole, Logos) werden übersprungen |
| Bildgröße für das Modell (Pixel) | 1024 | längste Kante; größere Bilder werden vor dem Senden verkleinert |
| Abschnittsgröße (Tokens) | 800 | 100 bis 4000 |
| Überlappung (Tokens) | 100 | kleiner als die Abschnittsgröße |
| Treffer je Frage | 6 | so viele Abschnitte gehen als Quellen an das Modell (1 bis 20) |
| Volltextsuche dazunehmen | an | Vektor- und deutsche Volltextsuche zusammenführen (Reciprocal Rank Fusion) |

Die Auswahl „Embedding-Modell“ ist je aktivem Anbieter gruppiert. Sie
zeigt die schon angelegten Modelle und dazu die Modelle, die der Anbieter meldet, aber noch nicht
in MultiGPT angelegt sind, als „LM Studio · `<id>` (neu)“. Ein solches Modell wird beim
Speichern angelegt. Die OCR-Felder stehen im Abschnitt „Texterkennung (OCR)“. Ein nur für OCR
angelegtes Modell ist für den Chat inaktiv, wird für die Texterkennung aber trotzdem genutzt.

Die Felder für Abbildungen stehen im Abschnitt „Abbildungen“. Die Auswahl „Modell für
Abbildungen“ zeigt allgemeine Vision-Modelle unter „Empfohlen“; angeboten werden nur
OpenAI-kompatible Anbieter (LM Studio, OpenAI), bei Cloud-Anbietern nur Modelle mit
Bild-Eingabe. Ein dafür neu angelegtes Modell ist für den Chat inaktiv. **Kosten und
Datenschutz:** Je Abbildung entsteht ein Modellaufruf – lokal einige Sekunden je Bild, bei
Cloud-Anbietern Gebühren. Mit einem lokalen Modell bleiben die Bilder im Haus, mit einem
Cloud-Modell gehen sie an den Anbieter. Die RAG-Übersicht zeigt, ob die Beschreibung an ist,
welches Modell sie nutzt und ob es geladen ist; beim Dokument im Admin steht die Zahl der
beschriebenen Abbildungen.

**Prüfen:** Unten im Formular stehen die Knöpfe **„Speichern und Embedding testen“** und
**„Speichern und OCR testen“**. Sie speichern die angezeigten Werte und testen dann genau diese.
Der Embedding-Test bettet mit dem gewählten Modell einen kurzen Text ein und meldet die Dimension
oder die Ursache eines Fehlers (ohne Key). Der OCR-Test liest eine erzeugte Testseite mit
bekanntem Text mit dem gewählten Verfahren und zeigt den erkannten Text oder die Ursache. In der
RAG-Übersicht gibt es dieselben Tests als „Embedding testen“ und „OCR testen“ für die
gespeicherten Einstellungen.

## 5. Nach einem Modellwechsel: „Alles neu indexieren“

Vektoren verschiedener Modelle passen nicht zueinander. Nach einem Wechsel des Embedding-Modells,
der Präfixe oder der Zerteilung deshalb alle Dokumente neu indexieren. Dasselbe gilt, wenn das
Beschreiben der Abbildungen ein- oder ausgeschaltet oder sein Modell gewechselt wird – sonst
gilt die Änderung nur für neu hochgeladene Dokumente. MultiGPT erinnert nach dem
Speichern mit einem Hinweis daran.

- Im Admin: **„Dokumente (RAG)“ → „RAG-Übersicht“ → „Alles neu indexieren“** und bestätigen.
- Auf der Kommandozeile (Debian-Paket):

  ```sh
  sudo mgpt-ctl reindex
  ```

  Nur bestimmte Sammlungen oder Dokumente (`<id>` aus dem Admin, mehrfach möglich) oder nur
  Dokumente mit Fehler:

  ```sh
  sudo mgpt-ctl reindex --collection <id>
  sudo mgpt-ctl reindex --document <id>
  sudo mgpt-ctl reindex --errors-only
  ```

- In der Entwicklung: `make reindex`.

Bis ein Dokument fertig ist, bleiben seine alten Abschnitte durchsuchbar. Die Arbeit macht der
Worker.

Beim Update von einer älteren Version auf 768 Dimensionen verwirft die Migration alle
vorhandenen Abschnitte und reiht alle Dokumente neu ein. Danach muss das Embedding-Modell
eingerichtet und der Worker gelaufen sein, bevor die Suche wieder Treffer liefert.

## 6. Der Worker

Der Worker holt Aufträge aus einer Tabelle in PostgreSQL (`SELECT … FOR UPDATE SKIP LOCKED`, so
können mehrere Worker parallel laufen), indexiert die Dokumente und wiederholt vorübergehende
Fehler.

**Debian-Paket:** Das Paket bringt die Unit `multi-gpt-worker.service` mit. Sie wird bei der
Installation aktiviert und gestartet und bei Updates nach der Migration neu gestartet. Sie läuft
als Nutzer `multi-gpt` mit `/etc/multi-gpt/.env` und niedriger Priorität (`Nice=10`, IO
`idle`), damit die Weboberfläche Vorrang hat.

```sh
sudo systemctl status multi-gpt-worker
sudo journalctl -u multi-gpt-worker -f
sudo systemctl restart multi-gpt-worker
```

Beim Beenden unterbricht der Worker den laufenden Auftrag an der nächsten Prüfstelle (zwischen
PDF-Seiten oder Embedding-Paketen) und reiht ihn ohne Zählung des Versuchs neu ein.

**Entwicklung:** Den Worker im Vordergrund starten, Strg+C beendet ihn:

```sh
make worker
```

**Docker:** `compose.yaml` enthält einen eigenen Dienst `worker`.

**Wiederholungen:**

- Vorübergehende Fehler werden mit wachsendem Abstand wiederholt (30 s, 2 min, 8 min, 32 min,
  höchstens 1 h), bis `JOB_MAX_ATTEMPTS` (5) erreicht ist; danach steht das Dokument auf
  „Fehler“.
- Dauerhafte Fehler (Datei unlesbar, kein Text, falscher Typ) setzen das Dokument sofort auf
  „Fehler“.
- Ein Auftrag, dessen Worker kein Lebenszeichen mehr gibt (Absturz, `kill -9`), wird nach
  10 Minuten automatisch neu eingereiht.
- Ist der Anbieter (LM Studio) nicht erreichbar, bleibt das Dokument auf
  „wartet“ mit dem Text „Wartet: … Neuer Versuch ab hh:mm Uhr.“ Der Worker versucht es alle
  5 Minuten erneut, ohne Obergrenze und ohne den Versuch zu zählen.

## Fehlersuche

### Warnung „Worker läuft nicht?“

Die RAG-Übersicht zeigt diese Warnung, wenn Aufträge seit über 2 Minuten fällig sind, aber kein
Worker arbeitet, oder wenn ein Auftrag seit über 10 Minuten kein Lebenszeichen gibt.

```sh
sudo systemctl status multi-gpt-worker
sudo journalctl -u multi-gpt-worker -n 100
sudo systemctl enable --now multi-gpt-worker
```

Hängende Aufträge lassen sich in der RAG-Übersicht mit „Hängende Aufträge zurücksetzen“ sofort
neu einreihen.

### Dokumente bleiben auf „wartet“

- Läuft der Worker? Siehe oben.
- Steht beim Dokument „Versuch n fehlgeschlagen: …“, wird automatisch wiederholt. Die Ursache
  steht im Text und unter „Indexierungsaufträge“ in der Spalte „letzter Fehler“.
- **LM Studio offline:** Text „Wartet: Der Anbieter … ist nicht erreichbar.“
  LM-Studio-Rechner einschalten, mit `lms ps` prüfen, ob die Modelle geladen sind; der nächste
  Versuch folgt nach höchstens 5 Minuten. Ist für OCR „Tesseract als Ersatz“ an, liest Tesseract
  die Seiten des Dokuments statt olmOCR.

### Dokument steht auf „Fehler“

Den Fehlertext beim Dokument lesen (Admin → „Dokumente“ oder Seite der Sammlung). Ist die Ursache
behoben, im Admin bei den Dokumenten die Aktion **„Erneut versuchen (nur mit Fehler)“** wählen,
in der RAG-Übersicht **„Fehlgeschlagene erneut versuchen“** oder:

```sh
sudo mgpt-ctl reindex --errors-only
```

| Meldung | Ursache und Abhilfe |
|---|---|
| „Es ist kein Embedding-Modell eingerichtet. …“ | In den Einstellungen ein Embedding-Modell wählen, dann erneut versuchen. |
| „Das Embedding-Modell oder sein Anbieter ist deaktiviert.“ | Modell und Anbieter im Admin aktivieren. |
| „Das Embedding-Modell liefert 1536 statt 768 Dimensionen – das passt nicht zur Datenbank. …“ | Das Modell passt nicht zur festen Dimension der Datenbank. Ein Modell mit 768 Dimensionen wählen (z. B. `text-embedding-nomic-embed-text-v1.5` über LM Studio), dann „Alles neu indexieren“. |
| „Dieser Anbietertyp unterstützt keine Embeddings.“ | Anderen Anbieter wählen. |
| „Im Dokument wurde kein Text gefunden.“ | Leere Datei oder ein Scan, den auch die OCR nicht lesen konnte. OCR prüfen (siehe unten). |
| „Die Datei des Dokuments fehlt auf dem Server.“ | Datei unter `MEDIA_ROOT` gelöscht oder nicht lesbar. Dokument löschen und neu hochladen. |
| „Das Dokument ist zu umfangreich für die Verarbeitung.“ | Datei aufteilen. |
| „Das PDF enthält keine Textebene (gescannt), und die Texterkennung (OCR) ist nicht installiert …“ | Pakete aus Schritt 1 installieren, Worker neu starten, dann erneut versuchen. |

### OCR fehlt oder liest nichts

- Fehlen `tesseract` oder `pdftoppm`, steht im Log des Workers „OCR nicht verfügbar“. Hat das
  PDF gar keine Textebene, endet die Indexierung mit „Das PDF enthält keine Textebene
  (gescannt), und die Texterkennung (OCR) ist nicht installiert …“; gemischte PDFs werden ohne
  die gescannten Seiten indexiert. Pakete aus Schritt 1 installieren, Worker neu starten und die
  Dokumente neu indexieren.
- Falsche Sprache: `tesseract --list-langs` prüfen und `OCR_LANGUAGES` anpassen.
- Bei olmOCR: „Speichern und OCR testen“ in den Einstellungen nutzen, mit `lms ps` prüfen, ob
  `allenai/olmocr-2-7b` geladen ist.

### Abbildungen fehlen oder Dokumente warten

- Abbildungen werden nur beschrieben, wenn „Abbildungen beschreiben“ an ist, und erst für
  Dokumente, die danach (neu) indexiert werden. Übersprungen werden kleine Bilder, schmale
  Linien, Wiederholungen (z. B. ein Logo auf jeder Seite), Bilder über den Obergrenzen und
  gescannte Seiten (deren Text liest die OCR). Bilder, die pypdf nicht dekodieren kann (z. B.
  JBIG2), entfallen ebenfalls.
- Ist das Modell für Abbildungen nicht erreichbar, wartet die Indexierung wie bei der OCR
  („Wartet: … Neuer Versuch ab …“). Mit `lms ps` bzw. `mgpt-ctl lms ps` prüfen, ob es geladen
  ist. Mit Modellverwaltung kann der Grund auch „Zu wenig Grafikspeicher für …“ sein: Budget,
  Kontextfenster bzw. angeheftete Modelle prüfen (RAG-Übersicht → „LM Studio: geladene Modelle“).
- Lehnt das Modell ein einzelnes Bild ab (z. B. weil es keine Bilder versteht), entfällt nur
  diese Abbildung; im Log des Workers steht „Abbildung … abgelehnt“ mit Dokument-ID und Nummer.

### Suche im Chat liefert nichts

- Hinweis „Dokumentsuche fehlgeschlagen: … Die Antwort entsteht ohne Dokumentquellen.“: Ursache
  steht im Text, häufig ist das Embedding-Modell nicht erreichbar (LM Studio aus) oder nicht
  eingerichtet. „Embedding testen“ hilft.
- „In den gewählten Sammlungen wurde nichts Passendes gefunden.“: Sind die Dokumente
  „indexiert“? Nach einem Modellwechsel wurde vielleicht nicht neu indexiert.

## Quellen

- LM Studio: [lms-Befehle](https://lmstudio.ai/docs/cli),
  [`lms load`](https://lmstudio.ai/docs/cli/local-models/load),
  [`lms ps`](https://lmstudio.ai/docs/cli/local-models/ps),
  [`lms get`](https://lmstudio.ai/docs/cli/local-models/get),
  [TTL und Auto-Evict](https://lmstudio.ai/docs/developer/core/ttl-and-auto-evict),
  REST-API v1 ab 0.4: [Übersicht](https://lmstudio.ai/docs/developer/rest),
  [Modelle auflisten](https://lmstudio.ai/docs/developer/rest/list),
  [laden](https://lmstudio.ai/docs/developer/rest/load),
  [entladen](https://lmstudio.ai/docs/developer/rest/unload),
  [Änderungen](https://lmstudio.ai/docs/developer/api-changelog)
- Speicherschätzung: [NVIDIA: KV-Cache je Token](https://developer.nvidia.com/blog/mastering-llm-techniques-inference-optimization/),
  Architekturdaten aus `config.json` von
  [gpt-oss-20b](https://huggingface.co/openai/gpt-oss-20b/blob/main/config.json),
  [olmOCR-2-7B-1025](https://huggingface.co/allenai/olmOCR-2-7B-1025/blob/main/config.json),
  [nomic-embed-text-v1.5](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5/blob/main/config.json)
  [Headless](https://lmstudio.ai/docs/app/api/headless)
- olmOCR: [Modellseite in LM Studio](https://lmstudio.ai/models/allenai/olmocr-2-7b),
  [Projekt olmOCR](https://github.com/allenai/olmocr)
- Abbildungen: [Qwen3-VL-8B in LM Studio](https://lmstudio.ai/models/qwen/qwen3-vl-8b),
  [Gemma 3 12B in LM Studio](https://lmstudio.ai/models/google/gemma-3-12b),
  [Qwen3-VL](https://github.com/QwenLM/Qwen3-VL)
- nomic-embed-text: [Modellkarte (Präfixe)](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5)
- Tesseract: [Doku](https://tesseract-ocr.github.io/tessdoc/),
  [Sprachdaten](https://tesseract-ocr.github.io/tessdoc/Data-Files-in-different-versions.html)
