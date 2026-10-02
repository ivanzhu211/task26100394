# socialstats — 离线社媒声量与情绪统计库

Python + SQLite 实现，由上层分析工具直接调用，数据保存在调用方指定的 SQLite
文件中。品牌识别与机器情绪打分在上游完成，本库只提供三类操作：批量导入、
设置/撤销人工情绪、窗口查询。

## 使用

```python
from socialstats import SocialStats, Post

with SocialStats("stats.db") as stats:
    # 1) 批量导入（只追加新帖，重复编号忽略；时间可乱序、可早于已有记录）
    stats.import_posts([
        Post(
            post_id="globally-unique-id",
            ts_us=1_758_000_000_000_000,   # UTC 整数微秒
            platform="weibo",
            brands=("mybrand", "rival"),   # 非空品牌集合
            machine_sentiment=1,           # -1 / 0 / 1
        ),
    ])

    # 2) 人工情绪：可反复覆盖；撤销后恢复机器值。影响该帖关联的所有品牌
    stats.set_human_sentiment("globally-unique-id", -1)
    stats.clear_human_sentiment("globally-unique-id")

    # 3) 窗口查询：[end_us - 7/30/90 天, end_us)，含起点不含终点；
    #    platform=None 表示全部平台
    for st in stats.query(end_us=1_758_000_000_000_000, window_days=7, platform=None):
        print(st.brand, st.mentions, st.share, st.avg_sentiment)
```

`query` 返回全部已知品牌的 `BrandStat`：

- `mentions`：窗口内关联该品牌的帖子数（同一帖对同一品牌只计一次）；
- `share`：声量占比，分母为窗口内各品牌提及数之和（非去重帖子数）；
- `avg_sentiment`：平均有效情绪（有人工值用人工值），无提及时为 `None`，
  占比为 `0`。

所有操作成功返回即已持久化，关闭重开后结果不变。

## 设计

- 原始表 `posts` / `post_brands` 只追加；人工情绪是 `posts.human_sent` 一列。
- 两级预聚合表 `agg_day`（按 UTC 天）与 `agg_ms`（按毫秒），按
  `(时间桶, 平台, 品牌)` 维护提及数与有效情绪之和。导入与人工更正只做增量
  更新，查询耗时不随历史积累退化。
- 查询 = 完整天求和（`agg_day`）+ 两端不足一天的毫秒求和（`agg_ms`）+
  两端不足一毫秒的残余区间回扫原始表（微秒互不相同，每段至多 1000 帖），
  因此任意微秒终点都精确，且 50 万帖同秒时终点落在该秒内部也不退化。

## 测试与规模验证

```bash
python3 -m pytest tests/ -q        # 正确性：与直接逐帖计算对比
python3 bench/scale_bench.py       # 规模验证：100 万帖，50 万帖同秒
```

本机实测（1M 帖 / 21 品牌 / 8 平台 / 2 年跨度，50 万帖集中于同一秒且查询
终点落在该秒内部）：

| 指标 | 要求 | 实测 |
| --- | --- | --- |
| 窗口查询 p95 | ≤ 100 ms | 28.3 ms |
| 1000 组混合调用（迟到导入+更正+查询） | ≤ 60 s | 7.7 s |
| 峰值内存 | ≤ 512 MiB | 49 MiB |
