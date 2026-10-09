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
- **nginx:** part of the Debian package and runs on the same machine. It provides the host
  name in the home network and TLS, redirects HTTP to HTTPS, serves the static files directly
  and passes streamed answers through unbuffered. HTTPS is required for the voice features,
  because browsers only allow the microphone over HTTPS. In Docker and during development the
  app serves its static files itself (WhiteNoise).
- **gunicorn and Django:** gunicorn listens on localhost (`127.0.0.1`) only, with the
  `gthread` worker class (default: 2 processes × 8 threads, 300-second timeout), so long
  streamed answers do not block other requests.
  Answers are streamed via server-sent events.
- **PostgreSQL with pgvector:** the only database – for accounts, chats and the vectors of
  your own documents. No SQLite.
- **Worker:** a second process from the same code base that indexes documents in
  the background. It takes its jobs from a table in PostgreSQL.
- **Provider adapters:** one common interface with an adapter for
  OpenAI-compatible APIs (OpenAI, OpenRouter, LM Studio), one for Anthropic
  and one for Google Gemini. Implemented with `httpx` directly against the HTTP APIs.
- **MCP client:** connects to MCP servers via the official Python SDK and
  passes their tools on to the models. The client, the connection test and the tool
  loop in the chat are done.

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
| Web server and TLS | nginx (in the Debian package) |
| Static files | nginx in the package, WhiteNoise in Docker and development |
| Login throttling | django-axes |
| Packaging | Debian package with dh-virtualenv, systemd; Docker as a fallback |
