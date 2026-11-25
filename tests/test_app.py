from io import BytesIO
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import patch

from PIL import Image

from app import create_app
from activity import ActivityLog
from samsungtvws import exceptions
from images import prepare_photo
from tv import FrameTV, TVError


def image_bytes(size=(80, 40), color="red", exif=None, image_format="JPEG"):
    output = BytesIO()
    image = Image.new("RGB", size, color)
    image.save(output, image_format, **({"exif": exif} if exif else {}))
    return output.getvalue()


class PhotoTests(unittest.TestCase):
    def test_heic_orientation_fit_and_manual_rotation_are_converted_to_jpeg(self):
        exif = Image.Exif()
        exif[274] = 6
        data = image_bytes(exif=exif.tobytes(), image_format="HEIF")
        for rotation, bordered in ((0, True), (90, False)):
            with self.subTest(rotation=rotation):
                result = Image.open(BytesIO(prepare_photo(data, rotation, "fit")))
                self.assertEqual(result.format, "JPEG")
                self.assertEqual(result.size, (3840, 2160))
                self.assertEqual(result.getpixel((500, 1080))[0] < 20, bordered)
                self.assertNotIn(274, result.getexif())

    def test_heif_uses_primary_photo_and_crop_fills_without_borders(self):
        output = BytesIO()
        Image.new("RGB", (40, 80), "red").save(
            output, "HEIF", save_all=True,
            append_images=[Image.new("RGB", (40, 80), "blue")], primary_index=1,
        )
        result = Image.open(BytesIO(prepare_photo(output.getvalue(), 0, "fill")))
        for point in ((0, 0), (1920, 1080), (3839, 2159)):
            self.assertGreater(result.getpixel(point)[2], 230)
            self.assertLess(result.getpixel(point)[0], 20)

    def test_heic_enforces_pixel_limit_and_rejects_truncated_file(self):
        data = image_bytes(image_format="HEIF")
        with patch("images.Image.MAX_IMAGE_PIXELS", 1000):
            with self.assertRaisesRegex(ValueError, "Cannot read this photo"):
                prepare_photo(data, 0, "fit")
        with self.assertRaisesRegex(ValueError, "Cannot read this photo"):
            prepare_photo(data[:64], 0, "fit")

    def test_fit_preserves_entire_portrait_and_has_borders(self):
        result = Image.open(BytesIO(prepare_photo(image_bytes((40, 80)), 0, "fit")))
        self.assertEqual(result.size, (3840, 2160))
        self.assertEqual(result.getpixel((0, 1000)), (0, 0, 0))
        self.assertGreater(result.getpixel((1920, 1080))[0], 240)

    def test_exif_orientation_is_baked_in_and_manual_rotation_is_applied(self):
        exif = Image.Exif()
        exif[274] = 6
        source = image_bytes(exif=exif)
        portrait = Image.open(BytesIO(prepare_photo(source, 0, "fit")))
        landscape = Image.open(BytesIO(prepare_photo(source, 90, "fit")))
        self.assertEqual(portrait.getpixel((500, 1080)), (0, 0, 0))
        self.assertGreater(landscape.getpixel((500, 1080))[0], 240)
        self.assertNotIn(274, portrait.getexif())

    def test_fill_has_no_borders(self):
        result = Image.open(BytesIO(prepare_photo(image_bytes((40, 80)), 0, "fill")))
        self.assertGreater(result.getpixel((0, 0))[0], 240)

    def test_rejects_bad_image_and_invalid_options(self):
        for data, rotation, layout in (
            (b"not an image", 0, "fit"),
            (image_bytes(), 45, "fit"),
            (image_bytes(), 0, "stretch"),
        ):
            with self.subTest(rotation=rotation, layout=layout):
                with self.assertRaises(ValueError):
                    prepare_photo(data, rotation, layout)


