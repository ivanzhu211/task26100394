"""Correctness tests: SocialStats vs. a naive per-post reference."""

from __future__ import annotations

import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from social_stats import SocialStats, Post, WindowDays  # noqa: E402
from social_stats.core import DAY_US, SEC_US, _DENSE_THRESHOLD  # noqa: E402


class Naive:
    """Direct per-post computation, the ground truth."""

    def __init__(self) -> None:
        self.posts: dict[str, Post] = {}
        self.human: dict[str, int] = {}

    def import_posts(self, posts) -> None:
        for p in posts:
            assert p.post_id not in self.posts
            self.posts[p.post_id] = p

    def set_human_sentiment(self, post_id: str, value: int) -> bool:
        if post_id not in self.posts:
            return False
        self.human[post_id] = value
        return True

    def clear_human_sentiment(self, post_id: str) -> bool:
        if post_id not in self.posts:
            return False
        self.human.pop(post_id, None)
        return True

    def query(self, end_us: int, window: int, platform=None):
        start = end_us - window * DAY_US
        cnt: dict[str, int] = {}
        sent: dict[str, int] = {}
        brands: set[str] = set()
        for p in self.posts.values():
            brands.update(p.brands)
            if not (start <= p.ts_us < end_us):
                continue
            if platform is not None and p.platform != platform:
                continue
            eff = self.human.get(p.post_id, p.sentiment)
            for b in p.brands:
                cnt[b] = cnt.get(b, 0) + 1
                sent[b] = sent.get(b, 0) + eff
        total = sum(cnt.values())
        out = []
        for b in brands:
            c = cnt.get(b, 0)
            out.append((b, c, (c / total) if total else 0.0,
                        (sent[b] / c) if c else None))
        out.sort(key=lambda r: (-r[1], r[0]))
        return out


def to_tuples(stats):
    return [(s.brand, s.mentions, s.share, s.avg_sentiment) for s in stats]


class SocialStatsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "stats.db")
        self.stats = SocialStats(self.db)
        self.naive = Naive()

    def tearDown(self) -> None:
        self.stats.close()
        self.tmp.cleanup()

    def check_all(self, ends, platforms=(None, "tw", "fb", "ig")):
        for end in ends:
            for window in (7, 30, 90):
                for platform in platforms:
                    got = to_tuples(self.stats.query(end, window, platform))
                    want = self.naive.query(end, window, platform)
                    self.assertEqual(
                        got, want,
                        f"end={end} window={window} platform={platform}",
                    )

    def test_single_post_two_brands(self):
        post = Post("p1", 5 * DAY_US + 123, "tw", frozenset({"a", "b"}), 1)
        self.stats.import_posts([post])
        self.naive.import_posts([post])
        res = {s.brand: s for s in self.stats.query(6 * DAY_US, 7)}
        self.assertEqual(res["a"].mentions, 1)
        self.assertEqual(res["b"].mentions, 1)
        self.assertEqual(res["a"].share, 0.5)
        self.assertEqual(res["b"].share, 0.5)
        self.assertEqual(res["a"].avg_sentiment, 1.0)
        self.check_all([6 * DAY_US])

    def test_window_microsecond_boundaries(self):
        end = 10 * DAY_US + 42
        start = end - 7 * DAY_US
        posts = [
            Post("at_start", start, "tw", frozenset({"a"}), 1),
            Post("before_start", start - 1, "tw", frozenset({"a"}), 1),
            Post("before_end", end - 1, "tw", frozenset({"a"}), 1),
            Post("at_end", end, "tw", frozenset({"a"}), 1),
            Post("day_edge_in", start - start % DAY_US + DAY_US, "tw",
                 frozenset({"a"}), 1),
            Post("sec_edge_in", start - start % SEC_US + SEC_US, "tw",
                 frozenset({"a"}), 1),
        ]
        self.stats.import_posts(posts)
        self.naive.import_posts(posts)
        res = {s.brand: s for s in self.stats.query(end, 7, "tw")}
        # included: at_start, before_end, day_edge_in, sec_edge_in
        self.assertEqual(res["a"].mentions, 4)
        self.check_all([end, end + 1, start, start + 1, 0, 1])

    def test_late_import_and_repeated_corrections(self):
        base = 40 * DAY_US
        posts = [
            Post(f"p{i}", base + i * 977, "tw" if i % 2 else "fb",
                 frozenset({"a", "b"} if i % 3 else {"b"}), (i % 3) - 1)
            for i in range(200)
        ]
        self.stats.import_posts(posts[:100])
        self.naive.import_posts(posts[:100])
        # late, out-of-order arrivals, some older than everything stored
        late = posts[100:] + [
            Post("old1", base - 30 * DAY_US, "ig", frozenset({"a"}), 0),
            Post("old2", 1, "ig", frozenset({"c"}), -1),
        ]
        self.stats.import_posts(late)
        self.naive.import_posts(late)
        for i in range(0, 200, 7):
            pid = f"p{i}"
            for op in ("set", "set", "clear", "set"):
                if op == "set":
                    v = ((i // 7) % 3) - 1
                    self.assertTrue(self.stats.set_human_sentiment(pid, v))
                    self.assertTrue(self.naive.set_human_sentiment(pid, v))
                else:
                    self.assertTrue(self.stats.clear_human_sentiment(pid))
                    self.assertTrue(self.naive.clear_human_sentiment(pid))
        self.assertFalse(self.stats.set_human_sentiment("ghost", 1))
        self.assertFalse(self.stats.clear_human_sentiment("ghost"))
        ends = [base + k * 3_333_333 for k in range(12)] + [base - 1, base]
        self.check_all(ends)

    def test_randomized_vs_naive(self):
        rng = random.Random(20261003)
        brands = [f"b{i}" for i in range(6)]
        platforms = ["tw", "fb", "ig"]
        t0 = 100 * DAY_US
        n = 0
        for batch in range(6):
            posts = []
            for _ in range(150):
                n += 1
                posts.append(Post(
                    f"p{n}",
                    t0 + rng.randrange(0, 200 * DAY_US),
                    rng.choice(platforms),
                    frozenset(rng.sample(brands, rng.randint(1, 3))),
                    rng.choice((-1, 0, 1)),
                ))
            self.stats.import_posts(posts)
            self.naive.import_posts(posts)
            for _ in range(20):
                pid = f"p{rng.randint(1, n)}"
                if rng.random() < 0.6:
                    v = rng.choice((-1, 0, 1))
                    self.stats.set_human_sentiment(pid, v)
                    self.naive.set_human_sentiment(pid, v)
                else:
                    self.stats.clear_human_sentiment(pid)
                    self.naive.clear_human_sentiment(pid)
            ends = [t0 + rng.randrange(0, 220 * DAY_US) for _ in range(4)]
            ends += [t0 + rng.randrange(0, 220 * DAY_US // SEC_US) * SEC_US
                     + rng.choice((0, 1, SEC_US - 1)) for _ in range(4)]
            self.check_all(ends)

    def test_dense_second_endpoint_inside(self):
        sec0 = 60 * DAY_US
        n = _DENSE_THRESHOLD + 1000
        posts = [
            Post(f"d{i}", sec0 + i % SEC_US, "tw" if i % 2 else "fb",
                 frozenset({"a", "b"} if i % 5 else {"a"}), (i % 3) - 1)
            for i in range(n)
        ]
        self.stats.import_posts(posts)
        self.naive.import_posts(posts)
        # corrections inside the dense second
        for i in range(0, n, 251):
            self.stats.set_human_sentiment(f"d{i}", (i % 3) - 1)
            self.naive.set_human_sentiment(f"d{i}", (i % 3) - 1)
        ends = [sec0 + k for k in (1, 2, 999_999, 500_000, 123_457)]
        ends += [sec0 + SEC_US, sec0 - 1]
        self.check_all(ends, platforms=(None, "tw", "fb"))
        # undo everything and re-check
        for i in range(0, n, 251):
            self.stats.clear_human_sentiment(f"d{i}")
            self.naive.clear_human_sentiment(f"d{i}")
        self.check_all(ends, platforms=(None, "tw"))

    def test_reopen_persistence(self):
        posts = [
            Post("x1", 7 * DAY_US + 5, "tw", frozenset({"a", "b"}), 1),
            Post("x2", 8 * DAY_US, "fb", frozenset({"a"}), -1),
        ]
        self.stats.import_posts(posts)
        self.naive.import_posts(posts)
        self.stats.set_human_sentiment("x2", 1)
        self.naive.set_human_sentiment("x2", 1)
        before = to_tuples(self.stats.query(9 * DAY_US, 30))
        self.stats.close()
        self.stats = SocialStats(self.db)
        after = to_tuples(self.stats.query(9 * DAY_US, 30))
        self.assertEqual(before, after)
        self.assertEqual(after, to_tuples(self.stats.query(9 * DAY_US, 30)))
        self.assertEqual(after, self.naive.query(9 * DAY_US, 30))

    def test_empty_and_validation(self):
        self.assertEqual(self.stats.query(10 * DAY_US, 7), [])
        with self.assertRaises(ValueError):
            self.stats.query(0, 14)
        with self.assertRaises(ValueError):
            self.stats.import_posts([Post("b", 0, "tw", frozenset({"a"}), 2)])
        with self.assertRaises(ValueError):
            self.stats.import_posts([Post("b", 0, "tw", frozenset(), 1)])
        with self.assertRaises(ValueError):
            self.stats.set_human_sentiment("b", 5)
        self.stats.import_posts([Post("b", 0, "tw", frozenset({"a"}), 1)])
        with self.assertRaises(ValueError):
            self.stats.import_posts([Post("b", 0, "tw", frozenset({"a"}), 1)])
        # failed duplicate import must not corrupt state
        res = {s.brand: s for s in self.stats.query(DAY_US, 7)}
        self.assertEqual(res["a"].mentions, 1)


if __name__ == "__main__":
    unittest.main()
