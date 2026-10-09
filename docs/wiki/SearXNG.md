# SearXNG als Such-Backend für MultiGPT

## Wozu braucht MultiGPT SearXNG?

MultiGPT kann Fragen mit aktuellen Informationen aus dem Web beantworten. Dazu schaltet man im
Eingabefeld den Schalter **„Websuche“** ein. MultiGPT schickt die Frage an eine Suchmaschine, ruft die
besten Treffer ab, reduziert sie auf Text und gibt sie dem Modell als nummerierte Quellen mit. Unter
der Antwort stehen dann Titel und Links der verwendeten Seiten. Das funktioniert mit jedem Modell,
auch mit lokalen Modellen in LM Studio.

Als Suchmaschine verwendet MultiGPT **[SearXNG](https://docs.searxng.org/)**. SearXNG ist eine freie
Metasuchmaschine, die man selbst betreibt. Sie fragt andere Suchmaschinen ab und fasst die Ergebnisse
zusammen. Weil sie im eigenen Heimnetz läuft, braucht MultiGPT keinen API-Key und keinen Vertrag mit
einem Suchanbieter. MultiGPT fragt SearXNG über die Such-API ab:
`/search?q=…&format=json&language=…`.

> Die Websuche ist ein Recht: Nur Nutzer mit dem Recht „Websuche“ sehen den Schalter. Abgerufene
> Webseiten behandelt MultiGPT als nicht vertrauenswürdiges Quellmaterial, nie als Anweisung.

## Welche Variante?

| | a) [Docker](SearXNG-Docker) | b) [Nativ](SearXNG-Nativ) |
|---|---|---|
| Aufwand | gering: eine Compose-Datei und eine `settings.yml` | höher: Installationsskript, uWSGI, Valkey |
| Webserver in SearXNG | Granian (im offiziellen Image) | uWSGI aus Debian (so richtet das offizielle Skript es ein) |
| Updates | `docker compose pull` | `searxng.sh instance update` (git + pip) |
| Abhängigkeiten auf dem Rechner | Docker | Python-venv, uWSGI, Valkey, Build-Pakete |
| Firewall | Docker umgeht `ufw` und eigene `nft`-Regeln, Regeln gehören in die Kette `DOCKER-USER` | normale nftables-Regeln |

**Empfehlung:** Variante **a) Docker**. Die SearXNG-Doku empfiehlt Container oder Installationsskript
gleichermaßen, nennt Compose aber den empfohlenen Weg für Container. Das Image wird vom
SearXNG-Projekt gepflegt, das Update ist ein Befehl, und Granian (der künftige Nachfolger von uWSGI)
ist laut Doku derzeit nur im Container offiziell unterstützt. Die native Variante passt, wenn auf dem
Rechner kein Docker laufen soll.

Beide Varianten können auf demselben Rechner wie MultiGPT laufen (z. B. dem NAS) oder auf einem
anderen Rechner im Heimnetz.

## Gemeinsame Punkte für beide Varianten

### 1. JSON-Format freigeben (Pflicht)

MultiGPT braucht die Ergebnisse als JSON. In der Grundeinstellung liefert SearXNG nur HTML. Fragt man
ein Format ab, das nicht freigegeben ist, antwortet SearXNG mit **403 Forbidden**. In der
`settings.yml` muss deshalb stehen:

```yaml
search:
  formats:
    - html
    - json
```

### 2. Eigenen `secret_key` setzen

`server.secret_key` dient SearXNG für kryptografische Zwecke. Der Platzhalter `ultrasecretkey` darf
nicht stehen bleiben. Beide Anleitungen erzeugen einen Zufallswert mit `openssl rand -hex 32`. Den
Schlüssel nicht weitergeben und nicht in ein Repository einchecken.

### 3. Bot-Schutz (Limiter) und der MultiGPT-Rechner

SearXNG hat einen Bot-Schutz, den **Limiter** (`server.limiter`). Er braucht eine Valkey-Datenbank.
Für MultiGPT ist er ein Hindernis, denn er hält jeden Programmzugriff für einen Bot:

- Für `/search` verlangt er unter anderem einen `Accept`-Header mit `text/html` und einen
  `Accept-Language`-Header. MultiGPT fragt aber JSON an (`Accept: application/json`).
- `User-Agent`-Kennungen wie `curl` oder `python-requests` gelten als Bot.
- Anfragen mit einem anderen Format als HTML (also `format=json`) sind auf **4 je Stunde und IP**
  begrenzt.

Ist der Limiter aktiv, bekommt MultiGPT früher oder später **429 Too Many Requests**. Es gibt zwei
saubere Lösungen:

