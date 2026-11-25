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
| `FRAME_ACCESS_PASSWORD` | unset | Enables HTTP Basic auth (user `frame`), minimum 8 characters; 5 failures per minute are rate limited |
| `FRAME_OPEN_LAN` | unset | Set to `1` to allow LAN access with **no login and no Host restriction**. Anyone on your network can manage the photos |
| `FRAME_PORT` | `8765` | Port the app listens on (inside Docker the macvlan override uses `80`) |
| `FRAME_PUBLISH_HOST` | `127.0.0.1` | Docker Compose only: interface to publish the port on (`0.0.0.0` for LAN) |
| `FRAME_WEB_PORT` | `8766` | Host port for Docker Compose |
| `FRAME_LAN_IP`, `FRAME_LAN_PARENT`, `FRAME_LAN_SUBNET`, `FRAME_LAN_GATEWAY` | `192.168.3.50`, `eth0`, `192.168.3.0/24`, `192.168.3.1` | macvlan override only (see below) |

Put these in a `.env` file next to `compose.yaml` (ignored by Git) so they survive
`sudo` and new shells.

## Run on Linux or a Raspberry Pi with containers

Pick the access mode that fits your network. All examples assume the repo is in
`~/Samsung-PhotoManagement` and the TV is at `192.168.1.50`.

### 1. Local only (default, safest)

```bash
cd ~/Samsung-PhotoManagement
echo 'FRAME_TV_HOST=192.168.1.50' > .env
docker compose up --build -d
```

Open http://127.0.0.1:8766 on the Pi, or from another machine use an SSH tunnel:
`ssh -L 8766:127.0.0.1:8766 user@raspberrypi`.

### 2. Home network, no password, no host restrictions

For a trusted home network. Anyone on it can change your TV photos.

```bash
cd ~/Samsung-PhotoManagement
cat > .env <<'EOF'
FRAME_TV_HOST=192.168.1.50
FRAME_PUBLISH_HOST=0.0.0.0
FRAME_OPEN_LAN=1
EOF
docker compose up --build -d
docker ps   # PORTS must show 0.0.0.0:8766->8765/tcp
```

Open `http://<pi-ip>:8766` from any device on the network (plain `http`, not `https`).

### 3. Home network with a password

Same as above but replace `FRAME_OPEN_LAN=1` with `FRAME_ACCESS_PASSWORD=<8+ characters>`.
Sign in as user `frame`. Failed sign-ins are rate limited. The password travels over
plain HTTP and is visible through `docker inspect`, so use it only on a trusted network.

### 4. Its own IP address and port 80 (macvlan)

Gives the container its own LAN address, for example `http://192.168.3.50`, separate
from the Pi's. Requires a wired Pi (Wi-Fi adapters generally cannot do macvlan). The Pi
itself cannot reach the container at that IP; other devices can.

```bash
cd ~/Samsung-PhotoManagement
cat > .env <<'EOF'
FRAME_TV_HOST=192.168.1.50
FRAME_LAN_IP=192.168.1.60
FRAME_LAN_PARENT=eth0
FRAME_LAN_SUBNET=192.168.1.0/24
FRAME_LAN_GATEWAY=192.168.1.1
EOF
docker compose down
docker compose -f compose.yaml -f compose.macvlan.yaml up --build -d
```

Choose an unused address, ideally outside your router's DHCP range. This mode uses
`FRAME_OPEN_LAN=1` and port 80.

For a friendly name, add a DNS record in Pi-hole (`dns.hosts`):
`192.168.1.60 theframe.lan`, then browse to `http://theframe.lan/`. Avoid `.local`
(Windows treats it as mDNS) and turn off browser secure DNS if the name does not resolve.

### Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `ERR_CONNECTION_REFUSED` | The port is published on `127.0.0.1`. Set `FRAME_PUBLISH_HOST=0.0.0.0` in `.env` and recreate: `docker compose up -d --force-recreate` |
| `Host '...' is not trusted` | Neither `FRAME_OPEN_LAN=1` nor a password reached the container. Check `docker compose config` |
| Page will not load | Use `http://`, not `https://` |
| Variables ignored | Put them in `.env`; `sudo` drops exported variables |
| `docker compose` cannot find `.env` values | Run it from the folder containing `compose.yaml` |

The pairing token is stored in `%LOCALAPPDATA%\FramePhotoManager\` on Windows,
`~/FramePhotoManager/` on Linux and macOS, or `/data/FramePhotoManager` in Docker.
Do not commit or share it.

## Security

- By default the app has **no login** and only accepts requests from this computer
  (`localhost`/`127.0.0.1`). Keep it that way, or opt in to LAN access with
  `FRAME_OPEN_LAN=1` (no login) or `FRAME_ACCESS_PASSWORD` (HTTP Basic login).
- Never expose it to the internet. Anyone who can reach it can change your photos.
  For remote access use a VPN such as Tailscale, not port forwarding.
- Requests are protected by a per-process CSRF token, origin checks, and a strict
  Content Security Policy, in every mode.
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
| `compose.macvlan.yaml` | Optional override: container gets its own LAN IP on port 80 |

## Known TV quirks

- Thumbnails are fetched with `get_thumbnail_list`; the single-item call can hang.
- Mat `none` may be rejected for portrait photos on some firmware. Mats cannot
  remove black borders baked into a photo; use the fill layout instead.
- HEIC is converted locally before upload because direct HEIC upload is not
  reliable.
- Changing a mat only shows when that artwork is displayed.

## License

Add a license of your choice before publishing (for example MIT).
