"""Archive AlbionBB caller battles and guild death equipment as UTF-8 text."""
import argparse
import collections
import contextlib
import csv
import datetime as dt
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

BASE = Path(__file__).resolve().parent
BB = "https://api.albionbb.com/eu"
GAME = "https://gameinfo-ams.albiononline.com/api/gameinfo"
ITEMS = "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json"
UTC = dt.timezone.utc
SLOTS = {
    "MainHand": "主手", "OffHand": "副手", "Head": "头部", "Armor": "衣服",
    "Shoes": "鞋子", "Bag": "包", "Cape": "披风", "Mount": "坐骑",
    "Potion": "药水", "Food": "食物",
}
QUALITIES = {0: "不适用", 1: "普通", 2: "良好", 3: "杰出", 4: "卓越", 5: "大师级"}
REGEAR_SLOTS = ("MainHand", "OffHand", "Head", "Armor", "Shoes", "Cape")
REGEAR_ITEM_FIELDS = ["装备部位", "装备名称", "阶级附魔", "品质", "品质编号", "物品编号"]


def now():
    return dt.datetime.now(UTC)


def parse_time(value):
    # Normalize subsecond precision for Python 3.9's ISO parser.
    value = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], value)
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间戳缺少时区：" + value)
    return parsed.astimezone(UTC)


def on_day(value, day):
    return parse_time(value).date() == day


def same(left, right):
    return str(left or "").casefold() == str(right or "").casefold()


def write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def write_json(path, obj):
    write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def log(message):
    print(message, flush=True)


