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
        self._capturing = False
        self._error: Optional[str] = None
        self._last_frame_time: Optional[float] = None
        self._frame_count = 0
        self._last_fps_log = time.monotonic()
        # Cached values so status() can report them even between frames.
        self._exposure_us: Optional[float] = None
        self._gain_db: Optional[float] = None

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

    # ------------------------------------------------------------------
    # Camera settings control (thread-safe: called from asyncio thread
    # while the capture loop runs in the same thread inside start()).
    # ------------------------------------------------------------------

    def set_exposure(self, exposure_us: float) -> None:
        """Set camera exposure time in microseconds.

        Mirrors VimbaWorker.set_exposure: tries the modern GenICam feature
        name first (ExposureTime), then the legacy alias (ExposureTimeAbs).
        """
        self._exposure_us = float(exposure_us)
        if self._cam is None:
            logger.warning("set_exposure called but camera is not open (cached for when open)")
            return
        try:
            self._cam.ExposureTime.set(float(exposure_us))
            logger.info("Exposure set to %.1f us", exposure_us)
        except Exception:
            try:
                self._cam.ExposureTimeAbs.set(float(exposure_us))
                logger.info("Exposure set to %.1f us (via ExposureTimeAbs)", exposure_us)
            except Exception as e:
                logger.error("Failed to set exposure: %s", e)

    def set_gain(self, gain_db: float) -> None:
        """Set camera gain in dB.

        Mirrors VimbaWorker.set_gain: tries Gain first, then GainRaw.
        """
        self._gain_db = float(gain_db)
        if self._cam is None:
            logger.warning("set_gain called but camera is not open (cached for when open)")
            return
        try:
            self._cam.Gain.set(float(gain_db))
            logger.info("Gain set to %.2f dB", gain_db)
        except Exception:
            try:
                self._cam.GainRaw.set(float(gain_db))
                logger.info("Gain set to %.2f dB (via GainRaw)", gain_db)
            except Exception as e:
                logger.error("Failed to set gain: %s", e)

    def status(self) -> dict:
        """Return current camera settings as a dict for the control channel."""
        return {
            "exposure_us": self._exposure_us,
            "gain_db": self._gain_db,
            "camera_id": self._cam.get_id() if self._cam is not None else self.camera_id,
            "running": self._running,
            "capturing": self._capturing,
            "error": self._error,
            "last_frame_time": self._last_frame_time,
            "frame_count": self._frame_count,
        }

    # ------------------------------------------------------------------
    # Capture loop
    # ------------------------------------------------------------------

    def start(self) -> None:
        if not VMBPY_AVAILABLE:
            logger.warning("vmbpy not available -- starting synthetic test-pattern feed instead")
            asyncio.run_coroutine_threadsafe(self._synthetic_feed_loop(), self.loop)
            return

        self._running = True
        while self._running:
            self._capturing = False
            try:
                with vmbpy.VmbSystem.get_instance() as vmb:
                    cams = vmb.get_all_cameras()
                    if not cams:
                        self._error = "No Allied Vision cameras detected"
                        logger.warning(self._error)
                        time.sleep(2.0)
                        continue
                    self._cam = cams[0] if self.camera_id is None else next(
                        (c for c in cams if c.get_id() == self.camera_id), None
                    )
                    if self._cam is None:
                        self._error = f"Camera ID {self.camera_id} not found"
                        logger.warning(self._error)
                        time.sleep(2.0)
                        continue

                    try:
                        with self._cam:
                            self._error = None
                            try:
                                self._cam.set_pixel_format(vmbpy.PixelFormat.Bgr8)
                            except Exception:
                                self._cam.set_pixel_format(vmbpy.PixelFormat.Mono8)

                            # Disable auto modes so manually-set values take effect
                            # immediately, matching the VimbaWorker approach in fobos-gui.
                            for feat_name in ("ExposureAuto", "GainAuto"):
                                try:
                                    getattr(self._cam, feat_name).set("Off")
                                    logger.info("%s disabled", feat_name)
                                except Exception:
                                    pass

                            if self._exposure_us is not None:
                                self.set_exposure(self._exposure_us)
                            if self._gain_db is not None:
                                self.set_gain(self._gain_db)

                            self._capturing = True
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
                                        self._last_frame_time = time.time()
                                        now = time.monotonic()
                                        if now - self._last_fps_log > 5.0:
                                            fps = self._frame_count / (now - self._last_fps_log)
                                            logger.info("Capture FPS: %.1f", fps)
                                            self._frame_count = 0
                                            self._last_fps_log = now
                                except VmbTimeout:
                                    logger.warning("Camera frame wait timed out; retrying capture loop")
                                    continue
                                except Exception as exc:
                                    self._error = f"Frame capture error: {exc}"
                                    logger.error("Error in camera capture loop: %s", exc)
                                    break
                    except Exception as exc:
                        # Catch exceptions entering/exiting 'with self._cam:', including
                        # vmbpy.error.VmbCameraError: <VmbError.Already: -33> on exit
                        self._error = f"Camera session error: {exc}"
                        logger.warning("Camera session error (caught cleanly): %s", exc)
            except Exception as exc:
                self._error = f"VmbSystem error: {exc}"
                logger.error("VmbSystem error: %s", exc)
            finally:
                self._capturing = False
                self._cam = None

            if self._running:
                logger.info("Retrying camera open/capture loop in 2.0s...")
                time.sleep(2.0)

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
