"""A self-contained illustrated procurement and box allocation report."""
import base64
import collections
from concurrent.futures import ThreadPoolExecutor
import hashlib
from html import escape
import json
from pathlib import Path
from string import Template
import time
import urllib.request

import collector as c


def build_regear_page_data(report, battles, boxes, settings):
    people, totals, events = {}, {}, {}
    slots = {slot: index for index, slot in enumerate(c.REGEAR_SLOTS)}
    for battle in battles:
        session = c.regear_session(battle["start_utc"])
        for death in battle["deaths"]:
            eid = str(death["event_id"])
            if eid in events:
                if events[eid] != death:
                    raise ValueError("补装网页中的死亡事件冲突：" + eid)
                continue
            events[eid] = death
            if any(c.same(death["victim"], manager) for manager in settings.get("no_box_players", [])):
                continue
            assignment = c.box_for_player(death["victim"], boxes, settings.get("no_box_players", []))
            person = people.setdefault(death["victim"].casefold(), {
                "name": death["victim"], "box": assignment["箱子编号"], "status": assignment["分箱状态"],
                "candidate_boxes": assignment["候选箱号"], "items": {}, "missing_events": [],
            })
            if death["equipment_status"] != "complete":
                person["missing_events"].append(eid)
                continue
            for item in death["items"]:
                if item["slot"] not in c.REGEAR_SLOTS:
                    continue
                family, power, name = c.box_item_level(item["item_id"], item["name"])
                key = family + "|P" + str(power)
                prototype = {"key": key, "family": family, "power": power, "name": name,
                             "slot": item["slot"], "slot_name": c.SLOTS[item["slot"]],
                             "icon_key": "T4_" + family, "quantity": 0, "by_session": {}}
                for collection in [totals, person["items"]]:
                    gear = collection.setdefault(key, dict(prototype, by_session={}))
                    gear["quantity"] += item["count"]
                    gear["by_session"][session] = gear["by_session"].get(session, 0) + item["count"]
    order = lambda item: (slots[item["slot"]], item["name"], item["power"], item["family"])
    for person in people.values():
        person["items"] = sorted(person["items"].values(), key=order)
    people_list = sorted(people.values(), key=lambda p: p["name"].casefold())
    visible_columns = max((box["column"] for box in boxes if box["label"]), default=1)
    visible_rows = max((box["row"] for box in boxes), default=1)
    owner_counts = collections.Counter(box["owner"].casefold() for box in boxes if box["owner"])
    grid = []
    for box in boxes:
        if box["column"] > visible_columns:
            continue
        person = people.get((box["owner"] or "").casefold())
        assigned = bool(person and person["status"] == "已分配" and person["box"] == box["box"])
        grid.append({**box, "person": person["name"] if assigned else None,
                     "duplicate": bool(box["owner"] and owner_counts[box["owner"].casefold()] > 1)})
    pending = [person["name"] for person in people_list if person["status"] != "已分配"]
    expected = {key: item["quantity"] for key, item in totals.items()}
    distributed = collections.Counter()
    for person in people_list:
        for item in person["items"]:
            distributed[item["key"]] += item["quantity"]
    if expected != dict(distributed):
        raise ValueError("网页总补装与个人补装数量不一致。")
    placed = collections.Counter(box["person"] for box in grid if box["person"])
    if any(count != 1 for count in placed.values()):
        raise ValueError("同一人的装备不能放入多个箱子。")
    assigned_people = {p["name"] for p in people_list if p["status"] == "已分配"}
    if assigned_people != set(placed):
        raise ValueError("已分配人员的箱子不在网页网格中。")
    return {"date": report["period"], "caller": report["caller"], "source_status": report["status"],
            "source_generated_at": report["generated_at"], "sessions": sorted({c.regear_session(b["start_utc"]) for b in battles}),
            "totals": sorted(totals.values(), key=order), "people": people_list, "boxes": grid,
            "rows": visible_rows, "columns": visible_columns, "pending": pending,
            "slots": [{"key": slot, "name": c.SLOTS[slot]} for slot in c.REGEAR_SLOTS],
            "item_units": sum(expected.values()), "counts_reconciled": True}


def load_item_icons(root, icon_keys):
    cache = Path(root) / "cache" / "item_icons"
    cache.mkdir(parents=True, exist_ok=True)

    def load(key):
        url = "https://render.albiononline.com/v1/item/" + key + ".png?size=96"
        path = cache / (hashlib.sha256(url.encode("utf-8")).hexdigest() + ".png")
        if path.exists():
            content = path.read_bytes()
            if content.startswith(b"\x89PNG\r\n\x1a\n"):
                return key, "data:image/png;base64," + base64.b64encode(content).decode("ascii"), None
        error = None
        for attempt in range(2):
            try:
                request_url = url if attempt == 0 else url.replace("size=96", "size=128")
                request = urllib.request.Request(request_url, headers={"User-Agent": "AlbionGuildReports/1.0"})
                with urllib.request.urlopen(request, timeout=18) as response:
                    content = response.read()
                if not content.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError("图标响应不是 PNG")
                temp = path.with_name(path.name + ".tmp")
                temp.write_bytes(content)
                temp.replace(path)
                return key, "data:image/png;base64," + base64.b64encode(content).decode("ascii"), None
            except (OSError, ValueError) as exc:
                error = str(exc)
                if attempt == 0:
                    time.sleep(0.5)
        return key, None, key + ": " + str(error)

    icons, errors = {}, []
    keys = sorted(set(icon_keys))
    if keys:
        c.log("获取装备图标：{} 种（命中缓存后无需联网）".format(len(keys)))
    with ThreadPoolExecutor(max_workers=4) as executor:
        for index, (key, uri, error) in enumerate(executor.map(load, keys), 1):
            if uri:
                icons[key] = uri
            if error:
                errors.append(error)
            if index % 12 == 0 or index == len(keys):
                c.log("  图标 {}/{}".format(index, len(keys)))
    return icons, errors


def render_regear_page(data, icons):
    template = Template(Path(__file__).with_name("regear_template.html").read_text(encoding="utf-8"))
    json_text = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    icon_text = json.dumps(icons, ensure_ascii=False).replace("<", "\\u003c")
    return template.substitute(title=escape(data["caller"] + " 补装 · " + data["date"] + " UTC"),
                               data_json=json_text, icons_json=icon_text)


def export_regear_page(report, battles, boxes, tables, root, folder, group, stamp, settings):
    data = build_regear_page_data(report, battles, boxes, settings)
    if data["item_units"] != tables["item_units"]:
        raise ValueError("补装网页总量与原始分配统计不一致。")
    data["warnings"] = tables["warnings"] + report.get("warnings", [])
    data["generated_at"] = c.now().isoformat()
    icons, errors = load_item_icons(root, [item["icon_key"] for item in data["totals"]])
    data["icon_errors"] = errors
    page = folder / ("补装清单_{}_{}_{}.html".format(report["period"], group, stamp))
    c.write_text(page, render_regear_page(data, icons))
    c.write_json(folder / "regear_page.json", data)
    c.log("  补装网页：" + str(page))
    if errors:
        c.log("  提示：{} 种图标未取得，名称、P 等级和数量仍保留。".format(len(errors)))
    return page
