import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import collector as c
import regear_page as page
from test_regear import battle, death, equipped, report


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jM1sAAAAASUVORK5CYII=")


class RegearPageTests(unittest.TestCase):
    def boxes(self, text, people):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "boxes.txt"
            path.write_text(text, encoding="utf-8")
            boxes, unused = c.read_box_layout(path)
        return c.match_box_owners(boxes, set(people), {})

    def test_totals_normalize_p_quality_and_keep_session_counts(self):
        one = equipped(code="T4_MAIN_SWORD@3", quality=2)
        two = equipped(code="T5_MAIN_SWORD@2", quality=5)
        boxes = self.boxes("apple1263\t\t\nOther\t\t\t\n", ["apple1263"])
        battles = [battle(deaths=[death("1", items=[one])]), battle("2", "2026-10-31T14:00:00Z", [death("2", items=[two])])]
        data = page.build_regear_page_data(report(battles), battles, boxes, {})
        self.assertEqual(data["item_units"], 2)
        self.assertEqual(len(data["totals"]), 1)
        self.assertEqual(data["totals"][0]["power"], 7)
        self.assertEqual(data["totals"][0]["by_session"], {"12u": 1, "14u": 1})
        self.assertEqual((data["rows"], data["columns"]), (2, 1))
        self.assertEqual(data["boxes"][0]["person"], "apple1263")

    def test_duplicate_boxes_and_managers_do_not_duplicate_equipment(self):
        boxes = self.boxes("apple1263\tOther\n\tapple1263\n", ["apple1263", "ZZCall"])
        battles = [battle(deaths=[death("1"), death("2", "ZZCall")])]
        data = page.build_regear_page_data(report(battles), battles, boxes, {"no_box_players": ["ZZCall"]})
        self.assertEqual(data["item_units"], 2)
        self.assertEqual(data["pending"], ["apple1263"])
        self.assertEqual(data["managers"], ["ZZCall"])
        self.assertEqual(sum(box["person"] is not None for box in data["boxes"]), 0)
        self.assertEqual(sum(box["duplicate"] for box in data["boxes"]), 2)
        self.assertEqual(len(data["people"]), 2)

    def test_empty_box_coordinates_remain_and_missing_equipment_is_retained(self):
        boxes = self.boxes("A\t\tB\napple1263\t\t\n", ["apple1263"])
        missing = dict(death(), equipment_status="missing", items=[])
        battles = [battle(deaths=[missing])]
        data = page.build_regear_page_data(report(battles), battles, boxes, {})
        self.assertEqual(len(data["boxes"]), 6)
        self.assertEqual(data["boxes"][1]["box"], "1-2")
        self.assertEqual(data["boxes"][1]["label"], "")
        self.assertEqual(data["people"][0]["missing_events"], ["100"])
        self.assertEqual(data["item_units"], 0)

    def test_inline_data_is_escaped_and_template_is_self_contained(self):
        boxes = self.boxes("apple1263\n", ["apple1263"])
        battles = [battle()]
        data = page.build_regear_page_data(report(battles), battles, boxes, {})
        data["people"][0]["name"] = "</script><img onerror=alert(1)>"
        html = page.render_regear_page(data, {"T4_MAIN_SWORD": "data:image/png;base64," + base64.b64encode(PNG).decode()})
        self.assertNotIn("</script><img onerror=", html)
        self.assertIn("\\u003c/script>", html)
        self.assertNotIn("<script src=", html)
        self.assertIn("data:image/png;base64,", html)
        self.assertNotIn("$data_json", html)

    def test_icon_cache_is_reused_without_network_and_not_published(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            response = MagicMock()
            response.__enter__.return_value.read.return_value = PNG
            with patch("urllib.request.urlopen", return_value=response) as request:
                icons, errors = page.load_item_icons(root, ["T4_MAIN_SWORD", "T4_MAIN_SWORD"])
                self.assertEqual(request.call_count, 1)
                self.assertEqual(errors, [])
            with patch("urllib.request.urlopen", side_effect=AssertionError("cached icon should be offline")):
                cached, errors = page.load_item_icons(root, ["T4_MAIN_SWORD"])
            self.assertEqual(icons, cached)
            self.assertEqual(errors, [])
            self.assertFalse((root / "results").exists())


if __name__ == "__main__":
    unittest.main()
