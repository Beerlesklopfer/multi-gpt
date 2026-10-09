// MultiGPT – Online-Anzeige lokaler Anbieter wie LM Studio (M4-04, Plan 8a).
//
// Statuspunkte in der Kopfzeile, Abfrage von /api/providers/status/ alle 30 s,
// aber nur, solange der Tab sichtbar ist. Beim Wechsel online <-> offline gibt
// es einen Hinweis über die Statuszeile (aria-live), und chat.js lädt die
// Modellliste neu (Ereignis "multigpt:provider-status" mit detail.changed).
// Die Leiste rendert der Server nur, wenn es Anbieter mit Statusprüfung gibt.
"use strict";

(() => {
  const POLL_MS = 30000;

  function formatLastOnline(iso) {
    if (!iso) {
      return "";
    }
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) {
      return "";
    }
    const time = date.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
    const today = new Date();
    if (date.toDateString() === today.toDateString()) {
      return `zuletzt online um ${time}`;
    }
    const day = date.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" });
    return `zuletzt online am ${day} um ${time}`;
  }

  function statusText(name, online, lastOnline) {
    if (online) {
      return `${name} online`;
    }
    const last = formatLastOnline(lastOnline);
    return last ? `${name} offline, ${last}` : `${name} offline`;
  }

  document.addEventListener("DOMContentLoaded", () => {
    const bar = document.getElementById("provider-status");
    if (!bar) {
      return;
    }
    const url = bar.dataset.url;
    // Bekannter Stand je Anbieter-ID: {online, models}
    const known = new Map();
    let timer = null;
    let inflight = false;

    function itemFor(provider) {
      let li = bar.querySelector(`li[data-provider-id="${CSS.escape(String(provider.id))}"]`);
      if (!li) {
        li = document.createElement("li");
        li.className = "provider-status-item";
        li.dataset.providerId = String(provider.id);
        const dot = document.createElement("span");
        dot.className = "status-dot";
        dot.setAttribute("aria-hidden", "true");
        const text = document.createElement("span");
        text.className = "provider-status-text";
        li.append(dot, text);
        bar.append(li);
      }
      return li;
    }

    function show(li, name, online, lastOnline) {
      li.dataset.online = String(online);
      li.dataset.name = name;
      const text = statusText(name, online, lastOnline);
      li.querySelector(".provider-status-text").textContent = text;
      li.title = text;
    }

    // Serverseitig gerenderten Stand übernehmen (Zeitformat wie im Client).
    for (const li of bar.querySelectorAll("li[data-provider-id]")) {
      show(li, li.dataset.name, li.dataset.online === "true", li.dataset.lastOnline);
    }

    async function poll() {
      if (inflight || document.hidden) {
        return;
      }
      inflight = true;
      let providers;
      try {
        const response = await fetch(url, {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        if (!response.ok) {
          return;
        }
        providers = await response.json();
      } catch {
        return; // Server kurz nicht erreichbar: beim nächsten Mal wieder.
      } finally {
        inflight = false;
      }
      if (!Array.isArray(providers)) {
        return;
      }
      let changed = false;
      const seen = new Set();
      for (const p of providers) {
        const id = String(p.id);
        seen.add(id);
        const online = p.online === true;
        const models = Array.isArray(p.models) ? p.models.map(String).sort().join("\n") : "";
        const li = itemFor(p);
        const before = known.get(id);
        // Erster Abruf: Stand aus dem HTML als Vergleich, damit ein Wechsel
        // zwischen Seitenaufbau und erster Abfrage auch gemeldet wird.
        const previousOnline = before ? before.online : li.dataset.online === "true";
        show(li, String(p.name ?? ""), online, p.last_online);
        if (previousOnline !== online) {
          changed = true;
          window.MultiGPT.announce(
            `${p.name} ist jetzt ${online ? "online" : "offline"}.`,
            false,
          );
        } else if (before && before.models !== models) {
          changed = true;
        }
        known.set(id, { online, models });
      }
      for (const li of bar.querySelectorAll("li[data-provider-id]")) {
        if (!seen.has(li.dataset.providerId)) {
          li.remove(); // Anbieter deaktiviert oder Statusprüfung abgeschaltet
          changed = true;
        }
      }
      bar.hidden = !bar.querySelector("li");
      document.dispatchEvent(
        new CustomEvent("multigpt:provider-status", { detail: { providers, changed } }),
      );
    }

    function schedule() {
      window.clearInterval(timer);
      timer = null;
      if (!document.hidden) {
        timer = window.setInterval(poll, POLL_MS);
      }
    }

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        poll(); // Zurück im Tab: sofort aktualisieren
      }
      schedule();
    });
    document.addEventListener("multigpt:provider-status-refresh", () => poll());

    schedule();
    poll();
  });
})();
