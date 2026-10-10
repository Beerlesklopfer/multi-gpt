# Variante a) SearXNG mit Docker (Debian 13)

Diese Anleitung richtet SearXNG mit dem offiziellen Compose-Setup des SearXNG-Projekts ein. Es
startet zwei Container: `searxng-core` (Image `docker.io/searxng/searxng`, Webserver Granian) und
`searxng-valkey` (Image `docker.io/valkey/valkey:9-alpine`, Datenbank für den Bot-Schutz). Allgemeines
zu JSON, Limiter und Firewall steht im [Überblick](SearXNG).

**Platzhalter**, die du ersetzen musst:

| Platzhalter | Bedeutung | Beispiel |
|---|---|---|
| `<LAN-IP>` | IPv4-Adresse des Rechners, auf dem SearXNG läuft | `192.168.24.251` |
| `<LAN-IF>` | Netzwerkschnittstelle dieses Rechners zum Heimnetz (`ip -br addr`) | `enp3s0` |
| `192.168.24.250` | IP des MultiGPT-Rechners, an die eigene anpassen | |

Wenn SearXNG **auf demselben Rechner wie MultiGPT** läuft, nimm statt `<LAN-IP>` überall
`127.0.0.1`. Dann ist SearXNG von anderen Rechnern gar nicht erreichbar, und Schritt 6 entfällt.

## 1. Docker und Compose installieren

Zwei Wege, beide funktionieren mit dieser Anleitung. Man wählt **einen** davon.

**Weg 1: Pakete aus Debian 13** (einfach, Updates über Debian; Stand trixie: Docker 26.1,
Compose 2.26):

```sh
sudo apt update
sudo apt install docker.io docker-compose
```

Das Debian-Paket `docker-compose` bringt Compose v2 mit, der Befehl `docker compose` funktioniert
damit (ebenso `docker-compose`).

**Weg 2: Paketquelle von Docker** (neuere Versionen), nach
<https://docs.docker.com/engine/install/debian/>, die Debian 13 trixie unterstützt:

```sh
sudo apt update
sudo apt install ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update
sudo apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Prüfen:

```sh
sudo docker version
sudo docker compose version
```

Die SearXNG-Doku schlägt vor, den eigenen Nutzer in die Gruppe `docker` aufzunehmen
(`sudo usermod -aG docker $USER`, danach neu anmelden). Das ist bequem, aber Mitglieder der Gruppe
`docker` haben faktisch Root-Rechte. Diese Anleitung verwendet deshalb `sudo docker …`.

## 2. Projektordner und Compose-Datei

Der Ordner liegt hier unter `/opt/searxng`. Jeder andere Ort geht auch, alle folgenden Befehle laufen
dann in diesem Ordner.

```sh
sudo mkdir -p /opt/searxng/core-config
cd /opt/searxng
sudo curl -fsSL \
  -O https://raw.githubusercontent.com/searxng/searxng/master/container/docker-compose.yml \
  -O https://raw.githubusercontent.com/searxng/searxng/master/container/.env.example
sudo cp -i .env.example .env
```

Die heruntergeladene `docker-compose.yml` (Stand Oktober 2026) sieht so aus. Du musst sie nicht
ändern:

```yaml
name: searxng

services:
  core:
    container_name: searxng-core
    image: docker.io/searxng/searxng:${SEARXNG_VERSION:-latest}
    restart: always
    ports:
      - ${SEARXNG_HOST:+${SEARXNG_HOST}:}${SEARXNG_PORT:-8080}:${SEARXNG_PORT:-8080}
    env_file: ./.env
    volumes:
      - ./core-config/:/etc/searxng/:Z
      - core-data:/var/cache/searxng/

  valkey:
    container_name: searxng-valkey
    image: docker.io/valkey/valkey:9-alpine
    command: valkey-server --save 30 1 --loglevel warning
    restart: always
    volumes:
      - valkey-data:/data/

volumes:
  core-data:
  valkey-data:
```

Der Ordner `core-config/` wird im Container zu `/etc/searxng/`. Dort liegen `settings.yml` und
`limiter.toml`.

## 3. Port nur an LAN-IP oder 127.0.0.1 binden (`.env`)

Ohne `SEARXNG_HOST` veröffentlicht Docker den Port an **allen** Adressen des Rechners. Deshalb in der
`.env` die Adresse festlegen:

```sh
sudo tee -a /opt/searxng/.env <<'EOF'

