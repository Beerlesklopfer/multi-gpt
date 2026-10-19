# FLUX.1 über den Rust-Runner

Planungsstand: 2026-10-17. Entwurf, noch keine Implementierung oder Installation.

## Ziel und vereinbarter Rahmen

FLUX.1 läuft auf dem GPU-Rechner WS-Joerg in einem vom vorhandenen Rust-Runner
verwalteten Podman-Container. MultiGPT vergibt Bildaufträge und übernimmt Ergebnisse
in die bestehende Chat-Historie. Derselbe Runner übernimmt den Lebenszyklus des
FLUX-Dienstes; ein zweites Agent-Programm ist nicht vorgesehen.

- FLUX-Laufzeit und Python-Abhängigkeiten gehören in ein versioniertes Image.
- Modellgewichte und Download-Cache bleiben in lokal freigegebenen Volumes erhalten.
- Ergebnisse gehören zur jeweiligen Chat-Version. Neue Erzeugungen überschreiben
  keine früheren Ergebnisse; die Versionen bleiben auswählbar und vergleichbar.
- Keine zusätzliche dauerhafte Ergebnisablage auf WS-Joerg. Dort entstehen nur
  temporäre Dateien bis zur bestätigten Übernahme durch MultiGPT.
- Grundlage ist die laufende Installation `~/D/f/flux1-project` auf WS-Joerg.
  Ihr Quellcode und die konkrete Laufzeit wurden hier noch nicht eingesehen.

## Vorhandene Bausteine und notwendige Erweiterungen

| Bereich | Vorhanden | Ergänzung für FLUX |
|---|---|---|
| Verbindung | Pairing, ausgehender WebSocket, signierte Serverbefehle, Dienstkatalog | Fähigkeiten und Version des FLUX-Profils aushandeln; ältere Runner bleiben nutzbar |
| Container | feste Vorlage, lokale Grenzen, Prüfung vor und nach dem Start | eigene feste Vorlage für GPU und freigegebene Modell-Volumes |
| Bilderzeugung | `chat/images.py`, Anbieteradapter, Bildmodus und Werkzeug | Adapter, der Bildaufträge über den Runner zustellt |
| Versionen | `Message.parent`: Nachrichten mit gleichem Vorgänger sind Versionen | Bildmodell und Parameter bei „Neu erzeugen“ erhalten; LOP C1 berücksichtigen |
| Bildablage | `Attachment` an Antwort, Dateifelder im Medienspeicher | Generierungsparameter und Herkunft revisionsbezogen speichern |
| Betrieb | Runner-Verwaltung, Audit, Status und Laufzeitgrenzen | Laden/Entladen, Warteschlange, Abbruch und GPU-Belegung |

„In der DB“ bezeichnet hier die bestehende Verwaltung in MultiGPT: Metadaten und
Versionsbezüge liegen in PostgreSQL, Bildbytes derzeit im geschützten Medienspeicher
(`Attachment.file`, Vorschaubild). Dieser Plan stellt die Speicherung nicht auf
Binärdaten in PostgreSQL um.

## Geplanter Ablauf

1. Der Verwalter aktiviert das lokal eingerichtete FLUX-Profil eines gekoppelten
   Runners und ordnet es einem Bildmodell in MultiGPT zu.
2. Bildmodus und `generate_image` nutzen dieselbe Rechteprüfung und dieselbe
   Auftragslogik. Der Server legt die Zielantwort im Nachrichtenbaum fest.
3. Der Server speichert einen Auftrag mit eindeutiger ID, Modellstand und konkreten
   Parametern. Der Runner bestätigt Annahme oder meldet einen begründeten Fehler.
4. Der Runner startet bzw. verwendet den FLUX-Container, reserviert dessen GPU und
   übergibt einen strukturierten Auftrag an die interne FLUX-Schnittstelle.
   Generierung erfolgt nicht durch frei zusammengesetzte Shellbefehle.
