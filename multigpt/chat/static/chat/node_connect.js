// API-Keys (M15): Key ins Eingabefeld einfügen oder per „Erzeugen“ anlegen; JSON und
// Befehl darunter übernehmen ihn sofort. Kopieren auch ohne Clipboard-API (http):
// Ersatzweg über eine Textauswahl. Der Key bleibt nur im Browser, nichts wird gespeichert.
(function () {
  "use strict";

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise((resolve, reject) => {
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.className = "copy-buffer";
      document.body.append(area);
      area.select();
      const ok = document.execCommand("copy");
      area.remove();
      ok ? resolve() : reject(new Error("copy failed"));
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    for (const button of document.querySelectorAll("[data-connect-copy]")) {
      button.addEventListener("click", async () => {
        const target = document.getElementById(button.dataset.connectCopy);
        const done = button.parentElement.querySelector("[data-copy-done]");
        try {
          await copyText(target.textContent.trim());
          done.textContent = "Kopiert";
        } catch (e) {
          done.textContent = "Kopieren nicht möglich – bitte markieren und kopieren.";
        }
        done.hidden = false;
        window.setTimeout(() => { done.hidden = true; }, 4000);
      });
    }

    const box = document.querySelector("[data-connect]");
    if (!box) return;
    const input = box.querySelector("#connect-key-input");
    const status = box.querySelector("[data-connect-status]");
    const placeholder = box.dataset.placeholder;
    const section = box.closest("section") || document;
    const blocks = section.querySelectorAll("pre[data-template]");

    function render() {
      const key = input.value.trim() || placeholder;
      for (const pre of blocks) {
        pre.firstElementChild.textContent = pre.dataset.template.split(placeholder).join(key);
      }
    }
    input.addEventListener("input", render);
    input.addEventListener("paste", () => window.setTimeout(render, 0));

    const generate = box.querySelector("[data-generate-key]");
    if (generate && box.dataset.quickUrl) {
      generate.hidden = false;
      generate.addEventListener("click", async () => {
        generate.disabled = true;
        try {
          const token = box.querySelector("input[name=csrfmiddlewaretoken]").value;
          const response = await fetch(box.dataset.quickUrl, {
            method: "POST",
            headers: { "X-CSRFToken": token, "Accept": "application/json" },
            credentials: "same-origin",
          });
          const data = await response.json();
          if (!response.ok) throw new Error(data.error || "Fehler");
          input.value = data.secret;
          render();
          status.textContent = `Key „${data.name}“ erzeugt und eingesetzt – nur jetzt sichtbar, bitte kopieren.`;
        } catch (e) {
          status.textContent = `Key konnte nicht erzeugt werden: ${e.message}`;
        } finally {
          generate.disabled = false;
        }
      });
    }
  });
})();
