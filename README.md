# People Counter – Xtion Pro

A headless people counter based on the **ASUS Xtion Pro** depth camera featuring:

- **Person detection** via OpenNI2 + OpenCV (side-view or top-view mode)
- **MQTT publishing** of the current person count on every change
- **Live dashboard** in the browser (MJPEG stream + history chart)
- **Temporal smoothing** (median filter over N frames, no flickering)

---

## Table of Contents

- [Hardware Requirements](#hardware-requirements)
- [Installation – Ubuntu (x86\_64)](#installation--ubuntu-x86_64)
- [Installation – Raspberry Pi OS (ARM)](#installation--raspberry-pi-os-arm)
- [Configuration](#configuration)
- [Running](#running)
- [Web Interface](#web-interface)
- [MQTT](#mqtt)
- [Running as a System Service](#running-as-a-system-service)
- [Detection Modes](#detection-modes)
- [Troubleshooting](#troubleshooting)

---

## Hardware Requirements

| Component | Notes |
|---|---|
| ASUS Xtion Pro | Not "Xtion Pro Live" – no RGB sensor |
| USB 2.0 port | Must be USB 2.0, not USB 3.0 |
| Powered USB hub | **Required on Raspberry Pi** – the camera draws too much current from the Pi's own ports |
| Mounting | Side-view: ~1–3 m distance to persons; Top-view: 2–2.5 m height |

> **Note:** The ASUS Xtion Pro has **no RGB camera**. The stream shows only the depth image rendered as a false-colour heat map.

---

## Installation – Ubuntu (x86\_64)

Tested on **Ubuntu 22.04 / 24.04**.

### 1. Install system packages

```bash
sudo apt update
sudo apt install -y \
    libopenni2-dev \
    openni2-utils \
    python3-pip \
    python3-venv \
    libusb-1.0-0-dev
```

### 2. Set udev rule

Allows the camera to be accessed without `sudo`:

```bash
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="1d27", MODE="0666"' | \
    sudo tee /etc/udev/rules.d/99-xtion.rules
sudo udevadm control --reload-rules
```

Unplug the camera and plug it back in.

### 3. Test the camera

```bash
NiViewer2
```

If the depth image appears, the driver and camera are working correctly.

### 4. Create a Python virtual environment

```bash
python3 -m venv ~/venv_xtion
source ~/venv_xtion/bin/activate
```

### 5. Install Python packages

```bash
pip install \
    primesense \
    numpy \
    opencv-python-headless \
    paho-mqtt \
    flask
```

> `opencv-python-headless` instead of `opencv-python` — no display overhead, significantly smaller footprint.

### 6. Verify the library path

```bash
find /usr -name "libOpenNI2.so" 2>/dev/null
# Expected: /usr/lib/x86_64-linux-gnu/libOpenNI2.so
```

The script detects this path automatically. If needed, it can be set manually in the configuration (see [Configuration](#configuration)).

---

## Installation – Raspberry Pi OS (ARM)

Tested on **Raspberry Pi OS Bookworm (64-bit)** with Pi 4 and Pi 5.

### 1. Install system packages

```bash
sudo apt update
sudo apt install -y \
    libopenni2-dev \
    openni2-utils \
    python3-pip \
    python3-venv \
    libusb-1.0-0-dev
```

### 2. Set udev rule

```bash
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="1d27", MODE="0666"' | \
    sudo tee /etc/udev/rules.d/99-xtion.rules
sudo udevadm control --reload-rules
```

Plug the camera into the **powered USB hub**, then verify it is recognised:

```bash
lsusb | grep -i prime
# Expected: Bus ... ID 1d27:0601 ASUS
```

### 3. Test the camera

On a headless Pi, `NiViewer2` cannot be used. Run this quick Python test instead:

```bash
python3 -c "
from primesense import openni2
openni2.initialize('/usr/lib/aarch64-linux-gnu')
dev = openni2.Device.open_any()
print('OK:', dev.get_device_info().name)
openni2.unload()
"
```

### 4. Check the Python version

```bash
python3 --version
# Raspberry Pi OS Bookworm: Python 3.11.x  ✓
```

> **Important:** `primesense` only works with Python ≤ 3.11. The import fails on Python 3.12 and above due to a breaking change in the `enum` module.

### 5. Create a Python virtual environment

```bash
python3 -m venv ~/venv_xtion
source ~/venv_xtion/bin/activate
```

### 6. Install Python packages

```bash
pip install \
    primesense \
    numpy \
    opencv-python-headless \
    paho-mqtt \
    flask
```

### 7. Verify the library path

```bash
find /usr -name "libOpenNI2.so" 2>/dev/null
# Pi 4/5 (64-bit): /usr/lib/aarch64-linux-gnu/libOpenNI2.so
# Pi 3   (32-bit): /usr/lib/arm-linux-gnueabihf/libOpenNI2.so
```

The script detects this path automatically.

---

## Configuration

All parameters are located at the top of `people_counter.py`:

```python
# OpenNI2 library path (None = auto-detect)
OPENNI2_REDIST   = None

# MQTT
MQTT_BROKER      = "localhost"      # IP or hostname of your broker
MQTT_PORT        = 1883
MQTT_TOPIC_COUNT = "people_counter/count"
MQTT_TOPIC_STATE = "people_counter/status"

# Web server
WEB_PORT         = 5000
STREAM_FPS       = 10              # MJPEG frame rate (lower = less CPU load)

# Detection mode
MODE             = "side"          # "side" = side-view, "top" = top-down view

# Blob filter – side-view
SIDE_AREA_MIN    = 3000            # increase to reduce noise
SIDE_AREA_MAX    = 80000           # increase if camera is very close

# Temporal smoothing
SMOOTHING_FRAMES = 11              # median over N frames (1 = disabled)
```

---

## Running

```bash
source ~/venv_xtion/bin/activate
python3 people_counter.py
```

Expected output on successful start:

```
==========================================================
  People Counter – Linux/Pi (headless)
  Mode:   side  (side-view)
  Web:    http://0.0.0.0:5000
  MQTT:   localhost:1883 → people_counter/count
  Press Ctrl+C to quit
==========================================================
[OpenNI2] Found: /usr/lib/aarch64-linux-gnu/libOpenNI2.so
[OpenNI2] Camera: PS1080
[OpenNI2] Stream started (640×480 @ 30 FPS)
[MQTT] Connected: localhost:1883
[Camera] Thread started (mode: side)
```

---

## Web Interface

Open a browser and navigate to:

```
http://<device-ip>:5000
```

| Section | Content |
|---|---|
| Counter | Smoothed person count, raw value, FPS |
| MJPEG stream | Depth image with detected persons + history chart |
| JSON API | `GET /api` → `{"count": 2, "raw": 2, "fps": 9.8, "ts": ...}` |

The counter updates every 1.5 seconds via JavaScript polling. The MJPEG stream runs continuously in the background.

---

## MQTT

A message is published whenever the smoothed person count changes:

| Topic | Payload | Notes |
|---|---|---|
| `people_counter/count` | `"2"` | `retain=true`, QoS 1 |
| `people_counter/status` | `"online"` / `"offline"` | Last Will configured |

To monitor messages:

```bash
mosquitto_sub -h localhost -t "people_counter/#" -v
```

### openHAB integration (example)

```java
// items/people_counter.items
Number PeopleCount "Person count [%d]" { mqtt="<[broker:people_counter/count:state:default]" }
```

---

## Running as a System Service

Create the service file:

```bash
sudo nano /etc/systemd/system/people-counter.service
```

```ini
[Unit]
Description=People Counter – Xtion Pro
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu
ExecStart=/home/ubuntu/venv_xtion/bin/python /home/ubuntu/people_counter.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

> On Raspberry Pi, replace `User=ubuntu` with `User=pi` (or whichever user runs the script).

Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable people-counter
sudo systemctl start people-counter

# Check status:
sudo systemctl status people-counter

# Follow logs:
journalctl -u people-counter -f
```

---

## Detection Modes

### `MODE = "side"` – Side-view (default)

Camera mounted at eye level or higher, pointing horizontally into the room.
Detects persons as large, upright silhouettes in the depth image.

| Parameter | Default | Description |
|---|---|---|
| `SIDE_AREA_MIN` | 3000 | Minimum blob area in pixels |
| `SIDE_AREA_MAX` | 80000 | Maximum blob area in pixels |
| `SIDE_ASPECT_MIN` | 0.3 | Minimum height/width ratio |
| `SIDE_ASPECT_MAX` | 5.0 | Maximum height/width ratio |

### `MODE = "top"` – Top-down view

Camera mounted overhead (2–2.5 m height), pointing straight down or at a downward angle.
Detects heads as round blobs.

| Parameter | Default | Description |
|---|---|---|
| `TOP_AREA_MIN` | 500 | Minimum head blob area |
| `TOP_AREA_MAX` | 6000 | Maximum head blob area |
| `TOP_CIRCULARITY_MIN` | 0.40 | Minimum circularity (0.0–1.0) |

---

## Troubleshooting

### `libOpenNI2.so: file does not exist`

```bash
sudo apt install libopenni2-dev
find /usr -name "libOpenNI2.so"
# Set the found directory path in OPENNI2_REDIST
```

### `TypeError: abstract class` when importing primesense

The Python version is too new (≥ 3.12). Use Python 3.11.

```bash
python3 --version
# If 3.12+: install Python 3.11 via pyenv or manually
```

### Camera not found (`No devices found`)

```bash
lsusb | grep 1d27                        # Is the camera visible?
cat /etc/udev/rules.d/99-xtion.rules     # Is the udev rule in place?
# Unplug and replug the camera
```

On Raspberry Pi: always use a **powered USB hub**.

### Stream loads in browser but no image appears

```bash
curl -v http://localhost:5000/stream --max-time 3
# Check whether MJPEG data is being received
```

If FPS = 0, the camera thread is stalled. Check the logs:

```bash
journalctl -u people-counter -f
```

### Count flickers heavily (0–1–0–1)

Increase `SMOOTHING_FRAMES` (e.g. to 15–21) or raise `SIDE_AREA_MIN` to filter out small noise blobs.
