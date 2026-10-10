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
nie als Ganzes. Einige Zeilen schreibt das Paket aber bei **jeder** Konfiguration (Installation,
Upgrade, `sudo dpkg-reconfigure multi-gpt`) neu: `MULTI_GPT_BIND`, `ALLOWED_HOSTS` und
`CSRF_TRUSTED_ORIGINS`. Sie werden aus den debconf-Antworten (Hostnamen) und den IP-Adressen des
Rechners abgeleitet; Handänderungen daran gehen verloren – nur von Hand in `ALLOWED_HOSTS`
eingetragene Namen erscheinen bei der nächsten Abfrage als Vorschlag für die Hostnamen. Details
unter [nginx und TLS](nginx-und-TLS).

Einstellungen, die im Admin gepflegt werden, stehen nicht in dieser Datei, z. B. Grundregeln,
Standard-Kreativität und Standard-Denktiefe (siehe [Chat-Einstellungen](Chat-Einstellungen)).

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
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | abgeleitet: `localhost`, `127.0.0.1`, die Hostnamen aus debconf (`*.example.org` wird zu `.example.org`) und die IPv4-Adressen des Rechners (`hostname -I`); bei jeder Konfiguration neu geschrieben | Namen/Adressen, unter denen MultiGPT aufgerufen werden darf (kommagetrennt). Nach einer Änderung von Hostname oder IP: `sudo dpkg-reconfigure multi-gpt`. |
| `CSRF_TRUSTED_ORIGINS` | leer | abgeleitet: `https://<name>` für jeden Hostnamen und jede IP-Adresse; bei jeder Konfiguration neu geschrieben | Vollständige Origins mit Schema, über die Formulare abgeschickt werden dürfen. Ohne passenden Eintrag schlägt die Anmeldung fehl. |
| `SECURE_COOKIES` | `False` | `True` (bei der ersten nginx-Einrichtung gesetzt, wenn leer oder aus) | Cookies nur über HTTPS; zugleich wertet Django `X-Forwarded-Proto` von nginx aus. Nur mit TLS davor einschalten. |
| `MULTI_GPT_BIND` | `127.0.0.1:8000` | `127.0.0.1:<port>` (Adresse fest, Port bleibt erhalten) | Adresse, auf der gunicorn lauscht. Im Paket immer localhost – davor steht nginx. Eine LAN-Adresse aus einer älteren Installation stellt das Paket auf `127.0.0.1` um. |
| `AXES_FAILURE_LIMIT` | `5` | – | Fehlversuche bis zur Login-Sperre. |
| `AXES_COOLOFF_HOURS` | `0.25` | – | Dauer der Login-Sperre in Stunden (0.25 = 15 Minuten). |
| `AXES_PROXY_COUNT` | `0` | `1` (bei der ersten nginx-Einrichtung gesetzt, wenn leer oder `0`) | Anzahl Reverse Proxys vor gunicorn. Mit nginx davor `1`, damit die Login-Sperre die echte Client-IP sieht statt `127.0.0.1`. Ausgewertet wird der Wert über das Setting `REVERSE_PROXY_COUNT` von MultiGPTs eigener Client-IP-Funktion (`multigpt.accounts.client_ip`, eingebunden als `AXES_CLIENT_IP_CALLABLE`): Sie nimmt den n-letzten Eintrag aus `X-Forwarded-For`, alles davor kann der Client selbst gesetzt haben. Bei `0`, fehlendem oder ungültigem Header gilt `REMOTE_ADDR`. (django-axes selbst würde den Wert nur mit dem nicht installierten django-ipware auswerten.) |
| `USE_X_ACCEL_REDIRECT` | `False` | auskommentiert (`#USE_X_ACCEL_REDIRECT=True`) | Downloads hochgeladener Dokumente über nginx (`X-Accel-Redirect` auf die interne `location /_protected/media/`) statt über gunicorn. Nur mit nginx davor und Leserecht des nginx-Nutzers `www-data` auf `MEDIA_ROOT` – das ist im Paket noch **offen**, daher vorerst aus lassen (siehe [nginx und TLS](nginx-und-TLS)). Dateien aus Verzeichnisquellen liefert immer Django aus. |

