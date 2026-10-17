# Scratchpad auf dem Server

Im persönlichen Menü unter **Scratchpad** (`/scratchpad/`) findest du die Bilder
und Dateien deiner MCP-Werkzeugaufrufe, etwa SVG-Zeichnungen aus der Machinery
Toolbox. MultiGPT legt sie automatisch als private Kopie auf dem Server ab.

- **Dauerhaft:** Die lokale Datei hat keine Ablaufzeit. Eine vom Werkzeug
  erwähnte Gültigkeit von 15 Minuten betrifft nur dessen externen Link.
- **Unabhängig vom Chat:** Löschen des ursprünglichen Chats entfernt die
  Scratchpad-Kopie nicht.
- **Privat:** Nur das Konto, das die Antwort ausgelöst hat, kann die Datei
  im Scratchpad sehen und herunterladen. In geteilten Chats können die
  Empfänger weiterhin den Chat-Anhang gemäß ihrer Freigabe sehen.
- **Vorschau und Download:** Bilder sind direkt sichtbar; „Herunterladen“
  liefert die gespeicherte Datei. SVGs werden vor Speicherung und Anzeige bereinigt.

Die Dateien liegen unter `MEDIA_ROOT/scratchpad/<Konto-ID>/`; ihre Metadaten
in der Datenbank (`scratchpad.ScratchFile`). Datenbank und Medien gehören daher
zusammen ins Backup. Das Runner-Scratchpad `/scratch` auf PC oder Laptop ist
eine separate Ablage.

## Dateien löschen

Neben „Herunterladen“ steht **„Löschen“**. Auf der Bestätigungsseite das Häkchen
setzen und „Endgültig löschen“ wählen. Damit werden der Scratchpad-Eintrag und
seine gespeicherte Datei entfernt. Der ursprüngliche Chat-Anhang bleibt erhalten.
Nur der Besitzer kann löschen; „Abbrechen“ lässt die Datei unverändert.

## Bestehende Werkzeugdateien übernehmen

Als Verwalter auf dem Server:

```sh
sudo mgpt-ctl scratchpad_import
```

Optional einschränken, zum Beispiel auf ein Konto und ein Werkzeug:

```sh
sudo mgpt-ctl scratchpad_import --owner-id 1 --tool fits_image
```

Der Import ist wiederholbar: Ein bereits übernommener Chat-Anhang erzeugt
keinen zweiten Eintrag. Fehlende oder ungültige Dateien werden gemeldet.

Die Ablage dient zunächst erzeugten MCP-Dateien. Materialsammlung, Gliederung
und belegtes Schreiben aus dem geplanten Dokument-Scratchpad (M13) sind damit
noch nicht umgesetzt.
