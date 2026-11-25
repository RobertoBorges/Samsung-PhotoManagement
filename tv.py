from contextlib import contextmanager, nullcontext
import os
from pathlib import Path
import re
from threading import Lock

from samsungtvws import exceptions
from samsungtvws.art import SamsungTVArt

from activity import ActivityLog
from thumbnails import THUMBNAIL_TIMEOUT, ThumbnailArt

PERSONAL_CATEGORY = "MY-C0002"
ART_READS = {
    "mode": "get_artmode", "current": "get_current",
    "settings": "get_artmode_settings", "mattes": "get_matte_list",
    "slideshow": "get_slideshow_status", "brightness": "get_brightness",
    "temperature": "get_color_temperature", "version": "get_api_version",
    "rotation": "get_auto_rotation_status",
}


def public_data(value):
    if isinstance(value, dict):
        return {
            key: public_data(item) for key, item in value.items()
            if key.lower() not in {"token", "key", "seckey", "conn_info", "request_id"}
        }
    if isinstance(value, list):
        return [public_data(item) for item in value]
    return value


class TVError(Exception):
    pass


class FrameTV:
    def __init__(self, host: str, data_dir: Path | None = None, activity=None):
        self.host = host
        self.data_dir = data_dir or Path(
            os.environ.get("LOCALAPPDATA", str(Path.home()))
        ) / "FramePhotoManager"
        self.lock = Lock()
        self.paired = False
        self.activity = activity if activity is not None else ActivityLog()

    @contextmanager
    def channel(self, pairing: bool = False, thumbnail: bool = False):
        label = "Art"
        if not pairing and not self.paired:
            raise TVError("Click Connect / pair before using these controls.")
        if not self.lock.acquire(blocking=False):
            raise TVError("Another TV operation is running. Wait for it to finish.")
        art = None
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            timeout = 35 if pairing else (5 if thumbnail else 15)
            self.activity.info(
                f"TV: opening encrypted {label} channel (response timeout {timeout}s)."
            )
            if pairing:
                self.activity.info(
                    f"TV: waiting for connection approval / {label} channel readiness. "
                    "Approve Frame Photo Manager on the TV if prompted."
                )
            connection_class = ThumbnailArt if thumbnail else SamsungTVArt
            art = connection_class(
                host=self.host,
                port=8002,
                token_file=str(self.data_dir / "tv-token.txt"),
                timeout=timeout,
                name="Frame Photo Manager",
            )
            deadline = art.request_deadline() if thumbnail else nullcontext()
            with deadline:
                art.open()
                self.activity.info(f"TV: {label} channel connected and ready.")
                yield art
        finally:
            try:
                if art is not None:
                    art.close()
                    self.activity.info(f"TV: {label} channel closed.")
            finally:
                self.lock.release()

    def connect(self):
        with self.channel(pairing=True):
            self.paired = True
            self.activity.info("TV: pairing / connection check succeeded.")
            return {"message": "Art channel paired. Photo controls are ready."}

    @staticmethod
    def content_list(art, category=None):
        items = art.available(category=category) if category else art.available()
        if not isinstance(items, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("content_id"), str)
            or not item["content_id"]
            for item in items
        ):
            raise TVError("The TV returned an unexpected artwork list.")
        return items

    def artworks(self):
        with self.channel() as art:
            self.activity.info("TV: requesting artwork list; waiting for response.")
            items = self.content_list(art)
            self.activity.info(f"TV: received {len(items)} artwork item(s).")
            return [
                {
                    "id": item["content_id"],
                    "name": item.get("title") or item.get("name") or item["content_id"],
                    "personal": item.get("category_id") == PERSONAL_CATEGORY,
                    "favorite": item.get("favorite", item.get("is_favorite")),
                    "matte": item.get("matte_id"),
                    "portrait_matte": item.get("portrait_matte_id"),
                    "width": item.get("width"),
                    "height": item.get("height"),
                }
                for item in items
            ]

    def upload(self, photo: bytes):
        with self.channel() as art:
            self.activity.info(
                f"TV: uploading prepared JPEG ({len(photo):,} bytes); "
                "waiting for upload acknowledgement. No automatic retries."
            )
            content_id = art.upload(
                photo, file_type="JPEG", matte="none", portrait_matte="none"
            )
            if not isinstance(content_id, str) or not content_id:
                raise TVError("The TV did not acknowledge the upload. Check its artwork list before retrying.")
            self.activity.info("TV: upload acknowledged. Artwork has not been selected.")
            return {"id": content_id, "message": "Upload acknowledged by the TV. Select it to display."}

    def select(self, content_id: str):
        with self.channel() as art:
            self.activity.info("TV: sending artwork selection.")
            art.select_image(content_id, show=True)
            self.activity.info("TV: checking current artwork to confirm selection.")
            current = art.get_current()
            if not isinstance(current, dict) or current.get("content_id") != content_id:
                raise TVError("Selection was sent, but the TV did not confirm this artwork as current. Check the TV before retrying.")
            self.activity.info("TV: artwork selection confirmed.")
            return {"message": "The TV confirms this artwork is selected."}

    def mode(self, enabled: bool):
        with self.channel() as art:
            expected = "on" if enabled else "off"
            self.activity.info(f"TV: requesting Art Mode {expected}.")
            art.set_artmode("on" if enabled else "off")
            self.activity.info("TV: reading Art Mode state to confirm the change.")
            state = art.get_artmode()
            if state != expected:
                raise TVError("The command was sent, but the TV did not confirm the requested Art Mode state. Check the TV before retrying.")
            self.activity.info(f"TV: Art Mode {expected} confirmed.")
            return {"message": f"The TV confirms Art Mode is {expected}."}

    def thumbnail(self, content_id):
        with self.channel(thumbnail=True) as art:
            self.activity.info(
                f"TV: requesting artwork via thumbnail-list API (total deadline {THUMBNAIL_TIMEOUT}s)."
            )
            result = art.get_thumbnail_list([content_id])
            if not isinstance(result, dict) or len(result) != 1:
                raise TVError("The TV did not return exactly one thumbnail for this artwork.")
            data = next(iter(result.values()))
            if not isinstance(data, (bytes, bytearray)) or not data:
                raise TVError("The TV did not return a thumbnail for this artwork.")
            if len(data) > 10 * 1024 * 1024:
                raise TVError("The TV returned an unexpectedly large thumbnail.")
            self.activity.info(f"TV: thumbnail received ({len(data):,} bytes).")
            return bytes(data)

    def delete(self, content_ids):
        with self.channel() as art:
            self.activity.info("TV: checking My Photos membership before deletion.")
            personal = {
                item["content_id"] for item in self.content_list(art, PERSONAL_CATEGORY)
                if item.get("category_id") == PERSONAL_CATEGORY
            }
            if not set(content_ids).issubset(personal):
                raise TVError(
                    "Deletion blocked: the TV did not identify every selected item as "
                    "a personal photo in My Photos. Refresh the library first."
                )
            self.activity.info(f"TV: deleting {len(content_ids)} personal photo(s); waiting for acknowledgement.")
            if art.delete_list(content_ids) is not True:
                raise TVError("The TV did not acknowledge deletion. Refresh the library before retrying.")
            self.activity.info("TV: deletion acknowledged.")
            return {"message": "The TV acknowledged deletion of the selected personal photos."}

    def favorite(self, content_id, enabled):
        with self.channel() as art:
            self.activity.info("TV: changing artwork favorite status.")
            result = art.set_favourite(content_id, "on" if enabled else "off")
            self.acknowledged(result)
            return {"message": "The TV acknowledged the favorite change. Refresh the library to see its reported state."}

    @staticmethod
    def acknowledged(result):
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("event"), str)
            or not result["event"]
            or result["event"] == "error"
        ):
            raise TVError("The TV did not acknowledge this setting change. Read its settings before retrying.")

    def art_read(self, setting):
        if setting not in ART_READS:
            raise ValueError("Unknown Art setting.")
        with self.channel() as art:
            self.activity.info(f"TV: reading Art {setting}; waiting for response.")
            result = getattr(art, ART_READS[setting])()
            if result is None or result == {}:
                raise TVError("The TV returned no usable data for this Art feature.")
            return public_data(result)

    def art_write(self, setting, value):
        with self.channel() as art:
            self.activity.info(f"TV: changing Art {setting}; waiting for acknowledgement.")
            if setting == "brightness":
                result = art.set_brightness(value)
            elif setting == "temperature":
                result = art.set_color_temperature(value)
            elif setting == "sensor":
                result = art.set_brightness_sensor_setting(value)
            elif setting == "motion_timer":
                result = art.set_motion_timer(value)
            elif setting == "motion_sensitivity":
                result = art.set_motion_sensitivity(value)
            else:
                raise ValueError("Unknown Art setting.")
            self.acknowledged(result)
            return {"message": "The TV acknowledged the setting request. Use Read to check its current value."}

    @staticmethod
    def matte_options(data):
        if not isinstance(data, dict) or not isinstance(data.get("matte_types"), list):
            raise TVError("The TV did not report usable mat types.")
        if not isinstance(data.get("matte_colors", []), list):
            raise TVError("The TV did not report usable mat colors.")
        colors = [
            item["color"] for item in data.get("matte_colors", [])
            if isinstance(item, dict) and isinstance(item.get("color"), str)
        ]
        options = {}
        for item in data["matte_types"]:
            if not isinstance(item, dict):
                raise TVError("The TV returned an unexpected mat type.")
            if isinstance(item.get("matte_id"), str):
                options[item["matte_id"]] = item["matte_id"].replace("_", " ").title()
            name = item.get("matte_type")
            if name == "none":
                options["none"] = "No mat"
            elif isinstance(name, str) and colors:
                for color in colors:
                    options[f"{name}_{color}"] = f"{name.title()} / {color.title()}"
        if not options:
            raise TVError("The TV did not return usable mat choices. No change was made.")
        return [{"id": key, "name": label} for key, label in options.items()]

    def matte_record(self, art, content_id):
        matches = [
            item for item in self.content_list(art) if item["content_id"] == content_id
        ]
        if not matches:
            raise TVError("The artwork is no longer in the TV library. Refresh the library.")
        return next(
            (item for item in matches if item.get("category_id") == PERSONAL_CATEGORY),
            matches[0],
        )

    @staticmethod
    def matte_metadata(item):
        return {
            "id": item["content_id"],
            "landscape": item.get("matte_id"),
            "portrait": item.get("portrait_matte_id"),
            "width": item.get("width"),
            "height": item.get("height"),
        }

    def matte_details(self, content_id):
        with self.channel() as art:
            self.activity.info("TV: reading the photo's saved landscape and portrait mats.")
            item = self.matte_record(art, content_id)
            options = self.matte_options(art.get_matte_list())
            return {**self.matte_metadata(item), "options": options}

    def matte(self, content_id, matte_id, orientation="landscape"):
        if orientation not in ("landscape", "portrait"):
            raise ValueError("Choose landscape or portrait TV orientation.")
        with self.channel() as art:
            self.activity.info("TV: checking mat choices and reading the artwork's saved mats.")
            before = self.matte_record(art, content_id)
            options = self.matte_options(art.get_matte_list())
            if matte_id not in {option["id"] for option in options}:
                raise ValueError("Choose a mat reported by the TV. Numeric IDs such as 0 are not valid mat styles.")
            landscape = matte_id if orientation == "landscape" else before.get("matte_id")
            portrait = matte_id if orientation == "portrait" else before.get("portrait_matte_id")
            if not isinstance(landscape, str) or not landscape:
                raise TVError("The TV did not report the landscape mat, so the other orientation cannot be preserved safely.")
            self.activity.info(f"TV: applying the {orientation} mat; preserving the other orientation.")
            try:
                # This firmware prioritizes the portrait field when both fields are supplied.
                kwargs = {"portrait_matte": portrait} if orientation == "portrait" else {}
                result = art.change_matte(content_id, landscape, **kwargs)
            except exceptions.ResponseError as exc:
                code = re.search(r"error number ([+-]?\d+|unknown)\b", str(exc))
                detail = f" (TV error {code.group(1)})" if code else ""
                if matte_id == "none":
                    raise TVError(
                        f"The TV rejected No mat for this artwork{detail}. Mat availability "
                        "can depend on the photo's size and shape. To fill your landscape TV "
                        "without borders, prepare the original as a 3840 x 2160 Crop to fill "
                        "upload. This creates a new photo; existing originals are not replaced."
                    ) from exc
                raise TVError(
                    f"The TV rejected this mat for this artwork{detail}. "
                    "A globally listed style may not be available for this photo's size or shape."
                ) from exc
            self.acknowledged(result)
            self.activity.info("TV: reading saved mat values back to verify the change.")
            after = self.matte_record(art, content_id)
            expected = {"matte_id": landscape}
            if portrait is not None:
                expected["portrait_matte_id"] = portrait
            if any(after.get(key) != value for key, value in expected.items()):
                raise TVError(
                    "The TV acknowledged the request but its saved mat values do not match. "
                    "Read the photo's mats again; the change is not confirmed."
                )
            self.activity.info(f"TV: {orientation} mat change verified in the artwork library.")
            return {
                "message": f"The TV confirms the saved {orientation} mat. Display this artwork to see it; baked-in image borders are unaffected.",
                "artwork": self.matte_metadata(after),
            }

    def slideshow(self, duration, shuffle, category, engine):
        with self.channel() as art:
            self.activity.info("TV: changing slideshow; waiting for acknowledgement.")
            method = art.set_slideshow_status if engine == "slideshow" else art.set_auto_rotation_status
            result = method(duration=duration, type=shuffle, category_id=category)
            self.acknowledged(result)
            return {"message": "The TV acknowledged the slideshow request. Read its status to check the configuration."}
