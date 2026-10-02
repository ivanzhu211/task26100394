"""规模验证：100 万帖 / 21 品牌 / 8 平台 / 2 年历史，其中 50 万帖集中在同一秒。

用法: python3 bench/scale_bench.py [--posts N] [--keep-db PATH]
记录: 窗口查询 p95、1000 组混合调用总耗时、峰值内存。
"""
from __future__ import annotations

import argparse
import os
import random
import resource
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from socialstats import SocialStats, Post
from socialstats.types import DAY_US

BRANDS = [f"brand{i:02d}" for i in range(21)]
PLATFORMS = [f"plat{i}" for i in range(8)]
SPAN_US = 730 * DAY_US           # 2 年
DENSE_SECOND = 400 * 86_400      # 密集数据所在的秒（第 400 天整）


def gen_posts(total: int, dense: int, rng: random.Random):
    """生成器：dense 个帖子落在同一秒（微秒互不相同），其余随机分布于 2 年。"""
    dense_base = DENSE_SECOND * 1_000_000
    for i in range(total):
        if i < dense:
            ts = dense_base + i  # 同一秒内微秒各不相同
        else:
            ts = rng.randrange(0, SPAN_US)
        yield Post(
            post_id=f"post{i:07d}",
            ts_us=ts,
            platform=rng.choice(PLATFORMS),
            brands=tuple(rng.sample(BRANDS, 3)),
            machine_sentiment=rng.choice((-1, 0, 1)),
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--posts", type=int, default=1_000_000)
    ap.add_argument("--groups", type=int, default=1000)
    ap.add_argument("--keep-db", default=None)
    args = ap.parse_args()

    rng = random.Random(94)
    dense = args.posts // 2
    db_path = args.keep_db or tempfile.mktemp(suffix=".db")
    stats = SocialStats(db_path)

    t0 = time.perf_counter()
    stats.import_posts(gen_posts(args.posts, dense, rng))
    load_s = time.perf_counter() - t0
    print(f"批量装载 {args.posts} 帖: {load_s:.1f}s（不计入考核耗时）")

    # 记录密集秒内的部分帖子编号，供更正使用（更正落在被查询的历史区间内）
    dense_ids = [f"post{i:07d}" for i in range(0, dense, max(1, dense // 10_000))]

    query_ms: list[float] = []
    t0 = time.perf_counter()
    late_id = 0
    for g in range(args.groups):
        # 1) 单帖迟到导入：落在正在查询的历史区间内
        ts = DENSE_SECOND * 1_000_000 + rng.randrange(0, 1_000_000)
        stats.import_posts(
            [Post(f"late{late_id:06d}", ts, rng.choice(PLATFORMS),
                  tuple(rng.sample(BRANDS, 3)), rng.choice((-1, 0, 1)))]
        )
        late_id += 1
        # 2) 人工情绪更正：目标为密集秒内的历史帖
        pid = rng.choice(dense_ids)
        if rng.random() < 0.2:
            stats.clear_human_sentiment(pid)
        else:
            stats.set_human_sentiment(pid, rng.choice((-1, 0, 1)))
        # 3) 窗口查询：终点落在密集秒内部，平台条件变化
        end_us = DENSE_SECOND * 1_000_000 + rng.randrange(1, 1_000_000)
        platform = rng.choice([None] + PLATFORMS)
        days = rng.choice((7, 30, 90))
        q0 = time.perf_counter()
        stats.query(end_us, days, platform)
        query_ms.append((time.perf_counter() - q0) * 1000)
    mixed_s = time.perf_counter() - t0

    peak_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)
    p95 = statistics.quantiles(query_ms, n=20)[18]
    print(f"窗口查询 p95: {p95:.1f} ms（要求 ≤ 100 ms），max {max(query_ms):.1f} ms")
    print(f"混合调用 {args.groups} 组总耗时: {mixed_s:.1f} s（要求 ≤ 60 s）")
    print(f"峰值内存: {peak_mib:.0f} MiB（要求 ≤ 512 MiB）")

    stats.close()
    if not args.keep_db:
        for suffix in ("", "-wal", "-shm"):
            os.unlink(db_path + suffix) if os.path.exists(db_path + suffix) else None


if __name__ == "__main__":
    main()
