# Runner (Arbeitsumgebung für Modelle)

Der **Runner** gibt den Modellen eine echte Arbeitsumgebung: Dateien lesen, anlegen und gezielt
ändern, suchen, Befehle ausführen, testen und nachbessern – wie ein Programmierwerkzeug, aber
eingesperrt in einem Container auf **deinem** Rechner (PC oder Laptop im Heimnetz), nicht auf dem
Server.

- Der Container hat **kein Netz** (Ausnahme: Modus „Paketquellen“, s. u.) und nur einen
  flüchtigen Arbeitsbereich (`/workspace` im Arbeitsspeicher). Endet die Sitzung, bleibt auf dem
  Rechner nichts zurück.
- Was das Modell erarbeitet, liegt als **Arbeitsstand** (git-Bundle mit der ganzen Historie) auf
  dem Server und wird mit der Datenbank gesichert. Jeder Werkzeugaufruf, der Dateien ändert, ist
  ein eigener Commit – jeder Stand ist zurückholbar.
- Der Runner verbindet sich **nur ausgehend** mit MultiGPT (`wss://<server>/runner/ws`). Auf
  deinem Rechner bleibt kein Port offen. Jeder Zugriff läuft über MultiGPT: Rechte, Kontingent,
  Rückfragen und Audit-Log prüft immer der Server.

## Wie startet der Container?

**Gar nicht von Hand.** Den Arbeitscontainer startet das Programm `multigpt-runner`, das als
Dienst deines Nutzers läuft:

1. Einmalig: Token erzeugen (Abschnitt 1), Programm installieren und koppeln (Abschnitt 2). Ab
   dann läuft `multigpt-runner` als systemd-User-Dienst und hält die Verbindung zu MultiGPT.
2. Im Chat einen **Arbeitsstand** wählen (Abschnitt 3).
3. Der **erste Werkzeugaufruf** des Modells startet die Sitzung: Der Runner zieht das
   Arbeits-Image (`ghcr.io/beerlesklopfer/multigpt-runner-work`, nur mit dem Digest, den der Server
   vorgibt), startet den Container nach der festen Vorlage (kein Netz, nur tmpfs, keine Rechte),
   lädt den Arbeitsstand hinein und führt die Werkzeuge darin aus.
4. Nach 15 Minuten ohne Werkzeugaufruf (oder bei „Stopp“ im Chat) entfernt der Runner den
   Container wieder. Der Arbeitsstand ist dann schon auf dem Server.

Ob der Runner verbunden ist, zeigt die Seite **Runner** („verbunden“, Version, laufende
Sitzungen). Läuft nichts, hilft `systemctl --user status multigpt-runner` und
`journalctl --user -u multigpt-runner -f`.

### Arbeits-Image von Hand ausprobieren

Zum Anschauen – mit denselben Einschränkungen, die der Runner setzt:

```bash
podman pull ghcr.io/beerlesklopfer/multigpt-runner-work:main
podman run --rm -it --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --pids-limit 256 \
  --tmpfs /workspace:rw,size=256m,mode=0700,U --tmpfs /tmp:rw,size=64m \
  ghcr.io/beerlesklopfer/multigpt-runner-work:main bash
```

Das ist nur zum Ausprobieren; MultiGPT benutzt so gestartete Container nicht. `:main` ist der
Stand von `main`, `:latest` bzw. `:<version>` der letzte Release.

### Zum Testen in der Entwicklung (ohne TLS)

Für einen Entwicklungsserver ohne HTTPS:

1. Runner-Dienst des Servers starten: `.venv/bin/python manage.py runner_gateway`
   (lauscht auf `MULTI_GPT_RUNNER_BIND`, Vorgabe `127.0.0.1:8001`; für einen anderen Rechner im
   Heimnetz z. B. `--bind 0.0.0.0:8001`).
2. In der `.env` des Servers das Arbeits-Image **mit Digest** eintragen, z. B.
   `RUNNER_WORK_IMAGE=ghcr.io/beerlesklopfer/multigpt-runner-work:main-<commit>@sha256:…`
   (steht in der Zusammenfassung des CI-Laufs „Runner-Images“), und den Server neu starten.
