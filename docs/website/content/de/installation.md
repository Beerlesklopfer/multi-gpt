---
title: "Installation"
description: "MultiGPT als Debian-Paket, mit Docker oder für die Entwicklung mit make installieren."
lead: "Es gibt noch keine fertigen Pakete zum Herunterladen. Wer MultiGPT ausprobieren will, baut das Paket aus dem Quellcode mit `make deb`. Danach kann man mit eigenen API-Keys chatten."
menus:
  main:
    weight: 30
---

> **Stand:** `make deb` baut das Paket (`multi-gpt_0.1.0_amd64.deb`). Eine Testinstallation
> auf einem frischen Debian 13 steht noch aus. Der Docker-Weg ist bisher ungetestet. Das
> Paket bringt nginx mit TLS bereits mit; die vollständige Betriebsanleitung (mit Backup) folgt
> mit Meilenstein 12.

## Voraussetzungen

- Debian 13 (oder ein anderes System mit apt) mit Python 3.12 oder neuer.
- PostgreSQL mit der Erweiterung pgvector – lokal oder auf einem anderen Rechner.
- nginx (ab 1.25.1) und `ssl-cert` – installiert apt als Abhängigkeiten mit.
- Für den Paketbau: `debhelper` und `dh-virtualenv`.

## Weg 1: Debian-Paket (empfohlen)

Das Paket `multi-gpt` bringt seine eigene Python-Umgebung in `/usr/share/python/multi-gpt`
mit und wird über systemd gestartet. Es legt den Systemnutzer `multi-gpt` an, Daten liegen
in `/var/lib/multi-gpt`. nginx und TLS sind Teil des Pakets: gunicorn lauscht nur auf
`127.0.0.1`, davor steht nginx auf demselben Rechner.

**1. Paket bauen** (im Quellcode-Ordner):

```
sudo apt install build-essential debhelper dh-virtualenv python3-dev python3-venv
make deb
```

Das `.deb` liegt danach im übergeordneten Ordner.

**2. Installieren** (im Ordner mit dem `.deb`):

```
sudo apt install ./multi-gpt_<version>_amd64.deb
```

Bei der Installation passiert Folgendes:

- **Fragen per debconf:** der oder die **Hostnamen**, unter denen MultiGPT aufgerufen wird
  (Vorschlag: `hostname -f`), und der Pfad zum **TLS-Zertifikat** samt Schlüssel. Bleibt das
  Zertifikat leer, nutzt nginx das selbstsignierte snakeoil-Zertifikat: Browser warnen dann,
  und das Mikrofon ist ggf. eingeschränkt. `ALLOWED_HOSTS` und `CSRF_TRUSTED_ORIGINS` leitet
  das Paket aus den Hostnamen und den IP-Adressen des Rechners ab. Ändern lässt sich alles
  später mit `sudo dpkg-reconfigure multi-gpt`.
