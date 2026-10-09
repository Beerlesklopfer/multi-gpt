// MultiGPT – Grundfunktionen der Oberfläche. Kein Inline-JS (CSP).
"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const umschalter = document.getElementById("seitenleiste-umschalter");
  const leiste = document.getElementById("seitenleiste");
  if (!umschalter || !leiste) {
    return;
  }

  const setzeOffen = (offen) => {
    document.body.classList.toggle("seitenleiste-offen", offen);
    umschalter.setAttribute("aria-expanded", String(offen));
  };

  umschalter.addEventListener("click", () => {
    setzeOffen(!document.body.classList.contains("seitenleiste-offen"));
  });

  // Schließen mit Escape oder Klick außerhalb der Seitenleiste.
  document.addEventListener("keydown", (ereignis) => {
    if (ereignis.key === "Escape") {
      setzeOffen(false);
    }
  });
  document.addEventListener("click", (ereignis) => {
    if (
      document.body.classList.contains("seitenleiste-offen") &&
      !leiste.contains(ereignis.target) &&
      !umschalter.contains(ereignis.target)
    ) {
      setzeOffen(false);
    }
  });
});
