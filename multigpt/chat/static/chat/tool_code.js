// MultiGPT – Anzeige des Werkzeugs run_python (Berechnungen, M4a-10).
//
// Werkzeugzeilen (chat/_tool_call.html bzw. buildToolRow in chat.js) zeigen die
// Argumente als JSON. Für run_python wird daraus ein Codeblock mit
// Syntax-Hervorhebung (highlight.js, Sprache python) und die Ausgabe aufklappbar.
// Beides wirkt für geladene und für live gestreamte Zeilen (MutationObserver);
// chat.js bleibt unverändert, applyToolResult findet .tool-call-result weiter.
//
// Sicherheit: Code und Ausgabe stammen vom Modell bzw. aus der Sandbox und sind
// nicht vertrauenswürdig – nur textContent; highlight.js maskiert selbst.
"use strict";

(() => {
  const TOOL = "run_python";

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) {
      node.className = cls;
    }
    if (text !== undefined) {
      node.textContent = text;
    }
    return node;
  }

  function section(title, open) {
    const details = el("details", "tool-call-section");
    details.open = open;
    details.append(el("summary", "tool-call-section-title", title));
    return details;
  }

  function enhance(row) {
    if (row.dataset.codeView) {
      return;
    }
    const name = row.querySelector(".tool-call-name");
    const args = row.querySelector(".tool-call-arguments");
    if (!name || !args || name.textContent.trim() !== TOOL) {
      return;
    }
    let code = null;
    try {
      const parsed = JSON.parse(args.textContent);
      code = parsed && typeof parsed.code === "string" ? parsed.code : null;
    } catch {
      code = null;
    }
    if (code === null) {
      return; // unbrauchbare Argumente: JSON bleibt sichtbar
    }
    row.dataset.codeView = "1";

    const codeBox = section("Code (Python)", true);
    const pre = el("pre", "tool-call-code");
    const codeEl = el("code", "language-python", code);
    pre.append(codeEl);
    codeBox.append(pre);
    if (window.hljs && window.hljs.getLanguage && window.hljs.getLanguage("python")) {
      try {
        window.hljs.highlightElement(codeEl);
      } catch {
        // ohne Hervorhebung weiter
      }
    }
    const label = args.previousElementSibling;
    if (label && label.classList.contains("tool-call-label")) {
      label.hidden = true;
    }
    args.hidden = true;
    args.before(codeBox);

    // Ausgabe aufklappbar; .tool-call-result bleibt dasselbe Element.
    const result = row.querySelector(".tool-call-result");
    if (result && !result.closest(".tool-call-section")) {
      const outLabel = result.previousElementSibling;
      if (outLabel && outLabel.classList.contains("tool-call-label")) {
        outLabel.hidden = true;
      }
      const outBox = section("Ausgabe", true);
      result.before(outBox);
      outBox.append(result);
    }
  }

  function scan(root) {
    if (!(root instanceof Element)) {
      return;
    }
    if (root.matches(".tool-call-item")) {
      enhance(root);
    }
    for (const row of root.querySelectorAll(".tool-call-item")) {
      enhance(row);
    }
  }

  function start() {
    scan(document.body);
    new MutationObserver((records) => {
      for (const record of records) {
        for (const added of record.addedNodes) {
          scan(added);
        }
      }
    }).observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
