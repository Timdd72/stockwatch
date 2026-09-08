"""Versionierter Datenvertrag für die zusätzliche MKR-14-Analyse."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MkrSignal(str, Enum):
    BULLISH = "BULLISH"
    NEUTRAL = "NEUTRAL"
    BEARISH = "BEARISH"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class MkrDataQuality(str, Enum):
    GOOD = "GOOD"
    LIMITED = "LIMITED"
    INSUFFICIENT = "INSUFFICIENT"


class MkrCoverage(str, Enum):
    FULL = "FULL"
    LIMITED = "LIMITED"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class MkrLevelType(str, Enum):
    PRICE = "PRICE"
    INDICATOR = "INDICATOR"
    PERCENTAGE = "PERCENTAGE"
    RATIO = "RATIO"


class MkrDataOrigin(str, Enum):
    LOCAL = "LOCAL"
    WEB = "WEB"
    MIXED = "MIXED"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class MkrResearchTopic(str, Enum):
    CATALYSTS = "catalysts"
    FUNDAMENTALS = "fundamentals"
    UNI_STRUCTURE = "uni_structure"


class MkrEntryStatus(str, Enum):
    YES = "JA"
    NO = "NEIN"
    PARTIAL = "TEILWEISE"
    WATCH = "BEOBACHTEN"


class MkrSourceType(str, Enum):
    STOCKWATCH_HISTORY = "STOCKWATCH_HISTORY"
    STOCKWATCH_TECHNICAL = "STOCKWATCH_TECHNICAL"
    STOCKWATCH_FUNDAMENTALS = "STOCKWATCH_FUNDAMENTALS"
    STOCKWATCH_NEWS = "STOCKWATCH_NEWS"
    STOCKWATCH_POSITION = "STOCKWATCH_POSITION"
    STOCKWATCH_QUOTE = "STOCKWATCH_QUOTE"
    WEB = "WEB"


class MkrSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=500)
    url: str | None = Field(default=None, min_length=1, max_length=2000)
    source: str = Field(min_length=1, max_length=255)
    source_type: MkrSourceType = MkrSourceType.STOCKWATCH_TECHNICAL
    publisher: str | None = Field(default=None, max_length=255)
    published_at: datetime | None = None
    accessed_at: datetime | None = None
    framework_number: int | None = Field(default=None, ge=1, le=14)
    usage_note: str | None = Field(default=None, max_length=500)


class MkrLevel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: float | None = None
    level_type: MkrLevelType = MkrLevelType.PRICE
    currency: str | None = Field(default=None, min_length=1, max_length=8)
    basis: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def currency_matches_level_type(self) -> "MkrLevel":
        if self.level_type == MkrLevelType.PRICE and not self.currency:
            raise ValueError("PRICE-Level benötigen eine Währung.")
        if self.level_type != MkrLevelType.PRICE and self.currency is not None:
            raise ValueError("Nur PRICE-Level dürfen eine Währung besitzen.")
        return self


class MkrFramework(BaseModel):
    model_config = ConfigDict(extra="forbid")
    number: int = Field(ge=1, le=14)
    name: str = Field(min_length=1, max_length=100)
    signal: MkrSignal
    strength: int | None = Field(default=None, ge=1, le=5)
    confidence: int = Field(ge=0, le=100)
    data_quality: MkrDataQuality
    data_origin: MkrDataOrigin = MkrDataOrigin.LOCAL
    explanation: str = Field(min_length=1, max_length=1500)
    levels: list[MkrLevel] = Field(default_factory=list, max_length=12)
    sources: list[MkrSource] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def unavailable_has_no_strength(self) -> "MkrFramework":
        if self.signal == MkrSignal.NOT_AVAILABLE and self.strength is not None:
            raise ValueError("NOT_AVAILABLE darf keine Stärke besitzen.")
        return self


class MkrPriceLevels(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stop_invalidation: MkrLevel | None = None
    support_1: MkrLevel | None = None
    support_2: MkrLevel | None = None
    current_price: MkrLevel | None = None
    target_1: MkrLevel | None = None
    target_2: MkrLevel | None = None
    extended_target: MkrLevel | None = None


class MkrEntryTiming(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: MkrEntryStatus
    trigger_price: MkrLevel | None = None
    stop_invalidation: MkrLevel | None = None
    alerts: list[str] = Field(default_factory=list, max_length=6)


class MkrAvailabilitySection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    available: bool
    statement: str = Field(min_length=1, max_length=1000)
    sources: list[MkrSource] = Field(default_factory=list, max_length=12)


class MkrUniCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    points: int | None = Field(default=None, ge=1, le=5)
    explanation: str = Field(min_length=1, max_length=500)


class MkrUniScore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score: int | None = Field(default=None, ge=0, le=25)
    criteria: list[MkrUniCriterion] = Field(min_length=5, max_length=5)


class MkrDataCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full: int = Field(ge=0, le=14)
    limited: int = Field(ge=0, le=14)
    not_available: int = Field(ge=0, le=14)

    @model_validator(mode="after")
    def covers_all_frameworks(self) -> "MkrDataCoverage":
        if self.full + self.limited + self.not_available != 14:
            raise ValueError("Die Datenabdeckung muss genau 14 Frameworks umfassen.")
        return self


class MkrAnalysis(BaseModel):
    """Structured Output einer späteren Responses-API-Anfrage."""

    model_config = ConfigDict(extra="forbid")
    summary: list[str] = Field(min_length=3, max_length=3)
    frameworks: list[MkrFramework] = Field(min_length=14, max_length=14)
    price_levels: MkrPriceLevels
    entry_timing: MkrEntryTiming
    options: MkrAvailabilitySection
    wheel: MkrAvailabilitySection
    uni_score: MkrUniScore
    confidence: int = Field(ge=0, le=100)
    peg: float | None = Field(default=None, ge=0)
    avoid_if: str = Field(min_length=1, max_length=1000)
    risks: list[str] = Field(min_length=3, max_length=3)
    data_coverage: MkrDataCoverage
    sources: list[MkrSource] = Field(default_factory=list, max_length=30)
    generated_at: datetime

    @model_validator(mode="after")
    def exactly_one_of_each_framework(self) -> "MkrAnalysis":
        numbers = [item.number for item in self.frameworks]
        if sorted(numbers) != list(range(1, 15)):
            raise ValueError("Jedes MKR-Framework von 1 bis 14 muss genau einmal vorkommen.")
        return self


class MkrFrameworkAvailability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    number: int = Field(ge=1, le=14)
    name: str
    data_quality: MkrDataQuality
    coverage: MkrCoverage
    reason: str


class MkrInputData(BaseModel):
    """Read-only Eingangsdaten; fehlende Werte bleiben explizit ``None``."""

    model_config = ConfigDict(extra="forbid")
    prompt_version: str
    stock: dict[str, Any]
    price: dict[str, Any] | None
    position: dict[str, Any] | None
    technical_data: dict[str, Any] | None
    fundamental_data: dict[str, Any] | None
    analyst_data: dict[str, Any] | None
    earnings_data: list[dict[str, Any]]
    news_data: list[dict[str, Any]]
    data_sources: list[dict[str, Any]]
    framework_availability: list[MkrFrameworkAvailability] = Field(min_length=14, max_length=14)


class MkrWebFinding(BaseModel):
    """Belastbare externe Ergänzung; technische Indikatoren sind ausgeschlossen."""

    model_config = ConfigDict(extra="forbid")
    topic: MkrResearchTopic
    # Nur für rückwärtskompatible Structured Outputs/Caches vorhanden. Dieser
    # Modellwert ist niemals autoritativ; Python leitet ihn aus ``topic`` ab.
    framework_number: int = Field(ge=1, le=14)
    fact: str = Field(min_length=1, max_length=1500)
    as_of: datetime | None = None
    sources: list[MkrSource] = Field(min_length=1, max_length=5)

class MkrWebResearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    findings: list[MkrWebFinding] = Field(default_factory=list, max_length=20)
