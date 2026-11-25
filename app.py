from collections import OrderedDict
from io import BytesIO
import logging
import os
import re
import secrets
from threading import Lock
import time

from flask import Flask, abort, g, jsonify, render_template, request, send_file
from samsungtvws import exceptions
from websocket import WebSocketException
from werkzeug.exceptions import HTTPException

from images import prepare_photo
from activity import ActivityLog
from thumbnails import THUMBNAIL_TIMEOUT, ThumbnailTimeout
from tv import ART_READS, FrameTV, TVError

HOST = "192.168.3.217"
PORT = 8765
PHOTO_TTL = 3600
MAX_PHOTOS = 8
OPERATIONS = {
    "connect": "Connect / pair",
    "artworks": "Load artwork",
    "prepare": "Prepare photo",
    "upload": "Upload photo",
    "select": "Display artwork",
    "mode": "Change Art Mode",
    "thumbnail": "Load thumbnail",
    "delete_artwork": "Delete photos",
    "favorite": "Change favorite",
    "art_read": "Read Art setting",
    "art_write": "Change Art setting",
    "matte": "Change artwork mat",
    "matte_details": "Read artwork mats",
    "slideshow": "Configure slideshow",
}


def create_app(tv=None):
    app = Flask(__name__)
    app.config.update(
        MAX_CONTENT_LENGTH=25 * 1024 * 1024,
        TRUSTED_HOSTS=["127.0.0.1", "localhost"],
        ALLOW_CONTAINER_CLIENTS=os.environ.get("FRAME_ALLOW_CONTAINER_CLIENTS") == "1",
    )
    csrf = secrets.token_urlsafe(32)
    activity_log = ActivityLog()
    app.extensions["activity_log"] = activity_log
    tv_host = os.environ.get("FRAME_TV_HOST", HOST)
    frame = tv if tv is not None else FrameTV(tv_host, activity=activity_log)
    photos = OrderedDict()
    photo_lock = Lock()

    def expire_photos():
        now = time.monotonic()
        for key in list(photos):
            if now - photos[key][0] > PHOTO_TTL:
                del photos[key]

    def get_photo(photo_id):
        with photo_lock:
            expire_photos()
            if photo_id not in photos:
                abort(404, "Preview expired or was replaced. Prepare the photo again.")
            return photos[photo_id][1]

    @app.before_request
    def local_only():
        if not app.config["ALLOW_CONTAINER_CLIENTS"] and request.remote_addr not in ("127.0.0.1", "::1"):
            abort(403, "This app is accessible only from this computer.")
        if request.method not in ("GET", "HEAD", "OPTIONS") or request.endpoint == "activity":
            supplied = request.headers.get("X-Frame-Token", "")
            if not secrets.compare_digest(supplied, csrf):
                abort(403, "Invalid request token. Reload the page.")
            origin = request.headers.get("Origin")
            if origin is not None and origin != request.host_url.rstrip("/"):
                abort(403, "Cross-origin requests are not allowed.")
        if request.endpoint in OPERATIONS and request.method == "POST":
            g.operation = OPERATIONS[request.endpoint]
            g.started_at = time.monotonic()
            activity_log.info(f"{g.operation}: started.")

    @app.after_request
    def headers(response):
        if hasattr(g, "started_at"):
            elapsed = time.monotonic() - g.started_at
            message = (
                f"{g.operation}: {'completed' if response.status_code < 400 else 'failed'} "
                f"(HTTP {response.status_code}, {elapsed:.2f}s)."
            )
            if response.status_code < 400:
                activity_log.info(message)
            else:
                activity_log.warning(message)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'nonce-" + csrf
            + "'; style-src 'self' 'nonce-" + csrf
            + "'; img-src 'self' blob:; connect-src 'self'; "
            "frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        )
        return response

    @app.errorhandler(HTTPException)
    def http_error(exc):
        if hasattr(g, "operation"):
            activity_log.warning(f"{g.operation}: request rejected (HTTP {exc.code}).")
        return jsonify(error=exc.description), exc.code

    @app.errorhandler(ValueError)
    def input_error(exc):
        activity_log.warning("Input rejected: invalid photo, options, or response data.")
        return jsonify(error=str(exc)), 400

    @app.errorhandler(TVError)
    def tv_error(exc):
        activity_log.warning(f"TV operation was not confirmed: {exc}")
        return jsonify(error=str(exc)), 502

    @app.errorhandler(ThumbnailTimeout)
    def thumbnail_timeout(_exc):
        message = (
            f"The TV did not return a thumbnail within {THUMBNAIL_TIMEOUT} seconds. "
            "Thumbnail loading stopped; other controls are available. Retry a single image."
        )
        activity_log.error(message)
        return jsonify(error=message), 504

    @app.errorhandler(exceptions.UnauthorizedError)
    def unauthorized(_exc):
        activity_log.warning("TV rejected authorization. Allow Frame Photo Manager on the TV, then connect again.")
        return jsonify(error="The TV denied pairing. Allow Frame Photo Manager on the TV; check its external-device connection permissions, then click Connect again."), 502

    def connection_error(exc):
        detail = type(exc).__name__
        if isinstance(exc, exceptions.ResponseError):
            code = re.search(r"error number ([+-]?\d+|unknown)\b", str(exc))
            if code:
                detail += f", TV error number {code.group(1)}"
        activity_log.error(
            f"TV communication failed ({detail}). Check TV power, pairing approval, "
            "and firmware Art API support. Check the TV before retrying."
        )
        return jsonify(error="The TV did not confirm this operation. It may be offline, awaiting pairing approval, or not support this Art API request. Check the TV before retrying; uploads may have completed even if a response was lost."), 502

    for error_type in (
        exceptions.ConnectionFailure,
        exceptions.ResponseError,
        exceptions.HttpApiError,
        WebSocketException,
        OSError,
    ):
        app.register_error_handler(error_type, connection_error)

    @app.errorhandler(Exception)
    def unexpected(exc):
        activity_log.error(f"Unexpected application error ({type(exc).__name__}).")
        return jsonify(error="An unexpected application error occurred. Check the terminal. The TV operation, if any, may have completed; check the TV before retrying."), 500

    def json_body():
        body = request.get_json()
        if not isinstance(body, dict):
            abort(400, "Expected a JSON object.")
        return body

    def identifier(body, key="id"):
        value = body.get(key)
        if not isinstance(value, str) or not 1 <= len(value) <= 256:
            abort(400, "Choose a valid item.")
        return value

    def confirmed(body):
        if body.get("confirmed") is not True:
            abort(400, "This action requires explicit confirmation.")

    def integer(value, minimum, maximum):
        return type(value) is int and minimum <= value <= maximum

    @app.get("/")
    def index():
        return render_template("index.html", csrf=csrf, tv_host=tv_host)

    @app.get("/api/activity")
    def activity():
        try:
            after = int(request.args.get("after", "0"))
        except ValueError:
            abort(400, "Log cursor must be a nonnegative whole number.")
        if after < 0:
            abort(400, "Log cursor must be a nonnegative whole number.")
        return jsonify(activity_log.snapshot(after, request.args.get("instance")))

    @app.post("/api/connect")
    def connect():
        return jsonify(frame.connect())

    @app.post("/api/artworks")
    def artworks():
        return jsonify(items=frame.artworks())

    @app.post("/api/photos")
    def prepare():
        upload = request.files.get("photo")
        if upload is None:
            abort(400, "Choose a photo first.")
        try:
            rotation = int(request.form.get("rotation", "0"))
        except ValueError:
            abort(400, "Rotation must be a whole number.")
        data = upload.read()
        layout = request.form.get("layout", "fit")
        activity_log.info(
            f"Photo: processing {len(data):,} bytes; correcting camera orientation "
            "and applying rotation / screen layout."
        )
        photo = prepare_photo(data, rotation, layout)
        activity_log.info(f"Photo: 3840 x 2160 JPEG preview ready ({len(photo):,} bytes). Nothing uploaded.")
        photo_id = secrets.token_urlsafe(24)
        with photo_lock:
            expire_photos()
            while len(photos) >= MAX_PHOTOS:
                photos.popitem(last=False)
            photos[photo_id] = (time.monotonic(), photo)
        return jsonify(id=photo_id, preview=f"/api/photos/{photo_id}", width=3840, height=2160)

    @app.get("/api/photos/<photo_id>")
    def preview(photo_id):
        return send_file(BytesIO(get_photo(photo_id)), mimetype="image/jpeg")

    @app.post("/api/upload")
    def upload():
        photo_id = json_body().get("id")
        if not isinstance(photo_id, str):
            abort(400, "Prepare a photo first.")
        return jsonify(frame.upload(get_photo(photo_id)))

    @app.post("/api/select")
    def select():
        content_id = json_body().get("id")
        if not isinstance(content_id, str) or not 1 <= len(content_id) <= 256:
            abort(400, "Choose an artwork.")
        return jsonify(frame.select(content_id))

    @app.post("/api/art-mode")
    def mode():
        enabled = json_body().get("enabled")
        if not isinstance(enabled, bool):
            abort(400, "Art Mode must be true or false.")
        return jsonify(frame.mode(enabled))

    @app.post("/api/thumbnail")
    def thumbnail():
        data = frame.thumbnail(identifier(json_body()))
        # Decode and re-encode so TV-provided bytes cannot become active content.
        from PIL import Image, UnidentifiedImageError
        import warnings

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(data)) as image:
                    image.thumbnail((640, 360))
                    output = BytesIO()
                    image.convert("RGB").save(output, "JPEG")
        except (OSError, UnidentifiedImageError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
            raise TVError("The TV returned an unreadable thumbnail.") from exc
        return send_file(BytesIO(output.getvalue()), mimetype="image/jpeg")

    @app.post("/api/artworks/delete")
    def delete_artwork():
        body = json_body()
        confirmed(body)
        ids = body.get("ids")
        if (
            not isinstance(ids, list) or not 1 <= len(ids) <= 50
            or any(not isinstance(item, str) or not 1 <= len(item) <= 256 for item in ids)
            or len(set(ids)) != len(ids)
        ):
            abort(400, "Choose between 1 and 50 distinct personal photos.")
        return jsonify(frame.delete(ids))

    @app.post("/api/artworks/favorite")
    def favorite():
        body = json_body()
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            abort(400, "Favorite status must be true or false.")
        return jsonify(frame.favorite(identifier(body), enabled))

    @app.post("/api/art/read")
    def art_read():
        setting = json_body().get("setting")
        if not isinstance(setting, str) or setting not in ART_READS:
            abort(400, "Unknown Art setting.")
        return jsonify(value=frame.art_read(setting))

    @app.post("/api/art/write")
    def art_write():
        body = json_body()
        setting, value = body.get("setting"), body.get("value")
        valid = (
            (setting == "brightness" and integer(value, 0, 10))
            or (setting == "temperature" and integer(value, -5, 5))
            or (setting == "sensor" and isinstance(value, bool))
            or (setting == "motion_timer" and isinstance(value, str) and value in {"off", "5", "15", "30", "60", "120", "240"})
            or (setting == "motion_sensitivity" and isinstance(value, str) and value in {"1", "2", "3"})
        )
        if not valid:
            abort(400, "Invalid Art setting or value.")
        return jsonify(frame.art_write(setting, value))

    @app.post("/api/artworks/matte")
    def matte():
        body = json_body()
        matte_id = identifier(body, "matte")
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", matte_id):
            abort(400, "Choose a valid mat ID.")
        orientation = body.get("orientation", "landscape")
        if orientation not in ("landscape", "portrait"):
            abort(400, "Choose landscape or portrait TV orientation.")
        return jsonify(frame.matte(identifier(body), matte_id, orientation))

    @app.post("/api/artworks/matte/read")
    def matte_details():
        return jsonify(frame.matte_details(identifier(json_body())))

    @app.post("/api/art/slideshow")
    def slideshow():
        body = json_body()
        duration, shuffle = body.get("duration"), body.get("shuffle")
        category, engine = body.get("category"), body.get("engine")
        if (
            not integer(duration, 0, 1440)
            or not isinstance(shuffle, bool)
            or category not in ("MY-C0002", "MY-C0004")
            or engine not in ("slideshow", "rotation")
        ):
            abort(400, "Choose a duration of 0-1440 minutes, a valid category, order, and API.")
        return jsonify(frame.slideshow(duration, shuffle, category, engine))

    activity_log.info("Backend ready. No TV connection is made until Connect / pair is clicked.")
    return app


if __name__ == "__main__":
    from waitress import serve

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # The TV library logs pairing tokens at INFO level.
    logging.getLogger("samsungtvws").setLevel(logging.WARNING)
    bind_host = os.environ.get("FRAME_BIND_HOST", "127.0.0.1")
    print(f"Frame Photo Manager: http://{bind_host}:{PORT}", flush=True)
    print("No TV connection is made until you use the app. Press Ctrl+C to stop.", flush=True)
    serve(create_app(), host=bind_host, port=PORT, threads=4)