5. Fortschritt erscheint im Chat: wartet, lädt Modell, erzeugt Bild, überträgt Ergebnis.
6. Bilder werden begrenzt und stückweise übertragen. Der Server prüft sie und speichert
   sie über den vorhandenen Attachment-Weg an genau der vorgesehenen Antwort.
7. Erst nach dauerhaft bestätigter Übernahme entfernt der Runner temporäre Ergebnisse.
   Ein Verbindungsabbruch darf weder eine neue Chat-Version noch eine zweite Buchung
   oder doppelte Anhänge erzeugen.
8. „Neu erzeugen“ erstellt eine weitere Antwortversion. Ein Wechsel des aktiven
   Chat-Zweigs während eines Auftrags verschiebt dessen Ergebnis nicht.

Ein technischer Wiederholungsversuch desselben Auftrags ist keine neue Revision.
Eine vom Nutzer angeforderte neue Erzeugung bekommt dagegen eine neue Auftrags-ID
und eine eigene Antwortversion.

## Container und persistente Daten

Vorschlag: ein eigenes Profil `flux`, getrennt von normalen Coding-Sitzungen.
Entschieden am 2026-10-17: Die GPU wird bei der Ersteinrichtung ausgewählt.
Der Runner zeigt die erkannten Geräte an und speichert die Auswahl lokal;
es gibt keine automatische Freigabe des ersten Geräts. Solange die Auswahl
fehlt, darf kein GPU-Container starten. Vor einem späteren Start muss die
gespeicherte Geräteidentität erneut abgeglichen werden (geänderte Gerätenummern).
GPU-Zugriff und Mounts werden ausschließlich lokal vom Betreiber eingerichtet;
Server und Chatmodell können keine beliebigen Geräte oder Hostpfade anfordern.
Die existierende Coding-Vorlage behält ihre bisherigen Grenzen.

| Ort | Inhalt | Lebensdauer |
|---|---|---|
| Image mit festem Digest | FLUX-Laufzeit, Adapter im Container, Python-Pakete | Wechsel durch freigegebenes Update |
| Modell-Volume | Gewichte, Tokenizer und erforderliche Modelldateien | über Containerwechsel hinweg |
| Cache-Volume | wiederverwendbare Downloads und gegebenenfalls Laufzeit-Cache | persistent, gezielt löschbar |
| temporärer Arbeitsbereich | Eingaben und erzeugte Bilddateien | bis Übernahme, Abbruch oder begrenzter Aufbewahrungsfrist |
| MultiGPT | Bildanhänge, Parameter, Herkunft, Chat-Versionen | bestehende Chat-Aufbewahrung |

Empfehlung: Gewichte im Generierungsbetrieb nur lesbar einbinden. Ein gesonderter
Wartungslauf darf sie nach Freigabe herunterladen oder aktualisieren. Benötigt die
vorhandene Laufzeit Schreibzugriff, wird der genaue Bedarf bei der Bestandsaufnahme
ermittelt. Für die erste Stufe keine öffentlichen HTTP-Ports; Kommunikation nur
über den Runner bzw. ein internes Container-Netz.

GPU-Hersteller, Treiber, Container-Unterstützung und Rootless-Betrieb müssen auf
WS-Joerg geprüft werden. Die konkrete Gerätefreigabe wird erst danach festgelegt.

Vom Nutzer gemeldeter Hardwarestand: AMD Navi 31, PCI `0000:03:00.0`, Treiber
`amdgpu`; der Bildschirm läuft an dieser GPU mit 3440 × 1440. Die Sammelbezeichnung
„Radeon RX 7900 XT/7900 XTX/7900 GRE/7900M“ identifiziert weder das genaue Modell
noch die VRAM-Größe. Beides bleibt zu ermitteln. Für F1 sind daher eine passende
AMD-Rechenlaufzeit und deren Kompatibilität mit FLUX, Host und Rootless-Container
zu prüfen. Der gemeldete Grafiktreiber allein bestätigt diese Unterstützung nicht.
Der daneben gemeldete HDMI/DP-Audioteil benötigt keine Freigabe für FLUX.

