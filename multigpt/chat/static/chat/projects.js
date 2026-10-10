// MultiGPT – Projekte in der Seitenleiste (M5-07, chat/projects.py):
// Projekte ein-/ausklappen (im Browser gemerkt), Projektmenü (⋯), „Neues
// Projekt“, Chats in ein Projekt verschieben bzw. herausnehmen (Einträge im
// Chatmenü von sidebar.js und Knopf „Projekt …“ in der Chat-Kopfzeile),
// Projekt löschen mit der Wahl „Chats behalten“ / „Chats mitlöschen“.
// Rechte prüft der Server. Namen und Titel nur als Text (textContent).
"use strict";

(() => {
  const OPEN_KEY = "multigpt.projects.open";
  const UNTITLED = "Neuer Chat";

  const nav = () => document.getElementById("project-list");
  const mg = () => window.MultiGPT;

  function storageGet() {
    try {
      const ids = JSON.parse(window.localStorage.getItem(OPEN_KEY) || "[]");
      return new Set(Array.isArray(ids) ? ids.map(String) : []);
    } catch {
      return new Set();
    }
  }

  function storageSet(ids) {
    try {
      window.localStorage.setItem(OPEN_KEY, JSON.stringify(Array.from(ids)));
    } catch {
      // Privater Modus o. Ä.: Zustand gilt nur bis zum Neuladen.
    }
  }

  function urls() {
    const data = nav()?.dataset || {};
    return {
      list: data.apiProjects || "/api/projects/",
      detail: (id) => mg().fillTemplate(data.apiProjectTemplate || "/api/projects/0/", id),
      move: (id) =>
        mg().fillTemplate(data.apiMoveTemplate || "/api/conversations/0/project/", id),
      page: (id) => mg().fillTemplate(data.projectUrlTemplate || "/projekte/0/", id),
      projects: data.projectsUrl || "/projekte/",
      newChat: (id) => `${data.newChatUrl || "/"}?projekt=${encodeURIComponent(id)}`,
    };
  }

  // --- Einklappen ------------------------------------------------------------------

  function setOpen(item, open, remember = true) {
    const toggle = item.querySelector(".project-toggle");
    const chats = item.querySelector(".project-chats");
    if (!toggle || !chats) {
      return;
    }
    toggle.setAttribute("aria-expanded", String(open));
    chats.hidden = !open;
    if (remember) {
      const ids = storageGet();
      if (open) {
        ids.add(item.dataset.projectId);
      } else {
        ids.delete(item.dataset.projectId);
      }
      storageSet(ids);
    }
  }

  function initToggles() {
    const remembered = storageGet();
    for (const item of document.querySelectorAll(".project-item")) {
      const toggle = item.querySelector(".project-toggle");
      if (!toggle) {
        continue;
      }
      toggle.hidden = false;
      if (toggle.getAttribute("aria-expanded") !== "true" && remembered.has(item.dataset.projectId)) {
        setOpen(item, true, false);
      }
    }
    for (const button of document.querySelectorAll(".project-menu-button")) {
      button.hidden = false;
    }
  }

  // --- Dialoge -----------------------------------------------------------------------

  // Modaler Dialog mit eigenem Inhalt (body: Knoten). onSubmit liefert den
  // Rückgabewert; null bei Abbruch. Gleicher Aufbau wie sidebar.js.
  function customDialog({ heading, body, confirmLabel, danger, onSubmit, focus }) {
    return new Promise((resolve) => {
      const dialog = document.createElement("dialog");
      dialog.className = "dialog project-dialog";
      const form = document.createElement("form");
      form.method = "dialog";
      const h = document.createElement("h2");
      h.textContent = heading;
      h.id = `project-dialog-title-${Date.now()}`;
      dialog.setAttribute("aria-labelledby", h.id);
      form.append(h, ...body);
      const actions = document.createElement("div");
      actions.className = "dialog-actions";
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "button";
      cancel.textContent = "Abbrechen";
      const ok = document.createElement("button");
      ok.type = "submit";
      ok.className = danger ? "button button-danger-solid" : "button button-primary";
      ok.textContent = confirmLabel;
      actions.append(cancel, ok);
      form.append(actions);
      dialog.append(form);

      const opener = document.activeElement;
      let result = null;
      cancel.addEventListener("click", () => dialog.close());
      form.addEventListener("submit", (event) => {
        const value = onSubmit ? onSubmit(event) : true;
        if (value === undefined) {
          event.preventDefault(); // ungültig, Dialog bleibt offen
          return;
        }
        result = value;
      });
      dialog.addEventListener("close", () => {
        dialog.remove();
        if (opener && opener.isConnected) {
          opener.focus();
        }
        resolve(result);
      });
      document.body.append(dialog);
      dialog.showModal();
      (focus?.(dialog) || (danger ? cancel : ok)).focus();
    });
  }

  function paragraph(text, className = "") {
    const p = document.createElement("p");
    p.textContent = text;
    if (className) {
      p.className = className;
    }
    return p;
  }

  // --- Projekt anlegen, umbenennen, löschen ------------------------------------------------

  async function createProject() {
    const name = await mg().openDialog({
      heading: "Neues Projekt",
      input: { label: "Name des Projekts", value: "" },
      confirmLabel: "Anlegen",
    });
    if (!name) {
      return;
    }
    try {
      const project = await mg().requestJson("POST", urls().list, { name });
      window.location.assign(project.url || urls().page(project.id));
    } catch (err) {
      mg().announce(`Projekt konnte nicht angelegt werden: ${err.message}`, true);
    }
  }

  async function renameProject(item) {
    const current = item.dataset.name || "";
    const name = await mg().openDialog({
      heading: "Projekt umbenennen",
      input: { label: "Neuer Name", value: current },
      confirmLabel: "Speichern",
    });
    if (!name || name === current) {
      return;
    }
    try {
      const data = await mg().requestJson("PATCH", urls().detail(item.dataset.projectId), { name });
      item.dataset.name = data.name;
      item.querySelector(".project-name").textContent = data.name;
      item
        .querySelector(".project-menu-button")
        ?.setAttribute("aria-label", `Aktionen für Projekt „${data.name}“`);
      mg().announce("Projekt umbenannt.");
    } catch (err) {
      mg().announce(`Umbenennen fehlgeschlagen: ${err.message}`, true);
    }
  }

  async function patchProject(item, payload, done) {
    try {
      await mg().requestJson("PATCH", urls().detail(item.dataset.projectId), payload);
    } catch (err) {
      mg().announce(`Ändern fehlgeschlagen: ${err.message}`, true);
      return;
    }
    mg().announce(done);
    window.location.reload();
  }

  async function deleteProject(id, name) {
    let count = 0;
    try {
      count = (await mg().requestJson("GET", urls().detail(id))).chat_count || 0;
    } catch (err) {
      mg().announce(`Projekt nicht gefunden: ${err.message}`, true);
      return;
    }
    const chats = count === 1 ? "1 Chat" : `${count} Chats`;
    const body = [paragraph(`„${name}“ wird gelöscht. Was soll mit ${chats} geschehen?`)];
    const fieldset = document.createElement("fieldset");
    fieldset.className = "project-delete-choice";
    const legend = document.createElement("legend");
    legend.className = "sr-only";
    legend.textContent = "Chats des Projekts";
    fieldset.append(legend);
    const options = [
      ["keep", "Chats behalten (sie sind danach ohne Projekt)"],
      ["delete", `${chats} endgültig mitlöschen, mit allen Nachrichten und Anhängen – auch für Personen, mit denen sie geteilt sind`],
    ];
    for (const [value, text] of options) {
      const label = document.createElement("label");
      const input = document.createElement("input");
      input.type = "radio";
      input.name = "chats";
      input.value = value;
      input.checked = value === "keep";
      label.append(input, document.createTextNode(` ${text}`));
      fieldset.append(label);
    }
    body.push(fieldset, paragraph("Das lässt sich nicht rückgängig machen.", "hint"));
    let okButton = null;
    const mode = await customDialog({
      heading: "Projekt löschen?",
      body,
      confirmLabel: "Projekt löschen",
      danger: true,
      focus: (dialog) => {
        okButton = dialog.querySelector("button[type=submit]");
        fieldset.addEventListener("change", () => {
          const all = fieldset.querySelector("input:checked")?.value === "delete";
          okButton.textContent = all ? `Projekt und ${chats} löschen` : "Projekt löschen";
        });
        return fieldset.querySelector("input:checked");
      },
      onSubmit: () => fieldset.querySelector("input:checked")?.value || undefined,
    });
    if (!mode) {
      return;
    }
    try {
      await mg().requestJson("DELETE", `${urls().detail(id)}?chats=${mode}`);
    } catch (err) {
      mg().announce(`Löschen fehlgeschlagen: ${err.message}`, true);
      return;
    }
    const page = document.getElementById("project-page");
    const activeChatProject = document.getElementById("chat")?.dataset.projectId;
    if (page?.dataset.projectId === String(id)) {
      window.location.assign(urls().projects);
    } else if (activeChatProject === String(id) && mode === "delete") {
      window.location.assign(nav()?.dataset.newChatUrl || "/");
    } else {
      window.location.reload();
    }
  }

  // --- Projektmenü (⋯) -----------------------------------------------------------------------

  let openMenu = null;

  function closeMenu(returnFocus = false) {
    if (!openMenu) {
      return;
    }
    const { menu, button } = openMenu;
    openMenu = null;
    menu.remove();
    button.setAttribute("aria-expanded", "false");
    if (returnFocus) {
      button.focus();
    }
  }

  function showProjectMenu(button) {
    const item = button.closest(".project-item");
    const id = item.dataset.projectId;
    const archived = item.dataset.archived === "true";
    const pinned = item.dataset.pinned === "true";
    const menu = document.createElement("ul");
    menu.className = "chat-item-menu project-menu";
    menu.setAttribute("role", "menu");
    menu.setAttribute("aria-label", button.getAttribute("aria-label"));
    const entries = [
      ["link", "Neuer Chat im Projekt", urls().newChat(id)],
      ["link", "Projekt öffnen", urls().page(id)],
      ["rename", "Umbenennen"],
      [pinned ? "unpin" : "pin", pinned ? "Nicht mehr anheften" : "Anheften"],
      [archived ? "restore" : "archive", archived ? "Wiederherstellen" : "Archivieren"],
      ["delete", "Löschen …"],
    ];
    for (const [action, label, href] of entries) {
      const li = document.createElement("li");
      li.setAttribute("role", "none");
      const el = document.createElement(href ? "a" : "button");
      if (href) {
        el.href = href;
      } else {
        el.type = "button";
        el.dataset.projectAction = action;
      }
      el.setAttribute("role", "menuitem");
      el.tabIndex = -1;
      el.className = action === "delete" ? "is-danger" : "";
      el.textContent = label;
      li.append(el);
      menu.append(li);
    }
    menu.addEventListener("click", (event) => {
      const el = event.target.closest("[role=menuitem]");
      if (!el) {
        return;
      }
      if (!el.dataset.projectAction) {
        window.setTimeout(() => closeMenu(false), 0);
        return;
      }
      closeMenu(false);
      button.focus();
      const action = el.dataset.projectAction;
      if (action === "rename") {
        renameProject(item);
      } else if (action === "pin" || action === "unpin") {
        patchProject(item, { pinned: action === "pin" }, "Projekt geändert.");
      } else if (action === "archive" || action === "restore") {
        patchProject(
          item,
          { archived: action === "archive" },
          action === "archive" ? "Projekt archiviert." : "Projekt wiederhergestellt.",
        );
      } else if (action === "delete") {
        deleteProject(id, item.dataset.name || "");
      }
    });
    menu.addEventListener("keydown", (event) => {
      const items = Array.from(menu.querySelectorAll("[role=menuitem]"));
      const index = items.indexOf(document.activeElement);
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const step = event.key === "ArrowDown" ? 1 : -1;
        items[(index + step + items.length) % items.length].focus();
      } else if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        items[event.key === "Home" ? 0 : items.length - 1].focus();
      } else if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        closeMenu(true);
      } else if (event.key === "Tab") {
        closeMenu(false);
      }
    });
    item.querySelector(".project-row").append(menu);
    button.setAttribute("aria-expanded", "true");
    openMenu = { menu, button };
    menu.querySelector("[role=menuitem]").focus();
  }

  // --- Chat verschieben -------------------------------------------------------------------

  async function loadProjects() {
    const [active, archived] = await Promise.all([
      mg().requestJson("GET", urls().list),
      mg().requestJson("GET", `${urls().list}?archived=1`),
    ]);
    return [...active, ...archived];
  }

  function currentProjectOf(id) {
    const button = document.querySelector(
      `[data-chat-action="move-project"][data-conversation-id="${CSS.escape(String(id))}"]`,
    );
    if (button) {
      return button.dataset.projectId || "";
    }
    const li = document.querySelector(
      `#chat-list-items li.chat-item[data-conversation-id="${CSS.escape(String(id))}"], ` +
        `.project-chats li.chat-item[data-conversation-id="${CSS.escape(String(id))}"]`,
    );
    return li?.dataset.projectId || "";
  }

  async function moveChat(id, title) {
    let list;
    try {
      list = await loadProjects();
    } catch (err) {
      mg().announce(`Projekte konnten nicht geladen werden: ${err.message}`, true);
      return;
    }
    const current = currentProjectOf(id);
    const label = document.createElement("label");
    label.textContent = "Projekt";
    const select = document.createElement("select");
    select.className = "project-move-select";
    const none = document.createElement("option");
    none.value = "";
    none.textContent = "Ohne Projekt";
    select.append(none);
    for (const project of list) {
      const option = document.createElement("option");
      option.value = String(project.id);
      option.textContent = project.archived ? `${project.name} (archiviert)` : project.name;
      select.append(option);
    }
    select.value = current;
    label.append(select);
    const body = [paragraph(`Wohin gehört „${title || UNTITLED}“?`), label];
    if (!list.length) {
      const hint = document.createElement("p");
      hint.className = "hint";
      const link = document.createElement("a");
      link.href = urls().projects;
      link.textContent = "Projekt anlegen";
      hint.append("Du hast noch kein Projekt. ", link);
      body.push(hint);
    }
    const choice = await customDialog({
      heading: "Chat in Projekt verschieben",
      body,
      confirmLabel: "Verschieben",
      focus: () => select,
      onSubmit: () => select.value,
    });
    if (choice === null || choice === current) {
      return;
    }
    await saveMove(id, choice ? Number(choice) : null);
  }

  async function saveMove(id, projectId) {
    let data;
    try {
      data = await mg().requestJson("POST", urls().move(id), { project: projectId });
    } catch (err) {
      mg().announce(`Verschieben fehlgeschlagen: ${err.message}`, true);
      return;
    }
    const chat = document.getElementById("chat");
    if (chat?.dataset.conversationId === String(id) && chat.dataset.streaming !== "true") {
      window.location.reload(); // Kopfzeile, Vorauswahl und Seitenleiste neu
      return;
    }
    placeItem(id, data.project);
    mg().announce(
      data.project ? `Chat in „${data.project_name}“ verschoben.` : "Chat aus dem Projekt genommen.",
    );
  }

  // Eintrag in der Seitenleiste umhängen (ohne Neuladen).
  function placeItem(id, projectId) {
    const key = CSS.escape(String(id));
    const li = document.querySelector(
      `#chat-list-items li.chat-item[data-conversation-id="${key}"], .project-chats li.chat-item[data-conversation-id="${key}"]`,
    );
    if (!li) {
      return;
    }
    const target = projectId
      ? document.querySelector(`.project-chats[data-project-chats="${CSS.escape(String(projectId))}"]`)
      : document.getElementById("chat-list-items");
    const from = li.parentElement;
    if (target) {
      li.dataset.projectId = projectId ? String(projectId) : "";
      target.querySelector(".project-chats-note.hint")?.remove();
      target.prepend(li);
      target.hidden = target.id === "chat-list-items" ? false : target.hidden;
    } else {
      li.remove(); // Ziel nicht in dieser Ansicht (z. B. archiviertes Projekt)
    }
    if (from?.classList.contains("project-chats") && !from.querySelector("li.chat-item")) {
      const note = document.createElement("li");
      note.className = "project-chats-note hint";
      note.textContent = "Noch keine Chats.";
      from.append(note);
    }
    const ownList = document.getElementById("chat-list-items");
    const empty = document.getElementById("chat-list-empty");
    if (ownList && empty) {
      const has = Boolean(ownList.querySelector("li.chat-item"));
      ownList.hidden = !has;
      empty.hidden = has;
    }
  }

  // Einträge im Chatmenü von sidebar.js: nur eigene Chats (nicht „Mit mir geteilt“).
  function chatMenuExtras(li) {
    if (!li.closest("#chat-list-items, .project-chats")) {
      return [];
    }
    const entries = [["move-project", "In Projekt verschieben …"]];
    if (li.dataset.projectId) {
      entries.push(["remove-project", "Aus Projekt nehmen"]);
    }
    return entries;
  }

  // Neuer Chat auf „Neuer Chat im Projekt“: Eintrag in die Projektliste (sidebar.upsertItem).
  function newChatList() {
    const projectId = document.getElementById("chat")?.dataset.projectId;
    if (!projectId) {
      return null;
    }
    const target = document.querySelector(
      `.project-chats[data-project-chats="${CSS.escape(projectId)}"]`,
    );
    if (!target) {
      return null;
    }
    target.querySelector(".project-chats-note.hint")?.remove();
    const item = target.closest(".project-item");
    if (item) {
      setOpen(item, true, false);
    }
    return target;
  }

  // --- Suche (sofort beim Tippen; Absenden sucht auf dem Server) -----------------------------

  function applyFilter(needle) {
    for (const item of document.querySelectorAll(".project-item")) {
      const nameHit = (item.dataset.name || "").toLocaleLowerCase("de").includes(needle);
      let chatHits = 0;
      for (const li of item.querySelectorAll(".project-chats li.chat-item")) {
        const title = (li.dataset.title || UNTITLED).toLocaleLowerCase("de");
        li.hidden = Boolean(needle) && !nameHit && !title.includes(needle);
        chatHits += li.hidden ? 0 : 1;
      }
      item.hidden = Boolean(needle) && !nameHit && !chatHits;
      const chats = item.querySelector(".project-chats");
      const toggle = item.querySelector(".project-toggle");
      if (chats && toggle) {
        if (needle && !item.hidden) {
          chats.hidden = false;
        } else {
          chats.hidden = toggle.getAttribute("aria-expanded") !== "true";
        }
      }
    }
  }

  // --- Start ------------------------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", () => {
    if (!mg()) {
      return;
    }
    mg().chatMenuExtras = chatMenuExtras;
    mg().chatActions = {
      ...(mg().chatActions || {}),
      "move-project": moveChat,
      "remove-project": (id) => saveMove(id, null),
    };
    mg().projects = { newChatList };
    initToggles();

    document.getElementById("new-project-button")?.addEventListener("click", (event) => {
      if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey) {
        return;
      }
      event.preventDefault();
      createProject();
    });

    document.addEventListener("click", (event) => {
      const toggle = event.target.closest(".project-toggle");
      if (toggle) {
        const item = toggle.closest(".project-item");
        setOpen(item, toggle.getAttribute("aria-expanded") !== "true");
        return;
      }
      const menuButton = event.target.closest(".project-menu-button");
      if (menuButton) {
        const wasOpen = openMenu && openMenu.button === menuButton;
        closeMenu(false);
        if (!wasOpen) {
          showProjectMenu(menuButton);
        }
        return;
      }
      if (openMenu && !openMenu.menu.contains(event.target)) {
        closeMenu(false);
      }
    });

    const input = document.getElementById("chat-search-input");
    input?.addEventListener("input", () => {
      applyFilter(input.value.trim().toLocaleLowerCase("de"));
    });
    input?.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        window.setTimeout(() => applyFilter(""), 0); // nach dem Leeren durch sidebar.js
      }
    });
  });
})();
