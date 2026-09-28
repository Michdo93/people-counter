"""
People Counter – Linux / Raspberry Pi (Headless)
=================================================
Xtion Pro → OpenNI2 → Person Detection → MQTT + Web Server with Live MJPEG

Fixes compared to the previous version:
  - MJPEG stream runs in its own thread (no longer blocking)
  - Side-view mode: detects body silhouettes, not just heads from above
  - libOpenNI2.so is automatically detected

Installation:
  sudo apt install -y libopenni2-dev openni2-utils python3-pip
  pip3 install primesense numpy opencv-python-headless paho-mqtt flask

udev rule (one-time):
  echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="1d27", MODE="0666"' | \
      sudo tee /etc/udev/rules.d/99-xtion.rules && sudo udevadm control --reload-rules

Start:
  python3 people_counter_pi.py

Browser:
  http://<IP>:5000
"""

import cv2
import numpy as np
import collections
import time
import sys
import os
import threading
import json

import paho.mqtt.client as mqtt
from flask import Flask, Response, render_template_string

# ─────────────────────────────────────────────
#  Configuration
# ─────────────────────────────────────────────

OPENNI2_REDIST   = None          # None = search automatically

MQTT_BROKER      = "127.0.0.1"
MQTT_PORT        = 1883
MQTT_TOPIC_COUNT = "people_counter/count"
MQTT_TOPIC_STATE = "people_counter/status"

WEB_HOST         = "0.0.0.0"
WEB_PORT         = 5000
STREAM_FPS       = 10

DEPTH_MIN_MM     = 800
DEPTH_MAX_MM     = 3500

# ── Detection Mode ──────────────────────────
# “side”  = Camera mounted on the side → detects body silhouettes
# “top”   = Camera mounted from above         → detects round heads
MODE             = "side"

# Blob filter for page view (larger, less round blobs)
SIDE_AREA_MIN        = 3000
SIDE_AREA_MAX        = 80000
SIDE_ASPECT_MAX      = 5.0      # max height-to-width ratio (object is standing upright)
SIDE_ASPECT_MIN      = 0.3      # min.

# Blob filter for top view (round heads)
TOP_AREA_MIN         = 500
TOP_AREA_MAX         = 6000
TOP_CIRCULARITY_MIN  = 0.40

GAUSSIAN_BLUR    = (11, 11)
MORPH_KERNEL     = 5
SMOOTHING_FRAMES = 11

# ─────────────────────────────────────────────
#  Colors (BGR)
# ─────────────────────────────────────────────
C_BG       = (18,  18,  18)
C_ACCENT   = (0,  220, 140)
C_WARN     = (0,  100, 255)
C_TEXT_DIM = (100, 100, 100)
C_WHITE    = (255, 255, 255)
C_HEAD     = (0,  255, 120)

# ─────────────────────────────────────────────
#  Global State (thread-safe)
# ─────────────────────────────────────────────
lock        = threading.Lock()
g_count     = 0
g_raw       = 0
g_fps       = 0.0
g_history   = collections.deque(maxlen=60)
g_frame_jpg = None
running     = True

# ─────────────────────────────────────────────
#  OpenNI2
# ─────────────────────────────────────────────

SEARCH_PATHS = [
    "/usr/lib/x86_64-linux-gnu",
    "/usr/lib/aarch64-linux-gnu",
    "/usr/lib/arm-linux-gnueabihf",
    "/usr/local/lib",
    "/usr/lib",
]

def find_openni2():
    import subprocess
    try:
        r = subprocess.run(["find", "/usr", "-name", "libOpenNI2.so"],
                           capture_output=True, text=True, timeout=5)
        lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
        if lines:
            p = os.path.dirname(lines[0])
            print(f"[OpenNI2] Found: {lines[0]}")
            return p
    except Exception:
        pass
    for p in SEARCH_PATHS:
        if os.path.exists(os.path.join(p, "libOpenNI2.so")):
            return p
    return None


