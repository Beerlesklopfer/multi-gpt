# MultiGPT – Bedienoberfläche für die Entwicklung (Plan Abschnitt 10, Stand M1).
# `make` ohne Ziel zeigt die Hilfe.

PYTHON  ?= python3
VENV    ?= .venv
BIN     := $(VENV)/bin
PY      := $(BIN)/python
MANAGE  := $(PY) manage.py
DB_NAME ?= multigpt
DB_USER ?= $(shell id -un)
PSQL    := sudo -u postgres psql -v ON_ERROR_STOP=1 -X -q

.DEFAULT_GOAL := help
.PHONY: help install migrate user dev run static test lint fmt deb db-create clean

help: ## Diese Hilfe anzeigen
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

$(PY):
	$(PYTHON) -m venv $(VENV)

# .env aus der Vorlage mit frisch generierten Schlüsseln; nur Standardbibliothek,
# da `cryptography` noch nicht installiert ist. Fernet-Schlüssel = urlsafe-base64
# von 32 Zufallsbytes. Die Datei wird direkt mit Rechten 600 angelegt.
.env: | .env.example
	@$(PYTHON) -c 'import base64, os, secrets; \
	t = open(".env.example", encoding="utf-8").read(); \
	t = t.replace("__SECRET_KEY__", secrets.token_urlsafe(50)); \
	t = t.replace("__FIELD_ENCRYPTION_KEY__", base64.urlsafe_b64encode(os.urandom(32)).decode()); \
	fd = os.open(".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); \
	os.write(fd, t.encode()); os.close(fd)'
	@chmod 600 .env
	@echo ".env mit neuen Schlüsseln angelegt (Rechte 600)."

install: $(PY) .env ## venv anlegen, Abhängigkeiten installieren, .env erzeugen
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e '.[dev]'

migrate: ## Datenbankmigrationen ausführen
	$(MANAGE) migrate

user: ## Nutzer anlegen (vorerst Superuser; Rollenwahl ab M2)
	$(MANAGE) createsuperuser

dev: ## Entwicklungsserver starten
	$(MANAGE) runserver

# Die Settings lesen .env selbst, gunicorn.conf.py aber nur die Umgebung.
# Deshalb wird .env hier exportiert (für MULTI_GPT_BIND usw.).
run: ## gunicorn im Vordergrund starten
	set -a; [ ! -f .env ] || . ./.env; set +a; \
	exec $(BIN)/gunicorn -c deploy/gunicorn.conf.py multigpt.wsgi:application

static: ## Statische Dateien sammeln (collectstatic)
	$(MANAGE) collectstatic --noinput

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

# Entwicklungs-DB anlegen (einmalig, braucht sudo):
# - Rolle = aktueller Systemnutzer mit CREATEDB (Peer-Auth über den Unix-Socket;
#   CREATEDB, damit pytest die Test-DB anlegen kann).
# - pgvector ist keine "trusted" Extension: Ein Nicht-Superuser kann sie nicht
#   anlegen. Deshalb legt postgres sie in template1 an – damit hat jede neue DB,
#   auch die Test-DB, die Extension – und zusätzlich in $(DB_NAME), falls die DB
#   schon existierte. Die Migration chat/0001_pgvector (IF NOT EXISTS) ist dann
#   ein No-op.
db-create: ## Entwicklungs-DB samt Rolle und pgvector anlegen (sudo)
	$(PSQL) -d template1 -c 'CREATE EXTENSION IF NOT EXISTS vector'
	$(PSQL) -tAc "SELECT 1 FROM pg_roles WHERE rolname = '$(DB_USER)'" | grep -q 1 \
		|| $(PSQL) -c 'CREATE ROLE "$(DB_USER)" LOGIN CREATEDB'
	$(PSQL) -tAc "SELECT 1 FROM pg_database WHERE datname = '$(DB_NAME)'" | grep -q 1 \
		|| $(PSQL) -c 'CREATE DATABASE "$(DB_NAME)" OWNER "$(DB_USER)"'
	$(PSQL) -d $(DB_NAME) -c 'CREATE EXTENSION IF NOT EXISTS vector'

clean: ## Caches und Build-Reste entfernen (.venv und .env bleiben)
	find . -path ./$(VENV) -prune -o -type d -name __pycache__ -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache staticfiles build dist
	rm -rf debian/.debhelper debian/multi-gpt debian/files debian/*.substvars \
		debian/*.debhelper.log debian/debhelper-build-stamp
