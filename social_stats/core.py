"""SQLite-backed mention/sentiment statistics store.

Layout
------
posts          one row per post (machine + optional human sentiment)
post_brands    one row per (post, brand) mention
brands         registry of every brand ever seen (for zero-filled results)
rollup_day     per (day, platform, brand) mention count + effective sentiment sum
rollup_sec     per (second, platform, brand) same aggregates (window edges)
rollup_dense   per (second, 10ms bucket, platform, brand), only for seconds
               whose post count exceeds _DENSE_THRESHOLD (window edges that
               cut through extremely dense seconds)
dense_seconds  registry of seconds that have a rollup_dense projection

Queries combine full days from rollup_day with partial edge days from
rollup_sec, and partial edge seconds from rollup_dense or raw posts, so cost
is independent of total history size.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable

from social_stats.types import BrandStat, Post, WindowDays

DAY_US = 86_400 * 1_000_000  # one day in microseconds
SEC_US = 1_000_000
_DENSE_THRESHOLD = 4096  # posts within one second that trigger rollup_dense
_DENSE_BUCKET_US = 10_000  # 10 ms buckets inside dense seconds

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    post_id      TEXT PRIMARY KEY,
    ts_us        INTEGER NOT NULL,
    platform     TEXT NOT NULL,
    machine_sent INTEGER NOT NULL,
    human_sent   INTEGER
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_posts_ts ON posts (ts_us);

CREATE TABLE IF NOT EXISTS post_brands (
    post_id TEXT NOT NULL REFERENCES posts (post_id),
    brand   TEXT NOT NULL,
    PRIMARY KEY (post_id, brand)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS brands (
    brand TEXT PRIMARY KEY
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS rollup_day (
    day      INTEGER NOT NULL,
    platform TEXT NOT NULL,
    brand    TEXT NOT NULL,
    cnt      INTEGER NOT NULL,
    sent_sum INTEGER NOT NULL,
    PRIMARY KEY (day, platform, brand)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS rollup_sec (
    sec      INTEGER NOT NULL,
    platform TEXT NOT NULL,
    brand    TEXT NOT NULL,
    cnt      INTEGER NOT NULL,
    sent_sum INTEGER NOT NULL,
    PRIMARY KEY (sec, platform, brand)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS rollup_dense (
    sec      INTEGER NOT NULL,
    bucket   INTEGER NOT NULL,
    platform TEXT NOT NULL,
    brand    TEXT NOT NULL,
    cnt      INTEGER NOT NULL,
    sent_sum INTEGER NOT NULL,
    PRIMARY KEY (sec, bucket, platform, brand)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS dense_seconds (
    sec INTEGER PRIMARY KEY
) WITHOUT ROWID;
"""


