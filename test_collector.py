import datetime as dt
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import collector as c


def battle(bid="123", day="2026-10-26", event="456"):
    return {
        "battle_id": bid, "start_utc": day + "T23:59:00Z", "end_utc": "2026-10-27T00:03:00Z",
        "url": "https://example.invalid/battles/" + bid, "guild": "Old Guild",
        "players": [{"name": "ZZCall", "kills": 0, "deaths": 1},
                    {"name": "Healer", "kills": 0, "deaths": 0}],
        "deaths": [{"event_id": event, "battle_id": bid, "time_utc": "2026-10-27T00:01:00Z",
                    "victim": "ZZCall", "guild": "Old Guild", "killer": "Enemy", "killer_guild": "Other",
                    "equipment_status": "complete", "items": [], "empty_slots": list(c.SLOTS),
                    "url": "https://example.invalid/kills/" + event}],
    }


class CollectorTests(unittest.TestCase):
    def test_utc_boundary_and_long_fraction(self):
        day = dt.date(2026, 10, 30)
        self.assertTrue(c.on_day("2026-10-30T23:59:59.123456700Z", day))
        self.assertFalse(c.on_day("2026-10-31T00:00:00Z", day))
        self.assertTrue(c.on_day("2026-10-31T07:59:59+08:00", day))

    def test_item_enchantment_is_not_quality(self):
        item = c.decode_item("MainHand", {"Type": "T6_2H_LONGBOW@3", "Quality": 2}, {"T6_2H_LONGBOW": "长弓"})
        self.assertEqual((item["tier"], item["enchantment"], item["quality"]), (6, 3, 2))
        self.assertEqual(item["tier_enchantment"], "6.3")
        self.assertEqual(item["name"], "长弓")
        self.assertFalse(item["count_reported"])
        self.assertEqual(c.decode_item("Bag", {"Type": "T5_BAG", "Quality": 1}, {})["enchantment"], 0)

    def test_historical_guild_and_zero_kill_healer(self):
        detail = {"players": [
            {"name": "ZZCall", "guildName": "Old Guild"},
            {"name": "Healer", "guildName": "Old Guild", "kills": 0, "deaths": 0},
            {"name": "Enemy", "guildName": "New Guild"},
        ]}
        guild, people = c.guild_players(detail, "ZZCall")
        self.assertEqual(guild, "Old Guild")
        self.assertEqual([p["name"] for p in people], ["ZZCall", "Healer"])
        with self.assertRaises(ValueError):
            c.guild_players(detail, "MissingCaller")

    def test_duplicate_event_and_conflict(self):
        event = {"EventId": 1, "Victim": {"Name": "A"}}
        self.assertEqual(len(c.unique_events([event, dict(event)])), 1)
        with self.assertRaises(ValueError):
            c.unique_events([event, {"EventId": 1, "Victim": {"Name": "B"}}])

    def test_full_equipment_requires_all_slots_and_identity(self):
        victim = {"Name": "A", "GuildName": "G"}
        event = {"EventId": 1, "Victim": dict(victim, Equipment={s: None for s in c.SLOTS})}
        self.assertTrue(c.full_event(event, "1", victim))
        event["Victim"]["Equipment"].pop("Armor")
        self.assertFalse(c.full_event(event, "1", victim))
        event["Victim"]["Equipment"]["Armor"] = None
        event["Victim"]["Name"] = "B"
        self.assertFalse(c.full_event(event, "1", victim))

    def test_no_double_count_and_cross_midnight_battle(self):
        people, deaths = c.aggregate([battle(), battle()])
        caller = next(p for p in people if p["name"] == "ZZCall")
        self.assertEqual(caller["battles"], ["123"])
        self.assertEqual(caller["days"], ["2026-10-26"])
        self.assertEqual(caller["deaths"], 1)
        self.assertEqual(len(deaths), 1)

    def test_week_distinguishes_missing_day_and_zero_battles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for day in ["2026-10-26", "2026-10-27"]:
                c.write_json(root / "daily" / day / "report.json", {
                    "caller": "ZZCall", "min_players": 10, "timezone": "UTC",
                    "generated_at": "2026-10-28T00:00:00Z", "status": "complete", "warnings": [],
                    "battles": [battle()] if day.endswith("26") else [],
                })
            with patch.object(c, "now", return_value=dt.datetime(2026, 11, 2, tzinfo=c.UTC)):
                report = c.build_week(root, "ZZCall", dt.date(2026, 10, 27), 10)
            self.assertEqual(len(report["missing_days"]), 5)
            self.assertNotIn("2026-10-27", report["missing_days"])
            self.assertEqual(report["status"], "partial")
            first = c.read_json(root / "weekly" / "2026-10-26" / "report.json")
            with patch.object(c, "now", return_value=dt.datetime(2026, 11, 2, tzinfo=c.UTC)):
                c.build_week(root, "ZZCall", dt.date(2026, 10, 26), 10)
            self.assertEqual(first, c.read_json(root / "weekly" / "2026-10-26" / "report.json"))

    def test_week_rejects_different_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            c.write_json(root / "daily" / "2026-10-26" / "report.json", {
                "caller": "ZZCall", "min_players": 20, "timezone": "UTC",
            })
            report = c.build_week(root, "ZZCall", dt.date(2026, 10, 26), 10)
            self.assertEqual(len(report["missing_days"]), 7)
            self.assertTrue(any("口径不同" in w for w in report["warnings"]))

    def test_failed_rerun_preserves_complete_day(self):
        class FakeClient:
            def get(self, url):
                return [{"albionId": 123, "startedAt": "2026-10-26T23:59:00Z"}]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "daily" / "2026-10-26" / "report.json"
            previous = {"caller": "ZZCall", "min_players": 10, "timezone": "UTC",
                        "generated_at": "2026-10-27T00:00:00Z", "status": "complete",
                        "warnings": [], "battles": [battle()]}
            c.write_json(path, previous)
            with patch.object(c, "load_catalog", return_value=({}, [])), \
                    patch.object(c, "collect_battle", side_effect=RuntimeError("network unavailable")):
                result = c.collect_day(FakeClient(), root, "ZZCall", dt.date(2026, 10, 26), 10)
            self.assertEqual(result["status"], "partial")
            self.assertEqual(c.read_json(path), previous)
            self.assertTrue((Path(result["run_folder"]) / "report.json").exists())

    def test_new_item_name_uses_game_data(self):
        class FakeClient:
            def get(self, url):
                self.url = url
                return {"uniqueName": "T4_NEW_ITEM", "localizedNames": {"ZH-CN": "新物品"}}

        with tempfile.TemporaryDirectory() as directory:
            catalog = {}
            client = FakeClient()
            c.resolve_new_item(client, "T4_NEW_ITEM@4", catalog, Path(directory))
            item = c.decode_item("Cape", {"Type": "T4_NEW_ITEM@4", "Quality": 5}, catalog)
            self.assertTrue(client.url.endswith("/items/T4_NEW_ITEM/data"))
            self.assertEqual((item["name"], item["enchantment"], item["quality"]), ("新物品", 4, 5))


if __name__ == "__main__":
    unittest.main()
