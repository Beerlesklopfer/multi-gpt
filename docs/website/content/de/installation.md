---
title: "Installation"
description: "MultiGPT als Debian-Paket, mit Docker oder für die Entwicklung mit make installieren."
lead: "Es gibt noch keine fertigen Pakete zum Herunterladen. Wer MultiGPT ausprobieren will, baut das Paket aus dem Quellcode mit `make deb`. Danach kann man mit eigenen API-Keys chatten."
menus:
  main:
    weight: 30
---

> **Stand:** `make deb` baut das Paket (`multi-gpt_0.1.0_amd64.deb`). Eine Testinstallation
> auf einem frischen Debian 13 steht noch aus. Der Docker-Weg ist bisher ungetestet. Die
> vollständige Installationsanleitung (mit nginx und TLS) folgt mit Meilenstein 12.

## Voraussetzungen

- Debian 13 (oder ein anderes System mit apt) mit Python 3.12 oder neuer.
- PostgreSQL mit der Erweiterung pgvector – lokal oder auf einem anderen Rechner.
- Für den Paketbau: `debhelper` und `dh-virtualenv`.

## Weg 1: Debian-Paket (empfohlen)

Das Paket `multi-gpt` bringt seine eigene Python-Umgebung in `/usr/share/python/multi-gpt`
mit und wird über systemd gestartet. Es legt den Systemnutzer `multi-gpt` an, Daten liegen
in `/var/lib/multi-gpt`.

**1. Paket bauen** (im Quellcode-Ordner):

```
sudo apt install build-essential debhelper dh-virtualenv python3-dev python3-venv
make deb
```

Das `.deb` liegt danach im übergeordneten Ordner.

**2. Installieren:**

```
sudo apt install ../multi-gpt_<version>_<arch>.deb
```

Bei der Installation passiert Folgendes:

- **Fragen per debconf:** Bind-Adresse (z. B. `127.0.0.1:8000` hinter einem nginx auf
  demselben Rechner oder die LAN-Adresse), `ALLOWED_HOSTS` und die Adressen für
  `CSRF_TRUSTED_ORIGINS`. Die Antworten landen in `/etc/multi-gpt/.env`; ändern lassen sie
  sich später mit `sudo dpkg-reconfigure multi-gpt`.
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

**4. Konfiguration prüfen:** In `/etc/multi-gpt/.env` bei Bedarf `DATABASE_URL` und –
sobald nginx mit TLS davor steht – `SECURE_COOKIES` anpassen. Danach
`sudo systemctl restart multi-gpt`.

**5. Ersten Verwalter erstellen** (und das Schema nachziehen, falls die automatische
Migration nicht lief):

```
sudo mgpt-ctl migrate
sudo mgpt-ctl createsuperuser
```

`mgpt-ctl` ist ein Wrapper um Djangos `manage.py`, der als Nutzer `multi-gpt` mit
`/etc/multi-gpt/.env` läuft. `mgpt-ctl migrate` lässt sich jederzeit gefahrlos wiederholen.

Die App lauscht auf der per debconf gewählten Adresse. Ob sie läuft, zeigt
`curl http://<adresse>:<port>/healthz/`.

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
