# Konfiguration (`/etc/multi-gpt/.env`)

Alle Einstellungen von MultiGPT kommen aus Umgebungsvariablen. Im Betrieb (Debian-Paket) stehen
sie in **`/etc/multi-gpt/.env`** (Rechte `root:multi-gpt`, `0640`). Beide Dienste lesen die Datei
(`EnvironmentFile=`), ebenso `mgpt-ctl`. In der Entwicklung gilt stattdessen `.env` im Projektordner
(Vorlage: `.env.example`).

Nach jeder Änderung die Dienste neu starten:

```bash
sudo systemctl restart multi-gpt multi-gpt-worker
```

Das Paket erzeugt die Datei bei der Erstinstallation mit zufälligen Schlüsseln und überschreibt sie
nie. Einige Werte verwaltet **debconf** (`sudo dpkg-reconfigure multi-gpt`) – sie werden bei einer
erneuten Abfrage in die Datei geschrieben; Handänderungen an diesen Zeilen werden dabei zum Vorschlag.

## Grundlagen

| Schlüssel | Standard (Code) | Debian-Paket | Bedeutung |
|---|---|---|---|
| `SECRET_KEY` | – (Pflicht) | zufällig erzeugt | Django-Geheimschlüssel. Geheim halten, nicht ändern (sonst werden alle Sitzungen ungültig). |
| `FIELD_ENCRYPTION_KEY` | leer | zufällig erzeugt | Fernet-Schlüssel für API-Keys und MCP-Zugangsdaten in der Datenbank. **Nie verlieren oder ändern** – sonst lassen sich gespeicherte Keys nicht mehr entschlüsseln. Gehört ins Backup. |
| `DEBUG` | `False` | `False` | Nur in der Entwicklung `True`. |
| `DATABASE_URL` | – (Pflicht) | `postgres:///multi-gpt` | PostgreSQL mit pgvector. Standard: Unix-Socket, Peer-Auth als Systemnutzer `multi-gpt`. Für einen anderen Server z. B. `postgres://nutzer:passwort@host:5432/db`. |
| `DB_CONN_MAX_AGE` | `60` | – | Sekunden, die eine DB-Verbindung wiederverwendet wird. |
| `MEDIA_ROOT` | `<Projekt>/media` | `/var/lib/multi-gpt/media` | Hochgeladene Dokumente, Anhänge. Liegt der Ordner außerhalb von `/var/lib/multi-gpt`, braucht der Dienst ein systemd-Drop-in mit `ReadWritePaths=`. |
| `STATIC_ROOT` | `<Projekt>/staticfiles` | `/usr/share/python/multi-gpt/static` (in der Unit gesetzt) | Statische Dateien; im Paket beim Bau gesammelt. Nicht ändern. |
| `LOG_LEVEL` | `INFO` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. Logs enthalten nie Nachrichteninhalte oder Keys. |

## Netz und Sicherheit

| Schlüssel | Standard (Code) | Debian-Paket | Bedeutung |
|---|---|---|---|
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | aus debconf (Hostname und IP-Adressen des Rechners) | Namen/Adressen, unter denen MultiGPT aufgerufen werden darf (kommagetrennt). |
| `CSRF_TRUSTED_ORIGINS` | leer | aus debconf (`https://<hostname>` …) | Vollständige Origins mit Schema, über die Formulare abgeschickt werden dürfen. Ohne passenden Eintrag schlägt die Anmeldung fehl. |
| `SECURE_COOKIES` | `False` | `True` (ab der nginx-Umstellung) | Cookies nur über HTTPS; zugleich wertet Django `X-Forwarded-Proto` von nginx aus. Nur mit TLS davor einschalten. |
| `MULTI_GPT_BIND` | `127.0.0.1:8000` | `127.0.0.1:8000` (fest, ab der nginx-Umstellung) | Adresse, auf der gunicorn lauscht. Im Paket immer localhost – davor steht nginx. |
| `AXES_FAILURE_LIMIT` | `5` | – | Fehlversuche bis zur Login-Sperre. |
| `AXES_COOLOFF_HOURS` | `0.25` | – | Dauer der Login-Sperre in Stunden (0.25 = 15 Minuten). |
| `AXES_PROXY_COUNT` | `0` | `1` (ab der nginx-Umstellung) | Anzahl Reverse Proxys vor MultiGPT. Mit nginx davor `1`, damit die Login-Sperre die echte Client-IP sieht statt `127.0.0.1`. |

## gunicorn

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `MULTI_GPT_WORKERS` | `2` | Prozesse. |
| `MULTI_GPT_THREADS` | `8` | Threads je Prozess. Jede laufende Antwort (Streaming) belegt einen Thread; ein Vergleich mit 3 Modellen belegt 3. |
| `MULTI_GPT_TIMEOUT` | `300` | Sekunden bis zum Abbruch einer hängenden Anfrage (lange Antworten, OCR-Test). |

## Dokumente (RAG)

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `DOCUMENT_MAX_UPLOAD_MB` | `25` | Höchstgröße je Dokument (Upload und Verzeichnisquellen). nginx muss das zulassen (`client_max_body_size`, im Paket passend gesetzt). |
| `OCR_LANGUAGES` | `deu+eng` | Sprachen für Tesseract (Ersatz-OCR). |
| `JOB_MAX_ATTEMPTS` | `5` | Versuche je Indexierungsauftrag, danach Status „Fehler“. Wartezeiten wegen eines ausgeschalteten LM Studio zählen nicht mit. |
| `RAG_SOURCE_ROOTS` | leer (aus) | Erlaubte Wurzeln für [Verzeichnisquellen](RAG-Verzeichnisquellen), kommagetrennt, z. B. `/srv/nas/dokumente`. Leer = Funktion aus. |
| `RAG_SOURCE_MAX_FILES` | `5000` | Höchstzahl Dateien je Einlesevorgang einer Verzeichnisquelle. |

Embedding-Modell, Präfixe und OCR-Verfahren stehen **nicht** in der `.env`, sondern im Admin unter
„Dokumente (RAG)“ → „Einstellungen“ (siehe [RAG-Einrichtung](RAG-Einrichtung)). Die Websuche
(SearXNG-URL usw.) steht im Admin unter „Chat“ → „Sucheinstellungen“ (siehe [SearXNG](SearXNG)).

## Nur für Entwicklung und Tests

Diese Schlüssel wirken nur mit `DEBUG=True` bzw. nur in der Entwicklung – im Betrieb nicht setzen.

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `DB_TEST_TEMPLATE` | leer | Vorlage für die Test-Datenbank (UTF-8 mit pgvector), z. B. `multigpt_template` aus `make db-create`. |
| `RAG_FAKE_EMBEDDINGS` | `False` | Schein-Embeddings ohne Anbieter (nur mit `DEBUG`). |
| `WEBSEARCH_ALLOW_PRIVATE` | `False` | Seitenabruf der Websuche auch ins Heimnetz erlauben (nur mit `DEBUG`, für Tests). |
| `MULTI_GPT_ENV_FILE` | – | Pfad einer alternativen Env-Datei; setzt `mgpt-ctl` automatisch auf `/etc/multi-gpt/.env`. |
