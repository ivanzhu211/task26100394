"""Reproducible scale validation for social_stats.

Scenario (per requirements):
  * 1_000_000 posts, 21 brands, 8 platforms, avg 3 brands/post, 2-year span
  * 500_000 posts concentrated in a single second, distinct microsecond ts
  * phase 1: bulk load (reported, not counted against the limits)
  * phase 2: 1000 mixed groups, each = 1 late single-post import (ts inside
    the queried historical window) + 1 human-sentiment correction + 1 window
    query; query endpoints keep moving and half of them land *inside* the
    dense second
  * limits: window-query p95 <= 100 ms, mixed phase total <= 60 s,
    peak RSS <= 512 MiB

Run:  python3 bench/scale_check.py [--keep]
"""

from __future__ import annotations

import os
import random
import resource
import shutil
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from social_stats import SocialStats, Post  # noqa: E402
from social_stats.core import DAY_US, SEC_US  # noqa: E402

SEED = 20261003
N_POSTS = 1_000_000
N_DENSE = 500_000
N_BRANDS = 21
N_PLATFORMS = 8
SPAN_US = 2 * 365 * DAY_US
DENSE_SEC_US = (SPAN_US // 2) // SEC_US * SEC_US  # dense second in the middle
CHUNK = 50_000
GROUPS = 1000
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
DB_PATH = os.path.join(OUT_DIR, "scale.db")

BRANDS = [f"brand{i:02d}" for i in range(N_BRANDS)]
PLATFORMS = [f"plat{i}" for i in range(N_PLATFORMS)]


def peak_mib() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux reports KiB
    if sys.platform == "darwin":
        return rss / 2**20
    return rss / 2**10


def gen_posts(rng: random.Random):
    """Yield posts: 500k uniform over 2 years, then 500k in one second."""
    for i in range(N_POSTS - N_DENSE):
        yield Post(
            f"u{i}",
            rng.randrange(0, SPAN_US),
            rng.choice(PLATFORMS),
            frozenset(rng.sample(BRANDS, 3)),
            rng.choice((-1, 0, 1)),
        )
    for i in range(N_DENSE):
        yield Post(
            f"d{i}",
            DENSE_SEC_US + i,  # distinct microseconds inside one second
            rng.choice(PLATFORMS),
            frozenset(rng.sample(BRANDS, 3)),
            rng.choice((-1, 0, 1)),
        )


def chunked(it, n):
    buf = []
    for x in it:
        buf.append(x)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf


def main() -> None:
    keep = "--keep" in sys.argv
    shutil.rmtree(OUT_DIR, ignore_errors=True)
    os.makedirs(OUT_DIR)
    rng = random.Random(SEED)

    stats = SocialStats(DB_PATH)

    t0 = time.perf_counter()
    total = 0
    for chunk in chunked(gen_posts(rng), CHUNK):
        total += stats.import_posts(chunk)
    load_s = time.perf_counter() - t0
    print(f"[load] imported {total} posts in {load_s:.1f}s "
          f"(not counted against limits)")

    # reopen to prove persistence before measuring
    stats.close()
    stats = SocialStats(DB_PATH)

    windows = (7, 30, 90)
    query_ms: list[float] = []
    late_ids: list[str] = []
    t0 = time.perf_counter()
    for g in range(GROUPS):
        window = windows[g % 3]
        window_us = window * DAY_US
        if g % 2 == 0:
            # endpoint inside the dense second
            end_us = DENSE_SEC_US + 1 + rng.randrange(0, N_DENSE - 1)
        else:
            end_us = DENSE_SEC_US + rng.randrange(0, 30 * DAY_US)
        platform = None if g % 4 == 0 else PLATFORMS[g % N_PLATFORMS]

        # 1) late single-post import, ts inside the queried window
        pid = f"late{g}"
        late_ts = end_us - rng.randrange(0, window_us)
        stats.import_posts([Post(pid, late_ts, PLATFORMS[g % N_PLATFORMS],
                                 frozenset(rng.sample(BRANDS, 3)),
                                 rng.choice((-1, 0, 1)))])
        late_ids.append(pid)

        # 2) human-sentiment correction of a post in the queried window
        if g % 2 == 0:
            victim = f"d{rng.randrange(0, N_DENSE)}"  # dense-second post
        else:
            victim = late_ids[g // 2]  # an earlier late post
        stats.set_human_sentiment(victim, rng.choice((-1, 0, 1)))

        # 3) window query
        q0 = time.perf_counter()
        res = stats.query(end_us, window, platform)
        query_ms.append((time.perf_counter() - q0) * 1000)
        if res:
            assert abs(sum(s.share for s in res) - 1.0) < 1e-6 or \
                all(s.share == 0.0 for s in res)

    mixed_s = time.perf_counter() - t0
    stats.close()

    query_ms.sort()
    p50 = statistics.median(query_ms)
    p95 = query_ms[int(len(query_ms) * 0.95) - 1]
    p99 = query_ms[int(len(query_ms) * 0.99) - 1]
    peak = peak_mib()
    db_mib = os.path.getsize(DB_PATH) / 2**20

    print(f"[query] n={len(query_ms)} p50={p50:.1f}ms p95={p95:.1f}ms "
          f"p99={p99:.1f}ms max={query_ms[-1]:.1f}ms  (limit: p95<=100ms)")
    print(f"[mixed] {GROUPS} groups (late import + correction + query) "
          f"in {mixed_s:.1f}s  (limit: <=60s)")
    print(f"[mem]   peak RSS {peak:.0f} MiB  (limit: <=512 MiB)")
    print(f"[db]    file size {db_mib:.0f} MiB -> {DB_PATH}")

    ok = p95 <= 100 and mixed_s <= 60 and peak <= 512
    print("RESULT:", "PASS" if ok else "FAIL")
    if not keep:
        shutil.rmtree(OUT_DIR, ignore_errors=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
