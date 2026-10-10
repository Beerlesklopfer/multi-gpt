# Variante b) SearXNG nativ auf Debian 13 „trixie“

Diese Anleitung installiert SearXNG ohne Docker mit dem **offiziellen Installationsskript**
`utils/searxng.sh`. Das Skript richtet ein, was die SearXNG-Doku als Referenz beschreibt: Systemnutzer
`searxng`, Python-venv, `/etc/searxng/settings.yml`, uWSGI aus Debian als Dienst und auf Wunsch Valkey
aus Debian. Allgemeines zu JSON, Limiter und Firewall steht im [Überblick](SearXNG).

> **Granian statt uWSGI?** Die SearXNG-Doku nennt Granian den künftigen Nachfolger von uWSGI, sagt
> aber, dass Granian derzeit nur im Container offiziell unterstützt ist. Für die native Installation
> bleibt diese Anleitung deshalb bei uWSGI, wie es das Skript einrichtet.

**Platzhalter**, die du ersetzen musst:

| Platzhalter | Bedeutung | Beispiel |
|---|---|---|
| `<LAN-IP>` | IPv4-Adresse des Rechners, auf dem SearXNG läuft | `192.168.24.251` |
| `192.168.24.250` | IP des MultiGPT-Rechners, an die eigene anpassen | |

Läuft SearXNG **auf demselben Rechner wie MultiGPT**, bleibt es bei der Grundeinstellung
`127.0.0.1:8888`. Dann entfallen Schritt 5 und 6, und die URL in MultiGPT ist `http://127.0.0.1:8888`.

## Was das Skript installiert

| Was | Wo |
|---|---|
| Pakete aus Debian | `python3-dev python3-babel python3-venv python-is-python3 uwsgi uwsgi-plugin-python3 git build-essential libxslt-dev zlib1g-dev libffi-dev libssl-dev` |
| Systemnutzer | `searxng`, Home `/usr/local/searxng` |
| Quellcode | `/usr/local/searxng/searxng-src` (git-Klon) |
| Python-venv | `/usr/local/searxng/searx-pyenv` |
| Einstellungen | `/etc/searxng/settings.yml`, mit zufälligem `secret_key` |
| uWSGI-App | `/etc/uwsgi/apps-available/searxng.ini`, verlinkt nach `/etc/uwsgi/apps-enabled/` |
| Valkey (auf Nachfrage) | Debian-Paket `valkey-server`, Dienst `valkey-server` |

## 1. System aktualisieren und Skript holen

Die Doku verlangt, das System vorher zu aktualisieren:

```sh
sudo apt update
sudo apt full-upgrade
sudo apt install git curl wget sudo openssl
```

Der Klon wird nur für die Installation und spätere Wartung (Update) gebraucht. Er bleibt deshalb
liegen:

```sh
mkdir -p ~/src
cd ~/src
git clone https://github.com/searxng/searxng.git searxng
cd searxng
```

## 2. Installieren

In der Grundeinstellung richtet das Skript uWSGI mit einem **Unix-Socket** ein, an den man einen
Webserver (nginx oder Apache) hängt. MultiGPT spricht SearXNG aber direkt per HTTP an. Mit
`SEARXNG_UWSGI_USE_SOCKET=false` lauscht uWSGI stattdessen per HTTP auf `server.bind_address` und
`server.port`, in der Grundeinstellung also `127.0.0.1:8888`:

```sh
cd ~/src/searxng
sudo -H env SEARXNG_UWSGI_USE_SOCKET=false ./utils/searxng.sh install all
```

Das Skript arbeitet interaktiv und fragt mehrmals nach:

- **Valkey installieren?** Mit **Ja** antworten. Valkey ist nur für den Limiter nötig, schadet aber
  nicht und hält Schritt 7 offen. Das Skript installiert dann das Debian-Paket `valkey-server`.
  Dabei kommentiert es in der Unit-Datei des Pakets die Zeile `PrivateUsers=true` aus.
- **Reverse Proxy installieren?** Diese Frage kommt nur, wenn nginx oder Apache schon installiert
  ist, zum Beispiel weil MultiGPT auf demselben Rechner hinter nginx läuft. Mit **Nein** antworten,
  MultiGPT braucht keinen Proxy vor SearXNG.
- **Checks ausführen?** Mit **Ja** antworten.

Prüfen, ob uWSGI per HTTP lauscht:

```sh
grep '^http' /etc/uwsgi/apps-available/searxng.ini
sudo ss -ltnp | grep 8888
```

