"""Offline daily attendance and per-battle role reports."""
from pathlib import Path
from html import escape
from string import Template

import collector as c


ROLES = {"healer": "奶妈", "dps": "DPS", "tank": "tank", "support": "辅助", "unknown": "未知"}
ROLE_ORDER = {role: index for index, role in enumerate(ROLES)}
SOURCE_ROLES = {"healer": "healer", "range": "dps", "melee": "dps", "tank": "tank", "support": "support"}
DAILY_FIELDS = ["玩家", "12u", "12u角色", "14u", "14u角色", "总场次"]
DETAIL_FIELDS = ["场次", "战报编号", "开始时间UTC", "结束时间UTC", "公会", "玩家", "角色", "武器", "武器编号",
                 "击杀", "死亡", "IP", "治疗量", "伤害量", "战报链接"]


def role_for(value):
    return SOURCE_ROLES.get(str(value or "").strip().lower(), "unknown")


def role_names(roles):
    return " / ".join(ROLES[role] for role in roles)


def build_attendance(report, catalog=None):
    if report.get("kind") != "daily" or report.get("timezone") != "UTC":
        raise ValueError("出勤导出需要 UTC 每日报告。")
    catalog = catalog or {}
    people, seen, battles, details = {}, {}, [], []
    for source in sorted(report["battles"], key=lambda b: c.parse_time(b["start_utc"])):
        bid = str(source["battle_id"])
        if bid in seen:
            if seen[bid] != source:
                raise ValueError("出勤源数据中战报 ID 冲突：" + bid)
            continue
        seen[bid] = source
        if c.parse_time(source["start_utc"]).date().isoformat() != report["period"]:
            raise ValueError("出勤源数据混入其他 UTC 日期的战斗。")
        session = c.regear_session(source["start_utc"])
        battle = {"battle_id": bid, "session": session, "start_utc": source["start_utc"],
                  "end_utc": source.get("end_utc", ""), "guild": source["guild"],
                  "url": "https://europe.albionbb.com/battles/" + bid, "players": []}
        seen_players = {}
        for player in source["players"]:
            name = player["name"].strip()
            key = name.casefold()
            normalized = dict(player, name=key)
            if key in seen_players:
                if seen_players[key] != normalized:
                    raise ValueError("同场战报中的人员记录冲突：{} / {}".format(bid, name))
                continue
            seen_players[key] = normalized
            role = role_for(player.get("role"))
            weapon_id = (player.get("weapon") or {}).get("type") or ""
            weapon = weapon_id
            if weapon_id:
                localized = catalog.get(weapon_id) or catalog.get(weapon_id.split("@", 1)[0])
                if localized:
                    try:
                        family, power, localized = c.box_item_level(weapon_id, localized)
                        weapon = "P{} {}".format(power, localized)
                    except ValueError:
                        weapon = localized
            record = {"battle_id": bid, "session": session, "start_utc": battle["start_utc"],
                      "end_utc": battle["end_utc"], "guild": source["guild"], "player": name,
                      "role": role, "raw_role": player.get("role") or "", "weapon": weapon, "weapon_id": weapon_id,
                      "kills": player.get("kills", ""), "deaths": player.get("deaths", ""),
                      "ip": player.get("ip") or "", "healing": player.get("heal", ""),
                      "damage": player.get("damage", ""), "url": battle["url"]}
            battle["players"].append(record)
            details.append(record)
            if session not in {"12u", "14u"}:
                continue
            person = people.setdefault(key, {"player": name, "sessions": {
                slot: {"attended": False, "roles": set(), "battle_ids": []} for slot in ["12u", "14u"]}})
            attendance = person["sessions"][session]
            attendance["attended"] = True
            attendance["roles"].add(role)
            attendance["battle_ids"].append(bid)
        battle["players"].sort(key=lambda p: (ROLE_ORDER[p["role"]], p["player"].casefold()))
        battles.append(battle)
    daily = []
    for person in people.values():
        for session in person["sessions"].values():
            session["roles"] = sorted(session["roles"], key=ROLE_ORDER.get)
        person["total"] = sum(int(slot["attended"]) for slot in person["sessions"].values())
        person["roles"] = sorted({role for slot in person["sessions"].values() for role in slot["roles"]}, key=ROLE_ORDER.get)
        daily.append(person)
    daily.sort(key=lambda p: (-p["total"], p["player"].casefold()))
    healers = {slot: sorted(p["player"] for p in daily if "healer" in p["sessions"][slot]["roles"]) for slot in ["12u", "14u"]}
    return {"date": report["period"], "caller": report["caller"], "source_status": report["status"],
            "source_generated_at": report["generated_at"], "warnings": report.get("warnings", []),
            "daily": daily, "battles": battles, "details": details, "healers": healers,
            "stats": {"players": len(daily), "attendance_total": sum(p["total"] for p in daily),
                      "12u": sum(int(p["sessions"]["12u"]["attended"]) for p in daily),
                      "14u": sum(int(p["sessions"]["14u"]["attended"]) for p in daily),
                      "battles": len(battles), "battle_records": len(details)}}