# MultiGPT: nur an dieser Adresse lauschen (127.0.0.1, wenn MultiGPT auf demselben Rechner läuft)
SEARXNG_HOST=<LAN-IP>
SEARXNG_PORT=8080
EOF
sudo nano /opt/searxng/.env   # <LAN-IP> ersetzen
```

Optional lässt sich mit `SEARXNG_VERSION=<Tag>` eine feste Image-Version setzen, statt `latest` zu
verwenden. Gültige Tags stehen auf <https://hub.docker.com/r/searxng/searxng/tags>.

Kontrolle, ob die Adresse ankommt:

```sh
cd /opt/searxng
sudo docker compose config | grep -A5 'ports:'
```

Die Ausgabe muss `host_ip: <deine LAN-IP>` (bzw. `127.0.0.1`) enthalten.

## 4. `settings.yml` mit Schlüssel und JSON anlegen

Beim ersten Start legt der Container eine `settings.yml` aus einer Vorlage an, falls keine da ist, und
setzt dabei einen zufälligen Schlüssel. Wir legen die Datei vorher selbst an. So stehen JSON-Format und
Valkey gleich drin:

```sh
sudo tee /opt/searxng/core-config/settings.yml <<'EOF'
# Doku: https://docs.searxng.org/admin/settings/
use_default_settings: true

server:
  # wird im nächsten Schritt durch einen Zufallswert ersetzt
  secret_key: "ultrasecretkey"
  # Bot-Schutz: aus, weil nur der MultiGPT-Rechner zugreift (siehe Abschnitt 7)
  limiter: false
  image_proxy: true

search:
  formats:
    - html
    - json

valkey:
  url: valkey://searxng-valkey:6379/0
EOF
sudo sed -i "s/ultrasecretkey/$(openssl rand -hex 32)/" /opt/searxng/core-config/settings.yml
sudo chmod 600 /opt/searxng/core-config/settings.yml
```

Prüfen, dass der Platzhalter weg ist (die Ausgabe muss leer sein):

```sh
sudo grep ultrasecretkey /opt/searxng/core-config/settings.yml
```

`use_default_settings: true` übernimmt alle übrigen Einstellungen aus den SearXNG-Standards. Man
trägt nur ein, was abweicht. Der Container setzt beim Start Besitzer `searxng:searxng` auf die
eingebundenen Ordner (`FORCE_OWNERSHIP`, Standard `true`). Danach gehört die Datei auf dem Rechner
einer numerischen UID. Das ist so gewollt, bearbeiten kann man sie weiterhin mit `sudo`.

## 5. Starten und testen

```sh
cd /opt/searxng
sudo docker compose up -d
sudo docker compose ps
sudo docker compose logs -f core     # beenden mit Strg+C
```

Test auf dem SearXNG-Rechner:

```sh
curl -s 'http://<LAN-IP>:8080/search?q=test&format=json' | head -c 400; echo
```

Danach denselben Befehl auf dem **MultiGPT-Rechner** ausführen (siehe [Überblick → Test](SearXNG#test-mit-curl)).

## 6. Firewall: nur der MultiGPT-Rechner darf zugreifen

Nur nötig, wenn SearXNG an einer `<LAN-IP>` lauscht, also auf einem anderen Rechner als MultiGPT.

**Wichtig:** Ports, die Docker veröffentlicht, gehen an `ufw` und an eigenen Regeln in der
INPUT-Kette vorbei. Die Docker-Doku sagt außerdem, dass mit `nft` angelegte Regeln auf einem Rechner
mit Docker nicht unterstützt werden. Eigene Regeln gehören mit `iptables` in die Kette `DOCKER-USER`.
Diese Regel verwirft alle Verbindungen zum SearXNG-Port, die von außen kommen und nicht vom
MultiGPT-Rechner stammen:

```sh
sudo iptables -I DOCKER-USER -i <LAN-IF> -p tcp -m conntrack --ctorigdstport 8080 ! -s 192.168.24.250 -j DROP
sudo iptables -L DOCKER-USER -n -v
```

Diese Regel ist nach einem Neustart weg. Dauerhaft speichern lässt sie sich zum Beispiel mit dem
Debian-Paket `iptables-persistent`. Nach dem Anlegen der Regel:

```sh
sudo apt install iptables-persistent
sudo netfilter-persistent save
```

Prüfen: Von einem dritten Rechner im Heimnetz darf
`curl -m 5 'http://<LAN-IP>:8080/'` keine Antwort bekommen (Zeitüberschreitung). Vom MultiGPT-Rechner
aus muss der Test aus Schritt 5 funktionieren.

> Die Kombination aus `--ctorigdstport` und Quelladresse ist aus den Beispielen der Docker-Doku
> zusammengesetzt. Sie ist keine Vorgabe der SearXNG-Doku. Wer schon eine eigene Firewall-Lösung mit
> Docker betreibt, setzt die Regel dort sinngemäß um.

Zusätzlich gilt: **Im Router keine Portweiterleitung** auf diesen Rechner und Port.

## 7. Limiter einschalten (optional)

Bleibt SearXNG nur vom MultiGPT-Rechner erreichbar, reicht `limiter: false`. Wer den Bot-Schutz
trotzdem möchte, etwa weil auch Browser im Heimnetz SearXNG direkt nutzen, gibt den MultiGPT-Rechner
frei:

```sh
sudo sed -i 's/^  limiter: false/  limiter: true/' /opt/searxng/core-config/settings.yml
sudo tee /opt/searxng/core-config/limiter.toml <<'EOF'
# Doku: https://docs.searxng.org/admin/searx.limiter.html
[botdetection.ip_lists]
pass_ip = [
  '192.168.24.250',  # nur der MultiGPT-Rechner
]
EOF
cd /opt/searxng
sudo docker compose restart core
```

SearXNG ergänzt die Datei mit seinen Standardwerten, man muss nur die Abweichung eintragen.

**Hinweis zur Quell-IP:** `pass_ip` vergleicht die IP, die im Container ankommt. Kommt die Anfrage von
einem anderen Rechner, ist das dessen echte IP. Fragt MultiGPT dagegen auf **demselben Rechner** über
`127.0.0.1` an, sieht der Container in der Regel nicht `127.0.0.1`, sondern die Gateway-Adresse des
Docker-Netzes, weil Docker solche Verbindungen über seinen Proxy weiterleitet. Diese Adresse zeigt:

```sh
sudo docker network inspect searxng_default --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}'
```

Trägt man sie in `pass_ip` ein, ist allerdings jeder Prozess auf diesem Rechner freigegeben. In diesem
Fall ist `limiter: false` die ehrlichere und einfachere Lösung. Gesperrte Anfragen protokolliert
SearXNG nur auf Debug-Ebene. Ob der Limiter zuschlägt, sieht man am einfachsten an der Antwort 429
beim Test.

## 8. Einstellungen ändern

Nach jeder Änderung an `settings.yml` oder `limiter.toml`:

```sh
cd /opt/searxng
sudo docker compose restart core
```

Ändert sich `.env` (z. B. Adresse oder Port), den Container neu erzeugen:

```sh
cd /opt/searxng
sudo docker compose up -d
```

## 9. Update

Images aktualisieren (laut SearXNG-Doku):

```sh
cd /opt/searxng
sudo docker compose down
sudo docker compose pull
sudo docker compose up -d
```

Gelegentlich ändert das Projekt auch die Compose-Vorlage. Dann vorher die Dateien neu laden. Die eigene
`.env` bleibt dabei erhalten, weil nur `.env.example` überschrieben wird:

```sh
cd /opt/searxng
sudo docker compose down
sudo curl -fsSL \
  -O https://raw.githubusercontent.com/searxng/searxng/master/container/docker-compose.yml \
  -O https://raw.githubusercontent.com/searxng/searxng/master/container/.env.example
