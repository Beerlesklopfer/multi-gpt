# MultiGPT-Wiki

**MultiGPT** ist ein lokales Multi-KI-Chatsystem für die Familie. Ein Django-Server im Intranet
bietet Chats mit verschiedenen KI-Anbietern an, online und lokal über LM Studio. Die Daten liegen in
PostgreSQL mit pgvector. MultiGPT läuft dauerhaft auf einem Rechner im Heimnetz (z. B. einem NAS mit
Debian 13 „trixie“) und wird als Debian-Paket `multi-gpt` installiert. Aus dem Internet ist es
nicht erreichbar.

Hier stehen Anleitungen zu Diensten, die MultiGPT ergänzen.

## Anleitungen

- [Chat-Einstellungen](Chat-Einstellungen): Grundregeln für alle Modelle, Anzeige im Chat
  (auch der eigene Rollen-Prompt), Kreativität (Temperatur) und Denktiefe (Reasoning) je Chat,
  Projekt und Standard, Abbildung je Anbieter
- [Projekte](Projekte): Chats gruppieren, Anweisungen, Standardmodell und Sammlungen je Projekt,
  verschieben, Archiv, Löschen mit oder ohne Chats
- [Chats teilen](Chats-teilen): Chats mit Konten oder Gruppen teilen, Rechte Lesen, Schreiben,
  Bearbeiten, Löschen (RWUD), Kopie, Kosten und Datenschutz
- [Anbieter und Modelle](Anbieter-und-Modelle): Fähigkeiten-Matrix (Werkzeuge, Bilder, MCP),
  automatische Erkennung, welche Modelle MCP-Server nutzen dürfen
- [MCP-Server](MCP): Server anlegen oder aus JSON importieren, Online-Status und Ursachen,
  Werkzeuge einstufen (mit/ohne Rückfrage), Freigabe je Modell
- [Bilder erzeugen](Bilder): Bildmodell einrichten (z. B. OpenAI `gpt-image-1`), Werkzeug
  `generate_image`, Modus „Bild“, Hinweis bei Modellen ohne Werkzeuge, Kosten je Bild
- [Berechnungen](Berechnungen): Werkzeug `run_python` (numpy, sympy, mpmath, Diagramme mit
  matplotlib), Sandbox mit bubblewrap, Grenzen, „Sandbox testen“, Fehlersuche
- [Blätter und Dokumente (PDF)](Dokumente-erzeugen): Werkzeug `create_pdf` für Arbeitsblätter,
  Lineaturen mit Häuschen, Rechenblätter mit Lösungen, Uhr, Zahlenstrahl, Briefe und Tabellen
- [Kosten und Budgets](Kosten-und-Budgets): Kontenrahmen (Geld, nur Tokens, Pauschale),
  Preise mit Historie, Wechselkurse, Budgets je Konto, Beispiele für Anthropic, OpenAI und LM Studio
- [Fragen an eigene Dokumente (RAG)](RAG): Sammlungen, Upload, Status, Auswahl im Chat, Quellen
  und Datenschutz
  - [RAG einrichten](RAG-Einrichtung): Embedding-Modell, LM Studio, OCR, Worker, Neu-Indexieren
    und Fehlersuche
  - [Verzeichnisquellen](RAG-Verzeichnisquellen): NAS-Ordner regelmäßig in eine Sammlung
    einlesen
  - [Zitieren](Zitieren): Zitierstile, Fundstelle (Abschnitt, Seite, Absatz),
    Literaturangaben pflegen, Normen und Verlagstexte
- [Konfiguration](Konfiguration): alle Schlüssel in `/etc/multi-gpt/.env` mit Standardwerten und Bedeutung
- [mgpt-ctl](mgpt-ctl): alle Verwaltungsbefehle; **nach dem Update auf 0.3 einmalig** `sudo mgpt-ctl guess_capabilities --apply`
- [nginx und TLS](nginx-und-TLS): was das Paket für nginx einrichtet, Hostname und Zertifikat
  ändern, Standard-Server, Upload-Grenze und Fehlersuche
- [SearXNG als Such-Backend](SearXNG): wozu MultiGPT eine Suchmaschine braucht, welche Variante
  passt, Test, Eintragen in MultiGPT und Fehlersuche
  - [Variante a) SearXNG mit Docker](SearXNG-Docker)
  - [Variante b) SearXNG nativ auf Debian 13](SearXNG-Nativ)

## Weitere Informationen

- Quellcode und Fehlermeldungen: <https://github.com/Beerlesklopfer/multi-gpt>
- Website mit Beschreibung und Stand: <https://beerlesklopfer.github.io/multi-gpt/>
- Lizenz: AGPL-3.0-or-later
