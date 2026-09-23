"""
People Counter mit ASUS Xtion Pro (OpenNI) + OpenCV + MQTT
============================================================
Erkennt Personen anhand des Tiefenbildes (Kopf-Erkennung von oben)
und publisht die aktuelle Anzahl per MQTT, sobald sie sich ändert.

Abhängigkeiten installieren:
    pip install opencv-python primesense paho-mqtt numpy

Kamera muss von oben (2–2,5 m Höhe) schräg nach unten zeigen.
"""

import cv2
import numpy as np
import paho.mqtt.client as mqtt
import time
import sys

# ─────────────────────────────────────────────
#  Konfiguration
# ─────────────────────────────────────────────

# MQTT
MQTT_BROKER   = "localhost"       # IP/Hostname des Brokers
MQTT_PORT     = 1883
MQTT_TOPIC    = "people_counter/count"
MQTT_CLIENT_ID = "xtion_people_counter"

# Tiefenkamera: Erfassungsbereich (in mm)
DEPTH_MIN_MM  = 800               # Mindestabstand (Xtion-Limit)
DEPTH_MAX_MM  = 3500              # Maximalabstand

# Kopferkennung: Blob-Filter
# Alle Werte beziehen sich auf das normalisierte Tiefenbild (0–255)
BLOB_AREA_MIN   = 400             # Mindestfläche in Pixeln (zu klein = Rauschen)
BLOB_AREA_MAX   = 8000            # Maximalfläche (zu groß = Wand/Boden)
BLOB_CIRCULARITY_MIN = 0.35       # Kreisförmigkeit (Kopf von oben ≈ rund)

# Vorverarbeitung
GAUSSIAN_BLUR   = (11, 11)        # Glättung des Tiefenbildes
MORPH_KERNEL    = 5               # Morphologie-Kernel (Rauschen entfernen)

# Anzeige
SHOW_PREVIEW    = True            # Vorschaufenster anzeigen (False für Headless)

# ─────────────────────────────────────────────
#  MQTT-Client einrichten
# ─────────────────────────────────────────────

def create_mqtt_client():
    client = mqtt.Client(client_id=MQTT_CLIENT_ID)

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            print(f"[MQTT] Verbunden mit Broker {MQTT_BROKER}:{MQTT_PORT}")
        else:
            print(f"[MQTT] Verbindung fehlgeschlagen, Code: {rc}")

    def on_disconnect(client, userdata, rc):
        print(f"[MQTT] Getrennt (Code {rc}). Verbinde neu …")

    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect

    try:
        client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        client.loop_start()
    except Exception as e:
        print(f"[MQTT] Fehler beim Verbinden: {e}")
        print("[MQTT] Läuft ohne MQTT-Publishing weiter …")

    return client


def publish_count(client, count):
    """Sendet die Personenzahl als MQTT-Nachricht."""
    try:
        payload = str(count)
        result  = client.publish(MQTT_TOPIC, payload, qos=1, retain=True)
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            print(f"[MQTT] Publiziert → {MQTT_TOPIC}: {payload}")
        else:
            print(f"[MQTT] Publish fehlgeschlagen (rc={result.rc})")
    except Exception as e:
        print(f"[MQTT] Fehler beim Publishen: {e}")


# ─────────────────────────────────────────────
#  Tiefenkamera öffnen
# ─────────────────────────────────────────────

def open_depth_camera():
    """
    Öffnet die OpenNI-Kamera (Xtion Pro) über OpenCV.
    OpenCV muss mit OpenNI-Support kompiliert sein.
    """
    # CAP_OPENNI2 = 1200, CAP_OPENNI = 900
    for backend in [cv2.CAP_OPENNI2, cv2.CAP_OPENNI]:
        cap = cv2.VideoCapture(backend)
        if cap.isOpened():
            print(f"[Kamera] Geöffnet mit Backend {backend}")
            cap.set(cv2.CAP_PROP_OPENNI_REGISTRATION, 1)
            return cap

    print("[Kamera] FEHLER: Keine OpenNI-Kamera gefunden!")
    print("         Stelle sicher, dass:")
    print("         1. OpenNI 2 installiert ist")
    print("         2. OpenCV mit OpenNI gebaut wurde")
    print("         3. Die Kamera an USB angeschlossen ist")
    sys.exit(1)


# ─────────────────────────────────────────────
#  Personenerkennung im Tiefenbild
# ─────────────────────────────────────────────

