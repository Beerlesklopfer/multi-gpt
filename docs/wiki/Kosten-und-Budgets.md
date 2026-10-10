# Kosten und Budgets

MultiGPT rechnet jede Antwort über einen **Kontenrahmen** ab. Jeder Anbieter gehört zu genau
einem **Abrechnungskonto**, und jedes Konto rechnet auf seine Art: Cloud-Anbieter in Geld
(USD oder EUR), lokale Modelle wie LM Studio nur in Tokens, Abos als Pauschale. Budgets gelten
je Konto und zusätzlich als Gesamtbudget in Euro.

Verwaltet wird alles im Admin unter **Kosten und Abrechnung**. Preise trägt der Verwalter selbst
ein. MultiGPT bringt keine Preise mit, weil die Anbieter sie oft ändern.

## Begriffe

| Begriff | Bedeutung |
|---|---|
| Abrechnungskonto | Name, Art, Währung (nur monetär), Notiz, aktiv. Ein Konto kann mehrere Anbieter bündeln, z. B. „OpenAI“ für den direkten Zugang und OpenRouter |
| Art `monetär` | Preise je 1 Mio. Tokens in EUR oder USD; Budget in EUR |
| Art `nur Tokens` | Keine Kosten, nur Token-Zählung (z. B. LM Studio); optional ein Token-Kontingent je Monat |
| Art `Pauschale/Abo` | Kosten 0 je Anfrage, Anfragen und Tokens werden gezählt; kein Budget |
| Modellpreis | Preisversion eines Modells ab einem Datum („gültig ab“) |
| Wechselkurs | 1 USD in EUR ab einem Tag |
| Buchung | Verbrauch einer Antwort, eines Anhangs oder eine Gebühr, mit Tokens, Betrag und Kurs |

## 1. Kontenrahmen einrichten

Beim Anlegen eines Anbieters belegt MultiGPT das Konto automatisch vor:

- **lokale Anbieter** (Häkchen „lokal“) kommen auf das gemeinsame Token-Konto **„Lokale Modelle“**,
- **alle anderen** bekommen ein monetäres Konto mit dem Namen des Anbieters in **USD**. Gibt es
  schon ein Konto mit diesem Namen, nimmt MultiGPT dieses.

Anpassen unter **Kosten und Abrechnung › Abrechnungskonten**:

1. Art und Währung prüfen. Anthropic, OpenAI und Google rechnen in USD ab.
2. Anbieter bündeln: Am Anbieter (**Chat › Anbieter**, Abschnitt „Abrechnung“) ein vorhandenes
   Konto wählen, z. B. OpenRouter auf „OpenAI“.
3. Art und Währung lassen sich nicht mehr ändern, sobald es Preise oder Buchungen gibt. Dann ein
   neues Konto anlegen und die Anbieter umhängen.
4. **aktiv** ausschalten sperrt alle Modelle des Kontos (z. B. Abo gekündigt).

## 2. Preise pflegen

**Kosten und Abrechnung › Modellpreise** oder direkt am Modell (**Chat › KI-Modelle**, Abschnitt
„Preise“). Alle Preise gelten **je 1 Mio. Tokens in der Währung des Kontos**.

| Feld | Wofür | Leer bedeutet |
|---|---|---|
| Eingabe | Eingabe-Tokens ohne Cache | 0, falls Ausgabe gesetzt |
| Eingabe aus Cache | gelesene Cache-Tokens | wie Eingabe |
| Cache schreiben (5 Min.) | geschriebene Cache-Tokens (Anthropic 5 Minuten, OpenAI ab GPT-5.6) | wie Eingabe |
| Cache schreiben (1 Std.) | Anthropic 1-Stunden-Cache | wie „Cache schreiben (5 Min.)“ |
| Ausgabe | Ausgabe einschließlich Reasoning-/Nachdenk-Tokens | 0, falls Eingabe gesetzt |
| Langkontext ab (Tokens) | Schwelle für einen teureren Tarif, z. B. 200000 | kein Langkontext-Tarif |
| Langkontext: … | Preise oberhalb der Schwelle | Normalpreis |
| Gebühren je Einheit | JSON, z. B. `{"web_search": 0.01, "image:high": 0.17}` | keine Gebühren |

Erlaubte Einheiten: `web_search` und `web_fetch` (je Aufruf beim Anbieter), `image` (je Bild,
Varianten wie `image:high` oder `image:high:1024x1024`; fehlt die Variante, gilt die kürzere),
`audio_minute` (je Minute), `tts_characters` (je 1 Mio. Zeichen), `request` (je Anfrage).

