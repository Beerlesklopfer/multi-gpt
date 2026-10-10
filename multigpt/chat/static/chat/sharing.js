// MultiGPT – Chats teilen (RWUD, chat/sharing.py): Dialog „Teilen“ für den
// Besitzer, Kopie und Austragen beim Empfänger, Hinweis „Neue Nachrichten“
// (Abfrage des Stands beim Fokus und alle 30 s, keine Live-Synchronisation).
// Namen kommen vom Server und werden nur als Text eingesetzt (textContent).
// Rechte prüft der Server bei jeder Anfrage; hier nur Anzeige.
"use strict";

(() => {
  const POLL_MS = 30000;
  const RIGHTS = [
    ["can_write", "Schreiben", "W", "Nachrichten senden (auch mit Anhängen) und Antworten neu erzeugen"],
    ["can_update", "Bearbeiten", "U", "Nachrichten bearbeiten (braucht auch Schreiben), umbenennen, System-Prompt"],
    ["can_delete", "Löschen", "D", "Chat archivieren oder löschen – für alle"],
  ];

  const mg = () => window.MultiGPT;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined) {
      node.textContent = text;
    }
    return node;
  }

  function rightsLabel(flags) {
    return "R" + RIGHTS.filter(([key]) => flags[key]).map(([, , letter]) => letter).join("");
  }

  function rightsText(flags) {
    return ["Lesen", ...RIGHTS.filter(([key]) => flags[key]).map(([, name]) => name)].join(", ");
  }

  // --- Gleichzeitigkeit: angezeigtes Ende und Stand ---------------------------------

  function stateBox() {
    return document.getElementById("share-state");
  }

  // ID der letzten angezeigten Nachricht; nur in geteilten Chats (sonst null).
  function leafId() {
    if (!stateBox()) {
      return null;
    }
    const items = Array.from(document.querySelectorAll("#chat-log .chat-message[data-message-id]"));
    const shown = items.filter((item) => !item.hidden && item.dataset.messageId);
    const last = shown[shown.length - 1];
    return last ? Number(last.dataset.messageId) : null;
  }

  function isStreaming() {
    return document.getElementById("chat")?.dataset.streaming === "true";
  }

  async function fetchState() {
    const box = stateBox();
    if (!box) {
      return null;
    }
    const response = await fetch(box.dataset.apiState, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    if (response.status === 404) {
      return { revoked: true };
    }
    if (!response.ok) {
      return null;
    }
    return response.json();
  }

  function showRevoked() {
    const update = document.getElementById("share-update");
    if (update) {
      update.hidden = false;
      update.classList.add("is-revoked");
      update.querySelector(".share-update-text").textContent =
        "Die Freigabe für diesen Chat wurde beendet.";
      update.querySelector("[data-share-reload]")?.remove();
    }
    const form = document.getElementById("chat-form");
    if (form) {
      form.hidden = true;
    }
  }

  async function checkState() {
    const box = stateBox();
    if (!box || document.hidden || isStreaming()) {
      return;
    }
    let state;
    try {
      state = await fetchState();
    } catch {
      return; // Netzfehler: beim nächsten Mal
    }
    if (!state) {
      return;
    }
    if (state.revoked) {
      showRevoked();
      return;
    }
    if (state.stamp !== box.dataset.stamp && !isStreaming()) {
      const update = document.getElementById("share-update");
      if (update && update.hidden) {
        update.hidden = false;
        box.dataset.pending = state.stamp;
      }
    }
  }

  // Nach eigenen Antworten den Stand still übernehmen (kein Hinweis für Eigenes).
  async function rebase() {
    try {
      const state = await fetchState();
      if (state && !state.revoked && stateBox()) {
        stateBox().dataset.stamp = state.stamp;
      }
    } catch {
      // egal: dann zeigt der nächste Abgleich den Hinweis
    }
  }

  async function reload() {
    const update = document.getElementById("share-update");
    const view = mg().chatView;
    const box = stateBox();
    const ok = view ? await view.refreshMessages(null) : false;
    if (!ok) {
      window.location.reload();
      return;
    }
    if (update) {
      update.hidden = true;
    }
    if (box?.dataset.pending) {
      box.dataset.stamp = box.dataset.pending;
      delete box.dataset.pending;
    }
    await rebase();
    mg().announce("Verlauf neu geladen.");
  }

  function startPolling() {
    if (!stateBox()) {
      return;
    }
    window.setInterval(checkState, POLL_MS);
    window.addEventListener("focus", checkState);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        checkState();
      }
    });
    const chat = document.getElementById("chat");
    if (chat) {
      new MutationObserver(() => {
        if (chat.dataset.streaming === "false") {
          rebase();
        }
      }).observe(chat, { attributes: true, attributeFilter: ["data-streaming"] });
    }
    document.querySelector("[data-share-reload]")?.addEventListener("click", reload);
  }

  // --- Empfänger: Kopie und Austragen --------------------------------------------

  async function copyChat(button) {
    button.disabled = true;
    try {
      const data = await mg().requestJson("POST", button.dataset.apiCopy, {});
      window.location.assign(data.url);
    } catch (err) {
      button.disabled = false;
      mg().announce(`Kopie fehlgeschlagen: ${err.message}`, true);
    }
  }

  async function leaveChat(button) {
    const ok = await mg().openDialog({
      heading: "Aus deiner Liste entfernen?",
      text: `Der Chat bleibt bei ${button.dataset.owner} erhalten. Du siehst ihn erst wieder, wenn er erneut mit dir geteilt wird.`,
      confirmLabel: "Entfernen",
      danger: true,
    });
    if (!ok) {
      return;
    }
    try {
      const data = await mg().requestJson("POST", button.dataset.apiLeave, {});
      window.location.assign(data.url);
    } catch (err) {
      mg().announce(`Entfernen fehlgeschlagen: ${err.message}`, true);
    }
  }

  // --- Besitzer: Dialog „Teilen“ ----------------------------------------------------

  function flagBoxes(prefix, flags, onChange) {
    const group = el("fieldset", "share-flags");
    const legend = el("legend", "sr-only", "Rechte");
    group.append(legend);
    const read = el("label", "share-flag");
    const readBox = el("input");
    readBox.type = "checkbox";
    readBox.checked = true;
    readBox.disabled = true;
    read.append(readBox, document.createTextNode(" Lesen"));
    read.title = "Lesen ist immer enthalten (auch Kopieren und Exportieren).";
    group.append(read);
    const boxes = {};
    for (const [key, name, , hint] of RIGHTS) {
      const label = el("label", "share-flag");
      const box = el("input");
      box.type = "checkbox";
      box.name = `${prefix}-${key}`;
      box.checked = Boolean(flags[key]);
      label.title = hint;
      label.append(box, document.createTextNode(` ${name}`));
      if (onChange) {
        box.addEventListener("change", onChange);
      }
      boxes[key] = box;
      group.append(label);
    }
    return { group, boxes };
  }

  function collect(boxes) {
    const flags = {};
    for (const [key] of RIGHTS) {
      flags[key] = boxes[key].checked;
    }
    return flags;
  }

  function openShareDialog(button) {
    const listUrl = button.dataset.apiShares;
    const detailTemplate = button.dataset.apiShareTemplate;
    const dialog = el("dialog", "dialog share-dialog");
    const heading = el("h2", "", "Chat teilen");
    heading.id = `share-dialog-title-${Date.now()}`;
    dialog.setAttribute("aria-labelledby", heading.id);
    const intro = el(
      "p",
      "hint",
      "Lesen ist immer enthalten – mit Kopieren, Exportieren und „Als eigene Kopie fortsetzen“. " +
        "Antworten, die jemand anderes auslöst, laufen über dessen Konto und Budget.",
    );
    const listTitle = el("h3", "share-dialog-subtitle", "Freigaben");
    const list = el("ul", "share-list chat-share-list");
    const empty = el("p", "hint", "Noch mit niemandem geteilt.");
    const error = el("p", "form-error");
    error.setAttribute("role", "alert");
    error.hidden = true;

    const form = el("form", "share-add-form");
    const addTitle = el("h3", "share-dialog-subtitle", "Neu freigeben");
    const targetLabel = el("label", "share-target-label", "Mit");
    const select = el("select");
    select.required = true;
    select.id = `share-target-${Date.now()}`;
    targetLabel.htmlFor = select.id;
    const { group: newFlags, boxes: newBoxes } = flagBoxes("new", {});
    const note = el(
      "p",
      "hint",
      "Bearbeiten von Nachrichten braucht zusätzlich Schreiben. Löschen und Archivieren wirken für alle.",
    );
    const submit = el("button", "button button-primary", "Freigeben");
    submit.type = "submit";
    form.append(addTitle, targetLabel, select, newFlags, note, submit);

    const actions = el("div", "dialog-actions");
    const close = el("button", "button", "Schließen");
    close.type = "button";
    actions.append(close);
    dialog.append(heading, intro, listTitle, list, empty, error, form, actions);

    function showError(message) {
      error.textContent = message;
      error.hidden = !message;
    }

    function fillSelect(data) {
      const keep = select.value;
      select.replaceChildren();
      const placeholder = el("option", "", "Konto oder Gruppe wählen …");
      placeholder.value = "";
      select.append(placeholder);
      const taken = new Set(data.shares.map((s) => `${s.kind}:${s.target_id}`));
      for (const [kind, label, rows] of [
        ["user", "Konten", data.users],
        ["group", "Gruppen", data.groups],
      ]) {
        const optgroup = el("optgroup");
        optgroup.label = label;
        for (const row of rows) {
          const option = el("option", "", row.name);
          option.value = `${kind}:${row.id}`;
          option.disabled = taken.has(option.value);
          optgroup.append(option);
        }
        if (rows.length) {
          select.append(optgroup);
        }
      }
      select.value = keep && !taken.has(keep) ? keep : "";
    }

    function render(data) {
      list.replaceChildren();
      for (const share of data.shares) {
        const item = el("li", "share-item");
        item.dataset.shareId = share.id;
        const who = el("span", "share-group", share.name);
        const kind = el("span", "share-kind", share.kind === "group" ? "Gruppe" : "Konto");
        const badge = el("span", "share-rights", share.label);
        badge.title = share.text;
        const { group, boxes } = flagBoxes(`share-${share.id}`, share, async () => {
          const flags = collect(boxes);
          badge.textContent = rightsLabel(flags);
          badge.title = rightsText(flags);
          try {
            render(
              await mg().requestJson("PATCH", mg().fillTemplate(detailTemplate, share.id), flags),
            );
            showError("");
            mg().announce(`Rechte für ${share.name}: ${rightsText(flags)}.`);
          } catch (err) {
            showError(`Ändern fehlgeschlagen: ${err.message}`);
          }
        });
        group.setAttribute("aria-label", `Rechte für ${share.name}`);
        const revoke = el("button", "button button-small button-danger", "Widerrufen");
        revoke.type = "button";
        revoke.setAttribute("aria-label", `Freigabe für ${share.name} widerrufen`);
        revoke.addEventListener("click", async () => {
          try {
            render(await mg().requestJson("DELETE", mg().fillTemplate(detailTemplate, share.id)));
            showError("");
            mg().announce(`Freigabe für ${share.name} widerrufen.`);
          } catch (err) {
            showError(`Widerrufen fehlgeschlagen: ${err.message}`);
          }
        });
        const head = el("div", "share-item-head");
        head.append(who, kind, badge);
        item.append(head, group, revoke);
        list.append(item);
      }
      list.hidden = !data.shares.length;
      empty.hidden = Boolean(data.shares.length);
      fillSelect(data);
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const [kind, id] = (select.value || "").split(":");
      if (!kind) {
        select.focus();
        return;
      }
      submit.disabled = true;
      try {
        render(
          await mg().requestJson("POST", listUrl, {
            kind,
            target: Number(id),
            ...collect(newBoxes),
          }),
        );
        for (const box of Object.values(newBoxes)) {
          box.checked = false;
        }
        showError("");
        mg().announce("Chat geteilt.");
      } catch (err) {
        showError(`Teilen fehlgeschlagen: ${err.message}`);
      } finally {
        submit.disabled = false;
      }
    });

    close.addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => {
      dialog.remove();
      if (button.isConnected) {
        button.focus();
      }
    });
    document.body.append(dialog);
    dialog.showModal();
    mg()
      .requestJson("GET", listUrl)
      .then((data) => {
        render(data);
        select.focus();
      })
      .catch((err) => showError(`Freigaben konnten nicht geladen werden: ${err.message}`));
  }

  // --- Start ------------------------------------------------------------------------

  // Chat eines anderen Kontos, mit mir geteilt (Hinweis „Geteilt von …“)?
  function isForeign() {
    return Boolean(document.getElementById("share-banner"));
  }

  window.MultiGPT.sharing = { leafId, isForeign };

  document.addEventListener("DOMContentLoaded", () => {
    const open = document.getElementById("share-open");
    if (open) {
      open.hidden = false;
      open.addEventListener("click", () => openShareDialog(open));
    }
    for (const button of document.querySelectorAll("[data-share-copy], [data-share-leave]")) {
      button.hidden = false;
      button.addEventListener("click", () =>
        button.hasAttribute("data-share-copy") ? copyChat(button) : leaveChat(button),
      );
    }
    startPolling();
  });
})();
