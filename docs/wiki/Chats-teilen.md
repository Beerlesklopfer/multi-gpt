# Chats teilen

Einen Chat kannst du mit anderen Familienmitgliedern teilen: mit einzelnen Konten oder mit einer
Gruppe (z. B. „Familie“ oder „Kinder“). Wer einen geteilten Chat bekommt, findet ihn in der
Seitenleiste unter **„Mit mir geteilt“**. Je Freigabe legst du fest, was die anderen dürfen.

Diese Seite richtet sich an alle Nutzer und an Verwalter.

## Rechte (RWUD)

Die Rechte heißen wie bei Datenbanken nach Lesen, Schreiben, Aktualisieren und Löschen. Lesen ist
immer dabei, die anderen drei wählst du einzeln an oder ab. In der Liste der Freigaben steht die
Kurzform, z. B. „R“, „RW“ oder „RWUD“.

| Kürzel | Im Dialog | Was es erlaubt |
|---|---|---|
| **R** | Lesen (immer an) | Chat mit allen Versionen und Zweigen, Quellen und Anhängen ansehen, Nachrichten kopieren, als Markdown exportieren, Versionen für sich selbst umschalten, „Als eigene Kopie fortsetzen“ |
| **W** | Schreiben | neue Nachrichten senden, auch mit Bildern und Dateien (Büroklammer, Einfügen, Ziehen); Antworten neu erzeugen (als neue Version, nichts wird überschrieben); Modell für die eigene Nachricht wählen |
| **U** | Bearbeiten | vorhandene Nachrichten bearbeiten, eigene und fremde (es entsteht eine neue Version; dafür braucht es zusätzlich **W**, weil eine neue Antwort entsteht); Chat umbenennen; System-Prompt des Chats ändern |
| **D** | Löschen | Chat archivieren oder endgültig löschen – **für alle**, also auch für den Besitzer |

Einzelne Nachrichten oder Zweige lassen sich in MultiGPT nicht löschen, auch nicht vom Besitzer.

Nur wer den Chat angelegt hat (der **Besitzer**), kann teilen, Rechte ändern und Freigaben
widerrufen. Den Besitz übertragen geht nicht. Teilen dürfen Konten, deren Rolle das Recht „Mit
Gruppen teilen“ hat (Startrollen: Verwalter und Erwachsene).

Die Rechte des Kontos gelten weiter: Ein Konto ohne Chat-Recht (z. B. ohne Rolle) kann einen
geteilten Chat nur lesen, auch wenn die Freigabe mehr erlaubt. Welche Modelle jemand im geteilten
Chat wählen kann, richtet sich nach seiner eigenen Rolle und seinem Budget.

## Teilen, ändern, widerrufen

1. Chat öffnen und oben auf **„Teilen“** klicken.
2. Unter „Neu freigeben“ ein Konto oder eine Gruppe wählen, die Kästchen **Schreiben**,
   **Bearbeiten** und **Löschen** nach Wunsch setzen und **„Freigeben“** klicken.
3. In der Liste darüber lassen sich die Rechte jeder Freigabe direkt umschalten.
   **„Widerrufen“** entfernt die Freigabe.

Ein Widerruf greift **sofort**: Schon die nächste Anfrage des Empfängers bekommt keinen Zugriff
mehr, die Seite meldet „Die Freigabe für diesen Chat wurde beendet“. Läuft gerade eine Antwort,
die der Empfänger ausgelöst hat, bricht sie nach höchstens etwa zwei Sekunden ab. Das gilt auch,
wenn jemand aus einer Gruppe entfernt wird, an die geteilt ist.

Eigene geteilte Chats tragen in der Seitenleiste ein kleines Personen-Symbol.

## Als Empfänger

- Oben im Chat steht, wer ihn geteilt hat und was du darfst, z. B.
  „Geteilt von Anna · Lesen, Schreiben“.
- **„Als eigene Kopie fortsetzen“** legt einen neuen, eigenen Chat mit dem gerade angezeigten
  Verlauf an. Anhänge werden mitgenommen (als Verweis auf dieselbe Datei; sie bleiben erhalten,
  auch wenn das Original gelöscht wird). Kosten werden nicht mitkopiert.