Erwartet wird `http = 127.0.0.1:8888`.

## 3. `settings.yml` anpassen

```sh
sudo nano /etc/searxng/settings.yml
```

Die vom Skript angelegte Datei enthält unter anderem `limiter: true` und nur `html` als Format. So
müssen die Abschnitte aussehen (der Wert von `secret_key` bleibt, wie das Skript ihn erzeugt hat):

```yaml
use_default_settings: true

search:
  safe_search: 2
  autocomplete: 'duckduckgo'
  formats:
    - html
    - json

server:
  secret_key: "…vom Skript erzeugt, nicht ändern…"
  # Bot-Schutz aus, weil nur der MultiGPT-Rechner zugreift (sonst Schritt 7)
  limiter: false
  image_proxy: true

valkey:
  url: valkey://localhost:6379/0
```

Prüfen, dass kein Platzhalter-Schlüssel mehr drinsteht (die Ausgabe muss leer sein):

```sh
sudo grep ultrasecretkey /etc/searxng/settings.yml
```

Steht dort doch noch `ultrasecretkey`, einen Schlüssel setzen:

```sh
sudo sed -i "s/ultrasecretkey/$(openssl rand -hex 32)/" /etc/searxng/settings.yml
```

Rechte der Datei einschränken, denn sie enthält den Schlüssel:

```sh
sudo chown searxng:searxng /etc/searxng/settings.yml
sudo chmod 640 /etc/searxng/settings.yml
```

## 4. Neu starten und testen

Debian startet uWSGI über ein klassisches Init-Skript. Die SearXNG-Doku nennt dafür diese Befehle:

```sh
sudo -H service uwsgi restart searxng
sudo -H service uwsgi status searxng
```

Prüfen, dass der Dienst beim Booten startet:

```sh
systemctl is-enabled uwsgi
```

Steht dort nicht `enabled`, mit `sudo systemctl enable uwsgi` einschalten.

Test (auf dem SearXNG-Rechner):

```sh
curl -s 'http://127.0.0.1:8888/search?q=test&format=json' | head -c 400; echo
```

Fehlermeldungen von uWSGI stehen bei Debian unter `/var/log/uwsgi/app/searxng.log`. Das Skript bietet
außerdem einen Prüf- und Diagnosebefehl:

```sh
cd ~/src/searxng
sudo -H ./utils/searxng.sh instance check
sudo -H ./utils/searxng.sh instance inspect
```

## 5. An der LAN-IP lauschen (nur wenn MultiGPT auf einem anderen Rechner läuft)

```sh
sudo sed -i 's|^http = .*|http = <LAN-IP>:8888|' /etc/uwsgi/apps-available/searxng.ini
grep '^http' /etc/uwsgi/apps-available/searxng.ini
sudo -H service uwsgi restart searxng
```

Nie `0.0.0.0` eintragen. Das Update in Schritt 8 lässt diese Datei unverändert.

## 6. Firewall: nur der MultiGPT-Rechner darf zugreifen

Nur nötig, wenn SearXNG an einer `<LAN-IP>` lauscht (Schritt 5). Wähle **eine** der beiden Lösungen,
je nachdem, was auf dem Rechner schon im Einsatz ist.

**Mit nftables** (Debian-Standard, wenn sonst keine Firewall-Software läuft). Die Datei
`/etc/nftables.conf` beginnt bei Debian mit `flush ruleset`. Sie ersetzt beim Laden also alle Regeln.
Wer schon `ufw`, `firewalld` oder Docker auf dem Rechner hat, nimmt besser den zweiten Weg oder die
vorhandene Lösung.

```sh
sudo apt install nftables
sudo tee -a /etc/nftables.conf <<'EOF'

# SearXNG: Port 8888 nur für den MultiGPT-Rechner
table inet searxng {
	chain input {
		type filter hook input priority 0; policy accept;
		iifname != "lo" tcp dport 8888 ip saddr != 192.168.24.250 drop
		iifname != "lo" tcp dport 8888 meta nfproto ipv6 drop
	}
}
EOF
sudo nft -c -f /etc/nftables.conf
sudo systemctl enable --now nftables
sudo systemctl restart nftables
sudo nft list table inet searxng
```

**Mit ufw** (wenn ufw schon aktiv ist). Die Reihenfolge ist wichtig, die erlaubende Regel muss vor
der sperrenden stehen:

```sh
sudo ufw allow from 192.168.24.250 to any port 8888 proto tcp
sudo ufw deny 8888/tcp
sudo ufw status numbered
```

