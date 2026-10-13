// MultiGPT – Terminal einer Runner-Sitzung im Browser (M14, Entscheidung 8p).
// xterm.js (lokal, chat/vendor/xterm) zeigt den PTY-Strom; er läuft immer über
// MultiGPT, nie direkt zum Runner: bevorzugt über einen WebSocket zum
// Runner-Dienst (/runner/pty/<uuid>, Tastendrücke sofort), sonst über SSE
// (/runner/api/sitzungen/<uuid>/pty/) mit Eingaben per POST. Ausgaben sind nicht vertrauenswürdig:
// xterm stellt sie nur als Terminaltext dar (kein HTML).
// Schnittstelle: window.MultiGPT.runnerTerminal.open({...}) -> {close()}; auf der
// Sitzungsseite verdrahtet sich der Knopf [data-runner-terminal-open] selbst.
"use strict";

(() => {
  const INPUT_DELAY = 25; // ms: Tastendrücke bündeln (nur alter Weg und vor dem Prompt)
  const MAX_RECONNECTS = 3;
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
    const { screen, stateEl, streamUrl, wsUrl, inputTemplate, resizeTemplate, onEnd, focusOut } =
      options;
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
    let transport = null; // {name, input(data), resize(), close()}
    let reconnects = 0;

    function finish(text) {
      if (closed) {
        return;
      }
      closed = true;
      transport?.close();
      window.clearTimeout(inputTimer);
      window.clearTimeout(resizeTimer);
      window.removeEventListener("resize", onWindowResize);
      setState(text);
      term.options.disableStdin = true;
      onEnd?.(text);
    }

    function opened(id) {
      pty = String(id);
      setState("Verbunden.");
      term.focus();
      window.setTimeout(() => {
        ready = true; // spätestens nach 3 s, auch ohne Prompt
      }, 3000);
    }

    function output(b64) {
      ready = true;
      term.write(decode(b64));
    }

    function exited(code) {
      finish(code == null ? "Programm beendet." : `Programm beendet (Exit-Code ${code}).`);
    }

    // --- Direkter WebSocket zum Runner-Dienst (/runner/pty/<uuid>) ---------------
    // Tastendrücke gehen sofort hinaus; die Ausgabe kommt ohne Umweg über die
    // Datenbank. Ist der WebSocket nicht erreichbar (z. B. ohne nginx-Location)
    // oder meldet der Dienst „fallback“, gilt der alte Weg (SSE + POST).
    function startDirect() {
      const url = new URL(wsUrl, window.location.href);
      url.protocol = url.protocol === "https:" || url.protocol === "wss:" ? "wss:" : "ws:";
      url.searchParams.set("cols", String(term.cols));
      url.searchParams.set("rows", String(term.rows));
      let socket;
      try {
        socket = new WebSocket(url.toString());
      } catch {
        startSse();
        return;
      }
      let gotOpen = false;
      let ended = false;
      const send = (frame) => {
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify(frame));
          return true;
        }
        return false;
      };
      transport = {
        name: "direct",
        input(data) {
          if (!send({ type: "input", data })) {
            pending = data + pending; // nach dem Wiederaufbau senden
          }
        },
        resize() {
          send({ type: "resize", cols: term.cols, rows: term.rows });
        },
        close() {
          ended = true;
          send({ type: "close" });
          socket.close(1000);
        },
      };
      socket.onmessage = (event) => {
        let frame;
        try {
          frame = JSON.parse(event.data);
        } catch {
          return;
        }
        if (frame.type === "open") {
          gotOpen = true;
          reconnects = 0;
          opened(frame.pty);
          if (pending) {
            flushInput();
          }
        } else if (frame.type === "data") {
          output(frame.data);
        } else if (frame.type === "exit") {
          ended = true;
          exited(frame.code);
        } else if (frame.type === "end") {
          ended = true;
          finish(frame.message || "Terminal geschlossen.");
        } else if (frame.type === "failure") {
          ended = true;
          if (frame.fallback && !gotOpen) {
            startSse();
          } else {
            finish(frame.message || "Fehler im Runner.");
          }
        }
      };
      socket.onclose = () => {
        if (ended || closed) {
          return;
        }
        if (!gotOpen) {
          // Nie verbunden: Dienst nicht erreichbar → alter Weg.
          startSse();
          return;
        }
        // Verbindung abgebrochen: neu aufbauen (neue Shell).
        pty = null;
        ready = false;
        reconnects += 1;
        if (reconnects > MAX_RECONNECTS) {
          finish("Verbindung getrennt.");
          return;
        }
        setState("Verbindung getrennt – verbinde neu …");
        term.write("\r\n\x1b[2m[Verbindung getrennt – neue Shell]\x1b[0m\r\n");
        window.setTimeout(() => {
          if (!closed) {
            startDirect();
          }
        }, 500 * reconnects);
      };
    }

    // --- Alter Weg: SSE für die Ausgabe, POST für Eingaben ---------------------------
    function startSse() {
      if (closed) {
        return;
      }
      const url = new URL(streamUrl, window.location.href);
      url.searchParams.set("cols", String(term.cols));
      url.searchParams.set("rows", String(term.rows));
      const source = new EventSource(url.toString(), { withCredentials: true });
      transport = {
        name: "sse",
        async input(data) {
          try {
            const response = await requestJson("POST", fill(inputTemplate, pty), { data });
            if (!response.ok) {
              finish(`Eingabe abgelehnt (${response.status}).`);
            }
          } catch {
            finish("Verbindung zum Server unterbrochen.");
          }
        },
        resize() {
          requestJson("POST", fill(resizeTemplate, pty), {
            cols: term.cols,
            rows: term.rows,
          }).catch(() => {});
        },
        close() {
          source.close();
        },
      };
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
          opened(data.pty);
        }
      });
      source.addEventListener("data", (event) => {
        output(parse(event).data);
      });
      source.addEventListener("exit", (event) => {
        exited(parse(event).code);
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
    }

    function flushInput() {
      inputTimer = null;
      if (!pty || !pending || closed || !transport) {
        return;
      }
      if (!ready) {
        inputTimer = window.setTimeout(flushInput, 100); // Eingabe puffern, bis die Shell da ist
        return;
      }
      const data = pending;
      pending = "";
      transport.input(data);
    }

    function sendResize() {
      resizeTimer = null;
      if (!pty || closed || !transport) {
        return;
      }
      transport.resize();
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
      if (inputTimer) {
        return;
      }
      if (transport?.name === "direct" && ready) {
        flushInput(); // sofort senden
      } else {
        inputTimer = window.setTimeout(flushInput, INPUT_DELAY);
      }
    });
    term.onResize(() => {
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(sendResize, RESIZE_DELAY);
    });
    window.addEventListener("resize", onWindowResize);

    setState("Verbinde …");
    if (wsUrl && typeof window.WebSocket === "function") {
      startDirect();
    } else {
      startSse();
    }

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
        wsUrl: button.dataset.ptyWsUrl,
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
