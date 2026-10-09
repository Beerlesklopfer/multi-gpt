---
title: "Architektur"
slug: "architektur"
description: "Aufbau von MultiGPT: Browser, nginx, gunicorn und Django, PostgreSQL mit pgvector, Anbieter-APIs, LM Studio und MCP-Server."
lead: "Eine klassische, bewusst schlichte Web-App: ein Django-Server, eine Datenbank, keine weiteren Dienste wie Redis oder Celery."
menus:
  main:
    weight: 20
---

{{< architecture >}}

## Die Bausteine

- **Browser:** Die Oberfläche besteht aus Django-Templates und etwas Vanilla-JavaScript.
  Alle Dateien werden vom eigenen Server geladen, ohne CDN und ohne Node-Buildschritt.
- **nginx (optional):** sorgt für einen Hostnamen im Heimnetz und TLS. Für die
  Sprachfunktionen ist HTTPS Pflicht, weil Browser das Mikrofon sonst nicht freigeben.
  Ohne nginx liefert die App ihre statischen Dateien selbst aus (WhiteNoise).
- **gunicorn und Django:** gunicorn mit der Worker-Klasse `gthread` (Standard: 2 Prozesse
  × 8 Threads, Timeout 300 Sekunden), damit lange gestreamte Antworten andere Anfragen
  nicht blockieren. Antworten werden per Server-Sent Events gestreamt.
- **PostgreSQL mit pgvector:** die einzige Datenbank – für Konten, Chats und die Vektoren
  der eigenen Dokumente. Kein SQLite.
- **Worker:** ein zweiter Prozess aus derselben Codebasis, der Dokumente im
  Hintergrund indexiert. Er holt seine Aufträge aus einer Tabelle in PostgreSQL.
- **Anbieter-Adapter:** eine gemeinsame Schnittstelle, darunter ein Adapter für
  OpenAI-kompatible APIs (OpenAI, OpenRouter, LM Studio), einer für
  Anthropic und einer für Google Gemini. Umgesetzt mit `httpx` direkt gegen die HTTP-APIs.
- **MCP-Client:** verbindet sich über das offizielle Python-SDK mit
  MCP-Servern und reicht deren Werkzeuge an die Modelle weiter. Client, Verbindungstest
  und die Werkzeugschleife im Chat sind umgesetzt.

## Netz und Sicherheit

- Die App läuft nur im Heimnetz und ist aus dem Internet nicht erreichbar. Nach außen
  gehen nur HTTPS-Anfragen an die gewählten KI-Anbieter, im Heimnetz HTTP zu LM Studio.
- Alle Einstellungen kommen aus der Umgebung bzw. aus `/etc/multi-gpt/.env`, nie aus
  dem Repository.
- API-Keys und Zugangsdaten von MCP-Servern werden mit Fernet verschlüsselt in der
  Datenbank gespeichert und in der Verwaltung nur mit den letzten vier Zeichen angezeigt.
- Keys und Nachrichteninhalte erscheinen nicht in Logs.
- Der Dienst läuft unter einem eigenen Systemnutzer `multi-gpt` ohne Zugriff auf andere
  Freigaben des Servers.

## Technik

| Bereich | Wahl |
|---|---|
| Sprache | Python 3.12+ (Debian 13: 3.13) |
| Framework | Django 5.2 LTS |
| App-Server | gunicorn (`gthread`) |
| Datenbank | PostgreSQL mit pgvector |
| Statische Dateien | WhiteNoise |
| Login-Drosselung | django-axes |
| Paketierung | Debian-Paket mit dh-virtualenv, systemd; Docker als Ausweichweg |
