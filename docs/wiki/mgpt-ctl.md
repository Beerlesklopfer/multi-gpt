# mgpt-ctl – Verwaltungsbefehle

`mgpt-ctl` ist im Debian-Paket der Aufruf für alle Django-Verwaltungsbefehle von MultiGPT
(entspricht `manage.py` in der Entwicklung). Er läuft immer als Systemnutzer `multi-gpt`,
liest `/etc/multi-gpt/.env` und arbeitet in `/var/lib/multi-gpt`.

```bash
sudo mgpt-ctl <befehl> [optionen]
sudo mgpt-ctl help <befehl>     # alle Optionen eines Befehls
```

In der Entwicklung (Projektordner, `.venv`) gilt dasselbe mit
`.venv/bin/python manage.py <befehl>`.

## Nach einem Update

Das Paket migriert die Datenbank bei jedem Update selbst (postinst). Von Hand nötig ist nur:

| Wann | Befehl | Warum |
|---|---|---|
| Update auf 0.3 (einmalig) | `sudo mgpt-ctl guess_capabilities` (Vorschau), dann `sudo mgpt-ctl guess_capabilities --apply` | Vorhandene Modelle stehen bei „Werkzeuge“ auf „nein“. Ohne Werkzeuge bietet MultiGPT ihnen weder Websuche noch Seitenabruf noch Dokument-Werkzeuge an – das Modell antwortet dann „Ich kann nicht im Internet suchen“. Neue Modelle bekommen die Fähigkeiten automatisch. |
| Update auf 0.3 (einmalig) | im Admin „Alles neu indexieren“ oder `sudo mgpt-ctl reindex` | Erst neu indexierte Dokumente haben Absatz- und Abschnittsangaben (Zitieren) und – falls eingeschaltet – Abbildungsbeschreibungen. |

## Befehle von MultiGPT

### `index` – Indexierung beobachten und steuern

Zeigt Läufe (Hochladen, Neu-Indexierung, Verzeichnis einlesen), die Warteschlange des Workers und
steuert sie. Der Betrieb handelt dabei wie ein Verwalter; nur `upload` arbeitet im Namen eines
Kontos und prüft dessen Rechte wie die Oberfläche.

```bash
sudo mgpt-ctl index status                    # offene und zuletzt beendete Läufe, Worker, Aufträge
sudo mgpt-ctl index status --follow           # laufend aktualisieren wie top (Strg+C beendet)
sudo mgpt-ctl index status --follow --run 12  # einen Lauf verfolgen, endet mit dem Lauf
sudo mgpt-ctl index runs [--open] [--limit 50]
sudo mgpt-ctl index cancel 12                 # Lauf abbrechen
sudo mgpt-ctl index sources                   # Verzeichnisquellen mit ID
sudo mgpt-ctl index scan 3                    # Verzeichnisquelle 3 jetzt einlesen (Crawler)
sudo mgpt-ctl index upload Haus a.pdf b.docx --as anna   # Dateien in Annas Sammlung „Haus“
sudo mgpt-ctl index reindex [--collection 3] [--document 42] [--errors-only]
```

| Unterbefehl / Option | Bedeutung |
|---|---|
| `status --follow` / `-f` | Anzeige alle `--interval` Sekunden (Standard 2) neu, bis Strg+C. |
| `status --run <ID>` | Zähler eines Laufs (Dateien gefunden/geprüft/neu/geändert/entfernt, Dokumente eingereiht/fertig/Fehler/abgebrochen, Dauer). |
| `runs --open` | Nur laufende bzw. abbrechende Läufe. |
| `cancel <ID>` | Wartende Aufträge entfallen, laufende enden nach dem aktuellen Schritt; Indexiertes bleibt. |
| `scan <ID>` | Wie „Jetzt einlesen“ im Admin. Läuft schon ein Lauf der Quelle, bleibt es bei diesem. |
| `upload <Sammlung> <Dateien…> --as <Konto>` | Sammlung per ID oder Name (aus Sicht des Kontos); Typ- und Größenprüfung wie in der Oberfläche. |
| `reindex` | Wie der Befehl `reindex`, die Aufträge hängen an einem Lauf. |

Die Ausgabe enthält Namen von Sammlungen und Konten, aber keine Dateiinhalte und keine
Serverpfade von Dokumenten.

### `apikey` – API-Keys für den MCP-Server

Keys, mit denen andere Programme (z. B. n8n) MultiGPT steuern; siehe [API-Keys](API-Keys).

```bash
sudo mgpt-ctl apikey create anna --name "n8n NAS" --scopes docs.read,docs.write,index.control
sudo mgpt-ctl apikey create anna --name "Bericht" --scopes chat.ask,docs.read --expires 2027-06-30 --collection 3
sudo mgpt-ctl apikey list [anna]
sudo mgpt-ctl apikey revoke mgpt_ab12cd34ef     # oder die ID aus „list“
```

| Option | Bedeutung |
|---|---|
| `--scopes` | Kommagetrennt: `chat.ask`, `docs.read`, `docs.write`, `index.control`, `files.read`, `tools.run`, `usage.read` oder `all`. Höchstens, was die Rolle erlaubt. |
| `--expires` | `90d` (Standard), Anzahl Tage, Datum `JJJJ-MM-TT` (bis Tagesende) oder `never`. |
| `--collection <ID>` | Key nur für diese Sammlung(en), mehrfach möglich. |

