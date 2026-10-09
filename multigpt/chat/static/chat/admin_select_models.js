// MultiGPT – Admin „Modelle auswählen“: Filter ohne Neuladen und Zeilenumschaltung.
//
// Nicht angehakte Zeilen werden beim Absenden deaktiviert, damit nur die
// übernommenen Modelle Felder senden (Django begrenzt die Feldanzahl; OpenRouter
// meldet Hunderte Modelle). Ohne JS funktioniert die Seite weiter (Filter per ?q=).
"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("provider-models-form");
  const filter = document.getElementById("model-filter");
  const count = document.getElementById("model-filter-count");
  const takeAll = document.getElementById("take-all");
  if (!form) {
    return;
  }
  const rows = Array.from(form.querySelectorAll("tr.model-row"));

  function mark(row) {
    const take = row.querySelector("input.take");
    row.classList.toggle("model-off", !take || !take.checked);
  }

  function visibleRows() {
    return rows.filter((row) => !row.hidden);
  }

  function applyFilter() {
    const needle = (filter ? filter.value : "").trim().toLowerCase();
    for (const row of rows) {
      row.hidden = needle !== "" && !row.dataset.modelId.includes(needle);
    }
    if (count) {
      count.textContent = `${visibleRows().length} von ${rows.length} angezeigt`;
    }
  }

  for (const row of rows) {
    mark(row);
  }
  form.addEventListener("change", (event) => {
    if (event.target.classList.contains("take")) {
      mark(event.target.closest("tr"));
    }
  });
  if (takeAll) {
    takeAll.addEventListener("change", () => {
      for (const row of visibleRows()) {
        const take = row.querySelector("input.take");
        if (take) {
          take.checked = takeAll.checked;
          mark(row);
        }
      }
    });
  }
  if (filter) {
    filter.addEventListener("input", applyFilter);
    applyFilter();
  }
  form.addEventListener("submit", () => {
    for (const row of rows) {
      const take = row.querySelector("input.take");
      if (!take || !take.checked) {
        for (const input of row.querySelectorAll("input, select")) {
          input.disabled = true;
        }
      }
    }
  });
});
