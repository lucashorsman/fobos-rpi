# fobos-rpi-bridge

RPi5 bridge service for **FOBOS** — captures frames from an Allied Vision GigE camera via the Vimba X SDK, controls a laser diode through GPIO, and streams everything to a remote host GUI over WebSocket and HTTP.

## Why this exists

The Allied Vision camera speaks GigE Vision, which doesn't survive a VPN hop cleanly. This bridge runs on the Raspberry Pi 5 that's physically connected to the camera and laser hardware, handling:

- **Camera capture** — grabs frames via `vmbpy`, runs any local OpenCV processing (fiducial detection, undistortion, overlays), JPEG-encodes, and pushes them out
- **Laser control** — drives the laser diode through an AQY212 transistor on a GPIO pin with PWM intensity control via `gpiozero`
- **Streaming** — serves frames to the host-side FOBOS GUI over WebSocket and provides a standalone browser-based MJPEG preview with full controls

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Raspberry Pi 5                                 │
│                                                 │
│  ┌──────────────┐   ┌──────────────────────┐    │
│  │ Allied Vision│   │  camera_stream_server │    │
│  │ GigE Camera  │──▶│  (vmbpy + OpenCV)    │    │
│  └──────────────┘   └──────────┬───────────┘    │
│                                │                │
│                        FrameBroadcaster         │
│                       (latest frame wins)       │
│                       ┌────────┴────────┐       │
│                       ▼                 ▼       │
│  ┌────────────────────────┐  ┌──────────────┐   │
│  │  WebSocket server      │  │ HTTP server   │   │
│  │  :8765/video (binary)  │  │ :8080 MJPEG   │   │
│  │  :8765/control (JSON)  │  │ + REST API    │   │
│  └────────────────────────┘  └──────────────┘   │
│                                                 │
│  ┌──────────────┐   ┌──────────────────────┐    │
│  │ GPIO 17      │◀──│  laser_control       │    │
│  │ (AQY212)     │   │  (gpiozero PWMLED)   │    │
│  └──────────────┘   └──────────────────────┘    │
└─────────────────────────────────────────────────┘
         │                         │
         ▼                         ▼
  ┌─────────────┐          ┌─────────────┐
  │ FOBOS GUI   │          │ Any browser  │
  │ (PySide6)   │          │ preview page │
  └─────────────┘          └─────────────┘
```

The **"latest frame wins"** design is intentional — there's no frame queue. Each client gets the most recent frame when it's ready, so a slow VPN link skips stale frames rather than building up latency.

## Requirements

- **Hardware**: Raspberry Pi 5 with an Allied Vision GigE camera and (optionally) a laser diode on GPIO
- **Python**: ≥ 3.11
- **Vimba X SDK**: Must be installed separately on the Pi — `vmbpy` is not on PyPI. Install the SDK, then `pip install` the wheel it provides (usually under `api/python/`). A local copy of the wheel (`vmbpy-1.0.4-py3-none-any.whl`) is included in the repo for convenience.

## Setup

```bash
# Clone
git clone https://github.com/your-org/fobos-rpi.git
cd fobos-rpi

# Create venv and install (using uv)
uv sync

# Or with plain pip
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

If `vmbpy` isn't available (e.g. on a dev machine without the Vimba X SDK), the bridge falls back to a **synthetic test-pattern feed** automatically — you can still develop and test the WebSocket/HTTP plumbing without camera hardware.

Similarly, laser control runs in **stub mode** on non-RPi hosts.

## Usage

```bash
# Start with defaults (WS on :8765, web preview on :8080)
python3 main.py

# Custom ports and camera
python3 main.py --port 9000 --web-port 8081 --camera-id DEV_000F3103CB74 --laser-pin 17
```

| Flag            | Default               | Description                              |
|-----------------|-----------------------|------------------------------------------|
| `--port`        | `8765`                | WebSocket server port                    |
| `--web-port`    | `8080`                | HTTP preview server port                 |
| `--camera-id`   | `DEV_000F3103CB74`    | Allied Vision camera ID to connect to    |
| `--laser-pin`   | `17`                  | BCM GPIO pin for laser transistor base   |

Once running, open `http://<pi-hostname>:8080/` in a browser for a live MJPEG preview with camera selection and laser controls.

## API Reference

### WebSocket endpoints (`:8765`)

| Path       | Direction       | Format        | Description                     |
|------------|-----------------|---------------|---------------------------------|
| `/video`   | Pi → client     | Binary JPEG   | Continuous frame stream         |
| `/control` | Bidirectional   | JSON          | Commands and responses          |

#### Control commands

Send JSON to `/control`, receive a JSON reply:

```jsonc
// Laser
{"cmd": "laser_on", "intensity": 0.75}
{"cmd": "laser_off"}
{"cmd": "laser_set_intensity", "value": 0.5}

// Camera
{"cmd": "set_exposure", "value": 10000}       // microseconds
{"cmd": "set_gain", "value": 12.0}            // dB
{"cmd": "list_cameras"}
{"cmd": "select_camera", "camera_id": "DEV_000F3103CB74"}
{"cmd": "camera_status"}

// General
{"cmd": "status"}    // full laser + camera state
{"cmd": "ping"}      // returns {"cmd": "pong"}
```

### HTTP endpoints (`:8080`)

| Endpoint                       | Method     | Description                           |
|--------------------------------|------------|---------------------------------------|
| `/`                            | GET        | Camera preview page with laser controls |
| `/stream`                      | GET        | Raw MJPEG stream                      |
| `/snapshot`                     | GET        | Single JPEG frame                     |
| `/status`                      | GET        | JSON camera + laser status            |
| `/cameras`                     | GET        | List available cameras                |
| `/select_camera?id=<cam_id>`   | POST       | Switch active camera                  |
| `/laser`                       | GET/POST   | Laser status / control                |

#### Laser HTTP control

```bash
# Toggle on at full power
curl -X POST http://pi:8080/laser -H 'Content-Type: application/json' \
     -d '{"action": "on", "intensity": 1.0}'

# Set intensity
curl -X POST http://pi:8080/laser -H 'Content-Type: application/json' \
     -d '{"action": "set_intensity", "intensity": 0.5}'

# Turn off
curl -X POST http://pi:8080/laser -d '{"action": "off"}'

# Get status
curl http://pi:8080/laser?action=status
```

## Project Structure

```
fobos-rpi/
├── main.py                  # Entry point — starts capture thread, WS server, HTTP server
├── camera_stream_server.py  # CameraProcessor (vmbpy capture + OpenCV) + FrameBroadcaster
├── laser_control.py         # LaserController — GPIO PWM via gpiozero, with stub fallback
├── web_preview.py           # Async HTTP server (MJPEG stream, REST API, serves index.html)
├── index.html               # Browser-based camera preview + laser control UI
├── pyproject.toml           # Package metadata and dependencies
├── vmbpy-1.0.4-py3-none-any.whl  # Bundled vmbpy wheel for offline install
├── camera_test_stream.py    # CLI viewer — connects to /video and shows frames with cv2.imshow
├── test_cam_select.py       # Integration test for camera selection via HTTP
└── test.py                  # Quick WebSocket control channel smoke test
```

## Development

```bash
# Install dev dependencies
uv sync --extra dev

# Lint
ruff check .

# Run on a dev machine (no camera/GPIO — uses synthetic fallback)
python3 main.py
```

The synthetic fallback generates 640×480 test-pattern frames at 15 fps with the selected camera ID overlaid, so you can test the full pipeline end-to-end without hardware.

## License

MIT
