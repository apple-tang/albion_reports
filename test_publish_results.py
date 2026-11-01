import json
from pathlib import Path
import tempfile
import unittest

from publish_results import publish_result_set, publish_saved_results


class PublishResultsTests(unittest.TestCase):
    def source(self, root, prefix, day="2026-11-01", stamp="old", extension=".csv"):
        path = root / "archive" / stamp / (prefix + "_" + day + "_" + stamp + extension)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(stamp, encoding="utf-8")
        return path

    def attendance(self, root, stamp):
        return [self.source(root, "出勤名单", stamp=stamp, extension=".html"),
                self.source(root, "每日出勤", stamp=stamp), self.source(root, "逐场出勤", stamp=stamp)]

    def test_only_current_category_is_replaced_and_sources_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = self.attendance(root, "old")
            publish_result_set(root, "2026-11-01", "attendance", old)
            regear = [self.source(root, "总补装"), self.source(root, "箱子补装")]
            publish_result_set(root, "2026-11-01", "regear", regear)
            new = self.attendance(root, "new")
            published = publish_result_set(root, "2026-11-01", "attendance", new)
            target = root / "results" / "2026-11-01"
            self.assertEqual({p.name for p in target.iterdir()}, {p.name for p in new + regear})
            self.assertTrue(all(p.exists() for p in old + new + regear))
            self.assertEqual(len(published), 3)
            self.assertTrue(all(Path(p).read_text(encoding="utf-8") == "new" for p in published))

    def test_invalid_or_incomplete_sources_do_not_remove_current_results(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = self.attendance(root, "old")
            publish_result_set(root, "2026-11-01", "attendance", files)
            with self.assertRaises(ValueError):
                publish_result_set(root, "2026-11-01", "attendance", files[:1])
            wrong = self.source(root, "manifest", extension=".json")
            with self.assertRaises(ValueError):
                publish_result_set(root, "2026-11-01", "attendance", files + [wrong])
            self.assertEqual(len(list((root / "results" / "2026-11-01").iterdir())), 3)

    def test_obsolete_session_copy_is_removed_without_losing_the_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = [self.source(root, "总补装", stamp="12u_old"), self.source(root, "总补装", stamp="14u_old"),
                     self.source(root, "箱子补装", stamp="old")]
            publish_result_set(root, "2026-11-01", "regear", files)
            latest = [self.source(root, "总补装", stamp="12u_new"), self.source(root, "箱子补装", stamp="new")]
            publish_result_set(root, "2026-11-01", "regear", latest)
            self.assertEqual(len(list((root / "results" / "2026-11-01").iterdir())), 2)
            self.assertTrue(all(p.exists() for p in files))

    def test_backfill_selects_latest_finished_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for stamp in ["20261101T010000Z", "20261101T020000Z"]:
                files = self.attendance(root, stamp)
                metadata = root / "attendance" / "2026-11-01" / stamp / "attendance.json"
                metadata.parent.mkdir(parents=True)
                metadata.write_text(json.dumps({"files": dict(zip(["html", "daily_csv", "detail_csv"], map(str, files)))}), encoding="utf-8")
                for group, key, prefix in [("12u", "purchase_file", "总补装"), ("12u-14u", "allocation_file", "箱子补装")]:
                    metadata = root / "regear" / "2026-11-01" / group / stamp / "manifest.json"
                    metadata.parent.mkdir(parents=True)
                    source = self.source(root, prefix, stamp=stamp)
                    metadata.write_text(json.dumps({key: str(source)}), encoding="utf-8")
            incomplete = root / "regear" / "2026-11-01" / "12u" / "20261101T030000Z" / "manifest.json"
            incomplete.parent.mkdir(parents=True)
            incomplete.write_text(json.dumps({"purchase_file": str(self.source(root, "总补装", stamp="incomplete"))}), encoding="utf-8")
            files = publish_saved_results(root)
            self.assertEqual(len(files), 5)
            self.assertTrue(all("20261101T020000Z" in path for path in files))
            self.assertTrue(incomplete.exists())


if __name__ == "__main__":
    unittest.main()