- **nginx mit TLS:** Das Paket richtet die Site `/etc/nginx/sites-available/multi-gpt` ein
  (HTTPS, Weiterleitung von HTTP auf HTTPS, gestreamte Antworten ohne Puffer, Upload-Grenze
  passend zu `DOCUMENT_MAX_UPLOAD_MB`), prüft sie mit `nginx -t` und aktiviert sie. Debians
  unveränderte Standard-Site `default` wird dafür abgeschaltet. Einzelheiten, eigenes
  Zertifikat und Fehlersuche stehen im
  [Wiki: nginx und TLS](https://github.com/Beerlesklopfer/multi-gpt/wiki/nginx-und-TLS).
- **Konfiguration:** Bei der ersten Installation entsteht `/etc/multi-gpt/.env` (Eigentümer
  `root:multi-gpt`, Rechte 0640) mit frisch erzeugten Schlüsseln. Updates überschreiben die
  Datei nicht.
- **Datenbank:** Läuft auf dem Rechner ein PostgreSQL, legt das Paket (preinst) die Rolle
  `multi-gpt`, die gleichnamige Datenbank und die Erweiterung pgvector an. Vorhandenes
  bleibt unverändert; zeigt `DATABASE_URL` auf einen anderen Rechner, passiert nichts.
- **Migration:** Ist die Datenbank erreichbar, migriert das Paket (postinst) das Schema
  automatisch, bevor der Dienst startet oder neu startet – bei der Installation und bei
  jedem Update. Ist sie nicht erreichbar, gibt es nur einen Hinweis; die Installation läuft
  trotzdem durch.
- **Zusätzliche Pakete für Dokumente (RAG):** apt installiert als Abhängigkeiten
  `poppler-utils` (PDF-Seiten als Bild) sowie `tesseract-ocr`, `tesseract-ocr-deu` und
  `tesseract-ocr-eng` für die Texterkennung gescannter Seiten. Weitere Sprachen gibt es als
  Pakete `tesseract-ocr-<sprache>` (dann `OCR_LANGUAGES` in `/etc/multi-gpt/.env` anpassen).
- **Zwei Dienste:** `multi-gpt.service` (Weboberfläche) und `multi-gpt-worker.service`
  (Worker, der hochgeladene Dokumente im Hintergrund indexiert). Beide werden aktiviert,
  gestartet und bei Updates nach der Migration neu gestartet. Prüfen mit
  `systemctl status multi-gpt multi-gpt-worker`.

**3. Datenbank von Hand anlegen** (nur nötig, wenn das Paket sie nicht anlegen konnte, etwa
weil PostgreSQL bei der Installation nicht lief):

```
sudo -u postgres createuser multi-gpt
sudo -u postgres createdb -O multi-gpt multi-gpt
sudo -u postgres psql -d multi-gpt -c 'CREATE EXTENSION IF NOT EXISTS vector'
```

**4. Konfiguration prüfen:** In `/etc/multi-gpt/.env` bei Bedarf `DATABASE_URL` anpassen.
Danach `sudo systemctl restart multi-gpt`.

**5. Ersten Verwalter erstellen** (und das Schema nachziehen, falls die automatische
Migration nicht lief):

```
sudo mgpt-ctl migrate
sudo mgpt-ctl createsuperuser
```

`mgpt-ctl` ist ein Wrapper um Djangos `manage.py`, der als Nutzer `multi-gpt` mit
`/etc/multi-gpt/.env` läuft. `mgpt-ctl migrate` lässt sich jederzeit gefahrlos wiederholen.

MultiGPT ist danach unter `https://<hostname>/` erreichbar. Ob die App läuft, zeigt
`curl -k https://<hostname>/healthz/` (`-k` nur mit dem snakeoil-Zertifikat).

**6. Dokumentsuche einrichten (optional):** Im Admin unter „Dokumente (RAG)“ →
„Einstellungen“ ein Embedding-Modell wählen (lokal z. B. nomic-embed-text über LM Studio)
und mit „Speichern und Embedding testen“ prüfen; für gescannte PDFs das OCR-Verfahren
(olmOCR über LM Studio oder Tesseract) wählen und mit „Speichern und OCR testen“ prüfen.
Die Anleitung dazu steht im [Wiki: RAG einrichten](https://github.com/Beerlesklopfer/multi-gpt/wiki/RAG-Einrichtung),
Ordner vom Server oder NAS beschreibt
[Wiki: Verzeichnisquellen](https://github.com/Beerlesklopfer/multi-gpt/wiki/RAG-Verzeichnisquellen)
(dafür `RAG_SOURCE_ROOTS` in `/etc/multi-gpt/.env` setzen).

**7. Websuche einrichten (optional):** Ein SearXNG im Heimnetz aufsetzen, im Admin die
Adresse eintragen und mit „SearXNG testen“ prüfen, danach die Websuche einschalten. Die
Anleitung steht im [Wiki: SearXNG](https://github.com/Beerlesklopfer/multi-gpt/wiki/SearXNG).

## Weg 2: Docker

Für Systeme ohne apt. Das Image baut und installiert dasselbe Debian-Paket;
`compose.yaml` bringt PostgreSQL mit pgvector als zweiten Dienst mit. Konfiguration kommt
aus `.env` im Projektordner (mindestens `SECRET_KEY`, `FIELD_ENCRYPTION_KEY`,
`ALLOWED_HOSTS`, `POSTGRES_PASSWORD`).

```
docker compose build
docker compose up -d db
docker compose run --rm web mgpt-ctl migrate
docker compose run --rm web mgpt-ctl createsuperuser
docker compose up -d
```

Hinweis: Der Docker-Weg ist noch nicht getestet.

## Weg 3: Entwicklung mit make

Das Makefile ist die Bedienoberfläche für die Entwicklung. Voraussetzung ist ein lokales
PostgreSQL mit pgvector.

```
make db-create install migrate user dev
```

- `make db-create` legt einmalig per `sudo` Datenbank, Rolle und die Erweiterung `vector` an.
- `make install` erzeugt `.venv` und eine `.env` mit generierten Schlüsseln (Rechte 600).
- `make user` legt einen Nutzer an, `make dev` startet den Entwicklungsserver.
- `make worker` startet den Worker für die Indexierung von Dokumenten im Vordergrund,
  `make reindex` reiht alle Dokumente neu ein (nach einem Wechsel des Embedding-Modells).
  Für OCR braucht der Entwicklungsrechner `poppler-utils`, `tesseract-ocr`,
  `tesseract-ocr-deu` und `tesseract-ocr-eng`.
- `make run` startet gunicorn, `make test` und `make lint` prüfen den Code,
  `make help` zeigt alle Ziele.
