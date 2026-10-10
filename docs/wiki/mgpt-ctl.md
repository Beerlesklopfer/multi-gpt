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

Legt Indexierungsaufträge an; der Worker (`multi-gpt-worker.service`) arbeitet sie ab.
Laufende Läufe sind im Admin unter „Dokumente (RAG)“ → „Läufe“ zu sehen und abzubrechen.

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
