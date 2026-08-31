"""SQLAlchemy-Modelle für StockWatch."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


PRICE_TYPES = ("REALTIME", "DELAYED", "INTRADAY", "DAILY_CLOSE", "MANUAL")


class Base(DeclarativeBase):
    pass


class Stock(Base):
    __tablename__ = "stocks"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    exchange: Mapped[str] = mapped_column(String(128), nullable=False)
    external_quote_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    positions: Mapped[list["Position"]] = relationship(
        back_populates="stock", cascade="all, delete-orphan"
    )
    analysis_snapshots: Mapped[list["AnalysisSnapshot"]] = relationship(
        back_populates="stock", cascade="all, delete-orphan"
    )


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), nullable=False)
    purchase_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    purchase_date: Mapped[date] = mapped_column(Date, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    stock: Mapped[Stock] = relationship(back_populates="positions")


class AnalysisSnapshot(Base):
    __tablename__ = "analysis_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    market_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    manual_source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_entered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    manual_entered_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    change_1d_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    performance_5d: Mapped[float | None] = mapped_column(Float, nullable=True)
    performance_20d: Mapped[float | None] = mapped_column(Float, nullable=True)
    performance_60d: Mapped[float | None] = mapped_column(Float, nullable=True)
    sma20: Mapped[float | None] = mapped_column(Float, nullable=True)
    sma50: Mapped[float | None] = mapped_column(Float, nullable=True)
    sma200: Mapped[float | None] = mapped_column(Float, nullable=True)
    rsi14: Mapped[float | None] = mapped_column(Float, nullable=True)
    volatility_20d: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_sma20_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_sma50_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    trend: Mapped[str | None] = mapped_column(String(16), nullable=True)

    stock: Mapped[Stock] = relationship(back_populates="analysis_snapshots")


class SecurityCatalog(Base):
    """Lokaler Suchkatalog, bewusst getrennt von den beobachteten Aktien."""

    __tablename__ = "security_catalog"
    __table_args__ = (
        UniqueConstraint("source", "symbol", name="uq_security_catalog_source_symbol"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    isin: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    exchange: Mapped[str | None] = mapped_column(String(128), nullable=True)
    country: Mapped[str | None] = mapped_column(String(64), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    security_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_symbol_alpha_vantage: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    provider_symbol_finnhub: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ApiUsage(Base):
    __tablename__ = "api_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    endpoint: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    data_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    http_status: Mapped[int | None] = mapped_column(nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ProviderCache(Base):
    __tablename__ = "provider_cache"
    __table_args__ = (
        UniqueConstraint(
            "provider", "symbol", "data_type", name="uq_provider_cache_item"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    data_type: Mapped[str] = mapped_column(String(64), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


class StockProviderSetting(Base):
    __tablename__ = "stock_provider_settings"
    __table_args__ = (
        UniqueConstraint("stock_id", "data_type", name="uq_stock_provider_data_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), nullable=False)
    data_type: Mapped[str] = mapped_column(String(32), nullable=False)
    primary_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    fallback_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    auto_select: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_success_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class StockProviderSymbol(Base):
    """Provider-spezifisches Symbol, getrennt vom lokalen Börsensymbol."""
    __tablename__ = "stock_provider_symbols"
    __table_args__ = (UniqueConstraint("stock_id","provider",name="uq_stock_provider_symbol"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"),nullable=False,index=True)
    provider: Mapped[str] = mapped_column(String(32),nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(32),nullable=True)
    status: Mapped[str] = mapped_column(String(32),nullable=False)
    source: Mapped[str] = mapped_column(String(32),nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),default=utc_now,onupdate=utc_now,nullable=False)


class ProviderCapability(Base):
    __tablename__ = "provider_capabilities"
    __table_args__ = (
        UniqueConstraint(
            "stock_id", "provider", "data_type", name="uq_stock_provider_capability"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    data_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CompanyData(Base):
    __tablename__ = "company_data"
    __table_args__ = (UniqueConstraint("stock_id", "provider", name="uq_company_provider"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    country: Mapped[str | None] = mapped_column(String(64), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    exchange: Mapped[str | None] = mapped_column(String(128), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(128), nullable=True)
    market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    pe_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_growth: Mapped[float | None] = mapped_column(Float, nullable=True)
    roe: Mapped[float | None] = mapped_column(Float, nullable=True)
    debt_to_equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class NewsItem(Base):
    __tablename__ = "news_items"
    __table_args__ = (UniqueConstraint("stock_id", "url", name="uq_stock_news_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), nullable=False)
    datetime: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    headline: Mapped[str] = mapped_column(String(500), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)


class AnalystRecommendation(Base):
    __tablename__ = "analyst_recommendations"
    __table_args__ = (UniqueConstraint("stock_id", "period", "provider", name="uq_analyst_period"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), nullable=False)
    period: Mapped[date] = mapped_column(Date, nullable=False)
    strong_buy: Mapped[int | None] = mapped_column(nullable=True)
    buy: Mapped[int | None] = mapped_column(nullable=True)
    hold: Mapped[int | None] = mapped_column(nullable=True)
    sell: Mapped[int | None] = mapped_column(nullable=True)
    strong_sell: Mapped[int | None] = mapped_column(nullable=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)


class EarningsResult(Base):
    __tablename__ = "earnings_results"
    __table_args__ = (UniqueConstraint("stock_id", "event_date", "provider", name="uq_earnings_event"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    estimated_eps: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_eps: Mapped[float | None] = mapped_column(Float, nullable=True)
    surprise_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)


class SchedulerSetting(Base):
    __tablename__ = "scheduler_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    scheduler_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class UpdateSchedule(Base):
    __tablename__ = "update_schedules"
    __table_args__ = (
        UniqueConstraint("stock_id", "data_type", name="uq_schedule_stock_data_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), nullable=False)
    data_type: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    schedule_type: Mapped[str] = mapped_column(String(16), default="cron", nullable=False)
    cron_expression: Mapped[str] = mapped_column(String(128), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    weekdays_only: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class SchedulerRun(Base):
    __tablename__ = "scheduler_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey("update_schedules.id"), nullable=False)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), nullable=False)
    data_type: Mapped[str] = mapped_column(String(32), nullable=False)
    planned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_used: Mapped[str | None] = mapped_column(String(32), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)


class AiUsage(Base):
    __tablename__ = "ai_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )
    stock_id: Mapped[int | None] = mapped_column(ForeignKey("stocks.id"), nullable=True)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    input_tokens: Mapped[int] = mapped_column(default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(default=0, nullable=False)
    estimated_cost_eur: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal("0"), nullable=False
    )
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tool_calls: Mapped[int] = mapped_column(default=0, nullable=False)


class StockAiAnalysis(Base):
    __tablename__ = "stock_ai_analysis"

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    rating: Mapped[str] = mapped_column(String(24), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    legacy_rating: Mapped[str | None] = mapped_column(String(24), nullable=True)
    position_rating: Mapped[str | None] = mapped_column(String(24), nullable=True)
    position_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_rating: Mapped[str | None] = mapped_column(String(24), nullable=True)
    entry_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    outlook: Mapped[str] = mapped_column(String(16), nullable=False)
    risk: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    position_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    entry_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reasons_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    risks_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    target_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    protect_profit_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    protect_profit_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    time_horizon: Mapped[str] = mapped_column(String(64), nullable=False)
    data_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


class AiNewsAssessment(Base):
    __tablename__ = "ai_news_assessments"
    __table_args__ = (
        UniqueConstraint("analysis_id", "news_item_id", name="uq_ai_analysis_news"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[int] = mapped_column(
        ForeignKey("stock_ai_analysis.id"), nullable=False, index=True
    )
    news_item_id: Mapped[int] = mapped_column(ForeignKey("news_items.id"), nullable=False)
    relevance: Mapped[str] = mapped_column(String(16), nullable=False)
    expected_direction: Mapped[str] = mapped_column(String(16), nullable=False)
    expected_strength: Mapped[str] = mapped_column(String(16), nullable=False)
    horizon: Mapped[str] = mapped_column(String(16), nullable=False)
    short_reason: Mapped[str] = mapped_column(String(500), nullable=False)


class ScannerRun(Base):
    __tablename__ = "scanner_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    market: Mapped[str] = mapped_column(String(32), nullable=False, default="NASDAQ")
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    catalog_total: Mapped[int] = mapped_column(default=0, nullable=False)
    candidates_total: Mapped[int] = mapped_column(default=0, nullable=False)
    candidates_checked: Mapped[int] = mapped_column(default=0, nullable=False)
    candidates_shortlisted: Mapped[int] = mapped_column(default=0, nullable=False)
    finnhub_requests: Mapped[int] = mapped_column(default=0, nullable=False)
    cache_hits: Mapped[int] = mapped_column(default=0, nullable=False)
    premium_skips: Mapped[int] = mapped_column(default=0, nullable=False)
    quality_excluded: Mapped[int] = mapped_column(default=0, nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)


class ScannerCandidate(Base):
    __tablename__ = "scanner_candidates"
    __table_args__ = (
        UniqueConstraint("scanner_run_id", "catalog_id", name="uq_scanner_run_catalog"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    scanner_run_id: Mapped[int] = mapped_column(ForeignKey("scanner_runs.id"), nullable=False, index=True)
    catalog_id: Mapped[int] = mapped_column(ForeignKey("security_catalog.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    pe_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_growth: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit_growth: Mapped[float | None] = mapped_column(Float, nullable=True)
    roe: Mapped[float | None] = mapped_column(Float, nullable=True)
    debt_to_equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    week_52_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    week_52_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    quality_score: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    opportunity_score: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    momentum_score: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    fundamental_score: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    analyst_score: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    risk_score: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    candidate_type: Mapped[str] = mapped_column(String(32), nullable=False, default="WATCH")
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ScannerSecurityState(Base):
    __tablename__ = "scanner_security_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    catalog_id: Mapped[int] = mapped_column(ForeignKey("security_catalog.id"), unique=True, nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class ScannerProviderCapability(Base):
    __tablename__ = "scanner_provider_capabilities"
    __table_args__ = (
        UniqueConstraint("catalog_id", "provider", "endpoint", name="uq_scanner_capability"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    catalog_id: Mapped[int] = mapped_column(ForeignKey("security_catalog.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ScannerTechnicalRun(Base):
    __tablename__ = "scanner_technical_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    scanner_run_id: Mapped[int] = mapped_column(ForeignKey("scanner_runs.id"), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    candidates_checked: Mapped[int] = mapped_column(default=0, nullable=False)
    finnhub_requests: Mapped[int] = mapped_column(default=0, nullable=False)
    alpha_vantage_requests: Mapped[int] = mapped_column(default=0, nullable=False)
    cache_hits: Mapped[int] = mapped_column(default=0, nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)


class ScannerCandidateTechnical(Base):
    __tablename__ = "scanner_candidate_technicals"
    __table_args__ = (
        UniqueConstraint("technical_run_id", "scanner_candidate_id", name="uq_technical_run_candidate"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    technical_run_id: Mapped[int] = mapped_column(ForeignKey("scanner_technical_runs.id"), nullable=False, index=True)
    scanner_candidate_id: Mapped[int] = mapped_column(ForeignKey("scanner_candidates.id"), nullable=False)
    history_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    trading_days: Mapped[int] = mapped_column(nullable=False)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    performance_5d: Mapped[float | None] = mapped_column(Float, nullable=True)
    performance_20d: Mapped[float | None] = mapped_column(Float, nullable=True)
    performance_60d: Mapped[float | None] = mapped_column(Float, nullable=True)
    sma20: Mapped[float | None] = mapped_column(Float, nullable=True)
    sma50: Mapped[float | None] = mapped_column(Float, nullable=True)
    sma200: Mapped[float | None] = mapped_column(Float, nullable=True)
    rsi14: Mapped[float | None] = mapped_column(Float, nullable=True)
    volatility_20d: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_sma20_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_sma50_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    trend: Mapped[str | None] = mapped_column(String(16), nullable=True)
    high_20d: Mapped[float | None] = mapped_column(Float, nullable=True)
    low_20d: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_high_20d_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_low_20d_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    high_60d: Mapped[float | None] = mapped_column(Float, nullable=True)
    low_60d: Mapped[float | None] = mapped_column(Float, nullable=True)
    momentum_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    opportunity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    candidate_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ScannerAiAssessment(Base):
    __tablename__ = "scanner_ai_assessments"

    id: Mapped[int] = mapped_column(primary_key=True)
    scanner_run_id: Mapped[int] = mapped_column(ForeignKey("scanner_runs.id"), nullable=False, index=True)
    technical_run_id: Mapped[int] = mapped_column(ForeignKey("scanner_technical_runs.id"), nullable=False, index=True)
    scanner_candidate_id: Mapped[int] = mapped_column(ForeignKey("scanner_candidates.id"), nullable=False)
    catalog_id: Mapped[int] = mapped_column(ForeignKey("security_catalog.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    entry_rating: Mapped[str] = mapped_column(String(24), nullable=False)
    entry_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    outlook: Mapped[str] = mapped_column(String(16), nullable=False)
    risk: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    entry_reason: Mapped[str] = mapped_column(Text, nullable=False)
    reasons_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    risks_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    entry_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    time_horizon: Mapped[str] = mapped_column(String(32), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


class ScannerAiNewsAssessment(Base):
    __tablename__ = "scanner_ai_news_assessments"
    id: Mapped[int] = mapped_column(primary_key=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("scanner_ai_assessments.id"), nullable=False, index=True)
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    relevance: Mapped[str] = mapped_column(String(16), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    strength: Mapped[str] = mapped_column(String(16), nullable=False)
    horizon: Mapped[str] = mapped_column(String(16), nullable=False)
    short_reason: Mapped[str] = mapped_column(String(500), nullable=False)
    important_event: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class OpportunityWatchlist(Base):
    __tablename__ = "opportunity_watchlist"
    id: Mapped[int] = mapped_column(primary_key=True)
    catalog_id: Mapped[int] = mapped_column(ForeignKey("security_catalog.id"), unique=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_opportunity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_classification: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_entry_rating: Mapped[str | None] = mapped_column(String(24), nullable=True)
    last_rsi14: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    weak_checks: Mapped[int] = mapped_column(default=0, nullable=False)
    favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    favorited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    initial_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    initial_quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    initial_opportunity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    initial_entry_rating: Mapped[str | None] = mapped_column(String(24), nullable=True)
    initial_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class OpportunityWatchObservation(Base):
    """Unveränderlicher Bewertungsstand eines manuellen Favoriten."""
    __tablename__ = "opportunity_watch_observations"
    __table_args__ = (UniqueConstraint("watchlist_id","scanner_candidate_id",name="uq_watch_observation_candidate"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    watchlist_id: Mapped[int] = mapped_column(ForeignKey("opportunity_watchlist.id"),nullable=False,index=True)
    scanner_candidate_id: Mapped[int] = mapped_column(ForeignKey("scanner_candidates.id"),nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),default=utc_now,nullable=False)
    price: Mapped[float | None] = mapped_column(Float,nullable=True)
    quality_score: Mapped[float | None] = mapped_column(Float,nullable=True)
    opportunity_score: Mapped[float | None] = mapped_column(Float,nullable=True)
    entry_rating: Mapped[str | None] = mapped_column(String(24),nullable=True)
    trend: Mapped[str | None] = mapped_column(String(16),nullable=True)
    performance_5d: Mapped[float | None] = mapped_column(Float,nullable=True)
    performance_20d: Mapped[float | None] = mapped_column(Float,nullable=True)
    performance_60d: Mapped[float | None] = mapped_column(Float,nullable=True)
    sma20: Mapped[float | None] = mapped_column(Float,nullable=True)
    sma50: Mapped[float | None] = mapped_column(Float,nullable=True)
    rsi14: Mapped[float | None] = mapped_column(Float,nullable=True)
    volatility_20d: Mapped[float | None] = mapped_column(Float,nullable=True)
    reason: Mapped[str] = mapped_column(Text,nullable=False)


class OpportunityCandidateSummary(Base):
    """Deduplizierte, zeitlich begrenzte Anzeige interessanter Kandidaten."""
    __tablename__ = "opportunity_candidate_summaries"
    id: Mapped[int] = mapped_column(primary_key=True)
    catalog_id: Mapped[int] = mapped_column(ForeignKey("security_catalog.id"), unique=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    latest_scanner_run_id: Mapped[int] = mapped_column(ForeignKey("scanner_runs.id"), nullable=False)
    latest_candidate_id: Mapped[int] = mapped_column(ForeignKey("scanner_candidates.id"), nullable=False)
    latest_technical_id: Mapped[int | None] = mapped_column(ForeignKey("scanner_candidate_technicals.id"), nullable=True)
    latest_ai_assessment_id: Mapped[int | None] = mapped_column(ForeignKey("scanner_ai_assessments.id"), nullable=True)
    latest_news_event_id: Mapped[int | None] = mapped_column(ForeignKey("opportunity_news_events.id"), nullable=True)
    quality_score: Mapped[float] = mapped_column(Float, nullable=False)
    opportunity_score: Mapped[float] = mapped_column(Float, nullable=False)
    classification: Mapped[str] = mapped_column(String(32), nullable=False)


class OpportunitySearchRun(Base):
    __tablename__ = "opportunity_search_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    scanner_run_id: Mapped[int | None] = mapped_column(ForeignKey("scanner_runs.id"), nullable=True)
    technical_run_id: Mapped[int | None] = mapped_column(ForeignKey("scanner_technical_runs.id"), nullable=True)
    candidates_found: Mapped[int] = mapped_column(default=0, nullable=False)
    finnhub_requests: Mapped[int] = mapped_column(default=0, nullable=False)
    alpha_vantage_requests: Mapped[int] = mapped_column(default=0, nullable=False)
    openai_requests: Mapped[int] = mapped_column(default=0, nullable=False)
    cache_hits: Mapped[int] = mapped_column(default=0, nullable=False)
    estimated_ai_cost_eur: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=0, nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    phase: Mapped[str | None] = mapped_column(String(32), nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checked_total: Mapped[int] = mapped_column(default=0, nullable=False)
    news_candidates: Mapped[int] = mapped_column(default=0, nullable=False)
    watchlist_candidates: Mapped[int] = mapped_column(default=0, nullable=False)
    rotation_candidates: Mapped[int] = mapped_column(default=0, nullable=False)
    finalists: Mapped[int] = mapped_column(default=0, nullable=False)
    current_symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    web_search_calls: Mapped[int] = mapped_column(default=0, nullable=False)
    news_found: Mapped[int] = mapped_column(default=0, nullable=False)


class OpportunityNewsEvent(Base):
    __tablename__ = "opportunity_news_events"
    __table_args__ = (
        UniqueConstraint("provider", "external_news_id", name="uq_opportunity_news_external"),
        UniqueConstraint("provider", "url", name="uq_opportunity_news_url"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_news_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    catalog_id: Mapped[int] = mapped_column(ForeignKey("security_catalog.id"), nullable=False)
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    event_score: Mapped[float] = mapped_column(Float, nullable=False)
    direction_hint: Mapped[str] = mapped_column(String(16), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opportunity_search_run_id: Mapped[int | None] = mapped_column(ForeignKey("opportunity_search_runs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ManualActionRun(Base):
    __tablename__ = "manual_action_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    action_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="RUNNING")
