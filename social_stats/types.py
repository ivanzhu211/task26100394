"""Public data types for the social stats library."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Iterable


class WindowDays(enum.IntEnum):
    """Allowed query window lengths, in days (1 day == 86400 seconds)."""

    DAYS_7 = 7
    DAYS_30 = 30
    DAYS_90 = 90


@dataclass(frozen=True)
class Post:
    """One incoming post. ``brands`` must be a non-empty set of brand names.

    ``sentiment`` is the machine sentiment: -1, 0 or 1.
    ``ts_us`` is the UTC timestamp in integer microseconds.
    """

    post_id: str
    ts_us: int
    platform: str
    brands: frozenset[str]
    sentiment: int


@dataclass(frozen=True)
class BrandStat:
    """Per-brand statistics for one window query."""

    brand: str
    mentions: int
    share: float  # mentions / total mentions in window (0.0 when total is 0)
    avg_sentiment: float | None  # None when the brand has no mentions