@contextlib.contextmanager
def run_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".run.lock").open("a+b") as handle:
        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("已有采集/汇总在运行，请等待它结束。") from exc
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class Client:
    def __init__(self, transport="auto"):
        self.transport = transport
        self.shell = shutil.which("pwsh") or shutil.which("powershell")
        self.last_request = 0

    def get(self, url):
        error = None
        for attempt in range(3):
            time.sleep(max(0, 0.4 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                if self.transport == "powershell" or (self.transport == "auto" and os.name == "nt"):
                    if not self.shell:
                        raise RuntimeError("找不到 PowerShell，请安装或使用 --transport urllib。")
                    result = subprocess.run(
                        [self.shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                         "-File", str(BASE / "fetch.ps1"), "-Url", url],
                        capture_output=True, timeout=60,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    if result.returncode:
                        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())
                    text = result.stdout.decode("utf-8-sig")
                else:
                    request = urllib.request.Request(url, headers={"User-Agent": "AlbionGuildArchive/1.0"})
                    with urllib.request.urlopen(request, timeout=45) as response:
                        text = response.read().decode("utf-8-sig")
                return json.loads(text)
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                error = exc
                if attempt < 2:
                    log("  请求失败，重试 {}/2：{}".format(attempt + 1, str(exc)[:150]))
                    time.sleep(1 + attempt * 2)
        raise RuntimeError("{}: {}".format(url, error))


def load_catalog(client, root, refresh=False):
    cache = root / "cache" / "item_names.json"
    old = read_json(cache) if cache.exists() else None
    if old and old.get("source") == ITEMS and not refresh and time.time() - cache.stat().st_mtime < 7 * 86400:
        return old["names"], []
    log("读取公开物品字典（首次运行可能较慢）...")
    try:
        items = client.get(ITEMS)
        if not isinstance(items, list):
            raise ValueError("物品字典不是数组")
        names = {}
        for item in items:
            if item.get("UniqueName"):
                localized = item.get("LocalizedNames") or {}
                names[item["UniqueName"]] = localized.get("ZH-CN") or localized.get("EN-US") or item["UniqueName"]
        if not names:
            raise ValueError("物品字典没有有效名称")
        write_json(cache, {"downloaded_at": now().isoformat(), "source": ITEMS, "names": names})
        return names, []
    except (RuntimeError, ValueError) as exc:
        if old:
            return old["names"], ["物品字典更新失败，使用旧缓存：" + str(exc)]
        return {}, ["物品字典不可用，保留原始物品编号：" + str(exc)]


def decode_item(slot, item, catalog):
    code = item["Type"]
    base, _, enchant = code.partition("@")
    tier = re.match(r"^T(\d+)_", base)
    tier = int(tier.group(1)) if tier else None
    enchantment = int(enchant) if enchant.isdigit() else 0
    quality = item.get("Quality")
    name = catalog.get(code) or catalog.get(base) or code
    return {
        "slot": slot, "slot_name": SLOTS.get(slot, slot), "item_id": code, "name": name,
        "tier": tier, "enchantment": enchantment,
        "tier_enchantment": "{}.{}".format(tier, enchantment) if tier is not None else "未知",
        "quality": quality, "quality_name": QUALITIES.get(quality, "未知"),
        "count": item.get("Count", 1), "count_reported": "Count" in item,
        "name_resolved": name != code,
        "legendary_soul": item.get("LegendarySoul"),
    }


def resolve_new_item(client, code, catalog, raw):
    base = code.split("@", 1)[0]
    if code in catalog or base in catalog:
        return
    try:
        data = client.get(GAME + "/items/" + urllib.parse.quote(base, safe="") + "/data")
        write_json(raw / ("item_" + base + ".json"), data)
        if data.get("uniqueName") != base:
            return
        names = data.get("localizedNames") or {}
        name = names.get("ZH-CN") or names.get("EN-US")
        if name:
            catalog[base] = name
    except (RuntimeError, ValueError, AttributeError):
        pass


def unique_events(events):
    if not isinstance(events, list):
        raise ValueError("Kills 响应不是数组")
    found = {}
    for event in events:
        if not isinstance(event, dict) or not event.get("EventId") or not isinstance(event.get("Victim"), dict):
            raise ValueError("Kills 记录缺少 EventId/Victim")
        key = str(event["EventId"])
        if key in found and found[key] != event:
            raise ValueError("同一个 EventId 返回冲突记录：" + key)
        found[key] = event
    return list(found.values())


def guild_players(detail, caller):
    players = detail.get("players")
    if not isinstance(players, list):
        raise ValueError("战报 players 不是数组，接口可能变更")
    caller_rows = [p for p in players if same(p.get("name"), caller)]
    if len(caller_rows) != 1 or not caller_rows[0].get("guildName"):
        raise ValueError("战报中找不到唯一 caller 或其公会，不能确定统计范围")
    guild = caller_rows[0]["guildName"]
    selected = [p for p in players if same(p.get("guildName"), guild)]
    names = [p["name"].casefold() for p in selected]
    if len(set(names)) != len(names):
        raise ValueError("战报中出现重复成员名")
    return guild, selected


def full_event(event, event_id, victim):
    if not isinstance(event, dict) or str(event.get("EventId")) != str(event_id):
        return False
    actual = event.get("Victim") or {}
    equipment = actual.get("Equipment")
    return (same(actual.get("Name"), victim.get("Name"))
            and same(actual.get("GuildName"), victim.get("GuildName"))
            and isinstance(equipment, dict) and set(SLOTS).issubset(equipment))


def collect_battle(client, row, caller, catalog, raw):
    battle_id = str(row["albionId"])
    detail = client.get(BB + "/battles/" + battle_id)
    write_json(raw / ("battle_" + battle_id + ".json"), detail)
    if str(detail.get("albionId")) != battle_id:
        raise ValueError("返回的战报 ID 不一致")
    guild, players = guild_players(detail, caller)
    warnings = []
    if detail.get("totalPlayers") != len(detail["players"]):
        warnings.append("战报总人数与 players 列表长度不一致。")
    guild_meta = [g for g in detail.get("guilds", []) if same(g.get("name"), guild)]
    if len(guild_meta) != 1:
        warnings.append("未找到唯一公会汇总，无法交叉核对本会人数。")
    else:
        for field, actual in [("players", len(players)), ("deaths", sum(p["deaths"] for p in players)),
                              ("kills", sum(p["kills"] for p in players))]:
            if guild_meta[0].get(field) != actual:
                warnings.append("公会汇总 {}={}，成员明细合计 {}。".format(field, guild_meta[0].get(field), actual))
    result = {
        "battle_id": battle_id, "start_utc": detail["startedAt"], "end_utc": detail.get("finishedAt"),
        "url": "https://europe.albionbb.com/battles/" + battle_id,
        "guild": guild, "total_players": detail.get("totalPlayers"),
        "total_kills": detail["totalKills"], "players": players, "deaths": [], "warnings": warnings,
    }
    try:
        events_raw = client.get(BB + "/battles/kills?ids=" + battle_id)
        write_json(raw / ("kills_" + battle_id + ".json"), events_raw)
        events = unique_events(events_raw)
    except (RuntimeError, ValueError) as exc:
        events = []
        warnings.append("Kills 获取失败：" + str(exc))
    result["events_received"] = len(events)
    if len(events) != detail["totalKills"]:
        warnings.append("战报击杀数 {}，实际 Kills {}，明细可能缺失。".format(detail["totalKills"], len(events)))
    for event in events:
        victim = event["Victim"]
        if not same(victim.get("GuildName"), guild):
            continue
        eid = str(event["EventId"])
        log("    死亡装备 {} / {}".format(victim.get("Name"), eid))
        death = {
            "event_id": eid, "battle_id": battle_id, "time_utc": event["TimeStamp"],
            "victim": victim["Name"], "victim_id": victim.get("Id"), "guild": guild,
            "killer": (event.get("Killer") or {}).get("Name"),
            "killer_guild": (event.get("Killer") or {}).get("GuildName"),
            "equipment_status": "missing", "items": [], "empty_slots": [],
            "url": "https://europe.albionbb.com/battles/" + battle_id,
            "detail_api_url": BB + "/kills/" + eid,
        }
        errors = []
        for source, url in [("albionbb", BB + "/kills/" + eid), ("gameinfo", GAME + "/events/" + eid)]:
            try:
                full = client.get(url)
                write_json(raw / (source + "_event_" + eid + ".json"), full)
                if not full_event(full, eid, victim):
                    raise ValueError("装备槽位不全，或死者/事件不一致")
                if full.get("BattleId") is not None and str(full["BattleId"]) != battle_id:
                    raise ValueError("事件的 BattleId 不一致")
                equipment = full["Victim"]["Equipment"]
                for slot, item in equipment.items():
                    if item is None:
                        death["empty_slots"].append(slot)
                    elif isinstance(item, dict) and item.get("Type"):
                        resolve_new_item(client, item["Type"], catalog, raw)
                        death["items"].append(decode_item(slot, item, catalog))
                    else:
                        raise ValueError("装备项格式异常：" + slot)
                death["equipment_status"] = "complete"
                death["equipment_source"] = source
                break
            except (RuntimeError, ValueError, KeyError) as exc:
                death["items"] = []
                death["empty_slots"] = []
                errors.append(source + ": " + str(exc))
        if death["equipment_status"] != "complete":
            death["errors"] = errors
            warnings.append("{} 的死亡事件 {} 装备详情缺失".format(victim["Name"], eid))
        elif any(not item["name_resolved"] for item in death["items"]):
            warnings.append("事件 {} 有物品未映射名称，请按原始编号核对。".format(eid))
        result["deaths"].append(death)
    counts = collections.Counter(d["victim"].casefold() for d in result["deaths"])
    for player in players:
        actual = counts[player["name"].casefold()]
        if actual != player["deaths"]:
            warnings.append("{}：战报死亡 {} 次，收集到 {} 次。".format(player["name"], player["deaths"], actual))
    known_names = {p["name"].casefold() for p in players}
    for death in result["deaths"]:
        if death["victim"].casefold() not in known_names:
            warnings.append("死者 {} 未出现在本会参战名单中。".format(death["victim"]))
    return result


def aggregate(battles):
    people, deaths, seen_battles = {}, {}, set()
    for battle in battles:
        if battle["battle_id"] in seen_battles:
            continue
        seen_battles.add(battle["battle_id"])
        day = parse_time(battle["start_utc"]).date().isoformat()
        for p in battle["players"]:
            key = p["name"].casefold()
            row = people.setdefault(key, {"name": p["name"], "battles": [], "days": [], "guilds": [], "kills": 0, "deaths": 0})
            row["battles"].append(battle["battle_id"])
            row["days"] = sorted(set(row["days"] + [day]))
            row["guilds"] = sorted(set(row["guilds"] + [battle["guild"]]))
            row["kills"] += p["kills"]
            row["deaths"] += p["deaths"]
        for death in battle["deaths"]:
            deaths[death["event_id"]] = death
    return sorted(people.values(), key=lambda p: (-len(p["battles"]), p["name"].casefold())), sorted(deaths.values(), key=lambda d: (d["time_utc"], d["event_id"]))


def report_text(report):
    people, deaths = aggregate(report["battles"])
    expected = sum(p["deaths"] for p in people)
    complete = sum(d["equipment_status"] == "complete" for d in deaths)
    title = "每日战报" if report["kind"] == "daily" else "每周汇总"
    lines = [title + " | " + report["period"] + " | UTC", "Caller：" + report["caller"],
             "状态：" + report["status"], "生成时间：" + report["generated_at"],
             "范围：每场战报中 caller 所属公会；最低 {} 人；按战斗开始日归属。".format(report["min_players"]),
             "战斗 {} 场；去重参战 {} 人；战报死亡 {} 次；死亡明细 {} 次；装备齐全 {} 次。".format(
                 len(report["battles"]), len(people), expected, len(deaths), complete),
             "参战名单仅反映战报记录；死亡装备为穿戴快照，不代表实际掉落/损毁。"]
    if report.get("missing_days"):
        lines.append("尚未采集日期：" + ", ".join(report["missing_days"]))
    lines += ["", "检查提示："] + (report["warnings"] or ["未发现数量或请求异常。"])
    lines += ["", "参战统计（姓名 | 参战场次 | 参战天数 | 击杀 | 死亡）："]
    for person in people:
        lines.append("{} | {} | {} | {} | {}".format(person["name"], len(person["battles"]), len(person["days"]), person["kills"], person["deaths"]))
    lines += ["", "逐场名单："]
    for battle in report["battles"]:
        lines += ["{} | {} | {} | 本会 {} 人".format(battle["battle_id"], battle["start_utc"], battle["guild"], len(battle["players"])),
                  battle["url"], ", ".join(sorted(p["name"] for p in battle["players"])), ""]
    return "\n".join(lines) + "\n"


def death_text(deaths):
    lines = ["死亡时装备明细（UTC，穿戴快照）", "附魔等级与品质分开记录；空槽位表示接口明确返回 null。", ""]
    for death in deaths:
        lines.append("{} | {} | 战斗 {} | 事件 {} | {}".format(death["time_utc"], death["victim"], death["battle_id"], death["event_id"], death["guild"]))
        lines.append("击杀者：{} / {}；装备状态：{}".format(death["killer"], death["killer_guild"], death["equipment_status"]))
        lines.append(death["url"])
        for item in death["items"]:
            lines.append("  {}：{} | {} | 品质 {}({}) | 数量 {} | {}".format(
                item["slot_name"], item["name"], item["tier_enchantment"], item["quality"], item["quality_name"], item["count"], item["item_id"]))
        if death["empty_slots"]:
            lines.append("  空槽位：" + "、".join(SLOTS.get(s, s) for s in death["empty_slots"]))
        lines += death.get("errors", []) + [""]
    if not deaths:
        lines.append("当前记录无死亡明细；是否齐全请查看 report.txt 的检查提示。")
    return "\n".join(lines) + "\n"


def export_report(folder, report):
    people, deaths = aggregate(report["battles"])
    write_json(folder / "report.json", report)
    write_text(folder / "report.txt", report_text(report))
    write_text(folder / "deaths.txt", death_text(deaths))
    write_text(folder / "attendance.jsonl", "".join(json.dumps(p, ensure_ascii=False) + "\n" for p in people))
    write_text(folder / "deaths.jsonl", "".join(json.dumps(d, ensure_ascii=False) + "\n" for d in deaths))
    equipment = []
    for death in deaths:
        for item in death["items"]:
            equipment.append(dict(item, event_id=death["event_id"], battle_id=death["battle_id"], victim=death["victim"], time_utc=death["time_utc"]))
    write_text(folder / "equipment.jsonl", "".join(json.dumps(i, ensure_ascii=False) + "\n" for i in equipment))
    counts = collections.Counter()
    for item in equipment:
        counts[(item["item_id"], item["name"], item["quality_name"])] += item["count"]
    write_text(folder / "equipment_totals.txt", "装备累计数量（仅统计已获得的穿戴明细）\n" + "\n".join(
        "{} | {} | {} | {}".format(name, code, quality, count)
        for (code, name, quality), count in sorted(counts.items())) + "\n")


def regear_session(start_utc):
    time_utc = parse_time(start_utc)
    if time_utc.hour == 12:
        return "12u"
    if time_utc.hour == 14:
        return "14u"
    return "other-{:02d}u".format(time_utc.hour)


def read_box_layout(path):
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    rows = list(csv.reader(io.StringIO(text, newline=""), delimiter="\t"))
    if not rows or not any(cell.strip() for row in rows for cell in row):
        raise ValueError("箱子排序文件为空。")
    columns = max(len(row) for row in rows)
    boxes = []
    for row_index, row in enumerate(rows, 1):
        for column in range(1, columns + 1):
            boxes.append({"box": "{}-{}".format(row_index, column), "row": row_index,
                          "column": column, "label": row[column - 1].strip() if column <= len(row) else ""})
    return boxes, text


def match_box_owners(boxes, players, aliases):
    players_by_name = {p.strip().casefold(): p for p in players}
    aliases = {key.strip().casefold(): value.strip().casefold() for key, value in aliases.items()}
    result = []
    for source in boxes:
        box = dict(source, owner=None, match="空箱", candidates=[])
        label = source["label"].casefold()
        if not label:
            result.append(box)
            continue
        target = aliases.get(label, label)
        if target in players_by_name:
            box.update(owner=players_by_name[target], match="配置简称" if label in aliases else "精确匹配")
        elif label not in aliases:
            candidates = [name for key, name in players_by_name.items() if len(label) >= 3 and key.startswith(label)]
            if len(candidates) == 1:
                box.update(owner=candidates[0], match="唯一前缀简称")
            else:
                box.update(match="简称有歧义" if candidates else "本日报无对应人员", candidates=sorted(candidates))
        else:
            box["match"] = "配置对应人员未出现在本日报"
        result.append(box)
    return result


def box_for_player(player, boxes, no_box_players=()):
    if any(same(player, name) for name in no_box_players):
        return {"箱子编号": "无需箱子", "玩家": player, "箱子原名": "", "分箱状态": "管理（无需箱子）",
                "候选箱号": "", "match": "管理配置"}
    candidates = [box for box in boxes if same(box["owner"], player)]
    if len(candidates) == 1:
        box = candidates[0]
        return {"箱子编号": box["box"], "玩家": player, "箱子原名": box["label"],
                "分箱状态": "已分配", "候选箱号": "", "match": box["match"]}
    if candidates:
        return {"箱子编号": "待确认", "玩家": player, "箱子原名": candidates[0]["label"],
                "分箱状态": "重复箱子归属", "候选箱号": "、".join(b["box"] for b in candidates), "match": "重复"}
    ambiguous = [box for box in boxes if any(same(name, player) for name in box["candidates"])]
    if ambiguous:
        return {"箱子编号": "待确认", "玩家": player, "箱子原名": "、".join(b["label"] for b in ambiguous),
                "分箱状态": "简称需确认", "候选箱号": "、".join(b["box"] for b in ambiguous), "match": "有歧义"}
    return {"箱子编号": "待分配", "玩家": player, "箱子原名": "", "分箱状态": "未找到箱子",
            "候选箱号": "", "match": "未匹配"}


def regear_item_row(item):
    return {"装备部位": item["slot_name"], "装备名称": item["name"],
            "阶级附魔": "T" + item["tier_enchantment"], "品质": item["quality_name"],
            "品质编号": item["quality"] if item["quality"] is not None else "未知", "物品编号": item["item_id"]}


def build_regear_tables(battles, boxes, no_box_players=()):
    purchases, allocations, events = {}, {}, {}
    warnings, missing_equipment = [], []
    expected_deaths = sum(p["deaths"] for b in battles for p in b["players"])
    for battle in battles:
        for death in battle["deaths"]:
            event_id = str(death["event_id"])
            if event_id in events:
                if events[event_id] != death:
                    raise ValueError("补装源数据中 EventId 冲突：" + event_id)
                continue
            events[event_id] = death
            if death["equipment_status"] != "complete":
                missing_equipment.append({"player": death["victim"], "event_id": event_id})
                continue
            assignment = box_for_player(death["victim"], boxes, no_box_players)
            slots_seen = set()
            for item in death["items"]:
                if item["slot"] not in REGEAR_SLOTS:
                    continue
                if item["slot"] in slots_seen:
                    raise ValueError("同一死亡事件有重复装备槽位：" + event_id)
                slots_seen.add(item["slot"])
                count = item["count"]
                if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
                    raise ValueError("装备数量不是正整数：事件 " + event_id)
                key = (item["item_id"], item["quality"])
                item_row = regear_item_row(item)
                purchase = purchases.setdefault(key, dict(item_row, quantity=0, events=set(), players=set()))
                purchase["quantity"] += count
                purchase["events"].add(event_id)
                purchase["players"].add(death["victim"])
                allocation_key = (assignment["箱子编号"], death["victim"].casefold(), key)
                allocation = allocations.setdefault(allocation_key, dict(item_row, **assignment, quantity=0, events=set()))
                allocation["quantity"] += count
                allocation["events"].add(event_id)
                if not item["name_resolved"]:
                    warnings.append("未解析物品名称：" + item["item_id"])
    if len(events) != expected_deaths:
        warnings.append("战报死亡 {} 次，现有死亡明细 {} 次。".format(expected_deaths, len(events)))
    if missing_equipment:
        warnings.append("有 {} 次死亡的装备详情缺失，不能据此清单完成全部补装。".format(len(missing_equipment)))
    unassigned = sorted({a["玩家"] for a in allocations.values() if a["分箱状态"] not in {"已分配", "管理（无需箱子）"}})
    managers = sorted({a["玩家"] for a in allocations.values() if a["分箱状态"] == "管理（无需箱子）"})
    if unassigned:
        warnings.append("以下人员箱子待确认，装备仍计入总采购与分箱清单：" + "、".join(unassigned))
    purchase_counts = {key: row["quantity"] for key, row in purchases.items()}
    allocation_counts = collections.Counter()
    for allocation in allocations.values():
        allocation_counts[(allocation["物品编号"], allocation["品质编号"])] += allocation["quantity"]
    normalized = {(key[0], key[1] if key[1] is not None else "未知"): count for key, count in purchase_counts.items()}
    if normalized != dict(allocation_counts):
        raise ValueError("总补装清单与分箱清单的物品数量不一致。")
    purchase_rows = []
    slot_order = {SLOTS[slot]: index for index, slot in enumerate(REGEAR_SLOTS)}
    for row in sorted(purchases.values(), key=lambda r: (slot_order[r["装备部位"]], r["物品编号"], str(r["品质编号"]))):
        purchase_rows.append({**{field: row[field] for field in REGEAR_ITEM_FIELDS}, "数量": row["quantity"],
                              "死亡次数": len(row["events"]), "涉及人员": "、".join(sorted(row["players"]))})
    def allocation_order(row):
        coordinate = row["箱子编号"].split("-")
        box_order = tuple(map(int, coordinate)) if len(coordinate) == 2 and all(n.isdigit() for n in coordinate) else (999, 999)
        return (box_order, row["玩家"].casefold(), slot_order[row["装备部位"]], row["物品编号"], str(row["品质编号"]))
    allocation_rows = []
    for row in sorted(allocations.values(), key=allocation_order):
        fields = ["箱子编号", "玩家", "箱子原名", "分箱状态", "候选箱号"] + REGEAR_ITEM_FIELDS
        allocation_rows.append({**{field: row[field] for field in fields}, "数量": row["quantity"],
                                "死亡次数": len(row["events"]), "来源事件": "、".join(sorted(row["events"]))})
    return {"purchases": purchase_rows, "allocations": allocation_rows, "warnings": sorted(set(warnings)),
            "expected_deaths": expected_deaths, "received_deaths": len(events),
            "missing_equipment": missing_equipment, "unassigned_players": unassigned, "no_box_players": managers,
            "item_units": sum(purchase_counts.values()), "counts_reconciled": True}


def write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


def box_item_level(item_id, name):
    match = re.fullmatch(r"T(\d+)_(.+?)(?:@(\d+))?", item_id)
    if not match:
        raise ValueError("无法换算 P 等级的装备编号：" + item_id)
    power = int(match.group(1)) + int(match.group(3) or 0)
    name = re.sub(r"^(?:新手级|初学者级|熟练者级|老手级|专家级|大师级|宗师级|长老级|禅师级)", "", name)
    name = re.sub(r"^(?:Beginner's|Novice's|Journeyman's|Adept's|Expert's|Master's|Grandmaster's|Elder's)\s+", "", name)
    return match.group(2), power, name


def player_box_rows(allocations):
    players, item_totals = {}, collections.Counter()
    slot_names = [SLOTS[slot] for slot in REGEAR_SLOTS]
    for allocation in allocations:
        player = allocation["玩家"]
        row = players.setdefault(player.casefold(), {
            "箱子编号": allocation["箱子编号"], "玩家": player, "分箱状态": allocation["分箱状态"],
            "slots": {slot: {} for slot in slot_names}, "总件数": 0,
        })
        if allocation["候选箱号"]:
            row["分箱状态"] = allocation["分箱状态"] + "（" + allocation["候选箱号"] + "）"
        family, power, name = box_item_level(allocation["物品编号"], allocation["装备名称"])
        key = (family, power)
        item = row["slots"][allocation["装备部位"]].setdefault(key, {"name": name, "quantity": 0})
        item["quantity"] += allocation["数量"]
        row["总件数"] += allocation["数量"]
        item_totals[key] += allocation["数量"]

    def player_order(row):
        parts = row["箱子编号"].split("-")
        if len(parts) == 2 and all(part.isdigit() for part in parts):
            return (0, int(parts[0]), int(parts[1]), row["玩家"].casefold())
        return (1 if row["箱子编号"] == "无需箱子" else 2, 0, 0, row["玩家"].casefold())

    rows = []
    for player in sorted(players.values(), key=player_order):
        row = {field: player[field] for field in ["箱子编号", "玩家", "分箱状态", "总件数"]}
        for slot in slot_names:
            row[slot] = "; ".join("P{} {} x{}".format(power, item["name"], item["quantity"])
                                  for (family, power), item in sorted(player["slots"][slot].items()))
        rows.append(row)
    return rows, item_totals


def export_regear(report, root, box_file, settings):
    if report.get("kind") != "daily" or report.get("timezone") != "UTC":
        raise ValueError("补装导出需要 UTC 每日报告。")
    boxes, box_text = read_box_layout(box_file)
    players = {p["name"] for b in report["battles"] for p in b["players"]}
    players.update(d["victim"] for b in report["battles"] for d in b["deaths"])
    boxes = match_box_owners(boxes, players, settings.get("box_aliases", {}))
    sessions, seen = {}, {}
    for battle in report["battles"]:
        bid = battle["battle_id"]
        if bid in seen:
            if seen[bid] != battle:
                raise ValueError("补装源数据中 BattleId 冲突：" + bid)
            continue
        seen[bid] = battle
        if not on_day(battle["start_utc"], dt.date.fromisoformat(report["period"])):
            raise ValueError("补装源数据混入其他 UTC 日期的战斗。")
        sessions.setdefault(regear_session(battle["start_utc"]), []).append(battle)
    if not sessions:
        sessions = {"12u": [], "14u": []}
    generated = now()
    stamp = generated.strftime("%Y%m%dT%H%M%S_%fZ")
    output = {"date": report["period"], "generated_at": generated.isoformat(), "sessions": [], "box_exports": []}
    for session, battles in sorted(sessions.items()):
        tables = build_regear_tables(battles, boxes, settings.get("no_box_players", []))
        warnings = list(tables["warnings"])
        if report["status"] != "complete":
            warnings.append("源日报状态为 {}，本次不是已确认的全天完整补装清单。".format(report["status"]))
        warnings.extend(report.get("warnings", []))
        status = "待核对" if warnings else "完整"
        folder = root / "regear" / report["period"] / session / stamp
        suffix = "{}_{}_{}".format(report["period"], session, stamp)
        purchase_file = folder / ("总补装_" + suffix + ".csv")
        common = {"日期UTC": report["period"], "场次": session, "数据状态": status}
        purchase_fields = ["日期UTC", "场次"] + REGEAR_ITEM_FIELDS + ["数量", "死亡次数", "涉及人员", "数据状态"]
        write_csv(purchase_file, purchase_fields, [dict(row, **common) for row in tables["purchases"]])
        manifest = {"date": report["period"], "session": session, "generated_at": generated.isoformat(),
                    "source_generated_at": report["generated_at"], "source_status": report["status"],
                    "caller": report["caller"], "battle_ids": sorted(b["battle_id"] for b in battles),
                    "included_slots": list(REGEAR_SLOTS), "box_file": str(Path(box_file).resolve()),
                    "boxes": boxes, "box_aliases": settings.get("box_aliases", {}),
                    "configured_no_box_players": settings.get("no_box_players", []),
                    "status": status, "warnings": warnings,
                    **{key: value for key, value in tables.items() if key not in {"purchases", "allocations", "warnings"}},
                    "purchase_file": str(purchase_file)}
        write_json(folder / "manifest.json", manifest)
        write_text(folder / "箱子排序_本次快照.txt", box_text)
        write_text(folder / "补装检查.txt", "{} {} UTC | {}\n死亡 {} 次；战斗装备 {} 件；采购与分配源明细逐物品核对一致。\n{}\n".format(
            report["period"], session, status, tables["received_deaths"], tables["item_units"],
            "\n".join(warnings + (["管理（无需箱子），装备仍计入采购：" + "、".join(tables["no_box_players"])] if tables["no_box_players"] else [])) or "未发现缺失项。"))
        output["sessions"].append(manifest)
        log("补装 {}：{} 次死亡，{} 件装备；{}".format(session, tables["received_deaths"], tables["item_units"], status))
        log("  采购：" + str(purchase_file))
        for warning in tables["warnings"]:
            log("  提示：" + warning)
    box_groups = {}
    for session, battles in sessions.items():
        group = "12u-14u" if session in {"12u", "14u"} else session
        box_groups.setdefault(group, []).extend(battles)
    for group, battles in sorted(box_groups.items()):
        tables = build_regear_tables(battles, boxes, settings.get("no_box_players", []))
        rows, item_totals = player_box_rows(tables["allocations"])
        purchase_totals = collections.Counter()
        for purchase in tables["purchases"]:
            family, power, unused = box_item_level(purchase["物品编号"], purchase["装备名称"])
            purchase_totals[(family, power)] += purchase["数量"]
        if purchase_totals != item_totals or sum(row["总件数"] for row in rows) != tables["item_units"]:
            raise ValueError("按人合并后的分箱数量与采购清单不一致。")
        folder = root / "regear" / report["period"] / group / stamp
        allocation_file = folder / ("箱子补装_{}_{}_{}.csv".format(report["period"], group, stamp))
        fields = ["箱子编号", "玩家", "分箱状态"] + [SLOTS[slot] for slot in REGEAR_SLOTS] + ["总件数"]
        write_csv(allocation_file, fields, rows)
        warnings = list(tables["warnings"]) + report.get("warnings", [])
        if report["status"] != "complete":
            warnings.append("源日报状态为 {}，本次不是已确认的全天完整补装清单。".format(report["status"]))
        manifest = {
            "schema_version": 2, "date": report["period"], "group": group,
            "generated_at": generated.isoformat(), "source_generated_at": report["generated_at"],
            "source_status": report["status"], "sessions": sorted({regear_session(b["start_utc"]) for b in battles}),
            "battle_ids": sorted(b["battle_id"] for b in battles), "caller": report["caller"],
            "box_file": str(Path(box_file).resolve()), "boxes": boxes,
            "box_aliases": settings.get("box_aliases", {}), "configured_no_box_players": settings.get("no_box_players", []),
            "included_slots": list(REGEAR_SLOTS), "allocation_file": str(allocation_file), "rows": rows,
            "allocation_details": tables["allocations"], "item_units": tables["item_units"],
            "unassigned_players": tables["unassigned_players"], "no_box_players": tables["no_box_players"],
            "missing_equipment": tables["missing_equipment"], "warnings": warnings,
            "counts_reconciled": True, "status": "待核对" if warnings else "完整",
        }
        write_json(folder / "manifest.json", manifest)
        write_text(folder / "箱子排序_本次快照.txt", box_text)
        write_text(folder / "补装检查.txt", "{} {} UTC | {}\n{} 人，每人一行，共 {} 件；按装备种类和 P 等级与采购清单核对一致。\n{}\n".format(
            report["period"], group, manifest["status"], len(rows), tables["item_units"], "\n".join(warnings) or "未发现缺失项。"))
        output["box_exports"].append(manifest)
        log("合并分箱 {}：{} 人，{} 件装备".format(group, len(rows), tables["item_units"]))
        log("  分箱：" + str(allocation_file))
    output["status"] = "complete" if all(s["status"] == "完整" for s in output["sessions"] + output["box_exports"]) else "partial"
    from publish_results import publish_result_set
    output["published_files"] = publish_result_set(root, report["period"], "regear",
        [session["purchase_file"] for session in output["sessions"]] + [group["allocation_file"] for group in output["box_exports"]])
    log("  同步用结果：" + str(root / "results" / report["period"]))
    return output


def collect_day(client, root, caller, day, min_players, refresh_items=False, regear_options=None, attendance_export=False):
    if day > now().date():
        raise ValueError("不能采集未来日期。")
    stamp = now().strftime("%Y%m%dT%H%M%S_%fZ")
    folder = root / "daily" / day.isoformat()
    run = folder / "runs" / stamp
    run.mkdir(parents=True)
    raw = run / "raw"
    query = urllib.parse.urlencode({"minPlayers": min_players, "start": day.isoformat(), "end": (day + dt.timedelta(days=1)).isoformat()})
    url = BB + "/stats/players/" + urllib.parse.quote(caller, safe="") + "?" + query
    log("查询 {} 的 {} UTC 战报...".format(caller, day))
    try:
        rows = client.get(url)
        if not isinstance(rows, list):
            raise ValueError("玩家战报列表格式异常")
        write_json(raw / "player_battles.json", rows)
        selected = {}
        for row in rows:
            if on_day(row["startedAt"], day):
                selected[str(row["albionId"])] = row
    except (RuntimeError, ValueError, KeyError) as exc:
        write_json(run / "failed.json", {"error": str(exc), "date": day.isoformat()})
        raise RuntimeError("战报发现失败，未更新日报：" + str(exc)) from exc
    catalog, warnings = load_catalog(client, root, refresh_items) if selected else ({}, [])
    battles = []
    for index, row in enumerate(sorted(selected.values(), key=lambda r: r["startedAt"]), 1):
        log("[{}/{}] 战报 {}".format(index, len(selected), row["albionId"]))
        try:
            battle = collect_battle(client, row, caller, catalog, raw)
            if not on_day(battle["start_utc"], day):
                raise ValueError("战斗开始日期与玩家列表不一致")
            if battle["total_players"] < min_players:
                raise ValueError("返回战报人数低于请求筛选条件")
            battles.append(battle)
            warnings.extend("战报 {}：{}".format(battle["battle_id"], w) for w in battle["warnings"])
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            warnings.append("战报 {} 采集失败：{}".format(row["albionId"], exc))
    status = "partial" if warnings else "complete"
    if day == now().date():
        warnings.append("UTC 当日尚未结束，本次只包含查询时已有的战报；结束后请重新采集。")
        if status == "complete":
            status = "provisional"
    report = {
        "schema_version": 1, "kind": "daily", "period": day.isoformat(), "caller": caller,
        "timezone": "UTC", "min_players": min_players, "generated_at": now().isoformat(),
        "status": status, "warnings": warnings, "discovered_battle_ids": sorted(selected),
        "source_url": url, "run_folder": str(run), "battles": battles,
    }
    export_report(run, report)
    previous = read_json(folder / "report.json") if (folder / "report.json").exists() else None
    # Preserve a known complete archive when a later source request is incomplete.
    if previous and previous["status"] == "complete" and status != "complete":
        log("本次不完整，保留原完整日报；本次结果：" + str(run))
    else:
        export_report(folder, report)
    log("日报：" + str(folder / "report.txt"))
    log("本次状态：" + status)
    build_week(root, caller, day, min_players)
    if attendance_export:
        from attendance import export_attendance
        attendance_result = export_attendance(read_json(folder / "report.json"), root)
        report["attendance_status"] = attendance_result["status"]
    if regear_options:
        regear_result = export_regear(read_json(folder / "report.json"), root, regear_options["box_file"], regear_options["settings"])
        report["regear_status"] = regear_result["status"]
    return report


def build_week(root, caller, day, min_players):
    monday = day - dt.timedelta(days=day.weekday())
    dates = [monday + dt.timedelta(days=i) for i in range(7)]
    battles, missing, warnings, sources = [], [], [], []
    seen = set()
    for date in dates:
        path = root / "daily" / date.isoformat() / "report.json"
        if not path.exists():
            missing.append(date.isoformat())
            continue
        report = read_json(path)
        if report["caller"] != caller or report["min_players"] != min_players or report["timezone"] != "UTC":
            missing.append(date.isoformat())
            warnings.append("{} 的采集口径不同，未纳入。".format(date))
            continue
        sources.append({"date": date.isoformat(), "generated_at": report["generated_at"], "status": report["status"]})
        warnings.extend("{}：{}".format(date, w) for w in report["warnings"])
        for battle in report["battles"]:
            if battle["battle_id"] not in seen:
                battles.append(battle)
                seen.add(battle["battle_id"])
    if missing:
        warnings.insert(0, "本周有未采集日期，不能把缺采日期当成零参战。")
    if dates[-1] >= now().date():
        warnings.append("本自然周尚未结束，当前为阶段性汇总。")
    report = {
        "schema_version": 1, "kind": "weekly", "period": "{} ~ {}".format(monday, dates[-1]),
        "caller": caller, "timezone": "UTC", "min_players": min_players,
        "generated_at": now().isoformat(), "status": "partial" if warnings else "complete",
        "warnings": warnings, "missing_days": missing, "source_days": sources,
        "battles": sorted(battles, key=lambda b: b["start_utc"]),
    }
    folder = root / "weekly" / monday.isoformat()
    export_report(folder, report)
    log("周报：" + str(folder / "report.txt"))
    return report


def main():
    parser = argparse.ArgumentParser(description="欧服 caller 公会参战/死亡装备归档（UTC）")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--date", help="采集日期 YYYY-MM-DD（UTC）")
    action.add_argument("--week", help="本地汇总该日期所在的周，YYYY-MM-DD")
    action.add_argument("--regear", help="从已有日报生成补装 CSV，YYYY-MM-DD，不联网")
    action.add_argument("--attendance", help="从已有日报生成彩色出勤网页及每日/逐场 CSV，YYYY-MM-DD，不联网")
    parser.add_argument("--player", help="caller 名称，默认读 config.json")
    parser.add_argument("--min-players", type=int, help="战报最低总人数，默认 10")
    parser.add_argument("--root", type=Path, default=BASE / "data", help="数据根目录")
    parser.add_argument("--refresh-items", action="store_true")
    parser.add_argument("--boxes", type=Path, help="箱子排序文件，默认数据根目录/箱子排序.txt")
    parser.add_argument("--transport", choices=["auto", "powershell", "urllib"], default="auto")
    args = parser.parse_args()
    settings = read_json(BASE / "config.json")
    caller = args.player or settings["player"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", caller):
        raise ValueError("caller 名称只能包含英文字母、数字、下划线或短横线。")
    min_players = args.min_players if args.min_players is not None else settings["min_players"]
    if min_players < 0:
        raise ValueError("最低人数不能小于零。")
    if not args.date and not args.week and not args.regear and not args.attendance:
        print("欧服战报归档 | {} | UTC | 最低 {} 人".format(caller, min_players))
        mode = input("1. 按日采集  2. 本地周汇总  3. 导出补装 CSV  4. 导出出勤名单  0. 退出 [1]：").strip() or "1"
        if mode == "0":
            return 0
        if mode not in {"1", "2", "3", "4"}:
            raise ValueError("请输入 0、1、2、3 或 4。")
        default = (now().date() - dt.timedelta(days=1)).isoformat()
        value = input("输入 UTC 日期 YYYY-MM-DD [{}]：".format(default)).strip() or default
        if mode == "1":
            args.date = value
        elif mode == "2":
            args.week = value
        elif mode == "3":
            args.regear = value
        else:
            args.attendance = value
    day = dt.date.fromisoformat(args.date or args.week or args.regear or args.attendance)
    root = args.root.resolve() / caller
    with run_lock(root):
        if args.date:
            result = collect_day(Client(args.transport), root, caller, day, min_players, args.refresh_items,
                                 {"box_file": args.boxes or args.root / "箱子排序.txt", "settings": settings}, attendance_export=True)
        elif args.week:
            result = build_week(root, caller, day, min_players)
        elif args.regear:
            result = export_regear(read_json(root / "daily" / day.isoformat() / "report.json"), root,
                                   args.boxes or args.root / "箱子排序.txt", settings)
        else:
            from attendance import export_attendance
            result = export_attendance(read_json(root / "daily" / day.isoformat() / "report.json"), root)
    return 0 if all(result.get(field, "complete") == "complete" for field in ["status", "regear_status", "attendance_status"]) else 2


if __name__ == "__main__":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        sys.exit(main())
    except KeyboardInterrupt:
        log("已中断。已保存的原始响应/历史日报保留，重新运行即可。")
        sys.exit(130)
    except Exception as error:
        log("错误：" + str(error))
        sys.exit(1)
