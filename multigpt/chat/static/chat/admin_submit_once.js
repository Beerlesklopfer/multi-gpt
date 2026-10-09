/* MultiGPT – Admin: Formular nur einmal absenden.
 * Das Speichern eines Anbieters prüft danach die Verbindung und kann einige
 * Sekunden dauern; ein zweiter Klick würde den Anbieter doppelt anlegen wollen.
 */
(function () {
  "use strict";
  var form = document.querySelector("#content-main form");
  if (!form) return;
  var sent = false;
  form.addEventListener("submit", function (event) {
    if (sent) {
      event.preventDefault();
      return;
    }
    sent = true;
    form.querySelectorAll('input[type="submit"], button[type="submit"]').forEach(function (button) {
      // Erst nach dem Absenden sperren, damit der Name des Knopfes (_save,
      // _continue, …) noch mitgeschickt wird.
      window.setTimeout(function () {
        button.disabled = true;
      }, 0);
    });
    document.body.style.cursor = "progress";
  });
})();
