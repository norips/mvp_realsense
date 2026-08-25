# khang_realsense

Minimal RGB-D web server for an Intel RealSense camera. Exposes a single endpoint,
`GET /camera/rgbd`, in the payload format consumed by SVLR's `RemoteRGBDFrameSource`.

Extracted from the camera half of `crisp_panda_webserver.py` — no robot, no gripper,
no ROS. Just [`realsense_webserver.py`](realsense_webserver.py) on top of `pyrealsense2`.

## Prerequisites

Install pixi:
```bash
curl -fsSL https://pixi.sh/install.sh | bash
```

### udev rules (required)

Without RealSense udev rules the USB node stays `root:root`, the process cannot claim
the device, and `pipeline.start()` fails with:

```
RuntimeError: failed to set power state
```

Install the rules once, per machine:

```bash
sudo curl -o /etc/udev/rules.d/99-realsense-libusb.rules \
  https://raw.githubusercontent.com/IntelRealSense/librealsense/master/config/99-realsense-libusb.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Then **unplug and replug the camera** — the rules apply when the device is enumerated,
not retroactively. Verify:

```bash
# node should now be group-writable by plugdev, not crw-rw-r-- root root
lsusb | grep RealSense                      # note the bus/device numbers
ls -l /dev/bus/usb/<bus>/<device>

pixi run python -c "import pyrealsense2 as rs; print([d.get_info(rs.camera_info.name) for d in rs.context().devices])"
```

The last command should print your camera, e.g. `['Intel RealSense D405']`.

## Run

```bash
pixi run server
# or, with options
pixi run python realsense_webserver.py --width 848 --height 480 --fps 30
```

Then:

```bash
curl -s http://localhost:8000/camera/rgbd | head -c 200
```

## Options

Every flag also reads an environment variable, so the server drops into an existing
SVLR deployment without changing the command line.

| Flag | Env var | Default | Description |
|------|---------|---------|-------------|
| `--host` | `SVLR_PANDA_SERVER_HOST` | `0.0.0.0` | Bind address |
| `--port` | `SVLR_PANDA_SERVER_PORT` | `8000` | Bind port |
| `--width` | `SVLR_CAMERA_WIDTH` | `640` | Stream width |
| `--height` | `SVLR_CAMERA_HEIGHT` | `480` | Stream height |
| `--fps` | `SVLR_CAMERA_FPS` | `30` | Stream frame rate |
| `--serial` | `REALSENSE_SERIAL` | *(auto)* | Pick a specific camera by serial |
| `--jpeg-quality` | `SVLR_JPEG_QUALITY` | `90` | Color JPEG quality, clamped to 50–100 |

Not every resolution/fps combination is valid for every model — a rejected combination
fails at `pipeline.start()`. Check what your camera supports with `pixi run rs-enumerate-devices` (the tool ships
with the `pyrealsense2` package in this environment).

## Endpoint

### `GET /camera/rgbd`

`200` with a JSON body:

| Field | Type | Description |
|-------|------|-------------|
| `ok` | bool | Always `true` on success |
| `color_bgr_jpeg_b64` | str | Base64 JPEG, **BGR** channel order |
| `depth_npy_b64` | str | Base64 `.npy`, float32 `HxW`, **metres** |
| `intrinsics` | obj | `width, height, fx, fy, ppx, ppy, model, coeffs` — color stream |
| `camera_name` | str | `"realsense"` |
| `timestamp_s` | float | Server wall-clock time of the response |

`503` with `{"detail": "..."}` when no frame is available yet — normal for the first
moments after startup, while the pipeline warms up.

Depth is **aligned to the color stream**, so `depth[y, x]` corresponds to `color[y, x]`
and the color intrinsics deproject both.

### Client

Point SVLR's `RemoteRGBDFrameSource` at the server; it decodes this payload directly:

```python
from controller.camera import RemoteRGBDFrameSource

camera = RemoteRGBDFrameSource(url="http://<host>:8000/camera/rgbd")
frame = camera.read()          # CameraFrame: color_bgr, depth_image_m, intrinsics
```

Or decode it by hand:

```python
import base64, io, cv2, numpy as np, requests

data = requests.get("http://localhost:8000/camera/rgbd", timeout=10).json()
color = cv2.imdecode(np.frombuffer(base64.b64decode(data["color_bgr_jpeg_b64"]), np.uint8), cv2.IMREAD_COLOR)
depth = np.load(io.BytesIO(base64.b64decode(data["depth_npy_b64"])))   # float32 metres
```

## How it works

A single daemon thread calls `wait_for_frames()` continuously and keeps only the most
recent aligned frame under a lock. Requests read that snapshot, so an infrequent poller
gets the current view rather than a stale frame from the pipeline queue, and a fast
poller never blocks on the sensor.

Encoding happens per request, not per frame — an idle server does no JPEG work.

## Troubleshooting

| Symptom | Cause |
|---------|-------|
| `failed to set power state` | Missing udev rules — see above. Replug after installing. |
| `No device connected` | Check `lsusb`; USB 2.0 ports and hubs can also drop high-bandwidth streams. |
| Persistent `503` | Camera opened but delivering no frames; the thread prints its read errors to stdout. |
| `Couldn't resolve requests` at startup | The `--width/--height/--fps` combination is unsupported by this model. |
