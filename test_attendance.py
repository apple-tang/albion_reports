import copy
import csv
import tempfile
from pathlib import Path
import unittest

import collector as c
import attendance as a


def player(name="Healer", role="healer"):
    return {"name": name, "role": role, "kills": 0, "deaths": 0, "ip": 1500, "heal": 12000,
            "damage": 0, "weapon": {"type": "T4_MAIN_HOLYSTAFF@3"}}


def battle(bid="1", start="2026-10-31T12:30:00Z", players=None):
    return {"battle_id": bid, "start_utc": start, "end_utc": "2026-10-31T15:00:00Z", "guild": "Test Guild",
            "players": [player()] if players is None else players}


def report(battles):
    return {"kind": "daily", "timezone": "UTC", "period": "2026-10-31", "caller": "ZZCall",
            "generated_at": "2026-11-01T01:00:00Z", "status": "complete", "warnings": [], "battles": battles}


class AttendanceTests(unittest.TestCase):
    def test_daily_counts_sessions_not_battle_reports(self):
        data = a.build_attendance(report([battle(), battle("2", "2026-10-31T14:01:00Z"), battle("3", "2026-10-31T14:30:00Z")]))
        self.assertEqual(data["daily"][0]["total"], 2)
        self.assertEqual(data["daily"][0]["sessions"]["14u"]["battle_ids"], ["2", "3"])
        self.assertEqual(data["stats"], {"players": 1, "attendance_total": 2, "12u": 1, "14u": 1, "battles": 3, "battle_records": 3})

    def test_changing_roles_are_retained_per_session_and_per_battle(self):
        data = a.build_attendance(report([battle(players=[player(role="tank")]),
            battle("2", "2026-10-31T14:01:00Z"), battle("3", "2026-10-31T14:30:00Z", [player(role="range")])]))
        daily = data["daily"][0]
        self.assertEqual(daily["sessions"]["12u"]["roles"], ["tank"])
        self.assertEqual(daily["sessions"]["14u"]["roles"], ["healer", "dps"])
        self.assertEqual([p["role"] for p in data["details"]], ["tank", "healer", "dps"])
        self.assertEqual(data["healers"], {"12u": [], "14u": ["Healer"]})

    def test_source_roles_support_and_unknown_are_not_guessed(self):
        self.assertEqual([a.role_for(role) for role in ["healer", "range", "melee", "tank", "support", None, "new-role"]],
                         ["healer", "dps", "dps", "tank", "support", "unknown", "unknown"])

    def test_other_sessions_appear_only_in_details_and_utc_is_respected(self):
        data = a.build_attendance(report([battle(start="2026-10-31T20:00:00+08:00"),
            battle("2", "2026-10-31T13:00:00Z", [player("OtherOnly")])]))
        self.assertEqual([p["player"] for p in data["daily"]], ["Healer"])
        self.assertEqual(data["daily"][0]["total"], 1)
        self.assertEqual(len(data["details"]), 2)
        self.assertEqual(data["details"][1]["session"], "other-13u")

    def test_repeated_reports_and_case_variants_do_not_double_count(self):
        original = battle(players=[player(), player("HEALER")])
        data = a.build_attendance(report([original, copy.deepcopy(original)]))
        self.assertEqual(data["stats"]["players"], 1)
        self.assertEqual(data["stats"]["battle_records"], 1)
        self.assertEqual(data["daily"][0]["total"], 1)
        conflict = copy.deepcopy(original)
        conflict["players"][0]["role"] = "tank"
        with self.assertRaises(ValueError):
            a.build_attendance(report([original, conflict]))
        with self.assertRaises(ValueError):
            a.build_attendance(report([battle(players=[player(), player(role="tank")])]))

    def test_export_csv_colors_and_escaping_with_source_status(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            c.write_json(root / "cache" / "item_names.json", {"names": {"T4_MAIN_HOLYSTAFF": "老手级神圣法杖"}})
            source = report([battle(players=[player(), player("DPS", "melee"), player("Tank", "tank"),
                player("Support", "support"), player('<script>alert(1)</script>', None)])])
            source["status"] = "provisional"
            source["warnings"] = ["UTC 当日尚未结束"]
            data = a.export_attendance(source, root)
            self.assertEqual(data["status"], "partial")
            daily_path = Path(data["files"]["daily_csv"])
            self.assertTrue(daily_path.read_bytes().startswith(b"\xef\xbb\xbf"))
            with daily_path.open(encoding="utf-8-sig", newline="") as file:
                daily = list(csv.DictReader(file))
            self.assertEqual(len(daily), 5)
            self.assertTrue(all(row["总场次"] == "1" and row["12u"] == "1" and row["14u"] == "0" for row in daily))
            with Path(data["files"]["detail_csv"]).open(encoding="utf-8-sig", newline="") as file:
                details = list(csv.DictReader(file))
            self.assertEqual(len(details), 5)
            self.assertEqual(details[0]["武器"], "P7 神圣法杖")
            page = Path(data["files"]["html"]).read_text(encoding="utf-8")
            self.assertNotIn('<script>alert(1)</script>', page)
            self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', page)
            self.assertIn('class="role role-healer"', page)
            self.assertIn('class="role role-dps"', page)
            self.assertIn('class="role role-tank"', page)
            self.assertIn('class="role role-support"', page)
            self.assertIn('阶段性记录', page)
            self.assertNotIn('<script src=', page)
            saved = c.read_json(Path(data["files"]["html"]).parent / "attendance.json")
            self.assertEqual(saved["status"], "partial")

    def test_empty_report_and_wrong_date(self):
        data = a.build_attendance(report([]))
        self.assertEqual(data["stats"]["attendance_total"], 0)
        self.assertEqual(data["daily"], [])
        self.assertIn("没有 12u / 14u 出勤记录", a.render_attendance(data))
        with self.assertRaises(ValueError):
            a.build_attendance(report([battle(start="2026-11-01T00:00:00Z")]))


if __name__ == "__main__":
    unittest.main()
