// MultiGPT – Auswahl der Sammlungen im Eingabefeld (M7, RAG).
// Zeigt je lesbarer Sammlung einen Schalter; die Auswahl gilt je Chat und wird
// im Browser gemerkt (localStorage, Fehler werden ignoriert). chat.js ruft beim
// Senden window.MultiGPT.collectionPicker.extend(payload) auf und schickt damit
// "collections": [ids] mit – nur, wenn mindestens eine Sammlung gewählt ist.
// Namen sind Nutzerdaten: nur textContent.
"use strict";

(() => {
  const STORAGE_PREFIX = "multigpt.collections.";

  function storageGet(key) {
    try {
      return window.localStorage.getItem(key);
    } catch {
      return null;
    }
  }

  function storageSet(key, value) {
    try {
      if (value === null) {
        window.localStorage.removeItem(key);
      } else {
        window.localStorage.setItem(key, value);
      }
    } catch {
      // Privater Modus o. Ä.: Auswahl gilt dann nur bis zum Neuladen.
    }
  }

  // Sammlungen des Projekts (data-project-collections, chat/projects.py): Vorauswahl,
  // solange für den Chat noch keine eigene Auswahl gemerkt ist.
  function projectSelection() {
    const raw = document.getElementById("chat")?.dataset.projectCollections || "";
    return new Set(raw.split(",").filter(Boolean));
  }

  function readSelection(conversationId) {
    const stored = conversationId ? storageGet(STORAGE_PREFIX + conversationId) : null;
    if (stored === null) {
      return projectSelection();
    }
    try {
      const ids = JSON.parse(stored || "[]");
      return new Set(Array.isArray(ids) ? ids.map(String) : []);
    } catch {
      return new Set();
    }
  }

  function init() {
    const fieldset = document.getElementById("collection-picker");
    const list = document.getElementById("collection-picker-list");
    const chat = document.getElementById("chat");
    // Ohne eingerichtete Dokumentsuche keine Auswahl (der Server lehnte sonst ab).
    if (!fieldset || !list || !chat || fieldset.dataset.searchReady !== "true") {
      return;
    }

    const conversationId = () => chat.dataset.conversationId || "";

    function selected() {
      return Array.from(list.querySelectorAll("input:checked"), (input) => Number(input.value));
    }

    function save() {
      const id = conversationId();
      if (!id) {
        return; // neuer Chat: wird gespeichert, sobald er eine ID hat
      }
      const ids = selected();
      // Im Projekt auch eine leere Auswahl merken, sonst käme die Vorauswahl zurück.
      const keep = ids.length || projectSelection().size;
      storageSet(STORAGE_PREFIX + id, keep ? JSON.stringify(ids) : null);
    }

    function render(collections) {
      const chosen = readSelection(conversationId());
      list.replaceChildren(
        ...collections.map((collection) => {
          const label = document.createElement("label");
          label.className = "tool-switch collection-switch";
          const input = document.createElement("input");
          input.type = "checkbox";
          input.name = "collections";
          input.value = String(collection.id);
          input.checked = chosen.has(String(collection.id));
          const text = document.createElement("span");
          text.textContent = String(collection.name ?? "");
          const count = Number(collection.indexed_count) || 0;
          label.title = `${count} indexierte${count === 1 ? "s Dokument" : " Dokumente"}${
            collection.is_owner ? "" : ` · von ${collection.owner_name ?? ""}`
          }`;
          label.append(input, text);
          return label;
        }),
      );
      const manage = document.createElement("a");
      manage.className = "collection-picker-manage";
      manage.href = fieldset.dataset.manageUrl || "#";
      manage.textContent = "Verwalten";
      list.append(manage);
      fieldset.hidden = !collections.length;
      // Nicht mehr lesbare Sammlungen fallen aus der gespeicherten Auswahl; eine
      // reine Projekt-Vorauswahl wird dabei nicht festgeschrieben.
      if (storageGet(STORAGE_PREFIX + conversationId()) !== null) {
        save();
      }
    }

    async function load() {
      try {
        const response = await fetch(fieldset.dataset.apiCollections, {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        if (!response.ok) {
          return;
        }
        const data = await response.json();
        render(Array.isArray(data) ? data.filter((c) => c && c.id != null) : []);
      } catch {
        // Ohne Liste eben ohne Dokumentsuche; Chatten geht weiter.
      }
    }

    list.addEventListener("change", save);

    // Neuer Chat bekommt beim ersten Senden eine ID: Auswahl dann darunter merken.
    // „Neuer Chat“ ohne Neuladen (ID leer): Auswahl zurücksetzen.
    new MutationObserver(() => {
      if (conversationId()) {
        save();
      } else {
        for (const input of list.querySelectorAll("input:checked")) {
          input.checked = false;
        }
      }
    }).observe(chat, {
      attributes: true,
      attributeFilter: ["data-conversation-id"],
    });

    window.MultiGPT.collectionPicker = {
      extend(payload) {
        if (fieldset.hidden) {
          return payload;
        }
        const ids = selected();
        return ids.length ? { ...payload, collections: ids } : payload;
      },
      selected,
    };

    load();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