3. Auf der Seite **Runner** einen Token erzeugen.
4. Auf dem Rechner mit Podman koppeln, mit `ws://` statt `wss://`:
   ```bash
   export MGPT_INSECURE_DEV=1
   printf '%s\n%s\n' "<token>" "<öffentlicher Schlüssel>" | \
     multigpt-runner pair --url ws://<server>:8001/runner/ws --images "<Image mit Digest>"
   ```
   Für die Unit `Environment=MGPT_INSECURE_DEV=1` ergänzen (`systemctl --user edit multigpt-runner`).

`MGPT_INSECURE_DEV` nie auf der Produktion: Ohne TLS gehen Token und Werkzeugaufrufe im Klartext
über das Netz.

## Voraussetzungen

- Recht **„Runner nutzen“** in deiner Rolle (Startwerte: Verwalter und Erwachsene ja,
  Jugendliche und Gäste nein; ändert ein Verwalter im Admin unter *Konten und Gruppen › Rollen*).
- **Linux auf amd64 oder arm64** (PC, Laptop, Raspberry Pi mit 64-Bit-System) mit **systemd**.
- **Podman** (rootless). Docker geht technisch auch, ist aber root-gleich und nicht empfohlen.
- MultiGPT ist über **HTTPS** erreichbar (siehe [nginx und TLS](nginx-und-TLS)). Bei einem
  selbstsignierten Zertifikat hält der Runner dessen Fingerprint fest; die Kopplungsseite trägt
  ihn automatisch ein.

Der Runner ist ein **eigenständiges Programm** (`multigpt-runner`, in Rust geschrieben, eine
Datei ohne Abhängigkeiten). Es läuft als systemd-Dienst **deines Nutzers**, nicht als root, und
startet die Arbeitscontainer über deinen Podman-Socket – immer nach derselben festen Vorlage.

## 1. Token erzeugen

1. Im Kontomenü (oben rechts) **Runner** öffnen.
2. Name eingeben (z. B. „Laptop“), Gültigkeit wählen, **Token erzeugen**.
3. Der Token (`mgrt_…`) ist **nur jetzt** sichtbar. Darunter stehen die Befehle zum Einrichten,
   die **Eingabe für `pair`** (Zeile 1 Token, Zeile 2 öffentlicher Schlüssel des Servers) und die
   fertige Unit-Datei – alles mit Kopierknopf.

Ein Token gehört zu genau einem Rechner. Er lässt sich jederzeit **widerrufen**; ein verbundener
Runner wird dann sofort getrennt. MultiGPT speichert nur einen Hash des Tokens.

## 2. Programm installieren und koppeln (Linux)

### Programm von MultiGPT herunterladen (empfohlen)

MultiGPT liefert das Programm selbst aus – das Zielsystem braucht weder Internet noch Rust.
Auf der Seite **Runner** stehen die Programme für **amd64** und **arm64** mit Prüfsumme; die
Prüfsummen sind mit dem Schlüssel des jeweiligen Releases signiert (`SHA256SUMS`,
`SHA256SUMS.sig`). Herunterladen geht nur angemeldet bzw. mit einem Kopplungs-Token.

```sh
systemctl --user enable --now podman.socket   # Podman-Socket des Nutzers
loginctl enable-linger $USER                   # Dienst läuft auch ohne Anmeldung
cd ~/Downloads && ARCH=$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')
sha256sum --ignore-missing -c <<'SUMS'         # Prüfsummen von der Seite „Runner“
<sha256>  multigpt-runner-linux-amd64
<sha256>  multigpt-runner-linux-arm64
SUMS
install -D -m 0755 multigpt-runner-linux-$ARCH ~/.local/bin/multigpt-runner
~/.local/bin/multigpt-runner --version
```

### Alternative für Entwickler: `cargo install`

Mit Rust (ab 1.88) direkt aus dem Quellbaum von MultiGPT:

```sh
cargo install --locked --path runner-rs/multigpt-runner   # legt ~/.cargo/bin/multigpt-runner an
install -D -m 0755 ~/.cargo/bin/multigpt-runner ~/.local/bin/multigpt-runner
```

