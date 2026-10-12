// MultiGPT – persönliches Menü rechts oben (accounts/_user_menu.html).
//
// Grundlage ist ein <details>, das ohne JavaScript auf- und zuklappt. Hier kommt
// dazu: Escape und Klick daneben schließen, Pfeiltasten wandern durch die
// Einträge, beim Öffnen per Tastatur steht der Fokus auf dem ersten Eintrag.
"use strict";

(() => {
  document.addEventListener("DOMContentLoaded", () => {
    const menu = document.getElementById("user-menu");
    if (!menu) {
      return;
    }
    const button = menu.querySelector("summary");
    const items = () => Array.from(menu.querySelectorAll(".user-menu-panel a, .user-menu-panel button"));
    let openedByKey = false;

    button.setAttribute("aria-haspopup", "true");
    button.setAttribute("aria-expanded", "false");

    function close(focusButton) {
      if (!menu.open) {
        return;
      }
      menu.open = false;
      if (focusButton) {
        button.focus();
      }
    }

    menu.addEventListener("toggle", () => {
      button.setAttribute("aria-expanded", String(menu.open));
      if (menu.open && openedByKey) {
        items()[0]?.focus();
      }
      openedByKey = false;
    });

    button.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " " || event.key === "ArrowDown") {
        openedByKey = true;
        if (event.key === "ArrowDown") {
          event.preventDefault();
          if (!menu.open) {
            menu.open = true;
          } else {
            items()[0]?.focus();
          }
        }
      }
    });

    menu.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        close(true);
        return;
      }
      if (!menu.open || (event.key !== "ArrowDown" && event.key !== "ArrowUp")) {
        return;
      }
      const list = items();
      const index = list.indexOf(document.activeElement);
      if (index === -1) {
        return;
      }
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      list[(index + step + list.length) % list.length].focus();
    });

    document.addEventListener("click", (event) => {
      if (menu.open && !menu.contains(event.target)) {
        close(false);
      }
    });
    // Fokus verlässt das Menü (Tab): schließen.
    menu.addEventListener("focusout", (event) => {
      if (menu.open && event.relatedTarget && !menu.contains(event.relatedTarget)) {
        close(false);
      }
    });
  });
})();
