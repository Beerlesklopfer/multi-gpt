# Chat-Bedienung: Zitieren, Kontextmenü, Scrollen, Tastatur

Diese Seite beschreibt, wie man im Chat mit markiertem Text arbeitet, wie sich der Verlauf beim
Schreiben der KI verhält und welche Tasten es gibt.

## Kontextmenü für markierten Text

Markierter Text in einer Antwort, in einer eigenen Nachricht, in einer Spalte des Vergleichs oder
in der Quellenliste unter einer Antwort lässt sich direkt weiterverwenden. Das Menü öffnet sich
auf drei Wegen:

- **Rechtsklick** auf die Markierung. Auf Links, in Eingabefeldern und auf Knöpfen (z. B.
  „Kopieren“ an Codeblöcken) erscheint immer das normale Browsermenü.
- **Leiste unter der Markierung:** Nach dem Markieren erscheint darunter eine kleine Leiste mit
  „Zitieren“, „Nachfragen“ und „⋯“ (alle Einträge). Das ist auch der Weg am Handy: lange auf
  den Text tippen, Markierung anpassen, dann die Leiste nutzen. Sie steht unter der Markierung,
  damit sie das Auswahlmenü des Handys nicht verdeckt.
- **Tastatur:** Kontextmenü-Taste oder Umschalt+F10, solange Text markiert ist.

Das Browsermenü bleibt erreichbar: **Umschalt+Rechtsklick** zeigt immer das Menü des Browsers.
Das Menü selbst weist unten darauf hin.

### Einträge

| Eintrag | Wirkung |
|---|---|
| Im Eingabefeld zitieren | fügt die Markierung als Markdown-Zitat ins Eingabefeld ein |
| Nachfragen … | Zitat plus „Was meinst du mit „…“?“; das „…“ ist markiert und wird beim Tippen ersetzt |
| Erklären, Vereinfachen, Übersetzen, Gegenargumente | Zitat plus passende Anweisung |
| Belege suchen | Zitat plus Anweisung; schaltet die Websuche ein, sind Sammlungen gewählt, läuft die Dokumentsuche |
| Kopieren | kopiert die Markierung als Text |
| Kopieren mit Quellenangabe | nur, wenn die Markierung einen Verweis [n] enthält oder in der Quellenliste liegt: Text und Literaturangabe im eigenen Zitierstil (siehe [Zitieren](Zitieren)) |

**Nichts wird automatisch gesendet.** Alle Einträge bereiten nur die nächste Nachricht vor;
gesendet wird wie immer mit Enter. „Übersetzen“ übersetzt ins Deutsche, ist der Text schon
deutsch, ins Englische.

### Mehrere Zitate in einer Nachricht

Jedes weitere Zitat wird **angehängt**, nie ersetzt. Die Zitate stehen in der Reihenfolge, in der
sie zitiert wurden, getrennt durch eine Leerzeile. Eigener Text im Eingabefeld bleibt erhalten:
Ein neues Zitat kommt hinter das letzte Zitat, eigener Text danach bleibt dahinter. Der Cursor
steht am Ende, man schreibt die Frage also direkt unter die Zitate.

Beispiel nach drei Zitaten aus zwei Antworten:

```
> **gpt-5.5:**
> Erster zitierter Absatz.

> **claude-sonnet:**
> Zweiter zitierter Absatz,
> über zwei Zeilen.

> **gpt-5.5:**
> Dritter Absatz.

Wie passen diese drei Aussagen zusammen?
```

Jede Zeile beginnt mit `> `. Die erste Zeile nennt die **Herkunft**, damit das Modell weiß, wer
was gesagt hat:

- Antworten: der Name des Modells (wichtig, wenn im Chat mehrere Modelle antworten oder im
  Vergleich),
- eigene Nachrichten: „Ich“ (aus Sicht des Schreibenden; „Du“ würde das Modell auf sich beziehen),
  in geteilten Chats der Name der Person,
- Quellenliste: „Quelle [n]“.

Formeln kommen als TeX (`$…$`) ins Zitat, Code als Codeblock.

Über dem Eingabefeld erscheint eine kleine Liste **„Zitate (n)“** mit einem × je Zitat zum
Entfernen. Maßgeblich ist der Text im Eingabefeld: Wer ein Zitat dort von Hand ändert oder
löscht, sieht es in der Liste nicht mehr.

### Bearbeiten, Vergleich, nur lesen

- Ist der Editor einer eigenen Nachricht offen („Bearbeiten“), zitiert das Menü in diesen Editor.
  Zuletzt benutzt gewinnt: Wer danach ins Eingabefeld unten klickt, zitiert wieder dorthin.
- Im Vergleich lassen sich Spalten genauso markieren; die Herkunft ist das Modell der Spalte.
- In einem geteilten Chat, den man nur lesen darf, gibt es kein Eingabefeld. Dann bietet das Menü
  nur „Kopieren“ (und ggf. „Kopieren mit Quellenangabe“).

### Abschalten

Unter **Einstellungen → Chat → Eigenes Kontextmenü im Chat** lässt sich das Menü abschalten. Dann
gibt es weder Menü noch Leiste noch Umschalt+F10, überall erscheint das Browsermenü.

