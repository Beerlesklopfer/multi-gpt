---
title: "Installation"
description: "Install MultiGPT as a Debian package, with Docker, or for development with make."
lead: "There are no ready-made packages to download yet. If you want to try MultiGPT, you build it from source. After installation there is currently only the login and an empty chat page."
menus:
  main:
    weight: 30
---

> **Status at milestone 1:** the package build has been reproduced and checked without
> debhelper, but a test installation on a fresh Debian 13 is still pending. The Docker
> route is untested so far. The complete installation guide (with nginx and TLS) follows
> with milestone 11.

## Requirements

- Debian 13 (or another system with apt) with Python 3.12 or newer.
- PostgreSQL with the pgvector extension – local or on another machine.
- For building the package: `debhelper` and `dh-virtualenv`.

## Option 1: Debian package (recommended)

The `multi-gpt` package ships its own Python environment in `/usr/share/python/multi-gpt`
and is started via systemd. It creates the system user `multi-gpt`; data lives in
`/var/lib/multi-gpt`.

**1. Build the package** (in the source folder):

```
sudo apt install build-essential debhelper dh-virtualenv python3-dev python3-venv
make deb
```

The `.deb` ends up in the parent folder.

**2. Install:**

```
sudo apt install ../multi-gpt_<version>_<arch>.deb
```

The first installation creates `/etc/multi-gpt/.env` (owner `root:multi-gpt`, mode 0640)
with freshly generated keys. Updates never overwrite this file.

**3. Create the database** (once, for a local PostgreSQL):

```
sudo -u postgres createuser multi-gpt
sudo -u postgres createdb -O multi-gpt multi-gpt
sudo -u postgres psql -d multi-gpt -c 'CREATE EXTENSION IF NOT EXISTS vector'
```

**4. Check the configuration:** in `/etc/multi-gpt/.env`, adjust mainly `ALLOWED_HOSTS`,
`DATABASE_URL` and – once nginx with TLS sits in front – `CSRF_TRUSTED_ORIGINS` and
`SECURE_COOKIES`. Then run `sudo systemctl restart multi-gpt`.

**5. Create the schema and the first administrator:**

```
sudo mgpt-ctl migrate
sudo mgpt-ctl createsuperuser
```

`mgpt-ctl` is a wrapper around Django's `manage.py` that runs as the `multi-gpt` user with
`/etc/multi-gpt/.env`. `mgpt-ctl migrate` is required **after every installation and
update**; the package does not migrate automatically.

By default the app listens on `127.0.0.1:8000`. `curl http://127.0.0.1:8000/healthz/`
shows whether it is running.

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
- `make run` starts gunicorn, `make test` and `make lint` check the code, and
  `make help` lists all targets.
