---
title: "Features"
description: "What MultiGPT does and is meant to do: many providers, LM Studio with model management, attachments, projects, shared chats, family accounts, billing accounts, your own documents with citations, web search, calculations, PDF sheets, images, MCP tools, MultiGPT as an MCP server and the runner that is currently being built."
lead: "This page describes what MultiGPT version 1 is meant to do, as of version 0.3.6. Done: chatting with many providers and LM Studio including model management, attachments, projects, shared chats, the context menu in the chat, family accounts with billing accounts and budgets, comparison mode, MCP tools, MultiGPT as an MCP server, calculations, PDF sheets, your own documents with citations, web search and image generation. Work on the runner is in progress for version 0.4.0; image editing, voice and the scratchpad are planned. Every section shows its status and milestone."
menus:
  main:
    weight: 10
---

Detailed guides are in the [wiki](https://github.com/Beerlesklopfer/multi-gpt/wiki) (German).

## Login and accounts

{{< status "done" "1" >}}

- Log in, log out and change your password.
- Login throttling: after five failed attempts, logging in with that user name from that
  address is blocked for 15 minutes.
- No self-registration. An administrator creates accounts – with
  `mgpt-ctl createsuperuser`, `make user` (with a choice of role) or in the admin area.
- **Personal menu** (since 0.3.5): the profile picture and name sit in the top right. A click
  opens settings, profile picture, my usage, API keys, family, administration, password and
  sign out – depending on your permissions. Without a profile picture your initials are
  shown; an uploaded picture is stored without metadata.
- The **version** is shown at the bottom of the sidebar, on the sign-in page and in the
  administration header (since 0.3.6).

## Chatting with many providers

{{< status "done" "3 / 4 / 5" >}}

- Chat list in the sidebar: new, rename, archive, delete, search. The sidebar can be hidden,
  answers use the full width, and the font size can be changed with A−/A+.
- The model can be chosen **per message**. Answers are streamed, a round button in the input
  field sends or stops, and an answer can be regenerated.
- Providers are connected via API key: OpenAI and all OpenAI-compatible services
  (e.g. OpenRouter and LM Studio), plus Anthropic and Google Gemini.
- In the admin area, “check connection now” tests a provider and names the cause of a
  problem, e.g. “connection refused”, “API key expired” or “check the base URL”. Unreachable
  cloud providers are greyed out in the chat.
- Your own messages can be edited as in ChatGPT, attachments included. Editing and
  “regenerate” create versions, and “‹ 1/2 ›” switches between them. Earlier versions are
  kept. Every message has a copy button.
- Markdown with code highlighting and a copy button. The libraries are bundled locally and
  the output is sanitised.
- Automatic chat titles, a system prompt per chat, export as Markdown.
- **Input history** (since 0.3.4): arrow up and down bring earlier messages back into the
  input field.
- **Delete an answer with what follows** (since 0.3.4): an answer can be deleted together with
  its question and everything after it. Costs stay booked.
- While the AI is still writing you can scroll freely: the view only follows while you are at
  the bottom; otherwise “↓ Neueste” (newest) takes you back to the end.
- With Anthropic, MultiGPT uses **prompt caching** for the system prompt, tools and history
  (since 0.3.4). This saves costs in longer chats.

## Context menu and quoting in the chat

{{< status "done" "13" >}}

Since 0.3.2: reuse selected text in answers, your own messages, comparison columns and source
lists – with a right-click, via a bar below the selection (on phones too) or with Shift+F10.

- **Quoting:** the selected text goes into the input field as a quote, with its origin (the
  model, “Ich” for yourself or “Quelle [n]” for a source). Several quotes go into one message,
  one after the other.
- **Quick actions:** ask a follow-up, explain, simplify, translate, counter-arguments, find
  evidence and copy with a source reference.
- Nothing is sent on its own. Shift+right-click still opens the browser's menu, and the
  custom menu can be switched off in the settings.
- Guide: [wiki: Chat-Bedienung](https://github.com/Beerlesklopfer/multi-gpt/wiki/Chat-Bedienung).

## Attachments and image input

{{< status "done" "5" >}}

- Attach images and documents with the paper clip, paste them from the clipboard or drag them
  into the input field. Images open large with a click (lightbox).
- Images only go to models with the “understands images” tick, across all three provider
  adapters. Documents (PDF, DOCX, TXT, MD) are passed on as text.
- **Privacy:** every image is re-encoded on the server. This drops EXIF data such as the
  location (GPS), camera data and embedded comments. The file type is checked by content,
  not by extension.
- Attachments are visible only in the chat they belong to and are served only after a
  permission check.

## Formulas and SVG preview

{{< status "done" "5" >}}

- Mathematical formulas are typeset with KaTeX. KaTeX lives on your own server; nothing is
  loaded from the internet.
- SVG code in answers gets a **preview**, with a switch to the code and “save as file”. The
  preview is just an image: scripts, links and external content in the SVG have no effect.

## Projects

{{< status "done" "5" >}}

Group chats into projects, as in ChatGPT or Claude – for example “Moving house” or “Tax return”.

- A project gives its chats **instructions**, a **default model** and preselected
  **collections** of your own documents.
- Projects appear in the sidebar above the chats; they can be expanded, pinned and archived.
  Search also finds project names.
- Only the person who created a project can see it.
- Guide: [wiki: Projekte](https://github.com/Beerlesklopfer/multi-gpt/wiki/Projekte).

## Sharing chats

{{< status "done" "5" >}}

- Share a chat with individual accounts or a group. Each share has **RWUD** permissions:
  read (always), write (join the conversation), update (edit messages, rename) and delete.
- Recipients find shared chats under “Mit mir geteilt” (shared with me) and can continue them
  as their own copy.
- **Revoking takes effect at once**, even for an answer that is still running.
- **Costs** are borne by whoever triggers an answer, not by the chat's owner. Which models
  someone may choose depends on their own role and budget.
- **Notices** (since 0.3.6): if a recipient deletes or archives the chat or deletes an answer,
  the owner gets a notice in the personal menu – with the name and chat title, without
  content.
- Guide: [wiki: Chats teilen](https://github.com/Beerlesklopfer/multi-gpt/wiki/Chats-teilen).

## Local models with LM Studio

{{< status "done" "4" >}}

[LM Studio](https://lmstudio.ai/docs) runs on a PC in the home network and provides an
OpenAI-compatible server. MultiGPT connects to it as a provider but does not start or wake it.

- A status indicator in the header shows whether LM Studio is reachable. The browser
  checks every 30 seconds while the tab is visible.
- When LM Studio is off, local models are greyed out. When it is on, the models that
  LM Studio actually reports are offered, along with their capabilities.
- If LM Studio goes away in the middle of an answer, the text received so far is kept and
  the cause is shown – with a dedicated hint when the model's context is too small.
- Local models cost € 0 and stay usable even when the budget is used up.
- In real use in the home network: chat (e.g. gpt-oss-20b), embeddings (nomic-embed-text) and
  OCR (olmOCR) run through LM Studio.

**Model management** (since 0.3.2, for LM Studio 0.4 or later): if you like, MultiGPT itself
decides which models are loaded in LM Studio.

- Before a chat, a comparison, embeddings, OCR or figure descriptions, MultiGPT loads the
  right model with its context window and unloads others to make room – **never beyond a
  memory budget** for the graphics card, keeping a reserve for the display and system.
- Pinned models and models in use stay loaded, and indexing does not push out the chat model.
  If something does not fit, the chat says “not enough graphics memory” and an indexing run
  waits.
- Indexing models stay loaded only for the run (since 0.3.4). Models unused for 30 minutes
  are unloaded by the worker.
- In the admin area you can choose the context window per model, with the expected memory
  shown next to it; a separate page lists the loaded models. On the console: `mgpt-ctl lms`.
- If several MultiGPT installations share one LM Studio, each unloads only the models it
  loaded itself (since 0.3.6).
- Guide: [wiki: Anbieter und Modelle](https://github.com/Beerlesklopfer/multi-gpt/wiki/Anbieter-und-Modelle).

## Models and capabilities

{{< status "done" "4 / 4a" >}}

The **capability matrix** in the admin area lists every provider's models with their
capabilities, editable in place:

- **Main type:** chat, image generation, embedding, speech recognition, speech output or music.
- **Ticks:** tools, understands images, edits images.
- **MCP:** none, all or selected MCP servers (see below).

New models get their capabilities **automatically**: LM Studio reports them itself, otherwise
MultiGPT infers them from the model ID. For existing models,
`sudo mgpt-ctl guess_capabilities --apply` catches up. Guide:
[wiki: Anbieter und Modelle](https://github.com/Beerlesklopfer/multi-gpt/wiki/Anbieter-und-Modelle).

## Roles, groups and budgets

{{< status "done" "2 / 6" >}}

Four default roles, adjustable in the admin area:

| Role | May |
|---|---|
| Administrator | Everything: providers, keys, models, accounts, roles, groups, budgets, everyone's usage |
| Adult | All enabled models and features, own collections, sharing with groups |
| Teenager | Only enabled models and features, a fixed system prompt for the role, monthly budget |
| Guest | Chat with one fixed model, no uploads, no sharing, small budget |

- Permissions are checked on the server, for every page and before every provider call –
  not just by hiding things in the interface. There are separate permissions for image
  generation, calculations and PDF documents, among others.
- **Privacy:** chats are private. Administrators do not see other people's chats either,
  only usage figures. The one exception: teenager accounts have an option "Einsicht in Chats
  erlaubt" (chat insight allowed), off by default. When it is on, administrators can read
  that account's chats but not write in them, and the member permanently sees a notice.
- **Groups:** collections and individual chats can be shared with groups.
- A "Familie" (family) page (`/familie/`) for administrators: create and lock accounts,
  assign roles, reset passwords, set budgets, switch chat insight, manage groups and view
  everyone's usage.

## Billing accounts and costs

{{< status "done" "6" >}}

Every provider bills through a **billing account**:

- **monetary** in EUR or USD (cloud providers), **tokens only** (local models such as
  LM Studio) or **flat rate** (subscription; requests and tokens are only counted).
- **Prices with validity dates:** per model from a given date, with separate prices for cache,
  long context and reasoning tokens. Image prices per size and quality. MultiGPT ships no
  prices; the administrator maintains them.
- **Exchange rates** for USD accounts; every answer is recorded as a **booking** with tokens,
  amount and rate. Since 0.3.6 the worker fetches the **ECB reference rate** USD→EUR
  automatically on working days (can be switched off), and EUR accounts can be switched to
  USD; existing bookings stay unchanged.
- **Budgets** per account, role and person (person before role): in euros, or as a quota for
  token accounts. From 80 % a warning appears; from 100 % the account's models are blocked
  until the next month. Free models stay usable.
- **Mein Verbrauch** (my usage, `/verbrauch/`) shows everyone their tokens and costs per
  account and model, their budget status and the last few months.
- Guide: [wiki: Kosten und Budgets](https://github.com/Beerlesklopfer/multi-gpt/wiki/Kosten-und-Budgets).

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

MultiGPT is a client for the [Model Context Protocol](https://modelcontextprotocol.io/).
Models that support tools can call functions from connected MCP servers.

- Only administrators add MCP servers – local (`stdio`) or via Streamable HTTP. Credentials
  are stored encrypted.
- **Import:** configurations in the `{"mcpServers": {…}}` format (as used by Claude Desktop,
  Cursor or n8n) can be pasted in. Entries with placeholders instead of real credentials are
  created disabled.
- **Online status:** every server shows “online”, “offline” with the cause, “unchecked” or
  “disabled”; the worker checks regularly.
- **Classification:** a tool table lists all of a server's tools. Tools that change something
  or send data out only run after a confirmation in the chat. New, unclassified tools require
  confirmation.
- **Access per model:** the administrator decides which models may use MCP (none, all,
  selected servers). This protects against models that are more easily tricked into unwanted
  calls. It is checked on the server, in addition to the role's permission.
- Every call appears in the chat as an expandable line with arguments, result and duration.
  At most 10 tool rounds per answer, with a time limit per call.
- **Tools on demand** (since 0.3.2): with many tools – n8n with more than 50, for example –
  the model gets only a core set and looks up more with `find_tools`. MultiGPT also estimates
  the request before sending, drops older messages if needed and reports a context that is
  too small with figures.
- Administrators see MCP servers going offline live in the status bar (since 0.3.5).
- Guide: [wiki: MCP](https://github.com/Beerlesklopfer/multi-gpt/wiki/MCP).

## MultiGPT as an MCP server

{{< status "done" "15" >}}

Since 0.3.2 MultiGPT is an MCP server itself: at `https://<hostname>/mcp/`, other programs such
as n8n, Claude Desktop or your own agents control MultiGPT with an account's **API key**.

- **Tools:** ask models (`ask`), upload, search and read documents, start, follow, pause and
  cancel indexing, read in directory sources, fetch generated files and call tools such as
  `create_pdf` or `run_python` directly. Results come back directly as text, image or PDF
  (since 0.3.3).
- **Reading chats** (since 0.3.6): with the “read chats” permission, a key lists and reads the
  account's own chats and chats shared with it – never anyone else's, not even for
  administrators. Chats created by `ask` go into an “API” project by default.
- **Permissions:** a key can never do more than its account. Every member creates keys under
  settings → API keys, as far as the role allows (on for administrators and adults). Keys are
  stored only as a hash, can be revoked and optionally expire.
- Every call is recorded in the audit log without content. For older clients there is also
  the HTTP+SSE transport at `/mcp/sse`.
- On the console: `mgpt-ctl apikey` and `mgpt-ctl index status --follow`.
- Guides: [wiki: API-Keys](https://github.com/Beerlesklopfer/multi-gpt/wiki/API-Keys) and
  [wiki: n8n](https://github.com/Beerlesklopfer/multi-gpt/wiki/n8n).

## Calculations in a sandbox

{{< status "done" "4a" >}}

Language models easily miscalculate. Models with tool support therefore get the built-in
`run_python` tool and calculate exactly.

- **numpy** (numerics, statistics), **scipy** (since 0.3.6), **sympy** (equations, derivatives, integrals, exact
  fractions), **mpmath** (high precision) and the standard library.
- **Charts** made with matplotlib appear as PNG or SVG in the answer; SVG is sanitised on the
  server.
- Code and output can be expanded in the chat.
- **Security:** the code is treated as untrusted and runs in a **bubblewrap sandbox** – no
  network, no server files, no keys or environment, with limits on time, memory and
  processes. Without a working sandbox the tool is not offered at all.
- A separate role permission “Berechnungen ausführen” (run calculations; on for
  administrators, adults, teenagers). Guide:
  [wiki: Berechnungen](https://github.com/Beerlesklopfer/multi-gpt/wiki/Berechnungen).

## Worksheets and documents as PDF

{{< status "done" "4a" >}}

Since 0.3.1: ask for a sheet and you get a **print-ready PDF** (the `create_pdf` tool) that
opens in the chat with “Ansehen” (view).

- **Handwriting rulings** for learning to write: rulings 0 to 4 with the “little house” guide,
  lined, squared 5 and 10 mm, dimensionally accurate in millimetres.
- **Maths:** arithmetic tasks with an answer sheet, digit boxes, times tables, number lines,
  clock faces. **The server** generates tasks and answers, not the model – so the answers are
  always right.
- **Science** and general worksheets with gap texts, tick boxes and pictures, plus letters,
  invitations and tables.
- When printing choose “actual size” or 100 % so the rulings stay accurate.
- **Security:** the PDF is rendered in a separate process without network or file access;
  only your own attachments may be embedded as images.
- A separate role permission “Dokumente erzeugen (PDF)” (create documents; on for
  administrators, adults, teenagers). Guide:
  [wiki: Dokumente erzeugen](https://github.com/Beerlesklopfer/multi-gpt/wiki/Dokumente-erzeugen).

## Ask your own documents (RAG)

{{< status "done" "7" >}}

- Create collections and upload documents (PDF, DOCX, TXT, MD and image files, up to 25 MB
  per file), private or shared with groups – read-only or with write access.
- A background service (worker) reads the text, recognises scanned pages via OCR, splits the
  text into passages and computes embeddings. Running jobs can be cancelled.
- **Several documents at once** (since 0.3.5, default 2) in separate worker processes. Runs
  can be **paused and resumed** (since 0.3.6), and uploads are processed before large runs.
- In the chat you pick collections. Search uses PostgreSQL and pgvector, optionally combined
  with German full-text search.
- The documents used are listed below the answer with their locators. A PDF can be viewed in
  the browser and jumps to the right page.
- **Figures:** optionally, a vision model describes images and charts in PDFs and Word
  documents. The description is searched as well and can be cited like text.
- **Document tools:** models with tool support can list documents (`list_documents`), fetch
  details and the table of contents (`document_info`) and read passages verbatim
  (`read_document`). They only read, and they only see collections you may read yourself.
- **Access control:** passages from other people's private collections never end up in a
  query; access is checked inside the search query itself. Administrators see only metadata
  in the RAG overview, never content.
- **Fully local:** embeddings with nomic-embed-text and OCR with olmOCR run through LM Studio
  in the home network, with Tesseract on the server as an optional fallback. So no document
  content leaves the house.
- **Directory sources:** an administrator can fill a collection from a folder on the server
  or NAS that is read in again periodically. Only folders below the directories allowed in
  `RAG_SOURCE_ROOTS` are accepted.
- Guide: [wiki: RAG](https://github.com/Beerlesklopfer/multi-gpt/wiki/RAG).

## Citations

{{< status "done" "7" >}}

- Every document source carries its **locator**: section heading, page and paragraph, e.g.
  “Abschn. 7.5.3, S. 12, Abs. 3”.
- **Citation style per account:** DIN ISO 690, APA 7, Harvard, Chicago (author-date) or
  MLA 9. “Copy citation” and “copy bibliography” are also available as BibTeX.
- The server formats citations by fixed rules, not the model.
- **Bibliographic data** can be maintained per document: books, edited volumes, chapters,
  articles, reports and **standards** with number and edition (e.g. “DIN EN ISO
  9001:2015-11”). A DOI lookup via Crossref is prepared and off by default.
- Guide: [wiki: Zitieren](https://github.com/Beerlesklopfer/multi-gpt/wiki/Zitieren).

## Web search and page fetching

{{< status "done" "8" >}}

- A "web search" switch in the input field. Results go to the model as numbered sources,
  and the links are listed below the answer. This also works with local models.
- The search backend is a self-hosted [SearXNG](https://docs.searxng.org/) in the home network.
- **Fetching pages and crawling sites:** models with tool support read single pages
  (`fetch_url`) or several pages of a website (`crawl_site`). URLs in your question are read
  directly. robots.txt is respected.
- **Protection:** addresses in the home network are blocked, including via redirects (SSRF
  protection). Fetched pages are treated as untrusted and reach the model only as marked
  source material, never as instructions. URLs do not appear in the log.
- **Invented links:** links in an answer that appear in no source, no tool result and not in
  the question get a ⚠. The “Belege prüfen” (check sources) button prepares a follow-up with
  web search. Base rules for all models urge them not to make things up.

## Create and edit images

{{< status "partial" "9" >}}

**Done (0.3.0): image generation** with OpenAI (gpt-image) or Google Gemini.

- Chat models with tool support call the `generate_image` tool themselves instead of
  “drawing” images as SVG code.
- An **“image” mode** in the input field with format (square, portrait, landscape) and quality.
- If the chosen model has no tool support, MultiGPT offers “generate with image model” before
  sending – never without your consent.
- Images are stored without metadata, and the cost per image is booked. The description only
  goes to the image model's provider.
- Guide: [wiki: Bilder](https://github.com/Beerlesklopfer/multi-gpt/wiki/Bilder).

**Planned:**

- Mark an area of an image and have it changed (inpainting), or create variations.
- Classic editing (crop, resize, rotate, convert, add text, collage) via a bundled MCP
  server – also by instruction, such as "make the image square". The original is kept.

## Voice

{{< status "planned" "10" >}}

- Microphone button: the recording becomes text that you can correct before sending.
- Speaker button on every answer, optionally reading answers aloud automatically.
- Requires HTTPS in the home network, because browsers only allow the microphone over HTTPS.

## Scratchpad

{{< status "planned" "13" >}}

- Collect material from chats, your own documents, the web, tools, images and notes, each
  with its origin and citation data.
- Turn it into a fully referenced document: the model writes only from the entries, MultiGPT
  sets in-text citations and the bibliography, and verbatim quotes are checked.
- As a first step, the context menu in the chat has been available since 0.3.2 (see above);
  your own prompt templates and “add to scratchpad” follow.

## Runner

{{< status "progress" "14" >}}

**In progress for version 0.4.0, not yet released.** The runner is a workspace for models –
not on the server, but as a **container on the computer the browser runs on**. Inside it, a
model works like a coding tool, but locked in.

- **How it works:** the model reads, searches and edits files, runs commands, tests and
  fixes – over many rounds until the task is done. Python, git and Node are available in the
  container.
- **Locked in:** by default the container has no network (can be enabled per role), no home
  directory and only a volatile in-memory workspace. Only a fixed image is started, with
  fixed limits for CPU, memory and processes.
- **Work stored on the server:** every change is committed; the work is kept as a git bundle
  with its full history in MultiGPT's database, is backed up with it and can be downloaded.
  Nothing is left on the computer after the session.
- **In the browser:** a terminal (also for programs that wait for input), a preview for HTML
  pages in a sandboxed frame and the model's task list. Results can be brought into the chat
  as an attachment on request.
- **Devices on the home network:** via `ssh` or `telnet`, but only to devices the
  administrator approves, and with a confirmation before every command. The model picks
  devices by name only; the server manages the credentials.
- **MultiGPT stays the central point:** every access goes through the server, which checks
  permissions, confirmations, quota and the audit log. The runner connects outgoing only, over
  TLS, and is paired via a token (revocable, with an expiry date).
- **Time quota:** container minutes per role or person and month, a warning from 80 %, a stop
  at 100 % – the work is kept.
- **Requirement:** rootless Podman; on Windows and macOS via Podman machine.

## Coming next: version 0.4.0

{{< status "progress" >}}

Besides the runner, these changes since 0.3.6 are done and will ship with 0.4.0 once it is
released after thorough testing:

- **“LM Studio” interface:** a provider type of its own where you only enter the address of
  the LM Studio server. Model management will run through it only.
- **Tokens per second for each answer:** the header of every answer shows the speed and the
  time to the first token, measured by MultiGPT itself – for all providers.
- **Swapping when graphics memory is full:** if another model is taking up the space, the
  chat offers to unload it and load the requested one – only after confirmation.
- **Reasoning effort per model:** MultiGPT reads the available levels from LM Studio, and a
  default can be set per model.

## Deliberately not in version 1

- No starting or waking LM Studio from within the app.
- No real-time voice conversation.
- No image models running on the server itself.
- No separation of several households in one installation.