Ein selbst gebautes Programm trägt keinen signierten Dienstkatalog. Der Server bietet seine
Werkzeuge deshalb nur an, wenn der Katalog eines Releases signiert mitgegeben wird
(`MGPT_CATALOG_FILE`, `MGPT_CATALOG_SIG_FILE`, `MGPT_CATALOG_KEY`) oder in einer
Entwicklungsinstallation (`DEBUG` mit `RUNNER_ACCEPT_UNSIGNED`). Für den Alltag das Programm
von MultiGPT nehmen.

### Koppeln und als Dienst starten

Den Befehl von der Seite **Runner** ausführen, danach die **Eingabe für `pair`** einfügen und
mit Strg+D abschließen:

```sh
~/.local/bin/multigpt-runner pair --url https://mgpt.example \
    --images ghcr.io/…/multigpt-runner-work@sha256:… [--ca-sha256 <Fingerprint>]
```

`pair` legt in `~/.config/multigpt-runner/` den Token (`token`, nur für dich lesbar) und den
öffentlichen Schlüssel des Servers (`server.pub`, schreibgeschützt) ab, schreibt die User-Unit
`~/.config/systemd/user/multigpt-runner.service` und startet den Dienst. Der Token steht nie in
einem Befehl (Shell-Verlauf), in der Unit-Datei oder im Container.

Prüfen: `systemctl --user status multigpt-runner` bzw. `journalctl --user -u multigpt-runner`.
Auf der Seite **Runner** steht der Rechner nach wenigen Sekunden als *verbunden*.

Die Unit-Datei (Auszug; der vollständige Inhalt steht auf der Seite **Runner**, von Hand
anlegen geht auch):

```ini
[Service]
ExecStart=%h/.local/bin/multigpt-runner
Environment=MGPT_URL=https://mgpt.example
Environment=MGPT_TOKEN_FILE=%h/.config/multigpt-runner/token
Environment=MGPT_SERVER_KEY_FILE=%h/.config/multigpt-runner/server.pub
Environment=MGPT_PODMAN_SOCKET=%t/podman/podman.sock
Environment=MGPT_IMAGES=ghcr.io/…/multigpt-runner-work@sha256:…
Restart=on-failure
RestartPreventExitStatus=2 3
```

**Exit-Codes:** `2` = Einstellungen fehlen oder sind falsch (z. B. Token- oder Schlüsseldatei
fehlt, Image ohne Digest). `3` = der Server hat abgelehnt (Token ungültig oder widerrufen, Recht
fehlt, Verbindung ersetzt, Protokollversion passt nicht) **oder** ein Rahmen trug keine gültige
Signatur dieses Servers. In beiden Fällen startet systemd den Dienst **nicht** neu – erst neu
koppeln (`pair` mit neuem Token). Bei Netzproblemen versucht es der Runner selbst wieder
(1 s, 2 s, 4 s … bis 60 s).

**Neuer Token:** auf der Seite **Runner** erzeugen und `pair` erneut ausführen (ersetzt Token
und Schlüssel); den alten Token widerrufen.

### Raspberry Pi und andere arm64-Geräte

- Ein **64-Bit-System** ist nötig (Raspberry Pi OS 64 bit bzw. Debian arm64, Pi 3/4/5). Prüfen:
  `uname -m` muss `aarch64` melden; auf 32-Bit-Systemen läuft das Programm nicht.
- Podman aus den Paketquellen: `sudo apt install podman` (Debian 12/13, Raspberry Pi OS).
- Das Programm `multigpt-runner-linux-arm64` von der Seite **Runner** nehmen; der Rest ist wie
  oben.
- Grenzen für RAM und Prozesse setzt Podman über cgroups v2. Meldet die Sitzung „Grenzen weichen
  ab“, fehlt dem Kernel der Speicher-Controller: in `/boot/firmware/cmdline.txt`
  `cgroup_enable=memory` ergänzen und neu starten.
- Der erste Start lädt das Arbeits-Image (je nach Leitung einige Minuten).

