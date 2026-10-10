# Berechnungen (run_python)

Sprachmodelle verrechnen sich, vor allem bei langen Zahlen, Brüchen, Gleichungen oder
Statistik. MultiGPT gibt Chatmodellen mit dem Häkchen **„Werkzeuge“** deshalb das eingebaute
Werkzeug `run_python`. Damit führt das Modell Python-Code aus und rechnet exakt, statt zu
schätzen. Der Code läuft abgeschottet in einer Sandbox auf dem Server.

## Was geht

- **Numerik** mit numpy: lineare Gleichungssysteme, Matrizen, Statistik, Zufallszahlen
- **Symbolik** mit sympy: Gleichungen lösen, Ableitungen, Integrale, Vereinfachen,
  exakte Brüche und Wurzeln
- **Hohe Genauigkeit** mit mpmath, z. B. π auf 50 Stellen
- **Standardbibliothek**: `math`, `fractions`, `decimal`, `statistics`, `datetime`, `itertools` …
- **Diagramme** mit matplotlib: Speichert der Code mit `plt.savefig('diagramm.png')` oder
  `plt.savefig('diagramm.svg')`, erscheint die Datei in der Antwort, mit Vorschau,
  Vergrößerung und Download. Offene Diagramme ohne `savefig` legt MultiGPT selbst als PNG ab.

Beispiele für Fragen: „Löse x² − 5x + 6 = 0“, „Wie viel Zins bringen 10 000 € bei 3,2 % über
7 Jahre mit monatlicher Verzinsung?“, „Zeichne sin(x) und cos(x) von 0 bis 2π“.

Im Chat steht der Aufruf als Zeile „Werkzeug run_python (Berechnungen)“. Aufgeklappt zeigt sie
den **Code** mit Syntax-Hervorhebung und die **Ausgabe**. Jeder Aufruf startet frisch, Variablen
aus früheren Aufrufen gibt es nicht. Ein Netz gibt es nicht, also auch kein `pip install`.

## Voraussetzungen

1. **bubblewrap** muss installiert sein. Das Debian-Paket zieht es als Abhängigkeit mit; von
   Hand: `sudo apt install bubblewrap`.
2. **Unprivilegierte User-Namespaces** müssen erlaubt sein. Unter Debian 13 ist das der Standard
   (`/proc/sys/kernel/unprivileged_userns_clone` = 1).
3. **Rolle:** Häkchen **„Berechnungen ausführen“** (Admin → Konten → Rollen). Es ist an für
   Verwalter, Erwachsene und Jugendliche und aus für Gäste. Neue Rollen haben es zunächst nicht.
4. **Modell:** Häkchen „Werkzeuge“. Die MCP-Freigabe des Modells spielt keine Rolle: Auch ein
   lokales Modell darf rechnen, die Sandbox schützt den Server.
5. **Einstellungen:** Admin → Chat → Chat-Einstellungen, Abschnitt „Berechnungen (run_python)“.
   Das Werkzeug ist dort standardmäßig eingeschaltet.

Fehlt bubblewrap oder funktioniert die Sandbox nicht, bietet MultiGPT das Werkzeug **gar nicht
an**. Es gibt keinen unsicheren Ersatz. Der Abschnitt im Admin zeigt dann „Sandbox verfügbar:
nein“ mit Ursache.

## Sicherheit

Der Code stammt vom Modell und gilt als **nicht vertrauenswürdig**. Eine Webseite oder ein
Dokument kann das Modell per Prompt-Injection zu bösartigem Code verleiten. Deshalb läuft der
Code nie im Server-Prozess, sondern in einem eigenen Prozess unter bubblewrap:

- **Kein Netz:** eigener, leerer Netz-Namespace, nur ein Loopback-Gerät ohne Dienste
- **Keine Server-Dateien:** sichtbar sind nur `/usr` (Python und Systembibliotheken, nur
  lesend) und die Rechenpakete. `/etc`, `/home`, `/var/lib/multi-gpt` (Datenbank-Dateien,
  Anhänge), `/etc/multi-gpt/.env`, der Datenbank-Socket, `/proc` sowie Django und MultiGPT
  selbst sind unsichtbar
- **Keine Geheimnisse:** Die Umgebung wird geleert (kein `SECRET_KEY`, keine `DATABASE_URL`)
- **Schreiben** nur in einen leeren Arbeitsordner und `/tmp`, beides im Speicher mit fester
  Größe und nach dem Lauf verworfen
- **Keine Rechte:** eigene Prozess- und Benutzer-IDs, alle Capabilities entzogen,
  `no_new_privs`, ein seccomp-Filter verbietet u. a. ptrace, mount, neue Namespaces, bpf,
  io_uring und Kernelmodule (x86_64 und aarch64)
