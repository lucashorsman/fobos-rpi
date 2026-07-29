"""
web_preview.py

Lightweight HTTP server that serves a live MJPEG camera preview page.
Runs alongside the existing WebSocket bridge on a separate port (default 8080).

Endpoints
---------
GET /           HTML page with embedded live stream + status panel
GET /stream     Raw MJPEG stream  (multipart/x-mixed-replace)
GET /snapshot   Single JPEG frame
GET /status     JSON camera status

Zero external dependencies — uses only asyncio + the existing FrameBroadcaster.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from camera_stream_server import CameraProcessor, FrameBroadcaster

logger = logging.getLogger("web_preview")


# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------

_HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FOBOS Camera Preview</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:        #0b0e17;
    --surface:   rgba(255,255,255,0.04);
    --border:    rgba(255,255,255,0.08);
    --text:      #e2e8f0;
    --text-dim:  #64748b;
    --accent:    #38bdf8;
    --green:     #22c55e;
    --red:       #ef4444;
    --amber:     #f59e0b;
    --radius:    12px;
  }

  body {
    font-family: 'Inter', system-ui, -apple-system, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
  }

  /* ---- header bar ---- */
  .header {
    width: 100%;
    padding: 16px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid var(--border);
    backdrop-filter: blur(12px);
    position: sticky;
    top: 0;
    z-index: 10;
    background: rgba(11,14,23,0.85);
  }
  .header-left {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .logo {
    font-size: 20px;
    font-weight: 700;
    letter-spacing: -0.02em;
  }
  .logo span { color: var(--accent); }

  .header-right {
    display: flex;
    align-items: center;
    gap: 14px;
  }

  .cam-select-wrap {
    display: flex;
    align-items: center;
    gap: 8px;
    background: var(--surface);
    border: 1px solid var(--border);
    padding: 6px 12px;
    border-radius: var(--radius);
    transition: all 0.2s ease;
  }
  .cam-select-wrap:hover, .cam-select-wrap:focus-within {
    border-color: rgba(255,255,255,0.25);
    background: rgba(255,255,255,0.06);
  }
  .cam-select-label {
    font-size: 11px;
    font-weight: 600;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .cam-select-dropdown {
    background: transparent;
    color: var(--text);
    border: none;
    font-size: 13px;
    font-weight: 500;
    font-family: inherit;
    outline: none;
    cursor: pointer;
  }
  .cam-select-dropdown option {
    background: #151926;
    color: var(--text);
  }

  /* ---- status badge ---- */
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 4px 12px;
    border-radius: 9999px;
    background: rgba(255,255,255,0.06);
    border: 1px solid var(--border);
    transition: all 0.3s ease;
  }
  .badge .dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--text-dim);
    transition: background 0.3s ease;
  }
  .badge.live .dot {
    background: var(--green);
    box-shadow: 0 0 8px rgba(34,197,94,0.5);
    animation: pulse 2s ease-in-out infinite;
  }
  .badge.offline .dot {
    background: var(--red);
    box-shadow: 0 0 8px rgba(239,68,68,0.4);
  }
  .badge.waiting .dot {
    background: var(--amber);
    animation: pulse 1.5s ease-in-out infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50%      { opacity: 0.4; }
  }

  /* ---- main content ---- */
  .main {
    flex: 1;
    width: 100%;
    max-width: 1200px;
    padding: 24px;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 20px;
  }

  /* ---- stream container ---- */
  .stream-wrap {
    position: relative;
    width: 100%;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
    aspect-ratio: 4 / 3;       /* fallback until image loads */
  }
  .stream-wrap img {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: contain;
    background: #000;
  }

  /* overlay when disconnected */
  .overlay {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 12px;
    background: rgba(0,0,0,0.75);
    backdrop-filter: blur(4px);
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.3s ease;
  }
  .overlay.visible { opacity: 1; pointer-events: auto; }
  .overlay svg { width: 48px; height: 48px; color: var(--text-dim); }
  .overlay p {
    font-size: 14px;
    color: var(--text-dim);
  }
  .spinner {
    width: 24px; height: 24px;
    border: 3px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ---- info cards ---- */
  .info-row {
    width: 100%;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px;
  }
  .info-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 16px;
    transition: border-color 0.2s;
  }
  .info-card:hover { border-color: rgba(255,255,255,0.15); }
  .info-card .label {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-dim);
    margin-bottom: 6px;
  }
  .info-card .value {
    font-size: 18px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }

  /* ---- footer ---- */
  .footer {
    padding: 16px;
    font-size: 12px;
    color: var(--text-dim);
    text-align: center;
    border-top: 1px solid var(--border);
    width: 100%;
  }
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <div class="logo"><span>FOBOS</span> Camera</div>
  </div>
  <div class="header-right">
    <div class="cam-select-wrap">
      <label for="cameraSelect" class="cam-select-label">Camera</label>
      <select id="cameraSelect" class="cam-select-dropdown">
        <option value="">Loading cameras...</option>
      </select>
    </div>
    <div class="badge waiting" id="statusBadge">
      <span class="dot"></span>
      <span id="statusLabel">Connecting</span>
    </div>
  </div>
</div>

<div class="main">
  <div class="stream-wrap">
    <img id="stream" src="/stream" alt="Camera stream">
    <div class="overlay visible" id="overlay">
      <div class="spinner"></div>
      <p id="overlayText">Connecting to camera…</p>
    </div>
  </div>

  <div class="info-row">
    <div class="info-card">
      <div class="label">Status</div>
      <div class="value" id="camStatus">—</div>
    </div>
    <div class="info-card">
      <div class="label">Exposure</div>
      <div class="value" id="camExposure">—</div>
    </div>
    <div class="info-card">
      <div class="label">Gain</div>
      <div class="value" id="camGain">—</div>
    </div>
    <div class="info-card">
      <div class="label">Camera</div>
      <div class="value" id="camId">—</div>
    </div>
  </div>
</div>

<div class="footer">fobos-rpi bridge &middot; live camera preview</div>

<script>
(function() {
  const img       = document.getElementById('stream');
  const overlay   = document.getElementById('overlay');
  const overlayTx = document.getElementById('overlayText');
  const badge     = document.getElementById('statusBadge');
  const badgeLbl  = document.getElementById('statusLabel');
  const camSelect = document.getElementById('cameraSelect');

  let streamOk = false;
  let retryTimer = null;

  function setStatus(state) {
    badge.className = 'badge ' + state;
    if (state === 'live')    badgeLbl.textContent = 'Live';
    if (state === 'offline') badgeLbl.textContent = 'Offline';
    if (state === 'waiting') badgeLbl.textContent = 'Connecting';
  }

  // Stream loaded its first frame
  img.onload = function() {
    if (!streamOk) {
      streamOk = true;
      overlay.classList.remove('visible');
      setStatus('live');
    }
  };

  // Stream errored or ended
  img.onerror = function() {
    streamOk = false;
    overlay.classList.add('visible');
    overlayTx.textContent = 'Stream disconnected — retrying…';
    setStatus('offline');
    scheduleRetry();
  };

  function scheduleRetry() {
    if (retryTimer) return;
    retryTimer = setTimeout(function() {
      retryTimer = null;
      setStatus('waiting');
      overlayTx.textContent = 'Reconnecting…';
      // Reload the MJPEG stream by resetting src with cache-buster
      img.src = '/stream?t=' + Date.now();
    }, 3000);
  }

  async function loadCameras() {
    try {
      const r = await fetch('/cameras');
      if (!r.ok) return;
      const data = await r.json();
      const cams = data.cameras || [];
      const selected = data.selected_camera_id;

      camSelect.innerHTML = '';
      if (cams.length === 0) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = 'No cameras found';
        camSelect.appendChild(opt);
        return;
      }

      cams.forEach(function(c) {
        const opt = document.createElement('option');
        opt.value = c.id;
        opt.textContent = c.name || c.id;
        if (selected ? c.id === selected : c.id === cams[0].id) {
          opt.selected = true;
        }
        camSelect.appendChild(opt);
      });
    } catch(e) {}
  }

  camSelect.addEventListener('change', async function() {
    const selectedId = camSelect.value;
    if (!selectedId) return;
    try {
      streamOk = false;
      overlay.classList.add('visible');
      overlayTx.textContent = 'Switching camera…';
      setStatus('waiting');
      const r = await fetch('/select_camera?id=' + encodeURIComponent(selectedId), { method: 'POST' });
      const res = await r.json();
      if (res.ok) {
        setTimeout(function() {
          img.src = '/stream?t=' + Date.now();
        }, 1000);
      }
    } catch(e) {
      console.error('Camera switch failed:', e);
    }
  });

  // Poll /status for camera info
  async function pollStatus() {
    try {
      const r = await fetch('/status');
      if (!r.ok) return;
      const s = await r.json();
      document.getElementById('camStatus').textContent =
        s.capturing ? 'Capturing' : s.running ? 'Starting' : 'Stopped';
      document.getElementById('camExposure').textContent =
        s.exposure_us != null ? (s.exposure_us / 1000).toFixed(1) + ' ms' : '—';
      document.getElementById('camGain').textContent =
        s.gain_db != null ? s.gain_db.toFixed(1) + ' dB' : '—';
      document.getElementById('camId').textContent =
        s.camera_id ? s.camera_id.split('/').pop() : '—';
    } catch(e) {}
  }
  setInterval(pollStatus, 3000);
  pollStatus();
  loadCameras();
})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Minimal async HTTP helpers
# ---------------------------------------------------------------------------

async def _read_request(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes]:
    """Read the HTTP request line, headers, and body.

    Returns (method, path, query_params, body).
    """
    line = await asyncio.wait_for(reader.readline(), timeout=5.0)
    parts = line.decode("utf-8", errors="replace").strip().split()
    method = parts[0] if len(parts) >= 1 else ""
    full_target = parts[1] if len(parts) >= 2 else "/"

    parsed_url = urlparse(full_target)
    path = parsed_url.path
    raw_params = parse_qs(parsed_url.query)
    query_params = {k: v[0] for k, v in raw_params.items() if v}

    content_length = 0
    # Drain headers and parse Content-Length
    while True:
        hdr = await asyncio.wait_for(reader.readline(), timeout=5.0)
        hdr_str = hdr.decode("utf-8", errors="replace").strip()
        if not hdr_str:
            break
        if ":" in hdr_str:
            k, v = hdr_str.split(":", 1)
            if k.strip().lower() == "content-length":
                try:
                    content_length = int(v.strip())
                except ValueError:
                    content_length = 0

    body = b""
    if content_length > 0:
        body = await asyncio.wait_for(reader.readexactly(content_length), timeout=5.0)

    return method, path, query_params, body


def _http_response(status: str, content_type: str, body: bytes, extra_headers: str = "") -> bytes:
    """Build a complete HTTP/1.1 response."""
    return (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"{extra_headers}"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode() + body


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class WebPreviewServer:
    """Async HTTP server for the camera preview page + MJPEG stream.

    Plugs into the existing ``FrameBroadcaster`` — no camera access of its own.
    """

    def __init__(
        self,
        broadcaster: "FrameBroadcaster",
        processor: "CameraProcessor | None" = None,
        port: int = 8080,
    ):
        self.broadcaster = broadcaster
        self.processor = processor
        self.port = port
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, "0.0.0.0", self.port,
        )
        logger.info("Web preview listening on http://0.0.0.0:%d/", self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # ---- connection handler ------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            method, path, query_params, body = await _read_request(reader)
            if method not in ("GET", "POST"):
                writer.write(_http_response("405 Method Not Allowed", "text/plain", b"GET or POST only"))
                await writer.drain()
                return

            if path == "/":
                await self._serve_html(writer)
            elif path == "/stream":
                await self._serve_mjpeg(writer)
            elif path == "/snapshot":
                await self._serve_snapshot(writer)
            elif path == "/status":
                await self._serve_status(writer)
            elif path in ("/cameras", "/api/cameras"):
                await self._serve_cameras(writer)
            elif path in ("/select_camera", "/cameras/select", "/api/cameras/select"):
                await self._serve_select_camera(writer, query_params, body)
            else:
                writer.write(_http_response("404 Not Found", "text/plain", b"not found"))
                await writer.drain()
        except (asyncio.TimeoutError, asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as exc:
            logger.debug("Web preview handler error: %s", exc)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # ---- route handlers ----------------------------------------------------

    async def _serve_html(self, writer: asyncio.StreamWriter) -> None:
        body = _HTML_PAGE.encode("utf-8")
        writer.write(_http_response("200 OK", "text/html; charset=utf-8", body))
        await writer.drain()

    async def _serve_mjpeg(self, writer: asyncio.StreamWriter) -> None:
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: multipart/x-mixed-replace; boundary=frame\r\n"
            b"Cache-Control: no-cache, no-store, must-revalidate\r\n"
            b"Connection: keep-alive\r\n"
            b"\r\n"
        )
        await writer.drain()

        last_seen_id = 0
        while True:
            jpeg_bytes, last_seen_id = await self.broadcaster.next_frame(last_seen_id)
            part = (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(jpeg_bytes)).encode() + b"\r\n"
                b"\r\n"
            )
            writer.write(part)
            writer.write(jpeg_bytes)
            writer.write(b"\r\n")
            try:
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                break

    async def _serve_snapshot(self, writer: asyncio.StreamWriter) -> None:
        jpeg_bytes, _ = await asyncio.wait_for(
            self.broadcaster.next_frame(0), timeout=5.0,
        )
        writer.write(_http_response(
            "200 OK", "image/jpeg", jpeg_bytes,
            extra_headers="Cache-Control: no-cache\r\n",
        ))
        await writer.drain()

    async def _serve_status(self, writer: asyncio.StreamWriter) -> None:
        status: dict = {}
        if self.processor is not None:
            status = self.processor.status()
        body = json.dumps(status).encode("utf-8")
        writer.write(_http_response(
            "200 OK", "application/json", body,
            extra_headers="Cache-Control: no-cache\r\n",
        ))
        await writer.drain()

    async def _serve_cameras(self, writer: asyncio.StreamWriter) -> None:
        cameras: list[dict] = []
        selected: str | None = None
        if self.processor is not None:
            cameras = self.processor.list_cameras()
            selected = self.processor.camera_id
        res = {
            "cameras": cameras,
            "selected_camera_id": selected,
        }
        body = json.dumps(res).encode("utf-8")
        writer.write(_http_response(
            "200 OK", "application/json", body,
            extra_headers="Cache-Control: no-cache\r\n",
        ))
        await writer.drain()

    async def _serve_select_camera(
        self, writer: asyncio.StreamWriter, query_params: dict[str, str], body: bytes
    ) -> None:
        camera_id = query_params.get("id") or query_params.get("camera_id")
        if not camera_id and body:
            try:
                data = json.loads(body.decode("utf-8"))
                if isinstance(data, dict):
                    camera_id = data.get("camera_id") or data.get("id")
            except Exception:
                pass

        if camera_id is not None and self.processor is not None:
            self.processor.set_camera(camera_id)
            res = {"ok": True, "camera_id": camera_id}
        elif camera_id is None:
            res = {"ok": False, "error": "missing 'id' or 'camera_id' parameter"}
        else:
            res = {"ok": False, "error": "camera processor unavailable"}

        resp_bytes = json.dumps(res).encode("utf-8")
        writer.write(_http_response(
            "200 OK" if res.get("ok") else "400 Bad Request",
            "application/json",
            resp_bytes,
            extra_headers="Cache-Control: no-cache\r\n",
        ))
        await writer.drain()

