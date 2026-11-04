/* Modellverwaltung LM Studio: „Kontextfenster“ mit „Erwarteter Speicher“.
   Jede Auswahl mit Klasse vram-context bekommt daneben eine Zeile mit der
   Schätzung (Modelldatei + Kontext-Cache), dem Budget und ggf. einer Warnung.
   Wechselt das zugehörige Modell (data-vram-for), werden die Stufen neu
   aufgebaut (nur bis zur maximalen Kontextlänge laut LM Studio). Die Zahlen
   kommen vom Server (nur für Verwalter, nur lesend).
   In der Liste „KI-Modelle“ (#result_list) steht statt der Zeilen nur ein kurzes
   Kennzeichen („7,1 GB ✓“, Warnung rot „9,0 GB ⚠“); der volle Text erscheint beim
   Darüberfahren bzw. mit der Tastatur (Fokus) und für Screenreader. */
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
      if (select.closest("#result_list")) {
        box.classList.add("vram-compact");
        box.id = "vram-" + (select.id || select.name || String(Math.random()).slice(2));
      } else {
        box.setAttribute("role", "status");
        box.setAttribute("aria-live", "polite");
      }
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

  function gbText(value) {
    return Number(value).toLocaleString("de-DE", { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + " GB";
  }

  // Kurzform für die Liste: Kennzeichen sichtbar, Einzelheiten im Aufklapper.
  function renderCompact(box, data, lines) {
    var mark = document.createElement("span");
    mark.className = "vram-mark";
    var total = data.known && data.estimate ? gbText(data.estimate.total_gb) : "?";
    var symbol = data.warning ? " ⚠" : data.fits === true ? " ✓" : "";
    mark.textContent = total + symbol;
    var details = document.createElement("span");
    details.className = "vram-details";
    details.setAttribute("role", "tooltip");
    details.id = box.id + "-details";
    lines.concat(data.warning ? [data.warning] : data.fits === true ? ["passt ins Budget."] : [])
      .forEach(function (line) {
        var div = document.createElement("span");
        div.textContent = line;
        details.appendChild(div);
      });
    box.tabIndex = 0;
    box.setAttribute("aria-describedby", details.id);
    box.title = "";
    box.append(mark, details);
    if (data.warning) box.classList.add("vram-warning");
    else if (data.fits === true) box.classList.add("vram-fits");
  }

  function render(box, data) {
    box.textContent = "";
    box.classList.remove("vram-warning", "vram-fits");
    box.removeAttribute("tabindex");
    if (!data || data.local === false) return;
    var lines = [data.text, data.budget_text].filter(Boolean);
    if (data.error) lines.push("LM Studio: " + data.error + " (letzte bekannte Werte)");
    if (box.classList.contains("vram-compact")) {
      renderCompact(box, data, lines);
      return;
    }
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
