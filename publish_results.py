"""Maintain the small set of user-facing files tracked by Git."""
import argparse
import datetime as dt
import json
from pathlib import Path
import shutil


CATEGORIES = {
    "attendance": {"出勤名单": ".html"},
    "regear": {"补装清单": ".html"},
}
REQUIRED = {category: set(rules) for category, rules in CATEGORIES.items()}


def publish_result_set(root, day, category, sources):
    root = Path(root).resolve()
    if dt.date.fromisoformat(day).isoformat() != day:
        raise ValueError("结果日期应为 YYYY-MM-DD。")
    rules = CATEGORIES[category]
    files, found = [], set()
    for source in sources:
        source = Path(source).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            raise ValueError("结果源文件不在当前数据目录内或不存在：" + str(source))
        matched = [prefix for prefix, suffix in rules.items()
                   if source.name.startswith(prefix + "_" + day + "_") and source.suffix.lower() == suffix]
        if len(matched) != 1:
            raise ValueError("不允许同步的结果文件：" + source.name)
        found.add(matched[0])
        files.append(source)
    if not REQUIRED[category].issubset(found) or len({p.name for p in files}) != len(files):
        raise ValueError("本组结果不齐全或有重复文件名，保留已有同步结果。")
    destination = root / "results" / day
    if not destination.resolve().is_relative_to(root):
        raise ValueError("结果目录不能指向当前数据目录之外。")
    destination.mkdir(parents=True, exist_ok=True)
    keep = {source.name for source in files}
    for source in files:
        target = destination / source.name
        if source == target.resolve():
            raise ValueError("结果源文件不能是同步目录中的文件。")
        temp = target.with_name(target.name + ".tmp")
        shutil.copyfile(source, temp)
        temp.replace(target)
    for prefix, suffix in rules.items():
        for previous in destination.glob(prefix + "_" + day + "_*" + suffix):
            if previous.name not in keep:
                try:
                    previous.unlink()
                except PermissionError:
                    print("旧结果正在使用，暂留本地；关闭文件后再次整理即可：" + str(previous))
    return [str(destination / source.name) for source in files]


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def publish_saved_results(root):
    root = Path(root).resolve()
    published = []
    attendance = root / "attendance"
    if attendance.is_dir():
        for day in sorted(p for p in attendance.iterdir() if p.is_dir()):
            for metadata in sorted(day.glob("*/attendance.json"), reverse=True):
                data = read_json(metadata)
                files = data.get("files", {})
                if files.get("html") and Path(files["html"]).is_file():
                    published.extend(publish_result_set(root, day.name, "attendance", [files["html"]]))
                    break
    regear = root / "regear"
    if regear.is_dir():
        for day in sorted(p for p in regear.iterdir() if p.is_dir()):
            manifests = list(day.glob("*/*/manifest.json"))
            for stamp in sorted({p.parent.name for p in manifests}, reverse=True):
                batch = [read_json(p) for p in manifests if p.parent.name == stamp]
                files = [data["regear_html"] for data in batch if data.get("regear_html")]
                if not files:
                    continue
                if all(Path(p).is_file() for p in files):
                    published.extend(publish_result_set(root, day.name, "regear", files))
                    break
    return published


def remove_csv_outputs(root):
    root = Path(root).resolve()
    removed, blocked = [], []
    for name in ["attendance", "regear", "results"]:
        directory = root / name
        if not directory.resolve().is_relative_to(root):
            raise ValueError("CSV 清理目录不能指向数据目录之外。")
        if not directory.exists():
            continue
        paths = list(directory.rglob("*.csv"))
        if any(not path.resolve().is_relative_to(directory.resolve()) for path in paths):
            raise ValueError("CSV 清理路径不能指向指定结果目录之外。")
        for path in paths:
            try:
                path.unlink()
                removed.append(str(path))
            except PermissionError:
                blocked.append(str(path))
    return {"removed": removed, "blocked": blocked}


if __name__ == "__main__":
    base = Path(__file__).resolve().parent
    settings = read_json(base / "config.json")
    parser = argparse.ArgumentParser(description="整理已有归档，只保留每个日期最新的同步用结果。")
    parser.add_argument("--root", type=Path, default=base / "data" / settings["player"])
    parser.add_argument("--remove-csv", action="store_true", help="删除已停用的生成 CSV，不影响其他文件")
    args = parser.parse_args()
    paths = publish_saved_results(args.root)
    print("同步用结果 {} 个文件：".format(len(paths)))
    for path in paths:
        print(path)
    if args.remove_csv:
        result = remove_csv_outputs(args.root)
        print("已删除 {} 个旧 CSV，{} 个仍被占用。".format(len(result["removed"]), len(result["blocked"])))
        for path in result["blocked"]:
            print("仍被占用：" + path)
