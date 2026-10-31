# 战报采集实现路径

## 已确定的口径

- 欧服，caller 为 ZZCall；日期使用 UTC。
- 只选 caller 出现的战报，默认保持网站的最低 10 人筛选。
- 以每场战报中的 caller 公会为准，统计该会的参战名单；不是查询当前公会名单。
- 日期按战斗开始时间归属，跨午夜战斗整场归到开始日。
- 参战表示战报记录到的人员，不等于游戏中所有到场人员。
- 死亡装备表示死者当时的穿戴快照，不表示实际掉落、损毁或可回收物品。

## 数据路径（2026-10-31 实测）

1. `https://api.albionbb.com/eu/stats/players/ZZCall`：玩家战报，参数 `start/end/minPlayers`；客户端再次按 UTC 过滤。
2. `/battles/{id}`：战斗详情和参战成员。
3. `/battles/kills?ids={id}`：完整击杀列表。
4. `/kills/{eventId}`：Open 装备详情。失败时尝试欧服 Gameinfo `/events/{eventId}`。
5. `ao-data/ao-bin-dumps` 的 `formatted/items.json`：物品中文名；`Type` 的 `T6_…@2` 表示 6.2，`Quality` 单独记录。初次试验使用的 `broderickhyman` 旧仓库已归档，缺失新物品，已切换来源；新物品另由 Gameinfo `/items/{id}/data` 兜底。

## 交付

- Python 3.9+ 标准库脚本，无需 pip；Windows 使用 PowerShell 的 HTTPS 请求兼容本站。
- 双击 `run.cmd`，选择按日采集或本地周汇总。
- 每日保存人员、逐次死亡、逐件装备 TXT/JSONL、完整报告 JSON、原始响应。
- EventId 去重；保留每次运行快照；校验击杀总数与每人死亡数量。
- 每次采集刷新该自然周的本地汇总；缺采日期、未结束日期、请求失败与数量不符均明确标注。
- 不创建定时任务。用户每天自行点击，周报从本地记录生成。

## 验证

- 用固定数据验证 UTC 边界、公会归属、重复事件、附魔/品质、周统计及缺失日期。
- 实际采集 2026-10-30，对照网站的六场战报，检查人数与死亡装备完整性。