**Preisänderung:** neue Zeile mit neuem „gültig ab“ anlegen, die alte stehen lassen. Alte
Buchungen behalten ihren Betrag und zeigen, welche Preisversion sie verwendet haben.

Modelle **ohne Preis** (oder nur mit Preis 0) kosten nichts und werden von keinem Budget
gesperrt. Damit ein Cloud-Modell nicht versehentlich frei bleibt, Preise immer eintragen.

### So rechnet MultiGPT

- Eingabe ohne Cache = gesamte Eingabe − Cache gelesen − Cache geschrieben, zum Eingabepreis.
- Cache gelesen und Cache geschrieben zu ihren eigenen Preisen.
- Ausgabe einschließlich Reasoning zum Ausgabepreis.
- **Langkontext gilt je Anfrage:** Liegt die gesamte Eingabe *einer* Anfrage (einschließlich
  Cache) über der Schwelle, gelten für diese Anfrage alle Langkontext-Preise. Eine Antwort mit
  Werkzeugrunden besteht aus mehreren Anfragen, jede wird einzeln eingestuft.
- Gebühren je Einheit kommen dazu.

## 3. Wechselkurse

Die Anbieter rechnen in USD ab, die Budgets der Familie stehen in Euro. Unter **Kosten und
Abrechnung › Wechselkurse** einen Kurs „1 USD in EUR“ mit Datum eintragen (die EZB veröffentlicht
EUR → USD, z. B. 1,16; einzutragen ist der Kehrwert, also etwa 0,86). Verwendet wird der letzte Kurs
am oder vor dem Tag der Buchung (Europe/Berlin). Monatlich ein Kurs genügt.

Fehlt ein Kurs, bleibt der EUR-Betrag **unbekannt** (nicht 0), und der Admin zeigt einen Hinweis.
Für Budgets zählen solche Beträge vorläufig mit dem neuesten Kurs, ohne jeden Kurs 1:1. Sobald ein
passender Kurs gespeichert wird, trägt MultiGPT die fehlenden EUR-Beträge nach.

**EZB-Kurs automatisch (optional, standardmäßig aus):** `BILLING_ECB_FETCH=true` in
`/etc/multi-gpt/.env` schaltet den Knopf „EZB-Kurs abrufen“ und den Befehl
`mgpt-ctl fetch_ecb_rate` frei. Abgerufen wird nur
`https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml`, ohne Weiterleitungen.

## 4. Budgets

- **Gesamtbudget (EUR):** an der Rolle („Monatsbudget gesamt“), je Mitglied überschreibbar (Seite
  „Familie“ oder Admin). Zählt alle monetären Konten.
- **Budget je Konto:** unter **Abrechnungskonten**, Abschnitt „Budgets je Rolle bzw. Nutzer“.
  Monetäre Konten in EUR, Token-Konten als Token-Kontingent (Eingabe + Ausgabe je Monat).
  Ein Eintrag für ein Mitglied geht vor dem seiner Rolle. Leer = unbegrenzt.
- **Pauschalkonten** haben kein Budget.

Ab 80 % erscheint ein Hinweis (Kopfzeile, beim Senden, „Mein Verbrauch“). Bei 100 % sind **nur
die Modelle dieses Kontos** gesperrt und in der Modellauswahl ausgegraut, mit dem Grund im
Tooltip. Ist das Gesamtbudget aufgebraucht, sind alle kostenpflichtigen Modelle gesperrt; lokale
Modelle bleiben nutzbar, solange ihr Kontingent reicht. Der Monat ist der Kalendermonat in
Europe/Berlin.

Geprüft wird vor jeder Antwort. Laufen mehrere Antworten gleichzeitig, kann ein Budget um deren
Kosten überschritten werden; danach greift die Sperre.

In geteilten Chats zahlt immer, wer die Antwort ausgelöst hat.

## 5. Beispiele