## Scrollen, während die KI schreibt

Der Verlauf folgt der Antwort nur, **solange man unten ist** (bis etwa 40 Pixel über dem Ende).
Wer nach oben scrollt – Mausrad, Wischen, Bild auf, Pfeiltasten oder Scrollbalken –, bleibt dort
stehen; die Antwort schreibt unten weiter. Dann erscheint unten mittig über dem Eingabefeld der
Knopf **„↓ Neueste“**. Ein Klick springt ans Ende, und der Verlauf folgt wieder. Wer von Hand
wieder ganz nach unten scrollt, erreicht dasselbe.

Das gilt auch für den Vergleich (die Spalten stehen im selben Verlauf) und für das Neuladen des
Verlaufs nach dem Ende der Antwort: Wer oben liest, wird nicht verschoben. Ist im System
„Bewegung reduzieren“ eingestellt, springt „↓ Neueste“ ohne Animation.

## Persönliches Menü und Profilbild

Rechts oben steht das eigene Profilbild mit Namen; ein Klick öffnet das persönliche Menü:
**Mitteilungen**, **Einstellungen**, **Profilbild ändern**, **Mein Verbrauch**, **API-Keys** (wenn
die Rolle sie erlaubt), **Familie** und **Verwaltung** (nur Verwalter), **Passwort ändern** und
**Abmelden**. Escape oder ein Klick daneben schließt es, Pfeil hoch/runter wandert durch die
Einträge. Auf dem Handy steht nur das Bild.

**Mitteilungen:** Gibt es ungelesene, zeigt eine Zahl am Profilbild und am Eintrag
„Mitteilungen“, wie viele es sind. Die Seite listet sie mit Datum; „Als gelesen markieren“ bzw.
„Alle als gelesen markieren“ nimmt die Zahl weg, „Zum Chat“ öffnet den betroffenen Chat (nicht bei
gelöschten Chats). Gelesene Mitteilungen werden nach 90 Tagen gelöscht. Derzeit gibt es sie, wenn
jemand deinen geteilten Chat löscht, archiviert oder eine Antwort daraus löscht
([Chats teilen](Chats-teilen.md#mitteilungen-an-den-besitzer)).

**Profilbild:** Einstellungen → „Profilbild“: JPG, PNG oder WebP bis 10 MB. MultiGPT schneidet es
quadratisch zu, verkleinert es auf 256 Pixel und speichert es ohne Metadaten (Ort, Kamera). Ohne
Bild erscheinen die Initialen auf einer Farbe, die fest zum Konto gehört. Angemeldete
Familienmitglieder können die Profilbilder der anderen sehen.

## Antwort löschen

Der Papierkorb unter einer Antwort löscht **die Frage dazu, alle Versionen dieser Antwort und den
ganzen Verlauf danach**. Der Chat geht an der Stelle davor weiter. Gab es die Frage in mehreren
Fassungen (Bearbeiten), bleiben die anderen Fassungen stehen und werden angezeigt. Vorher kommt
eine Rückfrage; rückgängig machen lässt sich das Löschen nicht.

- Erzeugte Dateien und Anhänge der gelöschten Nachrichten werden mit gelöscht.
- **Kosten bleiben gebucht** (Verbrauch und Budget ändern sich nicht).
- In geteilten Chats braucht das Löschen das Recht **D** („Löschen“), wie das Löschen des ganzen
  Chats; es gilt für alle, die den Chat sehen.
- Während eine Antwort läuft, ist der Knopf gesperrt.

## Tastaturkürzel

| Taste | Wo | Wirkung |
|---|---|---|
| Enter | Eingabefeld, Editor | senden |
| Umschalt+Enter | Eingabefeld, Editor | Zeilenumbruch |
| Pfeil hoch / runter | Eingabefeld (erste bzw. letzte Zeile) | frühere eigene Nachrichten dieses Chats zurückholen, wie in einer Shell; ganz unten kommt der angefangene Text wieder. Gespeichert wird dafür nichts im Browser |
| Strg+V | Eingabefeld | Bild aus der Zwischenablage als Anhang |
| Escape | Editor einer Nachricht | Bearbeiten abbrechen |
| Kontextmenü-Taste, Umschalt+F10 | markierter Text im Chat | Kontextmenü öffnen |
| ↑ ↓ | Kontextmenü | vorheriger bzw. nächster Eintrag (am Ende geht es oben weiter) |
| Pos1, Ende | Kontextmenü | erster bzw. letzter Eintrag |
| Buchstabe | Kontextmenü | nächster Eintrag mit diesem Anfangsbuchstaben |
| Enter, Leertaste | Kontextmenü | Eintrag ausführen |
| Escape | Kontextmenü | schließen; Fokus und Markierung kehren zurück |
| Tab | Kontextmenü | schließen und weiter zum nächsten Element |
| Escape | Leiste unter der Markierung | Leiste ausblenden |
| Bild auf/ab, Pos1, Ende | Verlauf | scrollen (Bild auf hält das Mitscrollen an) |
| Umschalt+Rechtsklick | überall | Browsermenü statt eigenem Menü |
