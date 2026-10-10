// MCP-Import im Admin: Vorschau beim Einfügen/Tippen und Knopf „Aus Zwischenablage
// einfügen“. Zeigt Name, Transport, URL bzw. Befehl – nie Header- oder Env-Werte.
// Maßgeblich bleibt die Prüfung auf dem Server (mcp/importer.py).
(function () {
  "use strict";

  const PLACEHOLDER = /<[^<>]*>|\$\{[^}]*\}|\bYOUR[_ -]|\bDEIN[_ -]|^\s*(changeme|xxx+)\s*$/i;
  const HTTP_TYPES = ["http", "streamable-http", "streamable_http", "streamablehttp"];

  function describe(name, spec) {
    if (!spec || typeof spec !== "object") return { name, error: "kein JSON-Objekt" };
    const url = spec.url || spec.serverUrl || "";
    let kind = String(spec.type || spec.transport || "").toLowerCase();
    if (!kind) kind = url ? "http" : spec.command ? "stdio" : "";
    const values = Object.entries(
      (HTTP_TYPES.includes(kind) ? spec.headers : spec.env) || {}
    );
    const placeholders = values.filter(([, v]) => PLACEHOLDER.test(String(v))).map(([k]) => k);
    if (HTTP_TYPES.includes(kind)) {
      return { name, transport: "HTTP", target: url, secrets: values.length, placeholders,
               error: url ? "" : "URL fehlt" };
    }
    if (kind === "stdio") {
      const args = Array.isArray(spec.args) ? spec.args.join(" ") : "";
      return { name, transport: "stdio", target: [spec.command, args].join(" ").trim(),
               secrets: values.length, placeholders, error: spec.command ? "" : "„command“ fehlt" };
    }
    if (kind === "sse") return { name, error: "SSE wird nicht unterstützt (Streamable HTTP nutzen)" };
    return { name, error: "unbekannter Typ" };
  }

  function render(preview, text) {
    preview.replaceChildren();
    if (!text.trim()) return;
    let data;
    try {
      data = JSON.parse(text);
    } catch (e) {
      preview.append(note("errornote", "Noch kein gültiges JSON."));
      return;
    }
    const servers = data && (data.mcpServers || data.servers || data);
    if (!servers || typeof servers !== "object" || Array.isArray(servers) || !Object.keys(servers).length) {
      preview.append(note("errornote", "Keine MCP-Server gefunden (erwartet „mcpServers“)."));
      return;
    }
    const table = document.createElement("table");
    const head = table.createTHead().insertRow();
    for (const label of ["Server", "Transport", "URL bzw. Befehl", "Zugangsdaten", "Hinweis"]) {
      const th = document.createElement("th");
      th.scope = "col";
      th.textContent = label;
      head.append(th);
    }
    const body = table.createTBody();
    for (const [name, spec] of Object.entries(servers)) {
      const d = describe(name, spec);
      const row = body.insertRow();
      const hint = d.error ||
        (d.placeholders && d.placeholders.length
          ? "Platzhalter bei " + d.placeholders.join(", ") + " – wird deaktiviert angelegt"
          : "");
      const cells = [d.name, d.transport || "–", d.target || "–",
                     d.secrets ? d.secrets + " Wert(e), verschlüsselt" : "keine", hint];
      for (const value of cells) row.insertCell().textContent = value;
      if (d.error) row.className = "errors";
    }
    preview.append(table);
  }

  function note(cls, text) {
    const p = document.createElement("p");
    p.className = cls;
    p.textContent = text;
    return p;
  }

  document.addEventListener("DOMContentLoaded", function () {
    const field = document.querySelector(".mcp-import-config");
    const preview = document.getElementById("mcp-import-preview");
    const pasteButton = document.getElementById("mcp-import-paste");
    if (!field || !preview) return;
    const update = () => render(preview, field.value);
    field.addEventListener("input", update);
    field.addEventListener("paste", () => setTimeout(update, 0));
    if (pasteButton && navigator.clipboard && navigator.clipboard.readText) {
      pasteButton.hidden = false;
      pasteButton.addEventListener("click", async function () {
        try {
          field.value = await navigator.clipboard.readText();
          update();
          field.focus();
        } catch (e) {
          preview.replaceChildren(note("errornote",
            "Kein Zugriff auf die Zwischenablage – bitte mit Strg+V einfügen."));
        }
      });
    }
    update();
  });
})();
