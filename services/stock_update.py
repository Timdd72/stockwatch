"""Zentraler, fälligkeitsbasierter Aktualisierungsablauf pro Aktie."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from database.models import ApiUsage, CompanyData, Stock
from database.repository import StockWatchRepository
from market_data.usage import ApiLimitExceeded

from .market_updates import MarketUpdateError, MarketUpdateService
from .provider_settings import DATA_TYPES, ProviderSettingsService


UpdateStatus = Literal["updated", "current", "limit", "failed", "disabled"]


@dataclass(frozen=True, slots=True)
class UpdateItem:
    data_type: str
    provider: str | None
    status: UpdateStatus
    message: str


@dataclass(frozen=True, slots=True)
class StockUpdateReport:
    stock_id: int
    completed_at: datetime
    items: tuple[UpdateItem, ...]
    api_calls: int


class StockUpdateService:
    """Orchestriert vorhandene Updates, ohne Providerlogik zu duplizieren."""

    MAX_AGES = {
        "fundamentals": timedelta(hours=24),
        "news": timedelta(hours=4),
        "analyst": timedelta(hours=24),
        "earnings": timedelta(hours=24),
    }

    def __init__(self, session_factory: sessionmaker[Session], updates: MarketUpdateService,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._sessions = session_factory
        self._updates = updates
        self._settings = ProviderSettingsService(session_factory)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def update_stock(self, stock_id: int, force: bool = False) -> StockUpdateReport:
        stock = self._stock(stock_id)
        before = self._usage_count()
        items: list[UpdateItem] = []
        operations = {
            "quote": self._updates.update_quote,
            "history": self._updates.update_history,
            "fundamentals": self._updates.update_fundamentals,
            "news": self._updates.update_news,
            "analyst": self._updates.update_analyst,
            "earnings": self._updates.update_earnings,
        }
        for data_type in DATA_TYPES:
            setting = self._settings.get_setting(stock_id, data_type)
            if setting.primary_provider == "none":
                items.append(UpdateItem(data_type, None, "disabled", "nicht konfiguriert"))
                continue
            if data_type != "quote" and not force and self._is_current(stock, data_type, setting.last_checked_at):
                items.append(UpdateItem(data_type, setting.last_success_provider, "current", "bereits aktuell"))
                continue
            try:
                operations[data_type](stock_id)
                provider = self._settings.get_setting(stock_id, data_type).last_success_provider
                items.append(UpdateItem(data_type, provider, "updated", "aktualisiert"))
            except ApiLimitExceeded:
                items.append(UpdateItem(data_type, None, "limit", "lokales API-Limit erreicht"))
            except (MarketUpdateError, LookupError, ValueError):
                items.append(UpdateItem(data_type, None, "failed", "Aktualisierung nicht verfügbar"))
        return StockUpdateReport(stock_id, self._clock(), tuple(items), self._usage_count() - before)

    def _is_current(self, stock: Stock, data_type: str, last_success: datetime | None) -> bool:
        now = self._clock()
        if data_type == "history":
            with self._sessions() as session:
                technical = StockWatchRepository(session).get_latest_technical_snapshot(stock.id)
            if technical is None:
                return False
            zone = ZoneInfo("America/New_York" if stock.currency.upper() == "USD" or "NASDAQ" in stock.exchange.upper() else "Europe/Berlin")
            stamp = _aware(technical.timestamp)
            return stamp.astimezone(zone).date() == now.astimezone(zone).date()
        if last_success is None:
            return False
        if data_type == "fundamentals":
            with self._sessions() as session:
                if session.scalar(select(func.count()).select_from(CompanyData).where(CompanyData.stock_id == stock.id)) == 0:
                    return False
        return _aware(last_success) >= now - self.MAX_AGES[data_type]

    def _stock(self, stock_id: int) -> Stock:
        with self._sessions() as session:
            stock = session.get(Stock, stock_id)
            if stock is None:
                raise LookupError("Die Aktie wurde nicht gefunden.")
            return stock

    def _usage_count(self) -> int:
        with self._sessions() as session:
            return int(session.scalar(select(func.count()).select_from(ApiUsage)) or 0)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
