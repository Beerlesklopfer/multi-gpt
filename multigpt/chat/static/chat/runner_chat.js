// MultiGPT – Runner im Chat (M14, Entscheidungen 8j, 8p, 8t).
// - Auswahl von Arbeitsstand und Rückfrage-Modus je Chat (GET/POST
//   /runner/api/chat/<id>/; ohne Chat wird er beim ersten Wählen angelegt).
// - Der MCP-Schalter „Runner“ folgt der Auswahl: mit Arbeitsstand an, ohne aus
//   (die Werkzeuge gibt es nur mit Arbeitsstand).
// - Aufgabenliste (todo) aus dem Stand und aus dem Chat-Stream (runner_todo).
// - Sitzung: Terminal (runner_terminal.js, xterm.js) und Vorschau im
//   abgeschotteten Rahmen (sandbox="allow-scripts", strenge CSP vom Server).
// Alles aus dem Container ist nicht vertrauenswürdig: nur textContent.
"use strict";

(() => {
  const SESSION_POLL = 15000; // ms, solange eine Sitzung aktiv ist
  const NULL_UUID = "00000000-0000-0000-0000-000000000000";
  const TODO_LABELS = {
    pending: "offen",
    in_progress: "in Arbeit",
    completed: "erledigt",
  };

  function init() {
    const picker = document.getElementById("runner-picker");
    const bench = document.getElementById("runner-workbench");
    const chat = document.getElementById("chat");
    if (!picker || !chat) {
      return;
    }
    const { requestJson, errorMessage, fillTemplate } = window.MultiGPT;
    const workspaceSelect = document.getElementById("runner-workspace");
    const modeSelect = document.getElementById("runner-mode");
    const scratchBox = document.getElementById("runner-scratch-box");
    const scratchSelect = document.getElementById("runner-scratch");
    const scratchHint = document.getElementById("runner-scratch-hint");
    const packagesBox = document.getElementById("runner-packages-box");
    const packagesInput = document.getElementById("runner-packages");
    const pickerState = document.getElementById("runner-picker-state");
    const todoBox = document.getElementById("runner-todo");
    const todoList = document.getElementById("runner-todo-list");
    const actions = document.getElementById("runner-session-actions");
    const sessionState = document.getElementById("runner-session-state");
    const auditLink = document.getElementById("runner-session-audit");
    const terminalToggle = document.getElementById("runner-terminal-toggle");
    const terminalRegion = document.getElementById("runner-terminal");
    const previewToggle = document.getElementById("runner-preview-toggle");
    const previewRegion = document.getElementById("runner-preview");
    const previewFrame = document.getElementById("runner-preview-frame");
    const previewLink = document.getElementById("runner-preview-link");
    const serverId = picker.dataset.runnerServer || "";

    let state = null;
    let session = null; // {uuid, status, running, preview_url, pty_url, …}
    let sessionTimer = null;
    let terminal = null;

    const conversationId = () => chat.dataset.conversationId || "";
    const apiUrl = (id) => fillTemplate(picker.dataset.apiTemplate, id);
    const sessionUrl = (uuid) => picker.dataset.sessionApiTemplate.replace(NULL_UUID, uuid);

    function say(text, isError = false) {
      pickerState.textContent = text;
      pickerState.classList.toggle("is-error", isError);
    }

    // --- MCP-Schalter des Runner-Servers ---

    function syncServerSwitch() {
      if (!serverId) {
        return;
      }
      const input = document.querySelector(`#tool-servers-list input[value="${serverId}"]`);
      if (!input) {
        return;
      }
      const label = input.closest("label");
      input.checked = Boolean(state && state.workspace_id);
      input.disabled = true; // gesteuert über den Bereich „Runner“
      label?.setAttribute("title", "Wird über den Bereich „Runner“ gesteuert (Arbeitsstand).");
    }

    const serverList = document.getElementById("tool-servers-list");
    if (serverList) {
      new MutationObserver(syncServerSwitch).observe(serverList, { childList: true });
    }

    // --- Auswahl ---

    function renderModes() {
      const allowed = new Set(state.modes_allowed || []);
      const options = [new Option(`Vorgabe (${labelOf(state.effective_mode)})`, "")];
      for (const mode of state.modes || []) {
        const option = new Option(mode.label, mode.value);
        option.disabled = !allowed.has(mode.value);
        options.push(option);
      }
      modeSelect.replaceChildren(...options);
      modeSelect.value = state.mode || "";
      modeSelect.disabled = !state.workspace_id;
      renderScratch();
      renderPackages();
    }

    // Paketquellen je Chat (nur mit Rollenrecht; gilt ab der nächsten Sitzung).
    function renderPackages() {
      if (!packagesBox) {
        return;
      }
      packagesBox.hidden = !(state.workspace_id && state.packages);
      packagesInput.checked = state.packages_selected !== false;
    }

    // Scratchpad je Arbeitsstand (container | local); nur wenn der Server es anbietet.
    function renderScratch() {
      if (!scratchBox) {
        return;
      }
      const modes = Array.isArray(state.scratch_modes) ? state.scratch_modes : [];
      const show = Boolean(state.workspace_id && modes.length);
      scratchBox.hidden = !show;
      scratchHint.hidden = !show;
      if (!show) {
        return;
      }
      scratchSelect.replaceChildren(...modes.map((m) => new Option(m.label, m.value)));
      scratchSelect.value = state.workspace_scratch_mode || "container";
    }

    function labelOf(value) {
      const found = (state.modes || []).find((m) => m.value === value);
      return found ? found.label : value || "–";
    }

    function apply(data) {
      state = data;
      picker.hidden = !data.available;
      // Neu angelegte Arbeitsstände ergänzen (Liste kommt vom Server).
      if (Array.isArray(data.workspaces)) {
        const known = new Set(Array.from(workspaceSelect.options, (o) => o.value));
        for (const ws of data.workspaces) {
          if (!known.has(String(ws.id))) {
            workspaceSelect.append(new Option(ws.name, String(ws.id)));
          }
        }
      }
      workspaceSelect.value = data.workspace_id ? String(data.workspace_id) : "";
      renderModes();
      renderTodo(data.todo || []);
      syncServerSwitch();
      setSession(data.session);
      say(
        data.workspace_id
          ? `Arbeitsstand „${data.workspace_name}“, ${
              data.network
                ? "mit Netz"
                : data.packages_runner
                  ? "ohne Netz, Paketquellen erlaubt"
                  : "ohne Netz"
            }.`
          : "",
      );
    }

    async function load() {
      const id = conversationId();
      if (!id) {
        picker.hidden = false;
        modeSelect.disabled = true;
        return;
      }
      try {
        const response = await fetch(apiUrl(id), {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        if (!response.ok) {
          throw new Error(await errorMessage(response));
        }
        apply(await response.json());
      } catch (err) {
        picker.hidden = false;
        say(`Runner-Stand nicht abrufbar. ${err.message || ""}`.trim(), true);
      }
    }

    async function save(change) {
      const view = window.MultiGPT.chatView;
      workspaceSelect.disabled = true;
      modeSelect.disabled = true;
      try {
        if (!conversationId() && view) {
          await view.ensureConversation();
        }
        const id = conversationId();
        if (!id) {
          throw new Error("Bitte zuerst eine Nachricht senden.");
        }
        const response = await requestJson("POST", apiUrl(id), change);
        if (!response.ok) {
          throw new Error(await errorMessage(response));
        }
        apply(await response.json());
      } catch (err) {
        say(err.message || "Speichern fehlgeschlagen.", true);
        if (state) {
          apply(state);
        }
      } finally {
        workspaceSelect.disabled = false;
        modeSelect.disabled = !(state && state.workspace_id);
      }
    }

    workspaceSelect.addEventListener("change", () => {
      save({ workspace: workspaceSelect.value ? Number(workspaceSelect.value) : null });
    });
    modeSelect.addEventListener("change", () => {
      save({ mode: modeSelect.value });
    });
    scratchSelect?.addEventListener("change", () => {
      save({ scratch_mode: scratchSelect.value });
    });
    packagesInput?.addEventListener("change", () => {
      save({ packages: packagesInput.checked });
    });

    // --- Aufgabenliste ---

    function renderTodo(items) {
      if (!todoBox) {
        return;
      }
      const list = Array.isArray(items) ? items : [];
      todoList.replaceChildren(
        ...list.map((item) => {
          const li = document.createElement("li");
          const status = String(item.status || "pending");
          li.className = `runner-todo-item runner-todo-${status.replace(/[^a-z_]/g, "")}`;
          const text = document.createElement("span");
          text.textContent = String(item.content ?? "");
          const badge = document.createElement("span");
          badge.className = "runner-todo-status";
          badge.textContent = TODO_LABELS[status] || status;
          li.append(text, " ", badge);
          return li;
        }),
      );
      todoBox.hidden = !list.length;
      updateBench();
    }

    // --- Sitzung: Terminal und Vorschau ---

    function updateBench() {
      if (!bench) {
        return;
      }
      const hasTodo = todoBox && !todoBox.hidden;
      bench.hidden = !hasTodo && !(session && session.uuid);
    }

    async function refreshSession() {
      window.clearTimeout(sessionTimer);
      sessionTimer = null;
      if (!session || !session.uuid) {
        return;
      }
      try {
        const response = await fetch(sessionUrl(session.uuid), {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
          cache: "no-store",
        });
        if (!response.ok) {
          throw new Error(String(response.status));
        }
        session = { ...session, ...(await response.json()) };
      } catch {
        // Nächster Versuch beim nächsten Takt.
      }
      renderSession();
      if (session && session.running && !document.hidden) {
        sessionTimer = window.setTimeout(refreshSession, SESSION_POLL);
      }
    }

    function setSession(data) {
      const uuid = data && data.uuid ? String(data.uuid) : "";
      if (!uuid) {
        session = null;
        renderSession();
        return;
      }
      const changed = !session || session.uuid !== uuid;
      session = { ...(changed ? {} : session), uuid, status: data.status };
      if (changed && terminal) {
        closeTerminal();
      }
      refreshSession();
    }

    function renderSession() {
      if (!actions) {
        return;
      }
      if (!session) {
        actions.hidden = true;
        closeTerminal();
        closePreview();
        updateBench();
        return;
      }
      actions.hidden = false;
      sessionState.textContent =
        `Sitzung: ${session.status_label || session.status}` +
        (session.packages ? " · Paketquellen erlaubt" : "");
      auditLink.href = session.audit_url || "#";
      terminalToggle.disabled = !session.running;
      previewToggle.hidden = !session.preview_url;
      if (session.preview_url) {
        previewLink.href = session.preview_url;
      } else {
        closePreview();
      }
      updateBench();
    }

    function closeTerminal() {
      if (terminal) {
        terminal.close();
        terminal = null;
      }
      if (terminalRegion) {
        terminalRegion.hidden = true;
      }
      terminalToggle?.setAttribute("aria-expanded", "false");
    }

    terminalToggle?.addEventListener("click", async () => {
      if (terminal) {
        closeTerminal();
        terminalToggle.focus();
        return;
      }
      if (!session || !session.running || !session.pty_url) {
        return;
      }
      terminalRegion.hidden = false;
      terminalToggle.setAttribute("aria-expanded", "true");
      const stateEl = terminalRegion.querySelector("[data-runner-terminal-state]");
      const api = window.MultiGPT.runnerTerminal;
      try {
        await api.ensureXterm({
          js: bench.dataset.xtermJs,
          fit: bench.dataset.xtermFit,
          css: bench.dataset.xtermCss,
        });
      } catch (err) {
        stateEl.textContent = err.message;
        return;
      }
      const base = session.pty_url.replace(/\/pty\/$/, "/pty/PTY/");
      terminal = api.open({
        screen: terminalRegion.querySelector("[data-runner-terminal-screen]"),
        stateEl,
        streamUrl: session.pty_url,
        wsUrl: session.pty_ws_url,
        inputTemplate: `${base}eingabe/`,
        resizeTemplate: `${base}groesse/`,
        focusOut: () => terminalRegion.querySelector("[data-runner-terminal-close]")?.focus(),
      });
    });
    terminalRegion
      ?.querySelector("[data-runner-terminal-close]")
      ?.addEventListener("click", () => {
        closeTerminal();
        terminalToggle.focus();
      });

    function closePreview() {
      if (previewRegion) {
        previewRegion.hidden = true;
        previewFrame.removeAttribute("src");
      }
      previewToggle?.setAttribute("aria-expanded", "false");
    }

    function openPreview() {
      if (!session || !session.preview_url) {
        return;
      }
      previewRegion.hidden = false;
      previewToggle.setAttribute("aria-expanded", "true");
      previewFrame.src = session.preview_url;
    }

    previewToggle?.addEventListener("click", () => {
      if (previewRegion.hidden) {
        openPreview();
      } else {
        closePreview();
        previewToggle.focus();
      }
    });
    document.getElementById("runner-preview-reload")?.addEventListener("click", openPreview);
    document.getElementById("runner-preview-close")?.addEventListener("click", () => {
      closePreview();
      previewToggle.focus();
    });

    // --- Ereignisse aus dem Chat-Stream (chat.js) ---

    document.addEventListener("multigpt:stream-event", (event) => {
      const { event: name, data } = event.detail || {};
      if (name === "runner_todo") {
        renderTodo(data && data.items);
      } else if (name === "runner_session") {
        setSession(data);
      } else if (name === "done" && session) {
        refreshSession(); // z. B. Vorschau nach „serve“
      } else if (name === "start" && !state && conversationId()) {
        load(); // erster Beitrag hat den Chat gerade angelegt
      }
    });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && session && session.running && sessionTimer === null) {
        refreshSession();
      }
    });

    load();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