1. **Limiter aus** (`server.limiter: false`). Das ist die einfachste Lösung, wenn SearXNG nur für
   MultiGPT da ist und per Firewall nur vom MultiGPT-Rechner erreichbar ist (siehe Punkt 4). Im
   Docker-Image ist der Limiter in der Grundeinstellung aus, im nativen Installationsskript ist er an.
2. **Limiter an, MultiGPT-Rechner freigeben.** In der Datei `limiter.toml` (im selben Ordner wie die
   `settings.yml`) trägt man **nur die IP des MultiGPT-Rechners** in `pass_ip` ein. Für IPs in
   `pass_ip` prüft SearXNG weder Header noch User-Agent, und es gilt keine Ratenbegrenzung:

   ```toml
   [botdetection.ip_lists]
   pass_ip = [
     '192.168.24.250',  # MultiGPT-Rechner, an die eigene IP anpassen
   ]
   ```

   Keine ganzen Netze wie `192.168.0.0/16` eintragen. Damit wäre der Bot-Schutz für das ganze
   Heimnetz aus.

Die SearXNG-Doku sagt nicht ausdrücklich, ob der Limiter für private Instanzen nötig ist. Sie
beschreibt ihn als Schutz davor, dass Bot-Verkehr über die eigene Instanz zu CAPTCHAs oder Sperren bei
den abgefragten Suchmaschinen führt. Ist SearXNG nur vom MultiGPT-Rechner aus erreichbar, kommt kein
fremder Bot-Verkehr an.

### 4. Nur im Intranet, nur für den MultiGPT-Rechner

- SearXNG darf **nicht aus dem Internet** erreichbar sein: keine Portweiterleitung im Router, kein
  öffentlicher DNS-Name.
- SearXNG lauscht nur an `127.0.0.1` (wenn MultiGPT auf demselben Rechner läuft) oder an der
  LAN-Adresse des SearXNG-Rechners (`<LAN-IP>`), **nie an `0.0.0.0`**.
- Läuft SearXNG auf einem anderen Rechner, lässt die Firewall dort nur den **MultiGPT-Rechner**
  (z. B. `192.168.24.250`) auf den SearXNG-Port. Wie das geht, steht in der jeweiligen Anleitung. Bei
  Docker ist das wichtig, weil veröffentlichte Ports an `ufw` und eigenen `nft`-Regeln vorbeigehen.

## Test mit curl

Vom **MultiGPT-Rechner** aus (Platzhalter ersetzen, Port 8080 bei Docker, 8888 bei nativ):

```sh
curl -s 'http://<host>:<port>/search?q=test&format=json' | head -c 400; echo
```

Erwartet wird JSON, das mit `{"query": "test", …` beginnt und eine Liste `results` enthält. Anzahl der
Treffer:

```sh
curl -s 'http://<host>:<port>/search?q=test&format=json' \
  | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["results"]), "Treffer")'
```

HTTP-Status und Antwortkopf zeigt `curl -i`:

```sh
curl -i 'http://<host>:<port>/search?q=test&format=json'
```

> Ist der Limiter an, wird `curl` von **anderen** Rechnern als dem in `pass_ip` eingetragenen am
> User-Agent als Bot erkannt (429). Den Test deshalb vom MultiGPT-Rechner aus machen.

## In MultiGPT eintragen

1. Als Verwalter im Django-Admin anmelden (`https://<multigpt-host>/admin/`).
2. Auf der Startseite im Abschnitt **„Chat“** auf **„Sucheinstellungen“** klicken
   (Adresse `/admin/chat/searchsettings/`). Es gibt nur einen Datensatz, die Liste öffnet direkt das
   Formular.
3. Ausfüllen:

   | Feld | Wert |
   |---|---|
   | **Websuche aktiv** | erst einschalten, wenn der Test (Schritt 4) erfolgreich war |
   | **Such-Backend** | `SearXNG` |
   | **SearXNG-URL** | Basisadresse der Instanz, z. B. `http://127.0.0.1:8080` (Docker, gleicher Rechner), `http://<LAN-IP>:8080` (Docker, anderer Rechner), `http://<LAN-IP>:8888` (nativ). Ein Pfad ist erlaubt (`https://host/searxng`), ein Schrägstrich am Ende ist egal. Nicht `/search` anhängen, das macht MultiGPT selbst. |
   | **Sprache** | Standard `de`, SearXNG-Sprachcode wie `de`, `en`, `de-DE` oder `all` |
   | **Jugendschutzfilter** | aus, mittel (Standard) oder streng. Wird als SafeSearch an die Suchmaschinen weitergegeben, soweit sie es unterstützen. |
   | **Trefferzahl** | Standard 5 (1–20) |
   | **Seiten abrufen** | Standard 3 (0–10). Bei 0 nutzt MultiGPT nur die Kurztexte der Treffer. |
   | **Zeitlimit (s)** | Standard 10 (1–60) |

