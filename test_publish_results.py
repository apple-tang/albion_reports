import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from publish_results import publish_result_set, publish_saved_results, remove_csv_outputs


class PublishResultsTests(unittest.TestCase):
    def source(self, root, prefix, stamp="old", extension=".html"):
        path = root / "archive" / stamp / (prefix + "_2026-11-01_" + stamp + extension)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(stamp, encoding="utf-8")
        return path

    def test_only_current_category_is_replaced_and_sources_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = self.source(root, "出勤名单")
            publish_result_set(root, "2026-11-01", "attendance", [old])
            regear = self.source(root, "补装清单")
            publish_result_set(root, "2026-11-01", "regear", [regear])
            new = self.source(root, "出勤名单", "new")
            paths = publish_result_set(root, "2026-11-01", "attendance", [new])
            self.assertEqual({p.name for p in (root / "results" / "2026-11-01").iterdir()}, {new.name, regear.name})
            self.assertTrue(old.exists())
            self.assertEqual(Path(paths[0]).read_text(encoding="utf-8"), "new")

    def test_csv_and_incomplete_sources_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = self.source(root, "出勤名单")
            publish_result_set(root, "2026-11-01", "attendance", [old])
            with self.assertRaises(ValueError):
                publish_result_set(root, "2026-11-01", "attendance", [])
            csv = self.source(root, "每日出勤", extension=".csv")
            with self.assertRaises(ValueError):
                publish_result_set(root, "2026-11-01", "attendance", [old, csv])
            self.assertEqual(len(list((root / "results" / "2026-11-01").iterdir())), 1)

    def test_backfill_only_reads_html_even_if_legacy_csv_was_deleted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for stamp in ["20261101T010000Z", "20261101T020000Z"]:
                attendance = self.source(root, "出勤名单", stamp)
                metadata = root / "attendance" / "2026-11-01" / stamp / "attendance.json"
                metadata.parent.mkdir(parents=True)
                metadata.write_text(json.dumps({"files": {"html": str(attendance), "daily_csv": "deleted.csv"}}), encoding="utf-8")
                regear = self.source(root, "补装清单", stamp)
                metadata = root / "regear" / "2026-11-01" / "12u-14u" / stamp / "manifest.json"
                metadata.parent.mkdir(parents=True)
                metadata.write_text(json.dumps({"regear_html": str(regear)}), encoding="utf-8")
            paths = publish_saved_results(root)
            self.assertEqual(len(paths), 2)
            self.assertTrue(all("20261101T020000Z" in p and p.endswith(".html") for p in paths))

    def test_open_old_result_does_not_block_new_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = self.source(root, "出勤名单")
            publish_result_set(root, "2026-11-01", "attendance", [old])
            new = self.source(root, "出勤名单", "new")
            with patch.object(Path, "unlink", side_effect=PermissionError("file open")):
                paths = publish_result_set(root, "2026-11-01", "attendance", [new])
            self.assertTrue(Path(paths[0]).exists())

    def test_csv_cleanup_is_scoped_and_preserves_other_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for directory in ["attendance", "regear", "results"]:
                path = root / directory / "2026-11-01" / "old.csv"
                path.parent.mkdir(parents=True)
                path.write_text("old", encoding="utf-8")
                path.with_suffix(".json").write_text("{}", encoding="utf-8")
                path.with_suffix(".html").write_text("report", encoding="utf-8")
            source = root / "box_source.csv"
            source.write_text("keep", encoding="utf-8")
            outcome = remove_csv_outputs(root)
            self.assertEqual(len(outcome["removed"]), 3)
            self.assertEqual(outcome["blocked"], [])
            self.assertTrue(source.exists())
            self.assertEqual(len(list(root.rglob("*.json"))), 3)
            self.assertEqual(len(list(root.rglob("*.html"))), 3)


if __name__ == "__main__":
    unittest.main()
