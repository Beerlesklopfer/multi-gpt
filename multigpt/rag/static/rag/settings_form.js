/* RAG-Einstellungen: Wird ein OCR-Modell gewählt, während das Verfahren noch
   auf „Tesseract“ steht, auf „olmOCR“ umstellen (mit kurzem Hinweis). */
(function () {
  "use strict";
  function init() {
    var model = document.getElementById("id_ocr_model");
    var backend = document.getElementById("id_ocr_backend");
    if (!model || !backend) return;
    model.addEventListener("change", function () {
      if (model.value && backend.value === "tesseract") {
        backend.value = "olmocr";
        var note = document.getElementById("rag-ocr-switch-note");
        if (!note) {
          note = document.createElement("div");
          note.id = "rag-ocr-switch-note";
          note.className = "help";
          note.setAttribute("role", "status");
          backend.parentNode.appendChild(note);
        }
        note.textContent = "Verfahren auf „olmOCR“ umgestellt, weil ein OCR-Modell gewählt wurde.";
      }
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
