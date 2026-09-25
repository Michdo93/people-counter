"""
People Counter – Xtion Pro (Linux / Raspberry Pi / Ubuntu)
==========================================================
Unterstützt Temporal Smoothing, GUI-Dashboard und MQTT-Publishing.

Voraussetzungen unter Linux / Pi OS:
  sudo apt update
  sudo apt install -y openni2-utils libopenni2-dev python3-pip
  pip3 install primesense numpy opencv-python paho-mqtt

Starten:
  python3 people_counter_linux.py
"""

import cv2
import numpy as np
import collections
import time
import sys
import os

# Versuche primesense zu laden
try:
    from primesense import openni2
except ImportError:
    print("[FEHLER] 'primesense' nicht installiert. Bitte ausführen:")
    print("         pip3 install primesense")
    sys.exit(1)

# Optionales MQTT
try:
    import paho.mqtt.client as mqtt
    HAS_MQTT = True
except ImportError:
    HAS_MQTT = False

# ─────────────────────────────────────────────
#  Konfiguration
# ─────────────────────────────────────────────

# Kamera-Erfassungsbereich (mm)
DEPTH_MIN_MM         = 800
DEPTH_MAX_MM         = 3500

# Blob-Filter für Kopferkennung
BLOB_AREA_MIN        = 500      # Mindestfläche in Pixeln
BLOB_AREA_MAX        = 6000     # Maximalfläche in Pixeln
BLOB_CIRCULARITY_MIN = 0.40     # Kreisförmigkeit (0.0 bis 1.0)

# Vorverarbeitung
GAUSSIAN_BLUR        = (15, 15)
MORPH_KERNEL         = 7

# Temporal Smoothing (Entprellung/Mittelwert)
SMOOTHING_FRAMES     = 11       # Median über N Frames (1 = kein Smoothing)
HISTORY_SECONDS      = 30

# GUI & Anzeige
SHOW_GUI             = True     # Auf False stellen, falls der Pi ohne Monitor läuft (Headless)
DEMO_MODE            = False    # True = Läuft ohne Kamera mit Testdaten

# MQTT-Konfiguration
USE_MQTT             = False    # Auf True setzen für MQTT-Publishing
MQTT_BROKER          = "localhost"
MQTT_PORT            = 1883
MQTT_TOPIC           = "people_counter/count"

WINDOW_NAME          = "People Counter – Xtion Pro (Linux)"

# Farben (BGR)
C_BG       = (18,  18,  18)
C_PANEL    = (30,  30,  30)
C_ACCENT   = (0,  220, 140)
C_ACCENT2  = (0,  160, 255)
C_WARN     = (0,  100, 255)
C_TEXT_DIM = (100, 100, 100)
C_WHITE    = (255, 255, 255)
C_HEAD     = (0,  255, 120)
C_YELLOW   = (0,  220, 220)


# ─────────────────────────────────────────────
#  OpenNI2 für Linux initialisieren
# ─────────────────────────────────────────────

def init_openni():
    """Initialisiert OpenNI2 unter Linux (greift automatisch auf /usr/lib zu)."""
    try:
        # Unter Linux sucht initialize() selbstständig in den Systempfaden
        openni2.initialize()
        print("[OpenNI2] Initialisiert über Systembibliothek.")
    except Exception as e:
        print(f"[FEHLER] OpenNI2 konnte nicht initialisiert werden: {e}")
        print("         Stelle sicher, dass 'libopenni2-dev' installiert ist.")
        sys.exit(1)

    try:
        dev = openni2.Device.open_any()
        print(f"[OpenNI2] Kamera erfolgreich geöffnet: {dev.get_device_info().name.decode('utf-8')}")
    except Exception as e:
        print(f"[FEHLER] Keine Kamera gefunden oder Zugriff verweigert: {e}")
        print("         Tipp: Überprüfe USB-Kabel (USB 2.0 Port am Pi nutzen) & udev-Regeln.")
        openni2.unload()
        sys.exit(1)

    depth_stream = dev.create_depth_stream()
    depth_stream.set_video_mode(
        openni2.c_api.OniVideoMode(
            pixelFormat=openni2.c_api.OniPixelFormat.ONI_PIXEL_FORMAT_DEPTH_1_MM,
            resolutionX=640, resolutionY=480, fps=30
        )
    )
    depth_stream.start()
    print("[OpenNI2] Tiefenstream gestartet (640x480 @ 30 FPS)")
    return dev, depth_stream


