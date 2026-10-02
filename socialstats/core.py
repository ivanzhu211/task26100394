"""离线社媒声量与情绪统计库（SQLite 持久化）。

设计要点：
- 原始帖子只追加，人工情绪直接改 posts.human_sent。
- 预聚合两级：agg_day（按 UTC 天）与 agg_ms（按毫秒），均按 (时间桶, 平台, 品牌)
  维护提及数与有效情绪之和。导入与人工更正时同步增量更新，因此查询耗时不随
  历史积累退化。
- 窗口查询：完整天走 agg_day，两端不足一天的边界走 agg_ms，两端不足一毫秒的
  残余区间回扫原始 posts（每段至多 1000 帖，因为微秒时间互不相同）。
"""
from __future__ import annotations

import sqlite3
from typing import Dict, Iterable, List, Optional, Tuple

from .types import DAY_US, VALID_SENTIMENTS, VALID_WINDOW_DAYS, BrandStat, Post

MS_US = 1_000  # 一毫秒的微秒数

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    post_id      TEXT PRIMARY KEY,
    ts_us        INTEGER NOT NULL,
    platform     TEXT NOT NULL,
    machine_sent INTEGER NOT NULL,
    human_sent   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_posts_ts ON posts(ts_us);
CREATE TABLE IF NOT EXISTS post_brands (
    post_id TEXT NOT NULL,
    brand   TEXT NOT NULL,
    PRIMARY KEY (post_id, brand)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS brands (
    brand TEXT PRIMARY KEY
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS agg_day (
    day      INTEGER NOT NULL,
    platform TEXT NOT NULL,
    brand    TEXT NOT NULL,
    cnt      INTEGER NOT NULL,
    sent_sum INTEGER NOT NULL,
    PRIMARY KEY (day, platform, brand)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS agg_ms (
    ms       INTEGER NOT NULL,
    platform TEXT NOT NULL,
    brand    TEXT NOT NULL,
    cnt      INTEGER NOT NULL,
    sent_sum INTEGER NOT NULL,
    PRIMARY KEY (ms, platform, brand)
) WITHOUT ROWID;
"""

_UPSERT_DAY = """
INSERT INTO agg_day(day, platform, brand, cnt, sent_sum) VALUES (?,?,?,?,?)
ON CONFLICT(day, platform, brand) DO UPDATE
SET cnt = cnt + excluded.cnt, sent_sum = sent_sum + excluded.sent_sum
"""
_UPSERT_MS = """
INSERT INTO agg_ms(ms, platform, brand, cnt, sent_sum) VALUES (?,?,?,?,?)
ON CONFLICT(ms, platform, brand) DO UPDATE
SET cnt = cnt + excluded.cnt, sent_sum = sent_sum + excluded.sent_sum
"""

_FLUSH_EVERY = 8192  # 每积累多少帖把聚合增量刷入 SQLite，控制内存


class SocialStats:
    """统计入口。调用串行执行；所有操作立即持久化并对后续查询可见。"""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SocialStats":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ 导入

    def import_posts(self, posts: Iterable[Post]) -> int:
        """批量导入新帖（重复编号忽略），返回实际插入条数。"""
        inserted = 0
        brand_rows: List[Tuple[str, str]] = []
        day_delta: Dict[Tuple[int, str, str], List[int]] = {}
        ms_delta: Dict[Tuple[int, str, str], List[int]] = {}
        pending = 0

        def flush() -> None:
            nonlocal pending
            if brand_rows:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO post_brands(post_id, brand) VALUES (?,?)",
                    brand_rows,
                )
                self._conn.executemany(
                    "INSERT OR IGNORE INTO brands(brand) VALUES (?)",
                    [(b,) for _, b in brand_rows],
                )
                brand_rows.clear()
            if day_delta:
                self._conn.executemany(
                    _UPSERT_DAY,
                    [(d, p, b, c[0], c[1]) for (d, p, b), c in day_delta.items()],
                )
                day_delta.clear()
            if ms_delta:
                self._conn.executemany(
                    _UPSERT_MS,
                    [(m, p, b, c[0], c[1]) for (m, p, b), c in ms_delta.items()],
                )
                ms_delta.clear()
            pending = 0

        with self._conn:  # 单事务，成功返回即已持久化
            for post in posts:
                self._validate_post(post)
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO posts"
                    "(post_id, ts_us, platform, machine_sent) VALUES (?,?,?,?)",
                    (post.post_id, post.ts_us, post.platform, post.machine_sentiment),
                )
                if cur.rowcount == 0:
                    continue  # 重复编号，忽略
                inserted += 1
                day = post.ts_us // DAY_US
                ms = post.ts_us // MS_US
                for brand in dict.fromkeys(post.brands):
                    brand_rows.append((post.post_id, brand))
                    for table_delta, key in (
                        (day_delta, (day, post.platform, brand)),
                        (ms_delta, (ms, post.platform, brand)),
                    ):
                        cell = table_delta.get(key)
                        if cell is None:
                            table_delta[key] = [1, post.machine_sentiment]
                        else:
                            cell[0] += 1
                            cell[1] += post.machine_sentiment
                pending += 1
                if pending >= _FLUSH_EVERY:
                    flush()
            flush()
        return inserted

    @staticmethod
    def _validate_post(post: Post) -> None:
        if not post.post_id:
            raise ValueError("post_id 不能为空")
        if not post.platform:
            raise ValueError("platform 不能为空")
        if not post.brands:
            raise ValueError("brands 必须非空")
        if post.machine_sentiment not in VALID_SENTIMENTS:
            raise ValueError(f"machine_sentiment 必须为 -1/0/1: {post.machine_sentiment}")

    # -------------------------------------------------------------- 人工情绪

    def set_human_sentiment(self, post_id: str, value: int) -> None:
        """设置/覆盖人工情绪，value ∈ {-1, 0, 1}。"""
        if value not in VALID_SENTIMENTS:
            raise ValueError(f"人工情绪必须为 -1/0/1: {value}")
        self._apply_correction(post_id, value)

    def clear_human_sentiment(self, post_id: str) -> None:
        """撤销人工情绪，恢复使用机器情绪。"""
        self._apply_correction(post_id, None)

    def _apply_correction(self, post_id: str, human: Optional[int]) -> None:
        row = self._conn.execute(
            "SELECT ts_us, platform, machine_sent, human_sent FROM posts WHERE post_id = ?",
            (post_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"未知帖子: {post_id}")
        ts_us, platform, machine_sent, old_human = row
        if old_human == human:
            return  # 有效情绪不变
        old_eff = old_human if old_human is not None else machine_sent
        new_eff = human if human is not None else machine_sent
        delta = new_eff - old_eff
        day = ts_us // DAY_US
        ms = ts_us // MS_US
        with self._conn:
            self._conn.execute(
                "UPDATE posts SET human_sent = ? WHERE post_id = ?", (human, post_id)
            )
            if delta:
                brands = [
                    r[0]
                    for r in self._conn.execute(
                        "SELECT brand FROM post_brands WHERE post_id = ?", (post_id,)
                    )
                ]
                # 更正影响该帖关联的所有品牌，但不改变提及数
                self._conn.executemany(
                    "UPDATE agg_day SET sent_sum = sent_sum + ?"
                    " WHERE day = ? AND platform = ? AND brand = ?",
                    [(delta, day, platform, b) for b in brands],
                )
                self._conn.executemany(
                    "UPDATE agg_ms SET sent_sum = sent_sum + ?"
                    " WHERE ms = ? AND platform = ? AND brand = ?",
                    [(delta, ms, platform, b) for b in brands],
                )

    # ------------------------------------------------------------------ 查询

    def query(
        self,
        end_us: int,
        window_days: int,
        platform: Optional[str] = None,
    ) -> List[BrandStat]:
        """窗口 [end_us - window_days 天, end_us) 内全部品牌的统计。"""
        if window_days not in VALID_WINDOW_DAYS:
            raise ValueError(f"window_days 必须为 7/30/90: {window_days}")
        start_us = end_us - window_days * DAY_US

        counts: Dict[str, List[int]] = {}  # brand -> [mentions, sent_sum]

        def add(rows: Iterable[Tuple[str, int, int]]) -> None:
            for brand, cnt, sent in rows:
                cell = counts.get(brand)
                if cell is None:
                    counts[brand] = [cnt, sent]
                else:
                    cell[0] += cnt
                    cell[1] += sent

        # 1) 完整天：agg_day
        day0 = -(-start_us // DAY_US)  # ceil
        day1 = end_us // DAY_US        # floor
        if day0 < day1:
            add(self._sum_agg("agg_day", "day", day0, day1, platform))
            boundaries = []
            if start_us < day0 * DAY_US:
                boundaries.append((start_us, day0 * DAY_US))
            if day1 * DAY_US < end_us:
                boundaries.append((day1 * DAY_US, end_us))
        else:
            boundaries = [(start_us, end_us)]

        # 2) 边界不足一天的部分：agg_ms + 微秒残余回扫原始表
        for lo, hi in boundaries:
            ms0 = -(-lo // MS_US)
            ms1 = hi // MS_US
            if ms0 < ms1:
                add(self._sum_agg("agg_ms", "ms", ms0, ms1, platform))
                if lo < ms0 * MS_US:
                    add(self._scan_raw(lo, ms0 * MS_US, platform))
                if ms1 * MS_US < hi:
                    add(self._scan_raw(ms1 * MS_US, hi, platform))
            else:
                add(self._scan_raw(lo, hi, platform))

        total = sum(c[0] for c in counts.values())
        stats = []
        for (brand,) in self._conn.execute("SELECT brand FROM brands ORDER BY brand"):
            cell = counts.get(brand)
            mentions = cell[0] if cell else 0
            stats.append(
                BrandStat(
                    brand=brand,
                    mentions=mentions,
                    share=(mentions / total) if total else 0.0,
                    avg_sentiment=(cell[1] / mentions) if mentions else None,
                )
            )
        return stats

    def _sum_agg(
        self, table: str, col: str, lo: int, hi: int, platform: Optional[str]
    ) -> List[Tuple[str, int, int]]:
        sql = f"SELECT brand, SUM(cnt), SUM(sent_sum) FROM {table} WHERE {col} >= ? AND {col} < ?"
        params: List[object] = [lo, hi]
        if platform is not None:
            sql += " AND platform = ?"
            params.append(platform)
        sql += " GROUP BY brand"
        return list(self._conn.execute(sql, params))

    def _scan_raw(
        self, lo_us: int, hi_us: int, platform: Optional[str]
    ) -> List[Tuple[str, int, int]]:
        sql = (
            "SELECT pb.brand, COUNT(*), SUM(COALESCE(p.human_sent, p.machine_sent))"
            " FROM posts p JOIN post_brands pb ON pb.post_id = p.post_id"
            " WHERE p.ts_us >= ? AND p.ts_us < ?"
        )
        params: List[object] = [lo_us, hi_us]
        if platform is not None:
            sql += " AND p.platform = ?"
            params.append(platform)
        sql += " GROUP BY pb.brand"
        return list(self._conn.execute(sql, params))
