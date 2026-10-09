# Eingebundene Fremdbibliotheken

Lokal ausgeliefert (keine CDNs zur Laufzeit). Einmalig aus der npm-Registry
geladen, Tarball gegen das `integrity`-Feld der Registry geprüft (SHA-512) und
unverändert übernommen. Die `.map`-Dateien liegen bei, weil
`ManifestStaticFilesStorage` die `sourceMappingURL`-Verweise auflöst.

| Bibliothek | Version | Lizenz | Quelle (Tarball) |
|---|---|---|---|
| marked | 18.0.14 | MIT (`marked/LICENSE`) | https://registry.npmjs.org/marked/-/marked-18.0.14.tgz |
| DOMPurify | 3.4.16 | Apache-2.0 oder MPL-2.0 (`dompurify/LICENSE`, `dompurify/LICENSE-MPL`) | https://registry.npmjs.org/dompurify/-/dompurify-3.4.16.tgz |
| highlight.js (Common-Bundle, Themes GitHub hell/dunkel) | 11.12.0 | BSD-3-Clause (`highlight/LICENSE`) | https://registry.npmjs.org/@highlightjs/cdn-assets/-/cdn-assets-11.12.0.tgz |

Pfade im Tarball: `package/lib/marked.umd.js(.map)`, `package/dist/purify.min.js(.map)`,
`package/highlight.min.js`, `package/styles/github.min.css`, `package/styles/github-dark.min.css`.

## SHA-256

```
cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30  ./dompurify/LICENSE
fab3dd6bdab226f1c08630b1dd917e11fcb4ec5e1e020e2c16f83a0a13863e85  ./dompurify/LICENSE-MPL
2c90a9b46d6463f26038a29b686e82bc91de01fdac9d5229e7cfe3b360134ea2  ./dompurify/purify.min.js
52a28819cc9aacf5d0542c765e3f7c70796600959d33eab63a7bbb89361f3155  ./dompurify/purify.min.js.map
6c081431591d9df696c82dc598fe1423765b8a299b200ed00b281afd0f64c490  ./highlight/LICENSE
8ab71eb09c51f501e5e25157d9cff100e46cc29bcbfc744d0b746d451fca7f53  ./highlight/highlight.min.js
9f208d022102b1d0c7aebfecd8e42ca7997d5de636649d2b31ea63093d809019  ./highlight/styles/github-dark.min.css
3a9a5def8b9c311e5ae43abde85c63133185eed4f0d9f67fea4b00a8308cf066  ./highlight/styles/github.min.css
8e3a3f82f59a60958f56ca08f445647c32a4733dc7ca6c2c46f6eb898471ab9c  ./marked/LICENSE
21568877a938d2c4e7d74e27f18e60da96bb73a68809610ca39216e1efebae62  ./marked/marked.umd.js
066a5274a87aecfb532f12e7ae63491296cc1405de15f787a316f44370e99f40  ./marked/marked.umd.js.map
```

## Aktualisieren

Neue feste Version wählen (nie `latest`), Tarball laden, `integrity` prüfen,
Dateien ersetzen, diese Tabelle und die Prüfsummen anpassen
(`sha256sum` über alle Dateien außer dieser), Lizenz auf Änderungen prüfen.