def detect_people(depth_map):
    """
    Erkennt Personen anhand ihres Kopfes im Tiefenbild.
    
    Funktionsprinzip (Kamera von oben):
    - Köpfe sind die dem Sensor nächsten zusammenhängenden Flächen
    - Sie erscheinen im Tiefenbild als helle, runde Flecken (Blobs)
    - Boden und Wände liegen weiter weg → dunkel im Bild
    
    Gibt zurück: (Anzahl, annotiertes BGR-Bild)
    """

    # 1. Ungültige Tiefenwerte maskieren (0 = kein Signal)
    valid_mask = (depth_map > DEPTH_MIN_MM) & (depth_map < DEPTH_MAX_MM)
    depth_filtered = np.where(valid_mask, depth_map, DEPTH_MAX_MM)

    # 2. Auf 8-Bit normalisieren: nah = hell, weit = dunkel
    #    Invertierung: nächste Objekte (Köpfe) werden weiß
    depth_norm = cv2.normalize(
        depth_filtered.astype(np.float32),
        None, 0, 255, cv2.NORM_MINMAX
    ).astype(np.uint8)
    depth_inverted = 255 - depth_norm

    # 3. Rauschen glätten
    blurred = cv2.GaussianBlur(depth_inverted, GAUSSIAN_BLUR, 0)

    # 4. Binarisierung: nur die nächsten Objekte (Köpfe) behalten
    #    Otsu-Schwellwert passt sich automatisch an die Szene an
    _, binary = cv2.threshold(
        blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )

    # 5. Morphologie: kleine Lücken schließen, Rauschen entfernen
    kernel = np.ones((MORPH_KERNEL, MORPH_KERNEL), np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=2)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN,  kernel, iterations=1)

    # 6. Konturen finden und als Köpfe filtern
    contours, _ = cv2.findContours(
        binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    # Vorschaubild in Farbe (Falschfarben des Tiefenbildes)
    preview = cv2.applyColorMap(depth_norm, cv2.COLORMAP_JET)

    head_count = 0

    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < BLOB_AREA_MIN or area > BLOB_AREA_MAX:
            continue

        # Kreisförmigkeit prüfen: 4π·Fläche / Umfang²
        perimeter = cv2.arcLength(cnt, True)
        if perimeter == 0:
            continue
        circularity = (4 * np.pi * area) / (perimeter ** 2)
        if circularity < BLOB_CIRCULARITY_MIN:
            continue

        # Gültiger Kopf gefunden
        head_count += 1
        x, y, w, h = cv2.boundingRect(cnt)
        cx, cy = x + w // 2, y + h // 2

        # Mittlere Tiefe des Kopfes berechnen
        mask_single = np.zeros(depth_map.shape, np.uint8)
        cv2.drawContours(mask_single, [cnt], -1, 255, -1)
        mean_depth = cv2.mean(depth_map, mask=mask_single)[0]

        # Annotation im Vorschaubild
        cv2.drawContours(preview, [cnt], -1, (0, 255, 0), 2)
        cv2.circle(preview, (cx, cy), 5, (255, 255, 255), -1)
        cv2.putText(
            preview,
            f"{mean_depth/1000:.1f}m",
            (x, y - 8),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1
        )

    # Zähler ins Bild schreiben
    cv2.putText(
        preview,
        f"Personen: {head_count}",
        (10, 30),
        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 2
    )

    return head_count, preview


# ─────────────────────────────────────────────
#  Hauptschleife
# ─────────────────────────────────────────────

def main():
    print("=" * 50)
    print(" People Counter – Xtion Pro + OpenCV + MQTT")
    print("=" * 50)

    mqtt_client = create_mqtt_client()
    cap         = open_depth_camera()

    last_count    = -1          # Startwert: noch nie publiziert
    frame_skip    = 0           # Frames überspringen (Performance)
    SKIP_FRAMES   = 2           # Jedes n-te Frame verarbeiten

    print("[INFO] Starte Erkennung … (q zum Beenden)")

    try:
        while True:
            # Tiefenbild abrufen
            cap.grab()

            frame_skip += 1
            if frame_skip % SKIP_FRAMES != 0:
                continue

            ret, depth_map = cap.retrieve(
                flag=cv2.CAP_OPENNI_DEPTH_MAP
            )
            if not ret or depth_map is None:
                print("[WARN] Kein Tiefenbild erhalten, überspringe …")
                time.sleep(0.05)
                continue

            # Personen erkennen
            count, preview = detect_people(depth_map)

            # MQTT nur bei Änderung publishen
            if count != last_count:
                print(f"[INFO] Personenzahl geändert: {last_count} → {count}")
                publish_count(mqtt_client, count)
                last_count = count

            # Vorschaufenster
            if SHOW_PREVIEW:
                cv2.imshow("People Counter – Tiefenbild", preview)
                key = cv2.waitKey(1) & 0xFF
                if key == ord('q'):
                    print("[INFO] Beende …")
                    break

    except KeyboardInterrupt:
        print("\n[INFO] Abgebrochen (Ctrl+C)")

    finally:
        cap.release()
        cv2.destroyAllWindows()
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        print("[INFO] Ressourcen freigegeben.")


if __name__ == "__main__":
    main()
