// MultiGPT – Chatliste in der Seitenleiste (M5-02): Filter, Aktionsmenü,
// Umbenennen, Archivieren/Wiederherstellen, Exportieren, Löschen.
// Die gleichen Aktionen lösen Knöpfe mit data-chat-action (Kopf der
// Chatansicht) aus. Rechte prüft der Server; hier nur Anzeige.
// Titel werden ausschließlich als Text eingesetzt (textContent), nie als HTML.
"use strict";

(() => {
  const MAX_TITLE = 200;
  const UNTITLED = "Neuer Chat";

  const nav = () => document.getElementById("chat-list");
  const list = () => document.getElementById("chat-list-items");

  function detailUrl(id) {
    return window.MultiGPT.fillTemplate(nav()?.dataset.apiDetailTemplate || "", id);
  }

  function exportUrl(id) {
    return window.MultiGPT.fillTemplate(nav()?.dataset.exportTemplate || "", id);
  }

  function activeConversationId() {
    return document.getElementById("chat")?.dataset.conversationId || "";
  }

  function itemFor(id) {
    return list()?.querySelector(`li.chat-item[data-conversation-id="${CSS.escape(String(id))}"]`);
  }

  // --- Leere Liste / Filter ----------------------------------------------------

  function updateEmptyHints() {
    const ul = list();
    if (!ul) {
      return;
    }
    const items = Array.from(ul.querySelectorAll("li.chat-item"));
    const visible = items.filter((li) => !li.hidden);
    ul.hidden = !items.length;
    const empty = document.getElementById("chat-list-empty");
    if (empty) {
      empty.hidden = Boolean(items.length);
    }
    const noMatch = document.getElementById("chat-list-nomatch");
    if (noMatch) {
      noMatch.hidden = !items.length || Boolean(visible.length);
    }
  }

  function applyFilter() {
    const input = document.getElementById("chat-search-input");
    const ul = list();
    if (!input || !ul) {
      return;
    }
    const needle = input.value.trim().toLocaleLowerCase("de");
    for (const li of ul.querySelectorAll("li.chat-item")) {
      const title = (li.dataset.title || UNTITLED).toLocaleLowerCase("de");
      li.hidden = Boolean(needle) && !title.includes(needle);
    }
    updateEmptyHints();
  }

  // --- Einträge ----------------------------------------------------------------

  function makeMenuButton(title) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "chat-item-menu-button";
    button.setAttribute("aria-haspopup", "menu");
    button.setAttribute("aria-expanded", "false");
    button.setAttribute("aria-label", `Aktionen für „${title || UNTITLED}“`);
    button.textContent = "⋯";
    return button;
  }

  // Neuen Chat oben einfügen (chat.js nach dem Anlegen) oder vorhandenen nach oben holen.
  function upsertItem({ id, url, title }) {
    const ul = list();
    if (!ul || nav()?.dataset.view === "archived") {
      return null;
    }
    let li = itemFor(id);
    if (!li) {
      li = document.createElement("li");
      li.className = "chat-item";
      li.dataset.conversationId = String(id);
      li.dataset.title = title || "";
      li.dataset.archived = "false";
      const link = document.createElement("a");
      link.className = "chat-link";
      link.href = url;
      link.textContent = title || UNTITLED;
      li.append(link, makeMenuButton(title));
    }
    ul.prepend(li);
    updateEmptyHints();
    return li;
  }

  function markActive(id) {
    for (const link of list()?.querySelectorAll("a.chat-link") || []) {
      const active = link.closest("li").dataset.conversationId === String(id);
      link.classList.toggle("is-active", active);
      if (active) {
        link.setAttribute("aria-current", "page");
      } else {
        link.removeAttribute("aria-current");
      }
    }
  }

  // Titel überall aktualisieren: Seitenleiste, Kopf der Chatansicht, Fenstertitel.
  function setTitle(id, title) {
    const li = itemFor(id);
    if (li) {
      li.dataset.title = title;
      li.querySelector("a.chat-link").textContent = title || UNTITLED;
      li.querySelector(".chat-item-menu-button")
        ?.setAttribute("aria-label", `Aktionen für „${title || UNTITLED}“`);
    }
    for (const el of document.querySelectorAll(
      `[data-chat-action][data-conversation-id="${CSS.escape(String(id))}"]`,
    )) {
      el.dataset.title = title;
    }
    if (activeConversationId() === String(id)) {
      const heading = document.getElementById("chat-title");
      if (heading) {
        heading.textContent = title || UNTITLED;
      }
      document.title = `${title || UNTITLED} – MultiGPT`;
    }
  }

  function removeItem(id) {
    itemFor(id)?.remove();
    updateEmptyHints();
  }

  // --- Dialoge -------------------------------------------------------------------

  // Modaler Dialog (natives <dialog>): liefert den eingegebenen Text bzw. true,
  // oder null bei Abbruch.
  function openDialog({ heading, text, input, confirmLabel, danger }) {
    return new Promise((resolve) => {
      const dialog = document.createElement("dialog");
      dialog.className = "dialog";
      const form = document.createElement("form");
      form.method = "dialog";
      const h = document.createElement("h2");
      h.textContent = heading;
      h.id = `dialog-title-${Date.now()}`;
      dialog.setAttribute("aria-labelledby", h.id);
      form.append(h);
      if (text) {
        const p = document.createElement("p");
        p.textContent = text;
        form.append(p);
      }
      let field = null;
      if (input) {
        const label = document.createElement("label");
        label.textContent = input.label;
        field = document.createElement("input");
        field.type = "text";
        field.value = input.value || "";
        field.maxLength = MAX_TITLE;
        field.required = true;
        label.append(field);
        form.append(label);
      }
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
        if (field && !field.value.trim()) {
          event.preventDefault();
          field.focus();
          return;
        }
        result = field ? field.value.trim() : true;
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
      if (field) {
        field.select();
      } else {
        (danger ? cancel : ok).focus();
      }
    });
  }

  // Auch für andere Seiten (Sammlungen, collections.js).
  window.MultiGPT.openDialog = openDialog;

  // --- Aktionen ------------------------------------------------------------------

  async function rename(id, currentTitle) {
    const title = await openDialog({
      heading: "Chat umbenennen",
      input: { label: "Neuer Titel", value: currentTitle },
      confirmLabel: "Speichern",
    });
    if (title === null || title === currentTitle) {
      return;
    }
    try {
      const data = await window.MultiGPT.requestJson("PATCH", detailUrl(id), { title });
      setTitle(id, data.title);
      window.MultiGPT.announce("Chat umbenannt.");
    } catch (err) {
      window.MultiGPT.announce(`Umbenennen fehlgeschlagen: ${err.message}`, true);
    }
  }

  async function setArchived(id, archived) {
    try {
      await window.MultiGPT.requestJson("PATCH", detailUrl(id), { archived });
    } catch (err) {
      window.MultiGPT.announce(
        `${archived ? "Archivieren" : "Wiederherstellen"} fehlgeschlagen: ${err.message}`,
        true,
      );
      return;
    }
    removeItem(id);
    const chat = document.getElementById("chat");
    if (activeConversationId() === String(id) && chat?.dataset.streaming !== "true") {
      // Kopf der Chatansicht (Hinweis "Archiviert", Knöpfe) neu aufbauen.
      window.location.reload();
      return;
    }
    window.MultiGPT.announce(archived ? "Chat archiviert." : "Chat wiederhergestellt.");
  }

  async function remove(id, title) {
    const ok = await openDialog({
      heading: "Chat löschen?",
      text: `„${title || UNTITLED}“ wird mit allen Nachrichten endgültig gelöscht. Das lässt sich nicht rückgängig machen.`,
      confirmLabel: "Endgültig löschen",
      danger: true,
    });
    if (!ok) {
      return;
    }
    try {
      await window.MultiGPT.requestJson("DELETE", detailUrl(id));
    } catch (err) {
      window.MultiGPT.announce(`Löschen fehlgeschlagen: ${err.message}`, true);
      return;
    }
    if (activeConversationId() === String(id)) {
      window.location.assign(nav()?.dataset.indexUrl || "/");
      return;
    }
    removeItem(id);
    window.MultiGPT.announce("Chat gelöscht.");
  }

  function runAction(action, id, title) {
    if (action === "rename") {
      rename(id, title);
    } else if (action === "archive") {
      setArchived(id, true);
    } else if (action === "restore") {
      setArchived(id, false);
    } else if (action === "delete") {
      remove(id, title);
    }
  }

  // --- Aktionsmenü je Eintrag ------------------------------------------------------

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

  function showMenu(button) {
    const li = button.closest("li.chat-item");
    const id = li.dataset.conversationId;
    const title = li.dataset.title || "";
    const archived = li.dataset.archived === "true";
    const menu = document.createElement("ul");
    menu.className = "chat-item-menu";
    menu.setAttribute("role", "menu");
    menu.setAttribute("aria-label", button.getAttribute("aria-label"));

    const entries = [
      ["rename", "Umbenennen"],
      archived ? ["restore", "Wiederherstellen"] : ["archive", "Archivieren"],
      ["export", "Exportieren"],
      ["delete", "Löschen"],
    ];
    for (const [action, label] of entries) {
      const item = document.createElement("li");
      item.setAttribute("role", "none");
      let el;
      if (action === "export") {
        el = document.createElement("a");
        el.href = exportUrl(id);
        el.setAttribute("download", "");
      } else {
        el = document.createElement("button");
        el.type = "button";
        el.dataset.menuAction = action;
      }
      el.setAttribute("role", "menuitem");
      el.tabIndex = -1;
      el.className = action === "delete" ? "is-danger" : "";
      el.textContent = label;
      item.append(el);
      menu.append(item);
    }

    menu.addEventListener("click", (event) => {
      const el = event.target.closest("[role=menuitem]");
      if (!el) {
        return;
      }
      if (!el.dataset.menuAction) {
        // Export-Link: erst nach dem Klick entfernen, sonst folgt der Browser ihm nicht.
        window.setTimeout(() => closeMenu(false), 0);
        return;
      }
      closeMenu(false);
      if (el.dataset.menuAction) {
        button.focus();
        runAction(el.dataset.menuAction, id, title);
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

    li.append(menu);
    button.setAttribute("aria-expanded", "true");
    openMenu = { menu, button };
    menu.querySelector("[role=menuitem]").focus();
  }

  // --- Start -------------------------------------------------------------------------

  // Für chat.js: neuen Chat eintragen, Titel setzen, aktiv markieren.
  window.MultiGPT.sidebar = { upsertItem, setTitle, markActive };

  document.addEventListener("DOMContentLoaded", () => {
    for (const button of document.querySelectorAll(
      ".chat-item-menu-button, [data-chat-action]",
    )) {
      button.hidden = false;
    }

    document.addEventListener("click", (event) => {
      const menuButton = event.target.closest(".chat-item-menu-button");
      if (menuButton) {
        const wasOpen = openMenu && openMenu.button === menuButton;
        closeMenu(false);
        if (!wasOpen) {
          showMenu(menuButton);
        }
        return;
      }
      const actionButton = event.target.closest("[data-chat-action]");
      if (actionButton) {
        runAction(
          actionButton.dataset.chatAction,
          actionButton.dataset.conversationId,
          actionButton.dataset.title || "",
        );
        return;
      }
      if (openMenu && !openMenu.menu.contains(event.target)) {
        closeMenu(false);
      }
    });

    const input = document.getElementById("chat-search-input");
    if (input) {
      input.addEventListener("input", applyFilter);
      input.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && input.value) {
          event.stopPropagation();
          input.value = "";
          applyFilter();
        }
      });
    }
  });
})();
