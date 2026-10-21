// MultiGPT – Modus „Bild“ (M9-01). Mit dem Schalter „Bild erzeugen“ geht die
// Nachricht als Bildbeschreibung direkt an das Bildmodell (Request mit
// "image": {format, quality}); Format und Qualität merkt sich der Browser.
// Ohne Modus: Klingt die Nachricht wie ein Bildauftrag, zeigt ein Hinweis vor dem
// Senden den Knopf „Mit Bildmodell erzeugen“ – nie ein Umleiten ohne Zustimmung.
// Auch bei Modellen mit Werkzeugen: kleine Modelle rufen „generate_image“ nicht
// zuverlässig auf (Prod-Chat 31: Mistral 7B antwortete „kann keine Bilder“). Die Muster
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
    return {
      request,
      code: typeof data.code === "string" ? compile(data.code) : null,
      refusal: typeof data.refusal === "string" ? compile(data.refusal) : null,
      refusalImage: typeof data.refusalImage === "string" ? compile(data.refusalImage) : null,
      refusalHead: Number.isInteger(data.refusalHead) ? data.refusalHead : 400,
    };
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
    // Mehrere Bildmodelle: Auswahl „Modell“; sonst fest das eine (data-model-name).
    const imageModel = document.getElementById("image-mode-model");
    const keepWorking = document.getElementById("image-mode-continue");
    const resolutionBox = document.getElementById("image-mode-resolution-box");
    const resolution = document.getElementById("image-mode-resolution");
    const customSize = document.getElementById("image-mode-custom-size");
    const widthInput = document.getElementById("image-mode-width");
    const heightInput = document.getElementById("image-mode-height");
    const currentName = () =>
      imageModel?.selectedOptions?.[0]?.dataset.name || box.dataset.modelName || "";
    // Nimmt das gewählte Bildmodell beliebige Größen (data-size)? Nur dann gibt es „Auflösung“.
    const canResize = () =>
      imageModel
        ? imageModel.selectedOptions?.[0]?.dataset.size === "true"
        : box.dataset.size === "true";
    // Gewünschte Größe "BxH" oder "" (Format gilt); null = ungültige eigene Angabe.
    function chosenSize() {
      if (!resolutionBox || !canResize() || !resolution.value) {
        return "";
      }
      if (resolution.value !== "custom") {
        return resolution.value;
      }
      const w = Number(widthInput.value);
      const h = Number(heightInput.value);
      const lo = Number(widthInput.min);
      const hi = Number(widthInput.max);
      const ok = (v) => Number.isInteger(v) && v >= lo && v <= hi;
      return ok(w) && ok(h) ? `${w}x${h}` : null;
    }
    // Wertet der Anbieter des gewählten Modells die Qualität aus (data-quality)? Sonst kein Feld.
    const canQuality = () =>
      imageModel
        ? imageModel.selectedOptions?.[0]?.dataset.quality === "true"
        : box.dataset.quality !== "false";
    const qualityField = document.getElementById("image-mode-quality-field");
    // Wie viele Bilder (Varianten) das gewählte Modell je Anfrage liefert (data-variants).
    const countField = document.getElementById("image-mode-count-field");
    const countSelect = document.getElementById("image-mode-count");
    const maxVariants = () =>
      Number(
        (imageModel ? imageModel.selectedOptions?.[0]?.dataset.variants : box.dataset.variants) ||
          1,
      );
    function updateCount() {
      if (!countField || !countSelect) {
        return;
      }
      const max = maxVariants();
      countField.hidden = max <= 1;
      for (const option of countSelect.options) {
        option.disabled = Number(option.value) > max;
      }
      if (Number(countSelect.value) > max) {
        countSelect.value = String(max);
      }
    }
    function updateQuality() {
      if (qualityField) {
        qualityField.hidden = !canQuality();
      }
    }
    function updateResolution() {
      if (!resolutionBox) {
        return;
      }
      resolutionBox.hidden = !canResize();
      customSize.hidden = resolution.value !== "custom";
      format.disabled = canResize() && Boolean(resolution.value); // die Auflösung ersetzt das Format
    }
    // Kann das gewählte Bildmodell Bilder bearbeiten (data-edit am Eintrag bzw. am Kasten)?
    // Maske (data-mask): Bereich zum Ändern malen, nur bei Modellen mit Maske und angehängtem Bild.
    const canMask = () =>
      imageModel
        ? imageModel.selectedOptions?.[0]?.dataset.mask === "true"
        : box.dataset.mask === "true";
    const canEdit = () =>
      imageModel
        ? imageModel.selectedOptions?.[0]?.dataset.edit === "true"
        : box.dataset.edit === "true";
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
    // Text je nach Chatmodell: ohne Werkzeuge kann es gar keine Bilder, mit Werkzeugen ruft es
    // das Bildwerkzeug nicht immer auf.
    function hintMessage() {
      const name = currentName();
      return chatModelHasTools()
        ? "Das gewählte Modell kann Bilder nur über ein Werkzeug erzeugen und ruft es nicht " +
            `immer auf. Mit dem Bildmodell ${name} entsteht sicher ein echtes Bild.`
        : "Das gewählte Modell kann keine Bilder erzeugen und würde höchstens Code (z. B. SVG) " +
            `schreiben. Mit dem Bildmodell ${name} entsteht ein echtes Bild.`;
    }
    hintText.textContent = hintMessage();
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

    // Hinweis nur ohne Modus und bei passendem Text (egal, ob das Chatmodell Werkzeuge hat).
    function wantsHint(text) {
      return (
        !toggle.checked && text.trim() !== dismissedFor && looksLikeImageRequest(text, patterns)
      );
    }

    function updateHint() {
      hint.hidden = !wantsHint(textarea.value);
      if (!hint.hidden) {
        hintText.textContent = hintMessage();
      }
    }

    function setActive(on) {
      toggle.checked = on;
      options.hidden = !on;
      box.classList.toggle("is-active", on);
      textarea.placeholder = on ? "Bild beschreiben …" : placeholder;
      document.dispatchEvent(new CustomEvent("multigpt:image-mode")); // Zusammenfassung (input_options.js)
      updateHint();
    }

    toggle.addEventListener("change", () => setActive(toggle.checked));
    if (
      imageModel &&
      saved.model &&
      imageModel.querySelector(`option[value="${CSS.escape(String(saved.model))}"]`)
    ) {
      imageModel.value = String(saved.model);
    }
    if (resolution && saved.resolution) {
      const known = resolution.querySelector(`option[value="${CSS.escape(String(saved.resolution))}"]`);
      if (known) {
        resolution.value = String(saved.resolution);
      }
    }
    if (widthInput && saved.width) {
      widthInput.value = String(saved.width);
    }
    if (heightInput && saved.height) {
      heightInput.value = String(saved.height);
    }
    if (keepWorking && saved.continue === false) {
      keepWorking.checked = false; // gemerkt: ausdrücklich ausgeschaltet
    }
    const remember = () =>
      storageSet({
        format: format.value,
        quality: quality.value,
        model: imageModel?.value,
        continue: keepWorking ? keepWorking.checked : true,
        resolution: resolution?.value,
        width: widthInput?.value,
        height: heightInput?.value,
      });
    for (const select of [format, quality, imageModel, keepWorking, resolution].filter(Boolean)) {
      select.addEventListener("change", remember);
      select.addEventListener("change", updateResolution);
      select.addEventListener("change", updateQuality);
      select.addEventListener("change", updateCount);
    }
    for (const input of [widthInput, heightInput].filter(Boolean)) {
      input.addEventListener("change", remember);
    }
    updateResolution();
    updateQuality();
    updateCount();
    const mask = setupMask({ canMask, imageModel });
    textarea.addEventListener("input", updateHint);
    modelSelect?.addEventListener("change", updateHint);
    document.addEventListener("multigpt:models-loaded", updateHint);
    useButton.addEventListener("click", () => {
      setActive(true);
      textarea.focus();
      MultiGPT.chatView?.setStatus(
        `Modus „Bild“ ist an: Die Nachricht geht an ${currentName()}. Format und Qualität oben wählen.`,
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
          if (chosenSize() === null) {
            MultiGPT.chatView?.setStatus(
              `Eigene Auflösung: Breite und Höhe von ${widthInput.min} bis ${widthInput.max} Pixel angeben.`,
              true,
            );
            return false;
          }
          // Mit angehängten Bildern wird bearbeitet: nur mit einem Modell, das das kann.
          if ((MultiGPT.attachments?.count() ?? 0) > 0 && !canEdit()) {
            MultiGPT.chatView?.setStatus(
              "Dieses Bildmodell kann Bilder nicht bearbeiten. Bitte ein anderes Modell wählen " +
                "oder die Anhänge entfernen.",
              true,
            );
            return false;
          }
          return true;
        }
        if (wantsHint(text)) {
          hint.hidden = false;
          hintText.textContent = hintMessage();
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
        if (Array.isArray(payload.attachments) && payload.attachments.length) {
          result.attachments = payload.attachments;
        }
        result.image = {
          format: format.value,
          quality: canQuality() ? quality.value : "auto",
        };
        const size = chosenSize();
        if (size) {
          result.image.size = size;
        }
        if (countSelect && maxVariants() > 1 && Number(countSelect.value) > 1) {
          result.image.n = Number(countSelect.value);
        }
        const maskId = mask.take();
        if (maskId) {
          result.image.mask = maskId;
        }
        if (imageModel?.value) {
          result.image.model = Number(imageModel.value);
        }
        if (keepWorking) {
          result.image.continue = keepWorking.checked;
        }
        return result;
      },
      label: () => (toggle.checked ? currentName() : ""),
      // Modus einschalten, ohne zu senden (tool_text.js: Aufruf als Text).
      activate: () => setActive(true),
    };
    setActive(false);
    watchRefusals(textarea);
    watchContinueButtons(textarea);
  });

  // --- Maske zeichnen (nur Modelle mit Maske, z. B. GPT Image) --------------------------------
  // Auf dem ersten angehängten Bild den Bereich übermalen, der geändert werden soll. Daraus wird
  // ein PNG in der Größe des Bildes: deckend, der bemalte Bereich durchsichtig (so erwartet es
  // die API). Es geht als Entwurf an den Server (Upload) und mit der Nachricht als image.mask.
  function setupMask({ canMask, imageModel }) {
    const boxEl = document.getElementById("image-mode-mask-box");
    const openButton = document.getElementById("image-mode-mask-open");
    const stateEl = document.getElementById("image-mode-mask-state");
    const clearButton = document.getElementById("image-mode-mask-clear");
    const dialog = document.getElementById("mask-dialog");
    const view = document.getElementById("mask-canvas");
    const brush = document.getElementById("mask-brush");
    const none = { take: () => 0 };
    if (!boxEl || !openButton || !dialog || !view || typeof dialog.showModal !== "function") {
      return none;
    }
    const chat = document.getElementById("chat");
    const base = chat?.dataset.apiAttachments || "";
    let maskId = 0; // hochgeladene Maske (Entwurf)
    let maskFor = 0; // Bild, zu dem sie gehört
    let source = null; // geladenes Bild im Dialog
    let paint = null; // Strichebene in Bildgröße
    let drawing = false;
    let last = null;

    const setStatus = (text, bad = false) => MultiGPT.chatView?.setStatus?.(text, bad);
    const firstImage = () => MultiGPT.attachments?.firstImage?.() || null;

    function discardUpload() {
      if (maskId && base) {
        fetch(`${base}${maskId}/`, {
          method: "DELETE",
          credentials: "same-origin",
          headers: { "X-CSRFToken": MultiGPT.csrfToken(), Accept: "application/json" },
        }).catch(() => {});
      }
      maskId = 0;
      maskFor = 0;
    }

    function update() {
      const image = firstImage();
      boxEl.hidden = !(canMask() && image);
      if (maskFor && (!image || image.id !== maskFor)) {
        discardUpload(); // anderes Bild: die Maske passt nicht mehr
      }
      stateEl.hidden = !maskId;
      openButton.hidden = Boolean(maskId);
    }

    function redraw() {
      const ctx = view.getContext("2d");
      ctx.clearRect(0, 0, view.width, view.height);
      ctx.drawImage(source, 0, 0);
      ctx.globalAlpha = 0.55;
      ctx.drawImage(paint, 0, 0);
      ctx.globalAlpha = 1;
    }

    function point(event) {
      const rect = view.getBoundingClientRect();
      return {
        x: ((event.clientX - rect.left) / rect.width) * view.width,
        y: ((event.clientY - rect.top) / rect.height) * view.height,
      };
    }

    function stroke(from, to) {
      const ctx = paint.getContext("2d");
      const rect = view.getBoundingClientRect();
      const scale = view.width / rect.width; // Pinselgröße gilt in Bildschirmpixeln
      ctx.strokeStyle = "#e11d48";
      ctx.fillStyle = "#e11d48";
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      ctx.lineWidth = Number(brush.value) * scale;
      ctx.beginPath();
      ctx.moveTo(from.x, from.y);
      ctx.lineTo(to.x, to.y);
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(to.x, to.y, ctx.lineWidth / 2, 0, Math.PI * 2);
      ctx.fill();
      redraw();
    }

    view.addEventListener("pointerdown", (event) => {
      if (!source) {
        return;
      }
      drawing = true;
      view.setPointerCapture(event.pointerId);
      last = point(event);
      stroke(last, last);
      event.preventDefault();
    });
    view.addEventListener("pointermove", (event) => {
      if (!drawing) {
        return;
      }
      const now = point(event);
      stroke(last, now);
      last = now;
    });
    for (const name of ["pointerup", "pointercancel"]) {
      view.addEventListener(name, () => {
        drawing = false;
      });
    }

    document.getElementById("mask-reset")?.addEventListener("click", () => {
      if (source) {
        paint.getContext("2d").clearRect(0, 0, paint.width, paint.height);
        redraw();
      }
    });
    document.getElementById("mask-cancel")?.addEventListener("click", () => dialog.close());

    openButton.addEventListener("click", () => {
      const image = firstImage();
      if (!image) {
        return;
      }
      const picture = new Image();
      picture.onload = () => {
        source = picture;
        view.width = picture.naturalWidth;
        view.height = picture.naturalHeight;
        paint = document.createElement("canvas");
        paint.width = view.width;
        paint.height = view.height;
        redraw();
        dialog.showModal();
      };
      picture.onerror = () => setStatus("Das Bild lässt sich nicht laden.", true);
      picture.src = image.url;
    });

    document.getElementById("mask-apply")?.addEventListener("click", async () => {
      const image = firstImage();
      if (!source || !paint || !image) {
        return;
      }
      // Maske: deckend weiß, bemalter Bereich ausgestanzt (durchsichtig).
      const out = document.createElement("canvas");
      out.width = paint.width;
      out.height = paint.height;
      const ctx = out.getContext("2d");
      ctx.fillStyle = "#fff";
      ctx.fillRect(0, 0, out.width, out.height);
      ctx.globalCompositeOperation = "destination-out";
      ctx.drawImage(paint, 0, 0);
      const blob = await new Promise((resolve) => out.toBlob(resolve, "image/png"));
      if (!blob) {
        setStatus("Die Maske konnte nicht erzeugt werden.", true);
        return;
      }
      const form = new FormData();
      form.append("file", blob, "maske.png");
      const conversation = chat?.dataset.conversationId;
      if (conversation) {
        form.append("conversation", conversation);
      }
      try {
        const response = await fetch(base, {
          method: "POST",
          body: form,
          credentials: "same-origin",
          headers: { "X-CSRFToken": MultiGPT.csrfToken(), Accept: "application/json" },
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(data.error || "Die Maske konnte nicht hochgeladen werden.");
        }
        discardUpload();
        maskId = data.id;
        maskFor = image.id;
        dialog.close();
        setStatus("Der markierte Bereich wird beim Senden geändert, der Rest bleibt.");
      } catch (error) {
        setStatus(error.message || "Die Maske konnte nicht hochgeladen werden.", true);
      }
      update();
    });

    clearButton?.addEventListener("click", () => {
      discardUpload();
      update();
      setStatus("Markierung entfernt.");
    });

    document.addEventListener("multigpt:attachments-changed", update);
    document.addEventListener("multigpt:image-mode", update);
    imageModel?.addEventListener("change", update);
    update();
    return {
      // Maske dieser Nachricht (der Server löscht den Entwurf nach dem Senden); danach leer.
      take() {
        if (!maskId || !canMask()) {
          return 0;
        }
        const id = maskId;
        maskId = 0;
        maskFor = 0;
        update();
        return id;
      },
    };
  }

  // --- „Weiter bearbeiten“ an einem erzeugten Bild ---------------------------------------
  // Kopiert das Bild als eigenen Entwurf (POST /api/attachments/<id>/copy/), legt es in die
  // Anhangsleiste und schaltet den Modus „Bild“ ein. Gesendet wird erst, wenn der Nutzer sendet.
  function watchContinueButtons(textarea) {
    document.addEventListener("click", async (event) => {
      const button = event.target instanceof Element && event.target.closest("[data-attachment-continue]");
      if (!button || button.disabled) {
        return;
      }
      const setStatus = MultiGPT.chatView?.setStatus || (() => {});
      const base = document.getElementById("chat")?.dataset.apiAttachments;
      if (!base || !MultiGPT.attachments?.addDraft) {
        return;
      }
      button.disabled = true;
      try {
        const response = await fetch(`${base}${encodeURIComponent(button.dataset.attachmentContinue)}/copy/`, {
          method: "POST",
          credentials: "same-origin",
          headers: { "X-CSRFToken": MultiGPT.csrfToken(), Accept: "application/json" },
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(data.error || "Das Bild konnte nicht übernommen werden.");
        }
        MultiGPT.attachments.addDraft(data);
        MultiGPT.imageMode?.activate();
        textarea.focus({ preventScroll: true });
        setStatus("Das Bild liegt als Vorlage im Eingabefeld, der Modus „Bild“ ist an. Beschreibe die Änderung und sende.");
      } catch (error) {
        setStatus(error.message || "Das Bild konnte nicht übernommen werden.", true);
      } finally {
        button.disabled = false;
      }
    });
  }

  // --- Knopf unter einer Antwort, die einen Bildwunsch abgelehnt hat --------------------
  // Antwortet ein Chatmodell auf einen Bildauftrag mit „kann keine Bilder erzeugen“ (Prod-Chat
  // 32: Qwen3-VL), bietet die Oberfläche unter der Antwort „Mit Bildmodell erzeugen“ an. Der
  // Knopf belegt nur vor (Modus „Bild“ an, die Frage im Eingabefeld); gesendet wird erst,
  // wenn der Nutzer sendet. Der Text der Frage kommt aus dem Verlauf und geht nur als Wert
  // ins Eingabefeld, nie als HTML. Muster müssen nur grob stimmen: Es bleibt ein Hinweis.
  const MAX_PROMPT = 4000;

  // Muster kommen vom Server (images.REFUSAL_PATTERN, in Python getestet).
  function refusedImage(text, patterns) {
    if (!patterns.refusal || !patterns.refusalImage) {
      return false;
    }
    const head = String(text || "").slice(0, patterns.refusalHead);
    return patterns.refusal.test(head) && patterns.refusalImage.test(head);
  }

  function questionOf(article) {
    for (let el = article.previousElementSibling; el; el = el.previousElementSibling) {
      if (el.dataset?.role === "user") {
        return el.querySelector(".chat-message-content")?.textContent?.trim() || "";
      }
    }
    return "";
  }

  function addRefusalHint(article, textarea, patterns) {
    if (article.dataset.imageRefusal || article.dataset.role !== "assistant") {
      return;
    }
    if (article.dataset.status !== "complete" || article.querySelector(".message-attachments")) {
      return; // läuft noch, oder die Antwort hat schon ein Bild
    }
    const content = article.querySelector(".chat-message-content");
    const question = questionOf(article);
    if (!content || !question || question.length > MAX_PROMPT) {
      return;
    }
    article.dataset.imageRefusal = "checked";
    if (!refusedImage(content.textContent, patterns)) {
      return;
    }
    const box = document.createElement("p");
    box.className = "image-refusal-hint";
    const label = document.createElement("span");
    label.textContent = "Dieses Modell erzeugt keine Bilder.";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button button-small button-primary";
    button.textContent = "Mit Bildmodell erzeugen";
    button.addEventListener("click", () => {
      const setStatus = MultiGPT.chatView?.setStatus || (() => {});
      if (textarea.value.trim() && textarea.value.trim() !== question) {
        setStatus("Im Eingabefeld steht schon Text. Bitte zuerst leeren, dann erneut klicken.", true);
        return;
      }
      MultiGPT.imageMode?.activate();
      textarea.value = question;
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
      textarea.focus({ preventScroll: true });
      setStatus("Modus „Bild“ ist an, deine Frage steht im Eingabefeld. Zum Erzeugen senden.");
    });
    box.append(label, " ", button);
    content.after(box);
  }

  function watchRefusals(textarea) {
    const patterns = loadPatterns();
    const log = document.getElementById("chat-log");
    if (!log) {
      return;
    }
    let pending = 0;
    const scan = () => {
      pending = 0;
      for (const article of log.querySelectorAll(".chat-message-assistant")) {
        addRefusalHint(article, textarea, patterns);
      }
    };
    scan();
    new MutationObserver(() => {
      if (!pending) {
        pending = window.setTimeout(scan, 300); // erst, wenn der Stream die Antwort abgeschlossen hat
      }
    }).observe(log, { childList: true, subtree: true, attributes: true, attributeFilter: ["data-status"] });
  }
})();