Ein Gerät, auf dem gearbeitet werden soll (z. B. ein Pi in der Werkstatt), wird so ein **eigener
Runner** deines Kontos – mit eigener Kopplung, eigenen Rechten und eigenem Audit. Befehle laufen
auch dort nur im Container nach der festen Vorlage.

### Zielsystem aus dem Chat einrichten (`setup_host`)

Statt von Hand kann ein Modell ein freigegebenes Gerät einrichten: Werkzeug **`setup_host`**
(fragt **immer** nach). Voraussetzungen: Recht „Runner-Hosts nutzen“ und ein vom Verwalter
freigegebener Host mit **SSH-Schlüssel** (Admin *Runner › Hosts*). Der Runner verbindet sich
aus einem kurzlebigen Hilfscontainer per ssh, bestimmt die Architektur, lädt das Programm von
MultiGPT (Prüfsumme wird verglichen), installiert es nach `~/.local/bin` und koppelt mit einem
frischen Token nur für diesen Aufruf. Schlägt die Einrichtung fehl, widerruft der Server diesen
Token sofort. Zugang und Token sieht das Modell nie.

### Windows und macOS

Das Programm gibt es nur für Linux. Unter Windows geht der Linux-Weg in WSL2 mit systemd, unter
macOS in einer Linux-VM (z. B. Podman machine, `podman machine ssh`). Der Server muss von dort
erreichbar sein (gleiche Adresse wie im Browser).

### Ausweg: Runner als Container (Quadlet)

Solange es den bisherigen Python-Agenten noch gibt, zeigt die Seite **Runner** unter „Ausweg“
zusätzlich eine Quadlet-Datei (Agent als Container mit eingebundenem Podman-Socket, Token und
Schlüssel als Podman-Secrets). Neue Installationen bitte mit dem Programm oben.

## 3. Im Chat arbeiten

1. Arbeitsstand anlegen: **Runner › Arbeitsstände**, „Neuer Arbeitsstand“ (z. B. „Bildschirmschoner“).
2. Im Chat erscheint unter den Eingabeoptionen der Bereich **Runner**: Arbeitsstand wählen
   und den **Rückfrage-Modus**:

   | Modus | Dateien ändern | Befehle |
   |---|---|---|
   | fragen | mit Rückfrage | mit Rückfrage |
   | Änderungen automatisch | ohne Rückfrage | mit Rückfrage |
   | alles automatisch (Vorgabe ohne Netz) | ohne Rückfrage | ohne Rückfrage |

   `setup_host` und `telnet` fragen **immer** nach, egal welcher Modus.

   Daneben die Auswahl **Scratch** (Ablage für Zwischenergebnisse unter `/scratch`): *im
   Container* (flüchtig, Vorgabe) oder *auf diesem Rechner* (Ordner `~/MultiGPT/Scratch/<id>/`).
   Nach MultiGPT kommt daraus nur, was das Modell ausdrücklich mit `publish` als Anhang
   veröffentlicht. Die Auswahl gilt ab der nächsten Sitzung.
3. Ein werkzeugfähiges Modell wählen und die Aufgabe beschreiben. Der erste Werkzeugaufruf startet
   die Sitzung (Container); das dauert beim ersten Mal etwas länger (Image laden).
4. Über dem Eingabefeld erscheinen die **Aufgabenliste** des Modells und die Knöpfe:
   - **Terminal**: eine Shell im Container, direkt im Browser (auch für Programme mit Eingabe).
     *Strg+Umschalt+F6* verlässt das Terminal mit der Tastatur.
   - **Vorschau**: HTML-Seiten, die das Modell mit `serve` bereitstellt, in einem abgeschotteten
     Rahmen – oder über „In neuem Tab öffnen“.
   - **Audit**: alle Werkzeugaufrufe der Sitzung.

Nach 100 Werkzeugrunden fragt der Chat, ob weitergearbeitet werden soll.

## Pakete nachladen (Modus „Paketquellen“)

Ohne Netz kann das Modell nur mit dem arbeiten, was im Arbeits-Image steckt (bash, git, Python,
Node). Mit dem Rollenrecht **„Paketquellen“** darf es zusätzlich Pakete nachladen – vor allem mit
apt, außerdem mit pip, npm, cargo und von GitHub –, erreicht dabei aber **nur fest freigegebene
Paketquellen**, nie das Heimnetz oder beliebige Seiten.