## Aufträge, Parameter und Ressourcen

Ein persistenter Bildauftrag benötigt mindestens: Auftrags-ID, auslösender Nutzer,
Chat und Zielantwort, Runner und Profil, Status, Zeitpunkte, Fehler, Eingabeparameter
und Ergebnisbezüge. Vorschlag: eigenes Auftragsmodell mit Verbindung zu `Message`;
keine alleinige Speicherung eines laufenden Auftrags im Worker-Speicher.

Für jede Erzeugung festhalten: Prompt, tatsächlich verwendeter Seed, Modellkennung
und Modellstand, Image-Digest, Breite/Höhe, Schrittzahl, Guidance und weitere von der
installierten FLUX-Variante unterstützte Parameter. Nicht unterstützte Parameter
werden abgewiesen; keine stille Übersetzung von „Qualität“ in beliebige Werte.
Ein gleicher Seed ermöglicht kontrollierte Vergleiche, garantiert aber keine
bitgleichen Ergebnisse über andere Hardware oder Softwarestände hinweg.

Empfehlung für den ersten Ausbau:

- Ein aktiver Generierungsauftrag je freigegebener GPU, weitere Aufträge warten.
- GPU-Lauf und Wartung schließen sich gegenseitig aus.
- Bildanzahl, Pixelzahl, Laufzeit, RAM, temporärer Speicher und Transfergröße begrenzen.
- Abbruch bis in die Laufzeit weiterreichen; nach Frist den zugehörigen Container
  stoppen, ohne Modell-Volumes oder fremde Container zu löschen.
- Nach Neustart Aufträge abgleichen: übernehmen, Ergebnis erneut zustellen oder
  nachvollziehbar als unterbrochen markieren. Keine automatische Neugenerierung
  mit neuem Seed und keine unbegrenzten Wiederholungen.
- LM Studio kann dieselbe physische GPU belegen. Der Runner koordiniert zunächst
  nur seine eigenen Aufträge; fremde Modelle werden nicht ungefragt entladen.
  Ein gemeinsames Reservierungskonzept ist vor parallelem Betrieb festzulegen.

Die vorhandene Bildaufbereitung begrenzt derzeit die längste Bildkante auf 2048 px
und kodiert Bilder neu. Vor höheren FLUX-Auflösungen ist zu entscheiden, ob zusätzlich
ein Original erhalten bleiben soll; im ersten Ausbau gelten die vorhandenen Grenzen.

## Wartung über denselben Runner

Vorgesehene Aktionen: Status und Version anzeigen, Logs abrufen, Dienst starten/
stoppen, Modell laden/entladen, freigegebenes Image wechseln, Modellgewichte
bereitstellen. Updates erfolgen im Leerlauf mit vorherigem Funktionstest und
Rückkehr zum bisherigen Image-Digest. Modell- und Cache-Volumes bleiben dabei erhalten.

Generierung darf keinen allgemeinen Wartungszugriff erhalten. Verwaltung, Download
von Modellen und Löschen von Caches benötigen eigene Berechtigungen und Audit-Einträge.
Eine interaktive Coding-Sitzung im GPU-Container ist kein Bestandteil des ersten Ausbaus.

## Umsetzung in prüfbaren Schritten

