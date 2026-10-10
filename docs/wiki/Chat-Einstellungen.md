# Chat-Einstellungen: Grundregeln, Kreativität und Denktiefe

Drei Dinge bestimmen, wie die Modelle antworten: die **Grundregeln** (Text am Anfang jedes
System-Prompts), die **Kreativität** (Temperatur) und die **Denktiefe** (Reasoning). Alle drei
legt der Verwalter im Admin unter **Chat → Chat-Einstellungen** fest. Kreativität und Denktiefe
lassen sich zusätzlich je Projekt und je Chat wählen.

## Was an das Modell geht

Bei jeder Antwort setzt MultiGPT den System-Prompt in dieser Reihenfolge zusammen:

1. **Grundregeln** (Chat-Einstellungen, gelten für alle Chats und alle Modelle),
2. fester Prompt der Rolle (Admin → Rollen),
3. technische Hinweise von MultiGPT (Quellmaterial, Websuche, Werkzeuge),
4. Anweisungen des Projekts (siehe [Projekte](Projekte)),
5. System-Prompt des Chats.

Im Chat zeigt der aufklappbare Bereich **„System-Prompt“** das an. Die Kopfzeile lautet z. B.
„System-Prompt: Grundregeln aktiv · eigener Prompt leer“. Aufgeklappt stehen dort:

- die Grundregeln im Wortlaut (nur lesend). Verwalter sehen daneben den Link **Ändern** in den
  Admin,
- **„Fester Prompt deiner Rolle (Rollenname)“** im Wortlaut (nur lesend), falls die eigene Rolle
  einen hat. Verwalter sehen daneben **Ändern** (führt zur Rolle im Admin). Gezeigt wird immer
  die Rolle des angemeldeten Kontos: Der Rollen-Prompt kommt von dem, der die Antwort auslöst.
  In einem geteilten Chat sieht ein Empfänger also seinen eigenen Rollen-Prompt, nie den des
  Besitzers. Im Chatverlauf und im Export erscheint der Rollen-Prompt nicht,
- „+ technische Hinweise zu Werkzeugen und Quellen“ (nur der Hinweis, nicht der Text),
- „+ Projekt-Anweisungen (Projektname): …“, gekürzt und aufklappbar. Das sieht nur der
  Besitzer des Chats, Empfänger geteilter Chats sehen fremde Projekte nicht,
- das Feld für den eigenen System-Prompt des Chats und die Auswahl **Kreativität** und
  **Denktiefe**.

Ändern dürfen alle drei der Besitzer und Empfänger mit dem Recht „Bearbeiten“ (U, siehe
[Chats teilen](Chats-teilen)). Alle anderen sehen den Bereich nur lesend.

## Grundregeln

Feld **„Grundregeln für alle Modelle“**. Neue Installationen und Updates bekommen einen
Standardtext (keine erfundenen URLs, Zahlen, Namen und Zitate; Unsicherheit offen sagen). Ein
geänderter Text wird bei Updates nicht überschrieben. Ein leeres Feld bedeutet: keine Grundregeln.

Antworten die Modelle zu ausschweifend oder zu fantasievoll, hilft ein Zusatz wie:

> Antworte sachlich und knapp. Wenn du etwas vermutest, kennzeichne es als Vermutung.

## Kreativität (Temperatur)

Die Temperatur steuert, wie stark ein Modell vom wahrscheinlichsten nächsten Wort abweicht.
Niedrige Werte geben sachliche, gleichbleibende Antworten, hohe Werte abwechslungsreichere, die
aber eher etwas erfinden. Ohne Angabe gilt der Standard des Anbieters, oft 0,7 bis 1,0.

Welcher Wert gilt, entscheidet die erste gesetzte Ebene:

1. **Chat:** Auswahl „Kreativität“ im Bereich „System-Prompt“: Präzise (0,2), Ausgewogen
   (0,7), Kreativ (1,0) oder **Standard**. Gespeichert wird mit „Speichern“, bei einem neuen
   Chat mit der ersten Nachricht.
2. **Projekt:** Feld „Kreativität“ in den Projekteinstellungen, gilt für Chats des Projekts
   ohne eigene Wahl.
3. **Verwalter:** „Standard-Kreativität (Temperatur)“ in den Chat-Einstellungen, 0 bis 2,
   Standard **0,3** (sachlich). Leer = Standard des Anbieters.

Im **Vergleich** nutzen alle Spalten dieselbe Kreativität des Chats.

### Modelle ohne einstellbare Temperatur