4. **Speichern**, dann oben rechts auf **„SearXNG testen“** klicken. Der Test prüft die
   *gespeicherten* Werte. MultiGPT sucht nach „test“ und meldet bei Erfolg:
   *„SearXNG erreichbar: N Treffer für „test“.“*
5. **Websuche aktiv** einschalten und speichern.
6. Nutzer brauchen das Recht **„Websuche“**, damit der Schalter im Eingabefeld erscheint.

MultiGPT schickt bei der Suche den User-Agent `MultiGPT/<version> (Websuche)`,
`Accept: application/json` und `Accept-Language` gleich der eingestellten Sprache (bei `all`: `de`).

## Fehlersuche

| Meldung beim Test / Symptom | Ursache | Abhilfe |
|---|---|---|
| **HTTP 403**, „JSON-Format nicht freigegeben“ | `json` fehlt in `search.formats` | In der `settings.yml` `json` unter `search.formats` ergänzen (siehe oben) und SearXNG neu starten |
| **HTTP 429**, „Zu viele Anfragen“ / Bot-Schutz | Limiter aktiv, MultiGPT-Rechner nicht freigegeben | `server.limiter: false` setzen **oder** die IP des MultiGPT-Rechners in `pass_ip` der `limiter.toml` eintragen, dann neu starten. Bei Docker auf demselben Rechner siehe den Hinweis zur Quell-IP in der [Docker-Anleitung](SearXNG-Docker). |
| **„Verbindung abgelehnt“** | SearXNG lauscht nicht an dieser Adresse oder diesem Port, oder läuft nicht | Docker: `SEARXNG_HOST`/`SEARXNG_PORT` in `.env`, `docker compose ps`. Nativ: `http =`-Zeile in `/etc/uwsgi/apps-available/searxng.ini`, `sudo -H service uwsgi status searxng`. Lauscht SearXNG nur an `127.0.0.1`, kommt ein anderer Rechner nicht heran. |
| **„Zeitüberschreitung“** | Firewall verwirft die Pakete, oder SearXNG antwortet zu langsam | Firewall-Regel auf dem SearXNG-Rechner prüfen: Ist genau die IP des MultiGPT-Rechners erlaubt? Sonst **Zeitlimit (s)** erhöhen. |
| **„Rechnername unbekannt“** | Der Name in der URL ist im Heimnetz nicht auflösbar | IP-Adresse verwenden oder den Namen im lokalen DNS bzw. in `/etc/hosts` des MultiGPT-Rechners eintragen |
| **HTTP 404**, „Adresse nicht gefunden“ | Falscher Pfad in der URL | Nur die Basisadresse eintragen, ohne `/search` |
| **„Ungültige Antwort: … kein JSON“** | Die URL zeigt nicht auf SearXNG (z. B. auf eine andere Webseite) | URL prüfen |
| **„keine Treffer“** | SearXNG erreichbar, aber die abgefragten Suchmaschinen liefern nichts (gesperrt, nicht erreichbar) | Im Browser `http://<host>:<port>/` öffnen und suchen. Logs ansehen (Docker: `docker compose logs -f core`). |

## Quellen

Gelesen am 2026-10-09:

- <https://docs.searxng.org/admin/installation.html>
- <https://docs.searxng.org/admin/installation-docker.html>
- <https://docs.searxng.org/admin/installation-granian.html>
- <https://docs.searxng.org/admin/settings/settings_server.html>
- <https://docs.searxng.org/admin/settings/settings_search.html>
- <https://docs.searxng.org/admin/searx.limiter.html>
- <https://docs.searxng.org/dev/search_api.html>
- Quellcode (Stand `master`, für die genauen Regeln des Bot-Schutzes, die die Doku nicht nennt):
  <https://github.com/searxng/searxng/blob/master/searx/limiter.py>,
  <https://github.com/searxng/searxng/blob/master/searx/limiter.toml>,
  <https://github.com/searxng/searxng/blob/master/searx/botdetection/ip_limit.py>,
  <https://github.com/searxng/searxng/blob/master/searx/botdetection/http_accept.py>,
  <https://github.com/searxng/searxng/blob/master/searx/botdetection/http_user_agent.py>,
  <https://github.com/searxng/searxng/blob/master/searx/webapp.py> (403 bei nicht freigegebenem Format)