**Anthropic (USD):** Konto „Anthropic“, monetär, USD. Je Claude-Modell die Preise von der
[Preisseite](https://platform.claude.com/docs/en/about-claude/pricing) übernehmen. Struktur dort:
Cache schreiben 5 Minuten = 1,25 × Eingabe, 1 Stunde = 2 × Eingabe, Cache lesen = 0,1 × Eingabe
(bei einigen Modellen weniger). Thinking zählt als Ausgabe. Websuche als Einheit, z. B.
`{"web_search": 0.01}` bei 10 USD je 1.000 Suchen. Einen Langkontext-Tarif gibt es laut
Preisseite aktuell nur bei einzelnen Modellen (z. B. Haiku über 100.000 Tokens).

**OpenAI (USD):** Konto „OpenAI“, monetär, USD; OpenRouter bei Bedarf auf dasselbe Konto. Je Modell
Eingabe, „Eingabe aus Cache“ und Ausgabe von der
[Preisseite](https://developers.openai.com/api/docs/pricing). Reasoning-Tokens zählen als Ausgabe.
Ab GPT-5.6 kostet Cache schreiben 1,25 × Eingabe. Manche Modelle haben einen Langkontext-Tarif
(z. B. über 272.000 Tokens). Bildmodelle: `{"image:high:1024x1024": 0.167}` usw.

**LM Studio (Tokens):** liegt automatisch auf „Lokale Modelle“. Keine Preise nötig. Optional ein
Kontingent, z. B. für die Rolle Gast 2.000.000 Tokens im Monat, damit der Rechner nicht dauernd
rechnet.

**Pauschale:** z. B. ein Abo mit fester Monatsgebühr: Konto mit Art „Pauschale/Abo“, Anbieter
zuordnen. Es wird nur gezählt.

## 6. Auswertung

- **Mein Verbrauch:** je Konto und Monat (wählbar): Antworten, Ein- und Ausgabe, Cache gelesen
  und geschrieben, Reasoning, Betrag in Kontowährung und Euro; Token-Konten nur Tokens.
- **Familie › Verbrauch aller:** dasselbe je Mitglied, ohne Chatinhalte.
- **Admin › Buchungen:** nur lesend, filterbar nach Konto, Art und Datum. „Als CSV exportieren“
  je Monat und Konto (Semikolon, Dezimalkomma, öffnet sich direkt in Excel/LibreOffice).

## Abrechnungsregeln der Anbieter (Stand 10.10.2026)

| Anbieter | Gemeldete Felder | Was MultiGPT daraus macht |
|---|---|---|
| Anthropic | `input_tokens` (ohne Cache), `cache_creation_input_tokens`, `cache_read_input_tokens`, `cache_creation.ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens`, `output_tokens` (inkl. Thinking), `output_tokens_details.thinking_tokens`, `server_tool_use.web_search_requests` | Eingabe = Summe der drei Eingabefelder; Cache-Schreiben nach 5 Min./1 Std. aufgeteilt; Websuchen als Einheit `web_search` |
| OpenAI | `prompt_tokens` (inkl. Cache), `prompt_tokens_details.cached_tokens` und `cache_write_tokens`, `completion_tokens` (inkl. Reasoning), `completion_tokens_details.reasoning_tokens` | direkt übernommen |
| Gemini | `promptTokenCount` (inkl. `cachedContentTokenCount`), `candidatesTokenCount`, `thoughtsTokenCount`, `toolUsePromptTokenCount` | Eingabe = Prompt + Werkzeug-Prompt; Ausgabe = Kandidaten + Gedanken |
| LM Studio | `prompt_tokens`, `completion_tokens` | nur Summen |

Quellen:

- Anthropic: [Messages API (usage)](https://platform.claude.com/docs/en/api/messages/create),
  [Prompt Caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching),
  [Streaming](https://platform.claude.com/docs/en/build-with-claude/streaming),
  [Thinking und Kosten](https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost),
  [Preise](https://platform.claude.com/docs/en/about-claude/pricing)
- OpenAI: [Chat Completions (usage)](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/retrieve),
  [Prompt Caching](https://developers.openai.com/api/docs/guides/prompt-caching),
  [Reasoning](https://developers.openai.com/api/docs/guides/reasoning),
  [Preise](https://developers.openai.com/api/docs/pricing)
- Google: [generateContent (UsageMetadata)](https://ai.google.dev/api/generate-content),
  [Thinking](https://ai.google.dev/gemini-api/docs/generate-content/thinking),
  [Caching](https://ai.google.dev/gemini-api/docs/generate-content/caching),
  [Preise](https://ai.google.dev/gemini-api/docs/pricing)

Nicht abgebildet: Batch-, Flex- und Priority-Tarife, Speichergebühren für explizites Caching bei
Gemini und Freikontingente (z. B. Gemini-Websuche). Solche Abweichungen gleicht der Verwalter bei
Bedarf über den Preis aus.
