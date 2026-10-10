// MultiGPT – Modus „Bild“ (M9-01). Mit dem Schalter „Bild erzeugen“ geht die
// Nachricht als Bildbeschreibung direkt an das Bildmodell (Request mit
// "image": {format, quality}); Format und Qualität merkt sich der Browser.
// Ohne Modus: Kann das gewählte Chatmodell keine Werkzeuge und klingt die
// Nachricht wie ein Bildauftrag, zeigt ein Hinweis vor dem Senden den Knopf
// „Mit Bildmodell erzeugen“ – nie ein Umleiten ohne Zustimmung. Die Muster
// kommen vom Server (images.REQUEST_PATTERNS). chat.js fragt über
// MultiGPT.imageMode {beforeSend, extend, label, isActive}. Kein Inline-JS.
"use strict";

(() => {
  const MultiGPT = (window.MultiGPT = window.MultiGPT || {});
  const STORAGE_KEY = "multigpt.imageMode";

  function storageGet() {
    try {
      return JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "{}") || {};
    } catch {
      return {};
    }
  }

  function storageSet(value) {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
    } catch {
      // Privater Modus o. Ä.: gilt dann nur bis zum Neuladen.
    }
  }

  function compile(source) {
    try {
      return new RegExp(source, "i");
    } catch {
      return null;
    }
  }

  // Muster aus dem Template (json_script); fehlen sie, gibt es keinen Hinweis.
  function loadPatterns() {
    const el = document.getElementById("image-request-patterns");
    let data = {};
    try {
      data = JSON.parse(el?.textContent || "{}") || {};
    } catch {
      data = {};
    }
    const request = Array.isArray(data.request) ? data.request.map(compile).filter(Boolean) : [];
    return { request, code: typeof data.code === "string" ? compile(data.code) : null };
  }

  // Klingt der Text wie ein Bildauftrag (und nicht ausdrücklich nach Code)?
  function looksLikeImageRequest(text, patterns) {
    const value = String(text || "").trim();
    if (!value || value.length > 2000) {
      return false;
    }
    if (patterns.code && patterns.code.test(value)) {
      return false;
    }
    return patterns.request.some((re) => re.test(value));
  }

  MultiGPT.looksLikeImageRequest = (text) => looksLikeImageRequest(text, loadPatterns());

  document.addEventListener("DOMContentLoaded", () => {
    const box = document.getElementById("image-mode");
    const toggle = document.getElementById("image-mode-toggle");
    const options = document.getElementById("image-mode-options");
    const format = document.getElementById("image-mode-format");
    const quality = document.getElementById("image-mode-quality");
    const textarea = document.getElementById("message-input");
    const modelSelect = document.getElementById("model-select");
    const field = document.querySelector("#chat-form .chat-input-field");
    if (!box || !toggle || !format || !quality || !textarea || !field) {
      return; // kein Bildmodell nutzbar bzw. nur lesen
    }
    const modelName = box.dataset.modelName || "";
    const patterns = loadPatterns();
    const placeholder = textarea.placeholder;
    let dismissedFor = null; // Text, für den der Hinweis weggeklickt wurde

    // Hinweis über dem Eingabefeld (role=status, damit er vorgelesen wird).
    const hint = document.createElement("div");
    hint.className = "image-request-hint";
    hint.id = "image-request-hint";
    hint.setAttribute("role", "status");
    hint.hidden = true;
    const hintText = document.createElement("p");
    hintText.textContent =
      "Das gewählte Modell kann keine Bilder erzeugen und würde höchstens Code (z. B. SVG) " +
      `schreiben. Mit dem Bildmodell ${modelName} entsteht ein echtes Bild.`;
    const useButton = document.createElement("button");
    useButton.type = "button";
    useButton.className = "button button-small button-primary";
    useButton.textContent = "Mit Bildmodell erzeugen";
    const ignoreButton = document.createElement("button");
    ignoreButton.type = "button";
    ignoreButton.className = "button button-small";
    ignoreButton.textContent = "Trotzdem an das Chatmodell";
    const actions = document.createElement("div");
    actions.className = "image-request-hint-actions";
    actions.append(useButton, ignoreButton);
    hint.append(hintText, actions);
    field.before(hint);

    const saved = storageGet();
    if (saved.format && format.querySelector(`option[value="${CSS.escape(saved.format)}"]`)) {
      format.value = saved.format;
    }
    if (saved.quality && quality.querySelector(`option[value="${CSS.escape(saved.quality)}"]`)) {
      quality.value = saved.quality;
    }

    function chatModelHasTools() {
      const option = modelSelect?.selectedOptions?.[0];
      return option?.dataset.tools === "true";
    }

    // Hinweis nur ohne Modus, bei Modellen ohne Werkzeuge und passendem Text.
    function wantsHint(text) {
      return (
        !toggle.checked &&
        !chatModelHasTools() &&
        text.trim() !== dismissedFor &&
        looksLikeImageRequest(text, patterns)
      );
    }

    function updateHint() {
      hint.hidden = !wantsHint(textarea.value);
    }

    function setActive(on) {
      toggle.checked = on;
      options.hidden = !on;
      box.classList.toggle("is-active", on);
      textarea.placeholder = on ? "Bild beschreiben …" : placeholder;
      updateHint();
    }

    toggle.addEventListener("change", () => setActive(toggle.checked));
    for (const select of [format, quality]) {
      select.addEventListener("change", () =>
        storageSet({ format: format.value, quality: quality.value }),
      );
    }
    textarea.addEventListener("input", updateHint);
    modelSelect?.addEventListener("change", updateHint);
    document.addEventListener("multigpt:models-loaded", updateHint);
    useButton.addEventListener("click", () => {
      setActive(true);
      textarea.focus();
      MultiGPT.chatView?.setStatus(
        `Modus „Bild“ ist an: Die Nachricht geht an ${modelName}. Format und Qualität oben wählen.`,
      );
    });
    ignoreButton.addEventListener("click", () => {
      dismissedFor = textarea.value.trim();
      hint.hidden = true;
      textarea.focus();
    });

    MultiGPT.imageMode = {
      isActive: () => toggle.checked,
      // false = nicht senden (Hinweis zeigen bzw. Anhänge im Modus „Bild“).
      beforeSend(text) {
        if (toggle.checked) {
          if ((MultiGPT.attachments?.count() ?? 0) > 0) {
            MultiGPT.chatView?.setStatus(
              "Im Modus „Bild“ sind noch keine Anhänge möglich. Bitte die Anhänge entfernen.",
              true,
            );
            return false;
          }
          return true;
        }
        if (wantsHint(text)) {
          hint.hidden = false;
          useButton.focus();
          MultiGPT.chatView?.setStatus(
            "Bildwunsch erkannt: Bitte wählen, ob das Bildmodell das Bild erzeugen soll.",
          );
          return false;
        }
        return true;
      },
      extend(payload) {
        if (!toggle.checked) {
          return payload;
        }
        // Nur Text, Ende des Zweigs und Bildoptionen; Werkzeuge usw. gelten hier nicht.
        const result = { content: payload.content, model: payload.model };
        if (payload.leaf) {
          result.leaf = payload.leaf;
        }
        result.image = { format: format.value, quality: quality.value };
        return result;
      },
      label: () => (toggle.checked ? modelName : ""),
    };
    setActive(false);
  });
})();
