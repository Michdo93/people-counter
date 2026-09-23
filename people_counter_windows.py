"""
People Counter – Xtion Pro auf Windows
=======================================
Nutzt die offizielle primesense Python-Bibliothek direkt (kein OpenCV+OpenNI-Build nötig).
Anzeige komplett in OpenCV.

Voraussetzungen:
  1. OpenNI2 installiert (C:\\Program Files\\OpenNI2)
  2. Asus Xtion Treiber + Firmware-Patch installiert (siehe Installationsartikel)
  3. NiViewer.exe funktioniert und zeigt ein Tiefenbild

Pakete installieren:
  pip install primesense numpy opencv-python

Umgebungsvariable setzen (in PowerShell, vor dem Start):
  $env:OPENNI2_DRIVERS_PATH64 = "C:\\Program Files\\OpenNI2\\Redist\\OpenNI2\\Drivers"

Starten:
  python people_counter_windows.py

Tasten im Fenster:
  q / ESC  = Beenden
  s        = Screenshot speichern
  d        = Demo-Modus umschalten (ohne Kamera testen)
"""

import cv2
import numpy as np
import collections
import time
import sys
import os

# ─────────────────────────────────────────────
#  Konfiguration
# ─────────────────────────────────────────────

# Pfad zur OpenNI2-Redistribution (wird an primesense übergeben)
OPENNI2_REDIST = r"C:\Program Files\OpenNI2\Redist"

# Tiefenkamera: Erfassungsbereich (mm)
DEPTH_MIN_MM         = 800
DEPTH_MAX_MM         = 3500

# Blob-Filter für Kopferkennung
BLOB_AREA_MIN        = 400
BLOB_AREA_MAX        = 8000
BLOB_CIRCULARITY_MIN = 0.35

# Vorverarbeitung
GAUSSIAN_BLUR        = (11, 11)
MORPH_KERNEL         = 5

# Verlaufsdiagramm
HISTORY_SECONDS      = 30

# Demo-Modus (True = läuft ohne Kamera)
DEMO_MODE            = False

WINDOW_NAME = "People Counter – Xtion Pro"

# ─────────────────────────────────────────────
#  Farben (BGR)
# ─────────────────────────────────────────────
C_BG       = (18,  18,  18)
C_PANEL    = (30,  30,  30)
C_ACCENT   = (0,  220, 140)
C_ACCENT2  = (0,  160, 255)
C_WARN     = (0,  100, 255)
C_TEXT     = (220, 220, 220)
C_TEXT_DIM = (100, 100, 100)
C_WHITE    = (255, 255, 255)
C_HEAD     = (0,  255, 120)

# ─────────────────────────────────────────────
#  primesense laden
# ─────────────────────────────────────────────

def init_openni():
    """
    Initialisiert OpenNI2 über die primesense-Bindings.
    Gibt (device, depth_stream) zurück oder beendet das Programm.
    """
    try:
        from primesense import openni2
    except ImportError:
        print("[FEHLER] primesense nicht installiert.")
        print("         Bitte: pip install primesense")
        sys.exit(1)

    # Prüfe ob Redist-Ordner existiert
    if not os.path.isdir(OPENNI2_REDIST):
        print(f"[FEHLER] OpenNI2-Redist nicht gefunden: {OPENNI2_REDIST}")
        print("         Bitte OPENNI2_REDIST im Skript anpassen.")
        sys.exit(1)

    try:
        openni2.initialize(OPENNI2_REDIST)
        print(f"[OpenNI2] Initialisiert aus: {OPENNI2_REDIST}")
    except Exception as e:
        print(f"[FEHLER] OpenNI2-Initialisierung fehlgeschlagen: {e}")
        print("         Tipp: Ist der Treiber installiert? Läuft NiViewer.exe?")
        sys.exit(1)

    try:
        dev = openni2.Device.open_any()
        print(f"[OpenNI2] Kamera geöffnet: {dev.get_device_info()}")
    except Exception as e:
        print(f"[FEHLER] Kamera konnte nicht geöffnet werden: {e}")
        openni2.unload()
        sys.exit(1)

    depth_stream = dev.create_depth_stream()
    depth_stream.set_video_mode(
        openni2.c_api.OniVideoMode(
            pixelFormat=openni2.c_api.OniPixelFormat.ONI_PIXEL_FORMAT_DEPTH_1_MM,
            resolutionX=640,
            resolutionY=480,
            fps=30
        )
    )
    depth_stream.start()
    print("[OpenNI2] Tiefenstream gestartet (640×480, 30 FPS)")
    return openni2, dev, depth_stream


def read_depth_frame(depth_stream):
    """Liest ein Tiefenbild als numpy uint16-Array (480×640)."""
    frame      = depth_stream.read_frame()
    buf        = frame.get_buffer_as_uint16()
    depth      = np.frombuffer(buf, dtype=np.uint16).reshape(480, 640).copy()
    return depth


