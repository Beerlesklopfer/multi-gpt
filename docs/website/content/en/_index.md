---
title: "MultiGPT"
description: "Self-hosted multi-AI chat system for families and households: your own data, many providers, one chat."
eyebrow: "Self-hosted · in development · version 0.4.0"
headline: "One AI chat for the whole family – on your own server"
lead: "MultiGPT is a web app for your home network that everyone in the household uses to chat with different AI models – from OpenAI, Anthropic and Google to a local LM Studio. Chats, accounts and API keys stay on your own server."
pillarsTitle: "The idea behind it"
pillarsLead: "These are the goals for version 1. The status labels further down show what has been built so far."
pillars:
  - title: "Your own data"
    text: "The interface, chat histories and API keys live only on your own server in your home network, in PostgreSQL. Keys are stored encrypted. The only traffic leaving the network is the requests to the AI providers you choose."
  - title: "One chat for many providers"
    text: "One interface for all connected models, with the model chosen per message. Local models from LM Studio join in for free whenever that PC is on."
  - title: "Family accounts with roles and budgets"
    text: "Every family member gets their own account. Roles define who may use which models and features. Monthly budgets keep costs in check."
menus:
  main:
    name: "Home"
    weight: 1
---

## Who is it for?

For technically minded people who want to give their family or household access to AI
models without a separate subscription for each person and without collecting all chats
at one provider. MultiGPT is meant for a server that is running anyway – a NAS or a small
home server with Debian – and that is reachable only from the home network.

## What exists today

You can chat with your own API keys: with OpenAI, Anthropic, Google Gemini, OpenRouter and
LM Studio in your home network, with streamed answers, a model choice per message, Markdown,
formulas and code highlighting. On top of that there are family accounts with roles, billing
accounts with budgets, a comparison mode for two or three models, MCP tools with
confirmation, questions to your own documents with citable sources (fully local via
LM Studio if you like, which MultiGPT can manage itself), web search via a self-hosted SearXNG,
encrypted API keys, an admin area for administrators and a Debian package that sets up the
database, schema and nginx itself. Other programs such as n8n or Claude can use MultiGPT as an
MCP server.

MultiGPT is installed on Debian 13 from a dedicated **apt repository**, so updates arrive with
the usual `apt upgrade`. How to set it up is described under
[installation]({{< relref "installation" >}}).

## New in 0.3.0 and 0.3.1

- **Attachments and image input:** drag or paste images and documents into the chat;
  location data is stripped from photos.
- **Projects** and **shared chats** with RWUD permissions.
- **Citations** with section, page and paragraph in DIN ISO 690, APA, Harvard, Chicago, MLA
  or BibTeX; figures in documents become searchable.
- **Image generation** with OpenAI or Gemini.
- **Calculations** with numpy, sympy and matplotlib in an isolated sandbox.
- **Page fetching** for models and flagging of invented links.
- **MCP** with JSON import, online status and access per model, plus the **capability
  matrix** and **billing accounts** for costs.
- **0.3.1:** print-ready **worksheets as PDF** – handwriting rulings, arithmetic sheets with
  answers, science worksheets.

Updating from 0.2? The required steps are under
[installation]({{< relref "installation" >}}).

## New in 0.3.2 to 0.3.6

- **MultiGPT as an MCP server (0.3.2):** n8n, Claude Desktop or your own agents ask models,
  upload and search documents and follow indexing – with an account's **API key**, which can
  never do more than the account. Since 0.3.3 results come back directly as text, image or
  PDF; since 0.3.6 keys can also **read** the account's own and shared **chats**.
- **Model management for LM Studio (0.3.2):** MultiGPT loads and unloads local models itself,
  never beyond a memory budget for the graphics card.
- **Context menu in the chat (0.3.2):** quote selected text with its origin, ask a follow-up,
  explain, translate. Plus **tools on demand**, so that many MCP tools fit into the context
  of local models.
- **apt repository (0.3.4)**, **prompt caching** with Anthropic, an **input history** with
  arrow up/down and deleting an answer **with what follows**.
- **Indexing** of several documents at once (0.3.5); runs can be paused and resumed (0.3.6).
- **Personal menu with profile picture** (0.3.5), **notices** for shared chats and the
  **version display** (0.3.6).
- **Costs:** the ECB rate for dollars is fetched automatically, and accounts can be switched
  to USD; **scipy** in the sandbox for calculations (0.3.6).

## New in 0.4.0

- **Runner:** a container on your own computer (rootless Podman) in which a model works like
  a coding tool – locked in, with a terminal, preview and web server; the work is stored on
  the server as a git bundle. It reaches devices on the home network via `ssh` or `telnet`
  only if the administrator approves them, and only with a confirmation. The web server in
  the container is by default reachable by the whole home network without any login (can be
  restricted, see features). You download the `multigpt-runner` program from MultiGPT; the
  guide is in the wiki under “Runner”.
- **“LM Studio” interface** as a provider type of its own, **tokens per second** and time to
  the first token in the header of every answer, a **swap prompt** when graphics memory is
  full, and the **reasoning effort per model**.
- **Scratchpad on the server** for files from tool calls and **reconnecting MCP servers** in
  the chat.

More under [features]({{< relref "features" >}}).

## Status

It already runs for real with OpenAI and LM Studio; the other cloud providers have mostly
been tested with simulations so far. A fresh Debian installation is still pending, and the
Docker build is untested. There is no Docker support for the runner, only Podman. Image editing, voice, music, backup and the actual scratchpad follow –
details in the [roadmap]({{< relref "roadmap" >}}).

## The tech in one sentence

Python and Django 5.2 behind gunicorn, PostgreSQL with pgvector as the only database,
an interface built from Django templates and a little vanilla JavaScript, no Node build
step, no CDNs. More under [architecture]({{< relref "architecture" >}}).

*Note: the application's user interface itself is in German.*