| Schritt | Ergebnis | Abnahme |
|---|---|---|
| F1 Bestandsaufnahme | GPU/Treiber, FLUX-Variante, Laufzeit, Abhängigkeiten, Modellpfade und Startablauf von WS-Joerg dokumentiert | ein vorhandener lokaler Beispielauftrag mit Parametern und Ergebnis nachvollzogen |
| F2 Container-Profil | versioniertes Image, GPU, lokale Volumes und internes Auftragsinterface | Modell laden und ein Bild erzeugen; Containerwechsel behält Gewichte; normales Coding-Profil erhält keine GPU-/Mountrechte |
| F3 Runner-Steuerung | Dienstkatalog, Fähigkeiten, Start/Stop/Status/Wartung | ohne GPU oder mit inkompatiblem Runner klare Meldung; kein beliebiger Hostzugriff |
| F4 Auftragsweg | persistente Aufträge, Fortschritt, Transfer, Bestätigung und Abbruch | Wiederverbindung und doppelte Zustellung erzeugen keine doppelten Ergebnisse; Grenzen und Abbruch greifen |
| F5 Chat-Integration | Bildmodell, Bildmodus/Werkzeug, Parameter und Antwortversionen | zwei Erzeugungen bleiben nebeneinander als Versionen erhalten; „Neu erzeugen“ nutzt wieder das Bildmodell; Freigaben gelten für alle Anhänge |
| F6 Betrieb und Auslieferung | Wartungsablauf, GPU-Konflikte, Kontingente, Dokumentation und Release-Artefakte | echter Test auf WS-Joerg einschließlich Neustart, belegter GPU und Image-Rollback |

Keine Änderung der laufenden Installation, kein Modell-Download und kein Release
werden allein durch dieses Planungsdokument ausgelöst.

## Noch zu klären

1. AMD Navi 31 mit `amdgpu` ist bestätigt. Welches genaue Kartenmodell, wie viel
   VRAM, welches Betriebssystem und welche Rechenlaufzeit sind installiert?
2. Welche FLUX.1-Variante und welche Laufzeit nutzt `flux1-project` tatsächlich?
   Gibt es bereits eine Auftrags-API oder nur CLI-Aufrufe?
3. Wo liegen die bereits heruntergeladenen Gewichte? Können sie ohne Kopie als
   lokal freigegebener Speicher übernommen werden?
4. Wie werden FLUX und LM Studio auf derselben GPU zeitlich koordiniert?
5. Welche Parameter braucht die erste Chat-Oberfläche? Empfehlung: Format/Größe,
   Seed und die für die konkrete Variante sinnvollen Generierungsparameter.
6. Wie sollen GPU-Nutzung und Wartezeiten kontiert werden? Vorhandene Runner-
   Container-Minuten und Bildkosten nicht ungeprüft gleichzeitig buchen.
7. Genügt zunächst die vorhandene Versionsauswahl mit Bildansicht, oder wird eine
   neue Ansicht für den direkten Vergleich zweier Versionen benötigt?
8. Welche Image-/Modellartefakte dürfen ausgeliefert werden, welche werden nur auf
   WS-Joerg installiert? Modellzugang und Bedingungen bei F1 prüfen.

Nächster Schritt: F1 anhand des tatsächlichen Projekts auf WS-Joerg abschließen;
anschließend Container-Profil und Auftragsinterface konkret spezifizieren.

## Begonnene Umsetzung

Erster Baustein: `runner-rs/multigpt-runner/src/flux.rs` und Einbindung in
`Config`. Ohne FLUX-Einstellungen bleibt das Profil deaktiviert. Lokale Variablen
`MGPT_FLUX_IMAGE` (mit Digest), `MGPT_FLUX_MODELS_VOLUME` und
`MGPT_FLUX_CACHE_VOLUME` beschreiben das Profil. `MGPT_FLUX_RENDER_DEVICE` hält
die spätere lokale GPU-Auswahl fest; es gibt keinen Standardwert. Eine fehlende
Auswahl ist ein Zustand vor Abschluss der Ersteinrichtung, keine Gerätefreigabe.
Hostpfade als Volume-Namen, breite Gerätepfade und Images ohne Digest werden
abgewiesen. Die Freigabe erweitert nicht die Coding-Image-Liste.

Noch nicht implementiert: Geräteerkennung und interaktive Ersteinrichtung,
GPU-Containerstart, FLUX-Laufzeit und Auftragsweg. Daher ist dies noch kein
einsatzfähiger FLUX-Dienst; die Variablen starten keine Container.
