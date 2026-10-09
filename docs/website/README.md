# Projektseite (Hugo)

Statische Projektseite für GitHub Pages, Deutsch (`/`) und Englisch (`/en/`).
Eigenes Layout ohne Theme, ohne JavaScript, ohne externe Ressourcen.

## Lokal bauen und ansehen

Benötigt Hugo **0.165.0** (normale Ausgabe, nicht "extended") – dieselbe Version wie
im Workflow `.github/workflows/website.yml`. Mit Go installieren:

```sh
CGO_ENABLED=0 go install github.com/gohugoio/hugo@v0.165.0   # landet in ~/go/bin
```

Vom Projektwurzelordner aus:

```sh
hugo server --source docs/website            # Vorschau auf http://localhost:1313/
hugo --source docs/website --minify          # Build nach docs/website/public/
```

`public/`, `resources/_gen/` und `.hugo_build.lock` sind per `.gitignore` ausgeschlossen.

## Aufbau

| Pfad | Inhalt |
|---|---|
| `hugo.toml` | Konfiguration, Sprachen, Platzhalter |
| `content/de/`, `content/en/` | Seiten je Sprache (gleiche Dateinamen = Übersetzungen) |
| `data/roadmap.toml` | Meilensteine mit Status – Quelle für die Roadmap |
| `data/features.toml` | Funktionskacheln der Startseite mit Status |
| `i18n/` | Oberflächentexte |
| `layouts/` | `baseof.html`, `home.html`, `page.html`, `_partials/`, `_shortcodes/` |
| `assets/css/main.css` | Stylesheet (über Hugo Pipes minifiziert und mit Fingerprint) |

Status nach einem Meilenstein ändern: `status` in `data/roadmap.toml` und
`data/features.toml` (`done`, `partial`, `planned`) sowie die Statusmarken
`{{</* status ... */>}}` in `content/*/features.md`.

## Platzhalter

- `baseURL` in `hugo.toml`: steht auf `https://example.invalid/multi-gpt/`. Beim
  Veröffentlichen setzt der Workflow die echte Adresse per `--baseURL`.
- `params.repoURL` in `hugo.toml`: leer, solange die GitHub-Adresse nicht feststeht.
  Die Seite zeigt dann "Repository-Adresse folgt".
- `params.license` in `hugo.toml`: leer, solange keine Lizenz festgelegt ist. Die Seite
  zeigt dann "Lizenz: noch offen".
- Screenshot auf der Startseite: klar markierter Platzhalter (`layouts/home.html`),
  bis es eine Chatansicht gibt.

## Veröffentlichen

Der Workflow läuft bei Pushes auf `main`, die `docs/website/**` ändern, und manuell
(`workflow_dispatch`). Im Repository muss unter *Settings → Pages* als Quelle
"GitHub Actions" gewählt sein.
