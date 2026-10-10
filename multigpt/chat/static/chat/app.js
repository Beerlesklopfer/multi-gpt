// MultiGPT – Grundfunktionen der Oberfläche. Kein Inline-JS (CSP).
"use strict";

// Gemeinsame Hilfen für sidebar.js, provider_status.js und chat.js.
window.MultiGPT = (() => {
  function readCookie(name) {
    for (const part of document.cookie ? document.cookie.split(";") : []) {
      const [key, ...rest] = part.trim().split("=");
      if (key === name) {
        return decodeURIComponent(rest.join("="));
      }
    }
    return "";
  }

  function csrfToken() {
    const input = document.querySelector("input[name=csrfmiddlewaretoken]");
    return readCookie("csrftoken") || (input ? input.value : "");
  }

  async function errorMessage(response) {
    try {
      const data = await response.json();
      if (data && typeof data.error === "string" && data.error) {
        return data.error;
      }
    } catch {
      // Kein JSON – allgemeine Meldung.
    }
    if (response.status === 403) {
      return "Dafür fehlt dir die Berechtigung.";
    }
    if (response.status === 404) {
      return "Der Chat wurde nicht gefunden.";
    }
    return `Die Anfrage ist fehlgeschlagen (HTTP ${response.status}).`;
  }

  // JSON-Anfrage; liefert die Antwort als Objekt oder wirft Error mit Meldung.
  async function requestJson(method, url, payload) {
    let response;
    try {
      response = await fetch(url, {
        method,
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
          "X-CSRFToken": csrfToken(),
        },
        body: payload === undefined ? undefined : JSON.stringify(payload),
      });
    } catch {
      throw new Error("Der Server ist nicht erreichbar.");
    }
    if (!response.ok) {
      throw new Error(await errorMessage(response));
    }
    return response.json();
  }

  function fillTemplate(template, id) {
    // Vorlagen aus {% url … 0 %}: das einzige Segment "/0/" ersetzen.
    return template.replace("/0/", `/${encodeURIComponent(id)}/`);
  }

  // Meldung über die Statuszeile des Chats, sonst über die globale Zeile.
  let hideTimer = null;
  function announce(text, isError = false) {
    const chatStatus = document.getElementById("chat-status");
    const line = chatStatus || document.getElementById("app-status");
    if (!line) {
      return;
    }
    line.textContent = text;
    line.classList.toggle("is-error", Boolean(text) && isError);
    if (!chatStatus) {
      window.clearTimeout(hideTimer);
      hideTimer = window.setTimeout(() => {
        line.textContent = "";
      }, 8000);
    }
  }

  return { csrfToken, errorMessage, requestJson, fillTemplate, announce };
})();

// Schriftgröße der Nachrichten: Knöpfe „A−“/„A+“ unter den Antworten
// (chat/_message_actions.html). Stufe als CSS-Variable am <html>, im Browser gemerkt.
(() => {
  const STORAGE_KEY = "multigpt.fontScale";
  const STEPS = [0.85, 0.92, 1, 1.1, 1.2, 1.35, 1.5];
  let index = STEPS.indexOf(1);
  try {
    const saved = STEPS.indexOf(Number(localStorage.getItem(STORAGE_KEY)));
    if (saved >= 0) {
      index = saved;
    }
  } catch (e) {
    // kein Zugriff auf localStorage
  }
  const apply = () => {
    document.documentElement.style.setProperty("--chat-font-scale", String(STEPS[index]));
    for (const button of document.querySelectorAll("[data-font-size]")) {
      const step = Number(button.dataset.fontSize);
      button.disabled = index + step < 0 || index + step >= STEPS.length;
    }
  };
  apply();
  document.addEventListener("DOMContentLoaded", apply);
  // Nachgeladene Nachrichten (Streaming, Versionen) bekommen den Zustand beim ersten Klick
  // bzw. über die Delegation hier.
  document.addEventListener("click", (event) => {
    const button = event.target.closest?.("[data-font-size]");
    if (!button) {
      return;
    }
    index = Math.min(STEPS.length - 1, Math.max(0, index + Number(button.dataset.fontSize)));
    try {
      localStorage.setItem(STORAGE_KEY, String(STEPS[index]));
    } catch (e) {
      // nur für diese Seite
    }
    apply();
    window.MultiGPT?.announce?.(`Schriftgröße ${Math.round(STEPS[index] * 100)} %`);
  });
})();

document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.getElementById("sidebar-toggle");
  const sidebar = document.getElementById("sidebar");
  if (!toggle || !sidebar) {
    return;
  }

  // Breit: Seitenleiste ein-/ausklappen (Zustand im Browser gemerkt).
  // Schmal: als Überlagerung öffnen und schließen.
  const wide = window.matchMedia("(min-width: 48.0625rem)");
  const STORAGE_KEY = "multigpt.sidebarCollapsed";
  const setOpen = (open) => {
    document.body.classList.toggle("sidebar-open", open);
    toggle.setAttribute("aria-expanded", String(open));
  };
  const setCollapsed = (collapsed) => {
    document.body.classList.toggle("sidebar-collapsed", collapsed);
    try {
      localStorage.setItem(STORAGE_KEY, collapsed ? "1" : "0");
    } catch (e) {
      // ohne Speicher: nur für diese Seite
    }
  };
  const syncExpanded = () => {
    toggle.setAttribute(
      "aria-expanded",
      String(
        wide.matches
          ? !document.body.classList.contains("sidebar-collapsed")
          : document.body.classList.contains("sidebar-open"),
      ),
    );
  };
  try {
    document.body.classList.toggle("sidebar-collapsed", localStorage.getItem(STORAGE_KEY) === "1");
  } catch (e) {
    // kein Zugriff auf localStorage
  }
  syncExpanded();
  wide.addEventListener("change", syncExpanded);

  toggle.addEventListener("click", () => {
    if (wide.matches) {
      setCollapsed(!document.body.classList.contains("sidebar-collapsed"));
      syncExpanded();
    } else {
      setOpen(!document.body.classList.contains("sidebar-open"));
    }
  });

  // Schließen mit Escape oder Klick außerhalb der Seitenleiste.
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      setOpen(false);
    }
  });
  document.addEventListener("click", (event) => {
    if (
      document.body.classList.contains("sidebar-open") &&
      !sidebar.contains(event.target) &&
      !toggle.contains(event.target)
    ) {
      setOpen(false);
    }
  });
});
