---
title: "Features"
description: "What MultiGPT is meant to do: many providers, LM Studio, family accounts, budgets, your own documents, web search, images, voice and MCP tools."
lead: "This page describes what MultiGPT version 1 is meant to do. Done: chatting with many providers, LM Studio, MCP tools, family accounts with budgets, comparison mode, your own documents and web search; images and voice are still planned. Every section shows its status and milestone."
menus:
  main:
    weight: 10
---

## Login and accounts

{{< status "done" "1" >}}

- Log in, log out and change your password.
- Login throttling: after five failed attempts, logging in with that user name from that
  address is blocked for 15 minutes.
- No self-registration. An administrator creates accounts – with
  `mgpt-ctl createsuperuser`, `make user` (with a choice of role) or in the admin area.

## Chatting with many providers

{{< status "done" "3 / 4 / 5" >}}

Tested end to end in the browser; the first real conversations with OpenAI work.

- Chat list in the sidebar: new, rename, archive, delete, search.
- The model can be chosen **per message**. Answers are streamed, a button stops them,
  and an answer can be regenerated.
- Providers are connected via API key: OpenAI and all OpenAI-compatible services
  (e.g. OpenRouter and LM Studio), plus Anthropic and Google Gemini.
- In the admin area, “check connection now” tests a provider and names the cause of a
  problem, e.g. “connection refused”, “check the API key” or “check the base URL”. Models can
  be picked from the provider's own list (“select models”, or a drop-down on the model ID
  field). Deprecated models are no longer offered.
- Your own messages can be edited as in ChatGPT. Editing and “regenerate” create versions,
  and “‹ 1/2 ›” switches between them. Earlier versions are kept. Every message has a copy
  button.
- Markdown with code highlighting and a copy button. The libraries are bundled locally and
  the output is sanitised.
- Automatic chat titles, a system prompt per chat, export as Markdown.

## Local models with LM Studio

{{< status "done" "4" >}}

