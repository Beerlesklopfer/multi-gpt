# MultiGPT-Wiki

**MultiGPT** ist ein lokales Multi-KI-Chatsystem für die Familie. Ein Django-Server im Intranet
bietet Chats mit verschiedenen KI-Anbietern an, online und lokal über LM Studio. Die Daten liegen in
PostgreSQL mit pgvector. MultiGPT läuft dauerhaft auf einem Rechner im Heimnetz (z. B. einem NAS mit
Debian 13 „trixie“) und wird als Debian-Paket `multi-gpt` installiert. Aus dem Internet ist es
nicht erreichbar.

Hier stehen Anleitungen zu Diensten, die MultiGPT ergänzen.

## Anleitungen

- [Fragen an eigene Dokumente (RAG)](RAG): Sammlungen, Upload, Status, Auswahl im Chat, Quellen
  und Datenschutz
  - [RAG einrichten](RAG-Einrichtung): Embedding-Modell, LM Studio, OCR, Worker, Neu-Indexieren
    und Fehlersuche
  - [Verzeichnisquellen](RAG-Verzeichnisquellen): NAS-Ordner regelmäßig in eine Sammlung
    einlesen
- [Konfiguration](Konfiguration): alle Schlüssel in `/etc/multi-gpt/.env` mit Standardwerten und Bedeutung
- [SearXNG als Such-Backend](SearXNG): wozu MultiGPT eine Suchmaschine braucht, welche Variante
  passt, Test, Eintragen in MultiGPT und Fehlersuche
  - [Variante a) SearXNG mit Docker](SearXNG-Docker)
  - [Variante b) SearXNG nativ auf Debian 13](SearXNG-Nativ)

## Weitere Informationen

- Quellcode und Fehlermeldungen: <https://github.com/Beerlesklopfer/multi-gpt>
- Website mit Beschreibung und Stand: <https://beerlesklopfer.github.io/multi-gpt/>
- Lizenz: AGPL-3.0-or-later
