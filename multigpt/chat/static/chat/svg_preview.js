// MultiGPT – Bildvorschau für SVG-Codeblöcke in Antworten (M5-01).
//
// markdown.js ruft nach dem Rendern decorate(el, text, final) auf. Codeblöcke
// mit der Sprache svg (oder xml, wenn der Inhalt mit <svg beginnt) bekommen in
// der Kopfzeile die Umschalter „Vorschau“/„Code“ und „Als Datei speichern“.
//
// Sicherheit: Das SVG stammt vom Modell und ist nicht vertrauenswürdig.
// - Dargestellt wird AUSSCHLIESSLICH als <img src="data:image/svg+xml,…">. Im
//   Bildmodus führen Firefox und Chromium keine Skripte aus, werten keine
//   Ereignisse aus und laden keine externen Ressourcen (Bilder, <use>, CSS,
//   Schriften); nur data:-Ressourcen werden aufgelöst. Nie als Inline-SVG ins DOM.
// - Vorher: Wohlgeformtheit prüfen (DOMParser, image/svg+xml), mit DOMPurify im
//   SVG-Profil bereinigen (ohne <script>, on*-Attribute, <foreignObject>),
//   href/xlink:href nur auf "#…" bzw. eingebettete Rasterbilder, url() in CSS
//   nur auf "#…", kein @import.
// - Größe begrenzt (MAX_BYTES); darüber nur Code mit Hinweis.
// - Der Download geht über eine Blob-URL mit dem bereinigten SVG.
"use strict";