**Wer darf?** Recht „Paketquellen“ in der Rolle (Startwerte: Verwalter und Erwachsene ja,
Jugendliche und Gäste nein; Admin unter *Konten und Gruppen › Rollen*). Ist auch **„Runner mit
Netz“** an, geht das freie Netz vor (dann gibt es kein `apt_install`). Ohne beide Rechte bleibt
das Netz aus. Über API-Keys gilt der Modus nur mit demselben Rollenrecht. Der Rückfrage-Modus
„alles automatisch“ bleibt erlaubt.

- Im Chat steht im Bereich **Runner** der Schalter **„Paketquellen“** (nur mit dem Rollenrecht
  sichtbar, Vorgabe an). Umschalten geht nur, solange keine Sitzung läuft; er gilt ab der
  nächsten Sitzung.
- In einer Sitzung mit Paketquellen gibt es **kein `setup_host` und kein `telnet`**. Für die
  Einrichtung eines Zielsystems bzw. telnet den Schalter ausschalten.
- Der Modus braucht ein aktuelles `multigpt-runner` (Werkzeug `apt_install` im signierten
  Dienstkatalog). Ältere Runner bekommen ihn nie angeboten; die Sitzung startet dann ohne Netz.

### apt: nur über das Werkzeug `apt_install`

Das Modell ruft `apt_install` mit einer Liste von Debian-Paketen auf, z. B. `["jq", "ripgrep"]`.
Der Runner führt dann im Container **als root** nur `apt-get update` (bei Bedarf) und
`apt-get install -y --no-install-recommends <pakete>` aus:

- Paketnamen werden streng geprüft (Kleinbuchstaben, Ziffern, `+ . -`, höchstens 20 je Aufruf);
  Optionen, Pfade oder Shell-Zeichen werden abgewiesen.
- Die Shell des Modells bleibt **Nutzer 1000**: `apt-get`/`dpkg` selbst aufzurufen scheitert an
  fehlenden Rechten, `sudo` gibt es nicht – und auch ein nachinstalliertes `sudo` wirkt nicht
  (`no-new-privileges`, keine setuid-Programme).
- `apt_install` zählt wie `bash` als Befehl (Rückfrage je nach Modus) und steht im Audit-Log.

**Über Sitzungen hinweg:** Die installierten apt-Pakete stehen in **`.mgpt/apt-packages`** im
Arbeitsstand. Startet später eine neue Sitzung mit diesem Arbeitsstand, installiert der Runner sie
automatisch nach. Wer ein Paket nicht mehr braucht, entfernt die Zeile aus der Datei.

### pip, npm, cargo, GitHub

Diese Werkzeuge arbeiten als Nutzer 1000 und installieren **in den Arbeitsstand** unter
`/workspace` – ohne root:

| Werkzeug | Beispiel | landet in |
|---|---|---|
| pip | `python3 -m venv .venv && .venv/bin/pip install requests` | `.venv/` |
| npm | `npm install left-pad`, `npx http-server` | `node_modules/` |
| cargo | `cargo build` (nach `apt_install(["cargo"])`) | `target/` |
| git | `git clone https://github.com/…` | Ordner im Arbeitsstand |

`.venv`, `node_modules` und `target` trägt der Runner automatisch in **`.gitignore`** ein: Sie
gehören nicht ins Bundle (das bleibt klein) und werden in einer neuen Sitzung neu installiert –
am besten über `requirements.txt` bzw. `package.json` im Arbeitsstand.

### Der Proxy und was gesperrt ist

Der Arbeitscontainer hat auch in diesem Modus **keinen direkten Weg nach außen** und kein DNS.
Er hängt nur an seinem internen Sitzungsnetz; dort steht ein **Proxy** (der Relay-Container der
Sitzung), den pip, npm, cargo, git und apt über `HTTP_PROXY`/`HTTPS_PROXY` benutzen. Der Proxy
lässt nur diese Hosts durch (Einstellung `RUNNER_PACKAGE_HOSTS` des Servers, signiert an den
Runner übergeben):

