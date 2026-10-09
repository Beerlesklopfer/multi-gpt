---
title: "Installation"
description: "Install MultiGPT as a Debian package, with Docker, or for development with make."
lead: "There are no ready-made packages to download yet. If you want to try MultiGPT, you build the package from source with `make deb`. After that you can chat using your own API keys."
menus:
  main:
    weight: 30
---

> **Status:** `make deb` builds the package (`multi-gpt_0.1.0_amd64.deb`). A test
> installation on a fresh Debian 13 is still pending. The Docker route is untested so far.
> The package already ships nginx with TLS; the complete operations guide (with backup)
> follows with milestone 12.

## Requirements

- Debian 13 (or another system with apt) with Python 3.12 or newer.
- PostgreSQL with the pgvector extension – local or on another machine.
- nginx (1.25.1 or newer) and `ssl-cert` – apt installs them as dependencies.
- For building the package: `debhelper` and `dh-virtualenv`.

## Option 1: Debian package (recommended)

The `multi-gpt` package ships its own Python environment in `/usr/share/python/multi-gpt`
and is started via systemd. It creates the system user `multi-gpt`; data lives in
`/var/lib/multi-gpt`. nginx and TLS are part of the package: gunicorn listens on
`127.0.0.1` only, with nginx in front of it on the same machine.

**1. Build the package** (in the source folder):

```
sudo apt install build-essential debhelper dh-virtualenv python3-dev python3-venv
make deb
```

The `.deb` ends up in the parent folder.

**2. Install** (in the folder containing the `.deb`):

```
sudo apt install ./multi-gpt_<version>_amd64.deb
```

During installation the following happens:

- **debconf questions:** the **host name(s)** under which MultiGPT is reached (suggested:
  `hostname -f`) and the path to the **TLS certificate** and its key. If the certificate is
  left empty, nginx uses the self-signed snakeoil certificate: browsers then show a warning,
  and the microphone may be restricted. The package derives `ALLOWED_HOSTS` and
  `CSRF_TRUSTED_ORIGINS` from the host names and the machine's IP addresses. Change all of
  this later with `sudo dpkg-reconfigure multi-gpt`.
- **nginx with TLS:** the package sets up the site `/etc/nginx/sites-available/multi-gpt`
  (HTTPS, redirect from HTTP to HTTPS, unbuffered streamed answers, upload limit matching
  `DOCUMENT_MAX_UPLOAD_MB`), checks it with `nginx -t` and enables it. Debian's unmodified
  default site `default` is disabled for this. Details, your own certificate and
  troubleshooting are in the
  [wiki: nginx und TLS](https://github.com/Beerlesklopfer/multi-gpt/wiki/nginx-und-TLS) (German).
- **Configuration:** the first installation creates `/etc/multi-gpt/.env` (owner
  `root:multi-gpt`, mode 0640) with freshly generated keys. Updates do not overwrite it.
- **Database:** if PostgreSQL runs on the machine, the package (preinst) creates the role
  `multi-gpt`, the database of the same name and the pgvector extension. Anything that
  already exists is left as it is; if `DATABASE_URL` points to another machine, nothing happens.
- **Migration:** if the database is reachable, the package (postinst) migrates the schema
  automatically before the service starts or restarts – on installation and on every
  update. If it is not reachable, you only get a notice; the installation still completes.
- **Additional packages for documents (RAG):** apt installs `poppler-utils` (PDF pages as
  images) plus `tesseract-ocr`, `tesseract-ocr-deu` and `tesseract-ocr-eng` for text
  recognition of scanned pages as dependencies. Further languages come as
  `tesseract-ocr-<language>` packages (then adjust `OCR_LANGUAGES` in `/etc/multi-gpt/.env`).
- **Two services:** `multi-gpt.service` (web interface) and `multi-gpt-worker.service` (the
  worker that indexes uploaded documents in the background). Both are enabled, started and
  restarted after the migration on updates. Check with
  `systemctl status multi-gpt multi-gpt-worker`.

**3. Create the database by hand** (only needed if the package could not create it, e.g.
because PostgreSQL was not running during installation):

```
sudo -u postgres createuser multi-gpt
sudo -u postgres createdb -O multi-gpt multi-gpt
sudo -u postgres psql -d multi-gpt -c 'CREATE EXTENSION IF NOT EXISTS vector'
```

**4. Check the configuration:** in `/etc/multi-gpt/.env`, adjust `DATABASE_URL` if needed.
Then run `sudo systemctl restart multi-gpt`.

**5. Create the first administrator** (and catch up on the schema if the automatic
migration did not run):

```
sudo mgpt-ctl migrate
sudo mgpt-ctl createsuperuser
```

`mgpt-ctl` is a wrapper around Django's `manage.py` that runs as the `multi-gpt` user with
`/etc/multi-gpt/.env`. `mgpt-ctl migrate` can safely be run again at any time.

MultiGPT is then reachable at `https://<hostname>/`. `curl -k https://<hostname>/healthz/`
shows whether the app is running (`-k` only with the snakeoil certificate).

**6. Set up document search (optional):** in the admin under “Dokumente (RAG)” →
“Einstellungen”, choose an embedding model (locally e.g. nomic-embed-text via LM Studio)
and check it with “Speichern und Embedding testen”; for scanned PDFs choose the OCR method
(olmOCR via LM Studio or Tesseract) and check it with “Speichern und OCR testen”. The guide
is in the [wiki: RAG einrichten](https://github.com/Beerlesklopfer/multi-gpt/wiki/RAG-Einrichtung)
(German); folders from the server or NAS are covered in
[wiki: Verzeichnisquellen](https://github.com/Beerlesklopfer/multi-gpt/wiki/RAG-Verzeichnisquellen)
(set `RAG_SOURCE_ROOTS` in `/etc/multi-gpt/.env` for this).

**7. Set up web search (optional):** run a SearXNG in the home network, enter its address
in the admin, check it with “SearXNG testen” and then switch web search on. The guide is in
the [wiki: SearXNG](https://github.com/Beerlesklopfer/multi-gpt/wiki/SearXNG) (German).

## Option 2: Docker

For systems without apt. The image builds and installs the same Debian package;
`compose.yaml` adds PostgreSQL with pgvector as a second service. Configuration comes from
`.env` in the project folder (at least `SECRET_KEY`, `FIELD_ENCRYPTION_KEY`,
`ALLOWED_HOSTS`, `POSTGRES_PASSWORD`).

```
docker compose build
docker compose up -d db
docker compose run --rm web mgpt-ctl migrate
docker compose run --rm web mgpt-ctl createsuperuser
docker compose up -d
```

Note: the Docker route has not been tested yet.

## Option 3: Development with make

The Makefile is the interface for development. It requires a local PostgreSQL with pgvector.

```
make db-create install migrate user dev
```

- `make db-create` creates the database, role and the `vector` extension once, using `sudo`.
- `make install` creates `.venv` and a `.env` with generated keys (mode 600).
- `make user` creates a user, `make dev` starts the development server.
- `make worker` runs the document indexing worker in the foreground, and `make reindex`
  queues all documents again (after changing the embedding model). For OCR the development
  machine needs `poppler-utils`, `tesseract-ocr`, `tesseract-ocr-deu` and
  `tesseract-ocr-eng`.
- `make run` starts gunicorn, `make test` and `make lint` check the code, and
  `make help` lists all targets.