def read_depth_frame(depth_stream):
    frame = depth_stream.read_frame()
    buf   = frame.get_buffer_as_uint16()
    return np.frombuffer(buf, dtype=np.uint16).reshape(480, 640).copy()


def generate_demo_frame(t):
    depth = np.full((480, 640), DEPTH_MAX_MM, dtype=np.uint16)
    n     = max(1, min(2, int(1 + abs(np.sin(t * 0.4)))))
    pos   = [(200, 240), (440, 240)]
    for i in range(n):
        cx = pos[i][0] + int(15 * np.sin(t + i * 1.3))
        cy = pos[i][1] + int(8  * np.cos(t * 0.6 + i))
        cv2.circle(depth, (cx, cy), 38, 1200, -1)
        cv2.circle(depth, (cx, cy), 58, 1500, -1)
    depth[410:, :] = 2300
    return depth


# ─────────────────────────────────────────────
#  Erkennung & Zeichnen
# ─────────────────────────────────────────────

def detect_people_raw(depth_map, area_min, area_max, circularity_min):
    valid    = (depth_map > DEPTH_MIN_MM) & (depth_map < DEPTH_MAX_MM)
    filtered = np.where(valid, depth_map, DEPTH_MAX_MM)

    norm     = cv2.normalize(filtered.astype(np.float32), None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    inv      = 255 - norm

    blur      = cv2.GaussianBlur(inv, GAUSSIAN_BLUR, 0)
    _, binary = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    k        = np.ones((MORPH_KERNEL, MORPH_KERNEL), np.uint8)
    binary   = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, k, iterations=2)
    binary   = cv2.morphologyEx(binary, cv2.MORPH_OPEN,  k, iterations=1)

    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    depth_color = cv2.applyColorMap(norm, cv2.COLORMAP_TURBO)
    heads       = []

    for cnt in contours:
        area = cv2.contourArea(cnt)
        if not (area_min <= area <= area_max):
            continue
        perim = cv2.arcLength(cnt, True)
        if perim == 0 or (4 * np.pi * area) / perim**2 < circularity_min:
            continue

        (cx, cy), r = cv2.minEnclosingCircle(cnt)
        cx, cy, r   = int(cx), int(cy), int(r)
        m           = np.zeros(depth_map.shape, np.uint8)
        cv2.drawContours(m, [cnt], -1, 255, -1)
        d_m         = cv2.mean(depth_map, mask=m)[0] / 1000.0
        heads.append((cx, cy, r, d_m))

        cv2.circle(depth_color, (cx, cy), r + 6, C_HEAD, 2)
        cv2.circle(depth_color, (cx, cy), 4, C_WHITE, -1)
        cv2.putText(depth_color, f"{d_m:.1f}m", (cx - 18, cy - r - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, C_WHITE, 1, cv2.LINE_AA)

    binary_bgr = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
    tinted = binary_bgr.copy()
    tinted[:, :, 0] = (tinted[:, :, 0] * 0.1).astype(np.uint8)
    tinted[:, :, 2] = (tinted[:, :, 2] * 0.2).astype(np.uint8)

    return len(heads), depth_color, tinted, heads


def draw_hud(canvas, smoothed, raw, fps, history, heads, demo, sf):
    W, PANEL = canvas.shape[1], 130
    FM, FD = cv2.FONT_HERSHEY_SIMPLEX, cv2.FONT_HERSHEY_DUPLEX

    cv2.rectangle(canvas, (0, 0), (W, PANEL), C_PANEL, -1)
    cv2.line(canvas, (0, PANEL), (W, PANEL), C_ACCENT, 1)

    col = C_ACCENT if smoothed < 5 else C_WARN
    txt = str(smoothed)
    tw  = cv2.getTextSize(txt, FD, 3.5, 4)[0][0]
    cv2.putText(canvas, txt, (30, 100), FD, 3.5, col, 4, cv2.LINE_AA)
    cv2.putText(canvas, "Personen",    (30 + tw + 14, 80),  FM, 0.52, C_TEXT_DIM, 1)
    cv2.putText(canvas, "(geglaettet)", (30 + tw + 14, 100), FM, 0.42, C_TEXT_DIM, 1)
    cv2.putText(canvas, f"Roh: {raw}", (30 + tw + 14, 120), FM, 0.42, C_YELLOW,   1)

    sx = W - 210
    mode_col = C_WARN if demo else C_ACCENT2
    cv2.putText(canvas, "DEMO" if demo else "LIVE (LINUX)", (sx, 28), FM, 0.55, mode_col, 1)
    cv2.putText(canvas, f"FPS  {fps:4.1f}",         (sx, 50), FM, 0.50, C_TEXT_DIM, 1)
    cv2.putText(canvas, f"Smooth: {sf} Frames",     (sx, 72), FM, 0.44, C_TEXT_DIM, 1)

    gx, gy = 260, 12
    gw, gh = W - 260 - 225, 100
    mx     = max(max(history) if history else 1, 1)
    cv2.rectangle(canvas, (gx, gy), (gx + gw, gy + gh), (40, 40, 40), -1)
    cv2.rectangle(canvas, (gx, gy), (gx + gw, gy + gh), (70, 70, 70), 1)
    n = len(history)
    if n > 1:
        pts = [(gx + int(i/(n-1)*gw), gy + gh - int(v/mx*(gh-4)) - 2) for i, v in enumerate(history)]
        for i in range(len(pts)-1):
            cv2.line(canvas, pts[i], pts[i+1], C_ACCENT, 2)
        cv2.circle(canvas, pts[-1], 4, C_WHITE, -1)


# ─────────────────────────────────────────────
#  Hauptschleife
# ─────────────────────────────────────────────

def main():
    global DEMO_MODE, SMOOTHING_FRAMES

    print("=" * 60)
    print("  People Counter – Xtion Pro (Linux / Pi OS / Ubuntu)")
    print("=" * 60)

    dev = depth_stream = None
    if not DEMO_MODE:
        dev, depth_stream = init_openni()

    # MQTT Client optional initialisieren
    mqtt_client = None
    if USE_MQTT and HAS_MQTT:
        try:
            mqtt_client = mqtt.Client()
            mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
            mqtt_client.loop_start()
            print(f"[MQTT] Verbunden mit {MQTT_BROKER}")
        except Exception as e:
            print(f"[MQTT] Fehler beim Verbinden: {e}")

    raw_ring  = collections.deque(maxlen=SMOOTHING_FRAMES)
    history   = collections.deque(maxlen=HISTORY_SECONDS)
    last_pub  = -1
    last_hist = time.time()
    t_start   = time.time()
    fps_ring  = collections.deque(maxlen=20)

    if SHOW_GUI:
        cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(WINDOW_NAME, 1280, 650)

    try:
        while True:
            t0 = time.time()

            if DEMO_MODE:
                depth_map = generate_demo_frame(time.time() - t_start)
                time.sleep(0.030)
            else:
                depth_map = read_depth_frame(depth_stream)

            raw_count, depth_view, mask_view, heads = detect_people_raw(
                depth_map, BLOB_AREA_MIN, BLOB_AREA_MAX, BLOB_CIRCULARITY_MIN
            )

            raw_ring.append(raw_count)
            smoothed = int(np.median(list(raw_ring)))

            # Wenn sich geglättete Personenzahl ändert
            if smoothed != last_pub:
                print(f"[INFO] Personenzahl geändert: {last_pub if last_pub >= 0 else '?'} -> {smoothed} (Rohwert: {raw_count})")
                last_pub = smoothed
                if mqtt_client:
                    mqtt_client.publish(MQTT_TOPIC, str(smoothed), qos=1, retain=True)

            now = time.time()
            if now - last_hist >= 1.0:
                history.append(smoothed)
                last_hist = now

            fps_ring.append(time.time() - t0)
            fps = 1.0 / (sum(fps_ring)/len(fps_ring)) if fps_ring else 0

            # GUI rendern (falls aktiviert)
            if SHOW_GUI:
                H, W, HUD_H = 480, 640, 130
                d_rs = cv2.resize(depth_view, (W, H))
                m_rs = cv2.resize(mask_view,  (W, H))
                row  = np.hstack([d_rs, m_rs])
                FW   = row.shape[1]
                hud  = np.full((HUD_H, FW, 3), C_BG, dtype=np.uint8)

                draw_hud(hud, smoothed, raw_count, fps, history, heads, DEMO_MODE, len(raw_ring))
                canvas = np.vstack([hud, row])
                cv2.imshow(WINDOW_NAME, canvas)

                key = cv2.waitKey(1) & 0xFF
                if key in (ord('q'), 27):
                    break

    except KeyboardInterrupt:
        print("\n[INFO] Durch Benutzer abgebrochen.")
    finally:
        if SHOW_GUI:
            cv2.destroyAllWindows()
        if depth_stream:
            depth_stream.stop()
        if dev:
            dev.close()
        openni2.unload()
        if mqtt_client:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
        print("[INFO] Kamera-Ressourcen erfolgreich freigegeben.")

if __name__ == "__main__":
    main()