`deb.debian.org`, `security.debian.org`, `pypi.org`, `files.pythonhosted.org`,
`registry.npmjs.org`, `github.com`, `codeload.github.com`, `objects.githubusercontent.com`,
`index.crates.io`, `static.crates.io`

Abgewiesen werden:

- alle anderen Hosts (z. B. `example.com`) und Unterdomänen, die nur so aussehen;
- **IP-Literale** (`http://1.1.1.1/`, IPv6, Dezimalschreibweise) – der Proxy nimmt nur Namen;
- **Heimnetz und Rechner selbst** (`192.168.0.0/16`, `10.0.0.0/8`, `172.16.0.0/12`, Loopback,
  Link-Local) – auch, wenn ein freigegebener Name dorthin aufgelöst würde;
- andere Ports (nur `GET` über Port 80 und `CONNECT` zu Port 443);
- jeder Versuch am Proxy vorbei (direkte Verbindung, eigenes DNS) – es gibt keine Route.

Fällt der Proxy aus, ist das Netz zu (fail closed).

**Grenze der Container-Schicht:** Weil das Wurzeldateisystem in diesem Modus beschreibbar ist,
misst der Runner die Größe der Schicht bei jedem Heartbeat. Die Grenze legt die Rolle fest
(„Runner: Container-Schicht (MB)“, Vorgabe 4096 MB); wird sie überschritten, endet die Sitzung.
Die Schicht ist flüchtig und mit dem Container weg – der Arbeitsstand bleibt.

### Eigener HTML-Server

Für die **Vorschau** gibt es weiter `serve` (mgpt-serve). Ein selbst installierter Server geht
genauso, solange er auf **Port 8080** im Container lauscht, z. B.:

```bash
python3 -m http.server 8080 --directory site
npx http-server site -p 8080
```

Veröffentlicht wird nur Port 8080, und zwar wie bisher nur über MultiGPT mit dem Token der
Sitzung (Knopf **Vorschau**). Andere Ports sind von außen nicht erreichbar.

## Seite „Runner“: Live-Status und Audit

Die Seite zeigt (alle 5 s aktualisiert) deine Runner, die laufenden und letzten Sitzungen und die
**Laufzeit** im Monat. Je Sitzung gibt es ein **Audit-Log**: Werkzeug, gekürzte Argumente
(Geheimnisse entfernt), Exit-Code, Dauer – nie Ausgaben. Einträge werden nach `API_AUDIT_DAYS`
Tagen (Standard 90) gelöscht.

Verwalter sehen im Admin nur **Metadaten** (Runner, Sitzungen, Größe der Arbeitsstände), keine
Inhalte, keine Argumente von Werkzeugaufrufen und keine Ausgaben anderer Konten.

## Laufzeit-Kontingent

Container-Minuten zählen auf das Abrechnungskonto **„Runner-Laufzeit“** (siehe
[Kosten und Budgets](Kosten-und-Budgets)):

- Kontingent je Rolle bzw. Person in Minuten pro Monat (Person vor Rolle), leer = unbegrenzt.
  Startwert für Erwachsene: **600 Minuten**.
- Gezählt wird nach der Uhr des Servers, solange der Runner die Sitzung als laufend meldet
  (Heartbeat alle 30 s; Ausfälle zählen nicht). Am Sitzungsende wird auf die angefangene Minute
  aufgerundet.
- Ab **80 %** erscheint ein Hinweis (Kopfzeile, Seite „Runner“). Bei **100 %** stoppt der Server
  alle Sitzungen des Kontos, neue starten erst im nächsten Monat. **Die Arbeitsstände bleiben
  erhalten.**
- *Mein Verbrauch* und die Familienübersicht zeigen die Minuten.

## Sicherheit in Kürze

- Drei Schichten: **Wer** – der Token weist deinen Rechner aus (auf dem Server nur als Hash,
  widerrufbar). **Was** – der Runner bietet nur Dienste aus einem **signierten Dienstkatalog**
  an (Signatur des Releases; verändert oder unsigniert → keine Werkzeuge, Eintrag im Audit).
  **Wo** – Befehle laufen nur im Container nach fester Vorlage.
