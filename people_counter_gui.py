"""
People Counter – Xtion Pro mit vollständiger OpenCV-Anzeige
============================================================
Zeigt in EINEM Fenster nebeneinander:
  Links:  Tiefenbild (Falschfarben) mit erkannten Köpfen (grüne Kreise)
  Rechts: Rohe Binärmaske (weiß = erkannter Kopfbereich)

Darüber ein großes HUD mit Personenzähler, FPS und Verlaufsdiagramm.

Abhängigkeiten:
    pip install opencv-python numpy

Keine MQTT, keine externen Dienste – läuft vollständig lokal.
Beenden: Taste  q  oder  ESC
"""

import cv2
import numpy as np
import collections
import time
import sys

# ─────────────────────────────────────────────
#  Konfiguration
# ─────────────────────────────────────────────

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

# Verlaufsdiagramm: wie viele Sekunden anzeigen
HISTORY_SECONDS      = 30

# Fenster
WINDOW_NAME          = "People Counter – Xtion Pro"

# Demo-Modus: läuft ohne Kamera mit simulierten Daten (zum Testen der GUI)
DEMO_MODE            = False   # auf True setzen wenn keine Kamera angeschlossen

# ─────────────────────────────────────────────
#  Farben (BGR)
# ─────────────────────────────────────────────
C_BG         = (18,  18,  18)      # fast schwarz
C_PANEL      = (30,  30,  30)      # dunkelgrau
C_ACCENT     = (0,   220, 140)     # Mint-Grün
C_ACCENT2    = (0,   160, 255)     # Cyan-Blau
C_WARN       = (0,   100, 255)     # Orange (bei voll)
C_TEXT       = (220, 220, 220)     # hellgrau
C_TEXT_DIM   = (100, 100, 100)     # gedimmt
C_WHITE      = (255, 255, 255)
C_HEAD_RING  = (0,   255, 120)     # Kopfring

# ─────────────────────────────────────────────
#  Hilfsfunktionen Zeichnen
# ─────────────────────────────────────────────

def draw_rounded_rect(img, pt1, pt2, color, radius=12, thickness=-1):
    """Füllt oder zeichnet ein abgerundetes Rechteck."""
    x1, y1 = pt1
    x2, y2 = pt2
    r = radius
    if thickness == -1:
        cv2.rectangle(img, (x1+r, y1), (x2-r, y2), color, -1)
        cv2.rectangle(img, (x1, y1+r), (x2, y2-r), color, -1)
        for cx, cy in [(x1+r, y1+r), (x2-r, y1+r), (x1+r, y2-r), (x2-r, y2-r)]:
            cv2.circle(img, (cx, cy), r, color, -1)
    else:
        cv2.rectangle(img, (x1+r, y1), (x2-r, y1), color, thickness)
        cv2.rectangle(img, (x1+r, y2), (x2-r, y2), color, thickness)
        cv2.rectangle(img, (x1, y1+r), (x1, y2-r), color, thickness)
        cv2.rectangle(img, (x2, y1+r), (x2, y2-r), color, thickness)


