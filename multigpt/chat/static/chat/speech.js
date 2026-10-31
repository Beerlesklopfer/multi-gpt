// MultiGPT – Sprache (M10): Mikrofon im Eingabefeld und Vorlesen unter den Antworten.
//
// Spracheingabe (M10-01): Der Knopf neben der Büroklammer nimmt mit MediaRecorder auf, ein
// zweiter Druck (oder die Höchstlänge) beendet die Aufnahme, Escape verwirft sie. Die Datei geht
// an POST /api/speech/transcribe/, der erkannte Text landet zum Korrigieren im Eingabefeld – er
// wird nie automatisch gesendet. Das Mikrofon braucht eine sichere Verbindung (https oder
// localhost); sonst bleibt der Knopf mit einem Hinweis stehen.
//
// Vorlesen (M10-02): Der Lautsprecherknopf unter einer Antwort fordert die Audiodatei an
// (POST /api/messages/<id>/speak/) und spielt sie ab; gibt es sie schon, wird sie nur abgespielt.
// Mit „Antworten automatisch vorlesen“ (Einstellungen) geschieht das nach jeder neuen Antwort.
// Beides lässt sich allein mit der Tastatur bedienen (echte Buttons, Statusmeldungen über
// MultiGPT.announce). Kein Inline-JS; Namen und Texte nur über textContent.
"use strict";

