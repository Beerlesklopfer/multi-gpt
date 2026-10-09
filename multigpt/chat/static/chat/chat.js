// MultiGPT – Chatansicht (M3-05): Modellauswahl, Senden, Streaming, Abbrechen,
// Neu erzeugen, neuer Chat, Werkzeugaufrufe mit Rückfrage (M4a-06), Bearbeiten,
// Versionen „‹ i/n ›“ und Kopieren an den Nachrichten.
// Kein Inline-JS, keine externen Ressourcen.
//
// Sicherheit (Plan 9): Nutzertext wird ausschließlich als Text eingesetzt
// (textContent), Modelltext nur über markdown.js (marked -> DOMPurify).
// Werkzeugargumente und -ergebnisse sind nicht vertrauenswürdig: nur textContent,
// nie Markdown oder HTML. Ebenso Titel und URLs der Web-Quellen (M8): Titel nur
// als Text, verlinkt werden nur http(s)-URLs.
"use strict";

(() => {
  const STORAGE_KEY = "multigpt.lastModel";
  const WEB_SEARCH_KEY = "multigpt.webSearch";
  const SOURCES_VISIBLE = 3; // wie templatetags/source_tags.py
  const SCROLL_STICK_PX = 48;
  const TITLE_MAX = 60;
  const RESULT_DISPLAY_CHARS = 4000;

  // Status eines Werkzeugaufrufs -> [Symbol, Text]; wie templatetags/tool_tags.py.
  const TOOL_STATUS = {
    awaiting_confirmation: ["?", "wartet auf Bestätigung"],
    running: ["\u21bb", "läuft"],
    ok: ["\u2713", "erfolgreich"],
    error: ["\u2717", "Fehler"],
    timeout: ["\u29d7", "Zeitüberschreitung"],
    rejected: ["\u2298", "abgelehnt"],
    interrupted: ["\u2717", "unterbrochen"],
  };

  // --- Hilfen ----------------------------------------------------------------

  const { csrfToken, errorMessage, fillTemplate } = window.MultiGPT;
  const markdown = window.MultiGPT.markdown;

  function storageGet(key) {
    try {
      return window.localStorage.getItem(key);
    } catch {
      return null;
    }
  }

  function storageSet(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch {
      // Privater Modus o. Ä.: Auswahl wird dann eben nicht gemerkt.
    }
  }

  function postJson(url, payload, signal, accept = "application/json") {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        Accept: accept,
        "X-CSRFToken": csrfToken(),
      },
      body: JSON.stringify(payload),
      signal,
    });
  }

  // Vorschau des Titels für die Seitenleiste; maßgeblich ist titles.py auf dem
  // Server (gleiche Regeln: erste Zeile ohne Markdown-Zeichen, Codeblöcke übersprungen).
  function cleanLine(line) {
    return line
      .replace(/[\x00-\x1f\x7f]/g, " ")
      .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
      .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
      .replace(/^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+(?:\[[ xX]\]\s+)?|\d{1,3}[.)]\s+)+/, "")
      .replace(/<\/?[A-Za-z][^>]*>/g, "")
      .replace(/\*+|~~|`+|(?<!\w)_+|_+(?!\w)/g, "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function titleFrom(text) {
    let inFence = false;
    let fallback = "";
    let line = "";
    for (const raw of text.split(/\r\n|\r|\n/)) {
      if (/^\s*(```|~~~)/.test(raw)) {
        inFence = !inFence;
        continue;
      }
      if (inFence) {
        fallback = fallback || cleanLine(raw);
        continue;
      }
      line = cleanLine(raw);
      if (line) {
        break;
      }
    }
    line = line || fallback;
    return line.length > TITLE_MAX ? `${line.slice(0, TITLE_MAX - 1).trimEnd()}\u2026` : line;
  }

  function formatDuration(ms) {
    if (typeof ms !== "number" || !Number.isFinite(ms)) {
      return "";
    }
    const value = Math.max(0, Math.round(ms));
    if (value < 1000) {
      return `${value} ms`;
    }
    if (value < 60000) {
      return `${(value / 1000).toFixed(1).replace(".", ",")} s`;
    }
    const seconds = Math.round(value / 1000);
    return `${Math.floor(seconds / 60)} min ${seconds % 60} s`;
  }

  function formatArguments(args) {
    try {
      return JSON.stringify(args ?? {}, null, 2);
    } catch {
      return String(args);
    }
  }

  // --- Quellen (M8) ------------------------------------------------------------
  // Gleiche Regeln wie templatetags/source_tags.py.

  function sourceUrl(raw) {
    if (typeof raw !== "string") {
      return null;
    }
    try {
      const url = new URL(raw.trim());
      return (url.protocol === "http:" || url.protocol === "https:") && url.hostname ? url : null;
    } catch {
      return null;
    }
  }

  function sourceDomain(url) {
    const host = url ? url.hostname : "";
    return host.startsWith("www.") ? host.slice(4) : host;
  }

  // --- SSE aus einem fetch-Stream lesen -----------------------------------------

  function parseEventBlock(block) {
    let event = "message";
    const data = [];
    for (const line of block.split("\n")) {
      if (!line || line.startsWith(":")) {
        continue; // Leerzeile oder Kommentar (Keep-alive)
      }
      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      let value = colon === -1 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) {
        value = value.slice(1);
      }
      if (field === "event") {
        event = value;
      } else if (field === "data") {
        data.push(value);
      }
    }
    if (!data.length) {
      return null;
    }
    let payload = null;
    try {
      payload = JSON.parse(data.join("\n"));
    } catch {
      payload = null;
    }
    return { event, data: payload || {} };
  }

  // Liefert {event, data}-Objekte. Events sind durch eine Leerzeile getrennt.
  async function* readEvents(response) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finished = false;
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) {
          finished = true;
          buffer += decoder.decode();
        } else {
          buffer += decoder.decode(value, { stream: true });
        }
        // Zeilenenden vereinheitlichen; ein "\r" am Pufferende kann zu
        // einem "\r\n" im nächsten Stück gehören und wartet deshalb.
        const keepCr = !finished && buffer.endsWith("\r");
        if (keepCr) {
          buffer = buffer.slice(0, -1);
        }
        buffer = buffer.replace(/\r\n?/g, "\n");
        let boundary;
        while ((boundary = buffer.indexOf("\n\n")) !== -1) {
          const block = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          const parsed = parseEventBlock(block);
          if (parsed) {
            yield parsed;
          }
        }
        if (keepCr) {
          buffer += "\r";
        }
        if (finished) {
          const parsed = buffer.trim() ? parseEventBlock(buffer) : null;
          if (parsed) {
            yield parsed;
          }
          return;
        }
      }
    } finally {
      if (!finished) {
        reader.cancel().catch(() => {});
      }
    }
  }

  // --- Seite -------------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", () => {
    const chat = document.getElementById("chat");
    if (!chat) {
      return;
    }
    const history = document.getElementById("chat-history");
    const log = document.getElementById("chat-log");
    const emptyState = document.getElementById("chat-empty");
    const statusLine = document.getElementById("chat-status");
    const form = document.getElementById("chat-form");
    const modelSelect = document.getElementById("model-select");
    const textarea = document.getElementById("message-input");
    const sendButton = document.getElementById("send-button");
    const newChatButton = document.getElementById("new-chat-button");

    const urls = {
      conversations: chat.dataset.apiConversations,
      models: chat.dataset.apiModels,
      messagesTemplate: chat.dataset.apiMessagesTemplate,
      detailTemplate: chat.dataset.apiDetailTemplate,
      conversationTemplate: chat.dataset.conversationUrlTemplate,
      mcpServers: chat.dataset.apiMcpServers,
      toolConfirmTemplate: chat.dataset.apiToolConfirmTemplate,
      branchTemplate: chat.dataset.apiBranchTemplate,
      fragmentTemplate: chat.dataset.messagesFragmentTemplate,
    };
    let conversationId = chat.dataset.conversationId || null;
    let controller = null; // AbortController des laufenden Streams
    let modelsReady = false;
    let mcpServers = []; // [{id, name, default_enabled}] aus /api/mcp-servers/

    // --- Anzeige ---

    // level: false/"info", true/"error" oder "warning" (Hinweis, kein Fehler).
    function setStatus(text, level = false) {
      statusLine.textContent = text;
      statusLine.classList.toggle("is-error", Boolean(text) && (level === true || level === "error"));
      statusLine.classList.toggle("is-warning", Boolean(text) && level === "warning");
    }

    function isNearBottom() {
      return history.scrollHeight - history.scrollTop - history.clientHeight <= SCROLL_STICK_PX;
    }

    function scrollToBottom() {
      history.scrollTop = history.scrollHeight;
    }

    function messageElements() {
      return Array.from(log.querySelectorAll(".chat-message")).filter((el) => !el.hidden);
    }

    function appendMessage(role, author, text) {
      const article = document.createElement("article");
      article.className = `chat-message chat-message-${role}`;
      article.dataset.role = role;
      const heading = document.createElement("h2");
      heading.className = "chat-message-author";
      heading.textContent = author;
      const content = document.createElement("div");
      content.className = "chat-message-content";
      content.textContent = text;
      article.append(heading, content);
      log.append(article);
      if (emptyState) {
        emptyState.hidden = true;
      }
      return article;
    }

    function setMessageStatus(article, status, errorText) {
      article.dataset.status = status;
      article.querySelector(".chat-message-status")?.remove();
      if (status !== "aborted" && status !== "error") {
        return;
      }
      const note = document.createElement("p");
      note.className = `chat-message-status chat-message-status-${status}`;
      const label = status === "aborted" ? "Abgebrochen" : "Fehler";
      note.textContent = `${label}${errorText ? `: ${errorText}` : ""}`;
      article.append(note);
    }

    // --- Quellen unter der laufenden Antwort (M8) ---
    // Aufbau wie chat/_sources.html; jedes Event "sources" ersetzt die Liste.

    // Webquelle: Link nur bei http(s), Domain daneben. Dokumentquelle (M7): nur
    // eigene, relative Pfade ("/…", nicht "//…"), Seite im Text, keine Domain.
    function buildSourceItem(source) {
      const item = document.createElement("li");
      item.className = "message-source";
      const raw = typeof source.url === "string" ? source.url.trim() : "";
      const title = String(source.title ?? "").replace(/\s+/g, " ").trim();
      let href = "";
      let site = "";
      let label;
      if (source.kind === "document") {
        href = raw.startsWith("/") && !raw.startsWith("//") && !raw.startsWith("/\\") ? raw : "";
        const page = Number(source.page);
        label = `${title || "Dokument"}${Number.isInteger(page) && page > 0 ? `, S. ${page}` : ""}`;
      } else {
        const url = sourceUrl(raw);
        href = url ? url.href : "";
        site = sourceDomain(url);
        label = title || site || raw || "Quelle";
      }
      if (href) {
        const link = document.createElement("a");
        link.className = "message-source-link";
        link.href = href;
        link.target = "_blank";
        link.rel = "noopener noreferrer nofollow";
        link.textContent = label;
        const hint = document.createElement("span");
        hint.className = "sr-only";
        hint.textContent = " (öffnet in neuem Tab)";
        link.append(hint);
        item.append(link);
        if (site) {
          const domain = document.createElement("span");
          domain.className = "message-source-domain";
          domain.textContent = site;
          item.append(" ", domain);
        }
      } else {
        const text = document.createElement("span");
        text.className = "message-source-link";
        text.textContent = label;
        item.append(text);
      }
      return item;
    }

    function renderSources(article, sources) {
      article.querySelector(".message-sources")?.remove();
      const list = (Array.isArray(sources) ? sources : []).filter((s) => s && typeof s === "object");
      if (!list.length) {
        return;
      }
      list.sort((a, b) => (Number(a.n) || 0) - (Number(b.n) || 0));
      const section = document.createElement("section");
      section.className = "message-sources";
      const titleId = `message-sources-${article.dataset.messageId || "neu"}`;
      section.setAttribute("aria-labelledby", titleId);
      const heading = document.createElement("h3");
      heading.className = "message-sources-title";
      heading.id = titleId;
      heading.textContent = "Quellen";
      const head = document.createElement("ol");
      head.className = "message-sources-list";
      head.append(...list.slice(0, SOURCES_VISIBLE).map(buildSourceItem));
      section.append(heading, head);
      const rest = list.slice(SOURCES_VISIBLE);
      if (rest.length) {
        const more = document.createElement("details");
        more.className = "message-sources-more";
        const summary = document.createElement("summary");
        summary.textContent = `Weitere Quellen (${rest.length})`;
        const tail = document.createElement("ol");
        tail.className = "message-sources-list";
        tail.start = SOURCES_VISIBLE + 1;
        tail.append(...rest.map(buildSourceItem));
        more.append(summary, tail);
        section.append(more);
      }
      article.querySelector(".chat-message-content").after(section);
    }

    // --- Werkzeugaufrufe (M4a-06) ---
    // Aufbau wie chat/_tool_call.html und chat/_tool_confirm.html.

    function node(tag, className, text) {
      const element = document.createElement(tag);
      if (className) {
        element.className = className;
      }
      if (text !== undefined) {
        element.textContent = text;
      }
      return element;
    }

    function toolList(article) {
      let list = article.querySelector(".tool-calls");
      if (!list) {
        list = node("ol", "tool-calls");
        list.setAttribute("aria-label", "Werkzeugaufrufe");
        article.insertBefore(list, article.querySelector(".chat-message-content"));
      }
      return list;
    }

    function toolRow(article, id) {
      return article.querySelector(`.tool-call-item[data-tool-call-id="${CSS.escape(String(id))}"]`);
    }

    function toolName(row) {
      return row.querySelector(".tool-call-name")?.textContent || "Werkzeug";
    }

    function setToolStatus(row, status, durationMs) {
      const [icon, label] = TOOL_STATUS[status] || ["•", status];
      row.dataset.status = status;
      row.querySelector(".tool-call-icon").textContent = icon;
      row.querySelector(".tool-call-status").textContent = label;
      if (durationMs !== undefined) {
        row.querySelector(".tool-call-duration").textContent = formatDuration(durationMs);
      }
    }

    function buildToolRow(data) {
      const row = node("li", "tool-call-item");
      row.dataset.toolCallId = String(data.id);
      const details = node("details", "tool-call");
      const summary = node("summary", "tool-call-summary");
      const icon = node("span", "tool-call-icon");
      icon.setAttribute("aria-hidden", "true");
      const title = node("span", "tool-call-title", "Werkzeug ");
      title.append(
        node("span", "tool-call-name", String(data.tool ?? "")),
        " ",
        node("span", "tool-call-server", `(${data.server || "Server entfernt"})`),
      );
      // Leerzeichen zwischen den Teilen wie im Template (Vorlesen, Kopieren).
      summary.append(
        icon,
        " ",
        title,
        " ",
        node("span", "tool-call-status"),
        " ",
        node("span", "tool-call-duration"),
      );
      const body = node("div", "tool-call-body");
      const output = node("div", "tool-call-output");
      output.hidden = true;
      const truncated = node(
        "p",
        "hint tool-call-truncated",
        `Ergebnis gekürzt angezeigt (höchstens ${RESULT_DISPLAY_CHARS} Zeichen).`,
      );
      truncated.hidden = true;
      const attachments = node("ul", "tool-call-attachments");
      attachments.hidden = true;
      output.append(
        node("p", "tool-call-label", "Ergebnis"),
        node("pre", "tool-call-result"),
        truncated,
        attachments,
      );
      body.append(
        node("p", "tool-call-label", "Argumente"),
        node("pre", "tool-call-arguments", formatArguments(data.arguments)),
        output,
      );
      details.append(summary, body);
      row.append(details);
      return row;
    }

    // Event tool_call: Zeile anlegen oder (Fortsetzung nach Rückfrage) aktualisieren.
    function upsertToolCall(article, data) {
      if (data.id == null) {
        return;
      }
      let row = toolRow(article, data.id);
      if (!row) {
        row = buildToolRow(data);
        toolList(article).append(row);
      }
      const status = typeof data.status === "string" ? data.status : "running";
      setToolStatus(row, status);
      if (status === "running") {
        row.querySelector(".tool-call-actions")?.remove();
        setStatus(`Werkzeug „${toolName(row)}“ wird ausgeführt …`);
      } else if (status === "awaiting_confirmation") {
        row.querySelector(".tool-call").open = true;
      }
    }

    // Event tool_result: Status, Dauer, Ergebnis (als Text, gekürzt) und Anhänge.
    function applyToolResult(article, data) {
      const row = data.id == null ? null : toolRow(article, data.id);
      if (!row) {
        return;
      }
      const status = typeof data.status === "string" ? data.status : "ok";
      // Abgelehnte Aufrufe liefen nie: keine Dauer (wie nach dem Neuladen).
      setToolStatus(row, status, status === "rejected" ? null : data.duration_ms);
      row.querySelector(".tool-call-actions")?.remove();
      const output = row.querySelector(".tool-call-output");
      const text = typeof data.result === "string" ? data.result : "";
      row.querySelector(".tool-call-result").textContent =
        text.length > RESULT_DISPLAY_CHARS ? `${text.slice(0, RESULT_DISPLAY_CHARS)}…` : text;
      row.querySelector(".tool-call-truncated").hidden = text.length < RESULT_DISPLAY_CHARS;
      const ids = Array.isArray(data.attachment_ids) ? data.attachment_ids : [];
      const list = row.querySelector(".tool-call-attachments");
      list.replaceChildren(
        ...ids.map((id) => node("li", "", `Anhang #${Number(id)} (Anzeige folgt)`)),
      );
      list.hidden = !ids.length;
      output.hidden = !text && !ids.length && status === "rejected";
      const duration = formatDuration(data.duration_ms);
      setStatus(
        `Werkzeug „${toolName(row)}“: ${TOOL_STATUS[status]?.[1] || status}${duration ? ` (${duration})` : ""}.`,
        status === "error" || status === "timeout",
      );
    }

    function pendingToolRows(article) {
      return Array.from(
        article.querySelectorAll('.tool-call-item[data-status="awaiting_confirmation"]'),
      );
    }

    function decisionButton(decision, id, name) {
      const approve = decision === "approve";
      const button = node(
        "button",
        `button button-small${approve ? " button-primary" : ""}`,
        approve ? "Ausführen" : "Ablehnen",
      );
      button.type = "button";
      button.dataset.toolDecision = decision;
      button.dataset.toolCallId = String(id);
      button.setAttribute("aria-pressed", "false");
      button.setAttribute("aria-label", `Werkzeug ${name} ${approve ? "ausführen" : "ablehnen"}`);
      return button;
    }

    function buildConfirmBox(article) {
      const box = node("div", "tool-confirm");
      box.setAttribute("role", "group");
      const titleId = `tool-confirm-title-${article.dataset.messageId || "neu"}`;
      box.setAttribute("aria-labelledby", titleId);
      const title = node("p", "tool-confirm-title");
      title.id = titleId;
      const icon = node("span", "", "⚠ ");
      icon.setAttribute("aria-hidden", "true");
      title.append(icon, "Bestätigung nötig");
      const actions = node("div", "tool-confirm-actions");
      const all = node("button", "button button-small button-primary", "Alle ausführen");
      all.type = "button";
      all.dataset.toolConfirmAll = "";
      const none = node("button", "button button-small", "Alle ablehnen");
      none.type = "button";
      none.dataset.toolRejectAll = "";
      actions.append(all, none);
      box.append(
        title,
        node(
          "p",
          "tool-confirm-text",
          "Das Modell möchte Werkzeuge ausführen, die etwas verändern oder Daten nach außen " +
            "senden können. Prüfe die Argumente und entscheide je Aufruf.",
        ),
        actions,
      );
      return box;
    }

    // Event confirmation_required: Knöpfe je Aufruf und für alle.
    function showConfirmation(article, ids) {
      for (const id of ids) {
        const row = toolRow(article, id);
        if (!row || row.querySelector(".tool-call-actions")) {
          continue;
        }
        setToolStatus(row, "awaiting_confirmation");
        row.querySelector(".tool-call").open = true;
        const actions = node("div", "tool-call-actions");
        actions.setAttribute("role", "group");
        actions.setAttribute("aria-label", `Werkzeug ${toolName(row)}: Entscheidung`);
        actions.append(
          decisionButton("approve", id, toolName(row)),
          decisionButton("reject", id, toolName(row)),
        );
        row.append(actions);
      }
      if (!article.querySelector(".tool-confirm")) {
        // Hinweis vor den Aufrufen, damit er vor den Knöpfen gelesen wird.
        article.insertBefore(buildConfirmBox(article), toolList(article));
      }
      refreshConfirmBox(article);
    }

    // Hinweis oben im sichtbaren Bereich zeigen, sonst bis ans Ende scrollen.
    function revealConfirmation(article) {
      const box = article.querySelector(".tool-confirm");
      if (!box) {
        return;
      }
      scrollToBottom();
      const offset = box.getBoundingClientRect().top - history.getBoundingClientRect().top;
      if (offset < 0) {
        history.scrollTop += offset - 8;
      }
    }

    // "Alle …" nur bei mehreren offenen Aufrufen.
    function refreshConfirmBox(article) {
      const box = article.querySelector(".tool-confirm");
      if (!box) {
        return;
      }
      const several = pendingToolRows(article).length > 1;
      for (const button of box.querySelectorAll("[data-tool-confirm-all], [data-tool-reject-all]")) {
        button.hidden = !several;
      }
    }

    function setConfirmDisabled(article, disabled) {
      for (const button of article.querySelectorAll(
        "[data-tool-decision], [data-tool-confirm-all], [data-tool-reject-all]",
      )) {
        button.disabled = disabled;
      }
    }

    // Offene Rückfragen anderer Antworten verfallen, sobald eine neue Antwort
    // beginnt (der Server lehnt sie ab und bricht die Nachricht ab).
    function expireConfirmations(except) {
      for (const article of log.querySelectorAll('.chat-message[data-status="awaiting_confirmation"]')) {
        if (article === except) {
          continue;
        }
        for (const row of pendingToolRows(article)) {
          setToolStatus(row, "rejected");
          row.querySelector(".tool-call-actions")?.remove();
        }
        article.querySelector(".tool-confirm")?.remove();
        setMessageStatus(article, "aborted", "Rückfrage nicht beantwortet");
      }
    }

    // Laufende Zeilen nach Abbruch/Fehler nicht ewig "läuft" zeigen lassen.
    function interruptRunningTools(article) {
      for (const row of article.querySelectorAll('.tool-call-item[data-status="running"]')) {
        setToolStatus(row, "interrupted");
      }
    }

    const toolDecisions = new WeakMap(); // article -> Map(id -> decision)

    function decide(article, ids, decision) {
      let decisions = toolDecisions.get(article);
      if (!decisions) {
        decisions = new Map();
        toolDecisions.set(article, decisions);
      }
      for (const id of ids) {
        decisions.set(String(id), decision);
        const row = toolRow(article, id);
        for (const button of row?.querySelectorAll("[data-tool-decision]") || []) {
          button.setAttribute("aria-pressed", String(button.dataset.toolDecision === decision));
        }
      }
      const pending = pendingToolRows(article).map((row) => row.dataset.toolCallId);
      const open = pending.filter((id) => !decisions.has(id));
      if (open.length) {
        setStatus(`Entscheidung gespeichert. Noch offen: ${open.length} Werkzeugaufruf(e).`);
        return;
      }
      const payload = {};
      for (const id of pending) {
        payload[id] = decisions.get(id);
      }
      toolDecisions.delete(article);
      confirmTools(article, payload);
    }

    log.addEventListener("click", (event) => {
      const button = event.target.closest(
        "[data-tool-decision], [data-tool-confirm-all], [data-tool-reject-all]",
      );
      const article = button?.closest(".chat-message");
      if (!button || !article || button.disabled || !form) {
        return;
      }
      if (controller) {
        setStatus("Bitte warte, bis die laufende Antwort fertig ist.", true);
        return;
      }
      if (button.dataset.toolDecision) {
        decide(article, [button.dataset.toolCallId], button.dataset.toolDecision);
      } else {
        const ids = pendingToolRows(article).map((row) => row.dataset.toolCallId);
        decide(article, ids, button.hasAttribute("data-tool-confirm-all") ? "approve" : "reject");
      }
    });

    async function confirmTools(article, decisions) {
      if (controller || !conversationId) {
        return;
      }
      setConfirmDisabled(article, true);
      setStatus("Entscheidung wird gesendet …");
      await runStream({ decisions }, article, {
        url: fillTemplate(urls.toolConfirmTemplate, conversationId),
        continuation: true,
      });
    }

    // Leisten unter den Nachrichten (chat/_message_actions.html): Knöpfe je nach
    // Zustand freigeben. Bearbeiten und Neu erzeugen brauchen das Eingabefeld
    // (Schreibrecht und Modelle), Umschalten nur das Schreibrecht (Server).
    // Läuft ein Stream oder wartet ein Vergleich (compare.js) auf die Wahl?
    function isBusy() {
      return Boolean(controller) || Boolean(window.MultiGPT.compare?.pending());
    }

    function updateMessageActions() {
      const busy = isBusy();
      for (const bar of log.querySelectorAll("[data-message-actions]")) {
        const article = bar.closest(".chat-message");
        bar.hidden = article.classList.contains("is-editing");
        for (const button of bar.querySelectorAll("[data-version-target]")) {
          button.disabled = busy || !button.dataset.versionTarget;
        }
        for (const button of bar.querySelectorAll("[data-edit-message]")) {
          button.hidden = !form;
          button.disabled = busy || !modelsReady;
        }
        for (const button of bar.querySelectorAll("[data-regenerate-message]")) {
          button.hidden =
            !form || !article.dataset.messageId || article.dataset.status === "awaiting_confirmation";
          button.disabled = busy || !modelsReady;
        }
      }
    }

    function setStreaming(active) {
      chat.dataset.streaming = String(active);
      log.setAttribute("aria-busy", String(active));
      if (!sendButton) {
        return;
      }
      sendButton.textContent = active ? "Abbrechen" : "Senden";
      sendButton.classList.toggle("button-stop", active);
      sendButton.classList.toggle("button-primary", !active);
      sendButton.disabled = !active && !modelsReady;
      if (modelSelect) {
        modelSelect.disabled = active || !modelsReady;
      }
      const toolSwitches = document.getElementById("tool-servers");
      if (toolSwitches) {
        toolSwitches.disabled = active;
      }
      if (webSearchToggle) {
        webSearchToggle.disabled = active;
      }
      const collectionPicker = document.getElementById("collection-picker");
      if (collectionPicker) {
        collectionPicker.disabled = active;
      }
      updateMessageActions();
    }

    function returnFocus() {
      const active = document.activeElement;
      if (!active || active === document.body || active === sendButton || !active.isConnected) {
        textarea?.focus();
      }
    }

    // --- Seitenleiste ---

    // Chat in der Seitenleiste eintragen bzw. nach oben holen (sidebar.js).
    function markSidebar(conv, text) {
      const sidebar = window.MultiGPT.sidebar;
      if (!sidebar) {
        return;
      }
      const item = sidebar.upsertItem({ id: conv.id, url: conv.url, title: conv.title || "" });
      sidebar.markActive(conv.id);
      if (item && text && !item.dataset.title) {
        const title = titleFrom(text);
        if (title) {
          sidebar.setTitle(conv.id, title);
        }
      }
    }

    function currentConversation() {
      return {
        id: conversationId,
        url: fillTemplate(urls.conversationTemplate, conversationId),
      };
    }

    // --- Modelle ---

    function selectedModel() {
      if (!modelSelect || !modelSelect.value) {
        return null;
      }
      const option = modelSelect.selectedOptions[0];
      return { id: Number(modelSelect.value), name: option.dataset.name || option.textContent };
    }

    function showNoModels() {
      if (!form) {
        return;
      }
      const notice = document.createElement("p");
      notice.className = "chat-notice";
      notice.textContent =
        "Für dein Konto ist kein KI-Modell freigeschaltet. Ein Verwalter muss dir Modelle freigeben.";
      form.replaceWith(notice);
    }

    // Lädt die Modellliste (auch erneut, wenn ein lokaler Anbieter online
    // oder offline geht). Lokale Modelle eines Anbieters, der offline ist oder
    // sie nicht meldet, sind ausgegraut und nicht wählbar (Plan 8a).
    async function loadModels() {
      let models;
      try {
        const response = await fetch(urls.models, {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        if (!response.ok) {
          throw new Error(await errorMessage(response));
        }
        models = await response.json();
      } catch (err) {
        modelSelect.replaceChildren(new Option("Keine Modelle verfügbar", ""));
        setStatus(`Modelle konnten nicht geladen werden. ${err.message || ""}`.trim(), true);
        return;
      }
      if (!Array.isArray(models) || !models.length) {
        showNoModels();
        return;
      }

      const providers = new Map();
      for (const model of models) {
        const key = String(model.provider ?? "");
        if (!providers.has(key)) {
          providers.set(key, []);
        }
        providers.get(key).push(model);
      }
      // Vom ausgeschöpften Monatsbudget gesperrte Modelle: ausgegraut (M6-03).
      const isAvailable = (model) => model.available !== false && model.blocked_by_budget !== true;
      const makeOption = (model) => {
        let label = model.display_name;
        if (model.blocked_by_budget === true) {
          label += " (Budget ausgeschöpft)";
        } else if (model.is_local) {
          if (model.online === false) {
            label += " (lokal, offline)";
          } else if (!isAvailable(model)) {
            label += " (lokal, nicht geladen)";
          } else {
            label += " (lokal, online)";
          }
        }
        const option = new Option(label, String(model.id));
        option.dataset.name = model.display_name;
        option.dataset.tools = String(model.supports_tools === true);
        if (model.is_local) {
          option.className = isAvailable(model) ? "model-online" : "model-offline";
        }
        option.disabled = !isAvailable(model);
        return option;
      };
      const nodes = [];
      for (const [provider, list] of providers) {
        if (providers.size > 1) {
          const group = document.createElement("optgroup");
          group.label = provider || "Weitere";
          group.append(...list.map(makeOption));
          nodes.push(group);
        } else {
          nodes.push(...list.map(makeOption));
        }
      }
      const previous = modelSelect.value;
      modelSelect.replaceChildren(...nodes);

      // Vorauswahl: bisherige Auswahl, zuletzt in diesem Chat genutzt, zuletzt
      // gewählt, sonst das erste verfügbare Modell.
      const usable = models.filter(isAvailable);
      const ids = new Set(usable.map((m) => String(m.id)));
      const preferred = [previous, chat.dataset.defaultModel, storageGet(STORAGE_KEY)].find(
        (id) => id && ids.has(id),
      );
      modelSelect.value = preferred || (usable.length ? String(usable[0].id) : "");

      modelsReady = usable.length > 0;
      if (!modelsReady && models.some((m) => m.blocked_by_budget === true)) {
        setStatus(
          "Monatsbudget ausgeschöpft – bis zum Monatsende sind nur lokale Modelle nutzbar. Zurzeit ist kein lokales Modell erreichbar.",
          true,
        );
      } else if (!modelsReady) {
        setStatus("Zurzeit ist kein Modell erreichbar. Lokale Modelle sind offline.", true);
      } else if (statusLine.textContent.startsWith("Zurzeit ist kein Modell")) {
        setStatus("");
      }
      setStreaming(Boolean(controller));
      updateToolSwitches();
      document.dispatchEvent(new CustomEvent("multigpt:models-loaded"));
    }

    // --- Schalter je MCP-Server (M4a-06) ---

    const toolFieldset = document.getElementById("tool-servers");
    const toolSwitchList = document.getElementById("tool-servers-list");

    function modelSupportsTools() {
      return modelSelect?.selectedOptions[0]?.dataset.tools === "true";
    }

    // Nur sichtbar, wenn es Server gibt und das gewählte Modell Werkzeuge kann.
    function updateToolSwitches() {
      if (toolFieldset) {
        toolFieldset.hidden = !mcpServers.length || !modelSupportsTools();
      }
    }

    async function loadMcpServers() {
      if (!toolFieldset || !urls.mcpServers) {
        return;
      }
      try {
        const response = await fetch(urls.mcpServers, {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        if (!response.ok) {
          throw new Error(await errorMessage(response));
        }
        const data = await response.json();
        mcpServers = Array.isArray(data) ? data.filter((s) => s && s.id != null) : [];
      } catch {
        mcpServers = []; // Ohne Liste eben ohne Werkzeuge; Chatten geht weiter.
      }
      toolSwitchList.replaceChildren(
        ...mcpServers.map((server) => {
          const label = document.createElement("label");
          label.className = "tool-switch";
          const input = document.createElement("input");
          input.type = "checkbox";
          input.name = "mcp_servers";
          input.value = String(server.id);
          input.checked = server.default_enabled !== false;
          const text = document.createElement("span");
          text.textContent = String(server.name ?? "");
          label.append(input, text);
          return label;
        }),
      );
      updateToolSwitches();
    }

    // Auswahl für den Request; null = Schlüssel weglassen (Voreinstellung des Servers).
    function selectedMcpServers() {
      if (!toolFieldset || toolFieldset.hidden) {
        return null;
      }
      return Array.from(toolSwitchList.querySelectorAll("input:checked"), (i) => Number(i.value));
    }

    // Ergänzt Werkzeugauswahl und Websuche (nur wenn der Schalter angezeigt wird).
    function withTools(payload) {
      const servers = selectedMcpServers();
      const result = servers ? { ...payload, mcp_servers: servers } : { ...payload };
      if (webSearchToggle) {
        result.web_search = webSearchToggle.checked;
      }
      // Sammlungen für die Dokumentsuche (M7, collections_picker.js).
      return window.MultiGPT.collectionPicker?.extend(result) ?? result;
    }

    // --- Schalter Websuche (M8) ---
    // Nur vorhanden, wenn die Websuche eingeschaltet ist und das Recht besteht (Server).

    const webSearchToggle = document.getElementById("web-search-toggle");
    if (webSearchToggle) {
      webSearchToggle.checked = storageGet(WEB_SEARCH_KEY) === "1";
      webSearchToggle.addEventListener("change", () => {
        storageSet(WEB_SEARCH_KEY, webSearchToggle.checked ? "1" : "0");
      });
    }

    // --- Senden und Streamen ---

    async function ensureConversation(signal) {
      if (conversationId) {
        return currentConversation();
      }
      const model = selectedModel();
      const response = await postJson(
        urls.conversations,
        model ? { default_model: model.id } : {},
        signal,
      );
      if (!response.ok) {
        throw new Error(await errorMessage(response));
      }
      const conv = await response.json();
      conversationId = String(conv.id);
      chat.dataset.conversationId = conversationId;
      if (conv.url) {
        window.history.replaceState(null, "", conv.url);
      }
      markSidebar({ id: conversationId, url: conv.url || currentConversation().url, title: conv.title });
      return currentConversation();
    }

    // --- System-Prompt des Chats (M5-03) ---

    const promptForm = document.getElementById("system-prompt-form");
    const promptInput = document.getElementById("system-prompt-input");
    const promptSave = document.getElementById("system-prompt-save");
    let savedPrompt = promptInput ? promptInput.value.trim() : "";

    function promptDirty() {
      return Boolean(promptInput) && promptInput.value.trim() !== savedPrompt;
    }

    function updatePromptUi() {
      if (promptSave) {
        promptSave.disabled = !promptDirty();
      }
      const summary = document.querySelector("#chat-settings > summary");
      if (!summary) {
        return;
      }
      let badge = summary.querySelector(".badge");
      if (savedPrompt && !badge) {
        badge = document.createElement("span");
        badge.className = "badge";
        badge.textContent = "aktiv";
        summary.append(" ", badge);
      } else if (!savedPrompt && badge) {
        badge.remove();
      }
    }

    async function saveSystemPrompt() {
      const data = await window.MultiGPT.requestJson(
        "PATCH",
        fillTemplate(urls.detailTemplate, conversationId),
        { system_prompt: promptInput.value },
      );
      savedPrompt = (data.system_prompt || "").trim();
      promptInput.value = data.system_prompt || "";
      const hint = document.getElementById("system-prompt-hint");
      if (hint) {
        hint.textContent = "Gilt für alle weiteren Antworten in diesem Chat.";
      }
      updatePromptUi();
    }

    // Vor dem Senden: ungespeicherte Änderung (bei neuem Chat: eingegebener Prompt) sichern.
    async function syncSystemPrompt() {
      if (promptDirty()) {
        await saveSystemPrompt();
      }
    }

    // Führt einen Stream aus. opts: userEl/restoreText (neue Nachricht),
    // replaced (ausgeblendete Nachrichten, die der neue Zweig ersetzt – beim Neu
    // erzeugen und Bearbeiten; erst bei "start" entfernt), onUndo (Server hat
    // nichts gespeichert) oder url + continuation (Fortsetzung derselben Antwort
    // nach einer Rückfrage). Danach wird der Verlauf vom Server neu geladen.
    async function runStream(payload, assistantEl, opts) {
      const replaced = [].concat(opts.replaced || []);
      const ctrl = new AbortController();
      controller = ctrl;
      setStreaming(true);
      assistantEl.dataset.streaming = "true";
      const contentEl = assistantEl.querySelector(".chat-message-content");
      contentEl.dataset.markdown = "true";
      let stick = true;
      const renderer = markdown
        ? markdown.streamRenderer(
            contentEl,
            () => {
              if (stick) {
                scrollToBottom();
              }
            },
            opts.continuation ? markdown.sourceOf(contentEl) : "",
          )
        : null;
      const textNode = document.createTextNode("");
      if (!renderer) {
        contentEl.append(textNode);
      }
      setStatus("Antwort wird erzeugt …");

      let started = false;
      let rejected = false;
      // Statuszeile zeigt einen Text des Servers: "info" weicht beim ersten
      // delta, ein Hinweis ("warning", z. B. Websuche fehlgeschlagen) bleibt bis done.
      let serverStatus = null;
      let status = null;
      let errorText = "";

      const undo = (message) => {
        // Server hat nichts gespeichert: Anzeige zurücksetzen.
        rejected = true;
        if (opts.continuation) {
          // Rückfrage bleibt offen; Knöpfe wieder freigeben.
          renderer?.finish();
          setConfirmDisabled(assistantEl, false);
          for (const button of assistantEl.querySelectorAll("[data-tool-decision]")) {
            button.setAttribute("aria-pressed", "false");
          }
          delete assistantEl.dataset.streaming;
          setStatus(message, true);
          return;
        }
        assistantEl.remove();
        if (opts.userEl) {
          opts.userEl.remove();
          if (!opts.onUndo && !textarea.value) {
            textarea.value = opts.restoreText;
            autosize();
          }
        }
        for (const el of replaced) {
          el.hidden = false;
        }
        if (emptyState && !messageElements().length) {
          emptyState.hidden = false;
        }
        setStatus(message, true);
        opts.onUndo?.();
      };

      try {
        await ensureConversation(ctrl.signal);
        await syncSystemPrompt();
        const settings = document.getElementById("chat-settings");
        if (settings?.open && !settings.contains(document.activeElement)) {
          settings.open = false; // Platz für den Verlauf
        }
        if (opts.userEl) {
          markSidebar(currentConversation(), opts.restoreText);
        }
        const response = await postJson(
          opts.url || fillTemplate(urls.messagesTemplate, conversationId),
          payload,
          ctrl.signal,
          "text/event-stream",
        );
        const type = response.headers.get("Content-Type") || "";
        if (!response.ok || !type.startsWith("text/event-stream") || !response.body) {
          if (response.status === 503 || response.status === 409) {
            // Lokaler Anbieter offline oder Modell nicht geladen: Status neu prüfen.
            document.dispatchEvent(new CustomEvent("multigpt:provider-status-refresh"));
          } else if (response.status === 403) {
            // z. B. Monatsbudget gerade ausgeschöpft: Modellauswahl neu laden (M6-03).
            loadModels();
          }
          undo(await errorMessage(response));
          return;
        }
        for await (const { event, data } of readEvents(response)) {
          if (event === "start") {
            started = true;
            if (opts.userEl && data.user_message_id != null) {
              opts.userEl.dataset.messageId = String(data.user_message_id);
            }
            if (data.assistant_message_id != null) {
              assistantEl.dataset.messageId = String(data.assistant_message_id);
            }
            for (const el of replaced) {
              el.remove();
            }
            expireConfirmations(assistantEl);
            if (opts.continuation) {
              assistantEl.querySelector(".tool-confirm")?.remove();
              assistantEl.dataset.status = "streaming";
            }
          } else if (event === "tool_call") {
            upsertToolCall(assistantEl, data);
            refreshConfirmBox(assistantEl);
            if (stick) {
              scrollToBottom();
            }
          } else if (event === "tool_result") {
            applyToolResult(assistantEl, data);
          } else if (event === "confirmation_required") {
            const ids = Array.isArray(data.tool_call_ids) ? data.tool_call_ids : [];
            showConfirmation(assistantEl, ids);
          } else if (event === "status") {
            if (typeof data.text === "string" && data.text) {
              serverStatus = data.level === "warning" ? "warning" : "info";
              setStatus(data.text, serverStatus === "warning" ? "warning" : false);
            }
          } else if (event === "sources") {
            stick = isNearBottom();
            renderSources(assistantEl, data.sources);
            const count = assistantEl.querySelectorAll(".message-source").length;
            if (count && serverStatus !== "warning") {
              setStatus(`${count === 1 ? "1 Quelle" : `${count} Quellen`} gefunden. Antwort wird erzeugt …`);
              serverStatus = "info";
            }
            if (stick) {
              scrollToBottom();
            }
          } else if (event === "delta") {
            if (serverStatus === "info") {
              serverStatus = null;
              setStatus("Antwort wird erzeugt …");
            }
            stick = isNearBottom();
            const chunk = typeof data.text === "string" ? data.text : "";
            if (renderer) {
              renderer.append(chunk);
            } else {
              textNode.appendData(chunk);
            }
            if (stick) {
              scrollToBottom();
            }
          } else if (event === "error") {
            errorText = typeof data.message === "string" ? data.message : "";
          } else if (event === "done") {
            status = typeof data.status === "string" ? data.status : "complete";
          }
          // "usage" wird erst in M6 angezeigt.
        }
        if (!status) {
          status = "error";
          errorText = errorText || "Die Verbindung wurde unerwartet beendet.";
        }
      } catch (err) {
        if (ctrl.signal.aborted) {
          if (!started && !opts.userEl?.dataset.messageId && !conversationId) {
            undo("Abgebrochen.");
            return;
          }
          status = "aborted";
        } else if (!started) {
          undo(err && err.message ? err.message : "Der Server ist nicht erreichbar.");
          return;
        } else {
          status = "error";
          errorText = "Die Verbindung wurde unterbrochen.";
        }
      } finally {
        controller = null;
        if (!rejected) {
          if (!started) {
            // Abbruch vor "start": Was der Server gespeichert hat, zeigt das
            // Neuladen des Verlaufs unten.
            for (const el of replaced) {
              el.remove();
            }
          }
          delete assistantEl.dataset.streaming;
          stick = isNearBottom();
          renderer?.finish();
          if (status === "error" && !errorText) {
            errorText = "Unbekannter Fehler.";
          }
          setMessageStatus(assistantEl, status, errorText);
          if (status !== "awaiting_confirmation") {
            interruptRunningTools(assistantEl);
            assistantEl.querySelector(".tool-confirm")?.remove();
          }
          if (status === "awaiting_confirmation") {
            setConfirmDisabled(assistantEl, false);
            setStatus("Bestätigung nötig: Bitte Werkzeugaufrufe ausführen oder ablehnen.");
          } else if (status === "aborted") {
            setStatus(errorText ? `Antwort abgebrochen: ${errorText}` : "Antwort abgebrochen.");
          } else if (status === "error") {
            setStatus(`Fehler: ${errorText}`, true);
          } else {
            setStatus("");
          }
        }
        setStreaming(false);
        if (!rejected) {
          // Serverstand übernehmen: Versionszähler, Bearbeiten-/Kopierknöpfe.
          await refreshMessages(opts.userEl || assistantEl);
        }
        const shown = messageById(assistantEl.dataset.messageId) || assistantEl;
        if (!rejected && status === "awaiting_confirmation") {
          // Ohne Scrollen: sonst verschiebt sich das ganze Layout statt des Verlaufs.
          shown.querySelector("[data-tool-decision]")?.focus({ preventScroll: true });
          revealConfirmation(shown);
        } else {
          returnFocus();
        }
      }
    }

    async function send() {
      if (controller || !modelsReady) {
        return;
      }
      const text = textarea.value;
      if (!text.trim()) {
        textarea.focus();
        return;
      }
      // Vergleichsmodus (M6, compare.js): eigene Spalten statt einer Antwort.
      const compare = window.MultiGPT.compare;
      if (compare?.pending()) {
        setStatus("Bitte zuerst eine Antwort wählen („Mit dieser Antwort weiter“).", true);
        return;
      }
      if (compare?.isActive()) {
        await compare.send(text);
        return;
      }
      const model = selectedModel();
      if (!model) {
        setStatus("Bitte zuerst ein Modell wählen.", true);
        modelSelect.focus();
        return;
      }
      storageSet(STORAGE_KEY, String(model.id));
      const userEl = appendMessage("user", "Du", text);
      const assistantEl = appendMessage("assistant", model.name, "");
      textarea.value = "";
      autosize();
      scrollToBottom();
      await runStream(withTools({ content: text, model: model.id }), assistantEl, {
        userEl,
        restoreText: text,
      });
    }

    // Ausgeblendet werden die Nachricht und alles danach; der neue Zweig ersetzt sie.
    function hideFrom(article) {
      const all = Array.from(log.querySelectorAll(".chat-message"));
      const hidden = all.slice(all.indexOf(article)).filter((el) => !el.hidden);
      for (const el of hidden) {
        el.hidden = true;
      }
      return hidden;
    }

    // Neue Version einer Antwort (Geschwister mit gleichem Vorgänger).
    async function regenerate(article) {
      if (controller || !modelsReady) {
        return;
      }
      const model = selectedModel();
      if (!article || article.dataset.role !== "assistant" || !article.dataset.messageId || !model) {
        return;
      }
      closeEditor(false);
      storageSet(STORAGE_KEY, String(model.id));
      const replaced = hideFrom(article);
      const assistantEl = appendMessage("assistant", model.name, "");
      scrollToBottom();
      sendButton.focus();
      await runStream(
        withTools({ regenerate: true, model: model.id, message_id: Number(article.dataset.messageId) }),
        assistantEl,
        { replaced },
      );
    }

    // --- Verlauf vom Server (Fragment chat:conversation_messages) ---

    function messageById(id) {
      return id ? log.querySelector(`.chat-message[data-message-id="${CSS.escape(String(id))}"]`) : null;
    }

    // Ersetzt die Nachrichten ab fromEl durch den Stand des Servers (gleiche
    // Templates wie beim Neuladen: Markdown, Werkzeugzeilen, Rückfrage-Knöpfe).
    // Davor liegende Nachrichten bleiben stehen, soweit sie zum Pfad passen.
    async function refreshMessages(fromEl) {
      if (!conversationId || !urls.fragmentTemplate) {
        return false;
      }
      let html;
      try {
        const response = await fetch(fillTemplate(urls.fragmentTemplate, conversationId), {
          credentials: "same-origin",
          headers: { Accept: "text/html" },
        });
        if (!response.ok) {
          return false;
        }
        html = await response.text();
      } catch {
        return false;
      }
      if (controller) {
        return false; // Inzwischen läuft ein neuer Stream; der lädt danach selbst.
      }
      // Eigenes, serverseitig escapetes Template; DOMParser führt nichts aus.
      const parsed = new DOMParser().parseFromString(`<body>${html}</body>`, "text/html");
      const fresh = Array.from(parsed.body.querySelectorAll(":scope > .chat-message"));
      const current = Array.from(log.querySelectorAll(".chat-message"));
      let index = Math.max(0, fromEl ? current.indexOf(fromEl) : 0);
      while (
        index > 0 &&
        (current[index - 1].hidden ||
          current[index - 1].dataset.messageId !== fresh[index - 1]?.dataset.messageId)
      ) {
        index -= 1;
      }
      const stick = isNearBottom();
      for (const el of current.slice(index)) {
        el.remove();
      }
      const added = fresh.slice(index).map((el) => document.adoptNode(el));
      log.append(...added);
      markdown?.renderAll(log);
      for (const article of added) {
        refreshConfirmBox(article);
      }
      if (editing && !editing.article.isConnected) {
        editing = null;
      }
      if (emptyState) {
        emptyState.hidden = Boolean(log.querySelector(".chat-message"));
      }
      updateMessageActions();
      if (stick) {
        scrollToBottom();
      }
      return true;
    }

    // --- Versionen umschalten (POST branch) ---

    async function switchVersion(article, button) {
      const target = button.dataset.versionTarget;
      if (!target || !urls.branchTemplate || !conversationId) {
        return;
      }
      const direction = button.getAttribute("aria-label");
      const index = Array.from(log.querySelectorAll(".chat-message")).indexOf(article);
      for (const other of article.querySelectorAll("[data-version-target]")) {
        other.disabled = true;
      }
      closeEditor(false);
      log.setAttribute("aria-busy", "true");
      try {
        const response = await postJson(fillTemplate(urls.branchTemplate, conversationId), {
          message_id: Number(target),
        });
        if (!response.ok) {
          throw new Error(await errorMessage(response));
        }
        if (!(await refreshMessages(article))) {
          throw new Error("Der Verlauf konnte nicht geladen werden.");
        }
      } catch (err) {
        setStatus(`Version konnte nicht gewechselt werden. ${err.message || ""}`.trim(), true);
        updateMessageActions();
        return;
      } finally {
        log.setAttribute("aria-busy", String(Boolean(controller)));
      }
      const shown = log.querySelectorAll(".chat-message")[index];
      const label = shown?.querySelector(".message-versions .sr-only")?.textContent;
      setStatus(label ? `${label} angezeigt.` : "");
      const same = shown?.querySelector(`[data-version-target][aria-label="${direction}"]`);
      const focusTarget =
        same && !same.disabled ? same : shown?.querySelector("[data-version-target]:not(:disabled)");
      focusTarget?.focus({ preventScroll: true });
    }

    // --- Kopieren ---

    async function copyMessage(article, button) {
      const content = article.querySelector(".chat-message-content");
      const text =
        article.dataset.role === "assistant" && markdown ? markdown.sourceOf(content) : content.textContent;
      const isUser = article.dataset.role === "user";
      try {
        if (!markdown?.copyText) {
          throw new Error("unavailable");
        }
        await markdown.copyText(text);
        button.classList.add("is-done");
        window.setTimeout(() => button.classList.remove("is-done"), 2000);
        if (!controller) {
          setStatus(isUser ? "Nachricht kopiert." : "Antwort kopiert.");
        }
      } catch {
        setStatus("Kopieren ist fehlgeschlagen.", true);
      }
    }

    // --- Bearbeiten (neuer Zweig ab einer eigenen Nachricht) ---

    let editing = null; // {article, box}

    function closeEditor(restoreFocus = true) {
      if (!editing) {
        return;
      }
      const { article, box } = editing;
      editing = null;
      box.remove();
      article.classList.remove("is-editing");
      article.querySelector(".chat-message-content").hidden = false;
      updateMessageActions();
      if (restoreFocus) {
        article.querySelector("[data-edit-message]")?.focus();
      }
    }

    function openEditor(article, text) {
      closeEditor(false);
      const content = article.querySelector(".chat-message-content");
      const box = node("form", "message-editor");
      box.setAttribute("aria-label", "Nachricht bearbeiten");
      const area = node("textarea", "message-editor-input");
      area.id = `message-edit-${article.dataset.messageId}`;
      area.rows = 2;
      area.value = text ?? content.textContent;
      area.setAttribute("aria-describedby", "message-editor-hint");
      const label = node("label", "sr-only", "Nachricht bearbeiten");
      label.htmlFor = area.id;
      const hint = node(
        "p",
        "message-editor-hint",
        "Enter sendet, Shift+Enter fügt einen Zeilenumbruch ein, Escape bricht ab. " +
          "Die bisherige Fassung bleibt als Version erhalten.",
      );
      hint.id = "message-editor-hint";
      const actions = node("div", "message-editor-actions");
      const cancel = node("button", "button button-small", "Abbrechen");
      cancel.type = "button";
      const submit = node("button", "button button-small button-primary", "Senden");
      submit.type = "submit";
      actions.append(cancel, submit);
      box.append(label, area, hint, actions);
      content.hidden = true;
      content.after(box);
      article.classList.add("is-editing");
      editing = { article, box };
      updateMessageActions();

      const resize = () => {
        area.style.height = "auto";
        area.style.height = `${area.scrollHeight + 2}px`;
      };
      area.addEventListener("input", resize);
      area.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          closeEditor();
        } else if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
          event.preventDefault();
          box.requestSubmit();
        }
      });
      cancel.addEventListener("click", () => closeEditor());
      box.addEventListener("submit", (event) => {
        event.preventDefault();
        submitEdit(article, area);
      });
      resize();
      area.focus();
      area.setSelectionRange(area.value.length, area.value.length);
    }

    async function submitEdit(article, area) {
      if (controller) {
        setStatus("Bitte warte, bis die laufende Antwort fertig ist.", true);
        return;
      }
      if (!modelsReady) {
        return;
      }
      const text = area.value;
      if (!text.trim()) {
        area.focus();
        return;
      }
      const model = selectedModel();
      if (!model) {
        setStatus("Bitte zuerst ein Modell wählen.", true);
        modelSelect.focus();
        return;
      }
      storageSet(STORAGE_KEY, String(model.id));
      closeEditor(false);
      const replaced = hideFrom(article);
      const userEl = appendMessage("user", "Du", text);
      const assistantEl = appendMessage("assistant", model.name, "");
      scrollToBottom();
      sendButton.focus();
      await runStream(
        withTools({ content: text, model: model.id, edit_of: Number(article.dataset.messageId) }),
        assistantEl,
        { userEl, replaced, onUndo: () => openEditor(article, text) },
      );
    }

    log.addEventListener("click", (event) => {
      const button = event.target.closest(
        "[data-copy-message], [data-edit-message], [data-regenerate-message], [data-version-target]",
      );
      const article = button?.closest(".chat-message");
      if (!button || !article || button.disabled) {
        return;
      }
      if (button.hasAttribute("data-copy-message")) {
        copyMessage(article, button);
      } else if (isBusy()) {
        setStatus("Bitte warte, bis die laufende Antwort fertig ist.", true);
      } else if (button.hasAttribute("data-edit-message")) {
        openEditor(article);
      } else if (button.hasAttribute("data-regenerate-message")) {
        regenerate(article);
      } else {
        switchVersion(article, button);
      }
    });

    function autosize() {
      textarea.style.height = "auto";
      textarea.style.height = `${textarea.scrollHeight + 2}px`;
      updateInputCopy();
    }

    // --- Eingabe kopieren ---

    const inputCopyButton = document.getElementById("input-copy-button");
    let inputCopyTimer = null;

    function updateInputCopy() {
      if (inputCopyButton) {
        inputCopyButton.disabled = !textarea.value;
      }
    }

    async function copyInput() {
      if (!textarea.value || !markdown?.copyText) {
        return;
      }
      const done = inputCopyButton.querySelector(".input-copy-done");
      try {
        await markdown.copyText(textarea.value);
        inputCopyButton.classList.add("is-done");
        done.hidden = false;
        window.clearTimeout(inputCopyTimer);
        inputCopyTimer = window.setTimeout(() => {
          inputCopyButton.classList.remove("is-done");
          done.hidden = true;
        }, 2000);
        if (!controller) {
          setStatus("Eingabe kopiert.");
        }
      } catch {
        setStatus("Kopieren ist fehlgeschlagen.", true);
      }
      // Der Ersatzweg über eine Textauswahl nimmt den Fokus weg.
      if (!inputCopyButton.disabled) {
        inputCopyButton.focus({ preventScroll: true });
      }
    }

    // --- Neuer Chat ---

    newChatButton?.addEventListener("click", async (event) => {
      if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey) {
        return; // Neuer Tab o. Ä.: normaler Link zur Startseite
      }
      event.preventDefault();
      if (form && !messageElements().length && !controller) {
        // Der aktuelle Chat ist noch leer – einfach hier schreiben.
        document.body.classList.remove("sidebar-open");
        textarea.focus();
        return;
      }
      const model = selectedModel();
      try {
        const response = await postJson(
          urls.conversations,
          model ? { default_model: model.id } : {},
        );
        if (!response.ok) {
          throw new Error(await errorMessage(response));
        }
        const conv = await response.json();
        window.location.assign(conv.url || fillTemplate(urls.conversationTemplate, conv.id));
      } catch (err) {
        setStatus(`Neuer Chat konnte nicht angelegt werden. ${err.message || ""}`.trim(), true);
      }
    });

    // --- Schnittstelle für den Vergleichsmodus (compare.js, M6) ---
    // begin() übernimmt den Senden/Abbrechen-Knopf (liefert einen
    // AbortController, null wenn schon etwas läuft), end() gibt ihn frei.

    window.MultiGPT.chatView = {
      readEvents,
      postJson,
      ensureConversation,
      syncSystemPrompt,
      withTools,
      refreshMessages,
      appendMessage,
      setStatus,
      markSidebar,
      currentConversation,
      selectedModel,
      scrollToBottom,
      isNearBottom,
      messageElements,
      returnFocus,
      autosize,
      updateMessageActions,
      storeModel(id) {
        storageSet(STORAGE_KEY, String(id));
      },
      isReady() {
        return modelsReady;
      },
      branchUrl() {
        return conversationId && urls.branchTemplate
          ? fillTemplate(urls.branchTemplate, conversationId)
          : null;
      },
      messagesUrl() {
        return fillTemplate(urls.messagesTemplate, conversationId);
      },
      begin() {
        if (controller) {
          return null;
        }
        controller = new AbortController();
        setStreaming(true);
        return controller;
      },
      end() {
        controller = null;
        setStreaming(false);
      },
    };

    // --- Start ---

    scrollToBottom();
    updateMessageActions();
    if (!form) {
      return;
    }

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (controller) {
        controller.abort();
      } else {
        send();
      }
    });
    // Abbrechen direkt am Knopf, unabhängig von der Formularprüfung.
    sendButton.addEventListener("click", (event) => {
      if (controller) {
        event.preventDefault();
        controller.abort();
      }
    });

    textarea.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        if (!controller) {
          send();
        }
      }
    });
    textarea.addEventListener("input", autosize);
    if (inputCopyButton) {
      inputCopyButton.hidden = !markdown?.copyText;
      inputCopyButton.addEventListener("click", copyInput);
      updateInputCopy();
    }

    promptInput?.addEventListener("input", updatePromptUi);
    promptForm?.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!conversationId) {
        setStatus("Der System-Prompt wird mit der ersten Nachricht gespeichert.");
        return;
      }
      try {
        await saveSystemPrompt();
        setStatus("System-Prompt gespeichert.");
      } catch (err) {
        setStatus(`System-Prompt konnte nicht gespeichert werden: ${err.message}`, true);
      }
    });

    // Lokaler Anbieter online/offline oder neue Modelle gemeldet (provider_status.js).
    document.addEventListener("multigpt:provider-status", (event) => {
      if (event.detail && event.detail.changed) {
        loadModels();
      }
    });

    modelSelect.addEventListener("change", () => {
      if (modelSelect.value) {
        storageSet(STORAGE_KEY, modelSelect.value);
      }
      updateToolSwitches();
    });

    setStreaming(false);
    for (const article of log.querySelectorAll(".chat-message")) {
      refreshConfirmBox(article);
    }
    const waiting = log.querySelectorAll('.chat-message[data-status="awaiting_confirmation"]');
    if (waiting.length) {
      revealConfirmation(waiting[waiting.length - 1]);
    }
    loadModels();
    loadMcpServers();
    textarea.focus();
  });
})();
