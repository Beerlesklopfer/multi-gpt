// MultiGPT – Kontextmenü für markierten Text im Chat (M13-01).
//
// Gilt für markierten Text in Antworten, Nutzernachrichten, Vergleichsspalten
// und Quellenlisten. Öffnen:
// - Rechtsklick auf die Auswahl (Umschalt+Rechtsklick: immer das Browsermenü;
//   auf Links, Eingabefeldern und Knöpfen greift nie das eigene Menü),
// - Leiste unter der Auswahl (auch der Weg am Handy: langes Tippen),
// - Kontextmenü-Taste bzw. Umschalt+F10 bei aktiver Auswahl.
// Menü nach dem Muster „Menu“ der WAI-ARIA APG: Pfeiltasten, Pos1/Ende,
// Anfangsbuchstabe, Esc schließt und gibt den Fokus zurück.
//
// Einträge: im Eingabefeld zitieren (Markdown „> “; weitere Zitate werden
// angehängt, nie ersetzt), Nachfragen, Schnellaktionen (nichts wird gesendet),
// Kopieren und Kopieren mit Quellenangabe. Ziel ist das aktive Eingabefeld:
// das Feld unten oder der Editor einer Nachricht. Ohne Eingabefeld (nur
// lesen) bleibt nur Kopieren.
//
// Abschaltbar in den Einstellungen (Konto, context_menu); dann lädt das
// Template diese Datei nicht. Nur Text wird gesetzt (textContent), nie HTML.
"use strict";

