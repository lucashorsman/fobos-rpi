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
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from camera_stream_server import CameraProcessor, FrameBroadcaster
    from laser_control import LaserController

logger = logging.getLogger("web_preview")


# ---------------------------------------------------------------------------
# HTML page loader
# ---------------------------------------------------------------------------

INDEX_HTML_PATH = Path(__file__).parent / "index.html"


def _load_html_page() -> bytes:
    """Load HTML preview page template from index.html."""
    if INDEX_HTML_PATH.exists():
        return INDEX_HTML_PATH.read_bytes()
    logger.error("index.html template not found at %s", INDEX_HTML_PATH)
    return b"<!DOCTYPE html><html><body><h1>Error: index.html missing</h1></body></html>"


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
        laser: "LaserController | None" = None,
        port: int = 8080,
    ):
        self.broadcaster = broadcaster
        self.processor = processor
        self.laser = laser
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
            elif path in ("/laser", "/api/laser", "/laser/control"):
                await self._serve_laser(writer, query_params, body)
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
        body = _load_html_page()
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
        cam_status: dict = {}
        if self.processor is not None:
            cam_status = self.processor.status()
        laser_status: dict = {}
        if self.laser is not None:
            laser_status = self.laser.status()

        status = {
            **cam_status,
            "camera": cam_status,
            "laser": laser_status,
        }
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

    async def _serve_laser(
        self, writer: asyncio.StreamWriter, query_params: dict[str, str], body: bytes
    ) -> None:
        if self.laser is None:
            resp = json.dumps({"ok": False, "error": "Laser controller unavailable"}).encode("utf-8")
            writer.write(_http_response("503 Service Unavailable", "application/json", resp))
            await writer.drain()
            return

        action = query_params.get("action") or query_params.get("cmd")
        intensity_val = query_params.get("intensity") or query_params.get("value")

        if body:
            try:
                data = json.loads(body.decode("utf-8"))
                if isinstance(data, dict):
                    action = data.get("action") or data.get("cmd") or action
                    if "intensity" in data:
                        intensity_val = data["intensity"]
                    elif "value" in data:
                        intensity_val = data["value"]
            except Exception:
                pass

        action = str(action).lower() if action else "status"

        if action == "on":
            val = float(intensity_val) if intensity_val is not None else 1.0
            self.laser.on(val)
        elif action == "off":
            self.laser.off()
        elif action == "toggle":
            if self.laser.is_on:
                self.laser.off()
            else:
                val = float(intensity_val) if intensity_val is not None else 1.0
                self.laser.on(val)
        elif action in ("set_intensity", "intensity"):
            if intensity_val is None:
                resp = json.dumps({"ok": False, "error": "missing intensity value"}).encode("utf-8")
                writer.write(_http_response("400 Bad Request", "application/json", resp))
                await writer.drain()
                return
            self.laser.set_intensity(float(intensity_val))
        elif action == "status":
            pass
        else:
            resp = json.dumps({"ok": False, "error": f"unknown action: {action}"}).encode("utf-8")
            writer.write(_http_response("400 Bad Request", "application/json", resp))
            await writer.drain()
            return

        res = {"ok": True, "laser": self.laser.status()}
        resp_bytes = json.dumps(res).encode("utf-8")
        writer.write(_http_response(
            "200 OK", "application/json", resp_bytes,
            extra_headers="Cache-Control: no-cache\r\n",
        ))
        await writer.drain()

