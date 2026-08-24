"""Striktes Structured-Output-Schema für Aktienanalysen."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PositionRating(str, Enum):
    ADD = "ADD"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    SELL = "SELL"
    STRONG_SELL = "STRONG_SELL"


class EntryRating(str, Enum):
    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    WATCH = "WATCH"
    AVOID = "AVOID"


class Outlook(str, Enum):
    POSITIVE = "POSITIVE"
    NEUTRAL = "NEUTRAL"
    NEGATIVE = "NEGATIVE"


class Risk(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Level(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Direction(str, Enum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    NEUTRAL = "NEUTRAL"


class Horizon(str, Enum):
    SHORT = "SHORT"
    MEDIUM = "MEDIUM"
    LONG = "LONG"


class PriceZone(BaseModel):
    model_config = ConfigDict(extra="forbid")
    low: float
    high: float

    @model_validator(mode="after")
    def ordered(self) -> "PriceZone":
        if self.low > self.high:
            raise ValueError("low muss kleiner oder gleich high sein")
        return self


class NewsAssessmentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    news_id: int
    relevance: Level
    expected_direction: Direction
    expected_strength: Level
    horizon: Horizon
    short_reason: str = Field(min_length=1, max_length=300)


class StockAnalysisOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position_rating: PositionRating
    position_confidence: float = Field(ge=0.0, le=1.0)
    entry_rating: EntryRating
    entry_confidence: float = Field(ge=0.0, le=1.0)
    outlook: Outlook
    risk: Risk
    summary: str = Field(min_length=1, max_length=900)
    position_reason: str = Field(min_length=1, max_length=500)
    entry_reason: str = Field(min_length=1, max_length=500)
    reasons: list[str] = Field(max_length=3)
    risks: list[str] = Field(max_length=3)
    target_zone: PriceZone | None
    entry_zone: PriceZone | None
    stop_zone: PriceZone | None
    protect_profit_zone: PriceZone | None
    time_horizon: str = Field(pattern=r"^1-3 months$")
    relevant_news: list[NewsAssessmentOutput] = Field(max_length=10)
