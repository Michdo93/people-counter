"""
automation/python/people_counter.py
=====================================
openHAB Python 3 Scripting Rules für den People Counter.

Ablageort: /etc/openhab/automation/python/people_counter.py

Regeln:
  1. PeopleCount_Changed   – Reagiert auf Änderung der Personenzahl
  2. PeopleCounter_Online  – Reagiert auf Online-/Offline-Status
  3. PeopleCount_Threshold – Sendet Benachrichtigung wenn Schwellwert überschritten

Voraussetzungen:
  - openHAB MQTT Binding konfiguriert (people_counter.things)
  - Items angelegt (people_counter.items)
  - openHAB Python 3 Scripting Add-on installiert
"""

from core.rules import rule
from core.triggers import when
from core.log import logging, LOG_PREFIX

log = logging.getLogger("{}.PeopleCounter".format(LOG_PREFIX))

# Schwellwert: ab dieser Personenzahl wird eine Warnung geloggt
THRESHOLD = 5


# ── Regel 1: Personenzahl hat sich geändert ───────────────────────────────────

@rule("PeopleCount Changed")
@when("Item PeopleCount changed")
def people_count_changed(event):
    """
    Wird ausgelöst wenn sich die geglättete Personenzahl ändert.
    Loggt die Änderung und kann weitere Aktionen auslösen
    (z.B. Licht einschalten, Benachrichtigung senden).
    """
    old_count = event.oldItemState
    new_count = event.itemState

    log.info("Person count changed: {} → {}".format(old_count, new_count))

    # Beispiel: Licht einschalten wenn jemand den Raum betritt
    # if int(str(new_count)) > 0 and int(str(old_count)) == 0:
    #     events.sendCommand("RoomLight", "ON")
    #     log.info("Room occupied – light turned on")

    # Beispiel: Licht ausschalten wenn Raum leer
    # if int(str(new_count)) == 0 and int(str(old_count)) > 0:
    #     events.sendCommand("RoomLight", "OFF")
    #     log.info("Room empty – light turned off")


# ── Regel 2: Online-/Offline-Status geändert ─────────────────────────────────

@rule("PeopleCounter Status Changed")
@when("Item PeopleCounter_State changed")
def people_counter_state_changed(event):
    """
    Wird ausgelöst wenn der People Counter online oder offline geht.
    ON  = online  (Kamera läuft)
    OFF = offline (Kamera gestoppt oder Verbindung unterbrochen)
    """
    new_state = str(event.itemState)

    if new_state == "ON":
        log.info("People Counter is ONLINE")
        # Beispiel: Benachrichtigung senden
        # events.sendCommand("NotificationItem", "People Counter is online")
    else:
        log.warning("People Counter is OFFLINE – camera or service may be down")
        # Sicherheitshalber Personenzahl auf 0 setzen wenn offline
        events.postUpdate("PeopleCount", "0")
        log.info("PeopleCount reset to 0 (counter offline)")


# ── Regel 3: Schwellwert-Warnung ─────────────────────────────────────────────

@rule("PeopleCount Threshold Warning")
@when("Item PeopleCount changed")
def people_count_threshold(event):
    """
    Warnung wenn der Raum zu voll wird (Personenzahl ≥ THRESHOLD).
    """
    try:
        count = int(str(event.itemState))
    except (ValueError, TypeError):
        return

    if count >= THRESHOLD:
        log.warning(
            "Room capacity warning: {} persons detected (threshold: {})".format(
                count, THRESHOLD
            )
        )
        # Beispiel: openHAB Notification senden
        # events.sendCommand("NotificationItem",
        #     "Warning: {} persons in room (max {})".format(count, THRESHOLD))


# ── Regel 4: Periodischer Status-Log (alle 5 Minuten) ────────────────────────

@rule("PeopleCount Periodic Log")
@when("Time cron 0 0/5 * * * ?")
def people_count_periodic(event):
    """
    Loggt alle 5 Minuten den aktuellen Stand – nützlich für Monitoring.
    """
    count = items["PeopleCount"]
    state = items["PeopleCounter_State"]

    log.info(
        "People Counter status – Count: {}  Online: {}".format(count, state)
    )
