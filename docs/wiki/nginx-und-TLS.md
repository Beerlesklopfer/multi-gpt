# nginx und TLS

Im Debian-Paket lauscht gunicorn nur auf `127.0.0.1`. Erreichbar ist MultiGPT ausschließlich über
**nginx mit TLS** auf demselben Rechner – nginx ist eine Abhängigkeit des Pakets (`nginx (>= 1.25.1)`,
dazu `ssl-cert`), und das Paket bringt die Site-Konfiguration mit. Aufgerufen wird MultiGPT danach
unter `https://<hostname>/`.

Diese Seite beschreibt, was das Paket einrichtet, wie man Hostname und Zertifikat ändert und wie
man Fehler findet. Alle Schlüssel der `.env` stehen unter [Konfiguration](Konfiguration).

> **Stand:** umgesetzt seit Commit `d9c76a9`. Eine Testinstallation auf einem frischen Debian 13
> steht noch aus. Offen ist die Auslieferung von Dokumenten per X-Accel-Redirect (siehe unten).
> Docker und die Entwicklung (`make dev`, `make run`) sind davon nicht betroffen: Dort liefert die
> App ihre statischen Dateien selbst aus (WhiteNoise), ein nginx ist nicht nötig.

**Platzhalter**, die du ersetzen musst, stehen in spitzen Klammern, z. B. `<hostname>`.

## Überblick

```
Browser ──HTTPS (443)──> nginx ──HTTP──> gunicorn auf 127.0.0.1:<port> ──> Django
        ──HTTP (80)───> nginx: 301 auf https://
```

| Teil | Aufgabe |
|---|---|
| **HTTP (Port 80)** | leitet jede Anfrage mit 301 auf `https://` um |
| **HTTPS (Port 443)** | TLS 1.2/1.3 nach Mozilla „intermediate“, HTTP/2, Weiterleitung an gunicorn |
| **Statische Dateien** | liefert nginx direkt aus `/usr/share/python/multi-gpt/static/` (mit vorkomprimierten `.gz`-Dateien, gehashte Namen ein Jahr im Cache) |
| **Gestreamte Antworten (SSE)** | ohne Puffer, bis 300 s offen (passend zu `MULTI_GPT_TIMEOUT`) |
| **Uploads** | `client_max_body_size` = `DOCUMENT_MAX_UPLOAD_MB` + 10 MB |
| **Sicherheits-Header** | `X-Content-Type-Options`, `Referrer-Policy`; HSTS nur mit eigenem Zertifikat |

Ist IPv6 auf dem Rechner aktiv, lauscht nginx zusätzlich auf `[::]:80` und `[::]:443`.

## Was das Paket einrichtet

### Die Site (conffile)

`/etc/nginx/sites-available/multi-gpt` ist ein **conffile** des Pakets: Eigene Änderungen daran
bleiben bei Aktualisierungen erhalten; hat sich auch die Paketversion geändert, fragt dpkg nach.
Das postinst aktiviert die Site über die Verknüpfung `/etc/nginx/sites-enabled/multi-gpt`.
Ist `sites-enabled/multi-gpt` keine Verknüpfung, sondern eine Datei, bleibt sie unverändert.

Die Site enthält keine rechnerspezifischen Werte. Sie bindet Dateien aus `/etc/multi-gpt/nginx/`
ein.

### Erzeugte Dateien unter `/etc/multi-gpt/nginx/`

Diese Dateien erzeugt das postinst bei **jeder** Konfiguration (Installation, Upgrade,
`dpkg-reconfigure multi-gpt`) neu – aus den debconf-Antworten und aus `/etc/multi-gpt/.env`.
**Handänderungen daran gehen verloren.**

| Datei | Inhalt |
|---|---|
| `upstream.conf` | `upstream multi_gpt_app` mit `127.0.0.1:<port>` (Port aus `MULTI_GPT_BIND`) |
| `http.conf` | `listen 80` (ggf. mit `default_server`), `server_name` |
| `https.conf` | `listen 443 ssl` (ggf. mit `default_server`), `server_name`, Zertifikat und Schlüssel, `client_max_body_size`, interne `location /_protected/media/` für X-Accel-Redirect |
| `headers.conf` | Sicherheits-Header; `Strict-Transport-Security` (HSTS, 2 Jahre) nur mit eigenem Zertifikat |

