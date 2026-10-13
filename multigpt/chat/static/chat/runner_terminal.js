// MultiGPT – Terminal einer Runner-Sitzung im Browser (M14, Entscheidung 8p).
// xterm.js (lokal, chat/vendor/xterm) zeigt den PTY-Strom; er läuft immer über
// MultiGPT (SSE /runner/api/sitzungen/<uuid>/pty/), nie direkt zum Runner.
// Eingaben gehen gesammelt per POST zurück. Ausgaben sind nicht vertrauenswürdig:
// xterm stellt sie nur als Terminaltext dar (kein HTML).
// Schnittstelle: window.MultiGPT.runnerTerminal.open({...}) -> {close()}; auf der
// Sitzungsseite verdrahtet sich der Knopf [data-runner-terminal-open] selbst.
"use strict";

(() => {
  const INPUT_DELAY = 25; // ms: Tastendrücke bündeln
  const RESIZE_DELAY = 200;

  function decode(b64) {
    const text = atob(b64 || "");
    const bytes = new Uint8Array(text.length);
    for (let i = 0; i < text.length; i += 1) {
      bytes[i] = text.charCodeAt(i);
    }
    return bytes;
  }

  function fill(template, pty) {
    return template.replace("PTY", encodeURIComponent(pty));
  }

  // Lädt xterm.js bei Bedarf nach (Pfade aus data-Attributen, keine CDNs).
  function ensureXterm(urls) {
    if (window.Terminal && window.FitAddon) {
      return Promise.resolve();
    }
    const load = (src) =>
      new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = src;
        script.onload = resolve;
        script.onerror = () => reject(new Error("xterm.js konnte nicht geladen werden."));
        document.head.append(script);
      });
    if (urls.css && !document.querySelector(`link[href="${urls.css}"]`)) {
      const link = document.createElement("link");
      link.rel = "stylesheet";
      link.href = urls.css;
      document.head.append(link);
    }
    return load(urls.js).then(() => load(urls.fit));
  }

  function open(options) {
    const { screen, stateEl, streamUrl, inputTemplate, resizeTemplate, onEnd, focusOut } = options;
    const { requestJson } = window.MultiGPT;
    const setState = (text) => {
      if (stateEl) {
        stateEl.textContent = text;
      }
    };
    const style = getComputedStyle(document.documentElement);
    const term = new window.Terminal({
      convertEol: false,
      cursorBlink: true,
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
      fontSize: 14,
      scrollback: 5000,
      screenReaderMode: true, // Ausgabe für Screenreader lesbar
      theme: {
        background: style.getPropertyValue("--terminal-bg").trim() || "#111418",
        foreground: style.getPropertyValue("--terminal-fg").trim() || "#e6e8eb",
      },
    });
    const fit = new window.FitAddon.FitAddon();
    term.loadAddon(fit);
    screen.replaceChildren();
    term.open(screen);
    try {
      fit.fit();
    } catch {
      // Unsichtbar: Standardgröße
    }
    // Strg+Umschalt+F6 verlässt das Terminal (Tab geht sonst an das Programm).
    term.attachCustomKeyEventHandler((event) => {
      if (event.type === "keydown" && event.key === "F6" && event.ctrlKey && event.shiftKey) {
        focusOut?.();
        return false;
      }
      return true;
    });

    let pty = null;
    let ready = false; // erst nach der ersten Ausgabe (Prompt) läuft die Shell sicher
    let closed = false;
    let pending = "";
    let inputTimer = null;
    let resizeTimer = null;

    const url = new URL(streamUrl, window.location.href);
    url.searchParams.set("cols", String(term.cols));
    url.searchParams.set("rows", String(term.rows));
    const source = new EventSource(url.toString(), { withCredentials: true });
    setState("Verbinde …");

    function finish(text) {
      if (closed) {
        return;
      }
      closed = true;
      source.close();
      window.clearTimeout(inputTimer);
      window.clearTimeout(resizeTimer);
      window.removeEventListener("resize", onWindowResize);
      setState(text);
      term.options.disableStdin = true;
      onEnd?.(text);
    }

    async function flushInput() {
      inputTimer = null;
      if (!pty || !pending || closed) {
        return;
      }
      if (!ready) {
        inputTimer = window.setTimeout(flushInput, 100); // Eingabe puffern, bis die Shell da ist
        return;
      }
      const data = pending;
      pending = "";
      try {
        const response = await requestJson("POST", fill(inputTemplate, pty), { data });
        if (!response.ok) {
          finish(`Eingabe abgelehnt (${response.status}).`);
        }
      } catch {
        finish("Verbindung zum Server unterbrochen.");
      }
    }

    function sendResize() {
      resizeTimer = null;
      if (!pty || closed) {
        return;
      }
      requestJson("POST", fill(resizeTemplate, pty), { cols: term.cols, rows: term.rows }).catch(
        () => {},
      );
    }

    function onWindowResize() {
      try {
        fit.fit();
      } catch {
        // ignorieren
      }
    }

    term.onData((data) => {
      pending += data;
      if (!inputTimer) {
        inputTimer = window.setTimeout(flushInput, INPUT_DELAY);
      }
    });
    term.onResize(() => {
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(sendResize, RESIZE_DELAY);
    });
    window.addEventListener("resize", onWindowResize);

    const parse = (event) => {
      try {
        return JSON.parse(event.data || "{}");
      } catch {
        return {};
      }
    };
    source.addEventListener("open", (event) => {
      // Hinweis: auch das eingebaute „open“ der EventSource kommt hier an (ohne data).
      const data = parse(event);
      if (data.pty) {
        pty = String(data.pty);
        setState("Verbunden.");
        term.focus();
        window.setTimeout(() => {
          ready = true; // spätestens nach 3 s, auch ohne Prompt
        }, 3000);
      }
    });
    source.addEventListener("data", (event) => {
      ready = true;
      term.write(decode(parse(event).data));
    });
    source.addEventListener("exit", (event) => {
      const code = parse(event).code;
      finish(code == null ? "Programm beendet." : `Programm beendet (Exit-Code ${code}).`);
    });
    source.addEventListener("end", (event) => {
      finish(parse(event).message || "Terminal geschlossen.");
    });
    source.addEventListener("failure", (event) => {
      finish(parse(event).message || "Fehler im Runner.");
    });
    source.onerror = () => {
      // Kein automatisches Neuverbinden: das öffnete eine neue Shell.
      finish(pty ? "Verbindung getrennt." : "Terminal konnte nicht geöffnet werden.");
    };

    return {
      close() {
        finish("Terminal geschlossen.");
        term.dispose();
      },
      fit() {
        onWindowResize();
      },
    };
  }

  // Sitzungsseite: Knopf „Terminal öffnen“ mit dem Bereich #runner-terminal.
  function wireButton(button) {
    const region = document.getElementById(button.getAttribute("aria-controls"));
    if (!region) {
      return;
    }
    const screen = region.querySelector("[data-runner-terminal-screen]");
    const stateEl = region.querySelector("[data-runner-terminal-state]");
    const closeButton = region.querySelector("[data-runner-terminal-close]");
    let current = null;
    const stop = () => {
      current?.close();
      current = null;
      region.hidden = true;
      button.setAttribute("aria-expanded", "false");
      button.focus();
    };
    button.setAttribute("aria-expanded", "false");
    button.addEventListener("click", async () => {
      if (current) {
        current.fit();
        return;
      }
      region.hidden = false;
      button.setAttribute("aria-expanded", "true");
      try {
        await ensureXterm({
          js: button.dataset.xtermJs,
          fit: button.dataset.xtermFit,
          css: button.dataset.xtermCss,
        });
      } catch (err) {
        stateEl.textContent = err.message;
        return;
      }
      current = open({
        screen,
        stateEl,
        streamUrl: button.dataset.ptyUrl,
        inputTemplate: button.dataset.ptyInputTemplate,
        resizeTemplate: button.dataset.ptyResizeTemplate,
        focusOut: () => closeButton.focus(),
      });
    });
    closeButton?.addEventListener("click", stop);
  }

  window.MultiGPT = window.MultiGPT || {};
  window.MultiGPT.runnerTerminal = { open, ensureXterm };

  document.addEventListener("DOMContentLoaded", () => {
    for (const button of document.querySelectorAll("[data-runner-terminal-open]")) {
      wireButton(button);
    }
  });
})();