- **„Aus meiner Liste entfernen“** hebt die Freigabe für dich auf. Bei einer Gruppenfreigabe gilt
  das nur für dich, die anderen Gruppenmitglieder sehen den Chat weiter. Teilt der Besitzer
  erneut, siehst du den Chat wieder.
- Wer mit **Löschen**-Recht archiviert oder löscht, bekommt eine deutliche Rückfrage, z. B.
  „Der Chat gehört Anna und wird für alle gelöscht.“ Der Besitzer wird darüber nicht
  benachrichtigt.

## Mehrere Personen in einem Chat

- An jeder Nutzernachricht steht, wer sie geschrieben hat („Du“ bzw. der Name), sobald mehr als
  eine Person im Chat ist – auch im Export.
- **Kosten** einer Antwort gehen auf das Konto dessen, der sie ausgelöst hat, nicht auf den
  Besitzer. Das zählt für „Mein Verbrauch“ und das Monatsbudget.
- **Versionen:** Jeder sieht seinen eigenen Zweig. Umschalten („‹ 1/2 ›“) ändert nur die eigene
  Ansicht, nie die der anderen. Der Besitzer sieht den Hauptpfad; Empfänger folgen ihm, bis sie
  selbst umschalten. Schreibt ein Empfänger am Ende des Hauptpfads weiter, sehen alle die neue
  Nachricht. Neu erzeugen und Bearbeiten legen neue Versionen an und lassen das Bestehende
  stehen.
- **Gleichzeitig schreiben:** Es geht nichts verloren. Hat jemand anderes inzwischen
  geschrieben, lehnt MultiGPT das Senden mit „Inzwischen gibt es neue Nachrichten in diesem
  Chat. Bitte neu laden und dann senden.“ ab; dein Text bleibt im Eingabefeld. Beim Zurückkehren
  ins Fenster und alle 30 Sekunden prüft die Seite, ob es Neues gibt, und zeigt dann „Neue
  Nachrichten in diesem Chat“ mit **„Neu laden“**. Live mitlesen wie in einem Messenger gibt es
  nicht.
- **Werkzeuge mit Rückfrage** (MCP) bestätigt nur, wer die Antwort ausgelöst hat.

## Datenschutz

- Eine Antwort bekommt immer den Kontext dessen, der sendet: den festen Prompt **seiner** Rolle,
  **seinen** Zitierstil, **seine** Werkzeuge, Websuche und Sammlungen. Persönliche Einstellungen
  des Besitzers gehen nicht an Antworten, die ein Empfänger auslöst. Der System-Prompt des Chats
  gilt für alle.
- Werkzeugergebnisse (z. B. aus einem MCP-Server des Besitzers) aus früheren Antworten gehen bei
  Antworten anderer Personen nur als Antworttext an das Modell, nie die Rohdaten.
- Sammlungen wählt jeder im Chat nach seinen eigenen Leserechten. Stehen unter einer Antwort
  Quellen aus einer Sammlung, die du nicht lesen darfst, siehst du nur Titel und Seite, ohne Link
  und ohne Literaturangaben; die Abschnittsansicht ist für dich gesperrt.
- **Verwalter** sehen fremde Chats nicht, auch nicht über das Teilen – außer jemand teilt einen
  Chat ausdrücklich mit ihnen. Im Admin erscheinen Freigaben nur als Metadaten (Besitzer,
  Empfänger, Rechte), nie Titel oder Inhalte. Die **Einsicht** in Chats von Jugendlichen (Seite
  „Familie“) ist davon getrennt und bleibt nur lesend, ohne Kopie.

## Für Verwalter

- Die Freigaben liegen in der Tabelle `Share` (wie bei Sammlungen), im Admin unter „Freigaben“
  nur zum Ansehen. Die eigene Ansicht je Empfänger steht in `ConversationView`, der Verfasser
  jeder Nachricht in `Message.author`.
- Die Migration `chat.0021_chat_sharing` bringt die Felder mit. Bestehende Nachrichten haben
  keinen Verfasser und zählen wie bisher für den Besitzer des Chats.
