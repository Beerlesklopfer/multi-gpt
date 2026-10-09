---
title: "Features"
description: "What MultiGPT is meant to do: many providers, LM Studio, family accounts, budgets, your own documents, web search, images, voice and MCP tools."
lead: "This page describes what MultiGPT version 1 is meant to do. Chatting with many providers, LM Studio and family accounts are done, MCP tools partially; budgets, your own documents, web search, images and voice are still planned. Every section shows its status and milestone."
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
- **In progress:** editing your own messages as in ChatGPT. Editing and “regenerate” create
  versions, and “‹ 1/2 ›” switches between them. Earlier versions are kept.
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
- Local models cost € 0 and are meant to stay usable even when the budget is used up
  (budgets come with milestone 6).
- Tested with simulated providers; a test with LM Studio in a real home network is still pending.

## Roles, groups and budgets

{{< status "partial" "2 / 6" >}}

Roles, groups, accounts and the central permission check are done (milestone 2). Budgets,
the usage overview and the "Family" page follow with milestone 6.

Four default roles, adjustable in the admin area:

| Role | May |
|---|---|
| Administrator | Everything: providers, keys, models, accounts, roles, groups, budgets, everyone's usage |
| Adult | All enabled models and features, own collections, sharing with groups |
| Teenager | Only enabled models and features, a fixed system prompt for the role, monthly budget |
| Guest | Chat with one fixed model, no uploads, no sharing, small budget |

- Permissions are checked on the server, for every page and before every provider call –
  not just by hiding things in the interface.
- **Budgets:** a monthly budget per role, overridable per person. A warning at 80 %; at
  100 % paid models are blocked until the next month.
- **Usage:** tokens and estimated costs per person, model and month.
- **Privacy:** chats are private. Administrators do not see other people's chats either,
  only usage figures. Whether parents may view chats of teenager accounts is still open;
  at most it would be an option per account, off by default and visible to the member.
- **Groups:** collections and individual chats can be shared with groups, read-only or
  with write access.
- A "Family" page for administrators: create and lock accounts, assign roles, reset
  passwords, manage groups.

## Comparison mode

{{< status "planned" "6" >}}

Ask two or three models the same question at the same time and read the answers side by side.

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

{{< status "planned" "7" >}}

- Create collections and upload documents (PDF, DOCX, TXT, MD), private or shared with groups.
- A background process splits the texts and computes embeddings; search uses PostgreSQL
  and pgvector.
- The documents used are listed below the answer, with page numbers.
- Passages from other people's private collections never end up in a query.

## Web search with sources

{{< status "planned" "8" >}}

- A "web search" switch in the input field. Results go to the model as numbered sources,
  and the links are listed below the answer. This also works with local models.
- The planned search backends are a self-hosted [SearXNG](https://docs.searxng.org/) or a
  search API.
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
