// MultiGPT – Seite „Familie“ (M6-04): Rückfrage vor Sperren, Passwort zurücksetzen,
// Einsicht einschalten und Gruppe löschen (Formulare mit data-confirm), Kopierknopf
// für ein neues Passwort, Monatswahl ohne Extra-Klick.
// Kein Inline-JS. Ohne JavaScript funktionieren alle Formulare normal (ohne Rückfrage);
// Rechte und Schutzregeln prüft der Server.
"use strict";

(() => {
  function confirmDialog(text, danger) {
    const open = window.MultiGPT && window.MultiGPT.openDialog;
    if (open) {
      return open({ heading: "Bitte bestätigen", text, confirmLabel: "Ja, fortfahren", danger });
    }
    return Promise.resolve(window.confirm(text));
  }

  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.dataset.confirm) {
      return;
    }
    if (form.dataset.confirmed === "1") {
      return;
    }
    event.preventDefault();
    const danger = Boolean(form.querySelector(".button-danger"));
    const ok = await confirmDialog(form.dataset.confirm, danger);
    if (ok) {
      form.dataset.confirmed = "1";
      form.requestSubmit();
    }
  });

  // Neues Passwort kopieren
  for (const button of document.querySelectorAll("[data-copy-target]")) {
    const target = document.getElementById(button.dataset.copyTarget);
    if (!target || !navigator.clipboard) {
      continue;
    }
    button.hidden = false;
    button.addEventListener("click", async () => {
      const done = button.parentElement.querySelector("[data-copy-done]");
      try {
        await navigator.clipboard.writeText(target.textContent.trim());
        if (done) {
          done.textContent = "Kopiert";
          done.hidden = false;
        }
      } catch {
        if (done) {
          done.textContent = "Kopieren nicht möglich – bitte markieren und abschreiben.";
          done.hidden = false;
        }
      }
    });
  }

  // Monatswahl (Verbrauch aller): Wechsel lädt sofort.
  for (const select of document.querySelectorAll("select[data-autosubmit]")) {
    const button = select.form && select.form.querySelector("[data-autosubmit-button]");
    if (button) {
      button.hidden = true;
    }
    select.addEventListener("change", () => select.form && select.form.submit());
  }
})();
