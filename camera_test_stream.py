"""
test_view_stream.py

Minimal standalone viewer -- connects to the bridge's /video WebSocket and
displays frames with cv2.imshow. Use this to sanity-check the stream before
wiring up the full PySide6 StreamWorker.

Usage:
    python3 test_view_stream.py --host localhost --port 8765
    python3 test_view_stream.py --host raspberrypi.local --port 8765

Press 'q' in the image window to quit.
"""

import argparse
import asyncio

import cv2
import numpy as np
import websockets


async def view_stream(host: str, port: int):
    uri = f"ws://{host}:{port}/video"
    print(f"Connecting to {uri} ...")
    async with websockets.connect(uri, max_size=None) as ws:
        print("Connected. Press 'q' in the window to quit.")
        async for message in ws:
            frame = cv2.imdecode(np.frombuffer(message, dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is not None:
                cv2.imshow("Stream preview", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
    cv2.destroyAllWindows()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    asyncio.run(view_stream(args.host, args.port))


if __name__ == "__main__":
    main()