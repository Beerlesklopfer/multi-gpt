# Bilder erzeugen

Chatmodelle können Bilder meist nicht selbst erzeugen. Bitten Nutzer z. B. „Zeichne mir die
Deutschlandflagge mit Bundesadler“, schreiben viele Modelle nur SVG-Code. MultiGPT sucht deshalb
selbst ein **Bildmodell** und gibt den Auftrag dorthin weiter. Das Bild erscheint in der Antwort
wie ein Anhang, mit Vorschau, Vergrößerung (Klick) und Download.

## Bildmodell einrichten

1. **Anbieter anlegen** (Admin → Chat → Anbieter), z. B. „OpenAI“ mit Art *OpenAI-kompatibel*
   und API-Key, oder „Google“ mit Art *Google* (Gemini-API-Key). Anthropic bietet keine
   Bilderzeugung an.
2. **Modell anlegen** bzw. mit „Modelle abrufen“ übernehmen und als **Fähigkeit
   „Bilderzeugung“** markieren. MultiGPT erkennt das bei neuen Modellen selbst:
   - OpenAI: `gpt-image-1`, `gpt-image-1-mini`, `gpt-image-1.5`, `gpt-image-2`,
     `chatgpt-image-latest`, ältere `dall-e-3`/`dall-e-2`
   - Google: `gemini-2.5-flash-image`, `gemini-3-pro-image`, `gemini-3.1-flash-image`
     (Imagen ist von Google abgeschaltet)
3. **Rolle freigeben** (Admin → Konten → Rollen): Häkchen **„Bilder erzeugen und bearbeiten“** und
   das Bildmodell bei den erlaubten Modellen (oder „alle Modelle“). Kinder- und Gastrollen haben
   das Recht anfangs nicht.
4. Optional **Standard-Bildmodell** wählen: Admin → Chat → Chat-Einstellungen, Abschnitt „Bilder“.
   Ohne Angabe – oder wenn das Standardmodell für ein Konto gesperrt ist – nimmt MultiGPT das
   erste freigegebene Bildmodell (Reihenfolge der Modelle).

MultiGPT wählt nur ein Bildmodell, das aktiv ist, dessen Anbieter aktiv und erreichbar ist, das
die Rolle erlaubt und das kein ausgeschöpftes Budget sperrt. Findet es keins, steht im Chat:
„Es ist kein Modell zur Bilderzeugung eingerichtet bzw. freigegeben.“

## Drei Wege zum Bild

### 1. Das Chatmodell ruft das Werkzeug auf

