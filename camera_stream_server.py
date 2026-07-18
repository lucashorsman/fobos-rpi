"""
camera_stream_server.py

Runs on the RPi5. Owns the Allied Vision camera via vmbpy (this is the piece
that has to live next to the hardware, since GigE Vision doesn't survive the
VPN hop). Captures frames, runs whatever OpenCV processing you want done
locally (fiducial detection, undistortion, overlay -- whatever doesn't need
to round-trip to the host), JPEG-encodes, and broadcasts over a WebSocket to
however many GUI clients are connected.

Design choice: "latest frame wins". Each connected client has its own
asyncio task that just grabs whatever the newest encoded frame is and sends
it, then loops. If the host is slow (bad VPN link, GUI thread busy), we never
build up a backlog of stale frames -- we just skip frames, which is what you
want for a live positioner view instead of a growing queue of old images.
"""

import asyncio
import logging
import time
from typing import Optional

import cv2
import numpy as np

try:
    import vmbpy
    from vmbpy.error import VmbTimeout
    VMBPY_AVAILABLE = True
except ImportError:
    VMBPY_AVAILABLE = False

logger = logging.getLogger("camera_stream_server")


class FrameBroadcaster:
    """
    Holds exactly one "latest frame" (already JPEG-encoded) and notifies
    waiting consumers via an asyncio.Event. This is the "latest frame wins"
    piece -- deliberately not a queue.
    """

    def __init__(self):
        self._jpeg_bytes: Optional[bytes] = None
        self._frame_id: int = 0
        self._event = asyncio.Event()

    def publish(self, jpeg_bytes: bytes) -> None:
        self._jpeg_bytes = jpeg_bytes
        self._frame_id += 1
        self._event.set()

    async def next_frame(self, last_seen_id: int) -> tuple[bytes, int]:
        """Block until a frame newer than last_seen_id is available."""
        while self._frame_id == last_seen_id or self._jpeg_bytes is None:
            self._event.clear()
            await self._event.wait()
        return self._jpeg_bytes, self._frame_id


class CameraProcessor:
    """
    Wraps vmbpy camera acquisition + your OpenCV processing hook, feeding
    encoded frames into a FrameBroadcaster. Runs its own frame-grab loop in
    a background thread (vmbpy's frame callback is synchronous/threaded),
    and hands finished JPEGs back to the asyncio world via
    loop.call_soon_threadsafe.
    """

    def __init__(
        self,
        broadcaster: FrameBroadcaster,
        loop: asyncio.AbstractEventLoop,
        jpeg_quality: int = 80,
        camera_id: Optional[str] = None,
    ):
        self.broadcaster = broadcaster
        self.loop = loop
        self.jpeg_quality = jpeg_quality
        self.camera_id = camera_id
        self._cam = None
        self._running = False
        self._frame_count = 0
        self._last_fps_log = time.monotonic()

    def process_frame(self, frame: np.ndarray) -> np.ndarray:
        """
        Hook for local OpenCV work -- fiducial/ArUco detection, distortion
        correction, overlay drawing, whatever you want done on the Pi rather
        than shipped raw to the host. Pass-through for now.
        """
        return frame

    def _vmbpy_frame_handler(self, cam, stream, frame):
        try:
            if frame.get_status() == vmbpy.FrameStatus.Complete:
                img = frame.as_opencv_image()  # BGR ndarray
                processed = self.process_frame(img)
                ok, jpeg = cv2.imencode(
                    ".jpg", processed, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality]
                )
                if ok:
                    self.loop.call_soon_threadsafe(self.broadcaster.publish, jpeg.tobytes())
                    self._frame_count += 1
                    now = time.monotonic()
                    if now - self._last_fps_log > 5.0:
                        fps = self._frame_count / (now - self._last_fps_log)
                        logger.info("Capture FPS: %.1f", fps)
                        self._frame_count = 0
                        self._last_fps_log = now
        except Exception:
            logger.exception("Error in frame handler")
        finally:
            cam.queue_frame(frame)

    def start(self) -> None:
        if not VMBPY_AVAILABLE:
            logger.warning("vmbpy not available -- starting synthetic test-pattern feed instead")
            asyncio.run_coroutine_threadsafe(self._synthetic_feed_loop(), self.loop)
            return

        with vmbpy.VmbSystem.get_instance() as vmb:
            cams = vmb.get_all_cameras()
            if not cams:
                raise RuntimeError("No Allied Vision cameras detected")
            self._cam = cams[0] if self.camera_id is None else next(
                c for c in cams if c.get_id() == self.camera_id
            )
            with self._cam:
                try:
                    self._cam.set_pixel_format(vmbpy.PixelFormat.Bgr8)
                except Exception:
                    self._cam.set_pixel_format(vmbpy.PixelFormat.Mono8)

                self._running = True
                logger.info("Capture loop started from camera %s", self._cam.get_id())
                while self._running:
                    try:
                        frame = self._cam.get_frame(timeout_ms=2000)
                        img = frame.as_opencv_image()
                        processed = self.process_frame(img)
                        ok, jpeg = cv2.imencode(
                            ".jpg", processed, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality]
                        )
                        if ok:
                            self.loop.call_soon_threadsafe(self.broadcaster.publish, jpeg.tobytes())
                            self._frame_count += 1
                            now = time.monotonic()
                            if now - self._last_fps_log > 5.0:
                                fps = self._frame_count / (now - self._last_fps_log)
                                logger.info("Capture FPS: %.1f", fps)
                                self._frame_count = 0
                                self._last_fps_log = now
                    except VmbTimeout:
                        logger.warning("Camera frame wait timed out; retrying capture loop")
                        continue

    def stop(self) -> None:
        self._running = False

    async def _synthetic_feed_loop(self) -> None:
        """Dev-machine fallback so you can test the WebSocket plumbing without hardware."""
        frame_num = 0
        while True:
            img = np.zeros((480, 640, 3), dtype=np.uint8)
            cv2.putText(
                img, f"NO CAMERA - test frame {frame_num}", (30, 240),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 0), 2,
            )
            ok, jpeg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])
            if ok:
                self.broadcaster.publish(jpeg.tobytes())
            frame_num += 1
            await asyncio.sleep(1 / 15)
