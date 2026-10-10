// MultiGPT – Werkzeugaufruf als Text (chat/text_calls.py, chat/_tool_text.html).
//
// Hat das Modell z. B. generate_image("…") nur als Text geschrieben, zeigt der
// Server unter der Antwort einen Hinweis. Hier wird der Knopf „Ausführen“
// eingeblendet, wenn das Werkzeug in dieser Ansicht nutzbar ist, und er belegt
// nur vor: generate_image -> Modus „Bild“ mit der Beschreibung im Eingabefeld,
// web_search -> Schalter „Websuche“ mit der Suchanfrage. Gesendet wird erst,
// wenn der Nutzer sendet. Nur eingebaute Werkzeuge, nie MCP (siehe text_calls.py).
// Das Argument stammt vom Modell: nur als Wert des Eingabefelds, nie als HTML.
"use strict";

(() => {
  const MultiGPT = (window.MultiGPT = window.MultiGPT || {});

  function controls() {
    const form = document.getElementById("chat-form");
    return {
      form,
      textarea: document.getElementById("message-input"),
      imageToggle: document.getElementById("image-mode-toggle"),
      searchToggle: document.getElementById("web-search-toggle"),
    };
  }

  function usable(tool) {
    const c = controls();
    if (!c.form || !c.textarea) {
      return false; // nur lesen
    }
    if (tool === "generate_image") {
      return Boolean(c.imageToggle && MultiGPT.imageMode?.activate);
    }
    if (tool === "web_search") {
      return Boolean(c.searchToggle && !c.searchToggle.disabled);
    }
    return false;
  }

  function scan(root) {
    if (!(root instanceof Element)) {
      return;
    }
    const boxes = root.matches(".tool-text-hint")
      ? [root]
      : root.querySelectorAll(".tool-text-hint");
    for (const box of boxes) {
      const button = box.querySelector("[data-tool-text-run]");
      if (button) {
        button.hidden = !usable(box.dataset.toolText);
      }
    }
  }

  function run(box) {
    const c = controls();
    const tool = box.dataset.toolText;
    const argument = box.dataset.toolTextArgument || "";
    if (!usable(tool) || !argument) {
      return;
    }
    const setStatus = MultiGPT.chatView?.setStatus || (() => {});
    if (c.textarea.value.trim() && c.textarea.value.trim() !== argument) {
      setStatus("Im Eingabefeld steht schon Text. Bitte zuerst leeren, dann erneut „Ausführen“.", true);
      return;
    }
    if (tool === "generate_image") {
      MultiGPT.imageMode.activate();
    } else if (!c.searchToggle.checked) {
      c.searchToggle.checked = true;
      c.searchToggle.dispatchEvent(new Event("change", { bubbles: true }));
    }
    c.textarea.value = argument;
    c.textarea.dispatchEvent(new Event("input", { bubbles: true }));
    c.textarea.focus({ preventScroll: true });
    setStatus(
      tool === "generate_image"
        ? "Modus „Bild“ ist an, die Bildbeschreibung steht im Eingabefeld. Zum Erzeugen senden."
        : "Websuche ist an, die Suchanfrage steht im Eingabefeld. Zum Suchen senden.",
    );
  }

  function start() {
    scan(document.body);
    document.addEventListener("click", (event) => {
      const button = event.target instanceof Element && event.target.closest("[data-tool-text-run]");
      if (button) {
        run(button.closest(".tool-text-hint"));
      }
    });
    const log = document.getElementById("chat-log");
    if (log) {
      new MutationObserver((records) => {
        for (const record of records) {
          for (const added of record.addedNodes) {
            scan(added);
          }
        }
      }).observe(log, { childList: true, subtree: true });
    }
  }

  // Nach image_mode.js (setzt MultiGPT.imageMode erst bei DOMContentLoaded);
  // defer-Skripte laufen bei readyState „interactive“, also davor.
  if (document.readyState === "complete") {
    start();
  } else {
    document.addEventListener("DOMContentLoaded", start);
  }
})();
