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

## Wenn das Modell sagt, es könne nicht suchen

Antworten wie „Ich kann leider nicht in Echtzeit im Internet suchen“ haben meist einen dieser Gründe:

- **Der Schalter „Websuche“ ist aus, und das Modell kann keine Werkzeuge.** Dann sucht niemand.
  Mit dem Schalter sucht MultiGPT *vor* der Antwort und gibt die Treffer als Quellmaterial mit;
  das geht mit jedem Modell. Bei Modellen ohne Werkzeuge zeigt das Eingabefeld unter dem Schalter
  den Hinweis „Dieses Modell kann nicht selbst suchen – mit dem Schalter sucht MultiGPT vorab“.
- **Das Modell kann Werkzeuge, ist aber nicht so eingetragen.** Selbst suchen (Werkzeug
  `web_search`) kann ein Modell nur mit dem Häkchen **„Werkzeuge“** am KI-Modell. In der
  Modellauswahl stehen solche Modelle mit dem Zusatz „· Werkzeuge“. Siehe
  [Anbieter und Modelle](Anbieter-und-Modelle): Neue Modelle bekommen das Häkchen automatisch
  (Meldung von LM Studio bzw. Liste bekannter Modelle), bestehende über die Admin-Aktion
  **„Fähigkeiten automatisch erkennen (Werkzeuge, Bilder)“** oder
  `mgpt-ctl guess_capabilities --apply`.

MultiGPT sagt dem Modell im System-Prompt in einem festen Satz (ohne persönliche Daten), was
gilt: Ist das Werkzeug angeboten, dass es `web_search` für aktuelle Fakten nutzen soll. Ist die
Websuche für den Nutzer eingerichtet, für diese Antwort aber weder der Schalter an noch das
Werkzeug angeboten (Modell ohne Werkzeuge), dass es auf den Schalter „Websuche“
hinweisen soll, statt zu behaupten, es gebe keine Suche.

## Seiten abrufen und Websites durchsuchen

Die Suche liefert nur Treffer. Damit ein Modell eine bestimmte Seite selbst lesen kann, gibt es zwei
eingebaute Werkzeuge. Sie lesen nur und laufen ohne Rückfrage:

- **`fetch_url(url, offset)`** ruft eine Seite ab und gibt ihren lesbaren Text zurück. Unterstützt
  werden HTML, Text, JSON und PDF. Bei PDF wird nur die Textebene gelesen, ohne Texterkennung;
  gescannte PDFs werden mit einer Meldung abgelehnt. Die Ausgabe ist auf etwa 12 000 Zeichen
  begrenzt. Ist die Seite länger, steht ein Hinweis dabei, und das Modell liest mit `offset` weiter.
- **`crawl_site(url, max_pages, same_site, path_prefix)`** folgt von einer Startseite aus den Links
  derselben Website. Es arbeitet in Breitensuche mit einer Linktiefe von höchstens 2 und liest
  höchstens 20 Seiten. Zurück kommen je Seite Titel, URL und ein kurzer Auszug, für die wichtigsten
  Seiten (Startseite zuerst) auch der Text, insgesamt etwa 20 000 Zeichen. `same_site=false` erlaubt
  zusätzlich Subdomains derselben Domain, nie fremde Websites. `path_prefix` (z. B. `/docs/`)
  beschränkt die Durchsuchung auf einen Bereich.

Jede gelesene Seite wird eine nummerierte Quelle [n] mit Titel, URL und Abrufdatum. Der Text geht
wie bei der Suche als nicht vertrauenswürdiges Quellmaterial an das Modell.

**Voraussetzungen:** Der Nutzer hat das Recht „Websuche“, „Websuche aktiv“ ist eingeschaltet, und das
Modell hat das Häkchen „Werkzeuge“. Eine SearXNG-URL ist für die beiden Werkzeuge nicht nötig.

**Mit dem Schalter „Websuche“ und ohne Werkzeuge:** Enthält die Frage Links, ruft MultiGPT bis zu
drei davon vor der Antwort ab, zusätzlich zur Suche. Damit funktioniert „Fasse diese Seite
zusammen: https://…“ mit jedem Modell. Eine nicht abrufbare Seite wird dem Modell als solche
genannt.

**Regeln beim Abruf:**

- Es gilt derselbe Schutz wie beim Abruf von Suchtreffern: nur `http`/`https`, keine Adressen im
  Heimnetz (auch nicht über Weiterleitungen), Größen- und Zeitgrenzen.
