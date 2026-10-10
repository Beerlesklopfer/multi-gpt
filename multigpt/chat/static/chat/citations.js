// MultiGPT – Quellenliste und Zitieren (M7 Dokumente, M8 Web).
//
// - render(article, sources): Quellenliste unter einer Antwort aus dem
//   SSE-Event "sources" (Einzelchat und Vergleich), Aufbau wie
//   chat/_sources.html und chat/_source.html.
// - enhance(section): Knöpfe „Zitat kopieren“ je Quelle und „Literaturverzeichnis
//   kopieren“, jeweils mit Menü für andere Stile und BibTeX.
// - decorate(content): Bei der Einstellung „Kurzbeleg“ werden Verweise [n] im
//   Antworttext durch den Kurzbeleg ersetzt (markdown.js ruft das nach jedem
//   Rendern auf); außerdem markLinks: Links ohne Quelle markieren.
//
// Alle Zitate formatiert der Server (sources.serialize, citations.py); hier
// wird nur Text gesetzt (textContent), nie HTML. Kein Inline-JS.
"use strict";

(() => {
  const SOURCES_VISIBLE = 3; // wie templatetags/source_tags.py
  const STYLE_LABELS = {
    din: "DIN ISO 690",
    apa: "APA 7",
    harvard: "Harvard",
    chicago: "Chicago",
    mla: "MLA 9",
    bibtex: "BibTeX",
  };
  const STYLES = ["din", "apa", "harvard", "chicago", "mla", "bibtex"];

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) {
      element.className = className;
    }
    if (text !== undefined) {
      element.textContent = text;
    }
    return element;
  }

  // Webquelle: nur http(s) mit Host. Dokumentquelle: nur eigene, relative Pfade.
  function safeHref(source) {
    const raw = typeof source.url === "string" ? source.url.trim() : "";
    if (source.kind === "document") {
      return raw.startsWith("/") && !raw.startsWith("//") && !raw.startsWith("/\\") ? raw : "";
    }
    try {
      const url = new URL(raw);
      return (url.protocol === "http:" || url.protocol === "https:") && url.hostname ? url.href : "";
    } catch {
      return "";
    }
  }

  function domainOf(href) {
    try {
      const host = new URL(href).hostname;
      return host.startsWith("www.") ? host.slice(4) : host;
    } catch {
      return "";
    }
  }

  function clean(value) {
    return String(value ?? "").replace(/\s+/g, " ").trim();
  }

  function buildItem(source) {
    const item = node("li", "message-source");
    const href = safeHref(source);
    const web = source.kind !== "document";
    const site = web && href ? domainOf(href) : "";
    const raw = typeof source.url === "string" ? source.url.trim() : "";
    const label = clean(source.label) || clean(source.title) || site || (web ? raw : "") || (web ? "Quelle" : "Dokument");
    if (href) {
      const link = node("a", "message-source-link", label);
      link.href = href;
      link.target = "_blank";
      link.rel = "noopener noreferrer nofollow";
      link.append(node("span", "sr-only", " (öffnet in neuem Tab)"));
      item.append(link);
      if (site) {
        item.append(" ", node("span", "message-source-domain", site));
      }
    } else {
      item.append(node("span", "message-source-link", label));
    }
    const location = clean(source.location);
    if (location) {
      item.append(" ", node("span", "message-source-location", location));
    }
    return item;
  }

  function citationData(source) {
    const keys = ["n", "kind", "short", "entry", "inline", "style", "formats"];
    return Object.fromEntries(keys.map((key) => [key, source[key]]));
  }

  function render(article, sources) {
    article.querySelector(".message-sources")?.remove();
    const list = (Array.isArray(sources) ? sources : []).filter((s) => s && typeof s === "object");
    if (!list.length) {
      return;
    }
    list.sort((a, b) => (Number(a.n) || 0) - (Number(b.n) || 0));
    const section = node("section", "message-sources");
    const titleId = `message-sources-${article.dataset.messageId || "neu"}`;
    section.setAttribute("aria-labelledby", titleId);
    section.dataset.citations = JSON.stringify(list.map(citationData));
    const heading = node("h3", "message-sources-title", "Quellen");
    heading.id = titleId;
    const head = node("ol", "message-sources-list");
    head.append(...list.slice(0, SOURCES_VISIBLE).map(buildItem));
    section.append(heading, head);
    const rest = list.slice(SOURCES_VISIBLE);
    if (rest.length) {
      const more = node("details", "message-sources-more");
      const tail = node("ol", "message-sources-list");
      tail.start = SOURCES_VISIBLE + 1;
      tail.append(...rest.map(buildItem));
      more.append(node("summary", null, `Weitere Quellen (${rest.length})`), tail);
      section.append(more);
    }
    article.querySelector(".chat-message-content").after(section);
    enhance(section);
    const content = article.querySelector(".chat-message-content");
    if (content) {
      decorate(content);
    }
  }

  // --- Kopieren ---

  function readCitations(section) {
    try {
      const data = JSON.parse(section?.dataset.citations || "[]");
      return Array.isArray(data) ? data.filter((c) => c && typeof c === "object") : [];
    } catch {
      return [];
    }
  }

  function textFor(citation, style) {
    const formats = citation.formats && typeof citation.formats === "object" ? citation.formats : {};
    if (style === "bibtex") {
      return typeof formats.bibtex === "string" ? formats.bibtex : "";
    }
    const entry = formats[style] && typeof formats[style].entry === "string" ? formats[style].entry : "";
    return entry || (typeof citation.entry === "string" ? citation.entry : "");
  }

  // Literaturverzeichnis: jeder Eintrag einmal, alphabetisch (BibTeX: Reihenfolge).
  function bibliography(citations, style) {
    const entries = [...new Set(citations.map((c) => textFor(c, style)).filter(Boolean))];
    if (style === "bibtex") {
      return entries.join("\n\n");
    }
    return entries.sort((a, b) => a.localeCompare(b, "de")).join("\n");
  }

  async function copy(text, button) {
    if (!text) {
      return;
    }
    const label = button.dataset.label || button.textContent;
    button.dataset.label = label;
    try {
      const copyText = window.MultiGPT.markdown?.copyText;
      if (copyText) {
        await copyText(text);
      } else {
        await navigator.clipboard.writeText(text);
      }
      button.textContent = "Kopiert";
    } catch {
      button.textContent = "Kopieren nicht möglich";
    }
    window.setTimeout(() => {
      button.textContent = button.dataset.label;
    }, 2000);
  }

  // Knopf plus Menü <details> mit allen Stilen; getText(style) liefert den Text.
  function copyControl(mainLabel, ariaLabel, ownStyle, getText) {
    const wrap = node("span", "cite-copy");
    const main = node("button", "link-button cite-copy-main", mainLabel);
    main.type = "button";
    main.setAttribute("aria-label", ariaLabel);
    main.addEventListener("click", () => copy(getText(ownStyle), main));
    const menu = node("details", "cite-menu");
    const summary = node("summary", "cite-menu-toggle", "Stil");
    summary.setAttribute("aria-label", `${ariaLabel} in anderem Stil`);
    const options = node("ul", "cite-menu-list");
    for (const style of STYLES) {
      const button = node("button", "link-button", STYLE_LABELS[style]);
      button.type = "button";
      button.addEventListener("click", async () => {
        await copy(getText(style), button);
        window.setTimeout(() => menu.removeAttribute("open"), 600);
      });
      const li = node("li");
      li.append(button);
      options.append(li);
    }
    menu.append(summary, options);
    wrap.append(main, " ", menu);
    return wrap;
  }

  function enhance(section) {
    if (!section || section.dataset.enhanced === "true") {
      return;
    }
    const citations = readCitations(section);
    if (!citations.length) {
      return;
    }
    section.dataset.enhanced = "true";
    const items = section.querySelectorAll(".message-source");
    items.forEach((item, index) => {
      const citation = citations[index];
      if (!citation) {
        return;
      }
      const own = citation.style in STYLE_LABELS ? citation.style : "din";
      item.append(
        " ",
        copyControl("Zitat kopieren", `Quelle ${citation.n} als Zitat kopieren`, own, (style) =>
          textFor(citation, style),
        ),
      );
    });
    const own = citations[0].style in STYLE_LABELS ? citations[0].style : "din";
    const footer = node("p", "cite-bibliography");
    footer.append(
      copyControl("Literaturverzeichnis kopieren", "Literaturverzeichnis kopieren", own, (style) =>
        bibliography(citations, style),
      ),
    );
    section.append(footer);
  }

  // --- Kurzbeleg im Antworttext ---

  const MARKER = /\[(\d{1,3}(?:\s*[,;]\s*\d{1,3})*)\]/g;

  function decorate(content) {
    const article = content?.closest("article");
    const section = article?.querySelector(".message-sources");
    enhance(section); // nachgeladene Verläufe (Zweigwechsel) bekommen so ihre Knöpfe
    markLinks(content);
    const citations = readCitations(section);
    const shorts = new Map(
      citations.filter((c) => c.inline && typeof c.short === "string" && c.short).map((c) => [String(c.n), c.short]),
    );
    if (!shorts.size) {
      return;
    }
    const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT, {
      acceptNode(text) {
        if (text.parentElement?.closest("pre, code, a, .cite-inline")) {
          return NodeFilter.FILTER_REJECT;
        }
        MARKER.lastIndex = 0;
        return MARKER.test(text.data) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
      },
    });
    const texts = [];
    while (walker.nextNode()) {
      texts.push(walker.currentNode);
    }
    for (const text of texts) {
      const fragment = document.createDocumentFragment();
      let last = 0;
      MARKER.lastIndex = 0;
      for (const match of text.data.matchAll(MARKER)) {
        const numbers = match[1].split(/[,;]/).map((n) => n.trim());
        if (!numbers.every((n) => shorts.has(n))) {
          continue;
        }
        fragment.append(text.data.slice(last, match.index));
        const span = node("span", "cite-inline", numbers.map((n) => shorts.get(n)).join("; "));
        span.title = `Quelle ${match[0]}`;
        fragment.append(span);
        last = match.index + match[0].length;
      }
      if (last) {
        fragment.append(text.data.slice(last));
        text.replaceWith(fragment);
      }
    }
  }

  // --- Ungeprüfte Links (gegen erfundene Links) ---
  //
  // Links in einer Antwort, deren URL bzw. Rechnername in keiner Quelle dieser
  // Antwort, keinem Werkzeugergebnis und nicht in der Frage vorkommt, bekommen
  // ein Warnsymbol; darunter steht ein Hinweis mit „Belege prüfen“. Nur im
  // Client, kein Netzaufruf. Abschaltbar in den Einstellungen (data-mark-links).

  const UNVERIFIED = "Link stammt nicht aus einer Quelle dieser Antwort – möglicherweise erfunden";
  const URL_IN_TEXT = /https?:\/\/[^\s<>"'`]+/gi;
  const URL_TAIL = /[.,;:!?)\]}»“"']+$/;

  function markingEnabled() {
    return document.querySelector("[data-mark-links]")?.dataset.markLinks !== "false";
  }

  function bareHost(host) {
    const lower = String(host || "").toLowerCase().replace(/\.$/, "");
    return lower.startsWith("www.") ? lower.slice(4) : lower;
  }

  // Vergleichsform: Rechner ohne www., Pfad ohne abschließenden Schrägstrich, ohne Fragment.
  function normalUrl(url) {
    const port = url.port ? `:${url.port}` : "";
    return `${bareHost(url.hostname)}${port}${url.pathname.replace(/\/+$/, "")}${url.search}`;
  }

  function webUrl(raw) {
    try {
      const url = new URL(raw, window.location.href);
      return (url.protocol === "http:" || url.protocol === "https:") && url.hostname ? url : null;
    } catch {
      return null;
    }
  }

  function knownLinks(article) {
    const urls = new Set();
    const hosts = new Set();
    const add = (raw) => {
      const url = webUrl(raw);
      if (url) {
        urls.add(normalUrl(url));
        hosts.add(bareHost(url.hostname));
      }
    };
    const scan = (text) => {
      for (const match of String(text || "").matchAll(URL_IN_TEXT)) {
        add(match[0].replace(URL_TAIL, ""));
      }
    };
    for (const link of article.querySelectorAll(".message-sources a[href]")) {
      add(link.getAttribute("href"));
    }
    for (const result of article.querySelectorAll(".tool-call-result")) {
      scan(result.textContent);
    }
    // Links, die der Nutzer in seiner Frage selbst genannt hat.
    let previous = article.previousElementSibling;
    while (previous && previous.dataset?.role !== "user") {
      previous = previous.previousElementSibling;
    }
    if (previous) {
      scan(previous.querySelector(".chat-message-content")?.textContent);
    }
    return { urls, hosts };
  }

  function unmark(link) {
    link.querySelectorAll(".link-unverified-icon, .link-unverified-label").forEach((el) => el.remove());
    link.classList.remove("link-unverified");
    if (link.title === UNVERIFIED) {
      link.removeAttribute("title");
    }
  }

  function markLinks(content) {
    const article = content?.closest("article");
    if (!article || article.dataset.role === "user") {
      return;
    }
    const enabled = markingEnabled();
    const known = enabled ? knownLinks(article) : null;
    const unverified = [];
    for (const link of content.querySelectorAll("a[href]")) {
      const url = webUrl(link.getAttribute("href"));
      if (!url || url.origin === window.location.origin) {
        continue;
      }
      link.rel = "noopener noreferrer nofollow";
      unmark(link);
      if (!enabled || known.urls.has(normalUrl(url)) || known.hosts.has(bareHost(url.hostname))) {
        continue;
      }
      link.classList.add("link-unverified");
      link.title = UNVERIFIED;
      const icon = node("span", "link-unverified-icon", "⚠");
      icon.setAttribute("aria-hidden", "true");
      link.append(icon, node("span", "sr-only link-unverified-label", ` (${UNVERIFIED})`));
      unverified.push(url.href);
    }
    updateNotice(article, content, unverified);
  }

  function updateNotice(article, content, unverified) {
    let notice = article.querySelector(".unverified-links-notice");
    if (!unverified.length) {
      notice?.remove();
      return;
    }
    if (!notice) {
      notice = node("p", "unverified-links-notice");
      (article.querySelector(".message-sources") || content).after(notice);
    }
    const input = document.getElementById("message-input");
    notice.replaceChildren(node("span", "unverified-links-icon", "⚠"));
    notice.firstChild.setAttribute("aria-hidden", "true");
    if (!input) {
      notice.append(" Diese Antwort enthält Links ohne Quelle.");
      return;
    }
    notice.append(" Diese Antwort enthält Links ohne Quelle. Mit „Belege prüfen“ nachsehen lassen: ");
    const button = node("button", "link-button unverified-links-check", "Belege prüfen");
    button.type = "button";
    button.addEventListener("click", () => prepareCheck(unverified));
    notice.append(button);
  }

  // Neue Nachricht vorbereiten (Websuche an), nicht senden.
  function prepareCheck(urls) {
    const input = document.getElementById("message-input");
    if (!input || input.disabled) {
      return;
    }
    const toggle = document.getElementById("web-search-toggle");
    if (toggle && !toggle.disabled && !toggle.checked) {
      toggle.checked = true;
      toggle.dispatchEvent(new Event("change", { bubbles: true }));
    }
    const list = [...new Set(urls)].slice(0, 3).join("\n");
    input.value =
      "Bitte prüfe die Links und Angaben deiner letzten Antwort. Rufe die Seiten ab " +
      "(fetch_url bzw. Websuche) und sage zu jedem Link, ob es ihn gibt und ob er die Aussage " +
      "belegt. Nenne nur Links, die du tatsächlich abrufen konntest.\n" +
      list;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.focus();
  }

  window.MultiGPT = window.MultiGPT || {};
  window.MultiGPT.sources = { render, enhance, decorate, markLinks };

  // Serverseitig gerenderte Listen; markdown.js ruft decorate nach jedem Rendern
  // auf, das hier deckt Antworten ohne Markdown-Rendern ab.
  document.addEventListener("DOMContentLoaded", () => {
    for (const section of document.querySelectorAll(".message-sources")) {
      enhance(section);
      const content = section.closest("article")?.querySelector(".chat-message-content");
      if (content) {
        decorate(content);
      }
    }
  });
})();