## gunicorn

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `MULTI_GPT_WORKERS` | `2` | Prozesse. |
| `MULTI_GPT_THREADS` | `8` | Threads je Prozess. Jede laufende Antwort (Streaming) belegt einen Thread; ein Vergleich mit 3 Modellen belegt 3. |
| `MULTI_GPT_TIMEOUT` | `300` | Sekunden bis zum Abbruch einer hängenden Anfrage (lange Antworten, OCR-Test). |
| `MULTI_GPT_FORWARDED_ALLOW_IPS` | `127.0.0.1,::1` | Proxys, deren `X-Forwarded-Proto` gunicorn glaubt (nginx auf demselben Rechner). Im Paket nicht ändern; nur nötig, wenn z. B. unter Docker ein Proxy in einem anderen Container davorsteht. |

## Dokumente (RAG)

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `DOCUMENT_MAX_UPLOAD_MB` | `25` | Höchstgröße je Dokument (Upload und Verzeichnisquellen). nginx muss das zulassen: Das Paket setzt `client_max_body_size` auf diesen Wert + 10 MB; nach einer Änderung `sudo dpkg-reconfigure multi-gpt`. |
| `OCR_LANGUAGES` | `deu+eng` | Sprachen für Tesseract (Ersatz-OCR). |
| `JOB_MAX_ATTEMPTS` | `5` | Versuche je Indexierungsauftrag, danach Status „Fehler“. Wartezeiten wegen eines ausgeschalteten LM Studio zählen nicht mit. |
| `RAG_SOURCE_ROOTS` | leer (aus) | Erlaubte Wurzeln für [Verzeichnisquellen](RAG-Verzeichnisquellen), kommagetrennt, z. B. `/srv/nas/dokumente`. Leer = Funktion aus. |
| `RAG_SOURCE_MAX_FILES` | `5000` | Höchstzahl Dateien je Einlesevorgang einer Verzeichnisquelle. |

Embedding-Modell, Präfixe und OCR-Verfahren stehen **nicht** in der `.env`, sondern im Admin unter
„Dokumente (RAG)“ → „Einstellungen“ (siehe [RAG-Einrichtung](RAG-Einrichtung)). Die Websuche
(SearXNG-URL usw.) steht im Admin unter „Chat“ → „Sucheinstellungen“ (siehe [SearXNG](SearXNG)).

## Kosten

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `BILLING_ECB_FETCH` | `False` | EZB-Referenzkurs USD abrufen (Knopf im Admin, `mgpt-ctl fetch_ecb_rate`). Externer Abruf, daher standardmäßig aus; Kurse lassen sich immer von Hand pflegen. Siehe [Kosten und Budgets](Kosten-und-Budgets). |

## Nur bei der Installation

Diese Variablen stehen **nicht** in der `.env`, sondern werden in der Umgebung von apt/dpkg gesetzt,
z. B. `sudo MULTI_GPT_SKIP_NGINX=1 apt install ./multi-gpt_<version>_amd64.deb`.

| Variable | Bedeutung |
|---|---|
| `MULTI_GPT_SKIP_NGINX` | `1`: Das postinst aktiviert die nginx-Site nicht und lässt Debians Site `default` unverändert. Nutzt das Docker-Image (dort lauscht gunicorn über `MULTI_GPT_BIND` auf `0.0.0.0`). Auf einem normalen System ist MultiGPT so nicht erreichbar. |

## Nur für Entwicklung und Tests

Diese Schlüssel wirken nur mit `DEBUG=True` bzw. nur in der Entwicklung – im Betrieb nicht setzen.

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `DB_TEST_TEMPLATE` | leer | Vorlage für die Test-Datenbank (UTF-8 mit pgvector), z. B. `multigpt_template` aus `make db-create`. |
| `RAG_FAKE_EMBEDDINGS` | `False` | Schein-Embeddings ohne Anbieter (nur mit `DEBUG`). |
| `WEBSEARCH_ALLOW_PRIVATE` | `False` | Seitenabruf der Websuche auch ins Heimnetz erlauben (nur mit `DEBUG`, für Tests). |
| `MULTI_GPT_ENV_FILE` | – | Pfad einer alternativen Env-Datei; setzt `mgpt-ctl` automatisch auf `/etc/multi-gpt/.env`. |
