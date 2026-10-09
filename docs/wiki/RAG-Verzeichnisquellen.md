# Verzeichnisquellen: NAS-Ordner als Sammlung

Eine **Verzeichnisquelle** liest einen Ordner auf dem Server oder einer eingehängten NAS-Freigabe
regelmäßig in eine Sammlung ein. Neue und geänderte Dateien werden indexiert, gelöschte
verschwinden aus der Sammlung. Die Dateien bleiben am Ort, MultiGPT liest sie nur und kopiert sie
nicht. Allgemeines zu Sammlungen steht unter [Fragen an eigene Dokumente](RAG), die Einrichtung
von Worker und Embedding-Modell unter [RAG einrichten](RAG-Einrichtung).

Verzeichnisquellen legt nur ein **Verwalter** im Admin an. Voraussetzung ist ein laufender
Worker (`multi-gpt-worker.service`) und ein eingerichtetes Embedding-Modell.

**Platzhalter**, die du ersetzen musst:

| Platzhalter | Bedeutung | Beispiel |
|---|---|---|
| `<wurzel>` | erlaubte Wurzel, unter der Verzeichnisquellen liegen dürfen | `/srv/nas/dokumente` |
| `<ordner>` | der Ordner, der eingelesen werden soll (unterhalb einer Wurzel) | `/srv/nas/dokumente/Verträge` |
| `<gruppe>` | Unix-Gruppe mit Leserechten auf die Dateien | `dokumente` |

## 1. Erlaubte Wurzeln festlegen: `RAG_SOURCE_ROOTS`

Aus Sicherheitsgründen darf ein Verwalter im Admin nur Ordner unterhalb der Wurzeln angeben, die
in der Konfiguration stehen. Ohne Eintrag ist die Funktion aus.

In `/etc/multi-gpt/.env` (Komma-Liste absoluter Pfade; relative Einträge werden ignoriert):

```sh
RAG_SOURCE_ROOTS=/srv/nas/dokumente,/srv/nas/archiv
```

Optional die Höchstzahl Dateien je Einlesevorgang (Grundeinstellung 5000; der Rest folgt im
nächsten Lauf):

```sh
RAG_SOURCE_MAX_FILES=5000
```

Danach beide Dienste neu starten:

```sh
sudo systemctl restart multi-gpt multi-gpt-worker
```

MultiGPT löst Pfade mit `realpath` auf und prüft bei der Anlage, bei jedem Lauf, für jede Datei
und bei jedem Download, ob sie noch unter einer Wurzel liegen.

## 2. Leserechte für den Nutzer `multi-gpt`

Beide Dienste laufen als Systemnutzer `multi-gpt`. Er braucht **Lesen und Betreten** (`r-x`) auf
alle Ordner bis hinunter zum eingelesenen Ordner und **Lesen** (`r`) auf die Dateien. Am
einfachsten über eine Gruppe:

```sh
# Gruppe anlegen (falls noch nicht vorhanden) und multi-gpt aufnehmen
sudo groupadd <gruppe>
sudo usermod -aG <gruppe> multi-gpt

# Ordner der Gruppe zuordnen und Gruppenrechte setzen (X = nur Ordner ausführbar)
sudo chgrp -R <gruppe> <ordner>
sudo chmod -R g+rX <ordner>
```

Die neue Gruppenzugehörigkeit gilt erst nach einem Neustart der Dienste:

```sh
sudo systemctl restart multi-gpt multi-gpt-worker
```

Prüfen, ob jeder Ordner auf dem Weg betretbar ist und ob `multi-gpt` lesen kann:

```sh
namei -l <ordner>
sudo -u multi-gpt ls <ordner>
```

