// MultiGPT – Formeln in Modellantworten (LaTeX, gesetzt mit KaTeX).
//
// Ablauf (aus markdown.js):
//   1. protect(text, final): Formeln vor marked schützen. Jede Formel wird
//      durch einen Platzhalter (nur Buchstaben/Ziffern) ersetzt, damit marked
//      keine Backslashes frisst (\[ → [, \, → ,) und keine Hervorhebungen
//      oder Tabellen aus _ * | bildet.
//   2. marked → DOMPurify wie bisher (Platzhalter sind reiner Text).
//   3. restore(el, state): Platzhalter in Code und Attributen wieder durch den
//      Quelltext ersetzen (Code bleibt roh, auch wenn protect ihn verfehlt).
//   4. typeset(el, state): übrige Platzhalter per katex.render in eigene
//      Elemente setzen. Die KaTeX-Ausgabe entsteht erst nach der Bereinigung
//      und stammt nicht aus dem Modelltext; trust: false schaltet \href, \url,
//      \htmlClass usw. ab, maxSize/maxExpand begrenzen den Aufwand.
//
// Erkannt (außerhalb von Code-Blöcken und Inline-Code):
//   Block:  \[ … \]   $$ … $$   \begin{equation|align|gather|…} … \end{…}
//   Inline: \( … \)   $ … $  – kein Leerzeichen direkt nach dem öffnenden und
//           vor dem schließenden $, das schließende $ steht nicht vor einer
//           Ziffer (Geldbeträge wie „5 $ und 10 $“ oder „$5“ bleiben Text).
// \$ bleibt ein Dollarzeichen. Während des Streams bleibt eine noch offene
// Formel am Ende roher Text, bis sie geschlossen ist; nach dem Stream bleibt
// ein nie geschlossener Begrenzer als Text stehen.
// Fehlt KaTeX, werden Formeln als Quelltext gezeigt.
"use strict";

