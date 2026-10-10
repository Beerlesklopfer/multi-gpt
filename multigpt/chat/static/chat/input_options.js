// MultiGPT – Eingabeoptionen ein-/ausblenden. Die Optionszeilen über dem
// Eingabefeld (Modell, Vergleichen samt Modellauswahl, Websuche, Werkzeuge,
// Dokumente) lassen sich einklappen, damit z. B. drei Vergleichsspalten mehr
// Höhe bekommen. Eingeklappt zeigt eine Zeile die aktive Auswahl. Der Zustand
// gilt je Browser (localStorage, Fehler werden ignoriert).
// Namen von Modellen und Sammlungen nur als Text. Kein Inline-JS.
"use strict";

(() => {
  const STORAGE_KEY = "multigpt.inputOptionsCollapsed";

  function storageGet() {
    try {
      return window.localStorage.getItem(STORAGE_KEY);
    } catch {
      return null;
    }
  }

  function storageSet(collapsed) {
    try {
      if (collapsed) {
        window.localStorage.setItem(STORAGE_KEY, "1");
      } else {
        window.localStorage.removeItem(STORAGE_KEY);
      }
    } catch {
      // Privater Modus o. Ä.: gilt dann nur bis zum Neuladen.
    }
  }

  function labelText(input) {
    return (input.dataset.name || input.closest("label")?.textContent || "").replace(/\s+/g, " ").trim();
  }

  document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("chat-form");
    const toggle = document.getElementById("input-options-toggle");
    const options = document.getElementById("chat-input-options");
    const summary = document.getElementById("input-options-summary");
    if (!form || !toggle || !options || !summary) {
      return;
    }
    const label = toggle.querySelector(".input-options-toggle-label");
    const modelSelect = document.getElementById("model-select");
    const compareToggle = document.getElementById("compare-toggle");
    let collapsed = false;

    function comparing() {
      return Boolean(compareToggle?.checked) && !compareToggle.closest("label")?.hidden;
    }

    function checkedNames(selector) {
      return Array.from(document.querySelectorAll(selector), labelText).filter(Boolean);
    }

    // z. B. „Vergleich: gpt-5.5, claude-sonnet-5-5 · Dokumente: CLV“
    function summaryText() {
      const parts = [];
      if (comparing()) {
        const names = checkedNames("#compare-models-list input:checked");
        parts.push(`Vergleich: ${names.length ? names.join(", ") : "keine Modelle gewählt"}`);
      } else {
        const option = modelSelect?.selectedOptions[0];
        if (option && option.value) {
          parts.push(`Modell: ${option.dataset.name || option.textContent}`);
        }
      }
      if (document.getElementById("web-search-toggle")?.checked) {
        parts.push("Websuche");
      }
      const tools = document.getElementById("tool-servers");
      if (tools && !tools.hidden && !comparing()) {
        const names = checkedNames("#tool-servers-list input:checked");
        if (names.length) {
          parts.push(`Werkzeuge: ${names.join(", ")}`);
        }
      }
      const docs = document.getElementById("collection-picker");
      if (docs && !docs.hidden) {
        const names = checkedNames("#collection-picker-list input:checked");
        if (names.length) {
          parts.push(`Dokumente: ${names.join(", ")}`);
        }
      }
      return parts.join(" · ");
    }

    function updateSummary() {
      const text = collapsed ? summaryText() : "";
      summary.textContent = text;
      summary.title = text;
      summary.hidden = !collapsed || !text;
    }

    function setCollapsed(value, remember = true) {
      collapsed = value;
      options.hidden = value;
      form.classList.toggle("options-collapsed", value);
      toggle.setAttribute("aria-expanded", String(!value));
      label.textContent = value ? "Eingabeoptionen einblenden" : "Eingabeoptionen ausblenden";
      toggle.title = label.textContent;
      if (remember) {
        storageSet(value);
      }
      updateSummary();
    }

    toggle.addEventListener("click", () => setCollapsed(!collapsed));

    // Auswahl geändert (auch Listen, die später gefüllt werden).
    form.addEventListener("change", updateSummary);
    document.addEventListener("multigpt:models-loaded", updateSummary);
    const observer = new MutationObserver(updateSummary);
    for (const id of ["compare-models-list", "collection-picker-list", "tool-servers-list"]) {
      const el = document.getElementById(id);
      if (el) {
        observer.observe(el, { childList: true });
      }
    }
    for (const id of ["collection-picker", "tool-servers"]) {
      const el = document.getElementById(id);
      if (el) {
        observer.observe(el, { attributes: true, attributeFilter: ["hidden"] });
      }
    }

    // Fehlt eine nötige Auswahl, vor dem Senden aufklappen, damit chat.js
    // bzw. compare.js den Fokus dorthin setzen kann.
    function revealIfIncomplete() {
      if (!collapsed) {
        return;
      }
      const count = document.querySelectorAll("#compare-models-list input:checked:not(:disabled)").length;
      const incomplete = comparing() ? count < 2 || count > 3 : !modelSelect?.value;
      if (incomplete) {
        setCollapsed(false, false);
      }
    }
    form.addEventListener("submit", revealIfIncomplete, true);
    document.getElementById("message-input")?.addEventListener(
      "keydown",
      (event) => {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
          revealIfIncomplete();
        }
      },
      true,
    );

    toggle.hidden = false;
    setCollapsed(storageGet() === "1", false);
  });
})();