def init_openni():
    try:
        from primesense import openni2
    except ImportError:
        print("[ERROR] pip3 install primesense")
        sys.exit(1)

    path = OPENNI2_REDIST or find_openni2()
    try:
        if path:
            openni2.initialize(path)
        else:
            openni2.initialize()
        print(f"[OpenNI2] Initialized: {path or 'system'}")
    except Exception as e:
        print(f"[ERROR] OpenNI2-Init: {e}")
        sys.exit(1)

    try:
        dev  = openni2.Device.open_any()
        name = dev.get_device_info().name
        if isinstance(name, bytes):
            name = name.decode()
        print(f"[OpenNI2] Camera: {name}")
    except Exception as e:
        print(f"[ERROR] Camera: {e}")
        openni2.unload()
        sys.exit(1)

    ds = dev.create_depth_stream()
    ds.set_video_mode(openni2.c_api.OniVideoMode(
        pixelFormat=openni2.c_api.OniPixelFormat.ONI_PIXEL_FORMAT_DEPTH_1_MM,
        resolutionX=640, resolutionY=480, fps=30
    ))
    ds.start()
    print("[OpenNI2] Stream started (640×480 @ 30 FPS)")
    return openni2, dev, ds


def read_frame(ds):
    f   = ds.read_frame()
    buf = f.get_buffer_as_uint16()
    return np.frombuffer(buf, dtype=np.uint16).reshape(480, 640).copy()


# ─────────────────────────────────────────────
#  Person Detection
# ─────────────────────────────────────────────

def preprocess(depth_map):
    """Depth image → normalized inverted grayscale image + binary mask."""
    valid    = (depth_map > DEPTH_MIN_MM) & (depth_map < DEPTH_MAX_MM)
    filtered = np.where(valid, depth_map, DEPTH_MAX_MM)
    norm     = cv2.normalize(filtered.astype(np.float32),
                              None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    inv      = 255 - norm
    blur     = cv2.GaussianBlur(inv, GAUSSIAN_BLUR, 0)
    _, binary = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    k        = np.ones((MORPH_KERNEL, MORPH_KERNEL), np.uint8)
    binary   = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, k, iterations=2)
    binary   = cv2.morphologyEx(binary, cv2.MORPH_OPEN,  k, iterations=1)
    return norm, binary


