# RAG einrichten (Betrieb)

Diese Seite beschreibt, was ein Verwalter für die Dokumentsuche einrichten muss: Embedding-Modell,
Texterkennung (OCR), den Worker und die Fehlersuche. Was Nutzer mit Sammlungen tun, steht unter
[Fragen an eigene Dokumente](RAG), das Anbinden von NAS-Ordnern unter
[Verzeichnisquellen](RAG-Verzeichnisquellen).

> **Stand:** Alles auf dieser Seite ist umgesetzt: Worker, Suche, die Verwaltung im Admin und die
> komplett lokale Verarbeitung – Embeddings mit nomic-embed-text über LM Studio (768 Dimensionen,
> Präfixe `search_document: ` und `search_query: `), OCR mit olmOCR über LM Studio und Tesseract
> als Ersatz, die Auswahl der von LM Studio gemeldeten Modelle in den Einstellungen sowie die
> Knöpfe „Speichern und Embedding testen“ und „Speichern und OCR testen“. Ein OCR-Test gegen ein
> echtes LM Studio mit olmOCR war erfolgreich.

**Platzhalter**, die du ersetzen musst, stehen in spitzen Klammern, z. B. `<schlüssel>`.

## Überblick

| Teil | Aufgabe |
|---|---|
| **Weboberfläche** (`multi-gpt.service`) | Upload, Sammlungen, Suche im Chat |
| **Worker** (`multi-gpt-worker.service`) | liest hochgeladene Dokumente, OCR, Zerteilung, Embeddings |
| **Embedding-Modell** | rechnet Text in Vektoren um, für Indexierung und jede Suchanfrage |
| **OCR** | liest gescannte PDF-Seiten ohne Textebene: olmOCR über LM Studio (Tesseract auf Wunsch als Ersatz) oder Tesseract auf dem Server |
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

| Variable | Grundeinstellung | Wirkung |
|---|---|---|
| `DOCUMENT_MAX_UPLOAD_MB` | `25` | Höchstgröße je hochgeladenem Dokument in MB |
| `OCR_LANGUAGES` | `deu+eng` | Sprachen für Tesseract, mit `+` verbunden |
| `JOB_MAX_ATTEMPTS` | `5` | Versuche je Indexierungsauftrag bei Fehlern, danach „Fehler“ |
| `RAG_SOURCE_ROOTS` | leer (aus) | erlaubte Wurzeln für [Verzeichnisquellen](RAG-Verzeichnisquellen) |
| `RAG_SOURCE_MAX_FILES` | `5000` | Höchstzahl Dateien je Einlesevorgang einer Verzeichnisquelle |

Steht nginx vor MultiGPT, muss `client_max_body_size` dort mindestens so groß sein wie
`DOCUMENT_MAX_UPLOAD_MB`, sonst lehnt nginx große Uploads ab. Nach Änderungen an der `.env`:

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

**Modelle dauerhaft laden.** In LM Studio werden Modelle, die erst bei der ersten Anfrage
geladen werden (JIT), standardmäßig nach 60 Minuten ohne Nutzung wieder entladen, und „Auto-Evict“
entlädt ein JIT-geladenes Modell, sobald ein anderes per JIT geladen wird. Modelle, die mit
`lms load` **ohne `--ttl`** geladen werden, bleiben dagegen geladen, bis man sie entlädt
([LM Studio: TTL und Auto-Evict](https://lmstudio.ai/docs/app/api/ttl-and-auto-evict)). Beide
Modelle deshalb auf dem LM-Studio-Rechner von Hand laden:

```sh
# Vorhandene Modelle und ihre Schlüssel anzeigen
lms ls

# Fehlt ein Modell: herunterladen (fragt bei mehreren Treffern nach)
lms get nomic-embed-text-v1.5
lms get allenai/olmocr-2-7b

# Laden, ohne --ttl; <schlüssel> aus der Ausgabe von "lms ls" übernehmen
lms load <schlüssel-nomic-embed>
lms load <schlüssel-olmocr>

# Prüfen, was geladen ist
lms ps
```

Typische Schlüssel sind `text-embedding-nomic-embed-text-v1.5` und `allenai/olmocr-2-7b`. Nach
einem Neustart des LM-Studio-Rechners mit `lms ps` prüfen und bei Bedarf erneut laden.

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
| Tesseract als Ersatz | an | ist das OCR-Modell nicht erreichbar, liest Tesseract die Seite |
| Abschnittsgröße (Tokens) | 800 | 100 bis 4000 |
| Überlappung (Tokens) | 100 | kleiner als die Abschnittsgröße |
| Treffer je Frage | 6 | so viele Abschnitte gehen als Quellen an das Modell (1 bis 20) |
| Volltextsuche dazunehmen | an | Vektor- und deutsche Volltextsuche zusammenführen (Reciprocal Rank Fusion) |

Die Auswahl „Embedding-Modell“ ist je aktivem Anbieter gruppiert. Sie
zeigt die schon angelegten Modelle und dazu die Modelle, die der Anbieter meldet, aber noch nicht
in MultiGPT angelegt sind, als „LM Studio · `<id>` (neu)“. Ein solches Modell wird beim
Speichern angelegt. Die OCR-Felder stehen im Abschnitt „Texterkennung (OCR)“. Ein nur für OCR
angelegtes Modell ist für den Chat inaktiv, wird für die Texterkennung aber trotzdem genutzt.

**Prüfen:** Unten im Formular stehen die Knöpfe **„Speichern und Embedding testen“** und
**„Speichern und OCR testen“**. Sie speichern die angezeigten Werte und testen dann genau diese.
Der Embedding-Test bettet mit dem gewählten Modell einen kurzen Text ein und meldet die Dimension
oder die Ursache eines Fehlers (ohne Key). Der OCR-Test liest eine erzeugte Testseite mit
bekanntem Text mit dem gewählten Verfahren und zeigt den erkannten Text oder die Ursache. In der
RAG-Übersicht gibt es dieselben Tests als „Embedding testen“ und „OCR testen“ für die
gespeicherten Einstellungen.

## 5. Nach einem Modellwechsel: „Alles neu indexieren“

Vektoren verschiedener Modelle passen nicht zueinander. Nach einem Wechsel des Embedding-Modells,
der Präfixe oder der Zerteilung deshalb alle Dokumente neu indexieren. MultiGPT erinnert nach dem
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
  [TTL und Auto-Evict](https://lmstudio.ai/docs/app/api/ttl-and-auto-evict),
  [Headless](https://lmstudio.ai/docs/app/api/headless)
- olmOCR: [Modellseite in LM Studio](https://lmstudio.ai/models/allenai/olmocr-2-7b),
  [Projekt olmOCR](https://github.com/allenai/olmocr)
- nomic-embed-text: [Modellkarte (Präfixe)](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5)
- Tesseract: [Doku](https://tesseract-ocr.github.io/tessdoc/),
  [Sprachdaten](https://tesseract-ocr.github.io/tessdoc/Data-Files-in-different-versions.html)
