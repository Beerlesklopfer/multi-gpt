# MultiGPT – Bedienoberfläche für die Entwicklung (Plan Abschnitt 10, Stand M1).
# `make` ohne Ziel zeigt die Hilfe.

PYTHON  ?= python3
VENV    ?= .venv
BIN     := $(VENV)/bin
PY      := $(BIN)/python
MANAGE  := $(PY) manage.py
DB_NAME ?= multigpt
DB_USER ?= $(shell id -un)
DB_TEMPLATE ?= multigpt_template
# Immer UTF-8, unabhängig vom Standard des Clusters (der kann SQL_ASCII sein).
DB_UTF8 := TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C.UTF-8' LC_CTYPE 'C.UTF-8'
PSQL    := sudo -u postgres psql -v ON_ERROR_STOP=1 -X -q
# Hugo: aus dem PATH, sonst aus ~/go/bin (go install github.com/gohugoio/hugo@<version>).
HUGO    ?= $(or $(shell command -v hugo 2>/dev/null),$(HOME)/go/bin/hugo)
SITE    := docs/website
GH      ?= $(or $(shell command -v gh 2>/dev/null),$(HOME)/go/bin/gh)
REMOTE  ?= origin

.DEFAULT_GOAL := help
.PHONY: help install migrate user dev run static sync-models worker reindex test lint fmt deb website website-serve deploy db-create clean

help: ## Diese Hilfe anzeigen
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

$(PY):
	$(PYTHON) -m venv $(VENV)

# .env aus der Vorlage mit frisch generierten Schlüsseln; nur Standardbibliothek,
# da `cryptography` noch nicht installiert ist. Fernet-Schlüssel = urlsafe-base64
# von 32 Zufallsbytes. Die Datei wird direkt mit Rechten 600 angelegt.
.env: | .env.example
	@$(PYTHON) -c 'import base64, os, secrets; \
	t = open(".env.example", encoding="utf-8").read(); \
	t = t.replace("SECRET_KEY=__SECRET_KEY__", "SECRET_KEY=" + secrets.token_urlsafe(50)); \
	t = t.replace("FIELD_ENCRYPTION_KEY=__FIELD_ENCRYPTION_KEY__", "FIELD_ENCRYPTION_KEY=" + base64.urlsafe_b64encode(os.urandom(32)).decode()); \
	fd = os.open(".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); \
	os.write(fd, t.encode()); os.close(fd)'
	@chmod 600 .env
	@echo ".env mit neuen Schlüsseln angelegt (Rechte 600)."

install: $(PY) .env ## venv anlegen, Abhängigkeiten installieren, .env erzeugen
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e '.[dev]'

migrate: ## Datenbankmigrationen ausführen
	$(MANAGE) migrate

# Rolle vorwählen: make user ROLE=adult (sonst interaktive Auswahl).
user: ## Konto mit Rolle anlegen (Passwort wird verdeckt abgefragt)
	$(MANAGE) create_account $(if $(ROLE),--role $(ROLE))

dev: ## Entwicklungsserver starten
	$(MANAGE) runserver

# Die Settings lesen .env selbst, gunicorn.conf.py aber nur die Umgebung.
# Deshalb wird .env hier exportiert (für MULTI_GPT_BIND usw.).
run: ## gunicorn im Vordergrund starten
	set -a; [ ! -f .env ] || . ./.env; set +a; \
	exec $(BIN)/gunicorn -c deploy/gunicorn.conf.py multigpt.wsgi:application

static: ## Statische Dateien sammeln (collectstatic)
	$(MANAGE) collectstatic --noinput

sync-models: ## Modelllisten der Anbieter abrufen
	$(MANAGE) sync_models

worker: ## Worker für die Indexierung im Vordergrund starten (Strg+C beendet)
	$(MANAGE) run_worker

reindex: ## Alle Dokumente neu indexieren (nach Wechsel des Embedding-Modells)
	$(MANAGE) reindex

test: ## Tests ausführen (pytest, braucht PostgreSQL)
	$(BIN)/pytest

lint: ## Code prüfen (ruff check + Formatprüfung)
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

fmt: ## Code formatieren und Autofixes anwenden
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

deb: ## Debian-Paket bauen
	dpkg-buildpackage -us -uc -b
	@echo "Hinweis: Das .deb liegt im übergeordneten Ordner: $$(ls -1 ../multi-gpt_*.deb 2>/dev/null | tail -n 1)"

