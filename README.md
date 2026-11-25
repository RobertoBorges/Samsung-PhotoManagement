# Frame Control Center

A small local web app for managing the photos on a Samsung **The Frame** TV:
upload, browse, display, favorite, delete, and adjust mats and Art Mode settings
from your browser.

It talks to the TV over your local network using the TV's Art Mode API
([`samsungtvws`](https://pypi.org/project/samsungtvws/)). No Developer Mode and no
cloud account are required.

> Unofficial project, not affiliated with Samsung. Developed and tested against a
> 2024 75" The Frame (QN75LS03DAFXZC). Art Mode API behavior varies by model and
> firmware.

## Features

- **Photo library:** search, filter, paginate, thumbnails, favorites, and confirmed
  single or bulk delete.
- **Upload:** JPEG, PNG, and HEIC/HEIF. Photos are converted locally and uploaded as
  a 3840x2160 JPEG, with EXIF orientation fixed and a choice of fit, fill (crop), or
  rotate. A preview is shown before anything is sent.
- **Mats and borders:** per-orientation mat selection, with options read from the
  TV and the change verified by reading the artwork back.
- **Art settings:** Art Mode on/off, brightness, color temperature, slideshow and
  auto-rotation.
- **Activity log:** a live backend log panel with stage timings. Tokens, photo
  contents, and filenames are never logged.
- **Collapsible sidebar UI**, light and dark themes.

Nothing changes on the TV until you click a control. Opening a page does not
contact it.

## How it works

```
Browser  <-->  Flask backend (this app)  <-->  Samsung TV (port 8002 + dynamic ports)
```

All TV communication happens in the Python backend. A browser cannot do this on
its own: photo upload and thumbnails use a raw TCP side-channel, and the TV's
REST API sends no CORS headers. The backend must therefore run on a machine that
can reach the TV's LAN address (your PC, a Raspberry Pi, a NAS). It cannot reach
the TV from a cloud host such as Azure unless you add a VPN or tunnel.

## Quick start

Requires Python 3.10 or newer.

**Windows (PowerShell)**

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\start.ps1
```

**Linux / macOS / Raspberry Pi (bash)**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

Open http://127.0.0.1:8765, click **Connect TV > Connect / pair**, and approve
**Frame Photo Manager** on the TV when prompted (allow up to 35 seconds).

### Set your TV's address

The TV address defaults to a value in `app.py`. Override it with an environment
variable:

```powershell
# Windows (PowerShell)
$env:FRAME_TV_HOST = "192.168.1.50"
.\start.ps1
```

```bash
# bash
FRAME_TV_HOST=192.168.1.50 .venv/bin/python app.py
```

Find the address on the TV under Settings > General > Network > Network Status.

## Docker

A Dockerfile and `compose.yaml` are included, including for Raspberry Pi (arm64).
See [docs/DOCKER.md](docs/DOCKER.md).

```powershell
# Windows (PowerShell)
$env:FRAME_TV_HOST = "192.168.1.50"
docker compose up --build -d
```

```bash
# bash
FRAME_TV_HOST=192.168.1.50 docker compose up --build -d
```

Then open http://127.0.0.1:8766.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `FRAME_TV_HOST` | value in `app.py` | IP address of the TV |
| `FRAME_BIND_HOST` | `127.0.0.1` | Interface the server listens on |
| `FRAME_ALLOW_CONTAINER_CLIENTS` | unset | Set to `1` only inside Docker so bridged requests are accepted |
| `FRAME_WEB_PORT` | `8766` | Host port for Docker Compose |

The pairing token is stored in `%LOCALAPPDATA%\FramePhotoManager\` on Windows,
`~/FramePhotoManager/` on Linux and macOS, or `/data/FramePhotoManager` in Docker.
Do not commit or share it.

## Security

- The app has **no user login**. It binds to loopback by default; keep it that way
  or put it behind your own authentication.
- Never expose it to the internet. Anyone who can reach it can change your photos.
- Requests are protected by trusted-host checks, a per-process CSRF token, origin
  checks, and a strict Content Security Policy.
- Errors shown in the UI and logs are sanitized.

## Tests

```powershell
# Windows (PowerShell)
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest tests.test_app tests.test_dashboard tests.test_thumbnails
.\.venv\Scripts\python.exe -m unittest tests.test_dashboard_browser
```

```bash
# bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest tests.test_app tests.test_dashboard tests.test_thumbnails
.venv/bin/python -m unittest tests.test_dashboard_browser
```

The browser suite is optional. It needs Playwright and Microsoft Edge (its path is
set for Windows in the test file), and runs
against a simulated TV. No test contacts a real TV.

## Project layout

| File | Purpose |
|------|---------|
| `app.py` | Flask routes, request guards, server entry point |
| `tv.py` | TV service layer: pairing, photos, mats, Art settings |
| `images.py` | Photo preparation, including HEIC/HEIF |
| `thumbnails.py` | Thumbnail fetching with a hard deadline |
| `activity.py` | Bounded in-memory activity log |
| `templates/index.html` | The whole UI |
| `Dockerfile`, `compose.yaml` | Container build and run |

## Known TV quirks

- Thumbnails are fetched with `get_thumbnail_list`; the single-item call can hang.
- Mat `none` may be rejected for portrait photos on some firmware. Mats cannot
  remove black borders baked into a photo; use the fill layout instead.
- HEIC is converted locally before upload because direct HEIC upload is not
  reliable.
- Changing a mat only shows when that artwork is displayed.

## License

Add a license of your choice before publishing (for example MIT).