### Eigene Ergänzungen: `/etc/multi-gpt/nginx/local/*.conf`

Alle Dateien `/etc/multi-gpt/nginx/local/*.conf` werden am Ende des HTTPS-`server`-Blocks
eingebunden und vom Paket **nie überschrieben**. Dorthin gehören eigene Direktiven, zum Beispiel
weitere `location`-Blöcke. Danach:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

> Achtung: `apt purge multi-gpt` löscht `/etc/multi-gpt/nginx/` vollständig, also auch `local/`.

### Änderungen an `/etc/multi-gpt/.env`

Bei jeder Konfiguration schreibt das postinst diese Zeilen der `.env` neu:

- `MULTI_GPT_BIND` = `127.0.0.1:<port>` – die Adresse ist immer `127.0.0.1`, der Port bleibt
  erhalten (Standard `8000`). Bei einem Upgrade von einer Version mit LAN-Bindung wird die
  Adresse umgestellt; das postinst meldet das.
- `ALLOWED_HOSTS` = `localhost`, `127.0.0.1`, die Hostnamen aus debconf und die IPv4-Adressen des
  Rechners (`hostname -I`, ohne Loopback). Aus `*.example.org` wird `.example.org`.
- `CSRF_TRUSTED_ORIGINS` = `https://<name>` für jeden Hostnamen und jede IP-Adresse.

Nur bei der **ersten** nginx-Einrichtung kommen hinzu: `SECURE_COOKIES=True` (wenn bisher leer
oder aus) und `AXES_PROXY_COUNT=1` (wenn bisher leer oder `0`). Spätere Änderungen an diesen beiden
Werten bleiben erhalten.

## Fragen bei der Installation (debconf)

| Frage | Bedeutung |
|---|---|
| **Hostname(n)** | Namen, unter denen MultiGPT aufgerufen wird (`server_name`). Mehrere mit Leerzeichen trennen, z. B. `nas.intranet.example nas`. Platzhalter wie `*.example.org` sind erlaubt. Vorschlag: `hostname -f` und der kurze Hostname; bei einer erneuten Abfrage die zuletzt eingerichteten Namen sowie Namen, die von Hand in `ALLOWED_HOSTS` stehen. Ungültige Namen werden mit Warnung ignoriert. |
| **Pfad zum TLS-Zertifikat** | PEM-Datei mit Zwischenzertifikaten, ausgestellt auf die Hostnamen, z. B. `/etc/ssl/certs/multigpt.pem`. **Leer lassen** = selbstsigniertes snakeoil-Zertifikat. |
| **Pfad zum privaten Schlüssel** | wird nur zu einem eigenen Zertifikat gefragt, z. B. `/etc/ssl/private/multigpt.key`. Nur für root lesbar; nginx liest ihn beim Start als root. |

Die IP-Adressen des Rechners muss man nicht angeben, sie werden automatisch zugelassen.

### snakeoil als Rückfall

Ohne eigenes Zertifikat – oder wenn Zertifikat oder Schlüssel fehlen bzw. der Pfad ungültig ist –
verwendet nginx das selbstsignierte Zertifikat aus dem Paket `ssl-cert`
(`/etc/ssl/certs/ssl-cert-snakeoil.pem`, Schlüssel `/etc/ssl/private/ssl-cert-snakeoil.key`).
Fehlt es, erzeugt das postinst es mit `make-ssl-cert generate-default-snakeoil`. Die Installation
läuft weiter, gibt aber eine Warnung aus. Folgen:

