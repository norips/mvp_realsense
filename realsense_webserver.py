"""Minimal RGB-D web server exposing a single GET /camera/rgbd endpoint.

Payload matches what SVLR's RemoteRGBDFrameSource expects:
    {ok, color_bgr_jpeg_b64, depth_npy_b64, intrinsics, camera_name, timestamp_s}
"""

from __future__ import annotations

import argparse
import base64
import os
import threading
import time
from io import BytesIO

import cv2 as cv
import numpy as np
import pyrealsense2 as rs
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse


class RealSenseSource:
    """Depth-aligned-to-color RealSense stream, latest frame kept by a thread."""

    def __init__(self, width: int, height: int, fps: int, serial: str = ""):
        self.pipeline = rs.pipeline()
        config = rs.config()
        if serial:
            config.enable_device(str(serial))
        config.enable_stream(rs.stream.color, width, height, rs.format.bgr8, fps)
        config.enable_stream(rs.stream.depth, width, height, rs.format.z16, fps)
        profile = self.pipeline.start(config)

        self.align = rs.align(rs.stream.color)
        self.depth_scale = float(profile.get_device().first_depth_sensor().get_depth_scale())
        self.intrinsics = profile.get_stream(rs.stream.color).as_video_stream_profile().get_intrinsics()

        self.lock = threading.Lock()
        self.latest: tuple[np.ndarray, np.ndarray] | None = None
        self.stopped = False
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        while not self.stopped:
            try:
                frames = self.align.process(self.pipeline.wait_for_frames())
                color_frame = frames.get_color_frame()
                depth_frame = frames.get_depth_frame()
                if not color_frame or not depth_frame:
                    continue
                color = np.asanyarray(color_frame.get_data()).copy()
                depth = np.asanyarray(depth_frame.get_data()).astype(np.float32) * self.depth_scale
                with self.lock:
                    self.latest = (color, depth)
            except Exception as exc:
                print(f"[realsense_webserver] read failed: {type(exc).__name__}: {exc}", flush=True)
                time.sleep(0.1)

    def read(self) -> tuple[np.ndarray, np.ndarray]:
        with self.lock:
            frame = self.latest
        if frame is None:
            raise RuntimeError("No camera frame available yet")
        return frame

    def stop(self) -> None:
        self.stopped = True
        self.thread.join(timeout=1.0)
        self.pipeline.stop()


def rgbd_payload(camera: RealSenseSource, jpeg_quality: int) -> dict:
    color, depth = camera.read()

    ok, jpg = cv.imencode(".jpg", color, [int(cv.IMWRITE_JPEG_QUALITY), jpeg_quality])
    if not ok:
        raise RuntimeError("Failed to encode RGB frame")

    buffer = BytesIO()
    np.save(buffer, depth)

    intr = camera.intrinsics
    return {
        "ok": True,
        "color_bgr_jpeg_b64": base64.b64encode(jpg.tobytes()).decode("ascii"),
        "depth_npy_b64": base64.b64encode(buffer.getvalue()).decode("ascii"),
        "intrinsics": {
            "width": int(intr.width),
            "height": int(intr.height),
            "fx": float(intr.fx),
            "fy": float(intr.fy),
            "ppx": float(intr.ppx),
            "ppy": float(intr.ppy),
            "model": str(intr.model),
            "coeffs": [float(c) for c in intr.coeffs],
        },
        "camera_name": "realsense",
        "timestamp_s": time.time(),
    }


def create_app(camera: RealSenseSource, jpeg_quality: int) -> FastAPI:
    app = FastAPI(title="RealSense RGB-D webserver", version="0.1.0")

    @app.get("/camera/rgbd")
    async def camera_rgbd():
        try:
            return JSONResponse(rgbd_payload(camera, jpeg_quality))
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"{type(exc).__name__}: {exc}")

    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve RealSense RGB-D frames over GET /camera/rgbd")
    parser.add_argument("--host", default=os.environ.get("SVLR_PANDA_SERVER_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SVLR_PANDA_SERVER_PORT", "8000")))
    parser.add_argument("--width", type=int, default=int(os.environ.get("SVLR_CAMERA_WIDTH", "640")))
    parser.add_argument("--height", type=int, default=int(os.environ.get("SVLR_CAMERA_HEIGHT", "480")))
    parser.add_argument("--fps", type=int, default=int(os.environ.get("SVLR_CAMERA_FPS", "30")))
    parser.add_argument("--serial", default=os.environ.get("REALSENSE_SERIAL", ""))
    parser.add_argument("--jpeg-quality", type=int, default=int(os.environ.get("SVLR_JPEG_QUALITY", "90")))
    args = parser.parse_args()
    args.jpeg_quality = int(np.clip(args.jpeg_quality, 50, 100))
    return args


def main() -> None:
    args = parse_args()
    print(f"[realsense_webserver] http://{args.host}:{args.port}/camera/rgbd")
    print(f"[realsense_webserver] {args.width}x{args.height}@{args.fps} serial={args.serial or 'auto'}")

    camera = RealSenseSource(args.width, args.height, args.fps, args.serial)
    try:
        uvicorn.run(create_app(camera, args.jpeg_quality), host=args.host, port=args.port)
    finally:
        camera.stop()


if __name__ == "__main__":
    main()
