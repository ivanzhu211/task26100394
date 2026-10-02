"""小规模数据下与直接逐帖计算的结果对比测试。"""
from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from socialstats import SocialStats, Post
from socialstats.types import DAY_US


def naive_query(posts, human, brands_all, end_us, window_days, platform):
    """直接逐帖计算的参考实现。"""
    start = end_us - window_days * DAY_US
    counts = {b: [0, 0] for b in brands_all}
    for p in posts.values():
        if not (start <= p.ts_us < end_us):
            continue
        if platform is not None and p.platform != platform:
            continue
        eff = human.get(p.post_id, p.machine_sentiment)
        for b in dict.fromkeys(p.brands):
            counts[b][0] += 1
            counts[b][1] += eff
    total = sum(c[0] for c in counts.values())
    return {
        b: (c[0], (c[0] / total) if total else 0.0, (c[1] / c[0]) if c[0] else None)
        for b, c in counts.items()
    }


def to_map(stats):
    return {s.brand: (s.mentions, s.share, s.avg_sentiment) for s in stats}


class CorrectnessTest(unittest.TestCase):
    def setUp(self):
        self.db = Path(self.id().split(".")[-1] + ".db")
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.db) + suffix).unlink(missing_ok=True)
        self.stats = SocialStats(str(self.db))

    def tearDown(self):
        self.stats.close()
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.db) + suffix).unlink(missing_ok=True)

    def assert_query_matches(self, posts, human, brands, end_us, days, platform=None):
        got = to_map(self.stats.query(end_us, days, platform))
        want = naive_query(posts, human, brands, end_us, days, platform)
        for b in want:
            g, w = got[b], want[b]
            self.assertEqual(g[0], w[0], f"{b} mentions")
            self.assertAlmostEqual(g[1], w[1], msg=f"{b} share")
            if w[2] is None:
                self.assertIsNone(g[2], f"{b} avg")
            else:
                self.assertAlmostEqual(g[2], w[2], msg=f"{b} avg")

    def test_single_post_two_brands(self):
        self.stats.import_posts([Post("p1", 1_000_000, "weibo", ("A", "B"), 1)])
        got = to_map(self.stats.query(2_000_000, 7))
        self.assertEqual(got["A"], (1, 0.5, 1.0))
        self.assertEqual(got["B"], (1, 0.5, 1.0))

    def test_window_microsecond_boundaries(self):
        end = 10 * DAY_US + 123_456_789
        start = end - 7 * DAY_US
        posts = [
            Post("at_start", start, "p", ("A",), 1),          # 含起点
            Post("before_start", start - 1, "p", ("A",), 1),  # 起点前 1µs，排除
            Post("at_end", end, "p", ("A",), 1),              # 不含终点
            Post("before_end", end - 1, "p", ("A",), 1),      # 终点前 1µs，含
        ]
        self.stats.import_posts(posts)
        got = to_map(self.stats.query(end, 7))
        self.assertEqual(got["A"][0], 2)

    def test_correction_and_undo_late_import(self):
        base = 5 * DAY_US
        self.stats.import_posts([Post("p1", base, "w", ("A", "B"), 1)])
        self.assert_query_matches(
            {"p1": Post("p1", base, "w", ("A", "B"), 1)}, {}, {"A", "B"}, base + DAY_US, 7
        )
        # 迟到导入（早于已存记录）
        late = Post("p0", base - 3 * DAY_US, "w", ("A",), -1)
        self.stats.import_posts([late])
        # 反复覆盖人工情绪
        self.stats.set_human_sentiment("p1", -1)
        self.stats.set_human_sentiment("p1", 0)
        self.stats.set_human_sentiment("p1", -1)
        posts = {"p1": Post("p1", base, "w", ("A", "B"), 1), "p0": late}
        self.assert_query_matches(posts, {"p1": -1}, {"A", "B"}, base + DAY_US, 7)
        # 撤销，恢复机器值
        self.stats.clear_human_sentiment("p1")
        self.assert_query_matches(posts, {}, {"A", "B"}, base + DAY_US, 7)

    def test_persistence_across_reopen(self):
        self.stats.import_posts([Post("p1", 1_000_000, "w", ("A", "B"), 0)])
        self.stats.set_human_sentiment("p1", 1)
        before = to_map(self.stats.query(2_000_000, 7))
        self.stats.close()
        self.stats = SocialStats(str(self.db))
        after = to_map(self.stats.query(2_000_000, 7))
        self.assertEqual(before, after)
        self.assertEqual(after["A"], (1, 0.5, 1.0))

    def test_no_mention_brand(self):
        self.stats.import_posts([Post("p1", 1_000_000, "w", ("A",), 1)])
        self.stats.import_posts([Post("p2", 100 * DAY_US, "w", ("B",), 1)])
        got = to_map(self.stats.query(2_000_000, 7))
        self.assertEqual(got["B"], (0, 0.0, None))

    def test_randomized_against_naive(self):
        rng = random.Random(20261003)
        brands = [f"B{i}" for i in range(6)]
        platforms = ["w", "x", "y"]
        posts = {}
        human = {}
        end0 = 200 * DAY_US
        for i in range(400):
            ts = rng.randrange(0, end0 + 10 * DAY_US)
            p = Post(
                f"p{i}",
                ts,
                rng.choice(platforms),
                tuple(rng.sample(brands, rng.randint(1, 3))),
                rng.choice((-1, 0, 1)),
            )
            posts[p.post_id] = p
            self.stats.import_posts([p])
            if rng.random() < 0.3:
                v = rng.choice((-1, 0, 1))
                human[p.post_id] = v
                self.stats.set_human_sentiment(p.post_id, v)
            if rng.random() < 0.1 and p.post_id in human:
                del human[p.post_id]
                self.stats.clear_human_sentiment(p.post_id)
        for _ in range(50):
            end = rng.randrange(0, end0 + 20 * DAY_US)
            days = rng.choice((7, 30, 90))
            platform = rng.choice([None, "w", "x", "y"])
            self.assert_query_matches(posts, human, set(brands), end, days, platform)


if __name__ == "__main__":
    unittest.main()
