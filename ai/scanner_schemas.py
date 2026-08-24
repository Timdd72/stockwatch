"""Entry-only Structured Output fuer technisch verifizierte Scanner-Kandidaten."""
from pydantic import BaseModel, ConfigDict, Field
from .schemas import Direction, EntryRating, Horizon, Level, Outlook, PriceZone, Risk

class ScannerNewsOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    headline: str = Field(min_length=1, max_length=500)
    relevance: Level
    direction: Direction
    strength: Level
    horizon: Horizon
    short_reason: str = Field(min_length=1, max_length=300)
    important_event: bool

class ScannerEntryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_rating: EntryRating
    entry_confidence: float = Field(ge=0.0, le=1.0)
    outlook: Outlook
    risk: Risk
    summary: str = Field(min_length=1, max_length=900)
    entry_reason: str = Field(min_length=1, max_length=500)
    reasons: list[str] = Field(max_length=3)
    risks: list[str] = Field(max_length=3)
    entry_zone: PriceZone | None
    target_zone: PriceZone | None
    stop_zone: PriceZone | None
    time_horizon: str = Field(pattern=r"^1-3 months$")
    relevant_news: list[ScannerNewsOutput] = Field(max_length=10)
