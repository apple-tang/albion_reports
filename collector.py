"""Archive AlbionBB caller battles and guild death equipment as UTF-8 text."""
import argparse
import collections
import contextlib
import datetime as dt
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


def collect_day(client, root, caller, day, min_players, refresh_items=False):
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
    parser.add_argument("--player", help="caller 名称，默认读 config.json")
    parser.add_argument("--min-players", type=int, help="战报最低总人数，默认 10")
    parser.add_argument("--root", type=Path, default=BASE / "data", help="数据根目录")
    parser.add_argument("--refresh-items", action="store_true")
    parser.add_argument("--transport", choices=["auto", "powershell", "urllib"], default="auto")
    args = parser.parse_args()
    settings = read_json(BASE / "config.json")
    caller = args.player or settings["player"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", caller):
        raise ValueError("caller 名称只能包含英文字母、数字、下划线或短横线。")
    min_players = args.min_players if args.min_players is not None else settings["min_players"]
    if min_players < 0:
        raise ValueError("最低人数不能小于零。")
    if not args.date and not args.week:
        print("欧服战报归档 | {} | UTC | 最低 {} 人".format(caller, min_players))
        mode = input("1. 按日采集  2. 本地周汇总  0. 退出 [1]：").strip() or "1"
        if mode == "0":
            return 0
        if mode not in {"1", "2"}:
            raise ValueError("请输入 0、1 或 2。")
        default = (now().date() - dt.timedelta(days=1)).isoformat()
        value = input("输入 UTC 日期 YYYY-MM-DD [{}]：".format(default)).strip() or default
        if mode == "1":
            args.date = value
        else:
            args.week = value
    day = dt.date.fromisoformat(args.date or args.week)
    root = args.root.resolve() / caller
    with run_lock(root):
        if args.date:
            result = collect_day(Client(args.transport), root, caller, day, min_players, args.refresh_items)
        else:
            result = build_week(root, caller, day, min_players)
    return 0 if result["status"] == "complete" else 2


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
