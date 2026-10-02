# social_stats — 离线社媒声量与情绪统计库

Python + SQLite 实现的离线统计库，供上层分析工具直接调用，数据保存在
调用方指定的 SQLite 文件中。仅依赖 Python 标准库（3.10+），无需任何
外部服务。

品牌识别与机器情绪打分在上游完成，本库只做三件事：**批量导入**、
**设置/撤销人工情绪**、**窗口查询**。

## 快速开始

```python
from social_stats import SocialStats, Post, WindowDays

stats = SocialStats("/path/to/stats.db")   # 文件不存在则自动创建

# 1) 批量导入（只追加新帖；时间可乱序、可早于全部已存记录）
stats.import_posts([
    Post(post_id="abc123",
         ts_us=1_760_000_000_123_456,      # UTC 整数微秒
         platform="weibo",
         brands=frozenset({"acme", "rival"}),
         sentiment=1),                      # 机器情绪: -1 / 0 / 1
])

# 2) 人工情绪：可反复修改；撤销后恢复机器值。返回 False 表示帖子不存在
stats.set_human_sentiment("abc123", -1)
stats.clear_human_sentiment("abc123")

# 3) 窗口查询：任意 UTC 微秒终点 + 7/30/90 天 + 单平台或全部平台
for row in stats.query(end_us=1_760_000_000_500_000,
                       window=WindowDays.DAYS_7,   # 或 30 / 90
                       platform=None):             # None = 全部平台
    print(row.brand, row.mentions, row.share, row.avg_sentiment)

stats.close()                                # 也可用 with 语句
```

## 语义约定

- 窗口为 `[end_us - window*86400s, end_us)`：含起点、不含终点，
  一天固定 86400 秒，不按自然日取整。
- 同一帖子对同一品牌只计一次，对关联的不同品牌分别计一次。
- 声量占比的分母是窗口内各品牌提及数之和（非去重帖子数）；
  没有提及的品牌占比为 `0.0`、平均情绪为 `None`。
- 平均情绪使用有效情绪：存在人工值时用人工值，否则用机器值；
  更正影响该帖关联的所有品牌，但不改变提及数。
- 重复导入同一 `post_id` 抛出 `ValueError`，数据库状态不变。
- 所有写操作在返回前已提交：成功的导入与更正立即对下一次查询可见，
  关闭并重新打开数据库后结果不变。

## 性能设计

查询成本与历史总量无关，靠三级预聚合实现：

| 层级 | 粒度 | 用途 |
|---|---|---|
| `rollup_day` | 天 × 平台 × 品牌 | 窗口内完整天 |
| `rollup_sec` | 秒 × 平台 × 品牌 | 窗口两端不足一天的部分 |
| `rollup_dense` | 10ms 桶 × 平台 × 品牌 | 端点切入"密集秒"（单秒 ≥4096 帖）时 |

边缘不足 10ms 的部分回退到原始帖子扫描（数据量极小）。导入与人工更正
以增量方式同步维护各级聚合（更正是 O(品牌数) 的 `sent_sum` 增减），
因此迟到补录与反复更正不会随历史积累变慢。

## 测试与规模验证

```bash
python3 -m unittest discover -s tests   # 正确性测试（对照逐帖朴素实现）
python3 bench/scale_check.py            # 规模验证（约 20 秒；--keep 保留 db）
```

正确性测试覆盖：多品牌重叠、窗口两端微秒边界、乱序/早于全部记录的
迟到导入、反复设置与撤销人工情绪、密集秒内部终点、关闭重开持久化，
以及"一帖两品牌各计 1 次、占比各 50%"等验收用例。

规模验证场景（可复现，固定随机种子）：100 万帖、21 品牌、8 平台、
平均每帖 3 品牌、历史跨度 2 年，其中 50 万帖集中于同一秒且微秒互不相同；
1000 组混合调用（单帖迟到导入 + 人工更正 + 窗口查询），查询终点持续变化，
一半终点落在密集秒内部。本机实测（Apple Silicon）：

| 指标 | 实测 | 要求 |
|---|---|---|
| 窗口查询 p95 | 14.1 ms（p50 1.4 ms，max 21.9 ms） | ≤ 100 ms |
| 1000 组混合调用 | 3.9 s | ≤ 60 s |
| 峰值内存 | 131 MiB | ≤ 512 MiB |
| 首次批量装载 | 16.9 s（不计入上述指标） | — |