Einige Modelle lehnen eine Temperatur ab (HTTP 400) oder sollen sie nicht bekommen. MultiGPT
sendet ihnen keine, sie antworten mit ihrem eigenen Wert (Stand 2026-10-10):

| Anbieter | ohne Temperatur | Grund |
|---|---|---|
| OpenAI | o1, o3, o4-mini, gpt-5 (auch mini, nano), gpt-5.x, gpt-6 | Reasoning-Modelle: nur der Standard. Ab gpt-5.1 mit Denktiefe „Aus/minimal“ (`reasoning_effort: none`) wieder erlaubt, außer GPT-6 Astra und GPT-6.1 Sol (kein `none`) |
| Anthropic | claude-opus-4-7 und neuer, alle Claude-5-Modelle (Opus, Sonnet, Haiku), Fable, Mythos | Werte außer dem Standard ergeben HTTP 400. Ebenso jede Anfrage mit Thinking, also bei älteren Claude-Modellen jede Denktiefe außer „Aus/minimal“ |
| Google | gemini-3 und neuer | Google rät dringend zum Standard 1,0 (sonst Schleifen, schlechtere Ergebnisse) |
| LM Studio, Ollama | – | Temperatur wird immer übergeben |

Die Regel richtet sich nach der Modell-ID und gilt auch mit Präfix (z. B. `openai/gpt-5` über
OpenRouter). Lehnt ein anderes Modell die Temperatur ab, wiederholt MultiGPT die Anfrage
automatisch ohne sie und merkt sich das Modell bis zum nächsten Neustart des Dienstes. Im Journal
steht dann nur „Modell <ID>: Temperatur abgelehnt, künftig ohne“ (Datenbank-ID, kein Text).

## Denktiefe (Reasoning)

Modelle mit Reasoning denken vor der Antwort nach. Die Denktiefe legt fest, wie gründlich: höher
heißt gründlicher, aber langsamer und teurer, denn die Denk-Tokens werden als Ausgabe berechnet
(im Verbrauch unter „davon Reasoning“). Stufen:

- **Standard** (der Anbieter entscheidet, MultiGPT sendet nichts),
- **Aus/minimal** (kein oder möglichst wenig Nachdenken),
- **Niedrig**, **Mittel**, **Hoch**,
- **Sehr hoch** und **Maximal** (nur einige OpenAI- und Claude-Modelle).

Welche Stufe gilt, entscheidet wie bei der Kreativität die erste gesetzte Ebene: Chat (Auswahl
„Denktiefe“ im Bereich „System-Prompt“) → Projekt (Feld „Denktiefe“) → Verwalter
(„Standard-Denktiefe“ in den Chat-Einstellungen, Standard leer = Anbieter). Rechte wie beim
System-Prompt (Besitzer und Empfänger mit „Bearbeiten“).

Kann das gewählte Modell kein Reasoning, ist die Auswahl ausgegraut; darunter steht, welche
Stufen das Modell kennt. Kennt ein Modell eine Stufe nicht, nimmt MultiGPT die nächste passende:
„Aus/minimal“ wird zur kleinsten Stufe des Modells, sonst wird abgerundet (z. B. „Maximal“ →
„Sehr hoch“ bei gpt-5.5), notfalls aufgerundet. Im **Vergleich** gilt dieselbe Stufe für alle
Spalten, je Modell so abgebildet; Modelle ohne Reasoning bekommen nichts.

### Abbildung je Anbieter (Stand 2026-10-10)

