from copy import deepcopy
from importlib.util import find_spec
from io import BytesIO
from pathlib import Path
import re
from threading import Event, Thread
import unittest
from unittest.mock import patch

from PIL import Image
from waitress import create_server

from app import create_app
from thumbnails import ThumbnailTimeout
from tv import TVError


EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
HAS_BROWSER = find_spec("playwright") is not None and EDGE.exists()


class SimulatedTV:
    def reset(self):
        self.calls = []
        self.connect_wait = None
        self.items = [
            {
                "id": "MY_1", "name": "Family photo", "personal": True, "favorite": "off",
                "width": 1200, "height": 1599, "matte": "flexible_antique",
                "portrait_matte": "flexible_black",
            },
            {"id": "STORE_1", "name": "Store art", "personal": False, "favorite": "on"},
            *[
                {"id": f"MY_{number}", "name": f"Photo {number}", "personal": True}
                for number in range(2, 15)
            ],
        ]

    def connect(self):
        self.calls.append(("connect",))
        if self.connect_wait:
            self.connect_wait.wait(8)
        return {"message": "Art channel paired."}

    def artworks(self):
        self.calls.append(("artworks",))
        return deepcopy(self.items)

    def upload(self, _photo):
        self.calls.append(("upload",))
        return {"id": "MY_NEW", "message": "Upload acknowledged."}

    def thumbnail(self, content_id):
        self.calls.append(("thumbnail", content_id))
        output = BytesIO()
        Image.new("RGB", (160, 90), "blue").save(output, "PNG")
        return output.getvalue()

    def delete(self, ids):
        self.calls.append(("delete", ids))
        self.items = [item for item in self.items if item["id"] not in ids]
        return {"message": "Deletion acknowledged."}

    def select(self, content_id):
        self.calls.append(("select", content_id))
        return {"message": "Selection confirmed."}

    def favorite(self, content_id, enabled):
        self.calls.append(("favorite", content_id, enabled))
        return {"message": "Favorite acknowledged. Refresh the library."}

    def art_read(self, setting):
        self.calls.append(("art_read", setting))
        values = {
            "current": {"content_id": "MY_1"},
            "brightness": 7,
            "temperature": -2,
            "mattes": {
                "matte_types": [{"matte_type": "flexible"}],
                "matte_colors": [{"color": "black"}],
            },
            "slideshow": {"value": "15"},
            "settings": {"data": [{"item": "brightness", "value": "7"}]},
            "version": "test-version",
            "mode": "on",
        }
        return values[setting]

    def art_write(self, setting, value):
        self.calls.append(("art_write", setting, value))
        return {"message": "Setting acknowledged."}

    def matte_details(self, content_id):
        self.calls.append(("matte_details", content_id))
        item = next(item for item in self.items if item["id"] == content_id)
        return {
            "id": content_id, "width": item.get("width"), "height": item.get("height"),
            "landscape": item.get("matte"), "portrait": item.get("portrait_matte"),
            "options": [
                {"id": "none", "name": "No mat"},
                {"id": "flexible_black", "name": "Flexible / Black"},
                {"id": "flexible_antique", "name": "Flexible / Antique"},
            ],
        }

    def matte(self, content_id, matte_id, orientation):
        self.calls.append(("matte", content_id, matte_id, orientation))
        item = next(item for item in self.items if item["id"] == content_id)
        item["matte" if orientation == "landscape" else "portrait_matte"] = matte_id
        return {
            "message": f"The TV confirms the saved {orientation} mat.",
            "artwork": {
                "id": content_id, "landscape": item.get("matte"),
                "portrait": item.get("portrait_matte"),
                "width": item.get("width"), "height": item.get("height"),
            },
        }

    def slideshow(self, *args):
        self.calls.append(("slideshow", *args))
        return {"message": "Slideshow acknowledged."}


