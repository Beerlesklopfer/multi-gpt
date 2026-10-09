---
title: "Architecture"
description: "How MultiGPT is built: browser, nginx, gunicorn and Django, PostgreSQL with pgvector, provider APIs, LM Studio and MCP servers."
lead: "A classic, deliberately simple web app: one Django server, one database, no extra services such as Redis or Celery."
menus:
  main:
    weight: 20
---

{{< architecture >}}

## The building blocks

- **Browser:** the interface consists of Django templates and a little vanilla JavaScript.
  All files are served by your own server, without a CDN and without a Node build step.
- **nginx (optional):** provides a host name in the home network and TLS. HTTPS is
  required for the voice features, because browsers only allow the microphone over HTTPS.
  Without nginx the app serves its static files itself (WhiteNoise).
- **gunicorn and Django:** gunicorn with the `gthread` worker class (default: 2 processes
  × 8 threads, 300-second timeout), so long streamed answers do not block other requests.
  Answers are streamed via server-sent events.
- **PostgreSQL with pgvector:** the only database – for accounts, chats and later the
  vectors of your own documents. No SQLite.
- **Worker (planned):** a second process from the same code base that indexes documents in
  the background. It takes its jobs from a table in PostgreSQL.
- **Provider adapters:** one common interface with an adapter for
  OpenAI-compatible APIs (OpenAI, OpenRouter, LM Studio), one for Anthropic
  and one for Google Gemini. Implemented with `httpx` directly against the HTTP APIs.
- **MCP client:** connects to MCP servers via the official Python SDK and
  passes their tools on to the models. The client and connection test are done; the tool
  loop in the chat is in progress.

## Network and security

- The app runs only in the home network and cannot be reached from the internet. Outgoing
  traffic is limited to HTTPS requests to the chosen AI providers and, inside the home
  network, HTTP to LM Studio.
- All settings come from the environment or from `/etc/multi-gpt/.env`, never from the
  repository.
- API keys and MCP server credentials are stored encrypted with Fernet in the database and
  shown in the admin area only by their last four characters.
- Keys and message contents never appear in logs.
- The service runs as its own system user `multi-gpt` without access to other shares on
  the server.

## Tech

| Area | Choice |
|---|---|
| Language | Python 3.12+ (Debian 13: 3.13) |
| Framework | Django 5.2 LTS |
| App server | gunicorn (`gthread`) |
| Database | PostgreSQL with pgvector |
| Static files | WhiteNoise |
| Login throttling | django-axes |
| Packaging | Debian package with dh-virtualenv, systemd; Docker as a fallback |