`sudo -u` prüft nur die Dateirechte, nicht die Abschottung durch systemd (Schritt 3). Bei einer per
CIFS/SMB eingehängten NAS-Freigabe bestimmen die Mount-Optionen (`uid`, `gid`, `file_mode`,
`dir_mode`, siehe [mount.cifs(8)](https://manpages.debian.org/trixie/cifs-utils/mount.cifs.8.en.html)) die
Rechte, nicht `chmod`.

Fehlt die Lesbarkeit, meldet der Admin das beim Anlegen der Quelle.

## 3. Pfade unter `/home`: systemd-Drop-in

Die Units des Pakets schotten die Dienste ab: Wegen `ProtectSystem=strict` ist außer
`/var/lib/multi-gpt` alles schreibgeschützt; Pfade unter `/srv`, `/mnt` und `/media` sind lesbar.
Pfade unter `/home`, `/root` und `/run/user` sind wegen `ProtectHome=yes` **unsichtbar**.

Liegt ein Ordner unter `/home`, braucht es ein Drop-in für **beide** Units: Die Weboberfläche
prüft den Pfad beim Anlegen und beim Download, der Worker liest die Dateien.

**Variante A:** `/home` nur lesend einblenden.

```sh
sudo systemctl edit multi-gpt
```

Im Editor eintragen:

```ini
[Service]
ProtectHome=read-only
```

Dasselbe für den Worker:

```sh
sudo systemctl edit multi-gpt-worker
```

**Variante B (enger):** Nur den einen Ordner einblenden, der Rest von `/home` bleibt leer. Im
Editor beider Units:

```ini
[Service]
ProtectHome=tmpfs
BindReadOnlyPaths=/home/<nutzer>/<ordner>
```

Für Pfade außerhalb von `/home`, etwa einen NAS-Mount, kann man den Schreibschutz zusätzlich
ausdrücklich setzen (optional, `ProtectSystem=strict` schützt ohnehin):

```ini
[Service]
ReadOnlyPaths=<wurzel>
```

Danach neu starten (`systemctl edit` lädt die Konfiguration selbst neu):

```sh
sudo systemctl restart multi-gpt multi-gpt-worker
```

Die Optionen sind in [systemd.exec(5)](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)
beschrieben. Das Drop-in liegt unter `/etc/systemd/system/<unit>.service.d/override.conf` und
bleibt bei Updates des Pakets erhalten. Rechte auf Home-Verzeichnisse beachten (Schritt 2): Auch
`/home/<nutzer>` selbst muss für die Gruppe betretbar sein.

## 4. Quelle im Admin anlegen

**Admin → „Dokumente (RAG)“ → „Verzeichnisquellen“ → hinzufügen.**

| Feld | Grundeinstellung | Bedeutung |
|---|---|---|
| Sammlung | – | eine vorhandene Sammlung **oder** „Neue Sammlung: Name“ und „Neue Sammlung: Besitzer“ |
| Verzeichnis | – | absoluter Pfad unter einer Wurzel aus `RAG_SOURCE_ROOTS`; wird als `realpath` gespeichert |
| Unterordner einbeziehen | an | auch Dateien in Unterordnern einlesen |
| Dateimuster | `*.pdf, *.docx, *.txt, *.md` | kommagetrennt; verarbeitet werden nur PDF, DOCX, TXT und MD |
| Ausschlussmuster | `.*, ~$*, *.tmp, *.part` | kommagetrennt; gilt für Datei- und Ordnernamen und relative Pfade, z. B. `Entwürfe/*` |
| Intervall (Minuten) | 60 | wie oft eingelesen wird, mindestens 5 |
| aktiv | an | pausierte Quellen werden nicht eingelesen |

Verzeichnis und Sammlung lassen sich nach der Anlage nicht mehr ändern; für einen anderen Ordner
eine neue Quelle anlegen. Nach dem Anlegen liest MultiGPT den Ordner sofort ein.

Die Besitzerin oder der Besitzer der Sammlung kann sie wie jede andere Sammlung mit Gruppen
teilen (siehe [Fragen an eigene Dokumente](RAG#sammlungen)).

**Liste:** Sammlung, Besitzer, Pfad, Status (aktiv, pausiert, wird eingelesen, Fehler),
Intervall, letzter Lauf, Ergebnis (neu, geändert, unverändert, entfernt, übersprungen, Fehler),
letzter Fehler und Zahl der Dokumente. Die RAG-Übersicht zeigt die Quellen ebenfalls.

**Aktionen:**

- **Jetzt einlesen:** sofort einen Lauf einreihen.
- **Pausieren** / **Aktivieren:** periodisches Einlesen aus- bzw. einschalten.
- **Löschen:** entfernt die Quelle und ihre Dokumente samt Abschnitten aus MultiGPT. Die Dateien
  auf dem Server bleiben unberührt.

## 5. Wie der Abgleich arbeitet

- Der Worker prüft jede Minute, welche Quellen fällig sind, und reiht einen Auftrag „Verzeichnis
  einlesen“ ein, höchstens einen offenen je Quelle. Einen eigenen Timer gibt es nicht.
- Für jede Datei vergleicht er Pfad, Änderungszeit und Größe mit dem letzten Stand. Weicht etwas
  ab, berechnet er die SHA-256-Prüfsumme und prüft den Dateityp am Inhalt wie beim Upload.
- **Neu oder geändert:** Dokument anlegen bzw. aktualisieren und zur Indexierung einreihen.
- **Gelöscht:** Dokument und Abschnitte werden entfernt.
- Fehler einzelner Dateien werden gezählt, der Lauf geht weiter.

**Übersprungen werden:**

- symbolische Links (Dateien und Ordner) – sie werden nie verfolgt;
- versteckte Dateien und Ordner (Name beginnt mit `.`), immer;
- Dateien über `DOCUMENT_MAX_UPLOAD_MB`, leere Dateien und Dateien mit falschem oder nicht
  unterstütztem Typ;
- Dateien, die nicht zu den Dateimustern passen oder unter ein Ausschlussmuster fallen.

**Schutz vor Datenverlust:** Ist der Ordner ganz leer (zum Beispiel weil die NAS-Freigabe gerade
nicht eingehängt ist) oder enthält er mehr als `RAG_SOURCE_MAX_FILES` Dateien, löscht der Lauf
**nichts**.

## 6. Was Nutzer sehen

- Die Seite der Sammlung zeigt „Wird aus einem Serververzeichnis eingelesen“.
- Dokumente aus der Quelle haben keinen Löschknopf und lassen sich nicht einzeln löschen; eine
  Sammlung mit Quelle kann ihr Besitzer nicht löschen.
- Herunterladen funktioniert; MultiGPT liest die Datei dabei vom Server, mit Prüfung der Rechte
  und der Wurzel.
- Suche, Quellen und Seitenzahlen funktionieren wie bei hochgeladenen Dokumenten.

## Grenzen

- Nur PDF, DOCX, TXT und MD, höchstens `DOCUMENT_MAX_UPLOAD_MB` je Datei.
- Höchstens `RAG_SOURCE_MAX_FILES` Dateien je Lauf.
- Intervall mindestens 5 Minuten; Änderungen erscheinen also nicht sofort (außer mit „Jetzt
  einlesen“).
- Keine symbolischen Links, keine versteckten Dateien.
- MultiGPT schreibt nie in den Ordner.
- Die Indexierung großer Ordner dauert, besonders mit OCR; der Worker arbeitet die Dokumente
  nacheinander ab.

## Fehlersuche

| Problem | Abhilfe |
|---|---|
| Admin lehnt den Pfad ab | Liegt er unter einer Wurzel aus `RAG_SOURCE_ROOTS`? Beide Dienste nach Änderung der `.env` neu gestartet? Zeigt ein Symlink aus der Wurzel hinaus? |
| Ordner „nicht lesbar“ | Rechte prüfen (Schritt 2), unter `/home` das Drop-in (Schritt 3) für beide Units. |
| Status „Fehler“ | Spalte „letzter Fehler“ der Quelle lesen; Fehler einzelner Dateien stehen beim jeweiligen Dokument. |
| Es passiert nichts | Ist die Quelle aktiv? Läuft der Worker (`systemctl status multi-gpt-worker`)? |
| Dateien fehlen | Dateimuster, Ausschlussmuster, Größe und versteckte Namen prüfen; das Ergebnis des letzten Laufs zeigt die Zahl der übersprungenen Dateien. |
