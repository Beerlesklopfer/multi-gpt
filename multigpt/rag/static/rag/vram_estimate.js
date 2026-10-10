/* Modellverwaltung LM Studio: „Kontextfenster“ mit „Erwarteter Speicher“.
   Jede Auswahl mit Klasse vram-context bekommt daneben eine Zeile mit der
   Schätzung (Modelldatei + Kontext-Cache), dem Budget und ggf. einer Warnung.
   Wechselt das zugehörige Modell (data-vram-for), werden die Stufen neu
   aufgebaut (nur bis zur maximalen Kontextlänge laut LM Studio). Die Zahlen
   kommen vom Server (nur für Verwalter, nur lesend). */
(function () {
  "use strict";

  function formatStage(value) {
    return String(value).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  }

  function modelChoice(select) {
    if (select.dataset.vramModel) return select.dataset.vramModel;
    var source = select.dataset.vramFor && document.getElementById(select.dataset.vramFor);
    return source ? source.value : "";
  }

  function outputFor(select) {
    var box = select.parentNode.querySelector(".vram-estimate");
    if (!box) {
      box = document.createElement("div");
      box.className = "vram-estimate help";
      box.setAttribute("role", "status");
      box.setAttribute("aria-live", "polite");
      select.insertAdjacentElement("afterend", box);
    }
    return box;
  }

  function rebuild(select, stages) {
    var current = select.value;
    var keep = !current || stages.indexOf(Number(current)) >= 0;
    while (select.options.length > 1) select.remove(1);
    stages.forEach(function (value) {
      var option = document.createElement("option");
      option.value = String(value);
      option.textContent = formatStage(value);
      select.appendChild(option);
    });
    select.value = keep ? current : "";
  }

  function render(box, data) {
    box.textContent = "";
    box.classList.remove("vram-warning");
    if (!data || data.local === false) return;
    var lines = [data.text, data.budget_text].filter(Boolean);
    if (data.error) lines.push("LM Studio: " + data.error + " (letzte bekannte Werte)");
    lines.forEach(function (line) {
      var div = document.createElement("div");
      div.textContent = line;
      box.appendChild(div);
    });
    if (data.warning) {
      var warn = document.createElement("strong");
      warn.className = "vram-warning-text";
      warn.textContent = data.warning;
      box.appendChild(warn);
      box.classList.add("vram-warning");
    } else if (data.fits === true) {
      var ok = document.createElement("div");
      ok.className = "vram-ok";
      ok.textContent = "passt ins Budget.";
      box.appendChild(ok);
    }
  }

  function update(select, modelChanged) {
    var box = outputFor(select);
    var choice = modelChoice(select);
    var token = String(Date.now() + Math.random());
    select.dataset.vramToken = token;
    if (!choice || !select.dataset.vramUrl) {
      render(box, null);
      return;
    }
    var params = new URLSearchParams({ model: choice });
    if (select.value && !modelChanged) params.set("context", select.value);
    if (select.dataset.vramCapability) params.set("capability", select.dataset.vramCapability);
    fetch(select.dataset.vramUrl + "?" + params.toString(), {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        if (select.dataset.vramToken !== token) return; // veraltete Antwort
        if (data && data.stages && modelChanged) rebuild(select, data.stages);
        render(box, data);
      })
      .catch(function () {
        render(box, null);
      });
  }

  function init() {
    document.querySelectorAll("select.vram-context").forEach(function (select) {
      select.addEventListener("change", function () {
        update(select, false);
      });
      var source = select.dataset.vramFor && document.getElementById(select.dataset.vramFor);
      if (source) {
        source.addEventListener("change", function () {
          update(select, true);
        });
      }
      update(select, false);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