class FakeTV:
    def __init__(self):
        self.calls = []

    def connect(self):
        self.calls.append("connect")
        return {"message": "Paired"}

    def upload(self, photo):
        self.calls.append(("upload", photo))
        return {"id": "MY_1", "message": "Uploaded"}

    def artworks(self):
        return [{"id": "MY_1", "name": "Photo"}]

    def select(self, content_id):
        self.calls.append(("select", content_id))
        return {"message": "Selected"}

    def mode(self, enabled):
        self.calls.append(("mode", enabled))
        return {"message": "Changed"}


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tv = FakeTV()
        self.app = create_app(self.tv)
        self.client = self.app.test_client()
        html = self.client.get("/").get_data(as_text=True)
        self.token = re.search(r'name="frame-token" content="([^"]+)"', html).group(1)
        self.headers = {"X-Frame-Token": self.token}

    def post(self, url, **kwargs):
        return self.client.post(url, headers=self.headers, **kwargs)

    def test_startup_and_home_do_not_contact_tv(self):
        self.assertEqual(self.tv.calls, [])
        self.assertIn("frame-ancestors 'none'", self.client.get("/").headers["Content-Security-Policy"])

    def test_rejects_cross_site_and_nonlocal_requests(self):
        self.assertEqual(self.client.post("/api/connect").status_code, 403)
        self.assertEqual(self.client.post("/api/connect", headers={**self.headers, "Origin": "http://evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/", base_url="http://evil.example").status_code, 400)
        self.assertEqual(self.client.get("/", environ_base={"REMOTE_ADDR": "192.168.3.20"}).status_code, 403)
        self.assertEqual(self.tv.calls, [])

    def test_container_bridge_optin_preserves_token_origin_and_host_protections(self):
        with patch.dict("os.environ", {"FRAME_ALLOW_CONTAINER_CLIENTS": "1"}):
            app = create_app(self.tv)
        client = app.test_client()
        bridge = {"REMOTE_ADDR": "172.20.0.1"}
        response = client.get("/", environ_base=bridge)
        self.assertEqual(response.status_code, 200)
        token = re.search(
            r'name="frame-token" content="([^"]+)"', response.get_data(as_text=True),
        ).group(1)
        headers = {"X-Frame-Token": token}
        self.assertEqual(client.get("/api/activity", environ_base=bridge).status_code, 403)
        self.assertEqual(client.get(
            "/api/activity", headers=headers, environ_base=bridge,
        ).status_code, 200)
        self.assertEqual(client.post(
            "/api/connect", headers={**headers, "Origin": "http://evil.example"},
            environ_base=bridge,
        ).status_code, 403)
        self.assertEqual(client.get(
            "/", base_url="http://evil.example", environ_base=bridge,
        ).status_code, 400)
        response = client.post("/api/photos", headers=headers, environ_base=bridge, data={
            "photo": (BytesIO(image_bytes()), "photo.jpg"),
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.tv.calls, [])

    def test_tv_host_environment_configures_connection_and_display(self):
        with patch.dict("os.environ", {"FRAME_TV_HOST": "192.0.2.10"}):
            with patch("app.FrameTV") as factory:
                app = create_app()
        factory.assert_called_once_with(
            "192.0.2.10", activity=app.extensions["activity_log"],
        )
        self.assertIn("192.0.2.10", app.test_client().get("/").get_data(as_text=True))
        factory.return_value.connect.assert_not_called()

    def test_prepare_preview_then_explicit_upload_and_select(self):
        response = self.post("/api/photos", data={
            "photo": (BytesIO(image_bytes()), "family.jpg"),
            "layout": "fit", "rotation": "0",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.tv.calls, [])
        data = response.get_json()
        preview = self.client.get(data["preview"])
        self.assertEqual(preview.mimetype, "image/jpeg")
        self.assertEqual(Image.open(BytesIO(preview.data)).size, (3840, 2160))
        self.assertEqual(self.post("/api/upload", json={"id": data["id"]}).get_json()["id"], "MY_1")
        self.assertEqual(len(self.tv.calls), 1)
        self.post("/api/select", json={"id": "MY_1"})
        self.assertEqual(self.tv.calls[-1], ("select", "MY_1"))

    def test_heic_preview_is_local_and_explicit_upload_sends_jpeg(self):
        source = image_bytes(image_format="HEIF")
        response = self.post("/api/photos", data={
            "photo": (BytesIO(source), "camera.HEIC", "image/heic"), "layout": "fill",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.tv.calls, [])
        prepared = response.get_json()
        preview = self.client.get(prepared["preview"])
        self.assertEqual(preview.mimetype, "image/jpeg")
        self.assertEqual(Image.open(BytesIO(preview.data)).size, (3840, 2160))
        uploaded = self.post("/api/upload", json={"id": prepared["id"]})
        self.assertEqual(uploaded.status_code, 200)
        self.assertEqual(len(self.tv.calls), 1)
        self.assertEqual(self.tv.calls[0][0], "upload")
        self.assertEqual(Image.open(BytesIO(self.tv.calls[0][1])).format, "JPEG")

    def test_invalid_upload_and_mode_do_not_contact_tv(self):
        self.assertEqual(self.post("/api/upload", json={"id": "missing"}).status_code, 404)
        self.assertEqual(self.post("/api/art-mode", json={"enabled": "false"}).status_code, 400)
        self.assertEqual(self.post("/api/select", json=[]).status_code, 400)
        self.assertEqual(self.post("/api/photos", data={"photo": (BytesIO(b"bad"), "bad.jpg")}).status_code, 400)
        self.assertEqual(self.tv.calls, [])

    def test_preview_expiration(self):
        response = self.post("/api/photos", data={"photo": (BytesIO(image_bytes()), "a.jpg")})
        with patch("app.time.monotonic", return_value=10**15):
            self.assertEqual(self.client.get(response.get_json()["preview"]).status_code, 404)

    def test_tv_failure_is_an_error_not_success(self):
        with patch.object(self.tv, "connect", side_effect=TimeoutError()):
            response = self.post("/api/connect")
        self.assertEqual(response.status_code, 502)
        self.assertIn("did not confirm", response.get_json()["error"])

    def test_activity_requires_token_and_polling_does_not_log_itself(self):
        self.assertEqual(self.client.get("/api/activity").status_code, 403)
        first = self.client.get("/api/activity", headers=self.headers).get_json()
        second = self.client.get(
            "/api/activity",
            query_string={"after": first["cursor"], "instance": first["instance"]},
            headers=self.headers,
        ).get_json()
        self.assertEqual(second["entries"], [])
        self.assertFalse(second["reset"])
        self.assertEqual(self.tv.calls, [])
        self.assertEqual(self.client.get("/api/activity?after=-1", headers=self.headers).status_code, 400)

    def test_operation_progress_and_completion_are_logged(self):
        with self.assertLogs("frame.activity", level="INFO") as terminal:
            response = self.post("/api/connect")
        self.assertEqual(response.status_code, 200)
        messages = [
            entry["message"] for entry in
            self.client.get("/api/activity", headers=self.headers).get_json()["entries"]
        ]
        self.assertIn("Connect / pair: started.", messages)
        self.assertTrue(any("Connect / pair: completed (HTTP 200," in msg for msg in messages))
        self.assertIn("Connect / pair: started.", "\n".join(terminal.output))

    def test_logs_do_not_expose_request_headers_or_raw_tv_errors(self):
        secret = "private-token-that-must-not-be-logged"
        with patch.object(
            self.tv, "connect",
            side_effect=exceptions.ResponseError(
                f"send_image request failed with error number -1; token={secret}"
            ),
        ):
            self.client.post(
                "/api/connect",
                headers={**self.headers, "X-Private-Test": secret},
            )
        data = self.client.get("/api/activity", headers=self.headers).get_json()
        serialized = str(data)
        self.assertNotIn(secret, serialized)
        self.assertNotIn(self.token, serialized)
        self.assertIn("TV error number -1", serialized)
        self.assertIn("Connect / pair: failed", serialized)

    def test_activity_is_available_while_an_operation_is_waiting(self):
        entered, release = Event(), Event()
        responses = []

        def wait_for_tv():
            entered.set()
            release.wait(5)
            return {"message": "Connected"}

        def connect():
            with self.app.test_client() as client:
                responses.append(client.post("/api/connect", headers=self.headers))

        with patch.object(self.tv, "connect", side_effect=wait_for_tv):
            thread = Thread(target=connect)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                snapshot = self.client.get("/api/activity", headers=self.headers).get_json()
                messages = [entry["message"] for entry in snapshot["entries"]]
                self.assertIn("Connect / pair: started.", messages)
                self.assertFalse(any("Connect / pair: completed" in msg for msg in messages))
            finally:
                release.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(responses[0].status_code, 200)


class ActivityTests(unittest.TestCase):
    def test_bounded_incremental_logs_and_server_reset(self):
        activity = ActivityLog(capacity=2)
        activity.info("First")
        initial = activity.snapshot()
        activity.warning("Second")
        activity.error("Third")
        incremental = activity.snapshot(initial["cursor"], initial["instance"])
        self.assertEqual([entry["message"] for entry in incremental["entries"]], ["Second", "Third"])
        self.assertEqual([entry["level"] for entry in incremental["entries"]], ["WARNING", "ERROR"])
        self.assertFalse(incremental["reset"])
        reset = activity.snapshot(999, "previous-server")
        self.assertTrue(reset["reset"])
        self.assertEqual(len(reset["entries"]), 2)


class TVTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.frame = FrameTV("192.168.3.217", Path(self.directory.name))
        self.frame.paired = True

    @patch("tv.SamsungTVArt")
    def test_controls_cannot_initiate_pairing(self, factory):
        self.frame.paired = False
        with self.assertRaises(TVError):
            self.frame.artworks()
        factory.assert_not_called()

    @patch("tv.SamsungTVArt")
    def test_upload_uses_prepared_bytes_and_does_not_select(self, factory):
        frame = self.frame
        art = factory.return_value
        art.upload.return_value = "MY_1"
        self.assertEqual(frame.upload(b"jpeg")["id"], "MY_1")
        art.upload.assert_called_once_with(
            b"jpeg", file_type="JPEG", matte="none", portrait_matte="none"
        )
        art.select_image.assert_not_called()
        art.close.assert_called_once()

    @patch("tv.SamsungTVArt")
    def test_unconfirmed_selection_fails_and_closes(self, factory):
        frame = self.frame
        factory.return_value.get_current.return_value = {"content_id": "OTHER"}
        with self.assertRaises(TVError):
            frame.select("MY_1")
        factory.return_value.close.assert_called_once()
        self.assertFalse(frame.lock.locked())

    @patch("tv.SamsungTVArt")
    def test_failed_open_releases_lock(self, factory):
        frame = self.frame
        factory.return_value.open.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            frame.connect()
        self.assertFalse(frame.lock.locked())
        factory.return_value.close.assert_called_once()

    @patch("tv.SamsungTVArt")
    def test_mode_requires_readback_confirmation(self, factory):
        frame = self.frame
        factory.return_value.get_artmode.return_value = "off"
        with self.assertRaises(TVError):
            frame.mode(True)

    @patch("tv.SamsungTVArt")
    def test_tv_connection_and_waiting_stages_are_logged_without_tokens(self, factory):
        factory.return_value.available.return_value = []
        self.frame.artworks()
        messages = [entry["message"] for entry in self.frame.activity.snapshot()["entries"]]
        self.assertIn("TV: Art channel connected and ready.", messages)
        self.assertIn("TV: requesting artwork list; waiting for response.", messages)
        self.assertIn("TV: received 0 artwork item(s).", messages)
        self.assertEqual(messages[-1], "TV: Art channel closed.")
        self.assertNotIn("tv-token", " ".join(messages))


if __name__ == "__main__":
    unittest.main()