Prüfen: Von einem dritten Rechner im Heimnetz darf `curl -m 5 'http://<LAN-IP>:8888/'` keine Antwort
bekommen. Vom MultiGPT-Rechner aus muss
`curl -s 'http://<LAN-IP>:8888/search?q=test&format=json' | head -c 400` JSON liefern.

Zusätzlich gilt: **Im Router keine Portweiterleitung** auf diesen Rechner und Port.

## 7. Limiter einschalten (optional)

Die Vorlage des Skripts schaltet den Limiter ein. In Schritt 3 haben wir ihn ausgeschaltet. Wer ihn
behalten möchte, setzt `limiter: true` und gibt **nur** den MultiGPT-Rechner frei. Läuft MultiGPT auf
demselben Rechner und fragt `http://127.0.0.1:8888` ab, ist das `127.0.0.1`.

```sh
sudo tee /etc/searxng/limiter.toml <<'EOF'
# Doku: https://docs.searxng.org/admin/searx.limiter.html
[botdetection.ip_lists]
pass_ip = [
  '192.168.24.250',  # nur der MultiGPT-Rechner (bzw. '127.0.0.1' auf demselben Rechner)
]
EOF
sudo sed -i 's/^  limiter: false/  limiter: true/' /etc/searxng/settings.yml
sudo -H service uwsgi restart searxng
```

SearXNG ergänzt die Datei mit seinen Standardwerten, man muss nur die Abweichung eintragen. Der
Limiter braucht Valkey (`valkey.url` in der `settings.yml`, Dienst `valkey-server`):

```sh
systemctl status valkey-server --no-pager
```

Das Debian-Paket lässt Valkey in der Grundeinstellung nur lokal lauschen
(`bind 127.0.0.1 -::1` in `/etc/valkey/valkey.conf`). Daran nichts ändern.

## 8. Update

SearXNG selbst (git und pip im venv) wird über das Skript aktualisiert. Erst den Klon aktualisieren,
dann die Instanz:

```sh
cd ~/src/searxng
git pull
sudo -H ./utils/searxng.sh instance update
```

Bei der Frage zur `settings.yml` **„leave file unchanged“** wählen (das ist die Vorgabe). Sonst gehen
die eigenen Einstellungen verloren. Das Skript startet uWSGI danach selbst neu. Anschließend:

```sh
sudo -H ./utils/searxng.sh instance check
```

uWSGI, Valkey und die übrigen Pakete kommen über die normalen Debian-Updates
(`sudo apt update && sudo apt upgrade`). Nach jedem Update in MultiGPT **„SearXNG testen“** klicken.
SearXNG ändert sich schnell. Vor großen Sprüngen lohnt ein Blick in die Hinweise zur Migration in der
SearXNG-Doku.

## 9. In MultiGPT eintragen

Admin → **Chat** → **Sucheinstellungen** → **SearXNG-URL** `http://<LAN-IP>:8888` (bzw.
`http://127.0.0.1:8888`) → Speichern → **„SearXNG testen“**. Details und Fehlersuche stehen im
[Überblick](SearXNG#in-multigpt-eintragen).

## Deinstallieren

```sh
cd ~/src/searxng
sudo -H ./utils/searxng.sh remove all
```

## Quellen

Gelesen am 2026-10-09:

- <https://docs.searxng.org/admin/installation.html>
- <https://docs.searxng.org/admin/installation-scripts.html>
- <https://docs.searxng.org/admin/installation-searxng.html>
- <https://docs.searxng.org/admin/installation-uwsgi.html>
- <https://docs.searxng.org/admin/installation-granian.html>
- <https://docs.searxng.org/admin/update-searxng.html>
- <https://docs.searxng.org/admin/settings/settings_server.html>
- <https://docs.searxng.org/admin/settings/settings_search.html>
- <https://docs.searxng.org/admin/settings/settings_valkey.html>
- <https://docs.searxng.org/admin/searx.limiter.html>
- Skript und Vorlagen (Stand `master`; die Doku beschreibt den HTTP-Modus und die Valkey-Installation
  nicht, das steht nur im Skript):
  <https://github.com/searxng/searxng/blob/master/utils/searxng.sh>,
  <https://github.com/searxng/searxng/blob/master/utils/lib.sh>,
  <https://github.com/searxng/searxng/blob/master/utils/lib_valkey.sh>,
  <https://github.com/searxng/searxng/blob/master/utils/templates/etc/searxng/settings.yml>,
  <https://github.com/searxng/searxng/blob/master/utils/templates/etc/uwsgi/apps-available/searxng.ini>