(() => {
  const MAX_FORMULA = 20000; // längere Formeln nicht setzen (Quelltext zeigen)
  const CACHE_SIZE = 400;
  const ENVIRONMENTS =
    /^(?:equation|align|alignat|aligned|gather|gathered|multline|flalign|eqnarray|split|cases|array|[pbBvV]?matrix|smallmatrix|CD)\*?$/;

  // Platzhalter: zufälliger Kern je Seitenaufruf, damit Modelltext ihn nicht
  // vorwegnehmen kann; nur Buchstaben/Ziffern, die marked unverändert lässt.
  const nonce = Array.from({ length: 10 }, () => "abcdefghijklmnopqrstuvwxyz"[Math.floor(Math.random() * 26)]).join(
    "",
  );
  const TOKEN = new RegExp(`MGPT${nonce}N(\\d+)Z`, "g");

  function token(index) {
    return `MGPT${nonce}N${index}Z`;
  }

  function katexOptions(display) {
    return {
      displayMode: display,
      output: "htmlAndMathml",
      throwOnError: false,
      trust: false,
      strict: "ignore",
      maxSize: 20,
      maxExpand: 500,
      globalGroup: false,
      macros: {}, // je Formel neu: \gdef wirkt nicht über die Formel hinaus
    };
  }

  // --- Erkennen -------------------------------------------------------------------

  const FENCE = /^[ \t>]*(`{3,}|~{3,})/;

  // Ende der Zeile ab pos (Index des \n oder text.length).
  function lineEnd(text, pos) {
    const end = text.indexOf("\n", pos);
    return end === -1 ? text.length : end;
  }

  // Code-Block ab Zeilenanfang pos: Ende (nach der schließenden Zaunzeile)
  // oder -1, wenn hier kein Zaun beginnt. Ohne schließenden Zaun: Textende.
  function fencedEnd(text, pos) {
    const first = text.slice(pos, lineEnd(text, pos));
    const open = FENCE.exec(first);
    if (!open) {
      return -1;
    }
    const char = open[1][0];
    const close = new RegExp(`^[ \\t>]*\\${char}{${open[1].length},}[ \\t]*$`);
    let line = lineEnd(text, pos) + 1;
    while (line <= text.length) {
      const end = lineEnd(text, line);
      if (close.test(text.slice(line, end))) {
        return end;
      }
      if (end >= text.length) {
        break;
      }
      line = end + 1;
    }
    return text.length;
  }

  // Inline-Code: Lauf aus n Backticks bis zum nächsten Lauf aus genau n
  // Backticks. Ohne Gegenstück zählen die Backticks als Text.
  function codeSpanEnd(text, pos) {
    let n = 0;
    while (text[pos + n] === "`") {
      n += 1;
    }
    const run = new RegExp(`(?<!\`)\`{${n}}(?!\`)`, "g");
    run.lastIndex = pos + n;
    const match = run.exec(text);
    if (!match || /\n[ \t]*\n/.test(text.slice(pos, match.index))) {
      return { end: pos + n, code: false };
    }
    return { end: match.index + n, code: true };
  }

  // Schließenden Begrenzer suchen; Formeln enden nicht über eine Leerzeile.
  function findClose(text, from, close) {
    let i = from;
    while (i < text.length) {
      const at = text.indexOf(close, i);
      if (at === -1) {
        return -1;
      }
      if (/\n[ \t>]*\n/.test(text.slice(from, at))) {
        return -1;
      }
      // \\ in der Formel (Zeilenumbruch einer Matrix) ist kein \] bzw. \).
      let slashes = 0;
      while (text[at - 1 - slashes] === "\\") {
        slashes += 1;
      }
      if (close[0] === "\\" ? slashes % 2 === 0 : slashes === 0) {
        return at;
      }
      i = at + 1;
    }
    return -1;
  }

  const BLANK_LINE = /[ \t>]*\n/y;

  // Inline-$: kein Leerzeichen nach dem öffnenden $ (geprüft vom Aufrufer),
  // kein Leerzeichen vor dem schließenden, keine Ziffer danach, nicht \$.
  function findDollarClose(text, from) {
    for (let i = from; i < text.length; i += 1) {
      const c = text[i];
      if (c === "\\") {
        i += 1;
        continue;
      }
      if (c === "\n") {
        BLANK_LINE.lastIndex = i + 1;
        if (BLANK_LINE.test(text)) {
          return -1;
        }
      }
      if (c === "$") {
        if (i > from && !/\s/.test(text[i - 1]) && !/\d/.test(text[i + 1] || "")) {
          return i;
        }
        if (text[i + 1] === "$") {
          return -1;
        }
      }
    }
    return -1;
  }

  // \begin{env} … \end{env} mit Verschachtelung derselben Umgebung.
  function findEnvironmentEnd(text, from, name) {
    const pattern = new RegExp(`\\\\(begin|end)\\{${name.replace("*", "\\*")}\\}`, "g");
    pattern.lastIndex = from;
    let depth = 1;
    let match;
    while ((match = pattern.exec(text))) {
      depth += match[1] === "begin" ? 1 : -1;
      if (depth === 0) {
        return pattern.lastIndex;
      }
    }
    return -1;
  }

  // Inhalt mehrzeiliger Formeln in Zitaten: die Zitatzeichen > der
  // Folgezeilen gehören zum Markdown, nicht zur Formel.
  function stripQuotes(body, prefix) {
    return prefix.includes(">") ? body.replace(/\n[ \t]*(?:>[ \t]?)+/g, "\n") : body;
  }

  function protect(text, final = true) {
    const source = String(text ?? "");
    const items = [];
    let out = "";
    let last = 0;
    let i = 0;
    let lineStart = 0;

    const add = (start, end, tex, display, pending = false) => {
      out += source.slice(last, start);
      const prefix = source.slice(lineStart, start);
      items.push({ raw: source.slice(start, end), tex: stripQuotes(tex, prefix).trim(), display, pending });
      out += token(items.length - 1);
      last = end;
      i = end;
    };
    // Offener Begrenzer: im Stream ab hier roher Text, sofern danach keine
    // Leerzeile mehr kommt (die Formel kann sich noch schließen); sonst bleibt
    // der Begrenzer Text.
    const open = (start) => !final && !/\n[ \t>]*\n/.test(source.slice(start));
    const unclosed = (start, length) => {
      if (open(start)) {
        add(start, source.length, "", false, true);
        return;
      }
      if (source[start] === "\\") {
        // \[ bzw. \( als Text zeigen, nicht von marked zu [ bzw. ( machen.
        out += `${source.slice(last, start)}\\`;
        last = start;
      }
      i = start + length;
    };

    while (i < source.length) {
      if (i === lineStart) {
        const end = fencedEnd(source, i);
        if (end !== -1) {
          i = end;
          continue;
        }
      }
      const c = source[i];
      if (c === "\n") {
        i += 1;
        lineStart = i;
        continue;
      }
      if (c === "`") {
        i = codeSpanEnd(source, i).end;
        continue;
      }
      if (c === "\\") {
        const next = source[i + 1];
        if (next === "[" || next === "(") {
          const close = next === "[" ? "\\]" : "\\)";
          const at = findClose(source, i + 2, close);
          if (at === -1) {
            unclosed(i, 2);
          } else {
            add(i, at + 2, source.slice(i + 2, at), next === "[");
          }
          continue;
        }
        if (source.startsWith("\\begin{", i)) {
          const match = /^\\begin\{([A-Za-z]+\*?)\}/.exec(source.slice(i, i + 40));
          if (match && ENVIRONMENTS.test(match[1])) {
            const end = findEnvironmentEnd(source, i + match[0].length, match[1]);
            if (end === -1) {
              if (open(i)) {
                add(i, source.length, "", false, true);
              } else {
                i += match[0].length;
              }
            } else {
              add(i, end, source.slice(i, end), true);
            }
            continue;
          }
        }
        // \$, \\ usw.: Escape samt Folgezeichen überspringen.
        i += 2;
        continue;
      }
      if (c === "$") {
        if (source[i + 1] === "$") {
          const at = findClose(source, i + 2, "$$");
          if (at === -1) {
            unclosed(i, 2);
          } else {
            add(i, at + 2, source.slice(i + 2, at), true);
          }
          continue;
        }
        const next = source[i + 1];
        if (next && !/\s/.test(next)) {
          const at = findDollarClose(source, i + 1);
          if (at !== -1) {
            add(i, at + 1, source.slice(i + 1, at), false);
            continue;
          }
          // Im Stream: kurzer offener Rest ohne Zeilenumbruch und ohne weiteres $
          // kann noch eine Formel werden. Sonst eher ein Geldbetrag.
          const rest = source.slice(i);
          if (
            !final &&
            rest.length < 200 &&
            !/[\n$]/.test(rest.slice(1)) &&
            !/^\$\d+(?:[.,]\d+)?\s/.test(rest)
          ) {
            add(i, source.length, "", false, true);
            continue;
          }
        }
        i += 1;
        continue;
      }
      i += 1;
    }
    out += source.slice(last);
    return { text: out, items };
  }

  // --- Setzen ---------------------------------------------------------------------

  // Gesetzte Formeln je (Modus, Quelltext); beim Stream wird so jede Formel nur
  // einmal gesetzt und danach geklont.
  const cache = new Map();

  function rendered(tex, display) {
    const key = `${display ? "D" : "I"}${tex}`;
    const hit = cache.get(key);
    if (hit) {
      cache.delete(key);
      cache.set(key, hit);
      return hit.cloneNode(true);
    }
    const holder = document.createElement("span");
    if (tex.length > MAX_FORMULA) {
      return null;
    }
    try {
      window.katex.render(tex, holder, katexOptions(display));
    } catch {
      return null;
    }
    cache.set(key, holder);
    if (cache.size > CACHE_SIZE) {
      cache.delete(cache.keys().next().value);
    }
    return holder.cloneNode(true);
  }

  function element(item) {
    const span = document.createElement("span");
    if (item.pending) {
      span.className = "math-pending";
      span.textContent = item.raw;
      return span;
    }
    span.className = item.display ? "math math-display" : "math math-inline";
    const holder = window.katex ? rendered(item.tex, item.display) : null;
    if (holder) {
      span.append(...holder.childNodes);
    } else {
      span.classList.add("math-raw");
      span.textContent = item.raw;
    }
    return span;
  }

  function replaceTokens(text, build) {
    const fragment = document.createDocumentFragment();
    let last = 0;
    for (const match of text.data.matchAll(TOKEN)) {
      fragment.append(text.data.slice(last, match.index), build(Number(match[1])));
      last = match.index + match[0].length;
    }
    fragment.append(text.data.slice(last));
    text.replaceWith(fragment);
  }

  function textNodes(root, filter) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(text) {
        TOKEN.lastIndex = 0;
        if (!TOKEN.test(text.data)) {
          return NodeFilter.FILTER_SKIP;
        }
        return filter(text) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
      },
    });
    const nodes = [];
    while (walker.nextNode()) {
      nodes.push(walker.currentNode);
    }
    return nodes;
  }

  // Platzhalter in Code (auch eingerücktem, den protect nicht erkennt) und in
  // Attributen (z. B. href) wieder zum Quelltext machen.
  function restore(root, state) {
    if (!state.items.length) {
      return;
    }
    const raw = (index) => state.items[index]?.raw ?? "";
    for (const text of textNodes(root, (t) => t.parentElement?.closest("pre, code"))) {
      replaceTokens(text, raw);
    }
    for (const el of root.querySelectorAll("[href], [title]")) {
      for (const name of ["href", "title"]) {
        const value = el.getAttribute(name);
        if (value && value.includes(`MGPT${nonce}N`)) {
          el.setAttribute(
            name,
            value.replace(TOKEN, (_, index) => raw(Number(index))),
          );
        }
      }
    }
  }

  function typeset(root, state) {
    if (!state.items.length) {
      return;
    }
    for (const text of textNodes(root, () => true)) {
      replaceTokens(text, (index) => (state.items[index] ? element(state.items[index]) : ""));
    }
  }

  window.MultiGPT = window.MultiGPT || {};
  window.MultiGPT.math = { protect, restore, typeset, ready: typeof window.katex !== "undefined" };
})();
