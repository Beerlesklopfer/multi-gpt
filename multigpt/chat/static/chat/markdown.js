// MultiGPT – Markdown-Darstellung der Modellantworten (M5-01, Plan 9).
//
// Ablauf IMMER: marked.parse -> DOMPurify.sanitize (als DOM-Fragment) -> einfügen.
// - Rohes HTML im Markdown wird nicht ausgeführt, sondern als Text gezeigt
//   (eigener Renderer); DOMPurify ist die zweite, maßgebliche Schranke.
// - Keine externen Ressourcen: Bilder werden zu Links, <img>, <style>, Formulare,
//   Medien und eingebettete Inhalte sind verboten, ebenso style-Attribute.
// - Links öffnen in neuem Tab mit rel="noopener noreferrer nofollow"; nur http(s),
//   mailto und relative Ziele (DOMPurify entfernt javascript: u. Ä.).
// - Code: Hervorhebung mit highlight.js (arbeitet auf Text), Kopierknopf.
// - Formeln (LaTeX): math.js schützt sie vor marked und setzt sie nach der
//   Bereinigung mit KaTeX in Platzhalter-Elemente (nicht durch DOMPurify).
// - SVG-Codeblöcke: svg_preview.js zeigt sie bereinigt als <img> (data:-URL).
// Fehlen die Bibliotheken, bleibt der Rohtext als Text stehen.
"use strict";