- Jeder Rahmen des Servers ist mit dem **Schlüssel dieser Installation** signiert und an die
  Verbindung gebunden; der Runner nimmt nichts anderes an (sonst Ende mit Exit-Code 3). Token,
  Programm und Schlüssel liegen außerhalb der Sandbox – das Modell kann sie weder lesen noch
  ändern.
- Zugänge für `telnet` und `setup_host` kommen nur vom Server, je bestätigtem Aufruf, und liegen
  nur in einem kurzlebigen Hilfscontainer – nie im Arbeitscontainer.

- Arbeitscontainer: nur das freigegebene Image (mit Digest), rootless, ohne Capabilities, ohne
  Host-Verzeichnisse, mit Grenzen für CPU, RAM, Prozesse und Ausgabe; Netz aus (ohne DNS).
- Modus „Paketquellen“ (nur mit Rollenrecht): Die feste Vorlage wird **nur** für den
  Arbeitscontainer und nur so weit gelockert, wie `apt_install` es braucht – beschreibbare,
  flüchtige Container-Schicht und genau die Capabilities `CHOWN`, `DAC_OVERRIDE`, `FOWNER`,
  `FSETID`, `SETUID`, `SETGID` (rootless nur im eigenen User-Namespace, also ohne Rechte auf dem
  Rechner). `no-new-privileges`, Nutzer 1000 für die Shell, keine Host-Namespaces, keine fremden
  Netze und keine Mounts bleiben. Netz nur über den Proxy mit Positivliste, kein Heimnetz. Runner
  und Testvektoren lehnen jede andere Kombination ab (z. B. beschreibbare Schicht ohne diesen
  Modus, weitere Capabilities, `privileged`, ein zweites Netz).
- Keine Geheimnisse des Rechners im Container (kein Home-Verzeichnis, keine SSH-Schlüssel,
  bereinigte Umgebung).
- Vorschau: nur über MultiGPT mit zufälligem Sitzungs-Token im Pfad, in einem Rahmen mit
  `sandbox` und strenger CSP (keine Netzverbindungen, kein Zugriff auf MultiGPT), ohne
  Verzeichnislisten.
- Alles, was aus dem Container kommt, gilt als nicht vertrauenswürdig.

Das vollständige Bedrohungsmodell steht in `docs/Implementierung.md`, Abschnitt 5.

## Fehlersuche

| Meldung | Ursache und Abhilfe |
|---|---|
| „Kein Runner verbunden“ | Agent läuft nicht oder erreicht den Server nicht: `systemctl --user status multigpt-runner`, Adresse und Zertifikat prüfen. |
| Dienst endet mit Code 2 | Einstellungen fehlen oder sind falsch (`journalctl --user -u multigpt-runner` nennt die Ursache, z. B. Token- oder Schlüsseldatei fehlt): `pair` erneut ausführen. |
| Dienst endet mit Code 3 | Token ungültig, abgelaufen oder widerrufen, Rolle ohne Recht oder Signatur des Servers ungültig (z. B. Server neu installiert): neuen Token erzeugen, `pair` erneut ausführen. |
| Runner verbunden, aber keine Werkzeuge | Dienstkatalog unsigniert, verändert oder von einem zu alten Release (die Seite **Runner** zeigt den Zustand): Programm von MultiGPT neu herunterladen. |
| „Fingerprint passt nicht“ im Log | Das Zertifikat des Servers hat sich geändert: Fingerprint auf der Seite **Runner** prüfen und `pair` mit neuem `--ca-sha256` ausführen. |
| „Laufzeit-Kontingent ausgeschöpft“ | Monatskontingent verbraucht; ein Verwalter kann es im Admin unter *Kosten und Abrechnung › Abrechnungskonten › Runner-Laufzeit* erhöhen. |
| „Der Arbeitsstand wird gerade in einer anderen Sitzung benutzt“ | Nur eine Sitzung je Arbeitsstand: die andere Sitzung beenden (oder warten, bis der Leerlauf sie stoppt). |
| Terminal: „Zu viele offene Terminals“ | Je Server-Prozess sind höchstens vier Terminals offen; ein anderes schließen. |
