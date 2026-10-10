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
| KaTeX (mit mhchem für `\ce{}`) | 0.19.0 | MIT (`katex/LICENSE`) | https://registry.npmjs.org/katex/-/katex-0.19.0.tgz |

Pfade im Tarball: `package/lib/marked.umd.js(.map)`, `package/dist/purify.min.js(.map)`,
`package/highlight.min.js`, `package/styles/github.min.css`, `package/styles/github-dark.min.css`.

KaTeX: `package/dist/katex.min.js`, `package/dist/contrib/mhchem.min.js`,
`package/dist/fonts/*.woff2` und `package/dist/katex.min.css`. Einzige Änderung:
In `katex.min.css` sind die Verweise auf die `.woff`- und `.ttf`-Fallbacks
entfernt (nur woff2 liegt bei, alle Zielbrowser können woff2; sonst scheitert
`collectstatic` an den fehlenden Dateien):

```
sed -E 's/,url\(fonts\/[A-Za-z0-9_-]+\.woff\) format\("woff"\),url\(fonts\/[A-Za-z0-9_-]+\.ttf\) format\("truetype"\)//g' \
  package/dist/katex.min.css > katex/katex.min.css
```

Größe KaTeX gesamt rund 630 KB (JS 270 KB, mhchem 34 KB, CSS 23 KB, 20 Fonts 300 KB);
Fonts lädt der Browser nur bei Bedarf.

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
766ccc1f306c885aa45542a9846bbd0a505b27a0374f146778171c2254ce18e3  ./katex/LICENSE
aaf20145c0b8ecd450ccf6eb0cebece2f77d8e6a02c30d291f28c1167b57b2df  ./katex/contrib/mhchem.min.js
0cdd387c9590a1a9f9794560022dbb59654a7d86f187aa0c81495ad42d3a7308  ./katex/fonts/KaTeX_AMS-Regular.woff2
de7701e42cf1f4cf0b766c03fb27977207eee2f4fd5d76fa82188406da43ea4c  ./katex/fonts/KaTeX_Caligraphic-Bold.woff2
5d53e70ad607c2352162dec9e0923fb54ecdafaccbf604cd8dcf7d00facb989b  ./katex/fonts/KaTeX_Caligraphic-Regular.woff2
74444efd593c005e3f4573b44524704c0af0a937fe911cca9e94068d0d140d3f  ./katex/fonts/KaTeX_Fraktur-Bold.woff2
51814d270d06ff0255dba0799994fa4d8c84d11f09951d47595f4abb1f3602dc  ./katex/fonts/KaTeX_Fraktur-Regular.woff2
0f60d1b897938ec918c8ce073092411baf9438f6739465693ff18b0f9d20b021  ./katex/fonts/KaTeX_Main-Bold.woff2
99cd42a3c072d918f2f44984a807cf7aa16e13545fd0875fc07c6c65f99e715b  ./katex/fonts/KaTeX_Main-BoldItalic.woff2
97479ca6cce906abc961ecac96faa5f9ca2e61b8e7670d475826bcdee9a7c267  ./katex/fonts/KaTeX_Main-Italic.woff2
c2342cd8b869e01752a9321dc17213fc40d4d04c79688c1d43f2cf316abd7866  ./katex/fonts/KaTeX_Main-Regular.woff2
dc47344dbb6cb5b655c8460d561f4df5f501b90c804ad3c6cec65fe322351ab1  ./katex/fonts/KaTeX_Math-BoldItalic.woff2
7af58c5ec8f132a2ddde9027c6d7814decce4d3b822a11192a42a20e2e973264  ./katex/fonts/KaTeX_Math-Italic.woff2
e99ae51144bf1232efcc1bfe5add36262c6866b0faab24fa75740e1b98577a62  ./katex/fonts/KaTeX_SansSerif-Bold.woff2
00b26ac825e2095056396e0553b8ac26d3f8ad158c3826e28b4c45b385c4714a  ./katex/fonts/KaTeX_SansSerif-Italic.woff2
68e8c73ef42afd3ccec58bf0fba302cce448938e7fc020a5e31f8a952eee1342  ./katex/fonts/KaTeX_SansSerif-Regular.woff2
036d4e95149b69ff9bcc0cd55771efeb25ffa3947293e69acd78d5ac328c684b  ./katex/fonts/KaTeX_Script-Regular.woff2
6b47c40166b6dbe21a5dfca7718413f2147fd2399be1ba605d8ad39cedf25dfe  ./katex/fonts/KaTeX_Size1-Regular.woff2
d04c54219f9eaec6d4d4fd42dfb28785975a4794d6b2fc71e566b9cd6db842dd  ./katex/fonts/KaTeX_Size2-Regular.woff2
73d591271b1604960cb10bb90fee021670af7297017e0e98480b332d11f51995  ./katex/fonts/KaTeX_Size3-Regular.woff2
a4af7d414440a1c1790825cfb700cf9cf43b0f2c4b04f0ebc523011ad9853ec0  ./katex/fonts/KaTeX_Size4-Regular.woff2
71d517d67827787cfabdf186914cc3358eda539e37931941f2b2fd4a21f68c0b  ./katex/fonts/KaTeX_Typewriter-Regular.woff2
864f909fc1f3da0f140321d0e277ac0a444d8909e072cdbf174bcea93b95c089  ./katex/katex.min.css
103a53763cc033bba8d175bf3f0ba597c3505c9b6747dd3f2c7bc2a6bfcc8ae7  ./katex/katex.min.js
8e3a3f82f59a60958f56ca08f445647c32a4733dc7ca6c2c46f6eb898471ab9c  ./marked/LICENSE
21568877a938d2c4e7d74e27f18e60da96bb73a68809610ca39216e1efebae62  ./marked/marked.umd.js
066a5274a87aecfb532f12e7ae63491296cc1405de15f787a316f44370e99f40  ./marked/marked.umd.js.map
```

## Aktualisieren

Neue feste Version wählen (nie `latest`), Tarball laden, `integrity` prüfen,
Dateien ersetzen, diese Tabelle und die Prüfsummen anpassen
(`sha256sum` über alle Dateien außer dieser), Lizenz auf Änderungen prüfen.
