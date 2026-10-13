---
title: "Funktionen"
slug: "funktionen"
description: "Was MultiGPT kann und können soll: viele Anbieter, LM Studio mit Modellverwaltung, Anhänge, Projekte, geteilte Chats, Familienkonten, Kontenrahmen, eigene Dokumente mit Zitaten, Websuche, Berechnungen, PDF-Blätter, Bilder, MCP-Werkzeuge, MultiGPT als MCP-Server und der Runner, an dem gerade gearbeitet wird."
lead: "Hier steht, was MultiGPT in Version 1 können soll, Stand Version 0.3.6. Umgesetzt sind Chatten mit vielen Anbietern und LM Studio samt Modellverwaltung, Anhänge, Projekte, geteilte Chats, das Kontextmenü im Chat, Familienkonten mit Kontenrahmen und Budgets, der Vergleichsmodus, MCP-Werkzeuge, MultiGPT als MCP-Server, Berechnungen, PDF-Blätter, eigene Dokumente mit Zitaten, die Websuche und die Bilderzeugung. Am Runner wird für Version 0.4.0 gearbeitet; Bildbearbeitung, Sprache und Scratchpad sind geplant. Jeder Abschnitt trägt seinen Status und den Meilenstein."
menus:
  main:
    weight: 10
---

