# Heizplan für Home Assistant

Wochenpläne für Heizungen mit **echter Solltemperatur**: Die Raumregler bleiben auf
*Komfort*, Heizplan schreibt zu jeder Zeit genau den Wert, der gerade gelten soll.
Was in der Karte als **Soll** steht, wird auch geregelt – kein Rätseln mehr über
Standby-/Nacht-Absenkungen im Regler.

![Übersicht](docs/uebersicht.png)
![Wochenplan](docs/wochenplan.png)

## Funktionen

- **Übersicht aller Räume** – Ist, Soll, Quelle (Plan / Manuell / Abwesend), Ventil %, Tagesverlauf, nächster Wechsel
- **Wochenraster je Raum** – Blöcke antippen zum Ändern, Kanten ziehen zum Verschieben (15-min-Raster), Tag auf andere Tage kopieren
- **Vorlagen** – Plan eines Raums als Vorlage speichern, auf beliebige Räume anwenden
- **Kurzzeit-Überschreiben** – ± in der Übersicht: 2 Stunden wärmer/kühler, danach automatisch zurück zum Plan
- **Modus** Wochenplan / Abwesend (feste Temperatur) / Aus
- **Heizperiode**: optional eine Entity (z. B. `switch.heizperiode`) – ist sie aus, schreibt Heizplan nichts
- Je Raum ein Sensor `sensor.heizplan_<raum>_soll` mit Attributen (Quelle, Block, nächster Wechsel, Ist, Ventil)
- Übernahme bestehender Zeitpläne aus dem *Scheduler-Component* (`heizplan.import_scheduler`)

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
| `heizplan.set_override` | Raum (`climate.*`, Raum-ID oder Name), Temperatur, Minuten |
| `heizplan.clear_override` | Raum zurück auf den Plan |
| `heizplan.import_scheduler` | `switch.schedule_*` übernehmen (nur `climate.set_temperature`-Aktionen; `workday` = Mo–Fr) |

Beispiel „Außer Haus“: `heizplan.set_mode` mit `mode: away`; bei Rückkehr `mode: plan`.

## Wie geschrieben wird

- Jede Minute wird je Raum der Sollwert berechnet: *Override* → *Abwesend* → *Wochenplan*.
- Geschrieben wird nur bei einer Änderung (Blockwechsel, Bearbeitung, Modus) und beim Start –
  eine manuelle Änderung am Thermostat bleibt also bis zum nächsten Blockwechsel bestehen.
- Vor dem Schreiben wird der Regler auf das Komfort-Preset gestellt (abschaltbar).
- Werte werden auf `min_temp`/`max_temp` der climate-Entity begrenzt.

## Entwicklung

```bash
pip install pytest-homeassistant-custom-component
pytest
```

Lizenz: MIT
