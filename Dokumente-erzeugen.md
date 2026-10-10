# Blätter und Dokumente (PDF)

Wer im Chat um ein **Blatt** bittet, ein Arbeitsblatt, ein Schreibblatt mit Lineatur,
Karopapier, ein Rechenblatt, einen Brief, eine Einladung, eine Tabelle oder ein Formular zum
Ausdrucken, bekommt ein **druckfertiges PDF**, kein Bild. Dafür gibt MultiGPT Chatmodellen mit
dem Häkchen **„Werkzeuge“** das eingebaute Werkzeug `create_pdf` (seit 0.3.1).

Das PDF hängt an der Antwort als Datei mit zwei Knöpfen: **„Ansehen“** öffnet es im Browser,
ein Klick auf den Dateinamen lädt es herunter.

> **Drucken mit 100 %.** Im Druckdialog „Tatsächliche Größe“ bzw. „100 %“ wählen, nicht
> „An Seite anpassen“. Sonst stimmen die Maße der Lineaturen nicht mehr.

## Vorlagen und Beispielprompts

Das Modell wählt die Vorlage selbst. Diese Wünsche funktionieren zum Beispiel:

### Deutsch: Schreiben lernen

| Vorlage | Inhalt |
| --- | --- |
| Lineatur 0 | 4 Linien, Ober-, Mittel- und Unterband je 6 mm, A5 quer, 5 Zeilen, Häuschen |
| Lineatur 1 | 4 Linien, Bänder je 5 mm, 5 mm Abstand zwischen den Zeilen (Klasse 1) |
| Lineatur 2 | 4 Linien, Bänder je 4 mm, 5 mm Abstand (Klasse 2) |
| Lineatur 3 | 2 Linien, Mittelband 3,5 mm, 8 mm Abstand (Klasse 3) |
| Lineatur 4 | eine Grundlinie alle 10 mm (Klasse 4) |
| liniert | 9 mm Zeilenabstand mit 20 mm Korrekturrand (wie Lineatur 25) |
| karo5, karo10 | Karo 5 × 5 mm bzw. 10 × 10 mm |
| blanko_rand | leeres Blatt mit Korrekturrand |

Das **Häuschen** links und rechts zeigt Dach (Oberband), Haus (Mittelband) und Keller
(Unterband). Das Mittelband ist bei Lineatur 0 und 1 grau hinterlegt
(Kontrastlineatur), die Grundlinie ist verstärkt. Vorlagewörter stehen in der ersten Zeile in
DejaVu Sans, einer freien Druckschrift; eine Schulausgangsschrift ist nicht eingebettet, weil
deren Lizenzen meist nicht frei sind.

- „Erstelle mir ein Schreibblatt Lineatur 0 mit Häuschen, A5 quer“
- „Schreibheft-Seite Lineatur 1 mit dem Wort ‚Oma‘ als Vorlage, hellgrüner Hintergrund“
- „Karopapier 5 mm, A4, zwei Seiten“

Die Maße folgen Herstellerangaben (Brunnen Schreiblernheft Lineatur 0) und Lehrmittelseiten.
Die Norm DIN 16552-1 ist nicht frei zugänglich. Einzelne Hefte weichen leicht ab.

### Mathe

| Vorlage | Inhalt |
| --- | --- |
| aufgaben | Rechenaufgaben (+, −, ·, :) im Zahlenraum, mit Lösungsseite |
| rechenkaestchen | Aufgaben im 5-mm-Karo, eine Ziffer je Kästchen |
| einmaleins | Einmaleins-Reihen, der Reihe nach oder gemischt, optional mit Geteilt |
| zahlenstrahl | Zahlenstrahle mit Lücken zum Ausfüllen |
| uhr | Ziffernblätter: Uhrzeit ablesen oder Zeiger einzeichnen |

Die **Aufgaben und Lösungen erzeugt der Server**, nicht das Modell. So stimmen die Lösungen
immer. Derselbe Startwert (`seed`) liefert dieselben Aufgaben; er steht im aufgeklappten
Werkzeugaufruf. Eine Klassenstufe im Wunsch steuert Schriftgröße und Zahlenraum (Klasse 1: bis 20,
Klasse 2: bis 100, Klasse 3: bis 1000).

