# Chat-Einstellungen: Grundregeln und Kreativität

Zwei Dinge bestimmen, wie sachlich die Modelle antworten: die **Grundregeln** (Text am Anfang
jedes System-Prompts) und die **Kreativität** (Temperatur). Beides legt der Verwalter im Admin
unter **Chat → Chat-Einstellungen** fest. Die Kreativität lässt sich zusätzlich je Projekt und je
Chat wählen.

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
- „+ fester Prompt deiner Rolle“, falls die Rolle einen hat (nur der Hinweis, nicht der Text),
- „+ technische Hinweise zu Werkzeugen und Quellen“ (nur der Hinweis, nicht der Text),
- „+ Projekt-Anweisungen (Projektname): …“, gekürzt und aufklappbar. Das sieht nur der
  Besitzer des Chats, Empfänger geteilter Chats sehen fremde Projekte nicht,
- das Feld für den eigenen System-Prompt des Chats und die Auswahl **Kreativität**.

Ändern dürfen beides der Besitzer und Empfänger mit dem Recht „Bearbeiten“ (U, siehe
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
| OpenAI | o1, o3, o4-mini, gpt-5 (auch mini, nano), gpt-5.x, gpt-6 | Reasoning-Modelle: nur der Standard. Ab gpt-5.1 nur mit Reasoning „none“ erlaubt, das MultiGPT nicht setzt |
| Anthropic | claude-opus-4-7 und neuer, alle Claude-5-Modelle (Opus, Sonnet, Haiku), Fable, Mythos | Werte außer dem Standard ergeben HTTP 400. Ebenso jede Anfrage mit Extended Thinking |
| Google | gemini-3 und neuer | Google rät dringend zum Standard 1,0 (sonst Schleifen, schlechtere Ergebnisse) |
| LM Studio, Ollama | – | Temperatur wird immer übergeben |

Die Regel richtet sich nach der Modell-ID und gilt auch mit Präfix (z. B. `openai/gpt-5` über
OpenRouter). Lehnt ein anderes Modell die Temperatur ab, wiederholt MultiGPT die Anfrage
automatisch ohne sie und merkt sich das Modell bis zum nächsten Neustart des Dienstes. Im Journal
steht dann nur „Modell <ID>: Temperatur abgelehnt, künftig ohne“ (Datenbank-ID, kein Text).

## Quellen

Gelesen am 2026-10-10:

- OpenAI, Reasoning: <https://developers.openai.com/api/docs/guides/reasoning>
- OpenAI, neueste Modelle („When reasoning effort is not `none`, remove `temperature`, `top_p`,
  and `top_logprobs`“): <https://developers.openai.com/api/docs/guides/latest-model>
- Anthropic, Migrationsleitfäden (Sampling-Parameter ab Claude Opus 4.7 bzw. in Claude 5):
  <https://platform.claude.com/docs/en/models/opus-5-5/migration-guide>,
  <https://platform.claude.com/docs/en/models/sonnet-5-5/migration-guide>,
  <https://platform.claude.com/docs/en/models/haiku-5-5/migration-guide>
- Google, Gemini 3 („keeping the temperature parameter at its default value of 1.0“):
  <https://ai.google.dev/gemini-api/docs/gemini-3>
