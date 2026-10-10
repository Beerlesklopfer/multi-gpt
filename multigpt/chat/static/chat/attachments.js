// MultiGPT – Anhänge im Chat: Bilder einfügen (Strg+V/Cmd+V), Drag&Drop,
// Büroklammer, sofortiger Upload mit Fortschritt, Vorschauleiste, Anhänge im
// Bearbeiten-Editor und Vollbildansicht (Lightbox) der Bilder in Nachrichten.
//
// Schnittstelle für chat.js/compare.js: window.MultiGPT.attachments (siehe unten).
// Sicherheit: Dateinamen und Meldungen nur als Text (textContent), Bilder nur
// über die geschützten URLs der API (bzw. lokal als blob:-Vorschau während des
// Uploads). Kein Inline-JS.
"use strict";

(() => {
  const IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"];
  const DOCUMENT_EXTENSIONS = [".pdf", ".docx", ".txt", ".md", ".csv"];
  const DEFAULT_MAX_PER_MESSAGE = 10;
  const FILE_ICON =
    '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
    '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>';

  const PAPERCLIP_ICON =
    '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
    '<path d="M21 11.5l-8.6 8.6a5 5 0 0 1-7.1-7.1l8.6-8.6a3.5 3.5 0 0 1 5 5l-8.6 8.6a2 2 0 0 1-2.8-2.8l7.9-7.9"/></svg>';

  const MultiGPT = (window.MultiGPT = window.MultiGPT || {});

  // --- Hilfen -------------------------------------------------------------------

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

  // Statisches, eigenes SVG (keine Nutzerdaten).
  function fileIcon() {
    const wrap = document.createElement("span");
    wrap.className = "attach-file-icon";
    wrap.innerHTML = FILE_ICON;
    return wrap;
  }

  function formatSize(bytes) {
    const value = Number(bytes);
    if (!Number.isFinite(value) || value < 0) {
      return "";
    }
    if (value < 1024) {
      return `${value} Bytes`;
    }
    const units = ["KB", "MB", "GB"];
    let size = value / 1024;
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) {
      size /= 1024;
      unit += 1;
    }
    return `${size.toLocaleString("de-DE", { maximumFractionDigits: size < 10 ? 1 : 0 })} ${units[unit]}`;
  }

  function extensionOf(name) {
    const dot = String(name || "").lastIndexOf(".");
    return dot === -1 ? "" : name.slice(dot).toLowerCase();
  }

  function isImageFile(file) {
    return IMAGE_TYPES.includes(file.type);
  }

  // Grobe Vorprüfung; maßgeblich ist die Inhaltsprüfung des Servers.
  function rejectReason(file, limits) {
    const image = isImageFile(file);
    if (!image && !DOCUMENT_EXTENSIONS.includes(extensionOf(file.name))) {
      if (file.type.startsWith("image/")) {
        return "Bildformat nicht unterstützt (erlaubt: PNG, JPEG, WebP, GIF).";
      }
      return "Dateityp nicht unterstützt (erlaubt: Bilder, PDF, DOCX, TXT, MD, CSV).";
    }
    if (!file.size) {
      return "Die Datei ist leer.";
    }
    const max = image ? limits.image : limits.document;
    if (max && file.size > max) {
      return `Die Datei ist zu groß (höchstens ${formatSize(max)}).`;
    }
    return "";
  }

  // Eingefügte Bilder heißen oft nur „image.png“: lesbarer Name mit Zeitstempel.
  function pastedName(file, index) {
    const generic = !file.name || /^image\.(png|jpe?g|gif|webp)$/i.test(file.name);
    if (!generic) {
      return file;
    }
    const ext = { "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif" }[file.type] || "png";
    const now = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    const stamp =
      `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ` +
      `${pad(now.getHours())}.${pad(now.getMinutes())}.${pad(now.getSeconds())}`;
    const suffix = index ? ` (${index + 1})` : "";
    return new File([file], `Eingefügtes Bild ${stamp}${suffix}.${ext}`, { type: file.type });
  }

  // Dateien aus der Zwischenablage. Liegt auch Text vor (z. B. Zellen aus einer
  // Tabellenkalkulation, die zusätzlich als Bild kopiert werden), gewinnt der Text.
  function clipboardFiles(data) {
    if (!data) {
      return [];
    }
    const text = data.getData ? data.getData("text/plain") : "";
    if (text && text.trim()) {
      return [];
    }
    let files = Array.from(data.files || []);
    if (!files.length && data.items) {
      files = Array.from(data.items)
        .filter((item) => item.kind === "file")
        .map((item) => item.getAsFile())
        .filter(Boolean);
    }
    return files.map(pastedName);
  }

  function hasFiles(dataTransfer) {
    return Boolean(dataTransfer) && Array.from(dataTransfer.types || []).includes("Files");
  }

  function parseJson(text) {
    try {
      return JSON.parse(text);
    } catch {
      return null;
    }
  }

  function uploadError(xhr) {
    const data = parseJson(xhr.responseText);
    if (data && typeof data.error === "string" && data.error) {
      return data.error;
    }
    if (xhr.status === 413) {
      return "Die Datei ist zu groß.";
    }
    if (xhr.status === 415) {
      return "Dateityp nicht unterstützt.";
    }
    if (xhr.status === 403) {
      return "Dafür fehlt dir die Berechtigung.";
    }
    return `Hochladen fehlgeschlagen (HTTP ${xhr.status}).`;
  }

  // Anhang aus der API-Antwort bzw. aus einer gerenderten Nachricht.
  function fromApi(data) {
    return {
      id: Number(data.id),
      kind: data.kind === "image" ? "image" : "file",
      name: String(data.name ?? ""),
      size: Number(data.size) || 0,
      url: typeof data.url === "string" ? data.url : "",
      thumbnailUrl: typeof data.thumbnail_url === "string" ? data.thumbnail_url : "",
      width: Number(data.width) || 0,
      height: Number(data.height) || 0,
    };
  }

  function fromElement(el) {
    return {
      id: Number(el.dataset.attachmentId),
      kind: el.dataset.kind === "image" ? "image" : "file",
      name: el.dataset.name || "",
      size: Number(el.dataset.size) || 0,
      url: el.dataset.url || "",
      thumbnailUrl: el.dataset.thumbnailUrl || "",
      width: Number(el.dataset.width) || 0,
      height: Number(el.dataset.height) || 0,
    };
  }

  // Nur eigene, relative Pfade als Bild-/Download-Ziel.
  function safePath(url) {
    return typeof url === "string" && url.startsWith("/") && !url.startsWith("//") && !url.startsWith("/\\")
      ? url
      : "";
  }

  // Liste wie chat/_attachment_list.html (für die Live-Anzeige nach dem Senden).
  function buildMessageList(attachments) {
    const list = node("ul", "message-attachments");
    list.setAttribute("aria-label", "Anhänge");
    // Erst die Bilder, dann die Dateien (wie das Template).
    const ordered = [
      ...attachments.filter((att) => att.kind === "image"),
      ...attachments.filter((att) => att.kind !== "image"),
    ];
    for (const att of ordered) {
      const item = node("li", `message-attachment message-attachment-${att.kind}`);
      item.dataset.attachmentId = String(att.id);
      item.dataset.kind = att.kind;
      item.dataset.name = att.name;
      item.dataset.size = String(att.size);
      item.dataset.url = safePath(att.url);
      item.dataset.thumbnailUrl = safePath(att.thumbnailUrl);
      if (att.width) {
        item.dataset.width = String(att.width);
        item.dataset.height = String(att.height);
      }
      const url = safePath(att.url);
      if (att.kind === "image") {
        const link = node("a", "attachment-thumb");
        link.href = url;
        link.dataset.attachmentImage = "";
        link.setAttribute("aria-label", `Bild ${att.name} vergrößern`);
        const img = node("img");
        img.src = safePath(att.thumbnailUrl) || url;
        img.alt = att.name;
        img.loading = "lazy";
        if (att.width && att.height) {
          img.width = att.width;
          img.height = att.height;
        }
        link.append(img);
        item.append(link);
      } else {
        const link = node("a", "attachment-chip");
        link.href = url;
        link.setAttribute("download", "");
        link.append(
          fileIcon(),
          node("span", "attachment-chip-name", att.name),
          " ",
          node("span", "attachment-chip-size", formatSize(att.size)),
          node("span", "sr-only", " (herunterladen)"),
        );
        item.append(link);
      }
      list.append(item);
    }
    return list;
  }

  // Anhänge in einer Nachricht (live) anzeigen bzw. ersetzen.
  function showInMessage(article, attachments) {
    article.querySelector(":scope > .message-attachments")?.remove();
    if (!attachments.length) {
      return;
    }
    const content = article.querySelector(".chat-message-content");
    const list = buildMessageList(attachments);
    if (content) {
      content.after(list);
    } else {
      article.append(list);
    }
  }

  function attachmentsOf(article) {
    return Array.from(
      article.querySelectorAll(":scope > .message-attachments > [data-attachment-id]"),
      fromElement,
    );
  }

  // --- Vorschauleiste (Eingabe und Editor) ---------------------------------------
  // Ein Eintrag: {key, file?, att?, state: uploading|done|error, existing, xhr,
  // objectUrl, el}. existing = Anhang einer schon gesendeten Nachricht (Editor).

  let keyCounter = 0;

  function createTray(options) {
    const { list, announce, uploadUrl, deleteUrl, conversationId, limits, onChange } = options;
    let items = [];
    let waiters = [];

    function changed() {
      list.hidden = !items.length;
      if (!items.some((i) => i.state === "uploading")) {
        const resolve = waiters;
        waiters = [];
        for (const fn of resolve) {
          fn();
        }
      }
      onChange?.();
    }

    function setProgress(item, percent) {
      const bar = item.el.querySelector("progress");
      if (bar) {
        bar.value = percent;
      }
      const meta = item.el.querySelector(".attach-item-meta");
      meta.textContent = `${formatSize(item.size)} · wird hochgeladen … ${percent} %`;
    }

    function render(item) {
      const name = item.att ? item.att.name : item.file.name;
      item.size = item.att ? item.att.size : item.file.size;
      const li = node("li", "attach-item");
      li.dataset.state = item.state;
      const preview = node("span", "attach-item-preview");
      const isImage = item.att ? item.att.kind === "image" : isImageFile(item.file);
      if (isImage) {
        const img = node("img");
        img.alt = "";
        if (item.att && safePath(item.att.thumbnailUrl)) {
          img.src = safePath(item.att.thumbnailUrl);
        } else if (item.file) {
          item.objectUrl = URL.createObjectURL(item.file);
          img.src = item.objectUrl;
        }
        preview.append(img);
      } else {
        preview.append(fileIcon());
      }
      const info = node("span", "attach-item-info");
      const title = node("span", "attach-item-name", name);
      title.title = name;
      const meta = node("span", "attach-item-meta", formatSize(item.size));
      info.append(title, meta);
      if (item.state === "uploading") {
        const bar = node("progress", "attach-item-progress");
        bar.max = 100;
        bar.value = 0;
        bar.setAttribute("aria-label", `Hochladen von ${name}`);
        info.append(bar);
      }
      const error = node("span", "attach-item-error");
      error.hidden = true;
      info.append(error);
      const remove = node("button", "icon-button attach-item-remove");
      remove.type = "button";
      remove.setAttribute("aria-label", `Anhang ${name} entfernen`);
      remove.title = "Entfernen";
      const cross = node("span", "", "×");
      cross.setAttribute("aria-hidden", "true");
      remove.append(cross);
      remove.addEventListener("click", () => removeItem(item, true));
      li.append(preview, info, remove);
      item.el = li;
      item.name = name;
      return li;
    }

    function fail(item, message) {
      item.state = "error";
      item.xhr = null;
      item.el.dataset.state = "error";
      item.el.querySelector("progress")?.remove();
      item.el.querySelector(".attach-item-meta").textContent = formatSize(item.size);
      const error = item.el.querySelector(".attach-item-error");
      error.textContent = message;
      error.hidden = false;
      item.el.querySelector(".attach-item-remove").setAttribute("aria-label", `Hinweis zu ${item.name} ausblenden`);
      announce(`${item.name}: ${message}`, true);
      changed();
    }

    function upload(item) {
      const xhr = new XMLHttpRequest();
      item.xhr = xhr;
      const form = new FormData();
      form.append("file", item.file, item.file.name);
      const conv = conversationId();
      if (conv) {
        form.append("conversation", conv);
      }
      xhr.open("POST", uploadUrl);
      xhr.setRequestHeader("X-CSRFToken", MultiGPT.csrfToken());
      xhr.setRequestHeader("Accept", "application/json");
      xhr.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable && item.state === "uploading") {
          setProgress(item, Math.min(99, Math.round((event.loaded / event.total) * 100)));
        }
      });
      xhr.addEventListener("load", () => {
        if (item.state !== "uploading") {
          return;
        }
        const data = parseJson(xhr.responseText);
        if (xhr.status !== 201 && xhr.status !== 200) {
          fail(item, uploadError(xhr));
          return;
        }
        if (!data || data.id == null) {
          fail(item, "Unerwartete Antwort des Servers.");
          return;
        }
        item.att = fromApi(data);
        item.state = "done";
        item.xhr = null;
        item.el.dataset.state = "done";
        item.el.querySelector("progress")?.remove();
        item.el.querySelector(".attach-item-meta").textContent = formatSize(item.att.size || item.size);
        const img = item.el.querySelector(".attach-item-preview img");
        if (img && safePath(item.att.thumbnailUrl)) {
          img.src = safePath(item.att.thumbnailUrl);
          if (item.objectUrl) {
            URL.revokeObjectURL(item.objectUrl);
            item.objectUrl = null;
          }
        }
        // Hinweis des Servers (z. B. Text gekürzt, gescanntes PDF ohne Text).
        if (typeof data.notice === "string" && data.notice) {
          const notice = item.el.querySelector(".attach-item-error");
          notice.textContent = data.notice;
          notice.classList.add("is-notice");
          notice.hidden = false;
          announce(`${item.name} hochgeladen. Hinweis: ${data.notice}`);
        } else {
          announce(`${item.name} hochgeladen.`);
        }
        changed();
      });
      xhr.addEventListener("error", () => {
        if (item.state === "uploading") {
          fail(item, "Der Server ist nicht erreichbar.");
        }
      });
      xhr.send(form);
    }

    function add(files) {
      const maxCount = limits.count || DEFAULT_MAX_PER_MESSAGE;
      let room = maxCount - items.filter((i) => i.state !== "error").length;
      let first = null;
      for (const file of files) {
        const item = { key: ++keyCounter, file, state: "uploading", existing: false };
        let reason = rejectReason(file, limits);
        if (!reason && room <= 0) {
          reason = `Höchstens ${maxCount} Anhänge je Nachricht.`;
        }
        list.append(render(item));
        items.push(item);
        first = first || item;
        if (reason) {
          fail(item, reason);
          continue;
        }
        room -= 1;
        announce(`${item.name} wird hochgeladen …`);
        upload(item);
      }
      changed();
      return first;
    }

    function addExisting(attachments) {
      for (const att of attachments) {
        const item = { key: ++keyCounter, att, state: "done", existing: true };
        list.append(render(item));
        items.push(item);
      }
      changed();
    }

    function dispose(item, deleteDraft) {
      if (item.xhr) {
        item.xhr.abort();
        item.xhr = null;
      }
      if (item.objectUrl) {
        URL.revokeObjectURL(item.objectUrl);
        item.objectUrl = null;
      }
      if (deleteDraft && item.state === "done" && !item.existing && item.att && deleteUrl) {
        // Entwurf auf dem Server löschen (sonst räumt der Server ihn später auf).
        fetch(MultiGPT.fillTemplate(deleteUrl, item.att.id), {
          method: "DELETE",
          credentials: "same-origin",
          headers: { "X-CSRFToken": MultiGPT.csrfToken(), Accept: "application/json" },
        }).catch(() => {});
      }
    }

    function removeItem(item, byUser) {
      const index = items.indexOf(item);
      if (index === -1) {
        return;
      }
      const wasError = item.state === "error";
      dispose(item, true);
      item.state = "removed";
      items.splice(index, 1);
      item.el.remove();
      if (byUser) {
        const next = items[index] || items[index - 1];
        (next ? next.el.querySelector(".attach-item-remove") : options.fallbackFocus?.())?.focus();
        if (!wasError) {
          announce(`${item.name} entfernt.`);
        }
      }
      changed();
    }

    return {
      add,
      addExisting,
      items: () => items.slice(),
      count: () => items.filter((i) => i.state !== "error").length,
      busy: () => items.some((i) => i.state === "uploading"),
      hasImages: () =>
        items.some(
          (i) => i.state !== "error" && (i.att ? i.att.kind === "image" : isImageFile(i.file)),
        ),
      done: () => items.filter((i) => i.state === "done").map((i) => i.att),
      ids: () => items.filter((i) => i.state === "done").map((i) => i.att.id),
      waitIdle() {
        if (!items.some((i) => i.state === "uploading")) {
          return Promise.resolve();
        }
        return new Promise((resolve) => waiters.push(resolve));
      },
      // Leiste leeren, ohne Entwürfe zu löschen (sie gehen mit der Nachricht raus).
      detach() {
        const taken = items.filter((i) => i.state === "done");
        for (const item of items) {
          dispose(item, false);
          item.el.remove();
        }
        items = [];
        changed();
        return taken.map((i) => ({ att: i.att, existing: i.existing }));
      },
      // Wieder einsetzen (Senden abgelehnt).
      restore(taken) {
        for (const { att, existing } of taken) {
          const item = { key: ++keyCounter, att, state: "done", existing };
          list.append(render(item));
          items.push(item);
        }
        changed();
      },
      // Alles verwerfen (Editor abgebrochen): neue Entwürfe löschen.
      discard() {
        for (const item of items) {
          dispose(item, true);
        }
        items = [];
        list.replaceChildren();
        changed();
      },
    };
  }

  // --- Lightbox für Bilder in Nachrichten ------------------------------------------

  function setupLightbox() {
    let dialog = null;
    let gallery = [];
    let index = 0;
    let opener = null;

    function build() {
      dialog = node("dialog", "attach-lightbox");
      dialog.setAttribute("aria-labelledby", "attach-lightbox-title");
      const head = node("div", "attach-lightbox-head");
      const title = node("p", "attach-lightbox-title");
      title.id = "attach-lightbox-title";
      const counter = node("span", "attach-lightbox-counter");
      const open = node("a", "button button-small attach-lightbox-open", "Original öffnen");
      open.target = "_blank";
      open.rel = "noopener";
      const close = node("button", "button button-small attach-lightbox-close", "Schließen");
      close.type = "button";
      head.append(title, counter, open, close);
      const figure = node("figure", "attach-lightbox-figure");
      const img = node("img", "attach-lightbox-image");
      figure.append(img);
      const prev = node("button", "icon-button attach-lightbox-nav attach-lightbox-prev");
      prev.type = "button";
      prev.setAttribute("aria-label", "Vorheriges Bild");
      prev.append(node("span", "", "‹"));
      const next = node("button", "icon-button attach-lightbox-nav attach-lightbox-next");
      next.type = "button";
      next.setAttribute("aria-label", "Nächstes Bild");
      next.append(node("span", "", "›"));
      for (const button of [prev, next]) {
        button.firstChild.setAttribute("aria-hidden", "true");
      }
      const hint = node("p", "sr-only", "Pfeiltasten wechseln das Bild, Escape schließt.");
      dialog.append(head, figure, prev, next, hint);
      document.body.append(dialog);
      close.addEventListener("click", () => dialog.close());
      prev.addEventListener("click", () => show(index - 1));
      next.addEventListener("click", () => show(index + 1));
      // Klick auf den abgedunkelten Rand schließt.
      dialog.addEventListener("click", (event) => {
        if (event.target === dialog || event.target === figure) {
          dialog.close();
        }
      });
      dialog.addEventListener("keydown", (event) => {
        if (event.key === "ArrowLeft") {
          event.preventDefault();
          show(index - 1);
        } else if (event.key === "ArrowRight") {
          event.preventDefault();
          show(index + 1);
        }
      });
      dialog.addEventListener("close", () => {
        img.removeAttribute("src");
        opener?.focus({ preventScroll: true });
        opener = null;
      });
    }

    function show(i) {
      if (!gallery.length) {
        return;
      }
      index = (i + gallery.length) % gallery.length;
      const att = gallery[index];
      const img = dialog.querySelector(".attach-lightbox-image");
      img.src = safePath(att.url);
      img.alt = att.name;
      dialog.querySelector(".attach-lightbox-title").textContent = att.name;
      dialog.querySelector(".attach-lightbox-counter").textContent =
        gallery.length > 1 ? `Bild ${index + 1} von ${gallery.length}` : "";
      dialog.querySelector(".attach-lightbox-open").href = safePath(att.url);
      for (const nav of dialog.querySelectorAll(".attach-lightbox-nav")) {
        nav.hidden = gallery.length < 2;
      }
    }

    document.addEventListener("click", (event) => {
      const link = event.target.closest?.("a[data-attachment-image]");
      if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey) {
        return;
      }
      const list = link.closest(".message-attachments");
      const own = link.closest("[data-attachment-id]");
      if (!list || !own) {
        return;
      }
      event.preventDefault();
      if (!dialog) {
        build();
      }
      gallery = Array.from(list.querySelectorAll(".message-attachment-image"), fromElement);
      opener = link;
      const start = gallery.findIndex((a) => a.id === Number(own.dataset.attachmentId));
      show(Math.max(0, start));
      dialog.showModal();
      dialog.querySelector(".attach-lightbox-close").focus();
    });
  }

  // --- Seite ------------------------------------------------------------------------

  // Platzhalter, damit chat.js auch ohne Eingabe (nur lesen) fragen kann.
  MultiGPT.attachments = {
    count: () => 0,
    beforeSend: async () => true,
    commit: () => {},
    restore: () => {},
    extend: (payload) => payload,
    attachEditor: () => null,
    showInMessage,
    attachmentsOf,
  };

  document.addEventListener("DOMContentLoaded", () => {
    setupLightbox();

    const chat = document.getElementById("chat");
    const form = document.getElementById("chat-form");
    const list = document.getElementById("attach-tray");
    const input = document.getElementById("attach-input");
    const button = document.getElementById("attach-button");
    if (!chat || !form || !list || !input || !button) {
      return;
    }
    const textarea = document.getElementById("message-input");
    const modelSelect = document.getElementById("model-select");
    const visionHint = document.getElementById("attach-vision-hint");
    const liveRegion = document.getElementById("attach-live");
    const dropZone = document.getElementById("attach-drop");
    const uploadUrl = chat.dataset.apiAttachments;
    const deleteUrl = chat.dataset.apiAttachmentTemplate;
    const limits = {
      image: Number(form.dataset.maxImageBytes) || 0,
      document: Number(form.dataset.maxDocumentBytes) || 0,
      count: Number(form.dataset.maxAttachments) || 0,
    };

    function announce(text, isError = false) {
      liveRegion.textContent = text;
      if (isError) {
        MultiGPT.chatView?.setStatus(text, true);
      }
    }

    const conversationId = () => chat.dataset.conversationId || "";

    // --- Bildfähigkeit des Modells ---

    function visionOf(id) {
      const option = modelSelect?.querySelector(`option[value="${CSS.escape(String(id))}"]`);
      // Unbekannt (Liste noch nicht geladen): nicht sperren, der Server prüft.
      return !option || option.dataset.vision !== "false";
    }

    function targetModels() {
      if (MultiGPT.compare?.isActive()) {
        return Array.from(
          document.querySelectorAll("#compare-models-list input:checked:not(:disabled)"),
          (i) => ({ id: i.value, name: i.dataset.name || "" }),
        );
      }
      const option = modelSelect?.selectedOptions[0];
      return option && option.value ? [{ id: option.value, name: option.dataset.name || option.textContent }] : [];
    }

    function blindModels() {
      return targetModels().filter((m) => !visionOf(m.id));
    }

    function visionText(models) {
      const names = models.map((m) => `„${m.name}“`).join(", ");
      return models.length > 1
        ? `Die Modelle ${names} können keine Bilder verarbeiten. Bitte andere Modelle wählen oder die Bilder entfernen.`
        : `Das Modell ${names} kann keine Bilder verarbeiten. Bitte ein anderes Modell wählen oder die Bilder entfernen.`;
    }

    function updateVisionHint() {
      if (!visionHint) {
        return;
      }
      const blind = tray.hasImages() ? blindModels() : [];
      visionHint.hidden = !blind.length;
      visionHint.textContent = blind.length ? visionText(blind) : "";
    }

    let tray = null;
    tray = createTray({
      list,
      announce,
      uploadUrl,
      deleteUrl,
      conversationId,
      limits,
      onChange: () => {
        form.classList.toggle("has-attachments", tray ? tray.count() > 0 : false);
        document.dispatchEvent(new CustomEvent("multigpt:attachments-changed"));
        if (tray) {
          updateVisionHint();
        }
      },
      fallbackFocus: () => textarea,
    });

    // --- Quellen: Büroklammer, Einfügen, Ablegen ---

    button.addEventListener("click", () => input.click());
    input.addEventListener("change", () => {
      const files = Array.from(input.files || []);
      input.value = "";
      if (files.length) {
        tray.add(files);
        textarea.focus();
      }
    });

    function pasteInto(target, event) {
      const files = clipboardFiles(event.clipboardData);
      if (!files.length) {
        return;
      }
      event.preventDefault();
      target.add(files);
    }

    textarea.addEventListener("paste", (event) => pasteInto(tray, event));

    // Ablegen: im offenen Editor in dessen Leiste, sonst in die Eingabe.
    let dragDepth = 0;
    function showDrop(on) {
      if (dropZone) {
        dropZone.hidden = !on;
      }
      chat.classList.toggle("is-dropping", on);
    }
    chat.addEventListener("dragenter", (event) => {
      if (!hasFiles(event.dataTransfer)) {
        return;
      }
      event.preventDefault();
      dragDepth += 1;
      showDrop(true);
    });
    chat.addEventListener("dragover", (event) => {
      if (!hasFiles(event.dataTransfer)) {
        return;
      }
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
    });
    chat.addEventListener("dragleave", () => {
      dragDepth = Math.max(0, dragDepth - 1);
      if (!dragDepth) {
        showDrop(false);
      }
    });
    chat.addEventListener("drop", (event) => {
      dragDepth = 0;
      showDrop(false);
      if (!hasFiles(event.dataTransfer)) {
        return;
      }
      event.preventDefault();
      const files = Array.from(event.dataTransfer.files || []);
      if (!files.length) {
        return;
      }
      const editorBox = event.target.closest?.(".message-editor");
      const editorTray = editorBox && editorTrays.get(editorBox);
      if (editorTray) {
        editorTray.tray.add(files);
      } else {
        tray.add(files);
        textarea.focus();
      }
    });
    // Daneben fallen gelassene Dateien nicht im Browser öffnen.
    window.addEventListener("dragover", (event) => {
      if (hasFiles(event.dataTransfer) && !chat.contains(event.target)) {
        event.preventDefault();
        event.dataTransfer.dropEffect = "none";
      }
    });
    window.addEventListener("drop", (event) => {
      if (hasFiles(event.dataTransfer) && !chat.contains(event.target)) {
        event.preventDefault();
      }
    });

    modelSelect?.addEventListener("change", updateVisionHint);
    document.addEventListener("multigpt:models-loaded", updateVisionHint);
    document.getElementById("compare-toggle")?.addEventListener("change", updateVisionHint);
    document.getElementById("compare-models-list")?.addEventListener("change", updateVisionHint);

    // --- Senden ---

    let outgoing = []; // [{att, existing}] der zuletzt gesendeten Nachricht
    let waiting = false;

    // Wartet auf laufende Uploads und prüft die Bildfähigkeit. false = nicht senden.
    async function waitAndCheck(target, models) {
      while (target.busy()) {
        MultiGPT.chatView?.setStatus("Warte, bis alle Anhänge hochgeladen sind …");
        await target.waitIdle();
      }
      const imageModels = target.hasImages() ? models().filter((m) => !visionOf(m.id)) : [];
      if (imageModels.length) {
        MultiGPT.chatView?.setStatus(visionText(imageModels), true);
        return false;
      }
      return true;
    }

    MultiGPT.attachments = {
      showInMessage,
      attachmentsOf,
      count: () => tray.count(),
      async beforeSend() {
        if (waiting) {
          return false;
        }
        waiting = true;
        form.classList.add("is-waiting");
        try {
          const ok = await waitAndCheck(tray, targetModels);
          if (ok) {
            MultiGPT.chatView?.setStatus("");
          }
          return ok;
        } finally {
          waiting = false;
          form.classList.remove("is-waiting");
        }
      },
      // Anhänge gehen mit dieser Nachricht raus: Leiste leeren, live anzeigen.
      commit(userEl) {
        outgoing = tray.detach();
        if (userEl) {
          showInMessage(
            userEl,
            outgoing.map((o) => o.att),
          );
        }
      },
      restore() {
        if (outgoing.length) {
          tray.restore(outgoing);
          outgoing = [];
        }
      },
      // Nur für neue Nachrichten (nicht Neu erzeugen, nicht Bearbeiten).
      extend(payload) {
        if (
          outgoing.length &&
          typeof payload.content === "string" &&
          !payload.regenerate &&
          payload.edit_of === undefined
        ) {
          return { ...payload, attachments: outgoing.map((o) => o.att.id) };
        }
        return payload;
      },
      attachEditor,
    };

    // --- Bearbeiten-Editor ---

    const editorTrays = new WeakMap(); // box -> {tray}

    // Leiste im Editor: bisherige Anhänge der Nachricht (entfernbar), neue per
    // Büroklammer, Einfügen und Ablegen. restored: Stand nach abgelehntem Senden.
    function attachEditor(article, box, area, restored) {
      const wrap = node("div", "message-editor-attachments");
      const editorList = node("ul", "attach-tray");
      editorList.setAttribute("aria-label", "Anhänge der Nachricht");
      editorList.hidden = true;
      const editorInput = node("input");
      editorInput.type = "file";
      editorInput.multiple = true;
      editorInput.accept = input.accept;
      editorInput.hidden = true;
      editorInput.tabIndex = -1;
      const add = node("button", "button button-small attach-editor-button");
      add.type = "button";
      add.innerHTML = PAPERCLIP_ICON;
      add.append(node("span", "", "Datei anhängen"));
      const hint = node("p", "attach-vision-hint chat-message-status chat-message-status-warning");
      hint.hidden = true;
      wrap.append(editorList, add, editorInput, hint);
      const actions = box.querySelector(".message-editor-actions");
      box.insertBefore(wrap, actions);

      let editorTray = null;
      editorTray = createTray({
        list: editorList,
        announce,
        uploadUrl,
        deleteUrl,
        conversationId,
        limits,
        onChange: () => {
          if (!editorTray) {
            return;
          }
          const blind = editorTray.hasImages() ? blindModels() : [];
          hint.hidden = !blind.length;
          hint.textContent = blind.length ? visionText(blind) : "";
        },
        fallbackFocus: () => area,
      });
      if (restored) {
        editorTray.restore(restored);
      } else {
        editorTray.addExisting(attachmentsOf(article));
      }
      add.addEventListener("click", () => editorInput.click());
      editorInput.addEventListener("change", () => {
        const files = Array.from(editorInput.files || []);
        editorInput.value = "";
        if (files.length) {
          editorTray.add(files);
          area.focus();
        }
      });
      area.addEventListener("paste", (event) => pasteInto(editorTray, event));
      editorTrays.set(box, { tray: editorTray });

      let released = false;
      return {
        count: () => editorTray.count(),
        // Vor dem Senden: Uploads abwarten, Bildfähigkeit prüfen.
        ready: () => waitAndCheck(editorTray, targetModels),
        ids: () => editorTray.ids(),
        attachments: () => editorTray.done(),
        // Senden: Entwürfe gehören jetzt zur neuen Nachricht.
        release() {
          released = true;
          return editorTray.items().filter((i) => i.state === "done").map((i) => ({ att: i.att, existing: i.existing }));
        },
        // Editor geschlossen: neue Entwürfe verwerfen (außer nach release()).
        close() {
          if (!released) {
            editorTray.discard();
          }
        },
      };
    }
  });
})();
