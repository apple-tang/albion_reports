# ZZCall 公会战报归档

双击本目录的 **run.cmd**，选择 `1`，输入 `YYYY-MM-DD` 格式的 **UTC 日期**。
直接回车默认采集 UTC 昨天。首次运行需要下载物品名称字典；无需安装 Python 第三方包。
本机已用现有 Python 3.9 和 PowerShell 实测。工具只读取公开战报，不连接游戏客户端。

本仓库同时包含代码和 `data/` 中的已采集战报、物品名称缓存及各次运行的原始响应。当前示例日期为 2026-10-30 UTC：6 场战斗、42 名去重参战人员、18 次死亡。后续运行生成的数据保存在本地，可通过 Git 提交同步到仓库。

## 看结果

每日数据：`data/ZZCall/daily/日期/`

- `report.txt`：汇总人数、逐人场次/天数/击杀/死亡、每场名单、完整性提示。
- `deaths.txt`：逐次死亡的中文装备、阶级和附魔（例如 6.2）、品质、数量。
- `equipment_totals.txt`：按物品编号和品质累计的装备数量。
- `report.json`：完整结构化报告。
- `attendance.jsonl`、`deaths.jsonl`、`equipment.jsonl`：每行一条 JSON，都是 UTF-8 文本，方便以后导入统计系统。
- `runs/运行时间/`：本次采集的独立快照，`raw/` 保留接口原始响应，方便核查。

周报：`data/ZZCall/weekly/周一日期/`，文件格式同上。
每次按日采集都会刷新那一周的汇总。也可以双击后选择 `2`，输入该周任意一天，只读取本地日报生成周报，不会联网补采。
一周按 UTC 周一至周日。缺少某一天的记录时会列出缺采日期，不能把缺采当成零参战。
要得到完整周报，请逐日采集该周七天；没有战报的日期也运行一次，保存已查询且无战报的记录。

## 统计口径

- 默认欧服、ZZCall、最低 10 人，与网站玩家战报页面的默认最低人数一致。
- **按每场战报里的 ZZCall 所属公会统计**，不是当前公会的全体成员。公会变化不会用今天的公会覆盖历史归属。
- 人员从 Details 的参战表读取，包含战报列出的零击杀、零死亡成员；公开战报没有记录的人无法推断是否到场。
- 跨 UTC 午夜的战斗全部归到开始日，因此次日凌晨的死亡也可能保存在前一天的日报里。
- 死亡从 Kills 的死者公会筛选，然后获取对应 Open 详情。重复运行按战报和 EventId 去重，不会把装备重复累计。
- 装备来自编号和中文物品字典，无需 OCR。`T6_…@2` 是 6.2；`Quality=4` 是卓越品质，两者单独记录。字典缺失的新物品尝试由游戏物品接口补全中文名。
- 保存十个穿戴槽位（主副手、头、衣、鞋、包、披风、坐骑、药水、食物），不统计背包库存。
- **这里的装备是死亡时穿戴快照，不表示实际掉落、损毁、被捡走或最终损失。**
- 接口未给 `Count` 时按单件穿戴物记 1，并在 JSON 中用 `count_reported=false` 标明；药水/食物有数量时保留接口数量。

## 完整性与重复采集

`complete` 表示本次发现的战报均采集成功，Kills 数量、公会汇总、成员死亡数量和装备详情通过核对。
这只代表当前数据源中的记录通过检查，不承诺网站没有漏收游戏事件。
`partial` 表示有请求失败、数量不符或名称未解析，请看 `report.txt` 的具体提示。
`provisional` 表示 UTC 当天还没结束，应在结束后重新采集。
网站近期战报也可能延迟更新，最好采集昨天的数据；必要时再次采集同一天。

每次运行都保留独立快照。如果已有 `complete` 日报，后续请求失败产生的非完整结果只保存在本次 `runs/` 中，原完整日报继续用于周汇总。
网络中断后重跑即可，历史文件保留。请求有间隔和有限重试，同一数据目录只允许一次采集/汇总运行。
公开接口可能限流、调整或不再保留较旧事件，建议及时逐日归档；缺失装备不会靠图片猜测补齐。

## 命令行

在本目录打开 PowerShell：

```powershell
py -3 collector.py --date 2026-10-30
py -3 collector.py --week 2026-10-30
py -3 collector.py --date 2026-10-30 --refresh-items
py -3 -m unittest -v
```

修改 `config.json` 可以更换 `player` 和 `min_players`。也可使用 `--player`、`--min-players`。
同一周请使用相同最低人数口径，周汇总会拒绝混用筛选条件不同的日报。
Windows 默认通过 PowerShell 发 HTTPS 请求，本机直接 curl/urllib 访问 AlbionBB 会收到 403，而 PowerShell 可正常请求。
其他系统默认用 urllib，也可手动指定 `--transport urllib` 或 `--transport powershell`。
退出码：0=完整，2=部分/临时结果，1=错误，130=手动中断。

## 数据来源

- 网站与战报接口：[AlbionBB Europe](https://europe.albionbb.com/players/ZZCall/battles)。
- 装备详情备用源：[Albion Europe Gameinfo](https://gameinfo-ams.albiononline.com/api/gameinfo/events/455392129)。
- 中文物品字典：[ao-data/ao-bin-dumps](https://github.com/ao-data/ao-bin-dumps/blob/master/formatted/items.json)，本地精简缓存每七天更新；较早的 `broderickhyman` 仓库已归档，不再作为默认源。

不创建定时任务；每天由你点击运行。运行方案与接口核实记录见 `IMPLEMENTATION.md`。