def put_text_centered(img, text, cx, cy, font, scale, color, thickness=1):
    (w, h), _ = cv2.getTextSize(text, font, scale, thickness)
    cv2.putText(img, text, (cx - w//2, cy + h//2), font, scale, color, thickness, cv2.LINE_AA)


def put_text(img, text, x, y, font, scale, color, thickness=1):
    cv2.putText(img, text, (x, y), font, scale, color, thickness, cv2.LINE_AA)


# ─────────────────────────────────────────────
#  Kamera
# ─────────────────────────────────────────────

def open_camera():
    for backend in [cv2.CAP_OPENNI2, cv2.CAP_OPENNI]:
        cap = cv2.VideoCapture(backend)
        if cap.isOpened():
            print(f"[Kamera] Geöffnet (Backend {backend})")
            return cap
    print("[Kamera] FEHLER: Keine OpenNI-Kamera gefunden.")
    print("         Starte mit DEMO_MODE=True zum Testen ohne Kamera.")
    sys.exit(1)


# ─────────────────────────────────────────────
#  Demo-Datengenerator (ohne echte Kamera)
# ─────────────────────────────────────────────

def generate_demo_frame(t):
    """Erzeugt ein synthetisches Tiefenbild mit 1–3 simulierten Köpfen."""
    depth = np.full((480, 640), DEPTH_MAX_MM, dtype=np.uint16)
    n_heads = int(1 + (np.sin(t * 0.3) + 1) * 1.5)   # 1–4 simuliert
    n_heads = min(n_heads, 3)
    positions = [(160, 200), (320, 240), (480, 200)]
    for i in range(n_heads):
        cx, cy = positions[i]
        cx += int(20 * np.sin(t + i))
        cy += int(10 * np.cos(t * 0.7 + i))
        cv2.circle(depth, (cx, cy), 35, 1100, -1)        # Kopf ≈ 1,1 m
        cv2.circle(depth, (cx, cy), 55, 1300, -1)        # Schultern
    # Boden
    depth[380:, :] = 2200
    return depth


# ─────────────────────────────────────────────
#  Personenerkennung
# ─────────────────────────────────────────────

def detect_people(depth_map):
    """
    Gibt zurück:
      count    – Anzahl erkannter Personen
      preview  – BGR-Tiefenbild (Falschfarben) mit Annotationen
      binary   – Binärmaske (weiß = Kopfbereich)
      heads    – Liste von (cx, cy, radius, tiefe_m)
    """
    valid_mask     = (depth_map > DEPTH_MIN_MM) & (depth_map < DEPTH_MAX_MM)
    depth_filtered = np.where(valid_mask, depth_map, DEPTH_MAX_MM)

    depth_norm     = cv2.normalize(
        depth_filtered.astype(np.float32), None, 0, 255, cv2.NORM_MINMAX
    ).astype(np.uint8)
    depth_inv      = 255 - depth_norm

    blurred        = cv2.GaussianBlur(depth_inv, GAUSSIAN_BLUR, 0)
    _, binary      = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    kernel         = np.ones((MORPH_KERNEL, MORPH_KERNEL), np.uint8)
    binary         = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=2)
    binary         = cv2.morphologyEx(binary, cv2.MORPH_OPEN,  kernel, iterations=1)

    contours, _    = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    preview        = cv2.applyColorMap(depth_norm, cv2.COLORMAP_TURBO)
    heads          = []

    for cnt in contours:
        area = cv2.contourArea(cnt)
        if not (BLOB_AREA_MIN <= area <= BLOB_AREA_MAX):
            continue
        perimeter = cv2.arcLength(cnt, True)
        if perimeter == 0:
            continue
        if (4 * np.pi * area) / perimeter**2 < BLOB_CIRCULARITY_MIN:
            continue

        (cx, cy), radius = cv2.minEnclosingCircle(cnt)
        cx, cy, radius   = int(cx), int(cy), int(radius)

        mask_s = np.zeros(depth_map.shape, np.uint8)
        cv2.drawContours(mask_s, [cnt], -1, 255, -1)
        mean_d = cv2.mean(depth_map, mask=mask_s)[0] / 1000.0

        heads.append((cx, cy, radius, mean_d))

        # Annotierung im Tiefenbild
        cv2.circle(preview, (cx, cy), radius + 6, C_HEAD_RING, 2)
        cv2.circle(preview, (cx, cy), 4, C_WHITE, -1)
        label = f"{mean_d:.1f}m"
        put_text(preview, label, cx - 20, cy - radius - 10,
                 cv2.FONT_HERSHEY_SIMPLEX, 0.45, C_WHITE, 1)

    binary_bgr = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
    return len(heads), preview, binary_bgr, heads


# ─────────────────────────────────────────────
#  HUD zeichnen
# ─────────────────────────────────────────────

def draw_hud(canvas, count, fps, history, heads):
    """
    Zeichnet das Info-Panel oben auf das Canvas:
    - Große Personenzahl links
    - FPS und Status rechts
    - Verlaufsdiagramm in der Mitte
    """
    H, W = canvas.shape[:2]
    PANEL_H = 110
    FONT_MONO = cv2.FONT_HERSHEY_SIMPLEX
    FONT_SANS = cv2.FONT_HERSHEY_DUPLEX

    # Panel-Hintergrund
    cv2.rectangle(canvas, (0, 0), (W, PANEL_H), C_PANEL, -1)
    cv2.line(canvas, (0, PANEL_H), (W, PANEL_H), C_ACCENT, 1)

    # ── Personenzähler links ──
    count_color = C_ACCENT if count < 5 else C_WARN
    big_label   = str(count)
    (tw, th), _ = cv2.getTextSize(big_label, FONT_SANS, 3.2, 4)
    cv2.putText(canvas, big_label, (30, 85), FONT_SANS, 3.2, count_color, 4, cv2.LINE_AA)
    cv2.putText(canvas, "Personen", (30 + tw + 12, 75),
                FONT_MONO, 0.55, C_TEXT_DIM, 1, cv2.LINE_AA)
    cv2.putText(canvas, "erkannt", (30 + tw + 12, 95),
                FONT_MONO, 0.55, C_TEXT_DIM, 1, cv2.LINE_AA)

    # ── FPS + Status rechts ──
    status_x = W - 160
    cv2.putText(canvas, f"FPS  {fps:4.1f}", (status_x, 35),
                FONT_MONO, 0.5, C_TEXT_DIM, 1, cv2.LINE_AA)
    mode_str = "DEMO" if DEMO_MODE else "LIVE"
    mode_col = C_ACCENT2 if not DEMO_MODE else C_WARN
    cv2.putText(canvas, mode_str, (status_x, 60),
                FONT_MONO, 0.55, mode_col, 1, cv2.LINE_AA)
    cv2.putText(canvas, f"Koepfe: {len(heads)}", (status_x, 85),
                FONT_MONO, 0.45, C_TEXT_DIM, 1, cv2.LINE_AA)

    # ── Verlaufsdiagramm Mitte ──
    gx, gy  = 200, 15
    gw, gh  = W - 200 - 180, 80
    max_val = max(max(history) if history else 1, 1)

    cv2.rectangle(canvas, (gx, gy), (gx + gw, gy + gh), (45, 45, 45), -1)
    cv2.rectangle(canvas, (gx, gy), (gx + gw, gy + gh), (70, 70, 70), 1)

    n = len(history)
    if n > 1:
        pts = []
        for i, v in enumerate(history):
            px = gx + int(i / (n - 1) * gw)
            py = gy + gh - int(v / max_val * (gh - 4)) - 2
            pts.append((px, py))
        for i in range(len(pts) - 1):
            cv2.line(canvas, pts[i], pts[i+1], C_ACCENT, 2)
        cv2.circle(canvas, pts[-1], 4, C_WHITE, -1)

    cv2.putText(canvas, f"{max_val}", (gx - 25, gy + 10),
                FONT_MONO, 0.38, C_TEXT_DIM, 1)
    cv2.putText(canvas, "0", (gx - 15, gy + gh),
                FONT_MONO, 0.38, C_TEXT_DIM, 1)
    cv2.putText(canvas, f"letzte {HISTORY_SECONDS}s", (gx, gy + gh + 14),
                FONT_MONO, 0.38, C_TEXT_DIM, 1)


# ─────────────────────────────────────────────
#  Hauptschleife
# ─────────────────────────────────────────────

def main():
    print("=" * 55)
    print("  People Counter – Xtion Pro | Visuelles Dashboard")
    print("=" * 55)
    print("  Taste  q / ESC  = Beenden")
    print("  Taste  d        = Demo-Modus umschalten")
    print("  Taste  s        = Screenshot speichern")
    print("=" * 55)

    global DEMO_MODE

    cap = None
    if not DEMO_MODE:
        cap = open_camera()

    # Verlaufsring: eine Messung pro Sekunde
    history    = collections.deque(maxlen=HISTORY_SECONDS)
    last_count = -1
    last_hist  = time.time()
    t_start    = time.time()

    fps_ring   = collections.deque(maxlen=20)
    frame_n    = 0

    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WINDOW_NAME, 1280, 620)

    while True:
        t0 = time.time()

        # ── Tiefenbild holen ──
        if DEMO_MODE:
            depth_map = generate_demo_frame(time.time() - t_start)
            time.sleep(0.033)
        else:
            cap.grab()
            frame_n += 1
            if frame_n % 2 != 0:          # jedes 2. Frame verarbeiten
                continue
            ret, depth_map = cap.retrieve(flag=cv2.CAP_OPENNI_DEPTH_MAP)
            if not ret or depth_map is None:
                print("[WARN] Kein Tiefenbild …")
                time.sleep(0.05)
                continue

        # ── Erkennung ──
        count, depth_view, binary_view, heads = detect_people(depth_map)

        if count != last_count:
            print(f"[INFO] Personen: {last_count if last_count >= 0 else '?'} → {count}")
            last_count = count

        # Verlauf (1×/Sekunde)
        now = time.time()
        if now - last_hist >= 1.0:
            history.append(count)
            last_hist = now

        # ── FPS berechnen ──
        dt = time.time() - t0
        fps_ring.append(dt)
        fps = 1.0 / (sum(fps_ring) / len(fps_ring)) if fps_ring else 0

        # ── Layout zusammensetzen ──
        #
        #  ┌──────────────────────────────────────────┐
        #  │           HUD-Panel (110 px)              │
        #  ├────────────────────┬─────────────────────┤
        #  │  Tiefenbild        │  Binärmaske          │
        #  │  (Falschfarben)    │  (weiß = Kopf)       │
        #  └────────────────────┴─────────────────────┘

        TARGET_W  = 640
        TARGET_H  = 480
        HUD_H     = 110

        depth_rs  = cv2.resize(depth_view,  (TARGET_W, TARGET_H))
        binary_rs = cv2.resize(binary_view, (TARGET_W, TARGET_H))

        # Binärmaske: farbige Overlay-Tönung
        tinted = binary_rs.copy()
        tinted[:, :, 0] = (tinted[:, :, 0].astype(np.float32) * 0.2).astype(np.uint8)
        tinted[:, :, 1] = (tinted[:, :, 1].astype(np.float32) * 0.9).astype(np.uint8)
        tinted[:, :, 2] = (tinted[:, :, 2].astype(np.float32) * 0.3).astype(np.uint8)

        # Labels auf die Hälften
        cv2.putText(depth_rs, "Tiefenbild (Falschfarben)",
                    (10, TARGET_H - 12), cv2.FONT_HERSHEY_SIMPLEX,
                    0.45, C_TEXT_DIM, 1, cv2.LINE_AA)
        cv2.putText(tinted, "Erkannte Kopf-Bereiche (Maske)",
                    (10, TARGET_H - 12), cv2.FONT_HERSHEY_SIMPLEX,
                    0.45, C_TEXT_DIM, 1, cv2.LINE_AA)

        row       = np.hstack([depth_rs, tinted])
        FULL_W    = row.shape[1]

        hud       = np.full((HUD_H, FULL_W, 3), C_BG, dtype=np.uint8)
        draw_hud(hud, count, fps, history, heads)

        canvas    = np.vstack([hud, row])

        # Trennlinie zwischen den Kamerabildern
        cv2.line(canvas, (FULL_W // 2, HUD_H), (FULL_W // 2, canvas.shape[0]),
                 (60, 60, 60), 1)

        cv2.imshow(WINDOW_NAME, canvas)

        key = cv2.waitKey(1) & 0xFF
        if key in (ord('q'), 27):           # q oder ESC
            break
        elif key == ord('d'):
            DEMO_MODE = not DEMO_MODE
            print(f"[INFO] Demo-Modus: {'AN' if DEMO_MODE else 'AUS'}")
        elif key == ord('s'):
            fname = f"screenshot_{int(time.time())}.png"
            cv2.imwrite(fname, canvas)
            print(f"[INFO] Screenshot gespeichert: {fname}")

    if cap:
        cap.release()
    cv2.destroyAllWindows()
    print("[INFO] Beendet.")


if __name__ == "__main__":
    main()