- `crawl_site` beachtet `robots.txt` mit dem User-Agent `MultiGPT (+Familien-Instanz)`. Fehlt die
  Datei oder ist sie nicht abrufbar, ist der Abruf erlaubt. Zwischen zwei Abrufen beim selben
  Rechner wartet es mindestens eine halbe Sekunde (bzw. `Crawl-delay`, höchstens 2 s).
- Formulare werden nie abgeschickt. URLs werden vereinheitlicht: Fragmente (`#…`) und
  Tracking-Parameter (`utm_*`, `fbclid`, `gclid`, Sitzungs-IDs …) fallen weg. Je Pfad folgt es
  höchstens drei Varianten mit anderer Query. Bilder, Videos, Archive, Programme, Office-Dateien und
  PDFs überspringt es.
- `fetch_url` ist ein einzelner Abruf wie im Browser und prüft `robots.txt` nicht.

**Einstellungen** im Admin unter **„Sucheinstellungen“**, Abschnitt „Seiten abrufen und Websites
durchsuchen“:

| Feld | Bedeutung |
|---|---|
| **Seiten abrufen (fetch_url)** | Standard an. Schaltet auch den Abruf von Links aus der Frage ab. |
| **Websites durchsuchen (crawl_site)** | Standard an |
| **Seiten je Durchsuchung** | Standard 10 (1–20) |
| **Zeitlimit Durchsuchung (s)** | Standard 30 (5–120). Danach zählen die bis dahin gelesenen Seiten. |
| **Gesperrte Domains** | Eine Domain je Zeile. Subdomains sind eingeschlossen, `example.com` sperrt also auch `www.example.com`. Gilt auch nach Weiterleitungen und für Suchtreffer. |

**„Abruf testen“** (oben rechts neben „SearXNG testen“) ruft eine eingegebene Adresse mit diesen
Regeln ab und meldet Titel und Textlänge oder den Grund, warum der Abruf nicht geht.

## Erfundene Links

Ohne Möglichkeit nachzusehen erfinden Modelle gern Fakten: Foren, Discord-Server, Mitgliederzahlen,
Treffen, Zitate, und zwar mit Links, die es nicht gibt. MultiGPT begegnet dem auf drei Wegen:

1. **Grundregeln für alle Modelle.** Vor jedem Chat geht an jedes Modell als erster Teil des
   System-Prompts:
   *„Erfinde keine URLs, Zahlen, Namen, Ereignisse oder Zitate. Nenne Links nur, wenn sie aus
   Quellmaterial oder Werkzeugergebnissen stammen. Wenn du etwas nicht überprüfen kannst, sag das
   ausdrücklich und biete an, mit der Websuche nachzusehen.“*
   Der Text ist ein Standard, den jede Installation per Migration bekommt. Ändern oder leeren kann
   man ihn im Admin unter **„Chat“ → „Chat-Einstellungen“** im Feld **„Grundregeln für alle
   Modelle“**. Ein leeres Feld bedeutet: keine Grundregeln. Ein Update überschreibt einen geänderten
   Text nicht. Reihenfolge im System-Prompt: Grundregeln, fester Prompt der Rolle, Hinweise von
   MultiGPT (Quellmaterial, Websuche), Projekt-Anweisungen, System-Prompt des Chats.
2. **Werkzeuge zum Nachsehen.** Mit `fetch_url`, `crawl_site` und `web_search` kann das Modell Links
   prüfen, statt sie zu vermuten.
3. **Markierung ungeprüfter Links.** Links in einer Antwort, deren Adresse bzw. Rechnername in keiner
   Quelle dieser Antwort, keinem Werkzeugergebnis und nicht in der Frage vorkommt, bekommen ein
   Warnsymbol ⚠. Der Hinweistext lautet „Link stammt nicht aus einer Quelle dieser Antwort –
   möglicherweise erfunden“. Unter der Antwort steht dann „Diese Antwort enthält Links ohne Quelle“
   mit dem Knopf **„Belege prüfen“**. Er schaltet die Websuche ein und schreibt eine Prüfbitte mit
   den Links ins Eingabefeld. Gesendet wird erst, wenn der Nutzer selbst sendet. Die Prüfung läuft
   nur im Browser, ohne Netzaufruf. Abschalten lässt sie sich unter **Einstellungen** →
   „Ungeprüfte Links markieren“. Alle Links in Antworten tragen `rel="noopener noreferrer
   nofollow"`.

Eine Markierung heißt nicht, dass der Link falsch ist, sondern nur, dass er aus keiner Quelle der
Antwort stammt.

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