- **Grenzen** für CPU-Zeit, Laufzeit, Speicher, Prozesse, Dateigröße und Ausgabe (siehe unten)
- **Diagramme** werden geprüft: PNG, JPEG und WebP neu kodiert (ohne Metadaten), SVG auf dem
  Server bereinigt (keine Skripte, keine Ereignis-Attribute, keine Links und Verweise nach
  außen). Angezeigt wird SVG nur als Bild, die Datei selbst ist ein Download mit strenger CSP.
- **Logs** enthalten nur Nachricht-Nr., Konto-Nr., Dauer und Exit-Status, nie Code oder Ausgabe

Eine Rückfrage vor jeder Ausführung ist deshalb nicht nötig. Wer sie trotzdem will, schaltet
**„Rückfrage vor Berechnungen“** in den Chat-Einstellungen ein.

## Grenzen

| Einstellung | Standard | Höchstwert | Wirkung |
|---|---|---|---|
| CPU-Zeit | 10 s | 60 s | Danach bricht der Lauf ab („CPU-Zeit-Grenze erreicht“) |
| Laufzeit gesamt | 20 s | 120 s | Wanduhr, zählt auch Warten; danach wird alles beendet |
| Speicher je Prozess | 512 MB | 4096 MB | Mehr Speicher führt zu `MemoryError` |
| Prozesse und Threads | 4 | 64 | Einschließlich Python und dem Start-Prozess; bremst Fork-Bomben |
| Dateigröße | 10 MB | 100 MB | je Datei im Arbeitsordner |
| Ausgabe | 64 KB | 1024 KB | Text an das Modell; der Rest wird gekürzt, weit darüber bricht der Lauf ab |

Je Lauf übernimmt MultiGPT höchstens 6 Bilder, je Antwort höchstens 12. Je Server-Prozess laufen
höchstens 2 Berechnungen gleichzeitig.

**Hinweis:** Manche symbolischen Rechnungen brauchen in sympy mehrere Sekunden, z. B. das
Gauß-Integral über ganz ℝ etwa 9 s. Brechen solche Rechnungen ab, die CPU-Zeit erhöhen.

## Prüfen: „Sandbox testen“

Admin → Chat → Chat-Einstellungen → Knopf **„Sandbox testen“** (oben rechts). MultiGPT rechnet
`print(1+1)` in der Sandbox und versucht dann Dinge, die scheitern müssen: eine
Netzverbindung, das Lesen von `/etc/passwd`, `/etc/multi-gpt/.env`, `/var/lib/multi-gpt` und
dem Programmordner, Schreiben unter `/usr`, Umgebungsvariablen des Servers und den Import von
Django. Jede Prüfung erscheint als grüne bzw. rote Meldung.

## Fehlersuche

**„Sandbox verfügbar: nein – bubblewrap (bwrap) ist nicht installiert“**
: `sudo apt install bubblewrap`, dann die Seite neu laden (der Status wird bis zu 1 min
  zwischengespeichert, „Sandbox testen“ prüft sofort neu).

**„Probelauf in bubblewrap fehlgeschlagen: bwrap: No permissions to create new namespace“**
: User-Namespaces sind gesperrt. Prüfen: `cat /proc/sys/kernel/unprivileged_userns_clone`
  (muss 1 sein) und `cat /proc/sys/user/max_user_namespaces` (größer 0). Unter Ubuntu
  zusätzlich die AppArmor-Sperre `kernel.apparmor_restrict_unprivileged_userns`. Wer die Unit
  per Drop-in mit `RestrictNamespaces=` härtet, muss `user pid net ipc uts mnt cgroup`
  erlauben oder die Zeile entfernen.

**„… loopback: Failed to create NETLINK_ROUTE socket“**
: Die Unit erlaubt `AF_NETLINK` nicht. Das Paket setzt
  `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK`. Ein eigenes Drop-in mit
  `RestrictAddressFamilies=` muss `AF_NETLINK` enthalten.

**„… Can't mount proc on /newroot/proc“**
: Tritt nur bei einer eigenen bwrap-Probe mit `--proc` auf. MultiGPT bindet kein /proc ein,
  weil die Unit-Härtung (`ProtectKernelTunables`, `ProtectKernelLogs`) es verbietet.

**Docker:** In Containern mit Standard-seccomp-Profil sind User-Namespaces gesperrt. Dort bleibt
das Werkzeug abgeschaltet.

**Die Antwort sagt „Abgebrochen: CPU-Zeit-Grenze …“**
: Die Rechnung war zu teuer. CPU-Zeit (und ggf. Laufzeit) in den Chat-Einstellungen erhöhen
  oder die Frage vereinfachen.

**Das Modell rechnet trotzdem im Kopf**
: Prüfen, ob das Modell „Werkzeuge“ hat und die Rolle „Berechnungen ausführen“ darf. Ein
  Satz im System-Prompt („Nutze für Rechnungen immer run_python“) hilft schwächeren Modellen.