`create` gibt den Key **einmal** auf stdout aus (Hinweise auf stderr), z. B.
`sudo mgpt-ctl apikey create … > key.txt`. Gespeichert wird nur ein Hash.

### `guess_capabilities` – Fähigkeiten der Modelle erkennen

Erkennt je KI-Modell die Hauptart (Chat, Bilderzeugung, Embedding, Spracherkennung,
Sprachausgabe, Musik), **Werkzeuge** und **Bilder verstehen**. Quelle: bei lokalen Anbietern
die Meldung von LM Studio (`/api/v0/models`, ab LM Studio 0.3.16), sonst eine gepflegte Liste
nach Modell-ID (im Zweifel „nein“). Die MCP-Freigabe je Modell wird **nie** geändert.

```bash
sudo mgpt-ctl guess_capabilities                      # Vorschau: was sich ändern würde
sudo mgpt-ctl guess_capabilities --apply              # Abweichungen speichern
sudo mgpt-ctl guess_capabilities --provider "LM Studio" --apply
```

| Option | Bedeutung |
|---|---|
| `--apply` | Abweichungen speichern. Ohne: nur anzeigen. |
| `--provider <Name oder ID>` | Nur Modelle dieses Anbieters. |

Dasselbe im Admin: „KI-Modelle“ → Modelle markieren → Aktion „Fähigkeiten automatisch
erkennen (Werkzeuge, Bilder)“ → Vorschau → „Übernehmen“. Einzelne Häkchen lassen sich
danach in der Matrix (Anbieter → KI-Modelle bzw. Liste der KI-Modelle) korrigieren; siehe
[Anbieter und Modelle](Anbieter-und-Modelle).

### `sync_models` – Modelllisten der Anbieter abgleichen

Ruft die Modelllisten der aktiven Cloud-Anbieter ab und legt fehlende Modelle **inaktiv** an
(mit erkannten Fähigkeiten). Vorhandene Modelle bleiben unverändert; lokale Anbieter
(LM Studio) gleicht die Statusprüfung von selbst ab.

```bash
sudo mgpt-ctl sync_models --dry-run
sudo mgpt-ctl sync_models --provider OpenAI
```

| Option | Bedeutung |
|---|---|
| `--provider <Name oder ID>` | Nur diesen Anbieter. |
| `--dry-run` | Nur anzeigen, nichts speichern. |

### `reindex` – Dokumente neu indexieren

Legt Indexierungsaufträge an; der Worker (`multi-gpt-worker.service`) arbeitet sie ab. Seit 0.4
hängen sie an einem Lauf: Die Ausgabe nennt dessen Nummer, `mgpt-ctl index status --follow --run
<Nummer>` zeigt den Fortschritt. Laufende Läufe sind außerdem im Admin unter „Dokumente (RAG)“ →
„Läufe“ zu sehen und abzubrechen.

```bash
sudo mgpt-ctl reindex                       # alle Dokumente
sudo mgpt-ctl reindex --collection 3        # nur Sammlung 3 (mehrfach möglich)
sudo mgpt-ctl reindex --document 42         # nur Dokument 42 (mehrfach möglich)
sudo mgpt-ctl reindex --errors-only         # nur Dokumente mit Status „Fehler“
```

### `create_account` – Familienkonto anlegen

```bash
sudo mgpt-ctl create_account anna --role adult --display-name "Anna"
```

Das Passwort wird verdeckt abgefragt. Rollen: `admin`, `adult`, `teen`, `guest`
(bzw. die im Admin angelegten Schlüssel).

### `fetch_ecb_rate` – Wechselkurs USD → EUR holen

Ruft den EZB-Referenzkurs USD ab und trägt fehlende EUR-Beträge in den Buchungen nach
(Kosten von Anbietern, die in USD abrechnen). Nur nötig, wenn der Kurs nicht von Hand im
Admin gepflegt wird; der automatische Abruf ist standardmäßig aus.

```bash
sudo mgpt-ctl fetch_ecb_rate
```

### `run_worker` – Hintergrundaufträge abarbeiten

Läuft im Paket als Dienst `multi-gpt-worker.service`; von Hand nur zur Fehlersuche:

```bash
sudo systemctl stop multi-gpt-worker
sudo mgpt-ctl run_worker --once     # alle fälligen Aufträge, dann Ende
sudo systemctl start multi-gpt-worker
```

| Option | Bedeutung |
|---|---|
| `--once` | Alle fälligen Aufträge abarbeiten und beenden. |
| `--poll <Sekunden>` | Wartezeit, wenn nichts fällig ist (Standard 3). |

## Django-Standardbefehle (Auswahl)

| Befehl | Zweck |
|---|---|
| `sudo mgpt-ctl migrate` | Datenbank migrieren (macht das Paket bei jedem Update selbst). |
| `sudo mgpt-ctl showmigrations` | Stand der Migrationen anzeigen. |
| `sudo mgpt-ctl createsuperuser` | Verwalterkonto anlegen. |
| `sudo mgpt-ctl changepassword <name>` | Passwort eines Kontos setzen. |
| `sudo mgpt-ctl check --deploy` | Sicherheits-Checkliste für den Betrieb. |
