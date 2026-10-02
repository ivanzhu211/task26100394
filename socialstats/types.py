"""公共类型定义。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

VALID_SENTIMENTS = (-1, 0, 1)
VALID_WINDOW_DAYS = (7, 30, 90)
DAY_US = 86_400_000_000  # 一天的微秒数


@dataclass(frozen=True)
class Post:
    """一条待导入的帖子。品牌识别与机器情绪打分已在上游完成。"""

    post_id: str
    ts_us: int            # UTC 整数微秒时间
    platform: str
    brands: Tuple[str, ...]  # 非空品牌集合
    machine_sentiment: int   # -1 / 0 / 1


@dataclass(frozen=True)
class BrandStat:
    """单个品牌在一个窗口内的统计结果。"""

    brand: str
    mentions: int
    share: float                    # 声量占比，分母为窗口内各品牌提及数之和
    avg_sentiment: Optional[float]  # 平均有效情绪；无提及时为 None