(() => {
  const chat = document.getElementById("chat");
  if (!chat) {
    return;
  }
  const log = document.getElementById("chat-log");
  const textarea = document.getElementById("message-input");
  const micButton = document.getElementById("mic-button");
  const micTimer = document.getElementById("mic-timer");
  const hasStt = chat.dataset.speechStt === "true";
  const hasTts = chat.dataset.speechTts === "true";
  const autoRead = chat.dataset.speechAuto === "true";
  const maxSeconds = Number(chat.dataset.speechMaxSeconds) || 120;
  const maxBytes = Number(chat.dataset.speechMaxBytes) || 25 * 1024 * 1024;
  const transcribeUrl = chat.dataset.apiSpeechTranscribe;
  const speakTemplate = chat.dataset.apiSpeakTemplate;

  const MSG_INSECURE =
    "Das Mikrofon ist nur über eine sichere Verbindung (https) oder auf localhost verfügbar.";
  const MSG_NO_RECORDER = "Dieser Browser kann keine Aufnahmen machen.";
  const RECORDER_TYPES = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];

  function say(text, isError = false) {
    window.MultiGPT?.announce?.(text, isError);
  }

  // Antwort des Servers auswerten; Fehlertexte kommen deutsch aus {"error"}.
  async function failure(response) {
    if (window.MultiGPT?.errorMessage) {
      return window.MultiGPT.errorMessage(response);
    }
    return `Die Anfrage ist fehlgeschlagen (HTTP ${response.status}).`;
  }

  // --- Spracheingabe ---

  let recorder = null;
  let stream = null;
  let chunks = [];
  let startedAt = 0;
  let tick = null;
  let cancelled = false;
  let busy = false;

  function unsupportedReason() {
    if (!window.isSecureContext) {
      return MSG_INSECURE;
    }
    if (!navigator.mediaDevices?.getUserMedia || typeof window.MediaRecorder === "undefined") {
      return MSG_NO_RECORDER;
    }
    return "";
  }

  function pickType() {
    if (typeof window.MediaRecorder?.isTypeSupported !== "function") {
      return "";
    }
    return RECORDER_TYPES.find((type) => window.MediaRecorder.isTypeSupported(type)) || "";
  }

  function clock(seconds) {
    const whole = Math.max(0, Math.floor(seconds));
    return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
  }

  function setMic(state) {
    const recording = state === "recording";
    micButton.classList.toggle("is-recording", recording);
    micButton.classList.toggle("is-busy", state === "busy");
    micButton.setAttribute("aria-pressed", String(recording));
    micButton.setAttribute("aria-busy", String(state === "busy"));
    const label = recording
      ? "Aufnahme beenden und Text einfügen"
      : state === "busy"
        ? "Text wird erkannt …"
        : "Spracheingabe starten";
    micButton.setAttribute("aria-label", label);
    micButton.title = recording ? "Aufnahme beenden (Escape verwirft sie)" : "Spracheingabe";
    micTimer.hidden = !recording;
  }

  function releaseStream() {
    window.clearInterval(tick);
    tick = null;
    if (stream) {
      for (const track of stream.getTracks()) {
        track.stop();
      }
    }
    stream = null;
  }

  function updateTimer() {
    const seconds = (performance.now() - startedAt) / 1000;
    micTimer.textContent = `${clock(seconds)} / ${clock(maxSeconds)}`;
    if (seconds >= maxSeconds) {
      say(`Höchstlänge von ${maxSeconds} Sekunden erreicht – die Aufnahme wird ausgewertet.`);
      stopRecording();
    }
  }

  async function startRecording() {
    const reason = unsupportedReason();
    if (reason) {
      say(reason, true);
      return;
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (error) {
      const name = error?.name;
      say(
        name === "NotAllowedError" || name === "SecurityError"
          ? "Der Zugriff auf das Mikrofon wurde nicht erlaubt. Bitte im Browser freigeben."
          : name === "NotFoundError"
            ? "Es wurde kein Mikrofon gefunden."
            : "Das Mikrofon lässt sich nicht öffnen.",
        true,
      );
      return;
    }
    const type = pickType();
    try {
      recorder = type ? new window.MediaRecorder(stream, { mimeType: type }) : new window.MediaRecorder(stream);
    } catch {
      releaseStream();
      say(MSG_NO_RECORDER, true);
      return;
    }
    chunks = [];
    cancelled = false;
    recorder.addEventListener("dataavailable", (event) => {
      if (event.data && event.data.size > 0) {
        chunks.push(event.data);
      }
    });
    recorder.addEventListener("stop", onRecorderStop);
    recorder.start();
    startedAt = performance.now();
    setMic("recording");
    updateTimer();
    tick = window.setInterval(updateTimer, 250);
    say("Aufnahme läuft. Zum Beenden den Knopf noch einmal drücken, Escape verwirft sie.");
  }

  function stopRecording() {
    if (recorder && recorder.state !== "inactive") {
      recorder.stop();
    }
  }

  function cancelRecording() {
    cancelled = true;
    stopRecording();
  }

  async function onRecorderStop() {
    const seconds = (performance.now() - startedAt) / 1000;
    const type = recorder?.mimeType || chunks[0]?.type || "audio/webm";
    recorder = null;
    releaseStream();
    if (cancelled) {
      setMic("idle");
      say("Aufnahme verworfen.");
      return;
    }
    const blob = new Blob(chunks, { type });
    chunks = [];
    if (blob.size === 0 || seconds < 0.3) {
      setMic("idle");
      say("Die Aufnahme ist leer. Bitte noch einmal versuchen.", true);
      return;
    }
    if (blob.size > maxBytes) {
      setMic("idle");
      say(`Die Aufnahme ist zu groß (höchstens ${Math.round(maxBytes / 1048576)} MB).`, true);
      return;
    }
    await upload(blob, seconds);
  }

  function extensionFor(type) {
    if (type.includes("ogg")) {
      return "ogg";
    }
    if (type.includes("mp4")) {
      return "m4a";
    }
    return "webm";
  }

  async function upload(blob, seconds) {
    busy = true;
    setMic("busy");
    say("Text wird erkannt …");
    const body = new FormData();
    body.append("file", blob, `aufnahme.${extensionFor(blob.type)}`);
    body.append("duration", seconds.toFixed(2));
    try {
      const response = await fetch(transcribeUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: { Accept: "application/json", "X-CSRFToken": window.MultiGPT.csrfToken() },
        body,
      });
      if (!response.ok) {
        throw new Error(await failure(response));
      }
      const data = await response.json();
      insertText(String(data.text || ""));
      say("Text erkannt. Bitte prüfen, bei Bedarf korrigieren und senden.");
    } catch (error) {
      say(error instanceof TypeError ? "Der Server ist nicht erreichbar." : error.message, true);
    } finally {
      busy = false;
      setMic("idle");
    }
  }

  // Text an der Schreibmarke einfügen (ohne Markierung: ans Ende) und das Feld wie nach Eingabe melden.
  function insertText(text) {
    if (!text || !textarea) {
      return;
    }
    const value = textarea.value;
    const start = textarea.selectionStart ?? value.length;
    const end = textarea.selectionEnd ?? value.length;
    const before = value.slice(0, start);
    const after = value.slice(end);
    const gapBefore = before && !/\s$/.test(before) ? " " : "";
    const gapAfter = after && !/^\s/.test(after) ? " " : "";
    textarea.value = `${before}${gapBefore}${text}${gapAfter}${after}`;
    const caret = (before + gapBefore + text).length;
    textarea.setSelectionRange(caret, caret);
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    textarea.focus();
  }

  if (micButton && hasStt) {
    micButton.hidden = false;
    const reason = unsupportedReason();
    if (reason) {
      micButton.setAttribute("aria-disabled", "true");
      micButton.title = reason;
    }
    micButton.addEventListener("click", () => {
      if (busy) {
        return;
      }
      if (micButton.getAttribute("aria-disabled") === "true") {
        say(unsupportedReason(), true);
      } else if (recorder) {
        stopRecording();
      } else {
        startRecording();
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && recorder) {
        event.preventDefault();
        cancelRecording();
      }
    });
    window.addEventListener("pagehide", () => {
      if (recorder) {
        cancelRecording();
      }
    });
  }

  // --- Vorlesen ---

  let current = null; // gerade spielendes <audio>

  function speakableStatus(article) {
    return ["complete", "aborted"].includes(article.dataset.status);
  }

  // Knöpfe einblenden: nur mit Sprachausgabe-Modell und bei fertigen Antworten mit Nummer.
  function revealSpeakButtons() {
    for (const button of log.querySelectorAll("[data-speak-message]")) {
      const article = button.closest(".chat-message");
      button.hidden = !(hasTts && article && article.dataset.messageId && speakableStatus(article));
    }
  }

  function audioOf(article) {
    return article.querySelector("audio.attachment-audio");
  }

  function wire(audio, button) {
    audio.addEventListener("play", () => {
      if (current && current !== audio) {
        current.pause();
      }
      current = audio;
      button?.classList.add("is-speaking");
    });
    const stopped = () => button?.classList.remove("is-speaking");
    audio.addEventListener("pause", stopped);
    audio.addEventListener("ended", stopped);
  }

  // Audio-Zeile an die Antwort hängen (wie chat/_attachment_item.html, nur mit DOM-Aufrufen).
  function appendAudio(article, attachment, button) {
    let list = article.querySelector(":scope > .message-attachments");
    if (!list) {
      list = document.createElement("ul");
      list.className = "message-attachments";
      list.setAttribute("aria-label", "Anhänge");
      const anchor = article.querySelector(".chat-message-actions");
      article.insertBefore(list, anchor);
    }
    const item = document.createElement("li");
    item.className = "message-attachment message-attachment-file";
    item.dataset.attachmentId = String(attachment.id);
    item.dataset.kind = "file";
    const audio = document.createElement("audio");
    audio.className = "attachment-audio";
    audio.controls = true;
    audio.preload = "none";
    audio.src = attachment.url;
    audio.setAttribute("aria-label", "Vorgelesene Antwort");
    const link = document.createElement("a");
    link.className = "attachment-chip attachment-audio-download";
    link.href = attachment.url;
    link.setAttribute("download", "");
    link.textContent = "Audio laden";
    item.append(audio, link);
    list.append(item);
    wire(audio, button);
    return audio;
  }

  async function play(audio) {
    try {
      await audio.play();
      return true;
    } catch {
      say("Das Abspielen wurde vom Browser blockiert. Bitte den Wiedergabeknopf am Audio drücken.", true);
      return false;
    }
  }

  async function speak(article, button) {
    let audio = audioOf(article);
    if (audio) {
      if (!audio.dataset.wired) {
        audio.dataset.wired = "1";
        wire(audio, button);
      }
      if (audio.paused) {
        await play(audio);
      } else {
        audio.pause();
      }
      return;
    }
    const id = article.dataset.messageId;
    if (!id || !speakTemplate) {
      return;
    }
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    say("Antwort wird vorbereitet …");
    try {
      const response = await fetch(window.MultiGPT.fillTemplate(speakTemplate, id), {
        method: "POST",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-CSRFToken": window.MultiGPT.csrfToken(),
        },
        body: "{}",
      });
      if (!response.ok) {
        throw new Error(await failure(response));
      }
      const data = await response.json();
      audio = audioOf(article) || appendAudio(article, data.attachment, button);
      audio.dataset.wired = "1";
      say(
        data.truncated
          ? "Die Antwort war lang und wird gekürzt vorgelesen."
          : "Die Antwort wird vorgelesen.",
      );
      await play(audio);
    } catch (error) {
      say(error instanceof TypeError ? "Der Server ist nicht erreichbar." : error.message, true);
    } finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }

  if (hasTts && log) {
    log.addEventListener("click", (event) => {
      const button = event.target.closest("[data-speak-message]");
      const article = button?.closest(".chat-message");
      if (button && article && !button.disabled) {
        speak(article, button);
      }
    });
    revealSpeakButtons();
    new MutationObserver(revealSpeakButtons).observe(log, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["data-status", "data-message-id"],
    });
  }

  // --- Automatisch vorlesen ---
  //
  // Nach dem Ende eines Streams in dieser Seitensitzung, sobald der Verlauf zur Ruhe gekommen ist
  // (chat.js ersetzt die Nachrichten nach dem Stream durch den Stand des Servers): die letzte
  // Antwort vorlesen. Beim bloßen Öffnen eines Chats oder Umschalten der Version geschieht nichts.
  if (hasTts && autoRead && log) {
    let armed = false;
    let settle = null;

    const lastAnswer = () => {
      const answers = log.querySelectorAll('.chat-message[data-role="assistant"]');
      return answers.length ? answers[answers.length - 1] : null;
    };

    const readLast = () => {
      settle = null;
      if (!armed || chat.dataset.streaming === "true") {
        return;
      }
      armed = false;
      const article = lastAnswer();
      if (
        !article ||
        !article.dataset.messageId ||
        !speakableStatus(article) ||
        article.querySelector(".message-attachment-image") ||
        !article.querySelector(".chat-message-content")?.textContent.trim()
      ) {
        return;
      }
      const button = article.querySelector("[data-speak-message]");
      if (button && !button.hidden) {
        speak(article, button);
      }
    };

    const later = (ms) => {
      window.clearTimeout(settle);
      settle = window.setTimeout(readLast, ms);
    };

    new MutationObserver(() => {
      if (armed && chat.dataset.streaming !== "true") {
        later(600);
      }
    }).observe(log, { childList: true, subtree: true });

    new MutationObserver(() => {
      if (chat.dataset.streaming === "true") {
        armed = true;
        window.clearTimeout(settle);
      } else if (armed) {
        later(1500); // Rückfall, falls der Verlauf nach dem Stream nicht mehr neu geladen wird
      }
    }).observe(chat, { attributes: true, attributeFilter: ["data-streaming"] });
  }
})();