(() => {
  const MAX_BYTES = 512 * 1024;
  const SVG_NS = "http://www.w3.org/2000/svg";
  const SAFE_HREF = /^\s*(?:#|data:image\/(?:png|jpe?g|gif|webp);)/i;
  const CSS_URL = /url\(\s*(['"]?)(?!\s*#)[^)]*\)/gi;
  const CSS_IMPORT = /@import[^;]*;?/gi;
  const START = /^\s*(?:<\?xml[^>]*\?>\s*)?(?:<!--[\s\S]*?-->\s*)*<svg[\s>]/i;

  // Ergebnis je Quelltext, damit der Stream nicht bei jedem Bild neu bereinigt.
  const cache = new Map();
  // Vom Nutzer gewählte Ansicht je Antwort und Block (überlebt neues Rendern).
  const chosen = new WeakMap();

  function isSvgBlock(lang, text) {
    lang = (lang || "").toLowerCase();
    if (lang === "svg") {
      return true;
    }
    return lang === "xml" && START.test(text);
  }

  function cleanCss(text) {
    return text.replace(CSS_IMPORT, "").replace(CSS_URL, "none");
  }

  // Liefert {svg, url} oder {error}.
  function prepare(text) {
    if (cache.has(text)) {
      return cache.get(text);
    }
    let result;
    if (new TextEncoder().encode(text).length > MAX_BYTES) {
      result = { error: "Vorschau nicht möglich: Das SVG ist größer als 512 KB." };
    } else {
      result = sanitize(text);
    }
    if (cache.size > 20) {
      cache.delete(cache.keys().next().value);
    }
    cache.set(text, result);
    return result;
  }

  function sanitize(text) {
    const failed = { error: "Vorschau nicht möglich: Das SVG ist fehlerhaft." };
    // 1. Wohlgeformt und Wurzel <svg> im SVG-Namensraum?
    const parsed = new DOMParser().parseFromString(text, "image/svg+xml");
    const top = parsed.documentElement;
    if (
      !top ||
      parsed.getElementsByTagName("parsererror").length ||
      top.namespaceURI !== SVG_NS ||
      top.localName !== "svg"
    ) {
      return failed;
    }
    // 2. DOMPurify im SVG-Profil; <use> zusätzlich, Ziele prüft Schritt 3.
    const fragment = window.DOMPurify.sanitize(text, {
      USE_PROFILES: { svg: true, svgFilters: true },
      ADD_TAGS: ["use"],
      FORBID_TAGS: ["foreignObject", "foreignobject", "script", "a"],
      ALLOW_DATA_ATTR: false,
      RETURN_DOM_FRAGMENT: true,
    });
    const svg = fragment.querySelector("svg");
    if (!svg) {
      return failed;
    }
    // 3. Externe Verweise und CSS-url() entfernen.
    for (const node of [svg, ...svg.querySelectorAll("*")]) {
      for (const attr of Array.from(node.attributes)) {
        const name = attr.localName.toLowerCase();
        if (name.startsWith("on")) {
          node.removeAttributeNode(attr);
        } else if (name === "href" || name === "src") {
          if (!SAFE_HREF.test(attr.value) || (node.localName === "use" && !attr.value.trim().startsWith("#"))) {
            node.removeAttributeNode(attr);
          }
        } else if (/url\(|@import/i.test(attr.value)) {
          attr.value = cleanCss(attr.value);
        }
      }
      if (node.localName === "style") {
        node.textContent = cleanCss(node.textContent);
      }
    }
    // Ohne feste Breite hat das Bild keine eigene Größe (Firefox: 0×0): dann
    // Containerbreite, mit viewBox im richtigen Seitenverhältnis.
    const width = svg.getAttribute("width") || "";
    const fluid = !width || width.trim().endsWith("%");
    if (fluid && !svg.hasAttribute("viewBox")) {
      svg.setAttribute("width", "300");
      svg.setAttribute("height", "150");
    }
    // XMLSerializer setzt die Namensräume (xmlns, xmlns:xlink) für den Bildmodus.
    const out = new XMLSerializer().serializeToString(svg);
    if (!out.includes(`xmlns="${SVG_NS}"`)) {
      return failed;
    }
    return { svg: out, fluid: fluid && svg.hasAttribute("viewBox"), url: `data:image/svg+xml;charset=utf-8,${encodeURIComponent(out)}` };
  }

  // --- Vollbild ----------------------------------------------------------------------
  // Eigener <dialog> im Aussehen der Anhänge-Lightbox (attachments.js hält ihre
  // Lightbox privat); gleiche CSS-Klassen.

  let dialog = null;
  let opener = null;

  function showLarge(url, title, from) {
    if (!dialog) {
      dialog = document.createElement("dialog");
      dialog.className = "attach-lightbox svg-lightbox";
      dialog.setAttribute("aria-labelledby", "svg-lightbox-title");
      const head = document.createElement("div");
      head.className = "attach-lightbox-head";
      const heading = document.createElement("p");
      heading.className = "attach-lightbox-title";
      heading.id = "svg-lightbox-title";
      const close = document.createElement("button");
      close.type = "button";
      close.className = "button button-small attach-lightbox-close";
      close.textContent = "Schließen";
      close.addEventListener("click", () => dialog.close());
      head.append(heading, close);
      const figure = document.createElement("figure");
      figure.className = "attach-lightbox-figure";
      const img = document.createElement("img");
      img.className = "attach-lightbox-image svg-preview-image";
      img.decoding = "async";
      figure.append(img);
      dialog.append(head, figure);
      dialog.addEventListener("click", (event) => {
        if (event.target === dialog || event.target === figure) {
          dialog.close();
        }
      });
      dialog.addEventListener("close", () => {
        img.removeAttribute("src");
        opener?.focus({ preventScroll: true });
        opener = null;
      });
      document.body.append(dialog);
    }
    const img = dialog.querySelector("img");
    img.src = url;
    img.alt = title;
    dialog.querySelector(".attach-lightbox-title").textContent = title;
    opener = from;
    dialog.showModal();
    dialog.querySelector(".attach-lightbox-close").focus();
  }

  // --- Codeblock erweitern -----------------------------------------------------------

  // Ist der n-te Codeblock des Textes noch offen (Stream)? Nur der letzte kann es sein.
  function openBlockIndex(text) {
    const blocks = [];
    try {
      window.marked.walkTokens(window.marked.lexer(text), (token) => {
        if (token.type === "code") {
          blocks.push(token.raw);
        }
      });
    } catch {
      return -1;
    }
    const last = blocks.at(-1);
    if (last === undefined || !/^\s*(`{3,}|~{3,})/.test(last)) {
      return -1;
    }
    const lines = last.trimEnd().split("\n");
    const closed = lines.length > 1 && /^\s*(`{3,}|~{3,})\s*$/.test(lines.at(-1));
    return closed ? -1 : blocks.length - 1;
  }

  function button(cls, text) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `code-copy ${cls}`;
    b.textContent = text;
    return b;
  }

  function enhance(wrapper, code, complete, modes, index) {
    const bar = wrapper.querySelector(".code-block-bar");
    const pre = wrapper.querySelector("pre");
    const copy = bar?.querySelector(".code-copy");
    if (!bar || !pre || !copy) {
      return;
    }
    wrapper.classList.add("svg-block");
    if (!complete) {
      // Während des Streams: Code, bis der Block geschlossen ist.
      return;
    }
    const text = code.textContent;
    const result = prepare(text);

    const tools = document.createElement("div");
    tools.className = "code-block-tools";
    const group = document.createElement("div");
    group.className = "svg-view-switch";
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", "Ansicht");
    const toPreview = button("svg-view", "Vorschau");
    const toCode = button("svg-view", "Code");
    group.append(toPreview, toCode);
    const save = button("svg-save", "Als Datei speichern");
    save.setAttribute("aria-label", "SVG als Datei speichern");
    copy.replaceWith(tools);
    tools.append(group, save, copy);

    const view = document.createElement("div");
    view.className = "svg-preview";
    if (result.error) {
      const note = document.createElement("p");
      note.className = "svg-preview-error";
      note.textContent = result.error;
      wrapper.insertBefore(note, pre);
      toPreview.disabled = true;
      save.disabled = true;
      toPreview.setAttribute("aria-pressed", "false");
      toCode.setAttribute("aria-pressed", "true");
      return;
    }
    const open = document.createElement("button");
    open.type = "button";
    open.className = "svg-preview-open";
    open.setAttribute("aria-label", "Vorschau vergrößern");
    const img = document.createElement("img");
    img.className = "svg-preview-image";
    img.classList.toggle("is-fluid", result.fluid);
    open.classList.toggle("is-fluid", result.fluid);
    img.alt = "Vorschau der SVG-Grafik";
    img.decoding = "async";
    img.addEventListener("error", () => {
      // Der Browser konnte das bereinigte SVG nicht zeichnen.
      view.replaceChildren();
      const note = document.createElement("p");
      note.className = "svg-preview-error";
      note.textContent = "Vorschau nicht möglich.";
      wrapper.insertBefore(note, view);
      toPreview.disabled = true;
      setMode("code", false);
    });
    img.src = result.url;
    open.append(img);
    open.addEventListener("click", () => showLarge(result.url, "SVG-Grafik", open));
    view.append(open);
    wrapper.insertBefore(view, pre);

    function setMode(mode, remember = true) {
      const preview = mode === "preview";
      view.hidden = !preview;
      pre.hidden = preview;
      toPreview.setAttribute("aria-pressed", String(preview));
      toCode.setAttribute("aria-pressed", String(!preview));
      if (remember) {
        modes.set(index, mode);
      }
    }
    toPreview.addEventListener("click", () => setMode("preview"));
    toCode.addEventListener("click", () => setMode("code"));
    save.addEventListener("click", () => {
      const url = URL.createObjectURL(new Blob([result.svg], { type: "image/svg+xml" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = "bild.svg";
      link.hidden = true;
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 10000);
    });
    setMode(modes.get(index) || "preview", false);
  }

  function decorate(el, text, final) {
    if (!window.DOMPurify?.isSupported) {
      return;
    }
    const blocks = Array.from(el.querySelectorAll(".code-block"));
    const candidates = [];
    blocks.forEach((wrapper, index) => {
      const code = wrapper.querySelector("pre > code");
      const match = code && /(?:^|\s)language-([\w+#.-]+)/.exec(code.className);
      if (match && isSvgBlock(match[1], code.textContent)) {
        candidates.push({ wrapper, code, index });
      }
    });
    if (!candidates.length) {
      return;
    }
    const open = final ? -1 : openBlockIndex(String(text ?? ""));
    if (!chosen.has(el)) {
      chosen.set(el, new Map());
    }
    const modes = chosen.get(el);
    for (const { wrapper, code, index } of candidates) {
      // Nur der letzte Block kann im Stream noch offen sein.
      const complete = !(open >= 0 && index === blocks.length - 1);
      enhance(wrapper, code, complete, modes, index);
    }
  }

  window.MultiGPT = window.MultiGPT || {};
  window.MultiGPT.svgPreview = { decorate, isSvgBlock, prepare };
})();