website: ## Projekt-Website (Hugo) nach docs/website/public bauen
	$(HUGO) --source $(SITE) --minify --cleanDestinationDir --panicOnWarning

website-serve: ## Website lokal mit Live-Reload anzeigen (http://localhost:1313/)
	$(HUGO) server --source $(SITE)

# Veröffentlicht wird auf GitHub Pages durch den Workflow website.yml, der auf
# GitHub baut. deploy prüft lokal, dass die Site baut und der Stand von
# docs/website committet und gepusht ist, und stößt dann den Workflow an.
deploy: website ## Website auf GitHub Pages veröffentlichen (braucht gh und Remote)
	@[ -x "$(GH)" ] || { echo "deploy: GitHub-CLI 'gh' fehlt (go install github.com/cli/cli/v2/cmd/gh@latest, dann gh auth login)." >&2; exit 1; }
	@git remote get-url $(REMOTE) >/dev/null 2>&1 || { echo "deploy: Git-Remote '$(REMOTE)' fehlt." >&2; exit 1; }
	@git diff --quiet HEAD -- $(SITE) .github/workflows/website.yml \
		&& [ -z "$$(git ls-files --others --exclude-standard -- $(SITE))" ] \
		|| { echo "deploy: Änderungen in $(SITE) sind nicht committet." >&2; exit 1; }
	git fetch -q $(REMOTE) main
	@[ -z "$$(git log --oneline $(REMOTE)/main..HEAD -- $(SITE) .github/workflows/website.yml)" ] \
		|| { echo "deploy: Website-Commits sind noch nicht nach $(REMOTE)/main gepusht." >&2; exit 1; }
	$(GH) workflow run website.yml --ref main
	@echo "Workflow gestartet. Fortschritt: gh run watch"

# Entwicklungs-DB anlegen (einmalig, braucht sudo):
# - Rolle = aktueller Systemnutzer mit CREATEDB (Peer-Auth über den Unix-Socket;
#   CREATEDB, damit pytest die Test-DB anlegen kann).
# - pgvector ist keine "trusted" Extension: Ein Nicht-Superuser kann sie nicht
#   anlegen. Deshalb legt postgres sie in template1 an – damit hat jede neue DB,
#   auch die Test-DB, die Extension – und zusätzlich in $(DB_NAME), falls die DB
#   schon existierte. Die Migration chat/0001_pgvector (IF NOT EXISTS) ist dann
#   ein No-op.
db-create: ## Entwicklungs-DB (UTF-8) samt Rolle, pgvector und Test-Vorlage anlegen (sudo)
	$(PSQL) -tAc "SELECT 1 FROM pg_roles WHERE rolname = '$(DB_USER)'" | grep -q 1 \
		|| $(PSQL) -c 'CREATE ROLE "$(DB_USER)" LOGIN CREATEDB'
	$(PSQL) -tAc "SELECT 1 FROM pg_database WHERE datname = '$(DB_NAME)'" | grep -q 1 \
		|| $(PSQL) -c "CREATE DATABASE \"$(DB_NAME)\" OWNER \"$(DB_USER)\" $(DB_UTF8)"
	$(PSQL) -d $(DB_NAME) -c 'CREATE EXTENSION IF NOT EXISTS vector'
	$(PSQL) -tAc "SELECT 1 FROM pg_database WHERE datname = '$(DB_TEMPLATE)'" | grep -q 1 \
		|| $(PSQL) -c "CREATE DATABASE \"$(DB_TEMPLATE)\" $(DB_UTF8)"
	$(PSQL) -d $(DB_TEMPLATE) -c 'CREATE EXTENSION IF NOT EXISTS vector'
	$(PSQL) -c "ALTER DATABASE \"$(DB_TEMPLATE)\" WITH IS_TEMPLATE true"
	@echo "In .env: DB_TEST_TEMPLATE=$(DB_TEMPLATE)"

clean: ## Caches und Build-Reste entfernen (.venv und .env bleiben)
	find . -path ./$(VENV) -prune -o -type d -name __pycache__ -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache staticfiles build dist
	rm -rf debian/.debhelper debian/multi-gpt debian/files debian/*.substvars \
		debian/*.debhelper.log debian/debhelper-build-stamp