class SocialStats:
    """Statistics store persisted in a caller-provided SQLite file."""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SocialStats":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def import_posts(self, posts: Iterable[Post]) -> int:
        """Append new posts. Returns the number of imported posts."""
        rows: list[tuple] = []
        brand_rows: list[tuple] = []
        brand_reg: set[str] = set()
        day_agg: dict[tuple, list[int]] = {}
        sec_agg: dict[tuple, list[int]] = {}
        touched_secs: set[int] = set()

        for post in posts:
            self._validate_post(post)
            pid = str(post.post_id)
            sec = post.ts_us // SEC_US
            day = post.ts_us // DAY_US
            rows.append((pid, post.ts_us, post.platform, post.sentiment))
            touched_secs.add(sec)
            for brand in post.brands:
                brand_rows.append((pid, brand))
                if brand not in brand_reg:
                    brand_reg.add(brand)
                for agg, key in (
                    (day_agg, (day, post.platform, brand)),
                    (sec_agg, (sec, post.platform, brand)),
                ):
                    cell = agg.get(key)
                    if cell is None:
                        agg[key] = [1, post.sentiment]
                    else:
                        cell[0] += 1
                        cell[1] += post.sentiment

        if not rows:
            return 0

        cur = self._conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.executemany(
                "INSERT INTO posts (post_id, ts_us, platform, machine_sent)"
                " VALUES (?, ?, ?, ?)",
                rows,
            )
            cur.executemany(
                "INSERT INTO post_brands (post_id, brand) VALUES (?, ?)",
                brand_rows,
            )
            cur.executemany(
                "INSERT OR IGNORE INTO brands (brand) VALUES (?)",
                [(b,) for b in brand_reg],
            )
            self._upsert_rollup(cur, "rollup_day", "day", day_agg)
            self._upsert_rollup(cur, "rollup_sec", "sec", sec_agg)
            for sec in touched_secs:
                self._maybe_densify(cur, sec)
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            raise ValueError(f"duplicate or invalid post: {exc}") from exc
        except BaseException:
            self._conn.rollback()
            raise
        return len(rows)

    @staticmethod
    def _validate_post(post: Post) -> None:
        if post.sentiment not in (-1, 0, 1):
            raise ValueError(f"bad sentiment {post.sentiment!r}")
        if not isinstance(post.ts_us, int):
            raise ValueError("ts_us must be an int (UTC microseconds)")
        if not post.platform:
            raise ValueError("platform must be a non-empty string")
        if not post.brands:
            raise ValueError("brands must be a non-empty set")

    @staticmethod
    def _upsert_rollup(
        cur: sqlite3.Cursor,
        table: str,
        time_col: str,
        agg: dict[tuple, list[int]],
    ) -> None:
        sql = (
            f"INSERT INTO {table} ({time_col}, platform, brand, cnt, sent_sum)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT DO UPDATE SET"
            "   cnt = cnt + excluded.cnt,"
            "   sent_sum = sent_sum + excluded.sent_sum"
        )
        cur.executemany(
            sql,
            [(t, p, b, c, s) for (t, p, b), (c, s) in agg.items()],
        )

    def _maybe_densify(self, cur: sqlite3.Cursor, sec: int) -> None:
        """Build the 10ms-bucket projection for a second once it is dense."""
        cur.execute("SELECT 1 FROM dense_seconds WHERE sec = ?", (sec,))
        if cur.fetchone() is not None:
            return
        cur.execute(
            "SELECT COUNT(*) FROM posts WHERE ts_us >= ? AND ts_us < ?",
            (sec * SEC_US, (sec + 1) * SEC_US),
        )
        if cur.fetchone()[0] < _DENSE_THRESHOLD:
            return
        cur.execute(
            "INSERT INTO rollup_dense (sec, bucket, platform, brand, cnt, sent_sum)"
            " SELECT ?, (p.ts_us - ?) / ?, p.platform, pb.brand,"
            "        COUNT(*), SUM(COALESCE(p.human_sent, p.machine_sent))"
            " FROM posts p JOIN post_brands pb ON pb.post_id = p.post_id"
            " WHERE p.ts_us >= ? AND p.ts_us < ?"
            " GROUP BY 1, 2, 3, 4",
            (
                sec,
                sec * SEC_US,
                _DENSE_BUCKET_US,
                sec * SEC_US,
                (sec + 1) * SEC_US,
            ),
        )
        cur.execute("INSERT INTO dense_seconds (sec) VALUES (?)", (sec,))

    def set_human_sentiment(self, post_id: str, value: int) -> bool:
        """Set/replace the human sentiment for a post. False if unknown post."""
        if value not in (-1, 0, 1):
            raise ValueError(f"bad sentiment {value!r}")
        return self._correct(post_id=str(post_id), new_human=value)

    def clear_human_sentiment(self, post_id: str) -> bool:
        """Remove the human sentiment, restoring the machine value."""
        return self._correct(post_id=str(post_id), new_human=None)

    def _correct(self, post_id: str, new_human: int | None) -> bool:
        cur = self._conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute(
                "SELECT ts_us, platform, machine_sent, human_sent"
                " FROM posts WHERE post_id = ?",
                (post_id,),
            )
            row = cur.fetchone()
            if row is None:
                self._conn.rollback()
                return False
            ts_us, platform, machine_sent, old_human = row
            old_eff = old_human if old_human is not None else machine_sent
            new_eff = new_human if new_human is not None else machine_sent
            cur.execute(
                "UPDATE posts SET human_sent = ? WHERE post_id = ?",
                (new_human, post_id),
            )
            delta = new_eff - old_eff
            if delta:
                sec = ts_us // SEC_US
                day = ts_us // DAY_US
                cur.execute(
                    "SELECT brand FROM post_brands WHERE post_id = ?",
                    (post_id,),
                )
                brands = [r[0] for r in cur.fetchall()]
                cur.execute(
                    "SELECT 1 FROM dense_seconds WHERE sec = ?", (sec,)
                )
                dense = cur.fetchone() is not None
                bucket = (ts_us - sec * SEC_US) // _DENSE_BUCKET_US
                for brand in brands:
                    cur.execute(
                        "UPDATE rollup_day SET sent_sum = sent_sum + ?"
                        " WHERE day = ? AND platform = ? AND brand = ?",
                        (delta, day, platform, brand),
                    )
                    cur.execute(
                        "UPDATE rollup_sec SET sent_sum = sent_sum + ?"
                        " WHERE sec = ? AND platform = ? AND brand = ?",
                        (delta, sec, platform, brand),
                    )
                    if dense:
                        cur.execute(
                            "UPDATE rollup_dense SET sent_sum = sent_sum + ?"
                            " WHERE sec = ? AND bucket = ?"
                            " AND platform = ? AND brand = ?",
                            (delta, sec, bucket, platform, brand),
                        )
            self._conn.commit()
            return True
        except BaseException:
            self._conn.rollback()
            raise

    def query(
        self,
        end_us: int,
        window: WindowDays,
        platform: str | None = None,
    ) -> list[BrandStat]:
        """Stats for [end_us - window days, end_us); platform=None means all."""
        try:
            days = WindowDays(window)
        except ValueError:
            raise ValueError(f"window must be one of {list(WindowDays)}") from None
        if not isinstance(end_us, int):
            raise ValueError("end_us must be an int (UTC microseconds)")
        start_us = end_us - int(days) * DAY_US

        acc: dict[str, list[int]] = {}
        cur = self._conn.cursor()

        def merge(rows: Iterable[tuple]) -> None:
            for brand, cnt, sent in rows:
                cell = acc.get(brand)
                if cell is None:
                    acc[brand] = [cnt, sent]
                else:
                    cell[0] += cnt
                    cell[1] += sent

        # Full days inside [start_us, end_us).
        day_lo = -(-start_us // DAY_US)
        day_hi = end_us // DAY_US  # exclusive
        if day_lo < day_hi:
            merge(
                cur.execute(
                    "SELECT brand, SUM(cnt), SUM(sent_sum) FROM rollup_day"
                    " WHERE day >= ? AND day < ? AND (? IS NULL OR platform = ?)"
                    " GROUP BY brand",
                    (day_lo, day_hi, platform, platform),
                )
            )
        # Partial edge days, each shorter than one day.
        edge1_end = min(day_lo * DAY_US, end_us)
        if start_us < edge1_end:
            self._add_sub_day(cur, merge, start_us, edge1_end, platform)
        edge2_start = max(day_hi * DAY_US, start_us)
        if edge2_start < end_us:
            self._add_sub_day(cur, merge, edge2_start, end_us, platform)

        cur.execute("SELECT brand FROM brands")
        total = sum(c for c, _ in acc.values())
        stats = []
        for (brand,) in cur.fetchall():
            cnt, sent = acc.get(brand, (0, 0))
            stats.append(
                BrandStat(
                    brand=brand,
                    mentions=cnt,
                    share=(cnt / total) if total else 0.0,
                    avg_sentiment=(sent / cnt) if cnt else None,
                )
            )
        stats.sort(key=lambda s: (-s.mentions, s.brand))
        return stats

    def _add_sub_day(
        self,
        cur: sqlite3.Cursor,
        merge,
        a: int,
        b: int,
        platform: str | None,
    ) -> None:
        """Accumulate mentions in [a, b), where 0 < b - a <= DAY_US."""
        head_upto = a
        if a % SEC_US:
            head_upto = min((a // SEC_US + 1) * SEC_US, b)
            self._add_sub_second(cur, merge, a, head_upto, platform)
        sec_lo = head_upto // SEC_US  # head_upto is second-aligned or == b
        sec_hi = b // SEC_US  # exclusive
        if sec_lo < sec_hi:
            merge(
                cur.execute(
                    "SELECT brand, SUM(cnt), SUM(sent_sum) FROM rollup_sec"
                    " WHERE sec >= ? AND sec < ? AND (? IS NULL OR platform = ?)"
                    " GROUP BY brand",
                    (sec_lo, sec_hi, platform, platform),
                )
            )
        tail_from = max(sec_hi * SEC_US, head_upto)
        if tail_from < b:
            self._add_sub_second(cur, merge, tail_from, b, platform)

    def _add_sub_second(
        self,
        cur: sqlite3.Cursor,
        merge,
        a: int,
        b: int,
        platform: str | None,
    ) -> None:
        """Accumulate mentions in [a, b) contained within a single second."""
        sec = a // SEC_US
        cur.execute("SELECT 1 FROM dense_seconds WHERE sec = ?", (sec,))
        if cur.fetchone() is None:
            self._add_raw(cur, merge, a, b, platform)
            return
        base = sec * SEC_US
        off_a = a - base
        off_b = b - base
        bucket_lo = -(-off_a // _DENSE_BUCKET_US)
        bucket_hi = off_b // _DENSE_BUCKET_US  # exclusive
        if bucket_lo < bucket_hi:
            merge(
                cur.execute(
                    "SELECT brand, SUM(cnt), SUM(sent_sum) FROM rollup_dense"
                    " WHERE sec = ? AND bucket >= ? AND bucket < ?"
                    " AND (? IS NULL OR platform = ?)"
                    " GROUP BY brand",
                    (sec, bucket_lo, bucket_hi, platform, platform),
                )
            )
        head_end = min(bucket_lo * _DENSE_BUCKET_US, off_b)
        if off_a < head_end:
            self._add_raw(cur, merge, base + off_a, base + head_end, platform)
        tail_from = max(bucket_hi * _DENSE_BUCKET_US, off_a)
        if tail_from < off_b:
            self._add_raw(cur, merge, base + tail_from, base + off_b, platform)

    @staticmethod
    def _add_raw(
        cur: sqlite3.Cursor,
        merge,
        a: int,
        b: int,
        platform: str | None,
    ) -> None:
        """Accumulate mentions in [a, b) by scanning raw posts (short range)."""
        merge(
            cur.execute(
                "SELECT pb.brand, COUNT(*),"
                " SUM(COALESCE(p.human_sent, p.machine_sent))"
                " FROM posts p JOIN post_brands pb ON pb.post_id = p.post_id"
                " WHERE p.ts_us >= ? AND p.ts_us < ?"
                " AND (? IS NULL OR p.platform = ?)"
                " GROUP BY pb.brand",
                (a, b, platform, platform),
            )
        )