sudo diff .env.example .env
sudo docker compose up -d
```

Nach dem Update in MultiGPT **„SearXNG testen“** klicken. SearXNG ändert sich schnell. Vor großen
Sprüngen lohnt ein Blick in die Hinweise zur Migration in der SearXNG-Doku.

Alte Images entfernen (optional):

```sh
sudo docker image prune
```

## 10. In MultiGPT eintragen

Admin → **Chat** → **Sucheinstellungen** → **SearXNG-URL** `http://<LAN-IP>:8080` (bzw.
`http://127.0.0.1:8080`) → Speichern → **„SearXNG testen“**. Details und Fehlersuche stehen im
[Überblick](SearXNG#in-multigpt-eintragen).

## Quellen

Gelesen am 2026-10-09:

- <https://docs.searxng.org/admin/installation.html>
- <https://docs.searxng.org/admin/installation-docker.html>
- <https://docs.searxng.org/admin/installation-granian.html>
- <https://docs.searxng.org/admin/settings/settings_server.html>
- <https://docs.searxng.org/admin/settings/settings_search.html>
- <https://docs.searxng.org/admin/settings/settings_valkey.html>
- <https://docs.searxng.org/admin/searx.limiter.html>
- <https://github.com/searxng/searxng/blob/master/container/docker-compose.yml>
- <https://github.com/searxng/searxng/blob/master/container/.env.example>
- <https://github.com/searxng/searxng/blob/master/container/entrypoint.sh> (Anlage der `settings.yml`, `FORCE_OWNERSHIP`)
- <https://github.com/searxng/searxng/blob/master/container/settings.template.yml>
- <https://docs.docker.com/engine/install/debian/>
- <https://docs.docker.com/engine/network/packet-filtering-firewalls/>
- <https://docs.docker.com/engine/network/firewall-iptables/>