- „Arbeitsblatt Plus und Minus bis 20, 20 Aufgaben, mit Lösungsblatt“
- „Einmaleins der 3er- und 4er-Reihe gemischt, Klasse 2“
- „Uhrzeiten ablesen, volle und halbe Stunden“
- „Zahlenstrahl von 0 bis 100 in Zehnerschritten mit 4 Lücken“
- „Rechenkästchen: 24 Plus-Aufgaben bis 100“

### Sachunterricht und allgemeine Arbeitsblätter

Die Vorlage `arbeitsblatt` setzt den Inhalt des Modells kindgerecht: große Schrift,
nummerierte Aufgaben, Name und Datum oben. Bausteine:

- Lückentext: `___` wird eine Schreiblinie
- Ankreuzen: `[ ]` wird ein Kästchen
- Schreiblinien als Antwortfeld: eigene Zeile `[linien:3]` (Lineatur je nach Klasse)
- Kasten zum Zeichnen: eigene Zeile `[kasten:60 Zeichne eine Blume]` (Höhe in mm)
- Zuordnungstabellen als Markdown-Tabelle
- Bilder aus derselben Antwort, z. B. ein vorher mit `generate_image` erzeugtes Pflanzenbild

Beispiele:

- „Sachunterricht: Teile einer Pflanze beschriften“
- „Arbeitsblatt Klasse 3 zu den Jahreszeiten mit Lückentext und Ankreuzfragen“

### Briefe, Listen, Tabellen

Die Vorlage `text` setzt Markdown als normales Dokument (A4 oder A5, Letter, hoch oder quer)
mit Kopfzeile, Seitenzahlen „Seite X von Y“ und Tabellen, deren Kopf sich auf jeder Seite
wiederholt. Beispiele: „Einladung zum Kindergeburtstag zum Ausdrucken“, „Wochenplan als
Tabelle, A4 quer“, „Rezept für Pfannkuchen als Blatt“.

### Freie Geometrie

Was keine Vorlage abdeckt, kann das Werkzeug [`run_python`](Berechnungen) zeichnen und als PDF
speichern (z. B. `plt.savefig('blatt.pdf')` mit Maßen in mm). Die Datei erscheint wie ein
Diagramm in der Antwort.

## Voraussetzungen und Rechte

- **Pakete:** WeasyPrint ist im MultiGPT-Paket enthalten. Aus Debian kommen `libpango-1.0-0`,
  `libpangoft2-1.0-0`, `libharfbuzz-subset0` und `fonts-dejavu-core` (Abhängigkeiten des
  Pakets). Bei einer Installation aus dem Quellcode von Hand:

  ```sh
  sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core
  ```

  Fehlt Pango, bietet MultiGPT das Werkzeug nicht an.
- **Rolle:** Recht „Dokumente erzeugen (PDF)“ (Admin → Rollen). An für Verwalter,
  Erwachsene und Jugendliche, aus für Gäste.
- **Modell:** Häkchen „Werkzeuge“ (siehe [Anbieter und Modelle](Anbieter-und-Modelle)).
- **Kosten:** keine, das PDF entsteht lokal auf dem Server.

## Sicherheit und Grenzen

- Das Modell liefert nur Text (Markdown) bzw. Angaben zur Vorlage. Rohes HTML erscheint als
  Text, Links werden als Text mit sichtbarer Adresse gesetzt.
- Beim Setzen lädt MultiGPT **nichts aus dem Netz und nichts vom Dateisystem**. Bilder kommen
  nur aus Anhängen desselben Chats.
- Das PDF entsteht in einem eigenen Prozess mit Zeitlimit (60 s) und Speichergrenze, ohne
  Zugriff auf Schlüssel oder Datenbank.
- Grenzen: 200 KB Text, 50 Seiten, 5 PDFs je Antwort.
- Das Erzeugen dauert einige Sekunden (Start des Satzprogramms).