(() => {
  const ready =
    typeof window.marked !== "undefined" &&
    typeof window.DOMPurify !== "undefined" &&
    window.DOMPurify.isSupported;

  const SANITIZE_CONFIG = {
    USE_PROFILES: { html: true },
    FORBID_TAGS: [
      "style",
      "img",
      "svg",
      "math",
      "form",
      "input",
      "button",
      "textarea",
      "select",
      "option",
      "iframe",
      "frame",
      "object",
      "embed",
      "video",
      "audio",
      "source",
      "track",
      "picture",
      "link",
      "meta",
      "base",
    ],
    FORBID_ATTR: ["style", "srcset", "ping", "formaction", "action", "background", "poster"],
    ALLOW_DATA_ATTR: false,
    ALLOWED_URI_REGEXP: /^(?:https?:|mailto:|#|\/(?!\/)|\.{0,2}\/|[^:/?#]*(?:[/?#]|$))/i,
    RETURN_DOM_FRAGMENT: true,
  };

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // Ziel eines Anhang-Verweises: ID oder Dateiname (ohne Pfad, ohne Leerzeichen).
  const ATTACHMENT_REF = /^(?:anhang|attachment):([^\s/\\]{1,200})$/i;

  if (ready) {
    // Links: neuer Tab, kein Opener, kein Referrer.
    window.DOMPurify.addHook("afterSanitizeAttributes", (node) => {
      if (node.tagName === "A") {
        if (node.hasAttribute("href")) {
          node.setAttribute("target", "_blank");
          node.setAttribute("rel", "noopener noreferrer nofollow");
        } else {
          node.removeAttribute("target");
        }
      }
    });

    window.marked.use({
      gfm: true,
      breaks: true,
      renderer: {
        // Rohes HTML im Modelltext als Text zeigen statt interpretieren.
        html(token) {
          return escapeHtml(token.text ?? token.raw ?? "");
        },
        // Keine externen Bilder laden: als Link darstellen.
        image(token) {
          // ![…](anhang:ID) bzw. ![…](attachment:NAME): Verweis auf einen Anhang
          // per ID oder Dateiname (linkAttachments unten). Modelle schreiben
          // gern attachment:balken.png statt anhang:12.
          const ref = ATTACHMENT_REF.exec(String(token.href || "").trim());
          if (ref) {
            let name = ref[1];
            try {
              name = decodeURIComponent(name); // marked kodiert Umlaute in href
            } catch {
              // roh lassen
            }
            const target = /^\d+$/.test(name)
              ? `#anhang-${name}`
              : `#anhang-name-${encodeURIComponent(name)}`;
            return `<a class="attachment-ref" href="${escapeHtml(target)}">${escapeHtml(token.text || "Anhang")}</a>`;
          }
          const label = token.text ? `Bild: ${token.text}` : "Bild";
          return `<a href="${escapeHtml(token.href || "")}">${escapeHtml(label)}</a>`;
        },
      },
    });
  }

  // --- Kopierknopf ---------------------------------------------------------------

  async function copyText(text) {
    // navigator.clipboard gibt es nur in sicheren Kontexten (HTTPS/localhost).
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text);
        return;
      } catch {
        // Weiter mit dem älteren Weg über eine Textauswahl.
      }
    }
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.className = "copy-buffer";
    document.body.append(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    if (!ok) {
      throw new Error("copy failed");
    }
  }

  function decorateCodeBlocks(root, highlight) {
    for (const code of root.querySelectorAll("pre > code")) {
      const pre = code.parentElement;
      if (pre.parentElement?.classList.contains("code-block")) {
        continue;
      }
      const match = /(?:^|\s)language-([\w+#.-]+)/.exec(code.className);
      const lang = match ? match[1] : "";
      if (highlight && window.hljs) {
        try {
          if (lang && window.hljs.getLanguage(lang)) {
            window.hljs.highlightElement(code);
          } else if (!lang && code.textContent.length < 20000) {
            const result = window.hljs.highlightAuto(code.textContent);
            if (result.relevance >= 5) {
              // highlight.js liefert HTML aus eigenem Escaping; trotzdem bereinigen.
              code.replaceChildren(
                window.DOMPurify.sanitize(result.value, {
                  ALLOWED_TAGS: ["span"],
                  ALLOWED_ATTR: ["class"],
                  RETURN_DOM_FRAGMENT: true,
                }),
              );
              code.classList.add("hljs");
            }
          }
        } catch {
          // Hervorhebung ist Kosmetik.
        }
      }
      const wrapper = document.createElement("div");
      wrapper.className = "code-block";
      const bar = document.createElement("div");
      bar.className = "code-block-bar";
      const label = document.createElement("span");
      label.className = "code-block-lang";
      label.textContent = lang || "Code";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "code-copy";
      button.textContent = "Kopieren";
      button.setAttribute("aria-label", `${lang || "Code"} kopieren`);
      button.addEventListener("click", async () => {
        try {
          await copyText(code.textContent);
          button.textContent = "Kopiert";
        } catch {
          button.textContent = "Fehlgeschlagen";
        }
        window.setTimeout(() => {
          button.textContent = "Kopieren";
        }, 2000);
      });
      bar.append(label, button);
      pre.replaceWith(wrapper);
      wrapper.append(bar, pre);
    }
  }

  // ![…](anhang:ID) bzw. ![…](attachment:NAME) im Modelltext (nach run_python,
  // create_pdf bzw. generate_image): nur Anhänge DERSELBEN Nachricht
  // (.message-attachments im Artikel, vom Server gerendert); NAME ist der
  // angezeigte Dateiname (data-name, ohne Groß-/Kleinschreibung). Bild -> kleine
  // Vorschau, sonst Link „Ansehen“; fremde bzw. (noch) unbekannte Verweise werden
  // ausgeblendet – der Anhang steht ohnehin unter der Antwort. Adressen kommen
  // nur aus data-url/-thumbnail-url des Servers.
  const LOCAL_PATH = /^\/(?!\/)/;

  function findAttachment(article, target) {
    const items =
      article?.querySelectorAll(":scope > .message-attachments > [data-attachment-id]") || [];
    if (target.startsWith("name-")) {
      let name = "";
      try {
        name = decodeURIComponent(target.slice("name-".length)).toLowerCase();
      } catch {
        return null;
      }
      return [...items].find((item) => (item.dataset.name || "").toLowerCase() === name) || null;
    }
    return [...items].find((item) => item.dataset.attachmentId === target) || null;
  }

  function linkAttachments(el) {
    const refs = el.querySelectorAll('a.attachment-ref[href^="#anhang-"]');
    if (!refs.length) {
      return;
    }
    const article = el.closest(".chat-message");
    for (const ref of refs) {
      const item = findAttachment(article, ref.getAttribute("href").slice("#anhang-".length));
      const url = item?.dataset.url || "";
      if (!item || !LOCAL_PATH.test(url)) {
        ref.remove();
        continue;
      }
      const label = ref.textContent;
      const link = document.createElement("a");
      link.className = "attachment-ref";
      link.href = url;
      if (item.dataset.kind === "image") {
        const thumb = item.dataset.thumbnailUrl || "";
        const img = document.createElement("img");
        img.src = LOCAL_PATH.test(thumb) ? thumb : url;
        img.alt = label;
        img.loading = "lazy";
        // Aussehen wie die Vorschaubilder der Anhänge (app.css).
        link.classList.add("attachment-thumb");
        link.setAttribute("aria-label", `Bild ${label} öffnen`);
        link.append(img);
      } else {
        link.textContent = `${label} – Ansehen`;
      }
      link.target = "_blank";
      link.rel = "noopener";
      ref.replaceWith(link);
    }
  }

  // Text als bereinigtes Markdown in el darstellen. final=false während des
  // Streams (ohne Hervorhebung, die bei jedem Bild neu liefe).
  // Quelltext je Element, damit eine fortgesetzte Antwort (Rückfrage bei
  // Werkzeugen) weiter angehängt werden kann.
  const sources = new WeakMap();

  function sourceOf(el) {
    return sources.has(el) ? sources.get(el) : el.textContent;
  }

  function render(el, text, final = true) {
    sources.set(el, String(text ?? ""));
    if (!ready) {
      el.textContent = text;
      return false;
    }
    // Formeln (math.js) vor marked durch Platzhalter schützen und erst nach der
    // Bereinigung setzen; Code bleibt roh.
    const math = window.MultiGPT?.math;
    let fragment;
    let formulas = { text: String(text ?? ""), items: [] };
    try {
      if (math) {
        formulas = math.protect(formulas.text, final);
      }
      const html = window.marked.parse(formulas.text, { async: false });
      fragment = window.DOMPurify.sanitize(html, SANITIZE_CONFIG);
      math?.restore(fragment, formulas);
    } catch {
      el.textContent = text;
      el.classList.remove("is-markdown");
      return false;
    }
    el.replaceChildren(fragment);
    el.classList.add("is-markdown");
    linkAttachments(el);
    decorateCodeBlocks(el, final);
    // SVG-Codeblöcke: Bildvorschau nur als <img> (svg_preview.js).
    window.MultiGPT?.svgPreview?.decorate(el, text, final);
    // Kurzbeleg statt [n] und Kopierknöpfe der Quellen (citations.js).
    window.MultiGPT?.sources?.decorate(el);
    math?.typeset(el, formulas);
    return true;
  }

  // Gedrosselter Renderer für den Stream: höchstens ein Rendern je Bild.
  // onRender wird nach jedem Rendern aufgerufen (z. B. zum Mitscrollen).
  // initial: bereits vorhandener Text (Fortsetzung einer Antwort).
  function streamRenderer(el, onRender, initial = "") {
    let text = initial;
    let frame = 0;
    const flush = () => {
      frame = 0;
      render(el, text, false);
      onRender?.();
    };
    return {
      append(chunk) {
        text += chunk;
        if (!frame) {
          frame = window.requestAnimationFrame(flush);
        }
      },
      finish() {
        if (frame) {
          window.cancelAnimationFrame(frame);
          frame = 0;
        }
        render(el, text, true);
        onRender?.();
      },
      get text() {
        return text;
      },
    };
  }

  // Serverseitig als Text ausgelieferte Antworten nach dem Laden rendern.
  function renderAll(root = document) {
    for (const el of root.querySelectorAll("[data-markdown=true]:not(.is-markdown)")) {
      render(el, el.textContent, true);
    }
  }

  window.MultiGPT = window.MultiGPT || {};
  window.MultiGPT.markdown = { ready, render, streamRenderer, renderAll, sourceOf, copyText };

  document.addEventListener("DOMContentLoaded", () => renderAll());
})();