Chatmodelle mit Häkchen **„Werkzeuge“** bekommen das eingebaute Werkzeug `generate_image`
(Beschreibung, Format quadratisch/hoch/quer, Qualität, transparenter Hintergrund). MultiGPT sagt
dem Modell im System-Prompt, dass es Bildwünsche damit erfüllt und keine Bilder als SVG oder
Code zeichnet – außer der Nutzer verlangt ausdrücklich Code. In der Antwort steht der Aufruf als
Zeile „Werkzeug generate_image (Bilderzeugung)“, darunter das Bild. Das Chatmodell selbst sieht
das Bild nicht, es bekommt nur die Bestätigung „Bild erzeugt (Anhang #…, Größe)“.

Je Antwort entstehen höchstens 4 Bilder.

### 2. Modus „Bild“ im Eingabefeld

Mit dem Schalter **„Bild erzeugen“** über dem Eingabefeld geht die Nachricht als Bildbeschreibung
direkt an das Bildmodell, ohne Chatmodell. Daneben stehen **Format** (quadratisch, hoch, quer)
und **Qualität** (automatisch, niedrig, mittel, hoch); beides merkt sich der Browser. Der
Schalter erscheint nur, wenn für das Konto ein Bildmodell nutzbar ist.

Im Modus „Bild“ gehen keine Anhänge mit; Bilder bearbeiten folgt später (M9-02). „Neu erzeugen“
an einer Bild-Antwort fragt derzeit das gewählte Chatmodell.

### 3. Hinweis bei Modellen ohne Werkzeuge

Kann das gewählte Chatmodell keine Werkzeuge und klingt die Nachricht wie ein Bildauftrag
(„zeichne mir …“, „male ein …“, „erstelle ein Bild …“, „draw me …“, „generate an image …“),
zeigt MultiGPT vor dem Senden einen Hinweis mit **„Mit Bildmodell erzeugen“** (schaltet den Modus
„Bild“ ein) und **„Trotzdem an das Chatmodell“**. Nichts wird ohne Zustimmung umgeleitet. Wer
ausdrücklich SVG, ASCII oder Code möchte, bekommt keinen Hinweis.

## Rückfrage vor jedem Bild

Bilder kosten Geld. Standard: Das Werkzeug läuft **ohne Rückfrage**, weil der Nutzer das Bild
ausdrücklich gewünscht hat; das Budget wird vor jedem Bild geprüft. Wer es strenger möchte,
schaltet in den Chat-Einstellungen **„Rückfrage vor Bilderzeugung“** ein. Dann wartet jeder
Aufruf von `generate_image` auf „Ausführen“ oder „Ablehnen“, wie bei MCP-Werkzeugen mit Rückfrage.
Der Modus „Bild“ fragt nie nach (der Nutzer hat ihn selbst gewählt).

## Kosten

Jedes Bild ist eine eigene Buchung auf dem Abrechnungskonto des Bildmodells (siehe
[Kosten und Budgets](Kosten-und-Budgets)), gebucht auf das Konto, das das Bild ausgelöst hat –
in geteilten Chats also auf den Absender, nicht auf den Besitzer des Chats.

Preise pflegt der Verwalter unter **Modellpreise** im Feld „Gebühren je Einheit“ als
`{Einheit: Preis}`. Die Einheit eines Bildes ist `image:<qualität>:<größe>`; fehlt dieser
Schlüssel, gilt `image:<qualität>`, dann `image`. Beispiele (Preise bitte beim Anbieter
nachsehen, sie ändern sich):

```json
{"image:low": 0.011, "image:medium": 0.042, "image:high": 0.167, "image:high:1536x1024": 0.25, "image": 0.042}
```

Die Qualität „automatisch“ (`image:auto`) fällt auf `image` zurück. OpenAI rechnet GPT-Image-
Modelle eigentlich nach Tokens ab (Text und Bild) und meldet sie in der Antwort; Stückpreise je
Qualität und Größe sind dafür eine gute Näherung.

## Datenschutz und Sicherheit

- Die Bildbeschreibung geht nur an den Anbieter des Bildmodells, das Bild nie an das Chatmodell.
- Erzeugte Bilder werden wie hochgeladene neu kodiert: ohne EXIF, XMP, ICC-Profil und
  Textblöcke (manche Generatoren schreiben dort den Prompt hinein), längste Kante 2048 px,
  höchstens `ATTACHMENT_MAX_IMAGE_MB` (siehe [Konfiguration](Konfiguration)).
- Ausgeliefert werden Bilder nur an Konten, die den Chat lesen dürfen. Verwalter sehen im Admin
  keine Bilder und keine Beschreibungen; im Log stehen nur Nummern.
- In geteilten Chats braucht Erzeugen das Recht **Schreiben** (W).
- Lehnt der Inhaltsfilter des Anbieters eine Beschreibung ab, erscheint: „Der Anbieter hat die
  Bildanfrage wegen seiner Inhaltsrichtlinien abgelehnt. Bitte die Beschreibung umformulieren.“

## Fehlersuche

| Meldung | Ursache und Abhilfe |
|---|---|
| Es ist kein Modell zur Bilderzeugung eingerichtet bzw. freigegeben. | Kein aktives Modell mit Fähigkeit „Bilderzeugung“ bei einem OpenAI-kompatiblen oder Google-Anbieter, oder die Rolle erlaubt es nicht. |
| Die Bilderzeugung ist für dieses Konto nicht freigegeben. | Rolle ohne „Bilder erzeugen und bearbeiten“. |
| Schalter „Bild erzeugen“ fehlt | Wie oben, oder das Budget des Bildmodells ist ausgeschöpft bzw. der Anbieter offline. |
| Der API-Key ist abgelaufen … / Zugang abgelehnt | Key beim Anbieter erneuern bzw. prüfen. Bei OpenAI muss die Organisation für GPT-Image-Modelle verifiziert sein. |
| Modell oder Adresse beim Anbieter nicht gefunden | Modell-ID prüfen; manche Modelle sind nur für freigeschaltete Konten verfügbar. |