(() => {
  const ZONES = ".chat-message-content, .compare-column-content, .message-sources";
  const IGNORE = "a, input, textarea, select, button, summary, [contenteditable], .message-editor";
  const FIELDS = "#message-input, .message-editor-input";
  const KEYBOARD_GRACE_MS = 600;
  const LABEL_CHARS = 48;
  const MARKER = /\[(\d{1,3}(?:\s*[,;]\s*\d{1,3})*)\]/g;

  // Schnellaktionen: Zitat plus Anweisung, nichts wird gesendet.
  const QUICK = [
    { id: "explain", label: "Erklären", text: "Erkläre die zitierte Stelle genauer." },
    {
      id: "simplify",
      label: "Vereinfachen",
      text: "Formuliere die zitierte Stelle einfacher: kurze Sätze, ohne Fachbegriffe.",
    },
    {
      id: "translate",
      label: "Übersetzen",
      text: "Übersetze die zitierte Stelle ins Deutsche, ist sie schon deutsch, ins Englische.",
    },
    {
      id: "counter",
      label: "Gegenargumente",
      text: "Welche Gegenargumente gibt es zur zitierten Aussage? Die stärksten zuerst.",
    },
    {
      id: "evidence",
      label: "Belege suchen",
      text: "Suche Belege für die zitierte Aussage und nenne die Quellen. Sag deutlich, wenn du keine findest.",
    },
  ];
  const ASK_TEXT = "Was meinst du mit „…“?";

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

  function elementOf(target) {
    return target?.nodeType === Node.ELEMENT_NODE ? target : target?.parentElement || null;
  }

  function announce(text) {
    window.MultiGPT?.announce?.(text);
  }

  // --- Auswahl lesen -----------------------------------------------------------

  // Text der Auswahl mit Zeilen wie angezeigt; Formeln als TeX ($…$), Code
  // als Codeblock, ohne Knöpfe und versteckte Hilfstexte.
  function selectedText(range, zone) {
    const r = range.cloneRange();
    const first = elementOf(r.startContainer)?.closest(".math, .cite-inline");
    if (first && zone.contains(first)) {
      r.setStartBefore(first);
    }
    const last = elementOf(r.endContainer)?.closest(".math, .cite-inline");
    if (last && zone.contains(last)) {
      r.setEndAfter(last);
    }
    const fragment = r.cloneContents();
    for (const math of fragment.querySelectorAll(".math")) {
      const tex = math.querySelector('annotation[encoding="application/x-tex"]')?.textContent;
      if (tex) {
        const display = math.classList.contains("math-display");
        const replacement = node(display ? "div" : "span", "", display ? `$$${tex}$$` : `$${tex}$`);
        math.replaceWith(replacement);
      }
    }
    for (const pre of fragment.querySelectorAll("pre")) {
      const code = pre.querySelector("code") || pre;
      const lang = /(?:^|\s)language-([\w+#.-]+)/.exec(code.className || "")?.[1] || "";
      pre.replaceWith(node("pre", "", `\`\`\`${lang}\n${code.textContent.replace(/\n+$/, "")}\n\`\`\``));
    }
    for (const el of fragment.querySelectorAll(
      ".sr-only, .code-block-bar, .cite-copy, .cite-bibliography, button, .link-unverified-icon, .katex-mathml",
    )) {
      el.remove();
    }
    // innerText braucht ein gerendertes Element: unsichtbar außerhalb des Fensters.
    const holder = node("div", `${zone.className} context-measure`);
    holder.setAttribute("aria-hidden", "true");
    holder.append(fragment);
    document.body.append(holder);
    const text = holder.innerText;
    holder.remove();
    return text
      .replace(/ /g, " ")
      .replace(/[ \t]+$/gm, "")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
  }

  function readCitations(article) {
    try {
      const data = JSON.parse(article?.querySelector(".message-sources")?.dataset.citations || "[]");
      return Array.isArray(data) ? data.filter((c) => c && typeof c === "object") : [];
    } catch {
      return [];
    }
  }

  // Nummern der Quellen, auf die sich die Auswahl bezieht: Einträge der
  // Quellenliste bzw. Verweise [n] und Kurzbelege im Antworttext.
  function referencedNumbers(range, zone, citations) {
    const numbers = new Set();
    if (zone.classList.contains("message-sources")) {
      zone.querySelectorAll(".message-source").forEach((item, index) => {
        if (range.intersectsNode(item) && citations[index]) {
          numbers.add(String(citations[index].n));
        }
      });
      return numbers;
    }
    const fragment = range.cloneContents();
    const texts = [fragment.textContent || ""];
    for (const cite of zone.querySelectorAll(".cite-inline[title]")) {
      if (range.intersectsNode(cite)) {
        texts.push(cite.title);
      }
    }
    for (const text of texts) {
      for (const match of text.matchAll(MARKER)) {
        for (const n of match[1].split(/[,;]/)) {
          numbers.add(n.trim());
        }
      }
    }
    return numbers;
  }

  // Herkunft je Zitat: Modell der Antwort, „Ich“ bzw. Name (geteilte Chats)
  // bei Nutzernachrichten, „Quelle [n]“ in der Quellenliste.
  function originOf(zone, range, citations) {
    const column = zone.closest(".compare-column");
    if (zone.classList.contains("message-sources")) {
      const items = Array.from(zone.querySelectorAll(".message-source"));
      const index = items.findIndex((item) => range.intersectsNode(item));
      const n = citations[index]?.n ?? (index >= 0 ? index + 1 : null);
      return n ? `Quelle [${n}]` : "Quellen";
    }
    if (column) {
      return column.querySelector(".compare-column-model")?.textContent.trim() || "";
    }
    const article = zone.closest(".chat-message");
    const author = article?.querySelector(".chat-message-author")?.textContent.trim() || "";
    if (article?.dataset.role === "user") {
      return !author || author === "Du" ? "Ich" : author;
    }
    return author;
  }

  // Momentaufnahme der Auswahl; null, wenn sie nicht in genau einem Bereich liegt.
  function snapshot() {
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount) {
      return null;
    }
    const range = selection.getRangeAt(0);
    const zone = elementOf(range.commonAncestorContainer)?.closest(ZONES);
    if (!zone || !zone.closest("#chat") || zone.closest(".message-editor")) {
      return null;
    }
    const text = selectedText(range, zone);
    if (!text) {
      return null;
    }
    const article = zone.closest(".chat-message, .compare-column");
    const citations = readCitations(article);
    const numbers = citations.length ? referencedNumbers(range, zone, citations) : new Set();
    const entries = citations
      .filter((c) => numbers.has(String(c.n)))
      .map((c) => {
        const style = c.style;
        const formats = c.formats && typeof c.formats === "object" ? c.formats : {};
        return (formats[style] && formats[style].entry) || c.entry || "";
      })
      .filter(Boolean);
    return {
      text,
      zone,
      origin: originOf(zone, range, citations),
      entries: [...new Set(entries)],
      inSources: zone.classList.contains("message-sources"),
      rect: range.getBoundingClientRect(),
      range: range.cloneRange(),
    };
  }

  // --- Zitieren ins Eingabefeld --------------------------------------------------

  // Markdown-Zitat: erste Zeile die Herkunft, jede Zeile mit „> “.
  function quoteBlock(snap) {
    const lines = [];
    if (snap.origin) {
      lines.push(`> **${snap.origin.replace(/([\\`*_])/g, "\\$1")}:**`);
    }
    for (const line of snap.text.split("\n")) {
      lines.push(line ? `> ${line}` : ">");
    }
    return lines.join("\n");
  }

  let lastField = null;

  function targetField() {
    const visible = (el) => el && el.isConnected && !el.disabled && el.offsetParent !== null;
    const editors = Array.from(document.querySelectorAll(".message-editor-input")).filter(visible);
    const main = document.getElementById("message-input");
    if (visible(lastField) && (lastField === main || editors.includes(lastField))) {
      return lastField;
    }
    return editors[0] || (visible(main) ? main : null);
  }

  // Zitate je Feld, in der Reihenfolge des Zitierens (für die Liste „Zitate (n)“).
  const quotesOf = new WeakMap();

  // Fügt das Zitat hinter dem letzten Zitat im Feld ein (sonst am Ende); Text
  // des Nutzers bleibt stehen. instruction kommt als eigener Absatz ans Ende,
  // select markiert darin einen Platzhalter.
  function insertQuote(field, snap, instruction = "", select = "") {
    const block = quoteBlock(snap);
    const value = field.value;
    const lines = value.split("\n");
    let lastQuote = -1;
    lines.forEach((line, index) => {
      if (/^\s{0,3}>/.test(line)) {
        lastQuote = index;
      }
    });
    let head;
    let tail;
    if (lastQuote >= 0) {
      head = lines.slice(0, lastQuote + 1).join("\n");
      tail = lines.slice(lastQuote + 1).join("\n").replace(/^\s+/, "");
    } else {
      head = value.replace(/\s+$/, "");
      tail = "";
    }
    let next = `${head ? `${head}\n\n` : ""}${block}\n\n${tail}`;
    let start;
    let end;
    if (instruction) {
      next = `${next.replace(/\s+$/, "")}\n\n${instruction}`;
      const at = select ? next.lastIndexOf(select) : -1;
      start = at >= 0 ? at : next.length;
      end = at >= 0 ? at + select.length : next.length;
    } else {
      start = end = next.length;
    }
    field.value = next;
    const list = quotesOf.get(field) || [];
    list.push({ block, label: snap.origin, text: snap.text });
    quotesOf.set(field, list);
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.focus({ preventScroll: true });
    field.setSelectionRange(start, end);
    field.scrollTop = field.scrollHeight;
    renderQuoteList(field);
  }

  // --- Liste „Zitate (n)“ über dem Feld ------------------------------------------

  const listOf = new WeakMap();

  function shorten(text) {
    const flat = text.replace(/\s+/g, " ").trim();
    return flat.length > LABEL_CHARS ? `${flat.slice(0, LABEL_CHARS - 1)}…` : flat;
  }

  function removeQuote(field, entry) {
    const value = field.value;
    const at = value.indexOf(entry.block);
    if (at >= 0) {
      field.value = `${value.slice(0, at)}${value.slice(at + entry.block.length)}`
        .replace(/\n{3,}/g, "\n\n")
        .replace(/^\s+/, "");
      field.dispatchEvent(new Event("input", { bubbles: true }));
    }
    quotesOf.set(
      field,
      (quotesOf.get(field) || []).filter((item) => item !== entry),
    );
    renderQuoteList(field);
    announce("Zitat entfernt.");
  }

  // Der Text im Feld ist die Wahrheit: Die Liste zeigt nur Zitate, die dort
  // noch unverändert stehen.
  function renderQuoteList(field) {
    const present = (quotesOf.get(field) || []).filter((item) => field.value.includes(item.block));
    quotesOf.set(field, present);
    let box = listOf.get(field);
    if (!present.length) {
      if (box) {
        box.hidden = true;
      }
      return;
    }
    if (!box) {
      box = node("div", "quote-list");
      box.setAttribute("role", "group");
      const title = node("p", "quote-list-title");
      title.id = `quote-list-${Math.random().toString(36).slice(2)}`;
      box.setAttribute("aria-labelledby", title.id);
      box.append(title, node("ul", "quote-list-items"));
      const anchor = field.closest(".chat-input-field") || field;
      anchor.before(box);
      listOf.set(field, box);
      field.addEventListener("input", () => renderQuoteList(field));
    }
    box.hidden = false;
    box.querySelector(".quote-list-title").textContent = `Zitate (${present.length})`;
    box.querySelector(".quote-list-items").replaceChildren(
      ...present.map((entry, index) => {
        const item = node("li", "quote-list-item");
        const label = node("span", "quote-list-label");
        if (entry.label) {
          label.append(node("strong", "", `${entry.label}: `));
        }
        label.append(shorten(entry.text));
        const remove = node("button", "icon-button quote-list-remove");
        remove.type = "button";
        remove.setAttribute("aria-label", `Zitat ${index + 1} entfernen: ${shorten(entry.text)}`);
        remove.title = "Zitat entfernen";
        remove.append(node("span", "", "×"));
        remove.firstChild.setAttribute("aria-hidden", "true");
        remove.addEventListener("click", () => {
          removeQuote(field, entry);
          field.focus({ preventScroll: true });
        });
        item.append(label, remove);
        return item;
      }),
    );
  }

  // --- Aktionen ------------------------------------------------------------------

  function setWebSearch() {
    const picker = window.MultiGPT?.collectionPicker;
    const fieldset = document.getElementById("collection-picker");
    if (picker && fieldset && !fieldset.hidden && picker.selected().length) {
      return "documents"; // gewählte Sammlungen: Dokumentsuche läuft ohnehin
    }
    const toggle = document.getElementById("web-search-toggle");
    if (toggle && !toggle.disabled) {
      if (!toggle.checked) {
        toggle.checked = true;
        toggle.dispatchEvent(new Event("change", { bubbles: true }));
      }
      return "web";
    }
    return "";
  }

  async function copy(text, done) {
    try {
      const copyText = window.MultiGPT?.markdown?.copyText;
      if (copyText) {
        await copyText(text);
      } else {
        await navigator.clipboard.writeText(text);
      }
      announce(done);
    } catch {
      announce("Kopieren ist fehlgeschlagen.");
    }
  }

  function citedText(snap) {
    const list = snap.entries.join("\n");
    return snap.inSources ? list : `${snap.text}\n\n${list}`;
  }

  function run(id, snap) {
    const field = targetField();
    if (id === "copy") {
      copy(snap.text, "Auswahl kopiert.");
      return;
    }
    if (id === "copy-cite") {
      copy(citedText(snap), "Auswahl mit Quellenangabe kopiert.");
      return;
    }
    if (!field) {
      return;
    }
    if (id === "quote") {
      insertQuote(field, snap);
      const count = (quotesOf.get(field) || []).length;
      announce(count > 1 ? `Zitat angehängt (${count} Zitate).` : "Zitat eingefügt.");
      return;
    }
    if (id === "ask") {
      insertQuote(field, snap, ASK_TEXT, "…");
      announce("Zitat eingefügt. Frage ergänzen.");
      return;
    }
    const quick = QUICK.find((q) => q.id === id);
    if (!quick) {
      return;
    }
    let text = quick.text;
    let note = "";
    if (id === "evidence") {
      const mode = setWebSearch();
      if (mode === "documents") {
        text = text.replace("nenne die Quellen", "nenne die Quellen aus den gewählten Dokumenten");
        note = " Dokumentsuche ist aktiv.";
      } else if (mode === "web") {
        note = " Websuche eingeschaltet.";
      }
    }
    insertQuote(field, snap, text);
    announce(`„${quick.label}“ vorbereitet, noch nicht gesendet.${note}`);
  }

  function menuEntries(snap) {
    const hasField = Boolean(targetField());
    const entries = [];
    if (hasField) {
      entries.push(
        { id: "quote", label: "Im Eingabefeld zitieren" },
        { id: "ask", label: "Nachfragen …" },
        null,
        ...QUICK.map((q) => ({ id: q.id, label: q.label })),
        null,
      );
    }
    entries.push({ id: "copy", label: "Kopieren" });
    if (snap.entries.length) {
      entries.push({ id: "copy-cite", label: "Kopieren mit Quellenangabe" });
    }
    return entries;
  }

  // --- Menü (WAI-ARIA APG „Menu“) ------------------------------------------------

  let menu = null; // {el, snap, returnTo}

  function menuItems() {
    return menu ? Array.from(menu.el.querySelectorAll('[role="menuitem"]')) : [];
  }

  function closeMenu(restoreFocus) {
    if (!menu) {
      return;
    }
    const { el, returnTo, snap } = menu;
    menu = null;
    el.remove();
    toolbarMore?.setAttribute("aria-expanded", "false");
    if (!restoreFocus) {
      return;
    }
    if (returnTo?.isConnected) {
      returnTo.focus({ preventScroll: true });
    }
    // Der Fokus im Menü hebt die Auswahl auf (Firefox): wiederherstellen.
    if (snap.range.startContainer.isConnected && snap.range.endContainer.isConnected) {
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(snap.range);
    }
  }

  // Fest im Fenster: oben bei top, reicht der Platz nicht, endet es bei bottom.
  function placeFixed(el, x, top, bottom = top) {
    const margin = 8;
    const width = el.offsetWidth;
    const height = el.offsetHeight;
    const left = Math.max(margin, Math.min(x, window.innerWidth - width - margin));
    let y = top;
    if (y + height > window.innerHeight - margin) {
      y = Math.max(margin, Math.min(bottom, window.innerHeight - margin) - height);
    }
    el.style.left = `${Math.round(left)}px`;
    el.style.top = `${Math.round(y)}px`;
  }

  function openMenu(snap, { x, y, above = y, viaMouse = false, returnTo = null }) {
    closeMenu(false);
    hideToolbar();
    const wrap = node("div", "context-menu");
    const list = node("ul", "context-menu-list");
    list.setAttribute("role", "menu");
    list.setAttribute("aria-label", "Aktionen für die Auswahl");
    for (const entry of menuEntries(snap)) {
      const li = node("li");
      if (!entry) {
        li.setAttribute("role", "separator");
        li.className = "context-menu-separator";
        list.append(li);
        continue;
      }
      li.setAttribute("role", "none");
      const item = node("button", "context-menu-item", entry.label);
      item.type = "button";
      item.setAttribute("role", "menuitem");
      item.tabIndex = -1;
      item.dataset.action = entry.id;
      li.append(item);
      list.append(li);
    }
    wrap.append(list);
    if (viaMouse) {
      const hint = node("p", "context-menu-hint", "Browsermenü: Umschalt+Rechtsklick");
      wrap.append(hint);
    }
    document.body.append(wrap);
    placeFixed(wrap, x, y, above);
    menu = { el: wrap, snap, returnTo };
    list.addEventListener("click", (event) => {
      const item = event.target.closest('[role="menuitem"]');
      if (!item || !menu) {
        return;
      }
      const current = menu.snap;
      const action = item.dataset.action;
      const back = action === "copy" || action === "copy-cite";
      closeMenu(back);
      run(action, current);
    });
    list.addEventListener("keydown", onMenuKey);
    menuItems()[0]?.focus();
  }

  function onMenuKey(event) {
    const items = menuItems();
    const index = items.indexOf(document.activeElement);
    let next = null;
    if (event.key === "ArrowDown") {
      next = items[(index + 1) % items.length];
    } else if (event.key === "ArrowUp") {
      next = items[(index - 1 + items.length) % items.length];
    } else if (event.key === "Home" || event.key === "PageUp") {
      next = items[0];
    } else if (event.key === "End" || event.key === "PageDown") {
      next = items[items.length - 1];
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closeMenu(true);
      return;
    } else if (event.key === "Tab") {
      closeMenu(true); // Tab verlässt das Menü und geht vom Auslöser aus weiter
      return;
    } else if (event.key.length === 1 && /\S/.test(event.key) && !event.ctrlKey && !event.metaKey && !event.altKey) {
      // Anfangsbuchstabe: nächster Eintrag mit diesem Buchstaben.
      const key = event.key.toLocaleLowerCase("de");
      const order = [...items.slice(index + 1), ...items.slice(0, index + 1)];
      next = order.find((item) => item.textContent.trim().toLocaleLowerCase("de").startsWith(key)) || null;
      if (!next) {
        return;
      }
    } else {
      return;
    }
    event.preventDefault();
    next?.focus();
  }

  // --- Leiste bei Textauswahl ------------------------------------------------------

  let toolbar = null;
  let toolbarMore = null;
  let toolbarSnap = null;

  function buildToolbar() {
    toolbar = node("div", "selection-toolbar");
    toolbar.setAttribute("role", "toolbar");
    toolbar.setAttribute("aria-label", "Auswahl");
    toolbar.hidden = true;
    const button = (label, action, aria) => {
      const b = node("button", "selection-toolbar-button", label);
      b.type = "button";
      b.dataset.action = action;
      if (aria) {
        b.setAttribute("aria-label", aria);
      }
      return b;
    };
    toolbar.append(
      button("Zitieren", "quote", "Im Eingabefeld zitieren"),
      button("Nachfragen", "ask"),
      button("Kopieren", "copy"),
    );
    toolbarMore = button("⋯", "more", "Weitere Aktionen");
    toolbarMore.setAttribute("aria-haspopup", "menu");
    toolbarMore.setAttribute("aria-expanded", "false");
    toolbar.append(toolbarMore);
    // Maus: Auswahl bleibt stehen, der Knopf nimmt keinen Fokus.
    toolbar.addEventListener("mousedown", (event) => event.preventDefault());
    toolbar.addEventListener("click", (event) => {
      const b = event.target.closest("[data-action]");
      const snap = toolbarSnap;
      if (!b || !snap) {
        return;
      }
      if (b.dataset.action === "more") {
        const rect = b.getBoundingClientRect();
        openMenu(snap, { x: rect.left, y: rect.bottom + 4, above: rect.top - 4, returnTo: b });
        toolbar.hidden = false; // „⋯“ bleibt sichtbar als Bezug für den Fokus
        toolbarSnap = snap;
        b.setAttribute("aria-expanded", "true");
        return;
      }
      hideToolbar();
      run(b.dataset.action, snap);
    });
    document.body.append(toolbar);
  }

  function hideToolbar() {
    if (toolbar) {
      toolbar.hidden = true;
    }
    toolbarSnap = null;
  }

  function positionToolbar(rect) {
    toolbar.hidden = false;
    // Unter der Auswahl: oben zeigt das Handy sein eigenes Auswahlmenü.
    placeFixed(
      toolbar,
      rect.left + rect.width / 2 - toolbar.offsetWidth / 2,
      rect.bottom + 8,
      rect.top - 8,
    );
  }

  function showToolbar(snap) {
    if (!toolbar) {
      buildToolbar();
    }
    const hasField = Boolean(targetField());
    for (const b of toolbar.querySelectorAll('[data-action="quote"], [data-action="ask"]')) {
      b.hidden = !hasField;
    }
    toolbar.querySelector('[data-action="copy"]').hidden = hasField;
    toolbarSnap = snap;
    positionToolbar(snap.rect);
  }

  // Beim Scrollen des Verlaufs mitwandern; außerhalb des Sichtbereichs verbergen.
  function followToolbar() {
    if (!toolbar || toolbar.hidden || !toolbarSnap) {
      return;
    }
    const rect = toolbarSnap.range.getBoundingClientRect();
    const view = document.getElementById("chat-history")?.getBoundingClientRect();
    if (!rect.height || (view && (rect.bottom < view.top || rect.top > view.bottom))) {
      toolbar.hidden = true;
      return;
    }
    positionToolbar(rect);
  }

  // --- Ereignisse ------------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", () => {
    const chat = document.getElementById("chat");
    if (!chat || chat.dataset.contextMenu === "false") {
      return;
    }
    let keyboardAt = -Infinity;
    let selecting = false;
    let timer = 0;

    document.addEventListener("focusin", (event) => {
      if (event.target.matches?.(FIELDS)) {
        lastField = event.target;
      }
    });

    document.addEventListener("contextmenu", (event) => {
      if (performance.now() - keyboardAt < KEYBOARD_GRACE_MS) {
        event.preventDefault(); // schon per Tastatur geöffnet
        return;
      }
      if (event.defaultPrevented || event.shiftKey) {
        return; // Umschalt+Rechtsklick: Browsermenü
      }
      const target = elementOf(event.target);
      if (!target || target.closest(IGNORE)) {
        return;
      }
      const snap = snapshot();
      if (!snap || !snap.zone.contains(target)) {
        return;
      }
      event.preventDefault();
      const active = document.activeElement;
      openMenu(snap, {
        x: event.clientX,
        y: event.clientY,
        viaMouse: true,
        returnTo: active && active !== document.body ? active : null,
      });
    });

    document.addEventListener("keydown", (event) => {
      const opener = event.key === "ContextMenu" || (event.key === "F10" && event.shiftKey);
      if (!opener || menu || elementOf(event.target)?.closest("input, textarea, select")) {
        if (event.key === "Escape" && !menu && toolbar && !toolbar.hidden) {
          hideToolbar();
        }
        return;
      }
      const snap = snapshot();
      if (!snap) {
        return;
      }
      event.preventDefault();
      keyboardAt = performance.now();
      const active = document.activeElement;
      openMenu(snap, {
        x: snap.rect.left,
        y: snap.rect.bottom + 4,
        above: snap.rect.top - 4,
        returnTo: active && active !== document.body ? active : null,
      });
    });

    // Klick außerhalb schließt das Menü (ohne Fokus zu verschieben).
    document.addEventListener("pointerdown", (event) => {
      if (menu && !menu.el.contains(event.target) && !toolbar?.contains(event.target)) {
        closeMenu(false);
      }
      if (toolbar && !toolbar.contains(event.target)) {
        selecting = true;
      }
    });
    document.addEventListener("pointerup", () => {
      if (selecting) {
        selecting = false;
        scheduleToolbar();
      }
    });
    window.addEventListener("resize", () => {
      closeMenu(false);
      followToolbar();
    });
    window.addEventListener("blur", () => closeMenu(false));

    function scheduleToolbar() {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        if (menu || selecting) {
          return;
        }
        const snap = snapshot();
        if (snap) {
          showToolbar(snap);
        } else {
          hideToolbar();
        }
      }, 250);
    }

    document.addEventListener("selectionchange", scheduleToolbar);
    let frame = 0;
    document.getElementById("chat-history")?.addEventListener(
      "scroll",
      () => {
        if (!frame) {
          frame = window.requestAnimationFrame(() => {
            frame = 0;
            followToolbar();
          });
        }
      },
      { passive: true },
    );
  });

  window.MultiGPT = window.MultiGPT || {};
  window.MultiGPT.contextMenu = { quoteBlock };
})();
