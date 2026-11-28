---
title: "Funktionen"
slug: "funktionen"
description: "Was MultiGPT kann, Stand 0.4.1: Chat mit vielen Anbietern, lokale Modelle über den eigenen Modellserver Eliezer oder LM Studio, eigene Dokumente mit Zitaten, Websuche, Bilder, Sprache, Werkzeuge über MCP, Runner, Familienkonten mit Rollen und Budgets."
lead: "Was MultiGPT heute kann, wie man es nutzt und wo die Grenzen liegen – Stand Version 0.4.1. Umgesetzt sind Chat mit vielen Anbietern, lokale Modelle über den eigenen Modellserver Eliezer oder LM Studio, eigene Dokumente mit Zitaten, Websuche, Bilder erzeugen und bearbeiten, Sprache, Werkzeuge über MCP, der Runner und Familienkonten mit Rollen und Budgets. Projekte teilen und die Verwaltung per mgpt-ctl sind fertig für die nächste Version. Geplant sind Musik, Hintergrund-Aufträge und das eigentliche Scratchpad."
menus:
  main:
    weight: 10
---

Jeder Abschnitt trägt eine Statusmarke und den Meilenstein aus der
[Roadmap]({{< relref "roadmap" >}}). „In Arbeit“ heißt: fertig, aber noch nicht
veröffentlicht. Was in welcher Version neu ist, steht auf der [Startseite]({{< relref "/" >}}).
Ausführliche Anleitungen stehen im [Wiki](https://github.com/Beerlesklopfer/multi-gpt/wiki).

## Chatten

{{< status "done" "3 / 5" >}}

Eine Oberfläche für alle angebundenen Modelle, am PC und am Handy.

- **Modell pro Nachricht** wählen. Antworten kommen gestreamt und lassen sich stoppen und neu
  erzeugen. Im Kopf jeder Antwort stehen Token pro Sekunde und die Zeit bis zum ersten Token.
- **Bearbeiten mit Versionen:** Eigene Nachrichten lassen sich ändern, auch mit Anhängen.
  Bearbeiten und „Neu erzeugen“ legen Versionen an, „‹ 1/2 ›“ schaltet um. Einzelne Versionen
  oder eine Antwort samt allem danach lassen sich löschen; gebuchte Kosten bleiben.
- **Darstellung:** Markdown mit Code-Hervorhebung, Formeln mit KaTeX, SVG als Vorschau (nur als
  Bild, ohne Skripte). Alle Bibliotheken liegen auf dem eigenen Server.
- **Anhänge:** Bilder und Dokumente (PDF, DOCX, TXT, MD) anhängen, einfügen oder hineinziehen.
  Bilder gehen nur an Modelle, die sie verstehen. Jedes Bild wird neu kodiert, EXIF-Daten wie
  der Aufnahmeort fallen weg.
- **Chatliste:** Suche, Archiv, automatische Titel, Export als Markdown, Eingabeverlauf mit
  Pfeil hoch und runter, Schriftgröße A−/A+.
- **Steuerung:** System-Prompt je Chat; Kreativität (Temperatur) und Denktiefe je Chat oder
  Projekt. Grundregeln des Verwalters gelten für alle Modelle und sind im Chat sichtbar.
- **Kontextmenü:** Markierten Text per Rechtsklick, Leiste oder Umschalt+F10 als Zitat mit
  Herkunft übernehmen, nachfragen, erklären, vereinfachen, übersetzen, Gegenargumente oder
  Belege suchen. Gesendet wird nichts von selbst.
- **Fehler** zeigt der Chat mit Ursache; „Technische Details“ nennen Status, Anbieter, Modell
  und Größe der Anfrage.

Anleitung: [Wiki: Chat-Bedienung](https://github.com/Beerlesklopfer/multi-gpt/wiki/Chat-Bedienung).

### Vergleichsmodus

{{< status "done" "6" >}}

Dieselbe Frage an zwei oder drei Modelle, die Antworten nebeneinander. Sie bleiben als
Versionen derselben Nachricht im Chat. MCP-Werkzeuge sind im Vergleich aus.

## Anbieter und Modelle

{{< status "done" "4 / 4a" >}}

- Angebunden wird per API-Key: OpenAI und OpenAI-kompatible Dienste (z. B. OpenRouter),
  Anthropic und Google Gemini, dazu die Anbieterart „LM Studio“ für lokale Modelle (siehe
  unten). Die Keys liegen verschlüsselt in der Datenbank.
- „Verbindung jetzt prüfen“ nennt bei Problemen die Ursache, etwa einen abgelaufenen Key. Nicht
  erreichbare Anbieter sind im Chat ausgegraut.
- Die **Fähigkeiten-Matrix** zeigt je Modell die Hauptart (Chat, Bild, Embedding,
  Spracherkennung, Sprachausgabe) und Häkchen für Werkzeuge, Bilder verstehen und Bilder
  bearbeiten. Neue Modelle bekommen ihre Fähigkeiten automatisch.
- Beim Modellwechsel weist der Chat bei Cloud-Modellen auf Kosten hin und stellt lokale Modelle
  kurz vor.
- Bei Anthropic nutzt MultiGPT Prompt-Caching, das spart bei längeren Chats Kosten.

Anleitung: [Wiki: Anbieter und Modelle](https://github.com/Beerlesklopfer/multi-gpt/wiki/Anbieter-und-Modelle).

## Lokale Modelle

{{< status "done" "4" >}}

{{< shot "chat-code" >}}

Lokale Modelle laufen auf einem PC mit Grafikkarte im Heimnetz, nicht auf dem MultiGPT-Server.
Dort läuft ein Modellserver mit OpenAI-kompatibler Schnittstelle. MultiGPT bindet ihn über die
Anbieterart „LM Studio“ an; es genügt die Adresse des Servers.

Der übliche Weg ist inzwischen **Eliezer**, ein eigener Modellserver in Rust. **LM Studio**
bleibt als Anbieter nutzbar.

- Eine Statusanzeige in der Kopfzeile zeigt, ob der Modellserver erreichbar ist. Ist er aus,
  sind lokale Modelle ausgegraut. MultiGPT startet oder weckt ihn nicht.
- Angeboten werden die Modelle, die der Server meldet, samt ihren Fähigkeiten.
- Lokale Modelle kosten nichts und bleiben auch bei ausgeschöpftem Budget nutzbar.
- Fällt der Server mitten in einer Antwort aus, bleibt der bisherige Text erhalten.

**Modellverwaltung** (am Anbieter einschaltbar): MultiGPT lädt das passende Modell vor Chat,
Vergleich, Embeddings und OCR selbst und entlädt dafür andere – nie über ein Speicherbudget
für die Grafikkarte, mit Reserve für Anzeige und System.

- Angeheftete Modelle bleiben geladen, die Indexierung verdrängt das Chatmodell nicht.
  Ungenutzte Modelle werden nach 30 Minuten entladen.
- Blockiert ein anderes Modell den Grafikspeicher, bietet der Chat an, es auszutauschen – nur
  nach Bestätigung.
- Denktiefe und Kontextfenster lassen sich je Modell vorgeben. Teilen sich mehrere
  Installationen einen Modellserver, entlädt jede nur ihre eigenen Modelle.

### Eigener Modellserver Eliezer

{{< status "partial" "16" >}}

Seit 0.4.1. Eliezer stellt Chat-, Bild-, Spracherkennungs-, Vorlese-, OCR- und
Embedding-Modelle bereit. Speicherbedarf und freien Platz liefert Eliezer selbst. Eine
**Hub-Suche** im Admin zeigt, welche Modellvariante in den Speicher passt.

Grenzen: Eliezer meldet nicht, ob ein Modell Werkzeuge kann – MultiGPT schätzt das aus dem
Modellnamen. Ein Bildauftrag belegt die Grafikkarte allein und verdrängt geladene Chatmodelle.
Den Betrieb über den Runner gibt es noch nicht.

## Eigene Dokumente und Zitieren

{{< status "done" "7" >}}

{{< shot "chat-rag" >}}

Fragen an eigene Unterlagen – Verträge, Anleitungen, Schulsachen – mit Quelle und Seite.

- **Sammlungen** mit PDF, DOCX, TXT, MD und Bilddateien (bis 25 MB je Datei), privat oder mit
  Gruppen geteilt, lesend oder mit Schreibrecht.
- Ein Hintergrunddienst liest den Text, erkennt gescannte Seiten per OCR und berechnet
  Embeddings. Mehrere Dokumente laufen gleichzeitig, Läufe lassen sich pausieren.
- Gesucht wird mit PostgreSQL und pgvector, auf Wunsch zusammen mit der deutschen Volltextsuche.
  Ein Vision-Modell beschreibt auf Wunsch Abbildungen, damit die Suche sie findet.
- **Komplett lokal möglich:** Embeddings und OCR über Eliezer oder LM Studio, Tesseract auf dem
  Server als Ersatz. Dann verlassen keine Dokumentinhalte das Haus.
- Modelle mit Werkzeugen können Dokumente auflisten, das Inhaltsverzeichnis abrufen und
  Abschnitte im Wortlaut lesen – nur in Sammlungen, die man selbst lesen darf.
- **Ordner vom Server oder NAS** kann ein Verwalter als Sammlung einbinden; sie werden
  regelmäßig neu eingelesen.
- **Zugriffsschutz:** Fremde private Sammlungen landen nie in einer Abfrage. Verwalter sehen in
  der RAG-Übersicht nur Metadaten.

**Zitieren:** Unter der Antwort stehen die Quellen mit Fundstelle, etwa „Abschn. 7.5.3, S. 12,
Abs. 3“. Ein PDF öffnet sich im Browser an der richtigen Seite. Die Zitate setzt der Server
nach festen Regeln im Stil des Kontos: DIN ISO 690, APA 7, Harvard, Chicago (Author-Date),
MLA 9 oder BibTeX. Literaturangaben lassen sich je Dokument pflegen, auch für Normen.

{{< shot "collections" >}}

Anleitungen: [Wiki: RAG](https://github.com/Beerlesklopfer/multi-gpt/wiki/RAG),
[Wiki: Zitieren](https://github.com/Beerlesklopfer/multi-gpt/wiki/Zitieren).

## Websuche

{{< status "done" "8" >}}

- Schalter „Websuche“ im Eingabefeld. Die Suchanfragen leitet MultiGPT aus dem Gespräch ab,
  unpassende Treffer fallen weg. Die Links stehen unter der Antwort – auch bei lokalen Modellen.
- Gesucht wird über ein selbst gehostetes [SearXNG](https://docs.searxng.org/); eine
  kommerzielle Such-API ist nicht angebunden.
- Modelle mit Werkzeugen lesen einzelne Seiten oder kleine Websites, auch URLs aus der Frage,
  und beachten robots.txt.
- **Schutz:** Adressen im Heimnetz sind gesperrt, auch über Weiterleitungen. Webtext geht nur
  als markiertes Quellmaterial an das Modell, nie als Anweisung.
- Links in einer Antwort, die in keiner Quelle stehen, bekommen ein ⚠ und den Knopf „Belege
  prüfen“.

## Bilder

{{< status "done" "9" >}}

- **Erzeugen** mit OpenAI (gpt-image), Google Gemini oder lokal mit FLUX.2 klein über Eliezer.
  Chatmodelle mit Werkzeugen rufen dafür selbst ein Bildmodell auf; dazu gibt es den Modus
  „Bild“ mit Format, Qualität und eigener Auflösung.
- **Bearbeiten:** angehängte Bilder von einem Bildmodell ändern lassen, mit Maske, wo der
  Anbieter das kann; Varianten erzeugen; am letzten Bild weiterarbeiten.
- **Ohne Bildmodell:** Das Werkzeug `transform_image` schneidet zu, skaliert, dreht, setzt Text,
  baut Collagen und wandelt Formate um – lokal, kostenlos, als neues Bild neben dem Original.
- Bilder werden ohne Metadaten gespeichert, die Kosten je Bild gebucht. Die Beschreibung geht
  nur an den Anbieter des Bildmodells.
- Eigenes Rollenrecht „Bilder erzeugen und bearbeiten“, ab Werk für Verwalter und Erwachsene.

Anleitung: [Wiki: Bilder](https://github.com/Beerlesklopfer/multi-gpt/wiki/Bilder).

## Sprache

{{< status "done" "10" >}}

Seit 0.4.1.

- Ein Mikrofon neben „Senden“ macht aus der Aufnahme Text, den man vor dem Senden korrigiert.
- Ein Lautsprecherknopf liest Antworten vor, auf Wunsch automatisch. Stimme und Vorlesen gelten
  je Konto. Escape beendet Aufnahme und Vorlesen.
- Erkennung und Vorlesen laufen lokal über Eliezer oder über OpenAI. Nötig sind je ein Modell
  für Spracherkennung und für Sprachausgabe.
- Der Browser gibt das Mikrofon nur über HTTPS frei.

## Werkzeuge

Modelle mit dem Häkchen „Werkzeuge“ können Funktionen aufrufen. Jeder Aufruf erscheint im
Chat als aufklappbare Zeile mit Argumenten, Ergebnis und Dauer. Welche Werkzeuge mitgehen,
wählt man in einer aufklappbaren Liste im Eingabefeld.

### MCP-Server anbinden

{{< status "done" "4a" >}}

MultiGPT ist Client für das [Model Context Protocol](https://modelcontextprotocol.io/).

- MCP-Server legt der Verwalter an, lokal (`stdio`) oder per HTTP. Konfigurationen im Format
  `{"mcpServers": {…}}` lassen sich einfügen. Jeder Server zeigt seinen Online-Status und lässt
  sich nach einem Ausfall im Chat neu verbinden.
- Werkzeuge, die etwas ändern oder nach außen senden, laufen erst nach Bestätigung im Chat.
- Welche Modelle MCP nutzen dürfen, legt der Verwalter je Modell fest (kein, alle, ausgewählte
  Server); lokale Modelle stehen ab Werk auf „kein“.
- Bei vielen Werkzeugen bekommt das Modell nur einen Kern und sucht weitere selbst, damit auch
  der Kontext lokaler Modelle reicht.

Anleitung: [Wiki: MCP](https://github.com/Beerlesklopfer/multi-gpt/wiki/MCP).

### Berechnungen

{{< status "done" "4a" >}}

Das Werkzeug `run_python` rechnet exakt mit numpy, scipy, sympy und mpmath und zeichnet
Diagramme mit matplotlib. Der Code läuft in einer bubblewrap-Sandbox ohne Netz, ohne
Server-Dateien und ohne Schlüssel, mit Grenzen für Zeit, Speicher und Prozesse. Ohne
funktionierende Sandbox wird das Werkzeug nicht angeboten. Anleitung:
[Wiki: Berechnungen](https://github.com/Beerlesklopfer/multi-gpt/wiki/Berechnungen).

### Arbeitsblätter und Dokumente als PDF

{{< status "done" "4a" >}}

Das Werkzeug `create_pdf` erzeugt druckfertige Blätter: Lineaturen 0 bis 4, liniert und
kariert, Rechenblätter mit Lösungen, Einmaleins, Zahlenstrahl, Uhrzeiten, Sachunterricht, dazu
Briefe, Einladungen und Tabellen. Aufgaben und Lösungen rechnet der Server, nicht das Modell.
Zum Drucken „Tatsächliche Größe“ wählen. Anleitung:
[Wiki: Dokumente erzeugen](https://github.com/Beerlesklopfer/multi-gpt/wiki/Dokumente-erzeugen).

### Scratchpad

{{< status "partial" "13" >}}

Dateien aus Werkzeugaufrufen (auch SVG, bereinigt) speichert MultiGPT dauerhaft; sie stehen an
der Antwort und im persönlichen Menü unter „Scratchpad“. Das eigentliche Scratchpad – Material
aus Chats, Dokumenten und dem Web mit Herkunft sammeln und daraus ein belegtes Dokument mit
geprüften Zitaten erzeugen – ist geplant.

### MultiGPT als MCP-Server

{{< status "done" "15" >}}

Andere Programme wie n8n, Claude Desktop oder eigene Agenten steuern MultiGPT unter
`https://<hostname>/mcp/` mit dem **API-Key** eines Kontos.

- Sie fragen Modelle, laden Dokumente hoch, durchsuchen und lesen sie, verfolgen und pausieren
  die Indexierung, holen erzeugte Dateien und rufen Werkzeuge wie `create_pdf` oder
  `transform_image` direkt auf. Mit dem Recht „Chats lesen“ lesen sie eigene und geteilte Chats.
- Ein Key kann nie mehr als sein Konto. Keys werden nur gehasht gespeichert, sind widerrufbar
  und auf Wunsch befristet. Jeder Aufruf landet ohne Inhalte im Audit-Log.

Anleitungen: [Wiki: API-Keys](https://github.com/Beerlesklopfer/multi-gpt/wiki/API-Keys),
[Wiki: n8n](https://github.com/Beerlesklopfer/multi-gpt/wiki/n8n).

## Runner

{{< status "done" "14" >}}

Seit 0.4.0. Der Runner ist eine Arbeitsumgebung für Modelle als **Container auf dem Rechner,
an dem der Browser läuft** – nicht auf dem Server. Darin arbeitet ein Modell wie ein
Coding-Werkzeug: Es liest, sucht und ändert Dateien, führt Befehle aus, testet und bessert nach.

- **Eingesperrt:** Podman rootless, festes Image mit Grenzen für CPU, Speicher und Prozesse,
  standardmäßig kein Netz und nur ein flüchtiger Arbeitsbereich im Arbeitsspeicher. Ein Volumen
  `/data` je Rechner hält Caches und Ergebnisse (Grenze je Rolle).
- **Arbeitsstand auf dem Server:** Jede Änderung wird committet; der Stand liegt als git-Bundle
  in der Datenbank von MultiGPT, wird mit ihr gesichert und lässt sich herunterladen.
- **Im Browser:** Terminal, Vorschau für HTML-Seiten im abgeschotteten Rahmen, Aufgabenliste des
  Modells und Container-Logs je Sitzung. Ergebnisse kommen auf Wunsch als Anhang in den Chat.
- **Pakete** (Debian, pip, npm, cargo, GitHub) lädt das Modell nur mit dem Rollenrecht
  „Paketquellen“ und nur über einen Proxy mit fester Liste erlaubter Quellen.
- **Geräte im Heimnetz** erreicht es per `ssh` oder `telnet` nur, wenn der Verwalter sie
  freigibt, und mit Rückfrage vor jedem Befehl.
- **Kontrolle:** Jeder Zugriff läuft über den Server (Rechte, Rückfragen, Audit-Log). Der
  Runner verbindet sich nur ausgehend über TLS und nimmt nur signierte Befehle an.
  Container-Minuten zählen gegen das Zeitkontingent „Runner-Laufzeit“ je Monat.
- **Installation:** Das Programm `multigpt-runner` (Linux amd64 und arm64) lädt man von
  MultiGPT herunter und betreibt es als Dienst des eigenen Nutzers. Nötig sind systemd und
  Podman rootless; unter Windows und macOS über WSL2 bzw. eine Linux-VM.

**Achtung:** Der Webserver im Container ist am Rechner unter den Ports 24080 und 24443
erreichbar, standardmäßig im ganzen Heimnetz und ohne Anmeldung. Auf den Rechner selbst
beschränkt ihn `MGPT_EXPOSE_BIND=127.0.0.1`.

Noch offen: Docker (nur Podman), die Übertragung nur der Änderungen statt des ganzen Stands
und die Live-Ausgabe der Werkzeugläufe im Chat-Terminal. Anleitung:
[Wiki: Runner](https://github.com/Beerlesklopfer/multi-gpt/wiki/Runner).

## Familie: Konten, Rollen, Privatsphäre

{{< status "done" "1 / 2 / 6" >}}

{{< shot "chat-phone" >}}

- **Konten** legt ein Verwalter an, eine Selbstregistrierung gibt es nicht. Nach fünf
  Fehlversuchen ist die Anmeldung 15 Minuten gesperrt.
- Im **persönlichen Menü** rechts oben: Einstellungen, Profilbild, Mein Verbrauch, API-Keys,
  Scratchpad und – je nach Rechten – Familie und Verwaltung.
- Rechte werden auf dem Server geprüft, vor jeder Seite und jedem Anbieteraufruf.

Vier Startrollen, in der Verwaltung anpassbar:

| Rolle | Darf |
|---|---|
| Verwalter | Alles: Anbieter, Modelle, Konten, Rollen, Gruppen, Budgets, Verbrauch aller |
| Erwachsener | Freigegebene Modelle und Funktionen, eigene Sammlungen, Teilen, API-Keys, Runner |
| Jugendlicher | Nur freigegebene Modelle und Funktionen, fester System-Prompt der Rolle, Budget |
| Gast | Chat mit festgelegtem Modell, kein Upload, kein Teilen, kleines Budget |

Jugendliche und Gäste haben anfangs keine Modelle; der Verwalter gibt sie je Rolle frei.

- **Privatsphäre:** Chats sind privat, auch Verwalter sehen fremde Chats nicht – nur
  Verbrauchszahlen. Einzige Ausnahme: Für Jugendlichen-Konten lässt sich „Einsicht in Chats
  erlaubt“ einschalten (ab Werk aus). Dann können Verwalter lesen, und das Mitglied sieht
  dauerhaft einen Hinweis.
- Die Seite **Familie** bündelt für Verwalter Konten, Rollen, Passwörter, Budgets, Gruppen und
  den Verbrauch aller.

### Anmeldenamen ohne Groß- und Kleinschreibung

{{< status "progress" >}}

Fertig für die nächste Version: „Anna“ und „anna“ sind dasselbe Konto, die Login-Sperre zählt
alle Schreibweisen zusammen. Gibt es beim Update zwei Konten, die sich nur in der Schreibweise
unterscheiden, bricht die Umstellung ab und nennt die Namen.

## Projekte und Teilen

{{< status "done" "5" >}}

- **Projekte** bündeln Chats, etwa „Umzug“ oder „Steuererklärung“. Ein Projekt gibt seinen
  Chats Anweisungen, ein Standardmodell und vorausgewählte Sammlungen mit.
- **Chats teilen** mit Konten oder Gruppen, mit Rechten nach RWUD: Lesen (immer), Schreiben,
  Bearbeiten, Löschen. Empfänger finden sie unter „Mit mir geteilt“. Ein Widerruf wirkt sofort.
- **Kosten** trägt, wer eine Antwort auslöst. Welche Modelle jemand nutzen darf, richtet sich
  nach seiner eigenen Rolle und seinem Budget.
- Löscht oder archiviert ein Empfänger etwas, bekommt der Besitzer eine Mitteilung – ohne
  Inhalte.

Anleitungen: [Wiki: Projekte](https://github.com/Beerlesklopfer/multi-gpt/wiki/Projekte),
[Wiki: Chats teilen](https://github.com/Beerlesklopfer/multi-gpt/wiki/Chats-teilen).

### Projekte teilen

{{< status "progress" >}}

Fertig für die nächste Version: ein ganzes Projekt mit Konten oder Gruppen teilen, mit
denselben Rechten wie bei Chats. Die Freigabe gilt für alle Chats des Projekts, auch für später
angelegte; einzelne Chats bleiben auf Wunsch privat. Empfänger sehen Anweisungen und Chats nur
lesend; ändern kann nur, wer das Projekt angelegt hat.

## Kosten und Budgets

{{< status "done" "6" >}}

{{< shot "usage" >}}

- Jeder Anbieter rechnet über ein **Abrechnungskonto** ab: in Euro oder Dollar, nur in Tokens
  (lokale Modelle) oder als Pauschale (Abo).
- **Preise** gelten je Modell ab einem Datum, mit eigenen Preisen für Cache und Reasoning.
  MultiGPT bringt keine Preise mit; der Verwalter kann Vorschläge aus der LiteLLM-Preisliste
  übernehmen. Der EZB-Kurs für Dollar kommt automatisch.
- Jede Antwort wird als **Buchung** mit Tokens, Betrag und Kurs festgehalten. Mit einem
  Admin-Schlüssel des Anbieters gleicht MultiGPT täglich mit dessen tatsächlichen Kosten ab.
- **Budgets** je Konto, Rolle und Person: Hinweis ab 80 %, ab 100 % sind die Modelle dieses
  Kontos bis zum Monatswechsel gesperrt. Kostenfreie Modelle bleiben nutzbar.
- **Mein Verbrauch** zeigt jeder Person Tokens, Kosten und den Stand ihrer Budgets.

Anleitung: [Wiki: Kosten und Budgets](https://github.com/Beerlesklopfer/multi-gpt/wiki/Kosten-und-Budgets).

## Installation und Betrieb

{{< status "partial" "12" >}}

- **Debian-Paket** für Debian 13 aus einer signierten apt-Paketquelle, Updates mit
  `apt upgrade`. Das Paket richtet PostgreSQL mit pgvector, das Schema und nginx mit TLS ein
  und migriert bei Updates selbst.
- Eigene Dienste für Weboberfläche, Hintergrundaufträge und Runner; Anhänge und Dokumente
  liefert nginx aus. Ein AppArmor-Profil für die Sandbox liegt bei.
- Verwaltungsbefehle über `mgpt-ctl`, etwa für Konten, API-Keys, Indexierung und
  Modellfähigkeiten. Die Version samt git-Stand steht in der Oberfläche.
- Docker gibt es als Ausweichweg, der Build ist aber ungetestet.

Schritt für Schritt: [Installation]({{< relref "installation" >}}).

### Verwaltung per mgpt-ctl

{{< status "progress" >}}

Fertig für die nächste Version: `mgpt-ctl` setzt Preise (aus der LiteLLM-Preisliste), Budgets
und Einstellungen von Anbietern und Modellen und prüft die Sandbox – ohne Admin-Oberfläche
(`prices`, `providers`, `models`, `budgets`, `sandbox_check`).

Ebenso **Sicherung:** `mgpt-ctl backup` legt Datenbank, Medien und Konfiguration in ein Archiv,
auf Wunsch täglich per Timer; Zurückspielen, Update und Entfernen beschreibt das Wiki.

## Geplant

### Musik

{{< status "planned" "11" >}}

Songs lokal über Eliezer erzeugen und beschreiben lassen, mit gespeicherten Einstellungen, um
ein Ergebnis zu wiederholen oder abzuwandeln.

### Hintergrund-Aufträge

{{< status "planned" "17" >}}

Antworten laufen im Worker weiter statt in der offenen Seite, dazu Aufgaben nach Zeitplan und
Meldungen, wenn etwas fertig ist oder auffällt.

## Grenzen

**Heute:**

- Mit OpenAI und lokalen Modellen läuft MultiGPT im echten Betrieb. Anthropic und Gemini sind
  bisher überwiegend simuliert getestet.
- Eine Testinstallation auf einem frischen Debian 13 steht aus.
- Der Runner läuft nur mit Podman, nicht mit Docker.

**Bewusst nicht in Version 1:**

- Kein Starten oder Aufwecken des Modellservers aus der App heraus.
- Kein Sprachdialog in Echtzeit – Sprache heißt Aufnahme zu Text und Vorlesen der Antwort.
- Keine Bildmodelle auf dem MultiGPT-Server selbst; lokale Bilder entstehen auf dem Rechner mit
  der Grafikkarte.
- Keine Trennung mehrerer Haushalte: Eine Installation gehört einer Familie.
