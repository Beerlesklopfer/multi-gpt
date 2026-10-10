// MultiGPT – Mitscrollen beim Streamen (Verlauf und Vergleichsspalten).
//
// Der Verlauf „klebt“ nur unten, solange der Nutzer unten ist (Schwelle
// STICK_PX). Scrollt er selbst nach oben (Mausrad, Touch, Tasten, Scrollbar),
// folgt die Ansicht nicht mehr, und der Knopf „↓ Neueste“ erscheint; ein Klick
// darauf springt ans Ende und klebt wieder. Früher wurde bei jedem Delta neu
// gemessen, ob der Nutzer unten ist: Das sanfte Scrollen von Firefox ist bei
// den ersten Bildern noch innerhalb der Schwelle, das nächste Delta zog den
// Verlauf also zurück ans Ende, und der Nutzer kam nie davon los.
//
// Maßgeblich ist deshalb die Absicht des Nutzers: Ein Scrollen nach oben zählt
// nur nach einer Eingabe (Rad, Touch, Taste, Zeiger), sonst ist es eine
// Folge geänderten Inhalts (z. B. kürzeres Markdown nach dem Neurendern).
"use strict";

(() => {
  const STICK_PX = 40;
  const INTENT_MS = 1000;
  const NAV_KEYS = new Set(["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "]);

  const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)");

  function isEditable(target) {
    return Boolean(target?.closest?.("input, textarea, select, [contenteditable]"));
  }

  // scroller: das scrollende Element; options.button: Knopf „↓ Neueste“ (optional).
  function scrollFollow(scroller, options = {}) {
    const button = options.button || null;
    let following = true;
    let lastTop = scroller.scrollTop;
    let intentAt = 0;
    let pointerDown = false;

    const distance = () => scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
    const nearBottom = () => distance() <= STICK_PX;
    const userActive = () => pointerDown || performance.now() - intentAt < INTENT_MS;
    const intent = () => {
      intentAt = performance.now();
    };

    function updateButton() {
      if (button) {
        button.hidden = following || distance() <= STICK_PX;
      }
    }

    function toBottom(smooth = false) {
      const top = scroller.scrollHeight;
      if (smooth && !reducedMotion?.matches && scroller.scrollTo) {
        scroller.scrollTo({ top, behavior: "smooth" });
      } else {
        scroller.scrollTop = top;
        lastTop = scroller.scrollTop;
      }
    }

    // Nach neuem Inhalt: unten bleiben, wenn der Nutzer unten ist. Nicht
    // während die Maustaste im Verlauf gedrückt ist (Text markieren, Scrollbar).
    function stick() {
      if (following && !pointerDown) {
        toBottom();
      }
      updateButton();
    }

    // Nach einem Neurendern (markdown.js, requestAnimationFrame): Liest der
    // Nutzer weiter oben, bleibt die Position genau, wo sie war.
    function afterRender() {
      if (following && !pointerDown) {
        toBottom();
      } else if (scroller.scrollTop !== lastTop && !userActive()) {
        scroller.scrollTop = lastTop;
      }
      updateButton();
    }

    // Ans Ende springen und wieder kleben (Senden, „↓ Neueste“).
    function jump(smooth = false) {
      following = true;
      toBottom(smooth);
      updateButton();
    }

    // DOM austauschen (Verlauf nach dem Stream neu laden), ohne die Ansicht zu
    // verschieben: Bezug ist das oberste sichtbare Element mit data-message-id.
    function keepPosition(change, selector = ".chat-message[data-message-id]") {
      if (following) {
        change();
        toBottom();
        updateButton();
        return;
      }
      const top = scroller.getBoundingClientRect().top;
      const anchor = Array.from(scroller.querySelectorAll(selector)).find(
        (el) => !el.hidden && el.getBoundingClientRect().bottom > top,
      );
      const id = anchor?.dataset.messageId;
      const offset = anchor ? anchor.getBoundingClientRect().top - top : 0;
      const before = scroller.scrollTop;
      change();
      const again = id
        ? scroller.querySelector(`[data-message-id="${CSS.escape(id)}"]`)
        : null;
      if (again && !again.hidden) {
        scroller.scrollTop += again.getBoundingClientRect().top - top - offset;
      } else {
        scroller.scrollTop = before;
      }
      lastTop = scroller.scrollTop;
      updateButton();
    }

    scroller.addEventListener(
      "wheel",
      (event) => {
        intent();
        if (event.deltaY < 0 && scroller.scrollTop > 0) {
          following = false; // sofort, auch während sanftes Scrollen noch läuft
          updateButton();
        }
      },
      { passive: true },
    );
    scroller.addEventListener("touchstart", intent, { passive: true });
    scroller.addEventListener("touchmove", intent, { passive: true });
    // Linke Taste: Scrollbar ziehen oder Text markieren. Nach einem Rechtsklick
    // kommt das pointerup beim Browsermenü an, nicht bei der Seite.
    scroller.addEventListener("pointerdown", (event) => {
      intent();
      pointerDown = event.button === 0;
    });
    const release = () => {
      if (pointerDown) {
        pointerDown = false;
        intent();
      }
    };
    for (const type of ["pointerup", "pointercancel", "contextmenu", "dragend", "blur"]) {
      window.addEventListener(type, release);
    }
    document.addEventListener("keydown", (event) => {
      if (NAV_KEYS.has(event.key) && !isEditable(event.target)) {
        intent();
      }
    });

    scroller.addEventListener(
      "scroll",
      () => {
        const top = scroller.scrollTop;
        const up = top < lastTop - 1;
        lastTop = top;
        if (up && userActive()) {
          following = false;
        } else if (!up && nearBottom()) {
          following = true;
        }
        updateButton();
      },
      { passive: true },
    );

    button?.addEventListener("click", () => jump(true));

    return {
      following: () => following,
      nearBottom,
      stick,
      afterRender,
      jump,
      keepPosition,
    };
  }

  window.MultiGPT = window.MultiGPT || {};
  window.MultiGPT.scrollFollow = scrollFollow;
})();
