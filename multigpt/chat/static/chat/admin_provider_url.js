/* MultiGPT – Admin: Basis-URL je Anbieterart automatisch vorbelegen.
 *
 * Daten aus #provider-url-defaults: {defaults: {kind: url}, presets: [{label, url}]}.
 * - Beim Wechsel der Art wird die Basis-URL ersetzt, solange sie leer ist oder
 *   noch einer bekannten Standard-/Vorschlags-URL entspricht (eigene URLs bleiben).
 * - Für OpenAI-kompatible Anbieter schlägt eine Liste bekannte Endpunkte vor.
 */
(function () {
  "use strict";

  var dataElement = document.getElementById("provider-url-defaults");
  var kind = document.getElementById("id_kind");
  var url = document.getElementById("id_base_url");
  if (!dataElement || !kind || !url) return;

  var data = {};
  try {
    data = JSON.parse(dataElement.textContent) || {};
  } catch (error) {
    return;
  }
  var defaults = data.defaults || {};
  var presets = data.presets || [];

  var known = {};
  Object.keys(defaults).forEach(function (key) {
    if (defaults[key]) known[defaults[key]] = true;
  });
  presets.forEach(function (preset) {
    known[preset.url] = true;
  });

  // Vorschlagsliste für OpenAI-kompatible Endpunkte.
  var list = document.createElement("datalist");
  list.id = "provider-url-presets";
  presets.forEach(function (preset) {
    var option = document.createElement("option");
    option.value = preset.url;
    option.label = preset.label;
    list.appendChild(option);
  });
  url.parentNode.appendChild(list);

  // onlyIfEmpty: beim Laden nur ein leeres Feld füllen; beim Wechsel der Art auch
  // eine bekannte Standard-/Vorschlags-URL ersetzen.
  function apply(onlyIfEmpty) {
    var current = url.value.trim();
    var target = defaults[kind.value] || "";
    url.setAttribute("placeholder", target);
    if (kind.value === "openai_compat") {
      url.setAttribute("list", list.id);
    } else {
      url.removeAttribute("list");
    }
    if (target && (!current || (!onlyIfEmpty && known[current]))) {
      url.value = target;
    }
  }

  kind.addEventListener("change", function () {
    apply(false);
  });
  // Beim Laden: nur ein leeres Feld mit dem Standard der gewählten Art füllen.
  apply(true);
})();
