// MultiGPT – Grundfunktionen der Oberfläche. Kein Inline-JS (CSP).
"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.getElementById("sidebar-toggle");
  const sidebar = document.getElementById("sidebar");
  if (!toggle || !sidebar) {
    return;
  }

  const setOpen = (open) => {
    document.body.classList.toggle("sidebar-open", open);
    toggle.setAttribute("aria-expanded", String(open));
  };

  toggle.addEventListener("click", () => {
    setOpen(!document.body.classList.contains("sidebar-open"));
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