def detect_side(depth_map):
    """
    Side view: Camera mounted at height, shows horizontally into the room.
    People appear as large, tall silhouettes.
    Detection via width projection: for each x-column check if a
    connected foreground area is present, then form clusters.
    """
    norm, binary = preprocess(depth_map)
    vis          = cv2.applyColorMap(norm, cv2.COLORMAP_TURBO)

    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    people = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if not (SIDE_AREA_MIN <= area <= SIDE_AREA_MAX):
            continue

        x, y, w, h = cv2.boundingRect(cnt)
        if w == 0:
            continue
        aspect = h / w      # Body: tall and slender → aspect > 1, typically

        if not (SIDE_ASPECT_MIN <= aspect <= SIDE_ASPECT_MAX):
            continue

        # Center of bounding box
        cx, cy = x + w // 2, y + h // 2

        # Medium Depth
        m   = np.zeros(depth_map.shape, np.uint8)
        cv2.drawContours(m, [cnt], -1, 255, -1)
        d_m = cv2.mean(depth_map, mask=m)[0] / 1000.0

        people.append((cx, cy, w, h, d_m))

        # Visualization
        cv2.rectangle(vis, (x, y), (x+w, y+h), C_HEAD, 2)
        cv2.circle(vis, (cx, cy), 5, C_WHITE, -1)
        cv2.putText(vis, f"{d_m:.1f}m  {w}x{h}px",
                    (x, y - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.42, C_WHITE, 1)

    return len(people), vis


def detect_top(depth_map):
    """Top view: Camera mounted from above → heads as round blobs."""
    norm, binary = preprocess(depth_map)
    vis          = cv2.applyColorMap(norm, cv2.COLORMAP_TURBO)

    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    heads = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if not (TOP_AREA_MIN <= area <= TOP_AREA_MAX):
            continue
        perim = cv2.arcLength(cnt, True)
        if perim == 0 or (4 * np.pi * area) / perim**2 < TOP_CIRCULARITY_MIN:
            continue
        (cx, cy), r = cv2.minEnclosingCircle(cnt)
        cx, cy, r   = int(cx), int(cy), int(r)
        m           = np.zeros(depth_map.shape, np.uint8)
        cv2.drawContours(m, [cnt], -1, 255, -1)
        d_m         = cv2.mean(depth_map, mask=m)[0] / 1000.0
        heads.append((cx, cy, r, d_m))
        cv2.circle(vis, (cx, cy), r + 6, C_HEAD, 2)
        cv2.circle(vis, (cx, cy), 4, C_WHITE, -1)
        cv2.putText(vis, f"{d_m:.1f}m", (cx-18, cy-r-10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, C_WHITE, 1)
    return len(heads), vis


def detect(depth_map):
    if MODE == "side":
        return detect_side(depth_map)
    return detect_top(depth_map)


# ─────────────────────────────────────────────
#  Render the dashboard frame
# ─────────────────────────────────────────────

def render_frame(vis, count, raw, fps, history):
    H, W  = vis.shape[:2]
    FM    = cv2.FONT_HERSHEY_SIMPLEX
    FD    = cv2.FONT_HERSHEY_DUPLEX

    # Counter in the upper left corner of the camera image
    cv2.rectangle(vis, (0, 0), (W, 48), (20, 20, 20), -1)
    col = C_ACCENT if count < 5 else C_WARN
    cv2.putText(vis, f"{count} People", (10, 34), FD, 1.1, col, 2, cv2.LINE_AA)
    cv2.putText(vis, f"Raw:{raw}  FPS:{fps:.1f}  Mode:{MODE}",
                (W - 280, 22), FM, 0.42, C_TEXT_DIM, 1)

    # History panel to the right of it
    panel_w = 220
    panel   = np.full((H, panel_w, 3), C_BG, dtype=np.uint8)

    # Large number
    big  = str(count)
    tw   = cv2.getTextSize(big, FD, 3.5, 4)[0][0]
    cv2.putText(panel, big, ((panel_w - tw)//2, 90), FD, 3.5, col, 4, cv2.LINE_AA)
    cv2.putText(panel, "People", (10, 115), FM, 0.45, C_TEXT_DIM, 1)

    # History diagram
    gx, gy, gw, gh = 10, 130, panel_w - 20, H - 160
    mx = max(max(history) if history else 1, 1)
    cv2.rectangle(panel, (gx, gy), (gx+gw, gy+gh), (40, 40, 40), -1)
    cv2.rectangle(panel, (gx, gy), (gx+gw, gy+gh), (70, 70, 70), 1)
    n = len(history)
    if n > 1:
        pts = [(gx + int(i/(n-1)*gw), gy + gh - int(v/mx*(gh-4)) - 2)
               for i, v in enumerate(history)]
        for i in range(len(pts)-1):
            cv2.line(panel, pts[i], pts[i+1], C_ACCENT, 2)
        cv2.circle(panel, pts[-1], 4, C_WHITE, -1)

    cv2.putText(panel, str(mx), (gx-2, gy+10), FM, 0.35, C_TEXT_DIM, 1)
    cv2.putText(panel, "0", (gx-2, gy+gh), FM, 0.35, C_TEXT_DIM, 1)
    cv2.putText(panel, f"{n}s", (gx, gy+gh+14), FM, 0.35, C_TEXT_DIM, 1)

    return np.hstack([vis, panel])


# ─────────────────────────────────────────────
#  MQTT
# ─────────────────────────────────────────────

def create_mqtt():
    client = mqtt.Client(client_id="xtion_people_counter")
    def on_connect(c, u, f, rc):
        if rc == 0:
            print(f"[MQTT] Connected: {MQTT_BROKER}:{MQTT_PORT}")
            c.publish(MQTT_TOPIC_STATE, "online", retain=True)
        else:
            print(f"[MQTT] Error rc={rc}")
    client.on_connect = on_connect
    client.will_set(MQTT_TOPIC_STATE, "offline", retain=True)
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        client.loop_start()
    except Exception as e:
        print(f"[MQTT] Not reachable: {e}")
    return client


# ─────────────────────────────────────────────
#  Camera Thread
# ─────────────────────────────────────────────

def camera_thread(mqtt_client):
    global g_count, g_raw, g_fps, g_frame_jpg, running

    openni2_mod, dev, ds = init_openni()
    raw_ring     = collections.deque(maxlen=SMOOTHING_FRAMES)
    last_pub     = -1
    last_hist    = time.time()
    fps_ring     = collections.deque(maxlen=20)
    last_encode  = 0.0
    enc_interval = 1.0 / STREAM_FPS

    print(f"[Camera] Thread started (Mode: {MODE})")

    try:
        while running:
            t0        = time.time()
            depth_map = read_frame(ds)
            raw_count, vis = detect(depth_map)

            raw_ring.append(raw_count)
            smoothed = int(np.median(list(raw_ring)))

            if smoothed != last_pub:
                print(f"[INFO] People: {last_pub if last_pub >= 0 else '?'} → {smoothed}  (Raw: {raw_count})")
                try:
                    mqtt_client.publish(MQTT_TOPIC_COUNT, str(smoothed), qos=1, retain=True)
                except Exception:
                    pass
                last_pub = smoothed

            now = time.time()
            if now - last_hist >= 1.0:
                with lock:
                    g_history.append(smoothed)
                last_hist = now

            fps_ring.append(time.time() - t0)
            fps = 1.0 / (sum(fps_ring) / len(fps_ring)) if fps_ring else 0.0

            with lock:
                g_count = smoothed
                g_raw   = raw_count
                g_fps   = fps

            # Frame encoding
            if now - last_encode >= enc_interval:
                with lock:
                    hist_snap = list(g_history)
                frame = render_frame(vis, smoothed, raw_count, fps, hist_snap)
                _, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
                with lock:
                    g_frame_jpg = jpg.tobytes()
                last_encode = now

    except Exception as e:
        print(f"[Camera] Error: {e}")
        import traceback; traceback.print_exc()
    finally:
        ds.stop()
        dev.close()
        openni2_mod.unload()
        print("[Camera] Stopped")


# ─────────────────────────────────────────────
#  MJPEG thread  ← FIX: runs separately, does not block Flask
# ─────────────────────────────────────────────

class MjpegStreamer:
    """Maintains a list of active generator clients and distributes frames."""
    def __init__(self):
        self._clients = []
        self._lock    = threading.Lock()

    def add(self, q):
        with self._lock:
            self._clients.append(q)

    def remove(self, q):
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def push(self, jpg):
        with self._lock:
            for q in self._clients:
                if q.qsize() < 2:        # Buffer a maximum of 2 frames
                    q.put(jpg)


streamer = MjpegStreamer()


def mjpeg_push_thread():
    """Reads g_frame_jpg and distributes to all open Stream-Clients."""
    import queue
    last = None
    while running:
        with lock:
            jpg = g_frame_jpg
        if jpg is not None and jpg is not last:
            streamer.push(jpg)
            last = jpg
        time.sleep(1.0 / STREAM_FPS)


# ─────────────────────────────────────────────
#  Flask
# ─────────────────────────────────────────────

app = Flask(__name__)

PAGE = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>People Counter</title>
<style>
* { box-sizing:border-box; margin:0; padding:0 }
body { background:#111; color:#ddd; font-family:'Courier New',monospace;
       display:flex; flex-direction:column; align-items:center;
       padding:16px; gap:14px; min-height:100vh }
h1   { color:#00dc8c; font-size:1.25rem; letter-spacing:.1em; margin-top:6px }
.cnt { background:#1a1a1a; border:2px solid #00dc8c; border-radius:12px;
       padding:16px 48px; text-align:center }
.num { font-size:4.5rem; font-weight:bold; color:#00dc8c; line-height:1 }
.lbl { color:#555; font-size:.75rem; margin-top:4px }
.meta{ color:#444; font-size:.7rem }
.box { border:1px solid #2a2a2a; border-radius:8px; overflow:hidden;
       width:100%; max-width:900px }
.box img { width:100%; display:block }
.cap { background:#161616; color:#444; font-size:.68rem; padding:3px 10px }
.dot { display:inline-block; width:7px; height:7px; border-radius:50%;
       background:#00dc8c; animation:pulse 2s infinite }
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
</style>
</head>
<body>
<h1>&#9679; People Counter &mdash; Xtion Pro</h1>
<div class="cnt">
  <div class="num" id="n">{{ count }}</div>
  <div class="lbl">Personen erkannt (Modus: {{ mode }})</div>
  <div class="meta">Roh: <span id="r">{{ raw }}</span> &nbsp;|&nbsp; FPS: <span id="f">{{ fps }}</span></div>
</div>
<div class="box">
  <img src="/stream" alt="Live-Stream">
  <div class="cap">
    <span class="dot"></span>
    Live-Dashboard (MJPEG) &nbsp;&bull;&nbsp; MQTT: {{ topic }}
  </div>
</div>
<div class="meta">{{ ts }}</div>
<script>
setInterval(async()=>{
  try{
    const d=await fetch('/api').then(r=>r.json());
    document.getElementById('n').textContent=d.count;
    document.getElementById('r').textContent=d.raw;
    document.getElementById('f').textContent=d.fps;
  }catch(e){}
}, 1500);
</script>
</body>
</html>"""


@app.route("/")
def index():
    with lock:
        c = g_count; r = g_raw; f = f"{g_fps:.1f}"
    return render_template_string(PAGE, count=c, raw=r, fps=f,
                                  mode=MODE, topic=MQTT_TOPIC_COUNT,
                                  ts=time.strftime("%H:%M:%S"))


def gen_stream():
    import queue
    q = queue.Queue()
    streamer.add(q)
    try:
        while running:
            try:
                jpg = q.get(timeout=2.0)
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n")
            except Exception:
                pass
    finally:
        streamer.remove(q)


@app.route("/stream")
def stream():
    return Response(gen_stream(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api")
def api():
    with lock:
        return Response(
            json.dumps({"count": g_count, "raw": g_raw,
                        "fps": round(g_fps, 1), "ts": time.time()}),
            mimetype="application/json"
        )


# ─────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────

def main():
    global running

    print("=" * 58)
    print("  People Counter – Linux/Pi (headless)")
    print(f"  Modus:  {MODE}  ({'Side View' if MODE=='side' else 'Top View'})")
    print(f"  Web:    http://0.0.0.0:{WEB_PORT}")
    print(f"  MQTT:   {MQTT_BROKER}:{MQTT_PORT} → {MQTT_TOPIC_COUNT}")
    print("  Press Ctrl+C to exit")
    print("=" * 58)

    mqtt_client = create_mqtt()

    # Threads starten
    threads = [
        threading.Thread(target=camera_thread, args=(mqtt_client,), daemon=True),
        threading.Thread(target=mjpeg_push_thread, daemon=True),
    ]
    for t in threads:
        t.start()

    time.sleep(2)  # Wait until the first frame is available

    try:
        app.run(host=WEB_HOST, port=WEB_PORT,
                threaded=True, use_reloader=False, debug=False)
    except KeyboardInterrupt:
        pass
    finally:
        running = False
        print("\n[INFO] Exiting ...")
        try:
            mqtt_client.publish(MQTT_TOPIC_STATE, "offline", retain=True)
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
        except Exception:
            pass
        for t in threads:
            t.join(timeout=3)
        print("[INFO] Finished.")


if __name__ == "__main__":
    main()
