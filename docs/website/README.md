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
`data/features.toml` (`done`, `partial`, `progress` = in Arbeit und noch nicht veröffentlicht – derzeit ungenutzt, `planned`) sowie die Statusmarken
`{{</* status ... */>}}` in `content/*/features.md`.

## Platzhalter

- `baseURL` in `hugo.toml`: `https://multigpt.bernau.family/` (eigene Domain auf GitHub
  Pages). Beim Veröffentlichen setzt der Workflow die Adresse per `--baseURL`.
- `params.repoURL` in `hugo.toml`: leer, solange die GitHub-Adresse nicht feststeht.
  Die Seite zeigt dann "Repository-Adresse folgt".
- `params.license` in `hugo.toml`: leer, solange keine Lizenz festgelegt ist. Die Seite
  zeigt dann "Lizenz: noch offen".
- Keine Platzhalter mehr für Bilder (siehe „Bilder“).

## Bilder

| Pfad | Inhalt |
|---|---|
| `static/images/screens/` | Bildschirmfotos als WebP, je hell und dunkel (`-dark`); Texte in `data/screens.toml` |
| `static/images/art/` | Illustrationen (mit MultiGPT erzeugt, FLUX.2 klein): Titelbild und Kacheln |
| `layouts/_shortcodes/architecture.html` | Architekturgrafik als Inline-SVG, Farben aus dem Stylesheet |

Im Text: `{{</* shot "usage" */>}}`. Die dunkle Variante wählt der Browser selbst
(`prefers-color-scheme`).

Bildschirmfotos neu aufnehmen (eigene Demo-Datenbank, nie die Produktion):

```sh
createdb -T multigpt_template multigpt_demo
DATABASE_URL=postgres:///multigpt_demo python manage.py migrate
DATABASE_URL=postgres:///multigpt_demo python manage.py shell < docs/website/tools/demo_seed.py
DATABASE_URL=postgres:///multigpt_demo DEBUG=True python manage.py runserver 127.0.0.1:8077
# zweites Terminal, Python mit playwright und `playwright install firefox`:
python docs/website/tools/screenshots.py http://127.0.0.1:8077 /tmp/shots
for f in /tmp/shots/*.png; do magick "$f" -strip -quality 80 \
  "docs/website/static/images/screens/$(basename "${f%.png}").webp"; done
```

## Veröffentlichen

Der Workflow läuft bei Pushes auf `main`, die `docs/website/**` ändern, und manuell
(`workflow_dispatch`). Im Repository muss unter *Settings → Pages* als Quelle
"GitHub Actions" gewählt sein. Eigene Domain: `multigpt.bernau.family` (CNAME bei
Hetzner auf `beerlesklopfer.github.io`, unter *Settings → Pages → Custom domain*
eingetragen, „Enforce HTTPS“ an). Die alte Adresse `beerlesklopfer.github.io/multi-gpt/`
leitet GitHub dorthin um, auch für die apt-Quelle.