| Anbieter | Modelle | Parameter | Stufen |
|---|---|---|---|
| OpenAI | o1, o3, o4-mini | `reasoning_effort` | low, medium, high („Aus“ → low) |
| OpenAI | gpt-5, gpt-5-mini, gpt-5-nano | `reasoning_effort` | minimal („Aus“), low, medium, high |
| OpenAI | gpt-5.1 | `reasoning_effort` | none („Aus“), low, medium, high |
| OpenAI | gpt-5.2 bis gpt-5.5 | `reasoning_effort` | none, low, medium, high, xhigh |
| OpenAI | gpt-5.6, GPT-6 Luna | `reasoning_effort` | none, low, medium, high, xhigh, max |
| OpenAI | GPT-6 Astra, GPT-6.1 Sol | `reasoning_effort` | low, medium, high, xhigh, max |
| Anthropic | Opus 5.5, Fable, Mythos | `output_config.effort` | low bis max, Thinking immer an („Aus“ → low) |
| Anthropic | Opus 5, Sonnet 5, Haiku 5.5 | `output_config.effort` | „Aus“ = `thinking: disabled`, sonst low bis max |
| Anthropic | Sonnet 5.5 | `output_config.effort` | „Aus“ = `thinking: between_tools`, sonst low bis max |
| Anthropic | Opus 4.6–4.8, Sonnet 4.6 | `thinking: adaptive` + `output_config.effort` | „Aus“ = ohne Thinking; 4.6 ohne xhigh |
| Anthropic | Opus/Sonnet/Haiku 4.5, Opus 4/4.1, Sonnet 4, Claude 3.7 Sonnet | `thinking.budget_tokens` | Aus, 2048, 8000, 16000 Tokens |
| Google | Gemini 3 Flash/Flash-Lite | `thinkingConfig.thinkingLevel` | MINIMAL („Aus“), LOW, MEDIUM, HIGH |
| Google | Gemini 3 Pro / 3.1 Pro, 3.7/3.8 Flash | `thinkingConfig.thinkingLevel` | 3 Pro nur LOW/HIGH; sonst LOW, MEDIUM, HIGH |
| Google | Gemini 2.5 Flash/Flash-Lite, 2.5 Pro | `thinkingConfig.thinkingBudget` | 0 („Aus“, nicht bei Pro), 1024, 8192, 24576 |
| lokal/OpenAI-kompatibel | gpt-oss | `reasoning_effort` | low, medium, high |
| lokal | andere (Qwen, Llama, …) | – | nichts |

Bei Claude mit „Sehr hoch“ oder „Maximal“ setzt MultiGPT `max_tokens` auf 64 000 (empfiehlt
Anthropic), bei `budget_tokens` auf 16 000 plus Budget. Claude über OpenRouter u. Ä. (Anbieterart
„OpenAI-kompatibel“) bekommt keine Denktiefe.

**Zusammen mit der Kreativität:** Bei OpenAI ab gpt-5.1 erlaubt „Aus/minimal“ (`none`) wieder
eine Temperatur. Bei Claude gilt mit Thinking keine Temperatur, ältere Claude-Modelle (bis 4.6)
bekommen sie also nur ohne Denktiefe oder mit „Aus/minimal“.

**Ablehnung:** Lehnt ein Anbieter den Parameter ab (HTTP 400/422), wiederholt MultiGPT die
Anfrage einmal ohne Denktiefe und merkt sich das Modell bis zum nächsten Neustart. Im Journal
steht nur „Modell <ID>: Denktiefe abgelehnt, künftig ohne“.

## Quellen

Gelesen am 2026-10-10:

- OpenAI, Reasoning: <https://developers.openai.com/api/docs/guides/reasoning>
- OpenAI, neueste Modelle („When reasoning effort is not `none`, remove `temperature`, `top_p`,
  and `top_logprobs`“): <https://developers.openai.com/api/docs/guides/latest-model>
- Anthropic, Migrationsleitfäden (Sampling-Parameter ab Claude Opus 4.7 bzw. in Claude 5):
  <https://platform.claude.com/docs/en/models/opus-5-5/migration-guide>,
  <https://platform.claude.com/docs/en/models/sonnet-5-5/migration-guide>,
  <https://platform.claude.com/docs/en/models/haiku-5-5/migration-guide>
- Google, Gemini 3 („keeping the temperature parameter at its default value of 1.0“,
  `thinking_level`, „You cannot use both `thinking_level` and the legacy `thinking_budget`“):
  <https://ai.google.dev/gemini-api/docs/gemini-3>
- OpenAI, Modellseiten (Stufen je Modell, z. B. „Reasoning.effort supports: none, low, medium
  (default), high and xhigh“): <https://developers.openai.com/api/docs/models/gpt-5.5>,
  …/gpt-5, …/gpt-5.1, …/gpt-5.2, …/gpt-5.4, …/gpt-5.6, …/gpt-6-luna,
  <https://developers.openai.com/api/docs/models>, …/gpt-oss-120b
- Anthropic, Effort: <https://platform.claude.com/docs/en/build-with-claude/effort>
- Anthropic, Thinking je Modell („Thinking support, defaults, and rejected configurations by
  model“): <https://platform.claude.com/docs/en/build-with-claude/thinking-troubleshooting>
- Google, Thinking-Budgets und -Stufen je Modell:
  <https://firebase.google.com/docs/ai-logic/thinking>,
  <https://ai.google.dev/gemini-api/docs/thinking>
- LM Studio 0.4.8 (`reasoning_effort` auf v1/chat/completions):
  <https://lmstudio.ai/changelog/lmstudio-v0.4.8>