- Browser warnen vor der Verbindung.
- HSTS bleibt aus.
- Das **Mikrofon** ist ggf. eingeschränkt, siehe [Mikrofon](#mikrofon-braucht-ein-vertrauenswürdiges-zertifikat).

## Standard-Server (default_server)

Die MultiGPT-Site ist für die Ports 80 und 443 der **Standard-Server** (`default_server`), solange
keine andere nginx-Site das schon ist. Dann antwortet sie auch auf Aufrufe per IP-Adresse oder
unter einem unbekannten Namen. Gesucht wird in `/etc/nginx/nginx.conf`, `/etc/nginx/conf.d/*.conf`
und `/etc/nginx/sites-enabled/*`.

**Debians Site `default`** setzt ebenfalls `default_server`. Das Paket schaltet sie nur ab, wenn
alle Bedingungen erfüllt sind:

- `/etc/nginx/sites-enabled/default` ist eine Verknüpfung auf `/etc/nginx/sites-available/default`,
- die Datei ist **unverändert** (md5-Summe wie von dpkg für das nginx-Paket registriert),
- das Paket hat sie nicht schon einmal abgeschaltet (Vermerk
  `/var/lib/multi-gpt/nginx-default-site-disabled`).

Die Datei selbst wird nie verändert, nur die Verknüpfung entfernt. Ist die Datei angepasst, bleibt
die Site `default` aktiv und das postinst meldet das. Hat der Verwalter sie nach dem Abschalten
wieder aktiviert, bleibt es dabei.

Hat eine andere Site auf einem Port schon `default_server`, setzt MultiGPT dort keins und gibt
einen Hinweis aus: Aufrufe per IP-Adresse oder unbekanntem Namen landen dann bei dieser anderen
Site, MultiGPT ist nur unter seinen Hostnamen erreichbar.

### Rückweg: Debians Site `default` wieder aktivieren

```bash
sudo ln -s /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default
sudo dpkg-reconfigure multi-gpt
```

`dpkg-reconfigure` erzeugt die Konfiguration neu; MultiGPT ist danach nicht mehr `default_server`.
Bei `apt remove` und `apt purge` stellt das Paket die Site `default` selbst wieder her, wenn es sie
abgeschaltet hatte – außer `nginx -t` schlägt damit fehl.

## Eigenes Zertifikat einspielen

1. Zertifikat (mit Zwischenzertifikaten) und Schlüssel auf den Rechner kopieren, z. B. nach
   `/etc/ssl/certs/multigpt.pem` und `/etc/ssl/private/multigpt.key`. Der Schlüssel soll nur für
   root lesbar sein.
2. Die Pfade eintragen:

   ```bash
   sudo dpkg-reconfigure multi-gpt
   ```

3. Das postinst prüft, ob beide Dateien existieren, prüft die Konfiguration mit `nginx -t` und lädt
   nginx neu. Mit eigenem Zertifikat ist HSTS eingeschaltet.

Zurück zu snakeoil: bei der Frage nach dem Zertifikat das Feld leeren.

## Hostname oder IP-Adresse ändern

```bash
sudo dpkg-reconfigure multi-gpt
```

Das fragt die Hostnamen erneut ab, ermittelt die aktuellen IP-Adressen des Rechners, schreibt die
Dateien unter `/etc/multi-gpt/nginx/` sowie `ALLOWED_HOSTS` und `CSRF_TRUSTED_ORIGINS` in der
`.env` neu, lädt nginx neu und startet `multi-gpt` und `multi-gpt-worker` neu.

Die IP-Adressen werden nur bei einer Konfiguration ermittelt. **Nach einer Änderung der IP-Adresse**
(z. B. neue DHCP-Adresse) deshalb ebenfalls `dpkg-reconfigure multi-gpt` ausführen, sonst lehnt
Django Aufrufe unter der neuen Adresse ab (`ALLOWED_HOSTS`) bzw. die Anmeldung scheitert an der
CSRF-Prüfung.

Handänderungen an `CSRF_TRUSTED_ORIGINS` und am Hostteil von `MULTI_GPT_BIND` gehen dabei verloren;
von Hand in `ALLOWED_HOSTS` eingetragene Namen erscheinen als Vorschlag in der Hostnamen-Frage.

## Upload-Grenze

nginx lässt Anfragen bis `DOCUMENT_MAX_UPLOAD_MB` + 10 MB zu (Puffer für den Rest des
Formulars). Nach einer Änderung von `DOCUMENT_MAX_UPLOAD_MB` in `/etc/multi-gpt/.env`:

```bash
sudo dpkg-reconfigure multi-gpt
```

Das setzt `client_max_body_size` neu und startet die Dienste neu.

## Fehlersuche

| Prüfung | Befehl |
|---|---|
| nginx-Konfiguration gültig? | `sudo nginx -t` |
| läuft nginx? | `systemctl status nginx` |
| Log von nginx | `journalctl -u nginx` |
| Log von MultiGPT | `journalctl -u multi-gpt` |
| antwortet gunicorn direkt? | `curl http://127.0.0.1:8000/healthz/` (Port aus `MULTI_GPT_BIND`) |
| antwortet MultiGPT über nginx? | `curl -k https://<hostname>/healthz/` (`-k` nur mit snakeoil) |

### `nginx -t` schlägt bei der Installation fehl

Das postinst prüft die neue Konfiguration mit `nginx -t`, bevor es nginx neu lädt. Schlägt das fehl,
bricht die Installation **nicht** ab. Stattdessen:

- Die vorigen Dateien unter `/etc/multi-gpt/nginx/` werden wiederhergestellt; war die Site neu,
  wird sie nicht aktiviert, und eine in diesem Lauf abgeschaltete Site `default` kommt zurück.
- Die fehlerhaften Dateien bleiben zur Ansicht als **`/etc/multi-gpt/nginx/*.failed`** liegen.
- Die Ausgabe von `nginx -t` steht in der Meldung des postinst. Sie sagt auch, wenn `nginx -t`
  schon ohne die MultiGPT-Änderung fehlschlägt – dann liegt der Fehler in der übrigen
  nginx-Konfiguration.

Nach der Behebung erneut einrichten:

```bash
sudo dpkg-reconfigure multi-gpt
```

### nginx läuft nicht

Läuft nginx bei der Installation nicht, meldet das postinst
„nginx läuft nicht – starten mit: sudo systemctl start nginx“.

## Dokument-Downloads per X-Accel-Redirect (offen)

Mit `USE_X_ACCEL_REDIRECT=True` in `/etc/multi-gpt/.env` prüft Django beim Download eines
hochgeladenen Dokuments die Rechte und überlässt die Auslieferung der Datei nginx (Header
`X-Accel-Redirect` auf die interne `location /_protected/media/`, die auf `MEDIA_ROOT` zeigt). Die
`location` erzeugt das Paket in `https.conf`. Dateien aus [Verzeichnisquellen](RAG-Verzeichnisquellen)
liefert weiterhin Django aus. Standard: aus.

> **Offen:** nginx läuft als `www-data` und braucht Leserecht auf `MEDIA_ROOT`. Das Paket legt
> `/var/lib/multi-gpt` und `/var/lib/multi-gpt/media` aber mit `0750` für `multi-gpt:multi-gpt` an –
> `www-data` kann die Dateien dort nicht lesen. Eine Lösung im Paket gibt es noch nicht. Den
> Schalter deshalb vorerst **nicht** einschalten; ohne ihn liefert gunicorn die Dateien aus.

## Ohne nginx: `MULTI_GPT_SKIP_NGINX` (Docker)

Mit `MULTI_GPT_SKIP_NGINX=1` in der Umgebung von apt/dpkg aktiviert das postinst die nginx-Site
nicht und lässt Debians Site `default` in Ruhe:

```bash
sudo MULTI_GPT_SKIP_NGINX=1 apt install ./multi-gpt_<version>_amd64.deb
```

Das Docker-Image nutzt das: nginx wird als Abhängigkeit mitinstalliert, aber nicht verwendet;
gunicorn lauscht im Container über `MULTI_GPT_BIND=0.0.0.0:8000` (aus `compose.yaml`). Steht dort
ein Proxy in einem anderen Container davor, muss `MULTI_GPT_FORWARDED_ALLOW_IPS` dessen Adresse
enthalten (siehe [Konfiguration](Konfiguration)). Auf einem normalen Debian-System ist MultiGPT
ohne nginx nicht erreichbar.

## Mikrofon braucht ein vertrauenswürdiges Zertifikat

Browser geben das Mikrofon nur über HTTPS frei. Mit dem selbstsignierten snakeoil-Zertifikat reicht
das **nicht zuverlässig**: Auch nach dem Bestätigen der Zertifikatswarnung kann der Zugriff
eingeschränkt sein. Für die Sprachfunktionen deshalb ein Zertifikat einspielen, dem die Geräte im
Heimnetz vertrauen (siehe [Eigenes Zertifikat einspielen](#eigenes-zertifikat-einspielen)).

## Paket entfernen

- `apt remove multi-gpt`: deaktiviert die Site (`sites-enabled/multi-gpt`), stellt ggf. Debians Site
  `default` wieder her und lädt nginx neu, wenn `nginx -t` erfolgreich ist.
- `apt purge multi-gpt`: zusätzlich werden `/etc/multi-gpt/nginx/` (mit `local/`), die `.env` und
  die Site `/etc/nginx/sites-available/multi-gpt` entfernt. Daten unter `/var/lib/multi-gpt` und die
  Datenbank bleiben.
