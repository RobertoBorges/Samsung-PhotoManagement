from io import BytesIO
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image
from samsungtvws import exceptions

from app import create_app
from tv import FrameTV, TVError
from thumbnails import ThumbnailTimeout


class DashboardAPITests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import Mock

        self.tv = Mock()
        self.app = create_app(self.tv)
        self.client = self.app.test_client()
        html = self.client.get("/").get_data(as_text=True)
        token = re.search(r'name="frame-token" content="([^"]+)"', html).group(1)
        self.headers = {"X-Frame-Token": token}

    def post(self, url, body):
        return self.client.post(url, json=body, headers=self.headers)

    def test_delete_requires_confirmation_valid_distinct_ids_and_bounded_batch(self):
        cases = [
            {"ids": ["MY_1"]},
            {"ids": ["MY_1"], "confirmed": "true"},
            {"ids": [], "confirmed": True},
            {"ids": ["MY_1", "MY_1"], "confirmed": True},
            {"ids": [None], "confirmed": True},
            {"ids": [f"MY_{n}" for n in range(51)], "confirmed": True},
        ]
        for body in cases:
            with self.subTest(body=body):
                self.assertEqual(self.post("/api/artworks/delete", body).status_code, 400)
        self.tv.delete.assert_not_called()
        self.tv.delete.return_value = {"message": "Acknowledged"}
        self.assertEqual(self.post("/api/artworks/delete", {"ids": ["MY_1"], "confirmed": True}).status_code, 200)
        self.tv.delete.assert_called_once_with(["MY_1"])

    def test_art_setting_ranges_types_and_allowlist(self):
        invalid = [
            {"setting": "brightness", "value": True},
            {"setting": "brightness", "value": 11},
            {"setting": "temperature", "value": -6},
            {"setting": "sensor", "value": "on"},
            {"setting": "motion_timer", "value": "999"},
            {"setting": "motion_sensitivity", "value": 3},
            {"setting": "__dict__", "value": 0},
            {"setting": [], "value": 0},
        ]
        for body in invalid:
            self.assertEqual(self.post("/api/art/write", body).status_code, 400)
        self.tv.art_write.assert_not_called()
        self.tv.art_write.return_value = {"message": "Acknowledged"}
        self.assertEqual(self.post("/api/art/write", {"setting": "brightness", "value": 7}).status_code, 200)
        self.tv.art_write.assert_called_once_with("brightness", 7)
        self.assertEqual(self.post("/api/art/read", {"setting": "delete"}).status_code, 400)
        self.tv.art_read.assert_not_called()

    def test_slideshow_rejects_bad_types_and_does_not_send_other_actions(self):
        base = {"duration": 15, "shuffle": True, "category": "MY-C0002", "engine": "slideshow"}
        for changes in [
            {"duration": True}, {"duration": -1}, {"duration": 1441},
            {"shuffle": "true"}, {"category": "store"}, {"engine": "delete"},
        ]:
            self.assertEqual(self.post("/api/art/slideshow", {**base, **changes}).status_code, 400)
        self.tv.slideshow.assert_not_called()
        self.tv.slideshow.return_value = {"message": "Acknowledged"}
        self.assertEqual(self.post("/api/art/slideshow", base).status_code, 200)
        self.tv.slideshow.assert_called_once_with(15, True, "MY-C0002", "slideshow")

    def test_mat_routes_validate_orientation_and_require_authenticated_read(self):
        body = {"id": "MY_1", "matte": "none"}
        for orientation in (None, [], 0, "automatic"):
            self.assertEqual(self.post(
                "/api/artworks/matte", {**body, "orientation": orientation},
            ).status_code, 400)
        self.tv.matte.assert_not_called()
        self.tv.matte.return_value = {"message": "Verified"}
        self.assertEqual(self.post(
            "/api/artworks/matte", {**body, "orientation": "portrait"},
        ).status_code, 200)
        self.tv.matte.assert_called_once_with("MY_1", "none", "portrait")
        self.assertEqual(self.client.post(
            "/api/artworks/matte/read", json={"id": "MY_1"},
        ).status_code, 403)
        self.assertEqual(self.post("/api/artworks/matte/read", {"id": []}).status_code, 400)
        self.tv.matte_details.assert_not_called()
        self.tv.matte_details.return_value = {"id": "MY_1", "landscape": "none"}
        response = self.post("/api/artworks/matte/read", {"id": "MY_1"})
        self.assertEqual(response.status_code, 200)
        self.tv.matte_details.assert_called_once_with("MY_1")

    def test_removed_endpoints_are_gone_and_connect_takes_no_target(self):
        for path in ("/api/power", "/api/remote/key", "/api/apps", "/api/apps/launch", "/api/diagnostics"):
            self.assertEqual(self.post(path, {}).status_code, 404)
        self.tv.connect.return_value = {"message": "Paired"}
        self.assertEqual(self.post("/api/connect", {}).status_code, 200)
        self.tv.connect.assert_called_once_with()

    def test_thumbnail_is_reencoded_and_requires_authenticated_explicit_request(self):
        output = BytesIO()
        Image.new("RGB", (1920, 1080), "blue").save(output, "PNG")
        self.tv.thumbnail.return_value = output.getvalue()
        self.assertEqual(self.client.post("/api/thumbnail", json={"id": "MY_1"}).status_code, 403)
        self.tv.thumbnail.assert_not_called()
        response = self.post("/api/thumbnail", {"id": "MY_1"})
        self.assertEqual(response.status_code, 200)
        image = Image.open(BytesIO(response.data))
        self.assertEqual(image.format, "JPEG")
        self.assertEqual(image.size, (640, 360))
        self.assertIn("blob:", response.headers["Content-Security-Policy"])
        self.tv.thumbnail.return_value = b"<svg><script>bad</script></svg>"
        self.assertEqual(self.post("/api/thumbnail", {"id": "MY_1"}).status_code, 502)

    def test_thumbnail_deadline_returns_explicit_timeout_instead_of_success(self):
        self.tv.thumbnail.side_effect = ThumbnailTimeout()
        response = self.post("/api/thumbnail", {"id": "MY_1"})
        self.assertEqual(response.status_code, 504)
        self.assertIn("other controls are available", response.get_json()["error"])
        entries = self.app.extensions["activity_log"].snapshot()["entries"]
        self.assertTrue(any("Thumbnail loading stopped" in entry["message"] for entry in entries))


class DashboardServiceTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.frame = FrameTV("192.168.3.217", Path(directory.name))
        self.frame.paired = True

    def mat_fixture(self, art):
        art.get_matte_list.return_value = {
            "matte_types": [{"matte_type": "none"}, {"matte_type": "flexible"}],
            "matte_colors": [{"color": "black"}, {"color": "antique"}],
        }
        art.change_matte.return_value = {"event": "change_matte"}
        return {
            "content_id": "MY_1", "category_id": "MY-C0002",
            "matte_id": "flexible_antique", "portrait_matte_id": "flexible_black",
            "width": 1200, "height": 1599,
        }

    @patch("tv.SamsungTVArt")
    def test_mat_read_reports_both_orientations_and_real_tv_choices_without_writing(self, factory):
        art = factory.return_value
        item = self.mat_fixture(art)
        art.available.return_value = [
            {"content_id": "MY_1", "category_id": "MY-C0008"}, item,
        ]
        result = self.frame.matte_details("MY_1")
        self.assertEqual(result["landscape"], "flexible_antique")
        self.assertEqual(result["portrait"], "flexible_black")
        self.assertEqual((result["width"], result["height"]), (1200, 1599))
        self.assertEqual({option["id"] for option in result["options"]}, {
            "none", "flexible_black", "flexible_antique",
        })
        art.change_matte.assert_not_called()

    @patch("tv.SamsungTVArt")
    def test_mat_change_preserves_other_orientation_and_verifies_selected_not_current_art(self, factory):
        art = factory.return_value
        before = self.mat_fixture(art)
        for orientation, field in (
            ("landscape", "matte_id"), ("portrait", "portrait_matte_id"),
        ):
            with self.subTest(orientation=orientation):
                after = {**before, field: "none"}
                art.available.side_effect = [[before], [after]]
                art.change_matte.reset_mock()
                result = self.frame.matte("MY_1", "none", orientation)
                if orientation == "portrait":
                    art.change_matte.assert_called_once_with(
                        "MY_1", after["matte_id"], portrait_matte=after["portrait_matte_id"],
                    )
                else:
                    art.change_matte.assert_called_once_with("MY_1", after["matte_id"])
                self.assertEqual(result["artwork"][orientation], "none")
                self.assertIn("confirms", result["message"])
        art.get_current.assert_not_called()
        art.select_image.assert_not_called()

    @patch("tv.SamsungTVArt")
    def test_invalid_or_missing_artwork_mat_is_never_sent(self, factory):
        art = factory.return_value
        item = self.mat_fixture(art)
        art.available.return_value = [item]
        for matte in ("0", "modern_black", "flexible_unlisted"):
            with self.subTest(matte=matte), self.assertRaises(ValueError):
                self.frame.matte("MY_1", matte)
        art.available.return_value = []
        with self.assertRaisesRegex(TVError, "no longer"):
            self.frame.matte("MY_1", "none")
        art.change_matte.assert_not_called()

    @patch("tv.SamsungTVArt")
    def test_mat_acknowledgement_without_persisted_change_is_not_success(self, factory):
        art = factory.return_value
        before = self.mat_fixture(art)
        for after in (before, {**before, "matte_id": "none", "portrait_matte_id": "none"}):
            with self.subTest(after=after):
                art.available.side_effect = [[before], [after]]
                with self.assertRaisesRegex(TVError, "saved mat values do not match"):
                    self.frame.matte("MY_1", "none")
        self.assertFalse(self.frame.lock.locked())

    @patch("tv.SamsungTVArt")
    def test_no_mat_rejection_explains_preparation_without_retries_or_replacement(self, factory):
        art = factory.return_value
        art.available.return_value = [self.mat_fixture(art)]
        art.change_matte.side_effect = exceptions.ResponseError(
            "`change_matte` request failed with error number -7",
        )
        with self.assertRaisesRegex(TVError, "TV rejected No mat.*TV error -7.*Crop to fill"):
            self.frame.matte("MY_1", "none")
        art.change_matte.assert_called_once_with("MY_1", "none")
        art.upload.assert_not_called()
        art.delete_list.assert_not_called()
        self.assertFalse(self.frame.lock.locked())

    @patch("tv.SamsungTVArt")
    def test_portrait_mat_requires_landscape_metadata_to_preserve_it(self, factory):
        art = factory.return_value
        item = self.mat_fixture(art)
        del item["matte_id"]
        art.available.return_value = [item]
        with self.assertRaisesRegex(TVError, "cannot be preserved safely"):
            self.frame.matte("MY_1", "none", "portrait")
        art.change_matte.assert_not_called()

    @patch("tv.SamsungTVArt")
    def test_delete_checks_live_personal_membership_and_never_deletes_store_art(self, factory):
        art = factory.return_value
        art.available.return_value = [
            {"content_id": "MY_1", "category_id": "MY-C0002"},
            {"content_id": "STORE_1", "category_id": "MY-C0008"},
        ]
        with self.assertRaises(TVError):
            self.frame.delete(["MY_1", "STORE_1"])
        art.delete_list.assert_not_called()
        art.available.assert_called_once_with(category="MY-C0002")
        art.delete_list.return_value = True
        self.assertIn("acknowledged", self.frame.delete(["MY_1"])["message"])
        art.delete_list.assert_called_once_with(["MY_1"])

    @patch("tv.ThumbnailArt")
    def test_thumbnail_uses_modern_list_api_and_releases_lock_after_timeout(self, factory):
        art = factory.return_value
        art.get_thumbnail_list.side_effect = ThumbnailTimeout()
        with self.assertRaises(ThumbnailTimeout):
            self.frame.thumbnail("MY_1")
        self.assertFalse(self.frame.lock.locked())
        art.close.assert_called_once()
        art.get_thumbnail.assert_not_called()
        art.get_thumbnail_list.assert_called_once_with(["MY_1"])
        art.get_thumbnail_list.side_effect = None
        art.get_thumbnail_list.return_value = {"MY_1.jpg": bytearray(b"jpeg")}
        self.assertEqual(self.frame.thumbnail("MY_1"), b"jpeg")

    @patch("tv.SamsungTVArt")
    def test_delete_rejects_missing_acknowledgement(self, factory):
        art = factory.return_value
        art.available.return_value = [{"content_id": "MY_1", "category_id": "MY-C0002"}]
        art.delete_list.return_value = False
        with self.assertRaises(TVError):
            self.frame.delete(["MY_1"])

    @patch("tv.SamsungTVArt")
    def test_failed_slideshow_is_not_retried_through_legacy_api(self, factory):
        art = factory.return_value
        art.set_slideshow_status.side_effect = exceptions.ResponseError("Unsupported")
        with self.assertRaises(exceptions.ResponseError):
            self.frame.slideshow(15, True, "MY-C0002", "slideshow")
        art.set_auto_rotation_status.assert_not_called()
        self.assertFalse(self.frame.lock.locked())

    @patch("tv.SamsungTVArt")
    def test_art_reads_do_not_expose_transport_tokens(self, factory):
        factory.return_value.get_artmode_settings.return_value = {
            "token": "secret", "data": [{"item": "brightness", "value": "5", "seckey": "secret"}],
        }
        result = self.frame.art_read("settings")
        self.assertNotIn("secret", str(result))
        self.assertEqual(result["data"][0]["value"], "5")

    @patch("tv.SamsungTVArt")
    def test_art_setting_change_requires_a_response_event(self, factory):
        art = factory.return_value
        art.set_brightness.return_value = {}
        with self.assertRaises(TVError):
            self.frame.art_write("brightness", 7)
        art.set_brightness.return_value = {"event": "brightness_changed"}
        self.assertIn("acknowledged", self.frame.art_write("brightness", 7)["message"])

