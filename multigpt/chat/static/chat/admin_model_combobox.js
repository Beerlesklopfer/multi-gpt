/* MultiGPT – Admin: Combobox für „Modell-ID“ in der Modell-Tabelle eines Anbieters.
 *
 * Die zuletzt vom Anbieter gemeldeten Modelle kommen als JSON aus dem Element
 * #reported-model-choices ([{id, capability, tools, vision, exists}] bzw. {by_provider: {...}}). Jedes Feld *-model_id
 * bekommt einen Knopf ▾, der eine filterbare Liste öffnet. Die Auswahl setzt die
 * Modell-ID und schlägt die Fähigkeit vor; der Anzeigename folgt jeder Änderung
 * der Modell-ID (change).
 */
(function () {
  "use strict";

  var dataElement = document.getElementById("reported-model-choices");
  if (!dataElement) return;
  var data = [];
  try {
    data = JSON.parse(dataElement.textContent) || [];
  } catch (error) {
    data = [];
  }

  // Zwei Formen: eine Liste (Modell-Tabelle eines Anbieters) oder
  // {"by_provider": {"<id>": [...]}} (Formular „KI-Modell“ mit Anbieterauswahl).
  function currentChoices() {
    if (Array.isArray(data)) return data;
    var provider = document.getElementById("id_provider");
    var byProvider = data.by_provider || {};
    return (provider && byProvider[provider.value]) || [];
  }

  var MAX_SHOWN = 200;
  var counter = 0;

  function fieldOf(input, name) {
    return document.getElementById(input.id.replace(/model_id$/, name));
  }

  function enhance(input) {
    if (!input || input.dataset.combobox || input.id.indexOf("__prefix__") !== -1) return;
    input.dataset.combobox = "1";
    counter += 1;
    var listId = "model-choices-" + counter;

    var wrapper = document.createElement("span");
    wrapper.className = "model-combobox";
    input.parentNode.insertBefore(wrapper, input);
    wrapper.appendChild(input);
    input.setAttribute("autocomplete", "off");
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-controls", listId);
    input.setAttribute("aria-expanded", "false");

    var button = document.createElement("button");
    button.type = "button";
    button.className = "model-combobox-toggle";
    button.textContent = "▾";
    button.setAttribute("aria-label", "Modell aus der Liste des Anbieters wählen");
    button.setAttribute("aria-controls", listId);
    button.setAttribute("aria-expanded", "false");
    wrapper.appendChild(button);

    var list = document.createElement("ul");
    list.id = listId;
    list.className = "model-combobox-list";
    list.setAttribute("role", "listbox");
    list.hidden = true;
    wrapper.appendChild(list);

    var active = -1;

    // Die Liste liegt fest über der Seite (position: fixed), damit sie nicht vom
    // Inline-Bereich abgeschnitten wird (Django setzt dort overflow: auto/scroll).
    function place() {
      var rect = wrapper.getBoundingClientRect();
      var viewportHeight = document.documentElement.clientHeight;
      var below = viewportHeight - rect.bottom;
      var above = rect.top;
      var upward = below < 200 && above > below;
      var space = (upward ? above : below) - 8;
      list.style.left = Math.max(0, rect.left) + "px";
      list.style.minWidth = rect.width + "px";
      list.style.maxHeight = Math.max(120, Math.min(space, 320)) + "px";
      if (upward) {
        list.style.top = "";
        list.style.bottom = viewportHeight - rect.top + 2 + "px";
      } else {
        list.style.bottom = "";
        list.style.top = rect.bottom + 2 + "px";
      }
    }

    function onViewportChange() {
      if (!list.hidden) place();
    }

    function setExpanded(open) {
      var wasOpen = !list.hidden;
      list.hidden = !open;
      if (open) place();
      if (open && !wasOpen) {
        window.addEventListener("scroll", onViewportChange, true);
        window.addEventListener("resize", onViewportChange);
      } else if (!open && wasOpen) {
        window.removeEventListener("scroll", onViewportChange, true);
        window.removeEventListener("resize", onViewportChange);
      }
      input.setAttribute("aria-expanded", open ? "true" : "false");
      button.setAttribute("aria-expanded", open ? "true" : "false");
      if (!open) {
        active = -1;
        input.removeAttribute("aria-activedescendant");
      }
    }

    function render(filterText) {
      list.textContent = "";
      active = -1;
      var choices = currentChoices();
      if (!choices.length) {
        var empty = document.createElement("li");
        empty.className = "model-combobox-empty";
        empty.textContent = "Noch keine Modellliste – bitte erst „Verbindung jetzt prüfen“.";
        list.appendChild(empty);
        return;
      }
      var needle = (filterText || "").trim().toLowerCase();
      var matches = choices.filter(function (choice) {
        return !needle || choice.id.toLowerCase().indexOf(needle) !== -1;
      });
      matches.slice(0, MAX_SHOWN).forEach(function (choice, index) {
        var item = document.createElement("li");
        item.id = listId + "-" + index;
        item.className = "model-combobox-option";
        item.setAttribute("role", "option");
        item.dataset.value = choice.id;
        item.dataset.capability = choice.capability || "";
        item.dataset.tools = String(choice.tools === true);
        item.dataset.vision = String(choice.vision === true);
        item.textContent = choice.id;
        if (choice.exists) {
          var badge = document.createElement("span");
          badge.className = "model-combobox-badge";
          badge.textContent = "vorhanden";
          item.appendChild(document.createTextNode(" "));
          item.appendChild(badge);
        }
        list.appendChild(item);
      });
      var info = document.createElement("li");
      info.className = "model-combobox-empty";
      if (!matches.length) {
        info.textContent = "Keine Treffer.";
        list.appendChild(info);
      } else if (matches.length > MAX_SHOWN) {
        info.textContent = "… " + (matches.length - MAX_SHOWN) + " weitere – bitte genauer filtern.";
        list.appendChild(info);
      }
    }

    function options() {
      return list.querySelectorAll(".model-combobox-option");
    }

    function highlight(index) {
      var items = options();
      if (!items.length) return;
      if (active >= 0 && items[active]) items[active].removeAttribute("aria-selected");
      active = (index + items.length) % items.length;
      items[active].setAttribute("aria-selected", "true");
      items[active].scrollIntoView({ block: "nearest" });
      input.setAttribute("aria-activedescendant", items[active].id);
    }

    function choose(item) {
      var value = item.dataset.value;
      input.value = value;
      var capability = fieldOf(input, "capability");
      if (capability && item.dataset.capability) capability.value = item.dataset.capability;
      // Häkchen der Fähigkeiten-Matrix vorbelegen (Schätzung aus der Modell-ID).
      var tools = fieldOf(input, "supports_tools");
      if (tools) tools.checked = item.dataset.tools === "true";
      var vision = fieldOf(input, "supports_vision");
      if (vision) vision.checked = item.dataset.vision === "true";
      setExpanded(false);
      input.dispatchEvent(new Event("change", { bubbles: true }));
      input.focus();
    }

    button.addEventListener("click", function () {
      if (list.hidden) {
        render("");
        setExpanded(true);
        input.focus();
      } else {
        setExpanded(false);
      }
    });

    // Anzeigename folgt der Modell-ID (Auswahl löst change aus, Tippen beim Verlassen).
    input.addEventListener("change", function () {
      var displayName = fieldOf(input, "display_name");
      if (displayName) displayName.value = input.value.trim();
    });

    input.addEventListener("input", function () {
      render(input.value);
      setExpanded(true);
    });

    input.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        if (list.hidden) {
          render(input.value);
          setExpanded(true);
        }
        highlight(active + 1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        if (!list.hidden) highlight(active < 0 ? -1 : active - 1);
      } else if (event.key === "Enter" && !list.hidden) {
        // Bei offener Liste nie das Formular absenden: gewählten oder einzigen
        // Treffer übernehmen, sonst nur schließen.
        event.preventDefault();
        var items = options();
        if (active >= 0 && items[active]) choose(items[active]);
        else if (items.length === 1) choose(items[0]);
        else setExpanded(false);
      } else if (event.key === "Tab") {
        setExpanded(false);
      } else if (event.key === "Escape" && !list.hidden) {
        event.preventDefault();
        setExpanded(false);
      }
    });

    // mousedown statt click: sonst schließt blur die Liste vor der Auswahl.
    list.addEventListener("mousedown", function (event) {
      var item = event.target.closest(".model-combobox-option");
      if (!item) return;
      event.preventDefault();
      choose(item);
    });

    document.addEventListener("click", function (event) {
      if (!wrapper.contains(event.target)) setExpanded(false);
    });
  }

  function enhanceAll(root) {
    (root || document).querySelectorAll('input[id$="-model_id"], input#id_model_id').forEach(enhance);
  }

  enhanceAll(document);
  // Neue Zeile über „KI-Modell hinzufügen“ (Django löst formset:added aus).
  document.addEventListener("formset:added", function (event) {
    enhanceAll(event.target);
  });
})();
