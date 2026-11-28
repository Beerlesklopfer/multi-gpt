---
title: "Features"
description: "What MultiGPT does, as of 0.4.1: chat with many providers, local models via our own model server Eliezer or LM Studio, your own documents with citations, web search, images, voice, tools via MCP, the runner, family accounts with roles and budgets."
lead: "What MultiGPT does today, how to use it and where its limits are – as of version 0.4.1. Done: chat with many providers, local models via our own model server Eliezer or LM Studio, your own documents with citations, web search, creating and editing images, voice, tools via MCP, the runner and family accounts with roles and budgets. Sharing projects and administration via mgpt-ctl are ready for the next version. Planned: music, background jobs and the actual scratchpad."
menus:
  main:
    weight: 10
---

Every section shows a status badge and the milestone from the
[roadmap]({{< relref "roadmap" >}}). “In progress” means: done, but not yet released. What is
new in which version is listed on the [home page]({{< relref "/" >}}). Detailed guides are in
the [wiki](https://github.com/Beerlesklopfer/multi-gpt/wiki) (German).

## Chatting

{{< status "done" "3 / 5" >}}

One interface for all connected models, on the PC and on the phone.

- **Choose the model per message.** Answers are streamed and can be stopped and regenerated.
  The header of every answer shows tokens per second and the time to the first token.
- **Editing with versions:** your own messages can be changed, attachments included. Editing
  and “regenerate” create versions, and “‹ 1/2 ›” switches between them. Single versions, or an
  answer with everything after it, can be deleted; costs already booked stay booked.
- **Rendering:** Markdown with code highlighting, formulas with KaTeX, SVG as a preview (as an
  image only, without scripts). All libraries are served from your own server.
- **Attachments:** attach, paste or drag in images and documents (PDF, DOCX, TXT, MD). Images
  only go to models that understand them. Every image is re-encoded, and EXIF data such as the
  location is dropped.
- **Chat list:** search, archive, automatic titles, export as Markdown, input history with arrow
  up and down, font size A−/A+.
- **Control:** a system prompt per chat; creativity (temperature) and reasoning effort per chat
  or project. The administrator's base rules apply to all models and are visible in the chat.
- **Context menu:** right-click, use the bar or press Shift+F10 to quote selected text with its
  origin, ask a follow-up, explain, simplify, translate, or look for counter-arguments or
  evidence. Nothing is sent on its own.
- **Errors** are shown with their cause; “technical details” give status, provider, model and
  request size.

Guide: [wiki: Chat-Bedienung](https://github.com/Beerlesklopfer/multi-gpt/wiki/Chat-Bedienung).

### Comparison mode

{{< status "done" "6" >}}

Ask two or three models the same question and read the answers side by side. They are kept
as versions of the same message in the chat. MCP tools are off in comparison mode.

## Providers and models

{{< status "done" "4 / 4a" >}}

- Providers are connected with an API key: OpenAI and OpenAI-compatible services (e.g.
  OpenRouter), Anthropic and Google Gemini, plus the “LM Studio” provider type for local models
  (see below). Keys are stored encrypted in the database.
- “Check connection now” names the cause of a problem, such as an expired key. Unreachable
  providers are greyed out in the chat.
- The **capability matrix** shows each model's main type (chat, image, embedding, speech
  recognition, text-to-speech) and ticks for tools, image understanding and image editing. New
  models get their capabilities automatically.
- When you switch models, the chat shows a cost hint for cloud models and a short introduction
  for local ones.
- With Anthropic, MultiGPT uses prompt caching, which saves costs in longer chats.

Guide: [wiki: Anbieter und Modelle](https://github.com/Beerlesklopfer/multi-gpt/wiki/Anbieter-und-Modelle).

## Local models

{{< status "done" "4" >}}

{{< shot "chat-code" >}}

Local models run on a PC with a graphics card in your home network, not on the MultiGPT
server. That PC runs a model server with an OpenAI-compatible interface. MultiGPT connects it
through the “LM Studio” provider type; the server's address is all it needs.

The usual way now is **Eliezer**, our own model server written in Rust. **LM Studio** can still
be used as a provider.

- A status indicator in the header shows whether the model server is reachable. If it is off,
  local models are greyed out. MultiGPT does not start or wake it.
- The models the server reports are offered, along with their capabilities.
- Local models cost nothing and stay usable even when the budget is used up.
- If the server drops out in the middle of an answer, the text so far is kept.

**Model management** (can be switched on per provider): before chat, comparison, embeddings and
OCR, MultiGPT loads the right model itself and unloads others to make room – never beyond a
memory budget for the graphics card, with a reserve for the display and system.

- Pinned models stay loaded, and indexing does not push out the chat model. Models unused for
  30 minutes are unloaded.
- If another model blocks the graphics memory, the chat offers to swap it out – only after
  confirmation.
- Reasoning effort and context window can be preset per model. If several installations share
  one model server, each only unloads its own models.

### Own model server Eliezer

{{< status "partial" "16" >}}

Since 0.4.1. Eliezer provides chat, image, speech recognition, text-to-speech, OCR and embedding
models. Memory needs and free space come from Eliezer itself. A **hub search** in the admin area
shows which model variant fits into memory.

Limits: Eliezer does not report whether a model supports tools – MultiGPT guesses this from the
model name. An image job takes the graphics card for itself and pushes out loaded chat models.
Running Eliezer through the runner is not available yet.

## Your own documents and citations

{{< status "done" "7" >}}

{{< shot "chat-rag" >}}

Ask questions about your own papers – contracts, manuals, school material – with source and
page.

- **Collections** of PDF, DOCX, TXT, MD and image files (up to 25 MB each), private or shared
  with groups, read-only or with write access.
- A background service extracts the text, recognises scanned pages via OCR and computes
  embeddings. Several documents are processed at once, and runs can be paused.
- Search uses PostgreSQL and pgvector, optionally combined with German full-text search. If you
  like, a vision model describes figures so that search can find them.
- **Fully local if you want:** embeddings and OCR via Eliezer or LM Studio, with Tesseract on the
  server as a fallback. Then no document content leaves the house.
- Models with tools can list documents, fetch the table of contents and read sections word for
  word – only in collections you may read yourself.
- **Folders on the server or NAS** can be added as a collection by an administrator; they are
  read in again regularly.
- **Access protection:** other people's private collections never end up in a query.
  Administrators see only metadata in the RAG overview.

**Citations:** below the answer, the sources are listed with their locator, e.g. “Sect. 7.5.3,
p. 12, para. 3”. A PDF opens in the browser at the right page. The server formats citations by
fixed rules in the account's style: DIN ISO 690, APA 7, Harvard, Chicago (author-date), MLA 9
or BibTeX. Bibliographic data can be edited per document, standards included.

{{< shot "collections" >}}

Guides: [wiki: RAG](https://github.com/Beerlesklopfer/multi-gpt/wiki/RAG),
[wiki: Zitieren](https://github.com/Beerlesklopfer/multi-gpt/wiki/Zitieren).

## Web search

{{< status "done" "8" >}}

- A “web search” switch in the input field. MultiGPT derives the search queries from the
  conversation and drops unrelated results. The links appear below the answer – with local
  models too.
- Searches go to a self-hosted [SearXNG](https://docs.searxng.org/); no commercial search API is
  connected.
- Models with tools read single pages or small websites, including URLs from your question, and
  respect robots.txt.
- **Protection:** addresses in the home network are blocked, also via redirects. Web text only
  reaches the model as marked source material, never as instructions.
- Links in an answer that appear in no source get a ⚠ and a “check sources” button.

## Images

{{< status "done" "9" >}}

- **Generate** with OpenAI (gpt-image), Google Gemini or locally with FLUX.2 klein via Eliezer.
  Chat models with tools call an image model themselves; there is also an “image” mode with
  format, quality and custom resolution.
- **Edit:** have an image model change attached images, with a mask where the provider supports
  it; create variations; keep working on the last image.
- **Without an image model:** the `transform_image` tool crops, resizes, rotates, adds text,
  builds collages and converts formats – locally, free of charge, as a new image next to the
  original.
- Images are stored without metadata, and costs are booked per image. The description only goes
  to the provider of the image model.
- A role permission of its own, “create and edit images”, on by default for administrators and
  adults.

Guide: [wiki: Bilder](https://github.com/Beerlesklopfer/multi-gpt/wiki/Bilder).

## Voice

{{< status "done" "10" >}}

Since 0.4.1.

- A microphone next to “Send” turns your recording into text, which you can correct before
  sending.
- A speaker button reads answers aloud, automatically if you like. Voice and read-aloud are set
  per account. Escape stops recording and playback.
- Recognition and read-aloud run locally via Eliezer or via OpenAI. You need one model for
  speech recognition and one for text-to-speech.
- Browsers only allow the microphone over HTTPS.

## Tools

Models with the “tools” tick can call functions. Every call appears in the chat as a
collapsible line with arguments, result and duration. A collapsible list in the input field
selects which tools are offered.

### Connecting MCP servers

{{< status "done" "4a" >}}

MultiGPT is a client for the [Model Context Protocol](https://modelcontextprotocol.io/).

- The administrator adds MCP servers, local (`stdio`) or over HTTP. Configurations in the
  `{"mcpServers": {…}}` format can be pasted in. Every server shows its online status and can be
  reconnected from the chat after an outage.
- Tools that change something or send data out only run after confirmation in the chat.
- The administrator decides per model which ones may use MCP (none, all, selected servers);
  local models are set to “none” by default.
- With many tools, the model gets only a core set and looks up the rest itself, so that the
  context of local models is enough.

Guide: [wiki: MCP](https://github.com/Beerlesklopfer/multi-gpt/wiki/MCP).

### Calculations

{{< status "done" "4a" >}}

The `run_python` tool calculates exactly with numpy, scipy, sympy and mpmath and draws charts
with matplotlib. The code runs in a bubblewrap sandbox without network, server files or keys,
with limits on time, memory and processes. Without a working sandbox the tool is not offered.
Guide: [wiki: Berechnungen](https://github.com/Beerlesklopfer/multi-gpt/wiki/Berechnungen).

### Worksheets and documents as PDF

{{< status "done" "4a" >}}

The `create_pdf` tool produces print-ready sheets: handwriting rulings 0 to 4, lined and squared
paper, arithmetic sheets with answers, times tables, number lines, clock faces, science
worksheets, plus letters, invitations and tables. The server computes tasks and answers, not
the model. Print at “actual size”. Guide:
[wiki: Dokumente erzeugen](https://github.com/Beerlesklopfer/multi-gpt/wiki/Dokumente-erzeugen).

### Scratchpad

{{< status "partial" "13" >}}

MultiGPT keeps files from tool calls (SVG too, sanitised) permanently; they appear with the
answer and under “Scratchpad” in the personal menu. The actual scratchpad – collecting material
from chats, documents and the web with its origin and turning it into a fully referenced
document with verified quotes – is planned.

### MultiGPT as an MCP server

{{< status "done" "15" >}}

Other programs such as n8n, Claude Desktop or your own agents control MultiGPT at
`https://<hostname>/mcp/` with an account's **API key**.

- They ask models, upload, search and read documents, follow and pause indexing, fetch generated
  files and call tools such as `create_pdf` or `transform_image` directly. With the “read chats”
  permission they read their own and shared chats.
- A key can never do more than its account. Keys are stored only as hashes, can be revoked and
  can have an expiry date. Every call is recorded in the audit log without content.

Guides: [wiki: API-Keys](https://github.com/Beerlesklopfer/multi-gpt/wiki/API-Keys),
[wiki: n8n](https://github.com/Beerlesklopfer/multi-gpt/wiki/n8n).

## Runner

{{< status "done" "14" >}}

Since 0.4.0. The runner is a workspace for models as a **container on the computer where the
browser runs** – not on the server. Inside, a model works like a coding tool: it reads, searches
and changes files, runs commands, tests and fixes.

- **Locked in:** rootless Podman, a fixed image with limits on CPU, memory and processes, no
  network by default and only a temporary workspace in RAM. A `/data` volume per computer keeps
  caches and results (limit per role).
- **Work kept on the server:** every change is committed; the work is stored as a git bundle in
  MultiGPT's database, backed up with it and available for download.
- **In the browser:** a terminal, a preview for HTML pages in a sandboxed frame, the model's task
  list and container logs per session. Results can be brought into the chat as attachments.
- **Packages** (Debian, pip, npm, cargo, GitHub) are only installed with the role permission
  “package sources”, and only through a proxy with a fixed list of allowed sources.
- **Devices in the home network** can be reached via `ssh` or `telnet` only if the administrator
  allows them, with a confirmation before every command.
- **Control:** every access goes through the server (permissions, confirmations, audit log). The
  runner only connects outbound over TLS and only accepts signed commands. Container minutes
  count against the monthly “runner time” quota.
- **Installation:** download the `multigpt-runner` program (Linux amd64 and arm64) from MultiGPT
  and run it as a service of your own user. It needs systemd and rootless Podman; on Windows and
  macOS via WSL2 or a Linux VM.

**Note:** the web server in the container is reachable on the computer at ports 24080 and
24443 – by default from the whole home network and without a login. `MGPT_EXPOSE_BIND=127.0.0.1`
restricts it to the computer itself.

Still open: Docker (Podman only), transferring only the changes instead of the whole work, and
live output of tool runs in the chat terminal. Guide:
[wiki: Runner](https://github.com/Beerlesklopfer/multi-gpt/wiki/Runner).

## Family: accounts, roles, privacy

{{< status "done" "1 / 2 / 6" >}}

{{< shot "chat-phone" >}}

- **Accounts** are created by an administrator; there is no self-registration. After five failed
  attempts, login is locked for 15 minutes.
- The **personal menu** at the top right holds settings, profile picture, my usage, API keys,
  scratchpad and – depending on permissions – family and admin.
- Permissions are checked on the server, before every page and every provider call.

Four default roles, adjustable in the admin area:

| Role | May |
|---|---|
| Administrator | Everything: providers, models, accounts, roles, groups, budgets, everyone's usage |
| Adult | Enabled models and features, own collections, sharing, API keys, runner |
| Teenager | Only enabled models and features, fixed system prompt of the role, budget |
| Guest | Chat with one set model, no uploads, no sharing, small budget |

Teenagers and guests start without models; the administrator enables them per role.

- **Privacy:** chats are private, and administrators cannot see other people's chats either –
  only usage figures. The one exception: for teenager accounts, “chat access allowed” can be
  switched on (off by default). Administrators can then read, and the member sees a permanent
  notice.
- The **family** page gives administrators accounts, roles, passwords, budgets, groups and
  everyone's usage in one place.

### Login names regardless of case

{{< status "progress" >}}

Ready for the next version: “Anna” and “anna” are the same account, and the login lock counts
all spellings together. If two accounts differ only in case, the update stops and names them.

## Projects and sharing

{{< status "done" "5" >}}

- **Projects** group chats, e.g. “moving house” or “tax return”. A project gives its chats
  instructions, a default model and preselected collections.
- **Share chats** with accounts or groups, with RWUD permissions: read (always), write, update,
  delete. Recipients find them under “shared with me”. Revoking takes effect at once.
- **Costs** are paid by whoever triggers an answer. Which models someone may use depends on
  their own role and budget.
- If a recipient deletes or archives something, the owner gets a notice – without content.

Guides: [wiki: Projekte](https://github.com/Beerlesklopfer/multi-gpt/wiki/Projekte),
[wiki: Chats teilen](https://github.com/Beerlesklopfer/multi-gpt/wiki/Chats-teilen).

### Sharing projects

{{< status "progress" >}}

Ready for the next version: share a whole project with accounts or groups, with the same
permissions as for chats. The share covers all chats of the project, including ones created
later; single chats can be kept private. Recipients see instructions and chats read-only; only
the person who created the project can change it.

## Costs and budgets

{{< status "done" "6" >}}

{{< shot "usage" >}}

- Each provider bills through a **billing account**: in euros or dollars, in tokens only (local
  models) or as a flat rate (subscription).
- **Prices** apply per model from a given date, with separate prices for cache and reasoning.
  MultiGPT ships no prices; the administrator can adopt suggestions from the LiteLLM price list.
  The ECB rate for dollars is fetched automatically.
- Every answer is recorded as a **booking** with tokens, amount and rate. With an admin key from
  the provider, MultiGPT compares daily with the provider's actual costs.
- **Budgets** per account, role and person: a warning from 80 %, and from 100 % the models of
  that account are blocked until the next month. Free models stay usable.
- **My usage** shows everyone their tokens, costs and the state of their budgets.

Guide: [wiki: Kosten und Budgets](https://github.com/Beerlesklopfer/multi-gpt/wiki/Kosten-und-Budgets).

## Installation and operation

{{< status "partial" "12" >}}

- **Debian package** for Debian 13 from a signed apt repository, with updates via `apt upgrade`.
  The package sets up PostgreSQL with pgvector, the schema and nginx with TLS, and migrates on
  updates by itself.
- Separate services for the web interface, background jobs and the runner; nginx serves
  attachments and documents. An AppArmor profile for the sandbox is included.
- Admin commands via `mgpt-ctl`, e.g. for accounts, API keys, indexing and model capabilities.
  The version including the git revision is shown in the interface.
- Docker is available as a fallback, but the build is untested.

Step by step: [installation]({{< relref "installation" >}}).

### Administration via mgpt-ctl

{{< status "progress" >}}

Ready for the next version: `mgpt-ctl` sets prices (from the LiteLLM price list), budgets and
provider and model settings, and checks the sandbox – without the admin interface (`prices`,
`providers`, `models`, `budgets`, `sandbox_check`).

Likewise **backup:** `mgpt-ctl backup` stores the database, media and configuration in one
archive, daily via a timer if you like; the wiki describes restoring, upgrading and removal.

## Planned

### Music

{{< status "planned" "11" >}}

Create songs locally via Eliezer and have songs described, with stored settings so a result
can be repeated or varied.

### Background jobs

{{< status "planned" "17" >}}

Answers keep running in the worker instead of the open page, plus scheduled tasks and notices
when something is done or looks wrong.

## Limits

**Today:**

- MultiGPT runs in real use with OpenAI and local models. Anthropic and Gemini have so far
  mostly been tested with simulated responses.
- A test installation on a fresh Debian 13 is still pending.
- The runner only works with Podman, not with Docker.

**Deliberately not in version 1:**

- No starting or waking the model server from the app.
- No real-time voice conversation – voice means recording to text and reading the answer aloud.
- No image models on the MultiGPT server itself; local images are made on the computer with the
  graphics card.
- No separation of several households: one installation belongs to one family.
