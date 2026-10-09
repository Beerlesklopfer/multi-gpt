// MultiGPT – Chatansicht (M3-05): Modellauswahl, Senden, Streaming, Abbrechen,
// Neu erzeugen, neuer Chat. Kein Inline-JS, keine externen Ressourcen.
//
// Sicherheit (Plan 9): Nutzertext wird ausschließlich als Text eingesetzt
// (textContent), Modelltext nur über markdown.js (marked -> DOMPurify).
"use strict";

(() => {
  const STORAGE_KEY = "multigpt.lastModel";
  const SCROLL_STICK_PX = 48;
  const TITLE_MAX = 60;

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
    };
    let conversationId = chat.dataset.conversationId || null;
    let controller = null; // AbortController des laufenden Streams
    let modelsReady = false;

    // --- Anzeige ---

    function setStatus(text, isError = false) {
      statusLine.textContent = text;
      statusLine.classList.toggle("is-error", Boolean(text) && isError);
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

    // "Neu erzeugen" nur an der letzten Antwort.
    function updateRegenerateButton() {
      for (const actions of log.querySelectorAll(".chat-message-actions")) {
        actions.remove();
      }
      if (!form || controller) {
        return;
      }
      const last = messageElements().at(-1);
      if (!last || last.dataset.role !== "assistant") {
        return;
      }
      const actions = document.createElement("div");
      actions.className = "chat-message-actions";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "link-button";
      button.textContent = "Neu erzeugen";
      button.title = "Antwort mit dem gewählten Modell neu erzeugen";
      button.disabled = !modelsReady;
      button.addEventListener("click", regenerate);
      actions.append(button);
      const stick = isNearBottom();
      last.append(actions);
      if (stick) {
        scrollToBottom();
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
      updateRegenerateButton();
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
      const isAvailable = (model) => model.available !== false;
      const makeOption = (model) => {
        let label = model.display_name;
        if (model.is_local) {
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
      if (!modelsReady) {
        setStatus("Zurzeit ist kein Modell erreichbar. Lokale Modelle sind offline.", true);
      } else if (statusLine.textContent.startsWith("Zurzeit ist kein Modell")) {
        setStatus("");
      }
      setStreaming(Boolean(controller));
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

    // Führt einen Stream aus. opts: userEl/restoreText (neue Nachricht) oder
    // replaced (alte Antwort beim Neu erzeugen, erst bei "start" entfernt).
    async function runStream(payload, assistantEl, opts) {
      const ctrl = new AbortController();
      controller = ctrl;
      setStreaming(true);
      assistantEl.dataset.streaming = "true";
      const contentEl = assistantEl.querySelector(".chat-message-content");
      contentEl.dataset.markdown = "true";
      let stick = true;
      const renderer = markdown
        ? markdown.streamRenderer(contentEl, () => {
            if (stick) {
              scrollToBottom();
            }
          })
        : null;
      const textNode = document.createTextNode("");
      if (!renderer) {
        contentEl.append(textNode);
      }
      setStatus("Antwort wird erzeugt …");

      let started = false;
      let rejected = false;
      let status = null;
      let errorText = "";

      const undo = (message) => {
        // Server hat nichts gespeichert: Anzeige zurücksetzen.
        rejected = true;
        assistantEl.remove();
        if (opts.userEl) {
          opts.userEl.remove();
          if (!textarea.value) {
            textarea.value = opts.restoreText;
            autosize();
          }
        }
        if (opts.replaced) {
          opts.replaced.hidden = false;
        }
        if (emptyState && !messageElements().length) {
          emptyState.hidden = false;
        }
        setStatus(message, true);
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
          fillTemplate(urls.messagesTemplate, conversationId),
          payload,
          ctrl.signal,
          "text/event-stream",
        );
        const type = response.headers.get("Content-Type") || "";
        if (!response.ok || !type.startsWith("text/event-stream") || !response.body) {
          if (response.status === 503 || response.status === 409) {
            // Lokaler Anbieter offline oder Modell nicht geladen: Status neu prüfen.
            document.dispatchEvent(new CustomEvent("multigpt:provider-status-refresh"));
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
            opts.replaced?.remove();
          } else if (event === "delta") {
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
            // Abbruch vor "start": Was der Server gespeichert hat, zeigt ein Neuladen.
            opts.replaced?.remove();
          }
          delete assistantEl.dataset.streaming;
          stick = isNearBottom();
          renderer?.finish();
          if (status === "error" && !errorText) {
            errorText = "Unbekannter Fehler.";
          }
          setMessageStatus(assistantEl, status, errorText);
          if (status === "aborted") {
            setStatus(errorText ? `Antwort abgebrochen: ${errorText}` : "Antwort abgebrochen.");
          } else if (status === "error") {
            setStatus(`Fehler: ${errorText}`, true);
          } else {
            setStatus("");
          }
        }
        setStreaming(false);
        returnFocus();
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
      await runStream({ content: text, model: model.id }, assistantEl, {
        userEl,
        restoreText: text,
      });
    }

    async function regenerate() {
      if (controller || !modelsReady) {
        return;
      }
      const last = messageElements().at(-1);
      const model = selectedModel();
      if (!last || last.dataset.role !== "assistant" || !model) {
        return;
      }
      storageSet(STORAGE_KEY, String(model.id));
      last.hidden = true;
      const assistantEl = appendMessage("assistant", model.name, "");
      scrollToBottom();
      sendButton.focus();
      await runStream({ regenerate: true, model: model.id }, assistantEl, { replaced: last });
    }

    function autosize() {
      textarea.style.height = "auto";
      textarea.style.height = `${textarea.scrollHeight + 2}px`;
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

    // --- Start ---

    scrollToBottom();
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
    });

    setStreaming(false);
    loadModels();
    textarea.focus();
  });
})();
