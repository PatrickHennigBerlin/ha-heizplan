# Heizplan für Home Assistant

Wochenpläne für Heizungen mit **echter Solltemperatur**: Die Raumregler bleiben auf
*Komfort*, Heizplan schreibt zu jeder Zeit genau den Wert, der gerade gelten soll.
Was in der Karte als **Soll** steht, wird auch geregelt – kein Rätseln mehr über
Standby-/Nacht-Absenkungen im Regler.

![Übersicht](docs/uebersicht.png)
![Wochenplan](docs/wochenplan.png)
![Alle Räume](docs/alle_raeume.png)

## Funktionen

- **Übersicht aller Räume** – Ist, Soll, Quelle (Plan / Manuell / Abwesend), Ventil %, Tagesverlauf, nächster Wechsel
- **Alle Räume auf einen Blick** – ein Tag (Mo–So wählbar), alle Räume untereinander, direkt bearbeitbar
- **Wochenraster je Raum** – Blöcke antippen zum Ändern, Kanten ziehen zum Verschieben (15-min-Raster), Tag auf andere Tage kopieren
- **Vorlagen** – Plan eines Raums als Vorlage speichern, auf beliebige Räume anwenden
- **Kurzzeit-Überschreiben** – ± in der Übersicht: 2 Stunden wärmer/kühler, danach automatisch zurück zum Plan
- **Modus** Wochenplan / Abwesend (feste Temperatur) / Aus
- **Heizperiode**: optional eine Entity (z. B. `switch.heizperiode`) – ist sie aus, schreibt Heizplan nichts
- Je Raum ein Sensor `sensor.heizplan_<raum>_soll` mit Attributen (Quelle, Block, nächster Wechsel, Ist, Ventil)
- **Schalt-Räume** (z. B. elektrische Fußbodenheizung über `switch.*`): Blöcke mit An/Aus statt Temperatur, eigene aktive Monate, optional unabhängig von der Heizperiode (`heizplan.update_room`)
- Übernahme bestehender Zeitpläne aus dem *Scheduler-Component* (`heizplan.import_scheduler`) – Temperatur- und An/Aus-Zeitpläne

## Installation (HACS)

1. HACS → ⋮ → *Benutzerdefinierte Repositories* → `https://github.com/PatrickHennigBerlin/ha-heizplan`, Typ **Integration**
2. *Heizplan* herunterladen, Home Assistant neu starten
3. Einstellungen → Geräte & Dienste → *Integration hinzufügen* → **Heizplan**
   - Heizperiode-Entity wählen (optional)
   - „Regler auf Komfort halten“ aktiv lassen
4. Karte ins Dashboard: `type: custom:heizplan-card` (die Karte wird von der Integration mitgeliefert, keine extra Ressource nötig)

## Karte

```yaml
type: custom:heizplan-card
title: Heizung            # optional
view: overview            # overview | week | templates
room: lounge              # optional: Startraum im Wochenplan
rooms: [lounge, kuechewindow, schlafzimmer]   # optional: Reihenfolge/Auswahl
hide_tabs: false
```

## Dienste

| Dienst | Zweck |
|---|---|
| `heizplan.set_mode` | `plan`, `away`, `off` |
| `heizplan.set_override` | Raum (Entity, Raum-ID oder Name), `temperature` bzw. `on`, Minuten |
| `heizplan.clear_override` | Raum zurück auf den Plan |
| `heizplan.update_room` | Name, Ventil-Sensor, `follow_season`, aktive `months` (z. B. Fußbodenheizung nur Okt–Apr) |
| `heizplan.import_scheduler` | `switch.schedule_*` übernehmen (`climate.set_temperature` und `switch.turn_on/off`; `workday` = Mo–Fr) |

Beispiel „Außer Haus“: `heizplan.set_mode` mit `mode: away`; bei Rückkehr `mode: plan`.

## Wie geschrieben wird

- Jede Minute wird je Raum der Sollwert berechnet: *Override* → *Abwesend* → *Wochenplan*.
- Geschrieben wird nur bei einer Änderung (Blockwechsel, Bearbeitung, Modus) und beim Start –
  eine manuelle Änderung am Thermostat bleibt also bis zum nächsten Blockwechsel bestehen.
- Steht ein Regler auf einer Absenk-Betriebsart (Standby/Nacht), wird er vor dem Schreiben auf Komfort gestellt (abschaltbar). Frostschutz und andere Betriebsarten bleiben für Sonderfälle frei – die Karte zeigt dann einen Hinweis.
- Ausnahme: beim **Start von Home Assistant** (z. B. nach Stromausfall) und beim **Einschalten der Heizperiode** werden alle Regler einmalig auf Komfort gestellt, auch aus Frostschutz.
- Werte werden auf `min_temp`/`max_temp` der climate-Entity begrenzt.

## Entwicklung

```bash
pip install pytest-homeassistant-custom-component
pytest
```

Lizenz: MIT
