# Frame Control Center: Docker

Requires Docker Desktop running with Linux containers. Run commands from this
directory in PowerShell.

## Build and run

```powershell
docker compose up --build -d
```

Open http://127.0.0.1:8766. The container uses port 8766 on your computer so it
does not conflict with the native app on port 8765.

Click **Connect / pair** and approve the app on the TV if prompted. The container
has its own saved pairing token; it does not
copy your Windows tokens into the image. No TV commands run at startup.

HEIC / HEIF conversion is included and runs locally in the container. Preview
preparation never uploads or selects a photo automatically.

## Configuration

To change the TV address or local browser port, set these before starting:

```powershell
$env:FRAME_TV_HOST = "192.168.3.217"
$env:FRAME_WEB_PORT = "8766"
docker compose up --build -d
```

These settings affect the current PowerShell process. The defaults are already
set in `compose.yaml`.

The image listens on port 8765 internally. `FRAME_BIND_HOST=0.0.0.0` and
`FRAME_ALLOW_CONTAINER_CLIENTS=1` let Docker's bridge forward browser requests.
Without these image settings, the native app still binds to and accepts only
loopback clients.

**Keep the published host address `127.0.0.1`.** Do not publish this app on your
LAN or the internet: it has no user login. Host validation, request tokens, and
cross-origin protections remain enabled inside the container.

## Logs, stopping, and updates

```powershell
docker compose logs -f frame
docker compose down
docker compose up --build -d
```

The live activity panel is also available in the browser. Refresh the browser
and reconnect the TV after restarting the container.

Pairing tokens persist in the `frame-data` volume under
`/data/FramePhotoManager`. Normal `docker compose down` preserves this volume.
Do not add `--volumes` unless you intend to erase saved pairing state. Previews
are temporary, remain in memory, and disappear on restart. Original photo files
are neither mounted nor copied into the image.

## Without Compose

```powershell
docker build -t frame-control-center:local .
docker run -d --name frame-control-center --restart unless-stopped --init --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true -p 127.0.0.1:8766:8765 -e FRAME_TV_HOST=192.168.3.217 -v frame-control-data:/data frame-control-center:local
docker logs -f frame-control-center
```

This example uses a separate volume from Compose.

## Package mirrors

The default build uses PyPI over verified HTTPS. If your network requires an
approved package mirror, pass its index URL as a temporary BuildKit secret
instead of storing it in the image or a Docker build argument:

```powershell
$env:FRAME_BUILD_PIP_INDEX_URL = "https://your-approved-mirror.example/simple/"
docker build --secret id=pip_index_url,env=FRAME_BUILD_PIP_INDEX_URL -t frame-control-center:local .
Remove-Item Env:\FRAME_BUILD_PIP_INDEX_URL
docker compose up -d --no-build
```

Use the mirror required by your network; do not disable TLS certificate
verification. Supply the secret again when rebuilding changed dependencies.

## TV network access

The Docker host must be able to reach the TV's LAN address. Allow Docker Desktop
through relevant firewall or VPN rules. Control uses TV port 8002;
photo transfers and thumbnails can also use dynamic ports supplied by the TV.
No inbound TV port publishing or host networking is required. An unreachable or
unsupported operation is reported as an error, not silently retried.
