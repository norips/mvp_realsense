"""Minimal RealSense RGB-D web server.

    GET /camera/rgbd  JSON payload for SVLR's RemoteRGBDFrameSource:
                      {ok, color_bgr_jpeg_b64, depth_npy_b64, intrinsics,
                       camera_name, timestamp_s}
    GET /debug        self-refreshing HTML page with the color frame, a colorized
                      depth frame, and depth/intrinsics stats
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
from fastapi.responses import HTMLResponse, JSONResponse


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


def colorize_depth(depth: np.ndarray) -> np.ndarray:
    """Depth in metres -> BGR heatmap, stretched over the 2-98th percentile."""
    valid = np.isfinite(depth) & (depth > 0)
    out = np.zeros(depth.shape + (3,), dtype=np.uint8)
    if valid.any():
        lo, hi = np.percentile(depth[valid], [2, 98])
        norm = np.clip((depth - lo) / max(float(hi - lo), 1e-6), 0.0, 1.0)
        out[valid] = cv.applyColorMap((norm * 255).astype(np.uint8), cv.COLORMAP_TURBO)[valid]
    return out


def jpeg_data_uri(image: np.ndarray, jpeg_quality: int) -> str:
    ok, jpg = cv.imencode(".jpg", image, [int(cv.IMWRITE_JPEG_QUALITY), jpeg_quality])
    if not ok:
        raise RuntimeError("Failed to encode debug image")
    return "data:image/jpeg;base64," + base64.b64encode(jpg.tobytes()).decode("ascii")


def debug_page(camera: RealSenseSource, jpeg_quality: int, refresh_s: float) -> str:
    color, depth = camera.read()
    valid = np.isfinite(depth) & (depth > 0)
    intr = camera.intrinsics

    rows = [
        ("color", f"{color.shape[1]}x{color.shape[0]} {color.dtype} BGR"),
        ("depth", f"{depth.shape[1]}x{depth.shape[0]} {depth.dtype} metres, aligned to color"),
        ("depth scale", f"{camera.depth_scale:.6f} m / unit"),
        ("valid depth", f"{100.0 * valid.mean():.1f}% of pixels"),
        ("depth range", f"{depth[valid].min():.3f} - {depth[valid].max():.3f} m" if valid.any() else "no valid pixels"),
        ("depth median", f"{np.median(depth[valid]):.3f} m" if valid.any() else "-"),
        ("centre pixel", f"{depth[depth.shape[0] // 2, depth.shape[1] // 2]:.3f} m"),
        ("intrinsics", f"fx={intr.fx:.2f} fy={intr.fy:.2f} ppx={intr.ppx:.2f} ppy={intr.ppy:.2f}"),
        ("distortion", f"{intr.model} {[round(float(c), 5) for c in intr.coeffs]}"),
        ("served at", time.strftime("%H:%M:%S")),
    ]
    table = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>RealSense debug</title>
<meta http-equiv="refresh" content="{refresh_s}">
<style>
 body {{ background:#111; color:#ddd; font:14px/1.5 monospace; margin:0; padding:16px; }}
 h1 {{ font-size:15px; font-weight:normal; color:#888; margin:0 0 12px; }}
 .imgs {{ display:flex; flex-wrap:wrap; gap:12px; }}
 figure {{ margin:0; }}
 figcaption {{ color:#888; padding:4px 0; }}
 img {{ max-width:100%; display:block; border:1px solid #333; }}
 table {{ border-collapse:collapse; margin-top:16px; }}
 th {{ text-align:left; color:#888; font-weight:normal; padding:2px 16px 2px 0; }}
 td {{ padding:2px 0; }}
</style></head><body>
<h1>RealSense debug &mdash; refreshing every {refresh_s}s</h1>
<div class="imgs">
  <figure><img src="{jpeg_data_uri(color, jpeg_quality)}"><figcaption>color (BGR)</figcaption></figure>
  <figure><img src="{jpeg_data_uri(colorize_depth(depth), jpeg_quality)}"><figcaption>depth (turbo, 2-98th pct)</figcaption></figure>
</div>
<table>{table}</table>
</body></html>"""


def create_app(camera: RealSenseSource, jpeg_quality: int, debug_refresh_s: float = 1.0) -> FastAPI:
    app = FastAPI(title="RealSense RGB-D webserver", version="0.2.0")

    @app.get("/camera/rgbd")
    async def camera_rgbd():
        try:
            return JSONResponse(rgbd_payload(camera, jpeg_quality))
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"{type(exc).__name__}: {exc}")

    @app.get("/debug", response_class=HTMLResponse)
    async def debug():
        try:
            return HTMLResponse(debug_page(camera, jpeg_quality, debug_refresh_s))
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
    parser.add_argument("--debug-refresh", type=float, default=float(os.environ.get("SVLR_DEBUG_REFRESH", "1.0")))
    args = parser.parse_args()
    args.jpeg_quality = int(np.clip(args.jpeg_quality, 50, 100))
    return args


def main() -> None:
    args = parse_args()
    print(f"[realsense_webserver] http://{args.host}:{args.port}/camera/rgbd")
    print(f"[realsense_webserver] http://{args.host}:{args.port}/debug")
    print(f"[realsense_webserver] {args.width}x{args.height}@{args.fps} serial={args.serial or 'auto'}")

    camera = RealSenseSource(args.width, args.height, args.fps, args.serial)
    try:
        app = create_app(camera, args.jpeg_quality, args.debug_refresh)
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        camera.stop()


if __name__ == "__main__":
    main()
