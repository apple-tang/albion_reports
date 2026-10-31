import copy
import csv
import tempfile
from pathlib import Path
import unittest

import collector as c


def equipped(slot="MainHand", code="T6_MAIN_SWORD@2", quality=4):
    return c.decode_item(slot, {"Type": code, "Quality": quality, "Count": 1}, {code: "测试装备"})


def death(eid="100", player="apple1263", items=None):
    return {"event_id": eid, "victim": player, "equipment_status": "complete",
            "items": items if items is not None else [equipped()]}


def battle(bid="1", start="2026-10-31T12:59:00Z", deaths=None):
    deaths = [death()] if deaths is None else deaths
    return {"battle_id": bid, "start_utc": start, "players": [
        {"name": name, "deaths": sum(d["victim"] == name for d in deaths)}
        for name in sorted({d["victim"] for d in deaths})], "deaths": deaths}


def report(battles):
    return {"kind": "daily", "timezone": "UTC", "caller": "ZZCall", "period": "2026-10-31",
            "generated_at": "2026-11-01T01:00:00Z", "status": "complete", "warnings": [], "battles": battles}


class RegearTests(unittest.TestCase):
    def layout(self, path, text):
        path.write_text(text, encoding="utf-8")
        boxes, unused = c.read_box_layout(path)
        return boxes

    def test_session_boundaries_are_utc_and_start_based(self):
        expected = {"11:59:59": "other-11u", "12:00:00": "12u", "12:59:59": "12u",
                    "13:00:00": "other-13u", "14:00:00": "14u", "14:59:59": "14u", "15:00:00": "other-15u"}
        for time, session in expected.items():
            with self.subTest(time=time):
                self.assertEqual(c.regear_session("2026-10-31T" + time + "Z"), session)
        self.assertEqual(c.regear_session("2026-10-31T20:00:00+08:00"), "12u")

    def test_row_column_coordinates_preserve_empty_cells(self):
        with tempfile.TemporaryDirectory() as temp:
            boxes = self.layout(Path(temp) / "boxes.txt", "A\tB\t\nC\t\tD\napple1263\tmzls\t\n")
            self.assertEqual(next(b for b in boxes if b["label"] == "apple1263")["box"], "3-1")
            self.assertEqual(next(b for b in boxes if b["box"] == "2-2")["label"], "")
            self.assertEqual(len(boxes), 9)

    def test_aliases_exact_names_and_ambiguous_prefix(self):
        boxes = [{"box": "1-1", "label": "TKU"}, {"box": "1-2", "label": "GODSGOD"},
                 {"box": "1-3", "label": "mengxin"}, {"box": "1-4", "label": "xiaoye"}]
        owners = c.match_box_owners(boxes, {"Tkulikus", "GodsGod", "Mengxin123", "Mengxin456", "xiaoye007"}, {"TKU": "Tkulikus"})
        self.assertEqual(owners[0]["owner"], "Tkulikus")
        self.assertEqual(owners[1]["owner"], "GodsGod")
        self.assertIsNone(owners[2]["owner"])
        self.assertEqual(owners[2]["match"], "简称有歧义")
        self.assertEqual(owners[3]["owner"], "xiaoye007")

    def test_multiple_deaths_quality_enchantment_and_exclusions(self):
        boxes = [{"box": "3-1", "label": "apple1263", "owner": "apple1263", "match": "精确匹配"}]
        items = [equipped(), equipped("OffHand", "T5_OFF_SHIELD@3", 2), equipped("Bag", "T5_BAG", 1),
                 equipped("Mount", "T3_MOUNT_HORSE", 1), equipped("Potion", "T7_POTION_REVIVE", 0), equipped("Food", "T7_MEAL_OMELETTE", 0)]
        deaths = [death("1", items=items), death("2"), death("3", items=[equipped(quality=3)]),
                  death("4", items=[equipped(code="T6_MAIN_SWORD@3")])]
        tables = c.build_regear_tables([battle(deaths=deaths)], boxes)
        quantities = {(r["物品编号"], r["品质编号"]): r["数量"] for r in tables["purchases"]}
        self.assertEqual(quantities[("T6_MAIN_SWORD@2", 4)], 2)
        self.assertEqual(quantities[("T6_MAIN_SWORD@2", 3)], 1)
        self.assertEqual(quantities[("T6_MAIN_SWORD@3", 4)], 1)
        self.assertEqual(tables["item_units"], 5)
        self.assertTrue(all(r["装备部位"] not in {"包", "坐骑", "药水", "食物"} for r in tables["purchases"]))

    def test_unmapped_and_duplicate_boxes_keep_all_quantities(self):
        boxes = [{"box": box, "label": "Exsilium1", "owner": "Exsilium1", "match": "精确匹配", "candidates": []} for box in ["2-6", "3-3"]]
        tables = c.build_regear_tables([battle(deaths=[death("1", "Exsilium1"), death("2", "ZZCall")])], boxes)
        self.assertEqual(tables["item_units"], 2)
        self.assertEqual(sum(r["数量"] for r in tables["allocations"]), 2)
        self.assertEqual(set(tables["unassigned_players"]), {"Exsilium1", "ZZCall"})
        duplicate = next(r for r in tables["allocations"] if r["玩家"] == "Exsilium1")
        self.assertEqual(duplicate["候选箱号"], "2-6、3-3")

    def test_duplicate_event_not_bought_twice_and_conflict_rejected(self):
        original = death()
        tables = c.build_regear_tables([battle(deaths=[original, copy.deepcopy(original)])], [])
        self.assertEqual(tables["item_units"], 1)
        conflict = copy.deepcopy(original)
        conflict["victim"] = "Other"
        with self.assertRaises(ValueError):
            c.build_regear_tables([battle(deaths=[original, conflict])], [])

    def test_missing_equipment_is_visible(self):
        missing = dict(death(), equipment_status="missing", items=[])
        tables = c.build_regear_tables([battle(deaths=[missing])], [])
        self.assertEqual(tables["missing_equipment"], [{"player": "apple1263", "event_id": "100"}])
        self.assertTrue(any("详情缺失" in w for w in tables["warnings"]))

    def test_managers_need_no_box_but_remain_in_procurement(self):
        deaths = [death("1", "ZZCall"), death("2", "yanfang"), death("3", "Traveler06")]
        tables = c.build_regear_tables([battle(deaths=deaths)], [], ["zzcall", "YANFANG"])
        self.assertEqual(tables["item_units"], 3)
        self.assertEqual(tables["unassigned_players"], ["Traveler06"])
        self.assertEqual(tables["no_box_players"], ["ZZCall", "yanfang"])
        managers = [r for r in tables["allocations"] if r["玩家"] != "Traveler06"]
        self.assertTrue(all(r["箱子编号"] == "无需箱子" and r["分箱状态"] == "管理（无需箱子）" for r in managers))
        self.assertEqual(sum(r["数量"] for r in tables["allocations"]), 3)
        manager_only = c.build_regear_tables([battle(deaths=deaths[:2])], [], ["ZZCall", "yanfang"])
        self.assertEqual(manager_only["warnings"], [])

    def test_export_rereads_box_file_and_reconciles_csv(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            box_file = root / "boxes.txt"
            self.layout(box_file, "A\tB\napple1263\t\n")
            data = report([battle(), battle("2", "2026-10-31T14:00:00Z", [death("200")])])
            first = c.export_regear(data, root, box_file, {})
            self.assertEqual([s["session"] for s in first["sessions"]], ["12u", "14u"])
            for session in first["sessions"]:
                with Path(session["purchase_file"]).open(encoding="utf-8-sig", newline="") as f:
                    purchases = list(csv.DictReader(f))
                self.assertEqual(sum(int(r["数量"]) for r in purchases), 1)
                self.assertEqual(session["status"], "完整")
            self.assertEqual(len(first["box_exports"]), 1)
            combined = first["box_exports"][0]
            path = Path(combined["allocation_file"])
            self.assertIn("2026-10-31_12u-14u_", path.name)
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertNotIn(b"\r\r\n", path.read_bytes())
            with path.open(encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                self.assertTrue(set(reader.fieldnames).isdisjoint({"日期UTC", "死亡次数", "数据状态", "品质", "品质编号", "箱子原名"}))
                allocations = list(reader)
            self.assertEqual(len(allocations), 1)
            self.assertEqual(allocations[0]["箱子编号"], "2-1")
            self.assertEqual(int(allocations[0]["总件数"]), 2)
            self.assertEqual(allocations[0]["主手"], "P8 测试装备 x2")
            self.layout(box_file, "A\tapple1263\n\t\n")
            second = c.export_regear(data, root, box_file, {})
            with Path(second["box_exports"][0]["allocation_file"]).open(encoding="utf-8-sig", newline="") as f:
                self.assertEqual(next(csv.DictReader(f))["箱子编号"], "1-2")
            self.assertEqual(combined["boxes"][2]["label"], "apple1263")

    def test_p_level_merges_tiers_and_quality_but_not_weapon_family(self):
        items = [equipped(code="T4_MAIN_SWORD@3", quality=2), equipped(code="T5_MAIN_SWORD@2", quality=4),
                 equipped(code="T7_MAIN_SWORD", quality=5), equipped(code="T4_MAIN_AXE@3"),
                 equipped(code="T5_MAIN_SWORD@3"), equipped(code="T8_MAIN_SWORD")]
        for item, name in zip(items, ["老手级阔剑", "专家级阔剑", "宗师级阔剑", "老手级战斧", "专家级阔剑", "禅师级阔剑"]):
            item["name"] = name
        data = [battle(deaths=[death(str(i), items=[item]) for i, item in enumerate(items)])]
        tables = c.build_regear_tables(data, [])
        rows, totals = c.player_box_rows(tables["allocations"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["总件数"], 6)
        self.assertIn("P7 阔剑 x3", rows[0]["主手"])
        self.assertIn("P7 战斧 x1", rows[0]["主手"])
        self.assertIn("P8 阔剑 x2", rows[0]["主手"])
        self.assertNotIn("禅师级", rows[0]["主手"])
        self.assertEqual(totals[("MAIN_SWORD", 7)], 3)
        self.assertEqual(len(tables["purchases"]), 6)

    def test_same_player_is_one_row_across_sessions_and_other_session_is_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            box_file = root / "boxes.txt"
            self.layout(box_file, "apple1263\n")
            data = report([battle(), battle("2", "2026-10-31T14:00:00Z", [death("200")]),
                           battle("3", "2026-10-31T13:00:00Z", [death("300")])])
            result = c.export_regear(data, root, box_file, {})
            self.assertEqual({b["group"] for b in result["box_exports"]}, {"12u-14u", "other-13u"})
            combined = next(b for b in result["box_exports"] if b["group"] == "12u-14u")
            self.assertEqual(len(combined["rows"]), 1)
            self.assertEqual(combined["rows"][0]["总件数"], 2)

    def test_provisional_and_other_session_not_silently_finalized(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            box_file = root / "boxes.txt"
            self.layout(box_file, "apple1263\n")
            data = report([battle(start="2026-10-31T13:00:00Z")])
            data["status"] = "provisional"
            exported = c.export_regear(data, root, box_file, {})
            self.assertEqual(exported["sessions"][0]["session"], "other-13u")
            self.assertEqual(exported["status"], "partial")
            self.assertTrue(any("provisional" in w for w in exported["sessions"][0]["warnings"]))

    def test_duplicate_battles_do_not_double_purchase(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            box_file = root / "boxes.txt"
            self.layout(box_file, "apple1263\n")
            exported = c.export_regear(report([battle(), battle()]), root, box_file, {})
            self.assertEqual(exported["sessions"][0]["item_units"], 1)


if __name__ == "__main__":
    unittest.main()
