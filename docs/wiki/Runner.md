# Runner (Arbeitsumgebung für Modelle)

Der **Runner** gibt den Modellen eine echte Arbeitsumgebung: Dateien lesen, anlegen und gezielt
ändern, suchen, Befehle ausführen, testen und nachbessern – wie ein Programmierwerkzeug, aber
eingesperrt in einem Container auf **deinem** Rechner (PC oder Laptop im Heimnetz), nicht auf dem
Server.

- Der Container hat **kein Netz** und nur einen flüchtigen Arbeitsbereich (`/workspace` im
  Arbeitsspeicher). Endet die Sitzung, bleibt auf dem Rechner nichts zurück.
- Was das Modell erarbeitet, liegt als **Arbeitsstand** (git-Bundle mit der ganzen Historie) auf
  dem Server und wird mit der Datenbank gesichert. Jeder Werkzeugaufruf, der Dateien ändert, ist
  ein eigener Commit – jeder Stand ist zurückholbar.
- Der Runner verbindet sich **nur ausgehend** mit MultiGPT (`wss://<server>/runner/ws`). Auf
  deinem Rechner bleibt kein Port offen. Jeder Zugriff läuft über MultiGPT: Rechte, Kontingent,
  Rückfragen und Audit-Log prüft immer der Server.

## Voraussetzungen

- Recht **„Runner nutzen“** in deiner Rolle (Startwerte: Verwalter und Erwachsene ja,
  Jugendliche und Gäste nein; ändert ein Verwalter im Admin unter *Konten und Gruppen › Rollen*).
- **Podman** (rootless). Docker geht technisch auch, ist aber root-gleich und nicht empfohlen.
- MultiGPT ist über **HTTPS** erreichbar (siehe [nginx und TLS](nginx-und-TLS)). Bei einem
  selbstsignierten Zertifikat hält der Runner dessen Fingerprint fest; die Kopplungsseite trägt
  ihn automatisch ein.

## 1. Token erzeugen

1. Im Kontomenü (oben rechts) **Runner** öffnen.
2. Name eingeben (z. B. „Laptop“), Gültigkeit wählen, **Token erzeugen**.
3. Der Token (`mgrt_…`) ist **nur jetzt** sichtbar. Darunter stehen die Befehle und die fertige
   Quadlet-Datei – beides mit Kopierknopf.

Ein Token gehört zu genau einem Rechner. Er lässt sich jederzeit **widerrufen**; ein verbundener
Runner wird dann sofort getrennt. MultiGPT speichert nur einen Hash des Tokens.

## 2a. Linux (Podman rootless mit Quadlet)

```sh
systemctl --user enable --now podman.socket   # Podman-Socket des Nutzers
loginctl enable-linger $USER                   # läuft auch ohne Anmeldung
podman secret create mgpt_runner_token -       # Token einfügen, dann Strg+D
mkdir -p ~/.config/containers/systemd
# Quadlet-Datei von der Seite „Runner“ als
#   ~/.config/containers/systemd/multigpt-runner.container speichern, dann:
systemctl --user daemon-reload
systemctl --user start multigpt-runner
```

Prüfen: `systemctl --user status multigpt-runner` bzw. `journalctl --user -u multigpt-runner`.
Auf der Seite **Runner** steht der Rechner nach wenigen Sekunden als *verbunden*.

Der Token liegt als **Podman-Secret** vor, nie in der Quadlet-Datei oder in einer
Umgebungsvariable (die `podman inspect` zeigen würde). Neuer Token: Secret ersetzen
(`podman secret rm mgpt_runner_token`, dann neu anlegen) und den Dienst neu starten.

**Wichtig:** Nach „Token ungültig/widerrufen“ oder „Recht fehlt“ beendet sich der Agent mit
Code 78 und startet **nicht** von selbst neu – erst nach einem neuen Token.

## 2b. Windows und macOS (Podman machine)

Auf Windows und macOS laufen Container in einer kleinen Linux-VM, der **Podman machine**.
Der Runner läuft dort wie unter Linux:

1. [Podman Desktop](https://podman-desktop.io) oder die Podman-CLI installieren und eine
   Maschine anlegen und starten (Standard: **rootless**):

   ```sh
   podman machine init
   podman machine start
   ```

2. Token als Secret anlegen – das geht direkt vom Rechner aus, die CLI spricht mit der VM:

   ```sh
   podman secret create mgpt_runner_token -     # Token einfügen, dann Strg+D (Windows: Strg+Z, Enter)
   ```

3. In die VM wechseln und dort die Quadlet-Datei ablegen:

   ```sh
   podman machine ssh
   # in der VM:
   systemctl --user enable --now podman.socket
   mkdir -p ~/.config/containers/systemd
   vi ~/.config/containers/systemd/multigpt-runner.container   # Inhalt von der Seite „Runner“
   systemctl --user daemon-reload
   systemctl --user start multigpt-runner
   exit
   ```

4. Die VM muss laufen, damit der Runner verbunden ist. In Podman Desktop lässt sich „Maschine beim
   Start starten“ einschalten; ohne Podman Desktop `podman machine start` nach der Anmeldung.

Hinweise:

- Der Server muss aus der VM erreichbar sein (gleiche Adresse wie im Browser). Bei einer
  Adresse aus dem Heimnetz klappt das in der Regel ohne Weiteres.
- Mit WSL2 unter Windows geht auch der Linux-Weg (2a) in einer WSL-Distribution mit systemd.

## 3. Im Chat arbeiten

1. Arbeitsstand anlegen: **Runner › Arbeitsstände**, „Neuer Arbeitsstand“ (z. B. „Bildschirmschoner“).
2. Im Chat erscheint unter den Eingabeoptionen der Bereich **Runner**: Arbeitsstand wählen
   und den **Rückfrage-Modus**:

   | Modus | Dateien ändern | Befehle |
   |---|---|---|
   | fragen | mit Rückfrage | mit Rückfrage |
   | Änderungen automatisch | ohne Rückfrage | mit Rückfrage |
   | alles automatisch (Vorgabe ohne Netz) | ohne Rückfrage | ohne Rückfrage |

   `ssh` und `telnet` fragen **immer** nach, egal welcher Modus.

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

- Arbeitscontainer: nur das freigegebene Image (mit Digest), rootless, ohne Capabilities, ohne
  Host-Verzeichnisse, mit Grenzen für CPU, RAM, Prozesse und Ausgabe; Netz aus (ohne DNS).
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
| Agent beendet sich mit Code 78 | Token ungültig, abgelaufen oder widerrufen bzw. Rolle ohne Recht: neuen Token erzeugen, Secret ersetzen, Dienst starten. |
| „Laufzeit-Kontingent ausgeschöpft“ | Monatskontingent verbraucht; ein Verwalter kann es im Admin unter *Kosten und Abrechnung › Abrechnungskonten › Runner-Laufzeit* erhöhen. |
| „Der Arbeitsstand wird gerade in einer anderen Sitzung benutzt“ | Nur eine Sitzung je Arbeitsstand: die andere Sitzung beenden (oder warten, bis der Leerlauf sie stoppt). |
| Terminal: „Zu viele offene Terminals“ | Je Server-Prozess sind höchstens vier Terminals offen; ein anderes schließen. |