# ─────────────────────────────────────────────
#  Demo-Datengenerator
# ─────────────────────────────────────────────

def generate_demo_frame(t):
    depth = np.full((480, 640), DEPTH_MAX_MM, dtype=np.uint16)
    n     = max(1, min(3, int(1 + (np.sin(t * 0.3) + 1) * 1.5)))
    pos   = [(160, 200), (320, 240), (480, 200)]
    for i in range(n):
        cx = pos[i][0] + int(20 * np.sin(t + i))
        cy = pos[i][1] + int(10 * np.cos(t * 0.7 + i))
        cv2.circle(depth, (cx, cy), 35, 1100, -1)
        cv2.circle(depth, (cx, cy), 55, 1400, -1)
    depth[400:, :] = 2400
    return depth


# ─────────────────────────────────────────────
#  Personenerkennung
# ─────────────────────────────────────────────

def detect_people(depth_map):
    valid          = (depth_map > DEPTH_MIN_MM) & (depth_map < DEPTH_MAX_MM)
    filtered       = np.where(valid, depth_map, DEPTH_MAX_MM)

    norm           = cv2.normalize(
        filtered.astype(np.float32), None, 0, 255, cv2.NORM_MINMAX
    ).astype(np.uint8)
    inv            = 255 - norm

    blur           = cv2.GaussianBlur(inv, GAUSSIAN_BLUR, 0)
    _, binary      = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    k              = np.ones((MORPH_KERNEL, MORPH_KERNEL), np.uint8)
    binary         = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, k, iterations=2)
    binary         = cv2.morphologyEx(binary, cv2.MORPH_OPEN,  k, iterations=1)

    contours, _    = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    depth_color    = cv2.applyColorMap(norm, cv2.COLORMAP_TURBO)
    heads          = []

    for cnt in contours:
        area = cv2.contourArea(cnt)
        if not (BLOB_AREA_MIN <= area <= BLOB_AREA_MAX):
            continue
        perim = cv2.arcLength(cnt, True)
        if perim == 0 or (4 * np.pi * area) / perim**2 < BLOB_CIRCULARITY_MIN:
            continue

        (cx, cy), r = cv2.minEnclosingCircle(cnt)
        cx, cy, r   = int(cx), int(cy), int(r)

        m           = np.zeros(depth_map.shape, np.uint8)
        cv2.drawContours(m, [cnt], -1, 255, -1)
        d_m         = cv2.mean(depth_map, mask=m)[0] / 1000.0

        heads.append((cx, cy, r, d_m))
        cv2.circle(depth_color, (cx, cy), r + 6, C_HEAD, 2)
        cv2.circle(depth_color, (cx, cy), 4, C_WHITE, -1)
        cv2.putText(depth_color, f"{d_m:.1f}m",
                    (cx - 18, cy - r - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, C_WHITE, 1, cv2.LINE_AA)

    binary_bgr = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
    # Grüntönung der Maske
    tinted = binary_bgr.copy()
    tinted[:, :, 0] = (tinted[:, :, 0] * 0.1).astype(np.uint8)
    tinted[:, :, 2] = (tinted[:, :, 2] * 0.2).astype(np.uint8)

    return len(heads), depth_color, tinted, heads


# ─────────────────────────────────────────────
#  HUD
# ─────────────────────────────────────────────

def draw_hud(canvas, count, fps, history, heads, demo):
    W      = canvas.shape[1]
    PANEL  = 110
    F_MONO = cv2.FONT_HERSHEY_SIMPLEX
    F_SANS = cv2.FONT_HERSHEY_DUPLEX

    cv2.rectangle(canvas, (0, 0), (W, PANEL), C_PANEL, -1)
    cv2.line(canvas, (0, PANEL), (W, PANEL), C_ACCENT, 1)

    # Zähler
    col = C_ACCENT if count < 5 else C_WARN
    txt = str(count)
    tw, _ = cv2.getTextSize(txt, F_SANS, 3.2, 4)[0]
    cv2.putText(canvas, txt, (30, 85), F_SANS, 3.2, col, 4, cv2.LINE_AA)
    cv2.putText(canvas, "Personen erkannt",
                (30 + tw + 14, 72), F_MONO, 0.52, C_TEXT_DIM, 1, cv2.LINE_AA)

    # Status rechts
    sx = W - 170
    cv2.putText(canvas, f"FPS  {fps:4.1f}", (sx, 32), F_MONO, 0.5, C_TEXT_DIM, 1, cv2.LINE_AA)
    mode   = "DEMO" if demo else "LIVE"
    mcol   = C_WARN if demo else C_ACCENT2
    cv2.putText(canvas, mode, (sx, 58), F_MONO, 0.58, mcol, 1, cv2.LINE_AA)
    cv2.putText(canvas, f"Blobs: {len(heads)}", (sx, 82), F_MONO, 0.44, C_TEXT_DIM, 1, cv2.LINE_AA)

    # Verlauf
    gx, gy = 230, 14
    gw, gh = W - 230 - 185, 82
    mx     = max(max(history) if history else 1, 1)
    cv2.rectangle(canvas, (gx, gy), (gx + gw, gy + gh), (45, 45, 45), -1)
    cv2.rectangle(canvas, (gx, gy), (gx + gw, gy + gh), (70, 70, 70), 1)
    n = len(history)
    if n > 1:
        pts = [(gx + int(i/(n-1)*gw), gy + gh - int(v/mx*(gh-4)) - 2)
               for i, v in enumerate(history)]
        for i in range(len(pts)-1):
            cv2.line(canvas, pts[i], pts[i+1], C_ACCENT, 2)
        cv2.circle(canvas, pts[-1], 4, C_WHITE, -1)
    cv2.putText(canvas, str(mx), (gx-28, gy+10), F_MONO, 0.38, C_TEXT_DIM, 1)
    cv2.putText(canvas, "0",     (gx-14, gy+gh), F_MONO, 0.38, C_TEXT_DIM, 1)
    cv2.putText(canvas, f"letzte {HISTORY_SECONDS}s",
                (gx, gy + gh + 14), F_MONO, 0.38, C_TEXT_DIM, 1)


# ─────────────────────────────────────────────
#  Hauptschleife
# ─────────────────────────────────────────────

def main():
    global DEMO_MODE

    print("=" * 58)
    print("  People Counter – Xtion Pro (Windows / primesense)")
    print("=" * 58)
    print("  q / ESC  = Beenden")
    print("  d        = Demo-Modus umschalten")
    print("  s        = Screenshot speichern")
    print("=" * 58)

    openni2_mod = dev = depth_stream = None

    if not DEMO_MODE:
        openni2_mod, dev, depth_stream = init_openni()

    history    = collections.deque(maxlen=HISTORY_SECONDS)
    last_count = -1
    last_hist  = time.time()
    t_start    = time.time()
    fps_ring   = collections.deque(maxlen=20)

    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WINDOW_NAME, 1280, 620)

    try:
        while True:
            t0 = time.time()

            if DEMO_MODE:
                depth_map = generate_demo_frame(time.time() - t_start)
                time.sleep(0.033)
            else:
                depth_map = read_depth_frame(depth_stream)

            count, depth_view, mask_view, heads = detect_people(depth_map)

            if count != last_count:
                print(f"[INFO] Personen: {last_count if last_count >= 0 else '?'} → {count}")
                last_count = count

            now = time.time()
            if now - last_hist >= 1.0:
                history.append(count)
                last_hist = now

            fps_ring.append(time.time() - t0)
            fps = 1.0 / (sum(fps_ring)/len(fps_ring)) if fps_ring else 0

            # Layout
            H, W     = 480, 640
            HUD_H    = 110
            depth_rs = cv2.resize(depth_view, (W, H))
            mask_rs  = cv2.resize(mask_view,  (W, H))

            cv2.putText(depth_rs, "Tiefenbild (Falschfarben)",
                        (10, H - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.44, C_TEXT_DIM, 1)
            cv2.putText(mask_rs, "Kopf-Maske (Erkennungsbereiche)",
                        (10, H - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.44, C_TEXT_DIM, 1)

            row    = np.hstack([depth_rs, mask_rs])
            FW     = row.shape[1]
            hud    = np.full((HUD_H, FW, 3), C_BG, dtype=np.uint8)
            draw_hud(hud, count, fps, history, heads, DEMO_MODE)
            canvas = np.vstack([hud, row])
            cv2.line(canvas, (FW//2, HUD_H), (FW//2, canvas.shape[0]), (60, 60, 60), 1)

            cv2.imshow(WINDOW_NAME, canvas)

            key = cv2.waitKey(1) & 0xFF
            if key in (ord('q'), 27):
                break
            elif key == ord('d'):
                DEMO_MODE = not DEMO_MODE
                if not DEMO_MODE and depth_stream is None:
                    openni2_mod, dev, depth_stream = init_openni()
                print(f"[INFO] Demo-Modus: {'AN' if DEMO_MODE else 'AUS'}")
            elif key == ord('s'):
                fn = f"screenshot_{int(time.time())}.png"
                cv2.imwrite(fn, canvas)
                print(f"[INFO] Gespeichert: {fn}")

    except KeyboardInterrupt:
        print("\n[INFO] Abgebrochen.")
    finally:
        cv2.destroyAllWindows()
        if depth_stream:
            depth_stream.stop()
        if dev:
            dev.close()
        if openni2_mod:
            openni2_mod.unload()
        print("[INFO] Beendet.")


if __name__ == "__main__":
    main()
