---
title: "Installation"
description: "MultiGPT als Debian-Paket, mit Docker oder für die Entwicklung mit make installieren."
lead: "Es gibt noch keine fertigen Pakete zum Herunterladen. Wer MultiGPT ausprobieren will, baut es aus dem Quellcode. Nach der Installation gibt es derzeit nur die Anmeldung und eine leere Chatseite."
menus:
  main:
    weight: 30
---

> **Stand Meilenstein 1:** Der Paketbau ist ohne debhelper nachgestellt und geprüft, eine
> Testinstallation auf einem frischen Debian 13 steht aber noch aus. Der Docker-Weg ist
> bisher ungetestet. Die vollständige Installationsanleitung (mit nginx und TLS) folgt mit
> Meilenstein 11.

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

Bei der ersten Installation entsteht `/etc/multi-gpt/.env` (Eigentümer `root:multi-gpt`,
Rechte 0640) mit frisch erzeugten Schlüsseln. Die Datei wird bei Updates nie überschrieben.

**3. Datenbank anlegen** (einmalig, bei lokalem PostgreSQL):

```
sudo -u postgres createuser multi-gpt
sudo -u postgres createdb -O multi-gpt multi-gpt
sudo -u postgres psql -d multi-gpt -c 'CREATE EXTENSION IF NOT EXISTS vector'
```

**4. Konfiguration prüfen:** In `/etc/multi-gpt/.env` vor allem `ALLOWED_HOSTS`,
`DATABASE_URL` und – sobald nginx mit TLS davor steht – `CSRF_TRUSTED_ORIGINS` und
`SECURE_COOKIES` anpassen. Danach `sudo systemctl restart multi-gpt`.

**5. Schema anlegen und ersten Verwalter erstellen:**

```
sudo mgpt-ctl migrate
sudo mgpt-ctl createsuperuser
```

`mgpt-ctl` ist ein Wrapper um Djangos `manage.py`, der als Nutzer `multi-gpt` mit
`/etc/multi-gpt/.env` läuft. `mgpt-ctl migrate` ist **nach jeder Installation und jedem
Update** nötig; das Paket migriert nicht automatisch.

Die App lauscht standardmäßig auf `127.0.0.1:8000`. Ob sie läuft, zeigt
`curl http://127.0.0.1:8000/healthz/`.

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
- `make run` startet gunicorn, `make test` und `make lint` prüfen den Code,
  `make help` zeigt alle Ziele.
