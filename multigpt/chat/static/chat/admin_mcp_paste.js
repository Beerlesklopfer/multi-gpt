// MCP-Server hinzufügen: JSON-Eintrag einfügen und das Formular damit füllen.
// Zerlegt wird auf dem Server (Admin „parse/“, mcp/importer.py) – gleiche Regeln wie
// beim Import (Platzhalter, Name eines einzelnen Eintrags). Das Textfeld hat keinen
// Namen und wird nicht mitgesendet; nach dem Übernehmen wird es geleert (Tokens).
(function () {
  "use strict";

  function setValue(id, value) {
    const field = document.getElementById(id);
    if (!field) return;
    field.value = value;
    field.dispatchEvent(new Event("change", { bubbles: true }));
  }

  document.addEventListener("DOMContentLoaded", function () {
    const box = document.getElementById("mcp-paste");
    const area = document.getElementById("mcp-paste-config");
    const apply = document.getElementById("mcp-paste-apply");
    const clip = document.getElementById("mcp-paste-clipboard");
    const status = document.getElementById("mcp-paste-status");
    const form = box && box.closest("form");
    if (!box || !area || !apply || !form) return;

    function say(text, isError) {
      status.textContent = text;
      status.className = isError ? "errornote" : "";
    }

    async function run() {
      const text = area.value.trim();
      if (!text) {
        say("Bitte zuerst den JSON-Eintrag einfügen.", true);
        area.focus();
        return;
      }
      const body = new FormData();
      body.append("config", text);
      body.append("csrfmiddlewaretoken", form.querySelector("[name=csrfmiddlewaretoken]").value);
      apply.disabled = true;
      let data;
      try {
        const response = await fetch(box.dataset.parseUrl, {
          method: "POST",
          body,
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        data = await response.json();
        if (!response.ok) throw new Error(data.error || "Der Eintrag ist nicht lesbar.");
      } catch (error) {
        say(error.message || "Der Eintrag ist nicht lesbar.", true);
        return;
      } finally {
        apply.disabled = false;
      }
      const entries = data.entries || [];
      const entry = entries.find((e) => !e.error) || entries[0];
      if (!entry) {
        say("Keine MCP-Server gefunden.", true);
        return;
      }
      if (entry.error) {
        say(`„${entry.name}“: ${entry.error}`, true);
        return;
      }
      setValue("id_name", entry.name);
      setValue("id_transport", entry.transport);
      setValue("id_command", entry.command || "");
      setValue("id_url", entry.url || "");
      setValue("id_credentials", entry.credentials || "");
      const notes = [`„${entry.name}“ übernommen – bitte prüfen und mit „Sichern“ speichern.`];
      if (entry.placeholders && entry.placeholders.length) {
        const active = document.getElementById("id_active");
        if (active) active.checked = false;
        notes.push(`Platzhalter bei ${entry.placeholders.join(", ")}: nicht übernommen, Server bleibt aus, bis der echte Wert eingetragen ist.`);
      }
      if (entries.length > 1) {
        notes.push(`${entries.length} Server im Text – übernommen ist nur der erste; für alle „Aus JSON importieren“.`);
      }
      area.value = "";
      say(notes.join(" "), false);
      document.getElementById("id_name")?.focus();
    }

    apply.addEventListener("click", run);
    area.addEventListener("paste", () => setTimeout(run, 0));
    if (navigator.clipboard && navigator.clipboard.readText) {
      clip.hidden = false;
      clip.addEventListener("click", async function () {
        try {
          area.value = await navigator.clipboard.readText();
        } catch (error) {
          say("Kein Zugriff auf die Zwischenablage – bitte mit Strg+V in das Feld einfügen.", true);
          area.focus();
          return;
        }
        run();
      });
    }
  });
})();