Ausführliche Anleitungen stehen im [Wiki](https://github.com/Beerlesklopfer/multi-gpt/wiki).

## Anmeldung und Konten

{{< status "done" "1" >}}

- Login, Logout und Passwort ändern.
- Login-Drosselung: Nach fünf Fehlversuchen ist die Anmeldung für diesen Nutzernamen und
  diese Adresse 15 Minuten gesperrt.
- Keine Selbstregistrierung. Konten legt ein Verwalter an – per
  `mgpt-ctl createsuperuser`, `make user` (mit Rollenwahl) oder in der Verwaltung.
- **Persönliches Menü** (seit 0.3.5): Rechts oben stehen Profilbild und Name. Ein Klick
  öffnet Einstellungen, Profilbild, Mein Verbrauch, API-Keys, Familie, Verwaltung, Passwort
  und Abmelden – je nach Rechten. Ohne eigenes Profilbild erscheinen die Initialen; ein
  hochgeladenes Bild wird ohne Metadaten gespeichert.
- Die **Version** steht unten in der Seitenleiste, auf der Anmeldeseite und im Kopf der
  Verwaltung (seit 0.3.6).

## Chatten mit vielen Anbietern

{{< status "done" "3 / 4 / 5" >}}

- Chatliste in der Seitenleiste: neu, umbenennen, archivieren, löschen, Suche. Die
  Seitenleiste lässt sich ausblenden, Antworten nutzen die volle Breite, die Schriftgröße
  lässt sich mit A−/A+ ändern.
- Das Modell lässt sich **pro Nachricht** wählen. Antworten erscheinen gestreamt, ein runder
  Knopf im Eingabefeld sendet bzw. stoppt, eine Antwort lässt sich neu erzeugen.
- Anbieter werden per API-Key angebunden: OpenAI und alle OpenAI-kompatiblen Dienste
  (z. B. OpenRouter und LM Studio), dazu Anthropic und Google Gemini.
- In der Verwaltung prüft „Verbindung jetzt prüfen“ einen Anbieter und nennt bei Problemen
  die Ursache, etwa „Verbindung abgelehnt“, „API-Key abgelaufen“ oder „Basis-URL prüfen“.
  Nicht erreichbare Cloud-Anbieter sind im Chat ausgegraut.
- Eigene Nachrichten lassen sich bearbeiten wie in ChatGPT, auch mit Anhängen. Bearbeiten und
  „Neu erzeugen“ legen Versionen an, „‹ 1/2 ›“ schaltet zwischen ihnen um. Frühere Fassungen
  bleiben erhalten. Jede Nachricht hat einen Kopierknopf.
- Markdown mit Code-Hervorhebung und Kopierknopf. Die Bibliotheken sind lokal eingebunden,
  die Ausgabe wird bereinigt.
- Automatische Chattitel, System-Prompt pro Chat, Export als Markdown.
- **Eingabeverlauf** (seit 0.3.4): Pfeil hoch und runter holen frühere Nachrichten ins
  Eingabefeld zurück.
- **Antwort samt Verlauf löschen** (seit 0.3.4): Eine Antwort lässt sich mit ihrer Frage und
  allem danach löschen. Die Kosten bleiben gebucht.
- Schreibt die KI noch, lässt sich der Verlauf frei scrollen: Er folgt nur, solange man unten
  ist, sonst bringt „↓ Neueste“ zurück ans Ende.
- Bei Anthropic nutzt MultiGPT **Prompt-Caching** für Systemprompt, Werkzeuge und Verlauf
  (seit 0.3.4). Das spart Kosten bei längeren Chats.

## Kontextmenü und Zitieren im Chat

{{< status "done" "13" >}}

Seit 0.3.2: Markierten Text in Antworten, eigenen Nachrichten, Vergleichsspalten und
Quellenlisten weiterverwenden – per Rechtsklick, über eine Leiste unter der Markierung (auch
am Handy) oder mit Umschalt+F10.

- **Zitieren:** Der markierte Text landet als Zitat im Eingabefeld, mit seiner Herkunft
  (Modell, „Ich“ oder „Quelle [n]“). Mehrere Zitate kommen nacheinander in eine Nachricht.
- **Schnellaktionen:** Nachfragen, Erklären, Vereinfachen, Übersetzen, Gegenargumente,
  Belege suchen und Kopieren mit Quellenangabe.
- Gesendet wird nichts von selbst. Umschalt+Rechtsklick zeigt weiter das Menü des Browsers,
  das eigene Menü lässt sich in den Einstellungen abschalten.
- Anleitung: [Wiki: Chat-Bedienung](https://github.com/Beerlesklopfer/multi-gpt/wiki/Chat-Bedienung).

## Anhänge und Bild-Eingabe

{{< status "done" "5" >}}

- Bilder und Dokumente per Büroklammer anhängen, aus der Zwischenablage einfügen oder ins
  Eingabefeld ziehen. Bilder öffnen sich per Klick groß (Lightbox).
- Bilder gehen nur an Modelle mit dem Häkchen „Bilder verstehen“, über alle drei
  Anbieter-Adapter. Dokumente (PDF, DOCX, TXT, MD) gehen als Text mit.
- **Datenschutz:** Jedes Bild wird auf dem Server neu kodiert. Dabei fallen EXIF-Daten wie
  der Aufnahmeort (GPS), Kameradaten und eingebettete Kommentare weg. Der Dateityp wird am
  Inhalt geprüft, nicht an der Endung.
- Anhänge sind nur für den Chat sichtbar, zu dem sie gehören, und werden nur nach
  Rechteprüfung ausgeliefert.

## Formeln und SVG-Vorschau

{{< status "done" "5" >}}

- Mathematische Formeln werden mit KaTeX gesetzt. KaTeX liegt auf dem eigenen Server, es
  wird nichts aus dem Netz geladen.
- SVG-Code in Antworten zeigt eine **Vorschau**, mit Umschalter auf den Code und „Als Datei
  speichern“. Die Vorschau ist nur ein Bild: Skripte, Links und nachgeladene Inhalte im SVG
  wirken nicht.

## Projekte

{{< status "done" "5" >}}

Chats zu Projekten bündeln, wie in ChatGPT oder Claude – etwa „Umzug“ oder „Steuererklärung“.

- Ein Projekt gibt seinen Chats **Anweisungen**, ein **Standardmodell** und vorausgewählte
  **Sammlungen** eigener Dokumente mit.
- Projekte stehen in der Seitenleiste über den Chats, aufklappbar, anheftbar und archivierbar.
  Die Suche findet auch Projektnamen.
- Projekte sieht nur, wer sie angelegt hat.
- Anleitung: [Wiki: Projekte](https://github.com/Beerlesklopfer/multi-gpt/wiki/Projekte).

## Chats teilen

{{< status "done" "5" >}}

- Einen Chat mit einzelnen Konten oder einer Gruppe teilen. Je Freigabe gelten Rechte nach
  **RWUD**: Lesen (immer), Schreiben (mitschreiben), Bearbeiten (Nachrichten ändern,
  umbenennen) und Löschen.
- Empfänger finden geteilte Chats unter „Mit mir geteilt“ und können sie als eigene Kopie
  fortsetzen.
- Ein **Widerruf wirkt sofort**, auch für eine gerade laufende Antwort.
- **Kosten** trägt, wer eine Antwort auslöst, nicht der Besitzer des Chats. Welche Modelle
  jemand wählen darf, richtet sich nach seiner eigenen Rolle und seinem Budget.
- **Mitteilungen** (seit 0.3.6): Löscht oder archiviert ein Empfänger den Chat oder löscht
  er eine Antwort, bekommt der Besitzer eine Mitteilung im persönlichen Menü – mit Name und
  Chattitel, ohne Inhalte.
- Anleitung: [Wiki: Chats teilen](https://github.com/Beerlesklopfer/multi-gpt/wiki/Chats-teilen).

## Lokale Modelle mit LM Studio

{{< status "done" "4" >}}

[LM Studio](https://lmstudio.ai/docs) läuft auf einem PC im Heimnetz und stellt einen
OpenAI-kompatiblen Server bereit. MultiGPT bindet ihn als Anbieter an, startet oder weckt
ihn aber nicht.

- Eine Statusanzeige in der Kopfzeile zeigt, ob LM Studio gerade erreichbar ist. Der
  Browser fragt alle 30 Sekunden nach, solange der Tab sichtbar ist.
- Ist LM Studio aus, sind lokale Modelle ausgegraut. Ist es an, werden die Modelle
  angeboten, die LM Studio tatsächlich meldet, samt ihren Fähigkeiten.
- Fällt LM Studio mitten in einer Antwort aus, bleibt der bisherige Text erhalten, und die
  Ursache wird angezeigt – mit eigenem Hinweis, wenn der Kontext des Modells zu klein ist.
- Lokale Modelle kosten 0 € und bleiben auch bei ausgeschöpftem Budget nutzbar.
- Im echten Heimnetz im Einsatz: Chat (z. B. gpt-oss-20b), Embeddings (nomic-embed-text) und
  OCR (olmOCR) laufen über LM Studio.

**Modellverwaltung** (seit 0.3.2, für LM Studio ab 0.4): Auf Wunsch bestimmt MultiGPT selbst,
welche Modelle in LM Studio geladen sind.

- Vor Chat, Vergleich, Embeddings, OCR und Abbildungen lädt MultiGPT das passende Modell mit
  seinem Kontextfenster und entlädt dafür andere – **nie über ein Speicherbudget** für die
  Grafikkarte, mit einer Reserve für Anzeige und System.
- Angeheftete und gerade genutzte Modelle bleiben geladen, die Indexierung verdrängt das
  Chatmodell nicht. Passt etwas nicht, meldet der Chat „Zu wenig Grafikspeicher“, ein
  Indexierungslauf wartet.
- Modelle der Indexierung bleiben nur für den Lauf geladen (seit 0.3.4). Modelle, die
  30 Minuten nicht genutzt wurden, entlädt der Worker.
- Im Admin lässt sich je Modell das Kontextfenster wählen, daneben steht der erwartete
  Speicher; eine Seite zeigt die geladenen Modelle. Auf der Konsole: `mgpt-ctl lms`.
- Teilen sich mehrere MultiGPT-Installationen ein LM Studio, entlädt jede nur Modelle, die
  sie selbst geladen hat (seit 0.3.6).
- Anleitung: [Wiki: Anbieter und Modelle](https://github.com/Beerlesklopfer/multi-gpt/wiki/Anbieter-und-Modelle).

## Modelle und Fähigkeiten

{{< status "done" "4 / 4a" >}}

Die **Fähigkeiten-Matrix** in der Verwaltung zeigt je Anbieter alle Modelle mit ihren
Fähigkeiten, direkt änderbar:

- **Hauptart:** Chat, Bilderzeugung, Embedding, Spracherkennung, Sprachausgabe oder Musik.
- **Häkchen:** Werkzeuge, Bilder verstehen, Bilder bearbeiten.
- **MCP:** kein, alle oder ausgewählte MCP-Server (siehe unten).

Neue Modelle bekommen ihre Fähigkeiten **automatisch**: LM Studio meldet sie selbst, sonst
schätzt MultiGPT sie aus der Modell-ID. Für vorhandene Modelle holt
`sudo mgpt-ctl guess_capabilities --apply` das nach. Anleitung:
[Wiki: Anbieter und Modelle](https://github.com/Beerlesklopfer/multi-gpt/wiki/Anbieter-und-Modelle).

## Rollen, Gruppen und Budgets

{{< status "done" "2 / 6" >}}

Vier Startrollen, in der Verwaltung anpassbar:

| Rolle | Darf |
|---|---|
| Verwalter | Alles: Anbieter, Keys, Modelle, Konten, Rollen, Gruppen, Budgets, Verbrauch aller |
| Erwachsener | Alle freigegebenen Modelle und Funktionen, eigene Sammlungen, Teilen mit Gruppen |
| Jugendlicher | Nur freigegebene Modelle und Funktionen, fester System-Prompt der Rolle, Monatsbudget |
| Gast | Chat mit einem festgelegten Modell, kein Upload, kein Teilen, kleines Budget |

- Rechte werden auf dem Server geprüft, vor jeder Seite und vor jedem Anbieteraufruf –
  nicht nur durch Ausblenden in der Oberfläche. Eigene Rechte gibt es u. a. für
  Bilderzeugung, Berechnungen und PDF-Dokumente.
- **Privatsphäre:** Chats sind privat. Auch Verwalter sehen fremde Chats nicht, nur
  Verbrauchszahlen. Einzige Ausnahme: Für Jugendlichen-Konten gibt es die Option
  „Einsicht in Chats erlaubt“ (standardmäßig aus). Ist sie an, können Verwalter die Chats
  dieses Kontos lesen, aber nicht schreiben, und das Mitglied sieht dauerhaft einen Hinweis.
- **Gruppen:** Sammlungen und einzelne Chats lassen sich mit Gruppen teilen.
- Eine Seite „Familie“ (`/familie/`) für Verwalter: Konten anlegen und sperren, Rollen
  zuweisen, Passwörter zurücksetzen, Budgets setzen, die Einsicht schalten, Gruppen pflegen
  und den Verbrauch aller ansehen.

## Kontenrahmen und Kosten

{{< status "done" "6" >}}

Jeder Anbieter rechnet über ein **Abrechnungskonto** ab:

- **monetär** in EUR oder USD (Cloud-Anbieter), **nur Tokens** (lokale Modelle wie LM Studio)
  oder **Pauschale** (Abo, Anfragen und Tokens werden nur gezählt).
- **Preise mit Gültigkeit:** je Modell ab einem Datum, mit eigenen Preisen für Cache,
  Langkontext und Reasoning-Tokens. Bildpreise je Größe und Qualität. MultiGPT bringt keine
  Preise mit, der Verwalter pflegt sie selbst.
- **Wechselkurse** für USD-Konten, jede Antwort wird als **Buchung** mit Tokens, Betrag und
  Kurs festgehalten. Seit 0.3.6 holt der Worker den **EZB-Referenzkurs** USD→EUR
  werktags automatisch (abschaltbar), und Konten in EUR lassen sich auf USD umstellen; bisherige
  Buchungen bleiben unverändert.
- **Budgets** je Konto, Rolle und Person (Person vor Rolle): in Euro, bei Token-Konten als
  Kontingent. Ab 80 % erscheint ein Hinweis, ab 100 % sind die Modelle dieses Kontos bis zum
  Monatswechsel gesperrt. Kostenfreie Modelle bleiben nutzbar.
- **Mein Verbrauch** (`/verbrauch/`) zeigt jeder Person Tokens und Kosten je Konto und Modell,
  den Stand der Budgets und die letzten Monate.
- Anleitung: [Wiki: Kosten und Budgets](https://github.com/Beerlesklopfer/multi-gpt/wiki/Kosten-und-Budgets).

## Vergleichsmodus

{{< status "done" "6" >}}

Dieselbe Frage an zwei oder drei Modelle gleichzeitig stellen und die Antworten
nebeneinander lesen.

- Ein Schalter im Eingabefeld öffnet die Auswahl von zwei oder drei Modellen.
- Die Antworten erscheinen als Spalten und werden im Chat-Baum als Versionen derselben
  Nachricht gespeichert. Später schaltet „‹ 1/3 ›“ zwischen ihnen um.
- Wählt man eine Antwort aus, wird ihr Modell zum Standardmodell des Chats.
- Im Vergleich sind MCP-Werkzeuge aus, damit keine Rückfragen in mehreren Spalten
  gleichzeitig entstehen.

## Werkzeuge über MCP

{{< status "done" "4a" >}}

MultiGPT ist Client für das [Model Context Protocol](https://modelcontextprotocol.io/).
Modelle, die Werkzeuge unterstützen, rufen damit Funktionen aus angebundenen MCP-Servern auf.

- MCP-Server legt nur ein Verwalter an – lokal (`stdio`) oder per Streamable HTTP. Zugangsdaten
  werden verschlüsselt gespeichert.
- **Import:** Konfigurationen im Format `{"mcpServers": {…}}` (wie bei Claude Desktop, Cursor
  oder n8n) lassen sich einfügen. Einträge mit Platzhaltern statt echter Zugangsdaten werden
  deaktiviert angelegt.
- **Online-Status:** Jeder Server zeigt „online“, „offline“ mit Ursache, „ungeprüft“ oder
  „deaktiviert“; der Worker prüft regelmäßig.
- **Einstufung:** Eine Werkzeugtabelle zeigt alle Werkzeuge eines Servers. Werkzeuge, die
  etwas verändern oder nach außen senden, laufen erst nach einer Bestätigung im Chat. Neue,
  nicht eingestufte Werkzeuge gelten als bestätigungspflichtig.
- **Freigabe je Modell:** Der Verwalter legt fest, welche Modelle MCP nutzen dürfen (kein,
  alle, ausgewählte Server). Das schützt vor Modellen, die sich leichter zu ungewollten
  Aufrufen verleiten lassen. Geprüft wird auf dem Server, zusätzlich zur Freigabe der Rolle.
- Jeder Aufruf erscheint im Chat als aufklappbare Zeile mit Argumenten, Ergebnis und Dauer.
  Höchstens 10 Werkzeugrunden je Antwort, Zeitlimit je Aufruf.
- **Werkzeuge nach Bedarf** (seit 0.3.2): Bei vielen Werkzeugen – etwa n8n mit über 50 –
  bekommt das Modell nur einen Kern und sucht weitere mit `find_tools`. Dazu schätzt MultiGPT
  die Anfrage vor dem Senden, lässt notfalls ältere Nachrichten weg und meldet einen zu
  kleinen Kontext mit Zahlen.
- Verwalter sehen MCP-Server, die offline gehen, live in der Statusleiste (seit 0.3.5).
- Anleitung: [Wiki: MCP](https://github.com/Beerlesklopfer/multi-gpt/wiki/MCP).

## MultiGPT als MCP-Server

{{< status "done" "15" >}}

Seit 0.3.2 ist MultiGPT auch selbst MCP-Server: Unter `https://<hostname>/mcp/` steuern andere
Programme wie n8n, Claude Desktop oder eigene Agenten MultiGPT mit dem **API-Key** eines Kontos.

- **Werkzeuge:** Modelle fragen (`ask`), Dokumente hochladen, durchsuchen und lesen,
  Indexierung starten, verfolgen, pausieren und abbrechen, Verzeichnisquellen einlesen,
  erzeugte Dateien abholen und Werkzeuge wie `create_pdf` oder `run_python` direkt aufrufen.
  Die Ergebnisse kommen direkt als Text, Bild oder PDF zurück (seit 0.3.3).
- **Chats lesen** (seit 0.3.6): Mit dem Recht „Chats lesen“ listet und liest ein Key eigene
  und mit dem Konto geteilte Chats – nie fremde, auch nicht für Verwalter. Chats, die `ask`
  anlegt, landen standardmäßig im Projekt „API“.
- **Rechte:** Ein Key kann nie mehr als sein Konto. Jedes Mitglied legt Keys unter
  Einstellungen → API-Keys an, soweit die Rolle es erlaubt (an für Verwalter und Erwachsene).
  Keys werden nur gehasht gespeichert, sind widerrufbar und haben auf Wunsch ein Ablaufdatum.
- Jeder Aufruf landet ohne Inhalte im Audit-Log. Für ältere Clients gibt es zusätzlich den
  Transport HTTP+SSE unter `/mcp/sse`.
- Auf der Konsole: `mgpt-ctl apikey` und `mgpt-ctl index status --follow`.
- Anleitung: [Wiki: API-Keys](https://github.com/Beerlesklopfer/multi-gpt/wiki/API-Keys) und
  [Wiki: n8n](https://github.com/Beerlesklopfer/multi-gpt/wiki/n8n).

## Berechnungen in der Sandbox

{{< status "done" "4a" >}}

Sprachmodelle verrechnen sich leicht. Modelle mit Werkzeugen bekommen deshalb das eingebaute
Werkzeug `run_python` und rechnen exakt.

- **numpy** (Numerik, Statistik), **scipy** (seit 0.3.6), **sympy** (Gleichungen, Ableitungen, Integrale, exakte
  Brüche), **mpmath** (hohe Genauigkeit) und die Standardbibliothek.
- **Diagramme** mit matplotlib erscheinen als PNG oder SVG in der Antwort, SVG wird auf dem
  Server bereinigt.
- Im Chat sind Code und Ausgabe aufklappbar zu sehen.
- **Sicherheit:** Der Code gilt als nicht vertrauenswürdig und läuft in einer
  **bubblewrap-Sandbox** – ohne Netz, ohne Server-Dateien, ohne Schlüssel und Umgebung, mit
  Grenzen für Zeit, Speicher und Prozesse. Ohne funktionierende Sandbox wird das Werkzeug gar
  nicht angeboten.
- Eigenes Rollenrecht „Berechnungen ausführen“ (an für Verwalter, Erwachsene, Jugendliche).
  Anleitung: [Wiki: Berechnungen](https://github.com/Beerlesklopfer/multi-gpt/wiki/Berechnungen).

## Arbeitsblätter und Dokumente als PDF

{{< status "done" "4a" >}}

Seit 0.3.1: Wer um ein Blatt bittet, bekommt ein **druckfertiges PDF** (Werkzeug
`create_pdf`), das sich im Chat mit „Ansehen“ öffnet.

- **Lineaturen** für das Schreibenlernen: Lineatur 0 bis 4 mit Häuschen, liniert, Karo 5 und
  10 mm, maßhaltig in Millimetern.
- **Mathe:** Rechenaufgaben mit Lösungsblatt, Rechenkästchen, Einmaleins, Zahlenstrahl,
  Uhrzeiten. Aufgaben und Lösungen erzeugt **der Server**, nicht das Modell – so stimmen die
  Lösungen immer.
- **Sachunterricht** und allgemeine Arbeitsblätter mit Lückentext, Ankreuzaufgaben und
  Bildern, dazu Briefe, Einladungen und Tabellen.
- Zum Drucken „Tatsächliche Größe“ bzw. 100 % wählen, damit die Lineaturen stimmen.
- **Sicherheit:** Das PDF entsteht in einem eigenen Prozess ohne Netz- und Dateizugriff; nur
  eigene Anhänge dürfen als Bild hinein.
- Eigenes Rollenrecht „Dokumente erzeugen (PDF)“ (an für Verwalter, Erwachsene, Jugendliche).
  Anleitung: [Wiki: Dokumente erzeugen](https://github.com/Beerlesklopfer/multi-gpt/wiki/Dokumente-erzeugen).

## Fragen an eigene Dokumente (RAG)

{{< status "done" "7" >}}

- Sammlungen anlegen und Dokumente hochladen (PDF, DOCX, TXT, MD und Bilddateien, bis 25 MB
  je Datei), privat oder mit Gruppen geteilt – nur lesend oder mit Schreibrecht.
- Ein Hintergrunddienst (Worker) liest den Text, erkennt gescannte Seiten per OCR, teilt den
  Text in Abschnitte und berechnet Embeddings. Laufende Aufträge lassen sich abbrechen.
- **Mehrere Dokumente gleichzeitig** (seit 0.3.5, Standard 2) in eigenen Worker-Prozessen.
  Läufe lassen sich **pausieren und fortsetzen** (seit 0.3.6), Hochgeladenes kommt vor großen
  Läufen dran.
- Im Chat wählt man Sammlungen aus. Gesucht wird mit PostgreSQL und pgvector, auf Wunsch
  zusammen mit der deutschen Volltextsuche.
- Unter der Antwort stehen die verwendeten Dokumente mit Fundstelle. Ein PDF lässt sich im
  Browser ansehen und springt dabei zur richtigen Seite.
- **Abbildungen:** Auf Wunsch beschreibt ein Vision-Modell Bilder und Diagramme in PDFs und
  Word-Dokumenten. Die Beschreibung wird mit durchsucht und lässt sich wie Text zitieren.
- **Dokument-Werkzeuge:** Modelle mit Werkzeugen können Dokumente auflisten
  (`list_documents`), Angaben und Inhaltsverzeichnis abrufen (`document_info`) und Abschnitte
  im Wortlaut lesen (`read_document`). Sie lesen nur und sehen nur Sammlungen, die man selbst
  lesen darf.
- **Zugriffsschutz:** Abschnitte aus fremden privaten Sammlungen landen nie in einer Abfrage;
  der Zugriff wird in der Suchabfrage selbst geprüft. Verwalter sehen in der RAG-Übersicht
  nur Metadaten, keine Inhalte.
- **Komplett lokal:** Embeddings mit nomic-embed-text und OCR mit olmOCR laufen über
  LM Studio im Heimnetz, Tesseract auf dem Server springt auf Wunsch ein. So verlassen keine
  Dokumentinhalte das Haus.
- **Verzeichnisquellen:** Ein Verwalter kann eine Sammlung aus einem Ordner auf dem Server
  oder NAS füllen, der periodisch neu eingelesen wird. Erlaubt sind nur Ordner unterhalb
  der in `RAG_SOURCE_ROOTS` freigegebenen Verzeichnisse.
- Anleitung: [Wiki: RAG](https://github.com/Beerlesklopfer/multi-gpt/wiki/RAG).

## Zitieren

{{< status "done" "7" >}}

- Jede Dokumentquelle trägt ihre **Fundstelle**: Gliederungsabschnitt, Seite und Absatz,
  z. B. „Abschn. 7.5.3, S. 12, Abs. 3“.
- **Zitierstil je Konto:** DIN ISO 690, APA 7, Harvard, Chicago (Author-Date) oder MLA 9.
  „Zitat kopieren“ und „Literaturverzeichnis kopieren“ gibt es auch als BibTeX.
- Die Zitate setzt der Server nach festen Regeln, nicht das Modell.
- **Literaturangaben** lassen sich je Dokument pflegen: Bücher, Sammelbände, Kapitel,
  Artikel, Berichte und **Normen** mit Nummer und Ausgabe (z. B. „DIN EN ISO 9001:2015-11“).
  Eine DOI-Abfrage bei Crossref ist vorbereitet und standardmäßig aus.
- Anleitung: [Wiki: Zitieren](https://github.com/Beerlesklopfer/multi-gpt/wiki/Zitieren).

## Websuche und Seitenabruf

{{< status "done" "8" >}}

- Ein Schalter „Websuche“ im Eingabefeld. Die Treffer gehen als nummerierte Quellen an
  das Modell, die Links stehen unter der Antwort. Das funktioniert auch mit lokalen Modellen.
- Such-Backend ist ein selbst gehostetes [SearXNG](https://docs.searxng.org/) im Heimnetz.
- **Seiten abrufen und Websites durchsuchen:** Modelle mit Werkzeugen lesen einzelne Seiten
  (`fetch_url`) oder mehrere Seiten einer Website (`crawl_site`). URLs aus der Frage werden
  direkt gelesen. robots.txt wird beachtet.
- **Schutz:** Adressen im Heimnetz sind gesperrt, auch über Weiterleitungen (SSRF-Schutz).
  Abgerufene Seiten gelten als nicht vertrauenswürdig und gehen nur als markiertes
  Quellmaterial an das Modell, nicht als Anweisung. URLs erscheinen nicht im Log.
- **Erfundene Links:** Links in einer Antwort, die in keiner Quelle, keinem Werkzeugergebnis
  und nicht in der Frage vorkommen, bekommen ein ⚠. Der Knopf „Belege prüfen“ bereitet eine
  Nachfrage mit Websuche vor. Grundregeln für alle Modelle mahnen, nichts zu erfinden.

## Bilder erzeugen und bearbeiten

{{< status "partial" "9" >}}

**Umgesetzt (0.3.0): Bilderzeugung** mit OpenAI (gpt-image) oder Google Gemini.

- Chatmodelle mit Werkzeugen rufen dafür selbst das Werkzeug `generate_image` auf, statt
  Bilder als SVG-Code zu „zeichnen“.
- Modus **„Bild“** im Eingabefeld mit Format (quadratisch, hoch, quer) und Qualität.
- Kann das gewählte Modell keine Werkzeuge, bietet MultiGPT vor dem Senden „Mit Bildmodell
  erzeugen“ an – nie ohne Zustimmung.
- Bilder werden ohne Metadaten gespeichert, die Kosten je Bild gebucht. Die Beschreibung geht
  nur an den Anbieter des Bildmodells.
- Anleitung: [Wiki: Bilder](https://github.com/Beerlesklopfer/multi-gpt/wiki/Bilder).

**Geplant:**

- Bereich auf dem Bild markieren und ändern lassen (Inpainting) oder Varianten erzeugen.
- Klassische Bearbeitung (zuschneiden, skalieren, drehen, umwandeln, Text einsetzen,
  Collage) über einen mitgelieferten MCP-Server – auch per Anweisung wie
  „mach das Bild quadratisch“. Das Original bleibt erhalten.

## Sprache

{{< status "planned" "10" >}}

- Mikrofonknopf: Die Aufnahme wird zu Text, den man vor dem Senden korrigieren kann.
- Lautsprecherknopf an jeder Antwort, optional automatisches Vorlesen.
- Braucht HTTPS im Heimnetz, weil Browser das Mikrofon sonst nicht freigeben.

## Scratchpad

{{< status "planned" "13" >}}

- Material aus Chats, eigenen Dokumenten, dem Web, Werkzeugen, Bildern und Notizen sammeln,
  jeweils mit Herkunft und Zitierangabe.
- Daraus ein belegtes Gesamtdokument erzeugen: Das Modell schreibt nur aus den Einträgen,
  Kurzbelege und Literaturverzeichnis setzt MultiGPT, wörtliche Zitate werden geprüft.
- Vorab gibt es seit 0.3.2 das Kontextmenü im Chat (siehe oben); eigene Prompt-Vorlagen und
  „Ins Scratchpad“ folgen.

## Runner

{{< status "progress" "14" >}}

**In Arbeit für Version 0.4.0, noch nicht veröffentlicht.** Der Runner ist eine
Arbeitsumgebung für Modelle – nicht auf dem Server, sondern als **Container auf dem Rechner,
an dem der Browser läuft**. Darin arbeitet ein Modell wie ein Coding-Werkzeug, aber
eingesperrt.

- **Arbeitsweise:** Das Modell liest, sucht und ändert Dateien, führt Befehle aus, testet
  und bessert nach – in vielen Runden, bis die Aufgabe erledigt ist. Im Container stehen
  Python, git und Node bereit.
- **Eingesperrt:** Der Container hat standardmäßig kein Netz (je Rolle einschaltbar), kein
  Home-Verzeichnis und nur einen flüchtigen Arbeitsbereich im Arbeitsspeicher. Gestartet wird nur ein festes Image mit
  festen Grenzen für CPU, Speicher und Prozesse.
- **Arbeitsstand auf dem Server:** Nach jeder Änderung wird committet; der Arbeitsstand liegt
  als git-Bundle mit der ganzen Historie in der Datenbank von MultiGPT, wird mit ihr
  gesichert und lässt sich herunterladen. Auf dem Rechner bleibt nach der Sitzung nichts
  übrig.
- **Im Browser:** ein Terminal (auch für Programme, die eine Eingabe erwarten), eine Vorschau
  für HTML-Seiten im abgeschotteten Rahmen und die Aufgabenliste des Modells. Ergebnisse
  holt man auf Wunsch als Anhang in den Chat.
- **Geräte im Heimnetz:** per `ssh` oder `telnet`, aber nur zu Geräten, die der Verwalter
  freigibt, und mit Rückfrage vor jedem Befehl. Das Modell wählt Geräte nur nach Namen,
  die Zugangsdaten verwaltet der Server.
- **MultiGPT bleibt der zentrale Punkt:** Jeder Zugriff läuft über den Server, der Rechte,
  Rückfragen, Kontingent und Audit-Log prüft. Der Runner verbindet sich nur ausgehend über
  TLS und wird per Token gekoppelt (widerrufbar, mit Ablauf).
- **Zeitkontingent:** Container-Minuten je Rolle bzw. Person und Monat, Hinweis ab 80 %,
  Stopp bei 100 % – der Arbeitsstand bleibt.
- **Voraussetzung:** Podman rootless; unter Windows und macOS mit Podman machine.

## Was noch kommt: Version 0.4.0

{{< status "progress" >}}

Neben dem Runner sind diese Änderungen seit 0.3.6 fertig und kommen mit 0.4.0, sobald es nach
ausgiebigem Test erscheint:

- **Schnittstelle „LM Studio“:** eine eigene Anbieterart, bei der nur die Adresse des
  LM-Studio-Servers anzugeben ist. Die Modellverwaltung läuft künftig nur darüber.
- **Token pro Sekunde je Antwort:** Im Kopf jeder Antwort stehen die Geschwindigkeit und die
  Zeit bis zum ersten Token, von MultiGPT selbst gemessen – für alle Anbieter.
- **Austausch bei vollem Grafikspeicher:** Blockiert ein anderes Modell den Platz, bietet der
  Chat an, es zu entladen und das gewünschte zu laden – nur nach Bestätigung.
- **Denktiefe je Modell:** Die möglichen Stufen liest MultiGPT aus LM Studio, und je Modell
  lässt sich eine Vorgabe setzen.

## Bewusst nicht in Version 1

- Kein Starten oder Aufwecken von LM Studio aus der App heraus.
- Kein Sprachdialog in Echtzeit.
- Keine eigenen Bildmodelle auf dem Server.
- Keine Trennung mehrerer Haushalte in einer Installation.