[LM Studio](https://lmstudio.ai/docs) runs on a PC in the home network and provides an
OpenAI-compatible server. MultiGPT connects to it as a provider but does not start or wake it.

- A status indicator in the header shows whether LM Studio is reachable. The browser
  checks every 30 seconds while the tab is visible.
- When LM Studio is off, local models are greyed out. When it is on, the models that
  LM Studio actually reports are offered.
- If LM Studio goes away in the middle of an answer, the text received so far is kept.
- Local models cost € 0 and stay usable even when the budget is used up.
- In real use in the home network: chat (e.g. gpt-oss-20b), embeddings (nomic-embed-text) and OCR (olmOCR) run through LM Studio.

## Roles, groups and budgets

{{< status "done" "2 / 6" >}}

Roles, groups, accounts and the central permission check came with milestone 2; budgets,
the usage overview and the "Family" page with milestone 6.

Four default roles, adjustable in the admin area:

| Role | May |
|---|---|
| Administrator | Everything: providers, keys, models, accounts, roles, groups, budgets, everyone's usage |
| Adult | All enabled models and features, own collections, sharing with groups |
| Teenager | Only enabled models and features, a fixed system prompt for the role, monthly budget |
| Guest | Chat with one fixed model, no uploads, no sharing, small budget |

- Permissions are checked on the server, for every page and before every provider call –
  not just by hiding things in the interface.
- **Budgets:** a monthly budget in euros per role, overridable per person. From 80 % a
  warning appears; from 100 % paid models are blocked until the next month. Free models
  (local providers such as LM Studio, or models without prices) stay usable.
- **Usage:** on the "Mein Verbrauch" page (`/verbrauch/`) everyone sees this month's tokens
  and estimated costs per model, the budget status and the last few months.
  Administrators see everyone's usage.
- **Privacy:** chats are private. Administrators do not see other people's chats either,
  only usage figures. The one exception: teenager accounts have an option "Einsicht in Chats
  erlaubt" (chat insight allowed), off by default. When it is on, administrators can read
  that account's chats but not write in them, and the member permanently sees a notice.
- **Groups:** collections and individual chats can be shared with groups, read-only or
  with write access.
- A "Familie" (family) page (`/familie/`) for administrators: create and lock accounts,
  assign roles, reset passwords, set budgets, switch chat insight, manage groups and view
  everyone's usage.

## Comparison mode

{{< status "done" "6" >}}

Ask two or three models the same question at the same time and read the answers side by side.

- A switch in the input field opens the choice of two or three models.
- The answers appear as columns and are stored in the chat tree as versions of the same
  message. Later, "‹ 1/3 ›" switches between them.
- Picking one answer makes its model the chat's default model.
- MCP tools are off in comparison mode, so no confirmations pop up in several columns at once.

## Tools via MCP

{{< status "done" "4a" >}}

Built and tested with an MCP test server: tools in all provider adapters, the MCP client
(official Python SDK) with a connection test and tool classification in the admin area, the
tool loop in the chat, the confirmation before unapproved tools and the display of every call.
A test with real models is still pending.

MultiGPT becomes a client for the [Model Context Protocol](https://modelcontextprotocol.io/).
Models that support tools can then call functions from connected MCP servers.

- Only administrators add MCP servers – local (`stdio`) or via Streamable HTTP.
- Each role has a list of allowed servers; this is checked before every call.
- Tools that change something or send data out only run after a confirmation in the chat.
  Unknown tools require confirmation.
- Every call appears in the chat as an expandable line with arguments, result and duration.
- At most 10 tool rounds per answer, with a time limit per call.

## Ask your own documents (RAG)

{{< status "done" "7" >}}

Collections, upload, background indexing with text recognition (OCR), search with sources
in the chat, administration in the admin, fully local processing via LM Studio and
directory sources are done.

- Create collections and upload documents (PDF, DOCX, TXT, MD, up to 25 MB per file),
  private or shared with groups – read-only or with write access.
- A background service (worker) reads the text, recognises scanned pages via OCR, splits the text into passages and computes embeddings. Each document shows
  its status and errors; temporary errors are retried automatically.
- In the chat you pick collections. Search uses PostgreSQL and pgvector, optionally combined
  with German full-text search. Models with tool support can also search on their own.
- The documents used are listed below the answer with page numbers, linked to the passage.
- Passages from other people's private collections never end up in a query; access is
  checked inside the search query itself.
- Administrators get a RAG overview in the admin (worker, queue, storage), can re-index and
  retry failed items – seeing only metadata, never content.
- **Fully local:** LM Studio in the home network computes the embeddings with
  nomic-embed-text (768 dimensions, with the prefixes `search_document:` and
  `search_query:`). Scanned pages are read by the olmOCR vision model via LM Studio, with
  Tesseract on the server as an optional fallback. So no document content leaves the house.
  The buttons "Speichern und Embedding testen" and "Speichern und OCR testen" (save and
  test) check the settings right away.
- **Directory sources:** an administrator can fill a collection from a folder on the server
  or NAS that is read in again periodically. Only folders below the directories allowed in
  `RAG_SOURCE_ROOTS` are accepted.

## Web search with sources

{{< status "done" "8" >}}

- A "web search" switch in the input field. Results go to the model as numbered sources,
  and the links are listed below the answer. This also works with local models.
  Models with tool support can also call the web search on their own.
- The search backend is a self-hosted [SearXNG](https://docs.searxng.org/) in the home
  network. The administrator enters its address in the admin and checks it with
  "SearXNG testen". The interface is swappable; no search API is connected so far.
- Fetched pages are treated as untrusted: they are marked as source material, never as
  instructions, and addresses in the home network are blocked.

## Create and edit images

{{< status "planned" "9" >}}

- An "image" mode with a choice of format (square, landscape, portrait) via an image provider.
- Mark an area of an image and have it changed (inpainting), or create variations –
  if the chosen provider supports it.
- Classic editing (crop, resize, rotate, convert, add text, collage) via a bundled MCP
  server – also by instruction, such as "make the image square".
- Every edit creates a new image; the original is kept.

## Voice

{{< status "planned" "10" >}}

- Microphone button: the recording becomes text that you can correct before sending.
- Speaker button on every answer, optionally reading answers aloud automatically.
- Requires HTTPS in the home network, because browsers only allow the microphone over HTTPS.

## Deliberately not in version 1

- No starting or waking LM Studio from within the app.
- No real-time voice conversation.
- No image models running on the server itself.
- No separation of several households in one installation.