@unittest.skipUnless(HAS_BROWSER, "Optional browser checks require playwright and Microsoft Edge.")
class DashboardBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls.tv = SimulatedTV()
        cls.tv.reset()
        cls.server = create_server(create_app(cls.tv), host="127.0.0.1", port=0, threads=4)
        cls.thread = Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.effective_port}"
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(channel="msedge", headless=True)
        except Exception:
            cls.playwright.stop()
            cls.stop_server()
            raise

    @classmethod
    def stop_server(cls):
        cls.server.close()
        cls.server.task_dispatcher.shutdown()
        cls.thread.join(3)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.stop_server()

    def setUp(self):
        from playwright.sync_api import expect

        self.expect = expect
        self.tv.reset()
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 1000})
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto(self.base)
        self.expect(self.page.locator("#activity-state")).to_contain_text("Live backend logs")

    def tearDown(self):
        if self.tv.connect_wait:
            self.tv.connect_wait.set()
        self.context.close()
        self.assertEqual(self.errors, [])

    def view(self, name):
        if self.page.locator("#mobile-menu-toggle").is_visible():
            if self.page.locator("#mobile-menu-toggle").get_attribute("aria-expanded") != "true":
                self.page.locator("#mobile-menu-toggle").click()
        self.page.get_by_role("navigation").get_by_role("button", name=name, exact=True).click()

    def open_connections(self):
        if not self.page.locator("#connection-menu").evaluate("(element) => element.open"):
            self.page.locator("#connection-menu > summary").click()

    def card(self, name):
        return self.page.locator(".photo-card").filter(
            has=self.page.get_by_role("heading", name=name, exact=True)
        )

    def load_library(self):
        self.page.locator("#refresh").click()
        self.expect(self.page.locator("#library-summary")).to_contain_text("15 total")

    def test_sections_do_not_contact_tv_until_requested(self):
        for name in ("Art settings", "Photos"):
            self.view(name)
        self.assertEqual(self.tv.calls, [])
        self.expect(self.page.locator("#view-photos")).to_be_visible()
        self.open_connections()
        self.page.locator("#connect").click()
        self.expect(self.page.locator("#status")).to_contain_text("Art channel paired")
        self.assertEqual(self.tv.calls, [("connect",)])

    def test_gallery_filter_thumbnails_and_confirmed_deletion(self):
        self.load_library()
        self.expect(self.page.locator(".photo-card")).to_have_count(12)
        self.expect(self.card("Store art").get_by_role("checkbox")).to_be_disabled()
        self.card("Family photo").get_by_role("button", name="Thumbnail", exact=True).click()
        image = self.card("Family photo").locator("img")
        self.expect(image).to_be_visible()
        self.page.wait_for_function(
            "document.querySelector('.photo-card img').naturalWidth > 0"
        )
        self.card("Family photo").get_by_role("checkbox").check()
        self.expect(self.page.locator("#delete-selected")).to_have_text("Delete selected (1)")
        self.page.once("dialog", lambda dialog: dialog.dismiss())
        self.page.locator("#delete-selected").click()
        self.assertFalse(any(call[0] == "delete" for call in self.tv.calls))
        self.page.once("dialog", lambda dialog: dialog.accept())
        self.page.locator("#delete-selected").click()
        self.expect(self.page.locator("#status")).to_have_text("Deletion acknowledged.")
        self.expect(self.card("Family photo")).to_have_count(0)
        self.assertIn(("delete", ["MY_1"]), self.tv.calls)
        self.page.locator("#library-filter").select_option("personal")
        self.expect(self.card("Store art")).to_have_count(0)
        self.page.locator("#library-search").fill("Photo 14")
        self.expect(self.page.locator(".photo-card")).to_have_count(1)

    def test_photo_preparation_is_preserved_and_rotation_invalidates_upload(self):
        output = BytesIO()
        Image.new("RGB", (100, 200), "red").save(output, "JPEG")
        self.page.locator("#open-upload").click()
        self.page.locator("#photo").set_input_files({
            "name": "test-family.jpg", "mimeType": "image/jpeg", "buffer": output.getvalue(),
        })
        self.page.locator("#prepare").click()
        self.expect(self.page.locator("#upload")).to_be_enabled()
        self.expect(self.page.locator("#preview")).to_be_visible()
        self.assertEqual(self.page.locator("#preview").evaluate("(image) => image.naturalWidth"), 3840)
        self.assertEqual(self.tv.calls, [])
        self.page.locator("#rotate").click()
        self.expect(self.page.locator("#upload")).to_be_disabled()
        self.page.locator("#prepare").click()
        self.expect(self.page.locator("#upload")).to_be_enabled()
        self.page.locator("#upload").click()
        self.expect(self.page.locator("#status")).to_have_text("Upload acknowledged.")
        self.expect(self.card("test-family.jpg")).to_have_count(1)
        self.expect(self.page.locator("#upload")).to_be_disabled()

    def test_heic_file_can_be_prepared_and_uploaded_as_jpeg(self):
        output = BytesIO()
        Image.new("RGB", (80, 160), "red").save(output, "HEIF")
        self.page.locator("#open-upload").click()
        self.expect(self.page.locator("#photo")).to_have_attribute("accept", re.compile(r"\.heic"))
        self.page.locator("#photo").set_input_files({
            "name": "camera.HEIC", "mimeType": "application/octet-stream",
            "buffer": output.getvalue(),
        })
        self.page.locator("#layout").select_option("fill")
        self.page.locator("#prepare").click()
        self.expect(self.page.locator("#upload")).to_be_enabled()
        preview = self.page.locator("#preview")
        self.expect(preview).to_be_visible()
        self.assertEqual(preview.evaluate("(image) => image.naturalWidth"), 3840)
        self.assertEqual(self.tv.calls, [])
        with patch.object(self.tv, "upload", wraps=self.tv.upload) as upload:
            self.page.locator("#upload").click()
            self.expect(self.page.locator("#status")).to_have_text("Upload acknowledged.")
            image = Image.open(BytesIO(upload.call_args.args[0]))
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size, (3840, 2160))
        self.expect(self.card("camera.HEIC")).to_have_count(1)

    def test_art_settings_mats_and_slideshow(self):
        self.load_library()
        self.card("Family photo").get_by_role("button", name="Edit mat").click()
        self.expect(self.page.locator("#view-art")).to_be_visible()
        self.expect(self.page.locator("#matte-artwork")).to_have_value("MY_1")
        self.expect(self.page.locator("#apply-matte")).to_be_disabled()
        self.page.locator("#load-mattes").click()
        self.expect(self.page.locator("#matte-metadata")).to_contain_text("1200 x 1599")
        self.expect(self.page.locator("#matte-id")).to_have_value("flexible_antique")
        self.page.locator("#matte-orientation").select_option("portrait")
        self.expect(self.page.locator("#matte-id")).to_have_value("flexible_black")
        self.page.locator("#matte-id").select_option("flexible_antique")
        self.page.locator("#apply-matte").click()
        self.expect(self.page.locator("#matte-result")).to_have_text("The TV confirms the saved portrait mat.")
        self.expect(self.page.locator("#matte-metadata")).to_contain_text("Portrait: flexible_antique")
        self.assertIn(("matte", "MY_1", "flexible_antique", "portrait"), self.tv.calls)
        self.assertEqual(self.tv.items[0]["matte"], "flexible_antique")
        self.page.locator("#matte-artwork").select_option("STORE_1")
        self.expect(self.page.locator("#apply-matte")).to_be_disabled()
        self.page.locator('[data-read="brightness"]').click()
        self.expect(self.page.locator("#brightness")).to_have_value("7")
        self.page.locator("#brightness").fill("6")
        self.page.locator('[data-write="brightness"]').click()
        self.expect(self.page.locator("#status")).to_have_text("Setting acknowledged.")
        self.assertIn(("art_write", "brightness", 6), self.tv.calls)
        self.page.locator("#apply-slideshow").click()
        self.expect(self.page.locator("#status")).to_have_text("Slideshow acknowledged.")
        self.assertIn(("slideshow", 15, True, "MY-C0002", "slideshow"), self.tv.calls)

    def test_mat_rejection_is_inline_and_borderless_shortcut_never_mutates_tv(self):
        self.load_library()
        self.card("Family photo").get_by_role("button", name="Edit mat").click()
        self.page.locator("#load-mattes").click()
        self.expect(self.page.locator("#matte-id")).to_be_enabled()
        self.page.locator("#matte-id").select_option("none")
        with patch.object(self.tv, "matte", side_effect=TVError(
            "The TV rejected No mat for this artwork (TV error -7). Use Crop to fill.",
        )) as matte:
            self.page.locator("#apply-matte").click()
            self.expect(self.page.locator("#matte-result")).to_contain_text("TV error -7")
            self.expect(self.page.locator("#status")).to_have_attribute("data-error", "true")
            self.expect(self.page.locator("#apply-matte")).to_be_disabled()
            matte.assert_called_once_with("MY_1", "none", "landscape")
        calls = deepcopy(self.tv.calls)
        self.page.locator("#matte-upload").click()
        self.expect(self.page.locator("#view-photos")).to_be_visible()
        self.expect(self.page.locator("#photo")).to_be_focused()
        self.expect(self.page.locator("#layout")).to_have_value("fill")
        self.expect(self.page.locator("#upload")).to_be_disabled()
        self.assertEqual(self.tv.calls, calls)

    def test_logs_remain_live_during_waiting_operation(self):
        self.tv.connect_wait = Event()
        starts = self.page.locator(".log-line").filter(has_text="Connect / pair: started.")
        initial_count = starts.count()
        try:
            self.open_connections()
            self.page.locator("#connect").click()
            self.expect(starts).to_have_count(initial_count + 1)
            self.expect(self.page.locator("#upload")).to_be_disabled()
            self.expect(self.page.locator("#sidebar-toggle")).to_be_enabled()
            self.expect(self.page.locator("#activity-toggle")).to_be_enabled()
            self.expect(self.page.locator("#refresh")).to_be_disabled()
        finally:
            self.tv.connect_wait.set()
        self.expect(self.page.locator("#status")).to_contain_text("Art channel paired")

    def test_narrow_layout_remains_usable(self):
        self.page.set_viewport_size({"width": 390, "height": 844})
        for name in ("Photos", "Art settings"):
            self.view(name)
            self.assertLessEqual(
                self.page.evaluate("document.documentElement.scrollWidth"),
                390,
                f"Horizontal overflow in {name}",
            )

    def test_thumbnail_batch_stops_after_first_failure_and_restores_controls(self):
        self.load_library()
        with patch.object(self.tv, "thumbnail", side_effect=ThumbnailTimeout()) as thumbnail:
            self.page.locator("#load-thumbnails").click()
            self.expect(self.page.locator("#status")).to_contain_text("stopped at image 1")
            self.expect(self.card("Family photo")).to_contain_text("other controls are available")
            self.expect(self.page.locator("#refresh")).to_be_enabled()
            self.expect(self.page.locator("#library-next")).to_be_enabled()
            thumbnail.assert_called_once()

    def test_browser_thumbnail_deadline_recovers_from_an_unresponsive_server(self):
        self.load_library()
        self.page.evaluate("""() => {
            const timeout = AbortSignal.timeout.bind(AbortSignal);
            AbortSignal.timeout = ms => timeout(ms === 20000 ? 80 : ms);
        }""")
        pending = []
        self.page.route("**/api/thumbnail", lambda route: pending.append(route))
        try:
            self.card("Family photo").get_by_role("button", name="Thumbnail", exact=True).click()
            self.expect(self.page.locator("#status")).to_contain_text("Thumbnail request timed out")
            self.expect(self.page.locator("#refresh")).to_be_enabled()
            self.expect(self.card("Family photo")).to_contain_text("Other controls are available")
        finally:
            for route in pending:
                route.abort()

    def test_sidebar_collapses_preserves_navigation_and_remembers_preference(self):
        sidebar = self.page.locator("#sidebar")
        expanded_width = sidebar.bounding_box()["width"]
        expanded_content_x = self.page.locator("#main-shell").bounding_box()["x"]
        toggle = self.page.get_by_role("button", name="Collapse sidebar", exact=True)
        toggle.click()
        self.expect(self.page.get_by_role("button", name="Expand sidebar", exact=True)).to_have_attribute("aria-expanded", "false")
        self.page.wait_for_function(
            "() => document.querySelector('#sidebar').getBoundingClientRect().width < 100"
        )
        self.assertLess(sidebar.bounding_box()["width"], expanded_width)
        self.assertLess(self.page.locator("#main-shell").bounding_box()["x"], expanded_content_x)
        self.view("Art settings")
        self.expect(self.page.get_by_role("heading", level=1)).to_have_text("Art settings")
        self.expect(self.page.locator('[data-view="art"]')).to_have_attribute("aria-current", "page")
        self.page.reload()
        self.expect(self.page.get_by_role("button", name="Expand sidebar", exact=True)).to_be_visible()
        self.page.get_by_role("button", name="Expand sidebar", exact=True).click()
        self.expect(self.page.get_by_role("button", name="Collapse sidebar", exact=True)).to_have_attribute("aria-expanded", "true")
        self.assertEqual(self.tv.calls, [])

    def test_logs_and_upload_editor_are_progressively_disclosed_without_tv_requests(self):
        self.assertFalse(self.page.locator("#activity-panel").evaluate("(element) => element.open"))
        self.assertFalse(self.page.locator("#upload-panel").evaluate("(element) => element.open"))
        self.page.locator("#activity-toggle").click()
        self.expect(self.page.locator("#activity-output")).to_be_visible()
        self.expect(self.page.locator("#sidebar-activity")).to_have_attribute("aria-expanded", "true")
        self.page.locator("#sidebar-activity").click()
        self.expect(self.page.locator("#activity-output")).to_be_hidden()
        self.page.locator("#open-upload").click()
        self.expect(self.page.locator("#photo")).to_be_visible()
        self.expect(self.page.locator("#photo")).to_be_focused()
        self.assertEqual(self.tv.calls, [])

    def test_mobile_menu_traps_focus_closes_on_escape_and_returns_focus(self):
        self.page.set_viewport_size({"width": 390, "height": 844})
        opener = self.page.get_by_role("button", name="Open navigation", exact=True)
        opener.click()
        self.expect(self.page.get_by_role("dialog", name="Application sidebar")).to_be_visible()
        self.assertTrue(self.page.locator("#main-shell").evaluate("(element) => element.inert"))
        self.page.locator("#mobile-menu-close").focus()
        self.page.keyboard.press("Shift+Tab")
        self.expect(self.page.locator("#sidebar-activity")).to_be_focused()
        self.page.keyboard.press("Tab")
        self.expect(self.page.locator("#mobile-menu-close")).to_be_focused()
        self.page.keyboard.press("Escape")
        self.expect(opener).to_be_focused()
        self.expect(opener).to_have_attribute("aria-expanded", "false")
        self.assertFalse(self.page.locator("#main-shell").evaluate("(element) => element.inert"))
        self.assertTrue(self.page.locator("#sidebar").evaluate("(element) => element.inert"))

    def test_mobile_menu_closes_after_selection_and_backdrop_click(self):
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.view("Art settings")
        self.expect(self.page.locator("#view-art")).to_be_visible()
        self.expect(self.page.locator("#page-title")).to_be_focused()
        self.expect(self.page.locator("#mobile-menu-toggle")).to_have_attribute("aria-expanded", "false")
        self.page.locator("#mobile-menu-toggle").click()
        self.page.locator("#sidebar-backdrop").click(position={"x": 385, "y": 120})
        self.expect(self.page.locator("#mobile-menu-toggle")).to_be_focused()
        self.expect(self.page.locator("#sidebar-backdrop")).to_be_hidden()
        self.assertEqual(self.tv.calls, [])

    def test_mobile_menu_is_expanded_even_when_desktop_preference_is_collapsed(self):
        self.page.locator("#sidebar-toggle").click()
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.page.locator("#mobile-menu-toggle").click()
        self.expect(self.page.locator('[data-view="photos"] .sidebar-expanded')).to_be_visible()
        self.assertGreater(self.page.locator("#sidebar").bounding_box()["width"], 200)
        self.page.set_viewport_size({"width": 1280, "height": 1000})
        self.expect(self.page.locator("#sidebar-backdrop")).to_be_hidden()
        self.expect(self.page.get_by_role("button", name="Expand sidebar", exact=True)).to_be_visible()
        self.assertFalse(self.page.locator("#main-shell").evaluate("(element) => element.inert"))

    def test_sidebar_still_works_when_browser_storage_is_blocked(self):
        self.page.add_init_script("""Object.defineProperty(window, 'localStorage', {
            get() { throw new DOMException('Storage blocked', 'SecurityError'); }
        });""")
        warnings = []
        self.page.on("console", lambda message: warnings.append(message.text) if message.type == "warning" else None)
        self.page.reload()
        self.page.locator("#sidebar-toggle").click()
        self.expect(self.page.get_by_role("button", name="Expand sidebar", exact=True)).to_be_visible()
        self.view("Art settings")
        self.expect(self.page.locator("#view-art")).to_be_visible()
        self.assertTrue(any("Sidebar preference" in message for message in warnings))