def badge(role, text=None):
    return '<span class="role role-{}">{}</span>'.format(role, escape(text or ROLES[role]))


def player_html(name, roles):
    css = "player-name role-text-" + roles[0] if len(roles) == 1 else "player-name"
    return '<strong class="{}">{}</strong>'.format(css, escape(name))


def render_attendance(data):
    daily_rows = []
    for person in data["daily"]:
        cells = [player_html(person["player"], person["roles"])]
        for slot in ["12u", "14u"]:
            session = person["sessions"][slot]
            cells.append('<span class="{}">{}</span>'.format("present" if session["attended"] else "absent", "参与" if session["attended"] else "未记录"))
            cells.append(" ".join(badge(role) for role in session["roles"]) or "")
        cells.append('<strong class="number">{}</strong>'.format(person["total"]))
        daily_rows.append('<tr data-player="{}" data-roles="{}">{}</tr>'.format(
            escape(person["player"].casefold(), quote=True), ",".join(person["roles"]), "".join("<td>" + cell + "</td>" for cell in cells)))
    summary = '<div class="table-scroll"><table id="daily-table"><thead><tr>{}</tr></thead><tbody>{}</tbody></table></div>'.format(
        "".join("<th scope=\"col\">" + escape(label) + "</th>" for label in DAILY_FIELDS),
        "".join(daily_rows) or '<tr><td colspan="6">没有 12u / 14u 出勤记录</td></tr>')
    detail_sections = []
    for battle in data["battles"]:
        rows = []
        for person in battle["players"]:
            cells = [player_html(person["player"], [person["role"]]), badge(person["role"]), escape(str(person["weapon"]))]
            cells.extend(escape(str(person[field])) for field in ["kills", "deaths", "ip", "healing", "damage"])
            rows.append('<tr data-player="{}" data-roles="{}">{}</tr>'.format(
                escape(person["player"].casefold(), quote=True), person["role"], "".join("<td>" + cell + "</td>" for cell in cells)))
        headers = ["玩家", "角色", "武器", "击杀", "死亡", "IP", "治疗量", "伤害量"]
        detail_sections.append('<section class="battle"><div class="battle-heading"><h2>{} UTC · {} · {} 人</h2><a href="{}" target="_blank" rel="noopener noreferrer">战报 {}</a></div><p class="battle-meta">{} · 结束 {}</p><div class="table-scroll"><table><thead><tr>{}</tr></thead><tbody>{}</tbody></table></div></section>'.format(
            escape(c.parse_time(battle["start_utc"]).strftime("%H:%M:%S")), escape(battle["session"]), len(battle["players"]),
            escape(battle["url"], quote=True), escape(battle["battle_id"]), escape(battle["guild"]),
            escape(c.parse_time(battle["end_utc"]).strftime("%H:%M:%S")) if battle["end_utc"] else "未记录",
            "".join('<th scope="col">' + header + "</th>" for header in headers), "".join(rows)))
    status = {"complete": "完整记录", "provisional": "阶段性记录", "partial": "数据待核对"}.get(data["source_status"], data["source_status"])
    metadata = '<span>{}</span><span>数据采集于 {} UTC</span><span>范围：战报中的本公会成员</span>'.format(
        escape(status), escape(c.parse_time(data["source_generated_at"]).strftime("%Y-%m-%d %H:%M:%S")))
    stats = "".join('<div><dt>{}</dt><dd>{}</dd></div>'.format(label, data["stats"][field]) for label, field in [
        ("出勤人员", "players"), ("12u 人数", "12u"), ("14u 人数", "14u"), ("时段出勤人次", "attendance_total"), ("战报场数", "battles")])
    healers = "".join('<div class="healers"><strong>{} 奶妈</strong><span>{}</span></div>'.format(
        slot, " ".join(badge("healer", name) for name in data["healers"][slot]) or "未记录") for slot in ["12u", "14u"])
    warnings = '<details class="warnings"><summary>数据检查 · {} 条</summary><ul>{}</ul></details>'.format(
        len(data["warnings"]), "".join("<li>" + escape(warning) + "</li>" for warning in data["warnings"])) if data["warnings"] else ""
    template = Template(Path(__file__).with_name("attendance_template.html").read_text(encoding="utf-8"))
    return template.substitute(title=escape(data["caller"] + " 出勤 · " + data["date"] + " UTC"),
                               metadata=metadata, stats=stats, healers=healers, warnings=warnings,
                               legend=" ".join(badge(role) for role in ROLES), summary=summary,
                               detail="".join(detail_sections) or "<p>没有战报记录</p>")


