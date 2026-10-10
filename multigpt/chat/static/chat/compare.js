// MultiGPT – Vergleichsmodus (M6, Plan 8 Punkt 7).
// Eine Nachricht geht an 2–3 Modelle; die Antworten erscheinen als Spalten
// nebeneinander (mobil untereinander). Gespeichert werden sie als Geschwister
// im Gesprächsbaum: erste Spalte per POST messages (content), weitere Spalten
// per regenerate mit message_id der ersten Antwort (nacheinander gestartet,
// parallel gestreamt; alle mit
// compare: true – ohne MCP-Werkzeuge, current_leaf bleibt bei der ersten
// Spalte). „Mit dieser Antwort weiter“ setzt den Zweig (POST branch); danach
// zeigt die normale Ansicht die Versionen „‹ i/n ›“.
//
// Sicherheit (Plan 9): Modelltext nur über markdown.js (bereinigt), Modellnamen
// und Fehlertexte nur als Text. Kein Inline-JS.
"use strict";

(() => {
  const MIN_MODELS = 2;
  const MAX_MODELS = 3;

  let active = false; // Schalter „Vergleichen“ an
  let pending = false; // Antworten liegen vor, Wahl steht aus

  // Von chat.js abgefragt (send(), Knöpfe im Verlauf).
  window.MultiGPT = window.MultiGPT || {};
  window.MultiGPT.compare = {
    isActive: () => active,
    pending: () => pending,
    send: async () => {},
  };

  function formatEur(raw) {
    if (raw === null || raw === undefined || raw === "") {
      return null;
    }
    const value = Number(raw);
    if (!Number.isFinite(value)) {
      return null;
    }
    const digits = value > 0 && value < 0.01 ? 4 : 2;
    return `${value.toLocaleString("de-DE", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    })} €`;
  }

  function formatUsage(usage) {
    const tokensIn = Number(usage.tokens_in) || 0;
    const tokensOut = Number(usage.tokens_out) || 0;
    const tokens = `${(tokensIn + tokensOut).toLocaleString("de-DE")} Tokens (${tokensIn.toLocaleString(
      "de-DE",
    )} ein, ${tokensOut.toLocaleString("de-DE")} aus)`;
    const cost = formatEur(usage.cost);
    return `${tokens} · ${cost ? cost : "Kosten unbekannt"}`;
  }

  document.addEventListener("DOMContentLoaded", () => {
    const view = window.MultiGPT.chatView;
    const form = document.getElementById("chat-form");
    const toggle = document.getElementById("compare-toggle");
    const panel = document.getElementById("compare-panel");
    if (!view || !form || !toggle || !panel) {
      return;
    }
    const toggleLabel = toggle.closest("label");
    const picker = document.getElementById("compare-models");
    const pickerList = document.getElementById("compare-models-list");
    const modelSelect = document.getElementById("model-select");
    const textarea = document.getElementById("message-input");
    const columnsEl = document.getElementById("compare-columns");
    const hint = document.getElementById("compare-hint");
    const template = document.getElementById("compare-column-template");
    const markdown = window.MultiGPT.markdown;
    const { errorMessage } = window.MultiGPT;

    let columns = []; // laufender bzw. zur Wahl stehender Vergleich
    let userEl = null;
    let streaming = false;

    // --- Auswahl der Modelle ---

    function modelOptions() {
      return Array.from(modelSelect.options).filter((o) => o.value);
    }

    function checkedBoxes() {
      return Array.from(pickerList.querySelectorAll("input:checked"));
    }

    function selectedModels() {
      return checkedBoxes()
        .filter((input) => !input.disabled)
        .map((input) => ({ id: Number(input.value), name: input.dataset.name }));
    }

    // Höchstens drei: weitere Kästchen sperren, solange drei gewählt sind.
    function updatePickerLimits() {
      const full = checkedBoxes().length >= MAX_MODELS;
      for (const input of pickerList.querySelectorAll("input")) {
        input.disabled = input.dataset.unavailable === "true" || (full && !input.checked);
      }
    }

    // Liste aus der Modellauswahl von chat.js (gleiche Freigaben und Zustände).
    function buildPicker() {
      const previous = new Set(checkedBoxes().map((i) => i.value));
      const options = modelOptions();
      toggleLabel.hidden = options.filter((o) => !o.disabled).length < MIN_MODELS;
      if (toggleLabel.hidden && active) {
        setActive(false);
      }
      const usable = options.filter((o) => !o.disabled).map((o) => o.value);
      if (!previous.size) {
        // Vorauswahl: gewähltes Modell und das nächste verfügbare.
        const first = usable.includes(modelSelect.value) ? modelSelect.value : usable[0];
        if (first) {
          previous.add(first);
        }
        const second = usable.find((id) => id !== first);
        if (second) {
          previous.add(second);
        }
      }
      let count = 0;
      pickerList.replaceChildren(
        ...options.map((option) => {
          const label = document.createElement("label");
          label.className = "tool-switch";
          const input = document.createElement("input");
          input.type = "checkbox";
          input.name = "compare_models";
          input.value = option.value;
          input.dataset.name = option.dataset.name || option.textContent;
          input.dataset.unavailable = String(option.disabled);
          input.checked = !option.disabled && previous.has(option.value) && count < MAX_MODELS;
          if (input.checked) {
            count += 1;
          }
          const text = document.createElement("span");
          text.textContent = option.textContent;
          label.append(input, text);
          return label;
        }),
      );
      updatePickerLimits();
    }

    function setActive(on) {
      active = on;
      toggle.checked = on;
      picker.hidden = !on;
      form.classList.toggle("is-comparing", on);
      if (on) {
        buildPicker();
      }
    }

    toggle.addEventListener("change", () => setActive(toggle.checked));
    pickerList.addEventListener("change", updatePickerLimits);
    document.addEventListener("multigpt:models-loaded", () => {
      if (active) {
        buildPicker();
      } else {
        toggleLabel.hidden = modelOptions().filter((o) => !o.disabled).length < MIN_MODELS;
      }
    });

    function setControlsDisabled(disabled) {
      toggle.disabled = disabled;
      picker.disabled = disabled;
    }

    // --- Spalten ---

    function createColumn(model) {
      const article = template.content.firstElementChild.cloneNode(true);
      article.querySelector(".compare-column-model").textContent = model.name;
      const col = {
        model,
        article,
        content: article.querySelector(".compare-column-content"),
        state: article.querySelector(".compare-column-state"),
        note: article.querySelector(".compare-column-note"),
        usage: article.querySelector(".compare-column-usage"),
        abortButton: article.querySelector("[data-compare-abort]"),
        chooseButton: article.querySelector("[data-compare-choose]"),
        controller: new AbortController(),
        messageId: null,
        status: "streaming",
        renderer: null,
      };
      col.state.textContent = "wartet …";
      col.abortButton.setAttribute("aria-label", `Antwort von ${model.name} abbrechen`);
      col.chooseButton.setAttribute("aria-label", `Mit der Antwort von ${model.name} weiter`);
      col.renderer = markdown ? markdown.streamRenderer(col.content, null) : null;
      col.abortButton.addEventListener("click", () => col.controller.abort());
      col.chooseButton.addEventListener("click", () => choose(col));
      return col;
    }

    function setNote(col, text, kind) {
      col.note.hidden = !text;
      col.note.textContent = text || "";
      col.note.className = `chat-message-status compare-column-note${
        kind ? ` chat-message-status-${kind}` : ""
      }`;
    }

    const STATE_TEXT = {
      streaming: "läuft …",
      complete: "fertig",
      aborted: "abgebrochen",
      error: "Fehler",
      failed: "nicht gestartet",
    };

    function finishColumn(col, status, errorText) {
      col.status = status;
      col.article.dataset.status = status;
      col.article.setAttribute("aria-busy", "false");
      delete col.article.dataset.streaming;
      col.state.textContent = STATE_TEXT[status] || status;
      col.renderer?.finish();
      col.abortButton.hidden = true;
      if (status === "aborted") {
        setNote(col, errorText ? `Abgebrochen: ${errorText}` : "Abgebrochen.", "aborted");
      } else if (status === "error" || status === "failed") {
        setNote(col, `Fehler: ${errorText || "Unbekannter Fehler."}`, "error");
      }
    }

    // Eine Spalte streamen. onStart(id|null) meldet, ob der Server die Antwort
    // angelegt hat (null: abgelehnt, z. B. Budget ausgeschöpft -> 403 JSON).
    async function runColumn(col, payload, onStart) {
      let started = false;
      let status = null;
      let errorText = "";
      const signal = col.controller.signal;
      col.state.textContent = STATE_TEXT.streaming;
      col.article.dataset.streaming = "true";
      try {
        const response = await view.postJson(view.messagesUrl(), payload, signal, "text/event-stream");
        const type = response.headers.get("Content-Type") || "";
        if (!response.ok || !type.startsWith("text/event-stream") || !response.body) {
          if (response.status === 503 || response.status === 409) {
            document.dispatchEvent(new CustomEvent("multigpt:provider-status-refresh"));
          }
          finishColumn(col, "failed", await errorMessage(response));
          onStart(null);
          return;
        }
        for await (const { event, data } of view.readEvents(response)) {
          if (event === "start") {
            started = true;
            col.messageId = data.assistant_message_id ?? null;
            col.article.dataset.messageId = String(col.messageId ?? "");
            if (userEl && data.user_message_id != null) {
              userEl.dataset.messageId = String(data.user_message_id);
            }
            onStart(col.messageId);
          } else if (event === "delta") {
            const chunk = typeof data.text === "string" ? data.text : "";
            if (col.renderer) {
              col.renderer.append(chunk);
            } else {
              col.content.append(chunk);
            }
          } else if (event === "status") {
            if (typeof data.text === "string" && data.text) {
              setNote(col, data.text, data.level === "warning" ? "warning" : null);
            }
          } else if (event === "sources") {
            // Quellenliste mit Zitieren wie im Einzelchat (citations.js).
            window.MultiGPT.sources?.render(col.article, data.sources);
            const count = Array.isArray(data.sources) ? data.sources.length : 0;
            if (count && col.note.hidden) {
              setNote(col, count === 1 ? "1 Quelle" : `${count} Quellen`, null);
            }
          } else if (event === "usage") {
            col.usage.textContent = formatUsage(data);
          } else if (event === "error") {
            errorText = typeof data.message === "string" ? data.message : "";
          } else if (event === "done") {
            status = typeof data.status === "string" ? data.status : "complete";
          }
        }
        if (!status) {
          status = "error";
          errorText = errorText || "Die Verbindung wurde unerwartet beendet.";
        }
      } catch (err) {
        if (signal.aborted) {
          status = started ? "aborted" : "failed";
          errorText = started ? "" : "Abgebrochen, bevor die Antwort begann.";
        } else {
          status = started ? "error" : "failed";
          errorText = started
            ? "Die Verbindung wurde unterbrochen."
            : (err && err.message) || "Der Server ist nicht erreichbar.";
        }
      }
      if (status === "aborted" && !started) {
        status = "failed";
      }
      finishColumn(col, status, errorText);
      if (!started) {
        onStart(null);
      }
    }

    function startColumn(col, payload) {
      let resolveStart;
      const startPromise = new Promise((resolve) => {
        resolveStart = resolve;
      });
      let reported = false;
      const onStart = (id) => {
        if (!reported) {
          reported = true;
          resolveStart(id);
        }
      };
      const done = runColumn(col, payload, onStart).finally(() => onStart(null));
      return { startPromise, done };
    }

    function resetPanel() {
      columns = [];
      columnsEl.replaceChildren();
      panel.hidden = true;
      hint.textContent = "";
    }

    // --- Senden ---

    async function send(text) {
      if (pending || streaming) {
        return;
      }
      const models = selectedModels();
      if (models.length < MIN_MODELS || models.length > MAX_MODELS) {
        view.setStatus("Bitte zwei oder drei Modelle zum Vergleichen wählen.", true);
        picker.querySelector("input:not(:disabled)")?.focus();
        return;
      }
      const master = view.begin();
      if (!master) {
        return;
      }
      streaming = true;
      setControlsDisabled(true);
      textarea.value = "";
      view.autosize();
      userEl = view.appendMessage("user", "Du", text);
      window.MultiGPT.attachments?.commit(userEl); // Anhänge gelten für alle Spalten
      resetPanel();
      columns = models.map(createColumn);
      columnsEl.append(...columns.map((c) => c.article));
      columnsEl.dataset.count = String(columns.length);
      hint.textContent = "Die Antworten entstehen gleichzeitig. Einzelne Spalten lassen sich abbrechen.";
      panel.hidden = false;
      view.scrollToBottom();
      view.setStatus(`Vergleich läuft: ${models.map((m) => m.name).join(", ")} …`);
      master.signal.addEventListener("abort", () => {
        for (const col of columns) {
          col.controller.abort();
        }
      });

      let anchorId = null;
      const runs = [];
      try {
        await view.ensureConversation(master.signal);
        await view.syncSystemPrompt();
        view.markSidebar(view.currentConversation(), text);
        // Erste Spalte legt die Frage an; scheitert sie vor dem Start (z. B.
        // Budget), übernimmt die nächste. Danach laufen die übrigen parallel.
        let index = 0;
        for (; index < columns.length && anchorId === null; index += 1) {
          const col = columns[index];
          if (master.signal.aborted) {
            finishColumn(col, "failed", "Abgebrochen, bevor die Antwort begann.");
            continue;
          }
          const run = startColumn(
            col,
            view.withTools({ content: text, model: col.model.id, compare: true }),
          );
          runs.push(run.done);
          anchorId = await run.startPromise;
        }
        // Jede Spalte startet, sobald die vorige angelegt ist (nur ein kurzer
        // Hin- und Rückweg): So entspricht die Reihenfolge der Versionen
        // „‹ i/n ›“ der Reihenfolge der Spalten. Die Antworten laufen parallel.
        for (; index < columns.length; index += 1) {
          const col = columns[index];
          if (master.signal.aborted) {
            finishColumn(col, "failed", "Abgebrochen, bevor die Antwort begann.");
            continue;
          }
          const run = startColumn(
            col,
            view.withTools({
              regenerate: true,
              model: col.model.id,
              message_id: anchorId,
              compare: true,
            }),
          );
          runs.push(run.done);
          await run.startPromise;
        }
      } catch (err) {
        for (const col of columns) {
          if (col.status === "streaming" && !col.messageId) {
            finishColumn(col, "failed", (err && err.message) || "Der Server ist nicht erreichbar.");
          }
        }
      }
      await Promise.allSettled(runs);
      streaming = false;
      view.end();
      setControlsDisabled(false);

      const choosable = columns.filter((c) => c.messageId);
      if (!choosable.length) {
        // Nichts gespeichert: Frage zurück ins Eingabefeld.
        const reason = columns.map((c) => c.note.textContent).find(Boolean) || "";
        userEl.remove();
        userEl = null;
        resetPanel();
        window.MultiGPT.attachments?.restore();
        if (!textarea.value) {
          textarea.value = text;
          view.autosize();
        }
        view.setStatus(`Vergleich nicht möglich. ${reason}`.trim(), true);
        if (!view.messageElements().length) {
          document.getElementById("chat-empty")?.removeAttribute("hidden");
        }
        textarea.focus();
        return;
      }
      pending = true;
      for (const col of choosable) {
        col.chooseButton.hidden = false;
      }
      hint.textContent = "Wähle die Antwort, mit der der Chat weitergehen soll.";
      view.setStatus("Vergleich fertig. Bitte eine Antwort wählen.");
      view.updateMessageActions();
      choosable[0].chooseButton.focus({ preventScroll: true });
    }

    // --- Wahl ---

    async function choose(col) {
      const url = view.branchUrl();
      if (!pending || !col.messageId || !url) {
        return;
      }
      for (const c of columns) {
        c.chooseButton.disabled = true;
      }
      try {
        const response = await view.postJson(url, { message_id: col.messageId, adopt_model: true });
        if (!response.ok) {
          throw new Error(await errorMessage(response));
        }
      } catch (err) {
        for (const c of columns) {
          c.chooseButton.disabled = false;
        }
        view.setStatus(`Antwort konnte nicht übernommen werden. ${err.message || ""}`.trim(), true);
        return;
      }
      pending = false;
      const from = userEl;
      userEl = null;
      resetPanel();
      setActive(false);
      // Weiter mit dem Modell der gewählten Spalte.
      if (modelSelect.querySelector(`option[value="${col.model.id}"]:not(:disabled)`)) {
        modelSelect.value = String(col.model.id);
        modelSelect.dispatchEvent(new Event("change"));
        view.storeModel(col.model.id);
      }
      await view.refreshMessages(from);
      view.updateMessageActions();
      view.setStatus(`Weiter mit der Antwort von ${col.model.name}.`);
      textarea.focus();
    }

    window.MultiGPT.compare.send = send;
    toggleLabel.hidden = true; // bis die Modelle geladen sind
  });
})();
