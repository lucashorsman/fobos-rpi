"""
main.py

Entry point for the RPi5 bridge. Starts:
  - the vmbpy camera capture loop (in a background thread, since vmbpy's
    streaming API is blocking/callback-based, not asyncio-native)
  - a single websockets server exposing:
      ws://<pi-host>:8765/video    -> binary JPEG frames, Pi -> host only
      ws://<pi-host>:8765/control  -> JSON commands, host -> Pi, JSON replies back

Run with: python3 main.py [--port 8765] [--camera-id <id>] [--laser-pin 17]
"""

import argparse
import asyncio
import json
import logging
import threading

import websockets

from camera_stream_server import CameraProcessor, FrameBroadcaster
from laser_control import LaserController
from web_preview import WebPreviewServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("rpi_bridge.main")

# Default explicit camera ID if not passed via command line (--camera-id)
DEFAULT_CAMERA_ID: str | None = "DEV_000F3103CB74"


async def video_handler(websocket, broadcaster: FrameBroadcaster):
    logger.info("Video client connected: %s", websocket.remote_address)
    last_seen_id = 0
    try:
        while True:
            jpeg_bytes, last_seen_id = await broadcaster.next_frame(last_seen_id)
            await websocket.send(jpeg_bytes)
    except websockets.exceptions.ConnectionClosed:
        logger.info("Video client disconnected: %s", websocket.remote_address)


async def control_handler(websocket, laser: LaserController, processor: "CameraProcessor"):
    logger.info("Control client connected: %s", websocket.remote_address)
    try:
        async for message in websocket:
            try:
                cmd = json.loads(message)
            except json.JSONDecodeError:
                await websocket.send(json.dumps({"ok": False, "error": "invalid JSON"}))
                continue

            action = cmd.get("cmd")
            reply = {"ok": True, "cmd": action}

            if action == "laser_on":
                laser.on(cmd.get("intensity", 1.0))
            elif action == "laser_off":
                laser.off()
            elif action == "laser_set_intensity":
                laser.set_intensity(float(cmd.get("value", 0.0)))
            elif action == "set_exposure":
                exposure_us = cmd.get("value")
                if exposure_us is None:
                    reply = {"ok": False, "error": "missing 'value' (exposure_us)"}
                else:
                    processor.set_exposure(float(exposure_us))
            elif action == "set_gain":
                gain_db = cmd.get("value")
                if gain_db is None:
                    reply = {"ok": False, "error": "missing 'value' (gain_db)"}
                else:
                    processor.set_gain(float(gain_db))
            elif action in ("get_cameras", "list_cameras"):
                reply["cameras"] = processor.list_cameras()
                reply["selected_camera_id"] = processor.camera_id
            elif action in ("set_camera", "select_camera"):
                camera_id = cmd.get("camera_id") if "camera_id" in cmd else cmd.get("value")
                if camera_id is None and "id" in cmd:
                    camera_id = cmd.get("id")
                processor.set_camera(camera_id)
                reply["camera_id"] = camera_id
            elif action == "camera_status":
                reply["camera"] = processor.status()
            elif action == "status":
                # StreamWorker reads reply["status"] to fire laser_status_received.
                # Keep everything under that key; include camera state alongside laser.
                reply["status"] = {**laser.status(), "camera": processor.status()}
            elif action == "ping":
                reply["cmd"] = "pong"
            else:
                reply = {"ok": False, "error": f"unknown cmd: {action}"}

            await websocket.send(json.dumps(reply))
    except websockets.exceptions.ConnectionClosed:
        logger.info("Control client disconnected: %s", websocket.remote_address)


def make_router(broadcaster: FrameBroadcaster, laser: LaserController, processor: "CameraProcessor"):
    async def router(websocket):
        path = websocket.request.path if hasattr(websocket, "request") else websocket.path
        if path == "/video":
            await video_handler(websocket, broadcaster)
        elif path == "/control":
            await control_handler(websocket, laser, processor)
        else:
            await websocket.close(code=1008, reason=f"unknown path {path}")
    return router


async def async_main(args):
    loop = asyncio.get_running_loop()
    broadcaster = FrameBroadcaster()
    laser = LaserController(gpio_pin=args.laser_pin)
    processor = CameraProcessor(broadcaster, loop, camera_id=args.camera_id)

    capture_thread = threading.Thread(target=processor.start, daemon=True)
    capture_thread.start()

    # Start the HTTP web preview server (MJPEG stream for browsers)
    web = WebPreviewServer(broadcaster, processor=processor, laser=laser, port=args.web_port)
    await web.start()

    router = make_router(broadcaster, laser, processor)
    async with websockets.serve(router, "0.0.0.0", args.port, max_size=None):
        logger.info("Bridge listening on port %d (/video, /control)", args.port)
        logger.info("Web preview at http://0.0.0.0:%d/", args.web_port)
        try:
            await asyncio.Future()  # run forever
        finally:
            await web.stop()
            processor.stop()
            laser.close()


def main():
    parser = argparse.ArgumentParser(description="FOBOS RPi5 bridge")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--web-port", type=int, default=8080,
                        help="HTTP port for the browser camera preview (default: 8080)")
    parser.add_argument("--camera-id", type=str, default=DEFAULT_CAMERA_ID,
                        help=f"Target Allied Vision camera ID (default: {DEFAULT_CAMERA_ID})")
    parser.add_argument("--laser-pin", type=int, default=17)
    args = parser.parse_args()

    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        logger.info("Shutting down")


if __name__ == "__main__":
    main()