def export_attendance(report, root):
    root = Path(root)
    catalog_file = root / "cache" / "item_names.json"
    catalog = c.read_json(catalog_file).get("names", {}) if catalog_file.exists() else {}
    data = build_attendance(report, catalog)
    data["generated_at"] = c.now().isoformat()
    stamp = c.parse_time(data["generated_at"]).strftime("%Y%m%dT%H%M%S_%fZ")
    folder = root / "attendance" / data["date"] / stamp
    suffix = data["date"] + "_12u-14u_" + stamp
    page = folder / ("出勤名单_" + suffix + ".html")
    daily_file = folder / ("每日出勤_" + suffix + ".csv")
    details_file = folder / ("逐场出勤_" + data["date"] + "_" + stamp + ".csv")
    daily_rows = []
    for person in data["daily"]:
        row = {"玩家": person["player"], "总场次": person["total"]}
        for slot in ["12u", "14u"]:
            row[slot] = int(person["sessions"][slot]["attended"])
            row[slot + "角色"] = role_names(person["sessions"][slot]["roles"])
        daily_rows.append(row)
    details = [{"场次": p["session"], "战报编号": p["battle_id"], "开始时间UTC": p["start_utc"],
                "结束时间UTC": p["end_utc"], "公会": p["guild"], "玩家": p["player"], "角色": ROLES[p["role"]],
                "武器": p["weapon"], "武器编号": p["weapon_id"], "击杀": p["kills"], "死亡": p["deaths"],
                "IP": p["ip"], "治疗量": p["healing"], "伤害量": p["damage"], "战报链接": p["url"]} for p in data["details"]]
    c.write_csv(daily_file, DAILY_FIELDS, daily_rows)
    c.write_csv(details_file, DETAIL_FIELDS, details)
    c.write_text(page, render_attendance(data))
    data["files"] = {"html": str(page), "daily_csv": str(daily_file), "detail_csv": str(details_file)}
    data["status"] = "complete" if data["source_status"] == "complete" else "partial"
    c.write_json(folder / "attendance.json", data)
    c.log("出勤：{} 人，时段出勤 {} 人次，逐场 {} 场 / {} 条记录".format(
        data["stats"]["players"], data["stats"]["attendance_total"], data["stats"]["battles"], data["stats"]["battle_records"]))
    c.log("  彩色网页：" + str(page))
    c.log("  每日 CSV：" + str(daily_file))
    c.log("  逐场 CSV：" + str(details_file))
    return data
