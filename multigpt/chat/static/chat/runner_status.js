// MultiGPT – Live-Status auf der Seite „Runner“ (M14-12).
// Fragt alle 5 s /runner/status/ ab (nur das eigene Konto) und aktualisiert
// Verbindung der Runner, Sitzungen und Laufzeit. Pausiert, solange der Tab
// verborgen ist. Alle Namen sind Nutzerdaten: nur textContent.
"use strict";

(() => {
  const DEFAULT_SECONDS = 5;

  function formatTime(iso) {
    if (!iso) {
      return "–";
    }
    const date = new Date(iso);
    return date.toLocaleString("de-DE", {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function minutesText(n) {
    const value = Number(n) || 0;
    return `${value.toLocaleString("de-DE")} ${value === 1 ? "Minute" : "Minuten"}`;
  }

  function cell(text, className) {
    const td = document.createElement("td");
    td.textContent = text;
    if (className) {
      td.className = className;
    }
    return td;
  }

  function link(text, href) {
    const a = document.createElement("a");
    a.href = href;
    a.textContent = text;
    return a;
  }

  function renderSessions(body, sessions) {
    if (!sessions.length) {
      const tr = document.createElement("tr");
      const td = cell("Noch keine Sitzungen.", "hint");
      td.colSpan = 6;
      tr.append(td);
      body.replaceChildren(tr);
      return;
    }
    body.replaceChildren(
      ...sessions.map((s) => {
        const tr = document.createElement("tr");
        if (s.active) {
          tr.className = "is-current";
        }
        const name = document.createElement("th");
        name.scope = "row";
        name.textContent = s.workspace;
        if (s.via_api_key) {
          const note = document.createElement("span");
          note.className = "usage-provider";
          note.textContent = "API-Key";
          name.append(" ", note);
        }
        const state = document.createElement("td");
        const badge = document.createElement("span");
        badge.className = `runner-state runner-state-${s.status}`;
        badge.textContent = s.status_label;
        state.append(badge);
        if (s.stop_reason && !s.active) {
          state.append(` (${s.stop_reason})`);
        }
        const chat = document.createElement("td");
        if (s.conversation) {
          chat.append(link(s.conversation.title, s.conversation.url));
        } else {
          chat.textContent = "–";
        }
        const audit = document.createElement("td");
        audit.append(link("Audit", s.url));
        tr.append(name, state, chat, cell(formatTime(s.started)), cell(String(s.minutes), "num"), audit);
        return tr;
      }),
    );
  }

  function renderRuntime(root, runtime) {
    const box = root.querySelector("[data-runner-runtime]");
    if (!box || !runtime) {
      return;
    }
    box.className = `runner-runtime budget-meter budget-meter-${runtime.level}`;
    const meter = box.querySelector("meter");
    if (meter && runtime.limit_minutes) {
      meter.value = Math.min(runtime.spent_minutes, runtime.limit_minutes);
      meter.textContent = `${runtime.percent} %`;
    }
    const text = box.querySelector("[data-runner-runtime-text]");
    if (text) {
      text.textContent =
        runtime.limit_minutes == null
          ? `${minutesText(runtime.spent_minutes)} verbraucht (unbegrenzt).`
          : `${runtime.spent_minutes.toLocaleString("de-DE")} von ${minutesText(runtime.limit_minutes)} verbraucht (${runtime.percent} %).`;
    }
    const notice = box.querySelector("[data-runner-runtime-notice]");
    if (notice) {
      notice.className = `budget-notice budget-notice-${runtime.level}`;
      notice.textContent = runtime.message || "";
      notice.hidden = !runtime.message;
    }
  }

  // Spalte „Verbindung“ der Tabelle „Deine Runner“ (Seite von core) mitziehen.
  function renderRunners(runners) {
    for (const runner of runners) {
      const el = document.querySelector(`[data-runner-connection="${runner.id}"]`);
      if (el) {
        el.textContent = runner.online
          ? "verbunden"
          : `getrennt${runner.last_seen ? `, zuletzt ${formatTime(runner.last_seen)}` : ""}`;
      }
    }
  }

  function init() {
    const root = document.getElementById("runner-live");
    if (!root || !root.dataset.statusUrl) {
      return;
    }
    const body = root.querySelector("[data-runner-sessions]");
    const updated = root.querySelector("[data-runner-updated]");
    let seconds = DEFAULT_SECONDS;
    let timer = null;
    let failures = 0;
    let busy = false;

    async function poll() {
      timer = null;
      if (document.hidden || busy) {
        return; // weiter bei visibilitychange
      }
      busy = true;
      try {
        const response = await fetch(root.dataset.statusUrl, {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
          cache: "no-store",
        });
        if (!response.ok) {
          throw new Error(String(response.status));
        }
        const data = await response.json();
        failures = 0;
        seconds = Number(data.poll_seconds) || DEFAULT_SECONDS;
        renderSessions(body, Array.isArray(data.sessions) ? data.sessions : []);
        renderRuntime(root, data.runtime);
        renderRunners(Array.isArray(data.runners) ? data.runners : []);
        // Nur Text ändern, nicht ansagen (aria-live polite würde alle 5 s stören).
        updated.textContent = `Stand ${formatTime(data.now)}`;
      } catch {
        failures += 1;
        updated.textContent = "Status gerade nicht abrufbar.";
      } finally {
        busy = false;
      }
      // Bei Fehlern seltener fragen (höchstens alle 60 s).
      timer = window.setTimeout(poll, Math.min(seconds * 1000 * 2 ** Math.min(failures, 4), 60000));
    }

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && timer === null) {
        poll();
      }
    });
    poll();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
