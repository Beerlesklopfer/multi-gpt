// MultiGPT – Sammlungen (M7, RAG): anlegen, umbenennen, löschen, teilen,
// Dokumente hochladen (mehrere, mit Fortschritt, Drag & Drop), Status mit
// automatischer Aktualisierung, Dokumente löschen.
// Kein Inline-JS. Namen, Titel und Fehlertexte sind Nutzerdaten und werden nur
// als Text eingesetzt (textContent), nie als HTML. Rechte prüft der Server.
"use strict";

(() => {
  const POLL_MS = 3000;
  const STATUS_LABELS = { pending: "wartet", indexed: "indexiert", error: "Fehler" };

  const { csrfToken, errorMessage, requestJson, fillTemplate } = window.MultiGPT;

  function announce(text, isError = false) {
    const line = document.getElementById("collection-status");
    if (line) {
      line.textContent = text;
      line.classList.toggle("is-error", Boolean(text) && isError);
    } else {
      window.MultiGPT.announce(text, isError);
    }
  }

  function dialog(options) {
    if (window.MultiGPT.openDialog) {
      return window.MultiGPT.openDialog(options);
    }
    // Rückfall ohne sidebar.js: native Dialoge.
    if (options.input) {
      const value = window.prompt(options.heading, options.input.value || "");
      return Promise.resolve(value === null ? null : value.trim() || null);
    }
    return Promise.resolve(window.confirm(`${options.heading}\n\n${options.text || ""}`));
  }

  function showError(el, text) {
    if (el) {
      el.textContent = text;
      el.hidden = !text;
    }
  }

  // Wie Djangos filesizeformat mit deutscher Darstellung.
  function formatSize(bytes) {
    if (typeof bytes !== "number" || !Number.isFinite(bytes)) {
      return "";
    }
    const units = ["Bytes", "KB", "MB", "GB"];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    if (unit === 0) {
      return `${value} ${value === 1 ? "Byte" : "Bytes"}`;
    }
    return `${value.toFixed(1).replace(".", ",")} ${units[unit]}`;
  }

  function formatDate(iso) {
    const date = iso ? new Date(iso) : null;
    if (!date || Number.isNaN(date.getTime())) {
      return "";
    }
    const pad = (n) => String(n).padStart(2, "0");
    return (
      `${pad(date.getDate())}.${pad(date.getMonth() + 1)}.${date.getFullYear()} ` +
      `${pad(date.getHours())}:${pad(date.getMinutes())}`
    );
  }

  // --- Liste: neue Sammlung --------------------------------------------------------

  function initList(page) {
    const form = document.getElementById("collection-create-form");
    if (!form) {
      return;
    }
    form.hidden = false;
    const input = document.getElementById("collection-name");
    const error = document.getElementById("collection-create-error");
    const button = form.querySelector("button[type=submit]");
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const name = input.value.trim();
      if (!name) {
        input.focus();
        return;
      }
      button.disabled = true;
      showError(error, "");
      try {
        const data = await requestJson("POST", page.dataset.apiCollections, { name });
        window.location.assign(data.url);
      } catch (err) {
        showError(error, err.message);
        button.disabled = false;
        input.focus();
      }
    });
  }

  // --- Sammlung: Kopf (umbenennen, löschen) --------------------------------------------

  function initHeader(page) {
    const renameButton = document.getElementById("collection-rename");
    const deleteButton = document.getElementById("collection-delete");
    const title = document.getElementById("collection-title");

    if (renameButton) {
      renameButton.hidden = false;
      renameButton.addEventListener("click", async () => {
        const current = page.dataset.collectionName || "";
        const name = await dialog({
          heading: "Sammlung umbenennen",
          input: { label: "Neuer Name", value: current },
          confirmLabel: "Speichern",
        });
        if (name === null || name === current) {
          return;
        }
        try {
          const data = await requestJson("PATCH", page.dataset.apiDetail, { name });
          page.dataset.collectionName = data.name;
          title.textContent = data.name;
          document.title = `${data.name} – Sammlungen – MultiGPT`;
          announce("Sammlung umbenannt.");
        } catch (err) {
          announce(`Umbenennen fehlgeschlagen: ${err.message}`, true);
        }
      });
    }

    if (deleteButton) {
      deleteButton.hidden = false;
      deleteButton.addEventListener("click", async () => {
        const ok = await dialog({
          heading: "Sammlung löschen?",
          text:
            `„${page.dataset.collectionName || ""}“ wird mit allen Dokumenten endgültig gelöscht. ` +
            "Chats, die sie nutzen, finden diese Dokumente danach nicht mehr. " +
            "Das lässt sich nicht rückgängig machen.",
          confirmLabel: "Endgültig löschen",
          danger: true,
        });
        if (!ok) {
          return;
        }
        try {
          await requestJson("DELETE", page.dataset.apiDetail);
          window.location.assign(page.dataset.listUrl);
        } catch (err) {
          announce(`Löschen fehlgeschlagen: ${err.message}`, true);
        }
      });
    }
  }

  // --- Sammlung: Teilen -----------------------------------------------------------------

  function initShares(page) {
    const section = document.getElementById("collection-shares");
    if (!section) {
      return;
    }
    const list = document.getElementById("share-list");
    const form = document.getElementById("share-form");
    const error = document.getElementById("share-error");

    function renderShares(shares) {
      list.replaceChildren(
        ...shares.map((share) => {
          const li = document.createElement("li");
          li.className = "share-item";
          li.dataset.groupId = String(share.group);
          const name = document.createElement("span");
          name.className = "share-group";
          name.textContent = String(share.group_name ?? "");
          const mode = document.createElement("span");
          mode.className = "share-mode";
          mode.textContent = share.can_write ? "lesen und schreiben" : "nur lesen";
          const remove = document.createElement("button");
          remove.type = "button";
          remove.className = "button button-small button-danger";
          remove.dataset.shareRemove = String(share.group);
          remove.textContent = "Entziehen";
          li.append(name, " ", mode, " ", remove);
          return li;
        }),
      );
      list.hidden = !shares.length;
      const summary = section.querySelector("summary");
      summary.replaceChildren("Teilen");
      if (shares.length) {
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.textContent = `${shares.length} ${shares.length === 1 ? "Gruppe" : "Gruppen"}`;
        summary.append(" ", badge);
      }
    }

    for (const button of list.querySelectorAll("[data-share-remove]")) {
      button.hidden = false;
    }

    list.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-share-remove]");
      if (!button) {
        return;
      }
      const groupName = button.closest("li")?.querySelector(".share-group")?.textContent || "";
      const ok = await dialog({
        heading: "Freigabe entziehen?",
        text: `Die Gruppe „${groupName}“ hat danach keinen Zugriff mehr auf diese Sammlung.`,
        confirmLabel: "Entziehen",
        danger: true,
      });
      if (!ok) {
        return;
      }
      showError(error, "");
      try {
        const data = await requestJson("DELETE", page.dataset.apiShares, {
          group: Number(button.dataset.shareRemove),
        });
        renderShares(data.shares || []);
        announce(`Freigabe für „${groupName}“ entzogen.`);
      } catch (err) {
        showError(error, err.message);
      }
    });

    if (form) {
      form.hidden = false;
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const group = document.getElementById("share-group");
        const mode = document.getElementById("share-mode");
        showError(error, "");
        try {
          const data = await requestJson("POST", page.dataset.apiShares, {
            group: Number(group.value),
            can_write: mode.value === "write",
          });
          renderShares(data.shares || []);
          announce(`Mit „${group.selectedOptions[0]?.textContent || ""}“ geteilt.`);
        } catch (err) {
          showError(error, err.message);
        }
      });
    }
  }

  // --- Sammlung: Dokumente (Tabelle, Status, Löschen) ------------------------------------

  function initDocuments(page) {
    const body = document.getElementById("documents-body");
    const table = document.getElementById("documents-table");
    const empty = document.getElementById("documents-empty");
    const canWrite = body?.dataset.canWrite === "true";
    let pollTimer = null;
    let polling = false;

    function updateEmpty() {
      const has = Boolean(body.querySelector("tr.document-row"));
      table.hidden = !has;
      empty.hidden = has;
    }

    function fillStatus(cell, doc) {
      const badge = document.createElement("span");
      badge.className = `status-badge status-${doc.status}`;
      badge.textContent = doc.status_label || STATUS_LABELS[doc.status] || String(doc.status);
      cell.replaceChildren(badge);
      // Fehlerursache bzw. bei "wartet" der Hinweis auf einen neuen Versuch.
      if ((doc.status === "error" || doc.status === "pending") && doc.error_text) {
        const p = document.createElement("p");
        p.className = doc.status === "error" ? "document-error" : "document-note";
        p.textContent = String(doc.error_text);
        cell.append(p);
      }
    }

    function buildRow(doc) {
      const tr = document.createElement("tr");
      tr.className = "document-row";
      tr.dataset.documentId = String(doc.id);
      tr.dataset.status = String(doc.status);
      const title = document.createElement("td");
      title.className = "document-title";
      title.textContent = String(doc.title ?? "");
      const status = document.createElement("td");
      status.className = "document-status";
      fillStatus(status, doc);
      const size = document.createElement("td");
      size.className = "col-size";
      size.textContent = formatSize(doc.size);
      const date = document.createElement("td");
      date.className = "col-date";
      const time = document.createElement("time");
      if (doc.created) {
        time.dateTime = String(doc.created);
      }
      time.textContent = formatDate(doc.created);
      date.append(time);
      const actions = document.createElement("td");
      actions.className = "document-actions";
      if (doc.download_url) {
        const link = document.createElement("a");
        link.className = "button button-small";
        link.href = String(doc.download_url);
        link.setAttribute("download", "");
        link.textContent = "Herunterladen";
        actions.append(link);
      }
      if (canWrite && !doc.from_source) {
        const del = document.createElement("button");
        del.type = "button";
        del.className = "button button-small button-danger";
        del.dataset.documentDelete = String(doc.id);
        del.textContent = "Löschen";
        actions.append(" ", del);
      }
      tr.append(title, status, size, date, actions);
      return tr;
    }

    function upsert(doc) {
      const existing = body.querySelector(`tr[data-document-id="${CSS.escape(String(doc.id))}"]`);
      if (existing) {
        existing.dataset.status = String(doc.status);
        fillStatus(existing.querySelector(".document-status"), doc);
        return existing;
      }
      const row = buildRow(doc);
      // Nach Titel einsortieren wie auf dem Server.
      const title = String(doc.title ?? "").toLocaleLowerCase("de");
      const next = Array.from(body.querySelectorAll("tr.document-row")).find(
        (tr) =>
          (tr.querySelector(".document-title")?.textContent || "").toLocaleLowerCase("de") > title,
      );
      body.insertBefore(row, next || null);
      updateEmpty();
      return row;
    }

    function hasPending() {
      return Boolean(body.querySelector('tr.document-row[data-status="pending"]'));
    }

    async function refresh() {
      const response = await fetch(page.dataset.apiDocuments, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) {
        throw new Error(await errorMessage(response));
      }
      const data = await response.json();
      const docs = Array.isArray(data) ? data : data.documents || [];
      const ids = new Set(docs.map((d) => String(d.id)));
      for (const tr of body.querySelectorAll("tr.document-row")) {
        if (!ids.has(tr.dataset.documentId)) {
          tr.remove(); // anderswo gelöscht
        }
      }
      for (const doc of docs) {
        upsert(doc);
      }
      updateEmpty();
    }

    // Polling nur, solange etwas wartet und der Tab sichtbar ist.
    function schedule() {
      window.clearTimeout(pollTimer);
      pollTimer = null;
      if (!hasPending() || document.visibilityState !== "visible") {
        return;
      }
      pollTimer = window.setTimeout(poll, POLL_MS);
    }

    async function poll() {
      if (polling) {
        return;
      }
      polling = true;
      try {
        const before = new Set(
          Array.from(body.querySelectorAll('tr[data-status="pending"]'), (tr) => tr.dataset.documentId),
        );
        await refresh();
        for (const id of before) {
          const tr = body.querySelector(`tr[data-document-id="${CSS.escape(id)}"]`);
          if (tr && tr.dataset.status === "indexed") {
            announce(`„${tr.querySelector(".document-title")?.textContent || ""}“ ist indexiert.`);
          } else if (tr && tr.dataset.status === "error") {
            announce(
              `„${tr.querySelector(".document-title")?.textContent || ""}“ konnte nicht verarbeitet werden.`,
              true,
            );
          }
        }
      } catch {
        // Netzwerkfehler: beim nächsten Takt erneut versuchen.
      } finally {
        polling = false;
        schedule();
      }
    }

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && hasPending()) {
        poll(); // sofort aktualisieren, dann weiter im Takt
      } else {
        schedule();
      }
    });

    for (const button of body.querySelectorAll("[data-document-delete]")) {
      button.hidden = false;
    }

    body.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-document-delete]");
      if (!button) {
        return;
      }
      const row = button.closest("tr");
      const title = row?.querySelector(".document-title")?.textContent || "";
      const ok = await dialog({
        heading: "Dokument löschen?",
        text: `„${title}“ wird aus der Sammlung entfernt und ist im Chat nicht mehr auffindbar.`,
        confirmLabel: "Löschen",
        danger: true,
      });
      if (!ok) {
        return;
      }
      try {
        await requestJson(
          "DELETE",
          fillTemplate(page.dataset.apiDocumentTemplate, button.dataset.documentDelete),
        );
        row?.remove();
        updateEmpty();
        announce(`„${title}“ gelöscht.`);
        document.getElementById("documents-title")?.focus?.();
      } catch (err) {
        announce(`Löschen fehlgeschlagen: ${err.message}`, true);
      }
    });

    updateEmpty();
    schedule();
    return { upsert, schedule };
  }

  // --- Sammlung: Hochladen ------------------------------------------------------------------

  function uploadFile(url, file, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", url);
      xhr.setRequestHeader("X-CSRFToken", csrfToken());
      xhr.setRequestHeader("Accept", "application/json");
      xhr.responseType = "json";
      xhr.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable) {
          onProgress(event.loaded / event.total);
        }
      });
      xhr.addEventListener("load", () => {
        const data = xhr.response;
        if (xhr.status >= 200 && xhr.status < 300 && data && data.id != null) {
          resolve(data);
          return;
        }
        if (data && typeof data.error === "string" && data.error) {
          reject(new Error(data.error));
        } else if (xhr.status === 413) {
          reject(new Error("Die Datei ist zu groß."));
        } else if (xhr.status === 403) {
          reject(new Error("Dafür fehlt dir die Berechtigung."));
        } else {
          reject(new Error(`Das Hochladen ist fehlgeschlagen (HTTP ${xhr.status}).`));
        }
      });
      xhr.addEventListener("error", () => reject(new Error("Der Server ist nicht erreichbar.")));
      xhr.addEventListener("abort", () => reject(new Error("Abgebrochen.")));
      const data = new FormData();
      data.append("file", file);
      xhr.send(data);
    });
  }

  function initUpload(page, documents) {
    const form = document.getElementById("upload-form");
    if (!form || !documents) {
      return;
    }
    form.hidden = false;
    const input = document.getElementById("upload-input");
    const drop = document.getElementById("upload-drop");
    const list = document.getElementById("upload-list");
    const maxBytes = Number(form.dataset.maxBytes) || 0;
    const queue = [];
    let busy = false;

    function addItem(file) {
      const li = document.createElement("li");
      li.className = "upload-item";
      const name = document.createElement("span");
      name.className = "upload-name";
      name.textContent = file.name;
      const progress = document.createElement("progress");
      progress.max = 1;
      progress.value = 0;
      progress.setAttribute("aria-label", `Fortschritt ${file.name}`);
      const state = document.createElement("span");
      state.className = "upload-state";
      state.textContent = "wartet …";
      li.append(name, progress, state);
      list.append(li);
      return { li, progress, state };
    }

    async function run() {
      if (busy) {
        return;
      }
      busy = true;
      while (queue.length) {
        const { file, item } = queue.shift();
        if (maxBytes && file.size > maxBytes) {
          item.progress.remove();
          item.li.classList.add("is-error");
          item.state.textContent = `Die Datei ist zu groß (höchstens ${formatSize(maxBytes)}).`;
          continue;
        }
        item.state.textContent = "wird hochgeladen …";
        try {
          const doc = await uploadFile(page.dataset.apiDocuments, file, (share) => {
            item.progress.value = share;
          });
          item.progress.value = 1;
          item.li.classList.add("is-done");
          item.state.textContent = "hochgeladen, wird verarbeitet";
          documents.upsert(doc);
          documents.schedule();
          window.setTimeout(() => item.li.remove(), 6000);
        } catch (err) {
          item.progress.remove();
          item.li.classList.add("is-error");
          item.state.textContent = err.message;
        }
      }
      busy = false;
      announce("Hochladen abgeschlossen.");
    }

    function enqueue(files) {
      for (const file of files) {
        queue.push({ file, item: addItem(file) });
      }
      run();
    }

    input.addEventListener("change", () => {
      enqueue(Array.from(input.files || []));
      input.value = "";
    });

    for (const type of ["dragenter", "dragover"]) {
      drop.addEventListener(type, (event) => {
        if (Array.from(event.dataTransfer?.types || []).includes("Files")) {
          event.preventDefault();
          drop.classList.add("is-dragover");
        }
      });
    }
    for (const type of ["dragleave", "dragend"]) {
      drop.addEventListener(type, () => drop.classList.remove("is-dragover"));
    }
    drop.addEventListener("drop", (event) => {
      event.preventDefault();
      drop.classList.remove("is-dragover");
      enqueue(Array.from(event.dataTransfer?.files || []));
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    const listPage = document.getElementById("collections-page");
    if (listPage) {
      initList(listPage);
    }
    const page = document.getElementById("collection-page");
    if (page) {
      initHeader(page);
      initShares(page);
      initUpload(page, initDocuments(page));
    }
  });
})();
